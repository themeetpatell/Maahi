"""Speaker — Piper / macOS `say` TTS with streaming and barge-in.

Why streaming: when the LLM produces 4 sentences, we don't want to wait
for the full reply before speaking. We pipe sentences to the engine as
soon as they're complete, which makes Maahi feel instant.

Why barge-in: `stop()` can interrupt speech at any moment. Playback no
longer holds a lock across its blocking wait, so `stop()` from another
thread (the barge-in watcher) takes effect immediately.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

from .config import get_config

log = logging.getLogger(__name__)


# Sentence-end regex — period/question/exclamation followed by whitespace or EOS.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[.!?])$")

# Bundled Piper voice filename (downloaded by setup.sh into <project>/voices/).
_DEFAULT_PIPER_VOICE = "en_US-lessac-medium.onnx"


def _piper_available(voice_path: str) -> bool:
    """True only if the piper binary is on PATH and the voice file exists."""
    if shutil.which("piper") is None:
        log.info("piper binary not on PATH.")
        return False
    if not Path(voice_path).exists():
        log.info("Piper voice file missing: %s", voice_path)
        return False
    return True


class Speaker:
    """A thread-safe TTS wrapper supporting interruption (barge-in).

    Locking model:
      - `_speak_lock` serializes whole utterances so two callers (e.g. the
        main loop and the proactive monitor) never overlap.
      - `_proc_lock` guards only the playback process handle and is held
        for microseconds — so `stop()` is never blocked by playback.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self.voice = cfg.tts.voice
        self.rate = cfg.tts.rate
        self._proc: subprocess.Popen[bytes] | None = None
        self._proc_lock = threading.Lock()
        self._speak_lock = threading.Lock()
        self._active = threading.Event()       # set while an utterance plays
        self._interrupted = threading.Event()  # set by stop()
        self._engine = cfg.tts.engine
        self._piper_voice = (
            cfg.tts.piper_voice
            or str(cfg.project_root / "voices" / _DEFAULT_PIPER_VOICE)
        )
        self._use_piper = (
            self._engine == "piper" and _piper_available(self._piper_voice)
        )
        if self._engine == "piper" and not self._use_piper:
            log.info("Piper unavailable — Speaker falling back to macOS 'say'.")

    # ----- one-shot speech -----
    def say(self, text: str) -> None:
        """Speak text and block until done (or interrupted)."""
        text = _clean_for_speech(text)
        if not text:
            return
        with self._speak_lock:
            self._interrupted.clear()
            self._active.set()
            try:
                log.debug("Speaking: %r", text)
                self._speak(text)
            finally:
                self._active.clear()

    def say_async(self, text: str) -> threading.Thread:
        """Speak text in a background thread; return the thread."""
        t = threading.Thread(target=self.say, args=(text,), daemon=True)
        t.start()
        return t

    # ----- streaming -----
    def stream(self, token_iter: Iterator[str]) -> str:
        """Speak as sentences arrive from a token stream. Returns full text.

        Stops early (returning whatever was collected) if `stop()` fires.
        """
        buf: list[str] = []
        full: list[str] = []
        with self._speak_lock:
            self._interrupted.clear()
            self._active.set()
            try:
                for token in token_iter:
                    buf.append(token)
                    full.append(token)
                    if self._interrupted.is_set():
                        break
                    joined = "".join(buf)
                    parts = _SENTENCE_END.split(joined)
                    if len(parts) > 1:
                        for sentence in parts[:-1]:
                            s = sentence.strip()
                            if s and not self._interrupted.is_set():
                                self._speak(s)
                        buf = [parts[-1]]
                tail = "".join(buf).strip()
                if tail and not self._interrupted.is_set():
                    self._speak(tail)
            finally:
                self._active.clear()
        return "".join(full)

    # ----- control -----
    def stop(self) -> None:
        """Interrupt whatever's being spoken right now."""
        self._interrupted.set()
        with self._proc_lock:
            proc = self._proc
            self._proc = None
        if proc and proc.poll() is None:
            proc.terminate()

    def is_speaking(self) -> bool:
        """True while an utterance is being produced or played."""
        return self._active.is_set()

    # ----- internals -----
    def _speak(self, text: str) -> None:
        """Speak one cleaned sentence via Piper, falling back to `say`."""
        if self._interrupted.is_set():
            return
        if self._use_piper and self._say_piper(text):
            return
        self._say_macos(text)

    def _run_playback(self, cmd: list[str]) -> None:
        """Run a playback command, tracking its process so stop() can kill it."""
        proc = subprocess.Popen(cmd)
        with self._proc_lock:
            self._proc = proc
        try:
            proc.wait()
        finally:
            with self._proc_lock:
                if self._proc is proc:
                    self._proc = None

    def _say_macos(self, text: str) -> None:
        """Speak via the macOS `say` binary."""
        self._run_playback(["say", "-v", self.voice, "-r", str(self.rate), text])

    def _say_piper(self, text: str) -> bool:
        """Synthesize with Piper and play via afplay.

        Returns True on success, False to signal the caller to fall back.
        """
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        wav_path = tmp.name
        tmp.close()
        try:
            gen = subprocess.run(
                ["piper", "-m", self._piper_voice, "-f", wav_path],
                input=text,
                text=True,
                capture_output=True,
                timeout=60,
            )
            if gen.returncode != 0:
                log.warning("Piper synth failed (%s) — falling back to say.",
                            gen.stderr.strip() or f"exit {gen.returncode}")
                return False
            if self._interrupted.is_set():
                return True  # interrupted during synth — skip playback
            self._run_playback(["afplay", wav_path])
            return True
        except FileNotFoundError as e:
            log.warning("Piper/afplay not found (%s) — falling back to say.", e)
            return False
        except subprocess.TimeoutExpired:
            log.warning("Piper synth timed out — falling back to say.")
            return False
        finally:
            try:
                Path(wav_path).unlink(missing_ok=True)
            except OSError:
                pass


# ============================================================
#  TEXT CLEANUP
# ============================================================
# `say` reads everything literally — including markdown asterisks, code
# fences, and stray symbols. Strip them before speaking.
# ============================================================

_CLEANUP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"```[^`]*```", re.S), " "),  # fenced code
    (re.compile(r"`([^`]*)`"), r"\1"),         # inline code
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),   # bold
    (re.compile(r"\*([^*]+)\*"), r"\1"),       # italic
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),  # markdown links → text only
    (re.compile(r"^#+\s*", re.M), ""),         # headers
    (re.compile(r"^\s*[-*+]\s+", re.M), ""),   # list bullets
    (re.compile(r"\s+"), " "),                 # collapse whitespace
]


def _clean_for_speech(text: str) -> str:
    for pat, repl in _CLEANUP_PATTERNS:
        text = pat.sub(repl, text)
    return text.strip()

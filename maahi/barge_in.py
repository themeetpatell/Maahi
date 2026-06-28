"""Barge-in watcher — let Meet interrupt Maahi mid-sentence.

While Maahi speaks, this drains mic frames and runs cheap STT on ~1s
windows. If Meet says the wake phrase or a configured stop phrase, the
watcher cuts the speaker off.

Why phrase-matching and not raw VAD: Maahi's TTS plays through the
speakers and the mic hears it. A volume/VAD trigger would make her
interrupt herself instantly. Matching specific phrases she'd never say
in a normal reply avoids that — though it still works best with
headphones, where there's no echo at all.
"""
from __future__ import annotations

import logging
import re
import time

from .audio_io import FRAME_MS, Microphone, _build_utterance
from .config import get_config
from .listener import matches_wake_phrase, transcribe
from .speaker import Speaker

log = logging.getLogger(__name__)

_WAKE_MODEL = "tiny.en"        # cheapest STT — this runs repeatedly during speech
_WINDOW_MS = 1000              # transcribe roughly one second of audio at a time
_START_TIMEOUT_S = 2.0         # how long to wait for speech to actually begin


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for loose matching."""
    norm = re.sub(r"[^a-z\s]", "", text.lower())
    return re.sub(r"\s+", " ", norm).strip()


class BargeInWatcher:
    """Monitors the mic during speech and interrupts on a stop phrase."""

    def __init__(self, mic: Microphone, speaker: Speaker) -> None:
        cfg = get_config()
        self._mic = mic
        self._speaker = speaker
        self._enabled = cfg.barge_in.enabled
        self._stop_words = cfg.barge_in.stop_words
        self._wake_phrases = cfg.wake.phrases

    def watch(self) -> None:
        """Block until Maahi stops speaking. Designed to run in a thread."""
        if not self._enabled:
            return
        if not self._await_speech_start():
            return

        window_frames = max(1, int(_WINDOW_MS / FRAME_MS))
        buf: list[bytes] = []
        while self._speaker.is_speaking():
            frame = self._mic.grab_frame(timeout=0.1)
            if frame is None:
                continue
            buf.append(frame)
            if len(buf) < window_frames:
                continue
            utt = _build_utterance(buf)
            buf = []
            text = transcribe(utt, model_name=_WAKE_MODEL).text
            if text and self._is_interrupt(text):
                log.info("Barge-in triggered: %r", text)
                self._speaker.stop()
                return

    # ----- internals -----

    def _await_speech_start(self) -> bool:
        """Wait briefly for the speaker to begin. False if it never does."""
        deadline = time.monotonic() + _START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._speaker.is_speaking():
                return True
            time.sleep(0.05)
        return self._speaker.is_speaking()

    def _is_interrupt(self, text: str) -> bool:
        """True if `text` contains a stop phrase or the wake phrase."""
        norm = _normalize(text)
        if not norm:
            return False
        if any(w in norm for w in self._stop_words):
            return True
        return matches_wake_phrase(norm, self._wake_phrases)

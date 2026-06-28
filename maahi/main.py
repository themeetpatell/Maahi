"""Maahi — main loop.

  while True:
    wait_for_wake()    # Mic is hot, listening for "Hey Maahi"
    transcribe()       # Full STT on the wake-utterance
    brain.respond()    # LLM + tools
    speaker.say()      # macOS `say`
"""
from __future__ import annotations

import logging
import random
import signal
import sys
import threading
from collections.abc import Callable

import time as _time

from . import fast_path, hud_server, hud_window, listening_state
from .audio_io import Microphone
from .barge_in import BargeInWatcher
from .brain import Brain
from .config import get_config
from .event_bus import bus, emit_error, emit_heard, emit_state, emit_transcript
from .hotkey import HotkeyListener
from .listener import (
    looks_like_command,
    matches_wake_phrase,
    transcribe,
    warmup_model,
)
from .memory import ConversationLog
from .personality import ERRORS, GREETINGS
from .proactive import ProactiveMonitor
from .speaker import Speaker

log = logging.getLogger("maahi")


# ============================================================
#  Force-wake state
#  When the HUD pulse-dot is clicked (or anything else publishes
#  `hud:wake_request`), we open a short window during which any
#  speech is treated as a command — no wake phrase required.
# ============================================================
_FORCE_WAKE_WINDOW_S = 6.0
_force_wake_until = 0.0
_force_wake_lock = threading.Lock()


def _arm_force_wake(window_s: float = _FORCE_WAKE_WINDOW_S) -> None:
    global _force_wake_until
    with _force_wake_lock:
        _force_wake_until = _time.time() + window_s
    log.info("Force-wake armed for %.1fs", window_s)


def _force_wake_active() -> bool:
    with _force_wake_lock:
        return _time.time() < _force_wake_until


def _setup_logging() -> None:
    cfg = get_config()
    cfg.logging.path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=cfg.logging.level,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        handlers=[
            logging.FileHandler(cfg.logging.path),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Silence the noisy "divide by zero in matmul" warnings that
    # faster-whisper emits on silent/short audio frames — they aren't
    # actionable and they spam the terminal.
    import warnings
    warnings.filterwarnings(
        "ignore",
        message=".*matmul.*",
        category=RuntimeWarning,
        module="faster_whisper.*",
    )
    warnings.filterwarnings("ignore", category=RuntimeWarning,
                            module="faster_whisper.*")
    # Faster-whisper logs every per-frame transcribe at INFO level — the
    # barge-in watcher hits this many times per second while Maahi speaks.
    # Bump it to WARNING so the terminal stays readable.
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _install_signal_handlers(speaker: Speaker) -> None:
    def _bye(signum, frame) -> None:  # noqa: ARG001
        log.info("Caught signal %d — shutting down", signum)
        speaker.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _bye)
    signal.signal(signal.SIGTERM, _bye)


def _strip_wake(text: str, phrases: tuple[str, ...]) -> str:
    """Remove the wake phrase (or a known phonetic mishear) from the front."""
    from .listener import _WAKE_PHONETIC_FALLBACKS

    norm = text.lstrip()
    low = norm.lower()
    # Configured phrases first (longest-prefix wins so we strip "hey maahi"
    # before falling back to "maahi" alone).
    for p in sorted(phrases, key=len, reverse=True):
        if low.startswith(p):
            return norm[len(p):].lstrip(" ,.!?")
    # Phonetic fallbacks — only match as a whole leading token, e.g.
    # "Mahi" or "Mommy, what time is it?" but not "marrying".
    first_token = low.split(" ", 1)[0].strip(" ,.!?")
    if first_token in _WAKE_PHONETIC_FALLBACKS:
        rest = norm.split(" ", 1)[1] if " " in norm else ""
        return rest.lstrip(" ,.!?")
    return norm


def _derive_command(
    text: str, forced: bool, wake_matched: bool, phrases: tuple[str, ...]
) -> str:
    """Turn a raw transcript into the bare command, given how it was gated.

    Mirrors the inline derivation in the wake loop so the two-tier STT
    escalation can re-derive a command from the higher-accuracy transcript.
    """
    if forced:
        return text.strip()
    if wake_matched:
        return _strip_wake(text, phrases).strip()
    return text.strip()


def run() -> None:
    """Orchestrator: HUD server + worker wake loop + HUD window on main thread."""
    _setup_logging()
    cfg = get_config()
    log.info("Maahi boot: starting")

    # Start HUD server (daemon thread, its own asyncio loop).
    if cfg.hud.enabled:
        log.info("Maahi boot: starting HUD server")
        hud_server.start_in_thread(cfg.hud)

    # Subscribe to listening_set events so the Pause toggle actually pauses.
    log.info("Maahi boot: arming listening-state subscriber")
    listening_state.start_listener()

    # Best-effort: spawn the menu-bar helper as a separate process. It runs
    # rumps (NSStatusItem) which insists on the macOS main thread — same as
    # pywebview — so it can't share this process.
    log.info("Maahi boot: spawning menu-bar helper (rumps + AppKit load — ~5s)")
    menubar_proc = _spawn_menubar()

    # Listener thread: turn HUD pulse-dot clicks (hud:wake_request) into
    # a force-wake window so the next utterance is treated as a command.
    def _hud_command_listener() -> None:
        import asyncio as _asyncio
        sub = bus().subscribe()

        async def _drain() -> None:
            while True:
                ev = await sub.get()
                if ev.type == "hud:wake_request":
                    _arm_force_wake()

        try:
            _asyncio.run(_drain())
        except Exception:  # noqa: BLE001
            log.exception("HUD command listener crashed")

    threading.Thread(target=_hud_command_listener,
                     name="hud-cmd-listener", daemon=True).start()

    # Global hotkey for force-wake from anywhere in macOS.
    log.info("Maahi boot: arming global hotkey")
    hotkey = HotkeyListener(cfg.hotkey)
    hotkey.start()

    # Rolling vision watcher — opt-in ambient screen awareness.
    log.info("Maahi boot: vision watcher (enabled=%s)", cfg.vision_watcher.enabled)
    from .vision_watcher import VisionWatcher
    watcher = VisionWatcher(cfg.vision_watcher)
    watcher.start()

    # Coordinated shutdown.
    stop_event = threading.Event()

    def _on_signal(signum, frame) -> None:  # noqa: ARG001
        log.info("Caught signal %d — shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # Wake loop on a worker thread so the macOS main thread is free for pywebview.
    log.info("Maahi boot: starting wake loop (mic + STT + brain)")
    worker = threading.Thread(
        target=_wake_loop, args=(stop_event,),
        name="maahi-wake", daemon=True,
    )
    worker.start()

    # Text-command worker: typed commands from the dashboard /api/command.
    threading.Thread(
        target=_text_command_worker, args=(stop_event,),
        name="text-cmd-worker", daemon=True,
    ).start()

    # Hand the main thread to the HUD window (blocks until closed), or
    # wait for the worker in headless mode.
    if cfg.hud.enabled:
        log.info("Maahi boot: opening HUD window (pywebview takes the main thread)")
        try:
            hud_window.run(cfg.hud)
        except KeyboardInterrupt:
            pass
        stop_event.set()
    else:
        try:
            while not stop_event.is_set():
                stop_event.wait(timeout=0.5)
        except KeyboardInterrupt:
            stop_event.set()

    log.info("Maahi shutting down…")
    worker.join(timeout=3.0)
    if menubar_proc is not None:
        try:
            menubar_proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def _text_command_worker(stop_event: threading.Event) -> None:
    """Process typed commands posted to /api/command.

    Subscribes to ``text_command`` events on the bus and runs each through
    its own ``Brain`` instance + ``Speaker``. Independent history from the
    voice path — fine for v1 — so a typed query doesn't confuse the voice
    conversation and vice versa.
    """
    import asyncio as _asyncio
    from .brain import Brain
    from .speaker import Speaker

    brain = Brain()
    speaker = Speaker()
    sub = bus().subscribe()

    async def _drain() -> None:
        while not stop_event.is_set():
            ev = await sub.get()
            if ev.type != "text_command":
                continue
            text = (ev.payload or {}).get("text", "").strip()
            if not text:
                continue
            log.info("Text command: %r", text)
            try:
                emit_transcript("user", text)
                emit_state("thinking")
                reply = brain.respond(text)  # emits maahi transcript + tool events
                emit_state("speaking")
                speaker.say(reply)
            except Exception as e:  # noqa: BLE001
                log.exception("Text command failed: %s", e)
                emit_error("text_command", str(e))
            finally:
                emit_state("idle")

    try:
        _asyncio.run(_drain())
    except Exception:  # noqa: BLE001
        log.exception("Text command worker crashed")


def _spawn_menubar():
    """Start the menu-bar helper subprocess. Returns Popen or None on failure."""
    try:
        import rumps  # noqa: F401  — probe so we don't spawn a noop process
    except ImportError:
        log.info("rumps not installed; menu-bar app skipped")
        return None
    try:
        import subprocess
        proc = subprocess.Popen(
            [sys.executable, "-m", "maahi.menubar"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        log.info("Menu-bar helper spawned (pid=%d)", proc.pid)
        return proc
    except Exception as e:  # noqa: BLE001
        log.warning("Could not spawn menu-bar helper: %s", e)
        return None


def _wake_loop(stop_event: threading.Event) -> None:
    """The voice loop. Runs on a worker thread; exits when stop_event is set."""
    cfg = get_config()
    speaker = Speaker()
    brain = Brain()
    convo = ConversationLog()
    monitor = ProactiveMonitor(speaker)

    log.info("Maahi online. Voice: %s | Model: %s", cfg.tts.voice, cfg.brain.model)
    emit_state("idle")
    speaker.say("Maahi online.")

    # Prewarm Ollama in the background so the first real command isn't
    # paying the model-load tax. Best-effort.
    threading.Thread(
        target=brain.prewarm, name="brain-prewarm", daemon=True,
    ).start()

    # Prewarm the Whisper tiers too, so the first utterance doesn't pay the
    # CTranslate2 cold-start. Warm fast model first (it's used most), then
    # the accurate model. Best-effort, off the hot path.
    def _warm_stt() -> None:
        names = [cfg.stt.fast_model, cfg.stt.model]
        for nm in dict.fromkeys(n for n in names if n):  # dedupe, keep order
            warmup_model(nm)

    threading.Thread(target=_warm_stt, name="stt-prewarm", daemon=True).start()

    # Lightweight model for the gating + fast-path pass. Falls back to the
    # accurate model if no fast tier is configured (two-tier disabled).
    fast_model = cfg.stt.fast_model or cfg.stt.model

    try:
        with Microphone() as mic:
            monitor.start()
            watcher = BargeInWatcher(mic, speaker)
            for utt in mic.utterances(silence_seconds=cfg.wake.silence_seconds):
                if stop_event.is_set():
                    break

                # Honor the Pause toggle from the HUD / menu bar. We still
                # drain the mic so audio doesn't pile up — we just don't act.
                if listening_state.is_paused():
                    continue

                # FAST pass: tiny.en transcribes for wake/command gating and
                # the fast-path. Quick is what matters here; the heavier model
                # only runs if we end up escalating to the LLM (below).
                full = transcribe(utt, model_name=fast_model)
                if not full.is_speech:
                    continue

                forced = _force_wake_active()
                wake_matched = matches_wake_phrase(full.text, cfg.wake.phrases)
                command_shaped = looks_like_command(full.text)

                if not (forced or wake_matched or command_shaped):
                    log.info("Heard (no wake): %r", full.text)
                    emit_heard(full.text)
                    continue

                if forced:
                    log.info("Force-wake (HUD click): %r", full.text)
                    _arm_force_wake(0.0)  # spend the window
                    command = full.text.strip()
                elif wake_matched:
                    log.info("Wake matched: %r", full.text)
                    command = _strip_wake(full.text, cfg.wake.phrases).strip()
                else:
                    # Command-shape fallback — no wake word, but the
                    # utterance starts with a clear command/question word.
                    log.info("Command-shape match: %r", full.text)
                    command = full.text.strip()
                emit_state("listening")

                # If the user only said "Hey Maahi" with no command, greet and wait.
                from_followup = False
                if not command:
                    greeting = random.choice(GREETINGS)
                    speaker.say(greeting)
                    convo.log_turn("maahi", greeting, {"reason": "greeting"})
                    emit_transcript("maahi", greeting)
                    command = _listen_followup(mic)
                    from_followup = True
                    if not command:
                        emit_state("idle")
                        continue

                log.info("Command: %r", command)
                convo.log_turn("user", command)
                emit_transcript("user", command)
                emit_state("thinking")

                # Fast-path: skip the LLM entirely for unambiguous intents
                # (time, open app, volume, lock, media). Siri-grade latency.
                fp_reply = fast_path.try_fast_path(command)
                if fp_reply is not None:
                    log.info("Fast-path reply: %r", fp_reply)
                    emit_state("speaking")
                    try:
                        _speak_interruptible(
                            mic, watcher, lambda: speaker.say(fp_reply),
                        )
                    except Exception as e:  # noqa: BLE001
                        log.exception("Speaker failed on fast-path: %s", e)
                    convo.log_turn("maahi", fp_reply, {"path": "fast_path"})
                    emit_transcript("maahi", fp_reply)
                    emit_state("idle")
                    continue

                # Escalating to the LLM: accuracy now matters more than the
                # last few milliseconds, so re-transcribe the utterance with
                # the heavier model. Skipped when the fast tier IS the main
                # model, or when the command came from a follow-up (whose
                # audio isn't this `utt`).
                if (
                    not from_followup
                    and fast_model != cfg.stt.model
                ):
                    accurate = transcribe(utt, model_name=cfg.stt.model)
                    if accurate.is_speech and accurate.text.strip():
                        refined = _derive_command(
                            accurate.text, forced, wake_matched, cfg.wake.phrases
                        )
                        if refined and refined != command:
                            log.info("Refined command (%s): %r",
                                     cfg.stt.model, refined)
                            command = refined
                            convo.log_turn("user", command, {"refined": True})
                            emit_transcript("user", command)

                try:
                    if cfg.tts.stream:
                        emit_state("speaking")
                        full_reply = _speak_interruptible(
                            mic, watcher,
                            lambda: speaker.stream(brain.stream_respond(command)),
                        )
                    else:
                        full_reply = brain.respond(command)
                        emit_state("speaking")
                        _speak_interruptible(
                            mic, watcher, lambda: speaker.say(full_reply),
                        )
                except Exception as e:  # noqa: BLE001
                    log.exception("Brain or speaker failed: %s", e)
                    err = random.choice(ERRORS)
                    emit_error("brain_or_speaker", str(e))
                    speaker.say(err)
                    convo.log_turn("maahi", err, {"error": str(e)})
                    emit_state("idle")
                    continue

                convo.log_turn("maahi", full_reply)
                emit_state("idle")
    except KeyboardInterrupt:
        log.info("Wake loop interrupted")
    finally:
        monitor.stop()
        speaker.stop()
        emit_state("idle")
        log.info("Maahi offline.")


def _speak_interruptible(
    mic: Microphone,
    watcher: BargeInWatcher,
    speak_fn: Callable[[], object],
) -> object:
    """Speak while a barge-in watcher listens for an interrupt.

    Runs the watcher in a side thread, speaks on this thread, then drops
    any mic frames captured during speech so Maahi's own echo doesn't
    get processed as a new command.
    """
    th = threading.Thread(target=watcher.watch, name="barge-in", daemon=True)
    th.start()
    try:
        return speak_fn()
    finally:
        th.join(timeout=1.5)
        mic.flush()


def _listen_followup(mic: Microphone, max_wait_s: float = 6.0) -> str:
    """After a bare wake, listen for the actual command."""
    cfg = get_config()
    # Pull next utterance, but bail if none arrives in time.
    import threading
    import queue as _q

    out: _q.Queue[str] = _q.Queue()

    def _worker() -> None:
        for utt in mic.utterances(silence_seconds=cfg.wake.silence_seconds):
            t = transcribe(utt)
            out.put(t.text if t.is_speech else "")
            return

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    try:
        return out.get(timeout=max_wait_s).strip()
    except _q.Empty:
        return ""


if __name__ == "__main__":
    run()

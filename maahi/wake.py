"""Wake-word loop.

We use a simple, reliable approach: continuous VAD → tiny Whisper transcribe →
substring match against the configured wake phrases. No custom model training,
no Picovoice license. Works on day one.

If you want lower CPU / higher accuracy later, swap to openwakeword by
implementing _OpenWakeWordDetector and flipping engine in config.yaml.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

from .audio_io import Microphone, Utterance
from .config import get_config
from .listener import matches_wake_phrase, transcribe

log = logging.getLogger(__name__)


def wait_for_wake() -> Utterance | None:
    """Block until the wake phrase is detected.

    Returns the FULL utterance that contained the wake word — so if Meet says
    "Hey Maahi, what's on my calendar today?" we already have the question.
    Returns None on KeyboardInterrupt.
    """
    cfg = get_config()
    log.info("Listening for wake phrase: %s", cfg.wake.phrases)

    try:
        with Microphone() as mic:
            for utt in mic.utterances(silence_seconds=cfg.wake.silence_seconds):
                # Use tiny.en for wake detection — cheap and fast.
                # If user already set tiny.en for STT, reuse it.
                wake_model = "tiny.en" if cfg.stt.model != "tiny.en" else cfg.stt.model
                t = transcribe(utt, model_name=wake_model)
                if not t.is_speech:
                    continue
                log.debug("Heard: %r", t.text)
                if matches_wake_phrase(t.text, cfg.wake.phrases):
                    log.info("Wake phrase matched: %r", t.text)
                    return utt
    except KeyboardInterrupt:
        return None
    return None


def stream_wake_events() -> Iterator[Utterance]:
    """Generator version — yields each wake-triggered utterance.

    Lets the main loop run forever without re-entering Microphone context.
    """
    while True:
        utt = wait_for_wake()
        if utt is None:
            return
        yield utt

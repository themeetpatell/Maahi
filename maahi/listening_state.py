"""Listening pause state.

Listens for ``listening_set`` events on the bus (published by
``POST /api/listening``) and exposes a thread-safe ``is_paused()``
predicate the wake loop checks before processing each utterance.

Kept in its own module so it's testable in isolation: no HUD, no
pywebview, no Ollama.
"""
from __future__ import annotations

import asyncio
import logging
import threading

from .event_bus import bus

log = logging.getLogger(__name__)

_paused = threading.Event()
_listener_started = False
_listener_lock = threading.Lock()
_LISTENER_THREAD: threading.Thread | None = None


def is_paused() -> bool:
    """True if listening is currently paused."""
    return _paused.is_set()


def set_paused(value: bool) -> None:
    """Direct setter — exposed for tests and admin scripts."""
    if value:
        _paused.set()
    else:
        _paused.clear()


def start_listener() -> threading.Thread:
    """Spin a daemon thread that flips _paused on listening_set events.

    Idempotent: calling more than once returns the original thread.
    """
    global _listener_started, _LISTENER_THREAD
    with _listener_lock:
        if _listener_started and _LISTENER_THREAD is not None:
            return _LISTENER_THREAD
        _listener_started = True

    def _run() -> None:
        sub = bus().subscribe()

        async def _drain() -> None:
            while True:
                ev = await sub.get()
                if ev.type != "listening_set":
                    continue
                value = bool((ev.payload or {}).get("paused"))
                set_paused(value)
                log.info("Listening %s", "paused" if value else "resumed")

        try:
            asyncio.run(_drain())
        except Exception:  # noqa: BLE001
            log.exception("listening_state listener crashed")

    th = threading.Thread(target=_run, name="listening-state", daemon=True)
    th.start()
    _LISTENER_THREAD = th
    return th

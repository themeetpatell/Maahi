"""Maahi event bus — in-process pub/sub.

The brain runs on a worker thread; the HUD server runs in an asyncio
loop. They meet here. Every interesting moment (wake, transcript, tool
call, speech chunk, vision capture, control action) becomes an Event
on this bus. The HUD subscribes and renders.

Design constraints:
- Thread-safe `publish()` — callable from any thread, never blocks the
  caller on a slow subscriber.
- Async-friendly `subscribe()` — yields events via `await sub.get()`.
- Bounded ring buffer for replay so the HUD can reconnect without
  missing recent history.
- Headless safe — if no one subscribes, events are still recorded in
  the ring buffer.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_SUBSCRIBER_QUEUE_MAX = 256
_REPLAY_BUFFER_MAX = 64


@dataclass(frozen=True)
class Event:
    """One thing that happened. Immutable by construction."""

    type: str
    payload: dict[str, Any]
    ts: float = field(default_factory=time.time)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "payload": self.payload,
            "ts": self.ts,
            "seq": self.seq,
        }


class Subscription:
    """Async-friendly handle to receive events.

    Wraps a thread-safe `queue.Queue` so producers on any thread can
    feed it, while consumers on an asyncio loop pull via `await get()`.
    """

    def __init__(self) -> None:
        self._q: queue.Queue[Event] = queue.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
        self._closed = False

    def _offer(self, ev: Event) -> None:
        """Non-blocking enqueue. Drops oldest on overflow."""
        if self._closed:
            return
        try:
            self._q.put_nowait(ev)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(ev)
            except queue.Full:
                log.debug("Subscription full after eviction; dropping event")

    async def get(self) -> Event:
        """Await the next event."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._q.get)

    def close(self) -> None:
        self._closed = True


class EventBus:
    """The bus itself. Thread-safe."""

    def __init__(self) -> None:
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()
        self._ring: deque[Event] = deque(maxlen=_REPLAY_BUFFER_MAX)
        self._seq = 0

    def publish(self, event_type: str, payload: dict[str, Any] | None = None) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(type=event_type, payload=payload or {}, seq=self._seq)
            self._ring.append(ev)
            subs = list(self._subs)
        for sub in subs:
            sub._offer(ev)
        return ev

    def subscribe(self) -> Subscription:
        sub = Subscription()
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        sub.close()
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    def recent(self, limit: int = _REPLAY_BUFFER_MAX) -> list[Event]:
        with self._lock:
            return list(self._ring)[-limit:]


_BUS: EventBus | None = None


def bus() -> EventBus:
    """Process-wide event bus singleton."""
    global _BUS
    if _BUS is None:
        _BUS = EventBus()
    return _BUS


# ----- Convenience publishers ------------------------------------------------


def emit_state(state: str, **extra: Any) -> None:
    """Top-level state: 'idle' | 'listening' | 'thinking' | 'speaking' | 'acting'."""
    bus().publish("state", {"state": state, **extra})


def emit_transcript(speaker: str, text: str, partial: bool = False) -> None:
    bus().publish("transcript", {"speaker": speaker, "text": text, "partial": partial})


def emit_heard(text: str) -> None:
    """Diagnostic: what the wake-check STT heard but didn't match as wake."""
    bus().publish("heard", {"text": text})


def emit_tool_start(name: str, args: dict[str, Any]) -> None:
    bus().publish("tool_start", {"name": name, "args": args})


def emit_tool_end(name: str, result: dict[str, Any]) -> None:
    bus().publish("tool_end", {"name": name, "result": result})


def emit_vision_capture(thumbnail_b64: str, question: str | None = None) -> None:
    bus().publish("vision_capture", {"thumb": thumbnail_b64, "question": question})


def emit_control_action(kind: str, **detail: Any) -> None:
    """kind: 'move' | 'click' | 'scroll' | 'type' | 'key' | 'ax'."""
    bus().publish("control_action", {"kind": kind, **detail})


def emit_speak_chunk(text: str) -> None:
    bus().publish("speak_chunk", {"text": text})


def emit_error(where: str, message: str) -> None:
    bus().publish("error", {"where": where, "message": message})

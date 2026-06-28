"""Rolling vision watcher — ambient screen awareness.

Background thread that snaps a screenshot every N seconds, sends it to the
local multimodal model, and keeps the last few one-line descriptions in a
thread-safe deque. The brain reads this buffer at prompt-assembly time so
Maahi can answer questions like "what was I just doing?" or "remind me what
that PR title was" without Meet having to ask in real time.

Design rules (strict):
  * Privacy-first. If the front app is in ``cfg.vision_watcher.privacy_apps``
    (Messages, password managers, etc.) the watcher SKIPS that tick. No
    capture taken, no model call.
  * Bounded RAM. Only the last N descriptions live in memory. The image
    bytes are discarded immediately after the model returns.
  * Strict local. Same Ollama endpoint as the brain; nothing leaves the Mac.
  * Throttled. Minimum 20s cadence enforced in config loader. Vision
    inference is expensive; you do not want this hot.
  * Crash-safe. Any exception in the loop is logged and the thread keeps
    polling. A broken capture never knocks Maahi offline.

This module is opt-in. ``cfg.vision_watcher.enabled = false`` (the default)
makes ``start()`` a no-op so nothing in this file ever runs on boot.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Deque

import httpx

from .config import VisionWatcherCfg, get_config
from .event_bus import bus

log = logging.getLogger(__name__)

_QUESTION = (
    "In one short sentence (under 18 words), describe what the user "
    "appears to be doing right now. Be concrete: name apps, document "
    "titles, or topics visible on screen. No fluff."
)


# ============================================================
#  OBSERVATION TYPES
# ============================================================


@dataclass(frozen=True)
class Observation:
    ts: str          # ISO8601 seconds
    app: str         # frontmost app at capture time
    text: str        # one-line description from the model


# ============================================================
#  SINGLETON BUFFER (read by brain.py at prompt time)
# ============================================================


_BUFFER: Deque[Observation] = deque(maxlen=1)
_BUFFER_LOCK = threading.Lock()


def recent_observations(limit: int = 0) -> tuple[Observation, ...]:
    """Snapshot of the most recent observations, newest last. Thread-safe."""
    with _BUFFER_LOCK:
        items = tuple(_BUFFER)
    if limit and limit < len(items):
        return items[-limit:]
    return items


def _push(obs: Observation) -> None:
    with _BUFFER_LOCK:
        _BUFFER.append(obs)


def _resize_buffer(maxlen: int) -> None:
    """Resize the singleton deque without dropping data older than necessary."""
    global _BUFFER
    with _BUFFER_LOCK:
        if _BUFFER.maxlen == maxlen:
            return
        _BUFFER = deque(_BUFFER, maxlen=maxlen)


# ============================================================
#  WATCHER
# ============================================================


class VisionWatcher:
    """Background thread that maintains the ambient observation buffer."""

    def __init__(self, cfg: VisionWatcherCfg | None = None) -> None:
        self._cfg = cfg or get_config().vision_watcher
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        _resize_buffer(self._cfg.buffer_size)

    def start(self) -> None:
        if not self._cfg.enabled:
            log.info("Vision watcher disabled in config.")
            return
        self._thread = threading.Thread(
            target=self._loop, name="vision-watcher", daemon=True,
        )
        self._thread.start()
        log.info(
            "Vision watcher on (interval=%ds, buffer=%d).",
            self._cfg.interval_seconds, self._cfg.buffer_size,
        )

    def stop(self) -> None:
        self._stop.set()

    # ----- internals -----

    def _loop(self) -> None:
        # First tick after one interval, not immediately — gives the rest of
        # boot some room.
        while not self._stop.wait(self._cfg.interval_seconds):
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                log.warning("Vision watcher tick failed: %s", e)

    def _tick(self) -> None:
        app = _front_app() or "Unknown"
        if app in self._cfg.privacy_apps:
            log.debug("Vision watcher skipping privacy app: %s", app)
            return

        # Lazy imports so a missing Pillow / httpx doesn't break Maahi boot.
        from .tools.vision import (
            _ask_ollama,
            _downscale_to_jpeg,
            _ensure_scratch,
            _make_thumbnail,
        )

        scratch = _ensure_scratch()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        png_path = scratch / f"watcher-{stamp}.png"

        try:
            subprocess.run(
                ["screencapture", "-x", "-C", str(png_path)],
                check=True, capture_output=True, timeout=8,
            )
        except subprocess.CalledProcessError as e:
            log.warning("Watcher capture failed: %s", (e.stderr or b"").decode(
                "utf-8", errors="ignore"))
            return
        except subprocess.TimeoutExpired:
            log.warning("Watcher capture timed out")
            return

        cfg = get_config()
        try:
            jpeg = _downscale_to_jpeg(
                png_path, cfg.vision.max_image_side, cfg.vision.jpeg_quality,
            )
        finally:
            try:
                Path(png_path).unlink(missing_ok=True)
            except OSError:
                pass

        try:
            text = _ask_ollama(_QUESTION, jpeg).strip().splitlines()[0][:200]
        except httpx.HTTPError as e:
            log.warning("Watcher vision call failed: %s", e)
            return

        if not text:
            return

        obs = Observation(
            ts=datetime.now().isoformat(timespec="seconds"),
            app=app,
            text=text,
        )
        _push(obs)
        bus().publish("vision_watcher", {
            "ts": obs.ts, "app": obs.app, "text": obs.text,
            "thumb": _make_thumbnail(jpeg),
        })
        log.info("Watcher [%s]: %s", obs.app, obs.text)


# ============================================================
#  FRONT-APP HELPER
# ============================================================


def _front_app() -> str | None:
    """Return the name of the macOS frontmost app, or None on failure."""
    try:
        proc = subprocess.run(
            [
                "osascript", "-e",
                'tell application "System Events" to '
                'get name of first process whose frontmost is true',
            ],
            capture_output=True, text=True, timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None

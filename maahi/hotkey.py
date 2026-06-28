"""Global hotkey listener — fires force-wake from anywhere on macOS.

Listens system-wide via ``pynput.keyboard.GlobalHotKeys`` so Meet can summon
Maahi without saying her name (useful in meetings or when his hands are on
the keyboard already). Translates a Maahi-style combo like ``cmd+option+m``
into pynput's ``<cmd>+<alt>+m`` notation, then publishes a
``hud:wake_request`` event — the same signal the HUD dot click produces.

Requires macOS Accessibility permission (already needed by Maahi for
AppleScript control, so this adds no new ask).

This module degrades gracefully:
  * ``pynput`` missing  → listener disabled, warning logged, Maahi continues
  * permission denied   → listener fails, warning logged, Maahi continues

We never raise into the wake loop — a broken hotkey must not crash voice.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from .config import HotkeyCfg
from .event_bus import bus

log = logging.getLogger(__name__)


# Map Maahi-flavored modifier names to pynput's tag syntax.
_PYNPUT_MODS: dict[str, str] = {
    "cmd": "<cmd>",
    "command": "<cmd>",
    "ctrl": "<ctrl>",
    "control": "<ctrl>",
    "alt": "<alt>",
    "option": "<alt>",
    "opt": "<alt>",
    "shift": "<shift>",
}


def _to_pynput(combo: str) -> str:
    """Translate 'cmd+option+m' → '<cmd>+<alt>+m'.

    Bare letters/digits pass through unchanged. Unknown tokens raise so the
    caller can surface the misconfig instead of silently never firing.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("hotkey combo is empty")
    out: list[str] = []
    for p in parts:
        if p in _PYNPUT_MODS:
            out.append(_PYNPUT_MODS[p])
        elif len(p) == 1 and (p.isalnum()):
            out.append(p)
        else:
            raise ValueError(f"unknown hotkey token: {p!r}")
    return "+".join(out)


class HotkeyListener:
    """Background daemon that publishes wake-requests on a global combo."""

    def __init__(self, cfg: HotkeyCfg) -> None:
        self._cfg = cfg
        self._listener: Optional[object] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Spin up the global listener. No-op when disabled or unavailable.

        The actual pynput import drags in Quartz / AppKit / ImageKit and can
        take 3-10 seconds on a cold cache. We do that work on a worker
        thread so the main boot path returns immediately and the wake loop
        starts in parallel.
        """
        if not self._cfg.enabled:
            log.info("Hotkey disabled in config.")
            return
        self._thread = threading.Thread(
            target=self._init_async, name="hotkey-init", daemon=True,
        )
        self._thread.start()

    def _init_async(self) -> None:
        """Heavy import + listener setup, off the boot path."""
        try:
            from pynput import keyboard  # heavy import; off the main thread
        except ImportError:
            log.warning(
                "pynput not installed — global hotkey unavailable. "
                "Run: pip install pynput",
            )
            return

        try:
            mapped = _to_pynput(self._cfg.combo)
        except ValueError as e:
            log.warning("Invalid hotkey combo %r: %s", self._cfg.combo, e)
            return

        def _on_fire() -> None:
            log.info("Global hotkey fired: %s", self._cfg.combo)
            bus().publish("hud:wake_request", {"source": "hotkey"})

        try:
            listener = keyboard.GlobalHotKeys({mapped: _on_fire})
            listener.daemon = True
            listener.start()
            self._listener = listener
            log.info("Global hotkey armed: %s (pynput=%s)", self._cfg.combo, mapped)
        except Exception as e:  # noqa: BLE001 — never crash the parent
            log.warning("Could not start hotkey listener: %s", e)

    def stop(self) -> None:
        listener = self._listener
        if listener is not None:
            try:
                listener.stop()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            self._listener = None

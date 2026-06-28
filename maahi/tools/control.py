"""Control tools — Maahi's hands on the Mac.

Three families:
  - cursor:   move / click / scroll
  - keyboard: type text / press key combos
  - ax:       semantic Accessibility actions via System Events

Cursor + keyboard use PyAutoGUI (CoreGraphics events). AX uses
`osascript` to talk to System Events — the right tool for "click the
Send button in Slack" without knowing pixel coordinates.

Every primitive publishes a `control_action` event so the HUD reflects
what Maahi did. Coordinates are clamped to the screen and logged.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Any

from ..config import get_config
from ..event_bus import emit_control_action

log = logging.getLogger(__name__)


_KEY_ALIASES: dict[str, str] = {
    "cmd": "command", "command": "command",
    "ctrl": "ctrl", "control": "ctrl",
    "opt": "option", "option": "option", "alt": "option",
    "shift": "shift",
    "enter": "enter", "return": "enter",
    "esc": "escape", "escape": "escape",
    "tab": "tab", "space": "space",
    "left": "left", "right": "right", "up": "up", "down": "down",
    "backspace": "backspace",
    "delete": "delete", "del": "delete",
    "home": "home", "end": "end",
    "pageup": "pageup", "pagedown": "pagedown",
}


def _parse_combo(combo: str) -> list[str]:
    """Turn 'cmd+shift+t' into ['command','shift','t']."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    return [_KEY_ALIASES.get(p, p) for p in parts]


def _screen_size() -> tuple[int, int]:
    import pyautogui
    return pyautogui.size()


def _clamp(x: int, y: int) -> tuple[int, int]:
    w, h = _screen_size()
    cx = max(0, min(w - 1, int(x)))
    cy = max(0, min(h - 1, int(y)))
    if (cx, cy) != (int(x), int(y)):
        log.info("Clamped (%s,%s) to (%s,%s)", x, y, cx, cy)
    return cx, cy


def _guard_enabled() -> dict[str, Any] | None:
    if not get_config().control.enabled:
        return {"ok": False, "error": "control disabled in config (control.enabled=false)"}
    return None


# ----- cursor ---------------------------------------------------------------


def cursor_move(x: int, y: int, duration_ms: int = 120) -> dict[str, Any]:
    """Move the mouse to (x, y). duration_ms controls smoothing."""
    bail = _guard_enabled()
    if bail:
        return bail
    import pyautogui

    cx, cy = _clamp(x, y)
    dur = max(0.0, duration_ms / 1000.0)
    try:
        pyautogui.moveTo(cx, cy, duration=dur)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"moveTo failed: {e}"}
    emit_control_action("move", x=cx, y=cy)
    return {"ok": True, "x": cx, "y": cy}


def cursor_click(
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    clicks: int = 1,
) -> dict[str, Any]:
    """Click at (x, y), or at current cursor if x/y omitted."""
    bail = _guard_enabled()
    if bail:
        return bail
    import pyautogui

    btn = button.lower()
    if btn not in ("left", "right", "middle"):
        return {"ok": False, "error": f"unknown button: {button}"}
    try:
        if x is not None and y is not None:
            cx, cy = _clamp(int(x), int(y))
            pyautogui.click(cx, cy, clicks=clicks, button=btn)
            pos = (cx, cy)
        else:
            pyautogui.click(clicks=clicks, button=btn)
            pos = pyautogui.position()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"click failed: {e}"}
    emit_control_action("click", x=pos[0], y=pos[1], button=btn, clicks=clicks)
    return {"ok": True, "x": pos[0], "y": pos[1], "button": btn, "clicks": clicks}


def cursor_scroll(dy: int) -> dict[str, Any]:
    """Vertical scroll. Positive = up, negative = down."""
    bail = _guard_enabled()
    if bail:
        return bail
    import pyautogui

    try:
        pyautogui.scroll(int(dy))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"scroll failed: {e}"}
    emit_control_action("scroll", dy=int(dy))
    return {"ok": True, "dy": int(dy)}


# ----- keyboard -------------------------------------------------------------


def keyboard_type(text: str, interval_ms: int = 15) -> dict[str, Any]:
    """Type the given text. interval_ms is per-character delay."""
    bail = _guard_enabled()
    if bail:
        return bail
    import pyautogui

    interval = max(0.0, interval_ms / 1000.0)
    try:
        pyautogui.typewrite(str(text), interval=interval)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"typewrite failed: {e}"}
    emit_control_action("type", chars=len(text))
    return {"ok": True, "chars": len(text)}


def keyboard_key(key: str) -> dict[str, Any]:
    """Press a key or key combo. Examples: 'return', 'cmd+t', 'cmd+shift+4'."""
    bail = _guard_enabled()
    if bail:
        return bail
    import pyautogui

    try:
        keys = _parse_combo(key)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"key press failed: {e}"}
    emit_control_action("key", combo="+".join(keys))
    return {"ok": True, "combo": "+".join(keys)}


# ----- AX (System Events) ---------------------------------------------------


def _osa(script: str, timeout_s: float = 15.0) -> str:
    p = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "osascript failed")
    return p.stdout.strip()


def ax_perform(app: str, action: str, target: str = "") -> dict[str, Any]:
    """Perform a semantic Accessibility action.

    action:
      - 'focus':  bring `app` to the front
      - 'click' | 'press': click the named element (`target` matches by
        name/title/description) inside `app`
      - 'menu':   click a menu path like 'File > New Window' in `app`
    """
    bail = _guard_enabled()
    if bail:
        return bail

    app_q = app.replace('"', '\\"')
    target_q = target.replace('"', '\\"')
    act = action.lower()

    try:
        if act == "focus":
            _osa(f'tell application "{app_q}" to activate')
            emit_control_action("ax", app=app, action="focus")
            return {"ok": True, "app": app, "action": "focus"}

        if act in ("click", "press"):
            if not target_q:
                return {"ok": False, "error": "target required for click/press"}
            script = (
                f'tell application "System Events" to '
                f'tell process "{app_q}" to click '
                f'(first button whose name is "{target_q}" '
                f'or title is "{target_q}" '
                f'or description is "{target_q}")'
            )
            _osa(script)
            emit_control_action("ax", app=app, action=act, target=target)
            return {"ok": True, "app": app, "action": act, "target": target}

        if act == "menu":
            parts = [p.strip() for p in target.split(">") if p.strip()]
            if not parts:
                return {"ok": False, "error": "menu target empty"}
            top = parts[0].replace('"', '\\"')
            chain = (
                " of ".join(f'menu item "{p}"' for p in reversed(parts[1:]))
                if len(parts) > 1
                else ""
            )
            chain_part = f"{chain} of " if chain else ""
            script = (
                f'tell application "System Events" to '
                f'tell process "{app_q}" to click '
                f'{chain_part}menu "{top}" of menu bar 1'
            )
            _osa(script)
            emit_control_action("ax", app=app, action="menu", target=target)
            return {"ok": True, "app": app, "action": "menu", "target": target}

        return {"ok": False, "error": f"unknown ax action: {action}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "AX action timed out"}
    except RuntimeError as e:
        return {"ok": False, "error": f"AX failed: {e}"}

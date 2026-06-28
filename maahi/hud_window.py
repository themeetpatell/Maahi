"""HUD windows — pywebview wrappers.

Two windows share one pywebview process:

  1. Floating HUD  — frameless, transparent, always-on-top. Created at
                     boot. Shows ambient state, transcripts, the pulse dot,
                     try-saying chips. Has an ⤢ Expand button that calls
                     ``window.pywebview.api.open_dashboard()``.

  2. Command Center dashboard  — full-window, resizable, with frame.
                     Lazily created the first time the user expands. From
                     then on, ``open_dashboard()`` raises the existing
                     window instead of duplicating it.

pywebview requires both window creations to happen on the macOS main
thread. The first is created in ``run()``; the second is created
synchronously from inside ``MaahiAPI.open_dashboard()`` which is invoked
on the main thread by pywebview's JS bridge.
"""
from __future__ import annotations

import logging
from typing import Any

from .config import HudCfg

log = logging.getLogger(__name__)

DASHBOARD_WIDTH = 1280
DASHBOARD_HEIGHT = 820
DASHBOARD_MIN_W = 900
DASHBOARD_MIN_H = 600


def _resolve_position(cfg: HudCfg) -> tuple[int, int]:
    """Translate negative offsets to absolute screen coords."""
    x, y = cfg.x, cfg.y
    if x >= 0 and y >= 0:
        return x, y
    try:
        import pyautogui
        sw, sh = pyautogui.size()
    except Exception:  # noqa: BLE001
        sw, sh = 1440, 900
    if x < 0:
        x = max(0, sw + x - cfg.width)
    if y < 0:
        y = max(0, sh + y - cfg.height)
    return int(x), int(y)


class MaahiAPI:
    """JS bridge exposed to the floating HUD.

    Methods here become callable from JS as
    ``await window.pywebview.api.<method_name>(...)``.
    """

    def __init__(self, cfg: HudCfg) -> None:
        self.cfg = cfg
        self._dashboard_win: Any = None

    def open_dashboard(self) -> dict[str, Any]:
        try:
            import webview
        except ImportError:
            return {"ok": False, "error": "pywebview not installed"}

        existing = self._dashboard_win
        if existing is not None:
            try:
                existing.show()
                existing.restore()
                return {"ok": True, "action": "raised"}
            except Exception as e:  # noqa: BLE001
                log.debug("Could not raise existing dashboard window: %s", e)
                self._dashboard_win = None

        url = f"http://127.0.0.1:{self.cfg.port}/dashboard"
        try:
            self._dashboard_win = webview.create_window(
                title="Maahi — Command Center",
                url=url,
                width=DASHBOARD_WIDTH,
                height=DASHBOARD_HEIGHT,
                min_size=(DASHBOARD_MIN_W, DASHBOARD_MIN_H),
                resizable=True,
                frameless=False,
                on_top=False,
                background_color="#06080F",
            )
            return {"ok": True, "action": "opened"}
        except Exception as e:  # noqa: BLE001
            log.exception("Could not open dashboard window: %s", e)
            return {"ok": False, "error": str(e)}


def run(cfg: HudCfg) -> None:
    """Block on the main thread running the floating HUD (lazy dashboard)."""
    if not cfg.enabled:
        log.info("HUD window disabled in config")
        return
    try:
        import webview  # pywebview
    except ImportError:
        log.error("pywebview not installed — HUD window cannot launch")
        return

    x, y = _resolve_position(cfg)
    url = f"http://127.0.0.1:{cfg.port}/"
    api = MaahiAPI(cfg)

    create_kwargs: dict[str, Any] = dict(
        title="Maahi",
        url=url,
        width=cfg.width,
        height=cfg.height,
        x=x,
        y=y,
        resizable=False,
        frameless=True,
        easy_drag=True,
        on_top=cfg.always_on_top,
        transparent=cfg.transparent,
        js_api=api,
    )
    if not cfg.transparent:
        create_kwargs["background_color"] = "#0A0E18"

    try:
        webview.create_window(**create_kwargs)
    except TypeError as e:
        log.warning("pywebview rejected kwargs (%s); retrying with minimum set", e)
        webview.create_window(
            title="Maahi",
            url=url,
            width=cfg.width,
            height=cfg.height,
            x=x, y=y,
            resizable=False,
            frameless=True,
            on_top=cfg.always_on_top,
            js_api=api,
        )

    webview.start()

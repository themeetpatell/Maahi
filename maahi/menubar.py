"""macOS menu-bar app for Maahi.

Runs as a separate process so pywebview can keep the main thread for the
floating HUD. Talks to the live HUD server over its REST endpoints.

Launch standalone:

    python -m maahi.menubar

main.py auto-spawns this when ``rumps`` is installed.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import webbrowser

import httpx

log = logging.getLogger(__name__)

HUD_HOST = "http://127.0.0.1:7421"
POLL_SECONDS = 5


def _http() -> httpx.Client:
    return httpx.Client(base_url=HUD_HOST, timeout=httpx.Timeout(2.0, connect=1.0))


def _open_url(url: str) -> None:
    try:
        subprocess.Popen(["open", url])
    except FileNotFoundError:
        webbrowser.open(url)


def main() -> int:
    try:
        import rumps
    except ImportError:
        log.error("rumps is not installed. Run `pip install rumps`.")
        return 1

    class MaahiApp(rumps.App):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__("Maahi", icon=None, quit_button=None)
            self.title = "•"
            self.is_paused = False
            self.menu = [
                rumps.MenuItem("Status: …", callback=None),
                None,
                rumps.MenuItem("Open Command Center",  callback=self._open_dashboard),
                rumps.MenuItem("Open HUD",             callback=self._open_hud),
                rumps.MenuItem("Pause listening",      callback=self._toggle_listen),
                None,
                rumps.MenuItem("Settings…",            callback=self._open_settings),
                rumps.MenuItem("Permissions…",         callback=self._open_perms),
                rumps.MenuItem("Memory…",              callback=self._open_memory),
                rumps.MenuItem("Skills…",              callback=self._open_skills),
                rumps.MenuItem("Logs…",                callback=self._open_logs),
                None,
                rumps.MenuItem("Quit menu bar",        callback=self._quit),
            ]
            rumps.Timer(self._poll, POLL_SECONDS).start()
            self._poll(None)

        def _open_hud(self, _) -> None:
            _open_url(HUD_HOST)

        def _open_dashboard(self, _) -> None:
            _open_url(HUD_HOST + "/dashboard")

        def _open_memory(self, _) -> None:
            _open_url(HUD_HOST + "/dashboard#memory")

        def _open_skills(self, _) -> None:
            _open_url(HUD_HOST + "/dashboard#skills")

        def _open_logs(self, _) -> None:
            _open_url(HUD_HOST + "/dashboard#logs")

        def _toggle_listen(self, _) -> None:
            self.is_paused = not self.is_paused
            try:
                with _http() as c:
                    c.post("/api/listening", json={"paused": self.is_paused})
            except httpx.HTTPError as e:
                log.warning("listening toggle failed: %s", e)
                rumps.notification(
                    "Maahi", "Could not reach Maahi",
                    "Is the main process running?",
                )
                self.is_paused = not self.is_paused
                return
            self.menu["Pause listening"].title = (
                "Resume listening" if self.is_paused else "Pause listening"
            )

        def _open_settings(self, _) -> None:
            _open_url(HUD_HOST + "/dashboard#voice")

        def _open_perms(self, _) -> None:
            _open_url(HUD_HOST + "/dashboard#permissions")

        def _quit(self, _) -> None:
            rumps.quit_application()

        def _poll(self, _: object) -> None:
            online = False
            try:
                with _http() as c:
                    r = c.get("/health")
                    online = (r.status_code == 200)
            except httpx.HTTPError:
                online = False
            if not online:
                self.title = "•"
                self.menu["Status: …"].title = "Status: offline"
            elif self.is_paused:
                self.title = "⏸"
                self.menu["Status: …"].title = "Status: paused"
            else:
                self.title = "○"
                self.menu["Status: …"].title = "Status: online"

    MaahiApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

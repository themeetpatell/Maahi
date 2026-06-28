"""HUD server — FastAPI + WebSocket bridge to the dashboard.

Serves the static HUD on http://127.0.0.1:<port>/ and a WebSocket at
/ws that streams `event_bus` events as JSON to the HUD.

Also exposes a REST surface for the Settings panel, privacy view, and
first-run permission wizard:

  GET  /api/config              → editable config subset
  POST /api/config              → atomic write + reload
  GET  /api/voices              → macOS `say -v ?` parsed
  POST /api/audition            → speak a sample in a voice
  GET  /api/brain/probe         → Ollama health + latency
  GET  /api/permissions         → probe Mic / Accessibility / Screen / Automation
  POST /api/permissions/open    → deep-link to a Settings pane
  GET  /api/transcript/today    → today's conversation turns
  POST /api/transcript/clear    → drop today's transcript
  POST /api/listening           → pause/resume the mic loop
  GET  /api/skills              → list installed skill packs

Loopback only. No auth — relies on the bind address.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Dict, Optional

from .config import HudCfg, get_config
from .event_bus import bus, emit_state


def _expected_token() -> str:
    """Token required on /api/* and /ws. Empty string disables auth."""
    try:
        return get_config().hud_auth.token or ""
    except Exception:  # noqa: BLE001 — config can fail during early boot
        return ""


def _check_bearer(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency. No-op when token empty; else require Bearer match."""
    expected = _expected_token()
    if not expected:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization.split(" ", 1)[1].strip() != expected:
        raise HTTPException(status_code=401, detail="bad token")


def _inject_token(html: str, token: str) -> str:
    """Inject ``window.MAAHI_TOKEN`` so the HUD JS can authenticate calls.

    Empty token still injects an empty string — keeps the JS contract stable.
    """
    snippet = f'<script>window.MAAHI_TOKEN={token!r};</script>\n'
    if "</head>" in html:
        return html.replace("</head>", snippet + "</head>", 1)
    return snippet + html


# Pydantic request models. Module-level so type resolution works under 3.9.

class ConfigPatch(BaseModel):
    patch: Dict[str, Any]


class AuditionBody(BaseModel):
    voice: str
    text: Optional[str] = None


class OpenPaneBody(BaseModel):
    pane: str


class ListeningBody(BaseModel):
    paused: bool


class CommandBody(BaseModel):
    text: str

log = logging.getLogger(__name__)

HUD_DIR = Path(__file__).resolve().parent / "hud"


def _build_app() -> FastAPI:
    app = FastAPI(title="Maahi HUD", version="1.0")

    if HUD_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(HUD_DIR)), name="static")

    # ---- Auth middleware: guards /api/* and /ws when token configured ----
    @app.middleware("http")
    async def _auth_mw(request, call_next):  # type: ignore[no-untyped-def]
        expected = _expected_token()
        path = request.url.path
        if expected and (path.startswith("/api/") or path == "/ws"):
            hdr = request.headers.get("authorization", "")
            qtok = request.query_params.get("token", "")
            ok = False
            if hdr.lower().startswith("bearer "):
                ok = hdr.split(" ", 1)[1].strip() == expected
            if not ok and qtok:
                ok = qtok == expected
            if not ok:
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    {"ok": False, "error": "unauthorized"}, status_code=401,
                )
        return await call_next(request)

    @app.get("/")
    async def root() -> Any:
        index = HUD_DIR / "index.html"
        if not index.exists():
            return {"error": "HUD assets missing", "expected": str(index)}
        return HTMLResponse(_inject_token(index.read_text(encoding="utf-8"),
                                          _expected_token()))

    @app.get("/dashboard")
    async def dashboard() -> Any:
        path = HUD_DIR / "dashboard.html"
        if not path.exists():
            return {"error": "Dashboard assets missing", "expected": str(path)}
        return HTMLResponse(_inject_token(path.read_text(encoding="utf-8"),
                                          _expected_token()))

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    # ----------------------------------------------------------
    # Settings panel
    # ----------------------------------------------------------

    @app.get("/api/config")
    async def get_config_view() -> dict[str, Any]:
        from . import settings_api
        return {"ok": True, "config": settings_api.public_view()}

    @app.post("/api/config")
    async def post_config(body: ConfigPatch) -> dict[str, Any]:
        from . import settings_api
        res = settings_api.write(body.patch)
        if not res.ok:
            raise HTTPException(status_code=500, detail=res.error or "write failed")
        return {
            "ok": True,
            "written": res.written,
            "backup": res.backup_path,
        }

    @app.get("/api/voices")
    async def voices() -> dict[str, Any]:
        from . import settings_api
        return {"ok": True, "voices": settings_api.list_macos_voices()}

    @app.post("/api/audition")
    async def audition(body: AuditionBody) -> dict[str, Any]:
        from . import settings_api
        return settings_api.audition_voice(body.voice, body.text)

    @app.get("/api/brain/probe")
    async def brain_probe() -> dict[str, Any]:
        from . import settings_api
        return settings_api.probe_brain()

    # ----------------------------------------------------------
    # Permissions wizard
    # ----------------------------------------------------------

    @app.get("/api/permissions")
    async def permissions() -> dict[str, Any]:
        from . import permissions as perms
        return {"ok": True, "status": perms.probe_all()}

    @app.post("/api/permissions/open")
    async def open_pane(body: OpenPaneBody) -> dict[str, Any]:
        from . import permissions as perms
        return perms.open_settings(body.pane)

    # ----------------------------------------------------------
    # Privacy view — transcript + listening control
    # ----------------------------------------------------------

    @app.get("/api/transcript/today")
    async def transcript_today() -> dict[str, Any]:
        from . import privacy
        return privacy.todays_turns()

    @app.post("/api/transcript/clear")
    async def transcript_clear() -> dict[str, Any]:
        from . import privacy
        return privacy.clear_today()

    @app.post("/api/listening")
    async def listening(body: ListeningBody) -> dict[str, Any]:
        bus().publish("listening_set", {"paused": body.paused})
        emit_state("paused" if body.paused else "idle")
        return {"ok": True, "paused": body.paused}

    # ----------------------------------------------------------
    # Skills catalog (read-only)
    # ----------------------------------------------------------

    @app.get("/api/skills")
    async def skills() -> dict[str, Any]:
        from .tools.registry import TOOLS
        return {
            "ok": True,
            "count": len(TOOLS),
            "tools": [
                {"name": t.name, "description": t.description,
                 "args": dict(t.arg_schema)}
                for t in TOOLS
            ],
        }

    # ----------------------------------------------------------
    # Dashboard backend
    # ----------------------------------------------------------

    @app.post("/api/command")
    async def submit_text_command(body: CommandBody) -> dict[str, Any]:
        from . import dashboard_api
        return dashboard_api.submit_command(body.text)

    @app.get("/api/memory/facts")
    async def memory_facts() -> dict[str, Any]:
        from . import dashboard_api
        return dashboard_api.get_facts()

    @app.get("/api/memory/preferences")
    async def memory_prefs() -> dict[str, Any]:
        from . import dashboard_api
        return dashboard_api.get_preferences()

    @app.get("/api/memory/index_stats")
    async def memory_index() -> dict[str, Any]:
        from . import dashboard_api
        return dashboard_api.get_index_stats()

    @app.get("/api/logs")
    async def logs(lines: int = 200) -> dict[str, Any]:
        from . import dashboard_api
        return dashboard_api.tail_logs(lines)

    @app.post("/api/dashboard/open")
    async def open_dashboard() -> dict[str, Any]:
        """Publish a dashboard:open event. The main process listens and
        either spawns the pywebview window (if no other is open) or opens
        the dashboard URL in the default browser as fallback."""
        bus().publish("dashboard:open", {})
        return {"ok": True}

    @app.get("/api/calendar/upcoming")
    async def calendar_upcoming(minutes: int = 480) -> dict[str, Any]:
        from .tools import calendar_tool
        try:
            return calendar_tool.events_starting_within(minutes=minutes)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), "events": []}

    @app.get("/api/reminders/open")
    async def reminders_open(limit: int = 10) -> dict[str, Any]:
        from .tools import reminders
        try:
            return reminders.list_open(limit=limit)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), "reminders": []}

    @app.websocket("/ws")
    async def ws(ws: WebSocket) -> None:
        expected = _expected_token()
        if expected:
            qtok = ws.query_params.get("token", "")
            if qtok != expected:
                await ws.close(code=4401, reason="unauthorized")
                return
        await ws.accept()
        log.info("HUD WS connected")
        sub = bus().subscribe()

        for ev in bus().recent(limit=32):
            try:
                await ws.send_text(json.dumps({"replay": True, **ev.to_dict()}))
            except Exception:
                pass

        async def pump_outbound() -> None:
            try:
                while True:
                    ev = await sub.get()
                    await ws.send_text(json.dumps(ev.to_dict()))
            except WebSocketDisconnect:
                pass
            except Exception as e:  # noqa: BLE001
                log.warning("HUD outbound pump stopped: %s", e)

        async def pump_inbound() -> None:
            try:
                while True:
                    raw = await ws.receive_text()
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    kind = msg.get("type")
                    if not kind:
                        continue
                    bus().publish(f"hud:{kind}", msg.get("payload") or {})
            except WebSocketDisconnect:
                pass
            except Exception as e:  # noqa: BLE001
                log.warning("HUD inbound pump stopped: %s", e)

        try:
            await asyncio.gather(pump_outbound(), pump_inbound())
        finally:
            bus().unsubscribe(sub)
            log.info("HUD WS disconnected")

    return app


def start_in_thread(cfg: HudCfg) -> uvicorn.Server | None:
    """Spin uvicorn on a daemon thread. Returns Server (None if disabled)."""
    if not cfg.enabled:
        log.info("HUD disabled in config")
        return None

    app = _build_app()
    uvi_cfg = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=cfg.port,
        log_level="warning",
        loop="asyncio",
        access_log=False,
    )
    server = uvicorn.Server(uvi_cfg)

    def _run() -> None:
        try:
            asyncio.run(server.serve())
        except Exception as e:  # noqa: BLE001
            log.exception("HUD server crashed: %s", e)

    th = threading.Thread(target=_run, name="hud-server", daemon=True)
    th.start()
    log.info("HUD server listening on http://127.0.0.1:%d", cfg.port)
    return server

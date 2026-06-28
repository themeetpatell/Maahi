"""Maahi command-center — FastAPI server + native chat cockpit.

This is the cockpit that replaces "open Slack and five dashboards". It serves
a single-page UI and a small JSON/SSE API over the Operator:

  GET  /                 the cockpit (web/index.html)
  GET  /healthz          liveness
  GET  /api/status       systems, autonomy, brain state, counts
  GET  /api/brief        the executive brief (?synthesize=0 to skip Claude)
  POST /api/chat         stream a chat turn as SSE events
  GET  /api/pending      actions parked for approval
  POST /api/approve      {id} run a parked action
  POST /api/reject       {id} drop a parked action
  GET  /api/ledger       recent audit entries
  POST /api/autonomy     {mode} set suggest|act_report|autopilot

Auth: if ``MAAHI_OPERATOR_TOKEN`` is set, every /api route requires
``Authorization: Bearer <token>``. Unset → open (fine for localhost).

The chat stream uses plain ``fetch`` + ReadableStream on the client (not
EventSource) so the auth header travels with it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import get_operator_config
from .core import get_operator

log = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent / "web"


def _placeholder_html() -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>Maahi</title>"
        "<body style='font:16px system-ui;background:#0b0f14;color:#cde;"
        "padding:3rem;max-width:40rem;margin:auto'>"
        "<h1>Maahi Operator</h1><p>Command-center API is live. The cockpit UI "
        "(<code>web/index.html</code>) is not present in this build.</p>"
        "<p>Try <code>GET /api/status</code> or <code>GET /api/brief</code>.</p>"
    )


def _index_html() -> str:
    path = _WEB_DIR / "index.html"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return _placeholder_html()


def create_app():
    """Build the FastAPI app. Imported lazily so the package stays light."""
    from fastapi import Depends, FastAPI, Header, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

    cfg = get_operator_config()
    app = FastAPI(title="Maahi Operator", version="2.0.0")

    def _auth(authorization: str | None = Header(default=None)) -> None:
        token = cfg.auth_token
        if not token:
            return
        expected = f"Bearer {token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _index_html()

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "service": "maahi-operator"}

    @app.get("/api/status")
    def status(_: None = Depends(_auth)) -> dict[str, Any]:
        return get_operator().status()

    @app.get("/api/brief")
    def brief(synthesize: int = 1, _: None = Depends(_auth)) -> dict[str, Any]:
        return get_operator().brief(synthesize=bool(synthesize)).to_dict()

    @app.get("/api/pending")
    def pending(_: None = Depends(_auth)) -> dict[str, Any]:
        return {"pending": get_operator().pending()}

    @app.get("/api/ledger")
    def ledger(limit: int = 50, _: None = Depends(_auth)) -> dict[str, Any]:
        return {"entries": get_operator().ledger_recent(limit=limit)}

    @app.post("/api/approve")
    async def approve(request: Request, _: None = Depends(_auth)) -> dict[str, Any]:
        body = await request.json()
        return get_operator().approve(str(body.get("id", "")))

    @app.post("/api/reject")
    async def reject(request: Request, _: None = Depends(_auth)) -> dict[str, Any]:
        body = await request.json()
        return get_operator().reject(str(body.get("id", "")))

    @app.post("/api/autonomy")
    async def autonomy(request: Request, _: None = Depends(_auth)) -> dict[str, Any]:
        body = await request.json()
        mode = get_operator().set_autonomy(str(body.get("mode", "act_report")))
        return {"autonomy": mode}

    @app.post("/api/chat")
    async def chat(request: Request, _: None = Depends(_auth)) -> StreamingResponse:
        body = await request.json()
        message = str(body.get("message", "")).strip()
        history = body.get("history") or []
        autonomy = body.get("autonomy")
        if not isinstance(history, list):
            history = []

        def _gen():
            if not message:
                yield _sse({"type": "error", "message": "empty message"})
                return
            try:
                for ev in get_operator().chat_stream(
                    message, history, autonomy=autonomy
                ):
                    yield _sse(ev.to_dict())
            except Exception as e:  # noqa: BLE001
                log.exception("chat stream crashed")
                yield _sse({"type": "error", "message": str(e)})

        return StreamingResponse(_gen(), media_type="text/event-stream")

    return app


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def serve(host: str | None = None, port: int | None = None) -> None:
    """Run the command-center with uvicorn (blocking)."""
    import uvicorn

    cfg = get_operator_config()
    app = create_app()
    log.info("Maahi command-center on http://%s:%s", host or cfg.host, port or cfg.port)
    uvicorn.run(app, host=host or cfg.host, port=port or cfg.port, log_level="info")

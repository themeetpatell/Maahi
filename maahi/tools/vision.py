"""Vision tools — on-demand screen understanding via Ollama multimodal.

Strict local: every byte stays on the Mac. We use `screencapture(1)` to
grab the screen (full or region), downscale with Pillow to keep token
counts sane, then POST the JPEG to Ollama's native /api/chat endpoint
with the `images` field.

Two public entry points:
  - vision_screen(question)              — full screen
  - vision_region(question, x, y, w, h)  — a rectangle

Both publish a `vision_capture` event so the HUD can show a thumbnail
of what Maahi just looked at.
"""
from __future__ import annotations

import base64
import io
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from ..config import get_config
from ..event_bus import emit_vision_capture

log = logging.getLogger(__name__)

_SUMMARY_MAX = 600
_THUMB_SIDE = 320
_THUMB_QUALITY = 60
_OLLAMA_TIMEOUT_S = 120.0


def _ensure_scratch() -> Path:
    cfg = get_config()
    p = Path(cfg.vision.scratch_dir).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _capture_full(path: Path) -> None:
    """Silent, no-cursor full-screen PNG via macOS screencapture."""
    subprocess.run(
        ["screencapture", "-x", "-C", str(path)],
        check=True,
        capture_output=True,
        timeout=10,
    )


def _capture_region(path: Path, x: int, y: int, w: int, h: int) -> None:
    rect = f"{x},{y},{w},{h}"
    subprocess.run(
        ["screencapture", "-x", "-C", "-R", rect, str(path)],
        check=True,
        capture_output=True,
        timeout=10,
    )


def _downscale_to_jpeg(png_path: Path, max_side: int, quality: int) -> bytes:
    """Open the captured PNG, downscale, return JPEG bytes."""
    from PIL import Image

    with Image.open(png_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        longest = max(w, h)
        if longest > max_side:
            scale = max_side / longest
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()


def _make_thumbnail(jpeg_bytes: bytes) -> str:
    from PIL import Image

    with Image.open(io.BytesIO(jpeg_bytes)) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = _THUMB_SIDE / max(w, h)
        if scale < 1.0:
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=_THUMB_QUALITY, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")


def _ask_ollama(question: str, jpeg_bytes: bytes) -> str:
    cfg = get_config()
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    payload = {
        "model": cfg.vision.model,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": question or "Describe what you see on this screen.",
                "images": [b64],
            }
        ],
        "options": {"num_predict": 512, "temperature": 0.2},
    }
    with httpx.Client(base_url=cfg.brain.host, timeout=_OLLAMA_TIMEOUT_S) as c:
        r = c.post("/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
    return (data.get("message") or {}).get("content", "").strip()


def _scratch_paths(prefix: str) -> Path:
    scratch = _ensure_scratch()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    seq = int(time.time() * 1000) % 1000
    return scratch / f"{prefix}-{stamp}-{seq}.png"


def _run_vision(
    png_path: Path,
    question: str,
) -> dict[str, Any]:
    """Shared post-capture pipeline. The capture must already exist at png_path."""
    cfg = get_config()
    try:
        jpeg = _downscale_to_jpeg(
            png_path, cfg.vision.max_image_side, cfg.vision.jpeg_quality
        )
    except Exception as e:  # noqa: BLE001
        log.exception("vision downscale failed")
        return {"ok": False, "error": f"image processing failed: {e}"}
    finally:
        try:
            png_path.unlink(missing_ok=True)
        except OSError:
            pass

    thumb = _make_thumbnail(jpeg)
    emit_vision_capture(thumb, question or None)

    try:
        answer = _ask_ollama(question, jpeg)
    except httpx.HTTPError as e:
        log.error("Ollama vision call failed: %s", e)
        return {"ok": False, "error": f"vision model unreachable: {e}"}

    if not answer:
        return {"ok": False, "error": "vision model returned empty response"}

    summary = answer if len(answer) <= _SUMMARY_MAX else answer[:_SUMMARY_MAX] + "…"
    return {"ok": True, "summary": summary, "full": answer, "model": cfg.vision.model}


def vision_screen(question: str = "") -> dict[str, Any]:
    """Capture the full screen and ask the vision model about it."""
    png_path = _scratch_paths("screen")
    try:
        _capture_full(png_path)
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or b"").decode("utf-8", errors="ignore")
        log.error("screencapture failed: %s", msg)
        return {
            "ok": False,
            "error": "screencapture failed — grant Screen Recording permission.",
            "detail": msg,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "screencapture timed out"}
    return _run_vision(png_path, question)


def vision_region(
    question: str,
    x: int,
    y: int,
    w: int,
    h: int,
) -> dict[str, Any]:
    """Capture a rectangle and ask about it."""
    if w <= 0 or h <= 0:
        return {"ok": False, "error": "width and height must be positive"}

    png_path = _scratch_paths("region")
    try:
        _capture_region(png_path, int(x), int(y), int(w), int(h))
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or b"").decode("utf-8", errors="ignore")
        return {"ok": False, "error": "screencapture failed", "detail": msg}
    return _run_vision(png_path, question)

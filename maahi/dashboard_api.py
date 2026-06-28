"""Backend helpers for the full-screen Jarvis dashboard.

These functions back the REST endpoints the dashboard polls / posts to:

  - get_facts / get_preferences   memory viewers
  - get_index_stats               vector-index telemetry
  - tail_logs                     last N lines of maahi.log
  - submit_command                publishes a text_command event so the
                                  brain (running in the wake loop) processes
                                  typed input the same way as voice
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import get_config
from .event_bus import bus
from .memory import _mem_root, facts_path, prefs_path

log = logging.getLogger(__name__)


# ============================================================
#  MEMORY VIEWERS
# ============================================================


def get_facts() -> dict[str, Any]:
    p = facts_path()
    if not p.exists():
        return {"ok": True, "path": str(p), "exists": False, "content": ""}
    return {
        "ok": True,
        "path": str(p),
        "exists": True,
        "content": p.read_text(encoding="utf-8"),
        "size_bytes": p.stat().st_size,
    }


def get_preferences() -> dict[str, Any]:
    p = prefs_path()
    if not p.exists():
        return {"ok": True, "path": str(p), "exists": False, "content": ""}
    return {
        "ok": True,
        "path": str(p),
        "exists": True,
        "content": p.read_text(encoding="utf-8"),
        "size_bytes": p.stat().st_size,
    }


def get_index_stats() -> dict[str, Any]:
    """Vector-index summary: note count, last update, size on disk."""
    root = _mem_root() / "vector_index"
    meta_path = root / "meta.json"
    vectors_path = root / "vectors.npy"
    if not meta_path.exists():
        return {"ok": True, "exists": False, "notes": 0,
                "vectors_bytes": 0, "last_update_ts": None}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "error": "Index meta unreadable."}
    last_ts = max((e.get("mtime", 0) for e in meta), default=None) if meta else None
    return {
        "ok": True,
        "exists": True,
        "notes": len(meta),
        "vectors_bytes": vectors_path.stat().st_size if vectors_path.exists() else 0,
        "last_update_ts": last_ts,
        "model": "nomic-embed-text",
    }


# ============================================================
#  LOG TAIL
# ============================================================


def tail_logs(lines: int = 200) -> dict[str, Any]:
    """Return the last N lines of the runtime log."""
    cfg = get_config()
    p = Path(cfg.logging.path)
    if not p.exists():
        return {"ok": True, "lines": [], "path": str(p), "exists": False}
    lines = max(1, min(int(lines), 5000))
    try:
        size = p.stat().st_size
        chunk = min(size, lines * 240)
        with p.open("rb") as f:
            f.seek(max(0, size - chunk))
            data = f.read().decode("utf-8", errors="replace")
        all_lines = data.splitlines()
        return {
            "ok": True,
            "path": str(p),
            "exists": True,
            "lines": all_lines[-lines:],
            "total_returned": min(lines, len(all_lines)),
        }
    except OSError as e:
        return {"ok": False, "error": str(e)}


# ============================================================
#  TEXT COMMAND PATH
# ============================================================


def submit_command(text: str) -> dict[str, Any]:
    """Publish a text_command event on the bus.

    The brain worker (spawned from main.py) listens for these and feeds
    them through the same Brain instance as voice commands. We don't run
    the brain here — that would duplicate state and bypass the speaker.
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Empty command."}
    bus().publish("text_command", {"text": text})
    return {"ok": True, "submitted": text}

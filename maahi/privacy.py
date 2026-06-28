"""Privacy view helpers.

Backs the HUD's "Today" panel. Reads the most recent conversation log
for the current day and exposes a one-shot clear button.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from .memory import _mem_root

log = logging.getLogger(__name__)


def _today_files() -> list[Path]:
    root = _mem_root() / "conversations"
    if not root.exists():
        return []
    prefix = date.today().strftime("%Y%m%d")
    return sorted(root.glob(f"{prefix}-*.jsonl"))


def todays_turns() -> dict[str, Any]:
    """Parsed turns from every conversation file dated today."""
    turns: list[dict[str, Any]] = []
    files: list[str] = []
    for p in _today_files():
        files.append(p.name)
        for raw in p.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            turns.append({
                "ts": entry.get("ts", ""),
                "role": entry.get("role", ""),
                "content": entry.get("content", ""),
                "source_file": p.name,
            })
    return {
        "ok": True,
        "date": date.today().isoformat(),
        "turn_count": len(turns),
        "files": files,
        "turns": turns,
    }


def clear_today() -> dict[str, Any]:
    """Truncate every conversation file dated today.

    Truncate rather than delete so any open file handles
    (e.g. ConversationLog in a running session) don't break.
    """
    cleared: list[str] = []
    for p in _today_files():
        try:
            p.write_text("", encoding="utf-8")
            cleared.append(p.name)
        except OSError as e:
            log.warning("Could not clear %s: %s", p, e)
    return {"ok": True, "cleared": cleared, "count": len(cleared)}

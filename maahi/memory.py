"""Memory layer.

Maahi remembers across sessions by writing to her own subfolder inside Meet's
Obsidian vault. Three buckets:

  facts.md          — long-term, durable facts she's learned about Meet
  preferences.md    — operating preferences (voice, working hours, etc.)
  conversations/    — JSONL transcripts, one file per session
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import get_config

log = logging.getLogger(__name__)


# ============================================================
#  PATHS
# ============================================================


def _mem_root() -> Path:
    root = get_config().vault.memory_dir
    root.mkdir(parents=True, exist_ok=True)
    (root / "conversations").mkdir(exist_ok=True)
    return root


def facts_path() -> Path:
    return _mem_root() / "facts.md"


def prefs_path() -> Path:
    return _mem_root() / "preferences.md"


# ============================================================
#  CONVERSATION LOG
# ============================================================


class ConversationLog:
    """Append-only JSONL log of one session's turns.

    Immutable in spirit — each call writes a new line, never edits past ones.
    """

    def __init__(self) -> None:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = _mem_root() / "conversations" / f"{ts}.jsonl"
        self._opened = datetime.now().isoformat(timespec="seconds")

    def log_turn(self, role: str, content: str, meta: dict[str, Any] | None = None) -> None:
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "role": role,             # user | maahi | tool
            "content": content,
            "meta": meta or {},
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ============================================================
#  FACTS — durable knowledge about Meet
# ============================================================


def recall_facts() -> str:
    """Return all stored facts as plain text. Empty string if none."""
    p = facts_path()
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def remember_fact(fact: str) -> dict[str, object]:
    """Append a single fact line. Idempotent for exact duplicates."""
    p = facts_path()
    existing = p.read_text(encoding="utf-8") if p.exists() else "# Maahi — Facts\n\n"
    if fact.strip() in existing:
        return {"ok": True, "skipped": "duplicate"}
    today = datetime.now().strftime("%Y-%m-%d")
    line = f"- ({today}) {fact.strip()}\n"
    p.write_text(existing + line, encoding="utf-8")
    return {"ok": True, "stored": fact.strip()}


# ============================================================
#  PREFERENCES
# ============================================================


def recall_preferences() -> str:
    p = prefs_path()
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def set_preference(key: str, value: str) -> dict[str, object]:
    """Upsert a key=value preference line."""
    p = prefs_path()
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else [
        "# Maahi — Preferences", ""
    ]
    prefix = f"- {key}:"
    new_line = f"- {key}: {value}"
    rebuilt = [ln for ln in lines if not ln.startswith(prefix)]
    rebuilt.append(new_line)
    p.write_text("\n".join(rebuilt) + "\n", encoding="utf-8")
    return {"ok": True, "set": {key: value}}

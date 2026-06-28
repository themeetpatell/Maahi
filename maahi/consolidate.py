"""Nightly memory consolidation.

Walks ``<memory_dir>/conversations/*.jsonl`` for a target date, asks the
local LLM to extract durable facts about Meet and stable preferences, and
appends them via the existing ``memory.remember_fact`` /
``memory.set_preference`` helpers so the system prompt picks them up on
next boot.

Idempotency: a ledger at ``<memory_dir>/consolidated.json`` records
which conversation files have already been processed. Re-running on the
same date is a no-op unless ``--force`` is passed.

Strict-local: runs the same Ollama model as the brain. No cloud.

CLI:

    python -m maahi.consolidate              # consolidates yesterday
    python -m maahi.consolidate --date 2026-05-23
    python -m maahi.consolidate --all        # backfill everything
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .config import get_config
from .memory import _mem_root, facts_path, prefs_path, remember_fact, set_preference

log = logging.getLogger(__name__)

LEDGER_NAME = "consolidated.json"

_SYSTEM_PROMPT = """\
You are Maahi's memory consolidator. You read a day's worth of conversation
turns between Meet and Maahi and extract DURABLE knowledge that should be
remembered across sessions.

Return strictly a JSON object with two keys:

  {
    "facts": ["one-line fact about Meet or his work", ...],
    "preferences": {"key": "value", ...}
  }

Rules:
- Facts are durable, not ephemeral. ("Meet's wife's birthday is May 12" YES.
  "Meet asked the time at 3pm" NO.)
- Preferences are stable settings about how Maahi should behave.
  ("voice": "Samantha", "morning_brief_time": "07:00"). Skip if uncertain.
- If nothing useful was learned, return {"facts": [], "preferences": {}}.
- Output ONLY the JSON object — no prose, no code fences.
"""


# ============================================================
#  LEDGER
# ============================================================


def _ledger_path() -> Path:
    return _mem_root() / LEDGER_NAME


def _load_ledger() -> dict[str, Any]:
    p = _ledger_path()
    if not p.exists():
        return {"processed": [], "last_run": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("Consolidation ledger corrupt; resetting")
        return {"processed": [], "last_run": None}


def _save_ledger(ledger: dict[str, Any]) -> None:
    _ledger_path().write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ============================================================
#  TRANSCRIPT LOADING
# ============================================================


@dataclass(frozen=True)
class Turn:
    ts: str
    role: str
    content: str


def _load_turns(jsonl_path: Path) -> list[Turn]:
    turns: list[Turn] = []
    for raw in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        turns.append(Turn(
            ts=str(entry.get("ts", "")),
            role=str(entry.get("role", "")),
            content=str(entry.get("content", "")),
        ))
    return turns


def _format_transcript(turns: list[Turn]) -> str:
    return "\n".join(f"[{t.role}] {t.content}" for t in turns if t.content)


def _files_for_date(target: date) -> list[Path]:
    root = _mem_root() / "conversations"
    if not root.exists():
        return []
    prefix = target.strftime("%Y%m%d")
    return sorted(root.glob(f"{prefix}-*.jsonl"))


def _all_files() -> list[Path]:
    root = _mem_root() / "conversations"
    if not root.exists():
        return []
    return sorted(root.glob("*.jsonl"))


# ============================================================
#  LLM CALL
# ============================================================


def _extract(transcript: str, *, http: httpx.Client | None = None) -> dict[str, Any]:
    """Ask the local LLM for facts + preferences. Always returns a dict."""
    cfg = get_config()
    client = http or httpx.Client(
        base_url=cfg.brain.host, timeout=httpx.Timeout(120.0, connect=10.0),
    )
    payload = {
        "model": cfg.brain.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": transcript[:12000]},
        ],
        "temperature": 0.2,
        "stream": False,
        "options": {"num_predict": 512},
    }
    try:
        r = client.post("/v1/chat/completions", json=payload)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.error("Consolidator LLM call failed: %s", e)
        return {"facts": [], "preferences": {}}

    data = r.json()
    text = (
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    ).strip()
    if not text:
        return {"facts": [], "preferences": {}}

    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Consolidator returned non-JSON: %r", text[:200])
        return {"facts": [], "preferences": {}}

    facts = parsed.get("facts") or []
    prefs = parsed.get("preferences") or {}
    if not isinstance(facts, list):
        facts = []
    if not isinstance(prefs, dict):
        prefs = {}
    return {
        "facts": [str(f).strip() for f in facts if str(f).strip()],
        "preferences": {str(k): str(v) for k, v in prefs.items() if k and v},
    }


# ============================================================
#  ORCHESTRATION
# ============================================================


def _backup_memory_files() -> Path | None:
    """Snapshot facts.md + preferences.md before any consolidation write.

    Returns the backup directory path on success, None if both files were
    missing (nothing to back up). A bad consolidator run can corrupt these
    via duplicate / hallucinated facts — restoring from the backup is a
    one-line cp.
    """
    backup_root = _mem_root() / ".backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_root / stamp
    copied = 0
    for src in (facts_path(), prefs_path()):
        if src.exists():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / src.name)
            copied += 1
    if copied == 0:
        return None
    # Keep only the most recent 30 snapshots to avoid unbounded growth.
    snaps = sorted(p for p in backup_root.iterdir() if p.is_dir())
    for old in snaps[:-30]:
        shutil.rmtree(old, ignore_errors=True)
    log.info("Memory backed up to %s (%d files)", dest, copied)
    return dest


def consolidate_files(
    paths: list[Path],
    *,
    http: httpx.Client | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Consolidate a batch of conversation files. Skips ones in the ledger."""
    _backup_memory_files()
    ledger = _load_ledger()
    processed: set[str] = set(ledger.get("processed", []))

    total_facts = 0
    total_prefs = 0
    handled: list[str] = []

    for p in paths:
        if not force and p.name in processed:
            log.info("Skipping already-consolidated %s", p.name)
            continue
        turns = _load_turns(p)
        if not turns:
            processed.add(p.name)
            continue
        transcript = _format_transcript(turns)
        extracted = _extract(transcript, http=http)

        for fact in extracted["facts"]:
            res = remember_fact(fact)
            if res.get("stored"):
                total_facts += 1
        for k, v in extracted["preferences"].items():
            set_preference(k, v)
            total_prefs += 1

        processed.add(p.name)
        handled.append(p.name)
        log.info("Consolidated %s — %d facts, %d prefs",
                 p.name, len(extracted["facts"]), len(extracted["preferences"]))

    ledger["processed"] = sorted(processed)
    ledger["last_run"] = datetime.now().isoformat(timespec="seconds")
    _save_ledger(ledger)

    return {
        "ok": True,
        "files_processed": handled,
        "facts_added": total_facts,
        "preferences_set": total_prefs,
    }


def consolidate_date(target: date, **kw: Any) -> dict[str, Any]:
    return consolidate_files(_files_for_date(target), **kw)


def consolidate_all(**kw: Any) -> dict[str, Any]:
    return consolidate_files(_all_files(), **kw)


# ============================================================
#  CLI
# ============================================================


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Consolidate Maahi conversation logs into long-term memory.",
    )
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--date", help="ISO date (YYYY-MM-DD). Defaults to yesterday.")
    grp.add_argument("--all", action="store_true",
                     help="Backfill every conversation file regardless of date.")
    parser.add_argument("--force", action="store_true",
                        help="Re-consolidate files already in the ledger.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )

    if args.all:
        out = consolidate_all(force=args.force)
    else:
        target = (
            date.fromisoformat(args.date) if args.date
            else date.today() - timedelta(days=1)
        )
        out = consolidate_date(target, force=args.force)

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

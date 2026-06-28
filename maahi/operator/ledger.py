"""The ledger — Maahi's memory of what she did, and what she wants to do.

Two stores, both file-backed under the operator state dir:

  1. An append-only **audit log** (``ledger.jsonl``). Every action — read,
     write, send, the lot — lands here with a timestamp, risk, decision, and
     outcome. This is the "report" half of act-then-report, and your paper
     trail when you ask "what did Maahi do while I slept?".

  2. A mutable **pending queue** (``pending.json``). When the policy says an
     action needs your yes, it's parked here with an id. The cockpit shows it;
     ``approve(id)`` / ``reject(id)`` resolves it.

Thread-safe with a process-local lock. Good enough for a single operator
process; if you ever shard this, move to SQLite.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import get_operator_config

_LOCK = threading.RLock()


def _now() -> float:
    return time.time()


def _gen_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class LedgerEntry:
    """One line in the audit log."""

    id: str
    ts: float
    actor: str                  # "maahi" | "meet" | connector key
    action: str                 # e.g. "zoho_crm.create_task"
    risk: str
    decision: str               # allow | confirm | approved | rejected | autopilot
    status: str                 # done | failed | pending | rejected
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class PendingAction:
    """An action waiting on Meet's confirmation."""

    id: str
    ts: float
    action: str
    risk: str
    summary: str
    params: dict[str, Any] = field(default_factory=dict)
    origin: str = "agent"       # who proposed it (chat, brief, autopilot…)
    reason: str = ""            # why it needs confirmation

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Ledger:
    def __init__(self, state_dir: Path | None = None) -> None:
        cfg = get_operator_config()
        self.dir = Path(state_dir or cfg.state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.dir / "ledger.jsonl"
        self.pending_path = self.dir / "pending.json"

    # ---- audit log ----

    def record(
        self,
        action: str,
        *,
        risk: str = "read",
        decision: str = "allow",
        status: str = "done",
        summary: str = "",
        actor: str = "maahi",
        detail: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        entry = LedgerEntry(
            id=_gen_id(),
            ts=_now(),
            actor=actor,
            action=action,
            risk=risk,
            decision=decision,
            status=status,
            summary=summary or action,
            detail=detail or {},
        )
        with _LOCK:
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(entry.to_json() + "\n")
        return entry

    def recent(self, limit: int = 50, *, since_ts: float | None = None) -> list[dict]:
        """Most-recent-last list of audit entries."""
        if not self.audit_path.exists():
            return []
        with _LOCK:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        out: list[dict] = []
        for line in lines[-(limit * 3 if since_ts else limit) :]:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_ts is not None and obj.get("ts", 0) < since_ts:
                continue
            out.append(obj)
        return out[-limit:]

    # ---- pending queue ----

    def _read_pending(self) -> list[dict]:
        if not self.pending_path.exists():
            return []
        try:
            return json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def _write_pending(self, items: list[dict]) -> None:
        tmp = self.pending_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.pending_path)

    def propose(
        self,
        action: str,
        *,
        risk: str,
        summary: str,
        params: dict[str, Any] | None = None,
        origin: str = "agent",
        reason: str = "",
    ) -> PendingAction:
        """Park an action that needs confirmation. Returns the pending record."""
        pa = PendingAction(
            id=_gen_id(),
            ts=_now(),
            action=action,
            risk=risk,
            summary=summary,
            params=params or {},
            origin=origin,
            reason=reason,
        )
        with _LOCK:
            items = self._read_pending()
            items.append(pa.to_dict())
            self._write_pending(items)
        self.record(
            action,
            risk=risk,
            decision="confirm",
            status="pending",
            summary=summary,
            detail={"pending_id": pa.id, "reason": reason},
        )
        return pa

    def pending(self) -> list[dict]:
        with _LOCK:
            return self._read_pending()

    def get_pending(self, action_id: str) -> dict | None:
        for item in self.pending():
            if item.get("id") == action_id:
                return item
        return None

    def resolve(self, action_id: str, *, approved: bool) -> dict | None:
        """Remove a pending action and audit the human's decision.

        Returns the resolved pending record (so the caller can now execute it),
        or None if the id is unknown.
        """
        with _LOCK:
            items = self._read_pending()
            found: dict | None = None
            rest: list[dict] = []
            for item in items:
                if item.get("id") == action_id and found is None:
                    found = item
                else:
                    rest.append(item)
            if found is None:
                return None
            self._write_pending(rest)
        self.record(
            found.get("action", "unknown"),
            risk=found.get("risk", "read"),
            decision="approved" if approved else "rejected",
            status="approved" if approved else "rejected",
            summary=found.get("summary", ""),
            actor="meet",
            detail={"pending_id": action_id},
        )
        return found


_LEDGER: Ledger | None = None


def get_ledger() -> Ledger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = Ledger()
    return _LEDGER

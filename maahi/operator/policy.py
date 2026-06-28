"""Autonomy policy — the "act then report" governor.

This is the safety rail that lets Maahi move fast without doing something
you can't take back. Every action the agent wants to take carries a ``risk``
level (from the connector's ``Capability``). The policy maps (risk, autonomy
mode) → one of:

    ALLOW    — do it now, silently or with a one-line report
    CONFIRM  — pause and ask Meet first (outward-facing / costly / destructive)
    DENY     — never (reserved; currently unused, here for completeness)

Autonomy modes:
    suggest     — propose everything; only pure reads run without asking
    act_report  — DEFAULT. Reversible work runs; send/spend/delete confirm
    autopilot   — run everything, including outbound + spend

The policy is pure and dependency-free so it's trivially testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .connectors.base import RISK_ORDER


class Autonomy(str, Enum):
    SUGGEST = "suggest"
    ACT_REPORT = "act_report"
    AUTOPILOT = "autopilot"

    @classmethod
    def parse(cls, value: str | None) -> "Autonomy":
        v = (value or "").strip().lower()
        for m in cls:
            if m.value == v:
                return m
        # Friendly aliases.
        if v in ("act", "act-then-report", "default", "balanced"):
            return cls.ACT_REPORT
        if v in ("auto", "full", "yolo"):
            return cls.AUTOPILOT
        if v in ("ask", "manual", "confirm"):
            return cls.SUGGEST
        return cls.ACT_REPORT


class Decision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


# The highest risk level each mode will run WITHOUT confirmation.
# Anything strictly above this line requires a human yes.
_CEILING: dict[Autonomy, str] = {
    Autonomy.SUGGEST: "read",      # only reads run free
    Autonomy.ACT_REPORT: "write",  # reads + reversible writes run free
    Autonomy.AUTOPILOT: "delete",  # everything runs free
}


@dataclass(frozen=True)
class PolicyVerdict:
    decision: Decision
    risk: str
    reason: str

    @property
    def needs_confirmation(self) -> bool:
        return self.decision is Decision.CONFIRM

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


def evaluate(risk: str, autonomy: Autonomy | str) -> PolicyVerdict:
    """Decide whether an action of ``risk`` may run under ``autonomy``."""
    mode = autonomy if isinstance(autonomy, Autonomy) else Autonomy.parse(autonomy)
    if risk not in RISK_ORDER:
        # Unknown risk → treat as the most dangerous, fail safe.
        risk = "delete"
    ceiling = _CEILING[mode]
    if RISK_ORDER[risk] <= RISK_ORDER[ceiling]:
        return PolicyVerdict(
            Decision.ALLOW,
            risk,
            f"{risk} ≤ {ceiling} ceiling for {mode.value}",
        )
    return PolicyVerdict(
        Decision.CONFIRM,
        risk,
        f"{risk} exceeds {ceiling} ceiling for {mode.value} — needs your yes",
    )


# Human-readable, for the cockpit and the brief.
RISK_BADGE: dict[str, str] = {
    "read": "read",
    "write": "draft/internal",
    "publish": "publish",
    "send": "outbound",
    "spend": "spend",
    "delete": "destructive",
}


def describe(risk: str) -> str:
    return RISK_BADGE.get(risk, risk)

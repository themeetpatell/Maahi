"""Voice → Operator bridge.

These two tools let the voice loop reach Maahi's *business* brain. When Meet
says "what's slipping in my pipeline?" or "brief me on the business", the voice
brain calls one of these, which runs the full Operator agent (Claude + every
business connector, under the autonomy policy) and hands back a spoken-length
answer.

Everything is imported lazily and degrades gracefully: if the operator brain
is offline (no ANTHROPIC_API_KEY) or its deps aren't installed, these return a
clear one-liner instead of raising — so the voice loop never breaks.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _shorten(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(". ", 1)[0]
    return (cut or text[:limit]).rstrip() + "…"


def business_brief() -> dict:
    """Executive brief across every venture (CRM, ads, ship, infra, comms)."""
    try:
        from ..operator.core import get_operator

        brief = get_operator().brief(synthesize=True)
    except Exception as e:  # noqa: BLE001
        log.warning("business_brief failed: %s", e)
        return {"ok": False, "error": f"Operator brief unavailable: {e}"}
    spoken = brief.narrative or brief.headline
    return {"ok": True, "value": _shorten(spoken),
            "headline": brief.headline, "pending": brief.pending_count}


def business_ask(request: str = "") -> dict:
    """Ask Maahi's business brain to do or answer something across the stack.

    Runs the full Operator agent: it will read CRM/ads/repos/etc. and act on
    reversible things, parking risky moves for approval (act-then-report).
    """
    request = (request or "").strip()
    if not request:
        return {"ok": False, "error": "ask what? give me a business request"}
    try:
        from ..operator.core import get_operator

        op = get_operator()
        if not op.agent.available():
            return {"ok": False,
                    "error": "Business brain is offline — set ANTHROPIC_API_KEY."}
        result = op.chat(request)
    except Exception as e:  # noqa: BLE001
        log.warning("business_ask failed: %s", e)
        return {"ok": False, "error": f"Operator unavailable: {e}"}
    text = result.get("text", "")
    out = {"ok": True, "value": _shorten(text)}
    if result.get("confirmations"):
        out["pending"] = len(result["confirmations"])
    return out

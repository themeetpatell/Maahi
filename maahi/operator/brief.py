"""The daily executive brief — your empire on one screen.

Pulls a live ``pulse()`` from every configured connector in parallel (CRM
deals, ad spend, deploy health, recent docs, unread mail…), assembles them
into a structured ``Brief``, and — when the Claude brain is available —
synthesizes a tight, founder-grade narrative that leads with what matters.

No connectors configured yet? You still get a clean, honest brief that tells
you exactly which systems to plug in. It never crashes on a dead integration.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import OperatorConfig, get_operator_config
from .connectors.registry import ConnectorRegistry, get_registry

log = logging.getLogger(__name__)


@dataclass
class SystemPulse:
    key: str
    label: str
    ok: bool
    configured: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Brief:
    generated_at: float
    headline: str
    pulses: list[SystemPulse]
    narrative: str = ""
    pending_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "headline": self.headline,
            "narrative": self.narrative,
            "pending_count": self.pending_count,
            "pulses": [p.to_dict() for p in self.pulses],
        }

    @property
    def configured_pulses(self) -> list[SystemPulse]:
        return [p for p in self.pulses if p.configured]

    def markdown(self) -> str:
        lines = [f"# Maahi Brief — {time.strftime('%A %d %b, %H:%M', time.localtime(self.generated_at))}", ""]
        if self.narrative:
            lines += [self.narrative, ""]
        lines.append("## Systems")
        for p in self.pulses:
            mark = "✅" if (p.ok and p.configured) else ("—" if not p.configured else "⚠️")
            lines.append(f"- {mark} **{p.label}**: {p.summary}")
        if self.pending_count:
            lines.append("")
            lines.append(f"_{self.pending_count} action(s) waiting for your approval._")
        return "\n".join(lines)


def _gather_pulse(registry: ConnectorRegistry) -> list[SystemPulse]:
    """Call ``pulse()`` on every connector concurrently. Bounded + safe."""
    connectors = registry.all()
    pulses: dict[str, SystemPulse] = {}

    def _one(key: str) -> SystemPulse:
        conn = connectors[key]
        if not conn.configured():
            return SystemPulse(key, conn.label, ok=False, configured=False,
                               summary="not connected")
        try:
            res = conn.pulse()
        except Exception as e:  # noqa: BLE001 — never let one system sink the brief
            return SystemPulse(key, conn.label, ok=False, configured=True,
                               summary=f"error: {e}")
        return SystemPulse(
            key, conn.label, ok=res.ok, configured=True,
            summary=res.summary or ("ok" if res.ok else (res.error or "error")),
            data=res.data if isinstance(res.data, dict) else {"value": res.data},
        )

    if not connectors:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(connectors))) as pool:
        futs = {pool.submit(_one, k): k for k in connectors}
        for fut in as_completed(futs, timeout=60):
            key = futs[fut]
            try:
                pulses[key] = fut.result()
            except Exception as e:  # noqa: BLE001
                conn = connectors[key]
                pulses[key] = SystemPulse(key, conn.label, ok=False,
                                          configured=True, summary=f"timeout/error: {e}")
    # Preserve roster order.
    return [pulses[k] for k in connectors if k in pulses]


def _headline(pulses: list[SystemPulse], pending: int) -> str:
    live = [p for p in pulses if p.configured]
    if not live:
        return "No systems connected yet — plug in your stack to light Maahi up."
    ok = sum(1 for p in live if p.ok)
    bits = [f"{ok}/{len(live)} systems healthy"]
    if pending:
        bits.append(f"{pending} awaiting your yes")
    return " · ".join(bits)


def _fallback_narrative(pulses: list[SystemPulse]) -> str:
    live = [p for p in pulses if p.configured]
    if not live:
        return ("Nothing connected yet. Set the API keys for your stack "
                "(see OPERATOR.md) and I'll start running the numbers.")
    good = [p for p in live if p.ok]
    bad = [p for p in live if not p.ok]
    parts = []
    if good:
        parts.append("Working: " + "; ".join(f"{p.label} — {p.summary}" for p in good[:6]))
    if bad:
        parts.append("Needs a look: " + "; ".join(f"{p.label} ({p.summary})" for p in bad[:4]))
    return ". ".join(parts) + "."


def _synthesize_narrative(brief_data: dict, cfg: OperatorConfig) -> str:
    """Ask Claude for a tight executive narrative. Falls back silently."""
    try:
        import anthropic
    except Exception:  # noqa: BLE001
        return ""
    if not cfg.anthropic_api_key:
        return ""
    import json as _json

    facts = _json.dumps(brief_data, ensure_ascii=False)[:6000]
    try:
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key, max_retries=1)
        resp = client.messages.create(
            model=cfg.fast_model or cfg.model,
            max_tokens=400,
            temperature=0.3,
            system=(
                f"You are Maahi, {cfg.owner_name}'s chief of staff. Write his "
                "morning brief: 3-5 sentences, lead with the single most "
                "important number or decision, name the venture, flag anything "
                "on fire. Direct, dry, no filler, no emoji, no preamble."
            ),
            messages=[{"role": "user", "content":
                       f"System pulse data (JSON):\n{facts}\n\nWrite the brief."}],
        )
        out = []
        for b in resp.content or []:
            if getattr(b, "text", ""):
                out.append(b.text)
        return "".join(out).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("Brief synthesis failed: %s", e)
        return ""


def build_brief(
    registry: ConnectorRegistry | None = None,
    cfg: OperatorConfig | None = None,
    *,
    synthesize: bool = True,
) -> Brief:
    """Assemble the executive brief. ``synthesize`` adds the Claude narrative."""
    registry = registry or get_registry()
    cfg = cfg or get_operator_config()

    pulses = _gather_pulse(registry)
    try:
        from .ledger import get_ledger

        pending = len(get_ledger().pending())
    except Exception:  # noqa: BLE001
        pending = 0

    brief = Brief(
        generated_at=time.time(),
        headline=_headline(pulses, pending),
        pulses=pulses,
        pending_count=pending,
    )
    narrative = ""
    if synthesize:
        narrative = _synthesize_narrative(brief.to_dict(), cfg)
    brief.narrative = narrative or _fallback_narrative(pulses)
    return brief

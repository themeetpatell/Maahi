"""Connector registry — one place that knows every system Maahi can touch.

Responsibilities:
  - Import each connector defensively (a broken/optional one is skipped with a
    warning, never blocks boot — same resilience as the skill-pack loader).
  - Flatten connector capabilities into a flat, namespaced tool catalog the
    Claude agent can call: ``<connector_key>.<capability>``.
  - Dispatch a tool call through the autonomy policy → either execute now
    (and record it) or park it for confirmation (and return a clear signal).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..ledger import get_ledger
from ..policy import Autonomy, Decision, evaluate
from .base import Capability, Connector, ConnectorResult

log = logging.getLogger(__name__)

# Canonical connector roster. Module path (relative to this package) →
# class name. Edit this list to add/remove a system. Import failures are
# tolerated so one bad connector never sinks the operator.
_ROSTER: tuple[tuple[str, str], ...] = (
    ("zoho_crm", "ZohoCRMConnector"),
    ("notion", "NotionConnector"),
    ("gdrive", "GoogleDriveConnector"),
    ("meta_ads", "MetaAdsConnector"),
    ("webflow", "WebflowConnector"),
    ("github", "GitHubConnector"),
    ("vercel", "VercelConnector"),
    ("supabase", "SupabaseConnector"),
    ("cloudflare", "CloudflareConnector"),
    ("gmail", "GmailConnector"),
    ("mcp", "MCPConnector"),
)


@dataclass(frozen=True)
class ToolSpec:
    """A namespaced, agent-facing view of a connector capability."""

    name: str                 # "<connector>.<capability>"
    connector: str
    capability: str
    description: str
    params: dict[str, str]
    risk: str


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of routing a tool call through policy + execution."""

    status: str               # "done" | "failed" | "needs_confirmation"
    result: ConnectorResult | None = None
    pending_id: str | None = None
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "result": self.result.to_dict() if self.result else None,
            "pending_id": self.pending_id,
            "summary": self.summary,
        }


def _load_roster() -> dict[str, Connector]:
    out: dict[str, Connector] = {}
    for module_name, class_name in _ROSTER:
        try:
            module = __import__(
                f"{__package__}.{module_name}", fromlist=[class_name]
            )
            cls = getattr(module, class_name)
            inst = cls()
            out[inst.key] = inst
        except Exception as e:  # noqa: BLE001
            log.warning("Connector %s unavailable: %s", module_name, e)
    return out


class ConnectorRegistry:
    """Holds connector instances and routes calls through the policy."""

    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = _load_roster()
        self._ledger = get_ledger()

    # ---- introspection ----

    def all(self) -> dict[str, Connector]:
        return dict(self._connectors)

    def get(self, key: str) -> Connector | None:
        return self._connectors.get(key)

    def configured(self) -> dict[str, Connector]:
        return {k: c for k, c in self._connectors.items() if c.configured()}

    def tools(self, *, only_configured: bool = False) -> list[ToolSpec]:
        """Flat, namespaced catalog of every capability."""
        specs: list[ToolSpec] = []
        for key, conn in self._connectors.items():
            if only_configured and not conn.configured():
                continue
            for cap in conn.capabilities():
                specs.append(
                    ToolSpec(
                        name=f"{key}.{cap.name}",
                        connector=key,
                        capability=cap.name,
                        description=cap.description,
                        params=dict(cap.params),
                        risk=cap.risk,
                    )
                )
        return specs

    def find_tool(self, name: str) -> ToolSpec | None:
        for spec in self.tools():
            if spec.name == name:
                return spec
        return None

    # ---- dispatch ----

    def dispatch(
        self,
        tool_name: str,
        params: dict[str, Any] | None = None,
        *,
        autonomy: Autonomy | str = Autonomy.ACT_REPORT,
        origin: str = "agent",
    ) -> DispatchResult:
        """Route a tool call: evaluate policy, then execute or park it.

        Returns a DispatchResult. ``needs_confirmation`` means the action was
        parked in the pending queue (with ``pending_id``) and NOT executed —
        the caller should surface it to Meet for approval.
        """
        params = params or {}
        spec = self.find_tool(tool_name)
        if spec is None:
            return DispatchResult("failed", ConnectorResult.fail(
                f"Unknown tool: {tool_name}"), summary=f"unknown tool {tool_name}")

        verdict = evaluate(spec.risk, autonomy)
        if verdict.decision is Decision.CONFIRM:
            pa = self._ledger.propose(
                tool_name,
                risk=spec.risk,
                summary=self._summarize(spec, params),
                params=params,
                origin=origin,
                reason=verdict.reason,
            )
            return DispatchResult(
                "needs_confirmation",
                pending_id=pa.id,
                summary=f"Needs your yes: {pa.summary}",
            )

        return self._execute(spec, params, decision=verdict.decision.value)

    def execute_approved(self, pending_id: str) -> DispatchResult:
        """Run an action that was previously parked and just got approved."""
        record = self._ledger.resolve(pending_id, approved=True)
        if record is None:
            return DispatchResult("failed", ConnectorResult.fail(
                f"No pending action {pending_id}"))
        spec = self.find_tool(record["action"])
        if spec is None:
            return DispatchResult("failed", ConnectorResult.fail(
                f"Unknown tool: {record['action']}"))
        return self._execute(spec, record.get("params", {}), decision="approved")

    def reject(self, pending_id: str) -> bool:
        return self._ledger.resolve(pending_id, approved=False) is not None

    def _execute(
        self, spec: ToolSpec, params: dict[str, Any], *, decision: str
    ) -> DispatchResult:
        conn = self._connectors.get(spec.connector)
        if conn is None:
            return DispatchResult("failed", ConnectorResult.fail(
                f"Connector {spec.connector} gone"))
        result = conn.call(spec.capability, **params)
        self._ledger.record(
            spec.name,
            risk=spec.risk,
            decision=decision,
            status="done" if result.ok else "failed",
            summary=result.summary or self._summarize(spec, params),
            detail={"params": _redact(params), "ok": result.ok,
                    "error": result.error},
        )
        return DispatchResult(
            "done" if result.ok else "failed",
            result=result,
            summary=result.summary,
        )

    @staticmethod
    def _summarize(spec: ToolSpec, params: dict[str, Any]) -> str:
        bits = ", ".join(f"{k}={_short(v)}" for k, v in params.items())
        return f"{spec.name}({bits})"


def _short(v: Any, n: int = 40) -> str:
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


# Param keys that should never hit the audit detail verbatim.
_SECRET_KEYS = ("token", "secret", "password", "api_key", "apikey", "authorization")


def _redact(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        if any(s in k.lower() for s in _SECRET_KEYS):
            out[k] = "***"
        else:
            out[k] = _short(v, 120)
    return out


_REGISTRY: ConnectorRegistry | None = None


def get_registry() -> ConnectorRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ConnectorRegistry()
    return _REGISTRY

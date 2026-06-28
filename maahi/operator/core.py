"""The Operator — the single front door to Maahi's business brain.

Everything (the command-center server, the CLI, the voice loop) talks to this
facade rather than reaching into agent/registry/ledger directly. It owns the
agent, exposes chat (buffered + streaming), the brief, system status, and the
approval queue for parked actions.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from .agent import AgentEvent, OperatorAgent
from .brief import Brief, build_brief
from .config import OperatorConfig, get_operator_config
from .connectors.registry import ConnectorRegistry, get_registry
from .ledger import Ledger, get_ledger
from .policy import Autonomy

log = logging.getLogger(__name__)


class Operator:
    def __init__(
        self,
        *,
        cfg: OperatorConfig | None = None,
        registry: ConnectorRegistry | None = None,
    ) -> None:
        self.cfg = cfg or get_operator_config()
        self.registry: ConnectorRegistry = registry or get_registry()
        self.ledger: Ledger = get_ledger()
        self.agent = OperatorAgent(
            autonomy=self.cfg.autonomy, registry=self.registry, cfg=self.cfg
        )

    # ---- chat ----

    def chat(self, message: str, history: list[dict] | None = None,
             *, autonomy: Autonomy | str | None = None) -> dict[str, Any]:
        return self.agent.chat(message, history, autonomy=autonomy)

    def chat_stream(self, message: str, history: list[dict] | None = None,
                    *, autonomy: Autonomy | str | None = None) -> Iterator[AgentEvent]:
        return self.agent.chat_stream(message, history, autonomy=autonomy)

    # ---- brief ----

    def brief(self, *, synthesize: bool = True) -> Brief:
        return build_brief(self.registry, self.cfg, synthesize=synthesize)

    # ---- status ----

    def status(self) -> dict[str, Any]:
        connectors = []
        for key, conn in self.registry.all().items():
            connectors.append({
                "key": key,
                "label": conn.label,
                "configured": conn.configured(),
                "missing_env": list(conn.missing_env()),
                "blurb": getattr(conn, "blurb", ""),
                "capability_count": len(conn.capabilities()),
            })
        return {
            "owner": self.cfg.owner_name,
            "brain_online": self.agent.available(),
            "model": self.cfg.model,
            "autonomy": self.agent.autonomy.value,
            "ventures": list(self.cfg.ventures),
            "connectors": connectors,
            "configured_count": sum(1 for c in connectors if c["configured"]),
            "total_count": len(connectors),
            "pending_count": len(self.ledger.pending()),
            "tool_count": len(self.registry.tools()),
        }

    # ---- approvals ----

    def pending(self) -> list[dict]:
        return self.ledger.pending()

    def approve(self, pending_id: str) -> dict[str, Any]:
        disp = self.registry.execute_approved(pending_id)
        return disp.to_dict()

    def reject(self, pending_id: str) -> dict[str, Any]:
        ok = self.registry.reject(pending_id)
        return {"status": "rejected" if ok else "failed",
                "summary": "Rejected" if ok else f"No pending action {pending_id}"}

    def ledger_recent(self, limit: int = 50) -> list[dict]:
        return self.ledger.recent(limit=limit)

    # ---- autonomy ----

    def set_autonomy(self, mode: Autonomy | str) -> str:
        self.agent.autonomy = Autonomy.parse(mode)
        self.ledger.record("autonomy.set", actor="meet",
                           summary=f"autonomy → {self.agent.autonomy.value}")
        return self.agent.autonomy.value


_OPERATOR: Operator | None = None


def get_operator() -> Operator:
    global _OPERATOR
    if _OPERATOR is None:
        _OPERATOR = Operator()
    return _OPERATOR

"""Connector contract — the spine of Maahi's reach into your business.

Every external system (Zoho CRM, Meta Ads, Notion, GitHub, Vercel…) is a
``Connector``. A connector declares a set of ``Capability`` operations the
agent may call, each tagged with a ``risk`` level that the autonomy policy
uses to decide "do it" vs "ask first". A connector is *configured* only when
its required environment variables are present — otherwise it degrades to a
clear "not configured" result instead of crashing the operator.

Two special methods power the daily brief:
  - ``health()`` — a cheap reachability/auth check.
  - ``pulse()``  — the headline numbers for this system today
                   (open deals, ad spend, failing deploys…).

Design rules:
  - Pure-stdlib + httpx. No system/voice deps. Imports clean on any box.
  - Never raise out of ``call`` / ``pulse`` / ``health`` — return a result.
  - Idempotent construction. Building a connector must not hit the network.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, ClassVar

log = logging.getLogger(__name__)

# Risk taxonomy, lowest → highest. The autonomy policy keys off these.
#   read    - pure read, no side effects (list deals, get metrics)
#   write   - reversible internal mutation (create a draft, log a note,
#             add an internal CRM task) — safe to do then report
#   publish - makes something live/visible (publish a page, push a commit)
#   send    - outbound to a human (email, DM, message)
#   spend   - moves money / budget (raise ad budget, launch a campaign)
#   delete  - destructive / hard to reverse
RISK_LEVELS: tuple[str, ...] = ("read", "write", "publish", "send", "spend", "delete")
RISK_ORDER: dict[str, int] = {r: i for i, r in enumerate(RISK_LEVELS)}


@dataclass(frozen=True)
class ConnectorResult:
    """Normalized outcome of any connector operation."""

    ok: bool
    summary: str = ""                 # one human line for the brief / cockpit
    data: Any = None                  # structured payload for the agent
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "data": self.data,
            "error": self.error,
            "meta": self.meta,
        }

    @classmethod
    def fail(cls, error: str, **meta: Any) -> "ConnectorResult":
        return cls(ok=False, error=error, summary=error, meta=meta)

    @classmethod
    def success(cls, summary: str, data: Any = None, **meta: Any) -> "ConnectorResult":
        return cls(ok=True, summary=summary, data=data, meta=meta)


@dataclass(frozen=True)
class Capability:
    """One operation a connector exposes to the agent.

    ``name`` is the bare operation (e.g. ``list_deals``); the agent sees it
    namespaced as ``<connector_key>.<name>`` (e.g. ``zoho_crm.list_deals``).
    """

    name: str
    description: str
    params: dict[str, str] = field(default_factory=dict)  # name -> "type: desc"
    risk: str = "read"

    def __post_init__(self) -> None:
        if self.risk not in RISK_ORDER:
            raise ValueError(f"Capability {self.name}: bad risk {self.risk!r}")


class Connector:
    """Base class. Subclass and set ``key``, ``label``, ``required_env``."""

    key: ClassVar[str] = "base"
    label: ClassVar[str] = "Base"
    required_env: ClassVar[tuple[str, ...]] = ()
    # A short blurb shown in the cockpit / setup docs.
    blurb: ClassVar[str] = ""

    # ---- configuration ----

    def configured(self) -> bool:
        """True when every required env var is present and non-empty."""
        return all(os.environ.get(k, "").strip() for k in self.required_env)

    def missing_env(self) -> tuple[str, ...]:
        return tuple(k for k in self.required_env if not os.environ.get(k, "").strip())

    def env(self, key: str, default: str = "") -> str:
        return os.environ.get(key, default).strip()

    # ---- capabilities ----

    def capabilities(self) -> tuple[Capability, ...]:
        """Operations this connector exposes. Override in subclasses."""
        return ()

    def capability(self, name: str) -> Capability | None:
        for c in self.capabilities():
            if c.name == name:
                return c
        return None

    # ---- dispatch ----

    def call(self, capability: str, **params: Any) -> ConnectorResult:
        """Invoke a capability by name. Never raises.

        Default dispatch looks for a method named ``op_<capability>`` on the
        subclass. Subclasses may override ``call`` entirely for custom routing.
        """
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label} is not configured (missing: "
                f"{', '.join(self.missing_env()) or 'credentials'})",
                not_configured=True,
            )
        if self.capability(capability) is None:
            return ConnectorResult.fail(
                f"{self.label} has no capability {capability!r}"
            )
        method = getattr(self, f"op_{capability}", None)
        if method is None:
            return ConnectorResult.fail(
                f"{self.label}.{capability} is declared but not implemented"
            )
        try:
            return method(**params)
        except TypeError as e:
            return ConnectorResult.fail(f"bad arguments for {capability}: {e}")
        except Exception as e:  # noqa: BLE001 — connectors must never crash the op loop
            log.exception("%s.%s failed", self.key, capability)
            return ConnectorResult.fail(f"{self.key}.{capability} failed: {e}")

    # ---- brief hooks ----

    def health(self) -> ConnectorResult:
        """Cheap reachability check. Default: just report configured state."""
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        return ConnectorResult.success(f"{self.label}: configured")

    def pulse(self) -> ConnectorResult:
        """Headline numbers for the daily brief. Override where it matters."""
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        return ConnectorResult.success(f"{self.label}: connected", data={})

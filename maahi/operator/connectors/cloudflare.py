"""Cloudflare connector — zones, DNS, cache, and analytics.

Wraps the Cloudflare API v4 (https://api.cloudflare.com/client/v4). Lists
zones and DNS records for the brief, reports zone status/plan, purges cache,
and creates DNS records. Cache purge and DNS creation are operational
mutations, so both are tagged ``write`` for the autonomy policy.

Auth is an API token in the ``Authorization`` bearer header. Every response
is wrapped in a ``{"success", "result", "errors"}`` envelope; we check
``success`` and surface ``errors[].message`` when it is false.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import Capability, Connector, ConnectorResult

_API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareConnector(Connector):
    """Connector for a Cloudflare account's zones and DNS."""

    key = "cloudflare"
    label = "Cloudflare"
    required_env = ("CLOUDFLARE_API_TOKEN",)
    blurb = (
        "Set CLOUDFLARE_API_TOKEN to an API token created at "
        "https://dash.cloudflare.com/profile/api-tokens (Zone.Read, plus "
        "DNS edit as needed). Optionally set CLOUDFLARE_ACCOUNT_ID."
    )

    # ---- helpers ----

    def _client(self) -> httpx.Client:
        """Build a Cloudflare API v4 client with bearer auth."""
        return httpx.Client(
            base_url=_API_BASE,
            headers={
                "Authorization": f"Bearer {self.env('CLOUDFLARE_API_TOKEN')}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(20.0, connect=8.0),
        )

    @staticmethod
    def _bool(value: Any, default: bool = True) -> bool:
        """Coerce a param into a bool, tolerating string forms."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        if value is None:
            return default
        return bool(value)

    @staticmethod
    def _unwrap(payload: Any) -> tuple[bool, Any, str]:
        """Unpack the Cloudflare envelope into (success, result, error_msg)."""
        if not isinstance(payload, dict):
            return False, None, "unexpected response shape"
        if payload.get("success"):
            return True, payload.get("result"), ""
        errors = payload.get("errors") or []
        msgs = [
            str(e.get("message", e)) if isinstance(e, dict) else str(e)
            for e in errors
        ]
        return False, payload.get("result"), "; ".join(msgs) or "request failed"

    def _request(
        self,
        op: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> tuple[ConnectorResult | None, Any]:
        """Run a request and unwrap the envelope.

        Returns ``(error_result, None)`` on failure or ``(None, result)`` on
        success, so callers can ``if err: return err`` then use the result.
        """
        try:
            with self._client() as c:
                r = c.request(method, path, params=params, json=json)
                r.raise_for_status()
                payload = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Cloudflare {op} failed: {e}"), None
        ok, result, err = self._unwrap(payload)
        if not ok:
            return ConnectorResult.fail(f"Cloudflare {op} failed: {err}"), None
        return None, result

    # ---- capabilities ----

    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability(
                "list_zones",
                "List zones on the account with status.",
                {"limit": "int: default 50"},
                "read",
            ),
            Capability(
                "dns_records",
                "List DNS records for a zone.",
                {"zone_id": "str", "limit": "int: default 100"},
                "read",
            ),
            Capability(
                "zone_analytics",
                "Report a zone's status and plan.",
                {"zone_id": "str"},
                "read",
            ),
            Capability(
                "purge_cache",
                "Purge everything from a zone's cache. Operational.",
                {"zone_id": "str"},
                "write",
            ),
            Capability(
                "create_dns_record",
                "Create a DNS record in a zone.",
                {
                    "zone_id": "str",
                    "type": "str",
                    "name": "str",
                    "content": "str",
                    "proxied": "bool: default true",
                },
                "write",
            ),
        )

    # ---- operations ----

    def op_list_zones(self, limit: int = 50) -> ConnectorResult:
        """List zones with id/name/status."""
        try:
            limit_i = int(limit)
        except (TypeError, ValueError):
            limit_i = 50
        err, result = self._request(
            "list_zones", "GET", "/zones", params={"per_page": limit_i}
        )
        if err:
            return err
        zones = result if isinstance(result, list) else []
        return ConnectorResult.success(f"{len(zones)} zones", data=zones)

    def op_dns_records(self, zone_id: str = "", limit: int = 100) -> ConnectorResult:
        """List DNS records for a zone."""
        zid = str(zone_id).strip()
        if not zid:
            return ConnectorResult.fail("Cloudflare dns_records: zone_id required")
        try:
            limit_i = int(limit)
        except (TypeError, ValueError):
            limit_i = 100
        err, result = self._request(
            "dns_records",
            "GET",
            f"/zones/{zid}/dns_records",
            params={"per_page": limit_i},
        )
        if err:
            return err
        records = result if isinstance(result, list) else []
        return ConnectorResult.success(f"{len(records)} DNS records", data=records)

    def op_zone_analytics(self, zone_id: str = "") -> ConnectorResult:
        """Report a zone's status and plan (safe, best-effort overview)."""
        zid = str(zone_id).strip()
        if not zid:
            return ConnectorResult.fail("Cloudflare zone_analytics: zone_id required")
        err, result = self._request("zone_analytics", "GET", f"/zones/{zid}")
        if err:
            return err
        zone = result if isinstance(result, dict) else {}
        name = str(zone.get("name", zid))
        status = str(zone.get("status", "unknown"))
        plan = zone.get("plan") or {}
        plan_name = str(plan.get("name", "unknown")) if isinstance(plan, dict) else "unknown"
        return ConnectorResult.success(
            f"{name}: status {status}, plan {plan_name}",
            data={"id": zid, "name": name, "status": status, "plan": plan_name, "zone": zone},
        )

    def op_purge_cache(self, zone_id: str = "") -> ConnectorResult:
        """Purge everything from a zone's cache."""
        zid = str(zone_id).strip()
        if not zid:
            return ConnectorResult.fail("Cloudflare purge_cache: zone_id required")
        err, result = self._request(
            "purge_cache",
            "POST",
            f"/zones/{zid}/purge_cache",
            json={"purge_everything": True},
        )
        if err:
            return err
        return ConnectorResult.success(
            f"Zone {zid} cache purged", data=result if isinstance(result, dict) else {"id": zid}
        )

    def op_create_dns_record(
        self,
        zone_id: str = "",
        type: str = "",
        name: str = "",
        content: str = "",
        proxied: bool = True,
    ) -> ConnectorResult:
        """Create a DNS record in a zone."""
        zid = str(zone_id).strip()
        if not zid:
            return ConnectorResult.fail("Cloudflare create_dns_record: zone_id required")
        rec_type = str(type).strip().upper()
        if not rec_type:
            return ConnectorResult.fail("Cloudflare create_dns_record: type required")
        rec_name = str(name).strip()
        if not rec_name:
            return ConnectorResult.fail("Cloudflare create_dns_record: name required")
        rec_content = str(content).strip()
        if not rec_content:
            return ConnectorResult.fail("Cloudflare create_dns_record: content required")
        body = {
            "type": rec_type,
            "name": rec_name,
            "content": rec_content,
            "proxied": self._bool(proxied, default=True),
        }
        err, result = self._request(
            "create_dns_record", "POST", f"/zones/{zid}/dns_records", json=body
        )
        if err:
            return err
        return ConnectorResult.success(
            f"DNS {rec_type} record {rec_name} created in {zid}",
            data=result if isinstance(result, dict) else {},
        )

    # ---- brief hooks ----

    def health(self) -> ConnectorResult:
        """Cheap auth check via the token-verify endpoint."""
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        err, _ = self._request("health", "GET", "/user/tokens/verify")
        if err:
            return ConnectorResult.fail(f"Cloudflare auth failed: {err.error}")
        return ConnectorResult.success("Cloudflare: connected")

    def pulse(self) -> ConnectorResult:
        """Zone count + names/status for the morning brief."""
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        r = self.op_list_zones(limit=100)
        if not r.ok:
            return r
        zones = r.data or []
        bits = [
            f"{z.get('name', z.get('id', '?'))} ({z.get('status', '?')})"
            for z in zones[:5]
        ]
        shown = ", ".join(bits) + ("…" if len(zones) > 5 else "")
        return ConnectorResult.success(
            f"{len(zones)} zones: {shown}" if zones else "0 zones",
            data={"zones": len(zones)},
        )

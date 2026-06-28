"""Meta (Facebook) Ads connector — campaigns, spend, and budget control.

Wraps the Meta Graph Marketing API. Reads live spend/ROAS for the daily
brief and exposes reversible pause/resume plus a money-moving daily-budget
change (tagged ``spend`` so the autonomy policy gates it).

Auth note: Meta authenticates by an ``access_token`` *query parameter* on
every request — not a bearer header. The ad-account id is normalized to the
``act_<id>`` form the Graph API expects.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import Capability, Connector, ConnectorResult

# Graph API version pinned for stable field/edge behavior.
_GRAPH_BASE = "https://graph.facebook.com/v21.0"


class MetaAdsConnector(Connector):
    """Connector for a single Meta Ads ad account."""

    key = "meta_ads"
    label = "Meta Ads"
    required_env = ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID")
    blurb = (
        "Set META_ACCESS_TOKEN to a long-lived token (Meta Business / Graph "
        "API Explorer, scopes: ads_read, ads_management) and META_AD_ACCOUNT_ID "
        "to your ad account id (with or without the act_ prefix). Get both at "
        "https://developers.facebook.com/tools/explorer and "
        "https://business.facebook.com/settings/ad-accounts."
    )

    # ---- helpers ----

    def _account_id(self) -> str:
        """Return the ad-account id normalized to ``act_<id>`` form."""
        raw = self.env("META_AD_ACCOUNT_ID")
        return raw if raw.startswith("act_") else f"act_{raw}"

    def _client(self) -> httpx.Client:
        """Build a Graph API client. ``access_token`` is added per-request."""
        return httpx.Client(
            base_url=_GRAPH_BASE,
            timeout=httpx.Timeout(20.0, connect=8.0),
        )

    def _params(self, **extra: Any) -> dict[str, Any]:
        """Merge the mandatory access_token into request query params."""
        params: dict[str, Any] = {"access_token": self.env("META_ACCESS_TOKEN")}
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    @staticmethod
    def _roas_value(rows: list[dict[str, Any]]) -> float | None:
        """Pull a single purchase-ROAS float out of an insights row, if any."""
        if not rows:
            return None
        roas = rows[0].get("purchase_roas")
        if isinstance(roas, list) and roas:
            try:
                return float(roas[0].get("value"))
            except (TypeError, ValueError):
                return None
        return None

    # ---- capabilities ----

    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability(
                "list_campaigns",
                "List campaigns in the ad account with status and budget.",
                {
                    "limit": "int: default 25",
                    "status": "str: optional effective_status filter",
                },
                "read",
            ),
            Capability(
                "account_insights",
                "Account-level performance metrics for a date preset.",
                {"date_preset": "str: default today (today|yesterday|last_7d|this_month)"},
                "read",
            ),
            Capability(
                "campaign_insights",
                "Per-campaign performance metrics for a date preset.",
                {"campaign_id": "str", "date_preset": "str: default last_7d"},
                "read",
            ),
            Capability(
                "pause_campaign",
                "Pause a campaign (sets status PAUSED). Reversible.",
                {"campaign_id": "str"},
                "write",
            ),
            Capability(
                "resume_campaign",
                "Resume a campaign (sets status ACTIVE). Reversible.",
                {"campaign_id": "str"},
                "write",
            ),
            Capability(
                "set_daily_budget",
                "Set a campaign's daily budget (minor units). Moves money.",
                {
                    "campaign_id": "str",
                    "amount_cents": "int: new daily budget in minor units",
                },
                "spend",
            ),
        )

    # ---- operations ----

    def op_list_campaigns(
        self, limit: int = 25, status: str = ""
    ) -> ConnectorResult:
        """List campaigns with status, objective, and budget fields."""
        try:
            limit_i = int(limit)
        except (TypeError, ValueError):
            limit_i = 25
        params = self._params(
            fields="name,status,effective_status,objective,daily_budget,lifetime_budget",
            limit=limit_i,
            effective_status=(f'["{status}"]' if str(status).strip() else None),
        )
        try:
            with self._client() as c:
                r = c.get(f"/{self._account_id()}/campaigns", params=params)
                r.raise_for_status()
                campaigns = r.json().get("data", [])
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Meta Ads list_campaigns failed: {e}")
        return ConnectorResult.success(
            f"{len(campaigns)} campaigns", data=campaigns
        )

    def op_account_insights(self, date_preset: str = "today") -> ConnectorResult:
        """Account-level spend/impressions/clicks/CTR/CPC/ROAS metrics."""
        preset = str(date_preset).strip() or "today"
        params = self._params(
            fields="spend,impressions,clicks,ctr,cpc,actions,purchase_roas",
            date_preset=preset,
        )
        try:
            with self._client() as c:
                r = c.get(f"/{self._account_id()}/insights", params=params)
                r.raise_for_status()
                rows = r.json().get("data", [])
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Meta Ads account_insights failed: {e}")
        if not rows:
            return ConnectorResult.success(
                f"No spend for {preset}", data={"rows": []}
            )
        row = rows[0]
        spend = row.get("spend", "0")
        return ConnectorResult.success(
            f"Spend {preset}: {spend}, CTR {row.get('ctr', '0')}%",
            data=row,
        )

    def op_campaign_insights(
        self, campaign_id: str = "", date_preset: str = "last_7d"
    ) -> ConnectorResult:
        """Per-campaign spend/impressions/clicks/CTR/CPC/ROAS metrics."""
        cid = str(campaign_id).strip()
        if not cid:
            return ConnectorResult.fail("Meta Ads campaign_insights: campaign_id required")
        preset = str(date_preset).strip() or "last_7d"
        params = self._params(
            fields="spend,impressions,clicks,ctr,cpc,purchase_roas",
            date_preset=preset,
        )
        try:
            with self._client() as c:
                r = c.get(f"/{cid}/insights", params=params)
                r.raise_for_status()
                rows = r.json().get("data", [])
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Meta Ads campaign_insights failed: {e}")
        if not rows:
            return ConnectorResult.success(
                f"No data for campaign {cid} ({preset})", data={"rows": []}
            )
        return ConnectorResult.success(
            f"Campaign {cid} spend {preset}: {rows[0].get('spend', '0')}",
            data=rows[0],
        )

    def op_pause_campaign(self, campaign_id: str = "") -> ConnectorResult:
        """Set a campaign's status to PAUSED (reversible)."""
        cid = str(campaign_id).strip()
        if not cid:
            return ConnectorResult.fail("Meta Ads pause_campaign: campaign_id required")
        try:
            with self._client() as c:
                r = c.post(f"/{cid}", params=self._params(status="PAUSED"))
                r.raise_for_status()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Meta Ads pause_campaign failed: {e}")
        return ConnectorResult.success(f"Campaign {cid} paused", data={"id": cid, "status": "PAUSED"})

    def op_resume_campaign(self, campaign_id: str = "") -> ConnectorResult:
        """Set a campaign's status to ACTIVE (reversible)."""
        cid = str(campaign_id).strip()
        if not cid:
            return ConnectorResult.fail("Meta Ads resume_campaign: campaign_id required")
        try:
            with self._client() as c:
                r = c.post(f"/{cid}", params=self._params(status="ACTIVE"))
                r.raise_for_status()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Meta Ads resume_campaign failed: {e}")
        return ConnectorResult.success(f"Campaign {cid} resumed", data={"id": cid, "status": "ACTIVE"})

    def op_set_daily_budget(
        self, campaign_id: str = "", amount_cents: int = 0
    ) -> ConnectorResult:
        """Set a campaign's daily budget in minor units (moves money)."""
        cid = str(campaign_id).strip()
        if not cid:
            return ConnectorResult.fail("Meta Ads set_daily_budget: campaign_id required")
        try:
            amount = int(amount_cents)
        except (TypeError, ValueError):
            return ConnectorResult.fail("Meta Ads set_daily_budget: amount_cents must be an integer")
        if amount <= 0:
            return ConnectorResult.fail("Meta Ads set_daily_budget: amount_cents must be positive")
        try:
            with self._client() as c:
                r = c.post(f"/{cid}", params=self._params(daily_budget=amount))
                r.raise_for_status()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Meta Ads set_daily_budget failed: {e}")
        return ConnectorResult.success(
            f"Campaign {cid} daily budget set to {amount}",
            data={"id": cid, "daily_budget": amount},
        )

    # ---- brief hooks ----

    def health(self) -> ConnectorResult:
        """Cheap auth/reachability check against the ad account node."""
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        try:
            with self._client() as c:
                r = c.get(
                    f"/{self._account_id()}",
                    params=self._params(fields="name,account_status"),
                )
                r.raise_for_status()
                acct = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Meta Ads auth failed: {e}")
        return ConnectorResult.success(
            f"Meta Ads: connected ({acct.get('name', self._account_id())})",
            data=acct,
        )

    def pulse(self) -> ConnectorResult:
        """Headline spend/CTR/ROAS for today, for the morning brief."""
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        r = self.op_account_insights(date_preset="today")
        if not r.ok:
            return r
        data = r.data if isinstance(r.data, dict) else {}
        rows = data.get("rows")
        if rows == []:  # no spend yet today
            return ConnectorResult.success(
                "Spend today: $0 (no activity yet)",
                data={"spend": 0.0, "roas": None},
            )
        try:
            spend = float(data.get("spend", 0) or 0)
        except (TypeError, ValueError):
            spend = 0.0
        ctr = data.get("ctr", "0")
        roas = self._roas_value([data])
        roas_txt = f"{roas:.2f}" if roas is not None else "n/a"
        return ConnectorResult.success(
            f"Spend today: ${spend:.2f}, CTR {ctr}%, ROAS {roas_txt}",
            data={"spend": spend, "roas": roas},
        )

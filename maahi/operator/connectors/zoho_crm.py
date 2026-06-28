"""Zoho CRM connector — Maahi's window into the sales pipeline.

Wraps the Zoho CRM v6 REST API so the operator can read the deal pipeline,
list contacts, search records, and log follow-up work (tasks, notes, stage
moves). Reads are side-effect free; the write capabilities create reversible
internal records (a task, a note, a stage change) and are safe to do-then-report.

Auth is a Zoho OAuth access token (header ``Authorization: Zoho-oauthtoken
<token>``). The API host varies by data-center, so it is configurable via
``ZOHO_CRM_API_DOMAIN`` (default ``https://www.zohoapis.com``); all calls hit
``{api_domain}/crm/v6``. Building the connector never touches the network.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import Capability, Connector, ConnectorResult


class ZohoCRMConnector(Connector):
    """Connector for Zoho CRM (Deals, Contacts, Leads, Tasks, Notes)."""

    key = "zoho_crm"
    label = "Zoho CRM"
    required_env = ("ZOHO_CRM_ACCESS_TOKEN",)
    blurb = (
        "Create a Self Client in the Zoho API Console "
        "(https://api-console.zoho.com), grant scope ZohoCRM.modules.ALL, and "
        "exchange the generated code for an OAuth access token. Set it as "
        "ZOHO_CRM_ACCESS_TOKEN. Optionally set ZOHO_CRM_API_DOMAIN to your "
        "data-center host (e.g. https://www.zohoapis.eu)."
    )

    # ---- http ----

    def _api_domain(self) -> str:
        """Resolve the data-center API host (no trailing slash)."""
        return self.env("ZOHO_CRM_API_DOMAIN", "https://www.zohoapis.com").rstrip("/")

    def _client(self) -> httpx.Client:
        """Build a configured, short-lived httpx client for the CRM API."""
        return httpx.Client(
            base_url=f"{self._api_domain()}/crm/v6",
            headers={
                "Authorization": f"Zoho-oauthtoken {self.env('ZOHO_CRM_ACCESS_TOKEN')}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(20.0, connect=8.0),
        )

    # ---- capabilities ----

    def capabilities(self) -> tuple[Capability, ...]:
        """Operations this connector exposes to the agent."""
        return (
            Capability(
                "list_deals",
                "List deals, optionally filtered to one stage.",
                {
                    "stage": "str: optional stage filter",
                    "limit": "int: default 20",
                },
                "read",
            ),
            Capability(
                "pipeline_summary",
                "Aggregate open deals: count and total Amount by Stage.",
                {},
                "read",
            ),
            Capability(
                "list_contacts",
                "List recent contacts.",
                {"limit": "int: default 20"},
                "read",
            ),
            Capability(
                "search",
                "Search a module by keyword.",
                {
                    "module": "str: e.g. Deals/Contacts/Leads",
                    "term": "str: search word",
                },
                "read",
            ),
            Capability(
                "create_task",
                "Create a follow-up task.",
                {
                    "subject": "str: task subject",
                    "due_date": "str: YYYY-MM-DD optional",
                    "what_id": "str: related record id optional",
                },
                "write",
            ),
            Capability(
                "add_note",
                "Attach a note to a record.",
                {
                    "parent_module": "str: e.g. Deals/Contacts",
                    "parent_id": "str: record id",
                    "title": "str: note title",
                    "content": "str: note body",
                },
                "write",
            ),
            Capability(
                "update_deal_stage",
                "Move a deal to a new stage.",
                {
                    "deal_id": "str: deal record id",
                    "stage": "str: new stage name",
                },
                "write",
            ),
        )

    # ---- operations ----

    def op_list_deals(self, stage: str = "", limit: int = 20) -> ConnectorResult:
        """List deals (optionally filtered to ``stage``)."""
        per_page = _as_int(limit, 20)
        params: dict[str, Any] = {
            "fields": "Deal_Name,Stage,Amount,Closing_Date,Account_Name",
            "per_page": per_page,
            "sort_by": "Modified_Time",
            "sort_order": "desc",
        }
        try:
            with self._client() as c:
                r = c.get("/Deals", params=params)
                if r.status_code == 204:
                    return ConnectorResult.success("0 deals", data=[])
                r.raise_for_status()
                deals = r.json().get("data", []) or []
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Zoho list deals failed: {e}")
        stage = str(stage or "").strip()
        if stage:
            deals = [d for d in deals if str(d.get("Stage", "")) == stage]
        return ConnectorResult.success(f"{len(deals)} deals", data=deals)

    def op_pipeline_summary(self) -> ConnectorResult:
        """Aggregate open deals into count + total Amount, broken out by stage."""
        try:
            with self._client() as c:
                r = c.get(
                    "/Deals",
                    params={
                        "fields": "Deal_Name,Stage,Amount,Closing_Date,Account_Name",
                        "per_page": 200,
                        "sort_by": "Modified_Time",
                        "sort_order": "desc",
                    },
                )
                if r.status_code == 204:
                    deals: list[dict[str, Any]] = []
                else:
                    r.raise_for_status()
                    deals = r.json().get("data", []) or []
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Zoho pipeline summary failed: {e}")

        by_stage: dict[str, dict[str, float]] = {}
        total_value = 0.0
        for d in deals:
            stage = str(d.get("Stage") or "Unknown")
            amount = _as_float(d.get("Amount"), 0.0)
            bucket = by_stage.setdefault(stage, {"count": 0, "value": 0.0})
            bucket["count"] += 1
            bucket["value"] += amount
            total_value += amount

        data = {
            "total_open": len(deals),
            "total_value": round(total_value, 2),
            "by_stage": {
                s: {"count": int(b["count"]), "value": round(b["value"], 2)}
                for s, b in by_stage.items()
            },
        }
        return ConnectorResult.success(
            f"{len(deals)} open deals, {data['total_value']} in pipeline",
            data=data,
        )

    def op_list_contacts(self, limit: int = 20) -> ConnectorResult:
        """List the most recently modified contacts."""
        per_page = _as_int(limit, 20)
        try:
            with self._client() as c:
                r = c.get(
                    "/Contacts",
                    params={
                        "fields": "Full_Name,Email,Phone,Account_Name",
                        "per_page": per_page,
                        "sort_by": "Modified_Time",
                        "sort_order": "desc",
                    },
                )
                if r.status_code == 204:
                    return ConnectorResult.success("0 contacts", data=[])
                r.raise_for_status()
                contacts = r.json().get("data", []) or []
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Zoho list contacts failed: {e}")
        return ConnectorResult.success(f"{len(contacts)} contacts", data=contacts)

    def op_search(self, module: str = "Deals", term: str = "") -> ConnectorResult:
        """Search ``module`` for records matching ``term``."""
        module = str(module or "Deals").strip() or "Deals"
        term = str(term or "").strip()
        if not term:
            return ConnectorResult.fail("Zoho search needs a non-empty term")
        try:
            with self._client() as c:
                r = c.get(f"/{module}/search", params={"word": term})
                if r.status_code == 204:
                    return ConnectorResult.success(f"0 results in {module}", data=[])
                r.raise_for_status()
                results = r.json().get("data", []) or []
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Zoho search failed: {e}")
        return ConnectorResult.success(
            f"{len(results)} results in {module}", data=results
        )

    def op_create_task(
        self, subject: str = "", due_date: str = "", what_id: str = ""
    ) -> ConnectorResult:
        """Create a follow-up task, optionally tied to a record."""
        subject = str(subject or "").strip()
        if not subject:
            return ConnectorResult.fail("Zoho create_task needs a subject")
        record: dict[str, Any] = {"Subject": subject}
        due_date = str(due_date or "").strip()
        if due_date:
            record["Due_Date"] = due_date
        what_id = str(what_id or "").strip()
        if what_id:
            record["What_Id"] = what_id
        try:
            with self._client() as c:
                r = c.post("/Tasks", json={"data": [record]})
                r.raise_for_status()
                payload = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Zoho create_task failed: {e}")
        new_id = _first_record_id(payload)
        return ConnectorResult.success(
            f"Task created: {subject}", data={"id": new_id, "response": payload}
        )

    def op_add_note(
        self,
        parent_module: str = "",
        parent_id: str = "",
        title: str = "",
        content: str = "",
    ) -> ConnectorResult:
        """Attach a note to a record under ``parent_module``/``parent_id``."""
        parent_module = str(parent_module or "").strip()
        parent_id = str(parent_id or "").strip()
        if not parent_module or not parent_id:
            return ConnectorResult.fail(
                "Zoho add_note needs parent_module and parent_id"
            )
        note: dict[str, Any] = {
            "Note_Title": str(title or "").strip(),
            "Note_Content": str(content or ""),
        }
        try:
            with self._client() as c:
                r = c.post(
                    f"/{parent_module}/{parent_id}/Notes", json={"data": [note]}
                )
                r.raise_for_status()
                payload = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Zoho add_note failed: {e}")
        new_id = _first_record_id(payload)
        return ConnectorResult.success(
            f"Note added to {parent_module}/{parent_id}",
            data={"id": new_id, "response": payload},
        )

    def op_update_deal_stage(
        self, deal_id: str = "", stage: str = ""
    ) -> ConnectorResult:
        """Move deal ``deal_id`` to ``stage``."""
        deal_id = str(deal_id or "").strip()
        stage = str(stage or "").strip()
        if not deal_id or not stage:
            return ConnectorResult.fail(
                "Zoho update_deal_stage needs deal_id and stage"
            )
        try:
            with self._client() as c:
                r = c.put(f"/Deals/{deal_id}", json={"data": [{"Stage": stage}]})
                r.raise_for_status()
                payload = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Zoho update_deal_stage failed: {e}")
        return ConnectorResult.success(
            f"Deal {deal_id} → {stage}", data={"response": payload}
        )

    # ---- brief hooks ----

    def health(self) -> ConnectorResult:
        """Cheap auth check via the org endpoint."""
        if not self.configured():
            return ConnectorResult.fail("Zoho CRM: not configured", not_configured=True)
        try:
            with self._client() as c:
                c.get("/org").raise_for_status()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Zoho CRM auth failed: {e}")
        return ConnectorResult.success("Zoho CRM: connected")

    def pulse(self) -> ConnectorResult:
        """Headline pipeline numbers for the morning brief."""
        r = self.op_pipeline_summary()
        if not r.ok:
            return r
        data = dict(r.data or {})
        total_open = int(data.get("total_open", 0))
        total_value = data.get("total_value", 0)
        data["closing_this_week"] = self._closing_this_week_count()
        return ConnectorResult.success(
            f"{total_open} open deals, AED {total_value} in pipeline",
            data=data,
        )

    def _closing_this_week_count(self) -> int:
        """Best-effort count of deals with Closing_Date in the next 7 days."""
        from datetime import date, timedelta

        today = date.today()
        horizon = today + timedelta(days=7)
        try:
            with self._client() as c:
                r = c.get(
                    "/Deals",
                    params={
                        "fields": "Deal_Name,Stage,Amount,Closing_Date",
                        "per_page": 200,
                        "sort_by": "Closing_Date",
                        "sort_order": "asc",
                    },
                )
                if r.status_code == 204:
                    return 0
                r.raise_for_status()
                deals = r.json().get("data", []) or []
        except httpx.HTTPError:
            return 0
        count = 0
        for d in deals:
            raw = str(d.get("Closing_Date") or "")[:10]
            try:
                cd = date.fromisoformat(raw)
            except ValueError:
                continue
            if today <= cd <= horizon:
                count += 1
        return count


# ---- helpers ----


def _as_int(value: Any, default: int) -> int:
    """Coerce ``value`` to int, falling back to ``default``."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    """Coerce ``value`` to float, falling back to ``default``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_record_id(payload: dict[str, Any]) -> str | None:
    """Pull the created/updated record id out of a Zoho write response."""
    try:
        rows = payload.get("data", []) or []
        if rows:
            return str(rows[0].get("details", {}).get("id")) or None
    except (AttributeError, TypeError, IndexError):
        pass
    return None

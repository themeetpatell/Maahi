"""Webflow connector — sites, CMS collections/items, and publishing.

Wraps the Webflow Data API v2. Lists sites and CMS content for the brief,
creates CMS items (drafts by default → reversible ``write``), and can push
a site live (``publish``, gated by the autonomy policy).

Auth is a bearer token in the ``Authorization`` header.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import Capability, Connector, ConnectorResult

_API_BASE = "https://api.webflow.com/v2"


class WebflowConnector(Connector):
    """Connector for a Webflow workspace's sites and CMS."""

    key = "webflow"
    label = "Webflow"
    required_env = ("WEBFLOW_API_TOKEN",)
    blurb = (
        "Set WEBFLOW_API_TOKEN to a Data API token from your Webflow site "
        "settings -> Apps & integrations -> API access (generate an API "
        "token with CMS + sites scopes)."
    )

    # ---- helpers ----

    def _client(self) -> httpx.Client:
        """Build a Webflow Data API v2 client with bearer auth."""
        return httpx.Client(
            base_url=_API_BASE,
            headers={
                "Authorization": f"Bearer {self.env('WEBFLOW_API_TOKEN')}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(20.0, connect=8.0),
        )

    # ---- capabilities ----

    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability(
                "list_sites",
                "List all sites in the workspace.",
                {},
                "read",
            ),
            Capability(
                "list_collections",
                "List CMS collections for a site.",
                {"site_id": "str"},
                "read",
            ),
            Capability(
                "list_items",
                "List CMS items in a collection.",
                {"collection_id": "str", "limit": "int: default 20"},
                "read",
            ),
            Capability(
                "create_item",
                "Create a CMS item (draft by default). Reversible.",
                {
                    "collection_id": "str",
                    "fields": "dict: field slug->value",
                    "is_draft": "bool: default true",
                },
                "write",
            ),
            Capability(
                "publish_site",
                "Publish a site to its Webflow subdomain. Makes live.",
                {"site_id": "str"},
                "publish",
            ),
        )

    # ---- operations ----

    def op_list_sites(self) -> ConnectorResult:
        """List every site in the workspace."""
        try:
            with self._client() as c:
                r = c.get("/sites")
                r.raise_for_status()
                sites = r.json().get("sites", [])
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Webflow list_sites failed: {e}")
        return ConnectorResult.success(f"{len(sites)} sites", data=sites)

    def op_list_collections(self, site_id: str = "") -> ConnectorResult:
        """List CMS collections for a site."""
        sid = str(site_id).strip()
        if not sid:
            return ConnectorResult.fail("Webflow list_collections: site_id required")
        try:
            with self._client() as c:
                r = c.get(f"/sites/{sid}/collections")
                r.raise_for_status()
                collections = r.json().get("collections", [])
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Webflow list_collections failed: {e}")
        return ConnectorResult.success(
            f"{len(collections)} collections", data=collections
        )

    def op_list_items(
        self, collection_id: str = "", limit: int = 20
    ) -> ConnectorResult:
        """List CMS items within a collection."""
        cid = str(collection_id).strip()
        if not cid:
            return ConnectorResult.fail("Webflow list_items: collection_id required")
        try:
            limit_i = int(limit)
        except (TypeError, ValueError):
            limit_i = 20
        try:
            with self._client() as c:
                r = c.get(f"/collections/{cid}/items", params={"limit": limit_i})
                r.raise_for_status()
                items = r.json().get("items", [])
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Webflow list_items failed: {e}")
        return ConnectorResult.success(f"{len(items)} items", data=items)

    def op_create_item(
        self,
        collection_id: str = "",
        fields: dict[str, Any] | None = None,
        is_draft: bool = True,
    ) -> ConnectorResult:
        """Create a CMS item; defaults to a draft (reversible)."""
        cid = str(collection_id).strip()
        if not cid:
            return ConnectorResult.fail("Webflow create_item: collection_id required")
        field_data = fields if isinstance(fields, dict) else {}
        if not field_data:
            return ConnectorResult.fail("Webflow create_item: fields required")
        draft = bool(is_draft) if not isinstance(is_draft, str) else is_draft.strip().lower() in ("1", "true", "yes")
        body = {"isDraftItem": draft, "fieldData": field_data}
        try:
            with self._client() as c:
                r = c.post(f"/collections/{cid}/items", json=body)
                r.raise_for_status()
                item = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Webflow create_item failed: {e}")
        return ConnectorResult.success(
            f"Item created in {cid} (draft={draft})", data=item
        )

    def op_publish_site(self, site_id: str = "") -> ConnectorResult:
        """Publish a site to its Webflow subdomain (makes it live)."""
        sid = str(site_id).strip()
        if not sid:
            return ConnectorResult.fail("Webflow publish_site: site_id required")
        try:
            with self._client() as c:
                r = c.post(
                    f"/sites/{sid}/publish",
                    json={"publishToWebflowSubdomain": True},
                )
                r.raise_for_status()
                result = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Webflow publish_site failed: {e}")
        return ConnectorResult.success(f"Site {sid} published", data=result)

    # ---- brief hooks ----

    def health(self) -> ConnectorResult:
        """Cheap auth/reachability check via the sites list."""
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        try:
            with self._client() as c:
                c.get("/sites").raise_for_status()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Webflow auth failed: {e}")
        return ConnectorResult.success("Webflow: connected")

    def pulse(self) -> ConnectorResult:
        """Site count + names (and last-publish of the first) for the brief."""
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        r = self.op_list_sites()
        if not r.ok:
            return r
        sites = r.data or []
        names = [str(s.get("displayName") or s.get("shortName") or s.get("id", "?")) for s in sites]
        shown = ", ".join(names[:5]) + ("…" if len(names) > 5 else "")
        last_published = sites[0].get("lastPublished") if sites else None
        return ConnectorResult.success(
            f"{len(sites)} sites: {shown}" if sites else "0 sites",
            data={"sites": len(sites), "names": names, "last_published": last_published},
        )

"""Notion connector — Maahi's reach into the team workspace.

Wraps the Notion API (v1) so the operator can search the workspace, read a
page, query a database, create a draft page, and append text to a page/block.
Reads have no side effects; ``create_page`` and ``append_block`` are reversible
internal mutations (a new page / extra paragraph), so they are tagged ``write``.

Auth is an internal-integration bearer token (``Authorization: Bearer <token>``)
plus the pinned ``Notion-Version`` header the API requires. Only pages and
databases explicitly shared with the integration are visible. Building the
connector never touches the network.
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import Capability, Connector, ConnectorResult

_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


class NotionConnector(Connector):
    """Connector for a Notion workspace (search, read, query, create)."""

    key = "notion"
    label = "Notion"
    required_env = ("NOTION_TOKEN",)
    blurb = (
        "Create an internal integration at "
        "https://www.notion.so/my-integrations, copy its secret into "
        "NOTION_TOKEN, then share the relevant pages/databases with the "
        "integration (… menu -> Connections) so Maahi can see them."
    )

    # ---- http ----

    def _client(self) -> httpx.Client:
        """Build a short-lived httpx client for the Notion API."""
        return httpx.Client(
            base_url=_API_BASE,
            headers={
                "Authorization": f"Bearer {self.env('NOTION_TOKEN')}",
                "Notion-Version": _NOTION_VERSION,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(20.0, connect=8.0),
        )

    # ---- capabilities ----

    def capabilities(self) -> tuple[Capability, ...]:
        """Operations this connector exposes to the agent."""
        return (
            Capability(
                "search",
                "Search shared pages and databases by title text.",
                {"query": "str", "limit": "int: default 10"},
                "read",
            ),
            Capability(
                "get_page",
                "Fetch one page's properties and metadata.",
                {"page_id": "str"},
                "read",
            ),
            Capability(
                "query_database",
                "List rows (pages) in a database.",
                {"database_id": "str", "limit": "int: default 20"},
                "read",
            ),
            Capability(
                "create_page",
                "Create a page under a parent page or database. Reversible.",
                {
                    "parent_id": "str: page or database id",
                    "title": "str",
                    "content": "str: optional plain text",
                    "parent_type": "str: page|database default page",
                },
                "write",
            ),
            Capability(
                "append_block",
                "Append a plain-text paragraph to a page or block. Reversible.",
                {"block_id": "str: page or block id", "text": "str"},
                "write",
            ),
        )

    # ---- operations ----

    def op_search(self, query: str = "", limit: int = 10) -> ConnectorResult:
        """Search shared pages/databases, newest-edited first."""
        q = str(query or "").strip()
        page_size = _as_int(limit, 10)
        body: dict[str, Any] = {
            "query": q,
            "page_size": page_size,
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        }
        try:
            with self._client() as c:
                r = c.post("/search", json=body)
                r.raise_for_status()
                results = (r.json() or {}).get("results") or []
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Notion search failed: {e}")
        items = [
            {
                "id": obj.get("id"),
                "object": obj.get("object"),
                "title": self._title_of(obj),
                "url": obj.get("url"),
                "last_edited_time": obj.get("last_edited_time"),
            }
            for obj in results
        ]
        return ConnectorResult.success(f"{len(items)} results", data=items)

    def op_get_page(self, page_id: str = "") -> ConnectorResult:
        """Fetch a single page object by id."""
        pid = str(page_id or "").strip()
        if not pid:
            return ConnectorResult.fail("Notion get_page: page_id required")
        try:
            with self._client() as c:
                r = c.get(f"/pages/{pid}")
                r.raise_for_status()
                page = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Notion get_page failed: {e}")
        return ConnectorResult.success(f"Page: {self._title_of(page)}", data=page)

    def op_query_database(
        self, database_id: str = "", limit: int = 20
    ) -> ConnectorResult:
        """List rows (pages) within a database."""
        did = str(database_id or "").strip()
        if not did:
            return ConnectorResult.fail("Notion query_database: database_id required")
        page_size = _as_int(limit, 20)
        try:
            with self._client() as c:
                r = c.post(f"/databases/{did}/query", json={"page_size": page_size})
                r.raise_for_status()
                rows = (r.json() or {}).get("results") or []
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Notion query_database failed: {e}")
        return ConnectorResult.success(f"{len(rows)} rows", data=rows)

    def op_create_page(
        self,
        parent_id: str = "",
        title: str = "",
        content: str = "",
        parent_type: str = "page",
    ) -> ConnectorResult:
        """Create a page under a parent page or database (reversible)."""
        pid = str(parent_id or "").strip()
        if not pid:
            return ConnectorResult.fail("Notion create_page: parent_id required")
        title_text = str(title or "").strip()
        if not title_text:
            return ConnectorResult.fail("Notion create_page: title required")
        ptype = str(parent_type or "page").strip().lower()

        title_value = {"title": [{"text": {"content": title_text}}]}
        if ptype == "database":
            body: dict[str, Any] = {
                "parent": {"database_id": pid},
                "properties": {"Name": title_value},
            }
        else:
            body = {
                "parent": {"page_id": pid},
                "properties": {"title": title_value},
            }

        text = str(content or "").strip()
        if text:
            body["children"] = [_paragraph_block(text)]

        try:
            with self._client() as c:
                r = c.post("/pages", json=body)
                r.raise_for_status()
                page = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Notion create_page failed: {e}")
        return ConnectorResult.success(
            f"Page created: {title_text}",
            data={"id": page.get("id"), "url": page.get("url"), "response": page},
        )

    def op_append_block(self, block_id: str = "", text: str = "") -> ConnectorResult:
        """Append a plain-text paragraph to a page or block (reversible)."""
        bid = str(block_id or "").strip()
        if not bid:
            return ConnectorResult.fail("Notion append_block: block_id required")
        body = str(text or "").strip()
        if not body:
            return ConnectorResult.fail("Notion append_block: text required")
        try:
            with self._client() as c:
                r = c.patch(
                    f"/blocks/{bid}/children",
                    json={"children": [_paragraph_block(body)]},
                )
                r.raise_for_status()
                payload = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Notion append_block failed: {e}")
        return ConnectorResult.success(f"Appended to {bid}", data=payload)

    # ---- brief hooks ----

    def health(self) -> ConnectorResult:
        """Cheap auth check via the bot-user endpoint."""
        if not self.configured():
            return ConnectorResult.fail("Notion: not configured", not_configured=True)
        try:
            with self._client() as c:
                r = c.get("/users/me")
                r.raise_for_status()
                me = r.json() or {}
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Notion auth failed: {e}")
        name = me.get("name") or (me.get("bot") or {}).get("workspace_name") or "bot"
        return ConnectorResult.success(f"Notion: connected as {name}")

    def pulse(self) -> ConnectorResult:
        """Recently edited pages for the morning brief."""
        r = self.op_search(query="", limit=10)
        if not r.ok:
            return r
        items = r.data or []
        titles = [str(i.get("title") or "(untitled)") for i in items]
        shown = ", ".join(titles[:3]) if titles else "none"
        return ConnectorResult.success(
            f"{len(items)} recently edited: {shown}",
            data={"recent": len(items), "titles": titles[:5]},
        )

    # ---- helpers ----

    def _title_of(self, obj: dict[str, Any]) -> str:
        """Pull a human-readable title out of a Notion page/database object.

        Databases expose a top-level ``title`` rich-text array; pages carry
        their title inside whichever property has ``type == "title"``. Falls
        back to ``"(untitled)"`` when nothing readable is present.
        """
        if not isinstance(obj, dict):
            return "(untitled)"
        # Database objects: top-level title rich-text array.
        top = obj.get("title")
        text = _plain_text(top)
        if text:
            return text
        # Page objects: find the property whose type is "title".
        props = obj.get("properties")
        if isinstance(props, dict):
            for prop in props.values():
                if isinstance(prop, dict) and prop.get("type") == "title":
                    text = _plain_text(prop.get("title"))
                    if text:
                        return text
        return "(untitled)"


# ---- module helpers ----


def _plain_text(rich: Any) -> str:
    """Concatenate ``plain_text`` from a Notion rich-text array."""
    if not isinstance(rich, list):
        return ""
    parts: list[str] = []
    for span in rich:
        if isinstance(span, dict):
            parts.append(str(span.get("plain_text") or ""))
    return "".join(parts).strip()


def _paragraph_block(text: str) -> dict[str, Any]:
    """Build a Notion paragraph block carrying ``text``."""
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _as_int(value: Any, default: int) -> int:
    """Coerce ``value`` to int, falling back to ``default``."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default

"""Gmail connector — Maahi's read/write reach into the inbox.

Wraps the Gmail REST API (v1, ``users/me``) so the operator can triage unread
mail, search the mailbox, read a full message, draft a reply, and send. Reads
have no side effects; ``draft`` creates a reversible internal draft; ``send``
puts a message in front of a human (outbound).

Auth is an OAuth2 bearer access token (``Authorization: Bearer <token>``) with
the ``gmail.modify`` scope. These tokens are short-lived (~1h), so a stale token
surfaces as a clean auth failure rather than a crash. Building the connector
never touches the network.
"""
from __future__ import annotations

import base64
import binascii
from email.message import EmailMessage
from typing import Any

import httpx

from .base import Capability, Connector, ConnectorResult

_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailConnector(Connector):
    """Connector for Gmail (triage, search, read, draft, send)."""

    key = "gmail"
    label = "Gmail"
    required_env = ("GMAIL_ACCESS_TOKEN",)
    blurb = (
        "Mint an OAuth2 access token with scope "
        "https://www.googleapis.com/auth/gmail.modify (e.g. via the Google "
        "OAuth 2.0 Playground at https://developers.google.com/oauthplayground) "
        "and set it as GMAIL_ACCESS_TOKEN. Note: access tokens expire ~1h, so "
        "refresh it before each session."
    )

    # ---- http ----

    def _client(self) -> httpx.Client:
        """Build a configured, short-lived httpx client for the Gmail API."""
        return httpx.Client(
            base_url=_API_BASE,
            headers={
                "Authorization": f"Bearer {self.env('GMAIL_ACCESS_TOKEN')}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(20.0, connect=8.0),
        )

    # ---- capabilities ----

    def capabilities(self) -> tuple[Capability, ...]:
        """Operations this connector exposes to the agent."""
        return (
            Capability(
                "list_unread",
                "List unread messages (from/subject/date/snippet).",
                {"limit": "int: default 10"},
                "read",
            ),
            Capability(
                "search",
                "Search the mailbox with a Gmail query string.",
                {
                    "query": "str: Gmail search query (e.g. from:boss is:unread)",
                    "limit": "int: default 10",
                },
                "read",
            ),
            Capability(
                "get_message",
                "Fetch one message's subject, from, and plaintext body.",
                {"id": "str: Gmail message id"},
                "read",
            ),
            Capability(
                "draft",
                "Create a draft email (from: me).",
                {
                    "to": "str: recipient address",
                    "subject": "str: subject line",
                    "body": "str: plaintext body",
                },
                "write",
            ),
            Capability(
                "send",
                "Send an email (from: me).",
                {
                    "to": "str: recipient address",
                    "subject": "str: subject line",
                    "body": "str: plaintext body",
                },
                "send",
            ),
        )

    # ---- operations ----

    def op_list_unread(self, limit: int = 10) -> ConnectorResult:
        """List unread messages with lightweight metadata."""
        return self._list_query("is:unread", limit, label="unread")

    def op_search(self, query: str = "", limit: int = 10) -> ConnectorResult:
        """Search the mailbox with a Gmail ``query`` string."""
        query = str(query or "").strip()
        if not query:
            return ConnectorResult.fail("Gmail search needs a non-empty query")
        return self._list_query(query, limit, label="match")

    def _list_query(self, q: str, limit: Any, *, label: str) -> ConnectorResult:
        """Run a list query then hydrate each id with metadata headers."""
        max_results = _as_int(limit, 10)
        try:
            with self._client() as c:
                r = c.get(
                    "/messages",
                    params={"q": q, "maxResults": max_results},
                )
                r.raise_for_status()
                ids = [m.get("id") for m in (r.json().get("messages") or [])]
                messages: list[dict[str, Any]] = []
                for mid in ids:
                    if not mid:
                        continue
                    mr = c.get(
                        f"/messages/{mid}",
                        params=[
                            ("format", "metadata"),
                            ("metadataHeaders", "From"),
                            ("metadataHeaders", "Subject"),
                            ("metadataHeaders", "Date"),
                        ],
                    )
                    mr.raise_for_status()
                    messages.append(_summarize_message(mr.json()))
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Gmail list failed: {e}")
        noun = label + ("s" if len(messages) != 1 else "")
        return ConnectorResult.success(f"{len(messages)} {noun}", data=messages)

    def op_get_message(self, id: str = "") -> ConnectorResult:
        """Fetch a full message and extract subject/from/plaintext body."""
        msg_id = str(id or "").strip()
        if not msg_id:
            return ConnectorResult.fail("Gmail get_message needs an id")
        try:
            with self._client() as c:
                r = c.get(f"/messages/{msg_id}", params={"format": "full"})
                r.raise_for_status()
                payload = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Gmail get_message failed: {e}")
        data = _summarize_message(payload)
        data["body"] = _extract_plaintext(payload.get("payload") or {})
        return ConnectorResult.success(
            f"Message: {data.get('subject') or '(no subject)'}", data=data
        )

    def op_draft(
        self, to: str = "", subject: str = "", body: str = ""
    ) -> ConnectorResult:
        """Create a draft email addressed to ``to``."""
        to = str(to or "").strip()
        if not to:
            return ConnectorResult.fail("Gmail draft needs a 'to' address")
        raw = _build_raw_message(to, str(subject or ""), str(body or ""))
        try:
            with self._client() as c:
                r = c.post("/drafts", json={"message": {"raw": raw}})
                r.raise_for_status()
                payload = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Gmail draft failed: {e}")
        return ConnectorResult.success(
            f"Draft created to {to}",
            data={"id": payload.get("id"), "response": payload},
        )

    def op_send(
        self, to: str = "", subject: str = "", body: str = ""
    ) -> ConnectorResult:
        """Send an email to ``to``."""
        to = str(to or "").strip()
        if not to:
            return ConnectorResult.fail("Gmail send needs a 'to' address")
        raw = _build_raw_message(to, str(subject or ""), str(body or ""))
        try:
            with self._client() as c:
                r = c.post("/messages/send", json={"raw": raw})
                r.raise_for_status()
                payload = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Gmail send failed: {e}")
        return ConnectorResult.success(
            f"Sent to {to}", data={"id": payload.get("id"), "response": payload}
        )

    # ---- brief hooks ----

    def health(self) -> ConnectorResult:
        """Cheap auth check via the profile endpoint."""
        if not self.configured():
            return ConnectorResult.fail("Gmail: not configured", not_configured=True)
        try:
            with self._client() as c:
                r = c.get("/profile")
                r.raise_for_status()
                email = (r.json() or {}).get("emailAddress")
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Gmail auth failed: {e}")
        if not email:
            return ConnectorResult.fail("Gmail auth failed: no emailAddress in profile")
        return ConnectorResult.success(f"Gmail: connected as {email}")

    def pulse(self) -> ConnectorResult:
        """Headline inbox numbers for the morning brief."""
        r = self.op_list_unread(limit=20)
        if not r.ok:
            return r
        messages = r.data or []
        senders = [str(m.get("from") or "").strip() for m in messages]
        senders = [s for s in senders if s]
        top = ", ".join(senders[:2]) if senders else "none"
        return ConnectorResult.success(
            f"{len(messages)} unread, top from: {top}",
            data={"unread": len(messages), "top_senders": senders[:5]},
        )


# ---- helpers ----


def _build_raw_message(to: str, subject: str, body: str) -> str:
    """Build a MIME message and base64url-encode it for the Gmail API.

    ``from`` is left to Gmail (the authenticated user). Returns an unpadded
    URL-safe base64 string as Gmail's ``raw`` field expects.
    """
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _header(payload: dict[str, Any], name: str) -> str:
    """Case-insensitive lookup of a header value from a message payload."""
    headers = (payload.get("payload") or {}).get("headers") or []
    target = name.lower()
    for h in headers:
        if str(h.get("name", "")).lower() == target:
            return str(h.get("value", ""))
    return ""


def _summarize_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Gmail message resource to id/from/subject/date/snippet."""
    return {
        "id": payload.get("id"),
        "from": _header(payload, "From"),
        "subject": _header(payload, "Subject"),
        "date": _header(payload, "Date"),
        "snippet": payload.get("snippet", ""),
    }


def _extract_plaintext(part: dict[str, Any]) -> str:
    """Walk a MIME part tree and return the first text/plain body, decoded."""
    mime = str(part.get("mimeType") or "")
    body = part.get("body") or {}
    data = body.get("data")
    if mime == "text/plain" and data:
        return _decode_b64url(data)
    for sub in part.get("parts") or []:
        found = _extract_plaintext(sub)
        if found:
            return found
    # Fallback: any decodable body data (e.g. a bare text/* message).
    if data and mime.startswith("text/"):
        return _decode_b64url(data)
    return ""


def _decode_b64url(data: str) -> str:
    """Decode Gmail's URL-safe base64 body data into text, tolerating padding."""
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode(
            "utf-8", errors="replace"
        )
    except (binascii.Error, ValueError):
        return ""


def _as_int(value: Any, default: int) -> int:
    """Coerce ``value`` to int, falling back to ``default``."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default

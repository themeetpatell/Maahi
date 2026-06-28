"""Google Drive connector — Maahi's reach into shared files and docs.

Wraps the Google Drive API (v3) so the operator can search files, list recent
activity, read a file's metadata or text body, and create a draft Google Doc.
Reads have no side effects; ``create_doc`` writes a new Doc into Drive, a
reversible internal mutation, so it is tagged ``write``.

Auth is an OAuth2 bearer access token (``Authorization: Bearer <token>``) with
the ``drive`` scope. These tokens are short-lived (~1h), so a stale token
surfaces as a clean auth failure rather than a crash. Building the connector
never touches the network.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .base import Capability, Connector, ConnectorResult

_API_BASE = "https://www.googleapis.com/drive/v3"
_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
_ABOUT_URL = "https://www.googleapis.com/drive/v3/about"
_DOC_MIME = "application/vnd.google-apps.document"
_FILE_FIELDS = "files(id,name,mimeType,modifiedTime,webViewLink,owners)"
_META_FIELDS = "id,name,mimeType,modifiedTime,size,webViewLink,owners"
_MAX_TEXT = 8000


class GoogleDriveConnector(Connector):
    """Connector for Google Drive (search, list, read, create docs)."""

    key = "gdrive"
    label = "Google Drive"
    required_env = ("GDRIVE_ACCESS_TOKEN",)
    blurb = (
        "Mint an OAuth2 access token with scope "
        "https://www.googleapis.com/auth/drive (e.g. via the Google OAuth 2.0 "
        "Playground at https://developers.google.com/oauthplayground) and set "
        "it as GDRIVE_ACCESS_TOKEN. Note: access tokens expire ~1h, so refresh "
        "it before each session."
    )

    # ---- http ----

    def _client(self) -> httpx.Client:
        """Build a short-lived httpx client carrying the OAuth bearer token.

        No ``base_url`` is set so the same client can hit the Drive API, the
        upload endpoint, and the ``about`` endpoint (different hosts/paths).
        """
        return httpx.Client(
            headers={"Authorization": f"Bearer {self.env('GDRIVE_ACCESS_TOKEN')}"},
            timeout=httpx.Timeout(20.0, connect=8.0),
        )

    # ---- capabilities ----

    def capabilities(self) -> tuple[Capability, ...]:
        """Operations this connector exposes to the agent."""
        return (
            Capability(
                "search",
                "Search Drive files by query (auto-wraps bare text as a "
                "name-contains match).",
                {
                    "query": "str: Drive query, e.g. name contains 'pitch'",
                    "limit": "int: default 20",
                },
                "read",
            ),
            Capability(
                "list_recent",
                "List the most recently modified files.",
                {"limit": "int: default 20"},
                "read",
            ),
            Capability(
                "get_metadata",
                "Fetch one file's metadata (name, type, size, link, owners).",
                {"file_id": "str"},
                "read",
            ),
            Capability(
                "read_file",
                "Read a file's text (exports Google Docs to plain text).",
                {"file_id": "str"},
                "read",
            ),
            Capability(
                "create_doc",
                "Create a Google Doc with optional plain-text body. Reversible.",
                {"name": "str", "content": "str: optional plain text"},
                "write",
            ),
        )

    # ---- operations ----

    def op_search(self, query: str = "", limit: int = 20) -> ConnectorResult:
        """Search Drive; wrap bare text as a ``name contains`` query."""
        q = str(query or "").strip()
        if not q:
            return ConnectorResult.fail("Google Drive search needs a non-empty query")
        if not _looks_like_query(q):
            safe = q.replace("'", "\\'")
            q = f"name contains '{safe}'"
        page_size = _as_int(limit, 20)
        try:
            with self._client() as c:
                r = c.get(
                    _API_BASE + "/files",
                    params={"q": q, "pageSize": page_size, "fields": _FILE_FIELDS},
                )
                r.raise_for_status()
                files = (r.json() or {}).get("files") or []
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Google Drive search failed: {e}")
        return ConnectorResult.success(f"{len(files)} files", data=files)

    def op_list_recent(self, limit: int = 20) -> ConnectorResult:
        """List the most recently modified files."""
        page_size = _as_int(limit, 20)
        try:
            with self._client() as c:
                r = c.get(
                    _API_BASE + "/files",
                    params={
                        "orderBy": "modifiedTime desc",
                        "pageSize": page_size,
                        "fields": _FILE_FIELDS,
                    },
                )
                r.raise_for_status()
                files = (r.json() or {}).get("files") or []
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Google Drive list_recent failed: {e}")
        return ConnectorResult.success(f"{len(files)} files", data=files)

    def op_get_metadata(self, file_id: str = "") -> ConnectorResult:
        """Fetch a single file's metadata."""
        fid = str(file_id or "").strip()
        if not fid:
            return ConnectorResult.fail("Google Drive get_metadata: file_id required")
        try:
            with self._client() as c:
                r = c.get(_API_BASE + f"/files/{fid}", params={"fields": _META_FIELDS})
                r.raise_for_status()
                meta = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Google Drive get_metadata failed: {e}")
        return ConnectorResult.success(
            f"File: {meta.get('name') or fid}", data=meta
        )

    def op_read_file(self, file_id: str = "") -> ConnectorResult:
        """Read a file's text, exporting Google Docs to plain text."""
        fid = str(file_id or "").strip()
        if not fid:
            return ConnectorResult.fail("Google Drive read_file: file_id required")
        try:
            with self._client() as c:
                # Determine the type so Docs are exported rather than downloaded.
                meta_r = c.get(
                    _API_BASE + f"/files/{fid}", params={"fields": "id,name,mimeType"}
                )
                meta_r.raise_for_status()
                meta = meta_r.json() or {}
                mime = str(meta.get("mimeType") or "")
                if mime == _DOC_MIME:
                    r = c.get(
                        _API_BASE + f"/files/{fid}/export",
                        params={"mimeType": "text/plain"},
                    )
                else:
                    r = c.get(_API_BASE + f"/files/{fid}", params={"alt": "media"})
                r.raise_for_status()
                text = r.text
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Google Drive read_file failed: {e}")
        truncated = len(text) > _MAX_TEXT
        body = text[:_MAX_TEXT]
        return ConnectorResult.success(
            f"Read {meta.get('name') or fid} ({len(body)} chars"
            + (", truncated" if truncated else "") + ")",
            data={
                "id": fid,
                "name": meta.get("name"),
                "mimeType": mime,
                "text": body,
                "truncated": truncated,
            },
        )

    def op_create_doc(self, name: str = "", content: str = "") -> ConnectorResult:
        """Create a Google Doc via multipart upload (reversible)."""
        doc_name = str(name or "").strip()
        if not doc_name:
            return ConnectorResult.fail("Google Drive create_doc: name required")
        body_text = str(content or "")
        metadata = {"name": doc_name, "mimeType": _DOC_MIME}
        # Multipart "related": a JSON metadata part + a plain-text body part.
        # httpx doesn't build multipart/related, so assemble it by hand.
        boundary = "maahi-gdrive-boundary"
        parts = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            "Content-Type: text/plain; charset=UTF-8\r\n\r\n"
            f"{body_text}\r\n"
            f"--{boundary}--"
        )
        try:
            with self._client() as c:
                r = c.post(
                    _UPLOAD_URL,
                    params={"uploadType": "multipart"},
                    content=parts.encode("utf-8"),
                    headers={
                        "Content-Type": f"multipart/related; boundary={boundary}"
                    },
                )
                r.raise_for_status()
                doc = r.json()
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Google Drive create_doc failed: {e}")
        return ConnectorResult.success(
            f"Doc created: {doc_name}",
            data={
                "id": doc.get("id"),
                "name": doc.get("name") or doc_name,
                "webViewLink": doc.get("webViewLink"),
                "response": doc,
            },
        )

    # ---- brief hooks ----

    def health(self) -> ConnectorResult:
        """Cheap auth check via the Drive ``about`` endpoint."""
        if not self.configured():
            return ConnectorResult.fail(
                "Google Drive: not configured", not_configured=True
            )
        try:
            with self._client() as c:
                r = c.get(_ABOUT_URL, params={"fields": "user"})
                r.raise_for_status()
                email = ((r.json() or {}).get("user") or {}).get("emailAddress")
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"Google Drive auth failed: {e}")
        if not email:
            return ConnectorResult.fail(
                "Google Drive auth failed: no user.emailAddress in about"
            )
        return ConnectorResult.success(f"Google Drive: connected as {email}")

    def pulse(self) -> ConnectorResult:
        """Recently modified files for the morning brief."""
        r = self.op_list_recent(limit=10)
        if not r.ok:
            return r
        files = r.data or []
        names = [str(f.get("name") or "(unnamed)") for f in files]
        shown = ", ".join(names[:3]) if names else "none"
        return ConnectorResult.success(
            f"{len(files)} recent files: {shown}",
            data={"recent": len(files), "names": names[:5]},
        )


# ---- module helpers ----


def _looks_like_query(q: str) -> bool:
    """Heuristic: does ``q`` already use Drive query operators?

    Drive queries contain operators like ``contains``, ``=``, ``in``,
    ``and``/``or``, or field names such as ``name``/``mimeType``. If none are
    present we treat the string as a bare term to wrap in ``name contains``.
    """
    low = f" {q.lower()} "
    operators = (
        " contains ",
        "=",
        "!=",
        "<",
        ">",
        " in ",
        " and ",
        " or ",
        " not ",
        "name",
        "mimetype",
        "modifiedtime",
        "trashed",
        "fullText",
        "parents",
        "owners",
        "starred",
        "sharedwithme",
    )
    return any(op in low for op in operators)


def _as_int(value: Any, default: int) -> int:
    """Coerce ``value`` to int, falling back to ``default``."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default

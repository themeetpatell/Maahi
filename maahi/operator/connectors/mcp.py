"""MCP connector — a generic client for remote Model Context Protocol servers.

Unlike every other connector (which wraps one specific SaaS API), this one is
a *meta* connector: it speaks the Model Context Protocol's **Streamable HTTP
transport** to any remote MCP server the user configures, and surfaces those
servers' tools so Maahi can reach them.

Transport in one breath: MCP over Streamable HTTP is JSON-RPC 2.0 carried as
HTTP POST bodies. A server replies either as a single JSON object
(``Content-Type: application/json``) or as a Server-Sent Events stream
(``text/event-stream``) where the JSON-RPC response rides in a ``data:`` line.
A short handshake (``initialize`` → ``notifications/initialized``) opens a
session; the server may hand back an ``mcp-session-id`` header that must be
echoed on every subsequent request.

Configuration lives in one env var, ``MAAHI_MCP_SERVERS`` — a JSON list of
``{"name", "url", "token"}`` objects. Any parse failure degrades to "no
servers" rather than crashing.

The capabilities here are *static wrappers*: ``list_servers``, ``list_tools``,
``call_tool``, ``ping``. The remote MCP tools themselves are addressed by name
through ``call_tool``'s ``tool``/``arguments`` params. Because we cannot know a
remote tool's true risk, ``call_tool`` is conservatively tagged ``write``.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .base import Capability, Connector, ConnectorResult

# MCP protocol revision we advertise in the handshake. Servers negotiate down
# if they speak an older one; this is just our preferred version.
_PROTOCOL_VERSION = "2025-06-18"

# Streamable HTTP wants both content types on Accept so the server may choose
# to answer as plain JSON or as an SSE stream.
_ACCEPT = "application/json, text/event-stream"

# Response header carrying the session id assigned by the server.
_SESSION_HEADER = "mcp-session-id"

# Network timeout for a single POST. Generous: a remote tool call may be slow.
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class MCPConnector(Connector):
    """Generic client for remote MCP servers over Streamable HTTP.

    Reads its server roster from ``MAAHI_MCP_SERVERS`` and exposes a fixed set
    of wrapper capabilities for listing servers, listing a server's tools,
    invoking a tool, and pinging a server.
    """

    key = "mcp"
    label = "MCP"
    required_env = ("MAAHI_MCP_SERVERS",)
    blurb = (
        "Set MAAHI_MCP_SERVERS to a JSON list of {name,url,token} remote MCP "
        "endpoints (Streamable HTTP)."
    )

    # ---- configuration helpers ----

    def _servers(self) -> list[dict]:
        """Parse ``MAAHI_MCP_SERVERS`` into a list of server dicts.

        Returns ``[]`` on *any* problem — missing var, invalid JSON, wrong
        shape — so malformed config always reads as "no servers configured"
        and never raises.
        """
        raw = self.env("MAAHI_MCP_SERVERS")
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if not isinstance(parsed, list):
            return []
        servers: list[dict] = []
        for item in parsed:
            if isinstance(item, dict) and str(item.get("name", "")).strip():
                servers.append(item)
        return servers

    def _find_server(self, name: str) -> dict | None:
        """Look up a configured server by its ``name`` (case-sensitive)."""
        target = str(name or "").strip()
        if not target:
            return None
        for server in self._servers():
            if str(server.get("name", "")).strip() == target:
                return server
        return None

    @staticmethod
    def _server_url(server: dict) -> str:
        """Extract a server's endpoint URL, trimmed."""
        return str(server.get("url", "")).strip()

    @staticmethod
    def _server_token(server: dict) -> str:
        """Extract a server's optional bearer token, trimmed."""
        return str(server.get("token", "")).strip()

    # ---- transport ----

    @staticmethod
    def _parse_sse(body: str) -> Any:
        """Pull the JSON-RPC payload out of an SSE body.

        Scans for the *last* ``data:`` line and ``json.loads`` it. Returns
        ``None`` if no usable ``data:`` line is found or it is not valid JSON.
        """
        last_data: str | None = None
        for line in body.splitlines():
            if line.startswith("data:"):
                chunk = line[len("data:"):].strip()
                if chunk:
                    last_data = chunk
        if last_data is None:
            return None
        try:
            return json.loads(last_data)
        except (ValueError, TypeError):
            return None

    def _post(
        self,
        url: str,
        token: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> tuple[Any, str | None]:
        """POST one JSON-RPC message and return ``(parsed_json, session_id)``.

        Handles both reply encodings:
          - ``application/json`` → ``json.loads(response.text)``
          - ``text/event-stream`` → the last ``data:`` line's JSON

        ``parsed_json`` is ``None`` when the body cannot be parsed. The second
        element is the ``mcp-session-id`` returned by *this* response if any,
        otherwise the ``session_id`` passed in (so callers can chain it).
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": _ACCEPT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if session_id:
            headers[_SESSION_HEADER] = session_id

        with httpx.Client(timeout=_TIMEOUT) as client:
            response = client.post(url, json=payload, headers=headers)

        new_session = response.headers.get(_SESSION_HEADER) or session_id

        content_type = response.headers.get("content-type", "").lower()
        body = response.text
        if not body.strip():
            # Notifications and some accepted requests reply with an empty body.
            return None, new_session
        if "text/event-stream" in content_type:
            parsed = self._parse_sse(body)
        else:
            try:
                parsed = json.loads(body)
            except (ValueError, TypeError):
                parsed = None
        return parsed, new_session

    def _handshake(
        self, url: str, token: str
    ) -> tuple[str | None, bool, str | None]:
        """Open an MCP session: ``initialize`` then ``notifications/initialized``.

        Returns ``(session_id, ok, error)``. ``session_id`` may be ``None``
        even on success (the server is allowed to run sessionless). ``ok`` is
        ``False`` with a populated ``error`` if the URL is missing, the network
        fails, or the ``initialize`` response carries a JSON-RPC error.
        """
        if not url:
            return None, False, "server has no url"

        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "maahi", "version": "2.0"},
            },
        }
        try:
            parsed, session_id = self._post(url, token, init_payload)
        except httpx.HTTPError as e:
            return None, False, f"initialize failed: {e}"

        if isinstance(parsed, dict) and parsed.get("error"):
            err = parsed["error"]
            message = (
                err.get("message") if isinstance(err, dict) else str(err)
            )
            return session_id, False, f"initialize error: {message}"

        # Tell the server we're ready. This is a notification (no id), so a
        # missing/empty response is expected and fine.
        initialized_payload = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }
        try:
            self._post(url, token, initialized_payload, session_id=session_id)
        except httpx.HTTPError as e:
            return session_id, False, f"initialized failed: {e}"

        return session_id, True, None

    def _rpc(
        self,
        url: str,
        token: str,
        session_id: str | None,
        method: str,
        params: dict[str, Any] | None = None,
        request_id: int = 2,
    ) -> tuple[Any, str | None]:
        """Send a JSON-RPC *request* (with id) on an established session.

        Returns ``(parsed_json, session_id)``. Raises ``httpx.HTTPError`` on a
        transport failure — callers wrap this.
        """
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        return self._post(url, token, payload, session_id=session_id)

    @staticmethod
    def _rpc_error(parsed: Any) -> str | None:
        """Return a human error string if a JSON-RPC reply is an error, else None."""
        if isinstance(parsed, dict) and parsed.get("error"):
            err = parsed["error"]
            if isinstance(err, dict):
                return str(err.get("message") or err)
            return str(err)
        return None

    @staticmethod
    def _rpc_result(parsed: Any) -> Any:
        """Extract the ``result`` member of a JSON-RPC reply (or ``None``)."""
        if isinstance(parsed, dict):
            return parsed.get("result")
        return None

    # ---- capabilities ----

    def capabilities(self) -> tuple[Capability, ...]:
        return (
            Capability(
                "list_servers",
                "List the configured remote MCP server names. No network.",
                {},
                "read",
            ),
            Capability(
                "list_tools",
                "List the tools a configured MCP server exposes.",
                {"server": "str: server name"},
                "read",
            ),
            Capability(
                "call_tool",
                (
                    "Invoke a tool on a remote MCP server. Conservatively "
                    "tagged write since the remote tool's risk is unknown."
                ),
                {
                    "server": "str: server name",
                    "tool": "str: tool name",
                    "arguments": "dict: tool args (optional)",
                },
                "write",
            ),
            Capability(
                "ping",
                "Handshake-only reachability/auth check for an MCP server.",
                {"server": "str: server name"},
                "read",
            ),
        )

    # ---- operations ----

    def op_list_servers(self) -> ConnectorResult:
        """Return the configured server names without touching the network."""
        names = [str(s.get("name", "")).strip() for s in self._servers()]
        names = [n for n in names if n]
        return ConnectorResult.success(
            f"{len(names)} MCP servers configured"
            + (f": {', '.join(names)}" if names else ""),
            data=names,
        )

    def op_list_tools(self, server: str = "") -> ConnectorResult:
        """Handshake with a server, then ``tools/list`` and return the tools."""
        name = str(server or "").strip()
        if not name:
            return ConnectorResult.fail("MCP list_tools: server required")
        conf = self._find_server(name)
        if conf is None:
            return ConnectorResult.fail(f"MCP list_tools: unknown server {name!r}")

        url = self._server_url(conf)
        token = self._server_token(conf)
        session_id, ok, error = self._handshake(url, token)
        if not ok:
            return ConnectorResult.fail(f"MCP {name} handshake failed: {error}")

        try:
            parsed, _ = self._rpc(url, token, session_id, "tools/list", {})
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"MCP {name} tools/list failed: {e}")

        rpc_error = self._rpc_error(parsed)
        if rpc_error:
            return ConnectorResult.fail(f"MCP {name} tools/list error: {rpc_error}")

        result = self._rpc_result(parsed)
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            tools = []
        return ConnectorResult.success(
            f"{len(tools)} tools on {name}",
            data=tools,
            server=name,
        )

    def op_call_tool(
        self,
        server: str = "",
        tool: str = "",
        arguments: Any = None,
    ) -> ConnectorResult:
        """Handshake then ``tools/call`` a named tool with ``arguments``.

        ``arguments`` may arrive as a dict or as a JSON string (which is
        decoded). Anything that does not decode to a dict falls back to ``{}``.
        """
        name = str(server or "").strip()
        if not name:
            return ConnectorResult.fail("MCP call_tool: server required")
        tool_name = str(tool or "").strip()
        if not tool_name:
            return ConnectorResult.fail("MCP call_tool: tool required")
        conf = self._find_server(name)
        if conf is None:
            return ConnectorResult.fail(f"MCP call_tool: unknown server {name!r}")

        args = self._coerce_arguments(arguments)

        url = self._server_url(conf)
        token = self._server_token(conf)
        session_id, ok, error = self._handshake(url, token)
        if not ok:
            return ConnectorResult.fail(f"MCP {name} handshake failed: {error}")

        try:
            parsed, _ = self._rpc(
                url,
                token,
                session_id,
                "tools/call",
                {"name": tool_name, "arguments": args},
            )
        except httpx.HTTPError as e:
            return ConnectorResult.fail(f"MCP {name} tools/call failed: {e}")

        rpc_error = self._rpc_error(parsed)
        if rpc_error:
            return ConnectorResult.fail(
                f"MCP {name} {tool_name} error: {rpc_error}"
            )

        result = self._rpc_result(parsed)
        return ConnectorResult.success(
            f"Called {tool_name} on {name}",
            data=result,
            server=name,
            tool=tool_name,
        )

    def op_ping(self, server: str = "") -> ConnectorResult:
        """Handshake-only check; report reachability for a server."""
        name = str(server or "").strip()
        if not name:
            return ConnectorResult.fail("MCP ping: server required")
        conf = self._find_server(name)
        if conf is None:
            return ConnectorResult.fail(f"MCP ping: unknown server {name!r}")

        url = self._server_url(conf)
        token = self._server_token(conf)
        session_id, ok, error = self._handshake(url, token)
        if not ok:
            return ConnectorResult.fail(f"MCP {name} unreachable: {error}")
        return ConnectorResult.success(
            f"{name} reachable (session "
            + (session_id if session_id else "none")
            + ")",
            data={"server": name, "session": bool(session_id)},
            server=name,
        )

    # ---- internal ----

    @staticmethod
    def _coerce_arguments(arguments: Any) -> dict[str, Any]:
        """Normalize the ``arguments`` param to a dict.

        Accepts a dict as-is, decodes a JSON string, and falls back to an empty
        dict for ``None`` or anything that does not resolve to a dict.
        """
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str):
            text = arguments.strip()
            if not text:
                return {}
            try:
                decoded = json.loads(text)
            except (ValueError, TypeError):
                return {}
            return decoded if isinstance(decoded, dict) else {}
        return {}

    # ---- brief hooks ----

    def health(self) -> ConnectorResult:
        """Reachability check: handshake the first configured server.

        Reports which servers are reachable. Only the first server is actually
        probed to keep this cheap; the rest are listed as "configured".
        """
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        servers = self._servers()
        if not servers:
            return ConnectorResult.fail(
                f"{self.label}: no usable servers in MAAHI_MCP_SERVERS",
                not_configured=True,
            )

        first = servers[0]
        first_name = str(first.get("name", "")).strip() or "?"
        try:
            _, ok, error = self._handshake(
                self._server_url(first), self._server_token(first)
            )
        except Exception as e:  # noqa: BLE001 — health must never raise
            return ConnectorResult.fail(f"MCP {first_name} handshake crashed: {e}")

        other_names = [
            str(s.get("name", "")).strip() for s in servers[1:]
        ]
        other_names = [n for n in other_names if n]
        if ok:
            summary = f"MCP: {first_name} reachable"
            if other_names:
                summary += f" ({len(other_names)} more configured: {', '.join(other_names)})"
            return ConnectorResult.success(
                summary,
                data={
                    "reachable": [first_name],
                    "configured": [first_name, *other_names],
                },
            )
        return ConnectorResult.fail(
            f"MCP {first_name} unreachable: {error}",
            configured=[first_name, *other_names],
        )

    def pulse(self) -> ConnectorResult:
        """Cheap headline: how many servers are configured, by name. No network."""
        if not self.configured():
            return ConnectorResult.fail(
                f"{self.label}: not configured", not_configured=True
            )
        names = [str(s.get("name", "")).strip() for s in self._servers()]
        names = [n for n in names if n]
        summary = (
            f"{len(names)} MCP servers configured: {', '.join(names)}"
            if names
            else "0 MCP servers configured"
        )
        return ConnectorResult.success(summary, data={"servers": len(names)})

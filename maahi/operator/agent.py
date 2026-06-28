"""The Operator agent — Maahi's business brain.

A Claude-native, tool-using agentic loop over every connector capability,
governed by the autonomy policy. This is the engine behind the command-center
chat and the autonomous routines.

Flow per turn:
  build tools (connectors + built-ins) → Claude → if it calls tools, route each
  through the policy (execute now, or park for your approval) → feed results
  back → repeat until Claude returns a final answer.

Streaming: ``chat_stream`` yields structured events (text deltas, tool starts,
tool results, confirmations) so the cockpit can render a live "thinking" trace.
``chat`` is the buffered convenience wrapper.

Anthropic tool names must match ``^[a-zA-Z0-9_-]{1,128}$`` — but our connector
tools are namespaced with a dot (``zoho_crm.list_deals``). We encode the dot as
``__`` on the way out and decode it on dispatch.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .config import OperatorConfig, get_operator_config
from .connectors.registry import ConnectorRegistry, get_registry
from .ledger import get_ledger
from .policy import Autonomy, describe

log = logging.getLogger(__name__)

_DOT = "__"  # encodes the namespacing dot for Anthropic tool names


def _enc(name: str) -> str:
    return name.replace(".", _DOT)


def _dec(name: str) -> str:
    return name.replace(_DOT, ".")


def _params_to_schema(params: dict[str, str]) -> dict[str, Any]:
    """Turn a connector's ``{"name": "type: desc"}`` into a JSON Schema."""
    type_map = {
        "str": "string", "string": "string",
        "int": "integer", "integer": "integer", "number": "number",
        "float": "number", "bool": "boolean", "boolean": "boolean",
        "dict": "object", "object": "object", "list": "array", "array": "array",
    }
    props: dict[str, Any] = {}
    for pname, spec in params.items():
        kind, _, desc = spec.partition(":")
        json_type = type_map.get(kind.strip().lower(), "string")
        props[pname] = {"type": json_type, "description": desc.strip() or pname}
    return {"type": "object", "properties": props}


# ---- built-in operator tools (beyond connectors) ----------------------------

_BUILTIN_TOOLS: list[dict[str, Any]] = [
    {
        "name": "daily_brief",
        "description": (
            "Generate the executive brief across all ventures right now: CRM "
            "pulse, ad performance, ship/infra status, comms. Use when Meet "
            "asks 'what's my day', 'brief me', 'status', or 'what's going on'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_capabilities",
        "description": (
            "List every business system Maahi can reach and whether it's "
            "configured. Use to answer 'what can you do' or to discover which "
            "connector to use."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "remember",
        "description": (
            "Persist a durable fact, decision, or preference about Meet or his "
            "ventures so future sessions recall it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"note": {"type": "string", "description": "the fact to store"}},
            "required": ["note"],
        },
    },
    {
        "name": "recall",
        "description": "Read back everything Maahi has remembered so far.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


@dataclass
class AgentEvent:
    """One structured event from a streaming turn."""

    type: str            # "text" | "tool_start" | "tool_end" | "confirm" | "done" | "error"
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, **self.data}


class OperatorAgent:
    def __init__(
        self,
        *,
        autonomy: Autonomy | str | None = None,
        registry: ConnectorRegistry | None = None,
        cfg: OperatorConfig | None = None,
    ) -> None:
        self.cfg = cfg or get_operator_config()
        self.registry = registry or get_registry()
        self.ledger = get_ledger()
        self.autonomy = Autonomy.parse(
            autonomy if autonomy is not None else self.cfg.autonomy
        )
        self._client = None  # lazy

    # ---- anthropic client ----

    def _anthropic(self):
        if self._client is None:
            import anthropic

            if not self.cfg.anthropic_api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set — the operator brain is offline."
                )
            self._client = anthropic.Anthropic(
                api_key=self.cfg.anthropic_api_key, max_retries=2
            )
        return self._client

    def available(self) -> bool:
        try:
            import anthropic  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return bool(self.cfg.anthropic_api_key)

    # ---- tool catalog ----

    def _tools(self) -> list[dict[str, Any]]:
        tools = list(_BUILTIN_TOOLS)
        for spec in self.registry.tools():
            tools.append({
                "name": _enc(spec.name),
                "description": f"[{describe(spec.risk)}] {spec.description}",
                "input_schema": _params_to_schema(spec.params),
            })
        return tools

    # ---- system prompt (the operator's soul) ----

    def _system_prompt(self) -> str:
        cfg = self.cfg
        configured = self.registry.configured()
        connected = ", ".join(c.label for c in configured.values()) or "none yet"
        ventures = ", ".join(cfg.ventures)
        return (
            f"You are Maahi — {cfg.owner_name}'s autonomous Chief of Staff and "
            "business operator. Not a chatbot. An operator who runs his empire "
            "with him.\n\n"
            f"ABOUT {cfg.owner_name.upper()}:\n{cfg.owner_bio}\n\n"
            f"VENTURES YOU HELP RUN: {ventures}.\n"
            f"LIVE SYSTEMS YOU CAN REACH RIGHT NOW: {connected}.\n\n"
            "OPERATING DOCTRINE — act then report:\n"
            "- You bias to action. When something is reversible (reading data, "
            "drafting, logging a note, creating an internal task), just do it, "
            "then tell him in one tight line what you did.\n"
            "- Outbound and costly moves (sending an email, publishing a page, "
            "changing ad spend, deleting anything) are gated: the system will "
            "PARK them for his approval and tell you it did. When that happens, "
            "tell him plainly what's waiting and why — don't pretend it's done.\n"
            "- Chain tools to finish a job. To brief him, pull from several "
            "systems and synthesize — don't dump raw data. Lead with the "
            "decision or the number that matters.\n\n"
            "VOICE:\n"
            "- Direct, warm, dry. Short sentences. No corporate filler, no "
            "'I'm happy to help', no emoji.\n"
            "- Brutally honest. If a venture metric is bad, say so with the "
            "number. If you don't know, say so and say how you'll find out.\n"
            "- Money and time spoken naturally. Lead with the headline.\n\n"
            f"Autonomy mode right now: {self.autonomy.value}. "
            "Use your tools liberally to ground every claim in real data."
        )

    # ---- the loop ----

    def chat_stream(
        self,
        message: str,
        history: list[dict] | None = None,
        *,
        autonomy: Autonomy | str | None = None,
    ) -> Iterator[AgentEvent]:
        """Stream a turn as structured events. ``history`` is prior turns in
        Anthropic format ([{role, content}])."""
        if autonomy is not None:
            self.autonomy = Autonomy.parse(autonomy)
        if not self.available():
            yield AgentEvent("error", {"message":
                "Operator brain offline — set ANTHROPIC_API_KEY."})
            return

        client = self._anthropic()
        tools = self._tools()
        messages: list[dict] = list(history or [])
        messages.append({"role": "user", "content": message})
        self.ledger.record("chat.user", summary=message[:200], actor="meet",
                            detail={"len": len(message)})

        final_text_parts: list[str] = []
        for step in range(self.cfg.max_agent_steps):
            assistant_blocks: list[dict] = []
            try:
                with client.messages.stream(
                    model=self.cfg.model,
                    max_tokens=self.cfg.max_tokens,
                    temperature=self.cfg.temperature,
                    system=self._system_prompt(),
                    tools=tools,
                    messages=messages,
                ) as stream:
                    for text in stream.text_stream:
                        if text:
                            final_text_parts.append(text)
                            yield AgentEvent("text", {"text": text})
                    final = stream.get_final_message()
            except Exception as e:  # noqa: BLE001
                log.exception("Operator turn failed")
                yield AgentEvent("error", {"message": f"Brain error: {e}"})
                return

            assistant_blocks = _blocks_to_dicts(final.content)
            messages.append({"role": "assistant", "content": assistant_blocks})

            tool_uses = [b for b in assistant_blocks if b.get("type") == "tool_use"]
            if final.stop_reason != "tool_use" or not tool_uses:
                yield AgentEvent("done", {"text": "".join(final_text_parts).strip()})
                return

            tool_results: list[dict] = []
            for tu in tool_uses:
                name = _dec(tu["name"])
                args = tu.get("input") or {}
                yield AgentEvent("tool_start", {"name": name, "input": args})
                outcome = self._dispatch(name, args)
                if outcome.get("status") == "needs_confirmation":
                    yield AgentEvent("confirm", {
                        "name": name,
                        "pending_id": outcome.get("pending_id"),
                        "summary": outcome.get("summary", ""),
                    })
                else:
                    yield AgentEvent("tool_end", {
                        "name": name,
                        "status": outcome.get("status"),
                        "summary": outcome.get("summary", ""),
                    })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": json.dumps(outcome, ensure_ascii=False)[:8000],
                })
            messages.append({"role": "user", "content": tool_results})

        yield AgentEvent("done", {
            "text": "".join(final_text_parts).strip()
            or "I worked through several steps but didn't land a final answer — "
               "ask me to narrow it down.",
        })

    def chat(
        self,
        message: str,
        history: list[dict] | None = None,
        *,
        autonomy: Autonomy | str | None = None,
    ) -> dict[str, Any]:
        """Buffered turn. Returns {text, events, tool_calls, confirmations}."""
        text_parts: list[str] = []
        events: list[dict] = []
        confirmations: list[dict] = []
        for ev in self.chat_stream(message, history, autonomy=autonomy):
            events.append(ev.to_dict())
            if ev.type == "text":
                text_parts.append(ev.data.get("text", ""))
            elif ev.type == "done":
                if ev.data.get("text"):
                    text_parts = [ev.data["text"]]
            elif ev.type == "confirm":
                confirmations.append(ev.data)
            elif ev.type == "error":
                text_parts.append(ev.data.get("message", "error"))
        return {
            "text": "".join(text_parts).strip(),
            "events": events,
            "confirmations": confirmations,
        }

    # ---- tool dispatch ----

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Route a tool call: built-ins handled here, rest to the registry."""
        if name == "daily_brief":
            from .brief import build_brief

            brief = build_brief(self.registry, self.cfg)
            return {"status": "done", "summary": brief.headline, "data": brief.to_dict()}
        if name == "list_capabilities":
            return {"status": "done", "summary": "capability map",
                    "data": self._capability_map()}
        if name == "remember":
            note = str(args.get("note", "")).strip()
            if not note:
                return {"status": "failed", "summary": "empty note"}
            self._memory_append(note)
            return {"status": "done", "summary": f"Remembered: {note[:80]}"}
        if name == "recall":
            return {"status": "done", "summary": "memory",
                    "data": {"memory": self._memory_read()}}

        disp = self.registry.dispatch(
            name, args, autonomy=self.autonomy, origin="chat"
        )
        return disp.to_dict()

    def _capability_map(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, conn in self.registry.all().items():
            out[key] = {
                "label": conn.label,
                "configured": conn.configured(),
                "missing_env": list(conn.missing_env()),
                "capabilities": [c.name for c in conn.capabilities()],
            }
        return out

    # ---- lightweight memory ----

    def _memory_path(self):
        return self.cfg.state_dir / "memory.md"

    def _memory_append(self, note: str) -> None:
        import time

        path = self._memory_path()
        stamp = time.strftime("%Y-%m-%d %H:%M")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"- [{stamp}] {note}\n")

    def _memory_read(self) -> str:
        path = self._memory_path()
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")[-6000:]


def _blocks_to_dicts(blocks: list) -> list[dict]:
    """Convert SDK content blocks to plain dicts we can re-send."""
    out: list[dict] = []
    for b in blocks or []:
        btype = getattr(b, "type", None)
        if btype == "text":
            out.append({"type": "text", "text": getattr(b, "text", "")})
        elif btype == "tool_use":
            out.append({
                "type": "tool_use",
                "id": getattr(b, "id", ""),
                "name": getattr(b, "name", ""),
                "input": getattr(b, "input", {}) or {},
            })
    return out

"""Maahi's brain — Ollama client + tool-calling loop.

Flow:
  user_text → system_prompt + history → Ollama → response
    if response is a tool JSON → execute → feed result back → loop
    else → speak it to Meet

We use the OpenAI-compatible /v1/chat/completions endpoint Ollama exposes,
which gives us a stable interface and easy streaming.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

from .config import get_config
from .event_bus import emit_tool_end, emit_tool_start, emit_transcript
from .memory import recall_facts, recall_preferences
from .personality import build_system_prompt
from .tools.registry import call_tool, render_tool_catalog

log = logging.getLogger(__name__)


# ============================================================
#  HYBRID ROUTER
# ============================================================
# Decide per-utterance whether the LOCAL fast model (Ollama) or the
# CLOUD powerful model (OpenAI) should handle it. The local model owns
# tool calls + snappy replies. OpenAI owns reasoning / knowledge / "act
# powerful" requests. Heuristic only — cheap, no extra LLM call.

# Words that strongly hint a tool call is needed → keep on LOCAL.
_TOOL_HINTS: frozenset[str] = frozenset({
    "time", "date", "today", "tomorrow", "yesterday", "now",
    "volume", "mute", "louder", "quieter",
    "open", "launch", "quit", "close", "switch",
    "screen", "screenshot", "see", "look",
    "calendar", "schedule", "meeting", "event", "agenda",
    "reminder", "remind", "todo", "task",
    "note", "notes", "obsidian", "vault",
    "battery", "wifi", "network", "ip",
    "app", "apps", "window", "front",
    "click", "type", "mouse", "cursor",
    "send", "message", "imessage", "text",
    "memory", "remember", "preference", "forget",
    "play", "pause", "next", "previous",
    "weather",
})

# Words that strongly hint reasoning / knowledge → route to OPENAI.
_REASONING_HINTS: frozenset[str] = frozenset({
    "explain", "why", "how", "compare", "analyze", "analyse",
    "summarize", "summarise", "translate", "rewrite", "draft",
    "code", "function", "algorithm", "debug", "refactor",
    "history", "science", "physics", "chemistry", "biology",
    "philosophy", "economics", "politics", "law", "medicine",
    "joke", "story", "poem", "essay", "letter",
    "definition", "define", "meaning", "concept", "theory",
    "difference", "between", "versus", "vs",
    "recommend", "suggest", "advice", "should",
})


def _classify_route(user_text: str, configured: str) -> str:
    """Return 'local' or 'openai' from the configured router + utterance.

    configured: "auto" | "local" | "openai" (from brain.router config)
    """
    if configured in ("local", "openai"):
        return configured
    text = (user_text or "").lower().strip()
    if not text:
        return "local"
    words = set(re.findall(r"[a-z']+", text))
    if words & _TOOL_HINTS:
        return "local"
    if words & _REASONING_HINTS:
        return "openai"
    if len(words) <= 3:
        return "local"
    if len(words) >= 8:
        return "openai"
    return "local"


# ============================================================
#  MESSAGE TYPES (frozen — immutable)
# ============================================================


@dataclass(frozen=True)
class Message:
    role: str        # "system" | "user" | "assistant" | "tool"
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


# ============================================================
#  TOOL CALL PARSER
# ============================================================
# Maahi prefers a compact call syntax:
#   @call tool_name(arg="value", count=3)           — preferred
# and still accepts JSON as a fallback:
#   {"tool": "...", "args": {...}}                  — tolerated
#   ```json {"tool": ...} ```                       — tolerated
#   any line that begins with {"tool":              — tolerated
#
# We do brace-counted extraction rather than regex because nested
# objects like "args": {} blow up naive non-greedy patterns.
# ============================================================


_AT_CALL_HEAD = re.compile(r"@call\s+([A-Za-z_]\w*)\s*\(")


def _coerce_value(raw: str) -> object:
    """Turn a raw @call argument token into a typed Python value."""
    raw = raw.strip()
    if not raw:
        return ""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        inner = raw[1:-1]
        return (
            inner.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")
        )
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("none", "null"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw  # bare word — pass through as a string


def _split_call_args(args_str: str) -> list[str]:
    """Split an arg string on top-level commas, respecting quotes/brackets."""
    parts: list[str] = []
    buf: list[str] = []
    in_str = False
    quote = ""
    escape = False
    depth = 0
    for ch in args_str:
        if in_str:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ("'", '"'):
            in_str = True
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p for p in parts if p.strip()]


def _parse_at_call(text: str) -> tuple[str, dict] | None:
    """Parse `@call name(arg=val, ...)`. Returns (name, args) or None."""
    head = _AT_CALL_HEAD.search(text)
    if head is None:
        return None
    name = head.group(1)
    open_idx = head.end() - 1  # index of the '('
    # Scan to the balanced ')', respecting string literals.
    depth = 0
    in_str = False
    quote = ""
    escape = False
    end_idx = -1
    for i in range(open_idx, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ("'", '"'):
            in_str = True
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    if end_idx == -1:
        return None
    args: dict[str, object] = {}
    for part in _split_call_args(text[open_idx + 1:end_idx]):
        key, sep, val = part.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key:
            args[key] = _coerce_value(val)
    return name, args


def _extract_first_json_object(text: str) -> str | None:
    """Return the first balanced top-level JSON object in text, or None.

    Walks character by character, tracks brace depth, and respects string
    literals (including escaped quotes) so braces inside strings don't count.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_tool_call(text: str) -> tuple[str, dict] | None:
    """Return (tool_name, args) if text is a tool call, else None.

    Tries the preferred `@call name(...)` syntax first, then falls back
    to JSON-object extraction.
    """
    at_call = _parse_at_call(text)
    if at_call is not None:
        return at_call

    # JSON fallback. Strip code fences first.
    cleaned = re.sub(r"```(?:json)?\s*", "", text)
    cleaned = cleaned.replace("```", "")

    # Walk the string and try each candidate object until one parses + has "tool"
    cursor = 0
    while cursor < len(cleaned):
        candidate = _extract_first_json_object(cleaned[cursor:])
        if candidate is None:
            return None
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and "tool" in obj:
            args = obj.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            return str(obj["tool"]), args
        # Advance past this candidate and keep looking
        next_brace = cleaned.find("{", cursor + cleaned[cursor:].find("{") + 1)
        if next_brace == -1:
            return None
        cursor = next_brace
    return None


# ============================================================
#  OLLAMA CLIENT
# ============================================================


class Brain:
    """Stateful conversation with Ollama. Owns history + tool loop."""

    # Same call within _CACHE_TTL_S returns the cached tool result without
    # re-executing — important for noisy reads like `now` or `get_volume`
    # that the LLM may invoke repeatedly in a single conversation turn.
    _CACHE_TTL_S = 30.0
    _CACHE_MAX = 64

    def __init__(self) -> None:
        self.cfg = get_config()
        self.history: list[Message] = []
        self._http = httpx.Client(
            base_url=self.cfg.brain.host,
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        self._system_message = self._build_system_message()
        self._tool_cache: dict[tuple, tuple[dict, float]] = {}
        # OpenAI client is lazy + optional. If OPENAI_API_KEY is unset,
        # the "openai" route silently falls back to local.
        self._openai_key: str = os.environ.get("OPENAI_API_KEY", "").strip()
        self._openai_http: httpx.Client | None = None
        if self._openai_key:
            self._openai_http = httpx.Client(
                base_url=self.cfg.brain.openai_host,
                timeout=httpx.Timeout(60.0, connect=5.0),
                headers={"Authorization": f"Bearer {self._openai_key}"},
            )
        elif self.cfg.brain.router == "openai":
            log.warning(
                "brain.router=openai but OPENAI_API_KEY is not set; "
                "falling back to local Ollama."
            )

        # Claude (Anthropic) — the frontier "powerful" brain. When
        # brain.powerful == "claude" and the SDK + ANTHROPIC_API_KEY are
        # present, the reasoning route runs on Claude instead of OpenAI.
        # Lazy + optional: any failure degrades to OpenAI, then local.
        self._claude = None
        self._powerful = getattr(self.cfg.brain, "powerful", "openai")
        if self._powerful == "claude":
            try:
                from .brain_claude import ClaudeChat, claude_available

                if claude_available():
                    self._claude = ClaudeChat(
                        model=self.cfg.brain.claude_model,
                        max_tokens=self.cfg.brain.max_tokens,
                        temperature=self.cfg.brain.temperature,
                    )
                    log.info("Claude brain online: %s", self.cfg.brain.claude_model)
                else:
                    log.warning(
                        "brain.powerful=claude but Claude is unavailable "
                        "(install `anthropic` + set ANTHROPIC_API_KEY); "
                        "powerful route will use OpenAI/local."
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("Claude brain init failed: %s", e)

    def prewarm(self) -> bool:
        """Load the model AND prime its KV cache with the real system prompt.

        We send the actual system message (not a throwaway ".") so Ollama
        caches that long, stable prefix. The first real command then only
        has to process the user delta — meaningfully faster first token.

        Best-effort: failures are logged and swallowed. Never blocks boot.
        """
        try:
            self._http.post(
                "/v1/chat/completions",
                json={
                    "model": self.cfg.brain.model,
                    "messages": [
                        self._system_message.to_dict(),
                        {"role": "user", "content": "hi"},
                    ],
                    "stream": False,
                    "keep_alive": self.cfg.brain.keep_alive,
                    "options": {"num_predict": 1},
                },
                timeout=httpx.Timeout(180.0, connect=10.0),
            ).raise_for_status()
            log.info("Brain prewarm complete: %s loaded", self.cfg.brain.model)
            return True
        except httpx.HTTPError as e:
            log.warning("Brain prewarm failed: %s", e)
            return False

    @staticmethod
    def _cache_key(name: str, args: dict) -> tuple:
        return (name, tuple(sorted((k, repr(v)) for k, v in (args or {}).items())))

    def _cached_or_call(self, name: str, args: dict) -> dict:
        import time as _t
        key = self._cache_key(name, args)
        hit = self._tool_cache.get(key)
        now = _t.time()
        if hit is not None and (now - hit[1]) < self._CACHE_TTL_S:
            log.debug("Tool cache hit: %s", name)
            return dict(hit[0])
        result = call_tool(name, args)
        if (
            isinstance(result, dict)
            and result.get("ok", True)
            and _is_cacheable(name)
        ):
            if len(self._tool_cache) >= self._CACHE_MAX:
                oldest = min(self._tool_cache.items(), key=lambda kv: kv[1][1])[0]
                self._tool_cache.pop(oldest, None)
            self._tool_cache[key] = (dict(result), now)
        return result

    # ----- public API -----

    def _pick_route(self, user_text: str) -> str:
        """Resolve the effective route for this utterance.

        Honors brain.router config and silently falls back to local if
        OpenAI was requested but no API key is configured.
        """
        route = _classify_route(user_text, self.cfg.brain.router)
        if route == "openai" and self._openai_http is None:
            log.debug("OpenAI route requested but no API key; using local")
            return "local"
        return route

    def respond(self, user_text: str) -> str:
        """Take a user utterance, run the tool loop, return final spoken text."""
        self.history.append(Message("user", user_text))
        route = self._pick_route(user_text)
        log.info("Route: %s (router=%s)", route, self.cfg.brain.router)

        for iteration in range(self.cfg.brain.max_iterations):
            reply = self._chat_once(route)
            tool_call = _parse_tool_call(reply)
            if tool_call is None:
                # Plain text reply → final answer. Strip any leftover
                # @call/JSON syntax so the TTS never reads it aloud.
                clean = _sanitize_speech(reply)
                self.history.append(Message("assistant", clean))
                emit_transcript("maahi", clean)
                return clean
            name, args = tool_call
            log.info("Tool call: %s args=%s", name, args)
            emit_tool_start(name, args)
            result = self._cached_or_call(name, args)
            emit_tool_end(name, result)
            # Push both the call and the result so the model sees what happened.
            self.history.append(Message("assistant", reply))
            self.history.append(Message(
                "tool",
                f"[tool:{name}] {json.dumps(result, ensure_ascii=False)}",
            ))
        # Loop bailed — return a graceful fallback.
        log.warning("Tool loop hit max_iterations (%d)", self.cfg.brain.max_iterations)
        fallback = "I'm caught in a loop on this one. Try rephrasing."
        self.history.append(Message("assistant", fallback))
        emit_transcript("maahi", fallback)
        return fallback

    def reset(self) -> None:
        """Clear history but keep the system prompt."""
        self.history = []
        self._system_message = self._build_system_message()

    # ----- internals -----

    def _build_system_message(self) -> Message:
        catalog = render_tool_catalog()
        prompt = build_system_prompt(catalog)
        facts = recall_facts()
        prefs = recall_preferences()
        if facts:
            prompt += f"\n\nLONG-TERM FACTS YOU'VE LEARNED:\n{facts}"
        if prefs:
            prompt += f"\n\nMEET'S PREFERENCES:\n{prefs}"
        # Ambient screen observations (only present when watcher is enabled).
        try:
            from .vision_watcher import recent_observations
            obs = recent_observations()
        except Exception:  # noqa: BLE001
            obs = ()
        if obs:
            lines = "\n".join(
                f"- [{o.ts}] {o.app}: {o.text}" for o in obs
            )
            prompt += (
                "\n\nRECENT SCREEN OBSERVATIONS (ambient context — Meet did "
                "not ask about these, but use them if relevant):\n" + lines
            )
        return Message("system", prompt)

    def _chat_once(self, route: str = "local") -> str:
        """One round-trip to the chosen brain. Returns assistant text.

        The "openai" route is the *powerful* route: it prefers Claude when
        configured, then OpenAI, then degrades to local Ollama.
        """
        messages = [self._system_message.to_dict()] + [m.to_dict() for m in self.history]
        if route == "openai":
            if self._claude is not None:
                try:
                    return self._claude.complete(messages)
                except Exception as e:  # noqa: BLE001
                    log.error("Claude failed: %s — falling back", e)
            if self._openai_http is not None:
                return self._chat_openai(messages)
        return self._chat_ollama(messages)

    def _chat_ollama(self, messages: list[dict]) -> str:
        payload = {
            "model": self.cfg.brain.model,
            "messages": messages,
            "temperature": self.cfg.brain.temperature,
            "stream": False,
            "keep_alive": self.cfg.brain.keep_alive,
            "options": {"num_predict": self.cfg.brain.max_tokens},
        }
        try:
            r = self._http.post("/v1/chat/completions", json=payload)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.error("Ollama call failed: %s", e)
            return "I lost connection to the brain. Check Ollama."
        return _extract_message(r.json())

    def _chat_openai(self, messages: list[dict]) -> str:
        assert self._openai_http is not None  # _pick_route gates this
        payload = {
            "model": self.cfg.brain.openai_model,
            "messages": messages,
            "temperature": self.cfg.brain.temperature,
            "max_tokens": self.cfg.brain.max_tokens,
            "stream": False,
        }
        try:
            r = self._openai_http.post("/chat/completions", json=payload)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.error("OpenAI call failed: %s — falling back to local", e)
            return self._chat_ollama(messages)
        return _extract_message(r.json())

    # ----- streaming primitives -----
    # Both endpoints are OpenAI-compatible SSE: each line is
    # ``data: {"choices":[{"delta":{"content":"..."}}]}`` ending with
    # ``data: [DONE]``. We yield the content deltas as they arrive so the
    # speaker can start talking at the first sentence instead of waiting
    # for the whole completion.

    def _chat_stream(self, route: str) -> Iterator[str]:
        """Yield assistant-text deltas from the chosen brain."""
        messages = [self._system_message.to_dict()] + [
            m.to_dict() for m in self.history
        ]
        if route == "openai":
            if self._claude is not None:
                try:
                    yield from self._claude.stream(messages)
                    return
                except Exception as e:  # noqa: BLE001
                    log.error("Claude stream failed: %s — falling back", e)
            if self._openai_http is not None:
                yield from self._chat_openai_stream(messages)
                return
        yield from self._chat_ollama_stream(messages)

    def _chat_ollama_stream(self, messages: list[dict]) -> Iterator[str]:
        payload = {
            "model": self.cfg.brain.model,
            "messages": messages,
            "temperature": self.cfg.brain.temperature,
            "stream": True,
            "keep_alive": self.cfg.brain.keep_alive,
            "options": {"num_predict": self.cfg.brain.max_tokens},
        }
        try:
            with self._http.stream(
                "POST", "/v1/chat/completions", json=payload
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    delta = _parse_sse_delta(line)
                    if delta:
                        yield delta
        except httpx.HTTPError as e:
            log.error("Ollama stream failed: %s", e)
            yield "I lost connection to the brain. Check Ollama."

    def _chat_openai_stream(self, messages: list[dict]) -> Iterator[str]:
        assert self._openai_http is not None  # _chat_stream gates this
        payload = {
            "model": self.cfg.brain.openai_model,
            "messages": messages,
            "temperature": self.cfg.brain.temperature,
            "max_tokens": self.cfg.brain.max_tokens,
            "stream": True,
        }
        try:
            with self._openai_http.stream(
                "POST", "/chat/completions", json=payload
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    delta = _parse_sse_delta(line)
                    if delta:
                        yield delta
        except httpx.HTTPError as e:
            log.error("OpenAI stream failed: %s — falling back to local", e)
            yield from self._chat_ollama_stream(messages)

    def stream_respond(self, user_text: str) -> Iterator[str]:
        """Streaming variant. Streams the spoken (non-tool) reply token-by-token.

        A tool-call turn must be buffered in full before we can dispatch it
        (we need the whole ``@call``/JSON), so those turns stay silent. But
        a prose turn — the actual answer — is streamed straight to the
        speaker as it arrives, so Maahi starts talking at the first sentence
        instead of after the whole completion finishes.
        """
        self.history.append(Message("user", user_text))
        route = self._pick_route(user_text)
        log.info("Route: %s (router=%s)", route, self.cfg.brain.router)

        for _ in range(self.cfg.brain.max_iterations):
            spoke = False  # did we stream this turn as prose?
            buf: list[str] = []
            for delta in self._chat_stream(route):
                buf.append(delta)
                if spoke:
                    yield delta
                    continue
                # Decide prose-vs-tool from the first non-whitespace chars.
                # A tool call always opens with '@', '{', or a code fence;
                # anything else is the spoken answer — flush + stream live.
                lead = "".join(buf).lstrip()
                if not lead:
                    continue
                if lead[0] in "@{" or lead.startswith("```"):
                    continue  # keep buffering silently; resolve after stream
                spoke = True
                yield "".join(buf)

            full = "".join(buf)
            if spoke:
                # Prose turn = final answer. Record the cleaned form.
                clean = _sanitize_speech(full)
                self.history.append(Message("assistant", clean))
                emit_transcript("maahi", clean)
                return

            tool_call = _parse_tool_call(full)
            if tool_call is None:
                # Looked like a tool call but wasn't — speak the buffered text.
                clean = _sanitize_speech(full)
                self.history.append(Message("assistant", clean))
                emit_transcript("maahi", clean)
                for chunk in _pseudo_tokens(clean):
                    yield chunk
                return

            name, args = tool_call
            log.info("Tool call: %s args=%s", name, args)
            emit_tool_start(name, args)
            result = self._cached_or_call(name, args)
            emit_tool_end(name, result)
            self.history.append(Message("assistant", full))
            self.history.append(Message(
                "tool",
                f"[tool:{name}] {json.dumps(result, ensure_ascii=False)}",
            ))

        # Tool loop exhausted without a spoken reply — graceful fallback.
        log.warning("Stream tool loop hit max_iterations (%d)",
                    self.cfg.brain.max_iterations)
        fallback = "I'm caught in a loop on this one. Try rephrasing."
        self.history.append(Message("assistant", fallback))
        emit_transcript("maahi", fallback)
        for chunk in _pseudo_tokens(fallback):
            yield chunk


def _pseudo_tokens(text: str, size: int = 8) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _extract_message(data: dict) -> str:
    """Pull assistant text from an OpenAI-compatible chat-completion response."""
    choices = data.get("choices") or []
    if not choices:
        return "Empty response from the model."
    return (choices[0].get("message") or {}).get("content", "").strip()


def _parse_sse_delta(line: str) -> str:
    """Extract the content delta from one OpenAI-compatible SSE line.

    Returns "" for the ``[DONE]`` sentinel, blank lines, keep-alive pings,
    and any line whose JSON lacks a ``choices[0].delta.content``.
    """
    if not line:
        return ""
    if line.startswith("data:"):
        line = line[len("data:"):]
    line = line.strip()
    if not line or line == "[DONE]":
        return ""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return ""
    choices = obj.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("delta") or {}).get("content") or ""


# Matches a full `@call name(...)` block. Used to strip leftover tool-call
# syntax from a model reply that ALSO contains prose — so the TTS never
# reads "@ call open underscore app" out loud.
_AT_CALL_BLOCK = re.compile(
    r"@call\s+[A-Za-z_]\w*\s*\([^)]*\)\s*",
    re.DOTALL,
)


def _sanitize_speech(text: str) -> str:
    """Remove any leftover @call blocks + JSON tool-call objects from text."""
    cleaned = _AT_CALL_BLOCK.sub("", text)
    # Also strip a bare JSON tool envelope if it slipped through.
    # Allows one level of nested braces (e.g. "args": {"x": 1}).
    cleaned = re.sub(
        r'\{(?:[^{}]|\{[^{}]*\})*"tool"\s*:\s*"[^"]+"(?:[^{}]|\{[^{}]*\})*\}',
        "",
        cleaned,
        flags=re.DOTALL,
    )
    # Collapse the whitespace gaps left behind.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


# Tools that are safe to cache for a short TTL. Anything that writes,
# sends, or has time-sensitive side effects is excluded.
_CACHEABLE_TOOLS: frozenset[str] = frozenset({
    "now", "system_info", "get_volume", "frontmost_app", "front_window",
    "running_apps", "obsidian_list", "obsidian_read", "obsidian_search",
    "obsidian_semantic_search", "reminders_open", "calendar_today",
    "calendar_week", "calendar_upcoming", "notes_list", "notes_read",
    "web_search", "web_fetch",
})


def _is_cacheable(tool_name: str) -> bool:
    return tool_name in _CACHEABLE_TOOLS

"""Claude provider for Maahi's voice brain.

This is what makes the Mac voice OS "Claude-runnable". The existing ``Brain``
(``maahi/brain.py``) speaks a text tool-call protocol (``@call name(args)``)
and routes utterances between a local Ollama model and a cloud model. This
module adds Claude — Anthropic's frontier model — as the cloud "powerful"
brain, so the same voice loop now reasons with Claude while keeping the
snappy local model for quick tool calls.

It deliberately takes the SAME OpenAI-style message dicts the brain already
builds (``{"role": ..., "content": ...}``) and adapts them to Anthropic's
Messages API, so wiring it in is a two-line change in ``brain.py``.

Soft dependency: the ``anthropic`` SDK is imported lazily. If it isn't
installed, or ``ANTHROPIC_API_KEY`` is unset, ``claude_available()`` returns
False and the brain silently falls back to its other providers — exactly how
the OpenAI path degrades today.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator

log = logging.getLogger(__name__)

# Resolved once. The SDK import is cheap but we cache the verdict.
_SDK_OK: bool | None = None


def _sdk_ok() -> bool:
    global _SDK_OK
    if _SDK_OK is None:
        try:
            import anthropic  # noqa: F401

            _SDK_OK = True
        except Exception:  # noqa: BLE001
            _SDK_OK = False
    return _SDK_OK


def claude_available() -> bool:
    """True when we can actually talk to Claude (SDK present + key set)."""
    return _sdk_ok() and bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


# ---- message adaptation -----------------------------------------------------
# The voice brain builds OpenAI-style messages with roles:
#   system | user | assistant | tool
# Anthropic wants: a separate `system` string + an alternating user/assistant
# list (no "tool" role in our text protocol). We:
#   - hoist every system message into one system string,
#   - map "tool" → "user" (tool results are just context for the next turn),
#   - merge consecutive same-role messages so alternation holds,
#   - guarantee the list starts with a user turn.


def split_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Return (system_text, anthropic_messages) from OpenAI-style messages."""
    system_parts: list[str] = []
    convo: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        if role == "system":
            if content.strip():
                system_parts.append(content)
            continue
        mapped = "assistant" if role == "assistant" else "user"
        convo.append({"role": mapped, "content": content})

    # Merge consecutive same-role turns.
    merged: list[dict] = []
    for msg in convo:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] = (
                merged[-1]["content"].rstrip() + "\n\n" + msg["content"].lstrip()
            )
        else:
            merged.append(dict(msg))

    # Anthropic requires the first message to be from the user.
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "(context)"})
    if not merged:
        merged = [{"role": "user", "content": "hi"}]

    return "\n\n".join(system_parts), merged


class ClaudeChat:
    """Thin Anthropic Messages client matching the voice brain's needs."""

    def __init__(
        self,
        model: str = "claude-opus-4-8",
        max_tokens: int = 512,
        temperature: float = 0.4,
    ) -> None:
        import anthropic  # lazy — only when actually used

        self._client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"].strip(),
            # The voice loop is latency-sensitive; keep retries modest.
            max_retries=2,
        )
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def complete(self, messages: list[dict]) -> str:
        """One non-streaming round-trip. Returns assistant text."""
        system, convo = split_messages(messages)
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=system or None,
                messages=convo,
            )
        except Exception as e:  # noqa: BLE001
            log.error("Claude completion failed: %s", e)
            raise
        return _text_of(resp)

    def stream(self, messages: list[dict]) -> Iterator[str]:
        """Stream assistant text deltas as they arrive."""
        system, convo = split_messages(messages)
        try:
            with self._client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=system or None,
                messages=convo,
            ) as stream:
                for text in stream.text_stream:
                    if text:
                        yield text
        except Exception as e:  # noqa: BLE001
            log.error("Claude stream failed: %s", e)
            raise


def _text_of(resp: object) -> str:
    """Pull concatenated text from an Anthropic Messages response."""
    try:
        blocks = resp.content  # type: ignore[attr-defined]
    except AttributeError:
        return ""
    out: list[str] = []
    for block in blocks or []:
        text = getattr(block, "text", None)
        if text:
            out.append(text)
    return "".join(out).strip()

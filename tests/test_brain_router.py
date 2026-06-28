"""Tests for the hybrid local/openai brain router classifier."""
from __future__ import annotations

import pytest

from maahi.brain import _classify_route


@pytest.mark.unit
class TestForcedRouter:
    """When router is explicitly set, classifier always honors it."""

    def test_local_forced_overrides_reasoning_hints(self) -> None:
        assert _classify_route("explain quantum physics in detail", "local") == "local"

    def test_openai_forced_overrides_tool_hints(self) -> None:
        assert _classify_route("what time is it", "openai") == "openai"

    def test_local_forced_on_empty(self) -> None:
        assert _classify_route("", "local") == "local"


@pytest.mark.unit
class TestAutoRouter:
    """When router == 'auto', heuristics pick the route."""

    @pytest.mark.parametrize("text", [
        "what time is it",
        "open safari",
        "set volume to fifty",
        "show me my calendar today",
        "send a message to Sarah",
        "what's on my screen",
        "remind me to call mom",
        "open obsidian note",
    ])
    def test_tool_hints_route_local(self, text: str) -> None:
        assert _classify_route(text, "auto") == "local"

    @pytest.mark.parametrize("text", [
        "explain how neural networks learn",
        "why is the sky blue in the afternoon",
        "compare nuclear fission and fusion",
        "translate this sentence into french for me",
        "tell me a joke about engineers",
        "summarize the theory of relativity",
        "what is the difference between TCP and UDP networks",
    ])
    def test_reasoning_hints_route_openai(self, text: str) -> None:
        assert _classify_route(text, "auto") == "openai"

    @pytest.mark.parametrize("text", ["yes", "thanks", "ok cool", "got it"])
    def test_short_utterance_routes_local(self, text: str) -> None:
        assert _classify_route(text, "auto") == "local"

    def test_empty_text_routes_local(self) -> None:
        assert _classify_route("", "auto") == "local"
        assert _classify_route("   ", "auto") == "local"

    def test_long_unclassified_routes_openai(self) -> None:
        # 8+ words, no tool/reasoning keywords → default to powerful brain.
        text = "imagine you are a brilliant friend giving me thoughtful feedback"
        assert _classify_route(text, "auto") == "openai"

    def test_tool_hint_beats_reasoning_hint(self) -> None:
        # Mixed signals — tool intent must win so we don't break tool calls.
        assert _classify_route("explain my calendar for today", "auto") == "local"

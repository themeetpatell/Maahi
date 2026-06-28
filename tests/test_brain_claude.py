"""Claude voice-brain adapter — message adaptation (no network)."""
from __future__ import annotations

from maahi.brain_claude import split_messages


def test_system_is_hoisted():
    system, convo = split_messages([
        {"role": "system", "content": "You are Maahi."},
        {"role": "user", "content": "hi"},
    ])
    assert system == "You are Maahi."
    assert convo == [{"role": "user", "content": "hi"}]


def test_tool_role_maps_to_user():
    _, convo = split_messages([
        {"role": "user", "content": "search"},
        {"role": "assistant", "content": "@call x()"},
        {"role": "tool", "content": "[tool:x] {}"},
    ])
    # tool -> user, and it follows the assistant turn cleanly
    assert convo[-1]["role"] == "user"
    assert "[tool:x]" in convo[-1]["content"]


def test_consecutive_same_role_merged():
    _, convo = split_messages([
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
    ])
    assert len(convo) == 1
    assert "a" in convo[0]["content"] and "b" in convo[0]["content"]


def test_first_message_forced_to_user():
    _, convo = split_messages([
        {"role": "assistant", "content": "leading assistant"},
        {"role": "user", "content": "hi"},
    ])
    assert convo[0]["role"] == "user"


def test_empty_history_is_safe():
    system, convo = split_messages([])
    assert convo and convo[0]["role"] == "user"


def test_multiple_system_messages_joined():
    system, _ = split_messages([
        {"role": "system", "content": "one"},
        {"role": "system", "content": "two"},
        {"role": "user", "content": "hi"},
    ])
    assert "one" in system and "two" in system

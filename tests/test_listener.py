"""Tests for listener.py — wake matching + command-shape detection.

Focus: natural-speech utterances that previously fell through to "no wake"
should now be recognized as commands, and mid-sentence wake fallbacks
should not false-wake.
"""
from __future__ import annotations

import pytest

from maahi.listener import looks_like_command, matches_wake_phrase


WAKE_PHRASES: tuple[str, ...] = ("maahi", "hey maahi")


# ============================================================
#  looks_like_command — courtesy + first-person prefixes
# ============================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    "utterance",
    [
        # Bare imperatives — previously worked.
        "open chrome",
        "what time is it",
        "play music",
        # Courtesy prefixes — previously failed.
        "Can you open the Chrome?",
        "Could you open Chrome please",
        "Would you please open Chrome",
        "Please open Chrome",
        "Hey open Chrome",
        # First-person intent — previously failed.
        "I need to open the chat GPT on my laptop",
        "I want to open Chrome",
        "I'd like to send a message to Sara",
        "I wanna play some music",
        "Let's open Chrome",
        # Stacked prefixes.
        "Hey can you please open Chrome",
        # Longer-but-still-command sentence (up to 22 words).
        "tell me what are the new things in AI as of today briefly",
    ],
)
def test_looks_like_command_accepts_natural_speech(utterance: str) -> None:
    assert looks_like_command(utterance) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "utterance",
    [
        "",
        "yeah I just want to know",            # starts with non-command
        "there is no meeting where I can see that",
        "that's pretty interesting actually",
        # Pure courtesy with no verb after stripping → not a command.
        "can you",
        "could you please",
        "i want to",
        # Long monologue that doesn't start with a command verb.
        "yeah I was just thinking about that thing we talked about earlier today and it actually makes a lot of sense now",
    ],
)
def test_looks_like_command_rejects_non_commands(utterance: str) -> None:
    assert looks_like_command(utterance) is False


# ============================================================
#  matches_wake_phrase — first/last-token rule for phonetics
# ============================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    "utterance",
    [
        "maahi what time is it",
        "Hey Maahi, open Chrome",
        "Mahi, do that thing",
        # Trailing-address pattern.
        "What time is it, Mahi?",
        "Open Chrome, mommy",       # phonetic fallback at the end
    ],
)
def test_wake_matches_at_edges(utterance: str) -> None:
    assert matches_wake_phrase(utterance, WAKE_PHRASES) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "utterance",
    [
        # Mid-sentence phonetic fallback — should NOT wake.
        "okay fair enough mahi okay",
        "well marie said the same thing yesterday actually",
        # No wake at all.
        "what time is it",
        "open chrome",
        "",
    ],
)
def test_wake_does_not_fire_mid_sentence(utterance: str) -> None:
    assert matches_wake_phrase(utterance, WAKE_PHRASES) is False

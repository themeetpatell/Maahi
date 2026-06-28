"""Tests for the global hotkey combo parser."""
from __future__ import annotations

import pytest

from maahi.hotkey import _to_pynput


@pytest.mark.unit
@pytest.mark.parametrize("combo,expected", [
    ("cmd+option+m", "<cmd>+<alt>+m"),
    ("CMD+SHIFT+J", "<cmd>+<shift>+j"),
    ("option+a", "<alt>+a"),
    ("opt+s", "<alt>+s"),
    ("command+1", "<cmd>+1"),
])
def test_pynput_translation(combo: str, expected: str) -> None:
    assert _to_pynput(combo) == expected


@pytest.mark.unit
def test_multi_char_non_modifier_raises() -> None:
    # "space" is multi-char and not in _PYNPUT_MODS — should raise.
    with pytest.raises(ValueError):
        _to_pynput("control+space")


@pytest.mark.unit
def test_empty_combo_raises() -> None:
    with pytest.raises(ValueError):
        _to_pynput("")
    with pytest.raises(ValueError):
        _to_pynput("+")


@pytest.mark.unit
def test_unknown_token_raises() -> None:
    with pytest.raises(ValueError):
        _to_pynput("cmd+frobnicate")


@pytest.mark.unit
def test_whitespace_tolerated() -> None:
    assert _to_pynput("  cmd  +  shift  +  m  ") == "<cmd>+<shift>+m"

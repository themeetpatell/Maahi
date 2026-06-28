"""Tests for the fast-path intent router."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from maahi import fast_path


# ============================================================
#  NORMALIZATION
# ============================================================


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ("What time is it?", "what time is it"),
    ("  please   open   chrome  ", "open chrome"),
    ("Can you turn it down!", "turn it down"),
    ("Hey just lock the screen.", "lock the screen"),
    ("Now mute.", "mute"),
])
def test_normalize_strips_filler_and_punct(raw: str, expected: str) -> None:
    assert fast_path._normalize(raw) == expected


# ============================================================
#  PURE-RESPONSE INTENTS (no side effects)
# ============================================================


@pytest.mark.unit
def test_time_intent_returns_string() -> None:
    out = fast_path.try_fast_path("what time is it?")
    assert isinstance(out, str)
    assert "It's" in out


@pytest.mark.unit
def test_date_intent_returns_string() -> None:
    out = fast_path.try_fast_path("what's the date?")
    assert isinstance(out, str)
    assert out.startswith("It's")


@pytest.mark.unit
def test_day_intent_returns_string() -> None:
    out = fast_path.try_fast_path("what day is it?")
    assert isinstance(out, str)


@pytest.mark.unit
def test_greet_intent_returns_string() -> None:
    out = fast_path.try_fast_path("good morning")
    assert isinstance(out, str)
    assert len(out) > 0


# ============================================================
#  SIDE-EFFECT INTENTS (subprocess mocked)
# ============================================================


@pytest.mark.unit
def test_open_app_calls_open_command() -> None:
    fake = MagicMock(returncode=0, stdout="", stderr="")
    with patch("maahi.fast_path.subprocess.run", return_value=fake) as run:
        out = fast_path.try_fast_path("open slack")
    assert out == "Opening Slack."
    args, _kw = run.call_args
    assert args[0] == ["open", "-a", "Slack"]


@pytest.mark.unit
def test_open_app_known_alias_resolves() -> None:
    fake = MagicMock(returncode=0, stdout="", stderr="")
    with patch("maahi.fast_path.subprocess.run", return_value=fake):
        out = fast_path.try_fast_path("open chrome")
    assert out == "Opening Google Chrome."


@pytest.mark.unit
def test_open_app_returns_none_on_failure() -> None:
    fake = MagicMock(returncode=1, stdout="", stderr="not found")
    with patch("maahi.fast_path.subprocess.run", return_value=fake):
        out = fast_path.try_fast_path("open nonexistentapp")
    assert out is None


@pytest.mark.unit
def test_set_volume_clamps_and_calls_osascript() -> None:
    fake = MagicMock(returncode=0, stdout="", stderr="")
    with patch("maahi.fast_path.subprocess.run", return_value=fake) as run:
        out = fast_path.try_fast_path("set the volume to 200")
    assert out == "Volume 100."
    call_args = run.call_args[0][0]
    assert "set volume output volume 100" in " ".join(call_args)


@pytest.mark.unit
def test_mute_intent() -> None:
    fake = MagicMock(returncode=0, stdout="", stderr="")
    with patch("maahi.fast_path.subprocess.run", return_value=fake):
        out = fast_path.try_fast_path("mute")
    assert out == "Muted."


# ============================================================
#  INFO INTENTS (battery, wifi)
# ============================================================


@pytest.mark.unit
def test_battery_parses_pmset_output() -> None:
    sample = (
        "Now drawing from 'Battery Power'\n"
        " -InternalBattery-0 (id=1234)\t82%; discharging; 4:32 remaining\n"
    )
    fake = MagicMock(returncode=0, stdout=sample, stderr="")
    with patch("maahi.fast_path.subprocess.run", return_value=fake):
        out = fast_path.try_fast_path("what's my battery")
    assert out == "Battery is 82 percent."


@pytest.mark.unit
def test_battery_recognizes_charging() -> None:
    sample = " -InternalBattery-0\t99%; charging; 0:01 remaining\n"
    fake = MagicMock(returncode=0, stdout=sample, stderr="")
    with patch("maahi.fast_path.subprocess.run", return_value=fake):
        out = fast_path.try_fast_path("battery")
    assert out is not None
    assert "charging" in out
    assert "99" in out


@pytest.mark.unit
def test_wifi_parses_networksetup_output() -> None:
    fake = MagicMock(
        returncode=0,
        stdout="Current Wi-Fi Network: HomeNet5G\n",
        stderr="",
    )
    with patch("maahi.fast_path.subprocess.run", return_value=fake):
        out = fast_path.try_fast_path("what's my wifi")
    assert out == "You're on HomeNet5G."


@pytest.mark.unit
def test_wifi_recognizes_disconnect() -> None:
    fake = MagicMock(
        returncode=0,
        stdout="You are not associated with an AirPort network.\n",
        stderr="",
    )
    with patch("maahi.fast_path.subprocess.run", return_value=fake):
        out = fast_path.try_fast_path("wifi name")
    assert out == "WiFi is not connected."


# ============================================================
#  NEGATIVE CASES
# ============================================================


@pytest.mark.unit
def test_unknown_command_returns_none() -> None:
    assert fast_path.try_fast_path("explain quantum computing") is None


@pytest.mark.unit
def test_empty_command_returns_none() -> None:
    assert fast_path.try_fast_path("") is None
    assert fast_path.try_fast_path("   ") is None


@pytest.mark.unit
def test_handler_crash_falls_through() -> None:
    """If a matched handler raises, we return None so the LLM picks up."""
    def _boom(_m: object) -> str:
        raise RuntimeError("oops")
    fake_intent = fast_path._Intent(
        "boom",
        fast_path.re.compile(r"^boom$"),
        _boom,
    )
    with patch.object(fast_path, "_INTENTS", (fake_intent, *fast_path._INTENTS)):
        assert fast_path.try_fast_path("boom") is None

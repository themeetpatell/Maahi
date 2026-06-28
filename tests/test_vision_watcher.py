"""Tests for the rolling vision watcher buffer + dataclass."""
from __future__ import annotations

import pytest

from maahi import vision_watcher
from maahi.vision_watcher import Observation


@pytest.fixture(autouse=True)
def _clear_buffer():
    """Reset the singleton ring buffer before each test."""
    with vision_watcher._BUFFER_LOCK:
        vision_watcher._BUFFER.clear()
    yield
    with vision_watcher._BUFFER_LOCK:
        vision_watcher._BUFFER.clear()


@pytest.mark.unit
def test_observation_is_frozen() -> None:
    o = Observation(ts="2026-01-01T00:00:00", app="Slack", text="messages")
    with pytest.raises(Exception):
        o.text = "different"  # type: ignore[misc]


@pytest.mark.unit
def test_push_and_recent_round_trip() -> None:
    vision_watcher._resize_buffer(3)
    vision_watcher._push(Observation("ts1", "Safari", "page A"))
    vision_watcher._push(Observation("ts2", "Slack", "channel B"))
    out = vision_watcher.recent_observations()
    assert len(out) == 2
    assert out[0].text == "page A"
    assert out[1].text == "channel B"


@pytest.mark.unit
def test_buffer_respects_maxlen() -> None:
    vision_watcher._resize_buffer(2)
    for i in range(5):
        vision_watcher._push(Observation(f"ts{i}", "App", f"text{i}"))
    out = vision_watcher.recent_observations()
    assert len(out) == 2
    # Newest items kept, oldest dropped.
    assert out[0].text == "text3"
    assert out[1].text == "text4"


@pytest.mark.unit
def test_recent_observations_limit() -> None:
    vision_watcher._resize_buffer(5)
    for i in range(5):
        vision_watcher._push(Observation(f"ts{i}", "App", f"text{i}"))
    out = vision_watcher.recent_observations(limit=2)
    assert len(out) == 2
    # Tail (newest) returned when limit < buffer size.
    assert out[0].text == "text3"
    assert out[1].text == "text4"


@pytest.mark.unit
def test_resize_preserves_recent_items() -> None:
    vision_watcher._resize_buffer(5)
    for i in range(5):
        vision_watcher._push(Observation(f"ts{i}", "App", f"text{i}"))
    vision_watcher._resize_buffer(2)
    out = vision_watcher.recent_observations()
    assert len(out) == 2
    assert out[-1].text == "text4"

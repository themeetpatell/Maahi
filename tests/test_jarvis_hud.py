"""Unit tests for the Jarvis HUD subsystems.

Covers:
- Event bus pub/sub fan-out and ring buffer.
- Subscription drop-oldest backpressure.
- Control tool argument parsing and coordinate clamping (no real input).
"""
from __future__ import annotations

import asyncio
import queue

import pytest


# ============================================================
#  event_bus
# ============================================================


@pytest.fixture(autouse=True)
def fresh_bus(monkeypatch):
    """Give each test a clean EventBus instance."""
    from maahi import event_bus

    monkeypatch.setattr(event_bus, "_BUS", None)
    yield
    monkeypatch.setattr(event_bus, "_BUS", None)


@pytest.mark.unit
def test_publish_and_recent_roundtrip():
    from maahi.event_bus import bus

    b = bus()
    b.publish("state", {"state": "idle"})
    b.publish("transcript", {"speaker": "user", "text": "hi"})

    recent = b.recent()
    assert len(recent) == 2
    assert recent[0].type == "state"
    assert recent[1].payload["text"] == "hi"
    assert recent[1].seq > recent[0].seq


@pytest.mark.unit
def test_subscriber_receives_published_events():
    from maahi.event_bus import bus

    b = bus()
    sub = b.subscribe()
    b.publish("tool_start", {"name": "vision_screen", "args": {}})

    async def _drain():
        return await asyncio.wait_for(sub.get(), timeout=1.0)

    ev = asyncio.run(_drain())
    assert ev.type == "tool_start"
    assert ev.payload["name"] == "vision_screen"


@pytest.mark.unit
def test_unsubscribe_stops_delivery():
    from maahi.event_bus import bus

    b = bus()
    sub = b.subscribe()
    b.unsubscribe(sub)

    b.publish("state", {"state": "thinking"})
    assert sub._q.empty()


@pytest.mark.unit
def test_slow_subscriber_drops_oldest_under_pressure(monkeypatch):
    """Per-subscriber queue is bounded; bus must not block on a slow consumer."""
    from maahi import event_bus

    monkeypatch.setattr(event_bus, "_SUBSCRIBER_QUEUE_MAX", 4)
    monkeypatch.setattr(event_bus, "_BUS", None)
    b = event_bus.bus()
    sub = b.subscribe()

    for i in range(10):
        b.publish("state", {"state": f"x{i}"})

    drained = []
    while True:
        try:
            drained.append(sub._q.get_nowait())
        except queue.Empty:
            break

    assert len(drained) <= 4
    assert drained[-1].payload["state"] == "x9"


@pytest.mark.unit
def test_ring_buffer_caps_history(monkeypatch):
    from maahi import event_bus

    monkeypatch.setattr(event_bus, "_REPLAY_BUFFER_MAX", 5)
    monkeypatch.setattr(event_bus, "_BUS", None)
    b = event_bus.bus()

    for i in range(20):
        b.publish("tick", {"i": i})

    recent = b.recent(limit=100)
    assert len(recent) == 5
    assert recent[0].payload["i"] == 15
    assert recent[-1].payload["i"] == 19


# ============================================================
#  control tool arg parsing & clamp
# ============================================================


@pytest.mark.unit
def test_parse_combo_normalizes_aliases():
    from maahi.tools.control import _parse_combo

    assert _parse_combo("cmd+t") == ["command", "t"]
    assert _parse_combo("CMD+Shift+4") == ["command", "shift", "4"]
    assert _parse_combo("return") == ["enter"]
    assert _parse_combo("esc") == ["escape"]
    assert _parse_combo("alt+f4") == ["option", "f4"]


@pytest.mark.unit
def test_parse_combo_rejects_empty():
    from maahi.tools.control import _parse_combo

    with pytest.raises(ValueError):
        _parse_combo("")
    with pytest.raises(ValueError):
        _parse_combo("+")


@pytest.mark.unit
def test_clamp_keeps_coords_in_screen(monkeypatch):
    from maahi.tools import control

    monkeypatch.setattr(control, "_screen_size", lambda: (1440, 900))

    assert control._clamp(100, 200) == (100, 200)
    assert control._clamp(-5, -10) == (0, 0)
    assert control._clamp(5000, 5000) == (1439, 899)


@pytest.mark.unit
def test_guard_returns_disabled_error(monkeypatch):
    from maahi.tools import control

    class FakeCtl:
        enabled = False

    class FakeCfg:
        control = FakeCtl()

    monkeypatch.setattr(control, "get_config", lambda: FakeCfg())
    result = control._guard_enabled()
    assert result is not None
    assert result["ok"] is False
    assert "disabled" in result["error"].lower()

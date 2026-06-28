"""Tests for the pause-listening state machine."""
from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def fresh_bus(monkeypatch: pytest.MonkeyPatch):
    from maahi import event_bus, listening_state
    monkeypatch.setattr(event_bus, "_BUS", None)
    monkeypatch.setattr(listening_state, "_listener_started", False)
    monkeypatch.setattr(listening_state, "_LISTENER_THREAD", None)
    listening_state.set_paused(False)
    yield
    listening_state.set_paused(False)


@pytest.mark.unit
def test_set_and_query() -> None:
    from maahi import listening_state
    assert listening_state.is_paused() is False
    listening_state.set_paused(True)
    assert listening_state.is_paused() is True
    listening_state.set_paused(False)
    assert listening_state.is_paused() is False


@pytest.mark.unit
def test_listener_consumes_listening_set_events() -> None:
    from maahi import event_bus, listening_state
    listening_state.start_listener()
    time.sleep(0.05)

    event_bus.bus().publish("listening_set", {"paused": True})
    deadline = time.time() + 1.0
    while time.time() < deadline:
        if listening_state.is_paused():
            break
        time.sleep(0.01)
    assert listening_state.is_paused() is True

    event_bus.bus().publish("listening_set", {"paused": False})
    deadline = time.time() + 1.0
    while time.time() < deadline:
        if not listening_state.is_paused():
            break
        time.sleep(0.01)
    assert listening_state.is_paused() is False


@pytest.mark.unit
def test_listener_ignores_unrelated_events() -> None:
    from maahi import event_bus, listening_state
    listening_state.start_listener()
    time.sleep(0.05)
    event_bus.bus().publish("state", {"state": "thinking"})
    event_bus.bus().publish("transcript", {"speaker": "user", "text": "hi"})
    time.sleep(0.05)
    assert listening_state.is_paused() is False


@pytest.mark.unit
def test_start_listener_is_idempotent() -> None:
    from maahi import listening_state
    t1 = listening_state.start_listener()
    t2 = listening_state.start_listener()
    assert t1 is t2

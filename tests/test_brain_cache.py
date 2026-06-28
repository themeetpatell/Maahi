"""Tests for Brain.prewarm() and the tool-result LRU cache."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def isolated_brain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from maahi import config as cfg_mod
    from maahi.config import (
        BargeInCfg, BrainCfg, Config, ControlCfg, HudCfg, LogCfg,
        OwnerCfg, ProactiveCfg, STTCfg, TTSCfg, VaultCfg, VisionCfg, WakeCfg,
    )
    memdir = tmp_path / "maahi" / "memory"
    memdir.mkdir(parents=True)
    fake = Config(
        owner=OwnerCfg("T", "t@example.com", ""),
        wake=WakeCfg(("hey maahi",), "whisper_loop", 0.5, 0.6),
        stt=STTCfg("tiny.en", "auto", "int8", "en"),
        brain=BrainCfg("ollama", "http://localhost:11434", "qwen2.5:7b", 0.4, 32, 8),
        tts=TTSCfg("Samantha", 200, True, "say", ""),
        vault=VaultCfg(tmp_path, memdir, "Daily Notes"),
        logging=LogCfg("INFO", tmp_path / "log.log"),
        hud=HudCfg(False, 7421, 420, 220, 40, -60, True, True, 8.0),
        vision=VisionCfg("qwen2.5vl:7b", 1280, 80, tmp_path / "vision"),
        control=ControlCfg(True, False, 1800),
        proactive=ProactiveCfg(True, 60, 5),
        barge_in=BargeInCfg(True, ("maahi stop",)),
        shell_allowlist=(),
    )
    monkeypatch.setattr(cfg_mod, "_cfg", fake)
    from maahi.brain import Brain
    return Brain()


@pytest.mark.unit
def test_cache_hit_short_circuits_tool_call(
    isolated_brain, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from maahi import brain as brain_mod

    calls = {"n": 0}
    def fake_call(name, args):
        calls["n"] += 1
        return {"ok": True, "now": "2026-05-24T15:00:00", "call_num": calls["n"]}
    monkeypatch.setattr(brain_mod, "call_tool", fake_call)

    a = isolated_brain._cached_or_call("now", {})
    b = isolated_brain._cached_or_call("now", {})
    assert calls["n"] == 1, "second cacheable call must hit the cache"
    assert a == b


@pytest.mark.unit
def test_uncacheable_tool_always_executes(
    isolated_brain, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from maahi import brain as brain_mod

    calls = {"n": 0}
    def fake_call(name, args):
        calls["n"] += 1
        return {"ok": True, "wrote": True}
    monkeypatch.setattr(brain_mod, "call_tool", fake_call)

    isolated_brain._cached_or_call("send_imessage", {"recipient": "a", "text": "hi"})
    isolated_brain._cached_or_call("send_imessage", {"recipient": "a", "text": "hi"})
    assert calls["n"] == 2


@pytest.mark.unit
def test_cache_evicts_when_full(
    isolated_brain, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from maahi import brain as brain_mod

    isolated_brain._CACHE_MAX = 3
    monkeypatch.setattr(
        brain_mod, "call_tool",
        lambda name, args: {"ok": True, "args": args},
    )
    for i in range(5):
        isolated_brain._cached_or_call("obsidian_search", {"query": f"q{i}"})
    assert len(isolated_brain._tool_cache) <= 3


@pytest.mark.unit
def test_prewarm_swallows_http_errors(isolated_brain) -> None:
    import httpx

    class _BoomHttp:
        def post(self, *a, **kw):
            raise httpx.ConnectError("ollama down")
    isolated_brain._http = _BoomHttp()
    assert isolated_brain.prewarm() is False

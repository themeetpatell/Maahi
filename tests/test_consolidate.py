"""Tests for the nightly memory consolidator (maahi/consolidate.py).

We stub the LLM call so the tests don't need a running Ollama.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest


@pytest.fixture()
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    vault = tmp_path / "vault"
    memdir = vault / "maahi" / "memory"
    convo_dir = memdir / "conversations"
    convo_dir.mkdir(parents=True)

    today = date.today()
    fname = f"{today.strftime('%Y%m%d')}-120000.jsonl"
    convo = convo_dir / fname
    convo.write_text(
        "\n".join([
            json.dumps({"ts": "2026-05-24T12:00:00", "role": "user",
                        "content": "Remember my passport expires in October 2027."}),
            json.dumps({"ts": "2026-05-24T12:00:05", "role": "maahi",
                        "content": "Got it."}),
        ]) + "\n",
        encoding="utf-8",
    )

    from maahi import config as cfg_mod
    from maahi.config import (
        BargeInCfg, BrainCfg, Config, ControlCfg, HudCfg, LogCfg,
        OwnerCfg, ProactiveCfg, STTCfg, TTSCfg, VaultCfg, VisionCfg, WakeCfg,
    )
    fake = Config(
        owner=OwnerCfg("Test", "t@example.com", ""),
        wake=WakeCfg(("hey maahi",), "whisper_loop", 0.5, 0.6),
        stt=STTCfg("tiny.en", "auto", "int8", "en"),
        brain=BrainCfg("ollama", "http://localhost:11434", "qwen2.5:7b", 0.4, 256, 8),
        tts=TTSCfg("Samantha", 200, True, "say", ""),
        vault=VaultCfg(vault, memdir, "Daily Notes"),
        logging=LogCfg("INFO", tmp_path / "log.log"),
        hud=HudCfg(False, 7421, 420, 220, 40, -60, True, True, 8.0),
        vision=VisionCfg("qwen2.5vl:7b", 1280, 80, tmp_path / "vision"),
        control=ControlCfg(True, False, 1800),
        proactive=ProactiveCfg(True, 60, 5),
        barge_in=BargeInCfg(True, ("maahi stop",)),
        shell_allowlist=(),
    )
    monkeypatch.setattr(cfg_mod, "_cfg", fake)
    return memdir


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
    def raise_for_status(self) -> None:
        pass
    def json(self) -> dict:
        return self._payload


class _FakeHttp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls = 0
    def post(self, _url, json=None):  # noqa: ARG002
        self.calls += 1
        return _FakeResponse(self._payload)


def _envelope(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


@pytest.mark.unit
def test_consolidate_extracts_facts_and_prefs(fake_env: Path) -> None:
    from maahi import consolidate

    fake = _FakeHttp(_envelope(json.dumps({
        "facts": ["Meet's passport expires in October 2027."],
        "preferences": {"speak_rate": "200"},
    })))
    result = consolidate.consolidate_date(date.today(), http=fake)
    assert result["ok"] is True
    assert result["facts_added"] == 1
    assert result["preferences_set"] == 1

    facts = (fake_env / "facts.md").read_text(encoding="utf-8")
    assert "passport expires in October 2027" in facts

    prefs = (fake_env / "preferences.md").read_text(encoding="utf-8")
    assert "speak_rate: 200" in prefs


@pytest.mark.unit
def test_consolidate_is_idempotent(fake_env: Path) -> None:
    from maahi import consolidate

    fake = _FakeHttp(_envelope(json.dumps({
        "facts": ["First-run fact."], "preferences": {},
    })))
    consolidate.consolidate_date(date.today(), http=fake)

    fake2 = _FakeHttp(_envelope(json.dumps({
        "facts": ["Second-run fact (should be ignored)."], "preferences": {},
    })))
    result = consolidate.consolidate_date(date.today(), http=fake2)
    assert fake2.calls == 0
    assert result["files_processed"] == []
    facts = (fake_env / "facts.md").read_text(encoding="utf-8")
    assert "Second-run fact" not in facts


@pytest.mark.unit
def test_consolidate_force_bypasses_ledger(fake_env: Path) -> None:
    from maahi import consolidate

    fake = _FakeHttp(_envelope(json.dumps({"facts": ["A"], "preferences": {}})))
    consolidate.consolidate_date(date.today(), http=fake)

    fake2 = _FakeHttp(_envelope(json.dumps({"facts": ["B"], "preferences": {}})))
    result = consolidate.consolidate_date(date.today(), http=fake2, force=True)
    assert fake2.calls == 1
    assert result["facts_added"] == 1
    facts = (fake_env / "facts.md").read_text(encoding="utf-8")
    assert "A" in facts and "B" in facts


@pytest.mark.unit
def test_consolidate_handles_non_json_llm_output(fake_env: Path) -> None:
    from maahi import consolidate

    fake = _FakeHttp(_envelope("here are some facts: meet likes coffee"))
    result = consolidate.consolidate_date(date.today(), http=fake)
    assert result["facts_added"] == 0
    ledger = json.loads((fake_env / "consolidated.json").read_text(encoding="utf-8"))
    assert ledger["processed"]


@pytest.mark.unit
def test_consolidate_strips_code_fences(fake_env: Path) -> None:
    from maahi import consolidate

    fenced = "```json\n" + json.dumps(
        {"facts": ["Fenced fact."], "preferences": {}},
    ) + "\n```"
    fake = _FakeHttp(_envelope(fenced))
    result = consolidate.consolidate_date(date.today(), http=fake)
    assert result["facts_added"] == 1

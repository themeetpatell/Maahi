"""Integration tests for the new HUD REST endpoints."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient


_BASE_CONFIG = {
    "owner": {"name": "Meet", "email": "meet@finanshels.com", "bio": "Founder."},
    "wake": {
        "phrases": ["hey maahi"], "engine": "whisper_loop",
        "vad_threshold": 0.5, "silence_seconds": 0.6,
    },
    "stt": {"model": "tiny.en", "device": "auto",
            "compute_type": "int8", "language": "en"},
    "brain": {"provider": "ollama", "host": "http://localhost:11434",
              "model": "qwen2.5:7b", "temperature": 0.4,
              "max_tokens": 256, "max_iterations": 8},
    "tts": {"engine": "say", "voice": "Samantha", "rate": 200, "stream": True,
            "piper_voice": ""},
    "vault": {"path": "/tmp/vault", "memory_dir": "/tmp/vault/maahi/memory",
              "daily_notes_dir": "Daily Notes"},
    "logging": {"level": "INFO", "path": "/tmp/maahi.log"},
    "hud": {"enabled": True, "port": 7421, "width": 420, "height": 220,
            "x": 40, "y": -60, "always_on_top": True,
            "transparent": True, "collapse_seconds": 8},
    "vision": {"model": "qwen2.5vl:7b", "max_image_side": 1280,
               "jpeg_quality": 80, "scratch_dir": "/tmp/vision"},
    "control": {"enabled": True, "require_confirm_for_clicks": False,
                "cursor_smooth_pxps": 1800},
    "proactive": {"enabled": True, "poll_seconds": 60, "lead_minutes": 5},
    "barge_in": {"enabled": True, "stop_words": ["maahi stop"]},
    "shell_allowlist": ["ls"],
}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    cfg_path = tmp_path / "config.yaml"
    memdir = tmp_path / "maahi" / "memory"
    memdir.mkdir(parents=True)
    (memdir / "conversations").mkdir()

    cfg = dict(_BASE_CONFIG)
    cfg["vault"] = dict(cfg["vault"])
    cfg["vault"]["path"] = str(tmp_path)
    cfg["vault"]["memory_dir"] = str(memdir)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    from maahi import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(cfg_mod, "_cfg", None)

    from maahi.hud_server import _build_app
    return TestClient(_build_app())


def test_get_config_returns_editable_subset(client: TestClient) -> None:
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["config"]["owner"]["name"] == "Meet"
    assert body["config"]["tts"]["voice"] == "Samantha"


def test_post_config_writes_yaml_atomically(
    client: TestClient, tmp_path: Path,
) -> None:
    r = client.post("/api/config", json={"patch": {"tts": {"voice": "Karen", "rate": 220}}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["written"] >= 2

    on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert on_disk["tts"]["voice"] == "Karen"
    assert on_disk["tts"]["rate"] == 220
    assert on_disk["owner"]["name"] == "Meet"


def test_post_config_ignores_unauthorized_keys(
    client: TestClient, tmp_path: Path,
) -> None:
    r = client.post("/api/config", json={"patch": {"shell_allowlist": ["rm"]}})
    assert r.status_code == 200
    on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert on_disk["shell_allowlist"] == ["ls"]


def test_skills_endpoint_lists_builtins(client: TestClient) -> None:
    r = client.get("/api/skills")
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["tools"]}
    assert "now" in names
    assert "obsidian_semantic_search" in names


def test_transcript_today_when_empty(client: TestClient) -> None:
    r = client.get("/api/transcript/today")
    assert r.status_code == 200
    assert r.json()["turn_count"] == 0


def test_voices_endpoint_returns_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from maahi import settings_api
    monkeypatch.setattr(
        settings_api, "list_macos_voices",
        lambda: [{"name": "Samantha", "locale": "en_US", "sample": "Hello."}],
    )
    r = client.get("/api/voices")
    assert r.status_code == 200
    assert r.json()["voices"][0]["name"] == "Samantha"


def test_permissions_endpoint_returns_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from maahi import permissions
    monkeypatch.setattr(permissions, "probe_all", lambda: {
        "mic": "granted", "accessibility": "granted",
        "automation": "denied", "screen": "unknown", "disk": "granted",
    })
    r = client.get("/api/permissions")
    assert r.status_code == 200
    assert r.json()["status"]["automation"] == "denied"


def test_listening_toggle_emits_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from maahi import event_bus
    monkeypatch.setattr(event_bus, "_BUS", None)
    sub = event_bus.bus().subscribe()
    r = client.post("/api/listening", json={"paused": True})
    assert r.status_code == 200
    assert r.json()["paused"] is True
    seen = []
    while not sub._q.empty():
        seen.append(sub._q.get_nowait())
    assert "listening_set" in {ev.type for ev in seen}

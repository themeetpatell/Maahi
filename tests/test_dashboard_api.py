"""Integration tests for the dashboard-backend endpoints."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient


_BASE_CONFIG = {
    "owner": {"name": "Meet", "email": "meet@finanshels.com", "bio": "Founder."},
    "wake": {"phrases": ["hey maahi"], "engine": "whisper_loop",
             "vad_threshold": 0.5, "silence_seconds": 0.6},
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
    log_path = tmp_path / "maahi.log"

    cfg = dict(_BASE_CONFIG)
    cfg["vault"] = dict(cfg["vault"])
    cfg["vault"]["path"] = str(tmp_path)
    cfg["vault"]["memory_dir"] = str(memdir)
    cfg["logging"] = dict(cfg["logging"])
    cfg["logging"]["path"] = str(log_path)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    from maahi import config as cfg_mod, event_bus
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(cfg_mod, "_cfg", None)
    monkeypatch.setattr(event_bus, "_BUS", None)

    from maahi.hud_server import _build_app
    return TestClient(_build_app())


@pytest.mark.unit
def test_post_command_publishes_event(client: TestClient) -> None:
    from maahi import event_bus
    sub = event_bus.bus().subscribe()
    r = client.post("/api/command", json={"text": "what time is it"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    seen_types = []
    while not sub._q.empty():
        seen_types.append(sub._q.get_nowait().type)
    assert "text_command" in seen_types


@pytest.mark.unit
def test_post_command_rejects_empty(client: TestClient) -> None:
    r = client.post("/api/command", json={"text": "   "})
    body = r.json()
    assert body["ok"] is False
    assert "empty" in body["error"].lower()


@pytest.mark.unit
def test_get_facts_when_missing(client: TestClient) -> None:
    r = client.get("/api/memory/facts")
    body = r.json()
    assert body["ok"] is True
    assert body["exists"] is False
    assert body["content"] == ""


@pytest.mark.unit
def test_get_facts_returns_content(client: TestClient, tmp_path: Path) -> None:
    memdir = tmp_path / "maahi" / "memory"
    (memdir / "facts.md").write_text(
        "# Maahi — Facts\n\n- (2026-05-24) Meet's passport expires Oct 2027.\n",
        encoding="utf-8",
    )
    body = client.get("/api/memory/facts").json()
    assert body["exists"] is True
    assert "passport expires Oct 2027" in body["content"]


@pytest.mark.unit
def test_get_preferences_when_missing(client: TestClient) -> None:
    assert client.get("/api/memory/preferences").json()["exists"] is False


@pytest.mark.unit
def test_get_index_stats_empty(client: TestClient) -> None:
    body = client.get("/api/memory/index_stats").json()
    assert body["ok"] is True
    assert body["exists"] is False
    assert body["notes"] == 0


@pytest.mark.unit
def test_get_index_stats_populated(client: TestClient, tmp_path: Path) -> None:
    memdir = tmp_path / "maahi" / "memory"
    idx_dir = memdir / "vector_index"
    idx_dir.mkdir(parents=True)
    np.save(idx_dir / "vectors.npy", np.zeros((3, 8), dtype=np.float32))
    meta = [
        {"path": "a.md", "mtime": 1700000000.0, "sha": "abc", "preview": "..."},
        {"path": "b.md", "mtime": 1700000500.0, "sha": "def", "preview": "..."},
        {"path": "c.md", "mtime": 1700001000.0, "sha": "ghi", "preview": "..."},
    ]
    (idx_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    body = client.get("/api/memory/index_stats").json()
    assert body["exists"] is True
    assert body["notes"] == 3
    assert body["last_update_ts"] == 1700001000.0
    assert body["vectors_bytes"] > 0


@pytest.mark.unit
def test_logs_tail(client: TestClient, tmp_path: Path) -> None:
    log_path = tmp_path / "maahi.log"
    log_path.write_text("\n".join(f"line {i}" for i in range(250)) + "\n",
                        encoding="utf-8")
    body = client.get("/api/logs?lines=50").json()
    assert body["exists"] is True
    assert body["total_returned"] == 50
    assert body["lines"][-1] == "line 249"


@pytest.mark.unit
def test_dashboard_route_serves_html(client: TestClient) -> None:
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "MAAHI" in r.text or "Command Center" in r.text


@pytest.mark.unit
def test_dashboard_open_endpoint_publishes_event(client: TestClient) -> None:
    from maahi import event_bus
    sub = event_bus.bus().subscribe()
    r = client.post("/api/dashboard/open")
    assert r.status_code == 200
    types = []
    while not sub._q.empty():
        types.append(sub._q.get_nowait().type)
    assert "dashboard:open" in types

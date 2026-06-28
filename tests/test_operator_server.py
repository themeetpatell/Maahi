"""Command-center HTTP surface — endpoints + auth gate."""
from __future__ import annotations

import os

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient  # noqa: E402


def _client():
    from maahi.operator.server import create_app

    return TestClient(create_app())


def test_healthz_open():
    r = _client().get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_status_shape():
    r = _client().get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert "connectors" in body
    assert body["total_count"] >= 10
    assert "autonomy" in body


def test_brief_endpoint_no_synth():
    r = _client().get("/api/brief?synthesize=0")
    assert r.status_code == 200
    assert "pulses" in r.json()


def test_index_serves_html():
    r = _client().get("/")
    assert r.status_code == 200
    assert "Maahi" in r.text


def test_auth_gate(monkeypatch):
    monkeypatch.setenv("MAAHI_OPERATOR_TOKEN", "s3cret")
    from maahi.operator.config import reload_operator_config

    reload_operator_config()
    try:
        client = _client()
        # No token → 401 on protected route.
        assert client.get("/api/status").status_code == 401
        # Correct token → 200.
        ok = client.get("/api/status", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        # Health stays open.
        assert client.get("/healthz").status_code == 200
    finally:
        monkeypatch.delenv("MAAHI_OPERATOR_TOKEN", raising=False)
        reload_operator_config()

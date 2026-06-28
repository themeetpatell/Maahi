"""Tests for the vault vector index and the semantic_search tool wrapper.

We stub out the real Ollama embedding call with a deterministic hash-based
embedder so the tests are hermetic and fast.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest


class _FakeEmbedder:
    """Hash text into a 16-d vector. Deterministic: same text → same vector."""

    DIM = 16

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def embed(self, text: str) -> np.ndarray:
        h = hashlib.sha256((text or "").encode("utf-8")).digest()
        arr = np.frombuffer(h[: self.DIM], dtype=np.uint8).astype(np.float32)
        return (arr - 128.0) / 128.0


@pytest.fixture()
def fake_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a tiny vault + memory dir and stub the embedder."""
    vault = tmp_path / "vault"
    memdir = vault / "maahi" / "memory"
    (vault / "Daily Notes").mkdir(parents=True)
    memdir.mkdir(parents=True)
    (vault / "Daily Notes" / "2026-05-24.md").write_text(
        "Morning standup. Focus on Maahi UX wave 1.", encoding="utf-8",
    )
    (vault / "ICP.md").write_text(
        "BiggDate ICP v2 — 27-34 NRI women in Dubai who've quit apps.",
        encoding="utf-8",
    )
    (vault / "Random.md").write_text(
        "Buy oat milk. Call electrician.", encoding="utf-8",
    )

    from maahi import config as cfg_mod
    from maahi.config import (
        BargeInCfg,
        BrainCfg,
        Config,
        ControlCfg,
        HudCfg,
        LogCfg,
        OwnerCfg,
        ProactiveCfg,
        STTCfg,
        TTSCfg,
        VaultCfg,
        VisionCfg,
        WakeCfg,
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

    from maahi.tools import _vector_index as vi
    monkeypatch.setattr(vi, "_INDEX", None)
    monkeypatch.setattr(vi, "EmbeddingClient", _FakeEmbedder)
    return vault


@pytest.mark.unit
def test_index_builds_and_ranks(fake_vault: Path) -> None:
    from maahi.tools._vector_index import get_index

    idx = get_index()
    stats = idx.update()
    assert stats["total"] == 3
    assert stats["changed"] == 3

    # Exact-text query: cosine to its own vector is 1.0
    hits = idx.search(
        "BiggDate ICP v2 — 27-34 NRI women in Dubai who've quit apps.",
    )
    assert hits[0]["path"].endswith("ICP.md")
    assert hits[0]["score"] > 0.99


@pytest.mark.unit
def test_index_is_incremental(fake_vault: Path) -> None:
    from maahi.tools._vector_index import get_index

    idx = get_index()
    idx.update()
    stats2 = idx.update()
    assert stats2["changed"] == 0
    assert stats2["removed"] == 0

    (fake_vault / "ICP.md").write_text("totally different content now", encoding="utf-8")
    stats3 = idx.update()
    assert stats3["changed"] == 1


@pytest.mark.unit
def test_index_drops_removed_notes(fake_vault: Path) -> None:
    from maahi.tools._vector_index import get_index

    idx = get_index()
    idx.update()
    (fake_vault / "Random.md").unlink()
    stats = idx.update()
    assert stats["removed"] == 1
    assert stats["total"] == 2


@pytest.mark.unit
def test_semantic_search_falls_back_to_grep_when_embeddings_fail(
    fake_vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from maahi.tools import _vector_index as vi

    class _Broken(_FakeEmbedder):
        def embed(self, text: str) -> np.ndarray:  # noqa: D401
            raise RuntimeError("ollama down")

    monkeypatch.setattr(vi, "_INDEX", None)
    monkeypatch.setattr(vi, "EmbeddingClient", _Broken)

    from maahi.tools.obsidian import semantic_search
    result = semantic_search("oat milk")
    assert result["ok"] is True
    assert result.get("fallback") == "grep"
    paths = [h["path"] for h in result["hits"]]
    assert any("Random.md" in p for p in paths)


@pytest.mark.unit
def test_semantic_search_rejects_empty_query(fake_vault: Path) -> None:
    from maahi.tools.obsidian import semantic_search
    assert semantic_search("   ")["ok"] is False

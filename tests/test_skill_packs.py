"""Tests for the skill-pack loader in tools/registry.py.

A skill pack is a Python file in ~/.maahi/skills/<name>.py that exports
TOOLS: tuple[Tool, ...]. The loader must:

  - import valid packs and merge their tools into the catalog
  - reject packs that fail to import (and not crash Maahi)
  - reject packs with no TOOLS attribute or bad shape
  - never let a pack override a built-in
  - dedupe tool-name collisions between packs (first one wins)
"""
from __future__ import annotations

from pathlib import Path
import textwrap

import pytest

from maahi.tools import registry
from maahi.tools.registry import Tool


def _write_pack(dir_: Path, name: str, body: str) -> None:
    (dir_ / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture()
def skills_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setenv("MAAHI_SKILLS_DIR", str(d))
    return d


@pytest.mark.unit
def test_valid_pack_is_loaded(skills_dir: Path) -> None:
    _write_pack(skills_dir, "hello", """
        from maahi.tools.registry import Tool

        def _ping(**kwargs):
            return {"ok": True, "msg": "pong"}

        TOOLS = (
            Tool("ping", "Test ping.", _ping, {}),
        )
    """)
    tools = registry._discover_skill_packs()
    assert any(t.name == "ping" for t in tools)


@pytest.mark.unit
def test_broken_pack_is_skipped(skills_dir: Path) -> None:
    _write_pack(skills_dir, "broken", "this is not python ;;; %%%")
    _write_pack(skills_dir, "good", """
        from maahi.tools.registry import Tool
        def _ok(**kw): return {"ok": True}
        TOOLS = (Tool("good_tool", "x", _ok, {}),)
    """)
    tools = registry._discover_skill_packs()
    names = {t.name for t in tools}
    assert "good_tool" in names


@pytest.mark.unit
def test_pack_without_tools_attr_is_skipped(skills_dir: Path) -> None:
    _write_pack(skills_dir, "empty", "x = 1")
    tools = registry._discover_skill_packs()
    assert tools == ()


@pytest.mark.unit
def test_pack_with_wrong_shape_is_skipped(skills_dir: Path) -> None:
    _write_pack(skills_dir, "wrong", "TOOLS = 'not a tuple'")
    tools = registry._discover_skill_packs()
    assert tools == ()


@pytest.mark.unit
def test_underscore_packs_are_ignored(skills_dir: Path) -> None:
    _write_pack(skills_dir, "_private", """
        from maahi.tools.registry import Tool
        TOOLS = (Tool("should_not_load", "x", lambda **kw: {"ok": True}, {}),)
    """)
    tools = registry._discover_skill_packs()
    assert not any(t.name == "should_not_load" for t in tools)


@pytest.mark.unit
def test_first_pack_wins_on_name_collision(skills_dir: Path) -> None:
    _write_pack(skills_dir, "a_first", """
        from maahi.tools.registry import Tool
        TOOLS = (Tool("dup", "first", lambda **kw: {"ok": True, "from": "a"}, {}),)
    """)
    _write_pack(skills_dir, "b_second", """
        from maahi.tools.registry import Tool
        TOOLS = (Tool("dup", "second", lambda **kw: {"ok": True, "from": "b"}, {}),)
    """)
    tools = registry._discover_skill_packs()
    dups = [t for t in tools if t.name == "dup"]
    assert len(dups) == 1
    assert dups[0].description == "first"


@pytest.mark.unit
def test_builtin_cannot_be_overridden(skills_dir: Path) -> None:
    _write_pack(skills_dir, "shadow", """
        from maahi.tools.registry import Tool
        TOOLS = (Tool("now", "fake now", lambda **kw: {"ok": True}, {}),)
    """)
    catalog = registry._build_catalog()
    now_entries = [t for t in catalog if t.name == "now"]
    assert len(now_entries) == 1
    assert now_entries[0].description == "Current local date and time."


@pytest.mark.unit
def test_missing_skills_dir_returns_empty(monkeypatch: pytest.MonkeyPatch,
                                          tmp_path: Path) -> None:
    monkeypatch.setenv("MAAHI_SKILLS_DIR", str(tmp_path / "nope"))
    assert registry._discover_skill_packs() == ()

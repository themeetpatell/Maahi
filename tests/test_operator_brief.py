"""The executive brief assembles safely with zero or partial config."""
from __future__ import annotations

from maahi.operator.brief import build_brief
from maahi.operator.connectors.registry import ConnectorRegistry


def test_brief_builds_without_keys():
    # synthesize=False so we never need a live Claude key.
    brief = build_brief(ConnectorRegistry(), synthesize=False)
    assert brief.headline
    assert brief.narrative          # falls back to a templated narrative
    assert len(brief.pulses) >= 10  # one per connector in the roster


def test_brief_marks_unconfigured_systems():
    brief = build_brief(ConnectorRegistry(), synthesize=False)
    # Every pulse is labeled and carries a configured flag.
    for p in brief.pulses:
        assert p.label
        assert isinstance(p.configured, bool)


def test_brief_markdown_renders():
    brief = build_brief(ConnectorRegistry(), synthesize=False)
    md = brief.markdown()
    assert "Maahi Brief" in md
    assert "## Systems" in md


def test_brief_to_dict_is_serializable():
    import json

    brief = build_brief(ConnectorRegistry(), synthesize=False)
    json.dumps(brief.to_dict())  # must not raise

"""Autonomy policy — the act-then-report governor."""
from __future__ import annotations

from maahi.operator.policy import Autonomy, Decision, describe, evaluate


def test_act_report_allows_reads_and_writes():
    assert evaluate("read", Autonomy.ACT_REPORT).decision is Decision.ALLOW
    assert evaluate("write", Autonomy.ACT_REPORT).decision is Decision.ALLOW


def test_act_report_confirms_outbound_and_costly():
    for risk in ("send", "spend", "delete", "publish"):
        v = evaluate(risk, Autonomy.ACT_REPORT)
        assert v.decision is Decision.CONFIRM, risk
        assert v.needs_confirmation


def test_suggest_confirms_everything_but_read():
    assert evaluate("read", Autonomy.SUGGEST).decision is Decision.ALLOW
    for risk in ("write", "publish", "send", "spend", "delete"):
        assert evaluate(risk, Autonomy.SUGGEST).decision is Decision.CONFIRM, risk


def test_autopilot_allows_everything():
    for risk in ("read", "write", "publish", "send", "spend", "delete"):
        assert evaluate(risk, Autonomy.AUTOPILOT).decision is Decision.ALLOW, risk


def test_unknown_risk_fails_safe():
    # Anything we don't recognize is treated as the most dangerous.
    assert evaluate("nuke_everything", Autonomy.ACT_REPORT).decision is Decision.CONFIRM


def test_autonomy_parse_aliases():
    assert Autonomy.parse("act-then-report") is Autonomy.ACT_REPORT
    assert Autonomy.parse("yolo") is Autonomy.AUTOPILOT
    assert Autonomy.parse("manual") is Autonomy.SUGGEST
    assert Autonomy.parse("") is Autonomy.ACT_REPORT  # safe default


def test_describe_is_human():
    assert describe("send") == "outbound"
    assert describe("write") == "draft/internal"

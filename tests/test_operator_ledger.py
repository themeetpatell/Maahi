"""The audit ledger + pending-approval queue."""
from __future__ import annotations

from maahi.operator.ledger import Ledger


def test_record_and_recent(tmp_path):
    led = Ledger(state_dir=tmp_path)
    led.record("zoho_crm.list_deals", risk="read", summary="listed 5 deals")
    led.record("github.create_issue", risk="write", summary="opened #1")
    recent = led.recent(limit=10)
    assert len(recent) == 2
    assert recent[-1]["action"] == "github.create_issue"
    assert recent[-1]["risk"] == "write"


def test_propose_and_approve(tmp_path):
    led = Ledger(state_dir=tmp_path)
    pa = led.propose("gmail.send", risk="send", summary="email to investor",
                     params={"to": "vc@fund.com"}, reason="outbound")
    assert pa.id
    assert len(led.pending()) == 1
    record = led.resolve(pa.id, approved=True)
    assert record is not None
    assert record["action"] == "gmail.send"
    assert led.pending() == []
    # The approval is audited.
    assert any(e["decision"] == "approved" for e in led.recent())


def test_propose_and_reject(tmp_path):
    led = Ledger(state_dir=tmp_path)
    pa = led.propose("meta_ads.set_daily_budget", risk="spend", summary="+$500/day")
    led.resolve(pa.id, approved=False)
    assert led.pending() == []
    assert any(e["decision"] == "rejected" for e in led.recent())


def test_resolve_unknown_id_returns_none(tmp_path):
    led = Ledger(state_dir=tmp_path)
    assert led.resolve("deadbeef", approved=True) is None

"""Connectors load, expose capabilities, and degrade + dispatch safely."""
from __future__ import annotations

from maahi.operator.connectors.base import RISK_ORDER, Capability, ConnectorResult
from maahi.operator.connectors.registry import ConnectorRegistry
from maahi.operator.policy import Autonomy

EXPECTED_KEYS = {
    "zoho_crm", "gmail", "meta_ads", "webflow", "github", "vercel",
    "supabase", "cloudflare", "notion", "gdrive", "mcp",
}


def test_all_connectors_load():
    reg = ConnectorRegistry()
    keys = set(reg.all().keys())
    missing = EXPECTED_KEYS - keys
    assert not missing, f"connectors failed to load: {missing}"


def test_every_capability_is_well_formed():
    reg = ConnectorRegistry()
    for key, conn in reg.all().items():
        caps = conn.capabilities()
        assert caps, f"{key} exposes no capabilities"
        for cap in caps:
            assert isinstance(cap, Capability)
            assert cap.risk in RISK_ORDER, f"{key}.{cap.name} bad risk {cap.risk}"


def test_namespaced_tool_catalog():
    reg = ConnectorRegistry()
    tools = reg.tools()
    assert len(tools) >= 40
    names = {t.name for t in tools}
    # Namespaced as <connector>.<capability>
    assert any(n.startswith("zoho_crm.") for n in names)
    assert "github.list_repos" in names


def test_unconfigured_connector_degrades_not_crashes():
    """A capability call on an unconfigured connector returns a clean failure."""
    reg = ConnectorRegistry()
    notion = reg.get("notion")
    assert notion is not None
    if not notion.configured():
        res = notion.call("search", query="x")
        assert isinstance(res, ConnectorResult)
        assert res.ok is False
        assert res.meta.get("not_configured") is True


def test_dispatch_parks_risky_actions_for_confirmation():
    """Under act_report, a 'send' action must be parked, never executed."""
    reg = ConnectorRegistry()
    disp = reg.dispatch("gmail.send",
                        {"to": "a@b.com", "subject": "hi", "body": "x"},
                        autonomy=Autonomy.ACT_REPORT, origin="test")
    assert disp.status == "needs_confirmation"
    assert disp.pending_id
    # It should now appear in the pending queue.
    assert any(p["id"] == disp.pending_id for p in reg._ledger.pending())
    # Clean up so we don't leak into other tests.
    reg.reject(disp.pending_id)


def test_dispatch_reads_run_without_confirmation():
    """A read on an unconfigured connector runs (and fails as not-configured),
    but is never parked for confirmation."""
    reg = ConnectorRegistry()
    if reg.get("notion").configured():
        return  # skip if a token happens to be set
    disp = reg.dispatch("notion.search", {"query": "x"},
                        autonomy=Autonomy.ACT_REPORT, origin="test")
    assert disp.status in ("done", "failed")
    assert disp.status != "needs_confirmation"


def test_unknown_tool_is_handled():
    reg = ConnectorRegistry()
    disp = reg.dispatch("nope.nope", {}, autonomy=Autonomy.AUTOPILOT)
    assert disp.status == "failed"

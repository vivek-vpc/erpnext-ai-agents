"""Agent behaviour across runs: no duplicates, snooze/ignore/cool-down, escalation, recovery, caps."""
import pytest

from erp_agents.core.erp.mock import MockERP

from conftest import OWNER
from conftest import tamper


def by_item(report):
    return {a["item_code"]: a for a in report.actions}


def alert_for(app, code):
    return app.alerts.latest_for_item(code)


def pending_id(app, code):
    return alert_for(app, code).approval_id


@pytest.fixture
def demo(make_app, clock):
    return make_app(erp=MockERP(today=clock().date()))


def test_first_run_classifies_every_scenario(demo):
    r = demo.inventory.run()
    got = {code: (a["status"], a["severity"]) for code, a in by_item(r).items()}
    assert got["STEEL-2MM"][0] == "OK" and got["WELD-ROD"][0] == "OK" and got["DEAD-Z"][0] == "OK"
    assert got["CABLE-3C"][0] == "OK"                                  # open PO arrives in time
    assert got["BOLT-M8"] == ("BREACH", "HIGH")
    assert got["OIL-5L"] == ("BREACH", "CRITICAL")
    assert got["RELAY-12V"] == ("BREACH", "CRITICAL")                  # PO arrives after stock-out
    assert got["FILTER-A"][0] == "BREACH"                              # new item, thresholds only
    assert got["PAINT-RED"] == ("EARLY_WARNING", "EARLY_WARNING")      # recent usage surge
    assert got["GASKET-X"] == ("EARLY_WARNING", "EARLY_WARNING")       # seasonal pattern
    assert r.new_alerts == 6 and not r.errors
    assert len(demo.approvals.list("pending")) == 6


def test_early_warnings_explain_the_history_behind_them(demo):
    demo.inventory.run()
    text = alert_for(demo, "PAINT-RED").reasoning_text
    assert "Not below any threshold yet" in text and "Usage is up" in text
    text = alert_for(demo, "GASKET-X").reasoning_text
    assert "Seasonal pattern" in text and "last year" in text


def test_second_run_does_not_duplicate(demo):
    demo.inventory.run()
    r2 = demo.inventory.run()
    assert r2.new_alerts == 0
    assert all(a["action"] in ("ok", "skipped:pending_approval") for a in r2.actions)
    assert len(demo.approvals.list("pending")) == 6


def test_dry_run_changes_nothing(demo):
    r = demo.inventory.run(dry_run=True)
    assert r.new_alerts == 6 and demo.alerts.list() == [] and demo.approvals.list() == []
    assert all(a["action"] in ("ok", "would_alert") for a in r.actions)


def test_approve_emits_work_order_then_cooldown_then_realert(demo, clock):
    demo.inventory.run()
    res = demo.approvals.decide(pending_id(demo, "BOLT-M8"), "approve", OWNER, "test")
    assert res.status == "decided"
    wo = demo.workorders.list()[0]
    assert wo["type"] == "REORDER_NEEDED" and wo["payload"]["item_code"] == "BOLT-M8"
    assert wo["payload"]["suggested_qty"] > 0 and wo["payload"]["approved_by"] == "owner"
    assert wo["payload"]["analysis"]["usage"]["planning_rate"] > 0           # full snapshot travels with it
    assert alert_for(demo, "BOLT-M8").status == "actioned"
    assert by_item(demo.inventory.run())["BOLT-M8"]["action"] == "skipped:ordered_recently"
    clock.advance(days=4)                                                    # cooldown (3d) over, PO never appeared
    assert by_item(demo.inventory.run())["BOLT-M8"]["action"] == "new_alert"


def test_snooze_then_alert_again_after_it_ends(demo, clock):
    demo.inventory.run()
    demo.approvals.decide(pending_id(demo, "PAINT-RED"), "snooze", OWNER, "test")
    assert by_item(demo.inventory.run())["PAINT-RED"]["action"] == "skipped:snoozed"
    clock.advance(days=3, hours=1)
    assert by_item(demo.inventory.run())["PAINT-RED"]["action"] == "new_alert"


def test_ignore_suppresses_until_it_gets_worse(demo, clock):
    demo.inventory.run()
    demo.approvals.decide(pending_id(demo, "BOLT-M8"), "dismiss", OWNER, "test")
    assert by_item(demo.inventory.run())["BOLT-M8"]["action"] == "skipped:ignored_by_owner"
    demo.erp.set_stock("BOLT-M8", on_hand=100)                               # now far below lead time -> CRITICAL
    r = by_item(demo.inventory.run())["BOLT-M8"]
    assert r["action"] == "new_alert" and r["severity"] == "CRITICAL"


def test_escalation_supersedes_pending_request(demo):
    demo.inventory.run()
    old = alert_for(demo, "BOLT-M8")
    demo.erp.set_stock("BOLT-M8", on_hand=100)
    demo.inventory.run()
    new = alert_for(demo, "BOLT-M8")
    assert new.id != old.id and new.severity > old.severity
    assert demo.alerts.get(old.id).status == "superseded"
    assert demo.approvals.get(old.approval_id).status == "cancelled"
    assert demo.approvals.get(new.approval_id).status == "pending"


def test_recovery_resolves_alert_and_cancels_stale_request(demo):
    demo.inventory.run()
    a = alert_for(demo, "OIL-5L")
    demo.erp.set_stock("OIL-5L", on_hand=900)                                # goods received
    r = by_item(demo.inventory.run())["OIL-5L"]
    assert r["action"] == "resolved"
    assert demo.alerts.get(a.id).status == "resolved"
    assert demo.approvals.get(a.approval_id).status == "cancelled"
    assert demo.approvals.decide(a.approval_id, "approve", OWNER, "test").status == "cancelled"   # stale button press is harmless
    assert demo.workorders.list() == []


def test_run_cap_keeps_the_worst_items(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()), cfg={"system": {"max_alerts_per_run": 2}})
    r = app.inventory.run()
    assert r.new_alerts == 2
    created = {a["item_code"] for a in r.actions if a["action"] == "new_alert"}
    assert created == {"RELAY-12V", "OIL-5L"}                                # both CRITICAL
    assert sum(a["action"] == "skipped:run_cap_reached" for a in r.actions) == 4
    r2 = app.inventory.run()                                                 # remaining ones follow on the next run
    assert r2.new_alerts == 2


def test_unanswered_request_reminds_then_expires_and_is_never_auto_approved(demo, clock):
    demo.inventory.run()
    assert demo.approvals.remind_due() == []                                 # too early
    clock.advance(hours=25)
    assert len(demo.approvals.remind_due()) == 6
    assert demo.approvals.remind_due() == []                                 # one nudge per 24h
    clock.advance(hours=24)
    assert len(demo.approvals.remind_due()) == 6                             # 2nd (max_reminders=2)
    clock.advance(hours=24)
    assert demo.approvals.remind_due() == []                                 # cap reached
    assert demo.workorders.list() == []                                      # nothing approved by silence
    clock.advance(hours=1)                                                   # > 72h since creation
    assert len(demo.approvals.expire_due()) == 6
    assert alert_for(demo, "BOLT-M8").status == "expired"
    assert by_item(demo.inventory.run())["BOLT-M8"]["action"] == "new_alert"  # condition persists -> asks again


# ------------------------------------------------------------------ admin settings drive the agent
def test_admin_threshold_change_takes_effect_on_next_run(demo):
    assert by_item(demo.inventory.run(dry_run=True))["STEEL-2MM"]["action"] == "ok"
    demo.settings.update_policy(OWNER, "item", "STEEL-2MM", {"min_days_cover": 90})     # cover is 60 days
    r = by_item(demo.inventory.run(dry_run=True))["STEEL-2MM"]
    assert r["action"] == "would_alert" and r["status"] == "BREACH"


def test_group_level_quantity_threshold(demo):
    demo.settings.update_policy(OWNER, "group", "Raw Material", {"min_qty": 700})       # STEEL has 600, WELD-ROD 400
    r = by_item(demo.inventory.run(dry_run=True))
    assert r["STEEL-2MM"]["status"] == "BREACH" and r["WELD-ROD"]["status"] == "BREACH"
    assert r["BOLT-M8"]["status"] == "BREACH"                               # unaffected: still its own breach
    assert r["CABLE-3C"]["status"] == "OK"                                  # other groups untouched


def test_disabled_group_is_skipped(demo):
    demo.settings.update_policy(OWNER, "group", "Consumables", {"enabled": False})
    r = demo.inventory.run(dry_run=True)
    assert "OIL-5L" not in by_item(r) and "FILTER-A" not in by_item(r)


def test_bad_policy_for_one_item_does_not_stop_the_run(demo):
    # Bypass validation to simulate a corrupted row; the run must report it and carry on.
    demo.stores.settings.save("item", "BOLT-M8", '{"min_qty": -1}', "x", "2026-01-01T00:00:00+00:00", [])
    r = demo.inventory.run(dry_run=True)
    assert any("BOLT-M8" in e for e in r.errors) and "OIL-5L" in by_item(r)


def test_missing_stock_row_is_reported_not_fatal(make_app, clock):
    erp = MockERP(today=clock().date())
    original = erp.get_stock
    erp.get_stock = lambda codes, warehouses=None: {c: s for c, s in original(codes, warehouses).items() if c != "OIL-5L"}
    app = make_app(erp=erp)
    r = app.inventory.run(dry_run=True)
    assert any("OIL-5L" in e for e in r.errors) and "BOLT-M8" in by_item(r)


def test_item_filter(demo):
    r = demo.inventory.run(dry_run=True, item_codes=["BOLT-M8"])
    assert list(by_item(r)) == ["BOLT-M8"]


def test_run_is_logged(demo):
    r = demo.inventory.run()
    row = demo.stock_stores.runs.get(r.run_id)
    assert row["agent"] == "inventory" and row["dry_run"] == 0


def test_approving_a_reorder_sizes_the_order_from_todays_figures_not_the_day_the_alert_was_raised(demo, clock):
    demo.inventory.run()
    alert = alert_for(demo, "BOLT-M8")
    asked_then = alert.analysis["suggested_qty"]
    clock.advance(days=2)
    demo.erp.set_stock("BOLT-M8", on_hand=40.0)                                  # stock changed while the suggestion was waiting
    now_a, _ = demo.inventory.analyze_one("BOLT-M8")
    assert now_a.suggested_qty and now_a.suggested_qty != asked_then
    demo.approvals.decide(alert.approval_id, "approve", OWNER, "test")
    p = demo.workorders.list()[0]["payload"]
    assert p["figures_refreshed"] and p["on_hand"] == 40.0 and p["position"] == now_a.position
    assert p["suggested_qty"] == now_a.suggested_qty
    assert f"said {asked_then:,.0f}" in p["suggested_qty_basis"] and "read again when you approved it" in p["suggested_qty_basis"]
    assert p["alert_id"] == alert.id and p["approved_by"] == "owner" and p["approval_id"] == alert.approval_id       # who approved it is untouched


def test_if_stock_recovered_while_the_suggestion_waited_the_request_shows_todays_stock_and_keeps_the_old_quantity(demo, clock):
    demo.inventory.run()
    alert = alert_for(demo, "BOLT-M8")
    clock.advance(days=2)
    demo.erp.set_stock("BOLT-M8", on_hand=50000.0)
    demo.approvals.decide(alert.approval_id, "approve", OWNER, "test")
    p = demo.workorders.list()[0]["payload"]
    assert p["on_hand"] == 50000.0 and p["suggested_qty"] == alert.analysis["suggested_qty"]
    assert "check the stock position before you approve" in p["suggested_qty_basis"] and "no longer show a shortage" in p["reasons"][0]


def test_if_the_erp_cannot_be_read_when_a_reorder_is_approved_the_original_figures_are_used(demo, monkeypatch):
    demo.inventory.run()
    alert = alert_for(demo, "BOLT-M8")
    monkeypatch.setattr(demo.inventory, "analyze_one", lambda code: (_ for _ in ()).throw(RuntimeError("ERP down")))
    assert demo.approvals.decide(alert.approval_id, "approve", OWNER, "test").status == "decided"
    p = demo.workorders.list()[0]["payload"]
    assert "figures_refreshed" not in p and p["suggested_qty"] == alert.analysis["suggested_qty"]


# ------------------------------------------------------------------ the quiet period after "Create order" ends when that order is done
# Seen on the live system (24 Oct 2026): MEC-0025 was ordered on the 22nd and the order was received in full; a new sales order then pushed it below its limit, but
# "Check stock" stayed silent for the rest of the 3-day quiet period and the earlier "Got it" stock alert still counted. Nothing was on its way any more.
def ordered(demo, code="BOLT-M8", stock_alerts=False):
    """Alert -> approve the reorder -> the vendor check -> approve the purchase order: a draft PO exists in the ERP and the item is in its quiet period.
    With stock_alerts the information-only alert goes first and is answered with "Got it", as on the live system."""
    if stock_alerts:
        demo.settings.update_system(OWNER, {"stock_alerts": True})
    demo.inventory.run()
    if stock_alerts:
        assert demo.approvals.decide(demo.inventory.notices.latest_active(code).approval_id, "ack", OWNER, "test").ok
        assert demo.inventory.notices.latest_active(code).status == "acknowledged"                                  # still counted as "already told" until the item is back to normal
    demo.approvals.decide(pending_id(demo, code), "approve", OWNER, "test")
    demo.procurement.run()
    request = next(r for r in demo.approvals.list("pending", 100) if r.kind == "procurement.po" and r.request.facts.get("item_code") == code)
    assert demo.approvals.decide(request.id, "approve", OWNER, "test").ok
    po = demo.proposals.list(item_code=code)[0]
    assert po.status == "ordered" and po.po_name
    demo.erp.set_stock(code, incoming=0)                                    # as if the goods had arrived and been used again: short, with nothing on its way
    return po


def po_doc(po, **state):
    return {"doctype": "Purchase Order", "name": po.po_name, "status": "Completed", "docstatus": 1, "per_received": 100.0, "per_billed": 100.0, "lines": [], **state}


def test_a_new_shortage_after_the_order_arrived_is_told_at_once(demo, clock, monkeypatch):
    po = ordered(demo, stock_alerts=True)
    assert by_item(demo.inventory.run())["BOLT-M8"]["action"] == "skipped:ordered_recently"            # the draft order is still waiting to be submitted: stay quiet
    notices_before = len(demo.approvals.list(limit=500))
    monkeypatch.setattr(demo.erp, "get_documents", lambda *a, **k: [po_doc(po)])                       # received in full
    clock.advance(hours=3)
    report = demo.inventory.run()
    actions = [a["action"] for a in report.actions if a["item_code"] == "BOLT-M8"]
    assert actions == ["quiet_period_ended", "new_notice", "new_alert"]                                  # the stock alert first, then the reorder request, as always
    assert report.new_notices >= 1 and len(demo.approvals.list(limit=500)) == notices_before + 2
    assert demo.inventory.notices.latest_active("BOLT-M8").status == "sent" and alert_for(demo, "BOLT-M8").status == "awaiting"                      # a fresh stock alert, not the old acknowledged one
    assert by_item(demo.inventory.run())["BOLT-M8"]["action"] == "skipped:pending_approval"             # and only once


@pytest.mark.parametrize("state,why", [
    ({"status": "To Receive and Bill", "per_received": 40.0}, "partly received: the rest is still on its way"),
    ({"status": "To Receive and Bill", "per_received": 0.0}, "submitted and not received yet"),
    ({"status": "Draft", "docstatus": 0, "per_received": 0.0}, "a draft waiting to be submitted"),
])
def test_the_quiet_period_holds_while_any_of_the_order_is_still_on_its_way(demo, monkeypatch, state, why):
    po = ordered(demo)
    monkeypatch.setattr(demo.erp, "get_documents", lambda *a, **k: [po_doc(po, **state)])
    assert by_item(demo.inventory.run())["BOLT-M8"]["action"] == "skipped:ordered_recently", why


@pytest.mark.parametrize("state", [{"status": "Closed"}, {"status": "Cancelled", "docstatus": 2, "per_received": 0.0}, {"status": "To Bill", "per_received": 100.0}])
def test_a_closed_cancelled_or_fully_received_order_is_not_on_its_way(demo, monkeypatch, state):
    po = ordered(demo)
    monkeypatch.setattr(demo.erp, "get_documents", lambda *a, **k: [po_doc(po, **state)])
    assert "quiet_period_ended" in [a["action"] for a in demo.inventory.run().actions if a["item_code"] == "BOLT-M8"]


def test_an_order_that_was_deleted_in_the_erp_is_not_on_its_way(demo, monkeypatch):
    ordered(demo)
    monkeypatch.setattr(demo.erp, "get_documents", lambda *a, **k: [])
    assert "quiet_period_ended" in [a["action"] for a in demo.inventory.run().actions if a["item_code"] == "BOLT-M8"]


def test_when_the_erp_cannot_say_the_quiet_period_stays(demo, monkeypatch):
    ordered(demo)

    def broken(*a, **k):
        raise RuntimeError("ERPNext is down")
    monkeypatch.setattr(demo.erp, "get_documents", broken)
    assert by_item(demo.inventory.run())["BOLT-M8"]["action"] == "skipped:ordered_recently"


def test_without_an_order_the_quiet_period_stays_until_it_runs_out(demo, clock, monkeypatch):
    """Right after "Create order" the purchase order does not exist yet (the vendor check takes a few minutes): asking again then would double-order."""
    demo.inventory.run()
    demo.approvals.decide(pending_id(demo, "BOLT-M8"), "approve", OWNER, "test")
    monkeypatch.setattr(demo.erp, "get_documents", lambda *a, **k: [])                                  # nothing in the ERP yet
    assert by_item(demo.inventory.run())["BOLT-M8"]["action"] == "skipped:ordered_recently"
    assert not demo.procurement.order_settled("BOLT-M8", clock() - __import__("datetime").timedelta(days=9))
    assert not demo.procurement.order_settled("NO-SUCH-ITEM", clock() - __import__("datetime").timedelta(days=9))


def test_a_dry_run_says_it_would_end_the_quiet_period_and_changes_nothing(demo, monkeypatch):
    po = ordered(demo)
    monkeypatch.setattr(demo.erp, "get_documents", lambda *a, **k: [po_doc(po)])
    before = alert_for(demo, "BOLT-M8")
    report = demo.inventory.run(dry_run=True)
    assert "would_end_quiet_period" in [a["action"] for a in report.actions if a["item_code"] == "BOLT-M8"]
    after = alert_for(demo, "BOLT-M8")
    assert (after.status, after.cooldown_until) == (before.status, before.cooldown_until)


def test_an_order_from_an_earlier_reorder_does_not_end_a_later_quiet_period(demo, clock, monkeypatch):
    """The completed order must come from the reorder that started this quiet period, not from one months ago."""
    po = ordered(demo)
    tamper(demo, "proposals", {"id": po.id}, lambda r: {"created_at": "2026-01-01T00:00:00+00:00"})
    monkeypatch.setattr(demo.erp, "get_documents", lambda *a, **k: [po_doc(po)])
    assert by_item(demo.inventory.run())["BOLT-M8"]["action"] == "skipped:ordered_recently"

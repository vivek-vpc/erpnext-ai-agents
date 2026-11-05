"""Vendor selector: scoring maths, the proposal -> approval -> draft purchase order flow, failures, snooze/expiry, API."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import APPROVER, OWNER
from erp_agents.core.api.app import create_app
from erp_agents.packs.stock_procurement.settings import ProcurementSettings
from erp_agents.core.workorders import REORDER_NEEDED
from ai_munimji_engine.packs.stock_procurement.erp import VendorOffer
from erp_agents.core.erp.mock import MockERP
from erp_agents.packs.stock_procurement.procurement.scoring import Need, rank_vendors

TODAY = date(2026, 10, 16)


def offer(code, price, lead, on_time, credit, orders=10, listed=False, name=None):
    return VendorOffer(code, name or code, price, price, lead, on_time, orders, TODAY - timedelta(days=15), credit, listed)


def three():
    return [offer("A", 10, 3, 0.96, 30, listed=True, name="Alpha"), offer("B", 9, 9, 0.70, 15, name="Beta"), offer("C", 11, 2, 0.99, 0, name="Gamma")]


NEED = Need(qty=100, cover_days=6, fallback_lead_days=7, uom="kg")


# ------------------------------------------------------------------ scoring
def test_balanced_vendor_wins_and_scores_are_exact():
    r = rank_vendors(three(), NEED, ProcurementSettings())
    assert [v.supplier for v in r.vendors] == ["A", "C", "B"]
    a = r.vendors[0]
    assert a.total == 1000 and a.scores["cost"] == pytest.approx(90) and a.scores["delivery"] == pytest.approx(100 * 2 / 3)
    assert a.score == pytest.approx(0.4 * 90 + 0.3 * (200 / 3) + 0.2 * 96 + 0.1 * 100 + 3)           # +3 listed supplier bonus
    assert r.weights == {"cost": 40, "delivery": 30, "reliability": 20, "terms": 10}


def test_why_not_names_the_factors_that_actually_decided_it():
    r = rank_vendors(three(), NEED, ProcurementSettings())
    beta, gamma = r.vendors[2], r.vendors[1]
    assert beta.arrives_after_stockout and beta.why_not.lower().startswith("arrives too late (9 days, after you run out)")
    assert "70% vs 96%" in beta.why_not
    assert "costs 100.00 more in total (1,100.00 vs 1,000.00)" in gamma.why_not and "credit 0 days vs 30" in gamma.why_not.lower()


def test_weights_change_the_winner_and_are_normalised():
    cost_only = ProcurementSettings(weight_cost=100, weight_delivery=0, weight_reliability=0, weight_terms=0)
    assert rank_vendors(three(), NEED, cost_only).best.supplier == "B"                    # cheapest, even though it is late
    assert normalised(ProcurementSettings(weight_cost=2, weight_delivery=2, weight_reliability=0, weight_terms=0)) == {"cost": 50, "delivery": 50, "reliability": 0, "terms": 0}
    with pytest.raises(ValueError):
        ProcurementSettings(weight_cost=0, weight_delivery=0, weight_reliability=0, weight_terms=0)


def normalised(s):
    from erp_agents.packs.stock_procurement.procurement.scoring import normalised_weights
    return normalised_weights(s)


def test_missing_data_is_flagged_not_hidden():
    offers = [offer("A", 10, None, None, None, name="Alpha"), offer("B", 10, 4, 0.9, 0, name="Beta")]
    r = rank_vendors(offers, NEED, ProcurementSettings())
    alpha = next(v for v in r.vendors if v.supplier == "A")
    assert alpha.lead_assumed and alpha.lead_days == 7 and alpha.scores["reliability"] == 50
    assert any("assumed 7 days" in f for f in alpha.flags) and any("no receipts" in f for f in alpha.flags)


def test_unpriced_and_new_vendors():
    offers = three() + [VendorOffer("N", "NoPrice", listed=True), offer("Z", 5, 3, 0.9, 0, orders=0, name="Zero")]
    r = rank_vendors(offers, NEED, ProcurementSettings())
    nop = next(v for v in r.vendors if v.supplier == "N")
    assert not nop.eligible and "no price" in nop.excluded_reason and r.vendors[-1].supplier in ("N", "Z")
    strict = rank_vendors(offers, NEED, ProcurementSettings(exclude_new_vendors=True))
    assert not next(v for v in strict.vendors if v.supplier == "Z").eligible
    assert rank_vendors([offer("A", 1, 1, 1, 0)], NEED, ProcurementSettings(min_vendors=3)).notes[0].startswith("Only 1 vendor had enough")


def test_everyone_too_late_and_price_jump_are_called_out():
    late = [offer("A", 10, 9, 0.9, 0), offer("B", 10, 12, 0.9, 0)]
    assert "Every vendor would deliver after you run out" in " ".join(rank_vendors(late, NEED, ProcurementSettings()).notes)
    jumped = VendorOffer("J", "Jumpy", unit_price=13, avg_price=10, lead_time_days=3, on_time_rate=0.9, orders=4)
    assert any("30% above their average" in f for f in rank_vendors([jumped], NEED, ProcurementSettings()).vendors[0].flags)


# ------------------------------------------------------------------ the agent
def wo_payload(code="BOLT-M8", qty=500.0, **kw):
    return {"item_code": code, "item_name": "Bolt M8 x 40", "uom": "pcs", "suggested_qty": qty, "suggested_qty_basis": "40 pcs/day x 38 days - 300 already held",
            "cover_days": 6.0, "lead_time_days": 5.0, "daily_rate": 40.0, "on_hand": 240.0, "stockout_date": "2026-10-22",
            "severity": "HIGH", "reasons": ["Stock lasts about 6 days, at or below your minimum of 7 days."], **kw}


@pytest.fixture
def world(make_app, clock):
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("BOLT-M8", "Bolt M8 x 40", lambda age: 40, 240, group="Fasteners", uom="pcs", lead_time_days=5)
    erp.add_vendors("BOLT-M8", three())
    app = make_app(erp=erp)
    return app, erp


def proposed(world, **payload):
    app, erp = world
    app.workorders.emit(REORDER_NEEDED, wo_payload(**payload))
    report = app.procurement.run()
    return report, app.approvals.list(status="pending")


def test_dry_run_changes_nothing_but_shows_the_recommendation(world):
    app, _ = world
    app.workorders.emit(REORDER_NEEDED, wo_payload())
    rep = app.procurement.run(dry_run=True)
    assert rep.actions[0]["action"] == "would_propose" and rep.actions[0]["vendor"] == "Alpha"
    assert app.workorders.list(status="pending") and not app.approvals.list() and not app.proposals.list()


def test_proposal_carries_the_reasoning_the_owner_asked_for(world):
    report, pending = proposed(world)
    assert report.proposed == 1 and len(pending) == 1
    req = pending[0].request
    assert req.kind == "procurement.po" and req.headline == "Order 500 pcs of Bolt M8 x 40 from Alpha for 5,000.00."
    heads = [s.heading for s in req.sections]
    assert heads[:3] == ["Recommendation", "Stock position", "Why this vendor"] and "Other vendors" in heads
    text = " ".join(" ".join(s.lines) for s in req.sections)
    assert "Order 500 pcs of Bolt M8 x 40 from Alpha at 10.00 each: total 5,000.00" in text
    assert "On hand: 240 pcs" in text and "Beta: Arrives too late" in text and "Gamma: " in text
    assert "DRAFT purchase order" in text
    assert {o.action for o in req.options} == {"approve", "snooze", "dismiss", "requisition", "change_qty", "change_vendor", "details"}
    assert any("Every vendor" == s.heading for s in req.details)
    assert app_wo_status(world) == "claimed"


def app_wo_status(world):
    return world[0].workorders.list()[0]["status"]


def test_approval_creates_a_draft_po_and_closes_the_loop(world):
    app, erp = world
    _, pending = proposed(world)
    res = app.approvals.decide(pending[0].id, "approve", APPROVER, "whatsapp")
    assert res.ok and res.status == "decided"
    prop = app.proposals.list()[0]
    assert prop.status == "ordered" and prop.po_name == "PO-MOCK-0001" and "approved by manager via whatsapp" in prop.status_note
    po = erp.purchase_orders[0]
    assert (po["supplier"], po["item_code"], po["qty"], po["rate"]) == ("A", "BOLT-M8", 500, 10)
    assert po["required_by"] == app.clock().date() + timedelta(days=3) and "Review before submitting" in po["remarks"]
    assert erp.items["BOLT-M8"].stock.incoming == 500
    assert app_wo_status(world) == "done"
    assert app.procurement.run().considered == 0                           # nothing left to do


def test_erp_refusal_rolls_the_decision_back_so_the_owner_can_retry(world):
    app, erp = world
    _, pending = proposed(world)
    erp.fail_purchase_orders = True
    res = app.approvals.decide(pending[0].id, "approve", OWNER, "whatsapp")
    assert not res.ok and res.status == "error" and "mock ERP refused" in res.message and "try again" in res.message
    assert app.approvals.get(pending[0].id).status == "pending" and not erp.purchase_orders
    assert app.proposals.list()[0].status == "awaiting"
    erp.fail_purchase_orders = False
    assert app.approvals.decide(pending[0].id, "approve", OWNER, "whatsapp").ok and len(erp.purchase_orders) == 1


def test_po_mode_off_only_records_the_decision(world):
    app, erp = world
    app.settings.update_procurement(OWNER, {"po_mode": "off"})
    _, pending = proposed(world)
    assert "only records your decision" in " ".join(pending[0].request.sections[0].lines)
    app.approvals.decide(pending[0].id, "approve", OWNER, "app")
    assert app.proposals.list()[0].status == "ordered" and not erp.purchase_orders and app.proposals.list()[0].po_name is None


def test_reject_closes_the_reorder_without_ordering(world):
    app, erp = world
    _, pending = proposed(world)
    app.approvals.decide(pending[0].id, "dismiss", OWNER, "app")
    assert app.proposals.list()[0].status == "rejected" and app_wo_status(world) == "done" and not erp.purchase_orders


def test_snooze_then_ask_again_with_fresh_numbers(world, clock):
    app, erp = world
    _, pending = proposed(world)
    app.approvals.decide(pending[0].id, "snooze", OWNER, "app")
    assert app.proposals.list()[0].status == "snoozed" and app.procurement.run().considered == 0
    clock.advance(days=3)                                                    # default snooze is 2 days
    rep = app.procurement.run()
    assert rep.resumed == 1 and rep.proposed == 1
    assert {p.status for p in app.proposals.list()} == {"superseded", "awaiting"}


def test_unanswered_request_expires_and_is_asked_again(world, clock):
    app, _ = world
    proposed(world)
    clock.advance(hours=73)
    assert len(app.approvals.expire_due()) == 1
    assert app.proposals.list()[0].status == "expired" and app_wo_status(world) == "pending"
    assert app.procurement.run().proposed == 1


def test_second_reorder_for_the_same_item_waits_for_the_first_answer(world):
    app, _ = world
    proposed(world)
    app.workorders.emit(REORDER_NEEDED, wo_payload())
    rep = app.procurement.run()
    assert rep.actions[0]["action"] == "skipped:awaiting_answer" and len(app.approvals.list(status="pending")) == 1


# ------------------------------------------------------------------ when no recommendation is possible
def test_no_priced_vendor_asks_the_owner_to_decide_and_says_why(world):
    app, erp = world
    erp.add_vendors("BOLT-M8", [VendorOffer("N", "Newco", listed=True)])
    report, pending = proposed(world)
    assert report.manual == 1 and pending[0].request.kind == "procurement.manual"
    assert "None of the suppliers has a price on record" in pending[0].request.headline
    assert [o.action for o in pending[0].request.options] == ["ack", "snooze"]
    app.approvals.decide(pending[0].id, "ack", OWNER, "app")
    assert app.proposals.list()[0].status == "acknowledged" and app_wo_status(world) == "done"


def test_no_suppliers_at_all(world):
    app, erp = world
    erp.add_vendors("BOLT-M8", [])
    _, pending = proposed(world)
    assert "No supplier is listed for this item" in pending[0].request.headline


def test_missing_quantity_asks_unless_a_default_is_set(world):
    app, erp = world
    report, pending = proposed(world, qty=None)
    assert report.manual == 1 and "No order quantity could be worked out" in pending[0].request.headline
    app.approvals.decide(pending[0].id, "ack", OWNER, "app")
    app.settings.update_procurement(OWNER, {"fallback_qty": 250})
    report, pending = proposed(world, qty=None)
    assert report.proposed == 1 and pending[0].request.facts["qty"] == 250
    assert "default order size from your procurement settings" in " ".join(pending[0].request.sections[0].lines)


def test_one_failing_item_does_not_stop_the_others(world):
    app, erp = world
    erp.add_item("NUT-M8", "Nut M8", lambda age: 10, 50)
    erp.add_vendors("NUT-M8", three())
    real = erp.get_vendor_offers
    erp.get_vendor_offers = lambda code, since: (_ for _ in ()).throw(RuntimeError("ERP down")) if code == "BOLT-M8" else real(code, since)
    app.workorders.emit(REORDER_NEEDED, wo_payload("BOLT-M8"))
    app.workorders.emit(REORDER_NEEDED, wo_payload("NUT-M8"))
    rep = app.procurement.run()
    assert rep.proposed == 1 and rep.errors and "BOLT-M8: ERP down" in rep.errors[0]
    assert [w["status"] for w in app.workorders.list() if w["payload"]["item_code"] == "BOLT-M8"] == ["pending"]     # retried next run


# ------------------------------------------------------------------ full chain from the inventory agent
def test_inventory_approval_flows_into_a_vendor_proposal(make_app, clock):
    erp = MockERP(today=clock().date())
    app = make_app(erp=erp)
    app.inventory.run()
    bolt = next(r for r in app.approvals.list(status="pending") if r.request.facts.get("item_code") == "BOLT-M8")
    assert app.approvals.decide(bolt.id, "approve", OWNER, "whatsapp").ok
    rep = app.procurement.run()
    assert rep.proposed == 1 and rep.actions[0]["item_code"] == "BOLT-M8"
    po = next(r for r in app.approvals.list(status="pending") if r.kind == "procurement.po")
    assert po.request.facts["item_code"] == "BOLT-M8" and po.request.facts["supplier"] in ("SUP-PRIME", "SUP-BUDGET", "SUP-SWIFT")


# ------------------------------------------------------------------ settings + API
@pytest.fixture
def api(make_app, clock):
    cfg = {"auth": {"tokens": [{"token": "owner", "user": "owner", "roles": ["admin", "approver"]}, {"token": "view", "user": "acct", "roles": ["viewer"]}]}}
    app = make_app(erp=MockERP(today=clock().date()), cfg=cfg)
    return app, TestClient(create_app(app))


def H(t):
    return {"Authorization": f"Bearer {t}"}


def test_procurement_settings_api_validates_audits_and_restricts(api):
    app, c = api
    assert c.get("/api/v1/settings/procurement", headers=H("view")).json()["effective"]["weight_cost"] == 40
    assert c.put("/api/v1/settings/procurement", json={"set": {"weight_cost": 70}}, headers=H("view")).status_code == 403
    r = c.put("/api/v1/settings/procurement", json={"set": {"weight_cost": 70, "po_mode": "off"}}, headers=H("owner"))
    assert r.status_code == 200 and r.json()["effective"]["weight_cost"] == 70 and app.settings.procurement().po_mode == "off"
    assert c.put("/api/v1/settings/procurement", json={"set": {"po_mode": "submit"}}, headers=H("owner")).status_code == 422
    assert c.put("/api/v1/settings/procurement", json={"set": {"bogus": 1}}, headers=H("owner")).status_code == 422
    c.put("/api/v1/settings/procurement", json={"clear": ["po_mode"]}, headers=H("owner"))
    assert app.settings.procurement().po_mode == "draft"
    audit = c.get("/api/v1/settings/audit?scope_type=procurement", headers=H("view")).json()
    assert {a["field"] for a in audit} == {"weight_cost", "po_mode"} and audit[0]["actor"] == "owner"
    assert "weight_cost" in c.get("/api/v1/settings/schema", headers=H("view")).json()["procurement"]


def test_procurement_endpoints_dry_run_proposals_and_preview(api):
    app, c = api
    app.workorders.emit(REORDER_NEEDED, wo_payload("PAINT-RED"))
    assert c.post("/api/v1/procurement/run", headers=H("view")).status_code == 403
    dry = c.post("/api/v1/procurement/run", headers=H("owner")).json()
    assert dry["dry_run"] is True and dry["proposed"] == 1 and c.get("/api/v1/procurement/proposals", headers=H("view")).json() == []
    live = c.post("/api/v1/procurement/run?dry_run=false", headers=H("owner")).json()
    assert live["proposed"] == 1
    props = c.get("/api/v1/procurement/proposals?status=awaiting", headers=H("view")).json()
    assert len(props) == 1 and props[0]["item_code"] == "PAINT-RED" and props[0]["ranking"]["vendors"][0]["why_not"] == ""
    prev = c.get("/api/v1/procurement/preview/PAINT-RED?qty=100&cover_days=5", headers=H("view")).json()
    assert len(prev["vendors"]) == 3 and prev["weights"]["cost"] == 40
    assert c.get("/api/v1/procurement/preview/PAINT-RED?qty=0", headers=H("view")).status_code == 422

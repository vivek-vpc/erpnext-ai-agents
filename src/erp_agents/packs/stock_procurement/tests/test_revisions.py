"""Changing a request before it is answered: the quantity or the vendor of an order or requisition request, on any channel."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import APPROVER, OWNER
from erp_agents.core.api.app import create_app
from ai_munimji_engine.core.workorders import REORDER_NEEDED
from erp_agents.core.erp.mock import MockERP


def wo(code="BOLT-M8", qty=500):
    return {"item_code": code, "item_name": "Bolt M8 x 40", "item_group": "Fasteners", "uom": "pcs", "suggested_qty": qty, "cover_days": 10.0, "lead_time_days": 5.0, "daily_rate": 20.0,
            "on_hand": 200.0, "available": 200.0, "severity": "HIGH", "reasons": ["Low."], "stockout_date": "2026-10-26"}


@pytest.fixture
def world(make_app, clock):
    erp = MockERP(today=clock().date())
    app = make_app(erp=erp)
    return app, erp


def propose(app, **settings):
    if settings:
        app.settings.update_procurement(OWNER, settings, [])
    app.workorders.emit(REORDER_NEEDED, wo())
    assert app.procurement.run().proposed == 1
    return app.approvals.list("pending")[0]


def pending(app):
    return [r for r in app.approvals.list("pending", 50) if r.kind in ("procurement.po", "procurement.pr")]


def change(app, rec, qty=None, actor=OWNER, channel="telegram", **more):
    params = {**({"qty": qty} if qty is not None else {}), **more}
    return app.approvals.decide(rec.id, "change_qty", actor, channel, params)


# ------------------------------------------------------------------ the buttons exist, on orders and on requisitions
def test_an_order_request_offers_to_change_the_quantity_and_the_vendor_without_ending_the_request(world):
    app, _ = world
    rec = propose(app)
    opts = {o.action: o for o in rec.request.options}
    assert not opts["change_qty"].terminal and not opts["change_vendor"].terminal and opts["change_qty"].label == "Change quantity"
    assert [o.action for o in rec.request.options][:3] == ["approve", "snooze", "dismiss"]            # WhatsApp's template buttons are matched to these by position


# ------------------------------------------------------------------ change quantity
def test_without_a_number_the_person_is_asked_for_one_and_nothing_changes(world):
    app, _ = world
    rec = propose(app)
    res = change(app, rec)
    assert res.ok and res.status == "need_input" and "How many pcs of Bolt M8 x 40" in res.prompt and "500" in res.prompt
    assert [r.id for r in pending(app)] == [rec.id]


def test_a_new_quantity_replaces_the_request_with_a_revised_one_and_checks_the_vendors_again(world):
    app, erp = world
    old = propose(app)
    prop = app.proposals.by_approval(old.id)
    res = change(app, old, 800)
    assert res.ok and res.status == "revised" and "800" in res.message
    now = pending(app)
    assert len(now) == 1 and now[0].id != old.id and now[0].id == res.approval.id                       # exactly one request is waiting: the new one
    assert app.approvals.get(old.id).status == "cancelled" and "replaced by a revised request" in app.approvals.get(old.id).status_note
    req = now[0].request
    text = json.dumps(req.model_dump(mode="json"))
    assert "Quantity changed from 500 to 800 pcs by owner via telegram" in text and "checked again for the new quantity" in text
    assert req.facts["qty"] == 800 and req.facts["total"] == pytest.approx(req.facts["unit_price"] * 800)
    revised = app.proposals.get(prop.id)
    assert revised.qty == 800 and revised.approval_id == now[0].id and revised.status == "awaiting" and revised.total == pytest.approx(req.facts["total"])
    assert revised.ranking["revisions"] and "Quantity changed" in revised.ranking["revisions"][0]
    assert app.workorders.get(prop.work_order_id)["status"] == "claimed"                                  # not sent back to be proposed again
    assert not erp.purchase_orders                                                                          # nothing was ordered


def test_the_old_request_can_no_longer_be_answered_and_the_new_one_creates_the_order_for_the_new_quantity(world):
    app, erp = world
    old = propose(app)
    new = change(app, old, 800).approval
    stale = app.approvals.decide(old.id, "approve", OWNER, "telegram")
    assert not stale.ok and stale.status == "cancelled"
    assert not erp.purchase_orders
    done = app.approvals.decide(new.id, "approve", OWNER, "telegram")
    assert done.ok
    assert len(erp.purchase_orders) == 1 and erp.purchase_orders[0]["qty"] == 800 and erp.purchase_orders[0]["approved_by"] == "owner via telegram"
    assert app.proposals.by_approval(new.id).status == "ordered"


def test_a_second_change_builds_on_the_first(world):
    app, erp = world
    r1 = change(app, propose(app), 800).approval
    r2 = change(app, r1, 1200).approval
    assert [r.id for r in pending(app)] == [r2.id]
    assert "from 800 to 1,200" in json.dumps(r2.request.model_dump(mode="json")) or "from 800 to 1200" in json.dumps(r2.request.model_dump(mode="json"))
    assert len(app.proposals.by_approval(r2.id).ranking["revisions"]) == 2
    assert app.approvals.decide(r2.id, "approve", OWNER, "app").ok and erp.purchase_orders[0]["qty"] == 1200


@pytest.mark.parametrize("raw,reason", [("0", "more than zero"), ("-5", "more than zero"), ("abc", "not a number"), ("nan", "more than zero"), ("inf", "more than zero"),
                                        (500, "already for 500"), (60000, "more than 100 times")])
def test_a_quantity_that_makes_no_sense_is_refused_with_the_reason_and_nothing_changes(world, raw, reason):
    app, _ = world
    rec = propose(app)
    res = app.approvals.decide(rec.id, "change_qty", OWNER, "telegram", {"qty": raw})
    assert not res.ok and res.status == "refused" and reason in res.message
    assert [r.id for r in pending(app)] == [rec.id] and app.proposals.by_approval(rec.id).qty == 500


def test_a_quantity_below_the_erps_minimum_order_quantity_is_refused_before_it_can_fail_in_the_erp(world):
    app, erp = world
    erp.min_order_qty = {"BOLT-M8": 400}
    rec = propose(app)
    res = change(app, rec, 100)
    assert res.status == "refused" and "minimum order quantity of 400" in res.message
    assert change(app, rec, 400).status == "revised"


def test_a_suggested_quantity_below_the_minimum_order_quantity_is_raised_to_it_and_the_request_says_so(world):
    app, erp = world
    erp.min_order_qty = {"BOLT-M8": 400}
    app.workorders.emit(REORDER_NEEDED, wo(qty=100))
    assert app.procurement.run().proposed == 1
    (rec,) = pending(app)
    text = json.dumps(rec.request.model_dump(mode="json"))
    assert rec.request.facts["qty"] == 400 and "minimum order quantity set for this item in your ERP (the assistant's own figure was 100)" in text
    assert app.proposals.by_approval(rec.id).qty == 400
    app.approvals.decide(rec.id, "approve", OWNER, "console")
    assert [po["qty"] for po in erp.purchase_orders] == [400]


def test_a_request_below_the_minimum_cannot_be_approved_and_says_what_to_do(world):
    app, erp = world
    rec = propose(app)                                                              # 500 when the ERP had no minimum
    erp.min_order_qty = {"BOLT-M8": 800}                                            # the ERP's minimum went up after the request was made
    res = app.approvals.decide(rec.id, "approve", OWNER, "telegram")
    assert not res.ok and "minimum order quantity of 800 pcs" in res.message and "Change quantity" in res.message
    assert not erp.purchase_orders and [r.id for r in pending(app)] == [rec.id]


def test_commas_and_text_numbers_are_understood(world):
    app, _ = world
    rec = propose(app)
    assert change(app, rec, "1,250").status == "revised" and pending(app)[0].request.facts["qty"] == 1250


def test_a_requisition_request_can_be_changed_too(world):
    app, erp = world
    rec = propose(app, requisition_first_items=["BOLT-M8"])
    assert rec.kind == "procurement.pr"
    new = change(app, rec, 700).approval
    assert new.kind == "procurement.pr" and new.request.facts["qty"] == 700
    assert app.approvals.decide(new.id, "approve", OWNER, "app").ok and erp.requisitions[0]["qty"] == 700


def test_someone_who_may_not_decide_cannot_change_a_request(world):
    from conftest import VIEWER
    app, _ = world
    rec = propose(app)
    res = app.approvals.decide(rec.id, "change_qty", VIEWER, "app", {"qty": 800})
    assert not res.ok and res.status == "forbidden" and [r.id for r in pending(app)] == [rec.id]


def test_a_request_answered_in_the_meantime_is_not_revived(world):
    app, erp = world
    rec = propose(app)
    prop = app.proposals.by_approval(rec.id)
    assert app.approvals.decide(rec.id, "dismiss", OWNER, "app").ok
    assert app.approvals.supersede(rec.id, rec.request, "late") is None                        # the swap itself refuses
    res = change(app, rec, 800)
    assert not res.ok and res.status in ("already_decided", "cancelled", "refused")
    assert app.proposals.get(prop.id).qty == 500 and not pending(app)


def test_a_failed_swap_changes_nothing(world, monkeypatch):
    app, _ = world
    rec = propose(app)
    monkeypatch.setattr(app.approvals, "supersede", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    res = change(app, rec, 800)
    assert not res.ok and res.status == "error" and "unchanged" in res.message
    assert [r.id for r in pending(app)] == [rec.id] and app.proposals.by_approval(rec.id).qty == 500


# ------------------------------------------------------------------ change vendor
def test_asking_to_change_the_vendor_lists_the_other_vendors_that_were_compared(world):
    app, _ = world
    rec = propose(app)
    res = app.approvals.decide(rec.id, "change_vendor", OWNER, "telegram", {})
    assert res.ok and res.status == "need_input" and len(res.choices) == 2
    assert res.prompt.startswith("Which vendor instead?") and "1. " in res.prompt and "2. " in res.prompt and "(or cancel)" in res.prompt
    current = app.proposals.by_approval(rec.id).supplier
    assert current not in {c["id"] for c in res.choices} and all("in total" in c["label"] for c in res.choices)


def test_choosing_another_vendor_replaces_the_request_and_says_so(world):
    app, erp = world
    rec = propose(app)
    prop = app.proposals.by_approval(rec.id)
    pick = app.approvals.decide(rec.id, "change_vendor", OWNER, "telegram", {}).choices[0]
    res = app.approvals.decide(rec.id, "change_vendor", OWNER, "telegram", {"supplier": pick["id"]})
    assert res.ok and res.status == "revised"
    new = res.approval
    text = json.dumps(new.request.model_dump(mode="json"))
    assert f"Vendor changed from {prop.supplier_name} to " in text and "You chose this vendor instead of the assistant's first choice" in text
    assert new.request.facts["supplier"] == pick["id"] and new.request.facts["qty"] == 500
    assert "the assistant's first choice" in text and prop.supplier_name in text                        # the vendor it recommended is still named, not silently dropped
    assert app.proposals.get(prop.id).supplier == pick["id"]
    assert app.approvals.decide(new.id, "approve", OWNER, "app").ok and erp.purchase_orders[0]["supplier"] == pick["id"]


def test_a_vendor_that_was_not_compared_cannot_be_chosen(world):
    app, _ = world
    rec = propose(app)
    res = app.approvals.decide(rec.id, "change_vendor", OWNER, "telegram", {"supplier": "SUP-NOBODY"})
    assert res.status == "refused" and "not one of the vendors that were compared" in res.message and [r.id for r in pending(app)] == [rec.id]
    current = app.proposals.by_approval(rec.id).supplier
    assert app.approvals.decide(rec.id, "change_vendor", OWNER, "telegram", {"supplier": current}).status == "refused"             # the vendor it already is


def test_the_vendor_choice_and_the_quantity_change_work_together(world):
    app, erp = world
    r1 = change(app, propose(app), 900).approval
    pick = app.approvals.decide(r1.id, "change_vendor", OWNER, "app", {}).choices[-1]
    r2 = app.approvals.decide(r1.id, "change_vendor", OWNER, "app", {"supplier": pick["id"]}).approval
    assert r2.request.facts["qty"] == 900 and r2.request.facts["supplier"] == pick["id"]
    assert len(app.proposals.by_approval(r2.id).ranking["revisions"]) == 2
    assert app.approvals.decide(r2.id, "approve", OWNER, "app").ok
    assert erp.purchase_orders[0]["qty"] == 900 and erp.purchase_orders[0]["supplier"] == pick["id"]


# ------------------------------------------------------------------ through the console's API
def test_the_console_api_changes_a_request_and_explains_a_refusal(make_app, clock):
    erp = MockERP(today=clock().date())
    app = make_app(erp=erp, cfg={"auth": {"tokens": [{"token": "o" * 40, "user": "owner", "roles": ["viewer", "approver", "admin"]}]}})
    rec = propose(app)
    c = TestClient(create_app(app))
    h = {"Authorization": "Bearer " + "o" * 40}
    r = c.post(f"/api/v1/approvals/{rec.id}/decision", json={"action": "change_qty", "params": {"qty": 750}}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "revised" and r.json()["approval"]["request"]["facts"]["qty"] == 750
    new_id = r.json()["approval"]["id"]
    bad = c.post(f"/api/v1/approvals/{new_id}/decision", json={"action": "change_qty", "params": {"qty": 0}}, headers=h)
    assert bad.status_code == 422 and "more than zero" in bad.json()["detail"]
    ask = c.post(f"/api/v1/approvals/{new_id}/decision", json={"action": "change_vendor", "params": {}}, headers=h)
    assert ask.status_code == 200 and ask.json()["status"] == "need_input" and ask.json()["choices"]
    assert c.get(f"/api/v1/approvals/{rec.id}", headers=h).json()["status"] == "cancelled"

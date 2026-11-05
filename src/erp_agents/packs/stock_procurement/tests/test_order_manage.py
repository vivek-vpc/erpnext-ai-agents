"""Changing or taking back an order that already exists: the message with Change quantity / Change vendor / Cancel order that follows Create order,
the same things typed in chat, and the console. Nothing is removed or replaced without a yes, and only an order this application created."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import OWNER, VIEWER, FakeHTTP
from erp_agents.core.api.app import create_app
from erp_agents.core.approvals.channels.simple import InboxChannel
from erp_agents.core.approvals.channels.telegram import TelegramChannel
from erp_agents.core.approvals.channels.whatsapp import WhatsAppChannel
from erp_agents.core.approvals.inbound import process_inbound
from ai_munimji_engine.core.approvals.models import InboundEvent
from erp_agents.packs.stock_procurement.assistant.chat import understand, wants_manage
from ai_munimji_engine.core.workorders import REORDER_NEEDED
from erp_agents.core.erp.mock import MockERP

CHAT = "555"
PO_NUMBER = "PUR-ORD-2026-00001"


def wo(code="BOLT-M8", qty=500):
    return {"item_code": code, "item_name": "Bolt M8 x 40", "item_group": "Fasteners", "uom": "pcs", "suggested_qty": qty, "cover_days": 10.0, "lead_time_days": 5.0, "daily_rate": 20.0,
            "on_hand": 200.0, "available": 200.0, "severity": "HIGH", "reasons": ["Low."], "stockout_date": "2026-10-26"}


@pytest.fixture
def world(make_app, clock):
    http = FakeHTTP()
    channels = {"telegram": TelegramChannel("123:TOKEN", webhook_secret="s", http=http), "inbox": InboxChannel()}
    cfg = {"approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "telegram", "address": CHAT}, {"type": "inbox"}]}]},
           "auth": {"tokens": [{"token": "owner-tok", "user": "owner", "roles": ["admin", "approver"]}, {"token": "view-tok", "user": "accountant", "roles": ["viewer"]}]}}
    app = make_app(erp=MockERP(today=clock().date()), channels=channels, cfg=cfg)
    return app, http


def press(app, rec, action, **params):
    return process_inbound(InboundEvent(channel="telegram", sender=CHAT, approval_id=rec.id, action=action, params=params, ack_token="cq1"), app.router, app.approvals, app.conversation)


def say(app, text):
    process_inbound(InboundEvent(channel="telegram", sender=CHAT, text=text), app.router, app.approvals, app.conversation)


def sent_after(http, since):
    """What reached the chat after `since`, in order, named by what each message is."""
    names = []
    for url, body, _ in http.calls[since:]:
        if url.endswith("/answerCallbackQuery"):
            continue
        if not url.endswith("/sendMessage"):
            continue
        text, markup = body.get("text", ""), str(body.get("reply_markup", ""))
        if "Cancel order" in markup:
            names.append("MANAGE")
        elif "Create order" in markup:
            names.append("REQUEST")
        elif text.startswith("Cancelled"):
            names.append("CANCELLED")
        elif text.startswith("Done"):
            names.append("DONE")
        else:
            names.append("OTHER: " + text)
    return names


def texts_after(http, since):
    return [b.get("text", "") for url, b, _ in http.calls[since:] if url.endswith("/sendMessage")]


def pending(app, kind):
    return [r for r in app.approvals.list("pending", 100) if r.kind == kind]


def make_order(app, name=None):
    """Alert-less path: a reorder request, the vendor check, Create order pressed on Telegram. Returns (the request, the PO in the ERP, the message with the buttons).
    `name`: the number the ERP gives the order, in the ERP's own format, so that it can be typed in chat."""
    if name:
        app.erp.po_name_format = name.rsplit("-", 1)[0] + "-{:05d}"
    app.workorders.emit(REORDER_NEEDED, wo())
    assert app.procurement.run().proposed == 1
    rec = pending(app, "procurement.po")[0]
    assert press(app, rec, "approve").status == "decided"
    (po,) = app.erp.purchase_orders
    (manage,) = pending(app, "procurement.order")
    return rec, po, manage


def proposal(app, rec):
    return app.proposals.by_approval(rec.id)


# ================================================================== the message after Create order
def test_create_order_is_followed_by_the_message_with_the_buttons(world):
    app, http = world
    before = len(http.calls)
    rec, po, manage = make_order(app)
    assert sent_after(http, before)[-1] == "MANAGE"                                       # the last message in the chat, where the person is looking
    markup = str(next(b["reply_markup"] for u, b, _ in reversed(http.calls) if u.endswith("/sendMessage") and "Cancel order" in str(b.get("reply_markup", ""))))
    for label in ("Change quantity", "Change vendor", "Cancel order", "Keep it"):
        assert label in markup
    assert po["name"] in manage.request.title and manage.request.facts["po_name"] == po["name"]
    assert (proposal(app, rec).ranking or {})["order_manage_approval"] == manage.id


def test_nothing_is_offered_when_no_order_was_created(world):
    app, http = world
    app.settings.update_procurement(OWNER, {"po_mode": "off"}, [])
    app.workorders.emit(REORDER_NEEDED, wo())
    app.procurement.run()
    rec = pending(app, "procurement.po")[0]
    assert press(app, rec, "approve").status == "decided"
    assert not app.erp.purchase_orders and not pending(app, "procurement.order")


def test_a_requisition_has_no_buttons_to_change_or_cancel_it(world):
    app, http = world
    app.workorders.emit(REORDER_NEEDED, wo())
    app.procurement.run()
    rec = pending(app, "procurement.po")[0]
    assert press(app, rec, "requisition").status == "decided"
    assert not pending(app, "procurement.order")


def test_keep_it_closes_the_message_and_changes_nothing(world):
    app, http = world
    rec, po, manage = make_order(app)
    assert press(app, manage, "keep").status == "decided"
    assert app.erp.purchase_orders == [po] and proposal(app, rec).status == "ordered"
    assert not pending(app, "procurement.order")


# ================================================================== cancel
def test_cancel_asks_first_and_a_typed_yes_deletes_the_draft(world):
    app, http = world
    rec, po, manage = make_order(app)
    res = press(app, manage, "cancel_order")
    assert res.status == "need_input" and "Reply yes to cancel it, or no to keep it" in res.prompt and po["name"] in res.prompt and "deleted in ERPNext" in res.prompt
    assert app.erp.purchase_orders == [po]                                               # asking removes nothing
    before = len(http.calls)
    say(app, "yes")
    assert app.erp.purchase_orders == []
    assert proposal(app, rec).status == "cancelled" and app.approvals.get(manage.id).status == "cancelled"
    reply = texts_after(http, before)
    assert any(t.startswith("Done") and po["name"] in t and "was deleted in ERPNext" in t for t in reply)


@pytest.mark.parametrize("word", ["Yes", "y", "yeah", "OK", "confirm"])
def test_other_ways_of_saying_yes(world, word):
    app, http = world
    make_order(app)
    manage = pending(app, "procurement.order")[0]
    press(app, manage, "cancel_order")
    say(app, word)
    assert app.erp.purchase_orders == []


@pytest.mark.parametrize("word", ["no", "No.", "nope", "keep it"])
def test_no_keeps_the_order(world, word):
    app, http = world
    rec, po, manage = make_order(app)
    press(app, manage, "cancel_order")
    before = len(http.calls)
    say(app, word)
    assert app.erp.purchase_orders == [po] and proposal(app, rec).status == "ordered"
    assert any("is kept as it is" in t or "nothing was changed" in t for t in texts_after(http, before))
    assert app.approvals.get(manage.id).status == "pending"                                # the buttons are still there


def test_anything_else_after_the_question_is_a_new_question_and_cancels_nothing(world):
    app, http = world
    rec, po, manage = make_order(app)
    press(app, manage, "cancel_order")
    say(app, "what is waiting for my OK?")
    assert app.erp.purchase_orders == [po] and proposal(app, rec).status == "ordered"


def test_cancelling_a_submitted_order_cancels_it_in_the_erp(world):
    app, http = world
    rec, po, manage = make_order(app)
    po["docstatus"], po["status"] = 1, "To Receive and Bill"
    res = press(app, manage, "cancel_order")
    assert "cancelled in ERPNext" in res.prompt
    say(app, "yes")
    assert po["docstatus"] == 2 and proposal(app, rec).status == "cancelled"


def test_an_order_with_goods_received_is_not_cancelled(world):
    app, http = world
    rec, po, manage = make_order(app)
    po.update(docstatus=1, per_received=40.0)
    res = press(app, manage, "cancel_order")
    assert res.status == "refused" and "40% of it has been received" in res.message
    assert app.erp.purchase_orders == [po] and proposal(app, rec).status == "ordered"


def test_when_the_erp_refuses_nothing_is_marked_cancelled(world):
    app, http = world
    rec, po, manage = make_order(app)
    app.erp.refuse_cancel = "ERPNext does not allow the service account to delete a purchase order (Permission denied)"
    press(app, manage, "cancel_order")
    before = len(http.calls)
    say(app, "yes")
    assert app.erp.purchase_orders == [po] and proposal(app, rec).status == "ordered" and app.approvals.get(manage.id).status == "pending"
    assert any("ERPNext did not cancel" in t and "Permission denied" in t and "Nothing was changed" in t for t in texts_after(http, before))


def test_an_order_already_deleted_in_the_erp_is_synced_not_an_error(world):
    app, http = world
    rec, po, manage = make_order(app)
    app.erp.purchase_orders.clear()
    res = press(app, manage, "cancel_order")
    assert res.status == "refused" and "no longer in ERPNext" in res.message
    assert proposal(app, rec).status == "cancelled" and app.approvals.get(manage.id).status == "cancelled"


def test_buttons_of_an_order_that_changed_since_are_refused(world):
    app, http = world
    rec, po, manage = make_order(app)
    app.proposals.update_status(proposal(app, rec).id, "cancelled", "test")
    res = press(app, manage, "cancel_order")
    assert res.status == "refused" and "has been changed, replaced or cancelled since this message" in res.message
    assert app.erp.purchase_orders == [po]


def test_personal_login_required_means_cancel_from_the_console(world):
    app, http = world
    rec, po, manage = make_order(app)
    app.settings.update_procurement(OWNER, {"require_personal_login": True}, [])
    press(app, manage, "cancel_order")
    before = len(http.calls)
    say(app, "yes")
    assert app.erp.purchase_orders == [po]
    assert any("cancel it in the console" in t for t in texts_after(http, before))


def test_cancelling_ends_the_quiet_period_at_once(make_app, clock):
    from test_agent import by_item, ordered
    demo = make_app(erp=MockERP(today=clock().date()))
    po = ordered(demo)
    manage = pending(demo, "procurement.order")[0]
    assert demo.approvals.decide(manage.id, "cancel_order", OWNER, "test", {"confirm": "yes"}).status == "decided"
    assert demo.erp.purchase_orders == [] and demo.proposals.get(po.id).status == "cancelled"
    assert demo.alerts.latest_for_item("BOLT-M8").cooldown_until <= clock()
    assert by_item(demo.inventory.run())["BOLT-M8"]["action"] == "new_alert"                  # short again with nothing on its way: told at once


# ================================================================== Change quantity = the same order, a new quantity
# Reported 24 Oct 2026: changing the quantity of a submitted order deleted it and made a new one. The quantity is now changed in that same order.
def test_change_quantity_changes_the_same_order(world):
    app, http = world
    rec, po, manage = make_order(app)
    asked = press(app, manage, "change_qty")
    assert asked.status == "need_input" and "changed in this same order" in asked.prompt and po["name"] in asked.prompt
    before = len(http.calls)
    say(app, "800")
    (same,) = app.erp.purchase_orders
    assert same is po and same["name"] == po["name"] and same["qty"] == 800                # same number, nothing deleted or re-created
    prop = proposal(app, rec)
    assert prop.status == "ordered" and prop.po_name == po["name"] and prop.qty == 800 and prop.total == round(800 * po["rate"], 2)
    assert not pending(app, "procurement.po")                                              # no revised request, nothing to approve again
    names = sent_after(http, before)
    assert names[-1] == "MANAGE" and "DONE" in names                                       # "Done: ... is now for 800", then the buttons for the changed order
    (fresh,) = pending(app, "procurement.order")
    assert fresh.id != manage.id and fresh.request.facts["qty"] == 800 and app.approvals.get(manage.id).status == "cancelled"
    assert any("is now for 800 pcs (was 500)" in t and "Same order" in t for t in texts_after(http, before))


def test_change_quantity_of_a_submitted_order_keeps_it_submitted(world):
    app, http = world
    rec, po, manage = make_order(app)
    po.update(docstatus=1, status="To Receive and Bill")
    res = press(app, manage, "change_qty", qty=750)
    assert res.ok and "Purchase order" in res.message and "tell them about the change" in res.message
    assert app.erp.purchase_orders == [po] and po["docstatus"] == 1 and po["qty"] == 750
    assert pending(app, "procurement.order")[0].request.title.startswith("Purchase order ")


def test_change_quantity_of_a_partly_received_order_but_not_below_what_arrived(world):
    app, http = world
    rec, po, manage = make_order(app)
    po.update(docstatus=1, per_received=40.0)                                              # 200 of 500 received
    res = press(app, manage, "change_qty", qty=100)
    assert res.status == "refused" and "200 have already been received" in res.message and po["qty"] == 500
    manage = pending(app, "procurement.order")[0]
    assert press(app, manage, "change_qty", qty=600).ok and po["qty"] == 600


@pytest.mark.parametrize("qty,why", [("0", "more than zero"), ("abc", "is not a number"), ("500", "already for 500"), ("90000", "more than 100 times")])
def test_change_quantity_refuses_bad_numbers(world, qty, why):
    app, http = world
    rec, po, manage = make_order(app)
    res = press(app, manage, "change_qty", qty=qty)
    assert res.status == "refused" and why in res.message and po["qty"] == 500


def test_when_the_erp_refuses_the_quantity_change_nothing_is_changed(world):
    app, http = world
    rec, po, manage = make_order(app)
    app.erp.refuse_update = "ERPNext does not allow the service account to change a purchase order (Permission denied)"
    res = press(app, manage, "change_qty", qty=800)
    assert res.status == "refused" and "Permission denied" in res.message and "Nothing was changed" in res.message
    assert po["qty"] == 500 and proposal(app, rec).qty == 500 and app.approvals.get(manage.id).status == "pending"


def test_quantity_change_from_the_console(world):
    app, http = world
    rec, po, manage = make_order(app)
    c = TestClient(create_app(app))
    r = c.post(f"/api/v1/approvals/{manage.id}/decision", json={"action": "change_qty", "params": {"qty": 650}}, headers={"Authorization": "Bearer owner-tok"})
    assert r.status_code == 200 and "is now for 650" in r.json()["message"] and po["qty"] == 650 and len(app.erp.purchase_orders) == 1


# ================================================================== Change vendor = replace by a revised request (an order's vendor cannot be changed)
def test_change_vendor_sends_a_revised_request_last_and_leaves_the_order_alone(world):
    app, http = world
    rec, po, manage = make_order(app)
    choice = press(app, manage, "change_vendor")
    assert choice.status == "need_input" and choice.choices
    before = len(http.calls)
    say(app, "1")
    assert sent_after(http, before)[-1] == "REQUEST"
    (new,) = pending(app, "procurement.po")
    prop = proposal(app, new)
    assert new.request.facts["supplier"] == choice.choices[0]["id"] and prop.status == "awaiting" and (prop.ranking or {})["replaces_po"] == po["name"]
    assert app.erp.purchase_orders == [po]                                                 # nothing changes until Create order is pressed on the revised request
    assert "This replaces draft purchase order" in " ".join(l for s in new.request.sections for l in s.lines)
    assert "requisition" not in [o.action for o in new.request.options]
    assert app.approvals.get(manage.id).status == "cancelled"


def revised(app):
    rec, po, manage = make_order(app)
    choice = press(app, manage, "change_vendor")
    press(app, manage, "change_vendor", supplier=choice.choices[0]["id"])
    (new,) = pending(app, "procurement.po")
    return rec, po, new


def test_creating_the_revised_order_replaces_the_old_one(world):
    app, http = world
    rec, po, new = revised(app)
    before = len(http.calls)
    assert press(app, new, "approve").status == "decided"
    (made,) = app.erp.purchase_orders
    assert made["name"] != po["name"] and made["supplier"] == new.request.facts["supplier"] and made["reference"].startswith(proposal(app, new).id + "-")
    prop = proposal(app, new)
    assert prop.status == "ordered" and prop.po_name == made["name"] and (prop.ranking or {}).get("replaces_po") is None and po["name"] in str((prop.ranking or {}).get("replaced"))
    assert sent_after(http, before)[-1] == "MANAGE"                                        # and the new order can be changed or cancelled in turn
    assert pending(app, "procurement.order")[0].request.facts["po_name"] == made["name"]


@pytest.mark.parametrize("action", ["dismiss", "snooze"])
def test_not_creating_the_revised_order_keeps_the_old_one(world, action):
    app, http = world
    rec, po, new = revised(app)
    assert press(app, new, action).status == "decided"
    prop = proposal(app, new)
    assert prop.status == "ordered" and prop.po_name == po["name"] and app.erp.purchase_orders == [po]


def test_a_revised_request_that_expires_keeps_the_old_one(world, clock):
    app, http = world
    rec, po, new = revised(app)
    clock.advance(days=30)
    assert app.approvals.expire_due()
    prop = proposal(app, new)
    assert prop.status == "ordered" and prop.po_name == po["name"] and app.erp.purchase_orders == [po]


def test_a_revised_request_that_is_cancelled_keeps_the_old_one(world):
    app, http = world
    rec, po, new = revised(app)
    assert app.approvals.cancel(new.id, "test")
    prop = proposal(app, new)
    assert prop.status == "ordered" and prop.po_name == po["name"] and app.erp.purchase_orders == [po]


def test_if_the_old_order_was_received_meanwhile_nothing_is_replaced(world):
    app, http = world
    rec, po, new = revised(app)
    po.update(docstatus=1, per_received=100.0)
    res = press(app, new, "approve")
    assert res.status == "refused" and "100% of it has been received" in res.message and "Press Reject on this request to keep the earlier order" in res.message
    assert app.erp.purchase_orders == [po]                                                 # the old order is untouched and no second one was made
    assert app.approvals.get(new.id).status == "pending" and proposal(app, new).status == "awaiting"      # the answer was not recorded: Reject is still possible
    assert press(app, new, "dismiss").status == "decided" and proposal(app, new).po_name == po["name"] and proposal(app, new).status == "ordered"


def test_if_the_erp_will_not_remove_the_old_order_nothing_is_replaced(world):
    app, http = world
    rec, po, new = revised(app)
    app.erp.refuse_cancel = "Permission denied"
    res = press(app, new, "approve")
    assert res.status == "refused" and "Permission denied" in res.message
    assert app.erp.purchase_orders == [po] and app.approvals.get(new.id).status == "pending"


def test_if_the_old_order_is_already_gone_the_new_one_is_still_created(world):
    app, http = world
    rec, po, new = revised(app)
    app.erp.purchase_orders.clear()
    assert press(app, new, "approve").status == "decided"
    (made,) = app.erp.purchase_orders
    assert made["supplier"] == new.request.facts["supplier"] and proposal(app, new).po_name == made["name"]


# ================================================================== in chat
@pytest.mark.parametrize("text,kind", [
    ("cancel PO PUR-ORD-2026-00012", "cancel"), ("please cancel the order for bolt-m8", "cancel"), ("can you delete purchase order pur-ord-2026-00012?", "cancel"),
    ("withdraw the po for bolt-m8", "cancel"), ("remove order pur-ord-2026-00012", "cancel"),
    ("edit po pur-ord-2026-00012", "edit"), ("change the quantity of po pur-ord-2026-00012", "edit"), ("i want to modify the order for bolt-m8", "edit"),
    ("update purchase order pur-ord-2026-00012", "edit"), ("amend the order for bolt-m8", "edit"),
])
def test_instructions_to_cancel_or_edit_an_order_are_recognised(text, kind):
    assert wants_manage(text.lower()) == kind


@pytest.mark.parametrize("text", [
    "what happens if i cancel the order?", "which orders were cancelled?", "status of po pur-ord-2026-00012", "update me on the status of po pur-ord-2026-00012", "cancel",
    "cancel my subscription", "change the vendor", "how do i cancel an order?", "create a purchase order for bolt-m8 for 500", "why was the order cancelled", "show cancelled purchase orders",
])
def test_questions_and_other_requests_are_not_taken_for_instructions(text):
    assert wants_manage(text) is None
    assert understand(text, []).intent != "order_manage"


def test_the_rules_route_the_instruction_to_order_manage():
    assert understand("cancel po pur-ord-2026-00012", []).intent == "order_manage"
    assert understand("edit the order for bolt-m8", []).manage == "edit"


def test_cancel_in_telegram_chat_asks_then_a_yes_cancels(world):
    app, http = world
    rec, po, manage = make_order(app, PO_NUMBER)
    before = len(http.calls)
    say(app, f"cancel PO {PO_NUMBER}")
    ask = texts_after(http, before)
    assert any(PO_NUMBER in t and "Do you want to cancel it? Reply yes or no" in t for t in ask) and app.erp.purchase_orders == [po]
    say(app, "yes")
    assert app.erp.purchase_orders == [] and proposal(app, rec).status == "cancelled"


def test_cancel_by_item_in_telegram_chat(world):
    app, http = world
    rec, po, manage = make_order(app)
    say(app, "cancel the order for BOLT-M8")
    say(app, "yes")
    assert app.erp.purchase_orders == []


def test_edit_in_telegram_chat_puts_the_buttons_in_front_of_the_person(world):
    app, http = world
    rec, po, manage = make_order(app, PO_NUMBER)
    before = len(http.calls)
    say(app, f"edit PO {PO_NUMBER}")
    names = sent_after(http, before)
    assert names[-1] == "MANAGE" and any(PO_NUMBER in t and "change the quantity or the vendor" in t for t in texts_after(http, before))
    assert app.erp.purchase_orders == [po]


def test_chat_will_not_touch_an_order_it_did_not_create(world):
    app, http = world
    rec, po, manage = make_order(app, PO_NUMBER)
    before = len(http.calls)
    say(app, "cancel PO PUR-ORD-2026-09999")
    assert any("I can only cancel purchase orders that I created" in t for t in texts_after(http, before)) and app.erp.purchase_orders == [po]
    say(app, "yes")                                                                         # nothing was asked, so a stray yes is just a message
    assert app.erp.purchase_orders == [po]
    before = len(http.calls)
    say(app, "cancel MAT-MR-2026-00001")
    assert any("PUR-ORD" in t for t in texts_after(http, before)) and app.erp.purchase_orders == [po]


def test_chat_says_which_order_when_it_is_not_named(world):
    app, http = world
    out = app.chat.ask("cancel the order", "owner", True)
    assert out["status"] == "clarify" and "Which order" in out["answer"] and "expect" not in out


def test_someone_who_cannot_approve_cannot_cancel_from_chat(world):
    app, http = world
    rec, po, manage = make_order(app, PO_NUMBER)
    out = app.chat.ask(f"cancel PO {PO_NUMBER}", "accountant", False)
    assert "Only people who can approve orders can cancel one" in out["answer"] and "expect" not in out and app.erp.purchase_orders == [po]


def test_the_console_chat_returns_the_question_to_confirm(world):
    app, http = world
    rec, po, manage = make_order(app, PO_NUMBER)
    out = app.chat.ask(f"cancel PO {PO_NUMBER}", "owner", True)
    assert out["status"] == "confirm" and out["expect"]["action"] == "cancel_order" and out["expect"]["approval_id"] == manage.id and app.erp.purchase_orders == [po]
    app.settings.update_section(OWNER, "assistant", {"enabled": True})
    assert app.chat.explain(f"cancel PO {PO_NUMBER}", "owner", True)["status"] == "not_enough_information"      # explaining never opens the buttons or asks anything


# ================================================================== console API
def test_console_cancel_needs_a_confirm_and_the_right_role(world):
    app, http = world
    rec, po, manage = make_order(app)
    c = TestClient(create_app(app))
    h = lambda t: {"Authorization": f"Bearer {t}"}                                       # noqa: E731
    assert c.post(f"/api/v1/approvals/{manage.id}/decision", json={"action": "cancel_order"}, headers=h("view-tok")).status_code == 403
    asked = c.post(f"/api/v1/approvals/{manage.id}/decision", json={"action": "cancel_order", "params": {}}, headers=h("owner-tok"))
    assert asked.status_code == 200 and asked.json()["status"] == "need_input" and [x["id"] for x in asked.json()["choices"]] == ["yes", "no"] and app.erp.purchase_orders == [po]
    no = c.post(f"/api/v1/approvals/{manage.id}/decision", json={"action": "cancel_order", "params": {"confirm": "no"}}, headers=h("owner-tok"))
    assert no.status_code == 200 and app.erp.purchase_orders == [po]
    yes = c.post(f"/api/v1/approvals/{manage.id}/decision", json={"action": "cancel_order", "params": {"confirm": "yes"}}, headers=h("owner-tok"))
    assert yes.status_code == 200 and yes.json()["status"] == "decided" and "was deleted in ERPNext" in yes.json()["message"] and app.erp.purchase_orders == []
    again = c.post(f"/api/v1/approvals/{manage.id}/decision", json={"action": "cancel_order", "params": {"confirm": "yes"}}, headers=h("owner-tok"))
    assert again.status_code == 410                                                      # the buttons are closed


def test_console_refusal_is_a_422_with_the_reason(world):
    app, http = world
    rec, po, manage = make_order(app)
    po.update(docstatus=1, per_received=100.0)
    c = TestClient(create_app(app))
    r = c.post(f"/api/v1/approvals/{manage.id}/decision", json={"action": "cancel_order", "params": {"confirm": "yes"}}, headers={"Authorization": "Bearer owner-tok"})
    assert r.status_code == 422 and "received" in r.json()["detail"] and app.erp.purchase_orders == [po]


# ================================================================== WhatsApp
def test_whatsapp_never_sends_the_default_template_for_this_message(make_app, clock):
    """The stock pack tells every channel that supports it that this message carries its own buttons (a plain message); a template the settings name for it still wins."""
    ch = WhatsAppChannel("T", "PNID", app_secret="s", verify_token="v", template_name="stock_approval", http=FakeHTTP())
    assert ch._template_for(SimpleNamespace(kind="procurement.order")) == "stock_approval"             # a bare channel knows nothing about this kind
    ch.plain_only("procurement.order")
    assert ch._template_for(SimpleNamespace(kind="procurement.order")) is None             # its buttons are not the template's (Create order / Remind me / Reject)
    assert ch._template_for(SimpleNamespace(kind="procurement.po")) == "stock_approval"
    named = WhatsAppChannel("T", "PNID", app_secret="s", verify_token="v", template_name="stock_approval", kind_templates={"procurement.order": "order_buttons"}, http=FakeHTTP())
    named.plain_only("procurement.order")
    assert named._template_for(SimpleNamespace(kind="procurement.order")) == "order_buttons"
    live = WhatsAppChannel("T", "PNID", app_secret="s", verify_token="v", template_name="stock_approval", http=FakeHTTP())
    make_app(erp=MockERP(today=clock().date()), channels={"whatsapp": live, "inbox": InboxChannel()})
    assert live._template_for(SimpleNamespace(kind="procurement.order")) is None                 # and the pack did that when the application started

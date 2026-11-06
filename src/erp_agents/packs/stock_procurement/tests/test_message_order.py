"""Changing a request on Telegram: the revised request is the LAST message, after "Done: ... a revised request is on its way" (seen on the live Telegram, 24 Oct 2026)."""
import pytest

from conftest import OWNER, FakeHTTP
from erp_agents.core.approvals.channels.simple import InboxChannel
from erp_agents.core.approvals.channels.telegram import TelegramChannel
from erp_agents.core.approvals.inbound import process_inbound
from ai_munimji_engine.core.approvals.models import InboundEvent
from ai_munimji_engine.core.workorders import REORDER_NEEDED
from erp_agents.core.erp.mock import MockERP
from conftest import table_rows

CHAT = "555"


def wo(code="BOLT-M8", qty=500):
    return {"item_code": code, "item_name": "Bolt M8 x 40", "item_group": "Fasteners", "uom": "pcs", "suggested_qty": qty, "cover_days": 10.0, "lead_time_days": 5.0, "daily_rate": 20.0,
            "on_hand": 200.0, "available": 200.0, "severity": "HIGH", "reasons": ["Low."], "stockout_date": "2026-10-26"}


@pytest.fixture
def world(make_app, clock):
    http = FakeHTTP()
    channels = {"telegram": TelegramChannel("123:TOKEN", webhook_secret="s", http=http), "inbox": InboxChannel()}
    cfg = {"approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "telegram", "address": CHAT}, {"type": "inbox"}]}]}}
    app = make_app(erp=MockERP(today=clock().date()), channels=channels, cfg=cfg)
    app.workorders.emit(REORDER_NEEDED, wo())
    assert app.procurement.run().proposed == 1
    rec = next(r for r in app.approvals.list("pending") if r.kind == "procurement.po")
    return app, http, rec


def sent_after(http, since):
    """What reached the chat after `since`, in order, named by what each message is."""
    names = []
    for url, body, _ in http.calls[since:]:
        if url.endswith("/answerCallbackQuery") and body.get("text", "").startswith("Done"):
            names.append("DONE")                                                          # a button press is answered with a pop-up, not a chat message
            continue
        if not url.endswith("/sendMessage"):
            continue
        text = body.get("text", "")
        if "reply_markup" in body and "Create order" in str(body["reply_markup"]):
            names.append("REQUEST")
        elif text.startswith("Cancelled"):
            names.append("CANCELLED")
        elif text.startswith("Done"):
            names.append("DONE")
        else:
            names.append("OTHER: " + text[:40])
    return names


def press(app, rec, action, **params):
    return process_inbound(InboundEvent(channel="telegram", sender=CHAT, approval_id=rec.id, action=action, params=params, ack_token="cq1"), app.router, app.approvals, app.conversation)


def test_pressing_a_button_that_changes_the_request_sends_the_revised_request_last(world):
    app, http, rec = world
    before = len(http.calls)
    res = press(app, rec, "change_qty", qty=800)
    assert res.status == "revised"
    assert sent_after(http, before) == ["CANCELLED", "DONE", "REQUEST"]                    # the new request, with its buttons, is the last message in the chat
    new = next(r for r in app.approvals.list("pending") if r.kind == "procurement.po")
    assert new.id != rec.id and new.request.facts["qty"] == 800


def test_choosing_another_vendor_does_the_same(world):
    app, http, rec = world
    options = press(app, rec, "change_vendor")
    assert options.status == "need_input" and options.choices
    before = len(http.calls)
    press(app, rec, "change_vendor", supplier=options.choices[0]["id"])
    assert sent_after(http, before) == ["CANCELLED", "DONE", "REQUEST"]


def test_typing_the_new_quantity_in_reply_to_the_request_does_the_same(world):
    app, http, rec = world
    external_id = next(d for d in app.stores.approvals.deliveries(rec.id) if d["channel"] == "telegram")["external_id"]
    before = len(http.calls)
    process_inbound(InboundEvent(channel="telegram", sender=CHAT, text="800", reply_to=external_id), app.router, app.approvals, app.conversation)
    assert sent_after(http, before) == ["CANCELLED", "DONE", "REQUEST"]


def test_answering_the_how_many_question_does_the_same(world):
    app, http, rec = world
    press(app, rec, "change_qty")                                                          # "How many?"
    before = len(http.calls)
    process_inbound(InboundEvent(channel="telegram", sender=CHAT, text="900"), app.router, app.approvals, app.conversation)
    assert sent_after(http, before) == ["CANCELLED", "DONE", "REQUEST"]


def test_the_console_still_gets_the_revised_request_at_once(world):
    app, http, rec = world
    res = app.approvals.decide(rec.id, "change_qty", OWNER, "app", {"qty": 800})          # no reply to send first: nothing is held back
    assert res.status == "revised" and res.after is None
    assert any(r.request.facts.get("qty") == 800 for r in app.approvals.list("pending") if r.kind == "procurement.po")
    assert "after" not in res.model_dump(mode="json")                                      # never part of what is returned over the API


def test_a_held_back_request_is_sent_once_whatever_happens_to_the_reply(world):
    app, http, rec = world
    from conftest import OWNER as owner
    res = app.approvals.decide(rec.id, "change_qty", owner, "telegram", {"qty": 800}, defer=True)
    assert res.after is not None and not any(r.request.facts.get("qty") == 800 and r.id in [d["approval_id"] for d in table_rows(app, "deliveries", {"channel": "telegram"})]
                                             for r in app.approvals.list("pending"))     # not delivered yet
    res.after()
    res.after()
    sent = [d for d in table_rows(app, "deliveries", {"channel": "telegram"}) if d["approval_id"] != rec.id]
    assert len(sent) == 1                                                                  # a second call does not send it again

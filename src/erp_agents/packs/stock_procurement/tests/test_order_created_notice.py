"""After Create order: the number of the draft that was created is sent back, on Telegram and on WhatsApp."""
import pytest

from conftest import OWNER, FakeHTTP
from erp_agents.core.approvals.channels.simple import InboxChannel
from erp_agents.core.approvals.channels.telegram import TelegramChannel
from erp_agents.core.approvals.channels.whatsapp import WhatsAppChannel
from erp_agents.core.approvals.inbound import process_inbound
from ai_munimji_engine.core.approvals.models import InboundEvent
from ai_munimji_engine.core.workorders import REORDER_NEEDED
from erp_agents.core.erp.mock import MockERP

CHAT, PHONE = "555", "919800000000"


def wo(code="BOLT-M8", qty=500):
    return {"item_code": code, "item_name": "Bolt M8 x 40", "item_group": "Fasteners", "uom": "pcs", "suggested_qty": qty, "cover_days": 10.0, "lead_time_days": 5.0, "daily_rate": 20.0,
            "on_hand": 200.0, "available": 200.0, "severity": "HIGH", "reasons": ["Low."], "stockout_date": "2026-10-26"}


@pytest.fixture
def world(make_app, clock):
    tg_http, wa_http = FakeHTTP(), FakeHTTP()
    channels = {"telegram": TelegramChannel("123:TOKEN", webhook_secret="s", http=tg_http), "whatsapp": WhatsAppChannel("T", "PNID", app_secret="s", verify_token="v", http=wa_http),
                "inbox": InboxChannel()}
    cfg = {"approvals": {"strategy": "all", "recipients": [
        {"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "telegram", "address": CHAT}, {"type": "whatsapp", "address": PHONE}, {"type": "inbox"}]},
        {"id": "second", "name": "Second", "roles": ["approver"], "channels": [{"type": "whatsapp", "address": "919811111111"}]}]}}
    app = make_app(erp=MockERP(today=clock().date()), channels=channels, cfg=cfg)
    return app, tg_http, wa_http


def request(app, **settings):
    if settings:
        app.settings.update_procurement(OWNER, settings, [])
    app.workorders.emit(REORDER_NEEDED, wo())
    assert app.procurement.run().proposed == 1
    return next(r for r in app.approvals.list("pending") if r.kind.startswith("procurement."))


def press(app, rec, action, channel, sender):
    return process_inbound(InboxEvent(rec, action, channel, sender), app.router, app.approvals)


def InboxEvent(rec, action, channel, sender):
    """A button press as the channel delivers it: Telegram's carries a token to answer with a pop-up, WhatsApp's is answered with a message."""
    return InboundEvent(channel=channel, sender=sender, approval_id=rec.id, action=action, ack_token="cq1" if channel == "telegram" else None)


def wa_texts(http, number):
    return [c[1]["text"]["body"] for c in http.calls if c[1] and c[1].get("type") == "text" and c[1].get("to") == number]


def announced(texts):
    """The announcement that the draft was created. (The message with the Change / Cancel buttons that follows it names the order too, and has its own tests.)"""
    return [t for t in texts if "was created in ERPNext" in t]


def tg_texts(http):
    return [c[1]["text"] for c in http.calls if c[0].endswith("/sendMessage") and str(c[1].get("chat_id")) == CHAT]


def test_after_create_order_the_po_number_reaches_telegram_and_whatsapp_when_the_answer_came_from_telegram(world):
    app, tg, wa = world
    rec = request(app)
    before = len(wa_texts(wa, PHONE))
    res = press(app, rec, "approve", "telegram", CHAT)
    assert res.status == "decided"
    (po,) = app.erp.purchase_orders
    name = po["name"]
    assert name in res.message and "Draft purchase order" in res.message and "500 pcs of Bolt M8 x 40 (BOLT-M8)" in res.message and "only a draft" in res.message
    assert any(name in t and t.startswith("Recorded: Create order") for t in tg_texts(tg))                    # under the request message on Telegram
    sent = announced(wa_texts(wa, PHONE)[before:])
    assert len(sent) == 1 and name in sent[0] and "Nothing has been sent to the vendor" in sent[0]             # and on WhatsApp, once
    assert any(name in t and "Change quantity" in str(c) for c in tg.calls if c[0].endswith("/sendMessage") for t in [c[1].get("text", "")])      # and the buttons to change or cancel it follow


def test_when_the_answer_came_from_whatsapp_that_chat_gets_one_reply_and_telegram_gets_the_number(world):
    app, tg, wa = world
    rec = request(app)
    before = len(wa_texts(wa, PHONE))
    press(app, rec, "approve", "whatsapp", PHONE)
    name = app.erp.purchase_orders[0]["name"]
    mine = announced(wa_texts(wa, PHONE)[before:])
    assert len(mine) == 1 and name in mine[0]                                                                  # the reply to the button press: not sent twice
    assert any(name in t for t in announced(tg_texts(tg)))


def test_the_other_approver_on_whatsapp_is_told_too_and_nobody_is_told_twice(world):
    app, tg, wa = world
    rec = request(app)
    press(app, rec, "approve", "telegram", CHAT)
    name = app.erp.purchase_orders[0]["name"]
    assert any(name in t for t in announced(wa_texts(wa, "919811111111")))
    assert sum(name in t for t in announced(wa_texts(wa, PHONE))) == 1 and sum(name in t for t in announced(tg_texts(tg))) == 1


def test_a_requisition_says_so(world):
    app, tg, wa = world
    rec = request(app)
    res = press(app, rec, "requisition", "telegram", CHAT)
    assert "Draft purchase requisition" in res.message and app.erp.requisitions[0]["name"] in res.message if hasattr(app.erp, "requisitions") else "requisition" in res.message
    assert any("Draft purchase requisition" in t for t in wa_texts(wa, PHONE))


def test_nothing_is_announced_when_the_order_could_not_be_created(world):
    app, tg, wa = world
    rec = request(app)
    app.erp.fail_purchase_orders = True
    before = len(wa_texts(wa, PHONE)), len(tg_texts(tg))
    res = press(app, rec, "approve", "telegram", CHAT)
    assert res.status == "error" and "Draft purchase order" not in res.message
    assert (len(wa_texts(wa, PHONE)), len(tg_texts(tg))) == before and app.approvals.get(rec.id).status == "pending"


def test_when_orders_are_switched_off_the_message_says_nothing_was_created(world):
    app, tg, wa = world
    rec = request(app, po_mode="off")
    res = press(app, rec, "approve", "telegram", CHAT)
    assert "Nothing was created in ERPNext" in res.message and not app.erp.purchase_orders
    assert any("Nothing was created in ERPNext" in t for t in wa_texts(wa, PHONE))


def test_answering_again_repeats_the_number(world):
    app, _tg, _wa = world
    rec = request(app)
    press(app, rec, "approve", "telegram", CHAT)
    again = press(app, rec, "approve", "telegram", CHAT)
    assert again.status == "already_decided" and again.message.startswith("Already answered:") and app.erp.purchase_orders[0]["name"] in again.message
    assert len(app.erp.purchase_orders) == 1


def test_an_answer_that_creates_nothing_is_not_announced_on_whatsapp(world):
    app, _tg, wa = world
    rec = request(app)
    before = len(wa_texts(wa, PHONE))
    press(app, rec, "dismiss", "telegram", CHAT)
    assert len(wa_texts(wa, PHONE)) == before                                                                  # a plain "Reject" keeps WhatsApp as quiet as it was

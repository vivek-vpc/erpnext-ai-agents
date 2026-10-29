"""Typing to the assistant from Telegram (and WhatsApp): questions, replies to a request, and the "how many?" answer after Change quantity."""
import itertools
import json
import threading
import time

import pytest

from conftest import OWNER, FakeHTTP
from erp_agents.core.approvals.channels.base import encode_action
from erp_agents.core.approvals.channels.simple import InboxChannel
from erp_agents.core.approvals.channels.telegram import TelegramChannel
from erp_agents.core.approvals.channels.whatsapp import WhatsAppChannel
from erp_agents.core.approvals.inbound import process_inbound
from erp_agents.core.approvals.models import InboundEvent
from erp_agents.packs.stock_procurement.assistant.conversation import Conversation, parse_qty, render
from erp_agents.core.assistant.verify import NOT_ENOUGH
from erp_agents.core.workorders import REORDER_NEEDED
from erp_agents.core.erp.mock import MockERP

OWNER_CHAT = "555"
SECRET = "s3cret"


class FakeLLM:
    model = "fake"

    def __init__(self):
        self.replies, self.calls = [], []

    def chat(self, system, user, schema):
        self.calls.append(user)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


def wo(code="BOLT-M8", qty=500):
    return {"item_code": code, "item_name": "Bolt M8 x 40", "item_group": "Fasteners", "uom": "pcs", "suggested_qty": qty, "cover_days": 10.0, "lead_time_days": 5.0, "daily_rate": 20.0,
            "on_hand": 200.0, "available": 200.0, "severity": "HIGH", "reasons": ["Low."], "stockout_date": "2026-10-26"}


@pytest.fixture
def world(make_app, clock):
    http = FakeHTTP()
    channels = {"telegram": TelegramChannel("123:TOKEN", webhook_secret=SECRET, http=http), "inbox": InboxChannel()}
    cfg = {"approvals": {"strategy": "all", "recipients": [
        {"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "telegram", "address": OWNER_CHAT}]},
        {"id": "second", "name": "Second", "roles": ["approver"], "channels": [{"type": "telegram", "address": "556"}]}]}}
    app = make_app(erp=MockERP(today=clock().date()), channels=channels, cfg=cfg)
    return app, http


def sent(http, chat=OWNER_CHAT):
    return [p for u, p, _ in http.calls if u.endswith("/sendMessage") and str(p.get("chat_id")) == str(chat)]


def texts(http, chat=OWNER_CHAT):
    return [p["text"] for p in sent(http, chat)]


UPDATE_IDS = itertools.count(100)                  # Telegram gives every update its own id; one that is delivered again has the same (core/approvals/inbound_ledger.py)


def say(app, text, sender=OWNER_CHAT, reply_to=None):
    """A message typed into Telegram, as the app receives it."""
    ev = app.channels["telegram"].parse_webhook({"update_id": next(UPDATE_IDS), "message": {"message_id": 9, "text": text, "from": {"id": int(sender), "is_bot": False, "first_name": "P"},
                                                                          "chat": {"id": int(sender), "type": "private"}, **({"reply_to_message": {"message_id": reply_to}} if reply_to else {})}})
    assert len(ev) == 1
    return process_inbound(ev[0], app.router, app.approvals, app.conversation)


def press(app, approval_id, action, sender=OWNER_CHAT):
    ev = app.channels["telegram"].parse_webhook({"update_id": next(UPDATE_IDS), "callback_query": {"id": "cq1", "from": {"id": int(sender)}, "data": encode_action(approval_id, action),
                                                                                  "message": {"message_id": 7, "chat": {"id": int(sender)}}}})
    return process_inbound(ev[0], app.router, app.approvals, app.conversation)


def propose(app, **settings):
    if settings:
        app.settings.update_procurement(OWNER, settings, [])
    app.workorders.emit(REORDER_NEEDED, wo())
    assert app.procurement.run().proposed == 1
    return next(r for r in app.approvals.list("pending") if r.kind.startswith("procurement."))


def message_id_of(app, approval_id, chat=OWNER_CHAT):
    return int(app.db.query_one("SELECT external_id FROM deliveries WHERE approval_id=? AND address=?", (approval_id, chat))["external_id"])


def waiting(app):
    return [r for r in app.approvals.list("pending", 50) if r.kind in ("procurement.po", "procurement.pr")]


# ------------------------------------------------------------------ reading a quantity
@pytest.mark.parametrize("text,qty", [("800", 800), ("  800 ", 800), ("qty 800", 800), ("Quantity: 800", 800), ("make it 800", 800), ("change to 1,200", 1200), ("800 pcs", 800), ("order 7.5 kg", 7.5),
                                      ("set the quantity to 90", None), ("how much FAS-0004", None), ("800 and 900", None), ("", None), ("FAS-0004", None)])
def test_reading_a_quantity(text, qty):
    assert parse_qty(text) == qty


# ------------------------------------------------------------------ 3. questions about stock, vendors and orders
def test_a_question_typed_to_the_bot_is_answered_from_the_same_numbers_as_the_console(world):
    app, http = world
    say(app, "how much BOLT-M8 do we have?")
    expected = app.chat.ask("how much BOLT-M8 do we have?", "owner", True)["answer"]
    assert expected in texts(http)[0] and "300" in texts(http)[0]


def test_a_follow_up_continues_with_the_item_just_discussed_and_vendor_tables_read_well_on_a_phone(world):
    app, http = world
    say(app, "how much BOLT-M8 do we have?")
    say(app, "compare vendors for 500")
    last = texts(http)[-1]
    assert "Still talking about the same item" in last and "Prime Supplies" in last and "Vendor | Price each" in last and "\n- Prime Supplies" in last


def test_the_remembered_item_is_forgotten_after_a_while(world):
    app, http = world
    now = [1000.0]
    app.conversation.clock = lambda: now[0]
    say(app, "how much BOLT-M8 do we have?")
    now[0] += 3600
    say(app, "compare vendors for 500")
    assert "Still talking about the same item" not in texts(http)[-1]


def test_free_text_cannot_order_or_approve_anything(world):
    app, http = world
    rec = propose(app)
    for t in ("approve it", "yes create the order", "create a purchase order for BOLT-M8", "delete everything"):
        say(app, t)
    assert app.approvals.get(rec.id).status == "pending" and not app.erp.purchase_orders and len(texts(http)) >= 5


def test_typing_create_a_purchase_order_sends_the_request_with_its_buttons_and_orders_nothing(world):
    app, http = world
    say(app, "create Purchase orders for BOLT-M8 for 800")
    (rec,) = waiting(app)
    assert rec.request.facts["qty"] == 800 and not app.erp.purchase_orders
    buttons = [b["text"] for m in sent(http) if m.get("reply_markup") for row in m["reply_markup"]["inline_keyboard"] for b in row]
    assert {"Create order", "Change quantity", "Change vendor"} <= set(buttons)
    assert "Nothing is ordered until you press Create order" in texts(http)[-1] and "800 pcs" in texts(http)[-1]
    press(app, rec.id, "change_qty")
    say(app, "900")
    (new,) = waiting(app)
    assert new.id != rec.id and new.request.facts["qty"] == 900
    press(app, new.id, "approve")
    (po,) = app.erp.purchase_orders
    assert po["qty"] == 900 and po["item_code"] == "BOLT-M8"


def test_asking_for_an_order_whose_request_is_already_waiting_sends_that_request_again_to_the_asker_only(world):
    app, http = world
    rec = propose(app)
    owner_before, other_before = len(sent(http)), len(sent(http, "556"))
    say(app, "create purchase orders for BOLT-M8")
    mine = sent(http)[owner_before:]
    assert "already waiting" in mine[0]["text"] and mine[-1].get("reply_markup") and rec.request.title in mine[-1]["text"]
    assert len(sent(http, "556")) == other_before and len(waiting(app)) == 1 and waiting(app)[0].id == rec.id
    assert len(app.db.query("SELECT 1 FROM deliveries WHERE approval_id=? AND address=?", (rec.id, OWNER_CHAT))) == 2     # the new message is remembered, so replying to it works
    assert not app.erp.purchase_orders


def test_a_stranger_cannot_ask_for_an_order(world):
    app, http = world
    say(app, "create a purchase order for BOLT-M8 for 800", sender="999")
    assert not waiting(app) and not app.workorders.list() and "BOLT-M8" not in " ".join(texts(http, "999"))


def test_asking_for_an_order_without_an_item_asks_which_and_a_later_number_is_not_an_order(world):
    app, http = world
    say(app, "create a purchase order")
    assert "Which item" in texts(http)[-1] and not waiting(app)
    say(app, "800")
    assert not waiting(app) and not app.workorders.list()


def test_too_many_messages_in_a_minute_get_a_polite_stop(world):
    app, http = world
    for _ in range(8):
        say(app, "how much BOLT-M8 do we have?")
    assert "a lot of questions in a minute" in texts(http)[-1] and len(texts(http)) == 8


def test_a_message_that_breaks_the_chat_gets_an_apology_and_the_next_message_still_works(world, monkeypatch):
    app, http = world
    monkeypatch.setattr(app.chat, "ask", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    say(app, "how much BOLT-M8 do we have?")
    assert "could not handle that just now" in texts(http)[-1]
    monkeypatch.undo()
    say(app, "how much BOLT-M8 do we have?")
    assert "300" in texts(http)[-1]


def test_a_stranger_typing_is_not_answered_with_any_company_data(world):
    app, http = world
    say(app, "how much BOLT-M8 do we have?", sender="999")
    reply = " ".join(texts(http, "999"))
    assert "300" not in reply and "BOLT" not in reply and "999" in reply                         # only their own chat id


# ------------------------------------------------------------------ 2. replying to a request
def test_replying_to_a_request_with_a_number_changes_its_quantity(world):
    app, http = world
    rec = propose(app)
    mid = message_id_of(app, rec.id)
    say(app, "800", reply_to=mid)
    assert any("Done: 800 pcs" in t for t in texts(http))
    new = waiting(app)
    assert len(new) == 1 and new[0].id != rec.id and new[0].request.facts["qty"] == 800
    assert any(u.endswith("/editMessageReplyMarkup") for u, _, _ in http.calls)                   # the old message loses its buttons
    revised = [p for p in sent(http) if p.get("reply_markup") and "Quantity changed from 500 to 800" in p["text"]]
    assert revised and any(b["text"] == "Create order" for row in revised[0]["reply_markup"]["inline_keyboard"] for b in row)       # the new request has its buttons


def test_a_reply_by_somebody_else_to_that_message_does_not_touch_the_request(world):
    app, http = world
    rec = propose(app)
    mid = message_id_of(app, rec.id)                                                               # the owner's message
    say(app, "800", sender="556", reply_to=mid)                                                     # the second manager replies to a message that was sent to the owner
    assert [r.id for r in waiting(app)] == [rec.id] and app.proposals.by_approval(rec.id).qty == 500


def test_a_question_in_reply_to_a_request_is_answered_by_the_checked_helper_from_that_requests_numbers(world):
    app, http = world
    rec = propose(app)
    app.settings.update_section(OWNER, "assistant", {"enabled": True})
    llm = FakeLLM()
    app.assistant.client_factory = lambda cfg: llm
    fact = next(f for f in app.assistant.facts_for(rec.id) if "Recommended vendor" in f.text or "Order" in f.text)
    llm.replies.append({"answerable": True, "answer": "The request is for 500 pcs.", "facts_used": [fact.id]})
    say(app, "why this quantity?", reply_to=message_id_of(app, rec.id))
    out = texts(http)[1:]                                                                            # after the request message itself
    assert "Let me check that against the numbers in the request" in out[0]
    assert out[1].startswith("The request is for 500 pcs.") and "From your data:" in out[1]
    assert llm.calls and "FACTS:" in llm.calls[0] and "why this quantity?" in llm.calls[0]


def test_an_answer_the_checks_reject_says_it_does_not_have_enough_information(world):
    app, http = world
    rec = propose(app)
    app.settings.update_section(OWNER, "assistant", {"enabled": True})
    llm = FakeLLM()
    app.assistant.client_factory = lambda cfg: llm
    llm.replies.append({"answerable": True, "answer": "Because the moon is full.", "facts_used": ["F99"]})
    say(app, "why?", reply_to=message_id_of(app, rec.id))
    assert texts(http)[-1] == NOT_ENOUGH


def test_with_the_helper_switched_off_a_question_about_a_request_falls_back_to_the_stock_data_and_says_so(world):
    app, http = world
    rec = propose(app)
    say(app, "how much do we have?", reply_to=message_id_of(app, rec.id))
    last = texts(http)[-1]
    assert last.startswith("The AI helper is switched off") and "300" in last                        # answered about THIS request's item, from the stock data


def test_when_the_model_is_unreachable_the_person_is_told_and_still_gets_the_data_answer(world):
    app, http = world
    rec = propose(app)
    app.settings.update_section(OWNER, "assistant", {"enabled": True})
    from erp_agents.core.assistant.llm import LLMError
    llm = FakeLLM()
    llm.replies.append(LLMError("Ollama is not running"))
    app.assistant.client_factory = lambda cfg: llm
    say(app, "how much do we have?", reply_to=message_id_of(app, rec.id))
    last = texts(http)[-1]
    assert "not available right now" in last and "300" in last


# ------------------------------------------------------------------ 1. the bot asks, the person answers
def test_pressing_change_quantity_asks_how_many_and_the_next_message_is_the_answer(world):
    app, http = world
    rec = propose(app)
    res = press(app, rec.id, "change_qty")
    assert res.status == "need_input"
    assert "How many pcs of Bolt M8 x 40" in texts(http)[-1] and any(u.endswith("/answerCallbackQuery") for u, _, _ in http.calls)
    say(app, "750")
    assert waiting(app)[0].request.facts["qty"] == 750 and any("Done: 750 pcs" in t for t in texts(http))


def test_cancel_drops_the_question_and_changes_nothing(world):
    app, http = world
    rec = propose(app)
    press(app, rec.id, "change_qty")
    say(app, "cancel")
    assert texts(http)[-1] == "Okay, nothing was changed." and [r.id for r in waiting(app)] == [rec.id]
    say(app, "750")                                                                                  # no longer expected: a lone number is just a message now
    assert [r.id for r in waiting(app)] == [rec.id]


def test_something_that_is_not_a_number_is_treated_as_a_new_question_and_drops_the_old_one(world):
    app, http = world
    rec = propose(app)
    press(app, rec.id, "change_qty")
    say(app, "how much BOLT-M8 do we have?")
    assert "300" in texts(http)[-1] and [r.id for r in waiting(app)] == [rec.id]
    say(app, "800")
    assert [r.id for r in waiting(app)] == [rec.id]


def test_a_refused_quantity_explains_why_and_the_request_stays(world):
    app, http = world
    rec = propose(app)
    press(app, rec.id, "change_qty")
    say(app, "0")
    assert "more than zero" in texts(http)[-1] and [r.id for r in waiting(app)] == [rec.id]


def test_the_answer_to_the_bot_expires(world):
    app, http = world
    rec = propose(app)
    now = [1000.0]
    app.conversation.clock = lambda: now[0]
    press(app, rec.id, "change_qty")
    now[0] += 3600
    say(app, "750")
    assert [r.id for r in waiting(app)] == [rec.id]


def test_change_vendor_lists_the_alternatives_and_the_person_picks_by_number_or_name(world):
    app, http = world
    rec = propose(app)
    press(app, rec.id, "change_vendor")
    prompt = texts(http)[-1]
    assert prompt.startswith("Which vendor instead?") and "1. " in prompt and "2. " in prompt
    say(app, "1")
    new = waiting(app)
    assert len(new) == 1 and new[0].id != rec.id and "Vendor changed from" in json.dumps(new[0].request.model_dump(mode="json"))
    press(app, new[0].id, "change_vendor")
    again = texts(http)[-1]
    name = again.splitlines()[1].split(". ", 1)[1].split(":")[0]
    say(app, name.lower())
    assert len(waiting(app)) == 1 and waiting(app)[0].id != new[0].id


def test_the_whole_conversation_ends_in_an_order_for_the_changed_quantity_and_vendor(world):
    app, http = world
    rec = propose(app)
    press(app, rec.id, "change_qty")
    say(app, "900")
    press(app, waiting(app)[0].id, "change_vendor")
    say(app, "2")
    final = waiting(app)[0]
    assert final.request.facts["qty"] == 900
    res = press(app, final.id, "approve")
    assert res.status == "decided"
    po = app.erp.purchase_orders[0]
    assert po["qty"] == 900 and po["supplier"] == final.request.facts["supplier"] and po["approved_by"] == "owner via telegram"


# ------------------------------------------------------------------ WhatsApp is the same conversation
def test_whatsapp_text_gets_the_same_answers_and_the_change_quantity_prompt(make_app, clock):
    http = FakeHTTP()
    wa = WhatsAppChannel("T", "PNID", app_secret="s", verify_token="v", http=http)
    cfg = {"approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "whatsapp", "address": "919800000000"}]}]}}
    app = make_app(erp=MockERP(today=clock().date()), channels={"whatsapp": wa}, cfg=cfg)
    rec = propose(app)
    msgs = lambda: [p["text"]["body"] for u, p, _ in http.calls if p.get("type") == "text"]
    process_inbound(InboundEvent(channel="whatsapp", sender="919800000000", text="how much BOLT-M8 do we have?"), app.router, app.approvals, app.conversation)
    assert "300" in msgs()[-1]
    process_inbound(InboundEvent(channel="whatsapp", sender="919800000000", approval_id=rec.id, action="change_qty"), app.router, app.approvals, app.conversation)
    assert "How many pcs of Bolt M8 x 40" in msgs()[-1]
    process_inbound(InboundEvent(channel="whatsapp", sender="919800000000", text="640"), app.router, app.approvals, app.conversation)
    assert waiting(app)[0].request.facts["qty"] == 640


# ------------------------------------------------------------------ a slow model must not hold up the buttons
def test_a_slow_answer_does_not_block_the_next_message(make_app, clock):
    http = FakeHTTP()
    channels = {"telegram": TelegramChannel("123:TOKEN", webhook_secret=SECRET, http=http)}
    cfg = {"approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "telegram", "address": OWNER_CHAT}]}]}}
    app = make_app(erp=MockERP(today=clock().date()), channels=channels, cfg=cfg)
    app.conversation = Conversation(app.approvals, app.assistant, app.chat, app.router, synchronous=False)
    release, started = threading.Event(), threading.Event()
    real = app.chat.ask

    def slow(*a, **k):
        started.set()
        release.wait(5)
        return real(*a, **k)
    app.chat.ask = slow
    t0 = time.time()
    say(app, "how much BOLT-M8 do we have?")
    assert time.time() - t0 < 1.0 and started.wait(2) and not texts(http)                      # the message was taken; the answer is still being worked out
    rec = propose(app)                                                                           # meanwhile a request arrives and a button works at once
    assert press(app, rec.id, "dismiss").status == "decided"
    release.set()
    app.conversation._pool.shutdown(wait=True)
    assert "300" in texts(http)[-1]


# ------------------------------------------------------------------ plain-text rendering
def test_rendering_a_table_and_key_values_for_a_phone():
    out = {"answer": "Here you go.", "blocks": [{"type": "table", "title": "Vendors", "columns": ["Vendor", "Price"], "rows": [[f"V{i}", str(i)] for i in range(12)]},
                                              {"type": "kv", "title": "Stock", "rows": [["In stock", "300 pcs"]]}], "follow_ups": ["a", "b", "c", "d"]}
    text = render(out)
    assert text.startswith("Here you go.") and "Vendor | Price" in text and "- V0 | 0" in text and "- V7 | 7" in text and "- V8" not in text and "and 4 more" in text
    assert "In stock: 300 pcs" in text and text.count("\n- ") >= 10 and "You could ask next:" in text and "- d" not in text

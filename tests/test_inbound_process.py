"""The engine's handling of what comes back from a messaging channel (core/approvals/inbound.py) and of what a person types (core/approvals/typed_answers.py), over the engine's own
in-memory stores and the real ApprovalService: nothing of the service is used. This is what a Frappe site runs; the service's tests of the same code are in tests/test_telegram.py,
tests/test_inbound_dedup.py and the stock pack's test_conversation.py."""
from types import SimpleNamespace

import pytest

from conftest import FakeHTTP
from ai_munimji_engine.core.approvals.channels.telegram import TelegramChannel
from ai_munimji_engine.core.approvals.inbound import process_inbound
from ai_munimji_engine.core.approvals.models import ApprovalOption, ApprovalRequest, DecisionResult, Recipient, RecipientChannel, Section
from ai_munimji_engine.core.approvals.router import ApprovalRouter
from ai_munimji_engine.core.approvals.service import ApprovalService, KindHandler
from ai_munimji_engine.core.approvals.typed_answers import (
    NOT_UNDERSTOOD, TypedConversation, parse_qty, pick,
)
from ai_munimji_engine.stores import memory_core_stores

OWNER = "555"


class Clock:
    """The conversation's clock (seconds); the service has its own."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def update(sender=int(OWNER), text=None, action=None, approval_id=None, update_id=1, reply_to=None):
    if action:
        return {"update_id": update_id, "callback_query": {"id": f"cq{update_id}", "from": {"id": sender}, "data": f"ap|{approval_id}|{action}"}}
    message = {"message_id": 5, "text": text, "from": {"id": sender, "is_bot": False}, "chat": {"id": sender, "type": "private"}}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": message}


@pytest.fixture
def world():
    http = FakeHTTP()
    channel = TelegramChannel("1:TOKEN", webhook_secret="s", http=http)
    owner = Recipient(id="owner", name="Owner", roles=["approver"], channels=[RecipientChannel(type="telegram", address=OWNER)])
    viewer = Recipient(id="viewer", name="Viewer", roles=["viewer"], channels=[RecipientChannel(type="telegram", address="666")])
    router = ApprovalRouter({"telegram": channel}, [owner, viewer])
    core = memory_core_stores()
    service = ApprovalService(core.approvals, router, inbound=core.inbound)
    log = SimpleNamespace(decided=[], changed=[], vendors=[], cancelled=[], boom=False)

    def on_decision(record, decision):
        if log.boom:
            raise RuntimeError("the ERP is down")
        log.decided.append((record.id, decision.action, decision.actor_id, decision.channel))

    def on_action(record, action, actor, channel_name, params):
        if action == "change_qty":
            if "qty" not in params:
                return DecisionResult(ok=True, status="need_input", message="How many?", prompt="How many should I order?", approval=record)
            log.changed.append(params["qty"])
            return DecisionResult(ok=True, status="revised", message=f"Done: the quantity is now {params['qty']:g}.", approval=record)
        if action == "change_vendor":
            choices = [{"id": "SUP-1", "label": "Quick Fasteners: 4.50"}, {"id": "SUP-2", "label": "Bharat Bolt: 4.10"}]
            if "supplier" not in params:
                return DecisionResult(ok=True, status="need_input", message="Which vendor?", prompt="Which vendor?", choices=choices, approval=record)
            log.vendors.append(params["supplier"])
            return DecisionResult(ok=True, status="revised", message=f"Done: {params['supplier']}.", approval=record)
        if action == "cancel_order":
            if "confirm" not in params:
                return DecisionResult(ok=True, status="need_input", message="Cancel?", prompt="Cancel the order? Reply yes or no.", approval=record)
            log.cancelled.append(params["confirm"])
            return DecisionResult(ok=True, status="revised", message="Done.", approval=record)
        return None

    service.register_handler("procurement.po", KindHandler(on_decision=on_decision, on_action=on_action))
    clock = Clock()
    conversation = TypedConversation(service, router, clock=clock)
    request = ApprovalRequest(
        kind="procurement.po", title="Order 100 bolts", headline="Order 100 bolts", severity="HIGH", sections=[Section(heading="Situation", lines=["Low stock"])],
        options=[ApprovalOption(action="approve", label="Create order"), ApprovalOption(action="reject", label="Reject"),
                 ApprovalOption(action="details", label="Details", terminal=False),
                 ApprovalOption(action="change_qty", label="Change quantity", terminal=False),
                 ApprovalOption(action="change_vendor", label="Change vendor", terminal=False),
                 ApprovalOption(action="cancel_order", label="Cancel order", terminal=False)],
    )
    record = service.submit(request)
    return SimpleNamespace(http=http, channel=channel, router=router, service=service, log=log, clock=clock, conversation=conversation, record=record)


def feed(w, payload, conversation=True):
    result = None
    for event in w.channel.parse_webhook(payload):
        result = process_inbound(event, w.router, w.service, w.conversation if conversation else None)
    return result


def texts(w, chat=OWNER):
    return [p["text"] for u, p, _ in w.http.calls if u.endswith("/sendMessage") and str(p.get("chat_id")) == chat]


def pressed(w):
    return [p["text"] for u, p, _ in w.http.calls if u.endswith("/answerCallbackQuery")]


# ------------------------------------------------------------------ buttons
def test_the_request_reached_the_person_who_may_answer_and_not_the_viewer(world):
    assert texts(world) and "Order 100 bolts" in texts(world)[0]
    assert texts(world, "666") == []


def test_a_registered_approver_decides_by_button_and_the_decision_says_where_it_came_from(world):
    result = feed(world, update(action="approve", approval_id=world.record.id))
    assert result.ok and result.status == "decided"
    assert world.log.decided == [(world.record.id, "approve", "owner", "telegram")]
    assert pressed(world) and "Create order" in pressed(world)[-1]


def test_the_second_press_is_refused_and_says_who_answered_first(world):
    feed(world, update(action="approve", approval_id=world.record.id, update_id=1))
    again = feed(world, update(action="reject", approval_id=world.record.id, update_id=2))
    assert not again.ok and again.status == "already_decided" and len(world.log.decided) == 1


def test_a_stranger_pressing_a_button_changes_nothing_and_is_told_so(world):
    assert feed(world, update(sender=999, action="approve", approval_id=world.record.id)) is None
    assert world.service.get(world.record.id).status == "pending" and not world.log.decided
    assert "not registered" in pressed(world)[-1]


def test_a_person_without_the_right_to_answer_is_refused_by_the_service(world):
    result = feed(world, update(sender=666, action="approve", approval_id=world.record.id))
    assert not result.ok and result.status == "forbidden" and not world.log.decided


def test_details_are_sent_as_a_message_and_the_request_stays_open(world):
    result = feed(world, update(action="details", approval_id=world.record.id))
    assert result.status == "info" and world.service.get(world.record.id).status == "pending"
    assert pressed(world)[-1] == "Details sent" and any("Low stock" in t for t in texts(world))


# ------------------------------------------------------------------ the same update twice
def test_an_update_delivered_again_is_handled_once(world):
    payload = update(action="approve", approval_id=world.record.id, update_id=77)
    assert feed(world, payload).ok
    assert feed(world, payload) is None
    assert len(world.log.decided) == 1


def test_an_answer_that_could_not_be_applied_changes_nothing_and_the_next_press_is_a_new_try(world):
    world.log.boom = True
    failed = feed(world, update(action="approve", approval_id=world.record.id, update_id=78))
    assert not failed.ok and failed.status == "error" and not world.log.decided and world.service.get(world.record.id).status == "pending"
    world.log.boom = False
    assert feed(world, update(action="approve", approval_id=world.record.id, update_id=79)).ok and len(world.log.decided) == 1


def test_a_message_that_raised_is_given_back(world):
    class Breaks:
        def expect(self, *a, **k):
            pass

        def submit(self, *a, **k):
            raise RuntimeError("boom")

    event = world.channel.parse_webhook(update(text="hello", update_id=90))[0]
    with pytest.raises(RuntimeError):
        process_inbound(event, world.router, world.service, Breaks())
    assert world.service.inbound.claim("telegram", "90") is True                      # released: a redelivery would be tried again


# ------------------------------------------------------------------ the bot asks, the person types
def test_change_quantity_asks_and_the_next_message_is_the_answer(world):
    feed(world, update(action="change_qty", approval_id=world.record.id, update_id=1))
    assert texts(world)[-1] == "How many should I order?" and "Type your answer" in pressed(world)[-1]
    feed(world, update(text="1,200", update_id=2))
    assert world.log.changed == [1200.0] and "the quantity is now 1200" in texts(world)[-1]


def test_change_vendor_takes_a_number_or_a_name(world):
    feed(world, update(action="change_vendor", approval_id=world.record.id, update_id=1))
    feed(world, update(text="2", update_id=2))
    feed(world, update(action="change_vendor", approval_id=world.record.id, update_id=3))
    feed(world, update(text="quick", update_id=4))
    assert world.log.vendors == ["SUP-2", "SUP-1"]


def test_cancel_order_needs_a_clear_yes_or_no(world):
    feed(world, update(action="cancel_order", approval_id=world.record.id, update_id=1))
    feed(world, update(text="maybe", update_id=2))                                    # not an answer: a new message, the question is dropped
    assert not world.log.cancelled and texts(world)[-1] == NOT_UNDERSTOOD
    feed(world, update(action="cancel_order", approval_id=world.record.id, update_id=3))
    feed(world, update(text="Yes.", update_id=4))
    assert world.log.cancelled == ["yes"]


def test_cancel_words_drop_the_question(world):
    feed(world, update(action="change_qty", approval_id=world.record.id, update_id=1))
    feed(world, update(text="never mind", update_id=2))
    assert texts(world)[-1] == "Okay, nothing was changed." and not world.log.changed
    feed(world, update(text="500", update_id=3))                                      # nothing is being asked any more
    assert not world.log.changed


def test_an_answer_that_comes_too_late_is_not_taken(world):
    feed(world, update(action="change_qty", approval_id=world.record.id, update_id=1))
    world.clock.now += 601
    feed(world, update(text="500", update_id=2))
    assert not world.log.changed and texts(world)[-1] == NOT_UNDERSTOOD


def test_a_number_in_reply_to_the_order_request_changes_its_quantity(world):
    message_id = world.service.store.deliveries(world.record.id, "sent")[0]["external_id"]
    feed(world, update(text="400", reply_to=int(message_id), update_id=1))
    assert world.log.changed == [400.0]
    feed(world, update(text="400", update_id=2))                                       # not a reply, nothing asked: no guessing
    assert world.log.changed == [400.0] and texts(world)[-1] == NOT_UNDERSTOOD


def test_a_number_in_reply_to_anything_but_a_waiting_order_is_not_a_quantity(world):
    alert = world.service.submit(ApprovalRequest(kind="inventory.reorder", title="Reorder bolts", headline="Reorder bolts", severity="HIGH",
                                                 options=[ApprovalOption(action="approve", label="Start reorder")]))
    alert_message = world.service.store.deliveries(alert.id, "sent")[0]["external_id"]
    feed(world, update(text="400", reply_to=int(alert_message), update_id=1))                     # a reorder is not an order: it has no quantity to change here
    assert not world.log.changed and texts(world)[-1] == NOT_UNDERSTOOD
    order_message = world.service.store.deliveries(world.record.id, "sent")[0]["external_id"]
    feed(world, update(action="approve", approval_id=world.record.id, update_id=2))
    feed(world, update(text="400", reply_to=int(order_message), update_id=3))                     # the order was answered meanwhile: nothing left to change
    assert not world.log.changed and texts(world)[-1] == NOT_UNDERSTOOD


def test_a_reply_that_is_not_a_number_is_not_acted_on(world):
    message_id = world.service.store.deliveries(world.record.id, "sent")[0]["external_id"]
    feed(world, update(text="why this much?", reply_to=int(message_id), update_id=1))
    assert not world.log.changed and texts(world)[-1] == NOT_UNDERSTOOD


def test_free_text_gets_one_sentence_and_changes_nothing(world):
    feed(world, update(text="approve everything please", update_id=1))
    assert texts(world)[-1] == NOT_UNDERSTOOD and world.service.get(world.record.id).status == "pending"


def test_without_a_conversation_the_person_is_pointed_to_the_buttons(world):
    feed(world, update(text="hello", update_id=1), conversation=False)
    assert "Please answer with the buttons" in texts(world)[-1]


def test_a_failure_while_handling_a_message_is_apologised_for_and_does_not_escape(world):
    world.conversation.approvals = SimpleNamespace(get=lambda *_: (_ for _ in ()).throw(RuntimeError("db gone")), approval_for_message=lambda *a: "x")
    feed(world, update(text="5", reply_to=1, update_id=1))
    assert "could not handle that just now" in texts(world)[-1]


# ------------------------------------------------------------------ strangers who write
def test_a_stranger_who_writes_gets_their_id_and_nothing_else_happens(world):
    assert feed(world, update(sender=888, text="/start", update_id=1)) is None
    [reply] = texts(world, "888")
    assert "888" in reply and OWNER not in reply and world.service.get(world.record.id).status == "pending"


def test_a_handler_for_unregistered_senders_can_take_the_message_of_a_channel_that_has_no_commands(world):
    """Telegram answers every stranger itself (their chat id); a channel without commands (WhatsApp) passes the text on, for example to a vendor's reply."""
    from ai_munimji_engine.core.approvals.channels.base import Channel
    from ai_munimji_engine.core.approvals.models import DeliveryResult, InboundEvent

    class Bare(Channel):
        type = "bare"

        def __init__(self):
            self.said = []

        def send_approval(self, record, address):
            return DeliveryResult(ok=True)

        def send_text(self, address, text):
            self.said.append(text)
            return DeliveryResult(ok=True)

    bare = Bare()
    router = ApprovalRouter({"bare": bare}, [])
    taken = []
    world.service.unknown_sender_handlers.append(lambda event, channel: taken.append(event.text) or True)
    assert process_inbound(InboundEvent(channel="bare", sender="888", text="my quote is 4.50"), router, world.service) is None
    assert taken == ["my quote is 4.50"] and bare.said == []
    world.service.unknown_sender_handlers.clear()
    process_inbound(InboundEvent(channel="bare", sender="888", text="hello"), router, world.service)
    assert bare.said == ["You are not registered to answer these requests."]


# ------------------------------------------------------------------ the parsing helpers
@pytest.mark.parametrize("text,qty", [("800", 800), ("qty 800", 800), ("quantity: 800", 800), ("make it 800", 800), ("change to 800", 800), ("1,200", 1200), ("800 pcs", 800), ("2.5", 2.5),
                                      ("compare vendors for 500", None), ("FAS-0003", None), ("", None), ("a lot", None)])
def test_a_quantity_is_a_number_alone_or_with_a_few_words(text, qty):
    assert parse_qty(text) == qty


def test_a_choice_is_named_by_its_number_its_vendor_or_its_id():
    choices = [{"id": "SUP-1", "label": "Quick Fasteners: 4.50"}, {"id": "SUP-2", "label": "Bharat Bolt: 4.10"}]
    assert pick(choices, "2") == "SUP-2" and pick(choices, "Bharat") == "SUP-2" and pick(choices, "sup-1") == "SUP-1" and pick(choices, "quick.") == "SUP-1"
    assert pick(choices, "3") is None and pick(choices, "") is None and pick(choices + [{"id": "SUP-3", "label": "Quick Bolts: 5"}], "quick") is None

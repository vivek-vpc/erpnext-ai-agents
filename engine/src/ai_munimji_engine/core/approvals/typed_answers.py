"""What a registered person's typed message can mean when no AI is involved (Telegram, WhatsApp).

  1. the answer to a question the bot just asked ("How many?" after pressing Change quantity, "Which vendor?" after Change vendor, "Cancel the order?" yes / no);
  2. a number in reply to one of the bot's order requests: the new quantity of that request.

Nothing here can order anything. Changing a request only revises the request (see ProcurementAgent): ordering still needs the Create order button.

The functions are what the standalone service's conversation (which adds the AI chat for everything else) and an application without an AI chat both use.
`TypedConversation` is the second: it takes what the two cases above can deal with and says, in one sentence, where to go for the rest.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Optional

from ...runtime import Awaiting, ConversationMemory, InProcessConversationMemory
from .channels.base import Channel
from .models import DecisionResult, InboundEvent, Recipient
from .router import ApprovalRouter

log = logging.getLogger(__name__)
WAIT_SECONDS = 600                 # how long the bot waits for the answer to its own question
ORDER_KINDS = ("procurement.po", "procurement.pr")
YES_NO = {"y": "yes", "yes": "yes", "yeah": "yes", "yep": "yes", "ok": "yes", "okay": "yes", "sure": "yes", "confirm": "yes", "yes cancel it": "yes", "yes, cancel it": "yes",
          "n": "no", "no": "no", "nope": "no", "no keep it": "no", "no, keep it": "no", "keep it": "no"}
CANCEL_WORDS = {"cancel", "no", "stop", "never mind", "nevermind", "forget it", "skip", "leave it"}
# "800", "qty 800", "quantity: 800", "make it 800", "change to 800", "1,200", "800 pcs"
QTY = re.compile(r"(?:(?:set\s+)?(?:the\s+)?(?:qty|quantity)|make\s+it|change\s+(?:it\s+)?to|order|new\s+quantity)?\s*[:=]?\s*(\d[\d,]*(?:\.\d+)?)\s*[a-z]{0,12}", re.I)
NOT_UNDERSTOOD = "I take answers to requests here: use the buttons under a request message, or reply to one with a number to change its quantity. Nothing was changed."
APOLOGY = "I could not handle that just now. Nothing was changed. Please try again in a moment."


def parse_qty(text: str) -> Optional[float]:
    m = QTY.fullmatch((text or "").strip())
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def pick(choices: list[dict[str, str]], text: str) -> Optional[str]:
    """The id of the choice a person named: its number in the list, its vendor's name (or the start of it) or its id. None when the text names none, or more than one."""
    t = text.strip().lower().rstrip(".)")
    if t.isdigit() and 1 <= int(t) <= len(choices):
        return choices[int(t) - 1]["id"]
    named = [c for c in choices if t and (t in c["label"].lower().split(":")[0] or t == c["id"].lower())]
    return named[0]["id"] if len(named) == 1 else None


def say(res: DecisionResult, reply: Callable[[str], Any]) -> None:
    reply(res.message if res.status != "need_input" else (res.prompt or res.message))
    if res.after is not None:                                   # a revised request is sent after this reply, so it is the last message in the chat
        res.after()


def answer_prompt(approvals: Any, waiting: Awaiting, text: str, actor: Any, event: InboundEvent, reply: Callable[[str], Any]) -> bool:
    """The text answers the question the bot asked (`waiting`): True when it was taken as the answer, False when it is something else (a new question: the old one is dropped)."""
    if waiting.action == "change_qty":
        qty = parse_qty(text)
        if qty is None:
            return False
        say(approvals.decide(waiting.approval_id, "change_qty", actor, event.channel, {"qty": qty}, defer=True), reply)
        return True
    if waiting.action == "change_vendor":
        chosen = pick(waiting.choices, text)
        if chosen is None:
            return False
        say(approvals.decide(waiting.approval_id, "change_vendor", actor, event.channel, {"supplier": chosen}, defer=True), reply)
        return True
    if waiting.action == "cancel_order":                       # "Cancel the order? Reply yes or no": anything but a clear yes or no is a new question
        answer = YES_NO.get(text.strip().lower().rstrip(".!"))
        if answer is None:
            return False
        say(approvals.decide(waiting.approval_id, "cancel_order", actor, event.channel, {"confirm": answer}, defer=True), reply)
        return True
    return False


def quantity_in_reply(approvals: Any, record: Any, text: str, actor: Any, event: InboundEvent, reply: Callable[[str], Any]) -> bool:
    """A number in reply to a request that is still waiting and is an order is the new quantity. True when the text was that and the answer was given."""
    if record is None or record.status != "pending" or record.kind not in ORDER_KINDS:
        return False
    qty = parse_qty(text)
    if qty is None:
        return False
    say(approvals.decide(record.id, "change_qty", actor, event.channel, {"qty": qty}, defer=True), reply)
    return True


def request_replied_to(approvals: Any, event: InboundEvent) -> Optional[str]:
    """The request a message was a reply to (the one that was sent as the message the person replied to), or None."""
    if not event.reply_to:
        return None
    return approvals.approval_for_message(event.channel, str(event.reply_to), str(event.sender))


class TypedConversation:
    """The conversation of an application with no AI chat. `expect` is called when a button asks the person something; `submit` takes their next message."""

    def __init__(self, approvals: Any, router: ApprovalRouter, memory: Optional[ConversationMemory] = None, clock: Callable[[], float] = time.monotonic,
                 not_understood: str = NOT_UNDERSTOOD, wait_seconds: float = WAIT_SECONDS) -> None:
        self.approvals, self.router, self.clock = approvals, router, clock
        self.memory = memory or InProcessConversationMemory()
        self.not_understood, self.wait_seconds = not_understood, wait_seconds

    def expect(self, channel: str, sender: str, approval_id: str, action: str, choices: Optional[list[dict[str, str]]] = None) -> None:
        """The bot has just asked this person something about a request: their next message is probably the answer."""
        self.memory.expect(channel, sender, Awaiting(approval_id, action, list(choices or []), self.clock() + self.wait_seconds))

    def submit(self, event: InboundEvent, recipient: Recipient, channel: Channel) -> None:
        try:
            self.handle(event, recipient, channel)
        except Exception:  # noqa: BLE001 - never leave a person talking to silence, and never let one message stop the next
            log.exception("could not handle a message from %s on %s", recipient.id, event.channel)
            try:
                channel.send_text(event.sender, APOLOGY)
            except Exception:  # noqa: BLE001 - the apology itself failed; nothing more can be done for this person
                log.debug("could not even send the apology", exc_info=True)

    def handle(self, event: InboundEvent, recipient: Recipient, channel: Channel) -> None:
        text = " ".join((event.text or "").split())
        if not text:
            return
        actor = self.router.actor_for(recipient, via=event.channel)
        reply = lambda t: channel.send_text(event.sender, t)       # noqa: E731

        waiting = self.memory.take_awaiting(event.channel, event.sender)
        if waiting and waiting.until >= self.clock():
            if text.lower() in CANCEL_WORDS:
                reply("Okay, nothing was changed.")
                return
            if answer_prompt(self.approvals, waiting, text, actor, event, reply):
                return                                              # anything else is a new message: the old question is dropped

        approval_id = request_replied_to(self.approvals, event)
        if approval_id and quantity_in_reply(self.approvals, self.approvals.get(approval_id), text, actor, event, reply):
            return
        reply(self.not_understood)

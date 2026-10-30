"""Talking to the assistant from a messaging app (Telegram, WhatsApp).

What a registered person's typed message can mean, in this order:

  1. the answer to a question the bot just asked ("How many?" after pressing Change quantity, "Which vendor?" after Change vendor);
  2. a reply to one of the bot's request messages: a number changes that request's quantity; anything else is a question about THAT request, answered by the local AI
     helper from the request's recorded numbers only (the same checked "Ask why" as in the console);
  3. otherwise a question about stock, vendors, orders or what is waiting: the same read-only chat as in the console.

Nothing here can order anything. Changing a request only revises the request (see ProcurementAgent): ordering still needs the Create order button.
The model can take a minute, so messages are handled on a small pool of worker threads: a button press is never stuck behind a slow answer.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from erp_agents.core.approvals.channels.base import Channel
from erp_agents.core.approvals.models import DecisionResult, InboundEvent, Recipient
from erp_agents.core.approvals.router import ApprovalRouter
from erp_agents.core.resilience import Limits, RateLimited, ResilienceSettings
from .chat import ChatLimit

log = logging.getLogger(__name__)
WAIT_SECONDS = 600                 # how long the bot waits for the answer to its own question
CONTEXT_SECONDS = 900              # "compare vendors" after a question about an item still means that item, for this long
ORDER_KINDS = ("procurement.po", "procurement.pr")
YES_NO = {"y": "yes", "yes": "yes", "yeah": "yes", "yep": "yes", "ok": "yes", "okay": "yes", "sure": "yes", "confirm": "yes", "yes cancel it": "yes", "yes, cancel it": "yes",
          "n": "no", "no": "no", "nope": "no", "no keep it": "no", "no, keep it": "no", "keep it": "no"}
CANCEL_WORDS = {"cancel", "no", "stop", "never mind", "nevermind", "forget it", "skip", "leave it"}
# "800", "qty 800", "quantity: 800", "make it 800", "change to 800", "1,200", "800 pcs"
QTY = re.compile(r"(?:(?:set\s+)?(?:the\s+)?(?:qty|quantity)|make\s+it|change\s+(?:it\s+)?to|order|new\s+quantity)?\s*[:=]?\s*(\d[\d,]*(?:\.\d+)?)\s*[a-z]{0,12}", re.I)


def parse_qty(text: str) -> Optional[float]:
    m = QTY.fullmatch((text or "").strip())
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


@dataclass
class _Awaiting:
    approval_id: str
    action: str
    choices: list[dict[str, str]]
    until: float


@dataclass
class _Context:
    items: list[str] = field(default_factory=list)
    until: float = 0.0


class Conversation:
    def __init__(self, approvals: Any, assistant: Any, chat: Any, router: ApprovalRouter, synchronous: bool = False,
                 clock: Callable[[], float] = time.monotonic, limits: Optional[Limits] = None) -> None:
        self.approvals, self.assistant, self.chat, self.router, self.clock = approvals, assistant, chat, router, clock
        self.limits = limits or getattr(chat, "limits", None) or Limits(ResilienceSettings())       # one set of limits for the whole application
        self._pool = None if synchronous else ThreadPoolExecutor(max_workers=2, thread_name_prefix="conversation")
        self._awaiting: dict[tuple[str, str], _Awaiting] = {}
        self._context: dict[tuple[str, str], _Context] = {}

    # ------------------------------------------------------------------ entry points
    def expect(self, channel: str, sender: str, approval_id: str, action: str, choices: Optional[list[dict[str, str]]] = None) -> None:
        """The bot has just asked this person something about a request: their next message is probably the answer."""
        self._awaiting[(channel, sender)] = _Awaiting(approval_id, action, list(choices or []), self.clock() + WAIT_SECONDS)

    def submit(self, event: InboundEvent, recipient: Recipient, channel: Channel) -> None:
        if self._pool is None:
            self._safe(event, recipient, channel)
        else:
            self._pool.submit(self._safe, event, recipient, channel)

    def _safe(self, event: InboundEvent, recipient: Recipient, channel: Channel) -> None:
        try:
            self.handle(event, recipient, channel)
        except Exception:  # noqa: BLE001 - never leave a person talking to silence, and never let one message stop the next
            log.exception("could not handle a message from %s on %s", recipient.id, event.channel)
            try:
                channel.send_text(event.sender, "I could not handle that just now. Nothing was changed. Please try again in a moment.")
            except Exception:  # noqa: BLE001 - the apology itself failed; nothing more can be done for this person
                log.debug("could not even send the apology", exc_info=True)

    # ------------------------------------------------------------------ one message
    def handle(self, event: InboundEvent, recipient: Recipient, channel: Channel) -> None:
        text = " ".join((event.text or "").split())
        if not text:
            return
        actor = self.router.actor_for(recipient, via=event.channel)
        key = (event.channel, event.sender)
        reply = lambda t: channel.send_text(event.sender, t)       # noqa: E731

        waiting = self._awaiting.pop(key, None)
        if waiting and waiting.until >= self.clock():
            if text.lower() in CANCEL_WORDS:
                reply("Okay, nothing was changed.")
                return
            if self._answer_prompt(waiting, text, actor, event, reply):
                return                                              # anything else is a new question: the old one is dropped

        try:
            self.limits.enforce("channel_chat", f"{key[0]}:{key[1]}")            # messages per chat per minute that may reach the assistant
        except RateLimited as exc:
            reply(str(exc))                                                      # says how many seconds to wait
            return
        resend = lambda approval_id: self._resend(approval_id, recipient, event)       # noqa: E731
        approval_id = self._request_replied_to(event)
        if approval_id:
            self._about_request(approval_id, text, actor, event, reply, resend)
        else:
            self._chat(text, actor, key, reply, resend=resend)

    # ------------------------------------------------------------------ 1. the answer to the bot's own question
    def _answer_prompt(self, waiting: _Awaiting, text: str, actor: Any, event: InboundEvent, reply: Callable[[str], Any]) -> bool:
        if waiting.action == "change_qty":
            qty = parse_qty(text)
            if qty is None:
                return False
            self._say(self.approvals.decide(waiting.approval_id, "change_qty", actor, event.channel, {"qty": qty}, defer=True), reply)
            return True
        if waiting.action == "change_vendor":
            pick = self._pick(waiting.choices, text)
            if pick is None:
                return False
            self._say(self.approvals.decide(waiting.approval_id, "change_vendor", actor, event.channel, {"supplier": pick}, defer=True), reply)
            return True
        if waiting.action == "cancel_order":                       # "Cancel the order? Reply yes or no": anything but a clear yes or no is a new question
            answer = YES_NO.get(text.strip().lower().rstrip(".!"))
            if answer is None:
                return False
            self._say(self.approvals.decide(waiting.approval_id, "cancel_order", actor, event.channel, {"confirm": answer}, defer=True), reply)
            return True
        return False

    @staticmethod
    def _pick(choices: list[dict[str, str]], text: str) -> Optional[str]:
        t = text.strip().lower().rstrip(".)")
        if t.isdigit() and 1 <= int(t) <= len(choices):
            return choices[int(t) - 1]["id"]
        named = [c for c in choices if t and (t in c["label"].lower().split(":")[0] or t == c["id"].lower())]
        return named[0]["id"] if len(named) == 1 else None

    def _say(self, res: DecisionResult, reply: Callable[[str], Any]) -> None:
        reply(res.message if res.status != "need_input" else (res.prompt or res.message))
        if res.after is not None:                                   # a revised request is sent after this reply, so it is the last message in the chat
            res.after()

    # ------------------------------------------------------------------ 2. a reply to one of the bot's request messages
    def _request_replied_to(self, event: InboundEvent) -> Optional[str]:
        if not event.reply_to:
            return None
        return self.approvals.approval_for_message(event.channel, str(event.reply_to), str(event.sender))

    def _resend(self, approval_id: str, recipient: Recipient, event: InboundEvent) -> None:
        record = self.approvals.get(approval_id)
        if record is not None and record.status == "pending":
            self.approvals.resend(record, recipient.id, event.channel, event.sender)

    def _about_request(self, approval_id: str, text: str, actor: Any, event: InboundEvent, reply: Callable[[str], Any], resend: Optional[Callable[[str], Any]] = None) -> None:
        record = self.approvals.get(approval_id)
        if record is None:
            self._chat(text, actor, (event.channel, event.sender), reply, resend=resend)
            return
        if record.status == "pending" and record.kind in ORDER_KINDS:
            qty = parse_qty(text)
            if qty is not None:                                     # a number in reply to an order request is the new quantity
                self._say(self.approvals.decide(approval_id, "change_qty", actor, event.channel, {"qty": qty}, defer=True), reply)
                return
        item = record.request.facts.get("item_code")
        cfg = self.assistant.settings.assistant()
        if cfg.enabled:
            reply("Let me check that against the numbers in the request. The local assistant can take a minute...")
        try:
            out = self.assistant.ask(approval_id, text, actor)
        except ValueError as exc:
            reply(str(exc))
            return
        status = out.get("status")
        if status == "answered":
            lines = [out["answer"]]
            facts = out.get("facts") or []
            if facts:
                lines += ["", "From your data:"] + [f"- {f['text'][:200]}" for f in facts[:4]]
            reply("\n".join(lines))
        elif status in ("disabled", "unavailable"):
            why = ("The AI helper is switched off (Settings, the 'Ask why' helper), so I can answer only from your stock and vendor data." if status == "disabled"
                   else "The AI helper is not available right now, so I can answer only from your stock and vendor data.")
            self._chat(text, actor, (event.channel, event.sender), reply, preface=why, items=[item] if item else None)
        else:
            reply(out.get("answer") or "I do not have enough information to answer that.")

    # ------------------------------------------------------------------ 3. a question about stock, vendors, orders, what is waiting
    def _chat(self, text: str, actor: Any, key: tuple[str, str], reply: Callable[[str], Any], preface: str = "", items: Optional[list[str]] = None,
              resend: Optional[Callable[[str], Any]] = None) -> None:
        ctx = self._context.get(key)
        context = {"items": items} if items else ({"items": ctx.items} if ctx and ctx.until >= self.clock() and ctx.items else None)
        try:
            out = self.chat.ask(text, actor.id, True, context)
        except ChatLimit as exc:
            reply(str(exc))
            return
        except ValueError as exc:
            reply(str(exc))
            return
        if out.get("items"):
            self._context[key] = _Context(list(out["items"]), self.clock() + CONTEXT_SECONDS)
        reply((preface + "\n\n" if preface else "") + render(out))
        exp = out.get("expect")
        if exp:                                                      # the answer asked something (cancel this order? yes / no): the next message is the reply
            self.expect(key[0], key[1], exp["approval_id"], exp["action"], exp.get("choices"))
        for act in out.get("actions") or []:                         # "a request is already waiting": put it in front of this person again, below the answer
            if act.get("resend") and act.get("approval_id") and resend is not None:
                resend(act["approval_id"])


def render(out: dict[str, Any]) -> str:
    """A chat answer as plain text for a phone: the answer, its tables as short lines, and what could be asked next."""
    lines = [str(out.get("answer") or "")]
    for block in (out.get("blocks") or [])[:2]:
        rows = block.get("rows") or []
        if block.get("type") == "table" and rows:
            lines += ["", str(block.get("title") or "")]
            cols = block.get("columns") or []
            if cols:
                lines.append(" | ".join(str(c) for c in cols))
            lines += ["- " + " | ".join(str(c) for c in row) for row in rows[:8]]
            if len(rows) > 8:
                lines.append(f"... and {len(rows) - 8} more (see the console)")
        elif block.get("type") == "kv" and rows:
            lines += ["", str(block.get("title") or "")] + [f"{k}: {v}" for k, v in rows[:12]]
    nxt = (out.get("follow_ups") or [])[:3]
    if nxt:
        lines += ["", "You could ask next:"] + [f"- {q}" for q in nxt]
    return "\n".join(lines).strip()

"""Channel-neutral approval objects.

An ApprovalRequest is *content + choices*, with no knowledge of Telegram, WhatsApp or any app.
Channels turn it into their own message format and translate button presses back into a
Decision. That is what keeps new channels (e.g. your own mobile app) a drop-in addition.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Any, Optional

from pydantic import BaseModel, Field

# Terminal actions end the request; non-terminal ones (details) just return information.
ACTION_DETAILS = "details"


class Section(BaseModel):
    heading: str
    lines: list[str]


class ApprovalOption(BaseModel):
    action: str                     # short machine name (fits Telegram callback_data: the whole button payload is limited to 64 bytes)
    label: str                      # <= 20 chars so it also fits WhatsApp reply buttons
    style: str = "default"          # default | primary | danger (channels may ignore)
    terminal: bool = True
    description: str = ""           # used by list-style UIs (WhatsApp list rows, mobile app)


class ApprovalRequest(BaseModel):
    kind: str                       # e.g. "inventory.reorder"
    title: str
    headline: str                   # one-line summary (notification preview)
    severity: str = "INFO"
    sections: list[Section] = Field(default_factory=list)
    details: list[Section] = Field(default_factory=list)   # shown on the "details" action
    options: list[ApprovalOption] = Field(default_factory=list)
    ref_type: Optional[str] = None
    ref_id: Optional[str] = None
    facts: dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    approval_id: str
    action: str
    actor_id: str
    channel: str
    params: dict[str, Any] = Field(default_factory=dict)
    decided_at: datetime
    note: str = ""


class ApprovalRecord(BaseModel):
    id: str
    kind: str
    status: str                     # pending | decided | expired | cancelled
    request: ApprovalRequest
    decision: Optional[Decision] = None
    created_at: datetime
    expires_at: Optional[datetime] = None
    decided_at: Optional[datetime] = None
    last_reminder_at: Optional[datetime] = None
    reminder_count: int = 0
    status_note: str = ""


class Recipient(BaseModel):
    """A person who can receive and answer approvals, with one or more channels."""

    id: str
    name: str
    roles: list[str] = Field(default_factory=lambda: ["approver"])
    channels: list["RecipientChannel"] = Field(default_factory=list)


class RecipientChannel(BaseModel):
    type: str                       # telegram | whatsapp | console | inbox
    address: Optional[str] = None   # Telegram chat id, WhatsApp number (digits), ...


Recipient.model_rebuild()


class DeliveryResult(BaseModel):
    ok: bool
    external_id: Optional[str] = None   # e.g. Telegram message_id, WhatsApp message id
    error: Optional[str] = None


class InboundEvent(BaseModel):
    """A user action coming back from a channel (button press, command)."""

    channel: str
    sender: str                     # chat id / phone number as the channel reports it
    approval_id: Optional[str] = None
    action: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)
    ack_token: Optional[str] = None  # e.g. Telegram callback_query id, to dismiss the spinner
    text: Optional[str] = None
    reply_to: Optional[str] = None   # the channel's id of the message the person replied to (so "why this quantity?" can be tied to a request)
    external_id: Optional[str] = None   # the id the channel gave THIS message (WhatsApp's message id, Telegram's update id): a message delivered again has the same one


class DecisionRefused(Exception):
    """Raised by a kind's handler: this answer cannot be applied, and trying again will not help. The whole decision rolls back and the person is told why."""


class DecisionResult(BaseModel):
    ok: bool
    status: str                     # decided | info | revised | need_input | refused | already_decided | expired | cancelled | invalid_action | forbidden | not_found | error
    message: str
    approval: Optional[ApprovalRecord] = None
    info_sections: list[Section] = Field(default_factory=list)
    prompt: str = ""                # need_input: what to ask the person (a number to type, a vendor to pick)
    choices: list[dict[str, str]] = Field(default_factory=list)      # need_input: the options, [{"id": ..., "label": ...}]
    # Work held back until the caller has sent its reply (only when decide(..., defer=True)): the revised request that follows "Done: ... a revised request is on its way".
    # The caller must run it, once, after replying. Never part of what is stored or returned over the API.
    after: Optional[Callable[[], None]] = Field(default=None, exclude=True, repr=False)

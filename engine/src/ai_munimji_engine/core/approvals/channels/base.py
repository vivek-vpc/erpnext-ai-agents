"""The one interface every approval channel implements.

To add a channel (Slack, email, your own mobile app push, ...):
  1. subclass Channel and implement `send_approval` (and `send_text`);
  2. if it can receive button presses, implement `verify_webhook` + `parse_webhook`
     (the generic `/webhooks/<type>` route calls them);
  3. register it in `registry.CHANNEL_TYPES`.
Nothing in the agents or the approval service changes.
"""
from __future__ import annotations

import hmac
from abc import ABC, abstractmethod
from typing import Any, Mapping, Optional

from ..models import ApprovalRecord, DeliveryResult, InboundEvent

CALLBACK_PREFIX = "ap"


def encode_action(approval_id: str, action: str) -> str:
    """Compact id for buttons. Telegram allows 64 bytes of callback_data; this stays well under."""
    return f"{CALLBACK_PREFIX}|{approval_id}|{action}"


def decode_action(data: str) -> Optional[tuple[str, str]]:
    parts = (data or "").split("|")
    if len(parts) == 3 and parts[0] == CALLBACK_PREFIX and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def safe_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class Channel(ABC):
    type: str = "base"
    supports_webhook: bool = False
    needs_address: bool = False         # True: a recipient must give a chat id / phone number on this channel, else it is skipped (and reported at start-up)
    echoes_outcome: bool = False        # True: update_outcome already posts the outcome text under the request message, so the service need not send it again

    def address_problem(self, address: Optional[str]) -> Optional[str]:
        """Why this recipient address cannot work on this channel (None = fine). Reported once at start-up instead of failing on every request."""
        return "has no address" if self.needs_address and not (address or "").strip() else None

    def handle_command(self, event: InboundEvent, known: bool) -> bool:
        """Optional: answer a plain-text command (e.g. /start) before the sender is checked against the recipients. True = handled, nothing else to do."""
        return False

    @abstractmethod
    def send_approval(self, record: ApprovalRecord, address: Optional[str]) -> DeliveryResult:
        """Deliver the request with its choices."""

    @abstractmethod
    def send_text(self, address: Optional[str], text: str) -> DeliveryResult:
        """Deliver a plain notification (replies, reminders, detail views)."""

    def send_reminder(self, record: ApprovalRecord, address: Optional[str]) -> DeliveryResult:
        return self.send_text(
            address,
            f"Reminder: {record.request.headline}\nStill waiting for your decision (request {record.id}). Scroll up to the original message to answer.",
        )

    def update_outcome(self, record: ApprovalRecord, external_id: Optional[str], address: Optional[str], text: str) -> None:
        """Optional: mark an already-delivered message as decided (e.g. remove its buttons)."""

    # ---- inbound (button presses) -------------------------------------------------------------
    def verify_webhook(self, headers: Mapping[str, str], query: Mapping[str, str], body: bytes) -> bool:
        return False

    def handle_verification(self, query: Mapping[str, str]) -> Optional[tuple[int, str]]:
        """GET handshake used by some platforms (WhatsApp). Return (status, body) or None."""
        return None

    def parse_webhook(self, payload: dict[str, Any]) -> list[InboundEvent]:
        return []

    def acknowledge(self, event: InboundEvent, text: str) -> None:
        """Answer the user after we processed their action."""
        self.send_text(event.sender, text)

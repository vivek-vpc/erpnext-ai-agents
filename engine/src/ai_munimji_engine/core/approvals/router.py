"""Who gets asked, over which channels, and who is allowed to answer."""
from __future__ import annotations

from typing import Optional

from ..common import Actor
from .channels.base import Channel
from .models import Recipient, RecipientChannel


class ApprovalRouter:
    """strategy = "all"      -> send over every channel configured for each recipient
       strategy = "failover" -> try a recipient's channels in order until one succeeds"""

    def __init__(self, channels: dict[str, Channel], recipients: list[Recipient], strategy: str = "all") -> None:
        if strategy not in ("all", "failover"):
            raise ValueError("strategy must be 'all' or 'failover'")
        self.channels = channels
        self.recipients = recipients
        self.strategy = strategy

    def targets(self) -> list[tuple[Recipient, list[RecipientChannel]]]:
        out = []
        for r in self.recipients:
            if not ({"approver", "admin"} & set(r.roles)):
                continue
            usable = [c for c in r.channels if c.type in self.channels and self.channels[c.type].address_problem(c.address) is None]
            if usable:
                out.append((r, usable))
        return out

    def recipient_for(self, channel_type: str, sender: str) -> Optional[Recipient]:
        """Map a channel-specific sender id (chat id / phone) to a configured person. Unknown senders -> None."""
        sender = _norm(sender)
        for r in self.recipients:
            for c in r.channels:
                if c.type == channel_type and c.address and _norm(c.address) == sender:
                    return r
        return None

    @staticmethod
    def actor_for(recipient: Recipient, via: str) -> Actor:
        return Actor(id=recipient.id, roles=frozenset(recipient.roles), via=via)


def _norm(addr: str) -> str:
    return "".join(ch for ch in str(addr) if ch.isalnum())

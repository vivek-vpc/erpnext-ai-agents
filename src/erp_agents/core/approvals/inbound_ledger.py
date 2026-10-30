"""The ids of the messages the channels have delivered, so that one delivered again is recognised (Phase 3 of the hardening review).

WhatsApp and Telegram deliver a webhook again when the answer was slow or failed, and Telegram hands out an update again after a restart that lost its offset. Each message
carries an id from the channel; `claim` records it and says whether this is the first time. A repository: the only code that reads or writes `inbound_seen`. In a Frappe app
this is a DocType whose name is the (channel, id) pair, and `claim` is an insert that fails when the name exists.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from ..common import Clock, iso, utcnow
from ..storage import Database

log = logging.getLogger(__name__)
MAX_ID = 200


class InboundLedger:
    def __init__(self, db: Database, clock: Clock = utcnow, memory_days: int = 7) -> None:
        self.db, self.clock, self.memory_days = db, clock, memory_days

    def claim(self, channel: str, external_id: Optional[str]) -> bool:
        """True the first time this id is seen on this channel, False when it was seen before. A message without an id cannot be recognised and is never a duplicate. Atomic:
        two deliveries of the same message arriving together cannot both get True."""
        if not external_id:
            return True
        cur = self.db.execute("INSERT OR IGNORE INTO inbound_seen(channel, external_id, seen_at) VALUES (?, ?, ?)", (channel, str(external_id)[:MAX_ID], iso(self.clock())))
        return cur.rowcount == 1

    def release(self, channel: str, external_id: Optional[str]) -> None:
        """Give an id back: the message was not handled after all, so a delivery that comes again may be tried again."""
        if external_id:
            self.db.execute("DELETE FROM inbound_seen WHERE channel=? AND external_id=?", (channel, str(external_id)[:MAX_ID]))

    def purge(self, days: Optional[int] = None) -> int:
        """Forget the ids older than `days` (the setting `resilience.inbound_memory_days`). A channel does not redeliver after that long. Returns how many were forgotten."""
        cutoff = iso(self.clock() - timedelta(days=days if days is not None else self.memory_days))
        return self.db.execute("DELETE FROM inbound_seen WHERE seen_at < ?", (cutoff,)).rowcount

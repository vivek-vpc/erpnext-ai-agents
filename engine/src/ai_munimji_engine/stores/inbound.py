"""The ids of the messages a channel has delivered (the table `inbound_seen`): a channel delivers a webhook again when the answer was slow, and the rule logic must recognise
the second delivery. `InboundLedger` (core/approvals/inbound_ledger.py) is the domain code; this is the storage it needs."""
from __future__ import annotations

from typing import ContextManager, Protocol

from ..storage import MemoryDb


class InboundSeenStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def claim(self, channel: str, external_id: str, seen_at: str) -> bool:
        """Record the id. True the first time (channel, id) is seen, False when it was seen before. Atomic: two deliveries arriving together cannot both get True.
        In a Frappe site this is an insert into a DocType whose name is the pair, and the duplicate-name error is the False."""

    def release(self, channel: str, external_id: str) -> None:
        """Forget one id (the message was not handled after all), so a delivery that comes again may be tried again."""

    def purge_before(self, cutoff: str) -> int:
        """Forget the ids first seen before `cutoff` (ISO text). Returns how many were forgotten."""


class MemoryInboundSeenStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def claim(self, channel: str, external_id: str, seen_at: str) -> bool:
        with self.db.lock:
            if any(r["channel"] == channel and r["external_id"] == external_id for r in self.db.rows("inbound_seen")):
                return False
            self.db.insert("inbound_seen", {"channel": channel, "external_id": external_id, "seen_at": seen_at})
            return True

    def release(self, channel: str, external_id: str) -> None:
        with self.db.lock:
            self.db.rows("inbound_seen")[:] = [r for r in self.db.rows("inbound_seen") if not (r["channel"] == channel and r["external_id"] == external_id)]

    def purge_before(self, cutoff: str) -> int:
        with self.db.lock:
            rows = self.db.rows("inbound_seen")
            kept = [r for r in rows if not r["seen_at"] < cutoff]
            gone = len(rows) - len(kept)
            rows[:] = kept
            return gone

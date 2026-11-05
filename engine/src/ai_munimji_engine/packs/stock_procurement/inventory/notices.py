"""Stock alerts: information-only notices ("low stock", "needed urgently") sent BEFORE any request to reorder.

An alert asks nothing of the owner except to acknowledge it. It is remembered per item so the same problem is not announced over and over:
one alert per episode, a worse level (low -> urgent) is announced at once, a still-low item is repeated only after a few days, and
everything is cleared when the item recovers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ai_munimji_engine.stores.stock.notices import NoticeStore

from ai_munimji_engine.core.common import Clock, iso, parse_dt, short_id, utcnow

LEVELS = {"LOW": 0, "URGENT": 1}
ACTIVE = ("sent", "acknowledged", "snoozed")


@dataclass
class Notice:
    id: str
    item_code: str
    level: str
    status: str
    created_at: datetime
    approval_id: Optional[str]
    snoozed_until: Optional[datetime]


class NoticeRepo:
    def __init__(self, store: NoticeStore, clock: Clock = utcnow) -> None:
        self.store, self.clock = store, clock

    def create(self, item_code: str, level: str, approval_id: Optional[str] = None) -> Notice:
        now, nid = iso(self.clock()), short_id(10)
        self.store.insert(nid, item_code, level, now, approval_id)
        return self.get(nid)                                                       # type: ignore[return-value]

    def get(self, nid: str) -> Optional[Notice]:
        r = self.store.get(nid)
        return _row(r) if r else None

    def by_approval(self, approval_id: str) -> Optional[Notice]:
        r = self.store.by_approval(approval_id)
        return _row(r) if r else None

    def latest_active(self, item_code: str) -> Optional[Notice]:
        r = self.store.latest_active(item_code, ACTIVE)
        return _row(r) if r else None

    def active_for_item(self, item_code: str) -> list[Notice]:
        return [_row(r) for r in self.store.active_for_item(item_code, ACTIVE)]

    def set_approval(self, nid: str, approval_id: str) -> None:
        self.store.set_approval(nid, approval_id, iso(self.clock()))

    def set_status(self, nid: str, status: str, snoozed_until: Optional[datetime] = None) -> None:
        self.store.set_status(nid, status, iso(self.clock()), iso(snoozed_until) if snoozed_until else None)

    def list(self, limit: int = 100) -> list[Notice]:
        return [_row(r) for r in self.store.list(limit)]


def _row(r) -> Notice:
    return Notice(id=r["id"], item_code=r["item_code"], level=r["level"], status=r["status"], created_at=parse_dt(r["created_at"]),
                  approval_id=r["approval_id"], snoozed_until=parse_dt(r["snoozed_until"]))

"""Work-order queue: how agents hand tasks to each other.

The inventory agent emits REORDER_NEEDED after the owner approves; the procurement agent (next
module) claims it, picks a vendor and asks for the PO approval. Plain table + typed payloads, no
broker needed at this scale. `subscribe()` lets in-process agents react immediately.
"""
from __future__ import annotations

import logging

from typing import Any, Callable, Optional

from ai_munimji_engine.stores.workorders import WorkOrderStore

from .common import Clock, iso, short_id, utcnow
from ai_munimji_engine.storage import dumps, loads

log = logging.getLogger(__name__)

REORDER_NEEDED = "REORDER_NEEDED"


class WorkOrderQueue:
    def __init__(self, store: WorkOrderStore, clock: Clock = utcnow) -> None:
        self.store = store
        self.clock = clock
        self._subs: list[Callable[[dict[str, Any]], None]] = []

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._subs.append(fn)

    def emit(self, type_: str, payload: dict[str, Any], source: str = "") -> dict[str, Any]:
        now = iso(self.clock())
        wo_id = short_id(10)
        self.store.insert(wo_id, type_, dumps(payload), source, now)
        wo = self.get(wo_id)
        for fn in self._subs:
            try:
                fn(wo)
            except Exception:  # noqa: BLE001 - a bad subscriber must never undo an approved decision
                log.exception("a work-order subscriber failed")
        return wo

    def get(self, wo_id: str) -> Optional[dict[str, Any]]:
        r = self.store.get(wo_id)
        return _row(r) if r else None

    def list(self, status: Optional[str] = None, type_: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        return [_row(r) for r in self.store.list(status, type_, limit)]

    def set_status(self, wo_id: str, status: str) -> None:
        self.store.set_status(wo_id, status, iso(self.clock()))


def _row(r) -> dict[str, Any]:
    return {"id": r["id"], "type": r["type"], "status": r["status"], "payload": loads(r["payload_json"]),
            "source": r["source"], "created_at": r["created_at"], "updated_at": r["updated_at"]}

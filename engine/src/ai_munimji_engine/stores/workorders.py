"""The hand-off queue between the agents (the table `work_orders`). `WorkOrderQueue` (core/workorders.py) is the domain code; this is the storage it needs."""
from __future__ import annotations

from typing import ContextManager, Optional, Protocol

from ..storage import MemoryDb, Row


class WorkOrderStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def insert(self, wo_id: str, type_: str, payload_json: str, source: str, now: str) -> None:
        """A new work order: status 'pending', created and updated at `now`."""

    def get(self, wo_id: str) -> Optional[Row]:
        """The row (id, type, status, payload_json, source, created_at, updated_at), or None."""

    def list(self, status: Optional[str], type_: Optional[str], limit: int) -> list[Row]:
        """Newest first by `created_at` (orders made at the same instant keep the order they were made in), optionally of one status and one type."""

    def set_status(self, wo_id: str, status: str, now: str) -> None:
        """Change the status and `updated_at`. An unknown id changes nothing."""


class MemoryWorkOrderStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def insert(self, wo_id: str, type_: str, payload_json: str, source: str, now: str) -> None:
        with self.db.lock:
            if any(r["id"] == wo_id for r in self.db.rows("work_orders")):
                raise ValueError(f"work order {wo_id} exists")          # the table's primary key
            self.db.insert("work_orders", {"id": wo_id, "type": type_, "status": "pending", "payload_json": payload_json, "source": source, "created_at": now, "updated_at": now})

    def get(self, wo_id: str) -> Optional[Row]:
        with self.db.lock:
            row = next((r for r in self.db.rows("work_orders") if r["id"] == wo_id), None)
            return dict(row) if row else None

    def list(self, status: Optional[str], type_: Optional[str], limit: int) -> list[Row]:
        with self.db.lock:
            rows = [r for r in self.db.rows("work_orders") if (not status or r["status"] == status) and (not type_ or r["type"] == type_)]
            return [dict(r) for r in sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]]

    def set_status(self, wo_id: str, status: str, now: str) -> None:
        with self.db.lock:
            row = next((r for r in self.db.rows("work_orders") if r["id"] == wo_id), None)
            if row is not None:
                row["status"], row["updated_at"] = status, now

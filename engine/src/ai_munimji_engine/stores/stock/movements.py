"""The storage of stock movements and counts: requests to move stock between warehouses (`stock_transfers`) and the cycle-count plan and reviews (`cycle_counts`,
`count_reviews`). `StockTransfers` and `CycleCounts` (packs/stock_procurement/inventory) are the domain code; these are the stores they need.

A row of `stock_transfers`: id, item_code, qty, uom, from_warehouse, to_warehouse, status (proposed | created | rejected | expired | cancelled), entry_name, reason,
requested_by, approval_id, status_note, created_at, updated_at. A row of `cycle_counts`: item_code, abc_class, last_counted_on, due_on, planned_on, updated_at.
A row of `count_reviews`: document, item_code, warehouse, variance, approval_id, reviewed_at."""
from __future__ import annotations

from typing import Any, ContextManager, Mapping, Optional, Protocol

from ...storage import MemoryDb, Row

TRANSFER_COLUMNS = frozenset({"status", "status_note", "approval_id", "entry_name", "updated_at"})      # what `TransferStore.update` may set


class TransferStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def insert(self, transfer_id: str, item_code: str, qty: float, uom: str, from_warehouse: str, to_warehouse: str, reason: Optional[str], requested_by: str, now: str) -> None:
        """A new request: status 'proposed'."""

    def get(self, transfer_id: str) -> Optional[Row]: ...

    def by_approval(self, approval_id: str) -> Optional[Row]: ...

    def list(self, status: Optional[str], limit: int) -> list[Row]:
        """Newest first, optionally of one status. (Requests made at the same instant keep the order they were made in.)"""

    def update(self, transfer_id: str, fields: Mapping[str, Any]) -> None:
        """Set the named columns (only those in TRANSFER_COLUMNS); the values are stored as given."""


class MemoryTransferStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def _find(self, transfer_id: str) -> Optional[Row]:
        return next((r for r in self.db.rows("stock_transfers") if r["id"] == transfer_id), None)

    def insert(self, transfer_id: str, item_code: str, qty: float, uom: str, from_warehouse: str, to_warehouse: str, reason: Optional[str], requested_by: str, now: str) -> None:
        with self.db.lock:
            if self._find(transfer_id) is not None:
                raise ValueError(f"transfer {transfer_id} exists")
            self.db.insert("stock_transfers", {"id": transfer_id, "item_code": item_code, "qty": qty, "uom": uom, "from_warehouse": from_warehouse, "to_warehouse": to_warehouse,
                                               "status": "proposed", "entry_name": None, "reason": reason, "requested_by": requested_by, "approval_id": None,
                                               "status_note": None, "created_at": now, "updated_at": now})

    def get(self, transfer_id: str) -> Optional[Row]:
        with self.db.lock:
            row = self._find(transfer_id)
            return dict(row) if row else None

    def by_approval(self, approval_id: str) -> Optional[Row]:
        with self.db.lock:
            row = next((r for r in self.db.rows("stock_transfers") if r["approval_id"] == approval_id), None)
            return dict(row) if row else None

    def list(self, status: Optional[str], limit: int) -> list[Row]:
        with self.db.lock:
            rows = [r for r in self.db.rows("stock_transfers") if not status or r["status"] == status]
            return [dict(r) for r in sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]]

    def update(self, transfer_id: str, fields: Mapping[str, Any]) -> None:
        unknown = set(fields) - TRANSFER_COLUMNS
        if unknown:
            raise ValueError(f"cannot set {', '.join(sorted(unknown))}")
        with self.db.lock:
            row = self._find(transfer_id)
            if row is not None:
                row.update(fields)


class CountStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def upsert_schedule(self, item_code: str, abc_class: str, last_counted_on: Optional[str], due_on: str, now: str) -> None:
        """Make or update the item's row: class, when it was last counted, when it is due and `updated_at`; `planned_on` is left as it is."""

    def schedule(self, item_code: str) -> Optional[Row]: ...

    def mark_planned(self, item_code: str, planned_on: str) -> None: ...

    def reviewed(self, document: str, item_code: str, warehouse: str) -> bool: ...

    def add_review(self, document: str, item_code: str, warehouse: str, variance: float, approval_id: Optional[str], now: str) -> None:
        """Remember that the line was reviewed; a line already remembered is left as it is."""


class MemoryCountStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def _find(self, item_code: str) -> Optional[Row]:
        return next((r for r in self.db.rows("cycle_counts") if r["item_code"] == item_code), None)

    def upsert_schedule(self, item_code: str, abc_class: str, last_counted_on: Optional[str], due_on: str, now: str) -> None:
        with self.db.lock:
            row = self._find(item_code)
            if row is None:
                self.db.insert("cycle_counts", {"item_code": item_code, "abc_class": abc_class, "last_counted_on": last_counted_on, "due_on": due_on, "planned_on": None,
                                                "updated_at": now})
            else:
                row.update(abc_class=abc_class, last_counted_on=last_counted_on, due_on=due_on, updated_at=now)

    def schedule(self, item_code: str) -> Optional[Row]:
        with self.db.lock:
            row = self._find(item_code)
            return dict(row) if row else None

    def mark_planned(self, item_code: str, planned_on: str) -> None:
        with self.db.lock:
            row = self._find(item_code)
            if row is not None:
                row["planned_on"] = planned_on

    def _review(self, document: str, item_code: str, warehouse: str) -> Optional[Row]:
        return next((r for r in self.db.rows("count_reviews") if r["document"] == document and r["item_code"] == item_code and r["warehouse"] == warehouse), None)

    def reviewed(self, document: str, item_code: str, warehouse: str) -> bool:
        with self.db.lock:
            return self._review(document, item_code, warehouse) is not None

    def add_review(self, document: str, item_code: str, warehouse: str, variance: float, approval_id: Optional[str], now: str) -> None:
        with self.db.lock:
            if self._review(document, item_code, warehouse) is None:
                self.db.insert("count_reviews", {"document": document, "item_code": item_code, "warehouse": warehouse, "variance": variance, "approval_id": approval_id,
                                                 "reviewed_at": now})

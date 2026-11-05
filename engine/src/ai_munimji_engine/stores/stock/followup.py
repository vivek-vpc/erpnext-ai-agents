"""The storage of the purchasing follow-up: late-delivery chases (`delivery_chases`), goods receipt checks (`receipt_checks`) and quote rounds (`quote_rounds`).
`DeliveryChaser`, `ReceiptChecker` and `QuoteRounds` (packs/stock_procurement/followup) are the domain code; these are the stores they need.

A row of `delivery_chases`: po_name, supplier, item_code, status (open | arrived | dismissed), first_late_on, asked_at, asks, sent_at, sends, approval_id, updated_at.
A row of `receipt_checks`: receipt, supplier, status (clean | flagged | acknowledged), problems_json, approval_id, checked_at.
A row of `quote_rounds`: id, item_code, qty, uom, suppliers_json, status (proposed | asked | compared | ordered | closed), reply_by, asked_at, approval_id, comparison_id,
requested_by, status_note, created_at, updated_at."""
from __future__ import annotations

from typing import Any, ContextManager, Mapping, Optional, Protocol

from ...storage import MemoryDb, Row

QUOTE_COLUMNS = frozenset({"status", "status_note", "approval_id", "comparison_id", "asked_at", "reply_by", "suppliers_json", "updated_at"})      # what `QuoteStore.update` may set


class ChaseStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def get(self, po_name: str) -> Optional[Row]: ...

    def start(self, po_name: str, supplier: str, item_code: str, first_late_on: str, asked_at: str, approval_id: str, now: str) -> None:
        """A new chase, status 'open', asked once."""

    def reopen(self, po_name: str, asked_at: str, approval_id: str, now: str) -> None:
        """Ask again: status 'open', `asks` up by one, the new request."""

    def open_chases(self) -> list[Row]:
        """Every chase whose status is 'open', in the order they were started."""

    def set_status(self, po_name: str, status: str, now: str) -> None: ...

    def record_send(self, po_name: str, now: str) -> None:
        """A reminder went to the vendor: `sent_at` = `now`, `sends` up by one."""


class MemoryChaseStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def _find(self, po_name: str) -> Optional[Row]:
        return next((r for r in self.db.rows("delivery_chases") if r["po_name"] == po_name), None)

    def get(self, po_name: str) -> Optional[Row]:
        with self.db.lock:
            row = self._find(po_name)
            return dict(row) if row else None

    def start(self, po_name: str, supplier: str, item_code: str, first_late_on: str, asked_at: str, approval_id: str, now: str) -> None:
        with self.db.lock:
            if self._find(po_name) is not None:
                raise ValueError(f"chase {po_name} exists")
            self.db.insert("delivery_chases", {"po_name": po_name, "supplier": supplier, "item_code": item_code, "status": "open", "first_late_on": first_late_on,
                                               "asked_at": asked_at, "asks": 1, "sent_at": None, "sends": 0, "approval_id": approval_id, "updated_at": now})

    def reopen(self, po_name: str, asked_at: str, approval_id: str, now: str) -> None:
        with self.db.lock:
            row = self._find(po_name)
            if row is not None:
                row.update(status="open", asked_at=asked_at, asks=row["asks"] + 1, approval_id=approval_id, updated_at=now)

    def open_chases(self) -> list[Row]:
        with self.db.lock:
            return [dict(r) for r in self.db.rows("delivery_chases") if r["status"] == "open"]

    def set_status(self, po_name: str, status: str, now: str) -> None:
        with self.db.lock:
            row = self._find(po_name)
            if row is not None:
                row["status"], row["updated_at"] = status, now

    def record_send(self, po_name: str, now: str) -> None:
        with self.db.lock:
            row = self._find(po_name)
            if row is not None:
                row.update(sent_at=now, sends=row["sends"] + 1, updated_at=now)


class ReceiptStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def get(self, receipt: str) -> Optional[Row]: ...

    def add(self, receipt: str, supplier: str, status: str, problems_json: str, approval_id: Optional[str], checked_at: str) -> None: ...

    def problems_since(self, since: str) -> list[Row]:
        """supplier and problems_json of every check made at or after `since`."""

    def acknowledge(self, approval_id: str) -> None:
        """The checks that carry this request become 'acknowledged'."""


class MemoryReceiptStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def get(self, receipt: str) -> Optional[Row]:
        with self.db.lock:
            row = next((r for r in self.db.rows("receipt_checks") if r["receipt"] == receipt), None)
            return dict(row) if row else None

    def add(self, receipt: str, supplier: str, status: str, problems_json: str, approval_id: Optional[str], checked_at: str) -> None:
        with self.db.lock:
            if any(r["receipt"] == receipt for r in self.db.rows("receipt_checks")):
                raise ValueError(f"receipt {receipt} was checked")
            self.db.insert("receipt_checks", {"receipt": receipt, "supplier": supplier, "status": status, "problems_json": problems_json, "approval_id": approval_id,
                                              "checked_at": checked_at})

    def problems_since(self, since: str) -> list[Row]:
        with self.db.lock:
            return [{"supplier": r["supplier"], "problems_json": r["problems_json"]} for r in self.db.rows("receipt_checks") if r["checked_at"] >= since]

    def acknowledge(self, approval_id: str) -> None:
        with self.db.lock:
            for r in self.db.rows("receipt_checks"):
                if r["approval_id"] == approval_id:
                    r["status"] = "acknowledged"


class QuoteStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def insert(self, round_id: str, item_code: str, qty: float, uom: str, suppliers_json: str, reply_by: str, requested_by: str, now: str) -> None:
        """A new round: status 'proposed'."""

    def get(self, round_id: str) -> Optional[Row]: ...

    def ids(self, status: Optional[str], limit: int) -> list[str]:
        """The ids of the newest rounds first, optionally of one status. (Rounds made at the same instant keep the order they were made in.)"""

    def update(self, round_id: str, fields: Mapping[str, Any]) -> None:
        """Set the named columns (only those in QUOTE_COLUMNS); the values are stored as given."""


class MemoryQuoteStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def _find(self, round_id: str) -> Optional[Row]:
        return next((r for r in self.db.rows("quote_rounds") if r["id"] == round_id), None)

    def insert(self, round_id: str, item_code: str, qty: float, uom: str, suppliers_json: str, reply_by: str, requested_by: str, now: str) -> None:
        with self.db.lock:
            if self._find(round_id) is not None:
                raise ValueError(f"round {round_id} exists")
            self.db.insert("quote_rounds", {"id": round_id, "item_code": item_code, "qty": qty, "uom": uom, "suppliers_json": suppliers_json, "status": "proposed",
                                            "reply_by": reply_by, "asked_at": None, "approval_id": None, "comparison_id": None, "requested_by": requested_by,
                                            "status_note": None, "created_at": now, "updated_at": now})

    def get(self, round_id: str) -> Optional[Row]:
        with self.db.lock:
            row = self._find(round_id)
            return dict(row) if row else None

    def ids(self, status: Optional[str], limit: int) -> list[str]:
        with self.db.lock:
            rows = [r for r in self.db.rows("quote_rounds") if not status or r["status"] == status]
            return [r["id"] for r in sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]]

    def update(self, round_id: str, fields: Mapping[str, Any]) -> None:
        unknown = set(fields) - QUOTE_COLUMNS
        if unknown:
            raise ValueError(f"cannot set {', '.join(sorted(unknown))}")
        with self.db.lock:
            row = self._find(round_id)
            if row is not None:
                row.update(fields)

"""Purchase-order proposals (the table `proposals`): what the vendor selector recommended, what the owner decided and the order it led to. `ProposalRepo`
(packs/stock_procurement/procurement/proposals.py) is the domain code; this is the storage it needs.

A row: id, work_order_id, item_code, status, approval_id, supplier, supplier_name, qty, rate, total, po_name, ranking_json, status_note, snoozed_until, created_at, updated_at."""
from __future__ import annotations

from typing import Any, ContextManager, Mapping, Optional, Protocol, Sequence

from ...storage import MemoryDb, Row

UPDATABLE = frozenset({"po_name", "snoozed_until"})            # the columns `update_status` may set besides the status, the note and `updated_at`


class ProposalStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def insert(self, proposal_id: str, work_order_id: str, item_code: str, status: str, supplier: Optional[str], supplier_name: Optional[str], qty: Optional[float],
               rate: Optional[float], total: Optional[float], ranking_json: Optional[str], note: str, now: str) -> None: ...

    def get(self, proposal_id: str) -> Optional[Row]: ...

    def by_approval(self, approval_id: str) -> Optional[Row]: ...

    def open_for_item(self, item_code: str, statuses: Sequence[str]) -> list[Row]:
        """The item's proposals whose status is one of `statuses`, in the order they were made."""

    def snoozed_due(self, now: str) -> list[Row]:
        """Proposals with status 'snoozed' whose `snoozed_until` is set and not after `now`."""

    def list(self, status: Optional[str], item_code: Optional[str], limit: int) -> list[Row]:
        """Newest first, optionally of one status and one item. (Proposals made at the same instant come in no promised order.)"""

    def set_ranking(self, proposal_id: str, ranking_json: str, now: str) -> None: ...

    def set_approval(self, proposal_id: str, approval_id: str, now: str) -> None: ...

    def revise(self, proposal_id: str, supplier: Optional[str], supplier_name: Optional[str], qty: Optional[float], rate: Optional[float], total: Optional[float], note: str,
               now: str) -> None: ...

    def update_status(self, proposal_id: str, status: str, note: str, now: str, extra: Mapping[str, Any]) -> None:
        """Change the status, the note and `updated_at`, and the columns named in `extra` (only `po_name` and `snoozed_until`; the values are stored as given)."""


class MemoryProposalStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def _find(self, proposal_id: str) -> Optional[Row]:
        return next((r for r in self.db.rows("proposals") if r["id"] == proposal_id), None)

    def insert(self, proposal_id: str, work_order_id: str, item_code: str, status: str, supplier: Optional[str], supplier_name: Optional[str], qty: Optional[float],
               rate: Optional[float], total: Optional[float], ranking_json: Optional[str], note: str, now: str) -> None:
        with self.db.lock:
            if self._find(proposal_id) is not None:
                raise ValueError(f"proposal {proposal_id} exists")
            self.db.insert("proposals", {"id": proposal_id, "work_order_id": work_order_id, "item_code": item_code, "status": status, "approval_id": None, "supplier": supplier,
                                         "supplier_name": supplier_name, "qty": qty, "rate": rate, "total": total, "po_name": None, "ranking_json": ranking_json,
                                         "status_note": note, "snoozed_until": None, "created_at": now, "updated_at": now})

    def get(self, proposal_id: str) -> Optional[Row]:
        with self.db.lock:
            row = self._find(proposal_id)
            return dict(row) if row else None

    def by_approval(self, approval_id: str) -> Optional[Row]:
        with self.db.lock:
            row = next((r for r in self.db.rows("proposals") if r["approval_id"] == approval_id), None)
            return dict(row) if row else None

    def open_for_item(self, item_code: str, statuses: Sequence[str]) -> list[Row]:
        with self.db.lock:
            return [dict(r) for r in self.db.rows("proposals") if r["item_code"] == item_code and r["status"] in statuses]

    def snoozed_due(self, now: str) -> list[Row]:
        with self.db.lock:
            return [dict(r) for r in self.db.rows("proposals") if r["status"] == "snoozed" and r["snoozed_until"] is not None and r["snoozed_until"] <= now]

    def list(self, status: Optional[str], item_code: Optional[str], limit: int) -> list[Row]:
        with self.db.lock:
            rows = [r for r in self.db.rows("proposals") if (not status or r["status"] == status) and (not item_code or r["item_code"] == item_code)]
            # SQLite reads the (item_code, created_at) index backwards when the item is given, so equal times come out the later-made first; without the item they keep the order made
            ordered = sorted(enumerate(rows), key=lambda t: (t[1]["created_at"], t[0] if item_code else -t[0]), reverse=True)
            return [dict(r) for _, r in ordered[:limit]]

    def set_ranking(self, proposal_id: str, ranking_json: str, now: str) -> None:
        with self.db.lock:
            row = self._find(proposal_id)
            if row is not None:
                row["ranking_json"], row["updated_at"] = ranking_json, now

    def set_approval(self, proposal_id: str, approval_id: str, now: str) -> None:
        with self.db.lock:
            row = self._find(proposal_id)
            if row is not None:
                row["approval_id"], row["updated_at"] = approval_id, now

    def revise(self, proposal_id: str, supplier: Optional[str], supplier_name: Optional[str], qty: Optional[float], rate: Optional[float], total: Optional[float], note: str,
               now: str) -> None:
        with self.db.lock:
            row = self._find(proposal_id)
            if row is not None:
                row.update(supplier=supplier, supplier_name=supplier_name, qty=qty, rate=rate, total=total, status_note=note, updated_at=now)

    def update_status(self, proposal_id: str, status: str, note: str, now: str, extra: Mapping[str, Any]) -> None:
        unknown = set(extra) - UPDATABLE
        if unknown:
            raise ValueError(f"cannot set {', '.join(sorted(unknown))}")
        with self.db.lock:
            row = self._find(proposal_id)
            if row is not None:
                row.update(status=status, status_note=note, updated_at=now, **extra)

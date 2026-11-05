"""Stock notices (the table `stock_notices`): the information-only "low" and "urgent" messages sent before any request to reorder. `NoticeRepo`
(packs/stock_procurement/inventory/notices.py) is the domain code; this is the storage it needs.

A row: id, item_code, level (LOW | URGENT), status (sent | acknowledged | snoozed | cleared | superseded), created_at, updated_at, approval_id, snoozed_until."""
from __future__ import annotations

from typing import ContextManager, Optional, Protocol, Sequence

from ...storage import MemoryDb, Row


class NoticeStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def insert(self, notice_id: str, item_code: str, level: str, now: str, approval_id: Optional[str]) -> None:
        """A new notice: status 'sent', created and updated at `now`."""

    def get(self, notice_id: str) -> Optional[Row]: ...

    def by_approval(self, approval_id: str) -> Optional[Row]: ...

    def latest_active(self, item_code: str, statuses: Sequence[str]) -> Optional[Row]:
        """The newest notice of the item whose status is one of `statuses` (notices made at the same instant: the one made last)."""

    def active_for_item(self, item_code: str, statuses: Sequence[str]) -> list[Row]:
        """Every notice of the item whose status is one of `statuses`, in the order they were made."""

    def set_approval(self, notice_id: str, approval_id: str, now: str) -> None: ...

    def set_status(self, notice_id: str, status: str, now: str, snoozed_until: Optional[str]) -> None:
        """Change the status and `updated_at`. A `snoozed_until` that is None keeps the one already stored."""

    def list(self, limit: int) -> list[Row]:
        """Newest first (the one made last first among equals)."""


class MemoryNoticeStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def _find(self, notice_id: str) -> Optional[Row]:
        return next((r for r in self.db.rows("stock_notices") if r["id"] == notice_id), None)

    @staticmethod
    def _newest_first(rows: list[Row]) -> list[Row]:
        return [r for _, r in sorted(enumerate(rows), key=lambda t: (t[1]["created_at"], t[0]), reverse=True)]       # ORDER BY created_at DESC, rowid DESC

    def insert(self, notice_id: str, item_code: str, level: str, now: str, approval_id: Optional[str]) -> None:
        with self.db.lock:
            if self._find(notice_id) is not None:
                raise ValueError(f"notice {notice_id} exists")
            self.db.insert("stock_notices", {"id": notice_id, "item_code": item_code, "level": level, "status": "sent", "created_at": now, "updated_at": now,
                                             "approval_id": approval_id, "snoozed_until": None})

    def get(self, notice_id: str) -> Optional[Row]:
        with self.db.lock:
            row = self._find(notice_id)
            return dict(row) if row else None

    def by_approval(self, approval_id: str) -> Optional[Row]:
        with self.db.lock:
            row = next((r for r in self.db.rows("stock_notices") if r["approval_id"] == approval_id), None)
            return dict(row) if row else None

    def latest_active(self, item_code: str, statuses: Sequence[str]) -> Optional[Row]:
        with self.db.lock:
            rows = self._newest_first([r for r in self.db.rows("stock_notices") if r["item_code"] == item_code and r["status"] in statuses])
            return dict(rows[0]) if rows else None

    def active_for_item(self, item_code: str, statuses: Sequence[str]) -> list[Row]:
        with self.db.lock:
            return [dict(r) for r in self.db.rows("stock_notices") if r["item_code"] == item_code and r["status"] in statuses]

    def set_approval(self, notice_id: str, approval_id: str, now: str) -> None:
        with self.db.lock:
            row = self._find(notice_id)
            if row is not None:
                row["approval_id"], row["updated_at"] = approval_id, now

    def set_status(self, notice_id: str, status: str, now: str, snoozed_until: Optional[str]) -> None:
        with self.db.lock:
            row = self._find(notice_id)
            if row is not None:
                row["status"], row["updated_at"] = status, now
                if snoozed_until is not None:
                    row["snoozed_until"] = snoozed_until

    def list(self, limit: int) -> list[Row]:
        with self.db.lock:
            return [dict(r) for r in self._newest_first(list(self.db.rows("stock_notices")))[:limit]]

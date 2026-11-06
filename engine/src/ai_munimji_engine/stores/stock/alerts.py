"""Stock alerts and their lifecycle (the table `alerts`). `AlertRepo` (packs/stock_procurement/inventory/alerts.py) is the domain code; this is the storage it needs.

A row: id, item_code, kind (BREACH | EARLY_WARNING), severity, status, created_at, updated_at, analysis_json, reasoning_text, approval_id, snoozed_until, suppress_until,
cooldown_until, status_note, run_id. Alerts made at the same instant are listed with the one made last first, where the text below says "newest"."""
from __future__ import annotations

from typing import ContextManager, Optional, Protocol, Sequence

from ...storage import MemoryDb, Row


class AlertStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def insert(self, alert_id: str, item_code: str, kind: str, severity: int, now: str, analysis_json: str, reasoning_text: str, run_id: Optional[str]) -> None:
        """A new alert: status 'awaiting', created and updated at `now`."""

    def get(self, alert_id: str) -> Optional[Row]: ...

    def latest_for_item(self, item_code: str) -> Optional[Row]:
        """The newest alert of the item, any status."""

    def open_for_item(self, item_code: str, statuses: Sequence[str]) -> list[Row]:
        """The item's alerts whose status is one of `statuses`, in the order they were made."""

    def list(self, status: Optional[str], item_code: Optional[str], limit: int) -> list[Row]:
        """Newest first, optionally of one status and one item."""

    def set_approval(self, alert_id: str, approval_id: str, now: str) -> None: ...

    def update_status(self, alert_id: str, status: str, note: str, now: str, snoozed_until: Optional[str], suppress_until: Optional[str], cooldown_until: Optional[str]) -> None:
        """Change the status, the note and `updated_at`. A date that is None keeps the one already stored; it cannot be cleared."""

    def set_reasoning(self, alert_id: str, text: str) -> None: ...

    def by_approval(self, approval_id: str) -> Optional[Row]:
        """The alert that carries this request (the first, if there are several)."""


class MemoryAlertStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def _find(self, alert_id: str) -> Optional[Row]:
        return next((r for r in self.db.rows("alerts") if r["id"] == alert_id), None)

    def insert(self, alert_id: str, item_code: str, kind: str, severity: int, now: str, analysis_json: str, reasoning_text: str, run_id: Optional[str]) -> None:
        with self.db.lock:
            if self._find(alert_id) is not None:
                raise ValueError(f"alert {alert_id} exists")
            self.db.insert("alerts", {"id": alert_id, "item_code": item_code, "kind": kind, "severity": severity, "status": "awaiting", "created_at": now, "updated_at": now,
                                      "analysis_json": analysis_json, "reasoning_text": reasoning_text, "approval_id": None, "snoozed_until": None, "suppress_until": None,
                                      "cooldown_until": None, "status_note": None, "run_id": run_id})

    def get(self, alert_id: str) -> Optional[Row]:
        with self.db.lock:
            row = self._find(alert_id)
            return dict(row) if row else None

    @staticmethod
    def _newest_first(rows: list[Row]) -> list[Row]:
        return [r for _, r in sorted(enumerate(rows), key=lambda t: (t[1]["created_at"], t[0]), reverse=True)]       # ORDER BY created_at DESC, rowid DESC

    def latest_for_item(self, item_code: str) -> Optional[Row]:
        with self.db.lock:
            rows = self._newest_first([r for r in self.db.rows("alerts") if r["item_code"] == item_code])
            return dict(rows[0]) if rows else None

    def open_for_item(self, item_code: str, statuses: Sequence[str]) -> list[Row]:
        with self.db.lock:
            return [dict(r) for r in self.db.rows("alerts") if r["item_code"] == item_code and r["status"] in statuses]

    def list(self, status: Optional[str], item_code: Optional[str], limit: int) -> list[Row]:
        with self.db.lock:
            rows = [r for r in self.db.rows("alerts") if (not status or r["status"] == status) and (not item_code or r["item_code"] == item_code)]
            return [dict(r) for r in self._newest_first(rows)[:limit]]

    def set_approval(self, alert_id: str, approval_id: str, now: str) -> None:
        with self.db.lock:
            row = self._find(alert_id)
            if row is not None:
                row["approval_id"], row["updated_at"] = approval_id, now

    def update_status(self, alert_id: str, status: str, note: str, now: str, snoozed_until: Optional[str], suppress_until: Optional[str], cooldown_until: Optional[str]) -> None:
        with self.db.lock:
            row = self._find(alert_id)
            if row is None:
                return
            row["status"], row["status_note"], row["updated_at"] = status, note, now
            for column, value in (("snoozed_until", snoozed_until), ("suppress_until", suppress_until), ("cooldown_until", cooldown_until)):
                if value is not None:
                    row[column] = value

    def set_reasoning(self, alert_id: str, text: str) -> None:
        with self.db.lock:
            row = self._find(alert_id)
            if row is not None:
                row["reasoning_text"] = text

    def by_approval(self, approval_id: str) -> Optional[Row]:
        with self.db.lock:
            row = next((r for r in self.db.rows("alerts") if r["approval_id"] == approval_id), None)
            return dict(row) if row else None

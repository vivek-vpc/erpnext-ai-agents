"""Approval requests and their deliveries on SQLite. The SQL is the SQL `ApprovalService` and `DecisionHistory` used to hold, moved here unchanged; the Protocol is
`ai_munimji_engine.stores.approvals.ApprovalStore`."""
from __future__ import annotations

import sqlite3
from typing import Any, ContextManager, Optional

from ai_munimji_engine.storage import Row

from ..core.storage import Database


def like(term: str) -> str:
    """A LIKE pattern that finds `term` as plain text: the wildcards in it are escaped (the query says ESCAPE with a backslash)."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class SqliteApprovalStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    # ---- requests
    def insert(self, approval_id: str, kind: str, request_json: str, ref_type: Optional[str], ref_id: Optional[str], created_at: str, expires_at: Optional[str]) -> None:
        self.db.execute(
            "INSERT INTO approvals(id, kind, status, request_json, ref_type, ref_id, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (approval_id, kind, "pending", request_json, ref_type, ref_id, created_at, expires_at),
        )

    def get(self, approval_id: str) -> Optional[Row]:
        row = self.db.query_one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        return dict(row) if row else None

    def list(self, status: Optional[str], limit: int) -> list[Row]:
        if status:
            rows = self.db.query("SELECT * FROM approvals WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = self.db.query("SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def latest_of_kind(self, kind: str) -> Optional[Row]:
        row = self.db.query_one("SELECT created_at, status FROM approvals WHERE kind=? ORDER BY created_at DESC LIMIT 1", (kind,))
        return dict(row) if row else None

    def due_for_expiry(self, now: str) -> list[str]:
        return [r["id"] for r in self.db.query("SELECT id FROM approvals WHERE status='pending' AND expires_at IS NOT NULL AND expires_at <= ?", (now,))]

    def decide_if_pending(self, approval_id: str, decision_json: str, decided_at: str) -> bool:
        cur = self.db.execute("UPDATE approvals SET status='decided', decision_json=?, decided_at=? WHERE id=? AND status='pending'", (decision_json, decided_at, approval_id))
        return cur.rowcount == 1

    def cancel_if_pending(self, approval_id: str, note: str) -> bool:
        cur = self.db.execute("UPDATE approvals SET status='cancelled', status_note=? WHERE id=? AND status='pending'", (note, approval_id))
        return cur.rowcount == 1

    def expire_if_pending(self, approval_id: str) -> bool:
        cur = self.db.execute("UPDATE approvals SET status='expired' WHERE id=? AND status='pending'", (approval_id,))
        return cur.rowcount == 1

    def add_reminder(self, approval_id: str, at: str) -> None:
        self.db.execute("UPDATE approvals SET reminder_count=reminder_count+1, last_reminder_at=? WHERE id=?", (at, approval_id))

    def search(self, terms: list[str], kind: Optional[str], status: Optional[str], since: Optional[str], before: Optional[str], limit: int) -> list[Row]:
        sql: list[str] = ["SELECT * FROM approvals WHERE 1=1"]
        params: list[Any] = []
        for t in terms:
            sql.append("AND (request_json LIKE ? ESCAPE '\\' OR COALESCE(decision_json,'') LIKE ? ESCAPE '\\' OR COALESCE(status_note,'') LIKE ? ESCAPE '\\' OR kind LIKE ? ESCAPE '\\' OR id = ?)")
            params += [like(t), like(t), like(t), like(t), t]
        if kind:
            sql.append("AND kind LIKE ? ESCAPE '\\'")
            params.append(like(kind))
        if status:
            sql.append("AND status = ?")
            params.append(status)
        if since:
            sql.append("AND created_at >= ?")
            params.append(since)
        if before:
            sql.append("AND created_at < ?")
            params.append(before)
        sql.append("ORDER BY created_at DESC LIMIT ?")
        params.append(limit)
        return [dict(r) for r in self.db.query(" ".join(sql), params)]

    def decision_texts(self) -> list[str]:
        return [r["decision_json"] for r in self.db.query("SELECT decision_json FROM approvals WHERE decision_json IS NOT NULL")]

    def with_reference(self, ref_type: Optional[str], ref_id: Optional[str], exclude_id: str) -> list[Row]:
        return [dict(r) for r in self.db.query("SELECT id, kind, status, created_at, status_note FROM approvals WHERE ref_type=? AND ref_id=? AND id!=? ORDER BY created_at",
                                               (ref_type, ref_id, exclude_id))]

    # ---- counts
    def pending_summary(self) -> Row:
        return dict(self.db.query_one("SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM approvals WHERE status='pending'"))

    def outcome_counts(self) -> list[Row]:
        try:
            rows = self.db.query("SELECT kind, COALESCE(NULLIF(json_extract(decision_json, '$.action'), ''), status) AS outcome, COUNT(*) AS n "
                                 "FROM approvals WHERE status != 'pending' GROUP BY kind, outcome")
        except sqlite3.OperationalError:                         # a SQLite built without its JSON functions: how they ended, without which answer was given
            rows = self.db.query("SELECT kind, status AS outcome, COUNT(*) AS n FROM approvals WHERE status != 'pending' GROUP BY kind, status")
        return [dict(r) for r in rows]

    def delivery_counts(self) -> list[Row]:
        return [dict(r) for r in self.db.query("SELECT channel, status, COUNT(*) AS n FROM deliveries GROUP BY channel, status")]

    # ---- deliveries
    def add_delivery(self, approval_id: str, recipient_id: str, channel: str, address: Optional[str], status: str, external_id: Optional[str], error: Optional[str],
                     sent_at: str) -> None:
        self.db.execute(
            "INSERT INTO deliveries(approval_id, recipient_id, channel, address, status, external_id, error, sent_at) VALUES (?,?,?,?,?,?,?,?)",
            (approval_id, recipient_id, channel, address, status, external_id, error, sent_at),
        )

    def deliveries(self, approval_id: str, status: Optional[str] = None) -> list[Row]:
        if status:
            rows = self.db.query("SELECT * FROM deliveries WHERE approval_id=? AND status=? ORDER BY id", (approval_id, status))
        else:
            rows = self.db.query("SELECT * FROM deliveries WHERE approval_id=? ORDER BY id", (approval_id,))
        return [dict(r) for r in rows]

    def failed_deliveries(self, max_attempts: int) -> list[Row]:
        return [dict(r) for r in self.db.query(
            "SELECT d.* FROM deliveries d JOIN approvals a ON a.id=d.approval_id "
            "WHERE d.status='failed' AND a.status='pending' AND d.attempts < ?", (max_attempts,),
        )]

    def update_delivery(self, delivery_id: int, status: str, external_id: Optional[str], error: Optional[str], attempts: int, sent_at: str) -> None:
        self.db.execute("UPDATE deliveries SET status=?, external_id=?, error=?, attempts=?, sent_at=? WHERE id=?", (status, external_id, error, attempts, sent_at, delivery_id))

    def approval_for_message(self, channel: str, external_id: str, address: str) -> Optional[str]:
        row = self.db.query_one("SELECT approval_id FROM deliveries WHERE channel=? AND external_id=? AND address=? ORDER BY id DESC LIMIT 1", (channel, external_id, address))
        return row["approval_id"] if row else None

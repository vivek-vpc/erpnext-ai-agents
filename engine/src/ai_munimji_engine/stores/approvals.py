"""Approval requests and the messages that carried them (the tables `approvals` and `deliveries`). `ApprovalService` (core/approvals/service.py) is the domain code: it decides
what may happen and in which order; this is the storage it needs, and the only code that reads or writes those two tables.

A row of `approvals` is a dict with: id, kind, status (pending | decided | expired | cancelled), request_json, decision_json, ref_type, ref_id, created_at, expires_at,
decided_at, last_reminder_at, reminder_count, status_note. A row of `deliveries`: id, approval_id, recipient_id, channel, address, status (sent | failed | given_up),
external_id, error, attempts, sent_at. Timestamps are ISO text and the two JSON columns are JSON text: the service converts. Every method that changes the status of a
request is a *conditional* change that says whether it won (`..._if_pending` returns True for exactly one caller), because several channels can answer at once.
"""
from __future__ import annotations

import json
from typing import Any, ContextManager, Optional, Protocol

from ..storage import MemoryDb, Row


class ApprovalStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    # ---- requests
    def insert(self, approval_id: str, kind: str, request_json: str, ref_type: Optional[str], ref_id: Optional[str], created_at: str, expires_at: Optional[str]) -> None:
        """A new request: status 'pending', no reminders yet."""

    def get(self, approval_id: str) -> Optional[Row]: ...

    def list(self, status: Optional[str], limit: int) -> list[Row]:
        """Newest first by `created_at` (requests made at the same instant keep the order they were made in), optionally of one status."""

    def latest_of_kind(self, kind: str) -> Optional[Row]:
        """The newest request of this kind, any status (its created_at and status are what the caller reads), or None."""

    def due_for_expiry(self, now: str) -> list[str]:
        """The ids of pending requests whose `expires_at` is set and not after `now`."""

    def decide_if_pending(self, approval_id: str, decision_json: str, decided_at: str) -> bool:
        """pending -> decided with the answer. False (nothing written) when the request is not pending any more: another channel answered first."""

    def cancel_if_pending(self, approval_id: str, note: str) -> bool:
        """pending -> cancelled, with the reason as `status_note`. False when it is not pending."""

    def expire_if_pending(self, approval_id: str) -> bool:
        """pending -> expired. False when it is not pending."""

    def add_reminder(self, approval_id: str, at: str) -> None:
        """`reminder_count` up by one, `last_reminder_at` = `at`."""

    def search(self, terms: list[str], kind: Optional[str], status: Optional[str], since: Optional[str], before: Optional[str], limit: int) -> list[Row]:
        """Newest first by `created_at`, at most `limit`. Every term must be found, ignoring case (ASCII), in the request text, the answer text, the note, the kind, or be
        the id; `kind` must be contained in the kind; `status` is exact; `created_at >= since` and `created_at < before` compare as text (both optional)."""

    def decision_texts(self) -> list[str]:
        """The `decision_json` of every request that has an answer."""

    def with_reference(self, ref_type: Optional[str], ref_id: Optional[str], exclude_id: str) -> list[Row]:
        """Other requests about the same thing (same ref_type and ref_id), oldest first by `created_at`: id, kind, status, created_at, status_note."""

    # ---- counts for the monitoring
    def pending_summary(self) -> Row:
        """{'n': how many are pending, 'oldest': the earliest created_at among them or None}."""

    def outcome_counts(self) -> list[Row]:
        """For the requests that are not pending: {'kind', 'outcome', 'n'}, where outcome is the action of the answer, or the status when there is no action."""

    def delivery_counts(self) -> list[Row]:
        """{'channel', 'status', 'n'} over all deliveries."""

    # ---- deliveries
    def add_delivery(self, approval_id: str, recipient_id: str, channel: str, address: Optional[str], status: str, external_id: Optional[str], error: Optional[str],
                     sent_at: str) -> None:
        """Record one message (attempts = 1)."""

    def deliveries(self, approval_id: str, status: Optional[str] = None) -> list[Row]:
        """The messages of one request in the order they were made, optionally of one status."""

    def failed_deliveries(self, max_attempts: int) -> list[Row]:
        """Failed messages of requests that are still pending and have been tried fewer than `max_attempts` times."""

    def update_delivery(self, delivery_id: int, status: str, external_id: Optional[str], error: Optional[str], attempts: int, sent_at: str) -> None: ...

    def approval_for_message(self, channel: str, external_id: str, address: str) -> Optional[str]:
        """The request that was sent as message `external_id` on `channel` to `address` (the latest of its deliveries), or None."""


class MemoryApprovalStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def _find(self, approval_id: str) -> Optional[Row]:
        return next((r for r in self.db.rows("approvals") if r["id"] == approval_id), None)

    # ---- requests
    def insert(self, approval_id: str, kind: str, request_json: str, ref_type: Optional[str], ref_id: Optional[str], created_at: str, expires_at: Optional[str]) -> None:
        with self.db.lock:
            if self._find(approval_id) is not None:
                raise ValueError(f"request {approval_id} exists")            # the table's primary key
            self.db.insert("approvals", {"id": approval_id, "kind": kind, "status": "pending", "request_json": request_json, "decision_json": None, "ref_type": ref_type,
                                         "ref_id": ref_id, "created_at": created_at, "expires_at": expires_at, "decided_at": None, "last_reminder_at": None,
                                         "reminder_count": 0, "status_note": None})

    def get(self, approval_id: str) -> Optional[Row]:
        with self.db.lock:
            row = self._find(approval_id)
            return dict(row) if row else None

    def list(self, status: Optional[str], limit: int) -> list[Row]:
        with self.db.lock:
            rows = [r for r in self.db.rows("approvals") if not status or r["status"] == status]
            return [dict(r) for r in sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]]

    def latest_of_kind(self, kind: str) -> Optional[Row]:
        with self.db.lock:
            rows = sorted((r for r in self.db.rows("approvals") if r["kind"] == kind), key=lambda r: r["created_at"], reverse=True)
            return dict(rows[0]) if rows else None

    def due_for_expiry(self, now: str) -> list[str]:
        with self.db.lock:
            return [r["id"] for r in self.db.rows("approvals") if r["status"] == "pending" and r["expires_at"] is not None and r["expires_at"] <= now]

    def decide_if_pending(self, approval_id: str, decision_json: str, decided_at: str) -> bool:
        with self.db.lock:
            row = self._find(approval_id)
            if row is None or row["status"] != "pending":
                return False
            row["status"], row["decision_json"], row["decided_at"] = "decided", decision_json, decided_at
            return True

    def cancel_if_pending(self, approval_id: str, note: str) -> bool:
        with self.db.lock:
            row = self._find(approval_id)
            if row is None or row["status"] != "pending":
                return False
            row["status"], row["status_note"] = "cancelled", note
            return True

    def expire_if_pending(self, approval_id: str) -> bool:
        with self.db.lock:
            row = self._find(approval_id)
            if row is None or row["status"] != "pending":
                return False
            row["status"] = "expired"
            return True

    def add_reminder(self, approval_id: str, at: str) -> None:
        with self.db.lock:
            row = self._find(approval_id)
            if row is not None:
                row["reminder_count"] += 1
                row["last_reminder_at"] = at

    def search(self, terms: list[str], kind: Optional[str], status: Optional[str], since: Optional[str], before: Optional[str], limit: int) -> list[Row]:
        with self.db.lock:
            out = []
            for r in self.db.rows("approvals"):
                if any(not (_has(r["request_json"], t) or _has(r["decision_json"], t) or _has(r["status_note"], t) or _has(r["kind"], t) or r["id"] == t) for t in terms):
                    continue
                if (kind and not _has(r["kind"], kind)) or (status and r["status"] != status) or (since and not r["created_at"] >= since) or (before and not r["created_at"] < before):
                    continue
                out.append(r)
            return [dict(r) for r in sorted(out, key=lambda r: r["created_at"], reverse=True)[:limit]]

    def decision_texts(self) -> list[str]:
        with self.db.lock:
            return [r["decision_json"] for r in self.db.rows("approvals") if r["decision_json"] is not None]

    def with_reference(self, ref_type: Optional[str], ref_id: Optional[str], exclude_id: str) -> list[Row]:
        if ref_type is None or ref_id is None:                  # SQL: `ref_type = NULL` is never true
            return []
        with self.db.lock:
            rows = [r for r in self.db.rows("approvals") if r["ref_type"] == ref_type and r["ref_id"] == ref_id and r["id"] != exclude_id]
            return [{k: r[k] for k in ("id", "kind", "status", "created_at", "status_note")} for r in sorted(rows, key=lambda r: r["created_at"])]

    # ---- counts
    def pending_summary(self) -> Row:
        with self.db.lock:
            pending = [r for r in self.db.rows("approvals") if r["status"] == "pending"]
            return {"n": len(pending), "oldest": min((r["created_at"] for r in pending), default=None)}

    def outcome_counts(self) -> list[Row]:
        with self.db.lock:
            counts: dict[tuple[str, str], int] = {}
            for r in self.db.rows("approvals"):
                if r["status"] == "pending":
                    continue
                action = (json.loads(r["decision_json"]).get("action") if r["decision_json"] else None) or r["status"]
                counts[(r["kind"], action)] = counts.get((r["kind"], action), 0) + 1
            return [{"kind": k, "outcome": o, "n": n} for (k, o), n in sorted(counts.items())]

    def delivery_counts(self) -> list[Row]:
        with self.db.lock:
            counts: dict[tuple[str, str], int] = {}
            for d in self.db.rows("deliveries"):
                counts[(d["channel"], d["status"])] = counts.get((d["channel"], d["status"]), 0) + 1
            return [{"channel": c, "status": s, "n": n} for (c, s), n in sorted(counts.items())]

    # ---- deliveries
    def add_delivery(self, approval_id: str, recipient_id: str, channel: str, address: Optional[str], status: str, external_id: Optional[str], error: Optional[str],
                     sent_at: str) -> None:
        self.db.insert("deliveries", {"approval_id": approval_id, "recipient_id": recipient_id, "channel": channel, "address": address, "status": status,
                                      "external_id": external_id, "error": error, "attempts": 1, "sent_at": sent_at}, auto_id="id")

    def deliveries(self, approval_id: str, status: Optional[str] = None) -> list[Row]:
        with self.db.lock:
            return [dict(d) for d in sorted(self.db.rows("deliveries"), key=lambda d: d["id"]) if d["approval_id"] == approval_id and (status is None or d["status"] == status)]

    def failed_deliveries(self, max_attempts: int) -> list[Row]:
        with self.db.lock:
            pending = {r["id"] for r in self.db.rows("approvals") if r["status"] == "pending"}
            return [dict(d) for d in sorted(self.db.rows("deliveries"), key=lambda d: d["id"])
                    if d["status"] == "failed" and d["approval_id"] in pending and d["attempts"] < max_attempts]

    def update_delivery(self, delivery_id: int, status: str, external_id: Optional[str], error: Optional[str], attempts: int, sent_at: str) -> None:
        with self.db.lock:
            row = next((d for d in self.db.rows("deliveries") if d["id"] == delivery_id), None)
            if row is not None:
                row.update(status=status, external_id=external_id, error=error, attempts=attempts, sent_at=sent_at)

    def approval_for_message(self, channel: str, external_id: str, address: str) -> Optional[str]:
        with self.db.lock:
            found = [d for d in self.db.rows("deliveries") if d["channel"] == channel and d["external_id"] == external_id and d["address"] == address]
            return max(found, key=lambda d: d["id"])["approval_id"] if found else None


def _has(text: Optional[Any], term: str) -> bool:
    """SQLite's LIKE '%term%': a substring, ignoring case for ASCII letters (an absent value, like the SQL's COALESCE(value,''), contains nothing but the empty term)."""
    return term.lower() in (text or "").lower()

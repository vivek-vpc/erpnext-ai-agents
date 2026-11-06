"""The seal over the decision history (the table `audit_chain`): one row per closed request or settings change, each hash over the one before, only ever appended to.
`AuditChain` (core/audit.py) is the domain code: it computes the digests and the links and walks the chain; this is the storage it needs. Because sealing means "every
closed request that is not sealed yet", this store also reads the tables of the requests and of the settings changes (the same backend holds them all).

A row of `audit_chain`: seq (1, 2, 3, ... in the order appended), at, source ('approval' | 'setting'), ref_id, event, digest, prev, hash."""
from __future__ import annotations

from typing import ContextManager, Optional, Protocol

from ..storage import MemoryDb, Row


class AuditStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def is_empty(self) -> bool: ...

    def last_hash(self) -> Optional[str]:
        """The hash of the newest entry, or None for an empty chain."""

    def append(self, at: str, source: str, ref_id: str, event: str, digest: str, prev: str, hash_: str) -> None: ...

    def entries(self) -> list[Row]:
        """The whole chain, oldest first."""

    def sealed_at(self, source: str, ref_id: str) -> Optional[str]:
        """When this record was first sealed (`at` of its first entry), or None."""

    def unsealed_approvals(self) -> list[Row]:
        """The requests that are not pending and have no entry of source 'approval', as full rows of `approvals`, ordered by decided_at (created_at when there is none), then id."""

    def unsealed_count(self) -> int:
        """How many requests `unsealed_approvals` would return."""

    def last_sealed_setting(self) -> int:
        """The highest settings-change id that has an entry; 0 when none has."""

    def settings_after(self, setting_id: int) -> list[Row]:
        """The rows of `settings_audit` with an id above `setting_id`, oldest first."""

    def current_approval(self, ref_id: str) -> Optional[Row]:
        """The row of `approvals` as it is now, or None when it has been deleted."""

    def current_setting(self, setting_id: int) -> Optional[Row]:
        """The row of `settings_audit` as it is now, or None."""


class MemoryAuditStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def is_empty(self) -> bool:
        with self.db.lock:
            return not self.db.rows("audit_chain")

    def last_hash(self) -> Optional[str]:
        with self.db.lock:
            rows = self.db.rows("audit_chain")
            return rows[-1]["hash"] if rows else None

    def append(self, at: str, source: str, ref_id: str, event: str, digest: str, prev: str, hash_: str) -> None:
        self.db.insert("audit_chain", {"at": at, "source": source, "ref_id": ref_id, "event": event, "digest": digest, "prev": prev, "hash": hash_}, auto_id="seq")

    def entries(self) -> list[Row]:
        with self.db.lock:
            return [dict(r) for r in sorted(self.db.rows("audit_chain"), key=lambda r: r["seq"])]

    def sealed_at(self, source: str, ref_id: str) -> Optional[str]:
        with self.db.lock:
            found = sorted((r for r in self.db.rows("audit_chain") if r["source"] == source and r["ref_id"] == ref_id), key=lambda r: r["seq"])
            return found[0]["at"] if found else None

    def _unsealed(self) -> list[Row]:
        sealed = {r["ref_id"] for r in self.db.rows("audit_chain") if r["source"] == "approval"}
        return [r for r in self.db.rows("approvals") if r["status"] != "pending" and r["id"] not in sealed]

    def unsealed_approvals(self) -> list[Row]:
        with self.db.lock:
            return [dict(r) for r in sorted(self._unsealed(), key=lambda r: (r["decided_at"] or r["created_at"], r["id"]))]

    def unsealed_count(self) -> int:
        with self.db.lock:
            return len(self._unsealed())

    def last_sealed_setting(self) -> int:
        with self.db.lock:
            return max((int(r["ref_id"]) for r in self.db.rows("audit_chain") if r["source"] == "setting"), default=0)

    def settings_after(self, setting_id: int) -> list[Row]:
        with self.db.lock:
            return [dict(r) for r in sorted(self.db.rows("settings_audit"), key=lambda r: r["id"]) if r["id"] > setting_id]

    def current_approval(self, ref_id: str) -> Optional[Row]:
        with self.db.lock:
            row = next((r for r in self.db.rows("approvals") if r["id"] == ref_id), None)
            return dict(row) if row else None

    def current_setting(self, setting_id: int) -> Optional[Row]:
        with self.db.lock:
            row = next((r for r in self.db.rows("settings_audit") if r["id"] == setting_id), None)
            return dict(row) if row else None

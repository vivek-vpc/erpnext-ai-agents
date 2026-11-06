"""The values an administrator has changed, and the record of each change (the tables `settings_overrides` and `settings_audit`). `SettingsStore` (core/settings_store.py)
is the domain code: it validates, layers and audits; this is the storage it needs.

A row of `settings_overrides`: scope_type, scope_key, data_json, updated_by, updated_at (one per scope). A row of `settings_audit`: id (1, 2, 3, ... in the order written),
ts, actor, scope_type, scope_key, field, old_json, new_json."""
from __future__ import annotations

from typing import ContextManager, Optional, Protocol

from ..storage import MemoryDb, Row


class SettingsOverrideStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def get(self, scope_type: str, scope_key: str) -> Optional[str]:
        """The saved values of one scope as JSON text, or None when nothing is saved."""

    def save(self, scope_type: str, scope_key: str, data_json: Optional[str], actor: str, now: str, changes: list[tuple[str, str, str]]) -> None:
        """In one transaction: store `data_json` as the values of the scope (replacing any earlier ones; None removes the scope's row), and write one audit row for each
        (field, old_json, new_json) in `changes`, in that order."""

    def audit(self, limit: int, scope_type: Optional[str] = None, scope_key: Optional[str] = None) -> list[Row]:
        """Audit rows, newest first (highest id first), optionally of one scope type and one scope key."""

    def audit_actors(self) -> list[str]:
        """Every distinct actor in the audit."""


class MemorySettingsOverrideStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def _find(self, scope_type: str, scope_key: str) -> Optional[Row]:
        return next((r for r in self.db.rows("settings_overrides") if r["scope_type"] == scope_type and r["scope_key"] == scope_key), None)

    def get(self, scope_type: str, scope_key: str) -> Optional[str]:
        with self.db.lock:
            row = self._find(scope_type, scope_key)
            return row["data_json"] if row else None

    def save(self, scope_type: str, scope_key: str, data_json: Optional[str], actor: str, now: str, changes: list[tuple[str, str, str]]) -> None:
        with self.db.transaction():
            row = self._find(scope_type, scope_key)
            if data_json is not None:
                if row is None:
                    self.db.insert("settings_overrides", {"scope_type": scope_type, "scope_key": scope_key, "data_json": data_json, "updated_by": actor, "updated_at": now})
                else:
                    row.update(data_json=data_json, updated_by=actor, updated_at=now)
            elif row is not None:
                self.db.rows("settings_overrides").remove(row)
            for field, old_json, new_json in changes:
                self.db.insert("settings_audit", {"ts": now, "actor": actor, "scope_type": scope_type, "scope_key": scope_key, "field": field, "old_json": old_json,
                                                  "new_json": new_json}, auto_id="id")

    def audit(self, limit: int, scope_type: Optional[str] = None, scope_key: Optional[str] = None) -> list[Row]:
        with self.db.lock:
            rows = [r for r in self.db.rows("settings_audit") if (not scope_type or r["scope_type"] == scope_type) and (not scope_key or r["scope_key"] == scope_key)]
            return [dict(r) for r in sorted(rows, key=lambda r: r["id"], reverse=True)[:limit]]

    def audit_actors(self) -> list[str]:
        with self.db.lock:
            return sorted({r["actor"] for r in self.db.rows("settings_audit")})

"""The standalone service's storage: the SQLite implementation of every store the engine defines, and the function that builds the bundle the application takes."""
from __future__ import annotations

from typing import Optional

from ai_munimji_engine.stores import CoreStores, memory_core_stores

from ..core.storage import Database
from .sqlite_approvals import SqliteApprovalStore
from .sqlite_core import (SqliteAgentLogStore, SqliteAuditStore, SqliteInboundSeenStore, SqliteJobRunStore, SqliteSettingsOverrideStore, SqliteWorkOrderStore)

BACKENDS = ("sqlite", "memory")


def sqlite_health(db: Database) -> list[tuple[str, bool, str]]:
    ok, why = db.writable()
    behind = [f"{who}: the file has version {have}, this version needs {need}" for who, have, need in db.schema_status() if have != need]
    return [("database", ok, "accepts a write" if ok else f"cannot be written to ({why})"),
            ("migrations", not behind, "the database is the shape this version needs" if not behind else "; ".join(behind))]


def sqlite_core_stores(db: Database) -> CoreStores:
    return CoreStores(kind="sqlite", transaction=db.transaction, approvals=SqliteApprovalStore(db), audit=SqliteAuditStore(db), settings=SqliteSettingsOverrideStore(db),
                      workorders=SqliteWorkOrderStore(db), agents=SqliteAgentLogStore(db), jobs=SqliteJobRunStore(db), inbound=SqliteInboundSeenStore(db), db=db, backend=db, close=db.close,
                      health=lambda: sqlite_health(db))


def build_core_stores(kind: str = "sqlite", path: str = ":memory:", db: Optional[Database] = None) -> CoreStores:
    """The bundle for `kind`: SQLite over the file at `path` (or over the Database given), or memory."""
    if kind == "sqlite":
        return sqlite_core_stores(db or Database(path))
    if kind == "memory":
        return memory_core_stores()
    raise ValueError(f"unknown storage {kind!r}: use one of {', '.join(BACKENDS)}")

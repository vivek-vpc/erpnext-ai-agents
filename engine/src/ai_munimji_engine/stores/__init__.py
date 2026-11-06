"""The storage interface of the core: one Protocol per group of tables (a module each), a `Memory*Store` beside it, and the bundle the application is built with.

`CoreStores` is what `build_app` takes. The standalone service builds the SQLite one (`erp_agents.stores.build_core_stores`), a test can build the memory one
(`memory_core_stores`), and the Frappe app will build one over DocTypes. Everything that needs storage is given one of these stores, never a database."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Optional

from ..storage import MemoryDb
from .agents import AgentLogStore, MemoryAgentLogStore
from .approvals import ApprovalStore, MemoryApprovalStore
from .audit import AuditStore, MemoryAuditStore
from .inbound import InboundSeenStore, MemoryInboundSeenStore
from .jobs import JobRunStore, MemoryJobRunStore
from .settings import MemorySettingsOverrideStore, SettingsOverrideStore
from .workorders import MemoryWorkOrderStore, WorkOrderStore


def memory_health() -> list[tuple[str, bool, str]]:
    return [("database", True, "kept in memory: nothing is written to disk"), ("migrations", True, "nothing to migrate")]


@dataclass
class CoreStores:
    kind: str                                          # "sqlite" | "memory" | the name a Frappe site gives its own
    transaction: Callable[[], ContextManager[Any]]     # the unit of work all the stores of this bundle share; nested use joins the outer one
    approvals: ApprovalStore
    audit: AuditStore
    settings: SettingsOverrideStore
    workorders: WorkOrderStore
    agents: AgentLogStore
    jobs: JobRunStore
    inbound: InboundSeenStore
    db: Any = None                                     # the SQLite Database, for the edges that are about SQLite itself (backup, doctor); None on any other backend; never for rule logic
    backend: Any = None                                # the backend's own handle (the SQLite Database, the MemoryDb ...): a pack builds its own stores over it
    close: Callable[[], None] = field(default=lambda: None)
    health: Callable[[], list[tuple[str, bool, str]]] = field(default=memory_health)      # what /ready says about the storage: (check name, ok, in words)


def memory_core_stores(db: Optional[MemoryDb] = None) -> CoreStores:
    db = db or MemoryDb()
    return CoreStores(kind="memory", transaction=db.transaction, approvals=MemoryApprovalStore(db), audit=MemoryAuditStore(db), settings=MemorySettingsOverrideStore(db),
                      workorders=MemoryWorkOrderStore(db), agents=MemoryAgentLogStore(db), jobs=MemoryJobRunStore(db), inbound=MemoryInboundSeenStore(db), db=None, backend=db,
                      close=db.close)

"""The run log of the two stock agents (the table `agent_runs`): one row per run of the inventory check or the procurement pass, with its summary.

A row: id, agent, started_at, finished_at, dry_run (0 | 1), summary_json."""
from __future__ import annotations

from typing import ContextManager, Optional, Protocol

from ...storage import MemoryDb, Row


class AgentRunStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def add(self, run_id: str, agent: str, started_at: str, finished_at: Optional[str], dry_run: bool, summary_json: Optional[str]) -> None: ...

    def get(self, run_id: str) -> Optional[Row]: ...

    def recent(self, agent: str, limit: int) -> list[Row]:
        """The newest real runs (not dry runs) of one agent: started_at and summary_json."""

    def list(self, limit: int) -> list[Row]:
        """Every run, newest first."""

    def latest_started(self) -> Optional[str]:
        """When the newest run began (any agent, dry or not), or None."""


class MemoryAgentRunStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def add(self, run_id: str, agent: str, started_at: str, finished_at: Optional[str], dry_run: bool, summary_json: Optional[str]) -> None:
        with self.db.lock:
            if any(r["id"] == run_id for r in self.db.rows("agent_runs")):
                raise ValueError(f"run {run_id} exists")
            self.db.insert("agent_runs", {"id": run_id, "agent": agent, "started_at": started_at, "finished_at": finished_at, "dry_run": int(dry_run), "summary_json": summary_json})

    def get(self, run_id: str) -> Optional[Row]:
        with self.db.lock:
            row = next((r for r in self.db.rows("agent_runs") if r["id"] == run_id), None)
            return dict(row) if row else None

    @staticmethod
    def _newest_first(rows: list[Row]) -> list[Row]:
        return sorted(rows, key=lambda r: r["started_at"], reverse=True)               # ORDER BY started_at DESC (equal times keep the order made)

    def recent(self, agent: str, limit: int) -> list[Row]:
        with self.db.lock:
            rows = self._newest_first([r for r in self.db.rows("agent_runs") if r["agent"] == agent and r["dry_run"] == 0])
            return [{"started_at": r["started_at"], "summary_json": r["summary_json"]} for r in rows[:limit]]

    def list(self, limit: int) -> list[Row]:
        with self.db.lock:
            return [dict(r) for r in self._newest_first(self.db.rows("agent_runs"))[:limit]]

    def latest_started(self) -> Optional[str]:
        with self.db.lock:
            return max((r["started_at"] for r in self.db.rows("agent_runs")), default=None)

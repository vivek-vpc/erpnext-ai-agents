"""What the scheduler has done (the table `job_runs`): one row per job, when it last began and ended and how often it has run and failed. `JobLog` (core/jobs.py) is the
domain code; this is the storage it needs."""
from __future__ import annotations

from typing import ContextManager, Optional, Protocol

from ..storage import MemoryDb, Row


class JobRunStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def started(self, job: str, at: str) -> None:
        """A run began: the row is made when it is the job's first, `last_start` is `at` and `runs` goes up by one."""

    def finished(self, job: str, at: str, ok: bool, error: Optional[str]) -> None:
        """The run ended: `last_end` is `at`, `last_ok` 1 or 0, `last_error` is `error` (None when it went well), `failures` goes up when it failed. A job that never
        started has no row, and nothing is written for it."""

    def get(self, job: str) -> Optional[Row]:
        """The row (job, last_start, last_end, last_ok, last_error, runs, failures), or None."""

    def all(self) -> list[Row]:
        """Every row, ordered by job."""


class MemoryJobRunStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def _find(self, job: str) -> Optional[Row]:
        return next((r for r in self.db.rows("job_runs") if r["job"] == job), None)

    def started(self, job: str, at: str) -> None:
        with self.db.lock:
            row = self._find(job)
            if row is None:
                self.db.insert("job_runs", {"job": job, "last_start": at, "last_end": None, "last_ok": None, "last_error": None, "runs": 1, "failures": 0})
            else:
                row["last_start"], row["runs"] = at, row["runs"] + 1

    def finished(self, job: str, at: str, ok: bool, error: Optional[str]) -> None:
        with self.db.lock:
            row = self._find(job)
            if row is not None:
                row["last_end"], row["last_ok"], row["last_error"] = at, int(ok), error
                row["failures"] += int(not ok)

    def get(self, job: str) -> Optional[Row]:
        with self.db.lock:
            row = self._find(job)
            return dict(row) if row else None

    def all(self) -> list[Row]:
        with self.db.lock:
            return [dict(r) for r in sorted(self.db.rows("job_runs"), key=lambda r: r["job"])]

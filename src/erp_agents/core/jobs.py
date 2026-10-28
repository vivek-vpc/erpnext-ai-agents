"""What the scheduler has done: when each scheduled job last began and ended, whether it ended well, and how often it has run and failed since this database was made.

One row per job (the scheduler's own id for it), written by `AgentScheduler.tracked`. It answers the questions the running process cannot answer for another: `/ready` (is the
scheduler alive: the maintenance job runs every ten minutes), the metrics, and `erp-agents doctor` (which reads the file while the application runs in another process).
Only the class of a failure is kept, never its text. The only place this table is read or written."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .common import Clock, iso, parse_dt, utcnow
from .storage import Database

MAINTENANCE_JOB = "approval_maintenance"           # the scheduler's housekeeping job, every ten minutes: its last run is the scheduler's heartbeat
HEARTBEAT_STALE_MINUTES = 35                       # three runs missed, with a little to spare: the scheduler is stuck, not merely busy


class JobLog:
    def __init__(self, db: Database, clock: Clock = utcnow) -> None:
        self.db, self.clock = db, clock

    def started(self, job: str) -> None:
        self.db.execute("INSERT INTO job_runs(job, last_start, runs) VALUES (?, ?, 1) ON CONFLICT(job) DO UPDATE SET last_start=excluded.last_start, runs=runs+1",
                        (job, iso(self.clock())))

    def finished(self, job: str, ok: bool, error: Optional[str] = None) -> None:
        self.db.execute("UPDATE job_runs SET last_end=?, last_ok=?, last_error=?, failures=failures+? WHERE job=?", (iso(self.clock()), int(ok), None if ok else (error or "failed")[:60], int(not ok), job))

    def get(self, job: str) -> Optional[dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM job_runs WHERE job=?", (job,))
        return self._row(row) if row else None

    def all(self) -> list[dict[str, Any]]:
        return [self._row(r) for r in self.db.query("SELECT * FROM job_runs ORDER BY job")]

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        d = dict(row)
        d["last_start"], d["last_end"] = parse_dt(d["last_start"]), parse_dt(d["last_end"])
        return d

    def last_activity(self, job: str) -> Optional[datetime]:
        """When the job last ended (or, if it is running now, began)."""
        row = self.get(job)
        times = [t for t in (row["last_end"], row["last_start"]) if t] if row else []
        return max(times) if times else None

"""Is the application ready to do its work? (GET /ready, beside the plain liveness check at /health.)

Alive and ready are different questions. /health says the process answers. /ready says it can do what it is for: it can write to its database, the database is the shape this version
needs, and the scheduler that does the daily checks, the reminders and the expiry is running and has run its maintenance job recently. Anything else a monitor wants to know (is
ERPNext reachable, is a model installed) is slower or outside this process and is in the metrics and in `doctor`, not here: a probe that restarts a container must not do that
because a vendor's server is down.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from .jobs import HEARTBEAT_STALE_MINUTES, MAINTENANCE_JOB


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def readiness(app: Any, now: Optional[datetime] = None) -> list[Check]:
    now = now or app.clock()
    ok, why = app.db.writable()
    checks = [Check("database", ok, "accepts a write" if ok else f"cannot be written to ({why})")]
    behind = [f"{who}: the file has version {have}, this version needs {need}" for who, have, need in app.db.schema_status() if have != need]
    checks.append(Check("migrations", not behind, "the database is the shape this version needs" if not behind else "; ".join(behind)))
    scheduler = getattr(app, "scheduler", None)
    if scheduler is None:
        checks.append(Check("scheduler", True, "not part of this process"))
        return checks
    if not scheduler.sched.running:
        checks.append(Check("scheduler", False, "the scheduler is not running: no daily check, reminder or expiry will happen"))
        return checks
    try:
        last = app.job_log.last_activity(MAINTENANCE_JOB)
    except Exception as exc:  # noqa: BLE001 - the database may be what is wrong; say so and go on
        checks.append(Check("scheduler", False, f"its record of the maintenance job cannot be read ({type(exc).__name__})"))
        return checks
    marks = [t for t in (last, scheduler.started_at) if t]
    minutes = (now - max(marks)).total_seconds() / 60 if marks else 0.0
    fine = minutes <= HEARTBEAT_STALE_MINUTES
    checks.append(Check("scheduler", fine, (f"running; the maintenance job last ran {minutes:.0f} minute(s) ago" if fine else
                                            f"running, but the maintenance job (every 10 minutes) has not run for {minutes:.0f} minutes: it is stuck")))
    return checks

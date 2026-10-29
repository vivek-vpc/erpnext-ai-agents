"""Background jobs: approval housekeeping and Telegram polling, plus whatever scheduled work the packs that are switched on add (the stock pack: the daily inventory check, the
procurement pass, the agents' morning brief)."""
from __future__ import annotations

import functools
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED
from apscheduler.schedulers.background import BackgroundScheduler

from . import metrics, obs
from .approvals.channels.telegram import TelegramChannel
from .approvals.inbound import process_inbound
from .bootstrap import App
from .jobs import MAINTENANCE_JOB                    # (defined with the record of runs, jobs.py: /ready and doctor read it too)

log = logging.getLogger(__name__)


class AgentScheduler:
    def __init__(self, app: App) -> None:
        self.app = app
        tz = ZoneInfo(app.settings.system().timezone)
        self.sched = BackgroundScheduler(timezone=tz, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600})
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._providers: list[Any] = []              # what the packs add: objects with add_jobs(), after_start() and reschedule(); see add_jobs()
        self.started_at: Optional[datetime] = None   # when start() ran: /ready gives the first maintenance run time to happen before it calls the scheduler stuck
        for name in app.packs.enabled_names():       # each pack that is switched on says what it wants scheduled
            pack = app.packs.loaded.get(name)
            if pack is not None:
                pack.jobs(self)

    # ---------------------------------------------------------------- what a pack adds
    def add_jobs(self, provider: Any) -> None:
        """A pack's scheduled work: an object with `add_jobs()` (registers its jobs with `self.sched`, called before the scheduler starts), `after_start()` (anything that needs
        the scheduler running) and `reschedule()` (an administrator changed a setting). Its methods are also offered as the scheduler's own (`scheduler._brief_job`)."""
        self._providers.append(provider)

    def __getattr__(self, name: str) -> Any:
        # only reached when the scheduler has no such attribute: look in what the packs added (class attributes, so two objects can never ask each other forever)
        if not name.startswith("__"):
            for provider in self.__dict__.get("_providers", []):
                if hasattr(type(provider), name):
                    return getattr(provider, name)
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    # ---------------------------------------------------------------- jobs
    def tracked(self, job_id: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        """The job, with a record of every run: when it began and ended and whether it ended well (the job_runs table; the metrics). A job says it failed by returning False (the
        jobs catch their own errors and log them, so the scheduler never sees an exception); one that raises is recorded as failed and raised again."""
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            jobs = getattr(self.app, "job_log", None)
            with obs.bind(job=job_id):
                if jobs is not None:
                    jobs.started(job_id)
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    self._record(jobs, job_id, False, type(exc).__name__)
                    raise
                self._record(jobs, job_id, result is not False, "failed")
                return result
        return run

    def _record(self, jobs: Any, job_id: str, ok: bool, error: str) -> None:
        try:
            if jobs is not None:
                jobs.finished(job_id, ok, None if ok else error)
        except Exception:  # noqa: BLE001 - keeping the record must never stop the job it describes
            log.exception("could not record the run of %s", job_id)
        metrics.JOB_RUNS.inc(job=job_id, outcome="ok" if ok else "failed")
        if ok:
            metrics.JOB_LAST_OK.set(self.app.clock().timestamp(), job=job_id)
        obs.event("job_run", logging.INFO if ok else logging.WARNING, job=job_id, ok=ok, error=None if ok else error)

    def _on_event(self, event: Any) -> None:
        """APScheduler tells us when a job was handed to a worker (how late, against its schedule) and when one was missed altogether."""
        try:
            if event.code == EVENT_JOB_SUBMITTED:
                due = event.scheduled_run_times[0]
                metrics.JOB_LAG.set(max(0.0, (datetime.now(timezone.utc) - due).total_seconds()), job=event.job_id)
            elif event.code == EVENT_JOB_MISSED:
                metrics.JOB_RUNS.inc(job=event.job_id, outcome="missed")
                log.warning("the scheduled job %s was not run at its time (the scheduler was busy or stopped)", event.job_id)
            elif event.code == EVENT_JOB_ERROR:
                log.error("the scheduled job %s raised an error", event.job_id)
        except Exception:  # noqa: BLE001 - a listener must not take the scheduler down
            log.exception("could not note a scheduler event")

    def _maintenance_job(self) -> bool:
        try:
            svc = self.app.approvals
            recovered = svc.reconcile_pending()          # an answer the ERP already acted on is finished BEFORE its request can expire (and be asked again as a new one)
            expired, reminded, retried = svc.expire_due(), svc.remind_due(), svc.retry_failed_deliveries()
            if self.app.audit is not None:
                self.app.audit.sweep()                   # anything closed that a failed seal missed
            if expired or reminded or retried or recovered:
                log.info("approvals: expired=%d reminded=%d redelivered=%d recovered=%d", len(expired), len(reminded), retried, len(recovered))
            return True
        except Exception:  # noqa: BLE001
            log.exception("approval maintenance failed")
            return False

    def reschedule(self) -> None:
        """Called when an admin changes the schedule - takes effect immediately, no restart."""
        for provider in self._providers:
            provider.reschedule()

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        for provider in self._providers:
            provider.add_jobs()
        self.sched.add_job(self.tracked(MAINTENANCE_JOB, self._maintenance_job), "interval", minutes=10, id=MAINTENANCE_JOB, replace_existing=True)
        self.sched.add_listener(self._on_event, EVENT_JOB_SUBMITTED | EVENT_JOB_MISSED | EVENT_JOB_ERROR)
        self.started_at = self.app.clock()
        self.sched.start()
        for provider in self._providers:
            provider.after_start()
        for name, ch in self.app.channels.items():
            if isinstance(ch, TelegramChannel) and ch.mode == "polling":
                t = threading.Thread(target=self._poll_telegram, args=(ch,), name="telegram-poller", daemon=True)
                t.start()
                self._threads.append(t)

    def shutdown(self) -> None:
        self._stop.set()
        if self.sched.running:
            self.sched.shutdown(wait=False)

    def _poll_telegram(self, ch: TelegramChannel) -> None:
        try:
            ch.delete_webhook()      # polling and webhooks are mutually exclusive in Telegram
        except Exception as exc:  # noqa: BLE001
            log.warning("could not clear Telegram webhook: %s", exc)
        backoff = 2.0
        while not self._stop.is_set():
            try:
                for event in ch.poll_once(timeout=20):
                    process_inbound(event, self.app.router, self.app.approvals, self.app.conversation)
                backoff = 2.0
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram polling error: %s (retry in %.0fs)", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)

"""The stock pack's scheduled work: the daily inventory check, the procurement pass (approved reorders are picked up within minutes), the agents' morning brief, and the
follow-up: late deliveries, goods receipts and posted stock counts every day, quote rounds every hour, the slow-stock list and the count plan every week.

The core's scheduler runs the clock; this says what runs when. Every job returns at once when the pack is switched off, so switching it off needs no restart.
"""
from __future__ import annotations

import logging
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger(__name__)
NAME = "stock_procurement"
INVENTORY_JOB = "inventory_check"
PROCUREMENT_JOB = "procurement_check"
BRIEF_JOB = "agent_brief"
FOLLOWUP_DAILY_JOB = "followup_daily"
FOLLOWUP_WEEKLY_JOB = "followup_weekly"
QUOTES_JOB = "quote_rounds"


class StockJobs:
    def __init__(self, scheduler: Any) -> None:
        self.scheduler = scheduler

    @property
    def app(self) -> Any:
        return self.scheduler.app

    @property
    def sched(self) -> Any:
        return self.scheduler.sched

    def _on(self) -> bool:
        return self.app.packs.is_enabled(NAME)

    # ---------------------------------------------------------------- jobs
    # Every job says whether it ended well by returning False for "no" (it catches and logs its own errors, so the scheduler would not otherwise know): the scheduler records it
    # (core/jobs.py, the metrics, /ready). True also when the pack is switched off and there was nothing to do.
    def _inventory_job(self) -> bool:
        if not self._on():
            return True
        try:
            report = self.app.inventory.run()
            if report.failed:
                log.error("inventory run %s FAILED, nothing was checked: %s", report.run_id, "; ".join(report.errors))
                return False
            log.info("inventory run %s: checked=%d new_alerts=%d errors=%d", report.run_id, report.checked, report.new_alerts, len(report.errors))
            return True
        except Exception:  # noqa: BLE001
            log.exception("inventory run failed")
            return False

    def _procurement_job(self) -> bool:
        if not self._on():
            return True
        try:
            report = self.app.procurement.run()
            if report.proposed or report.manual or report.errors:
                log.info("procurement run %s: proposed=%d manual=%d errors=%d", report.run_id, report.proposed, report.manual, len(report.errors))
            return True
        except Exception:  # noqa: BLE001
            log.exception("procurement run failed")
            return False

    def _brief_job(self) -> bool:
        """The inventory agent's morning brief (only when the agents are on and a time is set). It can take minutes on a laptop; max_instances=1 keeps one at a time."""
        if not self._on():
            return True
        try:
            svc = self.app.agents
            if not svc.enabled:
                return True
            res = svc.brief("scheduler")
            log.info("agent brief: source=%s status=%s", res.get("source"), res.get("status"))
            if svc.settings.send_brief and res.get("text"):
                svc.tell_approvers(res["text"])
            svc.log.prune(svc.settings.keep_traces_days)
            return True
        except Exception:  # noqa: BLE001
            log.exception("agent brief failed")
            return False

    def _followup_daily_job(self) -> bool:
        """Late deliveries, goods receipts and newly posted stock counts. Each part runs on its own: one failing does not stop the others."""
        if not self._on():
            return True
        ok = True
        for name, step in (("delivery chasing", self.app.followup.chaser.run), ("goods receipt check", self.app.followup.receipts.run),
                           ("stock count differences", self.app.stock_insight.counts.run_variances)):
            try:
                res = step()
                log.info("%s: %s", name, {k: v for k, v in res.items() if k != "actions"})
            except Exception:  # noqa: BLE001
                log.exception("%s failed", name)
                ok = False
        return ok

    def _followup_weekly_job(self) -> bool:
        if not self._on():
            return True
        ok = True
        for name, step in (("slow stock list", self.app.stock_insight.slow.run), ("count plan", self.app.stock_insight.counts.run_plan)):
            try:
                log.info("%s: %s", name, step())
            except Exception:  # noqa: BLE001
                log.exception("%s failed", name)
                ok = False
        return ok

    def _quotes_job(self) -> bool:
        if not self._on():
            return True
        try:
            res = self.app.followup.quotes.check()
            if res["compared"]:
                log.info("quote rounds: %d compared", res["compared"])
            return True
        except Exception:  # noqa: BLE001
            log.exception("checking the quote rounds failed")
            return False

    def _followup_triggers(self) -> tuple[CronTrigger, CronTrigger]:
        f, tz = self.app.settings.followup(), ZoneInfo(self.app.settings.system().timezone)
        return CronTrigger.from_crontab(f.daily_cron, timezone=tz), CronTrigger.from_crontab(f.weekly_cron, timezone=tz)

    def _sync_brief_job(self) -> None:
        cfg = self.app.settings.agents()
        cron = cfg.daily_brief_cron.strip() if cfg.enabled and self._on() else ""
        job = self.sched.get_job(BRIEF_JOB)
        if not cron:
            if job:
                self.sched.remove_job(BRIEF_JOB)
            return
        trigger = CronTrigger.from_crontab(cron, timezone=ZoneInfo(self.app.settings.system().timezone))
        if job:
            self.sched.reschedule_job(BRIEF_JOB, trigger=trigger)
        else:
            self.sched.add_job(self.scheduler.tracked(BRIEF_JOB, self._brief_job), trigger, id=BRIEF_JOB, replace_existing=True)
        log.info("the agents' stock brief runs on schedule %r", cron)

    def _trigger(self) -> CronTrigger:
        s = self.app.settings.system()
        return CronTrigger.from_crontab(s.check_cron, timezone=ZoneInfo(s.timezone))

    # ---------------------------------------------------------------- what the core's scheduler calls
    def add_jobs(self) -> None:
        track = self.scheduler.tracked                         # every job leaves a record of when it ran and how it ended
        self.sched.add_job(track(INVENTORY_JOB, self._inventory_job), self._trigger(), id=INVENTORY_JOB, replace_existing=True)
        # Approved reorders are picked up within minutes, not at the next daily stock check.
        self.sched.add_job(track(PROCUREMENT_JOB, self._procurement_job), "interval", minutes=3, id=PROCUREMENT_JOB, replace_existing=True)
        daily, weekly = self._followup_triggers()
        self.sched.add_job(track(FOLLOWUP_DAILY_JOB, self._followup_daily_job), daily, id=FOLLOWUP_DAILY_JOB, replace_existing=True)
        self.sched.add_job(track(FOLLOWUP_WEEKLY_JOB, self._followup_weekly_job), weekly, id=FOLLOWUP_WEEKLY_JOB, replace_existing=True)
        self.sched.add_job(track(QUOTES_JOB, self._quotes_job), "interval", hours=1, id=QUOTES_JOB, replace_existing=True)

    def after_start(self) -> None:
        self._sync_brief_job()

    def reschedule(self) -> None:
        """An administrator changed a setting: takes effect immediately, no restart."""
        if self.sched.get_job(INVENTORY_JOB):
            self.sched.reschedule_job(INVENTORY_JOB, trigger=self._trigger())
            log.info("inventory schedule is now %r", self.app.settings.system().check_cron)
        if self.sched.get_job(FOLLOWUP_DAILY_JOB):
            daily, weekly = self._followup_triggers()
            self.sched.reschedule_job(FOLLOWUP_DAILY_JOB, trigger=daily)
            self.sched.reschedule_job(FOLLOWUP_WEEKLY_JOB, trigger=weekly)
        self._sync_brief_job()

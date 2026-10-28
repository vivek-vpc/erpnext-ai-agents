"""Scheduled jobs leave a record, the scheduler's lateness is measured, and GET /ready says whether the application can do its work (Phase 2 of the hardening brief)."""
import io
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, EVENT_JOB_SUBMITTED
from fastapi.testclient import TestClient

from erp_agents.core import metrics, obs
from erp_agents.core.api.app import create_app
from erp_agents.core.erp.mock import MockERP
from erp_agents.core.jobs import HEARTBEAT_STALE_MINUTES, MAINTENANCE_JOB, JobLog
from erp_agents.core.scheduler import AgentScheduler
from erp_agents.core.storage import Database

KEY = "owner-key-0123456789abcdefghij"
AUTH = {"auth": {"tokens": [{"user": "owner", "token": KEY, "roles": ["admin", "approver"]}]}, "observability": {"metrics": True}}


@pytest.fixture(autouse=True)
def clean_metrics():
    metrics.REGISTRY.reset()
    yield
    metrics.configure(False)
    metrics.REGISTRY.reset()
    metrics.REGISTRY.set_collector("database", None)


@pytest.fixture
def app(make_app, clock):
    return make_app(erp=MockERP(today=clock().date()), cfg=AUTH)


class FakeScheduler:
    """What /ready looks at: whether it is running and when it started."""

    def __init__(self, clock, running=True):
        self.sched, self.started_at = SimpleNamespace(running=running), clock()


def check(body, name):
    return next(c for c in body["checks"] if c["name"] == name)


# ------------------------------------------------------------------ the record of runs
def test_a_job_leaves_when_it_began_and_ended_how_often_it_ran_and_how_it_ended(clock):
    jobs = JobLog(Database(":memory:"), clock)
    assert jobs.get("inventory_check") is None and jobs.last_activity("inventory_check") is None
    jobs.started("inventory_check")
    assert jobs.get("inventory_check")["last_end"] is None and jobs.last_activity("inventory_check") == clock()          # running now
    clock.advance(seconds=30)
    jobs.finished("inventory_check", True)
    row = jobs.get("inventory_check")
    assert (row["runs"], row["failures"], row["last_ok"], row["last_error"]) == (1, 0, 1, None) and row["last_end"] == clock()
    clock.advance(hours=1)
    jobs.started("inventory_check")
    jobs.finished("inventory_check", False, "x" * 200)
    row = jobs.get("inventory_check")
    assert (row["runs"], row["failures"], row["last_ok"], len(row["last_error"])) == (2, 1, 0, 60)                    # only the class of a failure, and not an essay
    jobs.started("approval_maintenance")
    assert [r["job"] for r in jobs.all()] == ["approval_maintenance", "inventory_check"]


@contextmanager
def logged_json():
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    buf = io.StringIO()
    root.handlers = [obs.make_handler(buf, "json")]
    root.setLevel(logging.INFO)
    try:
        yield buf
    finally:
        root.handlers = saved
        root.setLevel(level)


def test_a_tracked_job_is_recorded_counted_and_logged_with_its_own_id(app):
    scheduler = AgentScheduler(app)
    ran = []

    def job():
        logging.getLogger("erp_agents.test").info("inside the job")
        ran.append(1)
        return True
    with logged_json() as buf:
        scheduler.tracked("demo_job", job)()
    row = app.job_log.get("demo_job")
    assert ran == [1] and (row["runs"], row["last_ok"]) == (1, 1)
    lines = [json.loads(l) for l in buf.getvalue().strip().splitlines()]
    assert next(l for l in lines if l["msg"] == "inside the job")["job"] == "demo_job"
    assert next(l for l in lines if l.get("event") == "job_run")["ok"] is True
    assert metrics.JOB_RUNS.values[("demo_job", "ok")] == 1 and metrics.JOB_LAST_OK.values[("demo_job",)] == app.clock().timestamp()


def test_a_job_that_says_it_failed_or_raises_is_recorded_as_failed(app):
    scheduler = AgentScheduler(app)
    scheduler.tracked("quiet_failure", lambda: False)()                                    # the jobs catch their own errors and report failure with False

    def boom():
        raise RuntimeError("secret detail that must not be kept")
    with pytest.raises(RuntimeError):
        scheduler.tracked("loud_failure", boom)()
    assert app.job_log.get("quiet_failure")["last_error"] == "failed" and app.job_log.get("loud_failure")["last_error"] == "RuntimeError"
    assert "secret detail" not in json.dumps(app.job_log.all(), default=str)
    assert metrics.JOB_RUNS.values[("quiet_failure", "failed")] == 1 and metrics.JOB_RUNS.values[("loud_failure", "failed")] == 1
    scheduler.tracked("returns_nothing", lambda: None)()
    assert app.job_log.get("returns_nothing")["last_ok"] == 1                              # nothing returned = it ended well


def test_the_jobs_of_the_stock_pack_are_registered_tracked_and_report_how_they_ended(app):
    scheduler = AgentScheduler(app)
    scheduler.start()
    try:
        for job_id in ("inventory_check", "procurement_check", "followup_daily", "followup_weekly", "quote_rounds", MAINTENANCE_JOB):
            func = scheduler.sched.get_job(job_id).func
            assert hasattr(func, "__wrapped__"), job_id                                    # the tracked wrapper around the pack's own method
        scheduler.sched.get_job("inventory_check").func()
        assert app.job_log.get("inventory_check")["last_ok"] == 1 and app.job_log.get("inventory_check")["runs"] == 1
        app.inventory.run = lambda: (_ for _ in ()).throw(RuntimeError("the ERP is down"))
        scheduler.sched.get_job("inventory_check").func()
        row = app.job_log.get("inventory_check")
        assert (row["runs"], row["failures"], row["last_ok"], row["last_error"]) == (2, 1, 0, "failed")
        scheduler.sched.get_job(MAINTENANCE_JOB).func()
        assert app.job_log.get(MAINTENANCE_JOB)["last_ok"] == 1
    finally:
        scheduler.shutdown()


# ------------------------------------------------------------------ how late, and missed
def test_scheduler_lag_and_missed_runs_are_measured_from_the_schedulers_own_events(app):
    scheduler = AgentScheduler(app)
    due = datetime.now(timezone.utc) - timedelta(seconds=7)
    scheduler._on_event(SimpleNamespace(code=EVENT_JOB_SUBMITTED, job_id="inventory_check", scheduled_run_times=[due]))
    assert 6.5 < metrics.JOB_LAG.values[("inventory_check",)] < 12
    scheduler._on_event(SimpleNamespace(code=EVENT_JOB_SUBMITTED, job_id="quote_rounds", scheduled_run_times=[datetime.now(timezone.utc) + timedelta(seconds=5)]))
    assert metrics.JOB_LAG.values[("quote_rounds",)] == 0                                  # early is not late
    scheduler._on_event(SimpleNamespace(code=EVENT_JOB_MISSED, job_id="inventory_check"))
    scheduler._on_event(SimpleNamespace(code=EVENT_JOB_ERROR, job_id="inventory_check"))
    assert metrics.JOB_RUNS.values[("inventory_check", "missed")] == 1
    scheduler._on_event(object())                                                          # an event it cannot read is noted, not raised


# ------------------------------------------------------------------ /ready
def test_ready_says_yes_to_anybody_and_gives_the_checks_to_a_signed_in_person(app, clock):
    app.scheduler = FakeScheduler(clock)
    client = TestClient(create_app(app))
    r = client.get("/ready")
    assert r.status_code == 200 and r.json() == {"ready": True}
    body = client.get("/ready", headers={"Authorization": f"Bearer {KEY}"}).json()
    assert body["ready"] is True and [c["name"] for c in body["checks"]] == ["database", "migrations", "scheduler"] and all(c["ok"] for c in body["checks"])
    assert "accepts a write" in check(body, "database")["detail"] and "maintenance job last ran 0 minute" in check(body, "scheduler")["detail"]


def test_ready_is_a_separate_question_from_health_and_health_does_not_change(app, clock):
    app.scheduler = FakeScheduler(clock, running=False)
    client = TestClient(create_app(app))
    assert client.get("/ready").status_code == 503 and client.get("/ready").json() == {"ready": False}
    assert client.get("/health").status_code == 200 and client.get("/health").json() == {"ok": True}                  # alive, though not ready


def test_ready_says_no_when_the_database_cannot_be_written(app, clock):
    app.scheduler = FakeScheduler(clock)
    client = TestClient(create_app(app))
    app.db.execute("PRAGMA query_only=ON")                                                  # what a read-only file or a full disk looks like to the next write
    r = client.get("/ready", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 503 and check(r.json(), "database")["ok"] is False and "cannot be written to (OperationalError)" in check(r.json(), "database")["detail"]
    app.db.execute("PRAGMA query_only=OFF")
    assert client.get("/ready").status_code == 200


def test_ready_says_no_when_the_scheduler_is_not_running_or_stuck(app, clock):
    scheduler = FakeScheduler(clock, running=False)
    app.scheduler = scheduler
    client = TestClient(create_app(app))
    headers = {"Authorization": f"Bearer {KEY}"}
    body = client.get("/ready", headers=headers).json()
    assert body["ready"] is False and "not running" in check(body, "scheduler")["detail"]
    scheduler.sched.running = True
    clock.advance(minutes=HEARTBEAT_STALE_MINUTES - 5)
    assert client.get("/ready").status_code == 200                                          # a few runs late is not stuck
    clock.advance(minutes=10)
    body = client.get("/ready", headers=headers).json()
    assert body["ready"] is False and "has not run for 40 minutes" in check(body, "scheduler")["detail"]
    app.job_log.started(MAINTENANCE_JOB)
    app.job_log.finished(MAINTENANCE_JOB, True)
    assert client.get("/ready").status_code == 200                                          # the maintenance job ran: the heartbeat is back


def test_ready_says_no_when_the_database_is_not_the_shape_this_version_needs(app, clock, monkeypatch):
    app.scheduler = FakeScheduler(clock)
    monkeypatch.setattr(app.db, "schema_status", lambda: [("core", 2, 2), ("stock_procurement", 3, 4)])
    r = TestClient(create_app(app)).get("/ready", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 503 and "stock_procurement: the file has version 3, this version needs 4" in check(r.json(), "migrations")["detail"]


def test_without_a_scheduler_in_the_process_there_is_nothing_to_be_stuck(app):
    app.scheduler = None
    r = TestClient(create_app(app)).get("/ready", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200 and check(r.json(), "scheduler")["detail"] == "not part of this process"


def test_ready_is_hidden_through_a_tunnel_when_only_the_webhooks_are_open(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()), cfg={**AUTH, "server": {"remote_access": "webhooks_only"}})
    client = TestClient(create_app(app))
    assert client.get("/ready", headers={"x-forwarded-for": "203.0.113.9"}).status_code == 404 and client.get("/health", headers={"x-forwarded-for": "203.0.113.9"}).status_code == 200

"""`erp-agents doctor`, the checks of how the installation is RUNNING (Phase 2 of the hardening brief): the scheduler's heartbeat, free disk, the clock, requests that wait too long,
and the seal over the decision history. All read from the database file alone, which is opened so that it cannot be written; no network."""
import os
import sqlite3
import time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

import pytest

from erp_agents.core import doctor
from erp_agents.core.audit import AuditChain
from erp_agents.core.common import iso
from erp_agents.core.jobs import MAINTENANCE_JOB, JobLog
from erp_agents.core.ops import InstanceLock
from erp_agents.core.storage import Database

NOW = time.time()
Usage = namedtuple("Usage", "total used free")


def at(minutes_ago, base=NOW):
    return datetime.fromtimestamp(base - minutes_ago * 60, timezone.utc).replace(microsecond=0)


def cfg_for(tmp_path, **obs):
    return {"database": {"path": str(tmp_path / "data" / "app.db")}, "erp": {"type": "mock"}, "auth": {}, "server": {}, "approvals": {"recipients": []}, **({"observability": obs} if obs else {})}


def database(tmp_path):
    path = tmp_path / "data" / "app.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return Database(str(path)), str(path)


def approval(db, id, status="pending", created=None, decided=None):
    db.execute("INSERT INTO approvals(id, kind, status, request_json, decision_json, created_at, decided_at) VALUES (?,?,?,?,?,?,?)",
               (id, "purchase", status, "{}", None if status == "pending" else '{"answer": 1}', iso(created or at(1)), iso(decided)))


def ran(db, job, minutes_ago, ok=True, error=None):
    jobs = JobLog(db, lambda: at(minutes_ago))
    jobs.started(job)
    jobs.finished(job, ok, error)


def area(checks, name):
    return [c for c in checks if c.area == name]


def started_ago(path, minutes):
    """The running instance wrote its lock file when it started: make that as long ago as the test needs."""
    stamp = NOW - minutes * 60
    os.utime(f"{path}.lock", (stamp, stamp))


# ------------------------------------------------------------------ the scheduler
def test_a_running_instance_whose_maintenance_job_ran_a_few_minutes_ago_is_alive(tmp_path):
    db, path = database(tmp_path)
    ran(db, MAINTENANCE_JOB, 3)
    with InstanceLock(path):
        started_ago(path, 60)
        (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Scheduler")
    assert c.status == doctor.OK and "alive" in c.title and "3 minute(s) ago" in c.detail


def test_a_running_instance_whose_maintenance_job_stopped_is_a_warning_with_what_to_do(tmp_path):
    db, path = database(tmp_path)
    ran(db, MAINTENANCE_JOB, 180)
    with InstanceLock(path):
        started_ago(path, 5 * 60)                                                             # it has been up for five hours and the last maintenance run was three hours ago
        (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Scheduler")
    assert c.status == doctor.WARN and "gone quiet" in c.title and "3 hour(s)" in c.detail and "approval maintenance failed" in c.fix and "restart" in c.fix


def test_an_instance_that_has_only_just_started_is_not_called_stuck_for_what_ran_before_it(tmp_path):
    db, path = database(tmp_path)
    ran(db, MAINTENANCE_JOB, 3 * 60)                                                          # the last run was hours ago, in the previous process
    with InstanceLock(path):
        started_ago(path, 0)                                                                  # it started at NOW, ten minutes before the doctor looks
        (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW + 10 * 60), "Scheduler")
    assert c.status == doctor.OK and "started 10 minute(s) ago" in c.detail and "has not come round yet" in c.detail


def test_an_instance_that_has_never_recorded_a_run_is_judged_from_when_it_started(tmp_path):
    db, path = database(tmp_path)                                                             # a new database: no job has run yet
    with InstanceLock(path):
        started_ago(path, 0)
        assert area(doctor.check_operation(cfg_for(tmp_path), path, NOW + 5 * 60), "Scheduler")[0].status == doctor.OK
        assert area(doctor.check_operation(cfg_for(tmp_path), path, NOW + 3 * 3600), "Scheduler")[0].status == doctor.WARN


def test_with_no_instance_running_there_is_no_scheduler_to_be_quiet(tmp_path):
    db, path = database(tmp_path)
    ran(db, MAINTENANCE_JOB, 5 * 24 * 60)
    assert area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Scheduler") == []


def test_a_job_that_failed_the_last_time_it_ran_is_named(tmp_path):
    db, path = database(tmp_path)
    ran(db, "inventory_check", 20, ok=False, error="failed")
    ran(db, "quote_rounds", 20)
    (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Scheduler")
    assert c.status == doctor.WARN and "inventory_check" in c.title and "quote_rounds" not in c.title
    assert "1 failure(s) in 1 run(s)" in c.detail and "event=job_run" in c.fix


def test_a_database_from_before_schema_2_has_no_scheduler_record_and_is_not_an_error(tmp_path):
    db, path = database(tmp_path)
    db.execute("DROP TABLE job_runs")
    assert area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Scheduler") == []


# ------------------------------------------------------------------ the disk
@pytest.mark.parametrize("free_mb, status", [(20_000, doctor.OK), (400, doctor.WARN), (20, doctor.FAIL)])
def test_free_disk_space_beside_the_database(tmp_path, monkeypatch, free_mb, status):
    db, path = database(tmp_path)
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: Usage(500_000 * 1048576, 480_000 * 1048576, free_mb * 1048576))
    (c,) = doctor.check_disk(path)
    assert c.status == status and f"{free_mb:,.0f} MB free" in c.detail
    if status != doctor.OK:
        assert "/ready then answers 503" in c.fix


def test_a_database_that_is_large_for_the_free_space_warns_before_the_disk_is_nearly_full(tmp_path, monkeypatch):
    db, path = database(tmp_path)
    monkeypatch.setattr(Path, "stat", lambda self, **k: type("S", (), {"st_size": 400 * 1048576, "st_mtime": NOW})() if self.name == "app.db" else Path.__dict__["stat"](self, **k))
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: Usage(10**12, 10**12 - 900 * 1048576, 900 * 1048576))        # 900 MB free, a 400 MB database: a backup would not leave much
    assert doctor.check_disk(path)[0].status == doctor.WARN


def test_a_disk_that_cannot_be_read_is_information_not_a_crash(tmp_path, monkeypatch):
    db, path = database(tmp_path)

    def broken(p):
        raise OSError("no such device")
    monkeypatch.setattr(doctor.shutil, "disk_usage", broken)
    assert doctor.check_disk(path)[0].status == doctor.INFO


# ------------------------------------------------------------------ requests that wait
def test_nothing_waiting_is_ok_and_a_decided_old_request_does_not_count(tmp_path):
    db, path = database(tmp_path)
    approval(db, "old-decided", status="decided", created=at(10 * 24 * 60), decided=at(9 * 24 * 60))
    (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Approvals")
    assert c.status == doctor.OK and "no request is waiting" in c.title


def test_a_request_waiting_for_hours_is_ok_until_it_passes_the_limit(tmp_path):
    db, path = database(tmp_path)
    approval(db, "recent", created=at(2 * 60))
    (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Approvals")
    assert c.status == doctor.OK and "1 request(s) waiting" in c.title and "2 hour(s)" in c.detail
    approval(db, "stuck", created=at(30 * 60))
    (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Approvals")
    assert c.status == doctor.WARN and "waited longer" in c.title and "30 hour(s)" in c.detail and "the limit is 24 hour(s)" in c.detail and "console" in c.fix


def test_the_limit_is_the_one_in_the_settings(tmp_path):
    db, path = database(tmp_path)
    approval(db, "two-hours", created=at(2 * 60))
    assert area(doctor.check_operation(cfg_for(tmp_path, pending_hours=1), path, NOW), "Approvals")[0].status == doctor.WARN
    assert area(doctor.check_operation(cfg_for(tmp_path, pending_hours=3), path, NOW), "Approvals")[0].status == doctor.OK
    assert area(doctor.check_operation(cfg_for(tmp_path, pending_hours=-5), path, NOW), "Approvals")[0].status == doctor.OK        # a bad value is the start-up check's to report: 24 hours here


# ------------------------------------------------------------------ the clock
def test_a_record_dated_in_the_future_means_the_clock_is_wrong(tmp_path):
    db, path = database(tmp_path)
    approval(db, "from-the-future", created=at(-3 * 60))
    (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Clock")
    assert c.status == doctor.WARN and "dated in the future" in c.title and "3 hour(s) ahead" in c.detail and "Sync now" in c.fix


def test_a_small_difference_is_not_a_wrong_clock_and_neither_is_an_empty_database(tmp_path):
    db, path = database(tmp_path)
    assert area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Clock") == []
    approval(db, "a-moment-ahead", created=at(-2))
    (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Clock")
    assert c.status == doctor.OK


def test_the_scheduler_records_count_for_the_clock_too(tmp_path):
    db, path = database(tmp_path)
    ran(db, MAINTENANCE_JOB, -120)
    (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Clock")
    assert c.status == doctor.WARN and "2 hour(s) ahead" in c.detail


# ------------------------------------------------------------------ the seal over the decision history
def sealed(tmp_path):
    db, path = database(tmp_path)
    approval(db, "a1", status="decided", created=at(60), decided=at(30))
    approval(db, "a2", status="expired", created=at(50), decided=at(20))
    AuditChain(db, lambda: at(10)).sweep()
    return db, path


def test_an_intact_seal_is_ok_and_says_how_many_entries_and_how_many_wait_to_be_sealed(tmp_path):
    db, path = sealed(tmp_path)
    (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Decision history")
    assert c.status == doctor.OK and "2 sealed entries" in c.detail
    approval(db, "a3", status="decided", created=at(5), decided=at(4))
    (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Decision history")
    assert c.status == doctor.OK and "1 closed request(s) are sealed the next time" in c.detail


def test_a_request_changed_after_it_was_sealed_is_something_to_fix_now(tmp_path):
    db, path = sealed(tmp_path)
    db.execute("UPDATE approvals SET decision_json='{\"answer\": 2}' WHERE id='a1'")
    (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Decision history")
    assert c.status == doctor.FAIL and "does not verify" in c.title and "request a1 was changed after it was sealed" in c.detail
    assert "history --verify" in c.fix and "restore" in c.fix


def test_a_request_deleted_after_it_was_sealed_is_something_to_fix_now(tmp_path):
    db, path = sealed(tmp_path)
    db.execute("DELETE FROM approvals WHERE id='a2'")
    (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Decision history")
    assert c.status == doctor.FAIL and "request a2 was deleted after it was sealed" in c.detail


def test_an_edited_seal_is_something_to_fix_now(tmp_path):
    db, path = sealed(tmp_path)
    db.execute("UPDATE audit_chain SET event='decided' WHERE seq=1")
    (c,) = area(doctor.check_operation(cfg_for(tmp_path), path, NOW), "Decision history")
    assert c.status == doctor.FAIL and "the seal itself was edited at entry 1" in c.detail


# ------------------------------------------------------------------ read-only, and never a crash
def test_the_checks_open_the_database_so_that_it_cannot_be_written_and_change_nothing(tmp_path):
    db, path = sealed(tmp_path)
    ro = doctor._ReadOnlyDb(path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.conn.execute("DELETE FROM approvals")
    ro.close()
    db.close()
    before = Path(path).read_bytes()
    doctor.check_operation(cfg_for(tmp_path), path, NOW)
    assert Path(path).read_bytes() == before


def test_a_file_that_is_not_a_database_is_information_and_the_rest_of_the_doctor_goes_on(tmp_path):
    path = tmp_path / "data" / "app.db"
    path.parent.mkdir()
    path.write_bytes(b"this is not a sqlite file at all, just text " * 40)
    checks = doctor.check_operation(cfg_for(tmp_path), str(path), NOW)
    assert [c.status for c in checks if c.area == "Operation"] == [doctor.INFO] and "could not be read" in checks[-1].title
    assert not [c for c in checks if c.status == doctor.FAIL]                                  # the damage itself is the database check's to report, as before


def test_no_database_and_a_memory_database_have_nothing_to_check(tmp_path):
    assert doctor.check_operation(cfg_for(tmp_path), str(tmp_path / "absent.db"), NOW) == []
    assert doctor.check_operation(cfg_for(tmp_path), ":memory:", NOW) == []


def test_the_whole_doctor_includes_them_and_they_show_in_the_report(tmp_path, monkeypatch):
    db, path = sealed(tmp_path)
    approval(db, "waiting", created=at(60))
    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda p: Usage(10**12, 10**11, 9 * 10**11))
    checks = doctor.run_doctor(cfg_for(tmp_path), None, NOW)
    assert {"Disk", "Approvals", "Decision history"} <= {c.area for c in checks}
    text = doctor.render(checks)
    assert "[OK  ] the seal over the decision history is intact" in text and "[OK  ] 1 request(s) waiting, none for too long" in text

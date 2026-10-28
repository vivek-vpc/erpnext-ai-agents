"""Schema version 2 adds the job_runs table (core/jobs.py). A database made by version 1 is copied aside and upgraded, a version 1 backup is still a good backup, and a
version 2 file that lacks the table is damaged (Phase 2 of the hardening brief)."""
import sqlite3
from pathlib import Path

from erp_agents.core import doctor, ops, storage
from erp_agents.core.storage import Database


def as_version_one(path):
    """What a database made by the first version of this application looks like: no job_runs table, schema version 1."""
    c = sqlite3.connect(path)
    c.execute("DROP TABLE job_runs")
    c.execute("UPDATE schema_info SET version=1")
    c.commit()
    c.close()


def tables(path):
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def version_of(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT version FROM schema_info").fetchone()[0]
    finally:
        c.close()


def made(tmp_path, name="app.db"):
    path = str(tmp_path / name)
    db = Database(path)
    db.execute("INSERT INTO settings_overrides(scope_type, scope_key, data_json, updated_by, updated_at) VALUES ('item', 'K1', '{}', 't', '2026-10-28')")
    db.close()
    return path


def test_this_version_is_schema_2_and_a_new_database_has_the_table(tmp_path):
    assert storage.SCHEMA_VERSION == 2 and 2 in storage.MIGRATIONS
    path = made(tmp_path)
    assert version_of(path) == 2 and "job_runs" in tables(path)


def test_a_version_1_database_is_copied_aside_then_upgraded_and_keeps_its_data(tmp_path):
    path = made(tmp_path)
    as_version_one(path)
    assert "job_runs" not in tables(path) and version_of(path) == 1
    db = Database(path)
    db.execute("INSERT INTO job_runs(job, runs) VALUES ('inventory_check', 1)")                  # the table is there and works
    assert db.stored_version() == 2 and db.query_one("SELECT COUNT(*) AS n FROM settings_overrides")["n"] == 1
    db.close()
    (copy,) = [p for p in tmp_path.iterdir() if ".before-schema-1-" in p.name]
    assert version_of(str(copy)) == 1 and "job_runs" not in tables(str(copy))                        # the copy is the old database, untouched
    Database(path).close()
    assert len([p for p in tmp_path.iterdir() if "before-schema" in p.name]) == 1                    # opened again at version 2: no second copy


def test_a_version_1_backup_still_verifies_and_a_version_2_file_without_the_table_does_not(tmp_path):
    path = made(tmp_path)
    backup = tmp_path / "backup.db"
    ops.backup_database(path, str(backup))
    as_version_one(str(backup))
    verdict = ops.verify_database(str(backup))
    assert verdict.ok and verdict.version == 1, verdict.problems                                       # an older file is not damaged for lacking what the upgrade will make
    broken = tmp_path / "broken.db"
    ops.backup_database(path, str(broken))
    c = sqlite3.connect(broken)
    c.execute("DROP TABLE job_runs")
    c.commit()
    c.close()
    bad = ops.verify_database(str(broken))
    assert not bad.ok and any("tables are missing: job_runs" in p for p in bad.problems)


def test_restoring_a_version_1_backup_gives_a_database_that_upgrades_when_it_is_opened(tmp_path):
    path = made(tmp_path)
    backup = tmp_path / "v1.db"
    ops.backup_database(path, str(backup))
    as_version_one(str(backup))
    target = str(tmp_path / "restored.db")
    ops.restore_database(str(backup), target)
    assert version_of(target) == 1
    Database(target).close()
    assert version_of(target) == 2 and "job_runs" in tables(target)


def test_doctor_says_a_version_1_database_will_be_upgraded_on_the_next_start(tmp_path):
    path = made(tmp_path)
    as_version_one(path)
    found = doctor.check_database(path, 0.0)
    assert any(c.status == doctor.OK and "schema version 1" in c.title and "upgraded on the next start" in c.detail for c in found), [(c.status, c.title, c.detail) for c in found]
    assert Path(path).exists()

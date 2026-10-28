"""Operating the single instance: schema versions, one process per database, backup and restore, and the `doctor` check (review of 25 Oct 2026, finding 4).

Nothing here touches a network: the doctor test switches sockets off to prove it, and every file is made in a temporary folder.
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import erp_agents.core.storage as storage
from erp_agents.core.cli import main
from erp_agents.core import doctor, ops
from erp_agents.core.storage import Database, SchemaError

PY = sys.executable
WINDOWS = sys.platform == "win32"


def fresh(tmp_path, name="app.db", rows=0):
    path = str(tmp_path / name)
    db = Database(path)
    for i in range(rows):
        db.execute("INSERT INTO settings_overrides(scope_type, scope_key, data_json, updated_by, updated_at) VALUES (?,?,?,?,?)", ("item", f"K{i}", "{}", "t", "2026-10-25"))
    db.close()
    return path


def rows_in(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT COUNT(*) FROM settings_overrides").fetchone()[0]
    finally:
        c.close()


def version_in(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT version FROM schema_info").fetchone()[0]
    finally:
        c.close()


# ====================================================================== schema versions and migrations
def test_a_new_database_is_stamped_with_the_current_version(tmp_path):
    assert version_in(fresh(tmp_path)) == storage.SCHEMA_VERSION


def test_a_database_from_a_newer_version_is_refused_and_left_exactly_as_it_is(tmp_path):
    path = fresh(tmp_path, rows=3)
    c = sqlite3.connect(path)
    c.execute("UPDATE schema_info SET version=?", (storage.SCHEMA_VERSION + 4,))
    c.execute("CREATE TABLE a_table_only_the_newer_version_knows (x)")
    c.commit()
    c.close()
    before = sorted(p.name for p in tmp_path.iterdir()), Path(path).read_bytes()
    with pytest.raises(SchemaError, match="newer version.*Nothing was changed"):
        Database(path)
    assert (sorted(p.name for p in tmp_path.iterdir()), Path(path).read_bytes()) == before             # no copy, no write, no -wal file


def test_an_older_database_is_copied_aside_and_then_upgraded_in_order(tmp_path, monkeypatch):
    path = fresh(tmp_path, rows=2)
    now = storage.SCHEMA_VERSION                                                                          # two versions newer than the file, whatever the real one is
    steps = []
    monkeypatch.setattr(storage, "SCHEMA_VERSION", now + 2)
    monkeypatch.setattr(storage, "MIGRATIONS", {now + 1: lambda conn: (steps.append(now + 1), conn.execute("CREATE TABLE added_first (x)")),
                                                now + 2: lambda conn: (steps.append(now + 2), conn.execute("CREATE TABLE added_second (x)"))})
    Database(path).close()
    assert steps == [now + 1, now + 2] and version_in(path) == now + 2
    (copy,) = [p for p in tmp_path.iterdir() if f".before-schema-{now}-" in p.name]
    assert version_in(str(copy)) == now and rows_in(str(copy)) == 2                                      # the copy is the old database, complete
    c = sqlite3.connect(path)
    assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {"added_first", "added_second"}
    c.close()


def test_a_migration_that_fails_changes_nothing_and_says_where_the_copy_is(tmp_path, monkeypatch):
    path = fresh(tmp_path, rows=2)

    def broken(conn):
        conn.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("boom")
    now = storage.SCHEMA_VERSION
    monkeypatch.setattr(storage, "SCHEMA_VERSION", now + 1)
    monkeypatch.setattr(storage, "MIGRATIONS", {now + 1: broken})
    with pytest.raises(SchemaError, match=f"version {now + 1} failed.*Nothing was changed.*before-schema-{now}-"):
        Database(path)
    assert version_in(path) == now and rows_in(path) == 2
    c = sqlite3.connect(path)
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='half_done'").fetchone()                 # rolled back
    c.close()


def test_a_missing_migration_is_refused_before_anything_is_touched(tmp_path, monkeypatch):
    path = fresh(tmp_path)
    now = storage.SCHEMA_VERSION
    monkeypatch.setattr(storage, "SCHEMA_VERSION", now + 1)
    monkeypatch.setattr(storage, "MIGRATIONS", {})
    with pytest.raises(SchemaError, match="no migration for version"):
        Database(path)
    assert version_in(path) == now and not [p for p in tmp_path.iterdir() if "before-schema" in p.name]


def test_the_current_version_needs_no_copy_and_an_in_memory_database_makes_none(tmp_path):
    fresh(tmp_path)
    fresh(tmp_path)                                                                                      # opened again: same version
    assert not [p for p in tmp_path.iterdir() if "before-schema" in p.name]
    Database(":memory:").close()


# ====================================================================== one instance per database
def test_a_second_instance_on_the_same_database_is_refused_while_the_first_runs(tmp_path):
    path = str(tmp_path / "app.db")
    first = ops.InstanceLock(path).acquire()
    try:
        with pytest.raises(ops.AlreadyRunning, match=f"process {os.getpid()}"):
            ops.InstanceLock(path).acquire()
    finally:
        first.release()
    with ops.InstanceLock(path):                                                                          # released: the next one may start
        pass


def test_the_lock_is_held_across_processes_and_dropped_when_that_process_dies(tmp_path):
    path = str(tmp_path / "app.db")
    code = ("import sys, time; sys.path.insert(0, %r); from erp_agents.core.ops import InstanceLock; l = InstanceLock(%r).acquire(); import os; print('held', os.getpid(), flush=True); time.sleep(60)"
            % (str(Path(__file__).resolve().parent.parent / "src"), path))
    child = subprocess.Popen([PY, "-I", "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        word, pid = child.stdout.readline().split()                                                       # the venv's python.exe is a launcher: the real process has its own id
        assert word == "held"
        with pytest.raises(ops.AlreadyRunning, match=f"process {pid}"):
            ops.InstanceLock(path).acquire()
    finally:
        child.kill()
        child.wait()
    time.sleep(0.3)
    ops.InstanceLock(path).acquire().release()                                                            # no stale lock to clean up after a crash


def test_serve_does_not_start_a_second_copy(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(f"database: {{path: '{(tmp_path / 'x.db').as_posix()}'}}\nerp: {{type: mock}}\n", encoding="utf-8")
    ran = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: ran.append(1))
    with ops.InstanceLock(str(tmp_path / "x.db")):
        assert main(["--config", str(cfg), "serve"]) == 1
    assert "Not started: Another instance" in capsys.readouterr().out and not ran
    assert main(["--config", str(cfg), "serve"]) == 0 and ran == [1]                                       # and it starts when nobody else holds the database


# ====================================================================== backup and restore
def test_a_backup_is_a_complete_verified_copy_made_while_the_database_is_open(tmp_path):
    path = fresh(tmp_path, rows=5)
    live = Database(path)                                                                                 # in use, with its write-ahead log
    live.execute("INSERT INTO settings_overrides(scope_type, scope_key, data_json, updated_by, updated_at) VALUES ('item','LIVE','{}','t','x')")
    made = ops.backup_database(path)
    live.close()
    assert made.parent.name == "backups" and made.suffix == ".db" and rows_in(str(made)) == 6
    assert ops.verify_database(str(made)).ok
    assert not [p for p in made.parent.iterdir() if p.suffix in (".db-wal", "-wal", "-shm")]              # one self-contained file


def test_a_backup_never_overwrites_a_file_or_the_database_itself(tmp_path):
    path = fresh(tmp_path)
    target = tmp_path / "keep.db"
    target.write_bytes(b"precious")
    with pytest.raises(ops.OpsError, match="already exists"):
        ops.backup_database(path, str(target))
    assert target.read_bytes() == b"precious"
    with pytest.raises(ops.OpsError, match="itself|already exists"):
        ops.backup_database(path, path)
    with pytest.raises(ops.OpsError, match="no database file"):
        ops.backup_database(str(tmp_path / "missing.db"))


def test_verify_tells_a_good_file_from_a_damaged_empty_foreign_or_newer_one(tmp_path):
    good = ops.backup_database(fresh(tmp_path, rows=40), str(tmp_path / "good.db"))
    assert ops.verify_database(str(good)).ok
    bad = tmp_path / "damaged.db"
    data = bytearray(good.read_bytes())
    for i in range(4096 * 2, min(len(data), 4096 * 4)):                                                   # overwrite whole pages
        data[i] = 0xAB
    bad.write_bytes(bytes(data))
    assert not ops.verify_database(str(bad)).ok
    for name, content in (("empty.db", b""), ("text.db", b"this is not a database at all, just words" * 50)):
        (tmp_path / name).write_bytes(content)
        assert not ops.verify_database(str(tmp_path / name)).ok, name
    assert "does not exist" in ops.verify_database(str(tmp_path / "nope.db")).explain()
    newer = tmp_path / "newer.db"
    newer.write_bytes(good.read_bytes())
    c = sqlite3.connect(newer)
    c.execute("UPDATE schema_info SET version=?", (storage.SCHEMA_VERSION + 1,))
    c.commit()
    c.close()
    assert "newer" in ops.verify_database(str(newer)).explain()
    holes = tmp_path / "holes.db"
    c = sqlite3.connect(holes)
    c.execute("CREATE TABLE schema_info (version INTEGER)")
    c.execute("INSERT INTO schema_info VALUES (1)")
    c.commit()
    c.close()
    assert "tables are missing" in ops.verify_database(str(holes)).explain()


def test_restore_puts_the_backup_back_and_keeps_what_it_replaced(tmp_path):
    path = fresh(tmp_path, rows=3)
    saved = ops.backup_database(path)
    db = Database(path)
    db.execute("DELETE FROM settings_overrides")                                                          # the accident
    db.execute("INSERT INTO settings_overrides(scope_type, scope_key, data_json, updated_by, updated_at) VALUES ('item','AFTER','{}','t','x')")
    db.close()
    kept = ops.restore_database(str(saved), path, force=True)
    assert rows_in(path) == 3 and kept is not None and kept.exists() and rows_in(str(kept)) == 1             # restored; the replaced one is still there
    assert ".before-restore-" in kept.name and ops.verify_database(path, live=True).ok
    Database(path).close()                                                                                 # and the application opens it


def test_restore_asks_before_replacing_and_refuses_what_it_should(tmp_path):
    path = fresh(tmp_path, rows=3)
    saved = ops.backup_database(path)
    with pytest.raises(ops.OpsError, match="--force"):
        ops.restore_database(str(saved), path)
    assert rows_in(path) == 3
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database" * 100)
    with pytest.raises(ops.OpsError, match="not a usable backup"):
        ops.restore_database(str(junk), path, force=True)
    assert rows_in(path) == 3
    newer = tmp_path / "newer.db"
    newer.write_bytes(saved.read_bytes())
    c = sqlite3.connect(newer)
    c.execute("UPDATE schema_info SET version=?", (storage.SCHEMA_VERSION + 1,))
    c.commit()
    c.close()
    with pytest.raises(ops.OpsError, match="newer"):
        ops.restore_database(str(newer), path, force=True)
    with pytest.raises(ops.OpsError, match="itself"):
        ops.restore_database(path, path, force=True)
    assert rows_in(path) == 3 and not [p for p in tmp_path.iterdir() if ".before-restore-" in p.name]


def test_restore_refuses_while_the_application_is_running(tmp_path):
    path = fresh(tmp_path, rows=3)
    saved = ops.backup_database(path)
    with ops.InstanceLock(path):
        with pytest.raises(ops.OpsError, match="Stop the application before restoring"):
            ops.restore_database(str(saved), path, force=True)
    assert rows_in(path) == 3


def test_restore_into_a_new_place_needs_no_force_and_keeps_nothing_aside(tmp_path):
    saved = ops.backup_database(fresh(tmp_path, rows=2))
    target = tmp_path / "new" / "fresh.db"
    assert ops.restore_database(str(saved), str(target)) is None and rows_in(str(target)) == 2


def test_the_commands_round_trip(tmp_path, capsys):
    db = tmp_path / "data" / "app.db"
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(f"database: {{path: '{db.as_posix()}'}}\nerp: {{type: mock}}\n", encoding="utf-8")
    db.parent.mkdir(parents=True)
    fresh_db = Database(str(db))
    fresh_db.execute("INSERT INTO settings_overrides(scope_type, scope_key, data_json, updated_by, updated_at) VALUES ('item','A','{}','t','x')")
    fresh_db.close()
    assert main(["--config", str(cfg), "backup"]) == 0
    assert "Backup made and verified" in capsys.readouterr().out
    (made,) = list((tmp_path / "data" / "backups").iterdir())
    assert main(["--config", str(cfg), "backup-verify", str(made)]) == 0 and "ok:" in capsys.readouterr().out
    assert main(["--config", str(cfg), "backup", "--to", str(made)]) == 1 and "never overwritten" in capsys.readouterr().out
    assert main(["--config", str(cfg), "restore", str(made)]) == 1 and "--force" in capsys.readouterr().out
    assert main(["--config", str(cfg), "restore", str(made), "--force"]) == 0 and "is kept as" in capsys.readouterr().out
    (tmp_path / "bad.db").write_bytes(b"x" * 200)
    assert main(["--config", str(cfg), "backup-verify", str(tmp_path / "bad.db")]) == 1


# ====================================================================== the doctor
ICACLS_OPEN = r"""C:\ERP\erp-agents\data NT AUTHORITY\SYSTEM:(OI)(CI)(F)
                       BUILTIN\Administrators:(OI)(CI)(F)
                       NT AUTHORITY\Authenticated Users:(I)(OI)(CI)(M)
                       BUILTIN\Users:(I)(OI)(CI)(RX)
                       DESKTOP-1\owner:(I)(OI)(CI)(F)

Successfully processed 1 files; Failed processing 0 files
"""
ICACLS_CLOSED = r"""C:\ERP\erp-agents\.env DESKTOP-1\owner:(F)
                       NT AUTHORITY\SYSTEM:(F)
                       BUILTIN\Administrators:(F)
"""


def test_the_windows_access_list_is_read_correctly():
    entries = doctor.parse_icacls(ICACLS_OPEN)
    assert ("NT AUTHORITY\\SYSTEM", {"F"}, False) in entries and ("BUILTIN\\Users", {"RX"}, False) in entries
    assert doctor.broad_access(entries, doctor.READ_RIGHTS) == ["NT AUTHORITY\\Authenticated Users", "BUILTIN\\Users"]
    assert doctor.broad_access(entries, doctor.WRITE_RIGHTS) == ["NT AUTHORITY\\Authenticated Users"]
    assert doctor.broad_access(doctor.parse_icacls(ICACLS_CLOSED), doctor.READ_RIGHTS) == []
    assert doctor.broad_access(doctor.parse_icacls(r"x Everyone:(DENY)(F)"), doctor.READ_RIGHTS) == []        # a denial is not access


def test_a_secrets_file_open_to_every_local_user_is_flagged_with_the_fix_as_text(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("X=1")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(doctor, "_icacls", lambda p: ICACLS_OPEN)
    c = doctor.permission_check(env, ".env", secret=True)
    assert c.status == doctor.WARN and "Users" in c.title and c.fix.startswith("icacls ") and "/inheritance:r" in c.fix
    monkeypatch.setattr(doctor, "_icacls", lambda p: ICACLS_CLOSED)
    assert doctor.permission_check(env, ".env", secret=True).status == doctor.OK
    monkeypatch.setattr(doctor, "_icacls", lambda p: None)
    assert doctor.permission_check(env, ".env", secret=True).status == doctor.INFO


@pytest.mark.skipif(WINDOWS, reason="POSIX permission bits")
def test_posix_modes_are_checked(tmp_path):
    f = tmp_path / ".env"
    f.write_text("X=1")
    f.chmod(0o644)
    assert doctor.permission_check(f, ".env", secret=True).status == doctor.WARN
    f.chmod(0o600)
    assert doctor.permission_check(f, ".env", secret=True).status == doctor.OK
    d = tmp_path / "data"
    d.mkdir()
    d.chmod(0o777)
    assert doctor.permission_check(d, "data", secret=False).status == doctor.WARN


def cfg_for(tmp_path, **parts):
    base = {"database": {"path": str(tmp_path / "data" / "app.db")}, "erp": {"type": "mock"}, "auth": {}, "server": {}, "approvals": {"recipients": []}}
    base.update(parts)
    return base


def by(checks, area):
    return [c for c in checks if c.area == area]


def test_access_keys_anywhere_with_a_weak_key_is_something_to_fix_now(tmp_path):
    weak = cfg_for(tmp_path, auth={"access_keys": "anywhere", "tokens": [{"token": "demo-owner-token", "user": "owner", "roles": ["admin"]}]})
    (c,) = [c for c in by(doctor.check_access(weak), "Sign-in") if "any address" in c.title]
    assert c.status == doctor.FAIL and "owner" in c.detail and "demo-owner-token" not in json.dumps(c.__dict__)       # a secret is never printed
    strong = cfg_for(tmp_path, auth={"access_keys": "anywhere", "tokens": [{"token": "s" * 40, "user": "owner", "roles": ["admin"]}]})
    assert [c.status for c in doctor.check_access(strong) if "any address" in c.title] == [doctor.WARN]
    assert doctor.check_access(cfg_for(tmp_path))[0].status == doctor.OK                                              # the default
    assert doctor.check_access(cfg_for(tmp_path, auth={"access_keys": "everywhere"}))[0].status == doctor.WARN


def test_a_tunnel_that_exposes_the_console_is_flagged_and_webhooks_only_is_not(tmp_path):
    chans = {"whatsapp": {"enabled": True}}
    open_ = doctor.check_network(cfg_for(tmp_path, channels=chans))
    assert any(c.status == doctor.WARN and "tunnel exposes the console" in c.title and "webhooks_only" in c.fix for c in open_)
    closed = doctor.check_network(cfg_for(tmp_path, channels=chans, server={"remote_access": "webhooks_only"}))
    assert any(c.status == doctor.OK and "only the channel webhooks" in c.title for c in closed) and not any(c.status == doctor.WARN for c in closed)
    assert any(c.status == doctor.INFO for c in doctor.check_network(cfg_for(tmp_path)))                              # no webhooks, no tunnel needed
    assert any(c.status == doctor.WARN and "0.0.0.0" in c.title for c in doctor.check_network(cfg_for(tmp_path, server={"host": "0.0.0.0"})))


def test_mcp_exposure_is_reported(tmp_path):
    assert by(doctor.check_network(cfg_for(tmp_path, mcp={"http": True, "remote": True, "allowed_hosts": ["s.example.com"]})), "MCP")[0].status == doctor.WARN
    assert by(doctor.check_network(cfg_for(tmp_path, mcp={"http": True, "remote": True})), "MCP")[0].status == doctor.FAIL
    assert by(doctor.check_network(cfg_for(tmp_path, mcp={"http": True})), "MCP")[0].status == doctor.OK


def test_a_secret_written_into_the_settings_file_is_found_and_not_shown(tmp_path):
    f = tmp_path / "settings.yaml"
    f.write_text("erp:\n  api_key: ${ERPNEXT_API_KEY}\n  api_secret: abcdef1234567890SECRET\nauth:\n  tokens:\n    - { token: \"${ERP_AGENTS_OWNER_TOKEN:-demo-owner-token}\", user: owner }\n", encoding="utf-8")
    checks = doctor.check_secrets_in_files(f)
    bad = [c for c in checks if c.status == doctor.FAIL]
    assert bad and "line 3" in bad[0].detail and "SECRET" not in json.dumps([c.__dict__ for c in checks])
    assert any(c.status == doctor.INFO and "demo key" in c.title for c in checks)
    clean = tmp_path / "clean.yaml"
    clean.write_text("erp:\n  api_key: ${K}\n  api_secret: ${S}\n  type: erpnext\n", encoding="utf-8")
    assert [c.status for c in doctor.check_secrets_in_files(clean)] == [doctor.OK]


def test_the_shipped_settings_files_have_no_literal_secret():
    root = Path(__file__).resolve().parent.parent / "config"
    for name in ("settings.yaml", "settings.erpnext.yaml", "settings.example.yaml"):
        assert not [c for c in doctor.check_secrets_in_files(root / name) if c.status == doctor.FAIL], name


def test_database_backup_and_instance_findings(tmp_path):
    path = fresh(tmp_path, rows=2)
    cfg = cfg_for(tmp_path)
    cfg["database"]["path"] = path
    now = time.time()
    first = doctor.check_database(path, now)
    assert any(c.area == "Database" and c.status == doctor.OK for c in first)
    assert any(c.area == "Backups" and c.status == doctor.WARN and "erp-agents backup" in c.fix for c in first)
    assert any(c.area == "Instance" and c.status == doctor.OK for c in first)
    made = ops.backup_database(path)
    assert any(c.area == "Backups" and c.status == doctor.OK for c in doctor.check_database(path, now))
    assert any(c.area == "Backups" and c.status == doctor.WARN for c in doctor.check_database(path, now + 30 * 86400))
    with ops.InstanceLock(path):
        assert any(c.area == "Instance" and c.status == doctor.INFO and "running" in c.title for c in doctor.check_database(path, now))
    made.unlink()
    Path(path).write_bytes(b"garbage" * 1000)
    assert any(c.area == "Database" and c.status == doctor.FAIL and "restore" in c.fix for c in doctor.check_database(path, now))
    assert doctor.check_database(str(tmp_path / "absent.db"), now)[0].status == doctor.INFO


def test_approver_identities_are_listed_with_the_limit_stated_and_risky_setups_flagged(tmp_path):
    recipients = [{"id": "owner", "roles": ["admin", "approver"], "channels": [{"type": "telegram", "address": "555"}, {"type": "inbox"}]},
                  {"id": "mgr", "roles": ["approver"], "channels": [{"type": "whatsapp", "address": "+91 98000-00000"}, {"type": "telegram", "address": "555"}]}]
    out = doctor.check_approvers(cfg_for(tmp_path, approvals={"recipients": recipients}))
    text = " ".join(c.title + " " + c.detail for c in out)
    assert any(c.status == doctor.WARN and "admin role" in c.title for c in out)
    assert any(c.status == doctor.WARN and "share one telegram address" in c.title for c in out)
    assert "NOT checked against ERPNext" in text
    assert doctor.check_approvers(cfg_for(tmp_path))[0].status == doctor.WARN                                        # nobody can approve


def test_the_service_account_is_reported_as_not_checked_because_that_needs_erpnext(tmp_path):
    (c,) = doctor.check_erp_account(cfg_for(tmp_path, erp={"type": "erpnext"}))
    assert c.status == doctor.INFO and "erp-check-access" in c.fix
    assert doctor.check_erp_account(cfg_for(tmp_path)) == []


def test_the_doctor_makes_no_network_call_and_changes_nothing(tmp_path, monkeypatch, capsys):
    def no_network(*a, **k):
        raise AssertionError("the doctor tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    db = fresh(tmp_path, rows=2)
    cfg = tmp_path / "config" / "settings.yaml"
    cfg.parent.mkdir()
    cfg.write_text(f"database: {{path: '{Path(db).as_posix()}'}}\nerp:\n  type: erpnext\n  url: http://erp.invalid\n  api_key: ${{K}}\n  api_secret: ${{S}}\n"
                   "channels:\n  whatsapp: {enabled: true}\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    code = main(["--config", str(cfg), "doctor"])
    out = capsys.readouterr().out
    assert "read-only" in out and "ok," in out and "to look at" in out and "erp-check-access" in out
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and p.suffix != ".lock"}
    assert {p: b for p, b in before.items() if p.suffix != ".lock"} == {p: b for p, b in after.items() if p in before}      # nothing was written or changed
    assert code in (0, 1)


def test_doctors_exit_code_follows_what_is_wrong(tmp_path, capsys):
    cfg = tmp_path / "config" / "settings.yaml"
    cfg.parent.mkdir()
    cfg.write_text(f"database: {{path: '{(tmp_path / 'a.db').as_posix()}'}}\nerp: {{type: mock}}\nmcp: {{http: true, remote: true}}\n", encoding="utf-8")
    assert main(["--config", str(cfg), "doctor"]) == 1 and "FAIL" in capsys.readouterr().out                     # remote MCP with no allowed hosts
    cfg.write_text(f"database: {{path: '{(tmp_path / 'a.db').as_posix()}'}}\nerp: {{type: mock}}\n", encoding="utf-8")
    assert main(["--config", str(cfg), "doctor"]) == 0
    assert main(["--config", str(cfg), "doctor", "--strict"]) in (0, 1)


# ====================================================================== a pack's tables: versioned apart from the core's
PACK_DDL = "CREATE TABLE IF NOT EXISTS demo_things (id INTEGER PRIMARY KEY, name TEXT);"


def test_a_packs_tables_are_made_once_and_versioned_apart_from_the_cores(tmp_path):
    path = str(tmp_path / "app.db")
    db = Database(path)
    db.register_schema("demo", PACK_DDL, 1)
    db.register_schema("demo", PACK_DDL, 1)                                                              # registering again is a no-op
    db.execute("INSERT INTO demo_things(name) VALUES ('kept')")
    assert [dict(r) for r in db.query("SELECT pack, version FROM pack_schema")] == [{"pack": "demo", "version": 1}]
    assert version_in(path) == storage.SCHEMA_VERSION                                                    # the core's version is its own
    db.close()
    again = Database(path)
    again.register_schema("demo", PACK_DDL, 1)
    assert again.query_one("SELECT name FROM demo_things")["name"] == "kept"
    again.close()


def test_a_database_made_before_packs_existed_gets_a_version_row_and_keeps_its_data(tmp_path):
    path = str(tmp_path / "legacy.db")
    c = sqlite3.connect(path)
    c.executescript(PACK_DDL + "INSERT INTO demo_things(name) VALUES ('old row');")                       # the tables are already there, and there is no pack_schema table at all
    c.commit()
    c.close()
    db = Database(path)
    db.register_schema("demo", PACK_DDL, 1)
    assert db.query_one("SELECT version FROM pack_schema WHERE pack='demo'")["version"] == 1 and db.query_one("SELECT name FROM demo_things")["name"] == "old row"
    db.close()


def test_a_pack_schema_from_a_newer_version_is_refused_untouched(tmp_path):
    path = str(tmp_path / "app.db")
    db = Database(path)
    db.register_schema("demo", PACK_DDL, 3)
    db.close()
    before = Path(path).read_bytes()
    db = Database(path)
    with pytest.raises(SchemaError, match="demo tables.*newer version.*Nothing was changed"):
        db.register_schema("demo", PACK_DDL, 2)
    db.close()
    assert [p.name for p in tmp_path.iterdir() if "before-schema" in p.name] == []


def test_a_pack_schema_is_upgraded_one_step_at_a_time_after_a_copy(tmp_path):
    path = str(tmp_path / "app.db")
    db = Database(path)
    db.register_schema("demo", PACK_DDL, 1)
    db.execute("INSERT INTO demo_things(name) VALUES ('row')")
    db.close()
    steps = []
    db = Database(path)
    db.register_schema("demo", PACK_DDL, 3, {2: lambda conn: (steps.append(2), conn.execute("ALTER TABLE demo_things ADD COLUMN colour TEXT")), 3: lambda conn: steps.append(3)})
    assert steps == [2, 3] and db.query_one("SELECT version FROM pack_schema WHERE pack='demo'")["version"] == 3 and "colour" in db.query_one("SELECT * FROM demo_things").keys()
    db.close()
    (copy,) = [p for p in tmp_path.iterdir() if ".before-schema-demo-1-" in p.name]
    assert rows_of(str(copy), "demo_things") == 1


def test_a_failed_pack_migration_changes_nothing(tmp_path):
    path = str(tmp_path / "app.db")
    db = Database(path)
    db.register_schema("demo", PACK_DDL, 1)
    db.close()
    db = Database(path)

    def broken(conn):
        conn.execute("ALTER TABLE demo_things ADD COLUMN half_done TEXT")
        raise RuntimeError("boom")
    with pytest.raises(SchemaError, match="demo tables to schema version 2 failed.*Nothing was changed"):
        db.register_schema("demo", PACK_DDL, 2, {2: broken})
    assert db.query_one("SELECT version FROM pack_schema WHERE pack='demo'")["version"] == 1 and "half_done" not in [r["name"] for r in db.query("PRAGMA table_info(demo_things)")]
    with pytest.raises(SchemaError, match="no migration for version"):
        db.register_schema("demo", PACK_DDL, 2, {})
    db.close()


def rows_of(path, table):
    c = sqlite3.connect(path)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def test_the_stock_packs_tables_are_made_by_the_pack_not_the_core(tmp_path):
    from erp_agents.packs.stock_procurement.inventory.alerts import AlertRepo
    from erp_agents.packs.stock_procurement import schema as stock
    db = Database(str(tmp_path / "stock.db"))
    tables = lambda: {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}      # noqa: E731
    assert not ({"alerts", "proposals", "stock_notices", "chat_log", "assistant_answers", "agent_runs"} & tables())      # a bare core database has none of them
    AlertRepo(db)                                                                                         # the pack's repository registers its schema when it is built
    assert {"alerts", "proposals", "stock_notices", "chat_log", "assistant_answers", "agent_runs"} <= tables()
    assert db.query_one("SELECT version FROM pack_schema WHERE pack=?", (stock.NAME,))["version"] == stock.VERSION
    db.close()


def test_a_database_with_the_old_combined_schema_opens_and_keeps_every_row(tmp_path):
    """Before packs, one SCHEMA made every table. A file made then has the stock tables, data in them, and no pack_schema table."""
    from erp_agents.packs.stock_procurement import schema as stock
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript(storage.SCHEMA.replace("CREATE TABLE IF NOT EXISTS pack_schema (", "CREATE TABLE IF NOT EXISTS ignored_pack_schema ("))
    c.executescript(stock.SCHEMA)
    c.execute("DROP TABLE ignored_pack_schema")
    c.execute("INSERT INTO schema_info(version) VALUES (1)")
    c.execute("INSERT INTO alerts(id, item_code, kind, severity, status, created_at, updated_at, analysis_json, reasoning_text) VALUES ('a1','BOLT-M8','BREACH',3,'awaiting','2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00','{}','low')")
    c.commit()
    c.close()
    from erp_agents.packs.stock_procurement.inventory.alerts import AlertRepo
    db = Database(path)
    repo = AlertRepo(db)
    assert repo.latest_for_item("BOLT-M8").id == "a1"
    db.close()

"""`erp-agents doctor`: a read-only check of how this installation is set up, against the risks found in the penetration test (docs/PENTEST_REPORT.md) and the 25 Oct 2026 review.

It looks at THIS machine's files and settings only: it makes no network call (not to ERPNext, WhatsApp, Telegram or anything else), changes nothing, and never prints a secret.
Each finding says what was seen and, where there is one, the command that fixes it, as text for the owner to run: the doctor does not change permissions or settings itself.

It also looks at how the installation is RUNNING, from the database file alone (opened so that it cannot be written, and fine while the application is up): whether the scheduler's
heartbeat is current, whether a scheduled job failed its last run, free disk, a record dated in the future (the clock), requests that wait longer than `observability.pending_hours`,
and the seal over the decision history. The live view (is ERPNext slow, how late the scheduler runs) is in the metrics, docs/OBSERVABILITY.md.

What it cannot see, and says so: whether the ERPNext service account holds more roles than it needs (that needs ERPNext: run `erp-agents erp-check-access`), and anything outside
this computer (the firewall, the tunnel, ERPNext's own exposure).
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess  # nosec B404 - used once, to read a file's access list with icacls on Windows
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import obs
from .api.hardening import ACCESS_KEY_MODES, MIN_KEY_LENGTH, access_key_mode, demo_key_problem, weak_key, well_known_key_users
from .audit import AuditChain
from .common import parse_dt
from .config import HASH_KEY_ENV, hash_key_state
from .jobs import HEARTBEAT_STALE_MINUTES, MAINTENANCE_JOB
from .ops import AlreadyRunning, InstanceLock, verify_database
from .storage import SCHEMA_VERSION

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"
LOOPBACK = ("127.0.0.1", "::1", "localhost")
SECRET_KEY = re.compile(r"^\s*-?\s*\{?\s*([A-Za-z_]*(?:token|secret|password|api_key|apikey|key)[A-Za-z_]*)\s*:\s*(.*?)\s*\}?\s*$", re.I)
BROAD = ("everyone", "builtin\\users", "nt authority\\authenticated users", "users", "authenticated users", "builtin\\guests", "guests")
READ_RIGHTS = {"F", "M", "RX", "R", "GR", "GA", "RD", "REA"}
WRITE_RIGHTS = {"F", "M", "W", "GW", "GA", "WD", "AD", "WEA"}
BACKUP_STALE_DAYS = 7
DISK_FAIL_MB, DISK_WARN_MB = 50, 500            # free space beside the database: below the first the next write is likely to fail, below the second (or three times the database) it is time to act
CLOCK_TOLERANCE_MINUTES = 5                     # a record dated further ahead than this means the computer's clock is wrong (or was set back)
# every date the core tables hold, to find the newest one
CLOCK_COLUMNS = (("approvals", "created_at"), ("approvals", "decided_at"), ("audit_chain", "at"), ("settings_audit", "ts"), ("job_runs", "last_start"), ("job_runs", "last_end"))


@dataclass
class Check:
    area: str
    status: str
    title: str
    detail: str = ""
    fix: str = ""


# ---------------------------------------------------------------------------------------------------------------------- permissions
def parse_icacls(output: str) -> list[tuple[str, set[str], bool]]:
    """[(principal, rights, denied)] from `icacls <path>` output. Rights are the letters such as F, M, RX, R, W; inheritance flags (I, OI, CI, IO, NP) are left out."""
    entries: list[tuple[str, set[str], bool]] = []
    for raw in output.splitlines():
        line = raw.strip()
        m = re.search(r"(?:^|\s)([^\s:()][^:()]*?):((?:\([A-Za-z,]+\))+)\s*$", line)
        if not m:
            continue
        groups = re.findall(r"\(([A-Za-z,]+)\)", m.group(2))
        flags = {"I", "OI", "CI", "IO", "NP"}
        denied = "DENY" in groups
        rights = {r for g in groups for r in g.split(",") if r not in flags and r != "DENY"}
        entries.append((m.group(1).strip(), rights, denied))
    return entries


def broad_access(entries: list[tuple[str, set[str], bool]], need: set[str]) -> list[str]:
    """Names of everyday-wide groups (Everyone, Users, Authenticated Users) that hold one of the rights in `need`."""
    out = []
    for principal, rights, denied in entries:
        if denied:
            continue
        name = principal.lower().rsplit("\\", 1)[-1] if principal.lower().startswith(("builtin\\", "nt authority\\")) else principal.lower()
        if (principal.lower() in BROAD or name in BROAD) and rights & need:
            out.append(principal)
    return out


def _icacls(path: Path) -> Optional[str]:
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
    if not exe.is_file():
        return None
    try:
        done = subprocess.run([str(exe), str(path)], capture_output=True, text=True, timeout=15, check=False)  # nosec B603 - a list with a path we built, no shell, no user input
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def permission_check(path: Path, what: str, secret: bool) -> Check:
    """Is this file (or folder) open to people who should not have it? `secret`: reading it is the problem; otherwise changing it is."""
    area = "Files"
    if not path.exists():
        return Check(area, INFO, f"{what}: not there", str(path))
    is_dir = path.is_dir()
    if sys.platform == "win32":
        text = _icacls(path)
        if text is None:
            return Check(area, INFO, f"{what}: permissions not checked", "icacls was not available or refused; run `icacls " + str(path) + "` yourself")
        who = broad_access(parse_icacls(text), READ_RIGHTS if secret else WRITE_RIGHTS)
        if who:
            grant = "(OI)(CI)F" if is_dir else "F"
            return Check(area, WARN, f"{what}: open to {', '.join(who)}", str(path),
                         f'icacls "{path}" /inheritance:r /grant:r "%USERNAME%:{grant}" /grant:r "SYSTEM:{grant}"   (then check the application still starts)')
        return Check(area, OK, f"{what}: not open to every local user", str(path))
    mode = path.stat().st_mode & 0o777
    bad = (mode & 0o077) if secret else (mode & 0o022)
    if bad:
        want = "700" if is_dir else "600"
        return Check(area, WARN, f"{what}: mode {mode:03o} lets other accounts {'read' if secret else 'change'} it", str(path), f'chmod {want} "{path}"')
    return Check(area, OK, f"{what}: mode {mode:03o}", str(path))


# ---------------------------------------------------------------------------------------------------------------------- the checks
def _loopback(host: Any) -> bool:
    return str(host or "127.0.0.1") in LOOPBACK


def check_access(cfg: dict[str, Any]) -> list[Check]:
    auth = cfg.get("auth") or {}
    out: list[Check] = []
    erp = cfg.get("erp") or {}
    keys = [t for t in auth.get("tokens") or [] if t.get("token")]
    weak = [t.get("user", "?") for t in keys if weak_key(str(t["token"]), int(auth.get("min_key_length", MIN_KEY_LENGTH)))]
    raw = str(auth.get("access_keys") or "").strip().lower()
    mode = access_key_mode(auth)
    known = well_known_key_users(auth)
    if known:
        who = ", ".join(sorted(set(known)))
        if demo_key_problem(cfg):
            out.append(Check("Sign-in", FAIL, "a well-known access key is configured: the assistant will not start", f"key(s) for: {who}",
                             "set a long random ERP_AGENTS_OWNER_TOKEN in .env (for a demo on the built-in demo ERP only: auth.allow_demo_key: true)"))
        else:
            out.append(Check("Sign-in", INFO, "a well-known demo key is allowed (demo ERP only)", f"auth.allow_demo_key is on for: {who}; the key works on this machine only",
                             "use a long random ERP_AGENTS_OWNER_TOKEN for anything but a demo"))
    if raw and raw not in ACCESS_KEY_MODES:
        out.append(Check("Sign-in", WARN, f"auth.access_keys is '{raw[:20]}', which is not local, anywhere or off", "treated as local", "write local, anywhere or off"))
    if mode == "anywhere":
        out.append(Check("Sign-in", FAIL if weak else WARN, "access keys are accepted from any address",
                         "auth.access_keys is 'anywhere'" + (f"; short or well-known key(s) for: {', '.join(weak)}" if weak else ""),
                         "set auth.access_keys: local (the default), and use long random keys"))
    elif mode == "off":
        out.append(Check("Sign-in", OK, "access keys are switched off", "only ERPNext sign-in works"))
    else:
        out.append(Check("Sign-in", OK, "access keys work on the server machine only", f"{len(keys)} key(s) configured" + (f"; weak ones ({', '.join(weak)}) are local-only anyway" if weak else "")))
    if erp.get("type") == "erpnext":
        if auth.get("erpnext_login", True) is False:
            out.append(Check("Sign-in", WARN, "ERPNext sign-in is switched off", "people can only use access keys", "remove auth.erpnext_login: false"))
        else:
            out.append(Check("Sign-in", OK, "people sign in with their ERPNext user", "what they may do follows ERPNext's own permissions"))
    else:
        out.append(Check("Sign-in", INFO, "demo mode: no ERPNext sign-in", "access keys are the only way in; keep the server on this machine"))
    return out


def check_network(cfg: dict[str, Any]) -> list[Check]:
    server = cfg.get("server") or {}
    channels = cfg.get("channels") or {}
    out: list[Check] = []
    host = server.get("host", "127.0.0.1")
    out.append(Check("Network", OK if _loopback(host) else WARN, f"the server listens on {host}",
                     "only this machine can reach it" if _loopback(host) else "anyone who can reach that address can reach the sign-in page",
                     "" if _loopback(host) else "server.host: 127.0.0.1, and put a proxy or tunnel in front of it if others need it"))
    remote = str(server.get("remote_access", "full")).strip().lower()
    wa = channels.get("whatsapp") or {}
    tg = channels.get("telegram") or {}
    webhooks = bool(wa.get("enabled")) or (bool(tg.get("enabled")) and str(tg.get("mode", "polling")).lower() == "webhook")
    if remote not in ("full", "webhooks_only"):
        out.append(Check("Network", WARN, f"server.remote_access is '{remote[:20]}'", "an unknown value is treated as full", "write full or webhooks_only"))
    elif remote == "webhooks_only":
        out.append(Check("Network", OK, "through a tunnel or proxy only the channel webhooks are reachable", "the console and the API are closed to relayed requests"))
    elif webhooks:
        out.append(Check("Network", WARN, "a tunnel exposes the console and the API, not only the webhooks",
                         "WhatsApp or Telegram webhooks need a public address, and remote_access is full", "server.remote_access: webhooks_only (if nobody needs the console through the tunnel)"))
    else:
        out.append(Check("Network", INFO, "remote_access is full", "relevant only if you run a tunnel or proxy; with none, nothing is reachable from outside"))
    mcp = cfg.get("mcp") or {}
    if mcp.get("http"):
        if mcp.get("remote") and not mcp.get("allowed_hosts"):
            out.append(Check("MCP", FAIL, "mcp.remote is on but mcp.allowed_hosts is empty", "the application will not start", "list the host names, or remove mcp.remote"))
        elif mcp.get("remote"):
            out.append(Check("MCP", WARN, "the MCP endpoint answers other machines", "it uses static bearer tokens, not MCP OAuth, and serves no discovery metadata",
                             "keep it on a private network or behind a VPN or an authenticating proxy"))
        else:
            out.append(Check("MCP", OK, "the MCP endpoint answers this machine only", "mcp.remote is off"))
    else:
        out.append(Check("MCP", OK, "the MCP HTTP endpoint is off", "stdio (`erp-agents mcp`) is unaffected"))
    return out


def check_hash_key(cfg: dict[str, Any]) -> list[Check]:
    """The secret that keys the hash of a vendor's number (finding F-8). Only WhatsApp replies from vendors need it; the key itself is never shown."""
    state = hash_key_state()
    if state == "ok":
        return [Check("Secrets", OK, "the key for the hashes of vendors' numbers is set", f"{HASH_KEY_ENV}: a vendor's number is stored only as a keyed hash")]
    whatsapp = bool(((cfg.get("channels") or {}).get("whatsapp") or {}).get("enabled"))
    what = "is not set" if state == "missing" else "is shorter than 32 characters"
    if whatsapp:
        return [Check("Secrets", WARN, f"{HASH_KEY_ENV} {what}", "a vendor's WhatsApp reply cannot be recognised, so replies to your reminders and price requests are not passed on",
                      "put a long random value (32+ characters) in .env, for example: python -c \"import secrets; print(secrets.token_urlsafe(32))\"")]
    return [Check("Secrets", INFO, f"{HASH_KEY_ENV} {what}", "only needed when vendors reply on WhatsApp (nothing is stored without it)", "put a long random value in .env when you switch WhatsApp on")]


def check_secrets_in_files(config_path: Optional[Path]) -> list[Check]:
    if config_path is None or not config_path.is_file():
        return []
    literal, demo = [], False
    for n, line in enumerate(config_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        code = line.split("#", 1)[0]
        m = SECRET_KEY.match(code)
        if not m:
            continue
        value = m.group(2).strip().strip("'\"").rstrip(",}").strip().strip("'\"")
        if not value or value in ("null", "~", "true", "false") or value.isdigit() or value.endswith(":"):
            continue
        if value.startswith("${"):
            demo = demo or "demo-owner-token" in value
            continue
        literal.append(f"line {n} ({m.group(1)})")
    out = []
    if literal:
        out.append(Check("Secrets", FAIL, "a secret is written into the settings file", f"{config_path.name}: {', '.join(literal[:5])}", "move it to .env and refer to it as ${NAME}"))
    else:
        out.append(Check("Secrets", OK, "no secret is written into the settings file", f"{config_path.name} refers to .env with ${{NAME}}"))
    if demo:
        out.append(Check("Secrets", INFO, "the settings file falls back to the well-known demo key", "it works on this machine only, never through a tunnel", "set ERP_AGENTS_OWNER_TOKEN in .env"))
    return out


def check_files(config_path: Optional[Path], env_path: Optional[Path], db_path: str) -> list[Check]:
    out: list[Check] = []
    if env_path is not None:
        out.append(permission_check(env_path, ".env (the secrets)", secret=True))
    if config_path is not None:
        out.append(permission_check(config_path, "the settings file", secret=False))
    if db_path != ":memory:":
        db = Path(db_path)
        out.append(permission_check(db, "the database file", secret=True))
        out.append(permission_check(db.parent, "the data folder", secret=False))
    return out


def check_database(db_path: str, now: float) -> list[Check]:
    if db_path == ":memory:":
        return [Check("Database", INFO, "an in-memory database", "nothing is kept between runs")]
    db = Path(db_path)
    if not db.is_file():
        return [Check("Database", INFO, "no database yet", f"{db} is created when the application first starts")]
    out: list[Check] = []
    v = verify_database(db_path, live=True)
    if v.ok:
        out.append(Check("Database", OK, f"the database is intact (schema version {v.version})", f"{v.tables} tables; this version writes schema {SCHEMA_VERSION}"
                         + ("; it will be upgraded on the next start, after a copy is made" if v.version is not None and v.version < SCHEMA_VERSION else "")))
    else:
        out.append(Check("Database", FAIL, "the database has a problem", v.explain(), "restore the latest backup: erp-agents restore <file> --force (stop the application first)"))
    backups = sorted((db.parent / "backups").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True) if (db.parent / "backups").is_dir() else []
    if not backups:
        out.append(Check("Backups", WARN, "no backup has been made with this tool", f"nothing in {db.parent / 'backups'}", "erp-agents backup   (and copy .env and the settings file too: they are not in the database)"))
    else:
        age = (now - backups[0].stat().st_mtime) / 86400
        out.append(Check("Backups", OK if age <= BACKUP_STALE_DAYS else WARN, f"the newest backup is {age:.0f} day(s) old", backups[0].name,
                         "" if age <= BACKUP_STALE_DAYS else "erp-agents backup"))
    try:
        with InstanceLock(db_path):
            out.append(Check("Instance", OK, "no other instance is using this database", "single-instance operation holds"))
    except AlreadyRunning as exc:
        out.append(Check("Instance", INFO, "an instance is running", str(exc)))
    except OSError as exc:
        out.append(Check("Instance", INFO, "could not test for a running instance", str(exc)[:100]))
    return out


def check_approvers(cfg: dict[str, Any]) -> list[Check]:
    recipients = (cfg.get("approvals") or {}).get("recipients") or []
    out: list[Check] = []
    seen: dict[tuple[str, str], str] = {}
    approvers = []
    for r in recipients:
        roles = set(r.get("roles") or [])
        chans = [c for c in r.get("channels") or [] if c.get("type") in ("whatsapp", "telegram")]
        if roles & {"approver", "admin"}:
            approvers.append((r.get("id", "?"), sorted(roles), [c["type"] for c in chans]))
        if "admin" in roles and chans:
            out.append(Check("Approvers", WARN, f"{r.get('id', '?')} holds the admin role and answers from {' / '.join(c['type'] for c in chans)}",
                             "a chat message could change what that role may do", "give chat recipients only the approver role"))
        for c in chans:
            key = (c["type"], re.sub(r"\W", "", str(c.get("address", ""))))
            if key[1] and key in seen and seen[key] != r.get("id"):
                out.append(Check("Approvers", WARN, f"{r.get('id', '?')} and {seen[key]} share one {c['type']} address", "the first listed wins when an answer arrives", "one address per person"))
            seen[key] = r.get("id", "?")
    if approvers:
        out.append(Check("Approvers", INFO, f"{len(approvers)} approver(s) can answer from a chat: " + "; ".join(f"{i} ({'/'.join(ch) or 'console only'})" for i, _, ch in approvers),
                         "A WhatsApp or Telegram answer is trusted because it comes from the address written in this file. It is NOT checked against ERPNext when it arrives: "
                         "if someone leaves, or loses a role in ERPNext, remove them here too."))
    else:
        out.append(Check("Approvers", WARN, "nobody is set up to approve", "no recipient has the approver or admin role", "add a recipient under approvals.recipients"))
    return out


def check_erp_account(cfg: dict[str, Any]) -> list[Check]:
    if (cfg.get("erp") or {}).get("type") != "erpnext":
        return []
    return [Check("ERPNext", INFO, "the service account's roles were not checked", "that needs ERPNext, and this check makes no network call",
                  "run erp-agents erp-check-access (read-only): it lists the roles the account holds and the surplus ones to remove")]


# ---------------------------------------------------------------------------------------------------------------------- how it is running
# Read from the database file alone (opened so that it cannot be written, and fine while the application is running): the scheduler's heartbeat, the free disk, the clock,
# the requests that wait for an answer, and the seal over the decision history. No network call.
class _ReadOnlyDb:
    """Just enough of `Database` for AuditChain.verify() and the queries below, over a connection that cannot write."""

    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=5)
        self.conn.row_factory = sqlite3.Row

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchone()

    def tables(self) -> set[str]:
        return {r["name"] for r in self.query("SELECT name FROM sqlite_master WHERE type='table'")}

    def close(self) -> None:
        self.conn.close()


def _ago(delta_minutes: float) -> str:
    minutes = abs(delta_minutes)
    if minutes < 90:
        return f"{minutes:.0f} minute(s)"
    if minutes < 48 * 60:
        return f"{minutes / 60:.0f} hour(s)"
    return f"{minutes / 1440:.0f} day(s)"


def _pending_hours(cfg: dict[str, Any]) -> float:
    try:
        return obs.settings_from(cfg).pending_hours
    except ValueError:                           # the start-up check reports a bad value; here the default will do
        return obs.ObsSettings().pending_hours


def _running_since(db_path: str) -> Optional[datetime]:
    """When the instance that holds this database started (the lock file is written then), or None when no instance is running."""
    try:
        with InstanceLock(db_path):
            return None
    except AlreadyRunning:
        try:
            return datetime.fromtimestamp(Path(f"{db_path}.lock").stat().st_mtime, timezone.utc)
        except OSError:
            return datetime.now(timezone.utc)
    except OSError:
        return None


def check_disk(db_path: str) -> list[Check]:
    db = Path(db_path)
    try:
        free_mb, size_mb = shutil.disk_usage(db.parent).free / 1048576, db.stat().st_size / 1048576
    except OSError as exc:
        return [Check("Disk", INFO, "could not read the free disk space", type(exc).__name__)]
    detail = f"{free_mb:,.0f} MB free beside the database; the database is {size_mb:,.1f} MB and a backup needs about that much again"
    fix = "free space on that drive, or move the data folder to a bigger one (database.path). A full disk makes the database refuse every write, and /ready then answers 503."
    if free_mb < DISK_FAIL_MB:
        return [Check("Disk", FAIL, "the disk is nearly full", detail, fix)]
    if free_mb < max(DISK_WARN_MB, 3 * size_mb):
        return [Check("Disk", WARN, "the disk is getting full", detail, fix)]
    return [Check("Disk", OK, "there is room on the disk", detail)]


def check_scheduler(db: _ReadOnlyDb, tables: set[str], db_path: str, when: datetime) -> list[Check]:
    if "job_runs" not in tables:
        return []                                # a database from before schema 2: nothing recorded yet (it is upgraded on the next start)
    out: list[Check] = []
    started = _running_since(db_path)
    if started is not None:
        row = db.query_one("SELECT last_start, last_end FROM job_runs WHERE job=?", (MAINTENANCE_JOB,))
        runs = [t for t in (parse_dt(row["last_end"]), parse_dt(row["last_start"])) if t] if row else []
        last = max(runs) if runs else None
        ran_since_start = last is not None and last >= started
        minutes = (when - (last if ran_since_start else started)).total_seconds() / 60       # a run before this instance started was another process's: the quiet is counted from the start
        if minutes > HEARTBEAT_STALE_MINUTES:
            out.append(Check("Scheduler", WARN, "an instance is running, but its scheduler has gone quiet",
                             f"the maintenance job (every 10 minutes) has not run for {_ago(minutes)}, so the daily checks, the reminders and the expiry of old requests are not happening",
                             "look in the log for 'approval maintenance failed', then restart the application"))
        else:
            out.append(Check("Scheduler", OK, "the scheduler is alive", f"the maintenance job last ran {_ago(minutes)} ago" if ran_since_start
                             else f"the instance started {_ago(minutes)} ago; the maintenance job has not come round yet"))
    failing = [r for r in db.query("SELECT job, runs, failures, last_error FROM job_runs WHERE last_ok=0 ORDER BY job")]
    if failing:
        out.append(Check("Scheduler", WARN, "a scheduled job failed the last time it ran: " + ", ".join(r["job"] for r in failing[:6]),
                         "; ".join(f"{r['job']}: {r['failures']} failure(s) in {r['runs']} run(s), the last '{r['last_error'] or 'failed'}'" for r in failing[:6]),
                         "the log has a line event=job_run for each run and the error just before it"))
    return out


def check_backlog(db: _ReadOnlyDb, tables: set[str], when: datetime, limit_hours: float) -> list[Check]:
    if "approvals" not in tables:
        return []
    row = db.query_one("SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM approvals WHERE status='pending'")
    if not row or not row["n"]:
        return [Check("Approvals", OK, "no request is waiting for an answer")]
    oldest = parse_dt(row["oldest"])
    hours = (when - oldest).total_seconds() / 3600 if oldest else 0.0
    detail = f"{row['n']} waiting; the oldest for {_ago(hours * 60)}; the limit is {limit_hours:g} hour(s) (observability.pending_hours)"
    if hours > limit_hours:
        return [Check("Approvals", WARN, "a request has waited longer than it should", detail,
                      "answer or dismiss it in the console (Requests), and check that the approvers receive the messages (a failed delivery is shown there)")]
    return [Check("Approvals", OK, f"{row['n']} request(s) waiting, none for too long", detail)]


def check_clock(db: _ReadOnlyDb, tables: set[str], when: datetime) -> list[Check]:
    newest: Optional[datetime] = None
    for table, column in CLOCK_COLUMNS:
        if table in tables:
            row = db.query_one(f"SELECT MAX({column}) AS m FROM {table}")  # nosec B608 - names from the constant above, no input
            found = parse_dt(row["m"]) if row and row["m"] else None
            if found and (newest is None or found > newest):
                newest = found
    if newest is None:
        return []
    ahead = (newest - when).total_seconds() / 60
    if ahead > CLOCK_TOLERANCE_MINUTES:
        return [Check("Clock", WARN, "a record in the database is dated in the future", f"the newest is {_ago(ahead)} ahead of this computer's clock, so the clock is wrong or was set back",
                      "set the date, time and time zone right and turn on automatic time (Windows: Settings > Time & language > Date & time > Sync now). Reminders and expiry follow this clock.")]
    return [Check("Clock", OK, "no record is dated in the future", "judged from the database's own dates; ERPNext's time is a metric (erp_agents_erpnext_clock_skew_seconds), not asked here")]


def check_history(db: _ReadOnlyDb, tables: set[str]) -> list[Check]:
    if "audit_chain" not in tables:
        return []
    result = AuditChain(db).verify()                                           # type: ignore[arg-type]  # only query() and query_one() are used: read-only
    if result["ok"]:
        waiting = f"; {result['not_sealed_yet']} closed request(s) are sealed the next time the application runs" if result["not_sealed_yet"] else ""
        return [Check("Decision history", OK, "the seal over the decision history is intact", f"{result['entries']} sealed entries{waiting}")]
    return [Check("Decision history", FAIL, "the decision history does not verify", "; ".join(result["problems"][:3]),
                  "a request or a settings change was edited or deleted after it was sealed. `erp-agents history --verify` lists them all. If you did not do it, restore the last "
                  "backup you trust (erp-agents restore <file> --force, with the application stopped)")]


def check_operation(cfg: dict[str, Any], db_path: str, now: float) -> list[Check]:
    if db_path == ":memory:" or not Path(db_path).is_file():
        return []
    when = datetime.fromtimestamp(now, timezone.utc)
    out = check_disk(db_path)
    try:
        db = _ReadOnlyDb(db_path)
    except sqlite3.Error as exc:
        return out + [Check("Operation", INFO, "the database could not be opened to see how it is running", type(exc).__name__)]
    try:
        tables = db.tables()
        out += check_scheduler(db, tables, db_path, when) + check_backlog(db, tables, when, _pending_hours(cfg)) + check_clock(db, tables, when) + check_history(db, tables)
    except (sqlite3.Error, ValueError) as exc:                               # damage is reported by the database check above; this one must not stop the rest
        out.append(Check("Operation", INFO, "part of the database could not be read", f"{type(exc).__name__}: {str(exc)[:80]}"))
    finally:
        db.close()
    return out


# ---------------------------------------------------------------------------------------------------------------------- run and show
def env_file_for(config_path: Optional[Path]) -> Optional[Path]:
    """The .env the application reads: the project root next to config/, else the current directory (as config.load_config does)."""
    candidates = [config_path.resolve().parent.parent / ".env"] if config_path else []
    candidates.append(Path.cwd() / ".env")
    return next((p for p in candidates if p.is_file()), candidates[0] if candidates else None)


def run_doctor(cfg: dict[str, Any], config_path: Optional[str] = None, now: Optional[float] = None) -> list[Check]:
    cp = Path(config_path) if config_path else None
    db_path = str((cfg.get("database") or {}).get("path", "./data/erp_agents.db"))
    checks: list[Check] = []
    checks += check_access(cfg) + check_network(cfg) + check_secrets_in_files(cp) + check_hash_key(cfg) + check_files(cp, env_file_for(cp), db_path)
    when = time.time() if now is None else now
    checks += check_database(db_path, when) + check_operation(cfg, db_path, when) + check_approvers(cfg) + check_erp_account(cfg)
    return checks


MARK = {OK: "OK  ", INFO: "info", WARN: "WARN", FAIL: "FAIL"}


def render(checks: list[Check]) -> str:
    lines = ["Stock Assistant: installation check (read-only; nothing was contacted or changed)", ""]
    area = None
    for c in checks:
        if c.area != area:
            area = c.area
            lines.append(f"{area}")
        lines.append(f"  [{MARK[c.status]}] {c.title}")
        if c.detail:
            lines.append(f"         {c.detail}")
        if c.fix and c.status in (WARN, FAIL, INFO):
            lines.append(f"         -> {c.fix}")
    n = {s: sum(1 for c in checks if c.status == s) for s in (OK, INFO, WARN, FAIL)}
    lines += ["", f"{n[OK]} ok, {n[INFO]} for information, {n[WARN]} to look at, {n[FAIL]} to fix now."]
    return "\n".join(lines)


def exit_code(checks: list[Check], strict: bool = False) -> int:
    return 1 if any(c.status == FAIL or (strict and c.status == WARN) for c in checks) else 0

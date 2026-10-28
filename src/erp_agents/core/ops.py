"""Running the single instance safely: one process per database, a consistent backup, a checked restore.

The application keeps everything it decides in one SQLite file (settings changes and their audit trail, alerts, approvals and who answered them, proposals, the record of agent
tasks). SQLite and the in-process scheduler do not support two copies running on the same file, and until now nothing stopped a second one. These helpers need no network and
change nothing outside the files named.

  InstanceLock      one running `serve` per database file (an advisory lock the operating system drops by itself if the process dies)
  backup_database   a consistent copy of a database that may be in use (SQLite's own backup API, safe with WAL)
  verify_database   is this file a complete, undamaged database of a version this code understands?
  restore_database  put a verified backup in place; the database it replaces is kept beside it, never deleted
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .storage import SCHEMA, SCHEMA_VERSION

# Tables that are made when the file is opened, so a complete database (or backup) from a version before them simply does not have them yet:
# pack_schema, and audit_chain (the decision history's seal: it seals the older decisions on the first start).
MADE_ON_OPEN = ("pack_schema", "audit_chain")
TABLE_SINCE = {"job_runs": 2}                         # tables that came with a later schema version: a file of an earlier version does not have them, and is not damaged for that
REQUIRED_TABLES = tuple(t for t in dict.fromkeys(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA)) if t not in MADE_ON_OPEN)


class AlreadyRunning(RuntimeError):
    """Another process is using this database."""


class OpsError(RuntimeError):
    """A backup or restore was refused. Nothing was changed."""


# ---------------------------------------------------------------------------------------------------------------------- one instance
class InstanceLock:
    """An exclusive advisory lock on `<database>.lock`. Held while `serve` runs; the operating system releases it if the process is killed, so there is no stale lock to clean up.
    The first byte is the locked region; the process id is written after it (on Windows a locked byte cannot be read by others)."""

    def __init__(self, db_path: str) -> None:
        self.path = Path(f"{db_path}.lock")
        self._fh: Optional[Any] = None

    def _holder(self) -> str:
        try:
            with open(self.path, "rb") as f:
                f.seek(1)
                text = f.read(24).decode("ascii", "ignore").strip()
            return text if text.isdigit() else "unknown"
        except OSError:
            return "unknown"

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fh = os.fdopen(fd, "r+b")
        try:
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise AlreadyRunning(f"Another instance (process {self._holder()}) is using this database ({str(self.path)[:-5]}). Two copies on one database would corrupt "
                                 "the approvals and alerts: stop the other one first.") from exc
        fh.seek(1)
        fh.truncate(1)
        fh.write(str(os.getpid()).encode("ascii"))
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------------------------------------------------- verify
@dataclass
class Verdict:
    path: str
    ok: bool
    version: Optional[int] = None
    problems: list[str] = field(default_factory=list)
    tables: int = 0

    def explain(self) -> str:
        return "ok: a complete database, schema version %s" % self.version if self.ok else "; ".join(self.problems)


def verify_database(path: str, live: bool = False) -> Verdict:
    """Read-only. `live`: the file may be in use (reads through its write-ahead log); otherwise it is treated as a stand-alone file such as a backup."""
    v = Verdict(str(path), False)
    p = Path(path)
    if not p.is_file():
        v.problems.append("the file does not exist")
        return v
    uri = f"file:{p.as_posix()}?mode=ro" + ("" if live else "&immutable=1")
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        v.problems.append(f"it cannot be opened ({exc})")
        return v
    try:
        try:
            result = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
        except sqlite3.DatabaseError as exc:
            v.problems.append(f"it is not a readable SQLite database ({str(exc)[:80]})")
            return v
        if result != ["ok"]:
            v.problems.append("SQLite found damage: " + "; ".join(result[:3]))
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        v.tables = len(names)
        if "schema_info" in names:
            row = conn.execute("SELECT version FROM schema_info").fetchone()
            v.version = int(row[0]) if row else None
        # a table that a later schema version added is not missing from a file of an earlier version: the upgrade makes it
        missing = [t for t in REQUIRED_TABLES if t not in names and TABLE_SINCE.get(t, 1) <= (v.version or SCHEMA_VERSION)]
        if missing:
            v.problems.append("tables are missing: " + ", ".join(missing[:6]) + (" ..." if len(missing) > 6 else ""))
        if v.version is None:
            v.problems.append("it carries no schema version")
        elif v.version > SCHEMA_VERSION:
            v.problems.append(f"its schema version {v.version} is newer than the {SCHEMA_VERSION} this version understands")
    except sqlite3.DatabaseError as exc:
        v.problems.append(f"it could not be read ({str(exc)[:80]})")
    finally:
        conn.close()
    v.ok = not v.problems
    return v


# ---------------------------------------------------------------------------------------------------------------------- backup
def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def default_backup_path(db_path: str) -> Path:
    p = Path(db_path)
    return p.parent / "backups" / f"{p.stem}-{_stamp()}{p.suffix or '.db'}"


def backup_database(db_path: str, dest: Optional[str] = None) -> Path:
    """A consistent copy of the database, made while it is in use, by SQLite's own backup. Refuses to overwrite a file. The copy is verified before it is reported."""
    src = Path(db_path)
    if db_path == ":memory:" or not src.is_file():
        raise OpsError(f"there is no database file at {db_path}")
    target = Path(dest) if dest else default_backup_path(db_path)
    if target.exists():
        raise OpsError(f"{target} already exists; choose another name (a backup is never overwritten)")
    if target.resolve() == src.resolve():
        raise OpsError("the backup cannot be the database itself")
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(src))
    out = sqlite3.connect(str(target))
    try:
        source.backup(out)
        out.execute("PRAGMA journal_mode=DELETE")          # one self-contained file, no -wal / -shm beside it
    except BaseException:
        out.close()
        target.unlink(missing_ok=True)
        raise
    finally:
        source.close()
        out.close()
    verdict = verify_database(str(target))
    if not verdict.ok:
        target.unlink(missing_ok=True)
        raise OpsError(f"the copy did not verify, so it was removed: {verdict.explain()}")
    return target


# ---------------------------------------------------------------------------------------------------------------------- restore
def restore_database(backup: str, db_path: str, force: bool = False) -> Optional[Path]:
    """Put a verified backup in place of the database. Returns where the replaced database was kept (None when there was none).

    Refused, with nothing changed: a backup that does not verify or is from a newer version; an existing database without `force`; a database that another process is using
    (stop the application first). The replaced database, and its -wal / -shm files, are renamed to `<file>.before-restore-<time>`, never deleted."""
    verdict = verify_database(backup)
    if not verdict.ok:
        raise OpsError(f"{backup} is not a usable backup: {verdict.explain()}")
    if db_path == ":memory:":
        raise OpsError("there is no database file to restore into")
    dest = Path(db_path)
    if dest.exists() and not force:
        raise OpsError(f"{dest} exists. Restoring replaces it (it is kept as a .before-restore copy); add --force to go ahead.")
    if dest.exists() and dest.resolve() == Path(backup).resolve():
        raise OpsError("the backup is the database itself")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = InstanceLock(db_path).acquire()
    except AlreadyRunning as exc:
        raise OpsError(f"{exc} Stop the application before restoring.") from exc
    try:
        staging = dest.with_name(dest.name + ".restore-tmp")
        staging.unlink(missing_ok=True)
        src, out = sqlite3.connect(f"file:{Path(backup).as_posix()}?mode=ro&immutable=1", uri=True), sqlite3.connect(str(staging))
        try:
            src.backup(out)
        finally:
            src.close()
            out.close()
        checked = verify_database(str(staging))
        if not checked.ok:
            staging.unlink(missing_ok=True)
            raise OpsError(f"the copy made for the restore did not verify, so nothing was changed: {checked.explain()}")
        kept: Optional[Path] = None
        if dest.exists():
            kept = dest.with_name(f"{dest.name}.before-restore-{_stamp()}")
            for suffix in ("", "-wal", "-shm"):
                side = Path(str(dest) + suffix)
                if side.exists():
                    side.rename(Path(str(kept) + suffix))
        os.replace(staging, dest)
        return kept
    finally:
        lock.release()

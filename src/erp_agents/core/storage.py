"""SQLite storage (stdlib only, no ORM).

One small database holds: admin overrides + audit, alerts, approvals + deliveries,
work orders (the hand-off queue to future agents) and agent run logs.
Thread-safe (single connection guarded by an RLock) so the API, scheduler and
Telegram poller can share it. Transactions nest via a depth counter.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

log = logging.getLogger(__name__)

# The shape of the database this code understands. `schema_info.version` in the file says which shape it has:
#   - a file with a HIGHER number was made by a newer version of this application: it is refused, untouched (an older version must not guess at tables it does not know);
#   - a file with a LOWER number is copied aside (`<file>.before-schema-N-<time>`) and then upgraded by MIGRATIONS, one version at a time, each in its own transaction.
# To change the schema: add the migration as MIGRATIONS[N+1] (a function taking the connection), raise SCHEMA_VERSION to N+1, and add a test (tests/test_operations.py).
SCHEMA_VERSION = 3
JOB_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS job_runs (
    job        TEXT PRIMARY KEY,         -- the scheduler's id for the job (approval_maintenance, inventory_check ...)
    last_start TEXT,                     -- when its last run began (UTC)
    last_end   TEXT,                     -- when it ended
    last_ok    INTEGER,                  -- 1 = ended well, 0 = failed
    last_error TEXT,                     -- the class of the failure, never its text
    runs       INTEGER NOT NULL DEFAULT 0,
    failures   INTEGER NOT NULL DEFAULT 0
)"""

INBOUND_SEEN_DDL = """
CREATE TABLE IF NOT EXISTS inbound_seen (
    channel     TEXT NOT NULL,          -- whatsapp | telegram
    external_id TEXT NOT NULL,          -- the id the channel gave the message (WhatsApp's message id, Telegram's update id)
    seen_at     TEXT NOT NULL,          -- when it was first seen (UTC): ids are forgotten after a few days
    PRIMARY KEY (channel, external_id)
)"""


def _v2(conn: sqlite3.Connection) -> None:
    """Version 2: the scheduler remembers when each job last ran and how it ended (core/jobs.py), for /ready, the metrics and `doctor`."""
    conn.execute(JOB_RUNS_DDL)


def _v3(conn: sqlite3.Connection) -> None:
    """Version 3: the ids of the messages a channel has delivered (core/approvals/inbound_ledger.py), to recognise one that is delivered again."""
    conn.execute(INBOUND_SEEN_DDL)


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {2: _v2, 3: _v3}
# Columns added to a table after it first shipped (before migrations existed): an existing database gets them when it is opened (CREATE TABLE IF NOT EXISTS does not change a
# table that is there). Safe to repeat, so it stays as it is.
ADDED_COLUMNS = (("agent_tasks", "provider", "TEXT"),)


class SchemaError(RuntimeError):
    """The database cannot be used by this version of the application (made by a newer one, or an upgrade failed). Nothing was changed."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS pack_schema (
    pack     TEXT PRIMARY KEY,       -- the pack that owns some tables (core/packs.py)
    version  INTEGER NOT NULL        -- the shape of its tables: see Database.register_schema
);

CREATE TABLE IF NOT EXISTS settings_overrides (
    scope_type  TEXT NOT NULL,           -- global | group | item | system
    scope_key   TEXT NOT NULL,
    data_json   TEXT NOT NULL,
    updated_by  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (scope_type, scope_key)
);

CREATE TABLE IF NOT EXISTS settings_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    scope_type  TEXT NOT NULL,
    scope_key   TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_json    TEXT,
    new_json    TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id               TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    status           TEXT NOT NULL,      -- pending|decided|expired|cancelled
    request_json     TEXT NOT NULL,
    decision_json    TEXT,
    ref_type         TEXT,
    ref_id           TEXT,
    created_at       TEXT NOT NULL,
    expires_at       TEXT,
    decided_at       TEXT,
    last_reminder_at TEXT,
    reminder_count   INTEGER NOT NULL DEFAULT 0,
    status_note      TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

CREATE TABLE IF NOT EXISTS deliveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_id  TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    channel      TEXT NOT NULL,
    address      TEXT,
    status       TEXT NOT NULL,          -- sent|failed|given_up
    external_id  TEXT,
    error        TEXT,
    attempts     INTEGER NOT NULL DEFAULT 1,
    sent_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_approval ON deliveries(approval_id);

CREATE TABLE IF NOT EXISTS work_orders (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,          -- e.g. REORDER_NEEDED
    status       TEXT NOT NULL,          -- pending|claimed|done|failed
    payload_json TEXT NOT NULL,
    source       TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- the AI agents: one row per task (what was asked, what came out, what was sent to the model) and one per tool call
CREATE TABLE IF NOT EXISTS agent_tasks (
    id            TEXT PRIMARY KEY,
    agent         TEXT NOT NULL,          -- inventory | purchasing
    task          TEXT NOT NULL,          -- brief | review | ask
    status        TEXT NOT NULL,          -- ok | rejected | fallback | failed
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    model         TEXT,
    provider      TEXT,                   -- ollama | anthropic | openai | gemini
    remote        INTEGER NOT NULL DEFAULT 0,
    steps         INTEGER NOT NULL DEFAULT 0,
    tool_calls    INTEGER NOT NULL DEFAULT 0,
    llm_seconds   REAL,
    prompt_tokens INTEGER,
    output_tokens INTEGER,
    approval_id   TEXT,
    asked_by      TEXT,
    request_text  TEXT,
    result_json   TEXT,
    note          TEXT,
    trace_json    TEXT                    -- the messages sent to the model and its replies (pruned after agents.keep_traces_days)
);
CREATE INDEX IF NOT EXISTS ix_agent_tasks_agent ON agent_tasks(agent, started_at);
CREATE INDEX IF NOT EXISTS ix_agent_tasks_approval ON agent_tasks(approval_id);

-- the seal over the decision history (core/audit.py): one row per closed request or settings change, each hash over the one before. Only ever appended to.
-- A new table that changes no other: it needs no schema version of its own (an older version of the application simply does not use it).
CREATE TABLE IF NOT EXISTS audit_chain (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    source   TEXT NOT NULL,              -- approval | setting
    ref_id   TEXT NOT NULL,
    event    TEXT NOT NULL,              -- decided | expired | cancelled | changed | sealed (closed before the chain existed)
    digest   TEXT NOT NULL,              -- sha256 of the record as it was
    prev     TEXT NOT NULL,
    hash     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_chain_ref ON audit_chain(source, ref_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    via       TEXT NOT NULL,              -- mcp-stdio | mcp-http | agent | api
    tool      TEXT NOT NULL,
    args_json TEXT,
    ok        INTEGER NOT NULL,
    ms        INTEGER,
    task_id   TEXT,
    error     TEXT
);
""" + JOB_RUNS_DDL + ";\n" + INBOUND_SEEN_DDL + ";\n"


class Database:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._depth = 0
        self._registered: set[str] = set()
        self._expected: dict[str, int] = {}                  # the version of each pack's tables that this code needs (register_schema)
        try:
            self._check_version()                    # before anything is written: a database from a newer version is left exactly as it is
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._init_schema()
        except BaseException:
            self._conn.close()
            raise

    def stored_version(self) -> Optional[int]:
        """The schema version written in the file; None for a database that has none yet (a new, empty file)."""
        has = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_info'").fetchone()
        row = self._conn.execute("SELECT version FROM schema_info").fetchone() if has else None
        return int(row["version"]) if row else None

    def _check_version(self) -> None:
        found = self.stored_version()
        if found is not None and found > SCHEMA_VERSION:
            raise SchemaError(f"This database ({self.path}) has schema version {found}, made by a newer version of the application; this version understands up to {SCHEMA_VERSION}. "
                              "Install the newer version, or restore a backup that this version made. Nothing was changed.")

    def _init_schema(self) -> None:
        with self._lock:
            found = self.stored_version()
            if found is not None and found < SCHEMA_VERSION:
                self._upgrade(found)
            self._conn.executescript(SCHEMA)
            for table, column, declaration in ADDED_COLUMNS:
                if column not in {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            row = self._conn.execute("SELECT version FROM schema_info").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))

    def _upgrade(self, found: int) -> None:
        """Bring an older core database up to SCHEMA_VERSION."""
        self._run_migrations("", found, SCHEMA_VERSION, MIGRATIONS, lambda v: self._conn.execute("UPDATE schema_info SET version=?", (v,)))

    def _run_migrations(self, owner: str, found: int, target: int, migrations: dict[int, Callable[[sqlite3.Connection], None]], set_version: Callable[[int], Any]) -> None:
        """A copy first (a file database), then one migration at a time, each all-or-nothing. `owner` names the pack whose tables are upgraded ("" = the core's)."""
        what = f"the {owner} tables" if owner else "the database"
        missing = [v for v in range(found + 1, target + 1) if v not in migrations]
        if missing:
            raise SchemaError(f"{what[0].upper() + what[1:]} have schema version {found} and this version needs {target}, but there is no migration for version(s) "
                              f"{', '.join(map(str, missing))}. Nothing was changed." if owner else
                              f"The database has schema version {found} and this version needs {target}, but there is no migration for version(s) "
                              f"{', '.join(map(str, missing))}. Nothing was changed.")
        copy_to = None
        if self.path != ":memory:":
            tag = f"{owner}-" if owner else ""
            copy_to = Path(f"{self.path}.before-schema-{tag}{found}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
            dest = sqlite3.connect(str(copy_to))
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
            log.warning("upgrading %s from schema %d to %d; a copy of the old one is at %s", what, found, target, copy_to)
        for version in range(found + 1, target + 1):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                migrations[version](self._conn)
                set_version(version)
                self._conn.execute("COMMIT")
            except BaseException as exc:
                self._conn.execute("ROLLBACK")
                raise SchemaError(f"Upgrading {what} to schema version {version} failed ({type(exc).__name__}: {str(exc)[:160]}). Nothing was changed"
                                  + (f"; the copy made before the upgrade is at {copy_to}." if copy_to else ".")) from exc

    def register_schema(self, owner: str, ddl: str, version: int = 1, migrations: Optional[dict[int, Callable[[sqlite3.Connection], None]]] = None,
                        added_columns: tuple[tuple[str, str, str], ...] = ()) -> None:
        """A pack's tables. `ddl` must be safe to repeat (CREATE ... IF NOT EXISTS), so a database made before packs existed, which already has these tables, simply gets a version
        row. The pack's version is kept in `pack_schema`, apart from the core's: a version newer than this code understands is refused untouched (the pack does not load), an
        older one is copied aside and upgraded by `migrations` exactly as the core's is. Registering the same owner again is a no-op."""
        with self._lock:
            if owner in self._registered:
                return
            row = self._conn.execute("SELECT version FROM pack_schema WHERE pack=?", (owner,)).fetchone()
            found = int(row["version"]) if row else None
            if found is not None and found > version:
                raise SchemaError(f"The {owner} tables in this database ({self.path}) have schema version {found}, made by a newer version of the application; this version "
                                  f"understands up to {version}. Install the newer version, or restore a backup that this version made. Nothing was changed.")
            if found is not None and found < version:
                self._run_migrations(owner, found, version, migrations or {}, lambda v: self._conn.execute("UPDATE pack_schema SET version=? WHERE pack=?", (v, owner)))
            self._conn.executescript(ddl)
            for table, column, declaration in added_columns:
                if column not in {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")  # nosec B608 - names come from the pack's own code, not from input
            if row is None:
                self._conn.execute("INSERT INTO pack_schema(pack, version) VALUES (?, ?)", (owner, version))
            self._registered.add(owner)
            self._expected[owner] = version

    def schema_status(self) -> list[tuple[str, Optional[int], int]]:
        """(who, the version in the file, the version this code needs) for the core and for every pack that registered tables: `/ready` says not ready when any differs."""
        with self._lock:
            rows = [("core", self.stored_version(), SCHEMA_VERSION)]
            for owner, needed in sorted(self._expected.items()):
                row = self._conn.execute("SELECT version FROM pack_schema WHERE pack=?", (owner,)).fetchone()
                rows.append((owner, int(row["version"]) if row else None, needed))
            return rows

    def writable(self) -> tuple[bool, str]:
        """Can this database take a write right now? An empty write transaction takes the write lock exactly as a real one does, and fails the same way on a read-only file,
        a full disk or a lock held by another process."""
        try:
            with self.transaction():
                pass
            return True, ""
        except sqlite3.Error as exc:
            return False, type(exc).__name__

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic block. Nested use joins the outer transaction."""
        with self._lock:
            outermost = self._depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outermost:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outermost:
                    self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False, sort_keys=True)


def loads(text: Optional[str]) -> Any:
    return None if text is None else json.loads(text)

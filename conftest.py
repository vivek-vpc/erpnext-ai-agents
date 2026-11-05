import copy
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from erp_agents.core.bootstrap import build_app
from erp_agents.stores import BACKENDS, build_core_stores
from erp_agents.core.config import DEFAULT_CONFIG
from erp_agents.core.common import Actor

from erp_agents.core.packs import PackRegistry

STOCK_NEEDS = list(PackRegistry.discover().manifests["stock_procurement"].erp_permissions)           # what the stock pack says the ERPNext service account must hold
OWNER = Actor("owner", frozenset({"admin", "approver"}), "test")
APPROVER = Actor("manager", frozenset({"approver"}), "test")
VIEWER = Actor("accountant", frozenset({"viewer"}), "test")


def storage_kind() -> str:
    """Which storage the whole suite runs on: `sqlite` (the default) or `memory` (`ERP_AGENTS_TEST_STORAGE=memory`). A test that restarts the application on a database file
    (it passes `db_path`) is about SQLite itself and keeps using it; so are the tests marked `sqlite_only`."""
    kind = os.environ.get("ERP_AGENTS_TEST_STORAGE", "sqlite")
    if kind not in BACKENDS:
        raise pytest.UsageError(f"ERP_AGENTS_TEST_STORAGE must be one of {', '.join(BACKENDS)}, not {kind!r}")
    return kind


def table_rows(app, table, where=None):
    """The rows of a table as dicts, read behind the application's back: what is really stored (tests only). Oldest first."""
    where = where or {}
    if app.db is None:
        return [dict(r) for r in app.stores.backend.rows(table) if all(r.get(k) == v for k, v in where.items())]
    sql = f"SELECT * FROM {table}" + (" WHERE " + " AND ".join(f"{k}=?" for k in where) if where else "")
    return [dict(r) for r in app.db.query(sql, tuple(where.values()))]


def put_row(app, table, row):
    """Add a row behind the application's back, as an older version or a person with access to the storage could have (tests only). Give every column."""
    if app.db is None:
        app.stores.backend.insert(table, dict(row))
    else:
        app.db.execute(f"INSERT INTO {table}({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))


def tamper(app, table, where, change):
    """Change stored rows behind the application's back; `change(row)` returns the new values (tests only: what a person with access to the storage could do)."""
    if app.db is None:
        for row in app.stores.backend.rows(table):
            if all(row.get(k) == v for k, v in where.items()):
                row.update(change(dict(row)))
        return
    clause = " AND ".join(f"{k}=?" for k in where)
    for row in app.db.query(f"SELECT rowid AS _rowid_, * FROM {table} WHERE {clause}", tuple(where.values())):
        new = change({k: row[k] for k in row.keys() if k != "_rowid_"})
        app.db.execute(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in new)} WHERE rowid=?", (*new.values(), row["_rowid_"]))


def remove_rows(app, table, where):
    """Delete stored rows behind the application's back (tests only)."""
    if app.db is None:
        rows = app.stores.backend.rows(table)
        rows[:] = [r for r in rows if not all(r.get(k) == v for k, v in where.items())]
        return
    app.db.execute(f"DELETE FROM {table} WHERE " + " AND ".join(f"{k}=?" for k in where), tuple(where.values()))


def pytest_collection_modifyitems(config, items):
    if storage_kind() != "sqlite":
        skip = pytest.mark.skip(reason="about SQLite itself: it ran with the SQLite suite")
        for item in items:
            if "sqlite_only" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(params=BACKENDS)
def stores(request):
    """One bundle of the core's stores per backend: the contract tests run on every backend, whatever the suite is set to."""
    bundle = build_core_stores(request.param)
    yield bundle
    bundle.close()


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeHTTP:
    """Stands in for requests.Session for Telegram and WhatsApp. Records every call."""

    def __init__(self):
        self.calls = []        # (url, json, headers)
        self.fail_next = []    # list of (substring, status, body) consumed in order
        self.updates = []      # queued Telegram updates for getUpdates
        self._n = 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        for i, (sub, status, body) in enumerate(self.fail_next):
            if sub in url:
                self.fail_next.pop(i)
                return FakeResponse(body, status)
        self._n += 1
        if "api.telegram.org" in url:
            if url.endswith("/getUpdates"):
                batch, self.updates = self.updates, []
                return FakeResponse({"ok": True, "result": batch})
            if url.endswith("/sendMessage"):
                return FakeResponse({"ok": True, "result": {"message_id": 1000 + self._n}})
            return FakeResponse({"ok": True, "result": True})
        return FakeResponse({"messages": [{"id": f"wamid.{self._n}"}]})

    def calls_to(self, method_or_sub):
        return [c for c in self.calls if method_or_sub in c[0]]


@pytest.fixture(autouse=True)
def _vendor_hash_key(monkeypatch):
    """The secret that keys the hash of a vendor's number (ERP_AGENTS_HASH_KEY, finding F-8): a fixed one for every test. The tests of what happens without it unset it themselves."""
    monkeypatch.setenv("ERP_AGENTS_HASH_KEY", "test-only-hash-key-0123456789abcdef-0123456789")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture(autouse=True)
def _fresh_circuit_breakers():
    """The circuit breakers are shared by every HTTP session in the process (core/resilience.py): each test starts with them all closed and leaves none open for the next."""
    from erp_agents.core import resilience
    resilience.configure(resilience.ResilienceSettings())
    yield
    resilience.configure(resilience.ResilienceSettings())


@pytest.fixture
def make_app(clock):
    def _make(erp=None, channels=None, cfg=None, http=None, db_path=":memory:"):
        base = copy.deepcopy(DEFAULT_CONFIG)
        base["database"]["path"] = db_path                              # a file, to start the application again on the same data
        base["conversation"] = {"synchronous": True}                    # messages typed to the assistant are handled on the spot, so tests need not wait for a worker thread
        base.setdefault("auth", {})["min_key_length"] = 1             # the tests use short access keys; the length rule has its own tests (test_hardening.py)
        base["auth"]["access_keys"] = "anywhere"                       # TestClient calls from a non-local address; the real default (local only) has its own tests (test_security_phase1.py)
        base.setdefault("system", {})["stock_alerts"] = False        # these tests exercise the request flow; stock alerts (sent before it) have their own tests
        for k, v in (cfg or {}).items():
            base[k] = {**base.get(k, {}), **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
        kind = storage_kind() if db_path == ":memory:" else "sqlite"
        app = build_app(config=base, clock=clock, erp=erp, channels=channels, http=http, db_path=db_path, stores=build_core_stores(kind, db_path) if kind != "sqlite" else None)
        console = app.channels.get("console")
        if console is not None:
            console.writer = lambda s: None
        return app
    return _make


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def http():
    return FakeHTTP()

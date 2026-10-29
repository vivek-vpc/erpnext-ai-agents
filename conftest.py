import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from erp_agents.core.bootstrap import build_app
from erp_agents.core.config import DEFAULT_CONFIG
from erp_agents.core.common import Actor

from erp_agents.core.packs import PackRegistry

STOCK_NEEDS = list(PackRegistry.discover().manifests["stock_procurement"].erp_permissions)           # what the stock pack says the ERPNext service account must hold
OWNER = Actor("owner", frozenset({"admin", "approver"}), "test")
APPROVER = Actor("manager", frozenset({"approver"}), "test")
VIEWER = Actor("accountant", frozenset({"viewer"}), "test")


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
    def _make(erp=None, channels=None, cfg=None, http=None):
        base = copy.deepcopy(DEFAULT_CONFIG)
        base["database"]["path"] = ":memory:"
        base["conversation"] = {"synchronous": True}                    # messages typed to the assistant are handled on the spot, so tests need not wait for a worker thread
        base.setdefault("auth", {})["min_key_length"] = 1             # the tests use short access keys; the length rule has its own tests (test_hardening.py)
        base["auth"]["access_keys"] = "anywhere"                       # TestClient calls from a non-local address; the real default (local only) has its own tests (test_security_phase1.py)
        base.setdefault("system", {})["stock_alerts"] = False        # these tests exercise the request flow; stock alerts (sent before it) have their own tests
        for k, v in (cfg or {}).items():
            base[k] = {**base.get(k, {}), **v} if isinstance(v, dict) and isinstance(base.get(k), dict) else v
        app = build_app(config=base, clock=clock, erp=erp, channels=channels, http=http, db_path=":memory:")
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

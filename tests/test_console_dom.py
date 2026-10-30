"""X0 to X7 of the penetration test of 19 Oct 2026, in CI: the real console runs in a DOM (Node and jsdom, tests/browser/) against the real application whose data is hostile, and nothing a payload
would leave behind is found in any page. See tests/browser/console_dom.js for what is checked and what jsdom cannot do (it does not run an <img onerror>, and it does not enforce the
Content-Security-Policy; tests/test_hardening.py checks the policy's header and the shape of the page it relies on).

Needs Node 22.22+ or 24.15+ and `npm ci` in tests/browser (jsdom 30.1.1, pinned by the lockfile). Without them the test is skipped, unless ERP_AGENTS_REQUIRE_BROWSER_TESTS is set (the CI sets it
on the Linux run that installs them): then a missing prerequisite is a failure, so the check cannot disappear quietly."""
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from erp_agents.core.api.app import create_app
from erp_agents.core.auth.erpnext import ErpNextLogin
from erp_agents.core.auth.sessions import LoginThrottle, SessionStore
from erp_agents.core.erp.mock import MockERP
from test_auth_erpnext import FakeErp

BROWSER = Path(__file__).resolve().parent / "browser"
SCRIPT = BROWSER / "console_dom.js"
PAYLOAD = '<img src=x onerror="window.__xss=(window.__xss||0)+1">'


def missing_prerequisite():
    if not shutil.which("node"):
        return "Node is not installed"
    if not (BROWSER / "node_modules" / "jsdom").is_dir():
        return "jsdom is not installed: run `npm ci` in tests/browser"
    return ""


@pytest.fixture
def prerequisites():
    missing = missing_prerequisite()
    if missing:
        if os.environ.get("ERP_AGENTS_REQUIRE_BROWSER_TESTS"):
            pytest.fail(f"the console DOM test is required here and cannot run: {missing}")
        pytest.skip(missing)


def hostile_api(make_app, clock):
    """The application with ERPNext sign-in on (a stand-in ERPNext) and hostile text in the item names, the vendor names and one person's full name."""
    erp = MockERP(today=clock().date())
    for i, code in enumerate(list(erp.items)[:6]):
        object.__setattr__(erp.items[code].info, "name", f"{PAYLOAD} item {i}")
    for offers in erp.vendors.values():
        for offer in offers:
            object.__setattr__(offer, "name", f"Vendor {PAYLOAD}")
    app = make_app(erp=erp, cfg={"approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "inbox"}]}]}})
    fake = FakeErp()
    fake.users["evil@acme.com"] = dict(pwd="pw-evil", full_name=PAYLOAD, roles=["Purchase Manager"], enabled=1, user_type="System User")
    fake.users["vivek@acme.com"] = dict(pwd="pw-vivek", full_name="Vivek C", roles=["System Manager", "Purchase Manager"], enabled=1, user_type="System User")
    app.login = ErpNextLogin("http://erp.local", session_factory=fake.session)
    app.sessions = SessionStore(app.login)
    app.throttle = LoginThrottle()
    app.inventory.run()
    app.procurement.run()
    return create_app(app)


@contextmanager
def serving(api):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    assert server.started, "the test server did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(15)


def tour(api):
    with serving(api) as url:
        done = subprocess.run(["node", str(SCRIPT), url], cwd=str(BROWSER), capture_output=True, text=True, timeout=300)
    assert done.stdout.strip(), f"the tour printed nothing: {done.stderr[-600:]}"
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_hostile_text_is_inert_on_every_page_of_the_console(prerequisites, make_app, clock):
    results = tour(hostile_api(make_app, clock))
    names = {r["name"][:2] for r in results}
    assert {"X0", "X1", "X2", "X3", "X4", "X5", "X6", "X7"} <= names, f"the tour stopped early: {[r['name'][:60] for r in results]}"
    failed = [f"{r['name']}: {r['detail']}" for r in results if not r["ok"]]
    assert not failed, "\n".join(failed)

"""Findings of the penetration test of 19 Oct 2026 (docs/PENTEST_REPORT.md), each pinned by a test so it cannot quietly come back."""
import base64
import hashlib
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import FakeHTTP
from erp_agents.core.api import hardening
from erp_agents.core.api.app import create_app
from erp_agents.core.approvals.channels.simple import InboxChannel
from erp_agents.core.approvals.channels.whatsapp import WhatsAppChannel
from erp_agents.core.auth.sessions import LoginThrottle, SessionStore

WEB = Path(hardening.__file__).resolve().parents[2] / "packs" / "stock_procurement" / "web" / "index.html"
STRONG = "k" * 40
LOCAL = dict(base_url="http://127.0.0.1:8100", client=("127.0.0.1", 50000))      # the person at the server machine


def hdr(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def demo(make_app):
    app = make_app(cfg={"auth": {"tokens": [{"token": STRONG, "user": "owner", "roles": ["viewer", "approver", "admin"]}]}})
    return app, TestClient(create_app(app))


# ------------------------------------------------------------------ nothing to read for a stranger
def test_the_api_documentation_is_not_published(demo):
    _, c = demo
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404, path


def test_the_documentation_can_be_turned_on_by_the_administrator(make_app):
    app = make_app(cfg={"server": {"docs": True}})
    assert TestClient(create_app(app)).get("/openapi.json").status_code == 200


def test_health_tells_a_stranger_only_that_the_service_is_alive_and_a_signed_in_person_more(demo):
    _, c = demo
    assert c.get("/health").json() == {"ok": True}
    assert c.get("/health", headers=hdr("wrong")).json() == {"ok": True}
    full = c.get("/health", headers=hdr(STRONG)).json()
    assert full["ok"] is True and "erp_reachable" in full and "channels" in full


def test_the_anonymous_health_check_does_not_touch_the_erp(make_app):
    app = make_app()
    calls = []
    app.erp.healthcheck = lambda: calls.append(1) or True
    c = TestClient(create_app(app))
    for _ in range(5):
        c.get("/health")
    assert calls == []


# ------------------------------------------------------------------ headers
def test_every_answer_carries_the_browser_protections(demo):
    _, c = demo
    for r in (c.get("/ui/"), c.get("/api/v1/settings/system", headers=hdr(STRONG)), c.get("/api/v1/settings/system"), c.get("/nothing-here")):
        h = {k.lower(): v for k, v in r.headers.items()}
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "no-referrer", r.url
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert "geolocation=()" in h["permissions-policy"] and h["cross-origin-opener-policy"] == "same-origin"
    assert "no-store" in c.get("/api/v1/settings/system", headers=hdr(STRONG)).headers["cache-control"]


def test_the_console_may_only_run_its_own_script_and_cannot_be_framed(demo):
    _, c = demo
    csp = c.get("/ui/").headers["content-security-policy"]
    script = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", WEB.read_text(encoding="utf-8"), re.S)[0]
    digest = "'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"
    assert f"script-src {digest}" in csp                                   # pinned to this exact script
    assert "script-src 'unsafe-inline'" not in csp and "unsafe-eval" not in csp
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp and "object-src 'none'" in csp


def test_the_console_page_keeps_the_shape_the_policy_relies_on():
    html = WEB.read_text(encoding="utf-8")
    assert len(re.findall(r"<script(?![^>]*\bsrc=)", html)) == 1, "the page must have exactly one inline script (the policy pins it by hash)"
    assert not re.search(r"<[^>]+\son[a-z]+\s*=", html, re.I), "inline event-handler attributes would be blocked by the policy"
    assert "javascript:" not in html.lower()
    assert not re.search(r"""(src|href)\s*=\s*["']https?://""", html, re.I), "the page loads nothing from other sites"
    assert hardening.script_hash(WEB) is not None


def test_https_through_a_proxy_gets_strict_transport_security(demo):
    _, c = demo
    assert "strict-transport-security" not in c.get("/ui/").headers
    assert "max-age" in c.get("/ui/", headers={"X-Forwarded-Proto": "https"}).headers["strict-transport-security"]


# ------------------------------------------------------------------ size limits
def test_a_huge_body_is_refused_before_it_is_read(demo):
    _, c = demo
    assert c.post("/webhooks/whatsapp", content=b"A" * 2_000_000).status_code == 413
    assert c.post("/api/v1/chat", content=b"A" * 300_000, headers={**hdr(STRONG), "content-type": "application/json"}).status_code == 413
    assert c.post("/api/v1/auth/login", content=b"A" * 20_000, headers={"content-type": "application/json"}).status_code == 413
    assert c.post("/api/v1/chat", json={"question": "hello"}, headers=hdr(STRONG)).status_code == 200            # real requests are far below the limit


def test_a_body_that_grows_without_declaring_its_size_is_cut_off(demo):
    _, c = demo

    def chunks():
        for _ in range(40):
            yield b"A" * 10_000
    r = c.post("/api/v1/chat", content=chunks(), headers={**hdr(STRONG), "content-type": "application/json"})
    assert r.status_code == 413


def test_an_honest_webhook_is_still_accepted(make_app):
    http = FakeHTTP()
    wa = WhatsAppChannel("T", "P", app_secret="s3cret", verify_token="v", http=http)
    app = make_app(channels={"inbox": InboxChannel(), "whatsapp": wa})
    c = TestClient(create_app(app))
    body = b'{"entry": []}'
    sig = "sha256=" + __import__("hmac").new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert c.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig}).status_code == 200
    assert c.post("/webhooks/whatsapp", content=body).status_code == 401


# ------------------------------------------------------------------ "is this the server machine?"
def req(headers, client="127.0.0.1"):
    return SimpleNamespace(client=SimpleNamespace(host=client), headers={k.lower(): v for k, v in headers.items()})


def test_only_a_loopback_connection_addressed_to_a_loopback_name_is_the_server_machine():
    assert hardening.is_local_request(req({"host": "127.0.0.1:8100"}))
    assert hardening.is_local_request(req({"host": "localhost:8100"}))
    assert hardening.is_local_request(req({"host": "[::1]:8100"}, "::1"))
    assert not hardening.is_local_request(req({"host": "abc-def.trycloudflare.com"}))          # a tunnel keeps the public name
    assert not hardening.is_local_request(req({"host": "127.0.0.1:8100"}, "203.0.113.5"))       # not from this machine
    assert not hardening.is_local_request(req({}))                                              # no Host at all


@pytest.mark.parametrize("name", ["X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "Via", "X-Real-IP", "CF-Connecting-IP", "CF-Ray", "CF-Visitor", "True-Client-IP",
                                  "X-Client-IP", "X-Originating-IP", "Fastly-Client-IP", "CDN-Loop", "X-Envoy-External-Address", "X-Amzn-Trace-Id", "X-Azure-ClientIP", "Ngrok-Trace-Id", "X-Arr-Log-Id"])
def test_any_sign_of_a_proxy_means_it_is_not_the_server_machine(name):
    assert not hardening.is_local_request(req({"host": "127.0.0.1:8100", name: "1"}))


def test_a_tunnel_that_only_adds_an_unlisted_style_header_cannot_use_the_access_key(make_app):
    """The old check looked for a short list of header names. A proxy that sent none of them (but kept its public Host name) passed as 'local'."""
    app = make_app(cfg={"auth": {"access_keys": "local", "tokens": [{"token": STRONG, "user": "owner", "roles": ["admin", "approver"]}]}})
    c = TestClient(create_app(app), client=("127.0.0.1", 50000), base_url="http://shop.example.trycloudflare.com")
    assert c.get("/api/v1/settings/system", headers=hdr(STRONG)).status_code == 401
    local = TestClient(create_app(app), **LOCAL)
    assert local.get("/api/v1/settings/system", headers=hdr(STRONG)).status_code == 200


# ------------------------------------------------------------------ weak and well-known keys
def test_the_well_known_demo_key_works_on_the_server_machine_and_nowhere_else(make_app):
    app = make_app(cfg={"auth": {"min_key_length": 24, "allow_demo_key": True, "tokens": [{"token": "demo-owner-token", "user": "owner", "roles": ["admin", "approver"]}]}})        # the flag is how a demo starts (tests/test_demo_key.py)
    local = TestClient(create_app(app), **LOCAL)
    assert local.get("/api/v1/settings/system", headers=hdr("demo-owner-token")).status_code == 200
    remote = TestClient(create_app(app))                                                          # any other machine, or the tunnel
    assert remote.get("/api/v1/settings/system", headers=hdr("demo-owner-token")).status_code == 401
    assert remote.put("/api/v1/settings/system", json={"set": {"max_alerts_per_run": 3}}, headers=hdr("demo-owner-token")).status_code == 401
    tunnel = TestClient(create_app(app), **LOCAL)
    assert tunnel.get("/api/v1/settings/system", headers={**hdr("demo-owner-token"), "Cf-Connecting-Ip": "203.0.113.7"}).status_code == 401


def test_a_short_key_is_refused_from_outside_but_a_long_random_one_is_not(make_app):
    app = make_app(cfg={"auth": {"min_key_length": 24, "tokens": [{"token": "short-key", "user": "a", "roles": ["viewer"]}, {"token": STRONG, "user": "b", "roles": ["viewer"]}]}})
    remote = TestClient(create_app(app))
    assert remote.get("/api/v1/settings/system", headers=hdr("short-key")).status_code == 401
    assert remote.get("/api/v1/settings/system", headers=hdr(STRONG)).status_code == 200
    assert TestClient(create_app(app), **LOCAL).get("/api/v1/settings/system", headers=hdr("short-key")).status_code == 200


def test_starting_with_a_weak_key_is_reported_in_the_log_without_printing_it(make_app, caplog):
    with caplog.at_level(logging.WARNING):
        make_app(cfg={"auth": {"min_key_length": 24, "allow_demo_key": True, "tokens": [{"token": "demo-owner-token", "user": "owner", "roles": ["admin"]}]}})
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "access key for owner is short or well known" in text and "demo-owner-token" not in text


# ------------------------------------------------------------------ sessions and sign-in guessing
def ident(user):
    return SimpleNamespace(user=user)


def test_one_account_cannot_push_everybody_else_out():
    store = SessionStore(provider=None, max_sessions=20, per_user_max=5)
    victim = store.create(object(), ident("admin@acme.com"))
    mine = [store.create(object(), ident("employee@acme.com")) for _ in range(60)]
    assert store.get(victim) is not None, "the administrator was signed out by somebody else opening sessions"
    assert store.count() <= 1 + 5
    assert store.get(mine[0]) is None and store.get(mine[-1]) is not None          # the employee only loses their own oldest sessions


def test_when_the_whole_table_is_full_the_biggest_holder_loses_a_session_first():
    store = SessionStore(provider=None, max_sessions=6, per_user_max=5)
    a = [store.create(object(), ident("a")) for _ in range(5)]
    b = store.create(object(), ident("b"))
    c = store.create(object(), ident("c"))                                          # table full: 'a' (five sessions) gives one up, not 'b'
    assert store.get(b) is not None and store.get(c) is not None and store.get(a[0]) is None


def test_the_sign_in_guess_counter_forgets_quiet_addresses():
    now = [1000.0]
    t = LoginThrottle(limit=3, window=60, clock=lambda: now[0])
    for i in range(200):
        t.fail(f"ip:10.0.{i // 250}.{i % 250}")
    assert len(t._fails) == 200
    now[0] += 120
    assert not t.blocked("ip:10.0.0.1")
    t.fail("ip:new")
    t.blocked(*[f"ip:10.0.0.{i}" for i in range(200)])
    assert len(t._fails) <= 1


def test_text_typed_into_the_sign_in_box_cannot_forge_log_lines(make_app, caplog):
    from test_auth_erpnext import FakeErp
    from erp_agents.core.auth.erpnext import ErpNextLogin
    from erp_agents.core.bootstrap import build_app
    app = make_app()
    app.login = ErpNextLogin("http://erp.local", session_factory=FakeErp().session)
    app.sessions, app.throttle = SessionStore(app.login), LoginThrottle()
    c = TestClient(create_app(app))
    with caplog.at_level(logging.WARNING):
        r = c.post("/api/v1/auth/login", json={"username": "x\nWARNING:forged:owner signed in\r\n", "password": "p"})
    assert r.status_code == 401
    lines = [rec.getMessage() for rec in caplog.records if "failed sign-in" in rec.getMessage()]
    assert lines and all("\n" not in l and "\r" not in l for l in lines)
    assert hardening.clean_for_log("a\x1b[31m\nb") == "a?[31m?b" and len(hardening.clean_for_log("z" * 500)) == 80


# ------------------------------------------------------------------ only the webhooks through a tunnel
@pytest.fixture
def webhook_only(make_app):
    http = FakeHTTP()
    wa = WhatsAppChannel("T", "P", app_secret="s3cret", verify_token="v", http=http)
    app = make_app(channels={"inbox": InboxChannel(), "whatsapp": wa},
                   cfg={"server": {"remote_access": "webhooks_only"}, "auth": {"tokens": [{"token": STRONG, "user": "owner", "roles": ["admin", "approver"]}]}})
    return app


def test_in_webhooks_only_mode_a_stranger_finds_no_console_no_sign_in_and_no_api(webhook_only):
    c = TestClient(create_app(webhook_only))                                                    # not the server machine
    for path in ("/ui/", "/", "/api/v1/auth/mode", "/api/v1/settings/system", "/docs"):
        assert c.get(path, headers=hdr(STRONG)).status_code == 404, path
    assert c.post("/api/v1/auth/login", json={"username": "a", "password": "b"}).status_code == 404
    # ... but Meta can still reach the webhook, and a liveness check still answers
    assert c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "v", "hub.challenge": "42"}).text == "42"
    assert c.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "bad", "hub.challenge": "42"}).status_code == 403
    assert c.get("/health").json() == {"ok": True}
    r = c.get("/ui/")
    assert r.headers["x-frame-options"] == "DENY"                                               # even a refusal carries the protections


def test_in_webhooks_only_mode_through_the_tunnel_from_this_machine_is_still_closed(webhook_only):
    c = TestClient(create_app(webhook_only), **LOCAL)
    assert c.get("/ui/", headers={"Cf-Connecting-Ip": "203.0.113.7"}).status_code == 404
    assert c.get("/api/v1/settings/system", headers={**hdr(STRONG), "X-Forwarded-For": "203.0.113.7"}).status_code == 404


def test_in_webhooks_only_mode_the_person_at_the_server_machine_still_has_the_console(webhook_only):
    c = TestClient(create_app(webhook_only), **LOCAL)
    assert c.get("/ui/").status_code == 200
    assert c.get("/api/v1/settings/system", headers=hdr(STRONG)).status_code == 200


def test_the_default_leaves_the_console_reachable_as_before(make_app):
    app = make_app(cfg={"auth": {"tokens": [{"token": STRONG, "user": "owner", "roles": ["admin"]}]}})
    c = TestClient(create_app(app))
    assert c.get("/ui/").status_code == 200 and c.get("/api/v1/settings/system", headers=hdr(STRONG)).status_code == 200


# ------------------------------------------------------------------ the service account's roles (the app's own least-privilege check)
class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class _LeanAccountSession:
    """An ordinary ERPNext account: its own User record comes back without the Roles table, but Has Role can be listed."""

    def __init__(self, roles, perms):
        self.roles, self.perms, self.calls = roles, perms, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if url.endswith("get_logged_user"):
            return _Resp({"message": "assistant@acme.com"})
        if "/api/resource/User/" in url:
            return _Resp({"data": {"name": "assistant@acme.com"}})                 # no "roles" key
        if url.endswith("frappe.client.get_list"):
            assert params["doctype"] == "Has Role" and "assistant@acme.com" in params["filters"]
            return _Resp({"message": [{"role": r} for r in self.roles]})
        if url.endswith("getdoctype"):
            return _Resp({"docs": [{"permissions": self.perms.get(params["doctype"], [])}]})
        raise AssertionError(url)


def test_the_access_check_works_for_a_lean_service_account_that_cannot_read_its_own_roles():
    from conftest import STOCK_NEEDS as NEEDS
    from erp_agents.core.auth.access import check_service_account
    everything = {dt: [{"role": "Stock Assistant", "permlevel": 0, flag: 1} for d, flag in NEEDS if d == dt] for dt, _ in NEEDS}
    merged = {dt: [{"role": "Stock Assistant", "permlevel": 0, "read": 1, "create": 1}] for dt, _ in NEEDS}
    rep = check_service_account(_LeanAccountSession(["Stock Assistant"], merged), "http://erp.local", NEEDS)
    assert rep.user == "assistant@acme.com" and rep.roles == ["Stock Assistant"] and rep.missing == [] and rep.surplus == []
    rep = check_service_account(_LeanAccountSession(["Stock Assistant", "System Manager", "HR Manager"], merged), "http://erp.local", NEEDS)
    assert rep.surplus == ["HR Manager", "System Manager"] and rep.risky_held == ["HR Manager", "System Manager"]
    rep = check_service_account(_LeanAccountSession(["Nothing Useful"], merged), "http://erp.local", NEEDS)
    assert rep.missing, "an account without the needed permissions is reported as missing them"

"""Metrics (core/metrics.py, GET /metrics): off by default and absent when off; when on, protected, valid Prometheus text, the series the brief asks for, and nothing personal."""
import json
import re
from datetime import datetime, timezone

import pytest
import requests
from fastapi.testclient import TestClient
from requests.adapters import BaseAdapter

from conftest import OWNER, VIEWER
from erp_agents.core import metrics, obs
from erp_agents.core.agents.llm import OllamaToolClient, Reply
from erp_agents.core.api.app import create_app
from erp_agents.core.assistant.llm import LLMError
from erp_agents.core.erp.erpnext import ERPNextClient
from erp_agents.core.erp.mock import MockERP

KEYS = {"owner": "owner-key-0123456789abcdefghij", "viewer": "viewer-key-0123456789abcdefghij"}
SCRAPER = "scraper-token-0123456789abcdefghij"
AUTH = {"auth": {"tokens": [{"user": "owner", "token": KEYS["owner"], "roles": ["admin", "approver"]}, {"user": "viewer", "token": KEYS["viewer"], "roles": ["viewer"]}]}}


@pytest.fixture(autouse=True)
def clean_registry():
    metrics.REGISTRY.reset()
    yield
    metrics.configure(False)
    metrics.REGISTRY.reset()
    metrics.REGISTRY.set_collector("database", None)


def app_with(make_app, clock, on=True, **kw):
    cfg = {**AUTH, "observability": {"metrics": on}}
    return make_app(erp=kw.pop("erp", None) or MockERP(today=clock().date()), cfg=cfg, **kw)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ the text format
SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(?:[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*"(?:,|(?=\})))*\})? (-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)$')


def parse(text):
    """{(name, frozenset(labels)): value} and the TYPE of each metric; fails on a line that is not valid exposition text, a duplicate series or a sample with no TYPE line."""
    types, samples = {}, {}
    for line in text.splitlines():
        if line.startswith("# HELP "):
            continue
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ")
            assert name not in types, f"{name} declared twice"
            types[name] = kind
            continue
        m = SAMPLE.match(line)
        assert m, f"not a valid sample line: {line!r}"
        name = m.group(1)
        base = re.sub(r"_(bucket|sum|count)$", "", name) if re.sub(r"_(bucket|sum|count)$", "", name) in types else name
        assert base in types, f"{name} has no TYPE line"
        labels = frozenset(re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', m.group(2) or ""))
        assert (name, labels) not in samples, f"duplicate series {line!r}"
        samples[(name, labels)] = float(m.group(3))
    return types, samples


def value(samples, name, **labels):
    return samples.get((name, frozenset((k, str(v)) for k, v in labels.items())))


# ------------------------------------------------------------------ off by default
def test_by_default_nothing_is_recorded_and_the_endpoint_does_not_exist(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()), cfg=AUTH)
    assert app.observability.metrics is False and metrics.REGISTRY.enabled is False and metrics._http_seen not in obs.HTTP_HOOKS
    client = TestClient(create_app(app))
    for headers in ({}, bearer(KEYS["owner"])):
        assert client.get("/metrics", headers=headers).status_code == 404
    app.toolkit.call("stock_status", {"item_code": "BOLT-M8"}, VIEWER)
    metrics.TOOL_CALLS.inc(tool="x", via="y", outcome="ok")
    assert metrics.TOOL_CALLS.values == {} and metrics.REGISTRY.render().count("erp_agents_tool_calls_total{") == 0


def test_switching_it_on_and_off_again_follows_the_settings(make_app, clock):
    on = app_with(make_app, clock, True)
    assert metrics.REGISTRY.enabled and metrics._http_seen in obs.HTTP_HOOKS
    off = app_with(make_app, clock, False)
    assert not metrics.REGISTRY.enabled and metrics._http_seen not in obs.HTTP_HOOKS and on is not off


# ------------------------------------------------------------------ who may read it
def test_the_endpoint_wants_the_scrapers_token_or_an_administrator(make_app, clock, monkeypatch):
    app = app_with(make_app, clock)
    client = TestClient(create_app(app))
    monkeypatch.setenv(metrics.SCRAPER_ENV, SCRAPER)
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers=bearer("wrong-token-0123456789abcdefghij")).status_code == 401
    assert client.get("/metrics", headers=bearer(KEYS["viewer"])).status_code == 403
    for good in (SCRAPER, KEYS["owner"]):
        r = client.get("/metrics", headers=bearer(good))
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain; version=0.0.4")
    monkeypatch.setenv(metrics.SCRAPER_ENV, "short")                          # a short scraper token is not accepted as a key to anything
    assert client.get("/metrics", headers=bearer("short")).status_code == 401
    monkeypatch.delenv(metrics.SCRAPER_ENV)
    assert client.get("/metrics", headers=bearer(SCRAPER)).status_code == 401


def test_the_endpoint_is_hidden_through_a_tunnel_when_only_the_webhooks_are_open(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()), cfg={**AUTH, "observability": {"metrics": True}, "server": {"remote_access": "webhooks_only"}})
    client = TestClient(create_app(app))
    assert client.get("/metrics", headers={**bearer(KEYS["owner"]), "x-forwarded-for": "203.0.113.9"}).status_code == 404


# ------------------------------------------------------------------ valid text, bounded labels
def test_the_exposition_is_valid_and_histograms_add_up(make_app, clock):
    app = app_with(make_app, clock)
    for _ in range(3):
        app.toolkit.call("stock_status", {"item_code": "BOLT-M8"}, VIEWER)
    types, samples = parse(TestClient(create_app(app)).get("/metrics", headers=bearer(KEYS["owner"])).text)
    assert types["erp_agents_tool_calls_total"] == "counter" and types["erp_agents_tool_call_seconds"] == "histogram" and types["erp_agents_approvals_pending"] == "gauge"
    assert value(samples, "erp_agents_tool_calls_total", tool="stock_status", via="api", outcome="ok") == 3
    buckets = sorted(((float(dict(k[1])["le"]) if dict(k[1])["le"] != "+Inf" else float("inf")), v) for k, v in samples.items() if k[0] == "erp_agents_tool_call_seconds_bucket")
    assert [b[1] for b in buckets] == sorted(b[1] for b in buckets) and buckets[-1][1] == value(samples, "erp_agents_tool_call_seconds_count", tool="stock_status") == 3


def test_labels_are_escaped_scrubbed_and_bounded(make_app, clock):
    app_with(make_app, clock)
    metrics.TOOL_CALLS.inc(tool='odd "name"\nwith a break', via="api", outcome="ok")
    obs.register_secret("label-secret-0123456789")
    metrics.TOOL_CALLS.inc(tool="label-secret-0123456789", via="api", outcome="ok")
    text = metrics.REGISTRY.render()
    assert 'tool="odd \\"name\\" with a break"' in text and "label-secret-0123456789" not in text and 'tool="[hidden]"' in text           # quotes escaped, a line break becomes a space
    parse(text)
    for i in range(metrics.MAX_SERIES + 40):
        metrics.TOOL_CALLS.inc(tool=f"t{i}", via="api", outcome="ok")
    assert len(metrics.TOOL_CALLS.values) == metrics.MAX_SERIES
    assert value(parse(metrics.REGISTRY.render())[1], "erp_agents_metric_series_dropped_total") >= 40


# ------------------------------------------------------------------ the series
class Model:
    model, remote, provider, hosted = "scripted", False, "ollama", False

    def __init__(self, *replies):
        self.replies = list(replies)

    def set_tools(self, tools):
        pass

    def chat(self, messages, tools=None, schema=None):
        r = self.replies.pop(0) if self.replies else Reply(content="{}")
        if isinstance(r, Exception):
            raise r
        return r


def answer(text, tin=100, tout=10):
    return Reply(content=json.dumps({"answerable": True, "answer": text}), prompt_tokens=tin, output_tokens=tout)


def scrape(app):
    return parse(TestClient(create_app(app)).get("/metrics", headers=bearer(KEYS["owner"])).text)[1]


def test_agent_tasks_tokens_grounding_rejects_repairs_and_fallbacks_are_counted(make_app, clock):
    pytest.importorskip("mcp")
    app = app_with(make_app, clock)
    app.settings.update_section(OWNER, "agents", {"enabled": True})
    done = Reply(content="done", prompt_tokens=50, output_tokens=2)
    tool = Reply(tool_calls=[("stock_status", {"item_code": "BOLT-M8"})], prompt_tokens=60, output_tokens=5)
    good, invented = answer("BOLT-M8 has 300 pcs on hand."), answer("BOLT-M8 has 4,242 pcs on hand.")
    for replies in ((tool, done, good),                        # accepted at once
                    (tool, done, invented, good),              # rejected, repaired
                    (tool, done, invented, invented),          # rejected twice: the rules' answer
                    (LLMError("could not reach the model"),)):  # the model is down
        app.agents.client_factory = lambda cfg, r=replies: Model(*r)
        app.agents.ask("How much BOLT-M8 do we have?", VIEWER)
    s = scrape(app)
    assert value(s, "erp_agents_agent_tasks_total", agent="inventory", task="ask", status="ok") == 2
    assert value(s, "erp_agents_agent_tasks_total", agent="inventory", task="ask", status="fallback") == 2
    assert value(s, "erp_agents_grounding_rejects_total", agent="inventory", task="ask") == 3          # one in the second task, two in the third
    assert value(s, "erp_agents_grounding_repairs_total", agent="inventory", task="ask") == 2
    assert value(s, "erp_agents_agent_fallbacks_total", agent="inventory", task="ask", reason="rejected") == 1
    assert value(s, "erp_agents_agent_fallbacks_total", agent="inventory", task="ask", reason="unavailable") == 1
    for kind, outcome, n in (("answer", "ok", 3), ("repair", "ok", 2), ("gather", "ok", 6), ("gather", "error", 1)):
        assert value(s, "erp_agents_llm_requests_total", provider="ollama", model="scripted", kind=kind, outcome=outcome) == n, (kind, outcome)
    for direction, total in (("in", 830), ("out", 71)):
        assert value(s, "erp_agents_llm_tokens_used_total", provider="ollama", model="scripted", direction=direction) == total          # counted as the replies came
        assert value(s, "erp_agents_llm_tokens_total", provider="ollama", model="scripted", direction=direction) == total                # and read back from the database
    assert value(s, "erp_agents_tool_calls_total", tool="stock_status", via="agent", outcome="ok") == 3


def test_approvals_waiting_how_long_and_how_they_ended(make_app, clock):
    app = app_with(make_app, clock)
    assert value(scrape(app), "erp_agents_approvals_oldest_pending_age_seconds") == 0 and value(scrape(app), "erp_agents_approvals_pending") == 0
    app.inventory.run()
    waiting = value(scrape(app), "erp_agents_approvals_pending")
    assert waiting >= 3
    clock.advance(hours=5)
    s = scrape(app)
    assert value(s, "erp_agents_approvals_oldest_pending_age_seconds") >= 5 * 3600
    first = app.approvals.list("pending", 50)[0]
    app.approvals.decide(first.id, "dismiss", OWNER, "app")
    app.approvals.decide(app.approvals.list("pending", 50)[0].id, "approve", OWNER, "app")
    s = scrape(app)
    assert value(s, "erp_agents_approvals_pending") == waiting - 2
    assert value(s, "erp_agents_decisions_total", kind=first.kind, outcome="dismiss") == 1
    assert sum(v for (n, l), v in s.items() if n == "erp_agents_decisions_total") == 2
    assert value(s, "erp_agents_deliveries_total", channel="console", status="sent") >= 1
    assert value(s, "erp_agents_build_info", version=metrics.version()) == 1


# ------------------------------------------------------------------ outgoing requests
class Canned(BaseAdapter):
    def __init__(self, status=200, body=b'{"data": []}', fail=None, date="Wed, 28 Oct 2026 10:00:00 GMT"):
        super().__init__()
        self.status, self.body, self.fail, self.date = status, body, fail, date

    def send(self, request, **kwargs):
        if self.fail:
            raise self.fail
        r = requests.Response()
        r.status_code, r._content, r.url, r.request = self.status, self.body, request.url, request
        if self.date:
            r.headers["Date"] = self.date
        return r

    def close(self):
        pass


def test_erpnext_and_model_requests_are_counted_timed_and_the_clock_skew_is_read(make_app, clock):
    app_with(make_app, clock)
    erp = ERPNextClient("https://erp.example.test", "k" * 12, "s" * 12)
    erp.session.mount("https://", Canned())
    erp._list("Item", ["name"], [])
    erp.session.mount("https://", Canned(status=500, body=b"{}"))
    with pytest.raises(Exception):
        erp._list("Item", ["name"], [])
    erp.session.mount("https://", Canned(fail=requests.ConnectTimeout("slow")))
    assert erp.healthcheck() is False
    ollama = OllamaToolClient("http://127.0.0.1:11434", "m")
    ollama.session.mount("http://", Canned(body=json.dumps({"message": {"content": "hi"}}).encode(), date=None))
    ollama.chat([{"role": "user", "content": "q"}])
    s = parse(metrics.REGISTRY.render())[1]
    assert value(s, "erp_agents_http_requests_total", target="erpnext", kind="resource:Item", outcome="2xx") == 1
    assert value(s, "erp_agents_http_requests_total", target="erpnext", kind="resource:Item", outcome="5xx") == 1
    assert value(s, "erp_agents_http_requests_total", target="erpnext", kind="method:frappe.auth.get_logged_user", outcome="error") == 1
    assert value(s, "erp_agents_http_requests_total", target="llm", kind="/api/chat", outcome="2xx") == 1
    assert value(s, "erp_agents_http_request_seconds_count", target="erpnext") == 3
    expected = (datetime.now(timezone.utc) - datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)).total_seconds()
    assert abs(value(s, "erp_agents_erpnext_clock_skew_seconds") - expected) < 60


# ------------------------------------------------------------------ nothing personal
def test_nothing_personal_is_a_label_or_a_value(make_app, clock):
    pytest.importorskip("mcp")
    app = app_with(make_app, clock)
    app.settings.update_section(OWNER, "agents", {"enabled": True})
    app.inventory.run()
    app.agents.client_factory = lambda cfg: Model(Reply(tool_calls=[("vendor_options", {"item_code": "BOLT-M8", "qty": 500})]), Reply(content="done"),
                                                 answer("Prime Supplies is the best vendor for BOLT-M8."))
    app.agents.ask("Which vendor is best for BOLT-M8 for 500, asked by the owner?", OWNER)
    app.approvals.decide(app.approvals.list("pending", 50)[0].id, "dismiss", OWNER, "app")
    text = TestClient(create_app(app)).get("/metrics", headers=bearer(KEYS["owner"])).text
    samples = "\n".join(l for l in text.splitlines() if not l.startswith("#"))                    # the series themselves, not the help text
    assert samples.count("\n") > 20
    for private in ("BOLT-M8", "Prime Supplies", "owner", "asked by", KEYS["owner"], KEYS["viewer"]):
        assert private not in samples, private
    series = parse(text)[1]                                                                       # the quantity asked about (500) as a number and in a label, not as digits anywhere in
    assert 500.0 not in series.values() and not [v for labels in (k[1] for k in series) for _, v in labels if "500" in v]   # the text: a latency sum can contain them by chance

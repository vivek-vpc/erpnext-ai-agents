"""One agent task can be followed by its id through every log line it causes: the task, the tool calls, the model calls and the ERPNext requests, in the plain and the JSON
format, in the thread that asked and in the background thread that `submit` starts; an HTTP request has its own id, returned in X-Request-ID (Phase 2 of the hardening brief)."""
import io
import json
import logging
import time
from contextlib import contextmanager

import anyio
import pytest
import requests
from fastapi.testclient import TestClient
from requests.adapters import BaseAdapter

pytest.importorskip("mcp", reason="the optional 'agents' extra is not installed")

from conftest import OWNER, VIEWER, APPROVER
from erp_agents.core import obs
from erp_agents.core.agents.llm import Reply
from erp_agents.core.api.app import create_app
from erp_agents.core.erp.erpnext import ERPNextClient
from erp_agents.core.erp.mock import MockERP


class ScriptedModel:
    """Asks for one tool, then answers from what it returned."""
    model, remote, provider, hosted = "scripted", False, "ollama", False

    def __init__(self):
        self.replies = [Reply(tool_calls=[("stock_status", {"item_code": "BOLT-M8"})], prompt_tokens=80, output_tokens=10),
                        Reply(content="done", prompt_tokens=90, output_tokens=2),
                        Reply(content=json.dumps({"answerable": True, "answer": "BOLT-M8 has 300 pcs on hand."}), prompt_tokens=120, output_tokens=12)]

    def set_tools(self, tools):
        pass

    def chat(self, messages, tools=None, schema=None):
        return self.replies.pop(0) if self.replies else Reply(content="{}")


@contextmanager
def logged(fmt):
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    buf = io.StringIO()
    root.handlers, root.level = [obs.make_handler(buf, fmt)], logging.INFO
    try:
        yield buf
    finally:
        root.handlers, root.level = saved, level


@pytest.fixture
def world(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    app.settings.update_section(OWNER, "agents", {"enabled": True})
    app.agents.client_factory = lambda cfg: ScriptedModel()
    return app


def requests_logged(buf):
    """The one-line records of outgoing requests (other libraries may log lines of their own at DEBUG)."""
    return [l for l in map(json.loads, buf.getvalue().strip().splitlines()) if str(l.get("event", "")).endswith("_request")]


# ------------------------------------------------------------------ the whole task, by its id
def test_every_line_a_task_causes_carries_its_id_and_no_other_line_does(world):
    with logged("json") as buf:
        first = world.agents.ask("How much BOLT-M8 do we have?", VIEWER)
        logging.getLogger("erp_agents.test").info("a line after the task")
        world.agents.client_factory = lambda cfg: ScriptedModel()
        second = world.agents.ask("How much BOLT-M8 do we have?", VIEWER)
    lines = [json.loads(l) for l in buf.getvalue().strip().splitlines()]
    assert first["status"] == "answered" and first["task_id"] != second["task_id"]
    mine = [l for l in lines if l.get("task_id") == first["task_id"]]
    assert [l["event"] for l in mine if "event" in l] == ["agent_task_start", "llm_call", "tool_call", "llm_call", "llm_call", "agent_task"]
    kinds = [l["kind"] for l in mine if l.get("event") == "llm_call"]
    assert kinds == ["gather", "gather", "answer"]
    tool = next(l for l in mine if l.get("event") == "tool_call")
    assert (tool["tool"], tool["ok"], tool["via"]) == ("stock_status", True, "agent") and isinstance(tool["ms"], int)
    llm = next(l for l in mine if l.get("event") == "llm_call")
    assert (llm["provider"], llm["model"], llm["outcome"], llm["tokens_in"], llm["tokens_out"]) == ("ollama", "scripted", "ok", 80, 10)
    end = mine[-1]
    assert (end["event"], end["status"], end["source"], end["steps"], end["tool_calls"], end["tokens_in"]) == ("agent_task", "ok", "model", 3, 1, 290)
    assert not [l for l in lines if l.get("event") in ("tool_call", "llm_call", "agent_task") and l.get("task_id") not in (first["task_id"], second["task_id"])]
    assert not [l for l in mine if l.get("task_id") != first["task_id"]]
    later = next(l for l in lines if l["msg"] == "a line after the task")
    assert "task_id" not in later                                      # the id is unbound when the task ends


def test_the_plain_format_shows_the_id_on_each_line_of_the_task(world):
    with logged("text") as buf:
        res = world.agents.ask("How much BOLT-M8 do we have?", VIEWER)
    task_lines = [l for l in buf.getvalue().splitlines() if "erp_agents.calls" in l]
    assert len(task_lines) == 6 and all(f"[task={res['task_id']}]" in l for l in task_lines)
    assert any("tool_call tool=stock_status via=agent ok=True" in l for l in task_lines)


def test_a_task_started_in_the_background_carries_its_own_id_and_the_requests_id(world):
    plan = world.agents.ask_plan("How much BOLT-M8 do we have?", VIEWER)
    with logged("json") as buf:
        with obs.bind(request_id="req-from-the-browser"):
            task_id = world.agents.submit(plan)
        for _ in range(100):
            if (world.agents.task(task_id) or {}).get("status") not in (None, "running"):
                break
            time.sleep(0.1)
    mine = [l for l in map(json.loads, buf.getvalue().strip().splitlines()) if l.get("task_id") == task_id]
    assert [l["event"] for l in mine if "event" in l][0] == "agent_task_start" and [l["event"] for l in mine if "event" in l][-1] == "agent_task"
    assert {l.get("request_id") for l in mine} == {"req-from-the-browser"}               # a new thread keeps the ids that were bound when it was started


def test_a_model_that_cannot_be_reached_is_one_warning_with_the_class_of_the_failure_not_its_text(world):
    from erp_agents.core.assistant.llm import LLMError

    class Down(ScriptedModel):
        def chat(self, messages, tools=None, schema=None):
            raise LLMError("could not reach the model at http://127.0.0.1:11434 (secret detail)")
    world.agents.client_factory = lambda cfg: Down()
    with logged("json") as buf:
        res = world.agents.ask("How much BOLT-M8 do we have?", VIEWER)
    lines = [json.loads(l) for l in buf.getvalue().strip().splitlines()]
    call = next(l for l in lines if l.get("event") == "llm_call")
    assert (call["outcome"], call["error"], call["level"], call["task_id"]) == ("error", "LLMError", "WARNING", res["task_id"]) and "secret detail" not in buf.getvalue()
    assert next(l for l in lines if l.get("event") == "agent_task")["status"] == "fallback"


def test_a_tool_call_that_is_refused_says_why_in_a_word_and_never_quotes_what_the_caller_sent(world):
    with logged("json") as buf:
        res = world.toolkit.call("prepare_order_request", {"item_code": "BOLT-M8", "qty": 500, "reason": "private words"}, VIEWER, via="api")
    assert "error" in res
    (line,) = [json.loads(l) for l in buf.getvalue().strip().splitlines() if '"tool_call"' in l]
    assert (line["tool"], line["ok"], line["refused"], line["level"]) == ("prepare_order_request", False, "not_allowed", "WARNING")
    assert "BOLT-M8" not in buf.getvalue() and "private words" not in buf.getvalue()
    with logged("json") as buf:
        world.toolkit.call("no_such_tool", {"x": "typed by a stranger"}, APPROVER)
    (line,) = [json.loads(l) for l in buf.getvalue().strip().splitlines() if '"tool_call"' in l]
    assert (line["tool"], line["refused"]) == ("unknown", "unknown_tool") and "typed by a stranger" not in buf.getvalue()


# ------------------------------------------------------------------ the ERPNext requests
class Canned(BaseAdapter):
    """Stands in for the network: answers every request with a canned response (or fails it)."""

    def __init__(self, status=200, body=b'{"data": []}', fail=None):
        super().__init__()
        self.status, self.body, self.fail, self.seen = status, body, fail, []

    def send(self, request, **kwargs):
        self.seen.append(request.url)
        if self.fail:
            raise self.fail
        r = requests.Response()
        r.status_code, r._content, r.url, r.request = self.status, self.body, request.url, request
        r.headers["Date"] = "Wed, 28 Oct 2026 10:00:00 GMT"
        return r

    def close(self):
        pass


def erp_with(adapter):
    erp = ERPNextClient("https://erp.example.test", "ERPKEY-sentinel", "ERPSECRET-sentinel")
    erp.session.mount("https://", adapter)
    return erp


def test_an_erpnext_request_inside_a_task_is_one_line_with_the_task_id_and_nothing_secret():
    adapter = Canned()
    erp = erp_with(adapter)

    async def inside_a_task():
        with obs.bind(task_id="task-0001"):
            await anyio.to_thread.run_sync(lambda: erp._list("Item", ["name"], [["item_code", "=", "BOLT-M8"]]))
    with logged("json") as buf:
        anyio.run(inside_a_task)
    lines = [l for l in map(json.loads, buf.getvalue().strip().splitlines()) if l.get("event") == "erpnext_request"]          # (another library may log a DEBUG line of its own)
    assert len(lines) == 1, lines
    (line,) = lines
    assert (line["event"], line["method"], line["kind"], line["status"], line["task_id"], line["level"]) == ("erpnext_request", "GET", "resource:Item", 200, "task-0001", "INFO")
    assert isinstance(line["ms"], int)
    text = buf.getvalue()
    assert "BOLT-M8" not in text and "ERPKEY" not in text and "ERPSECRET" not in text and "filters" not in text           # the query string is never written
    assert adapter.seen and "filters" in adapter.seen[0]                                                                  # it was sent, though


def test_a_failing_erpnext_request_is_a_warning_and_a_timeout_names_its_class():
    erp = erp_with(Canned(status=500, body=b'{"exc": "boom"}'))
    with logged("json") as buf:
        with pytest.raises(Exception):
            erp._list("Item", ["name"], [])
    assert requests_logged(buf)[0]["level"] == "WARNING" and requests_logged(buf)[0]["status"] == 500

    obs.register_secret("ERPKEY-sentinel")                                   # the application registers its configured keys at start-up (bootstrap)
    erp = erp_with(Canned(fail=requests.ConnectTimeout("timed out talking to erp.example.test with ERPKEY-sentinel")))
    with logged("json") as buf:
        assert erp.healthcheck() is False
    first = requests_logged(buf)[0]
    assert (first["event"], first["error"], first["kind"], first["level"]) == ("erpnext_request", "ConnectTimeout", "method:frappe.auth.get_logged_user", "WARNING")
    assert "ERPKEY-sentinel" not in buf.getvalue() and "[hidden]" in buf.getvalue()           # the healthcheck's own warning quotes the error: the scrubber hides the key in it


def test_a_session_is_observed_once_however_often_it_is_passed_in():
    s = requests.Session()
    assert obs.observe_session(obs.observe_session(s, "llm"), "llm") is s
    s.mount("https://", Canned())
    with logged("json") as buf:
        s.get("https://x.example.test/v1/messages?key=abcdefgh12345")
    assert len(requests_logged(buf)) == 1 and "abcdefgh12345" not in buf.getvalue()
    assert obs.observe_session(object(), "llm") is not None                              # a test double without request() is left alone


# ------------------------------------------------------------------ the HTTP request
def served(world):
    api = create_app(world)

    @api.get("/_who")
    def who():
        logging.getLogger("erp_agents.test").warning("inside a route")
        return obs.current_ids()
    return TestClient(api)


def test_a_request_id_from_the_caller_is_bound_for_the_route_and_returned(world):
    client = served(world)
    with logged("json") as buf:
        r = client.get("/_who", headers={"X-Request-ID": "caller-id-0001"})
    assert r.json() == {"request_id": "caller-id-0001"} and r.headers["x-request-id"] == "caller-id-0001"
    line = next(json.loads(l) for l in buf.getvalue().strip().splitlines() if "inside a route" in l)
    assert line["request_id"] == "caller-id-0001"


def test_without_one_a_request_gets_an_id_and_a_harmful_one_is_replaced(world):
    client = served(world)
    made = client.get("/_who").headers["x-request-id"]
    assert len(made) == 12
    for bad in ("short", "has a space inside", "line\r\nbreak-0001", "x" * 200):
        try:
            r = client.get("/_who", headers={"X-Request-ID": bad})
        except Exception:                                                   # a header that the HTTP client itself refuses never reaches the server
            continue
        assert r.headers["x-request-id"] != bad and len(r.headers["x-request-id"]) == 12


def test_a_refusal_carries_the_id_too(world):
    client = served(world)
    for r in (client.get("/api/v1/approvals"), client.get("/no/such/route")):
        assert r.status_code in (401, 404) and len(r.headers["x-request-id"]) == 12

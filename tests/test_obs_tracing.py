"""Optional OpenTelemetry tracing (core/tracing.py): off by default and then not even imported; on, one agent task is one trace (task > model calls and tool calls > ERPNext requests)
that carries names and counts and nothing private, a failure is the class of the exception and never its text, and a missing package stops the start with the command to fix it
(Phase 2 of the hardening brief)."""
import dataclasses
import json
import subprocess
import sys

import pytest

pytest.importorskip("mcp", reason="the optional 'agents' extra is not installed")
pytest.importorskip("opentelemetry.sdk", reason='the optional "otel" extra is not installed')

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from conftest import OWNER, VIEWER
from erp_agents.core import obs, tracing
from erp_agents.core.assistant.llm import LLMError
from erp_agents.core.bootstrap import StartupRefused
from erp_agents.core.erp.mock import MockERP
from test_obs_task_follow import Canned, ScriptedModel, erp_with

ALLOWED = {"agent", "task", "task_id", "status", "source", "provider", "model", "kind", "tokens_in", "tokens_out", "outcome", "tool", "via", "ok", "refused", "method",
           "error.type"}


@pytest.fixture(autouse=True)
def tracing_off_afterwards():
    yield
    tracing.configure(False)


@pytest.fixture
def exporter():
    exp = InMemorySpanExporter()
    tracing.configure(True, exporter=exp)
    return exp


@pytest.fixture
def world(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    app.settings.update_section(OWNER, "agents", {"enabled": True})
    app.agents.client_factory = lambda cfg: ScriptedModel()
    return app


def through_erpnext(world, adapter):
    """The stock_status tool, but one that asks ERPNext (a canned one) for the item, as the real one does."""
    erp = erp_with(adapter)
    entry = world.toolkit._tools["stock_status"]

    def handler(actor, **args):
        erp._list("Item", ["name"], [["item_code", "=", args["item_code"]]])
        return {"item_code": args["item_code"], "on_hand": 300}
    world.toolkit._tools["stock_status"] = dataclasses.replace(entry, handler=handler)


def by_name(spans):
    out = {}
    for s in spans:
        out.setdefault(s.name, []).append(s)
    return out


def everything_in(spans):
    return json.dumps([{"name": s.name, "attributes": dict(s.attributes or {}), "events": [e.name for e in s.events], "status": [s.status.status_code.name, s.status.description],
                        "resource": dict(s.resource.attributes)} for s in spans], default=str)


# ------------------------------------------------------------------ off, by default
def test_tracing_is_off_by_default_and_the_application_does_not_even_load_the_sdk():
    code = ("import sys\n"
            "import erp_agents.core.cli, erp_agents.core.bootstrap, erp_agents.core.toolkit, erp_agents.core.agents.service, erp_agents.core.obs\n"
            "from erp_agents.core import tracing\n"
            "assert not tracing.enabled()\n"
            "with tracing.span('x', a=1) as s:\n"
            "    s.set(b=2)\n"
            "    s.fail('Boom')\n"
            "tracing.annotate(c=3)\n"
            "tracing.mark_failed('Boom')\n"
            "tracing.configure(False)\n"
            "assert not [m for m in sys.modules if m.startswith('opentelemetry.sdk') or m.startswith('opentelemetry.exporter')], 'the SDK was loaded'\n"
            "print('ok')\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr


def test_an_application_built_with_the_default_settings_has_tracing_off(world):
    assert world.observability.tracing is False and tracing.enabled() is False


def test_switching_it_off_stops_the_spans_at_once(world, exporter):
    world.agents.ask("How much BOLT-M8 do we have?", VIEWER)
    assert exporter.get_finished_spans()
    exporter.clear()
    tracing.configure(False)
    world.agents.client_factory = lambda cfg: ScriptedModel()
    world.agents.ask("How much BOLT-M8 do we have?", VIEWER)
    assert not exporter.get_finished_spans() and not tracing.enabled()


# ------------------------------------------------------------------ one task, one trace
def test_one_agent_task_is_one_trace_with_the_model_calls_the_tool_call_and_the_erpnext_request_below_it(world, exporter):
    through_erpnext(world, Canned())
    res = world.agents.ask("How much BOLT-M8 do we have?", VIEWER)
    spans = exporter.get_finished_spans()
    named = by_name(spans)
    assert {k: len(v) for k, v in named.items()} == {"agent.task": 1, "llm.call": 3, "tool.call": 1, "erp.request": 1}, [s.name for s in spans]
    assert len({s.context.trace_id for s in spans}) == 1                                              # one trace
    (task,), (tool,), (request,) = named["agent.task"], named["tool.call"], named["erp.request"]
    assert task.parent is None
    assert all(s.parent.span_id == task.context.span_id for s in named["llm.call"] + [tool])
    assert request.parent.span_id == tool.context.span_id                                             # the ERPNext request belongs to the tool call that made it
    assert dict(task.attributes) == {"agent": "inventory", "task": "ask", "task_id": res["task_id"], "status": "answered", "source": "model"}      # the task id joins the trace to the log
    assert dict(request.attributes) == {"method": "GET", "kind": "resource:Item", "status": 200}
    assert (dict(tool.attributes)["tool"], dict(tool.attributes)["ok"], dict(tool.attributes)["via"]) == ("stock_status", True, "agent")
    first = named["llm.call"][0]
    assert (dict(first.attributes)["provider"], dict(first.attributes)["model"], dict(first.attributes)["kind"], dict(first.attributes)["outcome"]) == ("ollama", "scripted", "gather", "ok")
    assert (dict(first.attributes)["tokens_in"], dict(first.attributes)["tokens_out"]) == (80, 10)
    assert all(s.status.status_code.name != "ERROR" for s in spans)
    assert dict(task.resource.attributes)["service.name"] == "erpnext-ai-agents"


def test_the_spans_carry_names_and_counts_and_nothing_private(world, exporter):
    through_erpnext(world, Canned())
    obs.register_secret("ERPKEY-sentinel")
    world.agents.ask("How much BOLT-M8 do we have? Call me on +91 98765 43210, my name is Priya Raman.", VIEWER)
    spans = exporter.get_finished_spans()
    text = everything_in(spans)
    for private in ("BOLT-M8", "98765", "Priya", "Raman", "filters", "item_code", "ERPKEY", "ERPSECRET", "How much", "300 pcs"):
        assert private not in text, private
    for s in spans:
        assert set(s.attributes or {}) <= ALLOWED, (s.name, set(s.attributes) - ALLOWED)
        assert not s.events


def test_a_value_that_is_not_a_plain_string_or_number_is_left_out_and_a_long_one_is_cut(exporter):
    with tracing.span("probe", text="x" * 500, number=3, flag=True, nothing=None, thing=object(), items=["a"]) as sp:
        sp.set(another={"k": "v"}, fine="ok")
    (probe,) = exporter.get_finished_spans()
    attrs = dict(probe.attributes)
    assert attrs == {"text": "x" * tracing.MAX_VALUE, "number": 3, "flag": True, "fine": "ok"}


def test_annotating_with_no_span_current_does_nothing(exporter):
    tracing.annotate(tool="stock_status")
    tracing.mark_failed("Boom")
    assert not exporter.get_finished_spans()


# ------------------------------------------------------------------ failures: the class, not the text
def test_a_failing_model_marks_its_span_by_the_class_of_the_failure_and_never_quotes_it(world, exporter):
    class Down(ScriptedModel):
        def chat(self, messages, tools=None, schema=None):
            raise LLMError("could not reach the model at http://127.0.0.1:11434 (secret detail)")
    world.agents.client_factory = lambda cfg: Down()
    world.agents.ask("How much BOLT-M8 do we have?", VIEWER)
    spans = exporter.get_finished_spans()
    call = by_name(spans)["llm.call"][0]
    assert call.status.status_code.name == "ERROR" and call.status.description is None
    assert dict(call.attributes)["error.type"] == "LLMError" and dict(call.attributes)["outcome"] == "error"
    assert "secret detail" not in everything_in(spans) and not any(s.events for s in spans)           # (no recorded exception: that carries the message and the stack)


def test_a_refused_tool_call_and_a_failing_erpnext_request_are_errors_by_their_class(world, exporter):
    world.toolkit.call("prepare_order_request", {"item_code": "BOLT-M8", "qty": 500, "reason": "private words"}, VIEWER, via="api")
    erp = erp_with(Canned(status=500, body=b'{"exc": "boom with a secret"}'))
    with pytest.raises(Exception):
        erp._list("Item", ["name"], [])
    spans = exporter.get_finished_spans()
    tool = by_name(spans)["tool.call"][0]
    assert tool.status.status_code.name == "ERROR" and dict(tool.attributes)["error.type"] == "not_allowed" and dict(tool.attributes)["ok"] is False
    request = by_name(spans)["erp.request"][0]
    assert request.status.status_code.name == "ERROR" and dict(request.attributes)["error.type"] == "500" and dict(request.attributes)["status"] == 500
    text = everything_in(spans)
    assert "private words" not in text and "BOLT-M8" not in text and "secret" not in text


def test_a_request_that_raises_marks_the_span_by_the_exception_class_and_the_exception_goes_on(exporter):
    import requests
    erp = erp_with(Canned(fail=requests.ConnectTimeout("timed out talking to erp.example.test with a secret")))
    assert erp.healthcheck() is False                                                                 # (the client's own handling of it)
    (request,) = by_name(exporter.get_finished_spans())["erp.request"]
    assert request.status.status_code.name == "ERROR" and dict(request.attributes)["error.type"] == "ConnectTimeout"
    assert "secret" not in everything_in(exporter.get_finished_spans())


# ------------------------------------------------------------------ switching it on
def test_the_real_exporter_is_set_up_from_the_settings_and_a_service_name_without_any_connection():
    tracing.configure(True, endpoint="http://127.0.0.1:9/v1/traces", service_name="stock-assistant-test")
    assert tracing.enabled()
    state = tracing._state
    assert dict(state.provider.resource.attributes)["service.name"] == "stock-assistant-test"
    with tracing.span("nothing-to-send-yet") as sp:
        sp.set(ok=True)
    tracing.configure(False)                                                                          # (the pending span is flushed to a port nothing listens on, and that is only logged)
    assert not tracing.enabled()


def test_the_application_starts_with_tracing_on_and_stops_it_when_built_with_it_off(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()), cfg={"observability": {"tracing": True, "otlp_endpoint": "http://127.0.0.1:9/v1/traces", "service_name": "svc"}})
    assert tracing.enabled() and app.observability.tracing is True and app.observability.service_name == "svc"
    make_app(erp=MockERP(today=clock().date()))
    assert not tracing.enabled()                                                                      # an application built without it does not inherit the setting


def test_tracing_on_without_the_packages_stops_the_start_and_says_how_to_install_them(make_app, clock, monkeypatch):
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)                                 # an import of it now fails, as if the extra were not installed
    with pytest.raises(StartupRefused) as refused:
        make_app(erp=MockERP(today=clock().date()), cfg={"observability": {"tracing": True}})
    assert 'pip install "erpnext-ai-agents[otel]"' in str(refused.value) and "observability.tracing" in str(refused.value)
    assert not tracing.enabled()


# ------------------------------------------------------------------ the settings
@pytest.mark.parametrize("raw, problem", [({"tracing": "yes"}, "observability.tracing must be true or false"),
                                          ({"otlp_endpoint": "localhost:4318"}, "otlp_endpoint must be a web address"),
                                          ({"otlp_endpoint": "ftp://collector/traces"}, "otlp_endpoint must be a web address"),
                                          ({"otlp_endpoint": "http://"}, "otlp_endpoint must be a web address")])
def test_the_tracing_settings_are_checked_when_the_application_starts(raw, problem):
    with pytest.raises(ValueError, match=problem):
        obs.settings_from({"observability": raw})


def test_the_defaults_and_a_good_endpoint():
    default = obs.settings_from({})
    assert (default.tracing, default.otlp_endpoint, default.service_name) == (False, "", "erpnext-ai-agents")
    good = obs.settings_from({"observability": {"tracing": True, "otlp_endpoint": " https://otel.example.test:4318/v1/traces ", "service_name": ""}})
    assert (good.tracing, good.otlp_endpoint, good.service_name) == (True, "https://otel.example.test:4318/v1/traces", "erpnext-ai-agents")

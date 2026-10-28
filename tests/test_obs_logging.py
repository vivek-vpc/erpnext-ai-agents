"""Logging that can be followed and cannot leak (Phase 2 of the hardening brief; core/obs.py): ids bound for a task or a request, the plain and the JSON format, the settings
section, and the scrubber's rules."""
import asyncio
import contextvars
import io
import json
import logging
import threading

import anyio
import pytest

from erp_agents.core import cli, obs
from erp_agents.core.bootstrap import StartupRefused, build_app


@pytest.fixture(autouse=True)
def clean_scrubber():
    obs.SCRUBBER.forget_all()
    yield
    obs.SCRUBBER.forget_all()


def written(fmt, *calls):
    """Make some log calls through a handler of the given format and return the lines."""
    buf = io.StringIO()
    logger = logging.getLogger("erp_agents.test_obs." + fmt)
    logger.handlers, logger.propagate, logger.level = [obs.make_handler(buf, fmt)], False, logging.DEBUG
    for call in calls:
        call(logger)
    return buf.getvalue().strip().splitlines()


# ------------------------------------------------------------------ the ids
def test_ids_are_bound_for_the_block_nest_and_are_restored():
    assert obs.current_ids() == {}
    with obs.bind(task_id="t1"):
        assert obs.current_ids() == {"task_id": "t1"}
        with obs.bind(request_id="r1"):
            assert obs.current_ids() == {"task_id": "t1", "request_id": "r1"}
        assert obs.current_ids() == {"task_id": "t1"}
    assert obs.current_ids() == {}


def test_a_failure_inside_the_block_still_restores_the_ids():
    with pytest.raises(RuntimeError):
        with obs.bind(task_id="t1"):
            raise RuntimeError("x")
    assert obs.current_ids() == {}


def test_the_ids_reach_worker_threads_and_tasks_that_anyio_and_asyncio_start_but_not_a_plain_thread():
    seen = {}

    async def go():
        with obs.bind(task_id="t-anyio"):
            seen["thread"] = await anyio.to_thread.run_sync(obs.current_ids)
            async with anyio.create_task_group() as tg:
                async def child():
                    seen["task"] = obs.current_ids()
                tg.start_soon(child)
            plain = []
            t = threading.Thread(target=lambda: plain.append(obs.current_ids()))
            t.start()
            t.join()
            seen["plain"] = plain[0]
            copied = []
            t = threading.Thread(target=contextvars.copy_context().run, args=(lambda: copied.append(obs.current_ids()),))
            t.start()
            t.join()
            seen["copied"] = copied[0]
    anyio.run(go)
    assert seen == {"thread": {"task_id": "t-anyio"}, "task": {"task_id": "t-anyio"}, "plain": {}, "copied": {"task_id": "t-anyio"}}

    async def aio2():
        async def inner():
            return obs.current_ids()
        with obs.bind(request_id="r-asyncio"):
            return await asyncio.create_task(inner())
    assert asyncio.run(aio2()) == {"request_id": "r-asyncio"}


def test_a_request_id_from_a_caller_is_kept_only_when_it_is_harmless():
    assert obs.clean_request_id("abc123-DEF_456.7") == "abc123-DEF_456.7"
    for bad in ("short", "has space in it", "x" * 80, "line\nbreak12345", "", None, "semi;colon12345"):
        assert obs.clean_request_id(bad) is None
    assert len(obs.new_request_id()) == 12 and obs.new_request_id() != obs.new_request_id()


# ------------------------------------------------------------------ the formats
def test_the_plain_format_is_the_old_line_until_an_id_is_bound():
    plain, tagged = written("text", lambda lg: lg.info("hello %s", "world")), None

    def inside(lg):
        with obs.bind(task_id="ab12cd34ef", request_id="req-0001abcd"):
            lg.warning("inside")
    tagged = written("text", inside)
    assert plain[0].endswith("INFO erp_agents.test_obs.text: hello world")
    assert tagged[0].endswith("WARNING erp_agents.test_obs.text [task=ab12cd34ef request=req-0001abcd]: inside")


def test_the_json_format_is_one_object_per_line_with_the_ids_the_fields_and_the_traceback():
    def calls(lg):
        lg.info("plain line")
        with obs.bind(task_id="t9"):
            lg.info("with fields", extra={"fields": {"event": "tool_call", "ms": 12, "ok": True}})
            try:
                raise ValueError("boom")
            except ValueError:
                lg.exception("it failed")
    lines = [json.loads(x) for x in written("json", calls)]
    assert [l["msg"] for l in lines] == ["plain line", "with fields", "it failed"]
    assert lines[0].keys() == {"ts", "level", "logger", "msg"} and lines[0]["level"] == "INFO" and lines[0]["ts"].endswith("+00:00")
    assert lines[1]["task_id"] == "t9" and lines[1]["event"] == "tool_call" and lines[1]["ms"] == 12 and lines[1]["ok"] is True
    assert "ValueError: boom" in lines[2]["exc"] and lines[2]["task_id"] == "t9"


def test_a_message_with_line_breaks_is_one_json_line():
    (line,) = written("json", lambda lg: lg.info("first\nsecond\r\nthird"))
    assert json.loads(line)["msg"] == "first\nsecond\r\nthird"


def test_an_unknown_format_is_refused_in_words():
    with pytest.raises(ValueError, match="use one of: text, json"):
        obs.make_handler(io.StringIO(), "xml")
    with pytest.raises(ValueError, match="use one of: text, json"):
        obs.log_format_from("yaml")


def test_configure_logging_replaces_its_own_handler_and_leaves_the_others_alone():
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    other = logging.NullHandler()
    root.addHandler(other)
    try:
        first = obs.configure_logging(logging.INFO, "text", io.StringIO())
        second = obs.configure_logging(logging.DEBUG, "json", io.StringIO())
        ours = [h for h in root.handlers if getattr(h, "_erp_agents", False)]
        assert ours == [second] and first not in root.handlers and other in root.handlers and root.level == logging.DEBUG
    finally:
        root.handlers, root.level = saved, level


def test_scrubbing_does_not_change_the_record_other_handlers_receive():
    seen = []

    class Keep(logging.Handler):
        def emit(self, record):
            seen.append((record.getMessage(), record.exc_info is not None))
    secret = "tok-secret-0123456789"
    obs.register_secret(secret)
    logger = logging.getLogger("erp_agents.test_obs.copy")
    buf = io.StringIO()
    logger.handlers, logger.propagate, logger.level = [obs.make_handler(buf, "text"), Keep()], False, logging.DEBUG
    try:
        raise RuntimeError(secret)
    except RuntimeError:
        logger.exception("failed with %s", secret)
    assert secret not in buf.getvalue() and "[hidden]" in buf.getvalue()
    assert seen == [(f"failed with {secret}", True)]


# ------------------------------------------------------------------ the scrubber
def test_a_configured_secret_is_hidden_in_every_form_it_can_take():
    key = "AK/with+odd chars=0123456789"
    obs.register_secret(key)
    from urllib.parse import quote
    for form in (key, quote(key, safe=""), quote(key)):
        assert obs.scrub(f"sent {form} to the server") == "sent [hidden] to the server"


def test_a_value_too_short_to_be_a_secret_is_not_registered():
    obs.register_secret("owner")
    obs.register_secret("")
    obs.register_secret(None)
    assert obs.scrub("the owner asked") == "the owner asked"


def test_a_sign_in_token_registered_while_running_is_hidden_and_the_list_is_bounded():
    for i in range(2100):
        obs.register_secret(f"session-token-{i:06d}", dynamic=True)
    assert obs.scrub("session-token-002099 was used") == "[hidden] was used"
    assert "session-token-000000" in obs.scrub("session-token-000000")                  # the oldest were forgotten: bounded memory


@pytest.mark.parametrize("raw,must_go", [
    ("Authorization: Bearer abcdef0123456789xyz", "abcdef0123456789xyz"),
    ("headers {'x-api-key': 'sk-ant-api03-0123456789abcdefghij'}", "sk-ant-api03-0123456789abcdefghij"),
    ("token erpkey-77aa91:erpsecret-55bb13 may not read Item", "erpsecret-55bb13"),
    ("POST /bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0/sendMessage", "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw0"),
    ("GET https://api.example.test/v1?api_key=ABCDEFGH12345&to=1", "ABCDEFGH12345"),
    ("access EAAGm0PX4ZCpsBAKl8ZAYz0123456789abcdef used", "EAAGm0PX4ZCpsBAKl8ZAYz0123456789abcdef"),
    ("key AKIAIOSFODNN7EXAMPLE leaked", "AKIAIOSFODNN7EXAMPLE"),
    ("secret=hunter2hunter2&x=1", "hunter2hunter2"),
])
def test_things_shaped_like_a_credential_are_hidden_even_when_nobody_registered_them(raw, must_go):
    assert must_go not in obs.scrub(raw)


@pytest.mark.parametrize("raw", ["+91 98765 43210", "+919876543210", "919876543210", "98765 43210", "98765-43210", "(+91) 98765-43210", "0091 9876543210"])
def test_a_phone_number_is_hidden_in_the_forms_people_write_it(raw):
    out = obs.scrub(f"vendor {raw} did not answer")
    assert "43210" not in out and out.startswith("vendor [number]") or "[number]" in out and "98765" not in out and "9876543210" not in out


def test_a_registered_address_is_found_again_in_another_form():
    obs.SCRUBBER.register_phone("+91 98765 43210")
    assert obs.scrub("sent to 919876543210 and +91-98765-43210 and 98765 43210") == "sent to [number] and [number] and [number]"


@pytest.mark.parametrize("raw", [
    "order PUR-ORD-2026-00543 for 1,234,567 pcs at 18435.00 on 2026-10-28 12:30", "run d10f99cf took 12.5 s, version 1.2.3, 7.3 days", "ip 192.168.1.20 port 8100", "qty 1000000 of BOLT-M8",
    "request 91ec4ea7e3 expires 2026-11-05", "the number 12345 is short", "cover 7.3 days, 40.93 a day",
])
def test_ordinary_numbers_and_codes_are_left_alone(raw):
    assert obs.scrub(raw) == raw


def test_a_traceback_is_scrubbed_too():
    secret = "ZZ-secret-key-0123456789"
    obs.register_secret(secret)
    (line,) = written("json", lambda lg: _fail(lg, secret))
    assert secret not in line and "RuntimeError" in json.loads(line)["exc"]


def _fail(logger, secret):
    try:
        raise RuntimeError(f"bad {secret}")
    except RuntimeError:
        logger.exception("task failed")


def test_every_secret_and_address_in_the_configuration_is_registered():
    cfg = {"auth": {"tokens": [{"user": "o", "token": "owner-access-key-0123456789"}]}, "llm_keys": {"anthropic": "sk-ant-model-key-0123456789"},
           "erp": {"type": "erpnext", "api_key": "erp-api-key-0123456", "api_secret": "erp-api-secret-0123456"},
           "channels": {"telegram": {"enabled": True, "bot_token": "999999999:telegram-bot-token-0123456789abcdef", "webhook_secret": "wh-secret-0123456789"},
                        "whatsapp": {"access_token": "wa-access-token-0123456789", "app_secret": "wa-app-secret-0123456", "verify_token": "wa-verify-0123456789", "phone_number_id": "1234567890123"}},
           "mcp": {"client_token": "mcp-client-token-0123456789"},
           "approvals": {"recipients": [{"id": "o", "channels": [{"type": "whatsapp", "address": "+91 98123 45678"}, {"type": "telegram", "address": "5551234567"}]}]}}
    obs.register_secrets_from_config(cfg)
    text = " ".join(["owner-access-key-0123456789", "sk-ant-model-key-0123456789", "erp-api-key-0123456", "erp-api-secret-0123456", "999999999:telegram-bot-token-0123456789abcdef", "wh-secret-0123456789",
                     "wa-access-token-0123456789", "wa-app-secret-0123456", "wa-verify-0123456789", "mcp-client-token-0123456789", "919812345678", "5551234567"])
    assert obs.scrub(text) == " ".join(["[hidden]"] * 10 + ["[number]"] * 2)
    assert obs.scrub("phone_number_id PNID") == "phone_number_id PNID"                  # a setting's name, and a value that is no secret, stay readable


def test_a_registered_environment_secret_is_hidden(monkeypatch):
    monkeypatch.setenv("ERP_AGENTS_METRICS_TOKEN", "metrics-scraper-token-0123456789")
    obs.register_secrets_from_config({})
    assert obs.scrub("scrape with metrics-scraper-token-0123456789") == "scrape with [hidden]"


# ------------------------------------------------------------------ one-line event records
def test_an_event_line_has_names_numbers_and_outcomes_and_the_ids_never_text():
    calls = logging.getLogger("erp_agents.calls")
    buf = io.StringIO()
    handler = obs.make_handler(buf, "text")
    calls.addHandler(handler)
    json_buf = io.StringIO()
    json_handler = obs.make_handler(json_buf, "json")
    calls.addHandler(json_handler)
    old_level = calls.level
    calls.setLevel(logging.INFO)
    try:
        with obs.bind(task_id="tk-1"):
            obs.event("llm_call", provider="ollama", model="llama3.1", kind="answer", ms=1200, outcome="ok", tokens_in=900, note=None, odd="two words")
        calls.setLevel(logging.WARNING)
        obs.event("llm_call", ms=1)                                                      # below the level: nothing is written
    finally:
        calls.removeHandler(handler)
        calls.removeHandler(json_handler)
        calls.setLevel(old_level)
    (line,) = buf.getvalue().strip().splitlines()
    assert 'llm_call provider=ollama model=llama3.1 kind=answer ms=1200 outcome=ok tokens_in=900 odd="two words"' in line and "[task=tk-1]" in line and "note" not in line
    obj = json.loads(json_buf.getvalue())
    assert obj["event"] == "llm_call" and obj["task_id"] == "tk-1" and obj["ms"] == 1200 and "note" not in obj


# ------------------------------------------------------------------ settings
def test_the_observability_section_has_safe_defaults_and_refuses_what_it_does_not_know():
    s = obs.settings_from({})
    assert (s.log_format, s.metrics, s.tracing, s.otlp_endpoint, s.service_name, s.pending_hours) == ("text", False, False, "", "erpnext-ai-agents", 24.0)
    ok = obs.settings_from({"observability": {"log_format": "JSON", "metrics": True, "tracing": False, "otlp_endpoint": " http://localhost:4318/v1/traces ", "service_name": "stock", "pending_hours": 8}})
    assert (ok.log_format, ok.metrics, ok.otlp_endpoint, ok.service_name, ok.pending_hours) == ("json", True, "http://localhost:4318/v1/traces", "stock", 8.0)
    for bad, why in (({"log_fromat": "json"}, "unknown setting"), ({"log_format": "xml"}, "use one of"), ({"metrics": "yes"}, "true or false"), ({"tracing": 1}, "true or false"),
                     ({"pending_hours": "soon"}, "number of hours"), ({"pending_hours": 0}, "more than 0"), ("json", "section")):
        with pytest.raises(ValueError, match=why):
            obs.settings_from({"observability": bad})


def test_a_wrong_observability_section_stops_the_start_in_plain_words():
    with pytest.raises(StartupRefused, match="observability.log_format is 'xml'"):
        build_app(config={"database": {"path": ":memory:"}, "erp": {"type": "mock"}, "observability": {"log_format": "xml"}, "channels": {}, "approvals": {}, "auth": {"tokens": []}})


def test_the_flag_wins_over_the_environment_and_the_environment_over_the_settings_file(monkeypatch):
    monkeypatch.delenv(obs.LOG_FORMAT_ENV, raising=False)
    assert obs.log_format_from(None, None) == "text"
    assert obs.log_format_from(None, "json") == "json"
    monkeypatch.setenv(obs.LOG_FORMAT_ENV, "text")
    assert obs.log_format_from(None, "json") == "text"
    assert obs.log_format_from("json", "text") == "json"


def test_the_command_line_takes_a_log_format_and_refuses_a_wrong_one(monkeypatch, capsys, tmp_path):
    chosen = []
    monkeypatch.delenv(obs.LOG_FORMAT_ENV, raising=False)
    monkeypatch.setattr(obs, "configure_logging", lambda level, fmt, stream=None: chosen.append(fmt))
    assert cli.main(["--log-format", "json", "backup-verify", str(tmp_path / "none.db")]) == 1
    assert chosen == ["json"]
    monkeypatch.setenv(obs.LOG_FORMAT_ENV, "xml")
    assert cli.main(["backup-verify", str(tmp_path / "none.db")]) == 2
    assert "use one of: text, json" in capsys.readouterr().err

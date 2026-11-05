"""The agents through the REST API, the chat, the scheduler and the privacy summary."""
import json
import time

import pytest

pytest.importorskip("mcp", reason="the optional 'agents' extra is not installed")

from fastapi.testclient import TestClient

from conftest import APPROVER, OWNER
from erp_agents.packs.stock_procurement.agents.inventory_agent import rules_brief
from erp_agents.core.agents.privacy import privacy_summary
from erp_agents.core.api.app import create_app
from erp_agents.packs.stock_procurement.assistant.chat import looks_like_a_question
from ai_munimji_engine.core.policy import AgentSettings
from erp_agents.core.erp.mock import MockERP
from erp_agents.core.scheduler import AgentScheduler
from erp_agents.packs.stock_procurement.jobs import BRIEF_JOB
from test_agents import done, good_brief, say, with_model

TOKENS = [{"token": "o" * 40, "user": "owner", "roles": ["viewer", "approver", "admin"]}, {"token": "m" * 40, "user": "manager", "roles": ["viewer", "approver"]},
          {"token": "v" * 40, "user": "viewer", "roles": ["viewer"]}, {"token": "w" * 40, "user": "viewer2", "roles": ["viewer"]}]


def hdr(letter):
    return {"Authorization": "Bearer " + letter * 40}


@pytest.fixture
def web(make_app, clock):
    def make(enabled=True, **settings):
        app = make_app(erp=MockERP(today=clock().date()), cfg={"auth": {"tokens": TOKENS}})
        app.settings.update_section(OWNER, "agents", {"enabled": enabled, **settings})
        return app, TestClient(create_app(app))
    return make


def finished(c, who, task_id):
    for _ in range(300):
        row = c.get(f"/api/v1/agents/tasks/{task_id}", headers=hdr(who)).json()
        if row["status"] != "running":
            return row
        time.sleep(0.03)
    raise AssertionError("the task did not finish")


# ------------------------------------------------------------------ status, privacy, brief
def test_status_and_privacy_are_open_to_anyone_who_may_look_and_show_no_secrets(web):
    app, c = web()
    st = c.get("/api/v1/agents/status", headers=hdr("v")).json()
    assert st["enabled"] is True and st["model"] == "llama3.1" and st["leaves_this_server"] is False and st["tools"] == "built-in"
    assert c.get("/api/v1/agents/status").status_code == 401
    pv = c.get("/api/v1/agents/privacy", headers=hdr("v")).json()
    assert pv["data_leaves_this_server"] is False and "Customer names" in " ".join(pv["never_sent_to_the_model"]) and pv["trace_kept_days"] == 30
    assert st["key_configured"] is None and st["provider"] == "ollama" and st["hosted"] is False


def test_only_the_administrator_changes_the_agent_settings_and_a_remote_model_needs_the_owners_say_so(web):
    app, c = web(enabled=False)
    put = lambda who, body: c.put("/api/v1/settings/module/agents", json={"set": body}, headers=hdr(who))        # noqa: E731
    assert put("m", {"enabled": True}).status_code == 403 and put("v", {"enabled": True}).status_code == 403
    assert put("o", {"enabled": True, "model": "llama3.1:latest", "max_steps": 4}).json()["effective"]["max_steps"] == 4
    assert put("o", {"base_url": "https://llm.example.com"}).status_code == 422                                  # on another machine: not unless allowed
    assert put("o", {"daily_brief_cron": "every morning"}).status_code == 422 and put("o", {"max_steps": 99}).status_code == 422
    ok = put("o", {"allow_remote": True, "base_url": "https://llm.example.com"})
    assert ok.status_code == 200 and c.get("/api/v1/agents/privacy", headers=hdr("v")).json()["data_leaves_this_server"] is True
    assert c.get("/api/v1/agents/status", headers=hdr("v")).json()["leaves_this_server"] is True
    assert put("o", {"allow_remote": False}).status_code == 422                                                  # cannot switch the guard on while the address is remote


def test_the_privacy_summary_says_so_when_the_model_is_on_another_machine():
    here = privacy_summary(AgentSettings(enabled=True))
    away = privacy_summary(AgentSettings(enabled=True, allow_remote=True, base_url="https://llm.example.com"))
    off = privacy_summary(AgentSettings(enabled=False, allow_remote=True, base_url="https://llm.example.com"))
    assert here["data_leaves_this_server"] is False and "same machine" in here["where_it_goes"]
    assert away["data_leaves_this_server"] is True and "llm.example.com" in away["where_it_goes"]
    assert off["data_leaves_this_server"] is False                                                           # nothing is sent while the agents are off


def test_the_brief_before_any_is_stored_is_the_rules_brief_and_running_one_is_for_approvers(web):
    app, c = web()
    first = c.get("/api/v1/agents/brief", headers=hdr("v")).json()
    assert first["stored"] is False and first["source"] == "rules" and first["priorities"]
    assert c.post("/api/v1/agents/brief", headers=hdr("v")).status_code == 403
    with_model(app, say(good_brief(app)))
    started = c.post("/api/v1/agents/brief", headers=hdr("m")).json()
    assert started["status"] == "running"
    row = finished(c, "m", started["task_id"])
    assert row["status"] == "ok" and row["result"]["source"] == "model" and "trace" not in row
    again = c.get("/api/v1/agents/brief", headers=hdr("v")).json()
    assert again["stored"] is True and again["task_id"] == started["task_id"] and again["source"] == "model"


def test_with_the_agents_off_the_brief_button_gives_the_rules_brief_at_once(web):
    app, c = web(enabled=False)
    llm = with_model(app, say({}))
    res = c.post("/api/v1/agents/brief", headers=hdr("m")).json()
    assert res["source"] == "rules" and res["priorities"] and llm.calls == []


# ------------------------------------------------------------------ questions
def test_a_question_runs_in_the_background_and_belongs_to_the_person_who_asked_it(web):
    app, c = web()
    with_model(app, done(), say({"answerable": False, "answer": ""}))
    res = c.post("/api/v1/agents/ask", headers=hdr("v"), json={"question": "How much BOLT-M8 do we have?"})
    assert res.status_code == 200 and res.json()["status"] == "running"
    tid = res.json()["task_id"]
    assert finished(c, "v", tid)["result"]["status"] == "not_enough_information"
    assert c.get(f"/api/v1/agents/tasks/{tid}", headers=hdr("w")).status_code == 404                           # another viewer does not see it
    assert c.get(f"/api/v1/agents/tasks/{tid}", headers=hdr("o")).status_code == 200                           # the administrator does
    assert [t["id"] for t in c.get("/api/v1/agents/tasks?task=ask", headers=hdr("w")).json()] == []
    assert [t["id"] for t in c.get("/api/v1/agents/tasks?task=ask", headers=hdr("v")).json()] == [tid]


def test_what_was_sent_to_the_model_is_for_the_administrator_only(web):
    app, c = web()
    with_model(app, done(), say({"answerable": False, "answer": ""}))
    tid = c.post("/api/v1/agents/ask", headers=hdr("o"), json={"question": "How much BOLT-M8 do we have?"}).json()["task_id"]
    finished(c, "o", tid)
    assert "trace" not in c.get(f"/api/v1/agents/tasks/{tid}?trace=true", headers=hdr("m")).json()
    admin = c.get(f"/api/v1/agents/tasks/{tid}?trace=true", headers=hdr("o")).json()
    assert admin["trace"][0]["messages"][0]["role"] == "system"


def test_asking_is_refused_cleanly_when_off_or_badly_formed(web):
    app, c = web(enabled=False)
    assert c.post("/api/v1/agents/ask", headers=hdr("v"), json={"question": "What is low?"}).status_code == 409
    app2, c2 = web()
    assert c2.post("/api/v1/agents/ask", headers=hdr("v"), json={"question": ""}).status_code == 422
    assert c2.post("/api/v1/agents/ask", headers=hdr("v"), json={"question": "x" * 301}).status_code == 422


# ------------------------------------------------------------------ the second opinion
def test_an_order_request_can_be_reviewed_through_the_api(web):
    app, c = web()
    r = app.toolkit.call("prepare_order_request", {"item_code": "BOLT-M8", "qty": 800, "reason": "test"}, APPROVER)
    rid = r["request_id"]
    assert c.post(f"/api/v1/agents/reviews/{rid}", headers=hdr("v")).status_code == 403
    assert c.post("/api/v1/agents/reviews/nope", headers=hdr("m")).status_code == 422
    assert c.get(f"/api/v1/agents/reviews/{rid}", headers=hdr("m")).json() == {"review": None}
    with_model(app, done(), say({"points": []}))
    started = c.post(f"/api/v1/agents/reviews/{rid}", headers=hdr("m")).json()
    row = finished(c, "m", started["task_id"])
    assert row["status"] == "ok" and row["result"]["verdict"] in ("agree", "check_first") and row["approval_id"] == rid
    again = c.post(f"/api/v1/agents/reviews/{rid}", headers=hdr("m")).json()
    assert again["cached"] is True and again["task_id"] == started["task_id"]
    assert c.get(f"/api/v1/agents/reviews/{rid}", headers=hdr("m")).json()["review"]["id"] == started["task_id"]


def test_with_the_agents_off_a_review_is_the_checks_alone(web):
    app, c = web(enabled=False)
    rid = app.toolkit.call("prepare_order_request", {"item_code": "BOLT-M8", "qty": 800, "reason": "test"}, APPROVER)["request_id"]
    res = c.post(f"/api/v1/agents/reviews/{rid}", headers=hdr("m")).json()
    assert res["source"] == "rules" and res["points"] == [] and res["verdict"] in ("agree", "check_first")


# ------------------------------------------------------------------ the chat hands over what its rules cannot place
def test_the_chat_asks_the_agents_only_for_questions_it_cannot_place_and_only_when_they_are_on(web):
    app, c = web()
    llm = with_model(app, done(), say({"answerable": True, "answer": "Nothing like that is recorded."}))
    asked = c.post("/api/v1/chat", headers=hdr("v"), json={"question": "Which warehouse is the busiest this month?"}).json()
    assert asked["source"] == "agent" and asked["status"] == "working" and "few minutes" in asked["answer"]                       # the web page polls; nothing waits minutes on a tunnel
    done_row = finished(c, "v", asked["task_id"])
    assert done_row["result"]["status"] == "answered" and done_row["result"]["answer"] == "Nothing like that is recorded." and len(llm.calls) == 2
    llm2 = with_model(app, say({}))
    assert c.post("/api/v1/chat", headers=hdr("v"), json={"question": "hello"}).json()["status"] == "help" and llm2.calls == []                # a greeting is not worth a model
    assert c.post("/api/v1/chat", headers=hdr("v"), json={"question": "What is running low?"}).json()["source"] == "data" and llm2.calls == []   # the rules answer this themselves


def test_with_the_agents_off_the_chat_shows_its_usual_help(web):
    app, c = web(enabled=False)
    llm = with_model(app, say({}))
    res = c.post("/api/v1/chat", headers=hdr("v"), json={"question": "Which warehouse is the busiest this month?"}).json()
    assert res["status"] == "help" and res["source"] == "data" and llm.calls == []


def test_which_messages_are_worth_a_model():
    assert looks_like_a_question("Is anything strange about our suppliers?") and looks_like_a_question("show me the slowest moving items")
    assert not looks_like_a_question("hello") and not looks_like_a_question("thanks a lot") and not looks_like_a_question("ok")


# ------------------------------------------------------------------ the morning brief on a schedule
def test_the_brief_job_is_scheduled_only_when_a_time_is_set_and_follows_the_setting(web):
    app, _ = web(daily_brief_cron="0 9 * * *")
    sched = AgentScheduler(app)
    sched._sync_brief_job()
    assert sched.sched.get_job(BRIEF_JOB) is not None
    app.settings.update_section(OWNER, "agents", {"daily_brief_cron": "30 8 * * *"})
    sched.reschedule()
    assert "8" in str(sched.sched.get_job(BRIEF_JOB).trigger)
    app.settings.update_section(OWNER, "agents", {"daily_brief_cron": ""})
    sched.reschedule()
    assert sched.sched.get_job(BRIEF_JOB) is None
    app.settings.update_section(OWNER, "agents", {"daily_brief_cron": "0 9 * * *", "enabled": False})
    sched.reschedule()
    assert sched.sched.get_job(BRIEF_JOB) is None                                                              # off means off


def test_the_brief_job_runs_the_agent_and_sends_the_brief_only_when_asked_to(web, monkeypatch):
    app, _ = web(daily_brief_cron="0 9 * * *")
    sent = []
    monkeypatch.setattr(app.agents, "tell_approvers", lambda text: sent.append(text) or 1)
    sched = AgentScheduler(app)
    with_model(app, say(good_brief(app)))
    sched._brief_job()
    assert app.agent_log.latest("inventory", "brief")["status"] == "ok" and sent == []
    app.settings.update_section(OWNER, "agents", {"send_brief": True})
    with_model(app, say(good_brief(app)))
    sched._brief_job()
    assert len(sent) == 1 and rules_brief(app.toolkit.call("stock_overview", {"limit": 8}, OWNER))["headline"] in sent[0]
    app.settings.update_section(OWNER, "agents", {"enabled": False})
    sched._brief_job()
    assert len(sent) == 1                                                                                      # off: nothing runs, nothing is sent

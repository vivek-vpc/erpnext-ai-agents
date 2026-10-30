"""'Ask why': answers are built only from recorded facts and are shown only if every deterministic check passes."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import APPROVER, OWNER, FakeResponse
from erp_agents.core.api.app import create_app
from erp_agents.core.assistant.llm import LLMError, OllamaClient
from erp_agents.core.assistant.verify import NOT_ENOUGH
from erp_agents.core.policy import AssistantSettings
from erp_agents.core.settings_store import SettingsError
from erp_agents.core.workorders import REORDER_NEEDED
from erp_agents.core.erp.mock import MockERP


class FakeLLM:
    model = "fake"

    def __init__(self):
        self.replies, self.calls = [], []

    def chat(self, system, user, schema):
        self.calls.append((system, user, schema))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


@pytest.fixture
def world(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    app.settings.update_section(OWNER, "assistant", {"enabled": True})
    llm = FakeLLM()
    app.assistant.client_factory = lambda cfg: llm
    app.inventory.run()
    bolt = next(r for r in app.approvals.list(status="pending") if r.request.facts.get("item_code") == "BOLT-M8")
    return app, llm, bolt


def fact_with(app, approval_id, text):
    return next(f for f in app.assistant.facts_for(approval_id) if text in f.text)


def ask(app, approval_id, q="Why did you raise this?"):
    return app.assistant.ask(approval_id, q, APPROVER)


# ------------------------------------------------------------------ facts
def test_facts_restate_the_recorded_request_and_its_rule_checks(world):
    app, _, bolt = world
    facts = app.assistant.facts_for(bolt.id)
    assert [f.id for f in facts[:3]] == ["F1", "F2", "F3"] and facts[0].text.startswith("Request: Reorder suggested: Bolt M8")
    texts = " | ".join(f.text for f in facts)
    assert "Situation: On hand: 300 pcs" in texts and "Rule lead_time_rule" in texts and "TRIPPED" in texts
    assert "Usage: planned" in texts and "Stock column On hand: 300 (start)" in texts
    assert len({f.text for f in facts}) == len(facts)                                   # no duplicates


def test_vendor_proposal_facts_include_every_vendor_and_why_not(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    app.workorders.emit(REORDER_NEEDED, {"item_code": "PAINT-RED", "item_name": "Red Paint 1L", "uom": "litres", "suggested_qty": 100,
                                         "cover_days": 9, "lead_time_days": 7, "daily_rate": 10, "on_hand": 90, "reasons": ["Low."]})
    app.procurement.run()
    rec = app.approvals.list(status="pending")[0]
    texts = " | ".join(f.text for f in app.assistant.facts_for(rec.id))
    assert "Vendor Prime Supplies (SUP-PRIME): unit price" in texts and "Vendor weights: cost 40%" in texts
    assert "not chosen:" in texts and "; chosen" in texts


# ------------------------------------------------------------------ the checks
def test_a_grounded_answer_is_shown_with_its_sources(world):
    app, llm, bolt = world
    f = fact_with(app, bolt.id, "Situation: On hand: 300 pcs")
    llm.replies.append({"answerable": True, "answer": "You have 300 pcs on hand, which is inside the order point.", "facts_used": [f.id]})
    res = ask(app, bolt.id)
    assert res["status"] == "answered" and res["answer"].startswith("You have 300 pcs") and res["check"] == "passed all checks"
    assert res["facts"] == [{"id": f.id, "text": f.text}] and res["cached"] is False
    system, user, schema = llm.calls[0]
    assert "Use ONLY the numbered FACTS" in system and user.startswith("FACTS:\nF1: \u27e6Request:") and user.endswith("QUESTION: \u27e6Why did you raise this?\u27e7")      # text from the records and the question go in between marks
    assert schema["required"] == ["answerable", "answer", "facts_used"]


@pytest.mark.parametrize("reply,reason", [
    ({"answerable": True, "answer": "You have 450 pcs on hand.", "facts_used": ["F1"]}, "numbers that are not in the recorded data: 450"),
    ({"answerable": True, "answer": "Demand will probably rise next month.", "facts_used": ["F1"]}, "guesses instead of stating recorded facts (probably)"),
    ({"answerable": True, "answer": "It was raised.", "facts_used": []}, "did not cite any recorded fact"),
    ({"answerable": True, "answer": "It was raised.", "facts_used": ["F999"]}, "cited facts that do not exist (F999)"),
    ({"answerable": True, "answer": "", "facts_used": ["F1"]}, "the answer was empty"),
    ({"answerable": True, "answer": "x" * 2000, "facts_used": ["F1"]}, "longer than 900 characters"),
    ("this is not json", "not valid JSON"),
    ({"answer": "hi"}, "did not have the required fields"),
])
def test_anything_that_fails_a_check_becomes_not_enough_information(world, reply, reason):
    app, llm, bolt = world
    llm.replies.append(reply)
    res = ask(app, bolt.id)
    assert res["status"] == "rejected" and res["answer"] == NOT_ENOUGH and reason in res["check"] and res["facts"] == []


def test_numbers_must_come_from_the_cited_facts(world):
    app, llm, bolt = world
    title = fact_with(app, bolt.id, "Request: Reorder suggested")         # contains no stock numbers
    llm.replies.append({"answerable": True, "answer": "You have 300 pcs on hand.", "facts_used": [title.id]})
    res = ask(app, bolt.id)
    assert res["status"] == "rejected" and "cites do not contain the numbers" in res["check"]


def test_numbers_from_the_question_may_be_repeated(world):
    app, llm, bolt = world
    f = fact_with(app, bolt.id, "Situation: On hand: 300 pcs")
    llm.replies.append({"answerable": True, "answer": "The recorded data does not cover an order of 999 pcs; it records 300 pcs on hand.", "facts_used": [f.id]})
    assert ask(app, bolt.id, "What if I order 999 pcs?")["status"] == "answered"


def test_model_saying_it_cannot_answer_is_passed_on_honestly(world):
    app, llm, bolt = world
    llm.replies.append({"answerable": False, "answer": "", "facts_used": []})
    res = ask(app, bolt.id, "What will the weather be next week?")
    assert res["status"] == "not_enough_information" and res["answer"] == NOT_ENOUGH
    llm.replies.append({"answerable": True, "answer": "I do not have enough information to say.", "facts_used": ["F1"]})
    assert ask(app, bolt.id, "Who is the CEO of the vendor?")["status"] == "not_enough_information"


def test_hedge_words_are_allowed_only_when_the_data_itself_uses_them(world):
    app, llm, bolt = world
    f = fact_with(app, bolt.id, "Situation: On hand: 300 pcs")
    llm.replies.append({"answerable": True, "answer": "Stock is likely low: 300 pcs on hand.", "facts_used": [f.id]})
    assert ask(app, bolt.id, "q1")["status"] == "rejected"


# ------------------------------------------------------------------ determinism, availability, settings
def test_same_question_gets_the_stored_answer_without_asking_the_model_again(world):
    app, llm, bolt = world
    f = fact_with(app, bolt.id, "Situation: On hand: 300 pcs")
    llm.replies.append({"answerable": True, "answer": "300 pcs are on hand.", "facts_used": [f.id]})
    first = ask(app, bolt.id, "How much is on hand?")
    again = ask(app, bolt.id, "  how much is   on hand? ")                  # same question, different spacing/case
    assert again["cached"] is True and again["answer"] == first["answer"] and len(llm.calls) == 1
    hist = app.assistant.history(bolt.id)
    assert len(hist) == 1 and hist[0]["question"] == "How much is on hand?" and hist[0]["asked_by"] == "manager" and "draft" not in hist[0]


def test_rejected_answers_are_also_stored_so_the_result_is_repeatable(world):
    app, llm, bolt = world
    llm.replies.append({"answerable": True, "answer": "You have 450 pcs.", "facts_used": ["F1"]})
    assert ask(app, bolt.id, "q")["status"] == "rejected"
    assert ask(app, bolt.id, "q")["status"] == "rejected" and len(llm.calls) == 1
    row = app.db.query_one("SELECT result_json FROM assistant_answers")
    assert "450" in json.loads(row["result_json"])["draft"]                      # kept for audit, never shown


def test_model_down_is_reported_and_not_cached(world):
    app, llm, bolt = world
    llm.replies.append(LLMError("connection refused"))
    res = ask(app, bolt.id)
    assert res["status"] == "unavailable" and "connection refused" in res["answer"]
    f = fact_with(app, bolt.id, "Situation: On hand: 300 pcs")
    llm.replies.append({"answerable": True, "answer": "300 pcs.", "facts_used": [f.id]})
    assert ask(app, bolt.id)["status"] == "answered"                            # asked again once it is back


def test_switched_off_means_the_model_is_never_called(world):
    app, llm, bolt = world
    app.settings.update_section(OWNER, "assistant", {"enabled": False})
    assert ask(app, bolt.id)["status"] == "disabled" and not llm.calls


def test_questions_are_validated(world):
    app, _, bolt = world
    with pytest.raises(ValueError):
        ask(app, bolt.id, "   ")
    with pytest.raises(ValueError):
        ask(app, bolt.id, "x" * 501)
    with pytest.raises(KeyError):
        ask(app, "nope", "why?")


def test_data_stays_on_this_machine_unless_explicitly_allowed():
    assert AssistantSettings().base_url == "http://127.0.0.1:11434"
    with pytest.raises(ValueError, match="must be on this machine"):
        AssistantSettings(base_url="http://192.168.1.50:11434")
    assert AssistantSettings(base_url="http://192.168.1.50:11434", allow_remote=True).allow_remote
    with pytest.raises(ValueError):
        AssistantSettings(base_url="ftp://localhost")


def test_settings_are_audited_and_validated(world):
    app, _, _ = world
    with pytest.raises(SettingsError):
        app.settings.update_section(OWNER, "assistant", {"base_url": "http://example.com:11434"})
    app.settings.update_section(OWNER, "assistant", {"model": "llama3.1:8b"})
    assert app.settings.assistant().model == "llama3.1:8b"
    assert {a["field"] for a in app.settings.audit(scope_type="assistant")} >= {"enabled", "model"}


# ------------------------------------------------------------------ the Ollama call itself
class Session:
    def __init__(self, payload, status=200):
        self.payload, self.status, self.posts = payload, status, []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json, timeout))
        return FakeResponse(self.payload, self.status)

    def get(self, url, timeout=None):
        return FakeResponse({"models": [{"name": "llama3.1:latest"}, {"name": "llava:latest"}]})


def test_ollama_request_is_repeatable_and_structured():
    s = Session({"message": {"content": '{"answerable": false, "answer": "", "facts_used": []}'}})
    c = OllamaClient("http://127.0.0.1:11434/", "llama3.1", timeout=99, session=s)
    out = c.chat("sys", "user", {"type": "object"})
    url, body, timeout = s.posts[0]
    assert url == "http://127.0.0.1:11434/api/chat" and timeout == 99 and out.startswith('{"answerable"')
    assert body["options"]["temperature"] == 0 and body["options"]["seed"] == 42 and body["options"]["top_k"] == 1
    assert body["stream"] is False and body["format"] == {"type": "object"} and body["messages"][0] == {"role": "system", "content": "sys"}
    assert c.status() == {"reachable": True, "model_installed": True, "models": ["llama3.1:latest", "llava:latest"]}
    with pytest.raises(LLMError):
        OllamaClient("http://127.0.0.1:11434", "m", session=Session({"error": "model not found"}, 404)).chat("s", "u", {})


# ------------------------------------------------------------------ API
def test_api_roles_and_errors(make_app, clock):
    cfg = {"auth": {"tokens": [{"token": "owner", "user": "owner", "roles": ["admin", "approver"]}, {"token": "view", "user": "acct", "roles": ["viewer"]}]}}
    app = make_app(erp=MockERP(today=clock().date()), cfg=cfg)
    llm = FakeLLM()
    app.assistant.client_factory = lambda c: llm
    c = TestClient(create_app(app))
    H = lambda t: {"Authorization": f"Bearer {t}"}
    assert c.put("/api/v1/settings/module/assistant", json={"set": {"enabled": True}}, headers=H("view")).status_code == 403
    assert c.put("/api/v1/settings/module/assistant", json={"set": {"enabled": True}}, headers=H("owner")).json()["effective"]["enabled"] is True
    assert c.put("/api/v1/settings/module/assistant", json={"set": {"base_url": "http://10.0.0.9:11434"}}, headers=H("owner")).status_code == 422
    assert c.get("/api/v1/settings/module/nonsense", headers=H("view")).status_code == 404
    app.inventory.run()
    rec = app.approvals.list(status="pending")[0]
    assert c.post("/api/v1/assistant/ask", json={"approval_id": rec.id, "question": "why"}, headers=H("view")).status_code == 403
    assert c.post("/api/v1/assistant/ask", json={"approval_id": "nope", "question": "why"}, headers=H("owner")).status_code == 404
    assert c.post("/api/v1/assistant/ask", json={"approval_id": rec.id, "question": " "}, headers=H("owner")).status_code == 422
    llm.replies.append({"answerable": False, "answer": "", "facts_used": []})
    r = c.post("/api/v1/assistant/ask", json={"approval_id": rec.id, "question": "why"}, headers=H("owner")).json()
    assert r["status"] == "not_enough_information" and r["answer"] == NOT_ENOUGH
    assert len(c.get(f"/api/v1/assistant/history/{rec.id}", headers=H("owner")).json()) == 1

"""Per-person rate limits (Phase 3 of the hardening brief): one limiter for the application, a refusal that says how many seconds to wait (and the HTTP header that says it to a
program), a count and a log line, and a limit on every action that starts a language-model task, which until now only the chat had."""
import hashlib
import hmac
import json
import logging

import pytest
from fastapi.testclient import TestClient

from conftest import FakeHTTP
from erp_agents.core import metrics
from erp_agents.core.api.app import create_app
from erp_agents.core.approvals.channels.simple import InboxChannel
from erp_agents.core.approvals.channels.whatsapp import WhatsAppChannel
from erp_agents.core.erp.mock import MockERP
from erp_agents.core.resilience import RateLimited
from erp_agents.packs.stock_procurement.assistant.chat import ChatLimit

OWNER_KEY, ANA_KEY, BEN_KEY = "owner-key-0123456789abcdefghij", "ana-key-0123456789abcdefghijkl", "ben-key-0123456789abcdefghijkl"
TOKENS = {"auth": {"tokens": [{"user": "owner", "token": OWNER_KEY, "roles": ["admin", "approver"]}, {"user": "ana", "token": ANA_KEY, "roles": ["viewer"]},
                             {"user": "ben", "token": BEN_KEY, "roles": ["viewer"]}]}}


class Time:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def build(make_app, clock, cfg=None):
    app = make_app(erp=MockERP(today=clock().date()), cfg={**TOKENS, **(cfg or {})})
    app.settings.update_section(__import__("conftest").OWNER, "agents", {"enabled": True})
    app.agents.submit = lambda plan: "task-1"                                                   # the model-backed actions start work in a thread: not what is tested here
    app.agents.brief_async = lambda who: {"task_id": "task-2", "status": "running"}
    app.agents.review_async = lambda approval_id, who, force=False: {"task_id": "task-3", "status": "running"}
    app.assistant.ask = lambda approval_id, question, actor: {"status": "answered", "answer": "x"}
    ticks = Time()
    app.limits.limiter.clock = ticks
    return app, TestClient(create_app(app)), ticks


@pytest.fixture
def web(make_app, clock):
    return build(make_app, clock)


def chat(client, key, text="hello"):
    return client.post("/api/v1/chat", json={"question": text}, headers=auth(key))


# ------------------------------------------------------------------ the chat
def test_the_twenty_first_question_in_a_minute_is_refused_with_the_wait_in_words_and_in_a_header(web):
    app, client, ticks = web
    for _ in range(20):
        assert chat(client, ANA_KEY).status_code == 200
        ticks.now += 1
    refused = chat(client, ANA_KEY)
    assert refused.status_code == 429
    wait = int(refused.headers["retry-after"])
    assert 1 <= wait <= 60 and refused.json()["detail"] == f"That is a lot of questions in a minute. Please wait {wait} second{'s' if wait != 1 else ''}."
    ticks.now += wait + 1
    assert chat(client, ANA_KEY).status_code == 200                                              # the wait it named was enough


def test_another_person_is_not_held_up_by_someone_elses_questions(web):
    app, client, ticks = web
    for _ in range(21):
        chat(client, ANA_KEY)
    assert chat(client, ANA_KEY).status_code == 429 and chat(client, BEN_KEY).status_code == 200


def test_the_limit_is_the_one_in_the_settings(make_app, clock):
    app, client, ticks = build(make_app, clock, {"resilience": {"chat_per_minute": 3}})
    assert [chat(client, ANA_KEY).status_code for _ in range(4)] == [200, 200, 200, 429]


# ------------------------------------------------------------------ the actions that start a language-model task
MODEL_ACTIONS = [("post", "/api/v1/assistant/ask", {"approval_id": "abc", "question": "why?"}, OWNER_KEY),
                 ("post", "/api/v1/agents/ask", {"question": "which vendor is best for BOLT-M8?"}, ANA_KEY),
                 ("post", "/api/v1/agents/brief", None, OWNER_KEY),
                 ("post", "/api/v1/agents/reviews/abc", None, OWNER_KEY)]


@pytest.mark.parametrize("method, path, body, key", MODEL_ACTIONS)
def test_an_action_that_starts_a_model_task_is_limited_to_six_a_minute(web, method, path, body, key):
    app, client, ticks = web
    answers = [client.request(method, path, json=body, headers=auth(key)).status_code for _ in range(7)]
    assert answers[:6] == [answers[0]] * 6 and answers[0] in (200, 404, 409) and answers[6] == 429, answers
    refused = client.request(method, path, json=body, headers=auth(key))
    assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1 and "Please wait" in refused.json()["detail"]
    ticks.now += 61
    assert client.request(method, path, json=body, headers=auth(key)).status_code != 429


def test_the_model_limit_is_one_count_for_all_four_actions_and_leaves_the_chat_alone(web):
    app, client, ticks = web
    for _ in range(3):
        client.post("/api/v1/agents/brief", headers=auth(OWNER_KEY))
    for _ in range(3):
        client.post("/api/v1/agents/reviews/abc", headers=auth(OWNER_KEY))
    assert client.post("/api/v1/assistant/ask", json={"approval_id": "abc", "question": "why?"}, headers=auth(OWNER_KEY)).status_code == 429          # six used between them
    assert chat(client, OWNER_KEY, "How much BOLT-M8 do we have?").status_code == 200                                                    # a question the rules answer is not held up


def test_an_agent_question_counts_against_both_the_chat_and_the_model_limit(web):
    app, client, ticks = web
    codes = [client.post("/api/v1/agents/ask", json={"question": "which vendor is best for BOLT-M8?"}, headers=auth(ANA_KEY)).status_code for _ in range(8)]
    assert codes.count(429) == 2                                                                  # the model limit (6) comes before the chat limit (20)


# ------------------------------------------------------------------ the count and the line
def test_a_refusal_is_counted_and_logged_without_saying_who(web, caplog):
    app, client, ticks = web
    metrics.REGISTRY.reset()
    metrics.configure(True)
    caplog.set_level(logging.INFO, logger="erp_agents.calls")
    try:
        for _ in range(22):
            chat(client, ANA_KEY)
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("rate_limited")]
        assert len(lines) == 2 and all(l.startswith("rate_limited scope=chat wait_seconds=") and "ana" not in l for l in lines)
        assert metrics.RATE_LIMITED.values[("chat",)] == 2
    finally:
        metrics.configure(False)
        metrics.REGISTRY.reset()


# ------------------------------------------------------------------ a chat on WhatsApp or Telegram
WA_SECRET, PHONE = "wa-app-secret", "919800000000"


def wa_say(client, n, text="how much BOLT-M8 do we have?", sender=PHONE):
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [{"from": sender, "id": f"wamid.{sender}.{n}", "type": "text", "text": {"body": text}}]}}]}]}).encode()
    sig = "sha256=" + hmac.new(WA_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


@pytest.fixture
def phones(make_app, clock):
    http = FakeHTTP()
    channels = {"whatsapp": WhatsAppChannel("TOKEN", "PNID", app_secret=WA_SECRET, verify_token="vt", http=http), "inbox": InboxChannel()}
    cfg = {"approvals": {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "whatsapp", "address": PHONE}]},
                                                             {"id": "second", "name": "Second", "roles": ["approver"], "channels": [{"type": "whatsapp", "address": "919811111111"}]}]},
           "auth": {"tokens": []}}
    app = make_app(erp=MockERP(today=clock().date()), channels=channels, cfg=cfg)
    ticks = Time()
    app.limits.limiter.clock = ticks
    return app, http, TestClient(create_app(app)), ticks


def replies(http, number):
    return [c[1]["text"]["body"] for c in http.calls if c[1] and c[1].get("type") == "text" and c[1].get("to") == number]


def test_the_seventh_message_from_one_phone_in_a_minute_gets_a_polite_stop_with_the_wait(phones):
    app, http, client, ticks = phones
    for n in range(7):
        wa_say(client, n)
        ticks.now += 1
    texts = replies(http, PHONE)
    assert len(texts) == 7 and "Please wait" in texts[-1] and "seconds." in texts[-1] and "a little" not in texts[-1]
    assert all("Please wait" not in t for t in texts[:6])
    ticks.now += 60
    wa_say(client, 8)
    assert "Please wait" not in replies(http, PHONE)[-1]


def test_one_phone_cannot_use_up_anothers_messages(phones):
    app, http, client, ticks = phones
    for n in range(7):
        wa_say(client, n)
    wa_say(client, 1, sender="919811111111")
    assert "Please wait" not in replies(http, "919811111111")[-1]


# ------------------------------------------------------------------ one limiter
def test_the_two_private_limiters_are_gone_and_the_old_exception_is_the_same_kind_as_the_new(web):
    app, _, _ = web
    assert not hasattr(app.chat, "_asked") and not hasattr(app.conversation, "_asked")
    assert issubclass(ChatLimit, RateLimited) and str(ChatLimit("chat", 12.2)) == "That is a lot of questions in a minute. Please wait 13 seconds."
    assert app.chat.limits is app.limits and app.conversation.limits is app.limits


def test_the_helper_that_restates_an_answer_counts_as_a_model_action_too(make_app, clock):
    """`explain` is a call to the language model: after six in a minute it is refused by the model limit, long before the twenty questions of the chat."""
    from conftest import OWNER
    from erp_agents.core.assistant.llm import LLMError
    app = make_app(erp=MockERP(today=clock().date()))
    app.settings.update_section(OWNER, "assistant", {"enabled": True})

    class Down:
        model = "stub"

        def chat(self, system, user, schema):
            raise LLMError("the model is not running")
    app.chat.client_factory = lambda cfg: Down()
    for _ in range(6):
        app.chat.explain("How much BOLT-M8 do we have?", "ana", False)
    with pytest.raises(ChatLimit) as refused:
        app.chat.explain("How much BOLT-M8 do we have?", "ana", False)
    assert refused.value.scope == "model" and app.chat.ask("hello", "ana", False)["status"] == "help"           # the chat itself is not held up

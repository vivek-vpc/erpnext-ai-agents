"""The circuit breaker where it matters: the ERPNext client, the sign-in, the model clients, and what each part of the application does while a dependency is down
(Phase 3 of the hardening brief). Every test counts the requests that really left, which is what a dead server would have to answer one by one."""
import pytest
import requests
from fastapi.testclient import TestClient

from conftest import OWNER, VIEWER
from erp_agents.core import resilience
from erp_agents.core.agents.llm import AnthropicToolClient, OllamaToolClient
from erp_agents.core.api.app import create_app
from erp_agents.core.assistant.llm import LLMError as AssistantLLMError, OllamaClient
from erp_agents.core.auth.erpnext import ErpNextLogin, LoginError
from erp_agents.core.erp.base import PurchaseOrderDraft
from erp_agents.core.erp.erpnext import ERPNextClient
from erp_agents.core.httpguard import CircuitOpen
from test_resilience_guard import Net, Time

DEAD = requests.ConnectionError("connection refused")
KEY = "owner-key-0123456789abcdefghij"


@pytest.fixture
def ticks():
    t = Time()
    resilience.configure(resilience.ResilienceSettings(), clock=t)
    return t


def erp_over(net, **kw):
    erp = ERPNextClient("https://erp.example.test", "K", "S", **kw)
    erp.session.mount("https://", net)
    return erp


# ------------------------------------------------------------------ ERPNext
def test_a_dead_erpnext_is_asked_five_times_and_then_not_at_all(ticks):
    net = Net(DEAD)
    erp = erp_over(net)
    for _ in range(5):
        with pytest.raises(requests.ConnectionError):
            erp._list("Item", ["name"], [])
    for _ in range(20):
        with pytest.raises(CircuitOpen):
            erp._list("Item", ["name"], [])
    assert net.sent == 5


def test_the_health_check_says_no_at_once_while_it_is_open(ticks):
    net = Net(DEAD)
    erp = erp_over(net)
    assert [erp.healthcheck() for _ in range(5)] == [False] * 5 and net.sent == 5
    assert [erp.healthcheck() for _ in range(5)] == [False] * 5 and net.sent == 5


@pytest.mark.parametrize("failure, counts", [(requests.ReadTimeout("slow"), True), (503, True), (502, True), (404, False), (403, False), (409, False), (429, False)])
def test_slow_and_failing_answers_count_and_a_missing_or_refused_record_does_not(ticks, failure, counts):
    net = Net(failure)
    erp = erp_over(net)
    for _ in range(8):
        try:
            erp._list("Item", ["name"], [])
        except Exception:                                                           # noqa: BLE001 - what the caller sees is not the point here
            pass
    assert (net.sent == 5) is counts and (net.sent == 8) is (not counts)


def test_a_creation_is_refused_before_anything_is_sent_while_it_is_open(ticks):
    net = Net(DEAD)
    erp = erp_over(net, company="Test Co")
    draft = PurchaseOrderDraft("S-A", "BOLT", 10, 1.0, "Nos", __import__("datetime").date(2026, 11, 1), reference="prop1")
    for _ in range(5):
        erp.create_purchase_order(draft)
    sent = net.sent
    result = erp.create_purchase_order(draft)
    assert not result.ok and "ERPNext is not answering" in result.error and net.sent == sent


# ------------------------------------------------------------------ the sign-in
def test_signing_in_while_erpnext_is_down_stops_asking_it_and_says_it_cannot_be_reached(ticks):
    net = Net(DEAD)

    def session():
        s = requests.Session()
        s.mount("https://", net)
        return s
    login = ErpNextLogin("https://erp.example.test", session_factory=session)
    for _ in range(12):
        with pytest.raises(LoginError) as refused:
            login.start("priya@example.test", "secret")
        assert refused.value.code == "unreachable"
    assert net.sent == 5


def test_the_sign_in_and_the_client_share_what_they_learn_about_erpnext(ticks):
    net = Net(DEAD)
    erp = erp_over(net)
    for _ in range(5):
        with pytest.raises(requests.ConnectionError):
            erp._list("Item", ["name"], [])
    other = requests.Session()
    other.mount("https://", Net(200))
    login = ErpNextLogin("https://erp.example.test", session_factory=lambda: other)
    with pytest.raises(LoginError) as refused:
        login.start("priya@example.test", "secret")
    assert refused.value.code == "unreachable"                                      # the client's failures closed the door for the sign-in too


# ------------------------------------------------------------------ the model
def ollama(net, **kw):
    client = OllamaToolClient("http://127.0.0.1:11434", "llama3.1", **kw)
    client.session.mount("http://", net)
    return client


def test_a_model_server_that_is_down_is_tried_three_times_and_then_left_alone(ticks):
    net = Net(DEAD)
    client = ollama(net)
    from erp_agents.core.agents.llm import LLMError
    for _ in range(3):
        with pytest.raises(LLMError, match="could not reach the model"):
            client.chat([{"role": "user", "content": "hi"}])
    for _ in range(10):
        with pytest.raises(LLMError, match="is not answering"):
            client.chat([{"role": "user", "content": "hi"}])
    assert net.sent == 3


def test_check_connection_goes_out_even_while_open_and_a_good_answer_closes_it(ticks):
    net = Net(DEAD, DEAD, DEAD, 200)
    client = ollama(net)
    from erp_agents.core.agents.llm import LLMError
    for _ in range(3):
        with pytest.raises(LLMError):
            client.chat([{"role": "user", "content": "hi"}])
    assert resilience.BREAKERS.get("model:ollama").state == resilience.OPEN
    reply = client.status()                                                          # a person pressed "Check connection" a moment after restarting the server
    assert reply["reachable"] is True and net.sent == 4
    assert resilience.BREAKERS.get("model:ollama").state == resilience.CLOSED


def test_check_connection_that_fails_keeps_it_open_and_does_not_hold_up_the_others(ticks):
    net = Net(DEAD)
    client = ollama(net)
    from erp_agents.core.agents.llm import LLMError
    for _ in range(3):
        with pytest.raises(LLMError):
            client.chat([{"role": "user", "content": "hi"}])
    assert client.status()["reachable"] is False and net.sent == 4
    with pytest.raises(LLMError, match="is not answering"):
        client.chat([{"role": "user", "content": "hi"}])
    assert net.sent == 4


def test_the_assistants_own_ollama_client_shares_the_breaker_of_the_same_server(ticks):
    net = Net(DEAD)
    agent_side = ollama(net)
    helper = OllamaClient("http://127.0.0.1:11434", "llama3.1")
    helper.session.mount("http://", Net(200))
    from erp_agents.core.agents.llm import LLMError
    for _ in range(3):
        with pytest.raises(LLMError):
            agent_side.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(AssistantLLMError, match="is not answering"):
        helper.chat("system", "hi", {})


def test_a_hosted_providers_quota_answer_never_opens_it_but_an_outage_does(ticks, monkeypatch):
    monkeypatch.setattr("erp_agents.core.agents.llm.time.sleep", lambda s: None)
    from erp_agents.core.agents.llm import LLMError
    quota = Net(429)
    claude = AnthropicToolClient("https://api.anthropic.com", "claude-test", "sk-ant-test-key-0123456789")
    claude.session.mount("https://", quota)
    for _ in range(6):
        with pytest.raises(LLMError, match="rate limiting"):
            claude.chat([{"role": "user", "content": "hi"}])
    assert quota.sent == 18 and resilience.BREAKERS.get("model:anthropic").state == resilience.CLOSED
    outage = Net(503)
    claude2 = AnthropicToolClient("https://api.anthropic.com", "claude-test", "sk-ant-test-key-0123456789")
    claude2.session.mount("https://", outage)
    with pytest.raises(LLMError):
        claude2.chat([{"role": "user", "content": "hi"}])                            # three tries in one call: three failures in a row
    assert outage.sent == 3
    with pytest.raises(LLMError, match="is not answering"):
        claude2.chat([{"role": "user", "content": "hi"}])
    assert outage.sent == 3


# ------------------------------------------------------------------ what the application does meanwhile
@pytest.fixture
def down(make_app, ticks):
    net = Net(DEAD)
    erp = erp_over(net)
    app = make_app(erp=erp)
    return app, net


def test_the_assistant_never_invents_a_figure_while_erpnext_is_down_and_stops_asking_it(down):
    app, net = down
    answers = [app.chat.ask("How much BOLT-M8 do we have?", "ana", True) for _ in range(8)]
    assert all(a["status"] == "error" and "I could not read the data just now" in a["answer"] and "Nothing was changed" in a["answer"] for a in answers)
    assert not any(ch.isdigit() for a in answers[:5] for ch in a["answer"].split("(")[0]), "no figure may appear"
    sent_after_five = net.sent
    app.chat.ask("How much BOLT-M8 do we have?", "ana", True)
    assert net.sent == sent_after_five                                               # open: nothing leaves
    assert "ERPNext is not answering" in answers[-1]["answer"]


def test_the_stock_check_is_recorded_as_failed_at_once_and_says_why(down):
    app, net = down
    for _ in range(6):
        report = app.inventory.run()
        assert report.failed
    sent = net.sent
    again = app.inventory.run()
    assert again.failed and net.sent == sent and any("ERPNext is not answering" in e for e in again.errors)


def test_the_signed_in_health_answer_names_the_state_of_each_dependency(down):
    app, net = down
    app.tokens[KEY] = OWNER
    client = TestClient(create_app(app))
    body = client.get("/health", headers={"Authorization": f"Bearer {KEY}"}).json()
    assert body["erp_reachable"] is False and body["dependencies"] == {"erpnext": "closed"}
    for _ in range(5):
        app.erp.healthcheck()
    body = client.get("/health", headers={"Authorization": f"Bearer {KEY}"}).json()
    assert body["dependencies"] == {"erpnext": "open"}
    assert client.get("/health").json() == {"ok": True}                              # anybody else still gets the plain answer


def test_the_agents_fall_back_to_the_rules_while_the_model_is_down_and_stop_asking_it(make_app, ticks):
    pytest.importorskip("mcp")
    app = make_app()
    app.settings.update_section(OWNER, "agents", {"enabled": True})
    net = Net(DEAD)
    app.agents.client_factory = lambda cfg: ollama(net)
    results = [app.agents.ask("How much BOLT-M8 do we have?", VIEWER) for _ in range(6)]
    assert all(r["source"] == "rules" and r["status"] == "unavailable" and r["answer"] == "The AI assistant is not available just now." for r in results)     # honest, nothing made up
    assert net.sent == 3                                                             # the first three tried; the other three never left
    assert "could not reach the model" in results[0]["note"] and "is not answering" in results[-1]["note"]


def test_the_person_is_told_in_a_sentence_not_in_the_name_of_an_exception(down):
    """Found by asking a real server whose ERPNext was down: the answer began "(CircuitOpen: ERPNext is not answering ...". The message is already a sentence for people."""
    app, net = down
    answers = [app.chat.ask("How much BOLT-M8 do we have?", "ana", True)["answer"] for _ in range(8)]
    assert "CircuitOpen" not in answers[-1] and "(ERPNext is not answering" in answers[-1] and "Nothing was changed" in answers[-1]
    assert "ConnectionError" in answers[0]                                                   # an ordinary failure still names what it was, for whoever reads the log

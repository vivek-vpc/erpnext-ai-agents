"""A language model that is down, slow, or sends back rubbish (Phase 3 of the hardening brief). Whatever it does, the application must say so plainly, give the plain-rules answer
or none, and never invent a figure, raise to the person, or take a wrong action. Three levels: the client (one HTTP exchange), the agents, and the chat with its "Ask why" helper."""
import json

import pytest
import requests
from requests.adapters import BaseAdapter

from conftest import OWNER, VIEWER
from erp_agents.core import resilience
from erp_agents.core.agents.llm import AnthropicToolClient, GeminiToolClient, LLMError, OllamaToolClient, OpenAIToolClient
from erp_agents.core.assistant.llm import OllamaClient
from erp_agents.core.erp.mock import MockERP


class ModelServer(BaseAdapter):
    """A model server that answers from a script: an exception to raise, or (status, body). Records the timeout each request was sent with."""

    def __init__(self, *script):
        super().__init__()
        self.script, self.sent, self.timeouts = list(script), 0, []

    def send(self, request, **kwargs):
        self.sent += 1
        self.timeouts.append(kwargs.get("timeout"))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, BaseException):
            raise step
        status, body = step
        r = requests.Response()
        r.status_code, r._content, r.url, r.request = status, body if isinstance(body, bytes) else json.dumps(body).encode(), request.url, request
        r.headers["Content-Type"] = "application/json"
        return r


def said(content="", **extra):
    """What Ollama sends for one reply."""
    return 200, {"message": {"role": "assistant", "content": content, **extra}, "prompt_eval_count": 12, "eval_count": 5}


def ollama(server, **kw):
    client = OllamaToolClient("http://127.0.0.1:11434", "llama3.1", **kw)
    client.session.mount("http://", server)
    return client


HELLO = [{"role": "user", "content": "hello"}]
OPEN_CIRCUITS = pytest.fixture(autouse=True)(lambda: resilience.configure(resilience.ResilienceSettings(model_failures=100)))      # these tests are about the answers, not the breaker


# ------------------------------------------------------------------ the client: down, slow, an error
def test_a_server_that_refuses_the_connection_is_said_plainly():
    with pytest.raises(LLMError, match="could not reach the model at http://127.0.0.1:11434"):
        ollama(ModelServer(requests.ConnectionError("connection refused"))).chat(HELLO)


@pytest.mark.parametrize("slow", [requests.ReadTimeout("the model took too long"), requests.ConnectTimeout("no answer")])
def test_a_model_that_is_too_slow_is_an_error_not_a_wait_for_ever(slow):
    with pytest.raises(LLMError, match="could not reach the model"):
        ollama(ModelServer(slow)).chat(HELLO)


@pytest.mark.parametrize("kw, expected", [({}, 300), ({"timeout": 7}, 7)])
def test_every_request_carries_the_configured_timeout(kw, expected):
    server = ModelServer(said("hi"))
    ollama(server, **kw).chat(HELLO)
    assert server.timeouts == [expected]
    helper = ModelServer((200, {"message": {"content": "{}"}}))
    assistant = OllamaClient("http://127.0.0.1:11434", "llama3.1")
    assistant.session.mount("http://", helper)
    assistant.chat("system", "user", {})
    assert helper.timeouts == [240]


@pytest.mark.parametrize("status, body", [(500, b"internal error"), (503, b"<html>overloaded</html>"), (404, {"error": "model 'llama3.1' not found"}), (401, b"")])
def test_an_error_from_the_server_is_reported_with_its_status(status, body):
    with pytest.raises(LLMError, match=f"HTTP {status}"):
        ollama(ModelServer((status, body))).chat(HELLO)


# ------------------------------------------------------------------ the client: rubbish that is not a reply
@pytest.mark.parametrize("body", [b"", b"null", b"[]", b"<html><body>502 Bad Gateway</body></html>", b'{"message": {"role": "assistant", "content": "abc', b'{"nomessage": 1}',
                                  b'{"message": "just text"}', b'{"message": [1, 2]}', b'{"message": null}', b"\xff\xfe\x00 not text at all"])
def test_a_body_that_is_not_a_reply_is_an_error_and_never_an_exception_of_another_kind(body):
    with pytest.raises(LLMError, match="unexpected response"):
        ollama(ModelServer((200, body))).chat(HELLO)


# ------------------------------------------------------------------ the client: a reply with odd parts
@pytest.mark.parametrize("message, calls", [
    ({"tool_calls": "oops"}, []), ({"tool_calls": {"function": {"name": "stock_status"}}}, []), ({"tool_calls": [1, "x", None]}, []), ({"tool_calls": [{"function": "stock_status"}]}, []),
    ({"tool_calls": [{"function": {"name": 5, "arguments": {}}}]}, [("5", {})]), ({"tool_calls": [{"function": {"name": ""}}]}, []),
    ({"tool_calls": [{"function": {"name": "stock_status", "arguments": "{not json"}}]}, [("stock_status", {})]),
    ({"tool_calls": [{"function": {"name": "stock_status", "arguments": ["BOLT-M8"]}}]}, [("stock_status", {})]),
    ({"tool_calls": [{"function": {"name": "stock_status", "arguments": '{"item_code": "BOLT-M8"}'}}]}, [("stock_status", {"item_code": "BOLT-M8"})]),
    ({"tool_calls": [{"function": {"name": "stock_status", "arguments": {"item_code": "BOLT-M8"}}}, 7]}, [("stock_status", {"item_code": "BOLT-M8"})]),
    ({"content": None}, []), ({"content": 42}, []), ({"content": ["a"]}, []),
])
def test_odd_parts_of_a_reply_are_dropped_or_made_harmless_and_never_crash_the_client(message, calls):
    reply = ollama(ModelServer(said(**message))).chat(HELLO)
    assert reply.tool_calls == calls and isinstance(reply.content, str)


@pytest.mark.parametrize("counts", [{"prompt_eval_count": "many", "eval_count": None}, {"prompt_eval_count": [1], "eval_count": {"x": 1}}, {"prompt_eval_count": -5, "eval_count": 1e99}])
def test_token_counts_that_are_not_numbers_are_zero_and_never_an_error(counts):
    status, body = said("hi")
    reply = ollama(ModelServer((status, {**body, **counts}))).chat(HELLO)
    assert reply.prompt_tokens >= 0 and reply.output_tokens >= 0 and reply.content == "hi"


@pytest.mark.parametrize("client_class, url, shape", [
    (OpenAIToolClient, "https://api.openai.com", {"choices": "none"}), (OpenAIToolClient, "https://api.openai.com", {"choices": []}), (OpenAIToolClient, "https://api.openai.com", [1]),
    (OpenAIToolClient, "https://api.openai.com", {"choices": [{"message": "just text"}]}), (OpenAIToolClient, "https://api.openai.com", {"choices": [{"message": None}]}),
    (AnthropicToolClient, "https://api.anthropic.com", {"content": "none"}), (AnthropicToolClient, "https://api.anthropic.com", {"content": [1, 2]}),
    (GeminiToolClient, "https://generativelanguage.googleapis.com", {"candidates": "none"}), (GeminiToolClient, "https://generativelanguage.googleapis.com", {"candidates": []}),
])
def test_the_hosted_clients_turn_a_reply_of_the_wrong_shape_into_an_error_too(client_class, url, shape, monkeypatch):
    monkeypatch.setattr("erp_agents.core.agents.llm.time.sleep", lambda s: None)
    client = client_class(url, "model", "sk-test-key-0123456789")
    client.session.mount("https://", ModelServer((200, shape)))
    with pytest.raises(LLMError):
        client.chat(HELLO)


# ------------------------------------------------------------------ the agents
@pytest.fixture
def world(make_app, clock):
    pytest.importorskip("mcp")
    app = make_app(erp=MockERP(today=clock().date()))
    app.settings.update_section(OWNER, "agents", {"enabled": True})
    return app


def ask_with(app, *script):
    server = ModelServer(*script)
    app.agents.client_factory = lambda cfg: ollama(server)
    return app.agents.ask("How much BOLT-M8 do we have?", VIEWER), server


@pytest.mark.parametrize("script", [(requests.ConnectionError("down"),), (requests.ReadTimeout("slow"),), ((500, b"error"),), ((200, b"<html>not a model</html>"),), ((200, b'{"message": '),)])
def test_an_agent_question_to_a_model_that_is_down_slow_or_sending_rubbish_gets_the_honest_answer(world, script):
    result, server = ask_with(world, *script)
    assert result["source"] == "rules" and result["status"] == "unavailable" and result["answer"] == "The AI assistant is not available just now." and server.sent == 1
    assert "BOLT-M8" not in result["answer"] and not any(ch.isdigit() for ch in result["answer"])                         # nothing invented


def test_a_model_that_answers_in_prose_instead_of_the_format_is_rejected_twice_and_the_rules_answer(world):
    result, server = ask_with(world, said(""), said("Sure! BOLT-M8 is plentiful, about 300 units, no need to worry."))
    assert result["source"] == "rules" and result["status"] != "answered" and "300" not in result["answer"]
    assert server.sent >= 2


def test_a_model_that_asks_for_a_tool_in_a_broken_way_is_not_a_crash(world):
    result, server = ask_with(world, said("", tool_calls="oops"), said(json.dumps({"answerable": False, "answer": "I do not have enough information."})))
    assert result["source"] in ("rules", "model") and result["status"] in ("unavailable", "fallback", "not_enough_information", "answered") and "Traceback" not in str(result)


def test_a_model_that_asks_for_a_tool_that_does_not_exist_or_with_wrong_arguments_is_refused_by_the_tool_layer(world):
    odd = said("", tool_calls=[{"function": {"name": "drop_all_tables", "arguments": {}}}, {"function": {"name": "prepare_order_request", "arguments": {"item_code": "BOLT-M8", "qty": 5}}}])
    result, _ = ask_with(world, odd, said(json.dumps({"answerable": False, "answer": "x"})))
    assert world.erp.purchase_orders == [] and world.approvals.list("pending") == []                                             # nothing was ordered or even asked for
    rows = [r["tool"] for r in world.agent_log.tool_calls(limit=20)]
    assert "drop_all_tables" not in rows or all(r["ok"] == 0 for r in world.agent_log.tool_calls(limit=20) if r["tool"] == "drop_all_tables")


# ------------------------------------------------------------------ the chat and the "Ask why" helper
@pytest.fixture
def chat_world(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    app.settings.update_section(OWNER, "assistant", {"enabled": True})
    return app


def helper_with(app, *script):
    server = ModelServer(*script)

    def factory(cfg):
        client = OllamaClient("http://127.0.0.1:11434", "llama3.1")
        client.session.mount("http://", server)
        return client
    app.chat.client_factory = factory
    return server


@pytest.mark.parametrize("script", [(requests.ConnectionError("down"),), (requests.ReadTimeout("slow"),), ((500, b"error"),), ((200, b"<html>no</html>"),), ((200, {"message": {"content": 123}}),),
                                    ((200, {"message": {"content": "not json at all"}}),), ((200, {"message": {"content": json.dumps({"intent": "drop table"})}}),),
                                    ((200, {"message": {"content": json.dumps({"intent": 5})}}),), ((200, {"message": {"content": json.dumps(["x"])}}),)])
def test_a_question_the_rules_cannot_place_never_fails_because_the_helper_model_misbehaves(chat_world, script):
    helper_with(chat_world, *script)
    answer = chat_world.chat.ask("what about the thingummy over there, you know?", "ana", False)
    assert answer["status"] in ("help", "not_enough_information") and "Traceback" not in json.dumps(answer)                  # the helper failing is never shown as an error
    assert answer.get("routed_by_ai") is None                                                       # rubbish does not become a route


def test_restating_an_answer_with_a_helper_that_sends_rubbish_says_not_enough_information(chat_world):
    helper_with(chat_world, (200, {"message": {"content": "{\"answer\": \"BOLT-M8 has 99999 pcs\"}"}}))
    out = chat_world.chat.explain("How much BOLT-M8 do we have?", "ana", False)
    assert "99999" not in json.dumps(out)                                                           # a figure that is not in the data is never passed on


@pytest.mark.parametrize("content", [123, ["a"], {"a": 1}, None, True])
def test_the_helper_client_does_not_pass_on_a_reply_that_is_not_text(content):
    client = OllamaClient("http://127.0.0.1:11434", "llama3.1")
    client.session.mount("http://", ModelServer((200, {"message": {"content": content}})))
    with pytest.raises(LLMError, match="unexpected response"):
        client.chat("system", "user", {})


@pytest.mark.parametrize("body", [b"", b"null", b"[]", b'{"message": "x"}', b'{"message": null}', b'{"message": [1]}', b"<html>no</html>"])
def test_the_helper_client_turns_a_body_that_is_not_a_reply_into_an_error(body):
    client = OllamaClient("http://127.0.0.1:11434", "llama3.1")
    client.session.mount("http://", ModelServer((200, body)))
    with pytest.raises(LLMError, match="unexpected response"):
        client.chat("system", "user", {})

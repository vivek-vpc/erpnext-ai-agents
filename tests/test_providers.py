"""Choosing where the language model runs: Ollama on this computer, or Claude, OpenAI or Gemini; the keys; and the tokens a hosted model uses.

No network and no keys are needed: a recording session stands in for each provider, so these tests show exactly what is sent and how the replies are read."""
import json
import os
import sqlite3

import pytest

pytest.importorskip("mcp", reason="the optional 'agents' extra is not installed")

import erp_agents.core.agents.llm as llm_module
from conftest import OWNER, VIEWER
from erp_agents.packs.stock_procurement.agents.inventory_agent import rules_brief
from erp_agents.core.agents.llm import AnthropicToolClient, GeminiToolClient, LLMError, OllamaToolClient, OpenAIToolClient, Reply, describe, make_client
from erp_agents.core.agents.runtime import AgentRuntime
from erp_agents.core.agents.usage import estimated_cost
from erp_agents.core.config import LLM_KEY_ENV, llm_key_sources_from_env, llm_keys_from_env, load_config, with_secrets
from ai_munimji_engine.core.policy import PROVIDER_MODELS, PROVIDER_URLS, AgentSettings
from erp_agents.core.storage import Database
from erp_agents.core.erp.mock import MockERP
from test_agents import FakeToolLLM, overview, say, use, with_model

SECRET = "sk-test-SECRET-1234567890"


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class Session:
    """Records every request and answers from a script of (status, payload) pairs or exceptions."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def _next(self):
        item = self.script.pop(0) if self.script else (200, {})
        if isinstance(item, Exception):
            raise item
        return Resp(*item)

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        self.calls.append({"method": "POST", "url": url, "body": json, "headers": headers, **kw})
        return self._next()

    def get(self, url, headers=None, timeout=None, **kw):
        self.calls.append({"method": "GET", "url": url, "body": None, "headers": headers, **kw})
        return self._next()


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(llm_module, "RETRY_SLEEP", 0)


CONVERSATION = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "U1"},
                {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "stock_status", "arguments": {"item_code": "A-1"}}}]},
                {"role": "tool", "tool_name": "stock_status", "content": '{"on_hand": 5}'},
                {"role": "user", "content": "Now answer"}]
TOOL = {"name": "stock_status", "description": "d", "parameters": {"type": "object", "properties": {"item_code": {"type": "string", "description": "code"}}, "required": ["item_code"]}}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer"], "properties": {"answer": {"type": "string", "maxLength": 50}}}


# ------------------------------------------------------------------ the setting
def test_every_provider_has_its_own_address_and_suggested_model_and_blank_means_that():
    for provider in ("ollama", "anthropic", "openai", "gemini"):
        s = AgentSettings(provider=provider, allow_remote=True)
        assert s.effective_base_url() == PROVIDER_URLS[provider] and s.effective_model() == PROVIDER_MODELS[provider]
    s = AgentSettings(provider="openai", allow_remote=True, base_url="https://llm.example.com/v1/", model="my-model")
    assert s.effective_base_url() == "https://llm.example.com/v1" and s.effective_model() == "my-model"


def test_a_hosted_provider_needs_the_owners_consent_and_says_why():
    for provider, label in (("anthropic", "Claude"), ("openai", "OpenAI"), ("gemini", "Gemini")):
        with pytest.raises(ValueError, match=f"{label}.*hosted service.*allow_remote"):
            AgentSettings(provider=provider)
        assert AgentSettings(provider=provider, allow_remote=True).provider == provider
    assert AgentSettings(provider="ollama").allow_remote is False                                        # the default needs no consent: nothing leaves this computer


def test_an_openai_compatible_service_on_this_computer_needs_no_consent_but_a_remote_one_must_use_https():
    assert AgentSettings(provider="openai", base_url="http://127.0.0.1:8000/v1").effective_base_url() == "http://127.0.0.1:8000/v1"
    with pytest.raises(ValueError, match="https"):
        AgentSettings(provider="openai", base_url="http://llm.example.com/v1", allow_remote=True)
    with pytest.raises(ValueError, match="localhost"):
        AgentSettings(provider="ollama", base_url="https://llm.example.com")                            # an Ollama elsewhere still needs the switch
    with pytest.raises(ValueError):
        AgentSettings(provider="bing")
    with pytest.raises(ValueError):
        AgentSettings(price_input_per_million=-1)


def test_an_api_key_cannot_be_a_setting():
    with pytest.raises(ValueError):
        AgentSettings(api_key="sk-abc")


# ------------------------------------------------------------------ the keys
def test_keys_come_from_the_environment_only_and_google_has_two_names(monkeypatch):
    for names in LLM_KEY_ENV.values():
        for n in names:
            monkeypatch.delenv(n, raising=False)
    assert llm_keys_from_env() == {}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  a-key  ")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    assert llm_keys_from_env() == {"anthropic": "a-key", "gemini": "g-key"}
    monkeypatch.setenv("GEMINI_API_KEY", "g2")
    assert llm_keys_from_env()["gemini"] == "g2"
    monkeypatch.setenv("ERP_AGENTS_ANTHROPIC_API_KEY", "own-key")                                         # a key made for this application wins over the one set for another tool
    assert llm_keys_from_env()["anthropic"] == "own-key"
    assert llm_key_sources_from_env() == {"anthropic": "ERP_AGENTS_ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
    monkeypatch.delenv("ERP_AGENTS_ANTHROPIC_API_KEY")
    monkeypatch.setenv("ERP_AGENTS_MCP_TOKEN", "tools-token")
    cfg = load_config(os.path.join(os.path.dirname(__file__), "no-such-file.yaml"))
    assert cfg["llm_keys"]["anthropic"] == "a-key" and cfg["mcp"]["client_token"] == "tools-token"
    assert with_secrets({"mcp": {"http": True}})["mcp"] == {"http": True, "client_token": "tools-token"}


def test_a_hosted_provider_without_its_key_says_what_to_add():
    for provider, variable in (("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY"), ("gemini", "GEMINI_API_KEY")):
        cfg = AgentSettings(provider=provider, allow_remote=True)
        with pytest.raises(LLMError, match=f"{variable}.*\\.env"):
            make_client(cfg, {})
        info = describe(cfg, {})
        assert info["hosted"] and info["remote"] and info["key_configured"] is False and info["key_variable"] == variable
        assert describe(cfg, {provider: SECRET})["key_configured"] is True and SECRET not in json.dumps(describe(cfg, {provider: SECRET}))
    assert isinstance(make_client(AgentSettings(), {}), OllamaToolClient)                               # a model on this computer needs no key
    assert describe(AgentSettings(), {})["key_configured"] is None and describe(AgentSettings(), {})["hosted"] is False


def test_an_openai_protocol_server_on_this_computer_needs_no_key_and_is_not_hosted():
    cfg = AgentSettings(provider="openai", base_url="http://127.0.0.1:11434/v1", model="llama3.1")
    info = describe(cfg, {})
    assert info["hosted"] is False and info["remote"] is False and info["key_configured"] is None
    session = Session((200, {"data": [{"id": "llama3.1:latest"}]}), (200, {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}))
    client = make_client(cfg, {}, session)
    assert client.hosted is False and client.remote is False
    assert client.status()["model_installed"] is True                                                    # Ollama lists the model as llama3.1:latest
    client.chat(CONVERSATION[:2])
    assert "Authorization" not in session.calls[1]["headers"]
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        make_client(AgentSettings(provider="openai", base_url="https://llm.example.com/v1", allow_remote=True), {})                  # a remote one still needs its key


def test_the_client_matches_the_provider():
    kinds = {"anthropic": AnthropicToolClient, "openai": OpenAIToolClient, "gemini": GeminiToolClient}
    for provider, cls in kinds.items():
        client = make_client(AgentSettings(provider=provider, allow_remote=True, model="m-1"), {provider: SECRET})
        assert isinstance(client, cls) and client.model == "m-1" and client.remote is True and client.hosted is True


# ------------------------------------------------------------------ OpenAI
def openai(session, model="gpt-4o-mini"):
    return OpenAIToolClient("https://api.openai.com/v1", model, SECRET, session=session, max_output_tokens=400)


def test_openai_gets_the_conversation_the_tools_and_the_schema_it_expects():
    s = Session((200, {"choices": [{"message": {"content": '{"answer": "x"}'}}], "usage": {"prompt_tokens": 120, "completion_tokens": 30}}))
    reply = openai(s).chat(CONVERSATION, tools=[TOOL], schema=SCHEMA)
    call = s.calls[0]
    assert call["url"] == "https://api.openai.com/v1/chat/completions" and call["headers"]["Authorization"] == f"Bearer {SECRET}"
    body = call["body"]
    assert body["model"] == "gpt-4o-mini" and body["temperature"] == 0 and body["max_completion_tokens"] == 400 and "max_tokens" not in body
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["messages"][2] == {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "stock_status", "arguments": '{"item_code": "A-1"}'}}]}
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "call_1", "content": '{"on_hand": 5}'} and body["messages"][4]["role"] == "user"
    assert body["tools"][0] == {"type": "function", "function": {"name": "stock_status", "description": "d", "parameters": TOOL["parameters"]}}
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert (reply.content, reply.prompt_tokens, reply.output_tokens) == ('{"answer": "x"}', 120, 30)


def test_openai_tool_calls_are_read_and_a_thinking_model_gets_no_temperature_and_room_to_think():
    s = Session((200, {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "function": {"name": "stock_status", "arguments": '{"item_code": "B-2"}'}},
                                                                                 {"id": "c2", "function": {"name": "open_orders", "arguments": "not json"}}]}}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}))
    reply = openai(s, "gpt-5-mini").chat(CONVERSATION[:2], tools=[TOOL])
    assert reply.tool_calls == [("stock_status", {"item_code": "B-2"}), ("open_orders", {})] and reply.content == ""
    assert "temperature" not in s.calls[0]["body"] and s.calls[0]["body"]["max_completion_tokens"] == 2400


def test_openai_errors_are_plain_and_never_contain_the_key():
    cases = [(401, "refused the API key"), (403, "refused the API key"), (404, "does not know the model 'gpt-4o-mini'"), (400, "HTTP 400"), (429, "rate limiting or out of quota")]
    for status, words in cases:
        s = Session(*[(status, {"error": {"message": f"bad key {SECRET}"}})] * 3)                              # rate limits are retried twice before giving up
        with pytest.raises(LLMError, match=words) as exc:
            openai(s).chat(CONVERSATION[:2])
        assert SECRET not in str(exc.value)
    import requests
    with pytest.raises(LLMError, match="could not reach OpenAI") as exc:
        openai(Session(requests.ConnectionError(f"failed with {SECRET}"))).chat(CONVERSATION[:2])
    assert SECRET not in str(exc.value)


def test_a_busy_provider_is_retried_twice_then_given_up_on():
    ok = (200, {"choices": [{"message": {"content": "fine"}}], "usage": {}})
    s = Session((529, "overloaded"), (503, "unavailable"), ok)
    assert openai(s).chat(CONVERSATION[:2]).content == "fine" and len(s.calls) == 3
    s = Session((503, "x"), (503, "x"), (503, "x"))
    with pytest.raises(LLMError, match="HTTP 503"):
        openai(s).chat(CONVERSATION[:2])
    assert len(s.calls) == 3
    s = Session((401, {}), ok)
    with pytest.raises(LLMError):
        openai(s).chat(CONVERSATION[:2])
    assert len(s.calls) == 1                                                                              # a refused key is not retried


def test_openai_status_lists_the_models_and_spends_nothing():
    s = Session((200, {"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-x"}]}))
    st = openai(s).status()
    assert st == {"reachable": True, "model_installed": True, "models": ["gpt-4o-mini", "gpt-x"]} and s.calls[0]["method"] == "GET" and s.calls[0]["url"].endswith("/models")
    assert openai(Session((200, {"data": [{"id": "other"}]}))).status()["model_installed"] is False
    bad = openai(Session((401, {}))).status()
    assert bad["reachable"] is False and "refused the API key" in bad["error"]


# ------------------------------------------------------------------ Anthropic
def claude(session, model="claude-haiku-5-5"):
    return AnthropicToolClient("https://api.anthropic.com", model, SECRET, session=session, max_output_tokens=400)


def test_claude_gets_a_system_prompt_alternating_turns_and_no_temperature():
    s = Session((200, {"content": [{"type": "text", "text": "hello"}], "usage": {"input_tokens": 50, "output_tokens": 9}}))
    reply = claude(s).chat(CONVERSATION, tools=[TOOL])
    call = s.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages" and call["headers"]["x-api-key"] == SECRET and call["headers"]["anthropic-version"] == "2023-06-01"
    body = call["body"]
    assert "temperature" not in body and body["max_tokens"] == 400 and body["system"] == "SYS" and body["model"] == "claude-haiku-5-5"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]                       # the tool result and the next request are one user turn
    assert body["messages"][1]["content"] == [{"type": "tool_use", "id": "call_1", "name": "stock_status", "input": {"item_code": "A-1"}}]
    assert body["messages"][2]["content"] == [{"type": "tool_result", "tool_use_id": "call_1", "content": '{"on_hand": 5}'}, {"type": "text", "text": "Now answer"}]
    assert body["tools"] == [{"name": "stock_status", "description": "d", "input_schema": TOOL["parameters"]}] and "tool_choice" not in body
    assert (reply.content, reply.prompt_tokens, reply.output_tokens) == ("hello", 50, 9)


def test_claude_is_made_to_answer_in_the_requested_shape_by_a_forced_tool_and_cached_tokens_are_counted():
    s = Session((200, {"content": [{"type": "tool_use", "id": "t1", "name": "answer", "input": {"answer": "42"}}],
                       "usage": {"input_tokens": 100, "output_tokens": 12, "cache_read_input_tokens": 40, "cache_creation_input_tokens": 10}}))
    client = claude(s)
    client.set_tools([TOOL])
    reply = client.chat(CONVERSATION, schema=SCHEMA)
    body = s.calls[0]["body"]
    assert json.loads(reply.content) == {"answer": "42"} and reply.tool_calls == []
    assert body["tool_choice"] == {"type": "tool", "name": "answer"}
    assert [t["name"] for t in body["tools"]] == ["stock_status", "answer"] and body["tools"][1]["input_schema"] == SCHEMA        # the API wants the tool already used in the conversation defined too
    assert (reply.prompt_tokens, reply.output_tokens) == (150, 12)


def test_claude_tool_requests_are_read():
    s = Session((200, {"content": [{"type": "text", "text": "Let me look."}, {"type": "tool_use", "id": "t1", "name": "stock_status", "input": {"item_code": "C-3"}}], "usage": {"input_tokens": 5, "output_tokens": 2}}))
    reply = claude(s).chat(CONVERSATION[:2], tools=[TOOL])
    assert reply.tool_calls == [("stock_status", {"item_code": "C-3"})]


def test_claude_errors_and_status():
    for status, words in ((401, "refused the API key"), (404, "does not know the model 'claude-haiku-5-5'"), (429, "rate limiting")):
        with pytest.raises(LLMError, match=words) as exc:
            claude(Session(*[(status, {"error": {"message": SECRET}})] * 3)).chat(CONVERSATION[:2])
        assert SECRET not in str(exc.value)
    s = Session((200, {"data": [{"id": "claude-haiku-5-5"}, {"id": "claude-sonnet-5-5"}]}))
    assert claude(s).status()["model_installed"] is True and s.calls[0]["url"].startswith("https://api.anthropic.com/v1/models") and s.calls[0]["headers"]["x-api-key"] == SECRET


# ------------------------------------------------------------------ Gemini
def gemini(session, model="gemini-2.5-flash"):
    return GeminiToolClient("https://generativelanguage.googleapis.com", model, SECRET, session=session, max_output_tokens=400)


def test_gemini_gets_contents_function_declarations_and_the_schema_in_its_own_terms():
    s = Session((200, {"candidates": [{"content": {"parts": [{"text": '{"answer": "x"}'}]}}], "usageMetadata": {"promptTokenCount": 80, "candidatesTokenCount": 11, "thoughtsTokenCount": 20}}))
    reply = gemini(s).chat(CONVERSATION, tools=[TOOL], schema=SCHEMA)
    call = s.calls[0]
    assert call["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent" and call["headers"]["x-goog-api-key"] == SECRET
    body = call["body"]
    assert body["systemInstruction"] == {"parts": [{"text": "SYS"}]} and body["generationConfig"]["responseMimeType"] == "application/json" and body["generationConfig"]["maxOutputTokens"] == 3200
    assert "temperature" not in body["generationConfig"]
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["contents"][1]["parts"] == [{"functionCall": {"name": "stock_status", "args": {"item_code": "A-1"}}, "thoughtSignature": "skip_thought_signature_validator"}]
    assert body["contents"][2]["parts"][0] == {"functionResponse": {"name": "stock_status", "response": {"on_hand": 5}}}
    assert body["contents"][2]["parts"][1] == {"text": "Now answer"} and "JSON schema" in body["contents"][2]["parts"][2]["text"] and '"answer"' in body["contents"][2]["parts"][2]["text"]
    decl = body["tools"][0]["functionDeclarations"][0]
    assert decl["name"] == "stock_status" and decl["parameters"] == {"type": "OBJECT", "properties": {"item_code": {"type": "STRING", "description": "code"}}, "required": ["item_code"]}
    assert (reply.content, reply.prompt_tokens, reply.output_tokens) == ('{"answer": "x"}', 80, 31)        # thinking tokens are billed as output


def test_gemini_schemas_lose_what_the_api_does_not_take():
    cleaned = GeminiToolClient.schema({"type": "object", "title": "T", "additionalProperties": False, "properties": {"a": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 9, "minLength": 1}},
                                                                                                                    "b": {"type": "integer", "default": 1, "enum": [1, 2]}}, "required": ["a"]})
    assert cleaned == {"type": "OBJECT", "properties": {"a": {"type": "ARRAY", "maxItems": 3, "items": {"type": "STRING"}}, "b": {"type": "INTEGER", "enum": [1, 2]}}, "required": ["a"]}


def test_gemini_function_calls_come_back_with_their_signatures_and_go_back_unchanged():
    parts = [{"text": "hmm", "thought": True}, {"functionCall": {"name": "stock_status", "args": {"item_code": "D-4"}}, "thoughtSignature": "SIG123"}]
    s = Session((200, {"candidates": [{"content": {"parts": parts}}], "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}}))
    reply = gemini(s).chat(CONVERSATION[:2], tools=[TOOL])
    assert reply.tool_calls == [("stock_status", {"item_code": "D-4"})] and reply.content == "" and reply.raw == parts          # the thought is not part of the answer
    again = [*CONVERSATION[:2], {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "stock_status", "arguments": {"item_code": "D-4"}}}], "raw": reply.raw},
             {"role": "tool", "tool_name": "stock_status", "content": "{}"}]
    s2 = Session((200, {"candidates": [{"content": {"parts": [{"text": "done"}]}}], "usageMetadata": {}}))
    gemini(s2).chat(again)
    assert s2.calls[0]["body"]["contents"][1] == {"role": "model", "parts": parts}                                                  # exactly as received, signature included


def test_gemini_with_no_answer_says_why_and_status_lists_models_that_can_generate():
    with pytest.raises(LLMError, match="no answer.*SAFETY"):
        gemini(Session((200, {"promptFeedback": {"blockReason": "SAFETY"}}))).chat(CONVERSATION[:2])
    with pytest.raises(LLMError, match="refused the API key"):
        gemini(Session((403, {"error": {"message": SECRET}}))).chat(CONVERSATION[:2])
    s = Session((200, {"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]}, {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]}]}))
    st = gemini(s).status()
    assert st["models"] == ["gemini-2.5-flash"] and st["model_installed"] is True and "x-goog-api-key" in s.calls[0]["headers"]
    assert gemini(Session(), "models/gemini-2.5-flash").model == "gemini-2.5-flash"


# ------------------------------------------------------------------ the agents on a hosted model
def hosted_app(make_app, clock, provider="anthropic", keys=None, **settings):
    app = make_app(erp=MockERP(today=clock().date()), cfg={"llm_keys": SECRET_KEYS if keys is None else keys})
    app.settings.update_section(OWNER, "agents", {"enabled": True, "provider": provider, "allow_remote": True, **settings})
    return app


SECRET_KEYS = {"anthropic": SECRET, "openai": SECRET, "gemini": SECRET}


def claude_brief_reply(app, tokens_in=950, tokens_out=60):
    brief = {"headline": rules_brief(overview(app))["headline"], "watch_outs": []}
    return (200, {"content": [{"type": "tool_use", "id": "t1", "name": "answer", "input": brief}], "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out, "cache_read_input_tokens": 10}})


def test_a_brief_on_claude_reports_the_tokens_it_used_and_records_them(make_app, clock, monkeypatch):
    app = hosted_app(make_app, clock, price_input_per_million=1.0, price_output_per_million=5.0)
    session = Session(claude_brief_reply(app))
    monkeypatch.setattr(llm_module.requests, "Session", lambda: session)
    res = app.agents.brief("owner")
    assert res["source"] == "model" and res["provider"] == "anthropic" and res["hosted"] is True and res["model"] == "claude-haiku-5-5"
    assert res["tokens"] == {"input": 960, "output": 60, "total": 1020, "hosted": True, "estimated_cost": 0.0013}            # (960 x 1 + 60 x 5) per million
    call = session.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages" and call["headers"]["x-api-key"] == SECRET and "temperature" not in call["body"]
    row = app.agent_log.get(res["task_id"])
    assert (row["provider"], row["remote"], row["prompt_tokens"], row["output_tokens"], row["status"]) == ("anthropic", 1, 960, 60, "ok")
    assert SECRET not in json.dumps(row, default=str) and SECRET not in json.dumps(res, default=str)


def test_a_local_model_reports_tokens_too_but_no_cost(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    app.settings.update_section(OWNER, "agents", {"enabled": True, "price_input_per_million": 1.0})
    with_model(app, say({"headline": rules_brief(overview(app))["headline"], "watch_outs": []}))
    res = app.agents.brief()
    assert res["tokens"]["hosted"] is False and res["tokens"]["estimated_cost"] is None and res["tokens"]["total"] == 150 and res["hosted"] is False


def test_with_no_price_entered_there_is_no_cost_estimate():
    s = AgentSettings()
    assert estimated_cost(1000, 1000, s) is None
    assert estimated_cost(1_000_000, 500_000, AgentSettings(price_input_per_million=3, price_output_per_million=15)) == 10.5


def test_a_hosted_provider_without_its_key_falls_back_to_the_rules_and_says_what_to_add(make_app, clock):
    app = hosted_app(make_app, clock, provider="openai", keys={})
    res = app.agents.brief()
    assert res["source"] == "rules" and res["status"] == "fallback" and "OPENAI_API_KEY" in res["note"] and res["priorities"]
    row = app.agent_log.get(res["task_id"])
    assert row["provider"] == "openai" and row["status"] == "fallback" and (row["prompt_tokens"] or 0) == 0
    status = app.agents.status(check_model=True)
    assert status["key_configured"] is False and status["key_variable"] == "OPENAI_API_KEY" and "OPENAI_API_KEY" in status["model_check"]["error"]


def test_the_monthly_token_limit_stops_a_hosted_model_but_not_the_rules(make_app, clock, monkeypatch):
    app = hosted_app(make_app, clock, monthly_token_limit=1000)
    session = Session(claude_brief_reply(app), claude_brief_reply(app))
    monkeypatch.setattr(llm_module.requests, "Session", lambda: session)
    first = app.agents.brief()
    assert first["source"] == "model" and first["tokens"]["total"] == 1020                              # the limit is checked before a task, so one task may cross it
    second = app.agents.brief()
    assert second["source"] == "rules" and "monthly limit of 1,000 tokens" in second["note"] and second["priorities"] and len(session.calls) == 1
    use = app.agents.usage()
    assert use["limit_reached"] is True and use["limit_used_percent"] == 102.0
    app.settings.update_section(OWNER, "agents", {"monthly_token_limit": 0})
    assert app.agents.brief()["source"] == "model"                                                      # 0 = no limit


def test_the_limit_counts_hosted_tokens_only_and_starts_again_each_month(make_app, clock):
    app = hosted_app(make_app, clock, monthly_token_limit=1000)
    local = app.agent_log.start_task("inventory", "brief", "llama3.1", False, provider="ollama")
    app.agent_log.finish_task(local, "ok", {}, prompt_tokens=50_000, output_tokens=9_000)
    assert app.agents._over_limit(app.settings.agents()) is False                                       # a model on this computer does not count against a hosted limit
    hosted = app.agent_log.start_task("inventory", "brief", "claude-haiku-5-5", True, provider="anthropic")
    app.agent_log.finish_task(hosted, "ok", {}, prompt_tokens=900, output_tokens=200)
    assert app.agents._over_limit(app.settings.agents()) is True
    clock.advance(days=20)                                                                              # 16 Oct + 20 days is November
    assert app.agents._over_limit(app.settings.agents()) is False


def test_usage_adds_up_by_model_and_keeps_local_and_hosted_apart(make_app, clock):
    app = hosted_app(make_app, clock, price_input_per_million=2.0, price_output_per_million=10.0, monthly_token_limit=10_000)
    for provider, model, remote, tin, tout in (("anthropic", "claude-haiku-5-5", True, 900, 100), ("anthropic", "claude-haiku-5-5", True, 600, 50), ("ollama", "llama3.1", False, 3000, 400)):
        t = app.agent_log.start_task("inventory", "brief", model, remote, provider=provider)
        app.agent_log.finish_task(t, "ok", {}, steps=1, llm_seconds=2.0, prompt_tokens=tin, output_tokens=tout)
    u = app.agents.usage(30)
    assert u["month_hosted"] == {"tasks": 2, "input": 1500, "output": 150, "total": 1650, "hosted": True, "estimated_cost": 0.0045}
    assert u["month_local"]["total"] == 3400 and u["month_local"]["estimated_cost"] is None
    assert u["monthly_token_limit"] == 10_000 and u["limit_used_percent"] == 16.5 and u["limit_reached"] is False and u["prices_entered"] is True
    by = {(r["provider"], r["model"]): r for r in u["by_model"]}
    assert by[("anthropic", "claude-haiku-5-5")]["total"] == 1650 and by[("anthropic", "claude-haiku-5-5")]["hosted"] is True and by[("ollama", "llama3.1")]["estimated_cost"] is None
    assert sum(d["input"] + d["output"] for d in u["daily"] if d["hosted"]) == 1650


def test_the_usage_page_and_the_status_never_show_the_key(make_app, clock):
    from fastapi.testclient import TestClient
    from erp_agents.core.api.app import create_app
    app = make_app(erp=MockERP(today=clock().date()), cfg={"llm_keys": SECRET_KEYS, "auth": {"tokens": [{"token": "v" * 40, "user": "viewer", "roles": ["viewer"]}, {"token": "o" * 40, "user": "owner", "roles": ["viewer", "approver", "admin"]}]}})
    c = TestClient(create_app(app))
    H = lambda t: {"Authorization": "Bearer " + t * 40}                                                 # noqa: E731
    assert c.put("/api/v1/settings/module/agents", json={"set": {"provider": "gemini"}}, headers=H("o")).status_code == 422          # consent is part of the same change
    ok = c.put("/api/v1/settings/module/agents", json={"set": {"provider": "gemini", "allow_remote": True, "enabled": True, "model": "gemini-2.5-flash"}}, headers=H("o"))
    assert ok.status_code == 200 and ok.json()["effective"]["provider"] == "gemini"
    assert c.put("/api/v1/settings/module/agents", json={"set": {"api_key": SECRET}}, headers=H("o")).status_code == 422             # a key cannot be typed into settings
    status = c.get("/api/v1/agents/status", headers=H("v")).json()
    assert status["provider"] == "gemini" and status["hosted"] is True and status["key_configured"] is True and status["key_variable"] == "GEMINI_API_KEY" and status["leaves_this_server"] is True
    claude = next(p for p in status["providers"] if p["id"] == "anthropic")
    assert claude["own_variable"] == "ERP_AGENTS_ANTHROPIC_API_KEY" and claude["key_source"] is None and next(p for p in status["providers"] if p["id"] == "ollama")["own_variable"] is None
    privacy = c.get("/api/v1/agents/privacy", headers=H("v")).json()
    assert privacy["provider_label"] == "Gemini (Google)" and privacy["data_leaves_this_server"] is True and "hosted service" in privacy["where_it_goes"]
    usage = c.get("/api/v1/agents/usage?days=7", headers=H("v"))
    assert usage.status_code == 200 and usage.json()["days"] == 7 and c.get("/api/v1/agents/usage", headers=H("x")).status_code == 401
    for name in ("status", "privacy", "usage", "tasks"):
        assert SECRET not in c.get(f"/api/v1/agents/{name}", headers=H("o")).text


def test_a_hosted_provider_is_refused_at_run_time_if_consent_is_missing_even_if_settings_were_tampered_with(make_app, clock, monkeypatch):
    app = hosted_app(make_app, clock)
    session = Session(claude_brief_reply(app))
    monkeypatch.setattr(llm_module.requests, "Session", lambda: session)
    tampered = app.settings.agents().model_copy(update={"allow_remote": False})                          # settings validation would refuse this; the run must too
    monkeypatch.setattr(app.settings, "agents", lambda: tampered)
    res = app.agents.brief()
    assert res["source"] == "rules" and "allow_remote" in res["note"] and session.calls == []             # nothing went to the hosted service


def test_switching_back_to_this_computer_sends_nothing_to_the_hosted_service(make_app, clock, monkeypatch):
    app = hosted_app(make_app, clock)
    hosted = Session(claude_brief_reply(app))
    monkeypatch.setattr(llm_module.requests, "Session", lambda: hosted)
    app.settings.update_section(OWNER, "agents", {"provider": "ollama", "allow_remote": False})
    app.agents.client_factory = lambda cfg: FakeToolLLM(say({"headline": rules_brief(overview(app))["headline"], "watch_outs": []}))
    res = app.agents.brief()
    assert res["source"] == "model" and res["provider"] == "ollama" and res["hosted"] is False and hosted.calls == []


# ------------------------------------------------------------------ plumbing
def test_what_a_provider_needs_handed_back_travels_with_the_tool_calls_but_is_not_stored(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    app.settings.update_section(OWNER, "agents", {"enabled": True})
    signed = Reply(tool_calls=[("stock_status", {"item_code": "BOLT-M8"})], raw=[{"functionCall": {"name": "stock_status", "args": {}}, "thoughtSignature": "SIG"}], seconds=1.0)
    llm = with_model(app, signed, Reply(content="done"), say({"answerable": False, "answer": ""}))
    res = app.agents.ask("How much BOLT-M8 do we have?", VIEWER)
    sent = [m for c in llm.calls for m in c["messages"] if m.get("tool_calls")]
    assert any(m.get("raw") for m in sent)                                                              # handed back to the client with the calls
    assert "thoughtSignature" not in json.dumps(app.agent_log.get(res["task_id"])["trace"])             # and kept out of the record


def test_the_runtime_tells_a_client_which_tools_exist(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    app.settings.update_section(OWNER, "agents", {"enabled": True})
    seen = []

    class Spy(FakeToolLLM):
        def set_tools(self, tools):
            seen.append([t["name"] for t in tools])
    app.agents.client_factory = lambda cfg: Spy(Reply(content="done"), say({"answerable": False, "answer": ""}))
    app.agents.ask("How much BOLT-M8 do we have?", VIEWER)
    assert seen and "stock_status" in seen[0] and "prepare_order_request" not in seen[0]
    assert AgentRuntime                                                                                  # imported for the type of the thing under test


@pytest.mark.sqlite_only
def test_an_existing_database_gets_the_new_column_and_keeps_its_rows(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE agent_tasks (id TEXT PRIMARY KEY, agent TEXT NOT NULL, task TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, model TEXT, "
                "remote INTEGER NOT NULL DEFAULT 0, steps INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0, llm_seconds REAL, prompt_tokens INTEGER, output_tokens INTEGER, "
                "approval_id TEXT, asked_by TEXT, request_text TEXT, result_json TEXT, note TEXT, trace_json TEXT)")
    old.execute("INSERT INTO agent_tasks(id, agent, task, status, started_at, model, prompt_tokens, output_tokens) VALUES ('old1','inventory','brief','ok','2026-10-01T00:00:00+00:00','llama3.1',500,40)")
    old.commit()
    old.close()
    db = Database(path)
    assert "provider" in {r["name"] for r in db.query("PRAGMA table_info(agent_tasks)")}
    assert db.query_one("SELECT prompt_tokens, provider FROM agent_tasks WHERE id='old1'")["prompt_tokens"] == 500
    Database(path)                                                                                      # opening it again changes nothing

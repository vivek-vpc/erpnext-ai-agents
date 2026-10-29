"""Talking to a language model that can call tools: one small client per provider, all with the same two methods, `chat` and `status`.

  ollama     a model on this computer (or another machine you run), over Ollama's /api/chat
  anthropic  Claude, over the Messages API
  openai     OpenAI (or any OpenAI-compatible service via base_url), over Chat Completions
  gemini     Google Gemini, over generateContent

The agents never need to know which model this is. `remote` says whether what is sent leaves this server (docs/PRIVACY.md); every reply says how many tokens it cost, which is what a
hosted provider bills for. An API key comes in from the environment through config.py, is sent only as a request header, and never appears in an error message or a log.

The runtime speaks one neutral message format (a system / user / assistant-with-tool_calls / tool conversation, like Ollama's) and each client translates it. A reply that must follow
a schema is asked for WITHOUT free tool use: small models do one or the other, and the hosted ones have their own ways to force a shape (below).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol
from urllib.parse import urlparse

import requests

from .. import obs, resilience
from ..assistant.llm import SEED, LLMError
from ..httpguard import guard_session
from ..netguard import no_redirects
from ..policy import LOCAL_HOSTS, PROVIDER_LABELS, PROVIDER_URLS, AgentSettings

__all__ = ["LLMError", "Reply", "ToolChatClient", "OllamaToolClient", "OpenAIToolClient", "AnthropicToolClient", "GeminiToolClient", "make_client", "describe", "is_remote",
           "salvage_tool_call", "KEY_ENV", "OFFICIAL_HOSTS", "key_for"]

KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}      # what the settings screen tells the owner to add to the .env file
RETRY_STATUSES = (429, 500, 502, 503, 504, 529)
RETRY_SLEEP = 2.0                                                                                          # seconds before the first retry, doubled for the second
# The only host each provider's API key is ever sent to (over https). A custom address (an OpenAI-compatible server, a proxy) never receives a key.
OFFICIAL_HOSTS = {p: (urlparse(u).hostname or "").lower() for p, u in PROVIDER_URLS.items() if p != "ollama"}


def is_official(provider: str, base_url: str) -> bool:
    """Is this the provider's own API address, over https?"""
    u = urlparse(base_url)
    return provider in OFFICIAL_HOSTS and u.scheme == "https" and (u.hostname or "").lower() == OFFICIAL_HOSTS[provider] and u.port in (None, 443)


def key_for(provider: str, base_url: str, keys: Optional[dict[str, str]]) -> str:
    """The key to attach for this address: the provider's key at its own address, nothing anywhere else (Ollama, a custom address)."""
    return (keys or {}).get(provider, "") if is_official(provider, base_url) else ""


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    load_seconds: float = 0.0               # loading the model into memory (Ollama, only after it was idle)
    prompt_seconds: float = 0.0             # reading the prompt (Ollama reports it)
    generate_seconds: float = 0.0           # writing the reply (Ollama reports it)
    raw: Any = None                         # what a provider needs handed back with the tool calls (Gemini's thought signatures)


class ToolChatClient(Protocol):
    model: str
    remote: bool

    def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None, schema: Optional[dict[str, Any]] = None) -> Reply: ...


def is_remote(base_url: str) -> bool:
    return (urlparse(base_url).hostname or "") not in LOCAL_HOSTS


def _plain(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in m.items() if k != "raw"} for m in messages]


# ------------------------------------------------------------------------------------------------------------------- what a reply may contain
# A model server, or something pretending to be one, can send any JSON. These keep a reply of the wrong shape from raising anything but LLMError, and drop the odd part of a
# good one (a tool call that is not a call, a token count that is not a number) instead of failing the whole reply.
def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _count(value: Any) -> int:
    """A token count: a whole number from 0 to a billion, and 0 for anything else."""
    if isinstance(value, bool):
        return 0
    try:
        return max(0, min(int(value), 10**9))
    except (TypeError, ValueError, OverflowError):
        return 0


def _seconds(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _call(name: Any, arguments: Any) -> Optional[tuple[str, dict[str, Any]]]:
    """One tool call from what a model sent: (name, arguments as a dict), or None when it is not a call. Arguments given as text are read as JSON; anything else is empty."""
    if not name or isinstance(name, (dict, list)):
        return None
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = {}
    return str(name), arguments if isinstance(arguments, dict) else {}


# ------------------------------------------------------------------------------------------------------------------- Ollama (this computer)
class OllamaToolClient:
    provider, hosted = "ollama", False

    def __init__(self, base_url: str, model: str, timeout: float = 300, session: Optional[Any] = None, num_ctx: int = 8192, max_output_tokens: int = 400) -> None:
        self.base_url, self.model, self.timeout, self.num_ctx, self.max_output_tokens = base_url.rstrip("/"), model, timeout, num_ctx, max_output_tokens
        self.session = session or guard_session(obs.observe_session(requests.Session(), "llm"), "model:ollama")      # the breaker is shared by everything that asks this server
        self.remote = is_remote(base_url)

    def set_tools(self, tools: list[dict[str, Any]]) -> None:
        pass

    def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None, schema: Optional[dict[str, Any]] = None) -> Reply:
        body: dict[str, Any] = {"model": self.model, "messages": _plain(messages), "stream": False, "keep_alive": "15m",
                                "options": {"temperature": 0, "seed": SEED, "top_k": 1, "top_p": 1, "num_ctx": self.num_ctx, "num_predict": self.max_output_tokens}}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tools]
        if schema:                                         # a reply that must follow a schema is asked for WITHOUT tools: small models do one or the other, not both
            body["format"] = schema
        t0 = time.monotonic()
        try:
            resp = self.session.post(f"{self.base_url}/api/chat", json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise LLMError(f"could not reach the model at {self.base_url}: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"the model server returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
            message = data["message"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMError("the model server sent an unexpected response") from exc
        if not isinstance(data, dict) or not isinstance(message, dict):
            raise LLMError("the model server sent an unexpected response")
        calls = [c for c in (_call(_dict(_dict(item).get("function")).get("name"), _dict(_dict(item).get("function")).get("arguments")) for item in _list(message.get("tool_calls"))) if c]
        secs = lambda key: _seconds(data.get(key)) / 1e9      # noqa: E731 - Ollama reports nanoseconds
        return Reply(content=_text(message.get("content")), tool_calls=calls, prompt_tokens=_count(data.get("prompt_eval_count")), output_tokens=_count(data.get("eval_count")),
                     seconds=time.monotonic() - t0, load_seconds=secs("load_duration"), prompt_seconds=secs("prompt_eval_duration"), generate_seconds=secs("eval_duration"))

    def status(self) -> dict[str, Any]:
        """Is the server up, and is the configured model installed?"""
        try:
            with resilience.manual_trial():                    # a person pressed Check connection: ask now, whatever the pause, and let the answer decide
                names = [m.get("name", "") for m in self.session.get(f"{self.base_url}/api/tags", timeout=10).json().get("models", [])]
        except (requests.RequestException, ValueError) as exc:
            return {"reachable": False, "model_installed": False, "error": str(exc)}
        return {"reachable": True, "model_installed": any(n == self.model or n.split(":")[0] == self.model or n == f"{self.model}:latest" for n in names), "models": names}


# ------------------------------------------------------------------------------------------------------------------- hosted providers
class HostedClient:
    """What the three hosted providers share: a key, an https address, retries on rate limits, and honest errors."""
    provider = ""
    hosted = True                                          # billed per token: only when the address is not on this computer (a vLLM on your own machine costs nothing)
    remote = True

    def __init__(self, base_url: str, model: str, api_key: str, timeout: float = 300, session: Optional[Any] = None, max_output_tokens: int = 400) -> None:
        official = is_official(self.provider, base_url)
        if not official:
            api_key = ""                                   # a key goes to the provider's own address only, whoever built this client
            if is_remote(base_url):
                raise LLMError(f"{KEY_ENV[self.provider]} is sent only to {OFFICIAL_HOSTS[self.provider]}, so another remote address cannot be used with it. Leave the address empty "
                               "to use the provider itself, or use an OpenAI-compatible server on this computer (it needs no key).")
        elif not api_key:
            raise LLMError(f"No API key for {PROVIDER_LABELS[self.provider]}: add {KEY_ENV[self.provider]}=... to the .env file next to the config folder and restart the Stock Assistant.")
        self.base_url, self.model, self.api_key, self.timeout, self.max_output_tokens = base_url.rstrip("/"), model, api_key, timeout, max_output_tokens
        target = urlparse(self.base_url)
        self._origin = (target.scheme, (target.hostname or "").lower(), target.port)
        self.session = session or guard_session(obs.observe_session(no_redirects(requests.Session()), "llm"), f"model:{self.provider}")     # belt and braces: every request below also says allow_redirects=False
        self.remote = self.hosted = is_remote(base_url)
        self.tools_known: list[dict[str, Any]] = []

    def set_tools(self, tools: list[dict[str, Any]]) -> None:
        self.tools_known = list(tools)                     # some providers want the definitions of tools already used in the conversation, also when answering

    # -- plumbing
    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _clean(self, text: Any) -> str:
        return (str(text).replace(self.api_key, "***") if self.api_key else str(text))[:240]

    def _request(self, method: str, url: str, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        label = PROVIDER_LABELS[self.provider]
        target = urlparse(url)
        if (target.scheme, (target.hostname or "").lower(), target.port) != self._origin:      # the key header never goes to a second host
            raise LLMError(f"refused to send a request for {label} to another address")
        for attempt in range(3):
            try:
                resp = (self.session.post(url, json=body, headers=self._headers(), timeout=self.timeout, allow_redirects=False) if method == "POST"
                        else self.session.get(url, headers=self._headers(), timeout=30, allow_redirects=False))
            except requests.RequestException as exc:
                raise LLMError(f"could not reach {label}: {self._clean(exc)}") from exc
            if 300 <= resp.status_code < 400:                # never followed: a redirect would carry the key header to wherever it points
                raise LLMError(f"{label} answered with a redirect (HTTP {resp.status_code}). Redirects are not followed, so the API key is never sent anywhere else.")
            if resp.status_code in RETRY_STATUSES and attempt < 2:
                time.sleep(RETRY_SLEEP * (attempt + 1))
                continue
            if resp.status_code in (401, 403):
                raise LLMError(f"{label} refused the API key (HTTP {resp.status_code}). Check {KEY_ENV[self.provider]} in the .env file.")
            if resp.status_code == 404:
                raise LLMError(f"{label} does not know the model '{self.model}' (HTTP 404). Use Check connection to see the models this key can use.")
            if resp.status_code == 429:
                raise LLMError(f"{label} is rate limiting or out of quota (HTTP 429): {self._clean(resp.text)}")
            if resp.status_code >= 400:
                raise LLMError(f"{label} returned HTTP {resp.status_code}: {self._clean(resp.text)}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise LLMError(f"{label} sent a reply that is not JSON") from exc
            if not isinstance(data, dict):
                raise LLMError(f"{label} sent an unexpected reply")
            return data
        raise LLMError(f"{label} did not answer")

    def _models(self) -> list[str]:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        """Does the key work, and can it use the configured model? (Lists the models: no tokens are spent.)"""
        try:
            with resilience.manual_trial():                    # a person pressed Check connection: ask now, whatever the pause, and let the answer decide
                names = self._models()
        except LLMError as exc:
            return {"reachable": False, "model_installed": False, "error": str(exc)}
        return {"reachable": True, "model_installed": any(n in (self.model, f"models/{self.model}", f"{self.model}:latest") for n in names), "models": names}


class OpenAIToolClient(HostedClient):
    provider = "openai"
    REASONING = re.compile(r"^(o\d|gpt-5)", re.I)           # models that think first: they take no temperature and their hidden thinking counts against the reply limit

    def _headers(self) -> dict[str, str]:
        return {**({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}), "Content-Type": "application/json"}

    def _models(self) -> list[str]:
        data = self._request("GET", f"{self.base_url}/models")
        return sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))

    def _messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        pending: list[tuple[str, str]] = []
        n = 0
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                calls = []
                for c in m["tool_calls"]:
                    n += 1
                    calls.append({"id": f"call_{n}", "type": "function", "function": {"name": c["function"]["name"], "arguments": json.dumps(c["function"]["arguments"])}})
                pending = [(c["function"]["name"], c["id"]) for c in calls]
                out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": calls})
            elif m["role"] == "tool":
                match = next((p for p in pending if p[0] == m.get("tool_name")), pending[0] if pending else None)
                if match is None:
                    out.append({"role": "user", "content": "Result of " + str(m.get("tool_name")) + ": " + m["content"]})
                else:
                    pending.remove(match)
                    out.append({"role": "tool", "tool_call_id": match[1], "content": m["content"]})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None, schema: Optional[dict[str, Any]] = None) -> Reply:
        thinks = bool(self.REASONING.match(self.model))
        body: dict[str, Any] = {"model": self.model, "messages": self._messages(messages), "max_completion_tokens": max(self.max_output_tokens * 6, 2400) if thinks else self.max_output_tokens}
        if not thinks:
            body["temperature"] = 0
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}} for t in tools]
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": False}}
        t0 = time.monotonic()
        data = self._request("POST", f"{self.base_url}/chat/completions", body)
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("OpenAI sent an unexpected reply") from exc
        if not isinstance(message, dict):
            raise LLMError("OpenAI sent an unexpected reply")
        calls = [c for c in (_call(_dict(_dict(item).get("function")).get("name"), _dict(_dict(item).get("function")).get("arguments")) for item in _list(message.get("tool_calls"))) if c]
        usage = _dict(data.get("usage"))
        return Reply(content=_text(message.get("content")), tool_calls=calls, prompt_tokens=_count(usage.get("prompt_tokens")), output_tokens=_count(usage.get("completion_tokens")),
                     seconds=time.monotonic() - t0)


class AnthropicToolClient(HostedClient):
    provider = "anthropic"
    VERSION = "2023-06-01"
    ANSWER_TOOL = "answer"

    def _headers(self) -> dict[str, str]:
        return {**({"x-api-key": self.api_key} if self.api_key else {}), "anthropic-version": self.VERSION, "content-type": "application/json"}

    def _models(self) -> list[str]:
        data = self._request("GET", f"{self.base_url}/v1/models?limit=100")
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]

    def _messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Roles must alternate, so consecutive user turns (tool results, then the next request) are one message of several blocks, tool results first."""
        out: list[dict[str, Any]] = []
        pending: list[tuple[str, str]] = []
        n = 0

        def push(role: str, blocks: list[dict[str, Any]]) -> None:
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in messages:
            if m["role"] == "system":
                continue
            if m["role"] == "user":
                push("user", [{"type": "text", "text": m["content"]}])
            elif m["role"] == "assistant" and m.get("tool_calls"):
                blocks: list[dict[str, Any]] = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                pending = []
                for c in m["tool_calls"]:
                    n += 1
                    blocks.append({"type": "tool_use", "id": f"call_{n}", "name": c["function"]["name"], "input": c["function"]["arguments"]})
                    pending.append((c["function"]["name"], f"call_{n}"))
                push("assistant", blocks)
            elif m["role"] == "assistant":
                push("assistant", [{"type": "text", "text": m["content"] or "(no answer)"}])
            elif m["role"] == "tool":
                match = next((p for p in pending if p[0] == m.get("tool_name")), pending[0] if pending else None)
                if match is None:
                    push("user", [{"type": "text", "text": "Result of " + str(m.get("tool_name")) + ": " + m["content"]}])
                else:
                    pending.remove(match)
                    push("user", [{"type": "tool_result", "tool_use_id": match[1], "content": m["content"]}])
        return out

    def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None, schema: Optional[dict[str, Any]] = None) -> Reply:
        # No temperature: current Claude models reject any value but the default (HTTP 400).
        body: dict[str, Any] = {"model": self.model, "max_tokens": self.max_output_tokens, "messages": self._messages(messages)}
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        if system:
            body["system"] = system
        defs = tools or []
        used_tools = any(isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result") for m in body["messages"] for b in m["content"])
        if not defs and used_tools:
            defs = self.tools_known                        # the API wants every tool named in the conversation to be defined, also when only an answer is asked for
        tool_list = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"] if t["parameters"].get("type") == "object" else {"type": "object", "properties": {}}}
                     for t in defs]
        if schema:                                         # a reply that must follow a schema: force one call of a tool whose input IS the schema
            tool_list.append({"name": self.ANSWER_TOOL, "description": "Give the final answer in exactly this format.", "input_schema": schema})
            body["tool_choice"] = {"type": "tool", "name": self.ANSWER_TOOL}
        if tool_list:
            body["tools"] = tool_list
        t0 = time.monotonic()
        data = self._request("POST", f"{self.base_url}/v1/messages", body)
        text, calls, answer = [], [], None
        blocks = data.get("content") or []
        if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
            raise LLMError("Anthropic sent an unexpected reply")
        for block in blocks:
            if block.get("type") == "text":
                text.append(_text(block.get("text")))
            elif block.get("type") == "tool_use":
                if block.get("name") == self.ANSWER_TOOL:
                    answer = block.get("input") or {}
                elif block.get("name") and not isinstance(block.get("name"), (dict, list)):
                    calls.append((str(block["name"]), block.get("input") if isinstance(block.get("input"), dict) else {}))
        usage = _dict(data.get("usage"))
        tokens_in = _count(usage.get("input_tokens")) + _count(usage.get("cache_read_input_tokens")) + _count(usage.get("cache_creation_input_tokens"))
        return Reply(content=json.dumps(answer) if answer is not None else "".join(text), tool_calls=calls, prompt_tokens=tokens_in, output_tokens=_count(usage.get("output_tokens")),
                     seconds=time.monotonic() - t0)


class GeminiToolClient(HostedClient):
    provider = "gemini"
    SKIP_SIGNATURE = "skip_thought_signature_validator"      # Google's documented stand-in for a function call that this program wrote, not the model
    KEEP = ("type", "description", "enum", "items", "properties", "required", "minItems", "maxItems", "nullable", "format", "minimum", "maximum")

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.model = self.model.removeprefix("models/")

    def _headers(self) -> dict[str, str]:
        return {**({"x-goog-api-key": self.api_key} if self.api_key else {}), "Content-Type": "application/json"}

    def _models(self) -> list[str]:
        data = self._request("GET", f"{self.base_url}/v1beta/models?pageSize=200")
        return sorted(m["name"].removeprefix("models/") for m in data.get("models", []) if "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"]) and m.get("name"))

    @classmethod
    def schema(cls, node: Any) -> Any:
        """The subset of JSON Schema Gemini's function declarations accept: no additionalProperties, titles, defaults or string lengths; type names as the API spells them."""
        if isinstance(node, list):
            return [cls.schema(x) for x in node]
        if not isinstance(node, dict):
            return node
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key not in cls.KEEP:
                continue
            if key == "properties" and isinstance(value, dict):
                out[key] = {name: cls.schema(sub) for name, sub in value.items()}
            elif key == "type" and isinstance(value, str):
                out[key] = value.upper()
            else:
                out[key] = cls.schema(value) if key == "items" else value
        return out

    def _contents(self, messages: list[dict[str, Any]], schema: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        def push(role: str, parts: list[dict[str, Any]]) -> None:
            if out and out[-1]["role"] == role:
                out[-1]["parts"].extend(parts)
            else:
                out.append({"role": role, "parts": list(parts)})

        for m in messages:
            if m["role"] == "system":
                continue
            if m["role"] == "user":
                push("user", [{"text": m["content"]}])
            elif m["role"] == "assistant" and m.get("tool_calls"):
                push("model", m["raw"] if m.get("raw") else [{"functionCall": {"name": c["function"]["name"], "args": c["function"]["arguments"]}, "thoughtSignature": self.SKIP_SIGNATURE}
                                                           for c in m["tool_calls"]])
            elif m["role"] == "assistant":
                push("model", [{"text": m["content"] or "(no answer)"}])
            elif m["role"] == "tool":
                try:
                    result = json.loads(m["content"])
                except ValueError:
                    result = {"result": m["content"]}
                push("user", [{"functionResponse": {"name": m.get("tool_name"), "response": result if isinstance(result, dict) else {"result": result}}}])
        if schema and out and out[-1]["role"] == "user":
            out[-1]["parts"].append({"text": "Reply with JSON only, matching this JSON schema exactly: " + json.dumps(schema)})
        return out

    def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None, schema: Optional[dict[str, Any]] = None) -> Reply:
        # Thinking models spend part of the output limit thinking, so the limit is generous; temperature is left at Google's default (lowering it can make Gemini 3 loop).
        body: dict[str, Any] = {"contents": self._contents(messages, schema), "generationConfig": {"maxOutputTokens": max(self.max_output_tokens * 8, 3200)}}
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": [{"name": t["name"], "description": t["description"], "parameters": self.schema(t["parameters"])} for t in tools]}]
        if schema:
            body["generationConfig"]["responseMimeType"] = "application/json"
        t0 = time.monotonic()
        data = self._request("POST", f"{self.base_url}/v1beta/models/{self.model}:generateContent", body)
        candidates = data.get("candidates") or []
        if not isinstance(candidates, list) or not all(isinstance(c, dict) for c in candidates):
            raise LLMError("Gemini sent an unexpected reply")
        if not candidates:
            raise LLMError("Gemini gave no answer" + (f" ({_dict(data.get('promptFeedback')).get('blockReason')})" if _dict(data.get("promptFeedback")).get("blockReason") else ""))
        parts = [p for p in _list(_dict(candidates[0].get("content")).get("parts")) if isinstance(p, dict)]
        text = "".join(_text(p.get("text")) for p in parts if "text" in p and not p.get("thought"))
        calls = [c for c in (_call(_dict(p.get("functionCall")).get("name"), _dict(p.get("functionCall")).get("args")) for p in parts) if c]
        usage = _dict(data.get("usageMetadata"))
        return Reply(content=text, tool_calls=calls, prompt_tokens=_count(usage.get("promptTokenCount")),
                     output_tokens=_count(usage.get("candidatesTokenCount")) + _count(usage.get("thoughtsTokenCount")), seconds=time.monotonic() - t0,
                     raw=list(parts) if calls else None)


# ------------------------------------------------------------------------------------------------------------------- choosing the client
CLIENTS = {"anthropic": AnthropicToolClient, "openai": OpenAIToolClient, "gemini": GeminiToolClient}


def make_client(cfg: AgentSettings, keys: Optional[dict[str, str]] = None, session: Optional[Any] = None) -> ToolChatClient:
    """The client for the provider the owner chose. Raises LLMError (with what to do) when a hosted provider has no key. `session` is for tests: nothing else needs it."""
    base, model = cfg.effective_base_url(), cfg.effective_model()
    if cfg.provider == "ollama":
        return OllamaToolClient(base, model, cfg.timeout_seconds, session=session, max_output_tokens=cfg.max_output_tokens)
    return CLIENTS[cfg.provider](base, model, key_for(cfg.provider, base, keys), cfg.timeout_seconds, session=session, max_output_tokens=cfg.max_output_tokens)


def describe(cfg: AgentSettings, keys: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """What the owner is choosing, without contacting anything: provider, model, address, whether it is hosted, whether data leaves this server, whether the key is in place."""
    base = cfg.effective_base_url()
    remote = is_remote(base)
    hosted = cfg.provider != "ollama" and remote           # billed per token and out of your hands; an OpenAI-protocol server on this computer is neither
    return {"provider": cfg.provider, "provider_label": PROVIDER_LABELS[cfg.provider], "model": cfg.effective_model(), "base_url": base, "hosted": hosted, "remote": remote,
            "key_needed": hosted, "key_configured": bool((keys or {}).get(cfg.provider)) if hosted else None, "key_variable": KEY_ENV.get(cfg.provider),
            "key_sent": is_official(cfg.provider, base)}


def salvage_tool_call(content: str, names: set[str]) -> Optional[tuple[str, dict[str, Any]]]:
    """Some models write a tool call as plain JSON in the reply ({"name": "stock_status", "parameters": {...}}) instead of using the tool-call field."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("name") not in names:
        return None
    args = data.get("parameters", data.get("arguments", {}))
    return str(data["name"]), (args if isinstance(args, dict) else {})

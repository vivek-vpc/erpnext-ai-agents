"""Client for a local Ollama server. Settings that make the output repeatable: temperature 0, fixed seed, top_k 1,
and a JSON schema the reply must follow."""
from __future__ import annotations

import json
from typing import Any, Optional, Protocol

import requests

from .. import obs, resilience
from ..httpguard import guard_session

SEED = 42


class LLMError(RuntimeError):
    """The model could not be reached or did not reply (not the same as a reply that fails the checks)."""


class LLMClient(Protocol):
    model: str

    def chat(self, system: str, user: str, schema: dict[str, Any]) -> str: ...


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout: float = 240, session: Optional[Any] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.session = session or guard_session(obs.observe_session(requests.Session(), "llm"), "model:ollama")     # the same breaker as the agents' client for this server

    def chat(self, system: str, user: str, schema: dict[str, Any]) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": schema,
            "options": {"temperature": 0, "seed": SEED, "top_k": 1, "top_p": 1, "num_ctx": 8192},
            "keep_alive": "15m",
        }
        try:
            resp = self.session.post(f"{self.base_url}/api/chat", json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise LLMError(f"could not reach the local model at {self.base_url}: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"the local model returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()["message"]["content"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LLMError("the local model sent an unexpected response") from exc

    def status(self) -> dict[str, Any]:
        """Is the server up, and is the configured model installed?"""
        try:
            with resilience.manual_trial():                    # a person pressed Check connection: ask now, whatever the pause, and let the answer decide
                resp = self.session.get(f"{self.base_url}/api/tags", timeout=10)
            names = [m.get("name", "") for m in resp.json().get("models", [])]
        except (requests.RequestException, ValueError) as exc:
            return {"reachable": False, "model_installed": False, "error": str(exc)}
        installed = any(n == self.model or n.split(":")[0] == self.model or n == f"{self.model}:latest" for n in names)
        return {"reachable": True, "model_installed": installed, "models": names}


def dumps_schema(schema: dict[str, Any]) -> str:
    return json.dumps(schema, sort_keys=True)

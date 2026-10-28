"""A stand-in for the language model that says exactly what a case's script says, in order, and nothing else.

One script serves both ways the application talks to a model: the tool-calling agents (`chat(messages, tools, schema) -> Reply`) and the older single-shot helpers that route a
question or restate facts (`chat(system, user, schema) -> str`). A script is a list of replies, each exactly one of:

    tools: [[stock_status, {item_code: BOLT-M8}]]     the model asks for these tool calls
    say: "I have what I need."                        plain words (ends the tool-calling stage; as an answer it is not JSON)
    answer: {answerable: true, answer: "..."}         a JSON reply, written as data
    raw: "{not json"                                  these characters, exactly
    error: "could not reach the model"                the model cannot be reached

The order of the replies is part of the case: if the application asks for a reply the script does not have, or asks for a tool call at the moment it wants the final answer,
the case fails and says which step. A reply the application never asked for is also a failure (the script and the code have drifted apart).
"""
from __future__ import annotations

import copy
import json
from typing import Any, Callable, Optional

from erp_agents.core.agents.llm import Reply
from erp_agents.core.assistant.llm import LLMError

TOKENS = (120, 30)                  # what a scripted reply "costs" unless it says otherwise: only so that token counts in the report are not zero


class Script:
    def __init__(self, steps: list[dict[str, Any]], resolve: Optional[Callable[[Any], Any]] = None) -> None:
        self.steps = list(steps)
        self.resolve = resolve or (lambda x: x)
        self.position = 0
        self.problems: list[str] = []
        self.calls: list[dict[str, Any]] = []          # what the application asked the model: kept for --show and for the engine's own tests

    def take(self, interface: str, kind: str, tools_offered: bool, shown: Any) -> Optional[dict[str, Any]]:
        call: dict[str, Any] = {"interface": interface, "kind": kind, "shown": copy.deepcopy(shown), "step": None}
        self.calls.append(call)
        if self.position >= len(self.steps):
            self.problems.append(f"the application asked the model for a reply ({kind}, number {self.position + 1}) and the script has none left")
            return None
        step = self.steps[self.position]
        self.position += 1
        if "tools" in step and not tools_offered:
            self.problems.append(f"reply {self.position} of the script calls a tool, but the application asked for {kind} (no tools offered)")
            return None
        call["step"] = step = self.resolve(step)
        return step

    def unused(self) -> int:
        return len(self.steps) - self.position


def _reply(step: dict[str, Any]) -> Reply:
    tin, tout = step.get("tokens") or TOKENS
    if "tools" in step:
        return Reply(tool_calls=[(str(n), dict(a or {})) for n, a in step["tools"]], prompt_tokens=tin, output_tokens=tout)
    if "answer" in step:
        return Reply(content=json.dumps(step["answer"]), prompt_tokens=tin, output_tokens=tout)
    return Reply(content=str(step.get("raw", step.get("say", ""))), prompt_tokens=tin, output_tokens=tout)


class ScriptedToolLLM:
    """The tool-calling client (core/agents/llm.py ToolChatClient)."""
    model, remote, hosted, provider = "scripted-model", False, False, "ollama"

    def __init__(self, script: Script) -> None:
        self.script = script

    def set_tools(self, tools: list[dict[str, Any]]) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {"reachable": True, "model_installed": True}

    def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None, schema: Optional[dict[str, Any]] = None) -> Reply:
        last = (messages[-1].get("content") or "") if messages else ""
        kind = "gather" if tools else ("repair" if str(last).startswith("Your answer was rejected") else "answer")
        step = self.script.take("tools", kind, bool(tools), messages)
        if step is None:
            raise LLMError("the script is out of step with the application")
        if "error" in step:
            raise LLMError(str(step["error"]))
        return _reply(step)


class ScriptedTextLLM:
    """The single-shot client (core/assistant/llm.py LLMClient): routes a question, or restates facts."""
    model = "scripted-model"

    def __init__(self, script: Script) -> None:
        self.script = script

    def status(self) -> dict[str, Any]:
        return {"reachable": True, "model_installed": True}

    def chat(self, system: str, user: str, schema: dict[str, Any]) -> str:
        step = self.script.take("text", "single reply", False, {"system": system, "user": user})
        if step is None:
            raise LLMError("the script is out of step with the application")
        if "error" in step:
            raise LLMError(str(step["error"]))
        return _reply(step).content

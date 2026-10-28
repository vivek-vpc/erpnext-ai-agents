"""Run the golden cases that need a language model on a REAL model, and put two models side by side.

The cases and what they expect are the ones CI replays with a scripted stand-in (tests/evals/cases). Here the stand-in is replaced by Ollama or a hosted model, the world is the
demo ERP (fictional data: nothing of the company's is sent anywhere, even to a hosted model), and a case is judged on what the person would read, what was written and which
tools were refused, not on the route the model took. A case whose point is the scripted reply (a model that invents a number) has no meaning against a real one and is left out.

    python -m tools.e2e.agents_eval --suite golden --model llama3.1
    python -m tools.e2e.agents_eval --suite golden --provider anthropic --against ollama:llama3.1 --allow-remote --out compare.md
    python -m tools.e2e.agents_eval --suite golden --model llama3.1 --record recorded/     # keep what the model said as scripts in the case format
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from erp_agents.core.agents.llm import describe, make_client
from erp_agents.core.assistant.llm import LLMError
from erp_agents.core.config import load_config
from erp_agents.core.policy import AGENT_PROVIDERS, AgentSettings

from .cases import Case, load_cases
from .metrics import summarise
from .report import case_table, comparison
from .runner import Result, run_case

PRIVACY = ("The cases use the demo ERP's fictional data: nothing of your company's is sent. What the model reads is each case's question and the tool results it asks for.")


@dataclass(frozen=True)
class ModelSpec:
    provider: str = "ollama"
    model: str = ""                     # empty = the provider's suggested model
    base_url: str = ""                  # empty = the provider's own address

    def settings(self, allow_remote: bool, timeout: int) -> AgentSettings:
        """Choosing a hosted provider on the command line is the consent to send what the model reads to it; a model on another machine needs --allow-remote."""
        return AgentSettings(provider=self.provider, model=self.model, base_url=self.base_url, allow_remote=allow_remote or self.provider != "ollama", timeout_seconds=timeout)


def parse_spec(text: str, base_url: str = "") -> ModelSpec:
    """`provider:model`, or just a model name for Ollama (a tag such as llama3.2:3b is part of the name, because `llama3.2` is not a provider)."""
    provider, sep, model = text.partition(":")
    if sep and provider in AGENT_PROVIDERS:
        return ModelSpec(provider, model, base_url)
    return ModelSpec("ollama", text, base_url)


class TextAdapter:
    """The router and the checked restatements talk to a model with `chat(system, user, schema) -> str`; this lets any provider's client answer that way."""

    def __init__(self, client: Any) -> None:
        self.client, self.model = client, client.model

    def status(self) -> dict[str, Any]:
        return self.client.status() if hasattr(self.client, "status") else {"reachable": True, "model_installed": True}

    def chat(self, system: str, user: str, schema: dict[str, Any]) -> str:
        return self.client.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], None, schema).content


class RecordingClient:
    """Wraps a client and keeps what it said, reply by reply, in the form a case's `llm:` script is written in: a recorded transcript that CI can replay."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.steps = inner, []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def set_tools(self, tools: list[dict[str, Any]]) -> None:
        getattr(self.inner, "set_tools", lambda _t: None)(tools)

    def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None, schema: Optional[dict[str, Any]] = None) -> Any:
        try:
            reply = self.inner.chat(messages, tools, schema)
        except LLMError as exc:
            self.steps.append({"error": str(exc)[:200]})
            raise
        self.steps.append(step_of(reply))
        return reply


def step_of(reply: Any) -> dict[str, Any]:
    import json
    if reply.tool_calls:
        step: dict[str, Any] = {"tools": [[name, args] for name, args in reply.tool_calls]}
    else:
        text = (reply.content or "").strip()
        parsed = None
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        if isinstance(parsed, dict):
            step = {"answer": parsed}
        else:
            step = {"raw": reply.content} if text.startswith(("{", "[", "```")) else {"say": reply.content}
    if reply.prompt_tokens or reply.output_tokens:
        step["tokens"] = [reply.prompt_tokens, reply.output_tokens]
    return step


def slug(spec: ModelSpec) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (spec.model or spec.provider).lower()).strip("-") or "model"


def recorded(case: Case, spec: ModelSpec, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """The case as a new case that replays what the model said. The expectations are the original's: read the answer and correct them before relying on it."""
    data = case.model_dump(by_alias=True, exclude_defaults=True)
    data.pop("known_gap", None)
    data.update(id=f"{case.id}-recorded-{slug(spec)}", reply="good", llm=steps,
                note=f"Recorded from {spec.provider}:{spec.model or 'the default model'} on {date.today().isoformat()}. Read what it answered and correct the expectations before relying on it.")
    return data


def live_cases(cases: list[Case], groups: Optional[list[str]] = None, ids: Optional[list[str]] = None) -> list[Case]:
    return [c for c in cases if c.runs_live and (not groups or c.group in groups) and (not ids or c.id in ids)]


def run_model(cases: list[Case], spec: ModelSpec, keys: dict[str, str], allow_remote: bool, timeout: int, record_dir: Optional[Path] = None,
              client_for: Optional[Callable[[AgentSettings], Any]] = None, say: Callable[[str], None] = print) -> tuple[dict[str, Any], list[Result]]:
    settings = spec.settings(allow_remote, timeout)
    make = client_for or (lambda s: make_client(s, keys))
    results: list[Result] = []
    for case in cases:
        client = make(settings)
        if record_dir is not None:
            client = RecordingClient(client)
        t0 = time.monotonic()
        result = run_case(case, clients=(client, TextAdapter(client)))
        results.append(result)
        say(f"  {'right' if result.passed else 'WRONG':5} {case.id:55} {result.observed.outcome:11} {time.monotonic() - t0:6.0f} s"
            + ("" if result.passed else "".join(f"\n         - {f}" for f in result.failures[:3])))
        if record_dir is not None and isinstance(client, RecordingClient) and client.steps:
            record_dir.mkdir(parents=True, exist_ok=True)
            (record_dir / f"{case.id}.{slug(spec)}.yaml").write_text(yaml.safe_dump({"cases": [recorded(case, spec, client.steps)]}, sort_keys=False, allow_unicode=True, width=170), encoding="utf-8")
    return summarise(results), results


def golden(args: Any) -> int:
    """`python -m tools.e2e.agents_eval --suite golden ...`: returns the exit code."""
    import logging
    previous = logging.root.manager.disable
    logging.disable(logging.WARNING - 1)                  # a line at start-up for every world is noise here; the process gets its logging back afterwards
    try:
        return _golden(args)
    finally:
        logging.disable(previous)


def _golden(args: Any) -> int:
    cases = live_cases(load_cases(), args.group, args.case)
    if not cases:
        print("no case matches: only cases a real model can answer are run (python -m tools.evals --list shows them all)")
        return 2
    keys = load_config(args.config).get("llm_keys") or {}
    specs = [ModelSpec(args.provider, args.model, args.base_url)]
    if args.against:
        specs.append(parse_spec(args.against, args.against_base_url))
    labels, checked = [], []
    for spec in specs:
        try:
            settings = spec.settings(args.allow_remote, args.timeout)
        except ValueError as exc:
            print(exc.errors()[0]["msg"].removeprefix("Value error, ") if hasattr(exc, "errors") else str(exc))
            return 2
        info = describe(settings, keys)
        if info["hosted"] and not info["key_configured"]:
            print(f"No API key for {info['provider_label']}: add {info['key_variable']}=... to the .env file first.")
            return 2
        client = make_client(settings, keys)
        state = client.status() if hasattr(client, "status") else {"reachable": True, "model_installed": True}
        print(f"{info['provider_label']}, model {info['model']} at {info['base_url']}: {state}")
        if not state.get("reachable", True) or not state.get("model_installed", True):
            print("That model cannot be used: start the server, or install the model, and try again.")
            return 2
        labels.append(f"{info['model']} ({info['provider_label']})")
        checked.append(info["remote"])
    print(PRIVACY + (" At least one model is on another machine or hosted: what it reads leaves this computer." if any(checked) else ""))
    print(f"{len(cases)} cases: a real model can take minutes per case on a laptop.\n")
    rows, results_by_model = [], []
    for spec, label in zip(specs, labels):
        print(f"{label}:")
        summary, results = run_model(cases, spec, keys, args.allow_remote, args.timeout, Path(args.record) if args.record else None)
        rows.append((label, summary))
        results_by_model.append((label, results))
    text = comparison(rows) + "\n" + case_table(results_by_model)
    print("\n" + text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print("Report written to", args.out)
    return 0

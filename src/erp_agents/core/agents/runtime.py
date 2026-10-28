"""The loop every agent runs, and the checks that stand between a model's words and a person.

    1. warm start   - the code makes the obvious tool calls itself (a laptop model spends a minute on each step it takes, so it is not asked to find these)
    2. gather       - the model may call more tools (only the job's allowlist, within the step and call budgets) until it has what it needs
    3. answer       - the model writes the answer as JSON in the job's format, with no tools
    4. verify       - the answer must fit the format, use only numbers / item codes that the tools returned, not guess, and pass the job's own checks;
                      if not, the model gets one chance to fix exactly what was wrong
    5. otherwise    - the job's deterministic fallback is used and the record says so. A rejected answer never reaches a person.

The model never calculates, never orders, and has no tool that does. docs/AGENTS.md describes the design and its limits.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

import anyio

from .. import obs
from ..assistant.verify import HEDGES, _contains, numbers
from .llm import LLMError, Reply, ToolChatClient, salvage_tool_call

if TYPE_CHECKING:                                    # the MCP package is optional: only a running agent needs it
    from .bridge import ToolBridge

log = logging.getLogger(__name__)

CODE_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9]*(?:-[A-Z0-9]+)+\b")              # BOLT-M8, ELE-0025, PUR-ORD-2026-00543
# Words that claim a calculation or a comparison ("110 times the daily use", "more than twice"). Working things out is the program's job: a small model gets it wrong with real numbers
# in the sentence, which no check of the numbers alone can see. Allowed only when a tool's own result uses the word.
DERIVED = re.compile(r"\b(times|twice|thrice|double[sd]?|triple[sd]?|half|halved|ratio|multiples? of|more than|less than|fewer than|greater than|higher than|lower than|"
                     r"increased?|decreased?|grew|fell|rose|dropped|plus|minus|sum of|added to|subtracted)\b", re.I)
SUM_SIGNS = re.compile(r"\d[\d,.]*\s*[+\u00d7*\u00f7=]\s*\d")                                            # 120 + 3840, 4 x 5 = 20
FINAL_REQUEST = ("Now write your answer as JSON in exactly the requested format. Use only numbers, item codes and vendor names that appear in the tool results above, "
                 "copied exactly. Do not calculate anything yourself. If the results do not answer the request, say that in the answer instead of guessing.")


@dataclass
class AgentJob:
    agent: str                                           # inventory | purchasing
    task: str                                            # brief | review | ask
    system: str
    user: str
    tools: tuple[str, ...]                               # the allowlist: the only tools this job may use
    schema: dict[str, Any]                               # the answer's format (JSON schema)
    check: Callable[[dict[str, Any], "Evidence"], list[str]]       # problems with an answer that fits the format (none = fine)
    warm: tuple[tuple[str, dict[str, Any]], ...] = ()    # tool calls the code makes first
    gate: Optional[Callable[[str, dict[str, Any], "Evidence"], Optional[str]]] = None   # may refuse a tool call; returns the reason
    rewrite: Optional[Callable[[str, dict[str, Any]], dict[str, Any]]] = None   # the program's own say over a tool call's arguments (text a person will read is never the model's)
    question: str = ""                                   # what the person wrote (numbers and codes in it count as known)
    gather: bool = True                                  # False = no tool-calling steps: the warm start is all the model sees (on a slow machine every step costs minutes)
    after_warm: Optional[Callable[["Evidence"], None]] = None       # the program's own calculations from what the warm start fetched (added to the evidence, so the model may quote them)
    digest: Optional[Callable[["Evidence"], str]] = None            # what the model is shown INSTEAD of the raw tool results: short, because reading the prompt is most of the time on a CPU
    final: Any = FINAL_REQUEST                           # the last instruction before the answer: text, or a function of the evidence (to hand over checks the program has made)


@dataclass
class ToolRun:
    name: str
    args: dict[str, Any]
    result: dict[str, Any]
    ok: bool


class Evidence:
    """Everything the tools returned in this task: what an answer may be built from."""

    def __init__(self, question: str = "") -> None:
        self.question = question
        self.runs: list[ToolRun] = []
        self._pool: Optional[list[float]] = None
        self._text: Optional[str] = None

    def add(self, run: ToolRun) -> None:
        self.runs.append(run)
        self._pool = self._text = None

    def results(self, tool: str) -> list[dict[str, Any]]:
        return [r.result for r in self.runs if r.name == tool and r.ok]

    def pool(self) -> list[float]:
        """The numbers an answer may contain: those in the person's question and those in the RESULTS of calls that succeeded. A call's arguments never count, failed or not:
        a failed call returned nothing, so a number the model typed into it is the model's own; and for a call that succeeded, every argument that is a fact comes back in its
        result (a history tool returns the number of weeks it was asked for, a vendor comparison the quantity, an overview the number of rows it listed instead of the limit). Each pack
        pins that for its own tools in a test: tests/test_grounding_arguments.py does it for the stock pack."""
        if self._pool is None:
            vals: list[float] = list(numbers(self.question))
            for r in self.runs:
                if r.ok:
                    vals.extend(_leaf_numbers(r.result))
            self._pool = vals
        return self._pool

    def text(self) -> str:
        if self._text is None:
            self._text = (self.question + " " + " ".join(json.dumps(r.result, default=str) for r in self.runs if r.ok)).lower()
        return self._text


def _leaf_numbers(obj: Any) -> list[float]:
    if isinstance(obj, bool) or obj is None:
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, str):
        return numbers(obj)
    if isinstance(obj, dict):
        return [n for v in obj.values() for n in _leaf_numbers(v)]
    if isinstance(obj, (list, tuple)):
        return [n for v in obj for n in _leaf_numbers(v)]
    return []


def grounding_problems(texts: list[str], ev: Evidence) -> list[str]:
    """Numbers and item codes in the model's sentences that no tool returned, and guessing words. (A faithful rounding, 12 for 12.3, is not an invented number.)"""
    problems: list[str] = []
    joined = " ".join(t for t in texts if t)
    codes = set(CODE_RE.findall(joined))
    unknown_codes = sorted(c for c in codes if c.lower() not in ev.text())
    if unknown_codes:
        problems.append("item or document codes that no tool returned: " + ", ".join(unknown_codes))
    pool = ev.pool()
    invented = []
    for n in numbers(CODE_RE.sub(" ", joined)):
        if _contains(n, pool) or any(abs(n - round(p)) < 1e-9 or abs(n - round(p, 1)) < 1e-9 for p in pool):
            continue
        invented.append(n)
    if invented:
        problems.append("numbers that no tool returned: " + ", ".join(f"{n:g}" for n in dict.fromkeys(invented)))
    low = joined.lower()
    guesses = [h for h in HEDGES if re.search(rf"\b{re.escape(h)}\b", low) and h not in ev.text()]
    if guesses:
        problems.append("guessing words (" + ", ".join(guesses) + "): state only what the tools returned")
    derived = sorted({m.group(1).lower() for m in DERIVED.finditer(joined) if m.group(1).lower() not in ev.text()})
    if any(" ".join(m.group(0).lower().split()) not in ev.text() for m in SUM_SIGNS.finditer(joined)):       # "lead time 7 + 3 safety days" is quoted from a tool; "120 + 3840" is not
        derived.append("a sum")
    if derived:
        problems.append("a calculation or comparison of your own (" + ", ".join(derived) + "): only state figures the tools returned and leave comparing to the program")
    return problems


def substance_problems(text: str, ev: Evidence) -> list[str]:
    """An answer has to be an answer: a sentence, or at least a figure or a code that the tools returned. Not a stray symbol, a bare yes, or "Sent."."""
    clean = " ".join((text or "").split())
    if len(re.findall(r"[A-Za-z]{2,}", clean)) >= 3 and len(clean) >= 12:
        return []
    pool = ev.pool()
    grounded = any(_contains(n, pool) for n in numbers(CODE_RE.sub(" ", clean))) or any(c.lower() in ev.text() for c in CODE_RE.findall(clean))
    if grounded and re.fullmatch(r"[A-Za-z0-9 .,\-/%:()'’]+", clean):         # a bare figure or code is fine when it is clean ("1.1 days"); stray symbols ("><0") are not
        return []
    return ["the answer is not an answer: write one complete sentence that states the figure"]


@dataclass
class Outcome:
    status: str = "failed"                  # ok | rejected | failed
    data: Optional[dict[str, Any]] = None
    problems: list[str] = field(default_factory=list)
    note: str = ""
    steps: int = 0
    tool_calls: int = 0
    llm_seconds: float = 0.0
    prompt_tokens: int = 0
    output_tokens: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)
    timings: list[dict[str, Any]] = field(default_factory=list)       # one entry per model reply: where the time went
    evidence: Optional[Evidence] = None


class AgentRuntime:
    def __init__(self, llm: ToolChatClient, max_steps: int = 6, max_tool_calls: int = 10) -> None:
        self.llm, self.max_steps, self.max_tool_calls = llm, max_steps, max_tool_calls

    async def run(self, job: AgentJob, bridge: ToolBridge) -> Outcome:
        out = Outcome()
        ev = Evidence(job.question)
        out.evidence = ev
        messages: list[dict[str, Any]] = [{"role": "system", "content": job.system}, {"role": "user", "content": job.user}]
        try:
            tools = await bridge.tools(job.tools)
            names = {t["name"] for t in tools}
            getattr(self.llm, "set_tools", lambda _tools: None)(tools)          # a provider that must see the definition of every tool used in the conversation keeps them
            seen: set[str] = set()
            for name, args in job.warm:
                result = await self._use(bridge, job, ev, out, seen, names, name, args)
                messages += [_call_message([(name, args)]), _tool_message(name, result)]
            if job.after_warm is not None:
                job.after_warm(ev)
            if job.digest is not None:                                       # the model reads a short digest, not the raw results
                messages = [messages[0], {"role": "user", "content": job.user + "\n\n" + job.digest(ev)}]
            for _ in range(max(0, self.max_steps - 2) if job.gather else 0):    # leave room for the answer and one repair
                if out.tool_calls >= self.max_tool_calls:
                    break
                reply = await self._chat(out, messages, "gather", tools=tools)
                calls = reply.tool_calls
                if not calls:
                    salvaged = salvage_tool_call(reply.content, names)
                    calls = [salvaged] if salvaged else []
                if not calls:
                    break
                messages.append(_call_message(calls, reply.raw))
                for name, args in calls:
                    messages.append(_tool_message(name, await self._use(bridge, job, ev, out, seen, names, name, args)))
            messages.append({"role": "user", "content": job.final(ev) if callable(job.final) else job.final})
            reply = await self._chat(out, messages, "answer", schema=job.schema)
            for attempt in range(2):
                data, problems = self._verify(reply.content, job, ev)
                if not problems:
                    out.status, out.data, out.problems = "ok", data, []
                    break
                out.status, out.problems = "rejected", problems
                if attempt == 1 or out.steps >= self.max_steps:
                    break
                messages += [{"role": "assistant", "content": reply.content},
                             {"role": "user", "content": "Your answer was rejected: " + "; ".join(problems[:4]) + ". Fix exactly that and reply again in the same JSON format, "
                                                         "using only numbers and codes that appear in the tool results."}]
                reply = await self._chat(out, messages, "repair", schema=job.schema)
            out.trace = [{"messages": _readable(messages)}, {"answer": reply.content, "problems": out.problems}, {"timings": out.timings}]
        except LLMError as exc:
            out.status, out.note = "failed", str(exc)[:300]
            out.trace = [{"messages": _readable(messages)}, {"error": out.note}, {"timings": out.timings}]
        return out

    # ------------------------------------------------------------------------------------------------ pieces
    async def _chat(self, out: Outcome, messages: list[dict[str, Any]], kind: str, tools: Optional[list[dict[str, Any]]] = None, schema: Optional[dict[str, Any]] = None) -> Reply:
        out.steps += 1
        started = time.monotonic()
        who = {"provider": getattr(self.llm, "provider", None), "model": getattr(self.llm, "model", None), "kind": kind}
        try:
            reply = await anyio.to_thread.run_sync(lambda: self.llm.chat(messages, tools, schema))
        except LLMError as exc:                                          # the model could not be reached or did not reply: one line, the class of the failure, not its text
            obs.event("llm_call", logging.WARNING, **who, ms=int((time.monotonic() - started) * 1000), outcome="error", error=type(exc).__name__)
            raise
        obs.event("llm_call", **who, ms=int((time.monotonic() - started) * 1000), tokens_in=reply.prompt_tokens, tokens_out=reply.output_tokens, outcome="ok")
        out.timings.append({"step": out.steps, "kind": kind, "seconds": round(reply.seconds, 1), "load": round(reply.load_seconds, 1), "reading_prompt": round(reply.prompt_seconds, 1),
                            "writing": round(reply.generate_seconds, 1), "prompt_tokens": reply.prompt_tokens, "output_tokens": reply.output_tokens})
        out.llm_seconds += reply.seconds
        out.prompt_tokens += reply.prompt_tokens
        out.output_tokens += reply.output_tokens
        return reply

    async def _use(self, bridge: ToolBridge, job: AgentJob, ev: Evidence, out: Outcome, seen: set[str], names: set[str], name: str, args: dict[str, Any]) -> dict[str, Any]:
        """One tool call, if the job allows it. A refusal is returned to the model as the tool's result so it can carry on."""
        key = name + json.dumps(args, sort_keys=True, default=str)
        if name not in names:
            return {"error": f"'{name}' is not available to you. You can use: {', '.join(sorted(names))}"}
        if out.tool_calls >= self.max_tool_calls:
            return {"error": "no more tool calls are allowed in this task; answer with what you have"}
        if job.gate is not None:
            refusal = job.gate(name, args, ev)
            if refusal:
                return {"error": refusal}
        if job.rewrite is not None:
            args = job.rewrite(name, args)
            key = name + json.dumps(args, sort_keys=True, default=str)
        if key in seen:
            return {"note": "you already asked exactly this; the result is above"}
        seen.add(key)
        out.tool_calls += 1
        result = await bridge.call(name, args)
        ev.add(ToolRun(name, args, result, "error" not in result))
        return result

    @staticmethod
    def _verify(content: str, job: AgentJob, ev: Evidence) -> tuple[Optional[dict[str, Any]], list[str]]:
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            data = json.loads(text)
        except ValueError:
            return None, ["the reply was not valid JSON"]
        import jsonschema                               # comes with the optional MCP package
        errors = sorted(jsonschema.Draft202012Validator(job.schema).iter_errors(data), key=lambda e: list(e.path))
        if errors:
            return None, [f"{'/'.join(str(p) for p in e.path) or 'the answer'}: {e.message[:120]}" for e in errors[:3]]
        return data, job.check(data, ev)


def _call_message(calls: list[tuple[str, dict[str, Any]]], raw: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": n, "arguments": a or {}}} for n, a in calls]}
    if raw is not None:                                    # what the provider must be given back with these calls (Gemini's thought signatures); never shown or stored
        message["raw"] = raw
    return message


def _readable(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in m.items() if k != "raw"} for m in messages]


def _tool_message(name: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "tool_name": name, "content": json.dumps(result, separators=(",", ":"), default=str)}

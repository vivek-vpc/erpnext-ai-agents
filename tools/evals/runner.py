"""Ask one case's question through the application's own entry points and say whether the answer is what the case expects.

Nothing here reaches into the application's internals to make an answer come out right: the question goes in where a person's would (the chat, the agent's Ask, "Ask why"),
the stand-in model is plugged in where a real one would be, and everything checked afterwards is what a person, the audit record or ERPNext would see.
"""
from __future__ import annotations

import importlib.util
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .cases import Case, Expect
from .fakellm import Script, ScriptedTextLLM, ScriptedToolLLM
from .worlds import APPROVER, OWNER, VIEWER, World, build_world, find_approval, resolve

class CaseError(RuntimeError):
    """The case itself cannot be run (it names a request that does not exist ...): a mistake in the case, not an answer of the application."""


ERP_WRITE_LISTS = ("purchase_orders", "requisitions", "stock_transfers", "sent_emails")      # what the demo ERP keeps of everything written to it


@dataclass
class Observed:
    answer: str = ""
    text: str = ""                                   # the answer and its tables, as a person reads them: what must_include is searched in
    intent: str = ""
    status: str = ""
    source: str = ""
    agent: str = ""
    outcome: str = "none"
    tools: list[dict[str, Any]] = field(default_factory=list)
    routed_by_ai: bool = False
    prepared: int = 0                                # approval requests created by the question
    erp_writes: int = 0                              # documents written to the ERP by the question (must always be 0: only a person's button does that)
    steps: int = 0
    rejects: int = 0                                 # replies of the model that the checks rejected
    repairs: int = 0                                 # of those, how many were given a second chance
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0
    error: str = ""
    model_calls: int = 0
    note: str = ""                                   # the problems the checks found in a rejected reply, as the task record keeps them
    reasons: str = ""                                # every reason the checks gave for rejecting a reply: what the model was told in a repair, the final note, the check of a restatement


@dataclass
class Result:
    case: Case
    observed: Observed = field(default_factory=Observed)
    failures: list[str] = field(default_factory=list)
    skipped: str = ""

    @property
    def passed(self) -> bool:
        return not self.failures and not self.skipped

    @property
    def counts(self) -> bool:
        """A case that is expected to fail (a known gap) counts as met only while it still fails."""
        return (not self.passed) if self.case.known_gap else self.passed


def agents_installed() -> bool:
    return importlib.util.find_spec("mcp") is not None and importlib.util.find_spec("jsonschema") is not None


# ------------------------------------------------------------------------------------------------------------------- running
def run_case(case: Case, clients: Optional[tuple[Any, Any]] = None) -> Result:
    """`clients` = (a tool-calling client, a single-shot client) to use instead of the case's script: a real model (tools/e2e/agents_eval.py)."""
    sections = case.ai_sections
    if "agents" in sections and not agents_installed():
        return Result(case, skipped="needs the optional 'agents' extra: pip install \".[agents]\"")
    world = build_world(case)
    app = world.app
    exp = Expect.model_validate(resolve(app, case.expect.model_dump()))
    steps = resolve(app, case.llm)
    script: Optional[Script] = None
    if clients is not None:
        tool_llm, text_llm = clients
    else:
        script = Script(steps)
        tool_llm, text_llm = ScriptedToolLLM(script), ScriptedTextLLM(script)
    for section in sections:
        app.settings.update_section(OWNER, section, {"enabled": True})
    if clients is not None and getattr(tool_llm, "remote", False):
        app.settings.update_section(OWNER, "agents", {"allow_remote": True})            # choosing a model on another machine or a hosted one on the command line is the consent
    app.agents.client_factory = lambda cfg: tool_llm
    app.chat.client_factory = lambda cfg: text_llm
    app.assistant.client_factory = lambda cfg: text_llm

    actor = APPROVER if case.asker == "approver" else VIEWER
    approvals_before = len(app.approvals.list(None, 100_000))
    writes_before = _erp_writes(app.erp)
    started = time.monotonic()
    obs = Observed()
    result: dict[str, Any] = {}
    try:
        result = _ask(case, world, actor)
    except ValueError as exc:                          # the application refused the question (empty, too long ...): that is an answer too
        obs.error = str(exc)
    obs.seconds = round(time.monotonic() - started, 3)
    obs.prepared = len(app.approvals.list(None, 100_000)) - approvals_before
    obs.erp_writes = _erp_writes(app.erp) - writes_before
    _observe(obs, case, app, result, script)
    return Result(case, obs, check(case, exp, obs, script, clients is not None))


def _ask(case: Case, world: World, actor: Any) -> dict[str, Any]:
    app, can_decide = world.app, case.asker == "approver"
    if case.entry == "chat":
        return app.chat.ask(case.question, actor.id, can_decide, case.context)
    if case.entry == "agent":
        return app.agents.ask(case.question, actor)
    if case.entry == "explain":
        return app.chat.explain(case.question, actor.id, can_decide, case.context)
    record = find_approval(app, (case.approval or {})["kind"], (case.approval or {})["item"])
    if record is None:
        raise CaseError(f"{case.id}: no {(case.approval or {}).get('kind')} request for {(case.approval or {}).get('item')} to ask about")
    return app.assistant.ask(record.id, case.question, actor)


def _erp_writes(erp: Any) -> int:
    return sum(len(getattr(erp, name, []) or []) for name in ERP_WRITE_LISTS)


# ------------------------------------------------------------------------------------------------------------------- what happened
def _observe(obs: Observed, case: Case, app: Any, result: dict[str, Any], script: Optional[Script]) -> None:
    obs.answer = str(result.get("answer") or "")
    obs.intent = str(result.get("intent") or "")
    obs.status = str(result.get("status") or "")
    obs.source = str(result.get("source") or "")
    obs.agent = str(result.get("agent") or "")
    obs.routed_by_ai = bool(result.get("routed_by_ai"))
    facts = " ".join(str(f.get("text", "")) for f in result.get("facts") or [] if isinstance(f, dict))
    tables = json.dumps(result.get("blocks") or [], ensure_ascii=False)
    obs.text = " ".join(x for x in (obs.answer, tables if result.get("blocks") else "", facts if case.entry in ("explain", "why") else "") if x)
    rows = app.db.query("SELECT tool, args_json, ok, via FROM tool_calls WHERE via != 'eval' ORDER BY id")
    obs.tools = [{"name": r["tool"], "args": json.loads(r["args_json"] or "{}"), "ok": bool(r["ok"]), "via": r["via"]} for r in rows]
    tasks = app.agent_log.list(limit=50)
    task = app.agent_log.get(tasks[0]["id"]) if tasks else None
    obs.model_calls = len(script.calls) if script is not None else (task["steps"] if task else 0)
    if task is not None:
        _from_task(obs, task)
    elif obs.model_calls or (script is None and case.ai_sections):
        _from_single_shot(obs, case, result, script)
    told = [str(c["shown"][-1].get("content", "")) for c in (script.calls if script else []) if c["kind"] == "repair" and isinstance(c["shown"], list) and c["shown"]]
    obs.reasons = " | ".join([*told, obs.note if obs.outcome == "fallback" else "", str(result.get("check") or "") if obs.status == "rejected" else ""]).strip(" |")


def _from_task(obs: Observed, task: dict[str, Any]) -> None:
    trace = task.get("trace") or []
    timings = next((t["timings"] for t in trace if isinstance(t, dict) and "timings" in t), [])
    obs.steps, obs.repairs = int(task.get("steps") or 0), sum(1 for t in timings if t.get("kind") == "repair")
    obs.tokens_in, obs.tokens_out = int(task.get("prompt_tokens") or 0), int(task.get("output_tokens") or 0)
    stored = (task.get("result") or {})
    obs.note = str(task.get("note") or "")
    if task["status"] == "ok":
        obs.outcome = "declined" if stored.get("status") == "not_enough_information" else ("repaired" if obs.repairs else "accepted")
        obs.rejects = obs.repairs
    elif task["status"] == "fallback":
        unreachable = any(isinstance(t, dict) and "error" in t for t in trace)
        obs.outcome = "unavailable" if unreachable else "fallback"
        obs.rejects = obs.repairs + (0 if unreachable else 1)
    else:
        obs.outcome = "unavailable"


def _from_single_shot(obs: Observed, case: Case, result: dict[str, Any], script: Optional[Script]) -> None:
    """The chat's router and the 'explain' / 'Ask why' restatements: one reply from the model, checked or mapped to a fixed list."""
    first = next((c for c in (script.calls if script else []) if c.get("step") is not None), None)
    if case.entry in ("explain", "why"):
        obs.outcome = {"answered": "accepted", "not_enough_information": "declined", "rejected": "fallback", "unavailable": "unavailable"}.get(obs.status, "none")
        obs.rejects = int(obs.status == "rejected")
        return
    if obs.routed_by_ai:
        obs.outcome = "accepted"
    elif first is not None and "error" in (first["step"] or {}):
        obs.outcome = "unavailable"
    else:
        said_unknown = False
        try:
            said_unknown = json.loads((first or {}).get("step", {}).get("raw") or json.dumps((first or {}).get("step", {}).get("answer"))).get("intent") == "unknown"
        except (ValueError, AttributeError, TypeError):
            pass
        obs.outcome = "declined" if said_unknown else "fallback"
        obs.rejects = 0 if said_unknown else 1


# ------------------------------------------------------------------------------------------------------------------- the checks
def check(case: Case, exp: Expect, obs: Observed, script: Optional[Script], live: bool = False) -> list[str]:
    bad: list[str] = []

    def same(field_: str, want: Any, got: Any) -> None:
        if want is not None and want != got:
            bad.append(f"{field_}: expected {want!r}, got {got!r}")

    if exp.error is not None:
        if exp.error.lower() not in obs.error.lower():
            bad.append(f"error: expected a refusal containing {exp.error!r}, got {obs.error!r}")
        return bad
    if obs.error:
        bad.append(f"the application refused the question: {obs.error}")
        return bad
    if not live:                                    # a real model may take another route to a right answer: against one, only the answer, the writes and the forbidden tools are judged
        same("intent", exp.intent, obs.intent)
        same("status", exp.status, obs.status)
        same("source", exp.source, obs.source)
        same("agent", exp.agent, obs.agent)
        same("routed_by_ai", exp.routed_by_ai, obs.routed_by_ai)
        same("outcome", case.wants_outcome, obs.outcome)
        bad += _tool_problems(exp, obs)
    else:
        bad += _tool_problems(Expect(no_tools=exp.no_tools), obs)
    low = obs.text.lower()
    for want in exp.must_include:
        if not _found(want, obs.text, low):
            bad.append(f"must include {want!r}, which the person did not read in: {obs.answer[:160]!r}")
    for banned in exp.must_not_include:
        if _found(banned, obs.text, low):
            bad.append(f"must not include {banned!r}, but the person read it in: {obs.answer[:160]!r}")
    for why in exp.rejected_because:
        if why.lower() not in obs.reasons.lower():
            bad.append(f"expected a reply to be rejected because of {why!r}; the checks said: {obs.reasons[:200] or 'nothing (no reply was rejected)'}")
    if obs.prepared != exp.prepared:
        bad.append(f"approval requests created: expected {exp.prepared}, got {obs.prepared}")
    if obs.erp_writes:
        bad.append(f"{obs.erp_writes} document(s) were written to the ERP; only a person's button may do that")
    if script is not None:
        bad += [f"stand-in model: {p}" for p in script.problems]
        if script.unused() and not script.problems:
            bad.append(f"stand-in model: {script.unused()} scripted repl{'y' if script.unused() == 1 else 'ies'} never asked for (the script and the application are out of step)")
    return bad


def _found(want: str, text: str, low: str) -> bool:
    if want.startswith("re:"):
        return re.search(want[3:], text, re.I) is not None
    return want.lower() in low


def _loose_equal(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()


def _tool_problems(exp: Expect, obs: Observed) -> list[str]:
    bad: list[str] = []
    called = obs.tools
    pos = 0
    for want in exp.tools:
        for i in range(pos, len(called)):
            if called[i]["name"] == want.name and all(k in called[i]["args"] and _loose_equal(called[i]["args"][k], v) for k, v in want.args.items()):
                pos = i + 1
                break
        else:
            seen = ", ".join(f"{c['name']}({', '.join(f'{k}={v}' for k, v in c['args'].items())})" for c in called) or "none"
            bad.append(f"tool call expected: {want.name}{want.args or ''} (in this order); calls made: {seen}")
    if exp.only_tools:
        extra = sorted({c["name"] for c in called} - {t.name for t in exp.tools})
        if extra:
            bad.append(f"tools called that the case does not list: {', '.join(extra)}")
    for name in exp.no_tools:
        if any(c["name"] == name for c in called):
            bad.append(f"{name} must not be called, but was")
    return bad


def run_all(cases: list[Case], clients: Optional[tuple[Any, Any]] = None) -> list[Result]:
    return [run_case(c, clients) for c in cases]

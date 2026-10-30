"""Does the evaluation notice when a check is broken? Each test switches off or weakens ONE safeguard of the application (the grounding check, the order gate, the role check ...),
runs the cases that exist to catch that, and requires that some case fails and that the gate over the whole suite fails with it. Without these a green suite would only mean
that nothing was tried. The unbroken run passes the same gate (test_evals_suite.py::test_the_gate_over_all_the_cases)."""
import json

import pytest

pytest.importorskip("mcp", reason="the optional 'agents' extra is not installed")

from erp_agents.core.agents import runtime
from erp_agents.core.assistant import verify as verify_module
from erp_agents.core.common import Actor
from erp_agents.packs.stock_procurement.agents import purchase_agent
from erp_agents.packs.stock_procurement.assistant import chat, service
from evals_support import baseline  # noqa: F401 - the session fixture
from tools.evals.metrics import gate, load_thresholds, summarise
from tools.evals.runner import run_case


def _accept_everything(raw, facts, question, max_chars):
    return verify_module.Verdict("answered", str(json.loads(raw).get("answer", "")), [], "no checks")


def _no_schema(content, job, ev):
    return (lambda data: (data, job.check(data, ev)))(json.loads((content or "").strip()))


MUTATIONS = [
    ("the grounding check lets every figure and code through", ("agent_bad",), lambda mp: mp.setattr(purchase_agent, "grounding_problems", lambda texts, ev: [])),
    ("an empty or meaningless answer is accepted", ("agent_bad",), lambda mp: mp.setattr(purchase_agent, "substance_problems", lambda text, ev: [])),
    ("the answer's format is no longer checked", ("agent_bad",), lambda mp: mp.setattr(runtime.AgentRuntime, "_verify", staticmethod(_no_schema))),
    ("guessing words are allowed", ("agent_bad",), lambda mp: (mp.setattr(runtime, "HEDGES", ()), mp.setattr(verify_module, "HEDGES", ()))),
    ("the order gate lets the model prepare any request", ("permissions",), lambda mp: mp.setattr(purchase_agent, "prepare_gate", lambda question, can_prepare: (lambda name, args, ev: None))),
    ("everybody counts as an approver", ("permissions",), lambda mp: mp.setattr(Actor, "has_role", lambda self, minimum: True)),
    ("the checked restatement accepts any reply", ("explain", "decision_history"), lambda mp: (mp.setattr(chat, "verify", _accept_everything), mp.setattr(service, "verify", _accept_everything))),
    ("the router may pick an action", ("router",), lambda mp: mp.setattr(chat, "INTENTS", (*chat.INTENTS, "order_request", "delete_everything"))),
    # hostile text (Phase 4): the fence around what a model is shown, and the order gate's idea of "the item the person typed"
    ("an agent reads tool results uncleaned", ("adversarial",), lambda mp: mp.setattr(runtime, "clean_json", lambda obj, *a, **k: obj)),
    ("an agent is shown the question without the marks", ("adversarial",), lambda mp: mp.setattr(purchase_agent, "data", lambda value, limit=0: str(value))),
    ("\"Ask why\" shows the facts raw", ("adversarial",), lambda mp: mp.setattr(service, "fact_line", lambda fact_id, text: f"{fact_id}: {text}")),
    ("the chat shows the model the question without the marks", ("adversarial",), lambda mp: mp.setattr(chat, "data", lambda value, limit=0: str(value))),
    ("an order is allowed for an item whose code only begins like the one typed", ("adversarial",), lambda mp: mp.setattr(purchase_agent, "mentions", lambda text, name: bool(name) and name in text)),
    ("the order gate lets the model prepare any request, whatever the data says", ("adversarial",), lambda mp: mp.setattr(purchase_agent, "prepare_gate", lambda question, can_prepare: (lambda name, args, ev: None))),
    ("a model's copied marks are shown to the person", ("adversarial",), lambda mp: mp.setattr(verify_module, "unfence", lambda value: value)),
]


@pytest.mark.parametrize("name,groups,apply", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_the_evaluation_notices_a_broken_check(name, groups, apply, baseline, monkeypatch):
    limits = load_thresholds()
    unbroken = baseline.all()
    assert not gate(summarise(unbroken), limits), "the unbroken suite must pass the gate first"
    apply(monkeypatch)
    mutated = {c.id: run_case(c) for c in baseline.cases if c.group in groups}
    noticed = [r.case.id for r in mutated.values() if not r.passed and not r.case.known_gap]
    assert noticed, f"nothing noticed: {name}. None of the {len(mutated)} cases in {', '.join(groups)} failed, so a regression like this would reach a person."
    problems = gate(summarise([mutated.get(r.case.id, r) for r in unbroken]), limits)
    assert problems, f"the cases failed ({', '.join(noticed[:3])}) but the gate still passed"

"""Running the golden cases on a real model, and comparing two (tools/evals/live.py, tools/e2e/agents_eval.py --suite golden).

No real model is involved: GoodModel is a small deterministic stand-in that behaves like a competent one (it asks for the tool a question needs and words what the tool returned),
SloppyModel is the same but gets one figure wrong. They go through exactly the path a real client takes: the application's own settings, tools, checks and fallbacks."""
import json
import re

import pytest

pytest.importorskip("mcp", reason="the optional 'agents' extra is not installed")

import yaml

from erp_agents.core.agents.llm import Reply
from tools.e2e import agents_eval
from tools.evals import live
from tools.evals.cases import load_cases
from tools.evals.live import ModelSpec, RecordingClient, TextAdapter, live_cases, parse_spec, run_model, step_of
from tools.evals.runner import run_case


def say(obj):
    return Reply(content=json.dumps(obj), prompt_tokens=50, output_tokens=10)


class GoodModel:
    model, remote, hosted, provider = "good-model", False, False, "ollama"
    wrong_stock = False

    def status(self):
        return {"reachable": True, "model_installed": True}

    def set_tools(self, tools):
        pass

    def chat(self, messages, tools=None, schema=None):
        props = (schema or {}).get("properties", {})
        user = next(m["content"] for m in messages if m["role"] == "user")
        if "intent" in props:                                                            # the router
            return say({"intent": "unknown"})
        if "facts_used" in props:                                                        # "explain" and "Ask why": restate one numbered fact
            facts = dict(re.findall(r"^(F\d+): (.*)$", user, re.M))
            question = re.search(r"QUESTION: (.*)", user).group(1)
            if "fastest" in question:
                return say({"answerable": False, "answer": "", "facts_used": []})
            key = "F1" if "running low" in question.lower() else "F2"
            return say({"answerable": True, "answer": facts[key], "facts_used": [key]})
        question = re.search(r"QUESTION: (.*)", user).group(1)                           # an agent
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if tools:
            call = None if results else self.wanted(question)
            return Reply(tool_calls=[call], prompt_tokens=50, output_tokens=10) if call else Reply(content="done", prompt_tokens=50, output_tokens=10)
        return say(self.answer(results))

    @staticmethod
    def wanted(q):
        low = q.lower()
        if "create a purchase order" in low:
            return "prepare_order_request", {"item_code": "BOLT-M8", "qty": 500, "reason": "asked"}
        if "how much" in low and "bolt-m8" in low:
            return "stock_status", {"item_code": "BOLT-M8"}
        if "vendor" in low:
            return "vendor_options", {"item_code": "BOLT-M8", "qty": 500}
        if "late" in low:
            return "late_deliveries", {}
        return None

    def answer(self, results):
        if not results:
            return {"answerable": False, "answer": ""}
        r = results[0]
        if "on_hand" in r:
            return {"answerable": True, "answer": f"{r['item_code']} has {999 if self.wrong_stock else r['on_hand']} pcs on hand."}
        if "vendors" in r:
            v = r["vendors"][0]
            return {"answerable": True, "answer": f"{r['best']} is the best vendor for {r['qty']} pcs of {r['item_code']}: {v['price_each']} each, {v['total']} in total, delivery in {v['delivery_days']} days."}
        if "orders" in r:
            o, e = r["orders"][0], r["orders"][0]["items"][0]
            return {"answerable": True, "answer": f"{o['document']} from {o['vendor']} is {o['days_late']} days late, with {e['still_to_come']} {e['uom']} of {e['item_code']} still to come."}
        return {"answerable": True, "answer": "I prepared the request for BOLT-M8 and sent it to the approvers."}


class SloppyModel(GoodModel):
    model = "sloppy-model"
    wrong_stock = True


def by_name(settings, keys=None, session=None):
    return SloppyModel() if settings.model == "sloppy" else GoodModel()


# ------------------------------------------------------------------ which cases, which model
def test_a_model_is_named_the_way_the_command_line_writes_it():
    assert parse_spec("llama3.1") == ModelSpec("ollama", "llama3.1", "")
    assert parse_spec("llama3.2:3b") == ModelSpec("ollama", "llama3.2:3b", "")                    # a tag is part of the name, "llama3.2" is not a provider
    assert parse_spec("anthropic:claude-haiku-5-5") == ModelSpec("anthropic", "claude-haiku-5-5", "")
    assert parse_spec("openai:gpt-4o-mini", "https://llm.example.com/v1") == ModelSpec("openai", "gpt-4o-mini", "https://llm.example.com/v1")


def test_only_cases_a_real_model_can_answer_are_run_live():
    cases = load_cases()
    live_ids = {c.id for c in live_cases(cases)}
    assert live_ids == {"agent-answers-a-stock-question", "agent-answers-a-vendor-question", "agent-names-the-late-orders", "agent-prepares-the-request-the-person-asked-for",
                        "explain-restates-the-facts", "history-why-this-alert-the-model-cites-facts", "why-the-model-says-the-facts-do-not-answer",
                        "scope-a-question-outside-stock-with-the-ai-on"}
    assert not any(c.reply == "bad" for c in live_cases(cases)) and not any(c.id == "router-places-a-vague-question" for c in live_cases(cases))
    assert [c.id for c in live_cases(cases, groups=["explain"])] == ["explain-restates-the-facts", "why-the-model-says-the-facts-do-not-answer"]
    assert [c.id for c in live_cases(cases, ids=["agent-names-the-late-orders"])] == ["agent-names-the-late-orders"]


# ------------------------------------------------------------------ a model that does its job, and one that does not
def test_a_competent_model_gets_every_live_case_right_through_the_real_checks():
    summary, results = run_model(live_cases(load_cases()), ModelSpec(model="good"), {}, False, 60, client_for=lambda s: GoodModel(), say=lambda m: None)
    assert [r.case.id for r in results if not r.passed] == []
    assert summary["met"] == summary["counted"] == 8 and summary["model"]["accepted"] >= 4
    assert summary["model"]["tokens_in"] > 0


def test_a_model_that_gets_a_figure_wrong_is_caught_by_the_checks_and_scored_wrong():
    summary, results = run_model(live_cases(load_cases(), ids=["agent-answers-a-stock-question"]), ModelSpec(model="sloppy"), {}, False, 60, client_for=lambda s: SloppyModel(), say=lambda m: None)
    (r,) = results
    assert not r.passed and r.observed.outcome == "fallback" and "999" not in r.observed.answer and "I do not have enough information" in r.observed.answer
    assert summary["pass_rate"] == 0 and summary["model"]["fallback"] == 1


# ------------------------------------------------------------------ recording what a model said
def test_what_a_model_says_is_kept_in_the_form_a_script_is_written():
    assert step_of(Reply(tool_calls=[("stock_status", {"item_code": "A"})], prompt_tokens=5, output_tokens=2)) == {"tools": [["stock_status", {"item_code": "A"}]], "tokens": [5, 2]}
    assert step_of(Reply(content='{"answerable": true, "answer": "x"}')) == {"answer": {"answerable": True, "answer": "x"}}
    assert step_of(Reply(content='{"answerable": tru')) == {"raw": '{"answerable": tru'}
    assert step_of(Reply(content="I have what I need.")) == {"say": "I have what I need."}


def test_a_recorded_transcript_replays_to_the_same_result(tmp_path):
    chosen = live_cases(load_cases(), ids=["agent-answers-a-vendor-question", "explain-restates-the-facts", "scope-a-question-outside-stock-with-the-ai-on"])
    run_model(chosen, ModelSpec(model="good"), {}, False, 60, record_dir=tmp_path, client_for=lambda s: GoodModel(), say=lambda m: None)
    recorded = {c.id: c for c in load_cases(tmp_path)}
    assert set(recorded) == {f"{c.id}-recorded-good" for c in chosen}
    for case in recorded.values():
        assert case.llm and case.reply == "good" and "Recorded from ollama:good" in case.note
        result = run_case(case)
        assert result.passed, (case.id, result.failures)                                              # replaying what the model said gives the answer it gave, through the same checks
    text = (tmp_path / "agent-answers-a-vendor-question.good.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["cases"][0]["llm"][0] == {"tools": [["vendor_options", {"item_code": "BOLT-M8", "qty": 500}]], "tokens": [50, 10]}


def test_a_model_that_cannot_be_reached_is_recorded_as_an_error_and_the_error_still_raises():
    from erp_agents.core.assistant.llm import LLMError

    class Down:
        model, remote = "down", False

        def chat(self, messages, tools=None, schema=None):
            raise LLMError("could not reach the model")
    rec = RecordingClient(Down())
    with pytest.raises(LLMError):
        rec.chat([{"role": "user", "content": "q"}], None, None)
    assert rec.steps == [{"error": "could not reach the model"}] and rec.model == "down"


def test_any_provider_can_answer_the_single_shot_questions():
    reply = Reply(content='{"intent": "unknown"}')
    seen = {}

    class Client:
        model = "m"

        def chat(self, messages, tools=None, schema=None):
            seen.update(messages=messages, tools=tools, schema=schema)
            return reply
    assert TextAdapter(Client()).chat("sort this", "QUESTION: x", {"type": "object"}) == '{"intent": "unknown"}'
    assert seen["messages"] == [{"role": "system", "content": "sort this"}, {"role": "user", "content": "QUESTION: x"}] and seen["tools"] is None


# ------------------------------------------------------------------ the command, with two models side by side
def test_the_command_runs_the_same_cases_on_two_models_and_prints_one_table(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(live, "make_client", by_name)
    monkeypatch.setattr(live, "load_config", lambda path=None: {})
    out = tmp_path / "compare.md"
    code = agents_eval.main(["--suite", "golden", "--model", "good", "--against", "sloppy", "--group", "agent_good", "--out", str(out), "--record", str(tmp_path / "rec")])
    text = out.read_text(encoding="utf-8")
    assert code == 0
    assert "## Same cases, different models" in text and "| | good (Ollama (on this computer)) | sloppy (Ollama (on this computer)) |" in text
    assert "| agent-answers-a-stock-question | right" in text and "**WRONG**" in text                        # the sloppy model got the stock figure wrong, the good one did not
    assert "| Right answers | 4 (100%) | 3 (75%) |" in text
    assert sorted(p.name for p in (tmp_path / "rec").glob("agent-answers-a-stock-question.*.yaml")) == ["agent-answers-a-stock-question.good.yaml", "agent-answers-a-stock-question.sloppy.yaml"]
    assert len(list((tmp_path / "rec").glob("*.yaml"))) == 8                                              # four cases, two models: one file each, the second model overwrites nothing
    assert "fictional data" in capsys.readouterr().out


def test_a_hosted_model_without_its_key_is_refused_before_anything_is_sent(monkeypatch, capsys):
    monkeypatch.setattr(live, "load_config", lambda path=None: {})
    assert agents_eval.main(["--suite", "golden", "--provider", "anthropic"]) == 2
    assert "No API key for Claude (Anthropic): add ANTHROPIC_API_KEY=... to the .env file first." in capsys.readouterr().out


def test_a_model_on_another_machine_needs_the_consent_flag(monkeypatch, capsys):
    monkeypatch.setattr(live, "load_config", lambda path=None: {})
    assert agents_eval.main(["--suite", "golden", "--base-url", "http://192.0.2.10:11434"]) == 2
    assert "localhost" in capsys.readouterr().out


def test_nothing_to_run_is_said_not_silently_passed(monkeypatch, capsys):
    monkeypatch.setattr(live, "load_config", lambda path=None: {})
    assert agents_eval.main(["--suite", "golden", "--case", "no-such-case"]) == 2
    assert "no case matches" in capsys.readouterr().out


def test_the_scripted_stand_in_and_a_real_client_are_interchangeable_for_the_runner():
    case = next(c for c in load_cases() if c.id == "agent-answers-a-stock-question")
    scripted = run_case(case)
    real_shaped = run_case(case, clients=(GoodModel(), TextAdapter(GoodModel())))
    assert scripted.passed and real_shaped.passed
    assert scripted.observed.answer == real_shaped.observed.answer

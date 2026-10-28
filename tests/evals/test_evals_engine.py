"""The evaluation engine itself (tools/evals): it must refuse a mistyped case, read numbers from the world, replay a script strictly, judge an answer the way the case says, and add the
results up correctly. A suite that is wrong about itself is worse than none, so these tests do not use the real cases: each builds its own."""
import itertools
import json
import textwrap

import pytest
from pydantic import ValidationError

from tools.evals import __main__ as cli
from tools.evals.cases import Case, Expect, load_cases
from tools.evals.fakellm import Script, ScriptedTextLLM, ScriptedToolLLM
from tools.evals.metrics import gate, summarise
from tools.evals.report import comparison, markdown
from tools.evals.runner import CaseError, Observed, Result, agents_installed, check, run_case
from tools.evals.worlds import VIEWER, build_world, resolve

needs_agents = pytest.mark.skipif(not agents_installed(), reason="the optional 'agents' extra is not installed")


def make(**kw):
    base = {"id": "case-one", "group": "item_stock", "question": "How much BOLT-M8 do we have?", "expect": {}}
    base.update(kw)
    return Case.model_validate(base)


def agent_case(llm, **kw):
    return make(entry="agent", **{"as": "viewer"}, reply="good", llm=llm, expect=kw.pop("expect", {"status": "answered"}), **kw)


GOOD = [{"tools": [["stock_status", {"item_code": "BOLT-M8"}]]}, {"say": "done"}, {"answer": {"answerable": True, "answer": "BOLT-M8 has 300 pcs on hand."}}]


# ------------------------------------------------------------------ the case format
def test_a_mistyped_case_is_refused_not_ignored():
    for bad in ({"exepct": {}}, {"expect": {"must_inclde": ["x"]}}, {"group": "stock"}, {"id": "Has Spaces"}, {"entry": "telegram"}, {"as": "owner"}):
        with pytest.raises(ValidationError):
            make(**bad)


def test_a_script_and_the_kind_of_reply_it_holds_go_together():
    with pytest.raises(ValidationError, match="needs an llm script"):
        make(reply="good")
    with pytest.raises(ValidationError, match="says what it holds"):
        make(llm=[{"say": "hi"}])
    with pytest.raises(ValidationError, match="exactly one of"):
        make(reply="bad", llm=[{"say": "hi", "raw": "x"}])
    with pytest.raises(ValidationError, match="exactly one of"):
        make(reply="bad", llm=[{"speak": "hi"}])
    with pytest.raises(ValidationError, match="names the request"):
        make(entry="why")


def test_a_setup_step_is_one_known_step():
    with pytest.raises(ValidationError, match="a single key"):
        make(setup=[{"open_order": {}, "run": "inventory"}])
    with pytest.raises(ValidationError, match="a single key"):
        make(setup=[{"shred": "everything"}])


def test_tool_expectations_may_be_written_short():
    e = Expect.model_validate({"tools": ["stock_status", {"substitutes": {"item_code": "OIL-5L"}}, {"name": "late_deliveries"}]})
    assert [(t.name, t.args) for t in e.tools] == [("stock_status", {}), ("substitutes", {"item_code": "OIL-5L"}), ("late_deliveries", {})]


def test_loading_names_the_file_and_the_case_that_is_wrong(tmp_path):
    (tmp_path / "a.yaml").write_text("cases:\n  - id: one\n    group: item_stock\n    question: q\n    expect: {}\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("cases:\n  - id: one\n    group: item_stock\n    question: q\n    expect: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"b\.yaml: the id 'one' is already used in a\.yaml"):
        load_cases(tmp_path)
    (tmp_path / "b.yaml").write_text("cases:\n  - id: two\n    group: nonsense\n    question: q\n    expect: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"b\.yaml, case 1 \(two\)"):
        load_cases(tmp_path)
    (tmp_path / "b.yaml").write_text("cases:\n  - id: two\n    note: this: is not valid\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"b\.yaml: not valid YAML"):
        load_cases(tmp_path)


# ------------------------------------------------------------------ the world
def test_every_case_gets_a_fresh_world_with_the_same_numbers():
    a, b = build_world(make()), build_world(make())
    assert a.app is not b.app
    assert a.app.toolkit.call("stock_status", {"item_code": "BOLT-M8"}, VIEWER)["on_hand"] == b.app.toolkit.call("stock_status", {"item_code": "BOLT-M8"}, VIEWER)["on_hand"] == 300
    assert str(a.today) == "2026-10-16"                                   # the application's own clock, never the machine's


def test_setup_steps_change_the_world():
    w = build_world(make(setup=[{"stock": {"item": "BOLT-M8", "on_hand": 5}}, {"open_order": {"name": "PUR-ORD-2026-00001", "item": "OIL-5L", "qty": 10, "due_in_days": 3}}]))
    assert w.app.toolkit.call("stock_status", {"item_code": "BOLT-M8"}, VIEWER)["on_hand"] == 5
    assert [d["name"] for d in w.app.erp.get_documents("Purchase Order", limit=10)] == ["PUR-ORD-2026-00001"]
    with pytest.raises(ValueError, match="unknown world"):
        build_world(make(world="mars"))
    with pytest.raises(ValueError, match="no pending"):
        build_world(make(setup=[{"decide": {"kind": "procurement.po", "item": "BOLT-M8", "action": "approve"}}]))


def test_a_number_can_be_read_from_the_world_instead_of_written_down():
    w = build_world(make())
    assert resolve(w.app, "has ${stock_status(item_code=BOLT-M8).on_hand} pcs") == "has 300 pcs"
    assert resolve(w.app, {"a": ["${vendor_options(item_code=BOLT-M8,qty=500).vendors.0.total}", 7]}) == {"a": ["18435", 7]}
    assert resolve(w.app, "${stock_status(item_code=BOLT-M8).daily_use}") == "40.93"
    with pytest.raises(ValueError, match="the tool said"):
        resolve(w.app, "${stock_status(item_code=NOPE-1).on_hand}")


# ------------------------------------------------------------------ the stand-in model
def test_the_script_is_replayed_in_order_and_runs_out_loudly():
    script = Script([{"say": "one"}, {"answer": {"a": 1}}])
    text = ScriptedTextLLM(script)
    assert text.chat("s", "u", {}) == "one" and text.chat("s", "u", {}) == '{"a": 1}'
    with pytest.raises(Exception, match="out of step"):
        text.chat("s", "u", {})
    assert "none left" in script.problems[0]


def test_a_tool_call_is_only_possible_when_tools_are_offered():
    script = Script([{"tools": [["stock_status", {}]]}])
    with pytest.raises(Exception, match="out of step"):
        ScriptedToolLLM(script).chat([{"role": "user", "content": "q"}], None, {"type": "object"})
    assert "calls a tool, but the application asked for answer" in script.problems[0]
    ok = ScriptedToolLLM(Script([{"tools": [["stock_status", {"item_code": "A"}]]}]))
    assert ok.chat([{"role": "user", "content": "q"}], [{"name": "stock_status"}], None).tool_calls == [("stock_status", {"item_code": "A"})]


def test_a_repair_request_is_recognised():
    script = Script([{"say": "x"}, {"say": "y"}])
    llm = ScriptedToolLLM(script)
    llm.chat([{"role": "user", "content": "q"}], None, {"type": "object"})
    llm.chat([{"role": "user", "content": "Your answer was rejected: numbers that no tool returned: 7. Fix exactly that"}], None, {"type": "object"})
    assert [c["kind"] for c in script.calls] == ["answer", "repair"]


@needs_agents
def test_a_script_that_is_out_of_step_with_the_application_fails_the_case_and_says_so():
    short = run_case(agent_case(GOOD[:2]))
    assert not short.passed and any("none left" in f for f in short.failures)
    long = run_case(agent_case(GOOD + [{"say": "an extra reply nobody asked for"}]))
    assert not long.passed and any("never asked for" in f for f in long.failures)
    assert run_case(agent_case(GOOD)).passed


# ------------------------------------------------------------------ judging an answer
def test_a_rules_case_is_judged_on_what_the_person_reads():
    ok = run_case(make(expect={"intent": "item_stock", "source": "data", "must_include": ["300 PCS", "re:lasts about \\d+\\.\\d days"], "must_not_include": ["Hydraulic"]}))
    assert ok.passed, ok.failures
    bad = run_case(make(expect={"intent": "vendor_compare", "must_include": ["nine hundred"], "must_not_include": ["300 pcs"], "outcome": "accepted"}))
    text = " | ".join(bad.failures)
    assert not bad.passed and "intent: expected 'vendor_compare', got 'item_stock'" in text and "must include 'nine hundred'" in text and "must not include '300 pcs'" in text
    assert "outcome: expected 'accepted', got 'none'" in text


def test_a_question_the_application_refuses_is_an_answer_too():
    assert run_case(make(question="", expect={"error": "Please type a question"})).passed
    wrong = run_case(make(question="", expect={"intent": "item_stock"}))
    assert not wrong.passed and "refused the question" in wrong.failures[0]


def test_nothing_written_to_the_erp_or_prepared_unasked_is_a_failure():
    o = Observed(erp_writes=1, prepared=2)
    case = make()
    msgs = check(case, case.expect, o, None)
    assert any("1 document(s) were written to the ERP" in m for m in msgs) and any("approval requests created: expected 0, got 2" in m for m in msgs)
    assert not check(make(expect={"prepared": 2}), Expect(prepared=2), Observed(prepared=2), None)


def test_a_question_that_prepares_a_request_creates_exactly_what_the_case_allows():
    r = run_case(make(world="quiet", question="Create a purchase order for BOLT-M8 for 500", expect={"intent": "order_request", "prepared": 1}))
    assert r.passed, r.failures and r.observed.prepared == 1
    r2 = run_case(make(world="quiet", question="Create a purchase order for BOLT-M8 for 500", expect={"intent": "order_request"}))
    assert not r2.passed and "approval requests created: expected 0, got 1" in " ".join(r2.failures)


def test_tool_expectations_are_checked_in_order_with_their_arguments():
    called = Observed(tools=[{"name": "stock_status", "args": {"item_code": "BOLT-M8"}, "ok": True, "via": "agent"}, {"name": "vendor_options", "args": {"item_code": "BOLT-M8", "qty": 500.0}, "ok": True, "via": "agent"}])
    case = make()
    assert not check(case, Expect.model_validate({"tools": [{"stock_status": {"item_code": "BOLT-M8"}}, {"vendor_options": {"qty": 500}}]}), called, None)
    wrong_order = check(case, Expect.model_validate({"tools": ["vendor_options", "stock_status"]}), called, None)
    assert any("tool call expected: stock_status" in m for m in wrong_order)
    assert any("tool call expected" in m for m in check(case, Expect.model_validate({"tools": [{"stock_status": {"item_code": "OIL-5L"}}]}), called, None))
    assert any("tools called that the case does not list: vendor_options" in m for m in check(case, Expect.model_validate({"tools": ["stock_status"], "only_tools": True}), called, None))
    assert any("prepare_order_request must not be called" in m for m in check(case, Expect.model_validate({"no_tools": ["prepare_order_request"]}),
                                                                             Observed(tools=[{"name": "prepare_order_request", "args": {}, "ok": True, "via": "agent"}]), None))


def test_against_a_real_model_only_the_answer_the_writes_and_the_forbidden_tools_are_judged():
    case = make()
    o = Observed(text="300 pcs", answer="300 pcs", intent="whatever", outcome="repaired", tools=[{"name": "other", "args": {}, "ok": True, "via": "agent"}])
    e = Expect.model_validate({"intent": "item_stock", "outcome": "accepted", "tools": ["stock_status"], "must_include": ["300"]})
    assert check(case, e, o, None, live=True) == []
    assert check(case, e, Observed(text="nothing", answer="nothing"), None, live=True)[0].startswith("must include '300'")


def test_every_kind_of_entry_reaches_its_part_of_the_application():
    r = run_case(make(entry="chat", question="What is running low?", expect={"intent": "stock_overview"}))
    assert r.passed, r.failures
    if agents_installed():
        r = run_case(agent_case(GOOD, expect={"status": "answered", "agent": "inventory", "source": "model", "outcome": "accepted", "tools": ["stock_status"]}))
        assert r.passed, r.failures
        assert r.observed.steps == 3 and r.observed.tokens_in > 0 and r.observed.repairs == 0
    r = run_case(make(entry="explain", reply="good", llm=[{"answer": {"answerable": True, "answer": "Of the 10 items watched, 2 need ordering now.", "facts_used": ["F1"]}}],
                      question="What is running low?", expect={"status": "answered", "outcome": "accepted", "must_include": ["2 need ordering"]}))
    assert r.passed, r.failures
    r = run_case(make(entry="why", reply="good", llm=[{"answer": {"answerable": False, "answer": "", "facts_used": []}}], approval={"kind": "inventory.reorder", "item": "BOLT-M8"},
                      question="Which vendor is fastest?", expect={"status": "not_enough_information", "outcome": "declined"}))
    assert r.passed, r.failures
    missing = make(entry="why", reply="good", llm=[{"say": "x"}], approval={"kind": "procurement.po", "item": "BOLT-M8"}, question="q", expect={})
    with pytest.raises(CaseError, match="no procurement.po request"):
        run_case(missing)


def test_a_comma_in_an_unquoted_reply_is_refused_because_it_would_split_the_reply_in_two():
    with pytest.raises(ValidationError, match="comma inside a value that is not in quotes"):
        make(reply="bad", llm=[{"answer": {"answerable": True, "answer": "BOLT-M8 has 4", "242 pcs on hand.": None}}])


@needs_agents
def test_a_rejected_reply_must_be_rejected_for_the_reason_the_case_names():
    bad = [{"tools": [["stock_status", {"item_code": "BOLT-M8"}]]}, {"say": "done"}, {"answer": {"answerable": True, "answer": "BOLT-M8 has 4,242 pcs on hand."}},
           {"answer": {"answerable": True, "answer": "BOLT-M8 has 300 pcs on hand."}}]
    right = run_case(agent_case(bad, expect={"outcome": "repaired", "rejected_because": ["numbers that no tool returned"], "must_not_include": ["4,242"]}))
    assert right.passed, right.failures
    assert "numbers that no tool returned: 4242" in right.observed.reasons
    wrong = run_case(agent_case(bad, expect={"outcome": "repaired", "rejected_because": ["guessing words"]}))
    assert not wrong.passed and "expected a reply to be rejected because of 'guessing words'" in wrong.failures[0]
    nothing = run_case(agent_case(GOOD, expect={"rejected_because": ["numbers that no tool returned"]}))
    assert not nothing.passed and "no reply was rejected" in nothing.failures[0]


# ------------------------------------------------------------------ adding the results up
_ids = itertools.count(1)


def result(group="item_stock", passed=True, reply="none", outcome="none", gap="", rejects=0, repairs=0):
    case = make(id=f"c-{next(_ids)}", group=group, known_gap=gap, **({"reply": reply, "llm": [{"say": "x"}]} if reply != "none" else {}))
    return Result(case, Observed(outcome=outcome, rejects=rejects, repairs=repairs), [] if passed else ["wrong"])


LIMITS = {"min_cases": 1, "max_cases": 10, "skipped_max": 0, "pass_rate": {"overall": 1.0, "per_group": {}}, "min_cases_per_group": {}, "bad_replies_caught": 1.0,
          "good_replies_wrongly_rejected_max": 0, "known_gaps_max": 1, "fallback_rate_max": 0.5, "repairs_min": 0}


def test_the_summary_counts_what_the_gate_judges():
    gap = result("ambiguous", gap="a real defect", passed=False)
    results = [result(), result(passed=False), result("agent_bad", reply="bad", outcome="repaired", repairs=1, rejects=1), result("agent_bad", passed=False, reply="bad", outcome="accepted"),
               result("agent_good", reply="good", outcome="repaired"), result("agent_good", reply="good", outcome="accepted"), gap]
    s = summarise(results)
    assert (s["cases"], s["counted"], s["met"]) == (7, 6, 4) and s["pass_rate"] == pytest.approx(4 / 6)
    assert s["groups"]["item_stock"] == {"cases": 2, "counted": 2, "met": 1, "rate": 0.5}
    assert s["groups"]["ambiguous"]["cases"] == 1 and s["groups"]["ambiguous"]["counted"] == 0 and s["groups"]["ambiguous"]["rate"] == 1.0         # a known gap covers its question but is not a miss
    assert s["bad_replies"] == {"cases": 2, "caught": 1} and s["good_replies"] == {"cases": 2, "wrongly_rejected": 1}
    assert s["model"]["cases"] == 4 and s["model"]["repaired"] == 2 and s["model"]["accepted"] == 2 and s["known_gaps"] == [gap.case.id]


def test_the_gate_says_which_limit_was_missed():
    ok = [result(), result("agent_bad", reply="bad", outcome="repaired")]
    assert gate(summarise(ok), LIMITS) == []
    assert any("overall pass rate 50.0%" in p for p in gate(summarise([result(), result(passed=False)]), LIMITS))
    assert any("bad model replies caught: 0 of 1" in p for p in gate(summarise([result("agent_bad", passed=False, reply="bad", outcome="accepted")]), LIMITS))
    assert any("good model replies were rejected" in p for p in gate(summarise([result("agent_good", reply="good", outcome="fallback")]), LIMITS))
    assert any("2 known gaps" in p for p in gate(summarise([result(gap="a", passed=False), result("ambiguous", gap="b", passed=False)]), LIMITS))
    assert any("known gap fixed" in p for p in gate(summarise([result(gap="a", passed=True)]), LIMITS))
    assert any("1 cases: the suite is meant to hold 5 to 10" in p for p in gate(summarise([result()]), {**LIMITS, "min_cases": 5}))
    assert any("at least 3 are needed" in p for p in gate(summarise(ok), {**LIMITS, "min_cases_per_group": {"item_stock": 3}}))
    assert any("fallback" in p or "rules' answer replaced" in p for p in gate(summarise([result("agent_bad", reply="bad", outcome="fallback")]), {**LIMITS, "fallback_rate_max": 0.1}))
    assert any("repairs" in p for p in gate(summarise(ok), {**LIMITS, "repairs_min": 3}))
    skipped = [Result(make(), skipped="needs the optional agents extra")]
    assert any("skipped" in p for p in gate(summarise(skipped), LIMITS))


def test_a_known_gap_counts_as_met_only_while_it_still_fails():
    assert result(gap="x", passed=False).counts and not result(gap="x", passed=True).counts
    assert result(passed=True).counts and not result(passed=False).counts


def test_the_report_and_the_comparison_say_what_happened():
    s = summarise([result(), result("agent_bad", reply="bad", outcome="repaired", repairs=1, rejects=1)])
    text = markdown(s, ["something is under its limit"])
    assert "**2 of 2 cases pass**" in text and "| agent_bad | 1 | 1 of 1 | 100% |" in text and "**Gate: FAILED**" in text and "- something is under its limit" in text
    assert "**Gate: passed**" in markdown(s, [])
    table = comparison([("model-a", s), ("model-b", summarise([result(passed=False)]))])
    assert table.splitlines()[2].startswith("| | model-a | model-b |") and "| Right answers | 2 (100%) | 0 (0%) |" in table


# ------------------------------------------------------------------ the command line
def test_the_command_line_lists_shows_and_refuses_a_partial_check(monkeypatch, capsys):
    cases = [make(id="one", expect={"intent": "item_stock"}), make(id="two", group="stock_overview", question="What is running low?", expect={"intent": "stock_overview"})]
    monkeypatch.setattr(cli, "load_cases", lambda: cases)
    assert cli.main(["--list"]) == 0 and "one" in capsys.readouterr().out
    assert cli.main(["--show", "two"]) == 0 and "meets the case" in capsys.readouterr().out
    assert cli.main(["--show", "nope"]) == 2
    assert cli.main(["--case", "one"]) == 0
    assert cli.main(["--check", "--case", "one"]) == 2 and "needs the whole suite" in capsys.readouterr().out
    assert cli.main(["--group", "ambiguous"]) == 2


def test_the_command_line_gives_logging_back_when_it_is_done(monkeypatch):
    """It silences the application's start-up lines while it runs; it used to leave them silenced for the rest of the process (a whole test session)."""
    import logging
    monkeypatch.setattr(cli, "load_cases", lambda: [make(expect={"intent": "item_stock"})])
    before = logging.root.manager.disable
    assert cli.main(["--list"]) == 0 and cli.main(["--case", "case-one"]) == 0
    assert logging.root.manager.disable == before


def test_a_broken_case_file_is_reported_in_words_not_a_traceback(monkeypatch, capsys):
    def broken():
        raise ValueError("x.yaml: not valid YAML")
    monkeypatch.setattr(cli, "load_cases", broken)
    assert cli.main([]) == 2 and "the cases cannot be loaded: x.yaml" in capsys.readouterr().out


def test_the_report_is_appended_to_a_summary_file_and_the_summary_written_as_json(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_cases", lambda: [make(expect={"intent": "item_stock"})])
    out, js = tmp_path / "summary.md", tmp_path / "summary.json"
    out.write_text("earlier step\n", encoding="utf-8")
    assert cli.main(["--markdown", str(out), "--json", str(js)]) == 0
    assert out.read_text(encoding="utf-8").startswith("earlier step\n") and "AI evaluation" in out.read_text(encoding="utf-8")
    assert json.loads(js.read_text(encoding="utf-8"))["met"] == 1


def test_yaml_text_is_the_format_a_person_writes():
    text = textwrap.dedent("""
        cases:
          - id: plain
            group: item_stock
            question: How much BOLT-M8 do we have?
            expect:
              must_include: ["300 pcs"]
    """)
    import yaml
    assert Case.model_validate(yaml.safe_load(text)["cases"][0]).expect.must_include == ["300 pcs"]

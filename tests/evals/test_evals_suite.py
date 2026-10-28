"""The deterministic AI evaluation: every golden case in tests/evals/cases/*.yaml is a test, and the gate over all of them is one more.

No model, no ERPNext, no network: the application is asked each question where a person would ask it, a scripted stand-in plays the language model, and what comes back
is compared with what the case says a correct answer must and must not contain. A failure names the case; `python -m tools.evals --show <id>` prints what the application said.
How to add a case, and what the gate is: docs/EVALUATION.md."""
import pytest

from tools.evals.cases import ENTRIES, GROUPS, load_cases
from evals_support import baseline  # noqa: F401 - the session fixture
from tools.evals.metrics import gate, load_thresholds, summarise

CASES = load_cases()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_case(case, baseline):
    result = baseline.result(case)
    if result.skipped:
        pytest.skip(result.skipped)
    if case.known_gap:
        if result.passed:
            pytest.fail("this case passes now: remove its known_gap line, and lower known_gaps_max in tests/evals/thresholds.json")
        pytest.xfail(case.known_gap)
    assert result.passed, "\n".join(result.failures) + f"\n(python -m tools.evals --show {case.id})"


def test_the_gate_over_all_the_cases(baseline):
    results = baseline.all()
    if any(r.skipped for r in results):
        pytest.skip("some cases need the optional 'agents' extra: pip install \".[agents]\"")
    problems = gate(summarise(results), load_thresholds())
    assert not problems, "the evaluation is under its limits (tests/evals/thresholds.json):\n- " + "\n- ".join(problems)


def test_the_limits_file_names_real_groups_and_every_group_has_a_minimum():
    limits = load_thresholds()
    assert set(limits["min_cases_per_group"]) == set(GROUPS)
    assert set(limits["pass_rate"]["per_group"]) <= set(GROUPS)
    assert {"min_cases", "max_cases", "skipped_max", "pass_rate", "bad_replies_caught", "good_replies_wrongly_rejected_max", "known_gaps_max", "fallback_rate_max", "repairs_min"} <= set(limits)


def test_the_cases_cover_every_way_in_and_the_five_kinds_of_question():
    assert {c.entry for c in CASES} == set(ENTRIES)
    intents = {c.expect.intent for c in CASES if c.expect.intent}
    assert {"item_stock", "stock_overview", "vendor_compare", "document_status", "pending_approvals", "decision_history"} <= intents
    assert {c.asker for c in CASES} == {"viewer", "approver"}
    assert any(c.known_gap for c in CASES) is (load_thresholds()["known_gaps_max"] > 0)          # the limit on known gaps is not left looser than the gaps that exist

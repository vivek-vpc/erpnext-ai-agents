"""The deterministic AI evaluation: every golden case in tests/evals/cases/*.yaml is a test, and the gate over all of them is one more.

No model, no ERPNext, no network: the application is asked each question where a person would ask it, a scripted stand-in plays the language model, and what comes back
is compared with what the case says a correct answer must and must not contain. A failure names the case; `python -m tools.evals --show <id>` prints what the application said.
How to add a case, and what the gate is: docs/EVALUATION.md."""
import pytest

from tools.evals.cases import load_cases
from evals_support import baseline  # noqa: F401 - the session fixture

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

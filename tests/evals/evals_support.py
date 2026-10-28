"""The golden cases are run once per test session and shared: the per-case tests, the gate and the mutation checks all read the same results.
(Not a conftest.py: a second file of that name would hide the root one from `from conftest import ...` in the other tests.)"""
import pytest

from tools.evals.cases import load_cases
from tools.evals.runner import Result, run_case


class Baseline:
    def __init__(self) -> None:
        self.cases = load_cases()
        self._results: dict[str, Result] = {}

    def result(self, case) -> Result:
        if case.id not in self._results:
            self._results[case.id] = run_case(case)
        return self._results[case.id]

    def all(self) -> list[Result]:
        return [self.result(c) for c in self.cases]


@pytest.fixture(scope="session")
def baseline() -> Baseline:
    return Baseline()

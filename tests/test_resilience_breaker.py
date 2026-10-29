"""The circuit breaker (core/resilience.py): stop asking a service that keeps failing, say so once, try again after a pause, close when it answers (Phase 3 of the hardening brief)."""
import logging
import threading

import pytest

from erp_agents.core import metrics, resilience
from erp_agents.core.resilience import BreakerOpen, BreakerState, CircuitBreaker, MemoryBreakerStore


class Time:
    """A clock the test moves by hand (seconds)."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def time_():
    return Time()


def breaker(time_, failures=3, pause=30.0, store=None, name="erpnext"):
    return CircuitBreaker(name, "ERPNext", failures, pause, clock=time_, store=store)


def fail(b, n=1):
    for _ in range(n):
        b.allow()
        b.record_failure("ConnectionError")


@pytest.fixture
def events(caplog):
    caplog.set_level(logging.INFO, logger="erp_agents.calls")

    def lines():
        return [r.getMessage() for r in caplog.records if r.name == "erp_agents.calls" and r.getMessage().startswith("circuit_change")]
    return lines


# ------------------------------------------------------------------ closed -> open
def test_it_opens_after_the_set_number_of_failures_in_a_row_and_not_before(time_):
    b = breaker(time_, failures=3)
    fail(b, 2)
    assert b.state == resilience.CLOSED
    b.allow()                                                   # still asking
    b.record_failure("ConnectionError")
    assert b.state == resilience.OPEN


def test_an_answer_in_between_starts_the_count_again(time_):
    b = breaker(time_, failures=3)
    fail(b, 2)
    b.allow()
    b.record_success()
    fail(b, 2)
    assert b.state == resilience.CLOSED


def test_while_open_it_refuses_at_once_and_says_when_to_try_again(time_):
    b = breaker(time_, failures=2, pause=30)
    fail(b, 2)
    time_.advance(10)
    with pytest.raises(BreakerOpen) as refused:
        b.allow()
    assert 19 < refused.value.retry_in <= 20 and "ERPNext is not answering" in str(refused.value) and "20 seconds" in str(refused.value)
    time_.advance(5)
    with pytest.raises(BreakerOpen) as again:
        b.allow()
    assert again.value.retry_in < refused.value.retry_in


# ------------------------------------------------------------------ open -> half open -> closed or open
def test_after_the_pause_exactly_one_probe_is_let_through(time_):
    b = breaker(time_, failures=2, pause=30)
    fail(b, 2)
    time_.advance(30)
    b.allow()                                                   # the probe
    assert b.state == resilience.HALF_OPEN
    with pytest.raises(BreakerOpen):
        b.allow()                                               # everybody else waits for its verdict


def test_a_probe_that_succeeds_closes_it_and_calls_flow_again(time_):
    b = breaker(time_, failures=2, pause=30)
    fail(b, 2)
    time_.advance(31)
    b.allow()
    b.record_success()
    assert b.state == resilience.CLOSED
    b.allow()
    b.allow()                                                   # no limit on closed


def test_a_probe_that_fails_opens_it_again_for_a_whole_new_pause(time_):
    b = breaker(time_, failures=2, pause=30)
    fail(b, 2)
    time_.advance(31)
    b.allow()
    b.record_failure("ReadTimeout")
    assert b.state == resilience.OPEN
    time_.advance(29)
    with pytest.raises(BreakerOpen):
        b.allow()
    time_.advance(2)
    b.allow()                                                   # the next probe
    assert b.state == resilience.HALF_OPEN


def test_a_probe_nobody_reported_on_is_replaced_after_another_pause(time_):
    b = breaker(time_, failures=1, pause=30)
    fail(b)
    time_.advance(31)
    b.allow()                                                   # this caller dies without reporting
    time_.advance(10)
    with pytest.raises(BreakerOpen):
        b.allow()
    time_.advance(25)
    b.allow()                                                   # a new probe, so the circuit cannot stay shut for ever


def test_an_error_that_says_nothing_about_the_service_frees_the_probe_without_counting(time_):
    b = breaker(time_, failures=2, pause=30)
    fail(b, 2)
    time_.advance(31)
    b.allow()
    b.record_neutral()                                          # for example a bad address in the settings
    assert b.state == resilience.HALF_OPEN
    b.allow()                                                   # another probe may go
    b.record_success()
    assert b.state == resilience.CLOSED


def test_a_late_failure_from_a_call_that_began_before_it_opened_changes_nothing(time_):
    b = breaker(time_, failures=1, pause=30)
    fail(b)
    time_.advance(10)
    b.record_failure("ConnectionError")                         # a straggler
    time_.advance(21)
    b.allow()                                                   # the pause was counted from the first opening


# ------------------------------------------------------------------ reported once
def test_each_change_is_one_log_line_and_refusals_are_not_lines(time_, events):
    b = breaker(time_, failures=2, pause=30)
    fail(b, 2)
    for _ in range(5):
        with pytest.raises(BreakerOpen):
            b.allow()
    time_.advance(31)
    b.allow()
    b.record_success()
    lines = events()
    assert len(lines) == 3, lines
    assert "dependency=erpnext state=open" in lines[0] and "state=half_open" in lines[1] and "state=closed" in lines[2]


def test_a_failure_streak_that_never_reaches_the_limit_is_silent(time_, events):
    b = breaker(time_, failures=5)
    fail(b, 4)
    b.allow()
    b.record_success()
    assert events() == []


@pytest.fixture
def metrics_on():
    metrics.REGISTRY.reset()
    metrics.configure(True)
    yield
    metrics.configure(False)
    metrics.REGISTRY.reset()


def test_the_state_the_changes_and_the_refusals_are_measured(time_, metrics_on):
    b = breaker(time_, failures=2, pause=30)
    assert resilience.STATE_NUMBER == {"closed": 0, "half_open": 1, "open": 2}
    fail(b, 2)
    assert metrics.CIRCUIT_STATE.values[("erpnext",)] == 2 and metrics.CIRCUIT_TRANSITIONS.values[("erpnext", "open")] == 1
    for _ in range(3):
        with pytest.raises(BreakerOpen):
            b.allow()
    assert metrics.CIRCUIT_REFUSED.values[("erpnext",)] == 3
    time_.advance(31)
    b.allow()
    b.record_success()
    assert metrics.CIRCUIT_STATE.values[("erpnext",)] == 0 and metrics.CIRCUIT_TRANSITIONS.values[("erpnext", "closed")] == 1


# ------------------------------------------------------------------ the state can live somewhere else
def test_two_workers_sharing_a_store_share_what_they_learn(time_):
    store = MemoryBreakerStore()
    one, two = breaker(time_, failures=2, store=store), breaker(time_, failures=2, store=store)
    fail(one)
    fail(two)                                                   # the second failure was seen by the other worker
    assert one.state == resilience.OPEN
    with pytest.raises(BreakerOpen):
        one.allow()


def test_a_store_only_has_to_offer_update_and_peek(time_):
    class Cache:
        """What a Frappe site would put in front of its cache: one atomic update, one read."""

        def __init__(self):
            self.data, self.calls = {}, 0

        def update(self, name, fn):
            self.calls += 1
            state = self.data.get(name) or BreakerState()
            out = fn(state)
            self.data[name] = state
            return out

        def peek(self, name):
            return self.data.get(name) or BreakerState()
    cache = Cache()
    b = breaker(time_, failures=1, store=cache)
    fail(b)
    assert cache.data["erpnext"].state == resilience.OPEN and cache.calls >= 2


def test_fifty_threads_failing_together_open_it_once(time_, events):
    b = breaker(time_, failures=10, pause=30)

    def worker():
        for _ in range(3):
            try:
                b.allow()
            except BreakerOpen:
                continue
            b.record_failure("ConnectionError")
    threads = [threading.Thread(target=worker) for _ in range(50)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert b.state == resilience.OPEN and len(events()) == 1


# ------------------------------------------------------------------ one breaker per dependency, from the settings
def test_each_dependency_has_its_own_breaker_with_its_own_limits(time_):
    s = resilience.ResilienceSettings(erpnext_failures=2, erpnext_pause_seconds=10, model_failures=4, model_pause_seconds=90)
    registry = resilience.Breakers(s, clock=time_)
    erp, llm = registry.get("erpnext"), registry.get("model:ollama")
    assert (erp.failures, erp.pause) == (2, 10) and (llm.failures, llm.pause) == (4, 90)
    assert registry.get("erpnext") is erp and llm.label == "the language model (ollama)" and erp.label == "ERPNext"
    fail(erp, 2)
    assert registry.states() == {"erpnext": "open", "model:ollama": "closed"}
    llm.allow()                                                 # the model is not affected by ERPNext being down

"""The per-person rate limits (core/resilience.py) and the `resilience:` settings (Phase 3 of the hardening brief)."""
import logging

import pytest

from erp_agents.core import metrics
from erp_agents.core.resilience import Limits, MemoryRateLimiter, RateLimited, ResilienceSettings, settings_from


class Time:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def time_():
    return Time()


# ------------------------------------------------------------------ the limiter
def test_the_n_plus_first_in_a_minute_is_refused_with_the_seconds_to_wait(time_):
    lim = MemoryRateLimiter(clock=time_)
    for _ in range(3):
        assert lim.hit("chat", "ana", 3, 60).allowed
        time_.advance(5)
    refused = lim.hit("chat", "ana", 3, 60)
    assert not refused.allowed and 44 < refused.retry_after <= 45                     # the first of the three was 15 seconds ago
    time_.advance(refused.retry_after + 0.1)
    assert lim.hit("chat", "ana", 3, 60).allowed


def test_a_refused_try_is_not_counted_so_waiting_is_enough(time_):
    lim = MemoryRateLimiter(clock=time_)
    for _ in range(2):
        lim.hit("chat", "ana", 2, 60)
    for _ in range(20):
        assert not lim.hit("chat", "ana", 2, 60).allowed                               # hammering does not extend the wait
    time_.advance(61)
    assert lim.hit("chat", "ana", 2, 60).allowed


def test_people_and_scopes_are_counted_apart(time_):
    lim = MemoryRateLimiter(clock=time_)
    assert lim.hit("chat", "ana", 1, 60).allowed and not lim.hit("chat", "ana", 1, 60).allowed
    assert lim.hit("chat", "ben", 1, 60).allowed                                       # another person
    assert lim.hit("model", "ana", 1, 60).allowed                                      # another kind of action


def test_the_table_does_not_grow_without_end(time_):
    lim = MemoryRateLimiter(clock=time_, max_keys=100)
    for i in range(500):
        lim.hit("chat", f"user{i}", 5, 60)
        time_.advance(0.01)
    time_.advance(120)
    lim.hit("chat", "late", 5, 60)
    assert lim.size() <= 101                                                           # everything that had gone quiet was forgotten


# ------------------------------------------------------------------ the limits of this application
def limits(time_, **kw):
    return Limits(ResilienceSettings(chat_per_minute=2, channel_chat_per_minute=1, model_per_minute=1, **kw), clock=time_)


def test_each_scope_has_the_limit_of_the_settings(time_):
    lim = limits(time_)
    lim.enforce("chat", "ana")
    lim.enforce("chat", "ana")
    with pytest.raises(RateLimited):
        lim.enforce("chat", "ana")
    lim.enforce("channel_chat", "whatsapp:ana")
    with pytest.raises(RateLimited):
        lim.enforce("channel_chat", "whatsapp:ana")
    lim.enforce("model", "ana")
    with pytest.raises(RateLimited):
        lim.enforce("model", "ana")


def test_the_message_names_the_wait_and_carries_it(time_):
    lim = limits(time_)
    lim.enforce("model", "ana")
    time_.advance(20)
    with pytest.raises(RateLimited) as refused:
        lim.enforce("model", "ana")
    exc = refused.value
    assert exc.scope == "model" and 39 < exc.retry_after <= 40
    assert str(exc) == "That is a lot of questions in a minute. Please wait 40 seconds."
    assert str(RateLimited("chat", 0.2)) == "That is a lot of questions in a minute. Please wait 1 second."


def test_an_unknown_scope_is_a_mistake_not_unlimited(time_):
    with pytest.raises(KeyError):
        limits(time_).enforce("chatt", "ana")


def test_a_refusal_is_one_line_and_one_count_and_never_the_text_asked(time_, caplog):
    metrics.REGISTRY.reset()
    metrics.configure(True)
    caplog.set_level(logging.INFO, logger="erp_agents.calls")
    try:
        lim = limits(time_)
        lim.enforce("model", "ana@example.test")
        for _ in range(2):
            with pytest.raises(RateLimited):
                lim.enforce("model", "ana@example.test")
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("rate_limited")]
        assert len(lines) == 2 and all("scope=model" in l for l in lines) and not any("ana@example.test" in l for l in lines)       # who is not written, only that it happened
        assert metrics.RATE_LIMITED.values[("model",)] == 2
    finally:
        metrics.configure(False)
        metrics.REGISTRY.reset()


# ------------------------------------------------------------------ the settings
def test_without_the_section_everything_is_as_decided():
    s = settings_from({})
    assert s == ResilienceSettings()
    assert (s.erpnext_failures, s.erpnext_pause_seconds, s.model_failures, s.model_pause_seconds) == (5, 30.0, 3, 60.0)
    assert (s.chat_per_minute, s.channel_chat_per_minute, s.model_per_minute, s.inbound_memory_days) == (20, 6, 6, 7)


def test_values_are_read_and_checked():
    s = settings_from({"resilience": {"erpnext_failures": 8, "model_pause_seconds": "90", "chat_per_minute": 30}})
    assert (s.erpnext_failures, s.model_pause_seconds, s.chat_per_minute) == (8, 90.0, 30)
    for bad, why in (({"erpnext_failures": 0}, "erpnext_failures"), ({"erpnext_failures": "many"}, "erpnext_failures"), ({"model_pause_seconds": 0}, "model_pause_seconds"),
                     ({"chat_per_minute": 100000}, "chat_per_minute"), ({"inbound_memory_days": 0}, "inbound_memory_days"), ({"erpnext_failure": 3}, "unknown setting"),
                     ({"chat_per_minute": True}, "chat_per_minute"), ("fast", "section")):
        with pytest.raises(ValueError, match=why):
            settings_from({"resilience": bad})


def test_the_application_refuses_to_start_on_a_wrong_value_and_says_which(make_app):
    from erp_agents.core.bootstrap import StartupRefused
    with pytest.raises(StartupRefused, match="resilience.chat_per_minute"):
        make_app(cfg={"resilience": {"chat_per_minute": 0}})

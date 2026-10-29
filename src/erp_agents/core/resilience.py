"""Resilience: stop asking a service that keeps failing, and stop a person asking too often (Phase 3 of the hardening review).

  * `CircuitBreaker`: closed (asking as usual) -> open after N failures in a row (not asking; every call fails at once and says when the next try is allowed) -> half open after a
    pause (ONE trial request) -> closed when it answers, open again for a new pause when it does not. One breaker per dependency: ERPNext, and each language-model provider.
    A change of state is one log line, a gauge and a counter, so a failing service is reported once and not once per call.
  * `RateLimiter` / `Limits`: at most N asks per person per minute for each kind of ask (chat, a chat message from a phone, a model-backed action). The refusal says how many
    seconds to wait.
  * `ResilienceSettings`: the `resilience:` section of the settings file, validated at start-up.

What decides is the state kept in a small STORE (memory here). A Frappe site with several workers would put the same few fields in its cache: nothing else changes. This module
imports no web, HTTP, scheduler or SQL library (the locks are only inside the in-memory stores); the part that knows about `requests` is `httpguard.py`.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, Protocol, TypeVar

from . import metrics, obs

log = logging.getLogger(__name__)
T = TypeVar("T")

CLOSED, HALF_OPEN, OPEN = "closed", "half_open", "open"
STATE_NUMBER = {CLOSED: 0, HALF_OPEN: 1, OPEN: 2}
WINDOW_SECONDS = 60.0                            # every limit is "per minute"


# ------------------------------------------------------------------------------------------------------------------------ settings
@dataclass(frozen=True)
class ResilienceSettings:
    """The `resilience:` section. Read once at start-up (a change needs a restart); without it everything is as written here."""
    erpnext_failures: int = 5                    # failed requests in a row that make the application stop asking ERPNext
    erpnext_pause_seconds: float = 30.0          # how long it stops before it tries one request again
    model_failures: int = 3                      # the same for a language model (a laptop model that is only slow still answers in the end, which resets the count)
    model_pause_seconds: float = 60.0
    chat_per_minute: int = 20                    # questions to the assistant per person per minute
    channel_chat_per_minute: int = 6             # messages from one WhatsApp or Telegram chat per minute that may reach the assistant
    model_per_minute: int = 6                    # actions per person per minute that start a language-model task (Ask why, an agent question, a brief, a review)
    inbound_memory_days: int = 7                 # how long the ids of channel messages are remembered, to recognise one that is delivered again


_BOUNDS = {"erpnext_failures": (1, 100), "erpnext_pause_seconds": (1.0, 3600.0), "model_failures": (1, 100), "model_pause_seconds": (1.0, 3600.0),
           "chat_per_minute": (1, 1000), "channel_chat_per_minute": (1, 1000), "model_per_minute": (1, 1000), "inbound_memory_days": (1, 90)}


def settings_from(cfg: dict[str, Any]) -> ResilienceSettings:
    raw = cfg.get("resilience") or {}
    if not isinstance(raw, dict):
        raise ValueError("resilience: write it as a section with keys such as erpnext_failures and chat_per_minute")
    unknown = sorted(set(raw) - set(ResilienceSettings.__dataclass_fields__))
    if unknown:
        raise ValueError(f"resilience: unknown setting(s) {', '.join(map(str, unknown))}; known: {', '.join(ResilienceSettings.__dataclass_fields__)}")
    values: dict[str, Any] = {}
    for key, (low, high) in _BOUNDS.items():
        if key not in raw:
            continue
        value = raw[key]
        whole = isinstance(getattr(ResilienceSettings, key), int)
        try:
            if isinstance(value, bool):
                raise ValueError
            number = int(value) if whole else float(value)
            if whole and float(value) != number:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"resilience.{key} must be a {'whole ' if whole else ''}number from {low:g} to {high:g}") from None
        if not low <= number <= high:
            raise ValueError(f"resilience.{key} must be a {'whole ' if whole else ''}number from {low:g} to {high:g}")
        values[key] = number
    return replace(ResilienceSettings(), **values)


# ------------------------------------------------------------------------------------------------------------------------ the circuit breaker
@dataclass
class BreakerState:
    state: str = CLOSED
    failures: int = 0                            # failures in a row while closed
    since: float = 0.0                           # when it opened, or when the trial request was let through (the clock's seconds)
    probing: bool = False                        # a trial request is out and has not reported yet


class BreakerStore(Protocol):
    """Where the state lives. `update` runs `fn(state)` on the named breaker's state as ONE atomic step and keeps what `fn` changed; `peek` reads a copy. A cache can do this."""

    def update(self, name: str, fn: Callable[[BreakerState], T]) -> T: ...

    def peek(self, name: str) -> BreakerState: ...


class MemoryBreakerStore:
    def __init__(self) -> None:
        self._states: dict[str, BreakerState] = {}
        self._lock = threading.Lock()

    def update(self, name: str, fn: Callable[[BreakerState], T]) -> T:
        with self._lock:
            return fn(self._states.setdefault(name, BreakerState()))

    def peek(self, name: str) -> BreakerState:
        with self._lock:
            return replace(self._states.get(name) or BreakerState())


class BreakerOpen(RuntimeError):
    """Not sent: the dependency has been failing. `retry_in`: seconds until a trial request is allowed."""

    def __init__(self, message: str, retry_in: float) -> None:
        super().__init__(message)
        self.retry_in = retry_in


class CircuitBreaker:
    def __init__(self, name: str, label: str, failures: int, pause: float, clock: Callable[[], float] = time.monotonic, store: Optional[BreakerStore] = None) -> None:
        self.name, self.label, self.failures, self.pause, self.clock = name, label, failures, pause, clock
        self.store: BreakerStore = store or MemoryBreakerStore()

    @property
    def state(self) -> str:
        return self.store.peek(self.name).state

    # ---------------------------------------------------------------- before a request
    def allow(self) -> None:
        """Let a request through, or raise BreakerOpen. The first request after the pause is the trial; the others wait for its verdict."""
        def step(s: BreakerState) -> tuple[Optional[float], Optional[tuple[str, str, int]]]:
            now = self.clock()
            if s.state == CLOSED:
                return None, None
            waited = now - s.since
            if s.state == OPEN:
                if waited >= self.pause:
                    s.state, s.since, s.probing = HALF_OPEN, now, True
                    return None, (HALF_OPEN, "the pause is over, one trial request is let through", s.failures)
                return self.pause - waited, None
            if s.probing and waited < self.pause:        # the trial request is still out
                return self.pause - waited, None
            s.since, s.probing = now, True               # half open and nobody is trying (or the trial was never reported on): this request is the trial
            return None, None
        refused, change = self.store.update(self.name, step)
        if change:
            self._announce(*change)
        if refused is not None:
            metrics.CIRCUIT_REFUSED.inc(dependency=self.name)
            wait = max(1, math.ceil(refused))
            raise BreakerOpen(f"{self.label} is not answering (the last requests failed), so this was not sent. The next try is allowed in {wait} second{'s' if wait != 1 else ''}.", refused)

    # ---------------------------------------------------------------- after it
    def record_success(self) -> None:
        def step(s: BreakerState) -> Optional[tuple[str, str, int]]:
            if s.state == CLOSED:
                s.failures = 0
                return None
            if s.state == HALF_OPEN:
                s.state, s.failures, s.probing = CLOSED, 0, False
                return CLOSED, "the trial request was answered", 0
            return None                                  # open: a call that began before it opened and ended well says nothing new
        change = self.store.update(self.name, step)
        if change:
            self._announce(*change)

    def record_failure(self, reason: str) -> None:
        def step(s: BreakerState) -> Optional[tuple[str, str, int]]:
            now = self.clock()
            if s.state == CLOSED:
                s.failures += 1
                if s.failures >= self.failures:
                    s.state, s.since, s.probing = OPEN, now, False
                    return OPEN, reason, s.failures
                return None
            if s.state == HALF_OPEN:
                s.state, s.since, s.probing = OPEN, now, False
                return OPEN, reason, s.failures
            return None                                  # already open: a straggler that began before it opened
        change = self.store.update(self.name, step)
        if change:
            self._announce(*change)

    def record_neutral(self) -> None:
        """The request failed for a reason that says nothing about the service (a mistake in our own settings): if it was the trial, let another one try."""
        def step(s: BreakerState) -> None:
            if s.state == HALF_OPEN:
                s.probing = False
        self.store.update(self.name, step)

    def snapshot(self) -> dict[str, Any]:
        s = self.store.peek(self.name)
        retry = max(0.0, self.pause - (self.clock() - s.since)) if s.state == OPEN else 0.0
        return {"state": s.state, "failures": s.failures, "retry_in": round(retry, 1)}

    def _announce(self, to: str, reason: str, failures: int) -> None:
        """One line, one gauge and one count for each change of state: a failing service is reported once, not once per call."""
        obs.event("circuit_change", logging.WARNING if to == OPEN else logging.INFO, dependency=self.name, state=to, failures=failures, reason=str(reason)[:60])
        metrics.CIRCUIT_STATE.set(STATE_NUMBER[to], dependency=self.name)
        metrics.CIRCUIT_TRANSITIONS.inc(dependency=self.name, to=to)


class Breakers:
    """One breaker per dependency, made on first use with the limits of the settings."""

    def __init__(self, settings: ResilienceSettings, clock: Callable[[], float] = time.monotonic, store: Optional[BreakerStore] = None) -> None:
        self.settings, self.clock = settings, clock
        self.store: BreakerStore = store or MemoryBreakerStore()
        self._made: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> CircuitBreaker:
        with self._lock:
            made = self._made.get(name)
            if made is None:
                s = self.settings
                if name == "erpnext":
                    made = CircuitBreaker(name, "ERPNext", s.erpnext_failures, s.erpnext_pause_seconds, self.clock, self.store)
                else:                                    # "model:<provider>"
                    made = CircuitBreaker(name, f"the language model ({name.split(':', 1)[-1]})", s.model_failures, s.model_pause_seconds, self.clock, self.store)
                self._made[name] = made
            return made

    def states(self) -> dict[str, str]:
        with self._lock:
            return {name: b.state for name, b in sorted(self._made.items())}


BREAKERS = Breakers(ResilienceSettings())        # replaced by configure() when the application is built; sessions look it up at each request


def configure(settings: ResilienceSettings, clock: Callable[[], float] = time.monotonic, store: Optional[BreakerStore] = None) -> Breakers:
    """Start with fresh breakers and these limits (called by build_app, and by a test that wants its own clock)."""
    global BREAKERS
    BREAKERS = Breakers(settings, clock, store)
    return BREAKERS


# ------------------------------------------------------------------------------------------------------------------------ rate limits
@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after: float = 0.0                     # seconds until one more ask would be allowed


class RateLimiter(Protocol):
    """Count one ask by `key` in `scope` against `limit` per `window` seconds. A cache (Frappe's rate limiter) can do this."""

    def hit(self, scope: str, key: str, limit: int, window: float) -> RateDecision: ...


class MemoryRateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 5000) -> None:
        self.clock, self.max_keys = clock, max_keys
        self._hits: dict[tuple[str, str], tuple[deque, float]] = {}
        self._lock = threading.Lock()

    def hit(self, scope: str, key: str, limit: int, window: float) -> RateDecision:
        now = self.clock()
        with self._lock:
            q = self._hits[(scope, key)][0] if (scope, key) in self._hits else deque()
            while q and now - q[0] >= window:
                q.popleft()
            if len(q) >= limit:
                self._hits[(scope, key)] = (q, window)
                return RateDecision(False, window - (now - q[0]))     # a refused ask is not counted: waiting is enough
            q.append(now)
            self._hits[(scope, key)] = (q, window)
            if len(self._hits) > self.max_keys:                       # a flood of different people: forget whoever has been quiet
                for k in [k for k, (hits, w) in self._hits.items() if not hits or now - hits[-1] >= w]:
                    del self._hits[k]
            return RateDecision(True)

    def size(self) -> int:
        with self._lock:
            return len(self._hits)


class RateLimited(Exception):
    """A person asked too often. `str(exc)` is what they are told."""

    def __init__(self, scope: str, retry_after: float) -> None:
        self.scope, self.retry_after = scope, retry_after
        wait = max(1, math.ceil(retry_after))
        super().__init__(f"That is a lot of questions in a minute. Please wait {wait} second{'s' if wait != 1 else ''}.")


class Limits:
    """The limits of this application: one per kind of ask, from the settings."""

    def __init__(self, settings: ResilienceSettings, limiter: Optional[RateLimiter] = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.limiter: RateLimiter = limiter or MemoryRateLimiter(clock)
        self.per_minute = {"chat": settings.chat_per_minute, "channel_chat": settings.channel_chat_per_minute, "model": settings.model_per_minute}

    def enforce(self, scope: str, key: str) -> None:
        """Count one ask, or raise RateLimited. The line in the log and the count say that it happened, never who asked or what."""
        decision = self.limiter.hit(scope, str(key), self.per_minute[scope], WINDOW_SECONDS)
        if not decision.allowed:
            obs.event("rate_limited", logging.INFO, scope=scope, wait_seconds=math.ceil(decision.retry_after))
            metrics.RATE_LIMITED.inc(scope=scope)
            raise RateLimited(scope, decision.retry_after)

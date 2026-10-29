"""The circuit breaker on a `requests` session (an edge: the one place that knows what a failed HTTP request looks like).

`guard_session(session, "erpnext")` wraps `session.request` once, like `obs.observe_session`. Before each request it asks the dependency's breaker (core/resilience.py): if the
service has been failing, the request is not sent and `CircuitOpen` is raised at once. After the request it tells the breaker how it went.

What counts as the SERVICE being down: no connection, a timeout, a broken TLS handshake or body, and an answer of 500 or above. What does NOT: any other answer (a 404 is a
missing record, a 401 a wrong key, a 429 a quota: the service is up and said no) and a mistake in our own request (a bad address, a bad header, a redirect loop).

`CircuitOpen` is a `requests.ConnectionError`, so every `except requests.RequestException` that already handles "could not reach it" handles "did not even try" the same way.
"""
from __future__ import annotations

from typing import Any

import requests

from . import resilience

COUNTED = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError)


class CircuitOpen(requests.ConnectionError):
    """The request was not sent: the service has been failing. `retry_in`: seconds until a trial request is allowed. Its message is a sentence for people, so a screen that
    shows an error shows it as it is (`shown_as_is`), without the name of the exception in front."""

    shown_as_is = True

    def __init__(self, message: str, retry_in: float = 0.0) -> None:
        super().__init__(message)
        self.retry_in = retry_in


def means_service_down(exc: BaseException) -> bool:
    return isinstance(exc, COUNTED) and not isinstance(exc, CircuitOpen)


def guard_session(session: Any, name: str) -> Any:
    """Change `session` in place and return it. A session without a `request` method (a test double) is returned as it is."""
    original = getattr(session, "request", None)
    if not callable(original) or getattr(session, "_erp_agents_guarded", False):
        return session

    def request(method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        breaker = resilience.BREAKERS.get(name)            # looked up at each request: build_app starts with fresh breakers
        try:
            breaker.allow(manual=resilience.manual_trial_active())
        except resilience.BreakerOpen as exc:
            raise CircuitOpen(str(exc), exc.retry_in) from None
        try:
            response = original(method, url, *args, **kwargs)
        except BaseException as exc:
            if means_service_down(exc):
                breaker.record_failure(type(exc).__name__)
            else:
                breaker.record_neutral()                    # a mistake in our own request, or the program stopping: not the service's fault
            raise
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and status >= 500:
            breaker.record_failure(f"HTTP {status}")
        else:
            breaker.record_success()
        return response
    session.request = request
    session._erp_agents_guarded = True
    return session

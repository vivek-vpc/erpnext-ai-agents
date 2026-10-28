"""Optional OpenTelemetry tracing: where the time of one agent task went, as spans an observability tool can draw.

OFF by default (`observability.tracing: false`). Off, nothing here imports OpenTelemetry, `span()` costs one `if`, and no network connection is made. On, it needs the extra
packages (`pip install "erpnext-ai-agents[otel]"`) and sends the spans over OTLP/HTTP to `observability.otlp_endpoint`.

The spans, in a tree, one trace per agent task:

    agent.task                      agent, task, task_id, status, source
      llm.call                      provider, model, kind (gather, answer, repair), tokens_in, tokens_out, outcome
      tool.call                     tool, via, ok, refused
        erp.request                 method, kind (the ERPNext document type or method), status

What they carry: names, outcomes and counts, as in the log lines. NEVER a question, an answer, a prompt, a document, an item, a vendor, a person, a phone number, a query string or the
text of an error: a failure is the CLASS of the exception (`error.type`), and the span is marked as an error without a message. Values are cut to 120 characters, and anything that is
not a string, a number or a true/false is left out. Spans leave this machine only when `otlp_endpoint` says where.

No web, scheduler or SQL import here, and nothing from obs.py (obs.py imports this module).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

MAX_VALUE = 120
DEFAULT_SERVICE = "erpnext-ai-agents"


class TracingUnavailable(RuntimeError):
    """Tracing is switched on but OpenTelemetry is not installed (or the exporter cannot be set up)."""


class _State:
    """What is needed while tracing is on. Kept in one object so that switching it off is one assignment."""

    def __init__(self, provider: Any, tracer: Any, status: Any, status_code: Any, current_span: Any) -> None:
        self.provider, self.tracer, self.Status, self.StatusCode, self.current_span = provider, tracer, status, status_code, current_span


_state: Optional[_State] = None


class Handle:
    """What `span()` hands back: add the outcome to the span once it is known."""

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    def set(self, **attrs: Any) -> None:
        _set(self._span, attrs)

    def fail(self, error_type: str) -> None:
        """Mark the span as an error, by the class of the failure and no text."""
        _fail(self._span, error_type)


class _Off:
    """The handle when tracing is off: every call does nothing."""

    __slots__ = ()

    def set(self, **attrs: Any) -> None:
        pass

    def fail(self, error_type: str) -> None:
        pass


OFF = _Off()


def enabled() -> bool:
    return _state is not None


def _safe(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_VALUE]
    return None


def _set(span: Any, attrs: dict[str, Any]) -> None:
    for key, value in attrs.items():
        safe = _safe(value)
        if safe is not None:
            span.set_attribute(key, safe)


def _fail(span: Any, error_type: str) -> None:
    state = _state
    if state is None:
        return
    span.set_attribute("error.type", str(error_type)[:MAX_VALUE])
    span.set_status(state.Status(state.StatusCode.ERROR))


def configure(enabled_: bool, endpoint: str = "", service_name: str = DEFAULT_SERVICE, exporter: Any = None) -> None:
    """Switch tracing on or off (the `observability.tracing` setting). `exporter`: a span exporter to use instead of OTLP (the tests pass an in-memory one); it is then used
    without batching, so a test can read the spans at once. Raises TracingUnavailable when it is on and the packages are missing."""
    global _state
    previous, _state = _state, None
    if previous is not None:
        try:
            previous.provider.shutdown()
        except Exception:  # noqa: BLE001 - a tracing provider that cannot flush must not stop the application
            log.warning("the tracing provider did not shut down cleanly", exc_info=True)
    if not enabled_:
        return
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        from opentelemetry.trace import Status, StatusCode, get_current_span
        if exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
            processor: Any = BatchSpanProcessor(exporter)
        else:
            processor = SimpleSpanProcessor(exporter)
    except ImportError:
        raise TracingUnavailable('observability.tracing is on, but the OpenTelemetry packages are not installed. Install them with:  pip install "erpnext-ai-agents[otel]"  '
                                 "(or set observability.tracing to false)") from None
    provider = TracerProvider(resource=Resource.create({"service.name": service_name or DEFAULT_SERVICE}))      # our own provider: nothing global is changed
    provider.add_span_processor(processor)
    _state = _State(provider, provider.get_tracer("erp_agents"), Status, StatusCode, get_current_span)


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    """A span around the block (a child of the one that is current). Off: does nothing. An exception that leaves the block marks the span as an error by its class, and goes on."""
    state = _state
    if state is None:
        yield OFF
        return
    with state.tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False) as sp:
        _set(sp, attrs)
        try:
            yield Handle(sp)
        except BaseException as exc:
            _fail(sp, type(exc).__name__)
            raise


def annotate(**attrs: Any) -> None:
    """Add to the span that is current (a call from deep inside the code that knows the outcome). Off, or no span current: nothing."""
    state = _state
    if state is None:
        return
    current = state.current_span()
    if current.is_recording():
        _set(current, attrs)


def mark_failed(error_type: str) -> None:
    """Mark the current span as an error by the class of the failure (a tool call that returned an error instead of raising one)."""
    state = _state
    if state is None:
        return
    current = state.current_span()
    if current.is_recording():
        _fail(current, error_type)

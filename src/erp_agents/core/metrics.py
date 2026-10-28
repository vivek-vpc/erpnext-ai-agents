"""Metrics in the Prometheus text format, with no library behind them: a counter, a gauge, a histogram and a renderer are a page of code, and a monitoring client is not
worth a dependency for that.

Off by default (`observability.metrics: false`): then nothing is recorded, the recording functions return at once, and GET /metrics does not exist. On, there are two kinds of series:

  * events counted as they happen, in memory (a model call, a tool call, an ERPNext request, a rejected reply, a scheduled job): they start again from zero when the process
    restarts, which is what Prometheus expects of a counter (it reads the drop as a reset);
  * the state the database holds, read when Prometheus asks (approvals waiting and for how long, decisions by outcome, deliveries, agent tasks and tokens): these survive a restart.

Every label has a bounded set of values (a tool name, an outcome, a model name), and each metric stops accepting new label combinations after `MAX_SERIES`; nothing personal is a
label or a value: no item, vendor, person, phone number or text. No web or scheduler library is imported here (docs/DEVELOPING.md).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable, Optional

from . import obs

log = logging.getLogger(__name__)
MAX_SERIES = 300                                   # label combinations one metric will hold
SECONDS = (0.005, 0.025, 0.1, 0.5, 1, 2.5, 5, 15, 30, 60, 120, 300, 900)
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
SCRAPER_ENV = "ERP_AGENTS_METRICS_TOKEN"            # the variable that holds the bearer token a scraper presents (24 characters or more); an administrator's sign-in works too


def label(value: Any, limit: int = 60) -> str:
    """A label value that is safe to write and cannot grow without bound."""
    return obs.scrub(" ".join(str(value if value is not None else "").split()))[:limit]


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _number(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(float(v))


class _Metric:
    kind = ""

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...]) -> None:
        self.name, self.help, self.labels = name, help_text, labels
        self._lock = threading.Lock()
        self.values: dict[tuple[str, ...], Any] = {}
        self.dropped = 0

    def _key(self, labels: dict[str, Any]) -> Optional[tuple[str, ...]]:
        key = tuple(label(labels.get(n, "")) for n in self.labels)
        if key not in self.values and len(self.values) >= MAX_SERIES:
            self.dropped += 1
            return None
        return key

    def samples(self) -> Iterable[tuple[str, dict[str, str], float]]:  # pragma: no cover - overridden
        return ()


class Counter(_Metric):
    kind = "counter"

    def inc(self, amount: float = 1, **labels: Any) -> None:
        if not REGISTRY.enabled:
            return
        with self._lock:
            key = self._key(labels)
            if key is not None:
                self.values[key] = self.values.get(key, 0) + amount

    def samples(self) -> Iterable[tuple[str, dict[str, str], float]]:
        with self._lock:
            return [(self.name, dict(zip(self.labels, k)), v) for k, v in sorted(self.values.items())]


class Gauge(_Metric):
    kind = "gauge"

    def set(self, value: float, **labels: Any) -> None:
        if not REGISTRY.enabled:
            return
        with self._lock:
            key = self._key(labels)
            if key is not None:
                self.values[key] = value

    def samples(self) -> Iterable[tuple[str, dict[str, str], float]]:
        with self._lock:
            return [(self.name, dict(zip(self.labels, k)), v) for k, v in sorted(self.values.items())]


class Histogram(_Metric):
    kind = "histogram"

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...], buckets: tuple[float, ...] = SECONDS) -> None:
        super().__init__(name, help_text, labels)
        self.buckets = tuple(sorted(buckets))

    def observe(self, value: float, **labels: Any) -> None:
        if not REGISTRY.enabled:
            return
        with self._lock:
            key = self._key(labels)
            if key is None:
                return
            counts, total, n = self.values.get(key, ([0] * len(self.buckets), 0.0, 0))
            counts = [c + (1 if value <= b else 0) for c, b in zip(counts, self.buckets)]
            self.values[key] = (counts, total + value, n + 1)

    def samples(self) -> Iterable[tuple[str, dict[str, str], float]]:
        out: list[tuple[str, dict[str, str], float]] = []
        with self._lock:
            for key, (counts, total, n) in sorted(self.values.items()):
                base = dict(zip(self.labels, key))
                out += [(f"{self.name}_bucket", {**base, "le": _number(b)}, c) for b, c in zip(self.buckets, counts)]
                out += [(f"{self.name}_bucket", {**base, "le": "+Inf"}, n), (f"{self.name}_sum", base, total), (f"{self.name}_count", base, n)]
        return out


Sample = tuple[str, str, str, list[tuple[dict[str, str], float]]]            # (name, type, help, [(labels, value)]): what a collector returns


class Registry:
    def __init__(self) -> None:
        self.enabled = False
        self._lock = threading.Lock()
        self._metrics: dict[str, _Metric] = {}
        self._collectors: dict[str, Callable[[], list[Sample]]] = {}

    def _add(self, metric: _Metric) -> Any:
        with self._lock:
            return self._metrics.setdefault(metric.name, metric)

    def counter(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> Counter:
        return self._add(Counter(name, help_text, labels))

    def gauge(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> Gauge:
        return self._add(Gauge(name, help_text, labels))

    def histogram(self, name: str, help_text: str, labels: tuple[str, ...] = (), buckets: tuple[float, ...] = SECONDS) -> Histogram:
        return self._add(Histogram(name, help_text, labels, buckets))

    def set_collector(self, key: str, fn: Optional[Callable[[], list[Sample]]]) -> None:
        """What is read from the database when Prometheus asks. One per key, so building the application twice does not stack two."""
        with self._lock:
            if fn is None:
                self._collectors.pop(key, None)
            else:
                self._collectors[key] = fn

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._metrics) + sorted({n for fn in self._collectors.values() for n, *_ in _safely(fn)})

    def reset(self) -> None:
        with self._lock:
            for m in self._metrics.values():
                m.values.clear()
                m.dropped = 0

    def render(self) -> str:
        with self._lock:
            metrics, collectors = list(self._metrics.values()), list(self._collectors.values())
        lines: list[str] = []
        for m in sorted(metrics, key=lambda x: x.name):
            lines += [f"# HELP {m.name} {_escape(m.help)}", f"# TYPE {m.name} {m.kind}"]
            lines += [_line(n, lab, v) for n, lab, v in m.samples()]
        for fn in collectors:
            for name, kind, help_text, rows in _safely(fn):
                lines += [f"# HELP {name} {_escape(help_text)}", f"# TYPE {name} {kind}"]
                lines += [_line(name, lab, v) for lab, v in rows]
        dropped = sum(m.dropped for m in metrics)
        lines += ["# HELP erp_agents_metric_series_dropped_total Label combinations refused because a metric already holds too many", "# TYPE erp_agents_metric_series_dropped_total counter",
                  _line("erp_agents_metric_series_dropped_total", {}, dropped)]
        return "\n".join(lines) + "\n"


def _line(name: str, labels: dict[str, str], value: float) -> str:
    shown = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
    return f"{name}{{{shown}}} {_number(value)}" if shown else f"{name} {_number(value)}"


def _safely(fn: Callable[[], list[Sample]]) -> list[Sample]:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a scrape must answer even when one part of the database cannot be read
        log.exception("could not collect some metrics")
        return []


REGISTRY = Registry()

# ---------------------------------------------------------------------------------------------------------------------------- the events
LLM_REQUESTS = REGISTRY.counter("erp_agents_llm_requests_total", "Replies asked of a language model, by provider, model, step (gather, answer, repair) and outcome.", ("provider", "model", "kind", "outcome"))
LLM_SECONDS = REGISTRY.histogram("erp_agents_llm_request_seconds", "Seconds a language model took to reply.", ("provider", "model"))
LLM_TOKENS = REGISTRY.counter("erp_agents_llm_tokens_used_total", "Tokens used by language model replies since this process started (the database total is erp_agents_llm_tokens_total).", ("provider", "model", "direction"))
TOOL_CALLS = REGISTRY.counter("erp_agents_tool_calls_total", "Tool calls, by tool, how they came in and outcome (ok, refused or failed).", ("tool", "via", "outcome"))
TOOL_SECONDS = REGISTRY.histogram("erp_agents_tool_call_seconds", "Seconds a tool call took.", ("tool",))
HTTP_REQUESTS = REGISTRY.counter("erp_agents_http_requests_total", "Requests to ERPNext and to language model servers, by target, what was asked and outcome (2xx, 3xx, 4xx, 5xx or error).", ("target", "kind", "outcome"))
HTTP_SECONDS = REGISTRY.histogram("erp_agents_http_request_seconds", "Seconds a request to ERPNext or a language model server took.", ("target",))
ERP_SKEW = REGISTRY.gauge("erp_agents_erpnext_clock_skew_seconds", "This computer's clock minus ERPNext's, from the Date header of its last answer (a large value breaks expiry and reminders).")
GROUNDING_REJECTS = REGISTRY.counter("erp_agents_grounding_rejects_total", "Replies of the model that the checks rejected (an invented figure, a guess, a wrong format ...).", ("agent", "task"))
GROUNDING_REPAIRS = REGISTRY.counter("erp_agents_grounding_repairs_total", "Rejected replies the model was given a second chance on.", ("agent", "task"))
FALLBACKS = REGISTRY.counter("erp_agents_agent_fallbacks_total", "Agent tasks answered by the plain rules because the model's answer was rejected twice, the model was unavailable or busy.", ("agent", "task", "reason"))
JOB_RUNS = REGISTRY.counter("erp_agents_scheduler_job_runs_total", "Scheduled jobs that ran, by job and outcome (ok, failed or missed).", ("job", "outcome"))
JOB_LAG = REGISTRY.gauge("erp_agents_scheduler_lag_seconds", "How late the last run of a scheduled job started, against the time it was due.", ("job",))
JOB_LAST_OK = REGISTRY.gauge("erp_agents_scheduler_last_success_timestamp_seconds", "When a scheduled job last finished well (Unix time).", ("job",))


def configure(enabled: bool) -> None:
    """Switch the recording on or off (the `observability.metrics` setting). Off: nothing is recorded and the observers are detached."""
    REGISTRY.enabled = bool(enabled)
    if enabled and _http_seen not in obs.HTTP_HOOKS:
        obs.HTTP_HOOKS.append(_http_seen)
    if not enabled and _http_seen in obs.HTTP_HOOKS:
        obs.HTTP_HOOKS.remove(_http_seen)


def _class(status: Optional[int], error: Optional[str]) -> str:
    return "error" if error or status is None else f"{status // 100}xx"


def _http_seen(target: str, kind: str, method: str, status: Optional[int], seconds: float, error: Optional[str], headers: Any) -> None:
    HTTP_REQUESTS.inc(target=target, kind=kind, outcome=_class(status, error))
    HTTP_SECONDS.observe(seconds, target=target)
    if target == "erpnext" and headers is not None:
        try:
            ERP_SKEW.set((datetime.now(timezone.utc) - parsedate_to_datetime(headers.get("Date"))).total_seconds())
        except (TypeError, ValueError, AttributeError):
            pass                                            # no Date header, or one that cannot be read: no reading


def record_llm(provider: Any, model: Any, kind: str, outcome: str, seconds: float, tokens_in: int = 0, tokens_out: int = 0) -> None:
    LLM_REQUESTS.inc(provider=provider, model=model, kind=kind, outcome=outcome)
    LLM_SECONDS.observe(seconds, provider=provider, model=model)
    if tokens_in:
        LLM_TOKENS.inc(tokens_in, provider=provider, model=model, direction="in")
    if tokens_out:
        LLM_TOKENS.inc(tokens_out, provider=provider, model=model, direction="out")


REFUSALS = {"unknown_tool", "pack_off", "not_allowed", "invalid_arguments"}          # a call that was not allowed to run (as against one that ran and failed)


def record_tool(tool: str, via: str, ok: bool, refused: Optional[str], seconds: float) -> None:
    TOOL_CALLS.inc(tool=tool, via=via, outcome="ok" if ok else ("refused" if refused in REFUSALS else "failed"))
    TOOL_SECONDS.observe(seconds, tool=tool)


# ---------------------------------------------------------------------------------------------------------------------------- the state in the database, read when asked
def version() -> str:
    try:
        from importlib.metadata import version as dist_version
        return dist_version("erpnext-ai-agents")
    except Exception:  # noqa: BLE001 - not installed as a package (a source checkout)
        return "unknown"


STARTED = time.time()


def database_collector(app: Any) -> Callable[[], list[Sample]]:
    """The series that come from the application's own records. Reads go through the services that own the tables."""
    def collect() -> list[Sample]:
        now = app.clock()
        approvals = app.approvals.metrics_snapshot()
        age = max(0.0, (now - approvals["oldest_pending"]).total_seconds()) if approvals["oldest_pending"] else 0.0
        tasks = app.agent_log.metrics_snapshot()
        by_status: dict[tuple[str, str, str], int] = {}
        for r in tasks:
            key = (r["agent"], r["task"], r["status"])
            by_status[key] = by_status.get(key, 0) + r["n"]
        out: list[Sample] = [
            ("erp_agents_build_info", "gauge", "Which version is running (always 1).", [({"version": version()}, 1)]),
            ("erp_agents_process_start_time_seconds", "gauge", "When this process started (Unix time).", [({}, STARTED)]),
            ("erp_agents_approvals_pending", "gauge", "Requests waiting for a person's answer.", [({}, approvals["pending"])]),
            ("erp_agents_approvals_oldest_pending_age_seconds", "gauge", "How long the longest-waiting request has been waiting (0 when none is).", [({}, age)]),
            ("erp_agents_decisions_total", "counter", "Closed requests by kind and how they ended (the person's answer, expired or cancelled).",
             [({"kind": label(r["kind"]), "outcome": label(r["outcome"])}, r["n"]) for r in approvals["decisions"]]),
            ("erp_agents_deliveries_total", "counter", "Messages to people by channel and status (sent, failed, given_up).",
             [({"channel": label(r["channel"]), "status": label(r["status"])}, r["n"]) for r in approvals["deliveries"]]),
            ("erp_agents_agent_tasks_total", "counter", "Agent tasks by agent, kind of task and how they ended (ok, fallback or failed), from the database.",
             [({"agent": label(a), "task": label(t), "status": label(st)}, n) for (a, t, st), n in sorted(by_status.items())]),
            ("erp_agents_llm_tokens_total", "counter", "Tokens used by agent tasks, by provider and model, from the database.",
             [({"provider": label(r["provider"]), "model": label(r["model"]), "direction": d}, r[col]) for r in tasks_by_model(tasks) for d, col in (("in", "tokens_in"), ("out", "tokens_out"))]),
        ]
        return out
    return collect


def tasks_by_model(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        m = merged.setdefault((r["provider"], r["model"]), {"provider": r["provider"], "model": r["model"], "tokens_in": 0, "tokens_out": 0})
        m["tokens_in"] += r["tokens_in"] or 0
        m["tokens_out"] += r["tokens_out"] or 0
    return list(merged.values())


def attach(app: Any) -> None:
    """Called when the application is built: switch recording on or off as the settings say, and (on) let /metrics read the database through this application."""
    on = bool(app.observability and app.observability.metrics)
    configure(on)
    REGISTRY.set_collector("database", database_collector(app) if on else None)

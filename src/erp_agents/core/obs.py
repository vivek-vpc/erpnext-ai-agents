"""Seeing what the running assistant does: an id that follows one task through every log line, a text and a JSON log format, and the scrubber that keeps secrets and phone
numbers out of every line that is written.

  * `bind(task_id=...)` / `bind(request_id=...)` put an id into a ContextVar; every log record made while it is bound carries it (anyio worker threads and asyncio tasks copy the
    context, so the id reaches the tool calls, the ERPNext calls and the model calls of the task).
  * `configure_logging` installs one handler on the root logger: the plain format as before (plus " [task=..]" while an id is bound) or one JSON object per line.
  * The scrubber runs in the handler's formatter, so it covers our own code and every library: a configured secret, anything shaped like a token or an API key, and anything that looks
    like a phone number, becomes "[hidden]" or "[number]". It cannot know every secret; it knows the ones in the configuration and the shapes of the common ones.
  * `event` writes the one-line records of a model call, a tool call, an ERPNext call and a task (logger `erp_agents.calls`), never the text sent or received.

No web, scheduler or HTTP library is imported here (docs/DEVELOPING.md): the module can move into a Frappe app unchanged.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional, TextIO
from urllib.parse import quote, unquote, urlsplit

LOG_FORMATS = ("text", "json")
LOG_FORMAT_ENV = "ERP_AGENTS_LOG_FORMAT"
HIDDEN, NUMBER = "[hidden]", "[number]"
MIN_SECRET = 8                                   # a shorter value would hide ordinary words
TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s%(context)s: %(message)s"

_task_id: ContextVar[Optional[str]] = ContextVar("erp_agents_task_id", default=None)
_request_id: ContextVar[Optional[str]] = ContextVar("erp_agents_request_id", default=None)
_job_id: ContextVar[Optional[str]] = ContextVar("erp_agents_job_id", default=None)
calls = logging.getLogger("erp_agents.calls")


# ------------------------------------------------------------------------------------------------------------------------ ids
@contextmanager
def bind(task_id: Optional[str] = None, request_id: Optional[str] = None, job: Optional[str] = None) -> Iterator[None]:
    """Everything logged inside the block (and in the threads and tasks it starts through anyio or asyncio) carries these ids: an agent task, an HTTP request, a scheduled job."""
    marks = []
    if task_id is not None:
        marks.append((_task_id, _task_id.set(task_id)))
    if request_id is not None:
        marks.append((_request_id, _request_id.set(request_id)))
    if job is not None:
        marks.append((_job_id, _job_id.set(job)))
    try:
        yield
    finally:
        for var, mark in reversed(marks):
            var.reset(mark)


def current_ids() -> dict[str, str]:
    ids = {"task_id": _task_id.get(), "request_id": _request_id.get(), "job": _job_id.get()}
    return {k: v for k, v in ids.items() if v}


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


_SANE_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")


def clean_request_id(value: Any) -> Optional[str]:
    """An id a caller sent in X-Request-ID, if it is harmless to write into a log line (letters, digits, dot, dash; 8 to 64 characters); otherwise None."""
    text = str(value or "").strip()
    return text if _SANE_ID.fullmatch(text) else None


# ------------------------------------------------------------------------------------------------------------------------ the scrubber
_TELEGRAM = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
_KEY_SHAPES = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|EAA[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{30,})")
_AUTH_VALUE = re.compile(r"(?i)\b(bearer|token|basic)\s+[A-Za-z0-9._~+/=:-]{8,}")
_HEADER = re.compile(r"(?i)\b(x-api-key|api-key|authorization|x-auth-token)(\s*[:=]\s*)(?:(?:bearer|token|basic)\s+)?[^\s,;\"')}]+")
_QUERY = re.compile(r"(?i)((?:[?&;]\s*|\b)(?:api[_-]?key|key|token|access[_-]?token|secret|password|passwd|signature|sig|auth)=)[^&\s\"')]{6,}")          # ?api_key=..., &token=..., secret=...
_PLUS_NUMBER = re.compile(r"\+\d[\d\s().-]{6,}\d")
_DIGIT_RUN = re.compile(r"(?<![\w.])\d{10,15}(?!\w)(?!\.\d)")
_SPACED_NUMBER = re.compile(r"(?<![\w.])\d{2,5}(?:[ -]\d{3,5}){1,4}(?![\w-])")          # 98765 43210; only when it has 10 or more digits (a date or a quantity does not)


def _long_enough(m: "re.Match[str]") -> str:
    return NUMBER if sum(ch.isdigit() for ch in m.group(0)) >= 10 else m.group(0)


class Scrubber:
    """Knows the secrets this installation holds (registered at start-up and as they are created) and the shapes of the common ones."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._static: set[str] = set()
        self._dynamic: deque[str] = deque(maxlen=2000)            # sign-in tokens: made while running, gone when they expire
        self._phones: set[str] = set()
        self._compiled: Optional[tuple[Optional[re.Pattern[str]], Optional[re.Pattern[str]]]] = None

    def register(self, value: Any, dynamic: bool = False) -> None:
        text = str(value or "").strip()
        if len(text) < MIN_SECRET:
            return
        with self._lock:
            if dynamic:
                self._dynamic.append(text)
            else:
                self._static.add(text)
            self._compiled = None

    def register_phone(self, value: Any) -> None:
        """A phone number or chat address in any written form: only its digits are kept, and it is found again with spaces, dashes or a plus in between."""
        digits = re.sub(r"\D", "", str(value or ""))
        if len(digits) >= 7:
            with self._lock:
                self._phones.add(digits)
                self._compiled = None

    def forget_all(self) -> None:
        with self._lock:
            self._static.clear()
            self._dynamic.clear()
            self._phones.clear()
            self._compiled = None

    def _patterns(self) -> tuple[Optional[re.Pattern[str]], Optional[re.Pattern[str]]]:
        with self._lock:
            if self._compiled is None:
                secrets: set[str] = set()
                for s in (*self._static, *self._dynamic):
                    secrets.update({s, quote(s, safe=""), quote(s)})
                known = re.compile("|".join(re.escape(s) for s in sorted(secrets, key=len, reverse=True))) if secrets else None
                phones = re.compile("|".join(r"\+?" + r"[\s().-]*".join(d) for d in sorted(self._phones, key=len, reverse=True))) if self._phones else None
                self._compiled = (known, phones)
            return self._compiled

    def scrub(self, text: Any) -> str:
        out = str(text)
        known, phones = self._patterns()
        if known is not None:
            out = known.sub(HIDDEN, out)
        out = _TELEGRAM.sub(HIDDEN, out)
        out = _KEY_SHAPES.sub(HIDDEN, out)
        out = _HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{HIDDEN}", out)
        out = _AUTH_VALUE.sub(lambda m: f"{m.group(1)} {HIDDEN}", out)
        out = _QUERY.sub(lambda m: m.group(1) + HIDDEN, out)
        if phones is not None:
            out = phones.sub(NUMBER, out)
        out = _PLUS_NUMBER.sub(NUMBER, out)
        out = _DIGIT_RUN.sub(NUMBER, out)
        return _SPACED_NUMBER.sub(_long_enough, out)


SCRUBBER = Scrubber()


def scrub(text: Any) -> str:
    return SCRUBBER.scrub(text)


def register_secret(value: Any, dynamic: bool = False) -> None:
    SCRUBBER.register(value, dynamic)


_SECRET_NAME = re.compile(r"(?i)token|secret|password|api_?key|private_?key|credential")


def register_secrets_from_config(cfg: dict[str, Any]) -> None:
    """Every secret and every address the configuration holds, and the ones that live in the environment."""
    for t in (cfg.get("auth") or {}).get("tokens") or []:
        register_secret(t.get("token"))
    for value in (cfg.get("llm_keys") or {}).values():
        register_secret(value)
    erp = cfg.get("erp") or {}
    for name in ("api_key", "api_secret"):
        register_secret(erp.get(name))
    for chan in (cfg.get("channels") or {}).values():
        for name, value in (chan or {}).items():
            if isinstance(value, str) and _SECRET_NAME.search(name):
                register_secret(value)
    register_secret((cfg.get("mcp") or {}).get("client_token"))
    for r in (cfg.get("approvals") or {}).get("recipients") or []:
        for rc in r.get("channels") or []:
            SCRUBBER.register_phone(rc.get("address"))
    for name in ("ERP_AGENTS_HASH_KEY", "ERP_AGENTS_METRICS_TOKEN", "ERP_AGENTS_MCP_TOKEN", "ERP_AGENTS_OWNER_TOKEN"):
        register_secret(os.environ.get(name))


# ------------------------------------------------------------------------------------------------------------------------ logging
class ContextFilter(logging.Filter):
    """Stamps the ids that are bound at this moment on the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        ids = current_ids()
        record.task_id, record.request_id, record.job = ids.get("task_id"), ids.get("request_id"), ids.get("job")
        record.context = (" [" + " ".join(f"{k.split('_')[0]}={v}" for k, v in ids.items()) + "]") if ids else ""
        return True


def _scrubbed(record: logging.LogRecord) -> logging.LogRecord:
    """A copy of the record whose message, traceback and fields are scrubbed. A copy, because other handlers may hold the same record and want the original."""
    rec = copy.copy(record)
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - a bad format string in somebody's log call must not stop the line
        message = str(record.msg)
    rec.msg, rec.args = SCRUBBER.scrub(message), ()
    if record.exc_info and not record.exc_text:
        rec.exc_text = SCRUBBER.scrub("".join(traceback.format_exception(*record.exc_info)).rstrip())
    elif record.exc_text:
        rec.exc_text = SCRUBBER.scrub(record.exc_text)
    rec.exc_info = None
    if record.stack_info:
        rec.stack_info = SCRUBBER.scrub(record.stack_info)
    fields = getattr(record, "fields", None)
    if isinstance(fields, dict):
        rec.fields = {k: (SCRUBBER.scrub(v) if isinstance(v, str) else v) for k, v in fields.items()}
    return rec


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(TEXT_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        rec = _scrubbed(record)
        if not hasattr(rec, "context"):
            rec.context = ""
        return super().format(rec)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts (UTC), level, logger, msg, the ids that were bound, the fields given with `extra={"fields": {...}}`, and the traceback if there is one."""

    def format(self, record: logging.LogRecord) -> str:
        rec = _scrubbed(record)
        out: dict[str, Any] = {"ts": datetime.fromtimestamp(rec.created, timezone.utc).isoformat(timespec="milliseconds"), "level": rec.levelname, "logger": rec.name,
                               "msg": rec.getMessage()}
        for key in ("task_id", "request_id", "job"):
            if getattr(rec, key, None):
                out[key] = getattr(rec, key)
        fields = getattr(rec, "fields", None)
        if isinstance(fields, dict):
            out.update({k: v for k, v in fields.items() if k not in out})
        if rec.exc_text:
            out["exc"] = rec.exc_text[:4000]
        return json.dumps(out, ensure_ascii=False, default=str)


def make_handler(stream: Optional[TextIO] = None, fmt: str = "text") -> logging.Handler:
    if fmt not in LOG_FORMATS:
        raise ValueError(f"the log format is {fmt!r}; use one of: {', '.join(LOG_FORMATS)}")
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler._erp_agents = True                       # type: ignore[attr-defined]  # ours, so that a second call replaces it instead of doubling every line
    return handler


def configure_logging(level: int = logging.INFO, fmt: str = "text", stream: Optional[TextIO] = None) -> logging.Handler:
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_erp_agents", False)]
    handler = make_handler(stream, fmt)
    root.addHandler(handler)
    root.setLevel(level)
    return handler


@dataclass(frozen=True)
class ObsSettings:
    """The `observability:` section of the settings file. Process-wide, read at start-up (a change needs a restart); everything is off or as before when the section is absent."""
    log_format: str = "text"                         # text | json
    metrics: bool = False                            # serve GET /metrics (Prometheus text)
    tracing: bool = False                            # OpenTelemetry spans (needs: pip install ".[otel]")
    otlp_endpoint: str = ""                          # where the spans go (OTLP over HTTP), e.g. http://localhost:4318/v1/traces; empty = the exporter's own default
    service_name: str = "erpnext-ai-agents"
    pending_hours: float = 24.0                      # an approval waiting longer than this counts as a backlog (doctor, and the example alert rule)


def settings_from(cfg: dict[str, Any]) -> ObsSettings:
    raw = cfg.get("observability") or {}
    if not isinstance(raw, dict):
        raise ValueError("observability: write it as a section with keys such as log_format, metrics and tracing")
    unknown = sorted(set(raw) - set(ObsSettings.__dataclass_fields__))
    if unknown:
        raise ValueError(f"observability: unknown setting(s) {', '.join(map(str, unknown))}; known: {', '.join(ObsSettings.__dataclass_fields__)}")
    fmt = str(raw.get("log_format") or "text").strip().lower()
    if fmt not in LOG_FORMATS:
        raise ValueError(f"observability.log_format is {fmt[:20]!r}; use one of: {', '.join(LOG_FORMATS)}")
    for name in ("metrics", "tracing"):
        if name in raw and not isinstance(raw[name], bool):
            raise ValueError(f"observability.{name} must be true or false (written as a real true/false, not text)")
    try:
        hours = float(raw.get("pending_hours", 24.0))
    except (TypeError, ValueError):
        raise ValueError("observability.pending_hours must be a number of hours") from None
    if not 0 < hours <= 24 * 365:
        raise ValueError("observability.pending_hours must be more than 0 and at most a year")
    return ObsSettings(fmt, bool(raw.get("metrics", False)), bool(raw.get("tracing", False)), str(raw.get("otlp_endpoint") or "").strip(),
                       str(raw.get("service_name") or "erpnext-ai-agents").strip()[:80] or "erpnext-ai-agents", hours)


def log_format_from(requested: Optional[str], configured: Optional[str] = None) -> str:
    """The flag wins, then the environment, then the settings file, then text."""
    value = (requested or os.environ.get(LOG_FORMAT_ENV) or configured or "text").strip().lower()
    if value not in LOG_FORMATS:
        raise ValueError(f"the log format is {value[:20]!r}; use one of: {', '.join(LOG_FORMATS)}")
    return value


# ------------------------------------------------------------------------------------------------------------------------ event lines
def _plain(value: Any) -> str:
    text = " ".join(str(value).split())
    return text if text and " " not in text else json.dumps(text)


HTTP_HOOKS: list[Callable[..., None]] = []             # called after every observed request: hook(target, kind, method, status, seconds, error, headers); metrics.py adds itself here


def _kind_of(method: str, url: str) -> str:
    """What was asked, in a bounded vocabulary: the ERPNext document type or method name, else the first two parts of the path. Never the query string."""
    parts = [unquote(p) for p in urlsplit(str(url)).path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "api" and parts[1] in ("resource", "method"):
        return f"{'resource' if parts[1] == 'resource' else 'method'}:{parts[2]}"[:80]
    return ("/" + "/".join(parts[:2]))[:80]


def observe_session(session: Any, target: str) -> Any:
    """Time every request made through this `requests` session and write one line about it (method, what was asked, status, milliseconds) with the ids that are bound.
    The session object is changed in place and returned: its `get`/`post`/... all go through `request`. A session that has no `request` method (a test double) is returned as it is."""
    original = getattr(session, "request", None)
    if not callable(original) or getattr(session, "_erp_agents_observed", False):
        return session

    def request(method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        started, kind, verb = time.monotonic(), _kind_of(method, url), str(method).upper()
        try:
            response = original(method, url, *args, **kwargs)
        except BaseException as exc:
            _http_seen(target, kind, verb, None, time.monotonic() - started, type(exc).__name__, None)
            raise
        _http_seen(target, kind, verb, getattr(response, "status_code", None), time.monotonic() - started, None, getattr(response, "headers", None))
        return response
    session.request = request
    session._erp_agents_observed = True
    return session


def _http_seen(target: str, kind: str, method: str, status: Optional[int], seconds: float, error: Optional[str], headers: Any) -> None:
    bad = error is not None or (status is not None and status >= 500)
    event(f"{target}_request", logging.WARNING if bad else logging.INFO, method=method, kind=kind, status=status, ms=int(seconds * 1000), error=error)
    for hook in HTTP_HOOKS:
        try:
            hook(target, kind, method, status, seconds, error, headers)
        except Exception:  # noqa: BLE001 - watching must never break the call it watches
            logging.getLogger(__name__).debug("an observer of the %s call failed", target, exc_info=True)


def event(name: str, level: int = logging.INFO, **fields: Any) -> None:
    """One line about something that happened (a model call, a tool call, an ERPNext call, a task), with the ids that are bound. Fields are names, numbers and outcomes:
    never the text of a question, an answer, a prompt or a document."""
    if not calls.isEnabledFor(level):
        return
    shown = {k: v for k, v in fields.items() if v is not None}
    calls.log(level, "%s %s", name, " ".join(f"{k}={_plain(v)}" for k, v in shown.items()), extra={"fields": {"event": name, **shown}})

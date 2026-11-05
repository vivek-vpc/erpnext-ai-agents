"""The record of what the agents did: one row per task (what was asked, the result, what was sent to the model) and one per tool call.

The only place the agents' tables are read or written (docs/DEVELOPING.md: SQL lives in repository classes)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

from ai_munimji_engine.storage import dumps, loads
from ai_munimji_engine.stores.agents import AgentLogStore

from ..common import Clock, iso, short_id, utcnow

MAX_TRACE_CHARS = 60_000
MAX_ARGS_CHARS = 600


class AgentLog:
    def __init__(self, store: AgentLogStore, clock: Clock = utcnow) -> None:
        self.store, self.clock = store, clock

    # ---------------------------------------------------------------- tool calls
    def log_tool(self, actor: str, via: str, tool: str, args: dict[str, Any], ok: bool, ms: int, task_id: Optional[str] = None, error: Optional[str] = None) -> None:
        self.store.add_tool_call(iso(self.clock()), actor[:80], via, tool, json.dumps(args, default=str)[:MAX_ARGS_CHARS], ok, ms, task_id, (error or "")[:300] or None)

    def tool_calls(self, task_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.tool_calls(task_id, limit)

    # ---------------------------------------------------------------- tasks
    def start_task(self, agent: str, task: str, model: str, remote: bool, request_text: str = "", approval_id: Optional[str] = None, asked_by: str = "", provider: str = "ollama") -> str:
        tid = short_id(10)
        self.store.add_task(tid, agent, task, iso(self.clock()), model, provider, remote, approval_id, asked_by[:80], request_text[:2000])
        return tid

    # ---------------------------------------------------------------- tokens used (what a hosted provider bills for)
    def tokens_since(self, since: datetime, hosted_only: bool = True) -> int:
        """Input plus output tokens of the tasks started at or after `since` (by default only those that went to a hosted model)."""
        return self.store.tokens_since(iso(since), hosted_only)

    def usage(self, since: datetime) -> dict[str, Any]:
        """Tokens and tasks since a moment: per provider and model, and per day."""
        return {"by_model": self.store.usage_by_model(iso(since)), "daily": self.store.usage_by_day(iso(since))}

    def finish_task(self, task_id: str, status: str, result: Optional[dict[str, Any]], note: str = "", steps: int = 0, tool_calls: int = 0, llm_seconds: float = 0.0,
                    prompt_tokens: int = 0, output_tokens: int = 0, trace: Optional[list[dict[str, Any]]] = None) -> None:
        trace_text = json.dumps(trace or [], default=str)
        if len(trace_text) > MAX_TRACE_CHARS:
            trace_text = json.dumps([{"truncated": f"the record of what was sent was longer than {MAX_TRACE_CHARS} characters"}, *(trace or [])[-6:]], default=str)[:MAX_TRACE_CHARS]
        self.store.finish_task(task_id, status, iso(self.clock()), dumps(result) if result is not None else None, note[:500], steps, tool_calls, round(llm_seconds, 1), prompt_tokens,
                               output_tokens, trace_text)

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        row = self.store.get(task_id)
        return self._row(row, with_trace=True) if row else None

    def list(self, agent: Optional[str] = None, task: Optional[str] = None, approval_id: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        return [self._row(r) for r in self.store.list(agent, task, approval_id, max(1, min(limit, 200)))]

    def metrics_snapshot(self) -> list[dict[str, Any]]:
        """Tasks and tokens so far, by agent, kind of task, status, provider and model (core/metrics.py reads this when Prometheus asks)."""
        return self.store.metrics_snapshot()

    def latest(self, agent: str, task: str, status: str = "ok") -> Optional[dict[str, Any]]:
        row = self.store.latest(agent, task, status)
        return self._row(row) if row else None

    def fail_stale(self, older_than_hours: int = 2) -> int:
        """A task still 'running' long after it started belongs to a server that stopped; say so instead of showing it as running forever."""
        cutoff = iso(self.clock() - timedelta(hours=older_than_hours))
        return self.store.fail_stale(iso(self.clock()), cutoff)

    def prune(self, keep_days: int) -> int:
        """Forget what was sent to the model after `keep_days` (the result and the counts stay)."""
        cutoff = iso(self.clock() - timedelta(days=keep_days))
        return self.store.prune(cutoff)

    @staticmethod
    def _row(row: Any, with_trace: bool = False) -> dict[str, Any]:
        d = {k: row[k] for k in row.keys() if k not in ("result_json", "trace_json")}
        d["result"] = loads(row["result_json"]) if row["result_json"] else None
        if with_trace:
            d["trace"] = loads(row["trace_json"]) if row["trace_json"] else None
        return d

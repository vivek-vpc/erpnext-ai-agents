"""The record of what the agents did: one row per task (what was asked, the result, what was sent to the model) and one per tool call.

The only place the agents' tables are read or written (docs/DEVELOPING.md: SQL lives in repository classes)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Optional

from ..common import Clock, iso, short_id, utcnow
from ..storage import Database, dumps, loads

MAX_TRACE_CHARS = 60_000
MAX_ARGS_CHARS = 600


class AgentLog:
    def __init__(self, db: Database, clock: Clock = utcnow) -> None:
        self.db, self.clock = db, clock

    # ---------------------------------------------------------------- tool calls
    def log_tool(self, actor: str, via: str, tool: str, args: dict[str, Any], ok: bool, ms: int, task_id: Optional[str] = None, error: Optional[str] = None) -> None:
        self.db.execute("INSERT INTO tool_calls(at, actor, via, tool, args_json, ok, ms, task_id, error) VALUES (?,?,?,?,?,?,?,?,?)",
                        (iso(self.clock()), actor[:80], via, tool, json.dumps(args, default=str)[:MAX_ARGS_CHARS], int(ok), ms, task_id, (error or "")[:300] or None))

    def tool_calls(self, task_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        if task_id:
            rows = self.db.query("SELECT * FROM tool_calls WHERE task_id=? ORDER BY id", (task_id,))
        else:
            rows = self.db.query("SELECT * FROM tool_calls ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- tasks
    def start_task(self, agent: str, task: str, model: str, remote: bool, request_text: str = "", approval_id: Optional[str] = None, asked_by: str = "", provider: str = "ollama") -> str:
        tid = short_id(10)
        self.db.execute("INSERT INTO agent_tasks(id, agent, task, status, started_at, model, provider, remote, approval_id, asked_by, request_text) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (tid, agent, task, "running", iso(self.clock()), model, provider, int(remote), approval_id, asked_by[:80], request_text[:2000]))
        return tid

    # ---------------------------------------------------------------- tokens used (what a hosted provider bills for)
    def tokens_since(self, since: datetime, hosted_only: bool = True) -> int:
        """Input plus output tokens of the tasks started at or after `since` (by default only those that went to a hosted model)."""
        row = self.db.query_one("SELECT COALESCE(SUM(COALESCE(prompt_tokens,0) + COALESCE(output_tokens,0)), 0) AS n FROM agent_tasks WHERE started_at >= ?" + (" AND remote=1" if hosted_only else ""), (iso(since),))  # two fixed strings, the value is bound  # nosec B608
        return int(row["n"])

    def usage(self, since: datetime) -> dict[str, Any]:
        """Tokens and tasks since a moment: per provider and model, and per day."""
        by_model = self.db.query("SELECT COALESCE(provider,'ollama') AS provider, model, remote, COUNT(*) AS tasks, COALESCE(SUM(prompt_tokens),0) AS tokens_in, COALESCE(SUM(output_tokens),0) AS tokens_out, "
                                 "COALESCE(SUM(llm_seconds),0) AS seconds FROM agent_tasks WHERE started_at >= ? GROUP BY 1, 2, 3 ORDER BY (tokens_in + tokens_out) DESC", (iso(since),))
        daily = self.db.query("SELECT substr(started_at,1,10) AS day, remote, COALESCE(SUM(prompt_tokens),0) AS tokens_in, COALESCE(SUM(output_tokens),0) AS tokens_out, COUNT(*) AS tasks "
                              "FROM agent_tasks WHERE started_at >= ? GROUP BY 1, 2 ORDER BY 1", (iso(since),))
        return {"by_model": [dict(r) for r in by_model], "daily": [dict(r) for r in daily]}

    def finish_task(self, task_id: str, status: str, result: Optional[dict[str, Any]], note: str = "", steps: int = 0, tool_calls: int = 0, llm_seconds: float = 0.0,
                    prompt_tokens: int = 0, output_tokens: int = 0, trace: Optional[list[dict[str, Any]]] = None) -> None:
        trace_text = json.dumps(trace or [], default=str)
        if len(trace_text) > MAX_TRACE_CHARS:
            trace_text = json.dumps([{"truncated": f"the record of what was sent was longer than {MAX_TRACE_CHARS} characters"}, *(trace or [])[-6:]], default=str)[:MAX_TRACE_CHARS]
        self.db.execute("UPDATE agent_tasks SET status=?, finished_at=?, result_json=?, note=?, steps=?, tool_calls=?, llm_seconds=?, prompt_tokens=?, output_tokens=?, trace_json=? WHERE id=?",
                        (status, iso(self.clock()), dumps(result) if result is not None else None, note[:500], steps, tool_calls, round(llm_seconds, 1), prompt_tokens, output_tokens, trace_text, task_id))

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
        return self._row(row, with_trace=True) if row else None

    def list(self, agent: Optional[str] = None, task: Optional[str] = None, approval_id: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        sql, params, conds = "SELECT * FROM agent_tasks", [], []
        for col, val in (("agent", agent), ("task", task), ("approval_id", approval_id)):
            if val:
                conds.append(f"{col}=?")
                params.append(val)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        rows = self.db.query(sql + " ORDER BY started_at DESC, rowid DESC LIMIT ?", (*params, max(1, min(limit, 200))))
        return [self._row(r) for r in rows]

    def metrics_snapshot(self) -> list[dict[str, Any]]:
        """Tasks and tokens so far, by agent, kind of task, status, provider and model (core/metrics.py reads this when Prometheus asks)."""
        rows = self.db.query("SELECT agent, task, status, COALESCE(provider, 'ollama') AS provider, COALESCE(model, '') AS model, COUNT(*) AS n, "
                             "COALESCE(SUM(prompt_tokens), 0) AS tokens_in, COALESCE(SUM(output_tokens), 0) AS tokens_out FROM agent_tasks GROUP BY 1, 2, 3, 4, 5")
        return [dict(r) for r in rows]

    def latest(self, agent: str, task: str, status: str = "ok") -> Optional[dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM agent_tasks WHERE agent=? AND task=? AND status IN (?, 'fallback') ORDER BY started_at DESC, rowid DESC LIMIT 1", (agent, task, status))
        return self._row(row) if row else None

    def fail_stale(self, older_than_hours: int = 2) -> int:
        """A task still 'running' long after it started belongs to a server that stopped; say so instead of showing it as running forever."""
        cutoff = iso(self.clock() - timedelta(hours=older_than_hours))
        cur = self.db.execute("UPDATE agent_tasks SET status='failed', finished_at=?, note='the server stopped before this finished' WHERE status='running' AND started_at < ?",
                              (iso(self.clock()), cutoff))
        return cur.rowcount

    def prune(self, keep_days: int) -> int:
        """Forget what was sent to the model after `keep_days` (the result and the counts stay)."""
        cutoff = iso(self.clock() - timedelta(days=keep_days))
        cur = self.db.execute("UPDATE agent_tasks SET trace_json=NULL WHERE started_at < ? AND trace_json IS NOT NULL", (cutoff,))
        self.db.execute("DELETE FROM tool_calls WHERE at < ?", (cutoff,))
        return cur.rowcount

    @staticmethod
    def _row(row: Any, with_trace: bool = False) -> dict[str, Any]:
        d = {k: row[k] for k in row.keys() if k not in ("result_json", "trace_json")}
        d["result"] = loads(row["result_json"]) if row["result_json"] else None
        if with_trace:
            d["trace"] = loads(row["trace_json"]) if row["trace_json"] else None
        return d

"""The record of what the AI agents did (the tables `agent_tasks` and `tool_calls`). `AgentLog` (core/agents/store.py) is the domain code: it shortens and serialises what is
recorded; this is the storage it needs.

A row of `agent_tasks`: id, agent, task, status (running | ok | rejected | fallback | failed), started_at, finished_at, model, provider, remote (0 | 1), steps, tool_calls,
llm_seconds, prompt_tokens, output_tokens, approval_id, asked_by, request_text, result_json, note, trace_json. A row of `tool_calls`: id (1, 2, 3, ... in the order
written), at, actor, via, tool, args_json, ok (0 | 1), ms, task_id, error."""
from __future__ import annotations

from typing import Any, ContextManager, Optional, Protocol

from ..storage import MemoryDb, Row


class AgentLogStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    # ---- tool calls
    def add_tool_call(self, at: str, actor: str, via: str, tool: str, args_json: str, ok: bool, ms: int, task_id: Optional[str], error: Optional[str]) -> None: ...

    def tool_calls(self, task_id: Optional[str], limit: int) -> list[Row]:
        """Those of one task, oldest first; without a task the newest `limit` of all, newest first."""

    # ---- tasks
    def add_task(self, task_id: str, agent: str, task: str, started_at: str, model: str, provider: str, remote: bool, approval_id: Optional[str], asked_by: str,
                 request_text: str) -> None:
        """A new task, status 'running'; steps and tool_calls 0; everything else empty."""

    def finish_task(self, task_id: str, status: str, finished_at: str, result_json: Optional[str], note: str, steps: int, tool_calls: int, llm_seconds: float,
                    prompt_tokens: int, output_tokens: int, trace_json: str) -> None: ...

    def get(self, task_id: str) -> Optional[Row]: ...

    def list(self, agent: Optional[str], task: Optional[str], approval_id: Optional[str], limit: int) -> list[Row]:
        """Newest started first (tasks started at the same instant: the later one first), optionally of one agent, one kind of task, one request."""

    def latest(self, agent: str, task: str, status: str) -> Optional[Row]:
        """The newest task of this agent and kind whose status is `status` or 'fallback'."""

    def tasks_about(self, approval_id: str, marker: str) -> list[Row]:
        """The tasks made for this request (`approval_id`) or whose result text contains `marker` (ignoring ASCII case), oldest started first."""

    def tokens_since(self, since: str, hosted_only: bool) -> int:
        """Prompt plus output tokens of the tasks started at or after `since`; with `hosted_only` only those with remote = 1."""

    def usage_by_model(self, since: str) -> list[Row]:
        """Per (provider (ollama when empty), model, remote) since `since`: provider, model, remote, tasks, tokens_in, tokens_out, seconds; most tokens first."""

    def usage_by_day(self, since: str) -> list[Row]:
        """Per (day = first 10 characters of started_at, remote): day, remote, tokens_in, tokens_out, tasks; oldest day first."""

    def metrics_snapshot(self) -> list[Row]:
        """Per (agent, task, status, provider (ollama when empty), model (empty when none)): those five, n, tokens_in, tokens_out."""

    def fail_stale(self, now: str, cutoff: str) -> int:
        """Tasks still 'running' that started before `cutoff` become 'failed' (finished_at = `now`, with a note). Returns how many."""

    def prune(self, cutoff: str) -> int:
        """Clear the trace of tasks started before `cutoff` and delete tool calls made before it. Returns how many traces were cleared."""


STALE_NOTE = "the server stopped before this finished"


class MemoryAgentLogStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    # ---- tool calls
    def add_tool_call(self, at: str, actor: str, via: str, tool: str, args_json: str, ok: bool, ms: int, task_id: Optional[str], error: Optional[str]) -> None:
        self.db.insert("tool_calls", {"at": at, "actor": actor, "via": via, "tool": tool, "args_json": args_json, "ok": int(ok), "ms": ms, "task_id": task_id, "error": error}, auto_id="id")

    def tool_calls(self, task_id: Optional[str], limit: int) -> list[Row]:
        with self.db.lock:
            rows = sorted(self.db.rows("tool_calls"), key=lambda r: r["id"])
            if task_id:
                return [dict(r) for r in rows if r["task_id"] == task_id]
            return [dict(r) for r in rows[::-1][:limit]]

    # ---- tasks
    def add_task(self, task_id: str, agent: str, task: str, started_at: str, model: str, provider: str, remote: bool, approval_id: Optional[str], asked_by: str,
                 request_text: str) -> None:
        with self.db.lock:
            if any(r["id"] == task_id for r in self.db.rows("agent_tasks")):
                raise ValueError(f"task {task_id} exists")          # the table's primary key
            self.db.insert("agent_tasks", {"id": task_id, "agent": agent, "task": task, "status": "running", "started_at": started_at, "finished_at": None, "model": model,
                                           "provider": provider, "remote": int(remote), "steps": 0, "tool_calls": 0, "llm_seconds": None, "prompt_tokens": None,
                                           "output_tokens": None, "approval_id": approval_id, "asked_by": asked_by, "request_text": request_text, "result_json": None,
                                           "note": None, "trace_json": None})

    def _find(self, task_id: str) -> Optional[Row]:
        return next((r for r in self.db.rows("agent_tasks") if r["id"] == task_id), None)

    def finish_task(self, task_id: str, status: str, finished_at: str, result_json: Optional[str], note: str, steps: int, tool_calls: int, llm_seconds: float,
                    prompt_tokens: int, output_tokens: int, trace_json: str) -> None:
        with self.db.lock:
            row = self._find(task_id)
            if row is not None:
                row.update(status=status, finished_at=finished_at, result_json=result_json, note=note, steps=steps, tool_calls=tool_calls, llm_seconds=llm_seconds,
                           prompt_tokens=prompt_tokens, output_tokens=output_tokens, trace_json=trace_json)

    def get(self, task_id: str) -> Optional[Row]:
        with self.db.lock:
            row = self._find(task_id)
            return dict(row) if row else None

    def _newest_first(self, rows: list[Row]) -> list[Row]:
        indexed = sorted(enumerate(rows), key=lambda t: (t[1]["started_at"], t[0]), reverse=True)     # ORDER BY started_at DESC, rowid DESC
        return [r for _, r in indexed]

    def list(self, agent: Optional[str], task: Optional[str], approval_id: Optional[str], limit: int) -> list[Row]:
        with self.db.lock:
            rows = [r for r in self.db.rows("agent_tasks") if (not agent or r["agent"] == agent) and (not task or r["task"] == task)
                    and (not approval_id or r["approval_id"] == approval_id)]
            return [dict(r) for r in self._newest_first(rows)[:limit]]

    def latest(self, agent: str, task: str, status: str) -> Optional[Row]:
        with self.db.lock:
            rows = [r for r in self.db.rows("agent_tasks") if r["agent"] == agent and r["task"] == task and r["status"] in (status, "fallback")]
            ordered = self._newest_first(rows)
            return dict(ordered[0]) if ordered else None

    def tasks_about(self, approval_id: str, marker: str) -> list[Row]:
        with self.db.lock:
            rows = [r for r in self.db.rows("agent_tasks") if r["approval_id"] == approval_id or (r["result_json"] and marker.lower() in r["result_json"].lower())]
            return [dict(r) for r in sorted(rows, key=lambda r: r["started_at"])]

    def tokens_since(self, since: str, hosted_only: bool) -> int:
        with self.db.lock:
            return sum((r["prompt_tokens"] or 0) + (r["output_tokens"] or 0) for r in self.db.rows("agent_tasks") if r["started_at"] >= since and (not hosted_only or r["remote"] == 1))

    def usage_by_model(self, since: str) -> list[Row]:
        with self.db.lock:
            groups: dict[tuple[Any, ...], Row] = {}
            for r in self.db.rows("agent_tasks"):
                if r["started_at"] < since:
                    continue
                key = (r["provider"] or "ollama", r["model"], r["remote"])
                g = groups.setdefault(key, {"provider": key[0], "model": key[1], "remote": key[2], "tasks": 0, "tokens_in": 0, "tokens_out": 0, "seconds": 0})
                g["tasks"] += 1
                g["tokens_in"] += r["prompt_tokens"] or 0
                g["tokens_out"] += r["output_tokens"] or 0
                g["seconds"] += r["llm_seconds"] or 0
            ordered = sorted(groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1] or ""), kv[0][2]))
            return sorted((g for _, g in ordered), key=lambda g: g["tokens_in"] + g["tokens_out"], reverse=True)

    def usage_by_day(self, since: str) -> list[Row]:
        with self.db.lock:
            groups: dict[tuple[Any, ...], Row] = {}
            for r in self.db.rows("agent_tasks"):
                if r["started_at"] < since:
                    continue
                key = (r["started_at"][:10], r["remote"])
                g = groups.setdefault(key, {"day": key[0], "remote": key[1], "tokens_in": 0, "tokens_out": 0, "tasks": 0})
                g["tokens_in"] += r["prompt_tokens"] or 0
                g["tokens_out"] += r["output_tokens"] or 0
                g["tasks"] += 1
            return [g for _, g in sorted(groups.items())]

    def metrics_snapshot(self) -> list[Row]:
        with self.db.lock:
            groups: dict[tuple[Any, ...], Row] = {}
            for r in self.db.rows("agent_tasks"):
                key = (r["agent"], r["task"], r["status"], r["provider"] or "ollama", r["model"] or "")
                g = groups.setdefault(key, {"agent": key[0], "task": key[1], "status": key[2], "provider": key[3], "model": key[4], "n": 0, "tokens_in": 0, "tokens_out": 0})
                g["n"] += 1
                g["tokens_in"] += r["prompt_tokens"] or 0
                g["tokens_out"] += r["output_tokens"] or 0
            return [g for _, g in sorted(groups.items())]

    def fail_stale(self, now: str, cutoff: str) -> int:
        with self.db.lock:
            stale = [r for r in self.db.rows("agent_tasks") if r["status"] == "running" and r["started_at"] < cutoff]
            for r in stale:
                r.update(status="failed", finished_at=now, note=STALE_NOTE)
            return len(stale)

    def prune(self, cutoff: str) -> int:
        with self.db.lock:
            cleared = [r for r in self.db.rows("agent_tasks") if r["started_at"] < cutoff and r["trace_json"] is not None]
            for r in cleared:
                r["trace_json"] = None
            self.db.rows("tool_calls")[:] = [r for r in self.db.rows("tool_calls") if not r["at"] < cutoff]
            return len(cleared)

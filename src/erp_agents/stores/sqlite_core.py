"""The core's stores on SQLite (the approvals are in `sqlite_approvals.py`). The SQL is the SQL the domain classes used to hold, moved here unchanged; the Protocols are in
`ai_munimji_engine.stores`."""
from __future__ import annotations

from typing import Any, ContextManager, Optional

from ai_munimji_engine.storage import Row

from ..core.storage import Database


class _Sqlite:
    def __init__(self, db: Database) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()


class SqliteInboundSeenStore(_Sqlite):
    def claim(self, channel: str, external_id: str, seen_at: str) -> bool:
        cur = self.db.execute("INSERT OR IGNORE INTO inbound_seen(channel, external_id, seen_at) VALUES (?, ?, ?)", (channel, external_id, seen_at))
        return cur.rowcount == 1

    def release(self, channel: str, external_id: str) -> None:
        self.db.execute("DELETE FROM inbound_seen WHERE channel=? AND external_id=?", (channel, external_id))

    def purge_before(self, cutoff: str) -> int:
        return self.db.execute("DELETE FROM inbound_seen WHERE seen_at < ?", (cutoff,)).rowcount


class SqliteJobRunStore(_Sqlite):
    def started(self, job: str, at: str) -> None:
        self.db.execute("INSERT INTO job_runs(job, last_start, runs) VALUES (?, ?, 1) ON CONFLICT(job) DO UPDATE SET last_start=excluded.last_start, runs=runs+1", (job, at))

    def finished(self, job: str, at: str, ok: bool, error: Optional[str]) -> None:
        self.db.execute("UPDATE job_runs SET last_end=?, last_ok=?, last_error=?, failures=failures+? WHERE job=?", (at, int(ok), error, int(not ok), job))

    def get(self, job: str) -> Optional[Row]:
        row = self.db.query_one("SELECT * FROM job_runs WHERE job=?", (job,))
        return dict(row) if row else None

    def all(self) -> list[Row]:
        return [dict(r) for r in self.db.query("SELECT * FROM job_runs ORDER BY job")]


class SqliteWorkOrderStore(_Sqlite):
    def insert(self, wo_id: str, type_: str, payload_json: str, source: str, now: str) -> None:
        self.db.execute("INSERT INTO work_orders(id, type, status, payload_json, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                        (wo_id, type_, "pending", payload_json, source, now, now))

    def get(self, wo_id: str) -> Optional[Row]:
        row = self.db.query_one("SELECT * FROM work_orders WHERE id=?", (wo_id,))
        return dict(row) if row else None

    def list(self, status: Optional[str], type_: Optional[str], limit: int) -> list[Row]:
        sql, params, conds = "SELECT * FROM work_orders", [], []
        if status:
            conds.append("status=?")
            params.append(status)
        if type_:
            conds.append("type=?")
            params.append(type_)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.query(sql, params)]

    def set_status(self, wo_id: str, status: str, now: str) -> None:
        self.db.execute("UPDATE work_orders SET status=?, updated_at=? WHERE id=?", (status, now, wo_id))


class SqliteAuditStore(_Sqlite):
    def is_empty(self) -> bool:
        return self.db.query_one("SELECT 1 FROM audit_chain LIMIT 1") is None

    def last_hash(self) -> Optional[str]:
        row = self.db.query_one("SELECT hash FROM audit_chain ORDER BY seq DESC LIMIT 1")
        return row["hash"] if row else None

    def append(self, at: str, source: str, ref_id: str, event: str, digest: str, prev: str, hash_: str) -> None:
        self.db.execute("INSERT INTO audit_chain(at, source, ref_id, event, digest, prev, hash) VALUES (?,?,?,?,?,?,?)", (at, source, ref_id, event, digest, prev, hash_))

    def entries(self) -> list[Row]:
        return [dict(r) for r in self.db.query("SELECT * FROM audit_chain ORDER BY seq")]

    def sealed_at(self, source: str, ref_id: str) -> Optional[str]:
        row = self.db.query_one("SELECT at FROM audit_chain WHERE source=? AND ref_id=? ORDER BY seq LIMIT 1", (source, ref_id))
        return row["at"] if row else None

    def unsealed_approvals(self) -> list[Row]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM approvals WHERE status!='pending' AND id NOT IN (SELECT ref_id FROM audit_chain WHERE source='approval') ORDER BY COALESCE(decided_at, created_at), id")]

    def unsealed_count(self) -> int:
        return self.db.query_one("SELECT COUNT(*) AS n FROM approvals WHERE status!='pending' AND id NOT IN (SELECT ref_id FROM audit_chain WHERE source='approval')")["n"]

    def last_sealed_setting(self) -> int:
        return self.db.query_one("SELECT MAX(CAST(ref_id AS INTEGER)) AS n FROM audit_chain WHERE source='setting'")["n"] or 0

    def settings_after(self, setting_id: int) -> list[Row]:
        return [dict(r) for r in self.db.query("SELECT * FROM settings_audit WHERE id > ? ORDER BY id", (setting_id,))]

    def current_approval(self, ref_id: str) -> Optional[Row]:
        row = self.db.query_one("SELECT * FROM approvals WHERE id=?", (ref_id,))
        return dict(row) if row else None

    def current_setting(self, setting_id: int) -> Optional[Row]:
        row = self.db.query_one("SELECT * FROM settings_audit WHERE id=?", (setting_id,))
        return dict(row) if row else None


class SqliteSettingsOverrideStore(_Sqlite):
    def get(self, scope_type: str, scope_key: str) -> Optional[str]:
        row = self.db.query_one("SELECT data_json FROM settings_overrides WHERE scope_type=? AND scope_key=?", (scope_type, scope_key))
        return row["data_json"] if row else None

    def save(self, scope_type: str, scope_key: str, data_json: Optional[str], actor: str, now: str, changes: list[tuple[str, str, str]]) -> None:
        with self.db.transaction():
            if data_json is not None:
                self.db.execute(
                    "INSERT INTO settings_overrides(scope_type, scope_key, data_json, updated_by, updated_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(scope_type, scope_key) DO UPDATE SET data_json=excluded.data_json, "
                    "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (scope_type, scope_key, data_json, actor, now),
                )
            else:
                self.db.execute("DELETE FROM settings_overrides WHERE scope_type=? AND scope_key=?", (scope_type, scope_key))
            for field, old_json, new_json in changes:
                self.db.execute(
                    "INSERT INTO settings_audit(ts, actor, scope_type, scope_key, field, old_json, new_json) VALUES (?,?,?,?,?,?,?)",
                    (now, actor, scope_type, scope_key, field, old_json, new_json),
                )

    def audit(self, limit: int, scope_type: Optional[str] = None, scope_key: Optional[str] = None) -> list[Row]:
        sql = "SELECT * FROM settings_audit"
        params: list[Any] = []
        conds = []
        if scope_type:
            conds.append("scope_type=?")
            params.append(scope_type)
        if scope_key:
            conds.append("scope_key=?")
            params.append(scope_key)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.db.query(sql, params)]

    def audit_actors(self) -> list[str]:
        return [r["actor"] for r in self.db.query("SELECT DISTINCT actor FROM settings_audit")]


class SqliteAgentLogStore(_Sqlite):
    # ---- tool calls
    def add_tool_call(self, at: str, actor: str, via: str, tool: str, args_json: str, ok: bool, ms: int, task_id: Optional[str], error: Optional[str]) -> None:
        self.db.execute("INSERT INTO tool_calls(at, actor, via, tool, args_json, ok, ms, task_id, error) VALUES (?,?,?,?,?,?,?,?,?)",
                        (at, actor, via, tool, args_json, int(ok), ms, task_id, error))

    def tool_calls(self, task_id: Optional[str], limit: int) -> list[Row]:
        if task_id:
            rows = self.db.query("SELECT * FROM tool_calls WHERE task_id=? ORDER BY id", (task_id,))
        else:
            rows = self.db.query("SELECT * FROM tool_calls ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ---- tasks
    def add_task(self, task_id: str, agent: str, task: str, started_at: str, model: str, provider: str, remote: bool, approval_id: Optional[str], asked_by: str,
                 request_text: str) -> None:
        self.db.execute("INSERT INTO agent_tasks(id, agent, task, status, started_at, model, provider, remote, approval_id, asked_by, request_text) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (task_id, agent, task, "running", started_at, model, provider, int(remote), approval_id, asked_by, request_text))

    def finish_task(self, task_id: str, status: str, finished_at: str, result_json: Optional[str], note: str, steps: int, tool_calls: int, llm_seconds: float,
                    prompt_tokens: int, output_tokens: int, trace_json: str) -> None:
        self.db.execute("UPDATE agent_tasks SET status=?, finished_at=?, result_json=?, note=?, steps=?, tool_calls=?, llm_seconds=?, prompt_tokens=?, output_tokens=?, trace_json=? WHERE id=?",
                        (status, finished_at, result_json, note, steps, tool_calls, llm_seconds, prompt_tokens, output_tokens, trace_json, task_id))

    def get(self, task_id: str) -> Optional[Row]:
        row = self.db.query_one("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
        return dict(row) if row else None

    def list(self, agent: Optional[str], task: Optional[str], approval_id: Optional[str], limit: int) -> list[Row]:
        sql, params, conds = "SELECT * FROM agent_tasks", [], []
        for col, val in (("agent", agent), ("task", task), ("approval_id", approval_id)):
            if val:
                conds.append(f"{col}=?")
                params.append(val)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        return [dict(r) for r in self.db.query(sql + " ORDER BY started_at DESC, rowid DESC LIMIT ?", (*params, limit))]

    def latest(self, agent: str, task: str, status: str) -> Optional[Row]:
        row = self.db.query_one("SELECT * FROM agent_tasks WHERE agent=? AND task=? AND status IN (?, 'fallback') ORDER BY started_at DESC, rowid DESC LIMIT 1", (agent, task, status))
        return dict(row) if row else None

    def tasks_about(self, approval_id: str, marker: str) -> list[Row]:
        like = "%" + marker.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return [dict(r) for r in self.db.query("SELECT * FROM agent_tasks WHERE approval_id=? OR result_json LIKE ? ESCAPE '\\' ORDER BY started_at", (approval_id, like))]

    def tokens_since(self, since: str, hosted_only: bool) -> int:
        row = self.db.query_one("SELECT COALESCE(SUM(COALESCE(prompt_tokens,0) + COALESCE(output_tokens,0)), 0) AS n FROM agent_tasks WHERE started_at >= ?" + (" AND remote=1" if hosted_only else ""), (since,))  # two fixed strings, the value is bound  # nosec B608
        return int(row["n"])

    def usage_by_model(self, since: str) -> list[Row]:
        return [dict(r) for r in self.db.query(
            "SELECT COALESCE(provider,'ollama') AS provider, model, remote, COUNT(*) AS tasks, COALESCE(SUM(prompt_tokens),0) AS tokens_in, COALESCE(SUM(output_tokens),0) AS tokens_out, "
            "COALESCE(SUM(llm_seconds),0) AS seconds FROM agent_tasks WHERE started_at >= ? GROUP BY 1, 2, 3 ORDER BY (tokens_in + tokens_out) DESC", (since,))]

    def usage_by_day(self, since: str) -> list[Row]:
        return [dict(r) for r in self.db.query(
            "SELECT substr(started_at,1,10) AS day, remote, COALESCE(SUM(prompt_tokens),0) AS tokens_in, COALESCE(SUM(output_tokens),0) AS tokens_out, COUNT(*) AS tasks "
            "FROM agent_tasks WHERE started_at >= ? GROUP BY 1, 2 ORDER BY 1", (since,))]

    def metrics_snapshot(self) -> list[Row]:
        return [dict(r) for r in self.db.query(
            "SELECT agent, task, status, COALESCE(provider, 'ollama') AS provider, COALESCE(model, '') AS model, COUNT(*) AS n, "
            "COALESCE(SUM(prompt_tokens), 0) AS tokens_in, COALESCE(SUM(output_tokens), 0) AS tokens_out FROM agent_tasks GROUP BY 1, 2, 3, 4, 5")]

    def fail_stale(self, now: str, cutoff: str) -> int:
        cur = self.db.execute("UPDATE agent_tasks SET status='failed', finished_at=?, note='the server stopped before this finished' WHERE status='running' AND started_at < ?",
                              (now, cutoff))
        return cur.rowcount

    def prune(self, cutoff: str) -> int:
        cur = self.db.execute("UPDATE agent_tasks SET trace_json=NULL WHERE started_at < ? AND trace_json IS NOT NULL", (cutoff,))
        self.db.execute("DELETE FROM tool_calls WHERE at < ?", (cutoff,))
        return cur.rowcount

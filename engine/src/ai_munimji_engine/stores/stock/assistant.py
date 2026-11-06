"""What the assistant answered and what it was asked (the tables `assistant_answers` and `chat_log`). `AssistantService` and `ChatService`
(packs/stock_procurement/assistant) are the domain code; this is the storage they need.

A row of `assistant_answers`: cache_key, approval_id, question, result_json, model, asked_by, created_at. A row of `chat_log`: id (1, 2, 3, ... in the order written), asked_by,
asked_at, question, intent, status, source."""
from __future__ import annotations

from typing import ContextManager, Optional, Protocol

from ...storage import MemoryDb, Row


class AssistantStore(Protocol):
    def transaction(self) -> ContextManager[None]: ...

    def cached_answer(self, cache_key: str) -> Optional[str]:
        """The `result_json` kept for this key, or None."""

    def put_answer(self, cache_key: str, approval_id: str, question: str, result_json: str, model: str, asked_by: str, created_at: str) -> None:
        """Keep the answer under the key, replacing any earlier one with the same key."""

    def answers_for(self, approval_id: str) -> list[Row]:
        """question, result_json, asked_by, created_at of the answers kept for one request, oldest first."""

    def log_chat(self, asked_by: str, asked_at: str, question: str, intent: str, status: str, source: str) -> None: ...

    def chats(self, limit: int) -> list[Row]:
        """The newest `limit` entries of the chat log, oldest first among them."""


class MemoryAssistantStore:
    def __init__(self, db: MemoryDb) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def cached_answer(self, cache_key: str) -> Optional[str]:
        with self.db.lock:
            row = next((r for r in self.db.rows("assistant_answers") if r["cache_key"] == cache_key), None)
            return row["result_json"] if row else None

    def put_answer(self, cache_key: str, approval_id: str, question: str, result_json: str, model: str, asked_by: str, created_at: str) -> None:
        with self.db.lock:
            rows = self.db.rows("assistant_answers")
            rows[:] = [r for r in rows if r["cache_key"] != cache_key]                  # INSERT OR REPLACE: the old row goes, the new one is the latest made
            self.db.insert("assistant_answers", {"cache_key": cache_key, "approval_id": approval_id, "question": question, "result_json": result_json, "model": model,
                                                 "asked_by": asked_by, "created_at": created_at})

    def answers_for(self, approval_id: str) -> list[Row]:
        with self.db.lock:
            rows = sorted((r for r in self.db.rows("assistant_answers") if r["approval_id"] == approval_id), key=lambda r: r["created_at"])
            return [{k: r[k] for k in ("question", "result_json", "asked_by", "created_at")} for r in rows]

    def log_chat(self, asked_by: str, asked_at: str, question: str, intent: str, status: str, source: str) -> None:
        self.db.insert("chat_log", {"asked_by": asked_by, "asked_at": asked_at, "question": question, "intent": intent, "status": status, "source": source}, auto_id="id")

    def chats(self, limit: int) -> list[Row]:
        with self.db.lock:
            rows = sorted(self.db.rows("chat_log"), key=lambda r: r["id"])
            return [dict(r) for r in rows[-limit:]] if limit > 0 else []

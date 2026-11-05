"""What every storage backend shares: the shape of a row, the unit of work, and the in-memory database the fakes are built on.

A *store* is the only code that reads or writes one group of tables. Its Protocol (in the module of the thing it serves, for example `core/approvals/store.py`) says what
it can do in the words of the domain, never in SQL. Three backends exist or are planned:

  * SQLite, in the standalone service (`erp_agents`): the SQL lives there, unchanged;
  * memory (`MemoryDb` below and the `Memory*Store` classes): for tests, and for anyone who wants the rule logic without a database;
  * a Frappe site (the AI Munimji app): DocTypes behind the same Protocols.

Rows are plain dicts with the same keys the tables have. Timestamps are ISO-8601 strings, JSON columns hold JSON text: the domain code converts, so every backend can keep
them as they are.
"""
from __future__ import annotations

import copy
import json
import threading
from contextlib import contextmanager
from typing import Any, ContextManager, Iterator, Optional, Protocol

Row = dict[str, Any]


def dumps(obj: Any) -> str:
    """JSON text for a column: stable key order, anything unknown written as text."""
    return json.dumps(obj, default=str, ensure_ascii=False, sort_keys=True)


def loads(text: Optional[str]) -> Any:
    return None if text is None else json.loads(text)


class Transactional(Protocol):
    """Every store can open the unit of work its backend has. All the stores of one backend share it: a handler that runs inside a decision and writes through another store
    is part of the same transaction, so they succeed or fail together. Nested use joins the outer transaction."""

    def transaction(self) -> ContextManager[Any]: ...


class MemoryDb:
    """Tables of dict rows with one lock. `transaction()` rolls back by restoring a snapshot taken when the outermost one began, which is simple and exact for the amounts
    of data a test or a small site holds. The same rules as the SQLite database: nested use joins the outer transaction, and an error leaves the outer one to decide."""

    def __init__(self) -> None:
        self._tables: dict[str, list[Row]] = {}
        self._ids: dict[str, int] = {}
        self._lock = threading.RLock()
        self._depth = 0

    # -- tables
    def rows(self, table: str) -> list[Row]:
        """The live list of a table's rows (a store edits it under `lock`)."""
        return self._tables.setdefault(table, [])

    def insert(self, table: str, row: Row, auto_id: Optional[str] = None) -> Row:
        """Add a copy of `row`; with `auto_id` the column of that name gets the next number of the table (a SQLite INTEGER PRIMARY KEY AUTOINCREMENT). Returns the stored row."""
        with self._lock:
            stored = dict(row)
            if auto_id:
                self._ids[table] = self._ids.get(table, 0) + 1
                stored[auto_id] = self._ids[table]
            self.rows(table).append(stored)
            return stored

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # -- unit of work
    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            outermost = self._depth == 0
            saved = (copy.deepcopy(self._tables), dict(self._ids)) if outermost else None
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outermost and saved is not None:
                    self._tables, self._ids = saved
                raise
            else:
                self._depth -= 1

    def close(self) -> None:
        """Nothing to release; here so a backend can be closed the same way whatever it is."""

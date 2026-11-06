"""The stock pack's stores on SQLite. The SQL is the SQL the pack's classes used to hold, moved here unchanged; the Protocols are in `ai_munimji_engine.stores.stock`.

`sqlite_stock_stores(db)` makes the tables if they are not there (the pack's schema, with its numbered migrations: `schema.py`) and returns the bundle the pack is set up with."""
from __future__ import annotations

from typing import Any, ContextManager, Mapping, Optional, Sequence

from ai_munimji_engine.storage import Row
from ai_munimji_engine.stores import CoreStores
from ai_munimji_engine.stores.stock import StockStores, memory_stock_stores
from ai_munimji_engine.stores.stock.followup import QUOTE_COLUMNS
from ai_munimji_engine.stores.stock.movements import TRANSFER_COLUMNS
from ai_munimji_engine.stores.stock.proposals import UPDATABLE as PROPOSAL_COLUMNS

from erp_agents.core.storage import Database
from .schema import ensure_schema


class _Sqlite:
    def __init__(self, db: Database) -> None:
        self.db = db

    def transaction(self) -> ContextManager[None]:
        return self.db.transaction()

    def _one(self, sql: str, params: tuple = ()) -> Optional[Row]:
        row = self.db.query_one(sql, params)
        return dict(row) if row else None

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        return [dict(r) for r in self.db.query(sql, params)]


def _marks(values: Sequence[Any]) -> str:
    return ",".join("?" * len(values))


class SqliteAlertStore(_Sqlite):
    def insert(self, alert_id: str, item_code: str, kind: str, severity: int, now: str, analysis_json: str, reasoning_text: str, run_id: Optional[str]) -> None:
        self.db.execute(
            "INSERT INTO alerts(id, item_code, kind, severity, status, created_at, updated_at, analysis_json, reasoning_text, run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (alert_id, item_code, kind, severity, "awaiting", now, now, analysis_json, reasoning_text, run_id),
        )

    def get(self, alert_id: str) -> Optional[Row]:
        return self._one("SELECT * FROM alerts WHERE id=?", (alert_id,))

    def latest_for_item(self, item_code: str) -> Optional[Row]:
        return self._one("SELECT * FROM alerts WHERE item_code=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (item_code,))

    def open_for_item(self, item_code: str, statuses: Sequence[str]) -> list[Row]:
        return self._all(f"SELECT * FROM alerts WHERE item_code=? AND status IN ({_marks(statuses)})", (item_code, *statuses))  # only "?" placeholders are formatted in  # nosec B608

    def list(self, status: Optional[str], item_code: Optional[str], limit: int) -> list[Row]:
        sql, params, conds = "SELECT * FROM alerts", [], []
        if status:
            conds.append("status=?")
            params.append(status)
        if item_code:
            conds.append("item_code=?")
            params.append(item_code)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        return self._all(sql, params)

    def set_approval(self, alert_id: str, approval_id: str, now: str) -> None:
        self.db.execute("UPDATE alerts SET approval_id=?, updated_at=? WHERE id=?", (approval_id, now, alert_id))

    def update_status(self, alert_id: str, status: str, note: str, now: str, snoozed_until: Optional[str], suppress_until: Optional[str], cooldown_until: Optional[str]) -> None:
        self.db.execute(
            "UPDATE alerts SET status=?, status_note=?, updated_at=?, snoozed_until=COALESCE(?, snoozed_until), "
            "suppress_until=COALESCE(?, suppress_until), cooldown_until=COALESCE(?, cooldown_until) WHERE id=?",
            (status, note, now, snoozed_until, suppress_until, cooldown_until, alert_id),
        )

    def set_reasoning(self, alert_id: str, text: str) -> None:
        self.db.execute("UPDATE alerts SET reasoning_text=? WHERE id=?", (text, alert_id))

    def by_approval(self, approval_id: str) -> Optional[Row]:
        return self._one("SELECT * FROM alerts WHERE approval_id=?", (approval_id,))


class SqliteNoticeStore(_Sqlite):
    def insert(self, notice_id: str, item_code: str, level: str, now: str, approval_id: Optional[str]) -> None:
        self.db.execute("INSERT INTO stock_notices(id, item_code, level, status, created_at, updated_at, approval_id) VALUES (?,?,?,?,?,?,?)",
                        (notice_id, item_code, level, "sent", now, now, approval_id))

    def get(self, notice_id: str) -> Optional[Row]:
        return self._one("SELECT * FROM stock_notices WHERE id=?", (notice_id,))

    def by_approval(self, approval_id: str) -> Optional[Row]:
        return self._one("SELECT * FROM stock_notices WHERE approval_id=?", (approval_id,))

    def latest_active(self, item_code: str, statuses: Sequence[str]) -> Optional[Row]:
        return self._one(f"SELECT * FROM stock_notices WHERE item_code=? AND status IN ({_marks(statuses)}) ORDER BY created_at DESC, rowid DESC LIMIT 1", (item_code, *statuses))  # only "?" placeholders are formatted in  # nosec B608

    def active_for_item(self, item_code: str, statuses: Sequence[str]) -> list[Row]:
        return self._all(f"SELECT * FROM stock_notices WHERE item_code=? AND status IN ({_marks(statuses)})", (item_code, *statuses))  # only "?" placeholders are formatted in  # nosec B608

    def set_approval(self, notice_id: str, approval_id: str, now: str) -> None:
        self.db.execute("UPDATE stock_notices SET approval_id=?, updated_at=? WHERE id=?", (approval_id, now, notice_id))

    def set_status(self, notice_id: str, status: str, now: str, snoozed_until: Optional[str]) -> None:
        self.db.execute("UPDATE stock_notices SET status=?, updated_at=?, snoozed_until=COALESCE(?, snoozed_until) WHERE id=?", (status, now, snoozed_until, notice_id))

    def list(self, limit: int) -> list[Row]:
        return self._all("SELECT * FROM stock_notices ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))


class SqliteProposalStore(_Sqlite):
    def insert(self, proposal_id: str, work_order_id: str, item_code: str, status: str, supplier: Optional[str], supplier_name: Optional[str], qty: Optional[float],
               rate: Optional[float], total: Optional[float], ranking_json: Optional[str], note: str, now: str) -> None:
        self.db.execute(
            "INSERT INTO proposals(id, work_order_id, item_code, status, supplier, supplier_name, qty, rate, total, ranking_json, status_note, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (proposal_id, work_order_id, item_code, status, supplier, supplier_name, qty, rate, total, ranking_json, note, now, now),
        )

    def get(self, proposal_id: str) -> Optional[Row]:
        return self._one("SELECT * FROM proposals WHERE id=?", (proposal_id,))

    def by_approval(self, approval_id: str) -> Optional[Row]:
        return self._one("SELECT * FROM proposals WHERE approval_id=?", (approval_id,))

    def open_for_item(self, item_code: str, statuses: Sequence[str]) -> list[Row]:
        return self._all(f"SELECT * FROM proposals WHERE item_code=? AND status IN ({_marks(statuses)})", (item_code, *statuses))  # only "?" placeholders are formatted in  # nosec B608

    def snoozed_due(self, now: str) -> list[Row]:
        return self._all("SELECT * FROM proposals WHERE status='snoozed' AND snoozed_until IS NOT NULL AND snoozed_until <= ?", (now,))

    def list(self, status: Optional[str], item_code: Optional[str], limit: int) -> list[Row]:
        sql, params, conds = "SELECT * FROM proposals", [], []
        if status:
            conds.append("status=?")
            params.append(status)
        if item_code:
            conds.append("item_code=?")
            params.append(item_code)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return self._all(sql, params)

    def set_ranking(self, proposal_id: str, ranking_json: str, now: str) -> None:
        self.db.execute("UPDATE proposals SET ranking_json=?, updated_at=? WHERE id=?", (ranking_json, now, proposal_id))

    def set_approval(self, proposal_id: str, approval_id: str, now: str) -> None:
        self.db.execute("UPDATE proposals SET approval_id=?, updated_at=? WHERE id=?", (approval_id, now, proposal_id))

    def revise(self, proposal_id: str, supplier: Optional[str], supplier_name: Optional[str], qty: Optional[float], rate: Optional[float], total: Optional[float], note: str,
               now: str) -> None:
        self.db.execute("UPDATE proposals SET supplier=?, supplier_name=?, qty=?, rate=?, total=?, status_note=?, updated_at=? WHERE id=?",
                        (supplier, supplier_name, qty, rate, total, note, now, proposal_id))

    def update_status(self, proposal_id: str, status: str, note: str, now: str, extra: Mapping[str, Any]) -> None:
        sets, params = ["status=?", "status_note=?", "updated_at=?"], [status, note, now]
        for col in ("po_name", "snoozed_until"):
            if col in extra:
                sets.append(f"{col}=?")
                params.append(extra[col])
        unknown = set(extra) - PROPOSAL_COLUMNS
        if unknown:
            raise ValueError(f"cannot set {', '.join(sorted(unknown))}")
        params.append(proposal_id)
        self.db.execute(f"UPDATE proposals SET {', '.join(sets)} WHERE id=?", params)  # the column names come from the fixed tuple above and every value is bound  # nosec B608


class SqliteAgentRunStore(_Sqlite):
    def add(self, run_id: str, agent: str, started_at: str, finished_at: Optional[str], dry_run: bool, summary_json: Optional[str]) -> None:
        self.db.execute("INSERT INTO agent_runs(id, agent, started_at, finished_at, dry_run, summary_json) VALUES (?,?,?,?,?,?)",
                        (run_id, agent, started_at, finished_at, int(dry_run), summary_json))

    def get(self, run_id: str) -> Optional[Row]:
        return self._one("SELECT * FROM agent_runs WHERE id=?", (run_id,))

    def recent(self, agent: str, limit: int) -> list[Row]:
        return self._all("SELECT started_at, summary_json FROM agent_runs WHERE agent=? AND dry_run=0 ORDER BY started_at DESC LIMIT ?", (agent, limit))

    def list(self, limit: int) -> list[Row]:
        return self._all("SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ?", (limit,))


class SqliteAssistantStore(_Sqlite):
    def cached_answer(self, cache_key: str) -> Optional[str]:
        row = self.db.query_one("SELECT result_json FROM assistant_answers WHERE cache_key=?", (cache_key,))
        return row["result_json"] if row else None

    def put_answer(self, cache_key: str, approval_id: str, question: str, result_json: str, model: str, asked_by: str, created_at: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO assistant_answers(cache_key, approval_id, question, result_json, model, asked_by, created_at) VALUES (?,?,?,?,?,?,?)",
                        (cache_key, approval_id, question, result_json, model, asked_by, created_at))

    def answers_for(self, approval_id: str) -> list[Row]:
        return self._all("SELECT question, result_json, asked_by, created_at FROM assistant_answers WHERE approval_id=? ORDER BY created_at", (approval_id,))

    def log_chat(self, asked_by: str, asked_at: str, question: str, intent: str, status: str, source: str) -> None:
        self.db.execute("INSERT INTO chat_log(asked_by, asked_at, question, intent, status, source) VALUES (?,?,?,?,?,?)", (asked_by, asked_at, question, intent, status, source))

    def chats(self, limit: int) -> list[Row]:
        return self._all("SELECT * FROM (SELECT * FROM chat_log ORDER BY id DESC LIMIT ?) ORDER BY id", (limit,))


class SqliteVendorStore(_Sqlite):
    def add_message(self, message_id: str, purpose: str, ref_id: str, supplier: str, channel: str, address_hash: Optional[str], hash_kind: Optional[str], status: str,
                    error: Optional[str], approval_id: Optional[str], sent_by: Optional[str], sent_at: str) -> None:
        self.db.execute("INSERT INTO vendor_messages(id, purpose, ref_id, supplier, channel, address_hash, hash_kind, status, error, approval_id, sent_by, sent_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (message_id, purpose, ref_id, supplier, channel, address_hash, hash_kind, status, error, approval_id, sent_by, sent_at))

    def messages(self, purpose: str, ref_id: str) -> list[Row]:
        return self._all("SELECT id, supplier, channel, status, error, approval_id, sent_at FROM vendor_messages WHERE purpose=? AND ref_id=? ORDER BY sent_at", (purpose, ref_id))

    def messages_for_approval(self, approval_id: str) -> list[Row]:
        return self._all("SELECT supplier, channel, status, error, sent_at FROM vendor_messages WHERE approval_id=? ORDER BY sent_at", (approval_id,))

    def close_replies(self, purpose: str, ref_id: str, at: str) -> int:
        return self.db.execute("UPDATE vendor_messages SET closed_at=?, address_hash=NULL WHERE purpose=? AND ref_id=? AND closed_at IS NULL", (at, purpose, ref_id)).rowcount

    def forget_hashes_before(self, cutoff: str) -> int:
        return self.db.execute("UPDATE vendor_messages SET address_hash=NULL WHERE address_hash IS NOT NULL AND sent_at<?", (cutoff,)).rowcount

    def open_message(self, address_hash: str, hash_kind: str, since: str) -> Optional[Row]:
        return self._one("SELECT * FROM vendor_messages WHERE address_hash=? AND COALESCE(hash_kind, '')=? AND status='sent' AND sent_at>=? AND closed_at IS NULL "
                         "ORDER BY sent_at DESC LIMIT 1", (address_hash, hash_kind, since))

    def open_chase_refs(self) -> set[str]:
        return {r["ref_id"] for r in self.db.query("SELECT DISTINCT ref_id FROM vendor_messages WHERE purpose='chase' AND closed_at IS NULL")}

    def last_sent_channel(self, approval_id: str) -> Optional[str]:
        row = self.db.query_one("SELECT channel FROM vendor_messages WHERE approval_id=? AND status='sent' ORDER BY sent_at DESC LIMIT 1", (approval_id,))
        return row["channel"] if row else None

    def add_reply(self, message_id: str, supplier: str, purpose: str, ref_id: str, channel: str, text: str, received_at: str, relayed: int, text_hash: str) -> None:
        self.db.execute("INSERT INTO vendor_replies(message_id, supplier, purpose, ref_id, channel, text, received_at, relayed, text_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                        (message_id, supplier, purpose, ref_id, channel, text, received_at, relayed, text_hash))

    def replies(self, purpose: str, ref_id: str) -> list[Row]:
        return self._all("SELECT supplier, channel, text, received_at FROM vendor_replies WHERE purpose=? AND ref_id=? ORDER BY id", (purpose, ref_id))

    def replies_for_approval(self, approval_id: str) -> list[Row]:
        return self._all("SELECT r.supplier, r.text, r.received_at FROM vendor_replies r JOIN vendor_messages m ON m.id = r.message_id WHERE m.approval_id=? ORDER BY r.id",
                         (approval_id,))

    def reply_seen(self, supplier: str, text_hash: str, since: str) -> bool:
        return self.db.query_one("SELECT 1 FROM vendor_replies WHERE supplier=? AND text_hash=? AND received_at>=?", (supplier, text_hash, since)) is not None

    def count_replies(self, supplier: str, since: str, relayed: Optional[int] = None) -> int:
        if relayed is None:
            return int(self.db.query_one("SELECT COUNT(*) AS n FROM vendor_replies WHERE supplier=? AND received_at>=?", (supplier, since))["n"])
        return int(self.db.query_one("SELECT COUNT(*) AS n FROM vendor_replies WHERE supplier=? AND relayed=? AND received_at>=?", (supplier, relayed, since))["n"])


class SqliteChaseStore(_Sqlite):
    def get(self, po_name: str) -> Optional[Row]:
        return self._one("SELECT * FROM delivery_chases WHERE po_name=?", (po_name,))

    def start(self, po_name: str, supplier: str, item_code: str, first_late_on: str, asked_at: str, approval_id: str, now: str) -> None:
        self.db.execute("INSERT INTO delivery_chases(po_name, supplier, item_code, status, first_late_on, asked_at, asks, approval_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (po_name, supplier, item_code, "open", first_late_on, asked_at, 1, approval_id, now))

    def reopen(self, po_name: str, asked_at: str, approval_id: str, now: str) -> None:
        self.db.execute("UPDATE delivery_chases SET status='open', asked_at=?, asks=asks+1, approval_id=?, updated_at=? WHERE po_name=?", (asked_at, approval_id, now, po_name))

    def open_chases(self) -> list[Row]:
        return self._all("SELECT * FROM delivery_chases WHERE status='open'")

    def set_status(self, po_name: str, status: str, now: str) -> None:
        self.db.execute("UPDATE delivery_chases SET status=?, updated_at=? WHERE po_name=?", (status, now, po_name))

    def record_send(self, po_name: str, now: str) -> None:
        self.db.execute("UPDATE delivery_chases SET sent_at=?, sends=sends+1, updated_at=? WHERE po_name=?", (now, now, po_name))


class SqliteReceiptStore(_Sqlite):
    def get(self, receipt: str) -> Optional[Row]:
        return self._one("SELECT * FROM receipt_checks WHERE receipt=?", (receipt,))

    def add(self, receipt: str, supplier: str, status: str, problems_json: str, approval_id: Optional[str], checked_at: str) -> None:
        self.db.execute("INSERT INTO receipt_checks(receipt, supplier, status, problems_json, approval_id, checked_at) VALUES (?,?,?,?,?,?)",
                        (receipt, supplier, status, problems_json, approval_id, checked_at))

    def problems_since(self, since: str) -> list[Row]:
        return self._all("SELECT supplier, problems_json FROM receipt_checks WHERE checked_at>=?", (since,))

    def acknowledge(self, approval_id: str) -> None:
        self.db.execute("UPDATE receipt_checks SET status='acknowledged' WHERE approval_id=?", (approval_id,))


class SqliteQuoteStore(_Sqlite):
    def insert(self, round_id: str, item_code: str, qty: float, uom: str, suppliers_json: str, reply_by: str, requested_by: str, now: str) -> None:
        self.db.execute("INSERT INTO quote_rounds(id, item_code, qty, uom, suppliers_json, status, reply_by, requested_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (round_id, item_code, qty, uom, suppliers_json, "proposed", reply_by, requested_by, now, now))

    def get(self, round_id: str) -> Optional[Row]:
        return self._one("SELECT * FROM quote_rounds WHERE id=?", (round_id,))

    def ids(self, status: Optional[str], limit: int) -> list[str]:
        if status:
            rows = self.db.query("SELECT id FROM quote_rounds WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = self.db.query("SELECT id FROM quote_rounds ORDER BY created_at DESC LIMIT ?", (limit,))
        return [r["id"] for r in rows]

    def update(self, round_id: str, fields: Mapping[str, Any]) -> None:
        unknown = set(fields) - QUOTE_COLUMNS
        if unknown:
            raise ValueError(f"cannot set {', '.join(sorted(unknown))}")
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE quote_rounds SET {cols} WHERE id=?", (*fields.values(), round_id))  # column names are checked against QUOTE_COLUMNS above, values are bound  # nosec B608


class SqliteTransferStore(_Sqlite):
    def insert(self, transfer_id: str, item_code: str, qty: float, uom: str, from_warehouse: str, to_warehouse: str, reason: Optional[str], requested_by: str, now: str) -> None:
        self.db.execute("INSERT INTO stock_transfers(id, item_code, qty, uom, from_warehouse, to_warehouse, status, reason, requested_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (transfer_id, item_code, qty, uom, from_warehouse, to_warehouse, "proposed", reason, requested_by, now, now))

    def get(self, transfer_id: str) -> Optional[Row]:
        return self._one("SELECT * FROM stock_transfers WHERE id=?", (transfer_id,))

    def by_approval(self, approval_id: str) -> Optional[Row]:
        return self._one("SELECT * FROM stock_transfers WHERE approval_id=?", (approval_id,))

    def list(self, status: Optional[str], limit: int) -> list[Row]:
        if status:
            return self._all("SELECT * FROM stock_transfers WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        return self._all("SELECT * FROM stock_transfers ORDER BY created_at DESC LIMIT ?", (limit,))

    def update(self, transfer_id: str, fields: Mapping[str, Any]) -> None:
        unknown = set(fields) - TRANSFER_COLUMNS
        if unknown:
            raise ValueError(f"cannot set {', '.join(sorted(unknown))}")
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE stock_transfers SET {cols} WHERE id=?", (*fields.values(), transfer_id))  # column names are checked against TRANSFER_COLUMNS above, values are bound  # nosec B608


class SqliteCountStore(_Sqlite):
    def upsert_schedule(self, item_code: str, abc_class: str, last_counted_on: Optional[str], due_on: str, now: str) -> None:
        self.db.execute("INSERT INTO cycle_counts(item_code, abc_class, last_counted_on, due_on, updated_at) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(item_code) DO UPDATE SET abc_class=excluded.abc_class, last_counted_on=excluded.last_counted_on, due_on=excluded.due_on, updated_at=excluded.updated_at",
                        (item_code, abc_class, last_counted_on, due_on, now))

    def schedule(self, item_code: str) -> Optional[Row]:
        return self._one("SELECT * FROM cycle_counts WHERE item_code=?", (item_code,))

    def mark_planned(self, item_code: str, planned_on: str) -> None:
        self.db.execute("UPDATE cycle_counts SET planned_on=? WHERE item_code=?", (planned_on, item_code))

    def reviewed(self, document: str, item_code: str, warehouse: str) -> bool:
        return self.db.query_one("SELECT 1 FROM count_reviews WHERE document=? AND item_code=? AND warehouse=?", (document, item_code, warehouse)) is not None

    def add_review(self, document: str, item_code: str, warehouse: str, variance: float, approval_id: Optional[str], now: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO count_reviews(document, item_code, warehouse, variance, approval_id, reviewed_at) VALUES (?,?,?,?,?,?)",
                        (document, item_code, warehouse, variance, approval_id, now))


def sqlite_stock_stores(db: Database) -> StockStores:
    """Make the pack's tables if they are not there, and return the pack's stores over them."""
    ensure_schema(db)
    return StockStores(alerts=SqliteAlertStore(db), notices=SqliteNoticeStore(db), proposals=SqliteProposalStore(db), runs=SqliteAgentRunStore(db),
                       assistant=SqliteAssistantStore(db), vendors=SqliteVendorStore(db), chases=SqliteChaseStore(db), receipts=SqliteReceiptStore(db),
                       quotes=SqliteQuoteStore(db), transfers=SqliteTransferStore(db), counts=SqliteCountStore(db))


def stock_stores_for(core: CoreStores) -> StockStores:
    """The stock pack's stores on the same storage as the core's: one transaction then covers both."""
    if core.kind == "sqlite":
        return sqlite_stock_stores(core.backend)
    if core.kind == "memory":
        return memory_stock_stores(core.backend)
    raise ValueError(f"the stock pack has no stores for the storage {core.kind!r}")

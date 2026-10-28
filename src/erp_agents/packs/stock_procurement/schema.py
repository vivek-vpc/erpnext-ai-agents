"""The tables of the stock pack: alerts and stock notices, purchase proposals, the "Ask why" answer cache, the chat log and the run log of the two agents; and (version 2)
the purchasing follow-up: messages to vendors and their replies, delivery chases, goods receipt checks, quote rounds, and the stock count plan; (version 3) the
requests to move stock between warehouses; and (version 4) the keyed hash, the reply window and the reply limits of the vendor tables.

The core creates its own tables; this pack registers these with `Database.register_schema` (the pack's version is kept apart in `pack_schema`). Every repository and service of
the pack calls `ensure_schema(db)` when it is built, so a database made before packs existed, which already has these tables, just gets its version row.
To change a table: raise VERSION, add MIGRATIONS[VERSION] (a function taking the connection) and a test (tests/test_operations.py shows the mechanism).
"""
from __future__ import annotations

import sqlite3
from typing import Callable

NAME = "stock_procurement"
VERSION = 4


def _v2(conn: sqlite3.Connection) -> None:
    """Version 2 only adds tables (created by SCHEMA below, which runs after the migrations): nothing in the old ones changes."""


def _v3(conn: sqlite3.Connection) -> None:
    """Version 3 only adds a table (stock_transfers): nothing in the old ones changes."""


def _v4(conn: sqlite3.Connection) -> None:
    """Version 4 adds columns to the two vendor tables (the keyed hash, the reply window that closes, the limits on replies). SCHEMA below makes them for a new database; here
    they are added to an old one, and the rows already there keep their values (an old hash stays an old, unkeyed hash: hash_kind stays empty)."""
    for table, column, ddl in (("vendor_messages", "hash_kind", "TEXT"), ("vendor_messages", "closed_at", "TEXT"),
                               ("vendor_replies", "relayed", "INTEGER NOT NULL DEFAULT 0"), ("vendor_replies", "text_hash", "TEXT")):
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if have and column not in have:              # no table at all (a database older than version 2): SCHEMA creates it with the column
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {2: _v2, 3: _v3, 4: _v4}

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id              TEXT PRIMARY KEY,
    item_code       TEXT NOT NULL,
    kind            TEXT NOT NULL,       -- BREACH | EARLY_WARNING
    severity        INTEGER NOT NULL,
    status          TEXT NOT NULL,       -- awaiting|snoozed|dismissed|actioned|resolved|superseded|expired|cancelled
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    analysis_json   TEXT NOT NULL,
    reasoning_text  TEXT NOT NULL,
    approval_id     TEXT,
    snoozed_until   TEXT,
    suppress_until  TEXT,
    cooldown_until  TEXT,
    status_note     TEXT,
    run_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_item ON alerts(item_code, created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);

CREATE TABLE IF NOT EXISTS proposals (
    id             TEXT PRIMARY KEY,
    work_order_id  TEXT NOT NULL,
    item_code      TEXT NOT NULL,
    status         TEXT NOT NULL,        -- awaiting|ordered|po_failed|snoozed|rejected|manual|acknowledged|expired|cancelled
    approval_id    TEXT,
    supplier       TEXT,
    supplier_name  TEXT,
    qty            REAL,
    rate           REAL,
    total          REAL,
    po_name        TEXT,
    ranking_json   TEXT,
    status_note    TEXT,
    snoozed_until  TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proposals_item ON proposals(item_code, created_at);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);

CREATE TABLE IF NOT EXISTS assistant_answers (
    cache_key    TEXT PRIMARY KEY,       -- hash of model + prompt version + facts + question: same input -> same answer
    approval_id  TEXT NOT NULL,
    question     TEXT NOT NULL,
    result_json  TEXT NOT NULL,          -- what the person was shown, including the check outcome
    model        TEXT NOT NULL,
    asked_by     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assistant_approval ON assistant_answers(approval_id, created_at);

CREATE TABLE IF NOT EXISTS stock_notices (
    id            TEXT PRIMARY KEY,
    item_code     TEXT NOT NULL,
    level         TEXT NOT NULL,         -- LOW | URGENT
    status        TEXT NOT NULL,         -- sent | acknowledged | snoozed | cleared | superseded
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    approval_id   TEXT,
    snoozed_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_notices_item ON stock_notices(item_code, created_at);

CREATE TABLE IF NOT EXISTS chat_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asked_by    TEXT NOT NULL,
    asked_at    TEXT NOT NULL,
    question    TEXT NOT NULL,
    intent      TEXT NOT NULL,
    status      TEXT NOT NULL,
    source      TEXT NOT NULL         -- rules | ai-routed
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id           TEXT PRIMARY KEY,
    agent        TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    dry_run      INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT
);

-- version 2: purchasing follow-up and stock intelligence
CREATE TABLE IF NOT EXISTS vendor_messages (
    id            TEXT PRIMARY KEY,
    purpose       TEXT NOT NULL,         -- chase | quote
    ref_id        TEXT NOT NULL,         -- the purchase order chased, or the quote round
    supplier      TEXT NOT NULL,
    channel       TEXT NOT NULL,         -- email | whatsapp
    address_hash  TEXT,                  -- keyed hash (HMAC-SHA256) of the vendor's number or address, to recognise a reply; no contact data is stored; erased when the reply window ends
    status        TEXT NOT NULL,         -- sent | failed
    error         TEXT,
    approval_id   TEXT,
    sent_by       TEXT,
    sent_at       TEXT NOT NULL,
    hash_kind     TEXT,                  -- version 4: 'hmac1' = keyed; empty with a hash = the unkeyed SHA-256 of versions 1 to 3, kept only until its window ends
    closed_at     TEXT                   -- version 4: when the order was received or the price round ended; a reply after this is not taken
);
CREATE INDEX IF NOT EXISTS idx_vendor_messages_ref ON vendor_messages(purpose, ref_id);
CREATE INDEX IF NOT EXISTS idx_vendor_messages_hash ON vendor_messages(address_hash);

CREATE TABLE IF NOT EXISTS vendor_replies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   TEXT NOT NULL,          -- the vendor_messages row it answers
    supplier     TEXT NOT NULL,
    purpose      TEXT NOT NULL,
    ref_id       TEXT NOT NULL,
    channel      TEXT NOT NULL,
    text         TEXT NOT NULL,          -- what the vendor wrote (cleaned and capped); never given to a language model
    received_at  TEXT NOT NULL,
    relayed      INTEGER NOT NULL DEFAULT 0,   -- version 4: 1 = passed on to the people who answered the request, 2 = the 'more replies' line, 0 = kept only
    text_hash    TEXT                    -- version 4: SHA-256 of the text without case and spacing, to drop an identical reply
);

CREATE TABLE IF NOT EXISTS delivery_chases (
    po_name        TEXT PRIMARY KEY,
    supplier       TEXT NOT NULL,
    item_code      TEXT,
    status         TEXT NOT NULL,        -- open | arrived | dismissed
    first_late_on  TEXT NOT NULL,
    asked_at       TEXT,                 -- when a person was last asked whether to chase
    asks           INTEGER NOT NULL DEFAULT 0,
    sent_at        TEXT,                 -- when the vendor was last sent a reminder
    sends          INTEGER NOT NULL DEFAULT 0,
    approval_id    TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipt_checks (
    receipt        TEXT PRIMARY KEY,
    supplier       TEXT NOT NULL,
    status         TEXT NOT NULL,        -- clean | flagged | acknowledged
    problems_json  TEXT,
    approval_id    TEXT,
    checked_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quote_rounds (
    id              TEXT PRIMARY KEY,
    item_code       TEXT NOT NULL,
    qty             REAL NOT NULL,
    uom             TEXT NOT NULL,
    suppliers_json  TEXT NOT NULL,       -- [{supplier, name}] asked for a price
    status          TEXT NOT NULL,       -- proposed | asked | compared | ordered | closed
    reply_by        TEXT,
    asked_at        TEXT,
    approval_id     TEXT,                -- the request to send the messages
    comparison_id   TEXT,                -- the request with the comparison
    requested_by    TEXT NOT NULL,
    status_note     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quote_rounds_item ON quote_rounds(item_code, status);

CREATE TABLE IF NOT EXISTS cycle_counts (
    item_code        TEXT PRIMARY KEY,
    abc_class        TEXT NOT NULL,      -- A | B | C
    last_counted_on  TEXT,
    due_on           TEXT NOT NULL,
    planned_on       TEXT,               -- the week it was last put on a count list
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS count_reviews (
    document     TEXT NOT NULL,
    item_code    TEXT NOT NULL,
    warehouse    TEXT NOT NULL,
    variance     REAL NOT NULL,
    approval_id  TEXT,
    reviewed_at  TEXT NOT NULL,
    PRIMARY KEY (document, item_code, warehouse)
);

-- version 3: moving stock between warehouses
CREATE TABLE IF NOT EXISTS stock_transfers (
    id              TEXT PRIMARY KEY,    -- also the reference written on the Stock Entry, so a retry finds it
    item_code       TEXT NOT NULL,
    qty             REAL NOT NULL,
    uom             TEXT NOT NULL,
    from_warehouse  TEXT NOT NULL,
    to_warehouse    TEXT NOT NULL,
    status          TEXT NOT NULL,       -- proposed | created | rejected | expired | cancelled
    entry_name      TEXT,                -- the draft Stock Entry in the ERP
    reason          TEXT,
    requested_by    TEXT NOT NULL,
    approval_id     TEXT,
    status_note     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_transfers_item ON stock_transfers(item_code, status);
"""


def ensure_schema(db) -> None:
    db.register_schema(NAME, SCHEMA, VERSION, MIGRATIONS)

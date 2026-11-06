"""The database a new installation gets is exactly the one the code made before the rule logic was split into the engine (Phase 2 of the Frappe work).

`tests/golden/schema_golden.json` was written by the code of commit 78372cb: every table and index with its SQL text, and the schema versions (the core at 3, the stock pack at 4). Anyone
who already has a database keeps it working because it is this shape; a change to it needs a numbered migration (core/storage.py MIGRATIONS, the pack's schema.py) and this file made
again on purpose."""
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from erp_agents.core.bootstrap import build_app
from erp_agents.core.config import DEFAULT_CONFIG

pytestmark = pytest.mark.sqlite_only

GOLDEN = json.loads((Path(__file__).parent / "golden" / "schema_golden.json").read_text(encoding="utf-8"))


def fresh_database(tmp_path):
    path = str(tmp_path / "fresh.db")
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["database"]["path"] = path
    app = build_app(config=cfg, db_path=path)
    app.stores.close()
    c = sqlite3.connect(path)
    schema = sorted([r[0], r[1], " ".join((r[2] or "").split())] for r in c.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    versions = {"schema_info": [r[0] for r in c.execute("SELECT version FROM schema_info")], "pack_schema": sorted([list(r) for r in c.execute("SELECT pack, version FROM pack_schema")])}
    c.close()
    return schema, versions


def test_a_new_database_has_the_tables_indexes_and_versions_it_always_had(tmp_path):
    schema, versions = fresh_database(tmp_path)
    assert versions == GOLDEN["versions"]
    assert [row[:2] for row in schema] == [row[:2] for row in GOLDEN["schema"]], "tables or indexes were added, removed or renamed"
    changed = [new[1] for new, old in zip(schema, GOLDEN["schema"]) if new != old]
    assert not changed, f"the SQL of these changed: {changed}"


def test_the_golden_file_holds_the_whole_schema_of_the_core_and_of_the_stock_pack():
    names = {row[1] for row in GOLDEN["schema"]}
    assert {"approvals", "deliveries", "audit_chain", "settings_overrides", "agent_tasks", "job_runs", "inbound_seen", "alerts", "proposals", "vendor_messages", "stock_transfers"} <= names
    assert len(GOLDEN["schema"]) == 41 and GOLDEN["versions"] == {"schema_info": [3], "pack_schema": [["stock_procurement", 4]]}

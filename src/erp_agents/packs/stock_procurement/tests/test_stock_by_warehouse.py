"""Stock shown warehouse by warehouse: the demo ERP, the analysis, the screen's rows, the chat answer, the tool and the reorder message."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import OWNER, VIEWER
from erp_agents.core.api.app import create_app
from erp_agents.core.erp.mock import MockERP
from ai_munimji_engine.packs.stock_procurement.inventory.analysis import where_stock_is


@pytest.fixture
def world(make_app, clock):
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("BOLT", "Bolt", lambda age: 10, 0, group="Fasteners", uom="pcs", warehouses={
        "Stores": {"on_hand": 100, "reserved": 60}, "Shop floor": {"on_hand": 40, "reserved": 0, "incoming": 30}})
    erp.add_item("NUT", "Nut", lambda age: 10, 500, group="Other", reserved=100)
    return make_app(erp=erp), erp


def split(row):
    return [(w["warehouse"], w["on_hand"], w["reserved"], w["incoming"]) for w in row]


def test_the_demo_erp_reports_the_split_and_it_adds_up(world):
    _, erp = world
    s = erp.get_stock(["BOLT"])["BOLT"]
    assert [(w.warehouse, w.on_hand, w.reserved, w.incoming) for w in s.by_warehouse] == [("Stores", 100, 60, 0), ("Shop floor", 40, 0, 30)]       # largest first
    assert sum(w.on_hand for w in s.by_warehouse) == s.on_hand == 140
    only = erp.get_stock(["BOLT"], ["Shop floor"])["BOLT"]
    assert [w.warehouse for w in only.by_warehouse] == ["Shop floor"] and only.on_hand == 40
    nut = erp.get_stock(["NUT"])["NUT"]
    assert [(w.warehouse, w.on_hand) for w in nut.by_warehouse] == [("Main Warehouse", 500)]


def test_the_analysis_carries_the_split_and_the_warehouse_filter_narrows_it(world):
    app, _ = world
    a, _src = app.inventory.analyze_one("BOLT")
    assert split(a.warehouses) == [("Stores", 100, 60, 0), ("Shop floor", 40, 0, 30)] and a.to_dict()["warehouses"] == a.warehouses
    app.settings.update_policy(OWNER, "global", "*", {"warehouses": ["Stores"]})
    a, _src = app.inventory.analyze_one("BOLT")
    assert split(a.warehouses) == [("Stores", 100, 60, 0)] and a.on_hand == 100                    # what is listed is what is counted


def test_the_screens_rows_show_where_the_stock_is(world):
    app, _ = world
    rows = {r["item_code"]: r for r in app.inventory.overview()}
    assert split(rows["BOLT"]["by_warehouse"]) == [("Stores", 100, 60, 0), ("Shop floor", 40, 0, 30)]
    assert sum(w["on_hand"] for w in rows["BOLT"]["by_warehouse"]) == rows["BOLT"]["on_hand"]
    assert [w["warehouse"] for w in rows["NUT"]["by_warehouse"]] == ["Main Warehouse"]


def test_the_items_endpoint_and_the_screen_use_the_split(make_app, clock):
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("BOLT", "Bolt", lambda age: 10, 0, warehouses={"Stores": {"on_hand": 100}, "Shop floor": {"on_hand": 40}})
    cfg = {"auth": {"tokens": [{"token": "view", "user": "acct", "roles": ["viewer"]}]}}
    c = TestClient(create_app(make_app(erp=erp, cfg=cfg)))
    item = c.get("/api/v1/inventory/items", headers={"Authorization": "Bearer view"}).json()[0]
    assert [w["warehouse"] for w in item["by_warehouse"]] == ["Stores", "Shop floor"]
    page = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "warehouseHtml(i)" in page and "i.by_warehouse" in page                                     # the Your stock table reads it


def test_the_chat_answer_says_where_the_stock_is(world):
    app, _ = world
    r = app.chat.ask("How much BOLT do we have?", "tester", True)
    assert "By warehouse: Stores 100 pcs (60 promised to customers); Shop floor 40 pcs." in r["answer"]
    assert dict(r["blocks"][0]["rows"])["Where"] == "Stores 100 pcs (60 promised to customers); Shop floor 40 pcs"
    assert any(f["text"].startswith("By warehouse: Stores 100 pcs") for f in r["facts"])             # checkable, like every other figure in the answer
    one = app.chat.ask("How much NUT do we have?", "tester", True)
    assert "All of it is in Main Warehouse." in one["answer"] and "By warehouse" not in one["answer"] and dict(one["blocks"][0]["rows"])["Where"] == "Main Warehouse"


def test_the_stock_tool_lists_the_warehouses(world):
    app, _ = world
    r = app.toolkit.call("stock_status", {"item_code": "BOLT"}, VIEWER)
    assert r["by_warehouse"] == [{"warehouse": "Stores", "on_hand": 100, "reserved": 60, "on_order": 0, "in_transit_warehouse": False},
                                 {"warehouse": "Shop floor", "on_hand": 40, "reserved": 0, "on_order": 30, "in_transit_warehouse": False}]
    assert r["on_hand"] == sum(w["on_hand"] for w in r["by_warehouse"])


def test_the_reorder_message_names_the_warehouses_only_when_there_are_several(world):
    app, _ = world
    app.settings.update_policy(OWNER, "global", "*", {"min_qty": 1000, "stock_add": []})
    app.inventory.run()
    texts = {r.request.facts["item_code"]: " ".join(l for s in r.request.sections for l in s.lines) for r in app.approvals.list(status="pending")}
    assert "By warehouse: Stores 100 pcs (60 promised to customers); Shop floor 40 pcs." in texts["BOLT"]
    assert "By warehouse" not in texts["NUT"]


def test_a_transit_warehouse_and_empty_ones_read_correctly(world):
    app, _ = world
    a, _src = app.inventory.analyze_one("BOLT")
    a.warehouses[:] = [{"warehouse": "Stores", "on_hand": 5.0, "reserved": 0.0, "incoming": 0.0, "in_transit": False},
                       {"warehouse": "Goods in Transit", "on_hand": 7.0, "reserved": 0.0, "incoming": 0.0, "in_transit": True},
                       {"warehouse": "Old shelf", "on_hand": 0.0, "reserved": 0.0, "incoming": 4.0, "in_transit": False}]
    assert where_stock_is(a) == ["Stores 5 pcs", "Goods in Transit 7 pcs (in transit)"]              # a warehouse holding nothing is left out
    a.warehouses.clear()
    assert where_stock_is(a) == []                                                                   # the ERP did not say: nothing is made up

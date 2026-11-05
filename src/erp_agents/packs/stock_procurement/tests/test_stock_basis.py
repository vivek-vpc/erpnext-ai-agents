"""Which stock columns count, and which warehouses: settings, per-group overrides, the agent, the API."""
import pytest
from fastapi.testclient import TestClient

from conftest import OWNER
from erp_agents.core.api.app import create_app
from erp_agents.core.erp.mock import MockERP


@pytest.fixture
def world(make_app, clock):
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("BOLT", "Bolt", lambda age: 10, 0, group="Fasteners", uom="pcs", warehouses={
        "Stores": {"on_hand": 100, "reserved": 60}, "Shop floor": {"on_hand": 40, "reserved": 0, "incoming": 30}})
    erp.add_item("NUT", "Nut", lambda age: 10, 500, group="Other", reserved=100)
    return make_app(erp=erp), erp


def test_mock_warehouses_sum_and_filter(world):
    _, erp = world
    assert erp.get_stock(["BOLT"])["BOLT"].on_hand == 140 and erp.get_stock(["BOLT"])["BOLT"].incoming == 30
    only = erp.get_stock(["BOLT"], ["Stores"])["BOLT"]
    assert (only.on_hand, only.reserved, only.incoming) == (100, 60, 0)
    assert erp.list_warehouses() == ["Main Warehouse", "Shop floor", "Stores"]


def test_global_warehouse_filter_changes_what_the_agent_sees(world):
    app, _ = world
    assert app.inventory.analyze_one("BOLT")[0].on_hand == 140
    app.settings.update_policy(OWNER, "global", "*", {"warehouses": ["Stores"]})
    a, sources = app.inventory.analyze_one("BOLT")
    assert (a.on_hand, a.reserved, a.available) == (100, 60, 40) and sources["warehouses"] == "global override"
    assert a.policy["warehouses"] == ["Stores"]


def test_warehouse_filter_can_differ_per_item_group(world):
    app, _ = world
    app.settings.update_policy(OWNER, "group", "Fasteners", {"warehouses": ["Shop floor"]})
    rows = {r["item_code"]: r for r in app.inventory.overview()}
    assert rows["BOLT"]["on_hand"] == 40 and rows["BOLT"]["warehouses"] == ["Shop floor"]
    assert rows["NUT"]["on_hand"] == 500 and rows["NUT"]["warehouses"] == []              # other group: all warehouses


def test_the_stock_formula_can_be_changed_per_item(world):
    app, _ = world
    assert app.inventory.analyze_one("NUT")[0].available == 400                           # default: subtract reserved
    app.settings.update_policy(OWNER, "item", "NUT", {"stock_subtract": []})
    nut, src = app.inventory.analyze_one("NUT")
    assert nut.available == 500 and src["stock_subtract"] == "item 'NUT' override"
    assert app.inventory.analyze_one("BOLT")[0].available == 140 - 60                      # untouched


def test_overview_rows_show_every_column_and_how_it_is_used(world):
    app, _ = world
    app.settings.update_policy(OWNER, "global", "*", {"stock_add": ["planned_qty"]})
    bolt = next(r for r in app.inventory.overview() if r["item_code"] == "BOLT")
    uses = {c["column"]: c["use"] for c in bolt["stock_columns"]}
    assert uses["reserved_qty"] == "subtract" and uses["ordered_qty"] == "not used" and uses["planned_qty"] == "add"
    assert bolt["stock_add"] == ["planned_qty"] and bolt["position"] == 140 - 60


def test_message_lists_columns_and_ignored_ones(world):
    app, _ = world
    app.settings.update_policy(OWNER, "global", "*", {"min_qty": 500, "stock_add": []})
    app.inventory.run()
    req = next(r for r in app.approvals.list(status="pending") if r.request.facts["item_code"] == "BOLT").request
    text = " ".join(l for s in req.sections for l in s.lines)
    assert "On hand: 140 pcs (60 reserved, 80 available)." in text and "arrival date unknown" not in text
    assert "Not counted, as set in your stock settings: 30 pcs on open purchase orders" in text
    details = " ".join(l for s in req.details for l in s.lines)
    assert "Reserved for sales orders 60 (subtract)" in details and "On order (purchase orders) 30 (not used)" in details
    assert "All warehouses" in details


@pytest.fixture
def api(make_app, clock):
    cfg = {"auth": {"tokens": [{"token": "owner", "user": "owner", "roles": ["admin", "approver"]}, {"token": "view", "user": "acct", "roles": ["viewer"]}]}}
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("BOLT", "Bolt", lambda age: 10, 0, warehouses={"Stores": {"on_hand": 100}, "Shop floor": {"on_hand": 40}})
    app = make_app(erp=erp, cfg=cfg)
    return app, TestClient(create_app(app))


def H(t):
    return {"Authorization": f"Bearer {t}"}


def test_stock_options_endpoint_feeds_the_settings_screen(api):
    _, c = api
    assert c.get("/api/v1/inventory/stock-options").status_code == 401
    opts = c.get("/api/v1/inventory/stock-options", headers=H("view")).json()
    by = {o["column"]: o for o in opts["columns"]}
    assert list(by)[:3] == ["reserved_qty", "reserved_qty_for_production", "reserved_qty_for_sub_contract"]
    assert by["reserved_qty"]["default"] == "subtract" and by["ordered_qty"]["default"] == "add" and by["indented_qty"]["default"] == "not used"
    assert by["planned_qty"]["label"] == "Planned production (work orders)"
    assert opts["warehouses"] == ["Main Warehouse", "Shop floor", "Stores"] and opts["warehouses_error"] is None


def test_stock_settings_through_the_api_are_validated(api):
    app, c = api
    ok = c.put("/api/v1/settings/policy/global/*", json={"set": {"stock_subtract": ["reserved_qty"], "stock_add": ["planned_qty"], "warehouses": ["Stores"]}}, headers=H("owner"))
    assert ok.status_code == 200
    item = c.get("/api/v1/inventory/items", headers=H("view")).json()[0]
    assert item["on_hand"] == 100 and item["warehouses"] == ["Stores"]
    bad = c.put("/api/v1/settings/policy/global/*", json={"set": {"stock_add": ["bogus_qty"]}}, headers=H("owner"))
    assert bad.status_code == 422 and "unknown stock column" in bad.json()["detail"]
    both = c.put("/api/v1/settings/policy/global/*", json={"set": {"stock_add": ["reserved_qty"]}}, headers=H("owner"))
    assert both.status_code == 422 and "cannot be both" in both.json()["detail"]


def test_stock_monitor_columns_are_optional_validated_and_ordered(api):
    app, c = api
    assert c.get("/api/v1/settings/system", headers=H("view")).json()["monitor_columns"] == []            # nothing extra by default
    opts = c.get("/api/v1/inventory/stock-options", headers=H("view")).json()["monitor_columns"]
    ids = [o["column"] for o in opts]
    assert ids[0] == "available" and {"ordered_qty", "planned_qty", "arrival_date", "position", "projected_qty"} <= set(ids)
    assert dict((o["column"], o["label"]) for o in opts)["ordered_qty"] == "On order (purchase orders)"
    ok = c.put("/api/v1/settings/system", json={"set": {"monitor_columns": ["arrival_date", "ordered_qty", "ordered_qty"]}}, headers=H("owner"))
    assert ok.status_code == 200 and ok.json()["monitor_columns"] == ["ordered_qty", "arrival_date"]       # canonical order, no duplicates
    assert c.put("/api/v1/settings/system", json={"set": {"monitor_columns": ["bogus"]}}, headers=H("owner")).status_code == 422
    assert c.put("/api/v1/settings/system", json={"set": {"monitor_columns": ["ordered_qty"]}}, headers=H("view")).status_code == 403
    assert "monitor_columns" in c.get("/api/v1/settings/schema", headers=H("view")).json()["system"]


def test_overview_rows_carry_the_values_for_every_optional_column(world):
    app, erp = world
    row = next(r for r in app.inventory.overview() if r["item_code"] == "BOLT")
    for key in ("available", "position", "incoming", "planned", "next_arrival", "projected", "stock_columns"):
        assert key in row
    assert row["incoming"] == 30 and row["available"] == 80


def test_a_warehouse_list_that_cannot_be_read_does_not_break_the_screen(api):
    app, c = api
    app.erp.list_warehouses = lambda: (_ for _ in ()).throw(RuntimeError("no permission"))
    opts = c.get("/api/v1/inventory/stock-options", headers=H("view")).json()
    assert opts["warehouses"] == [] and "no permission" in opts["warehouses_error"] and opts["columns"]


# ------------------------------------------------------------------ stock in transit: the owner decides what it counts as
@pytest.fixture
def transit(make_app, clock):
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("TRN", "Transit Bolt", lambda age: 10, 100, uom="pcs", reserved=20, extra_stock={"in_transit": 30})
    erp.add_item("PLAIN", "Plain Bolt", lambda age: 10, 100, uom="pcs")
    return make_app(erp=erp), erp


def test_by_default_stock_in_transit_is_arriving_it_is_planned_with_but_not_sold_from(transit):
    app, _ = transit
    a, src = app.inventory.analyze_one("TRN")
    assert a.policy["in_transit_stock"] == "arriving"
    assert (a.on_hand, a.in_transit, a.reserved, a.available, a.position) == (100, 30, 20, 80, 110)
    row = next(c for c in a.breakdown if c["column"] == "in_transit")
    assert row["value"] == 30 and row["use"] == "add" and row["label"] == "In transit between warehouses"
    assert app.inventory.analyze_one("PLAIN")[0].position == 100                                      # nothing in transit: nothing changes


def test_stock_in_transit_can_count_as_stock_on_the_shelf(transit):
    app, _ = transit
    app.settings.update_policy(OWNER, "global", "*", {"in_transit_stock": "shelf"})
    a, _ = app.inventory.analyze_one("TRN")
    assert (a.on_hand, a.in_transit, a.available, a.position) == (130, 30, 110, 110)
    assert next(c for c in a.breakdown if c["column"] == "in_transit")["use"] == "on hand"
    assert a.cover_days_on_hand == pytest.approx(11.0)                                                  # 110 available at 10 a day: transit stock counts toward "lasts about"


def test_stock_in_transit_can_be_left_out_and_the_analysis_says_so(transit):
    app, _ = transit
    app.settings.update_policy(OWNER, "global", "*", {"in_transit_stock": "ignore"})
    a, _ = app.inventory.analyze_one("TRN")
    assert (a.on_hand, a.in_transit, a.available, a.position) == (100, 30, 80, 80)
    assert next(c for c in a.breakdown if c["column"] == "in_transit")["use"] == "not used"
    assert any("30 pcs in transit between warehouses" in n and "Not counted" in n for n in a.notes)


def test_the_setting_can_differ_per_group_or_item_and_is_validated(transit):
    app, _ = transit
    app.settings.update_policy(OWNER, "item", "TRN", {"in_transit_stock": "shelf"})
    assert app.inventory.analyze_one("TRN")[0].available == 110
    from ai_munimji_engine.core.settings_store import SettingsError
    with pytest.raises(SettingsError):
        app.settings.update_policy(OWNER, "global", "*", {"in_transit_stock": "somewhere"})


def test_overview_rows_and_the_optional_column_show_stock_in_transit(transit):
    from ai_munimji_engine.packs.stock_procurement.settings import MONITOR_COLUMNS
    app, _ = transit
    row = next(r for r in app.inventory.overview() if r["item_code"] == "TRN")
    assert row["in_transit"] == 30 and row["in_transit_stock"] == "arriving"
    assert any(c["column"] == "in_transit" and c["value"] == 30 for c in row["stock_columns"])
    assert "in_transit" in MONITOR_COLUMNS


def test_the_messages_say_where_the_stock_in_transit_went(transit):
    from ai_munimji_engine.packs.stock_procurement.inventory.messages import build_request
    app, _ = transit
    app.settings.update_policy(OWNER, "global", "*", {"min_qty": 500})
    app.inventory.run()
    text = " ".join(l for r in app.approvals.list(status="pending") if r.request.facts.get("item_code") == "TRN" for s in r.request.sections for l in s.lines)
    assert "30 pcs in transit between warehouses" in text                                                # added to the position: listed with what is coming
    app.settings.update_policy(OWNER, "global", "*", {"in_transit_stock": "shelf"})
    a, _ = app.inventory.analyze_one("TRN")
    shelf_text = " ".join(l for s in build_request(a, "alert-1").sections for l in s.lines)
    assert "including 30 pcs in transit between warehouses, counted as on hand as you chose" in shelf_text


def test_the_chat_mentions_stock_in_transit_and_how_it_is_counted(transit):
    app, _ = transit
    r = app.chat.ask("How much TRN do we have?", "tester", True)
    assert "30 pcs are in transit between warehouses (counted as on its way)" in r["answer"]
    assert ["In transit", "30 pcs"] in r["blocks"][0]["rows"]
    assert "in transit" not in app.chat.ask("How much PLAIN do we have?", "tester", True)["answer"]


def test_the_settings_screen_can_offer_the_choice(api):
    _, c = api
    opts = c.get("/api/v1/inventory/stock-options", headers=H("view")).json()
    assert opts["in_transit"]["default"] == "arriving" and opts["in_transit"]["modes"] == ["arriving", "shelf", "ignore"]
    assert "in_transit" in [o["column"] for o in opts["monitor_columns"]]
    assert "in_transit" not in [o["column"] for o in opts["columns"]]                                    # not an ERPNext column: its own setting
    ok = c.put("/api/v1/settings/policy/global/*", json={"set": {"in_transit_stock": "shelf"}}, headers=H("owner"))
    assert ok.status_code == 200
    assert c.get("/api/v1/settings/policy/global/*", headers=H("view")).json()["effective"]["in_transit_stock"] == "shelf"
    bad = c.put("/api/v1/settings/policy/global/*", json={"set": {"in_transit_stock": "somewhere"}}, headers=H("owner"))
    assert bad.status_code == 422
    assert c.put("/api/v1/settings/policy/global/*", json={"set": {"stock_add": ["in_transit"]}}, headers=H("owner")).status_code == 422       # only the dedicated setting can place it
    assert c.put("/api/v1/settings/policy/global/*", json={"set": {"in_transit_stock": "ignore"}}, headers=H("view")).status_code == 403

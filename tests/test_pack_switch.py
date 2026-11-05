"""Switching the stock pack on and off: what answers, what refuses, and that the core carries on without it."""
import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

from conftest import OWNER, VIEWER
from erp_agents.core.api.app import create_app
from erp_agents.core.cli import main
from erp_agents.core.erp.mock import MockERP
from erp_agents.core.packs import PackError, PackRegistry
from erp_agents.core.scheduler import AgentScheduler

TOKENS = [{"token": "o" * 40, "user": "owner", "roles": ["viewer", "approver", "admin"]}]
HDR = {"Authorization": "Bearer " + "o" * 40}
STOCK = "stock_procurement"
OFF = {"enabled": {STOCK: False}}


def build(make_app, clock, packs=None):
    cfg = {"auth": {"tokens": TOKENS}}
    if packs is not None:
        cfg["packs"] = packs
    return make_app(erp=MockERP(today=clock().date()), cfg=cfg)


def pack_off(app):
    app.settings.update_section(OWNER, "packs", OFF)


# ====================================================================== switched off while running
def test_the_stock_pack_is_on_by_default_and_its_parts_are_there(make_app, clock):
    app = build(make_app, clock)
    assert app.packs.enabled_names() == [STOCK] and STOCK in app.packs.loaded
    assert sorted(app.services) == ["alerts", "assistant", "chat", "followup", "inventory", "procurement", "proposals", "stock_insight", "stock_stores", "transfers"]
    assert app.toolkit.registered_for(STOCK) == {t.name: (t.role, t.effect) for t in app.packs.manifests[STOCK].tools}


def test_switched_off_the_tools_refuse_at_once_and_say_why(make_app, clock):
    app = build(make_app, clock)
    assert "error" not in app.toolkit.call("stock_overview", {}, VIEWER)
    pack_off(app)
    res = app.toolkit.call("stock_overview", {}, VIEWER)
    assert res == {"error": "not available: the stock_procurement pack is switched off"}
    assert app.toolkit.specs() == [] and app.toolkit.spec("stock_overview") is None
    rows = app.agent_log.tool_calls(limit=2)
    assert rows[0]["tool"] == "stock_overview" and rows[0]["ok"] == 0                                    # the refusal is recorded like any other call


def test_switched_on_again_the_tools_answer_at_once(make_app, clock):
    app = build(make_app, clock)
    pack_off(app)
    app.settings.update_section(OWNER, "packs", {"enabled": {STOCK: True}})
    assert "error" not in app.toolkit.call("stock_overview", {}, VIEWER)


def test_switched_off_the_routes_answer_404_after_the_sign_in_check(make_app, clock):
    app = build(make_app, clock)
    c = TestClient(create_app(app))
    assert c.get("/api/v1/inventory/items", headers=HDR).status_code == 200
    pack_off(app)
    assert c.get("/api/v1/inventory/items", headers=HDR).status_code == 404
    assert c.post("/api/v1/chat", json={"question": "what is running low?"}, headers=HDR).status_code == 404
    assert c.get("/api/v1/settings/policy/global/*", headers=HDR).status_code == 404
    assert c.get("/api/v1/inventory/items").status_code == 401                                           # a stranger is turned away first: nobody learns which packs exist
    assert c.get("/api/v1/inventory/items", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/v1/approvals", headers=HDR).status_code == 200                                    # the core carries on
    assert c.get("/api/v1/settings/system", headers=HDR).status_code == 200


def test_the_packs_route_lists_each_pack_with_its_state(make_app, clock):
    app = build(make_app, clock)
    c = TestClient(create_app(app))
    rows = {r["name"]: r for r in c.get("/api/v1/packs", headers=HDR).json()}
    assert rows[STOCK]["enabled"] and rows[STOCK]["loaded"] and not rows[STOCK]["restart_required"] and [t["name"] for t in rows[STOCK]["tools"]][:2] == ["stock_overview", "stock_status"]
    assert rows["sales"]["status"] == "skeleton" and not rows["sales"]["enabled"] and not rows["sales"]["restart_required"]
    assert c.get("/api/v1/packs").status_code == 401


def test_the_switch_is_an_ordinary_audited_admin_setting(make_app, clock):
    app = build(make_app, clock)
    c = TestClient(create_app(app))
    assert c.put("/api/v1/settings/module/packs", json={"set": OFF}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.put("/api/v1/settings/module/packs", json={"set": {"enabled": {"Not A Name": True}}}, headers=HDR).status_code == 422
    assert c.put("/api/v1/settings/module/packs", json={"set": OFF}, headers=HDR).status_code == 200
    assert [r for r in app.settings.audit(5) if r["scope_type"] == "packs" and r["field"] == "enabled"]


def test_switched_off_the_scheduled_work_does_nothing(make_app, clock, monkeypatch):
    app = build(make_app, clock)
    sched = AgentScheduler(app)
    ran = []
    monkeypatch.setattr(app.inventory, "run", lambda *a, **k: ran.append("inventory"))
    monkeypatch.setattr(app.procurement, "run", lambda *a, **k: ran.append("procurement"))
    pack_off(app)
    sched._inventory_job()
    sched._procurement_job()
    sched._brief_job()
    assert ran == []
    app.settings.update_section(OWNER, "packs", {"enabled": {STOCK: True}})
    sched._inventory_job()
    assert ran == ["inventory"]


def test_switched_off_the_brief_schedule_is_removed_and_the_stock_jobs_are_the_packs(make_app, clock):
    app = build(make_app, clock)
    sched = AgentScheduler(app)
    app.settings.update_section(OWNER, "agents", {"enabled": True, "daily_brief_cron": "0 8 * * *"})
    sched._sync_brief_job()
    assert sched.sched.get_job("agent_brief") is not None
    pack_off(app)
    sched.reschedule()
    assert sched.sched.get_job("agent_brief") is None


def test_switched_off_the_mcp_server_publishes_no_tools(make_app, clock):
    pytest.importorskip("mcp")
    from mcp import Client
    from erp_agents.core.mcp_server import build_mcp_server
    app = build(make_app, clock)

    async def names():
        async with Client(build_mcp_server(app.toolkit, default_actor=VIEWER)) as c:
            return [t.name for t in (await c.list_tools()).tools], c.instructions
    on, text_on = asyncio.run(names())
    assert "stock_overview" in on and "Stock Assistant for ERPNext" in text_on
    pack_off(app)
    off, text_off = asyncio.run(names())
    assert off == [] and "Stock Assistant" not in text_off and "No tool orders, approves, pays or changes a setting" in text_off     # the core's rules stay


def test_switched_off_the_pack_commands_explain_themselves(make_app, clock, tmp_path, capsys):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(f"database: {{path: '{(tmp_path / 'a.db').as_posix()}'}}\nerp: {{type: mock}}\npacks:\n  enabled:\n    {STOCK}: false\n", encoding="utf-8")
    assert main(["--config", str(cfg), "run-once", "--dry-run"]) == 1
    assert "belongs to the stock_procurement pack, which is switched off" in capsys.readouterr().out
    assert main(["--config", str(cfg), "agents", "brief"]) == 1
    assert "belongs to a pack that is switched off" in capsys.readouterr().out
    assert main(["--config", str(cfg), "agents", "status"]) == 0                                         # a generic action still works


# ====================================================================== switched off from the start
def test_started_with_the_pack_off_its_code_is_not_loaded_and_the_core_works(make_app, clock):
    app = build(make_app, clock, packs=OFF)                                                              # the shared test config also carries stock settings (system.stock_alerts)
    assert app.packs.enabled_names() == [] and app.packs.loaded == {} and app.services == {}
    assert app.toolkit.specs() == [] and app.conversation is None
    with pytest.raises(AttributeError, match="no pack that is switched on provides it"):
        app.inventory
    assert app.settings.system().timezone and not hasattr(app.settings.system(), "check_cron")           # the stock fields are not part of the settings now, but nothing broke
    c = TestClient(create_app(app))
    assert c.get("/api/v1/inventory/items", headers=HDR).status_code == 404
    assert c.get("/api/v1/approvals", headers=HDR).status_code == 200 and c.get("/health", headers=HDR).json()["ok"] is True
    assert c.get("/ui/").status_code == 404 and c.get("/", follow_redirects=False).status_code == 404    # the console belongs to the pack that has screens
    assert {r["name"]: r["enabled"] for r in c.get("/api/v1/packs", headers=HDR).json()}[STOCK] is False


def test_started_with_the_pack_off_saved_stock_settings_are_kept_but_ignored(make_app, clock, tmp_path):
    db = str(tmp_path / "keep.db")
    from erp_agents.core.bootstrap import build_app
    from erp_agents.core.config import DEFAULT_CONFIG
    import copy
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["database"]["path"] = db
    first = build_app(config=cfg, clock=clock, erp=MockERP(today=clock().date()), db_path=db)
    first.settings.update_system(OWNER, {"check_cron": "30 7 * * *", "timezone": "Asia/Kolkata"})
    first.db.close()
    cfg["packs"] = OFF
    second = build_app(config=cfg, clock=clock, erp=MockERP(today=clock().date()), db_path=db)
    assert second.settings.system().timezone == "Asia/Kolkata"                                            # starts fine, the stock value is ignored, not an error
    second.db.close()
    cfg["packs"] = {}
    third = build_app(config=cfg, clock=clock, erp=MockERP(today=clock().date()), db_path=db)
    assert third.settings.system().check_cron == "30 7 * * *"                                             # and it is still there when the pack is on again


def test_a_pack_switched_on_after_start_needs_a_restart_and_says_so(make_app, clock):
    app = build(make_app, clock, packs=OFF)
    app.settings.update_section(OWNER, "packs", {"enabled": {STOCK: True}})
    assert app.packs.is_enabled(STOCK) and STOCK not in app.packs.loaded
    c = TestClient(create_app(app))
    row = {r["name"]: r for r in c.get("/api/v1/packs", headers=HDR).json()}[STOCK]
    assert row["enabled"] and not row["loaded"] and row["restart_required"]
    assert app.toolkit.call("stock_overview", {}, VIEWER)["error"].startswith("unknown tool")           # its tools were never registered: unknown, not half-working


def test_a_skeleton_that_is_switched_on_loads_nothing_and_breaks_nothing(make_app, clock, caplog):
    with caplog.at_level(logging.INFO):
        app = build(make_app, clock, packs={"enabled": {"sales": True, "finance": True}})
    assert app.packs.is_enabled("sales") and "sales" not in app.packs.loaded and STOCK in app.packs.loaded
    assert any("pack sales is a skeleton" in r.getMessage() for r in caplog.records)


def test_a_setting_for_a_pack_that_is_not_installed_is_warned_about(make_app, clock, caplog):
    with caplog.at_level(logging.WARNING):
        build(make_app, clock, packs={"enabled": {"ghost": True}})
    assert any("'ghost'" in r.getMessage() for r in caplog.records)


def test_a_pack_whose_tools_do_not_match_its_manifest_stops_the_application(make_app, clock, monkeypatch):
    from erp_agents.packs.stock_procurement import pack as stock
    real = stock.StockTools.definitions

    def one_too_few(self):
        return [d for d in real(self) if d.spec.name != "prepare_order_request"]
    monkeypatch.setattr(stock.StockTools, "definitions", one_too_few)
    with pytest.raises(PackError, match="declared but not registered: prepare_order_request"):
        build(make_app, clock)

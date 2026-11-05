"""The tools an AI client or agent may use (toolkit.py): the numbers are the application's own, the roles are ERPNext's, nothing orders by itself, nothing personal leaves."""
import json

import pytest

from conftest import APPROVER, OWNER, VIEWER
from ai_munimji_engine.core.common import Actor
from erp_agents.core.erp.mock import MockERP
from ai_munimji_engine.packs.stock_procurement.tools import SPEC, TOOLS
from ai_munimji_engine.core.toolkit import coerce_arguments


@pytest.fixture
def kit(make_app, clock):
    app = make_app(erp=MockERP(today=clock().date()))
    return app, app.toolkit


def test_every_tool_has_a_description_and_a_schema_that_names_its_required_fields():
    assert [t.name for t in TOOLS] == ["stock_overview", "stock_status", "usage_history", "vendor_options", "open_orders", "waiting_requests", "prepare_order_request",
                                       "late_deliveries", "receipt_mismatches", "vendor_scorecard", "demand_forecast", "slow_stock", "substitutes", "stock_counts", "decision_history",
                                       "prepare_quote_request", "prepare_stock_transfer"]
    for t in TOOLS:
        schema = t.input_schema()
        assert len(t.description) > 40 and schema["type"] == "object" and set(t.required) <= set(schema["properties"])
        assert t.effect in ("read", "prepare") and t.role in ("viewer", "approver")
    assert [t.name for t in TOOLS if t.effect == "prepare"] == ["prepare_order_request", "prepare_quote_request", "prepare_stock_transfer"]       # the only tools that do anything; and they only ask a person
    assert all(t.role == "approver" for t in TOOLS if t.effect == "prepare")


def test_the_stock_figures_are_the_ones_the_analysis_gives(kit):
    app, toolkit = kit
    a, _ = app.inventory.analyze_one("BOLT-M8")
    r = toolkit.call("stock_status", {"item_code": "BOLT-M8"}, VIEWER)
    assert r["item_code"] == "BOLT-M8" and r["on_hand"] == pytest.approx(a.on_hand, abs=0.01)
    assert r["status"] == a.status and r["lead_days"] == pytest.approx(a.lead_time_days, abs=0.1)
    assert r["suggested_qty"] == pytest.approx(a.suggested_qty, abs=0.01)


def test_the_overview_lists_the_worst_first_and_counts_every_state(kit):
    app, toolkit = kit
    r = toolkit.call("stock_overview", {}, VIEWER)
    assert r["items_total"] == 10 and sum(r["counts"].values()) == 10
    rank = {row["item_code"]: row["severity_rank"] for row in app.inventory.overview()}
    ranks = [rank[i["item_code"]] for i in r["items"]]
    assert ranks == sorted(ranks, reverse=True) and r["listed"] == len(r["items"]) <= 10
    assert len(toolkit.call("stock_overview", {"limit": 2}, VIEWER)["items"]) <= 2
    assert toolkit.call("stock_overview", {"group": "No Such Group"}, VIEWER)["items_total"] == 0


def test_the_usage_history_adds_up_to_what_erpnext_reports(kit):
    app, toolkit = kit
    r = toolkit.call("usage_history", {"item_code": "BOLT-M8", "weeks": 8}, VIEWER)
    today = app.inventory.today()
    from datetime import timedelta
    raw = app.erp.get_consumption(["BOLT-M8"], today - timedelta(days=56), today)["BOLT-M8"]
    expect = sum(d.qty for d in raw if 0 < (today - d.day).days <= 56)
    assert len(r["weekly"]) == 8 and r["total"] == pytest.approx(expect, abs=0.5) and r["average_per_day"] == pytest.approx(expect / 56, abs=0.05)
    assert [w["week_ending"] for w in r["weekly"]] == sorted(w["week_ending"] for w in r["weekly"])


def test_the_vendor_comparison_is_the_procurement_ranking_best_first(kit):
    app, toolkit = kit
    r = toolkit.call("vendor_options", {"item_code": "BOLT-M8", "qty": 500}, VIEWER)
    assert r["vendors"] and r["best"] == next(v["vendor"] for v in r["vendors"] if v["eligible"])
    scores = [v["score"] for v in r["vendors"] if v["eligible"]]
    assert scores == sorted(scores, reverse=True)
    a, _ = app.inventory.analyze_one("BOLT-M8")
    again = app.procurement.preview("BOLT-M8", 500, a.cover_days_on_hand, float(a.lead_time_days or 7), "pcs")
    assert r["best"] == next(v["name"] for v in sorted(again["vendors"], key=lambda v: (not v["eligible"], -(v.get("score") or 0))) if v["eligible"])


def test_an_unknown_item_gets_suggestions_not_a_guess(kit):
    _, toolkit = kit
    r = toolkit.call("stock_status", {"item_code": "BOLT-M9"}, VIEWER)
    assert "no item" in r["error"] and r["did_you_mean"][0]["item_code"] == "BOLT-M8"
    assert toolkit.call("stock_status", {"item_code": "bolt m8 x 40"}, VIEWER)["item_code"] == "BOLT-M8"          # the exact name works too


def test_numbers_sent_as_text_are_accepted_and_unknown_arguments_are_reported(kit):
    _, toolkit = kit
    r = toolkit.call("vendor_options", {"item_code": "BOLT-M8", "qty": "1,200", "colour": "red"}, VIEWER)
    assert r["qty"] == 1200 and r["ignored_arguments"] == ["colour"]
    assert "must be a number" in toolkit.call("vendor_options", {"item_code": "BOLT-M8", "qty": "lots"}, VIEWER)["error"]
    assert "missing: qty" in toolkit.call("vendor_options", {"item_code": "BOLT-M8"}, VIEWER)["error"]
    assert "one of" in toolkit.call("open_orders", {"kind": "invoice"}, VIEWER)["error"]
    assert "more than zero" in toolkit.call("vendor_options", {"item_code": "BOLT-M8", "qty": -3}, VIEWER)["error"]
    spec = SPEC["usage_history"]
    assert coerce_arguments(spec, {"item_code": "A", "weeks": "4.0"})[0]["weeks"] == 4


def test_roles_are_the_ones_the_rest_of_the_application_uses(kit):
    app, toolkit = kit
    r = toolkit.call("prepare_order_request", {"item_code": "BOLT-M8", "reason": "asked"}, VIEWER)
    assert "not allowed" in r["error"] and app.approvals.list("pending", 50) == []
    assert "error" not in toolkit.call("stock_status", {"item_code": "BOLT-M8"}, VIEWER)
    only_look = Actor("sam", frozenset(), "erpnext", caps=frozenset({"look"}))
    assert "not allowed" in toolkit.call("prepare_order_request", {"item_code": "BOLT-M8", "reason": "x"}, only_look)["error"]
    can_decide = Actor("kim", frozenset(), "erpnext", caps=frozenset({"look", "decide"}))
    assert "not allowed" not in toolkit.call("prepare_order_request", {"item_code": "BOLT-M8", "reason": "x", "qty": 100}, can_decide).get("error", "")
    assert "unknown tool" in toolkit.call("pay_vendor", {}, OWNER)["error"]


def test_preparing_an_order_sends_the_usual_request_and_orders_nothing(kit):
    app, toolkit = kit
    before = len(app.erp.get_documents("Purchase Order", limit=100))
    r = toolkit.call("prepare_order_request", {"item_code": "BOLT-M8", "qty": 800, "reason": "Stock will run out before the next delivery."}, APPROVER)
    assert r["status"] in ("created", "manual") and "error" not in r
    if r["status"] == "created":
        rec = app.approvals.get(r["request_id"])
        assert rec.status == "pending" and rec.request.facts["qty"] == 800 and "Create order" in [o.label for o in rec.request.options]
        assert "Nothing is ordered" in r["note"]
        again = toolkit.call("prepare_order_request", {"item_code": "BOLT-M8", "qty": 800, "reason": "again"}, APPROVER)
        assert again["status"] == "waiting" and len(app.approvals.list("pending", 50)) == 1                     # no second request for the same item
        waiting = toolkit.call("waiting_requests", {"item_code": "BOLT-M8"}, VIEWER)
        assert waiting["waiting"] == 1 and waiting["requests"][0]["request_id"] == r["request_id"]
    assert len(app.erp.get_documents("Purchase Order", limit=100)) == before


def test_a_quantity_below_the_minimum_order_is_refused_before_anything_is_sent(make_app, clock):
    erp = MockERP(today=clock().date())
    erp.item_min_order_qty = lambda code: 500 if code == "BOLT-M8" else None
    app = make_app(erp=erp)
    r = app.toolkit.call("prepare_order_request", {"item_code": "BOLT-M8", "qty": 100, "reason": "x"}, APPROVER)
    assert "minimum order quantity of 500" in r["error"] and app.approvals.list("pending", 50) == []


def test_results_carry_no_people_and_no_customers(kit):
    app, toolkit = kit
    toolkit.call("prepare_order_request", {"item_code": "BOLT-M8", "qty": 800, "reason": "x"}, APPROVER)
    blobs = [toolkit.call(n, a, OWNER) for n, a in (("stock_overview", {}), ("stock_status", {"item_code": "BOLT-M8"}), ("usage_history", {"item_code": "BOLT-M8"}), ("vendor_options", {"item_code": "BOLT-M8", "qty": 100}),
                                                   ("open_orders", {"item_code": "BOLT-M8"}), ("open_orders", {"kind": "requisition"}), ("waiting_requests", {}))]
    text = json.dumps(blobs).lower()
    for forbidden in ("customer", "created_by", "approved_by", "owner", "manager", "@", "password", "token"):
        assert forbidden not in text, forbidden


def test_open_orders_show_ordered_received_and_still_to_come(kit):
    app, toolkit = kit
    r = toolkit.call("open_orders", {}, VIEWER)
    assert r["kind"] == "purchase_order" and r["listed"] == len(r["documents"])
    for d in r["documents"]:
        assert d["still_to_come"] == pytest.approx(max(d["ordered"] - d["received"], 0), abs=0.01)
    assert toolkit.call("open_orders", {"kind": "requisition"}, VIEWER)["kind"] == "requisition"


def test_a_long_result_is_cut_and_says_so(make_app, clock, monkeypatch):
    app = make_app(erp=MockERP(today=clock().date()))
    import ai_munimji_engine.core.toolkit as tk
    monkeypatch.setattr(tk, "MAX_RESULT_CHARS", 900)
    r = app.toolkit.call("stock_overview", {"limit": 25}, VIEWER)
    assert r.get("warning") == "result shortened" and 1 <= len(r["items"]) < r["needing_attention"] + 1


def test_every_call_is_logged_with_who_how_and_whether_it_worked(kit):
    app, toolkit = kit
    toolkit.call("stock_status", {"item_code": "BOLT-M8"}, VIEWER, via="agent", task_id="t1")
    toolkit.call("stock_status", {"item_code": "NOPE-1"}, VIEWER, via="mcp-http")
    toolkit.call("prepare_order_request", {"item_code": "BOLT-M8", "reason": "x"}, VIEWER)
    rows = app.agent_log.tool_calls(limit=10)
    assert [(r["tool"], r["via"], r["ok"]) for r in reversed(rows)] == [("stock_status", "agent", 1), ("stock_status", "mcp-http", 0), ("prepare_order_request", "api", 0)]
    assert rows[-1]["actor"] == "accountant" and json.loads(rows[-1]["args_json"])["item_code"] == "BOLT-M8"
    assert [r["tool"] for r in app.agent_log.tool_calls(task_id="t1")] == ["stock_status"]


def test_the_toolkit_works_without_the_optional_mcp_package():
    import subprocess
    import sys
    code = "import sys; sys.modules['mcp'] = None; import ai_munimji_engine.core.toolkit, erp_agents.core.bootstrap; print('ok')"
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "ok", out.stderr[-400:]

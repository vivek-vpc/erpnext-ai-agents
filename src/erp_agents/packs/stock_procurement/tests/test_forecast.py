"""Demand forecasting for reorder points: seasonality and open sales orders, not only average daily use (inventory/forecast.py)."""
from datetime import timedelta

from conftest import OWNER
from ai_munimji_engine.packs.stock_procurement.erp import SalesDue
from erp_agents.core.erp.mock import MockERP
from erp_agents.packs.stock_procurement.inventory.analysis import STATUS_BREACH, analyze_item
from erp_agents.packs.stock_procurement.inventory.forecast import forecast_reorder_point
from erp_agents.packs.stock_procurement.settings import Policy


def seasonal_erp(clock, peak=30.0):
    """10 a day all year, but the coming 13 days (10 lead + 3 safety) ran at `peak` a day last year."""
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("SEAS", "Seasonal Part", lambda age: peak if 353 <= age <= 365 else 10.0, 200, lead_time_days=10)
    erp.add_vendors("SEAS", [])
    return erp


def test_last_years_season_moves_the_order_point_that_the_plain_rule_misses(make_app, clock):
    app = make_app(erp=seasonal_erp(clock))
    a, _ = app.inventory.analyze_one("SEAS")
    f = a.forecast
    assert a.cover_days_position == 20 and not any(r.code == "lead_time_rule" for r in a.reasons)          # 20 days of cover: the plain rule (13 days) is quiet
    assert f["seasonal_index"] == 2.0 and f["expected_use"] == 260 and f["reorder_point"] == 260             # 10/day x the capped uplift of 2 x 13 days
    assert a.status == STATUS_BREACH and a.reasons[0].code == "forecast_reorder_point"
    text = a.reasons[0].text
    assert "x2" in text and "30" in text and "260" in text and "200" in text                               # the numbers are in the sentence
    assert next(r for r in a.rules if r["rule"] == "forecast_reorder_point")["tripped"]


def test_the_forecast_can_be_switched_off(make_app, clock):
    app = make_app(erp=seasonal_erp(clock))
    app.settings.update_policy(OWNER, "global", "*", {"forecast_reorder_point": False})
    a, _ = app.inventory.analyze_one("SEAS")
    assert a.forecast is None and not any(r.code == "forecast_reorder_point" for r in a.reasons)


def test_a_quiet_season_last_year_never_lowers_the_order_point(make_app, clock):
    app = make_app(erp=seasonal_erp(clock, peak=1.0))
    a, _ = app.inventory.analyze_one("SEAS")
    assert a.forecast["seasonal_index"] == 1.0 and a.forecast["reorder_point"] == 130 and a.status != STATUS_BREACH


def test_open_sales_orders_due_within_the_lead_time_count_as_demand(make_app, clock):
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("SO-ITEM", "Sold Part", lambda age: 10.0, 200, lead_time_days=5)
    erp.add_vendors("SO-ITEM", [])
    today = clock().date()
    erp.sales_orders["SO-ITEM"] = [SalesDue("SO-ITEM", today + timedelta(days=4), 300), SalesDue("SO-ITEM", today + timedelta(days=60), 999)]   # the second is due too late to count
    app = make_app(erp=erp)
    app.settings.update_policy(OWNER, "global", "*", {"stock_subtract": []})                                 # reservations are not taken off available stock here
    a, _ = app.inventory.analyze_one("SO-ITEM")
    assert a.forecast["sales_due"] == 300 and a.forecast["reorder_point"] == 300 and not a.forecast["sales_already_reserved"]
    assert a.status == STATUS_BREACH and a.reasons[0].code == "forecast_reorder_point"
    assert "300 Nos still to deliver on open sales orders" in a.reasons[0].text.replace("pcs", "Nos")


def test_a_sales_order_already_subtracted_as_reserved_is_not_counted_twice(make_app, clock):
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("SO-ITEM", "Sold Part", lambda age: 10.0, 400, lead_time_days=5, reserved=100)
    erp.add_vendors("SO-ITEM", [])
    erp.sales_orders["SO-ITEM"] = [SalesDue("SO-ITEM", clock().date() + timedelta(days=3), 100)]
    app = make_app(erp=erp)                                                                                  # default stock settings subtract "reserved for sales orders"
    a, _ = app.inventory.analyze_one("SO-ITEM")
    assert a.forecast["sales_already_reserved"] and a.forecast["reorder_point"] == 80 and not a.forecast["differs"]
    assert a.available == 300 and a.status != STATUS_BREACH


def test_the_switch_for_sales_orders_alone(make_app, clock):
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("SO-ITEM", "Sold Part", lambda age: 10.0, 200, lead_time_days=5)
    erp.add_vendors("SO-ITEM", [])
    erp.sales_orders["SO-ITEM"] = [SalesDue("SO-ITEM", clock().date() + timedelta(days=2), 500)]
    app = make_app(erp=erp)
    app.settings.update_policy(OWNER, "global", "*", {"stock_subtract": [], "sales_orders_in_forecast": False})
    a, _ = app.inventory.analyze_one("SO-ITEM")
    assert a.forecast["sales_due"] == 0 and a.status != STATUS_BREACH                                       # not even read from the ERP


def test_the_weekly_forecast_shows_the_season_and_the_sales_orders_week_by_week(clock):
    from ai_munimji_engine.packs.stock_procurement.erp import DailyUsage
    today = clock().date()
    usage = [DailyUsage(today - timedelta(days=age), 30.0 if 359 <= age <= 365 else 10.0) for age in range(1, 481)]
    sales = [SalesDue("X", today - timedelta(days=2), 5), SalesDue("X", today + timedelta(days=9), 40)]
    f = forecast_reorder_point(usage, sales, Policy(stock_subtract=[]), today, 10.0, 10.0, 500.0, 480, weeks=3)
    assert [round(w["seasonal_index"], 2) for w in f.weekly] == [2.0, 1.0, 1.0]                              # last year's busy week is the coming one
    assert [w["sales_orders_due"] for w in f.weekly] == [5, 40, 0]                                         # an overdue line belongs to this week
    assert f.weekly[0]["expected_use"] == 140


def test_without_a_years_history_there_is_no_seasonal_guess(clock):
    from ai_munimji_engine.packs.stock_procurement.erp import DailyUsage, ItemInfo, StockInfo
    today = clock().date()
    usage = [DailyUsage(today - timedelta(days=age), 10.0) for age in range(1, 100)]
    a = analyze_item(ItemInfo("NEW", "New", uom="pcs", lead_time_days=5, created=today - timedelta(days=99)), StockInfo(on_hand=500), usage, Policy(), today)
    assert a.forecast["seasonal_index"] == 1.0 and "needs about" in a.forecast["seasonal_note"]

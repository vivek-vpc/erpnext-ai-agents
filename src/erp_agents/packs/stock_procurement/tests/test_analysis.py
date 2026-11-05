"""The maths and the rules. Constant-usage data makes every expected number checkable by hand."""
from datetime import date, timedelta

import pytest

from ai_munimji_engine.packs.stock_procurement.settings import Policy
from erp_agents.core.erp.mock import MockERP
from ai_munimji_engine.packs.stock_procurement.inventory.analysis import SEV_CRITICAL, SEV_EARLY, SEV_HIGH, SEV_INFO, STATUS_BREACH, STATUS_EARLY, STATUS_OK, analyze_item

TODAY = date(2026, 10, 16)


def run(fn, on_hand, policy=None, **kw):
    erp = MockERP(today=TODAY, scenarios=False)
    item = erp.add_item("X", "Item X", fn, on_hand, **kw)
    return analyze_item(item.info, item.stock, item.usage, policy or Policy(), TODAY)


def const(n):
    return lambda age: n


def codes(a):
    return {r.code for r in a.reasons}


# ------------------------------------------------------------------ basics
def test_healthy_item_numbers_are_exact():
    a = run(const(10), 600)
    assert a.status == STATUS_OK and a.severity == SEV_INFO
    assert a.usage.planning_rate == pytest.approx(10)
    assert a.cover_days_on_hand == pytest.approx(60)
    assert a.stockout_date == (TODAY + timedelta(days=60)).isoformat()
    assert a.latest_order_date == (TODAY + timedelta(days=53)).isoformat()   # cover 60 - lead 7
    assert a.reasons == []


def test_min_qty_rule_breaches_and_can_be_off():
    assert codes(run(const(1), 90, Policy(min_qty=100, min_days_cover=None, use_lead_time_rule=False))) == {"min_qty"}
    # at exactly the threshold it still alerts (<=)
    assert run(const(1), 100, Policy(min_qty=100, min_days_cover=None, use_lead_time_rule=False)).status == STATUS_BREACH
    assert run(const(1), 101, Policy(min_qty=100, min_days_cover=None, use_lead_time_rule=False, predictive_enabled=False)).status == STATUS_OK


def test_min_days_rule_boundary():
    p = Policy(use_lead_time_rule=False, min_days_cover=7, predictive_enabled=False)
    assert run(const(10), 69, p).status == STATUS_BREACH          # 6.9 days
    assert run(const(10), 70, p).status == STATUS_BREACH          # exactly 7
    assert run(const(10), 71, p).status == STATUS_OK


def test_lead_time_rule_uses_lead_plus_safety():
    p = Policy(min_days_cover=None, use_lead_time_rule=True, safety_stock_days=3, predictive_enabled=False)
    a = run(const(10), 100, p, lead_time_days=7)                  # cover 10 == 7 + 3
    assert a.status == STATUS_BREACH and codes(a) == {"lead_time_rule"}
    assert run(const(10), 101, p, lead_time_days=7).status == STATUS_OK


def test_admin_lead_time_override_beats_erp_value():
    a = run(const(10), 100, Policy(lead_time_days=20), lead_time_days=7)
    assert a.lead_time_days == 20 and a.lead_time_source == "admin setting"
    b = run(const(10), 100, Policy(), lead_time_days=None)
    assert b.lead_time_days == 7 and "default" in b.lead_time_source and any("No lead time" in n for n in b.notes)


# ------------------------------------------------------------------ severity
def test_critical_when_cover_shorter_than_lead_time():
    a = run(const(10), 50, lead_time_days=7)                      # cover 5 < lead 7
    assert a.severity == SEV_CRITICAL and a.gap_days == pytest.approx(2)


def test_high_when_breach_but_stock_outlasts_lead_time():
    a = run(const(10), 85, lead_time_days=7)                      # cover 8.5 <= 10, but > 7
    assert a.status == STATUS_BREACH and a.severity == SEV_HIGH


def test_no_stock_is_critical():
    a = run(const(10), 0)
    assert a.severity == SEV_CRITICAL and "no_stock" in codes(a)


def test_negative_on_hand_covered_by_open_order_is_not_critical():
    """-15 on hand with 50 on order = 35 once it arrives: no 'no stock' alert."""
    a = run(const(0), -15, Policy(min_qty=None, min_days_cover=None), incoming=50)
    assert a.position == 35
    assert a.status == STATUS_OK and a.severity == SEV_INFO and "no_stock" not in codes(a)
    assert any("on order" in n for n in a.notes)


def test_negative_on_hand_with_no_open_order_is_critical():
    a = run(const(0), -15, Policy(min_qty=None, min_days_cover=None))
    assert a.severity == SEV_CRITICAL and "no_stock" in codes(a)


def test_order_too_small_to_recover_negative_stock_is_still_critical():
    a = run(const(0), -15, Policy(min_qty=None, min_days_cover=None), incoming=10)
    assert a.severity == SEV_CRITICAL and "no_stock" in codes(a)


# ------------------------------------------------------------------ reserved and planned quantities
def test_reserved_stock_is_subtracted_so_it_cannot_hide_a_shortage():
    a = run(const(10), 120, Policy(use_lead_time_rule=False), reserved=90)          # 120 on hand, 90 promised -> 30 free
    assert (a.on_hand, a.reserved, a.available, a.position) == (120, 90, 30, 30)
    assert a.cover_days_on_hand == pytest.approx(3) and a.status == STATUS_BREACH and "min_days_cover" in codes(a)
    assert any("90 pcs of the 120 on hand is reserved, so 30 pcs is available" in n for n in a.notes)
    ignored = run(const(10), 120, Policy(use_lead_time_rule=False, stock_subtract=[], predictive_enabled=False), reserved=90)
    assert ignored.available == 120 and ignored.cover_days_on_hand == pytest.approx(12) and ignored.status == STATUS_OK
    assert any("Not counted, as set in your stock settings: 90 pcs reserved for sales orders" in n for n in ignored.notes)


def test_reservations_larger_than_stock_leave_nothing_available_and_say_so():
    a = run(const(0), 100, Policy(min_qty=None, min_days_cover=None), reserved=100)
    assert a.available == 0 and a.severity == SEV_CRITICAL and "no_stock" in codes(a)
    assert "all 100 pcs on hand is reserved" in a.reasons[0].text
    covered = run(const(0), 100, Policy(min_qty=None, min_days_cover=None), reserved=100, incoming=40)
    assert covered.position == 40 and covered.status == STATUS_OK and any("on order or in production" in n for n in covered.notes)


def test_the_no_stock_sentence_never_says_nothing_is_on_order_when_something_is():
    claimed = run(const(0), 0, Policy(min_qty=None, min_days_cover=None), reserved=40, incoming=40)            # position 0: everything on the way is already promised
    assert claimed.position == 0 and "no_stock" in codes(claimed)
    assert "nothing on order" not in claimed.reasons[0].text and "40 pcs on order or in production is already needed for orders waiting to be filled" in claimed.reasons[0].text
    held = run(const(0), 100, Policy(min_qty=None, min_days_cover=None), reserved=140, incoming=0)
    assert "nothing is on order or in production" in held.reasons[0].text                                      # the old wording stays where it is true
    some = run(const(0), 100, Policy(min_qty=None, min_days_cover=None), reserved=160, incoming=40)
    assert "all 100 pcs on hand is reserved, and the 40 pcs on order or in production is already needed" in some.reasons[0].text


def test_planned_production_counts_as_incoming_stock():
    p = Policy(min_qty=100, min_days_cover=None, use_lead_time_rule=False)
    assert run(const(1), 60, p).status == STATUS_BREACH                                      # position 60 <= 100
    with_plan = run(const(1), 60, p, planned=80)
    assert with_plan.position == 140 and with_plan.status == STATUS_OK
    assert any("planned on production work orders" in n and "arrival date is unknown" in n for n in with_plan.notes)
    off = run(const(1), 60, Policy(min_qty=100, min_days_cover=None, use_lead_time_rule=False, stock_add=["ordered_qty"]), planned=80)
    assert off.position == 60 and off.status == STATUS_BREACH and any("80 pcs planned on production work orders" in n and "Not counted" in n for n in off.notes)


# ------------------------------------------------------------------ the owner chooses the columns
def test_each_stock_column_can_be_added_subtracted_or_left_out():
    extra = {"reserved_production": 30, "requested": 40}
    base = dict(reserved=10, incoming=50, planned=20, extra_stock=extra)
    default = run(const(1), 200, Policy(), **base)                     # defaults: subtract sales + production + subcontract, add ordered + planned
    assert (default.available, default.position) == (200 - 10 - 30, 200 - 40 + 50 + 20)
    only_sales = run(const(1), 200, Policy(stock_subtract=["reserved_qty"], stock_add=[]), **base)
    assert (only_sales.available, only_sales.position) == (190, 190)
    with_requests = run(const(1), 200, Policy(stock_subtract=[], stock_add=["indented_qty", "ordered_qty"]), **base)
    assert (with_requests.available, with_requests.position) == (200, 200 + 40 + 50)
    by = {b["column"]: b["use"] for b in with_requests.breakdown}
    assert by["indented_qty"] == "add" and by["reserved_qty"] == "not used" and by["actual_qty"] == "start"
    assert [b["value"] for b in default.breakdown if b["column"] == "reserved_qty_for_production"] == [30]


def test_choosing_columns_is_validated():
    with pytest.raises(ValueError, match="unknown stock column"):
        Policy(stock_add=["colour_qty"])
    with pytest.raises(ValueError, match="cannot be both added and subtracted"):
        Policy(stock_add=["ordered_qty"], stock_subtract=["ordered_qty", "reserved_qty"])
    p = Policy(stock_add=["ordered_qty", "ordered_qty"], warehouses=[" Stores ", "Stores", ""])
    assert p.stock_add == ["ordered_qty"] and p.warehouses == ["Stores"]


def test_open_orders_not_counted_means_their_arrival_date_is_ignored_too():
    a = run(const(10), 50, Policy(stock_add=[]), lead_time_days=7, incoming=500, arrives_in_days=3)
    assert a.replenish_in_days == 7 and a.position == 50                         # the 3-day arrival is not used


def test_planned_production_alone_rescues_a_zero_stock_item():
    a = run(const(0), 0, Policy(min_qty=None, min_days_cover=None), planned=50)
    assert a.position == 50 and a.status == STATUS_OK and "no_stock" not in codes(a)


def test_zero_stock_with_order_on_the_way_is_not_critical_but_a_low_position_still_alerts():
    assert run(const(0), 0, Policy(min_qty=None, min_days_cover=None), incoming=50).status == STATUS_OK
    a = run(const(0), 0, Policy(min_qty=100, min_days_cover=None), incoming=50)     # position 50 <= minimum 100
    assert a.status == STATUS_BREACH and a.severity == SEV_HIGH and "no_stock" not in codes(a)


# ------------------------------------------------------------------ open purchase orders
def test_open_po_arriving_in_time_suppresses_alert():
    a = run(const(10), 50, lead_time_days=7, incoming=500, arrives_in_days=3)
    assert a.status == STATUS_OK and a.position == 550


def test_open_po_arriving_after_stockout_is_critical():
    a = run(const(10), 50, lead_time_days=7, incoming=500, arrives_in_days=8)
    assert a.severity == SEV_CRITICAL and "stockout_before_arrival" in codes(a)
    assert a.gap_days == pytest.approx(3)


def test_bridge_suggestion_when_po_is_big_enough_but_late():
    a = run(const(10), 50, lead_time_days=7, incoming=500, arrives_in_days=8)
    assert a.suggestion_type == "bridge"
    assert a.suggested_qty == pytest.approx(10 * (3 + 3))        # rate x (gap + safety)


def test_on_order_can_be_excluded_from_position():
    a = run(const(10), 50, Policy(stock_add=[]), lead_time_days=7, incoming=500)
    assert a.position == 50 and a.status == STATUS_BREACH


# ------------------------------------------------------------------ edge cases
def test_zero_consumption_only_quantity_rule_applies():
    a = run(const(0), 50)
    assert a.cover_days_on_hand is None and a.status == STATUS_OK
    b = run(const(0), 50, Policy(min_qty=100))
    assert b.status == STATUS_BREACH and codes(b) == {"min_qty"}
    assert b.suggested_qty is None


def test_new_item_usage_not_diluted_by_days_before_it_existed():
    a = run(const(6), 40, created_days_ago=10, lead_time_days=6)
    assert a.usage.data_days == 10 and a.usage.planning_rate == pytest.approx(6)
    assert a.status == STATUS_BREACH            # thresholds still work
    assert a.confidence == "Low"


def test_predictions_skipped_with_thin_history():
    a = run(const(6), 400, created_days_ago=10)
    assert a.status == STATUS_OK and any("predictions skipped" in n for n in a.notes)


def test_negative_stock_flagged():
    a = run(const(5), -3)
    assert any("negative" in n for n in a.notes)


# ------------------------------------------------------------------ predictive early warnings
def test_days_to_trigger_math():
    p = Policy(use_lead_time_rule=False, min_days_cover=7, lookahead_days=14)
    a = run(const(10), 200, p)                                    # 200/10 - 7 = 13 days
    assert a.status == STATUS_EARLY and a.days_to_trigger == pytest.approx(13)
    assert run(const(10), 220, p).status == STATUS_OK             # 15 days > lookahead


def test_lookahead_is_admin_configurable():
    surge = lambda age: 20 if age <= 14 else 10
    assert run(surge, 300, Policy(usage_method="base")).status == STATUS_OK
    assert run(surge, 300, Policy(usage_method="base", lookahead_days=20)).status == STATUS_EARLY
    assert run(surge, 300, Policy(lookahead_days=5)).status == STATUS_OK


def test_recent_surge_triggers_early_warning_with_trend_driver():
    surge = lambda age: 20 if age <= 14 else 10
    a = run(surge, 300)
    assert a.status == STATUS_EARLY and a.severity == SEV_EARLY
    assert any("Usage is up" in d for d in a.drivers)
    # Same data, long-window-only estimate sees nothing - the early warning really comes from the recent history.
    assert run(surge, 300, Policy(usage_method="base")).status == STATUS_OK


def test_predictive_can_be_switched_off():
    surge = lambda age: 20 if age <= 14 else 10
    assert run(surge, 300, Policy(predictive_enabled=False)).status == STATUS_OK


def test_seasonal_uplift_from_last_year():
    # last year, the same fortnight ran 3x the normal 6/day
    peak = lambda age: 18 if 352 <= age <= 365 else 6
    a = run(peak, 200)
    assert a.status == STATUS_EARLY
    assert a.seasonality.applied_factor == pytest.approx(2.0)     # capped by seasonal_max_factor
    assert any("Seasonal pattern" in d for d in a.drivers)
    assert run(peak, 200, Policy(seasonal_adjust=False)).status == STATUS_OK
    assert run(peak, 200, Policy(history_days=400)).status == STATUS_OK   # not enough history for the comparison


def test_seasonal_cap_is_configurable():
    peak = lambda age: 18 if 352 <= age <= 365 else 6
    a = run(peak, 200, Policy(seasonal_max_factor=1.5))
    assert a.seasonality.applied_factor == pytest.approx(1.5)


# ------------------------------------------------------------------ order suggestion
def test_suggested_quantity_and_rounding():
    base = run(const(10), 60, lead_time_days=7)                   # 10 x (7+3+30) - 60 = 340
    assert base.suggested_qty == pytest.approx(340) and base.suggestion_type == "replenish"
    assert run(const(10), 60, Policy(order_multiple=50), lead_time_days=7).suggested_qty == pytest.approx(350)
    assert run(const(10), 60, Policy(min_order_qty=500), lead_time_days=7).suggested_qty == pytest.approx(500)


def test_busy_week_percentile_and_confidence_reflect_lumpy_demand():
    lumps = lambda age: 70 if age % 7 == 0 else 0
    a = run(lumps, 400)
    assert a.usage.weekly_cv is not None
    steady = run(const(10), 400)
    assert steady.confidence == "High"

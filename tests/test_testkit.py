"""The world of the shared ERP-client checks is itself checked: it is one a real ERP can hold, and its numbers are the ones the checks and the docs rely on."""
from datetime import date, timedelta

from ai_munimji_engine.testkit.contract import CHECKS, Site, run_checks
from ai_munimji_engine.testkit.world import WORLD, default_names

TODAY = date(2026, 11, 6)


def test_the_world_has_no_problems():
    assert WORLD.problems() == []


def test_a_world_with_a_shelf_below_zero_is_refused():
    from dataclasses import replace

    from ai_munimji_engine.testkit.world import Sale

    broken = replace(WORLD, sales=(*WORLD.sales, Sale("moq", "wh_a", 50, 10)))                  # 5 on the shelf, 50 sold
    assert any("below zero" in p for p in broken.problems())
    two = replace(WORLD, suppliers=(*WORLD.suppliers, WORLD.suppliers[0]))
    assert any("two things" in p for p in two.problems())


def test_the_numbers_the_checks_rely_on():
    s = WORLD.stock("steady")
    assert (s["on_hand"], s["reserved"], s["incoming"]) == (338.0, 30.0, 0.0)       # wh_a 200 + 100 + 50 + 60 + 10 - 120 = 300, wh_b 40 - 2 = 38
    assert WORLD.stock("low")["incoming"] == 30.0 and WORLD.on_hand("low", "wh_a") == 8.0
    assert WORLD.stock("moving") == {"on_hand": 3.0, "in_transit": 7.0, "reserved": 0.0, "incoming": 0.0, "places": {"wh_a": 3.0, "wh_t": 7.0}}
    assert WORLD.next_arrival("low", TODAY) == TODAY - timedelta(days=3) + timedelta(days=5)
    assert WORLD.first_movement("steady", TODAY) == TODAY - timedelta(days=100) and WORLD.first_movement("new", TODAY) is None


def test_the_vendor_figures_the_checks_expect():
    offers = WORLD.offers("steady", TODAY, TODAY - timedelta(days=180))
    assert sorted(offers) == ["s1", "s2", "s3"], "the disabled supplier is never offered"
    s1, s2, s3 = offers["s1"], offers["s2"], offers["s3"]
    assert (s1["orders"], s1["unit_price"], s1["avg_price"], s1["lead_time_days"], s1["on_time_rate"], s1["payment_days"], s1["listed"]) == (2, 14.0, 13.0, 9.5, 0.5, 30.0, True)
    assert (s2["orders"], s2["unit_price"], s2["lead_time_days"], s2["on_time_rate"], s2["payment_days"], s2["listed"]) == (1, 11.0, 4.0, 1.0, None, False)
    assert (s3["orders"], s3["unit_price"], s3["listed"]) == (0, None, True)
    quoted = WORLD.offers("steady", TODAY, TODAY - timedelta(days=180), quote_rule_today=TODAY)
    assert quoted["s3"]["quote_price"] == 10.5 and quoted["s2"]["quote_price"] is None, "the expired quotation is not used"
    assert WORLD.offers("steady", TODAY, TODAY - timedelta(days=35))["s1"]["orders"] == 0


def test_every_check_names_data_it_needs_and_the_writing_ones_are_marked():
    assert len(CHECKS) >= 40
    assert all(isinstance(c.needs, frozenset) for c in CHECKS)
    assert {c.name for c in CHECKS if c.writes} >= {"create_purchase_order_makes_a_draft_with_the_reference_and_the_approver", "pressing_create_twice_gives_one_order"}


def test_a_check_that_needs_data_the_driver_lacks_is_skipped_with_the_reason_and_never_passed():
    from ai_munimji_engine.testkit.contract import ALL_DATA

    from erp_agents.core.erp.world_erp import world_site

    erp, site = world_site(today=TODAY)
    results = run_checks(erp, site, has=ALL_DATA - {"taxes"}, only={"get_purchase_taxes_follows_the_tax_rule", "healthcheck_is_true"})
    by = {r.name: r for r in results}
    assert by["get_purchase_taxes_follows_the_tax_rule"].status == "skip" and "taxes" in by["get_purchase_taxes_follows_the_tax_rule"].detail
    assert by["healthcheck_is_true"].status == "pass"


def test_a_client_that_is_wrong_fails_and_one_that_raises_is_an_error():
    from erp_agents.core.erp.world_erp import world_site

    erp, site = world_site(today=TODAY)
    erp.get_stock = lambda codes, warehouses=None: {c: __import__("erp_agents.core.erp.mock", fromlist=["x"]).StockInfo(on_hand=1.0) for c in codes}    # a client that answers 1 for everything
    wrong = run_checks(erp, site, only={"get_stock_sums_the_shelves_of_an_item"})[0]
    assert wrong.status == "fail" and "on hand" in wrong.detail

    def boom(*a, **k):
        raise RuntimeError("the ERP fell over")

    erp.list_warehouses = boom
    raised = run_checks(erp, site, only={"list_warehouses_leaves_out_groups_and_disabled_ones"})[0]
    assert raised.status == "error" and "fell over" in raised.detail


def test_default_names_are_flat_and_readable():
    names = default_names(WORLD)
    assert names["wh_a"] == "Contract A" and names["steady"] == "CT-STEADY" and names["duty"] == "Contract Duty" and names["s1"] == "Contract reliable supplier"
    assert len(set(names.values())) == len(names)
    s = Site(today=TODAY, names=names)
    assert s.n("steady") == "CT-STEADY" and s.key_of("CT-STEADY") == "steady"

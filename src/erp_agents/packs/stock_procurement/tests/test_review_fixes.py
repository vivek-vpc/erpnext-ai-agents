"""Fixed after review: each test here failed before the fix it belongs to (see CHANGELOG, "Fixed after review")."""
import json
import math
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from ai_munimji_engine.core.toolkit import ToolSpec, coerce_arguments
from ai_munimji_engine.packs.stock_procurement.erp import ReceiptLine, TaxRow, TaxSetup, VendorOffer
from ai_munimji_engine.packs.stock_procurement.followup.scorecard import score_vendors
from ai_munimji_engine.packs.stock_procurement.inventory.analysis import STATUS_OK, analyze_item
from ai_munimji_engine.packs.stock_procurement.procurement.agent import ProcurementAgent
from ai_munimji_engine.packs.stock_procurement.procurement.messages import build_po_request, expected_arrival
from ai_munimji_engine.packs.stock_procurement.procurement.scoring import Need, rank_vendors
from ai_munimji_engine.packs.stock_procurement.settings import Policy, ProcurementSettings
from erp_agents.core.erp.mock import MockERP

TODAY = date(2026, 10, 16)
TEN_YEARS_NOTE = "more than 10 years of cover at the current usage"


# ------------------------------------------------------------------------------------------------ dates that leave the calendar
def one_small_sale(age):
    return 1 if age == 40 else 0


def analyse(on_hand, policy=None, **kw):
    erp = MockERP(today=TODAY, scenarios=False)
    item = erp.add_item("X", "Item X", one_small_sale, on_hand, **kw)
    return analyze_item(item.info, item.stock, item.usage, policy or Policy(), TODAY)


@pytest.mark.parametrize("on_hand", [100_000, 1e6, 1e7, 1e12])
def test_a_huge_stock_with_one_small_sale_is_analysed_not_dropped(on_hand):
    a = analyse(on_hand)                                         # raised OverflowError: the item was silently never monitored
    assert a.kind == "OK" and a.status == STATUS_OK
    assert a.stockout_date is None and a.latest_order_date is None
    assert a.cover_days_on_hand > 3650                           # the number is kept
    assert any(TEN_YEARS_NOTE in n for n in a.notes), a.notes
    json.dumps(a.to_dict())                                      # and it can still be stored


def test_a_cover_inside_the_horizon_still_has_its_dates():
    erp = MockERP(today=TODAY, scenarios=False)
    item = erp.add_item("Y", "Item Y", lambda age: 2, 6000)       # 2 a day: 3,000 days of cover
    b = analyze_item(item.info, item.stock, item.usage, Policy(), TODAY)
    assert b.cover_days_on_hand == pytest.approx(3000)
    assert b.stockout_date == (TODAY + timedelta(days=3000)).isoformat()
    assert not any(TEN_YEARS_NOTE in n for n in b.notes)


@pytest.mark.parametrize("lead", [1e12, 4000, float("nan"), float("inf")])
def test_an_absurd_lead_time_on_the_item_is_ignored_with_a_note(lead):
    a = analyse(50, lead_time_days=lead)                         # raised OverflowError (or looped for ever with a calendar)
    assert a.lead_time_days == Policy().default_lead_time_days and "default" in a.lead_time_source
    assert any("lead time" in n.lower() and "not credible" in n.lower() for n in a.notes), a.notes
    assert a.replenish_date == (TODAY + timedelta(days=a.lead_time_days)).isoformat()


def test_a_lead_time_inside_ten_years_is_still_used():
    a = analyse(50, lead_time_days=3000)
    assert a.lead_time_days == 3000 and a.lead_time_source == "ERP item record"


def test_an_absurd_admin_lead_time_falls_back_to_the_items_and_says_so():
    a = analyse(50, Policy(lead_time_days=1e12), lead_time_days=12)
    assert (a.lead_time_days, a.lead_time_source) == (12, "ERP item record")
    assert any("admin setting" in n and "not credible" in n for n in a.notes), a.notes


def test_an_absurd_admin_lead_time_or_safety_stock_does_not_break_the_analysis():
    assert analyse(50, Policy(lead_time_days=1e12)).lead_time_days == 7     # the item record's own figure (MockERP gives 7)
    a = analyse(50, Policy(safety_stock_days=1e12))              # the forecast looked a trillion days ahead
    assert a.kind in ("OK", "BREACH", "EARLY_WARNING")


def test_the_arrival_date_of_an_order_survives_absurd_lead_times():
    assert expected_arrival(TODAY, 1e12) is None and expected_arrival(TODAY, float("nan")) is None and expected_arrival(TODAY, float("inf")) is None
    assert expected_arrival(TODAY, None) is None
    assert expected_arrival(TODAY, 9.4) == TODAY + timedelta(days=9)


# ------------------------------------------------------------------------------------------------ prices that cannot be right
def offer(code, name, price, *, quote=None, lead=3, on_time=0.9, avg=None):
    return VendorOffer(code, name, price, price if avg is None else avg, lead, on_time, 8, TODAY - timedelta(days=9), 30, False, quote_price=quote,
                       quote_ref="SQ-1" if quote is not None else None, quote_qty=1000.0 if quote is not None else None)


NEED = Need(qty=100, cover_days=30, fallback_lead_days=7, uom="kg")
GOOD = offer("GOOD", "Good Co", 10.0)


@pytest.mark.parametrize("bad", [-5.0, 0.0, float("nan"), float("inf"), float("-inf")])
def test_a_price_that_is_not_a_positive_number_never_wins(bad):
    r = rank_vendors([GOOD, offer("BAD", "Bad Co", bad)], NEED, ProcurementSettings())
    assert r.best.supplier == "GOOD"
    worst = next(v for v in r.vendors if v.supplier == "BAD")
    assert not worst.eligible and "not a positive number" in worst.why_not and "check the last order in the ERP" in worst.why_not
    json.dumps(r.to_dict(), allow_nan=False)                     # no NaN in what is stored


@pytest.mark.parametrize("bad", [-5.0, 0.0, float("nan")])
def test_a_bad_price_alone_leaves_no_winner(bad):
    r = rank_vendors([offer("BAD", "Bad Co", bad)], NEED, ProcurementSettings())
    assert r.best is None and r.vendors[0].why_not


@pytest.mark.parametrize("bad", [-5.0, 0.0, float("nan")])
def test_a_bad_quotation_price_never_wins(bad):
    quoted = offer("BAD", "Bad Co", 12.0, quote=bad)
    r = rank_vendors([GOOD, quoted], NEED, ProcurementSettings(use_quotations="prefer"))
    assert r.best.supplier == "GOOD"
    worst = next(v for v in r.vendors if v.supplier == "BAD")
    assert not worst.eligible and "not a positive number" in worst.why_not and "quotation" in worst.why_not


def test_a_quotation_price_is_not_looked_at_when_quotations_are_switched_off():
    r = rank_vendors([offer("A", "A Co", 12.0, quote=-5.0)], NEED, ProcurementSettings(use_quotations="off"))
    assert r.best.supplier == "A" and r.best.unit_price == 12.0


# ------------------------------------------------------------------------------------------------ the on-time rate is a share
@pytest.mark.parametrize("given, kept", [(1.7, 1.0), (-0.2, 0.0), (0.5, 0.5)])
def test_the_on_time_rate_is_kept_between_zero_and_one(given, kept):
    r = rank_vendors([offer("A", "A Co", 10.0, on_time=given), offer("B", "B Co", 10.0, on_time=0.5)], NEED, ProcurementSettings())
    a = next(v for v in r.vendors if v.supplier == "A")
    assert a.on_time_rate == kept and a.scores["reliability"] == pytest.approx(kept * 100)


def test_an_on_time_rate_that_is_not_a_number_counts_as_unknown():
    r = rank_vendors([offer("A", "A Co", 10.0, on_time=float("nan"))], NEED, ProcurementSettings())
    assert r.best.on_time_rate is None and r.best.scores["reliability"] == 50.0
    assert any("no receipts on record" in f for f in r.best.flags)


# ------------------------------------------------------------------------------------------------ typed quantities
def qty_of(raw):
    stub = SimpleNamespace(erp=SimpleNamespace())
    return ProcurementAgent._new_qty(stub, raw, SimpleNamespace(qty=None, item_code="X"), "kg")


@pytest.mark.parametrize("raw, expected", [
    ("12,5", 12.5), ("0,5", 0.5), ("1,5", 1.5), ("1,50", 1.5),                       # one comma, one or two digits after it: a decimal comma
    ("1,000", 1000.0), ("1,00,000", 100000.0), ("1,500", 1500.0), ("1,5000", 15000.0),   # anything else: thousands separators
    ("12,345,678", 12345678.0), ("1,234.5", 1234.5), ("800", 800.0), ("12.5", 12.5), (" 12,5 ", 12.5),
])
def test_commas_in_a_typed_quantity(raw, expected):
    qty, why = qty_of(raw)
    assert (qty, why) == (expected, "")


@pytest.mark.parametrize("raw", ["1.234,56", "abc", "", "nan", "inf", "-3", "0"])
def test_a_quantity_that_is_not_one_is_refused(raw):
    qty, why = qty_of(raw)
    assert qty is None and why


def test_a_number_argument_typed_as_text_reads_the_same_way():
    spec = ToolSpec("t", "d", {"qty": {"type": "number"}}, (), "viewer", "read")
    assert coerce_arguments(spec, {"qty": "12,5"})[0] == {"qty": 12.5}
    assert coerce_arguments(spec, {"qty": "1,000"})[0] == {"qty": 1000.0}
    assert coerce_arguments(spec, {"qty": "1,00,000"})[0] == {"qty": 100000.0}
    with pytest.raises(ValueError):
        coerce_arguments(spec, {"qty": "1.234,56"})


# ------------------------------------------------------------------------------------------------ a return is not a delivery
def receipt_line(receipt, *, posted, due=TODAY, received=10.0, accepted=10.0, rejected=0.0):
    return ReceiptLine(receipt=receipt, posting_date=posted, supplier="S", supplier_name="Sup", item_code="I", item_name="I", uom="pcs",
                       received_qty=received, accepted_qty=accepted, rejected_qty=rejected, rate=5.0, po_due=due)


def test_a_return_line_with_a_negative_quantity_does_not_count_as_a_delivery():
    delivered = [receipt_line(f"R{i}", posted=TODAY - timedelta(days=i), rejected=1.0, accepted=9.0) for i in range(6)]
    back = receipt_line("RET", posted=TODAY + timedelta(days=30), received=-4.0, accepted=-4.0, rejected=-2.0)         # sent back a month after the due date
    plain = score_vendors(delivered, {}, {})[0]
    with_return = score_vendors(delivered + [back], {}, {})[0]
    for field in ("lines", "receipts", "on_time", "avg_days_late", "received", "rejected", "score"):
        assert getattr(with_return, field) == getattr(plain, field), field


# ------------------------------------------------------------------------------------------------ a total before tax is not comparable
def tax_setup(*rows, template="GST"):
    return TaxSetup(template, "tax rule", tuple(rows), (), "India")


def tax_row():
    return TaxRow("Customs duty", "On Net Total", 25.0, 0.0, "Valuation", "Add", None, "Customs duty")


NO_SETUP = TaxSetup(None, "none", (), ("no Tax Rule matched this vendor and there is no default purchase taxes template",))


def mixed(settings=None):
    """TAXED costs 9 + 25% duty = 11.25 a unit; BARE says 10 and has no tax setup at all: it looks cheaper and is not known to be."""
    offers = [offer("TAXED", "Taxed Co", 9.0), offer("BARE", "Bare Co", 10.0)]
    return rank_vendors(offers, NEED, settings or ProcurementSettings(weight_cost=100, weight_delivery=0, weight_reliability=0, weight_terms=0),
                        {"TAXED": tax_setup(tax_row()), "BARE": NO_SETUP})


def test_a_total_before_tax_next_to_totals_with_tax_is_marked_as_not_comparable():
    r = mixed()
    assert r.best.supplier == "BARE"                             # kept: the ranking is not changed, it is explained
    note = r.notes[0]                                            # first: it is the thing to read before the winner
    assert "Bare Co has no tax setup" in note and "before tax" in note and "not comparable" in note
    assert any("not comparable" in f and "before tax" in f for f in r.best.flags)


def test_the_note_reaches_the_request_text():
    r = mixed()
    wo = {"item_code": "X", "item_name": "Item X", "uom": "kg", "severity": "HIGH"}
    request = build_po_request("P1", wo, r, r.best, NEED, ProcurementSettings(), TODAY)
    lines = [line for section in request.sections for line in section.lines]
    assert any("Bare Co has no tax setup" in line and "not comparable" in line for line in lines)
    assert any(line.startswith("Bare Co: ") and "not comparable" in line for line in lines)      # the winner's own flag, in Notes


def test_no_note_when_every_vendor_has_taxes_or_none_has():
    both = rank_vendors([offer("A", "A Co", 9.0), offer("B", "B Co", 9.5)], NEED, ProcurementSettings(), {"A": tax_setup(tax_row()), "B": tax_setup(tax_row())})
    none = rank_vendors([offer("A", "A Co", 9.0), offer("B", "B Co", 9.5)], NEED, ProcurementSettings(), {"A": NO_SETUP, "B": NO_SETUP})
    for r in (both, none):
        assert not any("not comparable" in n for n in r.notes) and not any("not comparable" in f for v in r.vendors for f in v.flags)


def test_two_vendors_without_a_setup_are_named_together():
    offers = [offer("T", "Taxed Co", 9.0), offer("B1", "Bare One", 10.0), offer("B2", "Bare Two", 10.5)]
    r = rank_vendors(offers, NEED, ProcurementSettings(), {"T": tax_setup(tax_row()), "B1": NO_SETUP, "B2": NO_SETUP})
    assert any("Bare One" in n and "Bare Two" in n and "not comparable" in n for n in r.notes)

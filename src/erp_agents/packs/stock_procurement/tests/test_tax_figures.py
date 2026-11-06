"""The tax figures a request was made with are kept with it. A person who changes the quantity or the vendor does not read the ERP's tax setup again (in the Frappe app that person
may not be allowed to see Tax Rules); and when no tax figures are available at all, the request says that taxes are not included instead of showing a total without saying so."""
from datetime import date, timedelta

import pytest

from conftest import OWNER
from ai_munimji_engine.core.workorders import REORDER_NEEDED
from ai_munimji_engine.packs.stock_procurement.erp import TaxRow, TaxSetup, VendorOffer
from ai_munimji_engine.packs.stock_procurement.procurement import taxes
from ai_munimji_engine.packs.stock_procurement.procurement.scoring import Need, rank_vendors
from ai_munimji_engine.packs.stock_procurement.settings import ProcurementSettings
from erp_agents.core.erp.mock import MockERP

TODAY = date(2026, 10, 16)
COST_ONLY = dict(weight_cost=100, weight_delivery=0, weight_reliability=0, weight_terms=0)
LOCAL = TaxSetup("India GST - ABC", "default template", (TaxRow("GST", "On Net Total", 18, 0.0, "Total", "Add", None, "GST"),), (), "Local")
ABROAD = TaxSetup("Import - ABC", "tax rule", (TaxRow("Basic customs duty", "On Net Total", 25, 0.0, "Valuation", "Add", None, "Duty"),
                                               TaxRow("IGST", "On Previous Row Total", 18, 0.0, "Total", "Add", 1, "IGST")), (), "Overseas")
NOT_ALLOWED = "Not permitted to read Tax Rule"


def offer(code, name, price, country="India"):
    return VendorOffer(code, name, price, price, 3, 0.95, 8, TODAY - timedelta(days=9), 30, False, country=country)


@pytest.fixture
def world(make_app, clock):
    erp = MockERP(today=clock().date(), scenarios=False)
    erp.add_item("BOLT", "Bolt", lambda age: 40, 240, group="Fasteners", uom="pcs", lead_time_days=5)
    erp.add_vendors("BOLT", [offer("LOCAL", "Local Co", 10.0), offer("ABROAD", "Overseas Ltd", 9.0, "Vietnam")])
    erp.purchase_taxes = {"LOCAL": LOCAL, "ABROAD": ABROAD}
    app = make_app(erp=erp)
    app.settings.update_procurement(OWNER, COST_ONLY)
    return app, erp


def wo(**kw):
    return {"item_code": "BOLT", "item_name": "Bolt", "item_group": "Fasteners", "uom": "pcs", "suggested_qty": 500.0, "cover_days": 20.0,
            "lead_time_days": 5.0, "daily_rate": 40.0, "on_hand": 240.0, "severity": "HIGH", "reasons": ["Low."], **kw}


def propose(app):
    app.workorders.emit(REORDER_NEEDED, wo())
    assert app.procurement.run().proposed == 1
    return app.approvals.list(status="pending")[0]


def person_cannot_read_taxes(erp):
    """What a Purchase User gets from the Frappe client: the Tax Rule is not theirs to read. Returns the list of calls made."""
    calls = []

    def refuse(supplier, item, day):
        calls.append(supplier)
        raise PermissionError(NOT_ALLOWED)

    erp.get_purchase_taxes = refuse
    return calls


def the_vendor(app, supplier):
    return next(v for v in app.proposals.list()[0].ranking["vendors"] if v["supplier"] == supplier)


def request_text(rec):
    return " ".join(line for s in rec.request.sections for line in s.lines)


# ------------------------------------------------------------------ the figures are kept
def test_the_figures_the_job_used_are_kept_with_the_proposal(world):
    app, _ = world
    propose(app)
    kept = app.proposals.list()[0].ranking["tax_setups"]
    assert set(kept) == {"LOCAL", "ABROAD"} and kept["ABROAD"]["template"] == "Import - ABC" and [r["description"] for r in kept["ABROAD"]["rows"]] == ["Basic customs duty", "IGST"]
    assert taxes.setup_from_dict(kept["ABROAD"]) == ABROAD and taxes.setup_from_dict(kept["LOCAL"]) == LOCAL                  # what was kept reads back as what was read


def test_a_setup_that_could_not_be_read_is_not_kept_as_if_it_were_an_answer(world):
    app, erp = world
    real = erp.get_purchase_taxes
    erp.get_purchase_taxes = lambda sup, item, day: (_ for _ in ()).throw(PermissionError(NOT_ALLOWED)) if sup == "ABROAD" else real(sup, item, day)
    propose(app)
    assert set(app.proposals.list()[0].ranking["tax_setups"]) == {"LOCAL"}


# ------------------------------------------------------------------ a person changing the request uses the kept figures
def test_changing_the_quantity_uses_the_kept_figures_and_does_not_read_the_erp_again(world):
    app, erp = world
    rec = propose(app)
    assert rec.request.facts["supplier"] == "LOCAL"
    calls = person_cannot_read_taxes(erp)                                    # from here on the person who presses the button cannot read Tax Rules
    res = app.approvals.decide(rec.id, "change_qty", OWNER, "app", {"qty": 800})
    assert res.ok and res.status == "revised" and calls == []
    abroad = the_vendor(app, "ABROAD")
    assert abroad["goods_total"] == 7200.0 and abroad["tax_total"] == 1800.0 and abroad["total"] == 9000.0 and abroad["tax_template"] == "Import - ABC"       # duty on 800, from the kept rows
    assert the_vendor(app, "LOCAL")["total"] == 8000.0 and res.approval.request.facts["supplier"] == "LOCAL"
    assert set(app.proposals.list()[0].ranking["tax_setups"]) == {"LOCAL", "ABROAD"}                                  # and they stay kept for the next change
    assert "Taxes are not included" not in request_text(res.approval)


def test_the_kept_figures_win_over_what_the_erp_would_say_now(world):
    app, erp = world
    rec = propose(app)
    erp.purchase_taxes = {}                                                  # somebody changed the tax setup after the request was made
    res = app.approvals.decide(rec.id, "change_qty", OWNER, "app", {"qty": 800})
    assert the_vendor(app, "ABROAD")["tax_total"] == 1800.0 and res.approval.request.facts["supplier"] == "LOCAL"


def test_only_a_vendor_without_kept_figures_is_asked_again(world):
    app, erp = world
    real = erp.get_purchase_taxes
    erp.get_purchase_taxes = lambda sup, item, day: (_ for _ in ()).throw(PermissionError(NOT_ALLOWED)) if sup == "ABROAD" else real(sup, item, day)
    rec = propose(app)                                                       # ABROAD could not be read when the request was made
    asked = []
    erp.get_purchase_taxes = lambda sup, item, day: asked.append(sup) or real(sup, item, day)
    app.approvals.decide(rec.id, "change_qty", OWNER, "app", {"qty": 800})
    assert asked == ["ABROAD"] and the_vendor(app, "ABROAD")["tax_total"] == 1800.0
    assert set(app.proposals.list()[0].ranking["tax_setups"]) == {"LOCAL", "ABROAD"}


def test_changing_the_vendor_keeps_the_figures_too(world):
    app, erp = world
    rec = propose(app)
    calls = person_cannot_read_taxes(erp)
    res = app.approvals.decide(rec.id, "change_vendor", OWNER, "app", {"supplier": "ABROAD"})
    assert res.ok and res.status == "revised" and calls == []
    assert res.approval.request.facts["supplier"] == "ABROAD" and the_vendor(app, "ABROAD")["tax_total"] == 1125.0
    assert set(app.proposals.list()[0].ranking["tax_setups"]) == {"LOCAL", "ABROAD"}


# ------------------------------------------------------------------ no tax data at all: said plainly
def test_with_no_tax_data_at_all_the_request_says_taxes_are_not_included(world):
    app, erp = world
    person_cannot_read_taxes(erp)
    rec = propose(app)
    text = request_text(rec)
    assert "Taxes are not included in these totals" in text and "before tax" in text
    abroad = the_vendor(app, "ABROAD")
    assert abroad["tax_total"] == 0.0 and abroad["tax_lines"] == [] and abroad["total"] == abroad["goods_total"] == 4500.0
    assert any("duties and taxes may be missing: could not read the ERP's tax setup" in f for f in abroad["flags"])
    assert "tax_setups" not in app.proposals.list()[0].ranking                                                          # nothing kept: there was nothing to keep


def test_changing_a_request_that_has_no_tax_data_still_says_so(world):
    app, erp = world
    person_cannot_read_taxes(erp)
    rec = propose(app)
    res = app.approvals.decide(rec.id, "change_qty", OWNER, "app", {"qty": 800})
    assert res.ok and "Taxes are not included in these totals" in request_text(res.approval)


def test_an_erp_with_no_tax_rule_keeps_its_own_wording(world):
    app, erp = world
    erp.purchase_taxes = {}
    text = request_text(propose(app))
    assert "Duties and taxes are not in this comparison" in text and "Taxes are not included in these totals" not in text


def test_ranking_tells_unreadable_from_not_set_up():
    offers = [offer("LOCAL", "Local Co", 10.0)]
    s = ProcurementSettings(**COST_ONLY)
    need = Need(qty=100, cover_days=30, fallback_lead_days=7, uom="pcs")
    unreadable = rank_vendors(offers, need, s, {"LOCAL": TaxSetup(None, "none", (), ("could not read the ERP's tax setup: no",), unavailable=True)})
    none_set = rank_vendors(offers, need, s, {"LOCAL": TaxSetup(None, "none", (), ("no Tax Rule matched this vendor",))})
    assert any(n.startswith("Taxes are not included in these totals") for n in unreadable.notes) and unreadable.tax_setups == {}
    assert any(n.startswith("Duties and taxes are not in this comparison") for n in none_set.notes) and list(none_set.tax_setups) == ["LOCAL"]     # "the ERP applies none" is an answer
    assert rank_vendors(offers, need, s).tax_setups == {} and "tax_setups" not in rank_vendors(offers, need, s).to_dict()


def test_taxes_off_stays_off_and_stores_nothing(world):
    app, _ = world
    app.settings.update_procurement(OWNER, {"include_taxes": False})
    propose(app)
    assert "tax_setups" not in app.proposals.list()[0].ranking

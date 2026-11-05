"""Vendors are compared on landed cost, using the duties and taxes the ERP itself applies (Tax Rule -> purchase taxes template)."""
import json
from datetime import date, timedelta
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from conftest import APPROVER, OWNER
from erp_agents.core.api.app import create_app
from erp_agents.core.assistant.facts import build_facts
from erp_agents.packs.stock_procurement.settings import ProcurementSettings
from erp_agents.core.workorders import REORDER_NEEDED
from erp_agents.core.erp.base import PurchaseOrderDraft, TaxRow, TaxSetup, VendorOffer
from erp_agents.core.erp.erpnext import ERPNextClient
from erp_agents.core.erp.mock import MockERP
from erp_agents.packs.stock_procurement.procurement import taxes
from erp_agents.packs.stock_procurement.procurement.messages import build_po_request
from erp_agents.packs.stock_procurement.procurement.scoring import Need, rank_vendors

TODAY = date(2026, 10, 16)


def row(desc, charge_type="On Net Total", rate=0.0, category="Total", add_deduct="Add", row_id=None, amount=0.0, account=""):
    return TaxRow(desc, charge_type, rate, amount, category, add_deduct, row_id, account or desc)


def setup(*rows, template="Import - ABC", source="tax rule", notes=()):
    return TaxSetup(template, source, tuple(rows), tuple(notes), "Overseas")


def offer(code, name, price, country="India", lead=3, on_time=0.95, credit=30, orders=8):
    return VendorOffer(code, name, price, price, lead, on_time, orders, TODAY - timedelta(days=9), credit, False, country=country)


NEED = Need(qty=100, cover_days=30, fallback_lead_days=7, uom="kg")
COST_ONLY = dict(weight_cost=100, weight_delivery=0, weight_reliability=0, weight_terms=0)


# ------------------------------------------------------------------ the arithmetic, the way ERPNext does it
def test_every_erpnext_charge_type_category_and_sign():
    s = setup(
        row("Basic customs duty", "On Net Total", 10, "Valuation"),                                    # 10% of 1000                = 100  (cost)
        row("IGST", "On Previous Row Total", 18, "Total", row_id=1),                                  # 18% of (1000 + 100)        = 198  (payable, not item cost)
        row("Port handling", "On Item Quantity", 0.5, "Valuation and Total"),                         # 0.5 x 100 units            =  50  (cost)
        row("Clearing fee", "Actual", amount=25, category="Valuation"),                               # fixed                      =  25  (cost)
        row("TDS", "On Net Total", 2, "Total", add_deduct="Deduct"),                                  # -2% of 1000                = -20  (payable, not cost)
        row("Surcharge", "On Previous Row Amount", 10, "Valuation", row_id=3),                        # 10% of row 3's 50          =   5  (cost)
    )
    r = taxes.compute(s, unit_price=10, qty=100, count_non_cost=False)
    assert [(l.name, l.amount, l.in_cost) for l in r.lines] == [("Basic customs duty", 100.0, True), ("IGST", 198.0, False), ("Port handling", 50.0, True),
                                                                ("Clearing fee", 25.0, True), ("TDS", -20.0, False), ("Surcharge", 5.0, True)]
    assert r.counted == 180.0 and r.not_cost == 178.0 and r.problems == []
    assert taxes.compute(s, 10, 100, count_non_cost=True).counted == 358.0 and taxes.compute(s, 10, 100, True).not_cost == 0.0
    assert r.lines[1].description == "18% of the running total up to row 1 (goods plus every charge so far)"
    assert r.lines[4].description.endswith("(deducted)")


def test_rows_that_cannot_be_calculated_are_reported_not_guessed():
    r = taxes.compute(setup(row("Bad ref", "On Previous Row Amount", 5, "Valuation", row_id=7), row("Odd", "On Something Else", 5, "Valuation")), 10, 100, False)
    assert r.counted == 0 and len(r.problems) == 2
    assert "refers to row 7" in r.problems[0] and "cannot calculate" in r.problems[1]


# ------------------------------------------------------------------ the point of it all
LOCAL = setup(row("GST", "On Net Total", 18, "Total"), template="India GST - ABC", source="default template")
ABROAD = setup(row("Basic customs duty", "On Net Total", 25, "Valuation"), row("IGST", "On Previous Row Total", 18, "Total", row_id=1), template="Import - ABC")


def test_a_cheaper_vendor_with_heavy_import_duty_no_longer_wins():
    offers = [offer("LOCAL", "Local Co", 10.0), offer("ABROAD", "Overseas Ltd", 9.0, "Vietnam")]
    s = ProcurementSettings(**COST_ONLY)
    assert rank_vendors(offers, NEED, s).best.supplier == "ABROAD"                                     # price only (no tax setups asked)
    r = rank_vendors(offers, NEED, s, {"LOCAL": LOCAL, "ABROAD": ABROAD})
    assert r.best.supplier == "LOCAL"
    abroad = next(v for v in r.vendors if v.supplier == "ABROAD")
    assert (abroad.goods_total, abroad.tax_total, abroad.total) == (900.0, 225.0, 1125.0)
    assert abroad.tax_not_cost == pytest.approx(0.18 * 1125) and abroad.tax_template == "Import - ABC" and abroad.tax_source == "tax rule"
    assert "unit price is lower (9.00 vs 10.00)" in abroad.why_not and "costs 125.00 more in total once duties and taxes are added" in abroad.why_not
    local = next(v for v in r.vendors if v.supplier == "LOCAL")
    assert (local.total, local.tax_total, local.tax_not_cost) == (1000.0, 0.0, 180.0)               # the GST is creditable: shown, not cost


def test_taxes_the_erp_books_as_payable_only_count_when_you_say_so():
    offers = [offer("LOCAL", "Local Co", 10.0)]
    off = rank_vendors(offers, NEED, ProcurementSettings(), {"LOCAL": LOCAL}).best
    on = rank_vendors(offers, NEED, ProcurementSettings(include_recoverable_taxes_in_cost=True), {"LOCAL": LOCAL}).best
    assert (off.total, off.tax_not_cost) == (1000.0, 180.0) and (on.total, on.tax_not_cost) == (1180.0, 0.0)


def test_switching_taxes_off_leaves_the_comparison_on_price():
    v = rank_vendors([offer("A", "A", 9.0, "Vietnam")], NEED, ProcurementSettings(include_taxes=False), {"A": ABROAD}).best
    assert (v.total, v.goods_total, v.tax_lines, v.flags) == (900.0, 900.0, [], [])


def test_when_the_erp_has_no_tax_setup_for_a_vendor_it_says_so():
    none = TaxSetup(None, "none", (), ("no Tax Rule matched this vendor and there is no default purchase taxes template",))
    r = rank_vendors([offer("A", "Alpha", 9.0)], NEED, ProcurementSettings(), {"A": none})
    assert any("duties and taxes may be missing: no Tax Rule matched" in f for f in r.best.flags)
    assert any("Duties and taxes are not in this comparison" in n and "Tax Rule, Purchase Taxes and Charges Template" in n for n in r.notes)
    missing = rank_vendors([offer("A", "Alpha", 9.0)], NEED, ProcurementSettings(), {}).best
    assert any("the ERP has no Tax Rule or default purchase taxes template" in f for f in missing.flags)
    r = rank_vendors([offer("A", "Alpha", 9.0), offer("B", "Beta", 12.0)], NEED, ProcurementSettings(no_tax_setup="exclude"), {"A": none, "B": LOCAL})
    assert r.best.supplier == "B" and "your settings exclude such vendors" in next(v for v in r.vendors if v.supplier == "A").why_not
    partial = rank_vendors([offer("A", "Alpha", 9.0), offer("B", "Beta", 12.0)], NEED, ProcurementSettings(), {"A": none, "B": LOCAL})
    assert not any("not in this comparison" in n for n in partial.notes)                            # one vendor has taxes: no blanket note


def test_problems_and_erp_notes_reach_the_vendors_flags():
    s = setup(row("Odd", "On Something Else", 5, "Valuation"), notes=("some template rows are included in the price",))
    flags = rank_vendors([offer("A", "Alpha", 9.0)], NEED, ProcurementSettings(), {"A": s}).best.flags
    assert any("may be wrong" in f and "cannot calculate" in f for f in flags) and any("included in the price" in f for f in flags)


# ------------------------------------------------------------------ what the owner reads
def test_the_request_shows_landed_cost_the_erp_template_and_what_was_left_out():
    cfg = ProcurementSettings()
    r = rank_vendors([offer("ABROAD", "Overseas Ltd", 9.0, "Vietnam"), offer("LOCAL", "Local Co", 12.0)], NEED, cfg, {"LOCAL": LOCAL, "ABROAD": ABROAD})
    wo = {"item_code": "BOLT", "item_name": "Bolt", "uom": "kg", "suggested_qty": 100, "on_hand": 50, "daily_rate": 2, "reasons": ["Low."]}
    best = r.best
    assert best.supplier == "ABROAD"                                                                   # 1125 landed vs 1200
    req = build_po_request("p1", wo, r, best, NEED, cfg, TODAY)
    assert req.headline == "Order 100 kg of Bolt from Overseas Ltd for 900.00 (1,125.00 in total with duties and taxes)."
    rec = " ".join(req.sections[0].lines)
    assert "goods 900.00; with duties and taxes the total cost is 1,125.00" in rec
    assert "Duties and taxes counted from your ERP's template 'Import - ABC' (chosen by a Tax Rule): Basic customs duty 225.00 (25% of goods)" in rec
    assert "Payable but not part of the item cost (not counted): IGST 202.50" in rec
    detail = " ".join(l for s in req.details for l in s.lines)
    assert "ERP template 'Import - ABC', tax rule" in detail and "ERP category Valuation" in detail and "ERP category Total, not item cost" in detail
    assert "payable but not item cost 202.50, not counted" in detail


# ------------------------------------------------------------------ through the agent, the purchase order and the API
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


def run(app):
    app.workorders.emit(REORDER_NEEDED, wo())
    app.procurement.run()
    return app.approvals.list(status="pending")[0]


def test_agent_compares_on_what_the_erp_says_applies(world):
    app, _ = world
    rec = run(app)
    assert rec.request.facts["supplier"] == "LOCAL"                                                     # 9 x 1.25 = 11.25 landed vs 10.00
    prop = app.proposals.list()[0]
    abroad = next(v for v in prop.ranking["vendors"] if v["supplier"] == "ABROAD")
    assert abroad["total"] == 5625.0 and abroad["goods_total"] == 4500.0 and abroad["tax_total"] == 1125.0 and abroad["tax_template"] == "Import - ABC"


def test_the_draft_purchase_order_gets_the_template_the_erp_chose_for_that_vendor(world):
    app, erp = world
    app.approvals.decide(run(app).id, "approve", APPROVER, "app")
    assert erp.purchase_orders[0]["supplier"] == "LOCAL" and erp.purchase_orders[0]["taxes_template"] == "India GST - ABC"


def test_no_template_means_none_is_sent_and_the_request_says_taxes_are_missing(world):
    app, erp = world
    erp.purchase_taxes = {}
    rec = run(app)
    assert any("Duties and taxes are not in this comparison" in l for s in rec.request.sections for l in s.lines)
    app.approvals.decide(rec.id, "approve", APPROVER, "app")
    assert erp.purchase_orders[0]["taxes_template"] is None


def test_one_vendors_failed_tax_lookup_does_not_stop_the_proposal(world):
    app, erp = world
    real = erp.get_purchase_taxes
    erp.get_purchase_taxes = lambda sup, item, day: (_ for _ in ()).throw(RuntimeError("ERP down")) if sup == "ABROAD" else real(sup, item, day)
    rec = run(app)
    abroad = next(v for v in app.proposals.list()[0].ranking["vendors"] if v["supplier"] == "ABROAD")
    assert rec.request.facts["supplier"] == "ABROAD"                                                    # no taxes known for it: cheaper on price, but flagged
    assert any("could not read the ERP's tax setup: ERP down" in f for f in abroad["flags"])


def test_taxes_off_does_not_even_ask_the_erp(world):
    app, erp = world
    app.settings.update_procurement(OWNER, {"include_taxes": False})
    calls = []
    erp.get_purchase_taxes = lambda *a: calls.append(a)
    assert run(app).request.facts["supplier"] == "ABROAD" and calls == []


def test_ask_why_can_explain_the_duties(world):
    app, _ = world
    rec = run(app)
    facts = " | ".join(f.text for f in build_facts(app.approvals.get(rec.id), ranking=app.proposals.list()[0].ranking))
    assert "ERP template 'Import - ABC', tax rule" in facts and "once duties and taxes are added" in facts


def test_settings_the_owner_used_to_write_are_gone_and_old_saves_cannot_break_loading(world):
    app, _ = world
    app.stores.settings.save("procurement", "*", json.dumps({"tax_rules": [{"name": "old"}], "home_country": "India", "weight_cost": 77}), "owner", "2026-10-01T00:00:00+00:00", [])
    s = app.settings.procurement()
    assert s.weight_cost == 77 and not hasattr(s, "tax_rules") and not hasattr(s, "home_country")
    with pytest.raises(Exception, match="Unknown procurement field"):
        app.settings.update_procurement(OWNER, {"tax_rules": []})
    app.settings.update_procurement(OWNER, {"snooze_days": 4})
    assert "tax_rules" not in app.settings.get_overrides("procurement", "*")                              # dropped on the next save


def test_the_api_exposes_only_what_the_owner_may_set(make_app, clock):
    cfg = {"auth": {"tokens": [{"token": "owner", "user": "owner", "roles": ["admin", "approver"]}, {"token": "view", "user": "a", "roles": ["viewer"]}]}}
    erp = MockERP(today=clock().date())
    erp.purchase_taxes = {"SUP-BUDGET": ABROAD, "SUP-PRIME": LOCAL}
    app = make_app(erp=erp, cfg=cfg)
    c = TestClient(create_app(app))
    H = lambda t: {"Authorization": f"Bearer {t}"}
    eff = c.get("/api/v1/settings/procurement", headers=H("view")).json()["effective"]
    assert eff["include_taxes"] is True and eff["no_tax_setup"] == "flag" and "tax_rules" not in eff and "home_country" not in eff
    assert c.put("/api/v1/settings/procurement", json={"set": {"no_tax_setup": "exclude", "include_recoverable_taxes_in_cost": True}}, headers=H("owner")).status_code == 200
    assert c.put("/api/v1/settings/procurement", json={"set": {"tax_rules": []}}, headers=H("owner")).status_code == 422
    assert c.put("/api/v1/settings/procurement", json={"set": {"no_tax_setup": "maybe"}}, headers=H("owner")).status_code == 422
    prev = c.get("/api/v1/procurement/preview/BOLT-M8?qty=100", headers=H("view")).json()
    budget = next(v for v in prev["vendors"] if v["supplier"] == "SUP-BUDGET")
    assert budget["tax_template"] == "Import - ABC" and budget["country"] == "Vietnam" and budget["total"] > budget["goods_total"]


# ------------------------------------------------------------------ the ERPNext adapter reads ERPNext's own tax setup
class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._d


class Session:
    """Stands in for ERPNext: lists by doctype (equality filters honoured), single documents by (doctype, name), and order creation."""

    def __init__(self, tables=None, docs=None):
        self.tables, self.docs = tables or {}, docs or {}
        self.headers, self.calls, self.posts = {}, [], []

    def get(self, url, params=None, timeout=None):
        rest = url.split("/api/resource/")[1]
        if "/" in rest:
            doctype, name = rest.split("/", 1)
            doc = self.docs.get((doctype, unquote(name)))
            self.calls.append(("doc", doctype, unquote(name)))
            return Resp({"data": doc}) if doc is not None else Resp({"exception": "not found"}, 404)
        flt = json.loads(params["filters"])
        self.calls.append(("list", rest, flt))
        rows = self.tables.get(rest, [])
        if isinstance(rows, int):
            return Resp({"exception": "denied"}, rows)
        for f in flt:
            if f[1] == "=":
                rows = [r for r in rows if r.get(f[0], f[2]) == f[2]]
        return Resp({"data": rows})

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return Resp({"data": {"name": "PO-1"}})


def tax_rule(template, name=None, **kw):
    base = {"name": name or f"TR-{template}", "tax_type": "Purchase", "purchase_tax_template": template, "supplier": None, "supplier_group": None,
            "tax_category": "Import", "company": None, "priority": 1, "from_date": None, "to_date": None}
    base.update({k: None for k in ("billing_city", "billing_county", "billing_state", "billing_zipcode", "billing_country",
                                   "shipping_city", "shipping_county", "shipping_state", "shipping_zipcode", "shipping_country", "item", "item_group")})
    return {**base, **kw}


TPL_ROWS = [
    {"description": "Basic customs duty", "charge_type": "On Net Total", "rate": 10, "category": "Valuation", "add_deduct_tax": "Add", "account_head": "Duty - ABC"},
    {"description": "IGST", "charge_type": "On Previous Row Total", "rate": 18, "category": "Total", "add_deduct_tax": "Add", "row_id": 1, "account_head": "IGST - ABC"},
]
SUPPLIER = [{"name": "S-A", "supplier_group": "Overseas", "tax_category": "Import", "country": "Vietnam"}]


def erp(session, **kw):
    return ERPNextClient("http://erp.local", "K", "S", session=session, **kw)


def docs_for(*templates):
    d = {("Purchase Taxes and Charges Template", t): {"taxes": TPL_ROWS} for t in templates}
    d[("Item", "BOLT")] = {"taxes": []}
    return d


def setup_for(rules, company=None, defaults=(), supplier=None, **kw):
    tables = {"Supplier": supplier or SUPPLIER, "Tax Rule": rules, "Purchase Taxes and Charges Template": list(defaults)}
    names = {r["purchase_tax_template"] for r in rules} | {d["name"] for d in defaults}
    s = Session(tables, docs_for(*names))
    return erp(s, company=company).get_purchase_taxes("S-A", "BOLT", TODAY), s


def test_the_tax_rule_choice_follows_erpnexts_own_logic_and_the_template_rows_are_read():
    got, s = setup_for([tax_rule("Import - ABC")], company="ABC Corp")
    assert (got.template, got.source, got.tax_category) == ("Import - ABC", "tax rule", "Import")
    assert ("list", "Tax Rule", [["tax_type", "=", "Purchase"]]) in s.calls
    assert [(r.description, r.charge_type, r.rate, r.category, r.row_id) for r in got.rows] == [
        ("Basic customs duty", "On Net Total", 10.0, "Valuation", None), ("IGST", "On Previous Row Total", 18.0, "Total", 1)]
    r = taxes.compute(got, 10, 100, False)                                                              # and the maths on what was read
    assert (r.counted, r.not_cost) == (100.0, 198.0)


def test_the_supplier_tax_category_must_match_exactly_like_in_erpnext():
    rules = [tax_rule("Domestic", tax_category="Domestic"), tax_rule("Blank category", tax_category=None)]
    assert setup_for(rules)[0].template is None                                                         # supplier is "Import": neither rule has that category
    no_cat = [{**SUPPLIER[0], "tax_category": None}]
    assert setup_for(rules, supplier=no_cat)[0].template == "Blank category"                            # a supplier with no category matches the blank rule only


def test_the_rule_that_sets_more_criteria_wins_then_priority():
    generic, by_group, by_supplier = tax_rule("Generic"), tax_rule("By group", supplier_group="Overseas"), tax_rule("By supplier", supplier="S-A")
    assert setup_for([generic, by_group])[0].template == "By group"                                     # 2 criteria beat 1
    assert setup_for([generic, by_group, tax_rule("Both", supplier_group="Overseas", supplier="S-A")])[0].template == "Both"
    assert setup_for([tax_rule("Low", priority=1), tax_rule("High", priority=5)])[0].template == "High"
    assert setup_for([tax_rule("Other group", supplier_group="Local"), by_supplier])[0].template == "By supplier"   # a rule for another group is out
    assert setup_for([tax_rule("Other supplier", supplier="S-Z")])[0].template is None


def test_rules_outside_their_dates_or_for_another_company_are_ignored():
    rules = [tax_rule("Expired", to_date="2026-01-01"), tax_rule("Future", from_date="2027-01-01"), tax_rule("Current", from_date="2026-01-01", to_date="2026-12-31")]
    assert setup_for(rules)[0].template == "Current"
    assert setup_for([tax_rule("X only", company="X")], company="Y")[0].template is None
    assert setup_for([tax_rule("Y only", company="Y")], company="Y")[0].template == "Y only"
    got, _ = setup_for([tax_rule("X only", company="X")])                                              # no company known: cannot say, and says so
    assert got.template is None and any("for company X, but no company is set for this agent" in n for n in got.notes)


def test_rules_that_depend_on_addresses_or_items_are_skipped_and_reported_not_guessed():
    got, _ = setup_for([tax_rule("Vietnam address", billing_country="Vietnam"), tax_rule("Steel only", item_group="Steel"), tax_rule("Plain")])
    assert got.template == "Plain"
    assert "Vietnam address (depends on addresses or items)" in got.notes[0] and "Steel only" in got.notes[0]


def test_equal_rules_pointing_at_different_templates_are_called_out():
    got, _ = setup_for([tax_rule("Alpha", name="TR-1"), tax_rule("Beta", name="TR-2")])
    assert got.template == "Beta" and any("several Tax Rules tie" in n and "Alpha, Beta" in n for n in got.notes)
    assert not any("tie" in n for n in setup_for([tax_rule("Same", name="TR-1"), tax_rule("Same", name="TR-2")])[0].notes)


def test_no_tax_rule_match_falls_back_to_the_default_template_like_erpnext_does():
    defaults = [{"name": "India GST - ABC", "company": "ABC Corp", "is_default": 1, "disabled": 0}, {"name": "Other", "company": "ABC Corp", "is_default": 0, "disabled": 0}]
    got, _ = setup_for([], company="ABC Corp", defaults=defaults)
    assert (got.template, got.source) == ("India GST - ABC", "default template") and got.rows[0].description == "Basic customs duty"
    assert "no Tax Rule matched this vendor" in got.notes


def test_nothing_set_up_is_reported_plainly_and_nothing_is_invented():
    got, _ = setup_for([])
    assert (got.template, got.source, got.rows) == (None, "none", ())
    assert got.notes == ("no Tax Rule matched this vendor", "there is no default purchase taxes template")


def test_several_companies_defaults_without_a_company_are_not_guessed():
    got, _ = setup_for([], defaults=[{"name": "A", "company": "X", "is_default": 1, "disabled": 0}, {"name": "B", "company": "Y", "is_default": 1, "disabled": 0}])
    assert got.template is None and any("several companies have a default purchase taxes template" in n for n in got.notes)


def test_when_the_tax_rules_cannot_be_read_that_is_said_and_the_default_is_still_tried():
    s = Session({"Supplier": SUPPLIER, "Tax Rule": 403, "Purchase Taxes and Charges Template": [{"name": "Dflt", "company": "X", "is_default": 1, "disabled": 0}]},
                docs_for("Dflt"))
    got = erp(s).get_purchase_taxes("S-A", "BOLT", TODAY)
    assert got.template == "Dflt" and got.source == "default template" and "could not read the ERP's Tax Rules" in got.notes[0]


def test_an_item_tax_template_adjusts_the_rates_as_in_erpnext():
    docs = docs_for("Import - ABC")
    docs[("Item", "BOLT")] = {"taxes": [{"item_tax_template": "Wrong category", "tax_category": "Domestic"}, {"item_tax_template": "Reduced GST", "tax_category": "Import", "valid_from": "2020-01-01"},
                                        {"item_tax_template": "Not yet valid", "tax_category": "Import", "valid_from": "2099-01-01"}]}
    docs[("Item Tax Template", "Reduced GST")] = {"taxes": [{"tax_type": "IGST - ABC", "tax_rate": 5}, {"tax_type": "Duty - ABC", "not_applicable": 1}]}
    got = erp(Session({"Supplier": SUPPLIER, "Tax Rule": [tax_rule("Import - ABC")]}, docs)).get_purchase_taxes("S-A", "BOLT", TODAY)
    assert got.item_template == "Reduced GST"
    assert [(r.description, r.rate, r.rate_from_item_template) for r in got.rows] == [("Basic customs duty", 0.0, True), ("IGST", 5.0, True)]
    assert taxes.compute(got, 10, 100, False).lines[1].description.endswith("(rate from the item's tax template)")


def test_unreadable_pieces_become_notes_not_failures():
    docs = {("Item", "BOLT"): {"taxes": [{"item_tax_template": "Missing doc", "tax_category": "Import"}]},
            ("Purchase Taxes and Charges Template", "T"): {"taxes": [TPL_ROWS[0] | {"included_in_print_rate": 1}]}}
    got = erp(Session({"Supplier": SUPPLIER, "Tax Rule": [tax_rule("T")]}, docs)).get_purchase_taxes("S-A", "BOLT", TODAY)
    assert got.template == "T" and len(got.rows) == 1
    assert any("included in the price" in n for n in got.notes) and any("could not read the item's tax template" in n for n in got.notes)
    gone = erp(Session({"Supplier": SUPPLIER, "Tax Rule": [tax_rule("Deleted")]}, {})).get_purchase_taxes("S-A", "BOLT", TODAY)
    assert gone.template == "Deleted" and gone.rows == () and "could not read template 'Deleted'" in gone.notes[0]
    no_supplier = erp(Session({"Supplier": 403, "Tax Rule": []}, {})).get_purchase_taxes("S-A", "BOLT", TODAY)
    assert "could not read the supplier's tax category" in no_supplier.notes


def test_template_names_with_slashes_and_spaces_are_quoted_in_the_url():
    seen = []

    class Q(Session):
        def get(self, url, params=None, timeout=None):
            seen.append(url)
            return super().get(url, params, timeout)

    q = Q({"Supplier": SUPPLIER, "Tax Rule": [tax_rule("GST 18% / Import")]}, {("Purchase Taxes and Charges Template", "GST 18% / Import"): {"taxes": []}, ("Item", "BOLT"): {}})
    erp(q).get_purchase_taxes("S-A", "BOLT", TODAY)
    assert any(u.endswith("/Purchase Taxes and Charges Template/GST%2018%25%20%2F%20Import") for u in seen)


def test_erpnext_purchase_order_carries_the_template():
    s = Session()
    c = erp(s)
    c.create_purchase_order(PurchaseOrderDraft("S", "A", 1, 1, "Nos", date(2026, 10, 25), taxes_template="Import - ABC"))
    c.create_purchase_order(PurchaseOrderDraft("S", "A", 1, 1, "Nos", date(2026, 10, 25)))
    assert s.posts[0][1]["taxes_and_charges"] == "Import - ABC" and "taxes_and_charges" not in s.posts[1][1]


def test_pressing_create_order_again_after_a_failed_attempt_never_makes_a_second_order(world):
    app, erp = world
    rec = run(app)
    erp.fail_purchase_orders = True
    first = app.approvals.decide(rec.id, "approve", APPROVER, "app")
    assert first.status != "decided" and not erp.purchase_orders           # refused: the decision is rolled back and stays pending
    erp.fail_purchase_orders = False
    assert app.approvals.decide(rec.id, "approve", APPROVER, "app").status == "decided"
    again = app.approvals.decide(rec.id, "approve", APPROVER, "app")
    assert again.status == "already_decided" and len(erp.purchase_orders) == 1
    assert erp.purchase_orders[0]["reference"] == app.proposals.list()[0].id          # the order carries the proposal id

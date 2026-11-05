"""Supplier quotations in the vendor comparison: the owner's choice (off / prefer / only), what counts as a valid quotation, and that the source of every price is said."""
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import OWNER
from erp_agents.core.api.app import create_app
from ai_munimji_engine.packs.stock_procurement.settings import ProcurementSettings
from ai_munimji_engine.core.workorders import REORDER_NEEDED
from ai_munimji_engine.packs.stock_procurement.erp import QuoteRule, VendorOffer
from erp_agents.core.erp.erpnext import ERPNextClient
from erp_agents.core.erp.mock import MockERP
from ai_munimji_engine.packs.stock_procurement.procurement.scoring import Need, rank_vendors

TODAY = date(2026, 10, 16)
NEED = Need(qty=100, cover_days=30, fallback_lead_days=7, uom="kg")
OFF, PREFER, ONLY = ProcurementSettings(), ProcurementSettings(use_quotations="prefer"), ProcurementSettings(use_quotations="only")


def offer(code, price, lead=5, on_time=0.9, credit=30, orders=10, **quote):
    q = dict(quote_price=None, quote_ref=None, quote_date=None, quote_valid_till=None, quote_qty=None, quote_lead_days=None)
    q.update(quote)
    return VendorOffer(code, code, price, price, lead, on_time, orders, TODAY - timedelta(days=15), credit, False, **q)


def quoted(code, price, ref, **kw):
    return dict(quote_price=price, quote_ref=ref, quote_date=TODAY - timedelta(days=2), quote_valid_till=TODAY + timedelta(days=9), quote_qty=1000.0, **kw)


def by(r):
    return {v.supplier: v for v in r.vendors}


# ------------------------------------------------------------------ the three choices
def test_off_ignores_quotations_completely():
    offers = [offer("A", 10, **quoted("A", 6, "SQ-1")), offer("B", 9)]
    r = rank_vendors(offers, NEED, OFF)
    assert r.best.supplier == "B" and by(r)["A"].unit_price == 10 and by(r)["A"].price_source == "last order" and by(r)["A"].quote_ref is None
    assert not r.notes


def test_prefer_uses_a_valid_quotation_instead_of_the_last_order_price_and_says_so():
    offers = [offer("A", 10, **quoted("A", 6, "SQ-1")), offer("B", 9)]
    r = rank_vendors(offers, NEED, PREFER)
    a, b = by(r)["A"], by(r)["B"]
    assert r.best.supplier == "A" and a.unit_price == 6 and a.total == 600 and a.price_source == "quotation" and a.quote_ref == "SQ-1"
    assert a.quote_valid_till == (TODAY + timedelta(days=9)).isoformat()
    assert b.unit_price == 9 and b.price_source == "last order"                        # no quotation: its last order price, as before
    assert not any("none of these vendors has a valid quotation" in n for n in r.notes)


def test_prefer_lets_a_vendor_that_has_quoted_but_was_never_ordered_from_be_compared():
    new = VendorOffer("N", "Newco", None, None, None, None, 0, None, 0, False, **quoted("N", 7, "SQ-N", quote_lead_days=6.0))
    off = by(rank_vendors([offer("A", 10), new], NEED, OFF))["N"]
    assert not off.eligible and off.excluded_reason == "no price on record: the ERP has no past order for this item from them"      # off: its quotation is not looked at
    r = rank_vendors([offer("A", 10), new], NEED, PREFER)
    n = by(r)["N"]
    assert n.eligible and n.unit_price == 7 and n.price_source == "quotation" and n.orders == 0
    assert n.lead_days == 6.0 and n.lead_from_quote and not n.lead_assumed
    assert any("using the 6 days they quoted" in f for f in n.flags)
    assert r.best.supplier == "A"                                      # cheaper, but a vendor with no track record and no credit does not win on price alone
    cost_first = ProcurementSettings(use_quotations="prefer", weight_cost=100, weight_delivery=0, weight_reliability=0, weight_terms=0)
    assert rank_vendors([offer("A", 10), new], NEED, cost_first).best.supplier == "N"             # ... unless price is what the owner cares about


def test_a_new_vendor_is_still_left_out_if_the_owner_never_wants_new_vendors():
    new = VendorOffer("N", "Newco", None, None, None, None, 0, None, 0, False, **quoted("N", 7, "SQ-N"))
    r = rank_vendors([offer("A", 10), new], NEED, ProcurementSettings(use_quotations="prefer", exclude_new_vendors=True))
    n = by(r)["N"]
    assert not n.eligible and "no past orders" in n.excluded_reason and r.best.supplier == "A"


def test_only_compares_just_the_vendors_that_have_quoted():
    offers = [offer("A", 10, **quoted("A", 8, "SQ-1")), offer("B", 5), offer("C", 9, **quoted("C", 8.5, "SQ-3"))]
    r = rank_vendors(offers, NEED, ONLY)
    assert [v.supplier for v in r.eligible] == ["A", "C"] and r.best.supplier == "A"
    b = by(r)["B"]
    assert not b.eligible and "no valid supplier quotation for this item" in b.excluded_reason and b.why_not == b.excluded_reason
    assert by(r)["A"].price_source == "quotation" and by(r)["C"].price_source == "quotation"


def test_only_with_no_quotations_at_all_compares_nobody_and_says_why():
    r = rank_vendors([offer("A", 10), offer("B", 9)], NEED, ONLY)
    assert r.best is None and all("no valid supplier quotation" in v.excluded_reason for v in r.vendors)


def test_prefer_with_no_quotations_falls_back_to_past_orders_and_tells_the_owner():
    r = rank_vendors([offer("A", 10), offer("B", 9)], NEED, PREFER)
    assert r.best.supplier == "B" and all(v.price_source == "last order" for v in r.vendors)
    assert any("none of these vendors has a valid quotation for this item: the prices are from past orders" in n for n in r.notes)


# ------------------------------------------------------------------ honesty about what a quotation does and does not promise
def test_a_quotation_for_a_smaller_quantity_is_flagged():
    a = by(rank_vendors([offer("A", 10, **{**quoted("A", 6, "SQ-1"), "quote_qty": 40.0}), offer("B", 9)], NEED, PREFER))["A"]
    assert any("their quotation is for 40 kg and you need 100" in f for f in a.flags)


def test_a_quotation_far_above_their_usual_price_is_flagged_as_a_quotation():
    o = VendorOffer("A", "A", 10, 10, 5, 0.9, 10, TODAY, 30, False, **quoted("A", 14, "SQ-1"))
    a = by(rank_vendors([o, offer("B", 9)], NEED, PREFER))["A"]
    assert any(f.startswith("their quotation is 40% above their average of 10") for f in a.flags)


def test_observed_delivery_history_beats_the_quoted_lead_time():
    a = by(rank_vendors([offer("A", 10, lead=3, **quoted("A", 8, "SQ-1", quote_lead_days=9.0)), offer("B", 9)], NEED, PREFER))["A"]
    assert a.lead_days == 3 and not a.lead_from_quote and not any("they quoted" in f for f in a.flags)


# ------------------------------------------------------------------ the setting
def test_the_setting_has_safe_defaults_and_rejects_nonsense():
    s = ProcurementSettings()
    assert s.use_quotations == "off" and s.quotation_max_age_days == 30
    for bad in ({"use_quotations": "always"}, {"quotation_max_age_days": 0}, {"quotation_max_age_days": 400}):
        with pytest.raises(ValueError):
            ProcurementSettings(**bad)


# ------------------------------------------------------------------ the demo ERP behaves like the real one
def test_the_demo_erp_hides_quotations_until_they_are_asked_for_and_ignores_lapsed_ones():
    erp = MockERP(today=TODAY)
    plain = erp.get_vendor_offers("BOLT-M8", TODAY - timedelta(days=365))
    assert [o.supplier for o in plain] == ["SUP-PRIME", "SUP-BUDGET", "SUP-SWIFT"] and all(o.quote_price is None for o in plain)       # the quote-only vendor is not listed
    asked = {o.supplier: o for o in erp.get_vendor_offers("BOLT-M8", TODAY - timedelta(days=365), QuoteRule(TODAY, 30))}
    assert asked["SUP-NEWCO"].quote_price and asked["SUP-PRIME"].quote_price and asked["SUP-BUDGET"].quote_price is None
    later = {o.supplier for o in erp.get_vendor_offers("BOLT-M8", TODAY - timedelta(days=365), QuoteRule(TODAY + timedelta(days=30), 30))}
    assert "SUP-NEWCO" not in later                                                      # both quotations ended


# ------------------------------------------------------------------ the real ERPNext client
class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, json.dumps(data, default=str)

    def json(self):
        return self._d


def erp_client(tables):
    from test_erpnext_client import VENDOR_TABLES, FakeSession
    return ERPNextClient("http://erp.local/", "K", "S", session=FakeSession({**VENDOR_TABLES, **tables}))


def sq(name, supplier, made, until=None, status="Submitted", rate=8.0, base=None, conv=1.0, ccy="INR", supplier_name=None):
    return {"name": name, "supplier": supplier, "supplier_name": supplier_name or supplier, "transaction_date": made, "valid_till": until, "currency": ccy, "conversion_rate": conv, "status": status}, \
        {"parent": name, "qty": 500, "rate": rate, "base_rate": base if base is not None else rate * conv, "lead_time_days": 6}


def tables_for(*quotes):
    return {"Supplier Quotation": [h for h, _ in quotes], "Supplier Quotation Item": [l for _, l in quotes]}


RULE = QuoteRule(TODAY, 30)
SINCE = date(2026, 1, 1)


def test_valid_quotations_are_read_in_company_currency_and_a_quote_only_supplier_appears():
    c = erp_client(tables_for(sq("SQ-1", "S-A", "2026-10-10", "2026-10-30"), sq("SQ-9", "S-NEW", "2026-10-12", "2026-11-01", rate=0.1, base=9.3, ccy="USD", conv=93.0, supplier_name="Newco")))
    offers = {o.supplier: o for o in c.get_vendor_offers("BOLT", SINCE, RULE)}
    a, new = offers["S-A"], offers["S-NEW"]
    assert (a.quote_price, a.quote_ref, a.quote_valid_till, a.quote_qty, a.quote_lead_days) == (8.0, "SQ-1", date(2026, 10, 30), 500.0, 6.0) and a.unit_price == 12     # the past order is untouched
    assert new.quote_price == 9.3 and new.unit_price is None and new.orders == 0 and new.currency == "USD"            # base rate (INR), not the 0.10 dollars
    assert new.name in ("Newco", "S-NEW")


@pytest.mark.parametrize("label,quote", [
    ("expired status", sq("SQ-X", "S-A", "2026-10-01", "2026-12-01", status="Expired")),
    ("stopped", sq("SQ-X", "S-A", "2026-10-01", "2026-12-01", status="Stopped")),
    ("cancelled", sq("SQ-X", "S-A", "2026-10-01", "2026-12-01", status="Cancelled")),
    ("past its end date", sq("SQ-X", "S-A", "2026-09-01", "2026-10-15")),
    ("no end date and older than the limit", sq("SQ-X", "S-A", "2026-09-01", None)),
    ("no price", sq("SQ-X", "S-A", "2026-10-10", "2026-11-01", rate=0.0, base=0.0)),
])
def test_quotations_that_do_not_count(label, quote):
    offers = {o.supplier: o for o in erp_client(tables_for(quote)).get_vendor_offers("BOLT", SINCE, RULE)}
    assert offers["S-A"].quote_price is None, label


def test_a_quotation_with_no_end_date_counts_for_the_owners_number_of_days():
    q = sq("SQ-1", "S-A", "2026-10-01", None)
    assert {o.supplier: o for o in erp_client(tables_for(q)).get_vendor_offers("BOLT", SINCE, QuoteRule(TODAY, 30))}["S-A"].quote_price == 8.0
    assert {o.supplier: o for o in erp_client(tables_for(q)).get_vendor_offers("BOLT", SINCE, QuoteRule(TODAY, 10))}["S-A"].quote_price is None


def test_the_newest_valid_quotation_of_a_supplier_wins_and_the_lowest_line_within_one():
    older, newer = sq("SQ-1", "S-A", "2026-10-05", "2026-11-01", rate=7.0), sq("SQ-2", "S-A", "2026-10-12", "2026-11-01", rate=8.5)
    a = {o.supplier: o for o in erp_client(tables_for(older, newer)).get_vendor_offers("BOLT", SINCE, RULE)}["S-A"]
    assert (a.quote_ref, a.quote_price) == ("SQ-2", 8.5)                                       # newer, even though dearer
    head, line1 = sq("SQ-3", "S-A", "2026-10-12", "2026-11-01", rate=9.0)
    tiered = erp_client({"Supplier Quotation": [head], "Supplier Quotation Item": [line1, {**line1, "qty": 5000, "rate": 7.5, "base_rate": 7.5}]})
    assert {o.supplier: o for o in tiered.get_vendor_offers("BOLT", SINCE, RULE)}["S-A"].quote_price == 7.5


def test_quotations_are_not_even_requested_when_the_owner_did_not_ask():
    c = erp_client(tables_for(sq("SQ-1", "S-A", "2026-10-10", "2026-10-30")))
    offers = {o.supplier: o for o in c.get_vendor_offers("BOLT", SINCE)}
    assert all(o.quote_price is None for o in offers.values())
    assert not any("Supplier Quotation" in call[0] for call in c.session.calls)


def test_unreadable_quotations_mean_no_quotations_and_nothing_breaks(caplog):
    import logging
    c = erp_client({"Supplier Quotation Item": 403})
    with caplog.at_level(logging.WARNING):
        offers = {o.supplier: o for o in c.get_vendor_offers("BOLT", SINCE, RULE)}
    assert offers["S-A"].unit_price == 12 and offers["S-A"].quote_price is None
    assert "Could not read the supplier quotations" in " ".join(r.getMessage() for r in caplog.records)


def test_an_older_erpnext_without_a_quoted_lead_time_field_is_still_read():
    class NoLead(type(erp_client({}).session)):
        def get(self, url, params=None, timeout=None):
            if url.endswith("Supplier Quotation Item") and "lead_time_days" in json.loads(params["fields"]):
                return Resp({"exception": "Unknown column lead_time_days"}, 417)
            return super().get(url, params, timeout)
    from test_erpnext_client import VENDOR_TABLES
    h, line = sq("SQ-1", "S-A", "2026-10-10", "2026-10-30")
    s = NoLead({**VENDOR_TABLES, "Supplier Quotation": [h], "Supplier Quotation Item": [{k: v for k, v in line.items() if k != "lead_time_days"}]})
    a = {o.supplier: o for o in ERPNextClient("http://erp.local/", "K", "S", session=s).get_vendor_offers("BOLT", SINCE, RULE)}["S-A"]
    assert a.quote_price == 8.0 and a.quote_lead_days is None


# ------------------------------------------------------------------ end to end through the agent
def world(make_app, clock, **settings):
    erp = MockERP(today=clock().date())
    app = make_app(erp=erp)
    if settings:
        app.settings.update_procurement(OWNER, settings, [])
    return app, erp


def wo(code="BOLT-M8", qty=500):
    return {"item_code": code, "item_name": "Bolt M8", "item_group": "Fasteners", "uom": "pcs", "suggested_qty": qty, "cover_days": 10.0, "lead_time_days": 5.0, "daily_rate": 20.0,
            "on_hand": 200.0, "available": 200.0, "severity": "HIGH", "reasons": ["Low."], "stockout_date": "2026-10-26"}


def propose(app):
    app.workorders.emit(REORDER_NEEDED, wo())
    rep = app.procurement.run()
    assert rep.proposed == 1, rep
    return app.approvals.list("pending")[0]


def test_off_changes_nothing_in_the_proposal(make_app, clock):
    app, _ = world(make_app, clock)
    req = propose(app).request
    text = json.dumps(req.model_dump(mode="json"))
    assert "supplier quotation" not in text.lower() and "Newco" not in text


def test_prefer_puts_the_quotation_and_its_source_in_the_request_and_the_order(make_app, clock):
    app, erp = world(make_app, clock, use_quotations="prefer")
    rec = propose(app)
    req = rec.request
    assert "Prime Supplies" in req.title                                                                   # its quotation is cheaper than its last order, and it is fast and reliable
    text = json.dumps(req.model_dump(mode="json"))
    assert "supplier quotation SQ-DEMO-BOLT-M8-1" in text and "Prices: a vendor's valid supplier quotation is used" in text
    assert "Newco Trading" in text and "supplier quotation SQ-DEMO-BOLT-M8-2" in text                         # the vendor that only quoted is compared too (Other vendors / Details)
    assert req.facts["unit_price"] == pytest.approx(round(round(MockERP(today=clock().date())._rng("BOLT-M8-price").uniform(2, 40), 2) * 0.94, 2))
    assert app.approvals.decide(rec.id, "approve", OWNER, "app").ok
    po = erp.purchase_orders[0]
    assert po["supplier"] == "SUP-PRIME" and "supplier quotation SQ-DEMO-BOLT-M8-1" in po["remarks"]


def test_only_with_nobody_quoting_asks_the_owner_instead_of_guessing(make_app, clock):
    app, erp = world(make_app, clock, use_quotations="only")
    app.workorders.emit(REORDER_NEEDED, wo("OIL-5L", 100))
    erp.vendors["OIL-5L"] = [o for o in erp.vendors["OIL-5L"] if o.supplier not in ("SUP-NEWCO",)]
    erp.vendors["OIL-5L"] = [VendorOffer(o.supplier, o.name, o.unit_price, o.avg_price, o.lead_time_days, o.on_time_rate, o.orders, o.last_order, o.payment_days, o.listed) for o in erp.vendors["OIL-5L"]]
    rep = app.procurement.run()
    assert rep.manual == 1
    req = app.approvals.list("pending")[0].request
    assert "compare only vendors that have a valid supplier quotation" in json.dumps(req.model_dump(mode="json")) and "Request for Quotation" in json.dumps(req.model_dump(mode="json"))


def test_the_requisition_note_carries_the_quotation_for_the_buyer(make_app, clock):
    app, erp = world(make_app, clock, use_quotations="prefer", requisition_first_items=["BOLT-M8"])
    rec = propose(app)
    assert rec.kind == "procurement.pr"
    assert app.approvals.decide(rec.id, "approve", OWNER, "app").ok
    assert "The price is from supplier quotation SQ-DEMO-BOLT-M8-1" in erp.requisitions[0]["note"]


def test_the_preview_and_the_chat_follow_the_setting(make_app, clock):
    app, _ = world(make_app, clock)
    off = app.procurement.preview("BOLT-M8", 500, 10.0, 5.0, "pcs")
    assert all(v["price_source"] != "quotation" for v in off["vendors"]) and len(off["vendors"]) == 3
    app.settings.update_procurement(OWNER, {"use_quotations": "prefer"}, [])
    on = app.procurement.preview("BOLT-M8", 500, 10.0, 5.0, "pcs")
    assert {v["name"] for v in on["vendors"] if v["price_source"] == "quotation"} == {"Newco Trading", "Prime Supplies"} and len(on["vendors"]) == 4
    answer = app.chat.ask("compare vendors for BOLT-M8 for 500", "owner", True)
    assert "from supplier quotation SQ-DEMO-BOLT-M8-1" in answer["answer"] and any("(quotation)" in json.dumps(b) for b in answer["blocks"])
    assert "Newco Trading" in answer["answer"] and "supplier quotation SQ-DEMO-BOLT-M8-2" in json.dumps(answer)      # compared too; its quotation is in the numbered sources


def test_the_choice_is_available_through_the_settings_api_and_validated(make_app, clock):
    app = make_app(cfg={"auth": {"tokens": [{"token": "o" * 40, "user": "owner", "roles": ["viewer", "approver", "admin"]}]}})
    c = TestClient(create_app(app))
    h = {"Authorization": "Bearer " + "o" * 40}
    assert c.get("/api/v1/settings/procurement", headers=h).json()["effective"]["use_quotations"] == "off"
    assert c.put("/api/v1/settings/procurement", json={"set": {"use_quotations": "prefer", "quotation_max_age_days": 45}}, headers=h).status_code == 200
    got = c.get("/api/v1/settings/procurement", headers=h).json()["effective"]
    assert got["use_quotations"] == "prefer" and got["quotation_max_age_days"] == 45
    assert c.put("/api/v1/settings/procurement", json={"set": {"use_quotations": "sometimes"}}, headers=h).status_code == 422
    assert [a for a in c.get("/api/v1/settings/audit", headers=h).json() if a["field"] == "use_quotations"]


def test_the_service_account_check_asks_for_permission_to_read_quotations():
    from conftest import STOCK_NEEDS as NEEDS                 # what the stock pack declares in its manifest
    assert ("Supplier Quotation", "read") in NEEDS


# ------------------------------------------------------------------ the draft order links the quotation it was priced from
class PostSession:
    """A fake ERPNext session that serves single documents and records what is posted."""

    def __init__(self, docs):
        self.docs, self.posts, self.headers = docs, [], {}

    def get(self, url, params=None, timeout=None):
        from urllib.parse import unquote
        key = unquote(url.rsplit("/api/resource/", 1)[1])
        return Resp({"data": self.docs[key]}) if key in self.docs else Resp({"exception": "not found"}, 404)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return Resp({"data": {"name": "PO-1"}})


def draft_with(quotation="SQ-1"):
    from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft
    return PurchaseOrderDraft("S-A", "BOLT", 500, 9.30, "Nos", date(2026, 10, 30), supplier_quotation=quotation)


def post_order(docs, draft=None):
    s = PostSession({"Company/Demo Co": {"default_currency": "INR"}, "Supplier/S-A": {"default_currency": "INR"}, **docs})
    ERPNextClient("http://erp.local/", "K", "S", session=s, company="Demo Co").create_purchase_order(draft or draft_with())
    return s.posts[0][1]["items"][0]


GOOD_SQ = {"docstatus": 1, "supplier": "S-A", "currency": "INR", "items": [{"name": "row-1", "item_code": "BOLT", "rate": 8.75, "base_rate": 8.75}, {"name": "row-2", "item_code": "BOLT", "rate": 9.5, "base_rate": 9.5}]}


def test_the_order_item_is_linked_to_its_quotation_at_the_quoted_price():
    item = post_order({"Supplier Quotation/SQ-1": GOOD_SQ})
    assert item["supplier_quotation"] == "SQ-1" and item["supplier_quotation_item"] == "row-1" and item["rate"] == 8.75       # the lowest line, exactly as quoted


@pytest.mark.parametrize("label,sq", [
    ("another supplier", {**GOOD_SQ, "supplier": "S-B"}),
    ("another currency", {**GOOD_SQ, "currency": "USD"}),
    ("not submitted", {**GOOD_SQ, "docstatus": 0}),
    ("no line for the item", {**GOOD_SQ, "items": [{"name": "r", "item_code": "NUT", "rate": 1, "base_rate": 1}]}),
])
def test_a_quotation_ERPNext_would_refuse_is_not_linked_and_the_order_is_still_created(label, sq):
    item = post_order({"Supplier Quotation/SQ-1": sq})
    assert "supplier_quotation" not in item and item["rate"] == 9.30, label


def test_an_unreadable_quotation_does_not_stop_the_order():
    item = post_order({})
    assert "supplier_quotation" not in item and item["rate"] == 9.30


def test_no_quotation_means_no_link_at_all():
    from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft
    item = post_order({"Supplier Quotation/SQ-1": GOOD_SQ}, PurchaseOrderDraft("S-A", "BOLT", 500, 9.30, "Nos", date(2026, 10, 30)))
    assert "supplier_quotation" not in item and item["rate"] == 9.30


def test_the_vendor_note_names_the_quotation_the_price_came_from(make_app, clock):
    app, erp = world(make_app, clock, use_quotations="prefer")
    rec = propose(app)
    assert "as per your quotation SQ-DEMO-BOLT-M8-1" in json.dumps(rec.request.model_dump(mode="json"))          # the owner sees the note before approving
    assert app.approvals.decide(rec.id, "approve", OWNER, "app").ok
    po = erp.purchase_orders[0]
    assert po["supplier_quotation"] == "SQ-DEMO-BOLT-M8-1" and "as per your quotation SQ-DEMO-BOLT-M8-1" in po["vendor_note"]

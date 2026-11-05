"""ERPNext adapter against a fake HTTP session (NOT a live ERPNext - see module docstring of the adapter)."""
import json
from datetime import date

import pytest

from erp_agents.core.erp import erpnext
from erp_agents.core.erp.erpnext import ERPNextClient, ERPNextError


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._d


class FakeSession:
    def __init__(self, tables):
        self.tables, self.headers, self.calls = tables, {}, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if url.endswith("get_logged_user"):
            return Resp({"message": "agent@x.com"})
        doctype = url.rsplit("/api/resource/", 1)[1]
        if "/" in doctype:                                   # a single document, e.g. Company/Demo Co
            return Resp({"data": self.tables.get(__import__("urllib.parse").parse.unquote(doctype), {})})
        rows = self.tables.get(doctype, []) if doctype in ("Company", "Currency Exchange") else self.tables[doctype]
        if isinstance(rows, int):
            return Resp({"exception": "nope"}, rows)
        start, size = int(params["limit_start"]), int(params["limit_page_length"])
        return Resp({"data": rows[start:start + size]})


def client(tables, **kw):
    return ERPNextClient("http://erp.local/", "KEY", "SECRET", session=FakeSession(tables), **kw)


def test_auth_header_and_healthcheck():
    c = client({})
    assert c.session.headers["Authorization"] == "token KEY:SECRET"
    assert c.healthcheck() is True


def test_items_mapped_with_defaults():
    c = client({"Item": [
        {"name": "BOLT", "item_name": "Bolt M8", "item_group": "Fasteners", "stock_uom": "Nos", "lead_time_days": 5, "creation": "2025-01-02 10:11:12.123"},
        {"name": "NOLEAD", "item_name": None, "item_group": None, "stock_uom": None, "lead_time_days": 0, "creation": None},
    ]})
    a, b = c.list_items()
    assert (a.code, a.name, a.group, a.uom, a.lead_time_days, a.created) == ("BOLT", "Bolt M8", "Fasteners", "Nos", 5.0, date(2025, 1, 2))
    assert (b.name, b.uom, b.lead_time_days, b.created) == ("NOLEAD", "units", None, None)
    filters = json.loads(c.session.calls[0][1]["filters"])
    assert ["is_stock_item", "=", 1] in filters and ["disabled", "=", 0] in filters


def test_stock_sums_warehouses_and_missing_items_have_zero():
    c = client({"Bin": [
        {"item_code": "A", "actual_qty": 10, "reserved_qty": 2, "ordered_qty": 100},
        {"item_code": "A", "actual_qty": 5, "reserved_qty": 0, "ordered_qty": 0},
    ], "Purchase Order Item": [], "Purchase Order": []}, warehouses=["Stores - X"])
    out = c.get_stock(["A", "B"])
    assert (out["A"].on_hand, out["A"].reserved, out["A"].incoming) == (15, 2, 100)
    assert out["B"].on_hand == 0
    assert ["warehouse", "in", ["Stores - X"]] in json.loads(c.session.calls[0][1]["filters"])


def test_stock_is_also_reported_warehouse_by_warehouse():
    c = client({"Bin": [
        {"item_code": "A", "warehouse": "Shop - X", "actual_qty": 40, "reserved_qty": 0, "ordered_qty": 30},
        {"item_code": "A", "warehouse": "Stores - X", "actual_qty": 100, "reserved_qty": 60, "ordered_qty": 0},
        {"item_code": "A", "warehouse": "Old - X", "actual_qty": 0, "reserved_qty": 0, "ordered_qty": 0},          # a Bin row stays after the last movement
        {"item_code": "A", "warehouse": "Transit - X", "actual_qty": 7, "reserved_qty": 0, "ordered_qty": 0},
    ], "Warehouse": [{"name": "Transit - X", "warehouse_type": "Transit", "is_group": 0, "disabled": 0}], "Purchase Order Item": [], "Purchase Order": []})
    out = c.get_stock(["A", "B"])
    assert [(w.warehouse, w.on_hand, w.reserved, w.incoming, w.in_transit) for w in out["A"].by_warehouse] == [
        ("Stores - X", 100, 60, 0, False), ("Shop - X", 40, 0, 30, False), ("Transit - X", 7, 0, 0, True)]
    assert (out["A"].on_hand, out["A"].in_transit, out["A"].reserved, out["A"].incoming) == (140, 7, 60, 30)       # the totals are what they were
    assert out["B"].by_warehouse == ()


def test_reserved_includes_production_and_subcontract_and_planned_is_read():
    c = client({"Bin": [
        {"item_code": "A", "actual_qty": 100, "reserved_qty": 10, "reserved_qty_for_production": 5, "reserved_qty_for_sub_contract": 2,
         "ordered_qty": 0, "planned_qty": 30},
        {"item_code": "A", "actual_qty": 20, "reserved_qty": 1, "reserved_qty_for_production": None, "reserved_qty_for_sub_contract": 0,
         "ordered_qty": 0, "planned_qty": 20},
    ]})
    s = c.get_stock(["A"])["A"]
    # every column is kept apart so the owner can choose which to add or subtract
    assert (s.on_hand, s.reserved, s.reserved_production, s.reserved_subcontract, s.planned, s.incoming) == (120, 11, 5, 2, 50, 0)
    assert "reserved_qty_for_production" in json.loads(c.session.calls[0][1]["fields"])


def test_every_stock_column_and_the_erps_projected_qty_are_read():
    c = client({"Bin": [{"item_code": "A", "actual_qty": 50, "reserved_qty": 1, "reserved_qty_for_production": 2, "reserved_qty_for_sub_contract": 3,
                         "reserved_qty_for_production_plan": 4, "ordered_qty": 5, "indented_qty": 6, "planned_qty": 7, "projected_qty": 99}],
                    "Purchase Order Item": [], "Purchase Order": []})
    s = c.get_stock(["A"])["A"]
    assert (s.reserved, s.reserved_production, s.reserved_subcontract, s.reserved_production_plan) == (1, 2, 3, 4)
    assert (s.incoming, s.requested, s.planned, s.projected) == (5, 6, 7, 99)
    assert "A" in c.get_stock(["A", "B"]) and c.get_stock(["A", "B"])["B"].on_hand == 0 and c.get_stock(["A", "B"])["B"].projected is None


def test_warehouse_filter_is_passed_on_and_overrides_the_connection_default():
    c = client({"Bin": [], "Stock Ledger Entry": [], "Warehouse": [
        {"name": "Stores - X", "is_group": 0, "disabled": 0}, {"name": "All Warehouses - X", "is_group": 1, "disabled": 0}, {"name": "Scrap - X", "is_group": 0, "disabled": 0}]},
        warehouses=["Default - X"])
    c.get_stock(["A"])
    c.get_stock(["A"], ["Stores - X", "Scrap - X"])
    c.get_consumption(["A"], date(2026, 9, 1), date(2026, 10, 1), ["Stores - X"])
    seen = [json.loads(call[1]["filters"]) for call in c.session.calls if call[0].endswith(("/Bin", "/Stock Ledger Entry"))]       # not the lookup of transit warehouses
    assert ["warehouse", "in", ["Default - X"]] in seen[0]
    assert ["warehouse", "in", ["Stores - X", "Scrap - X"]] in seen[1]
    assert ["warehouse", "in", ["Stores - X"]] in seen[2]
    assert c.list_warehouses() == ["Scrap - X", "Stores - X"]                        # group warehouses are only folders


def test_stock_falls_back_to_basic_bin_fields_when_the_erp_rejects_the_extra_ones():
    class Picky(FakeSession):
        def get(self, url, params=None, timeout=None):
            if url.endswith("/Bin") and "planned_qty" in params["fields"]:
                return Resp({"exception": "Field not permitted in query: planned_qty"}, 417)
            return super().get(url, params, timeout)

    c = ERPNextClient("http://erp.local", "K", "S", session=Picky({"Bin": [{"item_code": "A", "actual_qty": 9, "reserved_qty": 3, "ordered_qty": 0}]}))
    s = c.get_stock(["A"])["A"]
    assert (s.on_hand, s.reserved, s.planned) == (9, 3, 0)


def test_next_arrival_is_earliest_open_po_line_still_to_receive():
    c = client({
        "Bin": [{"item_code": "A", "actual_qty": 5, "reserved_qty": 0, "ordered_qty": 80}, {"item_code": "B", "actual_qty": 9, "reserved_qty": 0, "ordered_qty": 0}],
        "Purchase Order Item": [
            {"parent": "PO-1", "item_code": "A", "qty": 50, "received_qty": 50, "schedule_date": "2026-10-10"},   # fully received: ignored
            {"parent": "PO-2", "item_code": "A", "qty": 30, "received_qty": 0, "schedule_date": "2026-10-25"},
            {"parent": "PO-3", "item_code": "A", "qty": 50, "received_qty": 10, "schedule_date": "2026-10-20"},
            {"parent": "PO-4", "item_code": "A", "qty": 20, "received_qty": 0, "schedule_date": "2026-10-18"},    # parent closed: ignored
            {"parent": "PO-5", "item_code": "A", "qty": 20, "received_qty": 0, "schedule_date": None},            # no date: ignored
        ],
        "Purchase Order": [{"name": "PO-2", "status": "To Receive and Bill"}, {"name": "PO-3", "status": "To Receive"}],
    })
    out = c.get_stock(["A", "B"])
    assert out["A"].incoming == 80 and out["A"].next_arrival == date(2026, 10, 20)
    assert out["B"].next_arrival is None
    po_calls = [call for call in c.session.calls if call[0].endswith("/Purchase Order Item")]
    assert len(po_calls) == 1 and po_calls[0][1]["parent"] == "Purchase Order"
    assert json.loads(po_calls[0][1]["filters"])[0] == ["item_code", "in", ["A"]]       # only items that are on order


def test_unreadable_purchase_orders_leave_arrival_unknown_not_an_error():
    c = client({"Bin": [{"item_code": "A", "actual_qty": 5, "reserved_qty": 0, "ordered_qty": 80}], "Purchase Order Item": 403})
    out = c.get_stock(["A"])
    assert out["A"].incoming == 80 and out["A"].next_arrival is None


VENDOR_TABLES = {
    "Item Supplier": [{"supplier": "S-LISTED"}, {"supplier": "S-A"}],
    "Purchase Order Item": [
        {"parent": "PO-1", "qty": 100, "rate": 10, "schedule_date": "2026-06-10"},
        {"parent": "PO-2", "qty": 100, "rate": 12, "schedule_date": "2026-09-10"},
        {"parent": "PO-3", "qty": 50, "rate": 9, "schedule_date": "2026-08-10"},
    ],
    "Purchase Order": [
        {"name": "PO-1", "supplier": "S-A", "supplier_name": "Alpha", "transaction_date": "2026-06-01", "currency": "INR"},
        {"name": "PO-2", "supplier": "S-A", "supplier_name": "Alpha", "transaction_date": "2026-09-01", "currency": "INR"},
        {"name": "PO-3", "supplier": "S-B", "supplier_name": "Beta", "transaction_date": "2026-08-01", "currency": "INR"},
    ],
    "Purchase Receipt Item": [{"parent": "PR-1", "purchase_order": "PO-1"}, {"parent": "PR-2", "purchase_order": "PO-2"}],
    "Purchase Receipt": [{"name": "PR-1", "posting_date": "2026-06-08"}, {"name": "PR-2", "posting_date": "2026-09-14"}],
    "Supplier": [
        {"name": "S-A", "supplier_name": "Alpha", "payment_terms": "Net 30", "disabled": 0},
        {"name": "S-B", "supplier_name": "Beta", "payment_terms": None, "disabled": 0},
        {"name": "S-LISTED", "supplier_name": "Listed Co", "payment_terms": None, "disabled": 0},
    ],
    "Payment Terms Template Detail": [{"parent": "Net 30", "credit_days": 30}],
}


def test_vendor_offers_from_item_suppliers_orders_and_receipts():
    c = client(VENDOR_TABLES)
    offers = {o.supplier: o for o in c.get_vendor_offers("BOLT", date(2026, 1, 1))}
    a, b, listed = offers["S-A"], offers["S-B"], offers["S-LISTED"]
    assert (a.name, a.orders, a.unit_price, a.avg_price, a.currency) == ("Alpha", 2, 12, 11, "INR")
    assert a.lead_time_days == 10                      # (8 days + 14 days) ... PO-1: 7, PO-2: 13 -> mean 10
    assert a.on_time_rate == 0.5 and a.payment_days == 30 and a.listed and a.last_order == date(2026, 9, 1)
    assert (b.orders, b.unit_price, b.lead_time_days, b.on_time_rate, b.payment_days, b.listed) == (1, 9, None, None, None, False)
    assert (listed.orders, listed.unit_price, listed.listed) == (0, None, True)
    for call in c.session.calls:                       # child tables are listed through their parent doctype
        if call[0].endswith(("Purchase Order Item", "Item Supplier", "Purchase Receipt Item")):
            assert call[1]["parent"] in ("Purchase Order", "Item", "Purchase Receipt")


def test_vendor_offers_survive_missing_permissions_on_optional_doctypes():
    tables = {**VENDOR_TABLES, "Purchase Receipt Item": 403, "Supplier": 403, "Item Supplier": 403}
    offers = {o.supplier: o for o in client(tables).get_vendor_offers("BOLT", date(2026, 1, 1))}
    assert offers["S-A"].unit_price == 12 and offers["S-A"].lead_time_days is None and offers["S-A"].payment_days is None
    assert offers["S-A"].name == "Alpha" and not offers["S-A"].listed                      # name falls back to the order's supplier_name


def test_disabled_suppliers_are_left_out():
    tables = {**VENDOR_TABLES, "Supplier": [{"name": "S-A", "supplier_name": "Alpha", "payment_terms": None, "disabled": 1},
                                            {"name": "S-B", "supplier_name": "Beta", "payment_terms": None, "disabled": 0}]}
    assert [o.supplier for o in client(tables).get_vendor_offers("BOLT", date(2026, 1, 1))] == ["S-B", "S-LISTED"]


class PostSession(FakeSession):
    def __init__(self, tables, response):
        super().__init__(tables)
        self.response, self.posts = response, []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        return self.response


def test_purchase_order_is_created_as_an_unsubmitted_draft():
    from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft
    session = PostSession({}, Resp({"data": {"name": "PO-00042"}}))
    c = ERPNextClient("http://erp.local/", "K", "S", session=session, company="Demo Co")
    res = c.create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 500, 10.5, "Nos", date(2026, 10, 25), "check me"))
    assert res.ok and res.name == "PO-00042"
    url, body = session.posts[0]
    assert url == "http://erp.local/api/resource/Purchase Order"
    assert body["supplier"] == "S-A" and body["company"] == "Demo Co" and body["schedule_date"] == "2026-10-25"
    assert body["items"] == [{"item_code": "BOLT", "qty": 500, "rate": 10.5, "uom": "Nos", "schedule_date": "2026-10-25"}]
    assert "docstatus" not in body                                                           # never submitted by the agent


def test_a_refusal_from_erpnext_is_reported_in_its_own_readable_words():
    from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft
    msg = "Item BOLT: Ordered qty 1 cannot be less than minimum order qty 2 (defined in Item)."
    body = {"exception": "frappe.exceptions.ValidationError: " + msg, "_server_messages": json.dumps([json.dumps({"message": msg})])}
    res = ERPNextClient("http://erp.local", "K", "S", session=PostSession({}, Resp(body, 417))).create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 1, 1, "Nos", date(2026, 10, 25)))
    assert not res.ok and res.error == f"ERPNext refused the purchase order (HTTP 417): {msg}"


def test_purchase_order_refusals_are_reported_not_raised():
    from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft
    draft = PurchaseOrderDraft("S-A", "BOLT", 5, 1, "Nos", date(2026, 10, 25))
    refused = ERPNextClient("http://erp.local", "K", "S", session=PostSession({}, Resp({"exc": "PermissionError"}, 403))).create_purchase_order(draft)
    assert not refused.ok and "HTTP 403" in refused.error
    nameless = ERPNextClient("http://erp.local", "K", "S", session=PostSession({}, Resp({"data": {}}))).create_purchase_order(draft)
    assert not nameless.ok


def test_consumption_aggregates_outgoing_rows_per_day():
    c = client({"Stock Ledger Entry": [
        {"item_code": "A", "posting_date": "2026-10-01", "actual_qty": -3},
        {"item_code": "A", "posting_date": "2026-10-01", "actual_qty": -2},
        {"item_code": "A", "posting_date": "2026-10-02", "actual_qty": -4},
        {"item_code": "B", "posting_date": "2026-10-02", "actual_qty": -1},
    ]})
    out = c.get_consumption(["A", "B", "C"], date(2026, 9, 1), date(2026, 10, 16))
    assert [(u.day.isoformat(), u.qty) for u in out["A"]] == [("2026-10-01", 5), ("2026-10-02", 4)]
    assert "C" not in out
    f = json.loads(c.session.calls[0][1]["filters"])
    assert ["actual_qty", "<", 0] in f and ["is_cancelled", "=", 0] in f and ["voucher_type", "in", ["Delivery Note", "Sales Invoice"]] in f


def test_voucher_types_are_configurable():
    c = client({"Stock Ledger Entry": []}, consumption_voucher_types=["Delivery Note", "Stock Entry"])
    c.get_consumption(["A"], date(2026, 1, 1), date(2026, 2, 1))
    assert ["voucher_type", "in", ["Delivery Note", "Stock Entry"]] in json.loads(c.session.calls[0][1]["filters"])


def test_pagination_collects_every_page(monkeypatch):
    monkeypatch.setattr(erpnext, "PAGE", 2)
    rows = [{"item_code": "A", "posting_date": f"2026-10-{d:02d}", "actual_qty": -1} for d in range(1, 6)]
    out = client({"Stock Ledger Entry": rows}).get_consumption(["A"], date(2026, 10, 1), date(2026, 10, 31))
    assert len(out["A"]) == 5


def test_http_errors_surface_clearly():
    with pytest.raises(ERPNextError, match="Bin: HTTP 403"):
        client({"Bin": 403}).get_stock(["A"])


# ------------------------------------------------------------------ vendors are compared in company currency
def test_vendor_prices_are_compared_in_company_currency_not_each_vendors_own():
    c = client({
        "Item Supplier": [], "Purchase Receipt Item": [], "Purchase Receipt": [], "Payment Terms Template Detail": [],
        "Purchase Order Item": [
            {"parent": "PO-IN", "qty": 100, "rate": 10.10, "base_rate": 10.10, "schedule_date": "2026-10-01"},
            {"parent": "PO-VN", "qty": 100, "rate": 0.096, "base_rate": 8.45, "schedule_date": "2026-10-01"},
            {"parent": "PO-OLD", "qty": 100, "rate": 0.10, "schedule_date": "2026-10-02"},                # no base_rate: rate x the order's conversion rate
        ],
        "Purchase Order": [
            {"name": "PO-IN", "supplier": "BENGALURU", "supplier_name": "Bengaluru Tooling", "transaction_date": "2026-09-01", "currency": "INR", "conversion_rate": 1},
            {"name": "PO-VN", "supplier": "SAIGON", "supplier_name": "Saigon Fastener", "transaction_date": "2026-09-02", "currency": "USD", "conversion_rate": 88.0},
            {"name": "PO-OLD", "supplier": "HANOI", "supplier_name": "Hanoi Parts", "transaction_date": "2026-09-03", "currency": "USD", "conversion_rate": 90.0},
        ],
        "Supplier": [],
    })
    offers = {o.supplier: o for o in c.get_vendor_offers("FAS-0002", date(2026, 1, 1))}
    assert offers["BENGALURU"].unit_price == 10.10
    assert offers["SAIGON"].unit_price == 8.45 and offers["SAIGON"].currency == "USD"          # not 0.096: that would look 100 times cheaper
    assert offers["HANOI"].unit_price == pytest.approx(9.0) and offers["HANOI"].avg_price == pytest.approx(9.0)
    assert min(offers.values(), key=lambda o: o.unit_price).supplier == "SAIGON"                # a genuine 16% saving, not an artefact


def test_the_order_for_a_foreign_supplier_is_raised_in_that_suppliers_currency():
    from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft
    tables = {"Company/Demo Co": {"default_currency": "INR"}, "Supplier/SAIGON": {"default_currency": "USD"},
              "Currency Exchange": [{"exchange_rate": 88.0, "date": "2026-10-16"}]}
    session = PostSession(tables, Resp({"data": {"name": "PO-7"}}))
    c = ERPNextClient("http://erp.local/", "K", "S", session=session, company="Demo Co")
    assert c.create_purchase_order(PurchaseOrderDraft("SAIGON", "FAS-0002", 1000, 8.8, "Nos", date(2026, 10, 25))).ok
    body = session.posts[0][1]
    assert body["currency"] == "USD" and body["conversion_rate"] == 88.0 and body["items"][0]["rate"] == 0.1      # 8.80 rupees = 0.10 dollars, never "8.80 dollars"
    local = PostSession({"Company/Demo Co": {"default_currency": "INR"}, "Supplier/LOCAL": {"default_currency": "INR"}}, Resp({"data": {"name": "PO-8"}}))
    ERPNextClient("http://erp.local/", "K", "S", session=local, company="Demo Co").create_purchase_order(PurchaseOrderDraft("LOCAL", "FAS-0002", 10, 10.1, "Nos", date(2026, 10, 25)))
    assert "currency" not in local.posts[0][1] and local.posts[0][1]["items"][0]["rate"] == 10.1
    nofx = PostSession({"Company/Demo Co": {"default_currency": "INR"}, "Supplier/SAIGON": {"default_currency": "USD"}, "Currency Exchange": []}, Resp({"data": {"name": "PO-9"}}))
    res = ERPNextClient("http://erp.local/", "K", "S", session=nofx, company="Demo Co").create_purchase_order(PurchaseOrderDraft("SAIGON", "FAS-0002", 1, 8.8, "Nos", date(2026, 10, 25)))
    assert not res.ok and "exchange rate" in res.error and nofx.posts == []                      # refuses rather than guessing


# ------------------------------------------------------------------ one approved proposal never creates two purchase orders
class OrderSession:
    """A small ERPNext: it stores the orders it receives and can lose the answer to a POST after storing it."""

    def __init__(self, field_exists=True, lose_first_answer=False):
        self.headers, self.orders, self.posts, self.field_exists, self.lose = {}, [], [], field_exists, lose_first_answer
        self.custom_fields = []

    def get(self, url, params=None, timeout=None):
        if "/Custom Field/" in url:
            return Resp({"data": {}}) if self.field_exists else Resp({"exception": "not found"}, 404)
        if url.endswith("/Company/Demo%20Co"):
            return Resp({"data": {"default_currency": "INR"}})
        if url.endswith("/Supplier/S-A"):
            return Resp({"data": {"default_currency": "INR"}})
        if url.endswith("/Purchase Order"):
            flt = json.loads(params["filters"])
            ref = next(f[2] for f in flt if f[0] == "custom_agent_ref")
            return Resp({"data": [{"name": o["name"]} for o in self.orders if o.get("custom_agent_ref") == ref]})
        raise AssertionError(url)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("/Custom Field"):
            self.custom_fields.append(json)
            return Resp({"data": {"name": "Purchase Order-custom_agent_ref"}})
        self.orders.append({**json, "name": f"PO-{len(self.orders) + 1:03d}"})
        if self.lose:
            self.lose = False
            import requests
            raise requests.ConnectionError("the answer was lost")
        return Resp({"data": {"name": self.orders[-1]["name"]}})


def po_client(session):
    return ERPNextClient("http://erp.local", "K", "S", session=session, company="Demo Co")


def test_a_retry_after_a_lost_answer_reuses_the_order_instead_of_creating_a_second():
    from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft
    s = OrderSession(lose_first_answer=True)
    draft = PurchaseOrderDraft("S-A", "BOLT", 100, 10.0, "Nos", date(2026, 11, 1), reference="prop123")
    first = po_client(s).create_purchase_order(draft)
    assert not first.ok and "could not reach" in first.error and len(s.orders) == 1          # ERPNext did create it
    second = po_client(s).create_purchase_order(draft)
    assert second.ok and second.name == "PO-001" and len(s.orders) == 1                        # found by reference, nothing new created
    assert s.orders[0]["custom_agent_ref"] == "prop123"


def test_a_different_proposal_gets_its_own_order():
    from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft
    s = OrderSession()
    c = po_client(s)
    a = c.create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 1, 1, "Nos", date(2026, 11, 1), reference="p1"))
    b = c.create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 1, 1, "Nos", date(2026, 11, 1), reference="p2"))
    assert (a.name, b.name) == ("PO-001", "PO-002")


def test_without_the_erpnext_field_the_order_is_refused_with_the_fix_not_created_blindly():
    from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft
    s = OrderSession(field_exists=False)
    res = po_client(s).create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 1, 1, "Nos", date(2026, 11, 1), reference="p1"))
    assert not res.ok and "erp-agents erp-setup" in res.error and s.orders == []


def test_erp_setup_creates_the_fields_on_every_document_once():
    s = OrderSession(field_exists=False)
    c = po_client(s)
    assert c.ensure_po_reference_field() == "created"
    made = sorted((f["dt"], f["fieldname"]) for f in s.custom_fields)
    assert made == sorted([("Purchase Order", "custom_agent_ref"), ("Purchase Order", "custom_agent_approved_by"), ("Material Request", "custom_agent_ref"),
                           ("Material Request", "custom_agent_approved_by"), ("Material Request", "custom_agent_note"),
                           ("Stock Entry", "custom_agent_ref"), ("Stock Entry", "custom_agent_approved_by")])
    assert all(f["fieldtype"] in ("Data", "Small Text") and f["read_only"] == 1 for f in s.custom_fields)
    s.field_exists = True
    assert c.ensure_po_reference_field() == "exists" and len(s.custom_fields) == 7


def test_the_order_records_who_approved_it_because_erpnext_only_sees_the_service_account():
    from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft
    s = OrderSession()
    res = po_client(s).create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 5, 1, "Nos", date(2026, 11, 1), reference="p1", approved_by="priya@acme.com via app"))
    assert res.ok and s.orders[0]["custom_agent_approved_by"] == "priya@acme.com via app" and s.orders[0]["custom_agent_ref"] == "p1"


def test_a_cancelled_order_does_not_block_a_new_one_but_the_filter_excludes_it():
    from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft
    s = OrderSession()
    po_client(s).create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 1, 1, "Nos", date(2026, 11, 1), reference="p1"))
    flt = []

    class Spy(OrderSession):
        def get(self, url, params=None, timeout=None):
            if url.endswith("/Purchase Order"):
                flt.append(json.loads(params["filters"]))
            return super().get(url, params, timeout)

    sp = Spy()
    po_client(sp).create_purchase_order(PurchaseOrderDraft("S-A", "BOLT", 1, 1, "Nos", date(2026, 11, 1), reference="p9"))
    assert ["docstatus", "<", 2] in flt[0] and ["custom_agent_ref", "=", "p9"] in flt[0]


# ------------------------------------------------------------------ an item's history starts at its first stock movement when that is earlier than its creation
ITEMS = [
    {"name": "MIGRATED", "item_name": "Migrated item", "item_group": "G", "stock_uom": "Nos", "lead_time_days": 5, "creation": "2026-10-17 09:00:00"},
    {"name": "OLD", "item_name": "Old item", "item_group": "G", "stock_uom": "Nos", "lead_time_days": 5, "creation": "2024-01-01 09:00:00"},
    {"name": "NEW", "item_name": "New item", "item_group": "G", "stock_uom": "Nos", "lead_time_days": 5, "creation": "2026-10-19 09:00:00"},
    {"name": "NOMOVE", "item_name": "Never moved", "item_group": "G", "stock_uom": "Nos", "lead_time_days": 5, "creation": "2026-09-01 09:00:00"},
]


def test_history_begins_at_the_first_stock_movement_when_that_is_earlier_than_the_item_was_created():
    c = client({"Item": ITEMS, "Stock Ledger Entry": [
        {"item_code": "MIGRATED", "first_date": "2025-11-13"},          # a year of history loaded onto an item created last week
        {"item_code": "OLD", "first_date": "2024-06-01"},               # first moved long after it was created: the creation date stays
        {"item_code": "NEW", "first_date": "2026-10-19"},               # created and moved the same week: genuinely new
    ]})
    got = {i.code: i.created for i in c.list_items()}
    assert got == {"MIGRATED": date(2025, 11, 13), "OLD": date(2024, 1, 1), "NEW": date(2026, 10, 19), "NOMOVE": date(2026, 9, 1)}


def test_the_first_movement_is_asked_for_grouped_by_item_in_the_newer_syntax():
    c = client({"Item": ITEMS, "Stock Ledger Entry": []})
    c.list_items()
    url, params = c.session.calls[1]
    assert url.endswith("/api/resource/Stock Ledger Entry") and params["group_by"] == "item_code"
    assert json.loads(params["fields"]) == ["item_code", {"MIN": "posting_date", "as": "first_date"}]
    assert ["is_cancelled", "=", 0] in json.loads(params["filters"])


def test_an_older_erpnext_that_wants_the_text_syntax_is_asked_again_that_way():
    class Picky(FakeSession):
        def get(self, url, params=None, timeout=None):
            if url.endswith("Stock Ledger Entry") and any(isinstance(f, dict) for f in json.loads(params["fields"])):
                self.calls.append((url, params))
                return Resp({"exception": "SQL functions are not allowed as strings"}, 417)
            return super().get(url, params, timeout)
    s = Picky({"Item": ITEMS, "Stock Ledger Entry": [{"item_code": "MIGRATED", "first_date": "2025-11-13"}]})
    c = ERPNextClient("http://erp.local/", "K", "S", session=s)
    assert {i.code: i.created for i in c.list_items()}["MIGRATED"] == date(2025, 11, 13)
    assert "min(posting_date) as first_date" in json.loads(s.calls[-1][1]["fields"])


def test_if_the_first_movement_cannot_be_read_the_creation_date_is_used_and_nothing_breaks(caplog):
    import logging
    c = client({"Item": ITEMS, "Stock Ledger Entry": 403})
    with caplog.at_level(logging.WARNING):
        got = {i.code: i.created for i in c.list_items()}
    assert got["MIGRATED"] == date(2026, 10, 17) and len(got) == 4
    assert "could not read when each item's stock history begins" in " ".join(r.getMessage() for r in caplog.records)


def test_usage_of_a_migrated_item_is_measured_over_its_real_history_not_its_creation_week():
    """The whole point: a year of sales on an item created last week must not look like 'no recent consumption'."""
    from datetime import timedelta

    from erp_agents.packs.stock_procurement.settings import Policy
    from ai_munimji_engine.packs.stock_procurement.erp import DailyUsage, ItemInfo
    from erp_agents.packs.stock_procurement.inventory.analysis import compute_usage
    today = date(2026, 10, 20)
    sales = [DailyUsage(today - timedelta(days=d), 10.0) for d in range(3, 80)]            # 10 a day for the last 80 days, none in the last 3
    blind = compute_usage(sales, ItemInfo("X", "X", created=date(2026, 10, 17)), today, Policy())
    seen = compute_usage(sales, ItemInfo("X", "X", created=date(2025, 11, 13)), today, Policy())
    assert blind.data_days == 3 and blind.nonzero_days <= 1            # three days of "history", one of them with a sale: nothing to plan from
    assert seen.data_days > 300 and 7 < seen.planning_rate < 10        # the real picture: about 8 a day


# ------------------------------------------------------------------ stock in transit warehouses are reported apart from on-hand stock
def _transit_world(**kw):
    return client({"Bin": [
        {"item_code": "A", "warehouse": "Stores - X", "actual_qty": 100, "ordered_qty": 10},
        {"item_code": "A", "warehouse": "Goods In Transit - X", "actual_qty": 30},
        {"item_code": "A", "warehouse": "Quarantine - X", "actual_qty": 7},
    ], "Warehouse": [
        {"name": "Stores - X", "warehouse_type": "Stores", "is_group": 0, "disabled": 0},
        {"name": "Goods In Transit - X", "warehouse_type": "Transit", "is_group": 0, "disabled": 0},
        {"name": "Old Transit - X", "warehouse_type": "Transit", "is_group": 0, "disabled": 1},
        {"name": "Quarantine - X", "warehouse_type": "Quarantine", "is_group": 0, "disabled": 0}],
        "Purchase Order Item": [], "Purchase Order": []}, **kw)


def test_stock_in_a_transit_warehouse_is_reported_as_in_transit_not_as_on_hand():
    s = _transit_world().get_stock(["A"])["A"]
    assert (s.on_hand, s.in_transit, s.incoming) == (107, 30, 10)                      # quarantine is not transit: it stays in on-hand stock (the warehouse filter is the tool for that)


def test_the_transit_lookup_is_cached_and_only_enabled_transit_warehouses_count():
    c = _transit_world()
    assert c._transit_warehouses() == {"Goods In Transit - X"}
    c.get_stock(["A"])
    c.get_stock(["A"])
    assert sum(1 for call in c.session.calls if call[0].endswith("/Warehouse")) == 1


def test_if_the_warehouse_types_cannot_be_read_stock_counts_as_on_hand_and_nothing_fails():
    c = client({"Bin": [{"item_code": "A", "warehouse": "Goods In Transit - X", "actual_qty": 30}, {"item_code": "A", "warehouse": "Stores - X", "actual_qty": 100}],
                "Warehouse": 403, "Purchase Order Item": [], "Purchase Order": []})
    s = c.get_stock(["A"])["A"]
    assert (s.on_hand, s.in_transit) == (130, 0)


def test_a_warehouse_filter_still_applies_and_the_warehouse_is_read_with_every_bin():
    c = _transit_world()
    c.get_stock(["A"], ["Stores - X", "Goods In Transit - X"])
    assert ["warehouse", "in", ["Stores - X", "Goods In Transit - X"]] in json.loads(c.session.calls[0][1]["filters"])
    assert "warehouse" in json.loads(c.session.calls[0][1]["fields"])


# ------------------------------------------------------------------ which company a new document goes to
def _draft():
    from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft
    return PurchaseOrderDraft("S-A", "BOLT", 5, 10.0, "Nos", date(2026, 10, 25))


def test_with_several_companies_and_none_configured_nothing_is_created():
    session = PostSession({"Company": [{"name": "ABC Corp (Demo)"}, {"name": "Test Trading Co"}]}, Resp({"data": {"name": "PO-1"}}))
    res = ERPNextClient("http://erp.local", "K", "S", session=session).create_purchase_order(_draft())
    assert not res.ok and "several companies (ABC Corp (Demo), Test Trading Co)" in res.error and "Set erp.company" in res.error
    assert session.posts == []                                                                  # ERPNext was not asked to create anything


def test_the_configured_company_is_used_for_orders_and_requisitions():
    from ai_munimji_engine.packs.stock_procurement.erp import RequisitionDraft
    session = PostSession({"Company": [{"name": "A"}, {"name": "B"}]}, Resp({"data": {"name": "X-1"}}))
    c = ERPNextClient("http://erp.local", "K", "S", session=session, company="B")
    assert c.create_purchase_order(_draft()).ok
    assert c.create_material_request(RequisitionDraft(item_code="BOLT", qty=5, uom="Nos", required_by=date(2026, 10, 25), rate=None, note="", reference="", approved_by="")).ok
    assert [body["company"] for _url, body in session.posts] == ["B", "B"]


def test_a_single_company_is_named_explicitly_and_an_unreadable_list_changes_nothing():
    session = PostSession({"Company": [{"name": "Only Co"}]}, Resp({"data": {"name": "PO-1"}}))
    assert ERPNextClient("http://erp.local", "K", "S", session=session).create_purchase_order(_draft()).ok
    assert session.posts[0][1]["company"] == "Only Co"
    broken = ERPNextClient("http://erp.local", "K", "S", session=PostSession({"Company": 403}, Resp({"data": {"name": "PO-2"}})))
    assert broken._company_for_new_document() == (None, None)                                   # as before: ERPNext chooses


# ------------------------------------------------------------------ the order carries the template's tax rows (ERPNext fills them only in its own screen)
def _taxed_world(**extra):
    tables = {
        "Purchase Taxes and Charges Template/Domestic - X": {"taxes": [
            {"charge_type": "On Net Total", "account_head": "Input CGST - X", "description": "CGST", "rate": 0, "category": "Total", "add_deduct_tax": "Add"},
            {"charge_type": "On Net Total", "account_head": "Input SGST - X", "description": "SGST", "rate": 0, "category": "Total", "add_deduct_tax": "Add"},
            {"charge_type": "Actual", "account_head": "Port - X", "description": "Port handling", "tax_amount": 1000, "category": "Valuation", "add_deduct_tax": "Add"}]},
        "Supplier/S-A": {"tax_category": "Local"},
        "Item/BOLT": {"taxes": [{"item_tax_template": "HS 7318 / GST 18% - X", "tax_category": None}]},
        "Item Tax Template/HS 7318 / GST 18% - X": {"taxes": [{"tax_type": "Input CGST - X", "tax_rate": 9}, {"tax_type": "Input SGST - X", "tax_rate": 9}]},
        **extra,
    }
    session = PostSession(tables, Resp({"data": {"name": "PO-1"}}))
    return session, ERPNextClient("http://erp.local", "K", "S", session=session, company="Demo Co")


def _taxed_draft(template="Domestic - X"):
    from ai_munimji_engine.packs.stock_procurement.erp import PurchaseOrderDraft
    return PurchaseOrderDraft("S-A", "BOLT", 5, 10.0, "Nos", date(2026, 10, 25), taxes_template=template)


def test_the_order_gets_the_templates_tax_rows_and_the_items_own_tax_template():
    session, c = _taxed_world()
    assert c.create_purchase_order(_taxed_draft()).ok
    body = session.posts[0][1]
    assert body["taxes_and_charges"] == "Domestic - X"
    assert [(t["account_head"], t["charge_type"]) for t in body["taxes"]] == [("Input CGST - X", "On Net Total"), ("Input SGST - X", "On Net Total"), ("Port - X", "Actual")]
    assert body["taxes"][2]["tax_amount"] == 1000 and body["taxes"][2]["category"] == "Valuation"
    line = body["items"][0]
    assert line["item_tax_template"] == "HS 7318 / GST 18% - X" and json.loads(line["item_tax_rate"]) == {"Input CGST - X": 9.0, "Input SGST - X": 9.0}


def test_a_fixed_charge_in_company_currency_is_shown_in_the_orders_currency():
    session, c = _taxed_world()
    c._in_supplier_currency = lambda body, draft: body.update({"currency": "USD", "conversion_rate": 50.0})
    assert c.create_purchase_order(_taxed_draft()).ok
    assert session.posts[0][1]["taxes"][2]["tax_amount"] == 20.0                               # 1000 in company currency at 50 per USD


def test_if_the_template_cannot_be_read_the_order_is_still_created_with_the_template_named():
    session, c = _taxed_world()
    session.tables.pop("Purchase Taxes and Charges Template/Domestic - X")
    assert c.create_purchase_order(_taxed_draft()).ok
    body = session.posts[0][1]
    assert body["taxes_and_charges"] == "Domestic - X" and "taxes" not in body


def test_an_order_without_a_template_has_no_tax_rows():
    session, c = _taxed_world()
    assert c.create_purchase_order(_taxed_draft(template=None)).ok
    assert "taxes" not in session.posts[0][1] and "item_tax_template" not in session.posts[0][1]["items"][0]

"""The loader of the shared ERP-client checks' world (tools/testdata/contract.py), against an in-memory ERPNext: what it posts, in what state, and that a second run posts nothing."""
import json
from urllib.parse import unquote

import pytest

from ai_munimji_engine.testkit.world import WORLD
from test_testdata_writer import FakeERP, R

from ai_munimji_engine.testkit import loader as contract
from tools.testdata.writer import Writer


class ContractERP(FakeERP):
    """The in-memory ERPNext of the loader's tests, plus the two server methods the contract loader calls (the purchase receipt mapper and the order's status)."""

    def __init__(self):
        super().__init__()
        self.docs[("Company", "Contract Co")] = {"name": "Contract Co", "abbr": "TTC", "docstatus": 0}
        self.docs[("Account", "Duties and Taxes - TTC")] = {"name": "Duties and Taxes - TTC", "account_name": "Duties and Taxes", "company": "Contract Co", "is_group": 1}
        self.methods: list[tuple[str, dict]] = []

    def post(self, url, json=None, timeout=None):
        if "/api/method/" in url and "frappe.client" not in url:
            method = unquote(url.split("/api/method/")[1])
            self.methods.append((method, json))
            if method.endswith("make_purchase_receipt"):
                po = self.docs[("Purchase Order", json["source_name"])]
                return R({"message": {"doctype": "Purchase Receipt", "name": "new-purchase-receipt-1", "supplier": po["supplier"], "company": po["company"],
                                      "items": [{**line, "name": "new-line-1", "purchase_order": po["name"]} for line in po["items"]]}})
            if method.endswith("update_status"):
                self.docs[("Purchase Order", json["name"])]["status"] = json["status"]
                return R({"message": json["status"]})
        return super().post(url, json, timeout)

    def count(self, doctype, **where):
        return sum(1 for (t, _n), d in self.docs.items() if t == doctype and all(d.get(k) == v for k, v in where.items()))


@pytest.fixture
def loaded(tmp_path):
    erp = ContractERP()
    out = tmp_path / "contract-site.json"
    site = contract.run(Writer(erp, "http://erp.local", None, pause=0), out)
    return erp, out, site


def test_every_part_of_the_world_is_posted_and_submitted(loaded):
    erp, _out, site = loaded
    world = WORLD
    assert site["company"] == "Contract Co" and site["abbr"] == "TTC"
    assert erp.count("Stock Entry", docstatus=1) == len(world.moves)
    assert erp.count("Delivery Note", docstatus=1) == len(world.sales)
    assert erp.count("Purchase Order", docstatus=1) == len(world.orders)
    assert erp.count("Purchase Receipt", docstatus=1) == sum(1 for o in world.orders if o.received_after is not None)
    assert erp.count("Sales Order", docstatus=1) == len(world.sales_orders)
    assert erp.count("Supplier Quotation", docstatus=1) == len(world.quotes)
    assert erp.count("Item") == len(world.items) and erp.count("Supplier") == len(world.suppliers)
    assert erp.count("Warehouse") == len(world.warehouses)
    assert erp.count("Tax Rule") == len(world.tax_rules) and erp.count("Purchase Taxes and Charges Template") == len(world.tax_rules)


def test_the_site_description_names_every_part_and_the_reference_day(loaded):
    _erp, out, site = loaded
    described = json.loads(out.read_text(encoding="utf-8"))
    assert described == site
    assert described["names"]["steady"] == "CT-STEADY" and described["names"]["wh_a"] == "Contract A - TTC" and described["names"]["duty"] == "Contract Duty - TTC"
    assert described["names"]["s1"] == "Contract reliable supplier"
    assert {i.key for i in WORLD.items} | {w.key for w in WORLD.warehouses} | {s.key for s in WORLD.suppliers} <= set(described["names"])


def test_what_a_real_erp_refuses_is_done_last(loaded):
    erp, *_ = loaded
    assert erp.docs[("Supplier", "Contract disabled supplier")].get("disabled") == 1, "disabled after its order was made"
    assert erp.docs[("Item", "CT-OFF")].get("disabled") == 1
    assert erp.docs[("Warehouse", "Contract X - TTC")].get("disabled") == 1
    assert not erp.docs[("Item", "CT-STEADY")].get("disabled")


def test_the_orders_carry_their_dates_references_and_the_closed_one_is_closed(loaded):
    erp, *_ = loaded
    orders = {d["order_confirmation_no"]: d for (t, _), d in erp.docs.items() if t == "Purchase Order"}
    o2 = orders["TD/CT-O2"]
    assert (o2["custom_agent_ref"], o2["custom_agent_approved_by"]) == ("CT-REF-O2", "ops@example.com via desk")
    assert "custom_agent_ref" not in orders["TD/CT-O3"], "an order made by hand carries no reference"
    assert orders["TD/CT-O3"].get("status") == "Closed" and not orders["TD/CT-O1"].get("status")
    assert orders["TD/CT-O4"]["transaction_date"] > orders["TD/CT-O1"]["transaction_date"]
    receipts = {d["supplier_delivery_note"] for (t, _), d in erp.docs.items() if t == "Purchase Receipt"}
    assert receipts == {"TD/CT-O1", "TD/CT-O2", "TD/CT-O3", "TD/CT-O6"}, "the open order has no receipt"


def test_the_item_supplier_list_and_the_payment_terms_are_set(loaded):
    erp, *_ = loaded
    assert [r["supplier"] for r in erp.docs[("Item", "CT-STEADY")]["supplier_items"]] == ["Contract reliable supplier", "Contract quoting supplier"]
    assert erp.docs[("Supplier", "Contract reliable supplier")]["payment_terms"] == "Contract Net 30"
    assert "payment_terms" not in erp.docs[("Supplier", "Contract late supplier")]


def test_a_second_run_posts_nothing_and_keeps_the_reference_day(loaded, tmp_path):
    erp, out, first = loaded
    before = len(erp.docs)
    again = contract.run(Writer(erp, "http://erp.local", None, pause=0), out)
    assert len(erp.docs) == before, "idempotent"
    assert again["today"] == first["today"] and again["names"] == first["names"]


def test_a_dry_run_writes_nothing(tmp_path):
    erp = ContractERP()
    before = len(erp.docs)
    contract.run(Writer(erp, "http://erp.local", None, dry_run=True, pause=0), tmp_path / "site.json")
    assert len(erp.docs) == before and not (tmp_path / "site.json").exists()

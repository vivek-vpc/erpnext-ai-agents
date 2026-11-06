"""Posts the contract world (`ai_munimji_engine.testkit.world.WORLD`) into an ERPNext site, and writes down the names the site gave its parts.

It talks to the site through a *writer*, anything with the methods of `WriterLike` below. Two exist: tools/testdata's REST `Writer` (erpnext-ai-agents:
`python -m tools.testdata.cli contract --url http://localhost:8000 --key K --secret S`), which can load any ERPNext over HTTP; and the Frappe app's in-process writer
(AI-Munimji: dev/scripts/inprocess_writer.py), which loads the bench it runs in without HTTP or API keys. The loading logic is here, once, so that both load the same world.

It is small on purpose (about 60 documents: the shared ERP-client checks need a few items, suppliers and orders, not a year of trade) and idempotent: every document carries a
marker (`TD/CT-...`) in a normal field, so a second run finds what exists and posts the rest. It uses the company the site has (the only one, or `--company`), whatever its name.
Dates are relative to the day of the first run, which is kept in the output file: the checks read the reference day from there, never from the clock.

It touches nothing that exists: what it makes carries the marker, and the output file names the rest.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional, Protocol

from .world import WORLD, World, default_names


class LoaderError(RuntimeError):
    pass


class WriterLike(Protocol):
    """What the loader needs of a writer: the methods of tools/testdata's `Writer`."""
    dry_run: bool

    def find(self, doctype: str, filters: list, fields: Optional[list] = None, limit: int = 1000) -> list[dict]: ...
    def get(self, doctype: str, name: str) -> dict: ...
    def ensure(self, doctype: str, name: str, doc: dict) -> str: ...
    def ensure_one(self, doctype: str, filters: list, doc: dict) -> str: ...
    def create(self, doctype: str, doc: dict) -> str: ...
    def update(self, doctype: str, name: str, changes: dict) -> None: ...
    def submit(self, doctype: str, name: str) -> None: ...
    def call(self, method_path: str, **payload: Any) -> Any: ...


def strip_new(doc: Any) -> Any:
    """Make a document returned by an ERPNext mapper insertable: drop 'new-...' names and parent bookkeeping."""
    if isinstance(doc, list):
        return [strip_new(d) for d in doc]
    if isinstance(doc, dict):
        return {k: strip_new(v) for k, v in doc.items()
                if k not in ("name", "owner", "creation", "modified", "modified_by", "parent", "parenttype", "parentfield", "idx", "docstatus", "__islocal", "__unsaved", "amended_from")}
    return doc


MARK = "TD/CT-"
MAKE_PR = "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt"
UPDATE_PO_STATUS = "erpnext.buying.doctype.purchase_order.purchase_order.update_status"
CUSTOMER = "Contract Customer"
CUSTOMER_GROUP = "Contract Customers"
TERRITORY = "Contract Territory"
ITEM_GROUP = "Contract Goods"
TERMS = {30: ("Contract 30 days", "Contract Net 30")}               # credit days -> (the payment term, its template)
REF_FIELD, APPROVED_FIELD = "custom_agent_ref", "custom_agent_approved_by"      # the fields `erp-agents erp-setup` makes, and the app's fixtures


class Loader:
    def __init__(self, w: WriterLike, world: World = WORLD, today: Optional[date] = None, company: Optional[str] = None) -> None:
        self.w, self.world, self.today = w, world, today or date.today()
        self.company, self.abbr = self._company(company)
        self.names = self._names()

    # ------------------------------------------------------------------ names
    def _company(self, wanted: Optional[str]) -> tuple[str, str]:
        rows = self.w.find("Company", [["name", "=", wanted]] if wanted else [], ["name", "abbr"], limit=50)
        if len(rows) != 1:
            raise LoaderError("name the company: " + (f"{wanted!r} was not found" if wanted else f"the site has {len(rows)} companies") + " (--company)")
        return rows[0]["name"], rows[0]["abbr"]

    def _names(self) -> dict[str, str]:
        names = default_names(self.world)
        for wh in self.world.warehouses:
            names[wh.key] = f"{names[wh.key]} - {self.abbr}"             # a warehouse and a template carry the company abbreviation in ERPNext
        for rule in self.world.tax_rules:
            names[rule.template] = f"{names[rule.template]} - {self.abbr}"
        return names

    def n(self, key: str) -> str:
        return self.names[key]

    def when(self, days_ago: int) -> str:
        return (self.today - timedelta(days=days_ago)).isoformat()

    # ------------------------------------------------------------------ the parts
    def run(self) -> dict[str, Any]:
        self.masters()
        self.stock()
        self.orders()
        self.sales()
        self.quotes()
        self.taxes()
        self.disable()
        return {"today": self.today.isoformat(), "company": self.company, "abbr": self.abbr, "names": self.names}

    def masters(self) -> None:
        w, world = self.w, self.world
        w.ensure("Item Group", ITEM_GROUP, {"item_group_name": ITEM_GROUP, "parent_item_group": "All Item Groups", "is_group": 0})
        w.ensure("Customer Group", CUSTOMER_GROUP, {"customer_group_name": CUSTOMER_GROUP, "parent_customer_group": "All Customer Groups", "is_group": 0})       # ERPNext refuses a group here
        w.ensure("Territory", TERRITORY, {"territory_name": TERRITORY, "parent_territory": "All Territories", "is_group": 0})
        w.ensure_one("Customer", [["customer_name", "=", CUSTOMER]], {"customer_name": CUSTOMER, "customer_group": CUSTOMER_GROUP, "territory": TERRITORY, "customer_type": "Company"})
        for days, (term, template) in TERMS.items():
            row = {"payment_term": term, "invoice_portion": 100, "due_date_based_on": "Day(s) after invoice date", "credit_days": days}
            w.ensure("Payment Term", term, {"payment_term_name": term, "invoice_portion": 100, "due_date_based_on": "Day(s) after invoice date", "credit_days": days})
            w.ensure("Payment Terms Template", template, {"template_name": template, "terms": [row]})
        for s in world.suppliers:
            doc: dict[str, Any] = {"supplier_name": s.name, "supplier_group": "All Supplier Groups", "supplier_type": "Company"}
            if s.payment_days:
                doc["payment_terms"] = TERMS[s.payment_days][1]
            w.ensure_one("Supplier", [["supplier_name", "=", s.name]], doc)
            self.names[s.key] = self._supplier_name(s.name)
        for wh in world.warehouses:
            doc = {"warehouse_name": self.names[wh.key].removesuffix(f" - {self.abbr}"), "company": self.company, "is_group": 1 if wh.kind == "group" else 0}
            if wh.kind == "transit":
                doc["warehouse_type"] = "Transit"
            w.ensure("Warehouse", self.n(wh.key), doc)
        for it in world.items:
            doc = {"item_code": self.n(it.key), "item_name": it.name, "item_group": ITEM_GROUP, "stock_uom": "Nos", "is_stock_item": 1 if it.stock_item else 0,
                   "supplier_items": [{"supplier": self.n(k)} for k in it.suppliers]}
            if it.lead_time_days:
                doc["lead_time_days"] = it.lead_time_days
            if it.min_order_qty:
                doc["min_order_qty"] = it.min_order_qty
            w.ensure("Item", self.n(it.key), doc)

    def _supplier_name(self, supplier_name: str) -> str:
        rows = self.w.find("Supplier", [["supplier_name", "=", supplier_name]], ["name"], limit=1)
        return rows[0]["name"] if rows else supplier_name

    def _make(self, doctype: str, marker_field: str, marker: str, doc: dict[str, Any], submit: bool = True) -> str:
        """The document carrying this marker, made and submitted if it does not exist yet (a draft left by an earlier run is submitted)."""
        found = self.w.find(doctype, [[marker_field, "=", marker]], ["name", "docstatus"], limit=1)
        if found:
            name = found[0]["name"]
            if submit and found[0]["docstatus"] == 0:
                self.w.submit(doctype, name)
            return name
        name = self.w.create(doctype, {**doc, marker_field: marker})
        if submit:
            self.w.submit(doctype, name)
        return name

    def stock(self) -> None:
        for i, m in enumerate(self.world.moves):
            self._make("Stock Entry", "remarks", f"{MARK}M{i}", {
                "stock_entry_type": "Material Receipt", "purpose": "Material Receipt", "company": self.company, "posting_date": self.when(m.days_ago), "posting_time": "09:00:00",
                "set_posting_time": 1, "to_warehouse": self.n(m.warehouse),
                "items": [{"item_code": self.n(m.item), "t_warehouse": self.n(m.warehouse), "qty": m.qty, "basic_rate": m.rate, "conversion_factor": 1, "uom": "Nos"}],
            })

    def orders(self) -> None:
        for o in sorted(self.world.orders, key=lambda o: -o.days_ago):
            day, marker = self.when(o.days_ago), f"{MARK}{o.key.upper()}"
            due = (self.today - timedelta(days=o.days_ago) + timedelta(days=o.due_in)).isoformat()
            doc: dict[str, Any] = {
                "supplier": self.n(o.supplier), "company": self.company, "transaction_date": day, "schedule_date": due,
                "items": [{"item_code": self.n(o.item), "qty": o.qty, "rate": o.rate, "warehouse": self.n(o.warehouse), "schedule_date": due, "uom": "Nos"}],
            }
            if o.reference:
                doc[REF_FIELD], doc[APPROVED_FIELD] = o.reference, o.approved_by
            po = self._make("Purchase Order", "order_confirmation_no", marker, doc)
            if self.w.dry_run:
                continue                                               # the receipt is made from the order, which a dry run did not make
            if o.received_after is not None:
                got = (self.today - timedelta(days=o.days_ago - o.received_after)).isoformat()
                if not self.w.find("Purchase Receipt", [["supplier_delivery_note", "=", marker]], ["name"], limit=1):
                    pr = strip_new(self.w.call(MAKE_PR, source_name=po))
                    pr.update({"posting_date": got, "posting_time": "10:00:00", "set_posting_time": 1, "supplier_delivery_note": marker})
                    self.w.submit("Purchase Receipt", self.w.create("Purchase Receipt", pr))
            if o.closed and self.w.get("Purchase Order", po).get("status") != "Closed":
                self.w.call(UPDATE_PO_STATUS, status="Closed", name=po)

    def sales(self) -> None:
        for i, s in enumerate(self.world.sales):
            self._make("Delivery Note", "po_no", f"{MARK}D{i}", {
                "customer": CUSTOMER, "company": self.company, "posting_date": self.when(s.days_ago), "posting_time": "11:00:00", "set_posting_time": 1,
                "items": [{"item_code": self.n(s.item), "qty": s.qty, "rate": 20.0, "warehouse": self.n(s.warehouse), "uom": "Nos"}],
            })
        for i, so in enumerate(self.world.sales_orders):
            delivery = (self.today + timedelta(days=so.due_in)).isoformat()
            self._make("Sales Order", "po_no", f"{MARK}SO{i}", {
                "customer": CUSTOMER, "company": self.company, "transaction_date": self.today.isoformat(), "delivery_date": delivery,
                "items": [{"item_code": self.n(so.item), "qty": so.qty, "rate": 20.0, "warehouse": self.n(so.warehouse), "delivery_date": delivery, "uom": "Nos"}],
            })

    def quotes(self) -> None:
        for i, q in enumerate(self.world.quotes):
            made = self.today - timedelta(days=q.days_ago)
            doc: dict[str, Any] = {"supplier": self.n(q.supplier), "company": self.company, "transaction_date": made.isoformat(),
                                   "items": [{"item_code": self.n(q.item), "qty": q.qty, "rate": q.rate, "uom": "Nos"}]}
            if q.valid_for is not None:
                doc["valid_till"] = (made + timedelta(days=q.valid_for)).isoformat()
            self._make("Supplier Quotation", "quotation_number", f"{MARK}Q{i}", doc)

    def taxes(self) -> None:
        if not self.world.tax_rules:
            return
        w = self.w
        parent = w.find("Account", [["company", "=", self.company], ["account_name", "=", "Duties and Taxes"], ["is_group", "=", 1]], ["name"], limit=1)
        if not parent:
            raise LoaderError("the company has no 'Duties and Taxes' account group to put the contract duty account under")
        account = f"Contract Duty - {self.abbr}"
        w.ensure("Account", account, {"account_name": "Contract Duty", "parent_account": parent[0]["name"], "company": self.company, "account_type": "Tax", "is_group": 0})
        for rule in self.world.tax_rules:
            template = self.n(rule.template)
            w.ensure("Purchase Taxes and Charges Template", template, {
                "title": template.removesuffix(f" - {self.abbr}"), "company": self.company,
                "taxes": [{"category": r.category, "add_deduct_tax": r.add_deduct, "charge_type": r.charge_type, "account_head": account, "description": r.description, "rate": r.rate}
                          for r in rule.rows],
            })
            w.ensure_one("Tax Rule", [["tax_type", "=", "Purchase"], ["supplier", "=", self.n(rule.supplier)], ["purchase_tax_template", "=", template]],
                         {"tax_type": "Purchase", "supplier": self.n(rule.supplier), "purchase_tax_template": template, "company": self.company, "priority": 1})

    def disable(self) -> None:
        """Last, because ERPNext refuses a new order from a disabled supplier and a new entry in a disabled warehouse or for a disabled item."""
        for s in self.world.suppliers:
            if s.disabled:
                self.w.update("Supplier", self.n(s.key), {"disabled": 1})
        for wh in self.world.warehouses:
            if wh.kind == "disabled":
                self.w.update("Warehouse", self.n(wh.key), {"disabled": 1})
        for it in self.world.items:
            if it.disabled:
                self.w.update("Item", self.n(it.key), {"disabled": 1})


def run(w: WriterLike, out: Path, company: Optional[str] = None) -> dict[str, Any]:
    """Load the world and write the description of the site (reference day, names) to `out`. A second run keeps the reference day of the first."""
    today = None
    if out.exists():
        today = date.fromisoformat(json.loads(out.read_text(encoding="utf-8"))["today"])
    loader = Loader(w, WORLD, today, company)
    site = loader.run()
    if not w.dry_run:
        out.write_text(json.dumps(site, indent=1), encoding="utf-8")
    return site

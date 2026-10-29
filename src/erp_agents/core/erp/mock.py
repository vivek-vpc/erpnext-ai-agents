"""In-memory demo ERP with deterministic scenarios, one per behaviour the agent must handle.

Used by `erp-agents demo`, the tests, and as the default `erp.type: mock` so the whole system
can be tried on a laptop with nothing else installed.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional, Sequence, Any

from dataclasses import replace

from .base import (CountLine, DailyUsage, FoundDocument, ItemInfo, MessageResult, PurchaseOrderDraft, PurchaseOrderResult, QuoteRule, ReceiptLine, RequisitionDraft, SalesDue, StockInfo,
                   StockTransferDraft, SupplierContact, TaxSetup, VendorOffer, WarehouseStock)

HISTORY_DAYS = 480


MAIN_WAREHOUSE = "Main Warehouse"


@dataclass
class MockItem:
    info: ItemInfo
    stock: StockInfo
    usage: list[DailyUsage] = field(default_factory=list)
    scenario: str = ""
    by_warehouse: dict[str, StockInfo] = field(default_factory=dict)      # empty = everything sits in MAIN_WAREHOUSE


def _sum_stock(parts: list[StockInfo]) -> StockInfo:
    if not parts:
        return StockInfo(on_hand=0.0)
    names = [f for f in StockInfo.__dataclass_fields__ if f not in ("next_arrival", "projected", "by_warehouse")]
    arrivals = [p.next_arrival for p in parts if p.next_arrival]
    return StockInfo(**{f: sum(getattr(p, f) for p in parts) for f in names}, next_arrival=min(arrivals) if arrivals else None)


class MockERP:
    def __init__(self, today: Optional[date] = None, seed: int = 7, scenarios: bool = True) -> None:
        self.today = today or date.today()
        self.seed = seed
        self.items: dict[str, MockItem] = {}
        self.healthy = True
        self.vendors: dict[str, list[VendorOffer]] = {}
        self.purchase_taxes: dict[str, TaxSetup] = {}        # supplier -> the duties and taxes the demo ERP applies to them
        self.purchase_orders: list[dict] = []
        self.po_name_format = "PO-MOCK-{:04d}"                               # tests that need numbers that look like the ERP's (PUR-ORD-2026-00012) set this
        self.requisitions: list[dict] = []
        self.stock_transfers: list[dict] = []
        self.fail_purchase_orders = False        # tests: simulate the ERP refusing the write
        self.refuse_update = ''                  # tests: when set, changing an order's quantity is refused with this message
        self.refuse_cancel = ''                  # tests: when set, cancelling or deleting an order is refused with this message
        # purchasing follow-up and stock intelligence (see FollowUpERP): empty unless a test or the demo fills them
        self.receipts: list[ReceiptLine] = []
        self.sales_orders: dict[str, list[SalesDue]] = {}
        self.contacts: dict[str, SupplierContact] = {}
        self.alternatives: dict[str, list[str]] = {}
        self.counts: list[CountLine] = []
        self.stock_values: dict[str, float] = {}
        self.sent_emails: list[dict[str, Any]] = []
        self.refuse_email = ''                   # tests: when set, sending an e-mail fails with this message
        if scenarios:
            self._build_scenarios()

    # ------------------------------------------------------------------ ERPClient
    def list_items(self) -> list[ItemInfo]:
        return [m.info for m in self.items.values()]

    def get_stock(self, item_codes: Sequence[str], warehouses: Optional[Sequence[str]] = None) -> dict[str, StockInfo]:
        out: dict[str, StockInfo] = {}
        for c in item_codes:
            if c not in self.items:
                continue
            item = self.items[c]
            held = {name: s for name, s in (item.by_warehouse or {MAIN_WAREHOUSE: item.stock}).items() if not warehouses or name in warehouses}
            places = tuple(sorted((WarehouseStock(name, s.on_hand, s.reserved, s.incoming) for name, s in held.items() if s.on_hand or s.reserved or s.incoming),
                                  key=lambda p: (-p.on_hand, p.warehouse)))
            out[c] = replace(item.stock if not warehouses else _sum_stock(list(held.values())), by_warehouse=places)
        return out

    def get_purchase_taxes(self, supplier: str, item_code: str, posting_date: date) -> TaxSetup:
        """What the (demo) ERP's tax rules say applies: nothing, unless a test or demo set it for this supplier."""
        return self.purchase_taxes.get(supplier, TaxSetup(template=None, source="none"))

    def list_warehouses(self) -> list[str]:
        names = {MAIN_WAREHOUSE}
        for m in self.items.values():
            names.update(m.by_warehouse)
        return sorted(names)

    def get_consumption(self, item_codes: Sequence[str], start: date, end: date,
                        warehouses: Optional[Sequence[str]] = None) -> dict[str, list[DailyUsage]]:      # usage is not split by warehouse here
        return {
            c: [u for u in self.items[c].usage if start <= u.day <= end]
            for c in item_codes
            if c in self.items
        }

    def get_vendor_offers(self, item_code: str, since: date, quotes: Optional[QuoteRule] = None) -> list[VendorOffer]:
        """Like the real ERP: quotations are only read when asked for, and only the ones still valid; a supplier known only from a quotation appears only then."""
        none = dict(quote_price=None, quote_ref=None, quote_date=None, quote_valid_till=None, quote_qty=None, quote_lead_days=None)
        out = []
        for o in self.vendors.get(item_code, []):
            valid = quotes is not None and o.quote_price and (o.quote_valid_till is None and (o.quote_date is None or (quotes.today - o.quote_date).days <= quotes.max_age_days)
                                                              or (o.quote_valid_till is not None and o.quote_valid_till >= quotes.today))
            if valid:
                out.append(o)
                continue
            if o.quote_price and not o.unit_price and not o.avg_price and not o.listed:
                continue                                   # known only from a quotation, and quotations are not being read (or this one has lapsed)
            out.append(replace(o, **none))
        return out

    def item_min_order_qty(self, item_code: str) -> Optional[float]:
        return getattr(self, "min_order_qty", {}).get(item_code)

    def update_purchase_order_qty(self, name: str, reference: str, item_code: str, qty: float, acting: Any = None) -> PurchaseOrderResult:
        po = next((p for p in self.purchase_orders if p["name"] == name), None)
        if po is None:
            return PurchaseOrderResult(ok=False, error="it is no longer in ERPNext")
        found = po.get("reference") or ""
        if not reference or not (found == reference or found.startswith(reference + "-")):
            return PurchaseOrderResult(ok=False, error="it was not created by the Stock Assistant for this request, so it is left alone")
        if po.get("docstatus", 0) == 2:
            return PurchaseOrderResult(ok=False, error="it has been cancelled in ERPNext")
        if po.get("item_code") != item_code:
            return PurchaseOrderResult(ok=False, error=f"it has 0 lines for {item_code}, so change it in ERPNext")
        received = float(po.get("per_received") or 0) / 100 * float(po.get("qty") or 0)
        if qty < received:
            return PurchaseOrderResult(ok=False, error=f"{received:g} have already been received against it, so it cannot be for less than that")
        if self.refuse_update:
            return PurchaseOrderResult(ok=False, error=self.refuse_update)
        old = float(po.get("qty") or 0)
        po["qty"] = qty
        if po.get("item_code") in self.items:                                   # like a real open PO: what is still to come counts as incoming
            cur = self.items[po["item_code"]].stock
            self.set_stock(po["item_code"], incoming=max(cur.incoming + qty - old, 0.0))
        return PurchaseOrderResult(ok=True, name=name, total=round(qty * float(po.get("rate") or 0), 2), submitted=po.get("docstatus", 0) == 1, note="changed the quantity")

    def cancel_purchase_order(self, name: str, reference: str, acting: Any = None) -> PurchaseOrderResult:
        po = next((p for p in self.purchase_orders if p["name"] == name), None)
        if po is None:
            return PurchaseOrderResult(ok=True, name=name, note="already gone")
        found = po.get("reference") or ""
        if not reference or not (found == reference or found.startswith(reference + "-")):
            return PurchaseOrderResult(ok=False, error="it was not created by the Stock Assistant for this request, so it is left alone")
        if po.get("docstatus", 0) == 2:
            return PurchaseOrderResult(ok=True, name=name, note="already cancelled")
        if po.get("per_received") or po.get("per_billed"):
            return PurchaseOrderResult(ok=False, error=f"{po.get('per_received', 0):g}% of it has been received and {po.get('per_billed', 0):g}% billed, so it has to be dealt with in ERPNext")
        if self.refuse_cancel:
            return PurchaseOrderResult(ok=False, error=self.refuse_cancel)
        draft = po.get("docstatus", 0) == 0
        if draft:
            self.purchase_orders.remove(po)
        else:
            po["docstatus"], po["status"] = 2, "Cancelled"
        if po.get("item_code") in self.items:                                   # nothing is coming any more
            cur = self.items[po["item_code"]].stock
            self.set_stock(po["item_code"], incoming=max(cur.incoming - float(po.get("qty") or 0), 0.0))
        return PurchaseOrderResult(ok=True, name=name, note="deleted the draft" if draft else "cancelled it")

    def get_documents(self, doctype: str, name: Optional[str] = None, item_code: Optional[str] = None, since: Optional[date] = None, limit: int = 20,
                      open_only: bool = False) -> list[dict[str, Any]]:
        """What the demo ERP knows: the orders and requisitions created through it (so the chat has something to answer about)."""
        source = self.purchase_orders if doctype == "Purchase Order" else self.requisitions if doctype == "Material Request" else []
        out = []
        for d in reversed(source):
            if name and d["name"] != name:
                continue
            if open_only and (d.get("status") in ("Completed", "Closed", "Cancelled", "Delivered") or d.get("docstatus", 0) == 2):
                continue
            if item_code and d.get("item_code") != item_code:
                continue
            out.append({"doctype": doctype, "name": d["name"], "status": d.get("status", "Draft"), "docstatus": d.get("docstatus", 0), "party": d.get("supplier"), "party_id": d.get("supplier_id") or d.get("supplier"), "date": self.today.isoformat() if hasattr(self, "today") and hasattr(self.today, "isoformat") else None,
                        "due": d["required_by"].isoformat() if d.get("required_by") else None, "created_by": d.get("created_by"), "approved_by": d.get("approved_by", ""),
                        "per_received": d.get("per_received", 0.0), "per_billed": d.get("per_billed", 0.0), "total": None, "currency": None, "kind": "Purchase" if doctype == "Material Request" else None,
                        "lines": [{"item_code": d.get("item_code"), "item_name": d.get("item_code"), "qty": d.get("qty"), "uom": d.get("uom"), "done_qty": float(d.get("received_qty") or 0.0)}]})
        return out[:limit]

    def find_by_reference(self, doctype: str, reference: str) -> Optional[FoundDocument]:
        """Like the real client: the not-cancelled document made for this reference, with the approver written on it."""
        rows = {"Purchase Order": self.purchase_orders, "Material Request": self.requisitions, "Stock Entry": self.stock_transfers}.get(doctype, [])
        for d in rows:
            if reference and d.get("reference") == reference and d.get("docstatus", 0) < 2:
                return FoundDocument(doctype, d["name"], d.get("approved_by") or "")
        return None

    def create_material_request(self, draft: RequisitionDraft, acting: Any = None) -> PurchaseOrderResult:
        if self.fail_purchase_orders:
            return PurchaseOrderResult(ok=False, error="mock ERP refused the purchase requisition")
        for r in self.requisitions:
            if draft.reference and r.get("reference") == draft.reference:
                return PurchaseOrderResult(ok=True, name=r["name"])
        name = f"MR-MOCK-{len(self.requisitions) + 1:04d}"
        self.requisitions.append({"name": name, "reference": draft.reference, "approved_by": draft.approved_by, "item_code": draft.item_code, "qty": draft.qty, "uom": draft.uom,
                                  "rate": draft.rate, "required_by": draft.required_by, "note": draft.note, "created_by": acting.user if acting is not None else "service account"})
        return PurchaseOrderResult(ok=True, name=name)

    def create_purchase_order(self, draft: PurchaseOrderDraft, acting: Any = None) -> PurchaseOrderResult:
        if self.fail_purchase_orders:
            return PurchaseOrderResult(ok=False, error="mock ERP refused the purchase order")
        if draft.reference:
            for po in self.purchase_orders:
                if po.get("reference") == draft.reference and po.get("docstatus", 0) < 2:        # like the real client: a retry finds the order it already made (not a cancelled one)
                    return PurchaseOrderResult(ok=True, name=po["name"])
        self._po_seq = getattr(self, "_po_seq", 0) + 1                      # numbers are never reused, even after an order was deleted (like the ERP's)
        name = self.po_name_format.format(max(self._po_seq, len(self.purchase_orders) + 1))
        self.purchase_orders.append({"reference": draft.reference, "approved_by": draft.approved_by, "created_by": acting.user if acting is not None else "service account", "name": name, "supplier": draft.supplier, "item_code": draft.item_code,
                                     "qty": draft.qty, "rate": draft.rate, "required_by": draft.required_by, "remarks": draft.remarks,
                                     "vendor_note": draft.vendor_note, "taxes_template": draft.taxes_template, "supplier_quotation": draft.supplier_quotation})
        if draft.item_code in self.items:               # like a real open PO: counts as incoming stock
            cur = self.items[draft.item_code].stock
            self.set_stock(draft.item_code, incoming=cur.incoming + draft.qty, next_arrival=draft.required_by)
        return PurchaseOrderResult(ok=True, name=name)

    def create_stock_transfer(self, draft: StockTransferDraft, acting: Any = None) -> PurchaseOrderResult:
        """A DRAFT, like the real client: no stock moves until `submit_stock_transfer` (what a person does in ERPNext)."""
        if self.fail_purchase_orders:
            return PurchaseOrderResult(ok=False, error="mock ERP refused the stock transfer")
        for t in self.stock_transfers:
            if draft.reference and t.get("reference") == draft.reference and t["docstatus"] < 2:       # a retry finds the document it already made
                return PurchaseOrderResult(ok=True, name=t["name"])
        name = f"STE-MOCK-{len(self.stock_transfers) + 1:04d}"
        self.stock_transfers.append({"name": name, "docstatus": 0, "reference": draft.reference, "approved_by": draft.approved_by, "item_code": draft.item_code, "qty": draft.qty,
                                     "uom": draft.uom, "from_warehouse": draft.from_warehouse, "to_warehouse": draft.to_warehouse, "remarks": draft.remarks,
                                     "created_by": acting.user if acting is not None else "service account"})
        return PurchaseOrderResult(ok=True, name=name)

    def submit_stock_transfer(self, name: str) -> None:
        """Tests and the demo: what submitting the draft in ERPNext does, the stock leaves one warehouse and arrives in the other."""
        t = next(x for x in self.stock_transfers if x["name"] == name)
        item = self.items[t["item_code"]]
        held = dict(item.by_warehouse or {MAIN_WAREHOUSE: item.stock})
        src, dst = held.get(t["from_warehouse"], StockInfo(on_hand=0.0)), held.get(t["to_warehouse"], StockInfo(on_hand=0.0))
        held[t["from_warehouse"]], held[t["to_warehouse"]] = replace(src, on_hand=src.on_hand - t["qty"]), replace(dst, on_hand=dst.on_hand + t["qty"])
        item.by_warehouse = held
        item.stock = _sum_stock(list(held.values()))
        t["docstatus"] = 1

    def healthcheck(self) -> bool:
        return self.healthy

    # ------------------------------------------------------------------ FollowUpERP
    def get_receipt_lines(self, since: date) -> list[ReceiptLine]:
        return [r for r in self.receipts if r.posting_date is None or r.posting_date >= since]

    def get_open_sales_orders(self, item_codes: Sequence[str], until: date) -> dict[str, list[SalesDue]]:
        return {c: [s for s in self.sales_orders.get(c, []) if s.qty > 0 and (s.due is None or s.due <= until)] for c in item_codes if self.sales_orders.get(c)}

    def get_supplier_contact(self, supplier: str) -> Optional[SupplierContact]:
        return self.contacts.get(supplier)

    def get_item_alternatives(self, item_code: str) -> list[str]:
        return list(self.alternatives.get(item_code, []))

    def get_stock_counts(self, since: date) -> list[CountLine]:
        return [c for c in self.counts if c.posting_date is None or c.posting_date >= since]

    def get_stock_values(self, item_codes: Sequence[str]) -> dict[str, float]:
        """The value set by a test or the demo; otherwise on hand x the usual supplier's average price, like a valuation rate."""
        out = {}
        for c in item_codes:
            if c in self.stock_values:
                out[c] = self.stock_values[c]
            elif c in self.items:
                prices = [o.avg_price or o.unit_price for o in self.vendors.get(c, []) if (o.avg_price or o.unit_price)]
                out[c] = round(max(self.items[c].stock.on_hand, 0.0) * (prices[0] if prices else 0.0), 2)
        return out

    def last_purchase_dates(self, item_codes: Sequence[str]) -> dict[str, tuple[date, str]]:
        out: dict[str, tuple[date, str]] = {}
        for r in self.receipts:
            if r.item_code in item_codes and r.posting_date and r.submitted and (r.item_code not in out or r.posting_date > out[r.item_code][0]):
                out[r.item_code] = (r.posting_date, r.supplier)
        return out

    def send_email(self, recipients: Sequence[str], subject: str, body: str, reference_doctype: str, reference_name: str) -> MessageResult:
        if self.refuse_email:
            return MessageResult(ok=False, error=self.refuse_email)
        ref = f"COMM-MOCK-{len(self.sent_emails) + 1:04d}"
        self.sent_emails.append({"to": list(recipients), "subject": subject, "body": body, "doctype": reference_doctype, "name": reference_name, "reference": ref})
        return MessageResult(ok=True, reference=ref)

    def add_open_order(self, name: str, supplier: str, item_code: str, qty: float, due: date, received: float = 0.0, rate: float = 10.0, supplier_name: str = "") -> dict:
        """A submitted purchase order made in the ERP (not by this application), for the follow-up tests and the demo."""
        po = {"name": name, "reference": "", "approved_by": "", "created_by": "buyer", "supplier": supplier_name or supplier, "supplier_id": supplier, "item_code": item_code, "qty": qty,
              "rate": rate, "required_by": due, "docstatus": 1, "status": "To Receive and Bill", "per_received": round(received / qty * 100, 2) if qty else 0.0, "received_qty": received}
        self.purchase_orders.append(po)
        return po

    # ------------------------------------------------------------------ helpers for tests/demo
    def add_item(
        self,
        code: str,
        name: str,
        fn: Callable[[int], float],
        on_hand: float,
        *,
        group: str = "General",
        uom: str = "pcs",
        lead_time_days: Optional[float] = 7,
        incoming: float = 0,
        reserved: float = 0,
        planned: float = 0,
        extra_stock: Optional[dict[str, float]] = None,      # any other StockInfo column, e.g. {"requested": 30}
        warehouses: Optional[dict[str, dict[str, float]]] = None,   # {"Stores": {"on_hand": 80, "reserved": 10}, ...}: overrides the totals
        arrives_in_days: Optional[int] = None,
        history_days: int = HISTORY_DAYS,
        created_days_ago: Optional[int] = None,
        scenario: str = "",
    ) -> MockItem:
        """fn(age_in_days) -> quantity used that many days ago (age 1 = yesterday)."""
        created = self.today - timedelta(days=created_days_ago) if created_days_ago is not None else None
        usage = []
        for age in range(1, history_days + 1):
            day = self.today - timedelta(days=age)
            if created and day < created:
                break
            qty = fn(age)
            if qty > 0:
                usage.append(DailyUsage(day, float(qty)))
        usage.sort(key=lambda u: u.day)
        arrival = self.today + timedelta(days=arrives_in_days) if arrives_in_days is not None else None
        by_wh = {name_: StockInfo(**{"on_hand": 0.0, **vals}, next_arrival=arrival) for name_, vals in (warehouses or {}).items()}
        item = MockItem(
            info=ItemInfo(code=code, name=name, group=group, uom=uom, lead_time_days=lead_time_days, created=created),
            stock=_sum_stock(list(by_wh.values())) if by_wh else
            StockInfo(on_hand=on_hand, reserved=reserved, incoming=incoming, planned=planned, next_arrival=arrival, **(extra_stock or {})),
            usage=usage,
            scenario=scenario,
            by_warehouse=by_wh,
        )
        self.items[code] = item
        return item

    def add_vendors(self, code: str, offers: list[VendorOffer]) -> None:
        self.vendors[code] = offers

    def set_stock(self, code: str, **kwargs) -> None:
        cur = self.items[code].stock
        self.items[code].stock = StockInfo(**{**cur.__dict__, **kwargs})

    # ------------------------------------------------------------------ scenarios
    def _rng(self, code: str) -> random.Random:
        return random.Random(f"{self.seed}-{code}")  # repeatable demo data, not security  # nosec B311

    def _steady(self, code: str, mean: float, sd: float) -> Callable[[int], float]:
        rng = self._rng(code)
        cache: dict[int, float] = {}

        def fn(age: int) -> float:
            if age not in cache:
                cache[age] = max(0.0, round(rng.gauss(mean, sd)))
            return cache[age]

        return fn

    def _build_scenarios(self) -> None:
        # 1. Healthy: plenty of stock, stable demand -> no alert.
        self.add_item("STEEL-2MM", "Steel Sheet 2mm", self._steady("STEEL-2MM", 10, 2), 600,
                      group="Raw Material", uom="sheets", lead_time_days=7, scenario="healthy: no alert")

        # 2. Plain threshold breach (time to order), not yet a stock-out risk.
        self.add_item("BOLT-M8", "Bolt M8 x 40", self._steady("BOLT-M8", 40, 6), 300,
                      group="Fasteners", uom="pcs", lead_time_days=5, scenario="breach (HIGH): inside lead-time + safety window")

        # 3. History-based early warning: above every threshold, but usage surged in the last 2 weeks.
        base = self._steady("PAINT-RED", 8, 1.5)
        surge = self._steady("PAINT-RED-surge", 14, 2)
        self.add_item("PAINT-RED", "Red Paint 1L", lambda age: surge(age) if age <= 14 else base(age), 230,
                      group="Paints", uom="litres", lead_time_days=7, scenario="early warning: recent usage surge")

        # 4. Seasonal early warning: same period last year ran far above its baseline.
        calm = self._steady("GASKET-X", 5, 1)
        peak = self._steady("GASKET-X-peak", 9, 1.5)
        self.add_item("GASKET-X", "Gasket Type X", lambda age: peak(age) if 351 <= age <= 365 else calm(age), 140,
                      group="Fasteners", uom="pcs", lead_time_days=7, scenario="early warning: seasonal uplift seen last year")

        # 5. Critical: stock runs out before a new order could arrive.
        self.add_item("OIL-5L", "Hydraulic Oil 5L", self._steady("OIL-5L", 3, 0.8), 18,
                      group="Consumables", uom="cans", lead_time_days=10, scenario="CRITICAL: cover shorter than lead time")

        # 6. Low stock but an open PO arrives in time -> covered, no alert.
        self.add_item("CABLE-3C", "Cable 3-core", self._steady("CABLE-3C", 12, 2), 70,
                      group="Electrical", uom="m", lead_time_days=9, incoming=400, arrives_in_days=4,
                      scenario="covered: open PO arrives before stock-out")

        # 7. Plenty on order, but it arrives AFTER stock runs out.
        self.add_item("RELAY-12V", "Relay 12V", self._steady("RELAY-12V", 12, 2), 70,
                      group="Electrical", uom="pcs", lead_time_days=9, incoming=500, arrives_in_days=12,
                      scenario="CRITICAL: open PO arrives after stock-out")

        # 8. Brand-new item: only 10 days of history -> thresholds work, predictions are skipped.
        self.add_item("FILTER-A", "Air Filter A", self._steady("FILTER-A", 6, 1), 40,
                      group="Consumables", uom="pcs", lead_time_days=6, created_days_ago=10,
                      scenario="new item: thresholds only, no prediction")

        # 9. Dead stock: no consumption at all.
        self.add_item("DEAD-Z", "Obsolete Part Z", lambda age: 0, 50,
                      group="General", uom="pcs", lead_time_days=14, scenario="no consumption: ignored")

        # 10. Lumpy demand (big orders now and then): average is fine, busy weeks are riskier.
        rng = self._rng("WELD-ROD")
        lumps = {age: (rng.choice([40, 60, 80]) if age % 6 == 0 else 0) for age in range(1, HISTORY_DAYS + 1)}
        self.add_item("WELD-ROD", "Welding Rod 3.15mm", lambda age: lumps[age], 400,
                      group="Raw Material", uom="kg", lead_time_days=6, scenario="healthy but lumpy demand")

        # Every item gets the same three kinds of supplier so the vendor selector has something to weigh:
        # a balanced one, a cheap-but-slow one and a fast-but-dearer one. Prices differ per item.
        for code in self.items:
            price = round(self._rng(f"{code}-price").uniform(2, 40), 2)
            last = self.today - timedelta(days=20)
            self.add_vendors(code, [
                VendorOffer("SUP-PRIME", "Prime Supplies", round(price, 2), round(price * 0.99, 2), 4, 0.96, 14, last, 30, True, country="India"),
                VendorOffer("SUP-BUDGET", "Budget Traders", round(price * 0.92, 2), round(price * 0.93, 2), 11, 0.78, 6, last, 15, False, country="Vietnam"),
                VendorOffer("SUP-SWIFT", "Swift Industrial", round(price * 1.10, 2), round(price * 1.09, 2), 2, 0.99, 9, last, 0, False, country="India"),
            ])
        # Two demo quotations (visible only when the owner turns on 'prices from supplier quotations'): the usual supplier has quoted a better price than its
        # last order, and a vendor never ordered from has quoted the best price of all.
        for code in ("BOLT-M8", "OIL-5L"):
            price = round(self._rng(f"{code}-price").uniform(2, 40), 2)
            offers = self.vendors[code]
            offers[0] = replace(offers[0], quote_price=round(price * 0.94, 2), quote_ref=f"SQ-DEMO-{code}-1", quote_date=self.today - timedelta(days=2),
                                quote_valid_till=self.today + timedelta(days=12), quote_qty=1000.0, quote_lead_days=4.0)
            offers.append(VendorOffer("SUP-NEWCO", "Newco Trading", None, None, None, None, 21, None, 0, False, currency="INR", country="India", quote_price=round(price * 0.84, 2),
                                      quote_ref=f"SQ-DEMO-{code}-2", quote_date=self.today - timedelta(days=1), quote_valid_till=self.today + timedelta(days=9), quote_qty=1000.0, quote_lead_days=6.0))

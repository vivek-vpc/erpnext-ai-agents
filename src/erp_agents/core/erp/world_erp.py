"""The demo ERP loaded with the contract world: the same items, shelves, sales, orders, quotation and tax rule that the loader posts to a real ERPNext site, answered in memory.

It is `MockERP` for everything that is made and changed (drafts, references, cancelling), and answers the reads from the world, so that the shared checks
(`ai_munimji_engine.testkit.contract`) can be run against it. `world_site()` gives the checks their `Site`."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional, Sequence

from ai_munimji_engine.packs.stock_procurement.erp import DailyUsage, ItemInfo, QuoteRule, SalesDue, StockInfo, TaxRow, TaxSetup, VendorOffer, WarehouseStock
from ai_munimji_engine.testkit.contract import Site
from ai_munimji_engine.testkit.world import WORLD, World, default_names

from .mock import MockERP


class WorldERP(MockERP):
    def __init__(self, world: World = WORLD, today: Optional[date] = None, names: Optional[dict[str, str]] = None) -> None:
        super().__init__(today=today or date.today(), scenarios=False)
        self.world = world
        self.names = names or default_names(world)
        self.codes = {self.names[i.key]: i.key for i in world.items}
        for o in world.orders:                       # the orders of the world are documents in the demo ERP, so that finding, changing and cancelling see them
            day = self.today - timedelta(days=o.days_ago)
            received = o.qty if o.received_after is not None else 0.0
            self.purchase_orders.append({
                "name": f"PUR-ORD-{o.key.upper()}", "reference": o.reference, "approved_by": o.approved_by, "created_by": "buyer", "supplier": world.supplier(o.supplier).name,
                "supplier_id": self.names[o.supplier], "item_code": self.names[o.item], "qty": o.qty, "rate": o.rate, "required_by": day + timedelta(days=o.due_in), "docstatus": 1,
                "status": "Closed" if o.closed else "To Receive and Bill" if o.received_after is not None else "To Receive and Bill", "per_received": 100.0 if received else 0.0,
                "received_qty": received, "date": day, "uom": "Nos"})

    # ------------------------------------------------------------------ names
    def key(self, code: str) -> Optional[str]:
        return self.codes.get(code)

    def wh_keys(self, names: Optional[Sequence[str]]) -> Optional[list[str]]:
        if not names:
            return None
        back = {v: k for k, v in self.names.items()}
        return [back[n] for n in names if n in back]

    # ------------------------------------------------------------------ StockERP
    def list_items(self) -> list[ItemInfo]:
        out = []
        for it in self.world.listed_items():
            first = self.world.first_movement(it.key, self.today)
            out.append(ItemInfo(code=self.names[it.key], name=it.name, group="Contract Goods", uom="Nos", lead_time_days=it.lead_time_days, created=first or self.today))
        return out

    def list_warehouses(self) -> list[str]:
        return sorted(self.names[k] for k in self.world.usable_warehouses())

    def get_stock(self, item_codes: Sequence[str], warehouses: Optional[Sequence[str]] = None) -> dict[str, StockInfo]:
        keys = self.wh_keys(warehouses)
        out: dict[str, StockInfo] = {}
        for code in item_codes:
            k = self.key(code)
            if k is None or not any(i.key == k and i.stock_item and not i.disabled for i in self.world.items):
                out[code] = StockInfo(on_hand=0.0)
                continue
            s = self.world.stock(k, keys)
            places = tuple(sorted((WarehouseStock(self.names[w], on_hand, self.world.reserved(k, w), self.world.incoming(k, w), self.world.warehouse(w).kind == "transit")
                                   for w, on_hand in s["places"].items()), key=lambda p: (-p.on_hand, p.warehouse)))
            out[code] = StockInfo(on_hand=s["on_hand"], reserved=s["reserved"], incoming=s["incoming"], in_transit=s["in_transit"], by_warehouse=places,
                                  next_arrival=self.world.next_arrival(k, self.today, keys) if s["incoming"] else None)
        return out

    def get_consumption(self, item_codes: Sequence[str], start: date, end: date, warehouses: Optional[Sequence[str]] = None) -> dict[str, list[DailyUsage]]:
        keys = self.wh_keys(warehouses)
        out = {}
        for code in item_codes:
            k = self.key(code)
            used = self.world.usage(k, self.today, start, end, keys) if k else {}
            if used:
                out[code] = [DailyUsage(d, q) for d, q in used.items()]
        return out

    def get_open_sales_orders(self, item_codes: Sequence[str], until: date) -> dict[str, list[SalesDue]]:
        out = {}
        for code in item_codes:
            k = self.key(code)
            rows = [SalesDue(code, d, q) for d, q in self.world.open_demand(k, self.today, until)] if k else []
            if rows:
                out[code] = rows
        return out

    # ------------------------------------------------------------------ PurchasingERP
    def get_vendor_offers(self, item_code: str, since: date, quotes: Optional[QuoteRule] = None) -> list[VendorOffer]:
        k = self.key(item_code)
        if k is None:
            return []
        rule = quotes
        found = self.world.offers(k, self.today, since, quote_rule_today=rule.today if rule else None, max_age_days=rule.max_age_days if rule else 30)
        mine = [o for o in self.world.orders if o.item == k and self.today - timedelta(days=o.days_ago) >= since]
        out = []
        for sk, v in found.items():
            history = sorted((o for o in mine if o.supplier == sk), key=lambda o: -o.days_ago)
            out.append(VendorOffer(supplier=self.names[sk], name=v["name"], unit_price=v["unit_price"], avg_price=v["avg_price"], lead_time_days=v["lead_time_days"], on_time_rate=v["on_time_rate"],
                                   orders=v["orders"], last_order=self.today - timedelta(days=history[-1].days_ago) if history else None, payment_days=v["payment_days"], listed=v["listed"],
                                   quote_price=v["quote_price"], quote_qty=v["quote_qty"], quote_valid_till=v["quote_valid_till"]))
        return out

    def item_min_order_qty(self, item_code: str) -> Optional[float]:
        k = self.key(item_code)
        return self.world.item(k).min_order_qty if k else None

    def get_purchase_taxes(self, supplier: str, item_code: str, posting_date: date) -> TaxSetup:
        for rule in self.world.tax_rules:
            if self.names[rule.supplier] == supplier:
                rows = tuple(TaxRow(description=r.description, charge_type=r.charge_type, rate=r.rate, category=r.category, add_deduct=r.add_deduct) for r in rule.rows)
                return TaxSetup(template=self.names[rule.template], source="tax rule", rows=rows)
        return TaxSetup(template=None, source="none", notes=("no Tax Rule matched this vendor",))


def world_site(world: World = WORLD, today: Optional[date] = None) -> tuple[WorldERP, Site]:
    """A demo ERP holding the world, and the `Site` the checks need to talk to it (submit and discard act on the demo ERP's own lists)."""
    erp = WorldERP(world, today)

    def submit(doctype: str, name: str) -> None:
        doc = next(d for d in erp.purchase_orders if d["name"] == name)
        doc["docstatus"], doc["status"] = 1, "To Receive and Bill"

    def discard(doctype: str, name: str) -> None:
        lists = {"Purchase Order": erp.purchase_orders, "Material Request": erp.requisitions, "Stock Entry": erp.stock_transfers}[doctype]
        lists[:] = [d for d in lists if d["name"] != name]

    return erp, Site(today=erp.today, names=dict(erp.names), submit=submit, discard=discard, world=world)

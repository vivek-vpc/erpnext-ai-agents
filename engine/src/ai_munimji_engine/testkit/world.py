"""The dataset the shared ERP-client checks run on, written once in neutral words, with what every read of it must return worked out from the documents themselves.

A driver gives a client this world: the demo ERP builds it in memory (`MockERP.from_world`), the loader of `tools/testdata` posts it to an ERPNext site (`python -m tools.testdata.cli
contract`). The checks (`contract.py`) then ask the client and compare with the answers computed here, so a client is judged against the documents, not against another client.

Everything is a *logical key* ("steady", "wh_a"): the site's real names (a warehouse carries the company abbreviation in ERPNext) are given by the driver in a `Site`. Dates are days
before a reference day, the day the world was loaded; the checks read the reference day from the `Site`, never from the clock.

No third-party import, no I/O: it runs wherever the engine runs, including in a Frappe bench.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional


@dataclass(frozen=True)
class Warehouse:
    key: str
    kind: str = "normal"                     # normal | transit | group | disabled


@dataclass(frozen=True)
class Item:
    key: str
    name: str
    stock_item: bool = True
    disabled: bool = False
    lead_time_days: Optional[float] = None
    min_order_qty: Optional[float] = None
    suppliers: tuple[str, ...] = ()          # listed on the item record


@dataclass(frozen=True)
class Supplier:
    key: str
    name: str
    payment_days: Optional[int] = None       # credit days of its payment terms
    disabled: bool = False


@dataclass(frozen=True)
class Move:
    """Stock put on a shelf (ERPNext: a Stock Entry of type Material Receipt)."""
    item: str
    warehouse: str
    qty: float
    days_ago: int
    rate: float = 10.0


@dataclass(frozen=True)
class Sale:
    """Stock sent to a customer (ERPNext: a Delivery Note). This is what usage is measured from."""
    item: str
    warehouse: str
    qty: float
    days_ago: int


@dataclass(frozen=True)
class Order:
    """A submitted purchase order. `received_after` = days after the order date that goods arrived in full (None = still open)."""
    key: str
    supplier: str
    item: str
    warehouse: str
    qty: float
    rate: float
    days_ago: int
    due_in: int                              # the line's required-by date is this many days after the order date
    received_after: Optional[int] = None
    reference: str = ""                      # made by this application for a request (custom_agent_ref); "" = made by hand
    approved_by: str = ""
    closed: bool = False                     # closed in the ERP after it was received: a finished order


@dataclass(frozen=True)
class Quote:
    """A submitted supplier quotation."""
    supplier: str
    item: str
    rate: float
    qty: float
    days_ago: int
    valid_for: Optional[int]                 # days from its date; None = no end date


@dataclass(frozen=True)
class SalesOrder:
    """A submitted sales order not yet delivered: it reserves stock and is demand to come."""
    item: str
    warehouse: str
    qty: float
    due_in: int                              # days after the reference day


@dataclass(frozen=True)
class TaxRow:
    description: str
    charge_type: str = "On Net Total"
    rate: float = 0.0
    category: str = "Total"
    add_deduct: str = "Add"


@dataclass(frozen=True)
class TaxRule:
    """Purchases from `supplier` use the purchase taxes template `template` with `rows`."""
    supplier: str
    template: str
    rows: tuple[TaxRow, ...]


@dataclass(frozen=True)
class World:
    warehouses: tuple[Warehouse, ...]
    items: tuple[Item, ...]
    suppliers: tuple[Supplier, ...]
    moves: tuple[Move, ...]
    sales: tuple[Sale, ...]
    orders: tuple[Order, ...]
    quotes: tuple[Quote, ...]
    sales_orders: tuple[SalesOrder, ...]
    tax_rules: tuple[TaxRule, ...]

    # ---------------------------------------------------------------- lookups
    def item(self, key: str) -> Item:
        return next(i for i in self.items if i.key == key)

    def supplier(self, key: str) -> Supplier:
        return next(s for s in self.suppliers if s.key == key)

    def warehouse(self, key: str) -> Warehouse:
        return next(w for w in self.warehouses if w.key == key)

    def usable_warehouses(self) -> list[str]:
        """The keys of the warehouses that can hold stock: not a group, not disabled."""
        return sorted(w.key for w in self.warehouses if w.kind in ("normal", "transit"))

    def listed_items(self) -> list[Item]:
        """What `list_items` must return: stock items that are not disabled."""
        return [i for i in self.items if i.stock_item and not i.disabled]

    # ---------------------------------------------------------------- the world has to be one a real ERP can hold
    def problems(self) -> list[str]:
        """What is wrong with the world itself (empty if nothing): a name that is not defined, or a shelf that would go below zero on some day, which ERPNext refuses to post."""
        out: list[str] = []
        items, whs, sups = {i.key for i in self.items}, {w.key for w in self.warehouses}, {s.key for s in self.suppliers}
        keys = [*(i.key for i in self.items), *(w.key for w in self.warehouses), *(s.key for s in self.suppliers), *(r.template for r in self.tax_rules)]
        if len(set(keys)) != len(keys):
            out.append("a key is used for two things: the checks give every part one flat name")
        for what, rows in (("a move", self.moves), ("a sale", self.sales), ("an order", self.orders), ("a sales order", self.sales_orders)):
            for r in rows:
                if r.item not in items or r.warehouse not in whs:
                    out.append(f"{what} names an item or warehouse that is not in the world: {r}")
        for o in self.orders:
            if o.supplier not in sups:
                out.append(f"order {o.key} names an unknown supplier")
            if o.received_after is not None and not 0 <= o.received_after <= o.days_ago:
                out.append(f"order {o.key} is received before it is made or after today")
        for q in self.quotes:
            if q.supplier not in sups or q.item not in items:
                out.append(f"a quotation names an unknown supplier or item: {q}")
        ledger: dict[tuple[str, str], list[tuple[int, int, float]]] = {}
        for item in items:
            for w, days_ago, qty in self._received(item):
                ledger.setdefault((item, w), []).append((days_ago, 0, qty))              # receipts before sales on the same day
            for s in self.sales:
                if s.item == item:
                    ledger.setdefault((item, s.warehouse), []).append((s.days_ago, 1, -s.qty))
        for (item, w), events in ledger.items():
            balance = 0.0
            for days_ago, _order, qty in sorted(events, key=lambda e: (-e[0], e[1])):
                balance += qty
                if balance < 0:
                    out.append(f"{item} in {w} would be below zero {days_ago} days ago")
        return out

    # ---------------------------------------------------------------- stock
    def _received(self, item: str) -> list[tuple[str, int, float]]:
        """(warehouse, days_ago, qty) of everything that arrived: shelf receipts and purchase orders that were received."""
        out = [(m.warehouse, m.days_ago, m.qty) for m in self.moves if m.item == item]
        out += [(o.warehouse, o.days_ago - o.received_after, o.qty) for o in self.orders if o.item == item and o.received_after is not None]
        return out

    def on_hand(self, item: str, warehouse: str) -> float:
        return sum(q for w, _, q in self._received(item) if w == warehouse) - sum(s.qty for s in self.sales if s.item == item and s.warehouse == warehouse)

    def incoming(self, item: str, warehouse: str) -> float:
        """Ordered and not yet received (ERPNext: ordered_qty)."""
        return sum(o.qty for o in self.orders if o.item == item and o.warehouse == warehouse and o.received_after is None)

    def reserved(self, item: str, warehouse: str) -> float:
        return sum(s.qty for s in self.sales_orders if s.item == item and s.warehouse == warehouse)

    def stock(self, item: str, warehouses: Optional[list[str]] = None) -> dict[str, float]:
        """What `get_stock` must say for an item over `warehouses` (None = all): on_hand and in_transit (apart), reserved, incoming, and each shelf's on_hand."""
        use = [w.key for w in self.warehouses if w.kind in ("normal", "transit") and (not warehouses or w.key in warehouses)]
        normal = [k for k in use if self.warehouse(k).kind == "normal"]
        transit = [k for k in use if self.warehouse(k).kind == "transit"]
        return {
            "on_hand": sum(self.on_hand(item, k) for k in normal),
            "in_transit": sum(self.on_hand(item, k) for k in transit),
            "reserved": sum(self.reserved(item, k) for k in use),
            "incoming": sum(self.incoming(item, k) for k in use),
            "places": {k: self.on_hand(item, k) for k in use if self.on_hand(item, k) or self.reserved(item, k) or self.incoming(item, k)},
        }

    def first_movement(self, item: str, today: date) -> Optional[date]:
        """The day of the item's first stock ledger entry, or None if it has none."""
        days = [d for _, d, _ in self._received(item)]
        return today - timedelta(days=max(days)) if days else None

    def next_arrival(self, item: str, today: date, warehouses: Optional[list[str]] = None) -> Optional[date]:
        due = [today - timedelta(days=o.days_ago) + timedelta(days=o.due_in) for o in self.orders
               if o.item == item and o.received_after is None and (not warehouses or o.warehouse in warehouses)]
        return min(due) if due else None

    # ---------------------------------------------------------------- usage
    def usage(self, item: str, today: date, start: date, end: date, warehouses: Optional[list[str]] = None) -> dict[date, float]:
        out: dict[date, float] = {}
        for s in self.sales:
            day = today - timedelta(days=s.days_ago)
            if s.item == item and start <= day <= end and (not warehouses or s.warehouse in warehouses):
                out[day] = out.get(day, 0.0) + s.qty
        return dict(sorted(out.items()))

    def open_demand(self, item: str, today: date, until: date) -> list[tuple[date, float]]:
        return sorted((today + timedelta(days=s.due_in), s.qty) for s in self.sales_orders if s.item == item and today + timedelta(days=s.due_in) <= until)

    # ---------------------------------------------------------------- vendors
    def offers(self, item: str, today: date, since: date, quote_rule_today: Optional[date] = None, max_age_days: int = 30) -> dict[str, dict]:
        """What `get_vendor_offers` must return for an item, per supplier key. Prices are in company currency. A supplier appears when it is on the item's list, has an order
        since `since`, or (with a quote rule) a valid quotation; a disabled supplier never does."""
        it = self.item(item)
        mine = [o for o in self.orders if o.item == item and today - timedelta(days=o.days_ago) >= since]
        quoted = self.valid_quotes(item, quote_rule_today, max_age_days, today) if quote_rule_today is not None else {}
        keys = sorted({o.supplier for o in mine} | set(it.suppliers) | set(quoted))
        out: dict[str, dict] = {}
        for k in keys:
            sup = self.supplier(k)
            if sup.disabled:
                continue
            history = sorted((o for o in mine if o.supplier == k), key=lambda o: (o.days_ago * -1, o.key))
            rates = [o.rate for o in history]
            got = [o for o in history if o.received_after is not None]
            q = quoted.get(k)
            out[k] = {
                "name": sup.name,
                "orders": len(history),
                "unit_price": history[-1].rate if history else None,
                "avg_price": sum(rates) / len(rates) if rates else None,
                "lead_time_days": sum(o.received_after for o in got) / len(got) if got else None,
                "on_time_rate": sum(1.0 if o.received_after <= o.due_in else 0.0 for o in got) / len(got) if got else None,
                "payment_days": float(sup.payment_days) if sup.payment_days is not None else None,
                "listed": k in it.suppliers,
                "quote_price": q.rate if q else None,
                "quote_qty": q.qty if q else None,
                "quote_valid_till": (today - timedelta(days=q.days_ago) + timedelta(days=q.valid_for)) if q and q.valid_for is not None else None,
            }
        return out

    def valid_quotes(self, item: str, rule_day: date, max_age_days: int, reference_day: date) -> dict[str, Quote]:
        """The latest valid quotation per supplier on `rule_day`: still in date, or (with no end date) not older than `max_age_days`."""
        best: dict[str, Quote] = {}
        for q in self.quotes:
            if q.item != item:
                continue
            made = reference_day - timedelta(days=q.days_ago)
            if q.valid_for is not None:
                if made + timedelta(days=q.valid_for) < rule_day:
                    continue
            elif (rule_day - made).days > max_age_days:
                continue
            if q.supplier not in best or q.days_ago < best[q.supplier].days_ago:
                best[q.supplier] = q
        return best


def default_names(world: World) -> dict[str, str]:
    """The names the demo ERP gives the world's parts, and the base of the names a loader gives them on an ERPNext site (which then adds the company's abbreviation to a warehouse
    and a template). One flat mapping: the logical keys of the world are all different."""
    names: dict[str, str] = {}
    for w in world.warehouses:
        names[w.key] = "Contract " + w.key.removeprefix("wh_").upper()
    for i in world.items:
        names[i.key] = "CT-" + i.key.upper()
    for s in world.suppliers:
        names[s.key] = s.name
    for rule in world.tax_rules:
        names[rule.template] = "Contract " + rule.template.capitalize()
    return names


# =========================================================================================================== THE WORLD
# Reading guide. STEADY has two shelves, 100 days of history, a sale every fifth day, four orders (two from the reliable supplier, one of them late; one from a supplier that is
# punctual but never gave a reference; one from a supplier that was disabled afterwards), a listed supplier that was never ordered from, a valid quotation and an expired one, and
# an open sales order. LOW is short, with an order on its way. MOQ has a minimum order quantity. MOVING has stock in a transit warehouse. NEW has no history. OFF is disabled and
# SERVICE is not a stock item: neither is ever listed. WRITES is what the write checks use.
WORLD = World(
    warehouses=(Warehouse("wh_a"), Warehouse("wh_b"), Warehouse("wh_t", "transit"), Warehouse("wh_g", "group"), Warehouse("wh_x", "disabled")),
    items=(
        Item("steady", "Contract steady item", lead_time_days=9, suppliers=("s1", "s3")),
        Item("low", "Contract low item", suppliers=("s1",)),
        Item("moq", "Contract minimum-order item", min_order_qty=25),
        Item("new", "Contract item with no history"),
        Item("moving", "Contract item in transit"),
        Item("off", "Contract disabled item", disabled=True),
        Item("service", "Contract service", stock_item=False),
        Item("writes", "Contract item the write checks use"),         # no history and no order: the documents the write checks make (and cancel) never touch what the read checks count
    ),
    suppliers=(
        Supplier("s1", "Contract reliable supplier", payment_days=30),
        Supplier("s2", "Contract late supplier"),
        Supplier("s3", "Contract quoting supplier"),
        Supplier("s4", "Contract disabled supplier", disabled=True),
    ),
    moves=(
        Move("steady", "wh_a", 200, 100, 12.0), Move("steady", "wh_b", 40, 100, 12.0),
        Move("low", "wh_a", 12, 100, 8.0),
        Move("moq", "wh_a", 5, 100, 20.0),
        Move("moving", "wh_t", 7, 50, 5.0), Move("moving", "wh_a", 3, 50, 5.0),
    ),
    sales=tuple(Sale("steady", "wh_a", 10, d) for d in range(5, 65, 5)) + (Sale("steady", "wh_b", 2, 15), Sale("low", "wh_a", 4, 5)),
    orders=(
        Order("o1", "s1", "steady", "wh_a", 100, 12.0, 80, 10, received_after=7),
        Order("o2", "s1", "steady", "wh_a", 50, 14.0, 40, 10, received_after=12, reference="CT-REF-O2", approved_by="ops@example.com via desk"),
        Order("o3", "s2", "steady", "wh_a", 60, 11.0, 30, 5, received_after=4, closed=True),
        Order("o4", "s1", "low", "wh_a", 30, 8.0, 3, 5),
        Order("o6", "s4", "steady", "wh_a", 10, 9.0, 20, 5, received_after=3),
    ),
    quotes=(
        Quote("s3", "steady", 10.5, 100, 6, 20),
        Quote("s2", "steady", 9.0, 100, 40, 10),                       # expired 30 days ago
    ),
    sales_orders=(SalesOrder("steady", "wh_a", 30, 10),),
    tax_rules=(TaxRule("s1", "duty", (TaxRow("Import duty", "On Net Total", 5.0, "Valuation and Total", "Add"),)),),
)

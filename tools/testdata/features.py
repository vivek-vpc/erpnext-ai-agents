"""Feature scenarios: materials, vendors and documents posted into ERPNext so that every purchasing follow-up and stock-intelligence feature of the app has something to show.

Each scenario has its own materials (codes TST-...) and, where a vendor is involved, four test vendors (TST Alpha / Beta / Gamma / Delta). Nothing that exists is changed.
The documents are real ERPNext documents with back-dated posting dates, in the same flows as the rest of the test data (purchase order -> receipt -> invoice -> payment; delivery
notes for the sales the app measures usage from; a sales order; stock counts; item alternatives). Dates are relative to the day of the first run (kept in the state file),
so "late by 6 days" stays late by 6 days relative to that day.

  scenario   what it lets you test
  late       delivery chasing: late purchase orders, with stock that is short, plentiful, and a vendor with no contact
  grn        goods receipt check: more than ordered, rejected goods, a dearer receipt, a dearer invoice (unpaid and paid), receipts that match
  scorecard  vendor scorecard: a reliable vendor, an unreliable one with rising prices and rejections, and one with a single delivery
  rfq        quote requests: four vendors with different contacts (e-mail, WhatsApp only, none)
  seasonal   demand forecast: last year's festive jump for the coming weeks, and a big sales order not yet delivered
  slow       slow and dead stock, with every suggested action
  subs       substitute items: short with approved alternatives, short without, healthy with
  counts     stock count assistant: differences explained by a draft receipt, a unit mix-up, a recurring surplus, nothing; and a count plan

Idempotent and resumable: every document carries a marker (TD/F1-<key>) so a second run finds what exists; the state file keeps those without a marker field. Everything
created is listed in manifest.jsonl.

    python -m tools.testdata.cli features plan                 what would be created, and every material's stock position (reads nothing, writes nothing)
    python -m tools.testdata.cli features run [--only late,grn]    create it
    python -m tools.testdata.cli features report               every material's stock position as ERPNext holds it now
    python -m tools.testdata.cli features quote-replies --item TST-RFQ-1     post the vendors' supplier quotations (after you pressed Send requests)
    python -m tools.testdata.cli features check                the app's own tools, read-only, on what is now in ERPNext
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from . import catalog, steps, world
from .days import QUALITY_HOLD, STORES, Days, MARKERS, whole
from .post import MARK
from .steps import Context
from .writer import WriterError

log = logging.getLogger("testdata")
PFX = "F1-"
STATE = Path(__file__).resolve().parent / "features_state.json"
MAKE_PR = "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt"
MAKE_PI = "erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice"
COVER_DAYS = 45                                   # a material with no stock figure of its own ends with about this many days of its sales on the shelf

# ---------------------------------------------------------------------------------------------------------------------- vendors
V = world.Vendor
ALL_GROUPS = ("FAS", "MET", "ELE", "MEC", "CHM", "PKG", "FGI", "FGC")
VENDORS: dict[str, world.Vendor] = {
    "alpha": V("TST Alpha Industrial Supplies Pvt Ltd", "India", "Pune", "Maharashtra", supplies=ALL_GROUPS),
    "beta": V("TST Beta Traders", "India", "Ahmedabad", "Gujarat", supplies=ALL_GROUPS, legal="Partnership"),
    "gamma": V("TST Gamma Wholesale", "India", "Mumbai", "Maharashtra", supplies=ALL_GROUPS),
    "delta": V("TST Delta Hardware & Tools", "India", "Chennai", "Tamil Nadu", supplies=ALL_GROUPS),
}
# The customers of the test sales. Their own, with a credit limit nobody reaches: ERPNext refuses a delivery note to a customer over its limit, and these sales are only there to
# give the app consumption to measure (an earlier run spread them over existing customers, pushed one over its limit, and used up part of every one's credit).
TEST_CUSTOMERS = [
    world.Customer("TST Retail Customer One", "India", "Pune", "Maharashtra", "retail", credit_limit=1_000_000_000),
    world.Customer("TST Industrial Customer Two", "India", "Ahmedabad", "Gujarat", "industrial", credit_limit=1_000_000_000),
    world.Customer("TST Distributor Three", "India", "Nashik", "Maharashtra", "distributor", credit_limit=1_000_000_000),
]
PRICE_FACTOR = {"alpha": 1.00, "beta": 0.96, "gamma": 1.03, "delta": 1.05}
# How each vendor can be reached, as the supplier's contact in ERPNext. Placeholders that can never deliver: replace them with your own e-mail address and WhatsApp number
# (Contact -> the contact of that supplier) to receive the messages. (name, e-mail, mobile)
CONTACTS: dict[str, Optional[tuple[str, str, str]]] = {
    "alpha": ("Alpha Orders Desk", "alpha.orders@example.com", "+91 00000 00001"),       # e-mail and WhatsApp
    "beta": ("Beta Sales", "beta.sales@example.com", ""),                                # e-mail only
    "gamma": None,                                                                       # no contact at all
    "delta": ("Delta Counter", "", "+91 00000 00002"),                                   # WhatsApp only
}


@dataclass(frozen=True)
class Mat:
    code: str
    name: str
    scenario: str
    group: str                 # catalogue group code
    product: str               # catalogue product (gives the unit, the customs and GST data and the price)
    vendors: tuple[str, ...] = ("alpha",)
    lead: int = 7              # days; the item's lead time
    use: float = 0.0           # units sold per day, as weekly delivery notes over the last `weeks` weeks
    weeks: int = 13
    sell_end: int = 1          # days ago of the most recent delivery note
    stock: Optional[float] = None      # on hand today; None = about COVER_DAYS of sales
    price_factor: float = 1.0
    over_receipt_pct: float = 0.0
    over_billing_pct: float = 0.0     # ERPNext refuses an invoice above the order unless the item allows it
    alt: bool = False          # an alternative may be listed for it in ERPNext
    note: str = ""             # what the scenario should show


def mats() -> tuple[Mat, ...]:
    M = Mat
    return (
        # ---------------------------------------------------------------- late: delivery chasing
        M("TST-LATE-1", "Hydraulic Oil ISO 68 (late delivery, running short)", "late", "CHM", "Hydraulic Oil", ("alpha",), use=25, stock=140,
          note="PO due 6 days ago, 800 of 1,000 L still to come; stock lasts about 5-6 days, less than a new order would take"),
        M("TST-LATE-2", "Hex Bolt SS304 M10x50 (late delivery, plenty of stock)", "late", "FAS", "Hex Bolt SS304", ("beta",), use=100, stock=6000,
          note="PO 4 days late for 5,000; stock lasts about 60 days, so the delay is no risk. Two more POs are not late (one within the grace day, one not yet due)"),
        M("TST-LATE-3", "Bubble Wrap Roll 50m (late delivery, vendor has no contact)", "late", "PKG", "Bubble Wrap Roll", ("gamma",), use=4, stock=45,
          note="PO 3 days late; the vendor has no e-mail or mobile in ERPNext, so no reminder can be sent"),
        # ---------------------------------------------------------------- grn: goods receipt check
        M("TST-GRN-1", "Spur Gear Steel 20T (received more than ordered)", "grn", "MEC", "Spur Gear Steel", ("alpha",), use=8, over_receipt_pct=20,
          note="ordered 400, 460 received"),
        M("TST-GRN-2", "Welding Electrode E6013 (part of the delivery rejected)", "grn", "CHM", "Welding Electrode E6013", ("beta",), use=15,
          note="ordered 500 kg, all received but 75 kg rejected"),
        M("TST-GRN-3", "Pneumatic Cylinder 63mm (receipt dearer than the order)", "grn", "MEC", "Pneumatic Cylinder", ("alpha",), use=2,
          note="ordered at the vendor's price, received at 8% more"),
        M("TST-GRN-4", "Proximity Sensor M18 (invoice dearer than the order, unpaid)", "grn", "ELE", "Proximity Sensor", ("delta",), use=4, over_billing_pct=15,
          note="receipt matches; the vendor's invoice is about 7% above the order and not yet paid"),
        M("TST-GRN-5", "Control Panel PCB (invoice dearer than the order, already paid)", "grn", "ELE", "Control Panel PCB", ("beta",), use=3, over_billing_pct=15,
          note="receipt matches; the invoice is 5% above the order and has been paid: ask for a credit note"),
        M("TST-GRN-6", "Industrial Adhesive 1kg (partial deliveries that match)", "grn", "CHM", "Industrial Adhesive", ("alpha",), use=10,
          note="ordered 300, received 100 and then 200: nothing to flag"),
        # ---------------------------------------------------------------- scorecard
        M("TST-SC-1", "Brass Bar 25mm (vendor scorecard history)", "scorecard", "MET", "Brass Bar", ("alpha", "beta", "gamma"), use=14, weeks=22, stock=450,
          note="12 purchase orders over 5 months: Alpha always on time at a steady price; Beta late 4 times out of 6, prices up about 14%, one delivery 10% rejected; Gamma one delivery"),
        # ---------------------------------------------------------------- rfq
        M("TST-RFQ-1", "Industrial Adhesive 5kg (ask vendors for prices)", "rfq", "CHM", "Industrial Adhesive", ("alpha", "beta", "gamma", "delta"), use=20, stock=130, price_factor=5.0,
          note="short on stock, so a quantity is suggested; Alpha (e-mail and WhatsApp), Beta (e-mail), Delta (WhatsApp only) can be asked; Gamma has no contact"),
        # ---------------------------------------------------------------- seasonal
        M("TST-SEAS-1", "Corrugated Carton 5-ply 300x200 (festive season jump)", "seasonal", "PKG", "Corrugated Carton 5-ply", ("alpha",), lead=9, use=120, stock=2500,
          note="15 months of sales; last year the same weeks ran 1.8 times the normal rate; the plain rule sees 20 days of cover, the forecast says order now"),
        M("TST-SEAS-2", "Hand Tool Set 120pc (big sales order not yet delivered)", "seasonal", "FGC", "Hand Tool Set 120pc", ("beta",), use=6, stock=85,
          note="a submitted sales order for 90 due in 6 days; stock 85. By default ERPNext reserves it (all stock reserved); clear 'reserved for sales orders' in this item's stock settings to see the forecast count it"),
        # ---------------------------------------------------------------- slow and dead
        M("TST-DEAD-1", "Industrial Solvent Thinner (dead stock)", "slow", "CHM", "Industrial Solvent Thinner", ("alpha",), use=5, weeks=16, sell_end=200, stock=2000,
          note="last sold 200 days ago; nothing on order; about 2.6 lakh tied up (enough to rank in the weekly list): discount or clear"),
        M("TST-DEAD-2", "Cutting Fluid Soluble (bought recently, never used)", "slow", "CHM", "Cutting Fluid Soluble", ("alpha",), stock=1500,
          note="received 24 days ago (two deliveries) and never sold: ask the vendor to take it back"),
        M("TST-SLOW-1", "Safety Helmet (slow mover)", "slow", "FGC", "Safety Helmet", ("beta",), use=1, stock=900,
          note="sells 1 a day, 900 on the shelf: stop reordering"),
        M("TST-SLOW-2", "Self-adhesive Labels 50x25 (slow, more still on order)", "slow", "PKG", "Self-adhesive Labels", ("beta",), use=5, stock=2000,
          note="400 days of stock and 3,000 more on order: cut the open order"),
        # ---------------------------------------------------------------- subs
        M("TST-SUB-A1", "Industrial Relay 24V 40A, brand X (short)", "subs", "ELE", "Industrial Relay 24V", ("alpha",), use=20, stock=30, alt=True,
          note="about 1.5 days of stock, a new order takes 7: the alert names the approved alternative that is in stock"),
        M("TST-SUB-A2", "Industrial Relay 24V 40A, brand Y (approved alternative)", "subs", "ELE", "Industrial Relay 24V", ("alpha",), use=5, stock=800, alt=True,
          note="the alternative with stock"),
        M("TST-SUB-A3", "Industrial Relay 24V 40A, brand Z (approved alternative, out of stock)", "subs", "ELE", "Industrial Relay 24V", ("alpha",), use=5, stock=0, alt=True,
          note="listed as an alternative but out of stock itself"),
        M("TST-SUB-B1", "Spiral Wound Gasket 4in (short, no alternative listed)", "subs", "MEC", "Spiral Wound Gasket", ("delta",), use=10, stock=20,
          note="short, but ERPNext lists no alternative: nothing is suggested"),
        M("TST-SUB-C1", "Deep Groove Ball Bearing 6204 (healthy, has an alternative)", "subs", "MEC", "Deep Groove Ball Bearing", ("alpha",), use=10, stock=900, alt=True,
          note="plenty of stock: no alternative is suggested"),
        M("TST-SUB-C2", "Deep Groove Ball Bearing 6204, brand Y (alternative)", "subs", "MEC", "Deep Groove Ball Bearing", ("alpha",), use=3, stock=300, alt=True,
          note="the alternative of C1"),
        # ---------------------------------------------------------------- counts
        M("TST-CNT-1", "Spiral Wound Gasket 6in (recurring surplus, receipt not posted)", "counts", "MEC", "Spiral Wound Gasket", ("alpha",), use=6,
          note="counted 40 more than the system yesterday, and 15 more 45 days ago; a goods receipt for 40 is still a draft"),
        M("TST-CNT-2", "Pneumatic Cylinder 40mm (counted in boxes)", "counts", "MEC", "Pneumatic Cylinder", ("alpha",), use=3, stock=20,
          note="system had 240, counted 20: exactly 12 times less, probably boxes of 12"),
        M("TST-CNT-3", "Proximity Sensor M12 (shortage nothing explains)", "counts", "ELE", "Proximity Sensor", ("delta",), use=4, stock=70,
          note="system had 90, counted 70, no draft receipt, nothing in transit: recount"),
        M("TST-CNT-4", "Air Compressor 2HP (high value, never counted)", "counts", "FGI", "Air Compressor", ("alpha",), use=2, stock=40,
          note="high yearly usage value (class A) and never counted: first on the count plan"),
        M("TST-CNT-5", "Control Panel Assembly (high value, last counted 300 days ago)", "counts", "FGI", "Control Panel Assembly", ("alpha",), use=2, stock=40,
          note="class A, last counted 300 days ago: far overdue"),
    )


def make_item(m: Mat) -> catalog.Item:
    prod = next(p for p in catalog.PRODUCTS[m.group] if p.name == m.product)
    gname, margin = catalog.GROUPS[m.group]
    base = round(prod.price * m.price_factor, 2 if prod.price * m.price_factor < 100 else 0)
    sources = [(VENDORS[k].name, round(base * PRICE_FACTOR[k], 2), m.lead + i) for i, k in enumerate(m.vendors)]
    return catalog.Item(code=m.code, name=m.name, group_code=m.group, group=gname, uom=prod.uom, kg=prod.kg, hs=prod.hs, hs_desc=prod.hs_desc, gst=prod.gst, bcd_mfn=prod.bcd_mfn,
                        bcd_fta=prod.bcd_fta, add_cn=prod.add_cn, base_price=base, margin=margin, suppliers=sources, moq=1, country_of_origin="India")


# ---------------------------------------------------------------------------------------------------------------------- the plan
@dataclass
class Ev:
    day: date
    hh: int
    delta: float               # change in stock on hand (0 for documents that do not move stock)
    kind: str                  # the ERPNext document type, for counting
    label: str
    run: Callable[[], Any]
    draft: bool = False


class PlanError(Exception):
    pass


class Features(Days):
    def __init__(self, ctx: Context, today: date, only: Optional[set[str]] = None) -> None:
        super().__init__(ctx, today - timedelta(days=1), today, seed="features-v1")
        self.T = today
        self.state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
        self.only = None
        self.scenarios = only
        self.mats = {m.code: m for m in mats() if not only or m.scenario in only}
        self.items.update({code: make_item(m) for code, m in self.mats.items()})
        self.vendors.update({v.name: v for v in VENDORS.values()})
        self.customers.update({c.name: c for c in TEST_CUSTOMERS})
        self.evs: dict[str, list[Ev]] = {code: [] for code in self.mats}
        self.build()

    # ------------------------------------------------------------------ bookkeeping (own prefix and state file)
    def save(self) -> None:
        STATE.write_text(json.dumps(self.state, indent=1), encoding="utf-8")

    def load_names(self) -> int:
        n = 0
        for dt, field_name in MARKERS.items():
            for r in self.w.find(dt, [[field_name, "like", f"{MARK}{PFX}%"], ["docstatus", "<", 2]], ["name", field_name, "docstatus"], limit=100000):
                key = r[field_name][len(MARK):].split(" ", 1)[0].split(":", 1)[0]
                self.names[key], self.doctype_of[key] = r["name"], dt
                if r["docstatus"] == 0:
                    self.draft.add(key)
                n += 1
        for key, val in list(self.state.items()):
            if key.startswith("_"):
                continue
            dt, name = val
            if key not in self.names and self.w.exists(dt, name) and self.w.get(dt, name).get("docstatus") != 2:
                self.names[key], self.doctype_of[key] = name, dt
                n += 1
        return n

    def ago(self, days: int) -> date:
        return self.T - timedelta(days=days)

    def add(self, code: str, day: date, hh: int, delta: float, kind: str, label: str, run: Callable[[], Any], draft: bool = False) -> None:
        self.evs[code].append(Ev(day, hh, float(delta), kind, label, run, draft))

    def key(self, code: str, label: str) -> str:
        return f"{PFX}{code}-{label}"

    def rate_for(self, code: str, vkey: str) -> float:
        return round(self.items[code].base_price * PRICE_FACTOR[vkey], 2)

    def customer(self, *, currency: Optional[str] = None, india: Optional[bool] = None, nth: int = 0) -> world.Customer:
        return TEST_CUSTOMERS[nth % len(TEST_CUSTOMERS)]

    # ------------------------------------------------------------------ document builders
    def dn(self, key: str, nth: int, code: str, qty: float, day: date) -> str:
        cust = self.customer(india=True, nth=nth)
        doc = self.sales_base(cust, day, day)
        doc.update({"set_warehouse": self.acc(STORES), "posting_date": day.isoformat(), "posting_time": self.t(11), "set_posting_time": 1,
                    "items": [self.item_line(code, qty, self.sales_prices(cust, code, day), day, False)]})
        return self.put(key, "Delivery Note", doc)

    def po_doc(self, key: str, vkey: str, code: str, qty: float, rate: float, day: date, due: date) -> str:
        doc = self.po_base(VENDORS[vkey], day, due)
        doc["items"] = [self.item_line(code, qty, rate, day, True, schedule_date=due.isoformat())]
        return self.put(key, "Purchase Order", doc)

    @contextmanager
    def same_rate_off(self):
        """ERPNext's 'Maintain Same Rate Throughout the Purchase Cycle' (Buying Settings) refuses a receipt or invoice priced differently from its order, which is exactly
        what the price scenarios need. It is switched off only while such a document is posted and switched on again straight after, even if the posting fails."""
        was = self.w.get("Buying Settings", "Buying Settings").get("maintain_same_rate")
        self.state.setdefault("_same_rate_original", int(bool(was)))
        self.save()
        if was:
            self.w.update("Buying Settings", "Buying Settings", {"maintain_same_rate": 0})
        try:
            yield
        finally:
            if was:
                self.w.update("Buying Settings", "Buying Settings", {"maintain_same_rate": 1})

    @contextmanager
    def over_billing_allowed(self, pct: float = 15.0):
        """ERPNext also refuses an invoice above its receipt unless the site-wide 'Over Billing Allowance' (Accounts Settings) is high enough: the dearer-invoice scenarios need it.
        Raised only while such an invoice is posted and put back straight after, even if the posting fails."""
        was = float(self.w.get("Accounts Settings", "Accounts Settings").get("over_billing_allowance") or 0)
        self.state.setdefault("_over_billing_original", was)
        self.save()
        if was < pct:
            self.w.update("Accounts Settings", "Accounts Settings", {"over_billing_allowance": pct})
        try:
            yield
        finally:
            if was < pct:
                self.w.update("Accounts Settings", "Accounts Settings", {"over_billing_allowance": was})

    def receive(self, key: str, po_key: str, day: date, hh: int, *, qty: Optional[float] = None, ratio: Optional[float] = None, reject: float = 0.0,
                rate: Optional[float] = None, submit: bool = True) -> str:
        if key in self.names:                                       # made by an earlier run: a draft left behind is submitted now, under the same switch as a new one
            if rate is not None and key in self.draft:
                with self.same_rate_off():
                    return self.put(key, "Purchase Receipt", {}, submit=submit)
            return self.put(key, "Purchase Receipt", {}, submit=submit)
        doc = self.mapped(MAKE_PR, self.names[po_key])
        for line in doc["items"]:
            q = float(qty) if qty is not None else (whole(line["qty"] * ratio) if ratio and ratio < 1 else float(line["qty"]))
            rej = whole(q * reject) if reject else 0.0
            line.update({"qty": q - rej, "received_qty": q, "rejected_qty": rej})
            if rej:
                line["rejected_warehouse"] = self.acc(QUALITY_HOLD)
            if rate is not None:
                line["rate"] = rate
        doc.update({"posting_date": day.isoformat(), "posting_time": self.t(hh), "set_posting_time": 1, "conversion_rate": 1.0})
        if rate is not None:
            with self.same_rate_off():
                return self.put(key, "Purchase Receipt", doc, submit=submit)
        return self.put(key, "Purchase Receipt", doc, submit=submit)

    def invoice(self, key: str, pr_key: str, day: date, rate: Optional[float] = None) -> str:
        if key in self.names:
            if rate is not None and key in self.draft:
                with self.same_rate_off(), self.over_billing_allowed():
                    return self.put(key, "Purchase Invoice", {})
            return self.put(key, "Purchase Invoice", {})
        doc = self.mapped(MAKE_PI, self.names[pr_key])
        doc.update({"posting_date": day.isoformat(), "set_posting_time": 1, "bill_date": day.isoformat(), "update_stock": 0})
        doc.pop("due_date", None)
        doc.pop("payment_schedule", None)
        if rate is not None:
            for line in doc["items"]:
                line["rate"] = rate
            with self.same_rate_off(), self.over_billing_allowed():
                return self.put(key, "Purchase Invoice", doc)
        return self.put(key, "Purchase Invoice", doc)

    def recon(self, key: str, code: str, day: date, hh: int, delta: float) -> str:
        """A stock count: what was counted is what the system holds at that moment plus `delta`."""
        if key in self.names:
            return self.names[key]
        wh, when = self.acc(STORES), self.t(hh)
        bal = float(self.w.call("erpnext.stock.utils.get_stock_balance", item_code=code, warehouse=wh, posting_date=day.isoformat(), posting_time=when) or 0)
        counted = max(0.0, bal + delta)
        rows = self.w.find("Bin", [["item_code", "=", code], ["warehouse", "=", wh]], ["valuation_rate"], limit=1)
        rate = float(rows[0]["valuation_rate"]) if rows and rows[0].get("valuation_rate") else self.items[code].base_price
        self.notes.append(f"{code}: counted {counted:g} where the system had {bal:g} on {day.isoformat()}")
        return self.put(key, "Stock Reconciliation", {"purpose": "Stock Reconciliation", "company": self.ctx.company, "posting_date": day.isoformat(), "posting_time": when,
                                                      "set_posting_time": 1, "expense_account": self.acc("Stock Adjustment"), "cost_center": self.ctx.cc("Corporate & Admin"),
                                                      "items": [{"item_code": code, "warehouse": wh, "qty": counted, "valuation_rate": rate}]})

    # ------------------------------------------------------------------ event helpers
    def sell(self, m: Mat, dates: Optional[list[tuple[date, float]]] = None) -> None:
        """Delivery notes the app measures usage from: weekly by default (every window of 7 days holds exactly one), or at the dates given."""
        if dates is None:
            dates = [(self.ago(m.sell_end + 7 * k), float(round(m.use * 7))) for k in range(m.weeks)] if m.use else []
        for k, (day, qty) in enumerate(dates):
            key = self.key(m.code, f"DN{k:03d}")
            self.add(m.code, day, 11, -qty, "Delivery Note", f"sold {qty:g}", lambda key=key, day=day, qty=qty, code=m.code, n=sum(map(ord, m.code)) + k: self.dn(key, n % 9, code, qty, day))

    def po(self, m: Mat, label: str, vkey: str, qty: float, day: date, due: date, rate: Optional[float] = None) -> str:
        key = self.key(m.code, label)
        r = rate if rate is not None else self.rate_for(m.code, vkey)
        self.add(m.code, day, 9, 0, "Purchase Order", f"{label}: {qty:g} from {vkey} due {due.isoformat()}", lambda: self.po_doc(key, vkey, m.code, qty, r, day, due))
        return key

    def pr(self, m: Mat, label: str, po_key: str, day: date, delta: float, hh: int = 10, **kw: Any) -> str:
        key = self.key(m.code, label)
        draft = not kw.get("submit", True)
        self.add(m.code, day, hh, 0 if draft else delta, "Purchase Receipt", label, lambda: self.receive(key, po_key, day, hh, **kw), draft=draft)
        return key

    def pi(self, m: Mat, label: str, pr_key: str, day: date, rate: Optional[float] = None, pay: bool = False) -> str:
        key = self.key(m.code, label)
        self.add(m.code, day, 14, 0, "Purchase Invoice", label, lambda: self.invoice(key, pr_key, day, rate))
        if pay:
            self.add(m.code, day + timedelta(days=1), 16, 0, "Payment Entry", f"{label}-PAY", lambda: self.pay(f"{key}-PAY", "Purchase Invoice", self.names[key], day + timedelta(days=1)))
        return key

    def count(self, m: Mat, label: str, day: date, delta: float, hh: int = 21) -> None:
        self.add(m.code, day, hh, delta, "Stock Reconciliation", label, lambda: self.recon(self.key(m.code, label), m.code, day, hh, delta))

    # ------------------------------------------------------------------ the scenarios
    def build(self) -> None:
        for code, m in self.mats.items():
            getattr(self, "m_" + m.scenario)(m)
        for code, m in self.mats.items():
            if m.scenario not in ("seasonal",) or code != "TST-SEAS-1":
                self.sell(m)

    def m_late(self, m: Mat) -> None:
        a = self.ago
        if m.code == "TST-LATE-1":
            po = self.po(m, "PO1", "alpha", 1000, a(21), a(6))
            self.pr(m, "PR1", po, a(8), 200, qty=200)
        elif m.code == "TST-LATE-2":
            self.po(m, "PO1", "beta", 5000, a(19), a(4))                 # 4 days late
            self.po(m, "PO2", "beta", 1000, a(5), a(1))                  # due yesterday: inside the one grace day
            self.po(m, "PO3", "beta", 800, a(3), self.T + timedelta(days=4))     # not due yet
        else:
            self.po(m, "PO1", "gamma", 60, a(12), a(3))

    def m_grn(self, m: Mat) -> None:
        a = self.ago
        if m.code == "TST-GRN-1":
            po = self.po(m, "PO1", "alpha", 400, a(9), a(4))
            self.pr(m, "PR1", po, a(3), 460, qty=460)
        elif m.code == "TST-GRN-2":
            po = self.po(m, "PO1", "beta", 500, a(10), a(5))
            self.pr(m, "PR1", po, a(4), 425, reject=0.15)
        elif m.code == "TST-GRN-3":
            po = self.po(m, "PO1", "alpha", 40, a(9), a(5))
            self.pr(m, "PR1", po, a(4), 40, rate=round(self.rate_for(m.code, "alpha") * 1.08, 2))
        elif m.code == "TST-GRN-4":
            po = self.po(m, "PO1", "delta", 120, a(8), a(4))
            pr = self.pr(m, "PR1", po, a(5), 120)
            self.pi(m, "PI1", pr, a(3), rate=round(self.rate_for(m.code, "delta") * 1.07, 2))
        elif m.code == "TST-GRN-5":
            po = self.po(m, "PO1", "beta", 90, a(9), a(5))
            pr = self.pr(m, "PR1", po, a(6), 90)
            self.pi(m, "PI1", pr, a(5), rate=round(self.rate_for(m.code, "beta") * 1.05, 2), pay=True)
        else:
            po = self.po(m, "PO1", "alpha", 300, a(14), a(6))
            self.pr(m, "PR1", po, a(8), 100, qty=100)
            self.pr(m, "PR2", po, a(3), 200)

    def m_scorecard(self, m: Mat) -> None:
        a = self.ago
        for i, ago in enumerate((152, 122, 92, 62, 32)):                         # Alpha: always a day early, a steady price
            po = self.po(m, f"A{i}", "alpha", 200, a(ago + 10), a(ago), rate=round(self.rate_for(m.code, "alpha") * (1 + 0.002 * i), 2))
            self.pr(m, f"A{i}R", po, a(ago + 1), 200)
        late = (5, 0, 7, 0, 9, 6)                                                # Beta: due dates met twice in six, prices up about 14%
        prices = (590, 600, 615, 630, 650, 675)
        for i, (due_ago, delay, price) in enumerate(zip((155, 125, 95, 65, 35, 12), late, prices)):
            po = self.po(m, f"B{i}", "beta", 200, a(due_ago + 10), a(due_ago), rate=float(price))
            self.pr(m, f"B{i}R", po, a(due_ago - delay), 180 if i == 5 else 200, **({"qty": 200, "reject": 0.10} if i == 5 else {}))
        po = self.po(m, "G0", "gamma", 200, a(70), a(60), rate=640.0)             # Gamma: one delivery
        self.pr(m, "G0R", po, a(61), 200)

    def m_rfq(self, m: Mat) -> None:
        a = self.ago
        for label, vkey, ago, delay in (("A0", "alpha", 80, -2), ("A1", "alpha", 45, -2), ("B0", "beta", 60, 8)):
            po = self.po(m, label, vkey, 300, a(ago + 10), a(ago))
            self.pr(m, f"{label}R", po, a(ago - delay), 300)

    def m_seasonal(self, m: Mat) -> None:
        a = self.ago
        if m.code == "TST-SEAS-1":
            fest_start, fest_end = a(367), a(348)                                    # last year's festive weeks: 24 Oct - 12 Nov when run on 26 Oct
            days: list[tuple[date, float]] = []
            d = self.T - timedelta(days=1)
            first = a(469)
            while d >= first:                                                        # normal sales: a delivery every 3 days, 3 days' worth
                if not (fest_start <= d <= fest_end):
                    days.append((d, float(round(m.use * 3))))
                d -= timedelta(days=3)
            d = fest_start
            while d <= fest_end:                                                     # the festival: every day, 1.8 times the normal rate
                days.append((d, float(round(m.use * 1.8))))
                d += timedelta(days=1)
            self.sell(m, sorted(days, reverse=True))
        else:
            key = self.key(m.code, "SO1")
            self.add(m.code, a(2), 12, 0, "Sales Order", "SO1: 90 due in 6 days", lambda: self.order(key, self.customer(india=True, nth=1), [(m.code, 90.0)], a(2), self.T + timedelta(days=6)))

    def m_slow(self, m: Mat) -> None:
        a = self.ago
        if m.code == "TST-DEAD-1":                                                   # more was bought long ago than was ever sold
            key = self.key(m.code, "TOPUP")
            self.add(m.code, a(250), 8, 1400, "Stock Entry", "TOPUP: 1400 received", lambda: self.entry(key, "Material Receipt", [{"item_code": m.code, "t_warehouse": self.acc(STORES), "qty": 1400.0,
                                                                                                                                 "basic_rate": self.items[m.code].base_price}], a(250), 8, "Stock received long ago"))
        elif m.code == "TST-DEAD-2":
            po = self.po(m, "PO1", "alpha", 400, a(30), a(26))
            self.pr(m, "PR1", po, a(24), 400)
            po2 = self.po(m, "PO2", "alpha", 1100, a(30), a(26))
            self.pr(m, "PR2", po2, a(24), 1100, hh=11)
        elif m.code == "TST-SLOW-2":
            self.po(m, "PO1", "beta", 3000, a(3), self.T + timedelta(days=12))

    def m_subs(self, m: Mat) -> None:
        pass

    def m_counts(self, m: Mat) -> None:
        a = self.ago
        if m.code == "TST-CNT-1":
            self.count(m, "COUNT1", a(45), +15)
            self.count(m, "COUNT2", a(1), +40)
            po = self.po(m, "PO1", "alpha", 40, a(6), a(1))
            self.pr(m, "PR1", po, a(1), 40, submit=False, hh=15)                     # received on the shelf but not posted: still a draft
        elif m.code == "TST-CNT-2":
            self.count(m, "COUNT1", a(1), -220)
        elif m.code == "TST-CNT-3":
            self.count(m, "COUNT1", a(1), -20)
        elif m.code == "TST-CNT-5":
            self.count(m, "COUNT1", a(300), +3, hh=9)

    # ------------------------------------------------------------------ stock position (computed, no ERPNext needed)
    def opening(self, m: Mat) -> tuple[float, float, float]:
        """(opening quantity, lowest level reached, stock today) from the events: the opening receipt is chosen so that stock today is what the material asks for."""
        evs = sorted(self.evs[m.code], key=lambda e: (e.day, e.hh))
        run = low = 0.0
        for e in evs:
            run += e.delta
            low = min(low, run)
        if m.stock is not None:
            opening = m.stock - run
        else:
            opening = -low + round(m.use * COVER_DAYS)
        if opening < 0 or opening + low < -1e-9:
            raise PlanError(f"{m.code}: stock would go negative (opening {opening:g}, lowest {opening + low:g}); give it more stock or fewer sales")
        return opening, opening + low, opening + run

    def summary(self) -> list[dict[str, Any]]:
        out = []
        for code, m in self.mats.items():
            opening, low, now = self.opening(m)
            kinds: dict[str, int] = {}
            for e in self.evs[code]:
                kinds[e.kind] = kinds.get(e.kind, 0) + 1
            if opening > 0:
                kinds["Stock Entry"] = kinds.get("Stock Entry", 0) + 1
            out.append({"code": code, "name": m.name, "scenario": m.scenario, "stock_now": now, "opening": opening, "lowest": low, "docs": kinds, "uom": self.items[code].uom, "note": m.note,
                        "first_day": min([e.day for e in self.evs[code]] or [self.T]), "use": m.use})
        return out

    # ------------------------------------------------------------------ doing it
    def setup(self) -> None:
        """The test vendors with their contacts, the items, and the item alternatives."""
        w = self.w
        used = sorted({k for m in self.mats.values() for k in m.vendors})
        steps.step_suppliers(self.ctx, [VENDORS[k] for k in used])
        steps.step_customers(self.ctx, TEST_CUSTOMERS)
        for k in used:
            contact = CONTACTS[k]
            v = VENDORS[k]
            if not contact:
                continue
            filters = [["Dynamic Link", "link_doctype", "=", "Supplier"], ["Dynamic Link", "link_name", "=", v.name]]
            rows = w.find("Contact", filters, ["name"], limit=1) if not w.dry_run else []
            if rows:
                name = rows[0]["name"]
            else:
                name, email, mobile = contact
                doc = {"first_name": name, "company_name": v.name, "is_primary_contact": 1, "links": [{"link_doctype": "Supplier", "link_name": v.name}]}
                if email:
                    doc["email_ids"] = [{"email_id": email, "is_primary": 1}]
                if mobile:
                    doc["phone_nos"] = [{"phone": mobile, "is_primary_mobile_no": 1}]
                name = w.create("Contact", doc)
            w.update("Supplier", v.name, {"supplier_primary_contact": name})
        for code, m in self.mats.items():
            doc = steps.item_doc(self.ctx, self.items[code])
            if m.alt:
                doc["allow_alternative_item"] = 1
            if m.over_receipt_pct:
                doc["over_delivery_receipt_allowance"] = m.over_receipt_pct
            if m.over_billing_pct:
                doc["over_billing_allowance"] = m.over_billing_pct
            if w.ensure("Item", code, doc) == "exists" and not w.dry_run:          # an item made by an earlier run still gets the allowances this scenario needs
                extra = {k: v for k, v in (("allow_alternative_item", 1 if m.alt else 0), ("over_delivery_receipt_allowance", m.over_receipt_pct),
                                           ("over_billing_allowance", m.over_billing_pct)) if v}
                if extra:
                    w.update("Item", code, extra)
        for a, b in (("TST-SUB-A1", "TST-SUB-A2"), ("TST-SUB-A1", "TST-SUB-A3"), ("TST-SUB-C1", "TST-SUB-C2")):
            if a in self.mats and b in self.mats:
                w.ensure_one("Item Alternative", [["item_code", "=", a], ["alternative_item_code", "=", b]], {"item_code": a, "alternative_item_code": b, "two_way": 0})

    def run_all(self) -> None:
        self.load_names()
        self.setup()
        for code, m in self.mats.items():
            opening, _low, now = self.opening(m)
            evs = sorted(self.evs[code], key=lambda e: (e.day, e.hh))
            log.info("== %s %s (opening %g, stock today %g, %d documents)", code, m.name, opening, now, len(evs))
            try:
                if opening > 0:
                    first = min([e.day for e in evs] or [self.T]) - timedelta(days=2)
                    self.entry(self.key(code, "OPEN"), "Material Receipt", [{"item_code": code, "t_warehouse": self.acc(STORES), "qty": float(opening), "basic_rate": self.items[code].base_price}],
                               first, 8, "Opening stock for the feature test scenarios")
                for e in evs:
                    e.run()
            except WriterError as exc:
                self.failures.append((code, str(exc)))
                log.error("   FAILED %s: %s", code, exc)
        self.restore_same_rate()

    def cancel_foreign_sales(self) -> dict[str, int]:
        """Take back the delivery notes and sales orders of these scenarios that name a customer other than the test customers (a first run did that and used up part of those customers'
        credit). Submitted ones are cancelled (ERPNext reverses their stock effect and keeps the cancelled document), drafts are deleted. Nothing else is touched."""
        mine = {c.name for c in TEST_CUSTOMERS}
        done = {"cancelled": 0, "deleted": 0}
        for doctype in ("Delivery Note", "Sales Order"):
            for r in self.w.find(doctype, [["po_no", "like", f"{MARK}{PFX}%"], ["docstatus", "<", 2]], ["name", "customer", "docstatus", "po_no"], limit=100000):
                if r["customer"] in mine:
                    continue
                if r["docstatus"] == 1:
                    self.w.call("frappe.client.cancel", doctype=doctype, name=r["name"])
                    done["cancelled"] += 1
                else:
                    self.w.call("frappe.client.delete", doctype=doctype, name=r["name"])
                    done["deleted"] += 1
                key = r["po_no"][len(MARK):].split(" ", 1)[0]
                self.state.pop(key, None)
                self.names.pop(key, None)
        self.save()
        return done

    def restore_same_rate(self) -> None:
        was = self.state.get("_same_rate_original")
        if was is not None:
            cur = self.w.get("Buying Settings", "Buying Settings").get("maintain_same_rate")
            if bool(cur) != bool(was):
                self.w.update("Buying Settings", "Buying Settings", {"maintain_same_rate": int(bool(was))})
                self.notes.append("Buying Settings: 'Maintain Same Rate' was found switched off and has been put back as it was")
        billing = self.state.get("_over_billing_original")
        if billing is not None:
            cur = float(self.w.get("Accounts Settings", "Accounts Settings").get("over_billing_allowance") or 0)
            if cur != float(billing):
                self.w.update("Accounts Settings", "Accounts Settings", {"over_billing_allowance": float(billing)})
                self.notes.append("Accounts Settings: 'Over Billing Allowance' was found raised and has been put back as it was")

    def quote_replies(self, code: str, qty: float, prices: dict[str, float]) -> list[str]:
        """The supplier quotations the vendors would send back, dated today."""
        out = []
        for vkey, price in prices.items():
            sup, day = VENDORS[vkey], self.T
            base = self.po_base(sup, day, day + timedelta(days=7))
            doc = {"supplier": sup.name, "company": self.ctx.company, "transaction_date": day.isoformat(), "valid_till": (day + timedelta(days=14)).isoformat(), "currency": "INR",
                   "conversion_rate": 1.0, "buying_price_list": base["buying_price_list"], "tax_category": base["tax_category"], "ignore_pricing_rule": 1, "taxes": base["taxes"],
                   "items": [{**self.item_line(code, qty, price, day, True), "lead_time_days": 6 + len(out)}]}
            out.append(self.put(f"{PFX}SQ-{code}-{vkey}-{day:%Y%m%d}", "Supplier Quotation", doc))
        return out


# ---------------------------------------------------------------------------------------------------------------------- report and check
def stock_report(ctx: Context, codes: list[str]) -> list[dict[str, Any]]:
    """Every material's stock position as ERPNext holds it now (read-only)."""
    w = ctx.w
    out = []
    bins: dict[str, dict[str, float]] = {}
    for r in w.find("Bin", [["item_code", "like", "TST-%"]], ["item_code", "actual_qty", "reserved_qty", "ordered_qty", "stock_value"], limit=5000):
        b = bins.setdefault(r["item_code"], {"on_hand": 0.0, "reserved": 0.0, "on_order": 0.0, "value": 0.0})
        b["on_hand"] += float(r["actual_qty"] or 0)
        b["reserved"] += float(r["reserved_qty"] or 0)
        b["on_order"] += float(r["ordered_qty"] or 0)
        b["value"] += float(r["stock_value"] or 0)
    names = {r["name"]: r for r in w.find("Item", [["item_code", "like", "TST-%"]], ["name", "item_name", "stock_uom"], limit=5000)}
    for code in codes:
        b = bins.get(code, {"on_hand": 0.0, "reserved": 0.0, "on_order": 0.0, "value": 0.0})
        out.append({"code": code, "name": names.get(code, {}).get("item_name", "(not created yet)"), "uom": names.get(code, {}).get("stock_uom", ""), **b})
    return out


def check(config: str, today: date) -> None:
    """The app's own tools, read-only, on what is now in ERPNext: a throw-away database, only the console channel, nothing is sent and nothing is written to ERPNext."""
    import copy
    import tempfile

    from erp_agents.core.bootstrap import build_app
    from ai_munimji_engine.core.common import Actor
    from erp_agents.core.config import load_config
    cfg = copy.deepcopy(load_config(config))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:           # Windows keeps the throw-away database locked a moment after it is closed
        cfg["database"]["path"] = str(Path(tmp) / "check.db")
        cfg["channels"] = {"console": {"enabled": True}}
        cfg["approvals"] = {"strategy": "all", "recipients": [{"id": "owner", "name": "Owner", "roles": ["admin", "approver"], "channels": [{"type": "console"}]}]}
        app = build_app(config=cfg)
        who = Actor("check", frozenset({"viewer", "approver", "admin"}), "cli")

        def tool(name: str, **args: Any) -> dict[str, Any]:
            return app.toolkit.call(name, args, who)

        def show(title: str, data: Any) -> None:
            print(f"\n=== {title}")
            print(json.dumps(data, indent=1, default=str)[:3500])
        have = {i.code for i in app.erp.list_items() if i.code.startswith("TST-")}
        print(f"{len(have)} TST materials found in ERPNext")
        late = tool("late_deliveries")
        show("late_deliveries", [{k: o[k] for k in ("document", "vendor", "days_late", "chasing")} | {"items": [(i["item_code"], i["still_to_come"], i["cover_days_without_it"], i["cover_days_with_it"]) for i in o["items"]]} for o in late.get("orders", [])
                                 if any(i["item_code"].startswith("TST-") for i in o["items"])])
        gr = tool("receipt_mismatches", days=30)
        show("receipt_mismatches (TST only)", [{"receipt": r["receipt"], "vendor": r["vendor"], "differences": r["differences"], "before_you_pay": r["before_you_pay"]} for r in gr.get("receipts", []) if any("TST-" in d or "(TST" in d for d in r["differences"]) or "TST" in r["vendor"]])
        sc = tool("vendor_scorecard")
        show("vendor_scorecard (TST vendors)", [v for v in sc.get("vendors", []) if "TST" in v["vendor"]])
        for code in ("TST-SEAS-1", "TST-SEAS-2"):
            if code not in have:
                continue
            a, _ = app.inventory.analyze_one(code)
            print(f"\n=== analysis {code}: status {a.status}, available {a.available:g}, position {a.position:g}, cover {a.cover_days_on_hand and round(a.cover_days_on_hand, 1)} days, lead {a.lead_time_days:g}")
            print("   reasons:", [r.text for r in a.reasons])
            f = a.forecast or {}
            print("   forecast:", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in f.items() if k != "weekly"})
        sl = tool("slow_stock", limit=25)
        show("slow_stock (TST only)", [{k: x[k] for k in ("item_code", "kind", "on_hand", "value_tied_up", "action", "advice")} for x in sl.get("items", []) if x["item_code"].startswith("TST-")])
        for code in ("TST-SUB-A1", "TST-SUB-B1", "TST-SUB-C1"):
            if code not in have:
                continue
            show(f"substitutes {code}", tool("substitutes", item_code=code))
            a, _ = app.inventory.analyze_one(code)
            print("   alert sections:", [s.heading for s in app.stock_insight.substitutes.section(a)] or "none", "| status", a.status)
        show("stock_counts plan", [x for x in tool("stock_counts", kind="plan").get("count_these", [])][:25])
        show("stock_counts variances (TST only)", [d for d in tool("stock_counts", kind="variances").get("documents", [])])
        cls = {r["item_code"]: r["class"] for r in app.stock_insight.counts.schedule().get("items", []) if r["item_code"].startswith("TST-")}
        print("\n=== count classes:", cls)
        q = app.followup.quotes.candidates("TST-RFQ-1") if "TST-RFQ-1" in have else []
        print("\n=== quote candidates TST-RFQ-1:", [(c["name"], c["why_listed"], app.followup.messenger.route(c["supplier"]).channel) for c in q])
        for code in ("TST-LATE-1", "TST-LATE-3"):
            if code not in have:
                continue
            print(f"\n=== contact route {code}:", app.followup.messenger.route(app.erp.get_documents("Purchase Order", item_code=code, limit=1)[0]["party_id"]))

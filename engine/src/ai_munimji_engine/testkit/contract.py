"""The checks every ERP client must pass, written once against the port (`ai_munimji_engine.packs.stock_procurement.erp`).

A check is a function `fn(client, site)` that raises `AssertionError` when the client's answer differs from what the documents of the world say it must be (`world.py`). The checks
need no test framework: `run_checks(client, site)` runs them and returns one `Result` each, so the same code is run by pytest against the demo ERP and the REST client (in
erpnext-ai-agents) and by unittest against the Frappe client (in the app, on a bench), and the three answers can be set side by side.

Each check says what data it needs (`@check("stock", "orders")`): the driver says what it has loaded (`has=`), and a check that needs more is reported as skipped, with the reason.
A check that writes (`writes=True`) leaves nothing behind: it deletes or cancels what it made, or asks `site.discard`.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from ..packs.stock_procurement.erp import PurchaseOrderDraft, QuoteRule, RequisitionDraft, StockTransferDraft
from .world import WORLD, World

ALL_DATA = frozenset({"items", "stock", "history", "orders", "quotes", "sales_orders", "taxes", "submit", "discard"})


class Skip(Exception):
    """The check does not apply to this client or this site (the reason is the message)."""


@dataclass
class Site:
    """What a driver tells the checks about the site it gave the client: the reference day (the day the world was loaded), the names the site really uses for the world's logical
    keys ("wh_a" -> "Contract A - MDC"), and, if it can, how to submit or discard a document made by a check."""
    today: date
    names: dict[str, str]
    submit: Optional[Callable[[str, str], None]] = None          # (doctype, name): submit a draft, as a person would in the ERP
    discard: Optional[Callable[[str, str], None]] = None         # (doctype, name): remove a draft a check made (Material Request, Stock Entry: the port has no way to)
    world: World = field(default_factory=lambda: WORLD)

    def n(self, key: str) -> str:
        return self.names[key]

    def key_of(self, name: str) -> Optional[str]:
        return next((k for k, v in self.names.items() if v == name), None)


@dataclass(frozen=True)
class Check:
    name: str
    run: Callable[[Any, Site], None]
    needs: frozenset
    writes: bool


@dataclass
class Result:
    name: str
    status: str                  # pass | fail | skip | error
    detail: str = ""


CHECKS: list[Check] = []


def check(*needs: str, writes: bool = False):
    def register(fn):
        CHECKS.append(Check(fn.__name__, fn, frozenset(needs), writes))
        return fn
    return register


def run_checks(client: Any, site: Site, has: frozenset = ALL_DATA, only: Optional[set[str]] = None) -> list[Result]:
    """Run the checks the driver's data allows. A client that lacks a method a check calls is reported as skipped for that check, never as passed."""
    results = []
    for c in CHECKS:
        if only is not None and c.name not in only:
            continue
        missing = c.needs - has
        if missing:
            results.append(Result(c.name, "skip", "needs " + ", ".join(sorted(missing))))
            continue
        try:
            c.run(client, site)
            results.append(Result(c.name, "pass"))
        except Skip as exc:
            results.append(Result(c.name, "skip", str(exc)))
        except AssertionError as exc:
            results.append(Result(c.name, "fail", str(exc)[:600]))
        except Exception as exc:  # noqa: BLE001 - a client that raises is a finding, with its message
            results.append(Result(c.name, "error", f"{type(exc).__name__}: {str(exc)[:500]}"))
    return results


# ---------------------------------------------------------------------------------------------------------------------- helpers
def eq(actual: Any, expected: Any, what: str = "") -> None:
    assert actual == expected, f"{what + ': ' if what else ''}expected {expected!r}, got {actual!r}"


def near(actual: Optional[float], expected: Optional[float], what: str = "") -> None:
    if expected is None or actual is None:
        assert actual is None and expected is None, f"{what + ': ' if what else ''}expected {expected!r}, got {actual!r}"
        return
    assert abs(float(actual) - float(expected)) < 0.005, f"{what + ': ' if what else ''}expected {expected!r}, got {actual!r}"


def need(client: Any, method: str) -> Callable:
    fn = getattr(client, method, None)
    if fn is None:
        raise Skip(f"this client has no {method}")
    return fn


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def codes(site: Site, *keys: str) -> list[str]:
    return [site.n(k) for k in keys]


def date_of(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _draft(site: Site, reference: str, qty: float = 5.0, supplier: str = "s1", item: str = "writes", rate: float = 12.0) -> PurchaseOrderDraft:
    return PurchaseOrderDraft(supplier=site.n(supplier), item_code=site.n(item), qty=qty, rate=rate, uom="Nos", required_by=site.today + timedelta(days=14),
                              reference=reference, approved_by="tester@example.com via desk")


def _line_qty(doc: dict, item_code: str) -> float:
    return sum(float(l["qty"]) for l in doc["lines"] if l["item_code"] == item_code)


def _clean_order(client: Any, site: Site, name: Optional[str], reference: str) -> None:
    """Take back an order a check made: delete the draft or cancel it, then, where the driver can, remove what is left (a cancelled order stays in an ERP until it is deleted)."""
    if name:
        try:
            client.cancel_purchase_order(name, reference)
        except Exception:  # noqa: BLE001 - cleanup must not hide what the check found
            pass
        if site.discard:
            try:
                site.discard("Purchase Order", name)
            except Exception:  # noqa: BLE001
                pass


def _order_by_hand(client: Any, site: Site) -> dict:
    """The world's order that was made by hand (no reference): the late supplier's."""
    docs = client.get_documents("Purchase Order", item_code=site.n("steady"), limit=100)
    mine = [d for d in docs if d["party_id"] == site.n("s2") and not d["approved_by"]]
    assert len(mine) == 1, f"expected the late supplier's one order made by hand, found {len(mine)}"
    return mine[0]


# ---------------------------------------------------------------------------------------------------------------------- the connection
@check()
def healthcheck_is_true(client, site):
    assert client.healthcheck() is True


# ---------------------------------------------------------------------------------------------------------------------- items and warehouses
@check("items")
def list_items_lists_stock_items_and_leaves_out_disabled_and_services(client, site):
    got = {i.code for i in client.list_items()}
    mine = {site.n(k) for k in site.names if k in {i.key for i in site.world.items}}
    wanted = {site.n(i.key) for i in site.world.listed_items()}
    assert wanted <= got, f"missing from the list: {sorted(wanted - got)}"
    assert not ({site.n("off"), site.n("service")} & got), "a disabled item or a service was listed"
    assert got & mine == wanted, f"items of the world that should not be listed: {sorted((got & mine) - wanted)}"


@check("items")
def list_items_carries_name_unit_and_lead_time(client, site):
    by = {i.code: i for i in client.list_items()}
    steady = by[site.n("steady")]
    eq(steady.name, site.world.item("steady").name, "name")
    eq(steady.uom, "Nos", "unit")
    near(steady.lead_time_days, 9.0, "lead time")
    assert by[site.n("low")].lead_time_days is None, "an item with no lead time says None"


@check("items", "stock")
def list_items_begins_an_items_history_at_its_first_stock_movement(client, site):
    by = {i.code: i for i in client.list_items()}
    eq(date_of(by[site.n("steady")].created), site.today - timedelta(days=100), "steady")
    eq(date_of(by[site.n("moving")].created), site.today - timedelta(days=50), "moving")
    created_new = date_of(by[site.n("new")].created)
    # the day it was created, which is the day the world was loaded, give or take one: the ERP stamps `creation` in its own time zone, the loader took the reference day from its own clock
    assert created_new is not None and abs((created_new - site.today).days) <= 1, f"an item with no movement keeps the day it was created (got {created_new}, the world was loaded on {site.today})"


@check("items")
def list_warehouses_leaves_out_groups_and_disabled_ones(client, site):
    got = set(client.list_warehouses())
    wanted = {site.n(k) for k in site.world.usable_warehouses()}
    assert wanted <= got, f"missing: {sorted(wanted - got)}"
    assert not ({site.n("wh_g"), site.n("wh_x")} & got), "a group or disabled warehouse was listed"


# ---------------------------------------------------------------------------------------------------------------------- stock
@check("stock")
def get_stock_sums_the_shelves_of_an_item(client, site):
    stock = client.get_stock(codes(site, "steady", "low", "moq"))
    for key in ("steady", "low", "moq"):
        want = site.world.stock(key)
        s = stock[site.n(key)]
        near(s.on_hand, want["on_hand"], f"{key} on hand")
        near(s.reserved, want["reserved"], f"{key} reserved")
        near(s.incoming, want["incoming"], f"{key} incoming")


@check("stock")
def get_stock_says_what_is_on_each_shelf(client, site):
    s = client.get_stock(codes(site, "steady"))[site.n("steady")]
    want = site.world.stock("steady")["places"]
    got = {p.warehouse: p.on_hand for p in s.by_warehouse}
    eq(got, {site.n(k): v for k, v in want.items()}, "shelves")


@check("stock")
def get_stock_can_be_limited_to_some_warehouses(client, site):
    s = client.get_stock(codes(site, "steady"), [site.n("wh_b")])[site.n("steady")]
    want = site.world.stock("steady", ["wh_b"])
    near(s.on_hand, want["on_hand"], "on hand in wh_b")
    near(s.reserved, want["reserved"], "reserved in wh_b")


@check("stock")
def get_stock_gives_zero_for_an_item_with_no_stock_or_no_record(client, site):
    stock = client.get_stock([site.n("new"), "CT-NO-SUCH-ITEM"])
    for code in (site.n("new"), "CT-NO-SUCH-ITEM"):
        assert code in stock, f"{code} is missing from the answer"
        near(stock[code].on_hand, 0.0, f"{code} on hand")


@check("stock", "sales_orders")
def get_stock_reserves_what_an_open_sales_order_holds(client, site):
    s = client.get_stock(codes(site, "steady"))[site.n("steady")]
    near(s.reserved, 30.0, "reserved")


@check("stock", "orders")
def get_stock_counts_an_order_on_its_way_and_says_when_it_is_due(client, site):
    s = client.get_stock(codes(site, "low"))[site.n("low")]
    near(s.incoming, 30.0, "incoming")
    eq(date_of(s.next_arrival), site.world.next_arrival("low", site.today), "next arrival")


@check("stock")
def get_stock_reports_stock_in_transit_apart(client, site):
    s = client.get_stock(codes(site, "moving"))[site.n("moving")]
    near(s.on_hand, 3.0, "on hand")
    near(s.in_transit, 7.0, "in transit")
    flagged = {p.warehouse: p.in_transit for p in s.by_warehouse}
    eq(flagged.get(site.n("wh_t")), True, "the transit shelf is marked")


# ---------------------------------------------------------------------------------------------------------------------- usage
def _usage(client: Any, site: Site, key: str, start: date, end: date, warehouses=None) -> dict[date, float]:
    got = client.get_consumption([site.n(key)], start, end, warehouses).get(site.n(key), [])
    return {date_of(u.day): float(u.qty) for u in got}


@check("history")
def get_consumption_gives_the_days_something_was_used(client, site):
    start, end = site.today - timedelta(days=90), site.today
    eq(_usage(client, site, "steady", start, end), site.world.usage("steady", site.today, start, end), "steady")
    eq(_usage(client, site, "low", start, end), site.world.usage("low", site.today, start, end), "low")


@check("history")
def get_consumption_keeps_to_the_window(client, site):
    start, end = site.today - timedelta(days=30), site.today - timedelta(days=10)
    got = _usage(client, site, "steady", start, end)
    eq(got, site.world.usage("steady", site.today, start, end), "inside the window")
    assert got, "the window was chosen to hold sales"


@check("history")
def get_consumption_keeps_to_the_warehouses_asked_for(client, site):
    start, end = site.today - timedelta(days=90), site.today
    eq(_usage(client, site, "steady", start, end, [site.n("wh_b")]), site.world.usage("steady", site.today, start, end, ["wh_b"]), "wh_b only")


@check("history")
def get_consumption_is_empty_for_an_item_nothing_was_sold(client, site):
    eq(_usage(client, site, "new", site.today - timedelta(days=90), site.today), {}, "new")


@check("sales_orders")
def get_open_sales_orders_says_what_is_still_to_deliver(client, site):
    read = need(client, "get_open_sales_orders")
    due = read([site.n("steady"), site.n("low")], site.today + timedelta(days=30))
    rows = [(date_of(d.due), float(d.qty)) for d in due.get(site.n("steady"), [])]
    eq(rows, site.world.open_demand("steady", site.today, site.today + timedelta(days=30)), "steady")
    assert site.n("low") not in due or not due[site.n("low")], "low has no open sales order"
    short = read([site.n("steady")], site.today + timedelta(days=2))
    assert not short.get(site.n("steady")), "an order due after the date asked for is left out"


# ---------------------------------------------------------------------------------------------------------------------- vendors
def _offers(client: Any, site: Site, key: str, since_days: int, quotes: Optional[QuoteRule] = None) -> dict[str, Any]:
    since = site.today - timedelta(days=since_days)
    rows = client.get_vendor_offers(site.n(key), since, quotes)
    return {site.key_of(o.supplier) or o.supplier: o for o in rows}


@check("orders")
def get_vendor_offers_counts_orders_and_prices(client, site):
    got = _offers(client, site, "steady", 180)
    want = site.world.offers("steady", site.today, site.today - timedelta(days=180))
    eq(sorted(got), sorted(want), "which suppliers")
    for k, w in want.items():
        near(got[k].unit_price, w["unit_price"], f"{k} last price")
        near(got[k].avg_price, w["avg_price"], f"{k} average price")
        eq(got[k].orders, w["orders"], f"{k} orders")
        eq(got[k].name, w["name"], f"{k} name")


@check("orders")
def get_vendor_offers_works_out_delivery_time_and_punctuality(client, site):
    got = _offers(client, site, "steady", 180)
    want = site.world.offers("steady", site.today, site.today - timedelta(days=180))
    for k in ("s1", "s2"):
        near(got[k].lead_time_days, want[k]["lead_time_days"], f"{k} lead time")
        near(got[k].on_time_rate, want[k]["on_time_rate"], f"{k} on-time rate")


@check("orders")
def get_vendor_offers_reads_payment_terms_and_the_item_supplier_list(client, site):
    got = _offers(client, site, "steady", 180)
    near(got["s1"].payment_days, 30.0, "s1 payment days")
    assert got["s2"].payment_days is None, "no payment terms say None"
    eq(got["s1"].listed, True, "s1 is on the item's list")
    eq(got["s2"].listed, False, "s2 is not")


@check("orders")
def get_vendor_offers_includes_a_listed_supplier_that_was_never_ordered_from(client, site):
    got = _offers(client, site, "steady", 180)
    assert "s3" in got, "s3 is on the item's list"
    eq(got["s3"].orders, 0, "orders")
    assert got["s3"].unit_price is None, "no order, no price"


@check("orders")
def get_vendor_offers_leaves_out_a_disabled_supplier(client, site):
    got = _offers(client, site, "steady", 180)
    assert "s4" not in got and site.n("s4") not in got, "a disabled supplier was offered"


@check("orders")
def get_vendor_offers_looks_only_as_far_back_as_asked(client, site):
    got = _offers(client, site, "steady", 35)
    want = site.world.offers("steady", site.today, site.today - timedelta(days=35))
    eq({k: o.orders for k, o in got.items()}, {k: w["orders"] for k, w in want.items()}, "orders in the last 35 days")
    eq(got["s1"].orders, 0, "s1's orders are older than that")


@check("orders", "quotes")
def get_vendor_offers_adds_a_valid_quotation_when_asked(client, site):
    got = _offers(client, site, "steady", 180, QuoteRule(today=site.today, max_age_days=30))
    want = site.world.offers("steady", site.today, site.today - timedelta(days=180), quote_rule_today=site.today)
    near(got["s3"].quote_price, want["s3"]["quote_price"], "s3 quote price")
    near(got["s3"].quote_qty, want["s3"]["quote_qty"], "s3 quote quantity")
    eq(date_of(got["s3"].quote_valid_till), want["s3"]["quote_valid_till"], "s3 valid till")
    assert got["s2"].quote_price is None, "an expired quotation is not used"


@check("orders", "quotes")
def get_vendor_offers_says_nothing_of_quotations_unless_asked(client, site):
    got = _offers(client, site, "steady", 180)
    assert all(o.quote_price is None for o in got.values()), "quotation fields filled without being asked"


@check("items")
def item_min_order_qty_is_the_items_own(client, site):
    near(client.item_min_order_qty(site.n("moq")), 25.0, "moq")
    assert not client.item_min_order_qty(site.n("steady")), "an item with none says None"


@check("taxes")
def get_purchase_taxes_follows_the_tax_rule(client, site):
    rule = site.world.tax_rules[0]
    setup = client.get_purchase_taxes(site.n(rule.supplier), site.n("steady"), site.today)
    eq(setup.source, "tax rule", "source")
    eq(setup.template, site.n(rule.template), "template")
    eq(len(setup.rows), len(rule.rows), "rows")
    for row, want in zip(setup.rows, rule.rows):
        eq((row.charge_type, row.category, row.add_deduct), (want.charge_type, want.category, want.add_deduct), "row")
        near(row.rate, want.rate, "rate")


@check("taxes")
def get_purchase_taxes_does_not_invent_a_template(client, site):
    setup = client.get_purchase_taxes(site.n("s2"), site.n("steady"), site.today)
    assert setup.source in ("none", "default template"), f"source {setup.source!r}"
    if setup.source == "none":
        assert setup.template is None and not setup.rows, "no setup means no template and no rows"
        assert setup.notes, "and it says why"


# ---------------------------------------------------------------------------------------------------------------------- documents already made
@check("orders")
def find_by_reference_finds_the_order_made_for_a_request(client, site):
    found = need(client, "find_by_reference")("Purchase Order", "CT-REF-O2")
    assert found is not None, "the order made for CT-REF-O2 was not found"
    eq(found.doctype, "Purchase Order")
    eq(found.approved_by, "ops@example.com via desk", "approver")
    assert need(client, "find_by_reference")("Purchase Order", "CT-REF-NOBODY") is None, "an unknown reference finds nothing"


@check("orders")
def get_documents_by_name_reads_one_order(client, site):
    name = client.find_by_reference("Purchase Order", "CT-REF-O2").name
    docs = client.get_documents("Purchase Order", name=name)
    eq([d["name"] for d in docs], [name])
    d = docs[0]
    eq(d["docstatus"], 1, "submitted")
    eq(d["party_id"], site.n("s1"), "supplier")
    eq(d["approved_by"], "ops@example.com via desk", "approver")
    eq(_line_qty(d, site.n("steady")), 50.0, "quantity")
    near(d["lines"][0]["done_qty"], 50.0, "received")


@check("orders")
def get_documents_by_item_finds_every_order_of_it_and_since_cuts_old_ones(client, site):
    every = {d["party_id"] for d in client.get_documents("Purchase Order", item_code=site.n("steady"), limit=100)}
    eq(every, {site.n("s1"), site.n("s2"), site.n("s4")}, "suppliers of steady")
    recent = client.get_documents("Purchase Order", item_code=site.n("steady"), since=site.today - timedelta(days=45), limit=100)
    eq(sorted(d["party_id"] for d in recent), sorted([site.n("s1"), site.n("s2"), site.n("s4")]), "orders since 45 days ago (o2, o3, o6)")
    assert len(recent) == 3 and len(client.get_documents("Purchase Order", item_code=site.n("steady"), limit=100)) == 4


@check("orders")
def get_documents_open_only_leaves_out_finished_orders(client, site):
    every = {d["name"] for d in client.get_documents("Purchase Order", item_code=site.n("steady"), limit=100)}
    still_open = {d["name"] for d in client.get_documents("Purchase Order", item_code=site.n("steady"), limit=100, open_only=True)}
    eq(len(every - still_open), 1, "exactly one order of steady is closed and left out")
    closed = (every - still_open).pop()
    eq([d["status"] for d in client.get_documents("Purchase Order", name=closed)], ["Closed"], "it is the closed one")


@check("orders")
def get_documents_limit_is_honoured(client, site):
    eq(len(client.get_documents("Purchase Order", item_code=site.n("steady"), limit=2)), 2)


# ---------------------------------------------------------------------------------------------------------------------- creating drafts, and the duplicate guard
@check(writes=True)
def create_purchase_order_makes_a_draft_with_the_reference_and_the_approver(client, site):
    ref = unique("CT-W-PO")
    made = client.create_purchase_order(_draft(site, ref))
    try:
        assert made.ok and made.name, f"not created: {made.error}"
        doc = client.get_documents("Purchase Order", name=made.name)[0]
        eq(doc["docstatus"], 0, "a draft, never submitted")
        eq(doc["party_id"], site.n("s1"), "supplier")
        eq(_line_qty(doc, site.n("writes")), 5.0, "quantity")
        eq(doc["approved_by"], "tester@example.com via desk", "approver")
        found = client.find_by_reference("Purchase Order", ref)
        assert found is not None and found.name == made.name, "found again by its reference"
        eq(found.approved_by, "tester@example.com via desk", "approver found again")
    finally:
        _clean_order(client, site, made.name, ref)


@check(writes=True)
def pressing_create_twice_gives_one_order(client, site):
    ref = unique("CT-W-TWICE")
    first = client.create_purchase_order(_draft(site, ref))
    second = client.create_purchase_order(_draft(site, ref))
    try:
        assert first.ok and second.ok, f"{first.error} / {second.error}"
        eq(second.name, first.name, "the second press returns the first order")
        mine = [d for d in client.get_documents("Purchase Order", item_code=site.n("writes"), limit=200) if d["name"] == first.name]
        eq(len(mine), 1)
    finally:
        _clean_order(client, site, first.name, ref)


@check("submit", writes=True)
def a_cancelled_order_and_its_replacement_share_a_reference_and_the_replacement_is_accepted(client, site):
    ref = unique("CT-W-AGAIN")
    first = client.create_purchase_order(_draft(site, ref))
    assert first.ok, first.error
    second = None
    try:
        site.submit("Purchase Order", first.name)
        cancelled = client.cancel_purchase_order(first.name, ref)
        assert cancelled.ok, cancelled.error
        eq([d["docstatus"] for d in client.get_documents("Purchase Order", name=first.name)], [2], "the first is cancelled")
        second = client.create_purchase_order(_draft(site, ref, qty=6.0))
        assert second.ok and second.name != first.name, f"the replacement was refused or is the cancelled order: {second}"
        found = client.find_by_reference("Purchase Order", ref)
        assert found is not None and found.name == second.name, "a lookup finds the replacement, not the cancelled order"
    finally:
        _clean_order(client, site, first.name, ref)
        if second is not None:
            _clean_order(client, site, second.name, ref)


@check("orders", writes=True)
def an_order_made_by_hand_is_left_alone(client, site):
    hand = _order_by_hand(client, site)
    before = _line_qty(hand, site.n("steady"))
    for call in (lambda: client.cancel_purchase_order(hand["name"], "CT-REF-O2"), lambda: client.update_purchase_order_qty(hand["name"], "CT-REF-O2", site.n("steady"), before + 1)):
        res = call()
        assert not res.ok, "an order that does not carry the reference was changed"
    eq(_line_qty(client.get_documents("Purchase Order", name=hand["name"])[0], site.n("steady")), before, "untouched")
    ref = unique("CT-W-OTHER")
    other = client.create_purchase_order(_draft(site, ref))
    try:
        assert other.ok and other.name != hand["name"], "a new request makes its own order"
    finally:
        _clean_order(client, site, other.name, ref)


@check(writes=True)
def create_material_request_makes_a_draft_and_is_safe_to_repeat(client, site):
    ref = unique("CT-W-MR")
    draft = RequisitionDraft(item_code=site.n("writes"), qty=8.0, uom="Nos", required_by=site.today + timedelta(days=10), rate=12.0, note="Recommended: Contract reliable supplier",
                             reference=ref, approved_by="tester@example.com via desk")
    first = client.create_material_request(draft)
    second = client.create_material_request(draft)
    try:
        assert first.ok and first.name, first.error
        eq(second.name, first.name, "repeating returns the same requisition")
        doc = client.get_documents("Material Request", name=first.name)[0]
        eq(doc["docstatus"], 0, "a draft")
        eq(doc["kind"], "Purchase", "a purchase requisition")
        eq(doc["approved_by"], "tester@example.com via desk", "approver")
        found = client.find_by_reference("Material Request", ref)
        assert found is not None and found.name == first.name
    finally:
        if site.discard:
            site.discard("Material Request", first.name)


@check("stock", writes=True)
def create_stock_transfer_makes_a_draft_and_refuses_the_same_warehouse(client, site):
    create = need(client, "create_stock_transfer")
    ref = unique("CT-W-ST")
    draft = StockTransferDraft(item_code=site.n("writes"), qty=2.0, uom="Nos", from_warehouse=site.n("wh_a"), to_warehouse=site.n("wh_b"), remarks="contract check",
                               reference=ref, approved_by="tester@example.com via desk")
    made = create(draft)
    try:
        assert made.ok and made.name, made.error
        again = create(draft)
        eq(again.name, made.name, "repeating returns the same transfer")
        found = client.find_by_reference("Stock Entry", ref)
        assert found is not None and found.name == made.name, "found again by its reference"
        eq(found.approved_by, "tester@example.com via desk", "approver")
        same = create(StockTransferDraft(item_code=site.n("writes"), qty=1.0, uom="Nos", from_warehouse=site.n("wh_a"), to_warehouse=site.n("wh_a"), reference=unique("CT-W-ST")))
        assert not same.ok, "a transfer to the same warehouse was made"
    finally:
        if site.discard:
            site.discard("Stock Entry", made.name)


# ---------------------------------------------------------------------------------------------------------------------- changing and cancelling what this application made
@check(writes=True)
def update_purchase_order_qty_changes_a_draft(client, site):
    ref = unique("CT-W-QTY")
    made = client.create_purchase_order(_draft(site, ref, qty=5.0))
    try:
        assert made.ok, made.error
        res = client.update_purchase_order_qty(made.name, ref, site.n("writes"), 9.0)
        assert res.ok, res.error
        eq(res.submitted, False, "still a draft")
        eq(_line_qty(client.get_documents("Purchase Order", name=made.name)[0], site.n("writes")), 9.0, "new quantity")
    finally:
        _clean_order(client, site, made.name, ref)


@check("submit", writes=True)
def update_purchase_order_qty_changes_a_submitted_order_and_keeps_it_submitted(client, site):
    ref = unique("CT-W-QTYS")
    made = client.create_purchase_order(_draft(site, ref, qty=5.0))
    try:
        assert made.ok, made.error
        site.submit("Purchase Order", made.name)
        res = client.update_purchase_order_qty(made.name, ref, site.n("writes"), 7.0)
        assert res.ok, res.error
        eq(res.submitted, True, "still submitted")
        doc = client.get_documents("Purchase Order", name=made.name)[0]
        eq((doc["docstatus"], _line_qty(doc, site.n("writes"))), (1, 7.0), "submitted, new quantity")
    finally:
        _clean_order(client, site, made.name, ref)


@check(writes=True)
def cancel_purchase_order_deletes_a_draft_and_a_second_try_finds_it_gone(client, site):
    ref = unique("CT-W-DEL")
    made = client.create_purchase_order(_draft(site, ref))
    assert made.ok, made.error
    res = client.cancel_purchase_order(made.name, ref)
    assert res.ok, res.error
    eq(client.get_documents("Purchase Order", name=made.name), [], "the draft is gone")
    again = client.cancel_purchase_order(made.name, ref)
    assert again.ok, "an order that is already gone is a success: nothing is left to remove"


@check("submit", writes=True)
def cancel_purchase_order_cancels_a_submitted_one(client, site):
    ref = unique("CT-W-CAN")
    made = client.create_purchase_order(_draft(site, ref))
    assert made.ok, made.error
    try:
        site.submit("Purchase Order", made.name)
        res = client.cancel_purchase_order(made.name, ref)
        assert res.ok, res.error
        eq([d["docstatus"] for d in client.get_documents("Purchase Order", name=made.name)], [2], "cancelled")
    finally:
        _clean_order(client, site, made.name, ref)


@check("orders", writes=True)
def cancel_purchase_order_refuses_an_order_with_another_reference(client, site):
    name = client.find_by_reference("Purchase Order", "CT-REF-O2").name
    res = client.cancel_purchase_order(name, "CT-REF-SOMEONE-ELSE")
    assert not res.ok, "an order was cancelled by a request that did not make it"
    eq([d["docstatus"] for d in client.get_documents("Purchase Order", name=name)], [1], "still submitted")

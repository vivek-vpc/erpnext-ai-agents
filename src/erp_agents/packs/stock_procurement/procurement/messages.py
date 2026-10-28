"""Owner-facing text for a purchase-order proposal.

Like the inventory messages, every sentence is filled in from the stored ranking and stock numbers, so the
explanation cannot disagree with the data.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

from erp_agents.core.approvals.models import ACTION_DETAILS, ApprovalOption, ApprovalRequest, Section
from ..settings import ProcurementSettings
from ..inventory.analysis import _d, _n
from ..templating import fill_template
from .scoring import FACTORS, Need, Ranking, ScoredVendor

KIND_PO = "procurement.po"
KIND_PR = "procurement.pr"             # a purchase REQUISITION (an ERPNext Material Request) is created first for this item
KIND_MANUAL = "procurement.manual"
KIND_ORDER = "procurement.order"       # sent after Create order: the draft that now exists in the ERP, with the buttons to change or cancel it

FACTOR_LABEL = {"cost": "Total cost", "delivery": "Delivery time", "reliability": "Reliability", "terms": "Payment terms"}


def _money(x: Optional[float]) -> str:
    return "unknown" if x is None else f"{x:,.2f}"


def price_source_text(v: Any) -> str:
    """Where a vendor's price comes from, in words (works on a ScoredVendor or on the stored dict of one)."""
    get = (lambda k: v.get(k)) if isinstance(v, dict) else (lambda k: getattr(v, k, None))
    if get("price_source") == "quotation":
        until = get("quote_valid_till")
        return f"supplier quotation {get('quote_ref')}" + (f", valid until {_d(until)}" if until else ", with no end date")
    return "the average of past orders" if get("price_source") == "average of past orders" else "the last order on record"


def expected_arrival(today: date, lead_days: Optional[float]) -> Optional[date]:
    return today + timedelta(days=round(lead_days)) if lead_days is not None else None


def vendor_note_for(wo: dict[str, Any], best: ScoredVendor, need: Need, settings: ProcurementSettings, today: date, reference: str) -> str:
    """The note that goes on the purchase order for the vendor: the owner's template filled from the proposal, plus two
    fixed sentences only when a recorded fact calls for them (delivery after stock-out; price above the vendor's own average)."""
    if not settings.include_vendor_note:
        return ""
    arrival = expected_arrival(today, best.lead_days)
    values = {
        "item": wo["item_name"], "item_code": wo["item_code"], "qty": _n(need.qty), "uom": need.uom,
        "required_by": f"{arrival:%d %b %Y}" if arrival else "the earliest date you can",
        "reference": reference, "vendor": best.name, "unit_price": _money(best.unit_price), "total": _money(best.total),
    }
    parts = [fill_template(settings.vendor_note_template, values).strip()]
    if best.arrives_after_stockout and settings.vendor_note_urgent:
        parts.append(settings.vendor_note_urgent.strip())
    if getattr(best, "price_source", None) == "quotation" and getattr(best, "quote_ref", None):
        parts.append(f"The price is as per your quotation {best.quote_ref}.")
    if any("above their average" in f for f in best.flags):
        parts.append(f"Please confirm the unit price of {_money(best.unit_price)}: it is above your usual price.")
    return " ".join(p for p in parts if p)


def route_for(settings: ProcurementSettings, item_code: str, item_group: Optional[str]) -> str:
    """'requisition' when the owner wants a purchase requisition first for this item (by its code or its group), else 'order'."""
    if item_code in settings.requisition_first_items or (item_group and item_group in settings.requisition_first_groups):
        return "requisition"
    return "order"


def requisition_note(wo: dict[str, Any], best: ScoredVendor, need: Need, today: date, approved_by: str = "") -> str:
    """What the buyer who turns the requisition into an order should know: the vendor the assistant recommends and why it is a fair choice."""
    arrival = expected_arrival(today, best.lead_days)
    parts = [f"Recommended vendor: {best.name}" + (f" ({best.country})" if best.country else "") + f", {_money(best.unit_price)} each, "
             f"{_money(best.total)} in total" + (" including duties and taxes" if best.tax_lines else "") + f" for {_n(need.qty)} {need.uom}."]
    if getattr(best, "price_source", None) == "quotation":
        parts.append(f"The price is from {price_source_text(best)}.")
    if best.lead_days is not None:
        parts.append(f"Delivery in about {_n(best.lead_days)} days" + (f" (around {arrival:%d %b %Y})." if arrival else "."))
    if best.on_time_rate is not None:
        parts.append(f"On time for {best.on_time_rate * 100:.0f}% of past orders.")
    if wo.get("reasons"):
        parts.append(f"Why a reorder was started: {wo['reasons'][0]}")
    return " ".join(parts)


def build_po_request(proposal_id: str, wo: dict[str, Any], ranking: Ranking, best: ScoredVendor, need: Need,
                     settings: ProcurementSettings, today: date, vendor_note: str = "", route: str = "order", revision: str = "",
                     moq: Optional[float] = None, replaces: Optional[str] = None) -> ApprovalRequest:
    """`revision` (the owner changed the quantity or the vendor): said first in the recommendation, so nobody mistakes the new request for the old one."""
    pr = route == "requisition"
    u, name, qty = need.uom, wo["item_name"], need.qty
    arrival = expected_arrival(today, best.lead_days)
    on_hand, rate, days_left, stockout = wo.get("on_hand"), wo.get("daily_rate"), need.cover_days, wo.get("stockout_date")

    taxed = bool(best.tax_lines)
    if taxed:
        headline = f"Order {_n(qty)} {u} of {name} from {best.name} for {_money(best.goods_total)} ({_money(best.total)} in total with duties and taxes)."
        rec = [f"Order {_n(qty)} {u} of {name} from {best.name} at {_money(best.unit_price)} each: goods {_money(best.goods_total)}; "
               f"with duties and taxes the total cost is {_money(best.total)}."]
        counted = [l for l in best.tax_lines if l["in_cost"] or best.tax_not_cost == 0]
        left_out = [l for l in best.tax_lines if not l["in_cost"] and best.tax_not_cost != 0]
        src = f" from your ERP's template '{best.tax_template}'" + (" (chosen by a Tax Rule)" if best.tax_source == "tax rule" else " (the default template)" if best.tax_source == "default template" else "") if best.tax_template else ""
        if counted:
            rec.append(f"Duties and taxes counted{src}: " + "; ".join(f"{l['name']} {_money(l['amount'])} ({l['description']})" for l in counted) + ".")
        if left_out:
            rec.append("Payable but not part of the item cost (not counted): " + "; ".join(f"{l['name']} {_money(l['amount'])}" for l in left_out) + ".")
    else:
        headline = f"Order {_n(qty)} {u} of {name} from {best.name} for {_money(best.total)}."
        rec = [f"Order {_n(qty)} {u} of {name} from {best.name} at {_money(best.unit_price)} each: total {_money(best.total)}."]
    if pr:
        headline = f"Request {_n(qty)} {u} of {name}: the best vendor is {best.name}, {_money(best.total)} in total" + (" with duties and taxes." if taxed else ".")
        rec[0] = (f"Create a purchase requisition for {_n(qty)} {u} of {name}. The best vendor is {best.name} at {_money(best.unit_price)} each: "
                  + (f"goods {_money(best.goods_total)}; with duties and taxes the total cost is {_money(best.total)}." if taxed else f"total {_money(best.total)}."))
    if revision:
        rec.insert(0, revision)
    rec.append(f"Expected delivery in about {_n(best.lead_days)} days" + (f" (around {_d(arrival.isoformat())})" if arrival else "") + ".")
    if days_left is not None and best.lead_days is not None:
        gap = days_left - best.lead_days
        rec.append(f"That is {_n(abs(gap))} days {'before' if gap >= 0 else 'AFTER'} you run out"
                   + (f" (stock-out around {_d(stockout)})." if stockout else "."))
    if pr:
        rec.append("Approving creates a DRAFT purchase requisition (a Material Request) in your ERP; your buyer turns it into an order there."
                   if settings.po_mode == "draft" else "Approving only records your decision; no requisition is created automatically.")
    else:
        rec.append("Approving creates a DRAFT purchase order in your ERP for you to review and submit."
                   if settings.po_mode == "draft" else "Approving only records your decision; no purchase order is created automatically.")
    if wo.get("suggested_qty_basis") and wo.get("suggested_qty") == qty:
        rec.append(f"Quantity: {wo['suggested_qty_basis']}.")
    elif moq and qty == moq and (wo.get("suggested_qty") or 0) < moq:
        rec.append(f"Quantity: {_n(qty)} {u}, the minimum order quantity set for this item in your ERP" + (f" (the assistant's own figure was {_n(wo['suggested_qty'])})." if wo.get("suggested_qty") else "."))
    elif revision:
        pass                                                  # the first line already says what was changed
    elif wo.get("suggested_qty") != qty:
        rec.append(f"Quantity: {_n(qty)} {u} (the default order size from your procurement settings).")

    stock = []
    if on_hand is not None:
        reserved = (on_hand - wo["available"]) if wo.get("available") is not None else 0
        stock.append(f"On hand: {_n(on_hand)} {u}" + (f" ({_n(reserved)} reserved, {_n(wo['available'])} available)." if reserved > 0 else "."))
    if wo.get("in_transit"):
        how = {"arriving": "counted as on its way", "shelf": "counted as on hand", "ignore": "not counted"}.get(wo.get("in_transit_mode") or "arriving", "counted as on its way")
        stock.append(f"In transit between warehouses: {_n(wo['in_transit'])} {u} ({how}, as set in your stock settings).")
    if rate:
        stock.append(f"Usage: about {_n(rate, 2)} {u}/day" + (f", so roughly {_n(days_left)} days of stock." if days_left is not None else "."))
    if wo.get("reasons"):
        stock.append(f"Why a reorder was started: {wo['reasons'][0]}")

    w = ranking.weights
    n = len(ranking.eligible)
    top = ranking.eligible[0] if ranking.eligible else best
    chosen = top.supplier != best.supplier                    # the owner picked a vendor other than the assistant's first choice
    why = ([f"You chose this vendor instead of the assistant's first choice, {top.name} (score {top.score:.0f} of 100). This vendor scores {best.score:.0f} of 100."] if chosen
           else [f"Score {best.score:.0f} of 100, the highest of {n} vendors compared." if n > 1
                 else f"Score {best.score:.0f} of 100. It is the only vendor with enough history to compare."])
    why.append((f"Total cost {_money(best.total)} (goods plus duties and taxes)" if taxed else f"Total cost {_money(best.total)}")
               + _rank_note(best, ranking.eligible, "total", lowest=True) + ".")
    why.append(f"Delivery {_n(best.lead_days)} days" + (" (assumed, no history)" if best.lead_assumed else "")
               + _rank_note(best, ranking.eligible, "lead_days", lowest=True) + ".")
    why.append("On-time record " + (f"{best.on_time_rate * 100:.0f}%" if best.on_time_rate is not None else "unknown") + ".")
    if best.payment_days is not None:
        why.append(f"Credit terms {_n(best.payment_days, 0)} days.")
    if best.listed and settings.prefer_listed_suppliers:
        why.append("Listed as a supplier on the item (small bonus applied).")
    if settings.use_quotations != "off":
        why.append(f"Price from {price_source_text(best)}.")

    others = [f"{v.name}: " + (v.why_not or (f"the assistant's first choice (score {v.score:.0f} of 100)" if v.supplier == top.supplier else "")) for v in ranking.vendors if v.supplier != best.supplier]
    scored_how = [
        "Weights: " + ", ".join(f"{FACTOR_LABEL[k].lower()} {w[k]:.0f}%" for k in FACTORS) + ".",
        f"Based on past orders and receipts in your ERP over the last {settings.history_days} days.",
    ]
    if settings.use_quotations == "prefer":
        scored_how.append("Prices: a vendor's valid supplier quotation is used instead of its last order price; vendors without one use their last order.")
    elif settings.use_quotations == "only":
        scored_how.append("Prices: only vendors with a valid supplier quotation were compared, and the price is the one they quoted.")
    notes = [f"{best.name}: {f}." for f in best.flags] + list(ranking.notes)

    sections = [Section(heading="Recommendation", lines=rec)]
    if stock:
        sections.append(Section(heading="Stock position", lines=stock))
    sections += [Section(heading="Why this vendor", lines=why)]
    if others:
        sections.append(Section(heading="Other vendors", lines=others))
    sections.append(Section(heading="How vendors were scored", lines=scored_how))
    if vendor_note and settings.po_mode == "draft" and not pr:
        sections.append(Section(heading="Note to vendor", lines=[
            vendor_note, "This goes on the purchase order the vendor receives. You can change the wording in Configuration → Procurement."]))
    if notes:
        sections.append(Section(heading="Notes", lines=notes))

    return ApprovalRequest(
        kind=KIND_PR if pr else KIND_PO,
        title=(f"Purchase requisition: {name} ({wo['item_code']})" if pr else f"Purchase order: {name} ({wo['item_code']}) from {best.name}"), headline=headline,
        severity=wo.get("severity", "HIGH"), sections=sections, details=build_details(ranking, need),
        options=_po_options(settings, route, replaces), ref_type="procurement_proposal", ref_id=proposal_id,
        facts={"item_code": wo["item_code"], "supplier": best.supplier, "qty": qty, "total": best.total, "unit_price": best.unit_price},
    )


def build_order_request(proposal_id: str, po_name: str, wo: dict[str, Any], supplier: Optional[str], supplier_name: Optional[str], qty: Optional[float], total: Optional[float],
                        submitted: bool = False, changed: str = "") -> ApprovalRequest:
    """Sent after Create order (and again after its quantity was changed): the order that now exists in the ERP, and what can still be done to it from here.
    Nothing is asked of anyone: 'Keep it' only closes it. `changed` = what was just changed, said first."""
    u, name, code = wo.get("uom", "units"), wo.get("item_name") or wo.get("item_code", ""), wo.get("item_code", "")
    what = f"{_n(qty)} {u} of {name} ({code}) from {supplier_name or supplier}, {_money(total)} in total"
    kind = "Purchase order" if submitted else "Draft purchase order"
    state = ("It is submitted. If you have already sent it to the vendor, tell them about any change." if submitted
             else "It is only a draft: open it in ERPNext, check it and submit it. Nothing has been sent to the vendor.")
    sections = [
        Section(heading="The order", lines=[*([changed] if changed else []), f"{kind} {po_name} is in ERPNext: {what}.", state]),
        Section(heading="If it is not right", lines=[
            "Change quantity: the quantity is changed in this same order in ERPNext (draft or submitted); the vendor and the price stay as they are. "
            "It cannot go below what has already been received or invoiced.",
            "Change vendor: an order's vendor cannot be changed, so the vendors are checked again and you get a revised request. When you press Create order on it, "
            "this order is replaced by the new one; if you reject it, this order stays as it is.",
            "Cancel order: a draft is deleted in ERPNext, a submitted order is cancelled (only if nothing has arrived or been invoiced). You are asked to confirm first."]),
    ]
    options = [
        ApprovalOption(action="change_qty", label="Change quantity", terminal=False, description="Change the quantity in this same order"),
        ApprovalOption(action="change_vendor", label="Change vendor", terminal=False, description="Choose another of the vendors that were compared"),
        ApprovalOption(action="cancel_order", label="Cancel order", style="danger", terminal=False, description="Delete the draft in ERPNext (you are asked to confirm)"),
        ApprovalOption(action="keep", label="Keep it", style="primary", description="The order is fine as it is"),
    ]
    return ApprovalRequest(kind=KIND_ORDER, title=f"{kind} {po_name}: {name} ({code})", headline=f"{po_name}: {what}.", severity="INFO", sections=sections, options=options,
                           ref_type="procurement_proposal", ref_id=proposal_id,
                           facts={"item_code": code, "supplier": supplier, "qty": qty, "total": total, "po_name": po_name, "proposal_id": proposal_id})


def build_manual_request(proposal_id: str, wo: dict[str, Any], problem: str, ranking: Optional[Ranking],
                         settings: ProcurementSettings) -> ApprovalRequest:
    """No automatic recommendation is possible (no priced vendor, or no quantity): tell the owner and why."""
    name = wo["item_name"]
    lines = [problem]
    if wo.get("suggested_qty_basis"):
        lines.append(f"Reorder context: {wo['suggested_qty_basis']}")
    if wo.get("reasons"):
        lines.append(f"Why a reorder was started: {wo['reasons'][0]}")
    sections = [Section(heading="What is needed", lines=lines)]
    if ranking and ranking.vendors:
        sections.append(Section(heading="Vendors checked", lines=[f"{v.name}: {v.why_not or 'eligible'}" for v in ranking.vendors]))
    return ApprovalRequest(
        kind=KIND_MANUAL, title=f"Needs your decision: {name} ({wo['item_code']})", headline=f"{name}: {problem}",
        severity=wo.get("severity", "HIGH"), sections=sections,
        options=[
            ApprovalOption(action="ack", label="Got it", style="primary", description="I will handle this in the ERP"),
            ApprovalOption(action="snooze", label=f"Remind in {settings.snooze_days}d", description=f"Look again in {settings.snooze_days} days"),
        ],
        ref_type="procurement_proposal", ref_id=proposal_id, facts={"item_code": wo["item_code"]},
    )


def _po_options(s: ProcurementSettings, route: str = "order", replaces: Optional[str] = None) -> list[ApprovalOption]:
    """The first three are always approve, snooze, dismiss, in that order: WhatsApp's quick-reply template buttons are matched to
    them by position. Anything extra comes after. `replaces` = the request changes an order that already exists: pressing Create order swaps it, and rejecting leaves it alone."""
    pr = route == "requisition"
    options = [
        ApprovalOption(action="approve", label="Create requisition" if pr else "Create order", style="primary",
                       description=(f"Replace draft {replaces} with this order" if replaces else "Create the draft purchase requisition" if pr else "Create the draft purchase order") if s.po_mode == "draft" else "Record the approval"),
        ApprovalOption(action="snooze", label=f"Remind in {s.snooze_days}d", description="Keep the current order as it is" if replaces else f"Ask again in {s.snooze_days} days"),
        ApprovalOption(action="dismiss", label="Reject", style="danger", description=f"Do not replace it: draft {replaces} stays as it is" if replaces else "Do not order; the reorder request is closed"),
    ]
    if not pr and s.po_mode == "draft" and not replaces:
        options.append(ApprovalOption(action="requisition", label="Requisition instead", description="Create a purchase requisition for your buyer instead of an order"))
    options.append(ApprovalOption(action="change_qty", label="Change quantity", terminal=False, description="Order a different quantity: the vendors are checked again"))
    options.append(ApprovalOption(action="change_vendor", label="Change vendor", terminal=False, description="Choose another of the vendors that were compared"))
    options.append(ApprovalOption(action=ACTION_DETAILS, label="Details", terminal=False, description="Show every vendor's figures and scores"))
    return options


def _rank_note(best: ScoredVendor, vendors: list[ScoredVendor], attr: str, lowest: bool) -> str:
    vals = sorted(getattr(v, attr) for v in vendors if getattr(v, attr) is not None)
    if len(vals) < 2:
        return ""
    if getattr(best, attr) == vals[0] and lowest:
        return ", the lowest" if attr == "total" else ", the fastest"
    return ""


def build_details(ranking: Ranking, need: Need) -> list[Section]:
    rows = []
    for v in ranking.vendors:
        if not v.eligible:
            rows.append(f"{v.name} ({v.supplier}): not scored - {v.excluded_reason}.")
            continue
        s = v.scores
        landed = (f"goods {_money(v.goods_total)} + duties and taxes {_money(v.tax_total)} = total {_money(v.total)}"
                  + (f" (payable but not item cost {_money(v.tax_not_cost)}, not counted)" if v.tax_not_cost else "")
                  if v.tax_lines else f"total {_money(v.total)}")
        origin = f", {v.country}" if v.country else ""
        rows.append(
            f"{v.name} ({v.supplier}{origin}): price {_money(v.unit_price)}" + (f" (from {price_source_text(v)})" if v.price_source == "quotation" else "") + f", {landed}, lead {_n(v.lead_days)}d"
            f"{' (assumed)' if v.lead_assumed else ' (as quoted)' if v.lead_from_quote else ''}, on-time "
            f"{f'{v.on_time_rate * 100:.0f}%' if v.on_time_rate is not None else 'n/a'}, credit {_n(v.payment_days or 0, 0)}d, "
            f"{v.orders} past order line(s). Scores: cost {s['cost']:.0f}, delivery {s['delivery']:.0f}, "
            f"reliability {s['reliability']:.0f}, terms {s['terms']:.0f} -> {v.score:.0f}."
        )
    cover = f"{_n(need.cover_days)} days" if need.cover_days is not None else "unknown"
    taxed = [v for v in ranking.vendors if v.eligible and v.tax_lines]
    tax_rows = [f"{v.name} (ERP template '{v.tax_template}', {v.tax_source}): " + "; ".join(
        f"{l['name']} {_money(l['amount'])} ({l['description']}; ERP category {l['category']}" + ("" if l["in_cost"] else ", not item cost") + ")"
        for l in v.tax_lines) + "." for v in taxed]
    return [
        Section(heading="Every vendor", lines=rows),
        *([Section(heading="Duties and taxes by vendor", lines=tax_rows)] if tax_rows else []),
        Section(heading="How the score works", lines=[
            "Cost 100 x cheapest total / this total. Delivery 100 x fastest lead time / this lead time (capped at 40 if it arrives after you run out).",
            "Reliability is the on-time share; terms 100 x credit days / longest credit days. Combined with your weights.",
            f"Quantity {_n(need.qty)} {need.uom}; days of cover at the time of the request: {cover}.",
        ]),
    ]

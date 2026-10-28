"""What the purchasing follow-up tells a person, and what it asks them to answer. Template text filled from ERP data and the program's own numbers, never freehand.

None of these requests carries a vendor's e-mail address or phone number: the person sees how a message would be sent (by e-mail, by WhatsApp), and the address is
looked up only when they press Send (followup/vendors.py).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from erp_agents.core.approvals.models import ACTION_DETAILS, ApprovalOption, ApprovalRequest, Section
from ..inventory.analysis import _d, _n
from ..templating import fill_template

KIND_CHASE = "procurement.chase"
KIND_RECEIPT = "procurement.receipt_check"
KIND_QUOTE_REQUEST = "procurement.quote_request"
KIND_QUOTES = "procurement.quotes"

HOW = {"email": "by e-mail through ERPNext, to the address on the supplier's contact", "whatsapp": "by WhatsApp, to the mobile number on the supplier's contact"}


def _details() -> ApprovalOption:
    return ApprovalOption(action=ACTION_DETAILS, label="Details", terminal=False, description="Show everything this is based on")


def fill(template: str, values: dict[str, Any]) -> str:
    return " ".join(fill_template(template, values).split())


# ===================================================================== late deliveries
def effect_line(e: dict[str, Any]) -> str:
    """How the late delivery affects one item's stock cover."""
    u, item = e["uom"], f"{e['item_name']} ({e['item_code']})"
    if e.get("error"):
        return f"{item}: the stock could not be checked ({e['error']})."
    if not e.get("daily_use"):
        return f"{item}: no recent use, so the delay does not put stock at risk ({_n(e['available'])} {u} available)."
    if e["available"] <= 0:
        return (f"{item}: already out of stock ({_n(e['available'])} {u} available) - every day of delay is about {_n(e['daily_use'], 1)} {u} you cannot supply. "
                f"The {_n(e['still'])} {u} still to come would last about {_n(e['cover_with'])} days.")
    line = f"{item}: stock on hand lasts about {_n(e['cover_days'])} days (until {_d(e['runs_out_on'])}) at {_n(e['daily_use'], 1)} {u}/day."
    if e["cover_days"] < e.get("lead_days", 0):
        line += f" That is less than a new order would take ({_n(e['lead_days'])} days), so this delivery matters."
    return line + f" With the {_n(e['still'])} {u} still to come you would have about {_n(e['cover_with'])} days."


def build_chase_request(order: dict[str, Any], effects: list[dict[str, Any]], message: str, route: Any, sends: int, cfg: Any) -> ApprovalRequest:
    urgent = any(e.get("daily_use") and (e["available"] <= 0 or e["cover_days"] <= max(order["days_late"], 3)) for e in effects)
    lines = [f"{l['qty']:g} {l['uom']} of {l['item_name']} ({l['item_code']}): {_n(l['received'])} received, {_n(l['still'])} still to come." for l in order["lines"]]
    situation = [f"Purchase order {order['document']} from {order['vendor']} was due on {_d(order['due'])} and is {order['days_late']} days late."] + lines
    if sends:
        situation.append(f"The vendor has been reminded {sends} time{'s' if sends != 1 else ''} already.")
    sections = [Section(heading="Situation", lines=situation), Section(heading="Effect on your stock", lines=[effect_line(e) for e in effects] or ["No item lines to check."])]
    options = []
    if route.channel != "none" and sends < cfg.chase_max:
        sections.append(Section(heading="Message to the vendor", lines=[f"Sent {HOW[route.channel]}, only if you press Send reminder:", f"\"{message}\""]))
        options.append(ApprovalOption(action="send", label="Send reminder", style="primary", description="Send this message to the vendor now"))
    elif route.channel == "none":
        sections.append(Section(heading="Message to the vendor", lines=[f"No reminder can be sent: {route.why}. Call them, or add the contact in ERPNext."]))
    else:
        sections.append(Section(heading="Message to the vendor", lines=[f"They have had {sends} reminders, the most your settings send. Call them, or look for another vendor."]))
    options += [ApprovalOption(action="later", label="Ask me later", description=f"Ask again in {cfg.chase_every_days} days if it is still late"),
                ApprovalOption(action="dismiss", label="Stop chasing", style="danger", description="Do not ask about this order again"), _details()]
    first = order["lines"][0]["item_code"] if order["lines"] else None
    return ApprovalRequest(kind=KIND_CHASE, title=f"Late delivery: {order['document']} from {order['vendor']}, {order['days_late']} days late",
                           headline=f"{order['document']} from {order['vendor']} is {order['days_late']} days late" + (" and you are running short" if urgent else ""),
                           severity="HIGH" if urgent else "INFO", sections=sections, options=options, ref_type="purchase_order", ref_id=order["document"],
                           details=[Section(heading="Order", lines=[f"Ordered on {_d(order.get('date'))}; status in ERPNext: {order.get('state') or 'open'}."])],
                           facts={"po": order["document"], "supplier": order["supplier"], "item_code": first, "items": [l["item_code"] for l in order["lines"]],
                                  "days_late": order["days_late"]})


# ===================================================================== goods receipt checks
def build_receipt_request(receipt: str, vendor: str, posted: Optional[date], problems: list[dict[str, Any]], payment: str) -> ApprovalRequest:
    lines = [p["text"] for p in problems]
    sections = [Section(heading="What does not match", lines=lines), Section(heading="Before you pay", lines=[payment])]
    kinds = sorted({p["kind"] for p in problems})
    return ApprovalRequest(kind=KIND_RECEIPT, title=f"Goods receipt {receipt} from {vendor} does not match the order",
                           headline=f"{receipt} from {vendor}: {len(problems)} difference{'s' if len(problems) != 1 else ''} with the purchase order ({', '.join(kinds)})",
                           severity="HIGH", sections=sections, ref_type="purchase_receipt", ref_id=receipt,
                           details=[Section(heading="Receipt", lines=[f"Posted on {_d(posted.isoformat()) if posted else 'n/a'}; prices compared in the order's currency."])],
                           options=[ApprovalOption(action="ack", label="Checked", style="primary", description="I have looked at it"), _details()],
                           facts={"receipt": receipt, "item_code": problems[0].get("item_code") if problems else None, "problems": kinds})


# ===================================================================== quote requests
def build_quote_request(round_id: str, item: Any, qty: float, vendors: list[dict[str, Any]], message: str, reply_by: date, reason: str) -> ApprovalRequest:
    can = [v for v in vendors if v["route"] != "none"]
    lines = [f"{v['name']}: {HOW[v['route']]}" + (f" ({v['why_listed']})" if v.get("why_listed") else "") if v["route"] != "none" else
             f"{v['name']}: cannot be asked - {v['why']}" for v in vendors]
    sections = [Section(heading="Who would be asked", lines=lines),
                Section(heading="The message", lines=[f"\"{message}\"", f"Replies are due by {_d(reply_by.isoformat())}. The prices are read from Supplier Quotations in ERPNext "
                                                      "(the vendor can enter them from your e-mail, or you enter what they reply); the comparison comes to you then."])]
    if reason:
        sections.insert(0, Section(heading="Why", lines=[reason]))
    options = ([ApprovalOption(action="send", label="Send requests", style="primary", description=f"Ask these {len(can)} vendors for a price now")] if can else [])
    options += [ApprovalOption(action="dismiss", label="Cancel", style="danger", description="Ask nobody"), _details()]
    return ApprovalRequest(kind=KIND_QUOTE_REQUEST, title=f"Ask {len(can)} vendors for a price: {_n(qty)} {item.uom} of {item.name} ({item.code})",
                           headline=f"Ask {', '.join(v['name'] for v in can) or 'nobody (no contact on file)'} for a price on {item.code}", severity="INFO",
                           sections=sections, options=options, ref_type="quote_round", ref_id=round_id,
                           facts={"item_code": item.code, "qty": qty, "round": round_id, "vendors": [v["supplier"] for v in vendors]})


def build_quotes_request(round_id: str, item: Any, qty: float, ranking: Any, asked: list[dict[str, Any]], missing: list[str], reply_by: date) -> ApprovalRequest:
    rows, best = [], ranking.best
    for v in ranking.vendors:
        if v.eligible:
            rows.append(f"{v.name}: {_n(v.unit_price, 2)} each, {_n(v.total, 2)} in total" + (f" with {_n(v.tax_total, 2)} duties and taxes" if v.tax_total else "")
                        + (f", delivery in about {_n(v.lead_days)} days" if v.lead_days is not None else "") + f" (quotation {v.quote_ref}" + (f", valid until {_d(v.quote_valid_till)})" if v.quote_valid_till else ")")
                        + f" - score {_n(v.score, 0)}")
        elif v.supplier in {a["supplier"] for a in asked}:
            rows.append(f"{v.name}: {v.excluded_reason}")
    sections = [Section(heading="Prices quoted", lines=rows or ["No vendor has quoted yet."])]
    if missing:
        sections.append(Section(heading="Not replied", lines=[f"{', '.join(missing)} did not quote by {_d(reply_by.isoformat())}."]))
    if best is not None:
        sections.append(Section(heading="Recommendation", lines=[f"{best.name}: the best score on price, delivery and record. Prepare order makes the usual purchase request with "
                                                                 "this vendor; nothing is ordered until you press Create order on it."]))
        options = [ApprovalOption(action="approve", label="Prepare order", style="primary", description=f"Make a purchase request with {best.name}"),
                   ApprovalOption(action="dismiss", label="Not now", style="danger", description="Keep the quotations; order nothing"), _details()]
    else:
        options = [ApprovalOption(action="dismiss", label="Close", description="Nobody quoted; close this round"), _details()]
    return ApprovalRequest(kind=KIND_QUOTES, title=f"Prices for {_n(qty)} {item.uom} of {item.name} ({item.code})" + (f": best is {best.name}" if best else ": no quotation"),
                           headline=(f"{len(ranking.eligible)} quotation{'s' if len(ranking.eligible) != 1 else ''} for {item.code}; best {best.name} at {_n(best.unit_price, 2)} each"
                                     if best else f"No vendor quoted for {item.code}"),
                           severity="INFO", sections=sections, ref_type="quote_round", ref_id=round_id,
                           details=[Section(heading="How they were compared", lines=["The same scoring as your purchase requests (price with duties and taxes, delivery, on-time "
                                                                                     "record, payment terms), on the prices of their valid Supplier Quotations in ERPNext."])],
                           options=options, facts={"item_code": item.code, "qty": qty, "round": round_id, "supplier": best.supplier if best else None})

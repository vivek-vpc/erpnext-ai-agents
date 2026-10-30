"""The purchasing agent: a second opinion on an order request that is waiting for a person, and answers to questions about vendors and orders.

What the model does: add a few plain observations a busy approver would want (how the quantity compares with recent use, what the vendor history says) and answer questions.
What the program does: every check that decides the verdict (is this the best vendor, does it arrive in time, is the quantity near what the analysis suggests, is something
already on order), the facts, the gate on the one tool that does anything, and the fallback. The agent never changes a request and never orders.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from ..assistant.chat import wants_order
from erp_agents.core.common import fmt_num
from erp_agents.core.assistant.verify import numbers
from erp_agents.core.prompting import MAX_QUESTION, data
from .inventory_agent import RULES
from erp_agents.core.agents.runtime import FINAL_REQUEST, AgentJob, Evidence, ToolRun, grounding_problems, substance_problems

AGENT = "purchasing"
REVIEW_TOOLS = ("stock_status", "usage_history", "vendor_options", "open_orders", "waiting_requests")
ASK_PURCHASING_TOOLS = ("stock_status", "usage_history", "vendor_options", "open_orders", "waiting_requests", "late_deliveries", "receipt_mismatches", "vendor_scorecard",
                        "decision_history")
ASK_INVENTORY_TOOLS = ("stock_overview", "stock_status", "usage_history", "open_orders", "waiting_requests", "demand_forecast", "slow_stock", "substitutes", "stock_counts",
                       "decision_history")
PREPARE = "prepare_order_request"
QUOTE = "prepare_quote_request"
QUOTE_ASK = re.compile(r"\b(rfq|request for quotation|(ask|get|request|send|collect)\b.{0,40}\b(quotes?|quotations?|prices?|offers?))\b", re.I)
VERDICT_WORDS = {"agree": "Looks reasonable", "check_first": "Worth checking first"}
PURCHASING_WORDS = re.compile(r"\b(order|orders|po|pos|purchase|purchases|requisition|requisitions|vendor|vendors|supplier|suppliers|buy|quote|quotes|quotation|price|prices|cheapest|invoice|receipt|receipts|late|delivery|deliveries|scorecard|rfq)\b", re.I)

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["points"],
    "properties": {"points": {"type": "array", "maxItems": 3, "items": {"type": "string", "minLength": 1, "maxLength": 240}}},
}
ASK_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["answerable", "answer"],
    "properties": {"answerable": {"type": "boolean"}, "answer": {"type": "string", "maxLength": 900}},
}


# ------------------------------------------------------------------------------------------------------------------- the second opinion
def review_findings(facts: dict[str, Any], ev: Evidence) -> list[dict[str, str]]:
    """The checks that decide the verdict, from the tools' results. level: 'check' (worth a look before pressing Create order) or 'note'."""
    out: list[dict[str, str]] = []
    stat = next(iter(ev.results("stock_status")), None)
    vend = next(iter(ev.results("vendor_options")), None)
    orders = next(iter(ev.results("open_orders")), None)
    qty = float(facts.get("qty") or 0)
    uom = (stat or vend or {}).get("uom", "")
    if vend and facts.get("supplier"):                 # a requisition names no vendor
        rows = vend.get("vendors") or []
        chosen = next((v for v in rows if v.get("supplier_id") == facts.get("supplier")), None)
        best = next((v for v in rows if v.get("eligible")), None)
        if chosen is None:
            out.append({"level": "check", "text": "The vendor on this request is not among the vendors that can supply this quantity now."})
        else:
            if not chosen.get("eligible"):
                out.append({"level": "check", "text": f"{chosen['vendor']} cannot be recommended for this quantity: {chosen.get('why_not') or 'it does not meet your rules'}."})
            elif best is not None and best is not chosen:
                out.append({"level": "check", "text": f"{best['vendor']} ranks higher: {fmt_num(best['total'])} in total, delivery {fmt_num(best['delivery_days'])} days, against {chosen['vendor']} at "
                                                      f"{fmt_num(chosen['total'])} and {fmt_num(chosen['delivery_days'])} days."})
            if chosen.get("arrives_after_stockout"):
                out.append({"level": "check", "text": f"{chosen['vendor']} would deliver after the stock runs out."})
    if stat and stat.get("daily_use") and qty > 0:
        out.append({"level": "note", "text": f"{fmt_num(qty)} {uom} is about {fmt_num(qty / float(stat['daily_use']), 0)} days of use at {fmt_num(stat['daily_use'], 2)} a day."})
    if stat and stat.get("suggested_qty"):
        want = float(stat["suggested_qty"])
        if qty < 0.6 * want:
            out.append({"level": "check", "text": f"The quantity {fmt_num(qty)} {uom} is well below the {fmt_num(want)} {uom} the analysis suggests."})
        elif qty > 2 * want:
            out.append({"level": "check", "text": f"The quantity {fmt_num(qty)} {uom} is more than twice the {fmt_num(want)} {uom} the analysis suggests."})
    if orders:
        coming = sum(float(d.get("still_to_come") or 0) for d in orders.get("documents", []))
        if coming > 0:
            out.append({"level": "note", "text": f"{fmt_num(coming)} {uom} of this item is already on order and still to come."})
    return out


def verdict_of(findings: list[dict[str, str]]) -> str:
    return "check_first" if any(f["level"] == "check" for f in findings) else "agree"


def review_job(facts: dict[str, Any], today: str) -> AgentJob:
    code, qty = facts["item_code"], float(facts["qty"])
    request_text = f"{fmt_num(qty)} of {code}" + (f", total {fmt_num(facts['total'])}" if facts.get("total") else "")

    def after_warm(ev: Evidence) -> None:
        ev.add(ToolRun("checked", {}, {"findings": review_findings(facts, ev)}, True))                 # the program's own figures: the model may quote them like a tool's

    def final(ev: Evidence) -> str:
        found = review_findings(facts, ev)
        done = "; ".join(f["text"] for f in found) or "nothing wrong with the vendor, the timing or the quantity"
        return (FINAL_REQUEST + " The program has already checked the request and found: " + data(done, 900) + ". The approver will see these checks, so do not repeat them. "
                "Give 0 to 3 short observations of your own that would help the approver, for example how the quantity compares with recent weekly use or what the order history shows. "
                "Do not tell the approver to approve or reject, and do not contradict the checks. An empty list is fine.")

    return AgentJob(
        agent=AGENT, task="review", tools=REVIEW_TOOLS, schema=REVIEW_SCHEMA, check=lambda d, ev: grounding_problems(d["points"], ev), final=final, after_warm=after_warm, question=request_text,
        system="You are the purchasing agent of a small business's stock assistant. An order request is waiting for a person's approval. Your job is a short, honest second opinion for the approver. "
               "You never change the request. Today is " + today + ". " + RULES,
        user=f"Review this waiting request: {data(request_text, 200)}. The tool results above show the item's stock position, the vendors compared for this quantity, and what is already on order. "
             "Use usage_history if you want to compare the quantity with recent weekly use.",
        warm=(("stock_status", {"item_code": code}), ("vendor_options", {"item_code": code, "qty": qty}), ("open_orders", {"item_code": code})))


def rules_review(facts: dict[str, Any], ev: Evidence) -> dict[str, Any]:
    found = review_findings(facts, ev)
    return {"verdict": verdict_of(found), "findings": found, "points": []}


def review_result(data: Optional[dict[str, Any]], facts: dict[str, Any], ev: Evidence) -> dict[str, Any]:
    found = review_findings(facts, ev)
    return {"verdict": verdict_of(found), "findings": found, "points": list((data or {}).get("points") or [])}


def review_text(res: dict[str, Any]) -> str:
    lines = [f"Second opinion: {VERDICT_WORDS.get(res['verdict'], res['verdict'])}."]
    lines += [f"• {f['text']}" for f in res["findings"]]
    if res["points"]:
        lines.append("AI notes (their numbers are checked against ERPNext, their wording is not):")
        lines += [f"• {p}" for p in res["points"]]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------------------- questions
def route_question(question: str) -> str:
    """Which agent takes a question: orders, vendors, prices and requests go to purchasing; everything else about stock goes to inventory."""
    return AGENT if PURCHASING_WORDS.search(question) else "inventory"


def asked_for_order(question: str) -> bool:
    return wants_order(question.lower())                  # the chat's own test of "the person asked for an order", which reads lower-case text


def asked_for_quotes(question: str) -> bool:
    """The person asked, in so many words, for vendors to be asked for prices ("get quotes for BOLT-M8", "ask the vendors for prices on 500 bolts")."""
    return bool(QUOTE_ASK.search(question))


def prepare_gate(question: str, can_prepare: bool) -> Callable[[str, dict[str, Any], Evidence], Optional[str]]:
    """The only tool that does anything is allowed when the person asked for an order in so many words, and only for what they named."""
    def gate(name: str, args: dict[str, Any], ev: Evidence) -> Optional[str]:
        if name not in (PREPARE, QUOTE):
            return None
        if name == QUOTE:
            if not can_prepare or not asked_for_quotes(question):
                return "no vendor may be asked for a price: the person did not ask for that"
            if any(r.name == QUOTE for r in ev.runs):
                return "only one quote request can be prepared per question"
        elif not can_prepare or not asked_for_order(question):
            return "no order request may be prepared: the person did not ask for one"
        elif any(r.name == PREPARE for r in ev.runs):
            return "only one order request can be prepared per question"
        code = str(args.get("item_code") or "")
        low = question.lower()
        named = code.lower() in low or any(r.name == "stock_status" and r.ok and r.result.get("item_code") == code and str(r.result.get("item_name", "")).lower() in low for r in ev.runs)
        if not named:
            return "the item must be one the person named in their request"
        if args.get("qty") is not None:
            try:
                given = float(str(args["qty"]).replace(",", ""))
            except ValueError:
                return "the quantity must be a number"
            if not any(abs(given - n) < 1e-9 for n in numbers(question)):
                return "use the quantity the person gave, or leave the quantity out to use the suggested one"
        return None
    return gate


def prepare_rewrite(question: str, asked_by: str) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    def rewrite(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name in (PREPARE, QUOTE):
            return {**args, "reason": f'{asked_by} asked for this in chat: "{question[:160]}"'}
        return args
    return rewrite


def ask_job(question: str, asked_by: str, today: str, can_prepare: bool) -> AgentJob:
    agent = route_question(question)
    tools = ASK_PURCHASING_TOOLS if agent == AGENT else ASK_INVENTORY_TOOLS
    if agent == AGENT and can_prepare and asked_for_order(question):
        tools = tools + (PREPARE,)
    if agent == AGENT and can_prepare and asked_for_quotes(question):
        tools = tools + (QUOTE,)
    role = ("the purchasing agent of a small business's stock assistant. You answer questions about vendors, prices, purchase orders and requests"
            if agent == AGENT else "the inventory agent of a small business's stock assistant. You answer questions about stock, usage and what needs reordering")
    extra = (" You may use prepare_order_request ONLY because the person asked for an order; it only sends a request for approval and orders nothing." if PREPARE in tools else "")
    extra += (" You may use prepare_quote_request ONLY because the person asked for prices from vendors; it only sends a request for approval and messages no vendor."
              if QUOTE in tools else "")

    def check(data: dict[str, Any], ev: Evidence) -> list[str]:
        if not data["answerable"]:
            return []
        return substance_problems(data["answer"], ev) or grounding_problems([data["answer"]], ev)

    return AgentJob(
        agent=agent, task="ask", tools=tools, schema=ASK_SCHEMA, check=check, question=question,
        gate=prepare_gate(question, can_prepare), rewrite=prepare_rewrite(question, asked_by),
        system="You are " + role + ". Today is " + today + "." + extra + " " + RULES,
        user=f"QUESTION: {data(question, MAX_QUESTION)}\nLook up what you need with the tools, then answer. If the tools cannot answer it, set answerable to false.")

"""Chat with the agent: ask about stock, vendor comparison, and purchase order / requisition status.

Read-only, and every answer is built from live data. The one thing it can start is an ORDER REQUEST ("create a purchase order for X", approvers only):
that prepares the usual request for the person to answer; it creates nothing in the ERP and orders nothing.

  1. UNDERSTAND the question with plain rules (which item or document is meant, what is being asked). Only when the rules cannot
     place a question, and the local AI helper is switched on, the model is asked to pick one of a fixed list of question types;
     it never chooses items, numbers or text.
  2. FETCH the data with a read-only tool (the stock analysis, the vendor ranking, ERPNext's own documents, the request inbox).
  3. ANSWER from that data with fixed sentences. Each answer carries the numbered facts it was built from.
  4. OPTIONALLY the person can ask the AI helper to explain the answer in plain words; that text is shown only if it passes the same
     checks as "Ask why" (cites the facts, no invented numbers, no guessing), otherwise they are told there is not enough information.

If something cannot be found or is ambiguous, the chat says so and asks; it never fills a gap with an assumption.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Optional, Sequence

from erp_agents.core.common import Clock, iso, untrusted, utcnow
from erp_agents.core.policy import AssistantSettings
from erp_agents.core.resilience import Limits, RateLimited, ResilienceSettings
from erp_agents.core.settings_store import SettingsStore
from erp_agents.core.storage import Database, dumps, loads
from erp_agents.core.erp.base import ERPClient, ItemInfo
from ..inventory.analysis import _d, _n, where_stock_is
from erp_agents.core.assistant.facts import Fact
from erp_agents.core.assistant.llm import LLMClient, LLMError, OllamaClient
from erp_agents.core.assistant.verify import NOT_ENOUGH, verify
from erp_agents.core.prompting import DATA_RULE, data, fact_line
from ..schema import ensure_schema

log = logging.getLogger(__name__)

MAX_CHAT_QUESTION = 300
PROMPT_VERSION = "chat-explain-v1"
FOLLOWUP_INTENTS = ("receipt_mismatches", "late_deliveries", "vendor_scorecard", "slow_stock", "substitutes", "stock_counts", "demand_forecast", "decision_history")
INTENTS = ("item_stock", "stock_overview", "vendor_compare", "document_status", "pending_approvals", *FOLLOWUP_INTENTS)
DOC_PREFIX = {"PUR-ORD": "Purchase Order", "MAT-MR": "Material Request", "MAT-PRE": "Purchase Receipt"}
DOC_NAME = re.compile(r"\b([A-Za-z]{2,5}-[A-Za-z]{2,5})-(\d{4})-(\d{3,7})\b")
ITEM_CODE_LIKE = re.compile(r"\b[A-Za-z]{2,}[A-Za-z0-9]*-[A-Za-z0-9-]*\d[A-Za-z0-9-]*\b")          # NOPE-99, fas-0003: written the way item codes are (letters, a hyphen, a digit); "re-order" is not
SCORECARD_WORDS = re.compile(r"scorecard|rank\w*|performance|on[- ]time|\brecord\b|rating|price drift")      # a question that is about how vendors perform, not about buying one item
SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}

STOP = set("a an the is are was were of for on in at to from with and or but how what which who when where why do does did can could would should i we you me my our us it its "
           "this that these those please show tell give list about any all some there status compare vs versus between than then be been being have has had much many "
           "stock vendor vendors supplier suppliers".split())
# words about the question itself, never part of an item's name
GENERIC = set("low items item running reorder out need needs attention anything everything left days day remaining available overdue open pending week month today yesterday "
              "orders order purchase requisition requisitions po pos pr prs created create approve approval waiting ok received delivered draft late delayed year".split())
STOCK_RE = re.compile(r"\b(stock|inventory|run(?:ning)? (?:out|low)|low|on hand|in hand|how (?:much|many)|days (?:left|of cover)|available|reorder|shortages?|out of stock|cover|remaining|lasts?)\b")
VENDOR_WORDS = {"compare", "comparison", "vendor", "vendors", "supplier", "suppliers", "cheapest", "quote", "quotes", "landed", "duty", "duties", "tax", "taxes"}
DOC_RE = re.compile(r"\bpurchase orders?\b|\bmaterial requests?\b|\bpurchase receipts?\b|\bgoods receipts?\b|\bpurchase requisitions?\b")
DOC_WORDS = {"po", "pos", "pr", "prs", "requisition", "requisitions", "grn"}
# An instruction, not a question: it must START with the verb ("create a purchase order for X", "raise a PO for X", "order 500 of X", "reorder X").
# Kept out of INTENTS on purpose: the AI may route a vague question only to read-only intents, never to this one.
ORDER_VERBS = {"create", "raise", "make", "place", "prepare", "generate", "draft", "start", "buy", "order", "reorder", "re-order", "new"}
ORDER_TARGET = re.compile(r"\bpurchase orders?\b|\bpos?\b|\bpurchase requisitions?\b|\brequisitions?\b|\bprs?\b|\bmaterial requests?\b|\breorder\b|\bre-order\b|\border\b|\bbuy\b")
# Taking back or changing an order that already exists: also an instruction that must START with the verb ("cancel PO PUR-ORD-2026-00012", "edit the order for BOLT-M8").
CANCEL_VERBS = {"cancel", "delete", "withdraw", "revoke", "remove"}
EDIT_VERBS = {"edit", "change", "modify", "amend", "update", "revise", "replace"}
MANAGE_TARGET = re.compile(r"\bpurchase orders?\b|\bpos?\b|\border\b")
NOT_AN_INSTRUCTION = {"status", "history", "overdue", "late", "delayed", "received", "delivered", "why", "when", "who", "happens", "happen", "if"}
# Questions answered by one of the follow-up tools (the same ones the AI agents use), checked before the general purchase-document list. First match wins.
_V = r"(?:vendors?|suppliers?)"
FOLLOWUP_RULES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("receipt_mismatches", re.compile(r"\b(?:receipts?|grns?|goods)\b.*\b(?:match\w*|mismatch\w*|differ\w*|discrepan\w*|wrong|short|excess|more than ordered|rejected|check\w*)\b"
                                      r"|\b(?:mismatch\w*|discrepanc\w*)\b")),
    ("late_deliveries", re.compile(r"\b(?:late|overdue|delayed|not (?:yet )?(?:arrived|come)|chas\w+)\b.*\b(?:deliver\w*|shipments?)\b|\b(?:deliver\w*|shipments?)\b.*\b(?:late|overdue|delayed)\b"
                                   r"|\b" + _V + r"\b.*\b(?:late|overdue|delayed)\b|\bchas\w+\b")),
    ("vendor_scorecard", re.compile(r"\bscorecard\b|\brank\w*\b.*\b" + _V + r"\b|\b(?:best|worst|most reliable|least reliable|reliable)\b.*\b" + _V + r"\b"
                                    r"|\b" + _V + r"\b.*\b(?:best|worst|reliab\w*|doing|performing|record|rating)\b|\b(?:vendor|supplier) performance\b|\bon[- ]time (?:record|rate|delivery|deliveries)\b|\bprice drift\b")),
    ("slow_stock", re.compile(r"\b(?:slow|dead)[- ]?(?:stock|moving|movers?|items?)\b|\bnot (?:moving|selling)\b|\bt(?:ie|ies|ying) up (?:cash|money|capital)\b|\b(?:excess|over)[- ]?stock\w*\b")),
    ("substitutes", re.compile(r"\b(?:substitut\w*|alternatives?|alternate)\b")),
    ("stock_counts", re.compile(r"\b(?:cycle|stock) counts?\b|\bcount(?:ing)? (?:plan|list|schedule)\b|\bwhat (?:should|to|do) (?:we|i) count\b|\bcount (?:variances?|differences?)\b")),
    ("demand_forecast", re.compile(r"\bforecast\w*\b|\bseason\w*\b|\bexpected (?:demand|usage|use|sales)\b")),
)
HISTORY_RE = re.compile(r"^(?:and )?why (?:did|was|were|have|has)\b|\b(?:decision|audit) (?:history|trail|log)\b|\bwhat did (?:we|i|you) decide\b|\bpast decisions?\b")


def followup_intent(q: str) -> Optional[str]:
    """Which follow-up tool answers this question, if any (q is lower case)."""
    if HISTORY_RE.search(q):
        return "decision_history"
    return next((name for name, rule in FOLLOWUP_RULES if rule.search(q)), None)


ORDER_POLITE = re.compile(r"^(?:(?:please|kindly|can you|could you|would you|will you|i (?:want|need) to|i would like to|i'd like to|let's|lets|go ahead and)\s+)+")


QUESTION_WORDS = {"what", "which", "who", "whom", "whose", "when", "where", "why", "how", "is", "are", "was", "were", "do", "does", "did", "can", "could", "should", "will", "would", "has", "have",
                  "show", "list", "tell", "give", "find", "explain", "compare", "check"}


def looks_like_a_question(q: str) -> bool:
    """Worth the time of an AI agent: a few words that ask or tell it to find something. A greeting or a single word is not."""
    words = re.findall(r"[a-z']+", q.lower())
    return len(words) >= 3 and (q.strip().endswith("?") or words[0] in QUESTION_WORDS)


def wants_order(q: str) -> bool:
    body = ORDER_POLITE.sub("", q.strip().rstrip("?.! ")).strip()
    words = body.split()
    if not words or words[0] not in ORDER_VERBS or not ORDER_TARGET.search(body):
        return False
    return not (set(tokens(body)) & {"status", "history", "overdue", "late", "delayed", "received", "delivered", "pending", "open"})


def wants_manage(q: str) -> Optional[str]:
    """'cancel' or 'edit' when the person tells me to take back or change an order; None for anything else (a question such as 'what happens if I cancel the order?' is not an instruction)."""
    body = ORDER_POLITE.sub("", q.strip().rstrip("?.! ")).strip()
    words = body.split()
    kind = None if not words else "cancel" if words[0] in CANCEL_VERBS else "edit" if words[0] in EDIT_VERBS else None
    if kind is None or not (DOC_NAME.search(body) or MANAGE_TARGET.search(body)):
        return None
    return None if set(tokens(body)) & NOT_AN_INSTRUCTION else kind


# Moving stock between warehouses is an instruction too: it must START with the verb ("move 50 of BOLT-M8 from Stores to Finished Goods", "transfer BOLT-M8 from Stores to Shop floor").
TRANSFER_VERBS = {"move", "transfer", "shift", "relocate"}
TRANSFER_QTY = re.compile(r"\b(?:move|transfer|shift|relocate)\s+(?:about\s+|around\s+|approximately\s+)?(\d[\d,]*(?:\.\d+)?)(?![-\w.])")
TRANSFER_ROLE = re.compile(r"\b(from|to|into)\s+(?:the\s+)?$")


def wants_transfer(q: str) -> bool:
    """True when the person tells me to move stock between warehouses (a question such as 'what happens if I move it?' is not an instruction)."""
    body = ORDER_POLITE.sub("", q.strip().rstrip("?.! ")).strip()
    words = body.split()
    return bool(words) and words[0] in TRANSFER_VERBS and not (set(tokens(body)) & NOT_AN_INSTRUCTION)


def find_warehouses(q: str, known: Sequence[str]) -> tuple[Optional[str], Optional[str], bool]:
    """(the warehouse to take it from, the one to move it to, whether the wording was unclear) for a question written in lower case. A warehouse is named in full or without its
    company ("Stores" for "Stores - TTC") when that is unique. The word before a name says which it is ("from Stores", "to Finished Goods"); two names with no such word are
    read in the order written. Anything else is unclear: the caller asks."""
    names: dict[str, str] = {}
    for w in known:
        names[w.lower()] = w
    short: dict[str, list[str]] = {}
    for w in known:
        short.setdefault(w.lower().split(" - ")[0], []).append(w)
    names.update({s: ws[0] for s, ws in short.items() if len(ws) == 1 and s not in names})
    found: list[tuple[int, int, str]] = []                                  # (start, end, warehouse), longest names first so "stores - ttc" wins over "stores"
    for text in sorted(names, key=len, reverse=True):
        for m in re.finditer(r"(?<![\w-])" + re.escape(text) + r"(?![\w-])", q):
            if not any(s < m.end() and m.start() < e for s, e, _ in found):
                found.append((m.start(), m.end(), names[text]))
    found.sort()
    ordered = list(dict.fromkeys(w for _s, _e, w in found))
    src = dst = None
    for s, _e, w in found:
        role = TRANSFER_ROLE.search(q[max(0, s - 12):s])
        if role and role.group(1) == "from" and src is None:
            src = w
        elif role and role.group(1) in ("to", "into") and dst is None:
            dst = w
    if src and dst and src != dst:
        return src, dst, False
    if len(ordered) == 2 and src is None and dst is None:
        return ordered[0], ordered[1], False
    return src, dst, bool(ordered)


APPROVAL_RE = re.compile(r"\b(approve|approvals?|waiting for (?:my|me)|need(?:s)? my ok|my ok|awaiting my)\b")
STATUS_FILTERS = {"open": "open", "pending": "open", "waiting": "open", "draft": "draft", "drafts": "draft", "received": "done", "delivered": "done", "completed": "done", "complete": "done",
                  "done": "done", "overdue": "overdue", "late": "overdue", "delayed": "overdue", "cancelled": "cancelled", "canceled": "cancelled", "submitted": "submitted"}
PROPOSAL_WORDS = {"awaiting": "waiting for your OK", "manual": "needs your decision", "ordered": "created in ERPNext", "snoozed": "you asked to be reminded", "rejected": "you said no",
                  "acknowledged": "noted", "expired": "expired", "cancelled": "cancelled", "superseded": "asked again", "po_failed": "ERPNext refused it"}
HELP = ("I can answer questions about your stock, your vendors and your purchase orders and requisitions, using live data from ERPNext. For example:\n"
        "• What is running low?\n• How much {example} do we have?\n• Compare vendors for {example}\n• What is the status of our open purchase orders?\n"
        "• Which purchase requisitions did we create this week?\n• What is waiting for my OK?\n"
        "• Create a purchase order for {example} for 500\n"
        "• Edit PO PUR-ORD-2026-00012 (change its quantity or vendor), or Cancel PO PUR-ORD-2026-00012 (only orders I created)\n"
        "• Move 50 {example} from Stores to Finished Goods (I prepare a transfer request; you press Create transfer)\n"
        "I only read information. I can prepare an order or transfer request for you to approve, but only its Create order or Create transfer button does anything, "
        "and a transfer only makes a draft in ERPNext. I cancel an order only after you confirm.")

REPLY_SCHEMA = {"type": "object", "properties": {"answerable": {"type": "boolean"}, "answer": {"type": "string"}, "facts_used": {"type": "array", "items": {"type": "string"}}},
                "required": ["answerable", "answer", "facts_used"]}
CHAT_SYSTEM = """You answer a question from a stock or purchasing manager about their own stock, vendors and purchase documents.
Follow these rules exactly:
1. Use ONLY the numbered FACTS you are given. Do not use outside knowledge. Do not guess. Do not assume anything that is not written in the facts.
2. Do not calculate or estimate new numbers. Repeat numbers exactly as they are written in the facts.
3. If the facts do not contain the answer to the question, set "answerable" to false and leave "answer" empty.
4. Answer in at most 4 short, plain sentences, directly answering the question that was asked.
5. Put in "facts_used" the id (for example "F3") of every fact your answer relies on.
6. """ + DATA_RULE + """
Reply only with JSON."""

ROUTE_SCHEMA = {"type": "object", "properties": {"intent": {"type": "string", "enum": [*INTENTS, "unknown"]}}, "required": ["intent"]}
ROUTE_SYSTEM = ("You sort one business question into exactly one category. Categories: "
                "item_stock (the stock level or days left of one particular item), stock_overview (what is running low or out of stock overall), "
                "vendor_compare (comparing vendors, suppliers or prices), document_status (the status of purchase orders, requisitions or receipts), "
                "pending_approvals (requests waiting for the owner's approval), receipt_mismatches (goods receipts that do not match their purchase order), "
                "late_deliveries (purchase orders that are late), vendor_scorecard (how vendors rank on delivery, price and quality), slow_stock (dead or slow stock tying up cash), "
                "substitutes (approved alternative items), stock_counts (cycle counts and count differences), demand_forecast (expected demand, seasonality), "
                "decision_history (why something was decided earlier), unknown (anything else). " + DATA_RULE + " Reply only with JSON.")


class ChatLimit(RateLimited):
    """Too many questions in a short time. A RateLimited (core/resilience.py): it says how many seconds to wait."""


# ====================================================================== understanding the question
def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:[-_/.][a-z0-9]+)*", text.lower())


@dataclass
class Plan:
    intent: Optional[str] = None
    items: list[ItemInfo] = field(default_factory=list)
    near: list[ItemInfo] = field(default_factory=list)        # items the wording only partly matches
    group: Optional[str] = None
    docs: list[str] = field(default_factory=list)
    doctypes: list[str] = field(default_factory=list)
    qty: Optional[float] = None
    since_days: int = 30
    status: Optional[str] = None
    stock_filter: Optional[str] = None                        # out | low
    from_context: bool = False
    user: str = ""                                            # who is asking (a request prepared on demand names them)
    bad_qty: bool = False                                     # a quantity was written but is zero, negative or not a plain number
    period_given: bool = False                                # the question names a period ("this week", "last 14 days"): otherwise open orders are shown whatever their age
    manage: Optional[str] = None                              # "cancel" | "edit": the person wants an order that exists taken back or changed
    from_warehouse: Optional[str] = None                      # a stock transfer: the warehouse named as the source, the one named as the destination
    to_warehouse: Optional[str] = None
    warehouse_unclear: bool = False                           # warehouses were named but not which is which, or only one
    unknown_codes: list[str] = field(default_factory=list)    # something written like an item code that is no item (and no document number): a typo, or an item that does not exist
    original: str = ""                                      # the question as it was typed (a vendor's name is told by its capital letters)
    text: str = ""                                            # the question, lower case (a follow-up answer reads its wording, e.g. count plan or count differences)


def resolve_items(question: str, items: list[ItemInfo]) -> tuple[list[ItemInfo], list[ItemInfo]]:
    """(the items the question names, items it only partly matches). A code is exact; a name must be given in full or nearly so;
    when several items fit equally well the caller asks which one is meant."""
    q = question.lower()
    toks = tokens(q)
    by_code = {i.code.lower(): i for i in items}
    found = list({by_code[t].code: by_code[t] for t in toks if t in by_code}.values())
    if found:
        return found, []
    named = [i for i in items if i.name and len(i.name) >= 4 and i.name.lower() in q]
    if named:
        longest = max(len(i.name) for i in named)
        return [i for i in named if len(i.name) == longest], []
    qset = {t for t in toks if t not in STOP and t not in GENERIC and not t.isdigit()}
    scored = []
    for i in items:
        nt = [t for t in tokens(i.name or "") if t not in STOP and len(t) > 1]
        if not nt:
            continue
        hit = sum(1 for t in nt if t in qset)
        if hit:
            scored.append((hit / len(nt), hit, len(nt), i))
    strong = [(s, h, n, i) for s, h, n, i in scored if s >= 0.75 and (h >= 2 or n == 1)]
    if strong:
        top = max(s for s, *_ in strong)
        return [i for s, h, n, i in strong if s == top], []
    near = sorted((x for x in scored if x[0] >= 0.3), key=lambda x: (-x[0], -x[1], x[3].code))
    return [], [i for *_x, i in near[:5]]


def parse_qty(question: str) -> Optional[float]:
    q = question.lower()
    m = re.search(r"\b(?:for|qty|quantity|buy|order)\s+(\d[\d,]*(?:\.\d+)?)\b(?![-\w])", q) or \
        re.search(r"(?<![-\w])(\d[\d,]*(?:\.\d+)?)\s*(?:units?|pcs|pieces|nos|kgs?|litres?|liters?|rolls?|sets?|met(?:er|re)s?|cartons?|pallets?|packs?)\b", q)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return v if v > 0 else None


def parse_days(question: str) -> int:
    q = question.lower()
    m = re.search(r"last (\d{1,3}) days?", q)
    if m:
        return int(m.group(1))
    if "today" in q:
        return 0
    if "yesterday" in q:
        return 1
    if re.search(r"this week|last week|past week|\bweek\b", q):
        return 7
    if re.search(r"this year|last year|past year", q):
        return 366
    if re.search(r"this month|last month|past month|\bmonth\b", q):
        return 31
    return 30


def understand(question: str, items: list[ItemInfo], context: Optional[dict[str, Any]] = None, warehouses: Sequence[str] = ()) -> Plan:
    """`warehouses` = the names of the ERP's warehouses; needed only to read a request to move stock from one to another."""
    q = " ".join(question.lower().split())
    toks = tokens(q)
    qs = set(toks)
    plan = Plan(text=q, original=question)
    plan.docs = [f"{m.group(1).upper()}-{m.group(2)}-{m.group(3)}" for m in DOC_NAME.finditer(question)]
    plan.items, plan.near = resolve_items(question, items)
    known = {i.code.lower() for i in items}
    plan.unknown_codes = [c for c in dict.fromkeys(ITEM_CODE_LIKE.findall(question)) if c.lower() not in known and not DOC_NAME.fullmatch(c)]
    groups = sorted({i.group for i in items if i.group})
    qset = {t for t in toks if t not in STOP}
    for g in groups:
        gt = [t for t in tokens(g) if t not in STOP and len(t) > 1]
        if g.lower() in q or (gt and len(gt[0]) >= 5 and gt[0] in qset):
            plan.group = g
            break
    plan.qty = parse_qty(question)
    plan.bad_qty = plan.qty is None and bool(re.search(r"\b(?:for|qty|quantity|buy|order)\s+-?\d[\d,]*(?:\.\d+)?(?![-\w.])", q))
    plan.since_days = parse_days(question)
    plan.period_given = bool(re.search(r"last \d{1,3} days?|today|yesterday|\bweek\b|\bmonth\b|this year|last year|past year", q))
    plan.status = next((STATUS_FILTERS[t] for t in toks if t in STATUS_FILTERS), None)
    if re.search(r"out of stock|\brun(?:s|ning)? out\b|\bran out\b|zero stock|no stock|stock ?out", q):
        plan.stock_filter = "out"
    elif re.search(r"\blow\b|\bshort(?:age|ages)?\b|need(?:s)? (?:to be )?order|\breorder\b|attention|running low", q):
        plan.stock_filter = "low"

    doctypes = []
    if qs & {"requisition", "requisitions", "pr", "prs", "mr"} or re.search(r"material requests?|purchase requisitions?", q):
        doctypes.append("Material Request")
    if qs & {"po", "pos"} or re.search(r"purchase orders?", q) or (qs & {"order", "orders"} and not doctypes):
        doctypes.append("Purchase Order")
    if qs & {"grn"} or re.search(r"receipts?|goods receipts?", q):
        doctypes.append("Purchase Receipt")
    plan.doctypes = doctypes

    plan.manage = wants_manage(q)
    follow = followup_intent(q)
    if follow == "vendor_scorecard" and plan.items and not SCORECARD_WORDS.search(q):
        follow = None                                      # "which vendor is best for GASKET-X" names an item: it is a comparison for that item, not the ranking of all vendors
    if plan.manage:
        plan.intent = "order_manage"
    elif not plan.docs and wants_transfer(q):
        plan.intent = "stock_transfer"
        m = TRANSFER_QTY.search(q)
        if m:
            said = float(m.group(1).replace(",", ""))
            plan.qty, plan.bad_qty = (said if said > 0 else None), said <= 0
        else:
            plan.bad_qty = plan.qty is None and bool(re.search(r"\b(?:move|transfer|shift|relocate)\s+-?\d[\d,]*(?:\.\d+)?(?![-\w.])", q))
        plan.from_warehouse, plan.to_warehouse, plan.warehouse_unclear = find_warehouses(q, warehouses)
    elif not plan.docs and wants_order(q):
        plan.intent = "order_request"
    elif follow == "decision_history" or (follow and not plan.docs):
        plan.intent = follow
    elif plan.docs:
        plan.intent = "document_status"
    elif plan.status == "overdue" and not plan.items:
        plan.intent = "document_status"
        plan.doctypes = plan.doctypes or ["Purchase Order"]
    elif APPROVAL_RE.search(q) and not (qs & {"po", "pos", "requisition", "requisitions"} and "status" in qs):
        plan.intent = "pending_approvals"
    elif qs & DOC_WORDS or DOC_RE.search(q) or ((qs & {"order", "orders"}) and (qs & {"status", "open", "pending", "waiting", "placed", "delivered", "received", "overdue", "late", "draft", "drafts"})):
        plan.intent = "document_status"
    elif qs & VENDOR_WORDS or "which vendor" in q or "who should" in q:
        plan.intent = "vendor_compare"
    elif STOCK_RE.search(q) or plan.stock_filter:
        refers = bool(context and context.get("items") and re.search(r"\b(it|that|this|them)\b", q))
        plan.intent = "item_stock" if (plan.items or plan.near or refers) and not (plan.group and not plan.items) else "stock_overview"
    elif plan.items:
        plan.intent = "item_stock"
    elif plan.near or plan.group:
        plan.intent = "item_stock" if plan.near else "stock_overview"

    # a follow-up such as "and the vendors?" keeps talking about the item from before
    if plan.intent in ("item_stock", "vendor_compare") and not plan.items and not plan.near and context and context.get("items"):
        by_code = {i.code: i for i in items}
        ctx_items = [by_code[c] for c in context["items"] if c in by_code][:3]
        if ctx_items:
            plan.items, plan.from_context = ctx_items, True
    return plan


# ====================================================================== the answer
@dataclass
class Answer:
    intent: str
    answer: str
    facts: list[Fact] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    actions: list[dict[str, str]] = field(default_factory=list)
    status: str = "answered"                                   # answered | clarify | help | error
    source: str = "data"                                       # data (the rules read ERPNext) | agent (an AI agent, whose answer was checked against ERPNext's figures)
    extra: dict[str, Any] = field(default_factory=dict)        # e.g. the task to poll while an agent works

    def to_dict(self) -> dict[str, Any]:
        return {"intent": self.intent, "status": self.status, "answer": self.answer, "facts": [{"id": f.id, "text": f.text} for f in self.facts], "blocks": self.blocks,
                "follow_ups": self.follow_ups, "items": self.items, "actions": self.actions, "source": self.source, **self.extra}


def _facts(lines: list[str]) -> list[Fact]:
    return [Fact(f"F{i}", t) for i, t in enumerate(lines, start=1)]


def status_label(a: Any) -> tuple[str, str]:
    """(label, tone) for one analysed item, in the words the console uses."""
    if a.status == "OK":
        return "Fine", "ok"
    if a.status == "EARLY_WARNING":
        return "Keep an eye on it", "soon"
    if a.position is not None and a.position <= 0:
        return "Out of stock", "urgent"
    return ("Order now", "urgent") if a.severity >= 3 else ("Order soon", "soon")


class ChatService:
    def __init__(self, erp: ERPClient, inventory: Any, procurement: Any, proposals: Any, approvals: Any, settings: SettingsStore, db: Database,
                 clock: Clock = utcnow, client_factory: Optional[Callable[[AssistantSettings], LLMClient]] = None,
                 limits: Optional[Limits] = None) -> None:
        self.erp, self.inventory, self.procurement, self.proposals, self.approvals = erp, inventory, procurement, proposals, approvals
        ensure_schema(db)
        self.settings, self.db, self.clock = settings, db, clock
        self.client_factory = client_factory or (lambda cfg: OllamaClient(cfg.base_url, cfg.model, cfg.timeout_seconds))
        self.limits = limits or Limits(ResilienceSettings())          # the application's per-person limits (one set, shared with the chat on phones and the API)
        self.agent_fallback: Optional[Callable[[str, str, bool], Optional[dict[str, Any]]]] = None      # (question, user, can_decide) -> an agent's answer, or None (set by bootstrap)
        self.agent_background: Optional[Callable[[str, str, bool], Optional[dict[str, Any]]]] = None    # same, but starts the agent and returns {"task_id", "answer"} to poll
        self.tool: Optional[Callable[[str, dict[str, Any], str], dict[str, Any]]] = None             # (tool, arguments, who asks) -> the tool's result, with the core's role check (set by the pack)
        self.transfers: Optional[Any] = None             # the stock transfer service (prepares the request to move stock); set by the pack
        self._items_cache: tuple[float, list[ItemInfo]] = (0.0, [])
        self._overview_cache: tuple[float, list[dict]] = (0.0, [])
        self._warehouse_cache: tuple[float, list[str]] = (0.0, [])

    # ------------------------------------------------------------------ small helpers
    def _items(self) -> list[ItemInfo]:
        now = time.monotonic()
        if now - self._items_cache[0] > 300 or not self._items_cache[1]:
            self._items_cache = (now, self.erp.list_items())
        return self._items_cache[1]

    def _warehouses_for(self, question: str) -> list[str]:
        """The ERP's warehouse names, read only when the question is an instruction to move stock (the list is kept for a few minutes). Empty when the ERP cannot say."""
        if not wants_transfer(" ".join(question.lower().split())):
            return []
        now = time.monotonic()
        if now - self._warehouse_cache[0] > 300 or not self._warehouse_cache[1]:
            try:
                self._warehouse_cache = (now, self.erp.list_warehouses())
            except Exception as exc:  # noqa: BLE001 - the person is asked to name the warehouses again instead
                log.warning("could not read the warehouses for a transfer request: %s", untrusted(exc, 200))
                return []
        return self._warehouse_cache[1]

    def _overview(self) -> list[dict]:
        now = time.monotonic()
        if now - self._overview_cache[0] > 30 or not self._overview_cache[1]:
            self._overview_cache = (now, self.inventory.overview())
        return self._overview_cache[1]

    def _money(self, x: Optional[float]) -> str:
        if x is None:
            return "unknown"
        if not hasattr(self, "_symbol"):
            try:
                ccy = self.erp._company_currency()                   # type: ignore[attr-defined]  (the ERPNext adapter; the demo ERP has none)
            except Exception:  # noqa: BLE001
                ccy = ""
            self._symbol = SYMBOLS.get(ccy, (ccy + " ") if ccy else "")
        return f"{self._symbol}{x:,.2f}"

    def _throttle(self, user: str, scope: str = "chat") -> None:
        """Count one ask against the person's limit for this kind of ask; raises ChatLimit, which says how many seconds to wait."""
        try:
            self.limits.enforce(scope, user)
        except RateLimited as exc:
            raise ChatLimit(exc.scope, exc.retry_after) from None

    def _log(self, user: str, question: str, intent: str, status: str, source: str) -> None:
        try:
            self.db.execute("INSERT INTO chat_log(asked_by, asked_at, question, intent, status, source) VALUES (?,?,?,?,?,?)", (user, iso(self.clock()), question[:300], intent, status, source))
        except Exception:  # noqa: BLE001 - the log must never break an answer
            log.exception("could not write the chat log")

    def throttle(self, user: str) -> None:
        """Count one question against the person's limit per minute (raises ChatLimit)."""
        self._throttle(user)

    # ------------------------------------------------------------------ the public entry points
    def ask(self, question: str, user: str, can_decide: bool, context: Optional[dict[str, Any]] = None, background: bool = False) -> dict[str, Any]:
        """background=True (the web console): a question the rules cannot place starts an AI agent and the answer says 'working' with a task to poll."""
        question = untrusted(question or "", 100_000)          # control and invisible characters out; the length rule below still applies
        if not question:
            raise ValueError("Please type a question.")
        if len(question) > MAX_CHAT_QUESTION:
            raise ValueError(f"Please keep the question under {MAX_CHAT_QUESTION} characters.")
        self._throttle(user)
        routed = False
        try:
            items = self._items()
            plan = understand(question, items, context, self._warehouses_for(question))
            if plan.intent is None:
                plan.intent = self._route_with_ai(question)
                routed = plan.intent is not None
            plan.user = user
            ans = self._by_agent(question, user, can_decide, background) if (plan.intent is None and not plan.near and not plan.items and looks_like_a_question(question)) else None
            ans = ans or self._run(plan, items, can_decide)
        except Exception as exc:  # noqa: BLE001 - ERPNext unreachable, a bad record, ...: tell the person, change nothing
            log.warning("chat could not answer %r: %s", untrusted(question, 120), untrusted(exc, 200))
            ans = Answer("error", f"I could not read the data just now ({_short(exc)}). Nothing was changed. Please try again in a moment.", status="error")
            plan = Plan(intent="error")
        out = ans.to_dict()
        if routed:
            out["routed_by_ai"] = True
        self._log(user, question, ans.intent, ans.status, "ai-routed" if routed else "rules")
        return out

    def _by_agent(self, question: str, user: str, can_decide: bool, background: bool = False) -> Optional[Answer]:
        """A question the rules could not place goes to the AI agents when they are on. Their answer has passed the same kind of checks as every other (numbers from ERPNext only)."""
        if background:
            started = self.agent_background(question, user, can_decide) if self.agent_background is not None else None
            return Answer("agent", started["answer"], status="working", source="agent", extra={"task_id": started["task_id"]}) if started else None
        if self.agent_fallback is None:
            return None
        res = self.agent_fallback(question, user, can_decide)
        if not res:
            return None
        return Answer("agent", res["answer"], status="answered", source="agent")

    def explain(self, question: str, user: str, can_decide: bool, context: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Optional: the local AI helper restates the answer's facts in plain words. Shown only if it passes the same checks as 'Ask why'."""
        cfg = self.settings.assistant()
        if not cfg.enabled:
            return {"status": "disabled", "answer": "The AI helper is switched off in Settings.", "facts": []}
        question = untrusted(question or "", 100_000)[:MAX_CHAT_QUESTION]
        self._throttle(user)
        self._throttle(user, "model")                              # restating the answer is a call to the language model
        items = self._items()
        plan = understand(question, items, context)
        if plan.intent == "order_request":                          # explaining must never prepare a request a second time
            return {"status": "not_enough_information", "answer": "There is nothing to explain here: ask me to prepare the order request again, or use the one already waiting.", "facts": []}
        if plan.intent == "order_manage":                           # nor open the buttons that change or cancel an order
            return {"status": "not_enough_information", "answer": "There is nothing to explain here: ask me to change or cancel the order again.", "facts": []}
        if plan.intent == "stock_transfer":                         # nor prepare a transfer request
            return {"status": "not_enough_information", "answer": "There is nothing to explain here: ask me to prepare the transfer again, or use the request already waiting.", "facts": []}
        if plan.intent is None:
            plan.intent = self._route_with_ai(question)
        ans = self._run(plan, items, can_decide)
        if not ans.facts:
            return {"status": "not_enough_information", "answer": NOT_ENOUGH, "facts": []}
        key = hashlib.sha256(json.dumps([PROMPT_VERSION, cfg.model, [f.text for f in ans.facts], question.lower()]).encode()).hexdigest()
        row = self.db.query_one("SELECT result_json FROM assistant_answers WHERE cache_key=?", (key,))
        if row:
            res = loads(row["result_json"])
            res.pop("draft", None)
            return {**res, "cached": True}
        user_msg = "FACTS:\n" + "\n".join(fact_line(f.id, f.text) for f in ans.facts) + f"\n\nQUESTION: {data(question, MAX_CHAT_QUESTION)}"
        started = time.monotonic()
        try:
            raw = self.client_factory(cfg).chat(CHAT_SYSTEM, user_msg, REPLY_SCHEMA)
        except LLMError as exc:
            return {"status": "unavailable", "answer": "The AI helper is not available right now. " + str(exc), "facts": []}
        verdict = verify(raw, ans.facts, question, cfg.max_answer_chars)
        res = {"status": verdict.status, "answer": verdict.answer if verdict.status == "answered" else NOT_ENOUGH,
               "facts": [{"id": f.id, "text": f.text} for f in verdict.cited], "check": verdict.check, "model": cfg.model, "seconds": round(time.monotonic() - started, 1)}
        self.db.execute("INSERT OR REPLACE INTO assistant_answers(cache_key, approval_id, question, result_json, model, asked_by, created_at) VALUES (?,?,?,?,?,?,?)",
                        (key, "chat", question, dumps({**res, "draft": raw[:4000]}), cfg.model, user, iso(self.clock())))
        return {**res, "cached": False}

    def _route_with_ai(self, question: str) -> Optional[str]:
        """Only when the rules could not place the question. The model may pick a question type from a fixed list, nothing else."""
        cfg = self.settings.assistant()
        if not cfg.enabled:
            return None
        try:
            raw = self.client_factory(cfg).chat(ROUTE_SYSTEM, f"QUESTION: {data(question, MAX_CHAT_QUESTION)}", ROUTE_SCHEMA)
            intent = json.loads(raw).get("intent")
        except (LLMError, ValueError, AttributeError):
            return None
        return intent if intent in INTENTS else None

    # ------------------------------------------------------------------ dispatch
    def _run(self, plan: Plan, items: list[ItemInfo], can_decide: bool) -> Answer:
        sample = items[0].code if items else "FAS-0003"
        if plan.intent == "order_request":
            return self._order_request(plan, can_decide, sample)
        if plan.intent == "order_manage":
            return self._order_manage(plan, can_decide, sample)
        if plan.intent == "stock_transfer":
            return self._stock_transfer(plan, can_decide, sample)
        if plan.intent is None or plan.intent not in INTENTS:
            if plan.near:
                return self._ask_which(plan.near, "I could not tell exactly which item you mean.")
            return Answer("help", HELP.format(example=sample), status="help", follow_ups=["What is running low?", f"How much {sample} do we have?", "What is waiting for my OK?"])
        if plan.unknown_codes and not plan.items and not plan.near and plan.intent in ("item_stock", "stock_overview", "vendor_compare"):
            said = " or ".join(plan.unknown_codes[:3])         # an item that does not exist is said so, not answered with a different question's answer
            return Answer(plan.intent, f"I could not find an item with the code {said}. Give me an item's code or its full name, for example {sample}.", status="clarify",
                          follow_ups=[f"How much {sample} do we have?", "What is running low?"])
        if plan.intent == "pending_approvals":
            return self._pending(can_decide)
        if plan.intent in FOLLOWUP_INTENTS:
            return self._followup(plan, sample)
        if plan.intent == "stock_overview":
            return self._overview_answer(plan)
        if plan.intent == "document_status":
            return self._documents(plan, items)
        if not plan.items:
            return self._ask_which(plan.near, "I am not sure which item you mean.") if plan.near else Answer(
                plan.intent, "Which item do you mean? Give me its code or its full name, for example " + sample + ".", status="clarify",
                follow_ups=[f"Compare vendors for {sample}" if plan.intent == "vendor_compare" else f"How much {sample} do we have?"])
        if len(plan.items) > 3:
            return self._ask_which(plan.items[:8], "That matches several items.")
        return self._item_stock(plan) if plan.intent == "item_stock" else self._vendor_compare(plan)

    # ------------------------------------------------------------------ the follow-up questions, answered by the same tools the agents use
    def _followup(self, plan: Plan, sample: str) -> Answer:
        if self.tool is None:
            return Answer(plan.intent or "help", "I cannot answer that here.", status="help")
        needs_item = plan.intent in ("substitutes", "demand_forecast")
        if needs_item and not plan.items:
            if plan.near:
                return self._ask_which(plan.near, "I could not tell exactly which item you mean.")
            what = "approved alternatives of" if plan.intent == "substitutes" else "the forecast for"
            return Answer(plan.intent, f"Which item do you mean? For example: {what} {sample}.", status="clarify")
        code = plan.items[0].code if plan.items else None
        words = [w for w in re.findall(r"[A-Za-z0-9&.'-]+", plan.original)[1:] if w[0].isupper() and w.lower() not in STOP and not w.isdigit()]
        vendor = " ".join(words)                                  # "How is TST Beta doing?" -> "TST Beta": the scorecard of that vendor
        args: dict[str, Any] = {
            "receipt_mismatches": {"days": plan.since_days} if plan.period_given else {},
            "late_deliveries": {},
            "vendor_scorecard": {"vendor": vendor} if vendor else {},
            "slow_stock": {"limit": 10},
            "substitutes": {"item_code": code},
            "stock_counts": {"kind": "variances" if re.search(r"varian|differen|discrepan", plan.text) else "plan"},
            "demand_forecast": {"item_code": code, "weeks": 6},
            "decision_history": {"item_code": code, "query": " ".join(plan.docs), "limit": 5},
        }[plan.intent or ""]
        res = self.tool(plan.intent or "", args, plan.user)
        if plan.intent == "vendor_scorecard" and vendor and not res.get("vendors") and not res.get("error"):
            res = self.tool("vendor_scorecard", {}, plan.user)    # no vendor of that name: the best and the worst instead
        if res.get("error"):
            return Answer(plan.intent or "help", f"I could not check that: {res['error']}.", status="error")
        lines = getattr(self, "_fu_" + (plan.intent or ""))(res)
        head, rest = lines[0], lines[1:]
        return Answer(plan.intent or "help", "\n".join([head] + [f"• {l}" for l in rest]), facts=_facts(lines), items=[code] if code else [])

    @staticmethod
    def _fu_receipt_mismatches(r: dict[str, Any]) -> list[str]:
        if not r["with_differences"]:
            return [f"All {r['receipts_checked']} goods receipts of the last {r['days']} days match their purchase orders: quantity, price and nothing rejected."]
        out = [f"{r['with_differences']} of {r['receipts_checked']} goods receipts in the last {r['days']} days do not match their purchase order:"]
        for x in r["receipts"]:
            out.append(f"{x['receipt']} from {x['vendor']}" + (" (still a draft)" if x["draft"] else "") + ": " + " ".join(x["differences"]) + " " + x["before_you_pay"])
        return out

    @staticmethod
    def _fu_late_deliveries(r: dict[str, Any]) -> list[str]:
        if not r["late_orders"]:
            return ["No purchase order is late: everything still to come is within its due date."]
        out = [f"{r['late_orders']} purchase order{'s are' if r['late_orders'] != 1 else ' is'} late:"]
        for o in r["orders"]:
            parts = []
            for e in o["items"]:
                cover = (f"; stock on hand lasts about {_n(e['cover_days_without_it'])} days without it" if e.get("cover_days_without_it") is not None else "") + \
                        (f", {_n(e['cover_days_with_it'])} days with it" if e.get("cover_days_with_it") is not None else "")
                parts.append(f"{_n(e['still_to_come'])} {e['uom']} of {e['item_code']} still to come{cover}")
            sent = f" Reminders sent: {o['reminders_sent']}." if o["reminders_sent"] else ""
            out.append(f"{o['document']} from {o['vendor']}, {o['days_late']} days late (due {_d(o['due'])}): " + "; ".join(parts) + "." + sent)
        return out

    @staticmethod
    def _fu_vendor_scorecard(r: dict[str, Any]) -> list[str]:
        ranked = [v for v in r["vendors"] if v["rank"]]
        if not ranked:
            return ["There are not enough goods receipts yet to rank the vendors."]
        shown = f", showing {r['showing']} of {r['ranked_vendors']}" if r.get("showing") not in (None, "all") and r["ranked_vendors"] > len(ranked) else ""
        out = [f"Vendors ranked on on-time delivery, price drift and quality, last {r['days']} days{shown}:"]
        for v in r["vendors"]:
            if not v["rank"]:
                continue
            bits = [f"score {_n(v['score'])}"] + ([f"on time {_n(v['on_time_pct'])}%"] if v["on_time_pct"] is not None else []) + \
                   ([f"prices {'+' if (v['price_drift_pct'] or 0) > 0 else ''}{_n(v['price_drift_pct'], 1)}%"] if v["price_drift_pct"] is not None else []) + \
                   ([f"{_n(v['rejected'])} rejected"] if v["rejected"] else [])
            out.append(f"{v['rank']}. {v['vendor']}: " + ", ".join(bits) + "." + (f" {v['why_lower']}" if v["why_lower"] else ""))
        return out

    def _fu_slow_stock(self, r: dict[str, Any]) -> list[str]:
        if not r["items"]:
            return ["No dead or slow stock: every item with stock has been used recently and lasts less than your limit."]
        cash = f", tying up about {self._money(r['cash_tied_up'])}" if r.get("values_known") else ""
        out = [f"{r['dead']} dead and {r['slow']} slow item{'s' if r['dead'] + r['slow'] != 1 else ''}{cash}:"]
        for x in r["items"]:
            worth = f" ({self._money(x['value_tied_up'])} tied up)" if x.get("value_tied_up") is not None else ""
            out.append(f"{x['item_name']} ({x['item_code']}), {x['kind']}: {_n(x['on_hand'])} {x['uom']}{worth}. {x['advice']}")
        return out

    @staticmethod
    def _fu_substitutes(r: dict[str, Any]) -> list[str]:
        alts = [a for a in r["alternatives"] if not a.get("error")]
        if not alts:
            return [r.get("note") or f"ERPNext lists no approved alternative for {r['item_code']}."]
        out = [f"Approved alternatives of {r['item_name']} ({r['item_code']}) in ERPNext:"]
        for a in alts:
            cover = f", about {_n(a['cover_days'])} days at its own use" if a.get("cover_days") is not None else ""
            out.append(f"{a['item_name']} ({a['item_code']}): {_n(a['available'])} available{cover}.")
        return out

    @staticmethod
    def _fu_stock_counts(r: dict[str, Any]) -> list[str]:
        if r["kind"] == "variances":
            if not r["documents"]:
                return ["No recent stock count found a difference worth explaining."]
            out = [f"{r['lines']} count difference{'s' if r['lines'] != 1 else ''} to look at:"]
            for d in r["documents"]:
                for l in d["lines"]:
                    out.append(f"{d['document']}, {l['item_code']} in {l['warehouse']}: " + " ".join(l["why"]))
            return out
        if not r["count_these"]:
            return ["Nothing is due for counting this week."]
        out = [f"Count these {len(r['count_these'])} items this week ({r['due']} are due):"]
        for c in r["count_these"]:
            why = "negative stock in ERPNext" if c["negative_stock"] else ("never counted" if not c["last_counted"] else f"last counted {_d(c['last_counted'])}")
            out.append(f"{c['item_name']} ({c['item_code']}), class {c['class']}: {why}.")
        return out

    @staticmethod
    def _fu_demand_forecast(r: dict[str, Any]) -> list[str]:
        u = r["uom"]
        season = (f" Last year's season raises it x{_n(r['seasonal_index'], 2)}." if (r["seasonal_index"] or 1) > 1 else "")
        sales = (f" {_n(r['open_sales_orders_due'])} {u} is due on open sales orders" + (" (already taken off available stock)." if r["sales_orders_already_reserved"] else ".")
                 if r["open_sales_orders_due"] else "")
        out = [f"{r['item_name']} ({r['item_code']}): over the next {_n(r['lead_plus_safety_days'])} days (lead time + safety) expect about {_n(r['expected_use_in_lead_time'])} {u} "
               f"of use; reorder point {_n(r['reorder_point'])} {u}, stock position {_n(r['stock_position'])} {u}." + season + sales
               + (" It is time to order." if r["time_to_order"] else "")]
        for w in r["weekly"]:
            out.append(f"Week of {_d(w['week_starting'])}: about {_n(w['expected_use'])} {u} of use" + (f", {_n(w['sales_orders_due'])} {u} due on sales orders" if w["sales_orders_due"] else "") + ".")
        return out

    @staticmethod
    def _fu_decision_history(r: dict[str, Any]) -> list[str]:
        if not r["decisions"]:
            return [f"I found no decision on that in the last {r['days']} days."]
        out = [f"The last {r['listed']} decision{'s' if r['listed'] != 1 else ''} on that (of {r['found']}), newest first:"]
        for d in r["decisions"]:
            why = " Why: " + " ".join(d["why"][:2]) if d["why"] else ""
            out.append(f"{d['when'][:10]} {d['title']}: {d['outcome']}{why}")
        return out

    def _order_request(self, plan: Plan, can_decide: bool, sample: str) -> Answer:
        """'Create a purchase order for X': prepare the usual order request (vendors compared, Create order / Change quantity / Change vendor) for the person to answer.
        It creates NOTHING in the ERP, and only someone who may approve orders can ask. A request already waiting for the item is shown again, not duplicated."""
        if not can_decide:
            return Answer("order_request", "Only people who can approve orders can ask me to prepare one. You can see what is waiting under Needs your OK.", actions=[{"label": "Open Needs your OK", "view": "ok"}])
        if not plan.items and plan.near:
            return self._ask_which(plan.near, "I could not tell exactly which item you mean.")
        if not plan.items:
            return Answer("order_request", f"Which item should the order be for? Give me its code or full name, for example: create a purchase order for {sample} for 500.", status="clarify",
                          follow_ups=[f"Create a purchase order for {sample}"])
        if len(plan.items) > 1:
            return self._ask_which(plan.items[:8], "An order request is for one item at a time.")
        it = plan.items[0]
        a, _src = self.inventory.analyze_one(it.code)
        qty = plan.qty
        if plan.bad_qty or (qty is not None and not (0 < qty <= 1_000_000_000)):
            return Answer("order_request", "That quantity does not look right. Give me the number of units, for example: create a purchase order for " + it.code + " for 500.", status="clarify")
        moq_of = getattr(self.erp, "item_min_order_qty", None)
        moq = moq_of(it.code) if (moq_of and qty) else None
        if moq and qty < moq:
            return Answer("order_request", f"Your ERP does not accept an order below this item's minimum order quantity of {_n(moq)} {a.uom}. Try again with {_n(moq)} or more.", status="clarify")
        from ..inventory.agent import on_demand_payload
        res = self.procurement.request_purchase(on_demand_payload(a, plan.user or "someone", qty), plan.user or "someone")
        rec, name = res.get("approval"), f"{a.item_name} ({it.code})"
        if res["status"] == "need_qty":
            return Answer("order_request", f"How many {a.uom} of {name} do you want? There is no usage history to suggest a quantity from. For example: create a purchase order for {it.code} for 50.",
                          status="clarify", items=[it.code], follow_ups=[f"Create a purchase order for {it.code} for 50"])
        if res["status"] == "refused" or rec is None:
            return Answer("order_request", f"I could not prepare an order request for {name} just now ({res.get('why', 'unknown reason')}). Nothing was changed.", items=[it.code], status="error")
        facts = _facts([f"Request: {rec.request.title}", f"Quantity: {_n(rec.request.facts.get('qty'))} {a.uom}" if rec.request.facts.get("qty") else f"Item: {name}", f"Kind: {rec.kind}"])
        open_it = [{"label": "Open the request", "view": "ok", "approval_id": rec.id}]
        if res["status"] == "waiting":
            other = f" You asked for {_n(qty)} {a.uom}: press Change quantity on it to use that." if qty and qty != rec.request.facts.get("qty") else ""
            return Answer("order_request", f"A request for {name} is already waiting for your OK: {rec.request.title}. {rec.request.headline}{other} Press Change quantity or Change vendor on it "
                          "to adjust it; nothing is ordered until you press Create order.", facts, actions=[{**open_it[0], "resend": "1"}], items=[it.code])
        if res["status"] == "refreshed":
            return Answer("order_request", f"A request for {name} was already waiting, but it could not be used as it was. {res['why']} The new request is on its way to you with its buttons: {rec.request.title}. "
                          "Nothing is ordered until you press Create order.", facts, actions=open_it, items=[it.code])
        if res["status"] == "reorder_waiting":
            return Answer("order_request", f"A reorder suggestion for {name} is already waiting for your OK. Press Start reorder on it: the assistant then compares your vendors and prepares the purchase order for you to approve.", facts, actions=[{**open_it[0], "resend": "1"}], items=[it.code])
        if res["status"] == "manual":
            return Answer("order_request", f"I could not pick a vendor for {name} automatically: {res.get('problem')} I have asked you to decide, in a request that is now waiting for your OK.", facts, actions=open_it, items=[it.code])
        f = rec.request.facts
        prop = res.get("proposal")
        vendor = prop.supplier_name if prop and prop.supplier_name else f.get("supplier")
        what = "purchase requisition" if rec.kind == "procurement.pr" else "purchase order"
        return Answer("order_request", f"I prepared a {what} request for {_n(f.get('qty'))} {a.uom} of {name}: the best vendor is {vendor}, {self._money(f.get('total'))} in total. It is waiting for your OK "
                      "(the request has the buttons Create order, Change quantity and Change vendor). Nothing is ordered until you press Create order.", facts, actions=open_it, items=[it.code],
                      follow_ups=[f"Compare vendors for {it.code}", "What is waiting for my OK?"])

    def _order_manage(self, plan: Plan, can_decide: bool, sample: str) -> Answer:
        """'Cancel PO PUR-ORD-2026-00012' / 'edit the order for X'. Only an order THIS application created, and only for someone who may approve orders.
        Edit: the message with the buttons Change quantity / Change vendor / Cancel order is put in front of the person. Cancel: the person is asked to confirm
        (yes / no) before anything is removed in ERPNext, so one mistyped word never deletes an order."""
        cancel = plan.manage == "cancel"
        verb = "cancel" if cancel else "change"
        if not can_decide:
            return Answer("order_manage", f"Only people who can approve orders can {verb} one. You can see what is waiting under Needs your OK.", actions=[{"label": "Open Needs your OK", "view": "ok"}])
        example = f"{'cancel' if cancel else 'edit'} PO PUR-ORD-2026-00012"
        po = next((d for d in plan.docs if d.startswith("PUR-ORD")), None)
        if plan.docs and not po:
            return Answer("order_manage", f"I can only {verb} purchase orders that I created (their numbers start with PUR-ORD). {plan.docs[0]} is not one.", status="clarify")
        if po:
            prop = self.procurement.find_order(po_name=po)
            target = po
        else:
            if not plan.items and plan.near:
                return self._ask_which(plan.near, "I could not tell exactly which item you mean.")
            if not plan.items:
                return Answer("order_manage", f"Which order do you mean? Give me its number, or the item it is for, for example: {example}.", status="clarify")
            if len(plan.items) > 1:
                return self._ask_which(plan.items[:8], "An order is for one item at a time.")
            it = plan.items[0]
            prop = self.procurement.find_order(item_code=it.code)
            target = f"{it.name} ({it.code})" if it.name else it.code
        if prop is None:
            return Answer("order_manage", f"I can only {verb} purchase orders that I created from an order request, and I have none for {target} that is still open. "
                          "If it was made in ERPNext by hand, change it there. A request that is still waiting for your OK is not an order yet: press Reject on it, or Change quantity.",
                          status="clarify", actions=[{"label": "Open Needs your OK", "view": "ok"}])
        problem = self.procurement.order_problem(prop)
        if problem:
            return Answer("order_manage", problem, items=[prop.item_code], status="clarify")
        rec = self.procurement.manage_request(prop)
        state = self.procurement.order_state(prop.po_name)
        what = self.procurement.describe_order(prop, state)
        facts = _facts([f"Order: {what}", f"Item: {prop.item_code}"])
        if not cancel:
            return Answer("order_manage", f"Here is {what} You can change the quantity or the vendor with the buttons below, or cancel it. Change quantity changes it in this "
                          "same order (vendor and price stay). Change vendor gives you a revised request, and the order is replaced only when you press Create order on it.",
                          facts, actions=[{"label": "Open the request", "view": "ok", "approval_id": rec.id, "resend": "1"}], items=[prop.item_code])
        how = "deleted in ERPNext" if state and state.get("docstatus") == 0 else "cancelled in ERPNext"
        return Answer("order_manage", f"Cancel purchase order {what} It will be {how}. Do you want to cancel it? Reply yes or no.", facts,
                      actions=[{"label": "Open the request", "view": "ok", "approval_id": rec.id}], items=[prop.item_code], status="confirm",
                      extra={"expect": {"approval_id": rec.id, "action": "cancel_order", "choices": [{"id": "yes", "label": "Yes, cancel it"}, {"id": "no", "label": "No, keep it"}]}})

    def _stock_transfer(self, plan: Plan, can_decide: bool, sample: str) -> Answer:
        """'Move 50 of X from Stores to Finished Goods': prepare the request for the person to answer (Create transfer / Reject). It creates NOTHING in the ERP and moves no
        stock, and only someone who may approve can ask. A request already waiting for the same move is shown again, not duplicated. Anything unclear is asked, never guessed."""
        if not can_decide:
            return Answer("stock_transfer", "Only people who can approve can ask me to prepare a stock transfer. You can see what is waiting under Needs your OK.", actions=[{"label": "Open Needs your OK", "view": "ok"}])
        if self.transfers is None:
            return Answer("stock_transfer", "I cannot prepare stock transfers here.", status="error")
        example = f"move 50 {sample} from Stores to Finished Goods"
        if not plan.items and plan.near:
            return self._ask_which(plan.near, "I could not tell exactly which item you mean.")
        if not plan.items:
            return Answer("stock_transfer", f"Which item should be moved? Give me its code or full name, for example: {example}.", status="clarify", follow_ups=[example])
        if len(plan.items) > 1:
            return self._ask_which(plan.items[:8], "A transfer is for one item at a time.")
        it = plan.items[0]
        a, _src = self.inventory.analyze_one(it.code)
        held = where_stock_is(a)
        here = f" {it.name} ({it.code}) is held in: " + "; ".join(held) + "." if held else ""
        if plan.bad_qty or (plan.qty is not None and not (0 < plan.qty <= 1_000_000_000)):
            return Answer("stock_transfer", "The quantity must be a plain number above zero, for example: " + f"move 50 {it.code} from Stores to Finished Goods.", status="clarify", items=[it.code])
        if plan.qty is None:
            return Answer("stock_transfer", f"How many {a.uom} of {it.name} should be moved?{here} For example: move 50 {it.code} from Stores to Finished Goods.", status="clarify", items=[it.code],
                          follow_ups=[f"Move 50 {it.code} from Stores to Finished Goods"])
        if not (plan.from_warehouse and plan.to_warehouse):
            what = "I could not tell which warehouse is which." if plan.warehouse_unclear else "Which warehouses?"
            return Answer("stock_transfer", f"{what} Say it like this: move {_n(plan.qty)} {it.code} from <warehouse> to <warehouse>.{here}", status="clarify", items=[it.code])
        res = self.transfers.prepare(it.code, plan.qty, plan.from_warehouse, plan.to_warehouse, plan.user, "")
        move = f"{_n(plan.qty)} {a.uom} of {it.name} from {plan.from_warehouse} to {plan.to_warehouse}"
        if res["status"] == "refused":
            return Answer("stock_transfer", f"I could not prepare the transfer of {move}: {res['error']}. Nothing was changed.", items=[it.code], status="clarify")
        open_it = [{"label": "Open the request", "view": "ok", "approval_id": res["approval_id"]}]
        facts = _facts([f"Request: move {move}", f"Item: {it.code}"] + (["Held now: " + "; ".join(held)] if held else []))
        if res["status"] == "waiting":
            return Answer("stock_transfer", f"A request to move {it.name} between {plan.from_warehouse} and {plan.to_warehouse} is already waiting for your OK. Nothing is moved until you press Create transfer.",
                          facts, actions=[{**open_it[0], "resend": "1"}], items=[it.code])
        return Answer("stock_transfer", f"I prepared a request to move {move}. It is waiting for your OK (the request has the buttons Create transfer and Don't move it). Nothing is created in ERPNext "
                      "until you press Create transfer, and no stock moves until that draft is submitted there.", facts, actions=open_it, items=[it.code],
                      follow_ups=["What is waiting for my OK?", f"How much {it.code} do we have?"])

    def _ask_which(self, candidates: list[ItemInfo], lead: str) -> Answer:
        opts = [f"{i.name} ({i.code})" for i in candidates[:8]]
        text = lead + " Did you mean one of these? " + "; ".join(opts) + "."
        return Answer("clarify", text, status="clarify", follow_ups=[f"Tell me about {i.code}" for i in candidates[:4]],
                      facts=_facts([f"Possible item: {o}" for o in opts]))

    # ------------------------------------------------------------------ tools
    def _item_stock(self, plan: Plan) -> Answer:
        parts, facts, blocks, codes = [], [], [], []
        for it in plan.items:
            a, _src = self.inventory.analyze_one(it.code)
            codes.append(it.code)
            u = a.uom
            label, _tone = status_label(a)
            lines = [f"{a.item_name} ({a.item_code}): {_n(a.on_hand)} {u} on hand" + (f", {_n(a.available)} available after {_n(a.reserved)} promised to customers" if a.reserved > 0 else "")]
            split = where_stock_is(a)
            where = _where_sentence(a, split)
            if where:
                lines.append(where.rstrip("."))
            rate = a.usage.planning_rate if a.usage else 0
            lines.append(f"Selling about {_n(rate, 2)} {u} a day" if rate else "No recent sales, so the daily use is not known")
            if a.cover_days_on_hand is not None:
                lines.append(f"Stock lasts about {_n(a.cover_days_on_hand)} days" + (f" (runs out around {_d(a.stockout_date)})" if a.stockout_date else ""))
            lines.append(f"{_n(a.incoming)} {u} on open purchase orders" + (f", next arrival {_d(a.next_arrival)}" if a.next_arrival else ", arrival date not set") if a.incoming else "Nothing is on order")
            lines.append(f"New stock takes about {_n(a.lead_time_days)} days to arrive ({a.lead_time_source})")
            lines.append(f"Status: {label}" + (f". {a.reasons[0].text}" if a.reasons else ""))
            if a.suggested_qty:
                lines.append(f"Suggested order: about {_n(a.suggested_qty)} {u} ({a.suggested_qty_basis})")
            props = [p for p in self.proposals.list(item_code=it.code, limit=3) if p.status in ("awaiting", "manual", "snoozed", "ordered")]
            for p in props[:1]:
                lines.append(f"Latest purchase proposal: {PROPOSAL_WORDS.get(p.status, p.status)}" + (f" ({p.po_name})" if p.po_name else "") + (f", vendor {p.supplier_name}" if p.supplier_name else ""))
            facts += _facts_continue(facts, lines)
            sentence = f"{a.item_name} ({a.item_code}): you have {_n(a.on_hand)} {u}" + (f" ({_n(a.available)} available after {_n(a.reserved)} promised to customers)" if a.reserved > 0 else "") + "."
            if where:
                sentence += " " + where
            if rate:
                sentence += f" You sell about {_n(rate, 2)} a day" + (f", so it lasts about {_n(a.cover_days_on_hand)} days" if a.cover_days_on_hand is not None else "") + "."
            else:
                sentence += " There are no recent sales, so I cannot say how long it lasts."
            if a.in_transit:
                how = {"arriving": "counted as on its way", "shelf": "counted as on hand", "ignore": "not counted"}[(a.policy or {}).get("in_transit_stock", "arriving")]
                sentence += f" {_n(a.in_transit)} {u} {'are' if a.in_transit != 1 else 'is'} in transit between warehouses ({how})."
            sentence += (f" {_n(a.incoming)} {u} {'are' if a.incoming != 1 else 'is'} on order" + (f", due {_d(a.next_arrival)}." if a.next_arrival else " (no arrival date set).")) if a.incoming else " Nothing is on order."
            sentence += f" New stock takes about {_n(a.lead_time_days)} days to arrive."
            sentence += f" Status: {label}."
            if a.suggested_qty:
                sentence += f" Suggested reorder: about {_n(a.suggested_qty)} {u}."
            parts.append(sentence)
            blocks.append({"type": "kv", "title": f"{a.item_name} ({a.item_code})", "rows": [
                ["In stock", f"{_n(a.on_hand)} {u}"], *([["Where", _sole_warehouse(a) or "; ".join(split)]] if split else []),["Selling per day", _n(rate, 2) if rate else "no recent sales"],
                ["Lasts about", f"{_n(a.cover_days_on_hand)} days" if a.cover_days_on_hand is not None else "not known"],
                ["On order", f"{_n(a.incoming)} {u}" + (f", due {_d(a.next_arrival)}" if a.next_arrival else "") if a.incoming else "nothing"],
                *([["In transit", f"{_n(a.in_transit)} {u}"]] if a.in_transit else []),
                ["New stock takes", f"about {_n(a.lead_time_days)} days"], ["Status", label]] + ([["Suggested order", f"{_n(a.suggested_qty)} {u}"]] if a.suggested_qty else [])})
        follow = [f"Compare vendors for {codes[0]}", f"Purchase orders for {codes[0]}", "What else is running low?"]
        return Answer("item_stock", ("(Still talking about the same item.) " if plan.from_context else "") + " ".join(parts), facts, blocks, follow, codes)

    def _overview_answer(self, plan: Plan) -> Answer:
        rows = self._overview()
        if plan.group:
            rows = [r for r in rows if r.get("group") == plan.group]
        if not rows:
            return Answer("stock_overview", "I found no stock items" + (f" in the group {plan.group}" if plan.group else "") + ".", status="answered")
        out = [r for r in rows if r["status"] == "BREACH" and (r.get("position") is not None and r["position"] <= 0)]
        urgent = [r for r in rows if r["status"] == "BREACH" and r not in out and r.get("severity") == "CRITICAL"]
        soon = [r for r in rows if (r["status"] == "BREACH" and r not in out and r not in urgent) or r["status"] == "EARLY_WARNING"]
        fine = [r for r in rows if r["status"] == "OK"]
        off = [r for r in rows if r["status"] in ("DISABLED", "ERROR")]
        show = out if plan.stock_filter == "out" else (out + urgent + soon)
        scope = f" in {plan.group}" if plan.group else ""
        text = (f"Of {len(rows)} items{scope}: {len(out)} out of stock, {len(urgent)} to order now, {len(soon)} will run low soon, {len(fine)} are fine" + (f", {len(off)} not watched." if off else "."))
        if plan.stock_filter == "out":
            text = (f"{len(out)} item{'s are' if len(out) != 1 else ' is'} out of stock{scope}: " + ", ".join(f"{r['item_name']} ({r['item_code']})" for r in out[:8]) + "." if out else f"Nothing is out of stock{scope}.")
        elif show:
            text += " Needing attention: " + ", ".join(f"{r['item_name']} ({r['item_code']})" for r in show[:6]) + (f" and {len(show) - 6} more." if len(show) > 6 else ".")
        else:
            text += " Nothing needs ordering right now."
        lines = [f"{len(rows)} items watched{scope}: {len(out)} out of stock, {len(urgent)} to order now, {len(soon)} will run low soon, {len(fine)} fine, {len(off)} not watched"]
        for r in show[:10]:
            cover = f"lasts about {_n(r['cover_days'])} days" if r.get("cover_days") is not None else "days left not known"
            lines.append(f"{r['item_name']} ({r['item_code']}): {_n(r['on_hand'])} {r['uom']} on hand, {cover}, {_label_row(r)}")
        table = {"type": "table", "title": "Needing attention" if show else "Items", "columns": ["Item", "In stock", "Lasts about", "Status"],
                 "rows": [[f"{r['item_name']} ({r['item_code']})", f"{_n(r['on_hand'])} {r['uom']}", f"{_n(r['cover_days'])} days" if r.get("cover_days") is not None else "–", _label_row(r)] for r in show[:10]]}
        follow = [f"Tell me about {r['item_code']}" for r in show[:3]] + ["Which purchase orders are open?"]
        return Answer("stock_overview", text, _facts(lines), [table] if show else [], follow[:4], [r["item_code"] for r in show[:3]])

    def _vendor_compare(self, plan: Plan) -> Answer:
        parts, facts, blocks, codes = [], [], [], []
        cfg = self.settings.procurement()
        for it in plan.items[:2]:
            a, _src = self.inventory.analyze_one(it.code)
            codes.append(it.code)
            qty, basis = plan.qty, "the quantity you asked about"
            if not qty and a.suggested_qty:
                qty, basis = a.suggested_qty, "the suggested reorder size"
            if not qty and cfg.fallback_qty:
                qty, basis = cfg.fallback_qty, "your default order quantity"
            if not qty:
                return Answer("vendor_compare", f"How many {a.uom} of {a.item_name} should I compare for? For example: compare vendors for {it.code} for 500.", status="clarify",
                              follow_ups=[f"Compare vendors for {it.code} for 500"], items=codes)
            ranking = self.procurement.preview(it.code, qty, a.cover_days_on_hand, a.lead_time_days or 7.0, a.uom)
            vs = ranking["vendors"]
            elig = [v for v in vs if v["eligible"]]
            lines = [f"Comparison for {_n(qty)} {a.uom} of {a.item_name} ({it.code}), using {basis}"]
            for v in vs:
                if v["eligible"]:
                    tax = f", of which duties and taxes {self._money(v['tax_total'])}" if v.get("tax_lines") else ""
                    quoted = v.get("price_source") == "quotation"
                    lines.append(f"{v['name']}" + (f" ({v['country']})" if v.get("country") else "") + f": {self._money(v['unit_price'])} each" + (f" (supplier quotation {v['quote_ref']})" if quoted else "")
                                 + f", {self._money(v['total'])} in total{tax}, delivery about {_n(v['lead_days'], 0)} days"
                                 + (", on time for " + f"{v['on_time_rate'] * 100:.0f}% of past orders" if v.get("on_time_rate") is not None else ", no on-time record")
                                 + (f", {_n(v['payment_days'], 0)} days to pay" if v.get("payment_days") is not None else "") + f", score {v['score']:.0f} of 100")
                    for ln in v.get("tax_lines", []):
                        lines.append(f"{v['name']} {ln['name']}: {self._money(ln['amount'])}" + ("" if ln["in_cost"] else " (you claim this back, so it is not counted)"))
                else:
                    lines.append(f"{v['name']}: not compared, {v['why_not'] or v.get('excluded_reason') or 'no price on record'}")
            w = ranking.get("weights") or {}
            if w:
                lines.append("What counts, in percent: " + ", ".join(f"{k} {v:.0f}" for k, v in w.items()))
            lines += [f"Note: {n}" for n in ranking.get("notes", [])]
            facts += _facts_continue(facts, lines)
            if elig:
                best = elig[0]
                s = (f"For {_n(qty)} {a.uom} of {a.item_name}, the best vendor is {best['name']}: {self._money(best['unit_price'])} each"
                     + (f" (from supplier quotation {best['quote_ref']})" if best.get("price_source") == "quotation" else "") + f", {self._money(best['total'])} in total"
                     + (" including duties and taxes" if best.get("tax_lines") else "") + f", delivery in about {_n(best['lead_days'], 0)} days.")
                for v in elig[1:]:
                    s += f" {v['name']} was not chosen: {v['why_not']}"
                    s += "" if s.endswith(".") else "."
                for v in [x for x in vs if not x["eligible"]]:
                    s += f" {v['name']} could not be compared: {v['why_not'] or 'no price on record'}."
                for n in ranking.get("notes", []):
                    s += f" Note: {n}"
            else:
                s = f"None of your vendors can be compared for {a.item_name}: " + ("; ".join(f"{v['name']}: {v['why_not'] or 'no price on record'}" for v in vs) if vs else "no supplier is listed and there are no past orders for it") + "."
            parts.append(s + (f" (The quantity is {basis}.)" if basis != "the quantity you asked about" else ""))
            blocks.append({"type": "table", "title": f"Vendors for {_n(qty)} {a.uom} of {a.item_name}", "columns": ["Vendor", "Price each", "Total you pay", "Delivery", "On time", "Verdict"],
                           "rows": [[v["name"] + (f" ({v['country']})" if v.get("country") else ""), (self._money(v["unit_price"]) + (" (quotation)" if v.get("price_source") == "quotation" else "")) if v["eligible"] else "–", self._money(v["total"]) if v["eligible"] else "–",
                                     f"{_n(v['lead_days'], 0)} days" if v["eligible"] else "–", f"{v['on_time_rate'] * 100:.0f}%" if v.get("on_time_rate") is not None else "–",
                                     "Recommended" if v is (elig[0] if elig else None) else ("Not chosen" if v["eligible"] else "Not compared")] for v in vs]})
        return Answer("vendor_compare", ("(Still talking about the same item.) " if plan.from_context else "") + " ".join(parts), facts, blocks,
                      [f"How much {codes[0]} do we have?", f"Purchase orders for {codes[0]}"], codes)

    def _documents(self, plan: Plan, items: list[ItemInfo]) -> Answer:
        today = self.inventory.today()                               # the application's date (its clock and time zone), like every other answer: never the machine's
        since = None if (plan.status in ("open", "overdue") and not plan.period_given and not plan.docs) else today - timedelta(days=plan.since_days)
        code = plan.items[0].code if plan.items else None
        docs: list[dict] = []
        notfound: list[str] = []
        if plan.docs:
            for name in plan.docs:
                m = DOC_NAME.match(name)
                dt = DOC_PREFIX.get(m.group(1).upper()) if m else None
                if not dt:
                    notfound.append(name)
                    continue
                got = self.erp.get_documents(dt, name=name, limit=1)
                docs += got
                if not got:
                    notfound.append(name)
        else:
            kinds = plan.doctypes or ["Purchase Order", "Material Request"]
            for dt in kinds:
                docs += self.erp.get_documents(dt, item_code=code, since=since, limit=100 if plan.status else 20,          # the status is applied after reading: look at enough of them
                                               open_only=plan.status in ("open", "overdue", "draft"))                      # and the ERP itself leaves finished ones out when open ones are wanted
            docs.sort(key=lambda d: (d.get("date") or ""), reverse=True)
            docs = [d for d in docs if _status_match(d, plan.status, today)]
        waiting = []
        if not plan.docs and plan.status in (None, "open", "draft"):
            waiting = [p for p in self.proposals.list(item_code=code, limit=50) if p.status in ("awaiting", "manual", "snoozed")]
        lines: list[str] = []
        for d in docs[:10]:
            lines.append(_doc_line(d, code))
        for p in waiting[:5]:
            lines.append(f"Waiting in the Stock Assistant (not in ERPNext yet): {p.item_code}, {PROPOSAL_WORDS.get(p.status, p.status)}" + (f", vendor {p.supplier_name}" if p.supplier_name else ""))
        for n in notfound:
            lines.append(f"{n}: not found in ERPNext")
        if not lines:
            what = " and ".join({"Purchase Order": "purchase orders", "Material Request": "purchase requisitions", "Purchase Receipt": "purchase receipts"}[k] for k in (plan.doctypes or ["Purchase Order", "Material Request"]))
            return Answer("document_status", f"I found no {what}" + (f" for {plan.items[0].name} ({plan.items[0].code})" if plan.items else "") + (f" with that status" if plan.status else "") + (f" in the last {plan.since_days} days." if since else "."),
                          follow_ups=["What is running low?"], items=[i.code for i in plan.items[:1]])
        counts: dict[str, int] = {}
        for d in docs:
            counts[_doc_state(d)] = counts.get(_doc_state(d), 0) + 1
        summary_parts = []
        if docs:
            kinds = sorted({d["doctype"] for d in docs})
            noun = " and ".join({"Purchase Order": "purchase order", "Material Request": "purchase requisition", "Purchase Receipt": "purchase receipt"}[k] for k in kinds)
            summary_parts.append(f"I found {len(docs)} {noun}{'s' if len(docs) != 1 else ''}" + (f" for {plan.items[0].name}" if plan.items else "") + (f" in the last {plan.since_days} days" if not plan.docs and since else "")
                                 + (": " + ", ".join(f"{n} {k.lower()}" for k, n in counts.items()) if len(docs) > 1 else "") + ".")
            if len(docs) == 1:
                summary_parts.append(_doc_sentence(docs[0], code))
            else:
                if code:                                                                    # the quantity asked for: how much is ordered, and how much has still to come
                    ordered = sum(_item_qty(d, code)[0] for d in docs)
                    to_come = sum(max(_item_qty(d, code)[0] - _item_qty(d, code)[1], 0) for d in docs)
                    unit = _item_qty(docs[0], code)[2]
                    summary_parts.append(f"Together they are for {_n(ordered)} {unit}".rstrip() + f", of which {_n(to_come)} {unit}".rstrip() + " is still to come.")
                late = [d for d in docs if _overdue(d, today)]
                if late:
                    summary_parts.append(f"{len(late)} {'is' if len(late) == 1 else 'are'} overdue: " + ", ".join(f"{d['name']} (due {d['due']})" for d in late[:4]) + ".")
        if waiting:
            summary_parts.append(f"{len(waiting)} purchase suggestion{'s are' if len(waiting) != 1 else ' is'} still waiting in the Stock Assistant and not in ERPNext yet.")
        if notfound:
            summary_parts.append("I could not find " + ", ".join(notfound) + " in ERPNext.")
        if docs and code:                                                               # about one material: the vendor and the quantities are the point
            table = {"type": "table", "title": "Documents", "columns": ["Document", "Vendor", "Ordered", "Received", "Still to come", "Due", "State"],
                     "rows": [[d["name"], d.get("party") or "–", f"{_n(o)} {u}".strip(), _n(r), _n(max(o - r, 0)), d.get("due") or "–", _doc_state(d)]
                              for d in docs[:10] for o, r, u in [_item_qty(d, code)]]}
        else:
            table = {"type": "table", "title": "Documents", "columns": ["Document", "Vendor or type", "Quantity", "State", "Received", "Due", "Created by"],
                     "rows": [[d["name"], d.get("party") or (d.get("kind") or ""), _qty_summary(d), _doc_state(d), f"{d['per_received']:.0f}%" if d.get("per_received") is not None else "–", d.get("due") or "–",
                               (d.get("created_by") or "") + (f" (approved: {d['approved_by']})" if d.get("approved_by") and d.get("approved_by") not in (d.get("created_by") or "") else "")]
                              for d in docs[:10]]} if docs else None
        return Answer("document_status", " ".join(summary_parts), _facts(lines), [table] if table else [], ["What is running low?", "What is waiting for my OK?"],
                      [i.code for i in plan.items[:1]])

    def _pending(self, can_decide: bool) -> Answer:
        if not can_decide:
            return Answer("pending_approvals", "Only people who can approve orders in ERPNext can see the requests waiting for approval. You can still ask about stock, vendors and orders.", status="answered")
        pend = self.approvals.list("pending", 50)
        if not pend:
            return Answer("pending_approvals", "Nothing is waiting for your OK right now.", actions=[{"label": "Open Needs your OK", "view": "ok"}])
        by_kind: dict[str, int] = {}
        for r in pend:
            k = {"inventory.reorder": "reorder", "procurement.po": "purchase order", "procurement.pr": "purchase requisition", "procurement.manual": "request needing your decision",
                 "inventory.monitor_down": "warning", "inventory.alert": "stock alert"}.get(r.kind, "request")
            by_kind[k] = by_kind.get(k, 0) + 1
        text = f"{len(pend)} request{'s are' if len(pend) != 1 else ' is'} waiting for your OK: " + ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in by_kind.items()) + "."
        lines = [f"{r.request.title} ({r.request.severity}), asked {r.created_at:%d %b %H:%M}" for r in pend[:10]]
        table = {"type": "table", "title": "Waiting for your OK", "columns": ["Request", "Urgency", "Asked"], "rows": [[r.request.title, r.request.severity.title().replace("_", " "), f"{r.created_at:%d %b %H:%M}"] for r in pend[:10]]}
        return Answer("pending_approvals", text, _facts([text] + lines), [table], ["What is running low?"], [], [{"label": "Open Needs your OK", "view": "ok"}])


# ====================================================================== small helpers
def _sole_warehouse(a: Any) -> Optional[str]:
    """The one warehouse that holds all of an item's stock (not a transit warehouse), else None."""
    holders = [w for w in a.warehouses if w["on_hand"] or w["reserved"]]
    return holders[0]["warehouse"] if len(holders) == 1 and not holders[0]["in_transit"] else None


def _where_sentence(a: Any, split: list[str]) -> str:
    """Where an item's stock is, as a sentence: "By warehouse: Stores - TTC 120 pcs; Shop floor 40 pcs." or, when one warehouse holds it all, "All of it is in Stores - TTC."
    Empty when the ERP did not say."""
    if not split:
        return ""
    sole = _sole_warehouse(a)
    return f"All of it is in {sole}." if sole else "By warehouse: " + "; ".join(split) + "."


def _facts_continue(existing: list[Fact], lines: list[str]) -> list[Fact]:
    start = len(existing) + 1
    return [Fact(f"F{i}", t) for i, t in enumerate(lines, start=start)]


def _label_row(r: dict) -> str:
    if r["status"] == "DISABLED":
        return "Not watched"
    if r["status"] == "ERROR":
        return "Problem"
    if r["status"] == "OK":
        return "Fine"
    if r["status"] == "EARLY_WARNING":
        return "Keep an eye on it"
    if r.get("position") is not None and r["position"] <= 0:
        return "Out of stock"
    return "Order now" if r.get("severity") == "CRITICAL" else "Order soon"


def _short(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    if text and getattr(exc, "shown_as_is", False):         # the message is already a sentence for people (a circuit that is open): no exception name in front of it
        return text[:200]
    return f"{type(exc).__name__}: {text[:160]}" if text else type(exc).__name__


def _doc_state(d: dict) -> str:
    if d["docstatus"] == 2:
        return "Cancelled"
    if d["docstatus"] == 0:
        return "Draft (not submitted)"
    return d.get("status") or "Submitted"


def _overdue(d: dict, today: date) -> bool:
    if d["docstatus"] != 1 or not d.get("due") or d["doctype"] != "Purchase Order":
        return False
    try:
        return date.fromisoformat(d["due"]) < today and (d.get("per_received") or 0) < 100 and d.get("status") not in ("Completed", "Closed", "Cancelled")
    except ValueError:
        return False


def _status_match(d: dict, want: Optional[str], today: date) -> bool:
    if want is None:
        return True
    state = _doc_state(d).lower()
    if want == "draft":
        return d["docstatus"] == 0
    if want == "submitted":
        return d["docstatus"] == 1
    if want == "cancelled":
        return d["docstatus"] == 2
    if want == "done":
        return d["docstatus"] == 1 and (state in ("completed", "received") or (d.get("per_received") or 0) >= 100)
    if want == "overdue":
        return _overdue(d, today)
    return d["docstatus"] != 2 and state not in ("completed", "closed", "received", "cancelled")           # open


def _item_qty(d: dict, code: Optional[str]) -> tuple[float, float, str]:
    """(ordered, received, unit) of one material on a document: the sum over its lines for that item (all lines if the item is not found)."""
    lines = [l for l in d["lines"] if code and l.get("item_code") == code] or d["lines"]
    return (sum(float(l.get("qty") or 0) for l in lines), sum(float(l.get("done_qty") or 0) for l in lines), (lines[0].get("uom") or "") if lines else "")


def _qty_summary(d: dict) -> str:
    """What a document is for, in a few words: the first line, and how many more there are."""
    if not d["lines"]:
        return "–"
    l = d["lines"][0]
    return f"{_n(l['qty'])} {l.get('uom') or ''} {l.get('item_name') or l.get('item_code')}".replace("  ", " ") + (f" +{len(d['lines']) - 1} more" if len(d["lines"]) > 1 else "")


def _doc_line(d: dict, code: Optional[str] = None) -> str:
    first = sorted(d["lines"], key=lambda l: 0 if code and l.get("item_code") == code else 1)          # the material asked about comes first
    items = ", ".join(f"{_n(l['qty'])} {l.get('uom') or ''} {l.get('item_name') or l.get('item_code')}".replace("  ", " ") + (f" ({_n(l.get('done_qty') or 0)} received)" if d["docstatus"] == 1 else "")
                      for l in first[:3]) + (f" and {len(d['lines']) - 3} more lines" if len(d["lines"]) > 3 else "")
    bits = [f"{d['doctype']} {d['name']}", f"state {_doc_state(d)}"]
    if d.get("party"):
        bits.append(f"vendor {d['party']}")
    if d.get("kind"):
        bits.append(f"type {d['kind']}")
    if items:
        bits.append(items)
    if d.get("per_received") is not None:
        bits.append(f"{d['per_received']:.0f}% received")
    if d.get("per_billed") is not None:
        bits.append(f"{d['per_billed']:.0f}% billed")
    if d.get("due"):
        bits.append(f"due {d['due']}")
    if d.get("created_by"):
        bits.append(f"created by {d['created_by']}")
    if d.get("approved_by"):
        bits.append(f"approved in the Stock Assistant by {d['approved_by']}")
    return ", ".join(bits)


def _doc_sentence(d: dict, code: Optional[str] = None) -> str:
    noun = {"Purchase Order": "purchase order", "Material Request": "purchase requisition", "Purchase Receipt": "purchase receipt"}[d["doctype"]]
    s = f"{d['name']} is a {noun}" + (f" with {d['party']}" if d.get("party") else "") + f", {_doc_state(d).lower()}."
    if d["lines"]:
        l = next((x for x in d["lines"] if code and x.get("item_code") == code), d["lines"][0])
        s += f" It is for {_n(l['qty'])} {l.get('uom') or ''} {l.get('item_name') or l.get('item_code')}".replace("  ", " ") + (f" and {len(d['lines']) - 1} more line{'s' if len(d['lines']) > 2 else ''}" if len(d["lines"]) > 1 else "") + "."
    if d.get("per_received") is not None and d["docstatus"] == 1:
        s += f" {d['per_received']:.0f}% has been received" + (f", due {d['due']}" if d.get("due") else "") + "."
    elif d.get("due"):
        s += f" It is due {d['due']}."
    if d.get("created_by"):
        s += f" Created by {d['created_by']}" + (f", approved in the Stock Assistant by {d['approved_by']}." if d.get("approved_by") else ".")
    return s

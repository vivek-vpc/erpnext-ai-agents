"""Stock count assistant: schedule cycle counts, and explain the differences a count finds.

Schedule. Items are ranked by the value of what is used of them in a year (daily use x 365 x unit value). The items making up the first 80 % of that value are class A,
the next 15 % class B, the rest (and items with no use) class C. Each class is counted every `count_<class>_days`. An item is due when its last count (the latest
Stock Reconciliation in the ERP, or never) is that old. Each week the most urgent `counts_per_week` due items make the count list, A first. Items whose stock is
negative in the ERP are always put on the list: the figure is certainly wrong.

Variances. When a count is posted (a submitted Stock Reconciliation), each line that differs from the system by `variance_notable_pct` or more (and by at least
one unit) is explained with what the ERP shows, never guessed:
  * receipts still in draft for the item (goods on the shelf that the ERP does not have yet: counted MORE than the system);
  * stock in transit between warehouses;
  * a count that is a whole multiple of the system figure (or the other way round): probably counted in a different unit;
  * the same item was off in the same direction at its previous count: a recurring loss or gain, look at how it is issued or received;
  * the size of the difference in days of use, and its value.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Optional

from ai_munimji_engine.core.approvals.models import ACTION_DETAILS, ApprovalOption, ApprovalRequest, Section
from ai_munimji_engine.core.common import iso
from ai_munimji_engine.core.settings_store import SettingsError
from ai_munimji_engine.packs.stock_procurement.inventory.analysis import _d, _n

log = logging.getLogger(__name__)
KIND_COUNT_PLAN = "inventory.count_plan"
KIND_COUNT_VARIANCE = "inventory.count_variance"
COUNT_HISTORY_DAYS = 730
VARIANCE_LOOKBACK_DAYS = 14
UNIT_MULTIPLES = (2, 4, 5, 6, 10, 12, 20, 24, 25, 50, 100, 1000)
A_SHARE, B_SHARE = 0.80, 0.95


def abc_classes(usage_value: dict[str, float]) -> dict[str, str]:
    """A for the items that make up the first 80 % of the usage value, B for the next 15 %, C for the rest and for items with no usage value."""
    total = sum(v for v in usage_value.values() if v > 0)
    out, running = {}, 0.0
    for code, v in sorted(usage_value.items(), key=lambda kv: (-kv[1], kv[0])):
        if v <= 0 or total <= 0:
            out[code] = "C"
            continue
        share_before = running / total
        running += v
        out[code] = "A" if share_before < A_SHARE else "B" if share_before < B_SHARE else "C"
    return out


def explain(line: Any, daily_use: float, unit_value: Optional[float], draft_receipts: list[str], in_transit: float, previous: Optional[dict[str, Any]], uom: str) -> list[str]:
    """Plain sentences about one count difference, from what the ERP shows. Pure."""
    diff = line.counted_qty - line.system_qty
    out = []
    word = "more" if diff > 0 else "less"
    out.append(f"Counted {_n(line.counted_qty)} {uom}, the system had {_n(line.system_qty)}: {_n(abs(diff))} {uom} {word}"
               + (f" (about {_n(abs(diff) / daily_use, 1)} days of use)" if daily_use > 0 else "")
               + (f", worth about {abs(diff) * unit_value:,.2f}" if unit_value else "") + ".")
    if diff > 0 and draft_receipts:
        out.append(f"Goods receipt{'s' if len(draft_receipts) > 1 else ''} {', '.join(draft_receipts)} for this item {'are' if len(draft_receipts) > 1 else 'is'} still a draft: "
                   "goods that arrived but are not in the system yet would explain counting more.")
    if in_transit > 0:
        out.append(f"{_n(in_transit)} {uom} is in transit between warehouses: check whether it was counted where it was going or where it came from.")
    big, small = max(line.counted_qty, line.system_qty), min(line.counted_qty, line.system_qty)
    if small > 0:
        ratio = big / small
        k = next((m for m in UNIT_MULTIPLES if abs(ratio - m) < 0.02 * m), None)
        if k:
            out.append(f"One figure is {k} times the other: the count may have been made in a different unit (a box or pack of {k} instead of single {uom}).")
    if previous and previous.get("variance") and (previous["variance"] > 0) == (diff > 0):
        out.append(f"At the previous count ({_d(previous['date'])}) it was also {word} than the system ({_n(abs(previous['variance']))} {uom}): a recurring difference - look at how "
                   "it is issued, received or recorded.")
    if len(out) == 1:
        out.append("Nothing in the ERP explains it (no draft receipt, nothing in transit, no unit mix-up): recount it before the difference is accepted.")
    return out


class CycleCounts:
    def __init__(self, app: Any) -> None:
        self.app = app

    @property
    def cfg(self) -> Any:
        return self.app.settings.followup()

    def _counts(self, since: date) -> Optional[list[Any]]:
        read = getattr(self.app.erp, "get_stock_counts", None)
        if read is None:
            return None
        return read(since)

    def _usage(self) -> tuple[list[Any], dict[str, Any], dict[str, float], dict[str, float]]:
        """(items, stock, daily use, unit value) for every monitored item."""
        inv, erp = self.app.inventory, self.app.erp
        today = inv.today()
        planned = []
        for it in erp.list_items():
            try:
                policy, sources = self.app.settings.policy_with_sources(it.code, it.group)
            except SettingsError:
                continue
            planned.append((it, policy, sources))
        if not planned:
            return [], {}, {}, {}
        stock, usage = inv._load(planned, today)
        rates = {it.code: sum(u.qty for u in usage.get(it.code, []) if today - timedelta(days=90) <= u.day < today) / 90 for it, _, _ in planned}
        values: dict[str, float] = {}
        read = getattr(erp, "get_stock_values", None)
        if read is not None:
            try:
                values = read([it.code for it, _, _ in planned]) or {}
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read stock values for the count plan: %s", exc)
        unit = {c: values[c] / stock[c].on_hand for c in values if c in stock and stock[c].on_hand > 0 and values[c] > 0}
        return [it for it, _, _ in planned], stock, rates, unit

    def schedule(self) -> dict[str, Any]:
        """Every item with its class, when it was last counted and when it is due. Updates the stored schedule."""
        cfg, today = self.cfg, self.app.inventory.today()
        counts = self._counts(today - timedelta(days=COUNT_HISTORY_DAYS))
        if counts is None:
            return {"available": False, "note": "this ERP connection cannot read stock counts", "items": []}
        items, stock, rates, unit = self._usage()
        classes = abc_classes({it.code: rates.get(it.code, 0.0) * 365 * (unit.get(it.code) or 0.0) for it in items})
        last: dict[str, date] = {}
        for c in counts:
            if c.submitted and c.posting_date and (c.item_code not in last or c.posting_date > last[c.item_code]):
                last[c.item_code] = c.posting_date
        every = {"A": cfg.count_a_days, "B": cfg.count_b_days, "C": cfg.count_c_days}
        rows, now = [], iso(self.app.clock())
        for it in items:
            cls = classes.get(it.code, "C")
            counted = last.get(it.code)
            due = counted + timedelta(days=every[cls]) if counted else today
            negative = bool(stock.get(it.code) and stock[it.code].on_hand < 0)
            rows.append({"item_code": it.code, "item_name": it.name, "class": cls, "last_counted": counted.isoformat() if counted else None, "due_on": due.isoformat(),
                         "overdue_days": max((today - due).days, 0), "negative_stock": negative, "uom": it.uom})
            self.app.stock_stores.counts.upsert_schedule(it.code, cls, counted.isoformat() if counted else None, due.isoformat(), now)
        rows.sort(key=lambda r: (not r["negative_stock"], r["due_on"], r["class"], r["item_code"]))
        return {"available": True, "items": rows, "classes": {k: sum(1 for r in rows if r["class"] == k) for k in "ABC"},
                "due_now": sum(1 for r in rows if r["due_on"] <= today.isoformat() or r["negative_stock"])}

    def plan_week(self) -> dict[str, Any]:
        """This week's count list: negative stock first, then the due items, A before B before C, most overdue first."""
        cfg, today = self.cfg, self.app.inventory.today()
        sched = self.schedule()
        if not sched["available"]:
            return {**sched, "plan": []}
        week_end = (today + timedelta(days=6)).isoformat()
        due = [r for r in sched["items"] if r["negative_stock"] or r["due_on"] <= week_end]
        due.sort(key=lambda r: (not r["negative_stock"], r["class"], -r["overdue_days"], r["item_code"]))
        plan = due[:cfg.counts_per_week]
        return {"available": True, "plan": plan, "due": len(due), "classes": sched["classes"], "week_of": today.isoformat()}

    def run_plan(self, dry_run: bool = False) -> dict[str, Any]:
        if not self.cfg.counts_enabled:
            return {"sent": False, "note": "cycle counts are switched off"}
        p = self.plan_week()
        if not p.get("plan") or dry_run:
            return {"sent": False, "items": len(p.get("plan") or [])}
        today = self.app.inventory.today().isoformat()
        for r in p["plan"]:
            self.app.stock_stores.counts.mark_planned(r["item_code"], today)
        record = self.app.approvals.submit(build_plan_request(p))
        return {"sent": True, "approval_id": record.id, "items": len(p["plan"])}

    # ---------------------------------------------------------------- variances
    def variances(self, days: int = VARIANCE_LOOKBACK_DAYS, only_new: bool = False) -> dict[str, Any]:
        cfg, today = self.cfg, self.app.inventory.today()
        counts = self._counts(today - timedelta(days=COUNT_HISTORY_DAYS))
        if counts is None:
            return {"available": False, "note": "this ERP connection cannot read stock counts", "documents": []}
        recent = [c for c in counts if c.submitted and c.posting_date and c.posting_date >= today - timedelta(days=days)]
        notable = [c for c in recent if abs(c.counted_qty - c.system_qty) >= 1 and abs(c.counted_qty - c.system_qty) / max(abs(c.system_qty), 1.0) * 100 >= cfg.variance_notable_pct]
        if only_new:
            notable = [c for c in notable if not self.app.stock_stores.counts.reviewed(c.document, c.item_code, c.warehouse)]
        if not notable:
            return {"available": True, "documents": [], "lines": 0}
        items, stock, rates, unit = self._usage()
        names = {it.code: it for it in items}
        docs: dict[str, dict[str, Any]] = {}
        for c in notable:
            prev = max((p for p in counts if p.item_code == c.item_code and p.submitted and p.posting_date and p.posting_date < c.posting_date and abs(p.counted_qty - p.system_qty) >= 1),
                       key=lambda p: p.posting_date, default=None)
            drafts = self._draft_receipts(c.item_code)
            it = names.get(c.item_code)
            uom = it.uom if it else "units"
            st = stock.get(c.item_code)
            lines = explain(c, rates.get(c.item_code, 0.0), unit.get(c.item_code), drafts, float(st.in_transit) if st else 0.0,
                            {"variance": prev.counted_qty - prev.system_qty, "date": prev.posting_date.isoformat()} if prev else None, uom)
            d = docs.setdefault(c.document, {"document": c.document, "posted": c.posting_date.isoformat(), "lines": []})
            d["lines"].append({"item_code": c.item_code, "item_name": it.name if it else c.item_code, "warehouse": c.warehouse, "counted": c.counted_qty, "system": c.system_qty,
                               "variance": c.counted_qty - c.system_qty, "uom": uom, "explanation": lines})
        return {"available": True, "documents": list(docs.values()), "lines": len(notable)}

    def _draft_receipts(self, item_code: str) -> list[str]:
        try:
            return [d["name"] for d in self.app.erp.get_documents("Purchase Receipt", item_code=item_code, limit=10) if int(d.get("docstatus") or 0) == 0][:3]
        except Exception:  # noqa: BLE001 - unknown = not mentioned
            return []

    def run_variances(self, dry_run: bool = False) -> dict[str, Any]:
        if not self.cfg.counts_enabled:
            return {"sent": 0, "note": "cycle counts are switched off"}
        v = self.variances(only_new=True)
        if not v.get("documents") or dry_run:
            return {"sent": 0, "lines": v.get("lines", 0)}
        sent = 0
        now = iso(self.app.clock())
        for d in v["documents"]:
            with self.app.stores.transaction():
                record = self.app.approvals.submit(build_variance_request(d), deliver=False)
                for l in d["lines"]:
                    self.app.stock_stores.counts.add_review(d["document"], l["item_code"], l["warehouse"], l["variance"], record.id, now)
            self.app.approvals.deliver(record)
            sent += 1
        return {"sent": sent, "lines": v["lines"]}


def build_plan_request(p: dict[str, Any]) -> ApprovalRequest:
    lines = []
    for r in p["plan"]:
        why = "negative stock in the ERP" if r["negative_stock"] else ("never counted" if not r["last_counted"] else f"last counted {_d(r['last_counted'])}")
        lines.append(f"{r['item_name']} ({r['item_code']}), class {r['class']}: {why}.")
    more = p["due"] - len(p["plan"])
    return ApprovalRequest(kind=KIND_COUNT_PLAN, title=f"This week's stock counts: {len(p['plan'])} item{'s' if len(p['plan']) != 1 else ''}",
                           headline=f"Count {len(p['plan'])} items this week" + (f" ({more} more are due)" if more > 0 else ""), severity="INFO",
                           sections=[Section(heading="Count these", lines=lines),
                                     Section(heading="How", lines=["Count what is on the shelf, then post the figures as a Stock Reconciliation in ERPNext. Differences are "
                                                                   "explained to you the next morning."])],
                           details=[Section(heading="Classes", lines=[f"A {p['classes']['A']}, B {p['classes']['B']}, C {p['classes']['C']} items: A is the 80% of the value "
                                                                      "you use, counted most often."])],
                           options=[ApprovalOption(action="ack", label="Planned", style="primary", description="The counts are planned"),
                                    ApprovalOption(action=ACTION_DETAILS, label="Details", terminal=False, description="How the list was made")],
                           ref_type="count_plan", ref_id=p["week_of"], facts={"items": [r["item_code"] for r in p["plan"]]})


def build_variance_request(d: dict[str, Any]) -> ApprovalRequest:
    sections = [Section(heading=f"{l['item_name']} ({l['item_code']}) in {l['warehouse']}", lines=l["explanation"]) for l in d["lines"]]
    return ApprovalRequest(kind=KIND_COUNT_VARIANCE, title=f"Stock count {d['document']}: {len(d['lines'])} difference{'s' if len(d['lines']) != 1 else ''} to look at",
                           headline=f"{d['document']}: " + "; ".join(f"{l['item_code']} {'+' if l['variance'] > 0 else ''}{_n(l['variance'])} {l['uom']}" for l in d["lines"][:4]),
                           severity="INFO", sections=sections, ref_type="stock_count", ref_id=d["document"],
                           options=[ApprovalOption(action="ack", label="Checked", style="primary", description="I have looked at the differences"),
                                    ApprovalOption(action=ACTION_DETAILS, label="Details", terminal=False, description="Show all of it")],
                           facts={"document": d["document"], "item_code": d["lines"][0]["item_code"], "items": [l["item_code"] for l in d["lines"]]})

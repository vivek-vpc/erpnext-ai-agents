"""Vendor scorecard: on-time record, price drift and quality problems per vendor, ranked, with a plain-language note on why a vendor was ranked lower.

From the goods receipts of the last `scorecard_days` (and the deliveries that are late now):

  on time       share of receipt lines posted on or before the order line's due date, and how late the late ones were. A delivery still outstanding past its due
                date counts as late now.
  price drift   per item, the average price of the later half of the period against the earlier half (quantity weighted), averaged over the items that have both.
                Only a rise counts against a vendor; a fall is reported.
  quality       share of the received quantity that was rejected, and the receipts that did not match their order (more than ordered, wrong price).

Score out of 100 (higher is better): 50 points for on time, 25 for price, 25 for quality. A vendor with fewer than MIN_LINES receipt lines is listed but not ranked.
Pure: the numbers come from the ERP's documents, nothing from a language model.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Optional, Sequence

from ai_munimji_engine.packs.stock_procurement.erp import ReceiptLine
from ..inventory.analysis import _n

MIN_LINES = 2
W_ON_TIME, W_PRICE, W_QUALITY = 50.0, 25.0, 25.0
FULL_PRICE_PENALTY_PCT = 20.0          # a 20 % rise costs all the price points
FULL_REJECT_PENALTY = 0.10             # 10 % rejected costs all the rejection points (15 of the 25 quality points)
FULL_MISMATCH_PENALTY = 0.5            # half the receipts not matching costs the other 10


@dataclass
class VendorScore:
    supplier: str
    name: str
    lines: int = 0
    on_time: Optional[float] = None          # 0..1
    avg_days_late: Optional[float] = None    # of the late ones
    late_now: int = 0                        # open orders past due now
    price_drift_pct: Optional[float] = None
    drift_example: str = ""
    received: float = 0.0
    rejected: float = 0.0
    mismatches: int = 0
    receipts: int = 0
    score: Optional[float] = None
    rank: Optional[int] = None
    penalties: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    why_lower: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _drift(lines: list[ReceiptLine]) -> tuple[Optional[float], str]:
    by_item: dict[str, list[ReceiptLine]] = defaultdict(list)
    for l in lines:
        if l.rate and l.posting_date and l.accepted_qty > 0:
            by_item[l.item_code].append(l)
    drifts, example, biggest = [], "", 0.0
    for code, ls in by_item.items():
        ls.sort(key=lambda l: l.posting_date)
        if len(ls) < 2 or ls[0].posting_date == ls[-1].posting_date:
            continue
        mid = ls[0].posting_date + (ls[-1].posting_date - ls[0].posting_date) / 2
        early, late = [l for l in ls if l.posting_date <= mid], [l for l in ls if l.posting_date > mid]
        if not early or not late:
            continue
        p0 = sum(l.rate * l.accepted_qty for l in early) / sum(l.accepted_qty for l in early)
        p1 = sum(l.rate * l.accepted_qty for l in late) / sum(l.accepted_qty for l in late)
        pct = (p1 - p0) / p0 * 100 if p0 else 0.0
        drifts.append(pct)
        if abs(pct) > abs(biggest):
            biggest, example = pct, f"{code} from {_n(p0, 2)} to {_n(p1, 2)}"
    if not drifts:
        return None, ""
    return sum(drifts) / len(drifts), example


def score_vendors(lines: Sequence[ReceiptLine], late_now: dict[str, int], recorded: dict[str, list[dict[str, Any]]], names: Optional[dict[str, str]] = None) -> list[VendorScore]:
    by_vendor: dict[str, list[ReceiptLine]] = defaultdict(list)
    for l in lines:
        if l.submitted and l.supplier and l.received_qty > 0:        # a return (negative quantity) is goods going back, not a delivery: it is neither on time nor late
            by_vendor[l.supplier].append(l)
    vendors = sorted(set(by_vendor) | set(late_now))
    out = []
    for sup in vendors:
        ls = by_vendor.get(sup, [])
        v = VendorScore(supplier=sup, name=(ls[0].supplier_name if ls else (names or {}).get(sup, sup)), lines=len(ls), late_now=late_now.get(sup, 0))
        timed = [l for l in ls if l.po_due and l.posting_date]
        late = [(l.posting_date - l.po_due).days for l in timed if l.posting_date > l.po_due]
        judged = len(timed) + v.late_now
        if judged:
            v.on_time = (len(timed) - len(late)) / judged
            v.avg_days_late = sum(late) / len(late) if late else None          # of the delivered ones; the open ones are not finished yet
        v.price_drift_pct, v.drift_example = _drift(ls)
        v.received = sum(l.received_qty for l in ls)
        v.rejected = sum(l.rejected_qty for l in ls)
        v.receipts = len({l.receipt for l in ls})
        v.mismatches = len([p for p in recorded.get(sup, []) if p.get("kind") in ("quantity", "price")])
        _penalise(v)
        out.append(v)
    ranked = sorted([v for v in out if v.score is not None], key=lambda v: (-v.score, v.name))
    for i, v in enumerate(ranked, 1):
        v.rank = i
        if i > 1:
            v.why_lower = _why_lower(v, ranked[0])
    return ranked + sorted([v for v in out if v.score is None], key=lambda v: v.name)


def _penalise(v: VendorScore) -> None:
    if v.lines < MIN_LINES and not v.late_now:
        v.notes.append(f"Only {v.lines} receipt line{'s' if v.lines != 1 else ''} in the period: not enough deliveries to judge.")
        return
    p_time = (1 - v.on_time) * W_ON_TIME if v.on_time is not None else W_ON_TIME / 2
    p_price = min(max(v.price_drift_pct or 0.0, 0.0) / FULL_PRICE_PENALTY_PCT, 1.0) * W_PRICE
    reject_share = v.rejected / v.received if v.received else 0.0
    p_quality = min(reject_share / FULL_REJECT_PENALTY, 1.0) * 15.0 + (min(v.mismatches / max(v.receipts, 1) / FULL_MISMATCH_PENALTY, 1.0) * 10.0 if v.receipts else 0.0)
    v.penalties = {"on_time": round(p_time, 1), "price": round(p_price, 1), "quality": round(p_quality, 1)}
    v.score = round(100 - p_time - p_price - p_quality, 1)
    if v.on_time is not None:
        v.notes.append(f"On time {_n(v.on_time * 100, 0)}%" + (f"; the late ones were {_n(v.avg_days_late, 1)} days late on average" if v.avg_days_late else "")
                       + (f"; {v.late_now} order{'s are' if v.late_now != 1 else ' is'} late right now" if v.late_now else "") + ".")
    else:
        v.notes.append("No due dates to judge delivery against.")
    if v.price_drift_pct is not None:
        word = "rose" if v.price_drift_pct > 0.05 else "fell" if v.price_drift_pct < -0.05 else "held steady"
        v.notes.append(f"Prices {word}" + (f" {_n(abs(v.price_drift_pct), 1)}% over the period ({v.drift_example})" if word != "held steady" else "") + ".")
    if v.received:
        v.notes.append(f"{_n(reject_share * 100, 1)}% of the goods rejected ({_n(v.rejected)} of {_n(v.received)})"
                       + (f"; {v.mismatches} receipt difference{'s' if v.mismatches != 1 else ''} with the order" if v.mismatches else "") + ".")


def _why_lower(v: VendorScore, top: VendorScore) -> str:
    """Plain words: the factors where this vendor lost the most points compared with the best one."""
    gaps = {k: v.penalties.get(k, 0.0) - top.penalties.get(k, 0.0) for k in ("on_time", "price", "quality")}
    reasons = []
    for k, gap in sorted(gaps.items(), key=lambda kv: -kv[1]):
        if gap < 1.0:
            continue
        if k == "on_time":
            reasons.append(f"it delivered on time {_n((v.on_time or 0) * 100, 0)}% of the time against {_n((top.on_time or 0) * 100, 0)}%"
                           + (f" ({v.late_now} late now)" if v.late_now else ""))
        elif k == "price":
            reasons.append(f"its prices rose {_n(v.price_drift_pct or 0, 1)}%" + (f" ({v.drift_example})" if v.drift_example else ""))
        else:
            share = v.rejected / v.received * 100 if v.received else 0.0
            reasons.append(f"{_n(share, 1)}% of its goods were rejected" + (f" and {v.mismatches} receipts did not match the order" if v.mismatches else ""))
    if not reasons:
        return f"Ranked just below {top.name}: the scores are within a point or two of each other."
    return f"Ranked below {top.name} because " + "; ".join(reasons) + "."


def scorecard(app: Any, days: Optional[int] = None) -> dict[str, Any]:
    """The scorecard as plain data (the tool, the API and the CLI use it)."""
    from datetime import timedelta
    cfg = app.settings.followup()
    days = days or cfg.scorecard_days
    read = getattr(app.erp, "get_receipt_lines", None)
    if read is None:
        return {"available": False, "note": "this ERP connection cannot read goods receipts"}
    today: date = app.inventory.today()
    lines = read(today - timedelta(days=days))
    late_now: dict[str, int] = defaultdict(int)
    names: dict[str, str] = {}
    try:
        for o in app.followup.chaser.late_orders():
            late_now[o["supplier"]] += 1
            names[o["supplier"]] = o["vendor"]
    except Exception as exc:  # noqa: BLE001 - the scorecard still works from the receipts alone
        logging.getLogger(__name__).warning("the scorecard leaves out the deliveries late now: %s", type(exc).__name__)
    since = (app.clock() - timedelta(days=days)).isoformat()
    scores = score_vendors(lines, dict(late_now), app.followup.receipts.recorded_problems(since), names)
    return {"available": True, "days": days, "vendors": [{k: s.to_dict()[k] for k in ("rank", "name", "supplier", "score", "on_time", "avg_days_late", "late_now", "price_drift_pct",
                                                                                         "received", "rejected", "mismatches", "lines", "notes", "why_lower")} for s in scores]}

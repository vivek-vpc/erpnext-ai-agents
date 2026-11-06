"""Slow and dead stock: items that tie up cash, with a suggested action.

  dead   stock on hand and no use for `dead_after_days` (or none in the whole history read)
  slow   still used, but the stock lasts more than `slow_cover_days` at the current use

Suggested action, in this order:
  return to vendor     dead, and it was received within `return_window_days`: ask the vendor to take it back
  cut the open order   slow or dead and more is still on order: reduce or cancel that order first
  discount or clear    dead, bought longer ago: sell it off, use it elsewhere or write it off - and stop reordering
  stop reordering      slow: no new orders until it is down to the normal level (lead + safety + target days)

The cash tied up is the ERP's own stock value (ERPNext: Bin stock_value); the excess is the part above the normal level. Pure classification, numbers from the ERP.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Optional

from ai_munimji_engine.core.approvals.models import ACTION_DETAILS, ApprovalOption, ApprovalRequest, Section
from ai_munimji_engine.core.settings_store import SettingsError
from ai_munimji_engine.packs.stock_procurement.inventory.analysis import _d, _n

log = logging.getLogger(__name__)
KIND_SLOW = "inventory.slow_stock"
RATE_WINDOW_DAYS = 90
MAX_LISTED = 15


@dataclass
class SlowItem:
    item_code: str
    item_name: str
    uom: str
    kind: str                         # dead | slow
    on_hand: float
    daily_use: float
    cover_days: Optional[float]
    last_used: Optional[str]
    days_since_use: Optional[int]
    value: Optional[float]
    excess_qty: float
    excess_value: Optional[float]
    on_order: float
    last_received: Optional[str]
    last_supplier: Optional[str]
    action: str                       # return_to_vendor | cut_open_order | discount_or_clear | stop_reordering
    advice: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify(code: str, name: str, uom: str, on_hand: float, on_order: float, usage: list[Any], today: date, history_days: int, normal_days: float, value: Optional[float],
             last_receipt: Optional[tuple[date, str]], cfg: Any) -> Optional[SlowItem]:
    if on_hand <= 0:
        return None
    used = [u.day for u in usage if u.qty > 0]
    last = max(used) if used else None
    since = (today - last).days if last else None
    recent = sum(u.qty for u in usage if today - timedelta(days=RATE_WINDOW_DAYS) <= u.day < today)
    rate = recent / RATE_WINDOW_DAYS
    cover = on_hand / rate if rate > 0 else None
    dead = since is None or since >= cfg.dead_after_days
    slow = not dead and cover is not None and cover >= cfg.slow_cover_days
    if not (dead or slow):
        return None
    normal = rate * normal_days
    excess = max(on_hand - normal, 0.0)
    excess_value = value * excess / on_hand if value is not None and on_hand else None
    got_days = (today - last_receipt[0]).days if last_receipt else None
    u, n = uom, _n
    if dead and got_days is not None and got_days <= cfg.return_window_days and last_receipt and last_receipt[1]:
        action = "return_to_vendor"
        advice = (f"Ask {last_receipt[1]} to take it back: it was received {got_days} days ago ({_d(last_receipt[0].isoformat())}) and "
                  + ("none has been used since." if since is None or since >= got_days else f"nothing has been used for {since} days."))
    elif on_order > 0:
        action = "cut_open_order"
        advice = f"{n(on_order)} {u} more is still on order: reduce or cancel that order first, then stop reordering."
    elif dead:
        action = "discount_or_clear"
        advice = ("No use in the history read" if since is None else f"No use for {since} days") + ": sell it at a discount, use it up elsewhere or write it off, and stop reordering it."
    else:
        action = "stop_reordering"
        advice = (f"Stock lasts about {n(cover)} days at {n(rate, 2)} {u}/day: stop reordering until it is down to the normal level of about {n(normal)} {u} "
                  f"({n(normal_days)} days); {n(excess)} {u} is above that.")
    return SlowItem(code, name, uom, "dead" if dead else "slow", on_hand, rate, cover, last.isoformat() if last else None, since, value, excess if not dead else on_hand,
                    excess_value if not dead else value, on_order, last_receipt[0].isoformat() if last_receipt else None, last_receipt[1] if last_receipt else None, action, advice)


class SlowStock:
    def __init__(self, app: Any) -> None:
        self.app = app

    def report(self) -> dict[str, Any]:
        cfg = self.app.settings.followup()
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
            return {"items": [], "total_value": 0.0, "listed": 0}
        stock, usage = inv._load(planned, today)
        codes = [it.code for it, _, _ in planned]
        values = self._read("get_stock_values", codes) or {}
        received = self._read("last_purchase_dates", codes) or {}
        rows = []
        for it, policy, _ in planned:
            st = stock.get(it.code)
            if st is None:
                continue
            normal_days = (policy.lead_time_days or it.lead_time_days or policy.default_lead_time_days) + policy.safety_stock_days + policy.target_cover_days
            row = classify(it.code, it.name, it.uom, st.on_hand, st.incoming, usage.get(it.code, []), today, policy.history_days, normal_days, values.get(it.code),
                           received.get(it.code), cfg)
            if row is not None and (row.value is None or row.value >= cfg.slow_min_value):
                rows.append(row)
        rows.sort(key=lambda r: (-(r.excess_value or 0.0), -(r.value or 0.0), r.item_code))
        total = sum(r.excess_value or 0.0 for r in rows)
        return {"items": [r.to_dict() for r in rows], "dead": sum(1 for r in rows if r.kind == "dead"), "slow": sum(1 for r in rows if r.kind == "slow"),
                "cash_tied_up": round(total, 2), "values_known": bool(values), "listed": len(rows)}

    def _read(self, method: str, codes: list[str]) -> Optional[dict]:
        read = getattr(self.app.erp, method, None)
        if read is None:
            return None
        try:
            return read(codes)
        except Exception as exc:  # noqa: BLE001 - without values the list is still useful (sorted by quantity)
            log.warning("could not read %s for the slow-stock list: %s", method, exc)
            return None

    def run(self, dry_run: bool = False) -> dict[str, Any]:
        cfg = self.app.settings.followup()
        if not cfg.slow_stock_enabled:
            return {"sent": False, "note": "the slow-stock list is switched off"}
        rep = self.report()
        if not rep["items"] or dry_run:
            return {"sent": False, "items": rep["listed"]}
        record = self.app.approvals.submit(build_slow_request(rep, self.app.inventory.today()))
        return {"sent": True, "approval_id": record.id, "items": rep["listed"]}


ACTION_WORDS = {"return_to_vendor": "return to vendor", "cut_open_order": "cut the open order", "discount_or_clear": "discount or clear", "stop_reordering": "stop reordering"}


def build_slow_request(rep: dict[str, Any], today: date) -> ApprovalRequest:
    items = rep["items"][:MAX_LISTED]
    money = f"{rep['cash_tied_up']:,.2f}" if rep.get("values_known") else "unknown (the ERP gave no stock values)"
    lines = []
    for r in items:
        worth = f" - {r['excess_value']:,.2f} tied up" if r.get("excess_value") is not None else ""
        lines.append(f"{r['item_name']} ({r['item_code']}), {r['kind']}: {_n(r['on_hand'])} {r['uom']}{worth}. {ACTION_WORDS[r['action']].capitalize()}: {r['advice']}")
    return ApprovalRequest(kind=KIND_SLOW, title=f"Stock that ties up cash: {rep['dead']} dead, {rep['slow']} slow item{'s' if rep['listed'] != 1 else ''}",
                           headline=f"{rep['listed']} items tie up about {money} beyond what you use", severity="INFO",
                           sections=[Section(heading="Items", lines=lines), Section(heading="What the actions mean", lines=[
                               "Return to vendor: bought recently and not used - ask the vendor to take it back.",
                               "Cut the open order: more is still coming - reduce or cancel that order first.",
                               "Discount or clear: not used for a long time - sell it off, use it elsewhere or write it off.",
                               "Stop reordering: still used, but far more is held than needed - no new orders until it comes down.",
                               "Nothing is changed for you: to stop the stock check from suggesting a reorder, switch the item off or change its thresholds in Settings."])],
                           options=[ApprovalOption(action="ack", label="Seen", style="primary", description="I have looked at the list"),
                                    ApprovalOption(action=ACTION_DETAILS, label="Details", terminal=False, description="How the list was made")],
                           details=[Section(heading="How", lines=[f"Made on {_d(today.isoformat())}. Dead = no use for the set number of days; slow = stock lasts longer than the "
                                                                  "set number of days. Cash tied up = the ERP's stock value of the part above the normal level."])],
                           ref_type="slow_stock", ref_id=today.isoformat(), facts={"items": [r["item_code"] for r in items], "cash_tied_up": rep["cash_tied_up"]})

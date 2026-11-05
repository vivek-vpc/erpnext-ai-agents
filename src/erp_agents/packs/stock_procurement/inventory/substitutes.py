"""Substitute items: when an item is short and a new order cannot arrive in time, suggest an APPROVED alternative that is in stock.

"Approved" means the ERP lists it as an alternative of the item (ERPNext: Item Alternative; a two-way record works both ways). Nothing else is ever suggested:
similar names or groups are not a reason to swap one item for another. The suggestion is a line in the stock alert; whether the alternative may be used for a given
customer or job is the person's call.
"""
from __future__ import annotations

import logging
from typing import Any

from ai_munimji_engine.core.approvals.models import Section
from ai_munimji_engine.core.common import untrusted
from ai_munimji_engine.packs.stock_procurement.inventory.analysis import STATUS_BREACH, Analysis, _n

log = logging.getLogger(__name__)
MAX_ALTERNATIVES = 5


def short_and_unavailable(a: Analysis) -> bool:
    """Short now, and a new order (or the open one) does not arrive before it runs out."""
    if a.status != STATUS_BREACH:
        return False
    if a.available <= 0:
        return True
    if a.gap_days is not None and a.gap_days > 0:
        return True
    return a.cover_days_on_hand is not None and a.cover_days_on_hand < a.lead_time_days


class Substitutes:
    def __init__(self, app: Any) -> None:
        self.app = app

    def find(self, item_code: str) -> dict[str, Any]:
        read = getattr(self.app.erp, "get_item_alternatives", None)
        if read is None:
            return {"item_code": item_code, "available": False, "alternatives": [], "note": "this ERP connection cannot read item alternatives"}
        try:
            codes = read(item_code)[:MAX_ALTERNATIVES]
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the alternatives of %s: %s", item_code, exc)
            return {"item_code": item_code, "available": False, "alternatives": [], "note": "the item alternatives could not be read"}
        rows = []
        for code in codes:
            try:
                a, _ = self.app.inventory.analyze_one(code)
            except Exception as exc:  # noqa: BLE001 - an alternative that is not a stock item here is reported, not hidden
                rows.append({"item_code": untrusted(code, 140), "error": untrusted(str(exc), 120)})
                continue
            rows.append({"item_code": a.item_code, "item_name": a.item_name, "uom": a.uom, "available": a.available, "on_order": a.incoming,
                         "cover_days": a.cover_days_on_hand, "status": a.status})
        rows.sort(key=lambda r: -(r.get("available") or 0))
        note = "" if codes else "ERPNext lists no approved alternative for this item (Item Alternative)."
        return {"item_code": item_code, "available": True, "alternatives": rows, "note": note}

    def section(self, a: Analysis) -> list[Section]:
        """The alert's 'Approved alternative' section, only when the item is short and cannot be replenished in time."""
        if not short_and_unavailable(a):
            return []
        found = self.find(a.item_code)
        alts = [r for r in found["alternatives"] if not r.get("error")]
        if not alts:
            return []
        usable = [r for r in alts if (r.get("available") or 0) > 0]
        if not usable:
            return [Section(heading="Approved alternative", lines=[f"The approved alternatives ({', '.join(r['item_code'] for r in alts)}) are out of stock too."])]
        lines = []
        for r in usable[:3]:
            cover = f", about {_n(r['cover_days'])} days at its own use" if r.get("cover_days") is not None else ""
            lines.append(f"{r['item_name']} ({r['item_code']}) is an approved alternative in ERPNext: {_n(r['available'])} {r['uom']} available{cover}.")
        lines.append("Use it only where your customer or process accepts the alternative; it is not reserved or moved by this message.")
        return [Section(heading="Approved alternative", lines=lines)]

"""Calendar of festivals and promotions -> expected demand.

The owner enters events (dates + an uplift %, for all items, some item groups or some items). For any day inside an
event, expected daily demand = base rate x (1 + uplift/100). Nothing is guessed: no events, or no event in the
window, means every number is exactly what it was before.

If several events cover the same day, the strongest rise applies (or the deepest fall when none rises); they are
never multiplied together.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Optional, Sequence

HORIZON_DAYS = 730


@dataclass(frozen=True)
class Window:
    name: str
    kind: str
    start: date
    end: date
    factor: float                 # 1 + uplift/100
    uplift_pct: float

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"], d["end"] = self.start.isoformat(), self.end.isoformat()
        return d


def applies(event: Any, item_code: str, item_group: Optional[str]) -> bool:
    """An event with no item groups and no items is for everything; otherwise it must name this item or its group."""
    if not event.item_groups and not event.items:
        return True
    return item_code in event.items or (item_group is not None and item_group in event.item_groups)


def active_windows(events: Iterable[Any], item_code: str, item_group: Optional[str], today: date,
                   horizon_days: int = HORIZON_DAYS) -> list[Window]:
    """Events for this item that overlap today..today+horizon, soonest first."""
    last = today + timedelta(days=horizon_days)
    out = [Window(e.name, e.kind, e.start, e.end, 1 + e.uplift_pct / 100.0, e.uplift_pct)
           for e in events if applies(e, item_code, item_group) and e.end >= today and e.start <= last]
    return sorted(out, key=lambda w: (w.start, w.name))


def factor_on(day: date, windows: Sequence[Window]) -> float:
    f = [w.factor for w in windows if w.covers(day)]
    if not f:
        return 1.0
    return max(f) if max(f) > 1.0 else min(f)


def total_demand(rate: float, windows: Sequence[Window], today: date, days: float) -> float:
    """Expected demand over the next `days` days (fractions allowed), today included."""
    if not windows or days <= 0:
        return rate * max(days, 0.0)
    whole, part = int(math.floor(days)), days - math.floor(days)
    total = sum(rate * factor_on(today + timedelta(days=k), windows) for k in range(whole))
    return total + part * rate * factor_on(today + timedelta(days=whole), windows)


def days_until_demand(amount: float, rate: float, windows: Sequence[Window], today: date, cap: int = HORIZON_DAYS) -> float:
    """How many days until `amount` of stock has been used up. Same as amount/rate when no event applies."""
    if rate <= 0:
        return math.inf
    if not windows or amount <= 0:
        return amount / rate
    remaining = amount
    for k in range(cap):
        d = rate * factor_on(today + timedelta(days=k), windows)
        if d <= 0:
            continue
        if remaining <= d:
            return k + remaining / d
        remaining -= d
    return cap + remaining / rate                      # beyond the horizon: carry on at the base rate


def describe(windows: Sequence[Window]) -> str:
    return "; ".join(f"{w.name} ({w.uplift_pct:+g}%, {w.start:%d %b}" + (f" to {w.end:%d %b}" if w.end != w.start else "") + ")" for w in windows)

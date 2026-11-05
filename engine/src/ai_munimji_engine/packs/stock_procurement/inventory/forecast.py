"""Demand forecast for reorder points: seasonality and open sales orders, not just average daily use. Pure: no I/O, no language model.

The reorder point is the demand expected while a new order is on its way (lead time + safety days):

  expected use      the planned daily use, raised by last year's seasonal pattern for exactly those days (the same weeks last year against the normal level
                    before them), through the demand calendar if one applies. Seasonality only ever RAISES the reorder point: a quiet season last year is not a
                    reason to hold less now.
  open sales orders quantity still to deliver on submitted sales orders due by the end of those days. When the stock settings already subtract "reserved for
                    sales orders" from available stock, that quantity is out of the stock position already and is NOT counted a second time.

  reorder point     the larger of expected use and the open sales orders due (when they are not already subtracted)

The stock position (available + on order, as the stock settings count it) at or below the reorder point means it is time to order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Optional, Sequence

from ai_munimji_engine.packs.stock_procurement.erp import DailyUsage, SalesDue
from ..settings import Policy
from .events import Window, total_demand

SEASONAL_MIN_UPLIFT = 1.15       # last year's swings smaller than +15 % are noise
EPS = 1e-9


@dataclass
class Forecast:
    window_days: float                    # lead time + safety days
    planned_rate: float                   # the usual daily use (the analysis' planning rate)
    seasonal_index: float = 1.0           # applied to the planned rate for the window (1 = not applied)
    seasonal_note: str = ""
    last_year_window_rate: Optional[float] = None
    last_year_normal_rate: Optional[float] = None
    expected_use: float = 0.0             # over the window
    sales_due: float = 0.0                # open sales orders due by the end of the window
    sales_already_reserved: bool = False  # the stock settings subtract them already: not counted again
    sales_last_due: Optional[str] = None
    reorder_point: float = 0.0
    position: float = 0.0
    below: bool = False
    differs: bool = False                 # the forecast says more than the plain lead-time rule (seasonality applied or sales orders counted)
    weekly: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _daily(usage: Sequence[DailyUsage]) -> dict[date, float]:
    out: dict[date, float] = {}
    for u in usage:
        out[u.day] = out.get(u.day, 0.0) + u.qty
    return out


def _total(dm: dict[date, float], start: date, days: int) -> float:
    return sum(dm.get(start + timedelta(days=i), 0.0) for i in range(days))


def seasonal_index(dm: dict[date, float], today: date, start_offset: int, days: int, data_days: int, policy: Policy) -> tuple[float, str, Optional[float], Optional[float]]:
    """Last year's demand for the `days` starting `start_offset` days from today, against last year's normal level (the base window before it).
    Returns (index applied, why, last year's rate in those days, last year's normal rate)."""
    if not policy.seasonal_adjust:
        return 1.0, "seasonal adjustment is switched off", None, None
    need = 365 + policy.base_window_days
    if data_days < need:
        return 1.0, f"only {data_days} days of history; a seasonal comparison needs about {need}", None, None
    ly = today - timedelta(days=365) + timedelta(days=start_offset)
    window = _total(dm, ly, max(days, 1)) / max(days, 1)
    normal = _total(dm, today - timedelta(days=365) - timedelta(days=policy.base_window_days), policy.base_window_days) / policy.base_window_days
    if normal <= EPS:
        return 1.0, "no usage in last year's normal period", window, normal
    ratio = window / normal
    if ratio >= SEASONAL_MIN_UPLIFT:
        return min(ratio, policy.seasonal_max_factor), "applied", window, normal
    return 1.0, "last year's demand for these days was not meaningfully higher", window, normal


def forecast_reorder_point(usage: Sequence[DailyUsage], sales: Sequence[SalesDue], policy: Policy, today: date, planned_rate: float, window_days: float,
                           position: float, data_days: int, windows: Sequence[Window] = (), weeks: int = 0) -> Forecast:
    """The reorder point over `window_days`, and (when `weeks` > 0) a week-by-week forecast of expected use and sales orders due, for the forecast tool."""
    dm = _daily(usage)
    days = max(int(round(window_days)), 1)
    index, note, ly_window, ly_normal = seasonal_index(dm, today, 0, days, data_days, policy)
    rate = planned_rate * index
    expected = total_demand(rate, windows, today, window_days) if rate > EPS else 0.0
    until = today + timedelta(days=days)
    due = [s for s in sales if s.qty > 0 and (s.due is None or s.due <= until)]
    sales_due = sum(s.qty for s in due)
    reserved = "reserved_qty" in policy.stock_subtract
    use_sales = policy.sales_orders_in_forecast and not reserved
    rp = max(expected, sales_due if use_sales else 0.0)
    f = Forecast(window_days=window_days, planned_rate=planned_rate, seasonal_index=index, seasonal_note=note, last_year_window_rate=ly_window, last_year_normal_rate=ly_normal,
                 expected_use=expected, sales_due=sales_due, sales_already_reserved=reserved and sales_due > 0,
                 sales_last_due=max((s.due for s in due if s.due), default=None).isoformat() if any(s.due for s in due) else None,
                 reorder_point=rp, position=position)
    f.below = rp > EPS and position <= rp
    f.differs = index > 1.0 or (use_sales and sales_due > expected + EPS)
    for w in range(max(int(weeks), 0)):
        start = today + timedelta(days=7 * w)
        wk_index, _n, _a, _b = seasonal_index(dm, today, 7 * w, 7, data_days, policy)
        wk_sales = sum(s.qty for s in sales if s.qty > 0 and s.due is not None and start <= s.due < start + timedelta(days=7))
        if w == 0:
            wk_sales += sum(s.qty for s in sales if s.qty > 0 and (s.due is None or s.due < start))          # overdue lines belong to this week
        f.weekly.append({"week_starting": start.isoformat(), "expected_use": total_demand(planned_rate * wk_index, windows, start, 7) if planned_rate > EPS else 0.0,
                         "seasonal_index": wk_index, "sales_orders_due": wk_sales})
    return f

"""Deterministic stock analysis. No LLM, no I/O: same inputs always give the same numbers.

The result (`Analysis`) is a plain, JSON-serialisable structure that carries every figure the
reasoning text quotes, so a decision can always be re-explained from the stored snapshot.

Concepts
  position        on hand (+ open PO quantity if the policy counts it)
  planning rate   units/day used for *today's* threshold checks (recent / base / blend)
  forecast rate   planning rate x seasonal uplift - used only for predictive early warnings
  breach          a configured rule is already violated            -> kind BREACH
  early warning   nothing violated, but at the forecast rate a rule  -> kind EARLY_WARNING
                  will be violated within `lookahead_days`
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Optional, Sequence

from ..settings import Policy
from ai_munimji_engine.packs.stock_procurement.erp import IN_TRANSIT_COLUMN, IN_TRANSIT_LABEL, STOCK_COLUMNS, DailyUsage, ItemInfo, SalesDue, StockInfo, column_phrase, stock_column
from .events import DATE_HORIZON_DAYS, Window, date_after, days_until_demand, describe, total_demand
from .forecast import forecast_reorder_point

EPS = 1e-9
SEASONAL_MIN_UPLIFT = 1.15       # ignore last-year swings smaller than +15 % (noise)
MIN_WEEKS_FOR_PERCENTILE = 6

# severity levels (stored as ints)
SEV_INFO, SEV_EARLY, SEV_HIGH, SEV_CRITICAL = 0, 1, 2, 3
SEV_NAMES = {0: "INFO", 1: "EARLY_WARNING", 2: "HIGH", 3: "CRITICAL"}

STATUS_OK, STATUS_EARLY, STATUS_BREACH = "OK", "EARLY_WARNING", "BREACH"


@dataclass
class Reason:
    code: str               # min_qty | min_days_cover | lead_time_rule | stockout_before_arrival | predicted_crossing | no_stock
    text: str               # one-line human explanation with the numbers
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class UsageStats:
    data_days: int                       # days of data actually available (new items < window)
    recent_days: int
    base_days: int
    recent_rate: float
    base_rate: float
    planning_rate: float
    method: str
    recent_weight: float
    trend_pct: Optional[float]           # recent vs base, in %
    trend: Optional[str]                 # "up" | "down" | None (only when beyond trend_notable_pct)
    high_rate: Optional[float]           # 80th percentile of weekly average usage
    weekly_cv: Optional[float]
    nonzero_days: int


@dataclass
class Seasonality:
    available: bool
    note: str
    ratio: Optional[float] = None        # last year's forward window / its preceding base window
    applied_factor: float = 1.0
    last_year_forward_rate: Optional[float] = None
    last_year_base_rate: Optional[float] = None


@dataclass
class Analysis:
    item_code: str
    item_name: str
    item_group: Optional[str]
    uom: str
    as_of: str
    status: str = STATUS_OK
    severity: int = SEV_INFO
    # stock
    on_hand: float = 0.0
    reserved: float = 0.0
    available: float = 0.0                         # on hand minus reserved (when the policy subtracts reservations)
    incoming: float = 0.0                          # open purchase orders (shown whether or not the policy counts them)
    planned: float = 0.0                           # open production work orders (ditto)
    in_transit: float = 0.0                        # stock in transit warehouses (shown whether or not the policy counts it)
    breakdown: list[dict[str, Any]] = field(default_factory=list)   # every stock column, and how the policy uses it
    warehouses: list[dict[str, Any]] = field(default_factory=list)  # the same stock warehouse by warehouse (largest first): warehouse, on_hand, reserved, incoming, in_transit
    projected: Optional[float] = None              # the ERP's own projected quantity (comparison only)
    windows: list[Any] = field(default_factory=list)   # calendar events applied to this item (see inventory/events.py)
    next_arrival: Optional[str] = None
    position: float = 0.0                          # available + what is on the way (as the policy counts it)
    # time figures
    usage: Optional[UsageStats] = None
    seasonality: Optional[Seasonality] = None
    forecast_rate: float = 0.0
    cover_days_on_hand: Optional[float] = None
    cover_days_position: Optional[float] = None
    stockout_date: Optional[str] = None
    lead_time_days: float = 7.0
    lead_time_source: str = "default"
    safety_days: float = 0.0
    replenish_in_days: Optional[float] = None
    replenish_date: Optional[str] = None
    gap_days: Optional[float] = None               # >0 means stock-out before replenishment
    latest_order_date: Optional[str] = None
    days_to_trigger: Optional[float] = None        # predictive: days until the first rule would trip
    trigger_rule: Optional[str] = None
    high_rate_cover_days: Optional[float] = None
    forecast: Optional[dict[str, Any]] = None              # the reorder point from seasonality and open sales orders (inventory/forecast.py)
    # outputs
    reasons: list[Reason] = field(default_factory=list)
    drivers: list[str] = field(default_factory=list)
    rules: list[dict[str, Any]] = field(default_factory=list)
    suggested_qty: Optional[float] = None
    suggestion_type: str = "none"                  # replenish | bridge | none
    suggested_qty_basis: str = ""
    confidence: str = "Medium"
    confidence_why: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return {STATUS_BREACH: "BREACH", STATUS_EARLY: "EARLY_WARNING"}.get(self.status, "OK")

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["windows"] = [w.to_dict() for w in self.windows]
        return out


# ===================================================================== helpers
def _daily_map(usage: list[DailyUsage]) -> dict[date, float]:
    out: dict[date, float] = {}
    for u in usage:
        out[u.day] = out.get(u.day, 0.0) + u.qty
    return out


def _window_total(dm: dict[date, float], start: date, end_exclusive: date) -> float:
    total, d = 0.0, start
    while d < end_exclusive:
        total += dm.get(d, 0.0)
        d += timedelta(days=1)
    return total


def _percentile(values: list[float], pct: float) -> float:
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def _ceil_multiple(qty: float, multiple: float) -> float:
    return math.ceil(qty / multiple - 1e-9) * multiple


# ===================================================================== stats
def compute_usage(usage: list[DailyUsage], item: ItemInfo, today: date, policy: Policy) -> UsageStats:
    dm = _daily_map(usage)
    earliest_possible = today - timedelta(days=policy.history_days)
    data_start = max(earliest_possible, item.created) if item.created else earliest_possible
    if item.created is None and usage:
        # Unknown creation date: trust the full window (zero days are real zero demand).
        data_start = earliest_possible
    data_days = max(0, (today - data_start).days)

    base_days = max(1, min(policy.base_window_days, data_days))
    recent_days = max(1, min(policy.recent_window_days, data_days))
    base_total = _window_total(dm, today - timedelta(days=base_days), today)
    recent_total = _window_total(dm, today - timedelta(days=recent_days), today)
    base_rate = base_total / base_days
    recent_rate = recent_total / recent_days

    if policy.usage_method == "base":
        planning, w = base_rate, 0.0
    elif policy.usage_method == "recent":
        planning, w = recent_rate, 1.0
    else:
        w = policy.recent_weight
        planning = w * recent_rate + (1 - w) * base_rate

    trend_pct = trend = None
    if base_rate > EPS and data_days >= max(policy.recent_window_days * 2, 21):
        trend_pct = (recent_rate / base_rate - 1) * 100
        if trend_pct >= policy.trend_notable_pct:
            trend = "up"
        elif trend_pct <= -policy.trend_notable_pct:
            trend = "down"

    high_rate = cv = None
    weeks = base_days // 7
    if weeks >= MIN_WEEKS_FOR_PERCENTILE:
        rates = []
        for w_i in range(weeks):
            end = today - timedelta(days=7 * w_i)
            rates.append(_window_total(dm, end - timedelta(days=7), end) / 7)
        high_rate = _percentile(rates, 0.8)
        mean = sum(rates) / len(rates)
        if mean > EPS:
            var = sum((r - mean) ** 2 for r in rates) / len(rates)
            cv = math.sqrt(var) / mean

    nonzero = sum(1 for d, q in dm.items() if q > 0 and today - timedelta(days=base_days) <= d < today)
    return UsageStats(
        data_days=data_days, recent_days=recent_days, base_days=base_days,
        recent_rate=recent_rate, base_rate=base_rate, planning_rate=planning,
        method=policy.usage_method, recent_weight=w, trend_pct=trend_pct, trend=trend,
        high_rate=high_rate, weekly_cv=cv, nonzero_days=nonzero,
    )


def compute_seasonality(usage: list[DailyUsage], stats: UsageStats, today: date, policy: Policy) -> Seasonality:
    if not policy.seasonal_adjust:
        return Seasonality(False, "seasonal adjustment is switched off")
    need = 365 + policy.base_window_days
    if stats.data_days < need:
        return Seasonality(False, f"only {stats.data_days} days of history; seasonal comparison needs about {need}")
    dm = _daily_map(usage)
    ly_start = today - timedelta(days=365)
    fwd_end = ly_start + timedelta(days=policy.lookahead_days)
    base_start = ly_start - timedelta(days=policy.base_window_days)
    fwd = _window_total(dm, ly_start, fwd_end) / policy.lookahead_days
    base = _window_total(dm, base_start, ly_start) / policy.base_window_days
    if base <= EPS:
        return Seasonality(False, "no usage in last year's baseline period")
    ratio = fwd / base
    if ratio >= SEASONAL_MIN_UPLIFT:
        factor = min(ratio, policy.seasonal_max_factor)
        return Seasonality(True, "applied", ratio, factor, fwd, base)
    return Seasonality(True, "last year's demand for this period was not meaningfully higher", ratio, 1.0, fwd, base)


# ===================================================================== main entry
def analyze_item(
    item: ItemInfo,
    stock: StockInfo,
    usage: list[DailyUsage],
    policy: Policy,
    today: date,
    windows: Sequence[Window] = (),
    sales: Sequence[SalesDue] = (),
) -> Analysis:
    """`windows` = the calendar events (festivals, promotions) that apply to this item; none = demand is unchanged. `sales` = what is still to deliver on its open
    sales orders, with the dates the customers want it (none = not known)."""
    transit = max(stock.in_transit, 0.0)
    shelf = stock.on_hand + (transit if policy.in_transit_stock == "shelf" else 0.0)       # stock in transit counts as on the shelf only if the owner says so
    a = Analysis(
        item_code=item.code, item_name=item.name, item_group=item.group, uom=item.uom, as_of=today.isoformat(),
        on_hand=shelf, in_transit=transit, incoming=stock.incoming, planned=stock.planned, projected=stock.projected, warehouses=[asdict(w) for w in stock.by_warehouse],
        next_arrival=stock.next_arrival.isoformat() if stock.next_arrival else None,
        safety_days=policy.safety_stock_days,
        policy={k: v for k, v in policy.model_dump().items()},
    )

    # ---- lead time (admin override > ERP item > default). A figure that is not a number, or is more than ten years, is a typing mistake, not a lead time.
    chosen, ignored_lead = None, []
    for value, source in ((policy.lead_time_days, "admin setting"), (item.lead_time_days, "ERP item record")):
        if not value:
            continue
        if math.isfinite(value) and 0 < value <= DATE_HORIZON_DAYS:
            chosen = (value, source)
            break
        ignored_lead.append(f"The lead time in the {source} ({value:g} days) is not credible (a lead time must be a number of days, and not more than 10 years); it was ignored.")
    a.notes += ignored_lead
    if chosen:
        a.lead_time_days, a.lead_time_source = chosen
    else:
        a.lead_time_days = policy.default_lead_time_days
        a.lead_time_source = "default (no credible lead time)" if ignored_lead else "default (no lead time on item)"
        a.notes.append(f"No lead time on the item; assumed {policy.default_lead_time_days:g} days." if not ignored_lead else f"Assumed {policy.default_lead_time_days:g} days instead.")

    # ---- stock position
    # The owner chooses which ERP stock columns are subtracted (-> available) and added (-> position).
    u = item.uom
    subtracted = {c: max(stock_column(stock, c), 0.0) for c in policy.stock_subtract}
    added = {c: max(stock_column(stock, c), 0.0) for c in policy.stock_add}
    a.reserved = sum(subtracted.values())
    a.available = shelf - a.reserved
    arriving = {IN_TRANSIT_COLUMN: transit} if policy.in_transit_stock == "arriving" else {}
    supply = sum(added.values()) + sum(arriving.values())
    a.position = a.available + supply
    a.breakdown = [{"column": "actual_qty", "label": "On hand", "value": stock.on_hand, "use": "start"}] + [
        {"column": c, "label": label, "value": stock_column(stock, c),
         "use": "subtract" if c in subtracted else "add" if c in added else "not used"}
        for c, (label, _attr, _phrase) in STOCK_COLUMNS.items()
    ] + [{"column": IN_TRANSIT_COLUMN, "label": IN_TRANSIT_LABEL, "value": transit,
          "use": {"arriving": "add", "shelf": "on hand", "ignore": "not used"}[policy.in_transit_stock]}]
    if stock.on_hand < 0:
        a.notes.append("On-hand stock is negative in the ERP - check for unrecorded receipts.")
    if a.reserved > 0:
        a.notes.append(f"{_n(a.reserved)} {u} of the {_n(shelf)} on hand is reserved, so {_n(a.available)} {u} is available.")
    ignored = [f"{_n(b['value'])} {u} {column_phrase(b['column'])}" for b in a.breakdown[1:] if b["use"] == "not used" and b["value"] > 0]
    if ignored:
        a.notes.append("Not counted, as set in your stock settings: " + "; ".join(ignored) + ".")

    # ---- usage + forecast
    stats = compute_usage(usage, item, today, policy)
    season = compute_seasonality(usage, stats, today, policy)
    a.usage, a.seasonality = stats, season
    rate = stats.planning_rate
    a.forecast_rate = rate * season.applied_factor

    a.windows = list(windows)
    if rate > EPS:
        a.cover_days_on_hand = days_until_demand(max(a.available, 0.0), rate, windows, today)
        a.cover_days_position = days_until_demand(max(a.position, 0.0), rate, windows, today)
        a.stockout_date = _iso(date_after(today, a.cover_days_on_hand))
        if a.stockout_date is None:
            a.notes.append("No stock-out date is given: there is more than 10 years of cover at the current usage.")
        if stats.high_rate and stats.high_rate > EPS:
            a.high_rate_cover_days = max(a.available, 0.0) / stats.high_rate
        if windows:                                    # say what the calendar changed, with both figures
            plain = max(a.available, 0.0) / rate
            if abs(plain - a.cover_days_on_hand) >= 0.05:
                a.drivers.append(f"Your calendar changes demand ({describe(windows)}): cover is {_n(a.cover_days_on_hand)} days instead of "
                                 f"{_n(plain)} days at the usual rate.")
    else:
        a.notes.append("No recent consumption - days of cover cannot be calculated; only the quantity rule applies.")

    # ---- replenishment timing
    if stock.next_arrival and "ordered_qty" in added:        # an arrival date only matters if open orders are counted
        a.replenish_in_days = float((stock.next_arrival - today).days)
        a.replenish_date = stock.next_arrival.isoformat()
    elif supply > 0:
        what = " and ".join(f"{_n(v)} {u} {column_phrase(c)}" for c, v in {**added, **arriving}.items() if v > 0)
        a.notes.append(f"Open supply exists ({what}) but its arrival date is unknown; counted in position, timing not checked.")
    else:
        a.replenish_in_days = a.lead_time_days
        a.replenish_date = _iso(date_after(today, a.lead_time_days))
    if a.replenish_in_days is not None and a.cover_days_on_hand is not None:
        a.gap_days = a.replenish_in_days - a.cover_days_on_hand
    if a.cover_days_on_hand is not None and a.stockout_date is not None:           # no stock-out date (more than ten years of cover): no latest order date either
        a.latest_order_date = _iso(date_after(today, a.cover_days_on_hand - a.lead_time_days))

    # ---- rules
    lead_trigger = a.lead_time_days + policy.safety_stock_days
    if policy.forecast_reorder_point:
        a.forecast = forecast_reorder_point(usage, sales, policy, today, rate, lead_trigger, a.position, stats.data_days, windows).to_dict()
    _evaluate_rules(a, policy, rate, lead_trigger)

    # ---- confidence
    _confidence(a, stats, policy)

    # ---- predictive early warning (only if nothing is violated today)
    if a.status == STATUS_OK:
        _predict(a, policy, lead_trigger, windows, today)

    # ---- order suggestion
    if a.status != STATUS_OK:
        _suggest_quantity(a, policy, windows, today)

    return a


def _evaluate_rules(a: Analysis, policy: Policy, rate: float, lead_trigger: float) -> None:
    u = a.uom
    stats = a.usage
    assert stats is not None  # type narrowing: the analysis always fills usage before this runs  # nosec B101

    def add_rule(code: str, enabled: bool, threshold: Optional[float], value: Optional[float], tripped: bool, label: str) -> None:
        a.rules.append({"rule": code, "enabled": enabled, "threshold": threshold, "value": value, "tripped": tripped, "label": label})

    # min_qty
    if policy.min_qty is not None:
        tripped = a.position <= policy.min_qty
        add_rule("min_qty", True, policy.min_qty, a.position, tripped, "stock position vs minimum quantity")
        if tripped:
            a.reasons.append(Reason("min_qty", f"Stock position {_n(a.position)} {u} is at or below your minimum of {_n(policy.min_qty)} {u}.",
                                    {"position": a.position, "min_qty": policy.min_qty}))
    else:
        add_rule("min_qty", False, None, a.position, False, "minimum quantity rule is off")

    # min_days_cover
    if policy.min_days_cover is not None and a.cover_days_position is not None:
        tripped = a.cover_days_position <= policy.min_days_cover
        add_rule("min_days_cover", True, policy.min_days_cover, a.cover_days_position, tripped, "days of cover vs minimum days")
        if tripped:
            a.reasons.append(Reason("min_days_cover", f"Stock lasts about {_n(a.cover_days_position)} days, at or below your minimum of {_n(policy.min_days_cover)} days.",
                                    {"cover_days": a.cover_days_position, "min_days": policy.min_days_cover}))
    else:
        add_rule("min_days_cover", policy.min_days_cover is not None, policy.min_days_cover, a.cover_days_position, False,
                 "days-of-cover rule (needs recent usage)" if policy.min_days_cover is not None else "days-of-cover rule is off")

    # lead-time rule
    if policy.use_lead_time_rule and a.cover_days_position is not None:
        tripped = a.cover_days_position <= lead_trigger
        add_rule("lead_time_rule", True, lead_trigger, a.cover_days_position, tripped, "days of cover vs lead time + safety days")
        if tripped:
            a.reasons.append(Reason("lead_time_rule",
                                    f"Cover of {_n(a.cover_days_position)} days is within the supplier lead time of {_n(a.lead_time_days)} days "
                                    f"+ {_n(policy.safety_stock_days)} safety days - it is time to order.",
                                    {"cover_days": a.cover_days_position, "trigger_days": lead_trigger}))
    else:
        add_rule("lead_time_rule", policy.use_lead_time_rule, lead_trigger, a.cover_days_position, False, "lead-time rule")

    # the forecast reorder point: seasonality and open sales orders (only when it says more than the plain lead-time rule, and that rule has not tripped)
    f = a.forecast
    if f is not None:
        lead_tripped = any(r.code == "lead_time_rule" for r in a.reasons)
        tripped = bool(f["below"] and f["differs"] and not lead_tripped)
        add_rule("forecast_reorder_point", True, f["reorder_point"], a.position, tripped, "stock position vs forecast demand over lead time + safety days")
        if tripped:
            a.reasons.append(Reason("forecast_reorder_point", forecast_reason(f, u), {"reorder_point": f["reorder_point"], "position": a.position,
                                                                              "seasonal_index": f["seasonal_index"], "sales_due": f["sales_due"]}))
    else:
        add_rule("forecast_reorder_point", False, None, a.position, False, "forecast order point is off")

    # stock-out before a known arrival (even if position looks fine thanks to the open PO)
    if a.next_arrival and a.gap_days is not None and a.gap_days > 0 and stats.planning_rate > EPS:
        a.reasons.append(Reason("stockout_before_arrival",
                                f"Open order arrives on {_d(a.next_arrival)} but on-hand stock runs out about {_n(a.gap_days)} days earlier "
                                f"(around {_d(a.stockout_date)}).", {"gap_days": a.gap_days}))

    # "No stock" is only critical when nothing usable is on hand and nothing is on the way: free stock that is
    # zero or negative but fully covered by open orders / planned production (position above zero) is expected
    # to recover once they arrive.
    no_stock = a.available <= 0 and a.position <= 0
    if no_stock:
        if a.incoming > EPS:                          # something is on the way, but orders waiting to be filled already claim all of it
            claimed = f"the {_n(a.incoming)} {u} on order or in production is already needed for orders waiting to be filled"
            text = f"There is no stock on hand, and {claimed}." if a.on_hand <= 0 else f"There is no free stock: all {_n(a.on_hand)} {u} on hand is reserved, and {claimed}."
        else:
            text = ("There is no stock on hand and nothing on order or in production to replenish it." if a.on_hand <= 0 else
                    f"There is no free stock: all {_n(a.on_hand)} {u} on hand is reserved, and nothing is on order or in production.")
        a.reasons.insert(0, Reason("no_stock", text, {}))
    elif a.available <= 0:
        coming = a.position - a.available
        a.notes.append(
            f"Free stock is {_n(a.available)} {u} (on hand {_n(a.on_hand)}, reserved {_n(a.on_hand - a.available)}), but {_n(coming)} {u} is on order or in "
            f"production, so the stock position is {_n(a.position)} {u} once it arrives."
        )

    if a.reasons:
        a.status = STATUS_BREACH
        gap = a.gap_days is not None and a.gap_days > 0
        a.severity = SEV_CRITICAL if (no_stock or gap) else SEV_HIGH


def _predict(a: Analysis, policy: Policy, lead_trigger: float, windows: Sequence[Window] = (), today: Optional[date] = None) -> None:
    stats = a.usage
    assert stats is not None  # type narrowing: the analysis always fills usage before this runs  # nosec B101
    if not policy.predictive_enabled:
        return
    if stats.data_days < policy.min_history_days:
        a.notes.append(f"Only {stats.data_days} days of history (< {policy.min_history_days}); predictions skipped, thresholds still apply.")
        return
    r = a.forecast_rate
    if r <= EPS:
        return
    today = today or date.fromisoformat(a.as_of)

    def crossing(plain: float, threshold_qty: float) -> float:
        """Days until the position falls to `threshold_qty`: plain division, or day by day when the calendar changes demand."""
        return plain if not windows else days_until_demand(a.position - threshold_qty, r, windows, today)

    candidates: list[tuple[float, str]] = []
    plain_candidates: list[float] = []
    if policy.min_qty is not None:
        plain_candidates.append((a.position - policy.min_qty) / r)
        candidates.append((crossing(plain_candidates[-1], policy.min_qty), "min_qty"))
    if policy.min_days_cover is not None:
        plain_candidates.append(a.position / r - policy.min_days_cover)
        candidates.append((crossing(plain_candidates[-1], policy.min_days_cover * r), "min_days_cover"))
    if policy.use_lead_time_rule:
        plain_candidates.append(a.position / r - lead_trigger)
        candidates.append((crossing(plain_candidates[-1], lead_trigger * r), "lead_time_rule"))
    if not candidates:
        return
    t, rule = min(candidates, key=lambda c: c[0])
    a.days_to_trigger, a.trigger_rule = max(t, 0.0), rule
    if windows and abs(t - min(plain_candidates)) >= 0.05:
        a.drivers.append(f"Your calendar changes demand ({describe(windows)}): the threshold is reached in about {_n(max(t, 0.0))} days "
                         f"instead of {_n(max(min(plain_candidates), 0.0))} days at the usual rate.")
    if t > policy.lookahead_days:
        return

    a.status, a.severity = STATUS_EARLY, SEV_EARLY
    when = date.fromisoformat(a.as_of) + timedelta(days=a.days_to_trigger)
    rule_label = {
        "min_qty": f"your minimum quantity of {_n(policy.min_qty)} {a.uom}",
        "min_days_cover": f"your minimum of {_n(policy.min_days_cover)} days of cover",
        "lead_time_rule": f"the order point (lead time {_n(a.lead_time_days)} + {_n(policy.safety_stock_days)} safety days)",
    }[rule]
    a.reasons.append(Reason(
        "predicted_crossing",
        f"Not below any threshold yet, but at the expected usage of {_n(r, 2)} {a.uom}/day you will reach {rule_label} "
        f"in about {_n(a.days_to_trigger)} days (around {when.strftime('%d %b')}).",
        {"days_to_trigger": a.days_to_trigger, "rule": rule, "forecast_rate": r},
    ))

    # What is pushing the forecast up? (drivers)
    if stats.trend == "up" and stats.trend_pct is not None:
        a.drivers.append(
            f"Usage is up {_n(stats.trend_pct, 0)}%: last {stats.recent_days} days averaged {_n(stats.recent_rate, 2)}/day "
            f"vs {_n(stats.base_rate, 2)}/day over {stats.base_days} days."
        )
    se = a.seasonality
    if se and se.available and se.applied_factor > 1.0 and se.ratio is not None:
        a.drivers.append(
            f"Seasonal pattern: in the same period last year demand ran {_n((se.ratio - 1) * 100, 0)}% above its normal level "
            f"({_n(se.last_year_forward_rate, 2)} vs {_n(se.last_year_base_rate, 2)}/day); forecast raised by x{_n(se.applied_factor, 2)}."
        )
    if not a.drivers:
        a.drivers.append("No unusual trend or seasonality detected - stock is simply being used up at the normal pace.")


def _suggest_quantity(a: Analysis, policy: Policy, windows: Sequence[Window] = (), today: Optional[date] = None) -> None:
    r = a.forecast_rate or (a.usage.planning_rate if a.usage else 0.0)
    if r <= EPS:
        a.suggested_qty_basis = "No recent usage to size an order from; quantity left to the buyer."
        return
    today = today or date.fromisoformat(a.as_of)
    lead_plus_safety = a.lead_time_days + policy.safety_stock_days
    horizon = lead_plus_safety + policy.target_cover_days
    # Aim for lead + safety + target days of demand, and never less than the minimum quantity plus a lead-time buffer.
    # With calendar events, "days of demand" is the expected demand over those days, event uplifts included.
    demand = total_demand(r, windows, today, horizon)
    target_position = max(demand, (policy.min_qty or 0.0) + total_demand(r, windows, today, lead_plus_safety))
    qty = _ceil_multiple(max(target_position - a.position, 0.0), policy.order_multiple)
    if qty > 0:
        if policy.min_order_qty:
            qty = max(qty, policy.min_order_qty)
        a.suggested_qty, a.suggestion_type = qty, "replenish"
        if windows and abs(demand - r * horizon) >= 0.5:
            sized = (f"expected demand of {_n(demand)} {a.uom} over the next {_n(horizon)} days ({_n(a.lead_time_days)} lead + "
                     f"{_n(policy.safety_stock_days)} safety + {_n(policy.target_cover_days)} target days), including your calendar: {describe(windows)}")
        else:
            sized = f"{_n(r, 2)} {a.uom}/day x ({_n(a.lead_time_days)} lead + {_n(policy.safety_stock_days)} safety + {_n(policy.target_cover_days)} target days)"
        a.suggested_qty_basis = (
            sized + f" - {_n(a.position)} {a.uom} already held/on order"
            + (f", rounded up to packs of {_n(policy.order_multiple)}" if policy.order_multiple != 1 else "")
        )
    elif a.gap_days is not None and a.gap_days > 0:
        bridge = _ceil_multiple(r * (a.gap_days + policy.safety_stock_days), policy.order_multiple)
        a.suggested_qty, a.suggestion_type = bridge, "bridge"
        a.suggested_qty_basis = (
            f"The open order is large enough but lands ~{_n(a.gap_days)} days after you run out. Either ask the supplier to expedite it, "
            f"or buy a small bridge quantity: {_n(r, 2)} {a.uom}/day x ({_n(a.gap_days)} gap + {_n(policy.safety_stock_days)} safety days)"
        )
    else:
        a.suggested_qty_basis = "Stock plus open orders already cover the target level; no extra quantity suggested."


def _confidence(a: Analysis, stats: UsageStats, policy: Policy) -> None:
    score, why = 2, []
    if stats.data_days >= 90:
        why.append(f"{stats.data_days} days of history")
    elif stats.data_days >= policy.min_history_days:
        score -= 1
        why.append(f"only {stats.data_days} days of history")
    else:
        score -= 2
        why.append(f"very little history ({stats.data_days} days)")
    if stats.weekly_cv is not None:
        if stats.weekly_cv > 0.6:
            score -= 1
            why.append(f"demand is lumpy (week-to-week variation {_n(stats.weekly_cv * 100, 0)}%)")
        else:
            why.append(f"demand is fairly steady (week-to-week variation {_n(stats.weekly_cv * 100, 0)}%)")
    if stats.nonzero_days < 8 and stats.planning_rate > EPS:
        score -= 1
        why.append(f"usage seen on only {stats.nonzero_days} days")
    a.confidence = "High" if score >= 2 else "Medium" if score == 1 else "Low"
    a.confidence_why = why


def forecast_reason(f: dict[str, Any], u: str) -> str:
    """Why the forecast says it is time to order, with its numbers."""
    parts = []
    if f["seasonal_index"] > 1.0 and f.get("last_year_window_rate") is not None:
        parts.append(f"about {_n(f['expected_use'])} {u} of use: {_n(f['planned_rate'], 2)} {u}/day raised x{_n(f['seasonal_index'], 2)} because the same days last year "
                     f"ran at {_n(f['last_year_window_rate'], 2)}/day against a normal {_n(f['last_year_normal_rate'], 2)}/day")
    if f["sales_due"] > 0 and not f["sales_already_reserved"]:
        parts.append(f"{_n(f['sales_due'])} {u} still to deliver on open sales orders due by {_d(f['sales_last_due'])}" if f.get("sales_last_due") else
                     f"{_n(f['sales_due'])} {u} still to deliver on open sales orders")
    need = " and ".join(parts) or f"about {_n(f['reorder_point'])} {u}"
    return (f"The forecast for the next {_n(f['window_days'])} days (lead time + safety days) is {need}, so you need about {_n(f['reorder_point'])} {u}; "
            f"the stock position is {_n(f['position'])} {u} - it is time to order.")


# ===================================================================== formatting helpers
def _n(v: Optional[float], digits: int = 1) -> str:
    if v is None:
        return "n/a"
    if abs(v - round(v)) < 10 ** (-digits - 1):
        return f"{int(round(v)):,}"
    return f"{v:,.{digits}f}"


def _iso(day: Optional[date]) -> Optional[str]:
    return day.isoformat() if day else None


def _d(iso_date: Optional[str]) -> str:
    return date.fromisoformat(iso_date).strftime("%d %b") if iso_date else "n/a"


def where_stock_is(a: Analysis) -> list[str]:
    """Where the stock sits, one phrase per warehouse that holds some, largest first: "Stores - TTC 120 pcs", "Goods In Transit - TTC 30 pcs (in transit)".
    Empty when the ERP did not say, or nothing is held. The quantities are on-hand; what is promised to customers is added when there is some."""
    out = []
    for w in a.warehouses:
        if not (w["on_hand"] or w["reserved"]):
            continue
        extra = (["in transit"] if w["in_transit"] else []) + ([f"{_n(w['reserved'])} promised to customers"] if w["reserved"] > 0 else [])
        out.append(f"{w['warehouse']} {_n(w['on_hand'])} {a.uom}" + (f" ({', '.join(extra)})" if extra else ""))
    return out

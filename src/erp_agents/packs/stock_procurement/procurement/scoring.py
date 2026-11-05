"""Vendor scoring: plain maths over what the ERP knows, so every sentence in the approval message is checkable.

Each eligible vendor gets four 0-100 factor scores, combined with the owner's weights:

  cost         100 x cheapest total / this vendor's total
  delivery     100 x fastest lead time / this vendor's lead time; capped at 40 when it would arrive after
               the owner runs out of stock
  reliability  on-time share x 100 (50, and flagged, when the ERP has no receipts to judge by)
  terms        100 x credit days / longest credit days among the vendors (neutral 50 when nobody gives credit)

A listed supplier gets +3 points when `prefer_listed_suppliers` is on. Missing data is never silently
filled in: it becomes a neutral score AND a note the owner reads.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..settings import ProcurementSettings
from ai_munimji_engine.packs.stock_procurement.erp import TaxSetup, VendorOffer
from . import taxes

LISTED_BONUS = 3.0
LATE_DELIVERY_CAP = 40.0
NEUTRAL = 50.0

FACTORS = ("cost", "delivery", "reliability", "terms")


@dataclass
class Need:
    """What has to be bought and how urgently."""
    qty: float
    cover_days: Optional[float]            # days until stock-out at the current usage (None = no usage data)
    fallback_lead_days: float              # used for a vendor with no delivery history
    uom: str = "units"


@dataclass
class ScoredVendor:
    supplier: str
    name: str
    eligible: bool
    excluded_reason: str = ""
    unit_price: Optional[float] = None
    total: Optional[float] = None            # LANDED cost used to compare vendors: goods + the duties and taxes that count as cost
    goods_total: Optional[float] = None      # price x quantity: what the purchase order says
    tax_total: float = 0.0                   # duties and taxes counted in `total`
    tax_not_cost: float = 0.0                # taxes the ERP books as payable only (e.g. creditable): shown, left out of `total`
    tax_lines: list[dict[str, Any]] = field(default_factory=list)
    tax_template: Optional[str] = None       # the ERP purchase taxes template used (also put on the draft purchase order)
    tax_source: str = ""                     # "tax rule" | "default template" | "none"
    country: Optional[str] = None            # the vendor's country, for information
    lead_days: Optional[float] = None
    lead_assumed: bool = False
    lead_from_quote: bool = False            # no delivery history: the delivery time is the one they quoted
    price_source: str = ""                   # "quotation" | "last order" | "average of past orders": where unit_price comes from
    quote_ref: Optional[str] = None          # the supplier quotation the price comes from
    quote_valid_till: Optional[str] = None
    quote_qty: Optional[float] = None
    on_time_rate: Optional[float] = None
    payment_days: Optional[float] = None
    orders: int = 0
    listed: bool = False
    arrives_after_stockout: bool = False
    scores: dict[str, float] = field(default_factory=dict)
    points: dict[str, float] = field(default_factory=dict)        # weighted contribution of each factor
    score: float = 0.0
    flags: list[str] = field(default_factory=list)
    why_not: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Ranking:
    vendors: list[ScoredVendor]            # eligible first (best first), then excluded
    weights: dict[str, float]              # normalised to sum to 100
    notes: list[str] = field(default_factory=list)

    @property
    def best(self) -> Optional[ScoredVendor]:
        return self.vendors[0] if self.vendors and self.vendors[0].eligible else None

    @property
    def eligible(self) -> list[ScoredVendor]:
        return [v for v in self.vendors if v.eligible]

    def to_dict(self) -> dict[str, Any]:
        return {"vendors": [v.to_dict() for v in self.vendors], "weights": self.weights, "notes": self.notes}


def normalised_weights(s: ProcurementSettings) -> dict[str, float]:
    raw = {"cost": s.weight_cost, "delivery": s.weight_delivery, "reliability": s.weight_reliability, "terms": s.weight_terms}
    total = sum(raw.values())
    return {k: v * 100.0 / total for k, v in raw.items()}


def rank_vendors(offers: list[VendorOffer], need: Need, settings: ProcurementSettings,
                 tax_setups: Optional[dict[str, TaxSetup]] = None) -> Ranking:
    """`tax_setups` = for each supplier, the duties and taxes the ERP itself applies to this purchase (None = not asked)."""
    weights = normalised_weights(settings)
    notes: list[str] = []
    vendors: list[ScoredVendor] = []

    mode = settings.use_quotations
    for o in offers:
        quoted = o.quote_price if mode != "off" and o.quote_price else None        # a valid supplier quotation, when the owner wants those read
        price = quoted or o.unit_price or o.avg_price
        source = "quotation" if quoted else "last order" if o.unit_price else "average of past orders" if o.avg_price else ""
        goods = round(price * need.qty, 2) if price else None
        v = ScoredVendor(
            supplier=o.supplier, name=o.name, eligible=True, unit_price=price, total=goods, goods_total=goods,
            on_time_rate=o.on_time_rate, payment_days=o.payment_days, orders=o.orders, listed=o.listed, country=o.country,
            price_source=source, quote_ref=o.quote_ref if quoted else None, quote_valid_till=o.quote_valid_till.isoformat() if quoted and o.quote_valid_till else None,
            quote_qty=o.quote_qty if quoted else None,
        )
        if not price:
            v.eligible, v.excluded_reason = False, "no price on record: the ERP has no past order for this item from them" + (" and no valid supplier quotation" if mode != "off" else "")
        elif mode == "only" and not quoted:
            v.eligible, v.excluded_reason = False, "no valid supplier quotation for this item (your settings compare only vendors that have quoted)"
        elif settings.exclude_new_vendors and o.orders == 0:
            v.eligible, v.excluded_reason = False, "no past orders for this item (your settings do not recommend new vendors)"
        elif settings.include_taxes and tax_setups is not None:
            setup = tax_setups.get(o.supplier)
            if setup is None or not setup.rows:
                why = "; ".join(setup.notes) if setup and setup.notes else "the ERP has no Tax Rule or default purchase taxes template that applies to this vendor"
                v.tax_source = setup.source if setup else "none"
                v.flags.append(f"duties and taxes may be missing: {why}")
                if settings.no_tax_setup == "exclude":
                    v.eligible, v.excluded_reason = False, f"the ERP has no purchase tax setup for this vendor, so duties cannot be checked ({why}), and your settings exclude such vendors"
            else:
                res = taxes.compute(setup, price, need.qty, settings.include_recoverable_taxes_in_cost)
                v.tax_lines, v.tax_total, v.tax_not_cost = [l.to_dict() for l in res.lines], res.counted, res.not_cost
                v.tax_template, v.tax_source = setup.template, setup.source
                v.total = round(goods + res.counted, 2)
                v.flags += [f"duties and taxes may be wrong: {p}" for p in res.problems] + [f"duties and taxes: {n}" for n in setup.notes]
        if v.eligible:
            if o.lead_time_days is not None:
                v.lead_days = o.lead_time_days
            elif quoted and o.quote_lead_days:
                v.lead_days, v.lead_from_quote = o.quote_lead_days, True
                v.flags.append(f"no delivery history: using the {o.quote_lead_days:g} days they quoted")
            else:
                v.lead_days, v.lead_assumed = need.fallback_lead_days, True
                v.flags.append(f"no delivery history: assumed {need.fallback_lead_days:g} days")
            if quoted and o.quote_qty and o.quote_qty < need.qty:
                v.flags.append(f"their quotation is for {o.quote_qty:g} {need.uom} and you need {need.qty:g}: confirm the price holds for the larger quantity")
            if o.on_time_rate is None:
                v.flags.append("no receipts on record to judge on-time delivery")
            v.arrives_after_stockout = need.cover_days is not None and v.lead_days > need.cover_days
            if v.arrives_after_stockout:
                v.flags.append(f"would arrive about {v.lead_days - need.cover_days:.1f} days after you run out")
            if (settings.flag_price_jump_pct and o.avg_price and price
                    and price > o.avg_price * (1 + settings.flag_price_jump_pct / 100)):
                v.flags.append(f"{'their quotation' if quoted else 'latest price'} is {(price / o.avg_price - 1) * 100:.0f}% above their average of {o.avg_price:g}")
        vendors.append(v)

    elig = [v for v in vendors if v.eligible]
    if elig:
        min_total = min(v.total for v in elig)                     # type: ignore[type-var]
        min_lead = min(v.lead_days for v in elig)                  # type: ignore[type-var]
        max_credit = max((v.payment_days or 0.0) for v in elig)
        for v in elig:
            cost = 100.0 * min_total / v.total if v.total else 0.0
            delivery = 100.0 * (min_lead / v.lead_days) if v.lead_days and v.lead_days > 0 else 100.0
            if v.arrives_after_stockout:
                delivery = min(delivery, LATE_DELIVERY_CAP)
            reliability = v.on_time_rate * 100.0 if v.on_time_rate is not None else NEUTRAL
            terms = 100.0 * (v.payment_days or 0.0) / max_credit if max_credit > 0 else NEUTRAL
            v.scores = {"cost": cost, "delivery": delivery, "reliability": reliability, "terms": terms}
            v.points = {k: v.scores[k] * weights[k] / 100.0 for k in FACTORS}
            v.score = sum(v.points.values())
            if settings.prefer_listed_suppliers and v.listed:
                v.score += LISTED_BONUS
                v.points["listed"] = LISTED_BONUS
        elig.sort(key=lambda v: (-v.score, v.total or 0, v.lead_days or 0, v.supplier))
        if mode != "off" and not any(v.price_source == "quotation" for v in elig):
            notes.append("Prices from supplier quotations are switched on, but none of these vendors has a valid quotation for this item: the prices are from past orders.")
        if len(elig) < settings.min_vendors:
            notes.append(f"Only {len(elig)} vendor{'s' if len(elig) != 1 else ''} had enough history to compare; you asked for at least {settings.min_vendors}.")
        if all(v.arrives_after_stockout for v in elig):
            notes.append("Every vendor would deliver after you run out of stock; consider asking the best one to expedite.")
        if settings.include_taxes and tax_setups is not None and not any(v.tax_lines for v in elig):
            notes.append("Duties and taxes are not in this comparison: the ERP has no Tax Rule or default purchase taxes template that applies "
                         "to these vendors. Set them up in the ERP (Tax Rule, Purchase Taxes and Charges Template) and the next comparison will use them.")
        best = elig[0]
        for v in elig[1:]:
            v.why_not = _why_not(best, v, need.uom)

    excluded = sorted((v for v in vendors if not v.eligible), key=lambda v: v.name)
    for v in excluded:
        v.why_not = v.excluded_reason
    return Ranking(vendors=elig + excluded, weights=weights, notes=notes)


# ---------------------------------------------------------------------------------------------- explanations
def _money(x: float) -> str:
    return f"{x:,.2f}"


def _why_not(best: ScoredVendor, v: ScoredVendor, uom: str) -> str:
    """The one or two factors that cost this vendor the most points against the winner, in plain words."""
    reasons: list[str] = []
    if v.arrives_after_stockout:
        reasons.append(f"arrives too late ({v.lead_days:g} days, after you run out)")
    gaps = sorted(((best.points[k] - v.points[k], k) for k in FACTORS), reverse=True)
    for gap, k in gaps:
        if len(reasons) >= 2:
            break
        if gap < 0.5:
            break
        if k == "cost" and v.total and best.total:
            diff = v.total - best.total
            if (v.tax_total or best.tax_total) and diff > 0:
                cheaper = (f"its unit price is lower ({_money(v.unit_price)} vs {_money(best.unit_price)}), but " if v.unit_price and best.unit_price
                           and v.unit_price < best.unit_price else "")
                reasons.append(f"{cheaper}costs {_money(diff)} more in total once duties and taxes are added "
                               f"({_money(v.total)} = {_money(v.goods_total)} goods + {_money(v.tax_total)} duties and taxes, vs {_money(best.total)})")
            else:
                reasons.append(f"costs {_money(diff)} more in total ({_money(v.total)} vs {_money(best.total)})" if diff > 0
                               else f"is cheaper ({_money(v.total)} vs {_money(best.total)}) but loses on other factors")
        elif k == "delivery" and not v.arrives_after_stockout and v.lead_days and best.lead_days:
            reasons.append(f"delivers in {v.lead_days:g} days vs {best.lead_days:g}")
        elif k == "reliability":
            mine = f"{v.on_time_rate * 100:.0f}%" if v.on_time_rate is not None else "unknown"
            reasons.append(f"on-time record {mine} vs {best.on_time_rate * 100:.0f}%" if best.on_time_rate is not None
                           else f"on-time record {mine}")
        elif k == "terms":
            reasons.append(f"credit {v.payment_days or 0:g} days vs {best.payment_days or 0:g}")
    if not reasons:
        reasons.append("scored within a point of the winner, which came out ahead on balance")
    return "; ".join(reasons).capitalize() + f" (score {v.score:.0f} vs {best.score:.0f})."

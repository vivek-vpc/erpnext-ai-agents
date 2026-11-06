"""Landed cost from the ERP's own purchase tax setup.

The ERP decides WHICH purchase taxes template applies to a purchase (ERPNext: Tax Rule -> template, else the default
template) and WHAT it contains (rows: charge type, rate, category). This module only does the arithmetic, the same way
ERPNext does, on rows the adapter read:

  charge type              amount
  On Net Total             goods value x rate
  On Previous Row Amount   that row's amount x rate
  On Previous Row Total    the running total up to that row x rate   (goods + every charge so far)
  On Item Quantity         quantity x rate
  Actual                   the amount given

  Add / Deduct             sign of the amount
  category Valuation, or Valuation and Total    part of what the goods really cost you (duty, freight, non-creditable tax)
  category Total                                payable, but not item cost (e.g. creditable input tax): shown, not counted

Nothing is assumed. A row type the ERP has that is not handled here, or a row that points at a missing row, is reported.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ai_munimji_engine.packs.stock_procurement.erp import TaxRow, TaxSetup

COST_CATEGORIES = ("Valuation", "Valuation and Total")


def setup_to_dict(setup: TaxSetup) -> dict:
    """The tax figures as kept with a proposal (JSON-safe), so that a later recalculation uses the figures the request was first made with."""
    return asdict(setup)


def setup_from_dict(data: dict) -> TaxSetup:
    return TaxSetup(template=data.get("template"), source=data.get("source") or "none", rows=tuple(TaxRow(**r) for r in data.get("rows") or ()),
                    notes=tuple(data.get("notes") or ()), tax_category=data.get("tax_category"), item_template=data.get("item_template"),
                    unavailable=bool(data.get("unavailable")))


@dataclass
class ChargeLine:
    template: str
    name: str
    description: str          # "10% of goods", "18% of the running total up to row 1", "5.00 per unit"
    amount: float             # signed: a Deduct row is negative
    category: str
    in_cost: bool             # part of the item cost (counted), or payable only (e.g. creditable tax)
    account: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TaxResult:
    lines: list[ChargeLine]
    counted: float                       # charges that count as cost
    not_cost: float                      # charges left out of the cost (category "Total"), unless the owner counts them
    problems: list[str] = field(default_factory=list)


def compute(setup: TaxSetup, unit_price: float, qty: float, count_non_cost: bool) -> TaxResult:
    goods = unit_price * qty
    amounts: list[float] = []
    totals: list[float] = []
    lines: list[ChargeLine] = []
    problems: list[str] = []
    template = setup.template or ""

    for n, row in enumerate(setup.rows, start=1):
        amount, desc = 0.0, row.charge_type
        ct = row.charge_type
        if ct == "On Net Total":
            amount, desc = goods * row.rate / 100.0, f"{row.rate:g}% of goods"
        elif ct in ("On Previous Row Amount", "On Previous Row Total"):
            ref = row.row_id
            if not ref or not (1 <= ref < n):
                problems.append(f"row {n} ({row.description}) refers to row {ref}, which does not come before it")
                desc = f"{row.rate:g}% of an unknown row"
            elif ct == "On Previous Row Amount":
                amount, desc = amounts[ref - 1] * row.rate / 100.0, f"{row.rate:g}% of the amount of row {ref}"
            else:
                amount, desc = totals[ref - 1] * row.rate / 100.0, f"{row.rate:g}% of the running total up to row {ref} (goods plus every charge so far)"
        elif ct == "On Item Quantity":
            amount, desc = qty * row.rate, f"{row.rate:,.2f} per unit"
        elif ct == "Actual":
            amount, desc = row.amount, f"fixed {row.amount:,.2f}"
        else:
            problems.append(f"row {n} ({row.description}) has charge type {ct!r}, which this agent cannot calculate")
        if row.rate_from_item_template:
            desc += " (rate from the item's tax template)"
        signed = -amount if row.add_deduct == "Deduct" else amount
        amounts.append(signed)
        totals.append((totals[-1] if totals else goods) + signed)
        lines.append(ChargeLine(template, row.description or row.account or f"row {n}", desc + (" (deducted)" if row.add_deduct == "Deduct" else ""),
                                round(signed, 2), row.category, row.category in COST_CATEGORIES, row.account))

    counted = round(sum(l.amount for l in lines if l.in_cost or count_non_cost), 2)
    not_cost = 0.0 if count_non_cost else round(sum(l.amount for l in lines if not l.in_cost), 2)
    return TaxResult(lines, counted, not_cost, problems)

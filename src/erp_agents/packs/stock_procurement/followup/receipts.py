"""Goods receipt check: when goods arrive, compare quantity and price with the purchase order, and flag what does not match before the invoice is paid.

Each submitted goods receipt is checked once (`receipt_checks` remembers it). A line is flagged when:
  * more was accepted than was still to come on the order line (beyond `qty_tolerance_pct`); rejected goods do not count here, they are the next point;
  * goods were rejected (a quality problem);
  * the price on the receipt, or on the invoice against it, is `price_tolerance_pct` or more away from the order's price;
  * it was received without a purchase order to compare against.
A partial delivery is not a mismatch (the rest is still to come; late deliveries are chased separately). The person is told what differs and whether an invoice is
already waiting to be paid. Nothing is changed in the ERP.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any, Optional

from ai_munimji_engine.core.approvals.models import ApprovalRecord, Decision
from ai_munimji_engine.core.approvals.service import KindHandler
from ai_munimji_engine.core.common import iso
from ai_munimji_engine.storage import dumps, loads
from ai_munimji_engine.packs.stock_procurement.inventory.analysis import _n
from .messages import KIND_RECEIPT, build_receipt_request

log = logging.getLogger(__name__)
EPS = 1e-9
PAID = ("Paid",)
UNPAID = ("Draft", "Unpaid", "Partly Paid", "Overdue", "Submitted", "Unpaid and Discounted", "Overdue and Discounted", "Partly Paid and Discounted")


def line_problems(l: Any, price_tol_pct: float, qty_tol_pct: float) -> list[dict[str, Any]]:
    """What does not match on one receipt line, each with a sentence that carries its numbers. Pure."""
    out: list[dict[str, Any]] = []
    u, item = l.uom, f"{l.item_name} ({l.item_code})"
    if not l.purchase_order:
        out.append({"kind": "no order", "item_code": l.item_code, "text": f"{item}: {_n(l.received_qty)} {u} received without a purchase order to compare against."})
    elif l.po_qty is not None:
        still = max(l.po_qty - l.po_received_before, 0.0)
        extra = l.accepted_qty - still                  # what was KEPT: goods sent back as rejected are a quality problem (below), not more than ordered
        if extra > EPS and extra > still * qty_tol_pct / 100:
            out.append({"kind": "quantity", "item_code": l.item_code, "extra": extra,
                        "text": f"{item}: {_n(l.accepted_qty)} {u} accepted, but only {_n(still)} {u} were still to come on {l.purchase_order} ({_n(extra)} {u} more than ordered)."})
    if l.rejected_qty > EPS:
        share = l.rejected_qty / l.received_qty * 100 if l.received_qty else 100.0
        out.append({"kind": "quality", "item_code": l.item_code, "rejected": l.rejected_qty,
                    "text": f"{item}: {_n(l.rejected_qty)} {u} of {_n(l.received_qty)} rejected ({_n(share, 0)}%) - make sure you are not invoiced for them."})
    if l.po_rate:
        for what, rate in (("receipt", l.rate), ("invoice", l.invoice_rate)):
            if rate is None:
                continue
            pct = (rate - l.po_rate) / l.po_rate * 100
            if abs(pct) >= price_tol_pct and abs(rate - l.po_rate) > EPS:
                ref = f" ({l.invoice})" if what == "invoice" and l.invoice else ""
                out.append({"kind": "price", "item_code": l.item_code, "pct": pct,
                            "text": f"{item}: price on the {what}{ref} is {rate:,.2f}, the order says {l.po_rate:,.2f} ({pct:+.1f}%)."
                            + (f" On {_n(l.accepted_qty)} {u} that is {abs(rate - l.po_rate) * l.accepted_qty:,.2f} {'more' if pct > 0 else 'less'}." if l.accepted_qty else "")})
    return out


def payment_note(lines: list[Any]) -> str:
    invoices = {(l.invoice, l.invoice_status) for l in lines if l.invoice}
    if not invoices:
        return "No purchase invoice has been entered against it yet: check these differences before the vendor's invoice is booked and paid."
    paid = [i for i, s in invoices if s in PAID]
    waiting = [f"{i} ({s.lower() if s else 'status unknown'})" for i, s in invoices if s not in PAID]
    parts = []
    if waiting:
        parts.append(f"Invoice {', '.join(waiting)} is not paid yet: hold the payment until this is sorted out with the vendor.")
    if paid:
        parts.append(f"Invoice {', '.join(paid)} is already paid: ask the vendor for a credit note for any difference.")
    return " ".join(parts)


class ReceiptChecker:
    def __init__(self, app: Any) -> None:
        self.app = app
        app.approvals.register_handler(KIND_RECEIPT, KindHandler(on_decision=self._on_decision))

    @property
    def cfg(self) -> Any:
        return self.app.settings.followup()

    def _lines(self, days: int) -> Optional[list[Any]]:
        read = getattr(self.app.erp, "get_receipt_lines", None)
        if read is None:
            return None
        return read(self.app.inventory.today() - timedelta(days=days))

    def mismatches(self, days: Optional[int] = None, include_drafts: bool = True) -> dict[str, Any]:
        """Every receipt in the period that does not match its order, checked now (also drafts, so a receipt can be fixed before it is submitted)."""
        cfg = self.cfg
        lines = self._lines(days or cfg.receipt_lookback_days)
        if lines is None:
            return {"available": False, "note": "this ERP connection cannot read goods receipts"}
        by_receipt: dict[str, list[Any]] = defaultdict(list)
        for l in lines:
            if l.submitted or include_drafts:
                by_receipt[l.receipt].append(l)
        out = []
        for receipt, ls in by_receipt.items():
            problems = [p for l in ls for p in line_problems(l, cfg.price_tolerance_pct, cfg.qty_tolerance_pct)]
            if problems:
                row = self.app.stock_stores.receipts.get(receipt)
                out.append({"receipt": receipt, "vendor": ls[0].supplier_name, "posted": ls[0].posting_date.isoformat() if ls[0].posting_date else None,
                            "draft": not ls[0].submitted, "problems": [{k: p[k] for k in ("kind", "item_code", "text")} for p in problems], "before_you_pay": payment_note(ls),
                            "checked": row["status"] if row else "not yet"})
        return {"available": True, "days": days or cfg.receipt_lookback_days, "receipts_checked": len(by_receipt), "with_differences": len(out), "receipts": out}

    def run(self, dry_run: bool = False) -> dict[str, Any]:
        cfg = self.cfg
        report: dict[str, Any] = {"checked": 0, "flagged": 0, "clean": 0, "actions": []}
        if not cfg.receipt_check_enabled:
            return {**report, "note": "the goods receipt check is switched off"}
        lines = self._lines(cfg.receipt_lookback_days)
        if lines is None:
            return {**report, "note": "this ERP connection cannot read goods receipts"}
        by_receipt: dict[str, list[Any]] = defaultdict(list)
        for l in lines:
            if l.submitted:
                by_receipt[l.receipt].append(l)
        for receipt, ls in sorted(by_receipt.items()):
            if self.app.stock_stores.receipts.get(receipt):
                continue
            report["checked"] += 1
            problems = [p for l in ls for p in line_problems(l, cfg.price_tolerance_pct, cfg.qty_tolerance_pct)]
            if dry_run:
                report["actions"].append({"receipt": receipt, "action": "would_flag" if problems else "clean"})
                continue
            now = iso(self.app.clock())
            if not problems:
                self.app.stock_stores.receipts.add(receipt, ls[0].supplier, "clean", "[]", None, now)
                report["clean"] += 1
                continue
            req = build_receipt_request(receipt, ls[0].supplier_name, ls[0].posting_date, problems, payment_note(ls))
            with self.app.stores.transaction():
                record = self.app.approvals.submit(req, deliver=False)
                self.app.stock_stores.receipts.add(receipt, ls[0].supplier, "flagged", dumps(problems), record.id, now)
            self.app.approvals.deliver(record)
            report["flagged"] += 1
            report["actions"].append({"receipt": receipt, "action": "flagged", "approval_id": record.id, "problems": [p["kind"] for p in problems]})
        return report

    def recorded_problems(self, since_iso: str) -> dict[str, list[dict[str, Any]]]:
        """supplier -> the problems recorded on its receipts since then (for the scorecard)."""
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in self.app.stock_stores.receipts.problems_since(since_iso):
            out[r["supplier"]] += loads(r["problems_json"]) or []
        return dict(out)

    def _on_decision(self, record: ApprovalRecord, decision: Decision) -> None:
        self.app.stock_stores.receipts.acknowledge(record.id)

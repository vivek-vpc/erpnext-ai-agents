"""The vendor selector (procurement) agent.

One run = take every approved reorder (REORDER_NEEDED work order) -> look up the item's vendors and their
history in the ERP -> score them with the owner's weights -> ask the owner to approve ONE purchase order,
with the reasoning, including why each other vendor was not chosen.

Approving creates a DRAFT purchase order in the ERP (never submitted) when `po_mode` is "draft". If the
ERP refuses, the decision is rolled back and the owner is told, so nothing is half done.
When no recommendation is possible (no priced vendor, or no quantity) the owner gets a plain message
saying exactly what is missing instead of a guess.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from erp_agents.core.approvals.models import ApprovalRecord, Decision, DecisionRefused, DecisionResult
from erp_agents.core.approvals.service import ApprovalService, KindHandler, parse_approver
from erp_agents.core import acting
from erp_agents.core.common import Actor, Clock, iso, short_id, utcnow
from erp_agents.core.settings_store import SettingsStore
from erp_agents.core.storage import Database, dumps
from erp_agents.core.workorders import REORDER_NEEDED, WorkOrderQueue
from erp_agents.core.erp.base import ERPClient, PurchaseOrderDraft, PurchaseOrderResult, QuoteRule, RequisitionDraft, TaxSetup
from .messages import (
    KIND_MANUAL, KIND_ORDER, KIND_PO, KIND_PR, build_manual_request, build_order_request, build_po_request, expected_arrival, requisition_note, route_for, vendor_note_for,
)
from .proposals import Proposal, ProposalRepo
from ..inventory.analysis import _n
from .scoring import Need, Ranking, ScoredVendor, rank_vendors
from ..schema import ensure_schema

log = logging.getLogger(__name__)
AGENT_NAME = "procurement"


@dataclass
class ProcurementReport:
    run_id: str
    dry_run: bool
    started_at: str
    finished_at: str = ""
    considered: int = 0
    proposed: int = 0
    manual: int = 0
    skipped: int = 0
    resumed: int = 0
    errors: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _Vendor:
    """The recommended vendor's stored figures, read back as attributes for the requisition note."""

    def __init__(self, d: dict[str, Any]) -> None:
        self.name, self.country = d.get("name", "the recommended vendor"), d.get("country")
        self.unit_price, self.total, self.lead_days, self.on_time_rate = d.get("unit_price"), d.get("total"), d.get("lead_days"), d.get("on_time_rate")
        self.tax_lines = d.get("tax_lines") or []
        self.price_source, self.quote_ref, self.quote_valid_till = d.get("price_source"), d.get("quote_ref"), d.get("quote_valid_till")


class _Need:
    def __init__(self, qty: Any, uom: str) -> None:
        self.qty, self.uom = float(qty or 0), uom


class ProcurementAgent:
    def __init__(self, erp: ERPClient, settings: SettingsStore, proposals: ProposalRepo, approvals: ApprovalService,
                 workorders: WorkOrderQueue, db: Database, clock: Clock = utcnow) -> None:
        self.erp, self.settings, self.proposals = erp, settings, proposals
        ensure_schema(db)
        self.approvals, self.workorders, self.db, self.clock = approvals, workorders, db, clock
        handler = KindHandler(on_decision=self._on_decision, on_expired=self._on_expired, on_cancelled=self._on_cancelled, on_action=self._on_action,
                              outcome_detail=self._outcome_detail, reconcile=self._reconcile)
        approvals.register_handler(KIND_PO, handler)
        approvals.register_handler(KIND_PR, handler)
        approvals.register_handler(KIND_MANUAL, handler)
        approvals.register_handler(KIND_ORDER, KindHandler(on_decision=self._on_order_decision, on_action=self._on_order_action))
        approvals.on_decided.append(self._after_decided)
        self.on_order_removed: Optional[Callable[[str], None]] = None          # (item_code): an order was cancelled, so the item is no longer "ordered recently"; set by bootstrap

    def today(self) -> date:
        return self.clock().astimezone(ZoneInfo(self.settings.system().timezone)).date()

    def _cfg_for(self, payload: dict[str, Any]) -> Any:
        """The procurement settings for one work order. An order that comes from a quote round (`use_quotes`) is compared on the prices the vendors quoted, even when the
        owner does not read quotations otherwise."""
        cfg = self.settings.procurement()
        if payload.get("use_quotes") and cfg.use_quotations == "off":
            cfg = cfg.model_copy(update={"use_quotations": "prefer"})
        return cfg

    def _offers(self, code: str, cfg: Any) -> list[Any]:
        """The item's vendors and what the ERP knows about them: past orders, and (when the owner chose to) their valid supplier quotations."""
        since = self.today() - timedelta(days=cfg.history_days)
        if cfg.use_quotations == "off":
            return self.erp.get_vendor_offers(code, since)            # the original call: an ERP client written before quotations existed keeps working
        return self.erp.get_vendor_offers(code, since, QuoteRule(self.today(), cfg.quotation_max_age_days))

    # ================================================================== main run
    def run(self, dry_run: bool = False) -> ProcurementReport:
        run_id, now = short_id(8), self.clock()
        report = ProcurementReport(run_id=run_id, dry_run=dry_run, started_at=iso(now))

        if not dry_run:                                     # snoozed requests whose time has come are looked at again
            for p in self.proposals.snoozed_due():
                self.proposals.update_status(p.id, "superseded", "snooze ended; looked at again")
                self.workorders.set_status(p.work_order_id, "pending")
                report.resumed += 1

        for wo in reversed(self.workorders.list(status="pending", type_=REORDER_NEEDED, limit=200)):     # oldest first
            report.considered += 1
            code = wo["payload"].get("item_code", "?")
            try:
                action = self._handle(wo, dry_run)
            except Exception as exc:  # noqa: BLE001 - one bad item must not stop the run
                log.exception("procurement failed for %s", code)
                report.errors.append(f"{code}: {exc}")
                continue
            report.actions.append(action)
            if action["action"] in ("proposed", "would_propose"):
                report.proposed += 1
            elif action["action"] in ("manual", "would_ask_manual"):
                report.manual += 1
            else:
                report.skipped += 1

        report.finished_at = iso(self.clock())
        self.db.execute(
            "INSERT INTO agent_runs(id, agent, started_at, finished_at, dry_run, summary_json) VALUES (?,?,?,?,?,?)",
            (run_id, AGENT_NAME, report.started_at, report.finished_at, int(dry_run), dumps(report.to_dict())),
        )
        return report

    # ================================================================== one work order
    def _handle(self, wo: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        p = wo["payload"]
        code = p["item_code"]
        base = {"item_code": code, "work_order_id": wo["id"]}
        if self.proposals.open_for_item(code):
            return {**base, "action": "skipped:awaiting_answer"}

        cfg = self._cfg_for(p)
        qty = p.get("suggested_qty") or cfg.fallback_qty
        moq = self._moq(code)
        if qty and moq and float(qty) < moq:                 # ERPNext refuses an order below the item's minimum order quantity
            qty = moq
        offers = self._offers(code, cfg)

        if not qty:
            problem = ("No order quantity could be worked out because there is no recent usage to size it from. "
                       "Please decide the quantity and the vendor in your ERP (or set a default order quantity in the procurement settings).")
            return self._manual(wo, base, problem, None, dry_run)

        need = Need(qty=float(qty), cover_days=p.get("cover_days"),
                    fallback_lead_days=float(p.get("lead_time_days") or 7), uom=p.get("uom", "units"))
        ranking = rank_vendors(offers, need, cfg, self._tax_setups(offers, code, cfg))
        best = ranking.best
        chosen = next((v for v in ranking.eligible if v.supplier == p.get("prefer_supplier")), None) if p.get("prefer_supplier") else None
        if chosen is not None:                                # the vendor a person picked (from a quote comparison) comes first; Change vendor still works
            best = chosen
        if best is None:
            problem = ("Your settings compare only vendors that have a valid supplier quotation for this item, and none of them has one. "
                       "Ask your suppliers to quote (a Request for Quotation in your ERP), or change 'Prices from supplier quotations' in the procurement settings."
                       if cfg.use_quotations == "only" and offers and not any(o.quote_price for o in offers) else
                       "None of the suppliers has a price on record for this item, so no vendor can be recommended. "
                       "Please choose a vendor in your ERP." if offers else
                       "No supplier is listed for this item and the ERP has no past purchase orders for it, so no vendor can be recommended. "
                       "Please choose a vendor in your ERP.")
            return self._manual(wo, base, problem, ranking, dry_run)

        summary = {"vendor": best.name, "qty": need.qty, "total": best.total, "score": round(best.score, 1)}
        if dry_run:
            return {**base, "action": "would_propose", **summary, "ranking": ranking.to_dict()}

        today = self.today()
        with self.db.transaction():
            self.workorders.set_status(wo["id"], "claimed")
            prop = self.proposals.create(wo["id"], code, "awaiting", supplier=best.supplier, supplier_name=best.name, qty=need.qty,
                                         rate=best.unit_price, total=best.total, ranking=ranking.to_dict())
            note = vendor_note_for(p, best, need, cfg, today, prop.id)
            route = route_for(cfg, code, p.get("item_group"))
            # kept with the proposal so the document gets exactly what was approved, and the route that was offered
            self.proposals.set_ranking(prop.id, {**ranking.to_dict(), **({"vendor_note": note} if note else {}), "route": route})
            req = build_po_request(prop.id, p, ranking, best, need, cfg, today, vendor_note=note, route=route, moq=moq)
            record = self.approvals.submit(req, deliver=False)
            self.proposals.set_approval(prop.id, record.id)
        self.approvals.deliver(record)                       # network I/O after the database commit
        return {**base, "action": "proposed", "proposal_id": prop.id, "approval_id": record.id, **summary}

    def _moq(self, item_code: str) -> Optional[float]:
        """The minimum order quantity set on the item in the ERP, or None (none set, or the ERP could not say)."""
        moq_of = getattr(self.erp, "item_min_order_qty", None)
        if moq_of is None:
            return None
        try:
            v = moq_of(item_code)
        except Exception as exc:  # noqa: BLE001 - a failed lookup must not stop the proposal; the ERP itself still checks when the order is made
            log.warning("could not read the minimum order quantity of %s: %s", item_code, exc)
            return None
        return float(v) if v else None

    def _manual(self, wo: dict[str, Any], base: dict[str, Any], problem: str, ranking: Optional[Ranking], dry_run: bool) -> dict[str, Any]:
        if dry_run:
            return {**base, "action": "would_ask_manual", "problem": problem}
        cfg = self.settings.procurement()
        with self.db.transaction():
            self.workorders.set_status(wo["id"], "claimed")
            prop = self.proposals.create(wo["id"], base["item_code"], "manual", ranking=ranking.to_dict() if ranking else None, note=problem)
            record = self.approvals.submit(build_manual_request(prop.id, wo["payload"], problem, ranking, cfg), deliver=False)
            self.proposals.set_approval(prop.id, record.id)
        self.approvals.deliver(record)
        return {**base, "action": "manual", "proposal_id": prop.id, "approval_id": record.id, "problem": problem}

    def _tax_setups(self, offers: list[Any], item_code: str, cfg: Any) -> Optional[dict[str, TaxSetup]]:
        """Ask the ERP which duties and taxes it applies to this item from each vendor. None = taxes are switched off.
        A vendor the ERP cannot answer for gets a setup saying why, so the comparison says so instead of guessing."""
        if not cfg.include_taxes:
            return None
        setups: dict[str, TaxSetup] = {}
        for o in offers:
            try:
                setups[o.supplier] = self.erp.get_purchase_taxes(o.supplier, item_code, self.today())
            except Exception as exc:  # noqa: BLE001 - one vendor's tax lookup must not stop the proposal
                log.warning("could not read the purchase taxes for %s / %s: %s", o.supplier, item_code, exc)
                setups[o.supplier] = TaxSetup(template=None, source="none", notes=(f"could not read the ERP's tax setup: {exc}",))
        return setups

    # ================================================================== read-only preview (admin screen)
    def preview(self, item_code: str, qty: float, cover_days: Optional[float] = None, lead_time_days: float = 7.0, uom: str = "units") -> dict[str, Any]:
        cfg = self.settings.procurement()
        offers = self._offers(item_code, cfg)
        return rank_vendors(offers, Need(qty, cover_days, lead_time_days, uom), cfg, self._tax_setups(offers, item_code, cfg)).to_dict()

    FINISHED_ORDER_STATUSES = ("Completed", "Closed", "Cancelled", "Delivered")

    def order_settled(self, item_code: str, since: datetime) -> bool:
        """True when an order made for this item since `since` (from a reorder the owner approved) was created in the ERP and none of it is still on its way:
        received in full, closed, cancelled, or no longer there. False when no order was made, when any is still a draft or still to come, or when the ERP cannot say.
        The quiet period after "Create order" exists so the item is not asked about again while its order is on the way; this says the order is no longer on the way."""
        made = [p for p in self.proposals.list(status="ordered", item_code=item_code, limit=10) if p.po_name and p.created_at >= since]
        if not made:
            return False
        try:
            for p in made:
                for d in self.erp.get_documents("Purchase Order", name=p.po_name, limit=1):
                    if d["docstatus"] == 0 or (d["docstatus"] == 1 and d["status"] not in self.FINISHED_ORDER_STATUSES and (d.get("per_received") or 0) < 100):
                        return False
        except Exception:  # noqa: BLE001 - if the ERP cannot be asked, the quiet period stays
            log.warning("could not check whether the order for %s has arrived", item_code, exc_info=True)
            return False
        return True

    # ================================================================== an order request somebody asked for
    def request_purchase(self, payload: dict[str, Any], who: str) -> dict[str, Any]:
        """Prepare the usual order (or requisition) request for an item on demand. Nothing is created in the ERP: the request still has to be answered with Create order.
        If a request for the item is already waiting (or a reorder suggestion that leads to one) that one is returned instead of making a second.
        status: waiting | reorder_waiting | created | manual | need_qty | refused"""
        code = payload["item_code"]
        for p in self.proposals.open_for_item(code):
            rec = self.approvals.get(p.approval_id) if p.approval_id else None
            if rec is not None and rec.status == "pending":
                fresh = self._rebuild_if_stale(rec, p, who) if p.status == "awaiting" and rec.kind in (KIND_PO, KIND_PR) else None
                if fresh is not None:
                    return {"status": "refreshed", "approval": fresh[0], "proposal": p, "why": fresh[1]}
                return {"status": "waiting", "approval": rec, "proposal": p}
        for rec in self.approvals.list("pending", 200):
            if rec.kind == "inventory.reorder" and rec.request.facts.get("item_code") == code:
                return {"status": "reorder_waiting", "approval": rec}
        cfg = self.settings.procurement()
        if not (payload.get("suggested_qty") or cfg.fallback_qty):
            return {"status": "need_qty"}
        wo = self.workorders.emit(REORDER_NEEDED, payload, source=f"request:{who}")
        action = self._handle(wo, dry_run=False)
        if action["action"] in ("proposed", "manual"):
            rec = self.approvals.get(action["approval_id"])
            return {"status": "created" if action["action"] == "proposed" else "manual", "approval": rec, "proposal": self.proposals.get(action["proposal_id"]), "problem": action.get("problem")}
        return {"status": "refused", "why": action["action"]}

    def _rebuild_if_stale(self, record: ApprovalRecord, prop: Proposal, who: str) -> Optional[tuple[ApprovalRecord, str]]:
        """A waiting request that was made by an older version (no Change quantity / Change vendor buttons) or is for less than the item's minimum order quantity
        (the ERP would refuse the order) is replaced by a fresh one. The vendor choice is kept when that vendor is still comparable. None = it is fine as it is."""
        has_buttons = {"change_qty", "change_vendor"} <= {o.action for o in record.request.options}
        moq = self._moq(prop.item_code)
        short = bool(moq and prop.qty and float(prop.qty) < moq and record.kind == KIND_PO)
        if has_buttons and not short:
            return None
        wo = (self.workorders.get(prop.work_order_id) or {"payload": {}})["payload"]
        cfg = self._cfg_for(wo)
        uom = wo.get("uom", "units")
        qty = float(moq) if short else float(prop.qty or 0)
        offers = self._offers(prop.item_code, cfg)
        need = Need(qty=qty, cover_days=wo.get("cover_days"), fallback_lead_days=float(wo.get("lead_time_days") or 7), uom=uom)
        ranking = rank_vendors(offers, need, cfg, self._tax_setups(offers, prop.item_code, cfg))
        best = next((v for v in ranking.eligible if v.supplier == prop.supplier), None) or ranking.best
        if best is None:
            return None
        parts = []
        if not has_buttons:
            parts.append("The old request was made before you could change a quantity or a vendor, so it is rebuilt with the Change quantity and Change vendor buttons.")
        if short:
            parts.append(f"The quantity is raised from {_n(prop.qty)} to {_n(qty)} {uom} because your ERP does not accept an order below this item's minimum order quantity of {_n(moq)}.")
        if best.supplier != prop.supplier:
            parts.append(f"The best vendor is now {best.name} (it was {prop.supplier_name}).")
        why = " ".join(parts)
        new = self._revise(record, prop, wo, ranking, best, need, cfg, f"Rebuilt when {who} asked for this order again. {why}", moq=moq if short else None)
        return (new, why) if new is not None else None

    # ================================================================== changing a request before it is answered
    def _on_action(self, record: ApprovalRecord, action: str, actor: Actor, channel: str, params: dict[str, Any]) -> Optional[DecisionResult]:
        """'Change quantity' and 'Change vendor' on an order or requisition request. The request is replaced by a revised one (same proposal, nothing ordered yet);
        without the new value the person is asked for it. Anything the checks do not allow is refused with the reason, and nothing changes."""
        if action not in ("change_qty", "change_vendor"):
            return None
        prop = self.proposals.by_approval(record.id)
        if prop is None or prop.status != "awaiting" or record.kind not in (KIND_PO, KIND_PR):
            return self._refused(record, "This request can no longer be changed.")
        wo = (self.workorders.get(prop.work_order_id) or {"payload": {}})["payload"]
        cfg = self._cfg_for(wo)
        who = f"{actor.id} via {channel}"
        if action == "change_qty":
            return self._change_qty(record, prop, wo, cfg, params, who)
        return self._change_vendor(record, prop, wo, cfg, params, who)

    @staticmethod
    def _refused(record: ApprovalRecord, why: str) -> DecisionResult:
        return DecisionResult(ok=False, status="refused", message=why, approval=record)

    def _revise(self, record: ApprovalRecord, prop: Proposal, wo: dict[str, Any], ranking: Ranking, best: ScoredVendor, need: Need, cfg: Any, text: str,
                extra: Optional[dict[str, Any]] = None, moq: Optional[float] = None) -> Optional[ApprovalRecord]:
        """Store the revision and swap the request, all or nothing; then send the new request and retire the old one. None = it was answered in the meantime."""
        today = self.today()
        reopening = prop.status == "ordered" and bool(prop.po_name)                    # the order already exists in the ERP and is being changed
        replaces = prop.po_name if reopening else (prop.ranking or {}).get("replaces_po")
        if replaces:
            text = f"{text} This replaces draft purchase order {replaces}: it is removed when you press Create order, and stays as it is if you reject this request."
        note = vendor_note_for(wo, best, need, cfg, today, prop.id)
        route = (prop.ranking or {}).get("route") or route_for(cfg, prop.item_code, wo.get("item_group"))
        req = build_po_request(prop.id, wo, ranking, best, need, cfg, today, vendor_note=note, route=route, revision=text, moq=moq, replaces=replaces)
        with self.db.transaction():
            new = self.approvals.supersede(record.id, req, "replaced by a revised request")
            if new is None:
                return None
            self.proposals.revise(prop.id, supplier=best.supplier, supplier_name=best.name, qty=need.qty, rate=best.unit_price, total=best.total, note=text)
            kept = {k: (prop.ranking or {})[k] for k in ("order_revision", "replaced") if (prop.ranking or {}).get(k) is not None}
            stored = {**ranking.to_dict(), **({"vendor_note": note} if note else {}), "route": route, "revisions": [*((prop.ranking or {}).get("revisions") or []), text], **kept, **(extra or {})}
            if replaces:
                stored["replaces_po"] = replaces
                stored["order_revision"] = int((prop.ranking or {}).get("order_revision") or 0) + (1 if reopening else 0)
            self.proposals.set_ranking(prop.id, stored)
            if reopening:
                self.proposals.update_status(prop.id, "awaiting", f"the order {prop.po_name} is being changed", po_name=None)
            self.proposals.set_approval(prop.id, new.id)
        self.approvals.finish_supersede(record.id, new)
        return new

    def _new_qty(self, raw: Any, prop: Proposal, uom: str, what: str = "request") -> tuple[Optional[float], str]:
        """(the quantity, "") or (None, why it is refused)."""
        try:
            qty = round(float(str(raw).replace(",", "").strip()), 3)
        except ValueError:
            return None, f"{str(raw)[:30]!r} is not a number. Reply with just the quantity, for example 800."
        if not math.isfinite(qty) or qty <= 0:
            return None, "The quantity must be more than zero."
        if prop.qty and qty == float(prop.qty):
            return None, f"The {what} is already for {_n(qty)} {uom}."
        if prop.qty and qty > 100 * float(prop.qty):
            return None, f"{_n(qty)} is more than 100 times the {_n(prop.qty)} now {'ordered' if what == 'order' else 'requested'}: please check the number. If you really mean it, create that order in your ERP."
        moq_of = getattr(self.erp, "item_min_order_qty", None)
        moq = moq_of(prop.item_code) if moq_of else None
        if moq and qty < moq:
            return None, f"Your ERP does not accept an order below this item's minimum order quantity of {_n(moq)} {uom}."
        return qty, ""

    def _change_qty(self, record: ApprovalRecord, prop: Proposal, wo: dict[str, Any], cfg: Any, params: dict[str, Any], who: str) -> DecisionResult:
        uom, name = wo.get("uom", "units"), wo.get("item_name") or prop.item_code
        raw = params.get("qty")
        if raw is None or str(raw).strip() == "":
            return DecisionResult(ok=True, status="need_input", approval=record, message="How many?",
                                  prompt=f"How many {uom} of {name} do you want to order? It is {_n(prop.qty)} now. Reply with a number (or 'cancel').")
        qty, why = self._new_qty(raw, prop, uom)
        if qty is None:
            return self._refused(record, why)
        offers = self._offers(prop.item_code, cfg)
        need = Need(qty=qty, cover_days=wo.get("cover_days"), fallback_lead_days=float(wo.get("lead_time_days") or 7), uom=uom)
        ranking = rank_vendors(offers, need, cfg, self._tax_setups(offers, prop.item_code, cfg))
        best = ranking.best
        if best is None:
            return self._refused(record, f"No vendor can be compared for {_n(qty)} {uom}, so the request is unchanged.")
        text = (f"Quantity changed from {_n(prop.qty)} to {_n(qty)} {uom} by {who}. The vendors were checked again for the new quantity."
                + (f" The best vendor is now {best.name} (it was {prop.supplier_name})." if best.supplier != prop.supplier else ""))
        new = self._revise(record, prop, wo, ranking, best, need, cfg, text)
        if new is None:
            return self._refused(record, "This request was answered or closed in the meantime, so nothing was changed.")
        return DecisionResult(ok=True, status="revised", approval=new, message=f"Done: {_n(qty)} {uom}, from {best.name}, {best.total:,.2f} in total. A revised request is on its way; the old one is closed.")

    def _change_vendor(self, record: ApprovalRecord, prop: Proposal, wo: dict[str, Any], cfg: Any, params: dict[str, Any], who: str) -> DecisionResult:
        stored = prop.ranking or {}
        vendors = [ScoredVendor(**v) for v in stored.get("vendors", [])]
        others = [v for v in vendors if v.eligible and v.supplier != prop.supplier]
        wanted = str(params.get("supplier") or "").strip()
        if not wanted:
            if not others:
                return self._refused(record, "No other vendor could be compared for this item, so there is nothing to choose between.")
            choices = [{"id": v.supplier, "label": f"{v.name}: {v.total:,.2f} in total, delivery about {_n(v.lead_days)} days, score {v.score:.0f} of 100"} for v in others[:6]]
            NL = chr(10)
            labels = [c["label"] for c in choices]
            prompt = "Which vendor instead? Reply with the number:" + NL + NL.join(f"{n}. {t}" for n, t in enumerate(labels, 1)) + NL + "(or cancel)"
            return DecisionResult(ok=True, status="need_input", approval=record, message="Choose a vendor", prompt=prompt, choices=choices)
        chosen = next((v for v in others if v.supplier == wanted), None)
        if chosen is None:
            return self._refused(record, "That vendor is not one of the vendors that were compared for this request.")
        need = Need(qty=float(prop.qty or 0), cover_days=wo.get("cover_days"), fallback_lead_days=float(wo.get("lead_time_days") or 7), uom=wo.get("uom", "units"))
        ranking = Ranking(vendors=vendors, weights=stored.get("weights") or {}, notes=list(stored.get("notes") or []))
        text = f"Vendor changed from {prop.supplier_name} to {chosen.name} by {who}. The quantity is unchanged."
        new = self._revise(record, prop, wo, ranking, chosen, need, cfg, text, extra={"chosen_by": who, "chosen_supplier": chosen.supplier})
        if new is None:
            return self._refused(record, "This request was answered or closed in the meantime, so nothing was changed.")
        return DecisionResult(ok=True, status="revised", approval=new, message=f"Done: {chosen.name}, {chosen.total:,.2f} in total. A revised request is on its way; the old one is closed.")

    # ================================================================== an order that already exists: change it, replace it, or take it back
    def _restore_replaced(self, prop: Proposal, why: str) -> bool:
        """A request that was meant to REPLACE an order ended without being approved (rejected, snoozed, expired, cancelled): the order it would have replaced stays, as it was."""
        old = (prop.ranking or {}).get("replaces_po")
        if not old or prop.po_name:
            return False
        self.proposals.set_ranking(prop.id, {**(prop.ranking or {}), "replaces_po": None})
        self.proposals.update_status(prop.id, "ordered", why, po_name=old)
        self.workorders.set_status(prop.work_order_id, "done")
        return True

    def find_order(self, po_name: Optional[str] = None, item_code: Optional[str] = None) -> Optional[Proposal]:
        """An order this application created (never any other): by its number, or the newest for an item."""
        rows = [p for p in self.proposals.list(status="ordered", item_code=item_code, limit=300) if p.po_name and (p.ranking or {}).get("document") != "requisition"]
        if po_name:
            rows = [p for p in rows if p.po_name.lower() == po_name.lower()]
        return rows[0] if rows else None

    def order_state(self, po_name: str) -> Optional[dict[str, Any]]:
        """What ERPNext says about the order now; None when ERPNext could not be asked."""
        try:
            docs = self.erp.get_documents("Purchase Order", name=po_name, limit=1)
        except Exception:  # noqa: BLE001
            log.warning("could not read %s", po_name, exc_info=True)
            return None
        if not docs:
            return {"exists": False}
        d = docs[0]
        return {"exists": True, "docstatus": d["docstatus"], "status": d["status"], "per_received": float(d.get("per_received") or 0), "per_billed": float(d.get("per_billed") or 0)}

    def describe_order(self, prop: Proposal, state: Optional[dict[str, Any]] = None) -> str:
        wo = (self.workorders.get(prop.work_order_id) or {"payload": {}})["payload"]
        kind = "" if not state or not state.get("exists") else (" (a draft)" if state["docstatus"] == 0 else " (submitted)")
        return f"{prop.po_name}{kind}: {_n(prop.qty)} {wo.get('uom', 'units')} of {wo.get('item_name') or prop.item_code} ({prop.item_code}) from {prop.supplier_name}, {(prop.total or 0):,.2f} in total."

    def order_problem(self, prop: Proposal, strict: bool = True) -> Optional[str]:
        """Why this order cannot be changed or cancelled from here, in words; None when it can. strict=False: goods received or invoiced do not stop it
        (a quantity change; the ERP then refuses only a quantity below what arrived or was billed)."""
        po = prop.po_name
        state = self.order_state(po)
        if state is None:
            return f"I could not read {po} in ERPNext just now, so I changed nothing. Please try again in a moment."
        if not state["exists"] or state["docstatus"] == 2:
            self._forget_removed(prop, "it is no longer in ERPNext")
            return f"{po} is no longer in ERPNext (it was deleted or cancelled there), so there is nothing to change or cancel."
        if strict and (state["per_received"] > 0 or state["per_billed"] > 0):
            return (f"{po} cannot be changed or cancelled here: {state['per_received']:g}% of it has been received and {state['per_billed']:g}% billed. "
                    "Deal with it in ERPNext (cancel the receipt or invoice first).")
        return None

    def _forget_removed(self, prop: Proposal, why: str) -> None:
        """The order is gone from ERPNext (removed there, or by us): the proposal says so, the item is no longer 'ordered recently', and the buttons that offered to change it are closed."""
        self.proposals.update_status(prop.id, "cancelled", f"order {prop.po_name}: {why}")
        self.workorders.set_status(prop.work_order_id, "done")
        if self.on_order_removed is not None:
            self.on_order_removed(prop.item_code)
        mid = (prop.ranking or {}).get("order_manage_approval")
        if mid:
            self.approvals.cancel(mid, f"{prop.po_name} {why}")

    def _open_order_request(self, prop: Proposal, deliver: bool) -> ApprovalRecord:
        wo = (self.workorders.get(prop.work_order_id) or {"payload": {}})["payload"]
        record = self.approvals.submit(build_order_request(prop.id, prop.po_name or "", wo, prop.supplier, prop.supplier_name, prop.qty, prop.total), deliver=deliver)
        self.proposals.set_ranking(prop.id, {**(prop.ranking or {}), "order_manage_approval": record.id})
        return record

    def manage_request(self, prop: Proposal) -> ApprovalRecord:
        """The message with the buttons for this order (Change quantity, Change vendor, Cancel order): the one still waiting, or a fresh one that has not been sent to anybody yet."""
        mid = (prop.ranking or {}).get("order_manage_approval")
        rec = self.approvals.get(mid) if mid else None
        if rec is not None and rec.status == "pending":
            return rec
        return self._open_order_request(prop, deliver=False)

    def _after_decided(self, record: ApprovalRecord) -> None:
        """After 'Create order' was answered and the draft exists: send the message with the buttons to change or cancel it."""
        if record.kind != KIND_PO or record.decision is None or record.decision.action != "approve":
            return
        prop = self.proposals.by_approval(record.id)
        if prop is None or prop.status != "ordered" or not prop.po_name or (prop.ranking or {}).get("document") != "order":
            return
        self._open_order_request(prop, deliver=True)

    def _on_order_decision(self, record: ApprovalRecord, decision: Decision) -> None:
        return None                                                    # "Keep it": the order is fine, there is nothing to do

    def _on_order_action(self, record: ApprovalRecord, action: str, actor: Actor, channel: str, params: dict[str, Any]) -> Optional[DecisionResult]:
        """Change quantity / Change vendor / Cancel order on the message that follows 'Create order'."""
        if action not in ("change_qty", "change_vendor", "cancel_order"):
            return None
        facts = record.request.facts or {}
        po = facts.get("po_name")
        prop = self.proposals.get(facts.get("proposal_id") or "")
        if prop is None or prop.status != "ordered" or prop.po_name != po:
            return self._refused(record, f"{po} has been changed, replaced or cancelled since this message, so there is nothing to do here. Ask me about it again, for example: edit PO {po}.")
        problem = self.order_problem(prop, strict=action != "change_qty")       # a partly received order can still have its quantity changed (not below what arrived)
        if problem:
            return self._refused(record, problem)
        wo = (self.workorders.get(prop.work_order_id) or {"payload": {}})["payload"]
        cfg = self._cfg_for(wo)
        who = f"{actor.id} via {channel}"
        if action == "change_qty":
            return self._update_order_qty(record, prop, wo, actor, channel, params, cfg)
        if action == "change_vendor":
            return self._change_vendor(record, prop, wo, cfg, params, who)
        return self._cancel_order(record, prop, actor, channel, params, cfg)

    def _update_order_qty(self, record: ApprovalRecord, prop: Proposal, wo: dict[str, Any], actor: Actor, channel: str, params: dict[str, Any], cfg: Any) -> DecisionResult:
        """Change quantity on an order that exists: the SAME purchase order gets the new quantity in ERPNext (its number, vendor and price stay). Nothing is deleted or re-created."""
        po, uom, name = prop.po_name, wo.get("uom", "units"), wo.get("item_name") or prop.item_code
        raw = params.get("qty")
        if raw is None or str(raw).strip() == "":
            return DecisionResult(ok=True, status="need_input", approval=record, message="How many?",
                                  prompt=f"How many {uom} of {name} should {po} be for? It is {_n(prop.qty)} now. The quantity is changed in this same order; "
                                         "the vendor and the price stay as they are. Reply with a number (or 'cancel').")
        qty, why = self._new_qty(raw, prop, uom, "order")
        if qty is None:
            return self._refused(record, why)
        try:
            done = self.erp.update_purchase_order_qty(po, prop.id, prop.item_code, qty, acting=self._acting_for(actor.id, cfg))
        except RuntimeError:                                         # personal login is required and this answer did not come from a signed-in console session
            return self._refused(record, "Your settings only allow changes in ERPNext under the approver's own ERPNext name, and this answer did not come from a signed-in console session "
                                         f"(for example it came from {channel}). Please change it in the console. Nothing was changed.")
        if not done.ok:
            return self._refused(record, f"ERPNext did not change {po}: {str(done.error).rstrip('.')}. Nothing was changed.")
        old = prop.qty
        total = done.total if done.total is not None else round(qty * float(prop.rate or 0), 2)
        text = f"Quantity of {po} changed from {_n(old)} to {_n(qty)} {uom} by {actor.id} via {channel}, in the same order (vendor and price unchanged)."
        self.proposals.revise(prop.id, supplier=prop.supplier, supplier_name=prop.supplier_name, qty=qty, rate=prop.rate, total=total, note=text)
        self.proposals.set_ranking(prop.id, {**(prop.ranking or {}), "revisions": [*((prop.ranking or {}).get("revisions") or []), text]})
        req = build_order_request(prop.id, po, wo, prop.supplier, prop.supplier_name, qty, total, submitted=done.submitted, changed=text)
        new = self.approvals.supersede(record.id, req, f"{po} was changed to {_n(qty)} {uom}")
        if new is not None:
            self.proposals.set_ranking(prop.id, {**(self.proposals.get(prop.id).ranking or {}), "order_manage_approval": new.id})
            self.approvals.finish_supersede(record.id, new)               # the old message is closed; the new one, with the buttons, follows the reply
        what = "Purchase order" if done.submitted else "Draft purchase order"
        tail = " If you have already sent it to the vendor, tell them about the change." if done.submitted else ""
        return DecisionResult(ok=True, status="revised" if new is not None else "decided", approval=new or record,
                              message=f"Done: {what} {po} is now for {_n(qty)} {uom} (was {_n(old)}), {total:,.2f} in total. Same order, same vendor and price.{tail}")

    def _cancel_order(self, record: ApprovalRecord, prop: Proposal, actor: Actor, channel: str, params: dict[str, Any], cfg: Any) -> DecisionResult:
        po = prop.po_name
        confirm = str(params.get("confirm") or "").strip().lower()
        if confirm not in ("yes", "no"):
            state = self.order_state(po) or {}
            how = "deleted in ERPNext" if state.get("docstatus") == 0 else "cancelled in ERPNext"
            return DecisionResult(ok=True, status="need_input", approval=record, message="Cancel the order?",
                                  prompt=f"Cancel purchase order {self.describe_order(prop, state)} It will be {how}. Reply yes to cancel it, or no to keep it.",
                                  choices=[{"id": "yes", "label": "Yes, cancel it"}, {"id": "no", "label": "No, keep it"}])
        if confirm == "no":
            return DecisionResult(ok=True, status="info", approval=record, message=f"OK, {po} is kept as it is.")
        try:
            removed = self.erp.cancel_purchase_order(po, prop.id, acting=self._acting_for(actor.id, cfg))
        except RuntimeError:                                         # personal login is required and this answer did not come from a signed-in console session
            return self._refused(record, "Your settings only allow changes in ERPNext under the approver's own ERPNext name, and this answer did not come from a signed-in console session "
                                         f"(for example it came from {channel}). Please cancel it in the console. Nothing was changed.")
        if not removed.ok:
            return self._refused(record, f"ERPNext did not cancel {po}: {removed.error}. Nothing was changed.")
        phrase = {"deleted the draft": "was deleted in ERPNext", "cancelled it": "was cancelled in ERPNext"}.get(removed.note, "is no longer in ERPNext")
        self._forget_removed(prop, f"{phrase.replace('was ', '')} by {actor.id} via {channel}" if removed.note in ("deleted the draft", "cancelled it") else "it is no longer in ERPNext")
        self.approvals.cancel(record.id, f"{po} {phrase}. Nothing is on order from this request now.")
        name = (self.workorders.get(prop.work_order_id) or {"payload": {}})["payload"].get("item_name") or prop.item_code
        return DecisionResult(ok=True, status="decided", approval=self.approvals.get(record.id), message=f"Done: {po} {phrase}. Nothing is on order for {name} from this request now.")

    # ================================================================== approval outcomes
    def _outcome_detail(self, record: ApprovalRecord) -> str:
        """After an answered order or requisition request: the number of the draft that was created, so it can be found in ERPNext straight away."""
        if record.decision is None or record.decision.action not in ("approve", "requisition"):
            return ""
        prop = self.proposals.by_approval(record.id)
        if prop is None or prop.status != "ordered":
            return ""
        wo = (self.workorders.get(prop.work_order_id) or {"payload": {}})["payload"]
        what = f"{_n(prop.qty)} {wo.get('uom', 'units')} of {wo.get('item_name') or prop.item_code} ({prop.item_code}) from {prop.supplier_name}"
        if not prop.po_name:
            return "Nothing was created in ERPNext: creating orders is switched off in your procurement settings."
        if (prop.ranking or {}).get("document") == "requisition":
            return f"Draft purchase requisition {prop.po_name} was created in ERPNext for {what}. Your buyer turns it into an order there."
        return (f"Draft purchase order {prop.po_name} was created in ERPNext for {what}. It is only a draft: open it in ERPNext, check it and submit it. "
                "Nothing has been sent to the vendor.")

    def _on_decision(self, record: ApprovalRecord, decision: Decision) -> None:
        prop = self.proposals.by_approval(record.id)
        if prop is None:
            return
        cfg = self.settings.procurement()
        now = self.clock()
        if decision.action in ("approve", "requisition"):
            route = "requisition" if decision.action == "requisition" else ((prop.ranking or {}).get("route") or "order")
            self._place_order(prop, decision, cfg, route)
        elif decision.action == "ack":
            self.proposals.update_status(prop.id, "acknowledged", f"acknowledged by {decision.actor_id} via {decision.channel}")
            self.workorders.set_status(prop.work_order_id, "done")
        elif decision.action in ("snooze", "dismiss") and self._restore_replaced(prop, f"the change was not made ({decision.action} by {decision.actor_id} via {decision.channel}): the order stays as it was"):
            pass
        elif decision.action == "snooze":
            days = cfg.snooze_days
            self.proposals.update_status(prop.id, "snoozed", f"snoozed {days}d by {decision.actor_id}", snoozed_until=now + timedelta(days=days))
        elif decision.action == "dismiss":
            self.proposals.update_status(prop.id, "rejected", f"rejected by {decision.actor_id} via {decision.channel}")
            self.workorders.set_status(prop.work_order_id, "done")

    def _acting_for(self, actor_id: str, cfg: Any) -> Optional[acting.ActingAs]:
        """The approver's own ERPNext session, when this approval came from their signed-in console session (so ERPNext records THEM as
        the creator and checks THEIR permissions). Otherwise None: the service account creates it, and the approver is written in a field."""
        a = acting.current()
        if a is not None and a.user == actor_id:
            return a
        if cfg.require_personal_login:
            raise RuntimeError("your settings only allow orders and requisitions to be created under the approver's own ERPNext name, and this approval did not come from a "
                               "signed-in console session (for example it came from WhatsApp). Please approve it in the console")
        return None

    def _place_order(self, prop: Proposal, decision: Decision, cfg: Any, route: str = "order") -> None:
        """Create the draft purchase order, or the draft purchase requisition when that route was chosen, in the ERP."""
        pr = route == "requisition"
        thing = "purchase requisition" if pr else "purchase order"
        who = f"approved by {decision.actor_id} via {decision.channel}"
        if cfg.po_mode != "draft":
            self.proposals.update_status(prop.id, "ordered", f"{who}; no {thing} created (po_mode is off)")
            self.workorders.set_status(prop.work_order_id, "done")
            return
        if prop.po_name:                                    # already created (e.g. a retry after a later failure)
            self.proposals.update_status(prop.id, "ordered", f"{who}; {thing} {prop.po_name}", po_name=prop.po_name)
            self.workorders.set_status(prop.work_order_id, "done")
            return
        wo = self.workorders.get(prop.work_order_id) or {"payload": {}}
        pay = wo["payload"]
        recovered = decision.params.get("recovered_document")           # finishing an answer after a restart: the document is already in the ERP and is not made or taken back again
        moq = None if (pr or recovered) else self._moq(prop.item_code)
        if moq and float(prop.qty or 0) < moq:
            raise RuntimeError(f"this item has a minimum order quantity of {_n(moq)} {pay.get('uom', 'units')} in your ERP and this request is for {_n(prop.qty)}. "
                               f"Press Change quantity and enter {_n(moq)} or more, or type: create a purchase order for {prop.item_code}")
        best = next((v for v in (prop.ranking or {}).get("vendors", []) if v["supplier"] == prop.supplier), {})
        arrival = expected_arrival(self.today(), best.get("lead_days")) or self.today() + timedelta(days=int(pay.get("lead_time_days") or 7))
        who_acting = None if recovered else self._acting_for(decision.actor_id, cfg)
        approved_by = f"{decision.actor_id} via {decision.channel}"
        replaces = None if pr else (prop.ranking or {}).get("replaces_po")
        if replaces and not recovered:                       # this order replaces one that exists: take that one back first, so a failure leaves nothing doubled
            removed = self.erp.cancel_purchase_order(replaces, prop.id, acting=who_acting)
            if not removed.ok:
                raise DecisionRefused(f"The earlier order {replaces} could not be removed, so it was not replaced: {str(removed.error).rstrip('.')}. Press Reject on this request to keep the earlier order as it is.")
        revision = int((prop.ranking or {}).get("order_revision") or 0)
        if recovered:
            result = PurchaseOrderResult(ok=True, name=str(recovered))
        elif pr:
            note = requisition_note(pay, _Vendor(best), _Need(prop.qty, pay.get("uom", "units")), self.today())
            result = self.erp.create_material_request(RequisitionDraft(
                item_code=prop.item_code, qty=float(prop.qty or 0), uom=pay.get("uom", "units"), required_by=arrival, rate=prop.rate,
                note=note, reference=prop.id, approved_by=approved_by), acting=who_acting)
        else:
            draft = PurchaseOrderDraft(
                supplier=prop.supplier or "", item_code=prop.item_code, qty=float(prop.qty or 0), rate=float(prop.rate or 0),
                uom=pay.get("uom", "units"), required_by=arrival,
                remarks=f"Proposed by the ERP agents vendor selector; {who}. Review before submitting."
                        + (f" The price is from supplier quotation {best.get('quote_ref')}." if best.get("price_source") == "quotation" else ""),
                vendor_note=(prop.ranking or {}).get("vendor_note", ""),
                taxes_template=best.get("tax_template") or None,      # the template the ERP itself chose for this vendor
                supplier_quotation=(best.get("quote_ref") or "") if best.get("price_source") == "quotation" else "",     # the quotation the price came from
                reference=prop.id if not revision else f"{prop.id}-{revision}",      # lets a retry find the order instead of creating a second one
                approved_by=approved_by,                              # also written on the order: the service account would otherwise be the only name on it
            )
            result = self.erp.create_purchase_order(draft, acting=who_acting)
        if not result.ok:
            # Raising rolls the whole decision back: the owner is told and can press the button again.
            raise RuntimeError(f"the ERP did not accept the {thing}: {result.error}")
        done = {**(prop.ranking or {}), "document": "requisition" if pr else "order"}
        if replaces:
            done["replaces_po"] = None
            done["replaced"] = [*(done.get("replaced") or []), replaces]
        self.proposals.set_ranking(prop.id, done)
        by = ("found in ERPNext after a restart, where it had been made before the answer was written down" if recovered else
              f"created under {who_acting.user}'s own ERPNext login" if who_acting else "created by the service account, with the approver's name on it")
        self.proposals.update_status(prop.id, "ordered", f"{who}; draft {thing} {result.name} ({by})" + (f"; it replaced draft {replaces}" if replaces else ""), po_name=result.name)
        self.workorders.set_status(prop.work_order_id, "done")

    def _reconcile(self, record: ApprovalRecord) -> Optional[Decision]:
        """Was this request already acted on in the ERP, though the answer was never written down? Look for the draft by the reference it was made with: if it is there, the
        answer is the one written on it (an order or a requisition, by whom, from where). Raises when the ERP cannot be asked: the next pass tries again."""
        find = getattr(self.erp, "find_by_reference", None)
        prop = self.proposals.by_approval(record.id)
        if find is None or prop is None or prop.status not in ("awaiting", "manual"):
            return None
        revision = int((prop.ranking or {}).get("order_revision") or 0)
        for doctype, reference, action in (("Purchase Order", prop.id if not revision else f"{prop.id}-{revision}", "approve"), ("Material Request", prop.id, "requisition")):
            found = find(doctype, reference)
            if found is None:
                continue
            who = parse_approver(found.approved_by)
            if who is None:
                self.approvals.warn_once(f"approver:{record.id}", "request %s: %s exists in the ERP, but the approver written on it cannot be read, so the answer was not recorded. Answer the request again (it will find %s) or cancel it.",
                                         record.id, found.name, found.name)
                return None
            return Decision(approval_id=record.id, action=action, actor_id=who[0], channel=who[1], params={"recovered_document": found.name}, decided_at=self.clock(),
                            note="finished after a restart: the draft already existed in ERPNext")
        return None

    def _on_expired(self, record: ApprovalRecord) -> None:
        prop = self.proposals.by_approval(record.id)
        if prop and prop.status in ("awaiting", "manual"):
            if self._restore_replaced(prop, "nobody answered the request to change it: the order stays as it was"):
                return
            self.proposals.update_status(prop.id, "expired", "no answer before the request expired")
            self.workorders.set_status(prop.work_order_id, "pending")      # asked again on the next run

    def _on_cancelled(self, record: ApprovalRecord) -> None:
        prop = self.proposals.by_approval(record.id)
        if prop and prop.status in ("awaiting", "manual"):
            if self._restore_replaced(prop, "the request to change it was cancelled: the order stays as it was"):
                return
            self.proposals.update_status(prop.id, "cancelled", record.status_note or "cancelled")
            self.workorders.set_status(prop.work_order_id, "pending")

"""Quote requests: ask two or three vendors for a price, and bring the replies back as a comparison for approval.

  1. prepare   a person (or an AI agent on their behalf, through the prepare_quote_request tool) asks for prices on an item. The vendors to ask are chosen from the
               ERP: the item's listed suppliers and past vendors, best on-time record first, up to `quote_max_vendors`. A request shows who would be asked, how
               (e-mail or WhatsApp, from the supplier's contact in ERPNext) and the message. NOTHING is sent until a person presses Send requests.
  2. send      each vendor gets the message; the round waits `quote_reply_days`.
  3. compare   prices are read from the vendors' Supplier Quotations in ERPNext dated on or after the request (the vendor enters it from the e-mail, or a person
               types in what the vendor replied: a price is never read out of a message). When every vendor asked has quoted, or the reply date has passed, the
               comparison is sent, scored like any purchase request.
  4. order     Prepare order makes the usual purchase request with the vendor chosen, at the quoted price. It is still only a request: nothing is ordered until a
               person presses Create order on it.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Optional

from erp_agents.core.approvals.models import ApprovalRecord, Decision, DecisionRefused
from erp_agents.core.approvals.service import KindHandler
from erp_agents.core.common import iso, short_id, untrusted
from erp_agents.core.erp.base import QuoteRule
from erp_agents.core.storage import dumps, loads
from ..inventory.agent import on_demand_payload
from ..procurement.scoring import Need, rank_vendors
from .messages import KIND_QUOTE_REQUEST, KIND_QUOTES, build_quote_request, build_quotes_request, fill
from .vendors import VendorMessenger

log = logging.getLogger(__name__)
OPEN = ("proposed", "asked")


class QuoteRounds:
    def __init__(self, app: Any, messenger: VendorMessenger) -> None:
        self.app, self.messenger = app, messenger
        app.approvals.register_handler(KIND_QUOTE_REQUEST, KindHandler(on_decision=self._on_request_decision, on_cancelled=self._on_closed, on_expired=self._on_closed,
                                                                       outcome_detail=self._sent_detail))
        app.approvals.register_handler(KIND_QUOTES, KindHandler(on_decision=self._on_comparison_decision))
        app.approvals.on_decided.append(self._after_decided)

    @property
    def cfg(self) -> Any:
        return self.app.settings.followup()

    def today(self) -> date:
        return self.app.inventory.today()

    def get(self, round_id: str) -> Optional[dict[str, Any]]:
        row = self.app.db.query_one("SELECT * FROM quote_rounds WHERE id=?", (round_id,))
        if row is None:
            return None
        out = dict(row)
        out["suppliers"] = loads(out.pop("suppliers_json")) or []
        return out

    def list(self, status: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        if status:
            rows = self.app.db.query("SELECT id FROM quote_rounds WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = self.app.db.query("SELECT id FROM quote_rounds ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self.get(r["id"]) for r in rows]

    def _set(self, round_id: str, **fields: Any) -> None:
        fields["updated_at"] = iso(self.app.clock())
        cols = ", ".join(f"{k}=?" for k in fields)
        self.app.db.execute(f"UPDATE quote_rounds SET {cols} WHERE id=?", (*fields.values(), round_id))  # column names come from this module, values are bound  # nosec B608
        if fields.get("status") in ("compared", "ordered", "closed"):
            self.messenger.close_replies("quote", round_id)           # the round is over: a vendor's later message is not a reply to the request for a price

    # ---------------------------------------------------------------- 1. prepare
    def candidates(self, item_code: str) -> list[dict[str, Any]]:
        """Vendors worth asking: those on the item record and those bought from before, best record first."""
        since = self.today() - timedelta(days=self.app.settings.procurement().history_days)
        offers = self.app.erp.get_vendor_offers(item_code, since)
        def key(o: Any) -> tuple:
            return (not o.listed, -(o.on_time_rate if o.on_time_rate is not None else 0.5), -(o.orders or 0), o.name)
        out = []
        for o in sorted(offers, key=key):
            why = "listed on the item" if o.listed else f"{o.orders} past order{'s' if o.orders != 1 else ''}" if o.orders else ""
            out.append({"supplier": o.supplier, "name": o.name, "why_listed": why})
        return out

    def prepare(self, item_code: str, qty: float, requested_by: str, reason: str = "", suppliers: Optional[list[str]] = None) -> dict[str, Any]:
        """Prepare a quote round and the request that sends it. Orders nothing and sends nothing."""
        item = next((i for i in self.app.erp.list_items() if i.code == item_code), None)
        if item is None:
            return {"status": "refused", "error": f"no item {untrusted(item_code, 60)}"}
        for r in self.list(limit=50):
            if r["item_code"] == item.code and r["status"] in OPEN:
                return {"status": "waiting", "round": r["id"], "approval_id": r["approval_id"] if r["status"] == "proposed" else r["comparison_id"],
                        "note": "A quote round for this item is already open; no second one was made."}
        pool = self.candidates(item.code)
        if suppliers:
            wanted = {s.lower() for s in suppliers}
            pool = [c for c in pool if c["supplier"].lower() in wanted or c["name"].lower() in wanted]
        vendors = []
        for c in pool:
            r = self.messenger.route(c["supplier"])
            vendors.append({**c, "route": r.channel, "why": r.why})
        can = [v for v in vendors if v["route"] != "none"][: self.cfg.quote_max_vendors]
        shown = can + [v for v in vendors if v["route"] == "none"][: max(0, 2 - len(can))]            # show who could not be asked, when that leaves fewer than two
        if not shown:
            return {"status": "refused", "error": "the ERP lists no supplier for this item and none has sold it to you, so there is nobody to ask"}
        now, today = iso(self.app.clock()), self.today()
        reply_by = today + timedelta(days=self.cfg.quote_reply_days)
        rid = short_id(8)
        message = self.message_for({"supplier": "", "name": "<vendor name>"}, item, qty, reply_by, rid)
        req = build_quote_request(rid, item, qty, shown, message, reply_by, untrusted(reason, 300))
        with self.app.db.transaction():
            self.app.db.execute("INSERT INTO quote_rounds(id, item_code, qty, uom, suppliers_json, status, reply_by, requested_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (rid, item.code, qty, item.uom, dumps([{"supplier": v["supplier"], "name": v["name"]} for v in can]), "proposed", reply_by.isoformat(),
                                 requested_by, now, now))
            record = self.app.approvals.submit(req, deliver=False)
            self._set(rid, approval_id=record.id)
        self.app.approvals.deliver(record)
        return {"status": "created", "round": rid, "approval_id": record.id, "vendors": [v["name"] for v in can],
                "cannot_ask": [f"{v['name']}: {v['why']}" for v in vendors if v["route"] == "none"][:5], "reply_by": reply_by.isoformat(),
                "note": "The request was sent for approval. Nothing is sent to a vendor until a person presses Send requests."}

    def message_for(self, vendor: dict[str, Any], item: Any, qty: float, reply_by: date, rid: str) -> str:
        return fill(self.cfg.quote_message, {"vendor": vendor["name"], "item": item.name, "item_code": item.code, "qty": f"{qty:g}", "uom": item.uom,
                                             "reply_by": reply_by.isoformat(), "reference": f"RFQ-{rid}", "signature": self.cfg.signature})

    # ---------------------------------------------------------------- 2. send
    def _on_request_decision(self, record: ApprovalRecord, decision: Decision) -> None:
        rid = record.request.facts.get("round")
        r = self.get(rid) if rid else None
        if r is None:
            raise DecisionRefused("This quote round no longer exists.")
        if decision.action == "dismiss":
            self._set(rid, status="closed", status_note=f"cancelled by {decision.actor_id}")
            return
        if decision.action != "send":
            return
        item = next((i for i in self.app.erp.list_items() if i.code == r["item_code"]), None)
        if item is None:
            raise DecisionRefused(f"Item {r['item_code']} is no longer in the ERP.")
        today = self.today()
        reply_by = today + timedelta(days=self.cfg.quote_reply_days)
        sent, failed = [], []
        for v in r["suppliers"]:
            out = self.messenger.send("quote", rid, v["supplier"], f"Request for quotation: {item.name} ({item.code})", self.message_for(v, item, r["qty"], reply_by, rid),
                                      "Supplier", v["supplier"], record.id, f"{decision.actor_id} via {decision.channel}")
            (sent if out.ok else failed).append(v["name"] if out.ok else f"{v['name']} ({out.note})")
        if not sent:
            raise DecisionRefused("No request could be sent: " + "; ".join(failed) + ".")
        self._set(rid, status="asked", asked_at=iso(self.app.clock()), reply_by=reply_by.isoformat(),
                  suppliers_json=dumps([v for v in r["suppliers"] if v["name"] in sent]), status_note=("not sent to " + "; ".join(failed)) if failed else None)

    def _sent_detail(self, record: ApprovalRecord) -> str:
        if not record.decision or record.decision.action != "send":
            return ""
        r = self.get(record.request.facts.get("round") or "")
        if r is None:
            return ""
        names = ", ".join(v["name"] for v in r["suppliers"])
        return f"Asked {names} for a price; the comparison comes when they have quoted, or on {r['reply_by']}." + (f" {r['status_note'].capitalize()}." if r.get("status_note") else "")

    def _on_closed(self, record: ApprovalRecord) -> None:
        rid = record.request.facts.get("round")
        if rid:
            self._set(rid, status="closed", status_note="the request to send it was not answered")

    # ---------------------------------------------------------------- 3. compare
    def quotes_for(self, r: dict[str, Any]) -> tuple[list[Any], list[str]]:
        """The asked vendors' valid quotations dated on or after the request, and the names of those that have not quoted."""
        asked_on = (r.get("asked_at") or r["created_at"])[:10]
        offers = self.app.erp.get_vendor_offers(r["item_code"], self.today() - timedelta(days=self.app.settings.procurement().history_days),
                                                QuoteRule(self.today(), max(self.app.settings.procurement().quotation_max_age_days, self.cfg.quote_reply_days + 7)))
        asked = {v["supplier"] for v in r["suppliers"]}
        fresh = [o for o in offers if o.supplier in asked and o.quote_price and o.quote_date and o.quote_date.isoformat() >= asked_on]
        quoted = {o.supplier for o in fresh}
        return fresh, [v["name"] for v in r["suppliers"] if v["supplier"] not in quoted]

    def check(self, dry_run: bool = False) -> dict[str, Any]:
        """Rounds that have all their quotations, or whose reply date has passed, get their comparison."""
        report: dict[str, Any] = {"open": 0, "compared": 0, "actions": []}
        today = self.today()
        for r in self.list("asked", limit=100):
            report["open"] += 1
            try:
                fresh, missing = self.quotes_for(r)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read the quotations of round %s: %s", r["id"], exc)
                continue
            due = date.fromisoformat(r["reply_by"]) <= today if r.get("reply_by") else True
            if missing and not due:
                continue
            if dry_run:
                report["actions"].append({"round": r["id"], "action": "would_compare", "quotes": len(fresh)})
                continue
            rec = self._compare(r, fresh, missing)
            report["compared"] += 1
            report["actions"].append({"round": r["id"], "action": "compared", "approval_id": rec.id, "quotes": len(fresh)})
        return report

    def comparison(self, r: dict[str, Any], fresh: list[Any]) -> tuple[Any, Any]:
        item = next((i for i in self.app.erp.list_items() if i.code == r["item_code"]), None)
        cover = lead = None
        try:
            a, _ = self.app.inventory.analyze_one(r["item_code"])
            cover, lead = a.cover_days_on_hand, a.lead_time_days
        except Exception as exc:  # noqa: BLE001 - the comparison works without the stock figures (delivery is then judged without urgency)
            log.warning("comparing the quotations of %s without its stock figures: %s", r["item_code"], untrusted(str(exc), 120))
        cfg = self.app.settings.procurement().model_copy(update={"use_quotations": "only", "exclude_new_vendors": False})
        taxes = self.app.procurement._tax_setups(fresh, r["item_code"], cfg)
        return item, rank_vendors(fresh, Need(r["qty"], cover, float(lead or 7), r["uom"]), cfg, taxes)

    def _compare(self, r: dict[str, Any], fresh: list[Any], missing: list[str]) -> ApprovalRecord:
        item, ranking = self.comparison(r, fresh)
        req = build_quotes_request(r["id"], item, r["qty"], ranking, r["suppliers"], missing, date.fromisoformat(r["reply_by"]))
        with self.app.db.transaction():
            record = self.app.approvals.submit(req, deliver=False)
            self._set(r["id"], status="compared", comparison_id=record.id)
        self.app.approvals.deliver(record)
        return record

    # ---------------------------------------------------------------- 4. order
    def _on_comparison_decision(self, record: ApprovalRecord, decision: Decision) -> None:
        rid = record.request.facts.get("round")
        self._set(rid, status="ordered" if decision.action == "approve" else "closed", status_note=f"{decision.action} by {decision.actor_id}")

    def _after_decided(self, record: ApprovalRecord) -> None:
        """After 'Prepare order' is recorded (and the person has had their reply): the usual purchase request, with the chosen vendor at its quoted price."""
        if record.kind != KIND_QUOTES or not record.decision or record.decision.action != "approve":
            return
        f = record.request.facts
        try:
            a, _ = self.app.inventory.analyze_one(f["item_code"])
        except Exception:  # noqa: BLE001
            log.exception("could not analyse %s for the order from quote round %s", f.get("item_code"), f.get("round"))
            return
        who = f"{record.decision.actor_id} via {record.decision.channel}"
        payload = {**on_demand_payload(a, who, f["qty"], f"Chosen from the quotations of round RFQ-{f['round']}."), "prefer_supplier": f.get("supplier"), "use_quotes": True}
        res = self.app.procurement.request_purchase(payload, who)
        if res.get("status") not in ("created", "refreshed"):
            self.app.approvals.tell_recipients(record.id, f"The purchase request for {f['item_code']} was not made ({res.get('status')}: "
                                                          f"{res.get('why') or res.get('problem') or 'a request for it is already waiting'}).")

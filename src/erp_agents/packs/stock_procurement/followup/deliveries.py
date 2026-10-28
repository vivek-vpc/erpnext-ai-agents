"""Delivery chasing: watch open purchase orders, and when one is late ask a person whether to remind the vendor, with what the delay does to stock cover.

Once a day (and on demand) the open, submitted purchase orders are read from the ERP. An order with quantity still to come more than `chase_grace_days` past its due
date is late. For each late order the person is asked once (again after `chase_every_days` while it stays late): the request says how long stock on hand lasts
without the delivery and with it, and shows the reminder that Send reminder would send. Nothing is sent to a vendor without that press, and never more than
`chase_max` reminders per order. When the goods arrive (or the order is closed) the chase ends by itself and any open request is withdrawn.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Optional

from erp_agents.core.approvals.models import ApprovalRecord, Decision, DecisionRefused
from erp_agents.core.approvals.service import KindHandler
from erp_agents.core.common import iso, parse_dt, untrusted
from .messages import KIND_CHASE, build_chase_request, fill
from .vendors import VendorMessenger

log = logging.getLogger(__name__)
FINISHED = ("Completed", "Closed", "Cancelled", "Delivered", "Received", "On Hold")


def _date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class DeliveryChaser:
    def __init__(self, app: Any, messenger: VendorMessenger) -> None:
        self.app, self.messenger = app, messenger
        app.approvals.register_handler(KIND_CHASE, KindHandler(on_decision=self._on_decision, outcome_detail=self._outcome_detail))

    @property
    def cfg(self) -> Any:
        return self.app.settings.followup()

    def today(self) -> date:
        return self.app.inventory.today()

    # ---------------------------------------------------------------- reading the ERP
    def open_orders(self) -> list[dict[str, Any]]:
        """Submitted purchase orders with quantity still to come, as plain rows."""
        out = []
        for d in self.app.erp.get_documents("Purchase Order", limit=2000, open_only=True):          # the ERP leaves finished orders out, so an old open one is never lost behind newer ones
            if int(d.get("docstatus") or 0) != 1 or (d.get("status") or "") in FINISHED:
                continue
            lines = [{"item_code": l.get("item_code"), "item_name": l.get("item_name") or l.get("item_code"), "uom": l.get("uom") or "units", "qty": float(l.get("qty") or 0),
                      "received": float(l.get("done_qty") or 0), "still": max(float(l.get("qty") or 0) - float(l.get("done_qty") or 0), 0.0)} for l in d.get("lines") or []]
            lines = [l for l in lines if l["still"] > 0 and l["item_code"]]
            if not lines:
                continue
            out.append({"document": d["name"], "vendor": d.get("party") or d.get("party_id") or "?", "supplier": d.get("party_id") or d.get("party") or "", "due": d.get("due"),
                        "date": d.get("date"), "state": d.get("status"), "lines": lines})
        return out

    def late_orders(self) -> list[dict[str, Any]]:
        today, grace = self.today(), self.cfg.chase_grace_days
        late = []
        for o in self.open_orders():
            due = _date(o["due"])
            if due is None or (today - due).days <= grace:
                continue
            late.append({**o, "days_late": (today - due).days})
        return sorted(late, key=lambda o: -o["days_late"])

    def effects(self, order: dict[str, Any]) -> list[dict[str, Any]]:
        """For each item still to come: how long stock on hand lasts without the delivery, and with it."""
        out = []
        for l in order["lines"]:
            e = {"item_code": l["item_code"], "item_name": l["item_name"], "uom": l["uom"], "still": l["still"]}
            try:
                a, _ = self.app.inventory.analyze_one(l["item_code"])
            except Exception as exc:  # noqa: BLE001 - an item the analysis cannot read is reported as such, the chase still goes on
                out.append({**e, "error": untrusted(str(exc), 120), "available": 0.0, "daily_use": None})
                continue
            rate = a.usage.planning_rate if a.usage else 0.0
            e.update(uom=a.uom, item_name=a.item_name, available=a.available, daily_use=rate or None, lead_days=a.lead_time_days,
                     cover_days=a.cover_days_on_hand if a.cover_days_on_hand is not None else None, runs_out_on=a.stockout_date,
                     cover_with=(max(a.available, 0.0) + l["still"]) / rate if rate else None)
            out.append(e)
        return out

    def describe(self) -> list[dict[str, Any]]:
        """The late orders with their effect on stock, for the tool and the API (no contact details)."""
        rows = []
        for o in self.late_orders():
            chase = self._chase(o["document"])
            rows.append({"document": o["document"], "vendor": o["vendor"], "due": o["due"], "days_late": o["days_late"],
                         "reminders_sent": chase["sends"] if chase else 0, "chasing": chase["status"] if chase else "not yet asked",
                         "items": [{k: e.get(k) for k in ("item_code", "item_name", "uom", "still", "available", "daily_use", "cover_days", "runs_out_on", "cover_with", "error")}
                                   for e in self.effects(o)]})
        return rows

    # ---------------------------------------------------------------- the daily pass
    def run(self, dry_run: bool = False) -> dict[str, Any]:
        report: dict[str, Any] = {"late": 0, "asked": 0, "skipped": 0, "arrived": 0, "actions": []}
        if not self.cfg.chase_enabled:
            return {**report, "note": "delivery chasing is switched off"}
        now, today = self.app.clock(), self.today()
        late = self.late_orders()
        report["late"] = len(late)
        late_names = {o["document"] for o in late}
        if not dry_run:
            report["arrived"] = self._close_finished(late_names)
        for o in late:
            chase = self._chase(o["document"])
            why = self._skip_reason(chase, now)
            if why:
                report["skipped"] += 1
                report["actions"].append({"po": o["document"], "action": f"skipped:{why}"})
                continue
            if dry_run:
                report["actions"].append({"po": o["document"], "action": "would_ask", "days_late": o["days_late"]})
                continue
            try:
                record = self._ask(o, chase, today)
            except Exception as exc:  # noqa: BLE001 - one order must not stop the others
                log.exception("could not prepare the chase for %s", o["document"])
                report["actions"].append({"po": o["document"], "action": "error", "error": untrusted(str(exc), 160)})
                continue
            report["asked"] += 1
            report["actions"].append({"po": o["document"], "action": "asked", "approval_id": record.id})
        return report

    def _skip_reason(self, chase: Optional[dict[str, Any]], now: Any) -> str:
        if chase is None:
            return ""
        if chase["status"] == "dismissed":
            return "stopped_by_a_person"
        if chase["approval_id"]:
            rec = self.app.approvals.get(chase["approval_id"])
            if rec is not None and rec.status == "pending":
                return "awaiting_answer"
        asked = parse_dt(chase["asked_at"]) if chase["asked_at"] else None
        if asked is not None and now - asked < timedelta(days=self.cfg.chase_every_days):
            return "asked_recently"
        return ""

    def _ask(self, order: dict[str, Any], chase: Optional[dict[str, Any]], today: date) -> ApprovalRecord:
        cfg = self.cfg
        sends = chase["sends"] if chase else 0
        message = self.message_for(order)
        route = self.messenger.route(order["supplier"])
        req = build_chase_request(order, self.effects(order), message, route, sends, cfg)
        now = iso(self.app.clock())
        with self.app.db.transaction():
            record = self.app.approvals.submit(req, deliver=False)
            if chase is None:
                self.app.db.execute("INSERT INTO delivery_chases(po_name, supplier, item_code, status, first_late_on, asked_at, asks, approval_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                    (order["document"], order["supplier"], order["lines"][0]["item_code"], "open", today.isoformat(), now, 1, record.id, now))
            else:
                self.app.db.execute("UPDATE delivery_chases SET status='open', asked_at=?, asks=asks+1, approval_id=?, updated_at=? WHERE po_name=?", (now, record.id, now, order["document"]))
        self.app.approvals.deliver(record)
        return record

    def message_for(self, order: dict[str, Any]) -> str:
        cfg = self.cfg
        first = order["lines"][0]
        return fill(cfg.chase_message, {"vendor": order["vendor"], "order": order["document"], "item": first["item_name"] if len(order["lines"]) == 1 else f"{len(order['lines'])} items",
                                        "item_code": first["item_code"], "qty": f"{first['qty']:g}", "still": f"{first['still']:g}", "uom": first["uom"],
                                        "due": str(order["due"])[:10], "days_late": order["days_late"], "signature": cfg.signature})

    def _close_finished(self, late_names: set[str]) -> int:
        """Chases whose order is no longer late and open: the goods came, or the order was closed. Their open request is withdrawn."""
        closed = 0
        open_names = {o["document"] for o in self.open_orders()}
        for row in self.app.db.query("SELECT po_name, approval_id FROM delivery_chases WHERE status='open'"):
            if row["po_name"] in late_names:
                continue
            still_open = row["po_name"] in open_names
            if still_open:                                   # open but no longer late (a new due date): keep watching, nothing to withdraw unless it is pending
                continue
            self.app.db.execute("UPDATE delivery_chases SET status='arrived', updated_at=? WHERE po_name=?", (iso(self.app.clock()), row["po_name"]))
            if row["approval_id"]:
                self.app.approvals.cancel(row["approval_id"], "the delivery has arrived (or the order was closed)")
            closed += 1
        for ref in {r["ref_id"] for r in self.app.db.query("SELECT DISTINCT ref_id FROM vendor_messages WHERE purpose='chase' AND closed_at IS NULL")}:
            if ref not in open_names:                        # the goods came or the order was closed, even if a person had stopped the chase: a later message is not a reply
                self.messenger.close_replies("chase", ref)
        return closed

    def _chase(self, po: str) -> Optional[dict[str, Any]]:
        row = self.app.db.query_one("SELECT * FROM delivery_chases WHERE po_name=?", (po,))
        return dict(row) if row else None

    # ---------------------------------------------------------------- the person's answer
    def _on_decision(self, record: ApprovalRecord, decision: Decision) -> None:
        po = record.request.facts.get("po")
        now = iso(self.app.clock())
        if decision.action == "dismiss":
            self.app.db.execute("UPDATE delivery_chases SET status='dismissed', updated_at=? WHERE po_name=?", (now, po))
            return
        if decision.action != "send":
            return                                           # "later": asked again after chase_every_days if still late
        order = next((o for o in self.late_orders() if o["document"] == po), None)
        if order is None:
            raise DecisionRefused(f"Purchase order {po} is no longer late (it arrived, was closed or has a new due date), so no reminder was sent.")
        chase = self._chase(po)
        if chase and chase["sends"] >= self.cfg.chase_max:
            raise DecisionRefused(f"The vendor has already had {chase['sends']} reminders about {po}, the most your settings send.")
        out = self.messenger.send("chase", po, order["supplier"], f"Late delivery: purchase order {po}", self.message_for(order), "Purchase Order", po, record.id,
                                  f"{decision.actor_id} via {decision.channel}")
        if not out.ok:
            raise DecisionRefused(f"The reminder could not be sent ({out.note}).")
        self.app.db.execute("UPDATE delivery_chases SET sent_at=?, sends=sends+1, updated_at=? WHERE po_name=?", (now, now, po))

    def _outcome_detail(self, record: ApprovalRecord) -> str:
        if not record.decision or record.decision.action != "send":
            return ""
        row = self.app.db.query_one("SELECT channel FROM vendor_messages WHERE approval_id=? AND status='sent' ORDER BY sent_at DESC LIMIT 1", (record.id,))
        if row is None:
            return ""
        return "The reminder was sent " + ("by e-mail through ERPNext." if row["channel"] == "email" else "by WhatsApp.") + " A reply on WhatsApp is passed on to you here."

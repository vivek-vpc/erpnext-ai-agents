"""Moving stock from one warehouse to another.

  1. prepare   a person (in the console, in the chat, or an AI agent on their behalf through the prepare_stock_transfer tool) asks to move some quantity of an item from one
               warehouse to another. The request is checked against what ERPNext holds in those two warehouses (never more than the source has) and sent for approval,
               showing the stock before and after. NOTHING is created in the ERP until a person presses Create transfer.
  2. create    Create transfer makes a DRAFT Stock Entry of type Material Transfer in ERPNext, with the approver recorded on it (under the approver's own ERPNext login when the
               answer comes from their signed-in console session). The stock moves only when someone opens the draft in ERPNext and submits it: the same rule as a purchase order.
"""
from __future__ import annotations

import difflib
import logging
import math
from typing import Any, Optional

from erp_agents.core import acting
from erp_agents.core.approvals.models import ApprovalRecord, Decision, DecisionRefused
from erp_agents.core.approvals.service import KindHandler, parse_approver
from erp_agents.core.common import iso, short_id, untrusted
from erp_agents.core.erp.base import StockTransferDraft
from .analysis import _n, where_stock_is
from .messages import KIND_TRANSFER, build_transfer_request

log = logging.getLogger(__name__)
OPEN = ("proposed",)
MAX_QTY = 1_000_000_000


class StockTransfers:
    def __init__(self, app: Any) -> None:
        self.app = app
        app.approvals.register_handler(KIND_TRANSFER, KindHandler(on_decision=self._on_decision, on_expired=self._closed("expired", "no answer before the request expired"),
                                                                 on_cancelled=self._closed("cancelled", None), outcome_detail=self._outcome_detail,
                                                                 reconcile=self._reconcile))

    # ---------------------------------------------------------------- records
    def get(self, transfer_id: str) -> Optional[dict[str, Any]]:
        row = self.app.db.query_one("SELECT * FROM stock_transfers WHERE id=?", (transfer_id,))
        return dict(row) if row else None

    def by_approval(self, approval_id: str) -> Optional[dict[str, Any]]:
        row = self.app.db.query_one("SELECT * FROM stock_transfers WHERE approval_id=?", (approval_id,))
        return dict(row) if row else None

    def list(self, status: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        if status:
            rows = self.app.db.query("SELECT * FROM stock_transfers WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = self.app.db.query("SELECT * FROM stock_transfers ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def _set(self, transfer_id: str, **fields: Any) -> None:
        fields["updated_at"] = iso(self.app.clock())
        cols = ", ".join(f"{k}=?" for k in fields)
        self.app.db.execute(f"UPDATE stock_transfers SET {cols} WHERE id=?", (*fields.values(), transfer_id))  # column names come from this module, values are bound  # nosec B608

    # ---------------------------------------------------------------- 1. prepare
    def warehouse(self, text: str, known: list[str]) -> tuple[Optional[str], str]:
        """(the warehouse named by `text`, "") or (None, why not). The exact name, or the same ignoring case and the company suffix ("Stores" for "Stores - TTC") when that
        names only one; otherwise the person is told what was close, never guessed at."""
        want = " ".join(str(text or "").split()).lower()
        if not want:
            return None, "no warehouse was given"
        exact = [w for w in known if w.lower() == want]
        if exact:
            return exact[0], ""
        short = [w for w in known if w.lower().split(" - ")[0] == want]
        if len(short) == 1:
            return short[0], ""
        close = short or difflib.get_close_matches(text, known, n=3, cutoff=0.5) or [w for w in known if want in w.lower()][:3]
        return None, f"no warehouse '{untrusted(text, 80)}'" + (f"; did you mean {', '.join(close[:3])}?" if close else "")

    def prepare(self, item_code: str, qty: float, source: str, destination: str, requested_by: str, reason: str = "") -> dict[str, Any]:
        """Prepare the request for a transfer. Creates nothing in the ERP and moves nothing."""
        erp = self.app.erp
        if getattr(erp, "create_stock_transfer", None) is None:
            return {"status": "refused", "error": "this ERP connection cannot create stock transfers"}
        item = next((i for i in erp.list_items() if i.code == item_code), None)
        if item is None:
            return {"status": "refused", "error": f"no item {untrusted(item_code, 60)}"}
        if not isinstance(qty, (int, float)) or isinstance(qty, bool) or not math.isfinite(qty) or not 0 < qty <= MAX_QTY:
            return {"status": "refused", "error": "the quantity must be more than zero"}
        known = erp.list_warehouses()
        src, why = self.warehouse(source, known)
        if src is None:
            return {"status": "refused", "error": f"the warehouse to take it from: {why}"}
        dst, why = self.warehouse(destination, known)
        if dst is None:
            return {"status": "refused", "error": f"the warehouse to move it to: {why}"}
        if src == dst:
            return {"status": "refused", "error": "the two warehouses are the same"}
        stock = erp.get_stock([item.code], [src, dst]).get(item.code)
        held = {w.warehouse: w for w in (stock.by_warehouse if stock else ())}
        have, there = held.get(src), held.get(dst)
        if have is None or have.on_hand <= 0:
            return {"status": "refused", "error": f"{src} holds none of {item.code} in ERPNext, so there is nothing to move"}
        if qty > have.on_hand + 1e-9:
            return {"status": "refused", "error": f"{src} holds only {_n(have.on_hand)} {item.uom} of {item.code}, so {_n(qty)} cannot be moved from it"}
        for t in self.list("proposed", limit=100):
            if t["item_code"] == item.code and t["from_warehouse"] == src and t["to_warehouse"] == dst:
                other = f" It is for {_n(t['qty'])} {t['uom']}; to move a different quantity, press Don't move it on it and ask again." if abs(float(t["qty"]) - float(qty)) > 1e-9 else ""
                return {"status": "waiting", "transfer": t["id"], "approval_id": t["approval_id"], "qty": float(t["qty"]),
                        "note": "A request to move this item between these warehouses is already waiting for a person's answer; no second one was made." + other}
        where = []
        try:
            a, _src = self.app.inventory.analyze_one(item.code)
            where = where_stock_is(a)
        except Exception as exc:  # noqa: BLE001 - the request is still worth sending without the overall picture
            log.warning("showing the transfer of %s without the stock picture: %s", item.code, untrusted(str(exc), 120))
        tid, now = short_id(8), iso(self.app.clock())
        req = build_transfer_request(tid, item, float(qty), src, dst, {"on_hand": have.on_hand, "reserved": have.reserved},
                                     {"on_hand": there.on_hand if there else 0.0, "reserved": there.reserved if there else 0.0}, where, untrusted(reason, 300))
        with self.app.db.transaction():
            self.app.db.execute("INSERT INTO stock_transfers(id, item_code, qty, uom, from_warehouse, to_warehouse, status, reason, requested_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (tid, item.code, float(qty), item.uom, src, dst, "proposed", untrusted(reason, 300) or None, requested_by, now, now))
            record = self.app.approvals.submit(req, deliver=False)
            self._set(tid, approval_id=record.id)
        self.app.approvals.deliver(record)
        return {"status": "created", "transfer": tid, "approval_id": record.id, "item_code": item.code, "qty": float(qty), "from_warehouse": src, "to_warehouse": dst,
                "note": "The request was sent for approval. Nothing is created in ERPNext until a person presses Create transfer, and no stock moves until the draft is submitted there."}

    # ---------------------------------------------------------------- 2. create
    def _on_decision(self, record: ApprovalRecord, decision: Decision) -> None:
        t = self.by_approval(record.id)
        if t is None:
            raise DecisionRefused("This transfer request no longer exists.")
        who = f"{decision.actor_id} via {decision.channel}"
        if decision.action == "dismiss":
            self._set(t["id"], status="rejected", status_note=f"rejected by {who}")
            return
        if decision.action != "approve":
            return
        if t["entry_name"]:                                          # already made (a retry after a later failure)
            self._set(t["id"], status="created", status_note=f"approved by {who}; draft {t['entry_name']}")
            return
        recovered = decision.params.get("recovered_document")        # finishing an answer after a restart: the draft is already in the ERP
        if recovered:
            self._set(t["id"], status="created", entry_name=str(recovered), status_note=f"approved by {who}; draft {recovered} (found in ERPNext after a restart, where it had been made before the answer was written down)")
            return
        make = getattr(self.app.erp, "create_stock_transfer", None)
        if make is None:
            raise DecisionRefused("This ERP connection cannot create stock transfers.")
        who_acting = self._acting_for(decision.actor_id)
        draft = StockTransferDraft(item_code=t["item_code"], qty=float(t["qty"]), uom=t["uom"], from_warehouse=t["from_warehouse"], to_warehouse=t["to_warehouse"],
                                   remarks=f"Requested by {t['requested_by']}; approved by {who}. " + (f"Why: {t['reason']} " if t.get("reason") else "") + "Created by the Stock Assistant: check it before submitting.",
                                   reference=t["id"], approved_by=who)
        result = make(draft, acting=who_acting)
        if not result.ok:
            raise RuntimeError(f"the ERP did not accept the stock transfer: {result.error}")      # rolls the decision back: the person can press the button again
        by = f"created under {who_acting.user}'s own ERPNext login" if who_acting else "created by the service account, with the approver's name on it"
        self._set(t["id"], status="created", entry_name=result.name, status_note=f"approved by {who}; draft {result.name} ({by})")

    def _reconcile(self, record: ApprovalRecord) -> Optional[Decision]:
        """Was the draft stock transfer already made in the ERP, though the answer was never written down? See ProcurementAgent._reconcile."""
        find = getattr(self.app.erp, "find_by_reference", None)
        t = self.by_approval(record.id)
        if find is None or t is None or t["status"] not in OPEN:
            return None
        found = find("Stock Entry", t["id"])
        if found is None:
            return None
        who = parse_approver(found.approved_by)
        if who is None:
            self.app.approvals.warn_once(f"approver:{record.id}", "request %s: %s exists in the ERP, but the approver written on it cannot be read, so the answer was not recorded. "
                                         "Answer the request again (it will find %s) or cancel it.", record.id, found.name, found.name)
            return None
        return Decision(approval_id=record.id, action="approve", actor_id=who[0], channel=who[1], params={"recovered_document": found.name}, decided_at=self.app.clock(),
                        note="finished after a restart: the draft already existed in ERPNext")

    def _acting_for(self, actor_id: str) -> Optional[acting.ActingAs]:
        """The approver's own ERPNext session when the answer came from their signed-in console session (ERPNext then records THEM and checks THEIR permissions), else None:
        the service account creates it and the approver is written on it. The owner can require the personal login (the same switch as for purchase orders)."""
        a = acting.current()
        if a is not None and a.user == actor_id:
            return a
        if self.app.settings.procurement().require_personal_login:
            raise RuntimeError("your settings only allow documents to be created under the approver's own ERPNext name, and this approval did not come from a signed-in console "
                               "session (for example it came from WhatsApp). Please approve it in the console")
        return None

    def _outcome_detail(self, record: ApprovalRecord) -> str:
        t = self.by_approval(record.id)
        if t is None or not record.decision or record.decision.action != "approve" or not t.get("entry_name"):
            return ""
        return (f"Draft stock transfer {t['entry_name']} was created in ERPNext: {_n(t['qty'])} {t['uom']} of {t['item_code']} from {t['from_warehouse']} to {t['to_warehouse']}. "
                "It is only a draft: open it in ERPNext, check it and submit it. No stock has moved yet.")

    def _closed(self, status: str, note: Optional[str]) -> Any:
        def handler(record: ApprovalRecord) -> None:
            t = self.by_approval(record.id)
            if t and t["status"] in OPEN:
                self._set(t["id"], status=status, status_note=note or record.status_note or status)
        return handler

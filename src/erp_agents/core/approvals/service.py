"""Channel-agnostic approval workflow.

Lifecycle: submit -> (delivered on N channels) -> decide | expire | cancel.
  * Exactly one terminal decision is ever recorded, no matter how many channels/buttons race.
  * Nothing is ever auto-approved: unanswered requests are reminded, then expire.
  * Domain effects (create work order, snooze alert...) run in a handler *inside the same DB
    transaction* as the decision, so they succeed or fail together.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Optional

from .. import obs
from ..common import Actor, Clock, iso, parse_dt, short_id, utcnow
from ..policy import SystemSettings
from ..storage import Database, dumps, loads
from .models import ACTION_DETAILS, ApprovalRecord, ApprovalRequest, Decision, DecisionRefused, DecisionResult
from .router import ApprovalRouter

log = logging.getLogger(__name__)
MAX_DELIVERY_ATTEMPTS = 5


@dataclass
class KindHandler:
    on_decision: Optional[Callable[[ApprovalRecord, Decision], None]] = None   # runs inside the decision transaction
    on_expired: Optional[Callable[[ApprovalRecord], None]] = None
    on_cancelled: Optional[Callable[[ApprovalRecord], None]] = None
    # a non-terminal action that is not "details" (for example "change the quantity"): may answer with a DecisionResult; None = not mine
    on_action: Optional[Callable[[ApprovalRecord, str, Actor, str, dict[str, Any]], Optional[DecisionResult]]] = None
    # what came of an answered request, in plain words (for example "Draft purchase order PUR-ORD-... was created"): added to the outcome message every channel shows
    outcome_detail: Optional[Callable[[ApprovalRecord], str]] = None


class ApprovalService:
    def __init__(
        self,
        db: Database,
        router: ApprovalRouter,
        system_settings: Callable[[], SystemSettings] = SystemSettings,
        clock: Clock = utcnow,
    ) -> None:
        self.db = db
        self.router = router
        self.system_settings = system_settings
        self.clock = clock
        self._handlers: dict[str, KindHandler] = {}
        self.no_reminder_kinds: set[str] = set()         # kinds that are information only: nobody is reminded about them (a pack adds its own)
        self._local = threading.local()                  # per thread: work to do after the reply to a decision (see decide(defer=True))
        self.on_submitted: list[Callable[[ApprovalRecord], None]] = []       # told after a new request has been sent out; must not block (the AI second opinion starts its own thread)
        self.on_decided: list[Callable[[ApprovalRecord], None]] = []         # told after a request was answered and the answer applied: run after the person's reply (see decide(defer=True))
        # a message from someone who is not a registered person (for example a vendor answering a pack's message): each handler gets (event, channel) and returns
        # True when it took the message; it must treat the text as untrusted data and never act on it. None take it = the sender is told they are not registered.
        self.unknown_sender_handlers: list[Callable[[Any, Any], bool]] = []
        self.on_closed: list[Callable[[], None]] = []                       # told after any request was answered, expired, cancelled or replaced (the audit seal)

    def register_handler(self, kind: str, handler: KindHandler) -> None:
        self._handlers[kind] = handler

    # ---------------------------------------------------------------- create + deliver
    def submit(self, request: ApprovalRequest, expiry_hours: Optional[int] = None, deliver: bool = True) -> ApprovalRecord:
        now = self.clock()
        hours = expiry_hours or self.system_settings().approval_expiry_hours
        record = ApprovalRecord(
            id=short_id(10), kind=request.kind, status="pending", request=request,
            created_at=now, expires_at=now + timedelta(hours=hours),
        )
        self.db.execute(
            "INSERT INTO approvals(id, kind, status, request_json, ref_type, ref_id, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (record.id, record.kind, "pending", dumps(request.model_dump()), request.ref_type, request.ref_id, iso(now), iso(record.expires_at)),
        )
        if deliver:
            self.deliver(record)
            for listener in self.on_submitted:
                try:
                    listener(record)
                except Exception:  # noqa: BLE001 - a listener must never undo a request that has been sent
                    log.exception("a listener of new requests failed")
        return record

    def tell_recipients(self, approval_id: str, text: str) -> int:
        """Send a plain message to everyone who was sent this request, on the channel they got it on. Returns how many it reached."""
        reached = 0
        seen: set[tuple[str, str]] = set()
        for d in self.db.query("SELECT channel, address FROM deliveries WHERE approval_id=? AND status='sent' ORDER BY id", (approval_id,)):
            key = (d["channel"], d["address"] or "")
            ch = self.router.channels.get(d["channel"])
            if ch is None or not d["address"] or key in seen:
                continue
            seen.add(key)
            try:
                reached += int(ch.send_text(d["address"], text).ok)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not send a message about request %s on %s: %s", approval_id, d["channel"], exc)
        return reached

    def deliver(self, record: ApprovalRecord) -> None:
        for recipient, chans in self.router.targets():
            for rc in chans:
                res = self.router.channels[rc.type].send_approval(record, rc.address)
                self._log_delivery(record.id, recipient.id, rc.type, rc.address, res.ok, res.external_id, res.error)
                if res.ok and self.router.strategy == "failover":
                    break
                if not res.ok:
                    log.warning("delivery of %s via %s to %s failed: %s", record.id, rc.type, recipient.id, res.error)

    def resend(self, record: ApprovalRecord, recipient_id: str, channel: str, address: Optional[str]) -> bool:
        """Put a still-pending request in front of one person again, on the channel they are using. The new message is recorded like the first, so replying to it
        works and it is tidied up when the request is answered."""
        ch = self.router.channels.get(channel)
        if ch is None or record.status != "pending":
            return False
        res = ch.send_approval(record, address)
        self._log_delivery(record.id, recipient_id, channel, address, res.ok, res.external_id, res.error)
        return res.ok

    def _log_delivery(self, approval_id: str, recipient_id: str, channel: str, address: Optional[str],
                      ok: bool, external_id: Optional[str], error: Optional[str]) -> None:
        self.db.execute(
            "INSERT INTO deliveries(approval_id, recipient_id, channel, address, status, external_id, error, sent_at) VALUES (?,?,?,?,?,?,?,?)",
            (approval_id, recipient_id, channel, address, "sent" if ok else "failed", external_id, obs.scrub(error) if error else error, iso(self.clock())),      # a network error can quote a token or a number
        )

    def retry_failed_deliveries(self) -> int:
        """Re-send failed deliveries for still-pending requests (flaky network, API hiccup)."""
        rows = self.db.query(
            "SELECT d.* FROM deliveries d JOIN approvals a ON a.id=d.approval_id "
            "WHERE d.status='failed' AND a.status='pending' AND d.attempts < ?", (MAX_DELIVERY_ATTEMPTS,),
        )
        fixed = 0
        for d in rows:
            rec = self.get(d["approval_id"])
            channel = self.router.channels.get(d["channel"])
            if not rec or not channel:
                continue
            res = channel.send_approval(rec, d["address"])
            attempts = d["attempts"] + 1
            status = "sent" if res.ok else ("given_up" if attempts >= MAX_DELIVERY_ATTEMPTS else "failed")
            self.db.execute("UPDATE deliveries SET status=?, external_id=?, error=?, attempts=?, sent_at=? WHERE id=?",
                            (status, res.external_id, obs.scrub(res.error) if res.error else res.error, attempts, iso(self.clock()), d["id"]))
            fixed += int(res.ok)
        return fixed

    # ---------------------------------------------------------------- read
    def get(self, approval_id: str) -> Optional[ApprovalRecord]:
        row = self.db.query_one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        return _row_to_record(row) if row else None

    def approval_for_message(self, channel: str, external_id: str, address: str) -> Optional[str]:
        """The approval a chat message was about: the request that was sent as message `external_id` on `channel` to `address` (its most recent delivery)."""
        row = self.db.query_one("SELECT approval_id FROM deliveries WHERE channel=? AND external_id=? AND address=? ORDER BY id DESC LIMIT 1",
                                (channel, str(external_id), str(address)))
        return row["approval_id"] if row else None

    def list(self, status: Optional[str] = None, limit: int = 50) -> list[ApprovalRecord]:
        if status:
            rows = self.db.query("SELECT * FROM approvals WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = self.db.query("SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,))
        return [_row_to_record(r) for r in rows]

    def metrics_snapshot(self) -> dict[str, Any]:
        """What the monitoring reads (core/metrics.py): requests waiting and since when, closed requests by kind and how they ended, messages by channel and status."""
        row = self.db.query_one("SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM approvals WHERE status='pending'")
        try:
            decisions = self.db.query("SELECT kind, COALESCE(NULLIF(json_extract(decision_json, '$.action'), ''), status) AS outcome, COUNT(*) AS n "
                                      "FROM approvals WHERE status != 'pending' GROUP BY kind, outcome")
        except sqlite3.OperationalError:                         # a SQLite built without its JSON functions: how they ended, without which answer was given
            decisions = self.db.query("SELECT kind, status AS outcome, COUNT(*) AS n FROM approvals WHERE status != 'pending' GROUP BY kind, status")
        deliveries = self.db.query("SELECT channel, status, COUNT(*) AS n FROM deliveries GROUP BY channel, status")
        return {"pending": int(row["n"]), "oldest_pending": parse_dt(row["oldest"]), "decisions": [dict(r) for r in decisions], "deliveries": [dict(r) for r in deliveries]}

    # ---------------------------------------------------------------- decide
    def decide(self, approval_id: str, action: str, actor: Actor, channel: str,
               params: Optional[dict[str, Any]] = None, note: str = "", address: Optional[str] = None, defer: bool = False) -> DecisionResult:
        """Answer a request. With defer=True the caller sends its own reply (the "Done: ..." message) and then runs `result.after()`: a revised request that replaces this one
        is delivered by that, so it arrives AFTER the reply that says it is on its way, and is the last message in the chat, with its buttons, where the person is looking."""
        if not defer:
            return self._decide(approval_id, action, actor, channel, params, note, address)
        self._local.pending = []
        try:
            result = self._decide(approval_id, action, actor, channel, params, note, address)
        finally:
            pending, self._local.pending = self._local.pending, None
        if pending:
            ran: list[bool] = []

            def run_after() -> None:
                if ran:                                       # once only: a second call must not send the revised request twice
                    return
                ran.append(True)
                for step in pending:
                    try:
                        step()
                    except Exception:  # noqa: BLE001 - the answer is already recorded; a failed send is logged and retried by the delivery housekeeping
                        log.exception("could not send what followed an answer")
            result.after = run_after
        return result

    def _later(self, step: Callable[[], None]) -> None:
        """Do `step` now, or after the reply when the caller asked for that."""
        pending = getattr(self._local, "pending", None)
        if pending is None:
            step()
        else:
            pending.append(step)

    def _decide(self, approval_id: str, action: str, actor: Actor, channel: str,
                params: Optional[dict[str, Any]], note: str, address: Optional[str]) -> DecisionResult:
        if not actor.has_role("approver"):
            return DecisionResult(ok=False, status="forbidden", message="You are not allowed to answer approval requests.")
        record = self.get(approval_id)
        if record is None:
            return DecisionResult(ok=False, status="not_found", message="That request no longer exists.")

        if record.status != "pending":
            return self._closed_message(record)
        if record.expires_at and self.clock() >= record.expires_at:
            self.expire_due()
            return DecisionResult(ok=False, status="expired", message="This request expired without an answer; a fresh one will be sent if the issue persists.",
                                  approval=self.get(approval_id))

        option = next((o for o in record.request.options if o.action == action), None)
        if option is None:
            return DecisionResult(ok=False, status="invalid_action", message=f"Unknown action {action!r}.", approval=record)

        if not option.terminal:   # e.g. "details": information only, request stays open
            handler = self._handlers.get(record.kind)
            if option.action != ACTION_DETAILS and handler and handler.on_action:
                try:
                    answered = handler.on_action(record, action, actor, channel, params or {})
                except Exception as exc:  # noqa: BLE001 - a failed change leaves the request exactly as it was
                    log.exception("action %s on %s failed", action, record.kind)
                    return DecisionResult(ok=False, status="error", message=f"Could not do that ({exc}). The request is unchanged.", approval=record)
                if answered is not None:
                    return answered
            sections = record.request.details or record.request.sections
            return DecisionResult(ok=True, status="info", message=record.request.title, approval=record, info_sections=sections)

        now = self.clock()
        decision = Decision(approval_id=approval_id, action=action, actor_id=actor.id, channel=channel,
                            params=params or {}, decided_at=now, note=note)
        try:
            with self.db.transaction():
                cur = self.db.execute(
                    "UPDATE approvals SET status='decided', decision_json=?, decided_at=? WHERE id=? AND status='pending'",
                    (dumps(decision.model_dump()), iso(now), approval_id),
                )
                if cur.rowcount != 1:                       # lost a race with another channel
                    raise _Lost()
                handler = self._handlers.get(record.kind)
                if handler and handler.on_decision:
                    handler.on_decision(self.get(approval_id), decision)   # may raise -> whole decision rolls back
        except _Lost:
            return self._closed_message(self.get(approval_id))
        except DecisionRefused as exc:
            return DecisionResult(ok=False, status="refused", message=f"{exc} Nothing was changed.", approval=record)
        except Exception as exc:  # noqa: BLE001
            log.exception("handler for %s failed", record.kind)
            return DecisionResult(ok=False, status="error", message=f"Could not apply your decision ({exc}). Nothing was changed - please try again.",
                                  approval=record)

        final = self.get(approval_id)
        text = self.outcome_text(final)
        self._notify_outcome(final, text)
        if self._outcome_detail(final):
            self._announce_outcome(final, text, channel, address)
        self._closed()
        for listener in self.on_decided:
            self._later(lambda listener=listener: self._tell_listener(listener, final))
        return DecisionResult(ok=True, status="decided", message=text, approval=final)

    def _closed(self) -> None:
        for listener in self.on_closed:
            try:
                listener()
            except Exception:  # noqa: BLE001 - sealing must never undo an answer; the maintenance job seals what was missed
                log.exception("a listener of closed requests failed")

    @staticmethod
    def _tell_listener(listener: Callable[[ApprovalRecord], None], record: ApprovalRecord) -> None:
        try:
            listener(record)
        except Exception:  # noqa: BLE001 - the answer is already recorded; a listener's failure must not undo it
            log.exception("a listener of answered requests failed")

    def _outcome_detail(self, record: ApprovalRecord) -> str:
        handler = self._handlers.get(record.kind)
        if record.status != "decided" or handler is None or handler.outcome_detail is None:
            return ""
        try:
            return (handler.outcome_detail(record) or "").strip()
        except Exception:  # noqa: BLE001 - the words are a courtesy: never fail an answered request over them
            log.exception("outcome detail of %s failed", record.kind)
            return ""

    def outcome_text(self, record: ApprovalRecord) -> str:
        if record.status == "decided" and record.decision:
            label = next((o.label for o in record.request.options if o.action == record.decision.action), record.decision.action)
            detail = self._outcome_detail(record)
            return f"Recorded: {label} (by {record.decision.actor_id} via {record.decision.channel})." + (f" {detail}" if detail else "")
        if record.status == "expired":
            return "This request expired without an answer."
        if record.status == "cancelled":
            return f"Cancelled: {record.status_note or 'no longer needed'}."
        return f"Status: {record.status}."

    def _closed_message(self, record: ApprovalRecord) -> DecisionResult:
        if record.status == "decided":
            text = self.outcome_text(record).replace("Recorded:", "Already answered:", 1)
            return DecisionResult(ok=False, status="already_decided", message=text, approval=record)
        return DecisionResult(ok=False, status=record.status, message=self.outcome_text(record), approval=record)

    # ---------------------------------------------------------------- lifecycle
    def supersede(self, old_id: str, request: ApprovalRequest, reason: str) -> Optional[ApprovalRecord]:
        """Replace a pending request by a revised one, in one step: the old one is closed (its kind's cancel handler is NOT run: nothing is being abandoned) and
        the new one is stored. Returns None, changing nothing, if the old one was answered or closed in the meantime. Call `finish_supersede` after the surrounding
        transaction has committed to send the new request and retire the old messages."""
        with self.db.transaction():
            cur = self.db.execute("UPDATE approvals SET status='cancelled', status_note=? WHERE id=? AND status='pending'", (reason, old_id))
            if cur.rowcount != 1:
                return None
            return self.submit(request, deliver=False)

    def finish_supersede(self, old_id: str, new: ApprovalRecord) -> None:
        old = self.get(old_id)
        self._closed()
        if old is not None:
            self._notify_outcome(old, self.outcome_text(old))            # the old request is marked cancelled first
        self._later(lambda: self.deliver(new))                           # then the new one is sent: after the person's reply if the caller asked for that

    def cancel(self, approval_id: str, reason: str) -> bool:
        with self.db.transaction():
            cur = self.db.execute("UPDATE approvals SET status='cancelled', status_note=? WHERE id=? AND status='pending'", (reason, approval_id))
            if cur.rowcount != 1:
                return False
        rec = self.get(approval_id)
        self._closed()
        h = self._handlers.get(rec.kind)
        if h and h.on_cancelled:
            h.on_cancelled(rec)
        self._notify_outcome(rec, self.outcome_text(rec))
        return True

    def expire_due(self) -> list[str]:
        now = self.clock()
        rows = self.db.query("SELECT id FROM approvals WHERE status='pending' AND expires_at IS NOT NULL AND expires_at <= ?", (iso(now),))
        expired = []
        for r in rows:
            with self.db.transaction():
                cur = self.db.execute("UPDATE approvals SET status='expired' WHERE id=? AND status='pending'", (r["id"],))
                if cur.rowcount != 1:
                    continue
            rec = self.get(r["id"])
            h = self._handlers.get(rec.kind)
            if h and h.on_expired:
                h.on_expired(rec)
            self._notify_outcome(rec, self.outcome_text(rec))
            expired.append(rec.id)
        if expired:
            self._closed()
        return expired

    def remind_due(self) -> list[str]:
        """One nudge per `reminder_after_hours`, up to `max_reminders`. Never approves anything."""
        cfg = self.system_settings()
        now = self.clock()
        reminded = []
        for rec in self.list(status="pending", limit=500):
            if rec.reminder_count >= cfg.max_reminders or rec.kind in self.no_reminder_kinds:
                continue
            last = rec.last_reminder_at or rec.created_at
            if now - last < timedelta(hours=cfg.reminder_after_hours):
                continue
            for recipient, chans in self.router.targets():
                for rc in chans:
                    res = self.router.channels[rc.type].send_reminder(rec, rc.address)
                    if res.ok and self.router.strategy == "failover":
                        break
            self.db.execute("UPDATE approvals SET reminder_count=reminder_count+1, last_reminder_at=? WHERE id=?", (iso(now), rec.id))
            reminded.append(rec.id)
        return reminded

    def _announce_outcome(self, record: ApprovalRecord, text: str, channel: str, address: Optional[str]) -> None:
        """Tell everyone who was sent the request what came of it, on the channels that do not post the outcome themselves (WhatsApp): Telegram already shows it under
        the request message, and the person who just answered gets it as the reply to their answer. WhatsApp delivers a plain message only while its 24-hour window is open."""
        seen: set[tuple[str, str]] = set()
        for d in self.db.query("SELECT channel, address FROM deliveries WHERE approval_id=? AND status='sent' ORDER BY id", (record.id,)):
            key = (d["channel"], d["address"] or "")
            ch = self.router.channels.get(d["channel"])
            if ch is None or not d["address"] or key in seen or ch.echoes_outcome or key == (channel, address or ""):
                continue
            seen.add(key)
            try:
                res = ch.send_text(d["address"], text)
                if not res.ok:
                    log.warning("could not tell %s what came of request %s: %s", d["channel"], record.id, res.error)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not tell %s what came of request %s: %s", d["channel"], record.id, exc)

    def _notify_outcome(self, record: ApprovalRecord, text: str) -> None:
        for d in self.db.query("SELECT * FROM deliveries WHERE approval_id=? AND status='sent'", (record.id,)):
            ch = self.router.channels.get(d["channel"])
            if ch:
                try:
                    ch.update_outcome(record, d["external_id"], d["address"], text)
                except Exception:  # noqa: BLE001
                    log.info("update_outcome failed on %s", d["channel"])


class _Lost(Exception):
    pass


def _row_to_record(row) -> ApprovalRecord:
    decision = loads(row["decision_json"])
    return ApprovalRecord(
        id=row["id"], kind=row["kind"], status=row["status"],
        request=ApprovalRequest.model_validate(loads(row["request_json"])),
        decision=Decision.model_validate(decision) if decision else None,
        created_at=parse_dt(row["created_at"]), expires_at=parse_dt(row["expires_at"]),
        decided_at=parse_dt(row["decided_at"]), last_reminder_at=parse_dt(row["last_reminder_at"]),
        reminder_count=row["reminder_count"], status_note=row["status_note"] or "",
    )

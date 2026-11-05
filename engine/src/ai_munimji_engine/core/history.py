""""Why did you do that?": every past decision with its reasoning, searchable, as an audit trail.

A decision here is one of:
  * a request the program raised (a stock alert, a reorder suggestion, a purchase request, a late-delivery reminder ...): what it said and WHY, exactly as the person saw
    it (the request's sections and details are stored with it and never rewritten), and how it ended: answered (which button, by whom, from which channel, when, and
    what came of it), expired, cancelled or replaced (with the reason);
  * a change of settings: who changed which value, from what, to what.
The AI agents' work on a request (a second opinion, a request an agent prepared) is listed with it. Packs add their own context to a request (`extras`: the stock pack adds
the messages sent to vendors). Every closed request and settings change is sealed in a hash chain (core/audit.py), so a later change shows.

Search: words (all must appear, in any field), the kind of request, the item, who answered, a date range, the outcome. Nothing here changes anything.
For a language model (`for_model=True`) the people are left out: who answered becomes "a person", and their names are taken out of the text.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from ai_munimji_engine.storage import loads

from .approvals.models import ApprovalRecord
from .approvals.service import _row_to_record

log = logging.getLogger(__name__)
MAX_LIMIT = 500
SCAN_LIMIT = 20000                 # most rows looked at for one search (a small business makes far fewer)
REASON_HEADINGS = ("why", "reason", "situation", "recommendation", "effect")          # sections that say why; a pack adds its own words (`reason_headings`)


def _day(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        raise ValueError(f"not a date: {value!r} (use YYYY-MM-DD)") from None


class DecisionHistory:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.extras: list[Callable[[ApprovalRecord], list[dict[str, Any]]]] = []      # a pack's context for one request: [{"heading": ..., "lines": [...]}]
        self.reason_headings: set[str] = set(REASON_HEADINGS)

    # ---------------------------------------------------------------- labels
    def kind_description(self, kind: str) -> str:
        for m in self.app.packs.manifests.values():
            for a in m.approvals:
                if a.kind == kind:
                    return a.description
        return kind

    @staticmethod
    def action_label(rec: ApprovalRecord) -> Optional[str]:
        if not rec.decision:
            return None
        return next((o.label for o in rec.request.options if o.action == rec.decision.action), rec.decision.action)

    def _detail(self, rec: ApprovalRecord) -> str:
        try:
            return self.app.approvals._outcome_detail(rec)
        except Exception:  # noqa: BLE001 - the words are a courtesy
            return ""

    def outcome(self, rec: ApprovalRecord, with_people: bool = True) -> str:
        if rec.status == "decided" and rec.decision:
            who = f" by {rec.decision.actor_id}" if with_people else " by a person"
            detail = self._detail(rec)
            return f"{self.action_label(rec)}{who} via {rec.decision.channel}." + (f" {detail}" if detail else "")
        if rec.status == "expired":
            return "Expired without an answer."
        if rec.status == "cancelled":
            return f"Closed: {rec.status_note or 'no longer needed'}."
        return "Waiting for an answer."

    def reasons(self, rec: ApprovalRecord, limit: int = 6) -> list[str]:
        """The lines that say why: from the sections whose heading is about the reason or the situation, in the order shown."""
        out = []
        for s in rec.request.sections:
            if any(h in s.heading.lower() for h in self.reason_headings):
                out += s.lines
        return (out or [l for s in rec.request.sections for l in s.lines])[:limit]

    # ---------------------------------------------------------------- search
    def search(self, q: str = "", kind: Optional[str] = None, item: Optional[str] = None, who: Optional[str] = None, since: Optional[str] = None,
               until: Optional[str] = None, status: Optional[str] = None, include_settings: bool = True, limit: int = 50, offset: int = 0,
               for_model: bool = False) -> dict[str, Any]:
        limit, offset = max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))
        since, until = _day(since), _day(until)
        terms = [t for t in re.split(r"\s+", (q or "").strip()) if t][:8]
        before = (date.fromisoformat(until) + timedelta(days=1)).isoformat() if until else None
        entries = []
        for row in self.app.stores.approvals.search(terms, kind, status, since, before, SCAN_LIMIT):
            rec = _row_to_record(row)
            if item and str((rec.request.facts or {}).get("item_code") or "").lower() != item.lower() and item.lower() not in [str(i).lower() for i in (rec.request.facts or {}).get("items") or []]:
                continue
            if who and not (rec.decision and who.lower() in rec.decision.actor_id.lower()):
                continue
            entries.append(self._entry(rec, for_model))
        if include_settings and not kind and not item and status in (None, "changed"):
            entries += self._settings(terms, who, since, until, for_model)
        entries.sort(key=lambda e: e["at"], reverse=True)
        page = entries[offset:offset + limit]
        if for_model:
            page = self._scrub(page)
        return {"total": len(entries), "listed": len(page), "offset": offset, "entries": page}

    def _entry(self, rec: ApprovalRecord, for_model: bool) -> dict[str, Any]:
        f = rec.request.facts or {}
        d = rec.decision
        return {"type": "request", "id": rec.id, "at": rec.created_at.isoformat(), "kind": rec.kind, "what": self.kind_description(rec.kind),
                "title": rec.request.title, "headline": rec.request.headline, "severity": rec.request.severity, "item_code": f.get("item_code"),
                "status": rec.status, "answer": self.action_label(rec), "answered_by": None if for_model or not d else d.actor_id,
                "answered_via": d.channel if d else None, "answered_at": d.decided_at.isoformat() if d else None, "outcome": self.outcome(rec, with_people=not for_model),
                "why": self.reasons(rec), "sealed": self.app.audit.sealed_at("approval", rec.id) is not None if getattr(self.app, "audit", None) else None}

    def _settings(self, terms: list[str], who: Optional[str], since: Optional[str], until: Optional[str], for_model: bool) -> list[dict[str, Any]]:
        out = []
        for r in self.app.stores.settings.audit(SCAN_LIMIT):
            text = " ".join(str(r[k] or "") for k in ("actor", "scope_type", "scope_key", "field", "old_json", "new_json")).lower()
            if any(t.lower() not in text for t in terms) or (who and who.lower() not in r["actor"].lower()):
                continue
            if (since and r["ts"][:10] < since) or (until and r["ts"][:10] > until):
                continue
            old, new = r["old_json"], r["new_json"]
            scope = r["scope_type"] if r["scope_key"] in ("", "*") else f"{r['scope_type']} {r['scope_key']}"
            out.append({"type": "setting", "id": f"setting-{r['id']}", "at": r["ts"], "kind": "setting", "what": "A change of settings",
                        "title": f"{r['field']} changed ({scope})", "headline": f"{r['field']}: {old if old is not None else 'not set'} -> {new if new is not None else 'not set'}",
                        "severity": "INFO", "item_code": r["scope_key"] if r["scope_type"] == "item" else None, "status": "changed", "answer": None,
                        "answered_by": None if for_model else r["actor"], "answered_via": None, "answered_at": r["ts"],
                        "outcome": f"Changed by {'a person' if for_model else r['actor']}.", "why": [f"{r['field']} was {old if old is not None else 'not set'}, now {new if new is not None else 'not set'}."],
                        "sealed": self.app.audit.sealed_at("setting", str(r["id"])) is not None if getattr(self.app, "audit", None) else None})
        return out

    def _people(self) -> set[str]:
        names = set(self.app.stores.settings.audit_actors())
        for text in self.app.stores.approvals.decision_texts():
            d = loads(text) or {}
            if d.get("actor_id"):
                names.add(d["actor_id"])
        for rec in getattr(self.app.router, "recipients", []) or []:
            names.update({rec.id, rec.name})
        names.update(a.id for a in (getattr(self.app, "tokens", {}) or {}).values())
        return {n for n in names if n and len(n) >= 3 and n.lower() not in ("system", "scheduler", "cli", "test")}

    def _scrub(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """For a language model: no person's name or id anywhere in the text (they appear in revision notes such as 'changed by priya@acme.com via app')."""
        people = sorted(self._people(), key=len, reverse=True)
        if not people:
            return entries
        pattern = re.compile(r"(?<![\w@.])(" + "|".join(re.escape(p) for p in people) + r")(?![\w@])", re.I)      # whole names and addresses only
        return json.loads(pattern.sub("a person", json.dumps(entries)))

    # ---------------------------------------------------------------- one decision in full
    def entry(self, approval_id: str) -> Optional[dict[str, Any]]:
        rec = self.app.approvals.get(approval_id)
        if rec is None:
            return None
        out = self._entry(rec, for_model=False)
        out["sections"] = [s.model_dump() for s in rec.request.sections]
        out["details"] = [s.model_dump() for s in rec.request.details]
        out["options"] = [{"action": o.action, "label": o.label} for o in rec.request.options]
        out["facts"] = rec.request.facts
        out["note"] = rec.decision.note if rec.decision else ""
        out["params"] = rec.decision.params if rec.decision else {}
        out["status_note"] = rec.status_note
        out["sent_to"] = [{"channel": d["channel"], "recipient": d["recipient_id"], "status": d["status"], "at": d["sent_at"]}
                          for d in self.app.stores.approvals.deliveries(approval_id)]
        out["reminders"] = rec.reminder_count
        same = self.app.stores.approvals.with_reference(rec.request.ref_type, rec.request.ref_id, rec.id) if rec.request.ref_id else []
        out["versions"] = [{"id": r["id"], "status": r["status"], "at": r["created_at"], "note": r["status_note"]} for r in same if r["kind"] == rec.kind]      # earlier or later versions of this request
        out["related"] = [{"id": r["id"], "kind": r["kind"], "what": self.kind_description(r["kind"]), "status": r["status"], "at": r["created_at"]}
                          for r in same if r["kind"] != rec.kind]                                                                                 # what followed from it (e.g. managing the order)
        out["ai"] = [{"task": r["id"], "agent": r["agent"], "kind": r["task"], "status": r["status"], "at": r["started_at"], "model": r["model"],
                      "result": (loads(r["result_json"]) or {}) if r["result_json"] else None}
                     for r in self.app.stores.agents.tasks_about(approval_id, f'"request_id": "{approval_id}"')]
        extra = []
        for provide in self.extras:
            try:
                extra += provide(rec)
            except Exception as exc:  # noqa: BLE001 - context is a courtesy; the record itself is complete without it
                log.warning("context for request %s could not be read: %s", rec.id, type(exc).__name__)
        out["context"] = extra
        out["sealed_at"] = self.app.audit.sealed_at("approval", rec.id) if getattr(self.app, "audit", None) else None
        return out

    # ---------------------------------------------------------------- export
    def csv(self, **filters: Any) -> str:
        """The search result as CSV, for an auditor or a spreadsheet (one row per decision, the reasons joined)."""
        res = self.search(**{**filters, "limit": MAX_LIMIT})
        buf = io.StringIO()
        cols = ["at", "type", "id", "what", "title", "item_code", "status", "answer", "answered_by", "answered_via", "answered_at", "outcome", "why", "sealed"]
        w = csv.writer(buf)
        w.writerow(cols)
        for e in res["entries"]:
            w.writerow([_cell(" | ".join(e[c])) if isinstance(e.get(c), list) else ("" if e.get(c) is None else _cell(e[c])) for c in cols])
        return buf.getvalue()


def _cell(value: Any) -> str:
    """A spreadsheet must not run text from a request as a formula."""
    text = value.isoformat() if isinstance(value, datetime) else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text

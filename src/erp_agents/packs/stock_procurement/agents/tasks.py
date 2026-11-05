"""The stock pack's agent tasks: the inventory brief, a second opinion on an order request, and answers to questions.

The core's `AgentService` runs a task: it picks the model, offers the tools, applies the checks, keeps the record and the token count, and falls back to rules. What is specific to
stock and purchasing is here: what each task asks, which tools it may use, how its answer is checked and what the plain-rules result is. `StockAgentTasks` is handed to the service
(`add_tasks`), which then offers these methods as its own, so `app.agents.brief()` and `app.agents.review(...)` work as they always did. Everything the service has (its settings,
its `run` and `submit`, its record) is reachable from here as `self.<name>`.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from erp_agents.core.agents.runtime import Evidence, Outcome
from erp_agents.core.agents.service import BUSY, Plan
from erp_agents.core.approvals.models import ApprovalRecord
from erp_agents.core.assistant.verify import NOT_ENOUGH
from erp_agents.core.common import Actor, untrusted
from ..assistant.chat import MAX_CHAT_QUESTION
from ..procurement.agent import KIND_PO, KIND_PR
from . import inventory_agent as inventory
from . import purchase_agent as purchasing

log = logging.getLogger(__name__)

WORKERS = {name: Actor(f"agent:{name}", frozenset({"viewer"}), "agent") for name in ("inventory", "purchasing")}      # scheduled work can look, never prepare an order


class StockAgentTasks:
    def __init__(self, service: Any) -> None:
        self._service = service

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name == "_service":
            raise AttributeError(name)
        return getattr(self._service, name)

    # ---------------------------------------------------------------- the inventory brief
    def brief_plan(self, asked_by: str = "system") -> Plan:
        job = inventory.brief_job(self.today())
        actor = WORKERS["inventory"]

        def table(ev: Evidence) -> dict[str, Any]:
            """The rules' brief: the table is always theirs, and so is the headline unless the model's passes the checks."""
            found = ev.results("stock_overview")
            return inventory.rules_brief(found[0] if found else {"items": [], "counts": {}, "items_total": 0})

        def points(ev: Evidence) -> list[dict[str, str]]:
            return inventory.attention_points(ev, self.app.inventory.today())

        def shape(brief: dict[str, Any]) -> dict[str, Any]:
            return {**brief, "text": inventory.brief_text(brief)}

        def finish(o: Outcome) -> dict[str, Any]:
            ev = o.evidence or Evidence()
            data = o.data or {}
            brief = table(ev)
            return shape({**brief, "headline": data["headline"], "watch_outs": inventory.merge_watch_outs(points(ev), data["watch_outs"], tuple(p["item_code"] for p in brief["priorities"]))})

        def fallback(o: Outcome) -> dict[str, Any]:
            ev = o.evidence if (o.evidence and o.evidence.results("stock_overview")) else self._evidence_without_model(job, actor)
            return shape({**table(ev), "watch_outs": points(ev)})
        return Plan(job, actor, finish, fallback, request_text="daily stock brief", asked_by=asked_by)

    def brief_async(self, asked_by: str = "system") -> dict[str, Any]:
        """For a web request: the rules brief at once when the agents are off, otherwise start the agent in the background (or point to the one already running)."""
        if not self.enabled:
            return self._service.brief(asked_by)
        running = self._running("inventory", "brief")
        return {"task_id": running or self.submit(self._service.brief_plan(asked_by)), "status": "running"}

    def brief(self, asked_by: str = "system", use_model: Optional[bool] = None) -> dict[str, Any]:
        """The stock brief. With the agents off (or use_model=False) it is written by rules alone and not recorded."""
        plan = self._service.brief_plan(asked_by)
        if use_model is False or (use_model is None and not self.enabled):
            result = plan.fallback(Outcome())
            return {"task_id": None, "status": "rules", "source": "rules", "model": None, "note": "" if use_model is False else "The AI agents are switched off; this brief is written by rules alone.", **result}
        return self.run(plan)

    # ---------------------------------------------------------------- a second opinion on an order request
    def review_plan(self, approval_id: str, asked_by: str = "system") -> Plan:
        rec = self.app.approvals.get(approval_id)
        if rec is None or rec.kind not in (KIND_PO, KIND_PR):
            raise ValueError("A second opinion is available for purchase order and requisition requests only.")
        facts = rec.request.facts or {}
        if not facts.get("item_code") or not facts.get("qty"):
            raise ValueError("This request does not name an item and a quantity.")
        job = purchasing.review_job(facts, self.today())
        actor = WORKERS["purchasing"]

        def finish(o: Outcome) -> dict[str, Any]:
            return {**purchasing.review_result(o.data, facts, o.evidence or Evidence()), "request_key": self._key(facts)}

        def fallback(o: Outcome) -> dict[str, Any]:
            ev = o.evidence if (o.evidence and o.evidence.results("vendor_options")) else self._evidence_without_model(job, actor)
            return {**purchasing.rules_review(facts, ev), "request_key": self._key(facts)}
        return Plan(job, actor, finish, fallback, request_text=job.question, approval_id=approval_id, asked_by=asked_by)

    @staticmethod
    def _key(facts: dict[str, Any]) -> str:
        return f"{facts.get('item_code')}|{facts.get('qty')}|{facts.get('supplier')}"

    def _cached_review(self, approval_id: str) -> Optional[dict[str, Any]]:
        """The review already made of this request, if the request has not changed since."""
        rec = self.app.approvals.get(approval_id)
        for row in self.log.list(approval_id=approval_id, task="review", limit=3):
            if rec and row["status"] in ("ok", "fallback") and (row["result"] or {}).get("request_key") == self._key(rec.request.facts):
                return {"task_id": row["id"], "status": row["status"], "source": row["result"].get("source"), "cached": True, **row["result"]}
        return None

    def review(self, approval_id: str, asked_by: str = "system", force: bool = False, use_model: Optional[bool] = None) -> dict[str, Any]:
        plan = self._service.review_plan(approval_id, asked_by)
        cached = None if force else self._service._cached_review(approval_id)
        if cached:
            return cached
        if use_model is False or (use_model is None and not self.enabled):
            return {"task_id": None, "status": "rules", "source": "rules", "cached": False, **plan.fallback(Outcome())}
        return self.run(plan)

    def review_async(self, approval_id: str, asked_by: str = "system", force: bool = False) -> dict[str, Any]:
        """For a web request: the review at once if it exists or the agents are off, otherwise start it in the background."""
        plan = self._service.review_plan(approval_id, asked_by)               # raises ValueError for a request that cannot be reviewed
        cached = None if force else self._service._cached_review(approval_id)
        if cached:
            return cached
        if not self.enabled:
            return {"task_id": None, "status": "rules", "source": "rules", "cached": False, **plan.fallback(Outcome())}
        return {"task_id": self._running("purchasing", "review", approval_id) or self.submit(plan), "status": "running"}

    def on_submitted(self, record: ApprovalRecord) -> None:
        """Called for every new request that was sent out. With 'second_opinion' on, the purchasing agent reads it in the background."""
        cfg = self.settings
        if cfg.enabled and cfg.second_opinion and record.kind in (KIND_PO, KIND_PR):
            self._service.runtime.jobs.submit(self._service.review_and_tell, record.id, name="agent-second-opinion")

    def review_and_tell(self, approval_id: str) -> Optional[dict[str, Any]]:
        """Review the request and send the second opinion to the people who got it - only when it has something to say."""
        try:
            res = self._service.review(approval_id, asked_by="second_opinion")
        except Exception:  # noqa: BLE001
            log.exception("the second opinion on %s failed", approval_id)
            return None
        rec = self.app.approvals.get(approval_id)
        if res.get("cached") or rec is None or rec.status != "pending":
            return res
        if res.get("source") == "model" or res.get("verdict") == "check_first":
            self.app.approvals.tell_recipients(approval_id, purchasing.review_text(res))
        return res

    # ---------------------------------------------------------------- a question
    def ask_plan(self, question: str, actor: Actor) -> Plan:
        question = untrusted(question, 100_000)                  # control and invisible characters out; the length rule below still applies
        if not question:
            raise ValueError("Please type a question.")
        if len(question) > MAX_CHAT_QUESTION:
            raise ValueError(f"Please keep the question under {MAX_CHAT_QUESTION} characters.")
        job = purchasing.ask_job(question, actor.id, self.today(), can_prepare=actor.has_role("approver"))

        def prepared(o: Outcome) -> Optional[dict[str, Any]]:
            for r in (o.evidence.results("prepare_order_request") if o.evidence else []):
                if r.get("request_id"):
                    return r
            return None

        def note_for(p: Optional[dict[str, Any]]) -> str:
            if not p:
                return ""
            return (f" I prepared the request ({p.get('request_title') or p.get('item_code')}) and sent it for approval. {p.get('note', '')}").rstrip()

        def finish(o: Outcome) -> dict[str, Any]:
            data = o.data or {}
            p = prepared(o)
            if not data.get("answerable"):
                return {"status": "not_enough_information", "answer": (NOT_ENOUGH + note_for(p)).strip(), "agent": job.agent, "prepared": p}
            return {"status": "answered", "answer": " ".join(data["answer"].split()) + note_for(p), "agent": job.agent, "prepared": p}

        def fallback(o: Outcome) -> dict[str, Any]:
            p = prepared(o)
            if o.note == "busy":
                return {"status": "busy", "answer": BUSY, "agent": job.agent, "prepared": p}
            if o.status == "rejected":
                return {"status": "not_enough_information", "answer": (NOT_ENOUGH + note_for(p)).strip(), "agent": job.agent, "prepared": p}
            return {"status": "unavailable", "answer": ("The AI assistant is not available just now." + note_for(p)).strip(), "agent": job.agent, "prepared": p}
        return Plan(job, actor, finish, fallback, request_text=question, asked_by=actor.id)

    def ask(self, question: str, actor: Actor) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled", "answer": "The AI agents are switched off in Settings.", "task_id": None, "source": "rules"}
        return self.run(self._service.ask_plan(question, actor), wait=3)

    def chat_background(self, question: str, user: str, can_decide: bool) -> Optional[dict[str, Any]]:
        """For the web chat: start the agent in the background and hand back the task to poll (a reply can take minutes, longer than a tunnel waits). None = cannot help."""
        if not self.enabled:
            return None
        try:
            plan = self._service.ask_plan(question, Actor(user, frozenset({"approver"} if can_decide else {"viewer"}), "chat"))
        except ValueError:
            return None
        return {"task_id": self.submit(plan), "answer": "I am asking the AI assistant. On this computer that can take a few minutes; the answer will appear here."}

    def answer_for_chat(self, question: str, user: str, can_decide: bool) -> Optional[dict[str, Any]]:
        """Used by the chat when its rules did not understand a question. None = the agents cannot help (off, busy or not available): the chat shows its usual help."""
        if not self.enabled:
            return None
        res = self._service.ask(question, Actor(user, frozenset({"approver"} if can_decide else {"viewer"}), "chat"))
        return res if res.get("status") in ("answered", "not_enough_information") else None

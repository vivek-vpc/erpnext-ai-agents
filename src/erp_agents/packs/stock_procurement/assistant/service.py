"""'Ask why': answer a person's question about one request using only that request's recorded facts.

Flow: build numbered facts from stored data -> ask the local model with fixed settings (temperature 0, fixed seed,
JSON schema) -> check the reply in code (verify.py) -> show it only if every check passes, otherwise say there is
not enough information. Answers are stored, so the same question about the same request always gets the same
answer, and every answer can be audited later.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Callable, Optional

from erp_agents.core.approvals.service import ApprovalService
from erp_agents.core.common import Actor, Clock, iso, utcnow
from erp_agents.core.policy import AssistantSettings
from erp_agents.core.settings_store import SettingsStore
from erp_agents.core.storage import Database, dumps, loads
from ..inventory.alerts import AlertRepo
from ..procurement.proposals import ProposalRepo
from erp_agents.core.assistant.facts import Fact, build_facts
from erp_agents.core.assistant.llm import LLMClient, LLMError, OllamaClient
from erp_agents.core.assistant.verify import NOT_ENOUGH, verify
from erp_agents.core.prompting import DATA_RULE, clean, data, fact_line
from ..schema import ensure_schema

log = logging.getLogger(__name__)

PROMPT_VERSION = "ask-why-v1"
MAX_QUESTION = 500

SYSTEM_PROMPT = """You explain one recorded stock or purchase decision to the business owner.
Follow these rules exactly:
1. Use ONLY the numbered FACTS you are given. Do not use outside knowledge. Do not guess. Do not assume anything that is not written in the facts.
2. Do not calculate or estimate new numbers. Repeat numbers exactly as they are written in the facts.
3. If the facts do not contain the answer, set "answerable" to false and leave "answer" empty.
4. Answer in at most 4 short, plain sentences.
5. Put in "facts_used" the id (for example "F3") of every fact your answer relies on.
6. """ + DATA_RULE + """
Reply only with JSON."""

REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answerable": {"type": "boolean"},
        "answer": {"type": "string"},
        "facts_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answerable", "answer", "facts_used"],
}


class AssistantService:
    def __init__(self, db: Database, settings: SettingsStore, approvals: ApprovalService, alerts: AlertRepo,
                 proposals: ProposalRepo, clock: Clock = utcnow,
                 client_factory: Optional[Callable[[AssistantSettings], LLMClient]] = None) -> None:
        ensure_schema(db)
        self.db, self.settings, self.approvals, self.alerts, self.proposals, self.clock = db, settings, approvals, alerts, proposals, clock
        self.client_factory = client_factory or (lambda cfg: OllamaClient(cfg.base_url, cfg.model, cfg.timeout_seconds))

    # ---------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        cfg = self.settings.assistant()
        out: dict[str, Any] = {"enabled": cfg.enabled, "model": cfg.model, "base_url": cfg.base_url}
        client = self.client_factory(cfg)
        if hasattr(client, "status"):
            out.update(client.status())                       # type: ignore[attr-defined]
        return out

    # ---------------------------------------------------------------- facts for one request
    def facts_for(self, approval_id: str) -> list[Fact]:
        record = self.approvals.get(approval_id)
        if record is None:
            raise KeyError(f"Unknown request {approval_id!r}")
        analysis = ranking = None
        if record.request.ref_type == "inventory_alert":
            alert = self.alerts.by_approval(record.id)
            analysis = alert.analysis if alert else None
        elif record.request.ref_type == "procurement_proposal":
            prop = self.proposals.by_approval(record.id)
            ranking = prop.ranking if prop else None
        return build_facts(record, analysis, ranking)

    # ---------------------------------------------------------------- ask
    def ask(self, approval_id: str, question: str, actor: Actor) -> dict[str, Any]:
        cfg = self.settings.assistant()
        question = clean(question or "", 100_000)               # control, invisible and bidi characters out; the length rule below still applies
        if not question:
            raise ValueError("Please type a question.")
        if len(question) > MAX_QUESTION:
            raise ValueError(f"Please keep the question under {MAX_QUESTION} characters.")
        if not cfg.enabled:
            return {"status": "disabled", "answer": "The assistant is switched off in Configuration.", "facts": [], "check": "", "model": cfg.model}

        facts = self.facts_for(approval_id)
        key = hashlib.sha256(json.dumps([PROMPT_VERSION, cfg.model, [f.text for f in facts], question.lower()]).encode()).hexdigest()
        row = self.db.query_one("SELECT result_json FROM assistant_answers WHERE cache_key=?", (key,))
        if row:
            result = loads(row["result_json"])
            result.pop("draft", None)
            return {**result, "cached": True}

        user = "FACTS:\n" + "\n".join(fact_line(f.id, f.text) for f in facts) + f"\n\nQUESTION: {data(question, MAX_QUESTION)}"
        started = time.monotonic()
        try:
            raw = self.client_factory(cfg).chat(SYSTEM_PROMPT, user, REPLY_SCHEMA)
        except LLMError as exc:
            log.warning("assistant unavailable: %s", exc)
            return {"status": "unavailable", "answer": "The local assistant is not available right now. " + str(exc),
                    "facts": [], "check": "", "model": cfg.model, "cached": False}
        verdict = verify(raw, facts, question, cfg.max_answer_chars)
        result = {
            "status": verdict.status,
            "answer": verdict.answer if verdict.status == "answered" else NOT_ENOUGH,
            "facts": [{"id": f.id, "text": f.text} for f in verdict.cited],
            "check": verdict.check,
            "model": cfg.model,
            "seconds": round(time.monotonic() - started, 1),
        }
        # The draft is kept for auditing a rejected answer; it is never shown as an answer.
        self.db.execute(
            "INSERT OR REPLACE INTO assistant_answers(cache_key, approval_id, question, result_json, model, asked_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (key, approval_id, question, dumps({**result, "draft": raw[:4000]}), cfg.model, actor.id, iso(self.clock())),
        )
        return {**result, "cached": False}

    def history(self, approval_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT question, result_json, asked_by, created_at FROM assistant_answers WHERE approval_id=? ORDER BY created_at", (approval_id,))
        out = []
        for r in rows:
            res = loads(r["result_json"])
            res.pop("draft", None)
            out.append({"question": r["question"], "asked_by": r["asked_by"], "asked_at": r["created_at"], **res})
        return out

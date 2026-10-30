"""The stock pack's REST routes: thresholds and the procurement settings, the inventory views and runs, the vendor selector, the "Ask why" assistant, the chat, and the
agents' brief, second opinion and question endpoints.

The core mounts them on a router that answers 404 when the pack is switched off (after the usual sign-in check, so a stranger learns nothing). `require(role)` is the core's role
check, the same as on every other route.
"""
from __future__ import annotations

import json
import math
from typing import Any, Optional

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, Field

from erp_agents.core.common import Actor
from erp_agents.core.resilience import RateLimited
from erp_agents.core.erp.base import DEFAULT_IN_TRANSIT, DEFAULT_STOCK_ADD, DEFAULT_STOCK_SUBTRACT, IN_TRANSIT_LABEL, IN_TRANSIT_MODES, STOCK_COLUMNS
from erp_agents.core.settings_store import PermissionDenied, SettingsError
from .assistant.chat import ChatLimit
from .settings import MONITOR_COLUMNS


class ChatContext(BaseModel):
    items: list[str] = Field(default_factory=list, max_length=5)


class ChatBody(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    context: Optional[ChatContext] = None
    explain: bool = False


class AskBody(BaseModel):
    approval_id: str
    question: str


class AgentAskBody(BaseModel):
    question: str = Field(min_length=1, max_length=300)


class QuoteBody(BaseModel):
    item_code: str = Field(min_length=1, max_length=140)
    qty: Optional[float] = Field(None, gt=0, le=1_000_000_000)
    reason: str = Field("", max_length=300)
    vendors: list[str] = Field(default_factory=list, max_length=5, description="Only these suppliers (codes or names). Empty = the best ones on record.")


class TransferBody(BaseModel):
    item_code: str = Field(min_length=1, max_length=140)
    qty: float = Field(gt=0, le=1_000_000_000, description="In the item's own unit")
    from_warehouse: str = Field(min_length=1, max_length=140, description="As named in ERPNext (the part before ' - Company' is enough when it is unique)")
    to_warehouse: str = Field(min_length=1, max_length=140)
    reason: str = Field("", max_length=300)


class PolicyUpdate(BaseModel):
    set: dict[str, Any] = Field(default_factory=dict, description="Fields to override. null switches a nullable threshold off.")
    clear: list[str] = Field(default_factory=list, description="Fields to inherit again.")


def routes(router: Any, ctx: Any, deps: Any) -> None:
    require, MAX_LIST_LIMIT = deps.require, deps.MAX_LIST_LIMIT

    def too_often(exc: RateLimited) -> HTTPException:
        """429, with how long to wait in words (for a person) and in the Retry-After header (for a program)."""
        return HTTPException(429, str(exc), headers={"Retry-After": str(max(1, math.ceil(exc.retry_after)))})

    def model_limit(actor: Actor) -> None:
        """Starting a language-model task is limited per person, with one count for every action that does (Ask why, an agent question, a brief, a review)."""
        try:
            ctx.limits.enforce("model", actor.id)
        except RateLimited as exc:
            raise too_often(exc) from None

    # ---------------------------------------------------------------- purchasing follow-up and stock intelligence (the same results as the agents' tools)
    def tool(name: str, args: dict[str, Any], actor: Actor) -> dict:
        res = ctx.toolkit.call(name, {k: v for k, v in args.items() if v is not None}, actor)
        if isinstance(res, dict) and res.get("error") and len(res) <= 2:
            raise HTTPException(404 if res["error"].startswith("no item") else 422, res["error"])
        return res

    @router.get("/api/v1/followup/late-deliveries")
    def late_deliveries(actor: Actor = Depends(require("viewer"))) -> dict:
        """Open purchase orders past their due date, with what each delay does to stock cover."""
        return tool("late_deliveries", {}, actor)

    @router.get("/api/v1/followup/receipts")
    def receipt_mismatches(days: Optional[int] = Query(None, ge=1, le=120), actor: Actor = Depends(require("viewer"))) -> dict:
        return tool("receipt_mismatches", {"days": days}, actor)

    @router.get("/api/v1/followup/scorecard")
    def vendor_scorecard(days: Optional[int] = Query(None, ge=30, le=730), vendor: Optional[str] = Query(None, max_length=140), actor: Actor = Depends(require("viewer"))) -> dict:
        return tool("vendor_scorecard", {"days": days, "vendor": vendor}, actor)

    @router.get("/api/v1/followup/quotes")
    def quote_rounds(limit: int = Query(20, ge=1, le=MAX_LIST_LIMIT), _: Actor = Depends(require("viewer"))) -> list[dict]:
        return [{k: r[k] for k in ("id", "item_code", "qty", "uom", "status", "reply_by", "asked_at", "approval_id", "comparison_id", "status_note", "created_at")}
                | {"vendors": [v["name"] for v in r["suppliers"]]} for r in ctx.followup.quotes.list(limit=limit)]

    @router.post("/api/v1/followup/quotes")
    def prepare_quotes(body: QuoteBody, actor: Actor = Depends(require("approver"))) -> dict:
        """Prepare a request to ask vendors for a price. It messages no vendor: a person presses Send requests."""
        qty = body.qty
        if qty is None:
            try:
                a, _ = ctx.inventory.analyze_one(body.item_code)
            except KeyError as exc:
                raise HTTPException(404, str(exc))
            qty = a.suggested_qty or ctx.settings.procurement().fallback_qty
            if not qty:
                raise HTTPException(422, "there is no usage history to size the quantity from: give a quantity")
        res = ctx.followup.quotes.prepare(body.item_code, float(qty), actor.id, body.reason, body.vendors or None)
        if res["status"] == "refused":
            raise HTTPException(422, res["error"])
        return res

    @router.post("/api/v1/followup/run")
    def run_followup(dry_run: bool = True, weekly: bool = False, _: Actor = Depends(require("admin"))) -> dict:
        """Dry-run by default. The daily pass (late deliveries, goods receipts, count differences, quote rounds) or, with weekly=true, the slow-stock list and the count plan."""
        if weekly:
            return {"slow_stock": ctx.stock_insight.slow.run(dry_run), "count_plan": ctx.stock_insight.counts.run_plan(dry_run)}
        return {**ctx.followup.daily(dry_run), "count_differences": ctx.stock_insight.counts.run_variances(dry_run), "quotes": ctx.followup.quotes.check(dry_run)}

    @router.get("/api/v1/inventory/forecast/{item_code}")
    def demand_forecast(item_code: str, weeks: int = Query(6, ge=1, le=13), actor: Actor = Depends(require("viewer"))) -> dict:
        return tool("demand_forecast", {"item_code": item_code, "weeks": weeks}, actor)

    @router.get("/api/v1/inventory/slow-stock")
    def slow_stock(limit: int = Query(25, ge=1, le=25), actor: Actor = Depends(require("viewer"))) -> dict:
        return tool("slow_stock", {"limit": limit}, actor)

    @router.get("/api/v1/inventory/substitutes/{item_code}")
    def substitutes(item_code: str, actor: Actor = Depends(require("viewer"))) -> dict:
        return tool("substitutes", {"item_code": item_code}, actor)

    @router.get("/api/v1/inventory/counts")
    def stock_counts(kind: str = Query("plan", pattern="^(plan|variances)$"), actor: Actor = Depends(require("viewer"))) -> dict:
        return tool("stock_counts", {"kind": kind}, actor)

    @router.get("/api/v1/settings/procurement")
    def get_procurement(_: Actor = Depends(require("viewer"))) -> dict:
        return {"effective": ctx.settings.procurement().model_dump(), "overrides": ctx.settings._section_overrides("procurement")}

    @router.put("/api/v1/settings/procurement")
    def put_procurement(body: PolicyUpdate, actor: Actor = Depends(require("admin"))) -> dict:
        try:
            ctx.settings.update_procurement(actor, body.set, body.clear)
        except SettingsError as exc:
            raise HTTPException(422, str(exc))
        return {"effective": ctx.settings.procurement().model_dump(), "overrides": ctx.settings._section_overrides("procurement")}

    @router.get("/api/v1/settings/policy/{scope_type}/{scope_key:path}")
    def get_policy(scope_type: str, scope_key: str, group: Optional[str] = None, _: Actor = Depends(require("viewer"))) -> dict:
        """scope_type: global | group | item. For global use key '*'. For items, pass ?group= to see the inherited result."""
        if scope_type not in ("global", "group", "item"):
            raise HTTPException(404, "unknown scope")
        key = "*" if scope_type == "global" else scope_key
        item = key if scope_type == "item" else None
        grp = key if scope_type == "group" else group
        try:
            policy, sources = ctx.settings.policy_with_sources(item, grp)
        except SettingsError as exc:
            raise HTTPException(422, str(exc))
        return {"overrides": ctx.settings.get_overrides(scope_type, key), "effective": policy.model_dump(), "sources": sources}

    @router.put("/api/v1/settings/policy/{scope_type}/{scope_key:path}")
    def put_policy(scope_type: str, scope_key: str, body: PolicyUpdate, actor: Actor = Depends(require("admin"))) -> dict:
        try:
            policy = ctx.settings.update_policy(actor, scope_type, scope_key, body.set, body.clear)
        except SettingsError as exc:
            raise HTTPException(422, str(exc))
        except PermissionDenied as exc:
            raise HTTPException(403, str(exc))
        key = "*" if scope_type == "global" else scope_key
        return {"overrides": ctx.settings.get_overrides(scope_type, key), "validated_against_global": policy.model_dump()}

    @router.delete("/api/v1/settings/policy/{scope_type}/{scope_key:path}")
    def delete_policy(scope_type: str, scope_key: str, actor: Actor = Depends(require("admin"))) -> dict:
        ctx.settings.reset_scope(actor, scope_type, scope_key)
        return {"ok": True}

    @router.get("/api/v1/inventory/items")
    def list_items(_: Actor = Depends(require("viewer"))) -> list[dict]:
        """Every ERP item with live stock, days of cover and the thresholds in force (for the admin screen)."""
        return ctx.inventory.overview()

    @router.get("/api/v1/inventory/stock-options")
    def stock_options(_: Actor = Depends(require("viewer"))) -> dict:
        """What the stock-calculation settings can choose from: the ERP stock columns and its warehouses."""
        columns = [{"column": c, "label": label, "default": "subtract" if c in DEFAULT_STOCK_SUBTRACT else "add" if c in DEFAULT_STOCK_ADD else "not used"}
                   for c, (label, _attr, _phrase) in STOCK_COLUMNS.items()]
        try:
            warehouses, error = ctx.erp.list_warehouses(), None
        except Exception as exc:  # noqa: BLE001 - the screen still works, the filter just cannot offer a list
            warehouses, error = [], f"could not read warehouses from the ERP: {exc}"
        transit = {"label": IN_TRANSIT_LABEL, "default": DEFAULT_IN_TRANSIT, "modes": list(IN_TRANSIT_MODES)}
        return {"columns": columns, "in_transit": transit, "warehouses": warehouses, "warehouses_error": error,
                "monitor_columns": [{"column": c, "label": label} for c, label in MONITOR_COLUMNS.items()]}

    @router.get("/api/v1/inventory/transfers")
    def stock_transfers(limit: int = Query(20, ge=1, le=MAX_LIST_LIMIT), _: Actor = Depends(require("viewer"))) -> list[dict]:
        """Requests to move stock between warehouses, newest first, with the draft Stock Entry each one made."""
        return [{k: t[k] for k in ("id", "item_code", "qty", "uom", "from_warehouse", "to_warehouse", "status", "entry_name", "reason", "approval_id", "status_note", "created_at")}
                for t in ctx.transfers.list(limit=limit)]

    @router.post("/api/v1/inventory/transfers")
    def prepare_stock_transfer(body: TransferBody, actor: Actor = Depends(require("approver"))) -> dict:
        """Prepare a request to move stock from one warehouse to another. It creates nothing in ERPNext: a person presses Create transfer, which makes a draft."""
        res = ctx.transfers.prepare(body.item_code, body.qty, body.from_warehouse, body.to_warehouse, actor.id, body.reason)
        if res["status"] == "refused":
            raise HTTPException(404 if res["error"].startswith("no item") else 422, res["error"])
        return res

    @router.get("/api/v1/inventory/analysis/{item_code}")
    def analysis(item_code: str, _: Actor = Depends(require("viewer"))) -> dict:
        try:
            a, sources = ctx.inventory.analyze_one(item_code)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        return {"analysis": a.to_dict(), "policy_sources": sources}

    @router.post("/api/v1/inventory/run")
    def run_agent(dry_run: bool = True, _: Actor = Depends(require("admin"))) -> dict:
        """Dry-run by default: shows what the agent *would* do without sending anything."""
        return ctx.inventory.run(dry_run=dry_run).to_dict()

    @router.get("/api/v1/inventory/alerts")
    def list_alerts(status: Optional[str] = None, item_code: Optional[str] = None, limit: int = Query(100, ge=1, le=MAX_LIST_LIMIT),
                    _: Actor = Depends(require("viewer"))) -> list[dict]:
        return [
            {"id": a.id, "item_code": a.item_code, "kind": a.kind, "severity": a.severity, "status": a.status,
             "created_at": a.created_at.isoformat(), "approval_id": a.approval_id, "status_note": a.status_note,
             "reasoning": a.reasoning_text}
            for a in ctx.alerts.list(status, item_code, limit)
        ]

    @router.get("/api/v1/inventory/runs")
    def runs(limit: int = Query(20, ge=1, le=MAX_LIST_LIMIT), _: Actor = Depends(require("viewer"))) -> list[dict]:
        rows = ctx.db.query("SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [{"id": r["id"], "started_at": r["started_at"], "dry_run": bool(r["dry_run"]), "summary": json.loads(r["summary_json"] or "{}")} for r in rows]

    @router.post("/api/v1/procurement/run")
    def run_procurement(dry_run: bool = True, _: Actor = Depends(require("admin"))) -> dict:
        """Dry-run by default: shows which vendor it would recommend for each approved reorder, sends nothing."""
        return ctx.procurement.run(dry_run=dry_run).to_dict()

    @router.get("/api/v1/procurement/proposals")
    def list_proposals(status: Optional[str] = None, item_code: Optional[str] = None, limit: int = Query(100, ge=1, le=MAX_LIST_LIMIT),
                       _: Actor = Depends(require("viewer"))) -> list[dict]:
        return [p.to_dict() for p in ctx.proposals.list(status, item_code, limit)]

    @router.get("/api/v1/procurement/preview/{item_code}")
    def preview_vendors(item_code: str, qty: float, cover_days: Optional[float] = None, lead_time_days: float = 7.0,
                        _: Actor = Depends(require("viewer"))) -> dict:
        """How the vendors for an item would rank for a given quantity (nothing is stored or sent)."""
        if qty <= 0:
            raise HTTPException(422, "qty must be above zero")
        try:
            return ctx.procurement.preview(item_code, qty, cover_days, lead_time_days)
        except Exception as exc:  # noqa: BLE001 - ERP errors should read clearly in the UI
            raise HTTPException(502, f"could not read vendors from the ERP: {exc}")

    @router.get("/api/v1/assistant/status")
    def assistant_status(_: Actor = Depends(require("viewer"))) -> dict:
        return ctx.assistant.status()

    @router.post("/api/v1/assistant/ask")
    def assistant_ask(body: AskBody, actor: Actor = Depends(require("approver"))) -> dict:
        model_limit(actor)
        try:
            return ctx.assistant.ask(body.approval_id, body.question, actor)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @router.get("/api/v1/assistant/history/{approval_id}")
    def assistant_history(approval_id: str, _: Actor = Depends(require("approver"))) -> list[dict]:
        return ctx.assistant.history(approval_id)

    @router.post("/api/v1/chat")
    def chat(body: ChatBody, actor: Actor = Depends(require("viewer"))) -> dict:
        ctx_dict = {"items": [c[:80] for c in body.context.items]} if body.context else None
        try:
            if body.explain:
                return ctx.chat.explain(body.question, actor.id, actor.has_role("approver"), ctx_dict)
            return ctx.chat.ask(body.question, actor.id, actor.has_role("approver"), ctx_dict, background=True)
        except ChatLimit as exc:
            raise too_often(exc) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @router.get("/api/v1/agents/brief")
    def agents_brief(_: Actor = Depends(require("viewer"))) -> dict:
        row = ctx.agent_log.latest("inventory", "brief")
        if row is None:
            return {**ctx.agents.brief(use_model=False), "stored": False}
        return {"task_id": row["id"], "status": row["status"], "started_at": row["started_at"], "model": row["model"], "note": row["note"], "stored": True, **(row["result"] or {})}

    @router.post("/api/v1/agents/brief")
    def agents_brief_run(actor: Actor = Depends(require("approver"))) -> dict:
        model_limit(actor)
        return ctx.agents.brief_async(actor.id)

    @router.post("/api/v1/agents/ask")
    def agents_ask(body: AgentAskBody, actor: Actor = Depends(require("viewer"))) -> dict:
        if not ctx.agents.enabled:
            raise HTTPException(409, "The AI agents are switched off in Settings.")
        try:
            ctx.chat.throttle(actor.id)
            model_limit(actor)
            plan = ctx.agents.ask_plan(body.question, actor)
        except ChatLimit as exc:
            raise too_often(exc) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {"task_id": ctx.agents.submit(plan), "status": "running"}

    @router.get("/api/v1/agents/reviews/{approval_id}")
    def agents_review(approval_id: str, _: Actor = Depends(require("approver"))) -> dict:
        rows = ctx.agent_log.list(approval_id=approval_id, task="review", limit=1)
        return {"review": rows[0] if rows else None}

    @router.post("/api/v1/agents/reviews/{approval_id}")
    def agents_review_run(approval_id: str, force: bool = False, actor: Actor = Depends(require("approver"))) -> dict:
        model_limit(actor)
        try:
            return ctx.agents.review_async(approval_id, actor.id, force)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

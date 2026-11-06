"""The stock and procurement pack: its entry object. `PACK` is what the core loads (core/packs.py) when the pack is switched on.

`setup(app)` builds this pack's services on top of the core (the inventory and procurement agents, the chat and the "Ask why" assistant, the conversation on Telegram and
WhatsApp), registers what it adds to the core's registries (settings, database tables, tools, approval kinds, agent tasks), and hands them to the application as services, so
`app.inventory`, `app.procurement`, `app.alerts`, `app.proposals`, `app.assistant` and `app.chat` keep working. It imports the core and itself, never another pack.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ai_munimji_engine.core.common import Actor
from erp_agents.core.config import HASH_KEY_ENV, hash_key_state
from ai_munimji_engine.core.packs import Pack
from ai_munimji_engine.packs.stock_procurement.manifest import manifest as stock_manifest

from .agents.tasks import StockAgentTasks
from .followup.messages import KIND_CHASE, KIND_QUOTE_REQUEST, KIND_QUOTES, KIND_RECEIPT
from .followup.service import FollowUp, StockInsight
from .inventory.counts import KIND_COUNT_PLAN, KIND_COUNT_VARIANCE
from .inventory.slow_stock import KIND_SLOW
from ai_munimji_engine.packs.stock_procurement.inventory.messages import KIND_TRANSFER
from .inventory.transfers import StockTransfers
from .assistant.chat import ChatService
from .assistant.conversation import Conversation
from .assistant.service import AssistantService
from ai_munimji_engine.packs.stock_procurement.inventory.agent import InventoryAgent
from ai_munimji_engine.packs.stock_procurement.inventory.alerts import AlertRepo
from ai_munimji_engine.packs.stock_procurement.procurement.agent import ProcurementAgent
from ai_munimji_engine.packs.stock_procurement.procurement.proposals import ProposalRepo
from ai_munimji_engine.packs.stock_procurement.settings import attach as attach_settings
from .stores import stock_stores_for
from ai_munimji_engine.packs.stock_procurement.tools import StockTools

log = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
MANIFEST = stock_manifest()                  # the manifest is in the engine (ai_munimji_engine/packs/stock_procurement/manifest.yaml): the Frappe app reads the same file
NAME = MANIFEST.name
# kinds of approval request that are information only: nobody is reminded about them
NO_REMINDER_KINDS = ("inventory.alert", "procurement.order", KIND_SLOW, KIND_COUNT_PLAN, KIND_COUNT_VARIANCE)
# kinds whose WhatsApp message carries its own buttons: sent as plain messages, never with the default template (its buttons say Create order / Remind me / Reject)
WHATSAPP_PLAIN_KINDS = ("procurement.order", KIND_CHASE, KIND_RECEIPT, KIND_QUOTE_REQUEST, KIND_QUOTES, KIND_SLOW, KIND_COUNT_PLAN, KIND_COUNT_VARIANCE, KIND_TRANSFER)


class StockProcurementPack(Pack):
    manifest = MANIFEST

    def setup(self, app: Any) -> None:
        cfg = app.config
        attach_settings(app.settings, cfg.get("policy"), cfg.get("procurement"), cfg.get("calendar"), cfg.get("followup"))
        stock = stock_stores_for(app.stores)                                # the pack's tables, on the same storage as the core's (SQLite: made here if they are not there)
        app.services["stock_stores"] = stock
        alerts = AlertRepo(stock.alerts, app.clock)
        inventory = InventoryAgent(app.erp, app.settings, alerts, app.approvals, app.workorders, stock, app.stores.transaction, app.clock)
        proposals = ProposalRepo(stock.proposals, app.clock)
        procurement = ProcurementAgent(app.erp, app.settings, proposals, app.approvals, app.workorders, stock.runs, app.stores.transaction, app.clock)
        inventory.order_settled = procurement.order_settled                 # a quiet period ends when its order is done
        procurement.on_order_removed = inventory.end_quiet_period           # and when the order is cancelled
        assistant = AssistantService(stock.assistant, app.settings, app.approvals, alerts, proposals, app.clock)
        chat = ChatService(app.erp, inventory, procurement, proposals, app.approvals, app.settings, stock.assistant, app.clock, limits=app.limits)
        app.services.update(alerts=alerts, inventory=inventory, proposals=proposals, procurement=procurement, assistant=assistant, chat=chat)
        app.services.update(followup=FollowUp(app), stock_insight=StockInsight(app), transfers=StockTransfers(app))
        self._history_context(app)
        app.reschedule_scopes = (*(getattr(app, "reschedule_scopes", None) or ()), "followup")     # changing the follow-up times takes effect without a restart
        inventory.extra_sections.append(app.services["stock_insight"].substitutes.section)       # an approved alternative, when an item is short and cannot come in time
        app.approvals.unknown_sender_handlers.append(app.services["followup"].messenger.take_reply)  # a vendor answering on WhatsApp
        if "whatsapp" in app.channels and hash_key_state() != "ok":
            log.warning("%s is %s: a vendor's WhatsApp reply cannot be recognised until you put a long random value (32+ characters) in .env (see .env.example).",
                        HASH_KEY_ENV, "not set" if hash_key_state() == "missing" else "shorter than 32 characters")
        app.conversation = Conversation(app.approvals, assistant, chat, app.router, synchronous=bool((cfg.get("conversation") or {}).get("synchronous")), limits=app.limits,
                                       memory=app.runtime.memory)

        app.approvals.no_reminder_kinds.update(NO_REMINDER_KINDS)
        for channel in app.channels.values():
            plain = getattr(channel, "plain_only", None)
            if plain is not None:
                for kind in WHATSAPP_PLAIN_KINDS:
                    plain(kind)

        for definition in StockTools(app).definitions():
            app.toolkit.register(NAME, definition.spec, definition.handler)

        tasks = StockAgentTasks(app.agents)
        app.agents.add_tasks(tasks, last=("inventory:brief", "purchasing:review"))
        app.approvals.on_submitted.append(app.agents.on_submitted)          # a second opinion on each new order request, when switched on
        chat.transfers = app.services["transfers"]                            # "move 50 of X from Stores to Finished Goods" prepares a request, like an order
        chat.agent_fallback = app.agents.answer_for_chat
        chat.agent_background = app.agents.chat_background
        # the chat answers the follow-up questions (late deliveries, receipt differences ...) with the same tools, as the person asking (the core checks the role)
        chat.tool = lambda name, args, user: app.toolkit.call(name, args, Actor(user or "chat", frozenset({"viewer"}), "chat"))

    def routes(self, router: Any, ctx: Any, deps: Any) -> None:
        from .api import routes
        routes(router, ctx, deps)

    def jobs(self, scheduler: Any) -> None:
        from .jobs import StockJobs
        scheduler.add_jobs(StockJobs(scheduler))

    def web_dir(self) -> Path:
        return HERE / "web"

    def cli_commands(self) -> list[Any]:
        from .cli import COMMANDS
        return COMMANDS

    def agent_actions(self) -> dict[str, Any]:
        from .cli import AGENT_ACTIONS
        return AGENT_ACTIONS

    def mcp_tools(self, server: Any, run: Any) -> None:
        from .mcp_tools import register
        register(server, run)

    def mcp_instructions(self) -> str:
        from .mcp_tools import INSTRUCTIONS
        return INSTRUCTIONS


    @staticmethod
    def _history_context(app: Any) -> None:
        """What the decision history shows with one of this pack's requests: the purchase request behind it, and the messages sent to vendors (never their address)."""
        app.history.reason_headings.update(("what does not match", "before you pay", "suggested action", "prices quoted", "who would be asked", "items", "count these"))

        def context(rec: Any) -> list[dict[str, Any]]:
            out = []
            prop = app.proposals.by_approval(rec.id)
            if prop is not None:
                out.append({"heading": "Purchase request", "lines": [f"Vendor {prop.supplier_name or '-'}, {prop.qty or 0:g} at {prop.rate or 0:,.2f}, total {prop.total or 0:,.2f}; "
                                                                     f"status {prop.status}" + (f", order {prop.po_name}" if prop.po_name else "") + (f" ({prop.status_note})" if prop.status_note else "") + "."]})
            move = app.transfers.by_approval(rec.id)
            if move is not None:
                out.append({"heading": "Stock transfer", "lines": [f"{move['qty']:g} {move['uom']} of {move['item_code']} from {move['from_warehouse']} to {move['to_warehouse']}; status {move['status']}"
                                                                   + (f", draft {move['entry_name']}" if move["entry_name"] else "") + (f" ({move['status_note']})" if move["status_note"] else "") + "."]})
            sent = app.stock_stores.vendors.messages_for_approval(rec.id)
            if sent:
                out.append({"heading": "Messages to vendors", "lines": [f"{r['sent_at'][:16].replace('T', ' ')}: to {r['supplier']} by {r['channel']}, {r['status']}"
                                                                        + (f" ({r['error']})" if r["error"] else "") for r in sent]})
            replies = app.stock_stores.vendors.replies_for_approval(rec.id)
            if replies:
                out.append({"heading": "Vendor replies (their words)", "lines": [f"{r['received_at'][:16].replace('T', ' ')} {r['supplier']}: \"{r['text']}\"" for r in replies]})
            return out
        app.history.extras.append(context)


PACK = StockProcurementPack()

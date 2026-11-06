"""The stock pack's tools on an MCP server, with typed signatures so every client sees the same parameters and descriptions as our own agents do.

The core's `build_mcp_server` calls `register(server, run)` for each pack that is switched on; `run(name, args)` applies the core's checks (who is calling, the role, the pack
being on, the argument rules) and returns the tool's result or raises a tool error. Needs the optional dependency: pip install "erpnext-ai-agents[agents]".
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from mcp.server import MCPServer
from pydantic import Field

from erp_agents.core.mcp_server import PREPARES, READ_ONLY
from ai_munimji_engine.packs.stock_procurement.tools import SPEC

INSTRUCTIONS = ("Stock Assistant for ERPNext: stock position, usage, vendor comparison and purchase-order status for a small business, plus late deliveries, goods receipt "
                "checks, a vendor scorecard, demand forecasts, slow and dead stock, approved substitutes and cycle counts. "
                "'prepare_order_request' only sends an approval request to the people who approve orders; "
                "use it only when a person asked for an order, and tell them it still needs their answer. "
                "'prepare_quote_request' likewise only sends an approval request: no vendor is messaged until a person presses Send requests. "
                "'prepare_stock_transfer' likewise: it asks for stock to be moved between warehouses, and only a person pressing Create transfer makes a draft in ERPNext.")


def _d(tool: str, prop: str) -> Any:
    """The parameter's description comes from the one place the tools are defined (tools.py), so a client and our own agents see the same words."""
    return Field(description=SPEC[tool].properties[prop]["description"])


def register(server: MCPServer, run: Any) -> None:
    @server.tool(name="stock_overview", description=SPEC["stock_overview"].description, annotations=READ_ONLY)
    async def stock_overview(group: Annotated[Optional[str], _d("stock_overview", "group")] = None, limit: Annotated[int, _d("stock_overview", "limit")] = 10) -> dict[str, Any]:
        return await run("stock_overview", {"group": group, "limit": limit})

    @server.tool(name="stock_status", description=SPEC["stock_status"].description, annotations=READ_ONLY)
    async def stock_status(item_code: Annotated[str, _d("stock_status", "item_code")]) -> dict[str, Any]:
        return await run("stock_status", {"item_code": item_code})

    @server.tool(name="usage_history", description=SPEC["usage_history"].description, annotations=READ_ONLY)
    async def usage_history(item_code: Annotated[str, _d("usage_history", "item_code")], weeks: Annotated[int, _d("usage_history", "weeks")] = 13) -> dict[str, Any]:
        return await run("usage_history", {"item_code": item_code, "weeks": weeks})

    @server.tool(name="vendor_options", description=SPEC["vendor_options"].description, annotations=READ_ONLY)
    async def vendor_options(item_code: Annotated[str, _d("vendor_options", "item_code")], qty: Annotated[float, _d("vendor_options", "qty")]) -> dict[str, Any]:
        return await run("vendor_options", {"item_code": item_code, "qty": qty})

    @server.tool(name="open_orders", description=SPEC["open_orders"].description, annotations=READ_ONLY)
    async def open_orders(item_code: Annotated[Optional[str], _d("open_orders", "item_code")] = None,
                          kind: Annotated[Literal["purchase_order", "requisition"], _d("open_orders", "kind")] = "purchase_order") -> dict[str, Any]:
        return await run("open_orders", {"item_code": item_code, "kind": kind})

    @server.tool(name="waiting_requests", description=SPEC["waiting_requests"].description, annotations=READ_ONLY)
    async def waiting_requests(item_code: Annotated[Optional[str], _d("waiting_requests", "item_code")] = None) -> dict[str, Any]:
        return await run("waiting_requests", {"item_code": item_code})

    @server.tool(name="prepare_order_request", description=SPEC["prepare_order_request"].description, annotations=PREPARES)
    async def prepare_order_request(item_code: Annotated[str, _d("prepare_order_request", "item_code")], reason: Annotated[str, _d("prepare_order_request", "reason")],
                                    qty: Annotated[Optional[float], _d("prepare_order_request", "qty")] = None) -> dict[str, Any]:
        return await run("prepare_order_request", {"item_code": item_code, "qty": qty, "reason": reason})

    @server.tool(name="late_deliveries", description=SPEC["late_deliveries"].description, annotations=READ_ONLY)
    async def late_deliveries() -> dict[str, Any]:
        return await run("late_deliveries", {})

    @server.tool(name="receipt_mismatches", description=SPEC["receipt_mismatches"].description, annotations=READ_ONLY)
    async def receipt_mismatches(days: Annotated[Optional[int], _d("receipt_mismatches", "days")] = None) -> dict[str, Any]:
        return await run("receipt_mismatches", {"days": days})

    @server.tool(name="vendor_scorecard", description=SPEC["vendor_scorecard"].description, annotations=READ_ONLY)
    async def vendor_scorecard(days: Annotated[Optional[int], _d("vendor_scorecard", "days")] = None,
                               vendor: Annotated[Optional[str], _d("vendor_scorecard", "vendor")] = None) -> dict[str, Any]:
        return await run("vendor_scorecard", {"days": days, "vendor": vendor})

    @server.tool(name="demand_forecast", description=SPEC["demand_forecast"].description, annotations=READ_ONLY)
    async def demand_forecast(item_code: Annotated[str, _d("demand_forecast", "item_code")], weeks: Annotated[int, _d("demand_forecast", "weeks")] = 6) -> dict[str, Any]:
        return await run("demand_forecast", {"item_code": item_code, "weeks": weeks})

    @server.tool(name="slow_stock", description=SPEC["slow_stock"].description, annotations=READ_ONLY)
    async def slow_stock(limit: Annotated[int, _d("slow_stock", "limit")] = 10) -> dict[str, Any]:
        return await run("slow_stock", {"limit": limit})

    @server.tool(name="substitutes", description=SPEC["substitutes"].description, annotations=READ_ONLY)
    async def substitutes(item_code: Annotated[str, _d("substitutes", "item_code")]) -> dict[str, Any]:
        return await run("substitutes", {"item_code": item_code})

    @server.tool(name="stock_counts", description=SPEC["stock_counts"].description, annotations=READ_ONLY)
    async def stock_counts(kind: Annotated[Literal["plan", "variances"], _d("stock_counts", "kind")] = "plan") -> dict[str, Any]:
        return await run("stock_counts", {"kind": kind})

    @server.tool(name="decision_history", description=SPEC["decision_history"].description, annotations=READ_ONLY)
    async def decision_history(query: Annotated[str, _d("decision_history", "query")] = "", item_code: Annotated[Optional[str], _d("decision_history", "item_code")] = None,
                               days: Annotated[int, _d("decision_history", "days")] = 90, limit: Annotated[int, _d("decision_history", "limit")] = 5) -> dict[str, Any]:
        return await run("decision_history", {"query": query, "item_code": item_code, "days": days, "limit": limit})

    @server.tool(name="prepare_quote_request", description=SPEC["prepare_quote_request"].description, annotations=PREPARES)
    async def prepare_quote_request(item_code: Annotated[str, _d("prepare_quote_request", "item_code")], reason: Annotated[str, _d("prepare_quote_request", "reason")],
                                    qty: Annotated[Optional[float], _d("prepare_quote_request", "qty")] = None) -> dict[str, Any]:
        return await run("prepare_quote_request", {"item_code": item_code, "qty": qty, "reason": reason})

    @server.tool(name="prepare_stock_transfer", description=SPEC["prepare_stock_transfer"].description, annotations=PREPARES)
    async def prepare_stock_transfer(item_code: Annotated[str, _d("prepare_stock_transfer", "item_code")], qty: Annotated[float, _d("prepare_stock_transfer", "qty")],
                                     from_warehouse: Annotated[str, _d("prepare_stock_transfer", "from_warehouse")], to_warehouse: Annotated[str, _d("prepare_stock_transfer", "to_warehouse")],
                                     reason: Annotated[str, _d("prepare_stock_transfer", "reason")]) -> dict[str, Any]:
        return await run("prepare_stock_transfer", {"item_code": item_code, "qty": qty, "from_warehouse": from_warehouse, "to_warehouse": to_warehouse, "reason": reason})

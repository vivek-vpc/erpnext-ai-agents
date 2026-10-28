# The stock and procurement pack

The only pack with code today. It watches stock and warns before it runs out, compares vendors (price, duties and taxes, delivery time, record) and prepares a
purchase request that a person approves. Nothing is ordered until a person presses **Create order**; that makes a *draft* purchase order (or a draft material
request) in ERPNext with the approver recorded. Everything the user sees is described in the main [README](../../../../README.md); this page is about the pack as
a part of the program.

## What is in it

| Where | What |
|---|---|
| `manifest.yaml` | the declaration: the tools, the ERPNext roles and permissions it needs, 15 kinds of approval request and what answering them may create, the settings it adds, what an agent may send to a language model. The privacy statement, `erp-agents erp-check-access` and the sign-in defaults are built from it |
| `pack.py` | `PACK`, the entry object the core loads. `setup(app)` builds the services and registers them with the core; the other hooks add routes, commands, scheduled work and MCP tools |
| `inventory/` | the stock analysis (maths in `analysis.py`), its plain-English messages, the alert repository and the daily check |
| `procurement/` | vendor scoring (`scoring.py`), taxes and duties, proposals, the procurement pass that turns an approved reorder into a purchase request |
| `assistant/` | the chat (`chat.py`), "Ask why" (`service.py`) and the Telegram / WhatsApp conversation (`conversation.py`) |
| `agents/` | the inventory and purchasing agents' prompts, checks and tasks (`tasks.py`), run by the core's agent runtime |
| `tools.py`, `mcp_tools.py` | the tools (<!-- tools:counts -->17 tools: 14 read-only and 3 prepare-only<!-- /tools:counts -->; `decision_history` is one of the read-only ones; `prepare_order_request`, `prepare_quote_request` and `prepare_stock_transfer` prepare an approval) and their typed form for MCP |
| `followup/` | purchasing follow-up: chasing late deliveries, goods receipt checks, the vendor scorecard, quote rounds, and the messenger that reaches vendors (only after a person presses Send) |
| `inventory/forecast.py`, `slow_stock.py`, `substitutes.py`, `counts.py` | stock intelligence: the forecast order point, slow and dead stock, approved substitutes, cycle counts |
| `inventory/transfers.py` | moving stock between warehouses: the request a person answers and the draft Stock Entry that Create transfer makes |
| `settings.py`, `schema.py` | the settings sections (`policy`, `procurement`, `calendar` and the stock fields of `system`) and the database tables this pack owns, with a schema version |
| `api.py`, `cli.py`, `jobs.py` | REST routes under `/api/v1/inventory`, `/procurement`, `/assistant`, `/chat`, `/agents`; the commands `demo`, `run-once`, `analyze`, `policy`, `alerts`, `procurement` and `agents brief / review / ask`; the daily inventory check, the procurement pass every 3 minutes and the optional morning brief |
| `web/index.html` | the console |
| `tests/` | this pack's tests (over 600). Run them alone with `python -m pytest src/erp_agents/packs/stock_procurement/tests` |

## What the core does for it

The pack does not check roles, gate orders or verify answers itself; the core does, and the pack cannot turn that off:

* every tool call passes the role check in `core/toolkit.py`, whether it comes from the console, an MCP client or an agent;
* an agent job that offers `prepare_order_request` must name a gate that checks the person asked for an order in so many words, and may call it once for the item and quantity they named (`core/agents/service.py`);
* what an agent writes is checked against the tools' results (`core/agents/runtime.py`) and replaced by the program's own text when it fails.

## Switching it off

`packs.enabled.stock_procurement: false` (settings file, or `PUT /api/v1/settings/module/packs`). Its tools, routes, commands and scheduled work answer
"switched off" at once (and the console page is not served when it is off at start-up); approvals, sign-in, the settings and the MCP server's core rules keep working. Its tables and saved
settings stay in the database and come back when it is switched on again (that needs a restart). See `tests/test_pack_switch.py` for exactly what is guaranteed.

## Rules for changing it

* Import the core (`erp_agents.core...`) and this pack only. Never another pack (`tests/test_pack_boundaries.py`).
* A new tool is declared in `manifest.yaml` **and** registered in `tools.py`; the application refuses to start when the two differ.
* A new kind of approval request is declared in the manifest, with the actions a person may press and what answering it may create.
* The ERP is written only through `ERPClient`, only as a draft, with the approver recorded.
* Tests go in `tests/` here; the core's fixtures come from the root `conftest.py`.

More: [docs/DEVELOPING.md](../../../../docs/DEVELOPING.md) ("The structure" and "How to add a pack"), [docs/AGENTS.md](../../../../docs/AGENTS.md).

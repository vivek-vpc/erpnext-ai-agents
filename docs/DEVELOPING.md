# Developing: GitHub now, Frappe Marketplace next

The project is published as open source (GPL-3.0-or-later) and is meant to become a **Frappe app** on the Frappe Cloud Marketplace
([PUBLISHING_PLAN.md](PUBLISHING_PLAN.md) §3). The port is a swap of the *edges*; the *core* is shared. Every change should keep that true. This page says what
the core and the edges are today, the rules for new code, and an audit of where the rules are not met yet.

## What the Marketplace requires (docs.frappe.io, checked 21 Oct 2026)

- The app is open source (MIT or a GPL-compatible licence), hosted on GitHub, and published by its owner. The name must be unique across the Marketplace.
- It supports the **current stable Frappe and ERPNext** version (Frappe Cloud lists Versions 15 and 16 as Stable, 15 the default for new bench groups; v16 needs Python exactly 3.14,
  read from Frappe's own files on 5 Nov 2026 ([FRAPPE_FACTS.md](FRAPPE_FACTS.md) 2); check again before each release). Version-specific code branches on `frappe.__version__`.
- A **passing GitHub Actions (or similar) CI is mandatory.** Frappe recommends running Semgrep with its `frappe/semgrep-rules` locally.
- **Do not override base Frappe functionality** (login and password-reset pages and the like): extend it through hooks.
- A settings DocType for its global behaviour is recommended, not required ([FRAPPE_FACTS.md](FRAPPE_FACTS.md) 3, row 8).
- Listing: short description of 40 to 80 characters (one sentence, not repeating the name), a long description without installation steps, a **square logo of at
  least 200x200 px without text**, one category, five links (**Website, Support, Documentation, Terms of Service, Privacy Policy**), screenshots, and a demo
  video that is prepared and sent on request. The app must not steer users away from Frappe Cloud.
- This project's own rule (it is not on the Marketplace pages): the privacy policy has to say exactly what leaves the site: request summaries to WhatsApp and Telegram, and question text to a language model when the
  assistant is on. For the AI agents and the MCP server that statement is generated from code and tested (`core/agents/privacy.py`, `src/erp_agents/packs/stock_procurement/tests/test_privacy.py`); the
  draft policy to adapt is at the end of [PRIVACY.md](PRIVACY.md). The agent features are off by default, and a model on another machine is refused unless the
  owner allows it, which is what lets the policy say "nothing leaves your site unless you turn it on".

## The structure: a shared core and switchable packs

The code is in two packages of one repository (Phase 2 of the Frappe work). **`ai_munimji_engine`** (`engine/src/`, its own distribution `ai-munimji-engine`, which needs pydantic and PyYAML only) holds the rule logic:
the approval service and the audit seal, the settings store, the tool registry, the stock pack's analysis, scoring, alerts, proposals, tools and agents, the ERP ports, the stores (Protocols with an
in-memory class beside each) and the runtime (jobs, lock, chat memory). **`erp_agents`** (`src/`) is the standalone service built on it: the web application, the scheduler, the command line, the SQLite
stores, the REST client for ERPNext, the channels, sign-in, the AI agents and the console. A module that moved keeps its place in the tree, so `core/audit.py` in the documents is
`erp_agents/core/audit.py` or, for the 36 modules that moved (`tools/move_to_engine.py` lists them), `ai_munimji_engine/core/audit.py`. The engine never imports `erp_agents`
(`tests/test_engine_imports.py` imports every engine module with only the engine on the path and with the web, the database and the network refused).

The tree below is the one before the move; the modules in it that moved are the ones listed in `tools/move_to_engine.py`.

```
src/erp_agents/
  core/                         what every pack shares (nothing in it knows about stock, vendors or purchase orders)
    packs.py                      the manifest model, the pack registry and the Pack base class
    bootstrap.py                  builds the application: core services first, then each enabled pack
    toolkit.py                    the tool registry: roles, effects, one place every tool call passes through
    mcp_server.py, agents/        the MCP server, the agent runtime, the model clients, answer checking, the order gate
    approvals/                    approvals, channels (Telegram, WhatsApp, simple), inbound messages
    auth/, policy.py              who may do what (ERPNext roles), the settings store
    erp/                          the ERPClient port, its ERPNext implementation and a mock
    api/app.py                    FastAPI: sign-in, approvals, settings, /api/v1/packs, and the routes each pack adds
    scheduler.py, cli.py, ops.py  the clock, the command line, backup/restore/doctor
  packs/
    stock_procurement/          the only pack with code today: stock watching, vendor comparison, order requests
      (manifest.yaml                what the pack declares (tools, roles, approvals, settings, what goes to a model): it is in the engine,
                                    engine/src/ai_munimji_engine/packs/stock_procurement/, so that the Frappe app reads the same file; the skeleton packs keep theirs here)
      pack.py                       PACK: the entry object the core loads
      tools.py, mcp_tools.py        the seven stock tools (read and prepare) and their typed MCP form
      agents/, inventory/, procurement/, assistant/    the agents, the analysis, the scoring, the chat
      settings.py, schema.py        the settings sections and database tables this pack adds
      api.py, cli.py, jobs.py       its REST routes, its commands, its scheduled work
      web/index.html                the console
      tests/                        its tests
    sales/, finance/            empty skeletons: a manifest and a README, no code
tests/                          the core's tests, including the boundary tests
```

| Layer | What it holds | In the Frappe app |
|---|---|---|
| **Pure logic** (no web framework, scheduler or SQL; `tests/test_no_sqlite_in_the_domain.py` refuses `sqlite3` to every module that is not about SQLite; `assistant/chat.py` and the agent modules still reach `requests` through the model clients, which a bench provides) | in the stock pack: `inventory/analysis.py`, `procurement/scoring.py`, `inventory/messages.py`, `procurement/messages.py`, `assistant/chat.py` (`understand`, answer building), `agents/inventory_agent.py`, `agents/purchase_agent.py`; in the core: `assistant/facts.py`, `assistant/verify.py`, `approvals/models.py`, `policy.py`, `toolkit.py`, `agents/runtime.py` (the loop and its checks), `agents/privacy.py` | kept as is (shared library) |
| **Ports** (interfaces the logic talks to) | `ai_munimji_engine/core/erp/base.py` (`ERPClient`: is the ERP reachable; `RecoveryERP`: find a document by its reference) and the stock pack's own port `ai_munimji_engine/packs/stock_procurement/erp.py` (`StockERP`, `PurchasingERP`, `FollowUpERP`, `TransferERP`), `core/approvals/channels/base.py` (`Channel`), `core/auth/` (`AuthProvider`), `core/assistant/llm.py` (`LLMClient`), `core/agents/llm.py` (`ToolChatClient`: Ollama, Claude, OpenAI, Gemini; keys come in through `core/config.py` only), the `*Store` Protocols of `ai_munimji_engine.stores` (one per group of tables: SQLite in `erp_agents/stores` and `packs/<name>/stores.py`, memory beside each Protocol), and the classes that take them: `ApprovalService`, `AlertRepo`, `SettingsStore`, `AgentLog` ... | kept; implementations change |
| **Edges** (replaced) | `core/erp/erpnext.py` (REST and API key), `core/storage.py` and the `Sqlite*Store` classes (SQLite), `core/scheduler.py` (APScheduler, Telegram poller), `core/api/app.py` (FastAPI), the pack's `web/index.html` (console), `core/config.py` and `.env`, `core/mcp_server.py` and `core/agents/bridge.py` (MCP over stdio and HTTP), `core/agents/service.py` (threads) | `frappe.get_all/get_doc` in process; DocTypes; `hooks.py` `scheduler_events` and `frappe.enqueue`; whitelisted methods (the MCP endpoint behind Frappe's OAuth provider); Desk workspace and pages; Settings DocType with Password fields |

### What stays in the core, whatever the pack

These are the checks a pack cannot opt out of, because they live in the core and every tool call, route and agent task passes through them:

* **Roles.** `core/toolkit.py` checks the caller's role on every tool call, from REST, MCP or an agent alike. A tool is invisible to a caller who may not use it.
* **The order gate.** An agent job that offers a tool with the `prepare` effect must name a gate, one call per task is allowed, and the role is enforced (`fence_prepare_tools` in `core/agents/service.py`). A job without a gate does not start. Nothing an agent does creates an order: a person answers the approval.
* **Answer verification.** What an agent writes passes `core/agents/runtime.py` (`grounding_problems`) and the job's own rules, and is replaced by a deterministic fallback when it fails. The checking pool is the question plus the results of tool calls that succeeded, never a failed call's arguments.
* **The switch.** `core/toolkit.py`, the routes, the scheduled work and the MCP server answer "switched off" at once when an administrator turns a pack off, with no restart.
* **The manifest check.** When a pack loads, what it registered is compared with what its manifest declares. Any difference (a tool that is declared but not registered, or registered but not declared, with a different role or effect) stops the application, so the privacy statement, the permissions list and the README can trust the manifest.

### Boundary rules (tested)

1. **The core never imports a pack.** It finds packs by reading their `manifest.yaml` files and loads one only through `erp_agents.packs.<name>.pack:PACK`.
2. **A pack imports only the core and itself.** Never another pack. If sales needs a stock figure, it asks for it as a tool call through the core's registry.
3. **The core does not hard-wire the stock pack's names.** The approval kinds, tool names and table names the stock pack owns (`inventory.alert`, `stock_overview`, `agent_runs`, `chat_log` and the like) must not appear in core code; the pack registers them. The ERP port is no longer an exception: the stock pack's methods and types are in its own port, and `tests/test_erp_port.py` checks that the core's `ERPClient` names none of them.
4. **A skeleton pack has no code.** `sales` and `finance` hold a manifest and a README only.

`tests/test_pack_boundaries.py` checks all four by reading the source (rules 1, 2 and 4 by parsing the imports, rule 3 by searching for the banned names), so a violation fails the suite; `tests/test_pack_switch.py` proves the switch; `tests/test_order_gate.py` the gate.

## Rules for new code

1. **No SQL outside a store.** A store is a Protocol in `ai_munimji_engine.stores` that owns one group of tables and speaks the domain's words (`ApprovalStore`, `AlertStore`, `ProposalStore` ...);
   its SQLite class lives in `erp_agents/stores/` (the core) or `packs/<name>/stores.py` (a pack), with the SQL unchanged, and a `Memory*Store` beside the Protocol keeps the same rows in dicts.
   `ApprovalService`, `AlertRepo`, `ProposalRepo`, `NoticeRepo`, `WorkOrderQueue`, `SettingsStore`, `AgentLog` and the rest take a store, never a database. The Frappe app gives them stores over
   DocTypes, so a stray `db.execute` somewhere else is work nobody will find: `tests/test_no_sqlite_in_the_domain.py` fails if any module that is not about SQLite reaches `sqlite3`,
   and `tests/test_store_contracts_core.py` and the pack's `test_store_contracts_stock.py` run every store on SQLite and in memory. A new store comes with its Protocol, both classes and a contract test.
2. **The core imports no `fastapi`, `requests`, `apscheduler`, `sqlite3` or `threading`.** Talk to the world through a port.
3. **A job is a plain function** (`run(...)`) that does one pass and returns. It never sleeps or loops. The scheduler decides when it runs (APScheduler today,
   `scheduler_events` and `frappe.enqueue` in Frappe). Work that can take long (a model call, a message send) must be possible to put on a queue: do not add
   new in-process worker threads or in-memory state that must survive between calls.
4. **Webhooks first.** Frappe Cloud has public HTTPS, so inbound messages arrive by webhook. Polling (Telegram) is for the standalone service only.
5. **Settings and secrets come in through the settings object**, never `os.environ` in application code (only `core/config.py` reads the environment), and are never
   logged. In Frappe they become a Single DocType with Password fields.
6. **Permissions are ERPNext's.** Roles come from the auth provider and from ERPNext's own permission table; do not add a user table. In Frappe this becomes
   `frappe.has_permission`.
7. **The ERP is written only through `ERPClient`, and only as a draft with the approver recorded.** Custom fields the app needs are declared in one list, which
   becomes fixtures or a patch in the Frappe app.
8. **User-facing text is built in the message modules**, in plain English, so one translation layer (`_()`) can wrap it later. No sentences assembled inside
   routes. Numbers come from code and ERP data.
9. **Extend, never override.** Anything in the Frappe version that Frappe already provides (sign-in, permissions, notifications, background jobs) uses Frappe's own.
10. **Dependencies must be GPL-compatible and installable on Frappe Cloud** (pure Python or wheels); nothing that needs a local service. A local model (Ollama) is
    an optional adapter, off by default.
11. **Tests stay in two groups**: pure-Python tests for the core and the ports (they keep running everywhere, and in CI on Linux and Windows) and, later, Frappe
    integration tests. A change without a test does not merge.
12. **No personal data or real addresses** in code, tests, docs or logs: the CI scan fails on them.
13. **What an agent may know or do is a tool registered in `core/toolkit.py` (a pack's tools are in its `tools.py` and listed in its manifest), and nothing else.** A tool has a role (what ERPNext lets the caller do) and an effect: it reads, or it
    prepares an approval request for a person. No tool orders, approves, pays or changes a setting. Its result carries no customer or staff personal data and fits
    a small model's context (`src/erp_agents/packs/stock_procurement/tests/test_toolkit.py`, `src/erp_agents/packs/stock_procurement/tests/test_privacy.py`). The MCP server and our own agents publish and use the same list.
14. **A model's words are checked, its numbers are never trusted.** Anything an agent writes passes `core/agents/runtime.py` (`grounding_problems`: numbers and codes must
    be in the tools' results) and the job's own rules, and has a deterministic fallback that is shown instead when it fails. The part of an answer that decides
    something (the verdict, the table, the order of items) is computed by the program; the model words and comments.

15. **An ERP read that a connection may not offer is optional.** The follow-up reads (receipts, open sales orders, supplier contacts, item alternatives, stock counts, stock values, e-mail through the ERP) are a separate protocol, `FollowUpERP` in the stock pack's ERP port (`ai_munimji_engine/packs/stock_procurement/erp.py`), called through `getattr`; a feature whose read is missing says so instead of guessing, and an older or smaller ERP client keeps working.
16. **A pack depends only on the core.** It declares its tools, approvals, settings and ERPNext roles in its manifest, keeps its tests in its own folder, and never imports another pack. The core never imports a pack. See "How to add a pack" below.

17. **What someone may need to follow is visible, and nothing private is in it.** A new log line about what the assistant did goes through `obs.event` (names, numbers, outcomes: never a question, an answer, a prompt, a document or a person); a new metric is registered in `core/metrics.py` with a bounded set of label values; a new span is `tracing.span` with names and counts; a new scheduled job is registered through `scheduler.tracked` and returns `False` when it failed. Never log a secret on the assumption that the scrubber will catch it. These modules import no web or scheduler library. The tables in [OBSERVABILITY.md](OBSERVABILITY.md) are compared with the code by a test (`tests/test_obs_docs.py`), so a metric, event or span that is not documented fails the build.

18. **Whatever depends on another system fails safe, and says so.** A new HTTP dependency gets a circuit breaker (`guard_session` in `core/httpguard.py`); a new kind of request whose answer creates something elsewhere registers a `reconcile` hook (`KindHandler`), so an answer the program stopped before writing down is finished at the next start; a new channel gives every `InboundEvent` the id its provider assigned (`external_id`); a new action that starts a model task is limited (`model_limit`); and a client reads what a server sends as untrusted input (`core/agents/llm.py`: `_dict`, `_list`, `_text`, `_count`). The tables in [RESILIENCE.md](RESILIENCE.md) are compared with the code by `tests/test_resilience_docs.py`, and every test it names must exist.

19. **A new way in is decided, tested and listed.** A new route has a row in the table of routes of [SECURITY_CHECKS.md](SECURITY_CHECKS.md) (who may use it) or the build fails (`tests/test_routes_sweep.py`); text from outside (a name, a title, a question, a tool's result) reaches a language model only through `core/prompting.py`, never pasted into a prompt, and a new path of that kind gets a case in `tests/evals/cases/15_adversarial.yaml`; a value put into the console's markup goes through `esc()`; and the app never implements its own login in the Frappe port (the Marketplace asks that base behaviour is not overridden). Each security check has a row in the register with the test that proves it and the Frappe equivalent, and the register is compared with the code by `tests/test_security_register.py`.

## Audit (21 Oct 2026): where the rules are not met yet

(Paths are as of the restructure of 25 Oct 2026: the stock files are under `packs/stock_procurement/`, the shared ones under `core/`. Nothing in the audit changed with the move.)

SQL outside a store (rule 1). The first audit said "about a dozen calls"; the measurement of 5 Nov 2026 (a script over the syntax tree, [FRAPPE_FACTS.md](FRAPPE_FACTS.md) 6.2) found **180 SQL calls in 27 modules**, 36 modules that
import `sqlite3`, and 19 groups of tables, most with their SQL inside a service class (`ApprovalService` held 25 calls, `AuditChain` 12, `DeliveryChaser` 10). **All of it is now behind stores** (Phase 2 of the Frappe work):

| Group of tables | Protocol (`ai_munimji_engine.stores`) | Used by |
|---|---|---|
| `approvals`, `deliveries` | `ApprovalStore` | `ApprovalService`, the decision history, the monitor-down check |
| `audit_chain` (reads the other two) | `AuditStore` | `AuditChain` |
| `settings_overrides`, `settings_audit` | `SettingsOverrideStore` | `SettingsStore`, the decision history |
| `agent_tasks`, `tool_calls` | `AgentLogStore` | `AgentLog` |
| `work_orders`, `job_runs`, `inbound_seen` | `WorkOrderStore`, `JobRunStore`, `InboundSeenStore` | `WorkOrderQueue`, `JobLog`, `InboundLedger` |
| `alerts`, `stock_notices`, `proposals`, `agent_runs` | `AlertStore`, `NoticeStore`, `ProposalStore`, `AgentRunStore` (`stores.stock`) | `AlertRepo`, `NoticeRepo`, `ProposalRepo`, the two agents, the runs route |
| `assistant_answers`, `chat_log` | `AssistantStore` | `AssistantService`, `ChatService` |
| `vendor_messages`, `vendor_replies`, `delivery_chases`, `receipt_checks`, `quote_rounds` | `VendorStore`, `ChaseStore`, `ReceiptStore`, `QuoteStore` | the follow-up (`VendorMessenger`, `DeliveryChaser`, `ReceiptChecker`, `QuoteRounds`) |
| `stock_transfers`, `cycle_counts`, `count_reviews` | `TransferStore`, `CountStore` | `StockTransfers`, `CycleCounts` |

What still touches SQLite directly is about SQLite itself: `core/storage.py` (the schema and its migrations), `core/ops.py` (backup, restore, verify), `core/doctor.py` (reads the file read-only) and the
`Sqlite*Store` classes. The list is closed and tested.

Threads and in-memory state (rules 2 and 3). **Behind interfaces since Phase 2 of the Frappe work** (`ai_munimji_engine/runtime.py`, built into the application as `app.runtime`): background work is a
`JobRunner` (`ThreadJobRunner` for an agent task and the second opinion, `PoolJobRunner` for the conversation's two workers, `InlineJobRunner` for tests and for a caller that is itself a job); the
one-agent-at-a-time lock is an `Exclusive`; what a person was just asked in the chat and which items their last answer was about are a `ConversationMemory`. A Frappe site gives them `frappe.enqueue`,
a lock in the database or cache, and the cache (the task row already exists before the work starts, so polling by task id carries over unchanged). `tests/test_runtime.py` fails if any module other
than the runtime and the scheduler starts a thread. What is left, and stays with the standalone service: the sign-in sessions (`core/auth/sessions.py`: a Frappe site has its own), the SQLite lock
(`core/storage.py`) and the scheduler with its Telegram poller (a site has the scheduler of Frappe and webhooks). (The chat throttle and the per-chat message limit, once two private deques, are
since Phase 3 one `Limits` in `core/resilience.py` behind a `RateLimiter` interface, and the circuit breakers keep their state in a `BreakerStore`: a cache can replace either; see [RESILIENCE.md](RESILIENCE.md).)

HTTP libraries (rule 2) are confined to the edges: `core/erp/erpnext.py`, `core/auth/erpnext.py`, the Telegram and WhatsApp channels and `core/assistant/llm.py`. `fastapi` is
imported only by `core/api/app.py`, `apscheduler` only by `core/scheduler.py`, and the environment is read only by `core/config.py`: those rules hold.

## How to add a pack

A pack is a folder under `src/erp_agents/packs/` that adds one area (sales, finance, a second stock feature set) without touching the core or another pack. The stock pack is the worked example; `packs/sales/` and `packs/finance/` are the empty starting points.

1. **Make the folder** `packs/<name>/` (lower-case, letters, digits and underscores) with an `__init__.py`, a `manifest.yaml` and a `README.md`.
2. **Write the manifest** (`manifest.yaml`). It is read without importing the pack, and a mistake stops the application with the file named. Fields:
   * `name` (the folder name), `version`, `title`, `summary`, `status` (`active`, or `skeleton` for an empty folder), `requires_core: ">=0.1"`, `enabled_by_default`, `depends_on: []` (must stay empty: a pack cannot depend on another pack).
   * `tools`: each with `name`, `role` (`viewer`, `approver` or `admin`) and `effect` (`read` or `prepare`). `prepare` means it only prepares an approval request for a person. No tool orders, approves, pays or changes a setting.
   * `erp_roles`, `erp_permissions` (what the ERPNext service account must be able to do), `erp_look` and `erp_decide` (what a person needs in ERPNext to look at the screens and to answer requests).
   * `approvals`: each kind of approval request with the `actions` a person may press, the `role` that may, a `description` and what answering `creates` in the ERP.
   * `settings`: the sections the pack adds (`section`, `description`).
   * `sends_to_model`: plain sentences about what an agent of this pack may send to a language model. The core builds the privacy statement from them.
3. **Write `pack.py`** with `PACK = <YourPack>()`, a subclass of `erp_agents.core.packs.Pack` with `manifest = load_manifest(HERE / "manifest.yaml")`. Implement only the hooks you need; each has a do-nothing default:

   | Hook | Use it to |
   |---|---|
   | `setup(app)` | build your services on `app.erp`, `app.stores` (the storage: your own stores are built over `app.stores.backend`, see `packs/stock_procurement/stores.py`), `app.settings`, `app.approvals`, `app.clock`; register settings (`app.settings.register_section`, `extend_system`, `register_scoped`), approval kinds and handlers (`app.approvals.register_handler`), tools (`app.toolkit.register(pack, spec, handler)`) and agent tasks (`app.agents.add_tasks`); expose services as `app.services[...]` |
   | `routes(router, ctx, deps)` | add REST routes. The router answers 404 once the pack is off, after the usual sign-in check; use `deps.require(role)` for roles |
   | `mcp_tools(server, run)` | publish the tools over MCP with typed signatures; each calls `run(name, args)`, which applies the core's role checks |
   | `mcp_instructions()` | a sentence or two for AI clients about what the tools are for |
   | `cli_commands()` / `agent_actions()` | add `erp-agents <command>` and `erp-agents agents <action>`; start a handler with `pack_app(args, <name>)`, which explains when the pack is off |
   | `jobs(scheduler)` / `on_settings_change(...)` | add scheduled work; a job returns at once when `app.packs.is_enabled(<name>)` is false |
   | `web_dir()` | a folder with an `index.html` the core serves as the console |
   | in `setup(app)`: `app.history.extras`, `app.history.reason_headings` | what the decision history shows with one of your requests (the record behind it), and which of your section headings say why |

4. **Rules the core enforces for you.** Do not re-implement them, and do not try to bypass them:
   * Role checks on every tool call, the order gate for `prepare` tools, the grounding check on every agent answer and the switch all live in the core. A job that offers a `prepare` tool without a gate does not start.
   * Write to the ERP only through your pack's own ERP port (a Protocol of the kind of `StockERP` and `PurchasingERP`; the core's `ERPClient` is only "is it reachable"), only as a draft, with the approver recorded.
   * Store data through your own stores (rule 1). On SQLite the pack's tables are registered with `register_schema` (a version number and migrations; a database written by a newer version of your pack is refused, untouched) when its SQLite stores are built.
   * Settings and secrets come in through the settings object, never `os.environ`.
5. **Put the tests next to the pack** in `packs/<name>/tests/`. The root `conftest.py` provides the shared fixtures (`make_app`, `clock`, the mock ERP); `pyproject.toml` already puts every pack's `tests/` folder on the test path. Run the pack alone with `python -m pytest src/erp_agents/packs/<name>/tests`.
6. **Run the checks** in "Security checks" below. `tests/test_packs.py` and `tests/test_pack_boundaries.py` fail if the manifest is invalid, if what you register differs from what you declare, or if you import another pack.
7. **Switch it on.** A pack found in the folder is off unless its manifest says `enabled_by_default: true`. An administrator turns it on or off in the settings (`packs.enabled`, also `PUT /api/v1/settings/module/packs`); turning a pack off takes effect at once, turning one on needs a restart, and `GET /api/v1/packs` shows which state each pack is in. A pack's data stays in the database while it is off.

## Not done yet (Phase 2 left these on purpose)

* **The ERP implementations are still shaped by the stock pack.** `ERPNextClient` and `MockERP` implement all of the stock pack's port in one class each (the interface was split in Phase 2 of the Frappe work, the implementations were not): a sales or finance pack will add its own port beside the stock one, and its methods to those classes or to classes of its own.
* **One kind of scoped setting** ("policy": global, item group, item) is supported, with its old names kept so saved values still load.
* **One conversation handler per application** (Telegram and WhatsApp messages go to the pack that provides it). A second chat-capable pack needs a small router.
* **The console is one page** owned by the stock pack. A second pack with screens needs a place in the console's navigation.
* **The WhatsApp heading list and the Telegram start text** still mention stock.

## Security checks (run before every commit)

```powershell
python -m pytest -q                     # the whole suite: the core's tests in tests/ and each pack's in its own tests/ folder
python -m bandit -r src -q -x "src/erp_agents/packs/*/tests/*"   # static security scan of the program (not the tests): must report no issue
python -m pip_audit                     # known vulnerabilities in the installed dependencies
```

Install the tools with `pip install -e ".[dev,agents]"`. CI runs all three. The pack tests live inside `src/`, so the bandit command leaves them out (a test is full of `assert`, which bandit's B101 flags); the scan covers exactly the code that ships, as it did when all tests sat in `tests/`. Rules:

* A bandit finding is fixed, or silenced on that line with `# <why it is safe>  # nosec Bxxx` (the reason goes first: bandit reads everything after `nosec`). Never silence a rule for a whole file or turn one off.
* A new place that sends a credential (an API key, a token, a password) must use a session that does not follow redirects (`core.netguard.no_redirects`) and send it only to the host it belongs to.
* Text from outside (a person, a model, the ERP) goes through `core.common.untrusted` before it is put into an error message or a log line; free-text tool arguments have a length cap.
* A security fix comes with a regression test, and the test is checked by putting the old behaviour back and seeing it fail.

## Before the first public push (status in [GITHUB_READINESS_REVIEW.md](GITHUB_READINESS_REVIEW.md))

Done: `LICENSE` (GPL-3.0), SPDX licence in `pyproject.toml`, complete `.gitignore`, `SECURITY.md`, `CONTRIBUTING.md`, CI workflow, issue and pull-request templates,
README brought up to date, personal data removed from the docs and tests (the machine-specific pen-test report is kept outside the repository), the
Python 3.10/3.11 `policy set` bug fixed.

Needs the owner: the GitHub account or organisation, author name and contact for `pyproject.toml`, whether `ui-design/` may be published, `git init` and the first
commit (read `git status` first), branch protection, and a decision on the AUTH-02 service-account roles.

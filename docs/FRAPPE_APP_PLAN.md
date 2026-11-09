# Plan: a Frappe app version (Frappe Cloud Marketplace)

**Status: a plan, corrected on 5 Nov 2026 against a real bench. No Frappe app is released.** The Frappe side so far is a throw-away spike in a separate repository (`AI-Munimji`, branch `frappe/1-spike`, nothing published) that was run on Frappe 16.55.2 and ERPNext 16.53.0; this repository holds no `hooks.py`, no DocTypes and no Frappe CI. This repository is a **standalone Python service** (FastAPI, SQLite, an in-process scheduler) that talks to ERPNext over its REST API. It is **not** a Frappe app, it has **not** been submitted to the Frappe Marketplace, and it cannot be installed from the Marketplace. Licence: GPL-3.0-or-later (which the Marketplace accepts); that is a licence statement, not a listing.

Written 25 Oct 2026 and updated the same day for the **core + packs** layout ([DEVELOPING.md](DEVELOPING.md)). The Marketplace and Frappe version facts come from `docs.frappe.io` as read on 21 Oct 2026 and again on 5 Nov 2026; they change, so re-read the official pages before each release. Corrected on 5 Nov 2026 (Phase 2) with [FRAPPE_FACTS.md](FRAPPE_FACTS.md) (Phase 0: the documents read) and [FRAPPE_SPIKE_REPORT.md](FRAPPE_SPIKE_REPORT.md) (Phase 1: a bench); the decisions taken are in [FRAPPE_DECISIONS.md](FRAPPE_DECISIONS.md). A statement here that says "not tested" or "unverified" has not been run on a bench, and the only versions tried are Frappe 16.55.2 and ERPNext 16.53.0.

## 1. Where the boundary is

| | Standalone beta (this repository, today) | Frappe app (future, separate project step) |
|---|---|---|
| What it is | a Python service you run beside ERPNext | a bench app installed into an ERPNext site |
| Talks to ERPNext | REST with a service account's API key | in-process as the signed-in user: `frappe.get_doc(...).insert()` for writes, `frappe.get_list` or an explicit permission check for reads (`frappe.get_all` ignores permissions: it is for the job that runs as Administrator only) |
| Stores its data | its own SQLite file | DocTypes in the site's database |
| Runs scheduled work | APScheduler thread inside `serve` | `scheduler_events` and `frappe.enqueue`; one run per scheduler tick (4 minutes), as Administrator |
| Users and permissions | ERPNext sign-in over REST, roles copied into a session | Frappe's own session, roles and DocType permissions |
| Installed by | `pip install`, a config file, `.env` | `bench get-app` / `bench install-app`, or Frappe Cloud |
| Secrets | `.env` | Password fields in a Settings DocType |
| Review | none | the Frappe Marketplace team reviews the app and each release |

The standalone service stays useful on its own (it is how the live tests were done and how a business without Frappe Cloud runs it). The Frappe app would **reuse the business logic** and **replace the edges**; it would not wrap the service. Do not describe the standalone service as Marketplace-ready, installable from the Marketplace, or "a Frappe app in progress".

## 2. How the core and the packs become a Frappe app

The standalone code is a shared **core** and switchable **packs** (`src/erp_agents/core`, `src/erp_agents/packs/<name>`). The port keeps that split; it changes what is underneath.

**Decided (25 Oct 2026): one app, one Frappe module per pack (option A below).** The comparison is kept for the record.

| | A. One Frappe app, one Frappe *module* per pack (recommended first) | B. A core app plus one app per pack (`required_apps`) |
|---|---|---|
| Marketplace | one listing, one review, one name | a listing and a review per app; the pack apps cannot be installed without the core app |
| Fit with the code | core and each pack stay separate Python packages inside the app; the boundary tests keep applying | the same split, enforced by Frappe's own dependency |
| Switching a pack | a row in the settings; the pack's screens and methods answer "switched off" | install or uninstall the pack's app |
| Cost | the empty `sales` and `finance` skeletons are not shipped until they have code | more repositories, more CI, more reviews |

The owner chose A. Because core and packs are already separate packages that never import each other, moving to B later is a packaging change, not a rewrite.

**What the manifest becomes.** A pack's `manifest.yaml` stays the single declaration; a sync step on install and migrate (`after_install`, `after_migrate`) reads it and keeps the Frappe side in line.

| Manifest field | In the Frappe app |
|---|---|
| `name`, `version`, `enabled_by_default` | a row in a **Pack** table of the core Settings DocType (name, version, enabled); the pack's workspace and its whitelisted methods follow the flag, as the routes do today. Built only if version 0.1 needs it (decided in Phase 3); the spike did not build it |
| `tools` (name, role, effect) | whitelisted methods and the MCP endpoint, through the same tool registry; the role check stays in the registry and is made with `frappe.has_permission` |
| `erp_roles` | Role records created in `before_install` (the DocTypes' permission rows name them, so they must exist before the DocTypes are synced). The Munimji roles protect only the app's own DocTypes; what ERPNext holds is protected by ERPNext's own roles (Stock User, Purchase User, Purchase Manager), named in the install notes, and the app ships no permission rows on ERPNext's DocTypes. The permissions the app needs are no longer a service-account list (it runs as the signed-in user), so `erp_permissions` becomes documentation and a test |
| `erp_look`, `erp_decide` | the (DocType, permission) pairs behind "may look" and "may answer": `read` on Item or Purchase Order, `create` on Purchase Order |
| `approvals` (kind, actions, role, creates) | the allowed values and actions of the **Approval Request** DocType; the `creates` text is shown on the button |
| `settings` (sections) | one Single DocType per pack for its sections (for the stock pack: policy, procurement, calendar and the stock fields of the system section); scoped overrides (item group, item) become a small DocType |
| `sends_to_model` | unchanged: the privacy statement is generated from it and tested |

**Roles.** Today a person is a *viewer* if ERPNext lets them read an Item or a Purchase Order, an *approver* if it lets them create a Purchase Order, and an *admin* if they are Administrator or hold System Manager (`core/auth/erpnext.py`). In Frappe these are checks on the signed-in user: `frappe.has_permission("Purchase Order", "create")` for approver and System Manager for settings. The Munimji roles alone do not let anyone look at stock: a user with only Munimji Viewer cannot read Item, Bin or Stock Ledger Entry, so "may look" needs ERPNext's own roles as well (the standalone's "derived from ERPNext" has no direct equivalent). v16 has custom Permission Types: "approve" is a column in the Approval Request's permission rows, granted per role and checked with `frappe.has_permission(doc, "approve")`. A `has_permission` hook on the DocType adds the Purchase Order condition from the pack's `erp_decide` pairs; it can only deny, and its keyword argument is `ptype` (the documentation's `permission_type` is never filled in). A permission row grants every right that is not switched off (write, create, delete, share, print and email default to 1), so every right is written out in each row and a test pins each role. The role checks, the order gate and the answer verification stay in the core's registry and apply to every pack, exactly as now.

**DocTypes (built in Phase 4b: 25 of them, table and rules in the app's `docs/STORES.md`; where it differs from the paragraph below, it says so there and here).** The differences from this plan: the approval deliveries are their own DocType (the engine addresses a delivery by its number and looks messages up across requests), not a child table; the audit seal is appended by the scheduled job in its system mode, not by a person's session; a record is saved only by the app's stores (a plain API call cannot edit a request's status); a Lock DocType serialises the making of a document from a request. The paragraph as planned: Core: Agent Settings (Single, with the Pack table, channel and agent sections, Password fields), Approval Request with an Approval Delivery child table, Work Order, Agent Task, Tool Call. Stock pack: Inventory Alert, Stock Notice, Purchase Proposal, Chat Log, Assistant Answer, Agent Run, Stock Policy Override, and (since the follow-up features) Vendor Message, Vendor Reply, Delivery Chase, Receipt Check, Quote Round, Cycle Count and Count Review, plus its Single settings. In Frappe a delivery reminder can be a Communication on the Purchase Order directly, and a quote round maps naturally onto ERPNext's own Request for Quotation. These are the tables in `core/storage.py` and `packs/stock_procurement/schema.py`. The settings audit trail (`settings_audit`) is replaced by the version log on the Single DocTypes (checked on the bench: a Password value is stored encrypted and recorded masked, never in clear; but the version log is off whenever tests run, unless `save(ignore_version=False)`, so a test of the history has to turn it on). The decision history (`core/history.py`) reads Approval Requests the same way; its seal (`audit_chain`) becomes an append-only Audit Seal DocType without delete permission for anyone, next to Frappe's own Version log (not built in the spike). The per-pack schema version (`pack_schema`) and its migrations become entries in `patches.txt`.

## 3. hooks.py: what it would contain

Each row was tried in the Phase 1 spike unless it says otherwise ([FRAPPE_SPIKE_REPORT.md](FRAPPE_SPIKE_REPORT.md) section 5); the target is Frappe 16.

| Hook | Purpose |
|---|---|
| `app_name`, `app_title`, `app_publisher`, `app_description`, `app_email`, `app_license` | identity; nothing in `bench` validates the licence string (`GPL-3.0-or-later` installed and migrated without complaint); the name is decided in [FRAPPE_DECISIONS.md](FRAPPE_DECISIONS.md), with a trademark check before the public listing |
| `required_apps = ["erpnext"]` | declares the dependency on ERPNext; `pyproject.toml` must also carry `[tool.bench.frappe-dependencies]` (`bench validate-dependencies` enforces it, Frappe Cloud requires it, and Frappe's own template does not write it) |
| `before_install`, `before_uninstall` (and `after_migrate`, only if a manifest sync is built) | `before_install` creates the roles (they must exist before the DocTypes are synced); `before_uninstall` removes the roles, the custom fields, the scheduled-job record and the Permission Type (Frappe deletes a Permission Type only in developer mode, install, migrate or tests, so the hook sets a flag around the call). The database columns the app added are never dropped, and the uninstall page says so |
| `fixtures` | the custom fields the app needs on Purchase Order, Material Request and Stock Entry (`custom_agent_ref`, approver, note), as `Custom Field` records filtered to this app, replacing `erp-agents erp-setup`; tried: `bench export-fixtures` wrote `fixtures/custom_field.json` and the install imported it (fields on Material Request and Stock Entry not tried) |
| `scheduler_events` | a `cron` entry or two for the stock check, the procurement pass, reminders and expiry. **Nothing runs more often than the scheduler tick, 4 minutes (240 s)**: entries set to every minute, 2 minutes and 5 minutes all ran once per tick, so the procurement pass is every 4 minutes and the standalone service's 3-minute pass has no equivalent. Jobs run as Administrator. A schedule that an administrator edits (`check_cron`, the brief) is a Scheduled Job Type linked to a Scheduler Event: it survives `bench migrate` and can be edited, while a plain one made from `hooks.py` is reset or deleted by the next migrate |
| `doc_events` | optional: react to a Purchase Order submitted or cancelled by hand, instead of polling the ERP (not tried) |
| `has_permission`, `permission_query_conditions` | the Approval Request rule above (`has_permission` works, keyword `ptype`, deny only); each person sees the requests they may answer (`permission_query_conditions` not tried) |
| `app_include_js` / workspace and page files | the Desk screens (inbox, stock monitor, vendor comparison, Ask, settings) per pack. A form's script sits next to its DocType and needs no `app_include_js`; pages and dashboards were not tried |
| `extend_bootinfo` (`boot_session` is still a hook in v16 too) | optionally tell the browser which packs are on; `extend_bootinfo` is the documented one |
| whitelisted methods (`@frappe.whitelist`) | the tools, the approval answers, the chat; `@frappe.whitelist(methods=["POST"])` is what the approval button calls (tried: 200 for the approver, 403 for the viewer, 417 for a second press); `allow_guest=True` only for the WhatsApp and Telegram webhooks, each with its signature check (not built, and not in version 0.1) |

It must not override base Frappe behaviour (login, password reset).

## 4. Installing, running and uninstalling (a development bench, then a site)

Commands are bench's; each was run in the Phase 1 spike unless it says otherwise. Frappe 16 needs Python 3.14 (exactly), Node 24 and MariaDB 11.8.

1. **Develop**: set up a bench with the target Frappe and ERPNext (a dev container works), `bench get-app <repository-url> --branch <branch>` (for a local working copy `bench get-app --soft-link <path>`, and the directory must be named like the app), `bench --site <site> install-app <app>`, then `bench --site <site> migrate` after every change that touches DocTypes or patches, `bench build` for the Desk scripts, `bench start` to run web, workers and scheduler together.
2. **Configure**: open Agent Settings in the Desk; enter the channel tokens and a hosted-model key in the Password fields; switch the packs you want. No `.env`, no settings file, no service account key.
3. **Test**: `bench --site <test site> run-tests --app <app>`, on a separate site with `allow_tests` set; ERPNext builds its whole test data (company, warehouses, suppliers, items) the first time its test module is imported, about 250 s once per test site and 10 to 26 s for the tests after that. Plus Semgrep with Frappe's rules, `bench validate-dependencies`, bandit and pip-audit.
4. **Self-hosted site**: the same `get-app` and `install-app`, then `bench --site <site> migrate` and a restart of the bench processes.
5. **Frappe Cloud**: add the app to a private bench from the repository first (this is the trial the Marketplace asks for), install it on a site, and only then apply for the listing. Not tried. New bench groups default to Version 15 (v16 is listed as Stable), so a v16-only app needs a v16 bench group. Each push to the app's branch creates a release, and the Audit Report runs Semgrep.
6. **Upgrade**: a new release is a git tag plus patches; `migrate` runs the patches in `patches.txt` once each.
7. **Uninstall**: `bench --site <site> uninstall-app <app>` removes the module, the DocTypes and their tables (so their data goes with the app); `before_uninstall` removes what Frappe leaves behind: the roles, the custom fields, the scheduled-job record and the Permission Type. **The database columns the app added stay** (here: the two columns on Purchase Order with their unique index, and three `approve` columns on the permission tables), because Frappe never drops a column. The uninstall page says what goes with the app, what stays, and points to an export.

## 5. How the standalone service and the Frappe app live side by side

They must never both run the stock pack against the same ERPNext site: each would raise the same alerts and create its own draft purchase orders for the same item.

* **Different sites or a trial**: the standalone service keeps working as today. A site can try the app without the service touching it.
* **Moving a site over**: stop the service (or set `packs.enabled.stock_procurement: false`, which also stops its scheduled work and tools), take `erp-agents backup`, install the app, then import. The import does not exist: a one-time command that reads the SQLite backup and creates the Alert, Approval Request and Proposal records is part of package 4. Until then a move starts with an empty history; open approvals must be answered or dismissed first.
* **Orders stay recognisable**: the documents the service creates carry the request's reference in a custom field (`custom_agent_ref`), and changing or cancelling an order is refused unless the field matches the request, so an order made by hand is left alone. The app would use the same field. A request answered in the service can be changed or cancelled from the app only if the import keeps the request references; otherwise those orders are treated as made by hand.
* **A thin "Desk client" for the service** (a Frappe app that only shows the service's approvals in the Desk and calls its REST API) is possible but is **not** what the Marketplace wants: the app must work on Frappe Cloud without a separate server, and must not steer people away from it. Treat it as a transition aid for self-hosters, not a deliverable.
* **Not shared**: the SQLite file, the `InstanceLock`, and the sign-in sessions. The standalone service's single-instance rule (`serve` takes a lock on its database) says nothing about a Frappe site, so the coexistence rule above is a procedure, not something the code enforces.

## 6. What a Frappe app needs, and what is missing

| Needed | Why | State today |
|---|---|---|
| A bench app package: the Python module, `modules.txt`, `hooks.py`, `patches.txt`, a `config/` folder, a `pyproject.toml` with `[tool.bench.frappe-dependencies]` for Frappe and ERPNext (mandatory on Frappe Cloud; `frappe` must not be listed under `dependencies`; apt packages go in `[tool.frappe-cloud]`) | `bench` cannot install anything else | `bench new-app` writes all of it except the dependencies section, and still writes the old apt table; a throw-away spike exists in `AI-Munimji` |
| **Settings DocTypes** (Single) with Password fields | recommended by the Marketplace pages, not required; replaces `settings.yaml` and `.env` | the settings are validated pydantic sections (`core/settings_store.py`, `core/policy.py`, `packs/stock_procurement/settings.py`) that map one-to-one onto fields |
| **DocTypes** for the stored records (section 2) | replaces SQLite | `core/storage.py` and the pack's `schema.py`; each record type has a repository class |
| **Roles and permissions** (section 2) | Frappe-native permissions replace the copied role list; every right in a permission row defaults to 1, so each is written out and tested | `core/auth/` and `core/common.Actor`; the checks are in `require()` and `Toolkit.call` |
| **Background jobs**: the stock check, the procurement pass (every 4 minutes at best: the scheduler tick), reminders and expiry, the agents (one task at a time) | no long-lived threads on Frappe Cloud | thread-based (`core/scheduler.py`, `core/agents/service.py`, the conversation pool) |
| **Webhook endpoints** as whitelisted guest methods with the existing signature checks; **Telegram in webhook mode only** | Frappe Cloud gives public HTTPS; no worker may run a poll loop | FastAPI routes; the signature code is channel code and moves as it is |
| **A Frappe ERP client** implementing `ERPClient` with in-process calls | no API key, faster, permission-checked: reads for a signed-in user use `get_list` or an explicit check, never `get_all` (it ignores permissions); the company comes from `Warehouse.company`, not from user defaults | `core/erp/erpnext.py` is REST; `core/erp/base.py` is the interface, still shaped by the stock pack (made pack-neutral in Phase 2) |
| **Migrations**: `patches.txt`, install and uninstall hooks, the custom fields as fixtures | an app must upgrade and uninstall cleanly | per-pack schema versions with migrations in `core/storage.py` (none needed yet); `erp-agents erp-setup` creates the custom fields by REST |
| **UI** in Desk: inbox, stock monitor, vendor comparison, Ask, settings, as workspace, pages and list/form views | the single-file console cannot be served by a bench app | `packs/stock_procurement/web/index.html` (one file of about 1,150 lines, about 1,000 of them script) |
| **Translations** (`_()` around every user-visible string) | Frappe convention, wanted for a Marketplace app | all text is built in message modules, in English, in one place each |
| **Current stable Frappe and ERPNext** support, with branching on `frappe.__version__` | a Marketplace requirement | Frappe 16 needs Python exactly 3.14; Frappe Cloud lists Version 15 (the default for new bench groups) and 16 as Stable. Version 0.1 targets Frappe 16 and ERPNext 16 only, tried on Frappe 16.55.2 and ERPNext 16.53.0; the standalone service and the engine keep Python 3.10 to 3.14 |
| **App-specific CI**: a bench in GitHub Actions running `bench run-tests`, and Semgrep with Frappe's rules | a Marketplace requirement. Every command works locally (`run-tests`, `validate-dependencies`, Semgrep, bandit, pip-audit); the CI workflow `bench new-app` generates installs only Frappe, so it must also `get-app` and `install-app` ERPNext, and the ERPNext test data costs about 250 s per run unless the database is cached; it has never run on GitHub | CI exists for the standalone service (pytest, bandit, pip-audit, a secrets scan) and tests nothing Frappe-specific |
| **Listing assets**: unique app name, 40 to 80 character description, long description without install steps, square logo (at least 200x200, no text), one category, five links (Website, Support, Documentation, Terms of Service, Privacy Policy), screenshots, and a demo video that is prepared and sent on request | the listing | the privacy statement is generated from code and tested (`core/agents/privacy.py`, [PRIVACY.md](PRIVACY.md)); everything else is missing; the repository is named `erpnext-ai-agents`, and the app's name on the Marketplace still has to be checked for uniqueness |

## 7. Marketplace review: what the reviewers check

From the requirements read on 21 Oct 2026 ([DEVELOPING.md](DEVELOPING.md)): open source under an accepted licence and hosted on GitHub by its owner; support for the current stable Frappe and ERPNext; a passing CI; no override of base Frappe behaviour; a complete listing (assets above); and no steering of users away from Frappe Cloud. A settings DocType is recommended, not required, and a privacy policy "that says exactly what leaves the site" is this project's own rule, not on the Marketplace pages (the five links include a Privacy Policy). Each push to the app's branch creates a release, and the Audit Report runs Semgrep. Frappe's published guide also suggests running Semgrep with its own rules; the repository already has that job (not a required check) and a register of security checks with the Frappe equivalent of each ([SECURITY_CHECKS.md](SECURITY_CHECKS.md)), written so that the route sweep, the console test and the fence around prompts port with the code they test. The review covers the app **and each release**, and the stated review time was about 10 days. Preparing for it: run the app on a private bench on Frappe Cloud first, keep the agent features off by default (so "nothing leaves your site unless you turn it on" stays true), and have the privacy policy generated from the manifests so it cannot drift from the code.

## 8. What can be reused, and what has to change first

**Reusable once the SQL is behind repositories.** Measured in Phase 0 ([FRAPPE_FACTS.md](FRAPPE_FACTS.md) 6.2): of the 111 modules, 68 import without a web framework, scheduler, SQLite or `mcp` and 55 also without a network library; 36 import SQLite, among them the stock pack's tool registry (`tools.py`), `assistant/chat.py`, `agents/purchase_agent.py` and `approvals/service.py`. The agent runtime, privacy generator and language-model clients reach `requests` through `llm.py`: harmless in a bench, which provides `requests`, but not "no HTTP library", and outside version 0.1. Pure already: the analysis, forecast, vendor scoring, taxes, the message builders, the approval models and rendering, the policy and the tool registry's checks. Most of the 2,100 tests (they run against the `ERPClient` interface and a demo ERP) carry over.

**Has to change before any port**, and is worth doing in the standalone service first because each step is testable here:

1. **Put SQL behind repositories everywhere.** 36 modules import SQLite and about 195 call sites run SQL (Phase 2 measurement; the earlier "about a dozen calls outside repository classes" in `DEVELOPING.md` was wrong); the repositories are SQLite-specific. A storage interface with a SQLite implementation (today) and a DocType implementation (later) is the single largest enabler. The per-pack schema registry (`Database.register_schema`) already gives each pack its own tables, which is the natural unit for this.
2. **Move in-memory state into storage or cache**: the conversation's "waiting for an answer" memory and the one-at-a-time agent lock (sign-in sessions stay with the standalone service: Frappe has its own). (Done in Phase 3 of the hardening review: the rate limits are one `RateLimiter` behind an interface, the circuit breakers keep their state in a `BreakerStore`, and the ids of the messages a channel delivered are one table with one repository. What each becomes in the app is in [RESILIENCE.md](RESILIENCE.md).)
3. **Turn threads into jobs**: `submit` and `on_submitted` start threads; each becomes a `frappe.enqueue` call. The task row already exists before the work starts, so polling by task id carries over.
4. **Decouple the ERP client interface from stock and procurement**, so another pack can add its own methods (it carries stock, usage, vendor-quote and purchase-order calls today). This is the one place where the core is not yet pack-neutral.
5. **One conversation handler and one console page** are still assumed by the core (Telegram and WhatsApp messages go to one pack; the console is one file). A second pack with a chat or screens needs a small router and a navigation entry.

**Replaced**: the REST client (and with it the circuit breaker on ERPNext and the finishing of an answer after a stop, which exist only because ERPNext is a second system: in the app the draft and the approval are one transaction), SQLite, the scheduler, FastAPI routes and the web console, `.env` and `settings.yaml`, the MCP endpoint (a whitelisted method behind Frappe's OAuth 2 provider: this is also what real MCP OAuth support needs, see [AGENTS.md](AGENTS.md)), the Telegram poller, and the local Ollama option (a local model is not available on Frappe Cloud; hosted providers need their key in a Password field and the owner's consent).

## 9. Work packages and effort

One developer who knows this code, in working days (revised on 5 Nov 2026 from the spike, [FRAPPE_SPIKE_REPORT.md](FRAPPE_SPIKE_REPORT.md) section 7; the plan's own figures were in weeks, at five days a week). The ranges are wide on purpose: packages 1 and 6 are the least known. The phases of the work map onto the packages as in [FRAPPE_DECISIONS.md](FRAPPE_DECISIONS.md): Phase 2 = package 1; Phase 3 = 2; Phase 4 = 3 and 4; Phase 5 = 5 and 6; Phase 6 = 8; Phase 7 = 9; Phase 8 = the trial and the review. Package 7 (agents and MCP) and the webhooks of package 5 are not in version 0.1.

| # | Work package | Effort | Done when |
|---|---|---|---|
| 0 | Decisions: app name (unique on the Marketplace), repository, publisher account, target Frappe and ERPNext versions, whether the standalone service is kept (one app with a module per pack is already decided) | 2-3 days | done: [FRAPPE_FACTS.md](FRAPPE_FACTS.md) and [FRAPPE_DECISIONS.md](FRAPPE_DECISIONS.md) |
| 1 | Storage interface and repository clean-up in the standalone service; in-memory state moved out; threads behind a job interface; ERP client interface made pack-neutral | 12-18 days (plan: 10-15) | the whole suite passes on an in-memory fake storage as well as SQLite |
| 2 | Frappe app skeleton: package, `hooks.py`, Agent Settings with the Pack table and Password fields, roles, the manifest sync, install hooks and custom fields, a bench in a dev container | 3-5 days (plan: 10) | the app installs on a local bench and uninstalls cleanly; a pack can be switched off |
| 3 | `FrappeERPClient`: stock, usage history, vendor orders and quotations, taxes, document creation as the signed-in user | 6-9 days (plan: 10-15) | the existing ERP-client tests pass against a bench site with test data |
| 4 | DocTypes and repositories on them; `patches.txt` for upgrades; the one-time import from a SQLite backup | 8-12 days (plan: 10-15) | alerts, approvals and proposals round-trip on a site; a backup imports |
| 5 | Scheduler events and `frappe.enqueue` jobs; approvals, reminders, expiry; WhatsApp and Telegram webhooks as guest methods with the signature checks | 4-6 days, webhooks 3-4 more (plan: 7.5-10) | an alert is raised, sent, answered and turned into a draft purchase order on a site |
| 6 | Desk UI: approvals inbox with Change quantity, Change vendor and Cancel order; stock monitor; vendor comparison; Ask; settings | 12-16 days (plan: 15-20) | the end-to-end flows of the live test plan pass in the Desk |
| 7 | Agents and MCP: jobs for the agent tasks; the HTTP endpoint behind Frappe's OAuth provider (protected-resource metadata, token validation, audience binding); hosted-model keys and consent | 10-15 days (plan: 10-15), not in version 0.1 | an MCP client that discovers servers by itself can connect, and the agent checks pass on a site |
| 8 | Tests on Frappe (`bench run-tests`), app CI on a bench, Semgrep with Frappe's rules (the job exists), the route sweep rewritten over the whitelisted methods and the console DOM test over the Desk pages ([SECURITY_CHECKS.md](SECURITY_CHECKS.md)), support for the current stable version | 4-6 days (plan: 7.5-10) | CI green on a clean bench |
| 9 | Translations, privacy policy and support pages, logo, description, demo video, a private-bench trial on Frappe Cloud, the Marketplace review and its fixes | 8-12 days (plan: 10-15), the private-bench trial and the review not counted | the listing is approved |

**Total: version 0.1 (packages 1 to 6, 8 and 9) 57 to 84 working days, about 11 to 17 weeks; everything (adding package 7 and the webhooks) 70 to 103 days.** The plan said 95 to 135 days before the spike. Packages 3 to 6 cannot start until package 1 is done. The spike (one DocType, one scheduled job and one approval round trip on a bench) took about one working day, not the "1 to 1.5 weeks" guessed here ([FRAPPE_SPIKE_REPORT.md](FRAPPE_SPIKE_REPORT.md) row 12.1). The estimate still rests on reading the code for everything the spike did not build. The estimate is for the stock pack as it is today; the planned agent features (delivery chasing, goods receipt check, vendor scorecard, quote requests, demand forecasting, slow and dead stock, substitute items, stock counts) each add DocTypes, jobs and screens and are **not** included. Doing them in the standalone service first means they are ported once, but it also makes the port larger.

## 10. Risks and unknowns

- **Frappe's version and Python requirements move.** Check the current stable Frappe, ERPNext and Python requirements before package 2, and re-check before each release; the app must support the current stable version to be listed.
- **Frappe Cloud limits**: no long-running threads, no local models, no custom ports. Anything that depends on those is dropped or redone (Telegram polling, Ollama, the in-process scheduler).
- **Permissions change meaning.** Today the service account reads everything and the approver's own session is used only to create the draft. In a Frappe app every read is the signed-in user's: a person who may not read Stock Ledger Entries gets less data (confirmed on the bench: a user with only Munimji Viewer reads no Item, Bin or Stock Ledger Entry; the approver, with Stock User, reads all three). That is better security and a behaviour change to test.
- **Hooks and DocType details were checked only where [FRAPPE_SPIKE_REPORT.md](FRAPPE_SPIKE_REPORT.md) section 5 says so.** Untested: webhooks, `doc_events`, `permission_query_conditions`, Desk pages and dashboards, `frappe.enqueue`, translation extraction, fields on Material Request and Stock Entry, GitHub Actions, Frappe Cloud, and any other Frappe version.
- **Uninstall cannot remove everything.** Columns stay, and a Permission Type needs a flag to be deleted (section 4).
- **The console is a rewrite**, not a port: the largest single package, and the one most likely to grow.
- **Review is outside our control**: the Marketplace team reviews the app and every release.
- **Data leaves the site** (WhatsApp, Telegram, hosted models): the privacy policy must say exactly what, and the agent features must stay off by default, as they are today.

## 11. What not to claim until it is true

- That the app is "on", "available on" or "installable from" the Frappe Marketplace or Frappe Cloud.
- That a Frappe version exists, is "in progress" or "coming soon" with a date.
- That the standalone service is Marketplace-ready; it passes its own tests, not Frappe's, and none of the Marketplace requirements above is met.
- That the core and packs layout makes the port small: it makes the split clean and testable, the work in section 9 is still to be done.
- Compatibility with any Frappe or ERPNext version other than the two tried: Frappe 16.55.2 and ERPNext 16.53.0.

## 12. First steps, in order

1. Package 0 is decided: [FRAPPE_DECISIONS.md](FRAPPE_DECISIONS.md) (name, two repositories, Frappe 16 and ERPNext 16 only, the engine as `ai-munimji-engine`).
2. The spike is done (Phase 1, [FRAPPE_SPIKE_REPORT.md](FRAPPE_SPIKE_REPORT.md)); the estimates above are its revision.
3. Package 1 is Phase 2, in this repository (branch `frappe/2-engine`); it improves the standalone service whether or not the port happens.
4. Then packages 2 to 9 in the order of the phases (section 9), each phase reported and approved before the next.

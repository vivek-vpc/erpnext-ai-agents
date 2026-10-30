# Plan: a Frappe app version (Frappe Cloud Marketplace)

**Status: a plan only. Nothing of the Frappe app exists.** No `hooks.py`, no DocTypes, no Frappe package, no Frappe CI. This repository is a **standalone Python service** (FastAPI, SQLite, an in-process scheduler) that talks to ERPNext over its REST API. It is **not** a Frappe app, it has **not** been submitted to the Frappe Marketplace, and it cannot be installed from the Marketplace. Licence: GPL-3.0-or-later (which the Marketplace accepts); that is a licence statement, not a listing.

Written 25 Oct 2026 and updated the same day for the **core + packs** layout ([DEVELOPING.md](DEVELOPING.md)). The Marketplace and Frappe version facts come from `docs.frappe.io` as read on 21 Oct 2026; they change, so re-read the official pages before work starts and again before each release. Names of Frappe hooks, commands and DocType features below are from general Frappe practice and **must be checked against the version you target** (v16 at the time of writing) before anyone builds on them. Nothing here was run on a bench.

## 1. Where the boundary is

| | Standalone beta (this repository, today) | Frappe app (future, separate project step) |
|---|---|---|
| What it is | a Python service you run beside ERPNext | a bench app installed into an ERPNext site |
| Talks to ERPNext | REST with a service account's API key | in-process (`frappe.get_all`, `frappe.get_doc`) as the signed-in user |
| Stores its data | its own SQLite file | DocTypes in the site's database |
| Runs scheduled work | APScheduler thread inside `serve` | `scheduler_events` and `frappe.enqueue` |
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
| `name`, `version`, `enabled_by_default` | a row in a **Pack** table of the core Settings DocType (name, version, enabled); the pack's workspace and its whitelisted methods follow the flag, as the routes do today |
| `tools` (name, role, effect) | whitelisted methods and the MCP endpoint, through the same tool registry; the role check stays in the registry and is made with `frappe.has_permission` |
| `erp_roles` | Role records created or checked at install; the permissions the app needs are no longer a service-account list (it runs as the signed-in user), so `erp_permissions` becomes documentation and a test |
| `erp_look`, `erp_decide` | the (DocType, permission) pairs behind "may look" and "may answer": `read` on Item or Purchase Order, `create` on Purchase Order |
| `approvals` (kind, actions, role, creates) | the allowed values and actions of the **Approval Request** DocType; the `creates` text is shown on the button |
| `settings` (sections) | one Single DocType per pack for its sections (for the stock pack: policy, procurement, calendar and the stock fields of the system section); scoped overrides (item group, item) become a small DocType |
| `sends_to_model` | unchanged: the privacy statement is generated from it and tested |

**Roles.** Today a person is a *viewer* if ERPNext lets them read an Item or a Purchase Order, an *approver* if it lets them create a Purchase Order, and an *admin* if they are Administrator or hold System Manager (`core/auth/erpnext.py`). In Frappe these are checks on the signed-in user: `frappe.has_permission("Purchase Order", "create")` for approver and System Manager for settings. The Approval Request DocType cannot express "may create a Purchase Order" in its own permission rows, so a `has_permission` hook on it calls the pack's `erp_decide` pairs. The role checks, the order gate and the answer verification stay in the core's registry and apply to every pack, exactly as now.

**DocTypes.** Core: Agent Settings (Single, with the Pack table, channel and agent sections, Password fields), Approval Request with an Approval Delivery child table, Work Order, Agent Task, Tool Call. Stock pack: Inventory Alert, Stock Notice, Purchase Proposal, Chat Log, Assistant Answer, Agent Run, Stock Policy Override, and (since the follow-up features) Vendor Message, Vendor Reply, Delivery Chase, Receipt Check, Quote Round, Cycle Count and Count Review, plus its Single settings. In Frappe a delivery reminder can be a Communication on the Purchase Order directly, and a quote round maps naturally onto ERPNext's own Request for Quotation. These are the tables in `core/storage.py` and `packs/stock_procurement/schema.py`. The settings audit trail (`settings_audit`) is replaced by the version log on the Single DocTypes (check that Password values are not recorded). The decision history (`core/history.py`) reads Approval Requests the same way; its seal (`audit_chain`) becomes an append-only Audit Seal DocType without delete permission for anyone, next to Frappe's own Version log. The per-pack schema version (`pack_schema`) and its migrations become entries in `patches.txt`.

## 3. hooks.py: what it would contain

A plan, not code. Check each key against the target version.

| Hook | Purpose |
|---|---|
| `app_name`, `app_title`, `app_publisher`, `app_description`, `app_email`, `app_license` | identity; the licence string must say GPL-3.0 in the form `bench` expects; the name must be unique on the Marketplace (undecided) |
| `required_apps = ["erpnext"]` | declares the dependency on ERPNext |
| `after_install`, `before_uninstall`, `after_migrate` | create the roles and custom fields, sync the pack manifests into the Pack table, remove what the app added on uninstall |
| `fixtures` | the custom fields the app needs on Purchase Order, Material Request and Stock Entry (`custom_agent_ref`, approver, note), as `Custom Field` records filtered to this app, replacing `erp-agents erp-setup` |
| `scheduler_events` | a `cron` entry or two for the stock check, the 3-minute procurement pass, reminders and expiry. A schedule that an administrator edits (`check_cron`, the brief) cannot live in `hooks.py`, so a frequent tick job asks the settings whether anything is due |
| `doc_events` | optional: react to a Purchase Order submitted or cancelled by hand, instead of polling the ERP |
| `has_permission`, `permission_query_conditions` | the Approval Request rule above; each person sees the requests they may answer |
| `app_include_js` / workspace and page files | the Desk screens (inbox, stock monitor, vendor comparison, Ask, settings) per pack |
| `boot_session` | optionally tell the browser which packs are on |
| whitelisted methods (`@frappe.whitelist`) | the tools, the approval answers, the chat; `allow_guest=True` only for the WhatsApp and Telegram webhooks, each with its signature check |

It must not override base Frappe behaviour (login, password reset).

## 4. Installing, running and uninstalling (a development bench, then a site)

Commands are the usual bench ones; confirm them for the target version.

1. **Develop**: set up a bench with the target Frappe and ERPNext (a dev container works), `bench get-app <repository-url> --branch <branch>`, `bench --site <site> install-app <app>`, then `bench --site <site> migrate` after every change that touches DocTypes or patches, `bench build` for the Desk scripts, `bench start` to run web, workers and scheduler together.
2. **Configure**: open Agent Settings in the Desk; enter the channel tokens and a hosted-model key in the Password fields; switch the packs you want. No `.env`, no settings file, no service account key.
3. **Test**: `bench --site <test site> run-tests --app <app>` (check the exact form), plus Semgrep with Frappe's rules.
4. **Self-hosted site**: the same `get-app` and `install-app`, then `bench --site <site> migrate` and a restart of the bench processes.
5. **Frappe Cloud**: add the app to a private bench from the repository first (this is the trial the Marketplace asks for), install it on a site, and only then apply for the listing.
6. **Upgrade**: a new release is a git tag plus patches; `migrate` runs the patches in `patches.txt` once each.
7. **Uninstall**: `bench --site <site> uninstall-app <app>` runs `before_uninstall`, which removes what the app added (custom fields, roles); the DocType data is removed with the app, so the uninstall page must say that and point to an export.

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
| A bench app package: the Python module, `modules.txt`, `hooks.py`, `patches.txt`, a `config/` folder, a `pyproject.toml` that declares the supported Frappe and ERPNext versions (check the exact form for the version) | `bench` cannot install anything else | none |
| **Settings DocTypes** (Single) with Password fields | a Marketplace requirement; replaces `settings.yaml` and `.env` | the settings are validated pydantic sections (`core/settings_store.py`, `core/policy.py`, `packs/stock_procurement/settings.py`) that map one-to-one onto fields |
| **DocTypes** for the stored records (section 2) | replaces SQLite | `core/storage.py` and the pack's `schema.py`; each record type has a repository class |
| **Roles and permissions** (section 2) | Frappe-native permissions replace the copied role list | `core/auth/` and `core/common.Actor`; the checks are in `require()` and `Toolkit.call` |
| **Background jobs**: the stock check, the 3-minute procurement pass, reminders and expiry, the agents (one task at a time) | no long-lived threads on Frappe Cloud | thread-based (`core/scheduler.py`, `core/agents/service.py`, the conversation pool) |
| **Webhook endpoints** as whitelisted guest methods with the existing signature checks; **Telegram in webhook mode only** | Frappe Cloud gives public HTTPS; no worker may run a poll loop | FastAPI routes; the signature code is channel code and moves as it is |
| **A Frappe ERP client** implementing `ERPClient` with in-process calls | no API key, faster, permission-checked | `core/erp/erpnext.py` is REST; `core/erp/base.py` is the interface, still shaped by the stock pack |
| **Migrations**: `patches.txt`, install and uninstall hooks, the custom fields as fixtures | an app must upgrade and uninstall cleanly | per-pack schema versions with migrations in `core/storage.py` (none needed yet); `erp-agents erp-setup` creates the custom fields by REST |
| **UI** in Desk: inbox, stock monitor, vendor comparison, Ask, settings, as workspace, pages and list/form views | the single-file console cannot be served by a bench app | `packs/stock_procurement/web/index.html` (one file of about 1,150 lines, about 1,000 of them script) |
| **Translations** (`_()` around every user-visible string) | Frappe convention, wanted for a Marketplace app | all text is built in message modules, in English, in one place each |
| **Current stable Frappe and ERPNext** support, with branching on `frappe.__version__` | a Marketplace requirement | tested live on ERPNext v16 only; v16's own Python requirement must be checked, and this project's CI matrix is 3.10 to 3.14 |
| **App-specific CI**: a bench in GitHub Actions running `bench run-tests`, and Semgrep with Frappe's rules | a Marketplace requirement | CI exists for the standalone service (pytest, bandit, pip-audit, a secrets scan) and tests nothing Frappe-specific |
| **Listing assets**: unique app name, 40 to 80 character description, long description without install steps, square logo (at least 200x200, no text), one category, support URL, privacy policy URL, a short demo video | the listing | the privacy statement is generated from code and tested (`core/agents/privacy.py`, [PRIVACY.md](PRIVACY.md)); everything else is missing; the repository is named `erpnext-ai-agents`, and the app's name on the Marketplace still has to be checked for uniqueness |

## 7. Marketplace review: what the reviewers check

From the requirements read on 21 Oct 2026 ([DEVELOPING.md](DEVELOPING.md)): open source under an accepted licence and hosted on GitHub by its owner; support for the current stable Frappe and ERPNext; a passing CI; no override of base Frappe behaviour; a settings DocType; a complete listing (assets above); a privacy policy that says exactly what leaves the site; and no steering of users away from Frappe Cloud. Frappe's published guide also suggests running Semgrep with its own rules; the repository already has that job (not a required check) and a register of security checks with the Frappe equivalent of each ([SECURITY_CHECKS.md](SECURITY_CHECKS.md)), written so that the route sweep, the console test and the fence around prompts port with the code they test. The review covers the app **and each release**, and the stated review time was about 10 days. Preparing for it: run the app on a private bench on Frappe Cloud first, keep the agent features off by default (so "nothing leaves your site unless you turn it on" stays true), and have the privacy policy generated from the manifests so it cannot drift from the code.

## 8. What can be reused, and what has to change first

**Reusable as it is** (no web, scheduler, HTTP or SQL library in them): the core's tool registry, agent runtime and answer checks, approval models and service logic, privacy generator and language-model clients; and in the stock pack the analysis, vendor scoring, taxes and landed cost, the message builders, the chat understanding, the tools (`packs/stock_procurement/tools.py`) and the agents' prompts. Most of the 1,200 tests (they run against the `ERPClient` interface and a demo ERP) carry over.

**Has to change before any port**, and is worth doing in the standalone service first because each step is testable here:

1. **Put SQL behind repositories everywhere.** About a dozen calls still sit outside repository classes (the audit table in `DEVELOPING.md`); the repositories are SQLite-specific. A storage interface with a SQLite implementation (today) and a DocType implementation (later) is the single largest enabler. The per-pack schema registry (`Database.register_schema`) already gives each pack its own tables, which is the natural unit for this.
2. **Move in-memory state into storage or cache**: the conversation's "waiting for an answer" memory, sign-in sessions, the one-at-a-time agent lock. (Done in Phase 3 of the hardening review: the rate limits are one `RateLimiter` behind an interface, the circuit breakers keep their state in a `BreakerStore`, and the ids of the messages a channel delivered are one table with one repository. What each becomes in the app is in [RESILIENCE.md](RESILIENCE.md).)
3. **Turn threads into jobs**: `submit` and `on_submitted` start threads; each becomes a `frappe.enqueue` call. The task row already exists before the work starts, so polling by task id carries over.
4. **Decouple the ERP client interface from stock and procurement**, so another pack can add its own methods (it carries stock, usage, vendor-quote and purchase-order calls today). This is the one place where the core is not yet pack-neutral.
5. **One conversation handler and one console page** are still assumed by the core (Telegram and WhatsApp messages go to one pack; the console is one file). A second pack with a chat or screens needs a small router and a navigation entry.

**Replaced**: the REST client (and with it the circuit breaker on ERPNext and the finishing of an answer after a stop, which exist only because ERPNext is a second system: in the app the draft and the approval are one transaction), SQLite, the scheduler, FastAPI routes and the web console, `.env` and `settings.yaml`, the MCP endpoint (a whitelisted method behind Frappe's OAuth 2 provider: this is also what real MCP OAuth support needs, see [AGENTS.md](AGENTS.md)), the Telegram poller, and the local Ollama option (a local model is not available on Frappe Cloud; hosted providers need their key in a Password field and the owner's consent).

## 9. Work packages and effort

One experienced Frappe developer who knows this code, in working weeks. The ranges are wide on purpose: the first two packages are the least known. The packs add about one week to package 2 (the Pack table, the manifest sync, the permission hook); everything else is as before.

| # | Work package | Effort | Done when |
|---|---|---|---|
| 0 | Decisions: app name (unique on the Marketplace), repository, publisher account, target Frappe and ERPNext versions, whether the standalone service is kept (one app with a module per pack is already decided) | 2-3 days | written down |
| 1 | Storage interface and repository clean-up in the standalone service; in-memory state moved out; threads behind a job interface; ERP client interface made pack-neutral | 2-3 weeks | the whole suite passes on an in-memory fake storage as well as SQLite |
| 2 | Frappe app skeleton: package, `hooks.py`, Agent Settings with the Pack table and Password fields, roles, the manifest sync, install hooks and custom fields, a bench in a dev container | 2 weeks | the app installs on a local bench and uninstalls cleanly; a pack can be switched off |
| 3 | `FrappeERPClient`: stock, usage history, vendor orders and quotations, taxes, document creation as the signed-in user | 2-3 weeks | the existing ERP-client tests pass against a bench site with test data |
| 4 | DocTypes and repositories on them; `patches.txt` for upgrades; the one-time import from a SQLite backup | 2-3 weeks | alerts, approvals and proposals round-trip on a site; a backup imports |
| 5 | Scheduler events and `frappe.enqueue` jobs; approvals, reminders, expiry; WhatsApp and Telegram webhooks as guest methods with the signature checks | 1.5-2 weeks | an alert is raised, sent, answered and turned into a draft purchase order on a site |
| 6 | Desk UI: approvals inbox with Change quantity, Change vendor and Cancel order; stock monitor; vendor comparison; Ask; settings | 3-4 weeks | the end-to-end flows of the live test plan pass in the Desk |
| 7 | Agents and MCP: jobs for the agent tasks; the HTTP endpoint behind Frappe's OAuth provider (protected-resource metadata, token validation, audience binding); hosted-model keys and consent | 2-3 weeks | an MCP client that discovers servers by itself can connect, and the agent checks pass on a site |
| 8 | Tests on Frappe (`bench run-tests`), app CI on a bench, Semgrep with Frappe's rules (the job exists), the route sweep rewritten over the whitelisted methods and the console DOM test over the Desk pages ([SECURITY_CHECKS.md](SECURITY_CHECKS.md)), support for the current stable version | 1.5-2 weeks | CI green on a clean bench |
| 9 | Translations, privacy policy and support pages, logo, description, demo video, a private-bench trial on Frappe Cloud, the Marketplace review and its fixes | 2-3 weeks | the listing is approved |

**Total: about 19 to 27 working weeks (roughly 4.5 to 6.5 months) for one developer**, 11 to 16 of them in packages 3 to 7 that cannot start until package 1 is done. This is an estimate from reading the code, not from building anything on a bench. A spike that proves one DocType, one scheduled job and one approval round trip on a bench is about 1 to 1.5 weeks and is the right first step to replace it with a measured one. The estimate is for the stock pack as it is today; the planned agent features (delivery chasing, goods receipt check, vendor scorecard, quote requests, demand forecasting, slow and dead stock, substitute items, stock counts) each add DocTypes, jobs and screens and are **not** included. Doing them in the standalone service first means they are ported once, but it also makes the port larger.

## 10. Risks and unknowns

- **Frappe's version and Python requirements move.** Check the current stable Frappe, ERPNext and Python requirements before package 2, and re-check before each release; the app must support the current stable version to be listed.
- **Frappe Cloud limits**: no long-running threads, no local models, no custom ports. Anything that depends on those is dropped or redone (Telegram polling, Ollama, the in-process scheduler).
- **Permissions change meaning.** Today the service account reads everything and the approver's own session is used only to create the draft. In a Frappe app every read is the signed-in user's: a person who may not read Stock Ledger Entries gets less data. That is better security and a behaviour change to test.
- **Hooks and DocType details in this plan are unverified.** They are written from general Frappe practice; the spike is where they are checked.
- **The console is a rewrite**, not a port: the largest single package, and the one most likely to grow.
- **Review is outside our control**: the Marketplace team reviews the app and every release.
- **Data leaves the site** (WhatsApp, Telegram, hosted models): the privacy policy must say exactly what, and the agent features must stay off by default, as they are today.

## 11. What not to claim until it is true

- That the app is "on", "available on" or "installable from" the Frappe Marketplace or Frappe Cloud.
- That a Frappe version exists, is "in progress" or "coming soon" with a date.
- That the standalone service is Marketplace-ready; it passes its own tests, not Frappe's, and none of the Marketplace requirements above is met.
- That the core and packs layout makes the port small: it makes the split clean and testable, the work in section 9 is still to be done.
- Compatibility with any ERPNext version other than v16, which is the only one tried.

## 12. First steps, in order

1. The owner decides the rest of package 0 (name, repository, versions); one app with a module per pack is decided.
2. Do package 1 in this repository; it improves the standalone service whether or not the port happens.
3. Do the spike (one DocType, one scheduled job, one approval round trip on a local bench) and replace the estimates above with measured ones.
4. Only then commit to packages 2 to 9.

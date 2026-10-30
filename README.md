# Stock Assistant for ERPNext (erpnext-ai-agents)

Repository and package: `erpnext-ai-agents`. The command is `erp-agents` (also installed as `erpnext-ai-agents`); the Python package is `erp_agents`.

An assistant for small-business ERP (ERPNext first). It watches stock and tells you early when you need to reorder - *including early warnings from past
usage before any threshold is crossed* - compares your vendors (price, duties and taxes, delivery time, reliability) and prepares a purchase request.
**Nothing is ordered until a person presses Create order**: that creates a *draft* purchase order in ERPNext with the approver recorded. Approvals and questions
go through the web console, Telegram or WhatsApp. An optional local language model can explain a request in plain words, but every number comes from code and
ERP data, never from the model.

It runs on a laptop or a small VPS and needs only open-source software (Python, SQLite, FastAPI, APScheduler). The core makes no LLM calls, so running costs are zero.

![The console: a purchase order request with the reasons for the chosen vendor and the rejected ones](docs/img/demo.gif)

**Early concept, feedback wanted.** I am looking for people who run ERPNext (or who want to see agentic design done with a person in charge) to try the demo and tell me what is wrong, missing or confusing. It takes about 30 minutes: [docs/TESTING_GUIDE.md](docs/TESTING_GUIDE.md). Questions and ideas go to [Discussions](https://github.com/vivek-vpc/erpnext-ai-agents/discussions); problems to [Issues](https://github.com/vivek-vpc/erpnext-ai-agents/issues/new/choose).

### How it fits together

![Architecture: people and channels, gateway, six agents, MCP layer with the tools, human approval gate, ERPNext, vendors and audit log](docs/img/architecture.png)

> **Status: beta. Licence: GPL-3.0-or-later.**
>
> - 2,045 passed and 1 skipped (a POSIX-only permission test; it includes the AI evaluation, [docs/EVALUATION.md](docs/EVALUATION.md)) on Windows with Python 3.14, 29 Oct 2026, run against a demo ERP and fake transports (`python -m pytest`), plus `bandit` and `pip-audit`, which report nothing.
> - Checked live against an **ERPNext v16** test instance: reads, vendor comparison with duties and taxes, sign-in through ERPNext, permissions, and creation of a
>   draft Purchase Order and Material Request ([docs/E2E_TEST_RESULTS.md](docs/E2E_TEST_RESULTS.md) lists every case and what passed, failed or was not run).
>   Other ERPNext versions have not been tried.
> - **Telegram and WhatsApp**: the automated tests use fake transports. Messages have been delivered live on the author's own accounts. **WhatsApp has not been
>   tried against Meta's sandbox by anyone else**: expect to adjust the message template and buttons.
> - **One process only.** SQLite and the in-process scheduler do not support several copies running at once: `serve` takes a lock on the database and a second copy
>   refuses to start. The database carries a schema version (a newer file is refused untouched; an older one is copied aside, then upgraded; the core is at version 3, whose migrations add the table of scheduled-job records (2) and the ids of the messages the channels delivered (3), and are tested on real older files; the stock pack's tables are at version 4, whose migration adds columns to two existing tables and is tested on a real version-3 file). `erp-agents backup` / `restore` / `doctor` are described in section 7c.
> - **This is a standalone Python service (FastAPI + SQLite). It is not a Frappe app and is not on the Frappe Marketplace**, and cannot be installed from it. A Frappe app
>   would be a separate port that reuses the business logic and replaces the edges: nothing of it is started. What it would take: [docs/FRAPPE_APP_PLAN.md](docs/FRAPPE_APP_PLAN.md),
>   [docs/PUBLISHING_PLAN.md](docs/PUBLISHING_PLAN.md) and [docs/DEVELOPING.md](docs/DEVELOPING.md).
>
> Before relying on it: run `erp-agents erp-check` against your ERPNext, compare the numbers with Stock Balance, and run `erp-agents run-once --dry-run` for a week
> (see "Rolling out safely").

---

## 1. Try it (no ERPNext, no accounts)

Pick one. All three use a built-in demo ERP with realistic stock, vendors and open orders, and send nothing anywhere.

### A. Docker (about 3 minutes, any system)

```bash
git clone https://github.com/vivek-vpc/erpnext-ai-agents.git
cd erpnext-ai-agents
docker compose up --build
```

The log prints an access key. Open <http://127.0.0.1:8100/> and enter it. You will see stock alerts and reorder requests waiting for your OK. Press **Start reorder** on one, wait up to three minutes, and the assistant comes back with a purchase order showing why it chose this vendor and why it rejected the others.

The demo port is published on `127.0.0.1` only. The start-up log warns that access keys are accepted "from anywhere"; that is deliberate here, because requests from your browser reach the container over the Docker network, and it stays safe because only your computer can reach the port. Stop with `Ctrl+C`; `docker compose down -v` also removes the demo data.

### B. Linux or macOS (Python 3.10+)

```bash
git clone https://github.com/vivek-vpc/erpnext-ai-agents.git
cd erpnext-ai-agents
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,agents]"
erp-agents demo                    # 10 realistic scenarios in the terminal
erp-agents demo --interactive      # answer the approval requests yourself
python -m pytest                   # over 1,100 tests, no ERPNext needed
```

### C. Windows PowerShell (Python 3.10+ from python.org)

```powershell
git clone https://github.com/vivek-vpc/erpnext-ai-agents.git
cd erpnext-ai-agents
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev,agents]"
erp-agents demo                    # runs on a built-in demo ERP with 10 realistic scenarios
erp-agents demo --interactive      # answer the approval requests yourself in the terminal
python -m pytest                   # over 1,100 tests (needs the dev and agents extras)
```

The demo covers: healthy stock, a plain threshold breach, a **usage surge** early warning, a
**seasonal** early warning, stock-out before the lead time, an open PO that arrives in time (no
alert), an open PO that arrives too late, a brand-new item with little history, dead stock, and
lumpy demand.

Want to point it at your own ERPNext? Continue with section 4, and start with `erp-agents run-once --dry-run`.

## 2. What the agent decides, and why you can trust the explanation

All numbers come from plain code (`packs/stock_procurement/inventory/analysis.py`), not from an LLM, and the message is
filled in from those stored numbers - so the explanation can never disagree with the data.

**Threshold rules (admin-configurable)** - the item alerts when any enabled rule is tripped:

| Rule | Setting | Meaning |
|---|---|---|
| Minimum quantity | `min_qty` | stock position (on hand + open POs) is at or below this |
| Minimum days of cover | `min_days_cover` | stock lasts this many days or fewer at the planned usage rate |
| Order point | `use_lead_time_rule`, `safety_stock_days`, `lead_time_days` | cover <= supplier lead time + safety days |
| Late open PO | (automatic) | on-hand runs out before a known PO arrival date |

**What counts as stock (configurable)**: on hand (`actual_qty`) is the starting point; you choose which other ERP
stock columns are **subtracted** (`stock_subtract`) or **added** (`stock_add`), and which warehouses count
(`warehouses`, empty = all). Columns: reserved for sales orders / production / subcontracting / production plans
(`reserved_qty`, `reserved_qty_for_production`, `reserved_qty_for_sub_contract`, `reserved_qty_for_production_plan`),
on order (`ordered_qty`), requested on material requests (`indented_qty`) and planned production (`planned_qty`).
*Available* = on hand minus the subtracted columns; the stock *position* = available plus the added columns.
Defaults: subtract the first three reserved columns, add on order and planned. Days of cover use available stock,
the reorder rules use the position, and the approval message lists every column and whether it was used.
Set it in the console (*Stock Monitor → How stock is calculated*), or per group/item over the API/CLI, e.g.
`erp-agents policy set --scope group --key Fasteners 'warehouses=["Stores - AC"]' 'stock_subtract=[]'`.

**Stock by warehouse**: every figure above is one combined number, but you can always see where it sits. The Your stock page has a *warehouses* fold-out under each
*In stock* number (or "all in Stores - X" when one warehouse holds it all), a chat answer about an item adds "By warehouse: ...", the `stock_status` tool returns
`by_warehouse`, and a reorder request that has stock in several warehouses has a "By warehouse" line. The split follows the list of warehouses that count, so it always
adds up to the number shown. Stock in a warehouse you do not want counted (quarantine, scrap) is then easy to spot and to leave out.

**Moving stock between warehouses.** Open the *warehouses* fold-out (or the "all in ..." line) on the Your stock page and press **Move...** next to the warehouse to
take it from, or type to the chat: `move 50 BOLT-M8 from Stores to Finished Goods` (a warehouse can be named without its company when that is unique; the words *from*
and *to* say which is which), or ask an AI client through the `prepare_stock_transfer` tool, or `POST /api/v1/inventory/transfers`. It is checked against what ERPNext
holds in the source warehouse (never more than it has; a note when it would leave less than is promised to customers) and sent to the approvers as a request showing the
stock before and after. Pressing **Create transfer** makes a **draft** Stock Entry of type Material Transfer in ERPNext, with the approver recorded (under their own
ERPNext login when they answer from the console); **no stock moves until someone submits that draft in ERPNext**, the same rule as a purchase order. Only people who can
approve may ask; the assistant never starts a transfer by itself. It needs the *Stock Entry* create permission (see `erp-check-access`) and one run of `erp-agents erp-setup`.

**Stock in transit** (stock moving between your own warehouses, held in an ERPNext warehouse of type *Transit*) is reported apart from on-hand stock, and
**you choose what it counts as** (`in_transit_stock`; in the console, the row *On the way between your warehouses* under *What counts as stock you have?*):
`arriving` (the default) adds it to the stock *position*, so no reorder is started for goods that are already on their way, but it is not available to sell and does
not count toward days of cover; `shelf` counts it like stock on the shelf; `ignore` leaves it out. It can be set globally, per group or per item, and the requests,
the Ask page and the Stock Monitor (optional column) say how it was counted. A warehouse is recognised as transit by its type in ERPNext; if the types cannot be read,
its stock counts as on hand. Goods ordered from vendors but not yet received are a different thing: they are *On order*. Quarantine, scrap and returns warehouses
are not transit: leave them out with the list of warehouses that count.

**History-based early warning** - when nothing is violated yet, the agent projects forward. It
warns if a rule *will* trip within `lookahead_days` (default 14), and says what is pushing the
forecast up:

- **Recent trend**: usage over the last 14 days vs the last 90 (`usage_method`, `recent_weight`).
- **Seasonality**: how much last year's same fortnight ran above its baseline (needs ~15 months of
  history; capped by `seasonal_max_factor`).
- With neither, it simply says "at the normal pace you reach the order point in N days".
- New items with too little history (`min_history_days`) get threshold checks only, and say so.

Each approval message contains: current stock, days of cover, stock-out date, lead time and arrival
date, the latest safe order date, *which rule fired with the numbers*, how usage was estimated, a
suggested quantity with its arithmetic, and a confidence note. The **Details** button adds every
figure and where each threshold came from.

**Behaviour you would otherwise have to build**: no duplicate alerts; *Remind me later* and *Ignore*
are respected; an alert is re-raised if it gets worse (e.g. HIGH -> CRITICAL); alerts close and
stale requests are cancelled when stock recovers; after you approve, the agent waits
(`reorder_cooldown_days`, 3 days, set in this application and not in ERPNext) for the PO to appear, and stops waiting as soon as
that order has been received, closed or cancelled and the item is short again; unanswered requests get reminders and then expire -
**nothing is ever auto-approved**.

## 3. Change thresholds (admin)

Settings resolve as: built-in default -> `settings.yaml` -> **global** -> **item group** -> **item**.
Every change is validated and written to an audit log (who, when, old -> new).

```powershell
# everything: alert at 10 days of cover
erp-agents policy set --scope global min_days_cover=10
# the Fasteners group: also keep at least 500 units, look 21 days ahead
erp-agents policy set --scope group --key Fasteners min_qty=500 lookahead_days=21
# one item: switch the days rule off, use only the quantity rule
erp-agents policy set --scope item --key BOLT-M8 min_days_cover=null min_qty=300
erp-agents policy clear --scope item --key BOLT-M8 min_qty     # inherit again
erp-agents policy show --scope item --key BOLT-M8              # effective values + where each came from
erp-agents policy audit --scope item --key BOLT-M8
```

The same is available over HTTP (`PUT /api/v1/settings/policy/{global|group|item}/{key}`) for an
admin screen; `GET /api/v1/settings/schema` returns every field with its description and limits so
the form can be generated. Admins can also change the schedule (`PUT /api/v1/settings/system`,
e.g. `check_cron`), which takes effect immediately without a restart.

`POST /api/v1/inventory/run` is a **dry run by default** - it shows what the agent would do without
sending anything.

## 4. Connect your ERPNext

> **If your ERPNext has more than one company, set `erp.company`** in the configuration file. Without it ERPNext puts every new draft order in the service account's
> *default* company, which may not be the one your stock and vendors belong to (the assistant now refuses to create an order in that situation instead of guessing).

1. In ERPNext create a user for the agents (e.g. `agent@yourco.com`): the **service account**. Generate its API key and
   secret (User -> API Access). Give it as few roles as possible: it reads stock and orders for the background checks and
   creates the *draft* purchase order. `erp-agents erp-check-access` (read-only) lists the roles it holds, the smallest
   set of them that covers what the app needs, and the surplus ones to remove (System Manager and the like).
2. `erp-agents init`, then edit `config/settings.yaml`:
   ```yaml
   erp:
     type: erpnext
     url: http://localhost:8080          # your ERPNext (the Docker one from earlier)
     api_key: ${ERPNEXT_API_KEY}
     api_secret: ${ERPNEXT_API_SECRET}
   ```
3. Set the secrets (PowerShell): `$env:ERPNEXT_API_KEY="..."; $env:ERPNEXT_API_SECRET="..."`
4. `erp-agents erp-check` and compare the quantities with **Stock > Stock Balance**.
5. **Once**, as a person who may create Custom Fields: `erp-agents erp-setup`. It adds read-only fields to
   *Purchase Order*, *Material Request* and *Stock Entry* (for a stock transfer): the proposal reference (makes creation
   safe to retry: one approval can never create two documents) and "Approved by (Stock Assistant)" (ERPNext only sees the
   service account, so the real approver is written on the document). Until it has run, orders, requisitions and
   transfers are refused with that instruction. Run it again after updating to a version that adds a document.

### 4b. Signing in, and who can do what

People sign in with their **ERPNext username and password**; nobody is set up twice. The console sends them straight
to ERPNext's own login, keeps only the resulting ERPNext session in server memory (never on disk), and asks ERPNext,
as that person, for their roles and the permission table. What they may do follows from it:

| Level | Rule (read from ERPNext) |
|---|---|
| Look | the person may read *Item* or *Purchase Order* |
| Decide | the person may **create *Purchase Order*** (so nobody can create, through this app, an order ERPNext would not let them create) |
| Change settings | the person has the **System Manager** role (or is Administrator) |

There is no ladder: a System Manager who may not create purchase orders can change settings but cannot approve one.
Disabling a user or removing a role in ERPNext takes effect here within 15 minutes (`auth.recheck_minutes`); a session
lasts 12 hours (`auth.session_hours`) and a restart signs everyone out. Wrong-password guessing is throttled (ERPNext
locks accounts, and this endpoint must not become a way to lock your staff out). Settings changes and decisions are
recorded under the person's ERPNext user.

`auth.tokens` (access keys in the settings file) are the administrator's emergency way in, for the CLI and for when ERPNext
is down, accepted only on the server machine by default (see 4c). Keep any such key long and random. Set `auth.erpnext_login: false` to switch the ERPNext sign-in off.
Not yet done: sending WhatsApp requests to everyone who can Decide using the mobile number in their ERPNext profile
(recipients are still listed in the settings file), and the two-step-verification sign-in has only been tested
against a stand-in for ERPNext.

How ERPNext data is read: usage = outgoing Stock Ledger Entries for *Delivery Note* and *Sales
Invoice* (configurable: `consumption_voucher_types`), on-hand/reserved/on-order = *Bin*, lead time =
Item `lead_time_days`. Expected arrival = earliest `schedule_date` of open *Purchase Order* lines not yet
fully received (the agent user also needs read access to *Purchase Order* and *Purchase Order Item*;
without it the date shows as "Date not set" and the "PO arrives too late" check is skipped).
If you consume raw material through *Stock Entry*, add it to the voucher types (note that stock
transfers are also Stock Entries and would count as usage).

### 4c. Purchase requisitions, and who ERPNext shows as the creator

**Requisition or order.** By default approving a purchase proposal creates a draft *Purchase Order*. For items your buyer wants to
requisition first, name their item groups or item codes under Settings > Choosing vendors > "Purchase requisitions first"
(`requisition_first_groups`, `requisition_first_items`). For those the request is a **purchase requisition** (an ERPNext
*Material Request* of type Purchase, draft, with the recommended vendor, price and delivery in its "Stock Assistant note") and a
direct order is not offered. Every other item shows a second button, "Create a requisition instead". Nothing is ever submitted.

**Who is recorded as the creator.** ERPNext ignores an owner sent by an API, so the only way ERPNext itself shows the right person is
for that person to create the document:

| Approved | Created by | In ERPNext |
|---|---|---|
| in the console, signed in with ERPNext | the approver's **own ERPNext login** | Created By = the approver; ERPNext checks *their* permissions and keeps its own history |
| on WhatsApp, or with an access key | the service account | the approver's name and channel are in "Approved by (Stock Assistant)" |

Set `require_personal_login` (Settings > Choosing vendors) if approvals without a personal session should wait for the console instead
of being created by the service account. Because ERPNext applies the person's own permissions, an approver needs the roles ERPNext
itself requires to create the document (in a default ERPNext: *Purchase User* **and** *Purchase Manager*; Purchase Manager alone cannot
read Addresses, which a purchase order links to; for a stock transfer, a role that may create a *Stock Entry*, such as *Stock User*). Run `erp-agents erp-setup`
once: it adds the proposal reference, approver and note fields to Purchase Order, Material Request and Stock Entry.

**Access keys.** Ordinary users never need one. The keys in `auth.tokens` are accepted only on the server machine itself
(`auth.access_keys: local`, the default, **with or without ERPNext sign-in: the demo too**), never through the public tunnel, and the sign-in
screen offers them only there. `off` switches them off. `anywhere` accepts them from any address that can reach the server (the local network, a
tunnel, a proxy); it must be written in the settings file on purpose, start-up prints a warning while it is set, and the keys should then be long and random.
An unknown value is treated as `local`.

### 4d. Stock alerts: told first, asked second

Before the assistant asks you to reorder anything, it tells you what is wrong. For every item that is low it sends an
**information-only stock alert** on the same channels (console, inbox, WhatsApp, Telegram), and only then, for items that have
reached their reorder point, the usual reorder request.

| Level | When | Looks like |
|---|---|---|
| **Needed urgently** | out of stock, or new stock cannot arrive before the stock runs out | `URGENT: Relay 12V (RELAY-12V) is needed urgently` |
| **Low stock** | below a limit you set (days of cover, minimum quantity, lead time) | `Low stock: Bolt M8 x 40 (BOLT-M8)` |
| **Low stock soon** | an early warning: the item will reach its reorder point within the look-ahead window | `Low stock soon: Paint Red (PAINT-RED)` |

An alert has three parts, **Situation** (the numbers), **Why you are seeing this** (the rule that tripped, with your own limits) and
**What happens next**, and three buttons: **Got it**, **Remind me in N days**, **Details**. Pressing a button never orders or changes
anything. An early warning is only an alert: it does not ask for a reorder until the item really reaches its reorder point.

So the same problem is not announced over and over:

* one alert per item per episode; the repeat comes only after `system.stock_alert_repeat_days` (default 7) if the item is still low;
* a worse level (low to urgent) is sent at once and replaces the older alert that is still waiting;
* **Remind me in N days** keeps that item quiet for N days, unless it gets worse;
* when the item recovers, a waiting alert is closed ("stock is back above your limits"), and a new episode alerts again;
* at most `system.max_notices_per_run` (default 20) alerts per check, the most urgent first;
* alerts are not nagged with reminders, and they never expire into an approval.

Switch it off with `system.stock_alerts: false` (Settings, Reminders): the first message is then the reorder request itself, as before.
`erp-agents run-once --dry-run` reports the alerts it would send.

### 4e. What the assistant writes to ERPNext

Everything else it does in ERPNext is reading. It writes only the documents below, only after a person presses a button, and it never submits one.

<!-- erp-writes -->
| Document | What it writes | Through which tool and button |
|---|---|---|
| `Purchase Order` | A **draft**, with the approver recorded. Afterwards, only on an order it created itself (found by its reference): change the quantity, delete the draft, or cancel the submitted order | `prepare_order_request`, then **Create order**; later **Change quantity** and **Cancel order** (or the same words typed in a chat) |
| `Material Request` | A **draft** purchase requisition, for the items and groups you chose to requisition first, or when a person picks "Create a requisition instead" | `prepare_order_request`, then **Create a requisition instead** |
| `Stock Entry` | A **draft** of type Material Transfer. No stock moves until someone submits it in ERPNext | `prepare_stock_transfer`, then **Create transfer** |
| `Communication` | An e-mail to a supplier through ERPNext's own outgoing mail (recorded on the document there), only after a person presses Send | The late-delivery reminder and `prepare_quote_request`, then **Send reminder** / **Send requests** |
| `Custom Field` | Two fields, a reference and the approver's name, on Purchase Order, Material Request and Stock Entry, once | `erp-agents erp-setup`, an administrator's command (not a tool) |
<!-- /erp-writes -->

Never written: a submitted document of any kind (a person submits in ERPNext), Supplier Quotations, Purchase Receipts, Purchase Invoices, payments, items, suppliers, stock counts or ERPNext settings. The assistant's own settings and records live in its own database. [tests/test_docs_match_code.py](tests/test_docs_match_code.py) compares this table with the pack manifest and the ERPNext client, so a new kind of write cannot ship without being listed here.

## 5. Approval channels

Every channel is only a way to *show* the same approval and *carry back* the answer. The first
answer wins; late presses from another device get "Already answered".

| Channel | Setup | Notes |
|---|---|---|
| **console** | none | terminal output; `erp-agents approvals decide <id> approve` |
| **inbox** | none | the in-app inbox = the **backend for your mobile app** (below) |
| **telegram** | create a bot with @BotFather; token in `TELEGRAM_BOT_TOKEN`; your chat id in `OWNER_TELEGRAM_CHAT_ID` (section 5b) | free; buttons; no templates, no 24-hour limit; polling works on a PC with no public address |
| **whatsapp** | Meta Business Cloud API: token, phone-number id, app secret, verify token | list/buttons; see limits below |

Edit `channels:` and `approvals.recipients:` in `settings.yaml`. A recipient can have several
channels; `strategy: all` sends on every one, `failover` stops at the first that works. Only the
people listed as recipients can answer - strangers who message the bot are ignored.

*WhatsApp limits*: Meta only allows free-form business-initiated messages inside a 24-hour window
after the user last wrote to your number. Set `template_name` (env `WHATSAPP_TEMPLATE_NAME`) and the
channel falls back to that approved template outside the window. Create it in WhatsApp Manager as a
Utility template with body `Stock alert: {{1}}` and three **quick-reply** buttons in this order:
`Approve`, `Remind me later`, `Ignore`. Information-only stock alerts (section 4d) need their own template, because
their buttons say something else: name it in `WHATSAPP_ALERT_TEMPLATE_NAME`, body `Stock alert: {{1}}`, buttons
`Got it`, `Remind me later`, `Details`. Pressing a button comes back through the webhook like any other
reply. Without a template, delivery outside the window fails and is recorded (other channels still
deliver). Webhooks need a public HTTPS URL (use a tunnel such as cloudflared/ngrok during development).

*Telegram webhook mode* (production on a VPS): set `mode: webhook`, `webhook_secret`, run
`erp-agents telegram set-webhook https://your.host/webhooks/telegram`.

### 5b. Telegram setup (about ten minutes)

Telegram carries the same alerts and requests as WhatsApp, with the same buttons, and it needs no message templates and has no 24-hour window: every alert arrives.

1. **Create the bot.** In Telegram open **@BotFather**, send `/newbot`, choose a name and a user name ending in `bot`. It replies with a **token**. Treat it like a password (anyone holding it controls the bot; if it leaks, send `/revoke` to BotFather).
2. **Put the token in `.env`:** `TELEGRAM_BOT_TOKEN=...`. Leave `TELEGRAM_MODE=polling` (nothing public is needed; the app asks Telegram for button presses).
3. **Start the app:** `erp-agents --config config/settings.erpnext.yaml serve`.
4. **Find your chat id.** Open your new bot in Telegram and press **Start** (or send `/start`). The bot answers with *your Telegram chat id*. (Before the app is running: `erp-agents --config config/settings.erpnext.yaml telegram chats` lists people who wrote to the bot.)
5. **Put it in `.env`:** `OWNER_TELEGRAM_CHAT_ID=...` and restart the app.
6. **Check it:** `erp-agents --config config/settings.erpnext.yaml telegram status` (bot, mode, whether each recipient is ready) and `... telegram test` (sends a test message).

More approvers: add a `{ type: telegram, address: "<their chat id>" }` line to their entry under `approvals.recipients`. Each person must first press Start in the bot (Telegram does not let a bot write to someone who never started it), and the bot gives them their id.

How it behaves
* Buttons appear under the message. After somebody answers, the buttons disappear and the bot replies with the outcome. The first answer wins; a late press from another device is told "already answered".
* **Who may answer:** only the chat ids listed as recipients, and only in a **private chat with the bot** (group and channel ids are refused at start-up with an explanation). Anybody else who writes to the bot gets only their own chat id back (at most once every 30 seconds) and nothing else changes.
* **Who ERPNext shows:** a Telegram person is not an ERPNext user, so, as with WhatsApp, the service account creates the draft order or requisition and writes `owner via telegram` in its "Approved by (Stock Assistant)" field. With *Only create orders and requisitions under the approver's own ERPNext name* switched on (Settings, Choosing vendors) such an approval waits until it is approved in the console.
* **Both WhatsApp and Telegram?** With `approvals.strategy: all` the same request goes to every channel of every approver. If you would rather use Telegram and keep WhatsApp as the fallback, set `strategy: failover` and list `telegram` before `whatsapp` in the person's channels.
* **Webhook mode** (`TELEGRAM_MODE=webhook`) is for a server with a fixed public address: set `TELEGRAM_WEBHOOK_SECRET` (letters, digits, `_`, `-`), then `erp-agents ... telegram set-webhook https://<public address>/webhooks/telegram`. The address must be https, and requests without the secret are refused. It also works with `server.remote_access: webhooks_only`. A temporary tunnel address changes at every restart, which is why polling is the default.
* **The bot token never appears** in a log line, an error message or the database; network errors are shown as `could not reach Telegram` with the token masked.

### 5c. Talking to the assistant from Telegram or WhatsApp, and changing a request before you order

You are not limited to buttons. Everything below works the same on Telegram and WhatsApp (WhatsApp cannot show "reply to a message" links, so use the buttons there).

**Ask anything about stock, vendors or orders.** Just type it: `how much FAS-0004 do we have?`, `compare vendors for 500`, `status of PUR-ORD-2026-00549`, `what is waiting for my OK?`. It is the same read-only chat as the console's Ask page: the answer comes from ERPNext's numbers and the rules the app already uses, a follow-up ("compare vendors") continues with the item you just discussed (for 15 minutes), and if it cannot answer it says so. It has no way to approve anything: typing "approve it" does nothing.

**Ask for an order.** Type an instruction that starts with the action, for example `create a purchase order for FAS-0004`, `create purchase orders for FAS-0004 for 800`, `raise a PO for copper rod 10mm`, `reorder FAS-0004` or `order 500 of FAS-0004` (a "purchase requisition" works too, and `please` / `can you` in front is fine). Only people listed as approvers can do this. The assistant then prepares **the same request it prepares by itself**: it compares your vendors for the quantity you gave (or, if you gave none, the quantity it would suggest from your usage), and sends the request with **Create order**, **Change quantity** and **Change vendor**. Nothing exists in ERPNext until you press **Create order**, which makes the usual *draft* Purchase Order. Things it checks first:

* A request for that item is already waiting: you are not given a second one; the waiting request is shown to you again with its buttons (and, if you typed a different quantity, told to press **Change quantity**). A reorder suggestion that is still waiting is shown again too: press **Start reorder** on it.
* No item, or a name that fits several items: it asks which one. Zero, negative or silly quantities, and quantities below the item's minimum order quantity in ERPNext, are refused with the reason, and nothing is prepared.
* No usage history and no quantity given: it asks "How many?" instead of guessing.
* The item has a **minimum order quantity** in ERPNext and the suggested quantity is lower: the request is for the minimum, and says so (ERPNext refuses a purchase order below it). This applies to every order request, not only typed ones. A request that was made earlier for less than the minimum cannot be approved: you are told the minimum and what to do.
* The waiting request is from an older version of the app (it has no **Change quantity** / **Change vendor** buttons) or is below the minimum: it is rebuilt, the old one is closed, and the new one is sent to you with the buttons.
* Questions are still questions: `status of purchase orders`, `which purchase orders were created this week` and `how do I create a purchase order` only read information.

**Ask about one request: reply to its message.** Long-press (or swipe) a request and reply "why this quantity?" or "why this vendor?". That question goes to the local AI helper (Ollama), which answers **only from the numbers recorded for that request**; every answer is checked against those numbers and shown with the lines it used ("From your data:"). If the check fails you get "I do not have enough information to answer that", never a guess. The helper must be switched on (Settings, the "Ask why" helper) and Ollama running; if not, you are told, and the question is answered from your stock and vendor data instead. A model can take a minute, so it works in the background and the buttons stay usable meanwhile.

**Change the quantity or the vendor before you order.** Every order or requisition request has **Change quantity** and **Change vendor** buttons (also in the console):

* *Change quantity*: the bot asks "How many?"; type the number (or reply to the request with just the number, e.g. `800`). The assistant checks the vendors again for that quantity (totals, duties, quotations, minimum order quantity), and replaces the request with a new one that says "Quantity changed from 500 to 800 by you". The old message loses its buttons and its buttons no longer work. Refused, with the reason and nothing changed: zero or negative, not a number, the same quantity, more than 100 times the current one (a typo guard), or below the item's minimum order quantity in ERPNext.
* *Change vendor*: the bot lists the other vendors that were compared (with their totals and delivery times); reply with the number or the name. The request is replaced by a new one that says "You chose this vendor instead of the assistant's first choice" and still names the first choice. The quantity stays.
* After **Create order** the assistant replies with the number of the draft it created ("Draft purchase order PUR-ORD-2026-00740 was created in ERPNext for ... It is
  only a draft: open it in ERPNext, check it and submit it"): under the request on Telegram, as the reply on whichever channel you answered, and as a message on
  WhatsApp to everyone who was sent the request. WhatsApp only delivers a plain message while its 24-hour window is open (that is, you wrote to the number in the last
  24 hours); otherwise the number is in the console and on Telegram. If nothing could be created, no number is sent and the request stays open.
* Nothing is ordered by changing. You still press **Create order**, which makes the same **draft** Purchase Order in ERPNext as before, for the changed quantity and vendor. For anything else (delivery date, price, extra lines) approve and edit the draft in ERPNext before submitting it.

**Change or cancel an order that already exists.** Right after the "was created in ERPNext" message, a second message (the last one in the chat, and a card in the console under *Needs your OK*) shows the order with **Change quantity**, **Change vendor**, **Cancel order** and **Keep it**. You can also ask in chat: `cancel PO PUR-ORD-2026-00740`, `cancel the order for BOLT-M8`, `edit PO PUR-ORD-2026-00740`.

* *Cancel order*: you are asked first ("Reply yes or no", or Yes / No buttons in the console and the Ask page); nothing is removed until a yes. A **draft** is deleted in ERPNext; a **submitted** order is cancelled, but only if nothing has been received or invoiced against it (otherwise you are told to deal with it in ERPNext first). The proposal is marked cancelled, the buttons close, and the item stops counting as "ordered recently", so a new shortage is told at once.
* *Change quantity*: the bot asks "How many?" and the quantity is changed **in the same purchase order** in ERPNext: same number, same vendor, same price, nothing deleted or re-created. A draft is saved with the new quantity; a submitted order is changed with ERPNext's own **Update Items** and stays submitted (if you already sent it to the vendor, tell them). It cannot go below what has already been received or invoiced, and a closed, on-hold or cancelled order is refused. The other lines of the order are kept exactly as they are. You get "Done: ... is now for 800 (was 500)" and a fresh message with the buttons for the changed order.
* *Change vendor*: an order's vendor cannot be changed in ERPNext, so you get a **revised request** saying which order it replaces. The order is not touched until you press **Create order** on it; then the old draft is removed and the new one made (it carries the same proposal reference with a suffix, so a retry never doubles it). **Reject**, **Remind me**, expiry or cancelling the revised request leaves the old order exactly as it was. If the old order has since been received or invoiced, or ERPNext will not remove it, nothing is replaced and you are told why (press **Reject** to keep it).
* *Only orders this application created.* An order made by hand in ERPNext, or by another process (no matching "Stock Assistant" reference), is refused with "I can only cancel purchase orders that I created". Requisitions have no such buttons. Only someone who can approve orders may use them, and the AI agents and the MCP tools cannot cancel or change anything.
* *ERPNext permissions.* Changing the quantity needs **Write**, deleting a draft needs **Delete**, cancelling a submitted order needs **Cancel**, on Purchase Order. When you are signed in to the console the order is removed under **your own** ERPNext login (so ERPNext checks your roles and records you); from Telegram or WhatsApp the service account does it, so give that role Delete (and Cancel, if you want submitted orders cancelled) on Purchase Order, or leave it out and cancel in ERPNext. If ERPNext says no, the message names the permission and nothing is marked cancelled. With *Only create orders and requisitions under the approver's own ERPNext name* on, cancel from the console.
* On WhatsApp this message is a plain message (it needs the 24-hour window to be open); it never uses the order template, whose buttons say something else.
* Say `cancel` to drop a question the bot asked you. Anything that is not an answer is treated as a new question.

Only the people listed as recipients can do any of this; a stranger who writes gets only their own chat id. At most 6 messages a minute per person reach the helper.

### Your own mobile app later

The app needs nothing new on the server. It authenticates with a token (`auth.tokens`, role
`approver`) and uses:

```
GET  /api/v1/approvals?status=pending       list of requests (full content + options)
GET  /api/v1/approvals/{id}                 one request, including the "details" sections
POST /api/v1/approvals/{id}/decision        {"action": "approve" | "snooze" | "dismiss" | "details",
                                              "params": {"days": 5}}      # optional, e.g. custom snooze
```

For push notifications, pass a `push_hook` to `InboxChannel` (e.g. Firebase) - failures never block
delivery. To add another channel (Slack, email, ...): subclass `Channel`, implement `send_approval`
and `send_text` (plus `parse_webhook` for replies), and call `register_channel_type()`.

## 6. Run it

```powershell
erp-agents run-once --dry-run      # analyse only, send nothing
erp-agents run-once                # real run: creates approval requests
erp-agents serve                   # API + daily schedule + Telegram listener (port 8100)
```

Open `http://127.0.0.1:8100/` and sign in with your ERPNext account (section 4b). Access keys in `auth.tokens`
(roles: `viewer` read-only, `approver` + answer approvals, `admin` + change settings) remain as the emergency way in.
Keep the server on localhost or behind HTTPS.

## 7. Rolling out safely

1. **Dry run for 1-2 weeks**: `run-once --dry-run` daily; compare with what you would have ordered.
2. Tune thresholds per group/item until the alerts match your judgement.
3. Switch on one channel (console, then Telegram), still only to yourself.
4. Go live. Look at `erp-agents alerts` and `GET /api/v1/inventory/alerts` for the stored reasoning
   behind every alert (the decision log).

### 7b. Security (penetration test of 19 Oct 2026)

Full report with what was found, fixed and still open: [docs/PENTEST_REPORT.md](docs/PENTEST_REPORT.md). What it means for you day to day:

* **Keys.** The console key in `.env` (`ERP_AGENTS_OWNER_TOKEN`) must be long and random (24+ characters); there is no fallback value. A short key works only on this machine, never through a tunnel. The well-known demo key (`demo-owner-token`) is refused outright: the program does not start with it unless the settings say `auth.allow_demo_key: true`, which is accepted only with the built-in demo ERP (it is set in `config/settings.yaml`, the demo, and nowhere else).
* **What a visitor through the tunnel can reach** (`server.remote_access`): `full` (console, sign-in, API and webhooks) or `webhooks_only` (only the WhatsApp/Telegram webhooks and a liveness check; everything else answers 404). Choose `webhooks_only` if nobody needs the console away from this machine.
* **Browser protections** are on for every answer (no framing, a policy that lets the console run only its own script, no caching of API answers). Size limits: webhooks 1 MB, sign-in 16 KB, other API calls 256 KB.
* **API documentation** (`/docs`) is off; `server.docs: true` turns it on.
* **The ERPNext account the app uses** should hold only the four roles it needs (Purchase User, Stock User, Accounts User, Accounts Manager), not System Manager. Check with `erp-agents erp-check-access`.
* **Files.** `.env` should be readable only by you: `icacls .env /inheritance:r /grant:r "$env:USERNAME:F"`. `erp-agents doctor` checks this (section 7c).
* **Who can answer from a chat.** WhatsApp and Telegram answers are trusted by the address written in `settings.yaml`, and are not re-checked against ERPNext when they arrive: remove a person from the file when they leave.

### 7c. Operating it: `doctor`, backups, one instance

```powershell
erp-agents doctor              # read-only: file permissions, exposure, settings, database, backups, approvers, and how it is running (scheduler, disk, clock, waiting requests, decision history). No network, changes nothing.
erp-agents backup              # a verified copy of the database in data/backups/ (safe while the application runs; never overwrites)
erp-agents backup-verify FILE  # is this a complete, undamaged database that this version understands?
erp-agents restore FILE --force   # stop the application first; the database it replaces is kept beside it as .before-restore-<time>
```

`doctor` exits with an error for things to fix now (a secret written into the settings file, access keys open to anywhere with a weak key, a damaged database, MCP open to other machines without host names) and prints warnings for the rest (`--strict` makes warnings an error too). It cannot check the ERPNext service account (that needs ERPNext: `erp-agents erp-check-access`) or anything outside this computer. The backup holds the database only: copy `.env` and the settings file separately, and keep a copy somewhere other than this computer. Back up before every upgrade; an upgrade that changes the schema also copies the old file aside by itself.

### 7d. Watching it run: logs, `/ready`, metrics, traces

Every agent task has an id that appears on every log line it causes (`[task=...]`), so one question can be followed from its first line to its last. `GET /ready`, beside the plain `/health`, says whether the database accepts a write and the scheduler is alive (200 or 503). Prometheus metrics at `/metrics` and OpenTelemetry traces are optional and off until you switch them on in the `observability:` section; log lines can be written as JSON for a log collector. No key, token or phone number reaches a log, and no question, answer or item is in a metric or a trace. How to read it all, every metric, and three alert rules (an approval stuck, answers falling back to the plain rules, repeated ERPNext errors): [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).

### 7e. When something fails

When ERPNext or the language model keeps failing, the assistant stops asking it for a while and says so once (a circuit breaker) instead of making every person wait for a timeout, and it never invents a figure meanwhile. A lost answer from ERPNext never makes a second draft, and if the program stops right after ERPNext made a draft, the next start records the answer that was given. A message that WhatsApp or Telegram delivers twice is answered once. Each person is limited per minute for questions and for actions that start a language-model task, and the refusal says how many seconds to wait. What each part does, with the test that proves it: [docs/RESILIENCE.md](docs/RESILIENCE.md).

## 8. How it fits the bigger ERP-agents design

```
src/erp_agents/core/                          what every area shares
  erp/                    adapters (ERPNext, mock)     <- everything else only knows the ERPClient interface
  settings_store.py, storage.py, workorders.py            settings + audit, the database, the work-order queue
  approvals/              channel-neutral service + router + channels/ (telegram, whatsapp, simple console)
  auth/, policy.py        who may do what, from ERPNext's own roles
  toolkit.py, mcp_server.py, agents/                      the tool registry, the MCP server, the agent runtime and its checks
  api/, scheduler.py, cli.py, bootstrap.py                HTTP API, the clock, the command line, start-up
  packs.py                reads each pack's manifest, switches packs on and off, loads them
src/erp_agents/packs/
  stock_procurement/      the stock and procurement pack (the only one with code today)
    inventory/            analysis (maths), messages (explanations), agent (lifecycle)
    procurement/          vendor scoring, taxes, proposals, agent (lifecycle)
    assistant/, agents/   the chat and "Ask why", the two AI agents' prompts and tasks
    tools.py, manifest.yaml, pack.py, settings.py, schema.py, api.py, cli.py, jobs.py, web/, tests/
  sales/, finance/        empty skeletons: a manifest and a README, no code
```

### Packs: stock and procurement is one pack of a shared core

The program is a **core** (ERPNext access, sign-in and roles, approvals and their channels, the tool registry, the MCP server, the agent runtime with its answer
checks, settings, audit) and **packs** that add one area each. Today there is one working pack, `stock_procurement` (everything this README describes); `sales`
and `finance` are empty skeletons that show where the next ones would go. A pack is a folder with a `manifest.yaml` that declares its tools, the ERPNext roles
it needs, which actions need a person's approval, the settings it adds and what an agent may send to a language model. The core reads the manifests, loads the
packs that are switched on, and refuses to start if what a pack registers differs from what its manifest says.

What a pack cannot switch off: the role check on every tool call, the rule that nothing is ordered until a person presses Create order, and the check that
every number in an agent's answer came from a tool. Those live in the core and apply to every pack.

Switching a pack is an administrator setting (`packs.enabled` in the settings file, or `PUT /api/v1/settings/module/packs`); `GET /api/v1/packs` shows each pack
with its state. A pack that is off answers "switched off" at once (its tools, routes, commands and scheduled work); switching one on needs a restart. Its data stays
in the database while it is off. The default is the stock pack on, the others off, so nothing changes for you. How to write a pack:
[docs/DEVELOPING.md](docs/DEVELOPING.md).

Approving "Start reorder" writes a **`REORDER_NEEDED` work order** (item, suggested quantity and its
basis, usage, lead time, stock-out date, the full analysis snapshot). The next module - the
**procurement agent** - consumes it (`WorkOrderQueue.list(status="pending")` / `subscribe()`),
scores vendors, and uses the *same* `ApprovalService` to ask for PO approval. Sales and finance
agents plug into the same pieces.

## 8b. Vendor selector (procurement agent)

Runs every 3 minutes (and on demand): takes each approved `REORDER_NEEDED` work order, reads the item's
suppliers and history from the ERP, ranks them and asks the owner to approve ONE purchase order.

- **Data (ERPNext)**: suppliers from the item's *Supplier* table plus submitted *Purchase Orders* (price = latest rate;
  needs *Purchase Order* read access); lead time and on-time rate from *Purchase Receipts* vs the order date and
  required-by date; credit days from the supplier's *Payment Terms Template*. Anything the agent user cannot read
  is reported as unknown, never invented.
- **Scoring** (`packs/stock_procurement/procurement/scoring.py`, plain maths): cost, delivery (capped when it arrives after you run out),
  reliability and payment terms combined with the weights in *Vendor Selector* (`/api/v1/settings/procurement`).
  The approval message says why this vendor won and, for every other vendor, the factors that cost it the most points.
- **On approval** (`po_mode: draft`): a **draft** Purchase Order is created in the ERP (needs *create* permission on
  Purchase Order, nothing else is written; it is never submitted). If the ERP refuses, the decision is rolled back
  and the owner is told, so pressing the button again retries. `po_mode: off` only records the decision. Changing or
  cancelling that order later (buttons after Create order, or "cancel PO ..." in chat) additionally needs *write* (a new quantity in
  the same order), *delete* (a draft) or *cancel* (a submitted order) permission on Purchase Order; without it those buttons say so and change nothing.
- **When it cannot recommend** (no priced vendor, or no order quantity because there is no usage history) the owner gets
  a plain "needs your decision" message saying exactly what is missing. A *default order quantity* setting avoids the latter.
- CLI: `erp-agents procurement run [--dry-run]`, `erp-agents procurement proposals`. API: `POST /api/v1/procurement/run`,
  `GET /api/v1/procurement/proposals`, `GET /api/v1/procurement/preview/{item}?qty=`.

### Supplier quotations: you decide whether they count

By default the comparison prices each vendor from the **last purchase order** you placed with them, so a Supplier Quotation in ERPNext is ignored. Under Settings, *Choosing vendors*, *Prices from supplier quotations* (`use_quotations`) you can change that:

| Choice | What happens |
|---|---|
| **off** (default) | Exactly as before: quotations are not read at all. |
| **prefer** | A vendor's latest **valid** quotation for the item replaces its last-order price. A vendor you asked to quote but never ordered from can be compared too (it still counts as having no track record, so it is left out if *Never suggest a vendor I have not ordered from before* is on). Vendors with no quotation keep their last-order price. |
| **only** | Only vendors with a valid quotation are compared; the others are shown as "not compared" with the reason. If nobody has quoted you are asked what to do (and told to request quotations in ERPNext), the assistant does not guess. |

What counts as a valid quotation: submitted, not stopped, cancelled or expired, and either not past its end date or, if it has none, not older than *A quotation with no end date counts for (days)* (`quotation_max_age_days`, default 30). The price is ERPNext's own company-currency rate. If a quotation lists the item more than once the lowest price is used; the newest valid quotation of each vendor wins. A quoted delivery time is used only for a vendor with no delivery history, and says so. If the quotation was for fewer units than you need you are warned.

Everything says where the price came from: the approval message ("Price from supplier quotation PUR-SQTN-…, valid until …"), the vendor table on the Buying page, the chat, and the requisition note. When the order is created, the draft Purchase Order's item is **linked to the quotation** in ERPNext (when ERPNext would accept it: same supplier and currency) at exactly the quoted rate, and the vendor note says "The price is as per your quotation …". The ERPNext user the app runs as needs read access to *Supplier Quotation* (Purchase User has it; `erp-agents erp-check-access` checks).

## 8b-1. Landed cost: duties and taxes in the vendor comparison

Vendors are compared on **landed cost** = goods (price x quantity) + the duties and taxes your **ERP** applies to a
purchase of that item from that vendor, so a cheap import with heavy duty does not win on unit price alone. Nothing is
entered in this application: it reads the ERP's own tax setup (ERPNext shown here).

- **Which template**: ERPNext's *Tax Rule* (type Purchase) selection, done on the rules read through the normal API (this
  ERPNext does not expose its own function): date window; the supplier's *tax category* must match exactly (blank = blank);
  supplier / supplier group / company must be blank or equal; the rule that sets the most of those wins, then priority.
  No rule matches: the company's *default* purchase taxes template. Rules that also depend on addresses, item or item group
  cannot be judged without the order and are skipped **and reported**.
- **The rates**: the template's rows (*Purchase Taxes and Charges*), calculated the way ERPNext does: On Net Total, On
  Previous Row Amount, On Previous Row Total, On Item Quantity, Actual; Add/Deduct. Category *Valuation* and *Valuation and
  Total* rows are cost (duty, freight, non-creditable tax); category *Total* rows are payable but not item cost (e.g.
  creditable input tax): shown, not counted (`include_recoverable_taxes_in_cost` counts them). An *Item Tax Template* on the
  item that fits the supplier's tax category and date replaces the rate of matching rows.
- **Nothing is hidden**: no tax setup for a vendor, an unreadable template, a row type this agent cannot calculate, a
  tax-inclusive row: each is stated in the request ("duties and taxes may be missing/wrong: ..."); `no_tax_setup: exclude`
  refuses to recommend vendors whose duties cannot be checked. With `include_taxes: false` the comparison is on price only.
- The draft purchase order is created with the template the ERP chose for the winning vendor (`taxes_and_charges`).
  Checked against a live ERPNext v16 draft order (case PO-05 in [docs/E2E_TEST_RESULTS.md](docs/E2E_TEST_RESULTS.md)).
- Needs read access for the agent user to *Tax Rule*, *Purchase Taxes and Charges Template*, *Item Tax Template*, *Item* and
  *Supplier*. To change which duties apply, change them in the ERP.

## 8b-2. Demand calendar and the vendor note

**Calendar** (*Configuration → Calendar*, or `PUT /api/v1/settings/module/calendar`): the owner lists festivals and
promotions with dates, an uplift in % (negative for a slump) and which items (all, item groups, or item codes). On
those days expected demand = base rate x (1 + uplift/100); overlapping events use the strongest rise, never a product.
It changes days of cover and the stock-out date, the early-warning crossing date, and the suggested order quantity
(expected demand over lead time + safety + target days, event days included). The message states the effect with both
figures ("cover is 10 days instead of 20 days") and the suggested-quantity basis names the events. No event, no
event in range, or `enabled: false` = every number is exactly as before. History-based seasonality may already include
recurring festivals, so add events for what history cannot show.

**Vendor note** (*Configuration → Procurement → Note to the vendor*): the owner's template (placeholders `{item}`,
`{item_code}`, `{qty}`, `{uom}`, `{required_by}`, `{reference}`, `{vendor}`, `{unit_price}`, `{total}`) filled from the
proposal. Two fixed sentences are added only when a recorded fact calls for them: delivery after the stock-out date
(your editable "urgent" text) and a price above that vendor's own average. The owner sees the exact text in the
approval message; the draft purchase order gets the same text (ERPNext: Terms and Conditions).

## 8c. "Ask why" (local language model, checked answers)

Off by default; switch it on in *Configuration → Assistant (local AI)*. Uses Ollama on this machine
(`http://127.0.0.1:11434`, model `llama3.1`); a non-local address is refused unless `allow_remote` is on, so stock
and supplier data stay on this server.

- The model sees only numbered facts copied from the stored request (the text the owner received, the rule checks,
  the vendor scores) and the question. It must reply in a fixed JSON format, citing the fact ids it used.
- Repeatable: temperature 0, fixed seed, `top_k` 1, and every answer is stored, so the same question about the same
  request returns the same answer.
- Checked in code (`core/assistant/verify.py`) before anything is shown: the cited facts must exist; every number in the
  answer must appear in the facts or the question; no guessing words ("probably", "assume", ...); length limited.
  If any check fails, or the model says it cannot answer, the person sees *"I do not have enough information in the
  recorded data to answer that question."* The rejected draft is kept in the database for audit only.
- It explains; it never decides. Scoring, thresholds and approvals are unchanged.
- API: `POST /api/v1/assistant/ask {approval_id, question}`, `GET /api/v1/assistant/history/{approval_id}`,
  `GET /api/v1/assistant/status`. Speed on a laptop CPU: the first question loads the model (minutes), later ones
  take seconds to tens of seconds.

## 8d. Ask (chat with the assistant)

The **Ask** page answers questions about stock, vendor comparison, and purchase order / requisition status. It only reads; it can
approve or change nothing. Anyone who can look can use it; the inbox of requests waiting for approval is shown only to people who may
answer them.

How an answer is made (nothing is guessed):
1. **Understand** the question with plain rules: which item (by code, or by its full name; a vague name such as "bolt" gets "did you mean
   ...?"), which document number (for example `PUR-ORD-2026-00543`), a quantity ("for 500"), a period ("this week"), a status
   ("open", "draft", "overdue"). A follow-up such as "and the vendors?" keeps talking about the item before.
2. **Fetch** live data with a read-only tool: the stock analysis (the same one behind the Your stock page), the vendor ranking (the same
   one behind Buying, in company currency with duties and taxes), ERPNext's own Purchase Orders / Material Requests / Purchase Receipts, and
   the request inbox.
3. **Answer** with fixed sentences built from that data, with the numbered facts it came from ("Where this comes from"). If an item or
   quantity is missing or ambiguous it asks; if the data holds nothing it says so; if the question is outside stock, vendors and
   purchase documents it says what it can do.

The local AI helper (Settings > "Ask why" helper) is optional. With it on, it is used in two narrow ways: to pick one of a fixed list of
question types when the rules cannot place a question (it never chooses items, numbers or wording), and for an "Explain in plain words"
button under an answer, shown only if it passes the same checks as "Ask why" (cites the facts, invents no number, does not guess).
With it off, everything else works the same. Every question is logged (who, when, what kind, how it was understood); 20 questions a
minute per person. The service account needs to read Material Request for requisition questions (`erp-agents erp-check-access` lists
what it needs).

### 8e. Test data for trying the app

`python -m tools.testdata.cli days` loads two live business days (yesterday and today) on top of the existing history: quotation and requisition cycles, receipts against old open orders, payments on old invoices, returns, reserved stock, stock issues, a stock count and shipments (about 137 documents; it is safe to run again and `--only rfq1,mr1,oldpo` runs chosen blocks). Details and counts: [docs/E2E_FINDINGS.md](docs/E2E_FINDINGS.md). The app measures an item's usage from the earlier of its creation date and its **first stock movement**, so history loaded into ERPNext after the item was created still counts.

`python -m tools.testdata.cli features run` adds **28 materials (`TST-...`), four test vendors and three test customers, one set of documents for each purchasing follow-up and stock-intelligence feature**: late orders, receipts that do not match, a vendor scorecard, quote requests, a festive-season jump, slow and dead stock, substitute items and stock counts (about 610 documents). What each material shows, its stock position and what to ask: [docs/FEATURE_TEST_SCENARIOS.md](docs/FEATURE_TEST_SCENARIOS.md).

`python -m tools.testdata.cli scenarios` adds **ten materials with a long, readable history** for trying the vendor comparison and the consumption analysis (about
4,400 documents for 1 Dec 2025 to 20 Oct 2026; it takes about an hour and is safe to run again). Each material has four to six vendors in different places with
different prices, lead times, reliability and price trends, and six to eight customers (Indian and export); the purchases and sales are real documents (Purchase Order,
Receipt, customs entry, Purchase Invoice, payment; Sales Order, Delivery Note, Sales Invoice, payment). `python -m tools.testdata.scenario_report` prints what ERPNext
now holds: consumption by month, and every vendor as the app reads it.

| Item | Demand pattern | Vendors (places) | Stock today |
|---|---|---|---|
| MET-0025 Copper Rod 20mm | grows about 10% a month | Mumbai, Ahmedabad, Dubai, Busan, Houston | fine |
| MET-0026 Stainless Steel Sheet 304 8mm | dips in the rains, peaks in March and October | Ahmedabad, Mumbai, Singapore, Busan, Istanbul | **out of stock**, orders waiting |
| MEC-0025 Deep Groove Ball Bearing 6305 | jumps 2.5 times on 1 June (two new OEM customers); one vendor dropped, a cheaper one added | Rajkot, Pune, Wuxi, Osaka, Dusseldorf | fine |
| MEC-0026 Gate Valve Cast Iron 8 inch | a few a month plus seven big project orders | Hyderabad, Pune, Cleveland, Turin, Shanghai | fine, slow-moving |
| CHM-0025 Hydraulic Oil ISO 150 | steady | Kolkata, Hamburg, Houston, Yokohama, Singapore | fine |
| CHM-0026 Industrial Epoxy Paint Yellow | falls to a third (phased out) | Kolkata, Hamburg, Yokohama, Houston | about ten days left |
| ELE-0025 Industrial Relay 24V 50A | month-end bursts | Chennai, Shenzhen, Hanoi, Stuttgart, San Jose | fine |
| ELE-0026 Miniature Circuit Breaker 100A | rare, lumpy orders | Chennai, Shenzhen, Hanoi, Incheon | fine |
| FAS-0025 Hex Bolt SS304 M16x90 | grows to March, then level | Pune, Ludhiana, Mumbai, Ningbo, Ho Chi Minh City, Istanbul | **out of stock**, orders waiting |
| PKG-0025 Stretch Film 500mm 30 micron | festival peaks in October/November and March | Noida, Guangzhou, Da Nang, Bangkok, Sharjah | fine |

The two out-of-stock materials and the paint will raise stock alerts at the next stock check.


## 8f. AI agents and the MCP server (optional)

Two agents sit on top of the rules, using the same tools that an MCP server publishes to any AI client (Claude Desktop and others), <!-- tools:counts -->17 tools: 14 read-only and 3 prepare-only<!-- /tools:counts -->:

- the **inventory agent** writes the morning brief (what needs attention, in what order, anything odd);
- the **purchasing agent** gives a second opinion on an order request and answers the questions the Ask page cannot place.

They are **off by default** and need `pip install "erpnext-ai-agents[agents]"` and a language model that you choose under **Settings > AI agents**: **a model on this computer** (Ollama, for example `ollama pull llama3.1`: free, nothing leaves your computer, slow without a graphics card) **or a hosted one** (Claude, OpenAI or Gemini: fast, billed per token; you add its key to the `.env` file and confirm that what the agents read is sent to it). Tokens used are shown per answer and per month, with an optional monthly limit. The agents are fenced: no tool orders, approves, pays or changes a setting; every number an agent writes must come from a tool's result or the answer is replaced by the plain-rules version; a person still presses Create order. What is sent to the model, and that it stays on your computer unless you allow otherwise, is on **Settings > AI agents** and in [docs/PRIVACY.md](docs/PRIVACY.md).

```bash
erp-agents --config config/settings.erpnext.yaml agents brief --rules     # today's brief by rules alone, no model
erp-agents --config config/settings.erpnext.yaml agents brief             # the same, with the model's headline and extras
erp-agents --config config/settings.erpnext.yaml agents ask "which warehouse is the busiest?"
erp-agents --config config/settings.erpnext.yaml agents tasks             # every task, with the model used and how long it took
erp-agents --config config/settings.erpnext.yaml agents usage             # tokens used this month and by model, against your monthly limit
erp-agents --config config/settings.erpnext.yaml mcp                      # the tools over stdio, for Claude Desktop
```

Design, the tools, the checks, measured speed on a laptop CPU and the limits: [docs/AGENTS.md](docs/AGENTS.md).

The assistant's answers are checked on every change: 122 golden questions are replayed in CI with a scripted stand-in for the language model (bad replies included), and the build fails when a wrong reply could reach a person, when something is written to ERPNext, or when a request is prepared that nobody asked for. The same questions can be run on a real model, and on two side by side: [docs/EVALUATION.md](docs/EVALUATION.md).

## 8g. Purchasing follow-up and stock intelligence

Eight features that close the loop on purchasing and look deeper into stock. Each one is a **tool** in the same registry as the others (so the inventory and purchasing
agents, any MCP client, the REST API and `erp-agents followup ...` all get the same numbers), and the scheduled ones run on their own as well. Settings: **Settings →
follow-up** (`followup:` in the settings file); every part can be switched off.

| Feature | What it does | When | Tool |
|---|---|---|---|
| **Delivery chasing** | Finds open purchase orders past their due date with goods still to come (after `chase_grace_days`). Tells you how the delay affects stock cover: how long stock on hand lasts without the delivery, and with it. **Send reminder** sends the message shown to the vendor; Ask me later / Stop chasing. At most `chase_max` reminders per order; the chase ends when the goods arrive. | daily (`daily_cron`) | `late_deliveries` |
| **Goods receipt check** | Compares each goods receipt with its purchase order: more than was still to come, rejected goods, a price on the receipt or invoice `price_tolerance_pct` or more off the order, a receipt without an order. Says whether the invoice is paid yet ("hold the payment" / "ask for a credit note"). A partial delivery is not a mismatch. Changes nothing in ERPNext. | daily | `receipt_mismatches` |
| **Vendor scorecard** | Ranks vendors on on-time delivery (50 points), price drift (25) and quality: rejected goods and receipts that did not match (25), over `scorecard_days`, with a plain sentence on why a vendor ranks lower ("Ranked below Prime Supplies because it delivered on time 50% of the time against 100%; its prices rose 7.5%"). | on demand | `vendor_scorecard` |
| **Quote requests** | Picks two or three vendors (listed on the item first, then by record) and prepares a request that shows who would be asked, how, and the message. **Send requests** messages them. Prices come back as **Supplier Quotations in ERPNext** (the vendor enters one from the e-mail, or you type in what they replied: a price is never read out of a message). When all have quoted, or after `quote_reply_days`, you get the comparison; **Prepare order** makes the usual purchase request with that vendor at the quoted price. | the comparison: hourly | `prepare_quote_request` (prepares only) |
| **Demand forecasting** | Decides the order point from last year's seasonal pattern for the lead-time days and the open sales orders due in them, not only average daily use. Seasonality only ever raises the order point. A sales order is counted once: if your stock settings already subtract "reserved for sales orders", it is not added again. Policy fields `forecast_reorder_point` and `sales_orders_in_forecast`. | every stock check | `demand_forecast` |
| **Slow and dead stock** | Items that tie up cash: dead (no use for `dead_after_days`) or slow (stock lasts more than `slow_cover_days`), with the value tied up (ERPNext's stock value) and a suggested action: return to vendor (bought within `return_window_days`), cut the open order, discount or clear, stop reordering. | weekly (`weekly_cron`) | `slow_stock` |
| **Substitute items** | When an item is short and a new order cannot arrive in time, the alert names an **approved** alternative that is in stock: only what ERPNext lists under Item Alternative (two-way records work both ways), never a guess from names. | in each alert | `substitutes` |
| **Stock count assistant** | Classes items A/B/C by the value of what you use (80 / 15 / 5 %), counts A every `count_a_days`, B every `count_b_days`, C every `count_c_days`, and sends the week's list (negative stock always first). When a count is posted as a Stock Reconciliation, each difference of `variance_notable_pct` or more is explained from what ERPNext shows: a receipt still in draft, stock in transit, a count that is a whole multiple of the system figure (another unit), the same difference at the last count, its size in days of use and its value. | weekly list; differences daily | `stock_counts` |

**How vendors are reached.** Only after a person presses Send, and only to the **e-mail address or mobile number on the supplier's contact in ERPNext** (the
Supplier's own e-mail and mobile fields, then its linked Contacts, primary first). E-mail goes through **ERPNext's own outgoing e-mail account**
(`frappe.core.doctype.communication.email.make`), so it is recorded in the timeline of the purchase order or supplier; set up an outgoing Email Account in ERPNext
first. WhatsApp goes through your WhatsApp channel; set `whatsapp_country_code` (for example 91) for numbers written without one, and, to reach a vendor who has not
written to you in the last 24 hours, `whatsapp_vendor_template` (an approved template with one body variable). `vendor_channel` chooses e-mail first, WhatsApp first,
or only one. The address is looked up at the moment of sending and is never stored (only a keyed hash, to recognise a WhatsApp reply), never shown in a request,
never in a tool result and never sent to a language model. A vendor's WhatsApp reply is kept and passed to the people who answered the request, marked as their
words; nothing acts on it.

* **The hash is keyed.** It is HMAC-SHA256 with the secret `ERP_AGENTS_HASH_KEY` from `.env` (32+ characters; see `.env.example`), so a copy of the database alone cannot be tested
  against every possible phone number. Without the key nothing is hashed and a vendor's WhatsApp reply cannot be recognised (the start-up log and `doctor` say so; e-mail is
  unaffected). Messages sent before the keyed hash existed cannot be re-keyed, because the number was never stored: they keep their old hash only until their reply window
  ends, then it is erased. The hash of every message is erased when its window ends.
* **How long a reply is accepted.** `followup.vendor_reply_window_days` (default 30). It ends sooner when the purchase order is received or closed, and when a price round has
  been compared. A message after that is not treated as a reply.
* **A flood of replies.** Per vendor and day, the first `vendor_reply_relay_per_day` (default 3) replies are passed on; after that you get one line saying there are more, and the
  rest are kept with the request (Details). Beyond `vendor_reply_keep_per_day` (default 30) they are dropped, and a reply identical to one already kept is ignored.

**ERPNext permissions** the service account needs in addition: read Purchase Invoice, Sales Order, Contact, Item Alternative, Stock Reconciliation; create
Communication (to send e-mail). `erp-agents erp-check-access` lists them.

**Try it:** `erp-agents followup late | receipts | scorecard | quotes | slow | counts | variances`, `erp-agents followup forecast BOLT-M8`,
`erp-agents followup substitutes OIL-5L`, `erp-agents followup run --dry-run` (the daily pass) and `erp-agents followup weekly --dry-run`. REST: `GET
/api/v1/followup/late-deliveries | receipts | scorecard | quotes`, `POST /api/v1/followup/quotes` (approver; prepares only), `POST /api/v1/followup/run` (admin; dry run by
default), `GET /api/v1/inventory/forecast/{item} | slow-stock | substitutes/{item} | counts?kind=plan|variances`.

**Not tried against a live ERPNext yet.** The ERPNext reads and the e-mail call are written from Frappe's documented REST API and tested with a fake HTTP session
(`tests/test_erpnext_followup.py`). Before relying on it: check that a test e-mail from ERPNext arrives, run `erp-agents followup late` and compare with your open
purchase orders, and look at one receipt check by hand.

## 8h. "Why did you do that?": the decision history

Every past decision, with its reasoning, searchable, as an audit trail:

* **every request the program raised** (stock alerts, reorder suggestions, purchase requests, late-delivery reminders, receipt checks, quote comparisons ...), exactly as the
  person saw it: its reasons and details are stored with it and never rewritten;
* **how it ended**: which button, who pressed it, from which channel and when, and what came of it (for example the draft purchase order's number); or that it expired,
  was cancelled or was replaced by a revised version (with the reason);
* **every change of settings**: who changed which value, from what, to what;
* with each request, in full view: the earlier and later versions of it, what followed from it (managing the order), the AI agents' work on it (a second opinion, a
  request an agent prepared), where it was sent (channel, not the address) and, from the stock pack, the purchase request behind it and the messages sent to vendors.

Search with words that must all appear (an item code, a vendor, an order number), the kind of request, the item, who answered, a date range or the outcome.

```
erp-agents history BOLT-M8                       # every decision about BOLT-M8, newest first, with why
erp-agents history Prime --since 2026-10-01      # all words must appear; dates are YYYY-MM-DD
erp-agents history --show <request id>           # one decision in full
erp-agents history --who priya --csv audit.csv   # export for an auditor or a spreadsheet
erp-agents history --verify                      # has anything been changed or deleted since it was sealed?
```

REST (approvers): `GET /api/v1/history?q=&item=&kind=&who=&since=&until=&status=`, `GET /api/v1/history/{id}`, `GET /api/v1/history.csv`; `GET /api/v1/history/verify`
(administrators). The AI agents have it as the `decision_history` tool, so you can ask "why did you suggest ordering BOLT-M8 from Prime last week?" and get the reasons that
were given at the time. **The agents never see who decided**: they are told "a person", and names are taken out of the text.

**The seal.** Each request is sealed the moment it is answered, expires or is cancelled, and each change of settings when it is made: its content goes into a hash chain
(`audit_chain`), each entry over the one before. `--verify` recomputes the chain and every sealed record, and names any record that was changed or deleted afterwards and
any break in the chain. Decisions from before this version are sealed the first time it starts. This shows a change; it cannot prevent one: someone who can write to the
database file can rewrite the chain as well. For proof that holds up against that, keep backups and copy the latest hash that `--verify` prints somewhere the database
cannot reach (an e-mail to yourself, a note in ERPNext).

## 9. Known limitations / next steps

The short list of what the project does not do, and how each part was tested, is [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). The rest:

- Vendor prices are the latest order rate in company currency (the order's base rate, so foreign and domestic vendors
  compare fairly) or, if you switch it on, the vendor's valid supplier quotation (see "Supplier quotations"). Price lists are not read. A vendor with no past
  order and no quotation for the item cannot be priced.
- The language model is optional and local ("Ask why", chat routing and the AI agents). It never supplies a number: every answer is checked against the recorded facts, and
  if the check fails you are told there is not enough information. On a laptop CPU an 8-billion-parameter model needs minutes per task (docs/AGENTS.md has the
  measurements), so the agents run in the background and never sit on the path of an alert or an approval.
- The MCP endpoint over HTTP is private by design (the server machine only unless `mcp.remote` and `mcp.allowed_hosts` are set) and takes static bearer tokens: it is **not** an OAuth-protected MCP server and AI products that discover servers by themselves cannot use it. Use stdio, or your own private network (docs/AGENTS.md says what real OAuth support would take).
- Recipients and channel credentials are in `settings.yaml` (restart to change); only thresholds and
  the schedule are editable live.
- Reorder decisions use one combined figure (all warehouses summed, or the ones listed); stock is *shown* warehouse by warehouse, but there are no per-warehouse reorder levels.
- Usage is derived from outgoing movements; it does not know about planned promotions or one-off
  bulk orders (the "busy week" line and confidence notes flag lumpy demand, but they do not remove it).
- ERPNext adapter: tested against v16 only. PO arrival dates come from `schedule_date`.
- Single instance: SQLite and the in-process scheduler do not support several replicas (a second `serve` is refused). Schema versioning and numbered migrations exist (the core is at version 3, whose migrations add the table of scheduled-job records (2) and the ids of the messages the channels delivered (3), and are tested on real older files; the stock pack's tables are at version 4, whose migration adds columns to two existing tables and is tested on a real version-3 file).
- Chat approvers are identified by the address in `settings.yaml` and are not re-checked against ERPNext at reply time.
- WhatsApp: only delivered live on the author's own account; see the status note at the top.
- Licence: GPL-3.0-or-later (see `LICENSE`), which the Frappe Cloud Marketplace accepts (open source under MIT or a GPL-compatible licence). That is a licence statement only: this repository is not a Frappe app and is not listed on the Marketplace.
- Roadmap: a Frappe app version, a separate port ([docs/FRAPPE_APP_PLAN.md](docs/FRAPPE_APP_PLAN.md), [docs/PUBLISHING_PLAN.md](docs/PUBLISHING_PLAN.md)), per-warehouse monitoring with a manager per location, and a sales and finance
  agent. Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md); security problems go to [SECURITY.md](SECURITY.md).

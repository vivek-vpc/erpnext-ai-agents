# Privacy: what the Stock Assistant reads, keeps and sends

This page is written for the person who runs the app on their own ERPNext, and as the basis for the privacy-policy entry that the Frappe Marketplace asks every app to publish (the draft policy is at the end). The application's code is the source of truth: `src/erp_agents/core/agents/privacy.py` holds the lists below and `src/erp_agents/packs/stock_procurement/tests/test_privacy.py` fails if a tool starts returning personal data.

**In one sentence:** the app has no telemetry and no server of its own; the only places data goes are the messaging channels and the language model *you* switch on, and the language model is off, and on this machine only, until you decide otherwise.

## What the app reads from your ERPNext

Items, stock levels and stock values, warehouses, delivery and sales quantities (to work out usage), quantities still to deliver on open sales orders (no customer is read), purchase orders, requisitions and receipts, purchase invoices (status and price only), suppliers' prices and delivery history, supplier quotations, item alternatives, stock counts (Stock Reconciliation), and, only at the moment a person presses Send on a message to a vendor, that supplier's e-mail address or mobile number from its contact. It reads with the service account you gave it, and creates *draft* purchase orders or requisitions only when a person presses Create order.

## What it keeps

On the machine that runs it (a SQLite file, `database.path`): its settings, the approval requests and who answered them (kept as the decision history, with a tamper-evident seal; requests are never deleted, so the history goes back to the first one; when an AI agent reads the history it is told "a person", never who), alerts, a log of questions asked to the assistant, and, for the AI agents, a record of every task and tool call (see below). Nothing is copied to the developers.

## What leaves the machine, by feature

| Feature | What is sent | To whom | Default |
|---|---|---|---|
| Approval requests and replies | The request text: item, quantity, vendor, price, reasons; the person's reply | Telegram and/or WhatsApp (Meta), to the people **you** list as approvers | Off until you add a channel |
| Console / REST API | Everything on screen | The browser of a signed-in person (ERPNext sign-in) | On, on this machine only |
| "Ask why" and chat routing (`assistant`) | The facts of one request and the question | The Ollama server in `assistant.base_url` | Off. Must be on this machine unless `allow_remote` is on |
| **AI agents** (`agents`) | See the next section | The language model you choose: Ollama on this computer (default), or Claude (Anthropic), OpenAI or Gemini (Google) | **Off.** On this computer unless you choose a hosted provider **and** tick the box that confirms the data is sent to it |
| MCP server | The tool results listed below | The MCP client you connected (Claude Desktop, another agent) | stdio: only when you start it. HTTP: off |
| Messages to vendors (delivery reminders, requests for a price) | The message shown in the request: your order number, item, quantities, dates, your signature | The vendor, by e-mail through **your ERPNext's** outgoing mail account, or by WhatsApp (Meta), to the address on the supplier's contact in ERPNext | Only when a person presses Send reminder / Send requests. The address is not stored (a keyed hash only, to recognise a reply; it is erased when the reply window ends, 30 days by default, or sooner when the order is received) and never goes to a language model |

Telegram and WhatsApp are third-party services with their own privacy policies; a WhatsApp or Telegram message about a purchase leaves your control once sent. If that matters, use the console and sign-in only (leave the channels unconfigured).

## What the AI agents send to the model

Exactly what the tools in `core/toolkit.py` return to the agent, plus the text a person typed.

**Sent**
- Item codes, names, units and groups of the items an agent looks at.
- Stock quantities, quantities on order and in transit, daily use, weekly quantities delivered to customers (totals per week, with no customer named), and the stock rules (cover days, lead times).
- For the item being discussed: the vendors' names, prices, taxes and duties, delivery times and on-time record.
- Purchase order and requisition numbers with their vendor, quantities, due dates and status.
- The text of a question a person typed, and the item and quantity of an order request being reviewed.

**Never sent**
- Customer names, addresses, phone numbers or email addresses, and what any customer paid.
- Names, email addresses, phone numbers, Telegram or WhatsApp ids of your staff and approvers (the asker's id is kept in this server's record only; the model is not told who is asking).
- Bank details, payment or invoice details, tax or company registration numbers.
- Passwords, ERPNext API keys, session tokens or this application's access keys.
- Anything the tools do not return. The model cannot query ERPNext directly; it can only call the tools.

**Kept on this server**
- Each task: what was asked, the result, which model answered, how long it took, how many tokens.
- Each tool call: which tool, with which arguments, for whom, and whether it worked.
- The exact messages sent to the model and its replies (the *trace*). These are deleted after `agents.keep_traces_days` (30 by default); the result and the counts stay. Only the administrator can read a trace.

**Where it goes**
- With the default settings, to a model running on the same machine (Ollama). Nothing crosses the network.
- If you choose a hosted provider (Settings > AI agents, or `agents.provider`: `anthropic`, `openai` or `gemini`), or an Ollama on another machine, the "Sent" list above leaves your server to that service. Choosing it needs your explicit consent (`allow_remote`: saving is refused without it, and the application checks again every time an agent runs), the console says "Your data leaves this computer" in plain words, and every task records the provider and `remote`. Switching back to this computer stops all of it at once.
- **Which terms apply.** A hosted provider receives the data under its own terms and privacy policy (Anthropic, OpenAI, Google). Choose one whose terms fit your own obligations (for example no training on your data, a data-processing agreement, where it is processed) and tell the vendors and staff whose names appear in the list. This application does not choose or negotiate that for you.
- **API keys** are read from the environment (the `.env` file) only. They cannot be typed into the console or stored in the database, are sent only as the request header the provider requires, and never appear in the console, the task record, a log or an error message.
- **Tokens.** Hosted providers bill per token. The application counts the tokens each reply reports and shows them per answer and per month, optionally against your own prices and a monthly limit (the "Tokens used" card, `erp-agents agents usage`). The counts are kept with each task, on your server.

## What the model cannot do

It cannot order, approve, pay or change a setting: no tool does that. `prepare_order_request` sends the usual approval request to the people who approve, and only when a person asked for an order in so many words; the reason shown to approvers is the person's own sentence, not the model's. Every number the model writes is checked against the tools' results before anyone sees it; an answer that fails is replaced by the plain-rules result.

## Your controls

- `agents.enabled` (off by default), `agents.provider` (`ollama`: this computer), `agents.allow_remote` (off), `agents.monthly_token_limit` (0 = none), `agents.keep_traces_days`, `agents.send_brief` (off), `agents.second_opinion` (off), `agents.daily_brief_cron` (empty).
- `mcp.http` (off) and `mcp.stdio_role` (`viewer`: look only).
- `erp-agents agents privacy` prints the live summary for your settings; `erp-agents agents tasks` lists every task with the model used.

---

# Draft privacy policy for the Frappe Marketplace listing

> **Draft for the developer to complete and have reviewed.** Replace the bracketed parts. It describes the application as built; it is not legal advice and does not replace advice about the law that applies to you and your users (for example GDPR or India's DPDP Act).

**Stock Assistant for ERPNext: Privacy Policy**
Last updated: [date]

**Who we are.** [Developer or company name], [address], [contact email]. We publish the app. Each business that installs it runs it on its own site and is the controller of the data in it. We do not run a service that receives your data.

**What the app does with data.** The app reads stock, sales-quantity, purchasing and supplier information from your ERPNext site to monitor stock levels, compare vendors and prepare purchase-order requests for a person to approve. It stores its own settings, approval requests and a log of who answered them on your site.

**What we collect.** Nothing. The app contains no analytics, telemetry or crash reporting, and has no connection to servers operated by us.

**What leaves your site, and only if you turn it on.**
1. *Approval messages.* If you configure Telegram or WhatsApp, request messages (item, quantity, vendor, price, reasons) and replies are sent to those services and to the approvers you list. Their privacy policies apply.
2. *AI language model (optional, off by default).* If you enable the AI agents, item and stock information, vendor names and prices, purchase-order references and the text of questions people type are sent to the language model you choose. By default that is a model on the same machine as your site. If you choose a hosted provider (for example Anthropic, OpenAI or Google) or a server on another machine, this information is sent to that provider under its terms, and the provider bills per token. The app never sends customer details, staff names or contact details, bank or tax details, or credentials to a model.
3. *MCP clients (optional, off by default).* If you connect an MCP client such as a desktop AI assistant, the information listed in (2) is returned to that client for the questions it asks.

**Personal data in the app.** The names or sign-in ids of the staff who approve or ask are stored on your site to record who did what. Supplier names that are individuals are personal data and are used in vendor comparisons. Customer details are not read for any of the above.

**Retention.** Records stay on your site until you delete them. Records of what was sent to a language model are deleted after the number of days you set (30 by default).

**Your choices.** Every feature that sends data off your site is off until you enable it, and can be switched off in Settings. You can read exactly what was sent to a model in the app's task record.

**Security.** Access uses ERPNext sign-in and its roles. Secrets are read from the environment, never stored in settings. Security reports: [security contact or link to SECURITY.md].

**Changes.** We will note changes to this policy in the app's release notes.

**Contact.** [email]

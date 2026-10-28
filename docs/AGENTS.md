# AI agents and the MCP server

The Stock Assistant has always worked from rules: the same data gives the same answer, every number comes from ERPNext, and a person presses **Create order**. The AI agents sit **on top** of those rules. They read the same data through the same tools, write a few plain sentences, and add the things a table cannot say. They are optional, off by default, and replaceable by the rules at any moment.

```
 people ── console / Telegram / WhatsApp ──┐
                                           ▼
   ┌─────────────── agents (optional) ───────────────┐        ┌── any MCP client ──┐
   │  inventory agent      purchasing agent          │        │ Claude Desktop,    │
   │  (morning brief)      (second opinion, answers) │        │ other AI apps      │
   │        ▲  language model: writes, never decides │        └─────────┬──────────┘
   │        │  checks: numbers must come from tools  │                  │ MCP (stdio or HTTP)
   └────────┼────────────────────────────────────────┘                  │
            │  MCP (in-process)                                         │
            ▼                                                           ▼
   ┌───────────────────────  MCP server (core/mcp_server.py)  ───────────────────────┐
   │  the stock pack's tools (tools.py): look up stock, usage, vendors, orders,      │
   │  late deliveries, receipts, forecasts; some prepare a REQUEST for a person to   │
   │  answer (an order, a price request, a stock transfer); no tool writes to ERPNext│
   └──────────────────────────────────┬──────────────────────────────────────────────┘
                                      ▼
        stock analysis · vendor ranking · approvals · ERPNext (the existing code, unchanged)
```

## The rules that do not bend

1. **No tool orders, approves, pays, moves stock or changes a setting.** The only actions are `prepare_order_request`, `prepare_quote_request` and `prepare_stock_transfer`, which send the usual approval request. A person still answers it, no vendor is messaged until a person presses Send, and a transfer only becomes a draft Stock Entry when a person presses Create transfer (no stock moves until it is submitted in ERPNext). `prepare_stock_transfer` is offered to MCP clients, not to the built-in agents. The quote tool is offered to an agent only when the person asked for prices or quotes in so many words, for an item they named, once per question (the same gate as the order tool).
2. **Numbers come from tools, never from the model.** Every number and item code in an agent's words is checked against what the tools returned that task, plus the numbers in the person's own question. A faithful rounding (12 for 12.3) passes; an invented figure, or a guessing word ("probably"), does not. Only the *results of calls that succeeded* count: a tool call's arguments never do, so a number a model types into a call that was rejected cannot be quoted afterwards. This is safe for the successful calls because the tools echo every argument that is a fact in their result (`usage_history` returns `weeks`, `vendor_options` and `prepare_order_request` return `qty`, `stock_overview` reports its `limit` as `listed`); a test pins each of those, so a tool that stopped echoing would be noticed.
3. **A rejected answer never reaches a person.** The model gets one chance to fix exactly what was wrong; after that the plain-rules result is used, and the record says so.
4. **The order tool is fenced twice.** It is offered only to someone who may approve orders in ERPNext *and* only when they asked for an order in so many words ("create a purchase order for BOLT-M8 for 500"); and it may be used only for the item and quantity they named, once per question. The reason approvers read is the person's own sentence, never the model's.
5. **Text from ERPNext is data.** Item and vendor names are given to the model labelled as data; the model's tools cannot do anything a prompt injection would want.
6. **Privacy by construction.** The tools return no customer or staff personal data ([PRIVACY.md](PRIVACY.md)); the model is not told who is asking; a model on another machine is refused unless the owner allows it.

## The tools

The registry holds <!-- tools:counts -->17 tools: 14 read-only and 3 prepare-only<!-- /tools:counts -->. All of them are defined in the stock pack ([`src/erp_agents/packs/stock_procurement/tools.py`](../src/erp_agents/packs/stock_procurement/tools.py), declared in its [`manifest.yaml`](../src/erp_agents/packs/stock_procurement/manifest.yaml)) and registered with the core's tool registry ([`core/toolkit.py`](../src/erp_agents/core/toolkit.py)), which checks the caller's role on every call. They are plain Python with no web, HTTP or SQL imports, so they move into a Frappe app unchanged. The MCP server publishes exactly these, with the same names, descriptions and argument descriptions.

| Tool | Who may use it | Returns |
|---|---|---|
| `stock_overview(group?, limit?)` | anyone who can look | Items needing attention, worst first, with the count in each state |
| `stock_status(item_code)` | look | On hand (in total and warehouse by warehouse), available, on order, in transit, daily use, trend, lead time, days of cover, suggested quantity and why |
| `usage_history(item_code, weeks?)` | look | Weekly quantities delivered to customers (totals; no customer is named) |
| `vendor_options(item_code, qty)` | look | Vendors compared for that quantity: price, duties and taxes, delivery, on-time record, score |
| `open_orders(item_code?, kind?)` | look | Open or draft purchase orders / requisitions with ordered, received and still-to-come quantity |
| `waiting_requests(item_code?)` | look | Requests waiting for a person's answer |
| `prepare_order_request(item_code, reason, qty?)` | approvers | Sends the usual request (Create order / Change quantity / Change vendor). Orders nothing |
| `late_deliveries()` | look | Open purchase orders past due, with days of stock cover without the delivery and with it, and the reminders sent |
| `receipt_mismatches(days?)` | look | Goods receipts that do not match their order (quantity, rejected goods, price on receipt or invoice) and whether the invoice is paid |
| `vendor_scorecard(days?)` | look | Vendors ranked on on-time delivery, price drift and quality, with why a vendor ranks lower |
| `demand_forecast(item_code, weeks?)` | look | The order point from last year's season and open sales orders, and expected use week by week |
| `slow_stock(limit?)` | look | Dead and slow stock, the cash tied up and a suggested action |
| `substitutes(item_code)` | look | The approved alternatives listed in ERPNext and their stock |
| `stock_counts(kind?)` | look | This week's count list, or recent count differences with what explains them |
| `decision_history(query?, item_code?, days?, limit?)` | look | Past decisions and why: what each request said, which button ended it and what came of it, and settings changes. Never who answered: "a person" |
| `prepare_quote_request(item_code, reason, qty?)` | approvers | Prepares a request to ask two or three vendors for a price. Messages no vendor: a person presses Send requests |
| `prepare_stock_transfer(item_code, qty, from_warehouse, to_warehouse, reason)` | approvers | Prepares a request to move stock from one warehouse to another, checked against what the source holds. Creates nothing: a person presses Create transfer, which makes a draft Stock Entry |

Every call is checked against the caller's ERPNext-derived level, and logged (who, how, which tool, which arguments, whether it worked) in `tool_calls`. Results are capped at 7,000 characters. Numbers sent as text by a small model (`"500"`, `"1,200"`) are accepted; a wrong value is an error the model can read and correct.

## What is written to ERPNext

No tool writes to ERPNext. The three that prepare a request only put it in front of a person; what a button then creates is below. Everything else the assistant does in ERPNext is reading.

<!-- erp-writes -->
| Document | What it writes | Through which tool and button |
|---|---|---|
| `Purchase Order` | A **draft**, with the approver recorded. Afterwards, only on an order it created itself (found by its reference): change the quantity, delete the draft, or cancel the submitted order | `prepare_order_request`, then **Create order**; later **Change quantity** and **Cancel order** (or the same words typed in a chat) |
| `Material Request` | A **draft** purchase requisition, for the items and groups you chose to requisition first, or when a person picks "Create a requisition instead" | `prepare_order_request`, then **Create a requisition instead** |
| `Stock Entry` | A **draft** of type Material Transfer. No stock moves until someone submits it in ERPNext | `prepare_stock_transfer`, then **Create transfer** |
| `Communication` | An e-mail to a supplier through ERPNext's own outgoing mail (recorded on the document there), only after a person presses Send | The late-delivery reminder and `prepare_quote_request`, then **Send reminder** / **Send requests** |
| `Custom Field` | Two fields, a reference and the approver's name, on Purchase Order, Material Request and Stock Entry, once | `erp-agents erp-setup`, an administrator's command (not a tool) |
<!-- /erp-writes -->

Never written: a submitted document of any kind (a person submits in ERPNext), Supplier Quotations, Purchase Receipts, Purchase Invoices, payments, items, suppliers, stock counts or ERPNext settings. The assistant's own settings and records live in its own database. [tests/test_docs_match_code.py](../tests/test_docs_match_code.py) compares this table with the pack manifest and the ERPNext client, so a new kind of write cannot ship without being listed here.

## The MCP server

Install the extra: `pip install "erpnext-ai-agents[agents]"`.

**Over stdio** (Claude Desktop and most desktop clients start it as a program):

```json
{
  "mcpServers": {
    "stock-assistant": {
      "command": "python",
      "args": ["-m", "erp_agents.cli", "--config", "/path/to/config/settings.erpnext.yaml", "mcp"]
    }
  }
}
```

Who the client is: `mcp.stdio_role` in the settings file, `viewer` (look only, the default) or `approver` (may also prepare an order request). stdout belongs to the protocol; the server logs to stderr.

**Over HTTP** (private by design, see below): `mcp.http: true` serves Streamable HTTP at `/mcp` on the console's port. **It answers the server machine only unless you say otherwise**: a request from another address, or one relayed by a tunnel or proxy, gets 404 as if the endpoint did not exist. Callers send `Authorization: Bearer <token>` where the token is what the REST API takes (an ERPNext sign-in session, or an access key), checked by exactly the REST API's rules: access keys work only at the server machine unless `auth.access_keys` says otherwise, a weak key never works through a tunnel, and `server.remote_access: webhooks_only` closes `/mcp` to the outside like the rest of the API. The endpoint is stateless: each request carries its own caller.

Reaching it from another machine takes two explicit settings: `mcp.remote: true` and `mcp.allowed_hosts`, the names it is reached under written as the Host header carries them (`stock.example.com`, with the port when it is not 80 or 443). With `remote` and no names the application refuses to start, and start-up warns while `remote` is on. Whatever the settings, **the Host and Origin of every request are checked, even when the token is valid** (a page that tricks a browser into calling a local server has no token, but cannot hide its Host and Origin): the loopback names (`127.0.0.1`, `localhost`, `[::1]`, any port) are always accepted; any other Host must be listed; an Origin must be a loopback page, `https://<a listed host>`, or listed under `mcp.allowed_origins` (`https://app.example.com`, no path). A wildcard in either list is refused at start-up because it would switch the protection off. The MCP SDK itself disables the check when it is handed no settings, so the application always hands it explicit ones (a test pins this).

**What this endpoint is not: MCP OAuth.** The MCP specification (draft authorization page, read 25 Oct 2026; authorization is *optional* there) says an HTTP server that offers authorization must publish OAuth 2.0 Protected Resource Metadata (RFC 9728), point to an authorization server, and accept only access tokens issued for it as their audience (RFC 8707); a stdio server should *not* use it and takes credentials from the environment. This endpoint does none of that: it accepts the application's own static bearer tokens, publishes no metadata and has no authorization server behind it. Clients that discover a protected server by themselves (a connector directory, "add a remote MCP server by URL") therefore cannot use it, and it must not be listed or described as interoperable with them. Use **stdio** (`erp-agents mcp`) for a desktop client, or this endpoint inside a private network, a VPN or behind a proxy that authenticates callers itself.

*What real support would take* (not built, because none of it can be tested here without a live identity provider): an identity provider that issues access tokens bound to this server's URL (ERPNext's own OAuth 2 provider is the natural choice, and the Frappe app would use it); a token verifier that checks signature or introspection, issuer, expiry, audience and scopes, and maps the ERPNext user to the roles the application already uses; serving `/.well-known/oauth-protected-resource` and answering 401 with `WWW-Authenticate: Bearer resource_metadata="..."`. The installed SDK (`mcp` 2.3) has the pieces (`AuthSettings`, `TokenVerifier`, the metadata routes); none is switched on, and the tests assert that nothing here pretends otherwise.

## The two agents

### Inventory agent: the morning brief

`erp-agents agents brief`, the **Settings > AI agents** screen, or on a schedule (`agents.daily_brief_cron`, optionally sent to the approvers with `send_brief`).

- **The rules write the table:** which items, what to do (order now / order soon / keep an eye on it), and why. Instantly, the same way every time. An item below your rules can never be left out.
- **The rules also write the "Worth a look" points**, with no model: a waiting request that covers more than 120 days of the item's use, several requests that could each lead to an order for one item (a stock alert waiting beside its reorder suggestion is one story, not two), and an order past its due date with quantity still to come.
- **The model writes the top:** a one-line headline and up to two extra observations, about items the table and the program's points do not already cover. It reads a short digest of the overview, the waiting requests and the open orders, in **one** model call: the program fetches the three results itself.
- If the model is off, unreachable or rejected, the brief goes out with the rules' headline and the program's points, marked "Written by rules". On the author's data the model's extras added nothing the table did not already say.

### Purchasing agent: second opinion and questions

- **Second opinion** (`erp-agents agents review <request id>`, or `agents.second_opinion` to do it for every new request and send it to the same people). The **verdict is decided by the program**: is this the best-ranked vendor, does it arrive before the stock runs out, is the quantity near what the analysis suggests (not under 60% or over 200%), how many days of use the quantity is, and how much is already on order. The model adds up to three observations, checked like everything else (its words may quote the program's checks but may not calculate or compare on their own); if they fail, the program's checks go out alone. The notes the model adds are labelled as the AI's: their numbers are checked, their wording is not.
- **Questions.** In the Ask page, on Telegram and on WhatsApp, a question the rules cannot place (and that looks like a question) goes to an agent when agents are on. Stock questions go to the inventory agent's tools, anything about orders, vendors, prices or requests to the purchasing agent's. The answer is shown only if its numbers and codes come from the tools; otherwise: "I do not have enough information in the recorded data to answer that question." If a request was prepared along the way, you are told so even if the answer failed.

## How a task runs

1. The program makes the obvious tool calls itself (the *warm start*), so a slow model is not asked to find them.
2. If the job allows, the model may call more tools from its own short list (at most `agents.max_tool_calls`, within `agents.max_steps` replies).
3. The model writes its answer as JSON in a fixed format, with no tools.
4. The program checks the format, the item codes, the numbers, the guessing words and the job's own rules (for the brief: an item below your rules cannot be marked "keep an eye on it" when nothing is on order).
5. One repair attempt with the exact reasons, then the plain-rules result.

One task runs at a time (a laptop has one model). Every task is recorded in `agent_tasks` with the model, time per step, tokens, and the exact messages (kept `agents.keep_traces_days`, readable by the administrator only). `erp-agents agents tasks` lists them.

## Choosing where the model runs

**Settings > AI agents > "Where does the AI model run?"** (or `agents.provider` in the settings file). It is a choice you can change at any time, without restarting, to suit what matters to you: cost, speed, or keeping your data here.

| Choice | `provider` | Needs | Cost | What the agents read goes... |
|---|---|---|---|---|
| This computer (Ollama) | `ollama` (default) | Ollama running with a model that supports tool calling | free | nowhere: it stays on this computer |
| Claude (Anthropic) | `anthropic` | `ANTHROPIC_API_KEY` | per token | to Anthropic |
| OpenAI | `openai` | `OPENAI_API_KEY` | per token | to OpenAI |
| Gemini (Google) | `gemini` | `GEMINI_API_KEY` | per token | to Google |

`provider: openai` with a `base_url` also works for any service that speaks the OpenAI protocol (a vLLM or LM Studio on your own machine, or a hosted gateway). One on this computer is free and needs no key and no consent; one elsewhere is treated like any hosted provider.

**To switch to a hosted model:**
1. Put that provider's key in the `.env` file (the line is in `.env.example`) and restart the Stock Assistant once so it is read. The key is **never typed into the console, never stored in the database and never shown**; the screen only says whether it was found. A key already set in Windows or your shell for another tool is found too (and wins over the `.env` file); to give this application a key of its own, so its use is kept apart on the provider's bill, name it `ERP_AGENTS_ANTHROPIC_API_KEY`, `ERP_AGENTS_OPENAI_API_KEY` or `ERP_AGENTS_GEMINI_API_KEY`: those are looked at first, and the screen says which variable the key was found in.
2. Pick the provider. **Check connection** asks the provider which models your key can use (it spends no tokens and sends nothing from ERPNext), and each model is a button.
3. Tick the box that says what the agents read is sent to that provider. Saving is refused without it, and the application checks it again every time an agent runs.
4. Optionally, under **Cost control**, enter your provider's prices per million input and output tokens (to see an estimated cost) and a **monthly token limit**. At the limit the agents stop using the hosted model and the plain rules answer, until next month or until you raise it. A task that starts under the limit may finish over it.

If a hosted provider is chosen but its key is missing, rejected or out of quota, nothing breaks: the brief, the second opinion and the answers fall back to the rules, and the note says what to fix.

### Tokens: what is used and where you see it

A token is about three-quarters of a word. A hosted provider bills the **input** (everything the model reads, each time it replies) and the **output** (what it writes) separately; the application counts both from what each reply reports. They are shown:
- under the brief and under each Ask answer that a hosted model wrote ("Used 940 tokens (900 in, 40 out) on <model>", with an estimate when you entered prices);
- in **Tokens used** on the same screen: this month against your limit, and the last 30 days by model, with models on this computer listed apart (free);
- by `erp-agents agents usage`, `agents tasks` and `GET /api/v1/agents/usage`, and at the end of an `agents_eval` run.

Counted from the measured runs, a brief is about 1,000 input and 100 output tokens, a question 1,300 to 3,000 input and 60 to 140 output, and a second opinion 2,000 to 3,100 input and 100 to 200 output. A busy small business (a brief, ten questions and five reviews a day) would use very roughly a million tokens a month; your own figures are on the screen after a day. The estimate is only shown when you entered prices, because prices differ by model and change.

### What was verified, and what was not

Each client is written to its provider's documented API (the Messages API for Claude, Chat Completions for OpenAI, `generateContent` for Gemini) and tested without a network against recorded request and reply shapes: the messages, the tools, the forced answer shape, the token counts, retries on rate limits, and that the key never appears in an error. The OpenAI client was also run **live** against a real server that speaks that protocol (Ollama's `/v1` endpoint): tools, JSON-schema answers and token counts all worked. **I could not call Claude, OpenAI or Gemini themselves, because that needs your keys.** Before relying on one, press Check connection and run `python -m tools.e2e.agents_eval --provider anthropic` (or `openai`, `gemini`) once: it asks the standard questions, compares the answers with ERPNext's own figures, and reports the time and the tokens it used. Model names are suggestions that may have changed since this was written; the check lists the real ones.

Provider details that the clients take care of: Claude is sent no `temperature` (current models reject it) and is made to answer in the requested shape by a forced tool; OpenAI reasoning models get no temperature and room to think within the reply limit; Gemini is given a JSON-only reply and its function-calling signatures are handed back unchanged; a busy provider (HTTP 429 or 5xx) is retried twice.

## Turning it on

```bash
pip install "erpnext-ai-agents[agents]"
ollama pull llama3.1                       # any model with the "tools" capability
erp-agents --config config/settings.erpnext.yaml agents status
erp-agents --config config/settings.erpnext.yaml agents brief --rules      # the plain brief, no model
```

Then **Settings > AI agents** > "Let the AI agents help" (or `agents.enabled: true` in the settings file). Everything is also available over the REST API:

| Route | Who | What |
|---|---|---|
| `GET /api/v1/agents/status` | look | State, provider, model, whether it leaves this server, whether each provider's key was found (`?check=true` also asks the provider) |
| `POST /api/v1/agents/check` | settings | Does this provider / address / model work? Lists the models the key can use; works on a choice that is not saved yet |
| `GET /api/v1/agents/usage` | look | Tokens and tasks this month (against the limit) and by model and day |
| `GET /api/v1/agents/privacy` | look | What is sent, kept, and where |
| `GET /api/v1/agents/brief` · `POST` | look · approve | The latest brief · start a new one (polled) |
| `POST /api/v1/agents/ask` | look | Start a question (polled) |
| `GET/POST /api/v1/agents/reviews/{request id}` | approve | The second opinion on an order request |
| `GET /api/v1/agents/tasks[/{id}]` | look (own questions) · admin (`?trace=true`) | What ran, and what was sent |

## Checked on every change: the AI evaluation

100 golden questions run in CI on every pull request, with no model and no ERPNext: a scripted stand-in plays the model, including bad replies (an invented figure, a guess, malformed JSON), and the suite requires that each is caught, that nothing is written to ERPNext, and that no request is prepared unless the person asked. The limits are in `tests/evals/thresholds.json`. The same questions that a real model can answer can be run on it, and on a second model beside it, with `python -m tools.e2e.agents_eval --suite golden --against ...`. How it works, how to add a question, and what it found: [EVALUATION.md](EVALUATION.md).

## Measured on the author's laptop (24 Oct 2026)

Hardware: Intel Core i3-1215U (a 15-watt laptop chip: 2 fast + 4 efficient cores), 24 GB RAM, no usable GPU. Software: Ollama 0.40.6, llama3.1 8B (Q4_K_M, the `llama3.1:latest` download), context 8,192, against the live ERPNext (202 items, 32 below their rules). Everything read-only. Reproduce or compare another model with `python -m tools.e2e.agents_eval --model <name>`.

**How the time is spent.** A reply costs *(prompt tokens ÷ reading speed) + (written tokens ÷ writing speed)*. On this machine reading ran at **3 to 12 tokens/second** (it varied fourfold between runs, probably the chip's power management) and writing at **1.7 to 2.8 tokens/second**. So the length of what the model has to read is the lever: the same brief took 597 s, 478 s and 145 s as its prompt shrank from 4,006 to 973 tokens and the replies from three to one.

| The morning brief | Model replies | Prompt tokens | Written tokens | Time |
|---|---|---|---|---|
| First design: the model chose the actions and wrote every reason, one retry | 3 | 4,006 | 836 | 597 s |
| One reply, raw tool results | 1 | 2,853 | 156 | 478 s (371 s reading, 92 s writing) |
| **One reply, short digest (what ships)** | 1 | 973 | 126 | **145 s** (98 s reading, 46 s writing) |

| A question or second opinion (first run) | Total | Reading | Writing | Replies | Tokens in / out | Checks | Right? |
|---|---|---|---|---|---|---|---|
| How much ELE-0002 do we have right now? | **152 s** | 130 s | 21 s | 3 | 1,543 / 60 | accepted | yes |
| How many days of stock do we have left of FAS-0003? | **569 s** | 551 s | 17 s | 3 | 1,680 / 53 | accepted | **no: the answer was `:{`** |
| Which vendor is the best choice for FAS-0025 for 500? | **299 s** | 272 s | 24 s | 3 | 2,912 / 70 | accepted | yes |
| Are any of our purchase orders late? | **174 s** | 124 s | 50 s | 3 | 2,204 / 143 | accepted | **no: said "yes" for an order due tomorrow** |
| Second opinion on an order of 3,840 | **264 s** | 190 s | 72 s | 3 | 3,114 / 213 | accepted | **no: "3840 is 110 times the daily use of 110"** |

Median 264 s, fastest 152 s, slowest 569 s in that first run.

**Final run, with the fixes below in place** (same questions; Ollama had by then read similar prompts, so these are flattered): brief 135 s; ELE-0002 on hand 77 s; days left of FAS-0003 64 s; best vendor 103 s; late orders 36 s; second opinion 76 s. All six accepted, and the five with an answer to compare with were right.

**Cold run on new items** (model unloaded first, so the first question also pays for loading it; four questions the model had not seen, through the real `erp-agents agents ask`):

| Question | Total | Model replies | Reading | Writing | Answer |
|---|---|---|---|---|---|
| How much MET-0026 do we have right now? | **181 s** | 3 | 150 s (+12 s loading) | 15 s | `><0` (right figure, stray symbols: now rejected) |
| How many days of stock do we have left of FGC-0019? | **117 s** | 4 | 93 s | 20 s | `0` (right) |
| Which vendor is the best choice for MEC-0003 for 500? | **156 s** | 3 | 132 s | 21 s | the right vendor |
| Are there any purchase orders still to come for FAS-0024? | **55 s** | 3 | 32 s | 17 s | `0` (right) |

**What to expect on this laptop: a new question takes 1 to 3 minutes (55 to 181 s measured cold, 569 s at the worst before the fixes), a morning brief about 2½ minutes.** The model reads about 90% of that time. Ollama keeps what it has already read: when an identical prompt was run again minutes later, reading took 0.3 to 0.5 s and the task 31 to 78 s. A *new* question does not get that, since only the shared start of the prompt (the instructions) is reused. Judge speed by the cold numbers, not by a repeat.

**What the first run taught, and what was changed** (each has a regression test):
1. A non-answer (`:{`) passed because it fitted the format, and later `><0` (right figure, stray symbols) passed as a "terse answer". Now an answer is a real sentence, or a clean figure or code that a tool returned ("1.1 days").
2. The model compared two dates wrongly. Now `open_orders` marks late orders itself (`late`, `days_late`, `late_count`): anything that needs arithmetic or a comparison is computed by the tool, not asked of the model.
3. A sentence with real numbers and a false claim passed the number check. Now words that claim a calculation or comparison of the model's own (times, twice, half, more than, rose, fell...) are rejected unless a tool's result uses the word, and the program writes that fact itself ("3,840 Nos is about 35 days of use at 110 a day").
4. A reply that never ended held a task for 900 s. Now a reply is capped (`agents.max_output_tokens`, 400).

**What this means.** On this laptop an 8-billion-parameter model is fine for a scheduled brief and for a background question, and not for a conversation: nobody should wait minutes for an answer, and the console and chat already show a "working" state and fall back to the rules. It also made mistakes on three of five simple tasks that a stronger model would be expected to avoid; the checks stopped them from reaching a person only after they were added, which is the point of measuring. A hosted model would shorten every number in the tables above to seconds, at the price in [PRIVACY.md](PRIVACY.md): the listed data leaves your server. That trade, and a smaller local model as the middle road, are for the owner to weigh with these measurements and `agents_eval` on the candidate.

## Limits to know

- **Speed.** See the measurements above. A language model on a CPU is slow; the design keeps it to one or two replies per task and never puts it on the path of anything urgent: alerts, requests and approvals do not wait for it.
- **Small models make small mistakes.** The checks catch invented numbers, wrong codes, guessing words and a few semantic rules. They cannot tell that a *true* sentence is unhelpful. That is why the table and the verdict are the program's, and the model adds only commentary.
- **The model may be wrong about what is notable.** A watch-out is a prompt to look, not a finding. It quotes the numbers so you can check it.
- **WhatsApp** delivers plain messages (the brief, second opinions) only while the 24-hour window is open; Telegram has no such limit.

## For the Frappe Marketplace version

The agents are written to move: `core/toolkit.py`, the stock pack's `tools.py` and the agent modules have no web, scheduler or SQL imports; the language-model client is the only network code; settings are a validated section (a Frappe Single DocType maps onto it); the privacy statement is generated from code and tested ([PRIVACY.md](PRIVACY.md)). What changes in the Frappe app: the MCP endpoint becomes a whitelisted method with Frappe's OAuth provider, tasks and tool calls become DocTypes, the scheduler job becomes a Scheduled Job Type.

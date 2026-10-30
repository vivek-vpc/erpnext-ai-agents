# Known limitations

What this project does not do, or does only partly, as of 28 Oct 2026. Each point says what it means for you. The longer list of smaller limits (vendor prices, usage history, ERPNext version) is in [README section 9](../README.md#9-known-limitations--next-steps); security properties are in [SECURITY.md](../SECURITY.md).

## By design

- **One instance only.** The database is SQLite and the scheduler runs inside the process. `serve` takes a lock on the database and a second copy refuses to start. There is no failover: if the process stops, nothing is watched until it starts again (`doctor` and the "stock monitor is down" notice tell you).
- **SQLite, not a server database.** One file, right for one small business. Anyone who can read the file can read everything in it, and anyone who can write to it can change it. The seal over the decision history *shows* a change; it cannot prevent one ([SECURITY.md](../SECURITY.md)). Keep backups (`erp-agents backup`) somewhere else.
- **A person answers every request.** No order, transfer or message to a vendor happens by itself. The cost of that is speed: nothing moves while nobody answers, and requests expire.

## Trust and access

- **Chat approvers are trusted by their configured address.** A WhatsApp number or Telegram chat listed in `approvals.recipients` can approve with the roles written next to it. That is not checked against ERPNext when they answer; the draft is created by the service account and records the approver's name. Whoever takes over that phone number or chat can approve.
- **The MCP endpoint over HTTP uses a static bearer token, not OAuth.** It is off by default and answers the server machine only unless you open it. AI products that discover servers by themselves cannot use it; stdio (`erp-agents mcp`) is unaffected.
- **The ERPNext account must be limited by you.** The application tells you which roles are surplus (`erp-agents erp-check-access`); it cannot remove them. On the author's test instance the account still holds more than it needs (case AUTH-02 in [E2E_FINDINGS.md](E2E_FINDINGS.md)).

## What has been tested, and how

- **WhatsApp and Telegram are tested against fake transports.** Messages have been delivered live on the author's own accounts. Nobody else has tried WhatsApp against Meta's sandbox: expect to adjust the message template and buttons.
- **The live suite last ran on 22 Oct 2026** against an ERPNext v16 test instance, and later features were checked live one by one. The stock transfer was created, submitted and reversed live as the service account; the same step with an approver's own ERPNext session (`require_personal_login`) was not run live.
- **ERPNext v16 only.** Other versions have not been tried.
- **Text copied from the data is data, and the checks do not judge claims in words.** A figure or an item code that appears inside an item name, a vendor name or a note counts as recorded, so a model that repeats it is not rejected for it; and a sentence with no figure in it ("the owner approved this") is not checked against anything. What bounds the damage is not the checks but the code behind the model: it can only read, or prepare a request that a person must answer, and an order request is for the item and quantity the person typed. Names are marked, cleaned and cut before a model reads them (`core/prompting.py`), which lowers the chance that a small model is steered; it does not prove it cannot be. One related gap: when the person types an item's *name* rather than its code, the order gate accepts an item whose name is the beginning of what they typed ("Bolt M8" for "Bolt M8 extra long"); codes are matched as a whole, names are not. The request still goes to a person, who sees the item and the quantity before answering.
- **The AI evaluation tests the checks, not the model.** 122 golden questions run in CI with a scripted stand-in for the model ([EVALUATION.md](EVALUATION.md)), so they prove that a wrong reply is caught and nothing is written; they do not say how often a real model errs. The scripted replies are written by hand, the demo world is small (ten items, three vendors each), and the live comparison was run once on real models (Ollama, llama3.1 and llama3, 28 Oct 2026); it showed that llama3.1 got 5 of 8 cases right, that llama3 cannot call tools, and that the checks accept a wrong superlative ("the cheapest vendor" when another is cheaper). A hosted model has not been tried.

## When something fails

- **The circuit breakers and the rate limits are in the memory of the one running process.** They start again after a restart (the breaker learns in a few requests; a person gets at most one extra minute of questions). The ids of delivered messages and the recorded answers are in the database.
- **An answer the program stopped before writing down is finished at the next start only for orders, requisitions and stock transfers**, not for a change to an existing order, and it needs the application's own fields in ERPNext (`erp-agents erp-setup`). While ERPNext is unreachable it cannot be checked, and the request stays waiting.
- **A repeated message is recognised only if its channel gave it an id**, and for `inbound_memory_days` (7).
- **Nothing queues work for later while a service is down.** A question asked while the model is down gets the plain answer; it is not retried by itself when the model comes back. Nobody is messaged that ERPNext is down except by the existing "stock monitor is down" notice after repeated failed runs; the circuit state is in the log, the metrics and the signed-in `/health` ([RESILIENCE.md](RESILIENCE.md)).

## Watching it run

- **Metrics and traces are for one process and are not kept.** The counters of events (model calls, tool calls, ERPNext requests, fallbacks, job runs) start from zero when the application restarts; the figures read from the database (requests waiting, decisions, tasks, tokens) do not. The application keeps no history: Prometheus does. Both are off until you switch them on ([OBSERVABILITY.md](OBSERVABILITY.md)).
- **The log scrubber is a backstop.** It hides the secrets the configuration holds and the shapes of common keys, and it hides any run of 10 to 15 digits, so a number that is not a phone number (a timestamp, an order number) is sometimes hidden too. A secret it was never told and that has no recognisable shape would not be.
- **No trace context reaches ERPNext or the model server**, so their side of a request cannot be joined to a trace. Spans are exported on a best-effort basis: if the receiver is down they are dropped.
- **`/ready` and `doctor` see the effect, not the cause.** A stuck scheduler shows as a maintenance job that stopped; why is in the log, under the job's name. The alert rules in the document are starting points, not tuned to your volume.

## Not built yet

- **It is not a Frappe app** and is not on the Frappe Marketplace. It is a standalone Python service (FastAPI and SQLite). The port would replace the edges and keep the logic: [FRAPPE_APP_PLAN.md](FRAPPE_APP_PLAN.md).
- **Sales and finance agents** are empty skeleton packs.
- **Per-warehouse monitoring.** Stock is shown warehouse by warehouse and can be moved between warehouses, but reorder decisions use one combined figure.

## Documentation

- The architecture picture (`docs/img/architecture.png`) is older than the code and shows 15 tools. The numbers in the text are checked against the code by a test; the picture is not, and will be redrawn.

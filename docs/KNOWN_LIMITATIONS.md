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
- **The AI evaluation tests the checks, not the model.** 100 golden questions run in CI with a scripted stand-in for the model ([EVALUATION.md](EVALUATION.md)), so they prove that a wrong reply is caught and nothing is written; they do not say how often a real model errs. The scripted replies are written by hand, the demo world is small (ten items, three vendors each), and the live comparison on Ollama or a hosted model has been tested with stand-ins only: the first real run needs your machine or your key.

## Not built yet

- **It is not a Frappe app** and is not on the Frappe Marketplace. It is a standalone Python service (FastAPI and SQLite). The port would replace the edges and keep the logic: [FRAPPE_APP_PLAN.md](FRAPPE_APP_PLAN.md).
- **Sales and finance agents** are empty skeleton packs.
- **Per-warehouse monitoring.** Stock is shown warehouse by warehouse and can be moved between warehouses, but reorder decisions use one combined figure.

## Documentation

- The architecture picture (`docs/img/architecture.png`) is older than the code and shows 15 tools. The numbers in the text are checked against the code by a test; the picture is not, and will be redrawn.

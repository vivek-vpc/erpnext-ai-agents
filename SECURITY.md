# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private reporting: on the repository page choose **Security → Report a vulnerability**. Describe what you found, how to reproduce it, and what an attacker could do with it. You will get an answer within a few days. A fix is prepared privately and released together with a note in the changelog; reporters are credited if they wish.

## What this project protects, and what it assumes

The Stock Assistant reads your ERP, asks people to approve purchases, and (once approved) creates **draft** documents in the ERP. It is designed so that:

- nothing is ordered or approved without a person pressing a button; free text typed into a chat can never approve anything;
- every API route refuses an unauthenticated caller, and the roles of ERPNext decide what a signed-in person may do;
- webhooks (WhatsApp, Telegram) are checked by signature or secret before anything is read;
- secrets live in the environment (`.env`), never in the repository or in logs (tokens are masked).

It assumes you run it behind your own network controls. Read [docs/PENTEST_REPORT.md](docs/PENTEST_REPORT.md) for what was tested, what was fixed, and the deployment checklist (service-account roles, which ports to expose, `server.remote_access`).

## Known properties and limits (read before you deploy)

The wider list of what the project does not do is [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

* **The decision history's seal shows changes, it does not prevent them.** `erp-agents history --verify` finds a request or settings change that was edited or deleted after it was sealed, and a break in the chain; someone with write access to the database file could rewrite the whole chain. Keep backups, and keep a copy of the latest hash elsewhere.

- **TLS and HSTS belong to your proxy.** The application speaks plain HTTP and cannot see TLS itself. It adds `Strict-Transport-Security: max-age=31536000` only to answers for requests that the proxy marks as https (`X-Forwarded-Proto: https`, or Cloudflare's `CF-Visitor`), so that header is only as good as the proxy's forwarding of those. Set HSTS at the proxy for every response, including the ones this application never produces, and never expose the application's own port to the internet without a TLS proxy in front. The other browser protections (CSP, framing, referrer, content type, no caching of API answers) are always on.
- **The MCP endpoint over HTTP is private by design and is not OAuth.** `mcp.http` is off by default. When on, it answers the server machine only (anyone else, or a request relayed by a tunnel or proxy, gets 404 as if it did not exist); `mcp.remote: true` together with `mcp.allowed_hosts` is the explicit opt-in for other machines, and start-up warns while it is set. Host and Origin of every request are checked even when the token is valid. It takes the application's own static bearer tokens: no OAuth, no protected-resource metadata (RFC 9728), no audience-bound tokens (RFC 8707), no per-client scopes. The MCP specification asks that of an HTTP server that other people's AI products discover by themselves, so this endpoint must not be listed or described as interoperable with them. Over stdio the specification says to take credentials from the environment, which `mcp.stdio_role` does. Details and what real support would take: [docs/AGENTS.md](docs/AGENTS.md).
- **Access keys work only on the server machine by default**, in the demo as well as with ERPNext sign-in. `auth.access_keys: anywhere` accepts them from any address that can reach the server; start-up warns while it is set.
- **Hosted language-model keys** (Claude, OpenAI, Gemini) are read from the environment, are sent only to the provider's own https host (never to a custom address, never after a redirect), and are never stored, logged or shown. "Check connection" ignores any address typed for a hosted provider and refuses to contact this machine or the private network on its behalf.
- **Outgoing requests that carry a credential do not follow redirects** (ERPNext, WhatsApp, Telegram, the hosted-model clients, the MCP client).
- **Text from outside** (item names, questions, tool arguments) is stripped of control and invisible characters and length-capped before it appears in a tool error or a log line.
- **What an AI answer may contain.** Numbers come from the person's question or from the results of tool calls that succeeded, and from nowhere else. The arguments of a tool call never count, failed or not: a rejected call returned nothing, so a number typed into it is the model's own.
- **Who may answer from a chat.** A WhatsApp or Telegram answer is trusted because it comes from the address written in the settings file (`approvals.recipients`) and carries the roles written there. It is **not** checked against ERPNext when it arrives: a person who leaves, or loses a role in ERPNext, keeps their authority in chat until they are removed from the file. Keep that list short, give chat recipients the approver role only (not admin), and review it when staff change. `erp-agents doctor` lists them.
- **Logs, metrics and traces carry no secret and no personal text.** Every log line, in either format and from any library, passes a scrubber that hides the keys, tokens and passwords the configuration holds, the shapes of common keys, the approvers' phone numbers and any run of 10 to 15 digits; it is a backstop and cannot know a secret it was never told. Metrics and traces hold names, outcomes and counts only (no item, vendor, person, phone number, question, answer or query string), and a failure is recorded as the class of the error, never its text. `GET /metrics` does not exist unless switched on, and then needs the scraper's own token (24 characters or more) or an administrator; `GET /ready` tells anybody only yes or no. Tracing is off by default and exports only to the endpoint you name. [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md)
- **One instance only.** `serve` takes a lock on the database and a second copy refuses to start (the lock is dropped by the operating system if the process dies). SQLite and the in-process scheduler do not support several replicas.
- **Backups and upgrades.** The database carries a schema version: a file made by a newer version is refused untouched, and an older one is copied aside and then upgraded by numbered migrations (the core is at version 2, whose one migration adds the table of scheduled-job records and is tested on a real version-1 file; the stock pack's tables are at version 4, whose migration adds columns to two existing tables and is tested on a real version-3 file). `erp-agents backup` makes a verified copy while the application runs, `backup-verify` checks one, `restore` puts one back (stop the application first; the replaced database is kept beside it). `.env` and the settings file are not in the database: copy them separately.

## Checking an installation

`erp-agents doctor` is a read-only check of this computer's files and settings: who can read `.env`, the settings file, the database and the data folder (Windows access lists or POSIX modes), whether the server is bound beyond this machine, whether a tunnel exposes the console and API when only the webhooks need to be public, whether access keys or MCP are open to other machines, whether a secret is written into the settings file, the state of the database and of the backups, who can answer from a chat, and how it is running (whether the scheduler's heartbeat is current, free disk, the clock, requests that wait too long, the seal over the decision history). It makes no network call, changes nothing, never prints a secret, and gives the fix for each finding as text for you to run. It does **not** check the ERPNext service account (that needs ERPNext: `erp-agents erp-check-access` does, read-only) or anything outside this computer (the firewall, the tunnel, ERPNext's own exposure).

## How this is tested

The regression tests for the reviews of 19 Oct (penetration test) and 24 and 25 Oct 2026 are `tests/test_hardening.py`, `tests/test_security_phase1.py` (a local capture server proving a key never goes to a second host or through a redirect, a plain question never being offered `prepare_order_request`, forged WhatsApp and Telegram webhooks being refused, the access-key defaults), `src/erp_agents/packs/stock_procurement/tests/test_grounding_arguments.py` (numbers from failed calls are rejected), `tests/test_mcp_server.py` (Host and Origin refused with a valid token, private by default, no OAuth discovery) and `tests/test_operations.py` (schema versions, instance lock, backup and restore, the doctor), `tests/test_obs_secrecy.py` (no key, token or phone number reaches a log line, in either format, through the real error paths), `tests/test_obs_metrics.py` and `tests/test_obs_tracing.py` (nothing personal is a label, a value or an attribute) and `tests/test_obs_doctor.py`. `bandit` and `pip-audit` are run with every change (see `docs/DEVELOPING.md`). Last full run: 1,792 passed and 1 skipped (a POSIX-only permission test) on Windows with Python 3.14, 28 Oct 2026; bandit reports no issue (20 justified `nosec` lines, each saying why); pip-audit reports no known vulnerability.

## Hardening you should do

1. Give the ERPNext account the app runs as only the roles it needs (Purchase User, Stock User, Accounts User, Accounts Manager); `erp-agents erp-check-access` tells you what is surplus.
2. Use long random values for `ERP_AGENTS_OWNER_TOKEN`, `WHATSAPP_VERIFY_TOKEN` and `TELEGRAM_WEBHOOK_SECRET`. Keys shorter than 24 characters are accepted from the local machine only.
3. Expose only the webhooks to the internet when staff use the console on the local network (`server.remote_access: webhooks_only`).
4. Run a single instance. SQLite and the in-process scheduler do not support several replicas.

## Supported versions

Security fixes go to the latest release (pre-1.0: the latest 0.x).

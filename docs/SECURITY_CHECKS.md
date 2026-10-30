# Security checks: what is tested, by which test, and what it becomes in a Frappe app

This is the register of the security checks of the penetration test of 19 Oct 2026 and the reviews of 24 and 25 Oct 2026, with the tests that now run them in CI, and (since Phase 4 of the hardening review) the checks made against the text that reaches a language model. Nothing here is a new penetration test: it is the same checks, made permanent. [PENTEST_REPORT.md](PENTEST_REPORT.md) says what was found; this page says what keeps it from coming back.

A test named here must exist (`tests/test_security_register.py` reads this page and looks each one up). Where a check has an equivalent in a Frappe app, the last column says what it would be; those lines are a plan written from general Frappe practice and **were not run on a bench**: check them against the version you target (see [FRAPPE_APP_PLAN.md](FRAPPE_APP_PLAN.md)).

## The levels

| Level | Meaning | In a Frappe app |
|---|---|---|
| `public` | answers anybody; nothing it returns is private | a `@frappe.whitelist(allow_guest=True)` method (only the webhooks and liveness would be) |
| `self` | checks its own credential inside the handler (a webhook's signature, the scraper's token) | a guest method that verifies the signature before it reads anything |
| `viewer` | may look: the person can read an Item or a Purchase Order in ERPNext, or holds a viewer access key | `frappe.has_permission(doctype, "read")` in a whitelisted method |
| `approver` | may answer requests and start work that prepares requests: the person can create a Purchase Order | `frappe.has_permission("Purchase Order", "create")` |
| `admin` | may change settings and start runs: Administrator or System Manager | `frappe.only_for("System Manager")` or the Settings DocType's permission rows |

## The routes

`tests/test_routes_sweep.py` compares this table with the routes the application really has (so a new route that is not listed here fails the build), turns away a stranger on every protected row in six ways, and checks that each level gets exactly what the row says: a person below the level is refused with 403, a person at or above it is not.

<!-- routes -->
| Method | Path | Level | Why this level |
|---|---|---|---|
| GET | `/health` | public | liveness; a stranger gets only `{"ok": true}` and no call to ERPNext, a signed-in person gets more |
| GET | `/ready` | public | yes or no for a stranger; the checks only for a signed-in person |
| GET | `/api/v1/auth/mode` | public | the sign-in screen asks it before anybody is signed in |
| POST | `/api/v1/auth/login` | public | the sign-in itself; throttled per address, the same answer for an unknown user and a wrong password |
| POST | `/api/v1/auth/verify` | public | the second step of the sign-in; throttled |
| POST | `/api/v1/auth/logout` | public | ends the caller's own token; nothing to protect |
| GET | `/` | public | redirects to the console |
| GET | `/ui/{path}` | public | the console's files; no data in them, script pinned by hash |
| GET | `/webhooks/{channel_type}` | self | a channel's handshake, checked against the verify token |
| POST | `/webhooks/{channel_type}` | self | a channel's message, checked by signature or secret before it is read |
| GET | `/metrics` | self | exists only when switched on; the scraper's own token or an administrator |
| GET | `/api/v1/auth/me` | viewer | who the caller is |
| GET | `/api/v1/packs` | viewer | which packs are installed and on |
| GET | `/api/v1/settings/schema` | viewer | the shape of the settings |
| GET | `/api/v1/settings/system` | viewer | the system settings (no secret in them) |
| PUT | `/api/v1/settings/system` | admin | changes settings |
| GET | `/api/v1/settings/module/{kind}` | viewer | a module's settings |
| PUT | `/api/v1/settings/module/{kind}` | admin | changes settings |
| GET | `/api/v1/settings/audit` | viewer | who changed which setting |
| GET | `/api/v1/settings/procurement` | viewer | the procurement settings |
| PUT | `/api/v1/settings/procurement` | admin | changes settings |
| GET | `/api/v1/settings/policy/{scope_type}/{scope_key}` | viewer | the stock rules for an item, group or the whole stock |
| PUT | `/api/v1/settings/policy/{scope_type}/{scope_key}` | admin | changes the stock rules |
| DELETE | `/api/v1/settings/policy/{scope_type}/{scope_key}` | admin | removes an override |
| GET | `/api/v1/agents/status` | viewer | whether the AI agents are on |
| GET | `/api/v1/agents/privacy` | viewer | what is sent to a model |
| POST | `/api/v1/agents/check` | admin | contacts a model server on the administrator's behalf |
| GET | `/api/v1/agents/usage` | viewer | tokens used |
| GET | `/api/v1/agents/tasks` | viewer | the agents' task list |
| GET | `/api/v1/agents/tasks/{task_id}` | viewer | one task |
| GET | `/api/v1/agents/brief` | viewer | the latest stock brief |
| POST | `/api/v1/agents/brief` | approver | starts the agent that writes the brief |
| POST | `/api/v1/agents/ask` | viewer | a question to the AI assistant (it may prepare an order request only for an approver) |
| GET | `/api/v1/agents/reviews/{approval_id}` | approver | the second opinion on a request |
| POST | `/api/v1/agents/reviews/{approval_id}` | approver | starts a second opinion |
| GET | `/api/v1/work-orders` | viewer | the work orders |
| GET | `/api/v1/approvals` | approver | the requests waiting for an answer |
| GET | `/api/v1/approvals/{approval_id}` | approver | one request |
| POST | `/api/v1/approvals/{approval_id}/decision` | approver | answers a request: the only way anything is ordered |
| GET | `/api/v1/history` | approver | the decision history |
| GET | `/api/v1/history.csv` | approver | the decision history as a file |
| GET | `/api/v1/history/verify` | admin | checks the seal over the history |
| GET | `/api/v1/history/{approval_id}` | approver | one request's history |
| GET | `/api/v1/assistant/status` | viewer | whether "Ask why" is on |
| POST | `/api/v1/assistant/ask` | approver | "Ask why" about a request |
| GET | `/api/v1/assistant/history/{approval_id}` | approver | earlier answers about a request |
| POST | `/api/v1/chat` | viewer | the chat (it never approves anything) |
| GET | `/api/v1/inventory/items` | viewer | the items |
| GET | `/api/v1/inventory/stock-options` | viewer | the stock columns and warehouses |
| GET | `/api/v1/inventory/alerts` | viewer | the stock alerts |
| GET | `/api/v1/inventory/runs` | viewer | the stock checks that ran |
| POST | `/api/v1/inventory/run` | admin | runs the stock check |
| GET | `/api/v1/inventory/analysis/{item_code}` | viewer | the analysis of one item |
| GET | `/api/v1/inventory/forecast/{item_code}` | viewer | the demand forecast of one item |
| GET | `/api/v1/inventory/slow-stock` | viewer | slow and dead stock |
| GET | `/api/v1/inventory/substitutes/{item_code}` | viewer | approved alternative items |
| GET | `/api/v1/inventory/counts` | viewer | cycle counts |
| GET | `/api/v1/inventory/transfers` | viewer | stock transfer requests |
| POST | `/api/v1/inventory/transfers` | approver | prepares a stock transfer request (a person still answers it) |
| GET | `/api/v1/procurement/proposals` | viewer | the purchase proposals |
| GET | `/api/v1/procurement/preview/{item_code}` | viewer | a preview of an order |
| POST | `/api/v1/procurement/run` | admin | runs the procurement pass |
| GET | `/api/v1/followup/late-deliveries` | viewer | late purchase orders |
| GET | `/api/v1/followup/receipts` | viewer | goods receipt differences |
| GET | `/api/v1/followup/scorecard` | viewer | the vendor scorecard |
| GET | `/api/v1/followup/quotes` | viewer | quote rounds |
| POST | `/api/v1/followup/quotes` | approver | starts a quote round (a person still answers it) |
| POST | `/api/v1/followup/run` | admin | runs the follow-up pass |
<!-- /routes -->

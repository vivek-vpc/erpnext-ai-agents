# Publishing plan: GitHub first, then the Frappe Cloud Marketplace

> **This plan was written on 18 Oct 2026.** The blocker tables below are kept as written, so that it is clear what was asked for; the section "Status on 25 Oct 2026" right
> after this note says what has since been done and what has not. **This repository is a standalone Python service (FastAPI + SQLite). It is not a Frappe app, it has
> not been submitted to the Frappe Marketplace, and nothing in it can be installed from there.** The Marketplace needs a separate port (section 3 here, and
> [FRAPPE_APP_PLAN.md](FRAPPE_APP_PLAN.md)), which has not been started.

## Status on 25 Oct 2026

Code: 1,282 passed and 1 skipped (a POSIX-only permission test) on Windows with Python 3.14, 25 Oct 2026; `bandit` and `pip-audit` report nothing. Inventory monitor, vendor selector, landed cost, demand calendar, approval channels (console, inbox, Telegram,
WhatsApp), the "Ask why" assistant, chat, the AI agents, the MCP server and the web console exist. Checked live against an ERPNext v16 test instance on 20-22 Oct 2026 (reads,
vendor comparison, sign-in, permissions, draft Purchase Order and Material Request; see [E2E_TEST_RESULTS.md](E2E_TEST_RESULTS.md)); that live suite has **not** been re-run since.
Telegram and WhatsApp messages were delivered on the author's own accounts; WhatsApp button replies are covered by fake-transport tests only.

| Item (18 Oct) | Status on 25 Oct 2026 |
|---|---|
| B1 currency | Closed (fixed with tests, 21 Oct). |
| B2 live WhatsApp | Partly: delivered live on the author's own account; button replies and templates are fake-transport tests; Meta's sandbox not tried by anyone else. |
| B3 draft Purchase Order live | Closed: passed live on 20-22 Oct (PO-05, TRACE-01). |
| B4 secrets and local files | `.gitignore` is complete (`.env`, `data/`, `*.db`, logs, generated test data, `ui-design/`). A local git repository now exists with a baseline commit that was scanned for secrets, personal data and local files; **nothing has been pushed or published**. Check any new first commit with [FIRST_COMMIT_CHECKLIST.md](FIRST_COMMIT_CHECKLIST.md). |
| B5 default admin token | Closed: the demo key and any short key work only on the server machine; the live config has no fallback; access keys are local-only by default, demo included. |
| B6 LICENSE | Closed: the GPL-3.0 text is in `LICENSE`; `pyproject.toml` says `GPL-3.0-or-later` (SPDX) and ships the file. |
| S1 README | Rewritten (status block, limits). |
| S2 CI | `.github/workflows/ci.yml`: tests on Linux and Windows, Python 3.10-3.14; `bandit` and `pip-audit`; a scan for secrets and personal data. **Not yet run on GitHub**: no repository is connected. |
| S3 `pyproject.toml` | SPDX licence and `license-files` done; `tools/` and `tests/` are not in the wheel. **Authors and project URLs are still missing** (they need the owner's name, contact and repository URL). |
| S4 WhatsApp setup doc | The README describes the templates; no separate `docs/whatsapp-setup.md` with screenshots. |
| S5 per-warehouse levels, sales and finance agents | Not started (roadmap). |
| S6 test-data generator | Kept as a dev tool; the generated manifest is git-ignored. |

Not in the 18 Oct list, and still open for a public release: the owner's decisions on the service account's roles, ERPNext's network exposure, tunnel mode and folder permissions
(see [PENTEST_REPORT.md](PENTEST_REPORT.md), "Re-validation, 25 Oct 2026"), the repository's name and URL, and the Marketplace port.

---

## 1. What must be finished before anything is published

### Blockers (do these first)

| # | Item | Why it blocks | How to close it |
|---|---|---|---|
| B1 | **Vendor prices are compared in each vendor's own currency, with no conversion.** `core/erp/erpnext.py` reads the Purchase Order Item `rate` (document currency) and the scoring never looks at currency. | Your test catalogue has Indian vendors at Rs 10.10 and Vietnamese/Chinese vendors at 0.096 / 1.021 (their own currency). Unconverted, the import looks about 100x cheaper and **wins the comparison with a wrong "why this vendor" message**. This is the worst kind of bug for this product. | Read `base_rate` (company currency) and the PO `conversion_rate`; compare and display in company currency; show the original price as a note ("USD 0.096 at 83.2"). Add tests with a USD vendor vs an INR vendor. Test case D3 in the test plan proves the fix. |
| B2 | **Nothing has run against live WhatsApp.** Payload shapes follow Meta's docs but are unverified. | You cannot publish a "WhatsApp approvals" feature that has never delivered a message. | Run test plan section C with a real Meta sandbox number. Fix whatever Meta rejects. |
| B3 | **Draft Purchase Order creation has never run against a live ERPNext** (README: "Not tested against a live ERPNext order"). | It is the one write the agent makes. | Run test cases D5/D6. Check supplier, qty, rate, required-by date, tax template, and that the PO is Draft. |
| B4 | **Secrets and local files must not reach git.** `.env` exists in the folder (ignored by `.gitignore`, good). `tools/testdata/manifest.jsonl` (about 660 KB) and `tools/testdata/*.log` are not ignored. | A leaked WhatsApp token or ERPNext key is public the moment you push. | Add to `.gitignore` (section 2 below); run `git status` before the first commit; never `git add .` blindly. If a secret was ever committed, rotate it, do not just delete the file. |
| B5 | **Shipped default admin token.** `config/settings.erpnext.yaml` uses `${ERP_AGENTS_OWNER_TOKEN:-demo-owner-token}`. | Anyone who knows the default (it will be public) can change thresholds and answer approvals if the server is ever bound to a non-local address. | Remove the default, or make `serve` refuse to start with the default token unless `host` is `127.0.0.1`. |
| B6 | **No LICENSE file** (pyproject says GPL-3.0-or-later). | Without the file, nobody can legally use or contribute to the repo, and the Marketplace reviewers will ask. | `Invoke-WebRequest https://www.gnu.org/licenses/gpl-3.0.txt -OutFile LICENSE` |

### Should finish (before the Marketplace, fine for a first GitHub release if stated in the README)

| # | Item | Note |
|---|---|---|
| S1 | README is stale ("103 tests", mentions Telegram first, no ERPNext user-permission list in one place). | Rewrite as: what it is, 5-minute demo, connect ERPNext, channels, limits. Keep the long reference in `docs/`. |
| S2 | CI. | Use `.github/workflows/ci.yml` (included): pytest on Linux and Windows, Python 3.10-3.13. |
| S3 | `pyproject.toml` metadata: authors, URLs, classifiers, `license` as an SPDX string, `tools/` kept out of the wheel. | Needed for a clean `pip install` and for the Marketplace listing. |
| S4 | WhatsApp templates: document the exact template text and buttons (README has it) and add `docs/whatsapp-setup.md` with screenshots from your real setup. | Most support questions will be here. |
| S5 | Multi-warehouse reorder levels, sales and finance agents. | Not needed for v0.1. List them under "Roadmap". |
| S6 | The test-data generator (`tools/testdata`). | Valuable, but keep it in the repo only as a documented dev tool. Do not ship the generated manifest. Note its fictional rates are test values, not tariff advice (already in the file header). |

---

## 2. Phase 1: GitHub (about 1 day after the blockers)

> Status 25 Oct 2026: step 1 below (`git init`) has been done locally and the baseline commit was checked; steps 3 to 6 (push, branch protection, tag, release) have **not** been done
> and need the owner. Before any push, run the checks in [FIRST_COMMIT_CHECKLIST.md](FIRST_COMMIT_CHECKLIST.md) on whatever is about to be pushed.

1. **Clean the folder** (PowerShell, in the repository folder):
   ```powershell
   Add-Content .gitignore "`ntools/testdata/manifest.jsonl`ntools/testdata/*.log`n*.db`n.vscode/`n.env.*`n!.env.example"
   git init -b main
   git status          # read this list. No .env, no data/, no .venv, no manifest.jsonl
   ```
2. **Add** `LICENSE`, `.github/workflows/ci.yml`, `SECURITY.md` (how to report a problem privately), `CONTRIBUTING.md` (run tests, code style), and an issue template.
3. **First commit and push** (create an empty repo on github.com first, no README, no licence):
   ```powershell
   git add -A
   git commit -m "Initial release: inventory monitor, vendor selector, approvals"
   git remote add origin https://github.com/vivek-vpc/erpnext-ai-agents.git
   git push -u origin main
   ```
4. **Protect `main`**: Settings > Branches > require the CI check before merging.
5. **Tag a pre-release**: `git tag v0.1.0-beta.1 && git push --tags`, then create a GitHub Release. Write "tested against fakes, live-tested on ERPNext v16 and WhatsApp sandbox: yes/no" honestly in the notes.
6. **Do not publish to PyPI yet.** Wait until the WhatsApp and ERPNext live tests (section C and D of the test plan) pass.

---

## 3. Phase 2: from a Python service to a Frappe app

> Status 25 Oct 2026: **not started.** Everything in this section is a plan. [FRAPPE_APP_PLAN.md](FRAPPE_APP_PLAN.md) lists the work, what is missing and the effort.

The Marketplace lists **Frappe apps** (a bench app that installs into a site). Frappe Cloud cannot run
your separate `erp-agents serve` process, SQLite file, Telegram poller or local Ollama. So the
Marketplace version is a **port**, not an upload. The good news: your design (an `ERPClient` interface,
channel-neutral approvals, plain-maths analysis) makes the port mostly a swap of the edges.

| Today | In the Frappe app |
|---|---|
| `core/erp/erpnext.py` (REST + API key) | `FrappeERPClient` using `frappe.get_all` / `frappe.get_doc` in-process. No API key, faster, permission-checked. |
| SQLite (`storage.py`) | DocTypes: Agent Settings (Single), Inventory Alert, Approval Request, Purchase Proposal, Work Order, Settings Audit. |
| APScheduler | `hooks.py` `scheduler_events` (cron) and `frappe.enqueue` for the procurement pass (every 4 minutes at best: the scheduler tick, measured on a bench on 5 Nov 2026). |
| `/webhooks/whatsapp` (FastAPI) | A whitelisted guest method, `/api/method/erp_agents.webhooks.whatsapp`, with the same signature check. Frappe Cloud already gives you public HTTPS, which removes the tunnel step. |
| Tokens in `.env` | `Password` fields in Agent Settings. |
| `packs/stock_procurement/web/index.html` console | Desk workspace plus list/form views; the approval inbox as a Desk page. |
| Telegram polling thread | Webhook mode only. |
| Ollama "Ask why" | Optional, off by default; local Ollama is not available on Frappe Cloud. |

Keep the core (`analysis.py`, `scoring.py`, `taxes.py`, `messages.py`, approvals, channels) as the
shared library. Two repos or one monorepo with `erp_agents_core/` and `erp_agents_frappe/`:

1. **Spike (about 1 week)**: `bench new-app erp_agents`, one DocType, one scheduled job that runs the inventory analysis through `FrappeERPClient` and writes Inventory Alerts. Prove it on a local bench (frappe_docker devcontainer).
2. **Approvals and WhatsApp** in the app; run the whole test plan again on the app (sections B to E).
3. **Procurement** and draft PO creation inside the app.
4. **Hardening**: Frappe roles (Agent Admin, Agent Approver), translations hooks, install/uninstall cleanly, patches for upgrades, Frappe v15 and v16 support.
5. **Frappe Cloud private-bench trial**: install from your GitHub repo on a trial site and re-run the end-to-end test.

> Verified again on 21 Oct 2026 against docs.frappe.io: a **passing CI is mandatory**, the app must not override base Frappe functionality, Semgrep with the Frappe rules is recommended, and custom apps declare the Frappe versions they support in `pyproject.toml`. The rules for new code that follow from this are in [DEVELOPING.md](DEVELOPING.md).

### Marketplace submission (from Frappe's guidelines)

- Frappe Cloud account, then Settings > Profile > **Become a Publisher**.
- Marketplace tab > **+ Add App** > **Add from GitHub** > pick the Frappe version > **Add to Marketplace** (starts as Draft).
- Listing: unique app name; title; one-sentence short description (40-80 characters); long description without installation steps; **square logo at least 200x200 px, no text**; one category; five links (**Website, Support, Documentation, Terms of Service, Privacy Policy**); screenshots; a demo video that is prepared and sent on request.
- Must support the current stable Frappe/ERPNext version and must not steer users away from Frappe Cloud.
- Publish a release; the Frappe team reviews the app and each release (stated review time: 10 days).

Privacy policy matters here: the app sends stock and supplier summaries to WhatsApp/Telegram (and to a
language model if the assistant is on). Say exactly what leaves the site.

---

## 4. Suggested order and rough effort

| Step | Effort | Done when |
|---|---|---|
| 1. Fix B1 (currency) with tests | 0.5 day | Test D3 passes |
| 2. Live WhatsApp + ERPNext runs (test plan C, D, E) | 2-3 days | All C/D/E cases pass or have a filed issue |
| 3. B4-B6, S1-S3, push to GitHub, beta release | 1 day | CI green on `main` |
| 4. Collect feedback for 1-2 weeks | - | - |
| 5. Frappe app spike and port | 3-5 weeks | Test plan passes on a Frappe Cloud trial site |
| 6. Marketplace listing and review | 1-2 weeks | Listing approved |

Publish on GitHub early; submit to the Marketplace only after the port passes the same end-to-end test.

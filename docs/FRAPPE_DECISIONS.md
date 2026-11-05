# FRAPPE_DECISIONS.md: what the owner decided for the Frappe app AI Munimji

These are decisions of the owner, given in writing on **5 Nov 2026** after the Phase 0 report ([FRAPPE_FACTS.md](FRAPPE_FACTS.md)) and the Phase 1 report ([FRAPPE_SPIKE_REPORT.md](FRAPPE_SPIKE_REPORT.md)). They are recorded here so that the plan ([FRAPPE_APP_PLAN.md](FRAPPE_APP_PLAN.md)) and the code can point to them. A decision changes only when the owner says so; this file then gets a new dated line, and the old one stays.

Nothing here says the app is released, listed or installable from the Marketplace. The only versions tried are Frappe 16.55.2 and ERPNext 16.53.0.

## The eight decisions

| # | Decision | Date | What follows from it |
|---|---|---|---|
| 1 | **Phases 2 to 7 are the plan's work packages**, mapped as in the table below. Webhooks, the agents, the AI features and MCP stay out of version 0.1. | 5 Nov 2026 | The estimate for version 0.1 is 57 to 84 working days ([FRAPPE_SPIKE_REPORT.md](FRAPPE_SPIKE_REPORT.md) section 7). |
| 2 | **Who may look at stock.** The Munimji roles (Viewer, Approver, Manager) protect only the app's own DocTypes. ERPNext's own roles (Stock User, Purchase User, Purchase Manager) protect what ERPNext holds. The app ships **no permission rows on ERPNext's DocTypes**. The required roles are documented in the install notes. | 5 Nov 2026 | A user with only Munimji Viewer cannot read Item, Bin or Stock Ledger Entry. That is intended. |
| 3 | **Uninstall.** Keep the custom Permission Type ("approve") and the documented flag that lets `before_uninstall` delete it. The database columns the app added stay after an uninstall (Frappe never drops a column). The uninstall page says what stays. | 5 Nov 2026 | Tested in the spike (`dev/check-uninstall.sh`): roles, custom fields, the scheduled-job record and the Permission Type are removed; the columns are not. |
| 4 | **A 4-minute tick.** The stock check and the procurement pass run at the scheduler's tick, 4 minutes (240 s), the finest Frappe offers. Any schedule an administrator can edit is a job linked to a Scheduler Event. Every "3 minutes" in the Frappe app's documents becomes 4 minutes. | 5 Nov 2026 | The standalone service keeps its own 3-minute pass (its APScheduler is unchanged); only the text about the Frappe app changed. |
| 5 | **The reads rule.** A signed-in user's reads use `frappe.get_list` or an explicit permission check. `frappe.get_all` is used only in the job that runs as Administrator. A test enforces this, and it applies to every later phase. | 5 Nov 2026 | `get_all` ignores permissions (spike, surprise 2). |
| 6 | **Plan corrections** are applied as the first commit of Phase 2: FRAPPE_FACTS.md section 9 and the wrong or partly right rows of FRAPPE_SPIKE_REPORT.md section 5, in FRAPPE_APP_PLAN.md. | 5 Nov 2026 | Done in the first commit of branch `frappe/2-engine`. |
| 7 | **Identity and publisher contact.** Commit identity in the AI-Munimji repository: name `VIVEK C`, e-mail `42648747+vivek-vpc@users.noreply.github.com`, set in that repository only; the existing commits are rewritten to it before any push. The publisher contact in `pyproject.toml` and `hooks.py` is the owner's own address, which will be public. | 5 Nov 2026 | Step 1 of Phase 2, in AI-Munimji. |
| 8 | **Confirmed from Phase 0** (four points): (a) Frappe v16 and ERPNext v16 only for version 0.1; (b) two repositories, `AI-Munimji` for the app and `erpnext-ai-agents` for the standalone service and the engine; (c) the engine is a PyPI package `ai-munimji-engine` under the conditions of [FRAPPE_FACTS.md](FRAPPE_FACTS.md) 6.3, with vendoring as the fallback; (d) the overlap with the name "Munimji ERP" is acceptable for the engineering work, with a trademark check before the public listing. | 5 Nov 2026 | Whether a Marketplace app may depend on its own PyPI package is still unknown and is tried early in the private-bench trial. |

## Phases and work packages

| Phase | Content | Packages of the plan | Days (revised) |
|---|---|---|---|
| 2 | Engine ready for two storages; plan corrections; git identity | 1 | 12 to 18 |
| 3 | The app proper: Settings DocType, roles, install and uninstall hooks, custom fields on Purchase Order, Material Request and Stock Entry, `_()` everywhere; the Pack table and manifest sync only if version 0.1 needs them | 2 | 3 to 5 |
| 4 | `FrappeERPClient`; DocTypes and repositories for the stock pack's records (alert, proposal, approval request, audit seal); permission tests with three users | 3 and 4 | 14 to 21 |
| 5 | Scheduler events and jobs; the approval flow and form (reasoning, why not the other vendors); inbox list; stock monitor; notifications | 5 and 6 | 16 to 22 |
| 6 | Tests and CI on GitHub Actions with a bench; Semgrep with Frappe's rules; security checks rebuilt for whitelisted methods; the privacy test | 8 | 4 to 6 |
| 7 | Listing, privacy, support, documentation and terms pages; logo; screenshots; demo-data command; changelog; release checklist | 9 | 8 to 12 |
| 8 | Trial on a private Frappe Cloud bench, submission and review fixes (mostly the owner's) | | not counted |

## Rules from the spike that apply to every later phase

1. A permission row grants every right that is not switched off. After writing any permission row, read the exported JSON and test each role for read, write, create and delete.
2. `frappe.get_all` ignores permissions (decision 5).
3. A permission hook's argument is `ptype`, and hooks can only deny.
4. Nothing scheduled runs more often than every 4 minutes. A job runs as Administrator.
5. Uninstall cannot remove database columns. The documents say so.
6. The version log is off in tests, and INFO log lines are invisible by default. Tests account for both.
7. `[tool.bench.frappe-dependencies]` is added to `pyproject.toml`, and the generated CI installs ERPNext.
8. Before the full suite, check free RAM and stop the containers that are not needed. The last full run took 54 minutes on a machine with 0.9 GB free; it should take about 5. Subsets while developing; the full suite before each commit.

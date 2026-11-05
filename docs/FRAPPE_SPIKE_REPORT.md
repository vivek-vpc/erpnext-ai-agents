# AI Munimji spike (Phase 1): what a real bench showed

Written on 5 Nov 2026 for Phase 1 of the Frappe app AI Munimji (`ai_munimji`). A throw-away spike was built and tested on a real bench, to prove the pieces and to measure the effort. The code is in a separate repository, `C:\ERP\AI-Munimji`, branch `frappe/1-spike` (10 commits, no remote, nothing pushed or published). This page is the only change in `erpnext-ai-agents`.

**What was run on:** Frappe 16.55.2 (`3d1284c`) and ERPNext 16.53.0 (`34f6ff8`), in Frappe's official dev container (image `frappe/bench`, Python 3.14.10, Node 24.22.2, MariaDB 11.8), from Windows 11 through Docker Desktop. Nothing here says the app is on the Marketplace, installable from it, or compatible with any other Frappe version.

## 1. Summary

- **It works.** A bench app installs on a site; a DocType (`Munimji Approval Request`) with a custom "approve" permission; a scheduled job reads `Bin` in process and creates a Pending request; a button on the form creates a **Draft** Purchase Order as the signed-in user, with the reference and the approver on it, and a second press is refused; an uninstall removes the records the install added. 20 tests pass; the round trip was also run over real HTTP (session login, CSRF token) and by clicking the real button in the v16 Desk in a headless Chromium.
- **The plan was right about the shape and wrong in places that matter.** Of the 46 claims checked (section 5): 20 right, 15 partly right, 3 wrong, 8 not tested. The wrong or incomplete ones are mostly about permissions, the scheduler and the uninstall.
- **The five findings that change the plan** (section 6): a permission row silently grants every right you do not switch off; `frappe.get_all` ignores permissions; nothing scheduled runs more often than every 4 minutes; Frappe cannot uninstall everything an app adds (database columns stay, and a Permission Type can only be deleted with a flag); v16 has custom Permission Types, which the plan did not know.
- **The effort is lower than the plan's, in the parts measured.** The bench from nothing took about 10 minutes (I had estimated 45 to 70), and the spike took about one working day of my time, of which about 70 minutes were on the bench (the plan said 1 to 1.5 weeks). Revised estimate for version 0.1 (nothing leaves the site, no model): **57 to 84 working days** against the plan's 95 to 135 for everything (section 7). The largest uncertainty is the Desk UI and the engine split, neither of which the spike touched.
- **Not tested** (section 8): GitHub Actions, Frappe Cloud, webhooks, agents and MCP, the engine, custom pages, translations, `frappe.enqueue`, anything on another Frappe version.

## 2. Where it is, and how to repeat it

The repository `C:\ERP\AI-Munimji` (branch `frappe/1-spike`):

| Path | What |
|---|---|
| `ai_munimji/` | the app: `hooks.py`, `install.py`, `uninstall.py`, `permissions.py`, `jobs.py`, the DocTypes under `ai_munimji/ai_munimji/doctype/`, the Permission Type, `fixtures/custom_field.json`, `tests/` |
| `pyproject.toml` | with `[tool.bench.frappe-dependencies]` for `frappe` and `erpnext` |
| `dev/` | the development environment and every script, with `dev/README.md` |

Size: 367 lines of app Python, 230 of tests, 32 of JavaScript, 346 of DocType JSON, 1,281 lines of dev scripts.

**The commands you run** (PowerShell, in `C:\ERP\AI-Munimji`):

```
.\dev\up.ps1                                   # images and containers (about 20 s when the images are on the machine)
.\dev\exec.ps1 bash /workspace/ai_munimji/dev/bootstrap.sh          # bench, ERPNext, the dev site, the app (about 10 minutes)
.\dev\exec.ps1 bash /workspace/ai_munimji/dev/run-script.sh /workspace/ai_munimji/dev/scripts/seed.py   # a company, a supplier, three items, two users
.\dev\exec.ps1 bash /workspace/ai_munimji/dev/run-script.sh /workspace/ai_munimji/dev/scripts/reset_demo.py   # two requests waiting
.\dev\exec.ps1 bench start                     # leave it running; the site is http://localhost:8000
```

Or all of it, from nothing, with a timing table at the end: `.\dev\verify-all.ps1 -Fresh` (it removes only the compose project `munimji-dev`).

**The round trip in the browser:**

1. Open `http://localhost:8000` and sign in as `munimji.approver@example.com`, password `Munimji-dev-1!` (the Administrator is `Administrator` / `admin`).
2. In the sidebar, "AI Munimji", then "Munimji Approval Request": two requests are Pending (the stock job made them: 17 bolts is 2 x 10 - 3, 12 washers is 2 x 10 - 8).
3. Open one and press **Create Purchase Order**, then **Yes**. The request becomes Ordered and shows the draft order; open it: its "Munimji request" field holds the request name, "Approved by (Munimji)" holds the approver, and it is a Draft.
4. The button is gone from an Ordered request. To see the server refuse a second press: `.\dev\roundtrip.ps1` (it runs the whole round trip over HTTP, including the refusal, HTTP 417).
5. Sign in as `munimji.viewer@example.com` and press the button on the other request: "not permitted", and the request stays Pending.
6. Put the demo back: the `reset_demo.py` command above.

**The uninstall:** `.\dev\exec.ps1 bash /workspace/ai_munimji/dev/check-uninstall.sh` (about 3 minutes). It builds a fresh throw-away site, snapshots it before the app (A), with the app (B), after the uninstall (C) and after installing again (D), and prints the differences.

**The checks before a commit:** `.\dev\exec.ps1 bash /workspace/ai_munimji/dev/gate.sh` (tests, bandit, pip-audit) and `.\dev\semgrep.ps1` (Frappe's Semgrep rules). The browser check: `.\dev\exec.ps1 bash -c "cd /workspace/ai_munimji/dev/browser && node round-trip.cjs"`.

## 3. What the spike contains

- **DocTypes** made the way a developer makes them (developer mode; Frappe writes the JSON and controller files into the app): `Munimji Approval Request` (item, warehouse, quantity, supplier, reason, status, approved by, purchase order; named `MAR-#####`; version log on) and `AI Munimji Settings` (a Single: a switch, a threshold, a fallback supplier, one Password field).
- **Roles** `Munimji Viewer`, `Munimji Approver`, `Munimji Manager`, made by `before_install` and removed by `before_uninstall`.
- **The "approve" permission**: a Permission Type (new in v16) granted to Approver, Manager and System Manager in the permission rows. `ai_munimji/permissions.py` is a `has_permission` hook that denies "approve" to anyone who cannot create a Purchase Order in ERPNext.
- **`create_draft_purchase_order()`** on the DocType controller (whitelisted, POST): checks "approve", locks the row (`SELECT ... FOR UPDATE`), refuses a request that is not Pending or already has an order, and inserts a Draft Purchase Order as the signed-in user, without `ignore_permissions`. The company comes from the warehouse. The form button calls it.
- **Two fields on Purchase Order**, exported as fixtures: `custom_agent_ref` (Data, unique, read-only) and `custom_agent_approver` (Link to User).
- **A scheduled job** `ai_munimji.jobs.check_stock`, registered with a `cron` entry in `hooks.py`: reads `Bin`, creates one Pending request per item and warehouse below the threshold, none while one is pending, and never an order.
- **An uninstall** (`before_uninstall`): removes the scheduled-job record, the Custom Field records, the Permission Type and the roles.
- **20 tests** with `ERPNextTestSuite`: the permission rows; the viewer; "approve" with and without the right to create orders; a buyer who may create orders but is not an approver; the draft order and its reference; the second order refused by the app and, separately, by the database; ordinary orders unaffected by the unique field; the Password field kept out of the table and out of the version log; the job and its idempotency; the schedule; what the install added. **Five deliberate breaks** (the hook allows everything; the approve check removed; the job asks again; the status check removed; the approver not recorded) are each caught by a named test; two were missed at first and two tests were added.
- **Checks:** bandit and pip-audit clean; Frappe's Semgrep rules (50 ids at `81a6e3d`) find nothing in `ai_munimji/`, and flag three things in a deliberate violation, so the check does see.

## 4. Setup: what was done, and what did not work

Recommended and used: Frappe's official dev container, run from the command line, with the bench in a Docker volume and only the repository bind-mounted (see `dev/compose.yml`). Everything outside the project folder was two image pulls and the project's volumes, run by you.

| # | What happened | Cause | What was done |
|---|---|---|---|
| 1 | The first `up.ps1` failed after 492 s: `lookup auth.docker.io: no such host` | a name-lookup failure inside Docker Desktop, probably transient (Windows had two active network adapters); Windows and a container could both resolve the name minutes later | `up.ps1` now pulls first, up to three tries, only missing images (the retry succeeded: 453 s) |
| 2 | Your ERPNext containers were found stopped (exit 137) | most likely a Docker Desktop restart while fixing 1 (not confirmed); they have restart policy `on-failure`, which does not bring them back | not touched by me; start them yourself when you want them |
| 3 | `exec.ps1` failed with exit 127 before the bench existed | it set the working directory to the bench directory | a small wrapper (`in-bench.sh`) falls back to the parent directory |
| 4 | Copying the generated skeleton: `cp: preserving times for '/workspace/ai_munimji/.': Operation not permitted` | `cp -a` on a bind mount | `cp -rn` |
| 5 | PowerShell broke commit messages and `bash -c` one-liners (quotes) | PowerShell 5.1 native-argument quoting | commit messages from files, every container command as a script file |
| 6 | My runner could not open the log folder (`/home/frappe/dev/logs/database.log`) | Frappe expects the working directory `sites/`, as the bench commands use | run from `sites/` |
| 7 | A script that deleted and recreated the DocTypes threw `Module None not found` | the Permission Type must be deleted before its DocType; the files were already deleted but the database had rolled back | deletion order fixed |
| 8 | The Viewer's permission row came out with write, create and delete | row fields default to 1 (section 6) | every right written out; a test pins it |
| 9 | A test of the version log failed | `Document.save()` turns the log off while tests run (section 6) | `save(ignore_version=False)` in that test |
| 10 | Two of five deliberate breaks were missed by the tests | see section 3 | two tests added |
| 11 | `development.localhost` did not resolve from PowerShell or curl | Windows' own resolver does not resolve `*.localhost` (browsers do) | `default_site` in the bench config, then plain `http://localhost:8000` works |
| 12 | The browser check could not find the login button, then the dialog button | v16's Desk: `button.es-button` and `.btn-modal-primary` instead of v15's `.btn-login` and `.btn-primary` | selectors updated |
| 13 | `verify-all.ps1 -Fresh` failed at its first step when its output was redirected | docker's progress goes to stderr, which `ErrorActionPreference = Stop` turned into an error | both scripts judge steps by exit code |
| 14 | The job's own log line never appeared | Frappe's default log level is WARNING on a dev server and ERROR elsewhere | measured from the worker log instead |
| 15 | The Semgrep image exits 0 even with findings | needs `--error` | added |
| 16 | At the end, this repository's own test suite on Windows took 54 minutes instead of about 5 | the machine ran out of memory: 0.9 GB free of 23.7 GB, with the Docker VM holding 4.3 GB next to a browser and the editor | the dev containers were stopped (`.\dev\down.ps1`); stop them before running the host suite, and start them again with `.\dev\up.ps1` |

## 5. The plan against the bench

`docs/FRAPPE_APP_PLAN.md` (written 25 Oct 2026), claim by claim. **Verdicts:** RIGHT, PARTLY (right in part, or incomplete), WRONG, NOT TESTED. Evidence is a test name, a command or a file in the AI-Munimji repository; "Run" means I ran it and the output was read.

### Section 1: where the boundary is

| # | Plan says | On the bench | Verdict |
|---|---|---|---|
| 1.1 | Talks to ERPNext in process (`frappe.get_all`, `frappe.get_doc`) as the signed-in user | `get_doc(...).insert()` runs as the signed-in user: the draft order's owner is the approver, and a person who may not create orders is refused by ERPNext itself. **But `frappe.get_all` ignores permissions**: for a Munimji Viewer it returned all 3 Bin rows, and `frappe.get_list` raised `PermissionError`. A read made for a user must use `get_list`. (Test: `test_an_approver_turns_a_request_into_a_draft_order_under_their_own_name`; run: `dev/scripts/permission_probe.py`) | PARTLY |
| 1.2 | Stores its data as DocTypes in the site database | two DocTypes created in 10 s in developer mode, JSON and controller files written into the app | RIGHT |
| 1.3 | Scheduled work is `scheduler_events` and `frappe.enqueue` | a `cron` entry becomes a Scheduled Job Type at migrate and runs. **The real cadence is once per scheduler tick, 4 minutes**, whatever the cron says (section 6). Jobs run as `Administrator`. `frappe.enqueue` was not used | PARTLY |
| 1.4 | Users and permissions are Frappe's own session, roles and DocType permissions | yes, and richer: v16 adds custom Permission Types (2.3) | RIGHT |
| 1.5 | Installed by `bench get-app` / `bench install-app` | `get-app --soft-link` of a local repository 6 s, `install-app` 4 to 7 s | RIGHT |
| 1.6 | Secrets are Password fields in a Settings DocType | stored encrypted in `__Auth`, masked (`*****`) in `tabSingles`, masked in the version log (test: `test_a_password_field_is_kept_out_of_the_table_and_out_of_the_version_log`) | RIGHT |

### Section 2: core and packs as a Frappe app

| # | Plan says | On the bench | Verdict |
|---|---|---|---|
| 2.1 | One app, one Frappe module per pack | module "AI Munimji" came from the app title; both DocTypes sit in it; the uninstall removes it. The Pack table and the manifest sync were not built | RIGHT (as far as tested) |
| 2.2 | A viewer may read an Item or a Purchase Order, an approver may create one, an admin is System Manager; each a `frappe.has_permission` check on the signed-in user | the Munimji roles are ordinary roles given to users, and **a user with only Munimji Viewer cannot read Item, Bin or Stock Ledger Entry** (`has_permission` False). "May look at stock" needs ERPNext's own roles too (the demo approver holds Purchase User, Purchase Manager and Stock User). The standalone's "derived from ERPNext" has no direct equivalent (`dev/scripts/permission_probe.py`) | PARTLY |
| 2.3 | The Approval Request DocType cannot express "may create a Purchase Order" in its permission rows, so a `has_permission` hook calls the pack's pairs | v16 has **custom Permission Types**: "approve" is a column in the permission rows, granted per role, checked with `frappe.has_permission(doc, "approve")`. The hook is still needed, only to add the Purchase Order condition, and **a hook can only deny**. Its keyword is `ptype`: the documentation's example (`permission_type`) is never filled in (tests: `test_the_approve_permission_needs_the_right_to_create_a_purchase_order`, `test_the_hook_documented_with_permission_type_is_never_given_it`) | PARTLY |
| 2.4 | DocTypes called Agent Settings, Approval Request... | `AI Munimji Settings` and `Munimji Approval Request` as in the Names table | RIGHT |
| 2.5 | The settings audit is replaced by the version log of the Single DocType; check that Password values are not recorded | recorded as `["demo_secret", null, "****************"]`, never the value. **The version log is off whenever tests run** (`Document.save()` sets `ignore_version = frappe.in_test`) unless `save(ignore_version=False)` | RIGHT, with a trap |
| 2.6 | The decision-history seal becomes an append-only DocType | not built | NOT TESTED |

### Section 3: hooks.py

| # | Plan says | On the bench | Verdict |
|---|---|---|---|
| 3.1 | `app_*` identity hooks; the licence string must say GPL-3.0 in the form bench expects | nothing validates it: the template writes the lower-case id offered at the prompt (`gpl-3.0`); `GPL-3.0-or-later` installed and migrated without complaint | WRONG (no form is expected) |
| 3.2 | `required_apps = ["erpnext"]` | works; what the plan missed is `[tool.bench.frappe-dependencies]` in `pyproject.toml`: `bench validate-dependencies` enforces it (with `frappe >=17` it fails with a clear message), Frappe Cloud makes it mandatory, **and Frappe's own template does not write it** | PARTLY |
| 3.3 | `after_install`, `before_uninstall`, `after_migrate` create the roles and custom fields, sync manifests, remove what the app added | the roles must exist before the DocTypes are synced (their permission rows name them), so they are made in **`before_install`**; `before_uninstall` ran and was needed for four things (4.4); `after_migrate` not used | PARTLY |
| 3.4 | `fixtures` carry the custom fields (replacing `erp-agents erp-setup`) | the two fields were made as Custom Fields, `bench export-fixtures --app ai_munimji` wrote `fixtures/custom_field.json`, the install imported it. Fields on Material Request and Stock Entry not tried | RIGHT |
| 3.5 | `scheduler_events` with a `cron` entry or two for the stock check and the 3-minute procurement pass | the mechanism works; **a 3-minute pass cannot exist**: every entry ran once per 4-minute tick (section 6) | PARTLY |
| 3.6 | A schedule an administrator edits cannot live in `hooks.py`, so a frequent tick job asks the settings | **wrong, a better way exists**: a Scheduled Job Type linked to a Scheduler Event survives `bench migrate` and can be edited; a plain one, which is what `hooks.py` makes, is reset or deleted by the next migrate (the plain probe job was deleted, the linked one stayed). Still bound to the 4-minute tick (`dev/scripts/cadence_start.py`) | WRONG |
| 3.7 | `doc_events` | not used | NOT TESTED |
| 3.8 | `has_permission`, `permission_query_conditions` | `has_permission` works (2.3); the query conditions were not used | PARTLY |
| 3.9 | `app_include_js`, workspace and page files for the Desk screens | the form's script sits next to the DocType and is delivered with it (checked through `getdoctype` and by clicking it); no `app_include_js` is needed for a form. The v16 Desk gave the module an "AI Munimji" entry in its sidebar. Pages and dashboards not tried | PARTLY |
| 3.10 | `boot_session` | both `boot_session` and `extend_bootinfo` are hooks in v16 (the Phase 0 page was wrong about this and was corrected in `69bf033`) | RIGHT |
| 3.11 | Whitelisted methods; `allow_guest` only for the webhooks | a controller method with `@frappe.whitelist(methods=["POST"])` is what the form calls (`run_doc_method`): 200 for the approver, 403 for the viewer, 417 for a second press. Semgrep's `guest-whitelisted-method` flagged a deliberate probe. The webhooks were not built | RIGHT |
| 3.12 | The app must not override base Frappe behaviour | nothing overridden; Frappe's Semgrep rules find nothing | RIGHT |

### Section 4: installing, running, uninstalling

| # | Plan says | On the bench | Verdict |
|---|---|---|---|
| 4.1 | `bench get-app <url> --branch`, `install-app`, `migrate`, `build`, `start` | all work. For a local working copy `get-app --soft-link <path>` makes edits show at once, and the directory name must be the app name. `bench new-app` asks for title, description, publisher, contact, licence, CI workflow and branch, then installs the app and builds the assets itself (7 s). `bench start --no-dev` still starts the asset watcher | RIGHT |
| 4.2 | `bench --site <test site> run-tests --app <app>` (check the exact form) | right form. Needs `allow_tests` set and a separate site, because **ERPNext builds its whole test data set (company, warehouses, suppliers, items) the first time its test module is imported: 247 s the first run, 10 to 26 s after**. Every `test_*.py` under the app is found (18 to 20 tests) | RIGHT |
| 4.3 | Upgrade is a git tag plus patches, run once each from `patches.txt` | a patch ran once over two migrates (one Note, one Patch Log row); `patches.txt` has `[pre_model_sync]` and `[post_model_sync]` | RIGHT |
| 4.4 | `before_uninstall` removes what the app added (custom fields, roles); the DocType data goes with the app | **`bench uninstall-app` removed the module, both DocTypes, the table and the module definition. It left: the Role records, the Custom Field records, the scheduled-job record (until the next migrate), the Permission Type, and the database columns.** `before_uninstall` now removes the first four. A Permission Type can only be deleted in developer mode, install, migrate or tests, so the hook sets a flag around the call. **Frappe never drops a column**: the two columns on Purchase Order (with the unique index) and three `approve` columns on the permission tables stay. Result on a fresh site: A (before) equals C (after the uninstall) except those columns, and B (with the app) equals D (installed again) apart from the random name of the job record (`dev/check-uninstall.sh`) | PARTLY |
| 4.5 | A private bench on Frappe Cloud first, then the listing | no repository, nothing pushed | NOT TESTED |

### Section 5: the service and the app side by side

| # | Plan says | On the bench | Verdict |
|---|---|---|---|
| 5.1 | Orders stay recognisable by `custom_agent_ref` | a unique, nullable, read-only Data field: the database refuses a second order with the same value (`UniqueValidationError` or `DuplicateEntryError`), and orders without it (many) are unaffected (tests: `test_the_database_refuses...`, `test_ordinary_orders...`). "Change or cancel only if the field matches" was not built | RIGHT |

### Section 6: what a Frappe app needs

| # | Plan says | On the bench | Verdict |
|---|---|---|---|
| 6.1 | A package with `modules.txt`, `hooks.py`, `patches.txt`, `config/` and a `pyproject.toml` declaring the versions | `bench new-app` writes all of it, **except `[tool.bench.frappe-dependencies]`**, and still writes the old `[deploy.dependencies.apt]` table | PARTLY |
| 6.2 | A Settings DocType (Single) with Password fields | works (1.6, 2.5) | RIGHT |
| 6.3 | DocTypes for the stored records | works; **trap: every right in a permission row defaults to 1** (write, create, delete, share, print, email), so `{"role": "Viewer", "read": 1}` makes a role that can write and delete (test: `test_the_permission_rows_grant_only_what_was_written`) | RIGHT, with a trap |
| 6.4 | Background jobs, no long-lived threads | the scheduler and a worker run the job; 4-minute tick; as Administrator. `frappe.enqueue` not used | PARTLY |
| 6.5 | Webhook endpoints as guest methods with signature checks | not built | NOT TESTED |
| 6.6 | A `FrappeERPClient` with in-process calls, as the signed-in user, permission-checked | trivial for the two calls tried (a `Bin` read, a Purchase Order insert), but see 1.1 (`get_all` against `get_list`) and 2.2 (a Munimji role carries no stock rights). The company is taken from `Warehouse.company`, not from user defaults | PARTLY |
| 6.7 | Migrations: `patches.txt`, install and uninstall hooks, custom fields as fixtures | all three exercised (3.3, 3.4, 4.3) | RIGHT |
| 6.8 | The Desk replaces the single-file console | a form with a button and a confirmation dialog works in the v16 Desk (headless Chromium: all checks pass). Custom pages, dashboards and the Ask screen were not tried; the large screens remain a rewrite | RIGHT (for a form) |
| 6.9 | Translations: `_()` around every user-visible string | `_()` and `__()` used; Frappe's translation rules in Semgrep pass; the extraction itself was not run | PARTLY |
| 6.10 | Support for the current stable Frappe and ERPNext | run on Frappe 16.55.2 and ERPNext 16.53.0 only | NOT TESTED (other versions) |
| 6.11 | App-specific CI: a bench in GitHub Actions running `bench run-tests`, and Semgrep | every command works locally (`run-tests`, `validate-dependencies`, Semgrep, bandit, pip-audit); **the generated `ci.yml` installs only Frappe, not ERPNext**, so an app that needs ERPNext must add `get-app` and `install-app` for it; the workflow itself was never run on GitHub | PARTLY |
| 6.12 | Listing assets | none made | NOT TESTED |

### Sections 8 to 12

| # | Plan says | On the bench | Verdict |
|---|---|---|---|
| 8.1 | The tools, agents, analysis and scoring are reusable as they are (no web, scheduler, HTTP or SQL library); the engine can be reused | the spike did not use the engine. Phase 0 measured that 36 of 111 modules, including the stock pack's tool registry, import SQLite ([FRAPPE_FACTS.md](FRAPPE_FACTS.md) 6.2) | NOT TESTED here |
| 10.1 | In a Frappe app every read is the signed-in user's, so a person who may not read Stock Ledger Entries gets less data | confirmed: the viewer reads nothing of Bin, Item or Stock Ledger Entry; the approver, with Stock User, reads all three | RIGHT |
| 10.2 | Frappe Cloud limits (no long-running threads, no custom ports, no local models) | not tried | NOT TESTED |
| 12.1 | "The spike is about 1 to 1.5 weeks" | about one working day of my time for what is described in section 3, with the dev environment and this report; about 70 minutes of it were on the bench (11:57 to about 13:08 UTC, from the first `up` to the last probe) | WRONG (too high) |

Two of the plan's own statements are not rows: section 9 (effort), answered in section 7 below, and the sentence in section 10 that the hooks and DocType details were unverified, which this table answers.

**Tally of the 46 rows above.** RIGHT 20 (1.2, 1.4, 1.5, 1.6, 2.1, 2.4, 2.5, 3.4, 3.10, 3.11, 3.12, 4.1, 4.2, 4.3, 5.1, 6.2, 6.3, 6.7, 6.8, 10.1; four of them with a trap or a limit). PARTLY 15 (1.1, 1.3, 2.2, 2.3, 3.2, 3.3, 3.5, 3.8, 3.9, 4.4, 6.1, 6.4, 6.6, 6.9, 6.11). WRONG 3 (3.1, 3.6, 12.1). NOT TESTED 8 (2.6, 3.7, 4.5, 6.5, 6.10, 6.12, 8.1, 10.2).

**Every wrong or incomplete claim, in one list:** 1.1 (reads as the signed-in user), 1.3 and 3.5 (the cadence), 2.2 (who may look), 2.3 (the approve permission and the hook), 3.1 (the licence string), 3.2 (the version declaration), 3.3 (before_install), 3.6 (an edited schedule), 3.8 and 3.9 (query conditions and Desk files, partly untried), 4.4 (the uninstall), 6.1 (the package), 6.4 (jobs), 6.6 (the ERP client), 6.9 (translations), 6.11 (CI), 12.1 (the spike's own estimate).

## 6. Surprises, in order of consequence

1. **A permission row grants every right you do not switch off.** `write`, `create`, `delete`, `share`, `print` and `email` default to 1 on `DocPerm`. A row made from `{"role": "Munimji Viewer", "read": 1}` produced a Viewer who could write and delete. It was found by reading the exported JSON, not by a test. Every right is now written out and a test pins the three roles. A real security finding for the app, and for the Semgrep-style checks to add in the port.
2. **`frappe.get_all` ignores permissions.** A viewer got all 3 `Bin` rows from it, and a `PermissionError` from `frappe.get_list`. The job uses `get_all` and is safe only because the scheduler runs it as Administrator. Everything the app reads for a signed-in user must use `get_list` or check permission, or the "as the signed-in user" rule is false for reads.
3. **Nothing scheduled runs more often than every 4 minutes.** A job set to every minute, to every 2 minutes and to every 5 minutes all ran once per scheduler tick: 12:52, 12:56 and 13:00 UTC, gaps of 193 to 246 s (worker log; `dev/scripts/cadence_report.py`). The documentation disagrees with itself (60 s on one page, 4 minutes on another); the code and the measurement say 240 s. The standalone service's 3-minute procurement pass becomes 4 minutes. Jobs run as `Administrator`.
4. **Frappe cannot uninstall everything an app adds.** Columns stay (by design, to protect data), a Permission Type cannot be deleted outside developer mode, install, migrate or tests, and the roles, custom fields and scheduled-job record stay unless the app removes them in `before_uninstall`. This is manageable and now tested, but "the uninstall removes what the install added" is true for records, not for columns.
5. **v16 has custom Permission Types** ("approve" is a column in the permission rows, granted in the Role Permission Manager). The plan had no such concept and invented a hook for it. The hook is still useful, to add the ERPNext condition, but it can only deny.
6. **The version log is off in every test** unless `save(ignore_version=False)`. Anything the app promises about its history needs a test that turns it on.
7. **INFO log lines are not written.** Frappe's default log level is WARNING on a dev server and ERROR elsewhere. The observability of the standalone service (task ids, events, metrics) does not carry over by copying: the log level, or another route, has to be decided.
8. **The template is not enough for Frappe Cloud**: no `[tool.bench.frappe-dependencies]`, the older apt table, a `ci.yml` that does not install ERPNext. `bench validate-dependencies` is a cheap CI step that catches a wrong range.
9. **ERPNext's test data costs 247 s once per test site.** After that, 20 tests run in 10 to 26 s. CI will pay it on every run unless it caches the database.
10. **The setup is much faster than assumed.** Bench, Frappe, ERPNext and a site from nothing: 618 s (about 10 minutes). The skeleton took 7 s, installing the app on a fresh site 5 to 7 s, uninstalling it 4 to 6 s.
11. **Windows' resolver does not resolve `*.localhost`**; browsers do. A default site makes plain `http://localhost:8000` work for any client.
12. **v16's Desk changed its markup** (`es-button`, `btn-modal-primary`): a browser test written for v15 selectors fails, and so will any custom script that relies on them.

## 7. Time, and a revised plan and estimate

### Time per step (first build)

| Step | Seconds | Note |
|---|---|---|
| docker compose up: pull and start | 453 | a first attempt had failed on a name lookup after 492 s |
| `bench init` (Frappe `version-16`) | 244 | |
| `bench get-app erpnext` (`version-16`) | 165 | |
| `bench new-site` | 63 | |
| `bench install-app erpnext` | 140 | |
| **Bench, ERPNext and a site, from nothing** | **618** | the five rows above without the pull |
| `bench new-app ai_munimji` | 7 | template, editable install and asset build |
| `bench get-app --soft-link` of the repository | 6 | |
| `bench install-app ai_munimji` | 4 | |
| roles, DocTypes and permission type (script) | 10 | |
| custom fields, `export-fixtures`, `migrate` | about 27 | |
| a second site with ERPNext and the app (test site) | 218 | new-site 58, ERPNext 153, app 7 |
| first `bench run-tests` | 247 | ERPNext test data built on first import |
| later `bench run-tests` (18 to 20 tests) | 10 to 26 | |
| uninstall check on a fresh site | 175 to 184 | new-site 45 to 49, ERPNext 98 to 110, app 5 to 6, uninstall 4 to 6 |
| seed (setup wizard, supplier, items, users) | 33 | |
| bandit and pip-audit (tools 17) | 1 and 12 to 15 | |
| Semgrep with Frappe's rules | 12 to 50 | |
| `npm install` of puppeteer-core | 90 | |
| browser round trip | 25 | |

### Time per step (rebuilt from nothing with `verify-all.ps1 -Fresh`, images already on the machine)

`.\dev\verify-all.ps1 -Fresh`, 5 Nov 2026: all 12 steps passed, **21.7 minutes** in all, including both round trips (12 checks over HTTP, 7 in the browser).

| Step | Seconds | Note |
|---|---|---|
| destroy the project | 0 | nothing to remove: the first attempt of this run (stopped at its first step by problem 13 in section 4) had already removed it |
| up (images on the machine) | 21 | |
| `bench init` | 170 | |
| `bench get-app erpnext` | 118 | |
| `bench new-site` | 66 | |
| `bench install-app erpnext` | 135 | |
| **Bench, ERPNext and a site, from nothing** | **492** | the four rows above and 3 s of configuration (about 8 minutes) |
| get-app and install-app of the app | 7 and 9 | |
| bootstrap, all of it | 517 | |
| seed | 47 | |
| test site (new-site 52, ERPNext 129, app 8) | 193 | |
| gate: tests, bandit, pip-audit | 295 | first `run-tests` 262 (ERPNext test data), tools 20, bandit 1, pip-audit 12 |
| bench start, until the site answers | 7 | |
| two requests waiting | 5 | |
| round trip over HTTP | 13 | |
| round trip in the browser (with a reset first) | 36 | |
| stop bench | 4 | |
| uninstall check on a fresh site | 164 | new-site 42, ERPNext 96, app 6, uninstall 6, install again 6 |

The first build (above) took 618 s for the same bench; the second took 492 s, with the images and the downloads cached.

### What the spike says about the plan's work packages

Your Phases 2 to 7 are not defined in anything I have, so I estimate by the plan's nine work packages (section 9 of `FRAPPE_APP_PLAN.md`), in working days for one developer who knows this code, working as I did here (me writing the code and tests, you reviewing and deciding; your reading and decision time is not counted). Plan days are its weeks at five a week.

| Package | Plan | Revised | Why |
|---|---|---|---|
| 1 Storage interface and repository clean-up in the standalone service | 10 to 15 | 12 to 18 | 36 modules import SQLite, more than the "dozen calls" the audit listed; needed by every engine option ([FRAPPE_FACTS.md](FRAPPE_FACTS.md) 6.2) |
| 2 App skeleton, settings, roles, install hooks | 10 | 3 to 5 | done in the spike in under a day: skeleton, roles, Settings DocType, custom fields, permission type, hooks; the Pack table and manifest sync are the rest |
| 3 `FrappeERPClient` | 10 to 15 | 6 to 9 | the two calls tried took hours with tests; the interface has about fifteen methods, and `get_list` against `get_all` has to be settled for each |
| 4 DocTypes, repositories, import of a SQLite backup | 10 to 15 | 8 to 12 | a DocType plus its JSON, permissions and tests took about an hour; 13 record types, the repositories and the import tool are the bulk |
| 5 Scheduler events and jobs (webhooks apart) | 7.5 to 10 | 4 to 6 | the job and its schedule took under an hour; the 4-minute tick is a design change, not work |
| 5b Webhooks with signature checks | (in 5) | 3 to 4 | not built; after version 0.1 |
| 6 Desk UI: inbox, stock monitor, vendor comparison, Ask, settings | 15 to 20 | 12 to 16 | a form with a button and a dialog took about an hour including the v16 selector surprise; the three large screens are pages and remain the largest and least known package |
| 7 Agents and MCP behind OAuth | 10 to 15 | 10 to 15 | not touched; not in version 0.1 (nothing leaves the site, no model) |
| 8 Tests, app CI on a bench, Semgrep, security checks | 7.5 to 10 | 4 to 6 | the test machinery works (section 4); GitHub Actions has not been run, and the register of security checks has to be rebuilt for whitelisted methods |
| 9 Listing, privacy and support pages, logo, video, private-bench trial | 10 to 15 | 8 to 12 | mostly not code; the Marketplace review (up to 10 days) is elapsed time and not counted |
| **Version 0.1** (1, 2, 3, 4, 5, 6, 8, 9) | | **57 to 84** | about 11 to 17 working weeks |
| **Everything** (adds 5b and 7) | 95 to 135 | **70 to 103** | about 14 to 21 working weeks |

If your Phases 2 to 7 are different from these packages, give me their definitions and I will redo the table. The estimate is mine, from what was measured and from reading the code; the two packages that it rests on least are 1 and 6.

## 8. Not tested

GitHub Actions (no repository, nothing pushed), Frappe Cloud (private bench, Audit Report), another Frappe or ERPNext version, the engine and the PyPI route, webhooks, agents and MCP, custom pages and dashboards, `frappe.enqueue`, `doc_events`, `permission_query_conditions`, translation extraction, fields on Material Request and Stock Entry, cancelling or changing an order made from a request, the decision-history seal, a SQLite import, the realtime service (the headless browser reported socket errors in the container; they did not affect the form), and any run on Linux or macOS.

## 9. Decisions for you

1. **Phases 2 to 7.** Give me their definitions, or accept the packages in section 7; then I plan Phase 2 in plan mode.
2. **Who may look at stock.** A Munimji Viewer cannot read Item, Bin or Stock Ledger Entry (2.2). My recommendation: keep the Munimji roles for the app's own DocTypes and require ERPNext's own roles (Stock User, Purchase User, Purchase Manager) for what ERPNext holds, documented in the install notes; do not ship permissions on ERPNext's DocTypes (that edges toward overriding base behaviour).
3. **Uninstall.** Accept that columns stay, and keep the flag around the Permission Type deletion? Or drop the custom Permission Type and use roles only? My recommendation: keep the Permission Type (it is the right tool) and the documented flag, and say on the uninstall page what stays.
4. **The 4-minute tick.** Accept it for the procurement pass and the stock check, and use a Scheduler Event-linked job for any schedule an administrator edits? My recommendation: yes.
5. **Reads for a signed-in user** use `get_list` or an explicit permission check; the job, which runs as Administrator, is the one place for `get_all`. A rule for the port, enforced by a test.
6. **Corrections.** Apply the corrections of `FRAPPE_FACTS.md` section 9 and the wrong rows of section 5 here to `FRAPPE_APP_PLAN.md` in the first commit of Phase 2?
7. **The git identity** of the AI-Munimji repository is the same as in this one, which includes your personal e-mail address in every commit. The repository is new, so changing it before a first push is easy; after, it is not. The publisher contact in `pyproject.toml` and `hooks.py` is the placeholder `contact@example.com`.

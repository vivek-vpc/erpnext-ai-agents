# Frappe facts: what was checked before building AI Munimji

Everything in this page was read or run on **5 Nov 2026**, for Phase 0 of AI Munimji, the Frappe app `ai_munimji`. It replaces guesses with checked facts. It does not change any code, and nothing here says the app exists, is on the Marketplace, or runs on any Frappe version: nothing of it has been built or run on a bench yet. [FRAPPE_APP_PLAN.md](FRAPPE_APP_PLAN.md) is the plan; where this page disagrees with it, section 9 lists the differences.

## 0. How to read this page

Each fact says where it came from. The words used for how sure it is:

| Word | Meaning |
|---|---|
| **Source** | read in the primary source: a file at a named commit, or the text of a documentation page |
| **Run** | I ran it here and quote the result |
| **Reported** | said by a search tool's summary or a forum post, not read at the source |
| **Inferred** | my reading of code or pages; the source does not say it in so many words |
| **Unknown** | I could not establish it; section 10 lists these |

Method. Files from GitHub were downloaded at a pinned commit and read in full, not summarised. The documentation pages were downloaded as their raw markdown (the pages I tried serve it when `.md` is appended to the address; the index is `https://docs.frappe.io/cloud/llms.txt`). For the three Marketplace pages I also compared the text of the HTML with a summary from the fetch tool, because that tool paraphrases. Scripts and downloads stayed outside the repository. After the page was written, a second script fetched 28 of its statements again at the pinned commits (versions, pins, the Frappe Cloud table, the scheduler tick, the Semgrep rule ids, the five links, the name searches) and all 28 matched.

Abbreviations for commits: `frappe@3d1284c` is `github.com/frappe/frappe` at `3d1284c5fdb52207a516a49bbf2fd186cb43b3ab` (head of `version-16`, 4 Nov 2026); `erpnext@34f6ff8` is `34f6ff862db319375d4bfad2e7e8ac06470bf1b5` (head of `version-16`, 4 Nov 2026); `press@bf5618b` is `github.com/frappe/press` at `bf5618b35ab067cc467df833e5f68e2e79fea3b5` (head of `develop`, 4 Nov 2026), the source of Frappe Cloud itself; `hrms@3b7f8f2` is the head of `version-16` of `github.com/frappe/hrms` on 4 Nov 2026.

## 1. The names (as given for this work)

The names come from the instruction for this work; they are recorded here so that they are written down once. The "Names table" that instruction refers to is not in `docs/FRAPPE_APP_PLAN.md`.

| What | Name |
|---|---|
| Frappe app (the `app_name` in `hooks.py`, the Python package) | `ai_munimji` |
| App title | AI Munimji |
| Settings DocType | AI Munimji Settings |
| DocTypes | prefixed `Munimji` |
| Roles | Munimji Viewer, Munimji Approver, Munimji Manager |
| Repository of the Frappe app | `AI-Munimji` |
| Engine (PyPI distribution) | `ai-munimji-engine` |
| Standalone service (unchanged) | `erpnext-ai-agents`, command `erp-agents`, Python package `erp_agents` |

Whether the names are free is section 7. The title and name pass Frappe's own checks. The pattern `APP_TITLE_PATTERN = ^(?![\W])[^\d_\s][\w -]+$` (`frappe@3d1284c:frappe/utils/boilerplate.py`, line 20) accepts "AI Munimji" (Run: the same expression in Python). `frappe.scrub`, which the template applies to the name (`frappe/__init__.py`: replace spaces and hyphens with underscores, lower-case), turns "AI Munimji" into `ai_munimji` (Source; read, not run).

## 2. Versions and what they need

### 2.1 Current releases

| | Latest release | Branch head | Source |
|---|---|---|---|
| Frappe v16 | v16.55.2, 4 Nov 2026 | `version-16` `3d1284c`, `__version__ = "16.55.2"` | GitHub releases API; `frappe/__init__.py` |
| ERPNext v16 | v16.53.0, 28 Oct 2026 | `version-16` `34f6ff8`, `__version__ = "16.53.0"` | GitHub releases API; `erpnext/__init__.py` |
| Frappe v15 | v15.126.0, 3 Nov 2026 | `version-15` `53bae3e` | GitHub releases API |
| ERPNext v15 | v15.124.5, 4 Nov 2026 | | GitHub releases API |
| Frappe and ERPNext `develop` | none | `__version__ = "17.0.0-dev"` for both | `frappe@358235e` (5 Nov 2026) |
| bench (CLI) | v5.31.1, 23 Oct 2026 | | GitHub releases API |

All Source. The head of ERPNext's `version-16` branch is ahead of its last release and already asks for `frappe >=16.55.0,<17.0.0`.

### 2.2 Python, Node, databases

| | Frappe v16 | ERPNext v16 | Frappe v15 | ERPNext v15 |
|---|---|---|---|---|
| `requires-python` | `>=3.14,<3.15` | `>=3.14` | `>=3.10,<3.15` | `>=3.10` |
| Frappe it needs | - | `frappe >=16.55.0,<17.0.0` (`[tool.bench.frappe-dependencies]`) | - | `frappe >=15.111.0,<16.0.0` |
| Build backend | `flit_core >=3.4,<4` | `flit_core >=3.4,<4` | | |

Sources: `pyproject.toml` of `frappe@3d1284c`, `erpnext@34f6ff8`, and the `version-15` branches of both (Source). `frappe develop` also requires `>=3.14,<3.15`.

Other requirements (Source: `docs.frappe.io/framework/user/en/installation`, updated 2026-10-29, table "Pre-requisites"; `package.json` of `frappe@3d1284c`; `.github/workflows/_base-server-tests.yml`):

| | Frappe v14/v15 | Frappe v16 / develop |
|---|---|---|
| MariaDB | 10.6.6+ | 11.8 |
| Python | 3.10+ | 3.14 |
| Node | 18+ | 24 (`package.json` `engines.node` is `>=24`) |
| Redis / valkey | 6 | 6+ |
| Yarn | 1.12+ | 1.22+ |
| pip | 20+ | 25.3+ |

Frappe's own server tests for v16 run on Python 3.14, Node 24, `mariadb:11.8` and `postgres:18.0` (`_base-server-tests.yml`); the app template (4.2) uses `mariadb:11.8` and `redis:alpine`. PostgreSQL is supported as an alternative to MariaDB (installation page). The documentation says v16 needs Python 3.14; the `pyproject.toml` says exactly 3.14.x and nothing newer. This confirms the earlier "community report" in `DEVELOPING.md`.

### 2.3 How Frappe Cloud pins them

Frappe Cloud's source holds a table of the versions it offers, with the Python, Node, bench and pip versions of each (`press@bf5618b:press/fixtures/frappe_version.json`; Source). It is a fixture file: the live dashboard (the "Dependencies" tab of a bench group) is what a customer actually gets, and may differ.

| Frappe Cloud "Frappe Version" | Status | Default for new bench groups | Python | Node | bench | pip | wkhtmltopdf |
|---|---|---|---|---|---|---|---|
| Version 16 | Stable | no | 3.14 | 24.22.1 | 5.31.0 | 26.2.2 | 0.12.5 |
| Version 15 | Stable | **yes** | 3.11 | 24.22.1 | 5.31.0 | 26.2.2 | 0.12.5 |
| Nightly (develop) | Develop | no | 3.14 | 24.22.1 | 5.27.0 | 26.2.2 | 0.12.5 |
| Versions 14, 13, 12 | End of Life | no | 3.10, 3.8, 3.7 | | | | |

- The dependency versions of a bench group can be edited by its owner (`docs.frappe.io/cloud/benches/editing-bench-dependency-version`, updated 2026-02-27); the page warns that an unsupported custom version can make later deploys fail. A patch in the same repository (`press/patches/v0_8_0/bump_v16_nightly_node_version_to_24_22_1.py`) moves existing Version 16 groups to Node 24.22.1 and pip 26.2.2.
- Frappe Cloud builds the bench image from a Dockerfile (`press@bf5618b:press/docker/Dockerfile`): Python from the deadsnakes package archive, Node with nvm, pip pinned, `frappe-bench==${BENCH_VERSION}`, then `bench init --frappe-path file://...` and, for every other app, `bench get-app file:///.../apps/<app>`. So an app's Python dependencies are installed by `bench get-app` (pip) into the bench's one shared environment.
- The MariaDB version on Frappe Cloud is not in the fixtures: Unknown (the installation page says 11.8 for v16).

### 2.4 Which version is "current stable"?

The Marketplace pages say an app must "support the current stable version of Frappe/ERPNext" and never give a number (Source, section 3). Frappe Cloud's own table lists **both Version 15 and Version 16 as Stable**, and Version 15 is the **default** for a new bench group (Source, 2.3). `develop` is 17.0.0-dev. So "current stable" is not defined by the pages; section 11, decision 3.

### 2.5 Developing on Windows

The installation page says only macOS and Debian/Ubuntu are officially supported; on Windows it suggests Ubuntu in WSL (Source). On the development machine used for this work (Windows 11): WSL 2 is present with an Ubuntu distribution (stopped) and a `docker-desktop` distribution (running); Docker 29.8.2 and Node 24.17.0 are installed; `uv`, which the installation page recommends for Python 3.14, is not (Run: `wsl --status`, `docker --version`, `node --version`). I started nothing.

## 3. The Marketplace requirements, and what changed

Pages read (raw markdown, front-matter dates): "Guidelines for publishing an app" (`.../cloud/marketplace/marketplace-guidelines`, updated 2026-02-16), "App authoring guidelines" (`.../app-authoring-guidelines`, 2026-02-16), "Publish an app on the Frappe Cloud Marketplace" (`.../publishing-an-app-to-marketplace`, 2026-10-24), "Versioning" (`.../cloud/custom-apps/app-versioning/versioning`, 2026-08-05), "Frappe listed as a Python dependency" (2026-09-03), "Installing App APT Dependencies" (2026-11-03), "Frappe Cloud Marketplace Overview" (2026-02-16). All Source.

"Then" below is section 7 of [FRAPPE_APP_PLAN.md](FRAPPE_APP_PLAN.md) and the list at the top of [DEVELOPING.md](DEVELOPING.md), written 21-25 Oct 2026.

| # | Requirement or fact (page) | Then |
|---|---|---|
| 1 | Open source: MIT, or a licence compatible with the GNU GPL (publishing) | same |
| 2 | The repository is on GitHub, **owned by you, and public**; the dashboard does not accept a private one (publishing) | "public" is new |
| 3 | The app name must be unique across the Marketplace even if the app is not a fork; the name is the `app_name` in `hooks.py` (guidelines; publishing) | same, plus where the name comes from |
| 4 | Works with the **current stable** Frappe, and ERPNext if it depends on it; no version number given (guidelines; authoring; publishing) | same |
| 5 | A **passing GitHub Actions (or similar) CI** for tests and linters is mandatory (authoring; publishing) | same |
| 6 | The app does not persuade users away from Frappe Cloud (guidelines) | same |
| 7 | The publisher gives **valid contact information** (guidelines) | new |
| 8 | A settings DocType: **"We recommend"** one for global behaviour (authoring) | was called a requirement: now a recommendation |
| 9 | Do not override base functionality such as `/login` and `/update`; extend through hooks (authoring) | same |
| 10 | Logo: at least 200 x 200 px, square with the logo in the centre, no text, shown in a circle (guidelines; publishing) | same |
| 11 | Summary: one sentence of 40 to 80 characters, not repeating the name or title, capitalise only proper nouns (guidelines; publishing) | same, plus the capitalisation rule |
| 12 | Long description: usage and features, not repeating the summary, **no installation steps**, screenshots go in the screenshots section (guidelines; publishing) | same |
| 13 | Title: up to 255 characters, may differ from the app name (guidelines; publishing) | new |
| 14 | One category (guidelines) | same |
| 15 | **Links, all five required** in the "Complete Listing" dialog: Website, Support, Documentation, Terms of Service, Privacy Policy (publishing) | changed: then only support URL and privacy policy URL |
| 16 | **Screenshots** (guidelines: "must have a logo, description and screenshots"; publishing: a Screenshots and Videos field) | new |
| 17 | A short demo video: the guidelines say "please prepare" one; the review team "can also ask you for" one (guidelines; publishing) | was a listing requirement: now prepared, on request |
| 18 | `pyproject.toml` at the repository root is **mandatory** on Frappe Cloud, with a `[tool.bench.frappe-dependencies]` section, for example `frappe = ">=16.0.0-dev,<=17.0.0"`; a branch that declares a different version from the bench is rejected (versioning) | new; then only "check the exact form" |
| 19 | `frappe` (or `erpnext`, `hrms`...) must **not** be listed under `[project] dependencies`: the deploy is blocked. Use the bench section instead (Frappe listed as a Python dependency) | new |
| 20 | System packages go in `[tool.frappe-cloud] apt-dependencies = [...]`; the older `[deploy.dependencies.apt]` table is still read (APT page). Frappe's own app template still writes the older table (`frappe@3d1284c`, `boilerplate.py` line 361) | new |
| 21 | Python dependencies are listed in `pyproject.toml` under `dependencies`, or in `requirements.txt`; **only one of the two files** may exist (Attention required in Custom Apps, 2026-06-18) | new |
| 22 | Process: become a Publisher (Settings, Profile); Marketplace, New App, "Add from GitHub", choose repository and **branch**; Frappe Cloud checks that it is a valid Frappe app and finds the compatible Frappe version; status Draft; "Complete Listing" has four steps (logo, links, description, **publish a release**); "Mark app ready for review" makes it In Review (publishing) | new detail |
| 23 | **Each push to the branch creates a new release**; a release is submitted from Versions, Show Releases, and then shows "Awaiting Approval" (publishing) | new; whether a person re-reviews each release is **not stated** |
| 24 | The team checks "security, code quality and these guidelines"; automated checks are on the **Audit Reports** tab, which includes a **Semgrep scan**; when it finds no serious gaps the app is ready for a personal review; review takes **up to 10 days** (publishing) | the 10 days is the same; the Audit Report is new |
| 25 | Paid apps: plans on the Pricing tab, payouts (publishing) | not applicable |

Not on the pages, although the earlier documents list it as something the Marketplace checks: **a privacy policy that says exactly what leaves the site**. The pages require a Privacy Policy URL only. The detail is this project's own policy (see [PRIVACY.md](PRIVACY.md)) and stays worth keeping, but it is not a Frappe rule.

## 4. Reference CI and linters

### 4.1 What the Marketplace points to

The authoring page says new apps usually come with the boilerplate, and links "this two workflows" as a reference: `.github/workflows/ci.yml` and `linter.yml` of `Arus-Info/ERPNext-Australian-Localisation` at commit `d178b87` (Source). Those files use Python 3.10, Node 18, `mariadb:10.6` and `actions/checkout@v3/v4`: they are older than the current boilerplate. They install ERPNext first (`bench get-app erpnext`, then `install-app erpnext`) before the app, which an app that depends on ERPNext needs; neither template names an ERPNext branch (**Inferred**: for v16 the command needs `--branch version-16`, otherwise the default branch is what gets built).

### 4.2 What Frappe's own app template writes now

`bench new-app` asks "Create GitHub Workflow action for unittests"; if yes it writes `.github/workflows/ci.yml`, `linter.yml` and `.pre-commit-config.yaml` (`frappe@3d1284c:frappe/utils/boilerplate.py`; Source). The CI workflow (`github_workflow_template`):

- triggers on push to the app's branch and on every pull request; one job, "Server", on `ubuntu-latest` with services `redis:alpine` twice (ports 13000 and 11000) and `mariadb:11.8`;
- steps: checkout; **"Find tests"** (`grep -rn "def test"`, so a repository with no test fails); Python 3.14; Node 24; pip and yarn caches; `sudo apt-get install mariadb-client`; `pip install frappe-bench`; `bench init --skip-redis-config-generation --skip-assets --python "$(which python)" ~/frappe-bench`; then in the bench: `bench get-app <app> $GITHUB_WORKSPACE`, `bench setup requirements --dev`, `bench new-site --db-root-password root --admin-password admin test_site`, `bench --site test_site install-app <app>`, `bench build` (with `CI=Yes`); then `bench --site test_site set-config allow_tests true` and `bench --site test_site run-tests --app <app>` (with `TYPE=server`).

The linter workflow (`linter_workflow_template`), on pull requests:

- job "Frappe Linter": checkout, Python 3.14, `pre-commit/action`, then `git clone --depth 1 https://github.com/frappe/semgrep-rules.git frappe-semgrep-rules`, `pip install semgrep` and `semgrep ci --config ./frappe-semgrep-rules/rules --config r/python.lang.correctness`;
- job "Vulnerable Dependency Check": Python 3.14, `pip install pip-audit`, `pip-audit --desc on .`.

The pre-commit template runs `pre-commit-hooks` (trailing whitespace, merge conflicts, AST, JSON/TOML/YAML checks, debug statements), `ruff` (import sort, lint, format), `prettier` and `eslint` on JavaScript.

### 4.3 Frappe's own linters.yml

`frappe@3d1284c:.github/workflows/linters.yml` adds a commit-message check (conventional commits), a "documentation required" check, the same Semgrep job, `pip-audit` with three vulnerabilities ignored by id, and pre-commit. The server tests of Frappe itself use `bench ... run-parallel-tests` (`_base-server-tests.yml`); the app template uses plain `run-tests --app`.

### 4.4 The Semgrep rules

`github.com/frappe/semgrep-rules` (licence MIT, default branch `develop`). Its head is `81a6e3d` of 30 Sep 2026, which is **the commit this project already pins** (nothing newer as of 5 Nov 2026; Run: GitHub compare API, 0 commits ahead). At that commit `rules/` holds 12 files and **50 rule ids** (Run: downloaded and listed). The ones a Frappe port of this project meets first:

| Area | Rule ids |
|---|---|
| Security | `guest-whitelisted-method`, `whitelisted-side-effect-on-get`, `missing-argument-type-hint`, `frappe-sql-format-injection`, `frappe-security-file-traversal`, `frappe-codeinjection-eval`, `frappe-ssti`, `frappe-format-string-injection`, `relaxed-permissions`, `frappe-setuser`, `unchecked-frappe-permission-call` |
| Correctness | `frappe-modifying-but-not-comitting`, `frappe-manual-commit`, `frappe-enqueue-without-after-commit`, `frappe-monkey-patching-not-allowed`, `frappe-single-value-type-safety`, `frappe-test-whitelist-missing-protection` |
| Hooks | `override-doctype-class` |
| Translation | eight `frappe-translation-*` rules and `frappe-missing-translate-function-python` / `-js`: user-visible text must go through `_()` |

`SECURITY_CHECKS.md` names `guest-whitelisted-method` and `frappe-sql-format-injection` in its Frappe column: both exist (confirmed). Run locally, as the Marketplace page says: `pip install semgrep`, clone the rules, then `semgrep --config ./frappe-semgrep-rules/rules --config r/python.lang.correctness .` from the app directory.

### 4.5 What this project's CI has, and lacks

It has (from [SECURITY_CHECKS.md](SECURITY_CHECKS.md) and the workflow): pytest on Python 3.10 to 3.14 on Ubuntu and Windows, bandit and pip-audit, a secrets scan, the Docker demo, the console DOM test, and a Semgrep job with these same rules (not a required check). It has **nothing that runs on a bench**: no `bench init`, no site, no `run-tests`. The app's repository needs the workflow of 4.2 (with ERPNext installed first) before the first release can be submitted.

## 5. Does this project fit?

### 5.1 Python

| | Python | Where |
|---|---|---|
| Frappe v16 / ERPNext v16 / Frappe Cloud Version 16 | exactly 3.14.x | 2.2, 2.3 |
| Frappe v15 / Frappe Cloud Version 15 | 3.10 to 3.14 / 3.11 | 2.2, 2.3 |
| This project | `requires-python >=3.10`, CI 3.10 to 3.14 | `pyproject.toml` |

A v16 app only ever runs on 3.14, and the Frappe template writes `requires-python = ">=3.14"` for the app. Code that the standalone service also uses has to keep working on 3.10, because that is the service's floor; so the engine, if it is shared, stays 3.10-compatible, while code that only the app uses may use 3.14. The app's CI needs 3.14 only; the standalone service already tests 3.14.

### 5.2 Dependencies against Frappe's pins (Run)

Method: a throwaway virtualenv on Python 3.14.6 (Windows); this project's `dependencies` and the `agents` and `otel` extras; Frappe's v16 `[project] dependencies` as a pip constraints file (83 entries; the two git-URL entries, `PyPika` and `gunicorn`, and all extras removed, because pip does not allow them in a constraints file); `pip install --dry-run`, nothing installed. Eight resolutions (four requirement sets, with and without Frappe's pins) all **succeeded**.

| Set | Packages without pins | With Frappe's pins | What the pins change |
|---|---|---|---|
| base | 22 | 23 | pydantic 2.14.3 to **2.12.5**, requests 2.34.2 to **2.33.1**, click 8.5.0 to 8.4.2 |
| base + agents (MCP) | 39 | 40 | the same three |
| base + agents + otel | 48 | 49 | the same three |
| pydantic, PyYAML, requests only | 11 | 11 | |

| Package | This project | Frappe v16 | Note |
|---|---|---|---|
| pydantic | `>=2.5` | `~=2.12.5` | fits; the pin is older than what is used today |
| PyYAML | `>=6.0` | `~=6.0.3` | fits |
| requests | `>=2.31` | `~=2.33.0` | fits (urllib3 and certifi come with it) |
| uvicorn | `>=0.27` | `~=0.54.0` | fits, but the app does not need it |
| httpx (dev) | `>=0.27` | `~=0.28.1` | fits |
| tzdata | Windows only | `~=2026.5` | fits |
| fastapi, starlette | `>=0.110` | not provided | **must not** be installed by the app: they are for the standalone service's web edge |
| apscheduler | `>=3.10,<4` | not provided | **must not**: Frappe has its own scheduler |
| mcp | `>=2.3,<3` | not provided | brings `httpx2`, `httpcore2`, `mcp-types`, `sse-starlette`, `python-multipart`, `PyJWT` (Frappe pins 2.15.x, resolved to 2.15.1) and `cryptography` (Frappe pins 50.0.x, resolved to 50.0.2); resolves, but is not needed in 0.1 |
| jsonschema | `>=4.20` (agents) | not provided | only for the MCP and agent paths |
| opentelemetry-* | `>=1.30,<2` (otel) | not provided | optional |
| anyio | **not declared** | not provided by name | imported at the top of `core/agents/runtime.py`, `core/mcp_server.py` and (inside a function) `core/cli.py`; it arrives through starlette or httpx. A shared engine must declare it or stop importing it |

On Linux `pywin32` (pulled in on Windows by `mcp`) would not appear.

### 5.3 The test suite on Frappe's pinned versions (Run)

A second throwaway environment on Python 3.14.6 with this project's requirements plus pytest, installed **under Frappe's constraints**: pydantic 2.12.5, requests 2.33.1, click 8.4.2, httpx 0.28.1, uvicorn 0.54.0, PyYAML 6.0.3 (and fastapi 0.162.0, starlette 1.10.0, mcp 2.3.0, APScheduler 3.11.3 unconstrained). The whole suite was run from the repository at `main` (`bf62394`):

- First run: **2181 passed, 1 skipped, 1 failed** in 326 s. The failure was `test_the_toolkit_works_without_the_optional_mcp_package`, which starts `python -I`; isolated mode ignores `PYTHONPATH`, and I had not installed the package into the throwaway environment. That is a mistake in my set-up, not a finding.
- After installing the package into that environment from a copy of the source (`pip install --no-deps`), the file of that test (`test_toolkit.py`) gave **15 passed**. I did not run the whole suite a second time.

So with the versions Frappe would force, the tests give the same result as on the newest versions: the 2182 passed and 1 skipped of `main`, with that one test run separately. No test in the suite needs pydantic 2.13 or later, or requests 2.34 or later. This says nothing about a bench: no Frappe code was involved.

### 5.4 What must change (a list, not done)

1. **A new distribution for the engine** whose hard dependencies are only what Frappe already provides or tolerates (pydantic, PyYAML; `requests` only if an HTTP edge stays in it), with loose lower bounds and no upper caps. Everything shared with a bench shares one environment ("Each bench maintains its own set of Python dependencies": Frappe Cloud, debugging page).
2. **Nothing in the engine may import** `fastapi`, `starlette`, `apscheduler`, `sqlite3` or `mcp` at import time (section 6.2 measures how far that is true).
3. **Declare `anyio`** or remove the import from the runtime.
4. **The app's `pyproject.toml`**: `requires-python >=3.14`, `[tool.bench.frappe-dependencies]` with `frappe` (and `erpnext`, as HRMS does) and no `frappe` under `[project] dependencies`.
5. **Do not change the standalone service's Python range** because of this: the engine can keep 3.10 to 3.14 while the app uses 3.14.

## 6. Reusing the engine

### 6.1 How a Frappe app's Python dependencies are installed

- Frappe Cloud runs `bench get-app file:///.../<app>` for each app (2.3), which installs the app and its `pyproject.toml` dependencies with pip into the bench's environment (Source: the Dockerfile; **Inferred** for the exact pip options, which are bench's).
- The Cloud documentation says to list Python packages under `dependencies` in `pyproject.toml` or in `requirements.txt`, only one of the two (Source, "Attention required in Custom Apps"). It shows PyPI packages with `~=` and `==` pins as examples.
- **Git URLs**: Frappe's own `pyproject.toml` has two (`PyPika @ git+https://github.com/frappe/pypika@<sha>` and `gunicorn @ git+https://github.com/frappe/gunicorn@<sha>`), so bench and pip handle them. The Frappe Cloud pages do not say whether an app on the Marketplace may use one: Unknown.
- **A private package index**: not mentioned anywhere I read: Unknown. Do not rely on one.
- System packages: `[tool.frappe-cloud] apt-dependencies` (section 3, row 20).
- Frappe is installed from git, never from PyPI, and an app that lists it as a dependency is blocked (section 3, row 19).

### 6.2 What the engine really needs (Run)

For each of the 111 modules of `erp_agents` (not counting tests) I started a fresh interpreter in isolated mode with a finder that refuses certain top-level names, and imported the module. Run A blocks what a Frappe app would not have: `fastapi`, `starlette`, `apscheduler`, `sqlite3`, `mcp`. Run B also blocks the network libraries (`requests`, `httpx`, `jsonschema`, `certifi`, `urllib3`), which shows the modules that are pure logic.

| Run | Imports cleanly | Fails, by the first blocked name reached |
|---|---|---|
| A (what a bench lacks) | **68 of 111** | `sqlite3` 36, `mcp` 3, `apscheduler` 2, `fastapi` 2 |
| B (pure logic) | **55 of 111** | the 13 more modules that need `requests` |

Imports cleanly in run B (the candidate engine for version 0.1, which sends nothing and calls no model): in `core`: `acting`, `agents.usage`, `api.hardening`, `api.observability`, `approvals.models`, `approvals.render`, `approvals.router`, `approvals.channels.base`, `approvals.channels.simple`, `assistant.facts`, `assistant.verify`, `auth.access`, `common`, `config`, `erp.base`, `erp.mock`, `metrics`, `netguard`, `obs`, `packs`, `policy`, `prompting`, `resilience`, `toolkit`, `tracing`; in `packs.stock_procurement`: `inventory.analysis`, `inventory.events`, `inventory.forecast`, `inventory.messages`, `inventory.substitutes`, `procurement.scoring`, `procurement.taxes`, `procurement.messages`, `followup.messages`, `followup.scorecard`, `followup.vendors`, `settings`, `templating`.

Only in run A (they need `requests`, which Frappe provides): `core.agents.llm`, `.privacy`, `.runtime`, `.service`, `core.approvals.channels.registry/telegram/whatsapp`, `core.assistant.llm`, `core.auth.erpnext/sessions`, `core.erp.erpnext`, `core.httpguard`, `packs.stock_procurement.agents.inventory_agent`.

Fail on `sqlite3` (36): in `core`, `agents.store`, `approvals.inbound`, `approvals.inbound_ledger`, `approvals.service`, `audit`, `bootstrap`, `cli`, `doctor`, `health`, `history`, `jobs`, `ops`, `settings_store`, `storage`, `workorders`; in the stock pack, `agents.purchase_agent`, `agents.tasks`, `assistant.chat`, `assistant.conversation`, `assistant.service`, `cli`, `followup.deliveries/quotes/receipts/service`, `inventory.agent/alerts/counts/notices/slow_stock/transfers`, `pack`, `procurement.agent/proposals`, `schema`, **`tools`**.

What this shows against the earlier documents (section 9): [DEVELOPING.md](DEVELOPING.md) lists 14 modules as pure logic; 12 import cleanly in run A and two do not, `assistant/chat.py` (it imports the database and settings store directly) and `agents/purchase_agent.py`. [AGENTS.md](AGENTS.md) says the stock pack's `tools.py` has "no web, HTTP or SQL imports, so they move into a Frappe app unchanged"; it imports `.inventory.agent`, which reaches `sqlite3`, so **the tool registry of the stock pack does not import without SQLite today**. That is the storage-interface work (package 1) showing up in a measurement, and it is why every option below needs it.

### 6.3 The options

| | (a) Publish `ai-munimji-engine` to PyPI; the app depends on it | (b) Vendor the pure-logic modules into the app repository | (c) A git-URL dependency: `ai-munimji-engine @ git+https://github.com/...@<tag>` |
|---|---|---|---|
| **Drift** | lowest: one source, the app pins an exact version | highest: two copies of the same code (the standalone service's and the app's) kept equal by a sync script and a CI check; a fix is made in one place and copied | low: one source, pinned to a tag |
| **Import paths** | unchanged | the package has to live inside the app's one package (flit builds one top-level package), so imports have to be rewritten by the sync step | unchanged |
| **Marketplace review risk** | the app repository is not the code that runs: the reviewers and the Audit Report (Semgrep) see only the app repository. Whether that matters to them is not documented: **Unknown** | none: everything that runs is in the reviewed repository | as (a), and also not documented as accepted for Marketplace apps: **Unknown** |
| **Supply chain** | a PyPI account compromise would run code on every customer's bench; reduced by two-factor authentication, trusted publishing from GitHub Actions, and exact pins | only the GitHub repository | the GitHub repository; needs git and network at build time (Frappe does this itself) |
| **Release process** | engine tag, then PyPI release, then the app bumps its pin and pushes (each push to the branch creates a Marketplace release, row 23) | one release | engine tag, then the app bumps its pin; no PyPI account |
| **Effort** | the repository split and a publishing workflow; package 1 (storage interface, threads out) is needed anyway | package 1, plus the sync script and the import rewrite | as (a) without the publishing workflow |
| **Name** | `ai-munimji-engine` is free on PyPI (section 7) | not needed | not needed |

Every option needs package 1 first: 36 modules, among them the tool registry, cannot be imported without SQLite today (6.2). Every option also needs the engine's dependencies kept minimal (5.4), because the bench environment is shared with the other apps of the customer.

**Recommendation: (a)**, with these conditions: the engine is its own distribution with only pydantic and PyYAML as hard dependencies; a test in this repository fails if a pure-logic module imports a blocked name (the script of 6.2 becomes that test); the app pins the exact engine version; PyPI releases come from GitHub with trusted publishing; and the one **Unknown** that decides between (a) and (b), whether a Marketplace app may depend on its own PyPI package, is tested early, in the private-bench trial and by asking Frappe Cloud support, before the app depends on it for real. If the answer is no, (b) is the fallback and the sync-and-rewrite step has to be built.

## 7. The name

Searches run on 5 Nov 2026. "Free" means no hit in the source named.

| Where | Query | Result | How |
|---|---|---|---|
| PyPI | `ai-munimji-engine`, `ai_munimji_engine`, `ai-munimji`, `ai_munimji`, `aimunimji`, `munimji`, `munimji-erp`, `munimji-engine`, `ai-munimji-app`, `munimji-ai` | **all free** (HTTP 404 on `pypi.org/pypi/<name>/json`; the `simple` index also 404 for three of them) | Run. The PyPI web search page answered with a JavaScript challenge, so I used the API |
| PyPI | `munim` | exists: version 0.8.2, "One MCP server holding a live session with every client's account at once", by an individual; unrelated, a close variant | Run |
| Frappe Cloud Marketplace | `ai_munimji`, anything containing "munim" or "munshi" | **no hit** among the 388 apps returned by `cloud.frappe.io/api/method/press.api.marketplace.get_marketplace_apps` | Run. The Marketplace web pages answer 403 to a script; the list may not include unpublished drafts |
| GitHub accounts | `AI-Munimji`, `ai-munimji`, `ai_munimji` | **free** as user or organisation names (404). `munimji` is taken: a user account created 2016-06-27 with no repositories. Also taken: `munimjicodebase`, `Munimjit`, `Munimji23`, `Munimjijobs`, `munimjirajasthan-stack`, `munimji2468`, `munimjicityscapegroup`, `MUNIMJI-ERP` | Run |
| GitHub repositories | `AI-Munimji` | one repository of that exact name under another user's account (`vishal-akbari-scaletech/AI-Munimji`, last pushed 13 May 2025, no description, 0 stars). Three more with "Munimji.ai" or "MunimJi.AI" in the name (2025-2026). 48 repositories contain "munimji" in all (among them a personal-finance project, a 2011 financial-services project and one whose description explains the Hindi word for a bookkeeper) | Run. Repository search only; code search needs sign-in |
| Web | "Munimji ERP" | **Munimji ERP**, a commercial garment-industry ERP of Dhona Compusoft Consultant, Kolkata; the vendor's directory page says it has served the garment industry "since 1991"; software directories list it (price on request or from a one-time figure); one listing says desktop on Windows. It is a different product from an ERPNext add-on, in the same field (ERP) | Reported (search summary) and one page read (a trade directory) |
| Web | "Munimji" with AI, accounting | **Munimji - AI Invoice Maker**, an iOS app from pixel force private limited (App Store page read: free with purchases, an AI invoicing and billing assistant). **Munimji Smart AI Expense Tracker**, a Telegram assistant (Product Hunt, 2025). "Munim AI", an AI accountant. The 1955 Hindi film Munimji | Reported, App Store page read |
| Web | "AI Munimji" (exact) | nothing found | Reported |

What it means: `ai_munimji` is free as an app name (as far as the Marketplace list shows), as a PyPI name, and `ai-munimji-engine` is free; `AI-Munimji` is free as a GitHub organisation or user name, and a repository of that name exists elsewhere (it does not block yours). There is **no hard clash** in any technical namespace. The overlap is commercial: "Munimji ERP" is an ERP product with the same word, and the word is also in use by AI accounting and invoicing apps. Whether that is acceptable is a brand and trademark question this page cannot settle: no trademark register was searched (they need a browser and, for a verdict, a lawyer): **Unknown**.

## 8. The hooks and commands of the plan, checked

Each item of [FRAPPE_APP_PLAN.md](FRAPPE_APP_PLAN.md) sections 3 and 4 that names a Frappe feature, checked against the `version-16` source or the documentation.

| Plan says | Checked | Verdict |
|---|---|---|
| `app_name`, `app_title`, `app_publisher`, `app_description`, `app_email`, `app_license` | all in the template `hooks.py` (`frappe@3d1284c`). The licence prompt offers the lower-cased SPDX ids of GitHub's licence list (`gpl-3.0`, `agpl-3.0`, `mit`...), default `mit`; ERPNext writes `"GNU General Public License (v3)"`. Whether the Marketplace reads the string: Unknown | yes; the exact string is free text |
| `required_apps = ["erpnext"]` | in the template (commented); Frappe Cloud also reads `[tool.bench.frappe-dependencies]` (HRMS lists `frappe` and `erpnext` there, both `>=16.50.0,<17.0.0`, while its `requires-python` is `>=3.10`: `hrms@3b7f8f2:pyproject.toml`; "Required app not found" page) | yes, and add the `pyproject.toml` form |
| `after_install`, `before_uninstall`, `after_migrate` | documented with `before_install`, `after_sync`, `after_uninstall`, `before_migrate` (`docs.frappe.io/framework/user/en/python-api/hooks`, updated 2026-09-17) | yes |
| `fixtures` for custom fields | documented (`fixtures = ["Custom Field"]`, or `{"dt": ..., "filters": ...}`, exported with `bench --site <site> export-fixtures`; the guide "Create Custom Fields During App Installation") | yes; filter to this app's fields |
| `scheduler_events` with `cron` | keys `hourly`, `daily`, `weekly`, `monthly`, their `_long` forms, `all`, `cron`; changes need `bench migrate` (hooks and background-jobs pages; `frappe/hooks.py` uses `cron`) | yes, with the next two rows |
| the standalone's 3-minute pass can be a scheduled event | the `all` event and every cron job run at the **scheduler tick**, **240 seconds by default** (`frappe/utils/scheduler.py`: `DEFAULT_SCHEDULER_TICK = 4 * 60`; `scheduled_job_type.py`: a job is due when its next run is before now, and "All" is `*/4`). The documentation disagrees with the code: the hooks page says every 60 seconds, the jobs page says 4 minutes. The source itself uses two setting names for it: `scheduler_tick_interval` (`scheduler.py`) and `scheduler_interval` (`scheduled_job_type.py`) | **no**: not finer than the tick (**Inferred** for cron finer than the tick) |
| "a schedule an administrator edits cannot live in hooks.py, so a frequent tick job asks the settings" | the framework documents "Configurable Scheduler Events": a `Scheduler Event` plus a `Scheduled Job Type` whose cron string can be changed later and survives migration | a better way exists |
| jobs and users | "the jobs triggered by scheduler are run by **Administrator**" (background-jobs page); `frappe.enqueue(method, queue=short/default/long, timeout, is_async, now, job_name, enqueue_after_commit, at_front, **kwargs)`; defaults 300, 300 and 1500 seconds; custom queues on Frappe Cloud need a private bench group and a config key (Custom Background Workers, 2026-09-29) | yes; a job has no signed-in user, so "created under the approver" must be passed in explicitly |
| `doc_events`, `has_permission`, `permission_query_conditions` | documented, with examples | yes |
| `app_include_js`, workspace and page files | `app_include_js` in the template; the navigation model changed in v16 (sidebars per module, `add_to_apps_screen`: `hooks.md`) | yes, but read the v16 navigation page before the Desk package |
| `boot_session` | **both names are hooks in v16.** `frappe/boot.py` line 102 still reads `hooks.boot_session`; the framework's own `hooks.py` (line 508) uses `extend_bootinfo`, which is the one the hooks page documents; ERPNext uses both (`boot_session` at line 101 of its `hooks.py`, `extend_bootinfo` at line 742). (Corrected on 5 Nov 2026: this page first called `boot_session` a wrong name, which was an error of mine, found while preparing Phase 1.) | right; use the documented `extend_bootinfo` |
| `@frappe.whitelist`, `allow_guest=True` only for the two webhooks | Frappe's Semgrep rule `guest-whitelisted-method` flags other guest methods; `whitelisted-side-effect-on-get` flags writes in methods that accept GET | yes |
| `patches.txt` "run once each" | yes, plus two sections `[pre_model_sync]` and `[post_model_sync]` (v14 and later), one-line `execute:` statements, and a trailing comment to run a patch again | yes |
| Settings DocType (Single) with Password fields | "Single DocType" is documented; the `Password` field type exists ("can be used to store sensitive data like ... secret keys"); how it is stored and read back was not checked | partly: storage of Password values: Unknown |
| `bench get-app`, `install-app`, `new-site`, `migrate`, `build`, `start`, `run-tests --app`, `uninstall-app` | used in the template workflow or the tutorial (`bench start`), and `uninstall-app` is documented: it removes the app's doctypes and modules, takes a backup unless `--no-backup`, and has `--dry-run`, `--force`, `--yes` | yes |
| the version log of Single DocTypes records changes without Password values | not checked | Unknown |

## 9. Corrections to earlier documents

Nothing was edited in this phase. These are the differences between earlier text and what was read, to be corrected in the next phase that touches those files.

1. **FRAPPE_APP_PLAN.md section 3**: nothing to correct for `boot_session`: it is still read by `frappe/boot.py`; the documented name is `extend_bootinfo` and is the better one to use (8; an earlier version of this page wrongly called `boot_session` a wrong name).
2. **Section 3, scheduler row, and section 6, background jobs**: nothing runs finer than the tick (240 seconds by default); the "3-minute procurement pass" becomes 4 minutes or a 5-minute cron. A schedule an administrator edits can be a `Scheduled Job Type` (8).
3. **Section 6 "Current stable Frappe and ERPNext support"**: v16 needs Python exactly 3.14.x (2.2); the plan said it "must be checked".
4. **Section 6, Settings DocTypes and DEVELOPING.md "What the Marketplace requires"**: a settings DocType is a recommendation, not a requirement (3, row 8).
5. **Section 6 and 7, listing assets**: five links (Website, Support, Documentation, Terms of Service, Privacy Policy) and screenshots are required; the demo video is prepared and sent on request (3, rows 15 to 17).
6. **Section 6, bench app package, and section 3, `pyproject.toml`**: the file is mandatory on Frappe Cloud, with `[tool.bench.frappe-dependencies]`; `frappe` must not be in `dependencies`; apt packages go in `[tool.frappe-cloud]` (3, rows 18 to 21).
7. **Section 7 and DEVELOPING.md**: a privacy policy "that says exactly what leaves the site" is this project's own rule, not on the Marketplace pages (3, last paragraph). Each push creates a release and the Audit Report runs Semgrep (rows 23, 24).
8. **DEVELOPING.md "Pure logic" table and AGENTS.md ("no web, HTTP or SQL imports")**: `assistant/chat.py`, `agents/purchase_agent.py` and the stock pack's `tools.py` import SQLite at import time; `core/agents/runtime.py` and `privacy.py` reach `requests` through `llm.py`, which is harmless in a bench because Frappe provides `requests`, but is not "no HTTP library" (6.2).
9. **FRAPPE_APP_PLAN.md section 2 and 12**: Frappe Cloud lists v15 and v16 as Stable, with v15 the default for new bench groups (2.3, 2.4); the plan assumed one current stable version.

## 10. Not verified, and why

- Whether the Marketplace review accepts an app that depends on its own PyPI package, or on a git URL (6.1, 6.3): not documented.
- Whether a private package index is available on Frappe Cloud: not documented.
- Whether an app in a **subdirectory** of a repository can be added from GitHub: the pages speak of "the repository and the branch" and of checking that it "is a valid Frappe app"; a template app keeps `pyproject.toml` and `hooks.py` at the root. Not documented either way.
- Whether a person reviews every release, or only the first one: the pages say a release must be submitted and shows "Awaiting Approval", and that the review "covers" the app; not more.
- Which version the Marketplace means by "current stable" (2.4), and what Frappe Cloud serves today to a new customer: the fixture file is the source repository's, the dashboard is authoritative.
- The MariaDB version on Frappe Cloud (2.3).
- Whether trademark or other rights stand in the way of "Munimji" or "AI Munimji" (7): no register searched; the Marketplace list may not show draft apps.
- How the `Password` field type stores values, and whether the version log leaves them out (8).
- Behaviour of a cron job finer than the scheduler tick (8; reading of the code only).
- The Linux resolution of the dependencies (5.2 was run on Windows) and Python 3.10 to 3.13 (not relevant to a v16 app).
- Everything that needs a bench: no bench was installed or started in this phase.

## 11. Decisions for the owner

1. **Is the name overlap with "Munimji ERP" acceptable?** Recommendation: yes for the engineering work, since there is no clash in any technical namespace (7). Before the public listing: look the name up in the trademark registers that matter to you (India first, since Munimji ERP is Indian; I cannot search them for you); always write the name as "AI Munimji for ERPNext"; say in the README and the listing that it is not affiliated with Munimji ERP. I propose no other name.
2. **Engine reuse: (a), (b) or (c)?** Recommendation: **(a), a PyPI package `ai-munimji-engine`**, with the conditions at the end of 6.3 and the early check of the one unknown; (b) is the fallback.
3. **Target Frappe and ERPNext versions.** Recommendation: **Frappe v16 and ERPNext v16 only** for version 0.1, declared as `frappe` and `erpnext` in `[tool.bench.frappe-dependencies]` with the upper bound `<17.0.0` and a lower bound set to the oldest v16 release that CI has actually run on (until then, the current `>=16.55.0` that ERPNext's own branch asks for). Reasons: 3.14 and Node 24 are the stack of the whole plan and of every live test so far; v15 would add Python 3.11 and a second set of tests. The cost to weigh: new Frappe Cloud bench groups still default to Version 15 (2.3), so a v16-only app is installable only on a v16 bench group.
4. **One repository or two?** Recommendation: **two**: `AI-Munimji` for the Frappe app (public, owned by the publisher, `pyproject.toml` and `hooks.py` at the root, bench-based CI), and `erpnext-ai-agents` for the standalone service together with the engine, which is published from it as `ai-munimji-engine`. The reasons: the Marketplace adds a repository and a branch, and the files it looks for sit at the root; the standalone service also has a `pyproject.toml` at its root; each push to the app's branch creates a release. A third repository only if the engine gets other users. How the engine is carved out of `erpnext-ai-agents` (a second distribution in the same repository, and what happens to the `erp_agents` import names) is the first question of Phase 1, not decided here.

Smaller things for you to rule on, which I will otherwise assume as stated:

5. The engine keeps **Python 3.10 to 3.14**; the app is **3.14 only**.
6. The corrections in section 9 are made in the first commit of the next phase, not now.
7. Development bench: **Docker (a dev container) rather than WSL**, since Docker Desktop is already running here and Frappe Cloud itself builds from a container; or WSL 2 Ubuntu with `uv` if you prefer the documented route. I have started neither.

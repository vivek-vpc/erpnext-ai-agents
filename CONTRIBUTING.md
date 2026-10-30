# Contributing

Thanks for helping. This project is open source under GPL-3.0-or-later and is being prepared for a later Frappe/ERPNext app version, so a few design rules matter more than usual.

## Set up and run the tests

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1      Linux/macOS: source .venv/bin/activate
pip install -e ".[dev,agents]"   # "agents" adds the MCP SDK for the AI agents and the MCP server; without it those tests are skipped
python -m pytest                 # runs against fakes and a demo ERP: no ERPNext, no accounts, no language model needed
erp-agents demo                  # a quick look at what it does
```

Live checks against a real ERPNext (`python -m tools.e2e.run_live`) are optional and need your own test instance; they create and delete a few draft documents.

The AI path has its own evaluation, which also runs in `pytest`: `python -m tools.evals --check` replays 122 golden questions with a scripted stand-in for the language model and fails when a check no longer holds ([docs/EVALUATION.md](docs/EVALUATION.md)). To run the same questions on a real model and compare two: `python -m tools.e2e.agents_eval --suite golden --against ...`.

## Rules for changes

1. **Tests with every change.** A bug fix starts with a test that fails (for a wrong answer from the assistant, a case in `tests/evals/cases/`: see [docs/EVALUATION.md](docs/EVALUATION.md)); a feature comes with tests for the normal case, the refusal cases and the "nothing happens" cases. CI must pass on Linux and Windows.
2. **The assistant never decides alone.** Anything that orders, approves or changes the ERP must go through an approval request and be created as a draft with the approver recorded. Numbers in messages and answers come from code and ERP data, never from a language model.
3. **Keep the edges thin** (see [docs/DEVELOPING.md](docs/DEVELOPING.md)). Business logic must not import web, scheduler, HTTP or SQL libraries, and SQL lives only in repository classes. This is what makes the later Frappe app a swap of the edges instead of a rewrite.
4. **No secrets, no personal data** in code, tests, docs or logs. Use `agent@example.com`-style placeholders. Test data is fictional.
5. **AI agents are fenced, not trusted.** An agent may use only the tools in `core/toolkit.py` (the same ones the MCP server publishes); no tool orders, approves, pays or changes a setting; what a tool returns carries no customer or staff personal data (`src/erp_agents/packs/stock_procurement/tests/test_toolkit.py` checks this); every number an agent writes is checked against the tools' results; and the deterministic result is always the fallback. See [docs/AGENTS.md](docs/AGENTS.md) and [docs/PRIVACY.md](docs/PRIVACY.md).
6. **User-facing text** is plain English written for a small-business owner, built in one place (the message modules), not inside routes.
7. **Run the security checks** before you push: `python -m bandit -r src -q -x "src/erp_agents/packs/*/tests/*"` must report no issue (a justified exception is a `# reason  # nosec Bxxx` comment on that line) and `python -m pip_audit` must report no known vulnerability. A security fix comes with a regression test ([docs/DEVELOPING.md](docs/DEVELOPING.md)).
8. Say honestly in the pull request what you tested and what you could not.

## Pull requests

Small, focused changes are easiest to review. Describe the problem, the change, and how you checked it. By contributing you agree that your work is released under the project's licence.

## Reporting problems

Bugs and ideas: open an issue. Security problems: see [SECURITY.md](SECURITY.md); do not open a public issue.

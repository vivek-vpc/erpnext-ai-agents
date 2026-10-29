"""docs/RESILIENCE.md says what the code does (Phase 3 of the hardening brief), and every statement in it names the test that proves it. The tables are compared with the code, every
test named in the document must exist (so renaming or deleting one is noticed), the example settings must load, and the Frappe table must name modules and functions that exist."""
import ast
import inspect
import re
from pathlib import Path

import pytest
import yaml

from erp_agents.core import metrics, resilience
from erp_agents.core.erp.erpnext import ERPNextClient
from erp_agents.core.resilience import Limits, ResilienceSettings

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "erp_agents"
DOC = ROOT / "docs" / "RESILIENCE.md"
TEXT = DOC.read_text(encoding="utf-8")


def table(name):
    found = re.search(rf"<!-- {name} -->(.*?)<!-- /{name} -->", TEXT, re.S)
    assert found, f"{DOC.name} has no <!-- {name} --> table"
    lines = [l for l in found.group(1).splitlines() if l.strip().startswith("|")]
    return [[c.strip() for c in l.strip().strip("|").split("|")] for l in lines[2:]]


def names_in(cell):
    return re.findall(r"`([^`]+)`", cell)


def all_test_names():
    names = set()
    for path in [*(ROOT / "tests").glob("test_*.py"), *(SRC / "packs").glob("*/tests/test_*.py")]:
        names |= {n.name for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}
    return names


def test_every_test_the_document_names_exists():
    cited = set(re.findall(r"`(test_[a-z0-9_]+)`", TEXT))
    assert len(cited) >= 50, "the document should name the tests that prove it"
    missing = sorted(cited - all_test_names())
    assert not missing, f"RESILIENCE.md names tests that do not exist: {missing}"


# ------------------------------------------------------------------ the settings and the limits
def shown(value):
    return f"{value:g}" if isinstance(value, float) else str(value)


def test_the_settings_table_matches_the_settings_and_their_defaults():
    defaults = ResilienceSettings()
    rows = {names_in(r[0])[0]: names_in(r[1])[0] for r in table("resilience:settings")}
    assert set(rows) == set(ResilienceSettings.__dataclass_fields__)
    for name, text in rows.items():
        assert text == shown(getattr(defaults, name)), name


def test_the_limits_table_matches_the_limits():
    in_code = Limits(ResilienceSettings()).per_minute
    documented = {names_in(r[0])[0]: int(r[1]) for r in table("resilience:limits")}
    assert documented == in_code


def test_the_example_settings_file_shows_every_setting_and_loads():
    lines = (SRC / "settings.example.yaml").read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("# resilience:"))
    block = []
    for l in lines[start:]:
        if not l.startswith("#"):
            break
        block.append(l[2:] if l.startswith("# ") else l[1:])
    assert resilience.settings_from(yaml.safe_load("\n".join(block))) == ResilienceSettings()           # the example shows the defaults
    for name in ResilienceSettings.__dataclass_fields__:
        assert re.search(rf"^#\s+{name}:", "\n".join(lines), re.M), f"settings.example.yaml does not show {name}"
    assert (ROOT / "config" / "settings.example.yaml").read_bytes() == (SRC / "settings.example.yaml").read_bytes()


# ------------------------------------------------------------------ the numbers in the sentences
def test_the_numbers_in_the_sentences_are_the_ones_in_the_code():
    s = ResilienceSettings()
    assert "After `erpnext_failures` failed requests" in TEXT and f"{inspect.signature(ERPNextClient.__init__).parameters['timeout'].default:g} seconds for ERPNext" in TEXT
    assert "500 or above" in TEXT and "429" in TEXT
    assert (s.erpnext_failures, s.model_failures) == (5, 3) and "tried three times" in TEXT          # (the model's limit as the table of the model says it)
    assert "300 seconds for the agents, 240 for the helper" in TEXT


# ------------------------------------------------------------------ names that must be real
def test_the_metrics_and_the_events_the_document_names_exist():
    registered = set(metrics.REGISTRY._metrics) | {"erp_agents_metric_series_dropped_total"}
    for name in re.findall(r"`(erp_agents_[a-z_]+)`", TEXT):
        assert name in registered, name
    obs_text = (ROOT / "docs" / "OBSERVABILITY.md").read_text(encoding="utf-8")
    for event in ("circuit_change", "inbound_duplicate", "decision_recovered", "rate_limited"):
        assert f"`{event}`" in TEXT, f"{event} is not named in RESILIENCE.md"
        assert f"| `{event}` |" in obs_text, f"{event} is not in the event table of OBSERVABILITY.md"


def test_the_frappe_table_names_modules_and_functions_that_exist():
    rows = table("resilience:frappe")
    assert len(rows) == 6
    for row in rows:
        for module in re.findall(r"`(core/[a-z_/]+\.py)`", row[1]):
            assert (SRC / module).is_file(), module
    wanted = {"core/resilience.py": ["class CircuitBreaker", "class RateLimiter", "class Limits", "class BreakerStore"], "core/httpguard.py": ["def guard_session"],
              "core/approvals/inbound_ledger.py": ["def claim"], "core/approvals/service.py": ["def reconcile_pending", "reconcile: Optional"], "core/erp/base.py": ["def find_by_reference"]}
    for path, needles in wanted.items():
        code = (SRC / path).read_text(encoding="utf-8")
        for needle in needles:
            assert needle in code, (path, needle)
    for name in ("ApprovalService.reconcile_pending", "KindHandler.reconcile", "find_by_reference", "RateLimiter", "Limits"):
        assert name in TEXT


def test_the_other_documents_point_to_it():
    for path in ("README.md", "SECURITY.md", "docs/DEVELOPING.md", "docs/KNOWN_LIMITATIONS.md", "docs/FRAPPE_APP_PLAN.md", "docs/OBSERVABILITY.md"):
        assert "RESILIENCE.md" in (ROOT / path).read_text(encoding="utf-8"), path


@pytest.mark.parametrize("path", ["README.md", "SECURITY.md"])
def test_the_documents_give_the_schema_version_the_code_has(path):
    from erp_agents.core.storage import SCHEMA_VERSION
    assert f"the core is at version {SCHEMA_VERSION}" in (ROOT / path).read_text(encoding="utf-8")

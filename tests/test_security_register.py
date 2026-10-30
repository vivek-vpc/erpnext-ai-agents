"""docs/SECURITY_CHECKS.md says which test keeps each security check true, and what the same property is in a Frappe app (Phase 4 of the hardening brief). This compares it with the code: every test and
every adversarial case it names exists, no check of the penetration test is missing from it, no adversarial case is left out, the numbers in its tables are the ones in the code, and the versions it states are
the ones the CI and the lockfile pin. A document that can drift from the code is worth less than none."""
import ast
import json
import re
from pathlib import Path

import pytest

from erp_agents.core import prompting
from surface import REGISTER, table
from tools.evals.cases import load_cases

ROOT = Path(__file__).resolve().parent.parent
TEXT = REGISTER.read_text(encoding="utf-8")


def all_test_names():
    names = set()
    for path in [*(ROOT / "tests").glob("test_*.py"), *(ROOT / "tests" / "evals").glob("test_*.py"), *(ROOT / "src" / "erp_agents" / "packs").glob("*/tests/test_*.py")]:
        names |= {n.name for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}
    return names


CHECKS = table("checks")
ADVERSARIAL = {c.id for c in load_cases() if c.group == "adversarial"}


def test_every_test_and_case_the_register_names_exists():
    known = all_test_names() | ADVERSARIAL | {c.id for c in load_cases()}
    cited = {name for row in CHECKS for name in re.findall(r"`([^`]+)`", row[3])}
    assert len(cited) >= 80, "the register should name the tests that prove each check"
    missing = sorted(n for n in cited if n not in known)
    assert not missing, f"docs/SECURITY_CHECKS.md names tests or cases that do not exist: {missing}"


def test_the_checks_have_unique_ids_and_a_frappe_column_and_a_proof():
    ids = [row[0] for row in CHECKS]
    assert ids == [f"PT-{i:02d}" for i in range(1, len(ids) + 1)] and len(ids) >= 26
    for row in CHECKS:
        assert len(row) == 5 and all(cell for cell in row), row[0]
        assert re.search(r"`test_|`adv-", row[3]), f"{row[0]} names no test"


def test_no_check_of_the_penetration_test_is_missing():
    sources = " ".join(row[2] for row in CHECKS)
    covered = set(re.findall(r"\b[TX]\d+\b", sources))
    for low, high in re.findall(r"\bX(\d+) to X(\d+)\b", sources):
        covered |= {f"X{i}" for i in range(int(low), int(high) + 1)}
    wanted = {f"T{i}" for i in range(1, 11)} | {f"X{i}" for i in range(0, 10)}
    assert wanted <= covered, f"checks of the 19 Oct script that the register does not cover: {sorted(wanted - covered)}"


def test_every_adversarial_case_is_cited():
    cited = {name for row in CHECKS for name in re.findall(r"`(adv-[^`]+)`", row[3])}
    assert ADVERSARIAL and ADVERSARIAL <= cited, f"adversarial cases the register does not cite: {sorted(ADVERSARIAL - cited)}"
    assert len(ADVERSARIAL) >= 20


def test_the_table_of_marks_and_limits_is_the_one_in_the_code():
    rows = {r[0].strip("`"): r[1] for r in table("fence")}
    assert prompting.OPEN in rows["Marks"] and prompting.CLOSE in rows["Marks"]
    for name in ("MAX_FIELD", "MAX_FACT_LINE", "MAX_TOOL_STRING", "MAX_QUESTION"):
        assert int(rows[name]) == getattr(prompting, name), name


def test_the_versions_the_register_states_are_the_ones_that_are_pinned():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    sha = re.search(r"commit\s+`([0-9a-f]{40})`", TEXT)
    version = re.search(r"Semgrep `([\d.]+)`", TEXT)
    assert sha and version
    assert f"git -C frappe-semgrep-rules checkout {sha.group(1)}" in ci, "the register states another commit of Frappe's rules than the CI uses"
    assert f'"semgrep=={version.group(1)}"' in ci
    package = json.loads((ROOT / "tests" / "browser" / "package.json").read_text(encoding="utf-8"))
    stated = re.search(r"jsdom (\d+\.\d+\.\d+)", TEXT)
    assert stated and package["dependencies"] == {"jsdom": stated.group(1)}, "the register states another jsdom than tests/browser/package.json pins"
    lock = json.loads((ROOT / "tests" / "browser" / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["packages"]["node_modules/jsdom"]["version"] == stated.group(1)


def test_the_semgrep_job_is_not_a_required_check_and_the_dom_test_is_required_in_one_cell():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "name: Semgrep with Frappe's rules (not a required check)" in ci
    assert "ERP_AGENTS_REQUIRE_BROWSER_TESTS" in ci and "npm ci --prefix tests/browser" in ci


@pytest.mark.parametrize("path", ["README.md", "SECURITY.md", "docs/DEVELOPING.md", "docs/PENTEST_REPORT.md", "docs/FRAPPE_APP_PLAN.md", "docs/EVALUATION.md"])
def test_the_other_documents_point_to_the_register(path):
    assert "SECURITY_CHECKS.md" in (ROOT / path).read_text(encoding="utf-8"), path

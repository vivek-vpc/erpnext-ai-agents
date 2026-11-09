"""tools/contract_table.py sets the three clients side by side and explains every difference."""
import json

from ai_munimji_engine.testkit import contract
from tools import contract_table


def column(path, **statuses):
    rows = [{"name": c.name, "status": statuses.get(c.name, "pass"), "detail": f"{statuses.get(c.name)} because ..." if c.name in statuses else ""} for c in contract.CHECKS]
    path.write_text(json.dumps(rows), encoding="utf-8")
    return str(path)


def test_the_table_has_a_row_per_check_and_lists_the_checks_that_differ(tmp_path):
    first, second = contract.CHECKS[0].name, contract.CHECKS[1].name
    demo = column(tmp_path / "demo.json")
    frappe = column(tmp_path / "frappe.json", **{first: "error", second: "skip"})
    text = contract_table.table([contract_table.load(f"Demo ERP={demo}"), contract_table.load(f"Frappe client={frappe}")])
    lines = text.splitlines()
    assert lines[0] == "| Check | Kind | Demo ERP | Frappe client |"
    assert f"| {first} | " in text and f"| {first} | " + ("writes" if contract.CHECKS[0].writes else "reads") + " | pass | ERROR |" in text
    assert f"| {second} | " + ("writes" if contract.CHECKS[1].writes else "reads") + " | pass | skip |" in text
    assert f"{len(contract.CHECKS)} pass" in text and f"{len(contract.CHECKS) - 2} pass, 1 error, 1 skip" in text
    assert "**2 of " in text and f"* `{first}`" in text and "  * Frappe client: ERROR, error because ..." in text and "  * Demo ERP:" not in text


def test_when_every_column_agrees_it_says_so(tmp_path):
    a, b = column(tmp_path / "a.json"), column(tmp_path / "b.json")
    text = contract_table.table([contract_table.load(f"A={a}"), contract_table.load(f"B={b}")])
    assert f"**All {len(contract.CHECKS)} checks give the same result in every column.**" in text


def test_a_check_missing_from_a_file_shows_as_a_dash(tmp_path):
    rows = [{"name": c.name, "status": "pass", "detail": ""} for c in contract.CHECKS[1:]]
    (tmp_path / "short.json").write_text(json.dumps(rows), encoding="utf-8")
    full = column(tmp_path / "full.json")
    text = contract_table.table([contract_table.load(f"Full={full}"), contract_table.load(f"Short={tmp_path / 'short.json'}")])
    assert f"| {contract.CHECKS[0].name} | " in text and " | pass | - |" in text


def test_a_column_without_a_file_name_is_refused():
    try:
        contract_table.load("no-equals-sign")
    except SystemExit as stop:
        assert "Label=file.json" in str(stop)
    else:
        raise AssertionError("it accepted a column with no file")

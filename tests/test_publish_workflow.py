"""The workflow that publishes the engine keeps the names PyPI was registered with, asks for no more than it needs, pins what it runs, and checks the tag against the version.

Nothing here publishes anything. The same checks run in that workflow's dry run on every pull request (tools/check_publish_workflow.py).
"""

import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_publish_workflow", ROOT / "tools" / "check_publish_workflow.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

REAL = yaml.safe_load(check.WORKFLOW.read_text(encoding="utf-8"))
PYPROJECT = (ROOT / "engine" / "pyproject.toml").read_text(encoding="utf-8")
VERSION = check.package_version(PYPROJECT)


def broken(change) -> list[str]:
    doc = copy.deepcopy(REAL)
    change(doc)
    return check.workflow_problems(doc, "publish-engine.yml")


def test_the_real_workflow_has_the_names_the_permissions_and_the_pins():
    assert check.WORKFLOW.name == "publish-engine.yml"
    assert check.workflow_problems(REAL, check.WORKFLOW.name) == []


def test_the_registered_names_are_in_the_workflow_and_in_the_steps_for_the_owner():
    steps = (ROOT / "docs" / "PUBLISH_ENGINE.md").read_text(encoding="utf-8")
    for name in ("publish-engine.yml", "ai-munimji-engine", "vivek-vpc", "erpnext-ai-agents", "environment `pypi`", "engine-v"):
        assert name in steps, f"docs/PUBLISH_ENGINE.md does not say {name}"
    assert REAL["jobs"]["publish"]["environment"] == "pypi"
    assert "engine-v*" in REAL[True]["push"]["tags"]  # YAML 1.1 reads `on` as True


@pytest.mark.parametrize(
    "change, expected",
    [
        (lambda d: d["jobs"]["publish"].update(environment="release"), "environment"),
        (lambda d: d["jobs"]["publish"].update(environment={"name": "production"}), "environment"),
        (lambda d: d["jobs"]["publish"]["permissions"].update({"contents": "write"}), "exactly the permissions"),
        (lambda d: d["jobs"]["publish"]["permissions"].update({"packages": "write"}), "exactly the permissions"),
        (lambda d: d["jobs"]["publish"]["permissions"].pop("id-token"), "exactly the permissions"),
        (lambda d: d["jobs"]["build"]["permissions"].update({"id-token": "write"}), "only publish may"),
        (lambda d: d.update(permissions={"contents": "write"}), "no permission"),
        (lambda d: d["jobs"]["publish"].pop("if"), "only for a tag"),
        (lambda d: d["jobs"]["publish"].update({"if": "github.ref == 'refs/heads/main'"}), "only for a tag"),
        (lambda d: d[True].pop("pull_request"), "dry run on pull requests"),
        (lambda d: d[True]["push"].update(tags=["v*"]), "tag engine-v*"),
        (lambda d: d["jobs"]["publish"].update(needs="other"), "wait for the build"),
        (lambda d: d["jobs"]["publish"]["steps"][1].update(uses="pypa/gh-action-pypi-publish@release/v1"), "SHA"),
        (lambda d: d["jobs"]["build"]["steps"][0].update(uses="actions/checkout@v4"), "SHA"),
        (lambda d: d["jobs"]["publish"]["steps"][1]["with"].update(password="${{ secrets.PYPI_API_TOKEN }}"), "password"),
        (lambda d: d["jobs"]["publish"]["steps"][1]["with"].update({"repository-url": "https://test.pypi.org/legacy/"}), "password"),
        (lambda d: d["jobs"]["publish"]["steps"][1].update(env={"TWINE_PASSWORD": "x"}), "a secret"),
        (lambda d: d["jobs"]["publish"]["steps"][1].update(env={"X": "${{ secrets.PYPI_API_TOKEN }}"}), "a secret"),
        (lambda d: d["jobs"]["publish"]["steps"].pop(1), "exactly once"),
        (lambda d: d["jobs"]["build"]["steps"].pop(5), "twine check"),
        (lambda d: d["jobs"]["build"]["steps"].pop(3), "names"),
        (lambda d: d["jobs"]["build"]["steps"].pop(6), "tag matches"),
        (lambda d: d["jobs"].pop("publish"), "no job called publish"),
    ],
)
def test_each_way_the_workflow_could_stop_matching_what_was_registered_is_caught(change, expected):
    problems = broken(change)
    assert problems and any(expected in p for p in problems), problems


def test_a_different_file_name_is_caught():
    assert any("PyPI was told" in p for p in check.workflow_problems(REAL, "publish.yml"))


def test_a_tag_must_match_the_version_of_the_package_and_the_files_built():
    wheel, sdist = f"ai_munimji_engine-{VERSION}-py3-none-any.whl", f"ai_munimji_engine-{VERSION}.tar.gz"
    assert check.tag_problems(f"engine-v{VERSION}", PYPROJECT, [wheel, sdist]) == []
    assert any("says" in p for p in check.tag_problems("engine-v9.9.9", PYPROJECT, [wheel, sdist]))
    assert any("not engine-vX.Y.Z" in p for p in check.tag_problems("v0.1.0", PYPROJECT, [wheel, sdist]))
    assert any("not engine-vX.Y.Z" in p for p in check.tag_problems("engine-v0.1.0-final", PYPROJECT, [wheel, sdist]))
    assert any("nothing was built" in p for p in check.tag_problems(f"engine-v{VERSION}", PYPROJECT, []))
    # 0.1.0 is on PyPI and PyPI never takes a file name twice: its tag must never pass for a later version
    assert VERSION != "0.1.0" and any("says" in p for p in check.tag_problems("engine-v0.1.0", PYPROJECT, [wheel, sdist]))
    assert VERSION != "0.1.1" and any("says" in p for p in check.tag_problems("engine-v0.1.1", PYPROJECT, [wheel, sdist]))  # 0.1.1 too
    assert VERSION != "0.1.2" and any("says" in p for p in check.tag_problems("engine-v0.1.2", PYPROJECT, [wheel, sdist]))  # and 0.1.2
    assert any("does not carry" in p for p in check.tag_problems(f"engine-v{VERSION}", PYPROJECT, ["ai_munimji_engine-0.0.9-py3-none-any.whl"]))


def test_the_version_is_read_from_the_project_table_and_nothing_else():
    assert check.package_version('[build-system]\nrequires = ["x"]\n\n[project]\nname = "p"\nversion = "1.2.3"\n') == "1.2.3"
    assert check.package_version('[tool.x]\nversion = "9.9.9"\n[project]\nname = "p"\n') is None

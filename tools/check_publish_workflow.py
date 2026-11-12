"""Checks of the workflow that publishes the engine (.github/workflows/publish-engine.yml), run by that workflow's dry run on every pull request and by tests/test_publish_workflow.py.

    python tools/check_publish_workflow.py workflow          # the names PyPI was registered with, the permissions, the pins
    python tools/check_publish_workflow.py tag engine-v0.1.3 # on a tag: the tag is engine-v + the version of engine/pyproject.toml, and the files in dist/ carry it

It prints each problem and exits 1 if there is one. It publishes nothing and needs no network.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-engine.yml"

# What was registered on PyPI as the trusted publisher of the project (docs/PUBLISH_ENGINE.md). Changing one of these without changing PyPI breaks publishing.
FILE_NAME = "publish-engine.yml"
ENVIRONMENT = "pypi"
TAG_PATTERN = "engine-v*"
PUBLISH_PERMISSIONS = {"id-token": "write", "contents": "read"}
PYPI_ACTION = "pypa/gh-action-pypi-publish"
PINNED = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-/]+@[0-9a-f]{40}$")
TAG = re.compile(r"^engine-v(\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?)$")


def _steps(job: dict) -> list[dict]:
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def workflow_problems(doc: dict, filename: str) -> list[str]:
    """What is wrong with the parsed workflow `doc` that was read from a file called `filename` (an empty list when nothing is)."""
    problems: list[str] = []
    if filename != FILE_NAME:
        problems.append(f"the file is called {filename}, but PyPI was told {FILE_NAME}")
    triggers = doc.get("on", doc.get(True)) or {}  # YAML 1.1 reads the key `on` as True
    if TAG_PATTERN not in ((triggers.get("push") or {}).get("tags") or []):
        problems.append(f"it does not run when a tag {TAG_PATTERN} is pushed")
    if "pull_request" not in triggers:
        problems.append("there is no dry run on pull requests")
    if (doc.get("permissions") or {}) != {}:
        problems.append("the workflow as a whole must grant no permission (each job asks for its own)")

    jobs = doc.get("jobs") or {}
    for name, job in jobs.items():
        if name != "publish" and "id-token" in (job.get("permissions") or {}):
            problems.append(f"the job {name} may ask for an identity token; only publish may")
        for step in _steps(job):
            uses = step.get("uses")
            if uses and not PINNED.match(str(uses)):
                problems.append(f"the job {name} uses {uses}, which is not pinned by a full commit SHA")

    publish = jobs.get("publish")
    if not isinstance(publish, dict):
        problems.append("there is no job called publish")
        return problems
    environment = publish.get("environment")
    environment_name = environment.get("name") if isinstance(environment, dict) else environment
    if environment_name != ENVIRONMENT:
        problems.append(f"the publish job uses the environment {environment_name!r}, but PyPI was told {ENVIRONMENT!r}")
    if (publish.get("permissions") or {}) != PUBLISH_PERMISSIONS:
        problems.append(f"the publish job must have exactly the permissions {PUBLISH_PERMISSIONS}, nothing more")
    condition = str(publish.get("if") or "")
    if "tag" not in condition or "engine-v" not in condition:
        problems.append("the publish job does not run only for a tag engine-v*")
    steps = _steps(publish)
    publishing = [s for s in steps if str(s.get("uses") or "").startswith(PYPI_ACTION + "@")]
    if len(publishing) != 1:
        problems.append(f"the publish job must publish with {PYPI_ACTION} exactly once")
    for step in publishing:
        with_ = step.get("with") or {}
        if any(key in with_ for key in ("password", "user", "repository-url")):
            problems.append("the publishing step passes a password, a user or another repository: trusted publishing needs none")
    secret_words = ("PYPI", "TOKEN", "PASSWORD", "TWINE", "SECRET", "API_KEY")
    if any(any(w in str(key).upper() for w in secret_words) for step in steps for key in (step.get("env") or {})) or "secrets." in json.dumps(publish):
        problems.append("the publish job sets or reads a token, a password or a secret: nothing is stored, PyPI trusts the workflow")

    build = jobs.get("build")
    if not isinstance(build, dict):
        problems.append("there is no job called build (the dry run)")
        return problems
    runs = " ".join(str(s.get("run") or "") for s in _steps(build))
    if "twine check" not in runs:
        problems.append("the dry run does not run twine check")
    if "check_publish_workflow.py workflow" not in runs:
        problems.append("the dry run does not check this workflow's names")
    if "check_publish_workflow.py tag" not in runs:
        problems.append("the build does not check that the tag matches the version")
    if "needs" in build:
        problems.append("the build must not wait for anything: it is the dry run")
    if "build" not in (publish.get("needs") if isinstance(publish.get("needs"), list) else [publish.get("needs")]):
        problems.append("the publish job does not wait for the build")
    return problems


def package_version(pyproject_text: str) -> str | None:
    match = re.search(r'(?ms)^\[project\].*?^version\s*=\s*"([^"]+)"', pyproject_text)
    return match.group(1) if match else None


def tag_problems(tag: str, pyproject_text: str, dist_names: list[str]) -> list[str]:
    """What is wrong with pushing `tag` for the package in `pyproject_text`, with the files `dist_names` built."""
    match = TAG.match(tag)
    if not match:
        return [f"the tag {tag!r} is not engine-vX.Y.Z"]
    version = package_version(pyproject_text)
    problems = []
    if match.group(1) != version:
        problems.append(f"the tag says {match.group(1)} but engine/pyproject.toml says {version}")
    if not dist_names:
        problems.append("nothing was built")
    for name in dist_names:
        if not (name.startswith(f"ai_munimji_engine-{version}-") or name == f"ai_munimji_engine-{version}.tar.gz"):
            problems.append(f"the file {name} does not carry the version {version}")
    return problems


def main(argv: list[str]) -> int:
    import yaml

    if argv[:1] == ["workflow"]:
        problems = workflow_problems(yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")), WORKFLOW.name)
    elif len(argv) == 2 and argv[0] == "tag":
        dist = sorted(p.name for p in (ROOT / "dist").glob("*") if p.is_file())
        problems = tag_problems(argv[1], (ROOT / "engine" / "pyproject.toml").read_text(encoding="utf-8"), dist)
    else:
        print(__doc__)
        return 2
    for problem in problems:
        print("PROBLEM:", problem)
    if not problems:
        print("ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""The engine (`ai_munimji_engine`) is installable on its own, into a Frappe bench: it needs pydantic and PyYAML, and imports no web framework, scheduler, database driver, model SDK or
network library, and nothing of `erp_agents`.

The measurement of docs/FRAPPE_FACTS.md 6.2 (which modules import when those names are refused) is this test; the same check runs again in a clean virtual environment under Frappe's pins
by tools/check_engine_wheel.py. A module that needs one of the refused names belongs in `erp_agents`, not in the engine."""
import ast
import json
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:                       # Python 3.10, which the engine and the service support, has no tomllib
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
SOURCE = ENGINE / "src"
PACKAGE = "ai_munimji_engine"

# what a bench must not be made to depend on, and what the engine must not know about
BLOCKED = ["fastapi", "starlette", "uvicorn", "apscheduler", "sqlite3", "mcp", "requests", "httpx", "jsonschema", "certifi", "urllib3", "anyio", "erp_agents"]
ALLOWED_THIRD_PARTY = {"pydantic", "yaml"}                      # pydantic and PyYAML (the distribution is pyyaml, the import is yaml)
OPTIONAL_THIRD_PARTY = {"opentelemetry": "ai_munimji_engine/core/tracing.py"}       # imported inside a function, only when tracing is switched on, and declared as the `otel` extra

CHILD = r'''
import importlib, importlib.abc, json, sys
BLOCKED = set(%(blocked)r)
class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("BLOCKED:" + name.split(".")[0])
        return None
sys.meta_path.insert(0, Refuse())
for m in [m for m in sys.modules if m.split(".")[0] in BLOCKED]:
    del sys.modules[m]
sys.path[:0] = %(paths)r                                          # the engine's source and nothing of the service
out = {}
for name in %(names)r:
    try:
        importlib.import_module(name)
    except ImportError as e:
        out[name] = "BLOCKED " + str(e).split("BLOCKED:")[1].split()[0] if "BLOCKED:" in str(e) else "IMPORT-ERROR " + str(e)[:200]
    except Exception as e:
        out[name] = "ERROR " + type(e).__name__ + " " + str(e)[:200]
    else:
        out[name] = "OK"
print(json.dumps(out))
'''


def module_names() -> list[str]:
    return sorted(info.name for info in pkgutil.walk_packages([str(SOURCE / PACKAGE)], prefix=PACKAGE + ".") if not info.name.endswith(".__main__"))


def imports_of(path: Path):
    """(line, top-level module name) for every import in the file, relative imports left out."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            yield node.lineno, node.module.split(".")[0]


def test_every_engine_module_imports_with_the_web_the_database_and_the_network_refused():
    code = CHILD % {"blocked": BLOCKED, "paths": [str(SOURCE)], "names": module_names()}
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-600:]
    results = json.loads(done.stdout.strip().splitlines()[-1])
    assert len(results) > 60                                                    # the walk found the modules
    failed = {name: result for name, result in results.items() if result != "OK"}
    assert not failed, "these engine modules reach something the engine must not need: " + json.dumps(failed, indent=1)


def test_no_engine_source_file_names_a_refused_module_in_an_import():
    offenders = [f"{path.relative_to(SOURCE).as_posix()}:{line} imports {top}" for path in sorted((SOURCE / PACKAGE).rglob("*.py")) for line, top in imports_of(path) if top in BLOCKED]
    assert not offenders, "\n".join(offenders)


def test_the_engine_imports_only_the_standard_library_pydantic_and_pyyaml():
    stdlib = set(sys.stdlib_module_names)
    offenders = [f"{path.relative_to(SOURCE).as_posix()}:{line} imports {top}" for path in sorted((SOURCE / PACKAGE).rglob("*.py")) for line, top in imports_of(path)
                 if top not in stdlib and top not in ALLOWED_THIRD_PARTY and top != PACKAGE and OPTIONAL_THIRD_PARTY.get(top) != path.relative_to(SOURCE).as_posix()]
    assert not offenders, "\n".join(offenders)


def test_the_optional_imports_are_inside_functions_so_that_importing_the_module_needs_nothing():
    tree = ast.parse((SOURCE / "ai_munimji_engine/core/tracing.py").read_text(encoding="utf-8"))
    top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not [n for n in top_level if (getattr(n, "module", None) or "").split(".")[0] in OPTIONAL_THIRD_PARTY or any(a.name.split(".")[0] in OPTIONAL_THIRD_PARTY for a in getattr(n, "names", []))]


def test_the_engine_declares_pydantic_and_pyyaml_and_nothing_else():
    project = tomllib.loads((ENGINE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    names = {d.split(">")[0].split("=")[0].split("<")[0].split("~")[0].split(";")[0].strip().lower() for d in project["dependencies"]}
    assert names == {"pydantic", "pyyaml"}
    assert set(project.get("optional-dependencies", {})) == {"tzdata", "otel"}   # everything else is an extra: the time zone database for Windows, and optional tracing
    assert project["name"] == "ai-munimji-engine" and project["license"] == "GPL-3.0-or-later"
    assert not any("@" in d and "gmail" in d for d in project["dependencies"])
    assert all("email" not in a for a in project.get("authors", []))            # no personal address in a package that will be public


def test_the_version_in_the_package_is_the_version_in_the_project():
    project = tomllib.loads((ENGINE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    import ai_munimji_engine
    assert ai_munimji_engine.__version__ == project["version"]


def test_the_services_wheel_holds_both_packages_and_the_engines_only_its_own():
    """Packaging, by the settings: the root project finds both source trees; the engine project finds its own only (so `pip install .[agents]` needs no published engine)."""
    root = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["packages"]["find"]
    assert root["where"] == ["src", "engine/src"]
    own = tomllib.loads((ENGINE / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["packages"]["find"]
    assert own["where"] == ["src"] and not (ENGINE / "src" / "erp_agents").exists()


@pytest.mark.parametrize("package", ["core", "packs/stock_procurement"])
def test_the_engines_core_names_nothing_of_a_pack_and_a_pack_nothing_of_another(package):
    """The same boundaries as the service's (tests/test_pack_boundaries.py), for the code that moved into the engine."""
    base = SOURCE / PACKAGE / package
    if package == "core":
        offenders = [f"{p.relative_to(SOURCE).as_posix()}:{n.lineno}" for p in base.rglob("*.py") for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
                     if isinstance(n, ast.ImportFrom) and n.level == 0 and (n.module or "").startswith(PACKAGE + ".packs")]
        assert not offenders, "the engine's core must not import a pack: " + ", ".join(offenders)
    else:
        allowed = (PACKAGE + ".core", PACKAGE + ".storage", PACKAGE + ".stores", PACKAGE + ".runtime", PACKAGE + ".packs.stock_procurement")
        offenders = [f"{p.relative_to(SOURCE).as_posix()}:{n.lineno} imports {n.module}" for p in base.rglob("*.py") for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
                     if isinstance(n, ast.ImportFrom) and n.level == 0 and (n.module or "").startswith(PACKAGE) and not (n.module or "").startswith(allowed)]
        assert not offenders, "a pack may import the engine's core, its stores and itself only: " + ", ".join(offenders)

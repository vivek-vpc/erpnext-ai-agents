"""The rule logic does not need SQLite: every module that is not about SQLite itself imports with `sqlite3` refused.

All the modules are imported in one fresh interpreter whose import system refuses `sqlite3`, so a stray `import sqlite3` (or an import of a module that has one) shows at once,
with the module's name. A module that cannot be imported is not kept by Python, so what it needs fails again for every module that imports it: one interpreter gives the same
answer as one per module, in a second instead of a minute and a half. The modules that ARE about SQLite are named below, with the reason; the list is closed: a new module that
needs SQLite must be added to it on purpose, and one that stops needing it must be taken off (the test says so). This is the measurement of docs/FRAPPE_FACTS.md 6.2 turned
into a test, and the first half of the engine's blocked-import rule (the second half, for the engine package itself, is in test_engine_imports.py)."""
import json
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PACKAGES = (("erp_agents", REPO / "src"), ("ai_munimji_engine", REPO / "engine" / "src"))

ABOUT_SQLITE = {
    "erp_agents.core.storage": "the SQLite Database class, its schema and its migrations",
    "erp_agents.core.ops": "backup, restore, verify and the instance lock of the SQLite file",
    "erp_agents.core.doctor": "reads the SQLite file read-only",
    "erp_agents.stores": "builds the SQLite stores",
    "erp_agents.stores.sqlite_approvals": "the SQL of the approvals",
    "erp_agents.stores.sqlite_core": "the SQL of the other core stores",
    "erp_agents.packs.stock_procurement.schema": "the stock pack's tables and migrations",
    "erp_agents.packs.stock_procurement.stores": "the SQL of the stock pack, and the choice of the stores",
    # these reach the SQLite modules only because they build or run the whole standalone application (wiring, web server, scheduler, command line)
    "erp_agents.core.bootstrap": "wires the standalone application together (builds the SQLite stores)",
    "erp_agents.core.api.app": "the web application (imports the wiring)",
    "erp_agents.core.cli": "the command line (imports the wiring and the doctor)",
    "erp_agents.core.scheduler": "the in-process scheduler (imports the wiring)",
    "erp_agents.packs.stock_procurement.pack": "the pack's entry object (chooses its stores)",
    "erp_agents.packs.stock_procurement.cli": "the pack's commands (import the wiring)",
}

CHILD = r'''
import importlib, importlib.abc, json, sys
class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "sqlite3":
            raise ImportError("BLOCKED:sqlite3")
        return None
sys.meta_path.insert(0, Refuse())
sys.modules.pop("sqlite3", None)
sys.path[:0] = %(paths)r
out = {}
for name in %(names)r:
    try:
        importlib.import_module(name)
    except ImportError as e:
        out[name] = "BLOCKED" if "BLOCKED:sqlite3" in str(e) else "IMPORT-ERROR " + str(e)[:200]
    except Exception as e:
        out[name] = "ERROR " + type(e).__name__ + " " + str(e)[:200]
    else:
        out[name] = "OK"
print(json.dumps(out))
'''


def module_names() -> list[str]:
    out = []
    for package, root in PACKAGES:
        for info in pkgutil.walk_packages([str(root / package)], prefix=package + "."):
            if ".tests" in info.name or info.name.endswith(".__main__"):
                continue
            out.append(info.name)
    return sorted(out)


def import_all_without_sqlite() -> dict[str, str]:
    code = CHILD % {"paths": [str(root) for _, root in PACKAGES], "names": module_names()}
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr[-500:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def results() -> dict[str, str]:
    return import_all_without_sqlite()


def test_every_module_that_is_not_about_sqlite_imports_without_it(results):
    needs = sorted(n for n, r in results.items() if r == "BLOCKED")
    other = {n: r for n, r in results.items() if r not in ("OK", "BLOCKED")}
    assert not other, f"these modules failed to import for another reason: {other}"
    unexpected = [n for n in needs if n not in ABOUT_SQLITE]
    assert not unexpected, "these modules reach SQLite but are not on the list of the modules that are about it (the rule logic must not): " + ", ".join(unexpected)
    stale = sorted(n for n in ABOUT_SQLITE if n not in needs)
    assert not stale, "these modules no longer need SQLite: take them off the list: " + ", ".join(stale)


@pytest.mark.parametrize("name", ["ai_munimji_engine.packs.stock_procurement.tools", "ai_munimji_engine.core.toolkit", "ai_munimji_engine.core.approvals.service", "ai_munimji_engine.core.audit",
                                  "ai_munimji_engine.packs.stock_procurement.inventory.agent", "ai_munimji_engine.packs.stock_procurement.procurement.agent",
                                  "ai_munimji_engine.stores", "ai_munimji_engine.stores.stock"])
def test_the_tool_registry_and_the_services_it_stands_on_import_without_sqlite(results, name):
    assert results[name] == "OK"

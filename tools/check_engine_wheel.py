"""Build the engine wheel and prove it works where it will run: alone, in a clean virtual environment, under Frappe's pins.

  python tools/check_engine_wheel.py --frappe-pyproject frappe-pyproject.toml     (Frappe v16's pyproject.toml: its dependencies become pip constraints)
  python tools/check_engine_wheel.py --constraints constraints.txt                (or the constraints themselves)
  python tools/check_engine_wheel.py --keep-wheel dist/                           (also copy the wheel there)

It (1) builds the wheel from engine/ with pip; (2) checks what is in it: the package, its metadata and licence, and nothing of the standalone service; (3) makes a new virtual environment,
installs the wheel with the constraints and runs `pip check`; (4) lists what got installed (pydantic, PyYAML and what they bring, nothing else); (5) imports every module of the package
there with the web, the database and the network refused. Nothing is published. Exit code 0 when all of it holds.

Frappe's pyproject lists two dependencies as git URLs and some with extras; pip does not allow those in a constraints file, so they are left out or stripped, as in docs/FRAPPE_FACTS.md 5.2.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BLOCKED = ["fastapi", "starlette", "uvicorn", "apscheduler", "sqlite3", "mcp", "requests", "httpx", "jsonschema", "certifi", "urllib3", "anyio", "erp_agents"]
ALLOWED_INSTALLED = {"ai-munimji-engine", "pydantic", "pydantic-core", "annotated-types", "typing-extensions", "typing-inspection", "pyyaml", "pip", "setuptools"}

IMPORT_ALL = r'''
import importlib, importlib.abc, json, pkgutil, sys
BLOCKED = set(%(blocked)r)
class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("BLOCKED:" + name.split(".")[0])
        return None
sys.meta_path.insert(0, Refuse())
import ai_munimji_engine
names = sorted(i.name for i in pkgutil.walk_packages(ai_munimji_engine.__path__, prefix="ai_munimji_engine.") if not i.name.endswith(".__main__"))
out = {}
for name in names:
    try:
        importlib.import_module(name)
        out[name] = "OK"
    except Exception as e:
        out[name] = type(e).__name__ + " " + str(e)[:160]
print(json.dumps({"version": ai_munimji_engine.__version__, "file": ai_munimji_engine.__file__, "modules": out}))
'''


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def constraints_from(pyproject: Path, target: Path) -> int:
    deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    kept = [re.sub(r"\[[^\]]*\]", "", d) for d in deps if " @ " not in d and "@git" not in d]
    target.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return len(kept)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frappe-pyproject", type=Path)
    ap.add_argument("--constraints", type=Path)
    ap.add_argument("--keep-wheel", type=Path)
    args = ap.parse_args()
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="engine-wheel-") as tmp:
        work = Path(tmp)
        constraints = args.constraints
        if args.frappe_pyproject:
            constraints = work / "constraints.txt"
            print(f"constraints: {constraints_from(args.frappe_pyproject, constraints)} entries from {args.frappe_pyproject}")
        # 1. build
        built = run([sys.executable, "-m", "pip", "wheel", REPO / "engine", "--no-deps", "-w", work / "dist", "-q"])
        wheels = list((work / "dist").glob("*.whl"))
        if built.returncode or len(wheels) != 1:
            print(built.stdout[-800:], built.stderr[-800:])
            return 1
        wheel = wheels[0]
        print("built", wheel.name, f"({wheel.stat().st_size // 1024} KB)")
        # 2. contents
        with zipfile.ZipFile(wheel) as z:
            names = z.namelist()
        stray = [n for n in names if not (n.startswith("ai_munimji_engine/") or ".dist-info/" in n)]
        if stray or any(n.startswith("erp_agents") for n in names):
            problems.append(f"the wheel holds files that are not the engine's: {stray[:5]}")
        if not any(n.endswith("/licenses/LICENSE") or n.endswith(".dist-info/LICENSE") for n in names):
            problems.append("the wheel has no LICENSE")
        print(f"{len(names)} files: the package and its metadata only" if not stray else f"{len(names)} files, {len(stray)} stray")
        # 3. a clean environment, under the pins
        venv = work / "venv"
        run([sys.executable, "-m", "venv", venv], check=True)
        py = venv / ("Scripts" if sys.platform == "win32" else "bin") / ("python.exe" if sys.platform == "win32" else "python")
        install = [py, "-m", "pip", "install", "--disable-pip-version-check", "-q", wheel]
        if constraints:
            install += ["-c", constraints]
        done = run(install)
        if done.returncode:
            print(done.stdout[-1500:], done.stderr[-1500:])
            return 1
        check = run([py, "-m", "pip", "check"])
        print("pip check:", (check.stdout.strip() or check.stderr.strip()))
        if check.returncode:
            problems.append("pip check failed: " + check.stdout.strip())
        # 4. what got installed
        listed = json.loads(run([py, "-m", "pip", "list", "--format=json", "--disable-pip-version-check"]).stdout)
        installed = {p["name"].lower().replace("_", "-"): p["version"] for p in listed}
        print("installed:", ", ".join(f"{n} {v}" for n, v in sorted(installed.items())))
        extra = sorted(set(installed) - ALLOWED_INSTALLED)
        if extra:
            problems.append(f"packages beyond the engine, pydantic and PyYAML were installed: {extra}")
        # 5. import everything with the web, the database and the network refused
        done = run([py, "-I", "-c", IMPORT_ALL % {"blocked": BLOCKED}])
        if done.returncode:
            print(done.stdout[-800:], done.stderr[-1200:])
            return 1
        report = json.loads(done.stdout.strip().splitlines()[-1])
        bad = {m: r for m, r in report["modules"].items() if r != "OK"}
        print(f"engine {report['version']} from {report['file']}")
        print(f"{len(report['modules'])} modules imported, {len(bad)} failed" + (f": {bad}" if bad else ""))
        if bad:
            problems.append(f"modules that do not import there: {bad}")
        # 6. the stock pack's manifest is a file of the wheel, and reads there
        done = run([py, "-I", "-c", "from ai_munimji_engine.packs.stock_procurement.manifest import manifest; m = manifest(); print(m.name, m.status, m.enabled_by_default, len(m.tools))"])
        said = done.stdout.strip()
        print("manifest in the wheel:", said or done.stderr.strip()[-300:])
        if done.returncode or said != "stock_procurement active True 17":
            problems.append(f"the manifest does not read from the installed wheel: {said or done.stderr.strip()[-300:]}")
        if args.keep_wheel:
            args.keep_wheel.mkdir(parents=True, exist_ok=True)
            shutil.copy2(wheel, args.keep_wheel / wheel.name)
            print("wheel kept in", args.keep_wheel / wheel.name)
    for p in problems:
        print("PROBLEM:", p)
    print("OK" if not problems else "FAILED")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

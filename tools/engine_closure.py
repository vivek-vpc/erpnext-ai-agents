"""The import closure (static, including imports inside functions) of the modules the Frappe app needs, among the modules of erp_agents.
Usage: python tools/engine_closure.py [module ...]  -> prints the closure, and which of its members import an edge-ish third-party library

This is how the list in tools/move_to_engine.py was made (on the tree before the move: it names `erp_agents` modules, so run it on commit 3c8cfe2 or earlier to repeat it)."""
import ast
import sys
from pathlib import Path

SRC = Path(r"C:\ERP\erpnext-ai-agents\src")
THIRD = {"fastapi", "starlette", "uvicorn", "apscheduler", "requests", "httpx", "mcp", "jsonschema", "certifi", "urllib3", "anyio", "sqlite3"}
STDLIB_OK = set(sys.stdlib_module_names)


def path_of(name):
    p = SRC / Path(*name.split("."))
    if p.with_suffix(".py").exists():
        return p.with_suffix(".py")
    if (p / "__init__.py").exists():
        return p / "__init__.py"
    return None


def imports_of(name):
    path = path_of(name)
    pkg = name.split(".") if path.name == "__init__.py" else name.split(".")[:-1]
    out, third = set(), set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                mod = ".".join(base + (node.module.split(".") if node.module else []))
                mods = [mod] + [f"{mod}.{a.name}" for a in node.names]
            else:
                mods = [node.module or ""] + ([f"{node.module}.{a.name}" for a in node.names] if node.module else [])
        for m in mods:
            if m.startswith("erp_agents"):
                if path_of(m):
                    out.add(m)
            else:
                top = m.split(".")[0]
                if top in THIRD:
                    third.add(top)
    return out, third


roots = sys.argv[1:] or ["ai_munimji_engine.packs.stock_procurement.tools", "ai_munimji_engine.core.approvals.service", "ai_munimji_engine.core.audit",
                         "ai_munimji_engine.packs.stock_procurement.inventory.alerts", "ai_munimji_engine.packs.stock_procurement.procurement.proposals"]
seen, todo, why = set(), list(roots), {}
while todo:
    m = todo.pop()
    if m in seen:
        continue
    seen.add(m)
    deps, third = imports_of(m)
    why[m] = third
    todo += [d for d in deps if d not in seen]
mods = sorted(m for m in seen if not m.endswith(".__init__"))
print(len(mods), "modules in the closure")
for m in mods:
    print(("  ! " if why[m] else "    ") + m + (f"   imports {sorted(why[m])}" if why[m] else ""))

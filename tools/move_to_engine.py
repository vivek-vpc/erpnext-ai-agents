"""One-time migration (Phase 2 of the Frappe work, step 6): move the rule logic from `erp_agents` into the engine package `ai_munimji_engine`, and rewrite every reference.

The modules that move are the import closure of what the Frappe app needs: the stock tool registry, the approval service, the audit chain, the alert and proposal stores, the procurement
agent and the vendor scoring. They import nothing but the standard library, pydantic and PyYAML (`tools/engine_closure.py` prints the closure; the list below is its result, written out so
that the migration is the same every time). A module keeps its place in the tree: `erp_agents.core.audit` becomes `ai_munimji_engine.core.audit`,
`erp_agents.packs.stock_procurement.inventory.alerts` becomes `ai_munimji_engine.packs.stock_procurement.inventory.alerts`.

What it does, in order: `git mv` of each module; an empty `__init__.py` for each new package; every import statement in `src`, `tests`, `tools` and `conftest.py` that names a moved module is
rewritten (a relative import of a moved module from a module that stays becomes absolute; a relative import between modules that moved together is left as it is, because they kept their
places relative to each other); and strings that name a moved module (a `monkeypatch.setattr("erp_agents.core.x.y", ...)`, an `__import__`) are rewritten too. It does not touch the documents:
they say `core/audit.py` and mean the same path in either package (DEVELOPING.md says so).

Usage: python tools/move_to_engine.py --dry-run   (prints what it would move)
       python tools/move_to_engine.py             (does it)
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC, ENGINE = REPO / "src", REPO / "engine" / "src"
OLD, NEW = "erp_agents", "ai_munimji_engine"

MOVE = """
erp_agents.core.acting
erp_agents.core.agents.store
erp_agents.core.approvals.channels.base
erp_agents.core.approvals.inbound_ledger
erp_agents.core.approvals.models
erp_agents.core.approvals.render
erp_agents.core.approvals.router
erp_agents.core.approvals.service
erp_agents.core.audit
erp_agents.core.common
erp_agents.core.history
erp_agents.core.jobs
erp_agents.core.metrics
erp_agents.core.obs
erp_agents.core.packs
erp_agents.core.policy
erp_agents.core.settings_store
erp_agents.core.toolkit
erp_agents.core.tracing
erp_agents.core.workorders
erp_agents.packs.stock_procurement.followup.scorecard
erp_agents.packs.stock_procurement.inventory.agent
erp_agents.packs.stock_procurement.inventory.alerts
erp_agents.packs.stock_procurement.inventory.analysis
erp_agents.packs.stock_procurement.inventory.events
erp_agents.packs.stock_procurement.inventory.forecast
erp_agents.packs.stock_procurement.inventory.messages
erp_agents.packs.stock_procurement.inventory.notices
erp_agents.packs.stock_procurement.procurement.agent
erp_agents.packs.stock_procurement.procurement.messages
erp_agents.packs.stock_procurement.procurement.proposals
erp_agents.packs.stock_procurement.procurement.scoring
erp_agents.packs.stock_procurement.procurement.taxes
erp_agents.packs.stock_procurement.settings
erp_agents.packs.stock_procurement.templating
erp_agents.packs.stock_procurement.tools
""".split()
MOVED = set(MOVE)


def to_new(old: str) -> str:
    return NEW + old[len(OLD):]


def to_old(new: str) -> str:
    return OLD + new[len(NEW):]


def moved_prefix(dotted: str):
    """The longest prefix of `dotted` that is a moved module, or None."""
    parts = dotted.split(".")
    for n in range(len(parts), 0, -1):
        if ".".join(parts[:n]) in MOVED:
            return ".".join(parts[:n])
    return None


def old_module_of(path: Path):
    """The module this file has (or, if it is already moved, had) in the old tree; None for a file that was never in it."""
    if SRC in path.parents:
        base = SRC
    elif ENGINE in path.parents:
        base = ENGINE
    else:
        return None
    parts = list(path.relative_to(base).with_suffix("").parts)
    name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
    old = name if base == SRC else to_old(name)
    return old if (base == SRC or old in MOVED) else None


def git(*cmd: str) -> None:
    subprocess.run(["git", *cmd], cwd=REPO, check=True)


def move_files(dry: bool) -> None:
    packages = set()
    for old in MOVE:
        src = SRC / Path(*old.split(".")).with_suffix(".py")
        dst = ENGINE / Path(*to_new(old).split(".")).with_suffix(".py")
        assert src.is_file(), src
        print(("would move" if dry else "move"), src.relative_to(REPO), "->", dst.relative_to(REPO))
        p = dst.parent
        while p != ENGINE:
            packages.add(p)
            p = p.parent
        if not dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            git("mv", str(src), str(dst))
    if not dry:
        for pkg in sorted(packages):
            init = pkg / "__init__.py"
            if not init.exists():
                init.write_text("", encoding="utf-8", newline="\n")
                git("add", str(init))


def rewrite(path: Path) -> bool:
    raw = path.read_bytes().decode("utf-8")
    text = raw.replace("\r\n", "\n")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    old_home = old_module_of(path)
    moved_together = old_home in MOVED if old_home else False
    package = None
    if old_home:
        package = old_home.split(".") if path.name == "__init__.py" else old_home.split(".")[:-1]
    edits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                if package is None:
                    continue
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                target = ".".join(base + (node.module.split(".") if node.module else []))
            else:
                target = node.module or ""
            if not (target == OLD or target.startswith(OLD + ".")):
                continue
            goes = [a for a in node.names if moved_prefix(target) or f"{target}.{a.name}" in MOVED]
            stays = [a for a in node.names if a not in goes]
            if not goes:
                continue
            if node.level and moved_together and not stays:
                continue                                   # between modules that moved together the relative import still reaches its target
            if node.level and moved_together and stays:
                raise SystemExit(f"{path}:{node.lineno}: a moved module imports {target} {[a.name for a in stays]}, which stays")
            indent = " " * node.col_offset

            def stmt(mod, names):
                return indent + f"from {mod} import " + ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in names)
            out = []
            if stays:
                out.append(stmt(("." * node.level + (node.module or "")) if node.level else target, stays))
            out.append(stmt(to_new(target), goes))
            edits.append((node.lineno, node.end_lineno, "\n".join(out)))
        elif isinstance(node, ast.Import):
            if any(moved_prefix(a.name) for a in node.names):
                indent = " " * node.col_offset
                out = indent + "import " + ", ".join((to_new(a.name) if moved_prefix(a.name) else a.name) + (f" as {a.asname}" if a.asname else "") for a in node.names)
                edits.append((node.lineno, node.end_lineno, out))
    lines = text.split("\n")
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start - 1:end] = replacement.split("\n")
    result = "\n".join(lines)

    def swap(m):
        pre = moved_prefix(m.group(0))
        return to_new(pre) + m.group(0)[len(pre):] if pre else m.group(0)
    result = re.sub(r"erp_agents(?:\.\w+)+", swap, result)                 # strings, comments and docstrings that name a moved module
    if result == text:
        return False
    path.write_bytes((result.replace("\n", "\r\n") if "\r\n" in raw else result).encode("utf-8"))
    return True


def main(dry: bool) -> None:
    move_files(dry)
    if dry:
        return
    changed = 0
    this = Path(__file__).resolve()
    for root in (REPO / "src", REPO / "tests", REPO / "tools", REPO / "engine" / "src"):
        for path in sorted(root.rglob("*.py")):
            if path.resolve() != this and rewrite(path):
                changed += 1
    conftest = REPO / "conftest.py"
    changed += int(rewrite(conftest))
    print(changed, "files rewritten")


if __name__ == "__main__":
    main("--dry-run" in sys.argv)

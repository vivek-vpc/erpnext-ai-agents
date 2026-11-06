"""The rules that make core + packs real, checked on the source itself:

  * the core never imports a pack (it finds packs by folder and loads one by name through `PackRegistry`);
  * a pack imports the core and itself, never another pack, and nothing else of the application;
  * a skeleton pack has no code;
  * every active pack has an entry object, a manifest and a README.
"""
import ast
from pathlib import Path

import pytest

from ai_munimji_engine.core.packs import PackRegistry

SRC = Path(__file__).resolve().parent.parent / "src" / "erp_agents"
CORE = SRC / "core"
ENGINE_CORE = SRC.parent.parent / "engine" / "src" / "ai_munimji_engine" / "core"            # the part of the core that moved into the engine keeps its place in the tree
PACKS = SRC / "packs"


def module_name(path: Path) -> list[str]:
    parts = list(path.relative_to(SRC.parent).with_suffix("").parts)
    return parts[:-1] if parts[-1] == "__init__" else parts


def imported(path: Path) -> list[tuple[int, str]]:
    """(line, absolute dotted module) for every import in the file, relative ones resolved."""
    package = module_name(path) if path.name == "__init__.py" else module_name(path)[:-1]
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                mod = ".".join(base + (node.module.split(".") if node.module else []))
            else:
                mod = node.module or ""
            out.append((node.lineno, mod))
            out += [(node.lineno, f"{mod}.{a.name}") for a in node.names]       # `from erp_agents import packs` and the like
    return out


def python_files(folder: Path) -> list[Path]:
    return [p for p in folder.rglob("*.py") if "__pycache__" not in p.parts and "tests" not in p.relative_to(folder).parts]


def test_the_core_never_imports_a_pack():
    offenders = [f"{p.relative_to(SRC)}:{line} imports {mod}" for p in python_files(CORE) for line, mod in imported(p) if mod == "erp_agents.packs" or mod.startswith("erp_agents.packs.")]
    assert not offenders, "the core must not import a pack (use PackRegistry and the Pack hooks):\n" + "\n".join(offenders)


@pytest.mark.parametrize("pack", sorted(p.name for p in PACKS.iterdir() if p.is_dir() and not p.name.startswith("_")))
def test_a_pack_imports_only_the_core_and_itself(pack):
    own = f"erp_agents.packs.{pack}"
    offenders = []
    for path in python_files(PACKS / pack):
        for line, mod in imported(path):
            if not mod.startswith("erp_agents"):
                continue
            if mod == own or mod.startswith(own + ".") or mod == "erp_agents.core" or mod.startswith("erp_agents.core."):
                continue
            if mod in ("erp_agents", "erp_agents.packs"):                       # the parent packages of an import that is checked on its own line
                continue
            offenders.append(f"{path.relative_to(SRC)}:{line} imports {mod}")
    assert not offenders, f"pack {pack} may import the core and itself only (another pack's data goes through a tool call):\n" + "\n".join(offenders)


def test_a_skeleton_pack_has_no_code():
    registry = PackRegistry.discover()
    for name, manifest in registry.manifests.items():
        if manifest.status != "skeleton":
            continue
        code = [p.name for p in python_files(registry.roots[name]) if p.name != "__init__.py"]
        assert not code, f"skeleton pack {name} must hold no logic, but has {code}"
        assert (registry.roots[name] / "__init__.py").read_text(encoding="utf-8").strip() == ""


def test_an_active_pack_has_its_entry_object_manifest_and_readme():
    registry = PackRegistry.discover()
    actives = [n for n, m in registry.manifests.items() if m.status == "active"]
    assert "stock_procurement" in actives
    for name in actives:
        root = registry.roots[name]
        assert (root / "pack.py").is_file() and (root / "manifest.yaml").is_file() and (root / "README.md").is_file(), name
        pack = registry.load(name)
        assert pack.manifest == registry.manifests[name]


def test_the_packs_package_itself_holds_nothing_but_packs():
    stray = [p.name for p in PACKS.iterdir() if p.is_file() and p.name != "__init__.py"]
    assert stray == [], f"loose files in packs/: {stray}"


def test_the_core_has_no_hard_wired_stock_names_in_its_registries():
    """The few places that used to name the stock pack's things are fed by the pack now. These strings must not come back into the core's code."""
    banned = ["inventory.alert", "procurement.order", "stock_overview", "prepare_order_request", "agent_runs", "stock_notices", "assistant_answers", "chat_log"]
    hits = []
    for path in (*python_files(CORE), *python_files(ENGINE_CORE)):
        text = path.read_text(encoding="utf-8")
        hits += [f"{path.name}: {word}" for word in banned if word in text and path.name not in ("doctor.py",)]
    assert not hits, "stock-specific names in the core:\n" + "\n".join(hits)

"""The stock pack's manifest ships inside the engine (so the standalone service and the Frappe app read one file), and the service keeps working with it there."""
from pathlib import Path

from ai_munimji_engine.core.packs import PackRegistry, engine_manifest_path
from ai_munimji_engine.packs.stock_procurement.manifest import MANIFEST_PATH, manifest

ROOT = Path(__file__).resolve().parent.parent
PACK_FOLDER = ROOT / "src" / "erp_agents" / "packs" / "stock_procurement"


def test_the_manifest_is_a_file_of_the_engine_and_parses():
    assert MANIFEST_PATH == engine_manifest_path("stock_procurement")
    assert MANIFEST_PATH.is_file() and MANIFEST_PATH.relative_to(ROOT).as_posix() == "engine/src/ai_munimji_engine/packs/stock_procurement/manifest.yaml"
    m = manifest()
    assert (m.name, m.status, m.enabled_by_default) == ("stock_procurement", "active", True)
    assert len(m.tools) == 17 and {t.effect for t in m.tools} == {"read", "prepare"}


def test_it_is_no_longer_beside_the_standalone_pack():
    assert not (PACK_FOLDER / "manifest.yaml").exists(), "two copies of the manifest would be two sources of truth"


def test_the_service_finds_and_loads_the_pack_with_the_engines_manifest():
    registry = PackRegistry.discover()
    assert registry.manifests["stock_procurement"] == manifest()
    assert registry.roots["stock_procurement"] == PACK_FOLDER             # the code is still the service's; only the declaration moved
    assert registry.load("stock_procurement").manifest == manifest()


def test_the_skeleton_packs_keep_their_manifests_where_they_were():
    registry = PackRegistry.discover()
    for name in ("sales", "finance"):
        assert (registry.roots[name] / "manifest.yaml").is_file()
        assert not engine_manifest_path(name).exists()


def test_both_wheels_carry_it_as_package_data():
    for toml in (ROOT / "pyproject.toml", ROOT / "engine" / "pyproject.toml"):
        assert 'ai_munimji_engine = ["packs/*/manifest.yaml"]' in toml.read_text(encoding="utf-8"), toml.name

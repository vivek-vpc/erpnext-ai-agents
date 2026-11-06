"""The pack mechanism of the core: manifests are validated without importing the pack, packs are found by folder, switched on and off by a setting, and what a pack
declares is checked against what it registers."""
from pathlib import Path

import pytest
import yaml

from ai_munimji_engine.core.packs import PackError, PackRegistry, PackSettings, load_manifest, parse_manifest

GOOD = {"name": "demo", "version": "1.0.0", "title": "Demo", "summary": "A demo pack.", "tools": [{"name": "demo_look", "role": "viewer", "effect": "read"}],
        "approvals": [{"kind": "demo.thing", "actions": ["ack"], "role": "approver", "description": "A thing."}], "settings": [{"section": "demo", "description": "x"}]}


def make(tmp_path, name="demo", **over):
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    data = {**GOOD, "name": name, **over}
    (folder / "manifest.yaml").write_text(yaml.safe_dump({k: v for k, v in data.items() if v is not None}), encoding="utf-8")
    return folder


def test_a_good_manifest_is_read_completely():
    m = parse_manifest(GOOD, "demo")
    assert m.name == "demo" and m.status == "active" and m.enabled_by_default is True and m.depends_on == ()
    assert m.tool_names == ("demo_look",) and m.tools[0].role == "viewer" and m.approvals[0].kind == "demo.thing" and m.settings[0].section == "demo"


@pytest.mark.parametrize("change,message", [
    ({"name": "Demo Pack"}, "name"), ({"name": "x"}, "name"), ({"version": "one"}, "version"), ({"version": None}, "'version' is required"), ({"title": ""}, "'title' is required"),
    ({"status": "beta"}, "status"), ({"enabled_by_default": "yes"}, "enabled_by_default"), ({"bogus": 1}, "unknown manifest key"),
    ({"tools": [{"name": "tool_a", "role": "viewer"}]}, "name, role and effect"), ({"tools": [{"name": "tool_a", "role": "root", "effect": "read"}]}, "role must be one of"),
    ({"tools": [{"name": "tool_a", "role": "viewer", "effect": "order"}]}, "no tool orders, approves, pays or changes a setting"),
    ({"tools": [{"name": "Bad Name", "role": "viewer", "effect": "read"}]}, "not valid"),
    ({"tools": [{"name": "tool_a", "role": "viewer", "effect": "read"}, {"name": "tool_a", "role": "viewer", "effect": "read"}]}, "declared twice"),
    ({"approvals": [{"kind": "nodot", "actions": ["a"], "role": "approver", "description": "d"}]}, "area.thing"),
    ({"approvals": [{"kind": "a.b", "actions": [], "role": "approver", "description": "d"}]}, "actions"),
    ({"approvals": [{"kind": "a.b", "actions": ["x"], "role": "boss", "description": "d"}]}, "role must be one of"),
    ({"settings": [{"section": "s"}]}, "section and description"), ({"erp_permissions": [["Item"]]}, "pairs"), ({"erp_roles": "Purchase User"}, "list of non-empty strings"),
])
def test_a_manifest_that_breaks_a_rule_is_refused_with_the_reason(change, message):
    with pytest.raises(PackError, match=message):
        parse_manifest({**GOOD, **change} if all(v is not None for v in change.values()) else {k: v for k, v in {**GOOD, **change}.items() if v is not None}, "demo/manifest.yaml")


def test_a_pack_cannot_depend_on_another_pack():
    with pytest.raises(PackError, match="depends only on the core, never on another pack"):
        parse_manifest({**GOOD, "depends_on": ["stock_procurement"]}, "demo")


def test_a_skeleton_declares_nothing():
    skeleton = {"name": "empty", "version": "0.0.1", "title": "Empty", "summary": "Nothing.", "status": "skeleton", "enabled_by_default": False}
    assert parse_manifest(skeleton, "empty").tools == ()
    for extra in ({"tools": GOOD["tools"]}, {"approvals": GOOD["approvals"]}, {"settings": GOOD["settings"]}, {"erp_roles": ["Stock User"]}, {"sends_to_model": ["items"]}):
        with pytest.raises(PackError, match="skeleton pack has no logic"):
            parse_manifest({**skeleton, **extra}, "empty")


def test_the_folder_name_must_match_the_manifest(tmp_path):
    folder = make(tmp_path, "demo")
    (folder / "manifest.yaml").write_text(yaml.safe_dump({**GOOD, "name": "other"}), encoding="utf-8")
    with pytest.raises(PackError, match="folder is 'demo'"):
        load_manifest(folder / "manifest.yaml")
    (folder / "manifest.yaml").write_text("name: [unclosed", encoding="utf-8")
    with pytest.raises(PackError, match="YAML"):
        load_manifest(folder / "manifest.yaml")


def test_packs_are_found_by_folder_without_importing_them(tmp_path):
    make(tmp_path, "alpha", tools=[{"name": "alpha_look", "role": "viewer", "effect": "read"}])
    make(tmp_path, "beta", tools=[{"name": "beta_look", "role": "viewer", "effect": "read"}])
    (tmp_path / "not_a_pack").mkdir()
    (tmp_path / "_private").mkdir()
    reg = PackRegistry.discover(directory=tmp_path)
    assert list(reg.manifests) == ["alpha", "beta"] and not reg.loaded                                  # nothing was imported: there is no code in these folders


def test_a_tool_name_cannot_belong_to_two_packs(tmp_path):
    make(tmp_path, "alpha")
    make(tmp_path, "beta")
    with pytest.raises(PackError, match="declared by both alpha and beta"):
        PackRegistry.discover(directory=tmp_path)


def test_the_real_packs_are_found_and_valid():
    reg = PackRegistry.discover()
    assert {"stock_procurement", "sales", "finance"} <= set(reg.manifests)
    stock = reg.manifests["stock_procurement"]
    assert stock.status == "active" and stock.enabled_by_default and {t.name for t in stock.tools} >= {"stock_overview", "prepare_order_request"}
    prepares = [t for t in stock.tools if t.effect == "prepare"]                                            # the only tools with an effect only prepare an approval request
    assert [t.name for t in prepares] == ["prepare_order_request", "prepare_quote_request", "prepare_stock_transfer"] and {t.role for t in prepares} == {"approver"}
    for name in ("sales", "finance"):
        m = reg.manifests[name]
        assert m.status == "skeleton" and not m.enabled_by_default and not m.tools and not m.approvals and not m.settings
        assert not (reg.roots[name] / "pack.py").exists()                                                  # no logic
        assert (reg.roots[name] / "README.md").is_file()


def test_which_packs_are_on_follows_the_setting_live(tmp_path):
    make(tmp_path, "alpha", tools=[{"name": "alpha_look", "role": "viewer", "effect": "read"}])
    make(tmp_path, "beta", tools=[{"name": "beta_look", "role": "viewer", "effect": "read"}], enabled_by_default=False)
    reg = PackRegistry.discover(directory=tmp_path)
    flags = {}
    reg.bind_settings(lambda: PackSettings(enabled=dict(flags)))
    assert reg.enabled_names() == ["alpha"]                                                                 # defaults
    flags.update(beta=True, alpha=False)
    assert reg.enabled_names() == ["beta"] and reg.is_enabled("beta") and not reg.is_enabled("alpha")       # read each time: no restart to switch one off
    assert not reg.is_enabled("nothing")
    flags["ghost"] = True
    assert reg.unknown_in_settings() == ["ghost"]


def test_the_settings_refuse_something_that_is_not_a_pack_name():
    with pytest.raises(ValueError, match="not a pack name"):
        PackSettings(enabled={"Not A Name": True})
    with pytest.raises(ValueError):
        PackSettings(enabled={"ok": True}, extra_field=1)


def test_what_a_pack_registers_must_be_what_it_declared(tmp_path):
    make(tmp_path, "alpha", tools=[{"name": "alpha_look", "role": "viewer", "effect": "read"}, {"name": "alpha_do", "role": "approver", "effect": "prepare"}])
    reg = PackRegistry.discover(directory=tmp_path)
    reg.check_registered("alpha", {"alpha_look": ("viewer", "read"), "alpha_do": ("approver", "prepare")})
    with pytest.raises(PackError, match="declared but not registered: alpha_do"):
        reg.check_registered("alpha", {"alpha_look": ("viewer", "read")})
    with pytest.raises(PackError, match="registered but not declared: sneaky"):
        reg.check_registered("alpha", {"alpha_look": ("viewer", "read"), "alpha_do": ("approver", "prepare"), "sneaky": ("viewer", "read")})
    with pytest.raises(PackError, match="role or effect differs: alpha_do"):
        reg.check_registered("alpha", {"alpha_look": ("viewer", "read"), "alpha_do": ("viewer", "prepare")})              # a pack cannot lower the role it declared


def test_a_skeleton_cannot_be_loaded_and_an_unknown_pack_is_refused():
    reg = PackRegistry.discover()
    with pytest.raises(PackError, match="skeleton"):
        reg.load("sales")
    with pytest.raises(PackError, match="no pack named"):
        reg.load("nope")


def test_what_the_enabled_packs_need_from_erpnext_is_the_union_of_their_manifests():
    reg = PackRegistry.discover()
    reg.bind_settings(lambda: PackSettings(enabled={}))
    assert ("Purchase Order", "create") in reg.erp_permissions() and len(reg.erp_permissions()) == len(set(reg.erp_permissions()))
    assert reg.erp_capabilities()["decide"] == [("Purchase Order", "create")] and ("Item", "read") in reg.erp_capabilities()["look"]
    reg.bind_settings(lambda: PackSettings(enabled={"stock_procurement": False}))
    assert reg.erp_permissions() == [] and reg.erp_capabilities() == {"look": [], "decide": []} and reg.privacy_lines() == []


def test_the_sign_in_defaults_match_what_the_stock_manifest_declares():
    """core/auth/erpnext.py keeps the stock pack's DocTypes as defaults so a bare ErpNextLogin behaves as it always did; the manifest is the source of truth, so they must agree."""
    from erp_agents.core.auth.erpnext import DEFAULT_DECIDE, DEFAULT_LOOK
    m = PackRegistry.discover().manifests["stock_procurement"]
    assert tuple(d for d, p in m.erp_look if p == "read") == DEFAULT_LOOK and tuple(d for d, p in m.erp_decide if p == "create") == DEFAULT_DECIDE


def test_the_application_builds_its_sign_in_from_the_enabled_packs(make_app, clock):
    from erp_agents.core.erp.mock import MockERP
    from erp_agents.core.auth.erpnext import ErpNextLogin
    app = make_app(erp=MockERP(today=clock().date()))
    wanted = app.packs.erp_capabilities()
    login = ErpNextLogin("http://erp.local", look=tuple(d for d, p in wanted["look"] if p == "read"), decide=tuple(d for d, p in wanted["decide"] if p == "create"))
    assert login.look == ("Item", "Purchase Order") and login.decide == ("Purchase Order",)
    app.settings.update_section(__import__("conftest").OWNER, "packs", {"enabled": {"stock_procurement": False}})
    wanted = app.packs.erp_capabilities()
    assert wanted == {"look": [], "decide": []}                                          # nothing is switched on: only an administrator could look

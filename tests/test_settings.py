import pytest

from ai_munimji_engine.core.settings_store import PermissionDenied, SettingsError, SettingsStore
from erp_agents.stores import build_core_stores
from ai_munimji_engine.packs.stock_procurement.settings import attach

from conftest import OWNER, VIEWER, storage_kind


@pytest.fixture
def store():
    return attach(SettingsStore(build_core_stores(storage_kind()).settings), base_policy={"min_days_cover": 7})


def test_precedence_config_global_group_item(store):
    assert store.effective_policy("A", "G").min_days_cover == 7
    store.update_policy(OWNER, "global", "*", {"min_days_cover": 10})
    store.update_policy(OWNER, "group", "Fasteners", {"min_days_cover": 14})
    store.update_policy(OWNER, "item", "BOLT", {"min_days_cover": 20})
    assert store.effective_policy("OTHER", "Paints").min_days_cover == 10          # global
    assert store.effective_policy("OTHER", "Fasteners").min_days_cover == 14       # group beats global
    assert store.effective_policy("BOLT", "Fasteners").min_days_cover == 20        # item beats group


def test_sources_explain_where_a_value_came_from(store):
    store.update_policy(OWNER, "group", "Fasteners", {"min_qty": 500})
    _, sources = store.policy_with_sources("BOLT", "Fasteners")
    assert sources["min_qty"] == "group 'Fasteners' override"
    assert sources["min_days_cover"] == "config file"


def test_null_switches_a_threshold_off_and_clear_restores_inheritance(store):
    store.update_policy(OWNER, "global", "*", {"min_qty": 100})
    store.update_policy(OWNER, "item", "BOLT", {"min_qty": None})
    assert store.effective_policy("BOLT").min_qty is None                  # explicitly off for this item
    assert store.effective_policy("OTHER").min_qty == 100
    store.update_policy(OWNER, "item", "BOLT", clear=["min_qty"])
    assert store.effective_policy("BOLT").min_qty == 100                   # inherited again


@pytest.mark.parametrize("values", [
    {"min_qty": -5},
    {"min_days_cover": 0},
    {"lookahead_days": 0},
    {"recent_window_days": 90, "base_window_days": 60},
    {"usage_method": "magic"},
    {"recent_weight": 1.5},
])
def test_invalid_values_are_rejected(store, values):
    with pytest.raises(SettingsError):
        store.update_policy(OWNER, "global", "*", values)
    assert store.get_overrides("global", "*") == {}      # nothing half-saved


def test_unknown_field_rejected(store):
    with pytest.raises(SettingsError, match="Unknown"):
        store.update_policy(OWNER, "global", "*", {"min_quantity": 5})


def test_only_admins_can_change_settings(store):
    with pytest.raises(PermissionDenied):
        store.update_policy(VIEWER, "global", "*", {"min_qty": 1})
    with pytest.raises(PermissionDenied):
        store.update_system(VIEWER, {"max_alerts_per_run": 5})
    assert store.get_overrides("global", "*") == {}


def test_audit_trail_records_who_changed_what(store):
    store.update_policy(OWNER, "global", "*", {"min_days_cover": 10})
    store.update_policy(OWNER, "global", "*", {"min_days_cover": 12})
    rows = store.audit()
    assert [(r["field"], r["old"], r["new"], r["actor"]) for r in rows] == [
        ("min_days_cover", 10, 12, "owner"),
        ("min_days_cover", "<inherited>", 10, "owner"),
    ]


def test_system_settings_validation_and_change_listener(store):
    seen = []
    store.on_change(lambda scope, key: seen.append(scope))
    store.update_system(OWNER, {"check_cron": "30 5 * * *"})
    assert store.system().check_cron == "30 5 * * *" and seen == ["system"]
    with pytest.raises(SettingsError):
        store.update_system(OWNER, {"check_cron": "every morning"})


def test_reset_scope_removes_all_overrides(store):
    store.update_policy(OWNER, "item", "BOLT", {"min_qty": 5, "safety_stock_days": 1})
    store.reset_scope(OWNER, "item", "BOLT")
    assert store.get_overrides("item", "BOLT") == {}
    assert store.effective_policy("BOLT").min_qty is None


def test_bad_yaml_defaults_fail_fast():
    with pytest.raises(Exception):
        attach(SettingsStore(build_core_stores(storage_kind()).settings), base_policy={"min_qty": -1})

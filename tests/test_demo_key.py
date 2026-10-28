"""Finding F-11 (review of 25 Oct 2026): `config/settings.yaml` fell back to the well-known key `demo-owner-token`, and nothing stopped the program from starting with it.

The key was already refused from any other machine, through a tunnel or a proxy (tests/test_hardening.py). Now the program also refuses to START with a well-known key unless
the settings say, in so many words, `auth.allow_demo_key: true`; and that flag is accepted only with the built-in demo ERP, never with a real ERPNext. The demo
(`config/settings.yaml`, the Docker demo) keeps working.
"""
import copy
import shutil
from pathlib import Path

import pytest
import yaml

from erp_agents.core import doctor
from erp_agents.core.bootstrap import StartupRefused, build_app
from erp_agents.core.cli import main
from erp_agents.core.config import load_config

ROOT = Path(__file__).resolve().parent.parent
WELL_KNOWN = ["demo-owner-token", "change-me", "changeme", "Demo-Owner-Token"]
STRONG = "k" * 40


def tokens(token):
    return {"tokens": [{"token": token, "user": "owner", "roles": ["admin", "approver"]}]}


@pytest.mark.parametrize("token", WELL_KNOWN)
def test_the_program_will_not_start_with_a_well_known_key_unless_the_demo_flag_is_set(make_app, token):
    with pytest.raises(StartupRefused) as err:
        make_app(cfg={"auth": tokens(token)})
    text = str(err.value)
    assert "owner" in text and "allow_demo_key" in text and "ERP_AGENTS_OWNER_TOKEN" in text
    assert token.lower() not in text.lower()                                       # the message names the user, never the key


def test_the_flag_lets_the_demo_start_with_the_built_in_demo_erp(make_app):
    app = make_app(cfg={"auth": {**tokens("demo-owner-token"), "allow_demo_key": True}})
    assert "demo-owner-token" in app.tokens


def test_the_flag_is_not_accepted_with_a_real_erpnext(make_app):
    from erp_agents.core.erp.mock import MockERP

    real = {"type": "erpnext", "url": "https://erp.example.com", "api_key": "k", "api_secret": "s"}
    with pytest.raises(StartupRefused) as err:
        make_app(erp=MockERP(), cfg={"erp": real, "auth": {**tokens("demo-owner-token"), "allow_demo_key": True}})
    assert "erpnext" in str(err.value).lower() and "demo" in str(err.value).lower()


def test_a_short_key_that_is_not_well_known_still_starts_and_stays_local_only(make_app):
    assert make_app(cfg={"auth": tokens("owner-secret-1")}).tokens                  # the old rule: allowed, warned about, accepted only on the server machine


def test_a_long_random_key_needs_no_flag(make_app):
    assert STRONG in make_app(cfg={"auth": tokens(STRONG)}).tokens


def test_the_flag_must_be_a_real_true_not_a_word(make_app):
    for value in ("yes", "true", 1, "False", None):
        with pytest.raises(StartupRefused):
            make_app(cfg={"auth": {**tokens("demo-owner-token"), "allow_demo_key": value}})


# ---------------------------------------------------------------------------------------------------- the demos that ship with the project
@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A folder with no .env of its own: the loader looks in the settings file's project folder and the current folder."""
    monkeypatch.chdir(tmp_path)
    for name in ("ERP_AGENTS_OWNER_TOKEN", "ERP_AGENTS_DATA", "ERP_AGENTS_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_the_local_demo_settings_still_start_and_say_so_in_words(isolated):
    (isolated / "config").mkdir()
    shutil.copy(ROOT / "config" / "settings.yaml", isolated / "config" / "settings.yaml")
    cfg = copy.deepcopy(load_config(str(isolated / "config" / "settings.yaml")))
    cfg["database"]["path"] = str(isolated / "demo.db")
    assert cfg["auth"]["allow_demo_key"] is True
    assert "demo-owner-token" in build_app(config=cfg).tokens


def test_the_docker_demo_settings_start_with_the_key_the_entrypoint_makes(isolated, monkeypatch):
    monkeypatch.setenv("ERP_AGENTS_DATA", str(isolated))
    monkeypatch.setenv("ERP_AGENTS_OWNER_TOKEN", STRONG)
    cfg = copy.deepcopy(load_config(str(ROOT / "docker" / "settings.demo.yaml")))
    assert not cfg["auth"].get("allow_demo_key")                                    # the Docker demo never needs the flag
    assert STRONG in build_app(config=cfg).tokens


def test_the_erpnext_settings_have_no_fallback_key():
    text = (ROOT / "config" / "settings.erpnext.yaml").read_text(encoding="utf-8")
    assert "demo-owner-token" not in text and "allow_demo_key" not in text


def test_every_settings_file_in_the_project_is_either_keyless_or_flagged():
    for path in (ROOT / "config").glob("*.yaml"):
        raw = path.read_text(encoding="utf-8")
        if "demo-owner-token" in raw:
            assert (yaml.safe_load(raw.replace("${ERP_AGENTS_OWNER_TOKEN:-demo-owner-token}", "x")) or {}).get("auth", {}).get("allow_demo_key") is True, path.name


# ---------------------------------------------------------------------------------------------------- what a person sees
def test_the_command_line_says_it_plainly_and_exits_instead_of_printing_a_traceback(isolated, capsys):
    settings = {"database": {"path": str(isolated / "x.db")}, "erp": {"type": "mock"}, "channels": {"console": {"enabled": False}},
                "approvals": {"recipients": []}, "auth": tokens("demo-owner-token")}
    path = isolated / "settings.yaml"
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    assert main(["--config", str(path), "run-once", "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "will not start" in err and "allow_demo_key" in err and "demo-owner-token" not in err and "Traceback" not in err


def cfg_for(tmp_path, **parts):
    base = {"database": {"path": str(tmp_path / "data" / "app.db")}, "erp": {"type": "mock"}, "auth": {}, "server": {}, "approvals": {"recipients": []}}
    base.update(parts)
    return base


def sign_in(checks):
    return [c for c in checks if c.area == "Sign-in"]


def test_doctor_fails_a_well_known_key_without_the_flag_and_does_not_print_it(tmp_path):
    checks = doctor.check_access(cfg_for(tmp_path, auth=tokens("demo-owner-token")))
    (c,) = [c for c in sign_in(checks) if "well-known" in c.title]
    assert c.status == doctor.FAIL and "owner" in c.detail and "allow_demo_key" in c.fix
    assert "demo-owner-token" not in repr([x.__dict__ for x in checks])


def test_doctor_accepts_the_flag_on_the_demo_erp_and_fails_it_on_a_real_one(tmp_path):
    ok = doctor.check_access(cfg_for(tmp_path, auth={**tokens("demo-owner-token"), "allow_demo_key": True}))
    assert [c.status for c in sign_in(ok) if "well-known" in c.title] == [doctor.INFO]
    real = doctor.check_access(cfg_for(tmp_path, erp={"type": "erpnext"}, auth={**tokens("demo-owner-token"), "allow_demo_key": True}))
    assert [c.status for c in sign_in(real) if "well-known" in c.title] == [doctor.FAIL]


def test_doctor_has_nothing_to_say_about_a_long_random_key(tmp_path):
    assert not [c for c in sign_in(doctor.check_access(cfg_for(tmp_path, auth=tokens(STRONG)))) if "well-known" in c.title]

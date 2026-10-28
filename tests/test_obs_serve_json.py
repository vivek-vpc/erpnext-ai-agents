"""`serve` in the JSON log format writes JSON from its first line to its last (Phase 2 of the hardening brief). Found by starting the real server with `log_format: json` in the settings
file: the lines written while the application was still being built, and the notices `serve` prints about remote access, were plain text, which a log collector cannot parse."""
import json
import logging

import pytest

import erp_agents.core.cli as cli

STRONG = "owner-key-0123456789abcdefghij"


@pytest.fixture
def logging_restored():
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    yield
    root.handlers = saved
    root.setLevel(level)


def settings(tmp_path, fmt=None, extra=""):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(f"database: {{path: '{(tmp_path / 'x.db').as_posix()}'}}\nerp: {{type: mock}}\nauth:\n  access_keys: anywhere\n  tokens:\n    - {{token: '{STRONG}', user: owner, roles: [admin]}}\n"
                   + (f"observability:\n  log_format: {fmt}\n" if fmt else "") + extra, encoding="utf-8")
    return str(cfg)


def serve(argv, monkeypatch, capsys):
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    assert cli.main(argv) == 0
    out = capsys.readouterr()
    return [l for l in (out.out + out.err).splitlines() if l.strip()]


def test_with_json_in_the_settings_file_every_line_serve_writes_is_one_json_object(tmp_path, monkeypatch, capsys, logging_restored):
    lines = serve(["--config", settings(tmp_path, "json"), "serve"], monkeypatch, capsys)
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except ValueError:
            pytest.fail(f"a line that is not JSON: {line[:120]!r}")
    messages = " | ".join(p["msg"] for p in parsed)
    assert "pack stock_procurement" in messages                          # written while the application is built: before, this one was plain text
    assert "Remote access:" in messages and "auth.access_keys is 'anywhere'" in messages        # printed by serve itself
    assert all(p["level"] in ("INFO", "WARNING") for p in parsed)


def test_with_the_flag_every_line_is_json_too(tmp_path, monkeypatch, capsys, logging_restored):
    lines = serve(["--log-format", "json", "--config", settings(tmp_path), "serve"], monkeypatch, capsys)
    assert lines and all(json.loads(l) for l in lines)


def test_without_it_the_text_format_and_the_printed_notices_are_as_they_were(tmp_path, monkeypatch, capsys, logging_restored):
    lines = serve(["--config", settings(tmp_path), "serve"], monkeypatch, capsys)
    printed = [l for l in lines if l.startswith(("Remote access:", "WARNING: auth.access_keys"))]
    assert len(printed) == 2                                             # print(), exactly as before
    with pytest.raises(ValueError):
        json.loads(printed[0])


def test_a_wrong_format_in_the_settings_file_is_still_reported_in_words(tmp_path, monkeypatch, capsys, logging_restored):
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    assert cli.main(["--config", settings(tmp_path, "xml"), "serve"]) == 2
    assert "observability.log_format is 'xml'" in capsys.readouterr().err

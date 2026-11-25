import pytest
from erp_agents.core.cli import main

# @pytest.fixture：把demo_config注册成pytest可以按名字提供的测试依赖
#       后面测试函数的参数里写了demo_config，
#       pytest 就会先调用这个 fixture，再把它返回的 cfg 传给测试函数
@pytest.fixture
def demo_config(tmp_path):
    # 'tmp_path' 也是 fixture，由 pytest 内置提供
    # tmp_path / "settings.yaml" 是 pathlib 拼接路径的写法
    cfg = tmp_path / "settings.yaml"
    db = tmp_path / "stock.db"
    cfg.write_text(
        f"database: {{path: '{db.as_posix()}'}}\nerp: {{type: mock}}\n",
        encoding="utf-8",
    )
    return cfg

def test_analyze_prints_stock_analysis(demo_config, capsys):
    result = main(["--config", str(demo_config), "analyze", "BOLT-M8"])

    assert result == 0
    # capsys.readouterr()：是pytest用来取出到目前位置捕获的终端输出的方法
    output = capsys.readouterr().out
    assert "BOLT-M8" in output
    assert "on hand" in output

def test_analyze_reports_unknown_item(demo_config, capsys):
    result = main(["--config", str(demo_config), "analyze", "NOT-A-REAL-ITEM"])

    assert result == 1
    assert "Unknown item 'NOT-A-REAL-ITEM'" in capsys.readouterr().out

def test_policy_requires_key_for_item_scope(demo_config, capsys):
    result = main(["--config", str(demo_config), "policy", "show", "--scope", "item"])

    assert result == 2
    output = capsys.readouterr().out
    assert "--key is required for group/item scope" in output

def test_policy_requires_key_for_group_scope(demo_config, capsys):
    result = main(["--config", str(demo_config), "policy", "show", "--scope", "group"])

    assert result == 2
    output = capsys.readouterr().out
    assert "--key is required for group/item scope" in output

def test_policy_clear_removes_only_requested_override(demo_config, capsys):
    result = main(["--config", str(demo_config), "policy", "set", "--scope", "item", "--key=BOLT-M8", "min_qty=300", "min_days_cover=14"])
    assert result == 0

    result = main(["--config", str(demo_config), "policy", "clear", "--scope", "item", "--key=BOLT-M8", "min_qty"])
    assert result == 0

    result = main(["--config", str(demo_config), "policy", "show", "--scope", "item", "--key=BOLT-M8"])
    assert result == 0
    output = capsys.readouterr().out
    assert '"min_qty":' not in output
    assert '"min_days_cover": 14\n' in output

def test_policy_reset_removes_all_item_overrides(demo_config, capsys):
    result = main(["--config", str(demo_config), "policy", "set", "--scope", "item", "--key=BOLT-M8", "min_qty=300", "min_days_cover=14"])
    assert result == 0

    result = main(["--config", str(demo_config), "policy", "show", "--scope", "item", "--key=BOLT-M8"])
    assert result == 0
    output = capsys.readouterr().out
    assert '"min_qty": 300' in output
    assert '"min_days_cover": 14' in output

    result = main(["--config", str(demo_config), "policy", "reset", "--scope", "item", "--key=BOLT-M8"])
    assert result == 0

    result = main(["--config", str(demo_config), "policy", "show", "--scope", "item", "--key=BOLT-M8"])
    assert result == 0
    assert "overrides: {}" in capsys.readouterr().out

def test_alerts_lists_generated_alerts(demo_config, capsys):
    result = main(["--config", str(demo_config), "run-once", "--item", "BOLT-M8"])
    assert result == 0
    capsys.readouterr()

    result = main(["--config", str(demo_config), "alerts"])
    assert result == 0
    output = capsys.readouterr().out
    assert "BOLT-M8" in output

def test_analyze_shows_item_policy_override_source(demo_config, capsys):
    result = main(["--config", str(demo_config), "policy", "set", "--scope", "item", "--key=BOLT-M8", "min_qty=300"])
    assert result == 0

    result = main(["--config", str(demo_config), "analyze", "BOLT-M8"])
    assert result == 0
    output = capsys.readouterr().out
    assert "settings in force: min_qty (item 'BOLT-M8' override)" in output

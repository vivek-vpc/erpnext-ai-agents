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
    output = capsys.readouterr().out
    assert "BOLT-M8" in output
    assert "on hand" in output

def test_analyze_resports_unknown_item(demo_config, capsys):
    result = main(["--config", str(demo_config), "analyze", "NOT-A-REAL-ITEM"])

    assert result == 1
    assert "Unknown item 'NOT-A-REAL-ITEM'" in capsys.readouterr().out
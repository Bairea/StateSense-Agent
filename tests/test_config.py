from pathlib import Path

import pytest

from statesense.config import ConfigError, load_config

REPO = Path(__file__).resolve().parents[1]

MINIMAL = """
[screenpipe]
base_url = "http://localhost:3030"

[gate]
ratio_min = 0.75

[taxonomy]
entertainment = ["bilibili"]

[[actions]]
id = "walk5"
text = "离开电脑走 5 分钟"
applies_to = ["PASSIVE_CONSUMPTION"]
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_loads_example_config_shipped_with_repo():
    cfg = load_config(REPO / "config" / "config.example.toml")
    assert cfg.screenpipe.base_url == "http://localhost:3030"
    assert cfg.gate.ratio_min == 0.75
    assert cfg.schedule.window_minutes == 60
    assert cfg.taxonomy.entertainment
    assert cfg.actions


def test_utf8_bom_is_tolerated(tmp_path):
    """Windows 记事本默认写 UTF-8 BOM，而 tomllib 会报「Invalid statement（第 1 行第 1 列）」。

    实测踩到过：位置指向文件开头，用户完全看不出原因。
    """
    p = tmp_path / "config.toml"
    p.write_bytes(b"\xef\xbb\xbf" + MINIMAL.encode("utf-8"))
    cfg = load_config(p)
    assert cfg.gate.ratio_min == 0.75


def test_malformed_toml_raises_config_error_not_a_traceback(tmp_path):
    """TOML 语法错误必须包成 ConfigError —— main 只接这一种。

    否则用户看到的是 Python traceback 加退出码 1，而不是「配置错误：…」加退出码 2。
    """
    p = _write(tmp_path, MINIMAL + "\nthis is not toml\n")
    with pytest.raises(ConfigError, match="配置文件解析失败"):
        load_config(p)


def test_invalid_utf8_raises_config_error(tmp_path):
    p = tmp_path / "config.toml"
    p.write_bytes(b"[screenpipe]\nbase_url = \"\xff\xfe\x00\"\n")
    with pytest.raises(ConfigError, match="配置文件解析失败"):
        load_config(p)


def test_missing_ratio_min_fails_fast(tmp_path):
    with pytest.raises(ConfigError, match="ratio_min"):
        load_config(_write(tmp_path, MINIMAL.replace("ratio_min = 0.75\n", "")))


def test_ratio_min_out_of_range_fails(tmp_path):
    with pytest.raises(ConfigError, match="ratio_min"):
        load_config(_write(tmp_path, MINIMAL.replace("0.75", "1.5")))


GATE_BLOCK = 'ratio_min = 0.75'


def test_unknown_gate_name_fails_fast(tmp_path):
    """拼错一个闸门名曾经会静默少跑一条闸门，若少的是 state_min 就会打扰不该打扰的人。"""
    body = MINIMAL.replace(GATE_BLOCK, 'enabled = ["state_min", "ratio_mim"]\nratio_min = 0.75')
    with pytest.raises(ConfigError, match="未知闸门名"):
        load_config(_write(tmp_path, body))


def test_missing_state_min_fails_fast(tmp_path):
    body = MINIMAL.replace(GATE_BLOCK, 'enabled = ["ratio_min"]\nratio_min = 0.75')
    with pytest.raises(ConfigError, match="state_min"):
        load_config(_write(tmp_path, body))


def test_empty_gate_list_fails_fast(tmp_path):
    body = MINIMAL.replace(GATE_BLOCK, 'enabled = []\nratio_min = 0.75')
    with pytest.raises(ConfigError, match="不能为空"):
        load_config(_write(tmp_path, body))


def test_known_gate_names_are_accepted(tmp_path):
    body = MINIMAL.replace(GATE_BLOCK, 'enabled = ["state_min"]')
    assert load_config(_write(tmp_path, body)).gate.enabled == ("state_min",)


def test_required_ratio_min_raises_instead_of_falling_back():
    """绝不静默降级 —— 一个悄悄变成 0 的闸门比一条报错危险得多。"""
    from statesense.config import KNOWN_GATES, GateConfig

    assert "ratio_min" in KNOWN_GATES
    with pytest.raises(ConfigError, match="ratio_min"):
        GateConfig().required_ratio_min()
    assert GateConfig(ratio_min=0.75).required_ratio_min() == 0.75


def test_empty_action_pool_fails(tmp_path):
    with pytest.raises(ConfigError, match="actions"):
        load_config(_write(tmp_path, MINIMAL.split("[[actions]]")[0]))


def test_empty_entertainment_list_fails(tmp_path):
    with pytest.raises(ConfigError, match="entertainment"):
        load_config(_write(tmp_path, MINIMAL.replace('["bilibili"]', "[]")))


def test_invalid_regex_fails(tmp_path):
    with pytest.raises(ConfigError, match="正则"):
        load_config(_write(tmp_path, MINIMAL.replace('["bilibili"]', '["bili("]')))


def test_duplicate_action_id_fails(tmp_path):
    body = MINIMAL + """
[[actions]]
id = "walk5"
text = "重复"
applies_to = ["PASSIVE_CONSUMPTION"]
"""
    with pytest.raises(ConfigError, match="重复"):
        load_config(_write(tmp_path, body))


def test_defaults_are_applied(tmp_path):
    cfg = load_config(_write(tmp_path, MINIMAL))
    assert cfg.schedule.tick_seconds == 60
    assert cfg.schedule.evaluate_every_minutes == 5
    assert cfg.thresholds.passive_minutes == 40
    assert cfg.gate.cooldown_minutes == 30
    assert cfg.gate.daily_cap == 8
    assert cfg.outcome.delay_minutes == 10
    assert cfg.notify.channel == "foreground_popup"
    assert cfg.store_path == (tmp_path / "statesense.db").resolve()


def test_base_url_can_be_overridden_by_env(tmp_path, monkeypatch):
    """spec §12/§15.1：上游存在 fallback port，写死 3030 会打到另一个实例。"""
    path = _write(tmp_path, MINIMAL)
    monkeypatch.delenv("SCREENPIPE_LOCAL_API_URL", raising=False)
    assert load_config(path).screenpipe.base_url == "http://localhost:3030"

    monkeypatch.setenv("SCREENPIPE_LOCAL_API_URL", "http://127.0.0.1:3031")
    assert load_config(path).screenpipe.base_url == "http://127.0.0.1:3031"

    monkeypatch.setenv("SCREENPIPE_LOCAL_API_URL", "   ")
    assert load_config(path).screenpipe.base_url == "http://localhost:3030"

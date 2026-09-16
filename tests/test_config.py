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


def test_missing_ratio_min_fails_fast(tmp_path):
    with pytest.raises(ConfigError, match="ratio_min"):
        load_config(_write(tmp_path, MINIMAL.replace("ratio_min = 0.75\n", "")))


def test_ratio_min_out_of_range_fails(tmp_path):
    with pytest.raises(ConfigError, match="ratio_min"):
        load_config(_write(tmp_path, MINIMAL.replace("0.75", "1.5")))


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

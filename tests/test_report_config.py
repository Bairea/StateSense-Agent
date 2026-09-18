from pathlib import Path

import pytest

from statesense.config import ConfigError, load_config

REPO = Path(__file__).resolve().parents[1]


def _write(tmp_path, body: str) -> Path:
    src = (REPO / "config" / "config.example.toml").read_text(encoding="utf-8")
    # 示例文件本身已带 [report] 节，测试要覆盖它就得先摘掉，否则 TOML 报重复表。
    src = src[: src.index("[report]")] + src[src.index("[taxonomy]") :]
    db_path = (tmp_path / "s.db").as_posix()
    src = src.replace('path = "statesense.db"', f'path = "{db_path}"')
    target = tmp_path / "config.toml"
    target.write_text(src + "\n" + body, encoding="utf-8")
    return target


def test_defaults_are_the_documented_ones(tmp_path):
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.report.gap_threshold_minutes == 15
    assert cfg.report.leak_min_active_minutes == 30
    assert cfg.report.leak_min_unclassified_ratio == 0.7
    assert cfg.report.leak_top_n == 10


def test_example_values_match_code_defaults(tmp_path):
    """示例文件里的取值必须与 dataclass 默认值一致 —— 否则本机复制出来的行为
    与不写这一节的行为不同，而两边都「看起来正常」。"""
    src = (REPO / "config" / "config.example.toml").read_text(encoding="utf-8")
    db_path = (tmp_path / "s.db").as_posix()
    target = tmp_path / "config.toml"
    target.write_text(
        src.replace('path = "statesense.db"', f'path = "{db_path}"'), encoding="utf-8"
    )
    cfg = load_config(target)
    assert cfg.report.gap_threshold_minutes == 15
    assert cfg.report.leak_min_active_minutes == 30
    assert cfg.report.leak_min_unclassified_ratio == 0.7
    assert cfg.report.leak_top_n == 10


def test_example_config_carries_the_section(tmp_path):
    """示例文件必须带上这一节 —— 否则本机复制出来就缺配置。"""
    src = (REPO / "config" / "config.example.toml").read_text(encoding="utf-8")
    assert "[report]" in src


def test_explicit_values_are_loaded(tmp_path):
    cfg = load_config(
        _write(
            tmp_path,
            """
[report]
gap_threshold_minutes = 20
leak_min_active_minutes = 45
leak_min_unclassified_ratio = 0.8
leak_top_n = 3
""",
        )
    )
    assert cfg.report.gap_threshold_minutes == 20
    assert cfg.report.leak_min_active_minutes == 45
    assert cfg.report.leak_min_unclassified_ratio == 0.8
    assert cfg.report.leak_top_n == 3


def test_non_positive_gap_threshold_fails_fast(tmp_path):
    with pytest.raises(ConfigError, match="gap_threshold_minutes 必须为正数"):
        load_config(_write(tmp_path, "\n[report]\ngap_threshold_minutes = 0\n"))


def test_negative_leak_min_active_fails_fast(tmp_path):
    with pytest.raises(ConfigError, match="leak_min_active_minutes 不能为负"):
        load_config(_write(tmp_path, "\n[report]\nleak_min_active_minutes = -1\n"))


@pytest.mark.parametrize("ratio", [0, 1.5, -0.2])
def test_out_of_range_leak_ratio_fails_fast(tmp_path, ratio):
    with pytest.raises(ConfigError, match="leak_min_unclassified_ratio 必须在"):
        load_config(
            _write(tmp_path, f"\n[report]\nleak_min_unclassified_ratio = {ratio}\n")
        )


def test_zero_leak_top_n_fails_fast(tmp_path):
    with pytest.raises(ConfigError, match="leak_top_n 必须为正数"):
        load_config(_write(tmp_path, "\n[report]\nleak_top_n = 0\n"))

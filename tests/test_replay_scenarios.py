from pathlib import Path

import pytest

from statesense.config import load_config
from statesense.replay.scenarios import (
    SCENARIOS,
    check_scenario,
    structural_findings,
)

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def config(tmp_path):
    src = (REPO / "config" / "config.example.toml").read_text(encoding="utf-8")
    src = src.replace('path = "statesense.db"', f'path = "{(tmp_path / "s.db").as_posix()}"')
    target = tmp_path / "config.toml"
    target.write_text(src, encoding="utf-8")
    return load_config(target)


def test_all_expected_scenarios_exist():
    assert set(SCENARIOS) == {"ladder", "outcome", "gates", "degraded", "sleep_gap"}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_builtin_scenario_passes(name, config):
    failures = check_scenario(name, config)
    assert failures == [], f"{name}: {failures}"


def test_unknown_scenario_raises(config):
    with pytest.raises(KeyError):
        check_scenario("nope", config)


def test_scenarios_do_not_touch_the_configured_database(config):
    """回放绝不能污染生产库 —— 每个剧本用自己的文件。"""
    check_scenario("ladder", config)
    assert not Path(config.store_path).exists()


def test_check_scenario_is_repeatable(config):
    """跑两次必须一样。

    端到端跑第二遍时发现过这个缺陷：上一轮的库文件还在，行数累加，
    `degraded` 从 6 行变成 12 行。一个不可重复的验证工具会给出假结果。
    """
    first = check_scenario("degraded", config)
    second = check_scenario("degraded", config)
    assert first == []
    assert second == []


def test_repeat_runs_do_not_accumulate_rows(config):
    """sleep_gap 每次只写 1 行评估 + 1 条运行事件；跑两遍后必须还是 1+1。"""
    from statesense.store.db import Store

    check_scenario("sleep_gap", config)
    check_scenario("sleep_gap", config)
    db = Path(config.store_path).parent / "replay" / "sleep_gap" / "replay-sleep-gap.db"
    store = Store(db)
    try:
        assert store._conn.execute("SELECT COUNT(*) AS n FROM evaluations").fetchone()["n"] == 1
        assert store._conn.execute("SELECT COUNT(*) AS n FROM run_events").fetchone()["n"] == 1
    finally:
        store.close()


def test_structural_findings_flag_the_unreachable_high_risk_threshold(config):
    """window=60 而 high_risk=65 时，ent 不可能到 65，该状态是死代码。

    这条不是剧本失败，而是配置本身的结构性问题 —— 显式记录，别让它悄悄留着。
    """
    findings = structural_findings(config)
    assert any("HIGH_RISK_PASSIVE_CONSUMPTION 不可达" in f for f in findings)


def test_structural_findings_are_silent_when_threshold_fits_in_window(tmp_path):
    src = (REPO / "config" / "config.example.toml").read_text(encoding="utf-8")
    src = src.replace('path = "statesense.db"', f'path = "{(tmp_path / "s.db").as_posix()}"')
    src = src.replace("high_risk_minutes = 65", "high_risk_minutes = 55")
    target = tmp_path / "config.toml"
    target.write_text(src, encoding="utf-8")
    assert structural_findings(load_config(target)) == []


def test_replay_flag_is_wired_into_the_parser():
    from statesense.__main__ import build_parser

    args = build_parser().parse_args(["--replay", "all"])
    assert args.replay == "all"

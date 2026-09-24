from pathlib import Path

import pytest

from statesense.replay.scenarios import (
    LADDER,
    SCENARIOS,
    check_scenario,
)


def test_all_expected_scenarios_exist():
    assert set(SCENARIOS) == {
        "ladder",
        "outcome",
        "gates",
        "degraded",
        "sleep_gap",
        "shadow",
    }


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_builtin_scenario_passes(name, config):
    failures = check_scenario(name, config)
    assert failures == [], f"{name}: {failures}"


def test_unknown_scenario_raises(config):
    with pytest.raises(KeyError):
        check_scenario("nope", config)


def test_ladder_covers_every_state_including_high_risk(config):
    """spec §2 判据 1 / §6.4：20 / 40 / high_risk 三档必须真的走完。

    这条断言曾经只写到 PASSIVE_CONSUMPTION —— 因为 high_risk_minutes 配成 65
    而窗口只有 60，那一档永远不可达。让断言迁就代码，等于把死状态合法化：
    配置层现在直接拒绝「阈值高于窗口」的组合，断言也恢复成规格要求的样子。
    """
    assert "HIGH_RISK_PASSIVE_CONSUMPTION" in LADDER
    assert check_scenario("ladder", config) == []


# ── 回放库的落点 ────────────────────────────────────────────

def test_scenarios_do_not_touch_the_configured_database(config):
    """回放绝不能污染生产库 —— 默认连目录都不留。"""
    assert check_scenario("ladder", config) == []
    assert not Path(config.store_path).exists()


def test_default_run_leaves_nothing_behind(config):
    """spec §11：默认写临时库，跑完删除 —— 既不污染生产库，也不留垃圾。"""
    assert check_scenario("ladder", config) == []
    assert not (Path(config.store_path).parent / "replay").exists()


def test_keep_db_writes_into_the_documented_place(config):
    """`--keep-db` 保留到 `<store 所在目录>/replay/<剧本>/`，可用 --report --db 继续读。"""
    assert check_scenario("sleep_gap", config, keep_db=True) == []
    db = Path(config.store_path).parent / "replay" / "sleep_gap" / "replay-sleep-gap.db"
    assert db.is_file()


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

    check_scenario("sleep_gap", config, keep_db=True)
    check_scenario("sleep_gap", config, keep_db=True)
    db = Path(config.store_path).parent / "replay" / "sleep_gap" / "replay-sleep-gap.db"
    store = Store(db)
    try:
        assert store._conn.execute("SELECT COUNT(*) AS n FROM evaluations").fetchone()["n"] == 1
        assert store._conn.execute("SELECT COUNT(*) AS n FROM run_events").fetchone()["n"] == 1
    finally:
        store.close()


# ── CLI 接线 ────────────────────────────────────────────────

def test_replay_flag_is_wired_into_the_parser():
    from statesense.__main__ import build_parser

    args = build_parser().parse_args(["--replay", "all"])
    assert args.replay == "all"
    assert args.keep_db is False


def test_keep_db_flag_is_wired_into_the_parser():
    """spec §11 把它写进了 CLI，那就必须真的存在 —— 曾经完全没有这个参数，
    于是规格里那行命令直接以退出码 2 结束。"""
    from statesense.__main__ import build_parser

    assert build_parser().parse_args(["--replay", "ladder", "--keep-db"]).keep_db is True


def test_unknown_scenario_exits_2_without_running_anything(config, capsys):
    from statesense.__main__ import run_replay

    assert run_replay("nope", config, keep_db=False) == 2
    assert "未知剧本" in capsys.readouterr().err


def test_replay_all_returns_0_when_every_scenario_passes(config, capsys):
    from statesense.__main__ import run_replay

    assert run_replay("all", config, keep_db=False) == 0
    out = capsys.readouterr().out
    for name in SCENARIOS:
        assert f"PASS  {name}" in out

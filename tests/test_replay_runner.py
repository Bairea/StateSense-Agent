from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from statesense.activity.base import ActivitySource
from statesense.replay.runner import (
    ScriptedFullscreenProbe,
    ScriptedReader,
    run_scenario,
)
from statesense.replay.scenario import Scenario, Segment
from statesense.replay.synthesize import snapshot_at

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)

SCENARIO = Scenario(
    name="mini",
    minutes=30,
    segments=(
        Segment(0, 30, "chrome.exe", "哔哩哔哩", "https://www.bilibili.com/video/BV1"),
    ),
)

#: 一条**任何规则都不命中**的条目（进程名与标题都不含娱乐/工作/灰色关键词）。
#: 全屏提权这类「只动未命中条目」的规则，只有用它才测得出来 —— 拿 bilibili
#: 当素材的话它本来就命中娱乐规则，提权前后一模一样，测试会永远通过。
UNMATCHED = Scenario(
    name="unmatched",
    minutes=60,
    segments=(Segment(0, 60, "SomeGame.exe", "Some Game"),),
)


def _drive(scenario: Scenario, config, ticks: int = 13):
    """跑 `ticks` 轮（每轮 5 分钟），返回 (状态集合, 最大 ent)。"""
    run = run_scenario(scenario, config, start=T0)
    try:
        for index in range(ticks):
            run.tick(0.0 if index == 0 else 5.0)
        rows = run.evaluations()
        return {r["state"] for r in rows}, max(r["ent_minutes"] for r in rows)
    finally:
        run.close()


def test_scripted_reader_satisfies_activity_source():
    """回放器能注入的前提是这道缝隙真的存在。"""
    assert isinstance(ScriptedReader(SCENARIO, T0), ActivitySource)


def test_synthesize_clips_segments_to_window():
    snap = snapshot_at(
        SCENARIO, T0, T0 + timedelta(minutes=10), 10, T0 + timedelta(minutes=10), T0
    )
    assert snap.data_status == "ok"
    assert snap.is_trustworthy is True
    assert len(snap.entries) == 1
    assert snap.entries[0].minutes == pytest.approx(10.0)
    assert snap.total_active_minutes == pytest.approx(10.0)
    assert snap.window_start == T0
    assert snap.window_end == T0 + timedelta(minutes=10)


def test_synthesize_omits_segments_outside_window():
    snap = snapshot_at(
        SCENARIO,
        T0 + timedelta(minutes=100),
        T0 + timedelta(minutes=110),
        10,
        T0 + timedelta(minutes=110),
        T0,
    )
    assert snap.entries == ()
    assert snap.total_active_minutes == 0.0


def test_synthesize_uses_start_and_end_not_captured_at():
    """回执的 before/after 两次回查共用同一个 captured_at，只有 start/end 不同。

    若用 captured_at 当窗口末端，两次回查会算成同一段 —— 回执就永远失真。
    """
    before = snapshot_at(
        SCENARIO, T0 - timedelta(minutes=10), T0, 10, T0 + timedelta(minutes=10), T0
    )
    after = snapshot_at(
        SCENARIO, T0, T0 + timedelta(minutes=10), 10, T0 + timedelta(minutes=10), T0
    )
    assert before.total_active_minutes == pytest.approx(0.0)
    assert after.total_active_minutes == pytest.approx(10.0)
    assert before.window_start == T0 - timedelta(minutes=10)
    assert after.window_start == T0


def test_synthesize_merges_overlapping_segments():
    scenario = Scenario(
        name="overlap",
        minutes=60,
        segments=(
            Segment(0, 30, "chrome.exe", "哔哩哔哩"),
            Segment(0, 30, "Code.exe", "GitHub"),
        ),
    )
    snap = snapshot_at(
        scenario, T0, T0 + timedelta(minutes=30), 60, T0 + timedelta(minutes=30), T0
    )
    assert len(snap.entries) == 2
    assert snap.total_active_minutes == pytest.approx(60.0)


def test_degraded_scenario_produces_untrustworthy_snapshot():
    scenario = Scenario(name="deg", minutes=10, data_status="unreachable")
    snap = snapshot_at(
        scenario, T0, T0 + timedelta(minutes=10), 10, T0 + timedelta(minutes=10), T0
    )
    assert snap.data_status == "unreachable"
    assert snap.is_trustworthy is False
    assert snap.entries == ()
    assert snap.total_active_minutes == 0.0


def test_scripted_reader_records_its_calls():
    reader = ScriptedReader(SCENARIO, T0)
    reader.read(T0, T0 + timedelta(minutes=10), 10, T0 + timedelta(minutes=10))
    assert reader.calls == [(T0, T0 + timedelta(minutes=10))]


def test_run_scenario_drives_the_real_scheduler(config):
    run = run_scenario(SCENARIO, config, start=T0)
    try:
        run.tick()
        rows = run.evaluations()
        assert len(rows) == 1
        assert rows[0]["total_active_minutes"] == pytest.approx(0.0)  # 首轮窗口起点即原点
        run.tick(10)
        rows = run.evaluations()
        assert len(rows) == 2
        assert rows[1]["ent_minutes"] == pytest.approx(10.0)
    finally:
        run.close()


def test_replay_tick_advances_virtual_time(config):
    run = run_scenario(SCENARIO, config, start=T0)
    try:
        run.tick()
        run.tick(5)
        assert run.clock.now() == T0 + timedelta(minutes=5)
    finally:
        run.close()


def test_replay_uses_an_isolated_database(config, tmp_path):
    """回放绝不污染生产库。"""
    other = tmp_path / "replay.db"
    run = run_scenario(SCENARIO, config, start=T0, db_path=other)
    try:
        run.tick()
        assert other.is_file()
        assert not Path(config.store_path).exists()
    finally:
        run.close()


# ── 回放的密闭性：绝不读真实环境 ─────────────────────────────

def test_scripted_probe_reports_the_scenario_value():
    assert ScriptedFullscreenProbe(2).state() == 2
    # 剧本没有声明时是 None = 无法判定，不是「不是全屏」。
    assert ScriptedFullscreenProbe(None).state() is None


def test_replay_ignores_the_machines_real_fullscreen_state(config, monkeypatch):
    """回放结果不能取决于「跑回放的这一刻我是不是正开着游戏」。

    这曾经是真的会漂：`run_scenario` 没注入探针，`Scheduler` 就退到
    `default_probe()` —— 在 Windows 上那是一次真实的系统调用。这里把那个
    兜底换成「永远说正在全屏游戏」，剧本的结论必须一字不变。
    """

    class AlwaysGaming:
        def state(self):
            return 2

    monkeypatch.setattr("statesense.scheduler.default_probe", lambda: AlwaysGaming())
    states, worst = _drive(UNMATCHED, config)
    assert states == {"NORMAL"}
    assert worst == 0.0


def test_replay_carries_the_scripted_fullscreen_signal(config):
    """对照组另一半：剧本声明「正在全屏游戏」时，未命中规则的条目被提权成娱乐。

    §13 要求全屏信号有一条**带对照组的端到端**测试。单元测试只证明
    `classify` 会提权；这一条证明它穿过 Scheduler → store → 落行整条链路
    仍然成立，并与上一条构成对照。
    """
    states, worst = _drive(replace(UNMATCHED, fullscreen_state=2), config)
    assert "HIGH_RISK_PASSIVE_CONSUMPTION" in states
    assert worst > 0.0

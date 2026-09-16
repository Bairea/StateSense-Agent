from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from statesense.activity.base import ActivitySource
from statesense.config import load_config
from statesense.replay.runner import ScriptedReader, run_scenario
from statesense.replay.scenario import Scenario, Segment
from statesense.replay.synthesize import snapshot_at

REPO = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)

SCENARIO = Scenario(
    name="mini",
    minutes=30,
    segments=(
        Segment(0, 30, "chrome.exe", "哔哩哔哩", "https://www.bilibili.com/video/BV1"),
    ),
)


@pytest.fixture()
def config(tmp_path):
    src = (REPO / "config" / "config.example.toml").read_text(encoding="utf-8")
    src = src.replace('path = "statesense.db"', f'path = "{(tmp_path / "s.db").as_posix()}"')
    target = tmp_path / "config.toml"
    target.write_text(src, encoding="utf-8")
    return load_config(target)


def test_scripted_reader_satisfies_activity_source():
    """回放器能注入的前提是这道缝隙真的存在。"""
    assert isinstance(ScriptedReader(SCENARIO, 60, T0), ActivitySource)


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
    reader = ScriptedReader(SCENARIO, 10, T0)
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

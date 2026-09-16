import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from statesense.activity.reader import ActivityReader
from statesense.clock import FrozenClock
from statesense.config import load_config
from statesense.notify.base import RESPONSE_ACCEPTED, RecordingNotifier
from statesense.scheduler import Scheduler
from statesense.store.db import Store

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[1]


def _body(ent_minutes: float, total: float = 60.0, status: str = "ok") -> bytes:
    payload = {
        "total_active_minutes": total,
        "data_status": status,
        "windows": [
            {
                "app_name": "chrome.exe",
                "window_name": "【某视频】_哔哩哔哩_bilibili",
                "browser_url": "https://www.bilibili.com/video/BV1",
                "minutes": ent_minutes,
            },
            {
                "app_name": "Code.exe",
                "window_name": "engine.py - Visual Studio Code",
                "browser_url": "",
                "minutes": max(total - ent_minutes, 0.0),
            },
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


@pytest.fixture()
def config(tmp_path):
    src = (REPO / "config" / "config.example.toml").read_text(encoding="utf-8")
    # TOML 基本字符串里反斜杠是转义符，Windows 路径必须用正斜杠。
    db_path = (tmp_path / "s.db").as_posix()
    src = src.replace('path = "statesense.db"', f'path = "{db_path}"')
    target = tmp_path / "config.toml"
    target.write_text(src, encoding="utf-8")
    return load_config(target)


@pytest.fixture()
def store(config):
    s = Store(config.store_path)
    s.migrate()
    yield s
    s.close()


def _scheduler(config, store, bodies: list[bytes], clock, notifier=None) -> Scheduler:
    queue = list(bodies)

    def fake_get(url, headers, timeout):
        body = queue.pop(0) if queue else _body(0.0, 0.0)
        return 200, body

    return Scheduler(
        config=config,
        clock=clock,
        reader=ActivityReader("http://localhost:3030", "k", 10.0, fake_get),
        store=store,
        notifier=notifier or RecordingNotifier(),
    )


# ── 基本评估 ────────────────────────────────────────────────

def test_run_once_records_normal_evaluation_without_intervening(config, store):
    report = _scheduler(config, store, [_body(5.0)], FrozenClock(T0)).run_once()
    assert report.state == "NORMAL"
    assert report.intervened is False
    assert store.fetch_evaluation(report.evaluation_id)["decision"] == "skip"


def test_run_once_intervenes_at_threshold_and_records_delivery(config, store):
    notifier = RecordingNotifier()
    report = _scheduler(config, store, [_body(45.0)], FrozenClock(T0), notifier).run_once()
    assert report.state == "PASSIVE_CONSUMPTION"
    assert report.intervened is True
    assert len(notifier.sent) == 1
    assert "45" in notifier.sent[0][1]


def test_ratio_gate_blocks_mixed_activity(config, store):
    """42 / 60 = 0.70 < 0.75，状态成立但闸门挡下。"""
    report = _scheduler(config, store, [_body(42.0)], FrozenClock(T0)).run_once()
    assert report.state == "PASSIVE_CONSUMPTION"
    assert report.intervened is False
    assert "ratio_min" in report.note


def test_data_status_not_ok_skips_without_concluding(config, store):
    report = _scheduler(
        config, store, [_body(0.0, 0.0, status="no_capture_in_range")], FrozenClock(T0)
    ).run_once()
    assert report.intervened is False
    row = store.fetch_evaluation(report.evaluation_id)
    assert row["data_status"] == "no_capture_in_range"
    assert row["skipped"] == 1, "必须能与「真的 NORMAL」区分开"
    assert row["decision"] == "skip"


# ── 闸门 ────────────────────────────────────────────────────

def test_cooldown_blocks_second_intervention(config, store):
    clock = FrozenClock(T0)
    sch = _scheduler(config, store, [_body(45.0), _body(45.0)], clock)
    assert sch.run_once().intervened is True
    clock.advance(minutes=5)
    assert sch.run_once().intervened is False


def test_cooldown_releases_after_window(config, store):
    clock = FrozenClock(T0)
    # 第二轮 tick 会先为到期的干预查回执（消耗 2 条），所以要多备 2 条给评估用。
    sch = _scheduler(config, store, [_body(45.0)] * 4, clock)
    assert sch.run_once().intervened is True
    clock.advance(minutes=31)
    assert sch.run_once().intervened is True


def test_failed_delivery_does_not_start_cooldown(config, store):
    """投递失败不该消耗冷却 —— 用户根本没被打扰到。"""

    class Failing:
        channel = "failing"

        def notify(self, title, body):
            from statesense.notify.base import DeliveryResult

            return DeliveryResult.failed(self.channel, "boom")

    clock = FrozenClock(T0)
    sch = _scheduler(config, store, [_body(45.0), _body(45.0)], clock, Failing())
    assert sch.run_once().intervened is True
    clock.advance(minutes=1)
    assert sch.run_once().intervened is True


# ── 回执 ────────────────────────────────────────────────────

def test_outcome_is_closed_after_delay(config, store):
    clock = FrozenClock(T0)
    # body 顺序：① 首轮评估（45）② 回执的 before 窗口（45）③ 回执的 after 窗口（3）
    sch = _scheduler(config, store, [_body(45.0), _body(45.0), _body(3.0)], clock)
    sch.run_once()
    iid = store._conn.execute("SELECT id FROM interventions").fetchone()["id"]
    clock.advance(minutes=10)
    report = sch.run_once()
    assert report.outcomes_closed == 1
    row = store.fetch_outcome(iid)
    assert row is not None
    assert row["outcome"] == "disengaged"
    assert row["ent_before"] == 45.0
    assert row["ent_after"] == 3.0


def test_outcome_not_closed_before_delay(config, store):
    clock = FrozenClock(T0)
    sch = _scheduler(config, store, [_body(45.0), _body(45.0)], clock)
    sch.run_once()
    clock.advance(minutes=9)
    assert sch.run_once().outcomes_closed == 0


def test_due_outcomes_are_closed_even_when_current_tick_skips(config, store):
    clock = FrozenClock(T0)
    sch = _scheduler(
        config, store, [_body(45.0), _body(0.0, 0.0, "unreachable"), _body(2.0)], clock
    )
    sch.run_once()
    clock.advance(minutes=10)
    report = sch.run_once()
    assert report.outcomes_closed == 1
    assert report.intervened is False


# ── 按钮回执 ────────────────────────────────────────────────

def test_user_response_is_persisted(config, store):
    notifier = RecordingNotifier(response=RESPONSE_ACCEPTED)
    sch = _scheduler(config, store, [_body(45.0)], FrozenClock(T0), notifier)
    sch.run_once()
    row = store._conn.execute("SELECT user_response FROM interventions").fetchone()
    assert row["user_response"] == "accepted"


def test_missing_user_response_is_persisted_as_null(config, store):
    sch = _scheduler(config, store, [_body(45.0)], FrozenClock(T0))
    sch.run_once()
    row = store._conn.execute("SELECT user_response FROM interventions").fetchone()
    assert row["user_response"] is None


# ── 动作轮转 ────────────────────────────────────────────────

def test_action_rotation_advances_between_interventions(config, store):
    clock = FrozenClock(T0)
    sch = _scheduler(config, store, [_body(45.0)] * 4, clock)
    sch.run_once()
    clock.advance(minutes=31)
    sch.run_once()
    rows = store._conn.execute("SELECT action_id FROM interventions ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0]["action_id"] != rows[1]["action_id"]


def test_late_night_wording_is_used_at_night(config, store):
    """凌晨 3 点触发时，文案里应出现「凌晨」，且这是正交标记而非状态。"""
    night = datetime(2026, 9, 16, 3, 0).astimezone()
    notifier = RecordingNotifier()
    sch = _scheduler(config, store, [_body(45.0)], FrozenClock(night), notifier)
    report = sch.run_once()
    assert report.state == "PASSIVE_CONSUMPTION"
    assert "凌晨" in notifier.sent[0][1]
    row = store.fetch_evaluation(report.evaluation_id)
    assert row["late_night"] == 1
    assert row["state"] == "PASSIVE_CONSUMPTION"


# ── 休眠/唤醒 ────────────────────────────────────────────────

def test_long_gap_skips_evaluation_instead_of_concluding_from_stale_window(config, store):
    """醒来后第一轮的窗口横跨没采集的时间，直接用会得出「几乎没活动」的假结论。"""
    clock = FrozenClock(T0)
    sch = _scheduler(config, store, [_body(45.0), _body(45.0)], clock)
    sch.run_once()
    clock.advance(minutes=200)  # > 2 × 60 分钟
    report = sch.run_once()
    assert report.evaluation_id is None
    assert "空档" in report.note
    assert store._conn.execute("SELECT COUNT(*) AS n FROM evaluations").fetchone()["n"] == 1


def test_gap_within_limit_still_evaluates(config, store):
    clock = FrozenClock(T0)
    sch = _scheduler(config, store, [_body(45.0), _body(45.0)], clock)
    sch.run_once()
    clock.advance(minutes=110)  # < 2 × 60 分钟
    assert sch.run_once().evaluation_id is not None


# ── CLI ─────────────────────────────────────────────────────

def test_db_option_is_accepted():
    from statesense.__main__ import build_parser

    args = build_parser().parse_args(["--check", "--db", "custom.db"])
    assert args.db == Path("custom.db")

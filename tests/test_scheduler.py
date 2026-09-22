import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from statesense.activity.reader import ActivityReader
from statesense.clock import FrozenClock
from statesense.notify.base import RESPONSE_ACCEPTED, RecordingNotifier
from statesense.scheduler import Scheduler
from statesense.store.db import Store

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


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


# ── 全屏信号端到端（带对照组）────────────────────────────────

QUNS_RUNNING_D3D_FULL_SCREEN = 3
#: 「正常，无全屏应用」——游戏退出后探针给出的取值。
QUNS_ACCEPTS_NOTIFICATIONS = 5


class _StubProbe:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def state(self):
        self.calls += 1
        return self.value


def _unnamed_game(ent_minutes: float, total: float = 60.0) -> bytes:
    """一个**没进配置清单**的游戏：窗口标题与进程名都是游戏自身。

    实测就是 Brotato 的形状（`Brotato.exe` / `Brotato`）—— 平台名抓不到它。
    """
    return json.dumps(
        {
            "total_active_minutes": total,
            "data_status": "ok",
            "windows": [
                {
                    "app_name": "SomeGame.exe",
                    "window_name": "SomeGame",
                    "browser_url": "",
                    "minutes": ent_minutes,
                }
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _probe_scheduler(config, store, body: bytes, probe):
    return Scheduler(
        config=config,
        clock=FrozenClock(T0),
        reader=ActivityReader("http://localhost:3030", "k", 10.0, lambda *a: (200, body)),
        store=store,
        notifier=RecordingNotifier(),
        fullscreen=probe,
    )


def test_fullscreen_signal_rescues_an_unnamed_game(config, store):
    """没进清单的游戏在旧逻辑下判 NORMAL。全屏信号在跑时它应当被识别并触发。"""
    probe = _StubProbe(QUNS_RUNNING_D3D_FULL_SCREEN)
    sch = _probe_scheduler(config, store, _unnamed_game(45.0), probe)
    report = sch.run_once()

    assert report.state == "PASSIVE_CONSUMPTION", "全屏信号应当让未命名的游戏被识别"
    assert report.intervened is True
    # **恰好一次**，不是「至少一次」：一轮里探两次，状态判定与行为回执就可能
    # 用上两个不同的取值 —— 那样差出来的不是「效果」而是「探测时机」。
    assert probe.calls == 1, "一轮只应探测一次"
    row = store.fetch_evaluation(report.evaluation_id)
    assert row["fullscreen_state"] == QUNS_RUNNING_D3D_FULL_SCREEN
    assert row["ent_minutes"] == 45.0


def test_fullscreen_is_sampled_once_even_when_an_outcome_is_due(config, store):
    """回执到期那一轮，判定与回执后侧必须共用同一个探测结果。"""
    probe = _StubProbe(QUNS_RUNNING_D3D_FULL_SCREEN)
    sch = _probe_scheduler(config, store, _unnamed_game(45.0), probe)
    first = sch.run_once()
    assert first.intervened is True
    probe.calls = 0

    # 推进到回执到期那一轮：这一轮既要评估，又要结算上一轮的回执。
    sch._clock.advance(minutes=15)  # noqa: SLF001 - 测试需要推进冻结时钟
    sch.run_once()

    assert probe.calls == 1, "一轮只应探测一次；回执前侧改用触发那一轮落库的取值"


def _intervention_id(store) -> int:
    return store._conn.execute("SELECT id FROM interventions").fetchone()["id"]  # noqa: SLF001


def test_outcome_before_side_uses_the_fullscreen_state_recorded_at_trigger(config, store):
    """游戏退出后结算回执：前侧仍按触发那一刻的取值算，后侧用当下的取值。

    阶段 2.2 要修的正是这条：打游戏时触发，10 分钟后结算时游戏已经退出。
    旧写法两侧绑同一个取值 → 前侧被按「没在玩游戏」重算 → ent_before 掉到 0
    → 回执落成 no_data；而「退出游戏」看起来就像「干预成功」。
    """
    probe = _StubProbe(QUNS_RUNNING_D3D_FULL_SCREEN)
    sch = _probe_scheduler(config, store, _unnamed_game(45.0), probe)
    assert sch.run_once().intervened is True

    probe.value = QUNS_ACCEPTS_NOTIFICATIONS  # 游戏退出
    sch._clock.advance(minutes=10)  # noqa: SLF001
    assert sch.run_once().outcomes_closed == 1

    row = store.fetch_outcome(_intervention_id(store))
    assert row["ent_before"] == 45.0, "前侧要用触发那一轮记下的全屏取值（游戏在跑）"
    assert row["ent_after"] == 0.0, "后侧用当下的取值（游戏已退出）"
    assert row["outcome"] == "disengaged"


def test_missing_trigger_evaluation_leaves_the_before_side_unknown(config, store, caplog):
    """取不到触发评估行时**保留未知**，绝不拿当下的全屏取值冒充历史。

    未知 = 不提权（保守），于是前侧通常被算成 0 并落成 no_data。那是如实记录，
    不是失败：把前侧「修好」的唯一诚实办法是当时就把那一轮的取值存下来。
    """
    import logging

    probe = _StubProbe(QUNS_RUNNING_D3D_FULL_SCREEN)
    sch = _probe_scheduler(config, store, _unnamed_game(45.0), probe)
    assert sch.run_once().intervened is True

    # 模拟「触发评估行不在了」：老库或行被清理过。外键挡得住正常写入，
    # 所以这里显式关掉它来构造这个状态。
    with store._conn:  # noqa: SLF001
        store._conn.execute("PRAGMA foreign_keys = OFF")  # noqa: SLF001
        store._conn.execute("UPDATE interventions SET evaluation_id = 999999")  # noqa: SLF001
        store._conn.execute("PRAGMA foreign_keys = ON")  # noqa: SLF001

    with caplog.at_level(logging.WARNING, logger="statesense.scheduler"):
        sch._clock.advance(minutes=10)  # noqa: SLF001
        assert sch.run_once().outcomes_closed == 1

    assert "找不到触发评估行" in caplog.text
    row = store.fetch_outcome(_intervention_id(store))
    assert row["ent_before"] == 0.0
    assert row["outcome"] == "no_data"


def test_evaluation_row_carries_the_rule_version(config, store):
    """每行判定都带上「用哪套规则判的」——库里的行本身读不出分类清单与阈值。"""
    from statesense.rulebook import version_of

    sch = _probe_scheduler(config, store, _unnamed_game(0.0), _StubProbe(None))
    report = sch.run_once()
    assert sch.rule_version == version_of(config)
    assert store.fetch_evaluation(report.evaluation_id)["rule_version"] == version_of(config)


def test_run_forever_logs_start_abort_and_end(config, store, monkeypatch, caplog):
    """`run_forever` 是死循环 —— **它返回这件事本身必须留下记录。**

    实测踩到过：任务计划程序报 `LastTaskResult=0`（成功），进程却不见了，日志里
    一句话没有，库里也没有 `run_events`（那两处补丁只覆盖 `run_tick_guarded`
    抓到的异常，覆盖不了进程级退出）。唯一线索是一轮 tick 的间隔只有 2.8 分钟。
    """
    import logging

    sch = _probe_scheduler(config, store, _unnamed_game(45.0), _StubProbe(None))

    def explode(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr("statesense.scheduler.time.sleep", explode)
    with caplog.at_level(logging.INFO), pytest.raises(KeyboardInterrupt):
        sch.run_forever()

    assert "常驻循环开始" in caplog.text
    assert "常驻循环异常中止：KeyboardInterrupt" in caplog.text
    assert "常驻循环结束" in caplog.text


def test_run_forever_logs_its_identity(config, store, monkeypatch, caplog):
    """启动行要能区分两次运行：pid + 间隔 + 窗口 + 库路径。"""
    import logging
    import os

    sch = _probe_scheduler(config, store, _unnamed_game(45.0), _StubProbe(None))

    def explode(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr("statesense.scheduler.time.sleep", explode)
    with caplog.at_level(logging.INFO), pytest.raises(KeyboardInterrupt):
        sch.run_forever()

    assert f"pid={os.getpid()}" in caplog.text
    assert str(config.store_path) in caplog.text


def test_without_fullscreen_signal_the_same_game_is_missed(config, store):
    """对照组：把信号关掉，同一个游戏完全不被识别 —— 这就是要解决的问题。"""
    sch = _probe_scheduler(config, store, _unnamed_game(45.0), _StubProbe(None))
    report = sch.run_once()

    assert report.state == "NORMAL"
    assert report.intervened is False
    assert store.fetch_evaluation(report.evaluation_id)["fullscreen_state"] is None

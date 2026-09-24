"""影子模式接进调度器之后，**规则侧的行为必须一字不变**。

这个文件的每条测试都在守同一条性质：影子可以失败、可以超时、可以写不进库、
可以给出与规则完全相反的候选 —— 而那一轮该不该弹窗、弹什么，只看规则。
一旦这条性质破了，用户会在某个晚上多收到一个谁也不认识的弹窗。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from statesense.activity.models import ActivitySnapshot, Entry
from statesense.clock import FrozenClock
from statesense.notify.base import RecordingNotifier
from statesense.scheduler import Scheduler
from statesense.shadow.collector import ShadowCollector
from statesense.shadow.models import ModelCandidate, RawModelReply
from statesense.shadow.provider import ScriptedShadowProvider
from statesense.store.db import Store

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)

INTERVENE_ENT = 45.0
"""够到 PASSIVE_CONSUMPTION、且 ratio_min 刚好达标的娱乐分钟数（45/60 = 0.75）。"""


class _Reader:
    """固定快照的活动源。这一层要测的是调度器，不是取数。"""

    def __init__(self, snapshot: ActivitySnapshot) -> None:
        self._snapshot = snapshot

    def read(self, start, end, window_minutes, captured_at) -> ActivitySnapshot:
        return self._snapshot


class _BrokenShadowStore:
    """`insert_shadow_signal` 必失败的 Store 替身。

    只覆盖这一个方法，其余全部转发 —— 这样「影子写失败」这条路径是在真实的
    Store 上被触发的，不必再去猜别的接口会怎么表现。
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    def insert_shadow_signal(self, evaluation_id: int, signal) -> None:
        raise sqlite3.OperationalError("disk I/O error")


class _ExplodingProvider:
    """`model_version` 一读就炸的 provider。

    用来验证「影子实现自身的缺陷也不能拖垮 tick」：`collect` 只把调用包在 try 里，
    收尾构造信号时读版本号仍可能抛，而那一步的失败必须由调度器接住。
    """

    @property
    def model_version(self) -> str:
        raise RuntimeError("实现自身的缺陷")

    @property
    def prompt_version(self) -> str:
        return "p1"

    def query(self, payload) -> RawModelReply:
        return RawModelReply(ModelCandidate.NORMAL.value, "看起来没问题")


def _snapshot(ent_minutes: float = INTERVENE_ENT, status: str = "ok") -> ActivitySnapshot:
    total = 60.0
    entries = (
        Entry("chrome.exe", "【某视频】_哔哩哔哩_bilibili", "", ent_minutes),
        Entry("Code.exe", "engine.py - Visual Studio Code", "", max(total - ent_minutes, 0.0)),
    )
    return ActivitySnapshot(
        window_start=T0 - timedelta(minutes=60),
        window_end=T0,
        window_minutes=60,
        total_active_minutes=total,
        entries=entries,
        data_status=status,
        captured_at=T0,
    )


def _scheduler(config, store, *, snapshot=None, provider=None, notifier=None) -> Scheduler:
    shadow = (
        None
        if provider is None
        else ShadowCollector(provider, timeout_seconds=config.shadow.timeout_seconds)
    )
    clock = FrozenClock(T0)
    return Scheduler(
        config=config,
        clock=clock,
        reader=_Reader(snapshot or _snapshot()),
        store=store,
        notifier=notifier or RecordingNotifier(clock=clock),
        shadow=shadow,
    )


@pytest.fixture()
def store(config):
    s = Store(config.store_path)
    s.migrate()
    yield s
    s.close()


# ── 关闭时什么都不多写 ──────────────────────────────────────

def test_shadow_disabled_writes_no_shadow_rows(config, store):
    """默认关闭：既没有调用，也没有影子行。空表就是「没开」这个事实本身。"""
    report = _scheduler(config, store).run_once()

    assert report.intervened is True, "这一幕本该触发干预，否则下面测不到投递"
    assert store.list_shadow_signals() == []


def test_shadow_disabled_still_decides_and_delivers(config, store):
    """反向对照：上面那条「没有影子行」不是因为它什么都没干。"""
    report = _scheduler(config, store).run_once()

    assert report.state == "PASSIVE_CONSUMPTION"
    assert report.intervened is True


# ── 候选绝不改变判定与投递 ──────────────────────────────────

def test_provider_saying_high_risk_does_not_create_a_delivery(config, store):
    """规则判 NORMAL（不打扰），模型却喊「重度被动消费」—— 依然不许弹窗。

    这是本阶段最重要的一条：模型只有在写好的判定规格里才被允许影响投递，
    而那份规格还没有（缺真实样本与人工真值）。
    """
    provider = ScriptedShadowProvider(
        [RawModelReply(ModelCandidate.HIGH_RISK_PASSIVE_CONSUMPTION.value, "我认为很严重")]
    )
    scheduler = _scheduler(config, store, snapshot=_snapshot(ent_minutes=5.0), provider=provider)

    report = scheduler.run_once()

    assert report.state == "NORMAL"
    assert report.intervened is False
    assert store.list_interventions() == []
    # 候选照样被记下来了 —— 否则谁也不知道模型当时喊过什么。
    row = store.list_shadow_signals()[0]
    assert row["candidate"] == "HIGH_RISK_PASSIVE_CONSUMPTION"
    assert row["state"] == "NORMAL"


def test_shadow_on_and_off_deliver_the_same_thing(config, store, tmp_path):
    """同一场景跑两次（关/开影子），判定、闸门留痕与投递必须逐字段相同。"""
    off_report = _scheduler(config, store).run_once()

    other = Store(tmp_path / "with-shadow.db")
    other.migrate()
    try:
        provider = ScriptedShadowProvider([RawModelReply(ModelCandidate.NORMAL.value, "没问题")])
        on_report = _scheduler(config, other, provider=provider).run_once()

        assert on_report.state == off_report.state
        assert on_report.intervened == off_report.intervened
        before = store.list_evaluations()[0]
        after = other.list_evaluations()[0]
        for column in ("state", "decision", "gate_trace", "ent_minutes"):
            assert before[column] == after[column], f"{column} 被影子改动了"
        assert [r["action_id"] for r in store.list_interventions()] == [
            r["action_id"] for r in other.list_interventions()
        ]
    finally:
        other.close()


def test_the_model_never_sees_the_rules_verdict(config, store):
    """输入只在判定**之前**构造，才保证「先看答案再作答」不会发生。

    这里直接检查送出去的载荷：它只能有白名单字段，尤其不能有规则的状态。
    """
    provider = ScriptedShadowProvider([RawModelReply(ModelCandidate.WATCH.value, "r")])
    scheduler = _scheduler(config, store, provider=provider)

    scheduler.run_once()

    assert len(provider.calls) == 1
    payload = provider.calls[0]
    assert payload.ent_minutes == INTERVENE_ENT
    assert not hasattr(payload, "state")
    assert set(payload.payload()) == {
        "ent_minutes",
        "gray_minutes",
        "work_minutes",
        "total_active_minutes",
        "window_minutes",
        "late_night",
    }


def test_one_shadow_row_per_evaluation_pointing_at_it(config, store):
    provider = ScriptedShadowProvider([RawModelReply(ModelCandidate.WATCH.value, "r")])
    scheduler = _scheduler(config, store, provider=provider)

    scheduler.run_once()

    evaluation_id = store.list_evaluations()[0]["id"]
    rows = store.list_shadow_signals()
    assert len(rows) == 1
    assert rows[0]["evaluation_id"] == evaluation_id


# ── 失败路径 ────────────────────────────────────────────────

def test_failed_call_is_recorded_and_does_not_block_delivery(config, store):
    """模型调用失败时，该提醒的仍然提醒 —— 影子的失败不是用户的失败。"""
    provider = ScriptedShadowProvider([TimeoutError("模拟超时")])
    scheduler = _scheduler(config, store, provider=provider)

    report = scheduler.run_once()

    assert report.intervened is True
    assert len(store.list_interventions()) == 1
    assert store.list_shadow_signals()[0]["outcome"] == "timeout"


def test_untrustworthy_input_records_no_data_and_does_not_intervene(config, store):
    provider = ScriptedShadowProvider([])
    scheduler = _scheduler(
        config,
        store,
        snapshot=_snapshot(ent_minutes=50.0, status="unreachable"),
        provider=provider,
    )

    report = scheduler.run_once()

    assert report.intervened is False
    assert store.list_shadow_signals()[0]["outcome"] == "no_data"
    assert provider.calls == [], "输入不可信时不该问模型"


def test_storage_failure_lands_in_run_events_and_still_delivers(config, store):
    """影子落库失败：记一条 `shadow_error`，然后**照常投递**。

    单独归一类而不并进 `tick_error` —— 这一轮的判定与投递其实都成功了，
    记成 tick_error 会让人以为判定出了问题。
    """
    provider = ScriptedShadowProvider([RawModelReply(ModelCandidate.NORMAL.value, "r")])
    scheduler = _scheduler(config, _BrokenShadowStore(store), provider=provider)

    report = scheduler.run_once()

    assert report.intervened is True
    assert len(store.list_interventions()) == 1
    events = store.list_run_events()
    assert [event["kind"] for event in events] == ["shadow_error"]
    assert "OperationalError" in events[0]["detail"]


def test_a_broken_shadow_implementation_never_raises_out_of_the_tick(config, store):
    """影子实现自身的缺陷也必须被那层 try 兜住，而不是让本轮 tick 挂掉。"""
    scheduler = _scheduler(config, store, provider=_ExplodingProvider())

    report = scheduler.run_once()  # 不抛

    assert report.intervened is True
    assert [event["kind"] for event in store.list_run_events()] == ["shadow_error"]


# ── 归类只做一次 ────────────────────────────────────────────

def test_classify_gives_the_same_verdict_with_or_without_given_buckets(config):
    """传进去的归类结果与自己算的那份必须导出同一个判定。

    影子输入需要归类结果，而判定内部也要用 —— 各算一遍不会算出不同答案（纯函数），
    但会把全轮最贵的一步做两遍。这条测试是那次复用改动的守门人：一旦
    `classify` 在给定 buckets 时走上了不同的分支，它会立刻发现。
    """
    from statesense.state.engine import classify
    from statesense.state.taxonomy import bucket_minutes

    snapshot = _snapshot()
    buckets = bucket_minutes(snapshot.entries, config.taxonomy)

    shared = classify(
        snapshot, config.taxonomy, config.thresholds, fullscreen_state=None, buckets=buckets
    )
    recomputed = classify(snapshot, config.taxonomy, config.thresholds, fullscreen_state=None)

    assert shared == recomputed

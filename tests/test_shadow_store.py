"""影子记录的持久化。

最要紧的一条是**「没有影子行」与「模型判为正常」必须分得开**。
两者一旦混同，分歧率的分母就是假的：没问过的轮次会被读成「模型说没问题」，
而那个数字只会让模型看起来更保守。

另一条是外键：影子行不能脱离评估行存在。配错时刻的表现是「分歧率看起来有数」，
比没有数据更难发现 —— 所以这件事交给数据库拒绝，而不是靠调用方自觉。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from statesense.intervention.models import Decision
from statesense.shadow.models import ModelCandidate, ModelSignal, ShadowOutcome
from statesense.state.models import State, StateVerdict
from statesense.store.db import RUN_EVENT_KINDS, SCHEMA_VERSION, Store

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)
RV = "test-rule-v1"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.migrate()
    yield s
    s.close()


def _verdict(state: str = "PASSIVE_CONSUMPTION") -> StateVerdict:
    return StateVerdict(
        state=State(state),
        late_night=False,
        total_active_minutes=60.0,
        ent_minutes=45.0,
        gray_minutes=5.0,
        work_minutes=10.0,
        ent_ratio=0.75,
        entries_minutes=60.0,
        fullscreen_state=None,
        window_minutes=60,
        data_status="ok",
        skipped=False,
        skip_reason=None,
    )


_SKIP = Decision(intervene=False, action_id=None, reason="测试", gate_trace=())


def _evaluation(store, at=T0, state: str = "PASSIVE_CONSUMPTION") -> int:
    return store.insert_evaluation(at, _verdict(state), _SKIP, rule_version=RV)


def _signal(
    *,
    outcome: ShadowOutcome = ShadowOutcome.OK,
    candidate: ModelCandidate | None = ModelCandidate.NORMAL,
    latency_ms: float | None = 12.5,
) -> ModelSignal:
    return ModelSignal(
        outcome=outcome,
        candidate=candidate,
        model_version="m1",
        prompt_version="p1",
        latency_ms=latency_ms,
        reason="理由",
        detail=None,
    )


# ── 往返 ────────────────────────────────────────────────────

def test_shadow_row_roundtrips_with_its_evaluation(store):
    """读回来的一行要同时带着两侧事实：候选，以及同一时刻的规则判定。

    分歧分析靠的就是这个配对 —— 分两次查会在两次查询之间留下不一致的窗口。
    """
    evaluation_id = _evaluation(store, state="WATCH")
    store.insert_shadow_signal(evaluation_id, _signal())

    rows = store.list_shadow_signals()

    assert len(rows) == 1
    row = rows[0]
    assert row["evaluation_id"] == evaluation_id
    assert row["state"] == "WATCH"
    assert row["rule_version"] == RV
    assert row["outcome"] == "ok"
    assert row["candidate"] == "NORMAL"
    assert row["model_version"] == "m1"
    assert row["prompt_version"] == "p1"
    assert row["latency_ms"] == 12.5


def test_failed_call_is_stored_without_a_candidate(store):
    evaluation_id = _evaluation(store)
    store.insert_shadow_signal(
        evaluation_id,
        _signal(outcome=ShadowOutcome.TIMEOUT, candidate=None, latency_ms=3000.0),
    )

    row = store.list_shadow_signals()[0]

    assert row["outcome"] == "timeout"
    assert row["candidate"] is None
    assert row["latency_ms"] == 3000.0


def test_rewriting_the_same_evaluation_replaces_the_row(store):
    """同一轮重写是重试，不是追加。主键就是这件事的保证。"""
    evaluation_id = _evaluation(store)
    store.insert_shadow_signal(evaluation_id, _signal(candidate=ModelCandidate.NORMAL))
    store.insert_shadow_signal(
        evaluation_id, _signal(candidate=ModelCandidate.HIGH_RISK_PASSIVE_CONSUMPTION)
    )

    rows = store.list_shadow_signals()

    assert len(rows) == 1
    assert rows[0]["candidate"] == "HIGH_RISK_PASSIVE_CONSUMPTION"


def test_since_filters_by_evaluation_time_not_by_shadow_write_time(store):
    """时间轴只有一个：影子表里没有自己的 `at`，时刻来自被指向的评估行。"""
    early = _evaluation(store, T0)
    late = _evaluation(store, T0 + timedelta(minutes=30))
    store.insert_shadow_signal(early, _signal())
    store.insert_shadow_signal(late, _signal())

    rows = store.list_shadow_signals(since=T0 + timedelta(minutes=10))

    assert [row["evaluation_id"] for row in rows] == [late]


# ── 外键 ────────────────────────────────────────────────────

def test_shadow_row_cannot_exist_without_its_evaluation(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_shadow_signal(9999, _signal())


def test_failed_foreign_key_insert_leaves_nothing_behind(store):
    """被拒绝的写入不能留下半行 —— 半个影子记录比没有更难解释。"""
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_shadow_signal(9999, _signal())

    assert store.list_shadow_signals() == []


# ── 「没问」与「判为否」是两件事 ─────────────────────────────

def test_no_row_means_not_asked_never_model_said_normal(store):
    """这条是本表存在的全部理由，所以它必须被写成一个测试。

    「模型判为正常」长这样：`outcome='ok'` + `candidate='NORMAL'`。
    「没问过」长这样：**没有行**。两者绝不能互相冒充。
    """
    evaluation_id = _evaluation(store)

    assert store.list_shadow_signals() == [], "没插过就不该有行"

    store.insert_shadow_signal(evaluation_id, _signal(candidate=ModelCandidate.NORMAL))
    row = store.list_shadow_signals()[0]
    assert row["outcome"] == "ok" and row["candidate"] == "NORMAL"


# ── 迁移 ────────────────────────────────────────────────────

def test_migrating_a_pre_shadow_database_adds_an_empty_table(store):
    """老库升级后：表在、旧行没有影子记录、旧行**不被回填**。

    回填一行「模型判为 NORMAL」等于伪造一次根本不存在的调用。
    """
    old_evaluation = _evaluation(store, T0)
    # 模拟 v8 之前的库：表还不存在。
    with store._conn:
        store._conn.execute("DROP TABLE shadow_signals")
        store._conn.execute("PRAGMA user_version = 7")

    store.migrate()

    assert store.user_version() == SCHEMA_VERSION
    assert store.list_evaluations()[0]["id"] == old_evaluation
    assert store.list_shadow_signals() == []


def test_migration_is_idempotent_with_the_shadow_table(store):
    store.migrate()
    store.migrate()
    assert store.user_version() == SCHEMA_VERSION


# ── 运行事件 ────────────────────────────────────────────────

def test_shadow_error_is_a_known_run_event_kind(store):
    """单独一类，不与 tick_error 合并：影子落库失败时判定与投递其实都成功了。"""
    assert "shadow_error" in RUN_EVENT_KINDS
    store.insert_run_event(T0, "shadow_error", "OperationalError: disk I/O error")

    events = store.list_run_events()

    assert events[0]["kind"] == "shadow_error"


def test_unknown_run_event_kind_is_still_rejected(store):
    with pytest.raises(ValueError, match="未知的运行事件类型"):
        store.insert_run_event(T0, "shadow_oops", "x")

from datetime import datetime, timedelta, timezone

import pytest

from statesense.intervention.models import Decision
from statesense.state.models import State, StateVerdict
from statesense.store.db import Store

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    s.migrate()
    yield s
    s.close()


def _verdict(ent: float = 45.0) -> StateVerdict:
    return StateVerdict(
        state=State.PASSIVE_CONSUMPTION,
        late_night=False,
        total_active_minutes=60.0,
        ent_minutes=ent,
        gray_minutes=5.0,
        work_minutes=10.0,
        ent_ratio=ent / 60.0,
        entries_minutes=60.0,
        window_minutes=60,
        data_status="ok",
        skipped=False,
        skip_reason=None,
    )


_SKIP = Decision(intervene=False, action_id=None, reason="测试", gate_trace=())


# ── Store 只读查询 ──────────────────────────────────────────

def test_list_evaluations_is_ordered_and_filters_by_since(store):
    for i in range(3):
        store.insert_evaluation(T0 + timedelta(minutes=10 * i), _verdict(), _SKIP)

    assert len(store.list_evaluations()) == 3
    rows = store.list_evaluations(since=T0 + timedelta(minutes=10))
    assert [r["at"] for r in rows] == [r["at"] for r in store.list_evaluations()][1:]


def test_list_evaluations_is_ordered_by_time_not_insert_order(store):
    """乱序插入也要按时间返回 —— report 的缺口检测依赖时间序。"""
    store.insert_evaluation(T0 + timedelta(minutes=20), _verdict(), _SKIP)
    store.insert_evaluation(T0, _verdict(), _SKIP)
    store.insert_evaluation(T0 + timedelta(minutes=10), _verdict(), _SKIP)
    ats = [r["at"] for r in store.list_evaluations()]
    assert ats == sorted(ats)


def test_list_run_events_returns_rows(store):
    store.insert_run_event(T0, "sleep_gap", "200.0")
    rows = store.list_run_events()
    assert len(rows) == 1
    assert rows[0]["kind"] == "sleep_gap"


def test_list_interventions_and_outcomes_are_empty_on_fresh_db(store):
    assert store.list_interventions() == []
    assert store.list_outcomes() == []


def test_list_methods_work_when_tables_are_empty(store):
    """report 在一张空表上必须能跑完 —— 空不是错误。"""
    assert store.list_evaluations() == []
    assert store.list_evaluations(since=T0) == []
    assert store.list_interventions(since=T0) == []
    assert store.list_outcomes(since=T0) == []
    assert store.list_run_events(since=T0) == []

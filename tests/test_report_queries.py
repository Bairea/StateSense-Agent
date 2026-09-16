import json
from datetime import datetime, timedelta, timezone

import pytest

from statesense.intervention.models import Decision, GateResult
from statesense.outcome.models import OutcomeVerdict
from statesense.report import queries
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


# ── 聚合纯函数 ──────────────────────────────────────────────

def _eval_row(store, at, *, ent=45.0, total=60.0, state="PASSIVE_CONSUMPTION",
              data_status="ok", skipped=0, entries=None, gates=None):
    verdict = StateVerdict(
        state=State(state),
        late_night=False,
        total_active_minutes=total,
        ent_minutes=ent,
        gray_minutes=0.0,
        work_minutes=0.0,
        ent_ratio=(ent / total if total > 0 else 0.0),
        entries_minutes=(total if entries is None else entries),
        window_minutes=60,
        data_status=data_status,
        skipped=bool(skipped),
        skip_reason=None,
    )
    decision = Decision(
        intervene=False,
        action_id=None,
        reason="t",
        gate_trace=tuple(GateResult(n, p, v, th) for n, p, v, th in (gates or [])),
    )
    return store.insert_evaluation(at, verdict, decision)


def test_overview_reports_gap_when_ticks_missing(store):
    _eval_row(store, T0)
    _eval_row(store, T0 + timedelta(minutes=5))
    _eval_row(store, T0 + timedelta(minutes=65))
    ov = queries.build_overview(
        store.list_evaluations(), [], evaluate_every_minutes=5, gap_threshold_minutes=15
    )
    assert ov.evaluations == 3
    assert len(ov.gaps) == 1
    assert ov.gaps[0].minutes == pytest.approx(60.0)


def test_overview_attaches_run_events_to_the_gap(store):
    _eval_row(store, T0)
    _eval_row(store, T0 + timedelta(minutes=200))
    store.insert_run_event(T0 + timedelta(minutes=100), "sleep_gap", "200.0")
    ov = queries.build_overview(
        store.list_evaluations(),
        store.list_run_events(),
        evaluate_every_minutes=5,
        gap_threshold_minutes=15,
    )
    assert len(ov.gaps) == 1
    assert [e.kind for e in ov.gaps[0].events] == ["sleep_gap"]


def test_overview_leaves_gap_without_run_event_empty(store):
    """没有 run_events 的缺口 → 只可能是进程当时不在运行。"""
    _eval_row(store, T0)
    _eval_row(store, T0 + timedelta(minutes=200))
    ov = queries.build_overview(
        store.list_evaluations(), [], evaluate_every_minutes=5, gap_threshold_minutes=15
    )
    assert ov.gaps[0].events == ()


def test_overview_on_empty_database_is_not_an_error(store):
    ov = queries.build_overview(
        [], [], evaluate_every_minutes=5, gap_threshold_minutes=15
    )
    assert ov.evaluations == 0
    assert ov.first_at is None
    assert ov.coverage == 0.0


def test_verdict_breakdown_counts_skipped_separately(store):
    _eval_row(store, T0, ent=0.0, total=0.0, state="NORMAL", skipped=1,
              data_status="unreachable")
    _eval_row(store, T0 + timedelta(minutes=5), ent=5.0, total=60.0, state="NORMAL")
    bd = queries.build_verdict_breakdown(store.list_evaluations())
    assert bd.total == 2
    assert bd.skipped == 1
    assert dict(bd.states)["NORMAL"] == 2
    assert dict(bd.data_statuses)["unreachable"] == 1


def test_gate_breakdown_names_the_blocking_gate(store):
    _eval_row(store, T0, gates=[("state_min", True, 1.0, 1.0), ("ratio_min", False, 0.70, 0.75)])
    gb = queries.build_gate_breakdown(store.list_evaluations())
    assert dict(gb.blocked_by)["ratio_min"] == 1
    assert len(gb.state_min_passed_then_blocked) == 1
    assert gb.state_min_passed_then_blocked[0].value == pytest.approx(0.70)
    assert gb.state_min_passed_then_blocked[0].threshold == pytest.approx(0.75)


def test_gate_breakdown_ignores_rounds_blocked_by_state_min(store):
    """state_min 就挡下的轮次不算「该提醒但没提醒」。"""
    _eval_row(store, T0, gates=[("state_min", False, 0.0, 1.0)])
    gb = queries.build_gate_breakdown(store.list_evaluations())
    assert gb.state_min_passed_then_blocked == ()
    assert dict(gb.blocked_by)["state_min"] == 1


def test_gate_breakdown_survives_corrupt_trace(store):
    """一行闸门数据坏掉，不该让整份报告失效 —— 坏行跳过，其余照常统计。"""
    _eval_row(store, T0, gates=[("ratio_min", False, 0.7, 0.75)])
    _eval_row(store, T0 + timedelta(minutes=5), gates=[("ratio_min", False, 0.6, 0.75)])
    first_at = store.list_evaluations()[0]["at"]
    with store._conn:
        store._conn.execute(
            "UPDATE evaluations SET gate_trace = 'not json' WHERE at = ?", (first_at,)
        )

    gb = queries.build_gate_breakdown(store.list_evaluations())
    assert dict(gb.blocked_by)["ratio_min"] == 1
    assert len(gb.state_min_passed_then_blocked) == 1


def test_ratio_histogram_counts_only_rows_with_activity(store):
    _eval_row(store, T0, ent=45.0, total=60.0)
    _eval_row(store, T0 + timedelta(minutes=5), ent=0.0, total=0.0, state="NORMAL")
    gb = queries.build_gate_breakdown(store.list_evaluations())
    assert sum(n for _, n in gb.ratio_histogram) == 1


def test_leak_anchor_fires_on_unclassified_activity(store):
    """Brotato 场景：活跃 59.9 分钟、娱乐/灰/工作全 0。"""
    _eval_row(store, T0, ent=0.0, total=59.9, state="NORMAL", entries=59.9)
    anchors = queries.find_leak_anchors(
        store.list_evaluations(), min_active_minutes=30, min_unclassified_ratio=0.7
    )
    assert len(anchors) == 1
    assert anchors[0].unclassified_minutes == pytest.approx(59.9)
    assert anchors[0].missing_detail_minutes == pytest.approx(0.0)


def test_leak_anchor_does_not_fire_when_detail_is_missing(store):
    """明细缺失（entries_minutes 远小于 total）不算漏判 —— 那是数据没取到。"""
    _eval_row(store, T0, ent=0.0, total=59.9, state="NORMAL", entries=0.0)
    anchors = queries.find_leak_anchors(
        store.list_evaluations(), min_active_minutes=30, min_unclassified_ratio=0.7
    )
    assert anchors == ()


def test_leak_anchor_does_not_fire_below_ratio(store):
    _eval_row(store, T0, ent=50.0, total=60.0, state="PASSIVE_CONSUMPTION", entries=57.0)
    anchors = queries.find_leak_anchors(
        store.list_evaluations(), min_active_minutes=30, min_unclassified_ratio=0.7
    )
    assert anchors == ()


def test_leak_anchor_does_not_fire_below_min_active(store):
    _eval_row(store, T0, ent=0.0, total=10.0, state="NORMAL", entries=10.0)
    anchors = queries.find_leak_anchors(
        store.list_evaluations(), min_active_minutes=30, min_unclassified_ratio=0.7
    )
    assert anchors == ()


def test_leak_anchor_reports_missing_detail_separately(store):
    """锚点同时给出「明细缺失」这一项，让人能自己判断是漏判还是没取到数。"""
    _eval_row(store, T0, ent=5.0, total=60.0, state="NORMAL", entries=45.0)
    anchors = queries.find_leak_anchors(
        store.list_evaluations(), min_active_minutes=30, min_unclassified_ratio=0.7
    )
    # unclassified = 45 - 5 = 40，占 60 的 0.667 < 0.7 —— 不触发
    assert anchors == ()
    anchors = queries.find_leak_anchors(
        store.list_evaluations(), min_active_minutes=30, min_unclassified_ratio=0.6
    )
    assert anchors[0].unclassified_minutes == pytest.approx(40.0)
    assert anchors[0].missing_detail_minutes == pytest.approx(15.0)


def _insert_intervention(store, evaluation_id, *, at, action_id, response, order=0):
    return store.insert_intervention(
        evaluation_id=evaluation_id,
        at=at,
        state="PASSIVE_CONSUMPTION",
        late_night=False,
        action_id=action_id,
        action_text="t",
        delivery_status="delivered",
        outcome_due_at=at + timedelta(minutes=10),
        user_response=response,
    )


def test_outcome_breakdown_stratifies_by_user_response(store):
    eid = _eval_row(store, T0)
    iid_a = _insert_intervention(
        store, eid, at=T0, action_id="walk5", response="accepted"
    )
    iid_b = _insert_intervention(
        store, eid, at=T0 + timedelta(minutes=1), action_id="stretch", response=None
    )
    store.insert_outcome(iid_a, T0 + timedelta(minutes=10),
                         OutcomeVerdict("disengaged", 45.0, 3.0, 10.0))
    store.insert_outcome(iid_b, T0 + timedelta(minutes=11),
                         OutcomeVerdict("continued", 45.0, 50.0, 10.0))

    ob = queries.build_outcome_breakdown(store.list_outcomes(), store.list_interventions())
    by_resp = dict(ob.by_response)
    assert dict(by_resp["accepted"])["disengaged"] == 1
    assert dict(by_resp["none"])["continued"] == 1
    assert ob.ent_before_mean == pytest.approx(45.0)
    assert ob.ent_after_mean == pytest.approx(26.5)


def test_outcome_breakdown_excludes_no_data_from_means(store):
    eid = _eval_row(store, T0)
    good = _insert_intervention(store, eid, at=T0, action_id="walk5", response=None)
    bad = _insert_intervention(
        store, eid, at=T0 + timedelta(minutes=1), action_id="stretch", response=None
    )
    store.insert_outcome(good, T0 + timedelta(minutes=10),
                         OutcomeVerdict("disengaged", 40.0, 10.0, 10.0))
    store.insert_outcome(bad, T0 + timedelta(minutes=11),
                         OutcomeVerdict("no_data", 0.0, 0.0, 0.0))

    ob = queries.build_outcome_breakdown(store.list_outcomes(), store.list_interventions())
    assert ob.no_data == 1
    assert ob.ent_before_mean == pytest.approx(40.0)
    assert ob.ent_after_mean == pytest.approx(10.0)


def test_intervention_breakdown_counts_cooldown_and_cap_blocks(store):
    _eval_row(store, T0, gates=[("state_min", True, 1.0, 1.0), ("cooldown", False, 5.0, 30.0)])
    _eval_row(store, T0 + timedelta(minutes=5),
              gates=[("state_min", True, 1.0, 1.0), ("daily_cap", False, 8.0, 8.0)])
    ib = queries.build_intervention_breakdown(
        store.list_evaluations(), store.list_interventions()
    )
    assert ib.cooldown_blocks == 1
    assert ib.daily_cap_blocks == 1


def test_build_trace_ends_at_the_row_not_after_the_moment(store):
    for i in range(10):
        _eval_row(store, T0 + timedelta(minutes=5 * i),
                  gates=[("state_min", True, 1.0, 1.0)])
    trace = queries.build_trace(
        store.list_evaluations(), T0 + timedelta(minutes=25), window_ticks=4
    )
    assert len(trace) == 4
    assert trace[-1].at == T0 + timedelta(minutes=25)
    assert trace[0].at == T0 + timedelta(minutes=10)


def test_build_trace_clamps_at_the_start_of_history(store):
    for i in range(3):
        _eval_row(store, T0 + timedelta(minutes=5 * i))
    trace = queries.build_trace(
        store.list_evaluations(), T0 + timedelta(minutes=10), window_ticks=12
    )
    assert len(trace) == 3


def test_build_trace_carries_gate_details(store):
    _eval_row(store, T0, gates=[("ratio_min", False, 0.70, 0.75)])
    trace = queries.build_trace(store.list_evaluations(), T0, window_ticks=1)
    assert trace[0].gates == (("ratio_min", False, 0.70, 0.75),)

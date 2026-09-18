import json
from datetime import datetime, timedelta, timezone

import pytest

from statesense.intervention.models import Decision, GateResult, parse_gate_trace
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
        fullscreen_state=None,
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
              data_status="ok", skipped=0, entries=None, gates=None,
              fullscreen_state=None):
    verdict = StateVerdict(
        state=State(state),
        late_night=False,
        total_active_minutes=total,
        ent_minutes=ent,
        gray_minutes=0.0,
        work_minutes=0.0,
        ent_ratio=(ent / total if total > 0 else 0.0),
        entries_minutes=(total if entries is None else entries),
        fullscreen_state=fullscreen_state,
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


def test_overview_coverage_never_exceeds_one(store):
    """满跑时覆盖率必须是 100%，不是 116%。

    规格 §5.2 写的是 `轮数 × every / 区间分钟数`，它在正常满跑时会算出
    span=30、every=5、轮数=7 → 1.167。覆盖率超过 100% 只会让人怀疑这个数本身，
    所以这里改用「按节奏本该有多少轮」当分母。偏差已记入验证日志。
    """
    for i in range(7):
        _eval_row(store, T0 + timedelta(minutes=5 * i))
    ov = queries.build_overview(
        store.list_evaluations(), [], evaluate_every_minutes=5, gap_threshold_minutes=15
    )
    assert ov.expected_evaluations == 7
    assert ov.coverage == pytest.approx(1.0)


# ── 此刻是否还在跑（spec §7.4）────────────────────────────────

def _liveness(store, now, *, threshold=15.0, run_events=()):
    return queries.build_liveness(
        store.list_evaluations(), run_events, now, gap_threshold_minutes=threshold
    )


def test_liveness_is_offline_when_the_last_evaluation_is_too_old(store):
    """同一个阈值要同时回答「过去断过没有」与「现在断了吗」。

    只实现前一半（缺口列表）时，打开报告的人看不到最关键的那句结论。
    """
    _eval_row(store, T0)
    lv = _liveness(store, T0 + timedelta(minutes=60))
    assert lv.offline is True
    assert lv.silent_minutes == pytest.approx(60.0)
    assert lv.threshold_minutes == 15.0
    assert lv.events == ()


def test_liveness_is_online_within_the_threshold(store):
    _eval_row(store, T0)
    lv = _liveness(store, T0 + timedelta(minutes=5))
    assert lv.offline is False
    assert lv.silent_minutes == pytest.approx(5.0)


def test_liveness_attaches_run_events_after_the_last_evaluation(store):
    """「有意跳过/出错」与「进程死了」必须在结尾处也能分开。"""
    _eval_row(store, T0)
    store.insert_run_event(T0 + timedelta(minutes=30), "tick_error", "RuntimeError: boom")
    lv = _liveness(store, T0 + timedelta(minutes=60), run_events=store.list_run_events())
    assert lv.offline is True
    assert [e.kind for e in lv.events] == ["tick_error"]


def test_liveness_ignores_future_timestamps(store):
    """时钟回拨或库里出现未来行时不能判成掉线 —— 「未来有数据」不是「进程死了」。"""
    _eval_row(store, T0 + timedelta(minutes=30))
    assert _liveness(store, T0).offline is False


def test_liveness_on_empty_range_says_nothing_is_known(store):
    lv = _liveness(store, T0)
    assert lv.last_at is None
    assert lv.silent_minutes is None
    assert lv.offline is False


def test_verdict_breakdown_counts_skipped_separately(store):
    _eval_row(store, T0, ent=0.0, total=0.0, state="NORMAL", skipped=1,
              data_status="unreachable")
    _eval_row(store, T0 + timedelta(minutes=5), ent=5.0, total=60.0, state="NORMAL")
    bd = queries.build_verdict_breakdown(store.list_evaluations())
    assert bd.total == 2
    assert bd.skipped == 1
    assert dict(bd.states)["NORMAL"] == 2
    assert dict(bd.data_statuses)["unreachable"] == 1


def test_verdict_breakdown_counts_rows_without_entry_detail(store):
    """报活跃却没有条目明细的轮次必须被数出来。

    漏判视图对它们不成立（两个差额都退化），静默略过就会被读成「没有漏判」。
    迁移前写入的行（`entries_minutes` 取 DEFAULT 0）也落在这一档，且在数据上
    无法与「Screenpipe 真的没返回明细」区分 —— 所以只能一起计数、一起说明。
    """
    _eval_row(store, T0, ent=0.0, total=59.9, state="NORMAL", entries=0.0)
    _eval_row(store, T0 + timedelta(minutes=5), ent=45.0, total=60.0, entries=60.0)
    # 没有活跃的轮次不算 —— 那是「真的没在电脑前」，不是明细缺失。
    _eval_row(store, T0 + timedelta(minutes=10), ent=0.0, total=0.0, state="NORMAL",
              entries=0.0)

    bd = queries.build_verdict_breakdown(store.list_evaluations())
    assert bd.entries_unknown == 1


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
    """一行闸门数据坏掉，不该让整份报告失效 —— 坏行跳过，其余照常统计。

    但它必须被**数出来**：坏行同时会从 blocked_by 里消失，不说出口就会被
    读成「闸门没挡过」。这正是本版本要消灭的那类静默二义。
    """
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
    assert gb.corrupt_rows == 1
    # spec §5.2：损坏行从视图 2 的**全部**统计中剔除 —— 直方图也不例外，
    # 否则渲染层的「已剔除」说明就成了谎话。
    assert sum(dict(gb.ratio_histogram).values()) == 1


def test_parse_gate_trace_skips_element_missing_threshold_instead_of_crashing():
    """缺 threshold 的元素按畸形处理：跳过本条，绝不抛 KeyError。

    §12 的底线是「不中断整份报告」—— 一条都读不出时返回 None 计入损坏，
    混合时保留可读的那条。
    """
    only_broken = json.dumps([{"name": "ratio_min", "passed": False, "value": 0.7}])
    assert parse_gate_trace(only_broken) is None

    mixed = json.dumps(
        [
            {"name": "ratio_min", "passed": False, "value": 0.7},
            {"name": "cooldown", "passed": True, "value": 30.0, "threshold": 60.0},
        ]
    )
    parsed = parse_gate_trace(mixed)
    assert parsed is not None
    assert [g.name for g in parsed] == ["cooldown"]


def test_gate_breakdown_survives_trace_element_missing_threshold(store):
    """端到端：库里躺着缺 threshold 键的留痕，报告要数出损坏并跑完全程。"""
    _eval_row(store, T0, gates=[("state_min", True, 2.0, 1.0)])
    at = store.list_evaluations()[0]["at"]
    with store._conn:
        store._conn.execute(
            "UPDATE evaluations SET gate_trace = ? WHERE at = ?",
            (json.dumps([{"name": "ratio_min", "passed": False, "value": 0.7}]), at),
        )
    gb = queries.build_gate_breakdown(store.list_evaluations())
    assert gb.corrupt_rows == 1


# ── 漏判二级视图聚合（原在入口层，零测试） ──────────────────

def _anchor(at):
    from statesense.report.models import LeakAnchor

    return LeakAnchor(
        at=at, total_active_minutes=60.0, ent_minutes=0.0, gray_minutes=0.0,
        work_minutes=0.0, unclassified_minutes=60.0, unclassified_ratio=1.0,
        missing_detail_minutes=0.0,
    )


def _snap(end, *, status="ok", entries=()):
    from statesense.activity.models import ActivitySnapshot

    return ActivitySnapshot(
        window_start=end - timedelta(minutes=60), window_end=end,
        window_minutes=60, total_active_minutes=60.0,
        entries=entries, data_status=status, captured_at=end,
    )


def _entry(app, minutes, title=""):
    from statesense.activity.models import Entry

    return Entry(app=app, title=title or app, url="", minutes=minutes)


def test_aggregate_leak_details_keeps_only_other_and_sorts_desc(config):
    from statesense.report.models import LeakDetailStatus

    details = queries.aggregate_leak_details(
        [_anchor(T0)],
        [_snap(T0, entries=(_entry("cursor", 40.0), _entry("神秘软件", 5.0),
                            _entry("bilibili", 15.0)))],
        taxonomy=config.taxonomy, top_n=10,
    )
    assert details[0].status is LeakDetailStatus.AVAILABLE
    assert [(e.label, e.minutes) for e in details[0].entries] == [("神秘软件", 5.0)]
    # label 取 title or app 的既有语义
    assert details[0].data_status == "ok"


def test_aggregate_leak_details_truncates_to_top_n(config):
    entries = tuple(_entry(f"神秘软件{i}", 10.0 - i) for i in range(5))
    details = queries.aggregate_leak_details(
        [_anchor(T0)], [_snap(T0, entries=entries)],
        taxonomy=config.taxonomy, top_n=2,
    )
    assert [e.label for e in details[0].entries] == ["神秘软件0", "神秘软件1"]


def test_aggregate_leak_details_distinguishes_unavailable_from_empty(config):
    """「取不到明细」与「没查到未命中条目」必须分家 —— 后者更可能还是明细缺失。"""
    from statesense.report.models import LeakDetailStatus

    unavailable, empty = queries.aggregate_leak_details(
        [_anchor(T0), _anchor(T0 + timedelta(minutes=5))],
        [
            _snap(T0, status="unreachable"),
            _snap(T0 + timedelta(minutes=5), entries=(_entry("terminal", 50.0),)),
        ],
        taxonomy=config.taxonomy, top_n=10,
    )
    assert unavailable.status is LeakDetailStatus.UNAVAILABLE
    assert unavailable.data_status == "unreachable"
    assert unavailable.entries == ()
    assert empty.status is LeakDetailStatus.EMPTY
    assert empty.data_status == "ok"


def test_gate_breakdown_counts_ratio_min_passed_and_blocked(store):
    """判据 4 要的是**对照**：只看「被挡多少次」推不出该不该调 ratio_min。"""
    _eval_row(store, T0, gates=[("state_min", True, 1.0, 1.0), ("ratio_min", True, 0.9, 0.75)])
    _eval_row(store, T0 + timedelta(minutes=5),
              gates=[("state_min", True, 1.0, 1.0), ("ratio_min", False, 0.4, 0.75)])
    _eval_row(store, T0 + timedelta(minutes=10),
              gates=[("state_min", False, 0.0, 1.0), ("ratio_min", False, 0.2, 0.75)])

    gb = queries.build_gate_breakdown(store.list_evaluations())
    assert gb.ratio_min_passed == 1
    assert gb.ratio_min_blocked == 2


def test_gate_breakdown_counts_every_failed_gate_not_just_the_first(store):
    """一轮可以同时被 cooldown 与 daily_cap 挡下。

    只看第一个失败闸门的口径会把 daily_cap 的触达次数少算，从而把一个
    恰好触及上限的日子读成「没到上限」。
    """
    _eval_row(store, T0, gates=[
        ("state_min", True, 1.0, 1.0),
        ("ratio_min", True, 0.9, 0.75),
        ("cooldown", False, 5.0, 30.0),
        ("daily_cap", False, 8.0, 8.0),
    ])
    gb = queries.build_gate_breakdown(store.list_evaluations())
    assert dict(gb.blocked_by) == {"cooldown": 1}          # 主因只有一个
    assert dict(gb.blocked_any)["cooldown"] == 1           # 触达次数都要算
    assert dict(gb.blocked_any)["daily_cap"] == 1


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


def _insert_intervention(store, evaluation_id, *, at, action_id, response,
                         channel="foreground_popup"):
    return store.insert_intervention(
        evaluation_id=evaluation_id,
        at=at,
        state="PASSIVE_CONSUMPTION",
        late_night=False,
        action_id=action_id,
        action_text="t",
        delivery_status="delivered",
        channel=channel,
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
    # 没点按钮的那一层叫 "null"，与库里的 SQL NULL 对齐 ——
    # 报告的使用者要能把这一行与自己的 SQL 查询对上，中间多一层翻译就多一次出错机会。
    assert dict(by_resp["null"])["continued"] == 1
    assert ob.ent_before_mean == pytest.approx(45.0)
    assert ob.ent_after_mean == pytest.approx(26.5)
    assert ob.ent_before_median == pytest.approx(45.0)
    assert ob.ent_after_median == pytest.approx(26.5)


def test_outcome_breakdown_median_resists_an_outlier(store):
    """均值被极端值拉动、中位数不动 —— 规格要求两个都出，正是为了看出这件事。"""
    eid = _eval_row(store, T0)
    ids = [
        _insert_intervention(
            store, eid, at=T0 + timedelta(minutes=i), action_id="walk5", response=None
        )
        for i in range(4)
    ]
    for i, (iid, after) in enumerate(zip(ids, (0.0, 100.0, 100.0, 100.0))):
        store.insert_outcome(
            iid, T0 + timedelta(minutes=10 + i),
            OutcomeVerdict("continued", 10.0, after, 10.0),
        )

    ob = queries.build_outcome_breakdown(store.list_outcomes(), store.list_interventions())
    assert ob.ent_after_mean == pytest.approx(75.0)
    assert ob.ent_after_median == pytest.approx(100.0)


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
    """阻挡次数取自 GateBreakdown，不在这里重算 —— 同一件事只能有一个来源。"""
    _eval_row(store, T0, gates=[("state_min", True, 1.0, 1.0), ("cooldown", False, 5.0, 30.0)])
    _eval_row(store, T0 + timedelta(minutes=5),
              gates=[("state_min", True, 1.0, 1.0), ("daily_cap", False, 8.0, 8.0)])
    gates = queries.build_gate_breakdown(store.list_evaluations())
    ib = queries.build_intervention_breakdown(store.list_interventions(), gates)
    assert ib.cooldown_blocks == 1
    assert ib.daily_cap_blocks == 1


def test_intervention_breakdown_counts_a_round_blocked_by_both(store):
    """cooldown 与 daily_cap 同时挡住时，两个计数都要 +1。"""
    _eval_row(store, T0, gates=[
        ("state_min", True, 1.0, 1.0),
        ("cooldown", False, 5.0, 30.0),
        ("daily_cap", False, 8.0, 8.0),
    ])
    gates = queries.build_gate_breakdown(store.list_evaluations())
    ib = queries.build_intervention_breakdown(store.list_interventions(), gates)
    assert ib.cooldown_blocks == 1
    assert ib.daily_cap_blocks == 1


def test_intervention_breakdown_separates_dry_run_from_real_popups(store):
    """`--dry-run` 与真弹窗返回的都是 delivered —— 只有通道能把它们分开。"""
    eid = _eval_row(store, T0)
    _insert_intervention(store, eid, at=T0, action_id="walk5", response=None,
                         channel="recording")
    _insert_intervention(store, eid, at=T0 + timedelta(minutes=31), action_id="walk5",
                         response="accepted", channel="foreground_popup")

    gates = queries.build_gate_breakdown(store.list_evaluations())
    ib = queries.build_intervention_breakdown(store.list_interventions(), gates)
    assert dict(ib.channels) == {"recording": 1, "foreground_popup": 1}


def test_build_trace_is_centred_on_the_moment(store):
    """spec §5.2：`--trace <时刻>` 以该时刻为中心。

    居中而不是「以它为末尾」：看某一刻的轨迹要回答「它当时为什么这么判、
    判完之后又怎样了」，后者在时刻的右边。曾经只取时刻之前的行，
    「之后怎样了」永远看不到。
    """
    for i in range(10):
        _eval_row(store, T0 + timedelta(minutes=5 * i),
                  gates=[("state_min", True, 1.0, 1.0)])
    trace = queries.build_trace(
        store.list_evaluations(), T0 + timedelta(minutes=25), window_ticks=4
    )
    assert len(trace) == 4
    # 在 4 轮的窗口里，at 落在相对下标 2：左边 2 轮、右边 1 轮。
    # 偶数窗口的取舍一律偏向过去 —— 「为什么这么判」的证据在左边。
    assert trace[0].at == T0 + timedelta(minutes=15)
    assert T0 + timedelta(minutes=25) in [row.at for row in trace]
    assert trace[-1].at == T0 + timedelta(minutes=30)


def test_build_trace_before_all_history_still_returns_rows(store):
    """`at` 早于全部记录时从最早一轮开始给 —— 空输出会被读成「工具坏了」。"""
    for i in range(10):
        _eval_row(store, T0 + timedelta(minutes=5 * i))
    trace = queries.build_trace(
        store.list_evaluations(), T0 - timedelta(days=1), window_ticks=4
    )
    assert len(trace) == 4
    assert trace[0].at == T0


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

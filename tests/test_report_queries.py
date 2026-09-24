import json
from datetime import datetime, timedelta, timezone

import pytest

from statesense.intervention.models import Decision, GateResult, parse_gate_trace
from statesense.outcome.models import OutcomeVerdict
from statesense.report import queries
from statesense.shadow.models import ModelCandidate, ModelSignal, ShadowOutcome
from statesense.state.models import State, StateVerdict
from statesense.perception import QUNS_ACCEPTS_NOTIFICATIONS, QUNS_BUSY
from statesense.store.db import Store

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)

#: 写入接口要求显式给出规则版本，不给默认值（见 store/db.py）。
RV = "test-rule-v1"


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
        store.insert_evaluation(T0 + timedelta(minutes=10 * i), _verdict(), _SKIP, rule_version=RV)

    assert len(store.list_evaluations()) == 3
    rows = store.list_evaluations(since=T0 + timedelta(minutes=10))
    assert [r["at"] for r in rows] == [r["at"] for r in store.list_evaluations()][1:]


def test_list_evaluations_is_ordered_by_time_not_insert_order(store):
    """乱序插入也要按时间返回 —— report 的缺口检测依赖时间序。"""
    store.insert_evaluation(T0 + timedelta(minutes=20), _verdict(), _SKIP, rule_version=RV)
    store.insert_evaluation(T0, _verdict(), _SKIP, rule_version=RV)
    store.insert_evaluation(T0 + timedelta(minutes=10), _verdict(), _SKIP, rule_version=RV)
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
              fullscreen_state=None, version=RV):
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
    return store.insert_evaluation(at, verdict, decision, rule_version=version)


def _strip_version(store, evaluation_id: int) -> None:
    """把某行的规则版本改回 NULL —— 「迁移前写入的行」在库里就长这样。

    写入接口不接受 None（不给默认值、必填），所以只能事后抹掉；
    这与 v6 之前写入的历史行在数据上完全同形。
    """
    with store._conn:
        store._conn.execute(
            "UPDATE evaluations SET rule_version = NULL WHERE id = ?", (evaluation_id,)
        )


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


def test_verdict_breakdown_counts_rule_versions_and_keeps_unknown_apart(store):
    """规则版本要能分得开，`unknown` 单独一档。

    分类清单与阈值不落在结果行里，跨版本比较只能靠这一档分开样本。
    把 unknown 并进任何已知版本，都会让「两组规则混算」这件事从数据上消失。
    """
    old = _eval_row(store, T0, version="v-old")
    _strip_version(store, old)
    _eval_row(store, T0 + timedelta(minutes=5), version="v-new")
    _eval_row(store, T0 + timedelta(minutes=10), version="v-new")

    bd = queries.build_verdict_breakdown(store.list_evaluations())
    assert dict(bd.rule_versions) == {"v-new": 2, "unknown": 1}


def test_group_by_rule_version_separates_samples_and_orders_stably(store):
    """分组是跨版本比较的第一步，且顺序必须稳定 —— 否则测试与报告都没法对比。"""
    _eval_row(store, T0, version="v-b")
    _eval_row(store, T0 + timedelta(minutes=5), version="v-a")
    unknown = _eval_row(store, T0 + timedelta(minutes=10), version="v-a")
    _strip_version(store, unknown)

    groups = queries.group_by_rule_version(store.list_evaluations())
    assert [name for name, _ in groups] == ["v-a", "v-b", "unknown"]
    assert [len(rows) for _, rows in groups] == [1, 1, 1]


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
                         channel="foreground_popup", delivery_status="delivered",
                         state="PASSIVE_CONSUMPTION", late_night=False):
    return store.insert_intervention(
        evaluation_id=evaluation_id,
        at=at,
        state=state,
        late_night=late_night,
        action_id=action_id,
        action_text="t",
        delivery_status=delivery_status,
        channel=channel,
        outcome_due_at=at + timedelta(minutes=10),
        user_response=response,
    )


def _null_channel(store, intervention_id: int) -> None:
    """把通道改回 NULL —— 迁移前写入的行在库里就长这样（写入接口要求显式通道）。"""
    with store._conn:
        store._conn.execute(
            "UPDATE interventions SET channel = NULL WHERE id = ?", (intervention_id,)
        )


def _cohort_rows(store, since=None):
    return store.list_intervention_cohort(since=since)


def _timing(store, *, gap_threshold_minutes=15, events=None):
    """按 `__main__` 的同一条路径组装视图 8。

    「消费事件数」与「合并前的可干预轮次数」必须出自同一次切分，测试里也不许
    各算一遍 —— 分开构造会让「19 轮合并成 1 个事件」和「19 轮合并成 2 个事件」
    同时通过测试。
    """
    evaluations = store.list_evaluations()
    if events is None:
        events = queries.build_consumption_events(
            evaluations, gap_threshold_minutes=gap_threshold_minutes
        )
    return queries.build_action_timing_breakdown(
        _cohort_rows(store),
        events,
        raw_ticks=queries.count_intervenable_rounds(
            evaluations, gap_threshold_minutes=gap_threshold_minutes
        ),
        gap_threshold_minutes=gap_threshold_minutes,
    )


def test_cohort_layers_are_mutually_exclusive_and_sum_to_total(store):
    """各层互斥、相加等于干预总数 —— 主分析的分母不能凭空变小。

    少算一层的表现是「分母看起来更干净」，而报告上完全看不出来。
    """
    eid = _eval_row(store, T0)
    minute = lambda n: T0 + timedelta(minutes=n)  # noqa: E731 - 测试内的小工具

    main = _insert_intervention(store, eid, at=minute(0), action_id="walk5", response="accepted")
    store.insert_outcome(main, minute(10), OutcomeVerdict("disengaged", 45.0, 5.0, 10.0))

    _insert_intervention(store, eid, at=minute(1), action_id="walk5", response=None,
                         channel="recording")
    _insert_intervention(store, eid, at=minute(2), action_id="walk5", response=None,
                         delivery_status="failed")
    _insert_intervention(store, eid, at=minute(3), action_id="walk5", response=None)
    no_data = _insert_intervention(store, eid, at=minute(4), action_id="walk5", response=None)
    store.insert_outcome(no_data, minute(14), OutcomeVerdict("no_data", 0.0, 0.0, 0.0))
    legacy = _insert_intervention(store, eid, at=minute(5), action_id="walk5", response=None)
    _null_channel(store, legacy)

    cb = queries.build_cohort_breakdown(_cohort_rows(store))
    assert {layer.name: layer.interventions for layer in cb.layers} == {
        "main": 1,
        "recording": 1,
        "channel_unknown": 1,
        "undelivered": 1,
        "missing_outcome": 1,
        "no_data": 1,
    }
    assert cb.total == 6
    assert sum(layer.interventions for layer in cb.layers) == cb.total
    assert cb.main_interventions == 1


def test_cohort_main_layer_excludes_rehearsal_and_no_data_from_the_means(store):
    """排练与 no_data 不得进效果均值 —— 这就是本版要修的口径问题。

    排练同样返回 delivered，只按用户回应分层时它与真实弹窗完全同形；
    把两者一起平均，「有效」会随排练次数变化而变化。
    """
    eid = _eval_row(store, T0)
    real = _insert_intervention(store, eid, at=T0, action_id="walk5", response=None)
    store.insert_outcome(real, T0 + timedelta(minutes=10), OutcomeVerdict("disengaged", 40.0, 10.0, 10.0))
    rehearsal = _insert_intervention(store, eid, at=T0 + timedelta(minutes=1),
                                     action_id="walk5", response=None, channel="recording")
    store.insert_outcome(rehearsal, T0 + timedelta(minutes=11), OutcomeVerdict("continued", 0.0, 99.0, 10.0))
    empty = _insert_intervention(store, eid, at=T0 + timedelta(minutes=2), action_id="walk5", response=None)
    store.insert_outcome(empty, T0 + timedelta(minutes=12), OutcomeVerdict("no_data", 0.0, 0.0, 0.0))

    cb = queries.build_cohort_breakdown(_cohort_rows(store))
    assert cb.main_interventions == 1
    assert cb.main_ent_before_mean == pytest.approx(40.0)
    assert cb.main_ent_after_mean == pytest.approx(10.0)

    # 对照：旧口径把三者算在一起，均值被排练与 no_data 拉动。
    ob = queries.build_outcome_breakdown(store.list_outcomes(), store.list_interventions())
    assert ob.ent_after_mean != cb.main_ent_after_mean


def test_cohort_filters_by_intervention_time_only(store):
    """区间起点前投递、区间内检查的回执不再被误记为孤儿（阶段 2.1 的验收线）。

    旧写法用两个时间轴拼分母：干预按 `interventions.at`、回执按 `outcomes.checked_at`。
    于是这条回执进得来、对应的干预进不来，报告凭空多出一层 orphan ——
    那不是数据脏，是取数口径错。这里把两种取法的差别钉住。
    """
    eid = _eval_row(store, T0 - timedelta(hours=2))
    early = _insert_intervention(store, eid, at=T0 - timedelta(hours=1),
                                 action_id="walk5", response=None)
    store.insert_outcome(early, T0 + timedelta(minutes=5),
                         OutcomeVerdict("continued", 40.0, 40.0, 10.0))

    # 旧口径：两轴各取一次，回执在区间内、干预在区间外。
    window_outcomes = store.list_outcomes(since=T0)
    window_interventions = store.list_interventions(since=T0)
    assert len(window_outcomes) == 1
    assert window_interventions == []
    orphaned = queries.build_outcome_breakdown(window_outcomes, window_interventions)
    # by_response 是 (层名, 该层回执分布) 的序列，所以先按层名取出来再读分布。
    by_response = dict(orphaned.by_response)
    assert dict(by_response[queries.ORPHAN_INTERVENTION])["continued"] == 1

    # 新口径：一条干预一行，过滤轴只有干预发生时刻 → 同进同出。
    assert _cohort_rows(store, since=T0) == []
    assert len(_cohort_rows(store, since=T0 - timedelta(hours=2))) == 1


def test_cohort_counts_days_and_rule_versions_separately(store):
    """主分析层要给出跨天数和触发时的规则版本 —— 次数不等于证据量。

    五分钟重叠窗口里同一次消费可以弹出多次，只看次数会把「一天里被提醒了
    很多次」读成「很多天的证据」；版本混在一起则无法归因。
    """
    day1 = T0
    day2 = T0 + timedelta(days=1)
    eid1 = _eval_row(store, day1, version="v-a")
    eid2 = _eval_row(store, day2, version="v-b")
    for at, eid in ((day1, eid1), (day1 + timedelta(minutes=5), eid1), (day2, eid2)):
        iid = _insert_intervention(store, eid, at=at, action_id="walk5", response=None)
        store.insert_outcome(iid, at + timedelta(minutes=10),
                             OutcomeVerdict("continued", 40.0, 40.0, 10.0))

    cb = queries.build_cohort_breakdown(_cohort_rows(store))
    assert cb.main_interventions == 3
    assert len(cb.main_days) == 2
    assert dict(cb.main_rule_versions) == {"v-a": 2, "v-b": 1}
    # 跨版本 → 合并均值必须空着，改由分片给出（阶段 1.1 验收：不混算）。
    assert cb.main_ent_before_mean is None
    assert cb.main_ent_after_mean is None
    assert cb.main_ent_before_median is None
    assert [(s.version, s.interventions) for s in cb.versions] == [("v-a", 2), ("v-b", 1)]
    assert sum(s.interventions for s in cb.versions) == cb.main_interventions
    assert [s.ent_before_mean for s in cb.versions] == [40.0, 40.0], "分片各算各的均值"


def test_cohort_single_version_keeps_the_merged_mean(store):
    """只有一个版本时不拆：分片与合并值必然相同，重复一遍像是有两套口径。"""
    eid = _eval_row(store, T0, version="v-only")
    iid = _insert_intervention(store, eid, at=T0, action_id="walk5", response=None)
    store.insert_outcome(
        iid, T0 + timedelta(minutes=10), OutcomeVerdict("continued", 40.0, 10.0, 10.0)
    )

    cb = queries.build_cohort_breakdown(_cohort_rows(store))
    assert cb.versions == ()
    assert cb.main_ent_before_mean == 40.0
    assert cb.main_ent_after_mean == 10.0


def test_cohort_unknown_version_is_its_own_slice_and_goes_last(store):
    """「版本未知」必须单独一档，且不并进已知版本 —— 迁移前的行用的是当时的规则。"""
    known = _eval_row(store, T0, version="v-known")
    legacy = _eval_row(store, T0 + timedelta(minutes=30), version="v-legacy")
    _strip_version(store, legacy)
    for eid, at in ((known, T0), (legacy, T0 + timedelta(minutes=30))):
        iid = _insert_intervention(store, eid, at=at, action_id="walk5", response=None)
        store.insert_outcome(
            iid, at + timedelta(minutes=10), OutcomeVerdict("continued", 40.0, 40.0, 10.0)
        )

    cb = queries.build_cohort_breakdown(_cohort_rows(store))
    assert [s.version for s in cb.versions] == ["v-known", "unknown"], "unknown 殿后"
    assert cb.main_ent_before_mean is None, "含未知版本时同样不许合并"


# ── 阶段 2.2：回执口径审计与阶段 2.3：动作 × 时机 ────────────


def _receipt(store, *, at, trigger_fullscreen, outcome="continued", before=40.0,
             after=40.0, action_id="walk5", evaluation_id=None, channel="foreground_popup"):
    eid = evaluation_id
    if eid is None:
        eid = _eval_row(store, at, fullscreen_state=trigger_fullscreen)
    iid = _insert_intervention(store, eid, at=at, action_id=action_id, response=None,
                               channel=channel)
    store.insert_outcome(iid, at + timedelta(minutes=10),
                         OutcomeVerdict(outcome, before, after, 10.0))
    return iid


def test_receipt_audit_stratifies_by_the_trigger_time_fullscreen_state(store):
    """审计按**触发那一刻**的全屏取值分层 —— 前侧证据受它影响，与通道无关。

    「游戏退出被读成干预有效」这类失真只会出现在游戏触发的那一类里，
    所以比例必须按这一维分开算，而不是给一个总体 no_data 率。
    """
    _receipt(store, at=T0, trigger_fullscreen=QUNS_BUSY)  # 触发时全屏（游戏）
    _receipt(store, at=T0 + timedelta(minutes=1), trigger_fullscreen=QUNS_BUSY,
             outcome="no_data", before=0.0, after=0.0)
    _receipt(store, at=T0 + timedelta(minutes=2), trigger_fullscreen=QUNS_ACCEPTS_NOTIFICATIONS)
    _receipt(store, at=T0 + timedelta(minutes=3), trigger_fullscreen=None)

    audit = queries.build_receipt_audit(_cohort_rows(store))
    strata = {s.name: s for s in audit.strata}
    assert audit.total_receipts == 4
    assert audit.affected_receipts == 2, "触发时在全屏游戏里的回执数"
    assert audit.affected_no_data == 1
    assert strata[queries.RECEIPT_GAMING].receipts == 2
    assert strata[queries.RECEIPT_GAMING].no_data == 1
    assert strata[queries.RECEIPT_NOT_GAMING].receipts == 1
    assert strata[queries.RECEIPT_UNKNOWN].receipts == 1


def test_receipt_audit_ignores_interventions_without_a_receipt(store):
    """没有回执就无从谈前后两值 —— 未结算的行只出现在视图 6 的 missing 层。"""
    eid = _eval_row(store, T0)
    _insert_intervention(store, eid, at=T0, action_id="walk5", response=None)
    _receipt(store, at=T0 + timedelta(minutes=1), trigger_fullscreen=QUNS_BUSY,
             evaluation_id=eid)

    audit = queries.build_receipt_audit(_cohort_rows(store))
    assert audit.total_receipts == 1


def test_consumption_events_merge_overlapping_ticks_into_one_event(store):
    """窗口重叠产生的连续可干预轮次是同一次消费 —— 按轮次当样本会放大十几倍。

    19 轮 PASSIVE 是同一个消费事件，不是 19 个样本。
    """
    for i in range(19):
        _eval_row(store, T0 + timedelta(minutes=5 * i), state="PASSIVE_CONSUMPTION")

    events = queries.build_consumption_events(
        store.list_evaluations(), gap_threshold_minutes=15
    )
    assert len(events) == 1
    assert events[0].ticks == 19
    assert events[0].minutes == 90.0


def test_raw_ticks_counts_rounds_before_merging_not_deliveries(store):
    """`raw_ticks` 是合并前的可干预轮次数，不是投递数。

    它 ÷ `total_events` 才是窗口重叠把样本量放大的倍数。旧实现写成
    `raw_ticks = len(delivered)`，与 `total_deliveries` 恒等 —— 于是「19 轮合并成
    1 个事件」这层信息在 JSON 里彻底看不见，读者只会看到两个相同的数字，
    而它们看起来本可以互相校验。
    """
    first = _eval_row(store, T0, state="PASSIVE_CONSUMPTION")
    for i in range(1, 19):
        _eval_row(store, T0 + timedelta(minutes=5 * i), state="PASSIVE_CONSUMPTION")
    # 不可干预的轮次既不算事件、也不算轮次 —— 阈值测的是「进入被动消费了吗」。
    _eval_row(store, T0 + timedelta(minutes=120), state="NORMAL", ent=5.0)
    _receipt(store, at=T0, trigger_fullscreen=QUNS_ACCEPTS_NOTIFICATIONS,
             action_id="walk5", evaluation_id=first)

    timing = _timing(store)
    assert timing.total_events == 1
    assert timing.total_deliveries == 1
    assert timing.raw_ticks == 19
    assert timing.raw_ticks != timing.total_deliveries, "旧实现里这两个数恒等"


def test_count_intervenable_rounds_matches_events_ticks(store):
    """轮次数必须等于各事件 `ticks` 之和：两个数出自同一次切分。

    各算一遍的后果是「19 轮合并成 1 个事件」与「19 轮合并成 2 个事件」能同时
    出现在同一份报告里，而报表上并排的两个数字看起来能互相校验。
    """
    for i in range(4):
        _eval_row(store, T0 + timedelta(minutes=5 * i), state="PASSIVE_CONSUMPTION")
    for i in range(2):
        _eval_row(store, T0 + timedelta(minutes=90 + 5 * i),
                  state="HIGH_RISK_PASSIVE_CONSUMPTION")

    evaluations = store.list_evaluations()
    events = queries.build_consumption_events(evaluations, gap_threshold_minutes=15)
    rounds = queries.count_intervenable_rounds(
        evaluations, gap_threshold_minutes=15
    )
    assert rounds == sum(e.ticks for e in events) == 6
    assert len(events) == 2, "跨过关机的一段必须是两个事件"


def test_consumption_events_split_on_a_continuity_gap_and_skip_non_intervenable(store):
    """跨过关机的一段不能算同一次消费；NORMAL / WATCH 不算消费。"""
    for i in range(4):
        _eval_row(store, T0 + timedelta(minutes=5 * i), state="PASSIVE_CONSUMPTION")
    _eval_row(store, T0 + timedelta(minutes=25), state="NORMAL", ent=5.0)
    # 65 分钟之后才继续：间隔超过连续性阈值 → 断成两个事件。
    for i in range(2):
        _eval_row(store, T0 + timedelta(minutes=90 + 5 * i),
                  state="HIGH_RISK_PASSIVE_CONSUMPTION")

    events = queries.build_consumption_events(
        store.list_evaluations(), gap_threshold_minutes=15
    )
    assert [(e.ticks, e.peak_state) for e in events] == [
        (4, "PASSIVE_CONSUMPTION"),
        (2, "HIGH_RISK_PASSIVE_CONSUMPTION"),
    ]


def test_action_timing_layers_use_only_the_main_cohort(store):
    """排练没有真实投递，进不了动作时机的分组 —— 与视图 6 同一分母。"""
    eid = _eval_row(store, T0, state="PASSIVE_CONSUMPTION")
    _receipt(store, at=T0, trigger_fullscreen=QUNS_ACCEPTS_NOTIFICATIONS,
             action_id="walk5", evaluation_id=eid)
    _receipt(store, at=T0 + timedelta(minutes=1), trigger_fullscreen=QUNS_ACCEPTS_NOTIFICATIONS,
             action_id="stretch", evaluation_id=eid, channel="recording")

    timing = _timing(store)
    assert [layer.action_id for layer in timing.layers] == ["walk5"]
    assert timing.total_deliveries == 1
    assert timing.layers[0].valid_receipts == 1


def test_action_timing_layers_carry_days_events_and_missing_receipts(store):
    """每层必须给「跨越天数 / 落在几个事件里 / 回执是否可用」三个旁证。

    次数相同、旁证不同的两层证据强度完全不同 —— 计划 2.3 要求低样本层只列数据，
    可读的前提正是这三个数就在同一行上。
    """
    day1 = T0
    day2 = T0 + timedelta(days=1)
    eid1 = _eval_row(store, day1, state="PASSIVE_CONSUMPTION")
    eid2 = _eval_row(store, day2, state="PASSIVE_CONSUMPTION")
    # 同一天、同一个事件里两次投递：不能算成两条独立证据。
    _receipt(store, at=day1, trigger_fullscreen=QUNS_ACCEPTS_NOTIFICATIONS, action_id="walk5",
             evaluation_id=eid1, after=10.0)
    _receipt(store, at=day1 + timedelta(minutes=5), trigger_fullscreen=QUNS_ACCEPTS_NOTIFICATIONS,
             action_id="walk5", evaluation_id=eid1, outcome="no_data", before=0.0, after=0.0)
    # 另一天的一次投递，回执还没结算。
    _insert_intervention(store, eid2, at=day2, action_id="walk5", response=None)

    timing = _timing(store)
    layer = timing.layers[0]
    assert timing.total_deliveries == 3, "分母是真实投递，含 no_data 与未结算"
    assert timing.total_events == 2
    assert timing.total_days == 2
    assert layer.deliveries == 3
    assert layer.valid_receipts == 1
    assert layer.no_data == 1
    assert layer.missing_receipts == 1
    assert layer.days == 2
    assert layer.events == 2
    assert layer.ent_before_mean == pytest.approx(40.0)
    assert layer.ent_after_mean == pytest.approx(10.0)


def test_action_timing_layers_are_sorted_stably(store):
    """分组表必须稳定排序 —— 顺序会漂移的表没法比较两天的结果。"""
    eid = _eval_row(store, T0, state="PASSIVE_CONSUMPTION")
    for action_id in ("stretch", "walk5", "calligraphy"):
        _receipt(store, at=T0 + timedelta(minutes=len(action_id)), trigger_fullscreen=5,
                 action_id=action_id, evaluation_id=eid)

    timing = _timing(store, events=())
    assert [layer.action_id for layer in timing.layers] == [
        "calligraphy", "stretch", "walk5"
    ]


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


# ── 影子模型（阶段 3.4） ────────────────────────────────────

def _shadow_row(
    store,
    evaluation_id: int,
    *,
    outcome="ok",
    candidate="NORMAL",
    model_version="m1",
    latency_ms=12.0,
    reason=None,
    detail=None,
) -> None:
    """往库里写一行影子记录。走真实的写入接口，顺带覆盖外键与列映射。"""
    store.insert_shadow_signal(
        evaluation_id,
        ModelSignal(
            outcome=ShadowOutcome(outcome),
            candidate=None if candidate is None else ModelCandidate(candidate),
            model_version=model_version,
            prompt_version="p1",
            latency_ms=latency_ms,
            reason=reason,
            detail=detail,
        ),
    )


def _shadow_breakdown(store, *, evaluations=None) -> "queries.ShadowBreakdown":
    rows = store.list_evaluations()
    return queries.build_shadow_breakdown(
        store.list_shadow_signals(),
        queries.build_consumption_events(rows, gap_threshold_minutes=15),
        evaluations=len(rows) if evaluations is None else evaluations,
    )


def test_shadow_breakdown_reports_coverage_and_every_outcome(store):
    """结局按封闭枚举**全量**出现，计 0 的档也在。

    少一档就分不清「这一档不存在」与「这一档为 0」，而后者正是要看的：
    「一次超时都没有」是好消息，「一次都没调用」是配置问题。
    """
    first = _eval_row(store, T0)
    _shadow_row(store, first)

    breakdown = _shadow_breakdown(store)

    assert breakdown.evaluations == 1
    assert breakdown.asked == 1
    assert [item.outcome for item in breakdown.outcomes] == [
        outcome.value for outcome in ShadowOutcome
    ]
    counts = {item.outcome: item.count for item in breakdown.outcomes}
    assert counts["ok"] == 1
    assert counts["timeout"] == counts["no_data"] == 0
    assert all(item.note for item in breakdown.outcomes), "每一档都要有人能读的说明"


def test_unknown_outcome_from_a_hand_edited_row_still_appears(store):
    """枚举外的结局单列一档，不能被丢掉 —— 丢掉会让分布之和不等于被问次数。"""
    evaluation_id = _eval_row(store, T0)
    with store._conn:
        store._conn.execute(
            "INSERT INTO shadow_signals (evaluation_id, outcome, candidate, "
            "model_version, prompt_version, latency_ms) VALUES (?, 'wat', NULL, 'm', 'p', 1.0)",
            (evaluation_id,),
        )

    breakdown = _shadow_breakdown(store)

    extra = [item for item in breakdown.outcomes if item.outcome == "wat"]
    assert len(extra) == 1
    assert extra[0].count == 1
    assert sum(item.count for item in breakdown.outcomes) == breakdown.asked


def test_relations_split_into_the_four_documented_buckets(store):
    """一致 / 更重 / 更轻 / 不下结论。方向相反的档不能合并成一个「分歧率」。"""
    ids = [
        _eval_row(store, T0 + timedelta(minutes=5 * i), state="PASSIVE_CONSUMPTION")
        for i in range(4)
    ]
    _shadow_row(store, ids[0], candidate="PASSIVE_CONSUMPTION")  # 一致
    _shadow_row(store, ids[1], candidate="HIGH_RISK_PASSIVE_CONSUMPTION")  # 更重
    _shadow_row(store, ids[2], candidate="WATCH")  # 更轻
    _shadow_row(store, ids[3], candidate="uncertain")  # 不下结论

    breakdown = _shadow_breakdown(store)

    counts = {item.name: item.count for item in breakdown.divergences}
    assert counts == {"一致": 1, "候选更重": 1, "候选更轻": 1, "候选不下结论": 1}


def test_failed_rows_are_not_counted_as_any_relation(store):
    """失败档没有候选，不属于任何关系档 —— 把它算进「一致」会让分歧率失真。"""
    ids = [_eval_row(store, T0 + timedelta(minutes=5 * i)) for i in range(2)]
    _shadow_row(store, ids[0], outcome="timeout", candidate=None, latency_ms=300.0)
    _shadow_row(store, ids[1], candidate="PASSIVE_CONSUMPTION")

    breakdown = _shadow_breakdown(store)

    assert sum(item.count for item in breakdown.divergences) == 1


def test_cross_bar_counts_are_symmetric_in_meaning(store):
    """两侧各数「对面没到档」的轮次，方向相反、互不抵消。"""
    ids = [_eval_row(store, T0 + timedelta(minutes=5 * i)) for i in range(3)]
    _shadow_row(store, ids[0], candidate="PASSIVE_CONSUMPTION")  # 两侧都到档
    _shadow_row(store, ids[1], candidate="uncertain")  # 只有规则到档
    _shadow_row(store, ids[2], candidate="NORMAL")  # 只有规则到档

    breakdown = _shadow_breakdown(store)

    assert breakdown.rule_only_ticks == 2
    assert breakdown.candidate_only_ticks == 0


def test_candidate_reaching_the_bar_alone_is_counted(store):
    """规则判 NORMAL、候选却够到可提醒档 —— 候选侧「更早提醒」的那一类。"""
    evaluation_id = _eval_row(store, T0, ent=5.0, total=60.0, state="NORMAL")
    _shadow_row(store, evaluation_id, candidate="HIGH_RISK_PASSIVE_CONSUMPTION")

    breakdown = _shadow_breakdown(store)

    assert breakdown.candidate_only_ticks == 1
    assert breakdown.rule_only_ticks == 0


def test_latency_stats_ignore_rows_that_did_not_call(store):
    """没调用就没有耗时。若把 0 算进均值，延迟会被「没问的那几轮」拉低。"""
    ids = [_eval_row(store, T0 + timedelta(minutes=5 * i)) for i in range(3)]
    _shadow_row(store, ids[0], latency_ms=100.0)
    _shadow_row(store, ids[1], latency_ms=300.0)
    _shadow_row(store, ids[2], outcome="no_data", candidate=None, latency_ms=None)

    breakdown = _shadow_breakdown(store)

    assert breakdown.latency_mean_ms == 200.0
    assert breakdown.latency_median_ms == 200.0
    assert breakdown.latency_max_ms == 300.0


def test_refusal_rate_denominator_is_asked_ticks(store):
    """拒答率的分母是「真的问了它几次」，不是「区间内评估了几轮」。"""
    ids = [_eval_row(store, T0 + timedelta(minutes=5 * i)) for i in range(2)]
    _shadow_row(store, ids[0], candidate="refused")
    _shadow_row(store, ids[1], candidate="NORMAL")

    breakdown = _shadow_breakdown(store, evaluations=10)

    assert breakdown.refused == 1
    assert breakdown.asked == 2
    assert breakdown.evaluations == 10


def test_model_versions_are_grouped_so_models_are_not_mixed(store):
    """换模型前后不混算 —— 与 rule_version 是同一个原则。"""
    ids = [_eval_row(store, T0 + timedelta(minutes=5 * i)) for i in range(3)]
    _shadow_row(store, ids[0], model_version="m-old")
    _shadow_row(store, ids[1], model_version="m-old")
    _shadow_row(store, ids[2], model_version="m-new")

    breakdown = _shadow_breakdown(store)

    assert dict(breakdown.model_versions) == {"m-old": 2, "m-new": 1}


# ── 首次可提醒时间 ──────────────────────────────────────────

def test_lead_time_counts_a_model_that_reaches_the_bar_before_the_rule(store):
    """段起点是规则第一次够到档的那一轮，所以「更早」只可能发生在段**之前**。

    这里就是那个场景：规则在 T0+15 才够到档，而模型在 T0+10 就够到了。
    """
    for i in range(3):
        _eval_row(store, T0 + timedelta(minutes=5 * i), ent=5.0, state="WATCH")
    for i in range(3, 5):
        _eval_row(store, T0 + timedelta(minutes=5 * i), ent=45.0, state="PASSIVE_CONSUMPTION")
    rows = store.list_evaluations()
    _shadow_row(store, rows[0]["id"], candidate="NORMAL")
    _shadow_row(store, rows[1]["id"], candidate="NORMAL")
    _shadow_row(store, rows[2]["id"], candidate="PASSIVE_CONSUMPTION")
    _shadow_row(store, rows[3]["id"], candidate="PASSIVE_CONSUMPTION")
    _shadow_row(store, rows[4]["id"], candidate="PASSIVE_CONSUMPTION")

    breakdown = _shadow_breakdown(store)

    assert breakdown.lead.events_total == 1
    assert breakdown.lead.model_earlier == 1
    assert breakdown.lead.earlier_mean_minutes == 5.0
    assert breakdown.lead.model_later == breakdown.lead.model_absent == 0


def test_lead_time_reports_same_tick_when_the_model_waits_for_the_rule(store):
    for i in range(3):
        _eval_row(store, T0 + timedelta(minutes=5 * i), ent=5.0, state="WATCH")
    for i in range(3, 5):
        _eval_row(store, T0 + timedelta(minutes=5 * i), ent=45.0, state="PASSIVE_CONSUMPTION")
    rows = store.list_evaluations()
    for index, row in enumerate(rows):
        _shadow_row(store, row["id"], candidate="PASSIVE_CONSUMPTION" if index >= 3 else "NORMAL")

    breakdown = _shadow_breakdown(store)

    assert breakdown.lead.same_tick == 1
    assert breakdown.lead.model_earlier == 0
    assert breakdown.lead.earlier_mean_minutes is None


def test_lead_time_reports_absent_when_the_model_never_reaches_the_bar(store):
    for i in range(5):
        _eval_row(store, T0 + timedelta(minutes=5 * i), ent=45.0, state="PASSIVE_CONSUMPTION")
    for row in store.list_evaluations():
        _shadow_row(store, row["id"], candidate="WATCH")

    breakdown = _shadow_breakdown(store)

    assert breakdown.lead.model_absent == 1
    assert breakdown.lead.model_earlier == breakdown.lead.same_tick == 0


def test_lead_time_does_not_let_one_row_serve_two_events(store):
    """一段影子行只归给它之后最近的那一段事件。

    否则同一行会同时抬高两段事件的提前量 —— 那是「分母被重复使用」的经典形态，
    数字会变大而看不出原因。

    摆法的关键：**唯一够档的影子行必须落在第一段事件的窗口内**，而第二段窗口里
    全程不够档。这样，若窗口的下界（上一段事件的结束时刻）失效，第一段的行就会
    再次服务第二段 —— 第二段凭空得到「提前 120 分钟」，恰好是本测试要拦住的数字。
    """
    # 第一段事件：T0 起（规则与模型同轮够档）。
    _eval_row(store, T0, ent=45.0, state="PASSIVE_CONSUMPTION")
    _eval_row(store, T0 + timedelta(minutes=5), ent=45.0, state="PASSIVE_CONSUMPTION")
    # 第二段事件：越过缺口之后；窗口内模型全程不够档。
    _eval_row(store, T0 + timedelta(minutes=120), ent=45.0, state="PASSIVE_CONSUMPTION")
    _eval_row(store, T0 + timedelta(minutes=125), ent=45.0, state="PASSIVE_CONSUMPTION")
    rows = store.list_evaluations()
    _shadow_row(store, rows[0]["id"], candidate="PASSIVE_CONSUMPTION")  # 服务第一段
    for row in rows[1:]:
        _shadow_row(store, row["id"], candidate="WATCH")

    breakdown = _shadow_breakdown(store)

    assert breakdown.lead.events_total == 2
    assert breakdown.lead.events_with_shadow == 2, "两段窗口里都有影子行，absent 不是「没问」"
    assert breakdown.lead.same_tick == 1, "第一段里模型与规则同轮够档"
    assert breakdown.lead.model_absent == 1, "第二段窗口内模型没有再次够档"
    assert breakdown.lead.model_earlier == 0
    assert breakdown.lead.earlier_mean_minutes is None


def test_events_without_any_shadow_row_are_not_counted_as_agreeing(store):
    """规则侧有消费、影子侧一行都没有：既不算「更早」也不算「更晚」。

    这一类只由 `events_with_shadow` 与总数之差显出来 —— 冒充成任何一档，
    都会把「影子没开那段时间」算成「模型同意规则」。
    """
    for i in range(5):
        _eval_row(store, T0 + timedelta(minutes=5 * i), ent=45.0, state="PASSIVE_CONSUMPTION")

    breakdown = _shadow_breakdown(store)

    assert breakdown.lead.events_total == 1
    assert breakdown.lead.events_with_shadow == 0
    assert breakdown.lead.model_absent == breakdown.lead.same_tick == 0

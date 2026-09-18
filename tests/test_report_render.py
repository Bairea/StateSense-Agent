import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from statesense.report import render
from statesense.report.models import (
    ContinuityGap,
    GateBreakdown,
    InterventionBreakdown,
    LeakAnchor,
    Liveness,
    OutcomeBreakdown,
    Overview,
    ReportData,
    RunEvent,
    VerdictBreakdown,
)

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


def _live(**over) -> Liveness:
    base = dict(
        now=T0,
        last_at=T0,
        silent_minutes=0.0,
        threshold_minutes=15.0,
        offline=False,
        events=(),
    )
    base.update(over)
    return Liveness(**base)


def _data(**over) -> ReportData:
    base = ReportData(
        overview=Overview(T0, T0, 2, 2, 1.0, ()),
        liveness=_live(),
        verdicts=VerdictBreakdown(2, (("NORMAL", 2),), (("ok", 2),), 0, 0, (), 0),
        gates=GateBreakdown((), (), (), 0, 0, (), 0),
        interventions=InterventionBreakdown(0, (), (), (), (), (), (), 0, 0),
        outcomes=OutcomeBreakdown((), (), 0, None, None, None, None),
        leaks=(),
    )
    return replace(base, **over)


# ── 默认输出（spec §5.4）─────────────────────────────────────

def test_default_text_leads_with_overview_and_conclusion():
    out = render.render_text(_data())
    assert "评估轮数" in out
    assert "数据可信" in out


def test_default_text_does_not_dump_every_view():
    """六个视图的表人不会看第二遍 —— 默认只给概览。"""
    out = render.render_text(_data())
    assert "闸门面" not in out
    assert "效果面" not in out
    assert "判定面" not in out


def test_default_output_does_not_dump_the_leak_table():
    """spec §5.4：默认只有概览 + 结论 + 缺口。漏判明细要 --views 5。"""
    data = _data(leaks=(LeakAnchor(T0, 59.9, 0.0, 0.0, 0.0, 59.9, 1.0, 0.0),))
    out = render.render_text(data)
    assert "疑似漏判（" not in out


def test_default_output_still_points_at_the_leak_count():
    """但数量要给一行：它是可执行的结论，藏在参数后面等于这一版的核心产物没产出。"""
    data = _data(leaks=(LeakAnchor(T0, 59.9, 0.0, 0.0, 0.0, 59.9, 1.0, 0.0),))
    out = render.render_text(data)
    assert "疑似漏判    1 轮" in out
    assert "--views 5" in out


def test_leak_pointer_disappears_once_the_view_is_requested():
    data = _data(leaks=(LeakAnchor(T0, 59.9, 0.0, 0.0, 0.0, 59.9, 1.0, 0.0),))
    out = render.render_text(data, views=("5",))
    assert "疑似漏判（" in out
    assert "--views 5" not in out


# ── 空缺 / 缺口 ─────────────────────────────────────────────

def test_empty_database_says_so_and_says_why_it_matters():
    data = _data(
        overview=Overview(None, None, 0, 0, 0.0, ()),
        liveness=_live(last_at=None, silent_minutes=None),
        verdicts=VerdictBreakdown(0, (), (), 0, 0, (), 0),
    )
    out = render.render_text(data)
    assert "无任何评估记录" in out
    assert "进程从未跑起来" in out


def test_gap_is_rendered_with_its_run_event():
    data = _data(
        overview=Overview(
            T0,
            T0,
            2,
            3,
            0.667,
            (ContinuityGap(T0, T0, 200.0, (RunEvent(T0, "sleep_gap", "200.0"),)),),
        )
    )
    out = render.render_text(data)
    assert "缺口" in out
    assert "sleep_gap" in out


def test_gap_without_run_event_is_called_out_as_process_death():
    """这正是本版本要消灭的二义：没有运行事件的缺口只有一个解释。"""
    data = _data(
        overview=Overview(T0, T0, 2, 3, 0.667, (ContinuityGap(T0, T0, 200.0, ()),))
    )
    out = render.render_text(data)
    assert "进程" in out


# ── 此刻是否还在跑（spec §7.4）───────────────────────────────

def test_liveness_online_is_stated_with_its_threshold():
    out = render.render_text(_data(liveness=_live(silent_minutes=3.0)))
    assert "仍在运行" in out
    assert "阈值 15 分钟" in out


def test_liveness_offline_dominates_the_conclusion():
    """其余结论都建立在「进程还在跑」这个前提上，所以掉线必须压过它们。"""
    data = _data(
        liveness=_live(now=T0 + timedelta(minutes=60), silent_minutes=60.0, offline=True)
    )
    out = render.render_text(data)
    assert "此刻已掉线" in out
    assert "60 分钟" in out
    assert "数据可信" not in out


def test_liveness_offline_separates_tick_error_from_process_death():
    data = _data(
        liveness=_live(
            now=T0 + timedelta(minutes=60),
            silent_minutes=60.0,
            offline=True,
            events=(RunEvent(T0 + timedelta(minutes=30), "tick_error", "RuntimeError: boom"),),
        )
    )
    out = render.render_text(data)
    assert "tick_error" in out
    assert "进程大概率已不在运行" not in out


# ── 视图 1 · 判定面 ─────────────────────────────────────────

def test_skipped_rows_are_highlighted():
    data = _data(
        verdicts=VerdictBreakdown(
            2, (("NORMAL", 2),), (("ok", 1), ("unreachable", 1)), 1, 0, (), 0
        )
    )
    out = render.render_text(data)
    assert "skipped" in out
    assert "50.0%" in out


def test_verdict_section_shows_proportions_not_just_counts():
    """spec §5.2：四状态分布要「计数 + 占比」。只给计数看不出严重程度。"""
    data = _data(
        verdicts=VerdictBreakdown(
            4, (("NORMAL", 3), ("WATCH", 1)), (("ok", 4),), 0, 0, (), 0
        )
    )
    out = render.render_text(data, views=("1",))
    assert "NORMAL=3（75.0%）" in out
    assert "WATCH=1（25.0%）" in out


def test_verdict_section_shouts_when_skipped_share_is_high():
    data = _data(
        verdicts=VerdictBreakdown(
            10, (("NORMAL", 10),), (("ok", 7), ("unreachable", 3)), 3, 0, (), 0
        )
    )
    out = render.render_text(data, views=("1",))
    assert "占比偏高" in out


def test_verdict_section_shows_fullscreen_distribution():
    """这条自动推断的准确率只能靠分布审计 —— 必须能看见它取过哪些值，
    以及「无法判定」占了多大比例。"""
    data = _data(
        verdicts=VerdictBreakdown(
            3, (("NORMAL", 3),), (("ok", 3),), 0, 0, (("2", 2), ("unknown", 1)), 0
        )
    )
    out = render.render_text(data, views=("1",))
    assert "全屏信号" in out
    assert "2=2" in out
    assert "unknown=1" in out


def test_verdict_section_discloses_the_leak_blind_spot():
    """有轮次取不到明细时，必须在判定面上说清「漏判结论覆盖了多少轮」。"""
    data = _data(
        verdicts=VerdictBreakdown(2, (("NORMAL", 2),), (("ok", 2),), 0, 0, (), 1)
    )
    out = render.render_text(data, views=("1",))
    assert "明细缺失" in out
    assert "漏判判据对它们不成立" in out


# ── 视图选择 ────────────────────────────────────────────────

def test_views_flag_adds_sections():
    out = render.render_text(_data(), views=("1", "2", "3", "4", "5"))
    assert "判定面" in out
    assert "闸门面" in out
    assert "干预面" in out
    assert "效果面" in out
    assert "疑似漏判" in out


def test_views_flag_is_selective():
    out = render.render_text(_data(), views=("1",))
    assert "判定面" in out
    assert "效果面" not in out


def test_view_ids_cover_the_specs_documented_set():
    """spec §11 写的是 `all|0,1,2,3,4,5`。0 号是默认输出，不必再打印一遍。"""
    assert render.VIEW_IDS == ("0", "1", "2", "3", "4", "5")
    out = render.render_text(_data(), views=("0",))
    assert out.count("概览") == 1


# ── 视图 2 · 闸门面 ─────────────────────────────────────────

def test_gate_section_names_the_blocking_gate_and_shows_ratio():
    data = _data(
        gates=GateBreakdown(
            blocked_by=(("ratio_min", 3),),
            blocked_any=(("ratio_min", 3),),
            ratio_histogram=(("0.7-0.8", 5),),
            ratio_min_passed=2,
            ratio_min_blocked=3,
            state_min_passed_then_blocked=(),
            corrupt_rows=0,
        )
    )
    out = render.render_text(data, views=("2",))
    assert "ratio_min" in out
    assert "0.7-0.8" in out


def test_gate_section_shows_the_ratio_min_contrast():
    """判据 4 要的是对照：只看「被挡多少次」推不出该不该调 ratio_min。"""
    data = _data(
        gates=GateBreakdown(
            (), (), (), ratio_min_passed=7, ratio_min_blocked=3,
            state_min_passed_then_blocked=(), corrupt_rows=0,
        )
    )
    out = render.render_text(data, views=("2",))
    assert "达标 7 轮 / 被挡 3 轮" in out


def test_gate_section_discloses_corrupt_rows():
    """坏行已从统计里剔除，不说出口就会被读成「闸门没挡过」。"""
    data = _data(gates=GateBreakdown((), (), (), 0, 0, (), corrupt_rows=4))
    out = render.render_text(data, views=("2",))
    assert "闸门数据损坏 4 轮" in out


# ── 视图 3 · 干预面 ─────────────────────────────────────────

def test_intervention_section_shows_the_delivery_channel():
    """`--dry-run` 与真弹窗都记 delivered —— 通道是唯一能把它们分开的东西。"""
    data = _data(
        interventions=InterventionBreakdown(
            1, (("2026-09-17", 1),), (("walk5", 1),), (("PASSIVE_CONSUMPTION", 1),),
            (("delivered", 1),), (("recording", 1),), (("null", 1),), 0, 0,
        )
    )
    out = render.render_text(data, views=("3",))
    assert "投递通道" in out
    assert "recording=1" in out


# ── 视图 4 · 效果面 ─────────────────────────────────────────

def test_outcome_section_stratifies_by_response():
    data = _data(
        outcomes=OutcomeBreakdown(
            outcomes=(("disengaged", 1),),
            by_response=(("accepted", (("disengaged", 1),)),),
            no_data=0,
            ent_before_mean=45.0,
            ent_after_mean=3.0,
            ent_before_median=45.0,
            ent_after_median=3.0,
        )
    )
    out = render.render_text(data, views=("4",))
    assert "accepted" in out
    assert "disengaged" in out


def test_outcome_section_shows_medians_and_no_data_share():
    """spec §5.2 要均值与中位数都给，以及 no_data 占比。"""
    data = _data(
        outcomes=OutcomeBreakdown(
            outcomes=(("continued", 3), ("no_data", 1)),
            by_response=(),
            no_data=1,
            ent_before_mean=45.0,
            ent_after_mean=3.0,
            ent_before_median=44.0,
            ent_after_median=2.5,
        )
    )
    out = render.render_text(data, views=("4",))
    assert "均值 45.0  中位数 44.0" in out
    assert "均值 3.0  中位数 2.5" in out
    assert "（25.0%）" in out


# ── 视图 5 · 漏判 ───────────────────────────────────────────

def test_leak_anchors_show_every_bucket():
    """spec §5.2：要给出 ent / gray / work，人才判断得出这是漏判还是别的。"""
    data = _data(leaks=(LeakAnchor(T0, 59.9, 3.0, 5.0, 10.0, 41.9, 0.699, 1.9),))
    out = render.render_text(data, views=("5",))
    assert "娱乐=3.0" in out
    assert "灰色=5.0" in out
    assert "工作=10.0" in out
    assert "未归类=41.9" in out


def test_leak_section_states_its_blind_spot():
    """读者会在这一节下「没有漏判」的结论 —— 那句话对明细缺失的轮次并不成立。"""
    data = _data(verdicts=VerdictBreakdown(2, (("NORMAL", 2),), (("ok", 2),), 0, 0, (), 3))
    out = render.render_text(data, views=("5",))
    assert "另有 3 轮报活跃却没有条目明细" in out
    assert "（无）" in out


def test_leak_detail_lines_are_rendered_when_present():
    data = _data(leak_details=("  Brotato.exe  46.9 分钟",))
    out = render.render_text(data, views=("5",))
    assert "Brotato.exe" in out


# ── 轨迹 ────────────────────────────────────────────────────

def test_trace_is_rendered_when_present():
    from statesense.report.models import TraceRow

    data = _data(
        trace=(
            TraceRow(T0, "WATCH", 0.4, 24.0, 60.0, False, "skip",
                     (("ratio_min", False, 0.4, 0.75),)),
        )
    )
    out = render.render_text(data)
    assert "轨迹" in out
    assert "WATCH" in out


# ── JSON ────────────────────────────────────────────────────

def test_json_output_is_parseable():
    payload = json.loads(render.render_json(_data()))
    assert payload["overview"]["evaluations"] == 2
    assert payload["leaks"] == []


def test_json_output_carries_the_liveness_verdict():
    payload = json.loads(render.render_json(_data()))
    assert payload["liveness"]["offline"] is False
    assert payload["liveness"]["threshold_minutes"] == 15.0


def test_json_output_serializes_datetimes():
    payload = json.loads(
        render.render_json(
            _data(overview=Overview(T0, T0, 1, 1, 1.0, (ContinuityGap(T0, T0, 5.0, ()),)))
        )
    )
    assert isinstance(payload["overview"]["gaps"][0]["minutes"], float)


def _strict_loads(text: str):
    """拒绝 Infinity / NaN 的解析器 —— jq、JavaScript、Go 都是这么干的。"""

    def reject(constant: str):
        raise ValueError(f"non-finite constant: {constant}")

    return json.loads(text, parse_constant=reject)


def test_json_output_never_emits_non_finite_constants():
    """实测踩到过：旧版本往 gate_trace 里写过 Infinity（表示「从未干预过」），
    原样输出会让 `--format json` 变成非法 JSON，而规格说它正是给 jq 用的。
    """
    from statesense.report.models import TraceRow

    data = _data(
        trace=(
            TraceRow(
                T0,
                "WATCH",
                0.47,
                26.4,
                56.5,
                False,
                "skip",
                (("cooldown", True, float("inf"), 30.0),),
            ),
        )
    )
    text = render.render_json(data)
    assert "Infinity" not in text
    assert _strict_loads(text)["trace"][0]["gates"][0][2] is None


def test_json_output_replaces_nan_too():
    data = _data(overview=Overview(T0, T0, 1, 1, float("nan"), ()))
    text = render.render_json(data)
    assert "NaN" not in text
    assert _strict_loads(text)["overview"]["coverage"] is None

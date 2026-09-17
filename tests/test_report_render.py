import json
from dataclasses import replace
from datetime import datetime, timezone

from statesense.report import render
from statesense.report.models import (
    ContinuityGap,
    GateBreakdown,
    InterventionBreakdown,
    LeakAnchor,
    OutcomeBreakdown,
    Overview,
    ReportData,
    RunEvent,
    VerdictBreakdown,
)

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


def _data(**over) -> ReportData:
    base = ReportData(
        overview=Overview(T0, T0, 2, 2, 1.0, ()),
        verdicts=VerdictBreakdown(2, (("NORMAL", 2),), (("ok", 2),), 0, 0),
        gates=GateBreakdown((), (), ()),
        interventions=InterventionBreakdown(0, (), (), (), (), (), 0, 0),
        outcomes=OutcomeBreakdown((), (), 0, None, None),
        leaks=(),
    )
    return replace(base, **over)


def test_default_text_leads_with_overview_and_conclusion():
    out = render.render_text(_data())
    assert "评估轮数" in out
    assert "数据可信" in out


def test_default_text_does_not_dump_every_view():
    """六个视图的表人不会看第二遍 —— 默认只给概览。"""
    out = render.render_text(_data())
    assert "闸门面" not in out
    assert "效果面" not in out


def test_empty_database_says_so_and_says_why_it_matters():
    data = _data(
        overview=Overview(None, None, 0, 0, 0.0, ()),
        verdicts=VerdictBreakdown(0, (), (), 0, 0),
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


def test_skipped_rows_are_highlighted():
    data = _data(
        verdicts=VerdictBreakdown(
            2, (("NORMAL", 2),), (("ok", 1), ("unreachable", 1)), 1, 0
        )
    )
    out = render.render_text(data)
    assert "skipped" in out
    assert "50.0%" in out


def test_leak_anchors_are_rendered():
    data = _data(leaks=(LeakAnchor(T0, 59.9, 0.0, 0.0, 0.0, 59.9, 1.0, 0.0),))
    out = render.render_text(data)
    assert "疑似漏判" in out
    assert "59.9" in out


def test_leak_detail_lines_are_rendered_when_present():
    data = _data(leak_details=("  Brotato.exe  46.9 分钟",))
    out = render.render_text(data)
    assert "Brotato.exe" in out


def test_views_flag_adds_sections():
    out = render.render_text(_data(), views=("1", "2", "3", "4"))
    assert "判定面" in out
    assert "闸门面" in out
    assert "干预面" in out
    assert "效果面" in out


def test_views_flag_is_selective():
    out = render.render_text(_data(), views=("1",))
    assert "判定面" in out
    assert "效果面" not in out


def test_gate_section_names_the_blocking_gate_and_shows_ratio():
    data = _data(
        gates=GateBreakdown(
            blocked_by=(("ratio_min", 3),),
            ratio_histogram=(("0.7-0.8", 5),),
            state_min_passed_then_blocked=(),
        )
    )
    out = render.render_text(data, views=("2",))
    assert "ratio_min" in out
    assert "0.7-0.8" in out


def test_outcome_section_stratifies_by_response():
    data = _data(
        outcomes=OutcomeBreakdown(
            outcomes=(("disengaged", 1),), by_response=(("accepted", (("disengaged", 1),)),),
            no_data=0, ent_before_mean=45.0, ent_after_mean=3.0,
        )
    )
    out = render.render_text(data, views=("4",))
    assert "accepted" in out
    assert "disengaged" in out


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


def test_json_output_is_parseable():
    payload = json.loads(render.render_json(_data()))
    assert payload["overview"]["evaluations"] == 2
    assert payload["leaks"] == []


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

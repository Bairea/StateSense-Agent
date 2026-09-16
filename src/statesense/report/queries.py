"""把 Store 交出的行聚合成意思。

纯函数：只接受「行序列」，不接受 Store。这样它们不需要数据库、不需要网络
就能被测试，也保证 report 无法顺手写库。
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from statesense._time import parse_iso as _parse
from statesense.report.models import (
    ContinuityGap,
    GateBlock,
    GateBreakdown,
    InterventionBreakdown,
    LeakAnchor,
    OutcomeBreakdown,
    Overview,
    RunEvent,
    TraceRow,
    VerdictBreakdown,
)

NO_RESPONSE = "none"


def _ranked(counter: Counter[str]) -> tuple[tuple[str, int], ...]:
    """按次数降序、同次数按键升序 —— 输出必须稳定，否则测试与对比都无从谈起。"""
    return tuple(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def _parse_trace(text: str) -> tuple[dict[str, Any], ...]:
    """闸门留痕解析。坏数据返回空元组，由调用方当作「没有闸门信息」处理，
    绝不中断整份报告 —— 一行坏数据不该让观测失效。"""
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        return ()
    if not isinstance(raw, list):
        return ()
    return tuple(
        g for g in raw if isinstance(g, dict) and "name" in g and "passed" in g
    )


def _first_failed(row: Any) -> dict[str, Any] | None:
    return next((g for g in _parse_trace(row["gate_trace"]) if not g["passed"]), None)


def _ratio_bucket(ratio: float, buckets: int) -> str:
    index = max(0, min(int(ratio * buckets), buckets - 1))
    return f"{index / buckets:.1f}-{(index + 1) / buckets:.1f}"


def build_overview(
    evaluations: Sequence[Any],
    run_events: Iterable[Any],
    *,
    evaluate_every_minutes: int,
    gap_threshold_minutes: float,
) -> Overview:
    moments = [_parse(r["at"]) for r in evaluations]
    events = tuple(RunEvent(_parse(r["at"]), r["kind"], r["detail"]) for r in run_events)
    if not moments:
        return Overview(None, None, 0, 0, 0.0, ())

    first, last = moments[0], moments[-1]
    span = (last - first).total_seconds() / 60
    expected = int(span // evaluate_every_minutes) + 1 if span > 0 else 1
    coverage = len(moments) / expected if expected else 0.0

    gaps: list[ContinuityGap] = []
    for previous, current in zip(moments, moments[1:]):
        minutes = (current - previous).total_seconds() / 60
        if minutes > gap_threshold_minutes:
            inside = tuple(e for e in events if previous <= e.at <= current)
            gaps.append(ContinuityGap(previous, current, round(minutes, 1), inside))
    return Overview(first, last, len(moments), expected, round(coverage, 3), tuple(gaps))


def build_verdict_breakdown(evaluations: Sequence[Any]) -> VerdictBreakdown:
    states: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    skipped = late_night = 0
    for row in evaluations:
        states[row["state"]] += 1
        statuses[row["data_status"]] += 1
        skipped += int(row["skipped"])
        late_night += int(row["late_night"])
    return VerdictBreakdown(
        total=len(evaluations),
        states=_ranked(states),
        data_statuses=_ranked(statuses),
        skipped=skipped,
        late_night=late_night,
    )


def build_gate_breakdown(
    evaluations: Sequence[Any], *, ratio_buckets: int = 10
) -> GateBreakdown:
    blocked: Counter[str] = Counter()
    histogram: Counter[str] = Counter()
    candidates: list[GateBlock] = []

    for row in evaluations:
        failing = _first_failed(row)
        if failing is not None:
            blocked[failing["name"]] += 1
            # state_min 挡下的轮次不算「该提醒但没提醒」—— 那本来就不该提醒。
            if failing["name"] != "state_min":
                candidates.append(
                    GateBlock(
                        at=_parse(row["at"]),
                        gate=failing["name"],
                        value=failing.get("value"),
                        threshold=failing["threshold"],
                    )
                )
        if row["total_active_minutes"] > 0:
            histogram[_ratio_bucket(row["ent_ratio"], ratio_buckets)] += 1

    return GateBreakdown(
        blocked_by=_ranked(blocked),
        ratio_histogram=tuple(sorted(histogram.items())),
        state_min_passed_then_blocked=tuple(candidates),
    )


def build_intervention_breakdown(
    evaluations: Sequence[Any], interventions: Sequence[Any]
) -> InterventionBreakdown:
    per_day: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    states: Counter[str] = Counter()
    deliveries: Counter[str] = Counter()
    responses: Counter[str] = Counter()

    for row in interventions:
        per_day[_parse(row["at"]).astimezone().date().isoformat()] += 1
        actions[row["action_id"]] += 1
        states[row["state"]] += 1
        deliveries[row["delivery_status"]] += 1
        responses[row["user_response"] or NO_RESPONSE] += 1

    blocked: Counter[str] = Counter()
    for row in evaluations:
        failing = _first_failed(row)
        if failing is not None:
            blocked[failing["name"]] += 1

    return InterventionBreakdown(
        total=len(interventions),
        per_day=tuple(sorted(per_day.items())),
        actions=_ranked(actions),
        states=_ranked(states),
        delivery_statuses=_ranked(deliveries),
        user_responses=_ranked(responses),
        cooldown_blocks=blocked["cooldown"],
        daily_cap_blocks=blocked["daily_cap"],
    )


def build_outcome_breakdown(
    outcomes: Sequence[Any], interventions: Sequence[Any]
) -> OutcomeBreakdown:
    response_by_id = {r["id"]: (r["user_response"] or NO_RESPONSE) for r in interventions}
    counts: Counter[str] = Counter()
    stratified: dict[str, Counter[str]] = defaultdict(Counter)
    before: list[float] = []
    after: list[float] = []
    no_data = 0

    for row in outcomes:
        outcome = row["outcome"]
        counts[outcome] += 1
        if outcome == "no_data":
            # no_data 不是说「用户没被影响」，而是说「这段窗口取不到数」。
            # 把它算进均值会把效果往零拉，所以只计数、不参与均值。
            no_data += 1
            continue
        layer = response_by_id.get(row["intervention_id"], "unknown")
        stratified[layer][outcome] += 1
        before.append(row["ent_before"])
        after.append(row["ent_after"])

    return OutcomeBreakdown(
        outcomes=_ranked(counts),
        by_response=tuple((k, _ranked(v)) for k, v in sorted(stratified.items())),
        no_data=no_data,
        ent_before_mean=round(statistics.fmean(before), 2) if before else None,
        ent_after_mean=round(statistics.fmean(after), 2) if after else None,
    )


def find_leak_anchors(
    evaluations: Sequence[Any],
    *,
    min_active_minutes: float,
    min_unclassified_ratio: float,
) -> tuple[LeakAnchor, ...]:
    """找出「有事发生但规则一条都没命中」的轮次。

    必须把两种成因分开：
      · entries - (ent+gray+work) → 真的未命中任何规则（漏判）
      · total - entries           → 明细没取到，不是漏判
    混在一起正是本版本要消灭的那类混淆。
    """
    anchors: list[LeakAnchor] = []
    for row in evaluations:
        total = row["total_active_minutes"]
        if total <= 0 or total < min_active_minutes:
            continue
        entries = row["entries_minutes"]
        missing = max(total - entries, 0.0)
        unclassified = max(
            entries - (row["ent_minutes"] + row["gray_minutes"] + row["work_minutes"]), 0.0
        )
        ratio = unclassified / total
        if ratio < min_unclassified_ratio:
            continue
        anchors.append(
            LeakAnchor(
                at=_parse(row["at"]),
                total_active_minutes=total,
                ent_minutes=row["ent_minutes"],
                gray_minutes=row["gray_minutes"],
                work_minutes=row["work_minutes"],
                unclassified_minutes=round(unclassified, 2),
                unclassified_ratio=round(ratio, 3),
                missing_detail_minutes=round(missing, 2),
            )
        )
    return tuple(anchors)


def build_trace(
    evaluations: Sequence[Any], at: datetime, *, window_ticks: int = 12
) -> tuple[TraceRow, ...]:
    """以 `at` 之前（含）最近的一轮为末尾，往前取 window_ticks 轮。

    末尾必须落在 at 或它之前 —— 否则「看某一刻的轨迹」会包含那一刻之后的事。
    """
    moments = [_parse(r["at"]) for r in evaluations]
    end = 0
    for index, moment in enumerate(moments):
        if moment <= at:
            end = index
        else:
            break
    start = max(0, end - window_ticks + 1)

    rows: list[TraceRow] = []
    for row in evaluations[start : end + 1]:
        rows.append(
            TraceRow(
                at=_parse(row["at"]),
                state=row["state"],
                ent_ratio=row["ent_ratio"],
                ent_minutes=row["ent_minutes"],
                total_active_minutes=row["total_active_minutes"],
                skipped=bool(row["skipped"]),
                decision=row["decision"],
                gates=tuple(
                    (g["name"], bool(g["passed"]), g.get("value"), g["threshold"])
                    for g in _parse_trace(row["gate_trace"])
                ),
            )
        )
    return tuple(rows)

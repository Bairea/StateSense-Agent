"""把 Store 交出的行聚合成意思。

纯函数：只接受「行序列」，不接受 Store。这样它们不需要数据库、不需要网络
就能被测试，也保证 report 无法顺手写库。
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from statesense._time import parse_iso as _parse
from statesense.intervention.models import first_failed, parse_gate_trace
from statesense.report.models import (
    ContinuityGap,
    GateBlock,
    GateBreakdown,
    InterventionBreakdown,
    LeakAnchor,
    Liveness,
    OutcomeBreakdown,
    Overview,
    RunEvent,
    TraceRow,
    VerdictBreakdown,
)

#: 干预行存在，但用户压根没理会弹窗（`user_response IS NULL`）。
#: 名字直接写 "null"，与库里的 SQL NULL 对齐 —— 报告的使用者要能把这一行
#: 与 SQL 查询对上，中间多一层「none」就多一次翻译。
NO_RESPONSE = "null"
#: outcomes 指向的干预行不存在。这是**数据不完整**，不是「用户没回应」，
#: 两者混为一谈会让分层分析悄悄少算一层。
ORPHAN_INTERVENTION = "orphan"


def _ranked(counter: Counter[str]) -> tuple[tuple[str, int], ...]:
    """按次数降序、同次数按键升序 —— 输出必须稳定，否则测试与对比都无从谈起。"""
    return tuple(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def _count(items: Sequence[tuple[str, int]], key: str) -> int:
    return next((count for name, count in items if name == key), 0)


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
    # 分母是「按节奏本该有多少轮」：区间跨度里包含的节奏点数。
    # 不用规格 §5.2 写的 `轮数 × every / 区间分钟数`，因为它在正常满跑时
    # 会算出 >100%（span=30、every=5、轮数=7 时是 1.167）—— 覆盖率超过 100%
    # 只会让人怀疑这个数字本身。见验证日志的偏差记录。
    expected = int(span // evaluate_every_minutes) + 1 if span > 0 else 1
    coverage = len(moments) / expected if expected else 0.0

    gaps: list[ContinuityGap] = []
    for previous, current in zip(moments, moments[1:]):
        minutes = (current - previous).total_seconds() / 60
        if minutes > gap_threshold_minutes:
            inside = tuple(e for e in events if previous <= e.at <= current)
            gaps.append(ContinuityGap(previous, current, round(minutes, 1), inside))
    return Overview(first, last, len(moments), expected, round(coverage, 3), tuple(gaps))


def build_liveness(
    evaluations: Sequence[Any],
    run_events: Iterable[Any],
    now: datetime,
    *,
    gap_threshold_minutes: float,
) -> Liveness:
    """此刻是否还在跑（spec §7.4）。

    这一项与 `build_overview` 的缺口列表是**两个不同的问题**：
    缺口回答「过去断过没有」，它回答「现在断了吗」。同一个阈值两用，
    是规格 §10 对这一个配置键的设计，不是巧合。

    `now` 必须由调用方传入（`clock.now()`），本函数不读时钟 —— 保持纯函数，
    报告才可能在测试里被固定在某一刻复现。
    """
    moments = [_parse(r["at"]) for r in evaluations]
    if not moments:
        return Liveness(
            now,
            None,
            None,
            threshold_minutes=gap_threshold_minutes,
            offline=False,
            events=(),
        )

    last = moments[-1]
    silent = (now - last).total_seconds() / 60
    events = tuple(
        RunEvent(_parse(r["at"]), r["kind"], r["detail"])
        for r in run_events
        if _parse(r["at"]) >= last
    )
    # 时钟回拨或库里出现未来行时 silent 会为负，一律不算掉线 ——
    # 「未来有数据」不是「进程死了」。
    offline = silent > gap_threshold_minutes
    return Liveness(
        now,
        last,
        round(silent, 1),
        threshold_minutes=gap_threshold_minutes,
        offline=offline,
        events=events,
    )


def build_verdict_breakdown(evaluations: Sequence[Any]) -> VerdictBreakdown:
    states: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    fullscreen: Counter[str] = Counter()
    skipped = late_night = entries_unknown = 0
    for row in evaluations:
        states[row["state"]] += 1
        statuses[row["data_status"]] += 1
        # 键用字符串：None（无法判定）要能与 5（正常）区分开，
        # 混成一个数就再也答不上「到底探测成功过几次」。
        # 不再判断列是否存在：run_report 拒绝任何低于当前 SCHEMA_VERSION 的库，
        # 所以读到这里时列一定在。为列存在性加分支只会掩盖真正需要判断的东西 ——
        # 「明细缺失」是**值**层面的问题（见下面的 entries_unknown），不是列层面的。
        value = row["fullscreen_state"]
        fullscreen["unknown" if value is None else str(value)] += 1
        skipped += int(row["skipped"])
        late_night += int(row["late_night"])
        # 报了活跃却没有任何条目明细：这些轮次上漏判视图无法判定。
        # 迁移前写入的行（entries_minutes 取 DEFAULT 0）也落在这一档，
        # 与「Screenpipe 真的没返回明细」在数据上不可区分 —— 所以只能一起计数、
        # 一起说明，而不是假装其中一种不存在。
        if row["total_active_minutes"] > 0 and row["entries_minutes"] <= 0:
            entries_unknown += 1
    return VerdictBreakdown(
        total=len(evaluations),
        states=_ranked(states),
        data_statuses=_ranked(statuses),
        skipped=skipped,
        late_night=late_night,
        fullscreen_states=_ranked(fullscreen),
        entries_unknown=entries_unknown,
    )


def build_gate_breakdown(
    evaluations: Sequence[Any], *, ratio_buckets: int = 10
) -> GateBreakdown:
    blocked_first: Counter[str] = Counter()
    blocked_any: Counter[str] = Counter()
    histogram: Counter[str] = Counter()
    candidates: list[GateBlock] = []
    ratio_min_passed = ratio_min_blocked = corrupt = 0

    for row in evaluations:
        # 分布用的是 ent_ratio 列，与闸门留痕无关 —— 坏行同样计入。
        if row["total_active_minutes"] > 0:
            histogram[_ratio_bucket(row["ent_ratio"], ratio_buckets)] += 1

        trace = parse_gate_trace(row["gate_trace"])
        if trace is None:
            # 读不出任何一条闸门：计数后跳过。既不能当「没有闸门」，
            # 也不能让整份报告中断 —— 数据坏掉的时候更需要报告能跑完。
            corrupt += 1
            continue

        # 「每一个未通过的闸门」都要计数：一轮可以同时被 cooldown 与 daily_cap 挡下，
        # 只看第一个会把 daily_cap 的触达次数少算。
        for gate in trace:
            if not gate.passed:
                blocked_any[gate.name] += 1
            if gate.name == "ratio_min":
                if gate.passed:
                    ratio_min_passed += 1
                else:
                    ratio_min_blocked += 1

        failing = first_failed(trace)
        if failing is not None:
            blocked_first[failing.name] += 1
            # state_min 挡下的轮次不算「该提醒但没提醒」—— 那本来就不该提醒。
            if failing.name != "state_min":
                candidates.append(
                    GateBlock(
                        at=_parse(row["at"]),
                        gate=failing.name,
                        value=failing.value,
                        threshold=failing.threshold,
                    )
                )

    return GateBreakdown(
        blocked_by=_ranked(blocked_first),
        blocked_any=_ranked(blocked_any),
        ratio_histogram=tuple(sorted(histogram.items())),
        ratio_min_passed=ratio_min_passed,
        ratio_min_blocked=ratio_min_blocked,
        state_min_passed_then_blocked=tuple(candidates),
        corrupt_rows=corrupt,
    )


def build_intervention_breakdown(
    interventions: Sequence[Any], gates: GateBreakdown
) -> InterventionBreakdown:
    """`gates` 由调用方传进来，不在这里重算。

    cooldown / daily_cap 的阻挡次数只能有一个来源：重算一份就是又造出
    「同一件事两处各算一份」，而这两份迟早会在某次改动后漂移。
    """
    per_day: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    states: Counter[str] = Counter()
    deliveries: Counter[str] = Counter()
    channels: Counter[str] = Counter()
    responses: Counter[str] = Counter()

    for row in interventions:
        per_day[_parse(row["at"]).astimezone().date().isoformat()] += 1
        actions[row["action_id"]] += 1
        states[row["state"]] += 1
        deliveries[row["delivery_status"]] += 1
        channels[row["channel"] or "unknown"] += 1
        responses[row["user_response"] or NO_RESPONSE] += 1

    return InterventionBreakdown(
        total=len(interventions),
        per_day=tuple(sorted(per_day.items())),
        actions=_ranked(actions),
        states=_ranked(states),
        delivery_statuses=_ranked(deliveries),
        channels=_ranked(channels),
        user_responses=_ranked(responses),
        cooldown_blocks=_count(gates.blocked_any, "cooldown"),
        daily_cap_blocks=_count(gates.blocked_any, "daily_cap"),
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
        layer = response_by_id.get(row["intervention_id"], ORPHAN_INTERVENTION)
        stratified[layer][outcome] += 1
        before.append(row["ent_before"])
        after.append(row["ent_after"])

    return OutcomeBreakdown(
        outcomes=_ranked(counts),
        by_response=tuple((k, _ranked(v)) for k, v in sorted(stratified.items())),
        no_data=no_data,
        ent_before_mean=round(statistics.fmean(before), 2) if before else None,
        ent_after_mean=round(statistics.fmean(after), 2) if after else None,
        ent_before_median=round(statistics.median(before), 2) if before else None,
        ent_after_median=round(statistics.median(after), 2) if after else None,
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

    取不到明细（`entries == 0` 而 `total > 0`）的轮次在这里**必然不产生锚点**：
    两个差额都退化。它们由 `VerdictBreakdown.entries_unknown` 单独计数，
    并由渲染层明确说出口 —— 静默略过会被读成「这些轮次没有漏判」。
    """
    anchors: list[LeakAnchor] = []
    for row in evaluations:
        total = row["total_active_minutes"]
        if total < min_active_minutes:
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
    """以 `at` 为中心的轨迹（spec §5.2）。

    「为中心」而不是「为末尾」：看某一刻的轨迹，要回答的是「它当时为什么这么判、
    判完之后又怎样了」，后者在 `at` 右边。左半取 `at` 之前（含最近一轮），
    右半取之后。

    `at` 早于全部记录时不返回空：改为从最早一轮开始给。空输出会被读成
    「工具坏了」，而实际情况是「那时候还没有数据」—— 那是两件不同的事。
    """
    moments = [_parse(r["at"]) for r in evaluations]
    pivot = -1
    for index, moment in enumerate(moments):
        if moment <= at:
            pivot = index
        else:
            break

    left = window_ticks // 2
    start = max(0, pivot - left)
    rows: list[TraceRow] = []
    for row in evaluations[start : start + window_ticks]:
        trace = parse_gate_trace(row["gate_trace"]) or ()
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
                    (g.name, g.passed, g.value, g.threshold) for g in trace
                ),
            )
        )
    return tuple(rows)

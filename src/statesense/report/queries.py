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
from statesense.config import (
    GATE_COOLDOWN,
    GATE_DAILY_CAP,
    GATE_RATIO_MIN,
    GATE_STATE_MIN,
    TaxonomyConfig,
)
from statesense.intervention.models import first_failed, parse_gate_trace
from statesense.report.models import (
    CohortBreakdown,
    CohortLayer,
    ContinuityGap,
    GateBlock,
    GateBreakdown,
    InterventionBreakdown,
    LeakAnchor,
    LeakDetailStatus,
    LeakEntryLine,
    LeakWindowDetail,
    Liveness,
    OutcomeBreakdown,
    Overview,
    RunEvent,
    TraceRow,
    VerdictBreakdown,
)
from statesense.state.taxonomy import Category, classify

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


def _moments(evaluations: Sequence[Any]) -> list[datetime]:
    """评估行的时刻序列。store 已按时间序返回，这里只负责解析这一件事。"""
    return [_parse(r["at"]) for r in evaluations]


def _run_events(rows: Iterable[Any]) -> tuple[RunEvent, ...]:
    return tuple(RunEvent(_parse(r["at"]), r["kind"], r["detail"]) for r in rows)


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
    moments = _moments(evaluations)
    events = _run_events(run_events)
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
    moments = _moments(evaluations)
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
    events = tuple(e for e in _run_events(run_events) if e.at >= last)
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


#: 规则版本缺失（迁移前写入的行）。与 `fullscreen_states` 的 unknown 同一约定：
#: 「未知」要能与任何一个真实取值区分开，混成一个数就再也答不上「这些行是哪套规则判的」。
UNKNOWN_VERSION = "unknown"


def group_by_rule_version(
    evaluations: Sequence[Any],
) -> tuple[tuple[str, tuple[Any, ...]], ...]:
    """按判定规则版本把评估行分组，`unknown` 始终单独一组并排在最后。

    跨版本比较（阶段 1.3）的第一步就是这一步：分类清单与阈值改了之后，
    历史行不会变，也读不出当时用的是哪套规则 —— 只有这个标识能分。
    两个版本的行混在一起算「同一批样本」，得出的差异既可能是阈值造成的，
    也可能是规则本身换了，而数据里分辨不出来。

    排序刻意稳定（已知版本按键升序、unknown 殿后），否则分组结果的顺序会随
    字典遍历顺序漂移，测试与报告的对比都无从谈起。
    """
    groups: dict[str, list[Any]] = defaultdict(list)
    for row in evaluations:
        groups[row["rule_version"] or UNKNOWN_VERSION].append(row)
    known = sorted((name, rows) for name, rows in groups.items() if name != UNKNOWN_VERSION)
    tail = (
        [(UNKNOWN_VERSION, groups[UNKNOWN_VERSION])] if UNKNOWN_VERSION in groups else []
    )
    return tuple((name, tuple(rows)) for name, rows in [*known, *tail])


def build_verdict_breakdown(evaluations: Sequence[Any]) -> VerdictBreakdown:
    states: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    fullscreen: Counter[str] = Counter()
    versions: Counter[str] = Counter()
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
        versions[row["rule_version"] or UNKNOWN_VERSION] += 1
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
        rule_versions=_ranked(versions),
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
        trace = parse_gate_trace(row["gate_trace"])
        if trace is None:
            # 读不出任何一条闸门：计数后跳过。既不能当「没有闸门」，
            # 也不能让整份报告中断 —— 数据坏掉的时候更需要报告能跑完。
            corrupt += 1
            continue

        # 分布用的是 ent_ratio 列 —— 与视图 2 其余统计同进同退：
        # 损坏行一律剔除，否则渲染层「已从上面的统计中剔除」就成了谎话（spec §5.2）。
        if row["total_active_minutes"] > 0:
            histogram[_ratio_bucket(row["ent_ratio"], ratio_buckets)] += 1

        # 「每一个未通过的闸门」都要计数：一轮可以同时被 cooldown 与 daily_cap 挡下，
        # 只看第一个会把 daily_cap 的触达次数少算。
        for gate in trace:
            if not gate.passed:
                blocked_any[gate.name] += 1
            if gate.name == GATE_RATIO_MIN:
                if gate.passed:
                    ratio_min_passed += 1
                else:
                    ratio_min_blocked += 1

        failing = first_failed(trace)
        if failing is not None:
            blocked_first[failing.name] += 1
            # state_min 挡下的轮次不算「该提醒但没提醒」—— 那本来就不该提醒。
            if failing.name != GATE_STATE_MIN:
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
        cooldown_blocks=_count(gates.blocked_any, GATE_COOLDOWN),
        daily_cap_blocks=_count(gates.blocked_any, GATE_DAILY_CAP),
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


#: 分层名。封闭枚举写在这里，渲染层与测试引用同一批字面量 ——
#: 各写各的拼法正是「报告里少了一层而没人发现」的土壤。
COHORT_MAIN = "main"
COHORT_RECORDING = "recording"
COHORT_CHANNEL_UNKNOWN = "channel_unknown"
COHORT_UNDELIVERED = "undelivered"
COHORT_MISSING_OUTCOME = "missing_outcome"
COHORT_NO_DATA = "no_data"

#: 主分析口径的准入条件，以「为什么不能进主分析」的形式写全 —— 判据只有一个方向，
#: 多出来的东西必然是被某一条理由挡住的。
COHORT_LAYER_NAMES: tuple[str, ...] = (
    COHORT_MAIN,
    COHORT_RECORDING,
    COHORT_CHANNEL_UNKNOWN,
    COHORT_UNDELIVERED,
    COHORT_MISSING_OUTCOME,
    COHORT_NO_DATA,
)

_COHORT_REASONS: dict[str, str] = {
    COHORT_MAIN: "真实弹出 + 已投递 + 有可用回执，计入效果分析",
    COHORT_RECORDING: "--dry-run 的排练，不是真实投递",
    COHORT_CHANNEL_UNKNOWN: "迁移前写入的行，投递通道未知",
    COHORT_UNDELIVERED: "投递未成功，用户根本没看到",
    COHORT_MISSING_OUTCOME: "回执尚未结算（未到期或进程没跑到）",
    COHORT_NO_DATA: "回执取不到数，不是「没影响」",
}


def cohort_layer(row: Any) -> str:
    """一条干预属于哪一层。**恰好一层**，判定按下面的顺序短路。

    顺序是刻意的，每一跳都有具体理由：
      1. 通道未知 → 不知道它是不是排练，谈不上效果；
      2. 排练（channel 不是 foreground_popup）→ 没人看见，谈不上效果；
      3. 投递未成功 → 同上；
      4. 没有回执行 → 还没到期，或者进程没跑到那一刻；
      5. 回执是 `no_data` → 取不到数，按「无结论」处理，不进均值；
      6. 其余才是主分析。

    先判通道再判投递，是因为「排练 + delivered」的组合是常态（排练本来就返回
    delivered）；倒过来判会让大量排练落进 `undelivered` 之外的层里，
    分母看起来对了，含义已经错了。
    """
    channel = row["channel"]
    if channel is None:
        return COHORT_CHANNEL_UNKNOWN
    if channel != "foreground_popup":
        return COHORT_RECORDING
    if row["delivery_status"] != "delivered":
        return COHORT_UNDELIVERED
    outcome = row["outcome"]
    if outcome is None:
        return COHORT_MISSING_OUTCOME
    if outcome == "no_data":
        return COHORT_NO_DATA
    return COHORT_MAIN


def build_cohort_breakdown(rows: Sequence[Any]) -> CohortBreakdown:
    """把同一批干预切成互斥的层，并给出主分析层的分母与前后娱乐。

    取数由 `Store.list_intervention_cohort` 完成，且**只按干预发生时刻过滤**。
    之前 report 用两个时间轴拼分母（干预按 `at`、回执按 `checked_at`），
    于是「区间起点前投递、区间内检查」的回执进得来、对应干预进不来，
    报告里凭空多出一层 `orphan` —— 那不是数据脏，是口径错。这里把这类行
    单独数出来（正常应为 0），以免同样的写法再长回来。

    各层计数相加必须等于干预总数。这不是巧合而是不变式：`cohort_layer` 是
    一个全定义函数，每条干预恰好落一层；哪里少算了，下面这行断言会当场炸，
    而不是让分母悄悄变小。
    """
    counts: dict[str, int] = {name: 0 for name in COHORT_LAYER_NAMES}
    outcomes: dict[str, Counter[str]] = {name: Counter() for name in COHORT_LAYER_NAMES}
    responses: dict[str, Counter[str]] = {name: Counter() for name in COHORT_LAYER_NAMES}
    days: set[str] = set()
    versions: Counter[str] = Counter()
    before: list[float] = []
    after: list[float] = []
    orphan = 0

    for row in rows:
        layer = cohort_layer(row)
        counts[layer] += 1
        responses[layer][row["user_response"] or NO_RESPONSE] += 1
        if row["outcome"] is not None:
            outcomes[layer][row["outcome"]] += 1
        if layer != COHORT_MAIN:
            continue
        days.add(_parse(row["at"]).astimezone().date().isoformat())
        versions[row["eval_rule_version"] or UNKNOWN_VERSION] += 1
        before.append(row["ent_before"])
        after.append(row["ent_after"])
        # 主分析层的行必然有回执；拿到行却没有检查时刻，说明取数又漏了关联。
        if row["outcome_checked_at"] is None:
            orphan += 1

    layers = tuple(
        CohortLayer(
            name=name,
            reason=_COHORT_REASONS[name],
            interventions=counts[name],
            user_responses=_ranked(responses[name]),
            outcomes=_ranked(outcomes[name]),
        )
        for name in COHORT_LAYER_NAMES
    )
    total = sum(layer.interventions for layer in layers)
    assert total == len(rows), f"分层计数 {total} 与干预总数 {len(rows)} 不一致"

    return CohortBreakdown(
        total=len(rows),
        layers=layers,
        main_interventions=len(before),
        main_days=tuple(sorted(days)),
        main_rule_versions=_ranked(versions),
        main_ent_before_mean=round(statistics.fmean(before), 2) if before else None,
        main_ent_after_mean=round(statistics.fmean(after), 2) if after else None,
        main_ent_before_median=round(statistics.median(before), 2) if before else None,
        main_ent_after_median=round(statistics.median(after), 2) if after else None,
        orphan_outcomes=orphan,
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


def aggregate_leak_details(
    anchors: Sequence[LeakAnchor],
    snapshots: Sequence[Any],
    *,
    taxonomy: TaxonomyConfig,
    top_n: int,
) -> tuple[LeakWindowDetail, ...]:
    """漏判二级视图的聚合：锚点 ↔ 已回查到的窗口快照，一一对位。

    回查是 IO，归入口层；这里的分类、排序、截断是纯函数（spec §4.2）。
    `data_status` 不是 ok 的窗口如实标成 unavailable —— 取不到明细
    与「没有未命中条目」是两回事，混在一起就成了本版本要消灭的二义。
    """
    details: list[LeakWindowDetail] = []
    for anchor, snapshot in zip(anchors, snapshots):
        if snapshot.data_status != "ok":
            details.append(
                LeakWindowDetail(
                    anchor.at, LeakDetailStatus.UNAVAILABLE, snapshot.data_status, ()
                )
            )
            continue
        others = sorted(
            (e for e in snapshot.entries if classify(e, taxonomy) is Category.OTHER),
            key=lambda e: e.minutes,
            reverse=True,
        )[:top_n]
        if not others:
            details.append(
                LeakWindowDetail(anchor.at, LeakDetailStatus.EMPTY, "ok", ())
            )
            continue
        details.append(
            LeakWindowDetail(
                anchor.at,
                LeakDetailStatus.AVAILABLE,
                "ok",
                tuple(LeakEntryLine(e.minutes, e.title or e.app) for e in others),
            )
        )
    return tuple(details)


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
    moments = _moments(evaluations)
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

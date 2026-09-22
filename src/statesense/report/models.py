"""report 的聚合结果。纯数据、无行为 —— 渲染层只读它。

刻意用 dataclass 而不是 dict：聚合结果要能被断言比较，
字段名也要在渲染层与测试之间保持一致。

**新增字段一律不给默认值。** 这些字段每一个都是规格点名要求出现在输出里的东西；
给默认值等于允许某个构造点漏掉它，而漏掉的表现是「报告少了一段」——
一种不会报错、只会让人读不出结论的失败。构造点只有 `queries.py` 里的几处，
显式写的成本很低。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True)
class RunEvent:
    at: datetime
    kind: str
    detail: str


@dataclass(frozen=True)
class ContinuityGap:
    start: datetime
    end: datetime
    minutes: float
    #: 落在缺口区间内的运行事件。为空意味着「进程当时不在运行」。
    events: tuple[RunEvent, ...]


@dataclass(frozen=True)
class Overview:
    first_at: datetime | None
    last_at: datetime | None
    evaluations: int
    expected_evaluations: int
    coverage: float
    gaps: tuple[ContinuityGap, ...]


@dataclass(frozen=True)
class Liveness:
    """此刻是否还在跑（spec §7.4 的后半句）。

    缺口列表回答的是「过去断过没有」，答不了「现在掉线了吗」——
    而后者才是打开报告的人真正想知道的。规格 §10 把这个键设计成
    「回看历史」与「判断当下」两用，只实现前一半等于把那个设计丢掉一半。
    """

    now: datetime
    last_at: datetime | None
    #: `now - last_at` 的分钟数。区间内没有任何评估时为 None。
    silent_minutes: float | None
    #: 判定所依据的 `report.gap_threshold_minutes`。放进结果里，
    #: 渲染层才能把「已掉线 42 分钟」与「阈值 15 分钟」一起写出来 ——
    #: 只说掉线不说阈值，读者没法判断这个结论有多硬。
    threshold_minutes: float
    #: 静默是否已超过 `threshold_minutes`。
    offline: bool
    #: 最后一条评估之后写入的运行事件 —— 用来把「有意跳过/出错」与「进程死了」分开。
    events: tuple[RunEvent, ...]


@dataclass(frozen=True)
class VerdictBreakdown:
    total: int
    states: tuple[tuple[str, int], ...]
    data_statuses: tuple[tuple[str, int], ...]
    skipped: int
    late_night: int
    #: 全屏信号的原始取值分布（含 "unknown" 一档）。
    #: 这条「自动推断」的准确率只能靠它事后审计 —— 只存布尔就审不动了。
    fullscreen_states: tuple[tuple[str, int], ...]
    #: 判定规则版本的分布（含 "unknown" 一档 = 迁移前写入的行）。
    #: 分类清单与阈值不在结果行里，跨版本比较只能靠这一档把样本分开；
    #: 少了它，「阈值调优的效果」与「规则换了」在数据上无法区分。
    rule_versions: tuple[tuple[str, int], ...]
    #: 「报了活跃却没有任何条目明细」的轮次。这些轮次上，
    #: 漏判视图的两个差额都退化为不可判定 —— 必须数出来并说出口，
    #: 否则视图 5 会把「明细缺失」静默算成「没漏判」。
    entries_unknown: int


@dataclass(frozen=True)
class GateBlock:
    at: datetime
    gate: str
    value: float | None
    threshold: float


@dataclass(frozen=True)
class GateBreakdown:
    #: 按「第一个未通过的闸门」计数。闸门按配置顺序跑，第一个失败的是主因。
    blocked_by: tuple[tuple[str, int], ...]
    #: 按「每一个未通过的闸门」计数。一轮可能同时被 cooldown 与 daily_cap 挡下，
    #: 而 cooldown/daily_cap 的**触达次数**要的正是这个口径 —— 只看第一个会让
    #: daily_cap 少算，把一个恰好触及上限的日子读成"没到上限"。
    blocked_any: tuple[tuple[str, int], ...]
    ratio_histogram: tuple[tuple[str, int], ...]
    #: `ratio_min` 达标与被挡的对照计数。缺了"达标"这一侧就没有对照，
    #: 判据 4（要不要调 ratio_min）无从读起。
    ratio_min_passed: int
    ratio_min_blocked: int
    #: state_min 通过、却被后续闸门挡下的轮次 —— 这才是「该提醒但没提醒」。
    state_min_passed_then_blocked: tuple[GateBlock, ...]
    #: `gate_trace` 读不出任何一条闸门的轮次。计数而不是静默跳过：
    #: 损坏的行同时会从 blocked_by 里消失，不说出口就会被读成"闸门没挡过"。
    corrupt_rows: int


@dataclass(frozen=True)
class InterventionBreakdown:
    total: int
    per_day: tuple[tuple[str, int], ...]
    actions: tuple[tuple[str, int], ...]
    states: tuple[tuple[str, int], ...]
    delivery_statuses: tuple[tuple[str, int], ...]
    #: 投递通道分布。`recording` = `--dry-run` 的排练，`foreground_popup` = 真弹窗。
    #: 「到底有没有真的弹过窗」只能靠这一行回答。
    channels: tuple[tuple[str, int], ...]
    user_responses: tuple[tuple[str, int], ...]
    cooldown_blocks: int
    daily_cap_blocks: int


@dataclass(frozen=True)
class OutcomeBreakdown:
    outcomes: tuple[tuple[str, int], ...]
    #: user_response 分层：键是 accepted / declined / null，
    #: 值是该层内的 outcome 分布。V0 规格 §16 风险 6 要求的缓解措施。
    by_response: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    no_data: int
    #: 均值会被少数极端值拉动，中位数不会 —— 两者都给才看得出分布形状。
    ent_before_mean: float | None
    ent_after_mean: float | None
    ent_before_median: float | None
    ent_after_median: float | None


@dataclass(frozen=True)
class CohortLayer:
    """一批干预里的一层。各层互斥，相加等于干预总数。

    分层的用途是**把不能进主分析的记录挑出来并说清原因**，而不是把它们删掉：
    「排练 2 次、通道未知 3 次」这种话本身就是要看的运行事实。
    """

    name: str
    reason: str
    interventions: int
    user_responses: tuple[tuple[str, int], ...]
    outcomes: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class CohortBreakdown:
    """真实干预的同一批记录（阶段 2.1）。

    与 `OutcomeBreakdown` 的区别：那个按用户回应分层，回答「按了按钮的结果如何」；
    这个按**能否作为效果证据**分层，回答「分母里到底有几次是真的弹了窗、真的收到了
    可用的回执」。前者会把排练与真实混在一起 —— 那正是这一版要修的口径问题。
    """

    total: int
    layers: tuple[CohortLayer, ...]
    #: 主分析层：channel=foreground_popup 且 delivery_status=delivered 且有可用回执。
    main_interventions: int
    #: 主分析层跨越的自然日数。五分钟重叠窗口里同一次消费可以弹出多次，
    #: 只看次数会把「一天里被提醒了很多次」读成「很多天的证据」。
    main_days: tuple[str, ...]
    #: 主分析层的触发规则版本分布。跨版本时主分析层内部也不能合并比较。
    main_rule_versions: tuple[tuple[str, int], ...]
    main_ent_before_mean: float | None
    main_ent_after_mean: float | None
    main_ent_before_median: float | None
    main_ent_after_median: float | None
    #: 区间起点之前投递、区间内才检查的回执。取数改成单一时间轴后这里应为 0；
    #: 一旦非 0，说明又出现了两个时间轴拼分母的写法。
    orphan_outcomes: int


@dataclass(frozen=True)
class ReceiptAuditStratum:
    """按**触发那一刻的全屏取值**分层的一档回执。

    这是阶段 2.2 的审计口径：全屏提权只作用于未归类条目，所以「触发时正在全屏
    游戏」的那一类回执，其前侧娱乐分钟最容易受历史全屏取值影响 ——
    修好之前它们会被按当下的取值重算，游戏退出时 ent_before 偏低甚至清零。
    """

    name: str
    description: str
    receipts: int
    no_data: int
    outcomes: tuple[tuple[str, int], ...]
    #: 看起来「变好」的回执（ent_after <= ent_before）。数字本身不作因果解读：
    #: 游戏退出、用户离开电脑都会让它变大。
    after_not_worse: int
    ent_before_mean: float | None
    ent_after_mean: float | None


@dataclass(frozen=True)
class ReceiptAudit:
    """回执口径审计（阶段 2.2）。

    审计的是**取数口径**，不是效果：`ent_before` 与 `ent_after` 由同一段代码、
    同一套提权规则算出，通道与排练与否都不改变它。所以这里刻意不再按通道分层 ——
    要做效果结论请用视图 6 的主分析层。
    """

    total_receipts: int
    strata: tuple[ReceiptAuditStratum, ...]
    #: 触发时在全屏游戏中的回执总数与其中 no_data 的条数。
    affected_receipts: int
    affected_no_data: int


@dataclass(frozen=True)
class ConsumptionEvent:
    """连续可干预轮次合并成的消费事件。**这是系统视角，不是人工标注。**

    库里只有评估结果、没有标注，所以这里的「事件」= 状态处于可干预档的连续轮次。
    它可以回答「这段时间是几次消费」，但不能替代「这几次是不是真的被动消费」。
    """

    start: datetime
    end: datetime
    ticks: int
    #: 事件内出现过的最高状态（阶梯越高，事件越"深"）。
    peak_state: str

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass(frozen=True)
class ActionTimingLayer:
    """动作 × 时机的一层。每一层都同时给出分母与样本量的旁证。

    低样本层**只列数据、不给排名**：轮次不是独立观察（五分钟重叠窗口里同一次
    消费可以触发多次），层的排序因此不能当因果结论用。
    """

    action_id: str
    state: str
    late_night: bool
    ent_bucket: str
    hour_bucket: str
    #: 该层的真实投递数（真的弹了窗：foreground_popup + delivered）。
    #: 注意它**不等于**视图 6 的主分析口径：主分析还要求回执可用，
    #: 因为均值不能被 no_data 污染；而「提醒了多少次」应当把它们算进去。
    deliveries: int
    #: 回执可用（非 no_data）与取不到数的条数，以及还没结算的条数。
    valid_receipts: int
    no_data: int
    missing_receipts: int
    #: 该层跨越的自然日数与落在几个消费事件里 —— 次数不等于证据量。
    days: int
    events: int
    outcomes: tuple[tuple[str, int], ...]
    ent_before_mean: float | None
    ent_after_mean: float | None


@dataclass(frozen=True)
class ActionTimingBreakdown:
    total_deliveries: int
    total_events: int
    total_days: int
    layers: tuple[ActionTimingLayer, ...]
    #: 事件被合并前的轮次数与事件数 —— 「按事件看」与「按轮次看」的差别写在这里。
    raw_ticks: int


@dataclass(frozen=True)
class LeakAnchor:
    at: datetime
    total_active_minutes: float
    ent_minutes: float
    gray_minutes: float
    work_minutes: float
    #: entries_minutes - (ent+gray+work)：未命中任何规则的分钟数。
    unclassified_minutes: float
    unclassified_ratio: float
    #: total_active_minutes - entries_minutes：Screenpipe 报了活跃却没给明细。
    #: 这一项不是漏判，单独给出以免两者混淆。
    missing_detail_minutes: float


@dataclass(frozen=True)
class LeakEntryLine:
    minutes: float
    label: str


class LeakDetailStatus(StrEnum):
    #: 封闭三态。empty（没查到未命中条目）与 unavailable（根本没查到）
    #: 是两回事 —— 混在一起就成了本版本要消灭的那类二义。
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    EMPTY = "empty"


@dataclass(frozen=True)
class LeakWindowDetail:
    """一个锚点那一轮回查 Screenpipe 的结构化结果 —— 文本归渲染层，这里只放意思。"""

    at: datetime
    status: LeakDetailStatus
    data_status: str
    entries: tuple[LeakEntryLine, ...]


@dataclass(frozen=True)
class TraceRow:
    at: datetime
    state: str
    ent_ratio: float
    ent_minutes: float
    total_active_minutes: float
    skipped: bool
    decision: str
    gates: tuple[tuple[str, bool, float | None, float], ...]


@dataclass(frozen=True)
class ReportData:
    """把各视图的结果捆在一起交给渲染层。渲染层只读它，不再取数。"""

    overview: Overview
    liveness: Liveness
    verdicts: VerdictBreakdown
    gates: GateBreakdown
    interventions: InterventionBreakdown
    outcomes: OutcomeBreakdown
    #: 效果分析的分母（阶段 2.1）：同一批干预、按能否作为证据分层。
    cohort: CohortBreakdown
    #: 回执口径审计（阶段 2.2）：哪些回执的前侧证据容易受全屏切换影响。
    receipt_audit: ReceiptAudit
    #: 动作 × 时机（阶段 2.3）。分组用的行与视图 6 同源，只是换了个切法。
    timing: ActionTimingBreakdown
    leaks: tuple[LeakAnchor, ...]
    trace: tuple[TraceRow, ...]
    #: 二级漏判视图的回查结果（窗口标题只在这里出现，绝不落库）。可能为空。
    leak_details: tuple[LeakWindowDetail, ...]

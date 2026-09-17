"""report 的聚合结果。纯数据、无行为 —— 渲染层只读它。

刻意用 dataclass 而不是 dict：聚合结果要能被断言比较，
字段名也要在渲染层与测试之间保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
class VerdictBreakdown:
    total: int
    states: tuple[tuple[str, int], ...]
    data_statuses: tuple[tuple[str, int], ...]
    skipped: int
    late_night: int
    #: 全屏信号的原始取值分布（含 "unknown" 一档）。
    #: 这条「自动推断」的准确率只能靠它事后审计 —— 只存布尔就审不动了。
    fullscreen_states: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class GateBlock:
    at: datetime
    gate: str
    value: float | None
    threshold: float


@dataclass(frozen=True)
class GateBreakdown:
    blocked_by: tuple[tuple[str, int], ...]
    ratio_histogram: tuple[tuple[str, int], ...]
    #: state_min 通过、却被后续闸门挡下的轮次 —— 这才是「该提醒但没提醒」。
    state_min_passed_then_blocked: tuple[GateBlock, ...]


@dataclass(frozen=True)
class InterventionBreakdown:
    total: int
    per_day: tuple[tuple[str, int], ...]
    actions: tuple[tuple[str, int], ...]
    states: tuple[tuple[str, int], ...]
    delivery_statuses: tuple[tuple[str, int], ...]
    user_responses: tuple[tuple[str, int], ...]
    cooldown_blocks: int
    daily_cap_blocks: int


@dataclass(frozen=True)
class OutcomeBreakdown:
    outcomes: tuple[tuple[str, int], ...]
    #: user_response 分层：键是 accepted / declined / none，
    #: 值是该层内的 outcome 分布。V0 规格 §16 风险 6 要求的缓解措施。
    by_response: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    no_data: int
    ent_before_mean: float | None
    ent_after_mean: float | None


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
    verdicts: VerdictBreakdown
    gates: GateBreakdown
    interventions: InterventionBreakdown
    outcomes: OutcomeBreakdown
    leaks: tuple[LeakAnchor, ...] = ()
    trace: tuple[TraceRow, ...] = ()
    #: 二级漏判视图的文本行（窗口标题只在这里出现，绝不落库）。可能为空。
    leak_details: tuple[str, ...] = ()

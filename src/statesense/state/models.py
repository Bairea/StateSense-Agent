"""状态判定的产物。state 与 late_night 是两个正交维度。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    NORMAL = "NORMAL"
    WATCH = "WATCH"
    PASSIVE_CONSUMPTION = "PASSIVE_CONSUMPTION"
    HIGH_RISK_PASSIVE_CONSUMPTION = "HIGH_RISK_PASSIVE_CONSUMPTION"


#: 状态档从浅到深。**这是全仓唯一的状态深浅顺序。**
#:
#: `classify` 里的 if 链隐含同一个顺序，但那是「怎么算出来的」，不是「谁比谁深」；
#: 影子对比要说清「模型判得比规则更重还是更轻」，就必须有一个能排序的东西。
#: 少了它，两个 `State` 只能比相等或不等 —— 那是「一不一样」，不是「谁更晚」。
#:
#: 顺序与 `config.ThresholdConfig` 的阶梯（watch <= passive <= high_risk）同源：
#: `load_config` 已经拒绝倒置的阈值，所以这里写死不必再校验。
SEVERITY_ORDER: tuple[State, ...] = (
    State.NORMAL,
    State.WATCH,
    State.PASSIVE_CONSUMPTION,
    State.HIGH_RISK_PASSIVE_CONSUMPTION,
)

_RANK: dict[State, int] = {state: index for index, state in enumerate(SEVERITY_ORDER)}


def severity_rank(state: State) -> int:
    """状态档的深浅名次，0 最浅。

    越深 = 越该被提醒 = 可提醒时间越早。取值只保证序关系，不保证等差。
    """
    return _RANK[state]


@dataclass(frozen=True)
class StateVerdict:
    state: State
    late_night: bool
    total_active_minutes: float
    ent_minutes: float
    gray_minutes: float
    work_minutes: float
    ent_ratio: float
    window_minutes: int
    data_status: str
    skipped: bool
    skip_reason: str | None
    #: 条目分钟数之和。与 total_active_minutes 的差额是「明细缺失」，
    #: 与 (ent+gray+work) 的差额是「未命中任何规则」。刻意不给默认值 ——
    #: 0.0 本身就是可疑信号，静默默认会让漏判检测失效。
    entries_minutes: float
    #: SHQueryUserNotificationState 的原始返回值；None = 无法判定。
    #: 刻意不给默认值：`None` 是「不知道」，与「不是全屏」是两件事，
    #: 而忘记传它会让整条全屏信号静默失效。
    fullscreen_state: int | None

"""剧本模型：一段时间线，由若干段活动拼成。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Segment:
    #: 距剧本原点的偏移与持续时长（分钟）。
    offset_minutes: float
    duration_minutes: float
    app: str
    title: str
    url: str = ""


@dataclass(frozen=True)
class Scenario:
    name: str
    #: 剧本覆盖的总时长（分钟）。
    minutes: float
    segments: tuple[Segment, ...] = field(default_factory=tuple)
    #: `data_status` 是**剧本级**属性，不是段级：它描述「这一轮取数据的结果」，
    #: 属于快照整体。挂在段上会产生语义不明 —— 同一快照里两段各自声称不同的
    #: data_status，而快照只有一个。
    data_status: str = "ok"
    #: 整段剧本期间 `SHQueryUserNotificationState` 的取值。
    #: 它必须由剧本给定，否则回放会去读真实的全屏状态，结果随「跑回放的这一刻
    #: 我是不是正开着游戏」变化 —— 一个不可重复的验证工具比没有更糟。
    #: 默认 `None` = 无法判定，不触发任何提权。
    fullscreen_state: int | None = None

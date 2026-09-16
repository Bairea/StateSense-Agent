"""状态判定的产物。state 与 late_night 是两个正交维度。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class State(StrEnum):
    NORMAL = "NORMAL"
    WATCH = "WATCH"
    PASSIVE_CONSUMPTION = "PASSIVE_CONSUMPTION"
    HIGH_RISK_PASSIVE_CONSUMPTION = "HIGH_RISK_PASSIVE_CONSUMPTION"


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

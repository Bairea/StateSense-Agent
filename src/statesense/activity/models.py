"""活动快照的数据契约。只承载行为元数据，绝不承载屏幕文本。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

TRUSTWORTHY_STATUSES = frozenset({"ok"})

#: 服务端可能明确告知的「明确不可信」状态。
KNOWN_UNTRUSTWORTHY_STATUSES = frozenset(
    {"empty_but_recording", "no_capture_in_range", "not_recording"}
)

#: 本地判定出的「读不到」。
UNREACHABLE = "unreachable"

#: data_status 的封闭枚举。读到枚举外的值一律按 unreachable 处理 ——
#: 不认识的字段意味着响应结构与预期不符，此时任何「没有活动」的结论都不成立。
KNOWN_STATUSES = TRUSTWORTHY_STATUSES | KNOWN_UNTRUSTWORTHY_STATUSES | {UNREACHABLE}


@dataclass(frozen=True)
class Entry:
    app: str
    title: str
    url: str
    minutes: float


@dataclass(frozen=True)
class ActivitySnapshot:
    window_start: datetime
    window_end: datetime
    window_minutes: int
    total_active_minutes: float
    entries: tuple[Entry, ...]
    data_status: str
    captured_at: datetime

    @property
    def is_trustworthy(self) -> bool:
        """data_status 不是 ok 时，任何「没有活动」的结论都不成立。"""
        return self.data_status in TRUSTWORTHY_STATUSES

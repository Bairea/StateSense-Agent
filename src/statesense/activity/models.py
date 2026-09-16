"""活动快照的数据契约。只承载行为元数据，绝不承载屏幕文本。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

TRUSTWORTHY_STATUSES = frozenset({"ok"})


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

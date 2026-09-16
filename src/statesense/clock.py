"""时间抽象。判定逻辑一律通过它取时间，测试中可替换为 FrozenClock。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区的 UTC 时间。"""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    """测试用：时间完全由测试控制。"""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FrozenClock 需要带时区的时间")
        self._instant = instant

    def now(self) -> datetime:
        return self._instant

    def advance(self, **delta: float) -> None:
        self._instant += timedelta(**delta)

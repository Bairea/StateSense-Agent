"""投递契约。失败必须返回结构化错误，绝不静默吞掉。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from statesense.clock import Clock, SystemClock

#: 用户在弹窗上点了「是」
RESPONSE_ACCEPTED = "accepted"
#: 用户在弹窗上点了「否」
RESPONSE_DECLINED = "declined"


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    channel: str
    error: str | None
    delivered_at: datetime | None
    user_response: str | None = None

    @classmethod
    def delivered(
        cls,
        channel: str,
        at: datetime,
        user_response: str | None = None,
    ) -> "DeliveryResult":
        return cls(
            status="delivered",
            channel=channel,
            error=None,
            delivered_at=at,
            user_response=user_response,
        )

    @classmethod
    def failed(cls, channel: str, error: str) -> "DeliveryResult":
        return cls(status="failed", channel=channel, error=error, delivered_at=None)

    @property
    def stored_status(self) -> str:
        """入库用字符串。失败原因必须保留，否则回执与重试无从判断。"""
        return "delivered" if self.status == "delivered" else f"failed:{self.error}"


class Notifier(Protocol):
    channel: str

    def notify(self, title: str, body: str) -> DeliveryResult: ...


class RecordingNotifier:
    """测试与 --dry-run 用：不发真通知，只记下来。"""

    channel = "recording"

    def __init__(self, response: str | None = None, clock: Clock | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self._response = response
        self._clock = clock or SystemClock()

    def notify(self, title: str, body: str) -> DeliveryResult:
        self.sent.append((title, body))
        return DeliveryResult.delivered(
            self.channel, self._clock.now(), user_response=self._response
        )

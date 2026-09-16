"""投递契约。失败必须返回结构化错误，绝不静默吞掉。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    channel: str
    error: str | None
    delivered_at: datetime | None

    @classmethod
    def delivered(cls, channel: str, at: datetime) -> "DeliveryResult":
        return cls(status="delivered", channel=channel, error=None, delivered_at=at)

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

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def notify(self, title: str, body: str) -> DeliveryResult:
        self.sent.append((title, body))
        return DeliveryResult.delivered(self.channel, datetime.now(timezone.utc))

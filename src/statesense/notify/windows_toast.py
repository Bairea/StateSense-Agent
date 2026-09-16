"""Windows Toast 投递。

Windows 10（Build 19045）的 Toast 必须绑定已注册的 AUMID，否则静默失败；
winotify 会自动在开始菜单创建带 AppUserModelID 的快捷方式。

已知局限：Windows 在「全屏应用」下默认抑制通知，而本项目的核心场景恰恰是
全屏刷视频。该场景需在真机实测；备用方案见 spec §8.3。
"""

from __future__ import annotations

from typing import Any

from statesense.clock import Clock
from statesense.config import NotifyConfig

from .base import DeliveryResult


def _notification_class() -> Any:
    """延迟导入：便于测试替换，也让非 Windows 平台能导入本模块。"""
    from winotify import Notification

    return Notification


class WindowsToastNotifier:
    channel = "windows_toast"

    def __init__(self, config: NotifyConfig, clock: Clock) -> None:
        self._config = config
        self._clock = clock

    def notify(self, title: str, body: str) -> DeliveryResult:
        try:
            factory = _notification_class()
            toast = factory(
                app_id=self._config.app_id,
                title=title,
                msg=body,
                duration=self._config.toast_duration,
            )
            toast.show()
        except Exception as exc:  # noqa: BLE001 - 投递失败不能拖垮调度器
            return DeliveryResult.failed(self.channel, f"{exc.__class__.__name__}: {exc}")
        return DeliveryResult.delivered(self.channel, self._clock.now())

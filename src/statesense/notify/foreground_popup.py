"""前台弹窗投递通道。

相对 Windows Toast 的三个优势（均已在本机实测）：
  1. 不受全局通知开关影响（本机 ToastEnabled=0，Toast 全部被丢弃）
  2. 不受「全屏应用抑制通知」影响（加显式抢前台后，全屏下可见）
  3. 不需要注册 AUMID，且零第三方依赖

代价：模态窗口会抢焦点并需要一次点击。对「把你从被动消费里拽出来」这个目的，
这恰恰是想要的；同时这一次点击顺带给出了显式回执，弥补了纯行为推断的歧义
（「离开电脑」也会被算成 disengaged，会高估干预效果）。
"""

from __future__ import annotations

import logging
import threading

from statesense.clock import Clock
from statesense.config import NotifyConfig

from .base import RESPONSE_ACCEPTED, RESPONSE_DECLINED, DeliveryResult
from .win32_popup import IDNO, IDYES, Win32Popup

log = logging.getLogger(__name__)

JOIN_GRACE_SECONDS = 2.0


class ForegroundPopupNotifier:
    channel = "foreground_popup"

    def __init__(self, config: NotifyConfig, clock: Clock, popup: Win32Popup | None = None) -> None:
        self._config = config
        self._clock = clock
        self._popup = popup if popup is not None else Win32Popup()

    def notify(self, title: str, body: str) -> DeliveryResult:
        # 一次只留一个窗口：先清掉同标题的残留（用户没点时它会一直挂着）。
        if not self._popup.close(title):
            log.debug("没有需要清理的残留弹窗：%s", title)

        answer: dict[str, int] = {}
        failure: dict[str, BaseException] = {}

        def worker() -> None:
            try:
                answer["id"] = self._popup.show(title, body)
            except BaseException as exc:  # noqa: BLE001 - 投递失败不能拖垮调度器
                failure["exc"] = exc

        thread = threading.Thread(target=worker, name="statesense-popup", daemon=True)
        thread.start()

        self._popup.beep()
        self._popup.force_front(title, self._config.foreground_timeout_seconds)
        thread.join(timeout=self._config.answer_timeout_seconds)

        timed_out = thread.is_alive()
        if timed_out:
            # 用户不在或没理会：关掉窗口，避免它永远挡住后续干预。
            if not self._popup.close(title):
                log.warning("弹窗未能自动关闭，可能仍停留在屏幕上：%s", title)
            thread.join(timeout=JOIN_GRACE_SECONDS)

        if "exc" in failure:
            exc = failure["exc"]
            return DeliveryResult.failed(self.channel, f"{type(exc).__name__}: {exc}")

        # 超时被程序关掉时，对话框返回的按钮 id 不是用户的意思 —— 绝不能当成回执。
        response = None if timed_out else self._map_response(answer.get("id"))
        return DeliveryResult.delivered(self.channel, self._clock.now(), user_response=response)

    @staticmethod
    def _map_response(button_id: int | None) -> str | None:
        if button_id == IDYES:
            return RESPONSE_ACCEPTED
        if button_id == IDNO:
            return RESPONSE_DECLINED
        # 超时被自动关闭，或返回了预期外的值：不猜用户的意思。
        return None

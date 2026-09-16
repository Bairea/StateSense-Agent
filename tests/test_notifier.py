import threading
import time
from datetime import datetime, timezone

from statesense.clock import FrozenClock
from statesense.config import NotifyConfig
from statesense.notify.base import (
    RESPONSE_ACCEPTED,
    RESPONSE_DECLINED,
    DeliveryResult,
    RecordingNotifier,
)
from statesense.notify.foreground_popup import ForegroundPopupNotifier
from statesense.notify.win32_popup import IDNO, IDYES

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


class FakePopup:
    """记录所有调用。show() 的行为可配置：立即返回按钮 id，或一直阻塞到 close()。"""

    def __init__(self, button_id: int | None = IDYES, block_forever: bool = False) -> None:
        self.button_id = button_id
        self.block_forever = block_forever
        self.shown: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.forced: list[tuple[str, float]] = []
        self.beeps = 0
        # 每次 show 用新的事件，避免开头的「清理残留」把后续 show 也放行。
        self._blocking: threading.Event | None = None

    def show(self, title: str, body: str) -> int:
        self.shown.append((title, body))
        if self.block_forever:
            self._blocking = threading.Event()
            self._blocking.wait(timeout=5.0)
            return IDNO
        assert self.button_id is not None
        return self.button_id

    def force_front(self, title: str, timeout: float) -> bool:
        self.forced.append((title, timeout))
        return True

    def close(self, title: str) -> bool:
        self.closed.append(title)
        if self._blocking is not None:
            self._blocking.set()
        return True

    def beep(self) -> None:
        self.beeps += 1


def _config(**kw) -> NotifyConfig:
    defaults = {"answer_timeout_seconds": 5.0, "foreground_timeout_seconds": 2.0}
    defaults.update(kw)
    return NotifyConfig(**defaults)


# ── 契约 ────────────────────────────────────────────────────

def test_delivery_result_delivered_without_response():
    ok = DeliveryResult.delivered("foreground_popup", T0)
    assert ok.status == "delivered"
    assert ok.error is None
    assert ok.user_response is None
    assert ok.stored_status == "delivered"


def test_delivery_result_carries_user_response():
    ok = DeliveryResult.delivered("foreground_popup", T0, user_response=RESPONSE_ACCEPTED)
    assert ok.user_response == "accepted"


def test_delivery_result_failed_has_no_response():
    bad = DeliveryResult.failed("foreground_popup", "boom")
    assert bad.status == "failed"
    assert bad.delivered_at is None
    assert bad.user_response is None
    assert bad.stored_status == "failed:boom"


def test_recording_notifier_without_response():
    n = RecordingNotifier()
    assert n.notify("标题", "正文").user_response is None
    assert n.sent == [("标题", "正文")]


def test_recording_notifier_with_canned_response():
    n = RecordingNotifier(response=RESPONSE_DECLINED)
    assert n.notify("t", "b").user_response == "declined"


# ── 前台弹窗 ────────────────────────────────────────────────

def test_yes_button_maps_to_accepted():
    popup = FakePopup(button_id=IDYES)
    result = ForegroundPopupNotifier(_config(), FrozenClock(T0), popup).notify("标题", "正文")
    assert result.status == "delivered"
    assert result.user_response == RESPONSE_ACCEPTED
    assert result.delivered_at == T0


def test_no_button_maps_to_declined():
    popup = FakePopup(button_id=IDNO)
    result = ForegroundPopupNotifier(_config(), FrozenClock(T0), popup).notify("标题", "正文")
    assert result.user_response == RESPONSE_DECLINED


def test_unknown_button_id_is_not_guessed():
    popup = FakePopup(button_id=99)
    result = ForegroundPopupNotifier(_config(), FrozenClock(T0), popup).notify("标题", "正文")
    assert result.status == "delivered"
    assert result.user_response is None


def test_stale_dialog_is_closed_before_showing():
    popup = FakePopup()
    ForegroundPopupNotifier(_config(), FrozenClock(T0), popup).notify("标题", "正文")
    assert popup.closed == ["标题"]


def test_beep_and_force_front_are_used():
    popup = FakePopup()
    ForegroundPopupNotifier(_config(), FrozenClock(T0), popup).notify("标题", "正文")
    assert popup.beeps == 1
    assert popup.forced == [("标题", 2.0)]


def test_show_receives_title_and_body():
    popup = FakePopup()
    ForegroundPopupNotifier(_config(), FrozenClock(T0), popup).notify("标题", "正文")
    assert popup.shown == [("标题", "正文")]


def test_unanswered_dialog_is_closed_and_response_is_none():
    """用户不在时不能永远挂着窗口挡住后续干预。"""
    popup = FakePopup(block_forever=True)
    notifier = ForegroundPopupNotifier(
        _config(answer_timeout_seconds=0.2), FrozenClock(T0), popup
    )
    started = time.monotonic()
    result = notifier.notify("标题", "正文")
    elapsed = time.monotonic() - started
    assert result.status == "delivered"
    assert result.user_response is None
    assert elapsed < 3.0, "超时后必须及时返回，不能挂在 join 上"
    # 一次是清残留，一次是超时后关闭
    assert popup.closed == ["标题", "标题"]


def test_show_failure_becomes_structured_error():
    class Exploding(FakePopup):
        def show(self, title: str, body: str) -> int:
            raise OSError("desktop unavailable")

    result = ForegroundPopupNotifier(_config(), FrozenClock(T0), Exploding()).notify("标题", "正文")
    assert result.status == "failed"
    assert "desktop unavailable" in (result.error or "")
    assert "failed:" in result.stored_status

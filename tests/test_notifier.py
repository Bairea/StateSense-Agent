from datetime import datetime, timezone

from statesense.clock import FrozenClock
from statesense.config import NotifyConfig
from statesense.notify.base import DeliveryResult, RecordingNotifier
from statesense.notify.windows_toast import WindowsToastNotifier

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


def test_delivery_result_helpers():
    ok = DeliveryResult.delivered("windows_toast", T0)
    assert ok.status == "delivered"
    assert ok.error is None
    assert ok.stored_status == "delivered"
    bad = DeliveryResult.failed("windows_toast", "toast unavailable")
    assert bad.status == "failed"
    assert bad.error == "toast unavailable"
    assert bad.delivered_at is None
    assert bad.stored_status == "failed:toast unavailable"


def test_recording_notifier_captures_messages():
    n = RecordingNotifier()
    result = n.notify("标题", "正文")
    assert result.status == "delivered"
    assert n.sent == [("标题", "正文")]


def test_windows_notifier_reports_failure_instead_of_raising(monkeypatch):
    """投递失败必须变成结构化结果，绝不能让调度器崩掉。"""

    class Boom:
        def __init__(self, **kwargs):
            raise RuntimeError("no AppUserModelID")

    monkeypatch.setattr("statesense.notify.windows_toast._notification_class", lambda: Boom)
    result = WindowsToastNotifier(NotifyConfig(), FrozenClock(T0)).notify("标题", "正文")
    assert result.status == "failed"
    assert "no AppUserModelID" in (result.error or "")
    assert result.delivered_at is None


def test_windows_notifier_reports_success(monkeypatch):
    sent: list = []

    class Fake:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def show(self):
            sent.append(self.kwargs)

    monkeypatch.setattr("statesense.notify.windows_toast._notification_class", lambda: Fake)
    result = WindowsToastNotifier(
        NotifyConfig(app_id="StateSense.Agent"), FrozenClock(T0)
    ).notify("标题", "正文")
    assert result.status == "delivered"
    assert result.delivered_at == T0
    assert sent[0]["title"] == "标题"
    assert sent[0]["msg"] == "正文"
    assert sent[0]["app_id"] == "StateSense.Agent"
    assert sent[0]["duration"] == "short"


def test_windows_notifier_passes_long_duration(monkeypatch):
    sent: list = []

    class Fake:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def show(self):
            sent.append(self.kwargs)

    monkeypatch.setattr("statesense.notify.windows_toast._notification_class", lambda: Fake)
    WindowsToastNotifier(NotifyConfig(toast_duration="long"), FrozenClock(T0)).notify("t", "b")
    assert sent[0]["duration"] == "long"


def test_show_failure_is_reported(monkeypatch):
    class FailsOnShow:
        def __init__(self, **kwargs):
            pass

        def show(self):
            raise OSError("shell not available")

    monkeypatch.setattr("statesense.notify.windows_toast._notification_class", lambda: FailsOnShow)
    result = WindowsToastNotifier(NotifyConfig(), FrozenClock(T0)).notify("t", "b")
    assert result.status == "failed"
    assert "shell not available" in (result.error or "")

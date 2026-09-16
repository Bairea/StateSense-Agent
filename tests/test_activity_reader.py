import json
from datetime import datetime, timedelta, timezone

import pytest

from statesense.activity.reader import ActivityReader

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)

OK_BODY = {
    "total_active_minutes": 47.5,
    "data_status": "ok",
    "apps": [{"name": "chrome.exe", "minutes": 40.0}],
    "windows": [
        {
            "app_name": "chrome.exe",
            "window_name": "【某视频】_哔哩哔哩_bilibili",
            "browser_url": "https://www.bilibili.com/video/BV1xx",
            "minutes": 40.0,
        },
        {
            "app_name": "Code.exe",
            "window_name": "engine.py - Visual Studio Code",
            "browser_url": "",
            "minutes": 7.5,
        },
    ],
    "key_texts": [{"text": "本行绝不应该被读进系统"}],
    "snippets": [{"text": "同样不应该"}],
}


def _reader(body: dict, status: int = 200, captured: list | None = None) -> ActivityReader:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def fake_get(url, headers, timeout):
        if captured is not None:
            captured.append((url, headers, timeout))
        return status, payload

    return ActivityReader("http://localhost:3030", "sp-test", 10.0, fake_get)


def test_parses_windows_into_entries():
    snap = _reader(OK_BODY).read(T0 - timedelta(minutes=60), T0, 60, T0)
    assert snap.total_active_minutes == 47.5
    assert snap.data_status == "ok"
    assert snap.is_trustworthy is True
    assert len(snap.entries) == 2
    bili = snap.entries[0]
    assert bili.app == "chrome.exe"
    assert "哔哩哔哩" in bili.title
    assert bili.url == "https://www.bilibili.com/video/BV1xx"
    assert bili.minutes == 40.0


def test_missing_windows_yields_no_entries_instead_of_silently_downgrading():
    """曾经会回退到 apps —— 但 apps 没有 title/url，分类只能靠进程名，属于静默降质。

    现在按契约只消费 windows；明细缺失时留空并告警。
    """
    body = {k: v for k, v in OK_BODY.items() if k != "windows"}
    snap = _reader(body).read(T0 - timedelta(minutes=60), T0, 60, T0)
    assert snap.entries == ()
    assert snap.total_active_minutes == 47.5, "总时长仍取服务端字段，不受明细缺失影响"
    assert snap.data_status == "ok"


def test_total_is_never_estimated_from_entries():
    """spec §15.1 明令禁止拿明细条数/条目的分钟数去估算总时长。"""
    body = dict(OK_BODY, total_active_minutes=0)
    snap = _reader(body).read(T0 - timedelta(minutes=60), T0, 60, T0)
    assert snap.total_active_minutes == 0.0
    assert len(snap.entries) == 2, "明细照常解析，但不许拿它反推总时长"


def test_unrecognized_data_status_is_treated_as_unreachable():
    """data_status 是封闭枚举；读到枚举外的值说明响应结构与预期不符。"""
    snap = _reader(dict(OK_BODY, data_status="something_new")).read(
        T0 - timedelta(minutes=60), T0, 60, T0
    )
    assert snap.data_status == "unreachable"
    assert snap.is_trustworthy is False


def test_missing_data_status_is_treated_as_unreachable():
    body = {k: v for k, v in OK_BODY.items() if k != "data_status"}
    snap = _reader(body).read(T0 - timedelta(minutes=60), T0, 60, T0)
    assert snap.data_status == "unreachable"


def test_all_known_statuses_pass_through_unchanged():
    from statesense.activity.models import KNOWN_STATUSES

    for status in sorted(KNOWN_STATUSES):
        snap = _reader(dict(OK_BODY, data_status=status)).read(
            T0 - timedelta(minutes=60), T0, 60, T0
        )
        assert snap.data_status == status


def test_request_carries_auth_headers_and_disables_text_fields():
    seen: list = []
    _reader(OK_BODY, captured=seen).read(T0 - timedelta(minutes=60), T0, 60, T0)
    url, headers, _ = seen[0]
    assert headers["Authorization"] == "Bearer sp-test"
    assert headers["X-Screenpipe-Client"] == "api"
    assert headers["X-Screenpipe-Agent"] == "statesense"
    assert "include_key_texts=false" in url
    assert "include_snippets=false" in url
    assert "include_memories=false" in url
    assert "include_guidance=false" in url
    assert "/activity-summary" in url


def test_ocr_text_never_reaches_the_snapshot():
    snap = _reader(OK_BODY).read(T0 - timedelta(minutes=60), T0, 60, T0)
    blob = repr(snap)
    assert "本行绝不应该被读进系统" not in blob
    assert "同样不应该" not in blob


def test_non_ok_data_status_is_preserved_not_swallowed():
    snap = _reader(dict(OK_BODY, data_status="no_capture_in_range")).read(
        T0 - timedelta(minutes=60), T0, 60, T0
    )
    assert snap.data_status == "no_capture_in_range"
    assert snap.is_trustworthy is False


def test_http_error_becomes_unreachable_snapshot():
    snap = _reader({}, status=403).read(T0 - timedelta(minutes=60), T0, 60, T0)
    assert snap.data_status == "unreachable"
    assert snap.entries == ()
    assert snap.total_active_minutes == 0.0


def test_warning_is_throttled_until_five_consecutive_failures(caplog):
    """spec §11：连续 5 次失败才写一条告警日志。每轮都写会变成日志风暴。"""
    import logging

    reader = _reader({}, status=403)
    with caplog.at_level(logging.WARNING, logger="statesense.activity.reader"):
        for _ in range(4):
            reader.read(T0 - timedelta(minutes=60), T0, 60, T0)
        assert caplog.text == "", "未满 5 轮不应有告警"
        reader.read(T0 - timedelta(minutes=60), T0, 60, T0)
        assert "连续 5 轮" in caplog.text


def test_failure_counter_resets_after_a_success(caplog):
    import json as _json
    import logging

    bodies = [b"<html>bad</html>"] * 4 + [_json.dumps(OK_BODY, ensure_ascii=False).encode()] + [
        b"<html>bad</html>"
    ] * 4
    queue = list(bodies)

    def fake_get(url, headers, timeout):
        return 200, queue.pop(0)

    reader = ActivityReader("http://localhost:3030", "k", 10.0, fake_get)
    with caplog.at_level(logging.WARNING, logger="statesense.activity.reader"):
        for _ in range(len(bodies)):
            reader.read(T0 - timedelta(minutes=60), T0, 60, T0)
    assert caplog.text == "", "中间成功过一次，计数应归零，不该凑满 5 轮"


def test_malformed_json_becomes_unreachable_snapshot():
    def fake_get(url, headers, timeout):
        return 200, b"<html>not json</html>"

    snap = ActivityReader("http://localhost:3030", "k", 10.0, fake_get).read(
        T0 - timedelta(minutes=60), T0, 60, T0
    )
    assert snap.data_status == "unreachable"


def test_transport_exception_becomes_unreachable_snapshot():
    def fake_get(url, headers, timeout):
        raise OSError("connection refused")

    snap = ActivityReader("http://localhost:3030", "k", 10.0, fake_get).read(
        T0 - timedelta(minutes=60), T0, 60, T0
    )
    assert snap.data_status == "unreachable"


def test_missing_numeric_fields_default_to_zero():
    body = {"data_status": "ok", "windows": [{"app_name": "x.exe", "window_name": "y"}]}
    snap = _reader(body).read(T0 - timedelta(minutes=60), T0, 60, T0)
    assert snap.total_active_minutes == 0.0
    assert snap.entries[0].minutes == 0.0


def test_window_bounds_are_recorded_verbatim():
    start, end = T0 - timedelta(minutes=60), T0
    snap = _reader(OK_BODY).read(start, end, 60, T0)
    assert snap.window_start == start
    assert snap.window_end == end
    assert snap.window_minutes == 60
    assert snap.captured_at == T0


def test_empty_api_key_is_rejected():
    with pytest.raises(ValueError, match="api_key"):
        ActivityReader("http://localhost:3030", "", 10.0, lambda *a: (200, b"{}"))

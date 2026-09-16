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


def test_falls_back_to_apps_when_windows_missing():
    body = {k: v for k, v in OK_BODY.items() if k != "windows"}
    snap = _reader(body).read(T0 - timedelta(minutes=60), T0, 60, T0)
    assert len(snap.entries) == 1
    assert snap.entries[0].app == "chrome.exe"
    assert snap.entries[0].title == ""


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

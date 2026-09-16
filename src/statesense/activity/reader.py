"""唯一接触 Screenpipe 的组件。上层只认 ActivitySnapshot，不知道 Screenpipe 存在。"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime

from statesense._time import iso

from .models import (
    KNOWN_STATUSES,
    UNREACHABLE,
    ActivitySnapshot,
    Entry,
)

log = logging.getLogger(__name__)

#: 连续失败多少轮之后才写一条告警日志。每轮都写会变成日志风暴。
FAILURES_BEFORE_WARNING = 5

HttpGet = Callable[[str, Mapping[str, str], float], "tuple[int, bytes]"]

_SKIP_TEXT_PARAMS = {
    "include_key_texts": "false",
    "include_snippets": "false",
    "include_memories": "false",
    "include_guidance": "false",
}


def urllib_get(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _num(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


class ActivityReader:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float,
        http_get: HttpGet = urllib_get,
    ) -> None:
        if not api_key:
            raise ValueError("api_key 不能为空")
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._http_get = http_get
        self._consecutive_failures = 0

    def read(
        self,
        start: datetime,
        end: datetime,
        window_minutes: int,
        captured_at: datetime,
    ) -> ActivitySnapshot:
        params = {
            "start_time": iso(start, seconds_only=True),
            "end_time": iso(end, seconds_only=True),
            **_SKIP_TEXT_PARAMS,
        }
        url = f"{self._base}/activity-summary?{urllib.parse.urlencode(params)}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "X-Screenpipe-Client": "api",
            "X-Screenpipe-Agent": "statesense",
            "Accept": "application/json",
        }

        try:
            status, body = self._http_get(url, headers, self._timeout)
        except Exception as exc:  # noqa: BLE001 - 任何异常都不能让调度器崩掉
            return self._degraded(start, end, window_minutes, captured_at, exc.__class__.__name__)

        if status != 200:
            return self._degraded(start, end, window_minutes, captured_at, f"http {status}")

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._degraded(start, end, window_minutes, captured_at, "malformed json")
        if not isinstance(payload, dict):
            return self._degraded(start, end, window_minutes, captured_at, "unexpected payload")

        windows = payload.get("windows")
        entries: list[Entry] = []
        if isinstance(windows, list):
            for row in windows:
                if not isinstance(row, dict):
                    continue
                entries.append(
                    Entry(
                        app=_text(row.get("app_name")),
                        title=_text(row.get("window_name")),
                        url=_text(row.get("browser_url")),
                        minutes=_num(row.get("minutes")),
                    )
                )

        total = _num(payload.get("total_active_minutes"))
        # 只在 windows 缺失/空但声称有活动时才提示。绝不拿 entries 之和去估算总时长 ——
        # entries 是窗口粒度的记录，用它估时间正是 spec §15.1 明令禁止的做法。
        if not entries and total > 0:
            log.warning(
                "activity-summary 有 %s 分钟活动却没有 windows 明细；本轮将无法识别任何状态",
                total,
            )

        raw_status = _text(payload.get("data_status"))
        if raw_status not in KNOWN_STATUSES:
            # 不认识的 data_status 说明响应结构与预期不符，按读不到处理。
            return self._degraded(
                start, end, window_minutes, captured_at, f"unrecognized data_status {raw_status!r}"
            )

        self._consecutive_failures = 0
        return ActivitySnapshot(
            window_start=start,
            window_end=end,
            window_minutes=window_minutes,
            total_active_minutes=total,
            entries=tuple(entries),
            data_status=raw_status,
            captured_at=captured_at,
        )

    def _degraded(
        self,
        start: datetime,
        end: datetime,
        window_minutes: int,
        captured_at: datetime,
        reason: str,
    ) -> ActivitySnapshot:
        """data_status 是封闭枚举，原因走日志 —— 它要入库并被相等比较，不能掺自由文本。"""
        self._consecutive_failures += 1
        # 每轮都写会变成日志风暴；按 spec §11 只在连续失败累积到阈值时提示。
        if self._consecutive_failures >= FAILURES_BEFORE_WARNING and (
            self._consecutive_failures % FAILURES_BEFORE_WARNING == 0
        ):
            log.warning(
                "activity-summary 连续 %d 轮不可用：%s", self._consecutive_failures, reason
            )
        else:
            log.debug("activity-summary 不可用（第 %d 轮）：%s", self._consecutive_failures, reason)
        return ActivitySnapshot(
            window_start=start,
            window_end=end,
            window_minutes=window_minutes,
            total_active_minutes=0.0,
            entries=(),
            data_status=UNREACHABLE,
            captured_at=captured_at,
        )

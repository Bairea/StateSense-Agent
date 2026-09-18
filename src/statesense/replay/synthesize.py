"""把剧本裁剪成某一时刻的活动快照。

产出的结构必须与真实 ActivityReader 返回的完全同形 —— 回放器不发明新结构，
否则它验证的就不是生产链路了。
"""

from __future__ import annotations

from datetime import datetime

from statesense.activity.models import ActivitySnapshot, Entry
from statesense.replay.scenario import Scenario


def snapshot_at(
    scenario: Scenario,
    start: datetime,
    end: datetime,
    window_minutes: int,
    captured_at: datetime,
    origin: datetime,
) -> ActivitySnapshot:
    """取 [start, end] 内与剧本有交集的部分。

    **窗口由 start/end 决定，不是 captured_at。** 真实的
    `ActivityReader.read(start, end, window_minutes, captured_at)` 就是这么用的 ——
    回执的 before/after 两次回查用的是同一 captured_at 但不同的 start/end，
    用 captured_at 当窗口末端会把两次回查算成同一段。
    """
    if scenario.data_status != "ok":
        # 降级场景：这一轮压根没取到数据，条目不成立。
        return ActivitySnapshot(
            window_start=start,
            window_end=end,
            window_minutes=window_minutes,
            total_active_minutes=0.0,
            entries=(),
            data_status=scenario.data_status,
            captured_at=captured_at,
        )

    window_start = (start - origin).total_seconds() / 60
    window_end = (end - origin).total_seconds() / 60

    entries: list[Entry] = []
    for segment in scenario.segments:
        overlap = min(window_end, segment.offset_minutes + segment.duration_minutes) - max(
            window_start, segment.offset_minutes
        )
        if overlap <= 0:
            continue
        entries.append(
            Entry(
                app=segment.app,
                title=segment.title,
                url=segment.url,
                minutes=round(overlap, 2),
            )
        )

    total = round(sum(e.minutes for e in entries), 2)
    return ActivitySnapshot(
        window_start=start,
        window_end=end,
        window_minutes=window_minutes,
        total_active_minutes=total,
        entries=tuple(entries),
        data_status=scenario.data_status,
        captured_at=captured_at,
    )

"""把剧本裁剪成某一时刻的活动快照。

产出的结构必须与真实 ActivityReader 返回的完全同形 —— 回放器不发明新结构，
否则它验证的就不是生产链路了。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from statesense.activity.models import ActivitySnapshot, Entry
from statesense.replay.scenario import Scenario


def snapshot_at(
    scenario: Scenario,
    now: datetime,
    window_minutes: int,
    origin: datetime,
) -> ActivitySnapshot:
    """取 [now - window, now] 内与剧本有交集的部分。

    段超出窗口的部分被裁掉，交叠部分按分钟计入条目。
    """
    start = now - timedelta(minutes=window_minutes)

    if scenario.data_status != "ok":
        # 降级场景：这一轮压根没取到数据，条目不成立。
        return ActivitySnapshot(
            window_start=start,
            window_end=now,
            window_minutes=window_minutes,
            total_active_minutes=0.0,
            entries=(),
            data_status=scenario.data_status,
            captured_at=now,
        )

    window_start = (start - origin).total_seconds() / 60
    window_end = (now - origin).total_seconds() / 60

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
        window_end=now,
        window_minutes=window_minutes,
        total_active_minutes=total,
        entries=tuple(entries),
        data_status=scenario.data_status,
        captured_at=now,
    )

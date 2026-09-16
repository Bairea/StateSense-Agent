"""状态推断。纯函数：不读时钟、不做 IO，时间由参数传入。

late_night 刻意不做成状态：凌晨 3 点刷 B 站，「凌晨」与「被动消费」是两个
正交事实，压成一个枚举必须二选一、必然丢信息。凌晨 3 点写代码尤其能说明
问题 —— 它的 state 是 NORMAL，但「凌晨」依然值得介入。
"""

from __future__ import annotations

from datetime import datetime

from statesense.activity.models import ActivitySnapshot
from statesense.config import TaxonomyConfig, ThresholdConfig
from statesense.state.models import State, StateVerdict
from statesense.state.taxonomy import Category, bucket_minutes


def is_late_night(
    instant: datetime, total_active_minutes: float, thresholds: ThresholdConfig
) -> bool:
    hour = instant.astimezone().hour
    in_window = thresholds.late_night_start_hour <= hour < thresholds.late_night_end_hour
    return in_window and total_active_minutes >= thresholds.late_night_min_active_minutes


def _round2(value: float) -> float:
    return round(value, 2)


def classify(
    snapshot: ActivitySnapshot,
    taxonomy: TaxonomyConfig,
    thresholds: ThresholdConfig,
) -> StateVerdict:
    buckets = bucket_minutes(snapshot.entries, taxonomy)
    ent = _round2(buckets[Category.ENTERTAINMENT])
    gray = _round2(buckets[Category.GRAY])
    work = _round2(buckets[Category.WORK])
    total = _round2(snapshot.total_active_minutes)
    ratio = _round2(ent / total) if total > 0 else 0.0

    shared = dict(
        late_night=is_late_night(snapshot.captured_at, total, thresholds),
        total_active_minutes=total,
        ent_minutes=ent,
        gray_minutes=gray,
        work_minutes=work,
        ent_ratio=ratio,
        window_minutes=snapshot.window_minutes,
        data_status=snapshot.data_status,
    )

    # data_status 不是 ok 时不下结论：那可能只是 recorder 没在采。
    if not snapshot.is_trustworthy:
        return StateVerdict(
            state=State.NORMAL, skipped=True, skip_reason=snapshot.data_status, **shared
        )

    if ent >= thresholds.high_risk_minutes:
        state = State.HIGH_RISK_PASSIVE_CONSUMPTION
    elif ent >= thresholds.passive_minutes:
        state = State.PASSIVE_CONSUMPTION
    elif ent >= thresholds.watch_minutes:
        state = State.WATCH
    else:
        state = State.NORMAL

    return StateVerdict(state=state, skipped=False, skip_reason=None, **shared)

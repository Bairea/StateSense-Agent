"""状态推断。纯函数：不读时钟、不做 IO，时间由参数传入。

late_night 刻意不做成状态：凌晨 3 点刷 B 站，「凌晨」与「被动消费」是两个
正交事实，压成一个枚举必须二选一、必然丢信息。凌晨 3 点写代码尤其能说明
问题 —— 它的 state 是 NORMAL，但「凌晨」依然值得介入。
"""

from __future__ import annotations

from datetime import datetime

from statesense.activity.models import ActivitySnapshot
from statesense.config import TaxonomyConfig, ThresholdConfig
from statesense.perception import is_gaming
from statesense.state.models import State, StateVerdict
from statesense.state.taxonomy import Category, bucket_minutes, entertainment_minutes


def is_late_night(
    instant: datetime, total_active_minutes: float, thresholds: ThresholdConfig
) -> bool:
    hour = instant.astimezone().hour
    in_window = thresholds.late_night_start_hour <= hour < thresholds.late_night_end_hour
    return in_window and total_active_minutes >= thresholds.late_night_min_active_minutes


def _round2(value: float) -> float:
    return round(value, 2)


def effective_entertainment_minutes(
    snapshot: ActivitySnapshot,
    taxonomy: TaxonomyConfig,
    *,
    fullscreen_state: int | None = None,
) -> float:
    """被动消费分钟数，含全屏提权。

    状态判定与行为回执**必须共用这一个口径** —— V0 审查发现过两处各算一份，
    一旦漂移，「干预前 vs 干预后」就不是同一个量，回执会失真。

    回执的 before/after 只能用**当前**全屏状态：历史状态没有留存。
    因此「打游戏时触发、随后退出游戏」的回执里 before 会被低估，
    可能落成 no_data。这是已知代价，如实记录，不假装精确。
    """
    ent = entertainment_minutes(snapshot.entries, taxonomy)
    if snapshot.is_trustworthy and is_gaming(fullscreen_state):
        other = _round2(bucket_minutes(snapshot.entries, taxonomy)[Category.OTHER])
        ent = _round2(ent + other)
    return ent


def classify(
    snapshot: ActivitySnapshot,
    taxonomy: TaxonomyConfig,
    thresholds: ThresholdConfig,
    *,
    # 默认 None = 「全屏状态未知」。未知是安全的保守行为（不提权），
    # 不是静默失效 —— 与 entries_minutes 那种「0.0 本身就是可疑信号」不同。
    # `StateVerdict.fullscreen_state` 则不给默认值：那是被记录下来的事实，
    # 必须永远显式写。
    fullscreen_state: int | None = None,
) -> StateVerdict:
    buckets = bucket_minutes(snapshot.entries, taxonomy)
    ent = effective_entertainment_minutes(
        snapshot, taxonomy, fullscreen_state=fullscreen_state
    )
    gray = _round2(buckets[Category.GRAY])
    work = _round2(buckets[Category.WORK])
    total = _round2(snapshot.total_active_minutes)

    # 全屏 D3D 应用在跑 → 把「没命中任何规则」的条目算作娱乐。
    #
    # 游戏窗口的标题与进程名就是游戏自身（实测 Brotato.exe / Brotato），平台名
    # 抓不到；这一步让「在玩游戏」不需要游戏清单也能被识别。只提权 OTHER，
    # 不动 WORK / GRAY —— 边打游戏边开终端时，终端时间不该算娱乐。
    #
    # 只在数据可信时提权：不可信时连 ent 本身都不该有结论。
    # 口径本身在 effective_entertainment_minutes 里，与回执共用。
    entries = _round2(sum(e.minutes for e in snapshot.entries))
    ratio = _round2(ent / total) if total > 0 else 0.0

    shared = dict(
        late_night=is_late_night(snapshot.captured_at, total, thresholds),
        total_active_minutes=total,
        ent_minutes=ent,
        gray_minutes=gray,
        work_minutes=work,
        ent_ratio=ratio,
        entries_minutes=entries,
        fullscreen_state=fullscreen_state,
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

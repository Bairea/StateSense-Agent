"""状态推断。纯函数：不读时钟、不做 IO，时间由参数传入。

late_night 刻意不做成状态：凌晨 3 点刷 B 站，「凌晨」与「被动消费」是两个
正交事实，压成一个枚举必须二选一、必然丢信息。凌晨 3 点写代码尤其能说明
问题 —— 它的 state 是 NORMAL，但「凌晨」依然值得介入。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from statesense.activity.models import ActivitySnapshot
from statesense.config import TaxonomyConfig, ThresholdConfig
from statesense.perception import is_gaming
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


def effective_entertainment_minutes(
    buckets: Mapping[Category, float],
    *,
    trustworthy: bool,
    gaming: bool,
) -> float:
    """被动消费分钟数，含全屏提权。**这是全仓唯一的计算公式。**

    状态判定与行为回执必须共用它 —— V0 审查发现过两处各算一份，
    一旦漂移，「干预前 vs 干预后」就不是同一个量，回执会失真。

    传入 `buckets` 而不是 `snapshot`：归类是整轮里最贵的一步，而
    `classify` 本来就要算一次。让两边各自再算一遍，既浪费又埋下
    「两份分类结果哪天不一致」的隐患 —— 参数化之后只有一处会分类。

    回执的 before/after 只能用**当前**全屏状态：历史状态没有留存。
    因此「打游戏时触发、随后退出游戏」的回执里 before 会被低估，
    可能落成 no_data。这是已知代价，如实记录，不假装精确。
    """
    ent = _round2(buckets[Category.ENTERTAINMENT])
    if trustworthy and gaming:
        ent = _round2(ent + _round2(buckets[Category.OTHER]))
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
    # 默认 None = 「自己归类」。传进来则复用调用方已经算好的那一份。
    #
    # 归类是整轮里最贵的一步，而一轮 tick 里可能需要它两次（判定 + 影子输入的
    # 采样）。两次各算一遍不会算出不同结果（纯函数、同一输入），但会把最贵的
    # 那一步做两遍 —— 影子模式默认关闭，这条参数只为「打开它时不额外付费」。
    #
    # 这个默认值不违反「不给默认值」的原则：它不改变任何被记录的事实，
    # 只影响要不要重算一次纯函数；两种取值下的 `StateVerdict` 必然相同。
    buckets: Mapping[Category, float] | None = None,
) -> StateVerdict:
    by_category = bucket_minutes(snapshot.entries, taxonomy) if buckets is None else buckets
    # 全屏应用在跑 → 把「没命中任何规则」的条目算作娱乐。
    #
    # 游戏窗口的标题与进程名就是游戏自身（实测 Brotato.exe / Brotato），平台名
    # 抓不到；这一步让「在玩游戏」不需要游戏清单也能被识别。只提权 OTHER，
    # 不动 WORK / GRAY —— 边打游戏边开终端时，终端时间不该算娱乐。
    #
    # 提权规则与阈值都在 effective_entertainment_minutes 里，与回执共用同一口径。
    # 数据不可信时 `trustworthy=False`，连 ent 本身都不该有结论。
    ent = effective_entertainment_minutes(
        by_category,
        trustworthy=snapshot.is_trustworthy,
        gaming=is_gaming(fullscreen_state),
    )
    gray = _round2(by_category[Category.GRAY])
    work = _round2(by_category[Category.WORK])
    total = _round2(snapshot.total_active_minutes)

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

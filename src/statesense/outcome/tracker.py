"""行为回执：干预后复查同一窗口，看被动消费是否下降。

原始值 ent_before / ent_after 必须入库，标签只是派生 —— 「多低才算有效」
这个判断以后可能会改，原始数据不能丢。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from statesense.activity.models import ActivitySnapshot
from statesense.config import OutcomeConfig

from .models import OutcomeVerdict

log = logging.getLogger(__name__)

NO_DATA = "no_data"


def label(before: float, after: float, config: OutcomeConfig) -> str:
    if after < before * config.disengaged_ratio:
        return "disengaged"
    if after < before * config.continued_ratio:
        return "partial"
    return "continued"


def evaluate(
    before: ActivitySnapshot,
    after: ActivitySnapshot,
    *,
    ent_before_minutes: Callable[[ActivitySnapshot], float],
    ent_after_minutes: Callable[[ActivitySnapshot], float],
    config: OutcomeConfig,
) -> OutcomeVerdict:
    """两侧各算一次，且**各自用自己的证据** —— 所以是两个可调用对象，不是一个。

    曾经这里只收一个 `ent_minutes`，调用方把「当下探测到的全屏状态」绑给它，
    前侧历史窗口因此也用当下的取值：打游戏时触发、随后退出游戏，`ent_before`
    会被按「没在玩游戏」重算，被动消费被低估甚至清零，回执落成 no_data，
    而「退出游戏」看起来像「干预有效」。

    两个参数不是风格问题：一个参数时，「两侧都用同一份证据」是**默认会犯的错**；
    拆成两个之后，想让两侧同源得显式传同一个对象，那是写得出、也看得见的动作。
    """
    ent_before = ent_before_minutes(before)
    ent_after = ent_after_minutes(after)
    window_minutes = float(after.window_minutes)

    # 任一侧采集中断，或干预前根本没有被动消费，都不能当结论。
    if not before.is_trustworthy or not after.is_trustworthy:
        return OutcomeVerdict(
            outcome=NO_DATA,
            ent_before=ent_before,
            ent_after=ent_after,
            after_window_minutes=window_minutes,
        )

    if ent_before <= 0:
        # 干预的前提是 ent >= 40，走到这里说明判定或取数有问题，不能当成「干预成功」。
        log.warning("干预前的被动消费为 0，回执不可信（判定与取数可能不一致）")
        return OutcomeVerdict(
            outcome=NO_DATA,
            ent_before=ent_before,
            ent_after=ent_after,
            after_window_minutes=window_minutes,
        )

    return OutcomeVerdict(
        outcome=label(ent_before, ent_after, config),
        ent_before=ent_before,
        ent_after=ent_after,
        after_window_minutes=window_minutes,
    )

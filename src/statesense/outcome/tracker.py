"""行为回执：干预后复查同一窗口，看被动消费是否下降。

原始值 ent_before / ent_after 必须入库，标签只是派生 —— 「多低才算有效」
这个判断以后可能会改，原始数据不能丢。
"""

from __future__ import annotations

from collections.abc import Callable

from statesense.activity.models import ActivitySnapshot
from statesense.config import OutcomeConfig

from .models import OutcomeVerdict

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
    ent_minutes: Callable[[ActivitySnapshot], float],
    config: OutcomeConfig,
) -> OutcomeVerdict:
    ent_before = ent_minutes(before)
    ent_after = ent_minutes(after)
    window_minutes = float(after.window_minutes)

    # 任一侧采集中断，或干预前根本没有被动消费，都不能当结论。
    if not before.is_trustworthy or not after.is_trustworthy or ent_before <= 0:
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

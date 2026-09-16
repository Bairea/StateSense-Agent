"""动作池与轮转选择。

用轮转而非随机：随机会让「哪个动作最有效」无法归因。轮转保证每个动作拿到
大致均衡的样本，正好喂给 V1 的「什么干预最有效」分析。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from statesense.config import Action
from statesense.state.models import State


def candidates(actions: Iterable[Action], state: State) -> tuple[Action, ...]:
    return tuple(a for a in actions if str(state) in a.applies_to)


def pick(pool: Sequence[Action], last_action_id: str | None) -> Action | None:
    if not pool:
        return None
    if last_action_id is None:
        return pool[0]
    for index, action in enumerate(pool):
        if action.id == last_action_id:
            return pool[(index + 1) % len(pool)]
    return pool[0]

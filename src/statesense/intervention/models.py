"""闸门与决策的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    #: 闸门的实际值。为 None 表示「这个概念此刻不适用」（例如从未干预过，就没有「距上次多少分钟」），
    #: 而不是用 inf 之类的哨兵值假装它是个数。
    value: float | None
    threshold: float


@dataclass(frozen=True)
class Decision:
    intervene: bool
    action_id: str | None
    reason: str
    gate_trace: tuple[GateResult, ...]

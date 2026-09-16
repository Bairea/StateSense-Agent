"""闸门与决策的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    value: float
    threshold: float


@dataclass(frozen=True)
class Decision:
    intervene: bool
    action_id: str | None
    reason: str
    gate_trace: tuple[GateResult, ...]

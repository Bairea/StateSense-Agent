"""闸门与决策的数据契约，以及闸门留痕的**唯一**序列化/解析实现。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


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


def dump_gate_trace(trace: tuple[GateResult, ...]) -> str:
    """落库形态。写与读必须成对出现，否则两边会各自漂移。"""
    return json.dumps([asdict(g) for g in trace], ensure_ascii=False)


def parse_gate_trace(text: str) -> tuple[GateResult, ...] | None:
    """解析闸门留痕。

    返回值三态，刻意区分：
      · `()`   —— 合法且为空（例如未启用任何闸门）；
      · 元组    —— 解析出的闸门；
      · `None` —— **这行坏了**，读不出任何一条闸门。

    把 `None` 与 `()` 混成一回事，正是本版本要消灭的那类二义：报告会因此
    分不清「没有闸门信息」和「闸门信息损坏」，闸门校准就会建立在坏数据上。

    单个元素畸形时保留其余元素：为一颗坏齿轮丢掉整行轨迹不值得。只有
    「一条都读不出来」才算损坏，此时必须让调用方计数并跳过，绝不中断整份报告。
    """
    try:
        raw: Any = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, list):
        return None

    gates: list[GateResult] = []
    for item in raw:
        if not isinstance(item, dict) or "name" not in item or "passed" not in item:
            continue
        try:
            gates.append(
                GateResult(
                    name=str(item["name"]),
                    passed=bool(item["passed"]),
                    value=None if item.get("value") is None else float(item["value"]),
                    threshold=float(item["threshold"]),
                )
            )
        except (TypeError, ValueError):
            continue
    if raw and not gates:
        return None
    return tuple(gates)


def first_failed(trace: tuple[GateResult, ...]) -> GateResult | None:
    """第一个未通过的闸门。闸门按配置顺序跑，所以「第一个」就是判定失败的主因。"""
    return next((g for g in trace if not g.passed), None)

"""介入闸门。状态回答「我在什么状态」，闸门回答「现在该不该打扰」。

每个条件都返回 (通过?, 实际值, 阈值)，无论通过与否都记录 ——
否则日志里看不出是被哪一条挡下的，阈值就无从调起。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from statesense.config import (
    GATE_COOLDOWN,
    GATE_DAILY_CAP,
    GATE_RATIO_MIN,
    GATE_STATE_MIN,
    KNOWN_GATES,
    GateConfig,
)
from statesense.intervention.models import GateResult
from statesense.state.models import State, StateVerdict

INTERVENABLE = frozenset({State.PASSIVE_CONSUMPTION, State.HIGH_RISK_PASSIVE_CONSUMPTION})


@dataclass(frozen=True)
class GateContext:
    verdict: StateVerdict
    config: GateConfig
    now: datetime
    last_intervention_at: datetime | None
    interventions_today: int


def evaluate_state_min(ctx: GateContext) -> GateResult:
    passed = ctx.verdict.state in INTERVENABLE
    return GateResult(name=GATE_STATE_MIN, passed=passed, value=1.0 if passed else 0.0, threshold=1.0)


def evaluate_ratio_min(ctx: GateContext) -> GateResult:
    threshold = ctx.config.required_ratio_min()
    return GateResult(
        name=GATE_RATIO_MIN,
        passed=ctx.verdict.ent_ratio >= threshold,
        value=ctx.verdict.ent_ratio,
        threshold=threshold,
    )


def evaluate_cooldown(ctx: GateContext) -> GateResult:
    limit = ctx.config.cooldown_minutes
    if ctx.last_intervention_at is None:
        # 从未干预过：没有「距上次多少分钟」这个量，只能显式为 None，不要用 inf 假装它是个数。
        return GateResult(name=GATE_COOLDOWN, passed=True, value=None, threshold=limit)
    elapsed = (ctx.now - ctx.last_intervention_at).total_seconds() / 60.0
    return GateResult(
        name=GATE_COOLDOWN, passed=elapsed >= limit, value=round(elapsed, 1), threshold=limit
    )


def evaluate_daily_cap(ctx: GateContext) -> GateResult:
    return GateResult(
        name=GATE_DAILY_CAP,
        passed=ctx.interventions_today < ctx.config.daily_cap,
        value=float(ctx.interventions_today),
        threshold=float(ctx.config.daily_cap),
    )


GATE_REGISTRY: dict[str, Callable[[GateContext], GateResult]] = {
    GATE_STATE_MIN: evaluate_state_min,
    GATE_RATIO_MIN: evaluate_ratio_min,
    GATE_COOLDOWN: evaluate_cooldown,
    GATE_DAILY_CAP: evaluate_daily_cap,
}

# 注册表必须覆盖配置层认可的全部闸门名。缺一个就意味着配置能写、运行时却静默不跑。
assert set(GATE_REGISTRY) == set(KNOWN_GATES), (
    f"GATE_REGISTRY 与 KNOWN_GATES 不一致：{set(GATE_REGISTRY) ^ set(KNOWN_GATES)}"
)


def run_gates(ctx: GateContext) -> tuple[GateResult, ...]:
    """按配置顺序跑闸门。名字合法性由配置层保证，这里不再过滤 —— 静默过滤正是缺陷来源。"""
    return tuple(GATE_REGISTRY[name](ctx) for name in ctx.config.enabled)

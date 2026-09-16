"""把闸门结果与动作选择合成为一次决策。纯函数。"""

from __future__ import annotations

from collections.abc import Iterable

from statesense.config import Action
from statesense.intervention.actions import pick
from statesense.intervention.gates import GateContext, run_gates
from statesense.intervention.models import Decision, GateResult


def _describe(result: GateResult) -> str:
    return f"{result.name} 未通过（{result.value} vs 阈值 {result.threshold}）"


def decide(ctx: GateContext, actions: Iterable[Action], last_action_id: str | None) -> Decision:
    trace = run_gates(ctx)
    blocked = [r for r in trace if not r.passed]
    if blocked:
        return Decision(
            intervene=False,
            action_id=None,
            reason="；".join(_describe(r) for r in blocked),
            gate_trace=trace,
        )

    chosen = pick(tuple(actions), last_action_id)
    if chosen is None:
        return Decision(
            intervene=False,
            action_id=None,
            reason="闸门全过但当前状态没有可用动作",
            gate_trace=trace,
        )
    return Decision(
        intervene=True,
        action_id=chosen.id,
        reason=f"{ctx.verdict.state} 且全部闸门通过",
        gate_trace=trace,
    )

from datetime import datetime, timezone

from statesense.config import Action, GateConfig
from statesense.intervention.decider import decide
from statesense.intervention.gates import GateContext
from statesense.state.models import State, StateVerdict

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)
POOL = (
    Action("walk5", "离开电脑走 5 分钟", ("PASSIVE_CONSUMPTION",)),
    Action("calligraphy", "练字 5 分钟", ("PASSIVE_CONSUMPTION",)),
)


def _ctx(state=State.PASSIVE_CONSUMPTION, ratio=0.75) -> GateContext:
    return GateContext(
        verdict=StateVerdict(
            state=state,
            late_night=False,
            total_active_minutes=60.0,
            ent_minutes=45.0,
            gray_minutes=5.0,
            work_minutes=10.0,
            ent_ratio=ratio,
            entries_minutes=60.0,
            window_minutes=60,
            data_status="ok",
            skipped=False,
            skip_reason=None,
        ),
        config=GateConfig(ratio_min=0.75),
        now=T0,
        last_intervention_at=None,
        interventions_today=0,
    )


def test_decide_intervenes_when_all_gates_pass():
    d = decide(_ctx(), POOL, None)
    assert d.intervene is True
    assert d.action_id == "walk5"


def test_decide_records_gate_trace():
    assert len(decide(_ctx(), POOL, None).gate_trace) == 4


def test_decide_reason_names_the_blocking_gate():
    d = decide(_ctx(ratio=0.5), POOL, None)
    assert d.intervene is False
    assert "ratio_min" in d.reason


def test_decide_reason_names_every_blocking_gate():
    d = decide(_ctx(state=State.NORMAL, ratio=0.1), POOL, None)
    assert d.intervene is False
    assert "state_min" in d.reason
    assert "ratio_min" in d.reason


def test_decide_does_not_intervene_when_pool_is_empty():
    d = decide(_ctx(state=State.HIGH_RISK_PASSIVE_CONSUMPTION), (), None)
    assert d.intervene is False
    assert "没有可用动作" in d.reason


def test_decide_advances_rotation():
    assert decide(_ctx(), POOL, "walk5").action_id == "calligraphy"

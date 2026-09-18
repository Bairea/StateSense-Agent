from datetime import datetime, timedelta, timezone

from statesense.config import GateConfig
from statesense.intervention.gates import GateContext, run_gates
from statesense.state.models import State, StateVerdict

T0 = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)


def _verdict(state=State.PASSIVE_CONSUMPTION, ratio=0.75) -> StateVerdict:
    return StateVerdict(
        state=state,
        late_night=False,
        total_active_minutes=60.0,
        ent_minutes=45.0,
        gray_minutes=5.0,
        work_minutes=10.0,
        ent_ratio=ratio,
        entries_minutes=60.0,
        fullscreen_state=None,
        window_minutes=60,
        data_status="ok",
        skipped=False,
        skip_reason=None,
    )


def _ctx(verdict=None, *, now=T0, last=None, today=0, gate=None) -> GateContext:
    return GateContext(
        verdict=verdict or _verdict(),
        config=gate or GateConfig(ratio_min=0.75),
        now=now,
        last_intervention_at=last,
        interventions_today=today,
    )


def _named(results, name):
    return next(r for r in results if r.name == name)


def test_all_gates_pass_in_the_happy_path():
    results = run_gates(_ctx())
    assert all(r.passed for r in results)
    assert [r.name for r in results] == ["state_min", "ratio_min", "cooldown", "daily_cap"]


def test_state_min_blocks_normal():
    assert _named(run_gates(_ctx(_verdict(state=State.NORMAL))), "state_min").passed is False


def test_state_min_blocks_watch():
    assert _named(run_gates(_ctx(_verdict(state=State.WATCH))), "state_min").passed is False


def test_state_min_allows_high_risk():
    ctx = _ctx(_verdict(state=State.HIGH_RISK_PASSIVE_CONSUMPTION))
    assert _named(run_gates(ctx), "state_min").passed is True


def test_ratio_min_blocks_below_threshold():
    result = _named(run_gates(_ctx(_verdict(ratio=0.70))), "ratio_min")
    assert result.passed is False
    assert result.value == 0.70
    assert result.threshold == 0.75


def test_ratio_min_passes_exactly_at_threshold():
    assert _named(run_gates(_ctx(_verdict(ratio=0.75))), "ratio_min").passed is True


def test_cooldown_blocks_within_window():
    result = _named(run_gates(_ctx(last=T0 - timedelta(minutes=10))), "cooldown")
    assert result.passed is False
    assert result.value == 10.0
    assert result.threshold == 30


def test_cooldown_passes_after_window():
    assert _named(run_gates(_ctx(last=T0 - timedelta(minutes=31))), "cooldown").passed is True


def test_cooldown_passes_when_never_intervened():
    assert _named(run_gates(_ctx(last=None)), "cooldown").passed is True


def test_daily_cap_blocks_at_limit():
    assert _named(run_gates(_ctx(today=8)), "daily_cap").passed is False


def test_daily_cap_passes_below_limit():
    assert _named(run_gates(_ctx(today=7)), "daily_cap").passed is True


def test_gate_trace_has_all_entries_even_when_everything_is_blocked():
    """日志必须能看出被哪条挡下，所以无论通过与否都要有记录。"""
    ctx = _ctx(_verdict(state=State.NORMAL, ratio=0.1), last=T0, today=99)
    results = run_gates(ctx)
    assert len(results) == 4
    assert sum(1 for r in results if not r.passed) == 4


def test_disabled_gate_is_not_run():
    gate = GateConfig(enabled=("state_min",), ratio_min=0.75)
    assert [r.name for r in run_gates(_ctx(gate=gate))] == ["state_min"]

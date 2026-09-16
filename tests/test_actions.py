from statesense.config import Action
from statesense.intervention.actions import candidates, pick
from statesense.state.models import State

POOL = (
    Action("walk5", "离开电脑走 5 分钟", ("PASSIVE_CONSUMPTION", "HIGH_RISK_PASSIVE_CONSUMPTION")),
    Action("calligraphy", "练字 5 分钟", ("PASSIVE_CONSUMPTION", "HIGH_RISK_PASSIVE_CONSUMPTION")),
    Action("reading", "读一段《道德经》", ("HIGH_RISK_PASSIVE_CONSUMPTION",)),
)


def test_candidates_filter_by_state():
    assert [a.id for a in candidates(POOL, State.PASSIVE_CONSUMPTION)] == ["walk5", "calligraphy"]
    assert [a.id for a in candidates(POOL, State.HIGH_RISK_PASSIVE_CONSUMPTION)] == [
        "walk5",
        "calligraphy",
        "reading",
    ]


def test_candidates_empty_for_normal_state():
    assert candidates(POOL, State.NORMAL) == ()


def test_pick_starts_at_first_when_no_history():
    assert pick(candidates(POOL, State.PASSIVE_CONSUMPTION), None).id == "walk5"


def test_pick_round_robins():
    pool = candidates(POOL, State.PASSIVE_CONSUMPTION)
    assert pick(pool, "walk5").id == "calligraphy"
    assert pick(pool, "calligraphy").id == "walk5"


def test_pick_wraps_around():
    pool = candidates(POOL, State.HIGH_RISK_PASSIVE_CONSUMPTION)
    assert pick(pool, "reading").id == "walk5"


def test_pick_falls_back_to_first_when_last_not_in_pool():
    """状态变化导致上次动作不在候选里时，从头发起，不能崩。"""
    assert pick(candidates(POOL, State.PASSIVE_CONSUMPTION), "reading").id == "walk5"


def test_pick_returns_none_for_empty_pool():
    assert pick((), "walk5") is None

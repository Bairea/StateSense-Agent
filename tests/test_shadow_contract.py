"""影子契约：输入白名单、输出枚举、信号不变式。

这些测试大半是**镜像断言**。契约散在两处（dataclass 的字段、模块里的常量）时，
只改一处不会报错 —— 而「悄悄扩大发送面」恰好就是这一类：
给 dataclass 加个字段、顺手带上窗口标题，程序照跑，没人会知道数据已经出去了。
让测试盯着两处的对应关系，比在注释里写「记得同步改」可靠。
"""

from __future__ import annotations

import pytest

from statesense.shadow.models import (
    ALLOWED_FIELDS,
    DEFERRED_FIELDS,
    INPUT_CONTRACT_VERSION,
    LEAKY_FIELDS,
    PRIVATE_FIELDS,
    ModelCandidate,
    ModelSignal,
    ShadowOutcome,
    StateShadowInput,
)
from statesense.state.models import SEVERITY_ORDER, State, severity_rank


def _input(**over) -> StateShadowInput:
    base = dict(
        ent_minutes=45.0,
        gray_minutes=5.0,
        work_minutes=10.0,
        total_active_minutes=60.0,
        window_minutes=60,
        late_night=False,
    )
    base.update(over)
    return StateShadowInput(**base)


def _signal(**over) -> ModelSignal:
    base = dict(
        outcome=ShadowOutcome.OK,
        candidate=ModelCandidate.NORMAL,
        model_version="test-model",
        prompt_version="test-prompt",
        latency_ms=12.0,
        reason=None,
        detail=None,
    )
    base.update(over)
    return ModelSignal(**base)


# ── 输入白名单 ──────────────────────────────────────────────

def test_input_fields_match_the_documented_allowlist():
    """`StateShadowInput` 的字段就是白名单，必须与 `ALLOWED_FIELDS` 逐字相同。

    这条断言是「模型实际看到了什么」的唯一守卫：加一个字段就会失败，
    而它失败的方式是「必须去改那份被人读的名单」，不是在某次调用里静默多发一列。
    """
    assert tuple(StateShadowInput.__dataclass_fields__) == ALLOWED_FIELDS


def test_payload_carries_exactly_the_allowlist():
    """载荷逐字段写出来，键集必须与白名单一致 —— 不多、不少、顺序相同。"""
    assert tuple(_input().payload()) == ALLOWED_FIELDS


@pytest.mark.parametrize("field", PRIVATE_FIELDS + LEAKY_FIELDS + DEFERRED_FIELDS)
def test_field_kept_out_of_the_input_stays_out(field):
    assert field not in ALLOWED_FIELDS
    assert field not in StateShadowInput.__dataclass_fields__
    assert field not in _input().payload()


def test_the_three_exclusion_lists_do_not_overlap():
    """「私人内容」「规则答案」「以后再说」是三种不同的理由，重叠会让理由说不清：
    同一个字段同时因为两个原因被排除时，将来想放开它就得先弄清该减掉哪一条。"""
    groups = [set(PRIVATE_FIELDS), set(LEAKY_FIELDS), set(DEFERRED_FIELDS)]
    union: set[str] = set()
    for group in groups:
        assert not (union & group), f"字段同时出现在两份名单里：{sorted(union & group)}"
        union |= group


def test_input_contract_version_is_a_literal_string():
    """版本号要落进库、要能被人一眼比对，因此是一个明确写下的字面量。"""
    assert isinstance(INPUT_CONTRACT_VERSION, str)
    assert INPUT_CONTRACT_VERSION


# ── 输出枚举 ────────────────────────────────────────────────

def test_candidate_states_mirror_the_state_enum():
    """候选里的四个状态档与 `State` 同字面量。

    另造一套拼法（比如小写）会让影子对比多一层翻译，而那一层出错的后果是
    「明明同一档却算成两档」—— 分歧率因此凭空变大，且没有任何报错。
    """
    values = {candidate.value for candidate in ModelCandidate}
    for state in State:
        assert state.value in values, f"{state} 在候选枚举里没有对应档"


def test_candidate_maps_back_to_a_state_and_abstentions_do_not():
    for state in SEVERITY_ORDER:
        assert ModelCandidate(state.value).state is state
    for abstain in (ModelCandidate.UNCERTAIN, ModelCandidate.REFUSED):
        assert abstain.state is None, "「不下结论」不能映射成任何状态"


def test_severity_order_covers_every_state_exactly_once():
    """顺序表漏掉一个状态，`severity_rank` 会在真实数据上抛 KeyError ——
    在这里挡住，比在 daemon 里挂掉便宜得多。"""
    assert set(SEVERITY_ORDER) == set(State)
    assert len(SEVERITY_ORDER) == len(State)
    assert [severity_rank(state) for state in SEVERITY_ORDER] == list(range(len(SEVERITY_ORDER)))


def test_severity_ranks_follow_the_threshold_ladder():
    """深浅顺序必须与阈值阶梯同向：WATCH 比 NORMAL 深、HIGH_RISK 比 PASSIVE 深。

    顺序写反了不会报错，只会让「候选更重 / 更轻」整片反过来 ——
    而那正是本视图唯一的结论口径。
    """
    assert severity_rank(State.NORMAL) < severity_rank(State.WATCH)
    assert severity_rank(State.WATCH) < severity_rank(State.PASSIVE_CONSUMPTION)
    assert severity_rank(State.PASSIVE_CONSUMPTION) < severity_rank(
        State.HIGH_RISK_PASSIVE_CONSUMPTION
    )


# ── ModelSignal 的不变式 ────────────────────────────────────

def test_ok_without_a_candidate_is_rejected():
    with pytest.raises(ValueError, match="自相矛盾"):
        _signal(candidate=None)


def test_failure_outcome_must_not_carry_a_candidate():
    """失败档带上候选，等于把「模型没答上来」写成了「模型答了某档」。"""
    with pytest.raises(ValueError, match="自相矛盾"):
        _signal(outcome=ShadowOutcome.TIMEOUT, candidate=ModelCandidate.NORMAL)


def test_only_no_data_may_omit_latency():
    """没调用就没有耗时，写 0.0 会与「一次极快的成功调用」同值。"""
    with pytest.raises(ValueError, match="latency_ms"):
        _signal(latency_ms=None)
    with pytest.raises(ValueError, match="latency_ms"):
        _signal(outcome=ShadowOutcome.NO_DATA, candidate=None, latency_ms=3.0)


def test_no_data_without_latency_is_valid():
    signal = _signal(outcome=ShadowOutcome.NO_DATA, candidate=None, latency_ms=None)
    assert signal.candidate is None and signal.latency_ms is None


def test_negative_latency_is_rejected():
    with pytest.raises(ValueError, match="负"):
        _signal(latency_ms=-0.5)

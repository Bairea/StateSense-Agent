"""采集器：把每条失败路径翻译成一个可落库的信号，且**绝不向调用方抛异常**。

这一层的核心性质不是「能调用模型」，而是「调用失败时系统仍然正常」。
因此这里大半在测失败：超时、畸形输出、调用抛错、输入不可信、答复池用尽 ——
每一条都必须变成一条记录，而不是一个异常。
"""

from __future__ import annotations

import pytest

from statesense._detail import DETAIL_LIMIT
from statesense.shadow.collector import ShadowCollector
from statesense.shadow.models import (
    ModelCandidate,
    RawModelReply,
    ShadowOutcome,
    StateShadowInput,
)
from statesense.shadow.provider import (
    OFFLINE_MODEL_VERSION,
    OFFLINE_PROMPT_VERSION,
    ScriptedShadowProvider,
)


class _Ticks:
    """按调用次序给出秒数的假单调钟。

    延迟要进报表，就必须能被钉成确定值 —— 直接读 `time.perf_counter` 的话，
    这里只能断言「大于 0」，那等于没断言。
    """

    def __init__(self, values: list[float]) -> None:
        self._values = values
        self._index = 0

    def __call__(self) -> float:
        value = self._values[min(self._index, len(self._values) - 1)]
        self._index += 1
        return value


def _payload() -> StateShadowInput:
    return StateShadowInput(
        ent_minutes=45.0,
        gray_minutes=5.0,
        work_minutes=10.0,
        total_active_minutes=60.0,
        window_minutes=60,
        late_night=False,
    )


def _collector(
    replies: list[RawModelReply | BaseException],
    *,
    ticks: list[float] | None = None,
    timeout_seconds: float = 3.0,
) -> tuple[ShadowCollector, ScriptedShadowProvider]:
    provider = ScriptedShadowProvider(replies)
    collector = ShadowCollector(
        provider,
        timeout_seconds=timeout_seconds,
        now_seconds=_Ticks(ticks or [0.0, 0.25]),
    )
    return collector, provider


# ── 成功路径 ────────────────────────────────────────────────

def test_ok_carries_the_candidate_the_latency_and_the_reason():
    collector, _ = _collector([RawModelReply("PASSIVE_CONSUMPTION", "看起来在被困住")])

    signal = collector.collect(_payload(), trustworthy=True)

    assert signal.outcome is ShadowOutcome.OK
    assert signal.candidate is ModelCandidate.PASSIVE_CONSUMPTION
    assert signal.latency_ms == 250.0
    assert signal.reason == "看起来在被困住"


def test_provider_identifiers_land_on_every_outcome():
    """模型与提示版本必须出现在**失败**档上：失败样本要能按模型分组统计。"""
    collector, provider = _collector([TimeoutError("慢")])

    signal = collector.collect(_payload(), trustworthy=True)

    assert signal.model_version == OFFLINE_MODEL_VERSION == provider.model_version
    assert signal.prompt_version == OFFLINE_PROMPT_VERSION == provider.prompt_version


def test_abstentions_are_valid_ok_outcomes():
    """不确定与拒答都是**成功的回答**，不是失败 —— 拒答率的分母是「问了几次」。"""
    collector, _ = _collector(
        [RawModelReply("uncertain", "证据不足"), RawModelReply("refused", "不判断")]
    )

    first = collector.collect(_payload(), trustworthy=True)
    second = collector.collect(_payload(), trustworthy=True)

    assert first.outcome is second.outcome is ShadowOutcome.OK
    assert first.candidate is ModelCandidate.UNCERTAIN
    assert second.candidate is ModelCandidate.REFUSED
    assert first.candidate.state is None


def test_reason_is_truncated_to_the_shared_limit():
    collector, _ = _collector([RawModelReply("NORMAL", "字" * 500)])

    signal = collector.collect(_payload(), trustworthy=True)

    assert signal.reason is not None
    assert len(signal.reason) <= DETAIL_LIMIT


# ── 失败路径 ────────────────────────────────────────────────

def test_transport_timeout_becomes_a_timeout_record():
    collector, _ = _collector([TimeoutError("传输层超时")])

    signal = collector.collect(_payload(), trustworthy=True)

    assert signal.outcome is ShadowOutcome.TIMEOUT
    assert signal.candidate is None
    assert signal.latency_ms == 250.0
    assert "TimeoutError" in (signal.detail or "")


def test_candidate_outside_the_enum_is_never_mapped_to_the_nearest_state():
    """`PASSIVE` 这种「像但不合法」的缩写必须落成畸形输出。

    就近映射（把它猜成 PASSIVE_CONSUMPTION）会把「模型说了胡话」记成一个合法的
    状态候选 —— 于是畸形容错被记成一次成功的判断，而失败率永远是 0。
    """
    collector, _ = _collector([RawModelReply("PASSIVE")])

    signal = collector.collect(_payload(), trustworthy=True)

    assert signal.outcome is ShadowOutcome.INVALID_OUTPUT
    assert signal.candidate is None
    assert "PASSIVE" in (signal.detail or "")


def test_any_provider_exception_becomes_a_record_not_a_raise():
    collector, _ = _collector([RuntimeError("连接被重置")])

    signal = collector.collect(_payload(), trustworthy=True)  # 不抛

    assert signal.outcome is ShadowOutcome.PROVIDER_ERROR
    assert "RuntimeError" in (signal.detail or "")
    assert signal.latency_ms is not None


def test_exhausted_replies_surface_as_provider_error():
    """替身答复用尽是剧本写漏了。它必须显形，不能被静默续用最后一条。"""
    collector, _ = _collector([])

    signal = collector.collect(_payload(), trustworthy=True)

    assert signal.outcome is ShadowOutcome.PROVIDER_ERROR
    assert "用尽" in (signal.detail or "")


def test_late_reply_is_discarded_by_the_result_layer_timeout():
    """返回了但已超期 —— 两层超时的第二层。迟到的答案既不是成功，也不该进记录。"""
    collector, _ = _collector(
        [RawModelReply("NORMAL")], ticks=[0.0, 5.0], timeout_seconds=3.0
    )

    signal = collector.collect(_payload(), trustworthy=True)

    assert signal.outcome is ShadowOutcome.TIMEOUT
    assert signal.candidate is None
    assert signal.latency_ms == 5000.0


def test_a_reply_inside_the_deadline_is_kept():
    """反向对照：同样一条答复，只要没超期就必须被采纳 —— 否则上一条测的是空跑。"""
    collector, _ = _collector(
        [RawModelReply("NORMAL")], ticks=[0.0, 2.0], timeout_seconds=3.0
    )

    assert collector.collect(_payload(), trustworthy=True).outcome is ShadowOutcome.OK


# ── 输入不可信 ──────────────────────────────────────────────

def test_untrustworthy_input_is_not_sent_to_the_model():
    """`data_status` 不是 ok 时连「有没有活动」都没有结论，问模型也没有意义。

    替身的答复池是空的：一旦真的调了一次，结果会是 provider_error。
    所以「记成 no_data」这条断言同时证明了没有发起调用。
    """
    collector, provider = _collector([])

    signal = collector.collect(_payload(), trustworthy=False)

    assert signal.outcome is ShadowOutcome.NO_DATA
    assert signal.candidate is None
    assert signal.latency_ms is None
    assert provider.calls == []


def test_no_data_detail_says_why_nothing_was_asked():
    collector, _ = _collector([])

    signal = collector.collect(_payload(), trustworthy=False)

    assert "未调用" in (signal.detail or "")


# ── 构造期校验 ──────────────────────────────────────────────

@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_non_positive_timeout_is_rejected_at_construction(bad):
    with pytest.raises(ValueError, match="timeout_seconds"):
        ShadowCollector(ScriptedShadowProvider(()), timeout_seconds=bad)

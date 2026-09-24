"""把一次模型调用翻译成一个可落库的信号。

**这个类最重要的性质是「它不会把任何东西抛给调用方」。** 影子模式的存在前提是
「绝不改变真实行为」，而一个能抛异常的调用点必然能改变真实行为 —— 于是它的
每一个失败路径都必须是**返回值**，不是异常。

关于超时，两层各管一段，这里写清楚免得被当成一个东西：

  · **传输层**（provider 实现）：早于返回即中断，抛 `TimeoutError`。
    collector 无法预先掐断一个阻塞调用 —— 没有线程、没有信号、没有 async，
    能做的只有等它回来。所以「别等太久」这件事只能由传输层保证。
  · **结果层**（这里）：返回了但已超过 `timeout_seconds` 的答复一律丢弃。
    它挡住的是「provider 没做超时」或「超时设得比实际等待短」这两种情况 ——
    迟到的答案既不能算成功，也不该进影子记录，否则「延迟」这一列就失去了上限。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from statesense._detail import clip, error_detail
from statesense.shadow.models import (
    ModelCandidate,
    ModelSignal,
    ShadowOutcome,
    StateShadowInput,
)
from statesense.shadow.provider import ShadowProvider


class ShadowCollector:
    """一次调用 = 一个 `ModelSignal`。没有「抛异常」这条出口。"""

    def __init__(
        self,
        provider: ShadowProvider,
        *,
        timeout_seconds: float,
        now_seconds: Callable[[], float] = time.perf_counter,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds 必须为正数，当前为 {timeout_seconds}")
        self._provider = provider
        self._timeout_seconds = timeout_seconds
        #: 延迟要进报表，因此它必须可注入：直接读 `time.perf_counter` 的话，
        #: 视图 9 的延迟断言只能写成「大于 0」，那等于没断言。
        #: 必须是**单调**源 —— `ModelSignal` 会拒绝负的延迟，而墙上时钟会倒退。
        self._now_seconds = now_seconds

    @property
    def model_version(self) -> str:
        return self._provider.model_version

    @property
    def prompt_version(self) -> str:
        return self._provider.prompt_version

    def collect(self, payload: StateShadowInput, *, trustworthy: bool) -> ModelSignal:
        """问一次模型。`trustworthy=False` 时**不调用**，直接记 `NO_DATA`。

        不调用是刻意的：`data_status` 不是 ok 时连「有没有活动」都没有结论，
        拿一份不可信的数据去问模型，得到的候选也不能用 —— 而它还占用了
        一次调用、一份延迟、一条样本。宁可记成「这一轮没问」。
        """
        if not trustworthy:
            return self._signal(
                ShadowOutcome.NO_DATA,
                candidate=None,
                latency_ms=None,
                detail="输入不可信（data_status 不是 ok）：未调用模型",
            )

        started = self._now_seconds()
        try:
            reply = self._provider.query(payload)
        except TimeoutError as exc:
            return self._signal(
                ShadowOutcome.TIMEOUT,
                candidate=None,
                latency_ms=self._elapsed_ms(started),
                detail=error_detail(exc),
            )
        except Exception as exc:  # noqa: BLE001 - 任何失败都只能变成一条记录
            return self._signal(
                ShadowOutcome.PROVIDER_ERROR,
                candidate=None,
                latency_ms=self._elapsed_ms(started),
                detail=error_detail(exc),
            )

        latency_ms = self._elapsed_ms(started)
        if latency_ms > self._timeout_seconds * 1000.0:
            # 迟到的答复不采纳：它既不是成功，也不该以「成功」的形态进报表。
            return self._signal(
                ShadowOutcome.TIMEOUT,
                candidate=None,
                latency_ms=latency_ms,
                detail=f"返回时已超过 {self._timeout_seconds:g} 秒上限：本轮丢弃该候选",
            )

        try:
            candidate = ModelCandidate(reply.candidate)
        except ValueError:
            # 有限枚举是唯一可落库的形态。枚举外的字符串一律记成畸形输出而不是
            # 就近映射 —— 就近映射会把「模型说了胡话」记成一个合法的状态候选。
            return self._signal(
                ShadowOutcome.INVALID_OUTPUT,
                candidate=None,
                latency_ms=latency_ms,
                detail=f"候选不在有限枚举里：{clip(reply.candidate)}",
            )

        return self._signal(
            ShadowOutcome.OK,
            candidate=candidate,
            latency_ms=latency_ms,
            reason=clip(reply.reason) if reply.reason else None,
            detail=None,
        )

    # ── 内部 ────────────────────────────────────────────────

    def _elapsed_ms(self, started: float) -> float:
        return round((self._now_seconds() - started) * 1000.0, 3)

    def _signal(
        self,
        outcome: ShadowOutcome,
        *,
        candidate: ModelCandidate | None,
        latency_ms: float | None,
        detail: str | None,
        reason: str | None = None,
    ) -> ModelSignal:
        """收敛构造点：模型与提示版本永远来自 provider，不在调用点各写一份。

        失败路径也必须带上它们 —— 「这个模型超时了几次」是分模型统计，
        缺了版本号，失败样本就没法归到任何模型名下。
        """
        return ModelSignal(
            outcome=outcome,
            candidate=candidate,
            model_version=self._provider.model_version,
            prompt_version=self._provider.prompt_version,
            latency_ms=latency_ms,
            reason=reason,
            detail=detail,
        )

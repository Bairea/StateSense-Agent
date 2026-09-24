"""模型调用的唯一入口。

**协议定在这里，实现不在这里。** 本轮只有离线实现（见文件末尾），
因为引入真实模型之前要先定清楚「发什么、收什么、失败长什么样」——
顺序反过来做的话，接口会跟着某个 SDK 的形状长出来，而那时已经来不及改了。

三条约定由实现方自己保证，`ShadowCollector` 只按它们翻译：

  1. **返回值一律按不可信处理。** 校验在 collector 里，provider 不负责判断
     自己给出的字符串是不是合法候选 —— 一个「自己校验自己」的 provider
     会把畸形输出静默吞掉，而那正是要观测的失败模式之一。
  2. **传输层超时必须抛 `TimeoutError`。** 这是唯一能把「超时」与「调用失败」
     分开的信号。collector 不能预先中断一个阻塞调用（见其模块文档），
     所以超时的执行责任在这里。
  3. **不得吞掉异常换成一个看起来正常的候选。** 失败要如实抛出来，
     否则「拒答率」「失败率」全是假的。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from statesense.shadow.models import INPUT_CONTRACT_VERSION, RawModelReply, StateShadowInput

#: 离线替身用的模型标识。**它不是模型**，报表会按这个字面前缀把它们标出来：
#: 一串漂亮的「分歧率 0%」若被当成模型效果好来读，就是拿夹具当真实数据。
OFFLINE_MODEL_VERSION = "offline-scripted"

#: 离线替身没有真实提示词，如实写明，不编一个版本号。
OFFLINE_PROMPT_VERSION = "offline-no-prompt"


class ShadowProvider(Protocol):
    """一次结构化问答。同步、单轮、无会话状态。"""

    @property
    def model_version(self) -> str:
        """模型标识。进 `shadow_signals.model_version`，报表按它分组。

        必须是**能区分具体模型与版本**的字符串（例如 `gpt-x-2026-05`）：
        写成 `"llm"` 之类的话，换模型之后新旧样本会混成一批，
        而阶段 3.5 要的正是「模型参与的 vs 纯规则的」可分离。
        """
        ...

    @property
    def prompt_version(self) -> str:
        """实际发出的提示词/指令版本。改了指令文本就必须改它。"""
        ...

    def query(self, payload: StateShadowInput) -> RawModelReply:
        """问一次。输入是白名单类型，实现方拿不到别的字段。

        传输层超时抛 `TimeoutError`；其余失败原样抛出。
        """
        ...


class ScriptedShadowProvider:
    """离线确定性替身：按调用次序给出预设答复。

    **它的存在只为一件事：让失败路径可重复。** 超时、畸形输出、调用失败这些
    在真实模型上很难按需复现，而它们恰恰是影子模式最需要被验证的部分 ——
    「模型答不上来时系统会不会假装它答了」只能靠这种替身反复验证。

    它**不是模型**：`model_version` 固定为 `OFFLINE_MODEL_VERSION`，报表会据此
    单独标出这些行。用它的结果去说「模型有多大增量价值」是不成立的。

    答复可以是一个异常实例 —— 抛出来即模拟该失败（`TimeoutError()` 走超时，
    其余走调用失败）。
    """

    def __init__(
        self,
        replies: Iterable[RawModelReply | BaseException],
        *,
        model_version: str = OFFLINE_MODEL_VERSION,
        prompt_version: str = OFFLINE_PROMPT_VERSION,
    ) -> None:
        self._replies = tuple(replies)
        self._index = 0
        self._model_version = model_version
        self._prompt_version = prompt_version
        #: 每次收到的输入。测试与回放靠它断言「送出去的确实是白名单字段」。
        self.calls: list[StateShadowInput] = []

    @property
    def model_version(self) -> str:
        return self._model_version

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    def query(self, payload: StateShadowInput) -> RawModelReply:
        self.calls.append(payload)
        if self._index >= len(self._replies):
            # 不重复最后一条，也不返回「不确定」兜底：预设答复用完意味着剧本
            # 少写了，静默续用会让剧本与断言脱节而看起来仍在通过。
            raise RuntimeError(
                f"预设答复已用尽（已调用 {self._index} 次）；"
                "请按实际轮次数补足 replies"
            )
        reply = self._replies[self._index]
        self._index += 1
        if isinstance(reply, BaseException):
            raise reply
        return reply


__all__ = [
    "OFFLINE_MODEL_VERSION",
    "OFFLINE_PROMPT_VERSION",
    "INPUT_CONTRACT_VERSION",
    "ScriptedShadowProvider",
    "ShadowProvider",
]

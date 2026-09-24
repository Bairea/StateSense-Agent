"""真实远端影子供应器：OpenAI 兼容的 `chat/completions`，零新依赖。

`shadow/provider.py` 的三条约定由本实现自证：

  1. **返回值按不可信处理**——本模块只把应答文本翻译成 `RawModelReply`，
     候选是否在枚举里由 collector 校验；自己校验自己会把畸形输出静默吞掉。
  2. **传输层超时抛 `TimeoutError`**——传输与错误语义全部委托 `LlmClient`
     （唯一实现，含包在 URLError 里的超时还原），本模块不重复一份。
  3. **失败原样抛出**——HTTP 4xx/5xx、应答结构损坏都如实抛给 collector 记成
     provider_error；「拒答率 / 失败率」的真实性靠这条。

解析策略的两层分工：

  · **应答结构**损坏 → `LlmClient` 抛错 → provider_error。端点没有按
    chat/completions 契约回答，是调用层的事。
  · **应答文本**不是合法 JSON、或 `candidate` 缺失/怪异 → 尽力取出一个
    候选字符串原样返回 → collector 的枚举校验把它记成 invalid_output。
    「模型答了但答得不成话」是影子要观测的失败模式，不能与「调用失败」混档。

安全：请求体只含 `payload()` 的白名单字段与提示词，没有标题 / URL / 正文；
密钥由 `LlmClient` 持有，不出现在本模块的任何异常消息里。
"""

from __future__ import annotations

import json

from statesense.llm import LlmClient
from statesense.shadow.models import RawModelReply, StateShadowInput

#: 提示词版本。改了下面这段话的任何字都必须动它——影子结果按它分组。
PROMPT_VERSION = "state-shadow-prompt@v1"

_SYSTEM_PROMPT = """你是一名屏幕使用状态的判定助手。根据一组聚合统计量，判断此刻的使用状态属于哪一档。

状态档从浅到深：
- NORMAL：正常，未进入被动消费；
- WATCH：有苗头，需要留意；
- PASSIVE_CONSUMPTION：已进入被动消费，该提醒了；
- HIGH_RISK_PASSIVE_CONSUMPTION：被动消费很深（「被困住」的量级）。

另有两档表示不下结论：uncertain（证据不足）、refused（拒答）。

统计量的含义（单位：分钟）：
- ent_minutes：娱乐类内容分钟数（含全屏推断的提权）；
- gray_minutes：灰色类（无法明确归类）分钟数；
- work_minutes：工作类分钟数；
- total_active_minutes：窗口内总活跃分钟数；
- window_minutes：回看窗口长度；
- late_night：是否处于凌晨时段（布尔）。
total 减去其余三类即「未命中任何清单」的分钟数。

只输出一个 JSON 对象，形如 {"candidate": "档名", "reason": "一句话理由"}，
其中 candidate 必须恰好是上面六档之一。不要输出 JSON 以外的任何文字。"""


def _strip_fences(text: str) -> str:
    """剥掉 Markdown 代码围栏。模型爱包 ```json```，剥完再解析。"""
    text = text.strip()
    if text.startswith("```"):
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -3]
    return text.strip()


def _try_json_object(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _parse_reply(content: str) -> RawModelReply:
    """应答文本 → `RawModelReply`。**解析越宽越好**：解析不出来的原文整个
    当候选交给 collector 的枚举校验——那里会记成 invalid_output 并把原文
    截断留档，这正是要观测的失败模式。"""
    text = _strip_fences(content)
    data = _try_json_object(text)
    if data is not None and data.get("candidate") is not None:
        candidate = str(data["candidate"]).strip()
        reason = data.get("reason")
        return RawModelReply(
            candidate=candidate,
            reason=str(reason).strip() if reason is not None else None,
        )
    return RawModelReply(candidate=text, reason=None)


class HttpShadowProvider:
    """发一次 chat/completions，把应答文本翻译成 `RawModelReply`。"""

    def __init__(
        self,
        url: str,
        model: str,
        api_key: str,
        *,
        timeout_seconds: float,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds 必须为正数，当前为 {timeout_seconds}")
        self._client = LlmClient(
            url, model, api_key, timeout_seconds=timeout_seconds
        )
        self._model = model

    @property
    def model_version(self) -> str:
        return self._model

    @property
    def prompt_version(self) -> str:
        return PROMPT_VERSION

    def query(self, payload: StateShadowInput) -> RawModelReply:
        user = json.dumps(payload.payload(), ensure_ascii=False, sort_keys=True)
        content = self._client.complete(_SYSTEM_PROMPT, user)
        return _parse_reply(content)

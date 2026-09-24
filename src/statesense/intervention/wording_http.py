"""真实远端文案供应器：与影子同一 `.env` 端点，输出**纯文本正文**。

与影子供应器共用 `LlmClient` 的传输与错误语义（结构损坏 → 抛错 → 适配层
回退模板）；解析纪律不同——文案的应答就是正文本身，这里只做去围栏、
去成对包裹引号的轻清理，空文本与超长交给适配层按失败处理。

安全：请求体只有 `context.payload()` 的白名单字段与提示词——没有窗口标题、
URL 或应用名（`top_label` 根本不在文案协议的参数里）；密钥由 `LlmClient`
持有，不出现在任何异常消息里。
"""

from __future__ import annotations

import json

from statesense.intervention.wording import WordingContext
from statesense.llm import LlmClient

#: 提示词版本。改了下面这段话的任何字都必须动它。
PROMPT_VERSION = "wording-prompt@v1"

_SYSTEM_PROMPT = """你是一名弹窗文案写手，为一条「提醒用户离开被动消费」的 Windows 前台弹窗写正文。

上下文（JSON）各字段含义：
- state：规则判定的状态档（NORMAL / WATCH / PASSIVE_CONSUMPTION / HIGH_RISK_PASSIVE_CONSUMPTION）；
- ent_minutes / gray_minutes / work_minutes / total_active_minutes / window_minutes：分钟数；
- ent_ratio：娱乐分钟占比（0 到 1）；
- top_category：占大头的类别（ENTERTAINMENT / GRAY / WORK / OTHER）；
- late_night：是否凌晨；
- action_id / action_text：建议动作，正文要把动作说出来；
- reminders_today：今天（不含本次）已经提醒过的次数；
- last_receipt：最近一次已结算的行为回执——continued = 用户恢复了，
  partial = 有所收敛，disengaged = 没理，no_data = 无法判断，null = 没有先例。

写作要求：
- 一到两句简体中文，直接可读；不要 markdown、不要引号、不要标题、不要复述字段名；
- 必须包含：娱乐分钟数与占比、建议动作；late_night 为真时先提一句凌晨；
- last_receipt 有先例时语气可以呼应（disengaged → 点出「和上次一样」；
  continued → 肯定「上次有效」），null 或 no_data 就平铺直叙；
- reminders_today 大于 0 时不要重复说教，一句话即可；
- 全文不超过 100 个字。

只输出正文本身，不要输出任何解释。"""


class HttpWordingProvider:
    """发一次 chat/completions，把应答清理成弹窗正文。**它实现 `RemoteWording`。**"""

    def __init__(
        self, url: str, model: str, api_key: str, *, timeout_seconds: float
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

    def render(self, context: WordingContext) -> str:
        user = json.dumps(context.payload(), ensure_ascii=False, sort_keys=True)
        return _clean(self._client.complete(_SYSTEM_PROMPT, user))


def _clean(text: str) -> str:
    """去围栏、去成对包裹引号、去首尾空白。空文本交给适配层按失败处理——
    「模型什么都没说」与「模型说了话」在适配层是两件事。"""
    text = text.strip()
    if text.startswith("```"):
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text

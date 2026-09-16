"""提醒文案。V0 用模板；将来接 LLM 只需替换这一个类，其他组件不用动。"""

from __future__ import annotations

from typing import Protocol

from statesense.config import Action
from statesense.state.models import State, StateVerdict

MAX_TITLE_CHARS = 28


class Wording(Protocol):
    def render(self, verdict: StateVerdict, action: Action, top_label: str | None) -> str: ...


def _shorten(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


class TemplateWording:
    """把数字写成一句人话。刻意保留具体分钟数与占比 —— 模糊的提醒没有说服力。"""

    def render(self, verdict: StateVerdict, action: Action, top_label: str | None) -> str:
        ent = int(round(verdict.ent_minutes))
        ratio = int(round(verdict.ent_ratio * 100))
        where = f"（{_shorten(top_label)}）" if top_label else ""

        parts: list[str] = []
        if verdict.late_night:
            parts.append("凌晨了。")
        parts.append(
            f"最近 {verdict.window_minutes} 分钟里有 {ent} 分钟在被娱乐内容占用{where}，占 {ratio}%。"
        )
        if verdict.state is State.HIGH_RISK_PASSIVE_CONSUMPTION:
            parts.append(f"这已经是「被困住」的量级了。{action.text}？")
        else:
            parts.append(f"{action.text}？")
        return "".join(parts)

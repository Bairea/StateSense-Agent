"""提醒文案。模板是基线；远端候选经适配层接入，隐私边界写进类型里。

两条协议各管一边：

  · `Wording`（本地协议）：`render(verdict, action, top_label)` ——
    top_label 是窗口标题，允许出现在**本机**弹窗里，但永不出本机；
  · `RemoteWording`（候选协议）：`render(WordingContext)` ——
    参数类型就是传输白名单，标题 / URL / 应用名在类型上不存在，
    远端实现想用也拿不到（与 shadow 的输入白名单同一思路：靠类型，不靠自觉）。

`RemoteWordingAdapter` 把候选接进本地协议：构造上下文（无 top_label）→
调用候选 → 失败或空文案时回退模板。**失败回退是硬要求**：Scheduler 的
tick 容错会把文案异常变成 tick_error，那一轮的真实投递就丢了——
「候选文案失败」的代价必须只是一句模板，不多、不少。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from statesense.config import Action
from statesense.state.models import State, StateVerdict
from statesense.state.taxonomy import Category

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


# ── 远端候选（阶段 3.3）：传输白名单、协议、替身与回退适配 ──────────

#: `WordingContext` 契约版本。字段集变化必须改它——候选文案按「看到什么」
#: 分组比较，字段变了还沿用同一版本号，等于把两套输入算成同一批样本。
WORDING_CONTEXT_VERSION = "wording-context@v1"

#: `WordingContext` 的字段清单，**顺序即契约**。镜像测试对着它逐字断言：
#: 加一个字段是「悄悄扩大了发送面」，必须显式改这里并同步 payload。
CONTEXT_FIELDS: tuple[str, ...] = (
    "state",
    "late_night",
    "ent_minutes",
    "gray_minutes",
    "work_minutes",
    "total_active_minutes",
    "window_minutes",
    "ent_ratio",
    "top_category",
    "action_id",
    "action_text",
)

#: **永不出本机**的字段。top_label（窗口标题）只存在于本地协议的参数里；
#: URL 与应用名只存在于 activity 层。候选文案的传输面在类型上碰不到它们。
WORDING_PRIVATE_FIELDS: tuple[str, ...] = ("top_label", "title", "url", "app")


@dataclass(frozen=True)
class WordingContext:
    """候选文案拿到的全部信息。**这个类型本身就是传输白名单。**

    与 shadow 输入白名单的两点刻意差异，各有一句理由：

      · `state` **在**这里——shadow 把它排除是因为模型要**预测**它；
        文案发生在判定之后，state 是文本要描述的事实，不给它反而写不准。
      · `ent_ratio` **在**这里——shadow 排除比值是不给判定模型递口径；
        文案的职责是把模板会显示的数字说清楚，比值是显示内容的一部分。
        它读自 verdict（classify 已算好），不构成第二处口径。

    `top_category` 是 top_label 的隐私安全替身（3.1 文档 W3）：文案可以说
    「娱乐内容占了大头」，说不了「你在看某个具体窗口」。
    """

    state: State
    late_night: bool
    ent_minutes: float
    gray_minutes: float
    work_minutes: float
    total_active_minutes: float
    window_minutes: int
    ent_ratio: float
    top_category: Category
    action_id: str
    action_text: str

    def payload(self) -> dict[str, object]:
        """构造传输载荷。**逐字段写，不用 asdict** —— 理由同 `StateShadowInput`：
        asdict 会让「加个字段」静默变成「默认发出去」；逐字段写出配合镜像测试，
        只改一半就会被拦住。"""
        return {
            "state": str(self.state),
            "late_night": self.late_night,
            "ent_minutes": self.ent_minutes,
            "gray_minutes": self.gray_minutes,
            "work_minutes": self.work_minutes,
            "total_active_minutes": self.total_active_minutes,
            "window_minutes": self.window_minutes,
            "ent_ratio": self.ent_ratio,
            "top_category": str(self.top_category),
            "action_id": self.action_id,
            "action_text": self.action_text,
        }


def _top_category(verdict: StateVerdict) -> Category:
    """哪一类分钟最多。并列时取更「重」的一类——弹窗正文已经按娱乐分钟
    说话了，类别口径与正文一致，不自造第二套。"""
    other = max(
        0.0,
        verdict.total_active_minutes
        - verdict.ent_minutes
        - verdict.gray_minutes
        - verdict.work_minutes,
    )
    ranked = (
        (Category.ENTERTAINMENT, verdict.ent_minutes),
        (Category.GRAY, verdict.gray_minutes),
        (Category.WORK, verdict.work_minutes),
        (Category.OTHER, other),
    )
    return max(ranked, key=lambda item: item[1])[0]


class RemoteWording(Protocol):
    """候选文案生成器：一次结构化问答，同步、单轮、无会话状态。"""

    def render(self, context: WordingContext) -> str:
        """按上下文给一句文案。失败**原样抛出**——回退是适配层的职责，
        实现方自己吞异常会把「模型答不上来」伪装成「模型说了话」。"""
        ...


class ScriptedRemoteWording:
    """离线确定性替身：按调用次序返回预设文案。**它不是模型。**

    与 `ScriptedShadowProvider` 同一套纪律：预设用尽即抛错（静默续用会让
    剧本与断言脱节，看起来仍在通过）；条目可以是异常实例（模拟失败）；
    每次收到的 context 记进 `calls`，供测试断言「送出去的确实是白名单」。
    """

    def __init__(self, replies: Iterable[str | BaseException]) -> None:
        self._replies = tuple(replies)
        self._index = 0
        #: 每次收到的上下文。测试与比较靠它断言传输面。
        self.calls: list[WordingContext] = []

    def render(self, context: WordingContext) -> str:
        self.calls.append(context)
        if self._index >= len(self._replies):
            raise RuntimeError(
                f"预设文案已用尽（已调用 {self._index} 次）；"
                "请按实际干预次数补足 replies"
            )
        reply = self._replies[self._index]
        self._index += 1
        if isinstance(reply, BaseException):
            raise reply
        return reply


class RemoteWordingAdapter:
    """把候选接进本地 `Wording` 协议的唯一适配层。**隐私边界在这里。**

    构造上下文时逐字段写出、不收 top_label——候选实现在参数类型上
    就没有标题可拿。候选失败或返回空文案时回退模板并计数：回退次数
    是「候选靠不靠得住」的第一手观测，藏起来就只剩神秘的成功率。
    """

    def __init__(self, remote: RemoteWording, fallback: Wording | None = None) -> None:
        self._remote = remote
        self._fallback = fallback if fallback is not None else TemplateWording()
        #: 候选失败（异常或空文案）后退回模板的次数。
        self.fallbacks = 0

    def render(self, verdict: StateVerdict, action: Action, top_label: str | None) -> str:
        context = WordingContext(
            state=verdict.state,
            late_night=verdict.late_night,
            ent_minutes=verdict.ent_minutes,
            gray_minutes=verdict.gray_minutes,
            work_minutes=verdict.work_minutes,
            total_active_minutes=verdict.total_active_minutes,
            window_minutes=verdict.window_minutes,
            ent_ratio=verdict.ent_ratio,
            top_category=_top_category(verdict),
            action_id=action.id,
            action_text=action.text,
        )
        try:
            text = self._remote.render(context)
        except Exception:  # noqa: BLE001 - 文案失败只能退回模板，不能让投递丢失
            text = ""
        if not text.strip():
            # 空文案弹一个空窗比失败更糟：按失败处理，回退模板。
            self.fallbacks += 1
            return self._fallback.render(verdict, action, top_label)
        return text

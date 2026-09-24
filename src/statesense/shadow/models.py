"""影子信号的契约：模型看到什么、能回答什么、失败长什么样。

三件事在这一个模块里定死，因为它们互为约束：

  · `StateShadowInput` —— **白名单**。送去模型的结构化输入，一个字段都不能多。
  · `ModelCandidate` —— **有限枚举**。模型只能在这几档里回答，「不确定」与
    「拒答」是明确的档，不是可以省略的边界情况。
  · `ShadowOutcome` —— **调用结局**。超时、畸形输出、调用失败各有各的档，
    绝不塌缩成一个「没有候选」——「模型判为正常」与「模型没答上来」是
    两件完全不同的事，混在一起的话，影子对比的分母就是假的。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from statesense.state.models import SEVERITY_ORDER, State

# ── 输入白名单 ──────────────────────────────────────────────

#: 输入契约的版本。改动 `StateShadowInput` 的字段集就必须动这一行 ——
#: 影子结果按它分组比较，字段变了却沿用同一个版本号，等于把两套输入
#: 算成同一批样本。
INPUT_CONTRACT_VERSION = "state-shadow-input@v1"

#: 允许离开本机的字段，**顺序即契约**。`StateShadowInput` 的字段必须与它逐字相同
#: （`tests/test_shadow_contract.py` 对着它镜像断言）。
#:
#: 为什么要有这个常量：字段集是「模型实际看到了什么」的唯一答案，而阶段 3.2 的
#: 完成条件是评审者能把它列出来。让 dataclass 自己当契约，就没法在一处读全 ——
#: 加一个字段是「悄悄扩大了发送面」，而这件事必须显式。
#:
#: **只发原子量，不发规则算出来的比值与汇总。** 这条原则有两层理由：
#:   · 信息上：`ent / total` 由模型自己除即可，发过去只是方便，不增加信息；
#:   · 结构上：在 Scheduler 里重算一次比值或合计，就等于把 `classify` 的口径
#:     复制到了第二个地方 —— 而那正是本仓库反复出过问题的写法
#:     （见 `effective_entertainment_minutes` 的模块说明）。留下的这几个字段
#:     要么是快照的直接读值，要么走唯一实现（`effective_entertainment_minutes`
#:     / `is_late_night`）。
ALLOWED_FIELDS: tuple[str, ...] = (
    "ent_minutes",
    "gray_minutes",
    "work_minutes",
    "total_active_minutes",
    "window_minutes",
    "late_night",
)

#: **永不出本机**的字段。它们只存在于 `activity` 与未来 `wording` 的路径里。
#:
#: 屏幕文本是不可信证据（`AGENTS.md`），窗口标题与 URL 可能含私人内容 ——
#: 两者都不进这个白名单。`ActivityReader` 仍是唯一知道 Screenpipe 的模块，
#: 这一层不新增任何采集能力，只是从已经算好的聚合量里取数。
PRIVATE_FIELDS: tuple[str, ...] = (
    "top_label",
    "title",
    "app",
    "url",
    "screen_text",
)

#: 属于「答案」而刻意不作为输入的字段：模型要预测的正是它。
#: 把它塞进输入，影子对比会退化成「抄一遍规则」，无论模型好坏都近乎 100% 一致 ——
#: 一个必然通过、却什么也没证明的验证。
LEAKY_FIELDS: tuple[str, ...] = ("state",)

#: 刻意留在 v1 之外的既有信号，及原因。写在这里是为了让「以后要不要加」
#: 是个可以讨论的决定，而不是一次悄悄扩面。
#:
#: 全屏原始取值（`fullscreen_state`）不在里面：它已经通过 `ent_minutes` 的
#: 提权规则进入了输入，单独再给一个 Windows 枚举值，只会让模型有机会
#: 过拟合一个平台细节 —— 且 `is_gaming` 的映射属于规则知识，不该由输入泄露。
#: 闸门结果与动作 ID 也不在里面：闸门是「该不该打扰」的规则，不是「我在什么状态」
#: 的证据；把它们给模型，等于让模型去猜闸门想不想让它说话。
#:
#: `ent_ratio` 与 `entries_minutes` 同样不在里面，原因不同：它们是规则算出的
#: 比值与汇总（`ent / total`、条目分钟数之和），信息上可由已有字段导出，
#: 结构上则会把 `classify` 的口径复制到第二个地方。
DEFERRED_FIELDS: tuple[str, ...] = (
    "ent_ratio",
    "entries_minutes",
    "fullscreen_state",
    "gate_trace",
    "action_id",
)


@dataclass(frozen=True)
class StateShadowInput:
    """送去模型的结构化输入。**这个类型本身就是白名单。**

    全部是已经算好的原子量，没有一个原始文本字段。构造点只有 `Scheduler` 一处，
    且必须在 `classify` 之前 —— 顺序不只是形式：一旦在判定之后构造，
    `verdict.state` 就在手边，而「顺手带上它」会让整个对比失去意义。
    """

    ent_minutes: float
    gray_minutes: float
    work_minutes: float
    total_active_minutes: float
    window_minutes: int
    late_night: bool

    def payload(self) -> dict[str, object]:
        """构造传输载荷。**逐字段写，不用 `asdict`。**

        `asdict` 会自动带上将来新增的每一个字段 —— 于是「给 dataclass 加个字段」
        就等于「默认发给远端」，白名单会随着重构慢慢失效，而且失效时没有任何
        报错。逐字段写出的话，新增字段必须同时改这里和 `ALLOWED_FIELDS`，
        而镜像测试会拦住只改一半的情况。
        """
        return {
            "ent_minutes": self.ent_minutes,
            "gray_minutes": self.gray_minutes,
            "work_minutes": self.work_minutes,
            "total_active_minutes": self.total_active_minutes,
            "window_minutes": self.window_minutes,
            "late_night": self.late_night,
        }


# ── 输出枚举 ────────────────────────────────────────────────


class ModelCandidate(StrEnum):
    """模型能给出的全部回答。

    前四项与 `State` 同字面量 —— 模型给的就是「状态候选」，另造一套拼法会让
    影子对比多一层无意义的翻译。后两项是**明确指出「不下结论」**：

      · `UNCERTAIN` —— 模型觉得证据不够。它**不是** NORMAL：读成正常会把
        「没下结论」记成「认为没问题」，而这正是本版本要消灭的那类二义。
      · `REFUSED` —— 模型拒答。拒答率是阶段 3.4 点名要看的指标之一，
        因此必须有自己的一档，不能并进 UNCERTAIN。
    """

    NORMAL = "NORMAL"
    WATCH = "WATCH"
    PASSIVE_CONSUMPTION = "PASSIVE_CONSUMPTION"
    HIGH_RISK_PASSIVE_CONSUMPTION = "HIGH_RISK_PASSIVE_CONSUMPTION"
    UNCERTAIN = "uncertain"
    REFUSED = "refused"

    @property
    def state(self) -> State | None:
        """状态候选对应的 `State`；不下结论的两档给 `None`。

        取 `None` 的调用方必须显式处理它 —— 想把它当 NORMAL 用，得自己写
        `or State.NORMAL`，那一步是看得见的。
        """
        for state in SEVERITY_ORDER:
            if state.value == self.value:
                return state
        return None


class ShadowOutcome(StrEnum):
    """一次影子调用的结局。封闭枚举，没有「其他」。

    `OK` 是唯一「模型给了候选」的档 —— 其余四档都不下任何模型结论。
    这正是计划里「超时、非法输出或证据不足时本轮不新增干预」的落点：
    它们连一个候选都产生不出来，自然也谈不上影响后续。
    """

    OK = "ok"
    #: 传输层超时，或返回时已超过 `shadow.timeout_seconds`。
    TIMEOUT = "timeout"
    #: 返回值不在 `ModelCandidate` 里，或与结局自相矛盾。
    INVALID_OUTPUT = "invalid_output"
    #: 调用本身失败：网络、鉴权、限流、实现抛错。
    PROVIDER_ERROR = "provider_error"
    #: 输入不可信（`data_status != ok`），**根本不该去问**。没调用就没有延迟。
    NO_DATA = "no_data"


@dataclass(frozen=True)
class RawModelReply:
    """模型原样返回的东西。**每一个字段都不可信。**

    存在的意义是把「不可信」写进类型里：校验发生在 `collector`，
    而 `collector` 只认这个类型，不认识任何具体实现 —— 于是一个自己造
    `ModelSignal` 的 provider 是写不出来的。
    """

    candidate: str
    reason: str | None = None


@dataclass(frozen=True)
class ModelSignal:
    """校验后的影子信号。**这是唯一会被落库的形态。**

    不变式（`__post_init__` 强制）：

      · `outcome is OK` 与「有候选」互为充要条件；
      · `latency_ms is None` 与 `outcome is NO_DATA` 互为充要条件 —— 没调用
        就没有耗时，写 0.0 会让「一次几乎瞬时的成功调用」与「根本没调用」
        在报表里同值。

    `reason` / `detail` 是模型给的理由与失败详情。它们**只写不读**：
    没有任何代码路径把这两列当指令、当判定输入或当配置。模型输出与屏幕文本
    同属不可信证据，落库只为事后复盘。
    """

    outcome: ShadowOutcome
    candidate: ModelCandidate | None
    #: 模型标识与提示/输入契约版本。阶段 3.5 要求 `--report` 能把「模型参与的」
    #: 与「纯规则的」分开，靠的就是它。
    model_version: str
    prompt_version: str
    latency_ms: float | None
    reason: str | None
    detail: str | None

    def __post_init__(self) -> None:
        gave_candidate = self.candidate is not None
        if (self.outcome is ShadowOutcome.OK) != gave_candidate:
            raise ValueError(
                f"outcome={self.outcome} 与 candidate={self.candidate} 自相矛盾："
                "只有 ok 才带候选，其余结局一律不带"
            )
        if (self.latency_ms is None) != (self.outcome is ShadowOutcome.NO_DATA):
            raise ValueError(
                f"outcome={self.outcome} 的 latency_ms={self.latency_ms} 不合法："
                "只有「没调用」才没有耗时"
            )
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError(f"latency_ms 不能为负：{self.latency_ms}")

"""配置加载。启动即校验，任何非法项都在进程启动时炸掉，不拖到运行中。"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


class ConfigError(Exception):
    """配置非法。"""


#: 闸门注册表里允许出现的名字。
#: 配置中出现未知名字必须**启动失败**：拼错一个名字会静默少跑一条闸门，
#: 若少掉的是 state_min，系统就会对并不处于被动消费状态的人弹窗。
KNOWN_GATES: tuple[str, ...] = ("state_min", "ratio_min", "cooldown", "daily_cap")

#: 必须始终启用的闸门。state_min 是「只在真的被困住时才打扰」这条安全属性的唯一守卫。
MANDATORY_GATES: tuple[str, ...] = ("state_min",)


@dataclass(frozen=True)
class ScreenpipeConfig:
    base_url: str = "http://localhost:3030"
    api_key_env: str = "SCREENPIPE_LOCAL_API_KEY"
    request_timeout_sec: float = 10.0


@dataclass(frozen=True)
class ScheduleConfig:
    tick_seconds: int = 60
    evaluate_every_minutes: int = 5
    window_minutes: int = 60


@dataclass(frozen=True)
class ThresholdConfig:
    watch_minutes: float = 20
    passive_minutes: float = 40
    #: 必须 <= schedule.window_minutes，否则该状态永远不可达（load_config 会拒绝）。
    high_risk_minutes: float = 55
    late_night_start_hour: int = 1
    late_night_end_hour: int = 6
    late_night_min_active_minutes: float = 10


@dataclass(frozen=True)
class GateConfig:
    enabled: tuple[str, ...] = ("state_min", "ratio_min", "cooldown", "daily_cap")
    ratio_min: float | None = None
    cooldown_minutes: float = 30
    daily_cap: int = 8

    def required_ratio_min(self) -> float:
        """取比例阈值。

        配置层已保证「enabled 含 ratio_min 时它必非 None」，这里再抛一次是为了
        不提供静默兜底 —— 一个悄悄降级成 0 的闸门比一条报错危险得多。
        """
        if self.ratio_min is None:
            raise ConfigError("gate.ratio_min 未设置；该项为必填，不接受默认值")
        return self.ratio_min


@dataclass(frozen=True)
class OutcomeConfig:
    delay_minutes: float = 10
    disengaged_ratio: float = 0.5
    continued_ratio: float = 0.8


@dataclass(frozen=True)
class NotifyConfig:
    channel: str = "foreground_popup"
    #: 用户完全不理会时，多久自动关掉弹窗（秒）。避免它永远挡住后续干预。
    answer_timeout_seconds: float = 180.0
    #: 轮询对话框句柄、强行抢前台的最长等待（秒）。
    foreground_timeout_seconds: float = 15.0


@dataclass(frozen=True)
class ReportConfig:
    """观测层的判据阈值。

    gap_threshold_minutes 同时用于两个语义不同、阈值相同的判定：
    回看历史时「相邻评估的间隔」，与判断当下时「最后一条评估距今」。
    合成一个键是有意的 —— 分两个键会让配置项翻倍，而它们本来就是一回事。
    """

    gap_threshold_minutes: float = 15
    #: 漏判锚点：窗口活跃至少这么多分钟，才值得怀疑规则漏掉了什么。
    leak_min_active_minutes: float = 30
    #: 漏判锚点：未归类占比至少这么高。0.7 是起点，用真实分布再调。
    leak_min_unclassified_ratio: float = 0.7
    #: 二级漏判视图最多列几条明细。
    leak_top_n: int = 10


@dataclass(frozen=True)
class TaxonomyConfig:
    entertainment: tuple[re.Pattern[str], ...] = ()
    gray: tuple[re.Pattern[str], ...] = ()
    work: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True)
class Action:
    id: str
    text: str
    applies_to: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    screenpipe: ScreenpipeConfig
    schedule: ScheduleConfig
    thresholds: ThresholdConfig
    gate: GateConfig
    outcome: OutcomeConfig
    notify: NotifyConfig
    store_path: Path
    report: ReportConfig
    taxonomy: TaxonomyConfig
    actions: tuple[Action, ...] = field(default_factory=tuple)


def _section(cls: type[T], raw: dict[str, Any], name: str) -> T:
    """按节构造配置 dataclass，把「键名拼错」翻译成启动期报错。

    不做这一步的话，`[screenpipe] base_uri = "..."` 会在构造 dataclass 时抛
    `TypeError` —— 它不是 `ConfigError`，`main` 接不住，用户看到的是 traceback
    加退出码 1，与「配置非法」应有的干净报错（退出码 2）完全不同。

    这与 BOM 那次是同一类缺陷：错误本身没错，错在它没有被翻译成用户能看懂的形式。
    """
    try:
        return cls(**raw.get(name, {}))
    except TypeError as exc:
        raise ConfigError(f"[{name}] 节的键有问题：{exc}") from exc


def _compile(patterns: list[str], where: str) -> tuple[re.Pattern[str], ...]:
    compiled = []
    for raw in patterns:
        try:
            compiled.append(re.compile(raw, re.IGNORECASE))
        except re.error as exc:
            raise ConfigError(f"{where} 里的正则非法: {raw!r} ({exc})") from exc
    return tuple(compiled)


def load_config(path: Path) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")

    raw_bytes = path.read_bytes()
    # Windows 记事本默认写 UTF-8 BOM。tomllib 会因此报
    # "Invalid statement (at line 1, column 1)" —— 位置指向文件开头，
    # 用户完全看不出原因。显式剥掉。
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raw_bytes = raw_bytes[3:]
    try:
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        # 必须包成 ConfigError：main 只接这一种，否则用户看到的是 traceback 加退出码 1。
        raise ConfigError(f"配置文件解析失败：{path}（{exc}）") from exc

    screenpipe = _section(ScreenpipeConfig, raw, "screenpipe")
    # spec §12/§15.1：base_url 可由 SCREENPIPE_LOCAL_API_URL 覆盖。
    # 上游明确存在 fallback port 与开发态实例，写死 3030 会打到另一个实例。
    env_base_url = os.environ.get("SCREENPIPE_LOCAL_API_URL", "").strip()
    if env_base_url:
        screenpipe = replace(screenpipe, base_url=env_base_url)

    schedule = _section(ScheduleConfig, raw, "schedule")
    if schedule.window_minutes <= 0 or schedule.evaluate_every_minutes <= 0:
        raise ConfigError("schedule.window_minutes 与 evaluate_every_minutes 必须为正数")

    thresholds = _section(ThresholdConfig, raw, "thresholds")
    if not (thresholds.watch_minutes <= thresholds.passive_minutes <= thresholds.high_risk_minutes):
        raise ConfigError("thresholds 必须满足 watch <= passive <= high_risk")
    # ent 是**窗口内**条目分钟数之和，其上界就是 window_minutes。阈值超过窗口
    # 意味着这个状态永远不可达 —— 它专属的动作永远不会被选中，而配置看起来
    # 一切正常，剧本也只能悄悄少断言一档。这类"能写但永远不生效"的值必须
    # 在启动时就被拒绝（与 gate.enabled 里拼错闸门名同一原则）。
    if thresholds.high_risk_minutes > schedule.window_minutes:
        raise ConfigError(
            f"thresholds.high_risk_minutes={thresholds.high_risk_minutes:g} 超过 "
            f"schedule.window_minutes={schedule.window_minutes:g}：ent 是窗口内条目"
            "分钟数之和，不可能超过窗口长度，该状态因此永远不可达。"
            "要么把 high_risk_minutes 降到窗口以内，要么加长 window_minutes。"
        )

    gate_raw = dict(raw.get("gate", {}))
    if "enabled" in gate_raw:
        gate_raw["enabled"] = tuple(gate_raw["enabled"])
    gate = _section(GateConfig, {"gate": gate_raw}, "gate")
    # 闸门名必须逐一可识别 —— 静默丢弃未知名字会让安全属性无声失效。
    if not gate.enabled:
        raise ConfigError("gate.enabled 不能为空；至少要启用 state_min")
    unknown = [name for name in gate.enabled if name not in KNOWN_GATES]
    if unknown:
        raise ConfigError(
            f"gate.enabled 含未知闸门名 {unknown}；已知闸门：{list(KNOWN_GATES)}"
        )
    missing = [name for name in MANDATORY_GATES if name not in gate.enabled]
    if missing:
        raise ConfigError(
            f"gate.enabled 必须包含 {missing}。state_min 是唯一阻止在"
            "非被动消费状态下打扰用户的闸门，不允许关闭。"
        )
    # ratio_min 是必填项。缺失时绝不静默降级为 0 —— 静默失效的闸门比报错危险得多。
    if "ratio_min" in gate.enabled:
        if gate.ratio_min is None:
            raise ConfigError("gate.ratio_min 未设置；该项为必填，不接受默认值")
        if not (0.0 < gate.ratio_min <= 1.0):
            raise ConfigError(f"gate.ratio_min 必须在 (0, 1] 区间内，当前为 {gate.ratio_min}")

    outcome = _section(OutcomeConfig, raw, "outcome")
    notify = _section(NotifyConfig, raw, "notify")

    store_raw = raw.get("store", {})
    store_path = (path.parent / store_raw.get("path", "statesense.db")).resolve()

    report = _section(ReportConfig, raw, "report")
    if report.gap_threshold_minutes <= 0:
        raise ConfigError("report.gap_threshold_minutes 必须为正数")
    if report.leak_min_active_minutes < 0:
        raise ConfigError("report.leak_min_active_minutes 不能为负")
    if not (0.0 < report.leak_min_unclassified_ratio <= 1.0):
        raise ConfigError(
            f"report.leak_min_unclassified_ratio 必须在 (0, 1] 区间内，"
            f"当前为 {report.leak_min_unclassified_ratio}"
        )
    if report.leak_top_n <= 0:
        raise ConfigError("report.leak_top_n 必须为正数")

    tax_raw = raw.get("taxonomy", {})
    taxonomy = TaxonomyConfig(
        entertainment=_compile(tax_raw.get("entertainment", []), "taxonomy.entertainment"),
        gray=_compile(tax_raw.get("gray", []), "taxonomy.gray"),
        work=_compile(tax_raw.get("work", []), "taxonomy.work"),
    )
    if not taxonomy.entertainment:
        raise ConfigError("taxonomy.entertainment 不能为空，否则无法识别被动消费")

    actions_raw = raw.get("actions", [])
    if not actions_raw:
        raise ConfigError("actions 动作池不能为空")
    actions: list[Action] = []
    seen: set[str] = set()
    for item in actions_raw:
        if not item.get("id") or not item.get("text"):
            raise ConfigError("每个 action 必须有 id 与 text")
        if item["id"] in seen:
            raise ConfigError(f"action id 重复: {item['id']}")
        seen.add(item["id"])
        actions.append(
            Action(id=item["id"], text=item["text"], applies_to=tuple(item.get("applies_to", ())))
        )

    return Config(
        screenpipe=screenpipe,
        schedule=schedule,
        thresholds=thresholds,
        gate=gate,
        outcome=outcome,
        notify=notify,
        store_path=store_path,
        report=report,
        taxonomy=taxonomy,
        actions=tuple(actions),
    )

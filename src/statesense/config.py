"""配置加载。启动即校验，任何非法项都在进程启动时炸掉，不拖到运行中。"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path


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
    high_risk_minutes: float = 65
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
    taxonomy: TaxonomyConfig
    actions: tuple[Action, ...] = field(default_factory=tuple)


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

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    screenpipe = ScreenpipeConfig(**raw.get("screenpipe", {}))

    schedule = ScheduleConfig(**raw.get("schedule", {}))
    if schedule.window_minutes <= 0 or schedule.evaluate_every_minutes <= 0:
        raise ConfigError("schedule.window_minutes 与 evaluate_every_minutes 必须为正数")

    thresholds = ThresholdConfig(**raw.get("thresholds", {}))
    if not (thresholds.watch_minutes <= thresholds.passive_minutes <= thresholds.high_risk_minutes):
        raise ConfigError("thresholds 必须满足 watch <= passive <= high_risk")

    gate_raw = dict(raw.get("gate", {}))
    if "enabled" in gate_raw:
        gate_raw["enabled"] = tuple(gate_raw["enabled"])
    gate = GateConfig(**gate_raw)
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

    outcome = OutcomeConfig(**raw.get("outcome", {}))
    notify = NotifyConfig(**raw.get("notify", {}))

    store_raw = raw.get("store", {})
    store_path = (path.parent / store_raw.get("path", "statesense.db")).resolve()

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
        taxonomy=taxonomy,
        actions=tuple(actions),
    )

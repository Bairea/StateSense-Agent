"""驱动真实 Scheduler 的回放运行器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from statesense.activity.models import ActivitySnapshot
from statesense.clock import FrozenClock
from statesense.config import Config
from statesense.intervention.wording import Wording
from statesense.notify.base import RecordingNotifier
from statesense.replay.scenario import Scenario
from statesense.replay.synthesize import snapshot_at
from statesense.scheduler import Scheduler, TickReport
from statesense.shadow.collector import ShadowCollector
from statesense.shadow.provider import ShadowProvider
from statesense.store.db import Store


class ScriptedReader:
    """满足 ActivitySource 的合成数据源。"""

    def __init__(self, scenario: Scenario, origin: datetime) -> None:
        self._scenario = scenario
        self._origin = origin
        #: 记录每次调用的窗口，便于断言「回执确实回查了前后两段」。
        self.calls: list[tuple[datetime, datetime]] = []

    def read(
        self,
        start: datetime,
        end: datetime,
        window_minutes: int,
        captured_at: datetime,
    ) -> ActivitySnapshot:
        self.calls.append((start, end))
        return snapshot_at(
            self._scenario, start, end, window_minutes, captured_at, self._origin
        )


class ScriptedFullscreenProbe:
    """剧本给定的全屏信号。

    **回放绝不能去读真实的全屏状态。** `Scheduler` 在不注入探针时会退到
    `default_probe()`，在 Windows 上那是一次真实的系统调用 —— 于是
    「可重复的验证工具」会随「跑回放的这一刻我是不是正开着游戏」给出不同结果。
    `--replay all` 曾经因此不是可复现的：它验证的是生产链路，却偷偷带上了一个
    生产环境才有的输入。

    剧本默认 `fullscreen_state=None` = 无法判定，与生产上「探针取不到值」
    是同一个语义，因此不会平白提高任何一轮的 ent。
    """

    def __init__(self, state: int | None) -> None:
        self._state = state

    def state(self) -> int | None:
        return self._state


@dataclass
class ReplayRun:
    """一次回放的句柄。剧本用它推进虚拟时间并跑真实调度器。"""

    store: Store
    scheduler: Scheduler
    clock: FrozenClock

    def tick(self, minutes: float = 0.0) -> TickReport | None:
        """推进虚拟时间后跑一轮。

        走 `run_tick_guarded` 而不是 `run_once` —— 常驻进程跑的就是这条路径，
        回放要验的是它。
        """
        if minutes:
            self.clock.advance(minutes=minutes)
        return self.scheduler.run_tick_guarded(self.clock.now())

    def evaluations(self) -> list:
        return self.store.list_evaluations()

    def close(self) -> None:
        self.store.close()


def run_scenario(
    scenario: Scenario,
    config: Config,
    *,
    start: datetime,
    db_path: Path | None = None,
    shadow: ShadowProvider | None = None,
    wording: Wording | None = None,
) -> ReplayRun:
    """用 FrozenClock + 脚本 reader 驱动**真实的 Scheduler**。

    不重跑判定逻辑 —— 被验证的必须是生产链路本身。但链路里的每一个外部输入
    （时间、活动数据、全屏信号）都必须由剧本给定，不能漏一个去读真实环境。

    `shadow` 与全屏探针同理：**回放绝不能去问一个真实模型。** 它既不可重复，
    又会让「跑回放」变成一次真实的对外调用。传 `None` 即影子关闭 —— 这与生产的
    默认状态一致，因此「影子关闭时投递与从前完全一样」这件事在回放里也能验。

    `wording` 同理只接受本地实现（模板或带模板回退的适配器）：文案是弹窗正文，
    交给真实远端等于回放期间对外发请求。`None` 即默认模板。
    """
    store = Store(db_path or config.store_path)
    store.migrate()
    clock = FrozenClock(start)
    reader = ScriptedReader(scenario, start)
    collector = (
        None
        if shadow is None
        else ShadowCollector(shadow, timeout_seconds=config.shadow.timeout_seconds)
    )
    scheduler = Scheduler(
        config=config,
        clock=clock,
        reader=reader,
        store=store,
        notifier=RecordingNotifier(clock=clock),
        fullscreen=ScriptedFullscreenProbe(scenario.fullscreen_state),
        shadow=collector,
        wording=wording,
    )
    return ReplayRun(store=store, scheduler=scheduler, clock=clock)

"""驱动真实 Scheduler 的回放运行器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from statesense.activity.models import ActivitySnapshot
from statesense.clock import FrozenClock
from statesense.config import Config
from statesense.notify.base import RecordingNotifier
from statesense.replay.scenario import Scenario
from statesense.replay.synthesize import snapshot_at
from statesense.scheduler import Scheduler, TickReport
from statesense.store.db import Store


class ScriptedReader:
    """满足 ActivitySource 的合成数据源。"""

    def __init__(self, scenario: Scenario, window_minutes: int, origin: datetime) -> None:
        self._scenario = scenario
        self._window = window_minutes
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
) -> ReplayRun:
    """用 FrozenClock + 脚本 reader 驱动**真实的 Scheduler**。

    不重跑判定逻辑 —— 被验证的必须是生产链路本身。
    """
    store = Store(db_path or config.store_path)
    store.migrate()
    clock = FrozenClock(start)
    reader = ScriptedReader(scenario, config.schedule.window_minutes, start)
    scheduler = Scheduler(
        config=config,
        clock=clock,
        reader=reader,
        store=store,
        notifier=RecordingNotifier(clock=clock),
    )
    return ReplayRun(store=store, scheduler=scheduler, clock=clock)

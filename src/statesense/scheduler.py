"""tick 主循环。一次 tick 的顺序：先补回执，再评估，最后（可能）投递。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from statesense.activity.models import ActivitySnapshot
from statesense.activity.reader import ActivityReader
from statesense.clock import Clock
from statesense.config import Config
from statesense.intervention.actions import candidates
from statesense.intervention.decider import decide
from statesense.intervention.gates import GateContext
from statesense.intervention.wording import TemplateWording, Wording
from statesense.notify.base import Notifier
from statesense.outcome.tracker import evaluate as evaluate_outcome
from statesense.state.engine import classify
from statesense.state.taxonomy import Category, bucket_minutes
from statesense.store.db import Store

log = logging.getLogger(__name__)

ACTION_CURSOR_KEY = "action_cursor"


@dataclass(frozen=True)
class TickReport:
    evaluation_id: int | None
    state: str | None
    intervened: bool
    outcomes_closed: int
    note: str


class Scheduler:
    def __init__(
        self,
        config: Config,
        clock: Clock,
        reader: ActivityReader,
        store: Store,
        notifier: Notifier,
        wording: Wording | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._reader = reader
        self._store = store
        self._notifier = notifier
        self._wording = wording or TemplateWording()

    # ── 对外 ────────────────────────────────────────────────

    def ent_minutes(self, snapshot: ActivitySnapshot) -> float:
        """被动消费分钟数。状态判定与回执比较必须用同一个口径。"""
        buckets = bucket_minutes(snapshot.entries, self._config.taxonomy)
        return round(buckets[Category.ENTERTAINMENT], 2)

    def run_once(self) -> TickReport:
        now = self._clock.now()
        closed = self._close_due_outcomes(now)
        return self._evaluate_tick(now, closed)

    def run_forever(self) -> None:  # pragma: no cover - 常驻路径靠手动验证
        interval = timedelta(minutes=self._config.schedule.evaluate_every_minutes)
        last: datetime | None = None
        while True:
            now = self._clock.now()
            if last is None or now - last >= interval:
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001 - 常驻进程不能因为单轮失败就退出
                    log.exception("本轮评估失败，跳过")
                last = now
            time.sleep(self._config.schedule.tick_seconds)

    # ── 内部 ────────────────────────────────────────────────

    def _close_due_outcomes(self, now: datetime) -> int:
        delay = self._config.outcome.delay_minutes
        window = int(delay)
        closed = 0
        for due in self._store.due_interventions(now):
            before = self._reader.read(due.at - timedelta(minutes=delay), due.at, window, now)
            after = self._reader.read(
                due.at, due.at + timedelta(minutes=delay), window, now
            )
            verdict = evaluate_outcome(before, after, self.ent_minutes, self._config.outcome)
            self._store.insert_outcome(due.id, now, verdict)
            closed += 1
        return closed

    def _evaluate_tick(self, now: datetime, closed: int) -> TickReport:
        window = self._config.schedule.window_minutes
        snapshot = self._reader.read(now - timedelta(minutes=window), now, window, now)
        verdict = classify(snapshot, self._config.taxonomy, self._config.thresholds)

        day_start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        ctx = GateContext(
            verdict=verdict,
            config=self._config.gate,
            now=now,
            last_intervention_at=self._store.last_intervention_at(),
            interventions_today=self._store.intervention_count_since(day_start),
        )
        last_action = self._store.get_kv(ACTION_CURSOR_KEY)
        pool = candidates(self._config.actions, verdict.state)
        decision = decide(ctx, pool, last_action)

        evaluation_id = self._store.insert_evaluation(now, verdict, decision)

        if not decision.intervene or decision.action_id is None:
            return TickReport(evaluation_id, str(verdict.state), False, closed, decision.reason)

        action = next(a for a in pool if a.id == decision.action_id)
        top_label = (
            max(snapshot.entries, key=lambda e: e.minutes).title if snapshot.entries else None
        )
        body = self._wording.render(verdict, action, top_label)
        result = self._notifier.notify("StateSense 轻推", body)

        self._store.insert_intervention(
            evaluation_id=evaluation_id,
            at=now,
            state=str(verdict.state),
            late_night=verdict.late_night,
            action_id=action.id,
            action_text=body,
            delivery_status=result.stored_status,
            outcome_due_at=now + timedelta(minutes=self._config.outcome.delay_minutes),
            user_response=result.user_response,
        )
        self._store.set_kv(ACTION_CURSOR_KEY, action.id)

        note = result.stored_status
        if result.user_response:
            note = f"{note} response={result.user_response}"
        return TickReport(evaluation_id, str(verdict.state), True, closed, note)

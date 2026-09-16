"""tick 主循环。一次 tick 的顺序：先补回执，再评估，最后（可能）投递。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from statesense.activity.base import ActivitySource
from statesense.activity.models import ActivitySnapshot
from statesense.clock import Clock
from statesense.config import Config
from statesense.intervention.actions import candidates
from statesense.intervention.decider import decide
from statesense.intervention.gates import GateContext
from statesense.intervention.wording import TemplateWording, Wording
from statesense.notify.base import Notifier
from statesense.outcome.tracker import evaluate as evaluate_outcome
from statesense.state.engine import classify
from statesense.state.taxonomy import entertainment_minutes
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
        reader: ActivitySource,
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
        self._last_evaluation_at: datetime | None = None

    # ── 对外 ────────────────────────────────────────────────

    def ent_minutes(self, snapshot: ActivitySnapshot) -> float:
        """被动消费分钟数。委托给 taxonomy —— 与状态判定共用同一口径，不另算一份。"""
        return entertainment_minutes(snapshot.entries, self._config.taxonomy)

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
                self.run_tick_guarded(now)
                last = now
            time.sleep(self._config.schedule.tick_seconds)

    def run_tick_guarded(self, now: datetime) -> TickReport | None:
        """常驻循环的单轮：异常必须落库，不能只留在日志里。

        原本 `except Exception` 只写日志，于是「崩在日志里」与「进程根本没在跑」
        在库里长得一模一样。补上落行之后，「无记录」只剩「进程死了」一个解释。

        写事件本身若也失败（磁盘满、库损坏），只能退回日志 —— 此时观测退化为
        「看起来像进程死了」，这是已知边界，不是新问题。
        """
        try:
            return self.run_once()
        except Exception as exc:  # noqa: BLE001 - 常驻进程不能因为单轮失败就退出
            log.exception("本轮评估失败，跳过")
            try:
                self._store.insert_run_event(
                    now, "tick_error", f"{type(exc).__name__}: {exc}"[:200]
                )
            except Exception:  # noqa: BLE001
                log.exception("写入 tick_error 运行事件失败")
            return None

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

        # 休眠/唤醒检测：醒来后第一轮拿到的窗口会横跨一段根本没采集的时间，
        # 直接用会得出「这段时间几乎没活动」的假结论。
        gap_limit = timedelta(minutes=window * 2)
        if self._last_evaluation_at is not None:
            gap = now - self._last_evaluation_at
            if gap > gap_limit:
                self._last_evaluation_at = now
                log.warning(
                    "检测到 %.0f 分钟空档（休眠/唤醒），超过 %d 分钟上限，跳过本轮评估",
                    gap.total_seconds() / 60,
                    window * 2,
                )
                # 必须落行：否则「有意跳过」与「进程死了」在库里完全一样。
                self._store.insert_run_event(now, "sleep_gap", f"{gap.total_seconds() / 60:.1f}")
                return TickReport(None, None, False, closed, f"空档 {gap} 超过 2× 窗口，跳过")
        self._last_evaluation_at = now

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

"""tick 主循环。一次 tick 的顺序：先补回执，再评估，最后（可能）投递。"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial

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
from statesense.perception import FullscreenProbe, default_probe, is_gaming
from statesense.rulebook import version_of
from statesense.state.engine import classify, effective_entertainment_minutes
from statesense.state.taxonomy import bucket_minutes
from statesense.store.db import Store

log = logging.getLogger(__name__)

ACTION_CURSOR_KEY = "action_cursor"


def _error_detail(exc: BaseException) -> str:
    """异常类名 + 消息**首行**，截断至 200 字符（spec §7.2）。

    取首行而不是整条消息：包装过的异常，消息里往往重复堆叠同一条信息，
    换行还会把 report 缺口视图里「一行一条运行事件」的版式冲掉。
    """
    message = str(exc).strip()
    first_line = message.splitlines()[0] if message else ""
    return f"{type(exc).__name__}: {first_line}"[:200]


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
        fullscreen: FullscreenProbe | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._reader = reader
        self._store = store
        self._notifier = notifier
        self._wording = wording or TemplateWording()
        self._fullscreen = fullscreen or default_probe()
        self._last_evaluation_at: datetime | None = None
        # 判定规则在一次进程生命周期内不会变（配置是 frozen 的），所以算一次就够。
        # 它是**判定**的标识，因此写在 evaluations 行上：跨版本比较阈值时靠它分组。
        self._rule_version = version_of(config)

    @property
    def rule_version(self) -> str:
        """本轮判定所用的规则版本。启动日志与写库共用同一个值。"""
        return self._rule_version

    # ── 对外 ────────────────────────────────────────────────

    def ent_minutes(self, snapshot: ActivitySnapshot, fullscreen_state: int | None) -> float:
        """被动消费分钟数。与状态判定共用同一口径 —— **包括全屏提权**。

        若这里退回原始 taxonomy 口径，全屏识别出的游戏会在回执里 ent_before=0，
        回执直接落成 no_data —— 刚加的信号会自己把反馈回路切断。

        `fullscreen_state` 刻意不给默认值：它必须由调用方在一轮 tick 内**取一次**
        再复用。探针是一次系统调用，每调一次都可能给出不同答案；若判定与回执
        各自去探，两者用的就不是同一个事实了。
        """
        return effective_entertainment_minutes(
            bucket_minutes(snapshot.entries, self._config.taxonomy),
            trustworthy=snapshot.is_trustworthy,
            gaming=is_gaming(fullscreen_state),
        )

    def run_once(self) -> TickReport:
        now = self._clock.now()
        # 一轮只探一次，并把同一个值贯穿本轮所有用途（回执 + 状态判定）。
        fullscreen = self._fullscreen.state()
        closed = self._close_due_outcomes(now, fullscreen)
        return self._evaluate_tick(now, closed, fullscreen)

    def run_forever(self) -> None:
        """死循环。**「它返回了」这件事本身必须留下记录。**

        实测踩到过：任务计划程序报 `LastTaskResult=0`（成功），而 `run_forever`
        是死循环、本不该返回 —— 进程不见了，日志里一句话没有，库里也没有
        `run_events`（那两处补丁只覆盖 `run_tick_guarded` 抓到的异常，覆盖不了
        进程级退出）。唯一线索是一轮 tick 的间隔只有 2.8 分钟而不是 5.0。

        对比之下，这里的两行日志把三种结局分开了：
          · 有「异常中止」→ 是代码/环境抛出来的，原因就在同一行；
          · 只有「结束」而没有「异常中止」→ 循环自己走完了（那就是缺陷本身）；
          · 两句都没有 → 进程被外力杀掉，日志来不及写。
        """
        interval = timedelta(minutes=self._config.schedule.evaluate_every_minutes)
        log.info(
            "常驻循环开始 pid=%d：每 %d 分钟一轮，回看窗口 %d 分钟，库=%s",
            os.getpid(),
            self._config.schedule.evaluate_every_minutes,
            self._config.schedule.window_minutes,
            self._store.path,
        )
        last: datetime | None = None
        try:
            while True:
                now = self._clock.now()
                if last is None or now - last >= interval:
                    self.run_tick_guarded(now)
                    last = now
                time.sleep(self._config.schedule.tick_seconds)
        except BaseException as exc:  # noqa: BLE001 - 记录后原样抛出，绝不吞
            log.warning("常驻循环异常中止：%s: %s", type(exc).__name__, exc)
            raise
        finally:
            log.warning(
                "常驻循环结束 pid=%d（这条之后若没有新的「常驻循环开始」，"
                "说明它没有被重启）",
                os.getpid(),
            )

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
                self._store.insert_run_event(now, "tick_error", _error_detail(exc))
            except Exception:  # noqa: BLE001
                log.exception("写入 tick_error 运行事件失败")
            return None

    # ── 内部 ────────────────────────────────────────────────

    def _close_due_outcomes(self, now: datetime, fullscreen: int | None) -> int:
        delay = self._config.outcome.delay_minutes
        window = int(delay)
        closed = 0
        for due in self._store.due_interventions(now):
            # 两侧各用**可归属其时段**的证据，这是阶段 2.2 校正的核心：
            #   · 干预前：触发那一轮落库的 fullscreen_state —— 那是当时的事实；
            #   · 干预后：当下这一次探测 —— 它覆盖的正是最近这段时间。
            # 过去两侧共用当下的取值，于是「打游戏时触发、随后退出游戏」时，
            # 前侧被按「没在玩游戏」重算、ent_before 被低估甚至落成 no_data，
            # 而「退出游戏」看起来就像「干预有效」。
            # 历史全屏状态并不存在于别处，恰好就在触发那一轮的行里。
            trigger = self._store.fetch_evaluation(due.evaluation_id)
            if trigger is None:
                # 取不到就**保留未知**（未知 = 不提权，保守），绝不拿当下取值冒充历史。
                # 后果通常是前侧 ent 偏低直至 no_data —— 那是如实记录，不是失败。
                log.warning(
                    "干预 %d 找不到触发评估行 %d：回执前侧按「全屏状态未知」处理，"
                    "不用当下取值替代",
                    due.id,
                    due.evaluation_id,
                )
            before_fullscreen = trigger["fullscreen_state"] if trigger is not None else None

            before = self._reader.read(due.at - timedelta(minutes=delay), due.at, window, now)
            after = self._reader.read(
                due.at, due.at + timedelta(minutes=delay), window, now
            )
            verdict = evaluate_outcome(
                before,
                after,
                ent_before_minutes=partial(self.ent_minutes, fullscreen_state=before_fullscreen),
                ent_after_minutes=partial(self.ent_minutes, fullscreen_state=fullscreen),
                config=self._config.outcome,
            )
            self._store.insert_outcome(due.id, now, verdict)
            closed += 1
        return closed

    def _evaluate_tick(
        self, now: datetime, closed: int, fullscreen: int | None
    ) -> TickReport:
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
        verdict = classify(
            snapshot,
            self._config.taxonomy,
            self._config.thresholds,
            fullscreen_state=fullscreen,
        )

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

        evaluation_id = self._store.insert_evaluation(
            now, verdict, decision, rule_version=self._rule_version
        )

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
            channel=result.channel,
            outcome_due_at=now + timedelta(minutes=self._config.outcome.delay_minutes),
            user_response=result.user_response,
        )
        self._store.set_kv(ACTION_CURSOR_KEY, action.id)

        # 通道写进 note：`--dry-run` 的排练与真实弹窗在库里必须一眼可辨。
        note = f"{result.stored_status}@{result.channel}"
        if result.user_response:
            note = f"{note} response={result.user_response}"
        return TickReport(evaluation_id, str(verdict.state), True, closed, note)

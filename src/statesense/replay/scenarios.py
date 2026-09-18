"""五个内置剧本：各自既是一段数据，也是一组期望。

这些剧本把「需要真实等待」的时序验证变成秒级回归。`gates` 与 `degraded`
尤其重要 —— 它们对应的两个缺陷都是「154 个测试全绿之后才被发现」的，
因为它们不靠单元测试暴露，靠配置组合与真实数据暴露。
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from statesense._time import parse_iso
from statesense.config import GATE_COOLDOWN, GATE_DAILY_CAP, Config
from statesense.intervention.models import GateResult, first_failed, parse_gate_trace
from statesense.replay.runner import ReplayRun, run_scenario
from statesense.replay.scenario import Scenario, Segment

#: 剧本原点取**本地正午**。
#:
#: 用 `astimezone()` 把这个朴素的 12:00 解释成本机本地时刻，而不是写死一个 UTC
#: 时刻。曾经写的是 `datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)`，注释说
#: 「本地正午」，实际只在 UTC+8 的机器上是正午 —— 换个时区，整段剧本会滑进
#: `late_night`（本地 1:00–6:00）窗口，同一份剧本在两台机器上给出不同措辞。
#: 取正午还有一层好处：整段剧本必然落在同一个本地日内，`daily_cap` 的跨天边界
#: 不会干扰断言。
START = datetime(2026, 9, 16, 12, 0).astimezone()

#: 阶梯剧本必须真的走过的四档（spec §2 判据 1、§6.4）。
LADDER = ("NORMAL", "WATCH", "PASSIVE_CONSUMPTION", "HIGH_RISK_PASSIVE_CONSUMPTION")

BILIBILI = "【某视频】_哔哩哔哩_bilibili"
BILIBILI_URL = "https://www.bilibili.com/video/BV1"


def _fun(minutes: float) -> Segment:
    return Segment(0, minutes, "chrome.exe", BILIBILI, BILIBILI_URL)


SCENARIOS: dict[str, Scenario] = {
    "ladder": Scenario("ladder", 90, (_fun(90),)),
    # 展示用的主剧本：行为继续，回执应为 continued。
    "outcome": Scenario("outcome", 60, (_fun(60),)),
    "gates": Scenario("gates", 300, (_fun(300),)),
    "degraded": Scenario("degraded", 30, (), data_status="unreachable"),
    "sleep_gap": Scenario("sleep_gap", 30, (_fun(30),)),
}

#: 行为停止的对照剧本：娱乐恰好在触发点结束。
_OUTCOME_STOPPED = Scenario("outcome-stopped", 40, (_fun(40),))


def _advance(run: ReplayRun, ticks: int, step: float = 5.0) -> None:
    """跑 `ticks` 轮，每轮推进 `step` 分钟（首轮不推进，落在剧本原点）。"""
    for index in range(ticks):
        run.tick(0.0 if index == 0 else step)


def _trace(row) -> tuple[GateResult, ...]:
    """闸门留痕。坏数据当作「没有闸门信息」—— 断言因此会失败并说明原因，
    好过在剧本里抛异常让整次回放炸掉。"""
    return parse_gate_trace(row["gate_trace"]) or ()


def _first_failed_name(row) -> str | None:
    gate = first_failed(_trace(row))
    return gate.name if gate is not None else None


def _gate(row, name: str) -> GateResult | None:
    return next((gate for gate in _trace(row) if gate.name == name), None)


# ── 剧本一：20 / 40 / high_risk 阶梯 ─────────────────────────

def _drive_ladder(config: Config, workdir: Path) -> list[str]:
    failures: list[str] = []
    run = run_scenario(SCENARIOS["ladder"], config, start=START,
                       db_path=workdir / "replay-ladder.db")
    try:
        _advance(run, 19)  # 0 → 90 分钟
        rows = run.evaluations()
        states = [r["state"] for r in rows]

        # spec §6.4 / §2 判据 1：三档阈值必须真的走完。
        # 这条断言一度只写到 PASSIVE_CONSUMPTION —— 因为 high_risk_minutes 配成
        # 65 而窗口只有 60，那一档永远不可达。**让断言迁就代码，等于把死状态合法化。**
        # 现在配置层直接拒绝「阈值高于窗口」的组合，断言也恢复成规格要求的样子。
        for expected in LADDER:
            if expected not in states:
                failures.append(f"阶梯未经过 {expected}；实际序列 {states}")

        interventions = run.store.list_interventions()
        if not interventions:
            failures.append("连续 90 分钟娱乐却没有触发任何干预")
        # 「分类器里出现过」还不够：HIGH_RISK 必须真的走通全部闸门并选中动作，
        # 否则「状态可达」只停留在判定层，它专属的动作仍是死代码。
        intervened_states = {r["state"] for r in interventions}
        if "HIGH_RISK_PASSIVE_CONSUMPTION" not in intervened_states:
            failures.append(
                "HIGH_RISK_PASSIVE_CONSUMPTION 从未真正触发干预，"
                f"实际触发过的状态 {sorted(intervened_states)}"
            )

        # 结构性不变量：ent 是窗口内条目之和，不可能超过窗口长度。
        # 违反它意味着取数或裁剪算错了，而不是「刷得特别狠」。
        worst = max((r["ent_minutes"] for r in rows), default=0.0)
        if worst > config.schedule.window_minutes:
            failures.append(
                f"ent 达到 {worst} 分钟，超过窗口上限 {config.schedule.window_minutes}"
            )
    finally:
        run.close()
    return failures


# ── 剧本二：T+10min 行为回执 ─────────────────────────────────

def _drive_outcome(config: Config, workdir: Path) -> list[str]:
    failures: list[str] = []

    def _one(scenario: Scenario, tag: str, expected: str) -> None:
        run = run_scenario(scenario, config, start=START,
                           db_path=workdir / f"replay-outcome-{tag}.db")
        try:
            # 0 → 50 分钟：触发点在第 40 分钟，回执在第 50 分钟到期。
            # 用 5 分钟步长，所以第 11 轮正好落在 50 —— 回执在这一轮被结算。
            _advance(run, 11)
            rows = run.store.list_outcomes()
            if not rows:
                failures.append(f"[{tag}] 干预后没有落任何 outcomes 行")
                return
            actual = rows[0]["outcome"]
            if actual != expected:
                failures.append(
                    f"[{tag}] 期望 outcome={expected}，实际 {actual}"
                    f"（ent_before={rows[0]['ent_before']}, ent_after={rows[0]['ent_after']}）"
                )
        finally:
            run.close()

    _one(_OUTCOME_STOPPED, "stopped", "disengaged")
    _one(SCENARIOS["outcome"], "kept", "continued")
    return failures


# ── 剧本三：cooldown 与 daily_cap 边界 ───────────────────────

def _drive_gates(config: Config, workdir: Path) -> list[str]:
    """cooldown 与 daily_cap 的**精确**边界（spec §6.4）。

    这一节刻意用 1 分钟步长。5 分钟步长只能给出「间隔 25 被挡 / 30 通过」，
    边界到底压在哪一点上（29 挡、30 过）根本测不到 —— 而「边界值精确，
    不多不少」要的正是那个点。
    """
    failures: list[str] = []
    run = run_scenario(SCENARIOS["gates"], config, start=START,
                       db_path=workdir / "replay-gates.db")
    try:
        _advance(run, 301, step=1.0)  # 0 → 300 分钟，每分钟一轮
        rows = run.evaluations()
        interventions = run.store.list_interventions()

        cap = config.gate.daily_cap
        cooldown = config.gate.cooldown_minutes

        # ① daily_cap：「不多不少」= 恰好跑满上限。
        if len(interventions) != cap:
            failures.append(
                f"触发 {len(interventions)} 次，daily_cap={cap}，应当恰好相等"
            )
        if not any(_first_failed_name(r) == GATE_DAILY_CAP for r in rows):
            failures.append(
                f"跑满 300 分钟后仍未触到 daily_cap={cap}，该闸门没有被这条剧本覆盖"
            )

        # ② cooldown 的边界必须踩实：最大值（被挡）恰好是 cooldown-1。
        elapsed = [gate for gate in (_gate(r, GATE_COOLDOWN) for r in rows) if gate is not None]
        blocked = [
            g for g in elapsed
            if not g.passed and g.value is not None and g.value < cooldown
        ]
        if not blocked:
            failures.append("没有任何一轮被 cooldown 以「不足阈值」挡下，边界未被覆盖")
        else:
            furthest = max(g.value for g in blocked)
            if furthest != cooldown - 1:
                failures.append(
                    f"被 cooldown 挡下的最大间隔是 {furthest}，应为 {cooldown - 1}"
                    " —— 边界没有踩实"
                )
        if not any(g.passed and g.value == cooldown for g in elapsed):
            failures.append(f"没有一轮在恰好 {cooldown} 分钟时通过 cooldown")
        if any(not g.passed and g.value is not None and g.value >= cooldown for g in elapsed):
            failures.append("有轮次在间隔已达阈值时仍被判为 cooldown 未通过")

        # ③ 干预间隔不得短于阈值。
        times = [parse_iso(r["at"]) for r in interventions]
        for previous, current in zip(times, times[1:]):
            gap = (current - previous).total_seconds() / 60
            if gap < cooldown:
                failures.append(f"两次干预仅隔 {gap:g} 分钟，短于 cooldown={cooldown}")

        # ④ 「第 cap 次通过」与「第 cap+1 次被挡」两侧都要看到。
        #    只看被挡的那一侧，无法区分「上限生效」与「根本没跑到上限」。
        daily = [gate for gate in (_gate(r, GATE_DAILY_CAP) for r in rows) if gate is not None]
        if not any(g.passed and g.value == cap - 1 for g in daily):
            failures.append(
                f"没有一轮在「今天已触发 {cap - 1} 次」时通过 daily_cap（即第 {cap} 次）"
            )
        if not any(not g.passed and g.value == cap for g in daily):
            failures.append(
                f"没有一轮在「今天已触发 {cap} 次」时被 daily_cap 挡下（即第 {cap + 1} 次）"
            )
    finally:
        run.close()
    return failures


# ── 剧本四：取不到数据时不得下结论 ───────────────────────────

def _drive_degraded(config: Config, workdir: Path) -> list[str]:
    failures: list[str] = []
    run = run_scenario(SCENARIOS["degraded"], config, start=START,
                       db_path=workdir / "replay-degraded.db")
    try:
        _advance(run, 6)
        rows = run.evaluations()
        if len(rows) != 6:
            failures.append(f"降级轮次也应逐轮落行，实际 {len(rows)} 行")
        for row in rows:
            if row["skipped"] != 1:
                failures.append(f"{row['at']} 的 data_status 不可信，却没有标记 skipped")
            # 成对断言（spec §6.4）：skipped=1 证明没被当成可信结论；
            # state 恰为 NORMAL 占位，证明谁也读不出结论。
            if row["state"] != "NORMAL":
                failures.append(
                    f"{row['at']} 不可信轮次的 state 列出现了 {row['state']} —— "
                    "降级轮次的取值只能是 NORMAL 占位，出现别的值说明它被当成了结论"
                )
            if row["data_status"] != "unreachable":
                failures.append(f"{row['at']} 的 data_status 被改成了 {row['data_status']}")
        if run.store.list_interventions():
            failures.append("数据不可信时仍然发生了干预 —— 核心安全属性被击穿")
    finally:
        run.close()
    return failures


# ── 剧本五：休眠/唤醒空档 ────────────────────────────────────

def _drive_sleep_gap(config: Config, workdir: Path) -> list[str]:
    failures: list[str] = []
    run = run_scenario(SCENARIOS["sleep_gap"], config, start=START,
                       db_path=workdir / "replay-sleep-gap.db")
    try:
        run.tick()          # 正常一轮
        run.tick(200.0)     # 超过 2 × 窗口
        rows = run.evaluations()
        if len(rows) != 1:
            failures.append(f"空档轮次不该落 evaluations 行，实际共 {len(rows)} 行")
        events = run.store.list_run_events()
        if not events or events[0]["kind"] != "sleep_gap":
            failures.append(f"空档没有落 sleep_gap 运行事件，实际 {[e['kind'] for e in events]}")
        elif float(events[0]["detail"]) < 200.0:
            failures.append(f"sleep_gap 的 detail 应为 200 分钟，实际 {events[0]['detail']}")
    finally:
        run.close()
    return failures


DRIVERS: dict[str, Callable[[Config, Path], list[str]]] = {
    "ladder": _drive_ladder,
    "outcome": _drive_outcome,
    "gates": _drive_gates,
    "degraded": _drive_degraded,
    "sleep_gap": _drive_sleep_gap,
}


def check_scenario(name: str, config: Config, *, keep_db: bool = False) -> list[str]:
    """在独立的库上跑一个剧本，返回失败原因（空列表 = 通过）。

    每个剧本都必须**从干净状态开始** —— 否则跑第二遍会在上次的库上累加行数，
    同一命令给出不同结果。一个不可重复的验证工具比没有验证工具更糟：
    它会在你第二次运行时给你一个假的失败或假的通过。

    默认在临时目录里跑、跑完删掉（spec §11：既不污染生产库，也不留垃圾）。
    `keep_db=True` 时改在 `<store 所在目录>/replay/<剧本>/` 下跑并保留，
    可以用 `--report --db <那里的库>` 继续读它。
    """
    if name not in DRIVERS:
        raise KeyError(f"未知剧本 {name!r}")

    if keep_db:
        workdir = Path(config.store_path).parent / "replay" / name
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True)
        return DRIVERS[name](config, workdir)

    workdir = Path(tempfile.mkdtemp(prefix=f"statesense-replay-{name}-"))
    try:
        return DRIVERS[name](config, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

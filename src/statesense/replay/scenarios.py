"""五个内置剧本：各自既是一段数据，也是一组期望。

这些剧本把「需要真实等待」的时序验证变成秒级回归。`gates` 与 `degraded`
尤其重要 —— 它们对应的两个缺陷都是「154 个测试全绿之后才被发现」的，
因为它们不靠单元测试暴露，靠配置组合与真实数据暴露。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from statesense.config import Config
from statesense.replay.runner import ReplayRun, run_scenario
from statesense.replay.scenario import Scenario, Segment

#: 剧本原点取本地正午，保证整段剧本落在同一个本地日内 ——
#: daily_cap 按本地日统计，跨天会让边界断言失去意义。
START = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)

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


def _first_failed(row) -> str | None:
    import json

    try:
        trace = json.loads(row["gate_trace"])
    except (TypeError, ValueError):
        return None
    if not isinstance(trace, list):
        return None
    return next((g["name"] for g in trace if isinstance(g, dict) and not g["passed"]), None)


def _failed_gate_value(row, name: str):
    import json

    trace = json.loads(row["gate_trace"])
    return next((g.get("value") for g in trace if g["name"] == name), None)


def structural_findings(config: Config) -> list[str]:
    """从配置本身就能推出的结构性问题。

    这些不是剧本失败，但会让某些代码成为死路 —— 每次 --replay 都提示一次，
    免得它悄悄留在配置里。
    """
    findings: list[str] = []
    if config.thresholds.high_risk_minutes > config.schedule.window_minutes:
        findings.append(
            f"high_risk_minutes={config.thresholds.high_risk_minutes:g} 高于 "
            f"window_minutes={config.schedule.window_minutes:g}："
            "ent 是窗口内条目分钟数之和，不可能超过窗口长度，因此 "
            "HIGH_RISK_PASSIVE_CONSUMPTION 不可达，其专属动作永远不会被选中。"
            "修正方向：把 high_risk_minutes 降到窗口以内，或加长 window_minutes"
        )
    return findings


# ── 剧本一：20 / 40 / 65 阶梯 ────────────────────────────────

def _drive_ladder(config: Config, workdir: Path) -> list[str]:
    failures: list[str] = []
    run = run_scenario(SCENARIOS["ladder"], config, start=START,
                       db_path=workdir / "replay-ladder.db")
    try:
        _advance(run, 19)  # 0 → 90 分钟
        rows = run.evaluations()
        states = [r["state"] for r in rows]

        for expected in ("NORMAL", "WATCH", "PASSIVE_CONSUMPTION"):
            if expected not in states:
                failures.append(f"阶梯未经过 {expected}；实际序列 {states}")

        if not run.store.list_interventions():
            failures.append("连续 90 分钟娱乐却没有触发任何干预")

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
            _advance(run, 11)  # 触发点 40 分钟，再走一轮到 45
            _advance(run, 2)   # 走到 55，回执到期
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
    failures: list[str] = []
    run = run_scenario(SCENARIOS["gates"], config, start=START,
                       db_path=workdir / "replay-gates.db")
    try:
        _advance(run, 61)  # 0 → 300 分钟
        rows = run.evaluations()
        interventions = run.store.list_interventions()

        # ① daily_cap：不该超过上限。
        if len(interventions) > config.gate.daily_cap:
            failures.append(
                f"触发 {len(interventions)} 次，超过 daily_cap={config.gate.daily_cap}"
            )
        if not any(_first_failed(r) == "daily_cap" for r in rows):
            failures.append(
                f"跑满 300 分钟后仍未触到 daily_cap={config.gate.daily_cap}，"
                "该闸门没有被这条剧本覆盖"
            )

        # ② cooldown：触发间隔不得短于阈值，且必须真的挡下过。
        times = [r["at"] for r in interventions]
        for previous, current in zip(times, times[1:]):
            from statesense._time import parse_iso

            gap = (parse_iso(current) - parse_iso(previous)).total_seconds() / 60
            if gap < config.gate.cooldown_minutes:
                failures.append(
                    f"两次干预仅隔 {gap:g} 分钟，短于 cooldown={config.gate.cooldown_minutes}"
                )

        cooldown_blocked = [
            r for r in rows
            if _first_failed(r) == "cooldown"
            and _failed_gate_value(r, "cooldown") is not None
            and _failed_gate_value(r, "cooldown") < config.gate.cooldown_minutes
        ]
        if not cooldown_blocked:
            failures.append("没有任何一轮被 cooldown 以「不足阈值」挡下，边界未被覆盖")
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


def check_scenario(name: str, config: Config) -> list[str]:
    """在独立的库上跑一个剧本，返回失败原因（空列表 = 通过）。

    每个剧本用自己的 db 文件，绝不碰 config 指向的生产库。
    """
    if name not in DRIVERS:
        raise KeyError(f"未知剧本 {name!r}")
    workdir = Path(config.store_path).parent
    return DRIVERS[name](config, workdir)

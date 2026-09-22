"""阶段 1（V1 分类与触发阈值）的边界回归 —— 全部在示例配置上判真伪。

计划 `docs/plans/2026-09-22-stage-1-v1-classification-and-thresholds.md` 里，
任务 1.2 的验收线是「全屏终端不因一次状态 2 就被无证据地判成游戏」，任务 1.3 的
验收线是「阈值须保持 HIGH_RISK 可达，且不得绕过 state_min」。这两条是**规则语义**：
不需要真实运行数据就能判真伪，所以先用本文件把它们固定成回归锁。

需要真实标注才能回答的那部分（哪些 OTHER 是真漏判、阈值是否太晚、被 ratio_min
挡下的案例该不该提醒）见 `test_stage1_event_calibration.py`。

用示例配置而不是手搓正则：要校准的是**随仓库发布的清单**，手搓一份专用清单
只会测到测试自己。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from statesense.activity.models import ActivitySnapshot, Entry
from statesense.config import (
    GATE_RATIO_MIN,
    Config,
    ThresholdConfig,
)
from statesense.intervention.decider import decide
from statesense.intervention.gates import INTERVENABLE, GateContext, run_gates
from statesense.intervention.models import GateResult, first_failed
from statesense.perception import (
    QUNS_ACCEPTS_NOTIFICATIONS,
    QUNS_BUSY,
    QUNS_RUNNING_D3D_FULL_SCREEN,
)
from statesense.report.queries import build_gate_breakdown
from statesense.state.engine import classify
from statesense.state.models import State, StateVerdict
from statesense.state.taxonomy import Category, classify as classify_entry

#: 本地正午。与回放剧本同款取法：把朴素时刻解释成本机本地时间，
#: 避免整批样本在不同时区滑进 late_night（本地 1:00–6:00）而改变结论。
NOON = datetime(2026, 9, 22, 12, 0).astimezone()

#: 状态取值的可读别名（原始值来自 perception，这里不重新定义取值本身）。
D3D = QUNS_RUNNING_D3D_FULL_SCREEN
BUSY = QUNS_BUSY
NOT_FULLSCREEN = QUNS_ACCEPTS_NOTIFICATIONS

#: 样本用的窗口内容。标题与进程名照抄真实场景，且刻意不写游戏名 ——
#: 「Brotato」这类游戏只能靠全屏信号识别，见下面第一条收益测试。
SAMPLES: dict[str, tuple[str, str, str]] = {
    "video": ("chrome.exe", "【某视频】_哔哩哔哩_bilibili", "https://www.bilibili.com/video/BV1"),
    "work": ("Code.exe", "statesense - Visual Studio Code", ""),
    "terminal": ("WindowsTerminal.exe", "Windows PowerShell", ""),
    "gray": ("chrome.exe", "某问题 - 知乎", ""),
    "doc": ("Acrobat.exe", "long-paper.pdf - Adobe Acrobat", ""),
    "game": ("Brotato.exe", "Brotato", ""),
}


def _item(kind: str, minutes: float) -> Entry:
    app, title, url = SAMPLES[kind]
    return Entry(app=app, title=title, url=url, minutes=minutes)


def _snap(
    config: Config,
    entries: tuple[Entry, ...] | list[Entry],
    total: float | None = None,
    *,
    at: datetime = NOON,
) -> ActivitySnapshot:
    window = config.schedule.window_minutes
    items = tuple(entries)
    return ActivitySnapshot(
        window_start=at - timedelta(minutes=window),
        window_end=at,
        window_minutes=window,
        total_active_minutes=float(total if total is not None else sum(e.minutes for e in items)),
        entries=items,
        data_status="ok",
        captured_at=at,
    )


def _window(config: Config, ent_minutes: float) -> ActivitySnapshot:
    """一个「娱乐 ent 分钟 + 剩余分钟在工作」的满窗，用于扫阈值边界。"""
    total = float(config.schedule.window_minutes)
    entries = [_item("video", ent_minutes)]
    if total - ent_minutes > 0:
        entries.append(_item("work", total - ent_minutes))
    return _snap(config, entries, total)


def _verdict(
    config: Config,
    ent_minutes: float,
    *,
    fullscreen_state: int | None = NOT_FULLSCREEN,
    thresholds: ThresholdConfig | None = None,
) -> StateVerdict:
    return classify(
        _window(config, ent_minutes),
        config.taxonomy,
        thresholds or config.thresholds,
        fullscreen_state=fullscreen_state,
    )


def _gates(
    config: Config,
    verdict: StateVerdict,
    *,
    now: datetime = NOON,
    last: datetime | None = None,
    today: int = 0,
    gate_config=None,
) -> tuple[GateResult, ...]:
    return run_gates(
        GateContext(
            verdict=verdict,
            config=gate_config or config.gate,
            now=now,
            last_intervention_at=last,
            interventions_today=today,
        )
    )


def _named(results: tuple[GateResult, ...], name: str) -> GateResult:
    return next(r for r in results if r.name == name)


# ── 任务 1.2：全屏提权的收益侧与误报侧 ──────────────────────────────


def test_unlisted_windowed_game_is_invisible_and_the_fullscreen_signal_is_what_fixes_it(
    config: Config,
):
    """收益侧：Brotato 这类「进程名就是游戏自身」的窗口，清单永远抓不到。

    同一份条目，只换全屏状态就够了 —— 差额就是这条信号的收益。注意它是**有边界的**：
    窗口化游玩时 2 与 3 都不会出现，这段仍然是漏判。计划 1.2 要求分开写「漏判原因」，
    这条测试把「靠全屏信号救回来」和「窗口化游戏救不回来」都固定在同一处。
    """
    items = [_item("game", 46.9)]
    without = classify(_snap(config, items, 60.0), config.taxonomy, config.thresholds,
                       fullscreen_state=NOT_FULLSCREEN)
    with_signal = classify(_snap(config, items, 60.0), config.taxonomy, config.thresholds,
                           fullscreen_state=D3D)

    # 无信号：既没命中任何规则，也没被提权 —— 46.9 分钟的游戏被判成 NORMAL。
    assert without.ent_minutes == 0.0
    assert without.state is State.NORMAL
    # 有信号：未归类条目被提权，46.9 分钟落在 [passive, high_risk) 之间。
    assert with_signal.ent_minutes == 46.9
    assert with_signal.state is State.PASSIVE_CONSUMPTION


@pytest.mark.parametrize("fullscreen", [BUSY, D3D])
def test_fullscreen_terminal_is_not_promoted_to_entertainment(config: Config, fullscreen: int):
    """计划 1.2 的验收线原文：全屏终端不因一次状态 2 就被无证据地判成游戏。

    挡住它的不是时长也不是运气，而是分类：示例清单里有
    `terminal` / `powershell` / `bash` / `cmd.exe`，而
    `effective_entertainment_minutes` 只提权 OTHER。于是「全屏终端」这一类误报
    在**分类阶段**就被排除；一旦有人把终端规则从清单里删掉，这条立刻变红。
    """
    verdict = classify(
        _snap(config, [_item("terminal", 45.0)]), config.taxonomy, config.thresholds,
        fullscreen_state=fullscreen,
    )
    assert verdict.work_minutes == 45.0
    assert verdict.ent_minutes == 0.0
    assert verdict.state is State.NORMAL


@pytest.mark.parametrize("fullscreen", [BUSY, D3D])
def test_short_fullscreen_document_cannot_reach_watch(config: Config, fullscreen: int):
    """接受状态 2 的第二个兜底：短暂的全屏文档只贡献几分钟，够不到 watch 门槛。

    这里提权**确实发生了**（ent=8 而不是 0），挡下结论的是时长。
    与上一条合起来看，才是 `perception.GAMING_STATES` 注释里「代价由三件事兜住」
    的可执行版本 —— 它同时说明这条兜底有多脆：文档读满一个窗口就兜不住了。
    """
    verdict = classify(
        _snap(config, [_item("doc", 8.0)]), config.taxonomy, config.thresholds,
        fullscreen_state=fullscreen,
    )
    assert verdict.ent_minutes == 8.0
    assert verdict.state is State.NORMAL


def test_long_unclassified_fullscreen_app_is_a_known_false_positive(config: Config):
    """误报侧的最小可复现案例：OTHER + 状态 2 + 满窗 60 分钟 → HIGH_RISK。

    这不是缺陷，是已知代价：无边框窗口的游戏与全屏文档落在同一个
    `SHQueryUserNotificationState` 取值上。计划 1.2 要回答「误报与收益各有多少」，
    本测试固定分子（1 个满窗文档 = 1 次误报），收益见上面第一条。
    原始值逐轮入库，所以它可事后审计 —— 这也正是记 `fullscreen_state` 而非布尔的理由。
    """
    verdict = classify(
        _snap(config, [_item("doc", 60.0)]), config.taxonomy, config.thresholds,
        fullscreen_state=BUSY,
    )
    assert verdict.ent_minutes == 60.0
    assert verdict.fullscreen_state == BUSY
    assert verdict.state is State.HIGH_RISK_PASSIVE_CONSUMPTION


def test_gray_is_counted_on_its_own_and_never_promoted(config: Config):
    """GRAY 不并进娱乐，也不进工作 —— 计划 1.2 明确要求「是否重分类由标注数据决定」。

    在标注到位之前，先锁住现状语义：全屏状态下知乎仍然只是 GRAY，不抬高 ent。
    """
    verdict = classify(
        _snap(config, [_item("gray", 50.0)]), config.taxonomy, config.thresholds,
        fullscreen_state=BUSY,
    )
    assert verdict.gray_minutes == 50.0
    assert verdict.ent_minutes == 0.0
    assert verdict.work_minutes == 0.0
    assert verdict.state is State.NORMAL


def test_unknown_fullscreen_state_promotes_nothing(config: Config):
    """「不知道」不等于「是」。取不到状态时保守不提权，与「知道不是」分开记。"""
    verdict = classify(
        _snap(config, [_item("doc", 60.0)]), config.taxonomy, config.thresholds,
        fullscreen_state=None,
    )
    assert verdict.ent_minutes == 0.0
    assert verdict.state is State.NORMAL


def test_entertainment_outranks_work_when_one_title_matches_both(config: Config):
    """优先级是刻意的：同时命中工作与娱乐按娱乐处理。

    在 GitHub 页面上看 B 站视频，正则两边都命中。这条与「全屏终端不提权」是一对：
    分类的召回优先于精确，全屏提权的范围则刻意收窄。两处取舍方向相反，不能混谈。
    """
    entry = Entry(
        app="chrome.exe",
        title="GitHub - 【某视频】_哔哩哔哩_bilibili",
        url="https://github.com/",
        minutes=45.0,
    )
    assert classify_entry(entry, config.taxonomy) is Category.ENTERTAINMENT

    verdict = classify(_snap(config, [entry], 60.0), config.taxonomy, config.thresholds)
    assert verdict.ent_minutes == 45.0
    assert verdict.state is State.PASSIVE_CONSUMPTION


# ── 任务 1.3：阈值语义 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ent", "expected"),
    [
        (19.9, State.NORMAL),
        (20.0, State.WATCH),
        (39.9, State.WATCH),
        (40.0, State.PASSIVE_CONSUMPTION),
        (54.9, State.PASSIVE_CONSUMPTION),
        (55.0, State.HIGH_RISK_PASSIVE_CONSUMPTION),
        (60.0, State.HIGH_RISK_PASSIVE_CONSUMPTION),
    ],
)
def test_example_config_ladder_boundaries(config: Config, ent: float, expected: State):
    """示例配置的 20 / 40 / 55 三档，边界取值取到的那一档（下界含）。

    固化的不是「20 这个数好看」，而是「阈值是含下界的台阶」这个语义 ——
    阶段 1.3 拿候选阈值比较时，1 分钟的差必须落在同一档，否则比较表的每格都要重算。
    """
    assert _verdict(config, ent).state is expected


def test_raising_a_threshold_never_adds_an_intervenable_round(config: Config):
    """单调性：阈值只能「抬高门槛」，不可能把原本不可干预的轮次变成可干预。

    这是阈值比较能在**不重跑真实数据**的前提下做的前提：候选阈值之间可以按包含关系
    排序比较。断言写成真子集而不是包含 —— 否则一条恒真的断言证明不了任何事。
    """
    grid = [0.0, 5.0, 12.5, 20.0, 25.0, 33.3, 40.0, 47.5, 55.0, 60.0]

    def intervenable(thresholds: ThresholdConfig, values: list[float]) -> set[float]:
        return {
            value
            for value in values
            if _verdict(config, value, thresholds=thresholds).state in INTERVENABLE
        }

    base = intervenable(config.thresholds, grid)
    assert base == {40.0, 47.5, 55.0, 60.0}

    raised = intervenable(replace(config.thresholds, passive_minutes=47.5), grid)
    assert raised < base
    # 只是把 passive 抬到 47.5：47.5 与 55 都被裹进 PASSIVE 一档，55 仍在可干预集合里 ——
    # 因为让它可干预的是 high_risk 那一档，不是 passive。抬高某一条阈值不会牵动别的档。
    assert raised == {47.5, 55.0, 60.0}


@pytest.mark.parametrize(
    "thresholds",
    [
        ThresholdConfig(watch_minutes=0.1, passive_minutes=0.2, high_risk_minutes=0.3),
        ThresholdConfig(watch_minutes=1, passive_minutes=2, high_risk_minutes=3),
    ],
    ids=["extreme-low", "low"],
)
def test_low_thresholds_cannot_bypass_state_min(config: Config, thresholds: ThresholdConfig):
    """计划 1.3 的验收线之一：阈值不得绕过 state_min。

    把三档压到 1 / 2 / 3 这种极端值，ent 落在 [watch, passive) 的轮次依然是 WATCH，
    仍然进不了可干预集合。这条是安全属性的下限：阈值是可调参数，state_min 不是。
    """
    ent = (thresholds.watch_minutes + thresholds.passive_minutes) / 2
    verdict = _verdict(config, ent, thresholds=thresholds)

    assert verdict.state is State.WATCH
    assert verdict.state not in INTERVENABLE
    assert _named(_gates(config, verdict), "state_min").passed is False


@pytest.mark.parametrize("high_risk", [55.0, 58.0, 60.0])
def test_every_candidate_keeps_high_risk_reachable(config: Config, high_risk: float):
    """计划 1.3 的验收线之一：阈值须保持 HIGH_RISK 可达。

    判据做成可执行的：把整个窗口填满娱乐（ent = window_minutes）那一刻必须真的判成
    HIGH_RISK。曾经 high_risk=65 配 window=60 就是死状态，配置层现在直接拒绝这种
    组合，但「拒绝非法值」与「合法值真的可达」是两件事，这条测的是后者。
    """
    thresholds = replace(config.thresholds, high_risk_minutes=high_risk)
    full = float(config.schedule.window_minutes)
    verdict = _verdict(config, full, thresholds=thresholds)

    assert verdict.ent_minutes == full
    assert verdict.state is State.HIGH_RISK_PASSIVE_CONSUMPTION
    assert _named(_gates(config, verdict, gate_config=replace(config.gate, ratio_min=0.75)),
                  "state_min").passed is True


def test_ratio_min_has_an_implied_floor_so_its_effective_range_is_narrow(config: Config):
    """ratio_min 的有效区间不是 (0, 1]，而是 (passive/window, 1]。

    ent ≥ passive 已经隐含 ratio ≥ 40/60 ≈ 0.667，所以更低的 ratio_min 永远拦不下
    任何一轮 —— 它写在配置里，却不产生任何行为。阶段 1.3 比较 ratio_min 时必须先扣掉
    这段空区间；否则「换了个值、结果没变」会被误读成「这个阈值很稳」。
    """
    floor = config.thresholds.passive_minutes / config.schedule.window_minutes
    assert config.gate.required_ratio_min() > floor

    for candidate in (0.4, 0.5, 0.6):
        gate_config = replace(config.gate, ratio_min=candidate)
        for ent in (40.0, 45.0, 52.0, 60.0):
            verdict = _verdict(config, ent)
            assert verdict.state in INTERVENABLE
            assert _named(_gates(config, verdict, gate_config=gate_config), GATE_RATIO_MIN).passed, (
                f"ratio_min={candidate} 拦下了一个 ent={ent} 的轮次，"
                "说明空区间的下界算错了"
            )


def test_state_ready_but_ratio_blocked_stays_silent_and_is_visible_in_the_report(config: Config):
    """计划 1.2 的开放问题：状态已达标却被比例闸门拦下，该提醒还是该安静？

    当前实现的回答是**保持安静**，而且不是静默的：闸门留痕里 ratio_min 记下了
    实际值与阈值，报告视图 3 能把这类轮次单独列出来。本测试固定这个选择，
    免得它随某次重构无声漂移；真要改成提醒，必须先有标注数据说明比例闸门是错的。
    """
    total = float(config.schedule.window_minutes)  # 60
    ent = 42.0                                     # 42 / 60 = 0.70 < 0.75
    verdict = classify(
        _snap(config, [_item("video", ent), _item("work", total - ent)], total),
        config.taxonomy,
        config.thresholds,
    )
    assert verdict.state is State.PASSIVE_CONSUMPTION
    assert verdict.ent_ratio == 0.7

    ctx = GateContext(
        verdict=verdict, config=config.gate, now=NOON,
        last_intervention_at=None, interventions_today=0,
    )
    decision = decide(ctx, config.actions, None)
    assert decision.intervene is False
    assert GATE_RATIO_MIN in decision.reason
    failing = first_failed(decision.gate_trace)
    assert failing is not None and failing.name == GATE_RATIO_MIN
    assert failing.threshold == config.gate.required_ratio_min()

    # 同一轮在报告里必须能被单独数出来，否则「保持安静」就等于「无从复查」。
    from statesense.intervention.models import dump_gate_trace

    row = {
        "at": NOON.isoformat(),
        "total_active_minutes": total,
        "ent_ratio": verdict.ent_ratio,
        "gate_trace": dump_gate_trace(decision.gate_trace),
    }
    breakdown = build_gate_breakdown([row])
    assert breakdown.ratio_min_passed == 0
    assert breakdown.ratio_min_blocked == 1
    assert [c.gate for c in breakdown.state_min_passed_then_blocked] == [GATE_RATIO_MIN]

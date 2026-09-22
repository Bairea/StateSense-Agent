"""阶段 1 的离线校准：合成标注语料 + 事件级评价。

计划 `docs/plans/2026-09-22-stage-1-v1-classification-and-thresholds.md` 的任务 1.1
要求「把重叠的五分钟评估窗口合并为同一次消费事件，统计事件级漏判、误报和首次提醒延迟」，
任务 1.3 要求「在同一批标注事件上比较候选阈值」。这两件事本来要等真实运行数据，
本文件先用**合成标注语料**把它们变成可执行、可复现、跑一次几十毫秒的检查。

**这份语料是合成的，不是使用记录，也不代表真实分布。**
它的作用有三条，仅此三条：
  1. 把计划里的口径（事件级、漏判、误报、首次提醒延迟）写成可运行的代码，
     真实标注到位后只需换掉语料，不必重写口径；
  2. 固定那些**不依赖真实分布**的结论（例如「阈值调不动分类漏判」），
     它们是口径的性质，不是数据的性质；
  3. 给计划里提到的每条边界各留一个最小案例（全屏终端、窗口化游戏、
     状态 2 的文档误报、短事件被比例闸门拦下）。

语料里的每一个数字都不得被引用为「用户实际怎么用」—— 用它反推真实分布是不成立的。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import pytest

from statesense.activity.models import ActivitySnapshot, Entry
from statesense.config import (
    GATE_RATIO_MIN,
    GATE_STATE_MIN,
    Config,
    ThresholdConfig,
)
from statesense.intervention.decider import decide
from statesense.intervention.gates import INTERVENABLE, GateContext, run_gates
from statesense.intervention.models import Decision, first_failed
from statesense.perception import (
    GAMING_STATES,
    QUNS_ACCEPTS_NOTIFICATIONS,
    QUNS_BUSY,
    QUNS_RUNNING_D3D_FULL_SCREEN,
)
from statesense.state.engine import classify
from statesense.state.models import State, StateVerdict
from statesense.rulebook import version_of
from statesense.state.taxonomy import Category, classify as classify_entry

#: 本地正午起点。与回放剧本同款取法，避免样本在不同时区滑进 late_night。
NOON = datetime(2026, 9, 22, 12, 0).astimezone()

D3D = QUNS_RUNNING_D3D_FULL_SCREEN
BUSY = QUNS_BUSY
NOT_FULLSCREEN = QUNS_ACCEPTS_NOTIFICATIONS

#: 评估节奏与连续性阈值，取自示例配置的默认值（schedule.evaluate_every_minutes、
#: report.gap_threshold_minutes）。写在这里是为了让语料的生成方式一眼可读。
CADENCE = 5
GAP = 15


# ── 语料 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Segment:
    """一段连续活动，以及人对它的标注。

    `label_passive` 是**真值**（此刻此人是否在被动消费），`reason` 必须写清依据 ——
    标不出理由的样本就是「不确定」，不确定的不进语料，免得把猜测当证据。
    """

    offset: int
    duration: int
    app: str
    title: str
    fullscreen: int | None
    label_passive: bool
    reason: str
    url: str = ""

    @property
    def start(self) -> int:
        return self.offset

    @property
    def end(self) -> int:
        return self.offset + self.duration


def _video(minutes: int) -> Segment:
    return Segment(
        30, minutes, "chrome.exe", "【某视频】_哔哩哔哩_bilibili", NOT_FULLSCREEN,
        label_passive=True,
        reason="明确在刷 B 站，人自己会认：真被动消费",
        url="https://www.bilibili.com/video/BV1",
    )


#: 合成的一天（本地 12:00 起）。每段都对应计划里一个待答问题：
#:   · 30 分钟的短视频 → 阈值是否太晚（真事件，现值 passive=40 判不到）
#:   · 全屏终端 → 状态 2 会不会把终端判成游戏（不会，分类先拦下）
#:   · 全屏文档 90 分钟 → 状态 2 的误报代价（会判成 HIGH_RISK）
#:   · 窗口化游戏 90 分钟 → 分类未命中，全屏信号也救不回来（真漏判）
DAY: tuple[Segment, ...] = (
    Segment(0, 30, "Code.exe", "statesense - Visual Studio Code", NOT_FULLSCREEN,
            label_passive=False, reason="在写代码"),
    _video(30),
    Segment(60, 30, "WindowsTerminal.exe", "Windows PowerShell", BUSY,
            label_passive=False, reason="全屏终端；状态 2 也覆盖它"),
    Segment(180, 90, "Acrobat.exe", "long-paper.pdf - Adobe Acrobat", BUSY,
            label_passive=False, reason="全屏读文档；状态 2 的已知误报源"),
    Segment(345, 90, "Brotato.exe", "Brotato", NOT_FULLSCREEN,
            label_passive=True, reason="窗口化打游戏，游戏名不在清单里"),
)

#: 没有轮次的区间 [起, 止)：机器不在（睡眠/关机）。它同时是「连续性缺口」的制造者，
#: 两次缺口都刻意长过窗口长度，保证后一段的窗口里不含前一段的残留。
ABSENT: tuple[tuple[int, int], ...] = ((95, 180), (275, 345))


@dataclass(frozen=True)
class Tick:
    """一轮评估的输入 + 该时刻的人工标注。"""

    at: datetime
    entries: tuple[Entry, ...]
    total_active_minutes: float
    fullscreen_state: int | None
    label_passive: bool
    reason: str

    def snapshot(self, window_minutes: int) -> ActivitySnapshot:
        return ActivitySnapshot(
            window_start=self.at - timedelta(minutes=window_minutes),
            window_end=self.at,
            window_minutes=window_minutes,
            total_active_minutes=self.total_active_minutes,
            entries=self.entries,
            data_status="ok",
            captured_at=self.at,
        )


def build_ticks(
    segments: Sequence[Segment],
    *,
    absent: Sequence[tuple[int, int]] = (),
    cadence: int = CADENCE,
    window_minutes: int = 60,
) -> tuple[Tick, ...]:
    """把分段剧本铺成每 `cadence` 分钟一轮的窗口序列。

    窗口一律取「此刻往回 `window_minutes` 分钟」，不裁剪到剧本起点 ——
    真实 reader 就是这么读的，裁掉会让第一轮凭空多出娱乐分钟。
    """
    end = max(s.end for s in segments)
    ticks: list[Tick] = []
    for offset in range(0, end + 1, cadence):
        if any(low <= offset < high for low, high in absent):
            continue
        window_start = offset - window_minutes
        entries = tuple(
            Entry(seg.app, seg.title, seg.url, float(overlap))
            for seg in segments
            if (overlap := min(offset, seg.end) - max(window_start, seg.start)) > 0
        )
        # 归属取「最近一次开始的那一段」，而不是「正好落在区间内的那一段」：
        # 状态在两次切换之间保持不变，而取区间会让**段末那一轮**落空，
        # 连带丢掉全屏信号 —— 那是夹具造成的假象，会直接改写事件边界。
        owner = next((s for s in reversed(segments) if s.start <= offset), None)
        ticks.append(
            Tick(
                at=NOON + timedelta(minutes=offset),
                entries=entries,
                total_active_minutes=float(sum(e.minutes for e in entries)),
                fullscreen_state=owner.fullscreen if owner else NOT_FULLSCREEN,
                label_passive=bool(owner and owner.label_passive),
                reason=owner.reason if owner else "机器不在，没有活动记录",
            )
        )
    return tuple(ticks)


# ── 评价脚手架 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Evaluation:
    """一轮的输入、判定与决策。三者绑在一起，事件级统计才不用在各处重新对齐时间。"""

    tick: Tick
    verdict: StateVerdict
    decision: Decision


@dataclass(frozen=True)
class Event:
    start: datetime
    end: datetime
    ticks: tuple[Evaluation, ...]

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


def run_day(
    ticks: Iterable[Tick], config: Config, *, thresholds: ThresholdConfig | None = None
) -> tuple[Evaluation, ...]:
    """按真实路径跑一天：判定 → 闸门 → 决策，并逐轮维持冷却与每日上限的状态。

    刻意复用生产的 `classify` 与 `decide`，而不是在测试里重写一遍规则 ——
    重写的版本只会证明测试自己自洽。
    """
    last_at: datetime | None = None
    last_action: str | None = None
    today = 0
    out: list[Evaluation] = []
    for tick in ticks:
        verdict = classify(
            tick.snapshot(config.schedule.window_minutes),
            config.taxonomy,
            thresholds or config.thresholds,
            fullscreen_state=tick.fullscreen_state,
        )
        decision = decide(
            GateContext(
                verdict=verdict,
                config=config.gate,
                now=tick.at,
                last_intervention_at=last_at,
                interventions_today=today,
            ),
            config.actions,
            last_action,
        )
        if decision.intervene:
            last_at = tick.at
            last_action = decision.action_id
            today += 1
        out.append(Evaluation(tick=tick, verdict=verdict, decision=decision))
    return tuple(out)


def _merge_runs(
    evals: Sequence[Evaluation],
    *,
    predicate: Callable[[Evaluation], bool],
    gap_minutes: float,
) -> tuple[Event, ...]:
    """把满足 `predicate` 的连续轮次并成一次事件。

    两件事同时成立才算同一次事件：轮次相邻且间隔不超过 `gap_minutes`。
    只按「状态相同」合并会把关机前后的两段算成一段 —— 那是两段时间上不连续的
    行为，合并后「事件时长」就没有意义了。
    """
    runs: list[list[Evaluation]] = []
    for ev in evals:
        if not predicate(ev):
            continue
        contiguous = runs and (ev.tick.at - runs[-1][-1].tick.at) <= timedelta(minutes=gap_minutes)
        if contiguous:
            runs[-1].append(ev)
        else:
            runs.append([ev])
    return tuple(Event(r[0].tick.at, r[-1].tick.at, tuple(r)) for r in runs)


def _is_consuming(ev: Evaluation) -> bool:
    return ev.verdict.state in INTERVENABLE


def _first_failed_name(ev: Evaluation) -> str | None:
    gate = first_failed(ev.decision.gate_trace)
    return gate.name if gate is not None else None


@dataclass(frozen=True)
class DayMetrics:
    """事件级评价的结果。字段名与计划里要统计的量一一对应。"""

    true_events: int
    system_events: int
    missed_events: int
    false_positive_events: int
    #: 每个真事件：第一次**判定到**可干预状态距事件起点的分钟数；None = 从未判到。
    first_remindable_delays: tuple[float | None, ...]
    #: 每个真事件：第一次真的投递距事件起点的分钟数；None = 从未提醒。
    first_popup_delays: tuple[float | None, ...]
    popups: int
    #: 状态已达标、但第一个未通过的闸门是 ratio_min 的轮次数与事件数。
    ratio_blocked_ready_ticks: int
    ratio_blocked_ready_events: int
    labels: tuple[str, ...] = field(default=())


def measure(
    evals: Sequence[Evaluation],
    *,
    cadence: float = CADENCE,
    gap_minutes: float = GAP,
    tolerance_minutes: float | None = None,
) -> DayMetrics:
    """事件级评价。口径就在这里，只此一处。

    `tolerance_minutes` 默认一个评估节奏，原因是**窗口是回看的**：一段 30 分钟的
    短事件，其积累量只有到事件结束后的下一轮才可能进满窗口。不放这个容差，
    「判定滞后一轮」会被记成漏判，而那不是分类或阈值的问题。
    """
    tolerance = cadence if tolerance_minutes is None else tolerance_minutes
    true_events = _merge_runs(evals, predicate=lambda e: e.tick.label_passive, gap_minutes=gap_minutes)
    system_events = _merge_runs(evals, predicate=_is_consuming, gap_minutes=gap_minutes)

    def _within(event: Event) -> list[Evaluation]:
        limit = event.end + timedelta(minutes=tolerance)
        return [e for e in evals if event.start <= e.tick.at <= limit]

    missed = 0
    remindable: list[float | None] = []
    popup_delays: list[float | None] = []
    for event in true_events:
        inside = _within(event)
        ready = next((e for e in inside if _is_consuming(e)), None)
        if ready is None:
            missed += 1
            remindable.append(None)
        else:
            remindable.append(round((ready.tick.at - event.start).total_seconds() / 60, 1))
        delivered = next((e for e in inside if e.decision.intervene), None)
        popup_delays.append(
            None if delivered is None
            else round((delivered.tick.at - event.start).total_seconds() / 60, 1)
        )

    false_positives = sum(
        1 for event in system_events if not any(e.tick.label_passive for e in event.ticks)
    )
    blocked_ticks = [e for e in evals if _is_consuming(e) and _first_failed_name(e) == GATE_RATIO_MIN]
    blocked_events = sum(
        1
        for event in system_events
        if any(_first_failed_name(e) == GATE_RATIO_MIN for e in event.ticks)
    )

    return DayMetrics(
        true_events=len(true_events),
        system_events=len(system_events),
        missed_events=missed,
        false_positive_events=false_positives,
        first_remindable_delays=tuple(remindable),
        first_popup_delays=tuple(popup_delays),
        popups=sum(1 for e in evals if e.decision.intervene),
        ratio_blocked_ready_ticks=len(blocked_ticks),
        ratio_blocked_ready_events=blocked_events,
        labels=tuple(event.ticks[0].tick.reason for event in true_events),
    )


def consumption_events(evals: Sequence[Evaluation], gap_minutes: float = GAP) -> tuple[Event, ...]:
    """系统视角的消费事件。与真事件用同一套合并规则，只有谓词不同。"""
    return _merge_runs(evals, predicate=_is_consuming, gap_minutes=gap_minutes)


# ── 任务 1.3：候选阈值比较 ──────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    name: str
    watch_minutes: float
    passive_minutes: float
    high_risk_minutes: float
    ratio_min: float

    def apply(self, config: Config) -> Config:
        return replace(
            config,
            thresholds=replace(
                config.thresholds,
                watch_minutes=self.watch_minutes,
                passive_minutes=self.passive_minutes,
                high_risk_minutes=self.high_risk_minutes,
            ),
            gate=replace(config.gate, ratio_min=self.ratio_min),
        )


#: 现值 + 一个「把门槛整体压低」的候选。候选只有两个，是因为本文件要验证的是
#: 比较这件事本身跑得通、且结论与灵敏度方向一致；穷举候选集是标注数据到位后的事。
CANDIDATES: tuple[Candidate, ...] = (
    Candidate("current", 20, 40, 55, 0.75),
    Candidate("eager", 25, 25, 50, 0.5),
)


def compare_candidates(
    ticks: Sequence[Tick], config: Config, candidates: Sequence[Candidate] = CANDIDATES
) -> dict[str, DayMetrics]:
    """在同一批标注轮次上比较候选阈值。返回「候选名 → 事件级指标」。

    这是只读、离线的纯函数：不改配置、不写库、不碰生产文件。候选规则不得直接改
    生产配置（计划 1.3 的硬边界）。
    """
    return {
        candidate.name: measure(run_day(ticks, candidate.apply(config)))
        for candidate in candidates
    }


# ── 语料自身的守卫 ─────────────────────────────────────────────────


def test_corpus_segments_land_in_the_categories_this_file_assumes(config: Config):
    """语料的自我守卫：每段样本必须先落在预期类别里，否则整份校准是空转。

    例如示例清单哪天补上了 `brotato`，后面「窗口化游戏漏判」的案例就不成立了 ——
    那时应当**改语料**，而不是让一条测试继续通过。
    """
    expected = {
        "statesense - Visual Studio Code": Category.WORK,
        "【某视频】_哔哩哔哩_bilibili": Category.ENTERTAINMENT,
        "Windows PowerShell": Category.WORK,
        "long-paper.pdf - Adobe Acrobat": Category.OTHER,
        "Brotato": Category.OTHER,
    }
    for segment in DAY:
        entry = Entry(segment.app, segment.title, segment.url, 1.0)
        assert classify_entry(entry, config.taxonomy) is expected[segment.title], segment.title

    # 全屏终端 + 状态 2 是本文件的关键输入：它必须真的落在 WORK，否则
    # 「终端不会被提权」测的就不是分类的作用，而是别的什么东西。
    terminal = next(s for s in DAY if s.title == "Windows PowerShell")
    assert terminal.fullscreen == BUSY


def test_corpus_has_no_accidental_activity_in_the_absent_ranges():
    """缺口区间必须真的没有轮次，否则「连续性缺口」只是名字。"""
    ticks = build_ticks(DAY, absent=ABSENT)
    for low, high in ABSENT:
        assert not [t for t in ticks if low <= (t.at - NOON).total_seconds() // 60 < high]
    # 缺口两侧的轮次间隔必须超过连续性阈值，否则合并不了成两个事件。
    gaps = [(b.at - a.at).total_seconds() / 60 for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) > GAP


# ── 任务 1.1：事件级口径 ────────────────────────────────────────────


def test_overlapping_windows_collapse_into_one_event(config: Config):
    """核心口径：窗口重叠时，连续多轮只是同一个消费事件的多次采样。

    全屏文档那一段连续 11 轮可干预。按轮次计数会得到「11 个样本」，
    按事件计数得到 1 个 —— 计划 1.1 要防的就是前者把一处问题读成很多处。
    """
    evals = run_day(build_ticks(DAY, absent=ABSENT), config)
    events = consumption_events(evals)
    doc_events = [e for e in events if e.start == NOON + timedelta(minutes=220)]

    assert len(doc_events) == 1
    assert len(doc_events[0].ticks) == 11
    assert doc_events[0].minutes == 50.0
    assert doc_events[0].ticks[0].tick.reason.startswith("全屏读文档")


def test_a_machine_off_gap_splits_one_episode_into_two_events(config: Config):
    """同一个人、同一件事，中间关机 25 分钟 → 两次事件，不是一次。

    只按「状态相同」合并会得到一段 60 分钟的事件，再拿它算「首次提醒延迟」
    就没有意义了。事件是**时间上连续**的行为段，不是「状态相同的一堆轮次」。
    """
    segments = (Segment(0, 60, "chrome.exe", "【某视频】_哔哩哔哩_bilibili", NOT_FULLSCREEN,
                        label_passive=True, reason="刷视频"),)
    ticks = build_ticks(segments, absent=((25, 45),))
    events = _merge_runs(
        run_day(ticks, config), predicate=lambda e: e.tick.label_passive, gap_minutes=GAP
    )
    assert [e.minutes for e in events] == [20.0, 15.0]


# ── 任务 1.2 / 1.3：合成语料上的结论 ────────────────────────────────


@pytest.fixture()
def day_ticks() -> tuple[Tick, ...]:
    return build_ticks(DAY, absent=ABSENT)


def test_current_config_on_the_synthetic_day(config: Config, day_ticks):
    """现值在合成语料上的读数。**这不是真实分布**，是口径的标定点。

    两个漏判、一个误报各有各的原因，读数本身不重要，重要的是它们分得开：
      · 30 分钟短视频：真消费，ent 峰值 30 < passive 40 → 判不到（阈值问题）
      · 窗口化游戏：ent 恒为 0 → 判不到（分类问题）
      · 全屏文档：ent 满窗 60 → HIGH_RISK（全屏规则问题）
    """
    metrics = measure(run_day(day_ticks, config))

    assert metrics.true_events == 2
    assert metrics.system_events == 1
    assert metrics.missed_events == 2
    assert metrics.false_positive_events == 1
    assert metrics.first_remindable_delays == (None, None)
    assert metrics.popups == 2

    # 计划 1.1 的验收要求「任一图表都能指出样本日期、有效窗口数和消费事件数」，
    # 下面三行就是那三项在测试里的形态：57 轮有效窗口，2 次事件，各自带原因。
    assert len(day_ticks) == 57
    assert day_ticks[0].at.date() == NOON.date()
    assert metrics.labels[0].startswith("明确在刷 B 站")
    assert metrics.labels[1].startswith("窗口化打游戏")


def test_each_candidate_carries_its_own_rule_version(config: Config):
    """候选阈值各自是一套规则，因此各有各的版本号 —— 两者的样本不可混算。

    这条把阶段 1.1 落库的那一列与阶段 1.3 的比较接起来：库里两批行的版本号不同，
    它们就不是同一批样本。真实数据到位后，比较的第一步就是按这一列分组，
    而不是先算出一个跨版本的均值。
    """
    versions = {candidate.name: version_of(candidate.apply(config)) for candidate in CANDIDATES}
    assert versions["current"] == version_of(config)
    assert versions["eager"] != versions["current"]
    assert len(set(versions.values())) == len(CANDIDATES)


def test_no_threshold_candidate_rescues_a_classification_miss(config: Config, day_ticks):
    """阈值调不动分类漏判 —— 这条结论不依赖真实数据，它是口径的性质。

    窗口化游戏的 ent 恒为 0：任何把门槛压低的候选都只能让**已经命中的**轮次更早
    触发，命不中的仍然是 0。计划 1.2 与 1.3 的分工因此不能颠倒：
    先修分类，再谈阈值。
    """
    results = compare_candidates(day_ticks, config)
    game_start = NOON + timedelta(minutes=345)
    for name, metrics in results.items():
        assert metrics.missed_events >= 1, name
        evals = run_day(day_ticks, next(c for c in CANDIDATES if c.name == name).apply(config))
        game = [e for e in evals if e.tick.at >= game_start]
        assert max(e.verdict.ent_minutes for e in game) == 0.0, name


def test_lowering_the_ladder_trades_missed_events_for_popups(config: Config, day_ticks):
    """阈值能修的是「阈值造成的漏判」，代价是弹窗变多 —— 用同一批事件量化。

    候选 eager（25 / 25 / 50，ratio_min 0.5）救回了 30 分钟短视频那一次漏判
    （首判在事件起点后 25 分钟，当时还被 ratio_min 挡下），但弹窗从 2 次涨到 5 次：
    多出来的三次里，有两次是对**已经结束**的视频的余波提醒（窗口里还留着那段视频）。
    这正是计划 1.1 说的「窗口重叠」，也是阶段 1.4 判断「是否引入额外弹窗」的依据。
    """
    results = compare_candidates(day_ticks, config)
    current, eager = results["current"], results["eager"]

    assert eager.missed_events < current.missed_events
    assert eager.popups > current.popups
    assert eager.first_remindable_delays[0] == 25.0
    assert eager.first_popup_delays[0] == 30.0


def test_state_ready_but_ratio_blocked_is_counted_per_event(config: Config, day_ticks):
    """计划 1.3 要求单独列出「被 ratio_min 单独挡住的已达状态事件」。

    现值下这类事件为 0：能进可干预的轮次（ent ≥ 40）在满窗上的比例都 ≥ 0.667，
    而 ratio_min 是 0.75，所以挡下的都是 ent 更小的轮次 —— 那些本来就没达标。
    压低门槛后才出现：短视频那一轮 ent=25、ratio≈0.455 被判到却被比例挡下，
    也就是计划 1.2 那个「该提醒还是该安静」的真实形态。
    """
    results = compare_candidates(day_ticks, config)

    assert results["current"].ratio_blocked_ready_events == 0
    assert results["eager"].ratio_blocked_ready_events == 1
    assert results["eager"].ratio_blocked_ready_ticks >= 1


def _snapshot(config: Config, ent_minutes: float, total: float = 60.0) -> ActivitySnapshot:
    """满窗快照：`ent_minutes` 分钟的娱乐 + 其余在工作。用于扫阈值边界。"""
    window = config.schedule.window_minutes
    items = [Entry("chrome.exe", "【某视频】_哔哩哔哩_bilibili", "", ent_minutes)]
    if total - ent_minutes > 0:
        items.append(Entry("Code.exe", "statesense - Visual Studio Code", "", total - ent_minutes))
    return ActivitySnapshot(
        window_start=NOON - timedelta(minutes=window),
        window_end=NOON,
        window_minutes=window,
        total_active_minutes=total,
        entries=tuple(items),
        data_status="ok",
        captured_at=NOON,
    )


def test_every_candidate_keeps_high_risk_reachable_and_state_min_intact(config: Config):
    """候选阈值必须仍然满足两条验收线：HIGH_RISK 可达、state_min 不被绕过。

    在候选**自己的配置**上验，而不是只验 `CANDIDATES` 的字段 ——
    字段合法不等于行为合法（比如哪天有人把 high_risk 改到窗口之外）。
    """
    window = config.schedule.window_minutes
    for candidate in CANDIDATES:
        applied = candidate.apply(config)
        assert candidate.passive_minutes <= candidate.high_risk_minutes <= window, candidate.name

        full = classify(
            _snapshot(config, float(window)),
            applied.taxonomy,
            applied.thresholds,
        )
        assert full.state is State.HIGH_RISK_PASSIVE_CONSUMPTION, candidate.name

        quiet = classify(_snapshot(config, 5.0), applied.taxonomy, applied.thresholds)
        assert quiet.state is State.NORMAL, candidate.name
        gate = next(
            r
            for r in run_gates(
                GateContext(quiet, applied.gate, NOON, last_intervention_at=None,
                            interventions_today=0)
            )
            if r.name == GATE_STATE_MIN
        )
        assert gate.passed is False, f"{candidate.name}: 低阈值把 state_min 绕过了"

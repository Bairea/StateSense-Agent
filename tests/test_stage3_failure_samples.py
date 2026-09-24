"""阶段 3.1 的失败样本集：把「模型要解的问题」钉成可执行的断言。

计划任务 3.1 的完成条件是「固定、可复查且脱敏的评估样本和明确的目标指标」。
本文件做三件事：

  1. 把合成语料（复用阶段 1 的 `DAY` 与事件级脚手架，**不另造一份** ——
     口径与语料各只此一处）上的规则基线读数逐类钉死。分类清单、阈值或
     提权规则任何一项改动都会让这里的断言失败，强制重看：失败样本集变了，
     模型的目标指标就得重新对表，而不是沿用旧数字。
  2. 钉死「输入可分性」：在影子输入白名单（`shadow/models.py` 的六个原子量）
     下，哪些失败类对模型结构性可见、哪些与别的场景是**同一个点**。
     这直接决定阶段 3.4 判定规格里「模型信号只能作用于哪些模糊窗口」——
     对输入空间里不可分的两类，任何模型都救不了，先写清楚免得白跑影子。
  3. 钉住护栏样本：全屏真游戏必须仍然可达 PASSIVE 以上 ——
     任何针对全屏文档误报的规则修复，不得把它一并压掉。

语料是合成的，不代表真实分布；本文件断言的是**口径的性质**，不是用户行为。
目标指标本身与「规则先行的处置」写在
`docs/plans/2026-09-24-stage-3-failure-samples.md`，那里是给人评审的规格，
这里是让它无法悄悄漂移的锁。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from statesense.activity.models import Entry
from statesense.perception import QUNS_ACCEPTS_NOTIFICATIONS, QUNS_BUSY
from statesense.state.models import State
from statesense.state.taxonomy import Category, classify as classify_entry

from test_stage1_event_calibration import (
    ABSENT,
    CADENCE,
    DAY,
    NOON,
    Segment,
    build_ticks,
    measure,
    run_day,
)

NOT_FULLSCREEN = QUNS_ACCEPTS_NOTIFICATIONS

#: 阶段 3.1 的护栏语料变体：原合成日 + 一段**全屏真游戏**。
#:
#: 原语料的游戏段是窗口化的（F2 漏判样本），没有「全屏真游戏」——于是
#: 「任何针对 F3（全屏文档误报）的规则修复会不会误伤全屏游戏」无样本可依。
#: 变体在缺口之后加一段全屏游戏：缺口保证它不被 Brotato 的残留分钟污染
#: （不隔开的话，435 之后每一轮的窗口都是满窗 OTHER，两个事件会合并成一个）。
GAME_SEGMENT = Segment(
    480, 90, "SomeGame.exe", "SomeGame", QUNS_BUSY,
    label_passive=True,
    reason="全屏打游戏，真被动消费（护栏样本）",
)
GUARD_DAY: tuple[Segment, ...] = (*DAY, GAME_SEGMENT)
GUARD_ABSENT: tuple[tuple[int, int], ...] = (*ABSENT, (440, 480))


# ── 基线读数（current 20/40/55, ratio 0.75） ────────────────────────


def test_baseline_readings_are_pinned_per_failure_class(config):
    """规则基线在合成日上的全部读数。改规则必改此测试，且必须说明为什么。

    最扎眼的一条：**全部 2 次弹窗都落在唯一一个误报事件上**（全屏文档）。
    现状不是「提醒得太多」，是「提醒的对象错了」。
    """
    evals = run_day(build_ticks(DAY, absent=ABSENT), config)
    metrics = measure(evals)

    assert metrics.true_events == 2
    assert metrics.system_events == 1
    assert metrics.missed_events == 2
    assert metrics.false_positive_events == 1
    assert metrics.first_remindable_delays == (None, None)
    assert metrics.first_popup_delays == (None, None)
    assert metrics.popups == 2
    assert metrics.ratio_blocked_ready_events == 0

    # 两个真事件按起点排序：短视频在前，窗口化游戏在后。
    assert metrics.labels[0].startswith("明确在刷 B 站")
    assert metrics.labels[1].startswith("窗口化打游戏")

    # 仅有的系统事件就是误报（全屏文档），两次弹窗全在里面。
    events = __import__("test_stage1_event_calibration").consumption_events(evals)
    assert len(events) == 1
    assert events[0].start == NOON + timedelta(minutes=220)
    assert sum(1 for ev in events[0].ticks if ev.decision.intervene) == 2


# ── 逐类钉死 ────────────────────────────────────────────────────────


def test_f1_short_video_miss_is_a_threshold_miss(config):
    """F1：30 分钟短视频，ent 峰值 30 < passive 40 —— 阈值问题。

    阶段 1 已裁决「压低阈值可救但 +3 弹窗（2 次余波）」并保留现值；
    模型的目标是在**不增加弹窗**的前提下救回这一类。这里钉的是问题本身。
    """
    evals = run_day(build_ticks(DAY, absent=ABSENT), config)
    metrics = measure(evals)

    assert metrics.labels[0].startswith("明确在刷 B 站")
    assert metrics.first_remindable_delays[0] is None

    # 事件窗 [30, 60]（含一个评估节奏的容差）内 ent 峰值恰为 30。
    lo = NOON + timedelta(minutes=30)
    hi = NOON + timedelta(minutes=60)
    ents = [
        ev.verdict.ent_minutes
        for ev in evals
        if lo <= ev.tick.at <= hi
    ]
    assert max(ents) == 30.0


def test_f2_windowed_game_miss_is_a_classification_miss(config):
    """F2：窗口化游戏 90 分钟，ent 恒为 0 —— 分类问题，阈值救不回。

    「任何阈值候选都救不回」已由阶段 1
    （test_no_threshold_candidate_rescues_a_classification_miss）钉死；
    这里钉的是失败形态本身：整段 ent 全 0，首次可判时间不存在。
    """
    evals = run_day(build_ticks(DAY, absent=ABSENT), config)
    metrics = measure(evals)

    assert metrics.labels[1].startswith("窗口化打游戏")
    assert metrics.first_remindable_delays[1] is None

    lo = NOON + timedelta(minutes=345)
    hi = NOON + timedelta(minutes=430)
    game_ticks = [ev for ev in evals if lo <= ev.tick.at <= hi]
    assert game_ticks, "护栏：语料改动后游戏段必须有轮次"
    assert all(ev.verdict.ent_minutes == 0.0 for ev in game_ticks)


def test_f3_fullscreen_document_false_positive_carries_every_popup(config):
    """F3：全屏文档 90 分钟被提权成满窗娱乐 → HIGH_RISK 误报。

    机理：QUNS_BUSY 被实测认定为「可能在玩游戏」（真游戏多返回 2），
    而 BUSY 同样覆盖全屏文档；提权只看 QUNS、不看内容 ——
    这是为「不维护游戏清单」付出的已知代价（perception.py）。
    """
    evals = run_day(build_ticks(DAY, absent=ABSENT), config)
    metrics = measure(evals)

    assert metrics.false_positive_events == 1
    assert metrics.popups == 2

    lo = NOON + timedelta(minutes=220)
    doc_ticks = [ev for ev in evals if lo <= ev.tick.at <= NOON + timedelta(minutes=270)]
    assert doc_ticks, "护栏：语料改动后文档段必须有轮次"
    assert all(ev.verdict.fullscreen_state == QUNS_BUSY for ev in doc_ticks)
    assert max(ev.verdict.ent_minutes for ev in doc_ticks) == 60.0
    # 误报的完整形态：ent 一过 passive(40) 弹窗就发出（首弹在 220，此时只是
    # PASSIVE 档），窗口填满后升到 HIGH_RISK——降级修复救不了弹窗，只降档。
    assert doc_ticks[0].verdict.state is State.PASSIVE_CONSUMPTION
    assert any(ev.verdict.state is State.HIGH_RISK_PASSIVE_CONSUMPTION for ev in doc_ticks)


# ── 输入可分性（影子白名单下模型能看到什么） ────────────────────────


def _fingerprint(tick, config):
    """一轮的影子输入指纹 `(ent, gray, work, total)`。

    **ent 必须走生产公式**：影子输入里的 `ent_minutes` 是提权后的值
    （`effective_entertainment_minutes`），不是原始 ENTERTAINMENT 桶——
    全屏文档提权后 ent=60，直接读桶会把它错记成 (0,0,0,60)，与窗口化游戏
    同点，可分性结论就全错了。这里取 `classify` 的裁决值，与 Scheduler
    构造 `StateShadowInput` 用的是同一条路径。白名单里没有 `other`，但它是
    `total - 其余三项`，模型可自行导出，四元组就是模型的全部可见信息。
    """
    from statesense.state.engine import classify

    verdict = classify(
        tick.snapshot(config.schedule.window_minutes),
        config.taxonomy,
        config.thresholds,
        fullscreen_state=tick.fullscreen_state,
    )
    return (
        verdict.ent_minutes,
        verdict.gray_minutes,
        verdict.work_minutes,
        round(tick.total_active_minutes, 2),
    )


def test_f2_f3_f1_are_pairwise_distinguishable_in_the_shadow_input(config):
    """三类失败样本在影子输入空间里两两不同 —— 模型**分得开**它们。

    指纹（ent, gray, work, total）：
      · 窗口化游戏  (0, 0, 0, 60)   —— other 隐含 60：满窗未归类；
      · 全屏文档    (60, 0, 0, 60)  —— ent 已含提权的 60；
      · 短视频窗口  (30, 0, 30, 60) —— 娱乐/工作各半。
    可分是「模型有资格试」的前提；分不开的类连试都不该试（见下一条）。
    """
    ticks = build_ticks(DAY, absent=ABSENT)

    def tick_at(offset: int):
        found = [t for t in ticks if t.at == NOON + timedelta(minutes=offset)]
        assert found, offset
        return found[0]

    # 取各自「窗口最满」的一轮：游戏 430、文档 270（段到 270 结束）都是满窗；
    # 视频事件本身只到 60 分钟处就换主人（owner 切到终端），所以它的指纹取
    # 60 分钟那轮——视频 30 分钟娱乐 + 终端 30 分钟工作，正是它峰值也够不到
    # 40 的形态。
    game = _fingerprint(tick_at(430), config)
    doc = _fingerprint(tick_at(270), config)
    video = _fingerprint(tick_at(60), config)

    assert game == (0.0, 0.0, 0.0, 60.0)
    assert doc == (60.0, 0.0, 0.0, 60.0)
    assert video == (30.0, 0.0, 30.0, 60.0)
    assert len({game, doc, video}) == 3


def test_f2_fingerprint_is_ambiguous_against_unclassified_work(config):
    """F2 的指纹与「任何未命中清单的满窗应用」是同一个点 —— 歧义必须写明。

    「窗口化游戏」与「用没进清单的 Word 写报告」在影子输入里完全相同
    （other 满窗、ent=0）。模型即使给出候选，也分不清是哪一种；
    3.4 的判定规格若采信这类候选，必须连同这个歧义一起采信。
    """
    ticks = build_ticks(
        (
            Segment(
                0, 60, "Word.exe", "report.docx - Microsoft Word", NOT_FULLSCREEN,
                label_passive=False,
                reason="未命中清单的写作应用（歧义对照）",
            ),
        ),
    )

    # 自我守卫：这份对照样本必须真的落在 OTHER（未归类）。哪天示例清单
    # 加了 Word，这条就失败 —— 届时应改语料，而不是让断言空转。
    entry = Entry("Word.exe", "report.docx - Microsoft Word", "", 1.0)
    assert classify_entry(entry, config.taxonomy) is Category.OTHER

    fingerprint = _fingerprint(ticks[-1], config)
    assert fingerprint == (0.0, 0.0, 0.0, 60.0)


# ── 护栏样本：全屏真游戏 ────────────────────────────────────────────


def test_fullscreen_game_reaches_high_risk_today(config):
    """护栏基线：现行规则下，全屏真游戏被正确判到 HIGH_RISK。

    这是 F3 误报与「真全屏游戏」共享同一机理的另一面：**修 F3 的任何
    规则改动都不得把这一段压到 PASSIVE 以下**。本条断言就是那条护栏的
    可执行形态——动提权规则的人必须同时改这条测试并说明为什么。
    """
    evals = run_day(build_ticks(GUARD_DAY, absent=GUARD_ABSENT), config)
    metrics = measure(evals)

    assert metrics.true_events == 3
    assert metrics.labels[2].startswith("全屏打游戏")

    # 短视频与窗口化游戏的漏判在变体语料上保持原样（缺口隔开了两段，
    # 全屏游戏的提权不会把 Brotato 的残留分钟顺手救回来）。
    assert metrics.first_remindable_delays[0] is None
    assert metrics.first_remindable_delays[1] is None

    # 全屏真游戏：ent 随窗口积累，40 分钟时够到 PASSIVE，55 分钟到 HIGH_RISK。
    assert metrics.first_remindable_delays[2] == 40.0
    lo = NOON + timedelta(minutes=520)
    hi = NOON + timedelta(minutes=540)
    tail = [ev for ev in evals if lo <= ev.tick.at <= hi]
    states = {ev.verdict.state for ev in tail}
    assert State.PASSIVE_CONSUMPTION in states
    assert State.HIGH_RISK_PASSIVE_CONSUMPTION in states

    # F3 的误报在变体语料上原样保留（缺口不影响文档段）。
    assert metrics.false_positive_events == 1


@pytest.mark.parametrize(
    "segments, absent",
    [(DAY, ABSENT), (GUARD_DAY, GUARD_ABSENT)],
    ids=["day", "guard-day"],
)
def test_cadence_and_gap_assumptions_hold(segments, absent):
    """语料守卫：轮次节奏与连续性阈值没被悄悄改掉。

    阶段 1 的读数是在 CADENCE=5 / GAP=15 下算的；这两个值进了示例配置的
    默认项，但测试若直接读配置就测不出「默认值本身变了」。这里把它们
    与阶段 1 模块的常量绑死。
    """
    from test_stage1_event_calibration import GAP

    assert (CADENCE, GAP) == (5, 15)
    ticks = build_ticks(segments, absent=absent)
    assert ticks, "语料不得为空"

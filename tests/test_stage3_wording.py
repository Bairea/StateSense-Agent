"""阶段 3.3 的离线文案评估：接口拆分、隐私边界、失败回退与等价性。

计划任务 3.3 的完成条件是「同一批干预机会下触发次数与动作 ID 和规则基线
一致；变化仅限文案，失败时不产生额外弹窗」。本文件四组测试对应四件事：

  1. 契约镜像：`WordingContext` 的字段就是文档清单，传输面碰不到私密字段；
  2. 隐私边界：候选实现拿到的上下文里没有窗口标题（反向对照：模板自己
     会带标题——本机弹窗允许，这正是判别器必须存在的原因）；
  3. 失败回退：候选抛错或返回空文案时，正文退回模板、回退计数加一，
     异常绝不外逃（外逃会被 tick 容错吃成 tick_error，投递就丢了）；
  4. 回放等价性：同一条时间线上，模板与候选（含全程失败的候选）的
     触发次数、动作 ID、时刻逐行一致——变化的只有正文。

替身是 `ScriptedRemoteWording`，**它不是模型**；长度 / 重复 / 泄露 / 耗时
这些比较指标在这里只证明「尺子能用」，真实模型的价值要等真实样本。
"""

from __future__ import annotations

import dataclasses
import json
from time import perf_counter

import pytest

from statesense.config import Action
from statesense.intervention.wording import (
    CONTEXT_FIELDS,
    WORDING_CONTEXT_VERSION,
    WORDING_PRIVATE_FIELDS,
    RemoteWordingAdapter,
    ScriptedRemoteWording,
    TemplateWording,
    WordingContext,
)
from statesense.replay.runner import run_scenario
from statesense.replay.scenarios import SCENARIOS, START, _advance
from statesense.state.models import State, StateVerdict
from statesense.state.taxonomy import Category

from test_wording import WALK

#: 回放剧本的轮数：90 分钟、每 5 分钟一轮（与影子剧本同款）。
_TICKS = 19


def _verdict(
    *,
    state: State = State.PASSIVE_CONSUMPTION,
    ent: float = 45.0,
    gray: float = 0.0,
    work: float = 10.0,
    total: float = 60.0,
    late_night: bool = False,
    window: int = 60,
) -> StateVerdict:
    ratio = ent / total if total else 0.0
    return StateVerdict(
        state=state,
        late_night=late_night,
        total_active_minutes=total,
        ent_minutes=ent,
        gray_minutes=gray,
        work_minutes=work,
        ent_ratio=ratio,
        entries_minutes=total,
        fullscreen_state=None,
        window_minutes=window,
        data_status="ok",
        skipped=False,
        skip_reason=None,
    )


# ── 契约镜像 ────────────────────────────────────────────────────────


def test_context_fields_match_the_documented_allowlist():
    """`WordingContext` 的字段与文档清单逐字一致，且不碰任何私密字段。

    与 shadow 的镜像测试同一纪律：字段集是「候选实际看到什么」的唯一答案。
    加字段必须同时改 CONTEXT_FIELDS 与 payload，只改一半这里就红。
    """
    fields = tuple(f.name for f in dataclasses.fields(WordingContext))
    assert fields == CONTEXT_FIELDS
    assert not set(CONTEXT_FIELDS) & set(WORDING_PRIVATE_FIELDS)
    assert WORDING_CONTEXT_VERSION == "wording-context@v1"


def test_payload_carries_exactly_the_allowlist_and_is_json_ready():
    """载荷逐字段对应白名单，且可以直接序列化传输。"""
    context = WordingContext(
        state=State.PASSIVE_CONSUMPTION,
        late_night=False,
        ent_minutes=45.0,
        gray_minutes=0.0,
        work_minutes=10.0,
        total_active_minutes=60.0,
        window_minutes=60,
        ent_ratio=0.75,
        top_category=Category.ENTERTAINMENT,
        action_id="walk5",
        action_text="离开电脑走 5 分钟",
    )
    payload = context.payload()
    assert list(payload) == list(CONTEXT_FIELDS)
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"ent": 45.0, "gray": 0.0, "work": 10.0, "total": 60.0}, Category.ENTERTAINMENT),
        ({"ent": 0.0, "gray": 0.0, "work": 60.0, "total": 60.0}, Category.WORK),
        ({"ent": 0.0, "gray": 60.0, "work": 0.0, "total": 60.0}, Category.GRAY),
        ({"ent": 0.0, "gray": 0.0, "work": 0.0, "total": 60.0}, Category.OTHER),
        # 并列取更重的一类：弹窗正文按娱乐分钟说话，类别口径与正文一致。
        ({"ent": 30.0, "gray": 0.0, "work": 30.0, "total": 60.0}, Category.ENTERTAINMENT),
    ],
)
def test_top_category_tracks_the_dominant_bucket(kwargs, expected):
    """`top_category` 是 top_label 的隐私安全替身，必须跟住主导类别。"""
    scripted = ScriptedRemoteWording(["x"])
    adapter = RemoteWordingAdapter(scripted)

    adapter.render(_verdict(**kwargs), WALK, None)

    assert scripted.calls[0].top_category is expected


# ── 隐私边界 ────────────────────────────────────────────────────────


def test_candidate_never_sees_the_window_title():
    """候选拿到的载荷里没有标题；模板自己有——判别器真的在分辨。"""
    scripted = ScriptedRemoteWording(["候选文案"])
    adapter = RemoteWordingAdapter(scripted)
    title = "SECRET-视频标题XYZ"

    text = adapter.render(_verdict(), WALK, title)

    assert text == "候选文案"
    assert len(scripted.calls) == 1
    assert "SECRET" not in json.dumps(scripted.calls[0].payload())
    # 反向对照：模板正文确实嵌标题。没有这条，上面的「没有」只是巧合。
    assert "SECRET" in TemplateWording().render(_verdict(), WALK, title)


# ── 失败回退 ────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [RuntimeError("模型进程炸了"), "", "   "])
def test_failure_and_empty_text_fall_back_to_the_template(bad):
    """候选抛错或给空文案：正文退回模板、回退计数加一、异常不外逃。

    模板回退文案必须和「从头就用模板」逐字一致——失败的代价只是一句
    模板，不多（没有额外弹窗）也不少（投递照常）。
    """
    scripted = ScriptedRemoteWording([bad])
    adapter = RemoteWordingAdapter(scripted)

    text = adapter.render(_verdict(late_night=True), WALK, None)

    assert text == TemplateWording().render(_verdict(late_night=True), WALK, None)
    assert "凌晨" in text, "回退走的必须是模板，不是某个碰巧像的字符串"
    assert adapter.fallbacks == 1


# ── 回放等价性（完成条件本体） ──────────────────────────────────────


def _run_with_wording(config, tmp_path, name: str, wording):
    run = run_scenario(
        SCENARIOS["ladder"],
        config,
        start=START,
        db_path=tmp_path / f"{name}.db",
        wording=wording,
    )
    try:
        _advance(run, _TICKS)
        return run.store.list_interventions()
    finally:
        run.store.close()


def test_candidate_wording_changes_only_the_text(config, tmp_path):
    """同一条时间线：候选文案只改正文，触发时刻 / 动作 ID / 状态逐行一致。"""
    control = _run_with_wording(config, tmp_path, "control", None)
    assert len(control) >= 2, "剧本投递不足两次，重复性与等价性断言都会空转"

    scripted = ScriptedRemoteWording(
        [f"候选文案第 {i} 句" for i in range(len(control))]
    )
    candidate = _run_with_wording(
        config, tmp_path, "candidate", RemoteWordingAdapter(scripted)
    )

    key = lambda rows: [(r["at"], r["action_id"], r["state"]) for r in rows]
    assert key(candidate) == key(control)
    # 正文真的换了——否则「只变文案」这条完成条件什么也没验证。
    assert [r["action_text"] for r in candidate] != [
        r["action_text"] for r in control
    ]
    assert len(scripted.calls) == len(control), "每次投递恰好问一次候选"


def test_failing_candidate_still_delivers_the_template_text(config, tmp_path):
    """候选全程失败：投递次数与正文和基线完全一致——失败不多弹、不少弹。"""
    control = _run_with_wording(config, tmp_path, "control", None)
    failing = _run_with_wording(
        config,
        tmp_path,
        "failing",
        RemoteWordingAdapter(ScriptedRemoteWording([RuntimeError("挂了")] * len(control))),
    )

    assert len(failing) == len(control)
    assert [r["action_text"] for r in failing] == [r["action_text"] for r in control]
    assert [r["action_id"] for r in failing] == [r["action_id"] for r in control]


# ── 离线比较指标（尺子本身要能用） ──────────────────────────────────


def test_comparison_metrics_on_the_replay_batch(config, tmp_path):
    """长度 / 重复 / 泄露三个指标在真实回放批次上各测一个方向。"""
    control = _run_with_wording(config, tmp_path, "control", None)
    scripted = ScriptedRemoteWording(
        [f"候选文案第 {i} 句" for i in range(len(control))]
    )
    candidate = _run_with_wording(
        config, tmp_path, "candidate", RemoteWordingAdapter(scripted)
    )
    template_bodies = [r["action_text"] for r in control]
    candidate_bodies = [r["action_text"] for r in candidate]

    # 泄露扫描：候选正文不带窗口标题内容；模板正文带（它被允许）。
    assert all("哔哩哔哩" not in body for body in candidate_bodies)
    assert any("哔哩哔哩" in body for body in template_bodies)

    # 长度：弹窗正文不至于失控（宽松上界，替身测不出真实模型的分布）。
    assert all(0 < len(body) <= 300 for body in candidate_bodies)

    # 重复：候选每次都不同；换成常量替身时指标必须能抓出「句句一样」。
    assert len(set(candidate_bodies)) == len(candidate_bodies)
    constant = RemoteWordingAdapter(ScriptedRemoteWording(["同一句"] * 4))
    repeated = [constant.render(_verdict(), WALK, None) for _ in range(4)]
    assert len(set(repeated)) == 1

    # 生成时间：宽松上界。替身测不出真实延迟，只证明计时这一环通了。
    adapter = RemoteWordingAdapter(ScriptedRemoteWording(["x"]))
    started = perf_counter()
    adapter.render(_verdict(), WALK, None)
    assert perf_counter() - started < 1.0

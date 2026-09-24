"""阶段 3.3 的离线文案评估：契约 v2、隐私边界、失败回退与等价性。

计划任务 3.3 的完成条件是「同一批干预机会下触发次数与动作 ID 和规则基线
一致；变化仅限文案，失败时不产生额外弹窗」。本文件六组测试对应六件事：

  1. 契约镜像：`WordingContext`（v2，含回执上下文）的字段就是文档清单，
     传输面碰不到私密字段；
  2. 回执上下文：reminders_today / last_receipt 真的从调度层流进载荷，
     且回执读取失败时不丢投递；
  3. 隐私边界：候选实现拿到的上下文里没有窗口标题（反向对照：模板自己
     会带标题——本机弹窗允许，这正是判别器必须存在的原因）；
  4. 失败回退：候选抛错、空文案、超长文案都退回模板并计数，异常绝不外逃；
  5. 回放等价性：同一条时间线上，模板与候选（含全程失败的候选）的
     触发次数、动作 ID、时刻逐行一致——变化的只有正文；
  6. 文案侧 http 供应器与配置接线。

替身是 `ScriptedRemoteWording`，**它不是模型**；比较指标在这里只证明
「尺子能用」，真实模型的价值要等真实样本。
"""

from __future__ import annotations

import dataclasses
import json
import urllib.request
from time import perf_counter

import pytest

from statesense.config import Action, ConfigError, load_config
from statesense.intervention.wording import (
    CONTEXT_FIELDS,
    MAX_BODY_CHARS,
    RECEIPT_VALUES,
    WORDING_CONTEXT_VERSION,
    WORDING_PRIVATE_FIELDS,
    RemoteWordingAdapter,
    ScriptedRemoteWording,
    TemplateWording,
    WordingContext,
)
from statesense.intervention.wording_http import PROMPT_VERSION, HttpWordingProvider, _clean
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


def _context(**verdict_kwargs) -> WordingContext:
    return WordingContext.from_verdict(
        _verdict(**verdict_kwargs), WALK, reminders_today=0, last_receipt=None
    )


def _context_with_receipt(reminders_today: int, last_receipt: str | None) -> WordingContext:
    return WordingContext.from_verdict(
        _verdict(), WALK, reminders_today=reminders_today, last_receipt=last_receipt
    )


# ── 契约镜像 ────────────────────────────────────────────────────────


def test_context_fields_match_the_documented_allowlist():
    """`WordingContext` 的字段与文档清单逐字一致，且不碰任何私密字段。

    v2 新增 reminders_today / last_receipt（W1 回执上下文）——两者都不私密：
    一个来自干预表计数，一个来自已结算回执的标签。
    """
    fields = tuple(f.name for f in dataclasses.fields(WordingContext))
    assert fields == CONTEXT_FIELDS
    assert "reminders_today" in fields and "last_receipt" in fields
    assert not set(CONTEXT_FIELDS) & set(WORDING_PRIVATE_FIELDS)
    assert WORDING_CONTEXT_VERSION == "wording-context@v2"


def test_payload_carries_exactly_the_allowlist_and_is_json_ready():
    payload = _context().payload()
    assert list(payload) == list(CONTEXT_FIELDS)
    assert json.loads(json.dumps(payload)) == payload
    seasoned = _context_with_receipt(3, "disengaged").payload()
    assert seasoned["reminders_today"] == 3
    assert seasoned["last_receipt"] == "disengaged"


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

    adapter.render(_context(**kwargs), None)

    assert scripted.calls[0].top_category is expected


# ── 隐私边界 ────────────────────────────────────────────────────────


def test_candidate_never_sees_the_window_title():
    """候选拿到的载荷里没有标题；模板自己有——判别器真的在分辨。"""
    scripted = ScriptedRemoteWording(["候选文案"])
    adapter = RemoteWordingAdapter(scripted)
    title = "SECRET-视频标题XYZ"

    text = adapter.render(_context(), title)

    assert text == "候选文案"
    assert len(scripted.calls) == 1
    assert "SECRET" not in json.dumps(scripted.calls[0].payload())
    # 反向对照：模板正文确实嵌标题。没有这条，上面的「没有」只是巧合。
    assert "SECRET" in TemplateWording().render(_context(), title)


# ── 失败回退 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [RuntimeError("模型进程炸了"), "", "   ", "长" * (MAX_BODY_CHARS + 1)],
    ids=["exception", "empty", "whitespace", "overlong"],
)
def test_failure_and_bad_text_fall_back_to_the_template(bad):
    """候选抛错、空文案、超长：正文退回模板、回退计数加一、异常不外逃。

    模板回退文案必须和「从头就用模板」逐字一致——失败的代价只是一句
    模板，不多（没有额外弹窗）也不少（投递照常）。
    """
    scripted = ScriptedRemoteWording([bad])
    adapter = RemoteWordingAdapter(scripted)

    text = adapter.render(_context(late_night=True), None)

    assert text == TemplateWording().render(_context(late_night=True), None)
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

    # W1 的证据：回执上下文从调度层真的流进了载荷——第二次投递时
    # 「今天已提醒次数」必须是 1（不含本次）。
    assert scripted.calls[0].reminders_today == 0
    assert scripted.calls[1].reminders_today == 1
    # 首次投递时没有任何已结算回执；第二次投递前 ladder 已结算出一条
    # ——词汇以 outcome.tracker 为准，文案只消费，不编值。
    assert scripted.calls[0].last_receipt is None
    assert scripted.calls[1].last_receipt in RECEIPT_VALUES


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


def test_broken_receipt_lookup_does_not_lose_delivery(config, tmp_path, monkeypatch):
    """回执读取炸了：文案按「没有先例」写，投递一次不少。

    W1 让调度层在投递路径上多查一次库——这条测试就是那次查询的保险丝：
    素材缺失的代价必须只是一句平淡的文案。
    """
    import statesense.store.db as store_db

    def _boom(self):
        raise RuntimeError("回执查询炸了")

    monkeypatch.setattr(store_db.Store, "last_receipt_status", _boom)

    control = _run_with_wording(config, tmp_path, "control", None)
    run = run_scenario(
        SCENARIOS["ladder"],
        config,
        start=START,
        db_path=tmp_path / "broken-receipt.db",
        wording=RemoteWordingAdapter(ScriptedRemoteWording(["候选文案"] * len(control))),
    )
    try:
        _advance(run, _TICKS)
        rows = run.store.list_interventions()
    finally:
        run.store.close()

    assert len(rows) == len(control)
    assert [r["action_id"] for r in rows] == [r["action_id"] for r in control]


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
    repeated = [constant.render(_context(), None) for _ in range(4)]
    assert len(set(repeated)) == 1

    # 生成时间：宽松上界。替身测不出真实延迟，只证明计时这一环通了。
    adapter = RemoteWordingAdapter(ScriptedRemoteWording(["x"]))
    started = perf_counter()
    adapter.render(_context(), None)
    assert perf_counter() - started < 1.0


# ── 文案侧 http 供应器（与影子共用传输层） ──────────────────────────


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _chat_body(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")


def test_http_wording_provider_returns_clean_text(monkeypatch):
    """应答就是正文：剥围栏、去包裹引号、去空白。"""
    provider = HttpWordingProvider(
        "https://example.test/v1/chat/completions", "glm-test", "sk-fake",
        timeout_seconds=3.0,
    )
    cases = [
        ('```json\n"凌晨了，该歇歇"\n```', "凌晨了，该歇歇"),
        ('"带引号的正文"', "带引号的正文"),
        ("  纯文本正文  ", "纯文本正文"),
    ]
    for raw, expected in cases:
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda request, timeout=None, _raw=raw: _FakeResponse(_chat_body(_raw)),
        )
        assert provider.render(_context()) == expected


def test_http_wording_provider_sends_payload_and_bearer(monkeypatch):
    seen: dict = {}

    def handler(request, timeout=None):
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(_chat_body("正文"))

    monkeypatch.setattr(urllib.request, "urlopen", handler)
    provider = HttpWordingProvider(
        "https://example.test/v1/chat/completions", "glm-test", "sk-fake",
        timeout_seconds=3.0,
    )
    provider.render(_context_with_receipt(2, "disengaged"))

    assert seen["auth"] == "Bearer sk-fake"
    user_content = seen["body"]["messages"][1]["content"]
    sent = json.loads(user_content)
    assert sent["reminders_today"] == 2
    assert sent["last_receipt"] == "disengaged"
    assert "SECRET" not in user_content, "载荷里不许有标题"
    assert provider.prompt_version == PROMPT_VERSION == "wording-prompt@v1"


def test_http_wording_structure_error_falls_back_via_adapter(monkeypatch):
    """端点回答了但结构坏 → 抛错 → 适配层回退模板（provider_error 语义）。"""
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda request, timeout=None: _FakeResponse(b'{"error": {"message": "quota"}}'),
    )
    adapter = RemoteWordingAdapter(
        HttpWordingProvider(
            "https://example.test/v1/chat/completions", "glm-test", "sk-fake",
            timeout_seconds=3.0,
        )
    )

    text = adapter.render(_context(late_night=True), None)

    assert text == TemplateWording().render(_context(late_night=True), None)
    assert adapter.fallbacks == 1


# ── 配置接线 ────────────────────────────────────────────────────────


def test_unknown_wording_provider_is_rejected_at_load(make_config):
    """封闭枚举 + 启动期拒绝：名字可写而实现没接，两头都堵死。"""
    from statesense.config import load_config

    conf_path = make_config(replace=[('provider = "template"', 'provider = "magic"')])
    with pytest.raises(ConfigError, match="wording.provider 未知"):
        load_config(conf_path)


def test_wording_config_defaults_to_template(config):
    """默认 template：不写 [wording] 节也是内置模板，且 --check 可见。"""
    from statesense.__main__ import make_wording, wording_phrase

    assert isinstance(make_wording(config), TemplateWording)
    assert wording_phrase(config) == "template(内置模板)"


def test_wording_http_wiring_requires_env(tmp_path, make_config, monkeypatch):
    """provider = "http" 时构造走 .env：缺项在 make_wording 即报错
    （fail closed），配齐则构造出适配器。"""
    from statesense.__main__ import make_wording, wording_phrase

    conf_path = make_config(replace=[('provider = "template"', 'provider = "http"')])
    for key in ("STATESENSE_LLM_URL", "STATESENSE_LLM_MODEL", "STATESENSE_LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # 隔离仓库根的真实 .env

    config = load_config(conf_path)
    with pytest.raises(ConfigError):
        make_wording(config)
    assert "配置不完整" in wording_phrase(config)

    monkeypatch.setenv("STATESENSE_LLM_URL", "https://example.test/v1/chat/completions")
    monkeypatch.setenv("STATESENSE_LLM_MODEL", "glm-test")
    monkeypatch.setenv("STATESENSE_LLM_API_KEY", "sk-fake")
    adapter = make_wording(config)
    assert isinstance(adapter, RemoteWordingAdapter)
    assert "glm-test" in wording_phrase(config)

"""`HttpShadowProvider` 的测试：请求形状、超时翻译、应答解析的两层分工。

**测试不打真网**：`urllib.request.urlopen` 被替换成桩，密钥全部是假的——
真实密钥只住在 `.env` 里，任何测试都不需要它。

两层分工在这里各测各的（`shadow/http_provider.py` 的解析策略）：

  · 应答**结构**损坏（缺 choices / 缺 content）→ 抛错 → collector 记
    provider_error——那是端点没按契约回答；
  · 应答**文本**不成话（不是 JSON / 候选不在枚举里）→ 尽力取候选原样上交 →
    collector 记 invalid_output——「模型答了但答得不成话」与「调用失败」
    必须分档，混在一起影子对比的分母就是假的。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from statesense.shadow.collector import ShadowCollector
from statesense.shadow.http_provider import (
    PROMPT_VERSION,
    HttpShadowProvider,
    _parse_reply,
    _strip_fences,
)
from statesense.shadow.models import ShadowOutcome, StateShadowInput

PAYLOAD = StateShadowInput(
    ent_minutes=45.0,
    gray_minutes=0.0,
    work_minutes=10.0,
    total_active_minutes=60.0,
    window_minutes=60,
    late_night=False,
)

#: 假密钥。真实密钥只在 .env 里，测试永远不需要它。
FAKE_KEY = "sk-fake-key-for-tests"


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


def _provider(timeout_seconds: float = 3.0) -> HttpShadowProvider:
    return HttpShadowProvider(
        "https://example.test/v1/chat/completions",
        "glm-test",
        FAKE_KEY,
        timeout_seconds=timeout_seconds,
    )


def _stub_urlopen(monkeypatch, handler) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", handler)


# ── 请求形状 ────────────────────────────────────────────────────────


def test_request_sends_bearer_key_and_whitelist_payload_only(monkeypatch):
    """请求 = POST + Bearer 假密钥 + 白名单六字段，一不少、一不多。"""
    seen: dict = {}

    def handler(request, timeout=None):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(_chat_body('{"candidate": "NORMAL"}'))

    _stub_urlopen(monkeypatch, handler)
    provider = _provider(timeout_seconds=3.0)
    provider.query(PAYLOAD)

    assert seen["url"] == "https://example.test/v1/chat/completions"
    assert seen["auth"] == f"Bearer {FAKE_KEY}"
    assert seen["timeout"] == 3.0

    assert seen["body"]["model"] == "glm-test"
    assert seen["body"]["stream"] is False
    user_content = seen["body"]["messages"][1]["content"]
    assert json.loads(user_content) == PAYLOAD.payload(), (
        "发出去的必须是 payload() 的白名单字段本身"
    )


def test_provider_identifiers_are_honest():
    """model_version 用配置里的模型名；prompt_version 是写死的字面量。"""
    provider = _provider()
    assert provider.model_version == "glm-test"
    assert provider.prompt_version == PROMPT_VERSION
    assert PROMPT_VERSION == "state-shadow-prompt@v1"


# ── 传输层：超时翻译与失败透传 ──────────────────────────────────────


@pytest.mark.parametrize(
    "raised",
    [
        TimeoutError("timed out"),  # 裸超时（3.10+ socket.timeout 的别名）
        urllib.error.URLError(TimeoutError("timed out")),  # 包在 URLError 里的超时
    ],
    ids=["bare", "wrapped-in-urlerror"],
)
def test_transport_timeout_always_surfaces_as_timeouterror(monkeypatch, raised):
    """协议第 2 条：传输层超时只准以 `TimeoutError` 的形态出现。"""
    _stub_urlopen(monkeypatch, lambda request, timeout=None: (_ for _ in ()).throw(raised))

    with pytest.raises(TimeoutError):
        _provider().query(PAYLOAD)


def test_http_error_passes_through_as_provider_error(monkeypatch):
    """401 是真实的端点回答，不是超时——原样抛出，collector 记 provider_error。
    HTTPError 的 str 只有状态行，密钥不会出现在 detail 里。"""
    _stub_urlopen(
        monkeypatch,
        lambda request, timeout=None: (
            _ for _ in ()
        ).throw(urllib.error.HTTPError("https://example.test/v1", 401, "Unauthorized", None, None)),
    )

    collector = ShadowCollector(_provider(), timeout_seconds=3.0)
    signal = collector.collect(PAYLOAD, trustworthy=True)

    assert signal.outcome is ShadowOutcome.PROVIDER_ERROR
    assert signal.candidate is None
    assert "401" in (signal.detail or "")
    assert FAKE_KEY not in (signal.detail or "")


def test_broken_response_structure_is_provider_error(monkeypatch):
    """端点回答了但不是 chat/completions 形状 → 抛错 → provider_error 档。"""
    _stub_urlopen(
        monkeypatch,
        lambda request, timeout=None: _FakeResponse(b'{"error": {"message": "quota"}}'),
    )

    collector = ShadowCollector(_provider(), timeout_seconds=3.0)
    signal = collector.collect(PAYLOAD, trustworthy=True)

    assert signal.outcome is ShadowOutcome.PROVIDER_ERROR
    assert "choices" in (signal.detail or "")


# ── 应答文本：宽解析 + 枚举校验的分档 ───────────────────────────────


def test_happy_path_json_content_becomes_an_ok_signal(monkeypatch):
    _stub_urlopen(
        monkeypatch,
        lambda request, timeout=None: _FakeResponse(
            _chat_body('{"candidate": "PASSIVE_CONSUMPTION", "reason": "娱乐占大头"}')
        ),
    )

    signal = ShadowCollector(_provider(), timeout_seconds=3.0).collect(
        PAYLOAD, trustworthy=True
    )

    assert signal.outcome is ShadowOutcome.OK
    assert signal.candidate is not None and signal.candidate.state is not None
    assert signal.reason == "娱乐占大头"
    assert signal.latency_ms is not None


def test_fenced_markdown_is_stripped_before_parsing(monkeypatch):
    """模型爱用 ```json``` 包答案——剥掉围栏是解析层的事，不是失败。"""
    _stub_urlopen(
        monkeypatch,
        lambda request, timeout=None: _FakeResponse(
            _chat_body('```json\n{"candidate": "WATCH", "reason": "有苗头"}\n```')
        ),
    )

    signal = ShadowCollector(_provider(), timeout_seconds=3.0).collect(
        PAYLOAD, trustworthy=True
    )

    assert signal.outcome is ShadowOutcome.OK
    assert signal.candidate is not None and signal.candidate.value == "WATCH"


def test_garbage_text_becomes_invalid_output_not_provider_error(monkeypatch):
    """非 JSON 的一句人话：模型答了但答得不成话 → invalid_output 档，
    **不是** provider_error——两档混了的话失败率就失真。"""
    _stub_urlopen(
        monkeypatch,
        lambda request, timeout=None: _FakeResponse(_chat_body("我觉得是第四档吧")),
    )

    signal = ShadowCollector(_provider(), timeout_seconds=3.0).collect(
        PAYLOAD, trustworthy=True
    )

    assert signal.outcome is ShadowOutcome.INVALID_OUTPUT
    assert signal.candidate is None
    assert "第四档" in (signal.detail or ""), "原文截断留档，事后才知道它答了什么"


def test_near_miss_candidate_is_invalid_output(monkeypatch):
    """「PASSIVE」这种像但不合法的缩写：不许就近映射成合法状态。"""
    _stub_urlopen(
        monkeypatch,
        lambda request, timeout=None: _FakeResponse(_chat_body('{"candidate": "PASSIVE"}')),
    )

    signal = ShadowCollector(_provider(), timeout_seconds=3.0).collect(
        PAYLOAD, trustworthy=True
    )

    assert signal.outcome is ShadowOutcome.INVALID_OUTPUT


# ── 解析函数的直接单测 ──────────────────────────────────────────────


def test_strip_fences_variants():
    assert _strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_fences('```\nbare\n```') == "bare"
    assert _strip_fences("  no fences  ") == "no fences"
    assert _strip_fences("```json\n unterminated") == "unterminated"


def test_parse_reply_without_candidate_falls_back_to_whole_text():
    """JSON 对象但没有 candidate 键 → 原文整个当候选，交给枚举校验。"""
    reply = _parse_reply('{"answer": "PASSIVE_CONSUMPTION"}')
    assert reply.candidate == '{"answer": "PASSIVE_CONSUMPTION"}'
    assert reply.reason is None

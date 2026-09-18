import sys

import pytest

from statesense import perception
from statesense.perception import (
    GAMING_STATES,
    KNOWN_STATES,
    QUNS_ACCEPTS_NOTIFICATIONS,
    QUNS_BUSY,
    QUNS_RUNNING_D3D_FULL_SCREEN,
    FullscreenProbe,
    NullFullscreenProbe,
    Win32FullscreenProbe,
    default_probe,
    is_gaming,
)


def test_gaming_accepts_busy_and_fullscreen_d3d():
    """**实测决定，不是照抄文档。**

    原设计只取 3（`QUNS_RUNNING_D3D_FULL_SCREEN`），因为 2 还包含演示模式与
    全屏文档，看起来更安全。但本机实测推翻了它：三角洲行动在前台时连采 6 次
    都返回 2，从未返回 3 —— 只取 3 等于这条信号在本机永远不触发。

    接受 2 的代价由三件事兜住：只提权未归类条目、时长够不到门槛就不触发、
    原始值逐轮入库可事后审计。
    """
    assert is_gaming(QUNS_RUNNING_D3D_FULL_SCREEN) is True
    assert is_gaming(QUNS_BUSY) is True
    for state in (None, 1, 4, QUNS_ACCEPTS_NOTIFICATIONS, 6, 7):
        assert is_gaming(state) is False, state


def test_known_states_is_a_closed_enum():
    assert KNOWN_STATES == {1, 2, 3, 4, 5, 6, 7}
    assert GAMING_STATES == {QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN}


def test_null_probe_is_always_unknown():
    probe = NullFullscreenProbe()
    assert probe.state() is None
    assert isinstance(probe, FullscreenProbe)


def test_win32_probe_satisfies_protocol():
    assert isinstance(Win32FullscreenProbe(), FullscreenProbe)


#: §13 封闭枚举契约的可达分支：真机永远跑不到「调用失败」和「枚举外取值」，
#: 只能从 _query_state 缝隙喂进去 —— 否则这两条最要紧的分支无测试可言。
@pytest.mark.parametrize(
    ("queried", "expected"),
    [
        (None, None),  # 非 Windows / 没有 shell32
        ((1, QUNS_BUSY), None),  # HRESULT 非 0：调用失败，值再正常也不许采用
        ((0, 99), None),  # 枚举外取值
        ((0, 0), None),  # 边界：0 不在 1–7 之内
        ((0, QUNS_BUSY), QUNS_BUSY),
        ((0, QUNS_ACCEPTS_NOTIFICATIONS), QUNS_ACCEPTS_NOTIFICATIONS),
    ],
    ids=["no-shell32", "call-failed", "out-of-enum", "zero", "busy", "normal"],
)
def test_win32_probe_state_contract(monkeypatch, queried, expected):
    monkeypatch.setattr(perception, "_query_state", lambda: queried)
    assert Win32FullscreenProbe().state() is expected


def test_default_probe_matches_platform():
    probe = default_probe()
    if sys.platform == "win32":
        assert isinstance(probe, Win32FullscreenProbe)
    else:
        assert isinstance(probe, NullFullscreenProbe)


def test_probe_never_returns_a_value_outside_the_enum():
    """真实调用。「不知道」是允许的（None），但返回值绝不能是枚举外的数 ——
    那意味着语义与预期不符，必须按「不知道」处理而不是照样用。"""
    value = Win32FullscreenProbe().state()
    assert value is None or value in KNOWN_STATES


@pytest.mark.skipif(sys.platform != "win32", reason="只有 Windows 有 shell32")
def test_win32_probe_works_on_this_machine():
    """实测锁定：这台机器上 SHQueryUserNotificationState 可用且返回已知值。

    若这条开始失败，说明系统 API 行为变了 —— 整条全屏信号的前提没了，
    应该立刻知道，而不是让判定悄悄退化成「永远无法判定」。
    """
    value = Win32FullscreenProbe().state()
    assert value in KNOWN_STATES, f"SHQueryUserNotificationState 返回了 {value!r}"

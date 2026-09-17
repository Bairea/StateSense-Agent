"""本地环境感知。当前只有一项：是否正在跑全屏 D3D 应用。

**为什么需要它**：游戏窗口的标题与进程名就是游戏自身（实测 `Brotato.exe` / `Brotato`），
配置里的平台名（`steam` / `wegame`）抓不到它 —— 而按游戏名逐个维护清单没有普适性。
Windows 自己有一个判断「现在有没有全屏应用在跑」的调用，正是它用来决定该不该弹通知的，
语义精确，且不需要任何游戏清单。

**为什么它不在 `classify` 里**：`classify` 是纯函数（不读时钟、不做 IO）。这里由
`Scheduler` 每轮取一次，把值作为参数传进去，纯函数性得以保持。

这是本项目唯一一处「自动推断」：规格 §3 原本把自动分类推断列为非目标。改成允许
这**一条**推断的理由是它不需要任何清单、且证据来自操作系统本身而非猜测。
"""

from __future__ import annotations

import ctypes
import sys
from typing import Protocol, runtime_checkable

#: SHQueryUserNotificationState 的返回值（winuser.h 的 QUERY_USER_NOTIFICATION_STATE）。
QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5
QUNS_QUIET_TIME = 6
QUNS_APP = 7

#: 封闭枚举。读到枚举外的值按「无法判定」处理 —— 语义与预期不符时不下结论。
KNOWN_STATES = frozenset(
    {
        QUNS_NOT_PRESENT,
        QUNS_BUSY,
        QUNS_RUNNING_D3D_FULL_SCREEN,
        QUNS_PRESENTATION_MODE,
        QUNS_ACCEPTS_NOTIFICATIONS,
        QUNS_QUIET_TIME,
        QUNS_APP,
    }
)

#: 判定为「正在玩游戏」的取值。**实测决定，不是照抄文档。**
#:
#: 原设计只取 3（`QUNS_RUNNING_D3D_FULL_SCREEN`），因为 2（`QUNS_BUSY`）还包含
#: 演示模式与全屏文档，看起来更"安全"。但本机实测推翻了它：三角洲行动在前台时
#: 连采 6 次都返回 **2**，从未返回 3 —— 只取 3 等于这条信号在本机永远不触发。
#: 无边框窗口模式的游戏似乎都落在 2。
#:
#: 接受 2 的代价由三件事兜住：
#:   1. 只提权「未命中任何规则」的条目 —— 工作/灰色仍然不算娱乐；
#:   2. 短暂的全屏文档只贡献几分钟，够不到 watch(20 分钟) 门槛；
#:   3. 原始值逐轮入库，误判可事后审计（这是记 `fullscreen_state` 而非布尔的理由）。
GAMING_STATES = frozenset({QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN})


@runtime_checkable
class FullscreenProbe(Protocol):
    def state(self) -> int | None:
        """返回 QUERY_USER_NOTIFICATION_STATE；无法判定时返回 `None`。

        `None` 与「不是全屏」是两件不同的事：前者是不知道，后者是知道。
        绝不把「不知道」当成「不是」—— 那会静默降低判定质量。
        """
        ...


class Win32FullscreenProbe:
    """真实探针。非 Windows、调用失败、返回枚举外的值 —— 一律 `None`。"""

    def state(self) -> int | None:
        if sys.platform != "win32":
            return None
        try:
            value = ctypes.c_int(0)
            result = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(value))
        except (OSError, AttributeError):
            # 没有 shell32、或该调用不存在（老系统）—— 按「无法判定」处理。
            return None
        if result != 0:
            return None
        return value.value if value.value in KNOWN_STATES else None


class NullFullscreenProbe:
    """永远「无法判定」。非 Windows 与测试用。"""

    def state(self) -> int | None:
        return None


def default_probe() -> FullscreenProbe:
    return Win32FullscreenProbe() if sys.platform == "win32" else NullFullscreenProbe()


def is_gaming(state: int | None) -> bool:
    """只有明确的全屏 D3D 才算。`None` 与其它取值都不算。"""
    return state in GAMING_STATES


STATE_LABELS: dict[int, str] = {
    QUNS_NOT_PRESENT: "不在电脑前 / 锁屏",
    QUNS_BUSY: "全屏应用运行中（判定为游戏）",
    QUNS_RUNNING_D3D_FULL_SCREEN: "全屏 D3D 应用（判定为游戏）",
    QUNS_PRESENTATION_MODE: "演示模式",
    QUNS_ACCEPTS_NOTIFICATIONS: "正常，无全屏应用",
    QUNS_QUIET_TIME: "静默时段",
    QUNS_APP: "Windows 应用模式",
}


def describe(state: int | None) -> str:
    """给人看的一行说明。`None` 明确说成「无法判定」，不写成「否」。"""
    if state is None:
        return "无法判定（不提权）"
    return STATE_LABELS.get(state, f"未知取值 {state}（不提权）")

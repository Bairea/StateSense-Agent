"""Win32 前台弹窗的薄封装。全部走 ctypes 标准库，无第三方依赖。

为什么不用 Windows Toast：本机全局通知开关是关的（ToastEnabled=0），且 Windows
在「全屏应用」下默认抑制通知 —— 而本项目的核心场景恰恰是全屏刷视频。原生
MessageBox 完全绕开通知平台，不受这两项影响。

但 MessageBox 默认仍会被「前台锁定」挡住：后台进程不允许抢前台，于是窗口被创建
却压在全屏应用后面（只闻其声、不见其形）。所以必须显式抢前台：
    AttachThreadInput → BringWindowToTop → SetForegroundWindow → SetWindowPos(HWND_TOPMOST)
本机实测（窗口模式 + 全屏模式各一轮）：attached=True setforeground=True，全屏可见。
"""

from __future__ import annotations

import sys
import time
from ctypes import wintypes

if sys.platform != "win32":
    raise ImportError(
        "win32_popup 只在 Windows 上可用。本项目 V0 的投递通道依赖原生 MessageBox；"
        "换平台需要先换 Notifier 实现（见 spec §8）。"
    )

import ctypes
import winsound

# MessageBox 按钮与样式
MB_YESNO = 0x00000004
MB_ICONWARNING = 0x00000030
MB_TOPMOST = 0x00040000
MB_SETFOREGROUND = 0x00010000
MB_SYSTEMMODAL = 0x00001000

# MessageBox 返回值
IDCANCEL = 2
IDYES = 6
IDNO = 7

# SetWindowPos
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
SW_SHOW = 5

# 窗口消息
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_SYSCOMMAND = 0x0112
SC_CLOSE = 0xF060

POLL_INTERVAL_SECONDS = 0.2
CLOSE_ATTEMPT_WAIT_SECONDS = 0.5


class Win32Popup:
    """真实实现。所有 ctypes 细节都关在这个类里，便于测试整体替换。"""

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._declare_signatures()

    def _declare_signatures(self) -> None:
        u = self._user32
        # 不声明 restype 的话，64 位下 HWND 会被截断成 int。
        u.MessageBoxW.restype = ctypes.c_int
        u.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_uint]
        u.FindWindowW.restype = wintypes.HWND
        u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        u.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.BringWindowToTop.argtypes = [wintypes.HWND]
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.FlashWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        u.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        u.GetDlgItem.restype = wintypes.HWND
        u.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]

    def show(self, title: str, body: str) -> int:
        """阻塞直到用户点按钮。返回按钮 id。"""
        flags = MB_YESNO | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND | MB_SYSTEMMODAL
        return int(self._user32.MessageBoxW(None, body, title, flags))

    def find(self, title: str) -> int:
        return int(self._user32.FindWindowW(None, title) or 0)

    def force_front(self, title: str, timeout: float) -> bool:
        """轮询找到对话框句柄，然后强行抢到前台。"""
        deadline = time.time() + timeout
        current_thread = self._kernel32.GetCurrentThreadId()
        while time.time() < deadline:
            hwnd = self.find(title)
            if hwnd:
                u = self._user32
                u.ShowWindow(hwnd, SW_SHOW)
                u.SetWindowPos(
                    hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
                )
                foreground = u.GetForegroundWindow()
                foreground_thread = u.GetWindowThreadProcessId(foreground, None)
                attached = bool(u.AttachThreadInput(current_thread, foreground_thread, True))
                u.BringWindowToTop(hwnd)
                u.SetForegroundWindow(hwnd)
                if attached:
                    u.AttachThreadInput(current_thread, foreground_thread, False)
                u.FlashWindow(hwnd, True)
                return True
            time.sleep(POLL_INTERVAL_SECONDS)
        return False

    def close(self, title: str) -> bool:
        """程序化关闭弹窗，返回是否确实关掉了。

        只用 WM_CLOSE 不够可靠：MB_YESNO 没有取消按钮时，关闭按钮可能被禁用。
        所以依次尝试多种「模拟用户动作」的消息，每试一种就确认窗口是否真的消失。
        """
        hwnd = self.find(title)
        if not hwnd:
            return False

        u = self._user32
        attempts = (
            lambda: u.PostMessageW(hwnd, WM_COMMAND, IDNO, u.GetDlgItem(hwnd, IDNO)),
            lambda: u.PostMessageW(hwnd, WM_COMMAND, IDCANCEL, u.GetDlgItem(hwnd, IDCANCEL)),
            lambda: u.PostMessageW(hwnd, WM_CLOSE, 0, 0),
            lambda: u.PostMessageW(hwnd, WM_SYSCOMMAND, SC_CLOSE, 0),
        )
        for attempt in attempts:
            attempt()
            if self._wait_gone(title, CLOSE_ATTEMPT_WAIT_SECONDS):
                return True
        return False

    def _wait_gone(self, title: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.find(title):
                return True
            time.sleep(0.05)
        return False

    def beep(self) -> None:
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)

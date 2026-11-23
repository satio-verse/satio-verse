"""她的手: 先用最底层的 Windows 鼠标事件练点按 (2026-11-03).

不装 pyautogui / pynput 这些包, 直接调 user32 的 SetCursorPos + SendInput: 这两步就是
系统里真正的鼠标动作, 别的库底下也是它们. 现在只做最基本的一下点按 (左/右/中键,
最多连点三下), 键盘、拖动、滚轮还没有. 坐标是虚拟桌面的像素坐标 (多显示器可以是负数),
先检查落在屏幕里再动鼠标, 不会点到屏幕外面去.

点按会真的动主人的鼠标, 所以调用方 (工作台的 mouse_click 工具) 还要先按权限问他;
这里只管把一件事做对, 不管该不该做.
"""
from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import dataclass

MAX_COORD = 100_000          # 解析坐标时的上限: 防止拿一个离谱的数去算
MAX_COUNT = 3                # 一次最多连点三下 (双击、三击够用了)
CLICK_GAP_S = 0.08           # 连点之间隔一下, 让系统认得出是双击

# MOUSEEVENTF_LEFTDOWN / LEFTUP / RIGHTDOWN / RIGHTUP / MIDDLEDOWN / MIDDLEUP
BUTTONS = {"left": (0x0002, 0x0004, "左键"), "right": (0x0008, 0x0010, "右键"),
           "middle": (0x0020, 0x0040, "中键")}
INPUT_MOUSE = 0
# SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN / SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN
VIRTUAL_SCREEN = (76, 77, 78, 79)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32), ("mouseData", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_void_p)]


class _INPUT(ctypes.Structure):
    class _UNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _UNION)]


@dataclass(frozen=True)
class Click:
    """解析、检查过的一次点按 (还没真的动鼠标)."""
    x: int
    y: int
    button: str = "left"
    count: int = 1

    def __post_init__(self):
        if self.button not in BUTTONS:
            raise ValueError("button 只能是 left / right / middle")
        if type(self.count) is not int or not 1 <= self.count <= MAX_COUNT:
            raise ValueError(f"count 只能是 1 到 {MAX_COUNT}")

    def shown(self) -> str:
        """给页面和模型看的一句话."""
        return f"在屏幕 ({self.x}, {self.y}) 用{BUTTONS[self.button][2]}点{_times(self.count)}"


def _times(count: int) -> str:
    return "一下" if count == 1 else "两下" if count == 2 else "三下"


def _number(value, name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} 要写整数")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{name} 要写整数")
        value = int(value)
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} 要写整数") from None
    if abs(number) > MAX_COORD:
        raise ValueError(f"{name} 太离谱了")
    return number


def parse_click(x, y, button="left", count=1) -> Click:
    """检查参数, 返回一次可以执行的点按; 参数不对抛 ValueError (还不会动鼠标)."""
    button = str(button or "left").strip().lower()
    if button not in BUTTONS:
        raise ValueError("button 只能是 left / right / middle")
    count = _number(count, "count")
    if not 1 <= count <= MAX_COUNT:
        raise ValueError(f"count 只能是 1 到 {MAX_COUNT}")
    return Click(_number(x, "x"), _number(y, "y"), button, count)


def click(x, y, button="left", count=1) -> str:
    """把鼠标挪到 (x, y) 再点; 返回一句结果. 只在 Windows 上能用."""
    return perform_click(parse_click(x, y, button, count))


def _user32():
    """拿 user32. 测试里换成替身, 不碰真的鼠标."""
    if sys.platform != "win32":
        raise OSError("点按现在只在 Windows 上能用")
    return ctypes.WinDLL("user32", use_last_error=True)


def screen_rect(u=None) -> tuple[int, int, int, int]:
    """虚拟桌面 (left, top, width, height); 多显示器时 left/top 可以是负数."""
    u = u or _user32()
    try:
        left, top, width, height = (int(u.GetSystemMetrics(i)) for i in VIRTUAL_SCREEN)
    except (AttributeError, TypeError, ValueError) as exc:
        raise OSError("量不出屏幕大小") from exc
    if width <= 0 or height <= 0:  # 个别环境不给虚拟桌面尺寸: 退回主屏
        left = top = 0
        width, height = int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    if width <= 0 or height <= 0:
        raise OSError("量不出屏幕大小")
    return left, top, width, height


def _send(u, flags: int) -> None:
    event = _INPUT(type=INPUT_MOUSE)
    event.mi = _MOUSEINPUT(0, 0, 0, flags, 0, None)
    sent = u.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))
    if sent != 1:
        raise OSError(ctypes.get_last_error() or 1, "鼠标事件没送出去 (可能被权限更高的窗口拦了)")


def perform_click(target: Click) -> str:
    """执行一次已经检查过的点按; 坐标不在屏幕上、挪不动鼠标都会说清楚."""
    if not isinstance(target, Click):
        raise TypeError("target 要是 parse_click 返回的 Click")
    u = _user32()
    left, top, width, height = screen_rect(u)
    if not (left <= target.x < left + width and top <= target.y < top + height):
        raise ValueError(f"坐标 ({target.x}, {target.y}) 不在屏幕上 "
                         f"(屏幕是 {left}, {top} 到 {left + width - 1}, {top + height - 1})")
    if not u.SetCursorPos(target.x, target.y):
        raise OSError(ctypes.get_last_error() or 1, "鼠标挪不过去 (可能被权限更高的窗口拦了)")
    down, up, _ = BUTTONS[target.button]
    for i in range(target.count):
        _send(u, down)
        _send(u, up)
        if i + 1 < target.count:
            time.sleep(CLICK_GAP_S)
    return target.shown() + ", 点好了"

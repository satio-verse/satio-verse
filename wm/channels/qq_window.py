"""读 QQ 登录窗口上的二维码, 重新画一张发给主人 (2026-11-07 方案: "要重新扫码时把二维码发到桌宠和微信让用户扫").

SnowLuma 是挂在电脑版 QQ 上的, 登录二维码只出现在 QQ 自己的登录窗口里, 协议端没有接口能拿. 这里只做"看", 不做"动手":
- 只截 snowluma_hook 认定"没登录账号"的 QQ 主进程的窗口 (已经登录的 QQ 的窗口里是聊天内容, 一律不碰); 窗口还得是登录窗口
  那么大 (CANDIDATE_MIN..MAX), 可见、没最小化;
- 用 PrintWindow 截 (不把窗口调到前台、不动鼠标键盘、不点任何东西); 截到的图只在内存里, 认出二维码内容就丢掉, 不存盘、不发、不记日志;
- 发出去的是重新画的一张干净的二维码 (只有二维码内容本身, 窗口里别的东西都不带);
- 截不到、认不出 (要点"登录"、要短信、要滑块验证、窗口缩在托盘里) 就返回 None, 由调用方只发文字. 不做任何绕过验证码、人机校验的事.
二维码本身几分钟就过期, QQ 会自己刷新, 所以每次要发都现截一次.
"""
from __future__ import annotations

import ctypes
import io
import os
import time
from ctypes import wintypes
from dataclasses import dataclass

CANDIDATE_MIN = (200, 250)   # 登录窗口的宽、高下限 / 上限 (登录窗口大约 400×550): 比这大的是聊天主窗口, 不碰
CANDIDATE_MAX = (640, 860)
TTL_S = 150                  # 一张截到的二维码当多久有效 (QQ 的二维码 2 分钟左右过期)


@dataclass
class Shot:
    text: str          # 二维码里的内容
    png: bytes         # 重新画的二维码
    pid: int
    at: float


def candidate_windows(pids) -> list[dict]:
    """这些进程的可见、没最小化、大小像登录窗口的顶层窗口: [{hwnd, pid, w, h}]."""
    if os.name != "nt":
        return []
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [proc, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    want, found = {int(p) for p in pids}, []

    def visit(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in want and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            rect = wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                w, h = rect.right - rect.left, rect.bottom - rect.top
                if CANDIDATE_MIN[0] <= w <= CANDIDATE_MAX[0] and CANDIDATE_MIN[1] <= h <= CANDIDATE_MAX[1]:
                    found.append({"hwnd": int(hwnd), "pid": pid.value, "w": w, "h": h})
        return True

    user32.EnumWindows(proc(visit), 0)
    return found


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def grab(hwnd: int):
    """PrintWindow 截一个窗口 -> BGR 图 (numpy), 截不到返回 None. 调用方认完二维码就丢掉它."""
    import numpy as np
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    hwnd_t = wintypes.HWND(hwnd)
    for fn, args, res in ((user32.GetWindowRect, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
                          (user32.GetWindowDC, [wintypes.HWND], wintypes.HDC),
                          (user32.ReleaseDC, [wintypes.HWND, wintypes.HDC], ctypes.c_int),
                          (user32.PrintWindow, [wintypes.HWND, wintypes.HDC, wintypes.UINT], wintypes.BOOL),
                          (gdi32.CreateCompatibleDC, [wintypes.HDC], wintypes.HDC),
                          (gdi32.CreateCompatibleBitmap, [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP),
                          (gdi32.SelectObject, [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
                          (gdi32.DeleteObject, [wintypes.HGDIOBJ], wintypes.BOOL),
                          (gdi32.DeleteDC, [wintypes.HDC], wintypes.BOOL),
                          (gdi32.GetDIBits, [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                             ctypes.c_void_p, wintypes.UINT], ctypes.c_int)):
        fn.argtypes, fn.restype = args, res
    old_ctx = None
    try:  # 按物理像素量窗口大小 (显示缩放 125% / 150% 时, 不识别 DPI 的进程量出来的是缩小过的)
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        old_ctx = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        pass
    try:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd_t, ctypes.byref(rect)):
            return None
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 0 or h <= 0 or w > 4000 or h > 4000:
            return None
        src = user32.GetWindowDC(hwnd_t)
        mem = gdi32.CreateCompatibleDC(src)
        bmp = gdi32.CreateCompatibleBitmap(src, w, h)
        old = gdi32.SelectObject(mem, bmp)
        try:
            if not user32.PrintWindow(hwnd_t, mem, 2):  # 2 = PW_RENDERFULLCONTENT: 带上 Chromium / Electron 画的内容
                return None
            info = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # 负高度: 自上而下
            buf = (ctypes.c_ubyte * (w * h * 4))()
            if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0) != h:
                return None
        finally:
            gdi32.SelectObject(mem, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(hwnd_t, src)
        return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3].copy()
    finally:
        if old_ctx:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old_ctx))


def decode(img) -> str | None:
    """图里的二维码内容 (OpenCV 自带的识别, 不联网). 认不出返回 None."""
    import cv2
    if img is None or not getattr(img, "size", 0):
        return None
    tries = [img]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    tries.append(cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))  # 小窗口里的二维码放大再认
    tries.append(cv2.bitwise_not(gray))                                              # 深色主题: 反过来
    detectors = [cv2.QRCodeDetector()]
    if hasattr(cv2, "QRCodeDetectorAruco"):
        detectors.append(cv2.QRCodeDetectorAruco())
    for pic in tries:
        for det in detectors:
            try:
                text, _points, _straight = det.detectAndDecode(pic)
            except cv2.error:
                continue
            if text and len(text) <= 1024:
                return text
    return None


def render_png(text: str) -> bytes:
    """二维码内容 -> 重新画的干净的 PNG (白底黑块, 留白够宽, 手机扫得出来)."""
    import qrcode
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    out = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(out, format="PNG")
    return out.getvalue()


def snapshot_qr(pids) -> Shot | None:
    """这些 (没登录账号的) QQ 主进程的登录窗口上有二维码就截下来重画; 没有返回 None. 截图不留."""
    for win in candidate_windows(pids):
        try:
            img = grab(win["hwnd"])
            text = decode(img) if img is not None else None
        finally:
            img = None  # 截到的图到此为止
        if text:
            return Shot(text, render_png(text), win["pid"], time.time())
    return None

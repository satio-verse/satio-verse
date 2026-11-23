"""服务端是不是以管理员身份在跑 (2026-11-11 插件宿主第 0 步, docs: 底层终端 (插件宿主) 方案第四节).

她、工作台任务和所有扩展、插件都以服务端的身份运行: 服务端是管理员, 它们就全是管理员. 工作区限制和黑名单只是工具层的
授权规则, 不是操作系统隔离, 拦不住"本来就是管理员"的进程. 所以服务端不该以管理员身份运行; 万一是 (比如在"以管理员身份
运行"的终端里起的), 启动时日志里写一行明显的警告 (wm/__main__.py 调 warn_if_elevated); /api/status 里带一个 privilege
字段 (wm/app.py 的 api_status 调 status), 好让桌宠、面板能提醒你.

/api/status 是看守判断服务端健不健康用的接口 (tools/serve.py 每隔几秒查一次), 这个字段不管出什么意外都不能让它报错: status()
和 is_elevated() 不抛异常, 查不出来就给 elevated: null. 进程的权限启动后不会变, status() 只查一次、记住 (warn_if_elevated 是
启动时重新查一次并记住; 测试用 forget() 清掉).

查的是进程令牌有没有"提升" (UAC 之后真的拿着管理员权限), 不是"账号在不在管理员组": 平时登录的管理员账号, 没点过
"以管理员身份运行"的进程不算. 桌宠宿主 (wmpet.exe) 自己是不是管理员, 这里查不到, 要宿主那边自己查 (C++).
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("wm.privilege")

TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20  # TOKEN_INFORMATION_CLASS 里的 TokenElevation
WARNING = ("服务端是以管理员身份在跑：她、工作台任务和所有扩展都会拿到管理员权限（黑名单和工作区限制只是工具层的规则，"
           "不是系统隔离）。请关掉这个管理员终端，用普通权限重新启动服务端（从桌宠或普通终端起）。")
_LOCK = threading.Lock()
_STATUS: dict | None = None  # 查过一次的结果 (进程的权限启动后不会变, 查一次记住)


def _windows() -> bool:
    return os.name == "nt"


def _token_elevated() -> bool | None:
    """当前进程令牌的 TokenIsElevated; 查不出来 (系统调用失败) 是 None."""
    try:
        import ctypes
        from ctypes import wintypes
        # 每次用新的库对象设参数类型, 不去改 ctypes.windll 那份全进程共用的
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            elevation, size = wintypes.DWORD(), wintypes.DWORD()
            if not advapi32.GetTokenInformation(token, TOKEN_ELEVATION, ctypes.byref(elevation),
                                                ctypes.sizeof(elevation), ctypes.byref(size)):
                return None
            return bool(elevation.value)
        finally:
            kernel32.CloseHandle(token)
    except (OSError, AttributeError, ValueError):
        return None


def _shell_is_admin() -> bool | None:
    """老办法 (shell32.IsUserAnAdmin), 令牌那条路走不通时的后备."""
    try:
        import ctypes
        return bool(ctypes.WinDLL("shell32").IsUserAnAdmin())
    except (OSError, AttributeError):
        return None


def _posix_root() -> bool | None:
    geteuid = getattr(os, "geteuid", None)
    return geteuid() == 0 if geteuid is not None else None


def _attempt(probe) -> bool | None:
    """跑一种查法: True / False; 查不出来、或者出了任何意外 (ctypes 里没想到的错也算) 都是 None, 不往外抛."""
    try:
        got = probe()
    except Exception as exc:  # noqa: BLE001 - 看守靠 /api/status 判断健康, 这里不管出什么事都不能漏出去
        log.warning("管理员自查的一种查法出错 (当作查不出来): %s: %s", type(exc).__name__, exc)
        return None
    return got if isinstance(got, bool) else None


def is_elevated() -> bool | None:
    """True: 以管理员身份在跑 (Windows 上是令牌已提升, 别的系统是 root); False: 不是; None: 查不出来.
    每次都查系统, 不抛异常; 要记住结果的用 status()."""
    if _windows():
        got = _attempt(_token_elevated)
        return got if got is not None else _attempt(_shell_is_admin)
    return _attempt(_posix_root)


def _measure() -> dict:
    try:
        elevated = is_elevated()
    except Exception as exc:  # noqa: BLE001 - is_elevated 自己不抛; 这里防它被换成别的东西 (改动、测试替身) 时漏出去
        log.warning("管理员自查出错 (给 elevated: null): %s: %s", type(exc).__name__, exc)
        elevated = None
    if not isinstance(elevated, bool):
        elevated = None
    return {"elevated": elevated, "warning": WARNING if elevated else ""}


def status() -> dict:
    """/api/status 的 privilege 字段: {elevated: True / False / None (查不出来), warning: 要给人看的话, 不是管理员时是空}.
    进程的权限启动后不会变, 查一次记住 (后面每次请求都不再查系统); 不抛异常; 给出去的是一份拷贝."""
    global _STATUS
    with _LOCK:
        if _STATUS is None:
            _STATUS = _measure()
        return dict(_STATUS)


def forget() -> None:
    """忘掉记住的结果, 下一次 status() 重新查 (启动自查、测试用)."""
    global _STATUS
    with _LOCK:
        _STATUS = None


def warn_if_elevated(logger: logging.Logger | None = None) -> dict:
    """服务端启动时调一次: 重新查一次 (并记住, 后面 /api/status 用这个结果), 以管理员身份在跑就在日志里写一行明显的警告.
    返回 status()."""
    forget()
    out = status()
    if out["elevated"]:
        (logger or log).warning("【警告·管理员权限】%s", out["warning"])
    return out

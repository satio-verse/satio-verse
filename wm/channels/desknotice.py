"""桌面通知 (急3, 2026-11-15): 申请卡片 15 秒没人点时在这台电脑上弹一条 Windows 通知. 只在本机, 不碰网络.

做法: 起一个隐藏窗口的 powershell, 用 WinRT 的 ToastNotificationManager 弹一条 toast. 标题和正文走环境变量传进去,
脚本里用 XmlDocument 的 InnerText 赋值 (不拼命令、不拼 XML), 卡片标题里有别人的话也注入不了.
差的部分 (要宿主6): 这是 Windows 通知中心的 toast, 图标是 PowerShell 的, 点它不会聚焦桌宠; 真正挂在桌宠托盘图标上、
点一下聚焦卡片的气泡要宿主 (C++) 加一条命令. 到时只换 ChannelManager.desk_notice 这一个函数.
pytest 里 (环境里有 PYTEST_CURRENT_TEST) show 一律不起进程.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

TIMEOUT_S = 15.0
CREATE_NO_WINDOW = 0x08000000  # 不闪黑窗
TITLE_ENV, TEXT_ENV = "WM_NOTICE_TITLE", "WM_NOTICE_TEXT"
TITLE_MAX, TEXT_MAX = 60, 240
# powershell 只拿这几样环境变量 (不把服务端环境里的模型 key 之类带给它)
ENV_KEEP = ("SystemRoot", "SystemDrive", "windir", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
            "ProgramData", "ProgramFiles", "COMSPEC", "USERNAME", "COMPUTERNAME", "HOMEDRIVE", "HOMEPATH")
# Windows PowerShell 自己登记过的应用号: toast 要挂在一个登记过的应用号下才弹得出来
APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
SCRIPT = """$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text/><text/></binding></visual></toast>')
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).InnerText = $env:WM_NOTICE_TITLE
$nodes.Item(1).InnerText = $env:WM_NOTICE_TEXT
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('%s').Show($toast)
""" % APP_ID.replace("'", "''")


def _clean(s: str, n: int) -> str:
    s = "".join(ch for ch in str(s) if ch == "\n" or (ch.isprintable() and ch != "\x00")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def build(title: str, text: str) -> tuple[list[str], dict[str, str]]:
    """(命令行, 环境变量). 标题、正文只在环境变量里, 命令行里没有."""
    exe = shutil.which("powershell.exe") or "powershell.exe"
    encoded = base64.b64encode(SCRIPT.encode("utf-16-le")).decode("ascii")
    argv = [exe, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded]
    return argv, {TITLE_ENV: _clean(title, TITLE_MAX), TEXT_ENV: _clean(text, TEXT_MAX)}


def _env(extra: dict[str, str]) -> dict[str, str]:
    keep = {k.lower() for k in ENV_KEEP}
    return {**{k: v for k, v in os.environ.items() if k.lower() in keep}, **extra}


def _run(argv: list[str], env: dict[str, str]) -> bool:
    try:
        done = subprocess.run(argv, env=_env(env), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=TIMEOUT_S, creationflags=CREATE_NO_WINDOW, check=False)
    except (OSError, subprocess.SubprocessError) as exc:  # 超时 subprocess 会杀掉子进程
        log.info("桌面通知没弹出来: %s", exc)
        return False
    if done.returncode != 0:
        log.info("桌面通知没弹出来: powershell 退出码 %s", done.returncode)
    return done.returncode == 0


async def show(title: str, text: str) -> bool:
    """弹一条. 成了返回 True; 非 Windows、出错、超时、pytest 里返回 False (都不抛)."""
    if sys.platform != "win32" or os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    argv, env = build(title, text)
    return await asyncio.to_thread(_run, argv, env)

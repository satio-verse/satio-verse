"""屏幕陪看 (桌宠): 你说"陪我刷 B 站 / 陪我看 / 看我玩游戏", 不给链接, 她就看你的屏幕 —— 像坐在旁边的朋友.

2026-11-08 用户: "我提出请你陪我一起刷B站视频，她自己就陪我看屏幕，而不是我还要给她提供链接之类的，你想想叫一个真人陪自己
刷视频、玩游戏会怎么做？" (方案: 知识库 09 问题与待办/桌宠陪看升级方案：像真人一样陪你刷视频（2026-11-08） 第 1 步; 用户同意
屏幕画面发给云端模型看: 只截前台那个窗口、压成小拼图、看完就删). 给了链接的照旧走 wm/watch_along.py (把视频取下来看,
清楚、有台词).

- 只在你叫她陪的时候看, 说"不看了"马上停; 看着的时候页面上有"她在看你的屏幕" (watch_state 带 mode: "screen").
- 只截你前台那个窗口 (自己 BitBlt 那一块, 不截整个屏幕); 聊天、密码、网银、邮箱、登录 / 支付页、桌宠自己这些窗口一律不截;
  写代码、办公、文件管理这类不是在看 / 玩的窗口也不截. 截图只在内存里, 压成一张小拼图给模型看, 不落盘.
- 几秒看一眼 (tick_s): 画面没变 (暂停了、停在一页) 就不问模型, 不花钱、她也不说话; 标题换了 (刷到下一条、换了片)
  先看一眼, 像朋友那样第一眼反应一句或不说; 一直在放就每 look_s 秒把这段里变过的几张画面一起给模型看, 想说才说.
- 不抢话: 你在说话、她这一轮没说完就等 (和陪看一样走桌宠会话的 aside); 两句之间至少隔 min_gap_s 秒 (刷到新的一条 first_gap_s).
- 每次看到的 (saw, 一句话) 攒着, 陪看结束时连同她说的、你说的一起记进记忆 (pipeline._watch_remember).
- 你切到别处 (聊天、写代码) 超过 away_s 秒, 说一声就不看了. "暂停一下" 她停看, "继续" 接着看; 说进度没用, 她看得到屏幕.
- 这一版没有声音 (看不到台词以外的话): 系统声音回环是方案第 3 步, 要新下载, 先问.
- 问模型出错 (连不上网) 就跳过这一眼, 下一眼接着看; 连着 max_errors 次才说一声"网好像断了，我先不看了"再停
  (2026-11-08 15:01 实测: 连不上 DeepSeek 一下, 整个陪看就停了、一声没吭).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import re
import sys
import time
from typing import Awaitable, Callable

from . import intent as I

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": True,
    "tick_s": 3.0,             # 多久看一眼前台窗口 (只截图比画面, 不问模型)
    "look_s": 20.0,            # 一直在放: 隔多久把这段的画面给模型看一次
    "first_look_s": 4.0,       # 刷到新的一条 (标题换了): 等它加载几秒再看第一眼
    "min_gap_s": 40.0,         # 她两句话之间至少隔多久
    "first_gap_s": 15.0,       # 刷到新的一条时的第一眼反应: 隔这么久就能说
    "late_drop_s": 8.0,        # 想说的时候你在说话 / 她没说完: 最多等多久, 过了这句作废
    "away_s": 300.0,           # 你切到别处 (不是在看 / 玩的窗口) 超过这么久就不看了
    "max_session_s": 7200.0,
    "max_looks_per_hour": 180,  # 问模型的上限 (一次约 0.0014 元)
    "width": 640,              # 每张截图缩到多宽
    "keep": 4,                 # 一次最多给模型看几张 (拼成一张 2x2 的小图)
    "change": 4.0,             # 缩略图平均差多少 (0-255) 才算画面变了
    "max_tokens": 600,         # 和陪看那一句一样: deepseek-flash 会先想
    "max_errors": 3,           # 问模型连着出错 (连不上网) 这么多次就说一声不看了; 不到就跳过这一眼接着看
    "key_change": None,        # 默认关. 开了: 整幅画面一下子大变 (平均差 >= 这个数, 如换场景、转场) 就不等 look_s 立刻看一眼 (游戏世界用)
    "key_gap_s": 8.0,          # 关键画面变化触发的两眼之间至少隔多久
}
DOWN_LINE = "网好像断了，我先不看了，好了再叫我～"  # 连不上模型好几次: 现成的话, 不用模型 (语音合成在本机)

LOOK_SCHEMA = {
    "type": "object",
    "properties": {"emo": {"type": "string", "enum": list(I.EMOTIONS)}, "reply": {"type": "string"},
                   "saw": {"type": "string"}},
    "required": ["emo", "reply", "saw"],
    "additionalProperties": False,
}

# -- 哪些窗口能看 -------------------------------------------------------------------------------------------------

# 一律不截: 聊天、密码管理、远程控制, 和 desktop/host/src/activity.cpp 里不留标题的那几类一致
PRIVATE_EXE = {
    "qq.exe", "tim.exe", "wechat.exe", "weixin.exe", "wechatappex.exe", "wxwork.exe", "dingtalk.exe", "feishu.exe",
    "lark.exe", "telegram.exe", "discord.exe", "kook.exe", "slack.exe", "teams.exe", "ms-teams.exe", "whatsapp.exe",
    "keepass.exe", "keepassxc.exe", "1password.exe", "bitwarden.exe", "enpass.exe", "lastpass.exe",
    "todesk.exe", "sunloginclient.exe", "anydesk.exe", "teamviewer.exe", "mstsc.exe",
}
SELF_EXE = {"wmpet.exe", "msedgewebview2.exe"}  # 桌宠自己 (它的页面是 WebView2)
# 不是在看 / 玩: 写代码、办公、文件管理、系统设置 (你切过去就算"去忙了")
WORK_EXE = {
    "code.exe", "cursor.exe", "devenv.exe", "idea64.exe", "pycharm64.exe", "clion64.exe", "webstorm64.exe",
    "goland64.exe", "rider64.exe", "sublime_text.exe", "notepad++.exe", "notepad.exe", "windowsterminal.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe", "claude.exe", "winword.exe", "excel.exe", "powerpnt.exe", "onenote.exe",
    "outlook.exe", "wps.exe", "et.exe", "wpp.exe", "obsidian.exe", "notion.exe", "typora.exe", "acrobat.exe",
    "acrord32.exe", "explorer.exe", "systemsettings.exe", "taskmgr.exe", "mmc.exe", "regedit.exe", "searchhost.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe", "lockapp.exe",
}
BROWSER_EXE = {"chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox", "360se.exe": "360 浏览器",
               "360chrome.exe": "360 极速浏览器", "qqbrowser.exe": "QQ 浏览器", "sogouexplorer.exe": "搜狗浏览器",
               "opera.exe": "Opera", "brave.exe": "Brave", "vivaldi.exe": "Vivaldi", "arc.exe": "Arc"}
PLAYER_EXE = {"potplayermini64.exe": "PotPlayer", "potplayermini.exe": "PotPlayer", "vlc.exe": "VLC",
              "mpc-hc64.exe": "MPC-HC", "哔哩哔哩.exe": "哔哩哔哩", "bilibili.exe": "哔哩哔哩",
              "cloudmusic.exe": "网易云音乐", "qqmusic.exe": "QQ 音乐", "kugou.exe": "酷狗音乐", "spotify.exe": "Spotify",
              "steam.exe": "Steam", "steamwebhelper.exe": "Steam", "wegame.exe": "WeGame", "yuanshen.exe": "原神",
              "genshinimpact.exe": "原神", "starrail.exe": "崩坏：星穹铁道", "zenlesszonezero.exe": "绝区零",
              "leagueclientux.exe": "英雄联盟", "league of legends.exe": "英雄联盟", "cs2.exe": "CS2",
              "dota2.exe": "DOTA 2", "tslgame.exe": "绝地求生", "valorant-win64-shipping.exe": "无畏契约"}
# 标题里有这些就不截 (网页版的网银、支付、邮箱、登录页, 浏览器里的聊天)
PRIVATE_TITLE = re.compile(
    r"(密码|口令|登录|登入|注册|验证码|支付|付款|收银台|银行|网银|钱包|账户|账号|余额|转账|证券|理财|邮箱|收件箱|邮件|"
    r"私信|微信|QQ空间|身份证|password|passcode|sign ?in|log ?in|login|bank|wallet|paypal|alipay|支付宝|"
    r"gmail|outlook|inbox|mail|messenger|whatsapp|telegram|discord)", re.I)
_TITLE_TAIL = re.compile(r"\s*([_\-|·—–]+\s*)?(哔哩哔哩_bilibili|哔哩哔哩|bilibili|YouTube|Google Chrome|Microsoft​? ?Edge|"
                         r"Mozilla Firefox|和另外 \d+ 个页面|and \d+ more pages?)\s*$", re.I)


_EDGE_PROFILE = re.compile(r"\s+-\s+[^-]{1,20}?\s+-\s+Microsoft​?\s?Edge\s*$")
# 网站首页、新标签页: 不算"刷到的一条" (11-08 实测: B 站首页记成了刷到的一条)
HOME_TITLE = re.compile(r"^(哔哩哔哩 \(゜-゜\)つロ 干杯~|哔哩哔哩|bilibili|YouTube|首页|新标签页|新建标签页|New Tab)$", re.I)


def clean_title(title: str) -> str:
    """窗口标题去掉站名、浏览器名的尾巴 ("xxx_哔哩哔哩_bilibili - Google Chrome" -> "xxx";
    Edge 的 "xxx 和另外 3 个页面 - 个人 - Microsoft Edge" 连用户配置名一起去掉)."""
    t = _EDGE_PROFILE.sub("", str(title or "").strip())
    for _ in range(4):
        t2 = _TITLE_TAIL.sub("", t).strip(" -_|·—–")
        if t2 == t:
            break
        t = t2
    return t[:80]


def app_name(exe: str) -> str:
    exe = (exe or "").lower()
    return BROWSER_EXE.get(exe) or PLAYER_EXE.get(exe) or exe.removesuffix(".exe")


def watchable(info: dict | None, screen_area: int | None = None) -> tuple[bool, str]:
    """前台窗口能不能看: (能不能, 为什么). private: 聊天 / 密码 / 网银 / 登录这类; self: 桌宠自己; work: 写代码、办公;
    small: 不认得的程序又不是大窗口 (多半不是在看视频或玩游戏); none: 没有前台窗口 / 最小化了."""
    if not info or info.get("minimized") or not info.get("rect"):
        return False, "none"
    exe, title = str(info.get("exe") or "").lower(), str(info.get("title") or "")
    if exe in PRIVATE_EXE or PRIVATE_TITLE.search(title):
        return False, "private"
    if exe in SELF_EXE:
        return False, "self"
    if exe in WORK_EXE:
        return False, "work"
    if exe in BROWSER_EXE or exe in PLAYER_EXE:
        return True, "ok"
    left, top, right, bottom = info["rect"]
    area = max(0, right - left) * max(0, bottom - top)
    screen_area = screen_area or info.get("screen_area")
    if screen_area and area >= 0.5 * screen_area:  # 不认得的程序占了大半个屏幕: 多半是游戏或别的播放器
        return True, "ok"
    return False, "small"


# -- 截前台那个窗口 (Windows) ----------------------------------------------------------------------------------------

_DPI: list = []


def _dpi_fn():
    """SetThreadDpiAwarenessContext (Win10 1607 起才有; 没有就 None). 用自己的一份 user32, 不改 ctypes.windll 的."""
    if not _DPI:
        import ctypes
        f = None
        with contextlib.suppress(AttributeError, OSError):
            f = ctypes.WinDLL("user32").SetThreadDpiAwarenessContext
            f.restype, f.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
        _DPI.append(f)
    return _DPI[0]


def _dpi_aware():
    """这个线程临时按物理像素算坐标 (显示缩放 125% 时窗口位置和截图对得上); 返回要还原的旧值."""
    if sys.platform != "win32" or _dpi_fn() is None:
        return None
    import ctypes
    with contextlib.suppress(OSError):
        return _dpi_fn()(ctypes.c_void_p(-4))  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    return None


def _dpi_restore(old) -> None:
    if old and _dpi_fn() is not None:
        import ctypes
        with contextlib.suppress(OSError):
            _dpi_fn()(ctypes.c_void_p(old))


_LIBS: dict = {}


def _libs():
    """自己的一份 user32 / kernel32 / gdi32 / dwmapi (WinDLL 新实例): 在这里设参数类型, 不影响进程里别处 (wm/hands.py)
    用 ctypes.windll 调同名函数. 句柄都按指针宽度传, 64 位下不截断."""
    if _LIBS:
        return _LIBS
    import ctypes
    from ctypes import wintypes as W
    u, k, g, d = (ctypes.WinDLL(n) for n in ("user32", "kernel32", "gdi32", "dwmapi"))
    sig = {
        (u, "GetForegroundWindow"): (W.HWND, []),
        (u, "GetWindowTextLengthW"): (ctypes.c_int, [W.HWND]),
        (u, "GetWindowTextW"): (ctypes.c_int, [W.HWND, W.LPWSTR, ctypes.c_int]),
        (u, "GetWindowThreadProcessId"): (W.DWORD, [W.HWND, ctypes.POINTER(W.DWORD)]),
        (u, "GetWindowRect"): (W.BOOL, [W.HWND, ctypes.POINTER(W.RECT)]),
        (u, "IsIconic"): (W.BOOL, [W.HWND]),
        (u, "GetSystemMetrics"): (ctypes.c_int, [ctypes.c_int]),
        (u, "GetDC"): (W.HDC, [W.HWND]),
        (u, "ReleaseDC"): (ctypes.c_int, [W.HWND, W.HDC]),
        (k, "OpenProcess"): (W.HANDLE, [W.DWORD, W.BOOL, W.DWORD]),
        (k, "QueryFullProcessImageNameW"): (W.BOOL, [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]),
        (k, "CloseHandle"): (W.BOOL, [W.HANDLE]),
        (g, "CreateCompatibleDC"): (W.HDC, [W.HDC]),
        (g, "CreateCompatibleBitmap"): (W.HBITMAP, [W.HDC, ctypes.c_int, ctypes.c_int]),
        (g, "SelectObject"): (W.HGDIOBJ, [W.HDC, W.HGDIOBJ]),
        (g, "BitBlt"): (W.BOOL, [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HDC, ctypes.c_int,
                                 ctypes.c_int, W.DWORD]),
        (g, "GetDIBits"): (ctypes.c_int, [W.HDC, W.HBITMAP, W.UINT, W.UINT, ctypes.c_void_p, ctypes.c_void_p, W.UINT]),
        (g, "DeleteObject"): (W.BOOL, [W.HGDIOBJ]),
        (g, "DeleteDC"): (W.BOOL, [W.HDC]),
        (d, "DwmGetWindowAttribute"): (ctypes.c_long, [W.HWND, W.DWORD, ctypes.c_void_p, W.DWORD]),
    }
    for (lib, name), (res, args) in sig.items():
        f = getattr(lib, name)
        f.restype, f.argtypes = res, args
    _LIBS.update(u=u, k=k, g=g, d=d)
    return _LIBS


def foreground() -> dict | None:
    """前台窗口: {hwnd, title, exe, rect (物理像素 l, t, r, b), minimized, screen_area}; 不是 Windows 或拿不到返回 None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes as W
    libs = _libs()
    u, k = libs["u"], libs["k"]
    old = _dpi_aware()
    try:
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        pid = W.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ""
        h = k.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            try:
                size = W.DWORD(1024)
                path = ctypes.create_unicode_buffer(1024)
                if k.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)):
                    exe = path.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
            finally:
                k.CloseHandle(h)
        r = W.RECT()
        if libs["d"].DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
            u.GetWindowRect(hwnd, ctypes.byref(r))  # 9: DWMWA_EXTENDED_FRAME_BOUNDS (不含看不见的拖动边)
        area = u.GetSystemMetrics(0) * u.GetSystemMetrics(1)  # 主屏物理像素 (这个线程按物理像素算)
        return {"hwnd": int(hwnd), "title": buf.value, "exe": exe, "rect": (r.left, r.top, r.right, r.bottom),
                "minimized": bool(u.IsIconic(hwnd)), "screen_area": int(area)}
    except OSError:
        return None
    finally:
        _dpi_restore(old)


def grab(rect, width: int = 640):
    """只截屏幕上 rect 那一块 (BitBlt 那一块, 不截整个屏幕), 缩到 width 宽, 返回 PIL 图; 太小、截不到返回 None."""
    if sys.platform != "win32":
        return None
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    if w < 120 or h < 90:
        return None
    import ctypes
    from ctypes import wintypes as W

    from PIL import Image
    libs = _libs()
    u, g = libs["u"], libs["g"]
    old = _dpi_aware()
    src = u.GetDC(None)
    dst = g.CreateCompatibleDC(src)
    bmp = g.CreateCompatibleBitmap(src, w, h)
    try:
        prev = g.SelectObject(dst, bmp)
        if not g.BitBlt(dst, 0, 0, w, h, src, left, top, 0x00CC0020 | 0x40000000):  # SRCCOPY | CAPTUREBLT
            return None
        g.SelectObject(dst, prev)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                        ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                        ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                        ("biClrImportant", W.DWORD)]
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        data = ctypes.create_string_buffer(w * h * 4)
        if not g.GetDIBits(dst, bmp, 0, h, ctypes.cast(data, ctypes.c_void_p), ctypes.cast(ctypes.pointer(bi), ctypes.c_void_p), 0):
            return None
        img = Image.frombuffer("RGB", (w, h), data, "raw", "BGRX", 0, 1)
    finally:
        g.DeleteObject(bmp)
        g.DeleteDC(dst)
        u.ReleaseDC(None, src)
        _dpi_restore(old)
    img = img.copy()
    img.thumbnail((width, width * 3))
    return img


def tiny(img):
    """比画面变没变用的缩略图 (32x18 灰度)."""
    import numpy as np
    return np.asarray(img.convert("L").resize((32, 18)), dtype=np.float32)


def changed(a, b, threshold: float) -> bool:
    import numpy as np
    return a is None or b is None or float(np.abs(a - b).mean()) >= threshold


def jumped(a, b, threshold: float) -> bool:
    """两张缩略图差得多 (大变化, 如换了一幕): 都有而且平均差 >= threshold; 没有上一张不算."""
    import numpy as np
    return a is not None and b is not None and float(np.abs(a - b).mean()) >= threshold


def sheet(frames: list[tuple[float, object]], cell: int = 480) -> str | None:
    """几张截图拼成一张 (一张就原样, 2-4 张 2x2), 左上角标第几秒; 返回 JPEG 的 data URL. 只在内存里."""
    if not frames:
        return None
    from PIL import Image, ImageDraw
    imgs = []
    for t, im in frames[-4:]:
        im = im.copy()
        im.thumbnail((cell, cell * 3))
        d = ImageDraw.Draw(im)
        d.rectangle((0, 0, 58, 16), fill=(0, 0, 0))
        d.text((4, 2), f"+{int(t)}s", fill=(255, 255, 255))
        imgs.append(im)
    if len(imgs) == 1:
        out = imgs[0]
    else:
        cw, ch = max(i.width for i in imgs), max(i.height for i in imgs)
        cols, rows = 2, (len(imgs) + 1) // 2
        out = Image.new("RGB", (cw * cols, ch * rows), (16, 16, 16))
        for n, im in enumerate(imgs):
            out.paste(im, ((n % cols) * cw, (n // cols) * ch))
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# -- 给模型看 -------------------------------------------------------------------------------------------------------

RULES = (
    "你在陪主人看他电脑屏幕上的东西 (你只看得到他前台那个窗口). 下面是这一阵他屏幕上变过的几张画面 (按时间从左到右、上到下,"
    " 左上角是第几秒) 和窗口标题.\n"
    "像坐在旁边一起看的朋友: 看到好笑、意外、好看、离谱的地方就说一句, 吐槽、感叹、好奇地问一句都行; 口语, 一般不超过 25 字;"
    " 主人刚才跟你说了话就接着他的话说. 不描述画面本身, 不报时间, 不说\"这张图\"\"截图\"; 画面上的字幕、弹幕、游戏里的字可以接.\n"
    "没什么想说的 (画面平淡、和你上一句差不多、看不清) 就把 reply 写成空字符串, 不说.\n"
    "saw 写一句你看到了什么 (给你自己记日记用, 20 字以内, 写内容不写画面; 看不清就写空字符串).\n"
    "只输出一个 JSON: {\"emo\": 说这句时的情绪, \"reply\": 要说的话, \"saw\": 看到了什么}."
)


def look_messages(ctx: dict) -> list[dict]:
    """ctx: {title, app, first, image (data URL), recent: [她前几句], heard: [(你说的, 她回的)]}."""
    head = f"主人在用「{ctx.get('app') or '电脑'}」, 窗口标题《{ctx.get('title') or '没有标题'}》"
    if ctx.get("first"):
        head += " (刚换到这个: 第一眼的反应就好, 不想说就不说)"
    body = [head]
    heard = [(u, r) for u, r in (ctx.get("heard") or []) if u][-3:]
    if heard:
        body.append("主人刚才跟你说: " + " / ".join(f"\"{u}\"" + (f" (你回了\"{r}\")" if r else "") for u, r in heard))
    recent = [x for x in (ctx.get("recent") or []) if x][-2:]
    if recent:
        body.append("你前面说过: " + " / ".join(f"\"{x}\"" for x in recent))
    text = "\n".join(body)
    user: object = text
    if ctx.get("image"):
        user = [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": ctx["image"], "detail": "low"}}]
    return [{"role": "system", "content": RULES}, {"role": "user", "content": user}]


def parse_look(raw: str) -> tuple[str, str, str]:
    """模型输出 -> (情绪, 要说的话, 看到了什么); 不像话的都当不说."""
    emo, reply, saw = "neutral", "", ""
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        start = raw.find("{")
        obj = json.loads(raw[start:raw.rfind("}") + 1]) if start >= 0 else {}
        emo = obj.get("emo") if obj.get("emo") in I.EMOTIONS else "neutral"
        reply, saw = str(obj.get("reply") or ""), str(obj.get("saw") or "")
    reply = re.sub(r"\s+", " ", reply).strip().strip("\"“”")
    if len(reply) > 60:
        cut = max(reply.rfind(c, 0, 60) for c in "。！？!?～~")
        reply = reply[:cut + 1] if cut > 10 else reply[:60]
    return emo, reply, re.sub(r"\s+", " ", saw).strip()[:40]


# -- 一次屏幕陪看 ------------------------------------------------------------------------------------------------------

Speak = Callable[[str, str, str], Awaitable[bool]]          # (历史里的提醒, 要说的话, 情绪) -> 说出去了没有
Think = Callable[[dict], Awaitable[tuple[str, str, str]]]   # ctx -> (情绪, 要说的话, 看到了什么)


class ScreenAlong:
    """一次屏幕陪看. 和 wm/watch_along.WatchAlong 对外一样 (alive / start / stop / pause / resume / note / summary / heard),
    桌宠会话不用分两套; 截图、前台窗口、模型、说话都由外面给 (桌宠会话给真的, 测试给假的)."""

    kind = "screen"

    def __init__(self, conf: dict, *, speak: Speak, think: Think, look: Callable[[], dict | None] = foreground,
                 grabber: Callable = grab, clock_fn: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep, on_end: Callable[[dict], Awaitable] | None = None,
                 on_change: Callable[[dict], Awaitable] | None = None, trace: Callable[[dict], None] | None = None,
                 threaded: bool = True, blocked: Callable[[], str | None] | None = None):
        self.c = {**DEFAULTS, **(conf or {})}
        self.blocked, self.last_block = blocked, ""  # blocked(): 这会儿为什么说不了 (桌宠会话给); 没说出口时记进陪看日志
        self.speak, self.think, self.look, self.grabber = speak, think, look, grabber
        self.now, self.sleep, self.on_end, self.on_change, self.trace = clock_fn, sleep, on_end, on_change, trace
        self.threaded = threaded      # 截图放到线程里做 (测试里假的截图直接调)
        self.state = "starting"       # starting / watching / paused / done
        self.reason = ""
        self.title, self.app = "", ""
        self.items: list[str] = []    # 看过的标题 (刷到的每一条), 按先后
        self.frames: list[tuple[float, object]] = []
        self.lines: list[tuple[float, str]] = []
        self.notes: list[tuple[float, str, str]] = []   # (时刻, 标题, 看到了什么)
        self.heard_log: list[tuple[float, str, str]] = []
        self.stats = {"looks": 0, "said": 0, "silent": 0, "dropped": 0, "gap": 0, "private": 0, "frames": 0, "errors": 0}
        self.started = self.now()
        self.away_since: float | None = None
        self.last_line_t = -1e9
        self.last_look_t = self.started
        self.look_times: list[float] = []
        self.errors, self.give_up = 0, False  # 问模型连着出错几次 (连不上网); 够 max_errors 就说一声不看了
        self.base_pos = None          # 桌宠会话判"在不在等进度"用的; 屏幕陪看不用进度
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None

    # -- 外面调的 (和 WatchAlong 一样) --
    @property
    def alive(self) -> bool:
        return self.state != "done"

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    def position(self) -> float | None:
        return None

    def set_cursor(self, sec: float, by: str = "user") -> None:
        """说进度没用: 她看得到屏幕."""

    def hold_at_start(self) -> None:
        self.pause("user")

    def pause(self, by: str = "user", app: str = "") -> None:
        if self.state in ("watching", "starting"):
            self.state = "paused"
            self.wake.set()
            self._trace("pause", by=by)

    def resume(self) -> None:
        if self.state == "paused":
            self.state = "watching"
            self.frames, self.last_look_t = [], self.now()
            self.wake.set()
            self._trace("resume")

    async def stop(self, reason: str = "user") -> None:
        if self.state == "done":
            return
        self.state, self.reason = "done", reason
        self.wake.set()
        if self.task is not None and self.task is not asyncio.current_task() and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        await self._finish()

    def heard(self, text: str, reply: str = "") -> None:
        """陪看的时候你跟她说的 (和她回的): 下一眼给模型看, 陪看完记进记忆."""
        if text:
            self.heard_log.append((self.now(), str(text)[:200], str(reply or "")[:200]))
            self.heard_log = self.heard_log[-40:]

    def note(self) -> str:
        """给她聊天时看的半句 (她自己的事)."""
        if self.state == "paused":
            now = "主人让你先别看, 等他说继续"
        elif self.away_since is not None:
            now = "主人切到别的窗口去了 (那种你不看), 你等他回来"
        elif self.title:
            now = f"他在看「{self.app}」里的《{self.title}》"
        else:
            now = "你还没看清他在看什么"
        seen = [n for _, _, n in self.notes[-3:]]
        tail = f"; 你刚才看到: {'; '.join(seen)}" if seen else ""
        # 2026-11-08 实测: 你说"嗯那也就看下内容吧", 她以为要去操作电脑 (滚了页面、按了 Alt+Tab); 她本来就看得到
        return (f"你在陪主人看他的屏幕 (你隔几秒看一眼他前台那个窗口, 只看得到那一个; 聊天、密码这类窗口你不看), 此刻{now}{tail}; "
                "他问你看到什么、让你看看内容, 就照你看到的说, 不用去操作电脑")

    def summary(self) -> dict:
        return {"mode": "screen", "title": self.items[-1] if self.items else "", "items": self.items[:30],
                "reason": self.reason, "lines": [s for _, s in self.lines],
                "notes": [f"《{t}》{n}" if t else n for _, t, n in self.notes][-30:],
                "heard": [[u, r] for _, u, r in self.heard_log][-12:],
                "watched_s": round(max(0.0, self.now() - self.started), 1), **self.stats}

    # -- 里面 --
    def _trace(self, event: str, **kw) -> None:
        if self.trace is not None:
            with contextlib.suppress(Exception):
                self.trace({"event": event, "mode": "screen", **kw})

    async def _finish(self) -> None:
        self.frames = []  # 截图只在内存里, 用完就扔
        if self.on_end is not None:
            with contextlib.suppress(Exception):
                await self.on_end(self.summary())

    async def _wait(self, seconds: float) -> None:
        self.wake.clear()
        sleeper = asyncio.ensure_future(self.sleep(seconds))
        waker = asyncio.ensure_future(self.wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for f in (sleeper, waker):
                f.cancel()

    async def _call(self, fn, *args):
        return await asyncio.to_thread(fn, *args) if self.threaded else fn(*args)

    async def _say_soon(self, note: str, reply: str, emo: str, within: float) -> bool:
        """不抢话地说一句 (你在说话、她没说完就隔 2 秒再试, 最多等 within 秒). 没说出口时 last_block 是这一阵里最常见的
        原因 (listening: 麦克风那边在听, 外放时也可能是视频的声音; speculating; busy: 她这一轮没说完)."""
        deadline = self.now() + within
        why: list[str] = []
        while self.state != "done":
            if await self.speak(note, reply, emo):
                return True
            try:
                why.append(str(self.blocked() or "?") if self.blocked is not None else "?")
            except Exception:  # noqa: BLE001
                why.append("?")
            if self.now() >= deadline:
                self.last_block = max(set(why), key=why.count)
                return False
            await self._wait(2.0)
        return False

    async def _run(self) -> None:
        try:
            await self._loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 (陪看出错不能把桌宠会话带崩)
            log.warning("屏幕陪看出错: %s", exc)
            self.reason = self.reason or f"error: {type(exc).__name__}"
        if self.state != "done":
            self.state, self.reason = "done", self.reason or "finished"
            await self._finish()

    async def _loop(self) -> None:
        c = self.c
        self.state = "watching"
        last_title, last_thumb, new_at, key_wait = None, None, None, False
        while self.state != "done":
            if self.now() - self.started > c["max_session_s"]:
                self.reason = "max_session"
                return
            if self.state == "paused":
                await self._wait(c["tick_s"])
                continue
            info = await self._call(self.look)
            ok, why = watchable(info, (info or {}).get("screen_area"))
            if not ok:
                self.stats["private"] += why == "private"
                if self.away_since is None:
                    self.away_since = self.now()
                    self._trace("away", why=why, app=app_name((info or {}).get("exe", "")))
                if self.now() - self.away_since > c["away_s"]:
                    self.reason = "away"
                    await self._say_soon("(这不是主人说的话, 是提醒) 你在陪主人看他的屏幕, 他切到别处好一阵了",
                                         "你去忙啦？那我先不看了，想一起看再叫我～", "neutral", c["late_drop_s"] * 3)
                    return
                await self._wait(c["tick_s"])
                continue
            self.away_since = None
            title, app = clean_title(info.get("title") or ""), app_name(info.get("exe") or "")
            if title != last_title or app != self.app:  # 刷到下一条、换了片、换了网页
                if last_title is not None:
                    self._trace("switch", title=title, app=app)
                last_title, self.title, self.app = title, title, app
                if title and not HOME_TITLE.match(title) and (not self.items or self.items[-1] != title):
                    self.items.append(title)  # 首页、新标签页不算刷到的一条
                new_at, self.frames, last_thumb, key_wait = self.now(), [], None, False
                if self.on_change is not None:
                    with contextlib.suppress(Exception):
                        await self.on_change({"title": title, "app": app})
            img = await self._call(self.grabber, info["rect"], int(c["width"]))
            if img is not None:
                thumb = tiny(img)
                if changed(thumb, last_thumb, float(c["change"])):
                    key_wait = key_wait or (c.get("key_change") is not None and jumped(thumb, last_thumb, float(c["key_change"])))
                    self.frames = [*self.frames, (self.now() - self.started, img)][-int(c["keep"]):]
                    last_thumb = thumb
                    self.stats["frames"] += 1
            first = new_at is not None and self.now() - new_at >= c["first_look_s"] and bool(self.frames)
            due = self.now() - self.last_look_t >= c["look_s"] and len(self.frames) >= 2
            key = key_wait and bool(self.frames) and self.now() - self.last_look_t >= float(c["key_gap_s"])  # 间隔没到就记着, 到了再看
            if (first or due or key) and self._budget():
                await self._look(first, key)
                key_wait = False
                if self.give_up:  # 连不上模型好几次, 已经说了一声
                    return
                new_at = None if first else new_at
            await self._wait(c["tick_s"])

    def _budget(self) -> bool:
        hour = self.now() - 3600
        self.look_times = [t for t in self.look_times if t > hour]
        return len(self.look_times) < int(self.c["max_looks_per_hour"])

    async def _look(self, first: bool, key: bool = False) -> None:
        c = self.c
        frames, self.frames = self.frames[-int(c["keep"]):], []
        since = self.last_look_t
        self.last_look_t = self.now()
        self.look_times.append(self.now())
        self.stats["looks"] += 1
        ctx = {"title": self.title, "app": self.app, "first": first, "image": sheet(frames),
               "recent": [s for _, s in self.lines][-2:], "heard": [(u, r) for t, u, r in self.heard_log if t >= since],
               **({"key": True} if key else {})}
        t0 = time.monotonic()
        try:
            emo, reply, saw = await self.think(ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 (11-08 15:01 实测: 连不上 DeepSeek 一下, 整个陪看就停了、一声没吭)
            self.errors += 1
            self.stats["errors"] += 1
            self._trace("look", first=first, frames=len(frames), result="error", error=f"{type(exc).__name__}: {exc}"[:120],
                        title=self.title[:60], errors=self.errors)
            if self.errors >= int(c["max_errors"]):  # 连着好几次: 说一声再停 (这句不用模型)
                self.reason, self.give_up = "model_down", True
                await self._say_soon("(这不是主人说的话, 是提醒) 你在陪主人看他的屏幕, 但你这边连不上模型好几次了",
                                     DOWN_LINE, "sad", c["late_drop_s"] * 3)
            return
        self.errors = 0
        think_s = round(time.monotonic() - t0, 1)
        if self.state == "done":
            return
        if saw:
            self.notes.append((self.now(), self.title, saw))
        gap = c["first_gap_s"] if first else c["min_gap_s"]
        if not reply:
            self.stats["silent"] += 1
            result = "silent"
        elif self.now() - self.last_line_t < gap:
            self.stats["gap"] += 1
            result = "gap"
        elif await self._say_soon(f"(这不是主人说的话, 是提醒) 你在陪主人看他的屏幕 (《{self.title}》), 你说了一句",
                                  reply, emo, c["late_drop_s"]):
            self.stats["said"] += 1
            self.lines.append((self.now(), reply))
            self.last_line_t = self.now()
            result = "said"
        else:
            self.stats["dropped"] += 1
            result = "dropped"
        self._trace("look", first=first, frames=len(frames), result=result, think_s=think_s, reply=reply[:60],
                    saw=saw[:40], title=self.title[:60], **({"why": self.last_block} if result == "dropped" else {}),
                    **({"key": True} if key else {}))

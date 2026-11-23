"""陪看 (桌宠): 你在电脑上看视频, 她跟着你的进度一段一段看, 快看完一段时说一句 (或者不说).

2026-11-07 用户: "先接陪看和梗" (知识库 09 问题与待办/桌宠与工作台外部资料调研（2026-11-07） 第六节第 1 件: 她在工作台里
自己长出来的本事, 桌宠用不上). 本事是她 10-28 自己做的工作台扩展 video-sense (video_watch: 把视频的一段压成带时间戳的
拼图 + 每格台词, 台词用本机 SenseVoice 离线出); 这里只负责"在桌宠上用": 什么时候调、拿回来的分镜交给模型说一句、
什么时候闭嘴. 她的扩展代码不改, 调法和工作台一样 (wm/agent/mcp_runtime.MCPClient), 只把当前目录换成这次陪看的临时目录.

- 只在你开口时开始: "陪我看" "一起看这个" + B 站链接 / BV 号, 或者本机视频文件的路径; 没给链接她问你要. 说"不看了"就停.
- 不截你的屏幕: 画面和台词都从视频本身来. B 站的视频由她的扩展匿名取 360p, 一次陪看只取一次 (之后用取下来的文件),
  看完连临时目录一起删; 本机的视频直接读.
- 进度: 先问系统媒体控件 (浏览器、播放器自己报的进度), 只认标题对得上这个视频的那个会话 (标题用她的扩展给的;
  没给的 B 站视频从 B 站公开接口匿名查); 对不上的一律不理 (2026-11-07 实测: 你在浏览器里看, 系统里只有开着的 B 站客户端报了一个停着的
  别的视频, 她当成你暂停了, 一直停在 0:45). 读不到 (B 站客户端就不报进度) 就问你一句看到几分了, 之后按墙钟往后推.
  你说"看到 5 分 20 了""暂停了""继续"随时校正; 认准的那个播放器报了暂停、换了片子也跟着停.
- 每一步 (片子多长、进度从哪来、每段切完、说没说、谁暂停的) 记进陪看日志 (logs/watch-*.jsonl), 卡住了看得出卡在哪.
- 节奏 (照 SAOS-3624/dsh-watch-together 的做法, 只借做法): 开头几段短 (1、2、3、4 分钟), 之后每段 5 分钟, 一段最多一句;
  在段尾前 lead_s 秒说; 你正在说话、她这一轮还没说完就等, 等过 late_drop_s 秒就作废这一句 (宁可少说一句, 不晚说);
  读不到进度就问你, 不猜.
- 问你看到哪了、打不开、太长、换片了、出错了这些话也一样等她说完、你说完再说 (问进度最多等 ask_cursor_s 秒, 别的最多
  notice_wait_s 秒). 2026-11-08 实测: 片子 2 秒就打开了, 她"好～我先把视频打开…"那句还没说完, 问进度那句只试了一次就被吞了,
  你没听到, 她空等 3 分钟自己停了. 现在等你回答的 3 分钟从问出口算; 你抢先说了进度就不问; 问了没回, 说一声再停.
- 她问了看到几分以后: "还没开始""还没点开" -> 停在 0:00 等你, 你说"开始了"再走; "开始了""已经开始了""才开始看" -> 从头跟.
  陪看的时候, 桌宠聊天的状态行里带一句她在陪看什么、画面从视频本身来、此刻在等进度 / 跟到哪 (note()).
  (2026-11-08 02:28 实测: 这几句都没认出来, 当普通聊天回了; 聊天那边又不知道她在陪看, 说"画面我看不到".)
- 花钱: 每段一次模型 (按桌宠的额度记); 模型认得出能看图才带拼图, 不然只给台词.
"""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from . import intent as I

log = logging.getLogger(__name__)

EXT = "creator:video-sense"  # 她的工作台扩展在扩展登记里的名字 (wm/agent/extensions.py 的 creator:<包名>)
DEFAULTS = {
    "enabled": True,
    "extension": EXT,
    "ramp_s": [60, 120, 180, 240],  # 开头几段的长度 (秒), 之后每段 segment_s
    "segment_s": 300,
    "cells": 5,                     # 一段分几格
    "lead_s": 15,                   # 段尾前多少秒说
    "late_drop_s": 20,              # 到点以后最多再等多久 (你在说话、她这一轮没说完), 过了就作废这一句
    "busy_retry_s": 2.0,
    "notice_wait_s": 60,            # 打不开、太长、换片、出错、问了没回这些话: 她这会儿没说完 / 你在说话就等, 最多等多久
    "ask_cursor_s": 180,            # 问你看到几分: 轮不到她说最多等这么久; 问出口以后再最多等你这么久, 没回就说一声不看了
    "need_source_s": 300,           # 说了"陪我看"没给链接, 等你发链接最多等多久
    "max_video_s": 3600,            # 比这长的不陪 (取下来太大、要看太久), 先说一声
    "max_session_s": 7200,
    "pause_max_s": 1200,            # 暂停超过这么久就不看了
    "player_check": True,           # 说之前问一眼系统媒体控件 (暂停了、换片了)
    "tool_timeout_s": 180,
    "vision": "auto",               # auto: 模型认得出能看图才带拼图; true / false 强制
    "max_tokens": 600,              # 那一句的上限: deepseek-flash 会先想, 160 不够它想完 (11-07 实测一段 160 全花在想上, 那句是空的)
    "title_lookup": True,           # 她的扩展没给标题时, B 站视频从公开接口查标题 (认播放器、她说话、生活日志都用)
    "max_errors": 3,                # 问模型连着出错 (连不上网) 这么多次就说一声不看了; 不到就这一段不说、接着看
}
DOWN_LINE = "网好像断了，我先不看了，好了再叫我～"  # 连不上模型好几次: 现成的话, 不用模型 (语音合成在本机)
SCHEMA = {
    "type": "object",
    "properties": {"emo": {"type": "string", "enum": list(I.EMOTIONS)}, "reply": {"type": "string"}},
    "required": ["emo", "reply"],
    "additionalProperties": False,
}

# -- 你说的话: 开始、停、看到几分、暂停、继续 ---------------------------------------------------------------
_STOP = re.compile(r"(不看了|别看了|不用(再)?陪我?看了?|不用陪了|停止陪看|关掉陪看|别陪我看了?|不要陪我看了?|先不看了|"
                   r"不想看了|看完了|不陪我看了?|不用跟着看了?)")
_WATCH = re.compile(r"(陪我|陪着我|和我|跟我|咱们|咱俩|我们|你也|一块儿?|一起)[^，。！？,.!?\n]{0,4}?(看|刷|追)")
_VIDEO = re.compile(r"(视频|片子|电影|番|剧|这集|这期|直播|b站|bilibili|这个|它|一下|屏幕|画面)", re.I)
# "陪我玩游戏""看我打一把": 看你的屏幕陪你玩 (wm/screen_along.py; 2026-11-08 用户: "叫一个真人陪自己刷视频、玩游戏会怎么做")
_GAME = re.compile(r"(陪我|陪着我|和我|跟我|咱们|咱俩|我们|一起)[^，。！？,.!?\n]{0,4}?(玩|打)[^，。！？,.!?\n]{0,6}?"
                   r"(游戏|一局|一把|排位|副本)|看我(玩|打)(游戏|一局|一把|排位)?")
_PAUSE = re.compile(r"(暂停了|先暂停|暂停一下|按了暂停|停一下|我去.{0,6}(一下|会儿)|等我一下)")
_RESUME = re.compile(r"(继续(看|放|播)?了?|接着(看|放|播)|开始放了|又开始了|回来了)")
_CURSOR_HINT = re.compile(r"(看到|到了|放到|在|现在|进度|第)")
# 她问了你看到几分 (或你说了暂停) 以后的回答 (2026-11-08 实测: "我还没开始看呢""啊我已经开始了"都没认出来, 当普通聊天回了,
# 陪看那边一直在等; 语音识别还把"从头开始看"听成了"从何开始看")
_NOT_STARTED = re.compile(r"(还没有?|没有?)(开始|点开|打开)|还没有?(放|播|看|点)(?!懂|完|过|到|清|明)")
_STARTED = re.compile(r"开始|(点开|打开|放|播)(了|啦)")
BV = re.compile(r"BV[0-9A-Za-z]{10}")
SHORT = re.compile(r"https?://(?:b23\.tv|bili2233\.cn)/[0-9A-Za-z]{4,16}")
VIDEO_FILE = re.compile(r"[A-Za-z]:[\\/][^\n\"<>|*?]{1,240}?\.(?:mp4|mkv|mov|avi|flv|webm|m4v|wmv|ts)(?![0-9A-Za-z])", re.I)
_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _num(s: str) -> int | None:
    """"12" / "十二" / "二十五" / "两" -> 数字 (0-99); 不认得返回 None."""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if len(s) > 1 and s[0] in "零〇":  # "零五" -> 5
        s = s[1:]
    if "十" in s:
        a, _, b = s.partition("十")
        tens = 1 if a == "" else _CN.get(a)
        ones = 0 if b == "" else _CN.get(b)
        return None if tens is None or ones is None else tens * 10 + ones
    return _CN.get(s) if len(s) == 1 else None


def parse_cursor(text: str) -> float | None:
    """"看到 3 分 20 了" "3:20" "1:02:03" "第五分钟" "三分半" "200 秒" "刚开始" -> 秒; 没说时间返回 None."""
    t = (text or "").replace("：", ":")
    if re.search(r"(刚开始|从头|开头|刚点开|刚打开)", t):
        return 0.0
    m = re.search(r"(?<!\d)(\d{1,2}):(\d{2}):(\d{2})(?!\d)", t)
    if m:
        return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
    m = re.search(r"(?<!\d)(\d{1,3}):(\d{2})(?!\d)", t)
    if m:
        return int(m[1]) * 60 + int(m[2])
    n = r"([0-9]{1,3}|[零一二两三四五六七八九十]{1,3})"
    m = re.search(n + r"\s*(?:个)?\s*(?:小时|钟头)(?:\s*" + n + r"\s*分)?", t)
    if m and _num(m[1]) is not None:
        return _num(m[1]) * 3600 + (_num(m[2]) or 0) * 60 if m[2] else _num(m[1]) * 3600
    m = re.search(n + r"\s*分(?:钟)?\s*(半|" + n + r"\s*秒?)?", t)
    if m and _num(m[1]) is not None:
        sec = 30 if m[2] == "半" else (_num(m[3]) if m[3] else 0)
        if sec is not None and sec < 60:
            return _num(m[1]) * 60 + sec
    m = re.search(r"(?<!\d)(\d{1,4})\s*秒", t)
    if m:
        return float(m[1])
    return None


def find_source(text: str) -> str | None:
    """消息里的 BV 号、B 站短链 (原样返回, 之后再解析) 或者本机视频文件 (要真的存在)."""
    t = text or ""
    if m := BV.search(t):
        return m.group(0)
    if m := SHORT.search(t):
        return m.group(0)
    for m in VIDEO_FILE.finditer(t):
        p = Path(m.group(0).strip())
        if p.is_file():
            return str(p)
    return None


async def resolve_short(url: str, timeout: float = 6.0) -> str | None:
    """b23.tv 短链 -> BV 号: 只问 b23.tv 要跳转地址 (不跟过去、不带 Cookie), 地址里有 BV 就返回."""
    if not SHORT.fullmatch(url):
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False,
                                         cookie_jar=aiohttp.DummyCookieJar()) as s:
            async with s.get(url, allow_redirects=False, headers={"User-Agent": "Mozilla/5.0"}) as r:
                loc = r.headers.get("Location", "")
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    m = BV.search(loc)
    return m.group(0) if m else None


def not_started(text: str) -> bool:
    """"我还没开始看呢" "还没点开": 你还没开始放 (说了具体时间的不算)."""
    t = text or ""
    return bool(_NOT_STARTED.search(t)) and parse_cursor(t) in (None, 0.0)


def command(text: str, *, active: bool, waiting: bool, video_now: bool, asking: bool = False,
            paused: bool = False) -> tuple[str, object] | None:
    """你这句话是不是在说陪看: ("stop",) ("start", 来源或 None) ("source", 来源) ("cursor", 秒) ("pause",) ("resume",)
    ("not_started",). 不是就 None (照常聊天). active: 正在陪看; waiting: 说了陪我看、在等链接; video_now: 宿主报你此刻在看视频;
    asking: 她问了你看到几分、在等你说 ("还没开始" -> 停在 0:00 等你; "开始了""已经开始了" -> 从头); paused: 停着
    ("开始了" -> 接着走)."""
    t = (text or "").strip()
    if not t or len(t) > 200:
        return None
    src = find_source(t)
    if (active or waiting) and _STOP.search(t):
        return ("stop", None)
    if not active and not _STOP.search(t) and ((_WATCH.search(t) and (src or _VIDEO.search(t) or video_now))
                                               or _GAME.search(t)):
        return ("start", src)
    if waiting and src:
        return ("source", src)
    if active:
        if src and _WATCH.search(t):
            return ("start", src)
        if asking and not_started(t):
            return ("not_started", None)
        sec = parse_cursor(t)
        if sec is not None and (_CURSOR_HINT.search(t) or len(t) <= 12):
            return ("cursor", sec)
        if _PAUSE.search(t):
            return ("pause", None)
        if _RESUME.search(t) or (paused and len(t) <= 16 and _STARTED.search(t)):
            return ("resume", None)
        if asking and len(t) <= 24 and _STARTED.search(t):  # "开始了" "已经开始了" "才开始看" "从何开始看" (听岔的"从头")
            return ("cursor", 0.0)
    return None


def clock(sec: float) -> str:
    sec = int(max(0, sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_clock(text) -> float | None:
    """她扩展回的 "03:20" / "1:02:03" -> 秒."""
    if isinstance(text, (int, float)):
        return float(text)
    m = re.fullmatch(r"\s*(?:(\d+):)?(\d{1,3}):(\d{2})(?:\.\d+)?\s*", str(text or ""))
    if not m:
        return None
    return int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3])


# -- 让她看这一段: 她的扩展 ----------------------------------------------------------------------------------------

class HerTool:
    """调她工作台里的扩展 (默认 video-sense). 怎么起她的 MCP 服务和工作台一样 (扩展登记给的命令、校验过的版本), 只是
    当前目录换成这次陪看的临时目录 —— 她的脚本把 B 站的视频下到"当前目录/out/", 不能下进她的扩展版本目录里.
    call(name, args) -> (她回的 JSON, [图片 data URL]). 扩展停了、版本校验没过, 抛 ValueError."""

    def __init__(self, registry, workdir: Path, server_id: str = EXT, timeout: float = 180):
        self.registry, self.workdir, self.server_id, self.timeout = registry, Path(workdir), server_id, timeout

    def config(self) -> dict:
        server = dict(self.registry.server(self.server_id, credentials=False))
        folder = Path(server.get("cwd") or ".")
        args = []
        for a in server.get("args") or []:
            p = folder / a
            args.append(str(p) if not os.path.isabs(a) and p.is_file() else a)  # server.py 写绝对路径, 当前目录才能换
        self.workdir.mkdir(parents=True, exist_ok=True)
        return {**server, "args": args, "cwd": str(self.workdir), "transport": "stdio"}

    async def call(self, name: str, args: dict) -> tuple[dict, list[str]]:
        from .agent.mcp_runtime import MCPClient
        config = self.config()
        async with MCPClient(config, timeout=self.timeout) as client:
            tools = {t.get("name") for t in await client.list_tools() if isinstance(t, dict)}
            if name not in tools:
                raise ValueError(f"她的扩展里没有 {name}")
            result = await client.request("tools/call", {"name": name, "arguments": args})
        data: dict = {}
        images: list[str] = []
        for part in result.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and not data:
                text = str(part.get("text") or "")
                start = text.find("{")
                with contextlib.suppress(ValueError):
                    data = json.loads(text[start:]) if start >= 0 else {}
                if result.get("isError"):
                    data = {"status": "error", "error": text[:300]}
            elif part.get("type") == "image" and len(images) < 3:
                mime, b64 = str(part.get("mimeType") or "image/jpeg"), str(part.get("data") or "")
                if mime.startswith("image/") and 0 < len(b64) <= 3_000_000:
                    images.append(f"data:{mime};base64,{b64}")
        return data, images


# -- 系统媒体控件: 在放还是暂停了、换没换片子 -----------------------------------------------------------------------

# 只用 PowerShell 自带的 WinRT 接口读 (不装 Python 包). 必须是纯 ASCII: Windows PowerShell 5.1 按 ANSI 读 .ps1.
PLAYER_PS = r"""param([int]$TimeoutMs = 4000)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
  $g = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op, $t) { $task = $g.MakeGenericMethod($t).Invoke($null, @($op)); if (-not $task.Wait($TimeoutMs)) { throw 'timeout' }; $task.Result }
  $mt = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
  $mgr = Await ($mt::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
  $rows = @()
  foreach ($s in @($mgr.GetSessions())) {
    $tl = $null; $pb = $null; $title = ''
    try { $tl = $s.GetTimelineProperties() } catch {}
    try { $pb = $s.GetPlaybackInfo() } catch {}
    try { $mi = Await ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties]); if ($mi) { $title = [string]$mi.Title } } catch {}
    $ms = 0; try { $ms = [long]($tl.LastUpdatedTime.ToUnixTimeMilliseconds()) } catch {}
    $rows += [pscustomobject]@{ app = [string]$s.SourceAppUserModelId; status = $(if ($pb) { [string]$pb.PlaybackStatus } else { 'Unknown' });
      position = $(if ($tl) { [math]::Round($tl.Position.TotalSeconds, 2) } else { -1 }); end = $(if ($tl) { [math]::Round($tl.EndTime.TotalSeconds, 2) } else { 0 });
      updated = $ms; title = $title }
  }
  $parts = @($rows | ForEach-Object { ConvertTo-Json -Compress -InputObject $_ -Depth 3 })
  Write-Output ('[' + ($parts -join ',') + ']')
} catch { Write-Output (ConvertTo-Json -Compress -InputObject @{ error = ($_.Exception.Message -replace '[\r\n]+', ' ') }) }
"""


async def read_player(script: Path, timeout: float = 15) -> list[dict] | None:
    """系统媒体控件里所有在报的会话 (浏览器标签页、播放器、B 站客户端…), 见 parse_sessions; 读不到 None.
    哪个是你在看的由 WatchAlong 按标题认: 只看"在放的那个 / 第一个"会认错 (见开头说的 11-07 那次)."""
    if not script.is_file():
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(PLAYER_PS, encoding="ascii")
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec("powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                                    "-File", str(script), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL, **flags)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (OSError, asyncio.TimeoutError):
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.kill()
        return None
    return parse_sessions(out.decode("utf-8-sig", "replace"))


def parse_sessions(text: str, now: float | None = None) -> list[dict] | None:
    """PLAYER_PS 的输出 -> [{status, title, app, position (秒; 不报进度的是 None), age (进度是几秒前刷新的)}]; 读不到 None.
    B 站客户端报 position 0、end 0、刷新时间 1601 年 (就是没有进度), 都算不知道."""
    try:
        rows = json.loads(text.strip() or "null")
    except ValueError:
        return None
    if not isinstance(rows, list):  # 出错时脚本给的是 {"error": …}
        return None
    now = time.time() if now is None else now
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        pos, updated = r.get("position"), r.get("updated")
        age = now - float(updated) / 1000 if isinstance(updated, (int, float)) and updated > 0 else None
        known = isinstance(pos, (int, float)) and pos > 0 and (r.get("end") or 0) > 0 and age is not None
        out.append({"status": str(r.get("status") or "Unknown"), "title": str(r.get("title") or "")[:200],
                    "app": str(r.get("app") or "")[:120], "position": float(pos) if known else None,
                    "age": round(age, 1) if age is not None else None})
    return out


_TITLE_EXT = re.compile(r"\.(mp4|mkv|avi|mov|flv|webm|wmv|m4v|ts|rmvb)$", re.I)
_TITLE_TAIL = re.compile(r"[\s_\-|·—]*(哔哩哔哩|bilibili|youtube).*$", re.I)  # 网页标题的站名尾巴 ("_哔哩哔哩_bilibili")


def _title_key(s) -> str:
    """比标题用: 去掉文件扩展名、网页标题的站名尾巴, 再去掉空白、标点 (中英文的都算)、下划线, 英文小写.
    两边都这么处理, 标题里本来就带"bilibili"的也一样截, 不影响比."""
    s = _TITLE_TAIL.sub("", _TITLE_EXT.sub("", str(s or "").strip()))
    return re.sub(r"[\W_]+", "", s).lower()


def same_title(a, b) -> bool:
    """系统媒体控件报的标题和这个视频的是不是同一个. 浏览器标签页常带"_哔哩哔哩_bilibili"之类的尾巴, 播放器常报带扩展名的
    文件名, 所以一个包含另一个就算 (或者几乎一样); 太短的 (不到 4 个字) 要一模一样."""
    ka, kb = _title_key(a), _title_key(b)
    if not ka or not kb:
        return False
    short, long_ = sorted((ka, kb), key=len)
    if len(short) < 4:
        return ka == kb
    return short in long_ or difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.85


def _playing(row: dict) -> bool:
    return str(row.get("status") or "").lower() == "playing"


# -- 说什么: 交给模型的那一段 ------------------------------------------------------------------------------------

RULES = (
    "你在和主人一起看视频: 主人在电脑上放, 你跟着他的进度看. 下面是他刚看完的这一段: 每格的起止时间和这一格里的台词"
    " (台词是本机离线转写的, 人名、外语、数字可能听错){images}.\n"
    "想说一句就说一句: 像坐在旁边一起看的朋友, 吐槽、感叹、好奇地问一句都行; 口语, 一般不超过 30 字; 不复述剧情、不报时间码、"
    "不说\"这一段\"\"这一格\"; 只说这一段里真有的, 没看懂别装懂; 后面的内容你也还没看, 别猜后面会怎样.\n"
    "没什么想说的 (画面平淡、和你上一句差不多、台词没听清) 就把 reply 写成空字符串, 不说.\n"
    "只输出一个 JSON: {{\"emo\": 说这句时的情绪, \"reply\": 要说的话}}."
)


def line_messages(ctx: dict, see: bool) -> list[dict]:
    """ctx: {title, segment: (a, b), rows: [{from, to, text}], recent: [她前几句], images: [data URL], first, last}."""
    a, b = ctx.get("segment") or (0, 0)
    head = f"《{ctx.get('title') or '主人在看的视频'}》 {clock(a)}-{clock(b)}"
    if ctx.get("first"):
        head += " (你们刚开始一起看)"
    if ctx.get("last"):
        head += " (这是最后一段, 看完了)"
    rows = []
    for r in ctx.get("rows") or []:
        f, t = parse_clock(r.get("from")), parse_clock(r.get("to"))
        text = re.sub(r"\s+", " ", str(r.get("text") or "")).strip()[:160]
        span = f"[{clock(f or 0)}-{clock(t or 0)}]" if f is not None and t is not None else "[?]"
        rows.append(f"{span} {text or '(这一格没台词)'}")
    body = head + "\n" + ("\n".join(rows) if rows else "(这一段没拿到分镜)")
    recent = [x for x in (ctx.get("recent") or []) if x][-2:]
    if recent:
        body += "\n你前面说过: " + " / ".join(f"\"{x}\"" for x in recent)
    images = (ctx.get("images") or [])[:2] if see else []
    system = RULES.format(images=", 图是这一段按时间排的画面 (左到右、上到下)" if images else "")
    user: object = body
    if images:
        user = [{"type": "text", "text": body}, *[{"type": "image_url", "image_url": {"url": u, "detail": "low"}} for u in images]]
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_line(raw: str) -> tuple[str, str]:
    """模型输出 -> (情绪, 要说的话); 说空话、不像话的都当不说."""
    emo, reply = "neutral", ""
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        start = raw.find("{")
        obj = json.loads(raw[start:raw.rfind("}") + 1]) if start >= 0 else {}
        emo = obj.get("emo") if obj.get("emo") in I.EMOTIONS else "neutral"
        reply = str(obj.get("reply") or "")
    reply = re.sub(r"\s+", " ", reply).strip().strip("\"“”")
    if len(reply) > 80:
        cut = max(reply.rfind(c, 0, 80) for c in "。！？!?～~")
        reply = reply[:cut + 1] if cut > 10 else reply[:80]
    return emo, reply


def _why(blocked) -> str:
    """说不了的原因 (桌宠会话的 _aside_blocked: listening / speculating / busy); 不知道就 "?"."""
    if blocked is None:
        return "?"
    try:
        return str(blocked() or "?")
    except Exception:  # noqa: BLE001
        return "?"


def can_see(llm, vision="auto") -> bool:
    """模型看不看得了图: 和外部渠道一样的认法 (deepseek 的 flash、名字带 vl / vision / omni、本机服务自己说能看)."""
    if vision is True or vision is False:
        return vision
    provider, model = str(getattr(llm, "provider", "") or ""), str(getattr(llm, "model", "") or "").lower()
    if not provider:  # 内置的本地小模型: 没装看图的部分
        return False
    info = getattr(llm, "info", None) or {}
    return (provider == "deepseek" and "flash" in model) or any(k in model for k in ("vl", "vision", "omni")) \
        or bool(getattr(llm, "local", False) and info.get("vision"))


# -- 一次陪看 ----------------------------------------------------------------------------------------------------

Speak = Callable[[str, str, str], Awaitable[bool]]          # (历史里的提醒, 要说的话, 情绪) -> 说出去了没有
Think = Callable[[dict], Awaitable[tuple[str, str]]]        # ctx -> (情绪, 要说的话; 空串是不说)
Tool = Callable[[str, dict], Awaitable[tuple[dict, list]]]  # (工具名, 参数) -> (她回的 JSON, 图)
Player = Callable[[], Awaitable[list[dict] | dict | None]]  # 系统媒体控件的所有会话 (read_player); 只给一个 dict 的也认
TitleOf = Callable[[str], Awaitable[str | None]]           # 来源 (BV 号…) -> 标题; 查不到 None
Trace = Callable[[dict], None]                             # 一步一记 (写进陪看日志)


class WatchAlong:
    """一次陪看. tool / speak / think / player / title_of / trace 由外面给 (桌宠会话给真的, 测试给假的)."""

    def __init__(self, conf: dict, source: str, *, tool: Tool, speak: Speak, think: Think, player: Player | None = None,
                 workdir: Path | None = None, clock_fn: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep, on_end: Callable[[dict], Awaitable] | None = None,
                 title_of: TitleOf | None = None, trace: Trace | None = None,
                 on_ready: Callable[[dict], Awaitable] | None = None, blocked: Callable[[], str | None] | None = None):
        self.c = {**DEFAULTS, **(conf or {})}
        self.source = source
        self.tool, self.speak, self.think, self.player = tool, speak, think, player
        self.workdir, self.now, self.sleep, self.on_end = workdir, clock_fn, sleep, on_end
        self.title_of, self.trace, self.on_ready = title_of, trace, on_ready  # on_ready: 片子打开了、知道标题了
        self.blocked, self.last_block = blocked, ""  # blocked(): 这会儿为什么说不了 (桌宠会话给); 没说出口时记进陪看日志
        self.state = "starting"      # starting / need_cursor / watching / paused / done
        self.reason = ""
        self.title = ""
        self.bvid = BV.search(source).group(0) if BV.search(source) else ""
        self.local: str | None = source if not self.bvid else None
        self.duration: float | None = None
        self.base_pos: float | None = None
        self.base_t = 0.0
        self.paused_at: float | None = None
        self.pause_by = ""
        self.match_title = ""            # 认系统媒体控件里哪个会话是这个视频用的标题; 空的 = 谁都不认 (只有 BV 号、没查到标题)
        self.player_app: str | None = None  # 认准过的那个会话是哪个程序报的 (换片子看它); None = 还没认出来过
        self.lines: list[tuple[float, str]] = []   # (片子里第几秒, 她说的话)
        self.notes: list[str] = []                 # 每段台词的开头 (陪看完概括、记进记忆用)
        self.errors = 0                            # 问模型连着出错几次 (连不上网); 够 max_errors 就说一声不看了
        self.heard_log: list[tuple[str, str]] = []  # 陪看时你说的 (和她回的)
        self.stats = {"segments": 0, "said": 0, "silent": 0, "dropped": 0, "asked": 0, "errors": 0, "tool_s": 0.0}
        self.started = self.now()
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None

    # -- 外面调的 --
    @property
    def alive(self) -> bool:
        return self.state != "done"

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    def position(self) -> float | None:
        if self.base_pos is None:
            return None
        if self.paused_at is not None:
            return self.base_pos
        return self.base_pos + (self.now() - self.base_t)

    def set_cursor(self, sec: float, by: str = "user") -> None:
        """by: user (你说的) / player (认准的那个播放器报的; 和推算的差不到 3 秒的不记日志, 免得每 20 秒记一笔)."""
        old = self.position()
        self.base_pos, self.base_t, self.paused_at = max(0.0, float(sec)), self.now(), None
        if self.state in ("need_cursor", "paused"):
            self.state = "watching"
        self.wake.set()
        if by != "player" or old is None or abs(old - self.base_pos) >= 3:
            self._trace("cursor", pos=round(self.base_pos, 1), by=by)

    def pause(self, by: str = "user", app: str = "") -> None:
        """by: user (你说的, 只等你说继续) / player (播放器报的, 播放器又在放了就自己接着走)."""
        if self.base_pos is not None and self.paused_at is None:
            self.base_pos, self.paused_at, self.pause_by = self.position(), self.now(), by
            self.state = "paused"
            self.wake.set()
            self._trace("pause", by=by, pos=round(self.base_pos, 1), **({"app": app} if app else {}))

    def hold_at_start(self) -> None:
        """你说还没开始放: 停在 0:00 等你 (你说"开始了""继续"就从头走)."""
        self.set_cursor(0.0)
        self.pause("user")

    def note(self) -> str:
        """给她聊天时看的半句 (她自己的事): 在陪主人看什么、画面从哪来、此刻在等进度 / 跟到哪 / 暂停了.
        (2026-11-08 实测: 聊天那边不知道她在陪看, 你问"你能看到这个画面吗", 她说看不到; 嘴上说"我跟着看", 陪看那边还在等进度.)"""
        name = f"《{self.title}》" if self.title else "一个视频"
        pos = self.position()
        if self.state == "starting":
            now = "视频还在打开"
        elif pos is None:
            now = "在等主人说他看到几分了 (他说了, 程序就让你跟上)"
        elif self.state == "paused":
            now = "主人还没开始放, 等他说开始" if pos < 1 else f"主人暂停在 {clock(pos)}, 等他说继续"
        else:
            now = f"跟到第 {clock(pos)}"
        heard = f"; 你刚看的那段台词: {self.notes[-1].split('台词: ', 1)[-1][:60]}" if self.notes else ""
        return (f"你在陪主人看{name} (画面和台词是你从视频本身看的, 不看屏幕; 快看完一段说一句), 此刻{now}{heard}; "
                "他问起内容就照你看到的说, 不用去操作电脑")

    def resume(self) -> None:
        if self.paused_at is not None:
            self.base_t, self.paused_at = self.now(), None
            self.state = "watching"
            self.wake.set()
            self._trace("resume", by=self.pause_by, pos=round(self.base_pos or 0.0, 1))

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
        """陪看的时候你跟她说的 (和她回的): 陪看完记进记忆 (pipeline._watch_remember)."""
        if text:
            self.heard_log = [*self.heard_log, (str(text)[:200], str(reply or "")[:200])][-40:]

    def summary(self) -> dict:
        return {"mode": "video", "title": self.title, "bvid": self.bvid, "reason": self.reason,
                "lines": [s for _, s in self.lines], "notes": self.notes[-30:],
                "heard": [[u, r] for u, r in self.heard_log][-12:],
                "watched_s": round(max(0.0, (self.position() or 0.0)), 1), **self.stats}

    # -- 里面 --
    async def _finish(self) -> None:
        if self.workdir is not None:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(shutil.rmtree, self.workdir, True)
        if self.on_end is not None:
            with contextlib.suppress(Exception):
                await self.on_end(self.summary())

    async def _wait(self, seconds: float) -> None:
        """睡一会儿, 你校正进度 / 暂停 / 停的时候提前醒."""
        self.wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake_or_sleep(seconds), seconds + 0.5)

    async def _wake_or_sleep(self, seconds: float) -> None:
        sleeper = asyncio.ensure_future(self.sleep(seconds))
        waker = asyncio.ensure_future(self.wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for f in (sleeper, waker):
                f.cancel()

    async def _call(self, name: str, args: dict) -> tuple[dict, list]:
        t0 = time.monotonic()
        try:
            return await self.tool(name, args)
        finally:
            self.stats["tool_s"] += time.monotonic() - t0

    async def _say(self, note: str, reply: str, emo: str = "neutral") -> bool:
        return await self.speak(note, reply, emo)

    async def _say_soon(self, note: str, reply: str, emo: str = "neutral", within: float | None = None,
                        skip: Callable[[], bool] | None = None) -> bool:
        """不抢话地说一句: 你在说话、她这一轮没说完 (speak 回 False) 就隔 busy_retry_s 再试, 最多等 within 秒
        (默认 notice_wait_s); skip() 为真就不说了 (你已经停了、已经说了进度). 说出去了回 True.
        没说出口时 last_block 是等的这一阵里最常见的原因 (listening / speculating / busy)."""
        c = self.c
        deadline = self.now() + float(c["notice_wait_s"] if within is None else within)
        why: list[str] = []
        while skip is None or not skip():
            if await self._say(note, reply, emo):
                return True
            why.append(_why(self.blocked))
            if self.now() >= deadline:
                self.last_block = max(set(why), key=why.count)
                return False
            await self._wait(c["busy_retry_s"])
        return False

    def _trace(self, event: str, **kw) -> None:
        if self.trace is not None:
            with contextlib.suppress(Exception):
                self.trace({"event": event, **kw})

    async def _sessions(self) -> list[dict] | None:
        if self.player is None:
            return None
        rows = await self.player()
        if isinstance(rows, dict):  # 只报一个的 (测试里的假播放器)
            rows = [rows]
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else None

    def _match(self, rows: list[dict] | None) -> dict | None:
        """系统媒体控件里放着这个视频的那个会话: 标题对得上才算 (不知道标题就谁都不认); 好几个对得上的先挑认准过的程序、在放的."""
        if not rows or not self.match_title:
            return None
        hits = [r for r in rows if same_title(r.get("title"), self.match_title)]
        if not hits:
            return None
        hits.sort(key=lambda r: ((r.get("app") or "") != self.player_app, not _playing(r)))
        m = hits[0]
        if self.player_app is None:
            self.player_app = str(m.get("app") or "")
            self._trace("player", action="bound", app=self.player_app, title=str(m.get("title") or "")[:80],
                        status=str(m.get("status") or ""), has_pos=m.get("position") is not None)
        return m

    async def _run(self) -> None:
        try:
            await self._loop()
            if self.state != "done":
                self.state, self.reason = "done", self.reason or "finished"
                await self._finish()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 (陪看出错不能把桌宠会话带崩)
            log.warning("陪看出错: %s", exc)
            if self.state != "done":
                self.reason = f"error: {type(exc).__name__}"
                with contextlib.suppress(Exception):  # 告诉你一声, 别悄悄停了 (等这会儿说完的说完; 等的时候你说不看了就不说)
                    await self._say_soon(f"(这不是主人说的话, 是提醒) 你们在一起看《{self.title or '视频'}》, 但你这边出了错没看成",
                                         "这个视频我这边没打开（可能太大、网不好），先不陪着看了……", "sad",
                                         skip=lambda: self.state == "done")
                if self.state != "done":
                    self.state = "done"
                    await self._finish()

    async def _loop(self) -> None:
        c = self.c
        # 1. 先摸清片子: 多长、叫什么; B 站的这一步由她的扩展取下来 (之后都用取下来的文件, 不再重复取)
        probe, _ = await self._call("video_probe", {"source": self.source})
        stopped = lambda: self.state == "done"  # noqa: E731 (等着说的时候你说了不看了: 不说了)
        if probe.get("status") == "error" or not probe.get("duration"):
            self.reason = "probe_failed"
            await self._say_soon("(这不是主人说的话, 是提醒) 主人让你陪他看视频, 但你没打开这个视频", "这个视频我打不开……换个链接试试？",
                                 "sad", skip=stopped)
            return
        self.duration = float(probe["duration"])
        title = str(probe.get("title") or "").strip()
        if not title and self.title_of is not None and c.get("title_lookup", True):
            try:  # 她的扩展没给标题 (一般会给): B 站视频从公开接口查 (查不到照样看, 只是认不了播放器)
                title = str(await asyncio.wait_for(self.title_of(self.source), 15) or "").strip()
            except Exception as exc:  # noqa: BLE001
                self._trace("title_lookup_failed", error=f"{type(exc).__name__}: {exc}"[:160])
                title = ""
        self.title = (title or Path(str(probe.get("input") or self.source)).stem)[:80]
        # 认播放器用的标题: 真标题; 本机文件没标题就用文件名 (播放器多半报文件名); B 站只有 BV 号的谁都不认
        self.match_title = title or (Path(self.source).stem if not self.bvid else "")
        got = str(probe.get("input") or "")
        if got:
            p = Path(got) if os.path.isabs(got) or self.workdir is None else self.workdir / got
            if p.is_file():
                self.local = str(p)
        self._trace("probe", duration=round(self.duration, 1), title=self.title, match=bool(self.match_title),
                    local=bool(self.local))
        if self.duration > c["max_video_s"]:
            self.reason = "too_long"
            await self._say_soon(f"(这不是主人说的话, 是提醒) 主人让你陪他看《{self.title}》, 但片子太长",
                                 f"这个有 {int(self.duration // 60)} 分钟，太长啦，我先不陪着看了，挑一段给我？", "shy", skip=stopped)
            return
        if self.on_ready is not None and self.match_title:  # 知道标题了 (桌宠页面的提示条写《标题》)
            with contextlib.suppress(Exception):
                await self.on_ready({"source": self.source, "title": self.title, "duration": round(self.duration, 1)})
        src = self.local or self.source
        # 2. 进度: 你开口时说了就用你说的; 没说先问系统媒体控件 (只认对得上的那个会话); 读不到就问你
        pos = self.position()
        if pos is None and self.player is not None:
            m = self._match(await self._sessions())
            if m and m.get("position") is not None and (m.get("age") or 0) <= 45:
                self.set_cursor(m["position"] + ((m.get("age") or 0) if _playing(m) else 0), by="player")
                pos = self.position()
        if pos is None:
            self.state = "need_cursor"
            t0 = self.now()  # 她"好～我先把视频打开…"多半还没说完: 等她说完再问; 你抢先说了进度就不问
            asked = await self._say_soon(f"(这不是主人说的话, 是提醒) 你们要一起看《{self.title}》, 你读不到播放进度",
                                         "你现在看到几分几秒啦？告诉我，我好跟上～", "curious" if "curious" in I.EMOTIONS else "neutral",
                                         within=c["ask_cursor_s"], skip=lambda: self.base_pos is not None or self.state == "done")
            self.stats["asked"] += 1 if asked else 0
            self._trace("ask_cursor", said=asked, waited_s=round(self.now() - t0, 1),
                        **({"why": self.last_block} if not asked and self.last_block else {}))
            deadline = self.now() + (c["ask_cursor_s"] if asked else 0)  # 等你回答从问出口算
            while self.base_pos is None and self.state != "done" and self.now() < deadline:
                await self._wait(min(10.0, deadline - self.now()))
            if self.base_pos is None:
                self.reason = "no_cursor"
                if asked:  # 问了没回: 说一声再停, 别悄悄停了
                    await self._say_soon(f"(这不是主人说的话, 是提醒) 你们要一起看《{self.title}》, 你问了主人看到哪了, 主人没回",
                                         "你没说看到哪了，那我先不跟啦，想一起看再叫我～", "neutral", skip=stopped)
                return
        if self.paused_at is None:  # 你说"还没开始" (停在 0:00 等你) 的, 照旧停着
            self.state = "watching"
        # 3. 一段一段看
        i = 0
        a = self.position() or 0.0
        while self.state != "done":
            if self.now() - self.started > c["max_session_s"]:
                self.reason = "max_session"
                return
            seg = (c["ramp_s"][i] if i < len(c["ramp_s"]) else c["segment_s"])
            a = max(0.0, min(a, self.duration))
            b = min(self.duration, a + seg)
            if self.duration - a < 5:  # 片子只剩不到 5 秒
                self.reason = "finished"
                return
            data, images = await self._call("video_watch", {
                "source": src, "cursor": str(int(a)), "segment_s": int(seg), "cells": int(c["cells"]),
                "lead_s": float(c["lead_s"]), **({"out_dir": str(self.workdir / f"seg-{int(a):05d}")} if self.workdir else {})})
            self.stats["segments"] += 1
            if data.get("status") == "finished":
                self.reason = "finished"
                return
            rows = data.get("storyboard") if isinstance(data.get("storyboard"), list) else []
            self._trace("segment", a=round(a), b=round(b), rows=len(rows), images=len(images))
            said_here = " ".join(re.sub(r"\s+", " ", str(r.get("text") or "")).strip() for r in rows if isinstance(r, dict))
            if said_here.strip():
                self.notes.append(f"{clock(a)}-{clock(b)} 台词: {said_here.strip()[:120]}")
            last = b >= self.duration - 5
            t0 = time.monotonic()
            failed = ""
            try:
                emo, reply = await self.think({"title": self.title, "segment": (a, b), "rows": rows, "images": images,
                                               "recent": [s for _, s in self.lines], "first": i == 0, "last": last})
                self.errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 (连不上网: 这一段不说, 接着看; 连着好几次才说一声停)
                self.errors += 1
                self.stats["errors"] += 1
                failed, (emo, reply) = f"{type(exc).__name__}: {exc}"[:120], ("neutral", "")
                if self.errors >= int(c["max_errors"]):
                    self._trace("line", a=round(a), b=round(b), result="error", error=failed, errors=self.errors)
                    self.reason = "model_down"
                    await self._say_soon(f"(这不是主人说的话, 是提醒) 你们在一起看《{self.title}》, 但你这边连不上模型好几次了",
                                         DOWN_LINE, "sad", skip=stopped)
                    return
            think_s = round(time.monotonic() - t0, 1)
            # 等到这一段快看完 (你暂停、校正进度会改这个时刻)
            moved = await self._until(b - c["lead_s"], a, b)
            if self.state == "done":
                return
            if moved is not None:  # 你跳到别处了: 这一句作废, 从新位置接着看
                self.stats["dropped"] += 1 if reply else 0
                self._trace("line", a=round(a), b=round(b), result="moved", to=round(moved, 1), think_s=think_s,
                            reply=reply[:60])
                a, i = moved, 0
                continue
            if failed:
                result = "error"
            elif not reply:
                self.stats["silent"] += 1
                result = "silent"
            elif await self._deliver(a, b, emo, reply):
                self.stats["said"] += 1
                self.lines.append((b, reply))
                result = "said"
            else:
                self.stats["dropped"] += 1
                result = "dropped"
            self._trace("line", a=round(a), b=round(b), result=result, think_s=think_s, reply=reply[:60],
                        **({"why": self.last_block} if result == "dropped" else {}),
                        **({"error": failed, "errors": self.errors} if failed else {}))
            if last:
                self.reason = "finished"
                return
            a, i = b, i + 1

    async def _until(self, when: float, a: float, b: float) -> float | None:
        """等到片子放到 when 秒. 你暂停就一直等 (太久就停); 进度跳出这一段 (往回或往后跳过) 返回新位置."""
        c = self.c
        checked = self.now()
        back = c["lead_s"] + 15  # 下一段是提前算的: 你还在看上一段的最后 lead_s 秒, 不算往回拖
        while self.state != "done":
            pos = self.position()
            if pos is None:
                await self._wait(5)
                continue
            if pos < a - back or pos > b + c["late_drop_s"] + 30:
                return pos
            if self.state == "paused":
                if self.now() - (self.paused_at or self.now()) > c["pause_max_s"]:
                    self.reason = "paused_too_long"
                    await self.stop("paused_too_long")
                    return None
                await self._wait(10)
                if self.pause_by == "player" and c["player_check"] and self.player is not None:
                    await self._player_says_stop()  # 播放器又在放了: 接着走
                continue
            if pos >= when:
                if c["player_check"] and self.player is not None:
                    if await self._player_says_stop():
                        continue  # 播放器说暂停了: 回去等; 换片了: state 已经是 done
                return None
            # 每隔 20 秒问一眼播放器 (暂停了就停表), 其余时间睡到点
            wait = min(when - pos, 20.0)
            await self._wait(max(0.5, wait))
            if c["player_check"] and self.player is not None and self.now() - checked >= 20:
                checked = self.now()
                await self._player_says_stop()
        return None

    async def _player_says_stop(self) -> bool:
        """问一眼系统媒体控件, 只看放着这个视频的那个会话 (标题对得上的; 别的程序、别的视频一律不理):
        暂停了 -> 停表 (True); 认准过的那个程序在放别的、这个视频不见了 -> 换片了, 说一声不看了 (True);
        在放 -> 有新进度就校正 (False)."""
        rows = await self._sessions()
        if not rows:
            return False
        m = self._match(rows)
        if m is None:
            other = next((r for r in rows if self.player_app is not None and (r.get("app") or "") == self.player_app
                          and _playing(r)), None)
            if other is None:
                return False
            self._trace("player", action="changed", app=self.player_app, title=str(other.get("title") or "")[:80])
            self.reason = "changed"
            with contextlib.suppress(Exception):  # 别悄悄停了 (她这会儿没说完就等一会儿再说)
                await self._say_soon(f"(这不是主人说的话, 是提醒) 你们在一起看《{self.title}》, 主人换了别的视频",
                                     "你换视频啦？那这个我先不跟了～", "neutral", skip=lambda: self.state == "done")
            await self.stop("changed")
            return True
        status = str(m.get("status") or "").lower()
        if status == "paused":
            self.pause("player", app=str(m.get("app") or ""))
            return True
        if status == "playing":
            if self.paused_at is not None and self.pause_by == "player":
                self.resume()
            if m.get("position") is not None and (m.get("age") or 99) <= 10:
                self.set_cursor(m["position"] + (m.get("age") or 0), by="player")
        return False

    async def _deliver(self, a: float, b: float, emo: str, reply: str) -> bool:
        """到点说: 你在说话、她这一轮没说完就隔 busy_retry_s 再试, 晚了 late_drop_s 就作废."""
        note = f"(这不是主人说的话, 是提醒) 你们在一起看《{self.title}》, 主人刚看完 {clock(a)}-{clock(b)} 这一段, 你说了一句"
        return await self._say_soon(note, reply, emo, within=self.c["late_drop_s"], skip=lambda: self.state == "done")

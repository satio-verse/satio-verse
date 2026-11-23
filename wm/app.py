"""aiohttp 应用: 静态前端 + /ws 实时通道 + /api 状态接口."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path

from aiohttp import WSMsgType, web

from . import beat
from . import diagnose
from . import hast
from . import intent as I
from . import ledger as L
from . import memory_core as MC
from . import motion_growth
from . import privilege
from . import services
from . import tz as TZ
from .components import Components
from .agent import heartbeat as HB
from .agent.core import AgentHub
from .channels import mailbox_http, vault_http
from .agent import downloads_http
from .agent import notebook_http
from . import llm_http
from .config import ROOT, resolve_path
from .digest import Digest
from .engines import Engines
from .pipeline import Session
from .resources import ResourceMonitor
from .trace import MARKS, METRICS, LatencyStats, TraceLog

log = logging.getLogger("wm.app")
WEB_DIR = ROOT / "web"

CFG = web.AppKey("cfg", dict)
CLOCK = web.AppKey("clock", TZ.Clock)
COMPONENTS = web.AppKey("components", Components)
ENGINES = web.AppKey("engines", Engines)
STATS = web.AppKey("stats", LatencyStats)
TRACELOG = web.AppKey("tracelog", TraceLog)
RESOURCES = web.AppKey("resources", ResourceMonitor)
SESSIONS = web.AppKey("sessions", set)  # 连着的本人会话 (/ws): 要重启时一个个告诉
BOOT = web.AppKey("boot", dict)
LEDGER = web.AppKey("ledger", L.Ledger)
AGENT = web.AppKey("agent", AgentHub)
MEMORY_CORE = web.AppKey("memory_core", MC.CoreService)  # 记忆核心 (阶段 1 影子): 要看关系网时才建


def boot_info() -> dict:
    """这次启动的情况 (看守进程 tools/serve.py 起服务端时用环境变量告诉的; 直接 python -m wm 起的就是 normal), 放进 hello:
    id 启动编号; mode normal / restarted (照请求重启的) / crashed (上次崩了) / reverted (上次崩了, 退回了刚合进来的改动);
    note 给人看的一句; learned 这次启动在验证的、刚合进来的本事. 桌宠醒来时据此说一句 (docs/roadmap/self-evolution.md 4.3)."""
    return {"id": os.environ.get("WM_BOOT_ID") or uuid.uuid4().hex[:8], "mode": os.environ.get("WM_BOOT_MODE") or "normal",
            "note": os.environ.get("WM_BOOT_NOTE") or "",
            "learned": [x for x in (os.environ.get("WM_BOOT_LEARNED") or "").split("\n") if x.strip()]}


@web.middleware
async def no_cache(request: web.Request, handler):
    resp = await handler(request)
    if request.path.startswith("/static/vendor/"):
        resp.headers["Cache-Control"] = "max-age=86400"  # 三方库和生成的 Soullink 模块很少变
    elif request.path.startswith("/static/") or request.path == "/":
        resp.headers["Cache-Control"] = "no-store"
    return resp


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


async def api_status(request: web.Request) -> web.Response:
    engines: Engines = request.app[ENGINES]
    await engines.refresh()
    return web.json_response({**engines.status(), "privilege": privilege.status()})  # 以管理员身份在跑时 elevated 为 true (wm/privilege.py)


async def api_resources(request: web.Request) -> web.Response:
    """实时资源: 最近一次采样 (CPU/内存/显卡/本项目进程/占 CPU 最多的外部进程) 和最近两分钟的曲线."""
    return web.json_response(request.app[RESOURCES].snapshot())


async def api_stats(request: web.Request) -> web.Response:
    return web.json_response(request.app[STATS].summary())


async def api_supervisor(request: web.Request) -> web.Response:
    """看守进程要重启 (或停掉) 服务端之前先调一下 (带它起服务端时给的钥匙, 别人调回 403): 告诉每个连着的桌宠
    (下行 server_restart {reason, eta_s, stop}: 小屋里她去床上睡一会儿, 这期间你的话变便利贴, 服务端回来再醒),
    再等正在说的那一轮说完 (最多 drain_s 秒, 音频发完了桌宠那边照样播得完). 回 {ok, sessions, waited_s}."""
    key = os.environ.get("WM_SUPERVISOR_KEY")
    if not key or request.headers.get("X-Supervisor-Key") != key:
        raise web.HTTPForbidden(text="只有看守进程能调")
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    body = body if isinstance(body, dict) else {}
    stop = body.get("action") == "stop"
    eta = body.get("eta_s")
    # 服务要重启/停掉: 先把正在跑的工作台任务停在原地, 给她留一句话, 别让跑到一半的会话硬断成"连接中断"
    # (2026-10-30 主人: "重启前先把在跑的会话停了、留一句『我先去重启，回来接着做』")
    hub = request.app[AGENT]
    busy = [s for s in list(hub.sessions.values()) if s.status != "idle" and not s.parent_id]
    for s in busy:
        try:
            s.run["restart_pending"] = True
            s.stop_event.set()
            await hub.teams.cancel(s.id)
            await hub.emit(s, {"type": "note",
                               "text": "我先去重启一下，回来接着做（这一轮的中间状态可能没存全，回来先核对产物）。"})
            hub.save(s)
        except Exception as exc:  # noqa: BLE001
            log.warning("重启前停会话 %s 没停成: %s", s.id, exc)
    if busy:
        t_stop = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - t_stop < 3 and any(s.status != "idle" for s in busy):
            await asyncio.sleep(0.1)
        log.info("重启前停了 %d 个在跑的工作台会话", len(busy))
    sessions = list(request.app[SESSIONS])
    for s in sessions:
        await s.send({"type": "server_restart", "reason": str(body.get("reason") or ("stop" if stop else "restart"))[:60],
                      "eta_s": eta if isinstance(eta, (int, float)) and not stop else None, "stop": stop})
    loop = asyncio.get_running_loop()
    t0, drain = loop.time(), min(float(body.get("drain_s", 8) or 0), 30.0)
    while loop.time() - t0 < drain and any(s.current is not None and not s.current.finalized for s in sessions):
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.3)  # 让 server_restart 发出去
    log.info("看守进程要%s: 告诉了 %d 个桌宠, 等了 %.1f 秒", "停掉服务端" if stop else "重启", len(sessions), loop.time() - t0)
    return web.json_response({"ok": True, "sessions": len(sessions), "waited_s": round(loop.time() - t0, 1)})


_VRM_NAME = re.compile(r"^[\w.\-]+\.vrm$", re.ASCII)
_MOTIONDB_NAME = re.compile(r"^[a-z]+\.(json|bin|motion)$", re.ASCII)


def _motiondb_dir(app: web.Application) -> Path:
    return resolve_path(app[CFG].get("motion", {}).get("locomotion_dir", "models/motion"))


def locomotion_styles(app: web.Application) -> list[str]:
    """已生成的运动匹配风格库 (tools/build_motion_db.py 的输出), json 和 bin 都在才算."""
    d = _motiondb_dir(app)
    return sorted(p.stem for p in d.glob("*.json")
                  if p.stem not in ("actions", "idle", "gestures") and (d / f"{p.stem}.bin").is_file()) if d.is_dir() else []


_HPE_NAME = re.compile(r"^[\w.\-]+\.task$", re.ASCII)
mimetypes.add_type("application/wasm", ".wasm")  # Windows 上默认不认识 .wasm, 浏览器会拒绝流式编译


async def hpe_file(request: web.Request) -> web.StreamResponse:
    """人体姿态估计的模型文件 (models/hpe/*.task, 浏览器里的 MediaPipe 载入)."""
    name = request.match_info["name"]
    path = ROOT / "models" / "hpe" / name
    if not _HPE_NAME.match(name) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})


async def motiondb_file(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    # <库>.motion 是 <库>.bin 的别名, 前端用它: 本机的 Chrome 对以 .bin 结尾的 fetch 一直不给响应 (同一个服务上
    # 48 MB 的 .vrm、JSON、脚本都正常; curl 也正常), 换个扩展名就好. .bin 仍然可以直接取
    m = _MOTIONDB_NAME.match(name)
    # 动作参数: 她自己练出来、验证过才换上的那一版 (wm/motion_growth.py, 在用户数据区) 叠在出厂参数上; 没换上过就照常给文件
    if name == "params.json" and (g := motion_growth.growth_of(request.app)) is not None and (body := g.params_payload()) is not None:
        return web.json_response(body, headers={"Cache-Control": "no-cache"})
    path = _motiondb_dir(request.app) / (name[:-len(".motion")] + ".bin" if m and m.group(1) == "motion" else name)
    if not m or not path.is_file():
        raise web.HTTPNotFound()
    # 重建数据库后浏览器要拿到新的 (未变时走 304)
    ctype = "application/json" if name.endswith(".json") else "application/x-wm-motion"
    return web.FileResponse(path, headers={"Cache-Control": "no-cache", "Content-Type": ctype})


def _avatar_dir(app: web.Application) -> Path:
    return resolve_path(app[CFG].get("avatar", {}).get("dir", "models/vrm"))


async def api_avatars(request: web.Request) -> web.Response:
    """列出 models/vrm 里的角色模型和默认角色; 前端据此加载, 没有就退回 2D 形象."""
    directory = _avatar_dir(request.app)
    items = [{"name": p.name, "bytes": p.stat().st_size}
             for p in sorted(directory.glob("*.vrm")) if _VRM_NAME.match(p.name)] if directory.exists() else []
    default = request.app[CFG].get("avatar", {}).get("default")
    if default not in {i["name"] for i in items}:
        default = items[0]["name"] if items else None
    return web.json_response({"default": default, "items": items})


async def avatar_file(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    path = _avatar_dir(request.app) / name
    if not _VRM_NAME.match(name) or not path.is_file():
        raise web.HTTPNotFound()
    # 每次向服务器确认 (没变时 304): 重新转换模型 (tools/convert_vrm.py) 后刷新页面就能拿到新文件
    return web.FileResponse(path, headers={"Content-Type": "model/gltf-binary", "Cache-Control": "no-cache"})


# /ws 的来源检查: 浏览器允许跨源 WebSocket, 不查的话本机打开的任何网页都能连上来, 还能发 profile 让服务端把对话发到它指定的地址.
# 防 DNS 重绑定: 网页把自己的域名解析到 127.0.0.1 也连得进来, 但 Host 是它的域名、Origin 是它的网址.
_LOOPBACK = ("127.0.0.1", "localhost", "[::1]")


def _local_authorities(port: int | None) -> set[str]:
    """本机的 "主机:端口" 写法 (80 端口时浏览器不写端口)."""
    if port is None:
        return set()
    out = {f"{h}:{port}" for h in _LOOPBACK}
    return out | set(_LOOPBACK) if port == 80 else out


def ws_refusal(request: web.Request) -> str | None:
    """握手前查 Host 和 Origin: 不放行时返回原因 (回 403), 放行返回 None.
    放行的: 本机页面 (http://127.0.0.1:端口、http://localhost:端口, 网页前端和桌宠页面都同源)、没有 Origin 的非浏览器客户端、
    server.allowed_origins 里的来源 (它们的 "主机:端口" 也算合格的 Host, 给局域网访问用)."""
    server = request.app[CFG].get("server") or {}
    sock = request.transport.get_extra_info("sockname") if request.transport else None
    port = sock[1] if isinstance(sock, tuple) and len(sock) >= 2 else server.get("port")  # 实际监听的端口 (测试里是随机的)
    extra = {str(o).strip().rstrip("/").lower() for o in server.get("allowed_origins") or []}
    local = _local_authorities(port)
    if request.headers.get("Host", "").strip().lower() not in local | {o.split("://", 1)[1] for o in extra if "://" in o}:
        return "Host 不对: 只接受 127.0.0.1 或 localhost (防 DNS 重绑定)"
    origin = request.headers.get("Origin")
    if origin is None:
        return None  # 不是浏览器 (测试、命令行工具): 浏览器的 WebSocket 握手一定带 Origin
    origin = origin.strip().rstrip("/").lower()
    if origin in extra or (origin.startswith("http://") and origin[len("http://"):] in local):
        return None
    return "Origin 不对: 只接受本机页面和 server.allowed_origins 里的来源"


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    app = request.app
    if (why := ws_refusal(request)) is not None:
        log.warning("拒绝了 /ws 连接: %s (Origin=%r, Host=%r)", why, request.headers.get("Origin"), request.headers.get("Host"))
        raise web.HTTPForbidden(text=why)
    ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024, heartbeat=30)
    await ws.prepare(request)

    # 所有下行消息走同一个队列, 保证 JSON 与二进制音频帧的先后顺序.
    outbox: asyncio.Queue = asyncio.Queue()

    async def sender():
        while (msg := await outbox.get()) is not None:
            try:
                if isinstance(msg, (bytes, bytearray)):
                    await ws.send_bytes(msg)
                else:
                    await ws.send_str(json.dumps(msg, ensure_ascii=False))
            except (ConnectionResetError, RuntimeError):
                break

    async def send(msg) -> None:
        outbox.put_nowait(msg)

    send_task = asyncio.create_task(sender())
    engines: Engines = app[ENGINES]
    await engines.probe_slots()  # Session 按连接分 LLM 槽位, 先按 llama-server 实际的槽位数限定
    session = Session(engines, app[CFG], send, app[STATS], app[TRACELOG])
    app[SESSIONS].add(session)
    await send({
        "type": "hello",
        "status": engines.status(),
        "stats": app[STATS].summary(),
        "marks": MARKS,
        "metrics": {k: v[2] for k, v in METRICS.items()},
        "skills": I.SKILLS,
        "asr_sample_rate": engines.asr.sample_rate,
        # 页面取麦克风时要浏览器做哪些处理 (能去掉外放回声时让它别降噪、别自动调音量; None = 照浏览器默认; wm/echo.py)
        "mic": engines.mic_processing() if hasattr(engines, "mic_processing") else None,
        "boot": app[BOOT],
        "room_gen": engines.room.generation if engines.room is not None else None,  # 删除代次: 页面见到更大的就清掉本机存的
    })
    await session.send_candidates(always=False)  # 有等用户确认的候选记忆就紧跟着 hello 发一份
    await session.send_channels()  # QQ 渠道开着时: 状态和还没处理的草稿
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await session.on_audio(msg.data)
            elif msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                await session.on_message(data)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        app[SESSIONS].discard(session)
        await session.close()
        outbox.put_nowait(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(send_task, 2)
    return ws


async def guest_handler(request: web.Request) -> web.WebSocketResponse:
    """/guest: 访客连接 (桌宠本机接口的 wm-guest 用, 给 AstrBot 这类第三方框架): 只认 chat, 每句交给渠道管理器的访客会话
    (没有工具、读不到主人的记忆、不写记忆, 按 user 分会话). 和 /ws 是两个入口, 这里没有 profile / tools / state 这些消息."""
    if (why := ws_refusal(request)) is not None:
        log.warning("拒绝了 /guest 连接: %s", why)
        raise web.HTTPForbidden(text=why)
    ws = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=30)
    await ws.prepare(request)
    channels = request.app[ENGINES].channels
    await ws.send_json({"type": "hello", "guest": True})
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") != "chat":
            await ws.send_json({"type": "error", "id": data.get("id"), "message": "访客连接只认 chat"})
            continue
        r = await channels.guest_chat(data.get("user"), data.get("text"))
        await ws.send_json({"type": "reply", "id": data.get("id"), **r})
    return ws


async def api_billing(request: web.Request) -> web.Response:
    """账本 (面板"账号与余额"页): 峰谷、余额、今天和这个月的花费、命中率、最近几天、最近几条. 余额超过 60 秒没查就在后台查一次."""
    ledger = request.app[LEDGER]
    ledger.kick(max_age=60)
    return web.json_response(ledger.summary())


async def api_billing_refresh(request: web.Request) -> web.Response:
    """面板上点"刷新": 马上查一次余额 (最多等 10 秒), 再回整份账本."""
    ledger = request.app[LEDGER]
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(ledger.refresh_balance(force=True), 10)
    return web.json_response(ledger.summary())


async def api_billing_budget(request: web.Request) -> web.Response:
    """面板改按渠道的额度 ({unit: tokens / yuan, limits: {qq, bili, pet, other, total}}, 0 = 不限; wm/ledger.py).
    只认本机页面: 别的网页不能改你的额度. 回整份账本."""
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="要 JSON") from None
    ledger = request.app[LEDGER]
    try:
        ledger.set_budget(body)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from None
    except OSError as exc:
        raise web.HTTPInternalServerError(text=f"额度存不下来: {exc}") from None
    return web.json_response(ledger.summary())


async def api_clock(request: web.Request) -> web.Response:
    """她按哪个时区过日子 (wm/tz.py): GET 设的什么、电脑自己的、她现在几点; POST {tz: "UTC+8" / ""} 改 (空 = 跟着电脑),
    马上生效、不用重启. 改只认本机页面."""
    clock = request.app[CLOCK]
    if request.method == "GET":
        return web.json_response(clock.state())
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="要 JSON") from None
    try:
        out = clock.set(str(body.get("tz") or "") if isinstance(body, dict) else "")
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from None
    except OSError as exc:
        raise web.HTTPInternalServerError(text=f"时区存不下来: {exc}") from None
    log.info("她的时区改成了 %s (现在 %s)", out["tz"] or f"跟着电脑 ({out['device']['name']})", out["now"])
    return web.json_response(out)


async def api_memory(request: web.Request) -> web.Response:
    """设置里的"记忆"页 (2026-11-01 用户要"记忆看得见"; 研究里评论最常抱怨"不知道它记了什么"):
    GET 她记得关于你的每一条 (哪来的、哪一类、哪天说的、上次想起、改过 / 忘掉)、等你确认的候选、她自己的理解、关系概况、
    最近几轮想起了什么 (wm/memory.py overview), 加上谁能看到: 每条标 remote (QQ / 微信和你私聊、工作台能不能看到: 和
    channels.store.owner_profile 同一个规则, 只有名字和喜好), visibility 是各处怎么用记忆 (sees 读什么, writes 在那儿说的记不记).
    POST {action: "forget", text}: 划掉一条程序写的 (你自己写的行在 Obsidian 里改); {action: "review", id, ok}: 确认 / 不要一条候选;
    {action: "affect_delete", id}: 删一条情绪事件; {action: "affect_clear"}: 情绪事件整个清空 (记忆阶段 4; 你点的删是真删).
    只认本机页面: 别的网页读不到你的记忆."""
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    mem = request.app[ENGINES].memory
    if mem is None:
        return web.json_response({"enabled": False, "reason": "没开长期记忆库 (memory.vault)"})
    loop = asyncio.get_running_loop()
    if request.method == "POST":
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        body = body if isinstance(body, dict) else {}
        if body.get("action") == "forget":
            done = await loop.run_in_executor(None, mem.forget, str(body.get("text") or ""))
            if not done:
                return web.json_response({"error": "没找到这一条程序记下的记忆 (你自己写的行请在 Obsidian 里改)"}, status=400)
            return web.json_response({"forgotten": done})
        if body.get("action") == "review":
            return web.json_response(await loop.run_in_executor(None, mem.review, str(body.get("id") or ""), body.get("ok") is True))
        if body.get("action") == "affect_delete":
            done = await loop.run_in_executor(None, mem.affect_delete, str(body.get("id") or ""))
            return web.json_response({"deleted": True} if done else {"error": "没有这条情绪事件"}, status=200 if done else 400)
        if body.get("action") == "affect_clear":
            return web.json_response({"cleared": await loop.run_in_executor(None, mem.affect_clear)})
        raise web.HTTPBadRequest(text="action 只能是 forget / review / affect_delete / affect_clear")
    from .channels.store import owner_profile
    data = await loop.run_in_executor(None, mem.overview)
    shared = set(owner_profile(mem.dir / "记忆" / "关于用户.md"))
    for f in data["facts"]:
        f["remote"] = f["status"] == "active" and f["text"] in shared
    skip = "、".join(x.rstrip("/") for x in mem.exclude)
    data["visibility"] = [
        {"where": "桌宠 (说话、打字)", "how": "每一轮按你说的话去找",
         "sees": "关于你的、对话摘要、拖进来的资料、你自己写的笔记; 不找日志、候选和她的日记" + (f"; 不看 {skip}" if skip else ""),
         "writes": '你直接说的"我叫…""我喜欢…"马上记下; 别的每晚整理成候选'},
        {"where": "QQ / 微信和你私聊、工作台", "how": "每次都带上",
         "sees": f"你的名字和喜好 ({len(shared)} 条)、她自己的理解 ({len(data['understanding'])} 条)、最近在别处和你聊过的; "
                 "住处、生日、工作、对话摘要都不给",
         "writes": 'QQ / 微信私聊里你打的"我叫…""我喜欢…"先进"等你确认的", 你点了才记; 工作台里说的不记'},
        {"where": "QQ 群、白名单里的别人", "how": "读不到", "sees": "你的记忆一条都看不到, 只看得到那个群 / 那个人自己的聊天",
         "writes": "不记: 群里、别人说的不会进你的记忆"},
    ]
    data["enabled"] = True
    return web.json_response(data)


async def api_memory_graph(request: web.Request) -> web.Response:
    """记忆核心的关系网 (记忆按脑区重设计阶段 1, 影子: 只给看, 不影响她检索、回话; wm/memory_core.py): 节点 (记忆、人物、
    那天的对话、做法和经验……, 每个标来源)、边、各类多少、最近改了什么. GET ?types=fact,event&q=字&limit=400&refresh=1.
    memory.core 是 off (默认) 时只回 enabled false, 什么都不建. 只认本机页面: 别的网页读不到你的记忆."""
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    if request.app[ENGINES].memory is None:
        return web.json_response({"enabled": False, "reason": "没开长期记忆库 (memory.vault)"})
    svc = request.app[MEMORY_CORE]
    if not svc.enabled:
        return web.json_response({"enabled": False, "reason": "记忆核心没开 (memory.core 是 off; 设成 shadow 才建, 只给看、不影响她)"})
    q = request.query
    types = [t for t in (q.get("types") or "").split(",") if t] or None
    try:
        limit = max(1, min(2000, int(q.get("limit") or 400)))
    except ValueError:
        limit = 400
    data = await asyncio.get_running_loop().run_in_executor(
        None, lambda: svc.view(types, q.get("q") or None, limit, q.get("refresh") == "1"))
    return web.json_response({"enabled": True, **data})


async def api_experience(request: web.Request) -> web.Response:
    """她的经历 (wm/cognition/store.py: 小窝认知的事件流、端东西的经验, 放在 ~/.worldmodel/experience):
    GET 存在哪、有几条; GET ?export=1 打包成 zip 下载; POST {action: "delete", confirm: true} 删掉 (服务端里记着的也清掉).
    只认本机页面: 别的网页不能拿走、删掉她的经历."""
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    from .cognition import store
    cfg, room = request.app[CFG], request.app[ENGINES].room
    folder = store.data_dir(cfg)
    if request.method == "POST":
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="要 JSON") from None
        if not isinstance(body, dict) or body.get("action") not in ("delete", "reset") or body.get("confirm") is not True:
            raise web.HTTPBadRequest(text='要 {"action": "delete" | "reset", "confirm": true}')
        if body["action"] == "reset":
            return web.json_response(await _reset_room_learning(request.app, body.get("scope")))
        try:
            gen = store.bump(folder, "删除她的经历", ["experience"])
            registry = getattr(request.app[ENGINES], 'room_registry', None)
            owner = registry if registry is not None else room
            gone = owner.forget(gen) if owner is not None else store.delete(folder)
            legacy = store.delete_legacy(cfg)
        except OSError as exc:
            raise web.HTTPInternalServerError(text=f"删不掉: {exc}") from None
        log.info("删掉了她的经历: %s (%s)", ", ".join(gone + legacy) or "本来就没有", folder)
        await _tell_room_gen(request.app, gen)
        return web.json_response({"deleted": gone, "legacy": legacy, **store.status(folder)})
    if request.query.get("export"):
        buf = io.BytesIO()
        store.export(folder, buf)
        return web.Response(body=buf.getvalue(), content_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="experience-{time.strftime("%Y%m%d-%H%M%S")}.zip"'})
    return web.json_response({**store.status(folder), "live": room.status() if room is not None else None})


RESET_SCOPES = ("experience", "growth", "growth-history", "understanding", "page")
RESET_DEFAULT = ("experience", "growth", "understanding", "page")  # 不给 scope 时: 动作成长只退回出厂参数, 训练历史要明说才清
# 重置小窝学习不碰的 (评审第 6 节: 范围要写清楚)
RESET_KEPT = ["聊天记忆和日记 (除了来源标着\"小窝\"的「我的理解」)", "没标来源的「我的理解」", "身体状态 (烫伤、摔伤)", "房间摆放",
              "刚体停下的姿态", "杯里的液体和水渍", "需求 (饿、渴、困)", "工具授权", "QQ 渠道", "动作成长\"空闲时自己练\"的开关",
              "仓库 logs/ 里的开发实验数据"]


async def _tell_room_gen(app, gen: int) -> None:
    """删完、重置完: 告诉开着的每个本人连接新的删除代次 (下行 room_gen), 开着的桌宠马上清本机存的, 不用等重连."""
    for s in list(app[SESSIONS]):
        with contextlib.suppress(Exception):
            await s.send({"type": "room_gen", "gen": gen})


async def _reset_room_learning(app, scope) -> dict:
    """完整重置小窝学习 (POST /api/experience {action: "reset", confirm: true, scope}): 按 scope 清
    experience (三个事件文件、运行中的认知、旧位置和 .migrated 副本, 代次加一) / growth (动作成长: 参数退回出厂, 训练历史留着) /
    growth-history (动作成长的训练历史也清: 毛病记录、换上过的每一版、练习记录…, 它那边的代次加一) /
    understanding (来源标着"小窝"的「我的理解」) / page (页面本机存的: 服务端清不了, 页面见到新代次自己清).
    不给 scope 是 RESET_DEFAULT (训练历史要明说才清; 评审第 6 节: 退回出厂参数不等于删除历史训练记录).
    回 {generation, scope, cleared: {每一项清了什么}, kept: [不碰的]}; 动作成长回包里的 kept 也并进来."""
    from .cognition import store
    cfg, engines = app[CFG], app[ENGINES]
    scope = [s for s in (scope if isinstance(scope, list) else RESET_DEFAULT) if s in RESET_SCOPES]
    folder, cleared = store.data_dir(cfg), {}
    gen = store.generation(folder)
    loop = asyncio.get_running_loop()
    if "experience" in scope or "page" in scope:  # 页面按代次清, 所以清页面也要加代次
        gen = store.bump(folder, "重置小窝学习", scope)
    if "experience" in scope:
        room = engines.room
        registry = getattr(engines, 'room_registry', None)
        owner = registry if registry is not None else room
        gone = await loop.run_in_executor(None, owner.forget, gen) if owner is not None else store.delete(folder)
        cleared["experience"] = {"files": gone, "legacy": store.delete_legacy(cfg)}
    elif getattr(engines, 'room_registry', None) is not None:
        engines.room_registry.set_generation(gen)
    elif engines.room is not None:
        engines.room.generation = gen
    kept = list(RESET_KEPT)
    if "growth" in scope or "growth-history" in scope:
        growth = app.get(motion_growth.KEY) if motion_growth.KEY is not None else None
        history = "growth-history" in scope
        if growth is None:
            cleared["growth"] = "动作成长没开"
        else:
            res = await loop.run_in_executor(None, lambda: growth.reset("重置小窝学习", history=history))
            cleared["growth"] = res
            dup = ("空闲时自己练", "logs/")  # 总清单里已经有这两样 (两边说法不一样, 按字符串去重会列两遍)
            kept += [k for k in (res.get("kept") or []) if isinstance(k, str) and k not in kept and not any(d in k for d in dup)]                 if isinstance(res, dict) else []
    if "understanding" in scope:
        mem = engines.memory
        cleared["understanding"] = (f"删了 {await loop.run_in_executor(None, mem.remove_understanding, '小窝')} 条来源标着\"小窝\"的「我的理解」"
                                    if mem is not None else "没开长期记忆, 没有可清的")
    if "page" in scope:
        cleared["page"] = "页面本机存的 (练习参数、她认识的东西): 页面连上时见到新代次自己清"
    log.info("重置小窝学习: %s (代次 %s)", ", ".join(cleared), gen)
    if "experience" in scope or "page" in scope:
        await _tell_room_gen(app, gen)
    return {"generation": gen, "scope": scope, "cleared": cleared, "kept": kept}


async def api_webview(request: web.Request) -> web.Response:
    """Read-only public-web fallback; third-party scripts never run on app origin."""
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    import html
    from .agent.tools import web_fetch
    url = (request.query.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")) or len(url) > 4000:
        raise web.HTTPBadRequest(text="url 要是 http/https 开头")
    try:
        offset = max(0, int(request.query.get('offset', 0)))
        text = await web_fetch(url, max_chars=20000, offset=offset)
    except Exception as exc:
        raise web.HTTPBadGateway(text="无法读取网页：" + str(exc)[:200])
    from urllib.parse import urlencode
    next_url = '/api/webview?' + urlencode({'url': url, 'offset': offset + 20000})
    previous_url = '/api/webview?' + urlencode({'url': url, 'offset': max(0, offset - 20000)})
    markup = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>网页文字阅读</title>'
    markup += '<style>body{font:14px/1.8 system-ui,sans-serif;padding:20px;color:#25252b;background:#fff}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}a{color:#6655aa;margin-right:16px}</style>'
    markup += '<p>网页文字阅读 · 不执行网页脚本</p><pre>' + html.escape(text) + '</pre>'
    markup += '<nav><a href="' + html.escape(previous_url, quote=True) + '">上一段</a><a href="' + html.escape(next_url, quote=True) + '">下一段</a></nav>'
    return web.Response(text=markup, content_type='text/html', headers={'Cache-Control': 'no-store',
        'Content-Security-Policy': "sandbox; default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
        'X-Content-Type-Options': 'nosniff'})


async def api_services(request: web.Request) -> web.Response:
    """本机服务 (wm/services.py 的看守; 面板"本地服务"页): GET 每样服务在不在; POST action = restart / stop (names 不给 = 全部)、
    up (看守没在跑就起一个). 只认本机页面: 别的网页不能替你停服务."""
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    if request.method == "POST":
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        action, names = body.get("action"), [str(n) for n in body.get("names") or []]
        if action == "up":
            await asyncio.to_thread(services.up)
        elif action in ("restart", "stop"):
            if not services.read_state()["running"]:
                raise web.HTTPConflict(text="看守没在跑: 桌宠带 --services 启动, 或者在终端跑 python -m wm.services up")
            services.request(action, names)
        else:
            raise web.HTTPBadRequest(text="action 只能是 up / restart / stop")
    return web.json_response(await services_view(request.app[CFG]))


async def services_view(cfg: dict) -> dict:
    """本地服务现在怎样 (/api/services 和诊断共用): 看守的状态; 看守没起过就按配置列出该有的服务, 当场查一下端口
    (开着的就是外面开的)."""
    st = services.read_state()
    if not st.get("services"):
        specs = services.specs(cfg)
        alive = await asyncio.gather(*(asyncio.to_thread(services.healthy, sp, 0.5) for sp in specs))
        st["services"] = [{"name": sp.name, "label": sp.label, "port": sp.port, "state": "external" if ok else "off"}
                          for sp, ok in zip(specs, alive)]
    for s in st.get("services", []):
        s["word"] = services.WORD.get(s["state"], s["state"])
    return st


async def api_diagnose(request: web.Request) -> web.Response:
    """诊断信息 (wm/diagnose.py; 设置的"诊断"页"复制诊断信息"): {report, text}. 去过隐私, 但里面有这台电脑的情况,
    只认本机页面."""
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    app = request.app
    r = await diagnose.report(app[CFG], app[LEDGER], app[COMPONENTS], app[ENGINES].channels,
                              lambda: services_view(app[CFG]))
    return web.json_response({"report": r, "text": diagnose.as_text(r, request.query.get("page", "")[:200])})


async def api_selfcheck(request: web.Request) -> web.Response:
    """环境自检 (wm/diagnose.py; 设置的"诊断"页): {checks: [{id, name, level, detail, fix}], at}. 只认本机页面."""
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    app = request.app
    checks = await diagnose.selfcheck(app[CFG], app[LEDGER], app[ENGINES].channels, lambda: services_view(app[CFG]))
    return web.json_response({"checks": checks, "at": time.time()})


async def api_components(request: web.Request) -> web.Response:
    """可选组件 (wm/components.py; 面板"版本与组件"页): GET 清单和装的进度; POST action = install / cancel / remove (id).
    只认本机页面: 别的网页不能替你下东西、删东西."""
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    comp = request.app[COMPONENTS]
    if request.method == "POST":
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        action, cid = body.get("action"), str(body.get("id") or "")
        if action == "install":
            err = comp.start(cid)
        elif action == "cancel":
            err = None if comp.cancel() else "没有在装的"
        elif action == "remove":
            err = await asyncio.to_thread(comp.remove, cid)
        else:
            err = "action 只能是 install / cancel / remove"
        if err:
            raise web.HTTPConflict(text=err)
    return web.json_response(comp.status())


async def api_weixin(request: web.Request) -> web.Response:
    """微信 (ClawBot, wm/channels/weixin.py), 面板"IM 机器人"页用: GET 状态 (扫码时带二维码 SVG); POST action=login 开始扫码、
    cancel 不扫了、code 交验证码 ({code})、logout 解绑. 和 /ws 一样只认本机页面 (查 Host 和 Origin), 别的网页不能替你扫码、解绑."""
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    wx = request.app[ENGINES].channels.weixin
    if request.method == "POST":
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        action = body.get("action") if isinstance(body, dict) else None
        if action == "login":
            await wx.begin_login()
        elif action == "cancel":
            await wx.cancel_login()
        elif action == "code":
            if not wx.give_code(str(body.get("code") or "")):
                return web.json_response({**wx.status(), "error": "现在不用验证码, 或者没填"}, status=400)
        elif action == "logout":
            await wx.logout()
            log.info("微信解绑了")
        else:
            raise web.HTTPBadRequest(text="action 只能是 login / cancel / code / logout")
    return web.json_response(wx.status())


PET_WEB = ROOT / "desktop" / "web"
# 工作台的页面在桌宠那边 (desktop/web/workbench.*): 桌宠的工作台窗口经宿主读 (/pet/*), 浏览器打开 /agent 时由服务端给
# 同一份 (只给这几个文件, 别的桌宠页面要宿主才跑得起来)
_PET_FILES = {"workbench.html", "workbench.css", "workbench.js", "workbench-pet.js", "ui-kit.js", "ui-kit.css", "ui-preferences.js", "ui-preferences.css",
              "workbench-studio.js", "workbench-studio.css",
              "workbench-navigation.js", "workbench-navigation.css",
              "workbench-canvas.js", "workbench-canvas.css", "workbench-platform.js", "workbench-platform.css", "panel-life.js", "panel-memory.js",
              "workbench-resident.js", "workbench-resident.css",
              "workbench-terminal.js", "workbench-terminal.css", "workbench-browser.js", "workbench-browser.css",
              "workbench-files.js", "workbench-files.css", "workbench-reading.js", "workbench-reading.css", "ui-material.css", "pet-preferences.js", "workbench-rail.css",
              "workbench-panel.js", "workbench-diff.js", "workbench-shell.css",  # 2026-11-01 照 Codex 改版: 右侧面板、行级 diff、新布局
              "vendor/xterm/xterm.js", "vendor/xterm/xterm.css", "vendor/xterm/addon-fit.js", "vendor/xterm/addon-search.js",
              "avatar-lean.js", "springs.js", "perf-flags.js", "brand/icon.png", "brand/icon-64.png", "live2d-avatar.js", "tz.js",
              "brand/avatar.png", "brand/avatar-128.png"}  # 2026-11-02 绫音的个人头像 (对话里、左栏、新聊天页)
# (springs.js 后来引了 perf-flags.js: 没给的话浏览器里整个 workbench-pet.js 载不进来, 桌前的她 3D、2D 都出不来)
# 桌前的她用 2D 时 (2026-10-27) 要的 Live2D SDK 和模型: 和宿主一样从不进版本库的 desktop/third_party/ 给, 只给这两个目录里的
_PET_THIRD = ROOT / "desktop" / "third_party"
_PET_THIRD_DIRS = ("live2d/", "live2d-models/")


async def agent_page(request: web.Request) -> web.FileResponse:
    return web.FileResponse(PET_WEB / "workbench.html")


async def pet_file(request: web.Request) -> web.FileResponse:
    name = request.match_info["name"]
    if name.startswith(_PET_THIRD_DIRS):
        path = (_PET_THIRD / name).resolve()
        if not any(path.is_relative_to((_PET_THIRD / d).resolve()) for d in _PET_THIRD_DIRS) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)
    if name not in _PET_FILES:
        raise web.HTTPNotFound()
    # 页面改了重新打开就是新的 (和宿主给 /pet/* 一样不缓存): 不然浏览器会拿缓存的旧模块配新页面, 一个 import 对不上整页就白了
    return web.FileResponse(PET_WEB / name, headers={"Cache-Control": "no-store"})


async def api_agent(request: web.Request) -> web.Response:
    """工作台 (wm/agent): GET 工作区、会话、能选的模型 (?id= 带上就是那个会话的全部事件); POST action = new / archive /
    settings / workspace. 只认本机页面 (查 Host 和 Origin): 别的网页不能替你开会话、读你的会话."""
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    hub = request.app[AGENT]
    if request.method == "GET":
        sid = request.query.get("id")
        if sid:
            s = hub.get(sid)
            if s is None:
                raise web.HTTPNotFound(text="没有这个会话")
            return web.json_response(s.view())
        return web.json_response(hub.state())
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    body = body if isinstance(body, dict) else {}
    action = body.get("action")
    try:
        if action == "new":
            s = hub.new_session(body.get("workspace"), str(body.get("mode") or ""), str(body.get("model") or ""))
            return web.json_response(s.view())
        if action == "archive":
            sid = str(body.get('id') or '')
            hub.archive(sid)
            return web.json_response({'ok': True, 'session': hub.get(sid).view()})
        if action == 'restore':
            return web.json_response({'ok': True, 'session': hub.restore_session(str(body.get('id') or '')).view()})
        if action == 'delete':
            return web.json_response({'ok': hub.delete_session(str(body.get('id') or ''), confirm=body.get('confirm'))})
        if action == 'rename':
            return web.json_response({'ok': True, 'session': hub.rename_session(str(body.get('id') or ''), body.get('title')).view()})
        if action in {'pin', 'unread'}:
            key = 'pinned' if action == 'pin' else 'unread'
            return web.json_response({'ok': True, 'session': hub.flag_session(str(body.get('id') or ''), key, body.get(key)).view()})
        if action == 'move_workspace':
            return web.json_response({'ok': True, 'session': hub.move_session(str(body.get('id') or ''), body.get('workspace')).view()})
        if action == 'fork':
            s = await hub.fork_session(str(body.get('id') or ''), title=body.get('title'), workspace=body.get('workspace'), message_id=body.get('message_id'))
            return web.json_response(s.view())
        if action == "settings":
            s = hub.settings(str(body.get("id") or ""),
                             **{k: body.get(k) for k in ("mode", "preset", "model", "title", "think", "effort", "web", "budget_cny", "time_budget_s", "max_steps")})
            return web.json_response(s.view())
        if action == "allow":  # "以后都允许"的名单: 删一条 (remove)
            return web.json_response({"allow": hub.allow_remove(str(body.get("remove") or ""))})
        if action == "workspace":
            hub.add_workspace(body.get('path'), body.get('name'))
            return web.json_response(hub.state())
    except (ValueError, OSError, TypeError, KeyError) as exc:
        return web.json_response({"error": str(exc)[:300]}, status=400)
    raise web.HTTPBadRequest(text="不支持的工作台操作")


async def api_agent_studio(request: web.Request) -> web.Response:
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    from .agent.studio_http import studio
    return await studio(request, request.app[AGENT])


async def api_agent_runtime(request: web.Request) -> web.Response:
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    from .agent.runtime_http import runtime
    return await runtime(request, request.app[AGENT])


async def api_agent_resident(request: web.Request) -> web.Response:
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    from .agent.resident_http import resident_handler
    return await resident_handler(request, request.app[AGENT])


async def api_agent_platform(request: web.Request) -> web.Response:
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    from .platform_http import platform
    return await platform(request, request.app[AGENT])


async def api_agent_workspace(request: web.Request) -> web.Response:
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    from .agent.workspace_http import workspace
    return await workspace(request, request.app[AGENT])


async def api_agent_life(request: web.Request) -> web.Response:
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    from .agent.life_http import life_handler
    return await life_handler(request, request.app[ENGINES].channels)


async def api_agent_terminal(request: web.Request) -> web.Response:
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    from .agent.terminal_http import terminal
    return await terminal(request, request.app[AGENT])


async def api_agent_permissions(request: web.Request) -> web.Response:
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    from .agent.permissions_http import permissions
    return await permissions(request, request.app[AGENT], learn=request.path.endswith('/learn'))


async def api_agent_context(request: web.Request) -> web.Response:
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    from .agent.studio_http import context_upload
    return await context_upload(request, request.app[AGENT])


async def api_agent_context_image(request: web.Request) -> web.Response:
    """附件里的图片原样给页面看 (对话里的缩略图)."""
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    from .agent.studio_http import context_image
    return await context_image(request, request.app[AGENT])


async def agent_ws(request: web.Request) -> web.WebSocketResponse:
    """一个会话的实时事件 (她想的字、用了什么工具、要你点头……). 上行: {type: send, text} / {type: stop} / {type: answer, id, how}
    / {type: steer, text} (她做事时补充一句, 这一步做完就交给她) / {type: enqueue, text} (这一轮做完再当新的一件事)."""
    if (why := ws_refusal(request)) is not None:
        raise web.HTTPForbidden(text=why)
    hub = request.app[AGENT]
    sid = request.query.get("id", "")
    s = hub.get(sid)
    if s is None:
        raise web.HTTPNotFound(text="没有这个会话")
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=2 * 1024 * 1024)
    await ws.prepare(request)

    async def send(ev: dict) -> None:
        await ws.send_json(ev)

    hub.subscribe(sid, send)
    try:
        await ws.send_json({"type": "hello", "session": s.view()})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            kind = data.get("type") if isinstance(data, dict) else None
            if kind == "send":
                why = await hub.send(sid, str(data.get("text") or ""), data.get('context_ids'))
                if why:
                    await ws.send_json({"type": "refused", "text": why})
                else:
                    await ws.send_json({'type': 'accepted', 'session': sid})
            elif kind == "steer":  # 她正忙的时候补充一句: 不停下这一轮, 手头这一步做完就交给她 (刚好做完了就当排队)
                try:
                    got = await hub.steer(sid, str(data.get("text") or ""))
                    await ws.send_json({'type': 'steer_ack', 'mode': got['mode'], 'item': got['item']})
                except ValueError as exc:
                    await ws.send_json({"type": "refused", "text": str(exc), "steer": True})
            elif kind == "stop":
                await hub.stop(sid)
            elif kind == "enqueue":  # 她正忙的时候把这句话排在后面 (这一轮做完再发)
                try:
                    item = await hub.enqueue(sid, str(data.get("text") or ""), data.get('context_ids'))
                    await ws.send_json({'type': 'queued_ack', 'item': item})
                except ValueError as exc:
                    await ws.send_json({"type": "refused", "text": str(exc)})
            elif kind == "answer":
                hub.answer(sid, str(data.get("id") or ""), str(data.get("how") or ""), via="page")  # 页面上你点的
    finally:
        hub.unsubscribe(sid, send)
    return ws


async def _startup(app: web.Application) -> None:
    beat.watch_loop()  # 事件循环每秒记一次: 看守据此分清"忙、慢"和"卡死" (wm/beat.py)
    app[RESOURCES].start()
    app[LEDGER].start()
    eng = app[ENGINES]
    await eng.load()
    dcfg = (app[CFG].get("memory") or {}).get("digest") or {}
    if eng.memory is not None and eng.llm.name == "llamacpp" and dcfg.get("enabled", True):
        eng.digest = Digest(eng.memory, eng.llm, dcfg, lambda r: app[TRACELOG].write(r, kind="digest"))
        eng.digest.start()
    beat.phase("连 QQ 渠道")
    await eng.channels.start(app[TRACELOG])  # channels.qq.enabled 时按配置连协议端; 桌宠的 channel_config 会再覆盖
    if app[AGENT].conf.get('resident_enabled', True):
        await app[AGENT].resident.start()
    app[AGENT].job_watch.start()  # 她起的后台作业做完了, 自己回到发起的会话 (wm/agent/jobwatch.py)
    app[AGENT].downloads.start()  # 下载队列: 有下载在排、在下时盯着下载服务, 下完交回发起的会话 (wm/agent/downloads.py)
    HB.start(app[CFG],  # 心跳: 11-26 分钟一拍, 记一行"平时", 看工作台的定时提醒和待办 (wm/agent/heartbeat.py)
             presence=lambda: getattr(eng.channels, "owner_seen", 0.0),  # 你最后一次真找她的时刻
             doing=lambda: getattr(eng.channels, "life_recent", ""))
    beat.phase(beat.READY)
    status = eng.status()
    log.info("引擎: LLM=%s(%s) ASR=%s TTS=%s RVC=%s", status["llm"]["engine"],
             "在线" if status["llm"]["ok"] else "未连接", status["asr"]["engine"],
             status["tts"]["engine"], status["rvc"]["engine"])


async def _cleanup(app: web.Application) -> None:
    await HB.stop()  # 心跳先停: 它只记账, 停得越早越安全
    await app[AGENT].resident.close()
    await app[AGENT].job_watch.close()
    await app[AGENT].downloads.close()  # 只停这边的盯梢; 下载服务是另一个进程, 接着下
    if hasattr(app[AGENT], 'terminals'):
        await app[AGENT].terminals.close()
    await app[RESOURCES].stop()
    await app[LEDGER].stop()
    if L.current() is app[LEDGER]:
        L.use(None)
    await app[AGENT].teams.close()
    with contextlib.suppress(Exception):  # 工作台留着的 MCP 长连接 (wm/agent/extensions.py) 关掉
        await app[AGENT].extensions.close()
    if hasattr(app[AGENT], 'workspace_jobs'):
        await app[AGENT].workspace_jobs.close()
    for s in list(app[AGENT].sessions.values()):  # 工作台在做的事停掉 (跑着的命令连子进程一起结束)
        if s.task is not None and not s.task.done():
            s.stop_event.set()
            s.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await s.task
    if app[ENGINES].digest is not None:
        await app[ENGINES].digest.stop()
    await app[ENGINES].channels.stop()
    await app[ENGINES].close()


def create_app(cfg: dict) -> web.Application:
    app = web.Application(middlewares=[no_cache])
    app[CFG] = cfg
    # 时区最先换 (wm/tz.py): 后面起的东西 (按天分的账本、渠道、她的一天) 都按她的钟
    app[CLOCK] = TZ.Clock(resolve_path(cfg["trace"]["log_dir"]) / "clock.json", (cfg.get("clock") or {}).get("tz", ""))
    app[CLOCK].start()
    # 账本先建好: 之后建的云端模型都往它里面记 (wm/ledger.py)
    app[LEDGER] = L.Ledger(resolve_path(cfg["trace"]["log_dir"]) / "billing", cfg.get("billing") or {})
    L.use(app[LEDGER])
    app[ENGINES] = Engines(cfg)
    app[MEMORY_CORE] = MC.CoreService(cfg)  # memory.core 是 off 时什么都不建
    alerts: set[asyncio.Task] = set()

    def budget_alert(text: str, row: dict) -> None:
        """某个渠道今天的额度到了 80% / 100% (账本每样每天说一次): 发到连着的桌宠的对话栏."""
        with contextlib.suppress(RuntimeError):  # 没有跑着的事件循环 (单测直接调账本) 就算了
            task = asyncio.get_running_loop().create_task(app[ENGINES].channels.notify(
                {"type": "channel_notice", "channel": "billing", "kind": "budget", "level": row["level"], "text": text}))
            alerts.add(task)
            task.add_done_callback(alerts.discard)

    app[LEDGER].on_alert = budget_alert
    app[AGENT] = AgentHub(cfg, app[ENGINES])  # 工作台 (wm/agent): 页面 /agent
    app[ENGINES].channels.attach_agent(app[AGENT])  # QQ / 微信上说"用工作台"时, 渠道能开工作台会话派活
    app[AGENT].life = app[ENGINES].channels.bind_life(app[AGENT].home, app[AGENT].blacklist)
    app[STATS] = LatencyStats()
    app[TRACELOG] = TraceLog(resolve_path(cfg["trace"]["log_dir"]))
    app[RESOURCES] = ResourceMonitor(cfg.get("resources", {}).get("interval_s", 2.0))
    app[SESSIONS] = set()
    app[BOOT] = boot_info()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/guest", guest_handler)
    app.router.add_get("/api/status", api_status)
    app.router.add_route("*", "/api/agent/resident", api_agent_resident)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_post("/api/supervisor", api_supervisor)
    app.router.add_get("/api/resources", api_resources)
    app.router.add_get("/api/avatars", api_avatars)
    app.router.add_get("/api/billing", api_billing)
    app.router.add_route("*", "/api/services", api_services)
    app.router.add_get("/api/webview", api_webview)
    app.router.add_route("*", "/api/experience", api_experience)  # 她的经历: 存在哪、导出、删除
    app.router.add_route("*", "/api/memory", api_memory)  # 记忆看得见: 她记得什么、哪来的、谁能看到; 忘掉一条、确认候选
    app.router.add_get("/api/memory/graph", api_memory_graph)  # 记忆核心的关系网 (阶段 1 影子, memory.core=shadow 时才建)
    app[COMPONENTS] = Components()
    app.router.add_route("*", "/api/components", api_components)
    app.router.add_post("/api/billing/refresh", api_billing_refresh)
    app.router.add_post("/api/billing/budget", api_billing_budget)
    app.router.add_get("/api/diagnose", api_diagnose)
    app.router.add_get("/api/selfcheck", api_selfcheck)
    app.router.add_route("*", "/api/clock", api_clock)
    app.router.add_get("/api/weixin", api_weixin)
    app.router.add_post("/api/weixin", api_weixin)
    app.router.add_get("/agent", agent_page)
    app.router.add_get("/pet/{name:.+}", pet_file)
    app.router.add_get("/agent/ws", agent_ws)
    app.router.add_get("/api/agent", api_agent)
    app.router.add_post("/api/agent", api_agent)
    app.router.add_get('/api/agent/studio', api_agent_studio)
    app.router.add_post('/api/agent/studio', api_agent_studio)
    for route, handler in (('runtime', api_agent_runtime), ('platform', api_agent_platform),
                           ('workspace', api_agent_workspace), ('life', api_agent_life), ('terminal', api_agent_terminal)):
        app.router.add_get('/api/agent/' + route, handler)
        app.router.add_post('/api/agent/' + route, handler)
    app.router.add_post('/api/agent/context', api_agent_context)
    app.router.add_post('/api/agent/context/upload', api_agent_context)
    app.router.add_get('/api/agent/context/image', api_agent_context_image)
    app.router.add_get('/api/agent/permissions', api_agent_permissions)
    app.router.add_post('/api/agent/permissions', api_agent_permissions)
    app.router.add_post('/api/agent/permissions/learn', api_agent_permissions)
    app.router.add_get("/avatars/{name}", avatar_file)
    app.router.add_get("/motiondb/{name}", motiondb_file)
    # 动作成长: 小屋的失败片段、她练出来的参数版本 (docs/roadmap/motion-growth.md); 她在说话、在做电脑任务单时不练
    motion_growth.register(app, cfg, ws_refusal, busy=lambda: motion_growth.session_busy(app[SESSIONS], app[ENGINES].channels))
    # 读主人的邮箱 (只读) 整理汇报: /api/mail, 只在看守起的正式服务端上读 (wm/channels/mailbox*.py)
    mailbox_http.register(app, cfg, ws_refusal, app[ENGINES], app[AGENT])
    # 账号保管箱: /vault 页面和 /api/vault (只在这一页由主人填密码、扫码登录; 密码加密存 ~/.worldmodel/vault, 接口只进不出),
    # 工作台的守门名单把这个目录也挡上 (wm/channels/vault*.py)
    vault_http.register(app, cfg, ws_refusal, app[ENGINES], app[AGENT])
    # 资料本 (学习、研究资料, 跨会话; 只给数据, 界面归工作台改版会话): /api/agent/notebooks (wm/agent/notebook_http.py)
    notebook_http.register(app, ws_refusal, app[AGENT])
    # 下载队列 (给你用的本机接口: 看队列、暂停、继续、取消、清理 (不含批准: 批准只走卡片); 她的工具不走这里): /api/agent/downloads (wm/agent/downloads_http.py)
    downloads_http.register(app, ws_refusal, app[AGENT])
    # 本机模型的检测 (哪家、上下文多长、看不看得了图; 只问本机 / 局域网地址): /api/llm/local (wm/llm_http.py)
    llm_http.register(app, ws_refusal)
    app.router.add_get("/hpe/{name}", hpe_file)
    app.router.add_static("/static/", WEB_DIR)
    # 身体运动层: 装了 HAST-SEC (非商业研究许可) 就由它驱动, 否则前端退回程序动作.
    try:
        motion = "hast" if hast.register(app, cfg) else "procedural"
    except (OSError, ValueError, KeyError) as exc:
        log.warning("HAST-SEC 运动层不可用, 退回程序动作: %s", exc)
        motion = "procedural"
    app[ENGINES].motion = motion
    # 开放空间行动层: 有中性风格库才启用 (动捕数据见 tools/setup_models.py mocap + tools/build_motion_db.py)
    styles = locomotion_styles(app)
    app[ENGINES].locomotion = styles if "neutral" in styles else []
    d = _motiondb_dir(app)
    app[ENGINES].interactions = bool(styles) and (d / "actions.json").is_file() and (d / "actions.bin").is_file()
    app[ENGINES].idle = bool(styles) and (d / "idle.json").is_file() and (d / "idle.bin").is_file()  # StayStill 待机库
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app

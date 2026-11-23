"""OneBot v11 客户端 (正向 WebSocket): 连协议端 (SnowLuma 的 ws-default, 默认 ws://127.0.0.1:3001/, role Universal:
动作和事件走同一条连接, 动作带 echo 对回执).

- 只连本机地址 (check_endpoint): 协议端握着 QQ 号, 不经网络转;
- 只实现白名单里的动作 (ACTIONS): 读登录信息、状态、群和群成员资料、取一条消息、取一条语音、发私聊和群消息、发 QQ 空间说说.
  踢人、禁言、撤回、加好友、处理申请、退群、传文件、改资料这些代码里没有: call 直接抛 ActionNotAllowed, 不往外发;
- 断线退避重连 (1, 2, 5, 10, 30 s); 断开时等回执的动作全部作废、定时任务停掉 (SnowLuma 2.0.0 修过的那类泄漏);
- 存活检查: 心跳 meta 事件里的 status.online, 加上定时 get_status. 账号静默离线时连接还"活着" (NapCat #2071), 只能靠这个发现;
  get_status 等不到回执就当连接死了, 断开重连;
- 鉴权: Authorization: Bearer <token>. token 每次连接时现取 (token_fn, 从 SnowLuma 自己的配置读), 不在这里存;
  握手被拒 (401/403) 就是 token 不对, 隔 30 s 再试.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger("wm.channels.onebot")

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
ACTIONS = frozenset({
    "get_login_info", "get_status", "get_group_info", "get_group_member_info", "get_msg",
    "get_record",  # 取一条语音 (协议端转成 wav; 只读不删)
    "fetch_ptt_text",  # 协议端自带的语音转文字 (本机 ASR 没开时也能听; 只读)
    "get_image",  # 按图片消息段里的 file / file_id 取当前下载地址 (地址会过期; 只读)
    "send_private_msg", "send_group_msg",
    "send_qzone_msg",  # 发 QQ 空间说说 (SnowLuma 的扩展动作; 只发不删、不拉黑)
    # 回自己说说下面的评论: 读自己的说说和评论、发评论 (管理器只在她自己的空间里回, 不带 target_uin)
    "get_qzone_msg_list", "get_qzone_msg_detail", "comment_qzone",
    # 她 QQ"我的收藏"里的表情: 读列表、发出去 (只发收藏里有的; 不加、不删、不改收藏)
    "fetch_custom_face_detail", "send_custom_face",
    "get_forward_msg",  # 读别人发的合并转发 (聊天记录) 里面的消息 (只读)
    # 文件段、群文件通知只带 file_id 时, 按 file_id 要下载直链 (SnowLuma 2.2; 只读, 不删不转存)
    "get_group_file_url", "get_private_file_url",
})


class OneBotError(RuntimeError):
    """动作失败: 没连上、超时、协议端回了失败."""


class ActionNotAllowed(OneBotError):
    """不在白名单里的动作 (根本没发出去)."""


def check_endpoint(url: str) -> str:
    """协议端地址: 只许 ws(s)://127.0.0.1 / localhost, 不许带用户名密码. 不合格抛 ValueError."""
    url = str(url or "").strip()
    u = urlsplit(url)
    if u.scheme not in ("ws", "wss") or not u.hostname:
        raise ValueError("协议端地址要写成 ws://127.0.0.1:3001/ 这样")
    if u.hostname.lower() not in LOOPBACK:
        raise ValueError(f"协议端只许连本机 (127.0.0.1 / localhost), 不能连 {u.hostname[:64]}")
    if u.username or u.password:
        raise ValueError("协议端地址里不要写用户名密码, token 单独配")
    return url


class OneBotClient:
    BACKOFF = (1, 2, 5, 10, 30)

    def __init__(self, endpoint: str, token_fn: Callable[[], str],
                 on_event: Callable[[dict], Awaitable[None]], on_state: Callable[[str, dict], Awaitable[None]],
                 heartbeat_s: float = 30, status_s: float = 60, timeout_s: float = 10, backoff=None,
                 before_connect: Callable[[], Awaitable[None]] | None = None):
        """on_event(事件): 消息、通知等 (心跳和生命周期这里消化掉). on_state(kind, info): 连接的变化,
        kind 是 connecting / open / closed / auth_failed / token_error / account ({"online": bool})."""
        self.endpoint = check_endpoint(endpoint)
        self._token_fn = token_fn
        self.on_event = on_event
        self.on_state = on_state
        self.heartbeat_s = float(heartbeat_s)
        self.status_s = float(status_s)
        self.timeout_s = float(timeout_s)
        self.backoff = tuple(backoff or self.BACKOFF)
        self.before_connect = before_connect
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._http: aiohttp.ClientSession | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = 0
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.last_frame = 0.0

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self._fail_pending("渠道停了")
        if self._http is not None and not self._http.closed:
            await self._http.close()

    async def call(self, action: str, **params) -> dict:
        """调一个白名单里的动作, 返回 data. 失败抛 OneBotError (不在白名单: ActionNotAllowed, 不往外发)."""
        if action not in ACTIONS:
            raise ActionNotAllowed(f"不允许的动作: {str(action)[:64]}")
        ws = self._ws
        if ws is None or ws.closed:
            raise OneBotError("没连上协议端")
        self._seq += 1
        echo = f"wm-{self._seq}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await ws.send_str(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            resp = await asyncio.wait_for(fut, self.timeout_s)
        except asyncio.TimeoutError:
            raise OneBotError(f"{action} 等了 {self.timeout_s:g} s 没有回执") from None
        except OneBotError:  # 连接断了, _fail_pending 作废的
            raise
        except (ConnectionResetError, RuntimeError, aiohttp.ClientError) as exc:
            raise OneBotError(f"{action} 没发出去: {type(exc).__name__}") from None
        finally:
            self._pending.pop(echo, None)
        if resp.get("status") == "failed" or resp.get("retcode") not in (0, None):
            why = resp.get("wording") or resp.get("message") or resp.get("msg") or ""
            raise OneBotError(f"{action} 失败 (retcode {resp.get('retcode')}){': ' + str(why)[:120] if why else ''}")
        data = resp.get("data")
        return data if isinstance(data, dict) else {"value": data}

    # -- 内部 ----------------------------------------------------------------
    def _fail_pending(self, why: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(OneBotError(why))
        self._pending.clear()

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=5))
        return self._http

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            if self.before_connect is not None:
                try:
                    await self.before_connect()
                except Exception as exc:
                    log.debug("协议端启动检查失败: %s", type(exc).__name__)
            await self.on_state("connecting", {})
            delay = None
            try:
                token = self._token_fn()
            except Exception as exc:  # SnowLuma 的配置文件找不到、没有 ws-default
                await self.on_state("token_error", {"error": str(exc)[:200]})
                delay = 30
            else:
                try:
                    http = await self._session()
                    headers = {"Authorization": f"Bearer {token}"} if token else {}
                    async with http.ws_connect(self.endpoint, headers=headers, heartbeat=self.heartbeat_s,
                                               max_msg_size=16 * 1024 * 1024) as ws:
                        self._ws = ws
                        attempt = 0
                        await self._serve(ws)
                except aiohttp.WSServerHandshakeError as exc:
                    if exc.status in (401, 403):
                        await self.on_state("auth_failed", {"error": f"协议端拒绝了 token (HTTP {exc.status})"})
                        delay = 30
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                    log.debug("连不上协议端: %s", type(exc).__name__)
                finally:
                    self._ws = None
                    self._fail_pending("和协议端的连接断了")
                if not self._stopping:
                    await self.on_state("closed", {})
            if self._stopping:
                break
            if delay is None:
                delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                attempt += 1
            await asyncio.sleep(delay)

    async def _serve(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        reader = asyncio.create_task(self._read(ws))
        watch = asyncio.create_task(self._watch(ws))
        try:
            await self.on_state("open", {})
            await reader
        finally:
            for t in (reader, watch):
                t.cancel()
            for t in (reader, watch):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    break
                continue
            self.last_frame = time.monotonic()
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            echo = data.get("echo")
            if echo is not None and "post_type" not in data:
                fut = self._pending.get(str(echo))
                if fut is not None and not fut.done():
                    fut.set_result(data)
                continue
            if data.get("post_type") == "meta_event":
                status = data.get("status")
                if data.get("meta_event_type") == "heartbeat" and isinstance(status, dict) and "online" in status:
                    await self.on_state("account", {"online": status.get("online") is not False})
                continue
            if "post_type" in data:
                try:
                    await self.on_event(data)
                except Exception:  # 一条事件处理坏了不能把连接弄断
                    log.exception("处理 OneBot 事件出错")

    async def _watch(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """定时 get_status: 账号离线照实报; 等不到回执就断开, 让 _run 重连."""
        while not ws.closed:
            await asyncio.sleep(self.status_s)
            try:
                data = await self.call("get_status")
            except OneBotError:
                log.warning("协议端 %g s 没回 get_status, 断开重连", self.timeout_s)
                await ws.close()
                return
            if "online" in data:
                await self.on_state("account", {"online": data.get("online") is not False})

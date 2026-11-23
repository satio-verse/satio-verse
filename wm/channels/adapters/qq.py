"""QQ (OneBot v11, 经本机的协议端) 适配器. 清单 manifests/qq-onebot.json.

S3: 协议层的翻译 normalize() -- OneBot 消息事件 -> Inbound. 纯函数: 没有计数器、不看白名单和治理、不碰别的渠道; 编号、去重、
排版和策略检查 (黑名单、白名单、治理) 都在核心 (manager._qq_message / _compose_qq), 所以被挡掉的消息不占编号.
S4: QQAdapter 拥有 OneBotClient, 报状态 (含 get_login_info 核对账号)、提示主人 (好友申请、群邀请、被禁言、被踢)、发消息 / 表情 / 语音条.
S4b: fetch -- 七个只读动作.

SnowLuma 的挂接 (before_connect 钩子)、令牌怎么来都留在核心, 以回调的形式传进来; 本模块不碰挂接、不碰 SnowLuma 的配置.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from .. import adapter as A
from ..media import host_ok
from ..onebot import OneBotClient, OneBotError
from ..outbound import clean_in

ID = "qq-onebot"


@dataclass(frozen=True)
class Settings:
    """核心交给 QQ 适配器的平台设置 (纯数据)."""
    self_id: int
    media_hosts: tuple = ()
    video_max_mb: float = 30


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def normalize(ev: dict, st: Settings) -> A.Inbound | None:
    """OneBot 消息事件 -> Inbound. 自己的回声、临时会话、不认识的会话类型返回 None. 段只翻译不编号 (见模块说明)."""
    uid = int(ev.get("user_id") or 0)
    if uid == st.self_id:
        return None
    if ev.get("message_type") == "group":
        conv = A.ConvRef("group", int(ev.get("group_id") or 0))
    elif ev.get("message_type") == "private" and ev.get("sub_type", "friend") == "friend":
        conv = A.ConvRef("private", uid)
    else:
        return None
    me, hosts = str(st.self_id), tuple(st.media_hosts)
    out, reply_to, mention = [], None, False
    segs = ev.get("message")
    if isinstance(segs, str):  # 协议端设成了 string 格式: 去掉 CQ 码只留文字
        segs = [{"type": "text", "data": {"text": re.sub(r"\[CQ:[^\]]*\]", "", segs)}}]
    for seg in segs if isinstance(segs, list) else []:
        t, d = (seg.get("type"), seg.get("data") or {}) if isinstance(seg, dict) else (None, {})
        if t == "text":
            out.append(A.Seg("text", {"text": clean_in(d.get("text"), A.IN_TEXT_CHARS)}))
        elif t == "at":
            qq = str(d.get("qq") or "")
            mention = mention or qq == me
            out.append(A.Seg("at", {"target": qq, "me": qq == me, "all": qq == "all"}))
        elif t == "reply":
            reply_to = str(d.get("id") or "")
            out.append(A.Seg("reply", {"id": reply_to}))
        elif t == "face":
            out.append(A.Seg("face", {"id": d.get("id")}))
        elif t in ("image", "mface"):
            # 商城表情 SnowLuma 转成带 emoji_id 的 image 段, summary 是表情自带的名字 ("[开心]")
            market = t == "mface" or bool(d.get("emoji_id"))
            sticker = market or str(d.get("sub_type")) == "1" or "表情" in str(d.get("summary") or "")
            what = clean_in(str(d.get("summary") or "").strip("[]［］ "), 12) if market else ""
            # 消息段里的 url 可能为空、过期或只在 file 里有地址: 先留下 file 标识, 拿不到时用 get_image 再换一个地址
            url = str(d.get("url") or "").strip()
            file_id = str(d.get("file") or d.get("file_id") or "").strip()
            if "://" in file_id and not host_ok(file_id, hosts):
                file_id = ""  # 别把不认得的地址当文件标识交给协议端
            if not host_ok(url, hosts):
                url = file_id if host_ok(file_id, hosts) else ""
            out.append(A.Seg("image", {"t": t, "sticker": sticker, "what": what, "url": url, "file_id": file_id}))
        elif t == "json":
            out.append(A.Seg("card", {"raw": d.get("data")}))
        elif t == "file":
            fn, size = clean_in(d.get("file") or d.get("name") or "", 40), _int(d.get("file_size"))
            url = str(d.get("url") or "")
            url_ok = host_ok(url, hosts)
            fid_img = str(d.get("file") or d.get("file_id") or "").strip()  # 当图片发来的文件: file_id 常是直链
            out.append(A.Seg("file", {
                "name": fn, "size": size, "url": url, "url_ok": url_ok,
                "video_ok": (size <= 0 or size <= st.video_max_mb * 1024 * 1024) and url_ok,
                "fid_img": fid_img, "fid_img_ok": host_ok(fid_img, hosts),
                "fid": str(d.get("file_id") or d.get("id") or "").strip(), "file_hash": str(d.get("file_hash") or "")}))
        elif t == "record":
            # 带上 message_id: 协议端能用 QQ 自带的转写 (fetch_ptt_text), 本机 ASR 不行时还有这条退路
            out.append(A.Seg("record", {"file": str(d.get("file") or ""), "message_id": ev.get("message_id")}))
        elif t == "video":
            url = str(d.get("url") or d.get("file") or "")
            out.append(A.Seg("video", {"url": url, "url_ok": host_ok(url, hosts)}))
        elif t == "forward" and str(d.get("id") or "").strip():
            out.append(A.Seg("forward", {"id": str(d["id"]).strip()}))
        elif t:
            out.append(A.Seg("other", {"type": t}))
    sender = ev.get("sender") or {}
    when = ev.get("time")
    return A.Inbound(conv=conv, sender=A.Sender(id=str(uid), nick=clean_in(sender.get("card") or sender.get("nickname") or "", 20)),
                     mid=str(ev.get("message_id") or ""), time=when if isinstance(when, (int, float)) else None,
                     mention=mention, reply_to=reply_to, segments=tuple(out))


# ---------------------------------------------------------------------------
# 适配器本体
# ---------------------------------------------------------------------------
FETCH_PRIMARY = {  # fetch(kind, ref, **别的参数) 里 ref 是哪个参数 (都是只读动作, 在 onebot.ACTIONS 白名单里)
    "get_image": "file", "get_record": "file", "fetch_ptt_text": "message_id", "get_forward_msg": "id",
    "get_msg": "message_id", "get_group_file_url": "file_id", "get_private_file_url": "file_id"}


@dataclass(frozen=True)
class Connect:
    """核心交给 QQ 适配器的连接设置 (纯数据加两个回调; 挂接 SnowLuma 的事留在核心, 这里只是在连之前调一下)."""
    endpoint: str
    token_fn: Callable[[], str]
    heartbeat_s: float = 30
    status_s: float = 60
    before_connect: Callable[[], Awaitable[None]] | None = None


class QQAdapter(A.ChannelAdapter):
    id = ID
    ledger_name = "qq"                   # 记账名现状 (wm/ledger.py): QQ 算 qq
    SEND_ACTION = "qq_send"
    IMPL = {"qq_send": "_send", "qq_fetch": "_fetch"}
    PUBLIC_EXTRA = frozenset({"configure", "on_event", "client", "dropped"})

    def __init__(self):
        super().__init__()
        self.client: OneBotClient | None = None   # 传输对象: 核心的 ChannelManager.client 属性读写的就是它
        self.settings: Settings | None = None
        self.connect: Connect | None = None
        self.dropped = 0                 # 自己就丢掉的 (回声、临时会话…): 核心的 ignored 把它算进去
        self._verified = False

    def configure(self, settings: Settings, connect: Connect | None = None) -> None:
        self.settings = settings
        if connect is not None:
            self.connect = connect

    async def _start(self, host) -> None:
        c = self.connect
        if c is None or self.settings is None:
            raise A.AdapterError("qq-onebot 还没配置")
        self.client = OneBotClient(c.endpoint, c.token_fn, self.on_event, self._on_state,
                                   heartbeat_s=c.heartbeat_s, status_s=c.status_s, before_connect=c.before_connect)
        await self.client.start()

    async def _stop(self) -> None:
        self._verified = False
        if self.client is not None:
            await self.client.stop()
            self.client = None

    def _online(self) -> bool:
        return self._verified

    # -- 状态: OneBotClient 的 connecting / open / closed / auth_failed / token_error / account -> Host.state --
    async def _on_state(self, kind: str, info: dict) -> None:
        if kind == "connecting":
            self._verified = False
            await self.host.state("connecting", {})
        elif kind == "open":
            try:
                me = await self.client.call("get_login_info")
            except OneBotError as exc:
                self.last_error = f"连上了, 但读不到登录信息: {exc}"[:200]
                await self.host.state("connecting", {"error": f"连上了, 但读不到登录信息: {exc}"})
                return
            uid = int(me.get("user_id") or 0)
            if uid != self.settings.self_id:
                self._verified = False
                self.last_error = "协议端挂的不是她的号"
                await self.host.state("wrong_account", {"error": f"协议端挂的是 {uid}, 不是她的号 {self.settings.self_id}: 不收不发"})
                return
            self._verified, self.last_error = True, None
            await self.host.state("online", {"nick": clean_in(me.get("nickname") or "", 30)})
        elif kind == "account":
            if self._verified:
                online = bool(info.get("online"))
                await self.host.state("online" if online else "offline",
                                      {} if online else {"error": "协议端说她的号离线了 (掉线或被踢), 先不收不发"})
        elif kind == "closed":
            self._verified = False
            await self.host.state("connecting", {"error": "和协议端的连接断了, 正在重连"})
        elif kind in ("auth_failed", "token_error"):
            self._verified = False
            self.last_error = str(info.get("error") or "")[:200] or None
            await self.host.state("auth_failed", {"error": info.get("error"), "sub": kind})

    # -- 入站: 事件 -> Inbound (交消息) 或 notice (只提示主人) ----------------------------
    async def on_event(self, ev: dict) -> None:
        kind = ev.get("post_type")
        if kind == "request":
            what = "friend_request" if ev.get("request_type") == "friend" else "group_invite"
            await self.host.notice(what, clean_in(ev.get("comment") or "", 60) + " (没处理, 要的话在 QQ 里自己点)",
                                   user_id=ev.get("user_id"), group_id=ev.get("group_id"))
        elif kind == "notice":
            await self._notice(ev)
        elif kind == "message":
            await self._message(ev)

    async def _message(self, ev: dict) -> None:
        inb = normalize(ev, self.settings)
        if inb is None:
            self.dropped += 1
            return
        await self.host.inbound(inb)

    async def _notice(self, ev: dict) -> None:
        me = self.settings.self_id
        if ev.get("notice_type") == "group_upload" and isinstance(ev.get("file"), dict):
            # 群里有人传了群文件: SnowLuma 只发这个通知 (文件编号、名字、大小, 没有下载地址). 当成那个人在群里发了
            # 一条带文件的消息, 走原来的白名单、触发、读文件那一套 (读的时候按 file_id 问协议端要直链, 见 fetch)
            f = ev["file"]
            await self._message({"post_type": "message", "message_type": "group", "sub_type": "normal",
                                 "group_id": ev.get("group_id"), "user_id": ev.get("user_id"), "self_id": me,
                                 "message_id": "", "time": ev.get("time"), "sender": {},
                                 "message": [{"type": "file", "data": {
                                     "file": f.get("name") or "", "name": f.get("name") or "",
                                     "file_id": str(f.get("id") or ""), "file_size": f.get("size") or 0}}]})
            return
        if ev.get("notice_type") == "group_ban" and int(ev.get("user_id") or 0) == me:
            what, text = "muted", f"她在群 {ev.get('group_id')} 被禁言了"
        elif ev.get("notice_type") == "group_decrease" and ev.get("sub_type") == "kick_me":
            what, text = "kicked", f"她被移出了群 {ev.get('group_id')}"
        else:
            return
        await self.host.notice(what, text, group_id=ev.get("group_id"))

    # -- 出站 -------------------------------------------------------------------
    async def _send(self, conv: A.ConvRef, out: A.Out) -> A.SendResult:
        target = {"group_id": conv.id} if conv.kind == "group" else {"user_id": conv.id}
        action = "send_group_msg" if conv.kind == "group" else "send_private_msg"
        try:
            if out.face is not None:
                data = await self.client.call("send_custom_face", emoji_id=out.face, **target)
            elif out.voice is not None:
                b64 = base64.b64encode(out.voice).decode("ascii")
                data = await self.client.call(action, message=[{"type": "record", "data": {"file": f"base64://{b64}"}}], **target)
            else:
                segs = [{"type": "text", "data": {"text": out.text}}]
                if out.quote and conv.kind == "group":
                    segs.insert(0, {"type": "reply", "data": {"id": out.quote}})
                data = await self.client.call(action, message=segs, **target)
        except (OneBotError, TypeError, ValueError, AttributeError) as exc:
            # 只接原来各处接的那几种: 文字只接 OneBotError、表情包接 OneBotError / AttributeError、语音条接 OneBotError / TypeError / ValueError
            if not isinstance(exc, OneBotError) and not (out.face is not None and isinstance(exc, AttributeError))                     and not (out.voice is not None and isinstance(exc, (TypeError, ValueError))):
                raise
            self.last_error = str(exc)[:200]
            return A.SendResult(False, reason="offline" if "连" in str(exc) else "error", detail=str(exc))
        mid = data.get("message_id") if isinstance(data, dict) else None
        return A.SendResult(True, mid=None if mid is None else str(mid))

    async def _fetch(self, kind: str, ref="", **params):
        if kind not in FETCH_PRIMARY:
            raise A.AdapterError(f"qq_fetch 只做只读的 {sorted(FETCH_PRIMARY)}, 不做 {str(kind)[:30]!r}")
        return await self.client.call(kind, **{FETCH_PRIMARY[kind]: ref}, **params)

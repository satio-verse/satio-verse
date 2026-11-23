"""渠道适配器接口 (对话终端第 2 步, 聊天2). 契约: 知识库 09 问题与待办/世界接口契约（2026-11-15）.md 第 3、4、6 节;
方案: 对话终端方案（2026-11-11）四、渠道适配器.

适配器是搬运工加翻译官: 连平台、把平台的消息翻成 Inbound 交给核心、把核心要发的东西 (Out) 发出去. 核心 (manager.ChannelManager)
留着信任档、访客会话、清洗和外发检查、白名单、限频额度、草稿、日志; 这些适配器碰不到.

方向:
  适配器 -> 核心: 只有 Host 的三个口 -- inbound (交一条消息)、state (报状态)、notice (提示主人, 不进模型).
  核心 -> 适配器: configure (平台自己的设置, 纯数据)、start / stop / send / fetch / status.

信任档由核心定: Inbound 里没有信任档的位置 (from_dict 见到 trust / is_owner / role 这类名字直接抛错); 核心按清单的 trust_from
和 trust_max 与自己写死的授权表取较低者. 本模块不 import manager / guest / store / memory / cloud (测试用 ast 守着).
"""
from __future__ import annotations

import copy
import functools
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..agent import plugin_manifest as PM

IN_TEXT_CHARS = 5000                     # 一条外部消息最多给模型看多少字 (同 manager._IN_TEXT_CHARS)
MANIFEST_DIR = Path(__file__).resolve().parent / "adapters" / "manifests"
STATE_KINDS = ("connecting", "online", "offline", "auth_failed", "wrong_account")
NOTICE_EXTRA = ("user_id", "group_id", "conv")  # notice 放行的额外键: 页面现在读的字段, 别的丢掉
CONV_KINDS = ("private", "group", "paired", "local")
SEG_KINDS = ("text", "at", "reply", "face", "image", "card", "file", "record", "video", "forward", "other")
FAIL_REASONS = ("window_closed", "quota", "rate", "offline", "closed_by_peer", "error")
FORBIDDEN_NAMES = frozenset(PM.TRUST_FIELDS) | frozenset(PM.TRUST_FIELDS_CHAT)


class AdapterError(RuntimeError):
    """适配器、清单或口子的用法不对."""


class InboundError(AdapterError, ValueError):
    """交上来的消息里带了不该有的东西 (信任档之类) 或形状不对."""


@dataclass(frozen=True)
class ConvRef:
    """哪个会话. kind: private / group (QQ) / paired (微信那一个人) / local (本机访客)."""
    kind: str
    id: str | int = ""
    name: str = ""

    def __post_init__(self):
        if self.kind not in CONV_KINDS:
            raise InboundError(f"会话类型只能是 {'/'.join(CONV_KINDS)}, 不能是 {str(self.kind)[:20]!r}")


@dataclass(frozen=True)
class Sender:
    """平台核对过的发信人. 本机访客没有身份, id 为空."""
    id: str = ""
    nick: str = ""
    is_bot: bool = False


@dataclass(frozen=True)
class Seg:
    """消息里的一段 (协议层翻成的结构, 还没编号、没排版). data 里只放平台来的数据."""
    kind: str
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in SEG_KINDS:
            raise InboundError(f"段类型 {str(self.kind)[:20]!r} 不认识")
        if not isinstance(self.data, dict):
            raise InboundError("段的数据要是 dict")
        bad = FORBIDDEN_NAMES & {str(k) for k in self.data}
        if bad:
            raise InboundError(f"段里不许带 {sorted(bad)}: 信任档由核心定, 适配器抬不了")


@dataclass(frozen=True)
class Inbound:
    """适配器交给核心的一条整理好的消息. 没有信任档、没有"是不是主人": 那是核心按会话和清单定的."""
    conv: ConvRef
    sender: Sender = field(default_factory=Sender)
    mid: str = ""
    time: float | None = None            # 平台给的时间 (秒); 没有就是 None, 核心用当时的钟
    mention: bool = False                # 有没有 @ 她
    reply_to: str | None = None          # 回复的是哪条 (没有回复是 None; 回复段里 id 为空是 "")
    segments: tuple = ()
    typed: str = ""                      # 对方自己打的字 (卡片、转发、图片、语音这些不算)
    gaps: tuple = ()                     # 平台那边已经知道处理不了的东西 (微信的占位字)

    def __post_init__(self):
        if not isinstance(self.conv, ConvRef) or not isinstance(self.sender, Sender):
            raise InboundError("conv 要是 ConvRef, sender 要是 Sender")
        if not all(isinstance(s, Seg) for s in self.segments):
            raise InboundError("segments 里要都是 Seg")

    @classmethod
    def from_dict(cls, d: dict) -> "Inbound":
        """从 dict 造 (给还没改成数据类的调用方). 见到信任档一类的名字、不认识的键都抛 InboundError."""
        if not isinstance(d, dict):
            raise InboundError("要是 dict")
        bad = FORBIDDEN_NAMES & {str(k) for k in d}
        if bad:
            raise InboundError(f"不许带 {sorted(bad)}: 信任档由核心定, 适配器抬不了")
        allowed = {"conv", "sender", "mid", "time", "mention", "reply_to", "segments", "typed", "gaps"}
        extra = {str(k) for k in d} - allowed
        if extra:
            raise InboundError(f"不认识的键 {sorted(extra)}")
        conv, sender = d.get("conv"), d.get("sender", Sender())
        if isinstance(conv, dict):
            conv = ConvRef(**conv)
        if isinstance(sender, dict):
            sender = Sender(**sender)
        segs = tuple(s if isinstance(s, Seg) else Seg(**s) for s in d.get("segments") or ())
        return cls(conv=conv, sender=sender, mid=str(d.get("mid") or ""), time=d.get("time"),
                   mention=bool(d.get("mention")), reply_to=d.get("reply_to"), segments=segs,
                   typed=str(d.get("typed") or ""), gaps=tuple(d.get("gaps") or ()))


@dataclass(frozen=True)
class Out:
    """核心交给 send 的: 只放平台发得出去的东西, 不含提示词、不含谁是主人."""
    text: str = ""
    quote: str | None = None             # 回复哪条 (QQ 群里第一条引用触发她的那条)
    face: str | None = None              # 收藏表情的编号
    voice: bytes | None = None           # 语音条 (wav)
    image: bytes | None = None
    caption: str = ""
    payload: dict | None = None          # 本机访客: 这一句的返回值


@dataclass(frozen=True)
class SendResult:
    """发出去没有. detail 留着原来的错误文字, 核心拿它写和以前一模一样的提示."""
    ok: bool
    mid: str | None = None
    reason: str | None = None
    detail: str = ""

    def __post_init__(self):
        if not self.ok and self.reason not in FAIL_REASONS:
            raise AdapterError(f"没发成的原因只能是 {'/'.join(FAIL_REASONS)}")


class Host:
    """核心给适配器的东西: 只有三个口."""
    __slots__ = ()

    async def inbound(self, msg: Inbound) -> None:
        raise NotImplementedError

    async def state(self, kind: str, info: dict | None = None) -> None:
        raise NotImplementedError

    async def notice(self, kind: str, text: str, **extra) -> None:
        raise NotImplementedError


class HostPorts(Host):
    """核心为每个适配器造一个. 只持有三个函数, 没有公开属性; 口子自己查参数, 脏的不往里递."""
    __slots__ = ("_in", "_st", "_nt")

    def __init__(self, inbound, state, notice):
        self._in, self._st, self._nt = inbound, state, notice

    async def inbound(self, msg: Inbound) -> None:
        if not isinstance(msg, Inbound):
            raise InboundError("inbound 只收 Inbound")
        await self._in(msg)

    async def state(self, kind: str, info: dict | None = None) -> None:
        if kind not in STATE_KINDS:
            raise AdapterError(f"状态只能是 {'/'.join(STATE_KINDS)}, 不能是 {str(kind)[:20]!r}")
        await self._st(kind, dict(info or {}))

    async def notice(self, kind: str, text: str, **extra) -> None:
        await self._nt(str(kind), str(text), {k: v for k, v in extra.items() if k in NOTICE_EXTRA})


class AdapterManifestError(AdapterError):
    """自带清单不过校验 (或来路政策): 不注册."""


def load_manifest(adapter_id: str, source: str = "bundled") -> dict:
    """(读过一次就记着, 给的是副本) 读 adapters/manifests/<id>.json, 过 validate_plugin_manifest 和 policy_issues(…, source). 有 error 就抛."""
    return copy.deepcopy(_load_manifest(adapter_id, source))


@functools.lru_cache(maxsize=16)
def _load_manifest(adapter_id: str, source: str) -> dict:
    path = MANIFEST_DIR / f"{adapter_id}.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise AdapterManifestError(f"{adapter_id}: 读不了清单文件") from None
    except ValueError:
        raise AdapterManifestError(f"{adapter_id}: 清单不是合法的 JSON") from None
    issues = PM.validate_plugin_manifest(manifest) + PM.policy_issues(manifest, source)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        raise AdapterManifestError(f"{adapter_id}: 清单不合格: " + "; ".join(f"{i.code}@{i.path}" for i in errors[:5]))
    return manifest


class ChannelAdapter:
    """一个平台一个. 子类填 id / SEND_ACTION / IMPL (动作名 -> 方法名), 实现 _start / _stop / _send / _fetch / _extra_status.

    send 是基类的: 先看清单声明了这个动作没有, 再按清单 proactive 看能不能发 (never 只能回这一次的请求, reply_window 要窗口开着),
    都过了才调子类的 _send. 清单说不能主动发的, 这里就发不了."""
    id = ""
    ledger_name = ""                     # 记账名: 现状 qq / other (wm/ledger.py), 这一步不改
    SEND_ACTION = ""
    IMAGE_ACTION = ""                    # 发图的动作名 (没有就空)
    IMPL: dict = {}
    PUBLIC_EXTRA: frozenset = frozenset()  # 子类额外的公开方法名 (测试对账用)

    def __init__(self):
        self.manifest = load_manifest(self.id)
        world = self.manifest["plugin"]["world"]
        self.prefix = world["prefix"]
        self.proactive = dict(world["proactive"])
        self.caps = dict(world["caps"])
        self.trust_from = world["trust_from"]
        self.trust_max = world["trust_max"]
        self.host: Host | None = None
        self.started = False
        self.last_error: str | None = None

    def actions(self) -> frozenset:
        """这个适配器真正做得了的动作名 (测试拿它对清单)."""
        return frozenset(self.IMPL)

    def declared_actions(self) -> frozenset:
        return frozenset(a["name"] for a in self.manifest["plugin"]["world"]["actions"])

    def attach(self, host: Host) -> None:
        """只挂上三个口, 不起平台连接 (核心在适配器还没 start 就收到东西时用)."""
        self.host = host

    async def start(self, host: Host) -> None:
        self.host = host
        await self._start(host)
        self.started = True

    async def stop(self) -> None:
        self.started = False
        await self._stop()

    async def send(self, conv: ConvRef, out: Out) -> SendResult:
        need = self.IMAGE_ACTION if out.image is not None else self.SEND_ACTION
        if not need or need not in self.IMPL:
            return SendResult(False, reason="error", detail=f"{self.id} 没有这个发送动作")
        mode = self.proactive["mode"]
        if mode == "never" and not self._reply_ok(conv, out):
            return SendResult(False, reason="window_closed", detail="这条通道不能主动发, 只能回这一次的请求")
        if mode == "reply_window" and not self._window_open():
            return SendResult(False, reason="window_closed", detail="回复窗口没开 (对方还没说过话)")
        if mode == "quota":
            return SendResult(False, reason="quota", detail="配额型通道还没实现")
        return await self._send(conv, out)

    async def fetch(self, kind: str, ref="", **params):
        name = f"{self.prefix}_fetch"
        if name not in self.IMPL:
            raise AdapterError(f"{self.id} 没有 {name}")
        return await self._fetch(kind, ref, **params)

    def status(self) -> dict:
        """给面板看的状态, 不含号码和密钥."""
        return {"id": self.id, "online": self._online(), "error": self.last_error, **self._extra_status()}

    # -- 子类 -------------------------------------------------------------
    def _reply_ok(self, conv: ConvRef, out: Out) -> bool:
        return False

    def _window_open(self) -> bool:
        return False

    def _online(self) -> bool:
        return self.started

    def _extra_status(self) -> dict:
        return {}

    async def _start(self, host: Host) -> None:
        raise NotImplementedError

    async def _stop(self) -> None:
        raise NotImplementedError

    async def _send(self, conv: ConvRef, out: Out) -> SendResult:
        raise NotImplementedError

    async def _fetch(self, kind: str, ref="", **params):
        raise NotImplementedError

"""账号保管箱 / 自动挂接 和 渠道管理器 (manager.ChannelManager) 之间的接线. 管理器只管在 __init__ 里建一个, 别的都在这里:

- 要用的账号用途还没点头 (vault.lease -> ask) 时弹一张申请卡片: 和别的申请一样, 15 秒没人点就上 QQ / 微信问 (QQ 掉着时
  走微信, 在你最近 reply_window_s 内找过她的前提下; 都发不出就一直等, 不过期), 点了"允许"以后同一个用途不再问, 页面上能收回;
- 挂接 QQ 出了要主人动手的事 (没存密码、密码被拒、QQ 要登录 / 扫码) 时在桌宠上提醒一次, 能发就也发给主人; 同一件事隔很久才再说.
卡片和提醒里没有密码、令牌、Cookie.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import vault as _vault
from .vault import Vault

log = logging.getLogger("wm.channels.vault_bridge")


class Bridge:
    def __init__(self, vault: Vault, manager, clock=time.monotonic):
        self.vault, self.m, self.clock = vault, manager, clock
        self.loop: asyncio.AbstractEventLoop | None = None
        self._said: dict[str, float] = {}
        self._toasts: set[asyncio.Task] = set()   # 在弹的桌面通知 (powershell 最多等 15 秒, 不能拖住挂接那一圈)
        vault.on_want = self.want

    def bind_loop(self) -> None:
        with contextlib.suppress(RuntimeError):
            self.loop = asyncio.get_running_loop()

    # -- 申请 ----------------------------------------------------------------
    def want(self, acct: dict, purpose: dict, by: str) -> None:
        """vault 要用的用途还没点头. 可能在任何线程里被调: 转到事件循环里弹卡片. 没有循环 (测试、还没启动) 就不弹,
        页面上照样看得到"等你点头"."""
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._card(acct, purpose, by)))

    async def _card(self, acct: dict, purpose: dict, by: str) -> None:
        m = self.m
        if not getattr(m, "conf", None):  # 渠道从没配置过: 卡片没处弹
            return
        who = acct.get("label") or acct.get("platform_name") or acct["id"]
        aid, pid = acct["id"], purpose["id"]

        def answer(ok: bool) -> None:
            with contextlib.suppress(Exception):
                self.vault.set_purpose(aid, pid, "allow" if ok else "deny", by="card")

        text = (f"用途：{purpose.get('name', pid)}。{purpose.get('desc', '')}"
                f"（点了「允许」以后同一个用途不再问；可以随时在 {_vault.PAGE_URL} 收回）")
        try:
            await m._ask_card("agent_ask", ("vault", aid, pid), f"她想用保管箱里的「{who}」", text, "允许", "不允许",
                              qq=f"我想用保管箱里存的「{who}」来{purpose.get('name', pid)}。可以吗?",
                              what=f"用保管箱里的「{who}」{purpose.get('name', pid)}", on_answer=answer, qq_cap=300)
        except Exception:
            log.exception("保管箱的申请卡片没弹出来")

    # -- 提醒 ----------------------------------------------------------------
    async def notice(self, kind: str, text: str, *, key: str | None = None, cooldown_s: float = 6 * 3600,
                     tell: bool = False, image: bytes | None = None) -> bool:
        """桌宠上提醒一次 (channel_notice); tell: 能发就也发给主人 (微信 / QQ 私聊); image: 带一张图 (QQ 登录的二维码),
        只有主人这会儿在微信上找得到她时才发得出去 (QQ 掉着才会要二维码; ClawBot 只能接着主人的话回), 发不出去就只发文字.
        同一个 key 在 cooldown_s 内只说一次. 返回这次说没说."""
        k = key or kind
        now = self.clock()
        last = self._said.get(k)
        if last is not None and now - last < cooldown_s:
            return False
        self._said[k] = now
        m = self.m
        with contextlib.suppress(Exception):
            await m.notify({"type": "channel_notice", "channel": m.channel, "kind": kind, "text": text})
        if tell:
            if not (image and await self._tell_image(text, image)):
                with contextlib.suppress(Exception):
                    await m._tell_owner(text)
            self._toast("QQ 停在登录界面等扫码" if image else text)  # QQ 掉着时 _tell_owner 常常发不出去: 桌面上再弹一条 (不带图)
        return True

    def _toast(self, text: str) -> None:
        """申请卡片的桌面通知 (急3, manager.desk_notice) 也给这几条"主人得动手"的提醒用. 只在这条提醒真说了 (过了冷却) 时来这里,
        所以弹的次数跟冷却走; 没有 desk_notice (测试、没有存申请的文件) 或弹不出来都不影响别的."""
        show = getattr(self.m, "desk_notice", None)
        if show is None:
            return

        async def run() -> None:
            with contextlib.suppress(Exception):
                await show("绫音的 QQ 没连上", text)
        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            return
        self._toasts.add(task)
        task.add_done_callback(self._toasts.discard)

    async def _tell_image(self, text: str, png: bytes) -> bool:
        """给主人发一张图 + 一句话: 只走微信 (主人最近 weixin_window 内在微信上找过她). 图经过腾讯的服务器 (主人和 ClawBot 的
        那段聊天), 所以只发 QQ 登录二维码这一种东西. 发不出去返回 False."""
        m = self.m
        try:
            conv, wx = m._owner_conv(), getattr(m, "weixin", None)
            if conv is None or conv.key[0] != "weixin" or wx is None or not hasattr(wx, "send_image"):
                return False
            if not m._outbound_allowed("channel_send", {"channel": "weixin", "text": text}, conv.key):
                return False
            await wx.send_image(png, caption=text)
            return True
        except Exception as exc:  # 图没发成不能连累提醒: 退回只发文字
            log.info("二维码没发到微信 (%s), 改发文字", type(exc).__name__)
            return False

    def reset(self, prefix: str = "") -> None:
        """问题好了: 同类的提醒下回出问题时重新说."""
        for k in [k for k in self._said if k.startswith(prefix)]:
            self._said.pop(k, None)

"""本机访客入口 (wm-guest 的 /guest, 给 AstrBot 这类第三方框架用): 一句进、一句出. 清单 manifests/local-guest.json.

没有发信人身份, 固定熟人档 (档由核心定); 不能主动发: 只有手里有这一次请求的回复句柄才发得出去 (proactive=never).
一句话的过程: ask() 把它整理成 Inbound 交给核心 (host.inbound), 核心处理完调 send() 把回话交回来, ask() 返回.
"""
from __future__ import annotations

import asyncio
import itertools

from .. import adapter as A
from ..outbound import clean_in

ID = "local-guest"


class LocalGuestAdapter(A.ChannelAdapter):
    id = ID
    ledger_name = ""                     # 本机访客不记渠道账 (核心的云端模型自己记 channels)
    SEND_ACTION = "guest_reply"
    IMPL = {"guest_reply": "_send"}
    PUBLIC_EXTRA = frozenset({"ask"})

    def __init__(self):
        super().__init__()
        self._pending: dict[int, asyncio.Future] = {}
        self._seq = itertools.count(1)

    async def _start(self, host) -> None:
        return None

    async def _stop(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def ask(self, user, text) -> dict:
        """一句话进来: 返回核心交回来的 {ok, reply, emo} 或 {ok: False, status, error}."""
        if self.host is None:
            raise A.AdapterError("local-guest 还没起")
        token = next(self._seq)
        fut = asyncio.get_running_loop().create_future()
        self._pending[token] = fut
        msg = A.Inbound(conv=A.ConvRef("local", token), sender=A.Sender(nick=clean_in(user, 64)), mid=str(token),
                        segments=(A.Seg("text", {"text": clean_in(text, 1000)}),))
        try:
            await self.host.inbound(msg)
            if not fut.done():  # 核心处理完却没交回话 (不该发生): 别让调用方一直等
                return {"ok": False, "status": 500, "error": "内部错误: 没有回话"}
            return fut.result()
        finally:
            self._pending.pop(token, None)

    def _reply_ok(self, conv: A.ConvRef, out: A.Out) -> bool:
        fut = self._pending.get(conv.id) if conv.kind == "local" else None
        return fut is not None and not fut.done() and out.payload is not None

    async def _send(self, conv: A.ConvRef, out: A.Out) -> A.SendResult:
        fut = self._pending.get(conv.id)
        fut.set_result(out.payload)
        return A.SendResult(True, mid=str(conv.id))

    def _online(self) -> bool:
        return self.started

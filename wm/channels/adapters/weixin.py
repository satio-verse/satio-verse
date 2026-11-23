"""微信 (ClawBot) 适配器: 包一个 X.WeixinClient. 清单 manifests/weixin-ilink.json.

只有扫码绑定的那一个人 (trust_from=paired_owner, 不能进群). 平台不让主动推送: 只能接着对方的话回 (proactive=reply_window),
靠对方最近一条话带来的 context_token 判断窗口开没开. 扫码绑定的管理接口 (begin_login / logout …) 不是消息通道, 仍经 client 用.

WeixinClient 没有状态回调 (不改它): 在线与否靠 status() 拉.
"""
from __future__ import annotations

from .. import adapter as A
from .. import weixin as X

ID = "weixin-ilink"
CONV = A.ConvRef("paired", 0)


class WeixinAdapter(A.ChannelAdapter):
    id = ID
    ledger_name = "other"                # 记账名现状 (wm/ledger.py): 微信算 other
    SEND_ACTION = "wx_send"
    IMAGE_ACTION = "wx_send_image"
    IMPL = {"wx_send": "_send", "wx_send_image": "_send"}
    PUBLIC_EXTRA = frozenset({"feed", "client"})

    def __init__(self, client: X.WeixinClient | None = None):
        super().__init__()
        self.client = client             # 传输对象: 核心的 ChannelManager.weixin 属性读写的就是它

    async def _start(self, host) -> None:
        self.client.start()              # 绑过微信就开始收 (没绑什么都不做)

    async def _stop(self) -> None:
        await self.client.close()

    async def feed(self, w: dict) -> None:
        """WeixinClient 的 on_message({text, mid, time, gaps, typed}) 进来的一条: 整理成 Inbound 交给核心."""
        if self.host is None:
            raise A.AdapterError("weixin-ilink 还没挂上口")
        await self.host.inbound(A.Inbound(
            conv=CONV, sender=A.Sender(id="paired"), mid=str(w["mid"]), time=w["time"], mention=True,
            segments=(A.Seg("text", {"text": w["text"]}),), typed=str(w.get("typed") or ""), gaps=tuple(w["gaps"])))

    def _window_open(self) -> bool:
        return bool(getattr(self.client, "context_token", ""))

    async def _send(self, conv: A.ConvRef, out: A.Out) -> A.SendResult:
        try:
            if out.image is not None:
                await self.client.send_image(out.image, caption=out.caption)
            else:
                await self.client.send_text(out.text)
        except X.WeixinError as exc:
            self.last_error = str(exc)[:200]
            return A.SendResult(False, reason="offline" if exc.code == X.STALE_TOKEN else "error", detail=str(exc))
        return A.SendResult(True)

    def _online(self) -> bool:
        return bool(getattr(self.client, "online", False))

    def _extra_status(self) -> dict:
        return {"bound": bool(getattr(self.client, "bound", False))}

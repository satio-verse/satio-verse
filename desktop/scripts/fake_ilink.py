"""面板自检用 (paneltest.ps1) 的假微信 iLink 服务: 给二维码, 扫码状态一直是"等扫码", 收消息一直是空的. 不连腾讯.

python desktop/scripts/fake_ilink.py [--port 8809]
"""
import argparse
import asyncio

from aiohttp import web


async def qrcode(request):
    return web.json_response({"qrcode": "selftest-qr", "qrcode_img_content": "https://liteapp.weixin.qq.com/q/selftest"})


async def status(request):
    await asyncio.sleep(1)
    return web.json_response({"status": "wait"})


async def updates(request):
    await asyncio.sleep(2)
    return web.json_response({"ret": 0, "msgs": []})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8809)
    a = ap.parse_args()
    app = web.Application()
    app.router.add_post("/ilink/bot/get_bot_qrcode", qrcode)
    app.router.add_get("/ilink/bot/get_qrcode_status", status)
    app.router.add_post("/ilink/bot/getupdates", updates)
    web.run_app(app, host="127.0.0.1", port=a.port, print=None)


if __name__ == "__main__":
    main()

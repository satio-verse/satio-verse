"""账号保管箱页面 (/vault) 的预览服务: 只用替身 (假的 SnowLuma 管理页、假的 B 站扫码登录), 保管箱放临时目录,
不碰真的账号、密码、QQ、B 站. 用法: python tools/vault_preview.py [端口, 默认 8862]; 浏览器打开 http://127.0.0.1:端口/vault.

用来在浏览器里点一遍页面. 替身的状态用 /sim/... 改 (GET 就行):
  /sim/qq?mode=none|login|mine|other|both     电脑上有什么 QQ: 没有 / 停在登录界面 / 登录着她的号 / 登录着别的号 / 两个都登录着
  /sim/qr?on=1                                 QQ 登录窗口上能读到二维码 (on=0 读不到)
  /sim/bili?script=86101,86090,0               扫码依次回什么状态 (86101 没扫 86090 扫了没确认 86038 过期 0 成功)
  /sim/bili?mid=999&uname=别人                 扫出来的是另一个号
  /sim/onebot?up=0                             OneBot 端口开着没有 (挂接成功会自己变成开着)
  /sim/card?answer=allow|deny                  替主人答复最近一张申请卡片
  /sim/state                                   看替身和启动器现在的样子
管理页的密码是 correct-horse-battery (只在这个替身里有效).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from aiohttp import web  # noqa: E402

import vault_fakes as F  # noqa: E402
from wm.channels import bili as BILI  # noqa: E402
from wm.channels import qq_window as QW  # noqa: E402
from wm.channels import snowluma as S  # noqa: E402
from wm.channels import snowluma_hook as H  # noqa: E402
from wm.channels import vault as V  # noqa: E402
from wm.channels import vault_http  # noqa: E402
from wm.channels.vault_bridge import Bridge  # noqa: E402

SELF_ID = 3091211145


class PreviewManager:
    """渠道管理器的替身: 只有保管箱页面和挂接用得到的那几样."""
    channel = "qq"

    def __init__(self, tmp: Path):
        self.conf = {"enabled": True, "self_id": SELF_ID, "snowluma_dir": str(tmp / "SnowLuma"), "snowluma_autostart": True,
                     "snowluma_attach": True, "endpoint": "ws://127.0.0.1:3001/", "bili_cookie_file": str(tmp / "bili_cookie.txt")}
        self.state, self.error = "connecting", None
        self.cards: list[dict] = []
        self.notices: list[str] = []
        self.told: list[str] = []
        self.weixin = types.SimpleNamespace(status=lambda: {"bound": True, "state": "online", "user": "o9cq…7f3a"})
        self.vault = V.Vault(V.HOME)
        self.bridge = Bridge(self.vault, self)
        self.snowluma = S.SnowLumaLauncher(self.vault, self.bridge)

    async def notify(self, msg: dict) -> None:
        if msg.get("type") == "channel_notice":
            self.notices.append(msg["text"])
            print("[提醒]", msg["text"], flush=True)

    async def _tell_owner(self, text: str) -> bool:
        self.told.append(text)
        return True

    async def _ask_card(self, kind, key, title, text, ok, no, qq="", what="", remote=True, **extra):
        self.cards.append({"key": key, "title": title, "text": text, "qq": qq, "on_answer": extra.get("on_answer")})
        print("[申请卡片]", title, "|", text, flush=True)
        return f"d{len(self.cards)}"


async def main(port: int) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vault-preview-"))
    V.HOME = tmp / "vault"
    # SnowLuma 的安装目录 (只是个样子: 里面没有真的 SnowLuma)
    (tmp / "SnowLuma" / "config").mkdir(parents=True)
    (tmp / "SnowLuma" / ("node.exe" if os.name == "nt" else "node")).write_bytes(b"fake")
    (tmp / "SnowLuma" / "index.mjs").write_text("// preview")
    (tmp / "SnowLuma" / "config" / "consent.json").write_text('{"version": "preview", "acceptedAt": "2026-11-07"}')

    sl = F.FakeSnowLuma(procs=[])
    sl_runner, sl_port = await F.serve(sl.app())
    bili = F.FakeBili()
    bili_runner, bili_port = await F.serve(bili.app())
    mgr = PreviewManager(tmp)
    BILI.use_vault(mgr.vault)

    # 把"本机的东西"换成替身: 没有真的 SnowLuma 进程、QQ 进程、端口
    async def listening(_endpoint):
        return sl.onebot_up
    S._listening = listening
    S._existing_process = lambda _directory: 4242
    S._spawn = lambda _directory: types.SimpleNamespace(pid=4242, poll=lambda: None)
    H.qq_main_pids = lambda: [p["pid"] for p in sl.procs]

    async def find_webui(_directory):
        return sl_port
    H.find_webui = find_webui
    shots = {"on": True}

    def snapshot(pids):
        if not shots["on"]:
            return None
        return QW.Shot("https://qm.qq.com/preview-login?k=PREVIEW", QW.render_png("https://qm.qq.com/preview-login?k=PREVIEW"), list(pids)[0], time.time())
    QW.snapshot_qr = snapshot

    cfg = {"server": {"port": port}, "vault": {"bili_passport": f"http://127.0.0.1:{bili_port}", "bili_api": f"http://127.0.0.1:{bili_port}"}}
    engines = types.SimpleNamespace(channels=mgr)
    app = web.Application()

    def refusal(request):
        host = request.headers.get("Host", "").lower()
        return None if host in {f"127.0.0.1:{port}", f"localhost:{port}"} else "Host 不对"

    vault_http.register(app, cfg, refusal, engines, None)

    async def sim_qq(request):
        mode = request.query.get("mode", "none")
        mine, other, login = F.qq_proc(1001, SELF_ID), F.qq_proc(1002, 111222333, nickname="别人"), F.qq_proc(1003, None)
        sl.procs[:] = {"none": [], "login": [login], "mine": [mine], "other": [other], "both": [mine, other]}.get(mode, [])
        sl.onebot_up = False
        return web.json_response({"ok": True, "procs": sl.procs})

    async def sim_qr(request):
        shots["on"] = request.query.get("on", "1") != "0"
        return web.json_response({"ok": True, "on": shots["on"]})

    async def sim_bili(request):
        if request.query.get("script"):
            bili.script = [int(x) for x in request.query["script"].split(",")]
        if request.query.get("mid"):
            bili.mid = request.query["mid"]
        if request.query.get("uname"):
            bili.uname = request.query["uname"]
        if request.query.get("valid"):
            bili.valid = request.query["valid"] != "0"
        return web.json_response({"ok": True, "script": bili.script, "mid": bili.mid, "valid": bili.valid})

    async def sim_onebot(request):
        sl.onebot_up = request.query.get("up", "1") != "0"
        return web.json_response({"ok": True, "up": sl.onebot_up})

    async def sim_card(request):
        if not mgr.cards:
            return web.json_response({"ok": False, "error": "没有卡片"})
        card = mgr.cards[-1]
        card["on_answer"](request.query.get("answer") == "allow")
        return web.json_response({"ok": True, "title": card["title"]})

    async def sim_state(request):
        return web.json_response({"snowluma_fake": {"procs": sl.procs, "logins": sl.logins, "logouts": sl.logouts, "loads": sl.loads,
                                                    "onebot_up": sl.onebot_up, "bad": sl.bad}, "launcher": mgr.snowluma.status(),
                                  "cards": [c["title"] for c in mgr.cards], "notices": mgr.notices, "told": mgr.told,
                                  "bili_calls": bili.calls}, dumps=lambda o: __import__("json").dumps(o, ensure_ascii=False))

    for path, handler in (("/sim/qq", sim_qq), ("/sim/qr", sim_qr), ("/sim/bili", sim_bili), ("/sim/onebot", sim_onebot),
                          ("/sim/card", sim_card), ("/sim/state", sim_state)):
        app.router.add_get(path, handler)

    async def loop():  # 真服务端里是 OneBot 的重连循环在调; 这里每 4 秒一次
        while True:
            await asyncio.sleep(4)
            try:
                await mgr.snowluma.ensure(mgr.conf)
            except Exception as exc:  # 预览服务别因为一次出错就停
                print("ensure 出错:", type(exc).__name__, flush=True)

    asyncio.create_task(loop())
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    print(f"账号保管箱预览: http://127.0.0.1:{port}/vault  (保管箱在 {V.HOME}; 假的 SnowLuma 管理页端口 {sl_port}, 密码 {sl.password})", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8862))

"""2D 广场本机演示 (样板: 主程序不引用). 2026-11-15, 世界1.

起一个本机中继、两个广场插件进程、两个世界会话 (脚本假模型, 不是真的绫音)、一个只听 127.0.0.1 的小网页.
网页把两只宠物的旁观面板并排放在断网沙箱 iframe 里, 页面脚本充当"面板桥". 点"叫她回来"走真的 WorldHost.leave().

  python tools/plaza_demo.py                 打印网址, 在浏览器里打开; Ctrl+C 退出
  python tools/plaza_demo.py --selftest      不开网页, 跑到两边各说完三句, 叫乙离场, 打印时间线, 退出码 0 / 1
  --relay-port 8801  --web-port 8802         被占用就换一个空的端口并打印出来

不连外网、不下载、不读 ~/.worldmodel、设置和密钥. 人设是下面写死的两句话 (不读面板里的预设).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PLUGIN = ROOT / "plugins" / "plaza-2d"

from aiohttp import web  # noqa: E402

from wm.world.envelope import Audit, WorldSpec  # noqa: E402
from wm.world.host import WorldHost, plugin_env  # noqa: E402
from wm.world.session import WorldSession  # noqa: E402

LINES = {
    "绫音": ["你好呀，我是绫音。", "今天广场好安静，你常来吗？", "那我们下次再约，先走啦。"],
    "测试人设": ["你好，我是测试人设～", "第一次来，觉得挺舒服的。", "好呀，下次见！"],
}
PERSONA = {"绫音": "温和、好奇的虚拟角色，说话简短口语化。", "测试人设": "爱开玩笑、有点话多的虚拟角色。"}


def load_relay():
    spec = importlib.util.spec_from_file_location("plaza_relay_demo", ROOT / "tools" / "plaza_relay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DemoBrain:
    """脚本假模型 (有一点自己的状态): 看到对方就走近, 走到身边再打招呼, 听到话就按顺序说下一句 (各三句)."""

    def __init__(self, name: str, first: bool, delay: float):
        self.name, self.first, self.delay = name, first, delay
        self.lines = list(LINES[name])
        self.target = None      # (距离档, 方向): 最近一次看到对方的样子
        self.greeted = False

    async def stream(self, messages, schema=None, max_tokens=0, id_slot=None, meta=None, **kw):
        assert "tools" not in kw
        await asyncio.sleep(self.delay)
        yield json.dumps({"actions": self.decide(messages[-1]["content"])}, ensure_ascii=False)

    def decide(self, user: str) -> list:
        said = False
        for line in user.splitlines():
            if not line.startswith("[#"):
                continue
            head, _, body = line.partition("] ")
            kind = head.split(" ")[-1]
            kv = dict(p.split("=", 1) for p in body.split(":", 1)[0].split() if "=" in p)
            if kind in ("pet_entered", "pet_moved"):
                self.target = (kv.get("distance_bucket"), kv.get("direction"))
            elif kind == "pet_left":
                self.target = None
            elif kind == "pet_said":
                said = True
        acts: list = []
        if said and self.lines:
            acts.append({"name": "plaza_say", "args": {"text": self.lines.pop(0)}})
            if not self.lines:
                acts.append({"name": "plaza_emote", "args": {"emote": "wave"}})
        elif self.target and not said:
            bucket, direction = self.target
            if bucket != "near" and direction in ("left", "right", "up", "down"):
                acts.append({"name": "plaza_move", "args": {"direction": direction}})
            elif bucket == "near" and self.first and not self.greeted and self.lines:
                self.greeted = True
                acts.append({"name": "plaza_say", "args": {"text": self.lines.pop(0)}})
        return acts


class DemoPet:
    def __init__(self, name: str, first: bool, relay_port: int, delay: float, feed: list, code: str = ""):
        import json as _j
        manifest = _j.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))
        self.name, self.code, self.relay_port = name, code, relay_port
        self.spec = WorldSpec.from_manifest(manifest)
        self.audit = Audit()
        self.brain = DemoBrain(name, first, delay)
        self.feed = feed
        self.session = WorldSession(self.spec, name, self.brain, audit=self.audit, persona=PERSONA[name],
                                    env_prompt=(PLUGIN / "ENV_PROMPT.md").read_text(encoding="utf-8"), batch_s=0.1)
        self.host = WorldHost(self.spec, self.session, audit=self.audit,
                              on_panel=lambda d: feed.append({"pet": name, "kind": "panel", "data": d}),
                              on_log=lambda i: feed.append({"pet": name, "kind": "log", "item": i}))
        self.left = None

    async def start(self) -> None:
        env = plugin_env(None, WM_PLUGIN_TOKEN="demo-" + self.name, WM_PLUGIN_PORT=0, PLAZA_NAME=self.name,
                         PLAZA_RELAY=f"ws://127.0.0.1:{self.relay_port}/plaza",
                         **({"PLAZA_CODE": self.code} if self.code else {"PLAZA_CREATE": "1"}))
        await self.host.launch([sys.executable, str(PLUGIN / "relay_client.py")], env, "demo-" + self.name, cwd=str(PLUGIN))

    def said(self) -> list:
        return [i["args"]["text"] for i in self.host.log if i["k"] == "act" and i["name"] == "plaza_say"]

    async def leave(self):
        self.left = await self.host.leave()
        return self.left


async def start_rig(args, delay):
    R = load_relay()
    relay = R.Relay(spawn=[(4, 6), (10, 6)])
    try:
        port = await relay.start(args.relay_port)
    except OSError:
        port = await relay.start(0)
        print(f"端口 {args.relay_port} 被占用, 中继换到 {port}", flush=True)
    feed: list = []
    a = DemoPet("绫音", True, port, delay, feed)
    await a.start()
    for _ in range(100):
        if a.host.panel and a.host.panel.get("code"):
            break
        await asyncio.sleep(0.1)
    b = DemoPet("测试人设", False, port, delay, feed, code=a.host.panel["code"])
    await b.start()
    return relay, a, b, feed


async def selftest(args) -> int:
    relay, a, b, feed = await start_rig(args, 0.05)
    ok = False
    try:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 30 and not (len(a.said()) >= 3 and len(b.said()) >= 3):
            await asyncio.sleep(0.1)
        print("== 时间线 ==")
        for it in feed:
            if it["kind"] != "log":
                continue
            i = it["item"]
            if i["k"] == "in" and i["type"] in ("pet_entered", "pet_left", "pet_said", "pet_emoted"):
                print(f"{it['pet']} 收到 {i['type']} {i['fields']} {i['text']}")
            elif i["k"] == "act" and i["name"] != "plaza_move":
                print(f"{it['pet']} 做 {i['name']} {i['args']}")
        chat_ok = a.said() == LINES["绫音"] and b.said() == LINES["测试人设"]
        heard_ok = [i["text"] for i in a.host.log if i["k"] == "in" and i["type"] == "pet_said"] == LINES["测试人设"]
        r = await b.leave()
        await asyncio.sleep(0.5)
        saw_left = any(i["k"] == "in" and i["type"] == "pet_left" for i in a.host.log)
        print(f"== 叫乙离场: {r.status}，{r.elapsed:.2f} 秒；甲看到她离开: {saw_left} ==")
        ok = chat_ok and heard_ok and r.status == "acknowledged" and r.elapsed < 3 and saw_left
        print("自检", "通过" if ok else "没通过", f"(聊三轮: {chat_ok}, 互相听到: {heard_ok})")
    finally:
        for p in (a, b):
            await p.host.stop()
        await relay.stop()
    return 0 if ok else 1


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>广场演示</title>
<style>
:root { --bg:#f6f7f9; --ink:#1d2433; --muted:#5d6676; --line:#d7dce5; }
@media (prefers-color-scheme: dark) { :root { --bg:#14171d; --ink:#e8ebf2; --muted:#9aa3b5; --line:#2e3442; } }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.5 system-ui,"Microsoft YaHei",sans-serif; }
.top { padding:10px 16px; border-bottom:1px solid var(--line); color:var(--muted); }
.row { display:grid; grid-template-columns:1fr 1fr; gap:8px; padding:8px; }
@media (max-width:900px) { .row { grid-template-columns:1fr; } }
iframe { width:100%; height:560px; border:1px solid var(--line); border-radius:10px; background:var(--bg); }
</style></head><body>
<div class="top">本机演示：两只宠物都是脚本假模型，不是真的绫音；不连外网。面板里点“叫她回来”会真的走叫停流程。</div>
<div class="row" id="row"></div>
<script>
var pets = ['绫音', '测试人设'], frames = [], ready = [false, false], queue = [[], []];
pets.forEach(function () {
  var f = document.createElement('iframe');
  f.setAttribute('sandbox', 'allow-scripts');   // 断网沙箱: 没有同源权限, 面板只能靠 postMessage 说话
  f.src = '/panel';
  document.getElementById('row').appendChild(f);
  frames.push(f);
});
function send(i, m) { if (ready[i]) frames[i].contentWindow.postMessage({ wmHost: m }, '*'); else queue[i].push(m); }
window.addEventListener('message', function (e) {
  var i = frames.findIndex(function (f) { return f.contentWindow === e.source; });
  if (i < 0 || !e.data || !e.data.wmPanel) return;
  var msg = JSON.parse(e.data.wmPanel);
  if (msg.op === 'ready') { ready[i] = true; send(i, { kind: 'hello', name: pets[i] }); queue[i].splice(0).forEach(function (m) { send(i, m); }); }
  if (msg.op === 'leave') fetch('/leave/' + i, { method: 'POST' });
});
var es = new EventSource('/events');
es.onmessage = function (e) {
  var it = JSON.parse(e.data), i = pets.indexOf(it.pet);
  send(i, it.kind === 'panel' ? { kind: 'panel', data: it.data } : { kind: 'log', item: it.item });
};
</script></body></html>
"""


async def serve(args) -> int:
    relay, a, b, feed = await start_rig(args, 0.7)
    pets = [a, b]

    async def page(request):
        return web.Response(text=PAGE, content_type="text/html")

    async def panel(request):
        return web.Response(body=(PLUGIN / "spectate.html").read_bytes(), content_type="text/html",
                            headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"})

    async def events(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        idx = 0
        try:
            while True:
                while idx < len(feed):
                    await resp.write(b"data: " + json.dumps(feed[idx], ensure_ascii=False).encode() + b"\n\n")
                    idx += 1
                await asyncio.sleep(0.1)
        except (ConnectionError, asyncio.CancelledError):
            pass
        return resp

    async def leave(request):
        i = int(request.match_info["i"])
        if i not in (0, 1) or pets[i].left is not None:
            return web.json_response({"ok": False})
        r = await pets[i].leave()
        return web.json_response({"ok": True, "status": r.status, "elapsed": round(r.elapsed, 2)})

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/panel", panel)
    app.router.add_get("/events", events)
    app.router.add_post("/leave/{i}", leave)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1.0)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", args.web_port).start()
    except OSError:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        print(f"端口 {args.web_port} 被占用，网页换到别的端口", flush=True)
    url = f"http://127.0.0.1:{runner.addresses[0][1]}/"
    print(f"广场演示已启动：{url}\n中继：ws://127.0.0.1:{relay.port}/plaza　邀请码：{a.host.panel['code']}\nCtrl+C 退出", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        for p in pets:
            await p.host.stop()
        await runner.cleanup()
        await relay.stop()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="2D 广场本机演示 (样板, 只听 127.0.0.1)")
    p.add_argument("--relay-port", type=int, default=8801)
    p.add_argument("--web-port", type=int, default=8802)
    p.add_argument("--selftest", action="store_true", help="不开网页, 跑一遍就退出")
    args = p.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(selftest(args) if args.selftest else serve(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""QQ 渠道 (6b) 和 wm-guest 的桌宠自检, 由 qqtest.ps1 调: 这里起替身 OneBot (SnowLuma 的 ws-default 那一侧, 不连真 QQ),
等测试桌宠把面板里的 QQ 设置经 /ws 发给 8794 的 mock 服务, 服务端连上替身以后:
  - 群里 @ 她三次: 草稿卡片自动回答 send / edit:改过的话 / drop (页面带 qqanswers), 看替身收到的消息对不对;
  - 主人私聊: 默认直接发;
  - wm-guest: 访客 token 写 model wm-pet 也按 wm-guest 走、/v1/models 只有 wm-guest、/mcp 回 403; 主人 token 也能用 wm-guest;
  - 宿主日志里有 QQ 渠道的状态 (页面报回宿主).
"""
import argparse
import asyncio
import json
import os
import sys
import threading
import time

import httpx
from aiohttp import web

from api_test import RESULTS, Mcp, check, close_pet, token_path, wait_ready

SELF, OWNER, GROUP = 20002, 10000, 111


class FakeOneBot:
    """在后台线程里跑的替身 OneBot 服务: 认 token, 记下发出去的消息, 能推事件."""

    def __init__(self, port: int, token: str):
        self.port, self.token = port, token
        self.sent, self.calls, self.opened = [], [], 0
        self.ws = None
        self.loop = asyncio.new_event_loop()
        self.mid = 9000

    async def handler(self, request):
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws, self.opened = ws, self.opened + 1
        async for msg in ws:
            d = json.loads(msg.data)
            self.calls.append(d["action"])
            data = {}
            if d["action"] == "get_login_info":
                data = {"user_id": SELF, "nickname": "她的小号"}
            elif d["action"] == "get_status":
                data = {"online": True, "good": True}
            elif d["action"].startswith("send_"):
                self.mid += 1
                self.sent.append((d["action"], d["params"]))
                data = {"message_id": self.mid}
            await ws.send_json({"status": "ok", "retcode": 0, "data": data, "echo": d.get("echo")})
        return ws

    def start(self):
        async def run():
            app = web.Application()
            app.router.add_get("/", self.handler)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", self.port).start()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(run(), self.loop).result(5)

    def push(self, ev: dict):
        asyncio.run_coroutine_threadsafe(self.ws.send_json(ev), self.loop).result(5)

    def texts(self):
        return ["".join(s["data"]["text"] for s in p["message"] if s["type"] == "text") for _, p in self.sent]


def ev(kind, text, mid, uid, at=False):
    segs = ([{"type": "at", "data": {"qq": str(SELF)}}] if at else []) + [{"type": "text", "data": {"text": text}}]
    e = {"post_type": "message", "message_type": kind, "user_id": uid, "self_id": SELF, "message_id": mid,
         "time": int(time.time()), "sender": {"nickname": "测试"}, "message": segs}
    e.update({"group_id": GROUP, "sub_type": "normal"} if kind == "group" else {"sub_type": "friend"})
    return e


def until(pred, seconds=20.0):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


def test_qq(bot: FakeOneBot, log_path: str) -> None:
    check("服务端连上替身 OneBot (面板的 QQ 设置经桌宠页面发到了服务端, token 从 SnowLuma 目录读)",
          until(lambda: "get_login_info" in bot.calls, 30), calls=bot.calls[:5])
    time.sleep(1.0)
    bot.push(ev("group", "在吗", 1, 30001, at=True))
    check("草稿 1: 自动点了'发', 群里收到 (引用 @ 她的那条)", until(lambda: len(bot.sent) >= 1),
          sent=bot.sent[:1])
    if bot.sent:
        action, params = bot.sent[0]
        check("  发到测试群、第一段是引用", action == "send_group_msg" and params.get("group_id") == GROUP
              and params["message"][0] == {"type": "reply", "data": {"id": "1"}}, params=params)
    bot.push(ev("group", "你好呀", 2, 30001, at=True))
    check("草稿 2: 自动'改一下', 发的是改过的话", until(lambda: len(bot.sent) >= 2) and bot.texts()[1] == "改过的话",
          texts=bot.texts())
    bot.push(ev("group", "再说一句", 3, 30001, at=True))
    time.sleep(5)
    check("草稿 3: 自动'不发', 群里没收到", len(bot.sent) == 2, texts=bot.texts())
    bot.push(ev("private", "在干嘛", 4, OWNER))
    check("主人私聊: 直接发", until(lambda: len(bot.sent) >= 3) and bot.sent[2][0] == "send_private_msg", sent=bot.sent[2:])
    text = open(log_path, encoding="utf-8", errors="ignore").read() if os.path.exists(log_path) else ""
    check("宿主日志里有 QQ 渠道的状态 (页面报回宿主)", "QQ 渠道: online" in text)


def test_guest(base: str, owner: str, guest: str) -> None:
    g = {"Authorization": f"Bearer {guest}"}
    r = httpx.get(f"{base}/v1/models", headers=g)
    check("访客 token: /v1/models 只有 wm-guest", r.status_code == 200 and [m["id"] for m in r.json()["data"]] == ["wm-guest"],
          body=r.text[:200])
    r = httpx.post(f"{base}/v1/chat/completions", headers=g, timeout=60,
                   json={"model": "wm-pet", "user": "astrbot:甲", "messages": [{"role": "user", "content": "你好"}]})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    check("访客 token 写 model wm-pet: 按 wm-guest 回答", r.status_code == 200 and body.get("model") == "wm-guest"
          and isinstance(body.get("choices", [{}])[0].get("message", {}).get("content"), str), status=r.status_code, body=r.text[:300])
    r = httpx.post(f"{base}/mcp", headers={**g, "Accept": "application/json, text/event-stream"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    check("访客 token: /mcp 回 403", r.status_code == 403, status=r.status_code)
    r = httpx.get(f"{base}/v1/models", headers={"Authorization": f"Bearer {owner}"})
    check("主人 token: 三个模型", [m["id"] for m in r.json()["data"]] == ["wm-pet", "wm-text", "wm-guest"], body=r.text[:200])
    with httpx.stream("POST", f"{base}/v1/chat/completions", headers={"Authorization": f"Bearer {owner}"}, timeout=60,
                      json={"model": "wm-guest", "stream": True, "messages": [{"role": "user", "content": "在吗"}]}) as s:
        lines = [l for l in s.iter_lines() if l.startswith("data:")]
    check("主人 token 用 wm-guest (流式)", s.status_code == 200 and lines and lines[-1] == "data: [DONE]", n=len(lines))


def test_astrbot_headers(base: str, owner: str, guest: str, server: str, log_path: str) -> None:
    """AstrBot 那样调 wm-guest: 不带 user, 会话号在 x-astrbot-conversation-id 头里; 人格开着工具时请求里带 tools."""
    def sessions():
        return httpx.get(f"{server}/api/status", timeout=10).json()["channels"]["qq"]["api_sessions"]

    def ask(text, conv=None, user=None, **extra):
        body = {"model": "wm-guest", "messages": [{"role": "system", "content": "AstrBot 的人设"}, {"role": "user", "content": text}],
                **({"user": user} if user else {}), **extra}
        h = {"Authorization": f"Bearer {guest}", **({"x-astrbot-conversation-id": conv} if conv else {})}
        return httpx.post(f"{base}/v1/chat/completions", headers=h, json=body, timeout=90)

    n0 = sessions()
    r = ask("我是甲", conv="conv-a")
    check("AstrBot 会话头: 第一次用 conv-a, 多一个访客会话", r.status_code == 200 and sessions() == n0 + 1, n0=n0, now=sessions())
    ask("还是我", conv="conv-a")
    check("  同一个 conv-a 再来, 还是那个会话", sessions() == n0 + 1, now=sessions())
    ask("我是乙", conv="conv-b")
    check("  conv-b 是另一个会话", sessions() == n0 + 2, now=sessions())
    ask("带了 user", conv="conv-a", user="explicit-user")
    check("  请求里有 user 时按 user 分 (头不用)", sessions() == n0 + 3, now=sessions())
    st, _, res = Mcp(base, owner).tool("pet_state")
    before = (res.get("structuredContent") or {}).get("guest") or {}
    tools = [{"type": "function", "function": {"name": "astrbot_execute_shell", "parameters": {"type": "object", "properties": {}}}}]
    r = ask("帮我执行个命令", conv="conv-a", tools=tools, tool_choice="auto")
    msg = (r.json().get("choices") or [{}])[0] if r.status_code == 200 else {}
    check("带了工具的请求: 照常回答, 回复里没有 tool_calls", r.status_code == 200 and "tool_calls" not in msg.get("message", {})
          and msg.get("finish_reason") == "stop", body=r.text[:300])
    st, _, res = Mcp(base, owner).tool("pet_state")
    after = (res.get("structuredContent") or {}).get("guest") or {}
    check("  pet_state 里 wm-guest 带工具的次数加了 1", after.get("with_tools") == (before.get("with_tools") or 0) + 1
          and after.get("requests") == (before.get("requests") or 0) + 1, before=before, after=after)
    text = open(log_path, encoding="utf-8", errors="ignore").read() if os.path.exists(log_path) else ""
    check("  宿主日志记了一笔", "wm-guest 请求里带了 1 个工具" in text)


def test_own(server: str) -> None:
    """面板里"她自己开口"的设置 (proactive、qzone) 经宿主、页面到了服务端: 服务端排出了今天说说的时间."""
    own = {}
    for _ in range(20):
        own = httpx.get(f"{server}/api/status", timeout=10).json()["channels"]["qq"].get("own") or {}
        if own.get("qzone_next"):
            break
        time.sleep(0.5)
    check("她自己开口的设置到了服务端 (排出了下一条说说的时间, 还没到点)", bool(own.get("qzone_next"))
          and own.get("proactive_today") == 0, own=own)


def test_logs(server: str, vault: str) -> None:
    """渠道日志落在临时库的 外部渠道/ 下 (主人记忆的检索排除), wm-guest 按会话分文件夹; 状态里有今天的计数."""
    api = httpx.get(f"{server}/api/status", timeout=10).json()["channels"]["qq"]["api"]
    root = os.path.join(vault, "外部渠道")
    check("服务端状态里有 wm-guest 今天的计数和日志位置", api["requests"] >= 7 and api["sent"] >= 1
          and os.path.normpath(api["log_dir"] or "") == os.path.normpath(os.path.join(root, "api")), api=api)

    def texts(pattern):
        import glob
        return {p: open(p, encoding="utf-8").read() for p in glob.glob(os.path.join(root, *pattern))}
    a = texts(("api", "astrbot_conv-a-*", "日志", "*.md"))
    b = texts(("api", "astrbot_conv-b-*", "日志", "*.md"))
    check("wm-guest 的日志按 AstrBot 会话分文件夹", len(a) == 1 and len(b) == 1
          and "我是甲" in "".join(a.values()) and "我是甲" not in "".join(b.values()) and "我是乙" in "".join(b.values()),
          a=list(a), b=list(b))
    q = texts(("qq", "群111", "日志", "*.md"))
    check("QQ 群的日志也在 外部渠道/qq 下", len(q) == 1 and "在吗" in "".join(q.values()), files=list(q))
    owner_logs = texts(("..", "小流", "日志", "*.md"))
    check("主人的日志里没有别人的话 (这次没和她说话, 应该是空的)", not owner_logs, files=list(owner_logs))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8795, help="测试桌宠的本机接口")
    ap.add_argument("--server", type=int, default=8794, help="mock 服务 (查访客会话数)")
    ap.add_argument("--vault", default="", help="mock 服务用的临时记忆库 (查渠道日志)")
    ap.add_argument("--onebot", type=int, default=3911)
    ap.add_argument("--token", default="qq-selftest-token-123456")
    ap.add_argument("--log", default="")
    ap.add_argument("--pet", type=int, default=0, help="测完关掉这个进程号的测试桌宠")
    ap.add_argument("--wait", type=float, default=90)
    args = ap.parse_args()
    bot = FakeOneBot(args.onebot, args.token)
    bot.start()
    base = f"http://127.0.0.1:{args.port}"
    try:
        if not until(lambda: os.path.exists(token_path()), 30):
            check("本机接口的 token 文件", False)
            return 1
        owner = open(token_path(), encoding="utf-8").read().strip()
        if not check("桌宠页面就绪 (角色加载完、连上 mock 服务)", wait_ready(base, owner, args.wait)):
            return 1
        guest_path = os.path.join(os.environ["APPDATA"], "WorldModel", "pet-api-guest-token.txt")
        guest = open(guest_path, encoding="utf-8").read().strip()
        check("访客 token 和主人 token 不一样", len(guest) >= 32 and guest != owner)
        for t, a in ((test_qq, (bot, args.log)), (test_guest, (base, owner, guest)),
                     (test_astrbot_headers, (base, owner, guest, f"http://127.0.0.1:{args.server}", args.log)),
                     (test_own, (f"http://127.0.0.1:{args.server}",)),
                     (test_logs, (f"http://127.0.0.1:{args.server}", args.vault))):
            try:
                t(*a)
            except Exception as e:  # 一组出错不挡后面的
                check(f"{t.__name__} 出错", False, error=f"{type(e).__name__}: {e}")
    finally:
        if args.pet:
            close_pet(args.pet)
    failed = [r["name"] for r in RESULTS if not r["ok"]]
    print(json.dumps({"total": len(RESULTS), "failed": failed}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

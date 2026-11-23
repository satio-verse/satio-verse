"""广场测试的替身和小工具 (世界1). 只在 pytest 里用.

load_relay / load_client: 按路径载入 tools/plaza_relay.py 和 plugins/plaza-2d/ 里的模块 (目录名带连字符, 不能直接 import).
ScriptLLM:   按脚本说话的假模型; 记下每次收到的整段消息.
FakePlugin:  进程内的假插件桥 (aiohttp), 测宿主收到什么、发出什么.
RelayRig:    起一个真中继 (随机端口) + 真的插件进程 + WorldHost (假模型), 测完收拾干净.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import aiohttp
import psutil
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "plaza-2d"
SKIP_PROC = os.environ.get("PLAZA_SKIP_PROC") == "1"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_relay():
    return sys.modules.get("plaza_relay_mod") or _load("plaza_relay_mod", ROOT / "tools" / "plaza_relay.py")


def load_perception_mod():
    if str(PLUGIN) not in sys.path:
        sys.path.insert(0, str(PLUGIN))
    import plaza_perception
    return plaza_perception


def load_manifest() -> dict:
    return json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))


def load_spec():
    from wm.world.envelope import WorldSpec
    return WorldSpec.from_manifest(load_manifest())


async def until(pred, timeout: float = 8.0, step: float = 0.02, what: str = "条件") -> None:
    t = 0.0
    while not pred():
        await asyncio.sleep(step)
        t += step
        if t > timeout:
            raise AssertionError(f"等不到: {what}")


class ScriptLLM:
    """假模型: rules 是 [(触发的子串, 动作列表或函数)], 按顺序第一条在最后一条用户消息里找到子串的触发, 每条规则触发后用掉.
    没有命中就回空动作. 记下每次收到的整段 messages."""

    def __init__(self, rules=(), default=None):
        self.rules = [list(r) + [False] for r in rules]
        self.default = default or []
        self.calls: list[list[dict]] = []

    async def stream(self, messages, schema=None, max_tokens=0, id_slot=None, meta=None, **kw):
        assert "tools" not in kw, "世界会话不许带工具"
        self.calls.append(messages)
        user = messages[-1]["content"]
        actions = self.default
        for r in self.rules:
            if not r[2] and r[0] in user:
                r[2] = True
                actions = r[1](user) if callable(r[1]) else r[1]
                break
        yield json.dumps({"actions": actions}, ensure_ascii=False)

    def all_text(self) -> str:
        return json.dumps(self.calls, ensure_ascii=False)


def say(text):
    return {"name": "plaza_say", "args": {"text": text}}


def move(d):
    return {"name": "plaza_move", "args": {"direction": d}}


class FakePlugin:
    """进程内的假插件桥: 宿主连上来, 测试读它收到的帧、给宿主发帧."""

    def __init__(self, token="tok"):
        self.token = token
        self.got: list[dict] = []
        self.ws: web.WebSocketResponse | None = None
        self.runner: web.AppRunner | None = None
        self.port = 0
        self.ack_leave = False

    async def start(self) -> int:
        app = web.Application()
        app.router.add_get("/host", self._host)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.port = self.runner.addresses[0][1]
        return self.port

    async def _host(self, request):
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                f = json.loads(msg.data)
                self.got.append(f)
                if f.get("t") == "leave" and self.ack_leave:
                    await ws.send_str(json.dumps({"t": "left"}))
        return ws

    async def send(self, frame: dict) -> None:
        await until(lambda: self.ws is not None, what="宿主连上桥")
        await self.ws.send_str(json.dumps(frame, ensure_ascii=False))

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()


def kill_check(pids) -> list[int]:
    """还活着的进程号 (逐个按号查, 不扫全系统)."""
    alive = []
    for pid in pids:
        with contextlib.suppress(Exception):
            p = psutil.Process(pid)
            if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                alive.append(pid)
    return alive


def tree_pids(pid: int) -> list[int]:
    with contextlib.suppress(Exception):
        p = psutil.Process(pid)
        return [p.pid, *[c.pid for c in p.children(recursive=True)]]
    return [pid]


class Pet:
    """一只宠物: 世界会话 + 宿主 (+ 真插件进程)."""

    def __init__(self, name: str, llm, relay_port: int, *, code: str = "", create: bool = False, persona: str = "",
                 backoff="0.1,0.3,0.5"):
        from wm.world.envelope import Audit
        from wm.world.host import WorldHost
        from wm.world.session import WorldSession
        self.name, self.llm = name, llm
        self.spec = load_spec()
        self.audit = Audit()
        self.session = WorldSession(self.spec, name, llm, audit=self.audit, persona=persona,
                                    env_prompt=(PLUGIN / "ENV_PROMPT.md").read_text(encoding="utf-8"), batch_s=0.05)
        self.host = WorldHost(self.spec, self.session, audit=self.audit)
        self.relay_port, self.code, self.create = relay_port, code, create
        self.backoff = backoff
        self.pids: list[int] = []

    async def start(self) -> None:
        from wm.world.host import plugin_env
        env = plugin_env(None, WM_PLUGIN_TOKEN="tok-" + self.name, WM_PLUGIN_PORT=0, PLAZA_NAME=self.name,
                         PLAZA_RELAY=f"ws://127.0.0.1:{self.relay_port}/plaza", PLAZA_BACKOFF=self.backoff,
                         **({"PLAZA_CODE": self.code} if self.code else {"PLAZA_CREATE": "1"}))
        await self.host.launch([sys.executable, str(PLUGIN / "relay_client.py")], env, "tok-" + self.name, cwd=str(PLUGIN))
        self.pids = tree_pids(self.host.pid)

    async def wait_in_room(self) -> str:
        await until(lambda: self.host.panel and self.host.panel.get("code") and self.host.panel.get("state") == "in_room",
                    what=f"{self.name} 进房间")
        return self.host.panel["code"]

    def types(self) -> list[str]:
        return [i["type"] for i in self.host.log if i["k"] == "in"]

    def said(self) -> list[str]:
        return [i["args"]["text"] for i in self.host.log if i["k"] == "act" and i["name"] == "plaza_say"]

    def heard(self) -> list[str]:
        return [i["text"] for i in self.host.log if i["k"] == "in" and i["type"] == "pet_said"]

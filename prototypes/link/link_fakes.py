"""测试用的替身: 假模型 (不连云端、不花钱) 和一台测试"电脑"上的服务端 (pytest 起的, 随机端口, 临时记忆库).

只在 pytest 里用 (见 conftest.py 开头的说明): PetHost 会 create_app, 隔离靠 conftest 搬来的那些 fixture.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections import deque
from pathlib import Path

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from test_pipeline import make_cfg  # 项目测试的基础配置: 本机模型、语音合成都是 mock, 不开识别和表情, 日志进临时目录
from wm.app import ENGINES, create_app

KEY = "sk-test-link-0000000000"
RELAY = Path(__file__).with_name("link_relay.py")


async def until(pred, timeout: float = 8.0, step: float = 0.02, what: str = "条件") -> None:
    t = 0.0
    while not pred():
        await asyncio.sleep(step)
        t += step
        if t > timeout:
            raise AssertionError(f"等不到: {what}")


class FakeModel:
    """假的 OpenAI 兼容服务: 记下每次请求的正文; 回话按 script 里排的来, 排完了回"某某说的第 n 句" (每句都不一样)."""

    def __init__(self, name: str):
        self.name = name
        self.requests: list[dict] = []
        self.script: deque[str] = deque()
        self.server: TestServer | None = None

    async def chat(self, request: web.Request):
        body = await request.json()
        self.requests.append(body)
        text = self.script.popleft() if self.script else f"{self.name}说的第{len(self.requests)}句"
        content = json.dumps({"emo": "happy", "reply": text}, ensure_ascii=False)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8"})
        await resp.prepare(request)
        chunks = [{"delta": {"role": "assistant", "content": ""}}]
        chunks += [{"delta": {"content": content[i:i + 8]}} for i in range(0, len(content), 8)]
        chunks += [{"delta": {}, "finish_reason": "stop"}]
        for c in chunks:
            frame = {"id": "x", "object": "chat.completion.chunk", "choices": [{"index": 0, "finish_reason": None, **c}]}
            await resp.write(b"data: " + json.dumps(frame, ensure_ascii=False).encode() + b"\n\n")
        usage = {"id": "x", "object": "chat.completion.chunk", "choices": [],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        await resp.write(b"data: " + json.dumps(usage).encode() + b"\n\ndata: [DONE]\n\n")
        return resp

    async def models(self, request: web.Request):
        return web.json_response({"data": [{"id": "fake-chat"}]})

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.chat)
        app.router.add_get("/v1/models", self.models)
        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("/v1"))

    async def close(self) -> None:
        if self.server is not None:
            await self.server.close()

    def bodies(self) -> list[str]:
        return [json.dumps(b, ensure_ascii=False) for b in self.requests]


class Inbox:
    """主人那条连接 (/ws) 收到的 JSON, 后台一直收着 (不收的话服务端往下发的状态会堵住)."""

    def __init__(self, ws):
        self.ws, self.all = ws, []
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            msg = await self.ws.receive()
            if msg.type == WSMsgType.TEXT:
                self.all.append(json.loads(msg.data))
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                return

    async def get(self, kind: str, pred=lambda m: True, timeout: float = 8.0) -> dict:
        def hits():
            return [m for m in self.all if m.get("type") == kind and pred(m)]
        await until(lambda: bool(hits()), timeout, what=f"主人连接上的 {kind}")
        return hits()[0]


def secrets_of(tag: str) -> dict:
    """一台"电脑"上主人的假隐私 (每台不一样). 测试查这些字串有没有流出去."""
    return {"name": f"阿青Name{tag}9", "home": f"青柠镇Home{tag}7", "pref": f"Pref{tag}5", "work": f"橙子{tag}科技",
            "birthday": {"A": "12月29日", "B": "11月30日"}.get(tag, "10月31日"),
            "hist": f"Hist{tag}4", "title": f"Title{tag}3"}


def make_vault(root: Path, s: dict) -> Path:
    """临时记忆库: 关于用户.md 里是程序记下的几条事实 (住处、生日、单位会被外发检查认成隐私)."""
    mem = root / "vault" / "小流" / "记忆"
    mem.mkdir(parents=True)
    (mem / "关于用户.md").write_text(
        "# 关于用户\n\n"
        f"- 用户叫{s['name']} (2026-10-01) #名字 #auto-memory\n"
        f"- 用户住在{s['home']} (2026-10-01) #事实 #auto-memory\n"
        f"- 用户的生日是{s['birthday']} (2026-10-01) #事实 #auto-memory\n"
        f"- 用户喜欢{s['pref']} (2026-10-01) #偏好 #auto-memory\n"
        f"- 用户在{s['work']}上班 (2026-10-01) #事实 #auto-memory\n", encoding="utf-8")
    return root / "vault"


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class PetHost:
    """一台测试"电脑"上的服务端: 假模型 + pytest 起的服务端 + 主人的 /ws 连接. 访客入口是 guest_url."""

    def __init__(self, tag: str, pet_name: str):
        self.tag, self.pet_name = tag, pet_name
        self.secrets = secrets_of(tag)
        self.model = FakeModel(pet_name)

    async def start(self, tmp: Path, per_hour: int = 0) -> "PetHost":
        tmp.mkdir(parents=True, exist_ok=True)
        base = await self.model.start()
        self.vault = make_vault(tmp, self.secrets)
        # agent.home: 工作台的数据目录一个进程里只能有一个调度器占着, 两台"电脑"各用各的临时目录
        cfg = make_cfg(tmp / "logs", "memory.embed.engine=none", "memory.graph_notes=false",
                       f"memory.vault={json.dumps(str(self.vault))}", f"agent.home={json.dumps(str(tmp / 'agent'))}")
        self.client = TestClient(TestServer(create_app(cfg)))
        await self.client.start_server()
        self.app = self.client.server.app
        self.port = self.client.server.port
        self.owner = await self.client.ws_connect("/ws")
        self.inbox = Inbox(self.owner)
        await self.inbox.get("hello")
        # 外部渠道的模型换成假模型 (QQ 关着也能换, /guest 用的就是它); 每小时句数按测试要的设
        await self.owner.send_json({"type": "channel_config", "private": {"min_gap_s": 0, "per_hour": per_hour},
                                    "model": {"provider": "custom", "base_url": base, "api_key": KEY, "model": "fake-chat"}})
        done = await self.inbox.get("channel_config_done")
        assert done["ok"], done
        self.guest_url = f"ws://127.0.0.1:{self.port}/guest"
        return self

    async def plant_owner_context(self) -> None:
        """主人在自己的会话里说一句话、报一个正在用的程序: 这两样也不该流到访客那边."""
        s = self.secrets
        await self.owner.send_json({"type": "output", "audio": False})
        await self.owner.send_json({"type": "state", "state": {
            "place": "desktop", "activity": {"category": "code", "app": s["title"], "minutes": 30}}})
        await self.owner.send_json({"type": "text", "text": f"我在写{s['hist']}的代码"})
        await self.inbox.get("trace")
        await asyncio.sleep(0.3)

    def api(self) -> dict:
        """服务端自己记的访客入口计数: 几句、说了几句、拦下几句、限频几句."""
        return self.app[ENGINES].channels.status()["api"]

    def snapshot(self) -> dict:
        return snapshot(self.vault)

    def canaries(self) -> list[str]:
        return list(self.secrets.values())

    async def close(self) -> None:
        await self.client.close()
        await self.model.close()


class RelayProcess:
    """另起一个进程跑中继 (测"进程被杀再起来"用). 账号直接往同一个 SQLite 文件里建."""

    def __init__(self, db: Path, port: int, frame_log: Path, per_min: int = 20):
        self.db, self.port, self.frame_log, self.per_min = db, port, frame_log, per_min
        self.proc: subprocess.Popen | None = None

    async def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-B", str(RELAY), "--db", str(self.db), "--port", str(self.port),
             "--per-min", str(self.per_min), "--frame-log", str(self.frame_log)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        line = await asyncio.get_running_loop().run_in_executor(None, self.proc.stdout.readline)
        if not line.strip():
            raise AssertionError("中继进程没起来: " + self.proc.stderr.read()[-800:])
        assert json.loads(line)["port"] == self.port

    def kill(self) -> None:
        """不打招呼直接杀掉."""
        self.proc.kill()
        self.proc.wait(10)
        for pipe in (self.proc.stdout, self.proc.stderr):
            pipe.close()

    def frames(self) -> str:
        return self.frame_log.read_text(encoding="utf-8") if self.frame_log.exists() else ""

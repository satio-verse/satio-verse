"""下载队列在工作台这边 (wm/agent/downloads.py、downloads_http.py): 她调七个工具走真的 _tool, 模型换成脚本, 假下载站在本机.
下载服务不另起进程: 用它那个类 (daemon.Service) 的同一个分发函数, 在测试进程里直接调 (进程那一层在 test_downloads_service.py 里测).
验收里的: 没点头时传输量为 0、点了才开始 (完全授权档也一样); 别的会话看得到停不了; 下完发起的会话收到回报."""
import asyncio
import json
import time
import types

import pytest

from dl_fakes import FakeSite, blob, sha256
from wm import downloads as DQ
from wm.agent import downloads as DL
from wm.agent import downloads_http, studio as ST
from wm.agent.core import AgentHub, Session
from wm.downloads import DownloadError, Limits
from wm.downloads import daemon as D

FAST = dict(retry_delays=(0.02, 0.02), checkpoint_s=0.02, read_timeout_s=3.0)


class FakeLLM:
    provider = "custom"

    async def close(self):
        pass


class FakeChannels:
    """渠道管理器的替身: 记下工作台弹到桌宠对话栏的卡片 (真的那个 15 秒没人点会上 QQ)."""
    conf = {}

    def __init__(self):
        self.asks, self.closed = [], []

    async def agent_ask(self, key, title, text, on_answer):
        self.asks.append({"key": key, "title": title, "text": text, "answer": on_answer})
        return f"d{len(self.asks)}"

    async def agent_ask_done(self, did, how):
        self.closed.append((did, how))

    async def agent_doing(self, text):
        pass


class LocalClient:
    """宿主找下载服务的那个 Client 的替身: 不走本机接口, 直接调 Service.call."""

    def __init__(self, home, root, allow, **limits):
        self.home, self.root = home, root
        self.svc = D.Service(home, root, Limits(**{**FAST, **limits}), frozenset(allow), 9999)
        self.started = False
        self.calls = []

    async def rpc(self, op, timeout=15.0, **body):
        if not self.started:
            self.started = True
            await self.svc.queue.start()
        self.calls.append(op)
        return {"ok": True, **(await self.svc.call(op, json.loads(json.dumps(body))))}  # 过一遍 JSON, 和真的一样

    def state(self):
        return {"running": self.started, "port": 0, "pid": 0, "watch": {"running": False, "state": ""},
                "summary": DQ.summary(self.home), "dir": str(self.root)}

    async def close(self):
        await self.svc.queue.close()


class Stage:
    def __init__(self, tmp_path, script, mode="workspace", rate=0):
        self.tmp, self.script, self.mode, self.rate = tmp_path, script, mode, rate
        self.seen, self.events = [], []

    async def __aenter__(self):
        self.site = FakeSite(rate=self.rate)
        await self.site.start()
        self.ws = self.tmp / "workspace"
        self.ws.mkdir(exist_ok=True)
        self.channels = FakeChannels()
        self.hub = self.new_hub()
        self.client = self.hub.downloads.client = LocalClient(DQ.home_for(self.hub.home), self.tmp / "files",
                                                              {f"127.0.0.1:{self.site.port}"})
        self.s = self.session("dl-a", self.mode)
        self.hub.downloads.start()
        return self

    def new_hub(self):
        hub = AgentHub({"agent": {"workspace": str(self.ws)}, "downloads": {"dir": str(self.tmp / "files")}},
                       engines=types.SimpleNamespace(channels=self.channels, memory=None), home=self.tmp / "agent")
        hub._llm = lambda _s: FakeLLM()
        hub.downloads.grace = 0  # 测试里模型是换掉的 (hub.models() 是空的): 回报不用等模型登记

        async def ask_model(sess, llm, tools):
            self.seen.append(sess.request_messages()[1:])
            out = self.script(sess, len(self.seen))
            out = await out if asyncio.iscoroutine(out) else out
            return {"content": out.get("content", ""), "tool_calls": out.get("tool_calls", []), "reasoning": ""}
        hub._ask_model = ask_model
        return hub

    def session(self, sid, mode="workspace", hub=None):
        hub = hub or self.hub
        s = Session(sid, str(self.ws), mode=mode, model="offline-test")
        hub.sessions[s.id] = s
        hub.save(s)

        async def sink(ev):
            self.events.append(ev)
        hub.subscribe(s.id, sink)
        return s

    async def __aexit__(self, *exc):
        await self.hub.downloads.close()
        await self.site.stop()

    async def turn(self, text, s=None):
        s = s or self.s
        assert await self.hub.send(s.id, text) is None
        await until(lambda: s.task is not None)
        await s.task

    async def tool(self, s, tool, /, **args):
        """像模型那样调一个工具 (走真的 _tool: 权限、黑名单、卡片都过)."""
        return await self.hub._tool(s, {"id": f"t{len(self.events)}", "function": {"name": tool, "arguments": json.dumps(args)}},
                                    self.hub._offered(s))

    def of(self, kind, s=None):
        return [e for e in self.events if e["type"] == kind and (s is None or e.get("session") == s.id)]

    def jobs(self):
        return self.client.svc.queue.jobs


def enqueue(url, n=1, **more):
    return {"content": "先排进下载队列", "tool_calls": [{"id": f"c{n}", "name": "download_enqueue", "args": json.dumps(
        {"urls": [url], "name": "测试模型 v1", "size": more.pop("size", None), "license": "MIT", **more})}]}


async def until(pred, timeout=8.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等不到")


def run(main):
    return asyncio.run(main())


# ---------------------------------------------------------------- 验收 1、6: 没点头不传; 点了才开始; 下完回到发起的会话
def test_enqueue_only_registers_then_the_nod_starts_it_and_the_result_comes_back(tmp_path):
    data = blob(200_000, 3)
    box = {}

    def script(s, n):
        if n == 1:
            return enqueue(box["url"], sha256=sha256(data), size=len(data))
        return {"content": "排好了，等你点头。" if n == 2 else "模型下好了，哈希核对过。"}

    async def main():
        async with Stage(tmp_path, script) as st:
            box["url"] = st.site.add("model.bin", data)
            await st.turn("把那个模型下下来")
            result = json.loads(st.seen[1][-1]["content"].split("\n", 1)[1])  # 她拿到的工具结果: 马上返回编号
            assert result["id"] == "D1" and result["state"] == "awaiting" and "等主人点头" in result["note"]
            asks = st.of("ask")
            assert len(asks) == 1 and asks[0]["label"] == "下载文件" and asks[0]["risk"] == "power" and asks[0]["turn_ok"] is False
            for piece in ("名称：测试模型 v1", f"来源：127.0.0.1:{st.site.port}", "大小：195.3 KB", "许可：MIT", "有 SHA-256"):
                assert piece in asks[0]["text"]
            card = asks[0]["id"]
            assert any(e["type"] == "tool" and e["id"] == card and e["state"] == "asking" for e in st.events)  # 页面上卡片挂在这一步
            row = next(e for e in st.events if e["type"] == "tool" and e["name"] == "download_enqueue")
            assert row["label"] == "排下载" and row["args"] == f"测试模型 v1 ← 127.0.0.1:{st.site.port}"  # 这一行写名称和来源, 不只是"排下载"
            assert st.channels.asks[0]["title"] == "工作台想下载文件" and len(st.channels.asks[0]["text"]) <= 300  # 桌宠也弹, 够短能上 QQ
            await asyncio.sleep(0.3)
            assert st.site.requests == [] and st.site.sent == 0 and st.s.status == "idle"  # 那一轮做完了, 没点头: 一个请求都没发
            assert st.hub.answer(st.s.id, card, "once") is False  # 无界面模式按 --ask 自动回的 (没说是页面): 不算你点头
            await asyncio.sleep(0.2)
            assert st.jobs()["D1"]["state"] == "awaiting" and st.site.requests == []
            assert st.hub.answer(st.s.id, card, "once", via="page") is True  # 你在页面上点"允许一次"
            await until(lambda: len(st.seen) == 3 and st.s.status == "idle")  # 下完了, 她自己接着做一轮
            back = st.seen[2][-1]["content"]
            assert back.startswith("[下载有结果了 · D1]") and "下完了" in back and "SHA-256 对上了" in back
            assert "这件下载是你上一轮做「把那个模型下下来」时启动的" in back and "download_status(id='D1')" in back and "job_output" not in back
            assert (tmp_path / "files" / "model.bin").read_bytes() == data
            done = st.of("job_done")
            assert [d["delivered"] for d in done] == ["continue"] and done[0]["job"]["how"] == "done"
            assert [(e["id"], e["how"]) for e in st.of("ask_done")] == [(card, "once")]
            assert st.channels.closed == [("d1", "once")]  # 桌宠上那张也收了
            assert st.jobs()["D1"]["report"] == "acked" and st.jobs()["D1"]["approvals"][0]["via"] == "page"
            assert [u["text"] for u in st.of("user")] == ["把那个模型下下来"]  # 回报不算你说的话
    run(main)


def test_full_authorization_still_asks_and_a_refusal_is_reported(tmp_path):
    box = {}

    def script(s, n):
        return enqueue(box["url"]) if n == 1 else {"content": "排好了。" if n == 2 else "好的，不下了。"}

    async def main():
        async with Stage(tmp_path, script, mode="unrestricted") as st:  # 完全授权档
            box["url"] = st.site.add("a.bin", blob(5000))
            await st.turn("下这个")
            assert st.jobs()["D1"]["state"] == "awaiting" and len(st.of("ask")) == 1  # 照样弹卡片
            assert "大小：未知" in st.of("ask")[0]["text"]
            await asyncio.sleep(0.2)
            assert st.site.requests == []
            st.channels.asks[0]["answer"](False)  # 你在桌宠 / QQ 上回"不用"
            await until(lambda: len(st.seen) == 3 and st.s.status == "idle")
            assert st.jobs()["D1"]["state"] == "denied" and st.jobs()["D1"]["approvals"][0]["via"] == "pet"
            assert "主人没同意下载 D1" in st.seen[2][-1]["content"] and "别换个办法自己去下" in st.seen[2][-1]["content"]
            assert st.site.requests == [] and st.of("ask_done")[0]["how"] == "deny"
    run(main)


# ---------------------------------------------------------------- 验收 4: 别的会话看得到但停不了
def test_another_session_sees_the_download_but_cannot_stop_it(tmp_path):
    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "好"}, rate=80_000) as st:
            other = st.session("dl-b")
            url = st.site.add("slow.bin", blob(400_000))
            text, err = await st.tool(st.s, "download_enqueue", urls=[url], name="慢慢下")
            assert not err and json.loads(text)["id"] == "D1"
            assert st.hub.answer(st.s.id, st.of("ask")[0]["id"], "once", via="page")
            await until(lambda: st.jobs()["D1"]["done"] > 0)
            text, err = await st.tool(other, "download_status")
            seen = json.loads(text)["jobs"]
            assert not err and [(j["id"], j["state"], j["mine"]) for j in seen] == [("D1", "running", False)] and seen[0]["by"]
            for name in ("download_pause", "download_cancel", "download_resume"):
                text, err = await st.tool(other, name, id="D1")
                assert err and "别的会话排的" in text
            assert st.jobs()["D1"]["state"] == "running"
            text, err = await st.tool(other, "download_enqueue", urls=[url], name="我也要")  # 同一个文件: 不再排一个
            got = json.loads(text)
            assert not err and got["id"] == "D1" and got["deduplicated"] and "停不了" in got["note"] and len(st.of("ask")) == 1
            text, err = await st.tool(other, "download_wait", id="D1", timeout_s=0)  # 看、等都可以
            assert not err and json.loads(text)["state"] == "running" and "还没下完" in json.loads(text)["note"]
            text, err = await st.tool(st.s, "download_pause", id="D1")  # 发起的会话可以
            assert not err and json.loads(text)["state"] == "paused"
            text, err = await st.tool(st.s, "download_cancel", id="D1")
            assert not err and json.loads(text)["state"] == "cancelled" and not (tmp_path / "files" / "slow.bin.part").exists()
            await asyncio.sleep(0.2)
            assert st.of("job_done") == []  # 她自己取消的不用再回报
    run(main)


# ---------------------------------------------------------------- 验收 5 (工具这一层): 不收的直接说, 不弹卡片
def test_refused_requests_never_reach_a_card(tmp_path):
    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "好"}) as st:
            for args, word in (({"urls": ["http://127.0.0.1:9/x.bin"]}, "只下公网"), ({"urls": ["http://192.168.0.3/x.bin"]}, "只下公网"),
                               ({"urls": ["ftp://example.com/x"]}, "http / https"),
                               ({"urls": [st.site.url("a.bin")], "dest": "..\\..\\out.bin"}, "不能当文件名"),
                               ({"urls": [st.site.url("a.bin")], "dest": "C:\\Windows\\x.bin"}, "相对路径"),
                               ({"urls": [st.site.url("a.bin")], "sha256": "123"}, "64 位十六进制"),
                               ({"urls": [st.site.url("a.bin")], "redirect_hosts": ["10.0.0.1"]}, "只下公网")):
                text, err = await st.tool(st.s, "download_enqueue", name="x", **args)
                assert err and word in text, (args, text)
            assert st.of("ask") == [] and st.channels.asks == [] and st.jobs() == {} and st.site.requests == []
            text, err = await st.tool(st.s, "download_status", id="D7")
            assert err and "没有编号是 D7" in text
            st.hub.blacklist.add({"kind": "domain", "value": "127.0.0.1", "reason": "测试: 这个域名不许碰"}, by="user")
            text, err = await st.tool(st.s, "download_enqueue", urls=[st.site.url("a.bin")], name="x")
            assert err and "黑名单" in text and st.jobs() == {}  # 你拉黑的域名: 连登记都不让
    run(main)


def test_read_only_sessions_can_look_but_not_queue_and_deleting_files_asks(tmp_path):
    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "好"}) as st:
            ro = st.session("dl-ro", mode="read")
            offered = st.hub._offered(ro)
            assert {"download_status", "download_wait"} <= offered and not ({"download_enqueue", "download_cancel", "download_cleanup"} & offered)
            assert DL.NAMES <= st.hub._offered(st.s)
            d = DL.decision(st.hub, st.s, "download_cleanup", {"scope": "all"})  # 连下好的文件一起删: 和别的删东西一样先问
            assert d.how == "ask" and d.risk == "power"
            assert DL.decision(st.hub, st.s, "download_cleanup", {}).how == "auto"
            assert DL.decision(st.hub, st.s, "download_enqueue", {"urls": ["https://example.com/a.bin"], "name": "x"}).how == "auto"
            text, err = await st.tool(st.s, "download_cleanup")
            assert not err and json.loads(text)["scope"] == "allWithoutFiles"
    run(main)


# ---------------------------------------------------------------- 下到一半又要问; 服务端重启以后卡片照样能点、结果照样交回
def test_redirect_card_and_host_restart(tmp_path):
    data = blob(60_000)

    async def main():
        cdn = FakeSite()
        await cdn.start()
        try:
            async with Stage(tmp_path, lambda s, n: {"content": "好的。"}) as st:
                st.client.svc.queue.allow = frozenset({f"127.0.0.1:{st.site.port}", f"127.0.0.1:{cdn.port}"})
                url = st.site.add("go.bin", b"", redirect=cdn.add("real.bin", data))
                text, err = await st.tool(st.s, "download_enqueue", urls=[url], name="会跳转的")
                assert not err
                first = st.of("ask")[0]["id"]
                assert st.hub.answer(st.s.id, first, "once", via="page")
                await until(lambda: len(st.of("ask")) == 2)  # 要转去没列的来源: 又弹一张
                second = st.of("ask")[1]
                assert second["id"] != first and "卡片上没列的来源" in second["text"] and f"127.0.0.1:{cdn.port}" in second["text"]
                assert cdn.requests == [] and len(st.channels.asks) == 2 and st.s.unread is True
                # 这时候 8790 重启了: 新的工作台, 下载服务还是那一个
                st.hub.downloads.task.cancel()
                st.hub.save(st.s)
                hub2 = st.new_hub()
                hub2.downloads.client = st.client
                hub2.downloads.start()
                await until(lambda: len(st.channels.asks) == 3)  # 桌宠上的卡片重新弹 (旧进程的没了)
                s2 = hub2.get(st.s.id)
                assert len([e for e in s2.events if e["type"] == "ask"]) == 2  # 会话里那张还在, 没有重放
                assert hub2.answer(st.s.id, second["id"], "once", via="page") is True  # 照样能点 (认卡片编号)
                await until(lambda: st.jobs()["D1"]["state"] == "done" and st.jobs()["D1"]["report"] == "acked")
                await until(lambda: any(e["type"] == "job_done" for e in s2.events))
                note = next(e for e in s2.events if e["type"] == "job_done")
                assert "下完了" in note["text"] and note["delivered"] == "note"  # 那一轮早就不在了: 记一笔, 下次你说话时带给她
                assert (tmp_path / "files" / "go.bin").read_bytes() == data and len(cdn.requests) == 1
                assert any("download_status(id='D1')" in n for n in s2.job_notes)
                hub2.downloads.task.cancel()
        finally:
            await cdn.stop()
    run(main)


def test_child_session_downloads_belong_to_the_top_session(tmp_path):
    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "好"}) as st:
            child = st.session("dl-child", mode="workspace")
            child.parent_id, child.depth = st.s.id, 1
            assert st.hub.downloads.root_session(child) is st.s
            got = await st.hub.downloads.enqueue(child, {"urls": [st.site.add("c.bin", blob(100))], "name": "子任务要的"})
            job = st.jobs()[got["id"]]
            assert job["owner"] == st.s.id and job["origin"] == child.id
            assert [e.get("session") for e in st.of("ask")] == [st.s.id]  # 卡片放在最上面那个会话里
            with pytest.raises(DownloadError):  # 不相干的会话动不了
                await st.client.rpc("cancel", id="D1", actor={"kind": "session", "id": "dl-x"})
            assert (await st.client.rpc("cancel", id="D1", actor={"kind": "session", "id": child.id}))["job"]["state"] == "cancelled"
    run(main)


def test_stopping_the_turn_ends_download_wait_but_not_the_download(tmp_path):
    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "好"}, rate=60_000) as st:
            text, err = await st.tool(st.s, "download_enqueue", urls=[st.site.add("slow.bin", blob(300_000))], name="慢的")
            assert not err
            assert st.hub.answer(st.s.id, st.of("ask")[0]["id"], "once", via="page")
            await until(lambda: st.jobs()["D1"]["done"] > 0)
            waiting = asyncio.ensure_future(st.tool(st.s, "download_wait", id="D1", timeout_s=120))
            await asyncio.sleep(0.3)
            assert not waiting.done()  # 还在等
            t0 = time.monotonic()
            st.s.stop_event.set()  # 你按了停
            text, err = await asyncio.wait_for(waiting, 5)
            assert err and "主人停掉了" in text and time.monotonic() - t0 < 3
            assert st.jobs()["D1"]["state"] == "running"  # 下载本身不受影响
    run(main)


def test_reports_wait_for_a_model_right_after_the_host_starts(tmp_path):
    """服务端刚起来, 页面、桌宠还没登记模型: 结束了的先不交 (交了的话接着做的那一轮会因为没有模型报错); 模型有了就交;
    一直没有, 等够 grace 秒也交."""
    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "好"}) as st:
            dl = st.hub.downloads
            dl.grace, dl.started = 30.0, time.monotonic()
            await st.tool(st.s, "download_enqueue", urls=[st.site.add("w.bin", blob(5000))], name="等模型")
            assert st.hub.answer(st.s.id, st.of("ask")[0]["id"], "once", via="page")
            await until(lambda: st.jobs()["D1"]["state"] == "done")
            await until(lambda: "D1" in dl.later)
            assert st.of("job_done") == [] and st.jobs()["D1"]["report"] == "pending"  # 先攒着, 还没交
            st.hub.models = lambda: [{"id": "custom|http://127.0.0.1:1/v1|fake", "label": "fake"}]  # 页面连上来登记了模型
            dl.wake.set()
            await until(lambda: st.of("job_done"))
            assert not dl.later and st.jobs()["D1"]["report"] == "acked" and "下完了" in st.of("job_done")[0]["text"]
            await st.tool(st.s, "download_enqueue", urls=[st.site.add("w2.bin", blob(5000))], name="一直没模型")
            st.hub.models = lambda: []
            dl.grace, dl.started = 0.5, time.monotonic()
            assert st.hub.answer(st.s.id, st.of("ask")[1]["id"], "once", via="page")
            await until(lambda: len(st.of("job_done")) == 2)  # 等够了也交
    run(main)


# ---------------------------------------------------------------- 给你用的接口; 清单
def test_owner_api_lists_approves_and_controls_every_session(tmp_path):
    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "好"}, rate=100_000) as st:
            dl = st.hub.downloads
            assert (await dl.overview())["idle"] is True and "overview" not in st.client.calls and st.client.calls == []  # 没活: 不把服务叫起来
            await st.tool(st.s, "download_enqueue", urls=[st.site.add("u.bin", blob(300_000))], name="页面上批的")
            seen = await dl.overview()
            assert [(j["id"], j["state"], j["ask"]["why"]) for j in seen["jobs"]] == [("D1", "awaiting", "new")]
            with pytest.raises(DownloadError):  # 这个接口里没有"点头": 只能在卡片上点
                await dl.user("approve", "D1")
            assert st.jobs()["D1"]["state"] == "awaiting" and st.site.requests == []
            assert st.hub.answer(st.s.id, st.of("ask")[0]["id"], "once", via="page")
            await until(lambda: st.jobs()["D1"]["done"] > 0)
            assert st.of("ask_done")[0]["how"] == "once"
            assert (await dl.user("pause", "D1"))["jobs"][0]["state"] == "paused" and st.jobs()["D1"]["pause_by"] == "user"
            text, err = await st.tool(st.s, "download_resume", id="D1")
            assert err and "主人暂停的" in text  # 你暂停的, 她不能自己接着下
            await dl.user("resume", "D1")
            await dl.user("cancel", "D1")
            await until(lambda: st.of("job_done"))  # 你取消的: 告诉发起的会话 (它没在做事: 记一笔, 下次你说话时带给她)
            assert "被取消了" in st.of("job_done")[0]["text"] and st.of("job_done")[0]["delivered"] == "note"
            got = await dl.user("cleanup", scope="records")
            assert got["cleaned"]["removed_records"] == 1 and got["jobs"] == []
            with pytest.raises(DownloadError):
                await dl.user("explode")
    run(main)


def test_http_route_is_registered_and_guarded(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "好"}) as st:
            app = web.Application()
            downloads_http.register(app, lambda request: None if request.headers.get("X-Test") == "ok" else "不是本机页面", st.hub)
            async with TestClient(TestServer(app)) as c:
                assert (await c.get("/api/agent/downloads")).status == 403
                r = await c.get("/api/agent/downloads", headers={"X-Test": "ok"})
                assert r.status == 200 and (await r.json())["jobs"] == []
                r = await c.post("/api/agent/downloads", headers={"X-Test": "ok"}, json={"action": "cancel", "id": "D9"})
                assert r.status == 400 and (await r.json())["code"] == "not-found"
    run(main)


def test_manifest_declares_exactly_the_tools_she_gets():
    declared = {t["name"]: t["risk"] for t in DL.MANIFEST["plugin"]["tools"]}
    assert set(declared) == DL.NAMES == {"download_enqueue", "download_status", "download_wait", "download_pause",
                                         "download_resume", "download_cancel", "download_cleanup"}
    assert {n for n, risk in declared.items() if risk == "read"} == DL.READ_ONLY
    assert DL.NAMES <= ST.NAMES and DL.READ_ONLY <= ST.READ_ONLY and all(n in ST.LABELS for n in DL.NAMES)
    assert DL.MANIFEST["plugin"]["service"] == {"cmd": ["python", "-m", "wm.downloads", "serve"], "health": "/health", "start": "on-demand"}

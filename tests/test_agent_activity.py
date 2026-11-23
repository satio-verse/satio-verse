"""统一状态 (wm/agent/activity.py; 2026-11-01 用户"好，接着做": 升级方案第 3 步最后一块): 会话这一轮、等你点头的、
排队的、中途补充的、子任务、后台作业、终端里在跑的、你在工作区面板跑的、核对没过的 —— 汇成一份"现在在做什么",
每一项写清在哪个会话、在等什么, 等你的排最前面. 只读."""
import asyncio
import time
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from test_pipeline import make_cfg
from wm.agent import activity as A
from wm.agent.core import AgentHub, Session
from wm.app import create_app


def hub_with(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return AgentHub({"agent": {"workspace": str(ws)}}, home=tmp_path / "agent"), str(ws)


def add(hub, sid, ws, title, **kw):
    s = Session(sid, ws, title=title)
    for k, v in kw.items():
        setattr(s, k, v)
    hub.sessions[sid] = s
    return s


def test_activity_says_what_each_thing_is_waiting_for(tmp_path):
    async def main():
        hub, ws = hub_with(tmp_path)
        loop = asyncio.get_running_loop()
        asking = add(hub, "s-ask", ws, "打包", status="waiting", run={"id": "r1", "started": time.time() - 30})
        asking.asks = {"c1": loop.create_future()}
        asking.events = [{"type": "ask", "id": "c1", "label": "跑命令", "text": "npm run build"}]
        asking.queue = [{"id": "q", "text": "再看看", "materials": [], "created": 1}]
        tool = add(hub, "s-tool", ws, "跑测试", status="running", run={"id": "r2", "started": time.time() - 9})
        tool.events = [{"type": "user", "text": "跑测试"}, {"type": "tool", "id": "t1", "name": "run_command", "label": "跑命令",
                                                           "args": "pytest -q", "state": "running", "t": time.time() - 5}]
        tool.steer = [{"id": "x", "text": "顺便看覆盖率", "created": 1}]
        think = add(hub, "s-think", ws, "写说明", status="running", run={"id": "r3", "started": time.time(), "steps_start": 4})
        think.stats["steps"] = 6
        add(hub, "s-check", ws, "做安装包", run={"id": "r4", "state": "completed", "ended": time.time(), "verification": "unreviewed",
                                                "check": {"verdict": "needs_check", "reasons": ["提到写好的 dist/app.zip 在工作区里找不到"]}})
        add(hub, "s-ok", ws, "验收过了", run={"id": "r5", "state": "completed", "verification": "accepted",
                                             "check": {"verdict": "needs_check", "reasons": ["…"]}})  # 你验收过了: 不再算
        stopped = add(hub, "s-stop", ws, "停下的", queue=[{"id": "q1", "text": "接着做", "materials": [], "created": 2}])
        stopped.stop_event.set()
        hub.teams.list = lambda parent_id=None: [
            {"id": "c-run", "parent_id": "s-tool", "status": "running", "title": "查覆盖率", "created": time.time()},
            {"id": "c-wait", "parent_id": "s-tool", "status": "waiting", "title": "改配置", "created": time.time()},
            {"id": "c-done", "parent_id": "s-tool", "status": "completed", "title": "做完的", "created": time.time()}]
        hub.job_watch._save([{"session": "s-think", "run": "r3", "job": "j3", "pid": 1, "name": "构建", "started": time.time() - 60}])
        hub.terminals = SimpleNamespace(records={"t1": {"id": "t1", "session": "s-tool", "state": "running", "command": "npm test"},
                                                 "t2": {"id": "t2", "session": "s-tool", "state": "waiting", "command": ""}})
        hub.workspace_jobs = SimpleNamespace(records={"w1": {"id": "w1", "session": "s-ask", "state": "running", "command": "dir"}})
        return A.build(hub)

    got = asyncio.run(main())
    items = got["items"]
    by = {(x["kind"], x["id"]): x for x in items}
    ask = by[("session", "s-ask")]
    assert ask["waiting_for"] == "you" and ask["detail"].startswith("等你点头：跑命令 npm run build") and "排队 1 条" in ask["detail"]
    assert by[("session", "s-tool")]["waiting_for"] == "tool" and by[("session", "s-tool")]["detail"].startswith("在跑命令：pytest -q")
    assert "待交给她 1 条" in by[("session", "s-tool")]["detail"]  # 有子任务在跑, 但这会儿在跑工具: 按工具说
    assert by[("session", "s-think")]["waiting_for"] == "model" and "第 2 步" in by[("session", "s-think")]["detail"]
    assert by[("check", "s-check")]["waiting_for"] == "you" and "dist/app.zip" in by[("check", "s-check")]["detail"]
    assert ("check", "s-ok") not in by
    assert by[("session", "s-stop")]["state"] == "queued" and by[("session", "s-stop")]["waiting_for"] == "you"
    assert by[("child", "c-wait")]["waiting_for"] == "you" and "「跑测试」派的" in by[("child", "c-wait")]["detail"]
    assert ("child", "c-done") not in by and by[("child", "c-run")]["state"] == "running"
    assert by[("job", "j3")]["detail"] == "后台作业 j3 在跑，做完会自己交回「写说明」"
    assert ("terminal", "t1") in by and ("terminal", "t2") not in by and ("command", "w1") in by
    waits = [x["waiting_for"] for x in items]
    assert waits[:waits.count("you")] == ["you"] * waits.count("you")  # 等你的排最前
    assert got["counts"] == {"you": 4, "working": 3, "background": 3, "needs_check": 1}


def test_counts_one_thing_once(tmp_path):
    """计数按一件事算 (2026-11-07 工作台改版会话查到的重复计数): 工作台申请 (会话一条 + QQ 转来的一条) 算 1 件;
    子任务的申请 (子任务会话 + 子任务记录 + QQ 转来的) 算 1 件; 在做的子任务 (子任务会话 + 子任务记录) 算 1 件在做."""
    async def main():
        hub, ws = hub_with(tmp_path)
        loop = asyncio.get_running_loop()
        asking = add(hub, "s-ask", ws, "打包", status="waiting", run={"id": "r1", "started": time.time()})
        asking.asks = {"c1": loop.create_future()}
        asking.events = [{"type": "ask", "id": "c1", "label": "跑命令", "text": "npm run build"}]
        add(hub, "s-parent", ws, "汇总", status="running", run={"id": "r2", "started": time.time()})
        child = add(hub, "sub-wait", ws, "改配置", status="waiting", parent_id="s-parent", run={"id": "r3", "started": time.time()})
        child.asks = {"c2": loop.create_future()}
        child.events = [{"type": "ask", "id": "c2", "label": "写文件", "text": "config.json"}]
        add(hub, "sub-run", ws, "查覆盖率", status="running", parent_id="s-parent", run={"id": "r4", "started": time.time()})
        hub.teams.list = lambda parent_id=None: [
            {"id": "sub-wait", "parent_id": "s-parent", "status": "waiting", "title": "改配置", "created": time.time()},
            {"id": "sub-run", "parent_id": "s-parent", "status": "running", "title": "查覆盖率", "created": time.time()}]
        hub.e = SimpleNamespace(channels=SimpleNamespace(activity_items=lambda: [
            {"kind": "ask", "id": "d1", "session": "s-ask", "ask_id": "c1", "title": "在工作台里跑命令", "state": "waiting",
             "waiting_for": "you", "since": None, "detail": "等主人点头"},
            {"kind": "ask", "id": "d2", "session": "sub-wait", "ask_id": "c2", "title": "在工作台里写文件", "state": "waiting",
             "waiting_for": "you", "since": None, "detail": "等主人点头"},
            {"kind": "ask", "id": "d3", "session": "", "title": "她想运行命令", "state": "waiting", "waiting_for": "you",
             "since": None, "detail": "等主人点头"}]))
        return A.build(hub)

    got = asyncio.run(main())
    assert sum(x["waiting_for"] == "you" for x in got["items"]) == 6  # 条目照旧都在 (界面自己合并)
    assert got["counts"]["you"] == 3        # s-ask、sub-wait、QQ 自己的那张申请
    assert got["counts"]["working"] == 2    # s-parent (等子任务也算在做)、sub-run


def test_children_waiting_while_she_waits_for_them(tmp_path):
    hub, ws = hub_with(tmp_path)
    add(hub, "s", ws, "汇总", status="running", run={"id": "r", "started": time.time()})
    hub.teams.list = lambda parent_id=None: [{"id": "c1", "parent_id": "s", "status": "running", "title": "a"},
                                             {"id": "c2", "parent_id": "s", "status": "queued", "title": "b"}]
    item = next(x for x in A.build(hub)["items"] if x["kind"] == "session")
    assert item["waiting_for"] == "children" and item["detail"] == "等子任务 2 个做完再汇总"


def test_sessions_not_opened_since_restart_still_show_what_needs_you(tmp_path):
    """服务端重启以后还没点开的会话: 文件里能看出来的 (核对没过、排着没开始的) 照样列出来."""
    hub, ws = hub_with(tmp_path)
    s = Session("s-disk", ws, title="昨天的活")
    s.run = {"id": "r", "state": "completed", "verification": "unreviewed", "check": {"verdict": "needs_check", "reasons": ["x"]}}
    s.queue = [{"id": "q", "text": "下一件", "materials": [], "created": 1}]
    hub.save(s)
    hub2 = AgentHub({"agent": {"workspace": ws}}, home=tmp_path / "agent")
    got = A.build(hub2)
    kinds = sorted((x["kind"], x["state"]) for x in got["items"])
    assert kinds == [("check", "needs_check"), ("session", "queued")] and got["counts"]["you"] == 2
    assert not hub2.sessions  # 只读文件, 没把会话加载进来


def test_activity_endpoint_and_counts_in_workbench_state(tmp_path):
    async def main():
        client = TestClient(TestServer(create_app(make_cfg(tmp_path / "logs"))))
        await client.start_server()
        try:
            st = await (await client.get("/api/agent")).json()
            view = await (await client.get("/api/agent/runtime?view=activity")).json()
            evil = await client.get("/api/agent/runtime?view=activity", headers={"Origin": "http://evil.example"})
        finally:
            await client.close()
        return st, view, evil.status

    st, view, evil = asyncio.run(main())
    assert st["activity"] == {"you": 0, "working": 0, "background": 0, "needs_check": 0}
    assert view["items"] == [] and view["counts"]["you"] == 0 and "at" in view
    assert evil == 403  # 只认本机页面


def test_terminal_quiet_for_hours_says_so(tmp_path):
    """2026-11-03 看板"在做"里的 PowerShell1 挂了 9 小时没人发现: 终端"在跑"却很久没有新输出时, 卡片上写明多久了."""
    hub, _ws = hub_with(tmp_path)
    now = time.time()
    hub.terminals = SimpleNamespace(records={
        "t-old": {"id": "t-old", "session": "s", "title": "PowerShell1", "state": "running", "command": "npm run dev",
                  "started": now - 9 * 3600, "last_output": now - 9 * 3600},
        "t-busy": {"id": "t-busy", "session": "s", "state": "running", "command": "pytest",
                   "started": now - 9 * 3600, "last_output": now - 5},
        "t-legacy": {"id": "t-legacy", "session": "s", "state": "running", "command": "dir", "started": now - 40 * 60}})
    by = {x["id"]: x for x in A.build(hub)["items"]}
    assert "9 小时没有新输出" in by["t-old"]["detail"] and by["t-old"]["quiet_s"] >= 9 * 3600 - 5
    assert "没有新输出" not in by["t-busy"]["detail"] and by["t-busy"]["quiet_s"] < 60
    assert "40 分钟没有新输出" in by["t-legacy"]["detail"]  # 旧记录没有 last_output: 从开的时候算

"""后台作业做完了自己回到发起的会话 (wm/agent/jobwatch.py; 2026-11-01 用户"好，接着做": 接着"运行中加要求"做
"后台作业做完自己回报"; Super-agent 接入设计第 1 步"长作业先返回 job_id, 完成时发事件").
作业状态照 workbench-jobs 的 SKILL.md 造在临时目录里 (jobs.json、exit/<id>.txt、logs/<id>.log); 直通工具的快照也造一份,
job_start 走真的 _tool (只把扩展进程换成一个假的 execute); 模型换成脚本. 不起真的扩展, 不花钱."""
import asyncio
import json
import os
import time

import psutil
import pytest

from wm.agent import jobwatch as J
from wm.agent.core import AgentHub, Session

START = ("已启动 {id}（pid {pid}）\n工作目录：E:\\Ayane_Use\n日志：x.log\n"
         "查看输出：job_output(job_id=\"{id}\")\n停止：job_stop(job_id=\"{id}\")")
HERE = os.getpid()
HERE_CREATED = int((psutil.Process(HERE).create_time() + J._EPOCH_1601) * 1e7)  # 这个进程自己的 FILETIME
DEAD = 4194303  # 不会有的 PID


class FakeLLM:
    provider = "custom"

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("WORKBENCH_JOBS_STATE_DIR", raising=False)


def put_job(root, job, *, state="running", pid=HERE, created=HERE_CREATED, log="编译中\n第 1 步\n第 2 步\n全部完成\n", name="构建"):
    """像 workbench-jobs 那样记一个作业 (jobs 是 id -> 记录) 并写日志."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / f"{job}.log").write_text(log, encoding="utf-8")
    try:
        data = json.loads((root / "jobs.json").read_text(encoding="utf-8"))
    except OSError:
        data = {"next": 1, "jobs": {}}
    data["jobs"][job] = {"id": job, "name": name, "command": "npm run build", "workdir": "E:\\Ayane_Use",
                         "log": str(root / "logs" / f"{job}.log"), "pid": pid, "started": time.time() - 252,
                         "state": state, "exit_code": None, "process_created": created}
    (root / "jobs.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def finish(root, job, code="0"):
    (root / "exit").mkdir(parents=True, exist_ok=True)
    (root / "exit" / f"{job}.txt").write_text(code, encoding="ascii")


def setup(tmp_path, script):
    """工作台 + 一个会话; job_start 是直通工具 (快照指向 creator:workbench-jobs), 调它时假扩展照样把作业记进 jobs.json.
    script(s, n) -> {content, tool_calls}; 记下每次请求真发出去的消息 (含背景)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    home = tmp_path / "agent"
    ext = home / "extensions"
    ext.mkdir(parents=True)
    (ext / "registry.json").write_text(json.dumps({"version": 1, "packages": {"workbench-jobs": {
        "id": "workbench-jobs", "enabled": True, "active_revision": "r1", "revisions": ["r1"], "latest_revision": "r1",
        "name": "后台任务", "description": "后台任务", "updated": 1}}}), encoding="utf-8")
    (ext / "direct-tools.json").write_text(json.dumps({"servers": {J.SERVER: {"revision": "r1", "label": "后台任务", "tools": [
        {"name": "job_start", "description": "在后台起一个 PowerShell 命令", "read_only": False,
         "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}, "name": {"type": "string"}}}}]}}}),
        encoding="utf-8")
    hub = AgentHub({"agent": {"workspace": str(ws)}}, home=home)
    s = Session("jobs-test", str(ws), model="offline-test")
    hub.sessions[s.id] = s
    hub.save(s)
    seen, events, root = [], [], J.state_dir(home)
    hub._llm = lambda _s: FakeLLM()

    async def ask_model(sess, llm, tools):
        seen.append(sess.request_messages()[1:])
        out = script(sess, len(seen))
        out = await out if asyncio.iscoroutine(out) else out
        return {"content": out.get("content", ""), "tool_calls": out.get("tool_calls", []), "reasoning": ""}
    hub._ask_model = ask_model

    async def execute(sess, cid, name, args):  # 假的扩展: 记作业、回和真扩展一样的话
        job = f"j{len(json.loads((root / 'jobs.json').read_text('utf-8'))['jobs']) + 1}" if (root / "jobs.json").exists() else "j1"
        put_job(root, job, name=args.get("name") or "")
        return START.format(id=job, pid=HERE), False
    hub.studio.execute = execute

    async def sink(ev):
        events.append(ev)
    hub.subscribe(s.id, sink)
    return hub, s, seen, events, root


def start_call(n=1):
    return {"content": "先在后台构建", "tool_calls": [{"id": f"c{n}", "name": "job_start",
                                                    "args": json.dumps({"command": "npm run build", "name": "构建"})}]}


def watching(hub):
    return hub.job_watch.entries()


async def until(pred, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等不到")


# ---------------------------------------------------------------- 只读扩展的状态
def test_check_reads_the_extension_state_like_job_list_does(tmp_path):
    root = tmp_path / "state"
    put_job(root, "j1")
    e = {"job": "j1", "started": time.time()}
    assert J.check(root, e, J.records(root)) is None  # 进程还在 (就是这个测试进程, 创建时间对得上)
    finish(root, "j1", "3")
    assert J.check(root, e, J.records(root))["how"] == "exited" and J.check(root, e, J.records(root))["code"] == 3
    put_job(root, "j2", state="stopped")
    assert J.check(root, {"job": "j2"}, J.records(root))["how"] == "stopped"
    assert J.check(root, {"job": "j9"}, J.records(root))["how"] == "gone"  # job_clean 清掉了
    put_job(root, "j3", pid=DEAD)
    e3 = {"job": "j3", "started": time.time()}
    assert J.check(root, e3, J.records(root)) is None and e3["misses"] == 1  # 刚好在写退出码: 再看一次
    assert J.check(root, e3, J.records(root))["how"] == "lost"
    put_job(root, "j4", created=HERE_CREATED + 10**8)  # PID 还在但创建时间对不上 = 被复用了, 不是那个作业
    e4 = {"job": "j4", "started": time.time()}
    J.check(root, e4, J.records(root))
    assert J.check(root, e4, J.records(root))["how"] == "lost"
    (root / "jobs.json").write_text("{坏的", encoding="utf-8")
    assert J.records(root) is None  # 读坏了这一次先不判


def test_started_recognizes_direct_and_mcp_call_job_start():
    text = START.format(id="j3", pid=123)
    assert J.started("job_start", {"command": "x"}, text, lambda n: J.SERVER) == ("j3", 123)
    assert J.started("mcp_call", {"server_id": J.SERVER, "tool": "job_start", "arguments": {}}, text, lambda n: None) == ("j3", 123)
    assert J.started("job_start", {}, text, lambda n: "creator:別的") is None  # 别的扩展叫同一个名字
    assert J.started("mcp_call", {"server_id": J.SERVER, "tool": "job_list"}, text, lambda n: None) is None
    assert J.started("job_start", {}, "参数不对", lambda n: J.SERVER) is None


def test_describe_says_how_it_ended_and_keeps_the_last_lines(tmp_path):
    root = tmp_path / "state"
    put_job(root, "j1", log="\n".join(f"第 {i} 行" for i in range(40)))
    finish(root, "j1", "1")
    rec = J.records(root)["j1"]
    info = J.describe({"job": "j1"}, rec, J.check(root, {"job": "j1"}, J.records(root)))
    assert info["text"].startswith("后台作业 j1「构建」结束了，退出码 1（跑了 4 分") and info["how"] == "exited"
    assert info["body"].startswith("[后台作业做完了 · j1]") and "第 39 行" in info["body"] and "第 20 行" not in info["body"]


# ---------------------------------------------------------------- 交回发起的会话
def test_job_finishing_while_the_turn_runs_is_handed_over_at_the_next_step(tmp_path):
    """这一轮里起的作业, 这一轮还没做完时结束了: 走真的 _tool 记下来; 下一步开头交给她 (工具结果后面), 标成作业回报、
    状态数据, 不升约束版本; 记录里写着第几步交的."""
    hub_ref = {}

    async def script(s, n):
        if n == 1:
            return start_call()
        if n == 2:  # 她在写别的的时候作业结束了
            finish(hub_ref["root"], "j1", "0")
            assert await hub_ref["hub"].job_watch.poll() is False
            return {"content": "构建在跑，我先写说明。"}
        return {"content": "构建也好了，退出码 0。"}

    async def main():
        hub, s, seen, events, root = setup(tmp_path, script)
        hub_ref.update(hub=hub, root=root)
        await hub.send(s.id, "构建一下然后写说明")
        await until(lambda: s.task is not None)
        await s.task
        return hub, s, seen, events

    hub, s, seen, events = asyncio.run(main())
    assert len(seen) == 3 and seen[2][-1]["role"] == "user" and seen[2][-1]["content"].startswith("[后台作业做完了 · j1]")
    assert "不是主人的新指令" in seen[2][-1]["content"] and "全部完成" in seen[2][-1]["content"]
    assert s.run["state"] == "completed" and s.run["jobs_done"] == [{"job": "j1", "how": "exited", "step": 3}]
    assert "constraint_version" not in s.run and not s.steer and not watching(hub)
    done = [e for e in events if e["type"] == "job_done"]
    assert len(done) == 1 and done[0]["delivered"] == "steer" and done[0]["job"]["code"] == 0


def test_job_finishing_after_a_completed_turn_starts_a_turn_to_report(tmp_path):
    """起作业的那一轮已经正常做完: 作业结束时她自己接着做一轮 (看结果、告诉你); 对话里是一张作业回报, 不是你说的话."""
    async def main():
        hub, s, seen, events, root = setup(tmp_path, lambda s, n: start_call() if n == 1 else
                                           {"content": "构建在后台跑着，好了我告诉你。" if n == 2 else "构建好了，退出码 0，全部完成。"})
        await hub.send(s.id, "在后台构建一下")
        await until(lambda: s.task is not None)
        await s.task
        first_run = s.run["id"]
        finish(root, "j1", "0")
        await hub.job_watch.poll()
        await until(lambda: len(seen) == 3 and s.status == "idle")
        await hub.job_watch.poll()  # 再看一遍: 交过的不再交
        return hub, s, seen, events, first_run

    hub, s, seen, events, first_run = asyncio.run(main())
    assert len(seen) == 3 and seen[2][-1]["content"].startswith("[后台作业做完了 · j1]") and "接着做完" in seen[2][-1]["content"]
    assert s.run["id"] != first_run and s.run["state"] == "completed" and s.run["goal"].startswith("[后台作业做完了 · j1]")
    done = [e for e in events if e["type"] == "job_done"]
    assert [d["delivered"] for d in done] == ["continue"]
    users = [e for e in events if e["type"] == "user"]
    assert [u["text"] for u in users] == ["在后台构建一下"]  # 那张作业回报不是你说的话
    assert not watching(hub) and not s.queue


def test_job_finishing_after_you_stopped_the_turn_does_not_revive_it(tmp_path):
    """那一轮你按了停: 作业后来结束了只记一笔 (对话里一张回报), 不自己接着做; 下次你说话时把这笔带给她."""
    hub_ref = {}

    async def script(s, n):
        if n == 1:
            return start_call()
        if n == 2:
            await hub_ref["hub"].stop(s.id)
            return {"content": "好，先停下。"}
        return {"content": "嗯，构建后来结束了。"}

    async def main():
        hub, s, seen, events, root = setup(tmp_path, script)
        hub_ref["hub"] = hub
        await hub.send(s.id, "在后台构建一下")
        await until(lambda: s.task is not None)
        await s.task
        finish(root, "j1", "0")
        how = await hub.job_watch.poll()
        await asyncio.sleep(0.05)
        calls_before = len(seen)
        notes = list(s.job_notes)
        await hub.send(s.id, "刚才那个怎么样了")
        await s.task
        return hub, s, seen, events, calls_before, notes, how

    hub, s, seen, events, calls_before, notes, how = asyncio.run(main())
    assert calls_before == 2  # 没有自己接着做
    assert len(notes) == 1 and "那一轮停下了" in notes[0]
    done = [e for e in events if e["type"] == "job_done"]
    assert len(done) == 1 and done[0]["delivered"] == "note" and done[0]["why"] == "那一轮停下了"
    asked = seen[2][-1]["content"]
    assert asked.startswith("刚才那个怎么样了") and "[后台作业] 后台作业 j1「构建」结束了" in asked  # 背景里带上了
    assert not s.job_notes and not watching(hub)


def test_watch_survives_a_restart_and_reports_to_the_session_on_disk(tmp_path):
    """在看的名单存在文件里: 服务端重启后 (新的 hub, 会话还没打开过) 作业结束了照样交回去, 接着做的那一轮照常开始."""
    async def main():
        hub, s, seen, events, root = setup(tmp_path, lambda s, n: start_call() if n == 1 else {"content": "好的。"})
        await hub.send(s.id, "在后台构建一下")
        await until(lambda: s.task is not None)
        await s.task
        assert [e["job"] for e in watching(hub)] == ["j1"]
        hub2 = AgentHub({"agent": {"workspace": str(tmp_path / "workspace")}}, home=tmp_path / "agent")
        seen2 = []
        hub2._llm = lambda _s: FakeLLM()

        async def ask_model(sess, llm, tools):
            seen2.append(sess.request_messages()[-1]["content"])
            return {"content": "构建好了。", "tool_calls": [], "reasoning": ""}
        hub2._ask_model = ask_model
        finish(root, "j1", "0")
        await hub2.job_watch.poll()
        s2 = hub2.get(s.id)
        await until(lambda: s2.task is not None and s2.task.done())
        return hub2, s2, seen2

    hub2, s2, seen2 = asyncio.run(main())
    assert len(seen2) == 1 and seen2[0].startswith("[后台作业做完了 · j1]")
    assert s2.run["state"] == "completed" and not watching(hub2)

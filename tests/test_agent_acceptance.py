"""三层验收 (wm/agent/acceptance.py; 2026-11-01 用户"好，接着做": 升级方案第 3 步剩下的; 研究总报告第七节第 10 个用例
"工具返回成功但产物缺失: 任务标失败或待核查, 桌宠不汇报完成"): 一轮做完时程序分开记 说 / 做 / 真; 她说出来的文件找不到、
说成了但最后的命令失败了 = 对不上, 让她核对一次, 还对不上就标待核查. 模型换成脚本, write_file 走真的 _tool."""
import asyncio
import json
from types import SimpleNamespace

from wm.agent import acceptance as A
from wm.agent.core import AgentHub, Session
from wm.agent.deliverables import DeliveryStore

RECHECK = "[程序核对 · 你说做完了，但有对不上的]"


# ---------------------------------------------------------------- 认"说写到 / 生成了"的文件
def test_claimed_paths_only_counts_files_she_says_she_made():
    c = A.claimed_paths
    assert c("做完了，说明写在 `docs/说明.md` 里，报告生成到 out/report.pdf。") == ["docs/说明.md", "out/report.pdf"]
    assert c("新建了 `src/app.py` 和 `tests/test_app.py`，另存为 backup.zip") == ["src/app.py", "tests/test_app.py", "backup.zip"]
    assert c(r"已经保存到 E:\Ayane_Use\notes\summary.txt 了") == [r"E:\Ayane_Use\notes\summary.txt"]
    assert c("生成了 out/图表.png，顺便把旧的 old.png 删了") == ["out/图表.png"]  # 不同小句里的不算
    assert c("我会写到 result.md（还没写）") == [] and c("报错在 main.py 第 3 行，还没修好") == []  # 还没做的不算
    assert c("这个文件 data.csv 我只是读了一下") == [] and c("说明写在 README 里") == []  # 只是提到、没后缀
    assert c("参考 https://example.com/a/b.html 写的，Python 3.10 下测过 v1.2.3") == []  # 网址、版本号


# ---------------------------------------------------------------- 三层核对
def fake_session(tmp_path, run_id="r1"):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return SimpleNamespace(id="sess", workspace=str(ws), run={"id": run_id, "state": "running", "tool_errors": 0}), ws


def test_check_run_separates_said_done_tools_and_real_results(tmp_path):
    store = DeliveryStore(tmp_path / "home")
    s, ws = fake_session(tmp_path)
    (ws / "a.txt").write_text("内容", encoding="utf-8")
    (ws / "tmp_probe.py").write_text("print(1)", encoding="utf-8")
    (ws / "docs").mkdir()
    (ws / "docs" / "说明.md").write_text("说明", encoding="utf-8")
    store.register("sess", "r1", str(ws), str(ws / "a.txt"))
    store.register("sess", "r1", str(ws), str(ws / "tmp_probe.py"))
    (ws / "tmp_probe.py").unlink()  # 她写过、用完删了、没再提的临时文件

    ok = A.check_run(store, s, "做完了，结果写在 a.txt，说明写在 说明.md 里。")  # 说明.md 在 docs/ 下: 路径说短了也算找到
    assert ok["verdict"] == "ok" and ok["said"] == "done" and ok["real"]["artifacts"] == 2
    assert ok["real"]["claimed"] == ["a.txt", "说明.md"] and ok["real"]["claimed_missing"] == []
    assert any("tmp_probe.py" in n and "临时文件" in n for n in ok["notes"])

    bad = A.check_run(store, s, "做完了，报告生成到 out/report.pdf。")
    assert bad["verdict"] == "needs_check" and bad["real"]["claimed_missing"] == ["out/report.pdf"]
    gone = A.check_run(store, s, "临时脚本 tmp_probe.py 也留着，可以再用。")  # 登记过、不在了、她却还提到它
    assert gone["verdict"] == "needs_check" and "现在不在了" in gone["reasons"][0]

    store.record_evidence("sess", "r1", tool_call_id="c1", command="npm test", exit_code=1, output="1 failed")
    lied = A.check_run(store, s, "测试全过了，搞定。")
    assert lied["verdict"] == "needs_check" and "退出码 1" in lied["reasons"][0] and lied["tools"]["failed_commands"] == 1
    honest = A.check_run(store, s, "测试还有 1 个没过，卡在路径大小写上。")
    assert honest["verdict"] == "ok"  # 她如实说了没过: 不算对不上

    stopped = A.check_run(store, s, "", "cancelled")
    assert stopped["said"] == "stopped" and stopped["verdict"] == "ok"  # 停了的只记, 不判待核查


# ---------------------------------------------------------------- 真的一轮
class FakeLLM:
    provider = "custom"

    async def close(self):
        pass


def setup(tmp_path, script):
    ws = tmp_path / "workspace"
    ws.mkdir()
    hub = AgentHub({"agent": {"workspace": str(ws)}}, home=tmp_path / "agent")
    s = Session("check-test", str(ws), model="offline-test")
    hub.sessions[s.id] = s
    hub.save(s)
    seen, events = [], []
    hub._llm = lambda _s: FakeLLM()

    async def ask_model(sess, llm, tools):
        seen.append([{k: v for k, v in m.items() if not k.startswith("_")} for m in sess.messages])
        out = script(sess, len(seen))
        out = await out if asyncio.iscoroutine(out) else out
        return {"content": out.get("content", ""), "tool_calls": out.get("tool_calls", []), "reasoning": ""}
    hub._ask_model = ask_model

    async def sink(ev):
        events.append(ev)
    hub.subscribe(s.id, sink)
    return hub, s, seen, events, ws


def write(path, content, n=1):
    return {"content": "", "tool_calls": [{"id": f"w{n}", "name": "write_file", "args": json.dumps({"path": path, "content": content})}]}


def run(hub, s, text="把报告做好"):
    async def main():
        assert await hub.send(s.id, text) is None
        for _ in range(500):
            if s.task is not None:
                break
            await asyncio.sleep(0.01)
        await s.task
    asyncio.run(main())


def test_she_claims_a_missing_file_gets_one_recheck_and_fixes_it(tmp_path):
    """她说报告生成了, 其实没写: 程序核对对不上, 同一轮里让她核对一次; 她补写了, 再核对就过了, 照常结束."""
    def script(s, n):
        return ({"content": "做完了，报告生成到 out/report.md。"} if n == 1 else
                write("out/report.md", "# 报告\n") if n == 2 else {"content": "刚才没写成，现在补上了，报告在 out/report.md。"})

    hub, s, seen, events, ws = setup(tmp_path, script)
    run(hub, s)
    assert len(seen) == 3 and seen[1][-1]["role"] == "user" and seen[1][-1]["content"].startswith(RECHECK)
    assert "out/report.md" in seen[1][-1]["content"] and "不是主人的新指令" in seen[1][-1]["content"]
    assert (ws / "out" / "report.md").exists()
    assert s.run["state"] == "completed" and s.run["rechecked"] and s.run["check"]["verdict"] == "ok"
    assert [e["stage"] for e in events if e["type"] == "check"] == ["recheck"]
    done = [e for e in events if e["type"] == "done"][-1]
    assert done["run"]["check"]["verdict"] == "ok"


def test_still_wrong_after_one_recheck_is_marked_needs_check_not_done(tmp_path):
    """核对过一次还说有、其实没有: 不再循环 (只核对一次), 这一轮标待核查, 结束事件里带着, 页面不说"执行结束"."""
    hub, s, seen, events, ws = setup(tmp_path, lambda s, n: {"content": "做完了，安装包生成到 dist/app.zip。"})
    run(hub, s)
    assert len(seen) == 2
    assert s.run["state"] == "completed" and s.run["check"]["verdict"] == "needs_check"
    assert s.run["check"]["real"]["claimed_missing"] == ["dist/app.zip"]
    assert [e["stage"] for e in events if e["type"] == "check"] == ["recheck", "final"]
    done = [e for e in events if e["type"] == "done"][-1]
    assert done["run"]["check"]["verdict"] == "needs_check"


def test_after_recheck_just_saying_it_is_there_is_still_needs_check(tmp_path):
    """核对时只说"在 dist 目录里"、没补也没改口: 上次找不到的文件再查一遍, 还是没有 -> 待核查 (浏览器里实点时发现的漏洞)."""
    hub, s, seen, events, ws = setup(tmp_path, lambda s, n: {"content": "做完了，安装包生成到 dist/app.zip。"} if n == 1
                                     else {"content": "核对过了，安装包在 dist 目录里。"})
    run(hub, s)
    assert len(seen) == 2 and s.run["check"]["verdict"] == "needs_check"
    assert s.run["check"]["reasons"] == ["核对以后 dist/app.zip 还是找不到"]


def test_after_recheck_admitting_it_was_not_made_is_ok(tmp_path):
    """核对时如实说了没生成: 不算对不上 (说错了改口就好), 这一轮照常结束."""
    hub, s, seen, events, ws = setup(tmp_path, lambda s, n: {"content": "做完了，安装包生成到 dist/app.zip。"} if n == 1
                                     else {"content": "核对了，其实安装包没生成，打包那一步报错了，我把报错贴在下面。"})
    run(hub, s)
    assert len(seen) == 2 and s.run["check"]["verdict"] == "ok"
    assert [e["stage"] for e in events if e["type"] == "check"] == ["recheck"]


def test_honest_run_has_no_recheck_and_records_the_three_layers(tmp_path):
    hub, s, seen, events, ws = setup(tmp_path, lambda s, n: write("a.txt", "你好\n") if n == 1 else {"content": "写好了 a.txt。"})
    run(hub, s)
    assert len(seen) == 2 and not [e for e in events if e["type"] == "check"]
    check = s.run["check"]
    assert check["verdict"] == "ok" and check["said"] == "done" and check["real"]["artifacts"] == 1
    assert check["real"]["claimed"] == ["a.txt"] and not check["real"]["claimed_missing"] and "rechecked" not in s.run


def test_stopped_turn_is_not_checked_but_a_budget_stop_is_recorded(tmp_path):
    """你按停的: 不核对、也不再让她核对 (停下来要快, 全套测试里"停掉在跑的命令"那条卡着 10 秒); 到预算停下的:
    结束事件发出去以后再记一份核对 (只记)."""
    hub_ref = {}

    async def script(s, n):
        if n == 1:
            return write("half.txt", "写了一半")
        await hub_ref["hub"].stop(s.id)
        return {"content": "报告生成到 out/report.md 了。"}

    hub, s, seen, events, ws = setup(tmp_path, script)
    hub_ref["hub"] = hub
    run(hub, s)
    assert s.run["state"] == "cancelled" and len(seen) == 2 and "check" not in s.run
    assert not [e for e in events if e["type"] == "check"]

    (tmp_path / "b").mkdir()
    hub2, s2, seen2, events2, ws2 = setup(tmp_path / "b", lambda s, n: write(f"part{n}.txt", "一段", n))
    s2.max_steps = 2
    run(hub2, s2)
    assert s2.run["state"] == "budget_exhausted" and s2.run["check"]["said"] == "budget"
    assert s2.run["check"]["real"]["artifacts"] == 2 and s2.run["check"]["verdict"] == "ok"

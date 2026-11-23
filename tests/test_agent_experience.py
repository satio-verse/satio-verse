"""工作经验 (wm/agent/experience.py; 2026-11-01 用户"好，接着做": ReActive 论文的经验层、升级方案的"程序经验"、dot 的
"越用越懂你"). 命令报错 -> 换了做法 -> 同一类命令成了: 这一轮结束后折成候选; 同一类报错第二次出现时程序把它附在工具结果后面
(未验证, 可以不采纳), 她照着做成了就加一次支持, 照着做还是一样的错就降级. 最后一条用真的命令跑 (模型换成脚本, 不花钱)."""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from test_pipeline import make_cfg
from wm.agent import experience as X
from wm.agent.core import AgentHub
from wm.app import create_app

DAY = 86400


# ---------------------------------------------------------------- 认"哪类命令"、报错特征
def test_command_head_signature_and_privacy():
    assert X.head("cd web && npm run build -- --prod") == "npm run build"
    assert X.head("python -m pytest tests/x.py -q") == "python -m pytest"
    assert X.head("python check.py") == "python check.py" and X.head("pytest tests/a.py") == "pytest"
    assert X.head("$env:FOO=1; node scripts/gen.mjs") == "node gen.mjs" and X.head("git push origin main") == "git push"
    tb = ("退出码 1\nTraceback (most recent call last):\n  File \"C:\\Users\\ASUS\\proj\\a.py\", line 3, in <module>\n"
          "ModuleNotFoundError: No module named 'lodash'")
    assert X.signature(tb) == "ModuleNotFoundError: No module named 'lodash'"
    assert X.signature("退出码 2\nC:\\Python310\\python.exe: can't open file 'C:\\Users\\ASUS\\ws\\check.py': "
                       "[Errno 2] No such file or directory") == \
        "python.exe: can't open file 'check.py': [Errno <n>] No such file or directory"  # 路径只留文件名, 数字换掉
    assert X.signature("退出码 1\nFAILED tests/t.py::test_a - AssertionError: assert 1 == 2\n== 1 failed, 3 passed in 0.1s ==") \
        == "FAILED tests/t.py::test_a - AssertionError: assert <n> == <n>"  # 不挑"1 failed, 3 passed"这种总数行
    assert X.signature("退出码 1\nnpm ERR! code ENOENT\nnpm ERR! syscall open\nnpm ERR! A complete log of this run ...") \
        == "npm ERR! code ENOENT"
    assert "secret" not in X.signature("退出码 1\ntoken=abc123secret failed")
    assert X.clean("python C:\\Users\\ASUS\\p\\run.py --api_key=sk-ABCDEFGHIJKLMNOP123") == "python run.py --<已隐去>"


def cmd(cid, command, ok, sig=""):
    return {"id": cid, "tool": "run_command", "ok": ok, "head": X.head(command), "cmd": command, "sig": sig}


def test_episodes_fold_fail_fix_success_and_skip_noise():
    sig = "Error: Cannot find module 'lodash'"
    calls = [cmd("c1", "npm test", False, sig), cmd("c2", "npm test", False, sig),  # 同一段里连着失败两次
             {"id": "c3", "tool": "edit_file", "ok": True, "file": "package.json"},
             cmd("c4", "npm install lodash", True), cmd("c5", "npm test", True),
             cmd("c6", "npm run lint", False, "error  Missing semicolon"), cmd("c7", "npm run lint", True),  # 原样重跑就过了: 偶发
             cmd("c8", "pytest", False, "AssertionError: x"),  # 一直没成: 不算
             cmd("c9", "git push", False, "fatal: rejected"), cmd("c10", "git push --force-with-lease", True)]
    eps = X.episodes(calls)
    assert [(e["head"], e["fail"], e["ok"], e["akey"]) for e in eps] == [
        ("npm test", "c1", "c5", "edit:.json"), ("git push", "c9", "c10", "as:git push --force-with-lease")]
    assert eps[0]["action"] == "先改 `package.json`，再`npm install lodash`" and eps[0]["sig"] == sig
    assert eps[1]["action"] == "改成 `git push --force-with-lease`"


# ---------------------------------------------------------------- 仓库: 整理、推、回流、降级、退役、你来改
class S:
    def __init__(self, rid):
        self.run = {"id": rid}


def call(cid, name, **args):
    return {"id": cid, "function": {"name": name, "arguments": json.dumps(args)}}


FAIL = "退出码 1\nError: Cannot find module 'lodash'"


def run_fix(store, rid, sid="s1", push_expected=False):
    """一轮: npm test 报错 -> npm install lodash -> npm test 成了. 返回第一次报错时附上的引用块."""
    s = S(rid)
    block = store.after_tool(s, call("a", "run_command", command="npm test"), FAIL, True)
    assert bool(block) == push_expected, block
    store.after_tool(s, call("b", "read_file", path="package.json"), "{}", False)  # 看的不算一步
    store.after_tool(s, call("c", "run_command", command="npm install lodash"), "退出码 0\nadded 1 package", False)
    store.after_tool(s, call("d", "run_command", command="npm test"), "退出码 0\nok", False)
    return block, store.learn(sid, s.run)


def test_learn_push_feedback_and_your_controls(tmp_path):
    now = [1_800_000_000.0]
    store = X.Experiences(tmp_path, clock=lambda: now[0])
    block, got = run_fix(store, "r1")
    assert got == {"pushed": 0, "hits": 0, "contradicted": 0, "new": 1, "folded": 0}
    (e,) = store.items.values()
    assert (e["head"], e["action"], e["state"], e["trust"], e["support"]) == \
        ("npm test", "先`npm install lodash`", "candidate", 3, 1)
    assert e["sources"][0] == {"session": "s1", "run": "r1", "fail": "a", "ok": "d", "at": now[0]}

    now[0] += DAY
    block, got = run_fix(store, "r2", push_expected=True)  # 同一类报错第二次: 推了, 她照着做成了
    assert block.startswith(X.HEAD) and "以前 `npm test` 报过类似的错（Error: Cannot find module 'lodash'）" in block
    assert "先`npm install lodash`，同一类命令再跑就成了（成过 1 次" in block and "未验证" in block
    assert got == {"pushed": 1, "hits": 1, "contradicted": 0, "new": 0, "folded": 0}  # 回流算过一次, 不再重复折叠
    assert (e["support"], e["hits"], e["pushed"], e["adopted"], e["state"]) == (2, 1, 1, 1, "supported")

    # 照着做了还是一样的错: 反例, 马上降级, 不再推
    s = S("r3")
    assert store.after_tool(s, call("a", "run_command", command="npm test"), FAIL, True)
    store.after_tool(s, call("c", "run_command", command="npm install lodash"), "退出码 0\n", False)
    store.after_tool(s, call("d", "run_command", command="npm test"), FAIL, True)
    assert store.learn("s1", s.run)["contradicted"] == 1
    assert e["contradict"] == 1 and e["state"] == "contradicted"
    assert store.after_tool(S("r4"), call("a", "run_command", command="npm test"), FAIL, True) == ""

    # 你确认过的: 升 L2, 推的时候写"主人确认过"; 停用就不推; 删掉就没了
    assert store.update(e["id"], "approve")["item"]["state"] == "supported"
    assert "主人确认过" in store.after_tool(S("r5"), call("a", "run_command", command="npm test"), FAIL, True)
    store.update(e["id"], "disable")
    assert store.after_tool(S("r6"), call("a", "run_command", command="npm test"), FAIL, True) == ""
    view = store.view()
    assert view["mode"] == "on" and view["items"][0]["enabled"] is False and "words" not in view["items"][0]
    assert view["stats"]["on"]["pushes"] == 2 and view["stats"]["on"]["hits"] == 1 and view["stats"]["on"]["again"] == 1
    assert store.update(e["id"], "delete") == {"deleted": e["id"]} and not store.items
    assert X.Experiences(tmp_path).items == {}  # 存盘了


def test_old_unverified_ones_retire_and_unrelated_errors_are_not_pushed(tmp_path):
    now = [1_800_000_000.0]
    store = X.Experiences(tmp_path, clock=lambda: now[0])
    run_fix(store, "r1")
    other = "退出码 1\nSyntaxError: invalid syntax"
    assert store.after_tool(S("r2"), call("a", "run_command", command="npm test"), other, True) == ""  # 报错不像: 不推
    assert store.after_tool(S("r3"), call("a", "run_command", command="npm run build"), FAIL, True) == ""  # 不是同一类命令
    now[0] += (X.RETIRE_DAYS + 1) * DAY
    assert store.view()["items"][0]["state"] == "retired"
    assert store.after_tool(S("r4"), call("a", "run_command", command="npm test"), FAIL, True) == ""
    reloaded = X.Experiences(tmp_path, clock=lambda: now[0])
    assert len(reloaded.items) == 1  # 退役的还留着 (看得到), 只是不推


def test_shadow_records_what_would_be_pushed_and_off_does_nothing(tmp_path):
    store = X.Experiences(tmp_path / "a")
    run_fix(store, "r1")
    store.mode = "shadow"
    block, got = run_fix(store, "r2")  # shadow: 不附, 只记; 她自己就那么做了也照常折叠
    assert block == "" and got["pushed"] == 0 and got["folded"] == 1
    st = store.view()["stats"]
    assert st["shadow"]["pushes"] == 1 and st["shadow"]["adopted"] == 1 and st["on"]["pushes"] == 0
    off = X.Experiences(tmp_path / "b", mode="off")
    assert run_fix(off, "r1") == ("", {"pushed": 0, "hits": 0, "contradicted": 0, "new": 0, "folded": 0})
    assert not (tmp_path / "b" / "experiences.json").exists()


def test_confirmed_mode_pushes_only_what_you_approved(tmp_path):
    """confirmed (升级方案原来写的"你点头才用"): 没确认的不推, 只记"本来会推" (照样攒证据); 你确认以后才推."""
    store = X.Experiences(tmp_path, mode="confirmed")
    run_fix(store, "r1")
    block, got = run_fix(store, "r2")
    assert block == "" and got["pushed"] == 0 and got["folded"] == 1
    assert store.view()["stats"]["shadow"]["pushes"] == 1 and store.view()["stats"]["on"]["pushes"] == 0
    (e,) = store.items.values()
    store.update(e["id"], "approve")
    block, got = run_fix(store, "r3", push_expected=True)
    assert "主人确认过" in block and got["hits"] == 1


def test_denied_or_secret_commands_are_not_recorded(tmp_path):
    store = X.Experiences(tmp_path)
    s = S("r1")
    store.after_tool(s, call("a", "run_command", command="npm test"), "主人拒绝了, 没有执行", True)  # 没真跑
    store.after_tool(s, call("b", "run_command", command="curl -H 'token=abc123' x"), "退出码 0\n", False)
    store.after_tool(s, call("c", "write_file", path="a.txt"), "没写成: 位置不能写", True)
    assert "r1" not in store._runs


# ---------------------------------------------------------------- 真的一轮: 真命令, 模型换成脚本
class FakeLLM:
    provider = "custom"

    async def close(self):
        pass


def test_second_time_the_same_error_she_gets_last_fix_and_it_is_supported(tmp_path):
    """第一轮: `python check.py` 报"找不到文件" -> 写 check.py -> 再跑成了 (折成候选). 第二轮同样的错: 工具结果后面附上这条经验,
    她照着写了又跑成了 -> 支持加一、成了"有支持"; 页面上有一条说明附了什么."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    hub = AgentHub({"agent": {"workspace": str(ws)}}, home=tmp_path / "agent")
    hub._llm = lambda _s: FakeLLM()
    seen, events = [], []

    def run_cmd(n):
        return {"content": "", "tool_calls": [{"id": f"c{n}", "name": "run_command",
                                               "args": json.dumps({"command": "python check.py"})}]}

    def write(n):
        return {"content": "", "tool_calls": [{"id": f"w{n}", "name": "write_file",
                                               "args": json.dumps({"path": "check.py", "content": "print('ok')\n"})}]}
    plan = {1: run_cmd, 2: write, 3: run_cmd, 5: run_cmd, 6: write, 7: run_cmd}

    async def ask_model(sess, llm, tools):
        seen.append([dict(m) for m in sess.messages])
        n = len(seen)
        out = plan[n](n) if n in plan else {"content": "检查脚本能跑了。", "tool_calls": []}
        return {"content": out["content"], "tool_calls": out["tool_calls"], "reasoning": ""}
    hub._ask_model = ask_model

    async def main():
        s = hub.new_session(mode="full")

        async def sink(ev):
            events.append(ev)
        hub.subscribe(s.id, sink)
        for text in ("把检查脚本跑起来", "再跑一次检查"):
            assert await hub.send(s.id, text) is None
            await asyncio.sleep(0)
            await s.task
            if text == "把检查脚本跑起来":
                assert s.run["experience"]["new"] == 1
                (ws / "check.py").unlink()  # 第二轮又没有这个文件了
        return s

    s = asyncio.run(main())
    assert len(seen) == 8
    first_fail = next(m for m in seen[1] if m.get("role") == "tool")["content"]
    assert X.HEAD not in first_fail  # 第一次还没有经验
    second_fail = [m for m in seen[5] if m.get("role") == "tool"][-1]["content"]
    assert "退出码" in second_fail and X.HEAD in second_fail
    assert "以前 `python check.py` 报过类似的错" in second_fail and "先写 `check.py`" in second_fail
    assert s.run["experience"] == {"pushed": 1, "hits": 1, "contradicted": 0, "new": 0, "folded": 0}
    (e,) = hub.experience.items.values()
    assert (e["support"], e["hits"], e["state"], e["akey"]) == (2, 1, "supported", "edit:.py")
    notes = [ev["text"] for ev in events if ev.get("type") == "note" and "附上了以前的经验" in ev.get("text", "")]
    assert len(notes) == 1 and "python check.py" in notes[0]
    log = [json.loads(ln) for ln in (tmp_path / "agent" / "experience-log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["mode"], r["adopted"], r["outcome"], r["steps"]) for r in log] == [("on", True, "success", 2)]


def test_experience_endpoint_and_actions(tmp_path):
    async def main():
        client = TestClient(TestServer(create_app(make_cfg(tmp_path / "logs"))))
        await client.start_server()
        try:
            view = await (await client.get("/api/agent/runtime?view=experiences")).json()
            bad = await client.post("/api/agent/runtime", json={"action": "experience", "exp_id": "exp-nope", "do": "approve"})
            evil = await client.get("/api/agent/runtime?view=experiences", headers={"Origin": "http://evil.example"})
            return view, bad.status, (await bad.json())["error"], evil.status
        finally:
            await client.close()

    view, bad, why, evil = asyncio.run(main())
    assert view["ok"] and view["mode"] == "on" and view["items"] == [] and view["stats"]["on"]["pushes"] == 0
    assert bad == 400 and why == "没有这条经验"
    assert evil == 403  # 只认本机页面

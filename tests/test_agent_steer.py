"""运行中加要求 (2026-11-01 用户要"根据调研接着做"; 研究总报告第 7 节第 2 个验收用例"长任务中途改输出语言";
Super-agent / DSH: 新输入在当前这一步做完后处理): 她做事时你补充一句, 不停下这一轮, 手头这一步做完就交给她, 后面的步骤照它来.
大多直接跑真的 _run / _turn, 模型和工具换成脚本 (工具能卡住, 好在"这一步还没做完"的时候插话); 最后一条走 WS + 假云端."""
import asyncio
import json

import pytest

from test_agent import events_until, run_agent
from test_cloud import call, say
from wm.agent.core import AgentHub, Session

STEER_HEAD = "[做到一半补充的要求 · 第 1 条]"


class FakeLLM:
    provider = "custom"

    async def close(self):
        pass


def setup(tmp_path, script):
    """script: 每次问模型时调 (s, n) -> {content, tool_calls}, 可以是 async. 记下每次请求的消息; 工具卡在 gate 上."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    hub = AgentHub({"agent": {"workspace": str(ws)}}, home=tmp_path / "agent")
    s = Session("steer-test", str(ws), model="offline-test")
    hub.sessions[s.id] = s
    hub.save(s)
    seen, gate, events = [], asyncio.Event(), []
    hub._llm = lambda _s: FakeLLM()

    async def ask_model(sess, llm, tools):
        seen.append([{k: v for k, v in m.items() if not k.startswith("_")} for m in sess.messages])
        out = script(sess, len(seen))
        out = await out if asyncio.iscoroutine(out) else out
        return {"content": out.get("content", ""), "tool_calls": out.get("tool_calls", []), "reasoning": ""}
    hub._ask_model = ask_model

    async def tool(sess, c, offered):
        await gate.wait()
        return f"{c['function']['name']} 的结果", None
    hub._tool = tool

    async def sink(ev):
        events.append(ev)
    hub.subscribe(s.id, sink)
    return hub, s, seen, gate, events


def disk(hub, s):
    return json.loads((hub.home / "sessions" / f"{s.id}.json").read_text("utf-8"))


async def until(pred, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等不到")


def test_steer_reaches_her_after_the_current_step_and_later_steps_follow_it(tmp_path):
    """工具还在跑的时候补一句: 先落盘、回执; 工具做完 (调用和结果配齐) 以后、下一次问模型之前交给她; 这一轮照常做完,
    记录里写着第几步交的、约束到了第几版; 对话里那句标着是中途补充的."""
    def script(s, n):
        if n == 1:
            return {"content": "先看看目录", "tool_calls": [{"id": "c1", "name": "list_dir", "args": "{}"}]}
        return {"content": "Done, written in English."}

    async def main():
        hub, s, seen, gate, events = setup(tmp_path, script)
        assert await hub.send(s.id, "写一份说明") is None
        await until(lambda: s.status == "running" and len(seen) == 1)
        got = await hub.steer(s.id, "改用英文写")
        on_disk = disk(hub, s)["steer"]
        gate.set()
        await s.task
        return s, seen, events, got, on_disk

    s, seen, events, got, on_disk = asyncio.run(main())
    assert got["mode"] == "steer" and [x["text"] for x in on_disk] == ["改用英文写"]  # 回执之前已经落盘
    assert len(seen) == 2
    second = seen[1]
    assert [m["role"] for m in second[-3:]] == ["assistant", "tool", "user"]  # 接在工具结果后面, 协议完整
    assert second[-1]["content"].startswith(STEER_HEAD) and "改用英文写" in second[-1]["content"]
    assert "以它为准" in second[-1]["content"] and "子任务" not in second[-1]["content"]  # 没有在跑的子任务就不提
    assert s.run["state"] == "completed" and s.run["constraint_version"] == 1
    assert [(x["text"], x["step"]) for x in s.run["steers"]] == [("改用英文写", 2)]
    assert not s.steer and not s.queue
    kinds = [(e["type"], e.get("steer")) for e in events if e["type"] in ("steered", "user")]
    assert kinds[0] == ("user", None) and ("user", True) in kinds
    pending = [e for e in events if e["type"] == "steered"]
    assert [len(e["steer"]) for e in pending] == [1, 0]
    shown = next(e for e in events if e["type"] == "user" and e.get("steer"))
    assert shown["text"] == "改用英文写" and shown["message_id"] == s.messages[-2]["_id"]  # 对话里只显示你说的那句


def test_steer_while_she_writes_the_final_answer_keeps_the_turn_going(tmp_path):
    """她以为做完了 (这一步没调工具) 的时候你又补了一句: 不结束, 下一步开头交给她, 她照新要求再做一步."""
    hub_ref = {}

    async def script(s, n):
        if n == 1:
            await hub_ref["hub"].steer(s.id, "再加一段总结")  # 模型还在写最后的回答时到的
            return {"content": "写好了。"}
        return {"content": "补上了总结。"}

    async def main():
        hub, s, seen, gate, events = setup(tmp_path, script)
        hub_ref["hub"] = hub
        await hub.send(s.id, "写一份说明")
        await until(lambda: s.task is not None)
        await s.task
        return s, seen

    s, seen = asyncio.run(main())
    assert len(seen) == 2 and seen[1][-1]["content"].startswith(STEER_HEAD) and "再加一段总结" in seen[1][-1]["content"]
    assert s.run["state"] == "completed" and s.run["steers"][0]["step"] == 2
    assert [m["content"] for m in s.messages if m["role"] == "assistant"] == ["写好了。", "补上了总结。"]


def test_undelivered_steer_goes_to_the_front_of_the_queue_and_waits_after_stop(tmp_path):
    """补了一句但这一轮停了 (还没交给她): 不丢, 排到队首 (比原来排着的靠前), 标着是补充转来的; 停下的不自己开始."""
    def script(s, n):
        return {"content": "", "tool_calls": [{"id": f"c{n}", "name": "list_dir", "args": "{}"}]}

    async def main():
        hub, s, seen, gate, events = setup(tmp_path, script)
        await hub.send(s.id, "长任务")
        await until(lambda: len(seen) == 1)
        await hub.enqueue(s.id, "原来排着的")
        await hub.steer(s.id, "顺便检查一下拼写")
        await hub.stop(s.id)
        gate.set()
        await s.task
        await asyncio.sleep(0.05)
        return s, seen, disk(hub, s)

    s, seen, on_disk = asyncio.run(main())
    assert s.run["state"] == "cancelled" and s.status == "idle" and len(seen) == 1
    assert [x["text"] for x in s.queue] == ["顺便检查一下拼写", "原来排着的"] and s.queue[0]["from_steer"]
    assert not s.steer and [x["text"] for x in on_disk["queue"]] == ["顺便检查一下拼写", "原来排着的"] and not on_disk["steer"]


def test_steer_when_idle_queues_and_starts_right_away(tmp_path):
    """她刚好做完了 (空闲): 补充的这句就当新的一件事, 马上开始."""
    async def main():
        hub, s, seen, gate, events = setup(tmp_path, lambda s, n: {"content": "好的"})
        got = await hub.steer(s.id, "再帮我看看这个")
        await until(lambda: s.task is not None)
        await s.task
        return s, got

    s, got = asyncio.run(main())
    assert got["mode"] == "queued" and s.run["goal"] == "再帮我看看这个" and s.run["state"] == "completed" and not s.queue


@pytest.mark.parametrize("case", ["subtask", "stopping", "empty"])
def test_steer_is_refused_without_touching_the_session(tmp_path, case):
    async def main():
        hub, s, seen, gate, events = setup(tmp_path, lambda s, n: {"content": "", "tool_calls": [{"id": "c1", "name": "list_dir", "args": "{}"}]})
        await hub.send(s.id, "长任务")
        await until(lambda: len(seen) == 1)
        if case == "subtask":
            s.parent_id = "parent"
        if case == "stopping":
            s.stop_event.set()
        before = disk(hub, s)
        with pytest.raises(ValueError):
            await hub.steer(s.id, "" if case == "empty" else "补一句")
        after = disk(hub, s)
        s.parent_id = ""
        s.stop_event.set()
        gate.set()
        await s.task
        return before, after, s

    before, after, s = asyncio.run(main())
    assert before == after and not s.steer


def test_pending_steer_survives_a_restart_as_a_queued_item(tmp_path):
    """服务重启时还有没交出去的补充: 那一轮已经断了, 读回来排到队首, 不丢、不自己开始."""
    s = Session("restart", str(tmp_path))
    s.run = {"id": "r1", "state": "running"}
    s.queue = [{"id": "q1", "text": "原来排着的", "materials": [], "created": 1}]
    s.steer = [{"id": "s1", "text": "改用英文", "created": 2}]
    back = Session.from_json(json.loads(json.dumps(s.to_json())))
    assert back.run["state"] == "interrupted" and not back.steer
    assert [(x["id"], x["text"], x.get("from_steer")) for x in back.queue] == [("s1", "改用英文", True), ("q1", "原来排着的", None)]


def test_workbench_ws_steer_lands_between_tool_result_and_next_request(tmp_path):
    """整条链路: 工作台 WS 发 steer -> steer_ack; 她在等你点头 (那一步没做完) 时补的, 你点了头、工具跑完, 下一次请求里
    工具结果后面就是这句补充."""
    script = [call(("run_command", json.dumps({"command": "Write-Output hi"}))), say("Done, in English.")]

    async def scenario(client, fake, ws_dir, llm):
        st = await (await client.get("/api/agent")).json()
        assert st["steer"] is True  # 页面看这一项: 旧服务端没有就照旧排队, 那句话不会丢
        s = await (await client.post("/api/agent", json={"action": "new"})).json()
        ws = await client.ws_connect(f"/agent/ws?id={s['id']}")
        await ws.receive_json()
        await ws.send_json({"type": "send", "text": "跑一下命令然后总结"})
        got = await events_until(ws, lambda m: m.get("type") == "ask")
        await ws.send_json({"type": "steer", "text": "总结用英文"})
        got += await events_until(ws, lambda m: m.get("type") == "steer_ack")
        await ws.send_json({"type": "answer", "id": next(m for m in got if m["type"] == "ask")["id"], "how": "once"})
        got += await events_until(ws, lambda m: m.get("type") == "done")
        view = await (await client.get(f"/api/agent?id={s['id']}")).json()
        await ws.close()
        return got, fake.requests, view

    got, reqs, view = run_agent(tmp_path, script, scenario)
    ack = next(m for m in got if m["type"] == "steer_ack")
    assert ack["mode"] == "steer" and ack["item"]["text"] == "总结用英文"
    msgs = reqs[1]["body"]["messages"]
    assert msgs[-2]["role"] == "tool" and msgs[-1]["role"] == "user" and msgs[-1]["content"].startswith(STEER_HEAD)
    assert any(m["type"] == "user" and m.get("steer") and m["text"] == "总结用英文" for m in got)
    assert view["run"]["constraint_version"] == 1 and view["steer"] == [] and view["run"]["state"] == "completed"

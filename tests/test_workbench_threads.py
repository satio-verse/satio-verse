"""QQ / 微信上派到工作台的事一件一条线 (wm/channels/wbthreads.py; 2026-11-01 用户: "根据 ReActive 论文、仓库和调研,
继续完善、丰富我们的桌宠项目"). 论文的 Router / Worker: QQ 上的她接活, 工作台会话干活, 做完把结论交回来由她跟你说;
你引用那条回报再说话 = 这件事的后续, 直通那个会话.

以前派过去就不管了 (结果只能回电脑前在工作台页面上看), 空闲时还会把不相干的事接在同一个会话后面.
工作台的模型换成脚本 (不花钱); 最后一条走真的 QQ 渠道 (假协议端、假云端)."""
import asyncio
import json

from test_channels import OWNER, cloud_model, pmsg, reply_json, run_qq, until
from test_cloud import say
from wm.agent.core import AgentHub
from wm.app import AGENT, ENGINES
from wm.channels import wbthreads as WT


# ---------------------------------------------------------------- 回报说什么
def test_report_text_is_short_and_honest():
    goal = "比较三家的价格"
    done = WT.report_text(goal, {"state": "completed", "check": {"verdict": "ok"}},
                          "做完了。\n\n## 结论\n- **A 最便宜**，B 次之。\n```\nprint(1)\n```\n细节见 report.md")
    assert done == "主人~ 你让我用工作台办的「比较三家的价格」做完了: 结论 A 最便宜，B 次之。 细节见 report.md (要改就引用这条回我)"
    bad = WT.report_text(goal, {"state": "completed", "check": {"verdict": "needs_check",
                                                                 "reasons": ["核对以后 dist/app.zip 还是找不到"]}}, "做完了")
    assert "那边说做完了, 但我核对时没对上: 核对以后 dist/app.zip 还是找不到" in bad and "做完了:" not in bad
    budget = WT.report_text(goal, {"state": "budget_exhausted", "reason": "达到本轮 30 步预算"}, "我先看看目录")
    assert "做到这一轮的上限先停了 (达到本轮 30 步预算), 还没做完" in budget and "引用这条回我" in budget
    assert "没做成: 出错了: KeyError" in WT.report_text(goal, {"state": "failed", "reason": "出错了: KeyError: 'x'"}, "")
    assert WT.report_text(goal, {"state": "cancelled"}, "做完了") is None  # 你在电脑上按停的: 不发
    assert WT.report_text(goal, {"state": "interrupted"}, "") is None  # 服务端重启停下的: 不发 (提示里写着)
    long = WT.report_text("一" * 60, {"state": "completed"}, "很长的结论" * 40)
    assert len(long) < 160


def test_chatter_quote_is_not_a_follow_up():
    for text in ("谢谢~", "辛苦啦！", "好的", "收到", "嗯嗯", "👍"):
        assert WT.is_chatter(text), text
    for text in ("再加一家 D", "好的, 再把标题改成蓝色", "谢谢, 不过第二条不对"):
        assert not WT.is_chatter(text), text


# ---------------------------------------------------------------- 线: 真的工作台, 模型换成脚本
class FakeLLM:
    provider = "custom"

    async def close(self):
        pass


def make_hub(tmp_path, script):
    ws = tmp_path / "workspace"
    ws.mkdir()
    hub = AgentHub({"agent": {"workspace": str(ws)}}, home=tmp_path / "agent")
    hub._llm = lambda _s: FakeLLM()
    seen = []

    async def ask_model(sess, llm, tools):
        msgs = [{k: v for k, v in m.items() if not k.startswith("_")} for m in sess.messages]
        seen.append(msgs)
        out = script(msgs, len(seen))
        out = await out if asyncio.iscoroutine(out) else out
        return {"content": out.get("content", ""), "tool_calls": out.get("tool_calls", []), "reasoning": ""}
    hub._ask_model = ask_model
    return hub, seen


def last_user(msgs):
    return next(m["content"] for m in reversed(msgs) if m["role"] == "user")


async def wait(pred, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等不到条件")


def test_report_then_quote_continues_the_same_session(tmp_path):
    """做完了回报一次 (结论在前); 引用那条回报再说: 交给同一个会话接着做一轮, 做完再回报; 回报的编号重启以后还认."""
    def script(msgs, n):
        return {"content": "加上了 D，D 比 A 还便宜。" if "再加一家 D" in last_user(msgs) else "做完了：三家里 A 最便宜。"}

    hub, seen = make_hub(tmp_path, script)
    told = []

    async def report(t, text):
        told.append((t["sid"], text))
        return True, [f"m{len(told)}"]

    async def main():
        th = WT.Threads(tmp_path / "threads.json")
        th.attach(hub, report)
        s = hub.new_session()
        th.open(s.id, "比较三家的价格", "QQ", ("private", 42))
        assert await hub.send(s.id, "比较三家的价格") is None
        await wait(lambda: told)
        assert told == [(s.id, "主人~ 你让我用工作台办的「比较三家的价格」做完了: 三家里 A 最便宜。 (要改就引用这条回我)")]
        assert th.items[s.id]["state"] == "done" and th.items[s.id]["told"]
        assert th.quoted(["别的", "m1"]) == s.id and th.quoted(["别的"]) is None

        note = await th.follow(s.id, "再加一家 D", "QQ 上")
        assert "已经交给工作台那边 (那边接着做了)" in note and "别说已经改好了" in note
        await wait(lambda: len(told) == 2)
        assert told[1][1].endswith("做完了: 加上了 D，D 比 A 还便宜。 (要改就引用这条回我)")
        assert last_user(seen[-1]) == "（主人在QQ 上引用你刚才的回报, 接着说）再加一家 D"
        assert any("三家里 A 最便宜" in str(m.get("content")) for m in seen[-1])  # 同一个会话: 前一轮的话都在

        again = WT.Threads(tmp_path / "threads.json")  # 重启以后
        again.attach(hub, report)
        assert again.quoted(["m2"]) == s.id and again.items[s.id]["state"] == "done"

    asyncio.run(main())


def test_quote_while_running_joins_this_run_and_reports_once(tmp_path):
    """她正在做时你引用回报补了一句: 加到这一轮 (不另开一轮), 做完只回报一次."""
    gate = asyncio.Event()

    async def script(msgs, n):
        if n == 1:
            await gate.wait()
            return {"content": "做完了：初稿好了。"}
        assert "[做到一半补充的要求" in last_user(msgs)
        return {"content": "按你说的把标题改成蓝色了。"}

    hub, seen = make_hub(tmp_path, script)
    told = []

    async def report(t, text):
        told.append(text)
        return True, ["m1"]

    async def main():
        th = WT.Threads(None)
        th.attach(hub, report)
        s = hub.new_session()
        th.open(s.id, "做个海报", "QQ", ("private", 42))
        await hub.send(s.id, "做个海报")
        await wait(lambda: seen)
        note = await th.follow(s.id, "标题改成蓝色", "QQ 上")
        assert "这句加到这一轮里了" in note
        lines = th.lines()
        assert lines and "「做个海报」还在做" in lines[0]
        gate.set()
        await wait(lambda: told)
        await asyncio.sleep(0.1)
        assert len(seen) == 2 and told == ["主人~ 你让我用工作台办的「做个海报」做完了: 按你说的把标题改成蓝色了。 (要改就引用这条回我)"]

    asyncio.run(main())


def test_needs_check_stop_and_restart_are_told_honestly(tmp_path):
    """三层验收没过: 回报照实说核对没对上; 你按停的: 不发, 提示里写"按停了"; 重启时还在做的: 提示里写停在半路."""
    def script(msgs, n):
        return {"content": "做完了，安装包生成到 dist/app.zip。"}

    hub, seen = make_hub(tmp_path, script)
    told = []

    async def report(t, text):
        told.append(text)
        return True, []

    async def main():
        th = WT.Threads(tmp_path / "threads.json")
        th.attach(hub, report)
        s = hub.new_session()
        th.open(s.id, "打个安装包", "QQ", ("private", 42))
        await hub.send(s.id, "打个安装包")
        await wait(lambda: told)
        assert "「打个安装包」工作台那边说做完了, 但我核对时没对上: 提到写好的 dist/app.zip 在工作区里找不到" in told[0]
        assert th.items[s.id]["state"] == "needs_check"

        s2 = hub.new_session()
        th.open(s2.id, "整理下载文件夹", "QQ", ("private", 42))
        await th._done(s2.id, {"state": "cancelled"})
        assert len(told) == 1 and th.items[s2.id]["state"] == "stopped"

        s3 = hub.new_session()
        th.open(s3.id, "跑一遍测试", "QQ", ("private", 42))  # 还没等到结束事件, 服务端就重启了
        later = WT.Threads(tmp_path / "threads.json")
        later.attach(hub, report)
        line = later.lines()[0]
        assert "「打个安装包」那边说做完了, 但核对没对上: 提到写好的 dist/app.zip 在工作区里找不到" in line
        assert "「整理下载文件夹」主人在电脑上按停了" in line
        assert "「跑一遍测试」做到一半服务端重启, 停在半路了" in line

    asyncio.run(main())


def test_unsent_report_is_mentioned_once_and_queued_work_waits(tmp_path):
    """回报没发出去 (QQ 没连上): 她下次跟你说话时提示里标"结果还没发给主人", 只标一次; 她自己过日子时看到的不算提过.
    做完了但还排着要接着做的、后台作业还在跑的: 先不回报."""
    hub, seen = make_hub(tmp_path, lambda msgs, n: {"content": "做完了：周报写好了，在 周报.md。"})
    calls = []

    async def offline(t, text):
        calls.append(text)
        return False, []

    async def main():
        th = WT.Threads(None)
        th.attach(hub, offline)
        s = hub.new_session()
        th.open(s.id, "写周报", "QQ", ("private", 42))
        s.queue.append({"id": "q1", "text": "再加一段", "materials": [], "created": 0})
        await th._done(s.id, {"state": "completed", "check": {"verdict": "ok"}})
        assert calls == [] and th.items[s.id]["state"] == "running"  # 还排着: 等真做完
        s.queue.clear()
        hub.job_watch.entries = lambda: [{"job": "j1", "session": s.id, "run": "r1"}]
        await th._done(s.id, {"state": "completed", "check": {"verdict": "ok"}})
        assert calls == []  # 她起的后台作业还在跑 (跑完会交回来接着做): 也等
        hub.job_watch.entries = lambda: []
        s.messages.append({"role": "assistant", "content": "做完了：周报写好了。"})
        await th._done(s.id, {"state": "completed", "check": {"verdict": "ok"}})
        assert len(calls) == 1 and not th.items[s.id]["told"]
        life = th.lines(mark=False)
        assert "「写周报」做完了: 周报写好了。; 结果还没发给主人, 找机会跟他说" in life[0]
        first = th.lines()
        assert "结果还没发给主人" in first[0]
        assert "结果还没发给主人" not in th.lines()[0]  # 提过一次就照常写结论

    asyncio.run(main())


# ---------------------------------------------------------------- 真的 QQ 渠道: 派活、回报、引用接着做
def test_qq_dispatch_report_and_quote_follow_up(tmp_path):
    """主人在 QQ 上说"用工作台…" -> 开一个会话做; 做完了她在 QQ 上回报一句; 主人引用那条回报说"再加一家D" -> 同一个会话
    接着做, 做完再回报; 引用回报只说"谢谢~"的照常聊, 不再开一轮. 她回主人时提示里有工作台那边的实际状态."""
    (tmp_path / "ws").mkdir()

    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "model": cloud_model(base)})
        await inbox.get("channel_config_done")
        hub = bot.app[AGENT]
        turns = []

        async def ask_model(sess, llm, tools):
            text = next(m["content"] for m in reversed(sess.messages) if m["role"] == "user")
            turns.append((sess.id, text))
            return {"content": "加上了 D，D 最便宜。" if "再加一家D" in text else "做完了：三家里 A 最便宜。",
                    "tool_calls": [], "reasoning": ""}
        hub._llm, hub._ask_model = (lambda _s: FakeLLM()), ask_model
        ch = bot.app[ENGINES].channels

        await bot.push(pmsg(OWNER, "用工作台帮我比较一下三家的价格", 1))
        await until(lambda: any("做完了: 三家里 A 最便宜" in t for t in bot.texts()), 10)
        await until(lambda: ch.wb.quotes, 5)  # 协议端回了这条的消息编号 (引用它就是这件事的后续)
        report_mid = next(iter(ch.wb.quotes))
        follow = pmsg(OWNER, "再加一家D", 2)
        follow["message"].insert(0, {"type": "reply", "data": {"id": report_mid}})
        await bot.push(follow)
        await until(lambda: any("做完了: 加上了 D，D 最便宜" in t for t in bot.texts()), 10)
        n_turns = len(turns)
        thanks = pmsg(OWNER, "谢谢~", 3)
        thanks["message"].insert(0, {"type": "reply", "data": {"id": report_mid}})
        await bot.push(thanks)
        await until(lambda: len(cloud.requests) >= 3, 10)
        await asyncio.sleep(0.3)
        sessions = [s for s in hub.sessions.values() if s.title.startswith("QQ：")]
        return turns, n_turns, sessions, bot.texts(), cloud.requests

    turns, n_turns, sessions, texts, requests = run_qq(
        tmp_path, scenario, f"agent.workspace={json.dumps(str(tmp_path / 'ws'))}",
        cloud_script=[say(reply_json("好嘞，交给工作台了")), say(reply_json("收到~")), say(reply_json("不客气~"))])
    assert len(sessions) == 1 and sessions[0].title == "QQ：帮我比较一下三家的价格"
    assert [sid for sid, _ in turns] == [sessions[0].id] * 2 and n_turns == 2  # 两轮都在同一个会话; "谢谢~"没再开一轮
    assert turns[1][1] == "（主人在QQ 上引用你刚才的回报, 接着说）再加一家D"
    reports = [t for t in texts if t.startswith("主人~ 你让我用工作台办的")]
    assert reports == ["主人~ 你让我用工作台办的「帮我比较一下三家的价格」做完了: 三家里 A 最便宜。 (要改就引用这条回我)",
                       "主人~ 你让我用工作台办的「帮我比较一下三家的价格」做完了: 加上了 D，D 最便宜。 (要改就引用这条回我)"]
    second = json.dumps(requests[1]["body"], ensure_ascii=False)  # 她回"再加一家D"那次
    assert "主人引用了你在工作台办的「帮我比较一下三家的价格」的回报接着说了一句, 已经交给工作台那边" in second
    assert "[你在工作台上帮主人办的事] 「帮我比较一下三家的价格」" in second
    third = requests[2]["body"]["messages"][-1]["content"]  # 引用回报只说"谢谢~": 照常聊, 没交给工作台
    assert "谢谢~" in third and "已经交给工作台那边" not in third
    assert "[你在工作台上帮主人办的事] 「帮我比较一下三家的价格」做完了: 加上了 D，D 最便宜。" in third

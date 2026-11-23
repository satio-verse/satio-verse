"""在聊天里交给她长期目标 (2026-11-01 用户: "好，接着做" —— dot 对照里排第 2 的"交给她一个目标"; 用户贴的 dot 官方介绍:
交给它一件事, 它在对话之间自己推进, 把要你审的成果和要你拍板的决定带回来; 你随时看它在做什么、给反馈、改方向或暂停).

QQ 上说"帮我盯着…有新的告诉我" -> 认出是长期目标 -> 建持续任务 (wm/agent/resident.py, 有预算), 记成一条线 (wbthreads, kind=goal).
每一轮"没变化"不打扰; 有值得说的进展 (tell)、要你定、做完了、卡住了 (含花到预算) 才回聊天; 引用那条再说话交给这件事
(在等你的接着做, 你的话下一轮开头交给她); 说"先别做了"就暂停; 做完了的再说当一件普通的后续. 她回你时提示里有这件事的步骤和进度.
工作台的模型换成脚本 (直接替换 _turn, 和 test_agent_resident 一样), 不花钱; 最后一条走真的 QQ 渠道 (假协议端、假云端)."""
import asyncio
import json

from test_channels import OWNER, cloud_model, pmsg, reply_json, run_qq, until
from test_cloud import say
from wm.agent.core import AgentHub
from wm.app import AGENT, ENGINES
from wm.channels import wbthreads as WT


# ---------------------------------------------------------------- 说什么、什么时候说
def test_goal_reports_and_stop_words():
    g = "盯着 UP 主的新视频"
    assert WT.goal_text(g, {"state": "scheduled", "summary": "看了一遍, 没有新的"}) is None  # 每一轮"没变化"不说
    assert WT.goal_text(g, {"state": "scheduled", "summary": "x", "tell": "UP 主更新了《新视频》"}) == \
        "主人~ 「盯着 UP 主的新视频」有新进展: UP 主更新了《新视频》"
    assert WT.goal_text(g, {"state": "waiting", "summary": "要不要我把视频转给你？"}) == \
        "主人~ 「盯着 UP 主的新视频」要你定一下: 要不要我把视频转给你？ (引用这条回我就行)"
    assert WT.goal_text(g, {"state": "completed", "summary": "转给你了"}) == "主人~ 你交给我的「盯着 UP 主的新视频」做完了: 转给你了"
    assert "花到预算了 (2 元)" in WT.goal_text(g, {"state": "blocked", "summary": "达到你设置的目标总预算", "budget_cny": 2})
    assert "卡住了: 网站打不开" in WT.goal_text(g, {"state": "blocked", "summary": "网站打不开"})
    for text in ("先别做了", "不用了，我自己来", "别管了", "停", "算了吧", "暂停一下"):
        assert WT.STOP_GOAL.search(text), text
    for text in ("不要买 A，买 B", "别忘了也看看评论区", "转给我吧"):
        assert not WT.STOP_GOAL.search(text), text


def test_steps_text_reads_her_plan():
    class S:
        plan = {}
        run = {"plan": {"steps": [{"title": "看新视频", "status": "doing"}, {"title": "整理评论", "status": "todo"},
                                  {"title": "列出 UP 主", "status": "done"}]}}
    assert WT.steps_text(S()) == "; 步骤: 在做「看新视频」, 排着「整理评论」, 做完 1 步"  # 这一轮还没列: 看上一轮留下的
    assert WT.steps_text(None) == ""


# ---------------------------------------------------------------- 真的持续任务, 一轮一轮推
def make(tmp_path, budget=2):
    work = tmp_path / "project"
    work.mkdir()
    hub = AgentHub({"agent": {"workspace": str(work)}, "billing": {"env_key": ""}}, home=tmp_path / "agent")
    told, plays, prompts = [], [], []
    th = WT.Threads(tmp_path / "threads.json")

    async def report(t, text):
        told.append(text)
        return True, [f"m{len(told)}"]
    th.attach(hub, report)
    hub.resident.watchers.append(th.goal_event)

    async def turn(s, message):  # 一轮: 照剧本报进展 (持续任务里正在跑的才报)
        prompts.append(message)
        item = hub.resident.for_session(s.id)
        play = plays.pop(0) if plays else None
        if play is None:
            s.messages.append({"role": "assistant", "content": "好的，按你说的改好了。"})
            return
        if play.get("plan"):
            s.plan = {"steps": play["plan"], "acceptance": ""}
        s.stats["cost"] += play.get("cost", 0.01)
        if item and item["state"] == "running":
            hub.resident.report(s, play["state"], play["summary"], play.get("delay", 3600), play.get("tell", ""))
    hub._turn = turn
    item = hub.resident.create(None, "盯着 UP 主的新视频, 有新的告诉主人", title="QQ：盯新视频", budget_cny=budget, origin="QQ")
    th.open(item["session_id"], "盯新视频", "QQ", ("private", 42), goal_id=item["id"])
    return hub, th, item, told, plays, prompts


async def round_(hub, item):
    hub.resident._change(item["id"], lambda v: v.update(next_at=0))  # 到点了
    await hub.resident.tick()
    for worker in list(hub.resident._workers.values()):
        await worker
    await asyncio.sleep(0.05)
    return hub.resident.get(item["id"])


def test_rounds_report_only_what_needs_you_and_your_reply_goes_to_the_next_round(tmp_path):
    hub, th, item, told, plays, prompts = make(tmp_path)

    async def main():
        plays.append({"state": "progress", "summary": "看了一遍, 没有新视频",
                      "plan": [{"title": "看新视频", "status": "doing"}, {"title": "有新的就告诉主人", "status": "todo"}]})
        got = await round_(hub, item)
        assert got["state"] == "scheduled" and told == []  # 没变化: 不打扰
        line = th.lines()[0]
        assert "「盯新视频」(长期目标) 在后台推进, 已经做了 1 轮, 下一轮" in line and "最近: 看了一遍, 没有新视频" in line
        assert "步骤: 在做「看新视频」, 排着「有新的就告诉主人」" in line

        plays.append({"state": "progress", "summary": "发现新视频", "tell": "UP 主更新了《新视频》"})
        await round_(hub, item)
        assert told == ["主人~ 「盯新视频」有新进展: UP 主更新了《新视频》"]

        plays.append({"state": "waiting", "summary": "要不要我把视频转给你？"})
        got = await round_(hub, item)
        assert got["state"] == "waiting" and told[-1].startswith("主人~ 「盯新视频」要你定一下: 要不要我把视频转给你？")
        assert th.quoted(["m2"]) == item["session_id"] and "等主人定" in th.lines()[0]

        note = await th.follow(item["session_id"], "转给我吧", "QQ 上")  # 引用"要你定"那条回: 交给这件事, 接着做
        assert "那边接着做了, 这句下一轮开头交给那边" in note
        got = hub.resident.get(item["id"])
        assert got["state"] == "scheduled" and [n["text"] for n in got["notes"]] == ["转给我吧"]

        plays.append({"state": "completed", "summary": "视频已经转给主人了"})
        await hub.resident.tick()  # 你的话不等到下一轮的点: 马上就排
        for worker in list(hub.resident._workers.values()):
            await worker
        await asyncio.sleep(0.05)
        assert "owner_notes" in prompts[-1] and "转给我吧" in prompts[-1]
        assert hub.resident.get(item["id"])["notes"] == []  # 交给她了
        assert told[-1] == "主人~ 你交给我的「盯新视频」做完了: 视频已经转给主人了"
        assert th.items[item["session_id"]]["state"] == "done"

        # 做完了还引用那条接着说: 当一件普通的后续, 那个会话接着做一轮, 做完按普通的事回报
        note = await th.follow(item["session_id"], "再把评论区也看看", "QQ 上")
        assert "已经交给工作台那边" in note
        s = hub.get(item["session_id"])
        await asyncio.sleep(0)
        if s.task:
            await s.task
        await asyncio.sleep(0.1)
        assert told[-1].startswith("主人~ 你让我用工作台办的「盯新视频」做完了: 按你说的改好了")

    asyncio.run(main())


def test_stop_word_pauses_and_budget_stop_is_told(tmp_path):
    hub, th, item, told, plays, prompts = make(tmp_path, budget=0.01)

    async def main():
        plays.append({"state": "progress", "summary": "看了一遍", "cost": 0.02})
        await round_(hub, item)
        got = await round_(hub, item)  # 花到预算: 这一轮不开始, 停下来说一声
        assert got["state"] == "blocked" and told == ["主人~ 「盯新视频」花到预算了 (0.01 元), 先停下了; 要接着做, 在工作台的持续任务里加点预算"]
        assert len(prompts) == 1

        note = await th.follow(item["session_id"], "先别做了", "QQ 上")
        assert "已经暂停" in note and hub.resident.get(item["id"])["state"] == "paused"
        await round_(hub, item)
        assert len(prompts) == 1 and "主人暂停了" in th.lines()[0]

    asyncio.run(main())


def test_same_goal_twice_is_not_created_twice_and_threads_survive_restart(tmp_path):
    hub, th, item, told, plays, prompts = make(tmp_path)
    assert th.duplicate("盯新视频")["goal_id"] == item["id"]
    again = WT.Threads(tmp_path / "threads.json")
    again.attach(hub, None)
    t = again.items[item["session_id"]]
    assert t["kind"] == "goal" and t["goal_id"] == item["id"]
    assert "(长期目标) 在后台推进, 已经做了 0 轮" in again.lines()[0]


# ---------------------------------------------------------------- 真的 QQ 渠道
def test_qq_owner_gives_a_goal_and_answers_its_question(tmp_path):
    """主人在 QQ 上说"帮我盯着…有新的就告诉我" -> 判断出是长期目标 -> 建持续任务, 她回一句收到; 第一轮要主人定, QQ 上问他;
    主人引用那条回"转给我吧" -> 第二轮带着这句做完, QQ 上说做完了."""
    (tmp_path / "ws").mkdir()
    goal = {"answer": "none", "code": "", "do": "goal", "topic": "盯着 UP 主小明的新视频, 有新的就告诉主人", "name": "",
            "purpose": "", "reasonable": "yes"}
    none = {"answer": "none", "code": "", "do": "none", "topic": "", "name": "", "purpose": "", "reasonable": "no"}

    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "model": cloud_model(base)})
        await inbox.get("channel_config_done")
        hub, ch = bot.app[AGENT], bot.app[ENGINES].channels
        prompts = []

        async def turn(s, message):
            prompts.append(message)
            item = hub.resident.for_session(s.id)
            if item and item["state"] == "running":
                if "转给我吧" in message:
                    hub.resident.report(s, "completed", "新视频已经转给主人了")
                else:
                    hub.resident.report(s, "waiting", "小明更新了一个新视频, 要不要我转给你？")
        hub._turn = turn

        await bot.push(pmsg(OWNER, "帮我盯着 UP 主小明的新视频，有新的就告诉我", 1))
        await until(lambda: any("要你定一下" in t for t in bot.texts()), 15)
        await until(lambda: ch.wb.quotes, 5)
        ask_mid = next(iter(ch.wb.quotes))
        follow = pmsg(OWNER, "转给我吧", 2)
        follow["message"].insert(0, {"type": "reply", "data": {"id": ask_mid}})
        await bot.push(follow)
        await until(lambda: any("做完了: 新视频已经转给主人了" in t for t in bot.texts()), 15)
        items = hub.resident.state()["items"]
        return prompts, items, bot.texts(), cloud.requests

    prompts, items, texts, requests = run_qq(
        tmp_path, scenario, f"agent.workspace={json.dumps(str(tmp_path / 'ws'))}",
        cloud_script=[say(json.dumps(goal, ensure_ascii=False)), say(reply_json("好嘞，我帮你盯着")),
                      say(reply_json("好，我去转"))] + [say(json.dumps(none))] * 3)
    assert len(items) == 1 and items[0]["title"] == "QQ：盯着 UP 主小明的新视频, 有新的就告诉主人"
    assert items[0]["origin"] == "QQ" and items[0]["budget_cny"] == 2 and items[0]["state"] == "completed"
    assert len(prompts) == 2 and "owner_notes" in prompts[1] and "转给我吧" in prompts[1]
    assert any(t.startswith("主人~ 「盯着 UP 主小明的新视频, 有新的就告诉主人」要你定一下: 小明更新了一个新视频, 要不要我转给你？") for t in texts)
    assert "主人~ 你交给我的「盯着 UP 主小明的新视频, 有新的就告诉主人」做完了: 新视频已经转给主人了" in texts
    first_reply = json.dumps(requests[1]["body"], ensure_ascii=False)
    assert "主人交给你一个长期目标「盯着 UP 主小明的新视频, 有新的就告诉主人」, 已经交给工作台那边分几轮在后台推进 (预算 2 元" in first_reply

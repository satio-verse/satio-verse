"""记忆看得见 (2026-11-01): 设置里的"记忆"页 —— 她记得关于你的每一条 (哪来的、哪一类、哪天说的、上次想起、改过 / 忘掉)、
候选、她的理解、最近几轮想起了什么、谁能看到; 在页面上忘掉一条、确认候选. 数据就是 Obsidian 里那几份文件."""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from test_pipeline import make_cfg
from wm.app import create_app
from wm.channels.store import owner_profile
from wm.memory import Memory

CAND = "- [ ] 用户下周要去北京出差 (来源: [[2026-10-24]] 14:02) #候选\n"


def seeded(tmp_path):
    m = Memory({"vault": str(tmp_path), "character": "小流"})
    m.init()
    m.start_session()
    m.observe("我叫小明，我最喜欢吃草莓", "记住啦！", [], "happy")
    m.observe("我不喜欢吃草莓了", "好哦", [], None)  # 改口: 旧的那行划掉, 注明更正
    facts = tmp_path / "小流" / "记忆" / "关于用户.md"
    facts.write_text(facts.read_text(encoding="utf-8") + "- 小明周末常去爬山\n", encoding="utf-8")  # 你自己写的
    (tmp_path / "小流" / "记忆" / "候选.md").write_text("# 候选\n\n" + CAND, encoding="utf-8")
    m.add_understanding("一起看视频的时候, 被在乎的感觉很重要", source="看视频", day="2026-10-30")
    return m


def test_overview_shows_where_each_memory_came_from_and_what_changed(tmp_path):
    m = seeded(tmp_path)
    r = m.recall("你还记得我不喜欢吃什么吗")
    assert r["line"] and "草莓" in r["line"]
    m.recall("举起右手")  # 动作指令不检索, 不进记录
    ov = m.overview()
    by = {f["text"]: f for f in ov["facts"]}
    assert by["用户叫小明"]["origin"] == "chat" and by["用户叫小明"]["status"] == "active" and by["用户叫小明"]["kind"] == "long"
    assert by["用户叫小明"]["said"] and "名字" in by["用户叫小明"]["tags"]
    old = by["用户喜欢吃草莓"]
    assert old["status"] == "corrected" and "更正为: 用户不喜欢吃草莓了" in old["note"]
    assert by["用户不喜欢吃草莓了"]["status"] == "active"
    assert by["小明周末常去爬山"]["origin"] == "you"
    assert [c["text"] for c in ov["candidates"]] == ["用户下周要去北京出差"] and ov["candidates"][0]["source"].startswith("2026-10-24")
    assert ov["understanding"] == [{"day": "2026-10-30", "source": "看视频", "text": "一起看视频的时候, 被在乎的感觉很重要"}]
    assert ov["relation"]["user_name"] == "小明" and ov["relation"]["turns"] == "2"
    assert len(ov["recalls"]) == 1  # 动作指令那一轮没有
    last = ov["recalls"][0]
    assert last["q"] == "你还记得我不喜欢吃什么吗" and last["hits"] and last["hits"][0]["file"].endswith("关于用户.md")
    assert "score" in last["hits"][0] and last["mode"] in ("bm25", "vector")
    assert by["用户不喜欢吃草莓了"]["kind"] == "long" and by["用户不喜欢吃草莓了"]["last"] is None  # 长期的不淡化, 不记"上次想起"


def test_forget_from_settings_only_touches_program_written_lines(tmp_path):
    m = seeded(tmp_path)
    assert m.forget("用户叫小明") == ["用户叫小明"]
    assert m.forget("小明周末常去爬山") == []  # 你自己写的不动 (在 Obsidian 里改)
    assert m.forget("没有这一条") == []
    by = {f["text"]: f for f in m.overview()["facts"]}
    assert by["用户叫小明"]["status"] == "forgotten" and "用户要求忘掉" in by["用户叫小明"]["note"]
    assert "小明" not in (m.recall("你还记得我喜欢吃什么吗")["line"] or "")
    assert m.recall("我叫什么名字")["answer"] == "你让我把这个忘掉了，我就不记着啦。"  # 和当面说"忘掉"一样
    text = (tmp_path / "小流" / "记忆" / "关于用户.md").read_text(encoding="utf-8")
    assert "~~用户叫小明" in text and "- 小明周末常去爬山" in text


def test_memory_api_marks_what_remote_chats_see_and_stays_local(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    seeded(vault)

    async def main():
        client = TestClient(TestServer(create_app(make_cfg(tmp_path / "logs", f"memory.vault={json.dumps(str(vault))}",
                                                           'memory.character="小流"'))))
        await client.start_server()
        try:
            got = await (await client.get("/api/memory")).json()
            evil = await client.get("/api/memory", headers={"Origin": "http://evil.example"})
            forgot = await client.post("/api/memory", json={"action": "forget", "text": "用户叫小明"})
            mine = await client.post("/api/memory", json={"action": "forget", "text": "小明周末常去爬山"})
            cid = got["candidates"][0]["id"]
            reviewed = await (await client.post("/api/memory", json={"action": "review", "id": cid, "ok": True})).json()
            after = await (await client.get("/api/memory")).json()
            forgot_body = await forgot.json()
        finally:
            await client.close()
        return got, evil.status, forgot.status, forgot_body, mine.status, reviewed, after

    got, evil, forgot_status, forgot, mine_status, reviewed, after = asyncio.run(main())
    assert got["enabled"] is True and evil == 403
    remote = {f["text"]: f["remote"] for f in got["facts"]}
    assert remote["用户叫小明"] and remote["用户不喜欢吃草莓了"]  # QQ / 微信和你私聊能看到: 名字和喜好
    assert not remote["用户喜欢吃草莓"] and not remote["小明周末常去爬山"]  # 改掉的、你自己写的笔记不给
    assert [v["where"] for v in got["visibility"]] == ["桌宠 (说话、打字)", "QQ / 微信和你私聊、工作台", "QQ 群、白名单里的别人"]
    assert "名字和喜好 (2 条)" in got["visibility"][1]["sees"]
    assert forgot_status == 200 and forgot == {"forgotten": ["用户叫小明"]} and mine_status == 400
    assert reviewed and not after["candidates"]
    assert any(f["text"] == "用户下周要去北京出差" and f["origin"] == "digest" for f in after["facts"])
    assert {f["text"]: f["status"] for f in after["facts"]}["用户叫小明"] == "forgotten"


def test_private_chat_says_only_become_candidates_until_you_confirm(tmp_path):
    """QQ / 微信私聊里你打的"我叫…""我喜欢…" (2026-11-01 用户: 让私聊里说的也记进记忆): 只进候选 (#私聊), 关于用户.md
    一个字节都不变; 你确认了才和当面说的一样记进去 (改名划掉旧的、名字记进关系、QQ 私聊时也认得). 已经记着的、已经在候选里的、
    点过"不要"的、问句不再提; 远程说"帮我忘掉"既不删你的记忆, 也不会把那句话当成新事实."""
    m = seeded(tmp_path)
    mem = tmp_path / "小流" / "记忆"
    facts_before = (mem / "关于用户.md").read_bytes()
    assert m.propose_said("我叫阿青，我喜欢吃榴莲，我住在杭州", "QQ 私聊") == ["用户叫阿青", "用户喜欢吃榴莲", "用户住在杭州"]
    assert (mem / "关于用户.md").read_bytes() == facts_before  # 没确认之前一个字节都不变
    cands = {c["text"]: c for c in m.candidates()}
    assert cands["用户叫阿青"]["private"] and cands["用户叫阿青"]["source"].startswith("QQ 私聊 ")
    assert not cands["用户下周要去北京出差"]["private"]  # 每晚整理出来的照旧
    assert m.propose_said("我叫阿青", "微信") == []  # 已经在候选里
    assert m.propose_said("我不喜欢吃草莓了", "QQ 私聊") == []  # 已经记着
    assert m.propose_said("我叫什么名字", "QQ 私聊") == []
    assert m.propose_said("帮我把我住在杭州这件事忘掉", "QQ 私聊") == []
    assert (mem / "关于用户.md").read_bytes() == facts_before  # 远程说"忘掉"不动你的记忆 (去设置的记忆页)

    m.review(cands["用户叫阿青"]["id"], True)
    m.review(cands["用户住在杭州"]["id"], False)
    assert m.propose_said("我住在杭州", "微信") == []  # 点过"不要"的不再提
    text = (mem / "关于用户.md").read_text(encoding="utf-8")
    assert "~~用户叫小明 (" in text and "更正为: 用户叫阿青" in text  # 和当面改名一样: 旧的那行划掉
    ov = m.overview()
    new = {f["text"]: f for f in ov["facts"]}["用户叫阿青"]
    assert new["origin"] == "private" and new["status"] == "active" and new["kind"] == "long"
    assert new["source"].startswith("QQ 私聊 ") and ov["relation"]["user_name"] == "阿青"
    assert "用户叫阿青" in owner_profile(mem / "关于用户.md")  # QQ / 微信私聊时她也认得
    r = m.recall("你还记得我叫什么吗")
    assert "阿青" in (r["line"] or "") + (r["answer"] or "")

    # 在 Obsidian 里勾上 [x] 也一样: 下一次检索前转正, 和当面说的一样记进 关于用户.md
    cand = mem / "候选.md"
    cand.write_text(cand.read_text(encoding="utf-8").replace("- [ ] 用户喜欢吃榴莲", "- [x] 用户喜欢吃榴莲"), encoding="utf-8")
    m.recall("最近怎么样")
    liked = {f["text"]: f for f in m.overview()["facts"]}["用户喜欢吃榴莲"]
    assert liked["origin"] == "private" and liked["file"] == "关于用户.md" and "用户喜欢吃榴莲" in owner_profile(mem / "关于用户.md")


def test_private_chat_candidates_have_their_own_daily_cap(tmp_path):
    """私聊里说的每天最多 daily_max 条; 和每晚整理出来的候选各算各的 (私聊说得多不挤掉整理出来的)."""
    m = seeded(tmp_path)
    m.daily_max = 2
    assert m.propose_said("我喜欢吃苹果，我喜欢吃香蕉，我喜欢吃橘子", "QQ 私聊") == ["用户喜欢吃苹果", "用户喜欢吃香蕉"]
    assert m.propose_said("我喜欢吃橘子", "QQ 私聊") == []  # 今天私聊的满了
    written, skipped = m.propose(f"{m.clock():%Y-%m-%d}", [{"text": "用户最近在学吉他", "times": ["21:20"]}], 1)
    assert written == ["用户最近在学吉他"] and not skipped

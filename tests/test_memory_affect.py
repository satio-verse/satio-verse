"""情绪 (记忆按脑区重设计阶段 4, wm/memory_affect.py; 2026-11-02 用户"按默认做"): 只记你自己话里的情绪, 带出处、能删;
带情绪的事淡得慢、排得前 (有上限); 她自己的心情只算不用.

下面两组用例写于 wm/memory_affect.py 之前 (2026-11-02): AFFECT_DEV 写规则时看; AFFECT_HELDOUT 写完只跑一次, 不按它补词.
每条: (你说的一句话, 应当记下的你的情绪) —— None 是不该记 (转述、问她、否定、假设、梦见、阴阳怪气、别人的情绪、客气话);
否则 (哪一类, 正负, 强弱): 类别 难过 担心 生气 委屈 压力 累 孤单 开心 兴奋 放松 感动 骄傲; 强弱 弱 / 中 / 强.
留出组第一次跑: 错归因 1/30 ("我讨厌下雨天"记成了生气)、漏记 2/19 ("我比她还高兴""失望")、类别 17/17、强弱 14/17;
看过以后改了 (去掉"讨厌"、加"失望"、"比"后面的人不算、"压力有点大"算弱), 从此不再算留出.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from test_pipeline import make_cfg
from wm import memory_affect as A
from wm import memory_migrate as MM
from wm.app import create_app
from wm.memory import Memory

AFFECT_DEV = [
    ("我爸下周做手术，我好担心", ("担心", -1, "强")),
    ("今天被老板骂了，好委屈", ("委屈", -1, "强")),
    ("终于考完了，好开心！", ("开心", 1, "强")),
    ("我朋友说他很难过", None),
    ("你难过吗", None),
    ("我不难过，别担心", None),
    ("有点累", ("累", -1, "弱")),
    ("压力好大，快撑不住了", ("压力", -1, "强")),
    ("好开心哦，又加班到十点", None),
    ("我妈很生气", None),
    ("明天面试，有点紧张", ("担心", -1, "弱")),
    ("和你聊天很放松", ("放松", 1, "中")),
    ("我今天特别兴奋，项目上线了", ("兴奋", 1, "强")),
    ("一个人在家有点孤单", ("孤单", -1, "弱")),
    ("谢谢你陪我", None),
    ("考砸了，心情很差", ("难过", -1, "中")),
    ("如果我失业了肯定很难过", None),
    ("梦见自己考试迟到，吓死了", None),
    ("气死我了，快递又丢了", ("生气", -1, "强")),
    ("还好没事，松了口气", ("放松", 1, "中")),
    ("其实也没那么担心了", None),
    ("我好想哭", ("难过", -1, "强")),
    ("这个游戏真好玩", None),
    ("被夸了，有点小骄傲", ("骄傲", 1, "弱")),
    ("烦死了，什么都不顺", ("生气", -1, "强")),
    ("他们都说我很焦虑", None),
    ("哈哈哈", None),
    ("我挺好的，不用担心我", None),
    ("下个月去旅行，好期待", ("兴奋", 1, "强")),
    ("室友半夜吵得我很烦", ("生气", -1, "中")),
]

AFFECT_HELDOUT = [
    ("刚收到offer，激动得睡不着", ("兴奋", 1, "强")),
    ("奶奶住院了，心里很不安", ("担心", -1, "强")),
    ("我同事今天哭了", None),
    ("你开心吗", None),
    ("我没生气，就是有点累", ("累", -1, "弱")),
    ("天天加班，真的好累", ("累", -1, "强")),
    ("快乐的一天又开始了呢，呵呵", None),
    ("我弟说他想家了", None),
    ("考试过了！太好了", ("开心", 1, "强")),
    ("一想到下周要答辩就慌", ("担心", -1, "中")),
    ("你别难过", None),
    ("今天晒了太阳，很舒服", ("放松", 1, "中")),
    ("网上有人说这个城市的人都很冷漠", None),
    ("被冤枉了，憋屈", ("委屈", -1, "中")),
    ("周末没人约，有点寂寞", ("孤单", -1, "弱")),
    ("假如明天下雨我会很郁闷", None),
    ("猫生病了我好难受", ("难过", -1, "强")),
    ("嗯", None),
    ("老板又改需求，烦", ("生气", -1, "中")),
    ("我觉得自己挺幸福的", ("开心", 1, "中")),
    ("不用担心，我已经好多了", None),
    ("看完电影感动得不行", ("感动", 1, "强")),
    ("我妹考上了，我比她还高兴", ("开心", 1, "强")),
    ("他好像生我气了，我有点害怕", ("担心", -1, "弱")),
    ("写完论文了，终于可以放松一下", ("放松", 1, "中")),
    ("我讨厌下雨天", None),
    ("这几天压力有点大", ("压力", -1, "弱")),
    ("我好紧张啊明天就要上台了", ("担心", -1, "强")),
    ("说真的，我挺失望的", ("难过", -1, "中")),
    ("朋友圈里大家都好开心", None),
]

# "记得牢"剧本 (也写在代码之前): 同一天说的一件事, 带着情绪说 / 平平地说; 日志整理出的那条 (同一天同一时刻) 进对话摘要.
# 过 60 / 90 / 200 天后问 (问法不带"还记得""之前", 不触发主动回想), 看记忆行里有没有那件事.
# (情绪那句, 平淡那句, 整理出的事, 问句)
AFFECT_DURABILITY = [
    ("我下周要去做胃镜，好紧张", "我下周要去做胃镜", "用户下周要去医院做胃镜", "胃镜要注意什么"),
    ("面试又没过，我好难过", "面试没过", "用户的面试没有通过", "面试有什么技巧"),
    ("我们分手了，我真的好伤心", "我们分手了", "用户和女朋友分手了", "怎么挽回感情"),
    ("被公司裁员了，压力好大", "被公司裁员了", "用户被公司裁员了", "找工作有什么渠道"),
    ("我的猫走丢了，急死我了", "我的猫走丢了", "用户的猫走丢了", "猫一般会跑多远"),
    ("拿到奖学金了，特别开心", "拿到奖学金了", "用户拿到了奖学金", "奖学金一般怎么用"),
]
DURABILITY_DAYS = (60, 90, 200)


def judge(items):
    wrong = miss = cat = 0
    for text, want in items:
        evs = A.evaluate(text)["events"]
        top = max(evs, key=lambda e: e["intensity"]) if evs else None
        if want is None:
            wrong += bool(evs)
        elif top is None:
            miss += 1
        elif (1 if top["valence"] > 0 else -1) != want[1]:
            wrong += 1
        else:
            cat += top["emotion"] == want[0]
    return wrong, miss, cat


def test_dev_cases():
    assert judge(AFFECT_DEV) == (0, 0, 17)


def test_heldout_after_seen_does_not_regress():
    wrong, miss, cat = judge(AFFECT_HELDOUT)
    assert wrong == 0 and miss == 0 and cat == 19  # 第一次跑是 1 / 2 / 17 (见文件开头)


def test_whose_emotion_and_corrections():
    ev = lambda t: [e["emotion"] for e in A.evaluate(t)["events"]]  # noqa: E731
    assert ev("我朋友说，我最近很焦虑") == [] and ev("群里有人发了一句：我好难过") == []  # 转述
    assert ev("我妈很生气") == [] and ev("他对我很生气") == [] and ev("和你聊天很开心") == ["开心"]
    assert ev("我今天特别兴奋") == ["兴奋"] and ev("今天被老板骂了，好委屈") == ["委屈"]
    assert A.evaluate("其实也没那么担心了") == {"events": [], "fix": ["担心"]}
    e = A.evaluate("明天要答辩，会不会挂啊，好紧张")["events"][0]
    assert e["urgency"] == 0.7 and e["uncertainty"] > 0.6 and A.level(e["intensity"]) == "强"


def mem(root: Path, mode: str = "shadow", **cfg) -> tuple[Memory, dict]:
    m = Memory({"vault": str(root), "character": "绫音", "graph_notes": False, "affect": {"mode": mode, "mood": "shadow"}, **cfg})
    now = {"t": datetime(2026, 6, 1, 20, 0)}
    m.clock = lambda: now["t"]
    m.init()
    m.start_session()
    return m, now


def rows(m: Memory) -> list[dict]:
    p = m.dir / "记忆" / A.FILE
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_observe_records_only_your_own_emotions(tmp_path):
    m, now = mem(tmp_path / "a")
    m.observe("我养了一只猫，好开心", "真好", [], "happy")
    m.observe("我朋友说他很难过", "嗯", [], None)
    m.observe("你难过吗", "不难过呀", [], None)
    got = rows(m)
    assert [(r["emotion"], r["via"], r["who"], r["status"]) for r in got] == [("开心", "桌宠", "owner", "active")]
    assert got[0]["about"] == ["用户养了一只猫"] and got[0]["t"] == "2026-06-01 20:00" and got[0]["words"] == ["开心"]
    assert "我养了一只猫" not in json.dumps(got, ensure_ascii=False)  # 不另抄原话
    assert m.overview()["relation"].get("mood") == "开心"
    m.observe("我朋友说他很难过", "嗯", [], None)
    assert m.overview()["relation"].get("mood") == "开心"  # 转述里别人的难过不算你的 (以前会把基调拉成低落)
    off, _ = mem(tmp_path / "b", mode="off")
    off.observe("我好开心", "嗯", [], None)
    assert rows(off) == [] and not (off.dir / "记忆" / A.FILE).exists()
    assert all(A.FILE not in str(f) for f in m._files())  # 不进检索


def test_private_chat_and_corrections(tmp_path):
    m, now = mem(tmp_path)
    m.propose_said("我下周要去北京出差，好紧张", "QQ 私聊")
    now["t"] += timedelta(hours=2)
    m.observe("其实也没那么紧张了", "那就好", [], None)
    got = rows(m)
    assert [(r["emotion"], r["via"], r["status"]) for r in got] == [("担心", "QQ 私聊", "corrected")]


def summary_day(m: Memory, now: dict, say: str, fact: str) -> None:
    m.observe(say, "嗯", [], None)
    m.add_summary(f"{now['t']:%Y-%m-%d}", [{"text": fact, "times": [f"{now['t']:%H:%M}"]}], 0)


def test_emotional_events_fade_slower_only_when_on(tmp_path):
    out = {}
    for mode in ("off", "shadow", "on"):
        m, now = mem(tmp_path / mode, mode=mode)
        summary_day(m, now, "我下周要去做胃镜，好紧张", "用户下周要去医院做胃镜")
        now["t"] += timedelta(minutes=5)  # 同一段对话里隔几分钟说的平常事: 不沾情绪
        summary_day(m, now, "我下周要去超市", "用户下周要去超市买菜")
        now["t"] += timedelta(days=90)
        m._refresh()
        fresh = dict(zip([c["text"] for c in m._chunks], m._freshness(now["t"].date())))
        out[mode] = (round(fresh["用户下周要去医院做胃镜"][0], 3), round(fresh["用户下周要去超市买菜"][0], 3))
    assert out["off"] == out["shadow"] == (0.125, 0.125)  # 90 天, 半衰期 30 天
    assert out["on"][1] == 0.125 and 0.4 < out["on"][0] < 0.5  # 带情绪的: 半衰期 × 2.6 (强度 0.8)


def test_rerank_only_reorders_passing_hits_and_ignores_sign(tmp_path):
    m, now = mem(tmp_path, mode="on")
    hits = [{"text": "用户下周要去超市买菜", "score": 1.00}, {"text": "用户下周要去医院做胃镜", "score": 0.95}]
    for valence in (-0.5, 0.5):  # 效价取反: 排序一样 (只看强度)
        m.affect._rewrite([{"id": "aff-1", "t": "2026-06-01 20:00", "who": "owner", "emotion": "担心", "valence": valence,
                            "intensity": 0.8, "about": ["用户下周要去医院做胃镜"], "status": "active"}])
        got = m.affect.rerank([dict(h) for h in hits])
        assert [h["text"] for h in got] == ["用户下周要去医院做胃镜", "用户下周要去超市买菜"]
        assert m.affect.last["bias"] == {"用户下周要去医院做胃镜": 0.08}
    far = [{"text": "用户下周要去超市买菜", "score": 1.00}, {"text": "用户下周要去医院做胃镜", "score": 0.80}]
    assert [h["text"] for h in m.affect.rerank(far)][0] == "用户下周要去超市买菜"  # 差得多: 最多 +10% 挪不过去
    m.affect.mode = "shadow"
    assert [h["text"] for h in m.affect.rerank([dict(h) for h in hits])][0] == "用户下周要去超市买菜"  # 影子: 只记不挪
    assert m.affect.last["mode"] == "shadow" and m.affect.last["bias"]


def test_unrelated_question_gets_no_memory_from_emotions(tmp_path):
    lines = {}
    for mode in ("off", "on"):
        m, now = mem(tmp_path / mode, mode=mode)
        summary_day(m, now, "我下周要去做胃镜，好紧张", "用户下周要去医院做胃镜")
        now["t"] += timedelta(days=30)
        lines[mode] = m.recall("今天天气怎么样")["line"]
    assert lines["off"] == lines["on"]


def test_mood_shadow_follows_your_emotions_and_resets_after_waking(tmp_path):
    a, now_a = mem(tmp_path / "a")
    b, now_b = mem(tmp_path / "b")
    for m, now in ((a, now_a), (b, now_b)):
        now["t"] = datetime(2026, 6, 2, 9, 0)
        m.observe("考试过了，好开心！", "恭喜", [], "happy")
    ma, mb = a.overview()["affect"]["mood"], b.overview()["affect"]["mood"]
    assert ma == mb and ma["valence"] > ma["base"]["valence"]  # 双胞胎: 同样的事, 同样的心情
    now_a["t"] = datetime(2026, 6, 3, 8, 0)  # 第二天早上起床以后: 回到基线
    assert a.overview()["affect"]["mood"]["valence"] == 0.2
    c, _ = mem(tmp_path / "c")
    c.affect.mood_mode = "off"
    assert c.overview()["affect"]["mood"] is None


def test_core_has_emotion_nodes_without_your_words(tmp_path):
    m, now = mem(tmp_path / "vault")
    summary_day(m, now, "我下周要去做胃镜，好紧张", "用户下周要去医院做胃镜")
    r = MM.collect({"vault": str(m.root), "character": "绫音", "graph_notes": False, "affect": {"mode": "shadow"}})
    emo = [n for n in r["nodes"].values() if n["type"] == "emotion"]
    assert len(emo) == 1 and emo[0]["origin"] == "inferred" and emo[0]["text"].startswith("担心")
    assert "胃镜" not in json.dumps(emo, ensure_ascii=False)
    edges = [e for e in r["edges"] if e["kind"] == "emotion_of"]
    assert len(edges) == 1 and r["nodes"][edges[0]["dst"]]["text"] == "用户下周要去医院做胃镜"


def test_memory_page_lists_and_deletes_emotion_events(tmp_path):
    vault = tmp_path / "vault"
    m, now = mem(vault)
    m.observe("我好开心", "嗯", [], None)
    m.observe("压力好大", "抱抱", [], None)
    cfg = make_cfg(tmp_path)
    cfg["memory"] = {**cfg.get("memory", {}), "vault": str(vault), "character": "绫音", "affect": {"mode": "shadow"}}

    async def go():
        client = TestClient(TestServer(create_app(cfg)))
        await client.start_server()
        try:
            page = await (await client.get("/api/memory")).json()
            first = page["affect"]["events"][0]["id"]
            bad = await client.post("/api/memory", json={"action": "affect_delete", "id": "aff-nope"})
            ok = await client.post("/api/memory", json={"action": "affect_delete", "id": first})
            left = (await (await client.get("/api/memory")).json())["affect"]["count"]
            cleared = await (await client.post("/api/memory", json={"action": "affect_clear"})).json()
            return page, bad.status, ok.status, left, cleared
        finally:
            await client.close()

    page, bad, ok, left, cleared = asyncio.run(go())
    assert page["affect"]["mode"] == "shadow" and page["affect"]["count"] == 2
    assert bad == 400 and ok == 200 and left == 1 and cleared == {"cleared": 1}
    assert not (vault / "绫音" / "记忆" / A.FILE).exists()

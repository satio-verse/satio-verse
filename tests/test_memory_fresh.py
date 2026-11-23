"""记忆鲜活度 (长期 / 事件、半衰期、想起一次就刷新) 和候选记忆 (日志整理出来的先等用户确认)."""
import asyncio
import json
from datetime import date, datetime, timedelta

import numpy as np

from test_pipeline import make_cfg, run as run_ws
from wm import memory_ops as O
from wm.memory import Memory, candidate_id

DAY0 = datetime(2026, 6, 1, 20, 0)


def _mem(tmp_path, now, **cfg):
    m = Memory({"vault": str(tmp_path), "character": "小流", **cfg})
    m.clock = lambda: now["t"]
    m.init()
    m.start_session()
    return m


def _at(now, days):
    now["t"] = DAY0 + timedelta(days=days, hours=1)


def test_memory_kind():
    assert O.memory_kind("用户叫小明", "", "facts") == O.LONG
    assert O.memory_kind("用户生日是3月12日", "", "summary") == O.LONG  # 带日子, 但是槽位
    assert O.memory_kind("用户10月28日要去医院复查", "", "facts") == O.EVENT  # "记住" 来的安排
    assert O.memory_kind("用户周末喜欢去钓鱼", "", "summary") == O.LONG
    assert O.memory_kind("用户的仓鼠叫团子", "", "summary") == O.LONG
    assert O.memory_kind("用户对花生过敏", "", "summary") == O.LONG
    assert O.memory_kind("用户和室友吵架了，不想说话", "", "summary") == O.EVENT
    assert O.memory_kind("用户和室友吵架了", "#auto-summary #长期", "summary") == O.LONG  # 手动改类别
    assert O.memory_kind("用户最喜欢的颜色是蓝色", "#事件", "summary") == O.EVENT
    assert O.memory_kind("明年春天想去京都看樱花", "", "note") == O.LONG  # 用户手写的不淡化
    assert O.memory_kind("用户: 我好累", "", "log") == O.EVENT


def test_age_and_freshness():
    today = date(2026, 9, 1)
    assert O.age_days("用户和室友吵架了", date(2026, 8, 2), today) == 30
    assert O.freshness(30, 30) == 0.5 and O.freshness(0, 30) == 1.0 and O.freshness(90, 0) == 1.0
    assert O.age_days("用户12月5日要考研", date(2026, 8, 1), today) == 0  # 还没到的安排不淡化
    assert O.age_days("用户8月20日去面试了", date(2026, 8, 1), today) == 12  # 从事情那天算
    assert O.age_days("用户和室友吵架了", date(2026, 6, 1), today, recalled=date(2026, 8, 30)) == 2  # 想起过
    assert O.with_age("用户最近失眠很严重", date(2026, 6, 1), today) == "用户最近失眠很严重 (3个月前说的)"
    assert O.with_age("用户最近失眠很严重", date(2026, 8, 20), today) == "用户最近失眠很严重"
    assert O.with_age("用户8月20日去面试了", date(2025, 8, 1), today) == "用户8月20日去面试了"  # 有日子的交给 with_time


def _seed_events(m):
    m.add_summary("2026-06-01", [{"text": "用户和室友吵架了，不想说话", "times": ["20:00"]},
                                 {"text": "用户最近在准备考研，压力很大", "times": ["20:01"]},
                                 {"text": "用户周末喜欢去钓鱼", "times": ["20:02"]}], 0)
    m.observe("我喜欢吃草莓", "嗯", [], "calm")


def test_old_events_fade_but_specific_questions_still_recall(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now, min_score=1.0, freshness={"refresh_on_recall": False})
    _seed_events(m)
    _at(now, 0)
    assert "室友" in m.recall("室友好烦")["line"]  # 擦边的问法, 新鲜的时候想得起来
    _at(now, 90)
    r = m.recall("室友好烦")
    assert r["line"] is None and r["faded"][0]["text"].startswith("用户和室友吵架了") and r["faded"][0]["age"] == 90
    r = m.recall("我和室友又吵架了")  # 问得准的照样想得起来, 带上多久以前说的
    assert "用户和室友吵架了，不想说话 (3个月前说的)" in r["line"] and r["hits"][0]["fresh"] == 0.12
    assert "室友" in m.recall("之前室友好烦来着")["line"]  # 主动回想 ("之前""还记得") 时不压低
    _at(now, 400)  # 长期的不淡化
    assert "草莓" in m.recall("你还记得我喜欢吃什么吗")["line"]
    assert "钓鱼" in m.recall("周末去哪玩")["line"]


def test_recall_refreshes_events(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now, min_score=1.0)
    _seed_events(m)
    _at(now, 90)
    assert m.recall("室友好烦")["line"] is None  # 没想起来的不算想起
    assert not (tmp_path / "小流" / "记忆" / ".recall.json").exists()
    assert "室友" in m.recall("我和室友又吵架了")["line"]
    rec = json.loads((tmp_path / "小流" / "记忆" / ".recall.json").read_text(encoding="utf-8"))
    assert rec == {"用户和室友吵架了，不想说话": {"last": "2026-08-30", "days": ["2026-06-01", "2026-08-30"]}}  # 说的那天算第一天
    _at(now, 91)
    assert "室友" in m.recall("室友好烦")["line"]  # 前一天刚想起过, 又鲜活了
    m2 = _mem(tmp_path, now, min_score=1.0)  # 重启后也记得
    assert "室友" in m2.recall("室友好烦")["line"]


class FakeEmbedder:
    name, label = "bge", "fake"

    def __init__(self, sims):
        self.sims = sims  # 记忆正文 -> 和问句的余弦相似度

    def encode(self, texts, query=False):
        if query:
            return np.array([[1.0, 0.0]])
        return [np.array([s, (1 - s * s) ** 0.5]) for s in (self.sims.get(t, 0.0) for t in texts)]


def test_vector_threshold_rises_with_age(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now, freshness={"refresh_on_recall": False})
    m.add_summary("2026-06-01", [{"text": "用户和室友吵架了", "times": ["20:00"]}], 0)
    m.observe("我讨厌下雨天", "记住了", [], "calm")
    m.embedder, m.min_sim = FakeEmbedder({"用户和室友吵架了": 0.52, "用户不喜欢下雨天": 0.52}), 0.48
    m._embed_missing()
    _at(now, 0)
    r = m.recall("室友的事")
    assert r["mode"] == "vector" and {h["text"] for h in r["hits"]} == {"用户和室友吵架了", "用户不喜欢下雨天"}
    _at(now, 90)  # 鲜活度 0.125, 门槛抬高 0.07: 0.52 不够了; 长期的照旧
    r = m.recall("室友的事")
    assert [h["text"] for h in r["hits"]] == ["用户不喜欢下雨天"] and r["faded"][0]["text"] == "用户和室友吵架了"
    assert r["hits"][0]["score"] == 0.52  # 长期记忆的分数不变
    assert len(m.recall("还记得室友的事吗")["hits"]) == 2  # 主动回想时门槛不抬高


def test_association_skips_faded_events(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now, freshness={"refresh_on_recall": False})
    m.add_summary("2026-06-01", [{"text": "用户的猫叫豆豆", "times": ["20:00"]},
                                 {"text": "用户带猫去打了疫苗", "times": ["20:01"]}], 0)
    _at(now, 10)
    assert "疫苗" in m.recall("豆豆最近好吗")["line"]  # 经"猫"联想到
    _at(now, 60)
    line = m.recall("豆豆最近好吗")["line"]
    assert "豆豆" in line and "疫苗" not in line  # 两个半衰期前的事件不再联想


def _cands(tmp_path):
    return (tmp_path / "小流" / "记忆" / "候选.md").read_text(encoding="utf-8")


def test_candidates_need_confirmation(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now, confirm={"daily_max": 3})
    seen = []
    m.listeners.append(lambda: seen.append(1))
    facts = [{"text": t, "times": ["20:0" + str(i)]} for i, t in
             enumerate(["用户下周要去北京出差", "用户妹妹在读大三", "用户最近在学吉他", "用户想养一只狗"])]
    written, over = m.propose("2026-06-01", facts, 4)
    assert written == ["用户下周要去北京出差", "用户妹妹在读大三", "用户最近在学吉他"] and over == ["用户想养一只狗"]
    assert "- [ ] 用户下周要去北京出差 (来源: [[2026-06-01]] 20:00) #候选" in _cands(tmp_path) and seen == [1]
    assert m._relation()[0]["digested"] == "2026-06-01 4"
    assert not (tmp_path / "小流" / "记忆" / "对话摘要.md").exists()
    assert m.recall("我要去北京出差")["line"] is None  # 候选不参与检索
    assert m.propose("2026-06-01", facts[:1], 5) == ([], [])  # 提过的不再提
    items = m.candidates()
    assert [c["text"] for c in items] == written and items[0]["day"] == "2026-06-01"
    assert items[0]["source"] == "2026-06-01 20:00" and items[0]["id"] == candidate_id("用户下周要去北京出差")

    r = m.review(items[0]["id"], True)  # 确认: 移到 对话摘要.md, 下一轮就想得起来
    assert r == {"ok": True, "id": items[0]["id"], "text": "用户下周要去北京出差", "confirmed": True, "left": 2}
    summary = (tmp_path / "小流" / "记忆" / "对话摘要.md").read_text(encoding="utf-8")
    assert "- 用户下周要去北京出差 (来源: [[2026-06-01]] 20:00) #auto-summary #已确认" in summary
    assert "北京出差" not in _cands(tmp_path)
    assert "北京出差" in m.recall("我要去北京出差")["line"]

    r = m.review(items[1]["id"], False)  # 不要: 划掉, 以后不再提
    assert r["confirmed"] is False and r["left"] == 1 and "- ~~用户妹妹在读大三" in _cands(tmp_path)
    assert m.propose("2026-06-02", [{"text": "用户妹妹在读大三", "times": ["09:00"]}], 1) == ([], [])
    assert m.review("nope", True)["ok"] is False
    assert len(seen) == 3


def test_obsidian_tick_confirms(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now)
    m.propose("2026-06-01", [{"text": "用户下周要去北京出差", "times": ["20:00"]},
                             {"text": "用户最近在学吉他", "times": ["20:01"]}], 2)
    seen = []
    m.listeners.append(lambda: seen.append(1))
    path = tmp_path / "小流" / "记忆" / "候选.md"
    path.write_text(_cands(tmp_path).replace("- [ ] 用户最近在学吉他", "- [x] 用户最近在学吉他"), encoding="utf-8")
    assert "吉他" in m.recall("我最近在学什么乐器来着")["line"]  # 下一次检索时转正
    assert "吉他" not in _cands(tmp_path) and "北京出差" in _cands(tmp_path) and seen == [1]
    assert "#已确认" in (tmp_path / "小流" / "记忆" / "对话摘要.md").read_text(encoding="utf-8")
    rec = json.loads((tmp_path / "小流" / "记忆" / ".recall.json").read_text(encoding="utf-8"))
    assert rec["用户最近在学吉他"] == {"last": "2026-06-01", "days": ["2026-06-01"]}  # 确认刷新鲜活度, 不算多提到一天


def test_auto_mode_writes_summary(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now, confirm={"mode": "auto"})
    assert m.propose("2026-06-01", [{"text": "用户下周要去北京出差", "times": ["20:00"]}], 1) == (["用户下周要去北京出差"], [])
    assert not (tmp_path / "小流" / "记忆" / "候选.md").exists() and m.candidates() == []


def test_digest_proposes_candidates(tmp_path):
    from wm.digest import Digest

    class FakeLLM:
        async def choice_probs(self, messages, options, id_slot=None):
            return {"是": 0.6, "否": 0.4}

    m = Memory({"vault": str(tmp_path), "character": "小流"})
    m.init()
    m.start_session()
    m.observe("我下周要去北京出差", "一路顺风", [], "calm")
    rec = asyncio.run(Digest(m, FakeLLM()).run_once())
    assert rec["to"] == "候选" and rec["written"] == ["用户下周要去北京出差"]
    assert "- [ ] 用户下周要去北京出差" in _cands(tmp_path)
    assert m.recall("我要去北京出差")["line"] is None


def test_ws_candidates_and_review(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    m = Memory({"vault": str(vault), "character": "绫音"})
    m.init()
    m.propose("2026-06-01", [{"text": "用户下周要去北京出差", "times": ["20:00"]}], 0)
    cfg = make_cfg(tmp_path, f"memory.vault={json.dumps(str(vault))}")

    async def scenario(ws, hello):
        assert hello["status"]["memory"]["candidates"] == 1 and hello["status"]["memory"]["confirm"] == "candidates"
        first = json.loads((await asyncio.wait_for(ws.receive(), 5)).data)  # 有候选: 紧跟着 hello 发一份
        await ws.send_json({"type": "memory_review", "id": first["items"][0]["id"], "ok": True})
        got = {}
        while len(got) < 2:
            msg = json.loads((await asyncio.wait_for(ws.receive(), 5)).data)
            got[msg["type"]] = msg
        await ws.send_json({"type": "memory_candidates"})
        again = json.loads((await asyncio.wait_for(ws.receive(), 5)).data)
        return first, got, again

    first, got, again = run_ws(cfg, scenario)
    assert first["type"] == "memory_candidates" and first["items"][0]["text"] == "用户下周要去北京出差"
    assert got["memory_review_done"]["ok"] and got["memory_review_done"]["confirmed"] and got["memory_review_done"]["left"] == 0
    assert got["memory_candidates"]["items"] == []  # 变了就推一份新列表
    assert again == {"type": "memory_candidates", "items": [], "mode": "candidates"}
    assert "#已确认" in (vault / "绫音" / "记忆" / "对话摘要.md").read_text(encoding="utf-8")


def test_ws_no_candidates_no_push(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = make_cfg(tmp_path, f"memory.vault={json.dumps(str(vault))}")

    async def scenario(ws, hello):
        await ws.send_json({"type": "ping", "id": 1})
        return json.loads((await asyncio.wait_for(ws.receive(), 5)).data)

    assert run_ws(cfg, scenario)["type"] == "pong"  # 没有候选时连上不多发消息

def _summary(tmp_path):
    return (tmp_path / "小流" / "记忆" / "对话摘要.md").read_text(encoding="utf-8")


def _rec(tmp_path):
    return json.loads((tmp_path / "小流" / "记忆" / ".recall.json").read_text(encoding="utf-8"))


def test_recalled_on_three_days_becomes_long_term(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now, min_score=1.0)
    _seed_events(m)
    _at(now, 10)
    assert "室友" in m.recall("我和室友又吵架了")["line"]
    assert "promoted" not in m.recall("室友又不理我了")  # 同一天想起几次只算一天
    assert _rec(tmp_path)["用户和室友吵架了，不想说话"]["days"] == ["2026-06-01", "2026-06-11"]
    _at(now, 20)
    r = m.recall("我和室友又吵架了")  # 说的那天 + 两个不同的日子 = 3 天
    assert r["promoted"] == ["用户和室友吵架了，不想说话"]
    assert "用户和室友吵架了，不想说话 (来源: [[2026-06-01]] 20:00) #auto-summary #长期 #常提" in _summary(tmp_path)
    m.recall("考研压力好大")  # 只想起过这一次的不升
    assert "准备考研，压力很大 (来源: [[2026-06-01]] 20:01) #auto-summary\n" in _summary(tmp_path).replace("\r", "")
    _at(now, 400)
    r = m.recall("室友好烦")  # 升成长期以后, 擦边的问法隔一年也想得起来, 也不再带"多久以前说的"
    assert r["line"] == "[记忆] 用户和室友吵架了，不想说话." and "fresh" not in r["hits"][0]
    assert "考研，压力很大 (一年多前说的)" in m.recall("考研压力好大")["line"]  # 没升的还是事件, 带着多久以前说的


def test_association_dates_and_manual_event_never_promote(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now)
    m.add_summary("2026-06-01", [{"text": "用户的猫叫豆豆", "times": ["20:00"]},
                                 {"text": "用户带猫去打了疫苗", "times": ["20:01"]},
                                 {"text": "用户6月20日要去面试", "times": ["20:02"]}], 0)
    path = tmp_path / "小流" / "记忆" / "对话摘要.md"
    path.write_text(path.read_text(encoding="utf-8") + "- 用户在学吉他 (来源: [[2026-06-01]] 20:03) #auto-summary #事件\n",
                    encoding="utf-8")
    for d in (1, 2, 3):
        _at(now, d)
        assert "疫苗" in m.recall("豆豆最近好吗")["line"]  # 联想带出来的: 刷新鲜活度, 不算提到
        assert "面试" in m.recall("我要去面试了")["line"]
        assert "吉他" in m.recall("我的吉他弦断了")["line"]
    rec = _rec(tmp_path)
    assert rec["用户带猫去打了疫苗"] == {"last": "2026-06-04", "days": ["2026-06-01"]}
    assert len(rec["用户6月20日要去面试"]["days"]) == 4 and len(rec["用户在学吉他"]["days"]) == 4
    assert "#长期" not in _summary(tmp_path)  # 带日子的安排、手动标了 #事件 的都不升


def test_mentions_tag_candidates_and_carry_over_on_confirm(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now)
    seen = []
    m.listeners.append(lambda: seen.append(1))
    m.propose("2026-06-01", [{"text": "用户最近在学吉他", "times": ["20:00"]}], 1)
    assert m.candidates()[0]["days"] == 1
    r = m.mentioned("2026-06-03", ["用户最近在学吉他", "用户叫小明", "没有这条"])  # 只认候选和对话摘要里的
    assert r == {"counted": ["用户最近在学吉他"], "promoted": [], "tagged": ["用户最近在学吉他"]} and len(seen) == 2
    assert "#候选 #常提" in _cands(tmp_path) and m.candidates()[0]["days"] == 2
    assert m.mentioned("2026-06-03", ["用户最近在学吉他"])["tagged"] == []  # 同一天不重复算
    m.mentioned("2026-06-05", ["用户最近在学吉他"])
    assert m.candidates()[0]["days"] == 3  # 候选再常提也要你确认
    m.review(m.candidates()[0]["id"], True)  # 确认时已经提到 3 天: 直接记成长期
    assert "- 用户最近在学吉他 (来源: [[2026-06-01]] 20:00) #常提 #auto-summary #已确认 #长期" in _summary(tmp_path)

    m.add_summary("2026-06-01", [{"text": "用户在准备考研", "times": ["20:05"]}], 1)  # 确认过的事件
    assert m.mentioned("2026-06-02", ["用户在准备考研"])["promoted"] == []
    assert m.mentioned("2026-06-09", ["用户在准备考研"])["promoted"] == ["用户在准备考研"]
    assert "用户在准备考研 (来源: [[2026-06-01]] 20:05) #auto-summary #长期 #常提" in _summary(tmp_path)


def test_digest_duplicate_on_another_day_counts(tmp_path):
    from wm.digest import Digest

    class FakeLLM:
        async def choice_probs(self, messages, options, id_slot=None):
            return {"是": 0.6, "否": 0.4}

    now = {"t": DAY0}
    m = _mem(tmp_path, now)
    d = Digest(m, FakeLLM())
    m.observe("我最近在学吉他", "加油", [], "happy")
    assert asyncio.run(d.run_once())["written"] == ["用户最近在学吉他"]
    _at(now, 2)
    m.observe("我最近在学吉他呢", "练得怎么样了", [], "happy")
    rec = asyncio.run(d.run_once())
    assert rec["written"] == [] and rec["rejected"][0]["same"] == "用户最近在学吉他"
    assert rec["mentioned"]["tagged"] == ["用户最近在学吉他"] and "#常提" in _cands(tmp_path)


def test_old_recall_file_format_still_works(tmp_path):
    now = {"t": DAY0}
    m = _mem(tmp_path, now, min_score=1.0)
    _seed_events(m)
    (tmp_path / "小流" / "记忆" / ".recall.json").write_text('{"用户和室友吵架了，不想说话": "2026-08-30"}', encoding="utf-8")
    m2 = _mem(tmp_path, now, min_score=1.0)
    _at(now, 91)
    assert "室友" in m2.recall("室友好烦")["line"]  # 旧格式里的"上次被想起"照样算
    assert _rec(tmp_path)["用户和室友吵架了，不想说话"] == {"last": "2026-08-31", "days": ["2026-08-30", "2026-08-31"]}

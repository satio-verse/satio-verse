"""关系记忆层: Obsidian 库里的 Markdown 读写、BM25 检索、接进对话流程."""
import asyncio
import json
from pathlib import Path

import pytest

from test_pipeline import collect, make_cfg, run as run_ws
from wm import intent as I
from wm.memory import Memory, extract_facts


def test_extract_facts_only_direct_statements():
    got = extract_facts("我叫小明，我最喜欢吃草莓，不过我讨厌下雨天。")
    assert ("名字", "用户叫小明") in got and ("偏好", "用户喜欢吃草莓") in got and ("偏好", "用户不喜欢下雨天") in got
    assert extract_facts("你喜欢什么颜色") == [] and extract_facts("举起右手") == []
    assert extract_facts("你还记得我喜欢吃什么吗") == []  # 问句不是事实
    assert extract_facts("我还养了一只橘猫") == [("事实", "用户养了一只橘猫")]
    assert extract_facts("你要记住，周五我要考试")[0] == ("事实", "周五用户要考试")  # "我"会被模型当成它自己


def test_memory_files_recall_and_forgetting(tmp_path):
    m = Memory({"vault": str(tmp_path), "character": "小流"})
    m.init()
    m.start_session()
    assert (tmp_path / "小流" / "README.md").exists() and (tmp_path / "小流" / "关系.md").exists()
    assert m.recall("你好", first=True)["line"] == "[记忆] 第一次见面."
    assert m.observe("我叫小明，我最喜欢吃草莓", "记住啦！", [{"do": "nod"}], "happy") == ["用户叫小明", "用户喜欢吃草莓"]
    assert m.observe("我最喜欢吃草莓", "嗯嗯", [], "happy") == []  # 记过的不重复记
    facts = (tmp_path / "小流" / "记忆" / "关于用户.md").read_text(encoding="utf-8")
    assert "用户喜欢吃草莓" in facts and "#auto-memory" in facts
    log = next((tmp_path / "小流" / "日志").glob("*.md")).read_text(encoding="utf-8")
    assert "用户: 我叫小明" in log and "动作: nod" in log
    rel = (tmp_path / "小流" / "关系.md").read_text(encoding="utf-8")
    assert "user_name: 小明" in rel and "turns: 2" in rel and "sessions: 1" in rel

    r = m.recall("你还记得我喜欢吃什么吗")
    assert r["line"] and "草莓" in r["line"] and r["hits"][0]["file"] == "小流/记忆/关于用户.md"
    assert "小明" in m.recall("嗨", first=True)["line"]  # 第一轮带关系概况
    # 用户自己在库里写的笔记也能检索到
    (tmp_path / "旅行计划.md").write_text("# 旅行\n\n明年春天想去京都看樱花。\n", encoding="utf-8")
    assert "京都" in m.recall("我们之前聊过去哪里旅行")["line"]
    # 在 Obsidian 里删掉一行, 下一轮就忘了
    path = tmp_path / "小流" / "记忆" / "关于用户.md"
    path.write_text("\n".join(x for x in path.read_text(encoding="utf-8").splitlines() if "草莓" not in x), encoding="utf-8")
    assert "草莓" not in (m.recall("你还记得我喜欢吃什么吗")["line"] or "")


def test_memory_write_is_atomic_and_keeps_previous_version(tmp_path, monkeypatch):
    """整份改写先写临时文件再换: 换的那一步失败时旧文件一个字节都不变, 上一版留在 .backup/."""
    from wm import memory as M

    m = Memory({"vault": str(tmp_path), "character": "小流"})
    m.init()
    m.start_session()
    m.observe("我住在南京", "嗯", [], None)
    facts = tmp_path / "小流" / "记忆" / "关于用户.md"
    before = facts.read_bytes()
    real_replace = M.os.replace

    def fail_replace(src, dst):
        if Path(dst) == facts:
            raise OSError("模拟断电")
        return real_replace(src, dst)

    monkeypatch.setattr(M.os, "replace", fail_replace)
    with pytest.raises(OSError):
        m.observe("我们全家搬到苏州了", "嗯", [], None)
    assert facts.read_bytes() == before  # 旧内容没被截断
    assert not list((tmp_path / "小流").rglob("*.tmp"))  # 临时文件收拾掉了
    backup = tmp_path / "小流" / ".backup" / "记忆" / "关于用户.md.bak"
    assert backup.read_bytes() == before


def test_memory_backups_are_previous_versions_and_not_recalled(tmp_path):
    """每次整份改写前留上一版 (.backup/ 下的 .bak); 它不进检索, 所以旧事实不会又被想起来."""
    m = Memory({"vault": str(tmp_path), "character": "小流"})
    m.init()
    m.start_session()
    m.observe("我住在南京", "嗯", [], None)
    m.observe("我们全家搬到苏州了", "嗯", [], None)

    facts = tmp_path / "小流" / "记忆" / "关于用户.md"
    backup = tmp_path / "小流" / ".backup" / "记忆" / "关于用户.md.bak"
    assert "用户住在南京" in backup.read_text(encoding="utf-8")
    assert "用户住在苏州" not in backup.read_text(encoding="utf-8")
    assert "用户住在苏州" in facts.read_text(encoding="utf-8")
    m._refresh()
    assert all("南京" not in c["text"] for c in m._chunks)  # 备份里那条没进索引
    assert all(".backup" not in str(f) for f in m._files())


def test_memory_line_goes_after_user_text_and_before_state():
    msgs = I.build_messages("你好", state="[当前] 你在用户面前, 站着; 手里没拿东西.", memory="[记忆] 用户叫小明.")
    assert msgs[-1]["content"] == "你好\n[记忆] 用户叫小明.\n[当前] 你在用户面前, 站着; 手里没拿东西."
    assert I.split_state(msgs[-1]["content"]) == ("[当前] 你在用户面前, 站着; 手里没拿东西.", "你好")


def test_memory_in_dialogue_loop(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = make_cfg(tmp_path, f"memory.vault={json.dumps(str(vault))}")

    async def first(ws, hello):
        assert hello["status"]["memory"]["enabled"]
        await ws.send_json({"type": "text", "text": "我叫小明，我最喜欢吃草莓"})
        log = await collect(ws, until_trace_turn=1)
        await asyncio.sleep(0.3)  # 后台写库
        return log

    log = run_ws(cfg, first)
    meta = next(m for m in log if m.get("type") == "trace")["trace"]["meta"]["memory"]
    assert meta["line"] == "[记忆] 第一次见面." and meta["ms"] < 80
    assert "用户喜欢吃草莓" in (vault / "绫音" / "记忆" / "关于用户.md").read_text(encoding="utf-8")

    async def second(ws, hello):  # 重新连接 = 第二次见面
        await ws.send_json({"type": "text", "text": "你还记得我喜欢吃什么吗"})
        return await collect(ws, until_trace_turn=1)

    meta = next(m for m in run_ws(cfg, second) if m.get("type") == "trace")["trace"]["meta"]["memory"]
    assert "草莓" in meta["line"] and "第 2 次见面" in meta["line"] and "小明" in meta["line"]


def _seeded(tmp_path):
    m = Memory({"vault": str(tmp_path), "character": "小流"})
    m.init()
    m.start_session()
    for t in ["我叫小明", "我最喜欢吃草莓", "我讨厌下雨天", "我养了一只橘猫", "你要记住，周五我要考试", "我住在杭州"]:
        m.observe(t, "嗯", [], "happy")
    (tmp_path / "旅行计划.md").write_text("# 旅行\n\n明年春天想去京都看樱花。\n", encoding="utf-8")
    return m


def test_recall_no_false_hits(tmp_path):
    m = _seeded(tmp_path)
    # 问角色自己的事: "喜欢/叫/住" 不算命中, 不会捞出 "用户喜欢…"
    for q in ("你喜欢什么颜色", "你叫什么名字", "你住在哪里"):
        assert m.recall(q)["line"] is None, q
    # 只靠单字撞上 ("天气"的"天" 对 "下雨天"的"天") 不算命中; 指令、寒暄不带记忆
    for q in ("今天天气不错", "你好呀", "举起右手", "去沙发上坐一会儿", "我好累啊"):
        assert m.recall(q)["line"] is None, q
    for q, want in (("你还记得我喜欢吃什么吗", "草莓"), ("我叫什么", "小明"), ("我家的猫最近不爱吃饭", "橘猫"),
                    ("今天又下雨了", "下雨"), ("我们之前聊过去哪里旅行", "京都"), ("我考试没考好", "考试")):
        assert want in (m.recall(q)["line"] or ""), q
    line = m.recall("我叫什么")["line"]
    assert "#" not in line and "2026" not in line and "(" not in line  # 标签、记录日期不进提示词


def test_excluded_folder_not_recalled(tmp_path):
    m = _seeded(tmp_path)
    kb = tmp_path / "WorldModel知识库" / "模块"
    kb.mkdir(parents=True)
    (kb / "记忆.md").write_text("# 记忆层\n\n京都 樱花 草莓 考试 用 BM25 检索.\n", encoding="utf-8")
    assert all(not h["file"].startswith("WorldModel知识库") for h in m.recall("京都樱花草莓考试")["hits"])


def test_relation_stream(tmp_path):
    from datetime import datetime, timedelta

    m = Memory({"vault": str(tmp_path), "character": "小流"})
    m.init()
    m.start_session()
    m.observe("我今天考试没考好，好难过", "抱抱你", [], "sad")
    m.observe("你累不累", "不累呀", [], "happy")  # 问角色的不算用户情绪
    meta, body = m._relation()
    assert meta["mood"] == "低落" and meta["familiarity"] == "刚认识不久"
    # 假装上次见面是 5 天前, 认识了 20 天, 聊过 50 轮
    meta.update(last_seen=(datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M"),
                first_met=(datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d %H:%M"),
                turns="50", sessions="4", mood_date=(datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"))
    m._write_relation(meta, body)
    m.add_summary(datetime.now().strftime("%Y-%m-%d"), [{"text": "用户周五要考数学", "times": ["10:00"]}], 0)
    m.start_session()
    line = m.recall("你好", first=True)["line"]
    assert "第 5 次见面" in line and "已经比较熟了" in line and "5 天前" in line
    assert "上次聊天时用户情绪低落" in line and "最近聊到: 用户周五要考数学" in line
    m.observe("哈哈今天太开心了", "真好", [], "happy")
    assert m._relation()[0]["mood"] == "开心"  # 这次见面的情绪重新算


def test_digest_candidates_rules_and_rewrite():
    from wm.digest import candidates, decide, rewrite, rule_worth, select

    entries = [{"time": t, "user": u, "reply": "嗯", "acts": a} for t, u, a in (
        ("14:00", "举起右手", "raise"), ("14:01", "给我表演个后空翻", None), ("14:02", "你还记得我喜欢吃什么吗", None),
        ("14:03", "你好呀", None), ("14:04", "我今天考试没考好", None), ("14:05", "我今天考试没考好", None),
        ("14:06", "我是不是太胖了", None), ("14:07", "我刚才吃了一碗面", None), ("14:08", "我跟你说，下周我要去上海面试啊", None))]
    c = candidates(entries)
    # 指令、请求、问句不要; 同一句说两遍只留一次, 两个时间都算来源
    assert [(x["user"], x["times"]) for x in c] == [("我今天考试没考好", ["14:04", "14:05"]), ("我刚才吃了一碗面", ["14:07"]),
                                                    ("我跟你说，下周我要去上海面试啊", ["14:08"])]
    assert [rule_worth(x["user"]) for x in c] == [1, -1, 1]
    assert rule_worth("我手机没电了") == 0
    # 规则说值得记, 模型很确定不值得才否决; 规则说不准, 模型很确定值得才采用; 规则说是小事, 模型说什么都不要
    assert decide(1, 0.35) and not decide(1, 0.2) and not decide(0, 0.8) and decide(0, 0.9) and not decide(-1, 0.99)
    assert decide(1, None) and not decide(0, None)  # 没有模型时只看规则
    assert rewrite("我跟你说，下周我要去上海面试啊", "2026-10-23") == "下周用户要去上海面试"
    assert rewrite("我的猫叫豆豆，今天它生病了", "2026-10-23") == "用户的猫叫豆豆，10月23日它生病了"
    assert rewrite("我想你了", "2026-10-23") == "用户想绫音了"
    # 相对时间换成具体日期 (2026-10-22 是星期四), 长期记忆里不留"下周六"
    assert rewrite("我妹妹下周六结婚", "2026-10-22") == "用户妹妹10月31日结婚"
    assert rewrite("我下个月要搬家", "2026-12-05") == "用户2027年1月要搬家"
    ok, bad = select(c, [0.5, None, 0.6], "2026-10-23", known="")
    assert [(o["text"], o["times"]) for o in ok] == [("用户10月23日考试没考好", ["14:04", "14:05"]), ("下周用户要去上海面试", ["14:08"])]
    assert bad[0]["why"] == "一时的小事"
    known = "- 用户10月23日考试没考好 (来源: [[2026-10-23]] 14:04) #auto-summary"
    ok, bad = select(c, [0.5, None, 0.6], "2026-10-23", known=known)
    assert [o["text"] for o in ok] == ["下周用户要去上海面试"] and bad[0]["why"] == "重复"  # 已经记过的不再写


def test_digest_writes_summary_with_provenance(tmp_path):
    from wm.digest import Digest

    class FakeLLM:
        calls = []

        async def choice_probs(self, messages, options, id_slot=None):
            self.calls.append((messages[-1]["content"], id_slot))
            return {"是": 0.6, "否": 0.4}

    m = Memory({"vault": str(tmp_path), "character": "小流", "confirm": {"mode": "auto"}})  # 不经确认直接写 (原来的行为)
    (tmp_path / "小流" / "日志").mkdir(parents=True)
    (tmp_path / "小流" / "日志" / "2026-10-20.md").write_text(
        "# 2026-10-20 的对话\n\n- 09:00 用户: 我以前的事 → 小流: 嗯\n", encoding="utf-8")
    m.init()  # 第一次启用: 旧日志不补
    assert m._relation()[0]["digested"] == "2026-10-20 1" and m.pending_log() is None
    m.start_session()
    m.observe("举起右手", "好的", [{"do": "raise"}], "happy")
    m.observe("我明天要去医院复查", "要我陪你吗", [], "calm")
    llm = FakeLLM()
    d = Digest(m, llm, {"slot": 2})
    rec = asyncio.run(d.run_once())
    assert rec["written"] == [f"用户{_tomorrow()}要去医院复查"] and llm.calls[0][1] == 2  # 用槽位 2, 不挤对话的槽位 0
    assert len(llm.calls) == 1 and "举起右手" not in llm.calls[0][0]  # 指令不给模型看
    text = (tmp_path / "小流" / "记忆" / "对话摘要.md").read_text(encoding="utf-8")
    assert "要去医院复查 (来源: [[" in text and "#auto-summary" in text
    assert asyncio.run(d.run_once()) is None  # 整理过的不再整理
    # 用户在 Obsidian 里删掉这一条: 不会被写回来; 检索也就没有了
    path = tmp_path / "小流" / "记忆" / "对话摘要.md"
    path.write_text("\n".join(x for x in text.splitlines() if "复查" not in x), encoding="utf-8")
    assert asyncio.run(d.run_once()) is None and "复查" not in path.read_text(encoding="utf-8")
    assert "复查" not in (m.recall("我要去医院")["line"] or "")


def _tomorrow():
    from datetime import datetime, timedelta

    t = datetime.now() + timedelta(days=1)
    return f"{t.month}月{t.day}日"


def test_digest_yields_when_user_speaks(tmp_path):
    from wm.digest import Digest

    class SlowLLM:
        async def choice_probs(self, *a, **k):
            await asyncio.sleep(5)
            return {"是": 0.5, "否": 0.5}

    m = Memory({"vault": str(tmp_path), "character": "小流"})
    m.init()
    m.start_session()
    m.observe("我最近在学吉他弹唱", "好厉害", [], "happy")

    async def go():
        d = Digest(m, SlowLLM(), {"idle_s": 0, "check_s": 0.01})
        d.start()
        await asyncio.sleep(0.1)
        assert d._job is not None and not d._job.done()
        d.touch()  # 用户开口: 取消正在跑的这批
        await asyncio.sleep(0.05)
        yielded = d.stats["yielded"]
        await d.stop()
        return yielded

    assert asyncio.run(go()) == 1
    assert m.pending_log() is not None  # 这批没整理完, 进度没推进, 下次重来


class TopicEmbedder:
    """假嵌入模型: 按话题关键词给向量 (坚果和花生是同一个话题), 用来测检索流程, 不测模型好坏."""
    name, label = "fake", "fake"
    TOPICS = [("花生", "坚果", "过敏"), ("猫", "毛孩子", "豆豆"), ("杭州", "城市", "住")]

    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    def encode(self, texts, query=False):
        import numpy as np

        self.calls += 1
        if self.fail and query:
            raise OSError("嵌入服务关了")
        out = np.zeros((len(texts), len(self.TOPICS) + 1), np.float32)
        for i, t in enumerate(texts):
            for j, words in enumerate(self.TOPICS):
                out[i, j] = sum(w in t for w in words)
            out[i, -1] = 0.3
        return out / np.linalg.norm(out, axis=1, keepdims=True)


def test_vector_recall_paraphrase_guard_and_fallback(tmp_path):
    m = _seeded(tmp_path)  # 小明 / 草莓 / 下雨天 / 橘猫 / 周五考试 / 杭州
    m.add_summary("2026-10-22", [{"text": "用户对花生过敏", "times": ["20:07"]}], 0)  # 日志整理记下的
    m.embedder, m.min_sim = TopicEmbedder(), 0.8
    m._embed_missing()
    r = m.recall("我能吃坚果吗")  # 词面上一个字都不沾, 向量能找到
    assert r["mode"] == "vector" and "花生" in r["line"]
    assert m.recall("你喜欢吃坚果吗")["line"] is None  # 问角色自己的事: 不取关于用户的记忆
    assert m.recall("去窗户那边看看")["mode"] == "skip"  # 动作指令不检索
    # 新记下的事实马上有向量, 下一轮直接用向量检索
    n = len(m._vec)
    m.observe("我住在杭州的西湖边", "真好", [], "happy")
    assert len(m._vec) > n and m.recall("我在哪个城市")["mode"] == "vector"
    # 嵌入服务关了: 这一轮退回 BM25, 不报错
    m.embedder = TopicEmbedder(fail=True)
    r = m.recall("我叫什么")
    assert r["mode"] == "bm25" and "小明" in r["line"]


def test_vector_waits_for_index(tmp_path):
    m = _seeded(tmp_path)
    m.embedder, m.min_sim = TopicEmbedder(), 0.8  # 还没算向量: 先用 BM25, 后台补算
    assert m.recall("我叫什么")["mode"] == "bm25"
    m._vec_job.join(5)
    assert m.recall("我叫什么")["mode"] == "vector"

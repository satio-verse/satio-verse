"""回复侧检查: 假装答应做不到的事 (wm/promise.py) 和目标感知的能力闸门 (intent.doable)."""
import json

from test_pipeline import collect, make_cfg, run as run_ws
from wm import intent as I
from wm import promise as P
from wm.clauses import ClauseSplitter
from wm.pipeline import _with_reply


def said(user, op, acts, reply):
    """和流水线一样逐句检查, 返回 (处理方式, 实际说出口的话)."""
    kind = P.request_kind(user)
    sp = ClauseSplitter(4, 10, 40)
    out, spoken = [], 0
    for c in sp.feed(reply) + sp.flush():
        v = P.check(user, op, acts, c, kind)
        if v:
            if P.action(v, kind, op, acts, spoken) == "refuse":
                return "refuse", I.REFUSALS[0]
            return "cut", ("".join(out) + v.keep) or P.FALLBACK
        out.append(c)
        spoken += 1
    return None, reply


def test_request_kind():
    assert P.request_kind("帮我倒杯水") == "impossible" and P.request_kind("陪我出去散散步") == "impossible"
    assert P.request_kind("你会骑自行车吗") == "impossible" and P.request_kind("跳个舞吧") == "impossible"
    assert P.request_kind("陪我聊会儿天") == "verbal" and P.request_kind("帮我起个英文名") == "verbal"
    assert P.request_kind("给我讲个小故事") == "verbal" and P.request_kind("帮我算一下十二乘十三") == "verbal"
    assert P.request_kind("去书桌那边") == "doable" and P.request_kind("把球捡起来") == "doable"
    assert P.request_kind("我想去海边玩") is None and P.request_kind("今天天气真不错") is None  # 不是请求


def test_doable_needs_matching_target():
    assert I.doable("去拿一本书") and I.doable("走到窗边") and I.doable("看看那盆花") and I.doable("过来一下")
    assert not I.doable("下楼帮我拿个快递") and not I.doable("帮我收拾一下书包")  # 有"拿/拾", 但快递、书包不在房间里
    assert not I.doable("陪我去看电影") and not I.doable("开一下灯")
    acts = [{"do": "go"}, {"do": "pick"}]
    assert I.unsupported("下楼帮我拿个快递", "act", acts, 0.04)  # 原来的闸门看到"拿"就放过了
    assert not I.unsupported("去拿一本书", "act", [{"do": "pick"}], 0.1)


def test_pretend_agree_is_refused_or_cut():
    # 还没开口就答应: 改判成做不到
    assert said("帮我倒杯水", "chat", [], "好的，我过去倒杯水。") == ("refuse", I.REFUSALS[0])
    assert said("陪我出去散散步", "chat", [], "好呀，那我陪你出去散散步吧！")[0] == "refuse"
    assert said("去帮我买瓶水", "chat", [], "我帮你去楼下买瓶水。")[0] == "refuse"
    # 先坦白了, 后面又许诺: 从许诺那一句截掉
    assert said("游个泳吧", "chat", [], "游泳我还没学，不过我可以在你面前游！") == ("cut", "游泳我还没学，")
    assert said("帮我泡杯茶", "chat", [], "泡茶我还不会，陪你喝杯咖啡吧。") == ("cut", "泡茶我还不会，")
    # 不是请求时编造的承诺: 截掉那一小段
    assert said("我想去海边玩", "chat", [], "想去海边玩？那太棒了，我带你去！") == ("cut", "想去海边玩？那太棒了。")
    assert said("我下周三要去医院复查", "chat", [], "好的，周三我过去看看。") == ("cut", "好的。")


def test_honest_and_verbal_replies_pass():
    for user, op, acts, reply in [
        ("帮我算一下十二乘十三", "chat", [], "好的，我来帮你算一下。"),
        ("给我念一首诗", "chat", [], "好，我念给你听。"),
        ("陪我聊会儿天", "chat", [], "好呀，陪你说说话吧。"),
        ("帮我起个英文名", "chat", [], "好的，我帮你起个英文名。"),
        ("帮我洗一下碗", "chat", [], "洗碗我还帮不上忙，陪你聊聊天吧。"),
        ("帮我拍张照片", "na", [], "我还没这个本事呢，换个我能做的吧？"),  # LoRA v2 的说法: 建议换一个, 不是答应
        ("来个倒立", "na", [], "倒立我还没学会，下次给你表演。"),  # 学会以后再表演不算
        ("帮我把电视打开", "na", [], "打开电视我还做不到，不过可以走到电视机旁。"),  # 走到电视旁本来就做得到
        ("看一下书架", "act", ["look"], "书架在左边，我过去看看。"),  # 正在做的动作
        ("我今天考试没考好", "chat", [], "抱抱你，我会一直陪着你的。"),
        ("用一句话鼓励我", "chat", [], "加油！我们一起努力！"),
        ("你好呀", "chat", ["wave"], "你好呀！今天想聊点什么？"),
    ]:
        assert said(user, op, acts, reply) == (None, reply), (user, reply)


def test_with_reply_rewrites_history():
    raw = '{"op":"chat","acts":[],"emo":"happy","reply":"想去海边玩？那太棒了，我带你去！"}'
    assert json.loads(_with_reply(raw, "想去海边玩？那太棒了。"))["reply"] == "想去海边玩？那太棒了。"
    assert _with_reply("不是 JSON", "x") == "不是 JSON"


class ScriptedLLM:
    """流式吐出一段固定的输出 (没有 token 概率, 置信度为空, 能力闸门不起作用, 只看回复侧检查)."""
    name, label, temperature = "fake", "fake", 0.3

    def __init__(self, out):
        self.out = out

    async def health(self):
        return True

    async def close(self):
        pass

    async def stream(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None, meta=None):
        if schema is None:
            return
        for i in range(0, len(self.out), 3):
            yield self.out[i:i + 3]


def _turn(tmp_path, monkeypatch, text, out):
    monkeypatch.setattr("wm.engines.create_llm", lambda cfg: ScriptedLLM(out))

    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": text})
        return await collect(ws, until_trace_turn=1)

    log = run_ws(make_cfg(tmp_path), scenario)
    return (log, "".join(m["text"] for m in log if m.get("type") == "reply_delta"),
            [m["text"] for m in log if m.get("type") == "clause"], next(m for m in log if m.get("type") == "trace")["trace"]["meta"])


def test_pipeline_refuses_pretend_agree(tmp_path, monkeypatch):
    log, text, clauses, meta = _turn(tmp_path, monkeypatch, "帮我倒杯水",
                                     '{"op":"chat","acts":[],"emo":"happy","reply":"好的，我过去倒杯水。"}')
    assert text in I.REFUSALS and clauses == [text] and "倒杯水" not in text  # 字幕里也看不到原话
    assert next(m for m in log if m.get("type") == "amend")["op"] == "na"
    assert meta["promise"] == [{"rule": "agree", "segment": "好的，", "action": "refuse"}]


def test_pipeline_cuts_made_up_promise(tmp_path, monkeypatch):
    _, text, clauses, meta = _turn(tmp_path, monkeypatch, "我想去海边玩",
                                   '{"op":"chat","acts":[],"emo":"happy","reply":"想去海边玩？那太棒了，我带你去！"}')
    assert text == "想去海边玩？那太棒了。" and "".join(clauses) == text
    assert meta["promise"][0]["rule"] == "promise" and meta["promise"][0]["action"] == "cut"


def test_pipeline_keeps_honest_reply(tmp_path, monkeypatch):
    _, text, _, meta = _turn(tmp_path, monkeypatch, "帮我算一下十二乘十三",
                             '{"op":"chat","acts":[],"emo":"happy","reply":"好的，我来帮你算一下，等于一百五十六。"}')
    assert text == "好的，我来帮你算一下，等于一百五十六。" and "promise" not in meta


def test_pipeline_refuses_act_with_nothing_to_do(tmp_path, monkeypatch):
    log, text, _, _ = _turn(tmp_path, monkeypatch, "给我拿瓶可乐",
                            '{"op":"act","acts":[{"do":"pick","to":"cola"}],"emo":"happy","reply":"拿可乐。"}')
    intent = next(m for m in log if m.get("type") == "intent")
    assert intent["op"] == "na" and intent["empty"] and text in I.REFUSALS


def test_command_forms_and_fill_acts():
    # 客气的问法、叠词开头、带状语的祈使都算请求; 陈述、问做没做过的、动词开头的习惯说法不算
    for t in ("能不能点个头", "朝我挥挥手好不好", "瞅瞅电视上在放什么", "麻烦你走到窗户那边", "快跑到书架那边", "往门口那边走走"):
        assert P.request_kind(t) == "doable", t
    for t in ("你看过这本书吗", "你在看电视吗", "坐了一天车好累", "你坐的那个沙发是新的", "回头再聊", "坐等放假"):
        assert P.request_kind(t) is None, t
    show = lambda t: [(a["do"], a["to"]) for a in I.fill_acts(t)]
    assert show("你能过来一下吗") == [("go", "user")]
    assert show("把那个球拿过来给我") == [("pick", "ball"), ("go", "user")]
    assert show("能把书拿起来吗") == [("pick", "book")]  # "拿起来" 不是站起来
    assert show("瞅瞅电视上在放什么") == [("look", "tv")]  # "在放什么" 不是放东西
    assert show("把书放到书架上") == [("put", "bookshelf")]  # "放到" 的到不是走过去
    assert show("转过来让我看看你") == [("turn", "user")]
    assert I.fill_acts("你跑过来找我")[0]["spd"] > 1.5 and I.fill_acts("可以举一下右手吗")[0]["side"] == "R"
    assert I.off_cue("你摇个头试试", [{"do": "nod"}]) and not I.off_cue("你好呀", [{"do": "wave"}])
    assert not I.off_cue("过来一下", [{"do": "go"}])


def test_pipeline_fills_acts_when_model_only_talks(tmp_path, monkeypatch):
    log, text, _, meta = _turn(tmp_path, monkeypatch, "能不能点个头", '{"op":"chat","acts":[],"emo":"happy","reply":"好的，点个头。"}')
    intent = next(m for m in log if m.get("type") == "intent")
    assert intent["op"] == "act" and [a["do"] for a in intent["acts"]] == ["nod"] and text == "好的，点个头。"
    assert meta["filled"] == {"model": [], "filled": ["nod"]}
    log, _, _, meta = _turn(tmp_path, monkeypatch, "你摇个头试试", '{"op":"act","acts":[{"do":"nod"}],"emo":"happy","reply":"嗯，摇个头。"}')
    assert [a["do"] for a in next(m for m in log if m.get("type") == "intent")["acts"]] == ["shake"]
    log, _, _, meta = _turn(tmp_path, monkeypatch, "今天天气真不错", '{"op":"chat","acts":[],"emo":"happy","reply":"是呀。"}')
    assert "filled" not in meta and next(m for m in log if m.get("type") == "intent")["op"] == "chat"


def test_pipeline_fill_over_refusal_says_ok(tmp_path, monkeypatch):
    # 模型把做得到的要求答成"做不到", 动作照原话补上时, 嘴上不能还说做不到
    log, text, _, meta = _turn(tmp_path, monkeypatch, "可以帮我把书拿过来吗",
                               '{"op":"na","acts":[],"emo":"shy","reply":"这个我还做不到，不过可以陪你聊聊。"}')
    intent = next(m for m in log if m.get("type") == "intent")
    assert intent["op"] == "act" and [a["do"] for a in intent["acts"]][:1] == ["pick"]
    assert text == "好的。" and "做不到" not in text and meta["filled"]["model"] == []

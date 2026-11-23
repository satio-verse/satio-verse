"""意图协议: 可行性声明、执行结果回流、社交后果预测参与决策."""
import asyncio
import json

from test_confidence import FakeLLM, _tokens  # noqa: F401  (FakeLLM 按 llama-server 格式填 token 概率)
from test_pipeline import collect, make_cfg, run as run_ws
from wm import intent as I
from wm import promise as P
from wm.social import SocialModel


def _go_door():
    return [('{"op":"', None), ("act", {"act": 0.9, "chat": 0.1}), ('","acts":[{"do":"', None),
            ("go", {"go": 0.95, "look": 0.05}), ('","to":"', None), ("door", {"door": 0.9, "window": 0.1}),
            ('"}],"emo":"', None), ("happy", None), ('","reply":"', None)]


def test_exec_note_and_add_note():
    assert I.exec_note("go", "door") == "刚才没能走到门"
    assert I.exec_note("sit", "sofa").endswith("上")
    assert I.exec_note("rm -rf", "door") is None  # 不认识的动作不说
    assert I.exec_note("go", "<b>x</b>") == "刚才没能走到那里"
    assert I.add_note("[当前] 你在房间中间, 站着.", "刚才没能走到门") == "[当前] 你在房间中间, 站着; 刚才没能走到门."
    assert I.add_note(None, "刚才没能站起来") == "[当前] 刚才没能站起来."


def test_body_cannot_walk_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr("wm.engines.create_llm", lambda cfg: FakeLLM(_go_door()))

    async def scenario(ws, hello):
        await ws.send_json({"type": "capabilities", "skills": ["wave", "nod", "rm -rf"]})  # 2D 形象: 不能走路
        await ws.send_json({"type": "text", "text": "走到门口去"})
        return await collect(ws, until_trace_turn=1)

    log = run_ws(make_cfg(tmp_path), scenario)
    intent = next(m for m in log if m.get("type") == "intent")
    assert intent["op"] == "na" and intent["acts"] == [] and intent["body"] == ["go"]
    said = "".join(m["text"] for m in log if m.get("type") == "reply_delta")
    assert said == I.body_refusal(["go"]) == "走过去我现在还做不了，换个别的吧？"  # 按技能坦白, 不是"还没学会"


DESK_SKILLS = ["wave", "raise", "point", "nod", "shake", "shrug", "think", "bow"]  # 桌宠现在声明的 (desktop/web/talk.js)


class Scripted:
    """意图那次流式吐出一段固定的 JSON (没有 token 概率: 置信度为空, 能力闸门不起作用), 记下每次请求的消息和 schema."""
    name, label, temperature = "fake", "fake", 0.3

    def __init__(self, out):
        self.out, self.calls = out, []

    async def health(self):
        return True

    async def close(self):
        pass

    async def stream(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None, meta=None, extra=None):
        if schema is None:
            return
        self.calls.append((messages, schema))
        for i in range(0, len(self.out), 3):
            yield self.out[i:i + 3]


def _desk_turn(tmp_path, monkeypatch, text, out, skills=DESK_SKILLS, place=None):
    llm = Scripted(out)
    monkeypatch.setattr("wm.engines.create_llm", lambda cfg: llm)

    async def scenario(ws, hello):
        if skills is not None:
            await ws.send_json({"type": "capabilities", "skills": skills})
        if place:
            await ws.send_json({"type": "state", "state": {"place": place}})  # 桌宠只带 place
        await ws.send_json({"type": "text", "text": text})
        return await collect(ws, until_trace_turn=1)

    log = run_ws(make_cfg(tmp_path), scenario)
    said = "".join(m["text"] for m in log if m.get("type") == "reply_delta")
    return log, said, next(m for m in log if m.get("type") == "trace")["trace"]["meta"], llm


def test_state_with_only_place():
    assert I.describe_state({"place": "desktop"}) == "[当前] 你在用户的电脑桌面上, 站着."
    assert I.describe_state({"place": "desktop", "at": "sofa", "posture": "seated", "items": {"ball": {"held": "left"}}}) \
        == "[当前] 你在用户的电脑桌面上, 坐着."  # 桌面上没有房间里的位置和东西
    assert I.describe_state({"place": "desktop", "user": {"present": True, "waving": True}}).endswith("用户在镜头前, 在向你挥手.")
    assert I.describe_state({"place": "room"}) == I.describe_state({}) == "[当前] 你在房间里, 站着; 手里没拿东西."
    assert I.place_of({"place": "desktop"}) == "desktop" and I.place_of({"place": "<b>"}) == I.place_of(None) == "room"
    assert I.answer_state("你在哪", {"place": "desktop"}) == "我在你的电脑桌面上呀。"
    assert I.answer_state("你手里拿着什么", {"place": "desktop"}) == "我手里什么也没拿。"


def test_desktop_prompt_has_no_room():
    msgs = I.build_messages("你好", place="desktop")
    text = "\n".join(m["content"] for m in msgs)
    assert msgs[0]["content"] == I.DESK_PROMPT and "squat" in text
    names = {n for k, v in I.TARGETS.items() if k != "user" for n in v.split("/")}
    assert not [n for n in names if len(n) > 1 and n in text], [n for n in names if len(n) > 1 and n in text]
    for _, out in I.DESK_EXAMPLES:
        assert all(a["do"] in I.SKILLS and a["do"] not in I.SPATIAL_SKILLS and not a.get("to") for a in out["acts"])
    assert I.build_schema("desktop")["properties"]["acts"]["items"]["properties"]["to"]["enum"] == ["user"]
    assert I.build_messages("你好") == I.build_messages("你好", place="room")  # 房间的提示词没变
    assert I.SYSTEM_PROMPT in I.build_messages("你好")[0]["content"] and "squat" not in I.SYSTEM_PROMPT
    assert "先把用户这一轮说的话看清楚" in I.SYSTEM_PROMPT and "别急着接话茬" in I.SYSTEM_PROMPT
    assert "先把主人这一轮说的话看清楚" in I.DESK_PROMPT and "别为了接上话编一个意思" in I.DESK_PROMPT


def test_desktop_session_uses_desk_prompt(tmp_path, monkeypatch):
    log, said, meta, llm = _desk_turn(tmp_path, monkeypatch, "举起右手",
                                      '{"op":"act","acts":[{"do":"raise","side":"R"}],"emo":"happy","reply":"好的，举起来啦。"}',
                                      place="desktop")
    messages, schema = llm.calls[0]
    assert messages[0]["content"] == I.DESK_PROMPT and messages[-1]["content"] == "举起右手\n[当前] 你在用户的电脑桌面上, 站着."
    assert schema["properties"]["acts"]["items"]["properties"]["to"]["enum"] == ["user"]
    assert next(m for m in log if m.get("type") == "intent")["acts"][0]["do"] == "raise" and said == "好的，举起来啦。"


def test_squat_only_when_declared(tmp_path, monkeypatch):
    out = '{"op":"act","acts":[{"do":"squat","amp":0.5}],"emo":"happy","reply":"好，蹲下啦。"}'
    for skills in (None, ["wave", "nod", "go", "sit"]):  # 没声明 capabilities / 声明了但没有 squat (网页版)
        log, said, meta, _ = _desk_turn(tmp_path, monkeypatch, "你能蹲下吗", out, skills=skills)
        intent = next(m for m in log if m.get("type") == "intent")
        assert intent["op"] == "na" and intent["body"] == ["squat"] and said == "蹲下我现在还做不了，换个别的吧？", skills
    log, said, meta, _ = _desk_turn(tmp_path, monkeypatch, "你能蹲下吗", out, skills=DESK_SKILLS + ["squat"], place="desktop")
    intent = next(m for m in log if m.get("type") == "intent")
    assert intent["op"] == "act" and intent["acts"] == [{"do": "squat", "side": None, "to": None, "amp": 0.5, "spd": 1.0, "at": 0.0}]
    assert said == "好，蹲下啦。"
    # 模型当成聊天时按原话补上 (说法 "蹲")
    log, said, meta, _ = _desk_turn(tmp_path, monkeypatch, "蹲一下",
                                    '{"op":"chat","acts":[],"emo":"happy","reply":"好呀。"}', skills=DESK_SKILLS + ["squat"])
    assert meta["filled"] == {"model": [], "filled": ["squat"]}
    assert P.request_kind("蹲下来") == "impossible" and P.request_kind("蹲下来", I.DEFAULT_CAN | {"squat"}) == "doable"


WEB_SKILLS = DESK_SKILLS + ["leg", "go", "look", "turn", "sit", "stand", "pick", "put"]  # 网页版挂了行动层 (web/app.js), 加 hand 之前


def test_hand_only_when_declared(tmp_path, monkeypatch):
    """伸手邀请牵手 (hand) 是新加的技能: 网页版挂了行动层才声明; 桌宠和老前端没声明, 照实说做不了."""
    out = '{"op":"act","acts":[{"do":"hand"}],"emo":"happy","reply":"好呀，手给你。"}'
    for skills, place in ((None, None), (WEB_SKILLS, None), (DESK_SKILLS, "desktop")):
        log, said, meta, _ = _desk_turn(tmp_path, monkeypatch, "我们牵手吧", out, skills=skills, place=place)
        intent = next(m for m in log if m.get("type") == "intent")
        want = "我在桌面上，伸手牵你还做不到呢。" if place else "伸手牵你我现在还做不了，换个别的吧？"
        assert intent["op"] == "na" and intent["body"] == ["hand"] and said == want, skills
    log, said, meta, _ = _desk_turn(tmp_path, monkeypatch, "我们牵手吧", out, skills=WEB_SKILLS + ["hand"])
    intent = next(m for m in log if m.get("type") == "intent")
    assert intent["op"] == "act" and intent["acts"] == [{"do": "hand", "side": "R", "to": None, "amp": 1.0, "spd": 1.0, "at": 0.0}]
    assert said == "好呀，手给你。"
    # 模型当成聊天时按原话补上 (说法 "牵")
    log, said, meta, _ = _desk_turn(tmp_path, monkeypatch, "牵着我的手",
                                    '{"op":"chat","acts":[],"emo":"happy","reply":"好呀。"}', skills=WEB_SKILLS + ["hand"])
    assert meta["filled"] == {"model": [], "filled": ["hand"]}
    assert "hand" in I.SYSTEM_PROMPT and "hand" not in I.DESK_PROMPT
    assert P.request_kind("拉着我的手") == "impossible" and P.request_kind("拉着我的手", I.DEFAULT_CAN | {"hand"}) == "doable"


def test_chat_with_acts_the_body_cannot_do_is_refused(tmp_path, monkeypatch):
    """原来只有 op=act 时才改判: 模型当成聊天、却配了身体做不了的"走过去", 动作去掉后嘴上照样答应."""
    log, said, meta, _ = _desk_turn(tmp_path, monkeypatch, "能去沙发上吗",
                                    '{"op":"chat","acts":[{"do":"go","to":"sofa"}],"emo":"happy","reply":"当然可以，我来帮你坐。"}')
    intent = next(m for m in log if m.get("type") == "intent")
    assert intent["op"] == "na" and intent["body"] == ["go"] and said == "走过去我现在还做不了，换个别的吧？"
    assert meta["body_cannot"] == ["go"] and meta["reply"] == said  # trace 里记实际说出口的


def test_desktop_pretend_agree_is_amended(tmp_path, monkeypatch):
    """桌面上 "能去沙发上吗" 是做不到的要求 (没有沙发, 也走不了): 模型当成聊天答应了, 回复侧检查改判并发 amend."""
    log, said, meta, _ = _desk_turn(tmp_path, monkeypatch, "能去沙发上吗",
                                    '{"op":"chat","acts":[],"emo":"happy","reply":"当然可以，我来帮你坐。"}', place="desktop")
    assert next(m for m in log if m.get("type") == "intent")["op"] == "chat"
    amend = next(m for m in log if m.get("type") == "amend")
    assert amend == {"type": "amend", "turn": 1, "op": "na", "why": "agree", "body": ["go"]}
    assert said == "我在桌面上，走过去还做不到呢。" and "坐" not in said
    assert meta["promise"] == [{"rule": "agree", "segment": "当然可以，", "action": "refuse"}]
    # 没声明 capabilities、在房间里: 走得过去, 照旧补动作
    log, said, meta, _ = _desk_turn(tmp_path, monkeypatch, "能去沙发上吗",
                                    '{"op":"chat","acts":[],"emo":"happy","reply":"当然可以，我来帮你坐。"}', skills=None)
    assert [a["do"] for a in next(m for m in log if m.get("type") == "intent")["acts"]] == ["go"]
    assert said == "当然可以，我来帮你坐。" and "amend" not in [m.get("type") for m in log]


def test_reply_check_uses_body():
    can = frozenset(DESK_SKILLS)
    v = P.check("我好累", "chat", [], "那我陪你去沙发上坐坐吧。", None, can=can)
    assert v and v.rule == "promise" and v.keep == ""  # 桌宠走不了, "陪你去沙发上坐坐" 兑现不了
    assert P.check("我好累", "chat", [], "那我陪你去沙发上坐坐吧。", None) is None  # 完整的身体做得到 (原来的行为)
    assert P.request_kind("能去沙发上吗") == "doable" and P.request_kind("能去沙发上吗", can) == "impossible"
    assert P.request_kind("能去沙发上吗", room=False) == "impossible"  # 桌面上没有沙发
    assert P.request_kind("过来一下", room=False) == "doable" and P.request_kind("过来一下", can, False) == "impossible"
    ok, cant = I.split_can([{"do": "point", "to": "door"}, {"do": "go", "to": "door"}, {"do": "look", "to": "user"}],
                           I.DEFAULT_CAN, room=False)
    assert ok == [{"do": "point", "to": None}, {"do": "look", "to": "user"}] and cant == ["go"]
    assert I.asked_cannot("去厨房帮我拿个碗", can) == ["go", "pick"]
    assert I.body_refusal(["go", "pick"], "desktop") == "我在桌面上，走过去、拿东西还做不到呢。"


def test_failed_action_is_told_next_turn(tmp_path, monkeypatch):
    seen = []

    class Spy(FakeLLM):
        async def stream(self, messages, **kw):
            if kw.get("schema") is not None:
                seen.append(messages[-1]["content"])
            async for d in super().stream(messages, **kw):
                yield d

    monkeypatch.setattr("wm.engines.create_llm", lambda cfg: Spy(_go_door()))

    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "走到门口去"})
        await collect(ws, until_trace_turn=1)
        await ws.send_json({"type": "motion_eval", "turn": 1, "do": "go", "to": "door", "ok": False, "reason": "走不到门口"})
        await ws.send_json({"type": "text", "text": "怎么了"})
        log = await collect(ws, until_trace_turn=2)
        await ws.send_json({"type": "text", "text": "好吧"})
        await collect(ws, until_trace_turn=3)
        return log

    log = run_ws(make_cfg(tmp_path), scenario)
    assert "刚才没能走到门" not in seen[0]
    assert "刚才没能走到门" in seen[1]  # 失败后的下一轮告诉模型
    assert "刚才没能走到门" not in seen[2]  # 只说一次
    trace = next(m for m in log if m.get("type") == "trace")["trace"]
    assert trace["turn"] == 2


def test_social_prediction_softens_amp(tmp_path, monkeypatch):
    monkeypatch.setattr("wm.engines.create_llm", lambda cfg: FakeLLM(_go_door()))
    key = "act|go|-"
    real_add = SocialModel.add

    def seeded_init(self):
        from collections import Counter, defaultdict
        self.counts, self.total = defaultdict(Counter), Counter()
        for _ in range(25):  # 这个组合以往 25 次里大多让用户退缩
            real_add(self, key, "withdraw")

    monkeypatch.setattr(SocialModel, "__init__", seeded_init)

    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "走到门口去"})
        return await collect(ws, until_trace_turn=1)

    log = run_ws(make_cfg(tmp_path), scenario)
    intent = next(m for m in log if m.get("type") == "intent")
    meta = next(m for m in log if m.get("type") == "trace")["trace"]["meta"]["social_pre"]
    assert meta["key"] == key and meta["n"] == 25 and meta["softened"] == 0.7, meta
    assert abs(intent["acts"][0]["amp"] - 0.7) < 1e-6


def test_station_is_not_stand():
    """"B 站""网站""车站""站点"里的站不是要她站起来 (刷 B 站时被回了"站起来还做不到")."""
    for text in ["帮我打开 B 站首页, 挑一个视频", "刷b站", "去网站看看", "车站见", "站点地图"]:
        assert "stand" not in I.asked_cannot(text, can=set()), text
    for text in ["站起来", "你站一下", "起身吧"]:
        assert "stand" in I.asked_cannot(text, can=set()), text

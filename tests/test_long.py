"""长回复 (讲故事、详细说说): 意图照常出 JSON 和一句短回应, 再追加一次不带 JSON 约束的流式生成."""
import asyncio

from test_pipeline import collect, make_cfg, run as run_ws
from wm import intent as I
from wm.engines import Engines
from wm.llm import MockLLM, mock_long
from wm.pipeline import Session
from wm.trace import LatencyStats, TraceLog


def test_long_request_rules():
    for t in ("你能给我讲故事吗", "给我讲个故事", "给我编个睡前故事", "我想听故事", "给我讲讲星座", "详细说说", "多说点",
              "说详细点", "接着讲", "再讲一个"):
        assert I.long_request(t), t
    for t in ("你讲的故事真好听", "你喜欢听故事吗", "我给你讲个故事", "别讲了", "给我讲个简短的笑话", "把手展开", "举起右手",
              "后来呢", "你好", "陪我聊聊你的一天"):
        assert not I.long_request(t), t
    assert I.long_request("后来呢", after_long=True) and I.long_request("继续", after_long=True)
    assert not I.long_request("举起右手", after_long=True)
    assert I.long_opener("给我讲个故事") == "好呀，给你讲一个。" and I.long_opener("详细说说") == "好，那我多说一点。"
    assert I.long_opener("后来呢", after_long=True) == "好，接着讲。" == I.long_opener("接着讲")


def test_clean_long():
    assert I.clean_long("（笑）这球**真**可爱😀。") == "这球真可爱。"
    assert I.clean_long("当然可以！", first=True) == "" and I.clean_long("好的！好呀！", first=True) == ""
    assert I.clean_long("当然可以！", first=False) == "当然可以！"
    assert I.clean_long("好，从前有一只小狐狸。", first=True) == "好，从前有一只小狐狸。"


class Spy(MockLLM):
    """MockLLM, 记下每次 stream 调用的消息和参数."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.calls = []

    async def stream(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None, meta=None, extra=None):
        self.calls.append({"messages": messages, "schema": schema is not None, "max_tokens": max_tokens,
                           "temperature": temperature, "id_slot": id_slot, "extra": extra})
        async for d in super().stream(messages, schema, max_tokens, temperature, id_slot, meta, extra):
            yield d


def _spy(monkeypatch):
    holder = {}

    def make(cfg):
        holder["llm"] = Spy(cfg)
        return holder["llm"]

    monkeypatch.setattr("wm.engines.create_llm", make)
    return holder


def _said(log, turn):
    return "".join(m["text"] for m in log if m.get("type") == "reply_delta" and m["turn"] == turn)


def _trace(log, turn):
    return next(m["trace"] for m in log if m.get("type") == "trace" and m["trace"]["turn"] == turn)


def test_story_in_mock_then_history(tmp_path, monkeypatch):
    spy = _spy(monkeypatch)

    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "给我讲个故事"})
        log = await collect(ws, until_trace_turn=1)
        await ws.send_json({"type": "text", "text": "举起右手"})
        return log + await collect(ws, until_trace_turn=2)

    log = run_ws(make_cfg(tmp_path), scenario)
    said = _said(log, 1)
    assert said.startswith("好呀，给你讲一个。") and len(said) > 300 and mock_long("故事")[-12:] in said
    assert next(m for m in log if m.get("type") == "intent" and m["turn"] == 1)["op"] == "chat"
    assert len([m for m in log if m.get("type") == "clause" and m["turn"] == 1]) >= 10  # 分句送去合成
    assert len([m for m in log if m.get("type") == "audio_seg" and m["turn"] == 1]) >= 10
    meta = _trace(log, 1)["meta"]
    assert meta["long"]["chars"] > 300 and meta["long"]["finish"] == "stop" and meta["reply"] == said
    calls = spy["llm"].calls
    long_calls = [c for c in calls if c["messages"][0]["content"] == I.LONG_PROMPT]
    assert len(long_calls) == 1
    c = long_calls[0]
    assert not c["schema"] and c["max_tokens"] == 400 and c["id_slot"] == 1 and c["temperature"] == 0.7
    assert c["extra"] == {"repeat_penalty": 1.1} and "好呀，给你讲一个。" in c["messages"][-1]["content"]
    # 第二轮: 普通轮照常 (不追加长回复), 长回复整段在历史里
    intents = [c for c in calls if c["schema"]]
    assert len(intents) == 2 and said in "".join(m["content"] for m in intents[1]["messages"])
    assert "long" not in _trace(log, 2)["meta"] and _said(log, 2) == "好的，举手。"


def test_normal_turn_has_no_long_pass(tmp_path, monkeypatch):
    spy = _spy(monkeypatch)

    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "我今天考试没考好"})
        return await collect(ws, until_trace_turn=1)

    log = run_ws(make_cfg(tmp_path), scenario)
    assert "long" not in _trace(log, 1)["meta"]
    assert all(c["messages"][0]["content"] != I.LONG_PROMPT for c in spy["llm"].calls)


def test_barge_in_stops_story_and_go_on(tmp_path, monkeypatch):
    spy = _spy(monkeypatch)

    async def scenario(ws, hello):
        sent = False

        async def on_msg(m):
            nonlocal sent
            # 正文讲了几句后插话 (跟用户开口打断走同一条路: 取消整轮)
            if m.get("type") == "reply_delta" and m["turn"] == 1 and "小狐狸" in m["text"] and not sent:
                sent = True
                await ws.send_json({"type": "interrupt"})

        await ws.send_json({"type": "text", "text": "给我讲个故事"})
        log = await collect(ws, until_trace_turn=1, on_msg=on_msg)
        await ws.send_json({"type": "text", "text": "后来呢"})
        return log + await collect(ws, until_trace_turn=2)

    log = run_ws(make_cfg(tmp_path, "llm.mock.tokens_per_s=60"), scenario)
    i_int = next(i for i, m in enumerate(log) if m.get("type") == "interrupted" and m["turn"] == 1)
    assert not [m for m in log[i_int:] if m.get("type") in ("reply_delta", "audio") and m["turn"] == 1]
    first = _said(log, 1)
    assert 0 < len(first) < len(mock_long("故事")) and _trace(log, 1)["interrupted"]
    # "后来呢": 上一轮是长回复, 接着讲; 历史里是被打断前说出口的部分
    assert len(_said(log, 2)) > 200 and "long" in _trace(log, 2)["meta"]
    second = [c for c in spy["llm"].calls if c["schema"]][1]
    assert first + "……" in "".join(m["content"] for m in second["messages"])


def test_truncated_story_ends_cleanly(tmp_path, monkeypatch):
    _spy(monkeypatch)

    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "给我讲个故事"})
        return await collect(ws, until_trace_turn=1)

    log = run_ws(make_cfg(tmp_path, "llm.long.max_tokens=60"), scenario)
    said, meta = _said(log, 1), _trace(log, 1)["meta"]["long"]
    assert meta["finish"] == "length" and said.endswith(I.LONG_END)
    body = said[len("好呀，给你讲一个。"):-len(I.LONG_END)]
    assert body and body[-1] in "。！？，" and body in mock_long("故事")  # 没说完的半句丢掉了


class LoopLLM:
    """意图照常; 长回复兜圈子: 同一句话一直重复."""
    name, label, temperature = "fake", "fake", 0.3

    async def health(self):
        return True

    async def close(self):
        pass

    async def stream(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None, meta=None, extra=None):
        if schema is not None:
            for ch in '{"op":"chat","acts":[],"emo":"happy","reply":"好呀，我多说点。"}':
                yield ch
        elif messages[0]["content"] == I.LONG_PROMPT:
            if meta is not None:
                meta["finish_reason"] = "stop"
            yield "第一句是开头的话。"
            for _ in range(6):
                yield "小狐狸一直往前走呀走。"


def test_repeating_story_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr("wm.engines.create_llm", lambda cfg: LoopLLM())

    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "详细说说"})
        return await collect(ws, until_trace_turn=1)

    log = run_ws(make_cfg(tmp_path), scenario)
    # 模型的短回应 ("好呀，我多说点。") 换成固定的开场
    assert _said(log, 1) == "好，那我多说一点。第一句是开头的话。小狐狸一直往前走呀走。" + I.LONG_END
    meta = _trace(log, 1)["meta"]["long"]
    assert meta["finish"] == "repeat" and meta["repeats"] == 2


def test_history_char_budget(tmp_path):
    async def main():
        cfg = make_cfg(tmp_path, "llm.prewarm=false")
        engines = Engines(cfg)

        async def send(m):
            pass

        s = Session(engines, cfg, send, LatencyStats(), TraceLog(tmp_path))
        for i in range(6):  # 普通轮: 6 轮都留着
            s._remember(f"第{i}句", '{"op":"chat","acts":[],"emo":"happy","reply":"嗯嗯，我在听，你接着说。"}')
        assert len(s.history) == 6
        s._remember("讲个故事", '{"reply":"' + "长" * 700 + '"}')
        s._remember("再讲一个", '{"reply":"' + "长" * 700 + '"}')
        size = sum(len(t) + len(r) for t, r in s.history)
        assert size <= cfg["llm"]["history_chars"] and s.history[-1][0] == "再讲一个"
        s._remember("后来呢", '{"reply":"' + "长" * 2000 + '"}')  # 一轮就超了预算: 至少留这一轮
        assert [t for t, _ in s.history] == ["后来呢"]
        await s.close()

    asyncio.run(main())


class OddOpenerLLM(LoopLLM):
    """意图那次的短回应不对题 (真模型常见: 问用户想听什么, 之后却照样讲); 长回复正常讲完."""

    def __init__(self):
        self.long_calls = 0

    async def stream(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None, meta=None, extra=None):
        if schema is not None:
            text = messages[-1]["content"]
            reply = "当然可以，你想听什么故事？" if I.long_request(text) or text.startswith("后来") else "好的，举手。"
            acts = "[]" if reply.startswith("当然") else '[{"do":"raise","side":"R"}]'
            for ch in '{"op":"' + ("chat" if acts == "[]" else "act") + '","acts":' + acts + ',"emo":"happy","reply":"' + reply + '"}':
                yield ch
        elif messages[0]["content"] == I.LONG_PROMPT:
            self.long_calls += 1
            if meta is not None:
                meta["finish_reason"] = "stop"
            yield "从前有一只小狐狸。它住在山脚下。"


def test_fixed_opener_and_go_on_after_one_interjection(tmp_path, monkeypatch):
    llm = OddOpenerLLM()
    monkeypatch.setattr("wm.engines.create_llm", lambda cfg: llm)

    async def scenario(ws, hello):
        log = []
        for i, text in enumerate(("给我讲个故事", "举起右手", "后来呢", "举起右手", "举起右手", "后来呢"), 1):
            await ws.send_json({"type": "text", "text": text})
            log += await collect(ws, until_trace_turn=i)
        return log

    log = run_ws(make_cfg(tmp_path, "llm.long.min_chars=0"), scenario)
    assert _said(log, 1) == "好呀，给你讲一个。从前有一只小狐狸。它住在山脚下。"  # "你想听什么故事？" 不说
    assert _trace(log, 1)["meta"]["llm"]["output"] == '{"op":"chat","acts":[],"emo":"happy"'  # 读到情绪就停, 不等回复
    assert _said(log, 3) == "好，接着讲。从前有一只小狐狸。它住在山脚下。"  # 隔一轮插话, "后来呢" 还是接着讲
    assert "long" not in _trace(log, 6)["meta"] and llm.long_calls == 2  # 隔了两轮就不算了


class ShortStoryLLM(LoopLLM):
    """长回复第一次只讲两句就收尾 (0.8B 常见), 第二次 ("接着讲") 再讲两句; 开头还把开场白重说一遍."""

    def __init__(self):
        self.long_msgs = []

    async def stream(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None, meta=None, extra=None):
        if schema is not None:
            for ch in '{"op":"chat","acts":[],"emo":"happy","reply":"好啊。"}':
                yield ch
        elif messages[0]["content"] == I.LONG_PROMPT:
            self.long_msgs.append((messages, max_tokens))
            if meta is not None:
                meta.update(finish_reason="stop", usage={"completion_tokens": 30})
            if len(self.long_msgs) == 1:
                yield "好呀，给你讲一个。从前有一只小狐狸。它住在山脚下。"
            else:
                yield "好的。后来它交了一个朋友。它们天天一起看星星。"


def test_short_story_asks_once_more(tmp_path, monkeypatch):
    llm = ShortStoryLLM()
    monkeypatch.setattr("wm.engines.create_llm", lambda cfg: llm)

    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "给我讲个故事"})
        return await collect(ws, until_trace_turn=1)

    log = run_ws(make_cfg(tmp_path), scenario)
    # 开场白不重复; 第二次开头的"好的。"不要; 两次的正文接起来
    assert _said(log, 1) == "好呀，给你讲一个。从前有一只小狐狸。它住在山脚下。后来它交了一个朋友。它们天天一起看星星。"
    assert len(llm.long_msgs) == 2  # 只再要一次
    more, budget = llm.long_msgs[1]
    assert more[-2] == {"role": "assistant", "content": "从前有一只小狐狸。它住在山脚下。"}
    assert more[-1] == {"role": "user", "content": I.long_more("给我讲个故事")} and budget == 400 - 30  # 共用 max_tokens
    meta = _trace(log, 1)["meta"]["long"]
    assert meta["more"] == 16 and meta["tokens"] == 60 and meta["finish"] == "stop"


class RunawayLLM(LoopLLM):
    """意图那次把故事写进了 reply, 一直写到上限 (JSON 没闭合); 长回复正常."""

    def __init__(self):
        self.calls = []

    async def stream(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None, meta=None, extra=None):
        self.calls.append((schema is not None, messages))
        if schema is not None:
            yield '{"op":"chat","acts":[],"emo":"happy","reply":"'
            for _ in range(max_tokens):
                yield "从前有座山，"
        elif messages[0]["content"] == I.LONG_PROMPT:
            if meta is not None:
                meta["finish_reason"] = "stop"
            yield "山里有座庙。" * 1 + "庙里住着一只小狐狸。" + "它每天晚上看星星。" * 1


def test_long_does_not_wait_for_runaway_reply(tmp_path, monkeypatch):
    llm = RunawayLLM()
    monkeypatch.setattr("wm.engines.create_llm", lambda cfg: llm)

    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "给我编个睡前故事"})
        log = await collect(ws, until_trace_turn=1)
        await ws.send_json({"type": "text", "text": "举起右手"})
        return log + await collect(ws, until_trace_turn=2)

    log = run_ws(make_cfg(tmp_path, "llm.long.min_chars=0"), scenario)
    assert _said(log, 1) == "好呀，给你讲一个。山里有座庙。庙里住着一只小狐狸。它每天晚上看星星。"
    trace = _trace(log, 1)
    assert "long" in trace["meta"] and len(trace["meta"]["llm"]["output"]) < 200  # 意图那次读到开场就停了
    history = llm.calls[-1][1]  # 第二轮意图请求里的历史: 解析出的字段加实际说出口的
    assert {"role": "assistant", "content": '{"op":"chat","acts":[],"emo":"happy","reply":"' + _said(log, 1) + '"}'} in history

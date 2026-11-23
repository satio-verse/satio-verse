"""意图置信度与升级契约."""
import asyncio
import json
import math

from test_pipeline import collect, make_cfg, run as run_ws
from wm import intent as I
from wm.confidence import field_confidence

ENUMS = I.field_enums()


def _tokens(parts, fmt="prob", temperature=0.3):
    """parts: [(text, {候选: 原始(温度 1)概率})]; 生成 llama-server 两种格式之一的 token 列表.
    prob 格式模拟采样之后的概率 (带温度), logprob 格式是原始分布."""
    out = []
    for text, alts in parts:
        alts = alts or {text: 1.0}
        if fmt == "prob":
            w = {t: p ** (1 / temperature) for t, p in alts.items()}
            z = sum(w.values())
            top = [{"token": t, "prob": v / z} for t, v in w.items()]
            out.append({"token": text, "prob": w[text] / z, "top_probs": top})
        else:
            top = [{"token": t, "logprob": math.log(p)} for t, p in alts.items()]
            out.append({"token": text, "logprob": math.log(alts[text]), "top_logprobs": top})
    return out


PARTS = [('{"op":"', None), ("act", {"act": 0.6, "chat": 0.3, "action": 0.1}), ('","acts":[{"do":"', None),
         ("sh", {"sh": 0.5, "go": 0.5}), ("ake", {"ake": 0.8, "rug": 0.2}), ('","to":null},{"do":"', None),
         ("go", {"go": 0.9, "look": 0.1}), ('","to":"', None), ("sofa", {"sofa": 0.7, "desk": 0.3}),
         ('"}],"emo":"', None), ("happy", {"happy": 0.5, "calm": 0.5}), ('","reply":"', None)]


def test_confidence_formats_agree_and_multitoken_values():
    a = field_confidence(_tokens(PARTS, "prob"), ENUMS, temperature=0.3)
    b = field_confidence(_tokens(PARTS, "logprob"), ENUMS)
    assert a == b  # 采样后的概率按 p^T 还原, 和原始分布在合法候选里归一化的结果完全一样
    assert math.isclose(a["op"], 0.6 / 0.9, abs_tol=1e-4)  # "action" 不是合法取值, 不参与归一化
    assert math.isclose(a["acts"][0]["do"], 0.5 * 0.8, abs_tol=1e-4)  # sh+ake: 两步连乘 (sh 之后只有 shake/shrug)
    assert "to" not in a["acts"][0] and math.isclose(a["acts"][1]["to"], 0.7, abs_tol=1e-4)
    assert math.isclose(a["overall"], a["op"] * 0.4 * 0.9 * 0.7, abs_tol=1e-3)
    assert math.isclose(a["emo"], 0.5, abs_tol=1e-4)
    assert field_confidence([], ENUMS) is None


class FakeLLM:
    """流式输出一段固定的 JSON, 同时按 llama-server 的格式填 token 概率."""
    name, label, temperature = "fake", "fake-0.8b", 0.3

    def __init__(self, parts):
        self.parts = parts

    async def health(self):
        return True

    async def close(self):
        pass

    async def stream(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None, meta=None):
        if schema is None:  # 预热请求
            return
        toks = _tokens(self.parts)
        for t in toks:
            if meta is not None:
                meta.setdefault("tokens", []).append(t)
            yield t["token"]
        yield '好的。"}'


class FakeEscalate(FakeLLM):
    label = "fake-big"

    def __init__(self, reply, delay=0.0):
        self.reply, self.delay = reply, delay

    async def complete(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None):
        assert set(schema["required"]) == {"op", "acts"}
        await asyncio.sleep(self.delay)
        return json.dumps(self.reply)


def _run_with(tmp_path, monkeypatch, escalate):
    low = [('{"op":"', None), ("act", {"act": 0.55, "chat": 0.45}), ('","acts":[{"do":"', None),
           ("go", {"go": 0.5, "look": 0.5}), ('","to":"', None), ("door", {"door": 0.6, "window": 0.4}),
           ('"}],"emo":"', None), ("calm", None), ('","reply":"', None)]

    def fake_create(cfg):
        return escalate if cfg.get("endpoint") == "http://big" else FakeLLM(low)

    monkeypatch.setattr("wm.engines.create_llm", fake_create)

    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "看看门口"})
        return await collect(ws, until_trace_turn=1)

    return run_ws(make_cfg(tmp_path, "llm.escalate.endpoint=http://big", "pipeline.escalate_timeout_ms=300"), scenario)


def test_low_confidence_escalates(tmp_path, monkeypatch):
    log = _run_with(tmp_path, monkeypatch, FakeEscalate({"op": "act", "acts": [{"do": "look", "to": "door"}]}))
    intent = next(m for m in log if m.get("type") == "intent")
    assert intent["escalated"] and [(a["do"], a["to"]) for a in intent["acts"]] == [("look", "door")]
    assert 0 < intent["conf"] < 0.5 and "uncertain" not in intent
    meta = next(m for m in log if m.get("type") == "trace")["trace"]["meta"]["confidence"]
    assert meta["low"] and meta["escalated"]["model"] == "fake-big"


def test_escalation_timeout_keeps_small_model(tmp_path, monkeypatch):
    log = _run_with(tmp_path, monkeypatch, FakeEscalate({"op": "chat", "acts": []}, delay=2.0))
    intent = next(m for m in log if m.get("type") == "intent")
    assert intent["uncertain"] and not intent.get("escalated")
    # 复核超时沿用小模型的结果; "看看门口" 给了 go 是选错了技能, 已经按原话换成 look (intent.off_cue)
    assert [(a["do"], a["to"]) for a in intent["acts"]] == [("look", "door")]
    meta = next(m for m in log if m.get("type") == "trace")["trace"]["meta"]
    assert meta["confidence"]["escalated"]["error"] == "TimeoutError"
    assert meta["filled"] == {"model": ["go"], "filled": ["look"]}


def test_capability_gate_rule():
    wave = [{"do": "wave"}]
    assert I.unsupported("翻个跟头", "act", wave, 0.3)  # 原话里没有任何技能的说法, 模型拿挥手凑数
    assert I.unsupported("飞起来给我看看", "act", wave, 0.3)  # "给我看看"不是让角色看
    assert not I.unsupported("翻个跟头", "act", wave, 0.9)  # 模型有把握时不改
    assert not I.unsupported("看看门口", "act", [{"do": "go"}], 0.2)  # 提到了做得到的动作: 是选错技能, 不改判
    assert not I.unsupported("别坐着了", "act", [{"do": "stand"}], 0.2)
    assert not I.unsupported("跟我打个招呼", "act", wave, 0.2)
    assert not I.unsupported("翻个跟头", "chat", [], 0.2) and not I.unsupported("翻个跟头", "act", wave, None)


def test_capability_gate_replaces_reply(tmp_path, monkeypatch):
    sub = [('{"op":"', None), ("act", {"act": 0.6, "na": 0.4}), ('","acts":[{"do":"', None),
           ("wave", {"wave": 0.5, "nod": 0.5}), ('"}],"emo":"', None), ("happy", None), ('","reply":"', None)]
    monkeypatch.setattr("wm.engines.create_llm", lambda cfg: FakeLLM(sub))

    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "翻个跟头"})
        return await collect(ws, until_trace_turn=1)

    log = run_ws(make_cfg(tmp_path), scenario)
    intent = next(m for m in log if m.get("type") == "intent")
    assert intent["op"] == "na" and intent["acts"] == [] and intent["gated"]
    said = "".join(m["text"] for m in log if m.get("type") == "reply_delta")
    assert said in I.REFUSALS and "好的" not in said  # 模型自己的回复 ("好的。") 不再采用
    assert [m["text"] for m in log if m.get("type") == "clause"] == [said]
    assert next(m for m in log if m.get("type") == "emotion")["emo"] == "shy"
    meta = next(m for m in log if m.get("type") == "trace")["trace"]["meta"]
    assert meta["confidence"]["gated"] == ["wave"] and '"op":"act"' in meta["llm"]["output"]

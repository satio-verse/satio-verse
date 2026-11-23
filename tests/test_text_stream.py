import json
import random

from wm.clauses import ClauseSplitter
from wm.intent import EXAMPLES, dumps_compact, normalize_acts
from wm.json_stream import JSONObjectStream


def run(chunks, keys=("reply",)):
    parser = JSONObjectStream(keys)
    events = []
    for c in chunks:
        events.extend(parser.feed(c))
    return parser, events


def split_random(text, seed):
    rng = random.Random(seed)
    out, i = [], 0
    while i < len(text):
        n = rng.randint(1, 5)
        out.append(text[i:i + n])
        i += n
    return out


def test_values_and_deltas_any_split():
    obj = {"op": "chat", "acts": [{"do": "wave", "side": "L", "amp": 0.5}], "emo": "happy",
           "reply": "你好\"呀\"\\n 换行\n😀 结束。"}
    text = "  noise " + json.dumps(obj, ensure_ascii=True)  # \uXXXX 和代理对都要能跨块解码
    for seed in range(30):
        parser, events = run(split_random(text, seed))
        assert parser.done and parser.result == obj
        assert "".join(e[2] for e in events if e[0] == "delta") == obj["reply"]
        order = [e[1] for e in events if e[0] == "value"]
        assert order == ["op", "acts", "emo", "reply"]


def test_acts_close_before_reply_starts():
    text = dumps_compact({"op": "act", "acts": [{"do": "raise"}], "emo": "neutral", "reply": "好的，举起来啦。"})
    seen_acts = False
    for event in run(list(text))[1]:
        if event[:2] == ("value", "acts"):
            seen_acts = True
        if event[0] == "delta":
            assert seen_acts


def test_primitive_last_value():
    parser, events = run(['{"a": 1', '2, "b": true', "}"], keys=())
    assert parser.result == {"a": 12, "b": True}
    assert events[-1][0] == "done"


def test_examples_parse_and_normalize():
    for _, out in EXAMPLES:
        parser, _ = run([dumps_compact(out)])
        assert parser.result == out
        assert len(normalize_acts(out["acts"])) == len(out["acts"])


def test_normalize_clamps_and_drops_unknown():
    acts = normalize_acts([{"do": "raise", "amp": 3, "spd": "x"}, {"do": "fly"}, {"do": "nod", "side": "L"}])
    assert acts[0]["amp"] == 1.0 and acts[0]["spd"] == 1.0 and acts[0]["side"] == "R"
    assert len(acts) == 2 and acts[1]["side"] is None


def split_all(text, chunk=1, **kw):
    s = ClauseSplitter(**kw)
    out = []
    for i in range(0, len(text), chunk):
        out += s.feed(text[i:i + chunk])
    return out + s.flush()


def test_clauses_basic():
    assert split_all("你好呀！今天想聊点什么？") == ["你好呀！", "今天想聊点什么？"]
    # 首句不足 4 字遇到逗号不切
    assert split_all("好的，举起来啦。") == ["好的，举起来啦。"]
    assert split_all("抱抱你，一次考试说明不了什么，想聊聊吗？") == ["抱抱你，一次考试说明不了什么，", "想聊聊吗？"]


def test_clauses_ascii_period_and_decimal():
    assert split_all("音量调到3.5了. 好的") == ["音量调到3.5了.", "好的"]


def test_clauses_ellipsis_waits_for_more():
    s = ClauseSplitter()
    assert s.feed("嗯…") == []
    assert s.feed("…让我想想。") == ["嗯……", "让我想想。"]


def test_clauses_max_length_and_closers():
    long = "一" * 50
    parts = split_all(long, max_chars=40)
    assert parts == ["一" * 40, "一" * 10]
    # 整段到达时闭引号跟着本句; 逐字到达时它落到下一句开头, 被去掉, 不拖慢首句.
    assert split_all("他说“好的！”然后走了。", chunk=100) == ["他说“好的！”", "然后走了。"]
    assert split_all("他说“好的！”然后走了。") == ["他说“好的！", "然后走了。"]


def test_clauses_drop_unspeakable():
    assert split_all("……。好") == ["好"]

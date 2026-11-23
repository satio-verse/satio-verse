"""韵律分析: 句末升降调、重音; 流水线下发 prosody 消息."""
import json
import numpy as np

from test_pipeline import collect, make_cfg, run as run_ws
from wm.prosody import analyze

SR = 16000


def tone(f0, f1, dur, amp=0.4):
    t = np.arange(int(SR * dur)) / SR
    ph = 2 * np.pi * np.cumsum(np.linspace(f0, f1, len(t))) / SR
    return (np.sin(ph) + 0.4 * np.sin(2 * ph)) * amp


def pcm(*parts):
    return (np.concatenate(parts) * 32767).astype(np.int16)


def test_rise_fall_and_accent():
    sil = np.zeros(int(SR * 0.3))
    rise = analyze(pcm(sil, tone(200, 200, 0.5), tone(200, 300, 0.5), sil), SR)
    fall = analyze(pcm(sil, tone(260, 260, 0.5), tone(260, 170, 0.5), sil), SR)
    assert [e["k"] for e in rise if e["k"] != "accent"] == ["rise"]
    assert [e["k"] for e in fall if e["k"] != "accent"] == ["fall"]
    assert abs(next(e["t"] for e in rise if e["k"] == "rise") - 1.3) < 0.08  # 句末在有声段结束处
    # 五个音节, 第三个又响又高: 它是最强的重音
    gap = np.zeros(int(SR * 0.06))
    syl = [tone(220, 220, 0.18, 0.25), gap, tone(220, 220, 0.18, 0.25), gap, tone(300, 300, 0.2, 0.6), gap,
           tone(220, 220, 0.18, 0.25), gap, tone(210, 210, 0.18, 0.25)]
    acc = [e for e in analyze(pcm(np.zeros(int(SR * 0.2)), *syl, np.zeros(int(SR * 0.3))), SR) if e["k"] == "accent"]
    best = max(acc, key=lambda e: e["s"])
    assert 0.68 < best["t"] < 0.92 and best["hi"]
    assert analyze(np.zeros(SR, dtype=np.int16), SR) == []


def test_prosody_messages_follow_audio(tmp_path):
    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "今天心情怎么样, 给我讲讲你今天做了什么事情吧"})
        return await collect(ws, until_trace_turn=1)

    log = run_ws(make_cfg(tmp_path), scenario)
    pros = [m for m in log if m.get("type") == "prosody"]
    assert pros, "mock TTS 的合成音也有基频, 应该分析出事件"
    for m in pros:
        dur = sum(x["bytes"] / 2 / x["sr"] for x in log if x.get("type") == "audio" and x["seg"] == m["seg"])
        assert all(0 <= e["t"] <= dur + 0.05 and e["k"] in ("accent", "rise", "fall") for e in m["events"])
        first_audio = next(i for i, x in enumerate(log) if x.get("type") == "audio" and x["seg"] == m["seg"])
        assert log.index(m) > first_audio


def test_social_outcome_labels_and_log(tmp_path):
    from wm.social import SocialLog, SocialTracker, label

    prev = {"reply": "今天天气很好，我们出去走走吧。"}
    assert label(prev, "好呀，去哪里走走", 3, False) == "continue"
    assert label(prev, "嗯", 2, False) == "withdraw"
    assert label(prev, "别说了，好烦", 1, False) == "escalate"
    assert label(prev, "我想学做蛋糕有什么推荐吗", 2, False) == "shift"
    assert label(prev, "好呀", 40, False) == "withdraw" and label(prev, None, 5, False) == "withdraw"
    log = SocialLog(tmp_path)
    tr = SocialTracker(log)
    tr.on_turn_done({"key": "act|wave|happy", "reply": "你好呀", "end_ms": 1000}, False)
    tr.on_user_turn("嗯", 2000)
    tr.on_turn_done({"key": "act|wave|happy", "reply": "你好", "end_ms": 3000}, True)  # 被打断
    rows = [json.loads(x) for x in next((tmp_path / "social").glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
    assert [r["outcome"] for r in rows] == ["withdraw", "escalate"]
    p = SocialLog(tmp_path).model.predict("act|wave|happy")  # 重新加载日志
    assert p["withdraw"] > 0.25 and p["escalate"] > 0.25 and abs(sum(p.values()) - 1) < 0.01

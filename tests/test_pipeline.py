"""用 mock 引擎跑完整的 WebSocket 往返: 文本输入 -> 意图 -> 分句 -> TTS -> (RVC) -> 音频帧 -> 打点落盘."""
import asyncio
import json

import numpy as np
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from wm.app import create_app
from wm.clock import now_ms
from wm.config import load_config
from wm.engines import Engines
from wm.pipeline import AUDIO_HEADER, AUDIO_MAGIC, Session
from wm.trace import LatencyStats, TraceLog


def make_cfg(tmp_path, *extra):
    return load_config(overrides=[
        "llm.engine=mock", "llm.mock.tokens_per_s=300", "llm.mock.ttft_ms=30",
        "tts.engine=mock", "tts.mock.rtf=0.02", "tts.mock.first_chunk_ms=20",
        "asr.engine=none", "face.engine=none", "memory.vault=\"\"", f"trace.log_dir={json.dumps(str(tmp_path))}", *extra])


async def mark(ws, turn, name):
    await ws.send_json({"type": "mark", "turn": turn, "name": name, "t": now_ms()})


async def collect(ws, until_trace_turn, acting=True, on_msg=None):
    """像浏览器一样回报 client_* 打点, 收集消息直到指定轮次的 trace 到达."""
    log, audio_started = [], set()
    while True:
        msg = await asyncio.wait_for(ws.receive(), 10)
        if msg.type == WSMsgType.BINARY:
            magic, turn, seg, sr, seq = AUDIO_HEADER.unpack_from(msg.data)
            assert magic == AUDIO_MAGIC
            log.append({"type": "audio", "turn": turn, "seg": seg, "sr": sr, "bytes": len(msg.data) - AUDIO_HEADER.size})
            if acting and turn not in audio_started:
                audio_started.add(turn)
                await mark(ws, turn, "client_audio_start")
            continue
        data = json.loads(msg.data)
        log.append(data)
        if on_msg:
            await on_msg(data)
        if acting and data["type"] == "backchannel":
            await mark(ws, data["turn"], "client_backchannel")
        if acting and data["type"] == "intent":
            await mark(ws, data["turn"], "client_intent")
        if acting and data["type"] == "audio_end":
            await mark(ws, data["turn"], "client_audio_end")
        if data["type"] == "trace" and data["trace"]["turn"] == until_trace_turn:
            return log


def index_of(log, pred):
    return next(i for i, m in enumerate(log) if pred(m))


def run(cfg, scenario):
    async def main():
        client = TestClient(TestServer(create_app(cfg)))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            hello = json.loads((await ws.receive()).data)
            assert hello["type"] == "hello"
            return await scenario(ws, hello)
        finally:
            await client.close()
    return asyncio.run(main())


def test_intent_first_then_audio(tmp_path):
    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "举起右手"})
        return await collect(ws, 1)

    log = run(make_cfg(tmp_path), scenario)
    i_start = index_of(log, lambda m: m["type"] == "turn_start")
    i_back = index_of(log, lambda m: m["type"] == "backchannel")
    i_intent = index_of(log, lambda m: m["type"] == "intent")
    i_audio = index_of(log, lambda m: m["type"] == "audio")
    assert i_start < i_back < i_intent < i_audio
    intent = log[i_intent]
    assert intent["op"] == "act" and intent["acts"][0]["do"] == "raise"
    trace = log[-1]["trace"]
    metrics = trace["metrics"]
    for key in ("intent_ms", "voice_ms", "tts_ttfb_ms", "llm_ttft_ms", "reaction_ms", "first_clause_ms"):
        assert key in metrics, key
    assert metrics["intent_ms"] < metrics["voice_ms"]
    assert not trace["interrupted"]
    assert "".join(m["text"] for m in log if m["type"] == "reply_delta") == "好的，举手。"
    # 日志落盘
    lines = list(tmp_path.glob("latency-*.jsonl"))[0].read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["turn"] == 1


def test_rvc_stage_is_timed(tmp_path):
    async def scenario(ws, hello):
        assert hello["status"]["rvc"]["enabled"]
        await ws.send_json({"type": "text", "text": "我今天考试没考好"})
        return await collect(ws, 1)

    log = run(make_cfg(tmp_path, "rvc.engine=mock"), scenario)
    metrics = log[-1]["trace"]["metrics"]
    assert "rvc_ms" in metrics and metrics["rvc_ms"] > 0
    segs = [m for m in log if m["type"] == "audio_seg"]
    assert len(segs) >= 2  # 回复被切成多句, 每句单独合成


def test_new_turn_interrupts_current(tmp_path):
    async def scenario(ws, hello):
        sent = False

        async def on_msg(m):
            nonlocal sent
            if m["type"] == "intent" and m["turn"] == 1 and not sent:
                sent = True
                await ws.send_json({"type": "text", "text": "点点头"})

        await ws.send_json({"type": "text", "text": "我今天考试没考好"})
        return await collect(ws, 2, on_msg=on_msg)

    log = run(make_cfg(tmp_path, "llm.mock.tokens_per_s=40"), scenario)
    interrupted = [m for m in log if m["type"] == "interrupted"]
    assert interrupted and interrupted[0]["turn"] == 1 and interrupted[0]["reason"] == "new_turn"
    traces = {m["trace"]["turn"]: m["trace"] for m in log if m["type"] == "trace"}
    assert traces[1]["interrupted"] and not traces[2]["interrupted"]
    # 被打断的那轮之后不应再有它的音频
    i_int = index_of(log, lambda m: m["type"] == "interrupted")
    assert not [m for m in log[i_int:] if m["type"] == "audio" and m["turn"] == 1]
    assert log[-1]["stats"]["turns"] == 2


class FakeASR:
    """按脚本逐次返回识别事件, 用来模拟端点切早."""
    name = "fake"
    sample_rate = 16000

    def __init__(self, script):
        self.script = script

    def create_session(self):
        script = list(self.script)

        class S:
            async def accept(self, pcm):
                return script.pop(0) if script else []

            def close(self):
                pass
        return S()


def run_session(tmp_path, script, heard_before_barge_in):
    async def main():
        cfg = make_cfg(tmp_path, "llm.mock.tokens_per_s=40")
        engines = Engines(cfg)
        await engines.load()
        engines.asr = FakeASR(script)
        sent = []

        async def send(m):
            sent.append(m)

        s = Session(engines, cfg, send, LatencyStats(), TraceLog(tmp_path))
        loud = (np.ones(640) * 8000).astype(np.int16).tobytes()
        await s.on_audio(loud)                 # 第一段被判定结束 -> 第 1 轮
        await asyncio.sleep(0.05)
        if heard_before_barge_in:
            s._client_mark({"turn": 1, "name": "client_audio_start", "t": now_ms()})
        await s.on_audio(loud)                 # 用户接着说 -> 插话打断第 1 轮
        await s.on_audio(loud)                 # 第二段结束 -> 第 2 轮
        turn = s.current
        await s.close()
        await engines.close()
        return turn

    return asyncio.run(main())


def test_premature_endpoint_is_merged(tmp_path):
    script = [[("final", "先挥挥左手")], [("partial", "再点")], [("final", "再点点头")]]
    turn = run_session(tmp_path, script, heard_before_barge_in=False)
    assert turn.text == "先挥挥左手再点点头"
    assert turn.trace.meta["merged_from"] == "先挥挥左手"


def test_real_barge_in_is_not_merged(tmp_path):
    script = [[("final", "讲个故事")], [("partial", "等等")], [("final", "等等换一个")]]
    turn = run_session(tmp_path, script, heard_before_barge_in=True)
    assert turn.text == "等等换一个" and "merged_from" not in turn.trace.meta


class FakeVadASR(FakeASR):
    """模拟 VAD + 整句识别引擎: 中间结果不可拿来投机, 投机只在 pause 事件上发起."""
    name = "fake_vad"
    streaming_partials = False


def run_script(tmp_path, steps, *extra, asr_cls=FakeASR):
    """steps: [(本帧的识别事件, 之后等待秒数)]. 返回 (最终的当前轮, 下发消息, 投机计数, 对话历史)."""
    async def main():
        cfg = make_cfg(tmp_path, "llm.mock.tokens_per_s=600", "pipeline.spec_stable_ms=60", *extra)
        engines = Engines(cfg)
        await engines.load()
        engines.asr = asr_cls([events for events, _ in steps])
        sent = []

        async def send(m):
            sent.append(m)

        s = Session(engines, cfg, send, LatencyStats(), TraceLog(tmp_path))
        loud = (np.ones(640) * 8000).astype(np.int16).tobytes()
        for _, pause in steps:
            await s.on_audio(loud)
            await asyncio.sleep(pause)
        result = (s.current, sent, dict(s.spec_stats), list(s.history))
        await s.close()
        await engines.close()
        return result

    return asyncio.run(main())


def turn_messages(sent, turn_id):
    """某一轮下发给前端的消息 (不含 spec 通知本身)."""
    out = []
    for i, m in enumerate(sent):
        if isinstance(m, (bytes, bytearray)):
            if AUDIO_HEADER.unpack_from(m)[1] == turn_id:
                out.append((i, "audio"))
        elif m.get("turn") == turn_id and m.get("type") != "spec":
            out.append((i, m["type"]))
    return out


def test_speculation_commits_when_final_matches(tmp_path):
    steps = [([("partial", "举起右手")], 0.1), ([], 0.3), ([("final", "举起右手。")], 0.3)]
    turn, sent, stats, history = run_script(tmp_path, steps)
    assert stats == {"started": 1, "committed": 1, "cancelled": 0}
    assert turn.trace.meta["speculative"]["basis"] == "举起右手"
    # 确认之前前端什么都没收到, 确认之后整轮原样放出
    i_commit = index_of(sent, lambda m: isinstance(m, dict) and m.get("type") == "spec" and m["state"] == "commit")
    msgs = turn_messages(sent, turn.id)
    assert msgs and all(i > i_commit for i, _ in msgs)
    assert [k for _, k in msgs][:3] == ["turn_start", "backchannel", "intent"]
    # 可交付打点按下发时刻计, 不早于确认
    marks = turn.trace.marks
    assert marks["intent_ready"] >= marks["spec_commit"] and marks["spec_start"] < marks["asr_final"]
    assert history[-1][0] == "举起右手"


def test_speculation_cancelled_on_mismatch(tmp_path):
    steps = [([("partial", "举起右手")], 0.1), ([], 0.2), ([("final", "举起右手吧")], 0.3)]
    turn, sent, stats, history = run_script(tmp_path, steps)
    assert stats == {"started": 1, "committed": 0, "cancelled": 1}
    assert turn.text == "举起右手吧" and "speculative" not in turn.trace.meta
    spec_id = next(m["turn"] for m in sent if isinstance(m, dict) and m.get("type") == "spec")
    assert turn_messages(sent, spec_id) == []  # 被取消的投机轮一条都没漏给前端
    assert [h[0] for h in history] == ["举起右手吧"]


def test_speculation_restarts_when_partial_changes(tmp_path):
    steps = [([("partial", "举起")], 0.1), ([], 0.1), ([("partial", "举起右手")], 0.1), ([], 0.2),
             ([("final", "举起右手")], 0.3)]
    turn, sent, stats, _ = run_script(tmp_path, steps)
    assert stats == {"started": 2, "committed": 1, "cancelled": 1}
    reasons = [m["reason"] for m in sent if isinstance(m, dict) and m.get("type") == "spec" and m["state"] == "cancel"]
    assert reasons == ["partial_changed"]
    assert turn.text == "举起右手" and "speculative" in turn.trace.meta


def test_speculation_can_be_disabled(tmp_path):
    steps = [([("partial", "举起右手")], 0.1), ([], 0.2), ([("final", "举起右手")], 0.3)]
    turn, sent, stats, _ = run_script(tmp_path, steps, "pipeline.speculate=false")
    assert stats["started"] == 0 and turn.text == "举起右手" and "speculative" not in turn.trace.meta


def spec_events(sent, state):
    return [m for m in sent if isinstance(m, dict) and m.get("type") == "spec" and m["state"] == state]


def test_pause_speculates_immediately(tmp_path):
    # pause 之后不等稳定期 (这里 spec_stable_ms=60, 每步只隔 20 ms) 就发起投机
    steps = [([("speech_start", "")], 0.02), ([("pause", "举起右手")], 0.02), ([], 0.02),
             ([("final", "举起右手")], 0.3)]
    turn, sent, stats, _ = run_script(tmp_path, steps, asr_cls=FakeVadASR)
    assert stats == {"started": 1, "committed": 1, "cancelled": 0}
    assert turn.trace.meta["speculative"]["basis"] == "举起右手"


def test_resume_cancels_and_next_pause_respeculates(tmp_path):
    steps = [([("speech_start", "")], 0.02), ([("pause", "举起")], 0.05), ([("resume", "")], 0.05),
             ([("pause", "举起右手")], 0.05), ([("final", "举起右手")], 0.3)]
    turn, sent, stats, _ = run_script(tmp_path, steps, asr_cls=FakeVadASR)
    assert stats == {"started": 2, "committed": 1, "cancelled": 1}
    assert [m["reason"] for m in spec_events(sent, "cancel")] == ["resumed"]
    assert turn.text == "举起右手"


def test_interim_partials_do_not_speculate(tmp_path):
    steps = [([("partial", "举起")], 0.15), ([], 0.15), ([("final", "举起右手")], 0.3)]
    turn, sent, stats, _ = run_script(tmp_path, steps, asr_cls=FakeVadASR)
    assert stats["started"] == 0 and turn.text == "举起右手"


def test_vad_speech_start_barges_in(tmp_path):
    steps = [([("final", "讲个故事")], 0.05), ([("speech_start", "")], 0.05)]
    turn, sent, stats, _ = run_script(tmp_path, steps, asr_cls=FakeVadASR)
    interrupted = [m for m in sent if isinstance(m, dict) and m.get("type") == "interrupted"]
    assert interrupted and interrupted[0]["reason"] == "barge_in"


class RecordingLLM:
    """包一层 MockLLM, 记录每次 stream 调用 (最后一条用户消息, 是否带 schema, max_tokens, 消息条数)."""

    def __init__(self, inner):
        self.inner, self.calls = inner, []
        self.name, self.label = inner.name, inner.label

    @property
    def last_meta(self):
        return self.inner.last_meta

    async def health(self):
        return True

    async def close(self):
        pass

    def stream(self, messages, schema=None, max_tokens=160, **kw):
        self.calls.append((messages[-1]["content"], schema is not None, max_tokens, len(messages)))
        return self.inner.stream(messages, schema=schema, max_tokens=max_tokens, **kw)


def run_prewarm(tmp_path, scenario, *extra):
    async def main():
        cfg = make_cfg(tmp_path, "llm.mock.tokens_per_s=600", *extra)
        engines = Engines(cfg)
        await engines.load()
        engines.llm = RecordingLLM(engines.llm)

        async def send(m):
            pass

        s = Session(engines, cfg, send, LatencyStats(), TraceLog(tmp_path))
        await scenario(s)
        result = (engines.llm.calls, dict(s.prewarm_stats))
        await s.close()
        await engines.close()
        return result

    return asyncio.run(main())


def test_prewarm_after_turn(tmp_path):
    async def scenario(s):
        await s.start_turn("text", "举起右手", now_ms())
        await asyncio.sleep(0.6)

    calls, stats = run_prewarm(tmp_path, scenario)
    assert (stats["done"], stats["cancelled"], stats["failed"]) == (1, 0, 0)
    (text, schema, _, n_turn), (warm_text, warm_schema, warm_max, n_warm) = calls
    assert (text, schema) == ("举起右手", True)
    # 预热: 用户消息为空、不带 schema、只生成 1 个 token, 并且已经带上刚写入的这轮历史
    assert (warm_text, warm_schema, warm_max) == ("", False, 1) and n_warm == n_turn + 2


def test_new_request_cancels_prewarm(tmp_path):
    async def scenario(s):
        s._schedule_prewarm()          # 模拟上一轮刚结束, 预热还在算 (首 token 要 300 ms)
        await asyncio.sleep(0.05)
        await s.start_turn("text", "举起右手", now_ms())
        await asyncio.sleep(0.1)

    calls, stats = run_prewarm(tmp_path, scenario, "llm.mock.ttft_ms=300")
    assert stats["cancelled"] == 1 and calls[1][0] == "举起右手"


def test_prewarm_can_be_disabled(tmp_path):
    async def scenario(s):
        await s.start_turn("text", "举起右手", now_ms())
        await asyncio.sleep(0.6)

    calls, stats = run_prewarm(tmp_path, scenario, "llm.prewarm=false")
    assert len(calls) == 1 and stats["done"] == 0


def test_keepalive_while_user_speaks(tmp_path):
    """说话期间按 keepalive_ms 发保温请求; 端点到来、真实请求开始后不再发."""
    async def main():
        cfg = make_cfg(tmp_path, "llm.mock.tokens_per_s=600", "llm.mock.ttft_ms=10", "llm.keepalive_ms=50",
                       "llm.prewarm=false")
        engines = Engines(cfg)
        await engines.load()
        engines.llm = RecordingLLM(engines.llm)
        engines.asr = FakeVadASR([[("speech_start", "")], [], [("final", "举起右手")]])

        async def send(m):
            pass

        s = Session(engines, cfg, send, LatencyStats(), TraceLog(tmp_path))
        frame = (np.ones(640) * 8000).astype(np.int16).tobytes()
        await s.on_audio(frame)
        await asyncio.sleep(0.35)          # 用户还在说: 期间应发出若干次保温请求
        warm_during_speech = s.prewarm_stats["keepalive"]
        await s.on_audio(frame)
        await s.on_audio(frame)            # 端点 -> 真实请求, 保温被取消
        await asyncio.sleep(0.3)
        after = [c for c in engines.llm.calls if c[0] == ""]
        result = (warm_during_speech, len(after), engines.llm.calls[-1][0], s.prewarm_stats["keepalive"])
        await s.close()
        await engines.close()
        return result

    warm_during_speech, n_warm_calls, last_call, final_count = asyncio.run(main())
    assert warm_during_speech >= 3
    assert last_call == "举起右手" and final_count == warm_during_speech


def test_clock_ping(tmp_path):
    async def scenario(ws, hello):
        await ws.send_json({"type": "ping", "id": 7, "t_client": 123.0})
        return json.loads((await ws.receive()).data)

    pong = run(make_cfg(tmp_path), scenario)
    assert pong["type"] == "pong" and pong["id"] == 7 and pong["t_server"] > 0


def test_humming_follows_a_tune_after_speaking(tmp_path):
    """让她哼两句: 照常说一句, 说完再接一段哼唱 (自己合成, 不走 TTS)."""
    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "哼两句小星星"})
        return await collect(ws, 1)

    log = run(make_cfg(tmp_path), scenario)
    trace = log[-1]["trace"]
    assert trace["meta"]["hum"]["name"] == "小星星"
    assert trace["meta"]["hum"]["notes"] == 14 and trace["meta"]["hum"]["seconds"] > 6
    seg = next(m for m in log if m["type"] == "audio_seg" and m["text"] == "♪ 小星星")
    first_audio = index_of(log, lambda m: m["type"] == "audio" and m.get("seg") == seg["seg"])
    assert first_audio > index_of(log, lambda m: m["type"] == "audio")  # 哼唱排在说话后面
    assert not trace["interrupted"]


def test_humming_follows_a_written_tune(tmp_path):
    """原话里带简谱就照那个调子哼 (1 1 5 5 6 6 5)."""
    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "跟着这个调子：1 1 5 5 6 6 5 哼给我听"})
        return await collect(ws, 1)

    log = run(make_cfg(tmp_path), scenario)
    hum = log[-1]["trace"]["meta"]["hum"]
    assert hum["name"] == "跟着调子" and hum["notes"] == 7
    assert 3.4 < hum["seconds"] < 3.8


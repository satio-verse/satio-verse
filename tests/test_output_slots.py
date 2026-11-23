"""给本机接口用的两处: 连接声明不要语音 (只下发文字和意图), 按连接分 LLM 槽位."""
import asyncio
import time

from aiohttp import web
from aiohttp.test_utils import TestServer

from test_pipeline import collect, make_cfg, run as run_ws
from wm.clock import now_ms
from wm.engines import Engines, SlotPool
from wm.llm import LlamaCppLLM, mock_decide
from wm.pipeline import Session
from wm.trace import LatencyStats, TraceLog


def test_output_without_audio(tmp_path):
    async def scenario(ws, hello):
        await ws.send_json({"type": "output", "audio": False})
        await ws.send_json({"type": "text", "text": "我今天考试没考好"})
        t0 = time.monotonic()
        quiet = await collect(ws, 1, acting=False)  # 不回报打点: 没有音频时服务端也不等客户端
        waited = time.monotonic() - t0
        await ws.send_json({"type": "output", "audio": True})
        await ws.send_json({"type": "text", "text": "举起右手"})
        return quiet, waited, await collect(ws, 2)

    quiet, waited, loud = run_ws(make_cfg(tmp_path, "rvc.engine=mock"), scenario)
    kinds = {m["type"] for m in quiet}
    assert not kinds & {"audio", "audio_seg", "face", "prosody"}
    assert {"turn_start", "intent", "emotion", "reply_delta", "clause", "audio_end", "turn_server_done"} <= kinds
    said = "".join(m["text"] for m in quiet if m["type"] == "reply_delta")
    assert said == mock_decide("我今天考试没考好")["reply"]  # 回复文字完整
    assert next(m for m in quiet if m["type"] == "audio_end")["segments"] == 0
    assert next(m for m in quiet if m["type"] == "turn_server_done")["segments"] == 0
    trace = quiet[-1]["trace"]
    assert trace["meta"]["audio"] is False and not trace["meta"].get("client_timeout")
    assert "tts_first_request" not in trace["marks"] and "rvc_ms" not in trace["metrics"]  # TTS 和 RVC 都没跑
    assert waited < 5  # 没等 client_done_timeout_s (20 s)
    # 改回要语音: 下一轮照常合成
    assert {"audio", "audio_seg"} <= {m["type"] for m in loud}
    assert "audio" not in loud[-1]["trace"]["meta"]


def test_slot_pool():
    pool = SlotPool([0, 3])
    a, b, c = pool.acquire(), pool.acquire(), pool.acquire()
    assert (a, b, c) == (0, 3, None)  # 分完了: 第三条连接用默认槽位, 和别人共用
    pool.release(c)                    # 共用的不占位, 释放了不影响别人
    assert pool.acquire() is None
    pool.release(a)
    assert pool.acquire() == 0         # 断开的连接把槽位还回来
    pool.release(b)
    pool.limit = 3                     # llama-server 只开了 3 个槽位 (--parallel 3): 没有 3 号
    assert pool.acquire() is None
    assert SlotPool(None).acquire() is None  # 没配置: 都用默认槽位 (原来的行为)


class SlotRecorder:
    """包一层 mock LLM, 记下每次请求用的槽位."""

    def __init__(self, inner):
        self.inner, self.name, self.label, self.calls = inner, inner.name, inner.label, []

    async def health(self):
        return True

    async def close(self):
        pass

    def stream(self, messages, schema=None, max_tokens=160, id_slot=None, **kw):
        self.calls.append((messages[-1]["content"], id_slot))
        return self.inner.stream(messages, schema=schema, max_tokens=max_tokens, id_slot=id_slot, **kw)


def test_each_connection_gets_own_slot(tmp_path):
    async def main():
        cfg = make_cfg(tmp_path, "llm.mock.tokens_per_s=600", "llm.session_slots=[0,3]", "llm.prewarm=true")
        engines = Engines(cfg)
        await engines.load()
        engines.llm = SlotRecorder(engines.llm)

        async def send(m):
            pass

        sessions = [Session(engines, cfg, send, LatencyStats(), TraceLog(tmp_path)) for _ in range(3)]
        for s, text in zip(sessions, ("举起右手", "挥挥手", "点点头")):
            await s.start_turn("text", text, now_ms())
        await asyncio.sleep(0.6)  # 各自说完, 各自预热
        slots = [s.slot for s in sessions]
        for s in sessions:
            await s.close()
        again = Session(engines, cfg, send, LatencyStats(), TraceLog(tmp_path))
        reused = again.slot
        await again.close()
        await engines.close()
        return slots, engines.llm.calls, reused

    slots, calls, reused = asyncio.run(main())
    assert slots == [0, 3, None] and reused == 0
    assert {t: s for t, s in calls if t} == {"举起右手": 0, "挥挥手": 3, "点点头": None}
    assert sorted({s for t, s in calls if not t}, key=str) == [0, 3, None]  # KV 预热也进各自的槽位


def test_slot_limit_from_llama_server(tmp_path):
    """llama-server 只开了 3 个槽位 (/props 的 total_slots) 时, 配置里的 3 号不分出去; 连不上时只当默认槽位存在."""
    async def main():
        app = web.Application()

        async def props(request):
            return web.json_response({"total_slots": 3, "build_info": "test"})

        app.router.add_get("/props", props)
        server = TestServer(app)
        await server.start_server()
        cfg = make_cfg(tmp_path, "llm.session_slots=[0,3]")
        engines = Engines(cfg)
        try:
            engines.llm = LlamaCppLLM({"endpoint": str(server.make_url(""))})
            await engines.probe_slots()
            limit, got = engines.slots.limit, [engines.slots.acquire(), engines.slots.acquire()]
            status = engines.status()["llm"]["slots"]
            await engines.llm.close()
        finally:
            await server.close()
        down = LlamaCppLLM({"endpoint": "http://127.0.0.1:9"})
        n_down = await down.slot_count()
        await down.close()
        return limit, got, status, n_down

    limit, got, status, n_down = asyncio.run(main())
    assert limit == 3 and got == [0, None]
    assert status == {"pool": [0, 3], "used": [0], "limit": 3}
    assert n_down == 1

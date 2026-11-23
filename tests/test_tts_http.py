"""HttpTTS 的 GPT-SoVITS 预设: 请求体 (参考音按项目根解析、streaming_mode 可配) 和流式 raw PCM 的拼接."""
import asyncio

import numpy as np
from aiohttp import web

from wm.config import ROOT
from wm.tts import HttpTTS, create_tts


def gsv_cfg(**http):
    return {"engine": "gpt_sovits", "http": {"base_url": "http://127.0.0.1:1", "sample_rate": 32000, **http}}


def test_request_resolves_ref_and_mode():
    tts = create_tts(gsv_cfg(ref_audio_path="models/tts/x/ref.wav", prompt_text="参考文本。", streaming_mode=3))
    body = tts._request("你好。")["json"]
    assert body["ref_audio_path"] == str(ROOT / "models/tts/x/ref.wav")  # api_v2 的工作目录是整合包根
    assert body["streaming_mode"] == 3 and body["media_type"] == "raw" and body["text_split_method"] == "cut0"
    assert body["prompt_text"] == "参考文本。"


def test_request_defaults():
    body = create_tts(gsv_cfg())._request("你好。")["json"]
    assert body["streaming_mode"] is True and body["ref_audio_path"] == ""


def test_min_chunk_length_defaults_to_8():
    """首包恒为 16 个 semantic token; 压到 8 省约 170 ms (实测 536 vs 709). 可从配置覆盖."""
    assert create_tts(gsv_cfg())._request("你好。")["json"]["min_chunk_length"] == 8
    assert create_tts(gsv_cfg(min_chunk_length=16))._request("你好。")["json"]["min_chunk_length"] == 16


def test_stream_joins_odd_chunks():
    """raw PCM 分块到达时可能切在半个采样上, 要拼回去."""
    pcm = (np.arange(1000) - 500).astype(np.int16).tobytes()

    async def handler(request):
        body = await request.json()
        assert body["text"] == "你好。"
        resp = web.StreamResponse()
        await resp.prepare(request)
        for i in range(0, len(pcm), 333):  # 奇数字节一块
            await resp.write(pcm[i:i + 333])
        await resp.write_eof()
        return resp

    async def main():
        app = web.Application()
        app.router.add_post("/tts", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        tts = HttpTTS(gsv_cfg(base_url=f"http://127.0.0.1:{port}"))
        await tts.load()
        out = [c async for c, sr in tts.stream("你好。")]
        await tts._session.close()
        await runner.cleanup()
        return np.concatenate(out)

    got = asyncio.run(main())
    assert got.tobytes() == pcm

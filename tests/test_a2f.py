"""面部动画 (A2F): 有界最小二乘解算器、真实模型的端到端推理 (没下载就跳过)、face 消息下发."""
import asyncio
import base64
import json

import numpy as np
import pytest
from scipy.optimize import lsq_linear

from test_pipeline import collect, make_cfg, run as run_ws
from wm.a2f import A2F, EMO_TO_EXPLICIT, FaceStream
from wm.config import resolve_path

MODEL_DIR = resolve_path("models/a2f/v2.3-mark")
HAS_MODEL = (MODEL_DIR / "network.onnx").exists() and (MODEL_DIR / "model_data.npz").exists()


def _bare_solver(A):
    a = A2F.__new__(A2F)
    a.A = A
    return a


def test_active_set_matches_bvls():
    rng = np.random.default_rng(0)
    for trial in range(40):
        n = 43
        M = rng.normal(size=(120, n))
        A = M.T @ M + 0.5 * np.eye(n)
        b = rng.normal(size=n) * 30
        upper = np.ones(n)
        if trial % 4 == 0:  # 互斥通道: 把其中一个的上界压到 1e-10
            upper[rng.integers(0, n, 3)] = 1e-10
        a = _bare_solver(A)
        x0 = None if trial % 2 else np.clip(rng.random(n), 0, upper)  # 冷启动和热启动都要对
        x = a._solve(b, x0, upper)
        L = np.linalg.cholesky(A)
        ref = lsq_linear(L.T, np.linalg.solve(L, b), bounds=(0, upper + 1e-15), method="bvls", tol=1e-12).x
        f = lambda z: 0.5 * z @ A @ z - b @ z  # noqa: E731
        assert np.all(x >= 0) and np.all(x <= upper + 1e-12)
        assert f(x) <= f(ref) + 1e-8 * max(1.0, abs(f(ref)))
        assert np.abs(x - ref).max() < 1e-6


def test_emotion_mapping_names_are_valid():
    explicit = ["amazement", "anger", "cheekiness", "disgust", "fear", "grief", "joy", "outofbreath", "pain", "sadness"]
    for emo, m in EMO_TO_EXPLICIT.items():
        assert set(m) <= set(explicit), emo
        assert all(0 <= v <= 1 for v in m.values())


@pytest.mark.skipif(not HAS_MODEL, reason="没有下载 Audio2Face-3D 模型 (tools/setup_models.py a2f)")
def test_a2f_model_end_to_end():
    a = A2F({"a2f": {"provider": "cpu"}})
    asyncio.run(a.load())
    assert len(a.names) == 52 and a.names[0] == "eyeBlinkLeft" and "jawOpen" in a.names
    assert len(a.active) == 43  # 8 个眼球视线和 tongueOut 不参与解算
    emo = a.emotion("happy")
    assert emo.shape == (26,) and emo[16 + a.explicit_names.index("joy")] > 0  # 前 16 维隐式, 后 10 维显式

    sr = 22050
    t = np.arange(int(sr * 1.2)) / sr
    voiced = (np.sin(2 * np.pi * 180 * t) + 0.5 * np.sin(2 * np.pi * 360 * t) + 0.3 * np.sin(2 * np.pi * 900 * t))
    envelope = (np.sin(2 * np.pi * 3 * t) > 0) & (t > 0.2) & (t < 1.0)  # 0.2 s 静音 + 一串"音节" + 静音
    pcm = (voiced * envelope * 9000).astype(np.int16)
    r = a.process(FaceStream(), pcm, sr, "neutral")
    W = r["weights"]
    assert W.shape == (a.num_frames(int(len(pcm) * 16000 / sr)), 52)
    assert W.min() >= 0 and W.max() <= 1
    jaw = W[:, a.names.index("jawOpen")]
    assert jaw[8:28].max() > jaw[:3].max() + 0.05  # 发声段的嘴比开头静音时张得大
    assert np.all(W[:, a.names.index("eyeLookInLeft")] == 0)  # 不参与解算的通道恒为 0

    # 流式: 按不等长的块喂, 先出右侧上下文已齐的帧, 句末补齐; 拼起来和整句一次算完一样
    # (CPU 上同一帧落在批次的不同行, 矩阵乘法有 1e-8 量级的舍入差; DirectML 上逐位相同)
    st, outs, i = FaceStream(), [], 0
    for n in (2000, 9000, 700, 6000, 99999):
        r = a.feed(st, pcm[i:i + n], sr, "neutral")
        i += n
        if r:
            outs.append(r)
    outs.append(a.feed(st, None, sr, "neutral", final=True))
    assert len(outs) >= 3 and outs[-1]["final"] and not outs[0]["final"]
    assert [o["start"] for o in outs] == list(np.cumsum([0] + [o["frames"] for o in outs[:-1]]))
    assert np.allclose(np.concatenate([o["weights"] for o in outs]), W, rtol=0, atol=1e-6)
    assert st.done == 0 and not st.src  # 一句结束后清空, 下一句从第 0 帧开始

    a.streaming = False  # 对照: 整句合成完才算
    st = FaceStream()
    assert a.feed(st, pcm[:9000], sr, "neutral") is None
    r = a.feed(st, pcm[9000:], sr, "neutral", final=True)
    assert r["start"] == 0 and np.array_equal(r["weights"], W)


def test_face_messages_follow_audio(tmp_path, monkeypatch):
    names = [f"ch{i}" for i in range(52)]
    calls = []

    class FakeFace:
        name, enabled, provider, fps = "fake", True, "cpu", 30

        def __init__(self):
            self.names = names

        async def load(self):
            pass

        async def run(self, stream, pcm, sr, emo=None, final=False):
            # 和 A2F 一样的流式约定: 先给已收到的音频里除最后 2 帧以外的帧, 句末补齐
            calls.append((0 if pcm is None else len(pcm), sr, emo, stream, final))
            stream.got = getattr(stream, "got", 0) + (0 if pcm is None else len(pcm))
            done = getattr(stream, "sent", 0)
            total = stream.got * 30 // sr + 1
            upto = total if final else max(done, total - 2)
            stream.sent = 0 if final else upto
            if final:
                stream.got = 0
            if upto <= done:
                return None
            w = np.full((upto - done, 52), 0.5, dtype=np.float32)
            return {"start": done, "weights": w, "frames": upto - done, "final": final,
                    "infer_ms": 1.0, "solve_ms": 1.0, "total_ms": 2.0}

    monkeypatch.setattr("wm.engines.create_face", lambda cfg: FakeFace())

    async def scenario(ws, hello):
        await ws.send_json({"type": "text", "text": "今天心情怎么样, 给我讲讲你今天做了什么事情吧"})
        return await collect(ws, until_trace_turn=1)

    log = run_ws(make_cfg(tmp_path), scenario)
    faces = [m for m in log if m.get("type") == "face"]
    segs = {m["seg"] for m in log if m.get("type") == "audio_seg"}
    assert faces and {m["seg"] for m in faces} == segs
    for m in faces:
        assert m["names"] == names and m["fps"] == 30
        assert len(base64.b64decode(m["w"])) == m["n"] * 52
        first_audio = next(i for i, x in enumerate(log) if x.get("type") == "audio" and x["seg"] == m["seg"])
        assert log.index(m) > first_audio  # 先发声音, 再发这句的面部动画
    for seg in segs:  # 每句: 分段从第 0 帧连续拼接, 最后一段带 final, 总帧数对上这句的音频
        parts = [m for m in faces if m["seg"] == seg]
        assert [m["start"] for m in parts] == list(np.cumsum([0] + [m["n"] for m in parts[:-1]]))
        assert parts[-1]["final"] and not any(m["final"] for m in parts[:-1])
        samples = sum(x["bytes"] // 2 for x in log if x.get("type") == "audio" and x["seg"] == seg)
        sr = next(x["sr"] for x in log if x.get("type") == "audio" and x["seg"] == seg)
        assert sum(m["n"] for m in parts) == samples * 30 // sr + 1
    assert max(sum(m["seg"] == seg for m in faces) for seg in segs) > 1  # mock TTS 每 0.1 s 一块: 至少一句分了几段
    assert len({id(c[3]) for c in calls}) == 1  # 同一轮的各句共用一份解算状态
    emo = next(m["emo"] for m in log if m.get("type") == "emotion")
    assert all(c[2] == emo for c in calls)
    trace = next(m for m in log if m.get("type") == "trace")["trace"]
    segments = trace["meta"]["face"]["segments"]
    assert "face_first_sent" in trace["marks"] and len(segments) == len(segs)
    assert sum(x["updates"] for x in segments) == len(faces)
    assert "face_lag_ms" in trace["metrics"]
    json.dumps(trace)


def test_auto_provider_without_directml_disables_face():
    class FakeOrt:
        @staticmethod
        def get_available_providers():
            return ["CPUExecutionProvider"]

    a = A2F({"a2f": {"provider": "auto"}})
    with pytest.raises(RuntimeError, match="DirectML"):  # CPU 比实时慢, 自动模式下直接关掉, 前端用频谱口型
        a._session(FakeOrt, "network.onnx")


def test_kokoro_preset():
    from wm.config import load_config
    from wm.tts import SherpaTTS, create_tts

    cfg = load_config(overrides=["tts.engine=kokoro"])
    tts = create_tts(cfg["tts"])
    assert isinstance(tts, SherpaTTS) and tts.name == "kokoro"
    assert tts.cfg["model_type"] == "kokoro" and 3 <= tts.cfg["sid"] <= 57  # 中文女声
    if not resolve_path(tts.cfg["model"]).exists():
        pytest.skip("没有下载 Kokoro (tools/setup_models.py tts-kokoro)")

    async def synth():
        await tts.load()
        return [p async for p, _ in tts.stream("你好呀。")]

    parts = asyncio.run(synth())
    assert tts.sample_rate == 24000 and sum(len(p) for p in parts) > 24000 * 0.3

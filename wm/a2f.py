"""语音驱动面部: NVIDIA Audio2Face-3D v2.3 (Mark), 一句话的语音 -> 30 fps 的 ARKit 52 通道权重.

照 Audio2Face-3D SDK 的回归模型路径复刻 (executor_regression / animator / blendshape_solver):
  1. 网络: 以每帧为中心取 8320 个 16 kHz 采样 (前后各 0.26 s), 连同 26 维情绪向量
     (16 维隐式, 取自情绪库 g2a_neutral 第 33 帧; 10 维显式, 由 LLM 的 emo 映射), 输出 301 维,
     前 272 维是皮肤 PCA 系数.
  2. 皮肤: 系数 x 皮肤强度, 再加闭眼/张唇的固定偏移; 上脸和下脸分别做二阶指数平滑, 按中性脸高度的
     sigmoid 遮罩混合 (下脸 1.4 倍, 上脸 1.0 倍).
  3. 解算: 只取正脸遮罩内的顶点, 解带 L1/L2/时间/对称正则的有界最小二乘 (权重在 0..1), 得到 ARKit 权重.
第 2 步和第 3 步的投影都是线性的, 载入时把 "PCA 重建 -> 遮罩 -> B^T" 合成两个 43x272 的矩阵,
每帧只剩一个 43 维的小二次规划, 不用重建 6 万个顶点.

流式: TTS 一句话常分几块吐出. 每来一块就把右侧 0.26 s 上下文已经齐了的帧先算出来 (feed), 这一句结束时
补完剩下的帧. 平滑器和解算器的状态在块之间延续, 结果和整句一次算完一致.

模型: https://huggingface.co/nvidia/Audio2Face-3D-v2.3-Mark (NVIDIA Open Model License).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .config import resolve_path

log = logging.getLogger("wm.a2f")

# LLM 的 emo -> A2F 显式情绪 (amazement anger cheekiness disgust fear grief joy outofbreath pain sadness).
# 数值偏保守: 情绪主要由 Soullink 的表情负责, 这里只让说话时的嘴形带一点情绪色彩.
EMO_TO_EXPLICIT: dict[str, dict[str, float]] = {
    "neutral": {},
    "calm": {},
    "happy": {"joy": 0.6},
    "excited": {"joy": 0.7, "amazement": 0.3},
    "sad": {"sadness": 0.6},
    "shy": {"cheekiness": 0.3, "joy": 0.2},
    "angry": {"anger": 0.6},
    "surprised": {"amazement": 0.6},
}


class NoFace:
    name = "none"
    enabled = False
    provider = None

    async def load(self) -> None:
        pass


def create_face(cfg: dict):
    engine = cfg.get("engine", "a2f")
    if engine in ("none", "off", ""):
        return NoFace()
    if engine == "a2f":
        return A2F(cfg)
    raise ValueError(f"未知面部引擎: {engine}")


# 流式时离已收到音频末尾留的余量 (16 kHz 采样): 重采样滤波器在末尾的输出会随后续音频变化, 窗口不碰这一段.
EDGE = 64


class FaceStream:
    """一轮对话的解算状态: 平滑器和上一帧权重跨分句延续, 换一轮就重新开始.
    另记着当前这一句已收到的音频和已算出的帧数, 一句结束时清空."""

    def __init__(self):
        self.upper: np.ndarray | None = None  # (3, 272): 二阶平滑器的 data[0..2]
        self.lower: np.ndarray | None = None
        self.prev: np.ndarray | None = None  # 上一帧的有效通道权重
        self.end_segment()

    def end_segment(self) -> None:
        self.src: list[np.ndarray] = []  # 这一句收到的原始 PCM 块
        self.src_sr: int | None = None
        self.done = 0  # 这一句已算出的帧数


class A2F:
    name = "a2f"
    enabled = True

    def __init__(self, cfg: dict):
        self.cfg = cfg.get("a2f", {})
        self.fps = int(self.cfg.get("fps", 30))
        self.provider: str | None = None
        self.names: list[str] = []
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="a2f")

    async def load(self) -> None:
        await asyncio.get_running_loop().run_in_executor(self._pool, self._load)

    # ------------------------------------------------------------------ 载入
    def _load(self) -> None:
        import onnxruntime as ort

        t0 = time.perf_counter()
        d = resolve_path(self.cfg.get("model_dir", "models/a2f/v2.3-mark"))
        info = json.loads((d / "network_info.json").read_text(encoding="utf-8"))
        mc = json.loads((d / "model_config.json").read_text(encoding="utf-8"))["config"]
        mc.update(self.cfg.get("overrides", {}))
        bp = json.loads((d / "bs_skin_config.json").read_text(encoding="utf-8"))["blendshape_params"]
        self.mc = mc
        ap = info["audio_params"]
        self.buf_len, self.buf_ofs, self.sr = ap["buffer_len"], ap["buffer_ofs"], ap["samplerate"]
        n_skin = info["params"]["num_shapes_skin"]

        self.session, self.provider = self._session(ort, d / "network.onnx")
        self.max_batch = int(self.cfg.get("max_batch", 32))
        self.streaming = bool(self.cfg.get("stream", True))  # False: 整句合成完才算 (对照用)

        # 情绪: 隐式 16 维固定取一帧, 显式 10 维按 LLM 情绪换.
        db = np.load(d / "implicit_emo_db.npz")
        shots = [s.decode() for s in db["emo_spec_names"]]
        k = shots.index(mc["source_shot"])
        frame = min(int(mc["source_frame"]), int(db["emo_spec_size"][k]) - 1)
        self.implicit = db["emo_db"][int(db["emo_spec_start"][k]) + frame].astype(np.float32)
        self.explicit_names = list(info["params"]["explicit_emotions"])

        # 皮肤: PCA 基 S (3V, 272), 中性脸, 固定偏移 K, 下脸遮罩 f.
        m = np.load(d / "model_data.npz")
        neutral = m["shapes_mean_skin"].astype(np.float32)  # (V, 3)
        y = neutral[:, 1]
        y01 = (y - y.min()) / (y.max() - y.min())
        f = 1.0 / (1.0 + np.exp(-(mc["face_mask_level"] - y01) / mc["face_mask_softness"]))
        offset = (m["eye_close_pose_delta"] * -mc["eyelid_open_offset"]
                  + m["lip_open_pose_delta"] * mc["lip_open_offset"]).astype(np.float32)

        # 解算: 正脸遮罩内的顶点, 有效通道.
        b = np.load(d / "bs_skin.npz")
        pose_names = [s.decode() if isinstance(s, bytes) else str(s) for s in b["poseNames"]]
        assert pose_names[0] == "neutral"
        self.names = pose_names[1:]
        active = np.array(bp["bsSolveActivePoses"], dtype=bool)
        self.active = np.flatnonzero(active)
        verts = b["frontalMask"].astype(np.int64)
        bneutral = b["neutral"].astype(np.float32)
        B = np.stack([b[self.names[i]][verts].reshape(-1) for i in self.active], axis=1).astype(np.float64)

        S = m["shapes_matrix_skin"][:, verts, :].reshape(n_skin, -1).T.astype(np.float64)  # (3P, 272)
        fv = np.repeat(f[verts], 3)
        upper = mc["upper_face_strength"] * (1.0 - fv)
        lower = mc["lower_face_strength"] * fv
        self.Pu = (B.T @ (upper[:, None] * S)).astype(np.float64)  # (A, 272)
        self.Pl = (B.T @ (lower[:, None] * S)).astype(np.float64)
        const = (neutral[verts] - bneutral[verts]).reshape(-1) + (upper + lower) * offset[verts].reshape(-1)
        self.q = B.T @ const

        scale = (np.linalg.norm(np.ptp(bneutral, axis=0)) / bp["templateBBSize"]) ** 2
        n = len(self.active)
        sym = self._pairs(bp["bsSolveSymmetryPoses"], active)
        Smat = np.zeros((len(sym), n))
        for r, (i, j) in enumerate(sym):
            Smat[r, i], Smat[r, j] = 1.0, -1.0
        self.A = (B.T @ B
                  + bp["strengthL1regularization"] ** 2 * 0.25 * scale * np.ones((n, n))
                  + bp["strengthL2regularization"] * 10.0 * scale * np.eye(n)
                  + bp["strengthTemporalSmoothing"] * 100.0 * scale * np.eye(n)
                  + bp["strengthSymmetry"] * 10.0 * scale * Smat.T @ Smat)
        self.temporal = bp["strengthTemporalSmoothing"] * scale  # SDK 里 b 这一项没有乘 100
        self.cancel = self._pairs(bp["bsSolveCancelPoses"], active)
        self.mult = np.asarray(bp["bsWeightMultipliers"], dtype=np.float64)
        self.offs = np.asarray(bp["bsWeightOffsets"], dtype=np.float64)

        dt = 1.0 / self.fps
        self.alpha_upper = self._alpha(mc["upper_face_smoothing"], dt)
        self.alpha_lower = self._alpha(mc["lower_face_smoothing"], dt)
        self.load_ms = (time.perf_counter() - t0) * 1000
        # 预热: DirectML 首次运行要编译, scipy 重采样首次调用要导入 (约 1 s), 都放在载入时.
        self.process(FaceStream(), np.zeros(22050 // 2, dtype=np.int16), 22050)
        log.info("A2F 载入 %.0f ms, 推理设备 %s, %d 个有效通道", self.load_ms, self.provider, n)

    def _session(self, ort, path):
        want = self.cfg.get("provider", "auto")
        avail = ort.get_available_providers()
        if want in ("auto", "dml") and "DmlExecutionProvider" in avail:
            so = ort.SessionOptions()
            so.enable_mem_pattern = False  # DirectML 要求
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            opts = {"device_id": int(self.cfg.get("device_id", 0))} if "device_id" in self.cfg \
                else {"performance_preference": "high_performance", "device_filter": "gpu"}
            try:
                s = ort.InferenceSession(str(path), so, providers=[("DmlExecutionProvider", opts), "CPUExecutionProvider"])
                if s.get_providers()[0] == "DmlExecutionProvider":
                    return s, "dml"
            except Exception as exc:  # 驱动或显卡不支持
                log.warning("DirectML 不可用: %s", exc)
        if want != "cpu":
            # CPU 上每秒语音要算 1.5 s 左右, 面部数据总比声音晚到, 不如让前端用频谱口型. 要试 CPU 就显式配 provider=cpu.
            raise RuntimeError("没有可用的 DirectML 显卡, CPU 推理比实时慢, 面部动画已关闭 (可设 face.a2f.provider=cpu 强制)")
        so = ort.SessionOptions()
        so.intra_op_num_threads = int(self.cfg.get("cpu_threads", 4))
        return ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"]), "cpu"

    @staticmethod
    def _alpha(smoothing: float, dt: float) -> float:
        return 1.0 - 0.5 ** (dt / smoothing) if smoothing > 0 else 1.0

    @staticmethod
    def _pairs(pair_ids: list[int], active: np.ndarray) -> list[tuple[int, int]]:
        ids = [p for p, a in zip(pair_ids, active) if a]
        groups: dict[int, list[int]] = {}
        for i, p in enumerate(ids):
            groups.setdefault(p, []).append(i)
        return [(g[0], g[1]) for p, g in groups.items() if p >= 0 and len(g) == 2]

    # ------------------------------------------------------------------ 推理
    def emotion(self, emo: str | None, strength: float | None = None) -> np.ndarray:
        s = self.cfg.get("emotion_strength", 1.0) if strength is None else strength
        explicit = np.zeros(len(self.explicit_names), dtype=np.float32)
        for name, v in EMO_TO_EXPLICIT.get(emo or "neutral", {}).items():
            explicit[self.explicit_names.index(name)] = v * s
        return np.concatenate([self.implicit, explicit])

    def num_frames(self, n_samples: int) -> int:
        return n_samples * self.fps // self.sr + 1

    def _centers(self, n: int, k0: int = 0) -> np.ndarray:
        return np.round(np.arange(k0, n) * self.sr / self.fps).astype(np.int64)

    def _infer(self, audio: np.ndarray, emotion: np.ndarray, k0: int = 0, k1: int | None = None) -> np.ndarray:
        """16 kHz float 音频 -> 第 k0..k1 帧的 301 维网络输出 (默认全部帧)."""
        k1 = self.num_frames(len(audio)) if k1 is None else k1
        centers = self._centers(k1, k0)
        n = len(centers)
        x = np.zeros(len(audio) + self.buf_len + 1, dtype=np.float32)
        x[self.buf_ofs:self.buf_ofs + len(audio)] = audio * self.mc["input_strength"]
        win = np.lib.stride_tricks.sliding_window_view(x, self.buf_len)[centers]  # 窗口从 center - ofs 开始
        out = np.empty((n, 301), dtype=np.float32)
        bs = self.max_batch
        emo = np.broadcast_to(emotion[None, None, :], (bs, 1, len(emotion))).astype(np.float32)
        for i in range(0, n, bs):
            chunk = win[i:i + bs]
            k = len(chunk)
            batch = np.zeros((bs, 1, self.buf_len), dtype=np.float32)  # 固定批大小, DirectML 不必为新形状重编译
            batch[:k, 0] = chunk
            out[i:i + k] = self.session.run(["result"], {"input": batch, "emotion": emo})[0][:k, 0]
        return out

    def _smooth(self, data: np.ndarray | None, raw: np.ndarray, alpha: float) -> tuple[np.ndarray, np.ndarray]:
        """SDK 的二阶插值器, 逐帧. raw (N, 272) -> smoothed (N, 272)."""
        out = np.empty_like(raw)
        for t in range(len(raw)):
            if data is None:
                data = np.repeat(raw[t][None], 3, axis=0)
            data[0] = raw[t]
            for i in (1, 2):
                data[i] += (data[i - 1] - data[i]) * alpha
            out[t] = data[2]
        return out, data

    def _solve(self, b: np.ndarray, x0: np.ndarray | None, upper: np.ndarray | None = None) -> np.ndarray:
        """min 0.5 x'Ax - b'x, 0 <= x <= upper. 原始活动集法 (每步只加入或放开一个边界), 从上一帧热启动."""
        A = self.A
        n = len(b)
        hi = np.ones(n) if upper is None else upper
        x = np.clip(np.zeros(n) if x0 is None else x0, 0.0, hi)
        at_lo = x <= 0.0
        at_hi = (x >= hi) & ~at_lo
        x[at_lo], x[at_hi] = 0.0, hi[at_hi]
        for _ in range(6 * n):
            bound = at_lo | at_hi
            free = ~bound
            if free.any():
                F = np.flatnonzero(free)
                rhs = b[F] - A[np.ix_(F, np.flatnonzero(bound))] @ x[bound]
                target = np.linalg.solve(A[np.ix_(F, F)], rhs)
                cur = x[F]
                step = target - cur
                # 走到第一个碰到的边界; 碰到了就把它加进边界集合
                with np.errstate(divide="ignore", invalid="ignore"):
                    t_lo = np.where(step < -1e-15, -cur / step, np.inf)
                    t_hi = np.where(step > 1e-15, (hi[F] - cur) / step, np.inf)
                k_lo, k_hi = int(np.argmin(t_lo)), int(np.argmin(t_hi))
                t = min(t_lo[k_lo], t_hi[k_hi], 1.0)
                if t < 1.0:
                    x[F] = cur + t * step
                    if t_lo[k_lo] <= t_hi[k_hi]:
                        x[F[k_lo]] = 0.0
                        at_lo[F[k_lo]] = True
                    else:
                        x[F[k_hi]] = hi[F[k_hi]]
                        at_hi[F[k_hi]] = True
                    continue
                x[F] = target
            # 子问题最优; 检查边界上的乘子 (下界处梯度 >= 0, 上界处 <= 0), 放开违反最严重的一个
            g = A @ x - b
            lam = np.where(at_lo, g, 0.0) - np.where(at_hi, g, 0.0)
            k = int(np.argmin(lam))
            if lam[k] >= -1e-10:
                break
            at_lo[k] = at_hi[k] = False
        return x

    def _to16k(self, pcm: np.ndarray, sr: int) -> np.ndarray:
        audio = np.asarray(pcm, dtype=np.float32)
        if pcm.dtype == np.int16:
            audio = audio / 32768.0
        if sr != self.sr:
            from math import gcd

            from scipy.signal import resample_poly

            g = gcd(sr, self.sr)
            audio = resample_poly(audio, self.sr // g, sr // g).astype(np.float32)
        return audio

    def feed(self, stream: FaceStream, pcm: np.ndarray | None, sr: int, emo: str | None = None,
             final: bool = False) -> dict | None:
        """喂这一句的下一块 PCM (int16 或 float, 任意采样率; 可为 None). 返回新算出的帧
        {"start": 句内起始帧, "weights": (n, 52) float32, "final", 各阶段耗时}; 没有新帧时返回 None.
        final=True 表示这一句结束: 末尾按静音补齐, 算完剩下的帧."""
        t0 = time.perf_counter()
        if pcm is not None and len(pcm):
            stream.src.append(np.asarray(pcm))
            stream.src_sr = sr
        if not stream.src or (not final and not self.streaming):
            if final:
                stream.end_segment()
            return None
        # 每次把整句已收到的音频一起重采样: 只多花一两毫秒, 但和整句一次算完的采样点完全一样
        audio = self._to16k(np.concatenate(stream.src), stream.src_sr)
        n_all = self.num_frames(len(audio))
        k0 = stream.done
        if final:
            k1 = n_all
        else:  # 只算窗口右端 (中心 + 0.26 s) 已经落在收到的音频里的帧
            k1 = k0 + int(np.searchsorted(self._centers(n_all, k0) + self.buf_ofs + EDGE, len(audio), side="right"))
        if k1 <= k0:
            if final:
                stream.end_segment()
            return None
        t1 = time.perf_counter()
        out = self._infer(audio, self.emotion(emo), k0, k1)
        t2 = time.perf_counter()
        W = self._post(stream, out)
        t3 = time.perf_counter()
        stream.done = k1
        if final:
            stream.end_segment()
        return {"start": k0, "weights": W, "frames": len(W), "final": final, "resample_ms": (t1 - t0) * 1000,
                "infer_ms": (t2 - t1) * 1000, "solve_ms": (t3 - t2) * 1000, "total_ms": (t3 - t0) * 1000}

    def process(self, stream: FaceStream, pcm: np.ndarray, sr: int, emo: str | None = None) -> dict:
        """一句话整段一次算完 -> {"weights": (N, 52) float32, 各阶段耗时}. 测试和基准用."""
        stream.end_segment()
        return self.feed(stream, pcm, sr, emo, final=True)

    def _post(self, stream: FaceStream, out: np.ndarray) -> np.ndarray:
        """网络输出 -> 平滑 -> 解算 ARKit 权重. 平滑器和上一帧权重存在 stream 里, 分块调用和一次调用结果一样."""
        coef = out[:, :272].astype(np.float64) * self.mc["skin_strength"]
        su, stream.upper = self._smooth(stream.upper, coef, self.alpha_upper)
        sl, stream.lower = self._smooth(stream.lower, coef, self.alpha_lower)
        rhs = su @ self.Pu.T + sl @ self.Pl.T + self.q  # (N, A) = B^T (pose - neutral)
        n_act = len(self.active)
        W = np.zeros((len(rhs), len(self.names)), dtype=np.float32)
        x = stream.prev
        for t in range(len(rhs)):
            b = rhs[t] + (self.temporal * x if x is not None else 0.0)
            x = self._solve(b, x)
            if self.cancel:
                hi = np.ones(n_act)
                for i, j in self.cancel:
                    hi[j if x[i] >= x[j] else i] = 1e-10
                x = self._solve(b, x, hi)
            W[t, self.active] = x
        stream.prev = x
        return np.clip(W * self.mult + self.offs, 0.0, 1.0).astype(np.float32)

    async def run(self, stream: FaceStream, pcm: np.ndarray | None, sr: int, emo: str | None = None,
                  final: bool = False) -> dict | None:
        return await asyncio.get_running_loop().run_in_executor(self._pool, self.feed, stream, pcm, sr, emo, final)

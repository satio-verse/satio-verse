"""记忆检索的嵌入模型 (语义检索): 补 BM25 的词面匹配, 换个说法也能找到 ("我能吃坚果吗" -> "用户对花生过敏").

两套后端, 配置 memory.embed.engine 选 ("none" 不用):
  bge   BAAI/bge-small-zh-v1.5 (MIT, 512 维, 约 2400 万参数): 进程内用 torch 在 CPU 上算, 取 [CLS] 再归一化.
        问句前加官方给的检索指令, 记忆条目不加.
  qwen  Qwen3-Embedding-0.6B Q8_0 (Apache-2.0, 1024 维): llama.cpp 另起嵌入服务 (python tools/start_llm.py --which embed),
        取最后一个 token. 问句前加指令 (模型说明建议用英文写, 不加时检索效果低 1-5%).
两者接口一样: encode(文本列表, query=是否问句) -> 归一化后的向量 (numpy, 行向量), 余弦相似度就是点积.
都是同步调用 (记忆检索本来就在线程池里跑).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request

import numpy as np

from . import localhttp
from .config import resolve_path

log = logging.getLogger("wm.embed")


def _normalize(v: np.ndarray) -> np.ndarray:
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


class BgeEmbedder:
    name = "bge"
    QUERY = "为这个句子生成表示以用于检索相关文章："

    def __init__(self, cfg: dict):
        self.dir = resolve_path(cfg.get("bge_dir", "models/embed/bge-small-zh-v1.5"))
        self.threads = int(cfg.get("threads", 4))
        self.instruct = bool(cfg.get("bge_instruct", True))
        self.label = "bge-small-zh-v1.5"

    def load(self) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        torch.set_num_threads(self.threads)
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(str(self.dir))
        self.model = AutoModel.from_pretrained(str(self.dir)).eval()
        self.encode(["预热"], query=True)

    def encode(self, texts: list[str], query: bool = False) -> np.ndarray:
        if query and self.instruct:
            texts = [self.QUERY + t for t in texts]
        out = []
        with self.torch.inference_mode():
            for i in range(0, len(texts), 64):
                enc = self.tok(texts[i:i + 64], padding=True, truncation=True, max_length=128, return_tensors="pt")
                out.append(self.model(**enc).last_hidden_state[:, 0].float().numpy())
        return _normalize(np.concatenate(out)) if out else np.zeros((0, 512), np.float32)


class LlamaEmbedder:
    name = "qwen"
    INSTRUCT = ("Instruct: Given a user's message to a companion character, retrieve the character's memories about the user "
                "that are relevant to the message\nQuery:")

    def __init__(self, cfg: dict):
        self.endpoint = cfg.get("endpoint", "http://127.0.0.1:8786").rstrip("/")
        self.timeout = float(cfg.get("timeout_s", 10))
        # 问句限时: 笔记本显卡空闲降频后一次要 140-210 ms, 会把整轮检索拖过 80 ms 的时限; 超时就这一轮用 BM25
        self.query_timeout = float(cfg.get("query_timeout_ms", 60)) / 1000
        self.instruct = bool(cfg.get("qwen_instruct", True))
        self.label = "Qwen3-Embedding-0.6B-Q8_0"

    def _post(self, texts: list[str], timeout: float | None = None) -> np.ndarray:
        req = urllib.request.Request(self.endpoint + "/v1/embeddings", json.dumps({"input": texts}).encode(),
                                     {"Content-Type": "application/json"})
        with localhttp.urlopen(req, timeout=timeout or self.timeout) as r:  # 本机的嵌入服务直连, 不走代理
            data = json.load(r)["data"]
        return np.array([d["embedding"] for d in sorted(data, key=lambda d: d["index"])], np.float32)

    def load(self) -> None:
        t0 = time.perf_counter()
        self._post(["预热"])
        log.info("嵌入服务就绪 (%s, %.0f ms)", self.endpoint, (time.perf_counter() - t0) * 1000)

    def encode(self, texts: list[str], query: bool = False) -> np.ndarray:
        if not texts:
            return np.zeros((0, 1024), np.float32)
        if query and self.instruct:
            texts = [self.INSTRUCT + t for t in texts]
        timeout = self.query_timeout if query else None
        return _normalize(np.concatenate([self._post(texts[i:i + 32], timeout) for i in range(0, len(texts), 32)]))


MIN_SIM = {"bge": 0.48, "qwen": 0.46}  # 相似度门槛, 在开发集上定的 (负例最多误命中 1/15 时召回最高)


def load_embedder(cfg: dict | None):
    """按配置载入嵌入模型, 载不了返回 None (检索退回 BM25). engine: none / bge / qwen /
    auto (嵌入服务开着就用 qwen, 否则有模型文件就用 bge). bge 要导入 torch, 第一次约 15 s, 在后台线程里调."""
    cfg = cfg or {}
    engine = cfg.get("engine", "none")
    if engine in ("none", "", None):
        return None
    if engine not in ("auto", "bge", "qwen"):
        raise ValueError(f"未知嵌入引擎: {engine}")
    if engine in ("auto", "qwen"):
        emb = LlamaEmbedder({**cfg, "timeout_s": cfg.get("timeout_s", 10 if engine == "qwen" else 2)})
        try:
            emb.load()
            emb.timeout = float(cfg.get("timeout_s", 10))
            return emb
        except OSError as exc:
            if engine == "qwen":
                raise
            log.info("嵌入服务没开 (%s), 改用 bge", exc)
    bge = BgeEmbedder(cfg)
    if not (bge.dir / "model.safetensors").exists():
        if engine == "bge":
            raise FileNotFoundError(bge.dir / "model.safetensors")
        return None
    bge.load()
    return bge

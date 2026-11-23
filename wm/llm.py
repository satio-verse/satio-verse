"""LLM 引擎: llama.cpp 的 OpenAI 兼容接口 (流式/非流式), 以及不需要模型的 MockLLM.

两个引擎接口一致:
  stream(messages, schema, ...) -> 异步迭代文本增量; 结束后 last_meta (或传入的 meta) 里有 token 数、服务端计时和
                                   finish_reason ("length" 表示写到 max_tokens 被截断)
  complete(messages, schema, ...) -> 完整文本 (基准对照用)
"""
from __future__ import annotations

import asyncio
import json
import re
import zlib
from typing import AsyncIterator

import aiohttp

from . import intent as I
from . import ledger as L


def _account(label: str, timings) -> None:
    """本机模型这次复用了多少 KV cache (cache_n)、新算了多少 (prompt_n)、生成了多少 (predicted_n), 记进账本 (不花钱).
    只生成 1 个 token 的是预热和打分, 不记."""
    if isinstance(timings, dict) and (timings.get("predicted_n") or 0) > 1:
        L.record("pet", "local", label, timings)


class LlamaCppLLM:
    name = "llamacpp"

    def __init__(self, cfg: dict):
        self.endpoint = cfg["endpoint"].rstrip("/")
        self.label = cfg.get("model_label", "llama.cpp")
        self.temperature = cfg.get("temperature", 0.3)
        self.timeout = cfg.get("timeout_s", 60)
        self.id_slot = cfg.get("id_slot", 0)
        self.top_logprobs = int(cfg.get("logprobs", 0))  # >0: 流式时带回每个 token 的前 K 个候选概率 (算意图置信度)
        self._session: aiohttp.ClientSession | None = None
        self.last_meta: dict = {}

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # 不复用连接: llama-server 流式响应结束后可能关闭长连接, 复用会偶发 ServerDisconnectedError.
            # 本机新建连接不到 1 ms.
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout),
                                                  connector=aiohttp.TCPConnector(force_close=True))
        return self._session

    async def health(self) -> bool:
        try:
            s = await self._http()
            async with s.get(self.endpoint + "/health", timeout=aiohttp.ClientTimeout(total=2)) as r:
                return r.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False

    async def slot_count(self) -> int:
        """llama-server 开了几个槽位 (--parallel, /props 的 total_slots). 连不上时只当默认槽位在: 返回 1.
        按连接分槽位 (engines.SlotPool) 用它: 超出的槽位号 llama-server 不报错, 随便挑一个空闲的槽位, 会冲掉别人的缓存."""
        try:
            s = await self._http()
            async with s.get(self.endpoint + "/props", timeout=aiohttp.ClientTimeout(total=2)) as r:
                return int((await r.json(content_type=None))["total_slots"]) if r.status == 200 else 1
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError):
            return 1

    async def lora_scales(self, scale: float) -> list[dict] | None:
        """llama-server 挂着的 LoRA 适配器都换成这个比例, 作为单次请求的 "lora" 参数 (只影响这一次请求).
        没挂适配器或连不上时返回 None, 请求照常. 长回复用它关掉为意图 JSON 训的 LoRA (它会把正文压成一两句)."""
        try:
            s = await self._http()
            async with s.get(self.endpoint + "/lora-adapters", timeout=aiohttp.ClientTimeout(total=2)) as r:
                adapters = await r.json(content_type=None) if r.status == 200 else []
            return [{"id": a["id"], "scale": float(scale)} for a in adapters] or None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError):
            return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _body(self, messages, schema, max_tokens, temperature, stream, id_slot, extra=None) -> dict:
        body = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": stream,
            "cache_prompt": True,
            "id_slot": self.id_slot if id_slot is None else id_slot,
            "chat_template_kwargs": {"enable_thinking": False},
            "seed": 13,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
            if self.top_logprobs:
                # 采样之后的候选概率 (已经过 JSON 约束, 不用对整个词表排序, 几乎不增加耗时);
                # 关掉 top_p/min_p 截断, 否则备选被砍掉, 选中值的概率总是 1. 温度的影响在 confidence.py 里还原.
                body.update({"n_probs": self.top_logprobs, "post_sampling_probs": True, "top_p": 1.0, "min_p": 0.0})
        if schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "turn", "strict": True, "schema": schema}}
        if extra:
            body.update(extra)  # 其他采样参数 (长回复用 repeat_penalty)
        return body

    async def stream(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None,
                     meta: dict | None = None, extra: dict | None = None) -> AsyncIterator[str]:
        """meta: 调用方自己的字典, 用量/耗时/token 概率写在这里 (预热请求和正式回合并发时 last_meta 会被覆盖).
        extra: 额外的请求参数, 原样并进请求体."""
        self.last_meta = meta if meta is not None else {}
        meta = self.last_meta
        s = await self._http()
        body = self._body(messages, schema, max_tokens, temperature, True, id_slot, extra)
        async with s.post(self.endpoint + "/v1/chat/completions", json=body) as r:
            if r.status != 200:
                raise RuntimeError(f"llama.cpp 返回 {r.status}: {(await r.text())[:300]}")
            buf = b""
            async for chunk in r.content.iter_any():
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        return
                    msg = json.loads(data)
                    if "timings" in msg:
                        meta["timings"] = msg["timings"]
                        _account(self.label, msg["timings"])
                    if msg.get("usage"):
                        meta["usage"] = msg["usage"]
                    for choice in msg.get("choices", []):
                        if choice.get("finish_reason"):
                            meta["finish_reason"] = choice["finish_reason"]
                        lp = (choice.get("logprobs") or {}).get("content")
                        if lp:  # 先存概率再交出文本: 调用方在字段闭合时就能算置信度
                            meta.setdefault("tokens", []).extend(lp)
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            yield text

    async def complete(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None) -> str:
        self.last_meta = {}
        s = await self._http()
        body = self._body(messages, schema, max_tokens, temperature, False, id_slot)
        async with s.post(self.endpoint + "/v1/chat/completions", json=body) as r:
            if r.status != 200:
                raise RuntimeError(f"llama.cpp 返回 {r.status}: {(await r.text())[:300]}")
            msg = await r.json()
        self.last_meta = {"usage": msg.get("usage"), "timings": msg.get("timings")}
        _account(self.label, msg.get("timings"))
        return msg["choices"][0]["message"]["content"]

    async def choice_probs(self, messages, options: list[str], id_slot=None) -> dict[str, float]:
        """下一个回答在几个单字选项上的概率 (归一化): 只生成 1 个 token, 读采样后的候选概率. 给分类用 (如日志摘要的"值不值得记"),
        比让小模型生成文字稳定, 也编不出东西. 温度 1、不截断, 概率才是模型本来的."""
        s = await self._http()
        body = self._body(messages, None, 1, 1.0, False, id_slot)
        body.update({"logprobs": True, "n_probs": 8, "post_sampling_probs": True, "top_p": 1.0, "min_p": 0.0, "top_k": 0})
        async with s.post(self.endpoint + "/v1/chat/completions", json=body) as r:
            if r.status != 200:
                raise RuntimeError(f"llama.cpp 返回 {r.status}: {(await r.text())[:300]}")
            msg = await r.json()
        top = (msg["choices"][0].get("logprobs") or {}).get("content") or [{}]
        probs = {o: 0.0 for o in options}
        for c in top[0].get("top_probs", []):
            tok = c["token"].strip()
            for o in options:  # "是的" 也算 "是"
                if tok == o or (tok.startswith(o) and len(tok) <= len(o) + 1):
                    probs[o] += c["prob"]
        total = sum(probs.values())
        return {o: v / total for o, v in probs.items()} if total else {o: 1 / len(options) for o in options}


# ---------------------------------------------------------------------------
# MockLLM: 关键词规则 + 按"近似 token 数 / 速率"模拟耗时. 只用来跑通流水线和测基准脚本本身.
# ---------------------------------------------------------------------------
_TOKEN = re.compile(r"[㐀-鿿]|[A-Za-z]{1,4}|\d{1,3}|\s+|[^\w\s]{1,2}|.", re.S)


def approx_tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _side(text: str) -> str | None:
    if any(k in text for k in ("两只", "双手", "两手", "一起举")):
        return "B"
    if "左" in text:
        return "L"
    if "右" in text:
        return "R"
    return None


def mock_decide(text: str) -> dict:
    """足够覆盖 README 示例的关键词规则. 不追求理解能力, 真实效果看 llama.cpp."""
    state, text = I.split_state(text)
    t = text.strip()
    if state and any(k in t for k in ("拿的是什么", "拿着什么", "手里")):
        held = re.findall(r"(左手|右手)拿着(\S+?)[ ;,.(]", state)
        reply = "，".join(f"{h}拿着{x}" for h, x in held) if held else "我手里什么也没拿"
        return {"op": "chat", "acts": [], "emo": "neutral", "reply": f"{reply}。"}
    if "放回" in t:
        return {"op": "act", "acts": [{"do": "put", "to": I.HOME}], "emo": "neutral", "reply": "好，放回去。"}
    acts: list[dict] = []

    def add(do: str, **params):
        a = {"do": do}
        if do in I.ARM_SKILLS and (side := _side(t)) and side != "R":
            a["side"] = side
        a.update({k: v for k, v in params.items() if v is not None})
        if acts and ("再" in t or "然后" in t) and not any(x["do"] == "go" for x in acts):  # go 之后的动作等走到再做
            a["at"] = 2.5 * len(acts)
        acts.append(a)

    spd = 0.6 if "慢" in t else (1.5 if "快" in t else None)
    if any(k in t for k in ("后空翻", "跳舞", "翻跟头", "劈叉")):
        return {"op": "na", "acts": [], "emo": "shy", "reply": "这个我还没学会，下次给你表演。"}
    # 物体交互: 坐下/站起来/拿起/放下 (放到某处). 先于"放下=停止"判断.
    item = next((k for k in I.CAPS["item"] if any(n in t for n in I.TARGETS[k].split("/"))), None)
    place = next((k for k in I.CAPS["place"] if any(f"放到{n}" in t or f"放在{n}" in t for n in I.TARGETS[k].split("/"))), None)
    if item and any(k in t for k in ("捡", "拿")):
        acts.append({"do": "pick", "to": item})
    if place or (("放下" in t or "放地上" in t) and (item or "东西" in t)):
        acts.append({"do": "put", **({"to": place} if place else {})})
    if "坐" in t:
        acts.append({"do": "sit", "to": "sofa"})
    elif "站起" in t or t.startswith("起来"):
        acts.append({"do": "stand"})
    if acts:
        names = "、".join(I.SKILLS[a["do"]] for a in acts)
        return {"op": "act", "acts": acts, "emo": "neutral", "reply": f"好的，{names}。"}
    if any(k in t for k in ("放下", "别动", "停下", "停止", "站住")):
        return {"op": "stop", "acts": [], "emo": "calm", "reply": "好，我放下啦。"}
    # 空间动作: 按场景物体的名字和别名找目标, 长的名字优先 (避免"门口"被"门"截走).
    names = sorted(((n, k) for k, v in I.TARGETS.items() for n in v.split("/")), key=lambda x: -len(x[0]))
    target = next((k for n, k in names if n in t and k != "user"), None)
    if "过来" in t or ("我这" in t and any(k in t for k in ("走", "来"))):
        target = "user"
    if target and any(k in t for k in ("走", "去", "过来", "到", "跑")):
        acts.append({"do": "go", "to": target, **({"spd": 1.8} if "跑" in t else {})})
    elif target and any(k in t for k in ("看", "瞧", "望")):
        acts.append({"do": "look", "to": target})
    if "转身" in t or "转过" in t:
        acts.append({"do": "turn", "side": "B"})
    elif "左转" in t or "右转" in t:
        acts.append({"do": "turn", "side": "L" if "左转" in t else "R"})
    if "挥" in t:
        add("wave", spd=spd)
    if any(k in t for k in ("举", "抬")):
        amp = 1 if ("头顶" in t or "过头" in t) else (0.8 if ("头" in t or "脑袋" in t) else (0.5 if ("肩" in t or "小一点" in t) else None))
        add("raise", amp=amp, spd=spd)
    if "指" in t:
        add("point", to=target if target and target != "user" else None)
    if "点头" in t or "点点头" in t:
        add("nod", spd=spd)
    if "摇头" in t or "摇摇头" in t:
        add("shake", spd=spd)
    if "耸肩" in t or "摊手" in t:
        add("shrug")
    if "腿" in t:
        add("leg")
    if "鞠躬" in t:
        add("bow")
    if "蹲" in t:
        add("squat", amp=0.5 if "半蹲" in t else None)
    if re.search(r"牵|拉.{0,3}手|握.{0,3}手|手给我|伸.{0,2}手", t):
        add("hand")
    for do in ("heart", "giggle", "chin", "clap", "peace", "cheeks"):  # 表情动作: 按能力闸门的说法认
        if re.search(I.SKILL_CUES[do], t):
            add(do)
    if acts:
        names = "、".join(I.SKILLS[a["do"]] for a in acts)
        return {"op": "act", "acts": acts, "emo": "happy" if "wave" in [a["do"] for a in acts] else "neutral",
                "reply": f"好的，{names}。"}

    emo = "neutral"
    if any(k in t for k in ("开心", "哈哈", "太棒", "耶", "offer", "成功")):
        emo = "excited"
    elif any(k in t for k in ("难过", "伤心", "没考好", "好累", "失败")):
        emo = "sad"
    elif any(k in t for k in ("生气", "气死")):
        emo = "angry"
    greet = any(k in t for k in ("你好", "嗨", "hi", "hello", "早上好", "晚上好"))
    replies = {
        "excited": "太好了！替你开心，快跟我说说是怎么做到的？",
        "sad": "抱抱你，先别急着怪自己，想聊聊发生了什么吗？",
        "angry": "听起来真的很气人，慢慢说，我在听。",
        "neutral": ["嗯嗯，我在听，你接着说，我想知道更多细节。", "这个问题挺有意思的，我们一起想想看吧。",
                    "我明白你的意思了，换个角度看，也许会有新的想法。"],
    }
    if greet:
        return {"op": "chat", "acts": [{"do": "wave"}], "emo": "happy", "reply": "你好呀！今天想聊点什么？"}
    if I.long_request(t):  # 讲故事、详细说说: 先一句短回应, 正文由追加的长回复生成 (见 mock_long)
        return {"op": "chat", "acts": [], "emo": "happy", "reply": "好呀，给你讲一个。" if "故事" in t else "好，那我多说一点。"}
    reply = replies[emo]
    if isinstance(reply, list):
        reply = reply[zlib.crc32(t.encode()) % len(reply)]
    acts = [{"do": "nod"}] if emo == "sad" else []
    return {"op": "chat", "acts": acts, "emo": emo, "reply": reply}


_STORY = ("从前有一只小狐狸，住在山脚下的树洞里。它最喜欢在傍晚出门，看天边的晚霞。有一天，晚霞特别红，"
          "小狐狸想，要是能走到天边就好了。于是它背上小包，沿着小河一直往前走。走着走着，天黑了，星星一颗一颗亮起来。"
          "小狐狸有点害怕，躲在一块大石头后面。这时候，一只萤火虫飞了过来，问它在找什么。小狐狸说，我想去天边看晚霞。"
          "萤火虫笑了，说天边太远啦，不过我可以带你去看另一样好东西。它们穿过草丛，来到一片小池塘边。池塘里倒映着满天的星星，"
          "风一吹，星星就轻轻晃起来。小狐狸看呆了，原来不用走到天边，身边也有这么好看的地方。后来，它每天傍晚都会来池塘边坐一坐，"
          "有时候看晚霞，有时候等星星。萤火虫也常常来陪它，两个成了最好的朋友。小狐狸明白了，美好的东西不一定在很远的地方，"
          "只要愿意停下来看一看，它们就在身边。好啦，故事讲完了，希望你今晚也能睡个好觉。")
_MORE = ("这个话题其实挺有意思的。我们可以从最简单的地方说起，先想想它平时是什么样子。很多事情看起来很普通，"
         "仔细想想，里面都有不少小细节。比如说，同一件事，早上做和晚上做，感觉就完全不一样。再比如说，"
         "一个人做和几个人一起做，心情也会不一样。所以我觉得，不用着急下结论，可以先慢慢观察，看看自己真正喜欢的是哪一部分。"
         "如果遇到不明白的地方，就停下来问一问，或者换个角度再想一遍。很多时候，答案就藏在这些小地方里。"
         "说了这么多，其实最重要的一点就是，按自己的节奏来，开心最要紧。")


def mock_long(text: str) -> str:
    """长回复 (没有 JSON 约束的第二次生成) 的固定正文: 讲故事给一个小故事, 其余给一段多说点的话. 都是几百字的短句."""
    return _STORY if "故事" in text or "后来" in text else _MORE


class MockLLM:
    name = "mock"

    def __init__(self, cfg: dict):
        m = cfg.get("mock", {})
        self.label = "mock"
        self.temperature = cfg.get("temperature", 0.3)  # 基准脚本算置信度时要读 (mock 没有 token 概率)
        self.ttft = m.get("ttft_ms", 120) / 1000.0
        self.rate = float(m.get("tokens_per_s", 40))
        self.last_meta: dict = {}

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    @staticmethod
    def _user_text(messages) -> str:
        return messages[-1]["content"]  # 可能带状态行, mock_decide 自己拆

    def _render(self, messages, schema) -> str:
        if schema is None and messages and messages[0]["content"] == I.LONG_PROMPT:
            return mock_long(self._user_text(messages).split("\n")[0])
        decision = mock_decide(self._user_text(messages))
        if schema is None:
            return decision["reply"]
        if "operation" in schema.get("properties", {}):  # 基线协议
            return json.dumps(I.to_verbose(decision), ensure_ascii=False)
        return I.dumps_compact(decision)

    async def stream(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None,
                     meta: dict | None = None, extra: dict | None = None) -> AsyncIterator[str]:
        text = self._render(messages, schema)
        all_tokens = approx_tokens(text)
        tokens = all_tokens[:max_tokens]
        self.last_meta = meta if meta is not None else {}
        self.last_meta.update(usage={"completion_tokens": len(tokens)},
                              finish_reason="length" if len(all_tokens) > max_tokens else "stop")
        # 按时间表发 token, 落后了就连发补齐: Windows 上 asyncio.sleep 精度约 15 ms, 逐个睡会远低于设定速率.
        loop = asyncio.get_running_loop()
        t0 = loop.time() + self.ttft
        for i, tok in enumerate(tokens):
            delay = t0 + i / self.rate - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            yield tok

    async def complete(self, messages, schema=None, max_tokens=160, temperature=None, id_slot=None) -> str:
        text = self._render(messages, schema)
        tokens = approx_tokens(text)[:max_tokens]
        self.last_meta = {"usage": {"completion_tokens": len(tokens)}}
        await asyncio.sleep(self.ttft + max(0, len(tokens) - 1) / self.rate)
        return "".join(tokens)


def create_llm(cfg: dict):
    engine = cfg.get("engine", "mock")
    if engine == "llamacpp":
        return LlamaCppLLM(cfg)
    if engine == "mock":
        return MockLLM(cfg)
    raise ValueError(f"未知 LLM 引擎: {engine}")

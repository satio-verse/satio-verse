"""本机模型 (provider selfhost, 2026-11-02 用户: "本地部署的大模型能不能接进来呢？接进来跟用api在桌宠、工作台上的整体功能无异"):
用本机的假 OpenAI 兼容服务 (FakeLocal: 不要 key, 能装成 llama-server / Ollama / LM Studio / vLLM 回 /props、/api/show 这些)
把桌宠、QQ、工作台三处都走一遍. 不连真的模型, 不花钱.
覆盖: 认得出本机 (selfhost、custom 填本机地址又没 key)、请求体 (json_schema 约束、思考开关、不带 key、没填模型名时问服务)、
正文里的 <think>、vLLM / Ollama 的 reasoning 字段、记 0 元且不算云端额度、上下文装不下 (kind=context; 工作台折了再试)、
问服务 (probe) 和给面板的提醒、读超时放长、打断时断开流."""
import asyncio
import json

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from test_channels import GA, OWNER, SELF, FakeOneBot, Inbox, gmsg, pmsg, qq_over, reply_json, until
from test_cloud import KEY, WEATHER_TOOLS, FakeCloud, _chunk, _collect, _said, _sse, call, say
from test_pipeline import collect, make_cfg
from wm import cloud as C
from wm import intent as I
from wm import ledger as L
from wm.app import ENGINES, create_app
from wm.channels import guest as G


async def recv(ws, kind, timeout=10):
    """等某一类 JSON 消息; 中间的语音二进制帧跳过 (test_cloud.recv 只认文字帧, 打断时已经在出声了会撞上)."""
    while True:
        msg = await asyncio.wait_for(ws.receive(), timeout)
        if msg.type == WSMsgType.BINARY:
            continue
        data = json.loads(msg.data)
        if data["type"] == kind:
            return data


@pytest.fixture(autouse=True)
def _local_defaults():
    """本机模型的默认值是模块里的全局量 (服务端起来时按配置改): 每条测试前后都回到出厂值."""
    C.configure({})
    yield
    C.configure({})


class FakeLocal(FakeCloud):
    """本机服务: 不查 key; kind 决定它装成哪家 (llama: /props; ollama: /api/show + /api/ps; lmstudio: /api/v0/models;
    vllm: /v1/models 带 max_model_len). 记下中途被客户端断开的流 (打断)."""

    def __init__(self, *script, kind="llama", ctx=32768, vision=False, model="qwen-local"):
        super().__init__(*script)
        self.kind, self.ctx, self.vision, self.model = kind, ctx, vision, model
        self.cut = 0

    async def chat(self, request):
        body = await request.json()
        self.requests.append({"auth": request.headers.get("Authorization"), "body": body})
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return await step(request, body)

    async def models(self, request):
        row = {"id": self.model, "object": "model"}
        if self.kind == "llama":
            row["meta"] = {"n_ctx_train": 262144}
        if self.kind == "vllm":
            row["max_model_len"] = self.ctx
        return web.json_response({"object": "list", "data": [row]})

    def _only(self, kind, data):
        return web.json_response(data) if self.kind == kind else web.json_response({"error": "not found"}, status=404)

    async def props(self, request):
        return self._only("llama", {"default_generation_settings": {"n_ctx": self.ctx}, "total_slots": 2,
                                    "modalities": {"vision": self.vision, "audio": False}})

    async def lms(self, request):
        return self._only("lmstudio", {"data": [{"id": self.model, "type": "vlm" if self.vision else "llm", "state": "loaded",
                                                 "max_context_length": 131072, "loaded_context_length": self.ctx,
                                                 "capabilities": ["tool_use"]}]})

    async def show(self, request):
        caps = ["completion", "tools"] + (["vision"] if self.vision else [])
        return self._only("ollama", {"capabilities": caps, "model_info": {"qwen3.context_length": 40960}})

    async def ps(self, request):
        return self._only("ollama", {"models": [{"name": self.model, "model": self.model, "context_length": self.ctx}]})

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.chat)
        app.router.add_get("/v1/models", self.models)
        app.router.add_get("/props", self.props)
        app.router.add_get("/api/v0/models", self.lms)
        app.router.add_post("/api/show", self.show)
        app.router.add_get("/api/ps", self.ps)
        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("/v1"))


def say_parts(*parts, reason_key=None, reasons=(), usage=None):
    """流式回几段正文, 按 parts 原样切开 (能把 <think> 标签切在中间); reasons 先发, 放在 reason_key 字段里."""
    async def handle(request, body):
        chunks = [_chunk({reason_key: r}) for r in reasons] if reason_key else []
        chunks += [_chunk({"content": p}) for p in parts]
        chunks += [_chunk(finish="stop"), _chunk(usage=usage or {"prompt_tokens": 300, "completion_tokens": 40,
                                                                   "prompt_tokens_details": {"cached_tokens": 200}})]
        return await _sse(request, chunks)
    return handle


def ctx_full(n_ctx=8192):
    """llama-server 装不下时的回法."""
    async def handle(request, body):
        return web.json_response({"error": {"code": 400, "message": "the request exceeds the available context size, try increasing it",
                                            "type": "exceed_context_size_error", "n_prompt_tokens": n_ctx + 900, "n_ctx": n_ctx}},
                                 status=400)
    return handle


def dribble(fake, text, gap=0.05):
    """慢慢一个字一个字地回 (CPU 上的本机模型); 客户端中途断开时记一笔 (fake.cut)."""
    async def handle(request, body):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        try:
            for ch in text:
                await resp.write(b"data: " + json.dumps(_chunk({"content": ch}), ensure_ascii=False).encode() + b"\n\n")
                await asyncio.sleep(gap)
            await resp.write(b"data: [DONE]\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            fake.cut += 1
            raise
        return resp
    return handle


def local_spec(base, model="qwen-local"):
    return C.parse_model({"provider": "selfhost", "base_url": base, "model": model})


def with_local(script, body, kind="llama", ctx=32768, vision=False, cfg=None):
    async def main():
        fake = FakeLocal(*script, kind=kind, ctx=ctx, vision=vision)
        base = await fake.start()
        llm = C.CloudLLM(local_spec(base), cfg or {})
        try:
            return await body(llm, fake, base)
        finally:
            await llm.close()
            await fake.close()
    return asyncio.run(main())


# ---------------------------------------------------------------------------
# 认得出本机、默认值
# ---------------------------------------------------------------------------
def test_selfhost_parse_and_detect():
    s = C.parse_model({"provider": "selfhost"})
    assert s == {"provider": "selfhost", "base_url": "http://127.0.0.1:8080/v1", "api_key": "", "model": C.AUTO}
    assert C.is_local(s) and C.public_model(s)["local"] is True and C.NAMES["selfhost"] == "本机模型"
    for alias in ("ollama", "LM-Studio", "vllm", "llama-server"):
        assert C.parse_model({"provider": alias, "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:8b"})["provider"] == "selfhost"

    def custom(base, key=""):
        return C.parse_model({"provider": "custom", "base_url": base, "api_key": key, "model": "m"})

    for base in ("http://127.0.0.1:1234/v1", "http://192.168.1.20:8000/v1", "http://gpu-box:8000/v1", "http://[::1]:8080/v1",
                 "http://nas.local:11434/v1", "http://100.88.1.2:8000/v1"):
        assert C.is_local(custom(base)), base
    assert not C.is_local(custom("http://127.0.0.1:3000/v1", KEY))  # 本机跑的中转站 (要 key): 照云端算钱、算额度
    assert not C.is_local(custom("https://api.example.com/v1")) and not C.is_local(custom("http://8.8.8.8/v1"))
    assert not C.is_local(C.parse_model({"provider": "deepseek", "api_key": KEY, "model": "m"}))
    assert C.public_model(custom("https://x.example/v1", KEY))["local"] is False
    with pytest.raises(C.ProfileError, match="selfhost"):
        C.parse_model({"provider": "gpt9", "model": "m"})


def test_configure_defaults_and_local_spec():
    C.configure({"base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:8b", "thinking": "auto", "ctx": "16384",
                 "read_timeout_s": "x", "unknown": 1})
    assert C.parse_model({"provider": "selfhost"}) == {"provider": "selfhost", "base_url": "http://127.0.0.1:11434/v1",
                                                       "api_key": "", "model": "qwen3:8b"}
    assert C.LOCAL_API["ctx"] == 16384 and C.LOCAL_API["read_timeout_s"] == 300.0 and C.LOCAL_API["thinking"] == "auto"
    assert C.local_spec()["model"] == "qwen3:8b" and "unknown" not in C.LOCAL_API
    C.configure({"server": {"model": "models/x.gguf", "port": 8799}})  # 只配了自己起的 llama-server: 地址就是它的端口
    assert C.local_spec() == {"provider": "selfhost", "base_url": "http://127.0.0.1:8799/v1", "api_key": "", "model": C.AUTO}
    C.configure({})
    assert C.local_spec() is None and C.LOCAL_API["base_url"] == "http://127.0.0.1:8080/v1"
    C.configure({"base_url": "127.0.0.1:8080"})  # 漏了 http://: 先不用 (工作台列模型时不出错)
    assert C.local_spec() is None


def test_local_timeouts_and_title():
    llm = C.CloudLLM(local_spec("http://127.0.0.1:9/v1"), {"read_timeout_s": 120})
    cloud = C.CloudLLM({"provider": "custom", "base_url": "https://x.example/v1", "api_key": KEY, "model": "m"}, {})
    assert llm.local and llm.read_timeout == 300 and llm.connect_timeout == 5 and llm.title == "本机模型"
    assert not cloud.local and cloud.read_timeout == 30 and cloud.title == "自定义接口"


# ---------------------------------------------------------------------------
# 正文里的 <think>
# ---------------------------------------------------------------------------
THINK_CASES = {
    "<think>想一想</think>正文": ("想一想", "正文"),
    "  <think>\n想</think>\n\n正文": ("\n想", "正文"),
    "正文<think>不是思考</think>": ("", "正文<think>不是思考</think>"),
    "<thinking>x": ("", "<thinking>x"),
    "<": ("", "<"),
    "<think>没写完就断了": ("没写完就断了", ""),
    '{"reply":"<think>"}': ("", '{"reply":"<think>"}'),
}


@pytest.mark.parametrize("text,want", THINK_CASES.items())
def test_think_split_any_chunking(text, want):
    for i in range(len(text) + 1):
        for j in range(i, len(text) + 1):
            sp = C.ThinkSplit()
            parts = [sp.feed(text[:i]), sp.feed(text[i:j]), sp.feed(text[j:]), sp.flush()]
            assert ("".join(p[0] for p in parts), "".join(p[1] for p in parts)) == want, (i, j)


# ---------------------------------------------------------------------------
# 请求体、思考、JSON
# ---------------------------------------------------------------------------
def test_local_request_schema_thinking_off_and_auto_model():
    reply = '{"op":"act","acts":[{"do":"wave"}],"emo":"happy","reply":"嗨~"}'

    async def body(llm, fake, base):
        llm.model = C.AUTO  # 面板没填模型名: 发之前问服务
        out, meta = await _collect(llm, I.build_messages("挥挥手"), I.build_schema())
        return out, meta, fake.requests, llm.model

    out, meta, reqs, model = with_local([say(reply)], body)
    b = reqs[0]["body"]
    assert reqs[0]["auth"] is None and b["model"] == "qwen-local" == model  # 不带 key; 用服务加载的那个
    assert b["response_format"] == {"type": "json_schema", "json_schema": {"name": "reply", "strict": True,
                                                                           "schema": I.build_schema()}}
    assert b["chat_template_kwargs"] == {"enable_thinking": False}  # 本机默认不思考 (llm.local_api.thinking)
    assert json.loads(out)["reply"] == "嗨~" and meta["cloud"]["model"] == "selfhost:qwen-local"


def test_local_thinking_on_no_constraint_and_inline_think():
    reply = '{"op":"chat","acts":[],"emo":"happy","reply":"好呀"}'

    async def body(llm, fake, base):
        llm.thinking = True
        out, _ = await _collect(llm, I.build_messages("你好"), I.build_schema())
        got = [ev async for ev in llm.chat([{"role": "user", "content": "1+1"}])]
        return out, got, fake.requests

    out, got, reqs = with_local([say_parts("<thi", "nk>先想想", "怎么回</th", "ink>\n\n" + reply[:10], reply[10:]),
                                 say_parts("<think>算一下</think>", "等于 2")], body)
    assert "response_format" not in reqs[0]["body"] and reqs[0]["body"]["chat_template_kwargs"] == {"enable_thinking": True}
    assert json.loads(out)["reply"] == "好呀"  # 思考那段没混进回答
    done = got[-1][1]
    assert "".join(e[1] for e in got if e[0] == "think") == "算一下" == done["reasoning"] and done["content"] == "等于 2"


@pytest.mark.parametrize("key", ["reasoning_content", "reasoning"])
def test_reasoning_field_variants(key):
    """llama-server / DeepSeek 叫 reasoning_content, vLLM 新版和 Ollama 叫 reasoning."""
    async def body(llm, fake, base):
        return [ev async for ev in llm.chat([{"role": "user", "content": "hi"}])]

    got = with_local([say_parts("答案", reason_key=key, reasons=["想", "一下"])], body)
    assert [e for e in got if e[0] == "think"] == [("think", "想"), ("think", "一下")] and got[-1][1]["reasoning"] == "想一下"


def test_lean_retry_keeps_thinking_switch():
    """服务不认 json_schema (回 400): 去掉可选参数再试, 本机的思考开关留着 (不然 Qwen3 这类默认开着想)."""
    async def picky(request, body):
        if "response_format" in body:
            return web.json_response({"error": {"message": "response_format type json_schema is not supported"}}, status=400)
        return await say('{"emo":"happy","reply":"在"}')(request, body)

    async def body(llm, fake, base):
        out, _ = await _collect(llm, [{"role": "user", "content": "在吗"}], G.SCHEMA)
        out2, _ = await _collect(llm, [{"role": "user", "content": "在吗"}], G.SCHEMA)
        return out, out2, fake.requests, llm.minimal

    out, out2, reqs, minimal = with_local([picky], body)
    assert json.loads(out)["reply"] == "在" and json.loads(out2)["reply"] == "在" and minimal
    assert [("response_format" in r["body"], r["body"].get("chat_template_kwargs")) for r in reqs] == \
        [(True, {"enable_thinking": False}), (False, {"enable_thinking": False}), (False, {"enable_thinking": False})]


def test_tools_offered_no_constraint():
    async def body(llm, fake, base):
        async def run(calls):
            return [{"content": "晴, 20 度", "is_error": False} for _ in calls]
        tools, _ = C.parse_tools(WEATHER_TOOLS)
        out, _ = await _collect(llm, I.build_messages("今天天气"), I.build_schema(), tools=tools, on_tools=run)
        return out, fake.requests

    out, reqs = with_local([call(("weather", '{"city":"上海"}')), say('{"op":"chat","acts":[],"emo":"happy","reply":"晴天"}')], body)
    assert all("response_format" not in r["body"] for r in reqs) and reqs[0]["body"]["tools"]  # 约束解码会把工具调用挡掉
    assert json.loads(out)["reply"] == "晴天" and reqs[1]["body"]["messages"][-1]["role"] == "tool"


# ---------------------------------------------------------------------------
# 记账、额度、出错
# ---------------------------------------------------------------------------
def test_local_records_zero_cost_and_skips_budget(tmp_path):
    from test_ledger import make

    led = make(tmp_path)
    led.set_budget({"unit": "tokens", "limits": {"pet": 10, "total": 10}})
    led.record("pet", "deepseek", "deepseek-flash", {"prompt_tokens": 100, "completion_tokens": 20})  # 云端额度已经用完
    L.use(led)
    try:
        async def body(llm, fake, base):
            out, meta = await _collect(llm, [{"role": "user", "content": "在吗"}], G.SCHEMA)
            cloud = C.CloudLLM({"provider": "custom", "base_url": base, "api_key": KEY, "model": "m"}, {})
            try:
                with pytest.raises(C.CloudError) as err:
                    await _collect(cloud, [{"role": "user", "content": "在吗"}], G.SCHEMA)
            finally:
                await cloud.close()
            return out, err.value.kind

        out, kind = with_local([say('{"emo":"happy","reply":"在的"}')], body)
        assert json.loads(out)["reply"] == "在的" and kind == "budget"  # 本机照常说; 云端被额度拦下
        rows = [json.loads(x) for x in next((tmp_path / "billing").glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
        local = [r for r in rows if r["provider"] == "local"]
        assert local and local[-1]["model"] == "qwen-local" and local[-1]["cost"] == 0.0 and local[-1]["miss"] == 100
        today = led.summary()["today"]
        assert today["local"]["turns"] == 1 and today["cost"] > 0 and today["turns"] == 1  # 本机进"本机"那栏, 云端那栏只有之前那笔
    finally:
        L.use(None)


def test_context_error_is_clear_and_not_minimal():
    async def body(llm, fake, base):
        with pytest.raises(C.CloudError) as err:
            await _collect(llm, [{"role": "user", "content": "很长"}], G.SCHEMA)
        return err.value, llm.minimal, len(fake.requests)

    err, minimal, n = with_local([ctx_full(8192)], body)
    assert err.kind == "context" and err.n_ctx == 8192 and "8192" in str(err) and "本机模型" in str(err)
    assert not minimal and n == 1  # 去掉参数也装不下: 不白试一次, 也不把模型改成"只发最基本的"


def test_connect_error_says_service_down():
    async def main():
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        llm = C.CloudLLM(local_spec(f"http://127.0.0.1:{port}/v1"), {})
        try:
            with pytest.raises(C.CloudError) as err:
                await _collect(llm, [{"role": "user", "content": "hi"}])
            info = await llm.probe()
            return err.value, info
        finally:
            await llm.close()

    err, info = asyncio.run(main())
    assert err.kind == "connect" and "服务没开" in str(err) and "127.0.0.1:" in str(err)
    assert info["ok"] is False and "连不上" in C.probe_notes(info, "http://127.0.0.1:1/v1")[0]


# ---------------------------------------------------------------------------
# 问服务 (probe)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind,ctx,vision,server", [("llama", 32768, True, "llama.cpp"), ("ollama", 4096, True, "ollama"),
                                                     ("lmstudio", 16384, False, "lmstudio"), ("vllm", 40960, None, "vllm")])
def test_probe_each_server(kind, ctx, vision, server):
    async def body(llm, fake, base):
        info = await llm.probe()
        again = await llm.probe()
        return info, again is info, C.probe_notes(info, base)

    info, cached, notes = with_local([say("x")], body, kind=kind, ctx=ctx, vision=bool(vision))
    assert info["ok"] and info["server"] == server and info["ctx"] == ctx and info["models"] == ["qwen-local"] and cached
    assert info["vision"] == vision
    if ctx < 16384:
        assert any(str(ctx) in n and "OLLAMA_CONTEXT_LENGTH" in n for n in notes), notes
    else:
        assert notes == []


def test_probe_ctx_from_config_and_cloud_not_probed():
    C.configure({"ctx": 12000})

    async def body(llm, fake, base):
        cloud = C.CloudLLM({"provider": "custom", "base_url": base, "api_key": KEY, "model": "m"}, {})
        try:
            return await llm.probe(), await cloud.probe()
        finally:
            await cloud.close()

    info, cloud = with_local([say("x")], body)
    assert info["ctx"] == 12000 and cloud["ok"] is False and cloud["server"] is None  # 云端的不乱打它的接口


# ---------------------------------------------------------------------------
# 桌宠 (/ws 的 profile)
# ---------------------------------------------------------------------------
def local_profile(base, persona="", model="qwen-local"):
    return {"type": "profile", "persona": persona, "skills": [], "model": {"provider": "selfhost", "base_url": base, "model": model}}


def run_pet(tmp_path, fake, scenario, *extra):
    async def main():
        base = await fake.start()
        client = TestClient(TestServer(create_app(make_cfg(tmp_path, *extra))))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            assert json.loads((await ws.receive()).data)["type"] == "hello"
            return await scenario(ws, base, client.server.app)
        finally:
            await client.close()
            await fake.close()
    return asyncio.run(main())


def test_ws_selfhost_turn_wallet_and_near_context(tmp_path, monkeypatch):
    reply = '{"op":"act","acts":[{"do":"raise","side":"R"}],"emo":"happy","reply":"举起来啦。"}'
    monkeypatch.setattr(L, "wallet_line", lambda: "[钱包] 测试钱包")
    fake = FakeLocal(say(reply), ctx=8192)

    async def scenario(ws, base, app):
        app[ENGINES].channels.pet_context = lambda: "[近况] 测试近况"
        await ws.send_json(local_profile(base, persona="说话轻快."))
        done = await recv(ws, "profile_done")
        await ws.send_json({"type": "text", "text": "举起右手"})
        log = await collect(ws, 1)
        await asyncio.sleep(0.3)  # 本机大模型走的是 OpenAI 兼容那条路: 不做 llama.cpp 的 KV 预热和保温
        return done, log

    done, log = run_pet(tmp_path, fake, scenario)
    assert done["ok"] and done["llm"]["local"] is True and done["llm"]["provider"] == "selfhost"
    assert any("8192" in w for w in done["warnings"]), done["warnings"]  # 上下文短: 面板上提醒
    intent = next(m for m in log if m["type"] == "intent")
    assert intent["op"] == "act" and [a["do"] for a in intent["acts"]] == ["raise"] and _said(log) == "举起来啦。"
    assert len(fake.requests) == 1
    b = fake.requests[0]["body"]
    sent = json.dumps(b["messages"], ensure_ascii=False)
    assert "测试近况" in sent and "测试钱包" not in sent  # 近况照带; 钱包不带 (这时说话不花钱)
    assert b["response_format"]["type"] == "json_schema" and b["chat_template_kwargs"] == {"enable_thinking": False}
    trace = next(m for m in log if m.get("type") == "trace")["trace"]
    assert trace["meta"]["llm"]["cloud"]["model"] == "selfhost:qwen-local"


def test_ws_selfhost_near_switch(tmp_path):
    """llm.local_api.near=false: 本机模型不带近况 (小模型会照抄); 云端照旧带."""
    reply = '{"op":"chat","acts":[],"emo":"happy","reply":"嗯嗯。"}'
    fake = FakeLocal(say(reply))

    async def scenario(ws, base, app):
        app[ENGINES].channels.pet_context = lambda: "[近况] 测试近况"
        await ws.send_json(local_profile(base))
        await recv(ws, "profile_done")
        await ws.send_json({"type": "text", "text": "你在干嘛"})
        await collect(ws, 1)

    run_pet(tmp_path, fake, scenario, "llm.local_api.near=false")
    assert "测试近况" not in json.dumps(fake.requests[0]["body"]["messages"], ensure_ascii=False)


def test_ws_selfhost_tool_roundtrip(tmp_path):
    fake = FakeLocal(call(("weather", '{"city":"上海"}')), say('{"op":"chat","acts":[],"emo":"happy","reply":"上海晴天。"}'))

    async def scenario(ws, base, app):
        await ws.send_json({"type": "tools", "tools": WEATHER_TOOLS})
        await ws.send_json(local_profile(base))
        done = await recv(ws, "profile_done")
        await ws.send_json({"type": "text", "text": "上海天气怎么样"})
        tc = await recv(ws, "tool_call")
        await ws.send_json({"type": "tool_result", "call_id": tc["call_id"], "content": "晴, 20 度"})
        log = await collect(ws, 1)
        return done, tc, log

    done, tc, log = run_pet(tmp_path, fake, scenario)
    assert done["tools"] == 2 and tc["name"] == "weather" and tc["arguments"] == {"city": "上海"}
    assert _said(log) == "上海晴天。" and "工具 weather" in fake.requests[1]["body"]["messages"][-1]["content"]


def test_ws_selfhost_down_warns_and_interrupt_cuts_stream(tmp_path):
    fake = FakeLocal()
    fake.script = [dribble(fake, '{"op":"chat","acts":[],"emo":"happy","reply":"' + "啦" * 200 + '"}')]

    async def scenario(ws, base, app):
        await ws.send_json(local_profile("http://127.0.0.1:9/v1"))  # 没开的服务
        down = await recv(ws, "profile_done")
        await ws.send_json(local_profile(base))
        await recv(ws, "profile_done")
        await ws.send_json({"type": "text", "text": "讲个长的"})
        await recv(ws, "reply_delta")
        await ws.send_json({"type": "interrupt"})
        await recv(ws, "interrupted")
        await until(lambda: fake.cut, 5)  # 打断以后服务那边的流断了 (llama-server 看到断开就停止生成)
        return down

    down = run_pet(tmp_path, fake, scenario)
    assert down["ok"] and any("连不上" in w for w in down["warnings"]), down
    assert fake.cut == 1


# ---------------------------------------------------------------------------
# QQ (外部渠道)
# ---------------------------------------------------------------------------
def run_qq_local(tmp_path, scenario, script, *extra, kind="llama", ctx=32768, vision=False):
    async def main():
        bot = FakeOneBot()
        url = await bot.start()
        fake = FakeLocal(*script, kind=kind, ctx=ctx, vision=vision)
        base = await fake.start()
        convs = [{"type": "group", "id": GA, "name": "测试群A", "send": "auto"}]
        client = TestClient(TestServer(create_app(make_cfg(tmp_path / "logs", *qq_over(url, *extra, convs=convs)))))
        await client.start_server()
        bot.app = client.server.app
        ch = bot.app[ENGINES].channels
        ch.selfmcp_home, ch.learned_file = tmp_path / "self-mcp", tmp_path / "learned.json"
        ch.bili_seen_file = tmp_path / "bili_seen.json"
        try:
            ws = await client.ws_connect("/ws")
            inbox = Inbox(ws)
            await inbox.get("hello")
            return await scenario(ws, inbox, bot, base, fake, ch)
        finally:
            await client.close()
            await bot.close()
            await fake.close()
    return asyncio.run(main())


def test_qq_selfhost_reply_schema_and_no_daily_budget(tmp_path):
    async def scenario(ws, inbox, bot, base, fake, ch):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "model": {"provider": "selfhost", "base_url": base, "model": "qwen-local"},
                            "daily_tokens": 100})
        done = await inbox.get("channel_config_done")
        await bot.push(gmsg(GA, 1, "一", 1, at=SELF))  # 一次用掉 120 token: 两次就超过云端额度 (100), 本机照样说
        await until(lambda: len(bot.sent) == 1, 10)
        await bot.push(gmsg(GA, 1, "二", 2, at=SELF))
        await until(lambda: len(bot.sent) == 2, 10)
        state = {"local": ch.cloud.local, "see": ch.can_see(), "limit": ch.budget.limit, "used": ch.budget.used}
        return done, state, [m for m in inbox.of("channel_notice") if m.get("kind") == "budget"]

    done, st, budget = run_qq_local(tmp_path, scenario, [say(reply_json("好呀")), say(reply_json("第二句不一样"))], vision=True)
    assert done["ok"] and not budget and st["limit"] == 0 and st["used"] >= 240  # 不算云端额度, 用量照记
    assert st["local"] and st["see"]  # 服务说它看得了图 (llama-server 挂了 mmproj)


def test_qq_selfhost_request_body(tmp_path):
    async def scenario(ws, inbox, bot, base, fake, ch):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "model": {"provider": "selfhost", "base_url": base, "model": "qwen-local"}})
        await inbox.get("channel_config_done")
        await bot.push(gmsg(GA, 1, "在吗", 1, at=SELF))
        await until(lambda: bot.sent, 10)
        return fake.requests

    reqs = run_qq_local(tmp_path, scenario, [say(reply_json("在的"))])
    b = reqs[-1]["body"]
    assert reqs[-1]["auth"] is None and "tools" not in b  # 访客会话没有工具 (本机模型也一样)
    assert b["response_format"]["json_schema"]["schema"] == G.SCHEMA and b["chat_template_kwargs"] == {"enable_thinking": False}


def test_qq_selfhost_owner_lookup_falls_back_to_ddg(tmp_path, monkeypatch):
    from wm.agent import tools as T
    asked = []

    async def fake_search(query, n=6, searcher=None, meta=None):
        asked.append(query)
        return "1. 本机搜到的标题\n   https://example.com/local\n   一段摘要"

    monkeypatch.setattr(T, "web_search", fake_search)

    async def scenario(ws, inbox, bot, base, fake, ch):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "model": {"provider": "selfhost", "base_url": base, "model": "qwen-local"}})
        await inbox.get("channel_config_done")
        await bot.push(pmsg(OWNER, "帮我查一下本机模型怎么接", 1))
        await until(lambda: bot.sent, 10)
        return fake.requests

    reqs = run_qq_local(tmp_path, scenario, [say(reply_json("查到了"))])
    assert asked and "本机模型" in asked[0]
    sent = json.dumps(reqs[-1]["body"]["messages"], ensure_ascii=False)
    assert "本机搜到的标题" in sent and "https://example.com/local" in sent


# ---------------------------------------------------------------------------
# 工作台
# ---------------------------------------------------------------------------
def run_agent_local(tmp_path, script, scenario, *extra, kind="llama", ctx=32768):
    async def main():
        fake = FakeLocal(*script, kind=kind, ctx=ctx)
        base = await fake.start()
        ws_dir = tmp_path / "ws"
        cfg = make_cfg(tmp_path / "logs", f"agent.workspace={json.dumps(str(ws_dir))}",
                       f"llm.local_api.base_url={json.dumps(base)}", 'llm.local_api.model="qwen-local"', *extra)
        client = TestClient(TestServer(create_app(cfg)))
        await client.start_server()
        try:
            return await scenario(client, fake, ws_dir)
        finally:
            await client.close()
            await fake.close()
    return asyncio.run(main())


async def events_until(ws, pred, timeout=20):
    got = []
    while True:
        msg = await asyncio.wait_for(ws.receive_json(), timeout)
        got.append(msg)
        if pred(msg):
            return got


def test_agent_uses_configured_local_model_with_tools_and_zero_cost(tmp_path):
    script = [call(("write_file", json.dumps({"path": "a.txt", "content": "本机写的\n"}))), say("写好了 a.txt。")]

    async def scenario(client, fake, ws_dir):
        st = await (await client.get("/api/agent")).json()
        s = await (await client.post("/api/agent", json={"action": "new"})).json()
        ws = await client.ws_connect(f"/agent/ws?id={s['id']}")
        await ws.receive_json()
        await ws.send_json({"type": "send", "text": "建个 a.txt"})
        got = await events_until(ws, lambda m: m.get("type") == "done", 30)
        view = await (await client.get(f"/api/agent?id={s['id']}")).json()
        await ws.close()
        return st, got, view, fake.requests

    st, got, view, reqs = run_agent_local(tmp_path, script, scenario, ctx=16384)
    ids = [m["id"] for m in st["models"]]
    assert any(i.startswith("selfhost|") and i.endswith("|qwen-local") for i in ids), st["models"]
    assert any(m["label"] == "本机模型 · qwen-local" for m in st["models"])
    assert (tmp_path / "ws" / "a.txt").read_text(encoding="utf-8") == "本机写的\n"
    first = reqs[0]["body"]
    assert reqs[0]["auth"] is None and first["tools"] and "response_format" not in first
    assert first["chat_template_kwargs"] == {"enable_thinking": True}  # 思考强度默认最高: 开着想
    assert first["max_tokens"] == 16384 // 4  # 上下文短: 一次最多写的跟着缩
    assert view["stats"]["cost"] == 0 and view["stats"]["unpriced"] == 0 and view["stats"]["miss"] > 0
    from wm.agent import core as A
    credit = A._json_chars(first["tools"]) * (1 - A.LOCAL_CHARS_PER_TOKEN / A.TOOL_CHARS_PER_TOKEN)
    assert view["fold_chars"] == int((16384 - 4096) * A.LOCAL_CHARS_PER_TOKEN + credit)  # 按本机的上下文算的折叠阈值


def test_agent_local_context_full_folds_and_retries(tmp_path):
    big = "很长的输出" * 300

    async def scenario(client, fake, ws_dir):
        from wm.app import AGENT
        s = await (await client.post("/api/agent", json={"action": "new"})).json()
        sess = client.server.app[AGENT].get(s["id"])
        # 先塞几轮很长的工具结果 (像她读了几个大文件), 再让她接着做
        for i in range(8):
            cid = f"c{i}"
            sess.messages += [{"role": "user", "content": f"第{i}件"},
                              {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function",
                                                                                   "function": {"name": "read_file", "arguments": "{}"}}]},
                              {"role": "tool", "tool_call_id": cid, "content": big}]
        sess.ensure_message_ids()
        ws = await client.ws_connect(f"/agent/ws?id={s['id']}")
        await ws.receive_json()
        await ws.send_json({"type": "send", "text": "接着做"})
        got = await events_until(ws, lambda m: m.get("type") in ("done", "error"), 30)
        await ws.close()
        return got, fake.requests

    # 上下文给得很大: 不提早折, 等服务说装不下了再折 (只留最后两条) 再试
    got, reqs = run_agent_local(tmp_path, [ctx_full(8192), say("折好了, 做完了。")], scenario, ctx=1000000)
    notes = [m["text"] for m in got if m.get("type") == "note"]
    assert any("装不下" in n and "折起来" in n for n in notes), notes
    assert got[-1]["type"] == "done" and not [m for m in got if m.get("type") == "error"]
    folded = [m for m in reqs[-1]["body"]["messages"] if m.get("role") == "tool" and "折起来了" in m.get("content", "")]
    assert len(folded) >= 6  # 只留最后两条没折


# ---------------------------------------------------------------------------
# 检测接口 /api/llm/local (面板上"检测"用)
# ---------------------------------------------------------------------------
def test_api_llm_local(tmp_path):
    async def main():
        fake = FakeLocal(say("x"), kind="ollama", ctx=4096, vision=True)
        base = await fake.start()
        cfg = make_cfg(tmp_path / "logs", f"llm.local_api.base_url={json.dumps(base)}", 'llm.local_api.model="qwen-local"')
        client = TestClient(TestServer(create_app(cfg)))
        await client.start_server()
        try:
            listed = await (await client.get("/api/llm/local")).json()
            one = await (await client.get("/api/llm/local", params={"base_url": base, "model": "qwen-local"})).json()
            cloud = await client.get("/api/llm/local", params={"base_url": "https://api.deepseek.com/v1"})
            evil = await client.get("/api/llm/local", headers={"Origin": "http://evil.example"})
            return listed, one, (cloud.status, await cloud.json()), evil.status
        finally:
            await client.close()
            await fake.close()

    listed, one, (cloud_status, cloud), evil = asyncio.run(main())
    item = listed["items"][0]
    assert item["configured"] and item["server"] == "ollama" and item["ctx"] == 4096 and item["vision"] and item["tools"]
    assert any("OLLAMA_CONTEXT_LENGTH" in n for n in item["notes"]) and listed["defaults"]["model"] == "qwen-local"
    assert one["items"][0]["ok"] and one["items"][0]["models"] == ["qwen-local"]
    assert cloud_status == 400 and "本机或局域网" in cloud["error"] and evil == 403  # 云端地址不问; 别的网页来的不理


# ---------------------------------------------------------------------------
# 本地服务看守: 自带的本机大模型 (llm.local_api.server) 默认不开, services.local_api 写 on 才开
# ---------------------------------------------------------------------------
def test_services_local_api_spec(tmp_path):
    from wm import services
    from wm.config import load_config

    exe, model = tmp_path / "llama-server.exe", tmp_path / "big.gguf"
    exe.write_bytes(b"x")
    model.write_bytes(b"x")
    cfg = load_config()
    cfg["llm"]["server"]["exe"] = str(exe)
    cfg["llm"]["local_api"]["server"] = {**cfg["llm"]["local_api"]["server"], "model": str(model), "port": 8799}
    cfg.setdefault("services", {}).pop("local_api", None)
    assert "local_api" not in [s.name for s in services.specs(cfg, python="py")]  # 默认不开 (要几 GB 内存)
    cfg["services"]["local_api"] = "on"
    spec = next(s for s in services.specs(cfg, python="py") if s.name == "local_api")
    assert spec.port == 8799 and spec.cmd[-2:] == ["--which", "local"] and spec.health == "/health"
    assert services.LABEL["local_api"] == "本机大模型" and "local_api" in services.ORDER

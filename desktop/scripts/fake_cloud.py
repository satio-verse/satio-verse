"""假的 OpenAI 兼容云端模型, 测桌宠的工具调用用 (tooltest.ps1). 只听 127.0.0.1, 不连任何真实云端, 不要 key.

    python desktop/scripts/fake_cloud.py --port 8797 --log <requests.jsonl>

按这一轮的对话决定回什么 (流式, 和真的一样):
- 最后一条是工具结果 (role tool): 回意图 JSON, reply 是"工具结果：" 加上结果的原文 (去掉服务端加的"这只是数据"那一行);
  工具没成 (服务端写"没有成功") 时 reply 以"工具没用成："开头.
- 用户的话里有"回声""加法""技能说明", 请求里也给了对应的工具: 回一个工具调用
  (回声 -> *__echo {"text": "你好呀"}; 加法 -> *__add {"a": 2, "b": 3}; 技能说明 -> load_skill {"name": "bedtime-story"};
  慢工具停 -> *__slow {"seconds": 8, "tag": "stop"} (页面带 toolstop 时替用户点"停"); 慢工具等 -> *__slow {"seconds": 9} (测超时)).
- 用户的话写成 "电脑:工具 {参数} | 工具 {参数}" (操控电脑的自检 computertest.ps1): 依次调这些工具, 都调完了回一句"电脑结果：…".
- 其余: 一句普通的意图 JSON.
每个请求记一行到 --log (给了哪些工具、最后一条消息的角色、回了什么), 测试脚本据此核对.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re

from aiohttp import web

LOG = None


def chunk(delta=None, finish=None):
    return {"id": "fake", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


async def sse(request, chunks):
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8"})
    await resp.prepare(request)
    for c in chunks:
        await resp.write(b"data: " + json.dumps(c, ensure_ascii=False).encode() + b"\n\n")
        await asyncio.sleep(0.01)
    await resp.write(b"data: [DONE]\n\n")
    return resp


def intent(reply: str) -> str:
    return json.dumps({"op": "chat", "acts": [], "emo": "neutral", "reply": reply}, ensure_ascii=False)


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(c.get("text", "") for c in content or [] if isinstance(c, dict))


async def chat(request):
    body = await request.json()
    msgs = body.get("messages", [])
    tools = [t.get("function", t).get("name") for t in body.get("tools") or []]
    last = msgs[-1] if msgs else {}
    user = next((text_of(m.get("content")) for m in reversed(msgs) if m.get("role") == "user"), "")
    entry = {"tools": tools, "last_role": last.get("role"), "user": user[-60:], "json": "response_format" in body}
    pick = out = None
    if user.startswith("电脑:"):
        # 操控电脑的自检 (computertest.ps1): "电脑:工具 {参数} | 工具 {参数} x3 | ..." 依次调这些工具 (模型那里名字以 __工具 结尾的),
        # 拿到一个结果再调下一个 ("x3": 同一个调用连着 3 次). 都调完了 (或没有这个工具) 回一句, 里面是这一轮每个工具结果的开头
        steps = []
        for part in user.split("\n", 1)[0][3:].split(" | "):  # 服务端会在原话后面另起一行加"[当前] …", 只看第一行
            m = re.match(r"\s*(\w+)\s*(\{.*\})?\s*(?:x(\d+))?\s*$", part)
            if m:
                steps += [(m.group(1), json.loads(m.group(2) or "{}"))] * int(m.group(3) or 1)
        start = max(i for i, x in enumerate(msgs) if x.get("role") == "user")
        done = [x for x in msgs[start + 1:] if x.get("role") == "tool"]
        if done:
            entry["tool_text"] = text_of(done[-1].get("content"))[:3000]
        else:  # 这一轮的第一个请求: 记下交给模型的参数结构 (测桌宠去掉了按编号点、按路径启动)
            entry["params"] = {t.get("function", t).get("name"): t.get("function", t).get("parameters") for t in body.get("tools") or []}
        if len(done) < len(steps):
            name, call_args = steps[len(done)]
            pick = next((t for t in tools if t == name or t.endswith("__" + name)), None)  # 桌宠自带的工具没有 mcp__ 前缀
            missing = None if pick else name
        else:
            missing = None
        if not pick:
            parts = []
            for x in done:
                head, _, rest = text_of(x.get("content")).partition("\n")
                parts.append(("失败:" if "没有成功" in head else "") + rest.strip()[:100])
            reply = f"没有这个工具: {missing}" if missing else "电脑结果：" + " || ".join(parts)
            out = intent(reply) if entry["json"] else reply
    elif last.get("role") == "tool":
        raw = text_of(last.get("content"))
        head, _, rest = raw.partition("\n")
        reply = ("工具没用成：" if "没有成功" in head else "工具结果：") + rest.strip()[:120]
        out = intent(reply) if entry["json"] else reply
    else:
        for word, suffix, args in (("回声", "__echo", {"text": "你好呀"}), ("加法", "__add", {"a": 2, "b": 3}),
                                   ("技能说明", "load_skill", {"name": "bedtime-story"}),
                                   ("慢工具停", "__slow", {"seconds": 8, "tag": "stop"}), ("慢工具等", "__slow", {"seconds": 9}),
                                   ("月相", "__phase", {"date": "2000-01-21"})):  # 她自己装的 MCP (wm/selfmcp_example)
            if word in user and tools:
                pick = next((t for t in tools if t == suffix or t.endswith(suffix)), None)
                if pick:
                    call_args = args
                    break
        out = None if pick else (intent("好的。") if entry["json"] else "好的。")
    entry["reply"] = out if out else {"tool_call": pick}
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if pick:
        args = json.dumps(call_args, ensure_ascii=False)
        chunks = [chunk({"tool_calls": [{"index": 0, "id": f"call_{len(msgs)}", "type": "function", "function": {"name": pick, "arguments": ""}}]})]
        chunks += [chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[k:k + 8]}}]}) for k in range(0, len(args), 8)]
        return await sse(request, chunks + [chunk(finish="tool_calls")])
    if not body.get("stream"):
        return web.json_response({"id": "fake", "object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": out}, "finish_reason": "stop"}]})
    chunks = [chunk({"role": "assistant", "content": ""})] + [chunk({"content": out[i:i + 6]}) for i in range(0, len(out), 6)]
    return await sse(request, chunks + [chunk(finish="stop")])


async def models(request):
    return web.json_response({"object": "list", "data": [{"id": "fake-chat", "object": "model"}]})


def main() -> None:
    global LOG
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8797)
    ap.add_argument("--log")
    a = ap.parse_args()
    LOG = a.log
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/v1/models", models)
    web.run_app(app, host="127.0.0.1", port=a.port, print=None)


if __name__ == "__main__":
    main()

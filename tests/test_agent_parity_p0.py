"""工作台底层对标 P0 (2026-11-07, docs/agent-parity-20261107.md): 模型请求重试与备用模型 (P0-2)、结构化压缩检查点 (P0-3)、
编辑可靠性 (P0-4: 改前必读、一次改多处、宽松匹配、GBK / UTF-16 / CRLF / BOM 原样存). 全用假云端 (tests/test_cloud.py 的 FakeCloud),
不连真的模型, 不花钱; 重试的等待换成不等 (cloud._sleep)."""
import asyncio
import json
import time

import pytest
from aiohttp import web

from test_cloud import KEY, FakeCloud, _chunk, _sse, call, fail, say, spec
from test_pipeline import make_cfg
from wm import cloud as C
from wm import ledger as L
from wm.agent import compaction as CP
from wm.agent import core
from wm.agent import tools as T


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    waits = []

    async def fake_sleep(s):
        waits.append(s)
    monkeypatch.setattr(C, "_sleep", fake_sleep)
    return waits


def broken(text):
    """流式回一段字以后连接断了 (像网络抖了一下)."""
    async def handle(request, body):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8"})
        await resp.prepare(request)
        await resp.write(b"data: " + json.dumps(_chunk({"role": "assistant", "content": text}), ensure_ascii=False).encode() + b"\n\n")
        request.transport.close()
        return resp
    return handle


# ---------------------------------------------------------------------------
# P0-2 重试 (cloud.py)
# ---------------------------------------------------------------------------
def run_cloud(script, opts, body):
    async def main():
        fake = FakeCloud(*script)
        base = await fake.start()
        llm = C.CloudLLM(spec(base), opts)
        try:
            return await body(llm, fake)
        finally:
            await llm.close()
            await fake.close()
    return asyncio.run(main())


async def _chat(llm, meta=None):
    meta = {} if meta is None else meta
    done = None
    async for ev in llm.chat([{"role": "user", "content": "hi"}], None, meta=meta):
        if ev[0] == "done":
            done = ev[1]
    return done, meta


AGENT = {"retries": 5, "retry_kinds": sorted(C.RETRY_KINDS), "read_timeout_s": 5}


def test_retries_transient_errors_before_first_token(_no_wait):
    script = [fail(503, "busy"), fail(429, "slow down", headers={"Retry-After": "3"}), say("好了")]

    async def body(llm, fake):
        done, meta = await _chat(llm)
        return done, meta, len(fake.requests)

    done, meta, n = run_cloud(script, AGENT, body)
    assert done["content"] == "好了" and n == 3
    assert [r["kind"] for r in meta["retries"]] == ["server", "rate_limit"]
    assert _no_wait[1] >= 3  # 认 Retry-After
    assert C.retry_delay(1) <= 0.55 and C.retry_delay(9) <= C.RETRY_MAX_S


def test_never_retries_auth_quota_or_bad_request():
    for status, tries in ((401, 1), (402, 1), (400, 2)):  # 400: 原来就有的"去掉可选参数再试一次", 不算重试
        def body(llm, fake):
            async def go():
                meta = {}
                with pytest.raises(C.CloudError):
                    await _chat(llm, meta)
                return len(fake.requests), meta.get("retries")
            return go()
        assert run_cloud([fail(status, "no")], AGENT, body) == (tries, None)


def test_chat_surfaces_retry_once_and_only_fast_errors():
    """桌宠 / QQ 默认: 1 次, 只管限流 / 服务出错 / 连不上 (超时、空回复不重来, 要快)."""
    async def ok(llm, fake):
        done, _ = await _chat(llm)
        return done["content"], len(fake.requests)
    assert run_cloud([fail(500, "x"), say("嗯")], {}, ok) == ("嗯", 2)

    async def timeout(llm, fake):
        with pytest.raises(C.CloudError) as err:
            await _chat(llm)
        return err.value.kind, len(fake.requests)
    assert run_cloud([fail(504, "slow"), say("嗯")], {}, timeout) == ("timeout", 1)

    async def empty(llm, fake):
        done, _ = await _chat(llm)
        return done["content"], len(fake.requests)
    assert run_cloud([say(""), say("嗯")], {}, empty) == ("", 1)


def test_agent_retries_empty_reply():
    async def body(llm, fake):
        done, meta = await _chat(llm)
        return done["content"], [r["kind"] for r in meta["retries"]]
    assert run_cloud([say(""), say("有了")], AGENT, body) == ("有了", ["empty"])


def test_half_streamed_failure_is_not_retried_inside_cloud():
    """交出一半才断的, cloud.py 不自己重来 (桌宠已经念出去了); 交给调用方."""
    async def body(llm, fake):
        got = []
        with pytest.raises(C.CloudError) as err:
            async for ev in llm.chat([{"role": "user", "content": "hi"}], None, meta={}):
                got.append(ev)
        return err.value.kind, got, len(fake.requests)
    kind, got, n = run_cloud([broken("说到一半"), say("完整")], AGENT, body)
    assert kind in C.RETRY_KINDS and got[0] == ("text", "说到一半") and n == 1


# ---------------------------------------------------------------------------
# 工作台里的重试、断流重来、备用模型
# ---------------------------------------------------------------------------
def hub_run(tmp_path, script, scenario, *extra):
    """不经页面, 直接建 AgentHub + 假云端 (账本登记了它, 工作台拿它的 key 另建一个)."""
    async def main():
        fake = FakeCloud(*script)
        base = await fake.start()
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        cfg = make_cfg(tmp_path / "logs", f"agent.workspace={json.dumps(str(ws))}", *extra)
        led = L.Ledger(tmp_path / "ledger", cfg.get("billing"))
        L.use(led)
        llm = C.CloudLLM(spec(base))
        hub = core.AgentHub(cfg, home=tmp_path / "agent")
        try:
            return await scenario(hub, fake, ws)
        finally:
            await llm.close()
            await fake.close()
            L.use(None)
    return asyncio.run(main())


async def run_turn(hub, s, text, timeout=20):
    events = []

    async def on(ev):
        events.append(ev)
    hub.subscribe(s.id, on)
    why = await hub.send(s.id, text)
    assert why is None, why
    await asyncio.wait_for(s.task, timeout)
    for _ in range(50):
        if s.status == "idle":
            break
        await asyncio.sleep(0.02)
    return events


def test_agent_survives_transient_errors_and_falls_back(tmp_path):
    script = [fail(503, "busy")] * 6 + [say("备用模型做完了")]

    async def scenario(hub, fake, ws):
        s = hub.new_session(str(ws))
        events = await run_turn(hub, s, "做点事")
        return s, events, [r["body"]["model"] for r in fake.requests]

    s, events, models = hub_run(tmp_path, script, scenario, 'agent.fallback_models={"fake-chat": "fake-backup"}')
    notes = [e["text"] for e in events if e["type"] == "note"]
    assert sum(t.startswith("连接不稳") for t in notes) == 5 and any("换 fake-backup 做" in t for t in notes)
    assert models[:6] == ["fake-chat"] * 6 and models[-1] == "fake-backup"
    assert s.run["state"] == "completed" and s.run["fallback"]["to"] == "fake-backup" and s.run["retries"] == 5
    assert any(e["type"] == "assistant" and "备用模型做完了" in e["text"] for e in events)


def test_agent_without_fallback_reports_error_after_retries(tmp_path):
    async def scenario(hub, fake, ws):
        s = hub.new_session(str(ws))
        events = await run_turn(hub, s, "做点事")
        return s, events, len(fake.requests)

    s, events, n = hub_run(tmp_path, [fail(503, "busy")], scenario)  # fake-chat 没配备用
    assert n == 6 and s.run["state"] == "failed" and any(e["type"] == "error" for e in events)


def broken_call(name, args):
    """流了一半工具调用以后连接断了 (工具调用的片段不推给页面, 工作台要靠 CloudError.partial 认出来)."""
    async def handle(request, body):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8"})
        await resp.prepare(request)
        chunk = _chunk({"tool_calls": [{"index": 0, "id": "call_x", "type": "function", "function": {"name": name, "arguments": args[:5]}}]})
        await resp.write(b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n")
        request.transport.close()
        return resp
    return handle


def test_agent_redoes_step_cut_during_tool_call(tmp_path):
    async def scenario(hub, fake, ws):
        s = hub.new_session(str(ws))
        events = await run_turn(hub, s, "看看目录")
        return s, events

    s, events = hub_run(tmp_path, [broken_call("list_dir", json.dumps({"path": "."})), call(("list_dir", "{}")), say("看完了")], scenario)
    assert any("丢掉重来" in e.get("text", "") for e in events) and s.run["state"] == "completed"
    assert not any(e["type"] == "error" for e in events)


def test_agent_discards_half_streamed_step_and_redoes_it(tmp_path):
    async def scenario(hub, fake, ws):
        s = hub.new_session(str(ws))
        events = await run_turn(hub, s, "说点什么")
        return s, events

    s, events = hub_run(tmp_path, [broken("说到一半"), say("这次说完整了")], scenario)
    kinds = [e["type"] for e in events]
    assert "stream_reset" in kinds and any("丢掉重来" in e.get("text", "") for e in events)
    answer = [m for m in s.messages if m["role"] == "assistant"]
    assert answer[-1]["content"] == "这次说完整了" and s.run["state"] == "completed"


# ---------------------------------------------------------------------------
# P0-3 结构化压缩
# ---------------------------------------------------------------------------
def _turn(i, size=400):
    return [{"role": "user", "content": f"第{i}件事" + "要" * size, "_id": f"u{i}"},
            {"role": "assistant", "content": "", "_id": f"a{i}", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": "结果" * size, "_id": f"t{i}"},
            {"role": "assistant", "content": f"第{i}件做完了" + "了" * size, "_id": f"d{i}"}]


def test_choose_cut_keeps_tool_pairs_and_prefers_turn_starts():
    msgs = sum((_turn(i) for i in range(6)), [])
    cut, split = CP.choose_cut(msgs, keep_chars=3000)
    assert msgs[cut]["role"] == "user" and not split and cut >= 4
    assert sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs[cut:]) >= 3000
    # 一整轮自己就很长: 切在这一轮中间 (不在工具结果前面)
    one = [{"role": "user", "content": "一件大事", "_id": "u"}]
    for i in range(12):
        one += [{"role": "assistant", "content": "", "_id": f"a{i}", "tool_calls": [{"id": f"c{i}", "type": "function",
                                                                                     "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 800, "_id": f"t{i}"}]
    cut, split = CP.choose_cut(one, keep_chars=2000, max_keep_chars=4000)
    assert split and one[cut]["role"] == "assistant" and one[cut - 1]["role"] == "tool"
    assert CP.choose_cut(msgs[:3], keep_chars=10) is None


def test_serialize_truncates_tool_results_and_keeps_tail():
    text = CP.serialize(sum((_turn(i, 2000) for i in range(3)), []), tool_chars=500, limit=6000)
    assert "[主人]" in text and "[调用 list_dir]" in text and "中间省略" in text and len(text) <= 6100
    assert text.startswith("…(更早的记录省略了")


SUMMARY = "## 目标\n- 测试压缩\n## 下一步\n- 接着做"


def test_budget_compaction_writes_checkpoint_and_keeps_originals(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "FOLD_CHARS", 9000)

    async def scenario(hub, fake, ws):
        s = hub.new_session(str(ws))
        s.messages = sum((_turn(i) for i in range(8)), [])
        hub.save(s)
        events = await run_turn(hub, s, "接着做")
        found = hub.history_search(s.id, "第0件事")
        return s, events, fake.requests, found, hub.home

    s, events, reqs, found, home = hub_run(tmp_path, [say(SUMMARY), say("接着做好了")], scenario)
    ask = reqs[0]["body"]["messages"]  # 第一次请求: 照平时那份重放, 最后追加写检查点的话 (前缀一样, 缓存能命中)
    step = reqs[1]["body"]["messages"]
    assert "检查点" in ask[-1]["content"] and ask[1]["content"] == "第0件事" + "要" * 400
    assert step[1]["role"] == "user" and step[1]["content"].startswith(CP.CKPT_PREFIX) and "测试压缩" in step[1]["content"]
    assert step[0] == ask[0]  # 系统提示没变
    assert s.compacted and s.compacted[0]["_id"] == "u0" and found["items"][0]["compacted"]
    assert any(e["type"] == "compact" for e in events) and s.run["compactions"] == 1
    tool_ids = {m.get("tool_call_id") for m in s.messages if m["role"] == "tool"}
    calls = {c["id"] for m in s.messages for c in m.get("tool_calls") or []}
    assert tool_ids == calls  # 留下来的调用和结果还是一对一对的
    saved = json.loads((home / "sessions" / f"{s.id}.json").read_text("utf-8"))
    assert saved["compacted"] and saved["messages"][0]["_ckpt"]


def test_overflow_compacts_with_span_prompt_then_retries(tmp_path):
    overflow = fail(400, "This model's maximum context length is 32768 tokens")

    async def scenario(hub, fake, ws):
        s = hub.new_session(str(ws))
        s.messages = sum((_turn(i, 100) for i in range(8)), [])
        for m in s.messages:
            if m["role"] == "tool":
                m["content"] = "短"
        hub.save(s)
        events = await run_turn(hub, s, "接着做")
        return s, events, fake.requests

    s, events, reqs = hub_run(tmp_path, [overflow, say(SUMMARY), say("装下了")], scenario)
    span = reqs[1]["body"]
    assert "tools" not in span and "交接用的检查点" in span["messages"][0]["content"] and "<记录>" in span["messages"][1]["content"]
    assert reqs[2]["body"]["messages"][1]["content"].startswith(CP.CKPT_PREFIX)
    assert s.run["state"] == "completed" and s.compacted


def test_manual_compact_with_focus(tmp_path):
    async def scenario(hub, fake, ws):
        s = hub.new_session(str(ws))
        s.messages = sum((_turn(i) for i in range(6)), [])
        hub.save(s)
        why = await hub.send(s.id, "/compact 留住第3件事的结论")
        return s, why, fake.requests

    s, why, reqs = hub_run(tmp_path, [say(SUMMARY)], scenario)
    assert why is None and "留住第3件事的结论" in reqs[0]["body"]["messages"][-1]["content"]
    assert s.messages[0]["content"].startswith(CP.CKPT_PREFIX) and s.status == "idle"


# ---------------------------------------------------------------------------
# P0-4 编辑可靠性
# ---------------------------------------------------------------------------
def test_stale_reason_requires_read_before_overwrite_and_detects_changes(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\n", encoding="utf-8")
    assert "还没读过" in T.stale_reason(f, None, "write_file")
    assert T.stale_reason(f, None, "edit_file") is None  # edit 有 old 一字不差兜着
    seen = T.observe(f)
    assert T.stale_reason(f, seen, "write_file") is None
    time.sleep(0.01)
    f.write_text("two\n", encoding="utf-8")
    assert "被改过" in T.stale_reason(f, seen, "edit_file")
    same = T.observe(f)
    import os
    os.utime(f, ns=(same[0] + 5_000_000_000, same[0] + 5_000_000_000))  # 只碰了修改时间, 内容一样: 不算改过
    assert T.stale_reason(f, same, "edit_file") is None
    assert T.stale_reason(tmp_path / "new.txt", None, "write_file") is None


def test_multi_edit_is_all_or_nothing(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="第 2 处"):
        T.edit_file(f, "", "", edits=[{"old": "a = 1", "new": "a = 10"}, {"old": "zzz", "new": "y"}])
    assert f.read_text("utf-8") == "a = 1\nb = 2\nc = 3\n"
    msg, before = T.edit_file(f, "", "", edits=[{"old": "a = 1", "new": "a = 10"}, {"old": "c = 3", "new": "c = 30"}])
    assert f.read_text("utf-8") == "a = 10\nb = 2\nc = 30\n" and "分 2 段" in msg and before.startswith("a = 1")
    with pytest.raises(ValueError, match="一样"):
        T.edit_file(f, "b = 2", "b = 2")


def test_tolerant_matching_trailing_space_indent_and_quotes(tmp_path):
    f = tmp_path / "t.py"
    f.write_text("def f():\n    if x:   \n        return 'a'\n    return 'b'\n", encoding="utf-8")
    msg, _ = T.edit_file(f, "    if x:\n        return 'a'", "    if x:\n        return 'A'")
    assert "行尾空格" in msg and "return 'A'" in f.read_text("utf-8")
    g = tmp_path / "g.py"
    g.write_text("class K:\n    def m(self):\n        a = 1\n        return a\n", encoding="utf-8")
    msg, _ = T.edit_file(g, "def m(self):\n    a = 1\n    return a", "def m(self):\n    a = 2\n    return a * 2")
    assert "整体缩进" in msg and g.read_text("utf-8") == "class K:\n    def m(self):\n        a = 2\n        return a * 2\n"
    h = tmp_path / "h.md"
    h.write_text("他说\u201c你好\u201d\n下一行\n", encoding="utf-8")
    msg, _ = T.edit_file(h, '他说"你好"', '他说"再见"')
    assert "引号" in msg and "再见" in h.read_text("utf-8")
    amb = tmp_path / "amb.txt"
    amb.write_text("x  \nx\n", encoding="utf-8")
    with pytest.raises(ValueError):  # 宽松匹配有两处对得上: 不猜
        T.edit_file(amb, "x \n", "y\n")


def test_gbk_utf16_crlf_and_bom_round_trip(tmp_path):
    gbk = tmp_path / "s.ini"
    gbk.write_bytes("; 中文注释\r\nport = 8000\r\n".encode("gbk"))
    assert "中文注释" in T.read_file(gbk) and "GBK 编码" in T.read_file(gbk)
    msg, _ = T.edit_file(gbk, "port = 8000", "port = 9000")
    assert gbk.read_bytes() == "; 中文注释\r\nport = 9000\r\n".encode("gbk") and "GBK" in msg
    u16 = tmp_path / "u.txt"
    u16.write_bytes(b"\xff\xfe" + "第一行\r\n第二行\r\n".encode("utf-16-le"))
    assert "第二行" in T.read_file(u16)
    T.edit_file(u16, "第二行", "第2行")
    assert u16.read_bytes() == b"\xff\xfe" + "第一行\r\n第2行\r\n".encode("utf-16-le")
    crlf = tmp_path / "w.ps1"
    crlf.write_bytes(b"\xef\xbb\xbfa\r\nb\r\n")
    msg = T.write_file(crlf, "x\ny\n")
    assert crlf.read_bytes() == b"\xef\xbb\xbfx\r\ny\r\n" and "Windows 换行" in msg and "BOM" in msg
    old = tmp_path / "old.ini"
    old.write_bytes("a = 1\n".encode("gbk") + "说明\n".encode("gbk"))
    T.write_file(old, "a = 2\n说明\n")
    assert old.read_bytes() == "a = 2\n说明\n".encode("gbk")


def test_agent_refuses_blind_overwrite_and_stale_edit(tmp_path):
    script = [call(("write_file", json.dumps({"path": "keep.txt", "content": "新的\n"}))),
              call(("read_file", json.dumps({"path": "keep.txt"}))),
              call(("write_file", json.dumps({"path": "keep.txt", "content": "新的\n"}))),
              say("好了")]

    async def scenario(hub, fake, ws):
        (ws / "keep.txt").write_text("主人写的\n", encoding="utf-8")
        s = hub.new_session(str(ws), mode="full")
        events = await run_turn(hub, s, "改一下 keep.txt")
        return events, (ws / "keep.txt").read_text("utf-8")

    events, final = hub_run(tmp_path, script, scenario)
    results = [e for e in events if e["type"] == "tool_result"]
    assert not results[0]["ok"] and "还没读过" in results[0]["text"]
    assert results[1]["ok"] and results[2]["ok"] and final == "新的\n"


def test_artifact_kind_is_only_a_hint(tmp_path):
    async def scenario(hub, fake, ws):
        (ws / "report.md").write_text("# 报告\n", encoding="utf-8")
        s = hub.new_session(str(ws))
        s.run = {"id": "r" * 20, "started": time.time(), "state": "running"}
        item = await hub.studio.call(s, "artifact_register", {"path": "report.md", "kind": "document"})
        return item

    item = hub_run(tmp_path, [say("x")], scenario)
    assert item["kind"] == "text" and item["path"].endswith("report.md")

"""工作台完整上下文字符预算与费用归属: 全部用内存假模型/临时目录, 不访问云端."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from wm import cloud as C
from wm import ledger as L
from wm.agent import core as A
from wm.agent import tools as T


class FakeModel:
    provider, model = "deepseek", "test-model"

    def __init__(self, reject_reasoning=False):
        self.requests = []
        self.reject_reasoning = reject_reasoning

    async def chat(self, messages, tools, **kwargs):
        self.requests.append(deepcopy({"messages": messages, "tools": tools}))
        if self.reject_reasoning and len(self.requests) == 1:
            raise C.CloudError("bad_request", "reasoning_content unsupported")
        yield "done", {"content": "完成", "tool_calls": [], "reasoning": ""}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "current", lambda: None)
    hub = A.AgentHub({}, home=tmp_path / "agent")
    s = A.Session("test-budget", str(tmp_path))
    return hub, s


def call(cid="read-1", args=None):
    return {"id": cid, "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps(args or {"path": "a.txt"}, ensure_ascii=False)}}


def old_result(s, size=5000):
    s.messages = [{"role": "assistant", "content": "", "reasoning_content": "完整的旧思考",
                   "tool_calls": [call()]},
                  {"role": "tool", "tool_call_id": "read-1", "content": "文" * size}]
    s.messages += [{"role": "user", "content": f"后续 {i}"} for i in range(A.KEEP_TAIL)]


def ask(hub, s, tools=None, llm=None):
    llm = llm or FakeModel()
    asyncio.run(hub._ask_model(s, llm, tools or []))
    return llm


def test_full_budget_matches_sent_json_and_does_not_store_background(session):
    hub, s = session
    s.messages = [{"role": "user", "content": "你好\n\"测试\""},
                  {"role": "assistant", "content": "继续", "reasoning_content": "完整思考" * 100,
                   "tool_calls": [call(args={"path": "中文.txt", "note": "长参数" * 100})]},
                  {"role": "tool", "tool_call_id": "read-1", "content": "工具输出"}]
    s.backgrounds[0] = "\n背景和换行\n"
    s.ensure_message_ids()  # UI metadata migrates once; verify semantic history remains unchanged.
    original = deepcopy(s.messages)
    tools = [{"type": "function", "function": {"name": "read_file", "description": "工具定义" * 100}}]
    llm = ask(hub, s, tools)
    sent = llm.requests[0]
    measured = s.last_request_context
    expected = len(json.dumps(sent, ensure_ascii=False, separators=(",", ":")))
    assert measured["chars"] == expected == sum(measured["parts"].values())
    assert all(v > 0 for v in measured["parts"].values())
    assert measured["background_chars"] == len(json.dumps(s.backgrounds[0], ensure_ascii=False)) - 2
    assert measured["unit"] == s.view()["context_unit"] == "normalized_json_chars"
    assert s.view()["context_chars"] == expected
    assert s.messages == original == s.to_json()["messages"]
    assert sent["messages"][1]["content"] == original[0]["content"] + s.backgrounds[0]


@pytest.mark.parametrize("component", ["system", "tools", "reasoning", "tool_calls", "background"])
def test_each_previously_missing_component_triggers_fold(session, monkeypatch, component):
    hub, s = session
    monkeypatch.setattr(A, "FOLD_CHARS", 25_000)
    old_result(s)
    tools = []
    if component == "system":
        monkeypatch.setattr(hub, "_system", lambda _: "系统" * 20_000)
    elif component == "tools":
        tools = [{"type": "function", "function": {"name": "x", "description": "定义" * 20_000}}]
    elif component == "reasoning":
        s.messages[0]["reasoning_content"] = "思考" * 20_000
    elif component == "tool_calls":
        s.messages[0]["tool_calls"][0]["function"]["arguments"] = json.dumps({"path": "路" * 40_000})
    else:
        s.backgrounds[2] = "背景" * 20_000
    s.ensure_message_ids()
    untouched = deepcopy(s.messages[0])
    assert sum(len(m["content"]) for m in s.messages) < A.FOLD_CHARS
    llm = ask(hub, s, tools)
    assert "折起来了" in s.messages[1]["content"]
    assert s.messages[0] == untouched  # 完整 reasoning、参数与调用 ID 原样保留
    assert len(s.messages) == 2 + A.KEEP_TAIL
    assert llm.requests[0]["messages"][2]["tool_call_id"] == "read-1"
    assert s.last_request_context["over_budget"]  # 无法折叠的部分仍在, 不假称已回到阈值内


def test_fold_recounts_sent_context_and_preserves_tool_batch_and_tail(session, monkeypatch):
    hub, s = session
    monkeypatch.setattr(A, "FOLD_CHARS", 8000)
    old_result(s, size=30_000)
    s.messages[0]["tool_calls"].append(call("read-2"))
    s.messages.insert(2, {"role": "tool", "tool_call_id": "read-2", "content": "二" * 1000})
    s.ensure_message_ids()  # UI metadata migrates once; verify semantic history remains unchanged.
    original = deepcopy(s.messages)
    llm = ask(hub, s)
    assert not s.last_request_context["over_budget"]
    assert s.last_request_context["chars"] == len(json.dumps(llm.requests[0], ensure_ascii=False, separators=(",", ":")))
    assert s.messages[0] == original[0] and s.messages[-A.KEEP_TAIL:] == original[-A.KEEP_TAIL:]
    assert [m["tool_call_id"] for m in s.messages if m["role"] == "tool"] == ["read-1", "read-2"]
    assert all("折起来了" in m["content"] for m in s.messages[1:3])
    assert hub._fold(s, force=True, tools=[]) == 0  # 已折过的不反复改写前缀


@pytest.mark.parametrize("kind", ["incomplete", "tail", "orphan"])
def test_incomplete_recent_or_unpaired_tool_results_are_not_folded(session, kind):
    hub, s = session
    old_result(s)
    if kind == "incomplete":
        s.messages[0]["tool_calls"].append(call("not-returned"))
    elif kind == "tail":
        s.messages.pop()  # 结果位于 KEEP_TAIL 内, 整组保留
    else:
        s.messages.pop(0)
    s.ensure_message_ids()  # UI metadata migrates once; verify semantic history remains unchanged.
    original = deepcopy(s.messages)
    assert hub._fold(s, force=True, tools=[]) == 0
    assert s.messages == original


def test_over_budget_without_foldable_results_is_visible_and_bounded(session, monkeypatch):
    hub, s = session
    monkeypatch.setattr(A, "FOLD_CHARS", 5000)
    reasoning = "不可裁剪的思考" * 2000
    s.messages = [{"role": "assistant", "content": "", "reasoning_content": reasoning}]
    llm = FakeModel()
    ask(hub, s, llm=llm)
    ask(hub, s, llm=llm)
    assert len(llm.requests) == 2
    assert all(r["messages"][1]["reasoning_content"] == reasoning for r in llm.requests)
    assert s.view()["context_over_budget"]
    assert len([e for e in s.events if "软阈值" in e.get("text", "")]) == 1


def test_reasoning_compatibility_retry_is_measured_without_mutating_history(session):
    hub, s = session
    s.messages = [{"role": "assistant", "content": "", "reasoning_content": "完整思考", "tool_calls": [call()]},
                  {"role": "tool", "tool_call_id": "read-1", "content": "结果"}]
    s.ensure_message_ids()  # UI metadata migrates once; verify semantic history remains unchanged.
    original = deepcopy(s.messages)
    llm = ask(hub, s, llm=FakeModel(reject_reasoning=True))
    assert len(llm.requests) == 2 and s.no_reason
    assert llm.requests[0]["messages"][1]["reasoning_content"] == "完整思考"
    assert "reasoning_content" not in llm.requests[1]["messages"][1]
    assert s.last_request_context["parts"]["reasoning"] == 0
    assert s.last_request_context["chars"] == len(json.dumps(llm.requests[1], ensure_ascii=False, separators=(",", ":")))
    assert s.messages == original  # 重试副本不意外改掉持久化历史
    assert llm.requests[1]["messages"][1]["tool_calls"] == original[0]["tool_calls"]


def test_view_refreshes_system_and_offered_tools_on_settings(session):
    hub, s = session
    hub.sessions[s.id] = s
    before = hub.get(s.id).view()
    s.mode, s.web = "read", False
    after = hub.get(s.id).view()
    assert after["context_chars"] != before["context_chars"]
    assert {t["function"]["name"] for t in s.context_tools} == set(A.P.READ_TOOLS) | A.ST.READ_ONLY | {'task_plan'}
    assert A.MODE_TEXT["read"] in s.context_system


def test_read_file_character_cursor_is_forwarded(session, monkeypatch):
    hub, s = session
    received = []

    def read(path, offset, limit, char_offset):
        received.append((path, offset, limit, char_offset))
        return "测试页"

    monkeypatch.setattr(T, "read_file", read)
    request = call(args={"path": "a.txt", "offset": 7, "limit": 9, "char_offset": 120_000})
    out, error = asyncio.run(hub._tool(s, request, {"read_file"}))
    assert not error and out == "测试页"
    assert received[0][1:] == (7, 9, 120_000)


def test_search_and_llm_costs_share_agent_bucket_without_inventing_tokens(session, tmp_path, monkeypatch):
    hub, s = session
    led = L.Ledger(tmp_path / "ledger", {"prices": [{"provider": "deepseek", "match": "", "hit": 1, "miss": 2, "out": 3}]})
    monkeypatch.setattr(L, "current", lambda: led)
    monkeypatch.setattr(hub, "_searcher", lambda _: None)

    async def search(query, n, searcher, meta):
        meta["deepseek"] = True
        return "搜索结果"

    monkeypatch.setattr(T, "web_search", search)
    usage = {"hit": 100, "miss": 200, "out": 300}
    led.record("agent", "deepseek", "test-model", usage)
    hub._account(s, SimpleNamespace(provider="deepseek", model="test-model"), usage)
    out, error = asyncio.run(hub._web(s, "search-1", "搜索", "web_search", {"query": "测试"}))
    assert out == "搜索结果" and not error
    rows = [json.loads(line) for path in (tmp_path / "ledger").glob("*.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and {r["who"] for r in rows} == {"agent"}
    assert rows[1]["fee"] and all(k not in rows[1] for k in ("hit", "miss", "out"))
    assert s.stats["cost"] == pytest.approx(sum(r["cost"] for r in rows))
    agg = L._empty()
    for row in rows:
        L._add(agg, row)
    assert set(agg["by_who"]) == {"agent"}
    assert agg["by_who"]["agent"]["cost"] == pytest.approx(s.stats["cost"])
    assert (agg["hit"], agg["miss"], agg["out"], agg["turns"]) == (100, 200, 300, 1)

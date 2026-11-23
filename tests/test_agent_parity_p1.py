"""工作台底层对标 P1 (2026-11-07, docs/agent-parity-20261107.md): 钩子 (P1-6)、技能名单和复盘提示 (P1-7)、重复调用保护 (P1-8)、
MCP 长连接 (P1-9)、全局 / 子目录项目说明 (P1-10)、子任务选预设 / 模型 / 思考强度和"等你点头"事件 (P1-11)、大原文落盘 (P1-12).
全用假云端和假 MCP, 不花钱; 钩子用本机 Python 跑 (exec 写法, 不经过 PowerShell)."""
import asyncio
import json
import sys

import pytest

from test_agent_parity_p0 import hub_run, run_turn
from test_cloud import call, say
from wm.agent import compaction as CP  # noqa: F401 (同一套假云端)
from wm.agent import core
from wm.agent.extensions import ExtensionRegistry


def tool_msgs(req):
    return [m for m in req["body"]["messages"] if m["role"] == "tool"]


# ---------------------------------------------------------------------------
# P1-8 重复调用保护
# ---------------------------------------------------------------------------
def test_identical_calls_get_reminded_then_blocked(tmp_path):
    same = call(("list_dir", json.dumps({"path": "."})))
    script = [same] * 8 + [say("换个思路")]

    async def scenario(hub, fake, ws):
        s = hub.new_session(str(ws))
        events = await run_turn(hub, s, "看看目录")
        return s, events, fake.requests

    s, events, reqs = hub_run(tmp_path, script, scenario)
    third = tool_msgs(reqs[3])[-1]["content"]
    assert "[提醒]" in third and "第 3 次" in third
    assert "[提醒]" not in tool_msgs(reqs[2])[-1]["content"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert results[-1]["ok"] is False and "这次没执行" in results[-1]["text"]
    assert s.run["repeat_blocked"] == 1 and s.run["repeats"] >= 4


def test_changed_results_and_polling_are_not_repeats(tmp_path):
    script = [call(("read_file", json.dumps({"path": "a.txt"}))), call(("write_file", json.dumps({"path": "a.txt", "content": "2\n"}))),
              call(("read_file", json.dumps({"path": "a.txt"}))), call(("read_file", json.dumps({"path": "a.txt"}))),
              call(("delegate_status", "{}")), call(("delegate_status", "{}")), call(("delegate_status", "{}")), say("好")]

    async def scenario(hub, fake, ws):
        (ws / "a.txt").write_text("1\n", encoding="utf-8")
        s = hub.new_session(str(ws), mode="full")
        await run_turn(hub, s, "改一下")
        return s, fake.requests

    s, reqs = hub_run(tmp_path, script, scenario)
    assert not any("[提醒]" in m["content"] for r in reqs for m in tool_msgs(r))
    assert not s.run.get("repeats")


# ---------------------------------------------------------------------------
# P1-10 全局 / 子目录项目说明
# ---------------------------------------------------------------------------
def test_global_and_nested_rules(tmp_path):
    script = [call(("read_file", json.dumps({"path": "sub/a.txt"}))), call(("read_file", json.dumps({"path": "sub/a.txt"}))), say("看完了")]

    async def scenario(hub, fake, ws):
        (hub.home).mkdir(parents=True, exist_ok=True)
        (hub.home / "AGENTS.md").write_text("回答前先说结论。", encoding="utf-8")
        (ws / "sub").mkdir()
        (ws / "sub" / "a.txt").write_text("内容\n", encoding="utf-8")
        (ws / "sub" / "AGENTS.md").write_text("这个目录里的文件改完要跑 check.py", encoding="utf-8")
        s = hub.new_session(str(ws))
        events = await run_turn(hub, s, "读一下 sub/a.txt")
        return events, fake.requests

    events, reqs = hub_run(tmp_path, script, scenario)
    system = reqs[0]["body"]["messages"][0]["content"]
    assert "主人给你的全局说明" in system and "回答前先说结论。" in system
    first, second = tool_msgs(reqs[1])[-1]["content"], tool_msgs(reqs[2])[-1]["content"]
    assert "[这个目录的项目说明 sub/AGENTS.md" in first and "check.py" in first
    assert "项目说明" not in second  # 同一个会话里只附一次
    assert any(e["type"] == "note" and "sub/AGENTS.md" in e["text"] for e in events)


# ---------------------------------------------------------------------------
# P1-11 子任务选项和"等你点头"事件
# ---------------------------------------------------------------------------
def test_subagent_preset_model_effort_and_waiting_event(tmp_path):
    script = [call(("run_command", json.dumps({"command": "python -c \"print(1)\"", "description": "跑一下"}))), say("子任务做完了")]

    async def scenario(hub, fake, ws):
        parent = hub.new_session(str(ws))
        parent.run = {"id": "p" * 20, "started": 0, "state": "running"}
        seen = []

        async def on(ev):
            seen.append(ev)
            if ev["type"] == "subagent" and ev["child"]["status"] == "waiting":
                child = hub.sessions[ev["child"]["id"]]
                hub.answer(child.id, next(iter(child.asks)), "deny")
        hub.subscribe(parent.id, on)
        rows = await hub.teams.delegate(parent, [{"goal": "跑个命令看看", "workspace_mode": "shared", "mode": "workspace",
                                                   "preset": "research", "effort": "off", "model": "fake-chat"}])
        child = hub.sessions[rows[0]["id"]]
        await hub.teams.wait(parent.id, [child.id], 20)
        return child, seen

    child, seen = hub_run(tmp_path, script, scenario)
    assert child.preset == "research" and child.effort == "off" and child.model.endswith("|fake-chat")
    statuses = [e["child"]["status"] for e in seen if e["type"] == "subagent"]
    assert "waiting" in statuses and statuses[-1] == "completed"
    assert statuses.index("waiting") < len(statuses) - 1


def test_subagent_rejects_unknown_preset_or_model(tmp_path):
    async def scenario(hub, fake, ws):
        parent = hub.new_session(str(ws))
        errors = []
        for task in ({"goal": "x", "preset": "nope"}, {"goal": "x", "model": "no-such-model"}, {"goal": "x", "effort": "huge"}):
            with pytest.raises(ValueError) as err:
                await hub.teams.delegate(parent, [task])
            errors.append(str(err.value))
        return errors

    errors = hub_run(tmp_path, [say("x")], scenario)
    assert "预设" in errors[0] and "模型" in errors[1] and "思考强度" in errors[2]


# ---------------------------------------------------------------------------
# P1-12 大原文落盘
# ---------------------------------------------------------------------------
def test_large_tool_originals_spill_to_files(tmp_path):
    script = [call(("run_command", json.dumps({"command": "python -c \"print('x' * 90000)\"", "description": "打很多字"}))), say("好")]

    async def scenario(hub, fake, ws):
        s = hub.new_session(str(ws), mode="full")
        await run_turn(hub, s, "打印很多字")
        mid = next(iter(s.folded_originals))
        pointer = s.folded_originals[mid]
        full = hub.history_read(s.id, mid, limit=20000)
        size = (hub.home / "sessions" / f"{s.id}.json").stat().st_size
        traj = hub.trajectory(s.id)
        return pointer, full, size, traj, hub.home

    pointer, full, size, traj, home = hub_run(tmp_path, script, scenario)
    assert pointer.startswith(core.SPILL_HEAD) and len(pointer) < 200
    assert full["chars"] > 90000 and full["text"].count("x") > 15000 and size < 80000
    assert list((home / "spill").rglob("*.txt"))
    assert any(r.get("original", "").count("x") >= 90000 for r in traj)


# ---------------------------------------------------------------------------
# P1-7 技能名单和复盘提示
# ---------------------------------------------------------------------------
def test_skill_index_in_prompt_and_usage_counted(tmp_path):
    script = [call(("skill_read", json.dumps({"id": "creator:tidy-notes"}))), say("照着技能做完了")]

    async def scenario(hub, fake, ws):
        hub.extensions.create({"id": "tidy-notes", "name": "整理笔记", "files": {
            "SKILL.md": "---\nname: 整理笔记\ndescription: 把乱的笔记按日期整理成目录\n---\n1. 先列出…"}})
        hub.extensions.set_enabled("tidy-notes", True, authorized=True)
        s = hub.new_session(str(ws))
        await run_turn(hub, s, "整理笔记")
        return fake.requests, hub.extensions.skill_index()

    reqs, index = hub_run(tmp_path, script, scenario)
    system = reqs[0]["body"]["messages"][0]["content"]
    assert "你会的技能" in system and "creator:tidy-notes：把乱的笔记按日期整理成目录" in system
    assert next(r for r in index if r["id"] == "creator:tidy-notes")["used"] == 1
    assert reqs[0]["body"]["messages"][0] == reqs[1]["body"]["messages"][0]  # 用过以后系统提示不变 (缓存)


def test_skill_nudge_after_complex_run(tmp_path):
    script = [call(("list_dir", json.dumps({"path": "."}))), call(("glob", json.dumps({"pattern": "*"}))), say("做完了"), say("不用存，结果是 x")]

    async def scenario(hub, fake, ws):
        s = hub.new_session(str(ws))
        events = await run_turn(hub, s, "看看")
        return s, events, fake.requests

    s, events, reqs = hub_run(tmp_path, script, scenario, "agent.skill_nudge_calls=2", "agent.skill_nudge_long=2")
    assert s.run["skill_nudged"] and "复盘提示" in reqs[3]["body"]["messages"][-1]["content"]
    assert any(e["type"] == "note" and "复盘" in e["text"] for e in events)
    assert s.run["summary"] == "不用存，结果是 x" and len(reqs) == 4


def test_no_nudge_for_simple_runs_or_when_off(tmp_path):
    async def scenario(hub, fake, ws):
        s = hub.new_session(str(ws))
        await run_turn(hub, s, "看看")
        return s, fake.requests

    s, reqs = hub_run(tmp_path, [call(("list_dir", "{}")), say("好")], scenario)
    assert not s.run.get("skill_nudged") and len(reqs) == 2
    (tmp_path / "off").mkdir()
    s, reqs = hub_run(tmp_path / "off", [call(("list_dir", "{}")), say("好"), say("x")], scenario,
                      "agent.skill_nudge=\"off\"", "agent.skill_nudge_calls=1", "agent.skill_nudge_long=1")
    assert not s.run.get("skill_nudged") and len(reqs) == 2


# ---------------------------------------------------------------------------
# P1-6 钩子
# ---------------------------------------------------------------------------
def hooks_json(home, hooks):
    home.mkdir(parents=True, exist_ok=True)
    (home / "hooks.json").write_text(json.dumps({"hooks": hooks}, ensure_ascii=False), encoding="utf-8")


def py(code, **extra):
    return {"type": "command", "command": sys.executable, "args": ["-c", code], **extra}


BLOCK_SECRET = ("import sys, json; d = json.load(sys.stdin)\n"
                "if 'secret' in json.dumps(d['tool_input']):\n    sys.stderr.write('不许碰 secret'); sys.exit(2)")
REWRITE = ("import sys, json; d = json.load(sys.stdin); i = d['tool_input']\n"
           "if d['tool_name'] == 'write_file': i['content'] = i['content'].upper()\n"
           "print(json.dumps({'updated_input': i}))")
CONTEXT = "import sys, json; json.load(sys.stdin); print(json.dumps({'additional_context': '记得跑测试'}))"


def test_pre_and_post_tool_hooks(tmp_path):
    script = [call(("write_file", json.dumps({"path": "secret.txt", "content": "x"}))),
              call(("write_file", json.dumps({"path": "ok.txt", "content": "hello\n"}))), say("好了")]

    async def scenario(hub, fake, ws):
        hooks_json(hub.home, {"PreToolUse": [{"matcher": "write_file", "hooks": [py(BLOCK_SECRET), py(REWRITE)]}],
                              "PostToolUse": [{"matcher": "write_file", "hooks": [py(CONTEXT)]}]})
        s = hub.new_session(str(ws))
        events = await run_turn(hub, s, "写两个文件")
        return s, events, fake.requests, ws

    s, events, reqs, ws = hub_run(tmp_path, script, scenario)
    results = [e for e in events if e["type"] == "tool_result"]
    assert not results[0]["ok"] and "钩子拦下了：不许碰 secret" in results[0]["text"] and not (ws / "secret.txt").exists()
    assert (ws / "ok.txt").read_text("utf-8") == "HELLO\n"  # PreToolUse 改了参数 (照样过守门)
    assert "[钩子补充" in tool_msgs(reqs[2])[-1]["content"] and "记得跑测试" in tool_msgs(reqs[2])[-1]["content"]
    assert s.run["hooks"]["PreToolUse"] == 4


def test_stop_hook_makes_her_continue_once(tmp_path):
    stop = ("import sys, json; d = json.load(sys.stdin)\n"
            "print(json.dumps({'decision': 'block', 'reason': '先跑一遍测试再说做完'} if not d['stop_hook_active'] else {}))")

    async def scenario(hub, fake, ws):
        hooks_json(hub.home, {"Stop": [{"hooks": [py(stop)]}]})
        s = hub.new_session(str(ws))
        events = await run_turn(hub, s, "做完它")
        return s, events, fake.requests

    s, events, reqs = hub_run(tmp_path, [say("做完了"), say("测试也跑了，做完了")], scenario)
    assert len(reqs) == 2 and "先跑一遍测试再说做完" in reqs[1]["body"]["messages"][-1]["content"]
    assert s.run["stop_hooks"] == 1 and s.run["summary"] == "测试也跑了，做完了"


def test_user_prompt_hook_blocks_or_adds_background(tmp_path):
    gate = ("import sys, json; d = json.load(sys.stdin)\n"
            "if '删库' in d['prompt']:\n    sys.stderr.write('这种话不做'); sys.exit(2)\n"
            "print('今天是周五')")

    async def scenario(hub, fake, ws):
        hooks_json(hub.home, {"UserPromptSubmit": [{"hooks": [py(gate)]}]})
        s = hub.new_session(str(ws))
        why = await hub.send(s.id, "帮我删库")
        await run_turn(hub, s, "今天干嘛")
        return why, fake.requests

    why, reqs = hub_run(tmp_path, [say("好")], scenario)
    assert why == "钩子拦下了这句话：这种话不做"
    first_user = next(m for m in reqs[0]["body"]["messages"] if m["role"] == "user")
    assert first_user["content"].startswith("今天干嘛") and "今天是周五" in first_user["content"]


def test_broken_or_slow_hooks_do_not_block(tmp_path):
    slow = "import time; time.sleep(5)"

    async def scenario(hub, fake, ws):
        hooks_json(hub.home, {"PreToolUse": [{"hooks": [py(slow, timeout=1), py("import sys; sys.exit(3)")]}]})
        s = hub.new_session(str(ws))
        events = await run_turn(hub, s, "看看")
        return events

    events = hub_run(tmp_path, [call(("list_dir", "{}")), say("好")], scenario)
    notes = [e["text"] for e in events if e["type"] == "note"]
    assert any("超过 1 秒" in t for t in notes) and any("退出码 3" in t for t in notes)
    assert next(e for e in events if e["type"] == "tool_result")["ok"]


def test_hooks_file_is_protected_from_her(tmp_path):
    script = [call(("write_file", json.dumps({"path": "PLACEHOLDER", "content": "{}"}))), say("好")]

    async def scenario(hub, fake, ws):
        hooks_json(hub.home, {})
        target = str(hub.home / "hooks.json")
        fake.script[0] = call(("write_file", json.dumps({"path": target, "content": "{\"hooks\": {}}"})))
        s = hub.new_session(str(ws), mode="unrestricted")
        events = await run_turn(hub, s, "改钩子")
        return events

    events = hub_run(tmp_path, script, scenario)
    result = next(e for e in events if e["type"] == "tool_result")
    assert not result["ok"] and "黑名单" in result["text"]


# ---------------------------------------------------------------------------
# P1-9 MCP 长连接
# ---------------------------------------------------------------------------
COUNTER = '''import json, os, sys
n = 0
for line in sys.stdin:
    q = json.loads(line)
    if 'id' not in q:
        continue
    m = q.get('method')
    if m == 'initialize':
        r = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'counter', 'version': '1'}}
    elif m == 'tools/list':
        r = {'tools': [{'name': 'count', 'description': 'count', 'inputSchema': {'type': 'object'}}]}
    elif m == 'tools/call':
        n += 1
        r = {'content': [{'type': 'text', 'text': f'{n} {os.getpid()}'}]}
    else:
        r = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': q['id'], 'result': r}), flush=True)
'''


def test_mcp_connections_persist_per_session_and_reconnect(tmp_path):
    folder = tmp_path / "srv"
    folder.mkdir()
    (folder / "server.py").write_text(COUNTER, encoding="utf-8")
    reg = ExtensionRegistry(tmp_path / "agent", settings_path=False, skill_roots=[],
                            servers=[{"id": "counter", "command": sys.executable, "args": ["server.py"], "cwd": str(folder)}])

    async def main():
        texts = lambda out: out["content"][0]["text"].split()  # noqa: E731
        a1 = texts(await reg.call("counter", "count", {}, owner="s1"))
        a2 = texts(await reg.call("counter", "count", {}, owner="s1"))
        b1 = texts(await reg.call("counter", "count", {}, owner="s2"))  # 别的会话: 另一条连接
        once = texts(await reg.call("counter", "count", {}))            # 不给 owner: 照旧一次一连
        entry = next(v for k, v in reg._pool.items() if k[0] == "s1")
        entry["client"].proc.kill()                                     # 进程没了: 下次自动重连
        await entry["client"].proc.wait()
        a3 = texts(await reg.call("counter", "count", {}, owner="s1"))
        await reg.close()
        return a1, a2, b1, once, a3, len(reg._pool)

    a1, a2, b1, once, a3, left = asyncio.run(main())
    assert a1[0] == "1" and a2[0] == "2" and a1[1] == a2[1]   # 同一个进程、记得住上一步
    assert b1[0] == "1" and b1[1] != a1[1] and once[0] == "1"
    assert a3[0] == "1" and a3[1] != a1[1] and left == 0

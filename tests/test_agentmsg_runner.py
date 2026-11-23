"""Agent 互发消息的运行器 (wm/agentmsg/runner.py): 命令模板、环境白名单、起进程、超时杀整棵、解析、不重发. 起的是假命令行替身
(tests/agentmsg_fake_cli.py), 没有真的 claude / codex、没有网络、不花钱. 计划: backups/tool4-agent-msg/计划.md 第 3.1 节."""
import json
import sys
import time
from pathlib import Path

import pytest

from wm.agentmsg import AgentMsgError, runner
from wm.agentmsg.limits import Ticket

FAKE = Path(__file__).resolve().parent / "agentmsg_fake_cli.py"


def ticket(target="claude"):
    return Ticket("k1", f"s|{target}", target, "", 0.5)


class Fake:
    def __init__(self, tmp_path, **ctl):
        self.ctl = tmp_path / "ctl.json"
        self.ctl.write_text(json.dumps(ctl, ensure_ascii=False), encoding="utf-8")
        self.argv0 = [sys.executable, "-X", "utf8", str(FAKE), "--ctl", str(self.ctl)]
        self.ws = tmp_path / "work space"
        self.ws.mkdir(exist_ok=True)

    def log(self):
        p = Path(str(self.ctl) + ".log")
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def run(self, target="claude", prompt="你好，帮我看一下", **kw):
        kw.setdefault("timeout_s", 60.0)
        return runner.run(ticket(target), target=target, prompt=prompt, workspace=str(self.ws), argv0=self.argv0, **kw)


# ---------------------------------------------------------------- 命令模板
def test_claude_template_is_read_only_and_loads_no_mcp():
    args = runner.build_args("claude", max_usd=0.0694)
    assert args[:3] == ["-p", "--output-format", "json"]
    assert "--strict-mcp-config" in args and "--mcp-config" not in args          # 评审必改 1: 一个 MCP 服务都不载 (尤其是 wm-pet)
    assert args[args.index("--permission-mode") + 1] == "plan"
    assert args[args.index("--tools") + 1] == "Read,Grep,Glob"
    assert args[args.index("--setting-sources") + 1] == "user"                   # 不载项目里的设置 (钩子)
    assert args[args.index("--max-budget-usd") + 1] == "0.0694"
    assert not any("dangerously" in a or a in ("--allowedTools", "--allowed-tools") for a in args)
    assert "-r" not in args
    assert runner.build_args("claude", "11111111-2222-3333-4444-555555555555")[-2:] == ["-r", "11111111-2222-3333-4444-555555555555"]


def test_codex_template_is_read_only_ignores_user_config_in_both_forms():
    fresh = runner.build_args("codex")
    resume = runner.build_args("codex", "019aaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert fresh[0] == "exec" and resume[:3] == ["exec", "resume", "019aaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
    for args in (fresh, resume):
        assert "--ignore-user-config" in args                                    # 评审必改 1: 不载用户 config.toml 里的 MCP 服务和设置
        assert 'sandbox_mode="read-only"' in args and 'approval_policy="never"' in args
        assert "-s" not in args and "-C" not in args and "--mcp-config" not in args    # resume 不接受 -s / -C
        assert args[-1] == "-" and "--json" in args                              # 提示走标准输入
        assert not any("dangerously" in a for a in args)


@pytest.mark.parametrize("bad", ["a;b", "x y z w 12", "abc", "../../etc/passwd", "ok&calc.exe", "1" * 65, "中文的会话号啊啊啊"])
def test_session_id_is_checked_before_it_goes_into_the_command_line(bad):
    for target in ("claude", "codex"):
        with pytest.raises(AgentMsgError):
            runner.build_args(target, bad)
    with pytest.raises(AgentMsgError):
        runner.build_args("someone-else")


# ---------------------------------------------------------------- 找程序
def test_find_program_prefers_test_launch_then_config_then_shim(tmp_path, monkeypatch):
    assert runner.find_program("claude", launch={"claude": ["x", "y"]}) == ["x", "y"]
    assert runner.find_program("codex", {"codex": {"argv0": ["C:/a/node.exe", "C:/a/codex.js"]}}) == ["C:/a/node.exe", "C:/a/codex.js"]
    shim = tmp_path / "npm"
    entry = shim / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("//", encoding="utf-8")
    (shim / "claude.cmd").write_text("@echo off", encoding="utf-8")
    (shim / "node.exe").write_bytes(b"MZ")
    monkeypatch.setattr(runner.shutil, "which", lambda name: str(shim / "claude.cmd") if name == "claude" else None)
    got = runner.find_program("claude")
    assert got == [str(shim / "node.exe"), str(entry)]                           # 不走 cmd.exe / .cmd 包装
    assert not any(x.lower().endswith((".cmd", ".bat")) or "cmd.exe" in x.lower() for x in got)
    entry.unlink()
    with pytest.raises(AgentMsgError) as e:
        runner.find_program("claude")
    assert e.value.code == "not_found"
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    with pytest.raises(AgentMsgError) as e:
        runner.find_program("codex")
    assert e.value.code == "not_found"
    monkeypatch.setattr(runner.shutil, "which", lambda name: "C:/tools/claude.exe")
    assert [Path(x) for x in runner.find_program("claude")] == [Path("C:/tools/claude.exe")]


def test_environment_is_a_whitelist_without_any_key(monkeypatch):
    for k, v in {"DEEPSEEK_API_KEY": "sk-secret-1", "OPENAI_API_KEY": "sk-secret-2", "WM_TOKEN": "t", "HTTPS_PROXY": "http://127.0.0.1:1082",
                 "PATH": "C:/x", "SystemRoot": "C:/Windows", "ANTHROPIC_API_KEY": "sk-secret-3"}.items():
        monkeypatch.setenv(k, v)
    env = runner.make_env()
    low = {k.upper() for k in env}
    assert {"PATH", "HTTPS_PROXY", "SYSTEMROOT"} <= low
    assert not any("KEY" in k or "TOKEN" in k or "SECRET" in k for k in low)


# ---------------------------------------------------------------- 起进程 (假命令行)
def test_run_claude_sends_the_prompt_on_stdin_in_the_workspace_with_the_fixed_template(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-not-travel")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1082")
    fake = Fake(tmp_path, reply="结论：bar 在第 3 行返回 None。", cost_usd=0.01, session="11111111-aaaa-bbbb-cccc-222222222222")
    res = fake.run(prompt="你好，帮我看一下 foo.py 里的 bar（含中文和 & % 符号）", max_usd=0.0694)
    assert (res.state, res.text, res.session_id) == ("replied", "结论：bar 在第 3 行返回 None。", "11111111-aaaa-bbbb-cccc-222222222222")
    assert res.cny == pytest.approx(0.072) and res.delivered
    (call,) = fake.log()
    assert call["args"] == runner.build_args("claude", max_usd=0.0694)           # 起的就是模板, 一个字不多
    assert call["stdin"] == "你好，帮我看一下 foo.py 里的 bar（含中文和 & % 符号）"       # 提示走标准输入, 中文原样
    assert Path(call["cwd"]).resolve() == fake.ws.resolve()
    names = {k.upper() for k in call["env"]}
    assert "DEEPSEEK_API_KEY" not in names and "HTTPS_PROXY" in names and "PATH" in names
    assert "sk-should-not-travel" not in json.dumps(call["env_values"])


def test_run_codex_reads_jsonl_and_prices_by_table_or_flat(tmp_path):
    fake = Fake(tmp_path, reply="可以。", session="019aaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                tokens={"input_tokens": 100000, "cached_input_tokens": 40000, "output_tokens": 10000})
    flat = fake.run("codex", codex_flat_cny=0.3)
    assert (flat.state, flat.text, flat.session_id, flat.cny) == ("replied", "可以。", "019aaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", 0.3)
    assert flat.tokens == {"input": 100000, "cached": 40000, "output": 10000}
    priced = fake.run("codex", codex_price={"input": 10.0, "cached": 2.5, "output": 40.0})
    assert priced.cny == pytest.approx((60000 * 10 + 40000 * 2.5 + 10000 * 40) / 1e6)
    again = fake.run("codex", resume="019aaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert fake.log()[-1]["args"][:3] == ["exec", "resume", "019aaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]
    assert again.state == "replied"


def test_the_other_side_reporting_an_error_is_a_failure_not_a_reply(tmp_path):
    claude = Fake(tmp_path, mode="fail", reply="超过预算了", cost_usd=0.07).run()
    assert claude.state == "failed" and "超过预算" in claude.error and claude.cny == pytest.approx(0.504)
    (tmp_path / "x").mkdir()
    codex = Fake(tmp_path / "x", mode="fail", reply="额度用完").run("codex")
    assert codex.state == "failed" and "额度用完" in codex.error


def test_garbage_and_crash_are_uncertain_and_never_retried(tmp_path):
    fake = Fake(tmp_path, mode="garbage")
    res = fake.run()
    assert res.state == "uncertain" and res.delivered and res.cny is None        # 钱按预占全额算 (调用的人看到 None)
    assert len(fake.log()) == 1
    (tmp_path / "b").mkdir()
    fake2 = Fake(tmp_path / "b", mode="crash")
    res2 = fake2.run()
    assert res2.state == "uncertain" and "退出码 3" in res2.error and len(fake2.log()) == 1


def test_missing_program_is_a_clean_failure_with_no_cost(tmp_path):
    res = runner.run(ticket(), target="claude", prompt="x", workspace=str(tmp_path), argv0=[str(tmp_path / "no-such-program.exe")])
    assert res.state == "failed" and res.cny == 0.0 and not res.delivered


def test_oversized_output_is_cut_off_and_uncertain(tmp_path):
    res = Fake(tmp_path, mode="big").run()
    assert res.state == "uncertain" and "太大" in res.error


def test_timeout_kills_the_whole_tree(tmp_path):
    import psutil
    fake = Fake(tmp_path, mode="grandchild", sleep=60)
    t0 = time.monotonic()
    res = fake.run(timeout_s=2.0)
    assert res.state == "timeout" and res.cny is None and time.monotonic() - t0 < 30
    pid = int(Path(str(fake.ctl) + ".pid").read_text())
    for _ in range(100):
        if not psutil.pid_exists(pid):
            break
        time.sleep(0.1)
    assert not psutil.pid_exists(pid)                                           # 孙进程也没了


def test_no_ticket_no_process(tmp_path):
    fake = Fake(tmp_path)
    for bad in (None, "k1", Ticket("k", "s|codex", "codex", "", 0.5)):          # 没有凭条 / 假凭条 / 对不上对方的凭条
        with pytest.raises(TypeError):
            runner.run(bad, target="claude", prompt="x", workspace=str(fake.ws), argv0=fake.argv0)
    assert fake.log() == []


# ---------------------------------------------------------------- 解析
def test_parsers_tolerate_missing_fields():
    assert runner.parse_claude("不是 JSON", 7.2).state == "uncertain"
    assert runner.parse_claude(json.dumps({"type": "result", "is_error": False}), 7.2).state == "uncertain"      # 没有 result
    ok = runner.parse_claude("日志行\n" + json.dumps({"result": "好", "total_cost_usd": -1}), 7.2)               # 取最后一行 JSON; 负数花费不信
    assert ok.state == "replied" and ok.text == "好" and ok.cny is None
    assert runner.parse_codex("乱七八糟\n", None, 0.3).state == "uncertain"
    assert runner.parse_codex(json.dumps({"type": "turn.started"}), None, 0.3).state == "uncertain"

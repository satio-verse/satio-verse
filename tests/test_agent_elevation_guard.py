"""插件宿主第 0 步 (2/3): 以管理员 (或别的用户) 身份运行的命令, 完全授权档也不放行.

完全授权 (unrestricted) 档只认黑名单 (wm/agent/policy.py 的固定守门在那一档不执行), 以前黑名单里没有"以管理员运行",
`Start-Process … -Verb RunAs` 判放行 (2026-11-11 核对). 现在 runas、runas.exe、runasuser、-verb、sudo、gsudo 是默认黑名单项;
命令类约束是逐字短语, 所以匹配前把 ^ ` 引号和 -EncodedCommand 的 Base64 还原一遍 (governance._command_views).
命令一条也不执行, 只是把字符串交给守门判; 全在临时目录."""
import base64

import pytest

from wm.agent import governance as G
from wm.agent import policy as P
from wm.agent.governance import BlacklistStore


def b64(text):
    """powershell -EncodedCommand 要的写法: UTF-16LE 再 Base64."""
    return base64.b64encode(text.encode("utf-16-le")).decode("ascii")


INNER = "Start-Process cmd -Verb RunAs"
ELEVATED = [
    # runas / runas.exe
    "runas /user:Administrator cmd", "RUNAS /USER:Administrator CMD", "runas.exe /savecred /user:Administrator cmd",
    r"C:\Windows\System32\runas.exe /user:Administrator cmd", "RunAs.EXE /user:Administrator cmd",
    "cmd /c runas /user:Administrator cmd",
    # Start-Process -Verb RunAs: 大小写、引号、冒号、空白、换行、别名、换用户
    "Start-Process cmd -Verb RunAs", "start-process cmd -verb runas", "START-PROCESS CMD -VERB RUNAS",
    "Start-Process cmd -Verb:RunAs", "Start-Process cmd -Verb 'RunAs'", 'Start-Process cmd -Verb "RunAs"',
    "Start-Process cmd   -Verb   RunAs", "Start-Process cmd -Verb\tRunAs", "Start-Process cmd\n-Verb\nRunAs",
    "Start-Process -FilePath cmd.exe -ArgumentList '/c whoami' -Verb RunAs -Wait", "saps cmd -Verb RunAs",
    "start cmd -verb runas", "Start-Process cmd -Verb RunAsUser",
    # 值是拼出来的: 整个 -Verb 参数拦
    "Start-Process cmd -Verb $verb", "Start-Process cmd -Verb ('Run' + 'As')",
    # 套一层 cmd /c、powershell -Command (引号里再套引号)
    'cmd /c "powershell -Command Start-Process cmd -Verb RunAs"',
    'cmd.exe /d /s /c "powershell.exe -NoProfile -Command ""Start-Process cmd -Verb RunAs"""',
    'powershell -Command "Start-Process cmd -Verb RunAs"', "powershell -c 'Start-Process cmd -Verb RunAs'",
    "pwsh -NoProfile -ExecutionPolicy Bypass -Command Start-Process cmd -Verb RunAs",
    'powershell -Command "& { Start-Process -FilePath cmd.exe -Verb RunAs }"',
    'cmd /c start /b powershell -c "saps cmd -verb runas"',
    'powershell -Command "cmd /c \\"powershell -c Start-Process cmd -Verb RunAs\\""',
    # 夹转义字符、引号
    "r^unas /user:Administrator cmd", "Start-Process cmd -Verb Ru`nAs", 'Start-Process cmd -Verb Ru"n"As',
    "Start-Process cmd -Verb 'Ru''nAs'",
    # powershell -EncodedCommand: 缩写、套两层
    f"powershell -EncodedCommand {b64(INNER)}", f"powershell -enc {b64(INNER)}", f"powershell -e {b64(INNER)}",
    f"pwsh -ec {b64(INNER)}", f"powershell -enc {b64('cmd /c powershell -enc ' + b64(INNER))}",
    # sudo
    "sudo whoami", "gsudo whoami", "sudo.exe whoami", "gsudo.exe cmd",
    # 不走 Start-Process 的写法
    "(New-Object -ComObject Shell.Application).ShellExecute('cmd.exe','','','runas',1)",
    "$p = New-Object System.Diagnostics.ProcessStartInfo 'cmd'; $p.Verb = 'runas'; [Diagnostics.Process]::Start($p)",
]
SAFE = [
    "Get-ChildItem -Force", "python build.py", "Get-Verb", "Get-Process | Format-Table Name", "git log --verbose",
    "dotnet build --verbosity quiet", "echo hello", "powershell -Command Get-Date", "cmd /c dir", "Start-Process notepad",
    "Start-Process notepad.exe -ArgumentList a.txt -Wait", "python runas_helper.py", "Get-Content sudoers.txt",
    "echo pseudo-code", f"powershell -EncodedCommand {b64('Get-Date')}", "certutil -hashfile a.bin SHA256 " + "ab12" * 16,
]
# 固定守门 (工作区内修改、自动执行两档) 见到字面的 runas / sudo 就拦; 拼出来、夹转义字符、编码过的靠黑名单 (所有档都先过它)
FIXED_GATE = [
    "Start-Process cmd -Verb RunAs", "Start-Process cmd -Verb:RunAs", "Start-Process cmd -Verb 'RunAs'", "Start-Process cmd -Verb RunAsUser",
    "runas.exe /user:x cmd", "RunAs.EXE /user:x cmd", "gsudo whoami", "gsudo.exe cmd", "sudo.exe whoami",
    'cmd /c "powershell -Command Start-Process cmd -Verb RunAs"', "(New-Object -ComObject Shell.Application).ShellExecute('cmd.exe','','','runas',1)",
]


@pytest.fixture
def store(tmp_path):
    return BlacklistStore(tmp_path / "agent")


@pytest.mark.parametrize("command", ELEVATED)
def test_default_blacklist_denies_every_way_to_elevate(store, tmp_path, command):
    for tool in ("run_command", "mcp.windows-mcp.PowerShell"):  # 自带的命令工具, 和别的 MCP 里叫 command 的参数
        d = store.check(tool, {"command": command, "workdir": str(tmp_path)}, tmp_path)
        assert d is not None and d.how == "deny" and "命中黑名单" in d.why, (tool, command)


@pytest.mark.parametrize("command", SAFE)
def test_ordinary_commands_are_not_caught(store, tmp_path, command):
    assert store.check("run_command", {"command": command, "workdir": str(tmp_path)}, tmp_path) is None


def test_full_authorization_has_no_fixed_gate_so_the_blacklist_is_the_only_wall():
    """前提: 完全授权档下固定守门不执行, 提权命令在这里判放行; 拦它的是黑名单 (上面两条测试)."""
    assert P.check_command(INNER, "unrestricted").how == "auto"
    assert P.check_command("sudo whoami", "unrestricted").how == "auto"


@pytest.mark.parametrize("command", FIXED_GATE)
@pytest.mark.parametrize("mode", ["workspace", "full"])
def test_fixed_gate_catches_literal_forms_too(command, mode):
    d = P.check_command(command, mode)
    assert d.how == "deny" and "管理员" in d.why


@pytest.mark.parametrize("command", ["Get-Verb", "python runas_helper.py", "Get-Content sudoers.txt", "echo pseudo-code",
                                     "git log --verbose"])
def test_fixed_gate_leaves_lookalikes_alone(command):
    assert P.check_command(command, "full").how == "auto"


def test_the_assistant_cannot_lift_the_rules_or_edit_the_blacklist_file(store, tmp_path):
    rule = next(r for r in store.list() if r["value"] == "runas")
    with pytest.raises(ValueError, match="只有用户"):
        store.remove(rule["id"], by="assistant")
    with pytest.raises(ValueError, match="只有用户"):
        store.update(rule["id"], {"enabled": False}, by="assistant")
    assert store.check("write_file", {"path": str(store.path)}, tmp_path).how == "deny"
    assert store.check("run_command", {"command": f"Set-Content '{store.path}' '{{}}'"}, tmp_path).how == "deny"


def test_the_user_can_lift_a_rule_that_gets_in_the_way(store, tmp_path):
    """-verb 整个参数都拦, 会误伤 Get-Command -Verb Get 这样的查询; 用户觉得碍事可以自己删, runas 那几条不受影响."""
    probe = {"command": "Get-Command -Verb Get"}
    assert store.check("run_command", probe, tmp_path).how == "deny"
    store.remove(next(r["id"] for r in store.list() if r["value"] == "-verb"), by="user")
    assert store.check("run_command", probe, tmp_path) is None
    assert store.check("run_command", {"command": INNER}, tmp_path).how == "deny"


def test_user_command_rules_see_through_the_same_tricks(store, tmp_path):
    """还原是所有命令类约束共用的: 用户自己写的 git push 约束, 也不会被 g^it push 或 -EncodedCommand 躲过去."""
    store.add({"kind": "command", "value": "git push"})
    for command in ("g^it push origin main", 'git "push" origin', f"powershell -enc {b64('git push origin main')}"):
        assert store.check("run_command", {"command": command}, tmp_path).how == "deny", command
    assert store.check("run_command", {"command": "git pull"}, tmp_path) is None


def test_views_are_bounded_and_ignore_garbage():
    assert G._command_views("Echo  A") == ["echo a"]
    assert G._command_views("r^un\"as\"") == ["r^un\"as\"", "runas"]
    deep = "x"
    for _ in range(9):  # 一层套一层的 Base64 (每套一层长 2.7 倍, 九层约 27 万字): 解的层数有上限, 不会无限展开
        deep = f"powershell -enc {b64(deep)}"
    assert len(G._command_views(deep)) <= 2 * G.MAX_COMMAND_VIEWS  # 解的层数有上限
    assert len(G._command_views("echo " + "A" * 100_000)) == 1  # 解出来全是空字符的不要 (也不慢)
    assert G._command_views("echo " + "A" * 17) == ["echo " + "a" * 17]  # 长度不对的 Base64 跳过, 不报错
    assert G._command_views("echo " + "////" * 8) == ["echo " + "////" * 8]  # 能解但解出来不是文字的不要
    assert G._command_views("abc!def" * 4) == ["abc!def" * 4]  # 没有够长的 Base64 候选

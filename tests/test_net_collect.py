"""网络插件第 0 步 (2/4): 只读采集 (wm/net/collect.py).

采集脚本 SCRIPT 是写死在模块里的一段 PowerShell, 这里不起 PowerShell, 只做两件事:
- 静态核对脚本: 里面每个 "动词-名词" 命令都在允许清单里, 没有任何会写东西的命令、没有 -EncodedCommand 之类;
- 把起进程那一步换成假的, 喂几份 PowerShell 输出样本 (中文网卡名、单元素数组、某一项报错、超时、输出不是 JSON ...),
  断言解析结果和 Snapshot.errors. 注册表也换成假的 winreg (没有任何写函数, 写一下就会报错).
每条测试一开始就把 subprocess.run / Popen 换成"一碰就失败", 所以没有哪条会真的起进程."""
from __future__ import annotations

import ast
import copy
import re
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest
from net_fakes import (NOISE_WORDS, Box, ps_output, ps_rows, sample_1111, sample_clean, sample_dead_proxy, sample_leftover_up,
                       sample_noise_adapters, sample_other_tun, sample_tailscale, sample_v2rayn, sample_verge_ok)

from wm.net import collect as C
from wm.net.checkup import analyze
from wm.net.snapshot import EnvProxy, Listener, Process, Snapshot, SystemProxy
from wm.net.trim import proxy_ports, trim

NET_DIR = Path(C.__file__).resolve().parent
MY_FILES = ["__init__.py", "snapshot.py", "collect.py", "trim.py", "checkup.py", "socks_probe.py", "__main__.py"]  # 另一个对话的 recipe.py、config_gen.py 不在内


@pytest.fixture(autouse=True)
def _no_real_process(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("测试不该真的起进程")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def _strip_strings(text: str) -> str:
    return re.sub(r"'[^']*'", "''", text)


# ---- 静态核对脚本 -----------------------------------------------------------------------------------------------

def test_the_script_only_uses_the_allowed_commands():
    used = set(re.findall(r"\b[A-Z][A-Za-z]*-[A-Z][A-Za-z]+\b", _strip_strings(C.SCRIPT)))
    assert used <= C.ALLOWED_CMDLETS, used - C.ALLOWED_CMDLETS
    # 该查的都查了
    assert {"Get-CimInstance", "Get-NetTCPConnection", "Get-NetAdapter", "Get-NetIPAddress", "Get-NetIPInterface", "Get-NetRoute",
            "ConvertTo-Json", "ForEach-Object"} <= used
    assert C.ALLOWED_CMDLETS == {
        "Get-CimInstance", "Get-NetTCPConnection", "Get-NetAdapter", "Get-NetIPAddress", "Get-NetIPInterface", "Get-NetRoute",
        "Select-Object", "Where-Object", "ForEach-Object", "Sort-Object", "ConvertTo-Json"}
    # Get-CimInstance 只查进程和服务
    classes = re.findall(r"Get-CimInstance\s+(?:-ClassName\s+)?(\w+)", C.SCRIPT)
    assert sorted(classes) == ["Win32_Process", "Win32_Service"] and set(classes) <= C.ALLOWED_CIM_CLASSES


@pytest.mark.parametrize("verb", ["Set-", "New-", "Remove-", "Enable-", "Disable-", "Start-", "Stop-", "Restart-", "Invoke-", "Add-", "Clear-"])
def test_the_script_has_no_command_that_changes_anything(verb):
    assert verb not in C.SCRIPT


def test_the_script_has_none_of_the_forbidden_programs_or_spellings():
    low = C.SCRIPT.lower()
    for word in ("netsh", "ipconfig", "pnputil", "-encodedcommand", "-file", "invoke-expression", "iex ", "start-process"):
        assert word not in low, word
    for pattern in (r"\broute\s", r"\breg\s", r"\bsc\s", r"\bnet\s", r"\bcmd\b"):
        assert not re.search(pattern, C.SCRIPT, re.IGNORECASE), pattern


def test_the_script_is_plain_ascii_without_double_quotes_so_nothing_needs_escaping():
    assert C.SCRIPT.isascii() and '"' not in C.SCRIPT and "`" not in C.SCRIPT and "%" not in C.SCRIPT
    assert C.SCRIPT.startswith("$ErrorActionPreference = 'Stop'\n")
    assert "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8" in C.SCRIPT.split("\n")[1]  # 开头就设成 UTF-8


def test_every_query_fails_loudly_except_the_persistent_routes_which_may_be_empty():
    lines = [line for line in C.SCRIPT.split("\n") if line.startswith("Emit '")]
    assert [re.match(r"Emit '(\w+)'", line).group(1) for line in lines] == list(C.PS_SECTIONS)
    for line in lines:
        silent = "-ErrorAction SilentlyContinue" in line
        assert silent == line.startswith("Emit 'routes_persistent'"), line
    assert "-PolicyStore PersistentStore" in C.SCRIPT and "-IncludeHidden" in C.SCRIPT and "-State Listen" in C.SCRIPT


def test_the_script_asks_for_no_command_lines_or_paths():
    low = C.SCRIPT.lower()
    for word in ("commandline", "executablepath", "-path", "$env:", "get-content", "get-childitem", "username", "get-process"):
        assert word not in low, word
    assert "-Property ProcessId,ParentProcessId,Name" in C.SCRIPT  # 进程只取这三样


def test_my_sources_never_mention_the_things_this_package_must_not_use():
    for name in MY_FILES:
        text = (NET_DIR / name).read_text(encoding="utf-8").lower()
        for word in ("psutil", "netsh", "runas", "shellexecute", "encodedcommand"):
            assert word not in text, (name, word)


def test_my_files_are_lf_utf8_without_bom_and_start_with_the_future_import():
    for name in MY_FILES:
        raw = (NET_DIR / name).read_bytes()
        assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf"), name
        body = ast.parse(raw.decode("utf-8")).body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]  # 模块说明
        if name != "__init__.py":  # __init__.py 只放模块说明
            assert isinstance(body[0], ast.ImportFrom) and body[0].module == "__future__" and body[0].names[0].name == "annotations", name
        else:
            assert body == []


# ---- 起进程那一步 -----------------------------------------------------------------------------------------------

def test_the_one_process_is_started_quietly_with_the_script_as_the_command_argument(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, ps_output(adapters=[]), b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = C._run_powershell(C.SCRIPT, 12.5)
    assert result.stdout == ps_output(adapters=[]) and result.error == "" and result.timed_out is False
    argv = seen["argv"]
    assert argv[0].lower().endswith("powershell.exe") and argv[-2:] == ["-Command", C.SCRIPT]
    assert {"-NoProfile", "-NonInteractive"} <= set(argv) and not {"-EncodedCommand", "-File", "-ExecutionPolicy"} & set(argv)
    kwargs = seen["kwargs"]
    assert kwargs["timeout"] == 12.5 and kwargs["stdin"] is subprocess.DEVNULL and kwargs["capture_output"] is True
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if sys.platform == "win32":
        assert kwargs["creationflags"] == 0x08000000  # 不弹黑窗


def test_a_timeout_keeps_the_part_that_was_already_printed(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"WMNET1|adapters|ok|[]\r\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = C._run_powershell(C.SCRIPT, 30)
    assert result.timed_out is True and result.stdout == b"WMNET1|adapters|ok|[]\r\n" and "超时（30 秒）" in result.error
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 1)))  # 一个字也没输出
    assert C._run_powershell(C.SCRIPT, 1).stdout == b""


def test_a_missing_powershell_and_an_abnormal_exit_are_reported(monkeypatch):
    def missing(argv, **kwargs):
        raise FileNotFoundError("powershell.exe")
    monkeypatch.setattr(subprocess, "run", missing)
    result = C._run_powershell(C.SCRIPT, 5)
    assert result.stdout == b"" and result.error.startswith("起不了 PowerShell") and not result.timed_out
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", "出错了 boom".encode("utf-8")))
    result = C._run_powershell(C.SCRIPT, 5)
    assert "退出码 1" in result.error and "boom" in result.error


# ---- 读脚本的输出 -----------------------------------------------------------------------------------------------

def test_parse_output_reads_marker_lines_and_ignores_everything_else():
    text = ("警告: 这不是输出\r\n"
            'WMNET1|a|ok|[{"n":1},{"n":2}]\r\n'
            "\r\n"
            'WMNET1|b|ok|{"n":3}\r\n'  # 单元素数组被写成了单个对象
            'WMNET1|c|err|"boom"\r\n'
            "WMNET1|d|ok|not json\r\n"
            "WMNET1|e|ok|5\r\n"
            "WMNET1|f|weird|[]\r\n"
            "WMNET1|only|three\r\n"
            'WMNET1|g|err|raw text that is not json\r\n'
            'WMNET1|h|ok|[{"name":"a|b|c"}]\r\n'  # 内容里有竖线
            'WMNET1|a|ok|[{"n":9}]\r\n')  # 同一项出现两次以后一次为准
    sections, errors = C.parse_output(text)
    assert sections == {"a": [{"n": 9}], "b": [{"n": 3}], "h": [{"name": "a|b|c"}]}
    assert errors == {"c": "PowerShell 报错：boom", "d": "PowerShell 的输出不是 JSON", "e": "PowerShell 的输出格式不对",
                      "f": "PowerShell 的输出格式不对", "g": "PowerShell 报错：raw text that is not json"}


def test_decoding_handles_bom_chinese_and_damage():
    assert C._decode("WMNET1|a|ok|[]".encode("utf-8-sig")) == "WMNET1|a|ok|[]"
    assert C._decode('以太网 本地连接* 9'.encode("utf-8")) == "以太网 本地连接* 9"
    assert "�" in C._decode(b"ok \xff\xfe broken") and C._decode(b"") == ""


# ---- collect(): 假的 PowerShell 输出 ----------------------------------------------------------------------------

@pytest.fixture
def fake_machine(monkeypatch):
    """把起进程和读注册表都换成假的, 返回 (记录起进程的调用的列表, 设置假输出的函数)."""
    calls = []
    state = {"result": C.RunResult(ps_output(**ps_rows()))}

    def fake_run(script, timeout_s):
        calls.append((script, timeout_s))
        return state["result"]
    monkeypatch.setattr(C, "_run_powershell", fake_run)
    monkeypatch.setattr(C, "read_system_proxy", lambda: SystemProxy(enabled=True, server="127.0.0.1:1082", pac=False))
    monkeypatch.setattr(C, "read_env_proxy", lambda: EnvProxy())

    def use(result):
        state["result"] = result
    return calls, use


def test_a_normal_run_with_chinese_adapter_names_bom_and_crlf(fake_machine):
    calls, use = fake_machine
    use(C.RunResult(ps_output(bom=True, crlf=True, noise=True, **ps_rows())))
    snap = C.collect()
    assert snap.errors == {}
    assert len(calls) == 1 and calls[0][0] == C.SCRIPT and 0 < calls[0][1] <= 30  # 只起一次进程, 跑的就是写死的那段脚本
    assert [a.name for a in snap.adapters] == ["WLAN", "以太网", "本地连接* 9", "cfw-tap", "Teredo Tunneling Pseudo-Interface"]
    assert snap.adapters[1].description == "Realtek Gaming 2.5GbE Family Controller" and snap.adapters[1].status == "Disconnected"
    assert snap.adapters[3].index == 4 and snap.adapters[4].status == "Not Present"
    # 来源三分; IPv6 地址后面的网卡序号去掉
    assert [(a.ip, a.prefix, a.origin) for a in snap.addresses] == [
        ("192.168.1.50", 24, "dhcp"), ("10.0.0.1", 24, "manual"), ("fe80::80f1:7357:83db:5073", 64, "system"), ("2001:db8::2", 64, "system")]
    assert [(i.family, i.metric, i.auto_metric, i.state) for i in snap.ip_interfaces] == [
        ("IPv4", 30, True, "Connected"), ("IPv4", 1, False, "Disconnected")]
    # 生效的和持久的路由分开标
    assert [(r.prefix, r.adapter, r.persistent) for r in snap.routes] == [
        ("0.0.0.0/0", "WLAN", False), ("0.0.0.0/0", "cfw-tap", False), ("0.0.0.0/0", "cfw-tap", True)]
    assert snap.routes[0].if_metric == 30 and snap.routes[1].next_hop == "10.0.0.0" and snap.routes[2].if_index == 4
    # 进程、服务、监听采到就筛 (wm/net/trim.py): 脚本采到的 chrome / game / node / svchost、Spooler、ADPSvc、System 的监听、node 的 3000 都不进快照
    assert [(x.address, x.port, x.pid) for x in snap.listeners] == [("0.0.0.0", 1082, 9660)]
    assert [(p.pid, p.ppid, p.name) for p in snap.processes] == [(7112, 1632, "nssm.exe"), (9660, 7112, "sing-box.exe")]  # sing-box 和托管它的 nssm
    assert [(s.name, s.state, s.pid) for s in snap.services] == [("SingBoxProxy", "Running", 7112)]  # nssm 托管的那个服务
    for word in ("chrome", "game.exe", "node.exe", "svchost", "Spooler", "ADPSvc", "5357", "3000"):
        assert word not in snap.to_json(), word
    assert snap.system_proxy == SystemProxy(True, "127.0.0.1:1082", False)
    assert datetime.fromisoformat(snap.collected_at).utcoffset().total_seconds() == 0  # UTC
    assert Snapshot.from_json(snap.to_json()) == snap


def test_a_one_element_array_that_powershell_turned_into_a_single_object(fake_machine):
    calls, use = fake_machine
    full = ps_rows()
    rows = {name: full[name][0] for name in ("adapters", "ip_interfaces", "addresses", "routes", "routes_persistent", "listeners")}
    rows["processes"] = full["processes"][1]  # sing-box 一个 (持久路由里也只有一条, 和 11-11 的真机一样)
    rows["services"] = {"name": "clash_verge_service", "state": "Running", "pid": 0}  # 名字是认得的客户端的服务, 筛选留下
    use(C.RunResult(ps_output(**rows)))
    snap = C.collect()
    assert snap.errors == {}
    assert len(snap.adapters) == 1 and len(snap.processes) == 1 and len(snap.services) == 1 and len(snap.listeners) == 1
    assert [r.persistent for r in snap.routes] == [False, True]


def test_an_empty_persistent_route_store_is_just_empty(fake_machine):
    calls, use = fake_machine
    rows = ps_rows()
    rows["routes_persistent"] = []
    use(C.RunResult(ps_output(**rows)))
    snap = C.collect()
    assert snap.errors == {} and [r.persistent for r in snap.routes] == [False, False]


def test_one_section_failing_only_loses_that_section(fake_machine):
    calls, use = fake_machine
    rows = ps_rows()
    rows["routes"] = ("err", "找不到任何对象。请验证属性值，然后重试。")
    rows["services"] = ("raw", "{broken json")
    use(C.RunResult(ps_output(**rows)))
    snap = C.collect()
    assert set(snap.errors) == {"routes", "services"}
    assert snap.errors["routes"] == "PowerShell 报错：找不到任何对象。请验证属性值，然后重试。"
    assert snap.errors["services"] == "PowerShell 的输出不是 JSON"
    assert [r.persistent for r in snap.routes] == [True]  # 生效的路由没采到, 持久的照常
    assert len(snap.adapters) == 5 and len(snap.processes) == 2 and snap.services == []
    assert snap.system_proxy.server == "127.0.0.1:1082"  # 注册表那两项不受影响


def test_a_timeout_keeps_what_finished_and_names_what_did_not(fake_machine):
    calls, use = fake_machine
    rows = ps_rows()
    partial = ps_output(adapters=rows["adapters"], ip_interfaces=rows["ip_interfaces"])
    use(C.RunResult(partial, "超时（30 秒）没采完", True))
    snap = C.collect(timeout_s=30)
    assert len(snap.adapters) == 5 and len(snap.ip_interfaces) == 2
    assert set(snap.errors) == {"addresses", "routes", "routes_persistent", "listeners", "processes", "services"}
    assert all("超时" in message for message in snap.errors.values())
    assert snap.addresses == [] and snap.routes == [] and snap.processes == []


def test_output_that_is_not_json_at_all(fake_machine):
    calls, use = fake_machine
    use(C.RunResult(b"Hello, this is not what the script prints\r\nStill nothing\r\n"))
    snap = C.collect()
    assert set(snap.errors) == set(C.PS_SECTIONS) and all(m == "PowerShell 没有给出这一项" for m in snap.errors.values())
    assert snap.adapters == [] and snap.system_proxy.enabled  # 注册表读到的还在


def test_powershell_that_could_not_start(fake_machine):
    calls, use = fake_machine
    use(C.RunResult(b"", "起不了 PowerShell（FileNotFoundError: powershell.exe）"))
    snap = C.collect()
    assert set(snap.errors) == set(C.PS_SECTIONS) and all(m.startswith("起不了 PowerShell") for m in snap.errors.values())


def test_an_abnormal_exit_code_does_not_matter_when_every_section_arrived(fake_machine):
    calls, use = fake_machine
    use(C.RunResult(ps_output(**ps_rows()), "PowerShell 异常退出（退出码 1）：xx"))
    assert C.collect().errors == {}


def test_the_total_time_limit_is_passed_on(fake_machine):
    calls, use = fake_machine
    C.collect(timeout_s=12)
    C.collect()
    assert 0 < calls[0][1] <= 12 and 0 < calls[1][1] <= 30


def test_a_registry_failure_is_recorded_and_the_rest_carries_on(fake_machine, monkeypatch):
    calls, use = fake_machine

    def broken():
        raise OSError("拒绝访问")
    monkeypatch.setattr(C, "read_system_proxy", broken)
    monkeypatch.setattr(C, "read_env_proxy", lambda: (_ for _ in ()).throw(ModuleNotFoundError("winreg")))
    snap = C.collect()
    assert set(snap.errors) == {"system_proxy", "env_proxy"}
    assert "OSError" in snap.errors["system_proxy"] and "ModuleNotFoundError" in snap.errors["env_proxy"]
    assert len(snap.adapters) == 5  # 采集脚本那边照常


# ---- 系统代理和环境变量 (注册表, 只读) ---------------------------------------------------------------------------

@pytest.mark.parametrize("values,expected", [
    ({"ProxyEnable": 1, "ProxyServer": "127.0.0.1:1082"}, SystemProxy(True, "127.0.0.1:1082", False)),
    ({"ProxyEnable": 0, "ProxyServer": "127.0.0.1:1082"}, SystemProxy(False, "127.0.0.1:1082", False)),
    ({}, SystemProxy(False, "", False)),
    ({"ProxyEnable": 1, "ProxyServer": "http=a:1;https=a:1;socks=a:2", "AutoConfigURL": "http://x/proxy.pac"},
     SystemProxy(True, "http=a:1;https=a:1;socks=a:2", True)),
    ({"ProxyEnable": 0, "AutoConfigURL": "  "}, SystemProxy(False, "", False)),
    ({"ProxyEnable": "oops"}, SystemProxy(False, "", False)),
    ({"ProxyEnable": 1, "ProxyServer": "http=u:p@a:1"}, SystemProxy(True, "http=a:1", False)),  # 账号密码去掉
])
def test_system_proxy_values(values, expected):
    assert C.system_proxy_from(values) == expected


def test_the_pac_address_itself_is_never_kept():
    got = C.system_proxy_from({"AutoConfigURL": "http://example.test/proxy.pac?token=abc123"})
    assert got.pac is True and "abc123" not in repr(got) and "example.test" not in repr(got)


def test_env_proxy_values():
    env = C.env_proxy_from({"HTTP_PROXY": "http://127.0.0.1:7890", "Https_Proxy": "127.0.0.1:7891", "no_proxy": "localhost,127.0.0.1",
                            "ALL_PROXY": "", "Path": "C:/x", "OPENAI_API_KEY": "sk-should-never-be-kept"})
    assert (env.http_proxy.present, env.http_proxy.host, env.http_proxy.port) == (True, "127.0.0.1", 7890)
    assert (env.https_proxy.present, env.https_proxy.host, env.https_proxy.port) == (True, "127.0.0.1", 7891)
    assert (env.all_proxy.present, env.all_proxy.host, env.all_proxy.port) == (False, "", None)  # 空字符串等于没设
    assert (env.no_proxy.present, env.no_proxy.host, env.no_proxy.port) == (True, "", None)  # no_proxy 只记有没有
    assert "sk-should" not in repr(env) and "localhost" not in repr(env)
    assert C.env_proxy_from({}) == EnvProxy()


def _fake_winreg(keys: dict, calls: list):
    """一个只有读函数的假 winreg: 缺了的 (任何写函数) 一用就 AttributeError. 值名不分大小写, 和真的一样."""
    class Key:
        def __init__(self, values):
            self.values = values

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(root, sub, reserved=0, access=0):
        calls.append(("open", root, sub, access))
        if sub not in keys:
            raise FileNotFoundError(sub)
        return Key(keys[sub])

    def query_value_ex(key, name):
        calls.append(("query", name))
        for k, v in key.values.items():
            if k.lower() == name.lower():
                return v, 1
        raise FileNotFoundError(name)
    return types.SimpleNamespace(HKEY_CURRENT_USER="HKCU", KEY_READ=0x20019, OpenKey=open_key, QueryValueEx=query_value_ex)


def test_the_registry_is_only_read_and_only_the_named_values_are_asked_for(monkeypatch):
    calls = []
    keys = {C.ENV_KEY: {"HTTP_PROXY": "http://alice:pw@127.0.0.1:7890/", "OPENAI_API_KEY": "sk-secret", "Path": "x"},
            C.INTERNET_SETTINGS: {"ProxyEnable": 1, "ProxyServer": "127.0.0.1:1082", "SomethingElse": "y"}}
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg(keys, calls))
    env = C.read_env_proxy()
    system = C.read_system_proxy()
    assert (env.http_proxy.host, env.http_proxy.port) == ("127.0.0.1", 7890) and "alice" not in repr(env) and "pw" not in repr(env)
    assert system == SystemProxy(True, "127.0.0.1:1082", False)
    opened = [c for c in calls if c[0] == "open"]
    assert opened == [("open", "HKCU", C.ENV_KEY, 0x20019), ("open", "HKCU", C.INTERNET_SETTINGS, 0x20019)]  # 只读 (KEY_READ)、只读当前用户那一份
    assert [c[1] for c in calls if c[0] == "query"] == ["http_proxy", "https_proxy", "all_proxy", "no_proxy",
                                                        "ProxyEnable", "ProxyServer", "AutoConfigURL"]  # 不枚举整个键, 别的值碰都不碰


def test_a_missing_registry_key_or_non_windows_system_is_an_error_not_a_crash(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", _fake_winreg({}, []))
    with pytest.raises(FileNotFoundError):
        C.read_env_proxy()
    monkeypatch.setitem(sys.modules, "winreg", None)  # import winreg 失败 (不是 Windows)
    with pytest.raises(ImportError):
        C.read_system_proxy()


@pytest.mark.skipif(sys.platform != "win32", reason="不是 Windows")
def test_the_real_registry_read_works_on_this_machine():
    system, env = C.read_system_proxy(), C.read_env_proxy()  # 只读; 只断言类型, 不看值
    assert isinstance(system, SystemProxy) and isinstance(system.enabled, bool) and isinstance(system.server, str)
    assert isinstance(env, EnvProxy) and all(isinstance(getattr(env, n).present, bool) for n in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"))


# ---- 采到就筛 (wm/net/trim.py) ----------------------------------------------------------------------------------

SAMPLES = [sample_1111, sample_other_tun, sample_clean, sample_verge_ok, sample_v2rayn, sample_leftover_up, sample_noise_adapters,
           sample_dead_proxy, sample_tailscale]


@pytest.mark.parametrize("make", SAMPLES)
def test_trimming_drops_every_unrelated_name_and_changes_nothing_the_analysis_says(make):
    snap = make()
    before = copy.deepcopy(snap)
    assert any(word in snap.to_json() for word in NOISE_WORDS)  # 样本里确实夹着无关的进程、服务、监听
    trimmed = trim(snap)
    assert snap == before and trimmed is not snap  # 传进来的不动, 返回新的一份
    text = trimmed.to_json().lower()
    for word in NOISE_WORDS:
        assert word.lower() not in text, word
    assert analyze(trimmed).to_json() == analyze(snap).to_json()  # 筛完分析出的报告和不筛一模一样
    assert analyze(trimmed).summary() == analyze(snap).summary()
    assert trim(trimmed) == trimmed  # 筛过的再筛不变
    for name in ("adapters", "addresses", "ip_interfaces", "routes"):  # 网卡、地址、接口、路由不筛
        assert getattr(trimmed, name) == getattr(snap, name), name
    assert (trimmed.system_proxy, trimmed.env_proxy, trimmed.errors, trimmed.collected_at) == (
        snap.system_proxy, snap.env_proxy, snap.errors, snap.collected_at)
    assert len(trimmed.processes) <= 12 and len(trimmed.services) <= 4 and len(trimmed.listeners) <= 8  # 只剩十来个


def test_trimming_the_snapshot_of_1111_keeps_exactly_what_the_report_needs():
    t = trim(sample_1111())
    assert sorted(p.name for p in t.processes) == ["HipsTray.exe", "clash-verge-service.exe", "nssm.exe", "services.exe", "sing-box.exe"]
    assert sorted(s.name for s in t.services) == ["SingBoxProxy", "clash_verge_service"]
    assert sorted(x.port for x in t.listeners) == [1082, 1083, 1084, 1085]  # 135、445 (System) 和 node 的都筛掉了


def test_listeners_are_kept_for_recognized_clients_and_for_the_ports_the_proxy_settings_name():
    b = Box()
    b.physical()
    sing = b.proc("sing-box.exe")
    other = b.proc("my-proxy.exe")  # 认不出的程序, 但系统代理指着它的端口
    envp = b.proc("env-proxy.exe")  # 环境变量代理指着它的端口
    quiet = b.proc("quiet.exe")  # 没人点名的端口
    b.listen(sing, 1082, 9090)  # 认得的客户端的监听: 全留
    b.listen(other, 8888)
    b.listen(envp, 7000, 7001)
    b.listen(quiet, 8889, 3000)
    b.system_proxy("127.0.0.1:8888")
    b.env(https_proxy=("127.0.0.1", 7000), no_proxy=True)
    t = trim(b.snap)
    assert sorted((x.port, x.pid) for x in t.listeners) == sorted([(1082, sing), (9090, sing), (8888, other), (7000, envp)])
    assert {p.name for p in t.processes} == {"sing-box.exe", "my-proxy.exe", "env-proxy.exe"}  # 属主跟着留 (分析要写"谁在听")
    b.system_proxy("127.0.0.1:8888", enabled=False)  # 关着的系统代理, 端口不算
    assert sorted(x.port for x in trim(b.snap).listeners) == [1082, 7000, 9090]
    assert proxy_ports(b.snap) == {7000}
    b.env(all_proxy=("::1", 7002), http_proxy=("localhost", None))
    assert proxy_ports(b.snap) == {7000, 7002}  # 没写端口的不算; no_proxy 没有端口
    assert proxy_ports(Snapshot()) == set()


def test_processes_kept_are_clients_security_software_listener_owners_and_two_generations_of_parents():
    b = Box()
    b.physical()
    great = b.proc("great.exe")
    grand = b.proc("grand.exe", ppid=great)
    parent = b.proc("parent.exe", ppid=grand)
    b.proc("sing-box.exe", ppid=parent)
    b.proc("HipsTray.exe")
    b.proc("360Tray.exe")
    b.proc("lone.exe")
    b.proc("lone-child.exe", ppid=great)
    assert {p.name for p in trim(b.snap).processes} == {"sing-box.exe", "parent.exe", "grand.exe", "HipsTray.exe", "360Tray.exe"}
    # 认不出的程序监听着系统代理的端口: 它和它的父进程、祖父进程都留
    b = Box()
    b.physical()
    great = b.proc("great.exe")
    grand = b.proc("grand.exe", ppid=great)
    parent = b.proc("parent.exe", ppid=grand)
    unknown = b.proc("my-proxy.exe", ppid=parent)
    b.listen(unknown, 8888)
    b.system_proxy("127.0.0.1:8888")
    assert {p.name for p in trim(b.snap).processes} == {"my-proxy.exe", "parent.exe", "grand.exe"}


def test_a_process_table_with_loops_and_orphans_does_not_hang_the_trim():
    snap = Snapshot(processes=[Process(pid=1, ppid=1, name="sing-box.exe"), Process(pid=2, ppid=3, name="xray.exe"),
                               Process(pid=3, ppid=2, name="a.exe"), Process(pid=4, ppid=999, name="HipsTray.exe"),
                               Process(pid=0, ppid=0, name="System Idle Process"), Process(pid=5, ppid=0, name="v2rayN.exe")],
                    listeners=[Listener(address="0.0.0.0", port=1, pid=77)])  # 属主不在进程表里
    t = trim(snap)
    assert sorted(p.pid for p in t.processes) == [0, 1, 2, 3, 4, 5]  # 5 的父进程是 pid 0, 留; 环里的两个互为父子
    assert t.listeners == [] and trim(Snapshot()) == Snapshot()


def test_services_kept_are_client_named_or_host_a_kept_process_except_the_shared_svchost():
    b = Box()
    b.physical()
    nssm = b.proc("nssm.exe")
    b.proc("sing-box.exe", ppid=nssm)
    b.service("Hosted by nssm", nssm)  # 托管 sing-box 的服务 (pid 是留下来的 nssm 的)
    scheduler = b.proc("svchost.exe")  # 界面是计划任务拉起来的: 父进程是共享的 svchost, 留着, 但它托管的服务不留
    b.proc("clash-verge.exe", ppid=scheduler)
    b.service("Schedule", scheduler)
    b.service("Spooler", scheduler)
    b.service("clash_verge_service", 0, "Stopped")  # 名字在客户端表里的: 不管在不在跑都留
    b.service("FlClashHelperService", 0)
    b.service("sing-box", 0)  # 服务名和客户端的进程名一样的也留
    b.service("Dnscache", b.proc("other-host.exe"))
    b.service("WSearch", 0, "Stopped")
    t = trim(b.snap)
    assert sorted(s.name for s in t.services) == ["FlClashHelperService", "Hosted by nssm", "clash_verge_service", "sing-box"]
    assert "svchost.exe" in {p.name for p in t.processes}  # 父进程留着
    assert analyze(t).to_json() == analyze(b.snap).to_json()


def test_collect_returns_the_trimmed_snapshot_and_trimming_keeps_the_errors(fake_machine):
    calls, use = fake_machine
    use(C.RunResult(ps_output(**ps_rows())))
    snap = C.collect()
    assert trim(snap) == snap and not any(word in snap.to_json() for word in ("chrome", "node.exe", "Spooler"))
    errors_snap = Snapshot(errors={"listeners": "超时"}, processes=[Process(pid=9, ppid=0, name="chrome.exe")])
    assert trim(errors_snap).errors == {"listeners": "超时"} and trim(errors_snap).processes == []

"""扫源码: 红线上的东西一样都不许出现 -- 真路径 (.worldmodel、vault)、联网下载、“管理员也照跑”的开关、降权再跑的办法、
改注册表、整份导出环境变量; 提权请求只许在探针里出现一次、只用 runas 这一个动词; 文件格式 (LF、UTF-8 无 BOM、.cmd 只有 ASCII)."""
from __future__ import annotations

import py_compile
import re

import pytest

from fakes import ISO

PROGRAMS = ["measure.py", "sandbox_win.py", "child_probe.py"]


def source(name: str) -> str:
    return (ISO / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", PROGRAMS)
def test_no_real_paths(name):
    text = source(name).replace("fake_vault.txt", "").lower()
    assert ".worldmodel" not in text
    assert "vault" not in text


@pytest.mark.parametrize("name", PROGRAMS)
def test_no_network_download_or_install(name):
    text = source(name).lower()
    for token in ("urllib", "urlopen", "urlretrieve", "http.client", "httplib", "requests", "aiohttp", "ftplib", "wget",
                  "curl", "pip install", "pip.main", "invoke-webrequest", "bitsadmin", "certutil", "webclient",
                  "msiexec", "choco", "winget"):
        assert token not in text, token


def test_every_socket_address_is_loopback():
    for name in PROGRAMS:
        text = source(name)
        for call in re.findall(r"(?:\.bind|create_connection|\.connect|\.connect_ex)\(\(([^,)]*),", text):
            assert call.strip() == '"127.0.0.1"', (name, call)


@pytest.mark.parametrize("name", PROGRAMS)
def test_no_run_anyway_switch(name):
    text = source(name).lower()
    for token in ("--force", "force_admin", "allow_admin", "allow-admin", "allow_elevated", "ignore_admin", "ignore-admin",
                  "run_as_admin_ok", "i_know", "--yes-admin", "--elevated", "--as-admin", "skip_selfcheck", "skip_check",
                  "bypass"):
        assert token not in text, token


def test_the_only_command_line_option_is_plan():
    text = source("measure.py")
    assert re.findall(r'add_argument\(\s*"([^"]+)"', text) == ["--plan"]


def test_the_probe_never_touches_the_environment():
    """继承环境的诊断配法里子进程拿得到整份环境(可能有密钥): 探针这个文件里压根没有读环境的写法."""
    text = source("child_probe.py")
    for token in ("environ", "getenv", "putenv", "expandvars", "GetEnvironmentStrings", "os.path.expanduser"):
        assert token not in text, token


def test_the_report_only_ever_gets_variable_names_never_values():
    """measure.py 里 os.environ 只出现在: 读那几个名字、extended_env 的取值(交给 spawn, 不记录)、Redactor 取名字."""
    text = source("measure.py")
    assert not re.search(r"(rec|result|report|notes|ctx\.notes)\[[^\]]*\]\s*=[^\n]*os\.environ\[", text)
    assert "os.environ[name]" in text and text.count("os.environ[name]") == 1       # extended_env 里那一处
    assert 'rec["环境变量"] = "最小五个 + " + "、' in text                              # 记的是名字的拼接


def test_environment_variables_read_are_only_names_and_system_paths():
    allowed = {"USERNAME", "COMPUTERNAME", "USERDOMAIN", "SystemRoot", "windir", "LOCALAPPDATA", "TMP", "TEMP"}
    for name in PROGRAMS:
        text = source(name)
        read = set(re.findall(r'os\.environ(?:\.get)?[\[(]\s*"([^"]+)"', text))
        assert read <= allowed, (name, read - allowed)
        assert "getenv" not in text


@pytest.mark.parametrize("name", PROGRAMS)
def test_environment_is_never_exported_whole(name):
    text = source(name)
    assert not re.search(r"dict\(os\.environ|os\.environ\.copy|os\.environ\.items|os\.environ\.keys|os\.environ\.values|"
                         r"\*\*os\.environ|json\.dumps\(os\.environ|str\(os\.environ|repr\(os\.environ|os\.environb", text)


@pytest.mark.parametrize("name", PROGRAMS)
def test_no_way_to_lower_privileges_and_rerun(name):
    text = source(name).lower()
    for token in ("parent_process", "explorer.exe", "schtasks", ".vbs", "wscript", "saferc", "createprocesswithtoken",
                  "createprocesswithlogon", "wtsquerytoken", "wtsqueryusertoken",
                  "run_unelevated", "seassignprimarytoken", "adjusttokenprivileges", "logonuser", "duplicatetokenex",
                  "runasuser", "runasinvoker", "trustlevel", "__compat_layer", "desktop shell", "shellwindow"):
        assert token not in text, token


def test_impersonation_exists_only_in_the_one_read_only_diagnostic_and_is_always_reverted():
    """D1 诊断要让临时线程换成别的令牌的身份: 只许出现在 open_as_token 一个函数里, 且 finally 里换回来; 别处一律没有."""
    layer = source("sandbox_win.py")
    assert layer.count("a.ImpersonateLoggedOnUser(") == 1 and layer.count("a.RevertToSelf()") == 1
    start = layer.index("def open_as_token(")
    end = layer.index("def close_handle(")
    body = layer[start:end]
    assert "a.ImpersonateLoggedOnUser(" in body and "finally:\n            a.RevertToSelf()" in body
    assert "threading.Thread(target=work" in body and body.count("CreateFileW(") == 1
    for forbidden in ("GENERIC_WRITE", "FILE_GENERIC_WRITE", "ACCESS_MODIFY", "SetFile", "DeleteFile", "CreateProcess"):
        assert forbidden not in body, forbidden
    assert "ImpersonateLoggedOnUser" not in source("measure.py") + source("child_probe.py")
    assert "RevertToSelf" not in source("measure.py") + source("child_probe.py")


def test_the_elevation_request_appears_exactly_once_and_only_in_the_probe():
    counts = {name: len(re.findall(r'"runas"', source(name))) for name in PROGRAMS}
    assert counts == {"measure.py": 0, "sandbox_win.py": 0, "child_probe.py": 1}
    for name in ("measure.py", "sandbox_win.py"):
        assert "ShellExecute" not in source(name)
    probe = source("child_probe.py")
    assert probe.count("ShellExecuteExW(") == 1 and "ShellExecuteW" not in probe and "ShellExecuteA" not in probe


def test_registry_is_only_read():
    allowed = {"OpenKey", "QueryValueEx", "HKEY_LOCAL_MACHINE", "HKEY_CURRENT_USER", "KEY_READ", "KEY_WOW64_64KEY"}
    for name in PROGRAMS:
        used = set(re.findall(r"winreg\.([A-Za-z_0-9]+)", source(name)))
        assert used <= allowed, (name, used - allowed)
        assert "reg add" not in source(name).lower() and "reg delete" not in source(name).lower()
    assert "KEY_WRITE" not in "".join(source(n) for n in PROGRAMS) and "KEY_ALL_ACCESS" not in "".join(source(n) for n in PROGRAMS)


def test_only_the_documented_security_changing_calls_exist():
    """sandbox_win 里改权限、改令牌的接口就这几个; 别的“改系统”的接口一个都没有."""
    text = source("sandbox_win.py")
    assert set(re.findall(r"a\.([A-Z][A-Za-z]+) = decl\(", text)) >= {"SetNamedSecurityInfoW", "SetEntriesInAclW"}
    for forbidden in ("SetSecurityInfo", "SetSecurityDescriptorDacl", "TakeOwnership", "SetFileSecurity", "LsaAddAccountRights",
                      "NetUserAdd", "NetLocalGroupAddMembers", "CreateService", "ChangeServiceConfig", "RegSetValue",
                      "SetSystemTime", "SetEnvironmentVariable", "SystemParametersInfo", "InstallDriver", "FwpmFilterAdd",
                      "INetFwPolicy", "netsh", "bcdedit", "powercfg", "sc.exe"):
        assert forbidden not in text, forbidden


def test_programs_only_start_processes_through_the_sandbox_layer():
    for name in ("measure.py", "child_probe.py"):
        text = source(name)
        for token in ("subprocess", "os.system", "os.popen", "os.spawn", "os.exec", "Popen", "multiprocessing",
                      "CreateProcess"):
            assert token not in text, (name, token)
    layer = source("sandbox_win.py")
    assert layer.count("a.CreateProcessW(") == 1 and layer.count("a.CreateProcessAsUserW(") == 1
    assert "subprocess.list2cmdline" in layer and "subprocess.Popen" not in layer and "subprocess.run" not in layer


@pytest.mark.parametrize("name", PROGRAMS + ["README.md", "真机结果分析-20261116.md", "plan-output-20261116.txt", "run_me.cmd", "看看会做什么.cmd", "tests/conftest.py",
                                             "tests/fakes.py", "tests/test_plan.py", "tests/test_selfcheck.py",
                                             "tests/test_source_scan.py", "tests/test_simulated_run.py",
                                             "tests/test_measure_pure.py", "tests/test_sandbox_pure.py",
                                             "tests/test_probe_protocol.py"])
def test_new_files_are_lf_utf8_without_bom(name):
    raw = (ISO / name).read_bytes()
    assert raw and not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    raw.decode("utf-8")
    assert raw.endswith(b"\n")


@pytest.mark.parametrize("name", ["run_me.cmd", "看看会做什么.cmd"])
def test_cmd_files_are_short_ascii_and_do_what_the_description_says(name):
    raw = (ISO / name).read_bytes()
    raw.decode("ascii")                                   # 内容只用 ASCII (文件名可以是中文)
    lines = raw.decode("ascii").splitlines()
    assert 3 <= len(lines) <= 5
    assert lines[0] == "@echo off" and lines[1] == 'cd /d "%~dp0"' and lines[-1] == "pause"
    run = [ln for ln in lines if ln.startswith("python ")]
    assert run == (["python measure.py"] if name == "run_me.cmd" else ["python measure.py --plan"])


def test_work_is_not_pre_created_and_results_only_holds_real_runs():
    """work\\ 和 results\\ 由 measure.py 运行时自己建; 交付时两个都不存在. 用户真跑过以后 results\\ 里会有
    <年月日-时分秒> 命名的结果文件夹, 那是真跑的产物, 除此以外不该有别的."""
    assert not (ISO / "work").exists()
    if (ISO / "results").exists():
        assert all(re.fullmatch(r"\d{8}-\d{6}", p.name) for p in (ISO / "results").iterdir())


@pytest.mark.parametrize("path", sorted(p.relative_to(ISO).as_posix() for p in ISO.rglob("*.py")))
def test_every_python_file_compiles(path, tmp_path):
    py_compile.compile(str(ISO / path), cfile=str(tmp_path / "out.pyc"), doraise=True)

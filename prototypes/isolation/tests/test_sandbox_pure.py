"""sandbox_win 里不碰系统的部分: SID 转换、环境块、命令行、访问掩码、结构体布局、接口表、Child 的收发."""
from __future__ import annotations

import ctypes
import io
import os
import sys
import threading
import time

import pytest

import sandbox_win as sw

WIN = sys.platform == "win32"


def test_sid_known_bytes():
    assert sw.sid_from_string("S-1-5-32-544") == bytes.fromhex("01020000000000052000000020020000")
    assert sw.sid_from_string("S-1-1-0") == bytes.fromhex("010100000000000100000000")
    assert sw.sid_from_string("S-1-16-4096") == bytes.fromhex("010100000000001000100000")


@pytest.mark.parametrize("text", ["S-1-5-32-544", "S-1-1-0", "S-1-5-12", "S-1-15-2-1", "S-1-16-4096",
                                  "S-1-5-21-1111111111-2222222222-3333333333-1001", "S-1-5-5-0-12345"])
def test_sid_roundtrip(text):
    assert sw.sid_to_string(sw.sid_from_string(text)) == text


@pytest.mark.parametrize("bad", ["", "X-1-5", "S-1-5", "S-2-5-1", "S-1-5-4294967296", "S-1-5-" + "-".join(["1"] * 16)])
def test_sid_from_string_rejects(bad):
    with pytest.raises(ValueError):
        sw.sid_from_string(bad)


def test_sid_to_string_rejects_short_or_inconsistent():
    with pytest.raises(ValueError):
        sw.sid_to_string(b"\x01\x02\x00")
    with pytest.raises(ValueError):
        sw.sid_to_string(sw.sid_from_string("S-1-5-32-544") + b"\x00")


def test_env_block_sorted_case_insensitively_and_double_nul():
    block = sw.build_env_block({"b": "2", "A": "1", "_x": "3", "Zed": "4"})
    assert block == "A=1\0b=2\0Zed=4\0_x=3\0\0"
    assert sw.build_env_block({}) == "\0\0"


@pytest.mark.parametrize("env", [{"a=b": "x"}, {"": "x"}, {"a": "x\0y"}, {"a\0b": "x"}])
def test_env_block_rejects_bad_entries(env):
    with pytest.raises(ValueError):
        sw.build_env_block(env)


def test_command_line_quotes_only_what_needs_it():
    line = sw.build_command_line("C:\\x y\\python.exe", ["-I", "-B", "-S", "C:\\a\\child probe.py"])
    assert line == '"C:\\x y\\python.exe" -I -B -S "C:\\a\\child probe.py"'
    assert sw.build_command_line("C:\\py\\python.exe", ["-I", "x.py"]) == "C:\\py\\python.exe -I x.py"


def test_appcontainer_name_rule():
    assert sw.valid_appcontainer_name("wm.ayane.isolation.proto")
    assert sw.valid_appcontainer_name("a" * 64)
    assert not sw.valid_appcontainer_name("a" * 65)
    assert not sw.valid_appcontainer_name("")
    assert not sw.valid_appcontainer_name("has space")
    assert not sw.valid_appcontainer_name("中文")


def test_access_masks_match_explorer_rights():
    assert sw.ACCESS_READ_EXEC == 0x1200A9      # 读取和执行
    assert sw.ACCESS_MODIFY == 0x1301BF         # 修改
    assert sw.FILE_ALL_ACCESS == 0x1F01FF       # 完全控制
    assert sw.INHERIT_BOTH == 3


def test_sandbox_error_message_shows_code():
    assert "CreateProcessW" in str(sw.SandboxError("CreateProcessW", 5))
    assert "0x80070005" in str(sw.SandboxError("CreateAppContainerProfile", 0x80070005))


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="下面的大小是 64 位下的")
def test_struct_sizes_match_windows_headers_on_x64():
    size = ctypes.sizeof
    assert size(sw.SECURITY_ATTRIBUTES) == 24
    assert size(sw.STARTUPINFOW) == 104
    assert size(sw.STARTUPINFOEXW) == 112
    assert size(sw.PROCESS_INFORMATION) == 24
    assert size(sw.SID_AND_ATTRIBUTES) == 16
    assert size(sw.TOKEN_MANDATORY_LABEL) == 16
    assert size(sw.TOKEN_DEFAULT_DACL) == 8
    assert size(sw.SECURITY_CAPABILITIES) == 24
    assert size(sw.TRUSTEE_W) == 32
    assert size(sw.EXPLICIT_ACCESS_W) == 48
    assert size(sw.IO_COUNTERS) == 48
    assert size(sw.JOBOBJECT_BASIC_LIMIT_INFORMATION) == 64
    assert size(sw.JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 144
    assert size(sw.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION) == 48
    assert sw.TOKEN_GROUPS.Groups.offset == 8
    assert sw.EXPLICIT_ACCESS_W.Trustee.offset == 16


# 每个用到的系统接口的参数个数, 对着微软文档里的原型核一遍 (只载入库、读 argtypes, 一个接口都不调)
API_ARGC = {
    "GetCurrentProcess": 0, "CloseHandle": 1, "CreatePipe": 4, "SetHandleInformation": 3,
    "InitializeProcThreadAttributeList": 4, "UpdateProcThreadAttribute": 7, "DeleteProcThreadAttributeList": 1,
    "CreateProcessW": 10, "ResumeThread": 1, "WaitForSingleObject": 2, "GetExitCodeProcess": 2,
    "TerminateProcess": 2, "OpenProcess": 3, "SetErrorMode": 1, "CreateJobObjectW": 2, "SetInformationJobObject": 4,
    "QueryInformationJobObject": 5, "AssignProcessToJobObject": 2, "TerminateJobObject": 2, "LocalFree": 1,
    "OpenProcessToken": 3, "GetTokenInformation": 5, "SetTokenInformation": 4, "CreateRestrictedToken": 9,
    "CreateProcessAsUserW": 11, "GetNamedSecurityInfoW": 8, "SetNamedSecurityInfoW": 7, "SetEntriesInAclW": 4,
    "ConvertStringSecurityDescriptorToSecurityDescriptorW": 4,
    "ConvertSecurityDescriptorToStringSecurityDescriptorW": 5, "GetSecurityDescriptorSacl": 4, "FreeSid": 1,
    "CreateAppContainerProfile": 6, "DeriveAppContainerSidFromAppContainerName": 2, "DeleteAppContainerProfile": 1,
    # 11-16 加的诊断 (open_as_token) 用到的
    "GetCurrentThread": 0, "GetFileAttributesW": 1, "CreateFileW": 7, "ImpersonateLoggedOnUser": 1, "RevertToSelf": 0,
    "OpenThreadToken": 4,
}


@pytest.mark.skipif(not WIN, reason="只在 Windows 上")
def test_api_table_builds_and_argument_counts_match_documentation():
    api = sw._api()
    for name, argc in API_ARGC.items():
        fn = getattr(api, name)
        assert fn.argtypes is not None and len(fn.argtypes) == argc, name
    # 返回 HRESULT 的三个要按有符号 32 位取 (出错是负数)
    for name in ("CreateAppContainerProfile", "DeriveAppContainerSidFromAppContainerName", "DeleteAppContainerProfile"):
        assert getattr(api, name).restype is ctypes.c_long


class FakeJob:
    def __init__(self):
        self.terminated = 0
        self.closed = False

    def terminate(self, code=1):
        self.terminated += 1

    def active(self):
        return 0

    def close(self):
        self.closed = True


def make_child(stdout_bytes: bytes = b"", *, live_pipe: bool = False):
    """不起进程的 Child: 管道换成内存或者真的 os.pipe, 作业对象换成假的."""
    fin = io.BytesIO()
    if live_pipe:
        read_fd, write_fd = os.pipe()
        fout = os.fdopen(read_fd, "rb")
        writer = os.fdopen(write_fd, "wb", buffering=0)
    else:
        fout, writer = io.BytesIO(stdout_bytes), None
    job = FakeJob()
    child = sw.Child(None, 1234, job, fin, fout, io.BytesIO(b"oops\n"), 1.5)
    return child, fin, writer, job


def test_child_ask_matches_by_id_and_skips_stale_replies():
    child, fin, _, job = make_child(b'{"ok": true, "id": 99}\n{"ok": true, "pong": "n", "id": 1}\n')
    reply = child.ask({"cmd": "ping", "nonce": "n"}, 2.0)
    assert reply["pong"] == "n"
    sent = fin.getvalue().decode("ascii")
    assert sent.endswith("\n") and '"cmd": "ping"' in sent and '"id": 1' in sent
    summary = child.close()
    assert summary == {"exited": True, "active_in_job": 0, "exit_code": None}
    assert job.terminated >= 1 and job.closed
    assert child.stderr_text() == "oops\n"
    assert child.close() is summary  # 重复 close 返回同一个字典


def test_child_send_is_ascii_even_for_chinese_paths():
    child, fin, _, _ = make_child(b"")
    child.send({"cmd": "read", "path": "G:\\中文\\x.txt"})
    data = fin.getvalue()
    data.decode("ascii")  # 不抛就是纯 ASCII
    assert b"\\u4e2d" in data
    child.close()


def test_child_recv_eof_and_bad_line():
    child, _, _, _ = make_child(b"not json\n")
    assert child.recv(2.0) == {"ok": False, "err": "bad_line"}
    with pytest.raises(EOFError):
        child.recv(2.0)
    child.close()


def test_child_recv_times_out_on_a_silent_pipe():
    child, _, writer, _ = make_child(live_pipe=True)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        child.recv(0.2)
    assert time.monotonic() - t0 < 2
    writer.write(b'{"ok": true, "id": 1}\n')
    assert child.recv(2.0)["ok"] is True
    writer.close()  # 对方关了管道 -> 读线程收到 EOF
    with pytest.raises(EOFError):
        child.recv(2.0)
    child.close()


def test_child_ask_gives_up_after_the_deadline_even_with_only_stale_replies():
    child, _, writer, _ = make_child(live_pipe=True)

    def noise():
        for _ in range(5):
            writer.write(b'{"ok": true, "id": 77}\n')
            time.sleep(0.05)
    threading.Thread(target=noise, daemon=True).start()
    with pytest.raises(TimeoutError):
        child.ask({"cmd": "ping"}, 0.4)
    writer.close()
    child.close()

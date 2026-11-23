"""探针 child_probe.py 的协议 (在测试进程里直接调它的函数): 一行进一行出、出错不抛、回话带编号、不回路径.
info 和 elevate 会碰系统接口, 这里一次都不调."""
from __future__ import annotations

import ctypes
import io
import json
import socket
import sys

import pytest

import child_probe


def run_main(monkeypatch, lines):
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(line + "\n" for line in lines)))
    monkeypatch.setattr(sys, "stdout", out)
    child_probe.main()
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_ping_unknown_bad_json_and_quit(monkeypatch):
    replies = run_main(monkeypatch, [
        json.dumps({"cmd": "ping", "nonce": "n1", "id": 1}),
        json.dumps({"cmd": "nope", "id": 2}),
        "this is not json",
        json.dumps({"cmd": "quit", "id": 4}),
        json.dumps({"cmd": "ping", "nonce": "after-quit", "id": 5}),
    ])
    assert [r.get("id") for r in replies] == [1, 2, None, 4]  # quit 以后不再处理
    assert replies[0]["ok"] is True and replies[0]["pong"] == "n1" and replies[0]["bits"] in (32, 64)
    assert replies[1] == {"ok": False, "err": "unknown_cmd", "id": 2}
    assert replies[2]["ok"] is False and replies[2]["err"] == "JSONDecodeError"
    assert replies[3] == {"ok": True, "id": 4}


def test_errors_keep_the_request_id_and_never_carry_paths(monkeypatch, tmp_path):
    missing = tmp_path / "secret_looking_name_xyz.txt"
    replies = run_main(monkeypatch, [json.dumps({"cmd": "read", "path": str(missing), "expect": "x", "id": 7})])
    assert replies[0]["id"] == 7 and replies[0]["ok"] is False and replies[0]["err"] == "FileNotFoundError"
    assert set(replies[0]) == {"ok", "err", "errno", "winerror", "id"}
    assert "secret_looking_name_xyz" not in json.dumps(replies[0])


def test_read_reports_length_and_match_but_never_content(tmp_path):
    f = tmp_path / "box.txt"
    f.write_bytes(b"FAKE-LINE\n")
    assert child_probe.do_read({"path": str(f), "expect": "FAKE-LINE"}) == {"ok": True, "n": 10, "eq": True}
    assert child_probe.do_read({"path": str(f), "expect": "other"}) == {"ok": True, "n": 10, "eq": False}


def test_read_stops_at_4096_bytes(tmp_path):
    f = tmp_path / "big.txt"
    f.write_bytes(b"x" * 10000)
    assert child_probe.do_read({"path": str(f), "expect": ""})["n"] == 4096


def test_write_writes_the_fixed_text_only(tmp_path):
    f = tmp_path / "w.txt"
    assert child_probe.do_write({"path": str(f)}) == {"ok": True}
    assert f.read_bytes() == b"probe-write-test"


def test_connect_succeeds_against_a_local_listener_and_fails_when_nothing_listens():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    assert child_probe.do_connect({"port": port}) == {"ok": True}
    conn, _ = srv.accept()
    conn.close()
    srv.close()
    with pytest.raises(OSError) as info:  # 没人听了: 探针里这会被主循环兜成 {"ok": false, "err": ...}
        child_probe.do_connect({"port": port})
    reply = child_probe.fail(info.value)
    assert reply["ok"] is False and reply["err"] in ("ConnectionRefusedError", "timeout", "TimeoutError")


def test_handlers_table_is_exactly_the_documented_commands():
    assert set(child_probe.HANDLERS) == {"ping", "info", "read", "write", "connect", "elevate"}


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="112 是 64 位下的大小")
def test_shell_execute_struct_layout():
    sei = child_probe._sei_class()
    assert ctypes.sizeof(sei) == 112
    names = [f[0] for f in sei._fields_]
    assert names[:5] == ["cbSize", "fMask", "hwnd", "lpVerb", "lpFile"]
    assert names[-1] == "hProcess"
    assert getattr(sei, "hProcess").offset == 104 and getattr(sei, "nShow").offset == 48


def test_elevate_uses_only_the_runas_verb_and_the_documented_flags():
    import inspect
    src = inspect.getsource(child_probe.do_elevate)
    assert src.count('"runas"') == 1
    assert "0x40 | 0x100 | 0x400" in src  # NOCLOSEPROCESS | NOASYNC | FLAG_NO_UI
    assert "nShow = 0" in src             # SW_HIDE
    for forbidden in ('"open"', '"runasuser"', "CreateProcess", "subprocess", "os.system", "Popen"):
        assert forbidden not in src

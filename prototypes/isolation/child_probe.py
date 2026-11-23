"""沙箱里跑的探针 (2026-11-14 隔离原型). 只用标准库; json、sys 以外的库都在用到时才导入, 导入失败只影响那一条指令.

协议: 从标准输入读一行一个 JSON 指令, 做一件事, 回一行 JSON (纯 ASCII). 出了任何错都不抛, 回
{"ok": false, "err": 异常类名, "errno": .., "winerror": ..}; 不回路径和异常文字 (里面可能有账号名).

指令: ping / info / read / write / connect / elevate / quit
- elevate 的程序、参数、目录全由测量脚本给, 原样交给 ShellExecuteExW; 动词只有 runas 这一个, 探针自己不拼任何东西.
- 探针只碰测量脚本在指令里点名的路径, 那些都是测量脚本自己建的假文件.
"""
import json
import sys


def fail(exc):
    return {"ok": False, "err": type(exc).__name__, "errno": getattr(exc, "errno", None),
            "winerror": getattr(exc, "winerror", None)}


def do_ping(m):
    return {"ok": True, "pong": m.get("nonce"), "py": "%d.%d.%d" % sys.version_info[:3],
            "bits": 64 if sys.maxsize > 2**32 else 32}


def do_info(m):
    """自己令牌的几项: 是不是提权的、提权类型 (1 默认 2 完整 3 受限)、完整性级别、是不是应用容器、是不是受限的."""
    import ctypes
    from ctypes import wintypes
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD)]
    adv.IsTokenRestricted.argtypes = [wintypes.HANDLE]
    tok = wintypes.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x8, ctypes.byref(tok)):
        return {"ok": False, "err": "OpenProcessToken", "winerror": ctypes.get_last_error()}

    def dword(info_class):
        value, size = wintypes.DWORD(), wintypes.DWORD()
        return value.value if adv.GetTokenInformation(tok, info_class, ctypes.byref(value), 4, ctypes.byref(size)) else None

    level, buf, size = None, ctypes.create_string_buffer(128), wintypes.DWORD()
    if adv.GetTokenInformation(tok, 25, buf, 128, ctypes.byref(size)):  # TokenIntegrityLevel: 里面第一个字段是 SID 指针
        sid = ctypes.c_void_p.from_buffer(buf).value
        count = ctypes.string_at(sid + 1, 1)[0]
        level = int.from_bytes(ctypes.string_at(sid + 8 + 4 * (count - 1), 4), "little")
    out = {"ok": True, "elevated": dword(20), "elev_type": dword(18), "appcontainer": dword(29), "il": level,
           "restricted": int(bool(adv.IsTokenRestricted(tok)))}
    k32.CloseHandle(tok)
    return out


def do_read(m):
    with open(m["path"], "rb") as f:
        data = f.read(4096)
    return {"ok": True, "n": len(data), "eq": data.strip() == str(m.get("expect", "")).encode("utf-8")}


def do_write(m):
    with open(m["path"], "wb") as f:
        f.write(b"probe-write-test")
    return {"ok": True}


def do_connect(m):
    import socket
    socket.create_connection(("127.0.0.1", int(m["port"])), timeout=3).close()
    return {"ok": True}


def _sei_class():
    import ctypes
    from ctypes import wintypes

    class SEI(ctypes.Structure):  # SHELLEXECUTEINFOW, 64 位下 112 字节
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
    return SEI


def do_elevate(m):
    """向 Windows 要一次"以管理员身份运行"(只这一种请求). 返回请求的返回值和错误码; 要到了进程就等它退出."""
    import ctypes
    from ctypes import wintypes
    sei_type = _sei_class()
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(sei_type)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.GetProcessId.argtypes = [wintypes.HANDLE]
    k32.GetProcessId.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    ctypes.WinDLL("ole32").CoInitializeEx(None, 0x2 | 0x4)  # 按文档, ShellExecuteEx 之前先初始化 COM
    sei = sei_type()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x40 | 0x100 | 0x400  # NOCLOSEPROCESS (拿进程句柄) | NOASYNC | FLAG_NO_UI (出错不弹框)
    sei.lpVerb = "runas"  # 唯一的动词
    sei.lpFile, sei.lpParameters, sei.lpDirectory = m["file"], m["params"], m["dir"]
    sei.nShow = 0  # SW_HIDE
    ok = shell.ShellExecuteExW(ctypes.byref(sei))
    err = ctypes.get_last_error()
    out = {"ok": True, "ret": int(bool(ok)), "err": 0 if ok else err}
    if ok and sei.hProcess:
        out["pid"] = k32.GetProcessId(sei.hProcess)
        done = k32.WaitForSingleObject(sei.hProcess, int(m.get("wait_ms", 5000))) == 0
        out["waited"] = "exited" if done else "timeout"
        k32.CloseHandle(sei.hProcess)
    return out


HANDLERS = {"ping": do_ping, "info": do_info, "read": do_read, "write": do_write, "connect": do_connect,
            "elevate": do_elevate}


def reply(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in iter(sys.stdin.readline, ""):
        line = line.strip()
        if not line:
            continue
        msg_id = None
        try:
            msg = json.loads(line)
            msg_id = msg.get("id")  # 回话要带上指令的编号, 测量脚本靠它对上号
            cmd = msg.get("cmd")
            if cmd == "quit":
                reply({"ok": True, "id": msg_id})
                return
            handler = HANDLERS.get(cmd)
            out = handler(msg) if handler else {"ok": False, "err": "unknown_cmd"}
        except Exception as exc:  # noqa: BLE001 - 探针不能因为一条指令出错就退出
            out = fail(exc)
        out["id"] = msg_id
        try:
            reply(out)
        except OSError:
            return


if __name__ == "__main__":
    main()

"""整套流程模拟用的替身 (只在测试里用).

把 sandbox_win 里所有碰系统的函数换成假的: 假探针按每种关法的规矩回话 -- 读写的是临时目录里真的假文件, 连的是测量脚本自己
开的本机测试端口 -- 让 measure.run() 从头跑到尾. 验的是测量脚本自己的逻辑 (步骤顺序、判定、收尾、报告、个人信息),
不是 Windows 的隔离本身: 那只能用户双击 run_me.cmd 跑.
"""
from __future__ import annotations

import builtins
import re
import shutil
import sys
import tempfile
from pathlib import Path

import child_probe
import measure
import sandbox_win as sw

ISO = Path(__file__).resolve().parent.parent
PKG = "S-1-15-2-1-2-3-4-5-6-7"
USER = "S-1-5-21-111-222-333-1001"
LOGON = "S-1-5-5-0-999"
FAKE_NAME = "Zed_Test_User"
FAKE_COMPUTER = "TESTBOX-01"


def make_fake_runtime(root: Path) -> Path:
    for rel in ("Lib/encodings", "Lib/site-packages/pip", "Lib/test", "Lib/unittest/test", "Lib/idlelib", "Lib/tkinter",
                "Lib/ensurepip", "Lib/__pycache__", "Lib/json/__pycache__", "Lib/turtledemo", "DLLs", "Tools", "include"):
        (root / rel).mkdir(parents=True)
    files = {
        "python.exe": b"MZ", "pythonw.exe": b"MZ", "python310.dll": b"d", "python3.dll": b"d", "vcruntime140.dll": b"d",
        "LICENSE.txt": b"l", "Lib/os.py": b"#", "Lib/encodings/__init__.py": b"", "Lib/json/__init__.py": b"",
        "Lib/json/decoder.pyc": b"c", "Lib/__pycache__/os.cpython-310.pyc": b"c", "Lib/json/__pycache__/x.pyc": b"c",
        "Lib/site-packages/pip/__init__.py": b"", "Lib/test/test_x.py": b"", "Lib/unittest/test/t.py": b"",
        "Lib/idlelib/i.py": b"", "Lib/tkinter/__init__.py": b"", "Lib/ensurepip/__init__.py": b"",
        "Lib/turtledemo/d.py": b"", "DLLs/_socket.pyd": b"p", "DLLs/libffi-7.dll": b"d", "DLLs/py.ico": b"i",
        "DLLs/python_lib.cat": b"c", "Tools/t.py": b"", "include/Python.h": b"",
    }
    for rel, data in files.items():
        (root / rel).write_bytes(data)
    return root


def under(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


TOKENS = {
    "appcontainer": {"elevated": 0, "elev_type": 1, "il": 0x1000, "appcontainer": 1, "restricted": 0},
    "restricted_downgrade": {"elevated": 0, "elev_type": 3, "il": 0x1000, "appcontainer": 0, "restricted": 0},
    "restricted_sids": {"elevated": 0, "elev_type": 3, "il": 0x1000, "appcontainer": 0, "restricted": 1},
    "control": {"elevated": 0, "elev_type": 3, "il": 0x2000, "appcontainer": 0, "restricted": 0},
}
DENIED = {"ok": False, "err": "PermissionError", "errno": 13, "winerror": 5}


class FakeChild:
    """假探针: 按关法的规矩回话. 想要的错 (回话超时、进程没了) 用 behavior 指定."""

    def __init__(self, sim: Sim, mode: str, behavior: str = "ok") -> None:
        self.sim, self.mode, self.behavior = sim, mode, behavior
        self.pid = 4000 + len(sim.children)
        self.create_ms = 1.5
        self.asked: list[str] = []
        self._summary: dict | None = None

    def ask(self, obj: dict, timeout: float) -> dict:
        cmd = obj["cmd"]
        self.asked.append(cmd)
        if self.behavior == "no_pong" and cmd == "ping":
            raise TimeoutError("等探针回话超时")
        if self.behavior == "interrupt_on_ping" and cmd == "ping":
            raise KeyboardInterrupt           # 用户在等第一句回话时按了 Ctrl+C
        if self.behavior == "hang_on_connect" and cmd == "connect":
            raise TimeoutError("等探针回话超时")
        if cmd == "ping":
            return {"ok": True, "pong": obj["nonce"]}
        if cmd == "info":
            return {"ok": True, **TOKENS[self.mode]}
        if cmd == "read":
            return self._file_op(obj, child_probe.do_read, self.can_read)
        if cmd == "write":
            return self._file_op(obj, child_probe.do_write, self.can_write)
        if cmd == "connect":
            if self.mode == "appcontainer":
                return {"ok": False, "err": "PermissionError", "errno": 13, "winerror": 10013}
            try:
                return child_probe.do_connect(obj)
            except Exception as exc:  # noqa: BLE001
                return child_probe.fail(exc)
        if cmd == "elevate":
            return self._elevate(obj)
        if cmd == "quit":
            return {"ok": True}
        raise AssertionError(f"没想到的指令 {cmd}")

    # -- 每种关法的规矩 (模拟用; 真正的结果只有用户双击跑才知道)
    def can_read(self, path: Path) -> bool:
        if self.mode in ("control", "restricted_downgrade"):
            return True
        work = self.sim.iso / "work"
        return any(under(path, work / name) for name in ("pyrt", "probe", "home"))

    def can_write(self, path: Path) -> bool:
        return self.mode == "control" or under(path, self.sim.iso / "work" / "home")

    def _file_op(self, obj, fn, allowed) -> dict:
        if not allowed(Path(obj["path"])):
            return dict(DENIED)
        try:
            return fn(obj)
        except Exception as exc:  # noqa: BLE001
            return child_probe.fail(exc)

    def _elevate(self, obj) -> dict:
        assert obj["file"].lower().endswith("\\system32\\cmd.exe") and obj["params"].startswith("/d /s /c ")
        policy = self.sim.opts["elevate"].get(self.mode, "grant" if self.mode == "control" else "deny")
        if policy == "hang":
            raise TimeoutError("等探针回话超时")
        if policy == "deny":
            return {"ok": True, "ret": 0, "err": 5}
        word = "ELEVATED" if policy == "grant" else "NOT_ELEVATED"
        match = re.search(r"\(>(\S+) echo " + word + r"\)", obj["params"])
        Path(match.group(1)).write_bytes((word + "\r\n").encode("ascii"))   # 假装是那条以管理员身份跑的命令写的
        return {"ok": True, "ret": 1, "err": 0, "pid": 5555, "waited": "exited"}

    # -- sandbox_win.Child 的接口
    def exited(self) -> bool:
        return True

    def wait_exit(self, ms: int) -> bool:
        return True

    def exit_code(self) -> int:
        return 0

    def kill(self) -> None:
        pass

    def stderr_text(self) -> str:
        return self.sim.opts.get("stderr", "")

    def close(self) -> dict:
        if self._summary is None:
            stuck = self.mode in self.sim.opts.get("not_exited", ())   # 结束作业后 5 秒还没退出
            self._summary = {"exited": not stuck, "active_in_job": 1 if stuck else 0, "exit_code": None if stuck else 0}
            self.sim.calls["closed"] += 1
        return self._summary


class FakeCmdChild:
    """诊断 D2 起的系统自带 cmd.exe /d /c exit 7 的替身: 立刻退出, 退出码按 opts['cmd_exit'] (默认 7)."""

    def __init__(self, sim: Sim) -> None:
        self.sim = sim
        self.pid = 9000
        self.create_ms = 1.0
        self._summary: dict | None = None

    def wait_exit(self, ms: int) -> bool:
        return not self.sim.opts.get("cmd_hang")

    def exit_code(self) -> int | None:
        return None if self.sim.opts.get("cmd_hang") else self.sim.opts.get("cmd_exit", 7)

    def stderr_text(self) -> str:
        return ""

    def close(self) -> dict:
        if self._summary is None:
            self._summary = {"exited": True, "active_in_job": 0, "exit_code": self.exit_code()}
        return self._summary


class Sim:
    """装好所有替身, 记下测量脚本调了什么. opts: answer (对“真试吗”的回答)、elevate (关法 -> grant/deny/notelevated/hang)、
    spawn_plan (关法 -> 依次每次起探针的结果: ok / no_pong / hang_on_connect / error:码)、registered (登记表里原来有没有)."""

    def __init__(self, monkeypatch, tmp_path: Path, **opts) -> None:
        self.opts = {"answer": "y", "elevate": {}, "spawn_plan": {}, "registered": False, **opts}
        self.calls: dict = {"spawn": [], "tokens": [], "register": 0, "unregister": 0, "set_dacl": [], "label": [],
                            "protect": [], "closed": 0, "cmd": [], "open_as_token": []}
        self.children: list[FakeChild] = []
        self._plan_pos: dict[str, int] = {}
        self._token_modes: dict[int, str] = {}
        self.fake_home = tmp_path / "Users" / FAKE_NAME
        self.iso = tmp_path / "iso"
        self.iso.mkdir()
        shutil.copy2(ISO / "child_probe.py", self.iso / "child_probe.py")
        self.runtime = make_fake_runtime(self.fake_home / "Programs" / "Python310")
        self.sys_temp = self.fake_home / "AppData" / "Local" / "Temp"
        self.sys_temp.mkdir(parents=True)
        mp = monkeypatch
        for key, value in (("USERPROFILE", str(self.fake_home)), ("HOME", str(self.fake_home)),
                           ("USERNAME", FAKE_NAME), ("COMPUTERNAME", FAKE_COMPUTER), ("USERDOMAIN", FAKE_COMPUTER),
                           ("LOCALAPPDATA", str(self.fake_home / "AppData" / "Local")), ("SystemRoot", "C:\\Windows")):
            mp.setenv(key, value)
        mp.setattr(measure, "HERE", self.iso)
        mp.setattr(measure, "SETTLE_S", 0.1)            # 模拟里不用等那么久 (真跑时是 0.2 和 0.5)
        mp.setattr(measure, "ELEV_REFUSED_LOOK_S", 0.1)
        mp.setattr(measure, "RETRY_WAIT_S", 0.05)
        mp.setattr(sys, "base_prefix", str(self.runtime))
        mp.setattr(tempfile, "gettempdir", lambda: str(self.sys_temp))
        self.prompts: list[str] = []
        mp.setattr(builtins, "input", lambda prompt="": self.prompts.append(prompt) or self.opts["answer"])
        mp.setattr(measure, "read_uac_settings",
                   lambda: {"EnableLUA": 1, "ConsentPromptBehaviorAdmin": 0, "PromptOnSecureDesktop": 0})
        mp.setattr(sw, "is_elevated", lambda: False)
        mp.setattr(sw, "own_integrity_rid", lambda: 0x2000)
        mp.setattr(sw, "current_user_sid", lambda: sw.sid_from_string(USER))
        mp.setattr(sw, "current_logon_sid", lambda: sw.sid_from_string(LOGON))
        mp.setattr(sw, "appcontainer_sid", lambda name: sw.sid_from_string(PKG))
        mp.setattr(sw, "appcontainer_registered", lambda sid: self.opts["registered"])
        mp.setattr(sw, "protect_user_only", lambda path, sid: self.calls["protect"].append(path))
        mp.setattr(sw, "set_dacl", lambda path, entries, **kw: self.calls["set_dacl"].append((path, entries, kw)))
        mp.setattr(sw, "set_low_label", lambda path: self.calls["label"].append(path))
        mp.setattr(sw, "dacl_sddl", self._sddl)
        mp.setattr(sw, "make_restricted_token", self._token)
        mp.setattr(sw, "close_handle", lambda handle: None)
        mp.setattr(sw, "spawn", self._spawn)
        mp.setattr(sw, "appcontainer_register", self._register)
        mp.setattr(sw, "appcontainer_unregister", self._unregister)
        mp.setattr(sw, "process_exists", lambda pid: False)
        mp.setattr(sw, "open_as_token", self._open_as_token)

    # -- 替身
    @staticmethod
    def _sddl(path: str) -> str:
        if "wm_iso_" in path:  # 假保管箱所在的文件夹: 只有本人、SYSTEM、Administrators
            return f"D:PAI(A;OICI;FA;;;{USER})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        return f"D:PAI(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICI;0x1301bf;;;{PKG})(A;OICIID;FA;;;{USER})(A;OICIID;0x1200a9;;;BU)"

    def _token(self, *, disable=(), restrict=(), low_integrity=True, default_dacl_sids=(), notes=None,
               flags=sw.DISABLE_MAX_PRIVILEGE):
        bad = self.opts.get("token_fail", ())
        if any(sid in bad for sid in restrict):   # 真机 11-16: CreateRestrictedToken 对应用容器 SID 回 87
            self.calls["token_rejected"] = self.calls.get("token_rejected", 0) + 1
            raise sw.SandboxError("CreateRestrictedToken", 87)
        handle = 100 + len(self.calls["tokens"])
        self.calls["tokens"].append({"disable": list(disable), "restrict": list(restrict), "low": low_integrity,
                                     "default_dacl": list(default_dacl_sids), "flags": flags})
        if notes is not None and self.opts.get("token_note"):
            notes.append(self.opts["token_note"])
        self._token_modes[handle] = "restricted_sids" if restrict else "restricted_downgrade"
        return handle

    def _register(self, name, display, description):
        self.calls["register"] += 1
        return sw.sid_from_string(PKG), True

    def _unregister(self, name):
        self.calls["unregister"] += 1
        return 0

    def _open_as_token(self, token, paths):
        self.calls["open_as_token"].append(list(paths))
        if self.opts.get("d1_fail"):
            return {"impersonated": False, "level": 1, "error": "线程令牌没到模拟级别 (类型 2, 级别 1), 诊断没做成", "results": {}}
        codes = self.opts.get("d1_codes", {})
        return {"impersonated": True, "level": 2, "error": None,
                "results": {p: codes.get(p.rsplit("\\", 1)[-1], [0, 0]) for p in paths}}

    def _spawn(self, exe, args, cwd, env, *, token=None, appcontainer_sid_bytes=None):
        mode = ("appcontainer" if appcontainer_sid_bytes else "control" if token is None
                else self._token_modes[token])
        if exe.lower().endswith("\\cmd.exe"):  # 诊断 D2: 同一个身份起系统自带的 cmd.exe /d /c exit 7 (不占 spawn_plan 的名额)
            assert args == ["/d", "/c", "exit", "7"] and mode != "control"
            self.calls["cmd"].append({"mode": mode, "env": env, "cwd": cwd, "token": token, "appc": appcontainer_sid_bytes})
            fail = self.opts.get("cmd_fail", {})
            if mode in fail:
                raise sw.SandboxError("CreateProcessW", fail[mode])
            child = FakeCmdChild(self)
            return child
        self.calls["spawn"].append({"mode": mode, "exe": exe, "args": args, "cwd": cwd, "env": env,
                                    "appc": appcontainer_sid_bytes})
        plan = self.opts["spawn_plan"].get(mode, [])
        pos = self._plan_pos.get(mode, 0)
        self._plan_pos[mode] = pos + 1
        step = plan[pos] if pos < len(plan) else "ok"
        if step.startswith("error:"):
            raise sw.SandboxError("CreateProcessW", int(step.split(":")[1]))
        child = FakeChild(self, mode, step)
        self.children.append(child)
        return child

    # -- 方便断言
    @property
    def results(self) -> Path:
        runs = sorted((self.iso / "results").iterdir())
        assert len(runs) == 1
        return runs[0]

    def report(self) -> dict:
        import json
        return json.loads((self.results / "report.json").read_text(encoding="utf-8"))

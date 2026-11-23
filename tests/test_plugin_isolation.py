"""插件宿主第三切片②: 动作在子进程里执行 —— 隔离、超时强杀、崩溃转码 (验收测试).

和 `test_plugin_host_actions.py` 用同一套最小插件包 (直接 import 它的 helper); 这里只关心执行边界:
子进程真的另起一个解释器、超时真的杀掉、崩溃/坏结果转成结构化错误、并发两次各起各的、
关开关时一个子进程都不起。全程 tmp_path, 不碰真 `~/.worldmodel/plugins`。
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
import pytest

from wm.agent import plugin_host as ph

from test_plugin_host_actions import demo_manifest, install

RESULT_KEYS = {"ok", "action", "result", "error", "receipt_id"}


@pytest.fixture
def host(tmp_path, monkeypatch):
    """开关打开, 插件目录指到临时目录。"""
    monkeypatch.setenv(ph.SWITCH_ENV, "on")
    root = tmp_path / "plugins"
    monkeypatch.setenv(ph.HOME_ENV, str(root))
    return root


def _installed(tmp_path, source, *, name="pkg", actions=None):
    """装一个插件 (默认清单 demo_look/demo_move), 返回 (插件 id, 装好以后的插件目录)."""
    manifest = demo_manifest(actions) if actions is not None else None
    plugin_id = install(tmp_path, manifest_obj=manifest, source=source, name=name)
    return plugin_id, Path(ph.home()) / plugin_id


def test_action_runs_in_a_separate_interpreter(host, tmp_path):
    plugin_id, _ = _installed(tmp_path, '''
import os
import sys


def look(room):
    return {"pid": os.getpid(), "ppid": os.getppid(), "exe": sys.executable, "room": room}


ACTIONS = {"demo_look": look}
''')
    out = ph.invoke(plugin_id, "demo_look", {"room": "客厅"})
    assert out["ok"] is True, out
    assert set(out) == RESULT_KEYS
    result = out["result"]
    assert result["pid"] != os.getpid()          # 另起了一个解释器
    assert result["ppid"] == os.getpid()         # 是宿主起的子进程
    assert Path(result["exe"]).stem.lower().startswith("python")
    assert result["room"] == "客厅"


def test_child_does_not_load_the_host_executor(host, tmp_path):
    plugin_id, _ = _installed(tmp_path, '''
import sys


def look(room):
    loaded = sorted(name for name in sys.modules if name == "wm" or name.startswith("wm."))
    return {"loaded": loaded, "plugin_host": "wm.agent.plugin_host" in sys.modules}


ACTIONS = {"demo_look": look}
''')
    out = ph.invoke(plugin_id, "demo_look", {"room": "x"})
    assert out["ok"] is True, out
    assert out["result"]["plugin_host"] is False          # 宿主执行器没被带进子进程
    assert out["result"]["loaded"] == []                  # 子进程也没顺手加载宿主的别的模块


def test_timeout_kills_the_child_process(host, tmp_path, monkeypatch):
    monkeypatch.setenv(ph.TIMEOUT_ENV, "1.5")
    plugin_id, plugin_dir = _installed(tmp_path, '''
import os
import time
from pathlib import Path


def look(room):
    Path(__file__).with_name("child.pid").write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(30)
    return {"room": room}


ACTIONS = {"demo_look": look}
''')
    pid_file = plugin_dir / "child.pid"
    started = time.monotonic()
    out = ph.invoke(plugin_id, "demo_look", {"room": "x"})
    elapsed = time.monotonic() - started
    assert out["ok"] is False and out["error"]["code"] == "E_TIMEOUT"
    assert "秒" in out["error"]["message"] and "杀掉" in out["error"]["message"]
    assert elapsed < 20, elapsed  # 没有傻等插件 sleep 的 30 秒
    assert pid_file.is_file(), "插件没跑起来, 这个测试没意义"
    pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert psutil.pid_exists(pid) is False, "超时以后子进程还活着 (pid=%d)" % pid


def test_plugin_exception_becomes_e_crash_without_traceback(host, tmp_path):
    plugin_id, _ = _installed(tmp_path, '''
def look(room):
    raise RuntimeError("插件自己的错误" * 40)


ACTIONS = {"demo_look": look}
''')
    out = ph.invoke(plugin_id, "demo_look", {"room": "x"})
    assert out["ok"] is False and out["error"]["code"] == "E_CRASH"
    message = out["error"]["message"]
    assert "RuntimeError" in message and "插件自己的错误" in message
    assert "Traceback" not in message and len(message) < 300


def test_child_interpreter_death_is_e_crash(host, tmp_path):
    plugin_id, _ = _installed(tmp_path, '''
import os


def look(room):
    os._exit(3)


ACTIONS = {"demo_look": look}
''')
    out = ph.invoke(plugin_id, "demo_look", {"room": "x"})
    assert out["ok"] is False and out["error"]["code"] == "E_CRASH"
    assert "exit=3" in out["error"]["message"]
    assert "Traceback" not in out["error"]["message"]


def test_bad_result_types_are_structured(host, tmp_path):
    plugin_id, _ = _installed(tmp_path, '''
def look(room):
    return [room]


ACTIONS = {"demo_look": look}
''')
    out = ph.invoke(plugin_id, "demo_look", {"room": "x"})
    assert out["ok"] is False and out["error"]["code"] == "E_RESULT_TYPE"
    ph.uninstall(plugin_id)

    plugin_id, _ = _installed(tmp_path, '''
def look(room):
    return {"room": object()}


ACTIONS = {"demo_look": look}
''', name="pkg2")
    out = ph.invoke(plugin_id, "demo_look", {"room": "x"})
    assert out["ok"] is False and out["error"]["code"] == "E_RESULT_NOT_JSON"
    assert "Traceback" not in out["error"]["message"]


def test_params_round_trip_through_json_and_bad_values_stay_out(host, tmp_path):
    plugin_id, _ = _installed(tmp_path, '''
def look(room, limit=None, mode=None):
    return {"room": room, "limit": limit, "mode": mode}


ACTIONS = {"demo_look": look}
''')
    room = "中文🙂\n第二行\t带制表符"
    out = ph.invoke(plugin_id, "demo_look", {"room": room, "limit": -5, "mode": "fast"})
    assert out["ok"] is True, out
    assert out["result"] == {"room": room, "limit": -5, "mode": "fast"}  # 原样过去、原样回来
    bad = ph.invoke(plugin_id, "demo_look", {"room": b"bytes"})
    assert bad["ok"] is False and bad["error"]["code"] == "E_PARAM_TYPE"  # 坏值在起子进程前就拦住


def test_concurrent_invocations_are_isolated(host, tmp_path):
    plugin_id, _ = _installed(tmp_path, '''
import os
import time


def look(room):
    time.sleep(0.8)
    return {"room": room, "pid": os.getpid()}


ACTIONS = {"demo_look": look}
''')
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ph.invoke, plugin_id, "demo_look", {"room": room}) for room in ("A", "B")]
        outs = [future.result(timeout=20) for future in futures]
    elapsed = time.monotonic() - started
    assert all(out["ok"] is True for out in outs), outs
    assert {out["result"]["room"] for out in outs} == {"A", "B"}       # 各拿各的参数
    assert len({out["result"]["pid"] for out in outs}) == 2            # 各起各的子进程
    assert elapsed < 3.0, elapsed                                       # 并发, 不是排队串行


def test_switch_off_never_starts_a_child(host, tmp_path, monkeypatch):
    plugin_id, plugin_dir = _installed(tmp_path, '''
from pathlib import Path

Path(__file__).with_name("imported.flag").write_text("yes", encoding="utf-8")


def look(room):
    return {"room": room}


ACTIONS = {"demo_look": look}
''')
    monkeypatch.delenv(ph.SWITCH_ENV, raising=False)
    out = ph.invoke(plugin_id, "demo_look", {"room": "x"})
    assert out["ok"] is False and out["error"]["code"] == "E_HOST_OFF"
    assert not (plugin_dir / "imported.flag").exists()  # 开关关着: 连子进程都没起, 更没 import 插件


def test_missing_entry_is_structured(host, tmp_path):
    plugin_id, plugin_dir = _installed(tmp_path, '''
def look(room):
    return {"room": room}


ACTIONS = {"demo_look": look}
''')
    (plugin_dir / "plugin.py").unlink()
    out = ph.invoke(plugin_id, "demo_look", {"room": "x"})
    assert out["ok"] is False and out["error"]["code"] == "E_ENTRY_MISSING"
    assert "plugin.py" in out["error"]["message"]

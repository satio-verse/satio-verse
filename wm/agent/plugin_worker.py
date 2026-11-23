"""插件动作执行子进程 (平台化第三步②).

父进程 (`wm/agent/plugin_host.py` 的 `invoke`) 用独立解释器起这个文件:

    python plugin_worker.py <插件目录> <动作名> <结果文件>

参数 JSON 从 stdin 读 (`{"params": {...}}`), 结果 JSON 写到第三个命令行参数给的文件:

    {"ok": true,  "result": {...}}                                  正常
    {"ok": false, "error": {"code": "...", "message": "..."}}       插件自己的问题

子进程只做一件事: 把插件目录和宿主仓库根放到 sys.path 最前, import 插件目录里的 `plugin.py`,
从 `ACTIONS` 取出动作并调用. 它不读登记表、不读审计、不 import 宿主的动作执行器; 插件自己的
异常只留类型和一句短消息 (不给 traceback), 父进程再统一包装成 5 键结果。

边界 (不夸大): 这不是操作系统沙箱 —— 子进程跟宿主是同一个用户、同样的文件/网络权限, 能碰什么
由插件代码自己决定. 它隔开的是"插件崩溃/挂死"和"宿主", 外加父进程侧的整段超时强杀. 插件自己
import 宿主模块 (比如 `wm.agent.governance`) 是允许的, filesorter 就靠它查黑名单.

行尾: 这个文件是新建的, 纯 LF。
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

ENTRY_NAME = "plugin.py"
ENTRY_ATTR = "ACTIONS"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _short(exc: BaseException) -> str:
    """异常 → "类型: 一句短消息". 不给 traceback (异常原文可能很长, 也可能带路径和凭据)。"""
    text = " ".join(str(exc).split())
    return (type(exc).__name__ + ": " + text[:200]).rstrip(": ")


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _fail(path: Path, code: str, message: str) -> int:
    _write(path, {"ok": False, "error": {"code": code, "message": message}})
    return 0


def main(argv) -> int:
    if len(argv) != 4:
        return 2
    plugin_dir, action, result_path = Path(argv[1]).resolve(), argv[2], Path(argv[3])
    sys.path.insert(0, str(REPO_ROOT))     # 插件 import wm.* (比如治理黑名单) 时用
    sys.path.insert(0, str(plugin_dir))    # 插件自己的同目录模块优先
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        params = payload.get("params") if isinstance(payload, dict) else None
        if not isinstance(params, dict):
            params = {}
    except (UnicodeDecodeError, ValueError, AttributeError):
        return _fail(result_path, "E_CRASH", "子进程收到的参数不是 JSON 对象")

    entry = plugin_dir / ENTRY_NAME
    if not entry.is_file():
        return _fail(result_path, "E_ENTRY_MISSING", "插件目录里没有 %s" % ENTRY_NAME)
    spec = importlib.util.spec_from_file_location("wm_plugin_child_%d" % os.getpid(), str(entry))
    if spec is None or spec.loader is None:
        return _fail(result_path, "E_ENTRY_IMPORT", "%s 不是能导入的 Python 文件" % ENTRY_NAME)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - 插件自己的错, 只留类型 + 一句短消息
        sys.modules.pop(spec.name, None)
        return _fail(result_path, "E_ENTRY_IMPORT", "导入 %s 失败：%s" % (ENTRY_NAME, _short(exc)))
    actions = getattr(module, ENTRY_ATTR, None)
    if not isinstance(actions, dict):
        return _fail(result_path, "E_ENTRY_MISSING",
                     "%s 里没有 %s 映射（要一个 {动作名: callable} 的对象）" % (ENTRY_NAME, ENTRY_ATTR))
    handler = actions.get(action)
    if not callable(handler):
        return _fail(result_path, "E_ACTION_MISSING",
                     "%s 的 ACTIONS 里没有可调用的动作 %s" % (ENTRY_NAME, action))
    try:
        result = handler(**params)
    except Exception as exc:  # noqa: BLE001 - 插件自己的错, 只留类型 + 一句短消息
        return _fail(result_path, "E_CRASH", "插件动作 %s 抛了异常：%s" % (action, _short(exc)))
    if not isinstance(result, dict):
        return _fail(result_path, "E_RESULT_TYPE", "动作没有按约定返回对象（%s）" % type(result).__name__)
    try:
        json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return _fail(result_path, "E_RESULT_NOT_JSON", "动作返回的值不是 JSON：%s" % _short(exc))
    _write(result_path, {"ok": True, "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""插件宿主的「装扩展」流程与动作执行器 (平台化第一、二切片).

第一切片的四件事继续不变: 把一个插件包从目录装进来、列出来、启用/停用、卸载。装的时候依次过三关:
`validate_plugin_manifest()` (清单自身)、`policy_issues(..., "third_party")` (来路政策)、
`find_conflicts()` (和已装插件的世界前缀/动作名冲突); 同一个 id 已装时拒装 (本版不覆盖安装)。
清单里自称的 `plugin.tier` 只登记成 `declared_tier` 用来显示; 真正给出的档 (`granted_tier`) 一律从最严的 `text`
起步, 要往上得显式 `grant_tier`。

第二切片 (上一版) 在这之上加了执行器和工具面:

- `grant_tier(plugin_id, tier, why)`: 显式提权, 只允许 text → panel → code (不允许提到 process, 这一版封顶 code)。
  每次真的提了往 `~/.worldmodel/plugins/audit.jsonl` 写一行审计 (谁、何时、从什么档到什么档、理由)。
- `invoke(plugin_id, action, params)`: 按清单 `plugin.world.actions` 核对动作和参数。
  `kind=query` 在 text 档就能调; `kind=act` 要 `granted_tier >= panel`, 否则拒 (需要你先授权)。
  参数只按清单声明的类型/必填/长度核对, 值一律当数据, 不做任何字符串求值。
  插件实现在插件目录的 `plugin.py`, 入口约定 `ACTIONS = {"<动作名>": callable}`, callable 收 `**params`、返回 dict;
  导入失败、动作缺失、抛异常都转成结构化错误 (只留异常 type + 一句短消息, 不给 traceback)。
  返回统一结构: {"ok", "action", "result", "error", "receipt_id"}。
- `tool_table()` / `tool_specs()` / `read_only(name)`: 给 `studio` 的只读清单 —— 已启用插件的动作出现在她的工具表里,
  名字就是清单里的动作名 `<prefix>_<动作动词>`, 描述用 `why`; 开关关着 / 没装 / 没启用时一条都不出现。

第三切片② (这一版) 把动作执行搬进子进程:

- `invoke()` 不再在宿主进程里 import 插件代码: 它用独立解释器起 `wm/agent/plugin_worker.py`, 参数 JSON 走 stdin,
  结果 JSON 写到临时文件; 插件的 stdout/stderr 直接丢掉, 不会堵管道.
- 整段执行有超时 (`WM_PLUGIN_TIMEOUT` 可调, 默认 10 秒), 到了就 kill 子进程并返回 `E_TIMEOUT`;
  子进程非零退出 / 没留下结果返回 `E_CRASH`; 插件动作自己抛异常由 worker 转成 `E_CRASH`
  (只留异常类型 + 一句短消息, 不给 traceback).
- 子进程只把插件目录和宿主仓库根放进 sys.path, 只 import 插件目录的 `plugin.py`; 不读登记表、不读审计、
  不 import 宿主的动作执行器. 插件自己 import 宿主模块 (例如治理黑名单) 仍然可以.
- 正常返回结构不变: {"ok", "action", "result", "error", "receipt_id"}。

第四切片 (这一版) 加回执核对 (插件自证 + 宿主如实记录):

- `invoke()` 的返回结构不变; 每次调用 (开关开着时) 往 `audit.jsonl` 追加一行审计: 插件 id、动作名、成败、
  错误码、回执编号, 以及 `verified` 三态 —— `true`=插件自证全过 / `false`=自证没过 / `null`=插件没报。
  审计只记结构和结论, 不抄插件返回的原文.
- 插件在 result 里报 `code=E_RECEIPT_MISMATCH` 时, 宿主把这次调用翻到失败侧 (`ok=false`, `error.code`
  就是这个码, 不归入 E_TIMEOUT/E_CRASH), 但仍保留 result (已成功的清单和回执) —— 否则 undo 没法用.
- 宿主**不**替插件核对世界、也不替它伪造 `verified`: 插件没给就记 `null`。这是插件自证, 不是独立审计。

第五切片 (这一版) 加宿主侧独立核对 (默认关, `WM_PLUGIN_HOST_VERIFY=on` 才开):

- 插件返回后, 宿主对**自己看得懂的**那部分世界再做一遍只读核对, 得到 `host_verified` 三态:
  `true`=宿主看过且对得上 / `false`=宿主看过但对不上 / `null`=宿主看不懂这个世界, 不做判断。
  只覆盖两类世界: (1) 文件整理类 —— `moved` / `restored` 里每条的目标路径必须在、源路径必须不在
  (只读 `os.lstat`, 不跟符号链接); (2) 记账类 —— 返回里的 `ledger_path` 必须落在这个插件自己的目录里
  才许读, 宿主自己去账本里找那条 id (add 类要在 / undo 类要不在)。路径越界 → 拒读, 记 `null`, 不是 `false`。
- 插件自证 (`verified`) 和宿主核对 (`host_verified`) **分开记**: 审计行两个都写, 返回值里也各是一个键。
  两者都说得出话却不一致时: 返回值多一条 `warnings` (`E_VERIFY_DISAGREE`), 审计行记这个码 —— 但**不**把这次
  调用判失败 (`ok` 仍按插件结果), 先观测。
- 调用成功后, 有回执的调用额外往同一个 `audit.jsonl` 追加一行结构化事件 (`action="event"`,
  `event="plugin.receipt"`): 项目里没有插件事件总线可接 (见下), 所以落在审计通道里。
- 边界 (别夸大): 宿主只核它能直接观测的这两类世界; 别的插件动作一律 `null`。这不是全量独立审计。

扫描清单 (建工具表) 一律只读 `wmworld.json`, 不 import 插件代码; `plugin.py` 只在 `invoke` 时才在子进程里 import。
授权闸门在父进程先过: text 档只放 query, act 要 panel 以上; 过不了闸门连子进程都不起.

不做 (写清楚, 不夸大): 这不是操作系统沙箱 —— 子进程和宿主同一个用户、同样的文件/网络权限, 能碰什么由插件
代码自己决定; 它隔开的是"插件崩溃/挂死"和"宿主", 加上超时强杀. 没有并发调用数上限 (同时调几个就起几个子进程),
没有插件事件总线 (第五切片的回执事件落在 `audit.jsonl` 的 `plugin.receipt` 行上, 没有总线可接),
也仍然没有接进 `ExtensionRegistry.create()`。

开关: `WM_PLUGIN_HOST=on` 才启用, 默认关。关着时所有操作都直接返回关着的结果, 不建目录、不读盘。
插件目录: `~/.worldmodel/plugins/<id>/` (`WM_PLUGIN_HOME` 可换), 每份清单就是 `wm/agent/plugin_manifest.py`
校验的那份契约 (知识库 `09 问题与待办/世界接口契约（2026-11-15）.md`)。
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import plugin_manifest as pm

SWITCH_ENV = "WM_PLUGIN_HOST"
HOME_ENV = "WM_PLUGIN_HOME"
MANIFEST_NAME = "wmworld.json"
REGISTRY_NAME = "registry.json"

# 这一版只有"用户从目录装进来"这一条来路; 自带插件 (bundled) 走仓库, 不经过这里。
SOURCE = "third_party"
# 不信任清单自称: 装进来的插件一律按 text 档登记 (宿主这一版不执行任何插件代码)。
GRANTED_TIER = "text"

MAX_FILES = 128
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
SKIP_PARTS = {"__pycache__"}

# ---- 第二切片: 动作执行器与工具面 ----
AUDIT_NAME = "audit.jsonl"   # 审计放在插件目录根: ~/.worldmodel/plugins/audit.jsonl
ENTRY_NAME = "plugin.py"     # 插件实现入口 (只有 invoke 时才会 import, 扫清单时不碰)
ENTRY_ATTR = "ACTIONS"       # 入口约定: ACTIONS = {"<动作名>": callable}
# 这一版允许授到的档. process 还没做 (没有独立进程和看守), 不能授; 只能往上提.
GRANTABLE_TIERS = ("text", "panel", "code")
TIER_ORDER = {"text": 0, "panel": 1, "code": 2}
ACT_TIER = "panel"           # kind=act 至少要授到这一档才能调
MAX_WHY = 500
# 本模块有 def list() (插件列表接口), 会遮住内置 list; 这里先留一份, 给 isinstance 用.
_BUILTIN_LIST = list

_OFF_REASON = "插件宿主没有打开：设置环境变量 WM_PLUGIN_HOST=on 才启用（默认关）"
_LOCK = threading.RLock()

# ---- 第三切片②: 动作在子进程里执行 ----
WORKER_NAME = "plugin_worker.py"          # 子进程入口 (和本文件同目录)
PACKAGE_ROOT = Path(__file__).resolve().parents[2]  # 宿主仓库根: 子进程要 import wm.* 时加进 sys.path
TIMEOUT_ENV = "WM_PLUGIN_TIMEOUT"
DEFAULT_TIMEOUT = 10.0
MIN_TIMEOUT = 0.1
MAX_TIMEOUT = 600.0
RESULT_NAME = "result.json"
# 子进程允许带回来的错误码; 别的都按 E_CRASH 处理, 不把子进程的话原样透传
CHILD_ERROR_CODES = frozenset({"E_ENTRY_MISSING", "E_ENTRY_IMPORT", "E_ACTION_MISSING",
                               "E_RESULT_TYPE", "E_RESULT_NOT_JSON", "E_CRASH"})
# 第四切片: 插件自证对不上时回在 result 里的码. 它不是子进程崩溃, 但这次动作算失败;
# 宿主只按插件报的码把结果翻到失败侧, 不替它核对、也不替它圆.
RECEIPT_MISMATCH_CODE = "E_RECEIPT_MISMATCH"

# ---- 第五切片: 宿主侧独立核对 (默认关) ----
# 打开后, 插件返回后宿主再对自己看得懂的世界做一次只读核对; 三态和插件的 verified 分开记.
VERIFY_ENV = "WM_PLUGIN_HOST_VERIFY"
VERIFY_DISAGREE_CODE = "E_VERIFY_DISAGREE"   # 插件说 true / 宿主说 false 时记的码 (只观测, 不判失败)
RECEIPT_EVENT = "plugin.receipt"             # 回执进审计通道时那行事件的名字
VERIFY_MAX_ITEMS = 200                       # 一次最多核对多少条文件记录 (插件可能报很长)
_UNSET = object()                            # 审计行里"这次没做宿主核对"的哨兵 (和 null 区分)


def switch_on() -> bool:
    """新的装扩展流程开没开。关着时下面四个操作一行盘都不碰。"""
    return os.environ.get(SWITCH_ENV, "off").strip().lower() in ("on", "1", "true", "yes")


def verify_on() -> bool:
    """宿主侧独立核对开没开 (`WM_PLUGIN_HOST_VERIFY=on` 才开, 默认关)。

    关着时 `invoke()` 的返回结构和审计行都和第五切片之前一模一样: 不多一个键, 不多一行盘。
    """
    return os.environ.get(VERIFY_ENV, "off").strip().lower() in ("on", "1", "true", "yes")


def home() -> Path:
    """插件目录 (环境变量 WM_PLUGIN_HOME 可覆盖)。只在开关打开以后才调用。"""
    raw = (os.environ.get(HOME_ENV) or "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".worldmodel" / "plugins"
    return base.resolve()


def _off() -> dict:
    return {"enabled": False, "reason": _OFF_REASON}


def _fail(reason: str, issues=None) -> dict:
    out = {"enabled": True, "ok": False, "reason": reason}
    if issues:
        out["issues"] = [{"path": i.path, "code": i.code, "message": i.message, "level": i.level} for i in issues]
    return out


def _inside(path: Path, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def _registry_path(root: Path) -> Path:
    return Path(root) / REGISTRY_NAME


def _load_registry(root: Path) -> dict:
    path = _registry_path(root)
    if not path.exists():
        return {"version": 1, "plugins": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("插件登记表坏了，拒绝对它做任何改动：%s (%s)" % (path, exc)) from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("plugins"), dict):
        raise ValueError("插件登记表格式不对，拒绝对它做任何改动：%s" % path)
    return data


def _save_registry(root: Path, state: dict) -> None:
    path = _registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    tmp = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _error_text(issues) -> str:
    rows = ["  - %s（%s）：%s" % (i.path or "（清单）", i.code, i.message) for i in issues if i.level == "error"]
    return "\n".join(rows)


def _collect_package(root: Path):
    """要复制哪些文件: [(相对路径元组, bytes)], 合计字节. 太大、链接、越出包目录都拒."""
    files, total = [], 0
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_PARTS and not d.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            entry = Path(base) / name
            rel = entry.relative_to(root)
            if entry.is_symlink() or not entry.is_file():
                raise ValueError("包里有不是普通文件的东西（链接/快捷方式）：%s" % rel.as_posix())
            target = entry.resolve()
            if not _inside(target, root):
                raise ValueError("包里的 %s 指向包目录外面" % rel.as_posix())
            size = entry.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError("文件 %s 有 %d 字节，超过单文件上限 %d" % (rel.as_posix(), size, MAX_FILE_BYTES))
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("插件包合计超过 %d 字节" % MAX_TOTAL_BYTES)
            files.append((rel.parts, entry.read_bytes()))
            if len(files) > MAX_FILES:
                raise ValueError("文件超过 %d 个" % MAX_FILES)
    return files, total


def _package_dir(raw) -> tuple:
    """校验用户给的包目录. 返回 (Path, None) 或 (None, 拒装理由)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, "要装的是哪个目录？请给出插件包目录（里面有 %s）" % MANIFEST_NAME
    try:
        src = Path(raw).expanduser().resolve(strict=True)
    except (OSError, ValueError):
        return None, "插件包目录不存在：%s" % (raw,)
    if not src.is_dir():
        return None, "插件包不是目录：%s" % src
    return src, None


def _public_entry(entry: dict) -> dict:
    return {key: entry.get(key) for key in ("id", "name", "version", "declared_tier", "granted_tier", "enabled", "installed_at", "files")}


def _installed_manifests(root: Path, state: dict) -> list:
    out = []
    for plugin_id in state.get("plugins", {}):
        if not isinstance(plugin_id, str) or not pm.ID.match(plugin_id):
            continue
        path = root / plugin_id / MANIFEST_NAME
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if isinstance(manifest, dict):
            out.append(manifest)
    return out


def install(package_dir) -> dict:
    """装一个插件包。返回 {"enabled": True, "ok": True, ...} 或 ok=False 加人能看懂的理由。"""
    if not switch_on():
        return _off()
    root = home()
    src, why = _package_dir(package_dir)
    if src is None:
        return _fail(why)
    if _inside(src, root):
        return _fail("不能从插件目录里面再装一次：%s" % src)
    manifest_path = src / MANIFEST_NAME
    if not manifest_path.is_file():
        return _fail("包里没有 %s（清单要和包放在同一层）：%s" % (MANIFEST_NAME, manifest_path))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return _fail("%s 读不出来或不是合法 JSON：%s" % (MANIFEST_NAME, exc))

    issues = pm.validate_plugin_manifest(manifest, package_dir=src)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        return _fail("清单不合格，拒装（%d 处）：\n%s" % (len(errors), _error_text(errors)), errors)
    if not isinstance(manifest.get("plugin") if isinstance(manifest, dict) else None, dict):
        return _fail("这是没有 plugin 块的老包，这一版插件宿主只收带 plugin 块的插件；老包仍走原来的扩展流程")
    warnings = [i for i in issues if i.level == "warn"]

    policy = pm.policy_issues(manifest, SOURCE)
    if pm.has_errors(policy):
        return _fail("来路不允许（%s），拒装（%d 处）：\n%s" % (
            "第三方" if SOURCE == "third_party" else SOURCE, len(policy), _error_text(policy)), policy)

    plugin_id = manifest["id"]
    plugin = manifest["plugin"]
    declared_tier = plugin.get("tier") if isinstance(plugin, dict) else None
    with _LOCK:
        try:
            state = _load_registry(root)
        except ValueError as exc:
            return _fail(str(exc))
        if plugin_id in state["plugins"]:
            old = state["plugins"].get(plugin_id) or {}
            return _fail("已经装了 id=%s 的插件（版本 %s）；这一版不覆盖安装，要换请先 uninstall" % (
                plugin_id, old.get("version") or "未知"))
        conflicts = pm.find_conflicts(manifest, _installed_manifests(root, state))
        if pm.has_errors(conflicts):
            return _fail("和已装插件冲突，拒装（%d 处）：\n%s" % (len(conflicts), _error_text(conflicts)), conflicts)

        try:
            files, total = _collect_package(src)
        except (OSError, ValueError) as exc:
            return _fail("插件包不合格，拒装：%s" % exc)

        dest = (root / plugin_id).resolve()
        if dest.parent != root:
            return _fail("插件 id 落到了插件目录外面：%s" % plugin_id)
        if dest.exists():
            return _fail("插件目录已经存在，拒装：%s" % dest)

        stamp = uuid.uuid4().hex
        stage = root / (".stage-" + stamp)
        installed_at = time.time()
        created = False
        try:
            root.mkdir(parents=True, exist_ok=True)
            stage.mkdir()
            for parts, data in files:
                target = stage.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            recheck = [i for i in pm.validate_plugin_manifest(manifest, package_dir=stage) if i.level == "error"]
            if recheck:
                raise ValueError("复制后自检没通过：\n" + _error_text(recheck))
            stage.rename(dest)
            created = True
            entry = {"id": plugin_id, "name": manifest.get("name"), "version": plugin.get("version"),
                     "declared_tier": declared_tier, "granted_tier": GRANTED_TIER, "enabled": False,
                     "installed_at": installed_at, "source": SOURCE, "files": [Path(*p).as_posix() for p, _ in files],
                     "bytes": total, "manifest": MANIFEST_NAME}
            state["plugins"][plugin_id] = entry
            _save_registry(root, state)
        except (OSError, ValueError) as exc:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            if created and dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            return _fail("装的时候出错了，已经清理，没留下半个插件：%s" % exc)

    return {"enabled": True, "ok": True, "id": plugin_id, "name": manifest.get("name"),
            "version": plugin.get("version"), "declared_tier": declared_tier, "granted_tier": GRANTED_TIER,
            "plugin_enabled": False, "installed_at": installed_at, "files": [Path(*p).as_posix() for p, _ in files],
            "warnings": [{"path": i.path, "code": i.code, "message": i.message} for i in warnings],
            "separate_permissions": pm.separate_permissions(manifest)}


def list() -> dict:  # noqa: A001 - 契约里这个操作就叫 list
    """列已装插件。装完默认是停着的。"""
    if not switch_on():
        return _off()
    root = home()
    try:
        state = _load_registry(root)
    except ValueError as exc:
        return _fail(str(exc))
    plugins = []
    for plugin_id in sorted(state["plugins"]):
        entry = state["plugins"][plugin_id]
        if not isinstance(entry, dict):
            continue
        item = _public_entry(entry)
        item["id"] = item.get("id") or plugin_id
        plugins.append(item)
    return {"enabled": True, "ok": True, "home": str(root), "plugins": plugins}


def set_enabled(plugin_id, enabled) -> dict:
    """启用/停用一个已装插件. 只改登记表, 不执行插件代码."""
    if not switch_on():
        return _off()
    if not isinstance(plugin_id, str) or not pm.ID.match(plugin_id):
        return _fail("插件 id 只能是小写字母、数字和 -，最多 48 个字符")
    if type(enabled) is not bool:
        return _fail("enabled 只能是 true / false")
    root = home()
    with _LOCK:
        try:
            state = _load_registry(root)
        except ValueError as exc:
            return _fail(str(exc))
        entry = state["plugins"].get(plugin_id)
        if not isinstance(entry, dict):
            return _fail("没有装这个插件：%s" % plugin_id)
        if entry.get("enabled") is enabled:
            return {"enabled": True, "ok": True, "id": plugin_id, "plugin_enabled": enabled, "changed": False}
        entry["enabled"] = enabled
        try:
            _save_registry(root, state)
        except OSError as exc:
            return _fail("启用状态没写进去：%s" % exc)
    return {"enabled": True, "ok": True, "id": plugin_id, "plugin_enabled": enabled, "changed": True}


def uninstall(plugin_id) -> dict:
    """卸载: 删插件目录和登记表里的那条. 目录已经不在时把登记表里的残留清掉."""
    if not switch_on():
        return _off()
    if not isinstance(plugin_id, str) or not pm.ID.match(plugin_id):
        return _fail("插件 id 只能是小写字母、数字和 -，最多 48 个字符")
    root = home()
    with _LOCK:
        try:
            state = _load_registry(root)
        except ValueError as exc:
            return _fail(str(exc))
        entry = state["plugins"].get(plugin_id)
        dest = (root / plugin_id).resolve()
        if dest.parent != root:
            return _fail("插件 id 落到了插件目录外面：%s" % plugin_id)
        if not isinstance(entry, dict) and not dest.exists():
            return _fail("没有装这个插件：%s" % plugin_id)
        removed = 0
        if dest.exists():
            if not _inside(dest, root):
                return _fail("插件目录超出了插件目录范围，拒绝删除：%s" % dest)
            try:
                removed = sum(1 for item in dest.rglob("*") if item.is_file())
                shutil.rmtree(dest)
            except OSError as exc:
                return _fail("插件目录没删干净：%s" % exc)
        state["plugins"].pop(plugin_id, None)
        try:
            _save_registry(root, state)
        except OSError as exc:
            return _fail("登记表没写进去：%s" % exc)
    return {"enabled": True, "ok": True, "id": plugin_id, "removed": True, "files_removed": removed}


# ---- 提权 (第二切片, 任务 A1) ----

def _audit_path(root: Path) -> Path:
    """审计文件: 插件目录根上的 audit.jsonl (谁、何时、从什么档到什么档、为什么)。"""
    return Path(root) / AUDIT_NAME


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _append_audit(root: Path, record: dict) -> Path:
    """追加一行审计. 先 flush 再 fsync: 掉电也不会只剩半行 (读回坏行时能只看前一行)。"""
    path = _audit_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _granted(entry) -> str:
    """登记表里的档; 坏了、没见过的一律按最严的 text 算。"""
    value = entry.get("granted_tier") if isinstance(entry, dict) else None
    return value if value in TIER_ORDER else GRANTED_TIER


def grant_tier(plugin_id, tier, *, why) -> dict:
    """显式提权: 只允许 text → panel → code, 不开放 process; 每次真的提了写一行审计。

    - 默认档是 text, 不调这个函数就一直是 text;
    - 只能往上提; 平级返回 changed=False (不写审计); 降档/收回这一版没有操作;
    - 审计写不进去时把档位退回原值并报错, 不留"提了但没记录"的状态。
    """
    if not switch_on():
        return _off()
    if not isinstance(plugin_id, str) or not pm.ID.match(plugin_id):
        return _fail("插件 id 只能是小写字母、数字和 -，最多 48 个字符")
    if tier not in GRANTABLE_TIERS:
        return _fail("档位只能往 text / panel / code 提；process 档这一版还没有（需要独立进程和看守），不能授")
    if not isinstance(why, str) or not why.strip() or len(why) > MAX_WHY:
        return _fail("提权要写清楚理由（why，1–%d 字），理由会记进审计" % MAX_WHY)
    reason = why.strip()
    root = home()
    with _LOCK:
        try:
            state = _load_registry(root)
        except ValueError as exc:
            return _fail(str(exc))
        entry = state["plugins"].get(plugin_id)
        if not isinstance(entry, dict):
            return _fail("没有装这个插件：%s" % plugin_id)
        current = _granted(entry)
        if tier == current:
            return {"enabled": True, "ok": True, "id": plugin_id, "granted_tier": current,
                    "previous_tier": current, "changed": False}
        if TIER_ORDER[tier] < TIER_ORDER[current]:
            return _fail("提权只能往上：现在已经是 %s 档，这一版没有降档或收回档位的操作" % current)
        entry["granted_tier"] = tier
        record = {"action": "grant_tier", "at": _now(), "ts": time.time(), "plugin_id": plugin_id,
                  "from": current, "to": tier, "why": reason, "by": "local_user"}
        try:
            _save_registry(root, state)
        except OSError as exc:
            entry["granted_tier"] = current
            return _fail("档位没写进登记表：%s" % exc)
        try:
            path = _append_audit(root, record)
        except OSError as exc:
            entry["granted_tier"] = current
            try:
                _save_registry(root, state)
            except OSError:
                pass
            return _fail("审计写不进去，提权没生效（已退回 %s 档）：%s" % (current, exc))
    return {"enabled": True, "ok": True, "id": plugin_id, "granted_tier": tier,
            "previous_tier": current, "changed": True, "why": reason, "audit_path": str(path)}


# ---- 只读清单: 扫 wmworld.json 建她的工具表 (第二切片, 任务 A3/B) ----

def _read_manifest(root: Path, plugin_id: str):
    """读已装插件的 wmworld.json. 返回 (manifest, None) 或 (None, 理由). 只读, 不 import 插件代码。"""
    path = Path(root) / plugin_id / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, "%s 读不出来（%s）" % (MANIFEST_NAME, type(exc).__name__)
    if not isinstance(data, dict):
        return None, "%s 不是对象" % MANIFEST_NAME
    return data, None


def _actions_of(manifest) -> list:
    """清单里的动作表. 只收格式对的条目 (清单装的时候已经校验过, 这里防的是之后被手改坏的)。"""
    plugin = manifest.get("plugin") if isinstance(manifest, dict) else None
    world = plugin.get("world") if isinstance(plugin, dict) else None
    actions = world.get("actions") if isinstance(world, dict) else None
    prefix = world.get("prefix") if isinstance(world, dict) else None
    out = []
    for action in actions if isinstance(actions, _BUILTIN_LIST) else []:
        if not isinstance(action, dict):
            continue
        name = action.get("name")
        if not isinstance(name, str) or not pm.ACTION_NAME.match(name):
            continue
        if isinstance(prefix, str) and not name.startswith(prefix + "_"):
            continue
        out.append(action)
    return out


def tool_table(reserved=(), *, read_only=False) -> dict:
    """已启用插件的动作 → {工具名: {"plugin", "action", "granted_tier"}}. 只读清单, 不 import。

    - 宿主开关关着 / 插件没启用 / 清单读不出来 → 这个插件一条都不出现;
    - kind=query 一律出现; kind=act 要 granted_tier >= panel, read_only=True (只读档) 时也不出现;
    - 和内置工具/直通工具重名时, 内置的赢 (reserved)。
    """
    if not switch_on():
        return {}
    root = home()
    try:
        state = _load_registry(root)
    except ValueError:
        return {}
    reserved = {name for name in (reserved or ()) if isinstance(name, str)}
    table = {}
    for plugin_id in sorted(state.get("plugins") or {}):
        if not isinstance(plugin_id, str) or not pm.ID.match(plugin_id):
            continue
        entry = state["plugins"].get(plugin_id)
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        manifest, _why = _read_manifest(root, plugin_id)
        if manifest is None:
            continue
        granted = _granted(entry)
        allow_act = TIER_ORDER[granted] >= TIER_ORDER[ACT_TIER]
        for action in _actions_of(manifest):
            name = action["name"]
            kind = action.get("kind")
            if kind not in ("query", "act") or name in table or name in reserved:
                continue
            if kind == "act" and (not allow_act or read_only):
                continue
            table[name] = {"plugin": plugin_id, "action": action, "granted_tier": granted}
    return table


_PARAM_SCHEMAS = {"string": {"type": "string"}, "int": {"type": "integer"},
                  "number": {"type": "number"}, "bool": {"type": "boolean"}}


def _params_schema(action) -> tuple:
    """动作参数 → (JSON Schema properties, required). 不合法的声明跳过, 不猜。"""
    props, required = {}, []
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    for name, spec in params.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            continue
        ptype = spec.get("type")
        if ptype == "enum":
            values = [value for value in (spec.get("values") or []) if isinstance(value, str)]
            schema = {"type": "string", "enum": values}
        elif ptype in _PARAM_SCHEMAS:
            schema = dict(_PARAM_SCHEMAS[ptype])
        else:
            continue
        if ptype == "string":
            max_len = spec.get("max_len")
            if isinstance(max_len, int) and not isinstance(max_len, bool) and max_len > 0:
                schema["maxLength"] = max_len
        props[name] = schema
        if spec.get("required") is True:
            required.append(name)
    return props, required


def tool_specs(reserved=(), *, read_only=False) -> list:
    """已启用插件的动作 → OpenAI 工具描述. 名字用清单里的动作名 (<prefix>_<动词>), 描述用 why。"""
    out = []
    for name, row in sorted(tool_table(reserved, read_only=read_only).items()):
        action = row["action"]
        props, required = _params_schema(action)
        out.append({"type": "function", "function": {
            "name": name, "description": str(action.get("why") or name)[:300],
            "parameters": {"type": "object", "properties": props, "required": required,
                           "additionalProperties": False}}})
    return out


def tool_names(reserved=(), *, read_only=False) -> set:
    return set(tool_table(reserved, read_only=read_only))


def read_only(name) -> bool:
    """这个工具名是不是插件宿主的只读查询动作 (kind=query). 不是插件工具返回 False。"""
    row = tool_table().get(name) if isinstance(name, str) else None
    return bool(row and row["action"].get("kind") == "query")


def find_tool(name, reserved=()):
    """工具名 → (插件 id, 动作清单) 或 (None, None). 给 studio 的调用分派用。"""
    row = tool_table(reserved).get(name) if isinstance(name, str) else None
    return (row["plugin"], row["action"]) if row else (None, None)


# ---- 执行器: 调插件动作 (第二切片, 任务 A2) ----

def _short_error(exc: BaseException) -> str:
    """异常 → "类型: 一句短消息". 不给 traceback: 异常原文可能很长, 也可能带路径和凭据。"""
    text = " ".join(str(exc).split())
    return (type(exc).__name__ + ": " + text[:200]).rstrip(": ")


def _result(ok, action, result=None, error=None, receipt_id=None, *, keep_result=False) -> dict:
    """统一 5 键返回. ok=false 时 result 默认置 None; 回执对不上是例外: 结果要留着给 undo 用。"""
    return {"ok": bool(ok), "action": action if isinstance(action, str) else "",
            "result": result if (ok or keep_result) else None, "error": None if ok else (error or {}),
            "receipt_id": receipt_id}


def _fail_result(action, code, message) -> dict:
    return _result(False, action, error={"code": code, "message": message})


def _check_params(action, params):
    """按清单声明的 params 核对入参. 返回 (值, None) 或 (None, (code, message))。参数值只当数据。"""
    declared = action.get("params") if isinstance(action.get("params"), dict) else {}
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return None, ("E_PARAMS", "参数要是一个对象")
    extra = sorted(str(key) for key in set(params) - set(declared))
    if extra:
        return None, ("E_PARAM_UNKNOWN", "清单里没有这些参数：" + "、".join(extra))
    clean = {}
    for name, spec in declared.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            continue
        if name not in params:
            if spec.get("required") is True:
                return None, ("E_PARAM_REQUIRED", "缺少必填参数：" + name)
            continue
        value = params[name]
        ptype = spec.get("type")
        if ptype == "string":
            if not isinstance(value, str):
                return None, ("E_PARAM_TYPE", "参数 %s 要是字符串" % name)
            max_len = spec.get("max_len")
            if isinstance(max_len, int) and not isinstance(max_len, bool) and len(value) > max_len:
                return None, ("E_PARAM_LENGTH", "参数 %s 太长（最多 %d 字）" % (name, max_len))
        elif ptype == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return None, ("E_PARAM_TYPE", "参数 %s 要是整数" % name)
        elif ptype == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, ("E_PARAM_TYPE", "参数 %s 要是数字" % name)
        elif ptype == "bool":
            if not isinstance(value, bool):
                return None, ("E_PARAM_TYPE", "参数 %s 要是 true / false" % name)
        elif ptype == "enum":
            values = [item for item in (spec.get("values") or []) if isinstance(item, str)]
            if not isinstance(value, str) or value not in values:
                return None, ("E_PARAM_VALUE", "参数 %s 只能是：" % name + " / ".join(values))
        else:
            return None, ("E_PARAM_SCHEMA", "清单里参数 %s 的类型不对" % name)
        clean[name] = value
    return clean, None


def _entry_action_names(path: Path):
    """只读地用 ast 看 plugin.py 顶层 `ACTIONS = {...}` 的键; 不执行任何代码. 没找到字面量返回 None。"""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        target = value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target == ENTRY_ATTR and isinstance(value, ast.Dict):
            return {key.value for key in value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    return None


def _timeout_seconds() -> float:
    """动作超时 (秒): WM_PLUGIN_TIMEOUT 可调, 默认 10, 夹在 0.1..600 之间; 写坏了用默认。"""
    raw = (os.environ.get(TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    if value != value or value in (float("inf"), float("-inf")):
        return DEFAULT_TIMEOUT
    return min(max(value, MIN_TIMEOUT), MAX_TIMEOUT)


def _worker_path() -> Path:
    return PACKAGE_ROOT / "wm" / "agent" / WORKER_NAME


def _run_entry(plugin_dir: Path, action: str, params: dict):
    """在子进程里 import 插件目录的 plugin.py 并调一个动作. 返回 (payload, error)。

    payload 是 worker 留下的 JSON 对象 ({"ok": ..., "result"/"error": ...}); error 是宿主侧错误 (code, message)。
    超时 → 杀掉子进程返回 E_TIMEOUT; 非零退出 / 没留下结果 / 结果读不出来 → E_CRASH (不给 traceback)。
    临时目录和子进程句柄无论如何都收干净。
    """
    timeout = _timeout_seconds()
    worker = _worker_path()
    if not worker.is_file():
        return None, ("E_ENTRY_MISSING", "宿主少了子进程执行器 %s" % WORKER_NAME)
    try:
        payload = json.dumps({"params": params}, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return None, ("E_PARAMS", "参数不是 JSON：%s" % _short_error(exc))
    work = Path(tempfile.mkdtemp(prefix="wm-plugin-"))
    result_path = work / RESULT_NAME
    try:
        try:
            proc = subprocess.Popen([sys.executable, str(worker), str(plugin_dir), action, str(result_path)],
                                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, cwd=str(plugin_dir))
        except OSError as exc:
            return None, ("E_CRASH", "子进程起不来：%s" % _short_error(exc))
        try:
            proc.communicate(input=payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return None, ("E_TIMEOUT", "插件动作超时（超过 %g 秒），子进程已杀掉" % timeout)
        raw = result_path.read_text(encoding="utf-8") if result_path.is_file() else None
        if raw is None:
            if proc.returncode:
                return None, ("E_CRASH", "插件动作子进程异常退出（exit=%s）" % proc.returncode)
            return None, ("E_CRASH", "插件动作子进程没有留下结果")
        try:
            data = json.loads(raw)
        except ValueError:
            return None, ("E_CRASH", "插件动作子进程留下的结果读不出来")
        if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
            return None, ("E_CRASH", "插件动作子进程的结果格式不对")
        return data, None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _receipt_id(result):
    """act 的回执编号: 插件返回里的 receipt_id / receipt (字符串). 这一版只透传, 不核对事件。"""
    if not isinstance(result, dict):
        return None
    for key in ("receipt_id", "receipt"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value[:200]
    return None


def _verified_state(result):
    """插件回执自证的三态: True=全过 / False=没过 / None=插件没报 (宿主不替它编).

    verified 必须是 {checked:int, ok:int}; ok==checked 才算全过 (checked=0 也算: 没有声称动过东西)。
    """
    if not isinstance(result, dict) or "verified" not in result:
        return None
    verified = result.get("verified")
    if not isinstance(verified, dict):
        return False
    checked, ok = verified.get("checked"), verified.get("ok")
    for value in (checked, ok):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False
    return ok == checked


# ---- 第五切片: 宿主自己核对 (只读; 看不懂就 None) ----

def _path_present(path) -> bool:
    """只读 stat: 这个路径在不在. 用 `os.lstat`, 不跟随符号链接 (插件说这里有东西, 宿主只看这一层)。

    只 stat, 不 open、不遍历、不写; 路径来自插件返回, 一律当数据。
    """
    if not isinstance(path, str) or not path.strip():
        return False
    try:
        os.lstat(path)
    except (OSError, ValueError):
        return False
    return True


def _ledger_check(result: dict):
    """看插件返回里要对账本的哪条 id 核对什么: ("present"/"absent", id), 判断不了一律 None。"""
    entry = result.get("entry")
    if isinstance(entry, dict):
        identifier = entry.get("id")
        if isinstance(identifier, str) and identifier.strip():
            return "present", identifier       # 记账: 这一条要在账本里
    entry_id = result.get("entry_id")
    if isinstance(entry_id, str) and entry_id.strip():
        return "absent", entry_id              # 撤销: 这一条要不在账本里
    identifier = result.get("id")
    if (isinstance(identifier, str) and identifier.strip()
            and "removed" not in result):
        return "present", identifier
    return None


def _verify_ledger(plugin_dir: Path, result: dict):
    """记账类: 宿主自己读账本, 核对那条 id 在不在.

    账本路径是插件返回里的 `ledger_path`; 它必须落在这个插件自己的目录里才许读 ——
    不让插件指定任意路径让宿主去读。越界 / 相对路径 / 读不出来 / 读不懂 → None (不猜)。
    """
    raw = result.get("ledger_path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    check = _ledger_check(result)
    if check is None:
        return None
    mode, entry_id = check
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute() or not _inside(path, plugin_dir):
            return None
    except (OSError, ValueError):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            return None                        # 账本读不懂: 不做判断
        if not isinstance(row, dict):
            return None
        if row.get("id") == entry_id:
            return mode == "present"
    return mode == "absent"


def _verify_files(items, src_key: str, dst_key: str):
    """文件整理类: 每条的目标路径必须在、源路径必须不在 (只读 lstat, 不跟符号链接)。"""
    if not isinstance(items, _BUILTIN_LIST):
        return None
    if not items:
        return True                            # 没声称动过东西: 和 checked=0 一致
    for item in items[:VERIFY_MAX_ITEMS]:
        if not isinstance(item, dict):
            return None
        src, dst = item.get(src_key), item.get(dst_key)
        if not isinstance(src, str) or not src.strip():
            return None
        if not isinstance(dst, str) or not dst.strip():
            return None
        if not _path_present(dst) or _path_present(src):
            return False
    return True


def _host_verify(plugin_dir: Path, result: dict):
    """宿主自己核对一次: True=对得上 / False=对不上 / None=看不懂这个世界 (不做判断)。"""
    if not isinstance(result, dict):
        return None
    try:
        if isinstance(result.get("ledger_path"), str):
            return _verify_ledger(plugin_dir, result)
        moved = result.get("moved")
        if isinstance(moved, _BUILTIN_LIST):
            return _verify_files(moved, "src", "dst")
        restored = result.get("restored")
        if isinstance(restored, _BUILTIN_LIST):
            return _verify_files(restored, "from", "to")
    except (OSError, ValueError):
        return None
    return None


def _verify_invoke(plugin_dir: Path, out: dict) -> dict:
    """给一次调用补上 host_verified; 和插件自证不一致时加一条 warning (不翻 ok)。"""
    result = out.get("result") if isinstance(out.get("result"), dict) else None
    host_state = _host_verify(plugin_dir, result) if result is not None else None
    plugin_state = _verified_state(result)
    enriched = dict(out)
    enriched["host_verified"] = host_state
    if host_state is not None and plugin_state is not None and host_state != plugin_state:
        warnings = _BUILTIN_LIST(enriched.get("warnings") or [])
        warnings.append({"code": VERIFY_DISAGREE_CODE,
                         "message": "插件自证和宿主核对不一致：插件说 %s，宿主看到 %s" % (
                             plugin_state, host_state),
                         "plugin_verified": plugin_state,
                         "host_verified": host_state})
        enriched["warnings"] = warnings
    return enriched


def _audit_invoke(root: Path, out: dict, plugin_id, *, host_verified=_UNSET) -> Path:
    """一次 invoke 一行审计: 只记结构和结论 (verified 三态), 不抄插件返回的原文。

    `host_verified` 由调用方传: 传了才写这个键 (宿主核对开着的那些行), 没传就保持老审计行的样子。
    """
    error = out.get("error") if isinstance(out.get("error"), dict) else {}
    record = {"action": "invoke", "at": _now(), "ts": time.time(),
              "plugin_id": plugin_id[:120] if isinstance(plugin_id, str) else "",
              "tool": out.get("action") if isinstance(out.get("action"), str) else "",
              "ok": out.get("ok") is True,
              "error_code": error.get("code"),
              "receipt_id": out.get("receipt_id"),
              "verified": _verified_state(out.get("result")),
              "by": "local_user"}
    if host_verified is not _UNSET:
        record["host_verified"] = host_verified if isinstance(host_verified, bool) else None
        record["warnings"] = [item.get("code") for item in out.get("warnings") or []
                              if isinstance(item, dict) and item.get("code")]
    return _append_audit(Path(root), record)


def _audit_receipt(root: Path, out: dict, plugin_id) -> Path:
    """一次成功调用的回执进事件流: 现有审计 jsonl 里追加一行结构化事件 (没有插件事件总线可接)。

    只记结构 (谁、哪个动作、回执编号、两种核对结论), 不抄插件返回的原文和用户数据。
    """
    result = out.get("result") if isinstance(out.get("result"), dict) else {}
    record = {"action": "event", "event": RECEIPT_EVENT, "at": _now(), "ts": time.time(),
              "plugin_id": plugin_id[:120] if isinstance(plugin_id, str) else "",
              "tool": out.get("action") if isinstance(out.get("action"), str) else "",
              "receipt_id": out.get("receipt_id"),
              "verified": _verified_state(result),
              "host_verified": out.get("host_verified") if isinstance(out.get("host_verified"), bool) else None,
              "by": "local_user"}
    return _append_audit(Path(root), record)


def _invoke(plugin_id, action, params=None) -> dict:
    """调一个已启用插件的动作. 返回统一结构 {"ok", "action", "result", "error", "receipt_id"}。

    - 开关关着 / 插件没装 / 没启用 / 清单里没有这个动作 → 拒, 给理由;
    - kind=query 在 text 档就能调; kind=act 要 granted_tier >= panel, 否则拒 (需要你先授权);
    - 参数只按清单声明的 params 核对 (类型/必填/多余都拒), 值一律当数据, 不做任何字符串求值;
    - 插件动作在子进程里执行 (plugin_worker.py): 参数和结果走 JSON, 超时 (WM_PLUGIN_TIMEOUT, 默认 10 秒)
      到了就杀掉子进程返回 E_TIMEOUT; 子进程崩了/没留下结果返回 E_CRASH; 插件自己抛异常也是 E_CRASH
      (只留异常类型 + 一句短消息, 不给 traceback)。
    """
    if not switch_on():
        return _result(False, action, error={"code": "E_HOST_OFF", "message": _OFF_REASON})
    if not isinstance(plugin_id, str) or not pm.ID.match(plugin_id):
        return _fail_result(action, "E_PLUGIN_ID", "插件 id 只能是小写字母、数字和 -，最多 48 个字符")
    if not isinstance(action, str) or not pm.ACTION_NAME.match(action):
        return _fail_result(action, "E_ACTION_NAME", "动作名要是清单里的 <前缀>_<动词>")
    root = home()
    with _LOCK:
        try:
            state = _load_registry(root)
        except ValueError as exc:
            return _fail_result(action, "E_REGISTRY", str(exc))
        entry = state["plugins"].get(plugin_id)
        if not isinstance(entry, dict):
            return _fail_result(action, "E_NOT_INSTALLED", "没有装这个插件：%s" % plugin_id)
        enabled = entry.get("enabled") is True
        granted = _granted(entry)
    if not enabled:
        return _fail_result(action, "E_NOT_ENABLED", "插件没有启用：先把它打开再调")
    manifest, why = _read_manifest(root, plugin_id)
    if manifest is None:
        return _fail_result(action, "E_MANIFEST", "插件清单读不出来：%s" % why)
    declared = next((item for item in _actions_of(manifest) if item.get("name") == action), None)
    if declared is None:
        return _fail_result(action, "E_NO_ACTION", "清单里没有这个动作：%s" % action)
    kind = declared.get("kind")
    if kind not in ("query", "act"):
        return _fail_result(action, "E_ACTION_KIND", "清单里这个动作的 kind 不对：%s" % kind)
    if kind == "act" and TIER_ORDER[granted] < TIER_ORDER[ACT_TIER]:
        return _fail_result(action, "E_NEEDS_GRANT",
                            "需要你先授权：kind=act 的动作要 granted_tier 到 panel 以上（现在是 %s 档）；"
                            "请先用 grant_tier 显式提权，理由会记进审计" % granted)
    clean, bad = _check_params(declared, params)
    if bad is not None:
        return _fail_result(action, bad[0], bad[1])
    plugin_dir = (root / plugin_id).resolve()
    if plugin_dir.parent != root.resolve() or not _inside(plugin_dir, root):
        return _fail_result(action, "E_PLUGIN_PATH", "插件目录落到了插件目录外面：%s" % plugin_id)
    # 先只读地看一眼 ACTIONS 字面量: 没声明这个动作时, 连 import 都不用做
    names = _entry_action_names(plugin_dir / ENTRY_NAME)
    if names is not None and action not in names:
        return _fail_result(action, "E_ACTION_MISSING", "%s 的 ACTIONS 里没有这个动作" % ENTRY_NAME)
    payload, error = _run_entry(plugin_dir, action, clean)
    if error is not None:
        return _fail_result(action, error[0], error[1])
    if payload.get("ok") is not True:
        child = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = child.get("code")
        if code not in CHILD_ERROR_CODES:
            code = "E_CRASH"
        return _fail_result(action, code, str(child.get("message") or "插件动作失败")[:300])
    result = payload.get("result")
    if not isinstance(result, dict):
        return _fail_result(action, "E_RESULT_TYPE", "动作没有按约定返回对象（%s）" % type(result).__name__)
    try:
        json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        return _fail_result(action, "E_RESULT_NOT_JSON", "动作返回的值不是 JSON：%s" % _short_error(exc))
    receipt_id = _receipt_id(result)
    if result.get("ok") is False and result.get("code") == RECEIPT_MISMATCH_CODE:
        message = str(result.get("message") or result.get("reason") or "插件回执核对没通过")[:300]
        return _result(False, action, result=result,
                       error={"code": RECEIPT_MISMATCH_CODE, "message": message},
                       receipt_id=receipt_id, keep_result=True)
    return _result(True, action, result=result, receipt_id=receipt_id)


def _plugin_dir_for(plugin_id):
    """已装插件的目录 (校验过在插件目录根下面); 名字/位置不对返回 None。"""
    if not isinstance(plugin_id, str) or not pm.ID.match(plugin_id):
        return None
    root = home()
    candidate = (root / plugin_id).resolve()
    if candidate.parent != root.resolve() or not _inside(candidate, root):
        return None
    return candidate


def invoke(plugin_id, action, params=None) -> dict:
    """调一个已启用插件的动作 (对外入口; 核对、执行、转码都在 _invoke 里)。

    - 返回统一结构 {"ok", "action", "result", "error", "receipt_id"};
    - 开关开着时, 每次调用往插件目录根追加一行 `audit.jsonl`: 插件 id、动作名、成败、错误码、
      回执编号, 以及 `verified` 三态 (true=插件自证全过 / false=自证没过 / null=插件没报);
      审计只记结构和结论, 不抄插件返回的原文;
    - 插件在 result 里报 `E_RECEIPT_MISMATCH` 时, 这次调用翻到失败侧, 但仍保留 result
      (已成功的清单和回执), 这样 undo 还能用;
    - 第五切片 (默认关, `WM_PLUGIN_HOST_VERIFY=on` 才开): 返回里多一个 `host_verified` 三态
      (true=宿主自己看过且对得上 / false=对不上 / null=宿主看不懂这个世界), 和插件的 `verified` 分开;
      两者都说得出话却相反时, 返回里多一条 `warnings` (`E_VERIFY_DISAGREE`), 审计行两个都记 ——
      `ok` 仍按插件结果, 不把这种插件直接判失败 (先观测);
    - 第五切片的回执进事件流: 成功的、带回执编号的调用, 在审计 jsonl 里再追加一行
      `{"action": "event", "event": "plugin.receipt", ...}` (项目里没有插件事件总线可接);
    - 宿主不替插件伪造 `verified`; 审计写失败不影响这次调用的结果。
    """
    out = _invoke(plugin_id, action, params)
    if not switch_on():
        return out
    root = home()
    host_verified = _UNSET
    if verify_on():
        plugin_dir = _plugin_dir_for(plugin_id)
        if plugin_dir is None:
            out = dict(out)
            out["host_verified"] = None
        else:
            out = _verify_invoke(plugin_dir, out)
        host_verified = out.get("host_verified")
    try:
        _audit_invoke(root, out, plugin_id, host_verified=host_verified)
    except OSError:
        pass
    if verify_on() and out.get("ok") is True and out.get("receipt_id"):
        try:
            _audit_receipt(root, out, plugin_id)
        except OSError:
            pass
    return out


# ---- 第六切片: 只读的插件宿主状态 (给工作台看; 不执行插件代码) ----

STATUS_RECENT = 5
STATUS_AUDIT_BYTES = 256 * 1024


def _audit_tail(root: Path):
    """读审计末尾一小段 (最多 STATUS_AUDIT_BYTES 字节) 里的行; 坏行跳过. 返回 (rows, partial)。"""
    path = _audit_path(root)
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > STATUS_AUDIT_BYTES:
                stream.seek(size - STATUS_AUDIT_BYTES)
                stream.readline()              # 从完整的一行开头读起
            raw = stream.read()
    except OSError:
        return [], False
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue                           # 半行/坏行: 跳过, 不猜
        if isinstance(row, dict):
            rows.append(row)
    return rows, size > STATUS_AUDIT_BYTES


def _recent_calls(rows) -> dict:
    """按 plugin_id 聚合审计里的 invoke: 调用次数、成/败、最近几条. 只认结构对的行。"""
    stats = {}
    for row in rows:
        if row.get("action") != "invoke":
            continue
        plugin_id = row.get("plugin_id")
        if not isinstance(plugin_id, str) or not plugin_id:
            continue
        item = stats.setdefault(plugin_id, {"calls": 0, "ok": 0, "failed": 0, "last": []})
        item["calls"] += 1
        if row.get("ok") is True:
            item["ok"] += 1
        else:
            item["failed"] += 1
        item["last"].append({"at": row.get("at"), "tool": row.get("tool"), "ok": row.get("ok"),
                             "error_code": row.get("error_code"), "verified": row.get("verified"),
                             "host_verified": row.get("host_verified")})
    for item in stats.values():
        item["last"] = item["last"][-STATUS_RECENT:]
    return stats


def _status_actions(root: Path, plugin_id: str):
    """动作清单: 名字/种类/理由来自清单; 是否真写在 plugin.py 的 ACTIONS 字面量里用 ast 对一遍 (不执行代码)。"""
    manifest, why = _read_manifest(root, plugin_id)
    if manifest is None:
        return [], why
    entry_names = _entry_action_names(Path(root) / plugin_id / ENTRY_NAME)
    rows = []
    for action in _actions_of(manifest):
        name = action["name"]
        rows.append({"name": name, "kind": action.get("kind"), "why": action.get("why"),
                     "in_entry": (name in entry_names) if isinstance(entry_names, set) else None})
    return rows, None


def status(plugin_id=None) -> dict:
    """只读: 已装插件、权限档、动作清单、最近几次调用. 不 import / 不执行插件代码。

    - 宿主开关关着 → {"enabled": False, ...} (工作台据此一个元素都不显示);
    - 只看登记表、清单、插件.py 的 ACTIONS 字面量和审计文件: 停用的插件也列出来, 标成停用;
    - 登记表坏了 → ok=False + 理由 (不抛异常)。
    """
    if not switch_on():
        return _off()
    if plugin_id is not None and (not isinstance(plugin_id, str) or not pm.ID.match(plugin_id)):
        return _fail("插件 id 只能是小写字母、数字和 -，最多 48 个字符")
    root = home()
    try:
        state = _load_registry(root)
    except ValueError as exc:
        return _fail(str(exc))
    if plugin_id is not None and not isinstance(state["plugins"].get(plugin_id), dict):
        return _fail("没有装这个插件：%s" % plugin_id)
    rows, partial = _audit_tail(root)
    recent = _recent_calls(rows)
    plugins = []
    for pid in sorted(state["plugins"]):
        if plugin_id is not None and pid != plugin_id:
            continue
        entry = state["plugins"].get(pid)
        if not isinstance(entry, dict):
            continue
        item = _public_entry(entry)
        item["id"] = item.get("id") or pid
        item["granted_tier"] = _granted(entry)
        item["source"] = entry.get("source")
        actions, why = _status_actions(root, pid)
        item["actions"] = actions
        item["actions_source"] = ENTRY_NAME
        if why:
            item["manifest_error"] = why
        item["recent"] = recent.get(pid, {"calls": 0, "ok": 0, "failed": 0, "last": []})
        plugins.append(item)
    return {"enabled": True, "ok": True, "home": str(root), "verify": verify_on(),
            "plugins": plugins, "recent_partial": partial}

"""Visible, editable constraints shared by every workbench permission mode.

Rules are an application authorization layer, not an OS sandbox: literal command/path
checks cannot infer the behaviour of arbitrary scripts or opaque third-party tools.
Natural-language instructions are explicitly advisory. The assistant may add enabled
constraints, but only the user-facing control plane may edit, disable or remove them.
"""
from __future__ import annotations

import base64
import binascii
import copy
import fnmatch
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import policy as P

KINDS = {"tool", "path", "command", "domain", "instruction"}
MAX_RULES = 256
MAX_AUDIT = 1000
MAX_FILE_BYTES = 2_000_000
MAX_ARGUMENT_NODES = 512
MAX_ARGUMENT_DEPTH = 8
MAX_ARGUMENT_CHARS = 200_000
MAX_REVISION = (1 << 53) - 1  # Exact in both JSON/JavaScript and native readers.
# 1: one-time addition of delivery/run evidence write protection.
# 2 (2026-11-07): paths recorded inside an MSIX-redirected process (…\Packages\<pkg>\LocalCache\Roaming\…) are rewritten to the
#    real location (policy.unvirtualize; the pet settings/token rules had never matched the real files), plus default rules for
#    the new hooks.json (agent hooks) and the user's global AGENTS.md (write).
# 3 (2026-11-11, 插件宿主第 0 步): 扩展登记表、扩展工具快照、扩展审计记录、她自己写的 MCP 的名单加写保护 (任务能用写文件的工具
#    直接改它们, 等于自己决定哪个版本的扩展开着、哪些扩展工具免问直接调、宿主起哪些程序), 以管理员身份运行 (runas、sudo、
#    Start-Process -Verb ...) 的命令进黑名单 (完全授权档只认黑名单, 以前这条路是通的).
# 4 (2026-11-11, 插件宿主第 0 步第三小步): ~/.worldmodel 下服务端自己写的三份状态 (asks.json 等你点头的申请、
#    workbench-threads.json 派到工作台的事、qq-digest.json QQ 攒着的重要消息) 加写保护 (任务能直接写它们, 就能伪造申请卡片、
#    派活的线和引用编号、QQ 消息).
# 5 (2026-11-17, S1 心跳): ~/.worldmodel/heartbeat/ 加写保护 —— 平时记录、抽屉、当天额度、封存链
#    都是"她这些天怎么过的"的账本, 任务能直接改就能伪造经过、把额度改成没花过.
# 6 (2026-11-19, 平台化第三步①): ~/.worldmodel/plugins/ 整目录加写保护 —— 第三方插件的登记表、装进来的包、回执和审计都在这里;
#    任务能直接写它, 就等于绕过安装与授权流程自己塞一个插件进来, 或者改回执抹掉自己做过的事.
DEFAULT_RULES_VERSION = 6
_PERSISTED_RULE_FIELDS = {"id", "source", "kind", "value", "enabled", "operations"}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
# 2026-11-08: _read() 每次都把每条规则重新 _validate (路径规则要 Path.resolve, 打包应用里还要走一层重定向), 而每一步拼系统提示、
# 判只读都要读好几次; 一个多智能体测试里核对了 2726 次、6.6 秒, 子任务那组测试因此超时. 文件内容一字不差且不到 READ_CACHE_S 秒,
# 就用上次核对过的结果 (存成 JSON 文本, 每次给一份新的, 调用方改了也不影响缓存); 内容一变就重新核对; 到点也整份重核一次,
# 免得目录链接这类文件系统的变化一直用旧的解析结果.
READ_CACHE_S = 30.0
_READ_CACHE: dict[str, tuple[bytes, float, str]] = {}
_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_ENV = re.compile(r"\$env:([A-Za-z_][A-Za-z_0-9]*)", re.IGNORECASE)
_TILDE = re.compile(r"(?<![\w.$~-])~(?=[\\/]|$)")  # 命令里单独的 ~ (PowerShell 认作家目录); ASUS~1、HEAD~1 这样的不算
_PATH_FIELDS = {"path", "paths", "file", "files", "filename", "file_path", "filepath", "directory", "directories",
                "cwd", "workdir", "destination", "source", "target", "root", "folder", "folders"}


def _text(value, name: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是文字")
    value = value.strip()
    if (required and not value) or len(value) > limit or "\x00" in value:
        raise ValueError(f"{name} 不能为空或超过 {limit} 个字符" if required else f"{name} 格式无效或超过 {limit} 个字符")
    return value


def _host(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    if value.startswith("*."):
        value = value[2:]
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("域名必须是有效的 http/https 地址或主机名")
        value = parsed.hostname.rstrip(".")
    if not value or any(c in value for c in "/\\@?#: "):
        raise ValueError("域名只填写主机名，不含路径或端口")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("域名格式无效") from exc
    labels = value.split(".")
    if len(value) > 253 or any(not label or len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in labels):
        raise ValueError("域名格式无效")
    return value


def _path(value: str, workspace: Path) -> Path:
    expanded = _ENV.sub(lambda m: os.environ.get(m[1], m[0]), os.path.expandvars(value.strip().strip('"')))
    p = Path(expanded).expanduser()
    return (p if p.is_absolute() else workspace / p).resolve()


def _strings(value, *, paths_only: bool = False, depth: int = 0, budget: list | None = None, field: str = ""):
    """Fully inspect a bounded argument tree, or reject it instead of omitting tails."""
    if budget is None:
        budget = [MAX_ARGUMENT_NODES, MAX_ARGUMENT_CHARS]
    if depth > MAX_ARGUMENT_DEPTH or budget[0] <= 0:
        raise ValueError("工具参数超出约束检查限额（最多 512 个节点、8 层），未执行")
    budget[0] -= 1
    if isinstance(value, str):
        budget[1] -= len(value)
        if budget[1] < 0:
            raise ValueError("工具参数超出约束检查限额（文字合计最多 20 万字符），未执行")
        if not paths_only or field.lower() in _PATH_FIELDS:
            yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > MAX_ARGUMENT_CHARS:
                raise ValueError("工具参数字段名无效或超出约束检查限额，未执行")
            yield from _strings(item, paths_only=paths_only, depth=depth + 1, budget=budget, field=key)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, paths_only=paths_only, depth=depth + 1, budget=budget, field=field)
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError("工具参数包含无法完整检查的值，未执行")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("工具参数包含无效数字，未执行")


def _literal(value: str) -> str:
    return " ".join(value.casefold().split())


def _command_contains(command: str, value: str) -> bool:
    """Literal phrase with token edges: 'format' must not ban Format-Table."""
    word = lambda char: char.isalnum() or char in "_.-"
    start = 0
    while (at := command.find(value, start)) >= 0:
        end = at + len(value)
        left = at == 0 or not word(value[0]) or not word(command[at - 1])
        right = end == len(command) or not word(value[-1]) or not word(command[end])
        if left and right:
            return True
        start = at + 1
    return False


# 命令类约束只逐字匹配, 有几种写法能让同一条命令对不上: cmd 的 ^、PowerShell 的 ` 和夹在词中间的引号不改变命令, 却能把
# runas 拆成 r^unas; powershell -EncodedCommand 把整条命令变成 Base64. 匹配前先还原成几份文字各对一遍, 任意一份命中就算命中
# (只会多拦, 不会少拦). 套一层 cmd /c、powershell -Command 不用还原: 里面的词还在原文里.
_NOISE = str.maketrans("", "", "`^\"'")
_BASE64 = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
MAX_COMMAND_VIEWS = 8


def _plausible(text: str) -> bool:
    return bool(text) and sum(ch.isprintable() or ch in "\r\n\t" for ch in text) * 10 >= len(text) * 9


def _decoded_texts(command: str) -> list[str]:
    """命令里藏着的 Base64 文字 (powershell -EncodedCommand 是 UTF-16LE 的, 别的多是 UTF-8); 解出来不像文字的不要."""
    found = []
    for token in _BASE64.findall(command)[:16]:
        try:
            raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=True)
        except (binascii.Error, ValueError):
            continue
        for codec in ("utf-16-le", "utf-8"):
            try:
                text = raw.decode(codec)
            except UnicodeDecodeError:
                continue
            if _plausible(text):
                found.append(text)
    return found


def _command_variants(command: str) -> list[str]:
    """命令的几种写法, 都是原样的文字 (没折叠): 原文 (永远在最前); 去掉 ` ^ 和引号的; 命令里 Base64 解出来的 (套了几层解几层).
    命令类和路径类约束都对这几份各找一遍, 任意一份命中就算; 份数受 MAX_COMMAND_VIEWS 限制 (每份文字最多两种写法)."""
    texts = [command]
    for text in texts:  # 解出来的接在后面, 下一轮也要看
        for decoded in _decoded_texts(text):
            if len(texts) < MAX_COMMAND_VIEWS and decoded not in texts:
                texts.append(decoded)
    variants = []
    for text in texts:
        for variant in (text, text.translate(_NOISE)):
            if variant not in variants:
                variants.append(variant)
    return variants


def _fold(variants: list[str]) -> list[str]:
    """折叠大小写和空白 (_literal), 折叠以后一样的只留第一份."""
    return list(dict.fromkeys(_literal(variant) for variant in variants))


def _command_views(command: str) -> list[str]:
    """命令类约束要对的几份文字 (都折叠了大小写和空白): 就是 _command_variants 折叠以后的."""
    return _fold(_command_variants(command))


def _path_text(text: str) -> str:
    """路径类约束在命令里找路径用的写法: 先展开 %变量%、$env:变量、~ (PowerShell 认作家目录), 斜杠统一成反斜杠, 折叠大小写."""
    expanded = _ENV.sub(lambda m: os.environ.get(m[1], m[0]), os.path.expandvars(text))
    return _TILDE.sub(lambda _: str(P._home()), expanded).replace("/", "\\").casefold()


def _validate(rule: dict, *, assistant: bool = False) -> dict:
    if not isinstance(rule, dict):
        raise ValueError("约束必须是对象")
    if set(rule) - {"kind", "value", "reason", "enabled", "operations"}:
        raise ValueError("约束含不支持的字段，来源和编号由系统记录")
    kind = rule.get("kind", "instruction")
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("不支持的约束类型")
    value = _text(rule.get("value", ""), "约束内容", 2000 if kind == "instruction" else 1024, required=True)
    reason = _text(rule.get("reason", ""), "原因", 1000)
    enabled = rule.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须是布尔值")
    if assistant and not enabled:
        raise ValueError("AI 只能添加启用中的约束")
    operations = rule.get("operations", "all")
    if not isinstance(operations, str) or operations not in {"all", "read", "write"} or (kind != "path" and operations != "all"):
        raise ValueError("只有路径约束可指定 read 或 write")
    if kind == "domain":
        value = _host(value)
    if kind == "tool" and (len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9_.:*?/-]+", value)):
        raise ValueError("工具名仅支持字母数字、分隔符与 * / ? 通配符")
    if kind == "path":
        p = Path(os.path.expandvars(value)).expanduser()
        if not p.is_absolute():
            raise ValueError("路径约束要使用完整路径")
        value = P.unvirtualize(str(p.resolve()))  # 打包应用里 resolve 出来的重定向路径换回真正的位置
    return {"kind": kind, "value": value, "reason": reason, "enabled": enabled, "operations": operations,
            "enforcement": "instruction" if kind == "instruction" else "tool_arguments"}


class BlacklistStore:
    def __init__(self, home: Path, *, vault: str | None = None, defaults: bool = True):
        self.home = Path(home).resolve()
        self.path = self.home / "blacklist.json"
        self.vault = vault
        self.defaults = defaults
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(os.path.normcase(str(self.path)), threading.RLock())
        with self._lock:
            if not self.path.exists():
                initial = {"version": 1, "revision": 0, "global_unrestricted": False,
                           "default_rules_version": DEFAULT_RULES_VERSION, "rules": [], "audit": []}
                if defaults:
                    for raw in self._defaults():
                        initial["rules"].append(self._new(raw, "user", "应用初始保护建议；可在约束设置中修改或删除", origin="default"))
                    initial["audit"].append({"id": uuid.uuid4().hex, "at": time.time(), "action": "initialize",
                                             "by": "user", "rule_id": "", "count": len(initial["rules"])})
                self._save(initial)
            elif defaults:
                self._migrate_defaults()

    def _delivery_defaults(self) -> list[dict]:
        return [{"kind": "path", "value": str(self.home / name), "operations": "write",
                 "reason": "成果、实际执行证据和人工验收记录通过专用接口管理，避免任务自行改写证据"}
                for name in ('deliveries', 'runs')]

    def _control_defaults(self) -> list[dict]:
        """Version 2 additions: agent hooks run commands, the global AGENTS.md steers every session."""
        return [{"kind": "path", "value": str(self.home / "hooks.json"),
                 "reason": "钩子配置通过设置界面管理，避免任务直接改钩子来执行命令"},
                {"kind": "path", "value": str(self.home / "AGENTS.md"), "operations": "write",
                 "reason": "给她的全局说明由你来改，避免网页或材料里的指令借她之手写进去"}]

    def _extension_defaults(self) -> list[dict]:
        """Version 3 additions (插件宿主第 0 步): 这几份文件决定哪个版本的扩展开着、哪些扩展工具标成只读而免问直接调、宿主起哪些
        她自己写的 MCP 程序. 任务能直接写它们, 等于自己给自己开权限; 启用、回退、安装要走专用的工具和授权. 只拦写, 读照旧可以.
        她的 MCP 名单在 ~/.worldmodel/self-mcp (和扩展不在一个目录). 以后再有同类文件 (比如装的时候的同意记录) 就加在这里,
        再把 DEFAULT_RULES_VERSION 加一."""
        extensions = self.home / "extensions"
        return [{"kind": "path", "value": str(extensions / "registry.json"), "operations": "write",
                 "reason": "扩展登记表决定哪个版本的扩展开着；启用、回退要走专用工具和你的授权，不让任务直接改文件"},
                {"kind": "path", "value": str(extensions / "direct-tools.json"), "operations": "write",
                 "reason": "扩展工具快照决定哪些扩展工具标成只读、免问直接交给她用；由同步脚本生成，不让任务直接改"},
                {"kind": "path", "value": str(extensions / "audit.jsonl"), "operations": "write",
                 "reason": "扩展的创建、启用、回退记录是事后核对的依据，不让任务直接改写"},
                {"kind": "path", "value": str(P._home() / ".worldmodel" / "self-mcp" / "registry.json"), "operations": "write",
                 "reason": "她自己写的 MCP 的名单，宿主按它起程序；登记要走安装流程（规范检查、记 SHA-256），不让任务直接改文件"}]

    def _state_defaults(self) -> list[dict]:
        """Version 4 additions (插件宿主第 0 步第三小步): ~/.worldmodel 下服务端自己写的三份状态 (tools/serve.py 把 WM_ASKS_FILE 设成
        看守目录上一级的 asks.json, wm/channels/manager.py 把另外两份放在它旁边). 任务能直接写它们, 就能伪造: 申请卡片 (重启后放回来,
        含合并改动、装 MCP)、派活的线和回报的消息编号 (你引用回报说话会直通到那个会话)、QQ 攒着的消息 (整理好了会当成真的 QQ 消息
        汇报给你). 只拦她的写文件工具和命令, 读照旧; 服务端自己读写不经过黑名单 (它只管工具调用). 默认规则只认默认位置: 把
        WM_ASKS_FILE 指到别处的话, 这三条不会跟着走, 要在约束设置里自己加."""
        base = P._home() / ".worldmodel"
        return [{"kind": "path", "value": str(base / name), "operations": "write", "reason": reason} for name, reason in [
            ("asks.json", "等你点头的申请（合并改动、装 MCP、学新本事这些）存在这里，重启后服务端照它把卡片放回来；"
                          "只有服务端自己写，不让任务直接改，免得伪造出申请卡片"),
            ("workbench-threads.json", "QQ、微信上派到工作台的事（每件一条线）和回报的消息编号存在这里，你引用回报说话会按它直通到"
                                       "那个会话；只有服务端自己写，不让任务直接改"),
            ("qq-digest.json", "QQ 上攒着还没跟你说的重要消息存在这里，整理好了会当成真的 QQ 消息汇报给你；"
                               "只有服务端自己写，不让任务直接改")]]

    def _heartbeat_defaults(self) -> list[dict]:
        """Version 5 additions (S1 心跳, 2026-11-17): 心跳自己的账本在 ~/.worldmodel/heartbeat/ ——
        beats-<日期>.jsonl (平时一行)、drawer.json (想通的攒着)、budget-<日期>.json、due.json、seal.json。
        任务能直接写它们, 就等于能伪造她这些天的经过, 或者把当天的思考额度改成"没花过"。只拦写, 读照旧;
        服务端自己写不经过黑名单 (它只管工具调用)。"""
        base = P._home() / ".worldmodel" / "heartbeat"
        return [{"kind": "path", "value": str(base), "operations": "write",
                 "reason": "心跳的平时记录、抽屉、额度和封存链只有服务端自己写；"
                           "任务能改它们，就能伪造她这些天的经过、把当天额度改成没花过"}]

    def _plugins_defaults(self) -> list[dict]:
        """Version 6 additions (平台化第三步①, 2026-11-19): 第三方插件目录 ~/.worldmodel/plugins/ 整目录写保护 ——
        登记表决定装了哪些插件、哪些开着, 包目录里是插件代码, receipts.jsonl / audit.jsonl 是它做过的记录. 任务能直接
        写这个目录, 就等于绕过安装、启停、授权流程自己塞一个插件进来, 或者改回执抹掉自己做过的事. 只拦写, 读照旧;
        宿主的安装/启用/授权走专用工具和你的点头, 在进程里直接读写, 不经过黑名单 (它只管她的工具调用). 默认规则只认
        默认位置: WM_PLUGIN_HOME 指到别处的话, 这个目录不会跟着走, 要在约束设置里自己加."""
        base = P._home() / ".worldmodel" / "plugins"
        return [{"kind": "path", "value": str(base), "operations": "write",
                 "reason": "第三方插件的登记表、包和回执都在这里；装、启停、授权要走专用流程和你的点头，"
                           "不让任务直接改这个目录"}]

    def _elevation_defaults(self) -> list[dict]:
        """Version 3 additions: 以管理员 (或别的用户) 身份运行. 完全授权档只认黑名单, policy.py 的固定守门在那一档不执行,
        以前黑名单里没有这条路, Start-Process -Verb RunAs 是放行的. 命令类约束是逐字短语 (大小写、空白不论; 短语两头不能紧挨
        字母、数字、_ . -), 所以 runas 和 runas.exe 要各写一条; 套一层 cmd /c、powershell -Command, 中间夹 ^ ` 或引号, 以及
        -EncodedCommand 的写法, check() 会先还原再对 (_command_views). 提权只能走以后专门的代办, 不能让她自己提."""
        why = "要你自己来（Windows 的提权框只有你能点，不能让她替你点）"
        return [{"kind": "command", "value": value, "reason": f"{what}{why}"} for value, what in [
            ("runas", "以管理员或别的用户身份运行（runas、Start-Process -Verb RunAs）"),
            ("runas.exe", "以管理员或别的用户身份运行（runas.exe）"),
            ("runasuser", "换用户运行（Start-Process -Verb RunAsUser）"),
            ("-verb", "Start-Process 的 -Verb 参数能要管理员权限，值又能拼出来，整个参数都拦"),
            ("sudo", "用 sudo 提权"), ("sudo.exe", "用 sudo.exe 提权"),
            ("gsudo", "用 gsudo 提权"), ("gsudo.exe", "用 gsudo.exe 提权")]]

    def _migrate_defaults(self) -> None:
        """Migrate an identifiable default profile once; never re-seed user removals."""
        state = self._read()
        previous = state['default_rules_version']
        if previous >= DEFAULT_RULES_VERSION:
            return
        # v2: a rule recorded through an MSIX-redirected path means the real location. _read() already re-validates every
        # rule into the real location (so it matches before any write); record which saved values change and persist them.
        fixed = []
        if previous < 2:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                saved = {r.get("id"): r.get("value") for r in raw.get("rules", []) if isinstance(r, dict)}
            except (OSError, ValueError, AttributeError):
                saved = {}
            for rule in state['rules']:
                before = saved.get(rule.get('id'))
                if (rule.get('kind') == 'path' and isinstance(before, str) and before != rule['value']
                        and os.path.normcase(P.unvirtualize(before)) == os.path.normcase(rule['value'])):
                    fixed.append({'id': rule['id'], 'from': before, 'to': rule['value']})
        default_profile = any(rule.get('origin') == 'default' for rule in state['rules']) or any(
            isinstance(event, dict) and event.get('action') == 'initialize' for event in state['audit'])
        # Old defaults=False stores had neither default-origin rows nor initialize
        # audit. Absence of proof is not permission to seed defaults into that file.
        if not default_profile:
            if fixed:
                self._record(state, 'fix_paths', 'user', '', fixed=fixed)
                self._save(state)
            return

        def related(rule, target, *, covers=False):
            if not isinstance(rule, dict) or rule.get('kind') != 'path' or rule.get('operations', 'all') == 'read':
                return False
            try:
                root = Path(rule['value']).resolve()
                return P.inside(target, root) or (not covers and P.inside(root, target))
            except (KeyError, OSError, ValueError, TypeError, RuntimeError):
                return False

        def same(rule, raw):
            """命令这类不是路径的约束: 种类和内容一样 (大小写、空白不论) 就是同一条."""
            return (isinstance(rule, dict) and rule.get('kind') == raw['kind']
                    and _literal(str(rule.get('value') or '')) == _literal(raw['value']))

        additions, skipped = [], []
        wanted = ((self._delivery_defaults() if previous < 1 else []) + (self._control_defaults() if previous < 2 else [])
                  + (self._extension_defaults() + self._elevation_defaults() if previous < 3 else [])
                  + (self._state_defaults() if previous < 4 else [])
                  + (self._heartbeat_defaults() if previous < 5 else [])
                  + (self._plugins_defaults() if previous < 6 else []))
        for raw in wanted:
            reason = ''
            if not state['rules']:
                reason = '保留用户已清空的规则列表'
            elif raw['kind'] != 'path':  # 不能拿路径的"重叠"去比: 一条命令当成相对路径会按当前目录算到不相干的地方
                if any(same(rule, raw) for rule in state['rules']):
                    reason = '已有同样的约束（含用户已停用的）'
                elif any(isinstance(event, dict) and event.get('by') == 'user'
                         and event.get('action') in {'remove', 'update'} and same(event.get('before'), raw)
                         for event in state['audit']):
                    reason = '保留用户先前移除或修改这条约束的决定'
            else:
                target = Path(raw['value'])
                if any(related(rule, target, covers=rule['enabled']) for rule in state['rules']):
                    reason = '已有覆盖规则或用户已停用重叠保护'
                elif any(isinstance(event, dict) and event.get('by') == 'user'
                         and event.get('action') in {'remove', 'update'} and related(event.get('before'), target)
                         for event in state['audit']):
                    reason = '保留用户先前移除或修改重叠保护的决定'
            if reason:
                skipped.append({'path': raw['value'], 'reason': reason})
            else:
                additions.append(self._new(raw, 'user', '应用更新新增的默认保护；可在约束设置中修改或删除', origin='default'))
        if len(state['rules']) + len(additions) > MAX_RULES:
            raise ValueError('约束数量已满，新增的默认保护尚未迁移；请先整理规则')
        state['rules'].extend(additions)
        state['default_rules_version'] = DEFAULT_RULES_VERSION
        self._record(state, 'migrate_defaults', 'user', '', from_version=previous, to_version=DEFAULT_RULES_VERSION,
                     added=[{'id': item['id'], 'path': item['value']} for item in additions], skipped=skipped, fixed=fixed)
        self._save(state)

    def _defaults(self) -> list[dict]:
        protected = P.protected_paths(self.vault)
        # The host's WorldModel directory also contains user-installed skills. Protect
        # its credential/control files, not every extension and companion asset inside it.
        if protected:
            appdata_root = protected.pop(0)
            protected += [appdata_root / name for name in ("pet-settings.json", "pet-api-token.txt", "pet-api-guest-token.txt")]
        # resolve() inside an MSIX-packaged process yields the redirected LocalCache path; record the real location.
        rules = [{"kind": "path", "value": P.unvirtualize(str(path.resolve())), "reason": "保护本机凭据、账号与私人数据"}
                 for path in protected]
        rules += [{"kind": "path", "value": P.unvirtualize(str(path.resolve())), "operations": "write", "reason": "避免修改系统目录"}
                  for path in P.system_paths()]
        rules += [{"kind": "path", "value": str(self.home / name), "reason": "权限配置通过设置界面管理，避免任务直接覆盖"}
                  for name in ("blacklist.json", "prefs.json")]
        rules += self._control_defaults()
        rules += self._extension_defaults()
        rules += self._state_defaults()
        rules += self._heartbeat_defaults()
        rules += self._plugins_defaults()
        rules += [{"kind": "path", "value": str(self.home / 'growth' / name), "operations": "write",
                   "reason": "固定验收题集与报告通过专用接口管理，避免任务自行改题或覆盖证据"}
                  for name in ('suites', 'reports', 'adoptions')]
        rules += self._delivery_defaults()
        for command, reason in [
            ("format ", "格式化磁盘"), ("diskpart", "修改磁盘分区"), ("shutdown", "关闭或重启电脑"),
            ("Restart-Computer", "重启电脑"), ("Stop-Computer", "关闭电脑"), ("reg add", "修改注册表"),
            ("reg delete", "删除注册表"), ("reg import", "导入注册表"), ("New-Service", "创建系统服务"),
            ("Remove-Service", "删除系统服务"), ("schtasks /create", "创建计划任务"), ("schtasks /delete", "删除计划任务"),
            ("net user", "修改系统账户"), ("net localgroup", "修改本机用户组"),
            ("Set-ExecutionPolicy", "修改脚本执行策略"), ("Set-MpPreference", "修改系统防护设置"),
            ("Add-MpPreference", "修改系统防护设置"), ("Invoke-Expression", "动态执行不透明命令"),
        ]:
            rules.append({"kind": "command", "value": command.strip(), "reason": reason})
        rules += self._elevation_defaults()
        return rules

    def _read(self) -> dict:
        key = os.path.normcase(str(self.path))
        try:
            if self.path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("约束文件过大")
            raw = self.path.read_bytes()
            hit = _READ_CACHE.get(key)
            if hit is not None and hit[0] == raw and time.monotonic() - hit[1] < READ_CACHE_S:
                return json.loads(hit[2])
            state = json.loads(raw.decode("utf-8"))
            if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1 or not isinstance(state.get("rules"), list) or not isinstance(state.get("audit"), list):
                raise ValueError("约束文件结构无效")
            # Older v1 files did not store these two fields. Only absence is a
            # compatible default; explicit null/string/bool revisions are corrupt.
            state.setdefault("revision", 0)
            state.setdefault("global_unrestricted", False)
            state.setdefault("default_rules_version", 0)
            if type(state["revision"]) is not int or not 0 <= state["revision"] <= MAX_REVISION or type(state["global_unrestricted"]) is not bool:
                raise ValueError("全局授权或版本记录无效")
            if type(state['default_rules_version']) is not int or not 0 <= state['default_rules_version'] <= MAX_REVISION:
                raise ValueError('默认规则迁移版本无效')
            if len(state["rules"]) > MAX_RULES or len(state["audit"]) > MAX_AUDIT:
                raise ValueError("约束文件数量超限")
            ids = set()
            for rule in state["rules"]:
                if (not isinstance(rule, dict) or not _PERSISTED_RULE_FIELDS.issubset(rule)
                        or not isinstance(rule["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", rule["id"])
                        or rule["id"] in ids or not isinstance(rule["source"], str) or rule["source"] not in {"user", "assistant"}):
                    raise ValueError("约束记录无效")
                # Creation defaults are convenient at the API boundary, but using
                # them for a damaged saved rule could silently weaken enforcement.
                rule.update(_validate({k: rule[k] for k in ("kind", "value", "reason", "enabled", "operations") if k in rule}))
                ids.add(rule["id"])
            _READ_CACHE[key] = (raw, time.monotonic(), json.dumps(state, ensure_ascii=False))
            return state
        except (OSError, ValueError, TypeError) as exc:
            _READ_CACHE.pop(key, None)
            raise ValueError("约束配置读取失败，执行已暂停；请检查 blacklist.json，未按空黑名单继续") from exc

    def _save(self, state: dict) -> None:
        revision = state.get("revision", 0)
        if type(revision) is not int or not 0 <= revision < MAX_REVISION or type(state.get("global_unrestricted", False)) is not bool:
            raise ValueError("全局授权或版本记录无效")
        updated = {**state, "revision": revision + 1, "global_unrestricted": state.get("global_unrestricted", False)}
        self.home.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(updated, ensure_ascii=False, indent=2)
        if len(payload.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("约束及审计记录过大，请先整理旧规则")
        tmp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
            state.update(revision=updated["revision"], global_unrestricted=updated["global_unrestricted"])
        finally:
            if tmp.exists():
                tmp.unlink()

    @staticmethod
    def _actor(by: str, *, editing: bool = False):
        if not isinstance(by, str) or by not in {"user", "assistant"} or (editing and by != "user"):
            raise ValueError("只有用户可以修改、停用或移除已有约束" if editing else "未知约束来源")

    @staticmethod
    def _new(raw: dict, by: str, evidence: str, origin: str = "added") -> dict:
        now = time.time()
        return {**_validate(raw, assistant=by == "assistant"), "id": uuid.uuid4().hex,
                "source": by, "origin": origin, "evidence": _text(evidence, "依据", 4000), "created": now, "updated": now}

    @staticmethod
    def _record(state: dict, action: str, by: str, rule_id: str, **data):
        state["audit"].append({"id": uuid.uuid4().hex, "at": time.time(), "action": action, "by": by, "rule_id": rule_id, **data})
        state["audit"] = state["audit"][-MAX_AUDIT:]

    def list(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._read()["rules"])

    def snapshot(self) -> dict:
        """One coherent read for Python and the native read-only control surface."""
        with self._lock:
            state = self._read()
            return copy.deepcopy({key: state[key] for key in ("version", "revision", "global_unrestricted", "rules", "audit")})

    def set_global_unrestricted(self, enabled: bool, by: str = "user") -> dict:
        if by != "user":
            raise ValueError("只有用户可以设置全局完全授权")
        if type(enabled) is not bool:
            raise ValueError("global_unrestricted 必须是布尔值")
        with self._lock:
            state = self._read()
            if state["global_unrestricted"] != enabled:
                before = state["global_unrestricted"]
                state["global_unrestricted"] = enabled
                self._record(state, "set_global_unrestricted", "user", "", before=before, after=enabled)
                self._save(state)
            return copy.deepcopy({key: state[key] for key in ("version", "revision", "global_unrestricted", "rules", "audit")})

    def audit(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._read()["audit"][-max(1, min(MAX_AUDIT, int(limit))):][::-1])

    def add(self, rule: dict, *, by: str = "user", evidence: str = "") -> dict:
        self._actor(by)
        fresh = self._new(rule, by, evidence)
        with self._lock:
            state = self._read()
            # Never silently overwrite a disabled rule, or turn a user rule into an AI rule.
            for old in state["rules"]:
                if all(old.get(k) == fresh[k] for k in ("kind", "value", "operations")):
                    if old["enabled"]:
                        return copy.deepcopy(old)
                    raise ValueError("同一约束已被停用；只有用户可在设置中重新启用，不能重复添加绕过这一决定")
            if len(state["rules"]) >= MAX_RULES:
                raise ValueError(f"最多保存 {MAX_RULES} 条约束")
            state["rules"].append(fresh)
            self._record(state, "add", by, fresh["id"], after=fresh)
            self._save(state)
            return copy.deepcopy(fresh)

    def update(self, rule_id: str, changes: dict, *, by: str = "user") -> dict:
        self._actor(by, editing=True)
        if not isinstance(changes, dict) or set(changes) - {"kind", "value", "reason", "enabled", "operations"}:
            raise ValueError("只能修改类型、内容、原因、启用状态和路径操作范围")
        with self._lock:
            state = self._read()
            old = next((rule for rule in state["rules"] if rule["id"] == rule_id), None)
            if old is None:
                raise ValueError("没有这条约束")
            before = copy.deepcopy(old)
            payload = {k: old[k] for k in ("kind", "value", "reason", "enabled", "operations")}
            old.update(_validate({**payload, **changes}))
            old["updated"] = time.time()
            self._record(state, "update", by, old["id"], before=before, after=copy.deepcopy(old))
            self._save(state)
            return copy.deepcopy(old)

    def remove(self, rule_id: str, *, by: str = "user") -> bool:
        self._actor(by, editing=True)
        with self._lock:
            state = self._read()
            old = next((rule for rule in state["rules"] if rule["id"] == rule_id), None)
            if old is None:
                return False
            state["rules"].remove(old)
            self._record(state, "remove", by, old["id"], before=old)
            self._save(state)
            return True

    def instruction_text(self) -> str:
        rows = [f"- [{rule['id'][:8]}] {rule['value']}" for rule in self.list() if rule["enabled"] and rule["kind"] == "instruction"]
        return "当前行为约束（自然语言规则，需在规划与执行时遵守）：\n" + "\n".join(rows) if rows else ""

    def check(self, tool: str, args: dict, workspace: Path | str) -> P.Decision | None:
        """Return the first hard deny. Instructions intentionally never pretend to match."""
        if not isinstance(tool, str) or not tool or not isinstance(args, dict):
            return P.Decision("deny", "工具参数必须是对象", "blacklist")
        try:
            rules = self.list()
            workspace = Path(workspace).resolve()
            values = list(_strings(args))
            path_values = list(_strings(args, paths_only=True))
        except ValueError as exc:
            return P.Decision("deny", str(exc), "blacklist")
        paths = []
        for raw in path_values:
            if raw and "://" not in raw:
                try:
                    paths.append(_path(raw, workspace))
                except (OSError, ValueError, RuntimeError):
                    return P.Decision("deny", "工具包含无法解析的路径", "blacklist")
        if tool in P.READ_TOOLS | P.WRITE_TOOLS and not args.get("path"):
            paths.append(workspace)
        command = str(args.get("command") or "")
        variants = _command_variants(command) if command else []
        command_views = _fold(variants)
        command_paths = list(dict.fromkeys(_path_text(variant) for variant in variants))  # 原文那一份在最前, 判定和以前一字不差
        hosts = set()
        for value in values:
            for match in _URL.finditer(value):
                try:
                    host = urlsplit(match[0]).hostname
                    if host:
                        hosts.add(_host(host))
                except ValueError:
                    continue
        for rule in rules:
            if not rule["enabled"] or rule["kind"] == "instruction":
                continue
            kind, value = rule["kind"], rule["value"]
            match = False
            if kind == "tool":
                match = fnmatch.fnmatchcase(tool.casefold(), value.casefold())
            elif kind == "command":
                needle = _literal(value)
                match = any(_command_contains(view, needle) for view in command_views)
            elif kind == "domain":
                match = any(host == value or host.endswith("." + value) for host in hosts)
            elif kind == "path":
                operation = "read" if tool in P.READ_TOOLS or tool in {"web_fetch", "web_search", "read_skill"} else "write"
                if rule["operations"] not in {"all", operation}:
                    continue
                root = Path(value).resolve()
                match = any(P.inside(path, root) or (tool in {"grep", "glob"} and P.inside(root, path)) for path in paths)
                # Only literal paths are recognizable in arbitrary shell code; the UI documents this limit.
                normalized = P.unvirtualize(str(root)).replace("/", "\\").casefold()
                if command_paths:
                    found = re.compile(re.escape(normalized) + r"(?=$|[\\\s\"'`;|&<>),])")
                    if any(found.search(text) for text in command_paths):  # 原文和还原后的几份, 任意一份命中
                        match = True
            if match:
                detail = rule["reason"] or rule["value"]
                return P.Decision("deny", f"命中黑名单 [{rule['id'][:8]}] {detail}", "blacklist")
        return None

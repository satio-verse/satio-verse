"""插件清单的 `plugin` 块: 格式和校验 (宿主第 1 步的一部分, 先把世界/渠道这一块定下来).

契约全文见知识库 `09 问题与待办/世界接口契约（2026-11-15）.md`. 这里只有纯函数, 不 import 任何 wm.*, 不执行包里的代码,
也没有接进 `ExtensionRegistry.create()` (接进去是后面的步骤, 到时要另行登记 extensions.py).

四个入口:
- `validate_plugin_manifest(manifest, package_dir=None)`: 清单自己对不对 (格式 + 内部一致). 返回 `Issue` 列表, 没有 error 才算合格.
- `policy_issues(manifest, source)`: 这个来路能不能拿这些声明. `source` 由宿主填 (bundled / third_party), 清单不能自称.
- `separate_permissions(manifest)`: 装的时候要单独列给用户看的权限 ("这条通道可以当我本人" 等).
- `find_conflicts(manifest, installed)`: 动作名前缀/重名.

`FIELD_DOCS` 记着每个字段谁填、谁读、填错了会怎样; 契约页的字段表由它生成.
"""
from __future__ import annotations

import re
from collections import namedtuple
from pathlib import Path, PurePosixPath

Issue = namedtuple("Issue", "path code message level")

SCHEMA = 2
SOURCES = ("bundled", "third_party")
PLUGIN_TIERS = ("text", "panel", "code", "process", "builtin")
WORLD_KINDS = ("chat", "space", "game")
SENSES = ("saw", "heard", "told", "receipt", "notice")
CHAT_SENSES = ("told", "receipt", "notice")
URGENCIES = ("piggyback", "batch", "now", "interrupt")
TRUST_FROM = ("platform_id", "paired_owner", "local_loopback", "none")
TRUST_LEVELS = ("stranger", "acquaintance", "owner_remote")  # 由低到高
PROACTIVE_MODES = ("free", "quota", "reply_window", "never")
GROUP_SEES = ("all", "mention_only")
ATTACHMENTS = ("image", "voice", "file", "video")
PARAM_TYPES = ("string", "int", "number", "bool", "enum")
SERVICE_START = ("on-demand", "always")
UPLOAD_MODES = ("never", "ask-each")
FILE_MODES = ("r", "rw")

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")  # 同 wm/agent/extensions.py 的 ID
PREFIX = re.compile(r"^[a-z][a-z0-9]{1,11}$")
EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
IDENT = EVENT_TYPE
ACTION_NAME = re.compile(r"^[a-z][a-z0-9]{1,11}_[a-z][a-z0-9_]{0,39}$")
PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$")
_CMP = r"(?:>=|<=|>|<|\^|~|=)?\s*\d+(?:\.\d+){0,2}(?:-[A-Za-z0-9.-]+)?"
HOST_RANGE = re.compile(r"^" + _CMP + r"(?:\s+" + _CMP + r")*$")
DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")  # 同 wm/selfmcp.py
PRIVATE_TLDS = {"local", "localhost", "internal", "lan", "home", "corp", "intranet", "localdomain", "test", "example", "invalid"}
LOCAL_ADDR = re.compile(r"^127\.0\.0\.1:(\d{4,5})$")
STD_FIELDS = {"text", "sender", "conv", "mention", "reply_to", "attachments"}  # 渠道事件的标准字段, 不用在 visible 里再列
# 明确的信任档字段: 哪一档的世界都不许带
TRUST_FIELDS = {"trust", "trust_tier", "trust_level", "is_owner", "is_master", "verified_owner", "privilege"}
# 这几个词在渠道里就是"发信人是什么身份", 只对 chat 档拦; 游戏、广场里它们是正常字段 (角色、东西归谁、房间权限)
TRUST_FIELDS_CHAT = {"owner", "role", "permission", "permissions", "auth"}

HINTS = {
    "trust": "信任档由核心定, 插件只能在 world.trust_max 里申请上限",
    "trust_level": "信任档由核心定, 插件只能在 world.trust_max 里申请上限",
    "trust_tier": "信任档由核心定, 插件只能在 world.trust_max 里申请上限",
    "is_owner": "谁是主人由核心按自己的配对表定, 插件不能声明",
    "owner": "谁是主人由核心按自己的配对表定, 插件不能声明",
    "session": "世界会话的能力在创建时由核心定死, 清单不能声明",
    "capabilities": "世界会话的能力在创建时由核心定死, 清单不能声明",
    "public_callback": "不许在公网上开端口; 只支持公网回调的平台不接 (或走社区服务器转发)",
    "listen": "不许在公网上开端口; 本机端口要连的写进 permissions.local",
    "webhook": "不许在公网上开端口; 只支持公网回调的平台不接",
    "inbound_port": "不许在公网上开端口",
    "public_port": "不许在公网上开端口",
    "as_instruction": "事件正文一律当数据, 没有当指令的开关",
    "trusted": "事件正文一律当数据, 没有当指令的开关",
    "instruction": "事件正文一律当数据, 没有当指令的开关",
}

# 每个对象允许的键. "" 是顶层; 数组元素写 `[]`, 按名字的参数表写 `{}`.
ALLOWED = {
    "": ("id", "name", "description", "files", "mcp", "ui", "plugin", "sha256", "revision", "created", "by", "validation"),
    "plugin": ("schema", "version", "author", "license", "host", "tier", "permissions", "service", "panel", "world",
               "tools", "schedule", "components", "settings", "budget", "lends", "provides", "practice", "assets"),
    "plugin.permissions": ("network", "local", "files", "accounts", "notify", "model", "upload", "elevated",
                           "exec", "borrow", "capture", "audio", "channel"),
    "plugin.permissions.files[]": ("path", "mode"),
    "plugin.permissions.accounts[]": ("service", "purpose"),
    "plugin.permissions.channel": ("act_as_owner", "answer_cards"),
    "plugin.service": ("cmd", "health", "start", "stop_cmd", "stop_with_host", "heartbeat_s", "max_runtime_s"),
    "plugin.panel": ("entry", "tools"),
    "plugin.world": ("kind", "prefix", "events", "actions", "perception", "env_prompt", "leave", "spectate",
                     "trust_from", "trust_max", "proactive", "caps"),
    "plugin.world.events[]": ("type", "sense", "urgency", "fields"),
    "plugin.world.actions[]": ("name", "kind", "outward", "receipt", "params", "why"),
    "plugin.world.actions[].params{}": ("type", "required", "max_len", "values"),
    "plugin.world.perception": ("visible", "hidden", "twin_fixtures"),
    "plugin.world.leave": ("ack_timeout_s",),
    "plugin.world.spectate": ("panel",),
    "plugin.world.proactive": ("mode", "quota_per_day", "reply_window_s"),
    "plugin.world.caps": ("dm", "group", "group_sees", "attachments", "max_len", "needs_proxy"),
}
# 本步只查类型、细节归宿主第 1 步或别的专题的键 (键 -> 允许的类型)
RESERVED_TYPES = {"tools": (list,), "schedule": (list,), "components": (list,), "settings": (list,), "assets": (list,),
                  "budget": (dict,), "practice": (dict,), "lends": (list, dict), "provides": (list, str)}
RESERVED_PERM_TYPES = {"exec": (list,), "borrow": (list,), "capture": (list, dict), "audio": (bool,)}

FIELD_DOCS = {
    # ---- 顶层 (老字段不变) ----
    "id": ("作者", "宿主 (登记表、权限卡、审计)", "格式不对拒装; 和已装的重名当同一个插件的新版本"),
    "name": ("作者", "宿主、权限卡标题", "空或超过 80 字拒装"),
    "description": ("作者", "权限卡、市场列表", "超过 1000 字拒装; 说谎没人拦, 靠权限卡上的权限本身说话"),
    "files": ("作者 (只在创建时)", "宿主 (落盘、算哈希)", "仍按 ExtensionRegistry.create() 原有规则: 单文件 256 KB、合计 1 MB"),
    "mcp": ("作者", "宿主 (起 MCP)", "本步只看是对象或空, 细节仍归 create()"),
    "ui": ("作者", "宿主 (沙箱预览)", "必须是包内 .html 的相对路径, 否则拒装"),
    "plugin": ("作者", "宿主 (权限卡、看守、核心)", "没有这一块的是老包, 照老规矩装; 有这一块就按本契约全部检查"),
    "sha256": ("宿主 (create 时算)", "宿主 (每次启动前核对)", "作者不用写; 写了被宿主覆盖"),
    "revision": ("宿主 (create 时算)", "宿主", "作者不用写; 写了被宿主覆盖"),
    "created": ("宿主", "宿主", "作者不用写"),
    "by": ("宿主", "宿主 (审计)", "作者不用写"),
    "validation": ("宿主", "宿主、面板", "作者不用写"),
    # ---- plugin ----
    "plugin.schema": ("作者", "宿主 (旧宿主见到不认识的格式号会拒绝)", "不是 2 拒装 (对应 DSH 的 manifestVersion)"),
    "plugin.version": ("作者", "宿主、权限卡 (更新只显示变化)", "不是 x.y.z 拒装; 改了任何权限必须升版本, 否则宿主按旧同意记录放行的风险由宿主第 1 步堵"),
    "plugin.author": ("作者", "权限卡 ('谁做的')", "空或超过 80 字拒装; 说谎没人拦"),
    "plugin.license": ("作者", "权限卡、市场上架检查", "空拒装; 许可不能空是上架检查的要求"),
    "plugin.host": ("作者", "宿主 (兼容范围, 我们会强制)", "不是版本范围串拒装; 范围不含当前宿主版本时宿主拒装 (这一步不查)"),
    "plugin.tier": ("作者声明; 宿主按来路核对", "宿主 (选隔离方式)、权限卡 ('它能做什么')",
                    "不在 text/panel/code/process/builtin 里拒装; builtin 只给来路是 bundled 的自带插件 (policy_issues 报 error)"),
    "plugin.permissions": ("作者", "宿主 (权限卡、运行时按它核对每次访问)", "缺这一块拒装 (空对象 {} 就是'什么权限都不要')"),
    "plugin.service": ("作者", "宿主看守 (起停、体检、重启)", "格式不对拒装; chat 档非 builtin 缺它拒装"),
    "plugin.panel": ("作者", "宿主 (面板桥、旁观面板)", "entry 不是包内相对路径拒装"),
    "plugin.world": ("作者", "宿主 (世界会话、事件入口、叫停)", "不是对象或 null 拒装; 里面的规则见下"),
    "plugin.tools": ("作者", "宿主 (没声明的工具不给调)", "本步只查是数组; 细节归宿主第 1 步"),
    "plugin.schedule": ("作者", "宿主 (定时)", "本步只查是数组"),
    "plugin.components": ("作者", "宿主 (可选组件)", "本步只查是数组"),
    "plugin.settings": ("作者", "宿主 (设置页)", "本步只查是数组"),
    "plugin.assets": ("作者", "宿主 (素材登记)", "本步只查是数组"),
    "plugin.budget": ("作者", "宿主 (记账和上限)", "本步只查是对象"),
    "plugin.practice": ("作者", "宿主 (练习场)", "本步只查是对象"),
    "plugin.lends": ("作者", "宿主 (借条)", "本步只查是数组或对象"),
    "plugin.provides": ("作者", "宿主 (能力槽)", "本步只查是字符串或数组"),
    # ---- permissions ----
    "plugin.permissions.network": ("作者", "宿主 (只放行这些域名)", "不是公网域名 (IP、localhost、内网后缀) 或超过 16 个拒装"),
    "plugin.permissions.local": ("作者", "宿主 (只放行这些本机端口)", "不是 127.0.0.1:端口 (1024–65535) 或超过 8 个拒装; 没写的端口连不上"),
    "plugin.permissions.files": ("作者", "宿主 (文件夹读写)", "元素不是 {path, mode} 拒装"),
    "plugin.permissions.files[].path": ("作者", "宿主、权限卡", "空或超长拒装"),
    "plugin.permissions.files[].mode": ("作者", "宿主", "不是 r / rw 拒装"),
    "plugin.permissions.accounts": ("作者", "宿主 (保管箱按用途租用)、权限卡", "元素不是 {service, purpose} 拒装; 第一版第三方不给账号权限由宿主第 1 步堵"),
    "plugin.permissions.accounts[].service": ("作者", "宿主 (保管箱)", "格式不对拒装"),
    "plugin.permissions.accounts[].purpose": ("作者", "权限卡 ('用哪个账号做什么')", "空或超过 40 字拒装"),
    "plugin.permissions.notify": ("作者", "宿主、权限卡", "不是布尔拒装"),
    "plugin.permissions.model": ("作者", "宿主 (借模型)", "不是 null 或字符串拒装"),
    "plugin.permissions.upload": ("作者", "宿主 (上云前问不问)", "不是 never / ask-each 拒装"),
    "plugin.permissions.elevated": ("作者", "宿主 (提权代办名单核对)", "不是字符串数组拒装; 点名名单外的动作由代办拒"),
    "plugin.permissions.exec": ("作者", "宿主 (固定命令模板)", "本步只查是数组"),
    "plugin.permissions.borrow": ("作者", "宿主 (借条)", "本步只查是数组"),
    "plugin.permissions.capture": ("作者", "宿主 (截哪个窗口)", "本步只查是数组或对象"),
    "plugin.permissions.audio": ("作者", "宿主 (系统声音)", "本步只查是布尔"),
    "plugin.permissions.channel": ("作者 (只有 chat 档世界能写)", "宿主 (权限卡'单独列出'的两项)", "写在非 chat 档拒装; 不写等于两项都是 false"),
    "plugin.permissions.channel.act_as_owner": ("作者申请, 用户在权限卡上点头", "核心 (决定要不要认'主人的号来的话'是本人)",
                                                 "必须同 world.trust_max=owner_remote 一起出现 (两边不一致拒装); 第三方来路本版一律拒"),
    "plugin.permissions.channel.answer_cards": ("作者申请, 用户在权限卡上点头", "核心 (申请卡片能不能在这条通道上答复)",
                                                 "必须同 act_as_owner 一起出现; 第三方来路本版一律拒"),
    # ---- service / panel ----
    "plugin.service.cmd": ("作者", "宿主看守 (起进程, 不经 shell)", "不是 1–16 个字符串拒装"),
    "plugin.service.health": ("作者", "宿主看守 (体检)", "不是以 / 开头的路径拒装"),
    "plugin.service.start": ("作者", "宿主看守", "不是 on-demand / always 拒装"),
    "plugin.service.stop_cmd": ("作者", "宿主看守 (停之前先请它收尾)", "不是字符串数组拒装"),
    "plugin.service.stop_with_host": ("作者", "宿主看守", "不是布尔拒装"),
    "plugin.service.heartbeat_s": ("作者", "宿主看守 (心跳断了就停)", "不是 1–3600 的整数拒装"),
    "plugin.service.max_runtime_s": ("作者", "宿主看守 (最长运行时间)", "不是 1–604800 的整数拒装"),
    "plugin.panel.entry": ("作者", "宿主 (沙箱 iframe)", "不是包内相对路径拒装; 文件不存在 (给了包目录时) 拒装"),
    "plugin.panel.tools": ("作者", "宿主 (面板桥只放行这些工具)", "不是字符串数组拒装"),
    # ---- world 通用 ----
    "plugin.world.kind": ("作者", "宿主 (选事件档和该有哪些声明)", "不是 chat / space / game 拒装; 这个值决定下面哪些字段必须有、哪些不许有"),
    "plugin.world.prefix": ("作者", "宿主 (动作名前缀、重名检查)", "格式不对拒装; 已被别的插件占用时 find_conflicts 报重名拒装"),
    "plugin.world.events": ("作者", "宿主 (事件入口)", "必须是数组 (可以空); 适配器交来清单里没有的事件类型会在运行时被丢掉并记审计"),
    "plugin.world.events[].type": ("作者", "宿主、旁观面板", "格式不对或重名拒装"),
    "plugin.world.events[].sense": ("作者", "宿主 (打进事件的感知通道)",
                                    "不在 saw/heard/told/receipt/notice 里拒装; chat 档只许 told/receipt/notice; notice 不进她的上下文 (运行时约定)"),
    "plugin.world.events[].urgency": ("作者", "宿主 (合批、打断规则)", "不在 piggyback/batch/now/interrupt 里拒装; notice 事件不许写; 不写按 batch"),
    "plugin.world.events[].fields": ("作者", "宿主 (事件里能带哪些字段)",
                                     "字段必须在 perception.visible 里 (标准字段除外), 不许碰 hidden, 不许叫 trust / is_owner 之类明确的信任档名字 (chat 档还拦 role / owner / permission(s) / auth; 游戏和广场里这几个是正常字段), 否则拒装"),
    "plugin.world.actions": ("作者", "宿主 (动作表; 清单里没有的动作调不了)", "必须是数组 (可以空)"),
    "plugin.world.actions[].name": ("作者", "宿主 (动作名带世界前缀)", "必须是 <prefix>_<动词>, 前缀对不上、重名都拒装"),
    "plugin.world.actions[].kind": ("作者", "宿主 (问世界 / 动世界走不同的权限档)", "不是 query / act 拒装"),
    "plugin.world.actions[].outward": ("作者", "宿主 (会让别人看到听到的先过出站检查)", "act 必须写明; query 写 true 拒装"),
    "plugin.world.actions[].receipt": ("作者", "宿主 (没回执不算做了)", "act 必须指向一个 sense=receipt 的事件类型, 否则拒装; query 不许写"),
    "plugin.world.actions[].params": ("作者", "宿主 (调用前按它核对参数)", "最多 8 个; 参数名或类型不对拒装"),
    "plugin.world.actions[].params{}.type": ("作者", "宿主", "不在 string/int/number/bool/enum 里拒装"),
    "plugin.world.actions[].params{}.required": ("作者", "宿主", "不是布尔拒装"),
    "plugin.world.actions[].params{}.max_len": ("作者", "宿主", "只有 string 能写; 范围 1–4000"),
    "plugin.world.actions[].params{}.values": ("作者", "宿主", "enum 必须写 1–32 个字符串; 其他类型不许写"),
    "plugin.world.actions[].why": ("作者", "权限卡、审计 (这个动作为什么要)", "空或超过 80 字拒装"),
    "plugin.world.perception": ("作者", "宿主 (认知和真值分开)", "space / game 档必须有; chat 档 hidden 为空时可省"),
    "plugin.world.perception.visible": ("作者", "宿主、双胞胎夹具", "交给她的字段名; 和 hidden 重名拒装; space / game 档不能空"),
    "plugin.world.perception.hidden": ("作者", "宿主 (这些永远不进事件和提示词)", "事件 fields 里出现它拒装; 非空时必须给双胞胎夹具"),
    "plugin.world.perception.twin_fixtures": ("作者", "宿主 (装之前离线跑: 差在她感知不到的地方, 行为要逐条相同)", "space / game 档或 hidden 非空时必须有; 文件不存在 (给了包目录时) 拒装"),
    "plugin.world.env_prompt": ("作者", "宿主 (拼进她的世界提示词)", "不是包内 .md 相对路径拒装"),
    "plugin.world.leave": ("作者", "宿主看守 (叫她回来后插件要在时限内确认离场)", "不写按 5 秒; 超过 30 秒或不是整数拒装"),
    "plugin.world.leave.ack_timeout_s": ("作者", "宿主看守 (超时结束进程)", "不是 1–30 的整数拒装"),
    "plugin.world.spectate": ("作者", "宿主 (旁观面板)", "space / game 档必须有; chat 档用标准聊天页, 可省"),
    "plugin.world.spectate.panel": ("作者", "宿主 (旁观面板只读自己世界的事件)", "必须等于 plugin.panel.entry 且文件存在, 否则拒装"),
    # ---- 只有渠道 (chat 档) 才有的声明 ----
    "plugin.world.trust_from": ("作者声明证据从哪来", "核心 (据此查自己的配对表/白名单/好友表, 定每个发信人的档)",
                                 "chat 档必须写, 其他档不许写; none 只能配 stranger, local_loopback 最高 acquaintance; 写错不会让人升档, 只会被拒装"),
    "plugin.world.trust_max": ("作者申请上限; 核心可以给得更低", "核心 (档由核心定, 这里只是上限)",
                                "chat 档必须写, 其他档不许写; owner_remote 必须同 act_as_owner 一起; 第三方来路本版一律拒; 插件抬不了档"),
    "plugin.world.proactive": ("作者", "核心 ('现在哪条通道找得到主人'的统一回答、申请卡片兜底)", "chat 档必须写, 其他档不许写; 说谎的后果是发不出去时被核心改走别的通道"),
    "plugin.world.proactive.mode": ("作者", "核心", "不在 free/quota/reply_window/never 里拒装"),
    "plugin.world.proactive.quota_per_day": ("作者", "核心 (限频)", "quota 必须写 1–100000 的整数; 其他模式不许写"),
    "plugin.world.proactive.reply_window_s": ("作者 (平台没公布窗口就不写)", "核心 (在不在回复窗口里)", "只有 reply_window 能写; 不是 1–86400 的整数拒装"),
    "plugin.world.caps": ("作者", "核心 (平台能力, 装的时候定死)", "chat 档必须写, 其他档不许写"),
    "plugin.world.caps.dm": ("作者", "核心", "必须是布尔"),
    "plugin.world.caps.group": ("作者", "核心", "必须是布尔; paired_owner 的通道只有一个对端, 不能写 true"),
    "plugin.world.caps.group_sees": ("作者", "核心 (群里要不要被 @ 才收)", "group=true 时必须写 all / mention_only; group=false 时不许写"),
    "plugin.world.caps.attachments": ("作者", "核心 (收发哪些附件)", "只能是 image/voice/file/video 且不重复"),
    "plugin.world.caps.max_len": ("作者", "核心 (单条多长)", "不是 1–100000 的整数拒装"),
    "plugin.world.caps.needs_proxy": ("作者", "宿主 (下载/联网要不要走代理)", "不是布尔拒装"),
}


class _Ctx:
    def __init__(self, package_dir=None):
        self.issues: list[Issue] = []
        self.package_dir = Path(package_dir) if package_dir is not None else None

    def err(self, path, code, message):
        self.issues.append(Issue(path, code, message, "error"))

    def warn(self, path, code, message):
        self.issues.append(Issue(path, code, message, "warn"))


def _j(base, key):
    return key if not base else base + "." + key


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _field_paths():
    out = []
    for obj, keys in ALLOWED.items():
        out += [_j(obj, k) for k in keys]
    return out


def field_paths():
    """校验器认识的每一个字段路径 (含数组元素 `[]`、按名字的参数表 `{}`), 契约页字段表和测试用."""
    return _field_paths()


def has_errors(issues):
    return any(i.level == "error" for i in issues)


def _object(c, path, value, allowed_key, required=(), allow_none=False):
    """是不是对象, 有没有不认识的键, 必填键在不在. 返回 dict 或 None (后面的检查就跳过)."""
    if value is None and allow_none:
        return None
    if not isinstance(value, dict):
        c.err(path, "E_NOT_OBJECT", "这里应该是一个对象")
        return None
    allowed = ALLOWED[allowed_key]
    for key in value:
        if key not in allowed:
            hint = HINTS.get(key)
            c.err(_j(path, str(key)), "E_UNKNOWN_KEY",
                  "不认识的字段" + (": " + hint if hint else ": 本版清单格式里没有它 (权限类字段不会悄悄放行)"))
    for key in required:
        if key not in value:
            c.err(_j(path, key), "E_MISSING", "缺少必填字段 " + key)
    return value


def _str(c, path, v, code, maxlen=80, minlen=1):
    if not isinstance(v, str) or not (minlen <= len(v.strip()) and len(v) <= maxlen):
        c.err(path, code, "需要 %d～%d 个字符的字符串" % (minlen, maxlen))
        return None
    return v


def _bool(c, path, v):
    if not isinstance(v, bool):
        c.err(path, "E_NOT_BOOL", "需要布尔值 true / false")
        return None
    return v


def _enum(c, path, v, choices, code):
    if v not in choices:
        c.err(path, code, "只能是 " + " / ".join(choices))
        return None
    return v


def _int_range(c, path, v, lo, hi, code="E_RANGE"):
    if not _is_int(v) or not lo <= v <= hi:
        c.err(path, code, "需要 %d～%d 的整数" % (lo, hi))
        return None
    return v


def _list(c, path, v, maxn, code="E_NOT_LIST"):
    if not isinstance(v, list) or len(v) > maxn:
        c.err(path, code, "需要最多 %d 项的数组" % maxn)
        return None
    return v


def _rel_syntax(name):
    if not isinstance(name, str) or not name or len(name) > 180 or "\\" in name or ":" in name or "\x00" in name:
        return False
    parts = name.split("/")
    if name.startswith("/") or any(p in ("", ".", "..") for p in parts):
        return False
    return not any(p.startswith(".") or p.endswith((".", " ")) for p in parts)


def _rel(c, path, name, suffixes=None):
    """包内相对路径: 语法 +(给了包目录时) 文件在不在、有没有越出目录."""
    if not _rel_syntax(name):
        c.err(path, "E_PATH_BAD", "要写成包内的相对路径 (用 / 分隔, 不能绝对、不能含 .. 、不能以点开头)")
        return None
    if suffixes and not name.lower().endswith(suffixes):
        c.err(path, "E_PATH_SUFFIX", "文件类型要是 " + " / ".join(suffixes))
        return None
    if c.package_dir is not None:
        try:
            root = c.package_dir.resolve()
            target = (root / PurePosixPath(name)).resolve()
            target.relative_to(root)
        except (ValueError, OSError):
            c.err(path, "E_FILE_OUTSIDE", "文件越出了包目录")
            return None
        if not target.is_file():
            c.err(path, "E_FILE_MISSING", "包里没有这个文件: " + name)
            return None
    return name


def _domains(c, path, v):
    items = _list(c, path, v, 16)
    for i, host in enumerate(items or []):
        p = "%s[%d]" % (path, i)
        h = host.lower() if isinstance(host, str) else ""
        if not isinstance(host, str) or host != h or not DOMAIN.match(h) or h.rsplit(".", 1)[-1] in PRIVATE_TLDS:
            c.err(p, "E_DOMAIN", "只能是小写的公网域名 (不能是 IP、localhost、内网后缀)")
    if items and len(set(items)) != len(items):
        c.err(path, "E_DUPLICATE", "域名重复")


def _local(c, path, v):
    items = _list(c, path, v, 8)
    for i, addr in enumerate(items or []):
        m = LOCAL_ADDR.match(addr) if isinstance(addr, str) else None
        if not m or not 1024 <= int(m.group(1)) <= 65535:
            c.err("%s[%d]" % (path, i), "E_LOCAL", "只能是 127.0.0.1:端口 (1024–65535)")
    if items and len(set(items)) != len(items):
        c.err(path, "E_DUPLICATE", "本机端口重复")


def _str_list(c, path, v, maxn, pattern=None, code="E_NOT_STR_LIST", maxlen=300):
    items = _list(c, path, v, maxn, code)
    for i, x in enumerate(items or []):
        if not isinstance(x, str) or not x or len(x) > maxlen or (pattern and not pattern.fullmatch(x)):
            c.err("%s[%d]" % (path, i), code, "元素格式不对")
    return items


def _permissions(c, path, perms):
    perms = _object(c, path, perms, "plugin.permissions")
    if perms is None:
        return {}
    if "network" in perms:
        _domains(c, _j(path, "network"), perms["network"])
    if "local" in perms:
        _local(c, _j(path, "local"), perms["local"])
    if "files" in perms:
        items = _list(c, _j(path, "files"), perms["files"], 16)
        for i, f in enumerate(items or []):
            p = "%s.files[%d]" % (path, i)
            f = _object(c, p, f, "plugin.permissions.files[]", required=("path", "mode"))
            if f:
                if "path" in f:
                    _str(c, p + ".path", f["path"], "E_STR", 300)
                if "mode" in f:
                    _enum(c, p + ".mode", f["mode"], FILE_MODES, "E_ENUM")
    if "accounts" in perms:
        items = _list(c, _j(path, "accounts"), perms["accounts"], 8)
        for i, a in enumerate(items or []):
            p = "%s.accounts[%d]" % (path, i)
            a = _object(c, p, a, "plugin.permissions.accounts[]", required=("service", "purpose"))
            if a:
                if "service" in a and (not isinstance(a["service"], str) or not ID.match(a["service"])):
                    c.err(p + ".service", "E_STR", "服务名只能是小写字母、数字和 -")
                if "purpose" in a:
                    _str(c, p + ".purpose", a["purpose"], "E_STR", 40)
    if "notify" in perms:
        _bool(c, _j(path, "notify"), perms["notify"])
    if "model" in perms and perms["model"] is not None:
        _str(c, _j(path, "model"), perms["model"], "E_STR", 80)
    if "upload" in perms:
        _enum(c, _j(path, "upload"), perms["upload"], UPLOAD_MODES, "E_ENUM")
    if "elevated" in perms:
        _str_list(c, _j(path, "elevated"), perms["elevated"], 16, maxlen=80)
    for key, types in RESERVED_PERM_TYPES.items():
        if key in perms and not isinstance(perms[key], types):
            c.err(_j(path, key), "E_TYPE", "类型不对 (本步只查类型)")
    return perms


def _service(c, path, svc):
    svc = _object(c, path, svc, "plugin.service", required=("cmd",), allow_none=True)
    if svc is None:
        return None
    if "cmd" in svc:
        items = _str_list(c, path + ".cmd", svc["cmd"], 16, code="E_CMD")
        if items is not None and not items:
            c.err(path + ".cmd", "E_CMD", "命令不能是空数组")
    if "stop_cmd" in svc:
        _str_list(c, path + ".stop_cmd", svc["stop_cmd"], 16, code="E_CMD")
    if "health" in svc and (not isinstance(svc["health"], str) or not svc["health"].startswith("/") or len(svc["health"]) > 200):
        c.err(path + ".health", "E_HEALTH", "要写成以 / 开头的路径")
    if "start" in svc:
        _enum(c, path + ".start", svc["start"], SERVICE_START, "E_ENUM")
    if "stop_with_host" in svc:
        _bool(c, path + ".stop_with_host", svc["stop_with_host"])
    if "heartbeat_s" in svc:
        _int_range(c, path + ".heartbeat_s", svc["heartbeat_s"], 1, 3600)
    if "max_runtime_s" in svc:
        _int_range(c, path + ".max_runtime_s", svc["max_runtime_s"], 1, 604800)
    return svc


def _panel(c, path, panel):
    panel = _object(c, path, panel, "plugin.panel", required=("entry",), allow_none=True)
    if panel is None:
        return None
    if "entry" in panel:
        _rel(c, path + ".entry", panel["entry"], (".html", ".htm"))
    if "tools" in panel:
        _str_list(c, path + ".tools", panel["tools"], 32, maxlen=128)
    return panel


def _events(c, path, events, kind):
    """返回 {类型: sense} (后面查回执用)."""
    seen = {}
    items = _list(c, path, events, 64)
    for i, ev in enumerate(items or []):
        p = "%s[%d]" % (path, i)
        ev = _object(c, p, ev, "plugin.world.events[]", required=("type", "sense"))
        if not ev:
            continue
        typ = ev.get("type")
        if "type" in ev:
            if not isinstance(typ, str) or not EVENT_TYPE.match(typ):
                c.err(p + ".type", "E_EVENT_TYPE", "事件类型只能是小写字母、数字和 _，字母开头，最多 32 个字")
                typ = None
            elif typ in seen:
                c.err(p + ".type", "E_DUPLICATE_EVENT", "事件类型重名: " + typ)
        sense = ev.get("sense")
        if "sense" in ev:
            if _enum(c, p + ".sense", sense, SENSES, "E_ENUM") and kind == "chat" and sense not in CHAT_SENSES:
                c.err(p + ".sense", "E_SENSE_NOT_ALLOWED", "chat 档 (只有文字) 只许 told / receipt / notice, 没有 saw / heard")
        if sense == "notice" and "urgency" in ev:
            c.err(p + ".urgency", "E_NOTICE_HAS_URGENCY", "notice 只给主人看, 不进她的上下文, 没有紧急程度")
        elif "urgency" in ev:
            _enum(c, p + ".urgency", ev["urgency"], URGENCIES, "E_ENUM")
        if typ and typ not in seen:
            seen[typ] = sense
    return seen


def _event_fields(c, path, events, perception, kind=None):
    visible = set(perception.get("visible") or []) if isinstance(perception, dict) and isinstance(perception.get("visible"), list) else set()
    hidden = set(perception.get("hidden") or []) if isinstance(perception, dict) and isinstance(perception.get("hidden"), list) else set()
    for i, ev in enumerate(events if isinstance(events, list) else []):
        if not isinstance(ev, dict) or "fields" not in ev:
            continue
        p = "%s[%d].fields" % (path, i)
        fields = _str_list(c, p, ev["fields"], 16, IDENT, "E_EVENT_FIELD", 32)
        if fields and len(set(fields)) != len(fields):
            c.err(p, "E_DUPLICATE", "字段重名")
        for j, f in enumerate(fields or []):
            fp = "%s[%d]" % (p, j)
            if not isinstance(f, str):
                continue
            if f in TRUST_FIELDS or (kind == "chat" and f in TRUST_FIELDS_CHAT):
                c.err(fp, "E_EVENT_FIELD_TRUST", "事件里不许带信任档/主人这类字段 (%s): 档由核心定, 不由插件说" % f)
            elif f in hidden:
                c.err(fp, "E_EVENT_FIELD_LEAKS_HIDDEN", "字段 %s 在 perception.hidden 里, 真值不能进事件" % f)
            elif f not in STD_FIELDS and f not in visible:
                c.err(fp, "E_EVENT_FIELD_NOT_VISIBLE", "字段 %s 没有写进 perception.visible (事件只能带她感知得到的)" % f)


def _params(c, path, params):
    if not isinstance(params, dict) or len(params) > 8:
        c.err(path, "E_PARAMS", "params 需要是最多 8 个参数的对象")
        return
    for name, spec in params.items():
        p = _j(path, str(name))
        if not isinstance(name, str) or not PARAM_NAME.match(name):
            c.err(p, "E_PARAM_NAME", "参数名只能是小写字母、数字和 _")
        spec = _object(c, p, spec, "plugin.world.actions[].params{}", required=("type",))
        if not spec:
            continue
        ptype = spec.get("type")
        if "type" in spec:
            _enum(c, p + ".type", ptype, PARAM_TYPES, "E_ENUM")
        if "required" in spec:
            _bool(c, p + ".required", spec["required"])
        if "max_len" in spec:
            if ptype != "string":
                c.err(p + ".max_len", "E_PARAM_MAXLEN", "只有 string 参数能写 max_len")
            else:
                _int_range(c, p + ".max_len", spec["max_len"], 1, 4000)
        if "values" in spec:
            if ptype != "enum":
                c.err(p + ".values", "E_PARAM_VALUES", "只有 enum 参数能写 values")
            else:
                _str_list(c, p + ".values", spec["values"], 32, maxlen=40)
        elif ptype == "enum":
            c.err(p + ".values", "E_MISSING", "enum 参数必须写 values")


def _actions(c, path, actions, prefix, events_by_type):
    seen = set()
    items = _list(c, path, actions, 64)
    for i, act in enumerate(items or []):
        p = "%s[%d]" % (path, i)
        act = _object(c, p, act, "plugin.world.actions[]", required=("name", "kind", "why"))
        if not act:
            continue
        name = act.get("name")
        if "name" in act:
            if not isinstance(name, str) or not ACTION_NAME.match(name):
                c.err(p + ".name", "E_ACTION_NAME", "动作名必须是 <前缀>_<动词>，小写字母、数字和 _")
            elif isinstance(prefix, str) and not name.startswith(prefix + "_"):
                c.err(p + ".name", "E_ACTION_PREFIX", "动作名必须以世界前缀 %s_ 开头" % prefix)
            elif name in seen:
                c.err(p + ".name", "E_DUPLICATE_ACTION", "动作名重名: " + name)
            if isinstance(name, str):
                seen.add(name)
        if "why" in act:
            _str(c, p + ".why", act["why"], "E_STR", 80)
        kind = act.get("kind")
        if "kind" in act:
            _enum(c, p + ".kind", kind, ("query", "act"), "E_ENUM")
        if kind == "act":
            if "outward" not in act:
                c.err(p + ".outward", "E_MISSING", "动世界的动作必须写明会不会让别人看到听到 (outward)")
            elif _bool(c, p + ".outward", act["outward"]) is None:
                pass
            receipt = act.get("receipt")
            if receipt is None:
                c.err(p + ".receipt", "E_ACT_NO_RECEIPT", "动世界的动作必须有回执事件: 没回执不算做了")
            elif not isinstance(receipt, str) or receipt not in events_by_type:
                c.err(p + ".receipt", "E_RECEIPT_UNKNOWN", "回执要指向 events 里声明过的事件类型")
            elif events_by_type[receipt] != "receipt":
                c.err(p + ".receipt", "E_RECEIPT_NOT_RECEIPT", "回执指向的事件 sense 必须是 receipt")
        elif kind == "query":
            if act.get("outward") is True:
                c.err(p + ".outward", "E_QUERY_OUTWARD", "只读的查询不能是 outward")
            elif "outward" in act and not isinstance(act["outward"], bool):
                c.err(p + ".outward", "E_NOT_BOOL", "需要布尔值 true / false")
            if act.get("receipt") is not None:
                c.err(p + ".receipt", "E_QUERY_HAS_RECEIPT", "只读的查询直接返回结果, 不写回执")
        if "params" in act:
            _params(c, p + ".params", act["params"])


def _perception(c, path, per, kind):
    per = _object(c, path, per, "plugin.world.perception")
    if per is None:
        return {}
    lists = {}
    for key in ("visible", "hidden"):
        if key in per:
            lists[key] = _str_list(c, _j(path, key), per[key], 32, IDENT, "E_PERCEPTION_NAME", 32) or []
            if len(set(lists[key])) != len(lists[key]):
                c.err(_j(path, key), "E_DUPLICATE", "名字重复")
    both = set(lists.get("visible", [])) & set(lists.get("hidden", []))
    if both:
        c.err(path, "E_PERCEPTION_OVERLAP", "同一个名字不能既交给她又当真值藏起来: " + ", ".join(sorted(both)))
    if "twin_fixtures" in per:
        items = _list(c, _j(path, "twin_fixtures"), per["twin_fixtures"], 8)
        for i, name in enumerate(items or []):
            _rel(c, "%s.twin_fixtures[%d]" % (path, i), name)
    return per


def _channel_fields(c, path, w, p):
    """chat 档才有的四项: trust_from / trust_max / proactive / caps. 返回要交叉核对的值."""
    out = {}
    if "trust_from" in w:
        out["trust_from"] = _enum(c, _j(path, "trust_from"), w["trust_from"], TRUST_FROM, "E_ENUM")
    if "trust_max" in w:
        out["trust_max"] = _enum(c, _j(path, "trust_max"), w["trust_max"], TRUST_LEVELS, "E_ENUM")
    if "proactive" in w:
        pa = _object(c, _j(path, "proactive"), w["proactive"], "plugin.world.proactive", required=("mode",))
        if pa:
            mode = _enum(c, _j(path, "proactive.mode"), pa.get("mode"), PROACTIVE_MODES, "E_ENUM") if "mode" in pa else None
            if "quota_per_day" in pa:
                if mode != "quota":
                    c.err(_j(path, "proactive.quota_per_day"), "E_QUOTA_NOT_ALLOWED", "只有 mode=quota 才写 quota_per_day")
                else:
                    _int_range(c, _j(path, "proactive.quota_per_day"), pa["quota_per_day"], 1, 100000)
            elif mode == "quota":
                c.err(_j(path, "proactive.quota_per_day"), "E_QUOTA_MISSING", "mode=quota 必须写每天配额 quota_per_day")
            if "reply_window_s" in pa:
                if mode != "reply_window":
                    c.err(_j(path, "proactive.reply_window_s"), "E_WINDOW_NOT_ALLOWED", "只有 mode=reply_window 才写 reply_window_s")
                else:
                    _int_range(c, _j(path, "proactive.reply_window_s"), pa["reply_window_s"], 1, 86400)
    if "caps" in w:
        caps = _object(c, _j(path, "caps"), w["caps"], "plugin.world.caps", required=("dm", "group"))
        if caps:
            for key in ("dm", "group", "needs_proxy"):
                if key in caps:
                    _bool(c, _j(path, "caps." + key), caps[key])
            out["group"] = caps.get("group")
            if caps.get("group") is True and "group_sees" not in caps:
                c.err(_j(path, "caps.group_sees"), "E_MISSING", "能进群的必须写群里能看到什么 (all / mention_only)")
            elif "group_sees" in caps:
                if caps.get("group") is False:
                    c.err(_j(path, "caps.group_sees"), "E_GROUP_SEES_NOT_ALLOWED", "不能进群的通道不写 group_sees")
                else:
                    _enum(c, _j(path, "caps.group_sees"), caps["group_sees"], GROUP_SEES, "E_ENUM")
            if "attachments" in caps:
                items = _list(c, _j(path, "caps.attachments"), caps["attachments"], 4)
                for i, a in enumerate(items or []):
                    _enum(c, "%s[%d]" % (_j(path, "caps.attachments"), i), a, ATTACHMENTS, "E_ENUM")
                if items and len(set(map(str, items))) != len(items):
                    c.err(_j(path, "caps.attachments"), "E_DUPLICATE", "附件类型重复")
            if "max_len" in caps:
                _int_range(c, _j(path, "caps.max_len"), caps["max_len"], 1, 100000)
    return out


def _trust_rules(c, w_path, perm_path, chan, perms):
    trust_from, trust_max = chan.get("trust_from"), chan.get("trust_max")
    owner = None
    answer = None
    ch = perms.get("channel") if isinstance(perms, dict) else None
    if isinstance(ch, dict):
        owner = ch.get("act_as_owner") if isinstance(ch.get("act_as_owner"), bool) else None
        answer = ch.get("answer_cards") if isinstance(ch.get("answer_cards"), bool) else None
    owner = bool(owner)
    answer = bool(answer)
    if trust_from == "none" and trust_max not in (None, "stranger"):
        c.err(_j(w_path, "trust_max"), "E_TRUST_FROM_NONE_MAX", "平台不给发信人身份 (trust_from=none) 时, 上限只能是 stranger")
    if trust_from == "local_loopback" and trust_max == "owner_remote":
        c.err(_j(w_path, "trust_max"), "E_TRUST_FROM_LOOPBACK_MAX", "本机回环入口没有发信人身份, 上限最高 acquaintance")
    if trust_from == "paired_owner" and chan.get("group") is True:
        c.err(_j(w_path, "caps.group"), "E_PAIRED_OWNER_GROUP", "paired_owner 的通道只有一个对端, 不能进群")
    if trust_max == "owner_remote" and not owner:
        c.err(_j(w_path, "trust_max"), "E_TRUST_MAX_NEEDS_OWNER_PERMISSION",
              "申请本人档 (owner_remote) 必须在 permissions.channel.act_as_owner 声明, 让用户在权限卡上单独点头")
    if owner and trust_max is not None and trust_max != "owner_remote":
        c.err(perm_path + ".act_as_owner", "E_OWNER_PERMISSION_WITHOUT_TRUST",
              "声明了 '可以当本人', 但 world.trust_max 没申请本人档 (owner_remote): 权限和档要一致")
    if answer and not owner:
        c.err(perm_path + ".answer_cards", "E_ANSWER_CARDS_NEEDS_OWNER", "不是本人的通道不能答复申请卡片: answer_cards 必须同 act_as_owner 一起")


def _world(c, w, plugin, perms, svc, panel):
    path = "plugin.world"
    w = _object(c, path, w, "plugin.world", required=("kind", "prefix", "events", "actions"), allow_none=True)
    if w is None:
        return
    kind = _enum(c, path + ".kind", w.get("kind"), WORLD_KINDS, "E_ENUM") if "kind" in w else None
    prefix = w.get("prefix")
    if "prefix" in w and (not isinstance(prefix, str) or not PREFIX.match(prefix)):
        c.err(path + ".prefix", "E_PREFIX", "前缀只能是小写字母和数字, 字母开头, 2～12 个字符")
        prefix = None
    events = w.get("events")
    events_by_type = _events(c, path + ".events", events, kind) if "events" in w else {}
    perception = _perception(c, path + ".perception", w["perception"], kind) if "perception" in w else {}
    if "events" in w:
        _event_fields(c, path + ".events", events, perception, kind)
    if "actions" in w:
        _actions(c, path + ".actions", w["actions"], prefix, events_by_type)
    if "env_prompt" in w:
        _rel(c, path + ".env_prompt", w["env_prompt"], (".md",))
    if "leave" in w:
        lv = _object(c, path + ".leave", w["leave"], "plugin.world.leave")
        if lv and "ack_timeout_s" in lv:
            _int_range(c, path + ".leave.ack_timeout_s", lv["ack_timeout_s"], 1, 30)
    spectate = None
    if "spectate" in w:
        spectate = _object(c, path + ".spectate", w["spectate"], "plugin.world.spectate", required=("panel",))
        if spectate and "panel" in spectate:
            if _rel(c, path + ".spectate.panel", spectate["panel"], (".html", ".htm")):
                entry = panel.get("entry") if isinstance(panel, dict) else None
                if entry != spectate["panel"]:
                    c.err(path + ".spectate.panel", "E_SPECTATE_PANEL_MISMATCH", "旁观面板必须同时是 plugin.panel.entry")
    # ---- 按 kind 分开的规则 ----
    hidden = perception.get("hidden") if isinstance(perception, dict) else None
    fixtures = perception.get("twin_fixtures") if isinstance(perception, dict) else None
    if kind in ("space", "game"):
        if not perception:
            c.err(path + ".perception", "E_PERCEPTION_REQUIRED", "%s 档必须说明她能看到什么 (perception)" % kind)
        else:
            if not perception.get("visible"):
                c.err(path + ".perception.visible", "E_VISIBLE_REQUIRED", "%s 档必须列出交给她的字段 (visible)" % kind)
            if not fixtures:
                c.err(path + ".perception.twin_fixtures", "E_TWIN_REQUIRED", "%s 档必须带双胞胎夹具" % kind)
        if spectate is None:
            c.err(path + ".spectate", "E_SPECTATE_REQUIRED", "%s 档必须有旁观面板 (spectate)" % kind)
    elif kind == "chat":
        if hidden and not fixtures:
            c.err(path + ".perception.twin_fixtures", "E_TWIN_REQUIRED", "声明了 hidden 真值就必须带双胞胎夹具")
    chat_keys = ("trust_from", "trust_max", "proactive", "caps")
    perms_channel = perms.get("channel") if isinstance(perms, dict) else None
    perm_path = "plugin.permissions.channel"
    if kind == "chat":
        for key in chat_keys:
            if key not in w:
                c.err(_j(path, key), "E_CHANNEL_FIELD_MISSING", "chat 档 (渠道) 必须声明 " + key)
        chan = _channel_fields(c, path, w, plugin)
        _trust_rules(c, path, perm_path, chan, perms)
        builtin = plugin.get("tier") == "builtin"
        if not builtin:
            if not svc:
                c.err("plugin.service", "E_CHAT_NEEDS_SERVICE", "chat 档的适配器是常驻的服务, 必须写 service (宿主自带同进程的适配器写 tier=builtin)")
            if not (perms.get("network") or perms.get("local")):
                c.err("plugin.permissions.network", "E_CHAT_NEEDS_ENDPOINT", "渠道要连哪里必须声明: network 或 local 至少写一个")
        elif svc:
            c.err("plugin.service", "E_BUILTIN_HAS_SERVICE", "builtin 跑在宿主进程里, 没有 service")
    elif kind in ("space", "game"):
        for key in chat_keys:
            if key in w:
                c.err(_j(path, key), "E_CHANNEL_FIELD_NOT_ALLOWED", "%s 是渠道 (chat 档) 才有的声明" % key)
        if isinstance(perms_channel, dict) or "channel" in (perms or {}):
            c.err(perm_path, "E_CHANNEL_PERM_NOT_CHAT", "permissions.channel 只有 chat 档世界能写")


def _channel_permissions(c, perms, world):
    ch = perms.get("channel") if isinstance(perms, dict) else None
    if ch is None and "channel" not in (perms or {}):
        return
    path = "plugin.permissions.channel"
    ch = _object(c, path, ch, "plugin.permissions.channel")
    if ch:
        for key in ("act_as_owner", "answer_cards"):
            if key in ch:
                _bool(c, _j(path, key), ch[key])


def _plugin(c, plugin):
    path = "plugin"
    plugin = _object(c, path, plugin, "plugin", required=("schema", "version", "author", "license", "host", "tier", "permissions"))
    if plugin is None:
        return
    if "schema" in plugin and (isinstance(plugin["schema"], bool) or plugin["schema"] != SCHEMA):
        c.err("plugin.schema", "E_SCHEMA", "格式号必须是 %d (旧宿主见到不认识的格式号会拒绝, 不会当成没有权限声明的老包)" % SCHEMA)
    if "version" in plugin and (not isinstance(plugin["version"], str) or not VERSION.match(plugin["version"])):
        c.err("plugin.version", "E_VERSION", "版本写成 1.0.0 或 1.0.0-beta")
    if "author" in plugin:
        _str(c, "plugin.author", plugin["author"], "E_STR", 80)
    if "license" in plugin:
        _str(c, "plugin.license", plugin["license"], "E_STR", 80)
    if "host" in plugin and (not isinstance(plugin["host"], str) or not HOST_RANGE.match(plugin["host"].strip())):
        c.err("plugin.host", "E_HOST_RANGE", "写成版本范围, 如 >=0.4 或 ^0.4.0")
    if "tier" in plugin:
        _enum(c, "plugin.tier", plugin["tier"], PLUGIN_TIERS, "E_ENUM")
    perms = _permissions(c, "plugin.permissions", plugin["permissions"]) if "permissions" in plugin else {}
    _channel_permissions(c, perms, plugin.get("world"))
    svc = _service(c, "plugin.service", plugin["service"]) if "service" in plugin else None
    panel = _panel(c, "plugin.panel", plugin["panel"]) if "panel" in plugin else None
    for key, types in RESERVED_TYPES.items():
        if key in plugin and plugin[key] is not None and not isinstance(plugin[key], types):
            c.err(_j("plugin", key), "E_TYPE", "类型不对 (本步只查类型)")
    if "world" in plugin and plugin["world"] is not None:
        _world(c, plugin["world"], plugin, perms, svc, panel)
    elif isinstance(perms, dict) and "channel" in perms:
        c.err("plugin.permissions.channel", "E_CHANNEL_PERM_NOT_CHAT", "permissions.channel 只有 chat 档世界能写")


def validate_plugin_manifest(manifest, package_dir=None):
    """清单自己对不对. 返回 Issue 列表; `has_errors()` 为 False 才算合格. 给了 package_dir 就核对引用的文件在不在、有没有越出包目录."""
    c = _Ctx(package_dir)
    if not isinstance(manifest, dict):
        c.err("", "E_NOT_OBJECT", "清单不是对象")
        return c.issues
    top = _object(c, "", manifest, "")
    if top is None:
        return c.issues
    if "id" not in top or not isinstance(top["id"], str) or not ID.match(top["id"]):
        c.err("id", "E_ID", "id 只能是小写字母、数字和 -, 最多 48 个字符")
    if "name" not in top:
        c.err("name", "E_MISSING", "缺少必填字段 name")
    else:
        _str(c, "name", top["name"], "E_STR", 80)
    if "description" in top and (not isinstance(top["description"], str) or len(top["description"]) > 1000):
        c.err("description", "E_STR", "description 要是不超过 1000 字的字符串")
    if "ui" in top and top["ui"] is not None:
        _rel(c, "ui", top["ui"], (".html", ".htm"))
    if "mcp" in top and top["mcp"] is not None and not isinstance(top["mcp"], dict):
        c.err("mcp", "E_TYPE", "mcp 要是对象或 null")
    if "files" in top and not isinstance(top["files"], dict):
        c.err("files", "E_TYPE", "files 要是对象")
    if "plugin" not in top:
        c.warn("plugin", "W_NO_PLUGIN_BLOCK", "没有 plugin 块: 这是老包, 照老规矩装, 不按本契约检查")
        return c.issues
    _plugin(c, top["plugin"])
    return c.issues


def _channel_perm_values(manifest):
    try:
        ch = manifest["plugin"]["permissions"].get("channel") or {}
        return ch.get("act_as_owner") is True, ch.get("answer_cards") is True
    except (KeyError, TypeError, AttributeError):
        return False, False


def separate_permissions(manifest):
    """装的时候要单独列给用户看的权限 (固定顺序). 不合法的清单返回空 (先过 validate)."""
    owner, answer = _channel_perm_values(manifest)
    return [name for name, on in (("act_as_owner", owner), ("answer_cards", answer)) if on]


def policy_issues(manifest, source):
    """这个来路能不能拿这些声明. `source` 由宿主按来路填, 清单不能自称."""
    if source not in SOURCES:
        raise ValueError("source 只能是 " + " / ".join(SOURCES))
    out = []
    if source == "bundled" or not isinstance(manifest, dict):
        return out
    plugin = manifest.get("plugin")
    if not isinstance(plugin, dict):
        return out
    if plugin.get("tier") == "builtin":
        out.append(Issue("plugin.tier", "E_BUILTIN_NOT_BUNDLED",
                         "builtin 跑在宿主进程里, 信任最高, 只给自带的 (bundled) 插件", "error"))
    world = plugin.get("world")
    owner, answer = _channel_perm_values(manifest)
    if isinstance(world, dict) and world.get("trust_max") == "owner_remote":
        out.append(Issue("plugin.world.trust_max", "E_THIRD_PARTY_OWNER",
                         "第三方渠道本版最高到熟人 (acquaintance), 不开放本人档", "error"))
    if owner:
        out.append(Issue("plugin.permissions.channel.act_as_owner", "E_THIRD_PARTY_OWNER",
                         "'这条通道可以当我本人' 本版不对第三方开放", "error"))
    if answer:
        out.append(Issue("plugin.permissions.channel.answer_cards", "E_THIRD_PARTY_ANSWER_CARDS",
                         "'可以在这条通道上答复申请卡片' 本版不对第三方开放", "error"))
    return out


def find_conflicts(manifest, installed):
    """新清单和已装清单的世界前缀、动作名冲突 (同一个 id 是更新, 不算冲突). 重名拒装."""
    out = []
    new_world = ((manifest or {}).get("plugin") or {}).get("world") if isinstance(manifest, dict) else None
    if not isinstance(new_world, dict):
        return out
    new_id = manifest.get("id")
    prefix = new_world.get("prefix")
    new_actions = {}
    for i, a in enumerate(new_world.get("actions") or []):
        if isinstance(a, dict) and isinstance(a.get("name"), str):
            new_actions[a["name"]] = i
    for other in installed or []:
        if not isinstance(other, dict) or other.get("id") == new_id:
            continue
        ow = (other.get("plugin") or {}).get("world") if isinstance(other.get("plugin"), dict) else None
        if not isinstance(ow, dict):
            continue
        if prefix is not None and ow.get("prefix") == prefix:
            out.append(Issue("plugin.world.prefix", "E_PREFIX_TAKEN", "前缀 %s 已被 %s 占用" % (prefix, other.get("id")), "error"))
        for a in ow.get("actions") or []:
            if isinstance(a, dict) and a.get("name") in new_actions:
                out.append(Issue("plugin.world.actions[%d].name" % new_actions[a["name"]], "E_ACTION_NAME_TAKEN",
                                 "动作名 %s 已被 %s 占用" % (a["name"], other.get("id")), "error"))
    return out

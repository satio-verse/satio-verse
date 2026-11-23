"""网络插件·消息和参数 (2026-11-11 "网络"插件干跑的一块; 对着《网络动作接口 v0.2》写): 请求怎么解、应答怎么编、net.start 的参数怎么生成
和复查、同意令牌怎么核对.

只在 wm/net/ 里用, 项目别处不引用; 只用标准库 (和同目录的 recipe.py、config_gen.py); 全是纯函数: 不读写文件、不起进程、不联网、
不读环境变量、不读时钟 (要用的时间由调用方传进来).

- 一行一个 JSON 对象 (UTF-8, 以换行结束), 一条请求最长 64 KB (MAX_REQUEST_BYTES, 不算结尾的换行). encode 编; decode_request 解,
  它不抛异常, 返回 Request (解出来的请求) 或 Rejection (该回的错误, .reply() 就是那条应答). 这一行"解不成一个 JSON 对象"都是 E_BAD_REQUEST:
  超长 (最先查, 不解析, 应答里的 id 是 null)、开头有 BOM、行尾以外的地方有回车换行、不是合法的 UTF-8 或 JSON、不是对象、任何一层有重复的键、
  NaN 或 Infinity、数不是整数 (带小数点或指数) 或绝对值超过 2^53-1 (MAX_SAFE_INT)、嵌套超过 8 层 (MAX_DEPTH, 最外面的对象算第 1 层).
  后三项 (数、嵌套、BOM) 是契约第 1 节〔补 2〕写死的: 两种语言的解析器在这几处最容易判得不一样; token 在这一行里, 它里面的数也一样处理.
  顶层有不认识的键也是 E_BAD_REQUEST; v 不对是 E_VERSION; 动作不在名单上是 E_UNKNOWN_ACTION.
  检查的先后: 长度 -> 能不能解 (含数和嵌套) -> v -> 顶层的键 -> id -> 动作 -> dry_run -> params_json (v 对不上时, 后面的键认不认识已经没意义, 所以 v 排在前面;
  所以 "v": 1.0 是 E_BAD_REQUEST, "v": 2 和 "v": "1" 才是 E_VERSION).
- LineBuffer 把字节流切成一行一行 (纯的: 喂字节进去、取出凑齐的行), 超长的行给出恰好 MAX_REQUEST_BYTES + 1 个字节, decode_request
  看到就回 E_BAD_REQUEST, 后半截丢掉, 连接不用断.
- 参数以一段 JSON 文本传 (params_json 是字符串): 接收方先对原文的 UTF-8 字节算 SHA-256 (params_fingerprint), 再解析
  (parse_json_strict: 重复的键拒; 数只许整数、绝对值不超过 2^53-1, 带小数点或指数的拒; 嵌套最多 8 层; 开头有 BOM 也拒. 要求和请求那一行一样,
  不合就是 E_PARAMS, fields 写 params_json). start_params 生成 net.start 的参数 (规范化的一行 JSON, 规则同
  config_gen.render), parse_start_params 是代办一侧的白名单复查 (契约 4.1 第 3 步): 进程名、配方名、上游地址、端口、时长直接用
  recipe.py 里现成的检查, 这里不另写一份规则.
- check_token 照契约第 2 节核对同意令牌 (不发令牌, 那是宿主的事): 形状 (plugin、requested_by 是插件编号的写法, plugin 不能是 owner 或 ayane, issuer 是字母数字加 -,
  两个时间是整数)、
  时间 (now 向下取整到秒记作 t, issued_at <= t <= expires_at 有效, 两头都含)、用过没有、动作和指纹. 它只回一个码; "形状对不对"另有 token_shape_ok
  (提权一侧的审计要在形状通过以后才写令牌号和谁申请的).
- mask_output_line: 给"带打过码的最后几行输出"用 (E_START_FAILED); 核心输出里的地址一律遮掉.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

from . import config_gen
from .recipe import Recipe, Upstream, host_problems, port_problems, show
from .recipe import problems as recipe_problems

VERSION = 1
MAX_REQUEST_BYTES = 65536   # 一条请求最长 64 KB (不算结尾的换行)
MAX_SAFE_INT = 2 ** 53 - 1  # 数的绝对值最大到这个 (9007199254740991, 别的语言的解析器也能原样表示的最大整数; 契约第 1 节〔补 2〕)
MAX_DEPTH = 8               # 对象和数组合起来最多这么多层, 最外面的对象算第 1 层 (契约第 1 节〔补 2〕)
_BOM_BYTES = bytes([0xEF, 0xBB, 0xBF])        # 开头的 BOM (UTF-8 写法); 请求这一行和 params_json 的开头都不许有
_BOM_TEXT = _BOM_BYTES.decode("utf-8")        # (U+FEFF)

# ---- 动作 ---------------------------------------------------------------------------------------------------------
NET_START = "net.start"       # 只在代办启动时那一条请求里认 (会话通道上不认)
NET_STOP = "net.stop"
NET_STATUS = "net.status"
NET_RESTORE = "net.restore"
HEARTBEAT = "heartbeat"
ACTIONS = (NET_START, NET_STOP, NET_STATUS, NET_RESTORE, HEARTBEAT)
SESSION_ACTIONS = (NET_STOP, NET_STATUS, NET_RESTORE, HEARTBEAT)   # 看护方的会话通道上认的

TOP_KEYS = ("v", "id", "action", "dry_run", "token", "params_json")   # 请求顶层只许这几个键

# ---- 错误码 (契约第 5 节; 注释里是谁回的) ---------------------------------------------------------------------------------
E_VERSION = "E_VERSION"                    # 代办、看护方
E_BAD_REQUEST = "E_BAD_REQUEST"            # 代办、看护方
E_UNKNOWN_ACTION = "E_UNKNOWN_ACTION"      # 代办、看护方
E_NOT_ELEVATED = "E_NOT_ELEVATED"          # 代办
E_TOKEN_MISSING = "E_TOKEN_MISSING"        # 代办
E_TOKEN_EXPIRED = "E_TOKEN_EXPIRED"        # 代办
E_TOKEN_USED = "E_TOKEN_USED"              # 代办
E_TOKEN_MISMATCH = "E_TOKEN_MISMATCH"      # 代办
E_PARAMS = "E_PARAMS"                      # 代办、看护方
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"    # 代办
E_CORE_UNKNOWN = "E_CORE_UNKNOWN"          # 代办
E_CORE_MISSING = "E_CORE_MISSING"          # 代办
E_CORE_HASH = "E_CORE_HASH"                # 代办
E_ALREADY_RUNNING = "E_ALREADY_RUNNING"    # 代办
E_USER_DECLINED = "E_USER_DECLINED"        # 代办
E_CONFIRM_TIMEOUT = "E_CONFIRM_TIMEOUT"    # 代办
E_START_FAILED = "E_START_FAILED"          # 代办
E_BUSY = "E_BUSY"                          # 代办、看护方
E_INTERNAL = "E_INTERNAL"                  # 代办、看护方
E_NOT_PERMITTED = "E_NOT_PERMITTED"        # 宿主
E_CARD_DECLINED = "E_CARD_DECLINED"        # 宿主
E_UAC_DECLINED = "E_UAC_DECLINED"          # 宿主

GUARDIAN_CODES = (E_VERSION, E_BAD_REQUEST, E_UNKNOWN_ACTION, E_PARAMS, E_BUSY, E_INTERNAL)   # 看护方会回的
HOST_CODES = (E_NOT_PERMITTED, E_CARD_DECLINED, E_UAC_DECLINED)                               # 只有宿主会回的
AGENT_CODES = (E_VERSION, E_BAD_REQUEST, E_UNKNOWN_ACTION, E_NOT_ELEVATED, E_TOKEN_MISSING, E_TOKEN_EXPIRED, E_TOKEN_USED,
               E_TOKEN_MISMATCH, E_PARAMS, E_CONFIG_MISMATCH, E_CORE_UNKNOWN, E_CORE_MISSING, E_CORE_HASH, E_ALREADY_RUNNING,
               E_USER_DECLINED, E_CONFIRM_TIMEOUT, E_START_FAILED, E_BUSY, E_INTERNAL)          # 代办会回的
ERROR_CODES = AGENT_CODES + HOST_CODES                                                          # 契约第 5 节的全部

# ---- 停的原因 -----------------------------------------------------------------------------------------------------
STOP_REASONS = ("user", "lease_returned", "selftest_failed", "game_exited", "host_exit")   # net.stop 的 reason 只许这几个 (契约 4.2)
REASON_HEARTBEAT_LOST = "heartbeat_lost"     # 契约 4.5: 超过 grace_s 没收到心跳
REASON_MAX_TIME = "max_time"                 # 到了 deadline_at
REASON_CORE_EXITED = "core_exited"           # 核心自己退了 (状态记成 failed)
REASON_SYSTEM_SHUTDOWN = "system_shutdown"   # 关机、注销
REASON_RESTORE = "restore"                   # 会话内的 net.restore 把会话收掉了 (契约没列, 这里补的)
REASON_GUARDIAN_ERROR = "guardian_error"     # 看护方自己出了故障 (读不了核心的输出), 按故障收尾, 状态记成 failed (第 2 轮评审补的)

# ---- net.start 的参数 (契约 4.1) ----------------------------------------------------------------------------------------
TEMPLATE = 1
START_KEYS = ("template", "recipe", "upstream", "core", "config_sha256", "heartbeat")
RECIPE_KEYS = ("name", "processes", "max_minutes")
UPSTREAM_KEYS = ("host", "port")
CORE_KEYS = ("id", "version")
HEARTBEAT_KEYS = ("interval_s", "grace_s")
INTERVAL_RANGE = (1, 30)     # 心跳间隔 (秒)
GRACE_RANGE = (10, 300)      # 心跳宽限 (秒)
DEFAULT_INTERVAL_S = 5
DEFAULT_GRACE_S = 30
_CORE_WORD = re.compile(r"[A-Za-z0-9._-]{1,32}")     # core.id、core.version: 1-32 个字符, 只许 ASCII 字母、数字和 . _ -
_SHA256 = re.compile(r"[0-9a-f]{64}")                # 64 位小写十六进制
_SHA256_ANY = re.compile(r"[0-9a-fA-F]{64}")
_TOKEN_ID = re.compile(r"[0-9a-fA-F]{32,128}")       # 令牌号: 随机数, 至少 128 位 (32 个十六进制字符), 十六进制
_SESSION = re.compile(r"[0-9a-f]{32}")               # 会话号: 随机的 128 位十六进制
# 令牌里三个字符串的写法 (契约第 2 节〔补 2〕: 确认窗是提权一侧的窗口, 要显示谁申请的, 不能照抄换行、超长的串、冒充系统提示的句子). 一律用 fullmatch:
# 正则里的 $ 会放过结尾的换行, 带换行的串必须被拒.
_PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")  # plugin、requested_by: 插件编号的写法. 出处 wm/agent/extensions.py 的 ID (^[a-z0-9][a-z0-9-]{0,47}$);
#                                                      wm/net 不许导入 wm.agent, 所以在这里另写一份, 登记表那边改了要回来改. owner、ayane 也符合它
_ISSUER = re.compile(r"[A-Za-z0-9-]{1,64}")          # issuer: 发令牌的宿主实例的编号, 字母、数字、-, 1-64 个字符
# owner、ayane 是令牌里 requested_by 的两个特殊写法 ("用户自己"、"她"), 不能拿来当插件编号: 令牌的 plugin 写成这两个词, 按形状不对拒 (契约第 2 节〔补 4〕: 提权一侧不指望
# 普通权限的扩展登记表来保证这两个词没被占; requested_by 里它们照旧合法).
RESERVED_PLUGIN_IDS = ("owner", "ayane")

# ---- 同意令牌 (契约第 2 节) ------------------------------------------------------------------------------------------------
TOKEN_KEYS = ("id", "action", "params_sha256", "plugin", "requested_by", "issued_at", "expires_at", "issuer")
TOKEN_ACTIONS = (NET_START, NET_RESTORE)   # 令牌只批这两个 (其余动作不要令牌)
TOKEN_MAX_TTL_S = 120

PIPE_PREFIX = "\\\\.\\pipe\\wm-net-"       # 看护方的会话通道: \\.\pipe\wm-net-<会话号>


# ---- 消息 ---------------------------------------------------------------------------------------------------------

def encode(obj: object) -> bytes:
    """一行 JSON, UTF-8, 以换行结束 (紧凑分隔符, 非 ASCII 原样不转义; 字符串里有写不成 UTF-8 的孤立代理字符时退回全转义, 照样是一行)."""
    try:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return (text + "\n").encode("utf-8")
    except UnicodeEncodeError:
        return (json.dumps(obj, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")


def ok(request_id: object, result: dict) -> dict:
    """成功的应答: {"v": 1, "id": 请求号, "ok": true, "result": {...}}."""
    return {"v": VERSION, "id": request_id, "ok": True, "result": result}


def fail(request_id: object, code: str, message: str, fields: list[str] | tuple[str, ...] | None = None) -> dict:
    """失败的应答: {"v": 1, "id": 请求号, "ok": false, "error": {"code": ..., "message": 给人看的中文[, "fields": [...]]}}.
    fields 只在给了的时候才带 (E_PARAMS 会给: 写明是哪几项)."""
    error: dict = {"code": code, "message": message}
    if fields is not None:
        error["fields"] = list(fields)
    return {"v": VERSION, "id": request_id, "ok": False, "error": error}


@dataclass(frozen=True)
class Request:
    """解出来的请求. token 是请求里的原样 (None = 没带; 令牌长什么样由 check_token 判); params_json 是原文 (None = 没带)."""
    id: str
    action: str
    dry_run: bool
    token: object
    params_json: str | None


@dataclass(frozen=True)
class Rejection:
    """该回的错误: 请求没过 decode_request. id 是请求里的号 (没有或不是字符串就是 None)."""
    code: str
    message: str
    id: str | None = None

    def reply(self) -> dict:
        return fail(self.id, self.code, self.message)


class _Duplicate(ValueError):
    """JSON 里同一个对象出现了两个一样的键."""


class ParamsError(ValueError):
    """参数文本没过严格解析 (小数、过长的数字、NaN、重复的键...)."""


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise _Duplicate(key)
        out[key] = value
    return out


def _no_constant(name: str) -> object:
    raise ParamsError(f"不认 {name}（JSON 里没有这个）")


def _no_float(text: str) -> object:
    raise ParamsError(f"数 {text[:20]} 不收：只许整数，写法不带小数点和指数")


_SAFE_INT_DIGITS = len(str(MAX_SAFE_INT))     # 16 位


def _safe_int(text: str) -> int:
    """整数的绝对值不超过 2^53-1. JSON 的整数没有前导零, 位数超过 16 位的一定超了: 不去转换一个几万位的数."""
    if len(text.lstrip("-")) <= _SAFE_INT_DIGITS:
        value = int(text)
        if abs(value) <= MAX_SAFE_INT:
            return value
    raise ParamsError(f"整数 {text[:20]} 超出范围：绝对值不超过 {MAX_SAFE_INT}")


def _depth(obj: object) -> int:
    """对象和数组一共嵌了几层 (最外面的算第 1 层; 里面没有对象和数组是 0). 不用递归, 几万层也不会爆栈."""
    deepest = 0
    pending: list[tuple[object, int]] = [(obj, 1)]
    while pending:
        item, level = pending.pop()
        if type(item) is dict:
            children = item.values()
        elif type(item) is list:
            children = item
        else:
            continue
        deepest = max(deepest, level)
        pending.extend((child, level + 1) for child in children)
    return deepest


def _loads_strict(text: str) -> object:
    """json.loads 加上契约要的规则: 重复的键、小数和指数写法、超范围的整数、NaN 和 Infinity 都抛 (_Duplicate 或 ParamsError); 嵌套太深 (超过解析器
    自己的递归上限) 抛 RecursionError, 不合法的 JSON 抛 ValueError; 调用方各自翻成自己的错误码. 嵌套够不够 8 层由调用方再用 _depth 查."""
    return json.loads(text, object_pairs_hook=_no_duplicates, parse_float=_no_float, parse_int=_safe_int, parse_constant=_no_constant)


def parse_json_strict(text: str) -> object:
    """params_json 的解析 (契约第 1 节〔补 2〕, 要求和请求那一行一样): 不许有重复的键 (任何一层); 数只许整数、绝对值不超过 2^53-1 (带小数点或
    指数写法的、NaN、Infinity、超范围的都拒); 嵌套最多 8 层 (最外面的对象算第 1 层); 开头有 BOM 也拒. 出问题抛 ParamsError (带中文原因)."""
    if text.startswith(_BOM_TEXT):
        raise ParamsError("开头不许有 BOM")
    try:
        obj = _loads_strict(text)
    except _Duplicate as exc:
        raise ParamsError(f"有重复的键 {show(exc.args[0])}") from None
    except ParamsError:
        raise
    except RecursionError:
        raise ParamsError(f"嵌套太深（最多 {MAX_DEPTH} 层）") from None
    except ValueError:
        raise ParamsError("不是合法的 JSON") from None
    if _depth(obj) > MAX_DEPTH:
        raise ParamsError(f"嵌套太深（最多 {MAX_DEPTH} 层）")
    return obj


def _strip_eol(raw: bytes) -> bytes:
    """去掉行尾的换行: 认换行 (LF) 或回车加换行 (CR LF). 单独的结尾回车不是行尾, 留着, 后面按"行尾以外的地方出现回车"拒."""
    if raw.endswith(b"\r\n"):
        return raw[:-2]
    if raw.endswith(b"\n"):
        return raw[:-1]
    return raw


def decode_request(line: bytes | str) -> Request | Rejection:
    """一行请求 -> Request (解出来的请求) 或 Rejection (该回的错误); 不抛异常. line 可以带结尾的换行 (\\n 或 \\r\\n), 也可以不带."""
    try:
        return _decode(line)
    except Exception:  # noqa: BLE001 - 解请求不许抛: 任何意外都按"不是合法的请求"算
        return Rejection(E_BAD_REQUEST, "请求没法解析")


def _decode(line: object) -> Request | Rejection:
    if isinstance(line, str):
        try:
            raw = line.encode("utf-8")
        except UnicodeEncodeError:
            return Rejection(E_BAD_REQUEST, "请求不是合法的 UTF-8 文本")
    elif isinstance(line, (bytes, bytearray, memoryview)):
        raw = bytes(line)
    else:
        return Rejection(E_BAD_REQUEST, "请求必须是一行文本")
    raw = _strip_eol(raw)
    if len(raw) > MAX_REQUEST_BYTES:
        return Rejection(E_BAD_REQUEST, f"请求超过 {MAX_REQUEST_BYTES // 1024} KB（一条最长 {MAX_REQUEST_BYTES} 字节）")
    if raw.startswith(_BOM_BYTES):
        return Rejection(E_BAD_REQUEST, "请求开头不许有 BOM")
    if b"\n" in raw or b"\r" in raw:
        return Rejection(E_BAD_REQUEST, "一行只能放一个 JSON 对象，行尾以外的地方不许有回车换行")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Rejection(E_BAD_REQUEST, "请求不是合法的 UTF-8 文本")
    try:
        obj = _loads_strict(text)         # 数的写法和范围这一行里任何地方都查 (token 里面的也算), 不合就是解不出来
    except _Duplicate as exc:
        return Rejection(E_BAD_REQUEST, f"请求里有重复的键 {show(exc.args[0])}")
    except ParamsError as exc:
        return Rejection(E_BAD_REQUEST, f"请求里有不收的写法：{exc}")
    except RecursionError:
        return Rejection(E_BAD_REQUEST, f"请求嵌套太深（最多 {MAX_DEPTH} 层）")
    except ValueError:
        return Rejection(E_BAD_REQUEST, "请求不是合法的 JSON")
    if type(obj) is not dict:
        return Rejection(E_BAD_REQUEST, "请求必须是一个 JSON 对象")
    if _depth(obj) > MAX_DEPTH:
        return Rejection(E_BAD_REQUEST, f"请求嵌套太深（最多 {MAX_DEPTH} 层，最外面的对象算第 1 层）")
    given = obj.get("id")
    request_id = given if type(given) is str else None
    version = obj.get("v")
    if type(version) is not int or version != VERSION:
        return Rejection(E_VERSION, f"协议版本对不上（这里认 v={VERSION}，收到的是 {show(version)}）", request_id)
    extra = [key for key in obj if key not in TOP_KEYS]
    if extra:
        names = "、".join(show(key) for key in extra[:6])
        return Rejection(E_BAD_REQUEST, f"请求顶层有不认识的键 {names}（只认 {'、'.join(TOP_KEYS)}）", request_id)
    if type(given) is not str or not given:
        return Rejection(E_BAD_REQUEST, "缺少 id，或者 id 不是不空的字符串", request_id)
    action = obj.get("action")
    if type(action) is not str:
        return Rejection(E_BAD_REQUEST, "缺少 action，或者 action 不是字符串", request_id)
    if action not in ACTIONS:
        return Rejection(E_UNKNOWN_ACTION, f"不认识的动作 {show(action)}", request_id)
    dry_run = obj.get("dry_run", False)
    if type(dry_run) is not bool:
        return Rejection(E_BAD_REQUEST, "dry_run 必须是 true 或 false", request_id)
    params_json = obj.get("params_json")
    if params_json is not None:
        if type(params_json) is not str:
            return Rejection(E_BAD_REQUEST, "params_json 必须是字符串（一段 JSON 文本）", request_id)
        try:
            params_json.encode("utf-8")
        except UnicodeEncodeError:
            return Rejection(E_BAD_REQUEST, "params_json 不是合法的 UTF-8 文本", request_id)
    return Request(given, action, dry_run, obj.get("token"), params_json)


class LineBuffer:
    """字节流切成一行一行 (纯的: feed 喂字节, 返回这次凑齐的行, 不含换行, 结尾的 \\r 也去掉). 半行留着等下一块.

    超长的行 (超过 limit 个字节) 只给出 limit + 1 个字节 (前 limit 个字节加一个 NUL; decode_request 看到长度超限就回 E_BAD_REQUEST,
    结尾放 NUL 是为了不让它被当成 \\r 剥掉而短回 limit 个字节), 它的后半截一直丢到这行的换行为止, 之后照常切: 不会因为一行超长就乱掉,
    内存也不会跟着涨."""

    def __init__(self, limit: int = MAX_REQUEST_BYTES) -> None:
        self._limit = limit
        self._buf = bytearray()
        self._skipping = False   # 正在丢一条超长行的后半截

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        out: list[bytes] = []
        while True:
            cut = self._buf.find(b"\n")
            if self._skipping:
                if cut < 0:
                    self._buf.clear()
                    return out
                del self._buf[:cut + 1]
                self._skipping = False
                continue
            if cut < 0:
                if len(self._buf) > self._limit + 1:     # 还没见到换行就已经超长 (多出的 1 个可能是 \r, 再多就一定超限)
                    out.append(bytes(self._buf[:self._limit]) + b"\0")
                    self._buf.clear()
                    self._skipping = True
                return out
            line = bytes(self._buf[:cut])
            del self._buf[:cut + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            out.append(line if len(line) <= self._limit else line[:self._limit] + b"\0")


# ---- 会话号和管道名 -------------------------------------------------------------------------------------------------

def valid_session(session: object) -> bool:
    """会话号: 恰好 32 个小写十六进制字符 (128 位随机数). 管道名由它拼出来, 所以别的写法一律不收 (比如带 \\ 的)."""
    return type(session) is str and _SESSION.fullmatch(session) is not None


def pipe_name(session: str) -> str:
    """看护方的会话通道的名字: \\\\.\\pipe\\wm-net-<会话号> (会话号不合规矩就抛 ValueError)."""
    if not valid_session(session):
        raise ValueError("会话号必须是 32 个小写十六进制字符")
    return PIPE_PREFIX + session


# ---- 参数: 指纹和生成 ------------------------------------------------------------------------------------------------

def params_fingerprint(text: str) -> str:
    """params_json 原文的 UTF-8 字节的 SHA-256 (十六进制). 令牌里的 params_sha256 就是它."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def start_params(recipe: Recipe, *, core_id: str, core_version: str, interval_s: int = DEFAULT_INTERVAL_S,
                 grace_s: int = DEFAULT_GRACE_S, upstream_owner: str | None = None) -> str:
    """生成 net.start 的 params_json: 契约 4.1 那个对象, 写成一行规范化 JSON (键排序、紧凑分隔符、非 ASCII 不转义, 规则同 config_gen.render).
    config_sha256 是 config_gen.fingerprint(config_gen.build(recipe, upstream_owner=...)); 配方有问题 build 就抛 RecipeError.
    生成的参数自己也过一遍 parse_start_params (core、心跳给得不对就抛 ValueError): 这里出来的文本代办一定认."""
    config = config_gen.build(recipe, upstream_owner=upstream_owner)
    text = config_gen.render({
        "template": TEMPLATE,
        "recipe": {"name": recipe.name, "processes": list(recipe.processes), "max_minutes": recipe.max_minutes},
        "upstream": {"host": recipe.upstream.host, "port": recipe.upstream.port},
        "core": {"id": core_id, "version": core_version},
        "config_sha256": config_gen.fingerprint(config),
        "heartbeat": {"interval_s": interval_s, "grace_s": grace_s},
    })
    _, found = _check_start(text)
    if found:
        raise ValueError("生成的 net.start 参数没通过白名单：" + "；".join(f"{field}（{why}）" for field, why in found))
    return text


# ---- 参数: 代办一侧的白名单 (契约 4.1 第 3 步) -------------------------------------------------------------------------------

@dataclass(frozen=True)
class StartParams:
    """net.start 的参数, 过了白名单以后的样子. recipe 里的 upstream 是 recipe.Upstream (host 加 port)."""
    recipe: Recipe
    core_id: str
    core_version: str
    config_sha256: str
    interval_s: int
    grace_s: int


Problem = tuple[str, str]   # (出问题的字段名, 中文说明)


def _keys(obj: dict, expected: tuple[str, ...], prefix: str, found: list[Problem]) -> None:
    """键必须恰好是 expected 这几个: 缺的、多的都记成问题 (字段名带上层的前缀)."""
    for key in expected:
        if key not in obj:
            found.append((prefix + key, "缺这一项"))
    for key in obj:
        if key not in expected:
            found.append((prefix + str(key)[:40], "不认识的键"))


def _recipe_field(message: str) -> str:
    """recipe.problems 的一条问题 -> 它说的是哪个字段 (问题都以"路径："开头, 路径是 name、processes、processes[2]、max_minutes)."""
    head = message.split("：", 1)[0]
    if head.startswith("processes"):
        return "recipe.processes"
    if head in ("name", "max_minutes"):
        return "recipe." + head
    return "recipe"


def _range_problem(value: object, low: int, high: int) -> str:
    if type(value) is not int:
        return "必须是整数（布尔值、小数不算）"
    if not low <= value <= high:
        return f"必须在 {low} 到 {high} 之间"
    return ""


def _check_start(text: object) -> tuple[StartParams | None, list[Problem]]:
    if type(text) is not str:
        return None, [("params_json", "缺少，或者不是字符串")]
    try:
        obj = parse_json_strict(text)
    except ParamsError as exc:
        return None, [("params_json", str(exc))]
    if type(obj) is not dict:
        return None, [("params_json", "必须是 JSON 对象")]
    found: list[Problem] = []
    _keys(obj, START_KEYS, "", found)
    if "template" in obj and not (type(obj["template"]) is int and obj["template"] == TEMPLATE):
        found.append(("template", f"必须是 {TEMPLATE}"))
    sections: dict[str, dict] = {}
    for name, keys in (("recipe", RECIPE_KEYS), ("upstream", UPSTREAM_KEYS), ("core", CORE_KEYS), ("heartbeat", HEARTBEAT_KEYS)):
        if name not in obj:
            continue
        if type(obj[name]) is not dict:
            found.append((name, "必须是对象"))
            continue
        sections[name] = obj[name]
        _keys(obj[name], keys, name + ".", found)
    recipe_part, upstream, core, heartbeat = (sections.get(name, {}) for name in ("recipe", "upstream", "core", "heartbeat"))
    # 配方三项 (名字、进程、时长): 用 recipe.problems 查 (缺的项先用合规的占位值, 缺项上面已经记了)
    shown = Recipe(name=recipe_part.get("name", "x"), processes=recipe_part.get("processes", ["x.exe"]),
                   upstream=Upstream("127.0.0.1", 1), max_minutes=recipe_part.get("max_minutes", 1))
    for message in recipe_problems(shown):
        found.append((_recipe_field(message), message))
    if "host" in upstream:
        found += [("upstream.host", message) for message in host_problems(upstream["host"])]
    if "port" in upstream:
        found += [("upstream.port", message) for message in port_problems(upstream["port"])]
    for key in CORE_KEYS:
        if key in core and not (type(core[key]) is str and _CORE_WORD.fullmatch(core[key])):
            found.append((f"core.{key}", "必须是 1 到 32 个字符，只许 ASCII 字母、数字和 . _ -"))
    if "config_sha256" in obj and not (type(obj["config_sha256"]) is str and _SHA256.fullmatch(obj["config_sha256"])):
        found.append(("config_sha256", "必须是 64 位小写十六进制"))
    for key, (low, high) in (("interval_s", INTERVAL_RANGE), ("grace_s", GRACE_RANGE)):
        if key in heartbeat:
            why = _range_problem(heartbeat[key], low, high)
            if why:
                found.append((f"heartbeat.{key}", why))
    if found:
        return None, found
    return StartParams(
        recipe=Recipe(name=recipe_part["name"], processes=tuple(recipe_part["processes"]), upstream=Upstream(upstream["host"], upstream["port"]),
                      max_minutes=recipe_part["max_minutes"]),
        core_id=core["id"], core_version=core["version"], config_sha256=obj["config_sha256"],
        interval_s=heartbeat["interval_s"], grace_s=heartbeat["grace_s"]), []


def _fields(found: list[Problem]) -> list[str]:
    out: list[str] = []
    for field, _ in found:
        if field not in out:
            out.append(field)
    return out


def parse_start_params(text: object) -> tuple[StartParams | None, list[str]]:
    """代办一侧的参数白名单 (契约 4.1 第 3 步). 返回 (解析结果, 出问题的字段名清单); 全过了清单是空的, 有问题结果是 None.

    先解析 (重复的键拒、小数拒); 每一层的键必须恰好是契约写的那几个 (缺的和多的都算问题, 字段名带层级前缀, 如 recipe.max_minutes、
    upstream.port); template 是 1; 配方名、进程名、时长、上游地址、端口按 recipe.py 里现成的检查; core 的两项 1-32 个 ASCII 字符
    (字母、数字、. _ -); config_sha256 是 64 位小写十六进制; 心跳间隔 1-30、宽限 10-300 (整数). 解析不了整段文本时字段名是 params_json."""
    params, found = _check_start(text)
    return params, _fields(found)


def start_param_problems(text: object) -> list[Problem]:
    """同 parse_start_params, 但返回 (字段名, 中文说明) 的清单, 给人看 (E_PARAMS 的 message 用)."""
    return _check_start(text)[1]


def params_failure(request_id: object, found: list[Problem]) -> dict:
    """E_PARAMS 的应答: fields 是出问题的字段名 (去重), message 里写前几条的说明."""
    shown = "；".join(f"{field}：{why}" for field, why in found[:4])
    return fail(request_id, E_PARAMS, f"参数没通过白名单（{len(_fields(found))} 项）：{shown}"[:600], _fields(found))


# ---- 参数: 会话通道上的动作 (看护方) ------------------------------------------------------------------------------------------

def parse_stop_params(text: object, session: str | None = None) -> tuple[str | None, list[Problem]]:
    """net.stop 的参数 {"session": ..., "reason": ...}: 两项都要有, reason 只许 STOP_REASONS 里的, 别的键不认; 给了 session 就核对它.
    返回 (reason, 问题清单)."""
    if type(text) is not str:
        return None, [("session", "缺少 params_json"), ("reason", "缺少 params_json")]
    try:
        obj = parse_json_strict(text)
    except ParamsError as exc:
        return None, [("params_json", str(exc))]
    if type(obj) is not dict:
        return None, [("params_json", "必须是 JSON 对象")]
    found: list[Problem] = []
    _keys(obj, ("session", "reason"), "", found)
    if "session" in obj and not (type(obj["session"]) is str and (session is None or obj["session"] == session)):
        found.append(("session", "不是这一段会话的会话号"))
    if "reason" in obj and obj["reason"] not in STOP_REASONS:
        found.append(("reason", "必须是 " + "、".join(STOP_REASONS) + " 之一"))
    return (None, found) if found else (obj["reason"], [])


def parse_empty_params(text: object) -> list[Problem]:
    """net.status、net.restore、heartbeat 的参数: 不带 params_json 或者是 {} (有任何键都不认)."""
    if text is None:
        return []
    if type(text) is not str:
        return [("params_json", "必须是字符串")]
    try:
        obj = parse_json_strict(text)
    except ParamsError as exc:
        return [("params_json", str(exc))]
    if type(obj) is not dict:
        return [("params_json", "必须是 JSON 对象")]
    return [(str(key)[:40], "不认识的键（这个动作没有参数）") for key in obj]


# ---- 同意令牌 (契约第 2 节) --------------------------------------------------------------------------------------------------

def _token_shape_problem(token: object) -> str:
    """令牌的形状: 恰好那 8 个字段, 类型对 (布尔值充数的不算); 令牌号是 128 位以上的十六进制; plugin、requested_by 是插件编号的写法 (另外 plugin 不能是
    owner 或 ayane, requested_by 里它们合法), issuer 是字母、数字、- (1-64 个字符); 两个时间必须是整数 (UTC 秒); 有效期在 0 到 120 秒之间."""
    if type(token) is not dict:
        return "令牌不是对象"
    for key in TOKEN_KEYS:
        if key not in token:
            return f"令牌缺 {key}"
    if any(key not in TOKEN_KEYS for key in token):
        return "令牌里有不认识的字段"
    if type(token["id"]) is not str or not _TOKEN_ID.fullmatch(token["id"]):
        return "令牌号必须是 128 位以上的十六进制"
    if type(token["action"]) is not str or not token["action"]:
        return "令牌的 action 必须是不空的字符串"
    if token["action"] not in TOKEN_ACTIONS:
        return "令牌批的动作不对"
    if type(token["params_sha256"]) is not str or not _SHA256_ANY.fullmatch(token["params_sha256"]):
        return "令牌里的 params_sha256 必须是 64 位十六进制"
    for key in ("plugin", "requested_by"):
        if type(token[key]) is not str or not _PLUGIN_ID.fullmatch(token[key]):
            return f"令牌的 {key} 必须是插件编号的写法（小写字母、数字、-，以字母或数字开头，最长 48 个字符）"
    if token["plugin"] in RESERVED_PLUGIN_IDS:
        return "令牌的 plugin 不能是 owner 或 ayane（那是 requested_by 里“用户自己”和“她”的写法，不能拿来当插件编号）"
    if type(token["issuer"]) is not str or not _ISSUER.fullmatch(token["issuer"]):
        return "令牌的 issuer 必须是字母、数字、-，1 到 64 个字符"
    if type(token["issued_at"]) is not int or type(token["expires_at"]) is not int:
        return "令牌的 issued_at、expires_at 必须是整数（UTC 秒）"
    span = token["expires_at"] - token["issued_at"]
    if span <= 0 or span > TOKEN_MAX_TTL_S:
        return f"令牌的有效期必须在 0 到 {TOKEN_MAX_TTL_S} 秒之间"
    return ""


def token_shape_ok(token: object) -> bool:
    """令牌的形状对不对 (契约第 2 节: 恰好 8 个字段, 类型对, 令牌号是十六进制, plugin 和 requested_by 是插件编号的写法, issuer 的写法, 两个时间是整数,
    有效期在 0 到 120 秒之间; 不看时间有没有过期、用没用过、动作和指纹对不对). check_token 只回一个码, 分不出是形状没过还是后面哪一查没过; 提权一侧的审计只在令牌的
    形状通过以后才写令牌号和谁申请的 (契约第 8 节〔补 3〕: 形状过了、后面查出过期、用过或对不上的, 照样写; 形状不对包括没带的, 空着), 所以公开成一个函数:
    调用方先问它, 再记."""
    return not _token_shape_problem(token)


def check_token(token: object, *, action: str, params_json: str | None, now: float, used_ids: Iterable[str]) -> str | None:
    """同意令牌的四查 (契约第 2 节), 返回错误码或 None (通过).

    先后: 没带 -> E_TOKEN_MISSING; 字段不全、类型不对 (三个字符串不合写法、时间不是整数)、有效期超过 120 秒 -> E_TOKEN_MISMATCH; 时间不对 (下面) ->
    E_TOKEN_MISMATCH 或 E_TOKEN_EXPIRED; 令牌号在 used_ids 里 (不分大小写) -> E_TOKEN_USED; action 或 params_sha256 对不上 -> E_TOKEN_MISMATCH.
    时间怎么比 (契约第 2 节〔补 2〕): 把 now 向下取整到秒, 记作 t; issued_at <= t <= expires_at 有效, 两头都含; issued_at 比 t 大 -> E_TOKEN_MISMATCH,
    t 比 expires_at 大 -> E_TOKEN_EXPIRED (now 是带小数的秒数也一样先取整).
    它不发令牌 (那是宿主的事), 也不记"用过了" (调用方在通过以后记).
    params_json 是请求里的原文; 没带 (None) 一定对不上 (没有原文就没有指纹可比: 要令牌的请求必须带 params_json)."""
    if token is None:
        return E_TOKEN_MISSING
    if _token_shape_problem(token):
        return E_TOKEN_MISMATCH
    second = math.floor(now)
    if token["issued_at"] > second:
        return E_TOKEN_MISMATCH
    if second > token["expires_at"]:
        return E_TOKEN_EXPIRED
    if token["id"].lower() in {str(used).lower() for used in used_ids}:
        return E_TOKEN_USED
    if token["action"] != action:
        return E_TOKEN_MISMATCH
    if type(params_json) is not str or token["params_sha256"].lower() != params_fingerprint(params_json):
        return E_TOKEN_MISMATCH
    return None


# ---- 核心输出打码 -------------------------------------------------------------------------------------------------------

_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b")
_IPV6 = re.compile(r"\[?(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}\]?(?::\d{1,5})?")
_HOST = re.compile(r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?!(?:exe|dll|sys|yaml|yml|json|log|txt|py|cfg|ini)\b)"
                   r"[A-Za-z]{2,24}\b(?::\d{1,5})?")
MASK = "***"


def mask_output_line(text: str, limit: int = 200) -> str:
    """核心输出的一行 -> 打过码的一行: 控制字符换成空格, IPv4 (带不带端口)、IPv6、域名一律换成 ***, 最长 limit 个字符 (超了截断加 …).
    宁可多遮: 像文件名的 (.exe .dll .yaml .json .log .txt .py ...) 不遮, 别的带点的词都当域名遮掉."""
    text = "".join(ch if ch.isprintable() else " " for ch in text[:limit * 2])
    for pattern in (_IPV4, _IPV6, _HOST):
        text = pattern.sub(MASK, text)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"

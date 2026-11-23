"""网络插件·配方 (2026-11-11 "网络"插件干跑的一块, docs: 09 问题与待办/网络代理与TUN插件方案): 哪些进程的流量要转给本机已有的
代理、上游在本机哪个端口, 以及对这份配方的检查.

只在 wm/net/ 里用, 项目别处不引用; 只用标准库; 全是纯函数 (不读写文件、不起进程、不联网、不读环境变量), 以后能整目录拿掉.

配方只说两件事: 点名哪几个进程 (processes) 走代理, 上游 (upstream) 是本机的哪个回环地址和端口 (用户自己装的那个代理, 不是我们
起的). problems / check 是生成配置 (config_gen.build) 的第一道门; config_gen.validate 重新核对规则里的进程名时用的也是这里的
process_problems —— 进程名的规矩只在这里写一份.

输入类型不对 (比如端口给了字符串) 也只是问题清单里的一条, 不会抛别的异常. 类型一律按"恰好是这个类型"认 (type(x) is int),
子类不收, 布尔值不算整数: 这是安全上的最后一道, 不给奇怪的子类留空子.

名字 (进程名和配方名) 里能用哪些字符, 由一张明确的码点表定 (接口契约第 7 节的"字符表": 实字 SOLID_RANGES、符号 SYMBOLS、空格),
表外的一律不许; 全模块只有 char_kind 一个查表函数. "不分大小写"只指 ASCII 的 A–Z 对 a–z (ascii_lower), 别的字符按原样比: 去重、
保留名、关键词、属主、.exe 后缀都这样比. 这里不用 Unicode 的字符类别和折叠 (不 import unicodedata, 不用 casefold 和 lower):
提权助手 (C++)、核心 (Go) 和这里 (Python) 三边手里的 Unicode 数据不是同一个版本, 冷僻字符上会对不上.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass

MAX_PROCESSES = 16          # 一份配方最多点名几个进程
MAX_PROCESS_NAME_LEN = 64   # 一个进程名 (连 .exe) 最长几个字符
MAX_NAME_LEN = 32           # 配方名 (给人看的) 最长几个字符 (接口契约评审后定的)
DEFAULT_MINUTES = 240       # 这一次最长开多久: 默认值 (分钟)
MAX_MINUTES = 720           # 这一次最长开多久: 上限 (分钟, 12 小时)
MAX_PORT = 65535

# 字符表 (接口契约第 7 节; 进程名和配方名都只认这张表, 表外的字符一律不许). 码点区间两端都含.
KIND_SOLID, KIND_SYMBOL, KIND_SPACE = "solid", "symbol", "space"
SOLID_RANGES = (                  # 实字: 名字里得至少有一个 (进程名是 .exe 前面)
    (0x0030, 0x0039),             # ASCII 数字 0-9
    (0x0041, 0x005A),             # ASCII 大写 A-Z
    (0x0061, 0x007A),             # ASCII 小写 a-z
    (0x4E00, 0x9FFF),             # 中日韩统一表意文字
    (0x3041, 0x3096),             # 平假名
    (0x30A1, 0x30FA),             # 片假名
    (0x30FC, 0x30FC),             # 片假名的长音符
    (0xAC00, 0xD7A3),             # 谚文音节
)
SYMBOLS = ".-_+"                  # 符号
SPACE = " "                       # 空格 U+0020 (进程名里只许中间的单个空格)

# 保留名 (共 19 个, 接口契约第 7 节): 不分大小写 (只指 ASCII), 带不带 .exe 都算. 服务端和看守用的 Python (python.exe、pythonw.exe、
# python3.exe、py.exe、pyw.exe)、桌宠 (wmpet.exe)、以后的提权助手 (wm-elevate.exe)、Windows 自己的核心进程和外壳 (svchost.exe、
# services.exe、lsass.exe、csrss.exe、wininit.exe、winlogon.exe、smss.exe、dwm.exe、explorer.exe、conhost.exe、System) 和服务管理器
# (nssm.exe), 走了代理会把整台电脑搅坏或断掉自己. 上游端口的属主 (复查时由调用方传进来, 比如 sing-box.exe) 不在这里,
# 在 process_problems 里另查.
RESERVED_NAMES = ("python.exe", "pythonw.exe", "python3.exe", "py.exe", "pyw.exe", "wmpet.exe", "wm-elevate.exe", "svchost.exe",
                  "services.exe", "lsass.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "smss.exe", "dwm.exe", "explorer.exe",
                  "conhost.exe", "nssm.exe", "System")
# 名字里不能含这些词: 它们是代理程序自己, 它们往外拨的流量再被送回上游就绕成环.
RESERVED_KEYWORDS = ("mihomo", "sing-box", "clash", "v2ray", "xray")

_PATH_SEPARATORS = "\\/"
_TABLE_HINT = "只许字符表里的字符：A–Z、a–z、0–9、汉字、平假名、片假名、谚文音节，加上 . _ - + 和空格"
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_LOOPBACK = ipaddress.ip_network("127.0.0.0/8")
_KINDS = {str: "字符串", int: "整数", bool: "布尔值", float: "小数", list: "列表", tuple: "元组", dict: "字典", type(None): "None"}


@dataclass(frozen=True)
class Upstream:
    """本机已有的代理端口 (用户自己装的, 不是我们起的)."""
    host: str      # 只能是本机回环地址: 127.0.0.0/8 里的 IPv4 字面量
    port: int


@dataclass(frozen=True)
class Recipe:
    """一份配方: 点名哪几个进程走代理 (进程名只是示例, 真名要在体检时现查), 转给哪个本机上游, 这一次最长开多久."""
    name: str                      # 给人看的名字, 比如游戏名
    processes: tuple[str, ...]     # 要走代理的进程名, 例如 ("game.exe",)
    upstream: Upstream
    max_minutes: int = DEFAULT_MINUTES   # 这一次最长开多久 (分钟)


class RecipeError(ValueError):
    """配方 (或按它生成的配置) 没通过检查; .problems 是全部问题 (中文, 一条说清一件事)."""

    def __init__(self, found: list[str] | str = ()):
        self.problems = [found] if isinstance(found, str) else list(found)
        lines = "".join(f"\n- {line}" for line in self.problems)
        super().__init__(f"没通过检查（共 {len(self.problems)} 个问题）：{lines}")


def kind_of(value: object) -> str:
    """给人看的类型名 (写进问题清单用)."""
    return _KINDS.get(type(value)) or type(value).__name__


def _show(value: object, limit: int, depth: int) -> str:
    kind = type(value)
    if kind is str:
        return repr(value if len(value) <= limit else value[:limit] + "…")
    if kind is int:
        return repr(value) if value.bit_length() <= 64 else "<超大整数>"
    if kind is bool or kind is float or value is None:
        return repr(value)
    if depth == 0 and (kind is list or kind is tuple):
        items = ", ".join(_show(item, limit, 1) for item in value[:6])
        return "[" + items + (", …" if len(value) > 6 else "") + "]"
    return f"<{kind.__name__}>"


def show(value: object, limit: int = 40) -> str:
    """把一个值写进问题清单: 字符串用 repr (换行、制表符会显示成 \\n \\t), 太长就截断; 列表只展开一层 (前 6 项);
    别的容器只写类型. 输入可能是任何东西, 包括嵌套很深、很大的, 所以这里绝不递归展开."""
    return _show(value, limit, 0)


def describe(value: object) -> str:
    """类型加值, 比如 字符串 '1082'、列表 ['any:53']; None、字典和不认识的类型只写类型名."""
    if value is None or type(value) not in _KINDS or type(value) is dict:
        return kind_of(value)
    return f"{kind_of(value)} {show(value)}"


def char_kind(ch: str) -> str | None:
    """字符表的查表函数 (全模块只有这一个): KIND_SOLID 实字、KIND_SYMBOL 符号 (. _ - +)、KIND_SPACE 空格 (U+0020)、
    None 表外 (一律不许). ch 必须是单个字符."""
    code = ord(ch)
    for low, high in SOLID_RANGES:
        if low <= code <= high:
            return KIND_SOLID
    if ch in SYMBOLS:
        return KIND_SYMBOL
    if ch == SPACE:
        return KIND_SPACE
    return None


def ascii_lower(text: str) -> str:
    """只把 ASCII 的 A–Z 转成 a–z, 别的字符原样 (不用 lower 和 casefold: 它们会动非 ASCII 字符, 三边的 Unicode 数据又不是同一版)."""
    return text.translate(_ASCII_LOWER)


def _name_key(name: str) -> str:
    """保留名、属主比较用的键: 取最后一段文件名 (万一带了路径), 去掉两头空白, ASCII 大写转小写, 去掉末尾的 .exe."""
    base = ascii_lower(name.replace("/", "\\").rsplit("\\", 1)[-1].strip())
    return base[:-4] if base.endswith(".exe") else base


def _owner_key(upstream_owner: object) -> str:
    return _name_key(upstream_owner) if type(upstream_owner) is str else ""


def _outside_table(text: str, skip: str = "") -> str:
    """text 里字符表以外的字符 (skip 里的不列, 它们另有说法), 去重后最多列 5 个, 带码点."""
    seen: list[str] = []
    for ch in text:
        if ch not in seen and ch not in skip and char_kind(ch) is None:
            seen.append(ch)
    return "、".join(f"{ch!r} (U+{ord(ch):04X})" for ch in seen[:5]) + ("等" if len(seen) > 5 else "")


def owner_problems(upstream_owner: object) -> list[str]:
    """上游端口属主 (upstream_owner) 本身有没有问题: 可以是 None (不查绕环), 否则必须是不空的字符串."""
    if upstream_owner is None:
        return []
    if type(upstream_owner) is not str:
        return [f"upstream_owner：必须是字符串或 None，现在是 {kind_of(upstream_owner)}"]
    if not _name_key(upstream_owner):
        return ["upstream_owner：是空的；不知道上游端口的属主就传 None（但那样绕环这一项就查不了）"]
    return []


def _reserved_problems(name: str, shown: str, path: str, upstream_owner: object) -> list[str]:
    out: list[str] = []
    key = _name_key(name)
    if key in {_name_key(reserved) for reserved in RESERVED_NAMES}:
        out.append(f"{path}：进程名 {shown} 是保留名（服务端、桌宠、提权助手和系统自己的进程，不能走代理）")
    lowered = ascii_lower(name)
    for word in RESERVED_KEYWORDS:
        if word in lowered:
            out.append(f"{path}：进程名 {shown} 里有 {word!r}，那是代理程序自己的名字（它的流量再送回代理会绕成环）")
            break
    owner = _owner_key(upstream_owner)
    if owner and key == owner:
        out.append(f"{path}：进程名 {shown} 就是上游端口的属主 {show(upstream_owner)}"
                   "（上游自己往外拨的流量会被再送回上游，绕成环）")
    return out


def process_problems(name: object, *, upstream_owner: object = None, path: str = "processes[0]") -> list[str]:
    """一个进程名的问题清单 (格式 + 保留名); 空清单就是通过. path 写在每条问题前面, 说明这个名字在哪 (processes[2]、rules[0] 之类).

    格式: 单纯的文件名, 最长 64 个字符; 每个字符都在字符表里 (表外的一律不许, 路径分隔符也在表外); 空格只能是中间的单个空格 (不能
    开头、结尾、连续); 以 .exe 结尾 (只有 ASCII 大小写不分); .exe 前面至少有一个实字.
    保留名: RESERVED_NAMES、含 RESERVED_KEYWORDS 的名字、上游端口的属主 upstream_owner (传 None 就不查这一项)."""
    if type(name) is not str:
        return [f"{path}：进程名必须是字符串，现在是 {kind_of(name)}"]
    if not name:
        return [f"{path}：进程名是空的"]
    shown = show(name)
    out: list[str] = []
    if len(name) > MAX_PROCESS_NAME_LEN:
        out.append(f"{path}：进程名 {shown} 太长（{len(name)} 个字符，最多 {MAX_PROCESS_NAME_LEN} 个）")
    # 逐个字符的检查只看前 MAX_PROCESS_NAME_LEN 个: 超长的名字反正已经不通过, 不让一个巨大的名字拖住检查
    head = name[:MAX_PROCESS_NAME_LEN]
    if any(ch in _PATH_SEPARATORS for ch in head):
        out.append(f"{path}：进程名 {shown} 带了路径分隔符，只能写文件名（比如 game.exe），不带目录")
    odd = _outside_table(head, skip=_PATH_SEPARATORS)
    if odd:
        out.append(f"{path}：进程名 {shown} 含有不允许的字符 {odd}（{_TABLE_HINT}）")
    if name[0] == SPACE or name[-1] == SPACE:
        out.append(f"{path}：进程名 {shown} 以空格开头或结尾")
    if SPACE * 2 in name:
        out.append(f"{path}：进程名 {shown} 里有连续的空格，中间只许单个空格")
    if ascii_lower(name[-4:]) != ".exe":
        out.append(f"{path}：进程名 {shown} 不是以 .exe 结尾（只有 ASCII 字母不分大小写）")
    elif len(name) <= MAX_PROCESS_NAME_LEN and not any(char_kind(ch) == KIND_SOLID for ch in name[:-4]):
        out.append(f"{path}：进程名 {shown} 的 .exe 前面连一个实字都没有（实字：字母、数字、汉字、假名、谚文）")
    out.extend(_reserved_problems(name, shown, path, upstream_owner))
    return out


def host_problems(host: object, path: str = "upstream.host") -> list[str]:
    """上游地址的问题清单: 必须是 127.0.0.0/8 里、写成规范 a.b.c.d 的 IPv4 字面量 (不接受 localhost、域名、0.0.0.0、局域网地址、IPv6)."""
    if type(host) is not str:
        return [f"{path}：上游地址必须是字符串，现在是 {kind_of(host)}"]
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        address = None
    if address is None or str(address) != host:
        return [f"{path}：{show(host)} 不是写成 a.b.c.d 的 IPv4 数字地址（localhost、域名、IPv6 都不接受）"]
    if address not in _LOOPBACK:
        return [f"{path}：{show(host)} 不是本机回环地址，上游只能在 127.0.0.0/8 里（比如 127.0.0.1）"]
    return []


def port_problems(port: object, path: str = "upstream.port") -> list[str]:
    """上游端口的问题清单: 1 到 65535 的整数, 布尔值不算整数."""
    if type(port) is not int:
        note = "布尔值不算整数" if type(port) is bool else f"现在是 {describe(port)}"
        return [f"{path}：端口必须是整数（1 到 {MAX_PORT}），{note}"]
    if not 1 <= port <= MAX_PORT:
        return [f"{path}：端口 {show(port)} 不在 1 到 {MAX_PORT} 之间"]
    return []


def _label_problems(name: object) -> list[str]:
    """配方名: 1–32 个字符, 每个字符都在字符表里, 至少有一个实字 (看不见的字符本来就在表外, 进不来)."""
    if type(name) is not str:
        return [f"name：必须是字符串，现在是 {kind_of(name)}"]
    if not name:
        return ["name：不能是空的"]
    out: list[str] = []
    if len(name) > MAX_NAME_LEN:
        out.append(f"name：太长（{len(name)} 个字符，最多 {MAX_NAME_LEN} 个）")
    head = name[:MAX_NAME_LEN]  # 超长的只看开头, 反正已经不通过
    odd = _outside_table(head)
    if odd:
        out.append(f"name：含有不允许的字符 {odd}（{_TABLE_HINT}）")
    if len(name) <= MAX_NAME_LEN and not any(char_kind(ch) == KIND_SOLID for ch in head):
        out.append("name：至少要有一个实字（字母、数字、汉字、假名、谚文），只有符号和空格不行")
    return out


def _minutes_problems(minutes: object) -> list[str]:
    if type(minutes) is not int:
        note = "布尔值不算整数" if type(minutes) is bool else f"现在是 {describe(minutes)}"
        return [f"max_minutes：必须是整数（1 到 {MAX_MINUTES}），{note}"]
    if not 1 <= minutes <= MAX_MINUTES:
        return [f"max_minutes：{show(minutes)} 不在 1 到 {MAX_MINUTES} 之间"]
    return []


def _processes_problems(processes: object, upstream_owner: object) -> list[str]:
    if type(processes) is not tuple and type(processes) is not list:
        return [f"processes：必须是进程名的元组（或列表），现在是 {kind_of(processes)}"]
    out: list[str] = []
    count = len(processes)
    if count < 1:
        out.append("processes：至少要点名 1 个进程")
    elif count > MAX_PROCESSES:
        out.append(f"processes：最多 {MAX_PROCESSES} 个，现在有 {count} 个")
    # 名单再长也只逐个查前 MAX_PROCESSES + 1 个: 超过上限本来就不通过, 不让一份巨大的输入拖住检查
    for index, name in enumerate(processes[:MAX_PROCESSES + 1]):
        out.extend(process_problems(name, upstream_owner=upstream_owner, path=f"processes[{index}]"))
    return out


def _recipe_problems(recipe: object, upstream_owner: object) -> list[str]:
    out = owner_problems(upstream_owner)
    if not isinstance(recipe, Recipe):
        return out + [f"配方：必须是 Recipe，现在是 {kind_of(recipe)}"]
    out += _label_problems(recipe.name)
    upstream = recipe.upstream
    if isinstance(upstream, Upstream):
        out += host_problems(upstream.host)
        out += port_problems(upstream.port)
    else:
        out.append(f"upstream：必须是 Upstream（host 加 port），现在是 {kind_of(upstream)}")
    out += _processes_problems(recipe.processes, upstream_owner)
    out += _minutes_problems(recipe.max_minutes)
    return out


def problems(recipe: object, *, upstream_owner: str | None = None) -> list[str]:
    """配方的问题清单 (中文, 一条说清一件事; 空清单就是通过). 输入是什么都行, 类型不对只算问题, 不抛异常.

    upstream_owner: 上游端口现在是哪个进程在占着 (比如 "sing-box.exe"); 给了就多查一项: 配方里不能点名它 —— 否则上游自己往外拨
    的流量会被再送回上游, 绕成环. 不知道就传 None, 那样这一项查不了."""
    try:
        return _recipe_problems(recipe, upstream_owner)
    except Exception as exc:  # 检查本身不许抛: 任何意外都按没通过算
        return [f"检查配方时出了意外（{type(exc).__name__}），按没通过算"]


def check(recipe: object, *, upstream_owner: str | None = None) -> None:
    """配方有问题就抛 RecipeError (带上全部问题), 没问题什么也不做."""
    found = problems(recipe, upstream_owner=upstream_owner)
    if found:
        raise RecipeError(found)


def unique_processes(processes: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """去重 (只有 ASCII 的大小写不分), 保留第一次出现的写法和顺序. 只在配方通过检查之后用."""
    seen: set[str] = set()
    out: list[str] = []
    for name in processes:
        key = ascii_lower(name)
        if key not in seen:
            seen.add(key)
            out.append(name)
    return tuple(out)

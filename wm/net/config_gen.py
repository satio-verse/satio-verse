"""网络插件·配置生成和白名单复查 (2026-11-11 "网络"插件干跑的一块, docs: 09 问题与待办/网络代理与TUN插件方案): 把配方 (recipe.py)
变成给代理核心 (mihomo, 当独立程序调用, 只当 TUN 入口) 的配置, 再写一个独立的白名单复查.

只在 wm/net/ 里用, 项目别处不引用; 只用标准库 (和同目录的 recipe.py); 全是纯函数, 只生成、只检查, 不运行核心.

- build(recipe): 先 check 配方, 再按模板 1 生成配置, 最后拿 validate 复查自己的产出, 不过就抛 RecipeError.
- render(config): 规范化的一行 JSON (键排序、紧凑分隔符、中文不转义), 末尾不加换行. 写进配置文件的就是它按 UTF-8 编码的字节.
  JSON 本身就是合法的 YAML, 核心读得了, 所以不引入 YAML 库.
- fingerprint(config): render(config) 的 UTF-8 字节的 SHA-256 (十六进制): 写进配置文件的和算指纹的是同一串字节. 同意卡片和
  提权助手靠它确认"点头的就是这一份".
- pretty(config): 缩进 2 格、给人看的版本; 不参与指纹, 也不写进配置文件.
- validate(config): 独立于 build 的白名单复查. 以后以管理员身份运行的小助手在启动核心前会再拿它查一遍别人递来的配置, 所以它是
  安全上的最后一道: 不信输入是 build 出来的 (输入可能是任何东西), 每一层的键都必须恰好是名单上的、值恰好是名单上的, 多一个少一个
  都不行; 类型只认 JSON 解出来的原生类型 (dict、list、str、int、bool), 子类和别的类型一律算问题; 类型不对只算问题, 绝不抛异常
  (复查过程中的任何意外也按"没通过"算).

注意 validate 查的是"解析之后的对象". 助手手里要是拿的文本, 光复查不够, 还得核对文本就是 render(解析出来的对象), 逐字节一样:
JSON 里的重复键、注释、转义花样, 在 Python 和核心的解析器之间可能被读成不同的东西, 只有"和规范输出一字不差"才没有这种空子.
调用 build / validate 时务必把上游端口现在的属主传给 upstream_owner (不传就查不了"进程规则点了上游自己"这种绕环).

这一层不读环境: 核心 mihomo 会拿 CLASH_CONFIG_STRING、CLASH_CONFIG_FILE、CLASH_POST_UP 一类环境变量当启动参数的默认值 (能换掉整份
配置、能指定要跑的脚本), 所以生成和复查自己绝不看环境变量 (连 os 都不 import; 测试里有 ast 扫描, 还有设上这些变量前后逐字节
对照的测试守着). 起核心时的环境得由起它的那一边管.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter

from .recipe import (MAX_PROCESSES, Recipe, RecipeError, check, describe, host_problems, kind_of, owner_problems, port_problems,
                     process_problems, show, unique_processes)

DEVICE = "WM-Ayane"           # 虚拟网卡的名字: 固定, 一眼认得出是我们的; 体检、恢复也按这个名字找
STACK = "gvisor"              # TUN 的协议栈
UPSTREAM_NAME = "upstream"    # 唯一的出口的名字, 规则都指向它
RULE_TYPE = "PROCESS-NAME"    # 规则类型: 只按进程名分流
FINAL_RULE = "MATCH,DIRECT"   # 最后一条规则永远是直连: 没点名的流量照常走
# 这几段不进虚拟网卡: 私有网段 (10/8、172.16/12、192.168/16)、运营商共享地址 (100.64/10)、链路本地 (169.254/16)、组播 (224/4)、
# 受限广播 (255.255.255.255) —— 局域网、虚拟机、WSL 才不受影响
EXCLUDE_V4 = (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
    "169.254.0.0/16", "224.0.0.0/4", "255.255.255.255/32",
)
ALLOWED_STACKS = ("gvisor", "mips")                          # 复查放行的栈 (build 只生成 STACK)
ALLOWED_LOG_LEVELS = ("debug", "info", "warning", "error")   # 复查放行的日志级别 (build 只生成 info)
UNEXPECTED = "复查时出了意外"  # validate 遇到意外 (按理不会有) 时问题清单里的话; 测试靠它确认没有哪一条是被"意外"兜住的

# 键必须恰好是这些 (多一个少一个都算问题). 顺序就是 build 生成时的顺序.
TOP_KEYS = ("mode", "log-level", "allow-lan", "ipv6", "find-process-mode", "tun", "dns", "proxies", "rules")
TUN_KEYS = ("enable", "device", "stack", "auto-route", "auto-detect-interface", "strict-route", "dns-mode", "dns-hijack",
            "route-exclude-address")
PROXY_KEYS = ("name", "type", "server", "port", "udp")

# 值必须恰好是这些 (类型也要一样: True 不能写成 1)
_TOP_VALUES = (("mode", "rule"), ("allow-lan", False), ("ipv6", False), ("find-process-mode", "always"))
_TUN_VALUES = (("enable", True), ("device", DEVICE), ("auto-route", True), ("auto-detect-interface", True),
               ("strict-route", False), ("dns-mode", "disabled"))
_PROXY_VALUES = (("name", UPSTREAM_NAME), ("type", "socks5"), ("udp", True))
_SCAN_LIMIT = MAX_PROCESSES + 2   # 列表再长也只逐项查前这么多项: 多出来的本来就不通过, 不让一份巨大的输入拖住复查


def build(recipe: Recipe, *, upstream_owner: str | None = None) -> dict:
    """配方 -> 给核心的配置 (模板 1). 配方没过 check 就抛 RecipeError; 过了就生成, 并拿 validate 再查自己的产出一遍.

    upstream_owner 见 recipe.problems. 每次返回全新的字典 (里面的列表也是新的), 调用方随便改不会碰到模块常量."""
    check(recipe, upstream_owner=upstream_owner)
    rules = [f"{RULE_TYPE},{name},{UPSTREAM_NAME}" for name in unique_processes(recipe.processes)]
    rules.append(FINAL_RULE)
    # 不开控制口 (没有 external-controller 和 secret), 不开任何入站口 (没有 mixed-port、port、socks-port 一类), 没有节点分组、订阅、
    # 规则集、hosts、嗅探: 核心不监听任何端口, 配置和行为只由这一份文件决定
    config = {
        "mode": "rule",  # 按规则分流: 只有点名的进程走上游, 其余全靠最后一条 MATCH 直连
        "log-level": "info",  # 日志只到 info: debug 会把每条连接都记下来, 太啰嗦, 也涉隐私
        "allow-lan": False,  # 不许局域网里别的机器借这个核心上网
        "ipv6": False,  # 只处理 IPv4: 上游是 IPv4 回环, IPv6 的流量不进虚拟网卡, 照常直连
        "find-process-mode": "always",  # 按进程名分流得先查出连接是哪个进程发的: always = 每条连接都查
        "tun": {
            "enable": True,  # 开虚拟网卡 (TUN): 游戏的 UDP 不认系统代理, 要靠它接住
            "device": DEVICE,  # 网卡名固定: 一眼认得出是我们的, 体检和恢复也按这个名字找
            "stack": STACK,  # gvisor: 核心自带的用户态协议栈, 不借系统栈转发
            "auto-route": True,  # 让核心自己把路由加到虚拟网卡上, 不用手动配 (路由挂在这块网卡上, 不是持久路由)
            "auto-detect-interface": True,  # 核心自己往外拨的连接绑到真实出口网卡, 不会被自己的路由再卷回虚拟网卡 (防环)
            "strict-route": False,  # 不开严格路由: 它在 Windows 上会加防火墙规则, 可能让虚拟机一类软件不正常; 这里只要点名进程走代理
            "dns-mode": "disabled",  # 不劫持 DNS: 域名照旧由系统解析, 只管点名进程的 IP 流量
            "dns-hijack": [],  # 同上, 没有任何要劫持的 DNS 地址
            "route-exclude-address": list(EXCLUDE_V4),  # 私有网段等不进虚拟网卡: 局域网、虚拟机、WSL 才不受影响
        },
        "dns": {"enable": False},  # 核心自己不开 DNS 服务 (也就不监听 DNS 端口)
        "proxies": [
            {
                "name": UPSTREAM_NAME,  # 唯一的出口, 名字固定, 所有进程规则都指向它
                "type": "socks5",  # 上游是用户自己装的本机代理 (SOCKS5), 我们不碰它的节点和订阅
                "server": recipe.upstream.host,  # 只会是 127.0.0.0/8 里的地址: 流量只转给本机, 不出本机
                "port": recipe.upstream.port,  # 用户本机代理的端口
                "udp": True,  # 游戏走 UDP: 要让核心把 UDP 也转给上游
            },
        ],
        "rules": rules,  # 每个点名的进程一条 (按配方的顺序), 最后一条永远是 MATCH,DIRECT: 没点名的流量照常直连
    }
    found = validate(config, upstream_owner=upstream_owner)
    if found:  # 不该发生: 生成器和复查对不上, 或配方在检查之后被人改了
        raise RecipeError(["生成出来的配置没通过白名单复查（是生成器的毛病，不是配方的）："] + found)
    return config


def render(config: dict) -> str:
    """配置 -> 文本: 规范化的一行 JSON (键排序、紧凑分隔符、中文不转义), 末尾不加换行. 写进配置文件的就是它按 UTF-8 编码的字节,
    fingerprint 也是对这串字节算的. JSON 本身就是合法的 YAML, 核心读得了, 所以不引入 YAML 库. 只对过了 validate 的配置有意义
    (键不是字符串、含 NaN 之类 JSON 写不出来的东西会抛 TypeError 或 ValueError)."""
    return json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def fingerprint(config: dict) -> str:
    """配置的指纹: render(config) 按 UTF-8 编码后的 SHA-256, 十六进制. 写进配置文件的和算指纹的是同一串字节."""
    return hashlib.sha256(render(config).encode("utf-8")).hexdigest()


def pretty(config: dict) -> str:
    """给人看的版本: 缩进 2 格、键保持原来的顺序、中文不转义、末尾一个换行. 不参与指纹, 也不写进配置文件."""
    return json.dumps(config, ensure_ascii=False, indent=2) + "\n"


def validate(config: object, *, upstream_owner: str | None = None) -> list[str]:
    """白名单复查: 返回问题清单 (中文, 一条说清一件事; 空清单就是通过). 不信输入是 build 出来的, 输入是什么都行, 绝不抛异常.

    规则见模块说明和下面各个小函数. upstream_owner 见 recipe.problems: 给了就多查"进程规则不能点名上游端口的属主"."""
    try:
        return owner_problems(upstream_owner) + _config_problems(config, upstream_owner)
    except Exception as exc:  # 复查不许抛: 任何意外都按没通过算
        return [f"{UNEXPECTED}（{type(exc).__name__}），按没通过算"]


def _config_problems(config: object, upstream_owner: object) -> list[str]:
    if type(config) is not dict:
        return [f"配置：必须是字典（JSON 对象），现在是 {kind_of(config)}"]
    out = _key_problems(config, TOP_KEYS, "顶层")
    out += _exact_problems(config, _TOP_VALUES, "")
    if "log-level" in config and not _one_of(config["log-level"], ALLOWED_LOG_LEVELS):
        out.append(f"log-level：必须是 {'、'.join(ALLOWED_LOG_LEVELS)} 之一，现在是 {describe(config['log-level'])}")
    if "tun" in config:
        out += _tun_problems(config["tun"])
    if "dns" in config:
        out += _dns_problems(config["dns"])
    if "proxies" in config:
        out += _proxies_problems(config["proxies"])
    if "rules" in config:
        out += _rules_problems(config["rules"], upstream_owner)
    return out


def _one_of(value: object, allowed: tuple[str, ...]) -> bool:
    """值是字符串 (恰好是 str) 而且在名单里. 先认类型再比较: 列表之类拿去比较没问题, 但不让它们蒙混."""
    return type(value) is str and value in allowed


def _same(value: object, want: object) -> bool:
    """严格相等: 类型也要一样 (True 不能写成 1, "true" 也不行)."""
    return type(value) is type(want) and value == want


def _keys_text(keys: list) -> str:
    return "、".join(show(key) for key in keys[:6]) + ("等" if len(keys) > 6 else "")


def _key_problems(obj: dict, expected: tuple[str, ...], where: str) -> list[str]:
    """键必须恰好是 expected 这几个: 缺的、多的都说出来."""
    out: list[str] = []
    missing = [key for key in expected if key not in obj]
    extra = [key for key in obj if key not in expected]
    if missing:
        out.append(f"{where}：缺了这些键 {_keys_text(missing)}")
    if extra:
        out.append(f"{where}：多了不许有的键 {_keys_text(extra)}（键必须恰好是名单上的 {len(expected)} 个）")
    return out


def _exact_problems(obj: dict, table: tuple, prefix: str) -> list[str]:
    """table 里每个键 (有的话) 的值必须恰好是写定的那个."""
    out: list[str] = []
    for key, want in table:
        if key in obj and not _same(obj[key], want):
            out.append(f"{prefix}{key}：必须是 {show(want)}，现在是 {describe(obj[key])}")
    return out


def _tun_problems(tun: object) -> list[str]:
    if type(tun) is not dict:
        return [f"tun：必须是字典，现在是 {kind_of(tun)}"]
    out = _key_problems(tun, TUN_KEYS, "tun")
    out += _exact_problems(tun, _TUN_VALUES, "tun.")
    if "stack" in tun and not _one_of(tun["stack"], ALLOWED_STACKS):
        out.append(f"tun.stack：必须是 {'、'.join(ALLOWED_STACKS)} 之一，现在是 {describe(tun['stack'])}")
    if "dns-hijack" in tun and not (type(tun["dns-hijack"]) is list and not tun["dns-hijack"]):
        out.append(f"tun.dns-hijack：必须是空列表（不劫持 DNS），现在是 {describe(tun['dns-hijack'])}")
    if "route-exclude-address" in tun:
        out += _exclude_problems(tun["route-exclude-address"])
    return out


def _exclude_problems(value: object) -> list[str]:
    """排除网段必须和 EXCLUDE_V4 是同一组 (不管顺序; 不能多不能少, 重复写的也算多)."""
    where = "tun.route-exclude-address"
    if type(value) is not list or len(value) > 4 * len(EXCLUDE_V4) or any(type(item) is not str for item in value):
        return [f"{where}：必须是那 {len(EXCLUDE_V4)} 段网段字符串的列表，现在是 {describe(value)}"]
    want, got = Counter(EXCLUDE_V4), Counter(value)
    if got == want:
        return []
    parts = []
    if want - got:
        parts.append(f"缺了 {show(sorted((want - got).elements()))}")
    if got - want:
        parts.append(f"多了 {show(sorted((got - want).elements()))}")
    return [f"{where}：必须恰好是那 {len(EXCLUDE_V4)} 段私有/保留网段（不管顺序），{'，'.join(parts)}"]


def _dns_problems(dns: object) -> list[str]:
    if type(dns) is not dict:
        return [f"dns：必须是字典，现在是 {kind_of(dns)}"]
    return _key_problems(dns, ("enable",), "dns") + _exact_problems(dns, (("enable", False),), "dns.")


def _proxies_problems(proxies: object) -> list[str]:
    if type(proxies) is not list:
        return [f"proxies：必须是列表，现在是 {kind_of(proxies)}"]
    out: list[str] = []
    if len(proxies) != 1:
        out.append(f"proxies：必须恰好一个代理（{UPSTREAM_NAME}），现在有 {len(proxies)} 个")
    for index, proxy in enumerate(proxies[:_SCAN_LIMIT]):
        out += _proxy_problems(proxy, f"proxies[{index}]")
    return out


def _proxy_problems(proxy: object, where: str) -> list[str]:
    if type(proxy) is not dict:
        return [f"{where}：必须是字典，现在是 {kind_of(proxy)}"]
    out = _key_problems(proxy, PROXY_KEYS, where)
    out += _exact_problems(proxy, _PROXY_VALUES, where + ".")
    if "server" in proxy:
        out += host_problems(proxy["server"], f"{where}.server")
    if "port" in proxy:
        out += port_problems(proxy["port"], f"{where}.port")
    return out


def _rules_problems(rules: object, upstream_owner: object) -> list[str]:
    """规则: 前面每条恰好是 PROCESS-NAME,<进程名>,upstream (进程名按 recipe.py 的规则再查), 最后一条恰好是 MATCH,DIRECT."""
    if type(rules) is not list:
        return [f"rules：必须是列表，现在是 {kind_of(rules)}"]
    out: list[str] = []
    count = len(rules)
    if count < 2:
        out.append(f"rules：至少要有两条（至少一条进程规则，最后一条是 {FINAL_RULE!r}），现在有 {count} 条")
    if count - 1 > MAX_PROCESSES:
        out.append(f"rules：进程规则最多 {MAX_PROCESSES} 条，现在有 {count - 1} 条")
    for index in range(min(max(count - 1, 0), _SCAN_LIMIT)):
        out += _process_rule_problems(rules[index], f"rules[{index}]", upstream_owner)
    if count and not _same(rules[-1], FINAL_RULE):
        out.append(f"rules[{count - 1}]：最后一条必须是 {FINAL_RULE!r}（其余流量直连），现在是 {describe(rules[-1])}")
    return out


def _process_rule_problems(rule: object, where: str, upstream_owner: object) -> list[str]:
    if type(rule) is not str:
        return [f"{where}：必须是字符串，现在是 {kind_of(rule)}"]
    parts = rule.split(",", 3)  # 最多切四段: 恰好三段才是对的形状, 逗号再多也不用全切开
    if len(parts) != 3 or parts[0] != RULE_TYPE or parts[2] != UPSTREAM_NAME:
        return [f"{where}：{show(rule)} 不是 '{RULE_TYPE},<进程名>,{UPSTREAM_NAME}' 这个形状"
                "（只许按进程名走上游，别的规则类型、出口名和附加参数都不行）"]
    return process_problems(parts[1], upstream_owner=upstream_owner, path=where)

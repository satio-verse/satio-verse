"""网络插件干跑: 配置生成器和白名单复查 (wm/net/recipe.py、wm/net/config_gen.py).

配方 -> 给代理核心 (mihomo, 当独立程序用, 只当 TUN 入口) 的配置; 另有一个独立的白名单复查 validate: 以后以管理员身份运行的小助手
在启动核心前会拿它再查一遍别人递来的配置, 所以它是安全上的最后一道.
这里全是纯函数的测试: 不起核心、不碰网卡、不联网、不读写文件 (只读这两个源文件本身, 扫一遍它们有没有碰文件、进程、网络).

- 期望的配置照任务说明原样写在本文件里 (不读 config_gen 里的常量), 生成器哪天悄悄改了任何一个值, 这里就会红;
- 篡改的测试从一份合法配置出发, 每次只改一处, 要求复查返回非空、不抛异常, 而且是被该拒的那条规则拒的 (不是被"意外"兜住的);
- 乱塞的测试用固定种子的随机数, 保证复查对什么输入都不抛异常."""
from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import ipaddress
import json
import os
import random
import sys
from pathlib import Path

import pytest

from wm.net import config_gen
from wm.net import recipe as recipe_mod
from wm.net.config_gen import build, fingerprint, pretty, render, validate
from wm.net.recipe import Recipe, RecipeError, Upstream, check, problems, process_problems, unique_processes

NET = Path(__file__).resolve().parents[1] / "wm" / "net"

EXCLUDE = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "224.0.0.0/4",
           "255.255.255.255/32"]
TOP_KEYS = ["mode", "log-level", "allow-lan", "ipv6", "find-process-mode", "tun", "dns", "proxies", "rules"]
TUN_KEYS = ["enable", "device", "stack", "auto-route", "auto-detect-interface", "strict-route", "dns-mode", "dns-hijack",
            "route-exclude-address"]


def make(processes=("nikke.exe",), host="127.0.0.1", port=1082, name="NIKKE", **extra):
    return Recipe(name=name, processes=processes, upstream=Upstream(host, port), **extra)


def expected_config(names, host="127.0.0.1", port=1082):
    """任务说明里的配置原样写在这里 (不读 config_gen 的常量); names 是按顺序、去过重的进程名."""
    return {
        "mode": "rule",
        "log-level": "info",
        "allow-lan": False,
        "ipv6": False,
        "find-process-mode": "always",
        "tun": {
            "enable": True,
            "device": "WM-Ayane",
            "stack": "gvisor",
            "auto-route": True,
            "auto-detect-interface": True,
            "strict-route": False,
            "dns-mode": "disabled",
            "dns-hijack": [],
            "route-exclude-address": list(EXCLUDE),
        },
        "dns": {"enable": False},
        "proxies": [{"name": "upstream", "type": "socks5", "server": host, "port": port, "udp": True}],
        "rules": [f"PROCESS-NAME,{name},upstream" for name in names] + ["MATCH,DIRECT"],
    }


def canonical_sha256(config):
    """不经过 config_gen.fingerprint 的独立算法: 规范化 JSON (键排序、紧凑分隔符、不转义非 ASCII) 的 SHA-256."""
    text = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def good_config():
    return build(make(("nikke.exe", "My Game.exe")))


# ---------------------------------------------------------------- 说明里写定的常量

def test_constants_are_what_the_spec_says():
    assert (config_gen.DEVICE, config_gen.STACK, config_gen.UPSTREAM_NAME) == ("WM-Ayane", "gvisor", "upstream")
    assert list(config_gen.EXCLUDE_V4) == EXCLUDE
    assert list(config_gen.TOP_KEYS) == TOP_KEYS and list(config_gen.TUN_KEYS) == TUN_KEYS
    assert list(config_gen.PROXY_KEYS) == ["name", "type", "server", "port", "udp"]
    assert list(config_gen.ALLOWED_STACKS) == ["gvisor", "mips"]
    assert list(config_gen.ALLOWED_LOG_LEVELS) == ["debug", "info", "warning", "error"]
    contract_reserved = {"python.exe", "pythonw.exe", "python3.exe", "py.exe", "pyw.exe", "wmpet.exe", "wm-elevate.exe",
                         "svchost.exe", "services.exe", "lsass.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "smss.exe",
                         "dwm.exe", "explorer.exe", "conhost.exe", "nssm.exe", "System"}
    assert len(recipe_mod.RESERVED_NAMES) == 19 and set(recipe_mod.RESERVED_NAMES) == contract_reserved  # 19 个, 不重复
    assert recipe_mod.SOLID_RANGES == ((0x30, 0x39), (0x41, 0x5A), (0x61, 0x7A), (0x4E00, 0x9FFF), (0x3041, 0x3096),
                                       (0x30A1, 0x30FA), (0x30FC, 0x30FC), (0xAC00, 0xD7A3))
    assert set(recipe_mod.SYMBOLS) == set(".-_+") and len(recipe_mod.SYMBOLS) == 4 and recipe_mod.SPACE == " "
    assert recipe_mod.RESERVED_KEYWORDS == ("mihomo", "sing-box", "clash", "v2ray", "xray")
    assert (recipe_mod.MAX_PROCESSES, recipe_mod.MAX_PROCESS_NAME_LEN, recipe_mod.MAX_NAME_LEN) == (16, 64, 32)
    assert (recipe_mod.DEFAULT_MINUTES, recipe_mod.MAX_MINUTES) == (240, 720)


def test_recipe_and_upstream_are_frozen_with_a_default_of_240_minutes():
    rcp = make()
    assert rcp.max_minutes == 240 and rcp.upstream == Upstream("127.0.0.1", 1082)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rcp.name = "别的"
    with pytest.raises(dataclasses.FrozenInstanceError):
        rcp.upstream.port = 1


# ---------------------------------------------------------------- 1. 正常配方

GOOD = [
    pytest.param(("nikke.exe",), "127.0.0.1", 1082, ["nikke.exe"], id="one-process"),
    pytest.param(("a.exe", "B.exe", "c.exe"), "127.0.0.1", 1082, ["a.exe", "B.exe", "c.exe"], id="three-processes-in-order"),
    pytest.param(("My Game.exe",), "127.0.0.1", 1082, ["My Game.exe"], id="space-in-the-name"),
    pytest.param(("NIKKE.exe", "nikke.exe", "Nikke.EXE"), "127.0.0.1", 1082, ["NIKKE.exe"], id="case-duplicates-keep-first"),
    pytest.param(("A.exe", "b.exe", "a.EXE", "B.exe", "c.exe"), "127.0.0.1", 1082, ["A.exe", "b.exe", "c.exe"],
                 id="case-duplicates-keep-order"),
    pytest.param(["list.exe"], "127.0.0.1", 1082, ["list.exe"], id="processes-as-a-list"),
    pytest.param(("绝区零.exe", "Game_2-b+c.v1.exe"), "127.0.0.1", 7890, ["绝区零.exe", "Game_2-b+c.v1.exe"], id="unicode-and-punctuation"),
    pytest.param(("x.exe",), "127.0.0.2", 65535, ["x.exe"], id="other-loopback-address-and-port-65535"),
    pytest.param(("x.exe",), "127.255.255.254", 1, ["x.exe"], id="far-end-of-127-8-and-port-1"),
    pytest.param(tuple(f"game{i}.exe" for i in range(16)), "127.0.0.1", 1082, [f"game{i}.exe" for i in range(16)],
                 id="sixteen-processes"),
]


@pytest.mark.parametrize("processes,host,port,names", GOOD)
def test_good_recipe_builds_exactly_the_expected_config(processes, host, port, names):
    rcp = make(processes, host, port)
    assert problems(rcp) == []
    check(rcp)  # 不抛
    config = build(rcp)
    assert config == expected_config(names, host, port)
    # 逐键: 九个顶层键、九个 tun 键、一个出口, 键的顺序也照说明
    assert list(config) == TOP_KEYS
    assert list(config["tun"]) == TUN_KEYS
    assert config["proxies"] == [{"name": "upstream", "type": "socks5", "server": host, "port": port, "udp": True}]
    assert config["rules"][:-1] == [f"PROCESS-NAME,{name},upstream" for name in names]
    assert config["rules"][-1] == "MATCH,DIRECT"
    assert validate(config) == []
    text = render(config)
    assert text == json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert not text.endswith("\n") and "\n" not in text
    assert json.loads(text) == config
    assert fingerprint(config) == hashlib.sha256(text.encode("utf-8")).hexdigest()  # 写进文件的和算指纹的是同一串字节
    assert fingerprint(json.loads(text)) == fingerprint(config) == canonical_sha256(config)
    assert json.loads(pretty(config)) == config


def test_render_is_one_line_of_canonical_json_with_no_trailing_newline():
    config = build(make(("绝区零.exe",)))
    text = render(config)
    assert text == json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert json.loads(text) == config
    assert not text.endswith("\n") and "\n" not in text and "\r" not in text and ", " not in text and ": " not in text
    assert "绝区零.exe" in text and "\\u" not in text  # 中文不转义
    assert text.startswith('{"allow-lan":false,"dns":{"enable":false},"find-process-mode":"always","ipv6":false,')  # 键排了序
    assert len(text.encode("utf-8")) == len(text) + 6  # 三个汉字各占 3 个字节, 比字符数多 6
    assert render(config) == text  # 同一份配置每次一样


def test_the_fingerprint_is_the_sha256_of_the_very_bytes_render_gives():
    for rcp in (make(), make(("a.exe", "My Game.exe")), make(("绝区零.exe",), "127.0.0.2", 7890)):
        config = build(rcp)
        assert fingerprint(config) == hashlib.sha256(render(config).encode("utf-8")).hexdigest()
        assert fingerprint(config) != hashlib.sha256(pretty(config).encode("utf-8")).hexdigest()


def test_pretty_is_for_humans_only_and_stays_out_of_the_fingerprint():
    config = build(make(("绝区零.exe", "b.exe")))
    text = pretty(config)
    assert text == json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    assert json.loads(text) == config
    assert text.startswith('{\n  "mode": "rule",\n  "log-level": "info",\n') and "绝区零.exe" in text and "\\u" not in text
    assert text != render(config) and text.count("\n") > 30
    assert fingerprint(json.loads(text)) == fingerprint(config)  # 怎么排版都不影响指纹
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() != fingerprint(config)


def test_build_returns_fresh_objects_and_leaves_the_recipe_alone():
    rcp = make(("a.exe", "A.EXE", "b.exe"))
    first = build(rcp)
    first["tun"]["route-exclude-address"].append("1.2.3.4/32")
    first["tun"]["dns-hijack"].append("any:53")
    first["rules"].clear()
    first["proxies"][0]["port"] = 1
    assert build(rcp) == expected_config(["a.exe", "b.exe"])  # 随便改上一份, 下一份不受影响
    assert list(config_gen.EXCLUDE_V4) == EXCLUDE
    assert rcp == make(("a.exe", "A.EXE", "b.exe")) and rcp.processes == ("a.exe", "A.EXE", "b.exe")


def test_unique_processes_is_case_insensitive_and_keeps_the_first_spelling():
    assert unique_processes(("A.exe", "b.exe", "a.EXE", "B.EXE", "c.exe")) == ("A.exe", "b.exe", "c.exe")
    assert unique_processes(["x.exe"]) == ("x.exe",)
    assert unique_processes(()) == ()


# ---------------------------------------------------------------- 指纹

def _reverse_keys(obj):
    """每一层字典的键倒着排 (值不变)."""
    if isinstance(obj, dict):
        return {key: _reverse_keys(obj[key]) for key in reversed(list(obj))}
    if isinstance(obj, list):
        return [_reverse_keys(item) for item in obj]
    return obj


def _leaf_paths(obj, prefix=()):
    """所有叶子的位置; 空列表、空字典也算一个叶子."""
    if isinstance(obj, dict) and obj:
        for key, value in obj.items():
            yield from _leaf_paths(value, prefix + (key,))
    elif isinstance(obj, list) and obj:
        for index, value in enumerate(obj):
            yield from _leaf_paths(value, prefix + (index,))
    else:
        yield prefix


def _get(obj, path):
    for step in path:
        obj = obj[step]
    return obj


def _put(obj, path, value):
    _get(obj, path[:-1])[path[-1]] = value


def _different(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "x"
    if isinstance(value, list):
        return value + ["x"]
    raise AssertionError(f"没想到会有这种叶子: {value!r}")


def test_fingerprint_is_stable_and_is_the_sha256_of_the_canonical_json():
    config = build(make(("a.exe", "b.exe")))
    first = fingerprint(config)
    assert fingerprint(copy.deepcopy(config)) == first == fingerprint(config)
    assert len(first) == 64 and first == first.lower() and int(first, 16) > 0
    assert first == canonical_sha256(config)
    assert first == hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False).encode("utf-8")).hexdigest()


def test_render_and_fingerprint_ignore_key_order_but_not_list_order():
    config = build(make(("a.exe", "b.exe", "c.exe")))
    reordered = _reverse_keys(config)
    assert list(reordered) != list(config) and json.dumps(reordered) != json.dumps(config)  # 确实换了顺序
    assert render(reordered) == render(config)  # render 是规范形式: 和键的顺序无关
    assert fingerprint(reordered) == fingerprint(config)
    assert pretty(reordered) != pretty(config)  # pretty 照传进来的键顺序排
    swapped = copy.deepcopy(config)
    swapped["rules"][0], swapped["rules"][1] = swapped["rules"][1], swapped["rules"][0]
    assert fingerprint(swapped) != fingerprint(config) and render(swapped) != render(config)


def test_fingerprint_changes_when_any_single_field_changes():
    config = build(make(("a.exe", "b.exe")))
    base = fingerprint(config)
    seen = {base}
    paths = list(_leaf_paths(config))
    assert len(paths) >= 25  # 每个叶子都试: 5 个顶层值、tun 15 个、dns 1 个、出口 5 个、规则 3 条
    for path in paths:
        changed = copy.deepcopy(config)
        _put(changed, path, _different(_get(changed, path)))
        assert fingerprint(changed) != base, path
        seen.add(fingerprint(changed))
    assert len(seen) == len(paths) + 1  # 改不同的地方, 指纹也各不相同
    # 多一个键、少一个键也变
    extra = copy.deepcopy(config)
    extra["secret"] = "x"
    fewer = copy.deepcopy(config)
    del fewer["dns"]
    assert fingerprint(extra) != base and fingerprint(fewer) != base and fingerprint(extra) != fingerprint(fewer)


# 对拍向量: 几组"配方 -> render 全文 -> 指纹", 以后另一种语言的实现 (提权助手那一边) 要和它们算得一模一样.
# render 全文就是写进配置文件的那串字节 (UTF-8), 指纹是它的 SHA-256. 前三组是组长点名的 (进程名只是测试数据); 全文照手抄的期望配置
# 经 json.dumps (键排序、紧凑分隔符、不转义非 ASCII) 得到, 下面的测试把它和生成器的产出互相对过. 过了 validate 的配置里, 字符串
# 不含任何要转义的字符 (引号、反斜杠、控制字符都被拒), 另一种语言的实现只要按 UTF-8 原样输出, 斜杠也不转义.
_HEAD = '{"allow-lan":false,"dns":{"enable":false},"find-process-mode":"always","ipv6":false,"log-level":"info","mode":"rule",'
_TAIL = ('"tun":{"auto-detect-interface":true,"auto-route":true,"device":"WM-Ayane","dns-hijack":[],"dns-mode":"disabled",'
         '"enable":true,"route-exclude-address":["10.0.0.0/8","172.16.0.0/12","192.168.0.0/16","100.64.0.0/10",'
         '"169.254.0.0/16","224.0.0.0/4","255.255.255.255/32"],"stack":"gvisor","strict-route":false}}')
VECTOR_TEXTS = {
    "one": (_HEAD + '"proxies":[{"name":"upstream","port":1082,"server":"127.0.0.1","type":"socks5","udp":true}],'
            '"rules":["PROCESS-NAME,nikke.exe,upstream","MATCH,DIRECT"],' + _TAIL),
    "three": (_HEAD + '"proxies":[{"name":"upstream","port":1083,"server":"127.0.0.1","type":"socks5","udp":true}],'
              '"rules":["PROCESS-NAME,nikke.exe,upstream","PROCESS-NAME,nikke_launcher.exe,upstream",'
              '"PROCESS-NAME,tbs_browser.exe,upstream","MATCH,DIRECT"],' + _TAIL),
    "space-and-non-ascii": (_HEAD + '"proxies":[{"name":"upstream","port":7897,"server":"127.0.0.1","type":"socks5","udp":true}],'
                            '"rules":["PROCESS-NAME,Wuthering Waves.exe,upstream","PROCESS-NAME,原神.exe,upstream",'
                            '"MATCH,DIRECT"],' + _TAIL),
}
VECTORS = [
    pytest.param(("nikke.exe",), "127.0.0.1", 1082, ["nikke.exe"], VECTOR_TEXTS["one"],
                 "9e6bf780287212dbc5aa6206377cd4e6feba051ae8d4bb1d4e2e89190a012510", id="one-process"),
    pytest.param(("nikke.exe", "nikke_launcher.exe", "tbs_browser.exe"), "127.0.0.1", 1083,
                 ["nikke.exe", "nikke_launcher.exe", "tbs_browser.exe"], VECTOR_TEXTS["three"],
                 "1c3877ff1b675dc4312672a445295f468ebb78cd02a0f9e7c211de938194f2f6", id="three-processes-port-1083"),
    pytest.param(("Wuthering Waves.exe", "原神.exe"), "127.0.0.1", 7897, ["Wuthering Waves.exe", "原神.exe"],
                 VECTOR_TEXTS["space-and-non-ascii"],
                 "88a7a9666e2ee43115527a45c244064a7f40bbc611bf10f7fb3bb3e1a975a393", id="space-and-non-ascii-names"),
    # 下面三组是我自己加的, 只钉指纹
    pytest.param(("a.exe", "B.exe", "c d.exe"), "127.0.0.1", 1082, ["a.exe", "B.exe", "c d.exe"], None,
                 "4b634f7aa39f8d30483b2bd987bfb692ba8a697fa3453f0e539f48933633d9e6", id="three-with-a-space"),
    pytest.param(("绝区零.exe",), "127.0.0.2", 7890, ["绝区零.exe"], None,
                 "b287d5995aa820e5fc0cf23773bb6b1295b7a07eff827d400bdb9898901f99d1", id="non-ascii-other-loopback"),
    pytest.param(("NIKKE.exe", "nikke.exe"), "127.0.0.1", 65535, ["NIKKE.exe"], None,
                 "fee4505398cc60bd53712c429c3e1cb0bd47c4d464559d5d4b6b9aacd7b39888", id="case-duplicate-and-port-65535"),
]


@pytest.mark.parametrize("processes,host,port,names,text,pinned", VECTORS)
def test_pinned_render_texts_and_fingerprints_for_cross_implementation_checks(processes, host, port, names, text, pinned):
    independent = json.dumps(expected_config(names, host, port), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    config = build(make(processes, host, port))
    if text is not None:
        assert independent == text  # 钉死的全文 = 手抄的期望配置 dumps 出来的
        assert render(config) == text  # 也 = 生成器的产出
    assert hashlib.sha256(independent.encode("utf-8")).hexdigest() == pinned  # 不经过 fingerprint 算出来的
    assert fingerprint(config) == pinned  # 生成器的产出
    assert fingerprint(config) == hashlib.sha256(render(config).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 2. 上游不合法

BAD_HOSTS = [
    "localhost", "0.0.0.0", "192.168.1.2", "::1", "example.com", "",
    "127.0.0.1 ", " 127.0.0.1", "127.0.0.1\n", "127.1", "127.0.0.01", "0x7f.0.0.1", "[::1]", "::ffff:127.0.0.1",
    "128.0.0.1", "126.255.255.255", "10.0.0.1", "1.2.3.4", "127.0.0.256", "127.0.0.1:1082", "127。0.0.1",
    None, 127, b"127.0.0.1", ipaddress.IPv4Address("127.0.0.1"),
]


@pytest.mark.parametrize("host", [pytest.param(host, id=repr(host)) for host in BAD_HOSTS])
def test_bad_upstream_host_is_named_and_refused(host):
    rcp = make(host=host)
    found = problems(rcp)
    assert len(found) == 1 and found[0].startswith("upstream.host"), found  # 说得出是哪一项, 而且只有这一项
    with pytest.raises(RecipeError) as err:
        build(rcp)
    assert err.value.problems == found
    with pytest.raises(RecipeError):
        check(rcp)


BAD_PORTS = [0, 65536, "1082", True, False, -1, 1082.0, None, 10 ** 30, [1082], b"1082"]


@pytest.mark.parametrize("port", [pytest.param(port, id=repr(port)) for port in BAD_PORTS])
def test_bad_upstream_port_is_named_and_refused(port):
    rcp = make(port=port)
    found = problems(rcp)
    assert len(found) == 1 and found[0].startswith("upstream.port"), found
    with pytest.raises(RecipeError) as err:
        build(rcp)
    assert err.value.problems == found


def test_the_whole_127_8_block_is_accepted_for_the_upstream_and_nothing_else():
    for host in ("127.0.0.1", "127.0.0.2", "127.1.2.3", "127.255.255.255"):
        assert problems(make(host=host)) == [], host
    for port in (1, 1082, 65535):
        assert problems(make(port=port)) == [], port
    assert problems(make(host="128.0.0.1")) and problems(make(host="126.0.0.1"))


# ---------------------------------------------------------------- 3. 进程名不合法

PUNCTUATION = {"comma": ",", "paren-open": "(", "paren-close": ")", "bracket": "[", "brace": "{", "double-quote": '"',
               "single-quote": "'", "backtick": "`", "star": "*", "question": "?", "colon": ":", "semicolon": ";",
               "pipe": "|", "less-than": "<", "greater-than": ">", "percent": "%", "ampersand": "&", "equals": "=",
               "at": "@", "hash": "#", "dollar": "$", "bang": "!", "tilde": "~", "caret": "^"}

BAD_PROCESSES = [
    pytest.param((), "至少要点名", id="empty-tuple"),
    pytest.param([], "至少要点名", id="empty-list"),
    pytest.param(tuple(f"g{i}.exe" for i in range(17)), "最多 16", id="seventeen"),
    pytest.param(("a.exe", "A.EXE") + tuple(f"g{i}.exe" for i in range(15)), "最多 16", id="seventeen-even-with-a-duplicate"),
    pytest.param(("C:\\x\\a.exe",), "路径分隔符", id="windows-path"),
    pytest.param(("\\\\host\\share\\a.exe",), "路径分隔符", id="unc-path"),
    pytest.param(("a/b.exe",), "路径分隔符", id="slash-path"),
    pytest.param(("../a.exe",), "路径分隔符", id="dot-dot-path"),
    *[pytest.param((f"a{char}b.exe",), "不允许的字符", id=f"punctuation-{label}") for label, char in PUNCTUATION.items()],
    pytest.param(("a",), ".exe", id="no-suffix"),
    pytest.param(("a.txt",), ".exe", id="txt"),
    pytest.param(("a.exe.txt",), ".exe", id="exe-in-the-middle"),
    pytest.param(("aexe",), ".exe", id="exe-without-the-dot"),
    pytest.param(("a.bat",), ".exe", id="bat"),
    pytest.param(("a" * 61 + ".exe",), "太长", id="too-long"),
    pytest.param(("a" * 200 + ".exe",), "太长", id="far-too-long"),
    pytest.param((" a.exe",), "空格", id="leading-space"),
    pytest.param(("a.exe ",), "空格", id="trailing-space"),
    pytest.param(("a  b.exe",), "连续的空格", id="double-space"),
    pytest.param(("a\n.exe",), "不允许的字符", id="newline-in-the-middle"),
    pytest.param(("a.exe\n",), "不允许的字符", id="newline-at-the-end"),
    pytest.param(("a\t.exe",), "不允许的字符", id="tab"),
    pytest.param(("a\r.exe",), "不允许的字符", id="carriage-return"),
    pytest.param(("a\x00.exe",), "不允许的字符", id="nul"),
    pytest.param(("a\x7f.exe",), "不允许的字符", id="del"),
    pytest.param(("a\u00a0b.exe",), "不允许的字符", id="no-break-space"),
    pytest.param(("a\u3000b.exe",), "不允许的字符", id="ideographic-space"),
    pytest.param(("a\u200bb.exe",), "不允许的字符", id="zero-width-space"),
    pytest.param(("a\u202eb.exe",), "不允许的字符", id="right-to-left-override"),
    pytest.param(("a\u2028b.exe",), "不允许的字符", id="line-separator"),
    pytest.param(("a，b.exe",), "不允许的字符", id="fullwidth-comma"),
    pytest.param(("a²b.exe",), "不允许的字符", id="superscript-digit"),
    pytest.param((".exe",), "实字", id="empty-stem"),
    pytest.param(("..exe",), "实字", id="dots-only-stem"),
    pytest.param(("-.exe",), "实字", id="dash-only-stem"),
    pytest.param(("+_-.exe",), "实字", id="punctuation-only-stem"),
    pytest.param((" .exe",), "实字", id="space-only-stem"),
    pytest.param(("",), "是空的", id="empty-string"),
    pytest.param((123,), "必须是字符串", id="int"),
    pytest.param((None,), "必须是字符串", id="none"),
    pytest.param((b"a.exe",), "必须是字符串", id="bytes"),
    pytest.param((["a.exe"],), "必须是字符串", id="nested-list"),
    pytest.param(("ok.exe", 5), "必须是字符串", id="int-after-a-good-one"),
    pytest.param("nikke.exe", "必须是进程名的元组", id="bare-string"),
    pytest.param({"a.exe"}, "必须是进程名的元组", id="set"),
    pytest.param((name for name in ("a.exe",)), "必须是进程名的元组", id="generator"),
    pytest.param(None, "必须是进程名的元组", id="none-instead-of-a-tuple"),
]


@pytest.mark.parametrize("processes,fragment", BAD_PROCESSES)
def test_bad_process_names_are_refused_with_a_reason(processes, fragment):
    rcp = make(processes)
    found = problems(rcp)
    assert any(fragment in line for line in found), found
    assert all(line.startswith("processes") for line in found), found  # 只有进程名这一项有问题
    with pytest.raises(RecipeError) as err:
        build(rcp)
    assert err.value.problems == found


def test_only_the_bad_one_among_good_names_is_pointed_at():
    found = problems(make(("ok.exe", "bad,name.exe", "also ok.exe")))
    assert len(found) == 1 and found[0].startswith("processes[1]")


GOOD_NAMES = ["a.exe", "A.EXE", "x.Exe", "nikke.exe", "My Game.exe", "Game Launcher 2.exe", "Game_2-b+c.v1.exe", "1.exe",
              "a.b.c.exe", "绝区零.exe", "崩坏 星穹铁道.exe", "ゲーム.exe", "게임.exe", "a" * 60 + ".exe", "Wuthering Waves.exe", "原神.exe"]


@pytest.mark.parametrize("name", [pytest.param(name, id=name[:20]) for name in GOOD_NAMES])
def test_ordinary_game_file_names_pass(name):
    assert problems(make((name,))) == []
    assert build(make((name,)))["rules"][0] == f"PROCESS-NAME,{name},upstream"


# ---------------------------------------------------------------- 4. 保留名

RESERVED_PLAIN = ["python.exe", "PYTHON.EXE", "Pythonw.exe", "wmpet.exe", "WMPet.exe", "svchost.exe", "SvcHost.exe",
                  "services.exe", "lsass.exe", "nssm.exe", "System.exe", "SYSTEM.EXE", "wm-elevate.exe", "WM-Elevate.EXE",
                  "python3.exe", "py.exe", "pyw.exe", "conhost.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "smss.exe",
                  "dwm.exe", "explorer.exe", "SVCHOST.EXE", "Python.EXE", "Explorer.EXE", "PY.exe"]
PROXY_WORDS = ["mihomo.exe", "Mihomo-Alpha.exe", "sing-box.exe", "my sing-box helper.exe", "SING-BOX.EXE", "clash.exe",
               "Clash Verge.exe", "clash-verge-service.exe", "xray.exe", "XRAY.EXE", "my-xray-tool.exe", "v2ray.exe",
               "v2rayN.exe", "V2RAY-core.exe"]


@pytest.mark.parametrize("name", [pytest.param(name, id=name) for name in RESERVED_PLAIN])
def test_system_and_own_processes_are_reserved(name):
    found = problems(make((name,)))
    assert len(found) == 1 and "保留名" in found[0], found
    with pytest.raises(RecipeError):
        build(make((name,)))


@pytest.mark.parametrize("name", [pytest.param(name, id=name) for name in PROXY_WORDS])
def test_names_containing_a_proxy_program_name_are_refused(name):
    found = problems(make((name,)))
    assert len(found) == 1 and "代理程序自己" in found[0], found
    with pytest.raises(RecipeError):
        build(make((name,)))


def test_look_alike_letters_are_refused_by_the_table_and_not_mistaken_for_reserved_names():
    """长 s 之类写法: 表里根本进不来 (不靠折叠去认); 也不会因为"折叠后像保留名"被当成保留名 (只有 ASCII 大小写不分)."""
    for name in ("\u017fvchost.exe", "cla\u017fh.exe", "\u017fing-box.exe", "\uff30\uff39\uff34\uff28\uff2f\uff2e.exe"):
        found = problems(make((name,)))
        assert any("不允许的字符" in line for line in found), (name, found)
        assert not any("保留名" in line or "代理程序自己" in line for line in found), (name, found)


# ---- 字符表 (接口契约第 7 节): 进程名和配方名只认这张表, 表外的一律不许; "不分大小写"只指 ASCII

OUTSIDE_THE_TABLE = {
    "long-s": "\u017f", "fullwidth-A": "\uff21", "e-acute": "\u00e9", "cyrillic-a": "\u0430", "greek-alpha": "\u03b1",
    "cjk-extension-a": "\u3400", "halfwidth-katakana": "\uff76", "katakana-middle-dot": "\u30fb",
    "iteration-mark": "\u3005", "ideographic-space": "\u3000", "fullwidth-digit": "\uff10",
    "arabic-indic-digit": "\u0660", "kelvin-sign": "\u212a",
}
# 区间边界: 每个区间两端的码点 (在表里) / 刚好在两端外面的码点 (不在表里)
BOUNDARY_INSIDE = [0x4E00, 0x9FFF, 0x3041, 0x3096, 0x30A1, 0x30FA, 0x30FC, 0xAC00, 0xD7A3]
BOUNDARY_OUTSIDE = [0x4DFF, 0xA000, 0x3040, 0x3097, 0x30A0, 0x30FD, 0xABFF, 0xD7A4]


@pytest.mark.parametrize("char", list(OUTSIDE_THE_TABLE.values()), ids=list(OUTSIDE_THE_TABLE))
def test_characters_outside_the_table_are_refused_everywhere(char):
    marker = f"U+{ord(char):04X}"
    for name in (f"a{char}b.exe", f"{char}.exe", f"x{char}.exe", f"{char}x.exe"):
        found = problems(make((name,)))
        assert any("不允许的字符" in line and marker in line for line in found), (name, found)  # 说得出是哪个字符
    found = problems(make(name=f"a{char}b"))
    assert found and all(line.startswith("name") for line in found), found
    assert any("不允许的字符" in line and marker in line for line in found), found
    config = build(make())
    config["rules"].insert(0, f"PROCESS-NAME,a{char}b.exe,upstream")  # 复查里的规则名也一样
    assert any("不允许的字符" in line and marker in line for line in validate(config))


@pytest.mark.parametrize("code", BOUNDARY_INSIDE, ids=lambda code: f"U+{code:04X}")
def test_the_two_ends_of_every_range_are_inside_the_table(code):
    char = chr(code)
    for name in (f"{char}.exe", f"a{char}b.exe", f"{char}{char}.exe", f"x {char}.exe"):
        assert problems(make((name,))) == [], name
    assert problems(make(name=char)) == [] and problems(make(name=f"a {char}")) == []


@pytest.mark.parametrize("code", BOUNDARY_OUTSIDE, ids=lambda code: f"U+{code:04X}")
def test_the_code_points_just_outside_the_ranges_are_refused(code):
    char = chr(code)
    for name in (f"{char}.exe", f"a{char}b.exe", f"x{char}.exe"):
        assert any("不允许的字符" in line for line in problems(make((name,)))), name
    assert any("不允许的字符" in line for line in problems(make(name=f"a{char}")))


def test_the_ascii_neighbours_of_the_ascii_ranges_follow_the_table():
    for char in "09AZaz":
        assert problems(make((f"{char}.exe",))) == [], char
    for char in ".-_+":  # 符号单独当文件名不行 (没有实字), 夹在实字中间可以
        assert problems(make((f"a{char}b.exe",))) == [], char
    for char in ("/", ":", "@", "[", "`", "{", "\x1f", "\x7f", ",", ";", "'", '"', "*", "?", "<", ">", "|", "(", ")"):
        assert any("不允许的字符" in line or "路径分隔符" in line for line in problems(make((f"a{char}b.exe",)))), repr(char)


def test_the_character_table_is_exactly_what_the_contract_says():
    """全部 0x110000 个码点各查一遍, 和手抄的区间 (不读 recipe 里的常量) 逐个对."""
    solid = set()
    for low, high in ((0x30, 0x39), (0x41, 0x5A), (0x61, 0x7A), (0x4E00, 0x9FFF), (0x3041, 0x3096), (0x30A1, 0x30FA),
                      (0x30FC, 0x30FC), (0xAC00, 0xD7A3)):
        solid.update(range(low, high + 1))
    assert len(solid) == 32403
    counts = {"solid": 0, "symbol": 0, "space": 0, None: 0}
    for code in range(0x110000):
        kind = recipe_mod.char_kind(chr(code))
        counts[kind] += 1
        if code in solid:
            assert kind == "solid", hex(code)
        elif code == 0x20:
            assert kind == "space", hex(code)
        elif code in (0x2E, 0x2D, 0x5F, 0x2B):  # . - _ +
            assert kind == "symbol", hex(code)
        else:
            assert kind is None, hex(code)
    assert counts == {"solid": 32403, "symbol": 4, "space": 1, None: 0x110000 - 32403 - 5}


def test_no_character_in_the_table_needs_json_escaping():
    """契约说"进程名规则保证了字符串里不会出现要转义的字符": 表里每个字符放进 JSON 字符串都原样输出 (另一种语言的实现不用管转义规则)."""
    allowed = {0x20, 0x2E, 0x2D, 0x5F, 0x2B}
    for low, high in ((0x30, 0x39), (0x41, 0x5A), (0x61, 0x7A), (0x4E00, 0x9FFF), (0x3041, 0x3096), (0x30A1, 0x30FA),
                      (0x30FC, 0x30FC), (0xAC00, 0xD7A3)):
        allowed.update(range(low, high + 1))
    assert len(allowed) == 32403 + 5
    for code in allowed:
        char = chr(code)
        assert json.dumps(char, ensure_ascii=False) == f'"{char}"', hex(code)


def test_only_ascii_letters_change_case():
    assert recipe_mod.ascii_lower("ABCxyz123.EXE-_+ ") == "abcxyz123.exe-_+ "
    others = "\u017f\u212a\u00c9\u0130\uff21\u03a3\u0410\u00df"  # 长 s、开尔文符号、É、İ、全角 A、Σ、西里尔 А、ß: 原样, 大写的也不变小写
    assert recipe_mod.ascii_lower(others) == others
    assert recipe_mod.ascii_lower("") == ""


def test_dedupe_compares_only_ascii_case():
    assert unique_processes(("NIKKE.exe", "nikke.exe", "Nikke.EXE")) == ("NIKKE.exe",)
    assert unique_processes(("\u00c9.exe", "\u00e9.exe")) == ("\u00c9.exe", "\u00e9.exe")  # 非 ASCII 的大小写对不合并


BAD_LABELS_BY_THE_TABLE = ["胜利女神（NIKKE）", "胜利女神：NIKKE", "胜利女神\tNIKKE", "NIKKE (x64)", "NIKKE:x", "a/b", "a\\b", "...", "- -",
                           "+", "_ _", " ", "  ", "-", ".", "\u3000"]


@pytest.mark.parametrize("label", [pytest.param(label, id=repr(label)[:24]) for label in BAD_LABELS_BY_THE_TABLE])
def test_recipe_names_outside_the_table_or_without_a_solid_character_are_refused(label):
    found = problems(make(name=label))
    assert found and all(line.startswith("name") for line in found), found
    with pytest.raises(RecipeError):
        build(make(name=label))


@pytest.mark.parametrize("name", ["python", "PYTHONW", "System", "system", "wm-elevate", "WM-ELEVATE", "wmpet"])
def test_reserved_names_count_with_or_without_the_exe_suffix(name):
    assert any("保留名" in line for line in process_problems(name)), name


# 上游端口的属主: (属主的写法, 配方里点名的写法)
OWNER_CASES = [("sing-box.exe", "sing-box.exe"), ("sing-box.exe", "SING-BOX.EXE"), ("sing-box", "Sing-Box.exe"),
               ("myproxy.exe", "MyProxy.EXE"), ("MyProxy", "myproxy.exe"), ("MYPROXY.EXE", "myproxy.exe"),
               ("  myproxy.exe ", "myproxy.exe"), ("C:\\Tools\\MyProxy\\myproxy.exe", "myproxy.exe"),
               ("C:/Tools/MyProxy/myproxy.exe", "myproxy.exe")]


@pytest.mark.parametrize("owner,name", OWNER_CASES)
def test_the_owner_of_the_upstream_port_cannot_be_named(owner, name):
    rcp = make((name,))
    found = problems(rcp, upstream_owner=owner)
    assert found and any(name in line for line in found), found
    with pytest.raises(RecipeError):
        check(rcp, upstream_owner=owner)
    with pytest.raises(RecipeError):
        build(rcp, upstream_owner=owner)


def test_the_owner_check_only_applies_when_the_owner_is_given():
    rcp = make(("MyProxy.exe", "game.exe"))
    assert problems(rcp) == []
    assert problems(rcp, upstream_owner=None) == []
    assert problems(rcp, upstream_owner="other.exe") == []
    assert problems(rcp, upstream_owner="myproxy.exe")
    assert build(rcp)["rules"][0] == "PROCESS-NAME,MyProxy.exe,upstream"
    # 属主自己写得不对也只是问题, 不抛异常
    for odd in ("", "   ", ".exe", 5, b"x.exe", ["x.exe"]):
        found = problems(make(), upstream_owner=odd)
        assert found and found[0].startswith("upstream_owner"), (odd, found)


def test_process_problems_names_the_place_it_was_found():
    assert process_problems("fine.exe") == []
    assert process_problems("a,b.exe", path="rules[3]")[0].startswith("rules[3]：")
    assert process_problems("a,b.exe")[0].startswith("processes[0]：")
    assert process_problems("sing-box.exe", upstream_owner="sing-box.exe")
    assert process_problems(5)[0].startswith("processes[0]：")


# ---------------------------------------------------------------- 配方的其它字段

BAD_LABELS = ["", "   ", "\t\n", "x" * 33, "x" * 65, "\u7edd" * 33, "a\nb", "a\tb", "a\x00b", "a\u200bb", "a\u202eb", "a\u2028b", None, 5,
              ["x"], b"x"]


@pytest.mark.parametrize("label", [pytest.param(label, id=repr(label)[:24]) for label in BAD_LABELS])
def test_bad_recipe_names_are_refused(label):
    found = problems(make(name=label))
    assert found and all(line.startswith("name") for line in found), found
    with pytest.raises(RecipeError):
        build(make(name=label))


@pytest.mark.parametrize("label", ["NIKKE", "胜利女神 NIKKE", "x" * 32, "绝" * 32, "a b-c_d x64", "a", "ゲーム", "게임", "1",
                                   "a.b+c", "Wuthering Waves", "原神 2.0"])
def test_ordinary_recipe_names_pass(label):
    assert problems(make(name=label)) == []


@pytest.mark.parametrize("minutes", [0, -1, 721, 10 ** 9, "240", True, False, 1.5, None, [240]])
def test_bad_max_minutes_is_refused(minutes):
    found = problems(make(max_minutes=minutes))
    assert len(found) == 1 and found[0].startswith("max_minutes"), found
    with pytest.raises(RecipeError):
        build(make(max_minutes=minutes))


@pytest.mark.parametrize("minutes", [1, 60, 240, 720])
def test_good_max_minutes_passes_and_stays_out_of_the_config(minutes):
    assert problems(make(max_minutes=minutes)) == []
    assert build(make(max_minutes=minutes)) == build(make())  # 时长是看守的事, 不进核心的配置


def test_check_raises_once_with_every_problem_in_it():
    rcp = Recipe(name="", processes=("a,b.exe", "python.exe"), upstream=Upstream("localhost", 0), max_minutes=0)
    found = problems(rcp)
    assert len(found) >= 6
    for prefix in ("name", "upstream.host", "upstream.port", "processes[0]", "processes[1]", "max_minutes"):
        assert any(line.startswith(prefix) for line in found), (prefix, found)
    with pytest.raises(RecipeError) as err:
        check(rcp)
    assert isinstance(err.value, ValueError) and err.value.problems == found
    assert all(line in str(err.value) for line in found) and f"{len(found)} 个问题" in str(err.value)
    assert check(make()) is None
    assert RecipeError("就一句话").problems == ["就一句话"]  # 传进来的是一句话, 不会被拆成一个个字
    assert RecipeError().problems == []


GARBAGE_RECIPES = [
    None, 0, "recipe", [], {}, object(), ("nikke.exe",), {"name": "x", "processes": ["a.exe"]},
    Recipe(None, None, None, None), Recipe("x", ("a.exe",), None), Recipe("x", ("a.exe",), "127.0.0.1:1082"),
    Recipe("x", ("a.exe",), Upstream(None, None)), Recipe("x", ("a.exe",), Upstream([], {})),
    Recipe(5, [5], Upstream(5, "5"), "5"), Recipe("x", "a.exe", Upstream("127.0.0.1", 1082)),
    Recipe("x", (("a.exe",),), Upstream("127.0.0.1", 1082)), Recipe("x", (object(),), Upstream("127.0.0.1", 1082)),
    Recipe("x", ("a.exe",), Upstream("127.0.0.1", 1082), float("nan")),
    Recipe("x", ("a.exe",), Upstream("127.0.0.1", 10 ** 5000)),
]


@pytest.mark.parametrize("garbage", [pytest.param(item, id=f"{index}-{type(item).__name__}")
                                     for index, item in enumerate(GARBAGE_RECIPES)])
def test_garbage_recipes_are_problems_never_exceptions(garbage):
    found = problems(garbage)
    assert found and all(type(line) is str for line in found)
    assert not any("意外" in line for line in found), found  # 是被规则拒的, 不是被兜底的"意外"拒的
    with pytest.raises(RecipeError):
        check(garbage)
    with pytest.raises(RecipeError):
        build(garbage)


def test_a_subclass_does_not_count_as_the_real_type():
    class Port(int):
        pass

    class Host(str):
        pass

    assert problems(make(port=Port(1082)))
    assert problems(make(host=Host("127.0.0.1")))
    assert problems(make(name=Host("x")))
    assert problems(make((Host("a.exe"),)))


# ---------------------------------------------------------------- 5. 复查 validate

def case(ident, mutate, fragment, owner=None):
    return pytest.param(mutate, owner, fragment, id=ident)


TAMPERS = [
    # 顶层
    case("top-drop-dns", lambda c: c.pop("dns"), "'dns'"),
    case("top-drop-rules", lambda c: c.pop("rules"), "'rules'"),
    case("top-drop-tun", lambda c: c.pop("tun"), "'tun'"),
    case("top-allow-lan-true", lambda c: c.update({"allow-lan": True}), "allow-lan"),
    case("top-allow-lan-1", lambda c: c.update({"allow-lan": 1}), "allow-lan"),
    case("top-allow-lan-string", lambda c: c.update({"allow-lan": "false"}), "allow-lan"),
    case("top-mode-global", lambda c: c.update({"mode": "global"}), "mode"),
    case("top-mode-direct", lambda c: c.update({"mode": "direct"}), "mode"),
    case("top-ipv6-true", lambda c: c.update({"ipv6": True}), "ipv6"),
    case("top-find-process-off", lambda c: c.update({"find-process-mode": "off"}), "find-process-mode"),
    case("top-find-process-strict", lambda c: c.update({"find-process-mode": "strict"}), "find-process-mode"),
    case("top-log-level-silent", lambda c: c.update({"log-level": "silent"}), "log-level"),
    case("top-log-level-list", lambda c: c.update({"log-level": ["info"]}), "log-level"),
    case("top-log-level-none", lambda c: c.update({"log-level": None}), "log-level"),
    # tun
    case("tun-strict-route-true", lambda c: c["tun"].update({"strict-route": True}), "tun.strict-route"),
    case("tun-dns-hijack-any-53", lambda c: c["tun"].update({"dns-hijack": ["any:53"]}), "tun.dns-hijack"),
    case("tun-dns-hijack-none", lambda c: c["tun"].update({"dns-hijack": None}), "tun.dns-hijack"),
    case("tun-dns-mode-hijack", lambda c: c["tun"].update({"dns-mode": "hijack"}), "tun.dns-mode"),
    case("tun-device-renamed", lambda c: c["tun"].update({"device": "Meta"}), "tun.device"),
    case("tun-device-case", lambda c: c["tun"].update({"device": "wm-ayane"}), "tun.device"),
    case("tun-stack-system", lambda c: c["tun"].update({"stack": "system"}), "tun.stack"),
    case("tun-stack-list", lambda c: c["tun"].update({"stack": ["gvisor"]}), "tun.stack"),
    case("tun-enable-1", lambda c: c["tun"].update({"enable": 1}), "tun.enable"),
    case("tun-enable-false", lambda c: c["tun"].update({"enable": False}), "tun.enable"),
    case("tun-auto-route-string", lambda c: c["tun"].update({"auto-route": "true"}), "tun.auto-route"),
    case("tun-auto-detect-off", lambda c: c["tun"].update({"auto-detect-interface": False}), "tun.auto-detect-interface"),
    case("tun-exclude-lose-one", lambda c: c["tun"]["route-exclude-address"].pop(), "tun.route-exclude-address"),
    case("tun-exclude-lose-the-first", lambda c: c["tun"]["route-exclude-address"].pop(0), "缺了"),
    case("tun-exclude-add-one", lambda c: c["tun"]["route-exclude-address"].append("8.8.8.0/24"), "多了"),
    case("tun-exclude-repeat-one", lambda c: c["tun"]["route-exclude-address"].append("10.0.0.0/8"), "多了"),
    case("tun-exclude-swap-one", lambda c: c["tun"]["route-exclude-address"].__setitem__(0, "10.0.0.0/9"), "缺了"),
    case("tun-exclude-empty", lambda c: c["tun"].update({"route-exclude-address": []}), "缺了"),
    case("tun-exclude-not-a-list", lambda c: c["tun"].update({"route-exclude-address": "10.0.0.0/8"}), "tun.route-exclude-address"),
    case("tun-exclude-not-strings", lambda c: c["tun"].update({"route-exclude-address": [1, 2, 3]}), "tun.route-exclude-address"),
    case("tun-extra-auto-redirect", lambda c: c["tun"].update({"auto-redirect": True}), "auto-redirect"),
    case("tun-extra-inet4-route-address", lambda c: c["tun"].update({"route-address": ["0.0.0.0/1"]}), "route-address"),
    case("tun-missing-device", lambda c: c["tun"].pop("device"), "'device'"),
    case("tun-not-a-dict", lambda c: c.update({"tun": []}), "tun："),
    case("tun-none", lambda c: c.update({"tun": None}), "tun："),
    # dns
    case("dns-enable-true", lambda c: c["dns"].update({"enable": True}), "dns.enable"),
    case("dns-enable-1", lambda c: c["dns"].update({"enable": 1}), "dns.enable"),
    case("dns-extra-nameserver", lambda c: c["dns"].update({"nameserver": ["1.1.1.1"]}), "nameserver"),
    case("dns-empty", lambda c: c.update({"dns": {}}), "'enable'"),
    case("dns-not-a-dict", lambda c: c.update({"dns": False}), "dns："),
    # proxies
    case("proxies-second-one", lambda c: c["proxies"].append(copy.deepcopy(c["proxies"][0])), "恰好一个"),
    case("proxies-empty", lambda c: c.update({"proxies": []}), "恰好一个"),
    case("proxies-type-http", lambda c: c["proxies"][0].update({"type": "http"}), "proxies[0].type"),
    case("proxies-type-ss", lambda c: c["proxies"][0].update({"type": "ss"}), "proxies[0].type"),
    case("proxies-server-public", lambda c: c["proxies"][0].update({"server": "1.2.3.4"}), "proxies[0].server"),
    case("proxies-server-localhost", lambda c: c["proxies"][0].update({"server": "localhost"}), "proxies[0].server"),
    case("proxies-server-ipv6", lambda c: c["proxies"][0].update({"server": "::1"}), "proxies[0].server"),
    case("proxies-server-any", lambda c: c["proxies"][0].update({"server": "0.0.0.0"}), "proxies[0].server"),
    case("proxies-server-lan", lambda c: c["proxies"][0].update({"server": "192.168.1.2"}), "proxies[0].server"),
    case("proxies-server-trailing-space", lambda c: c["proxies"][0].update({"server": "127.0.0.1 "}), "proxies[0].server"),
    case("proxies-server-none", lambda c: c["proxies"][0].update({"server": None}), "proxies[0].server"),
    case("proxies-udp-false", lambda c: c["proxies"][0].update({"udp": False}), "proxies[0].udp"),
    case("proxies-udp-1", lambda c: c["proxies"][0].update({"udp": 1}), "proxies[0].udp"),
    case("proxies-port-string", lambda c: c["proxies"][0].update({"port": "1082"}), "proxies[0].port"),
    case("proxies-port-true", lambda c: c["proxies"][0].update({"port": True}), "proxies[0].port"),
    case("proxies-port-zero", lambda c: c["proxies"][0].update({"port": 0}), "proxies[0].port"),
    case("proxies-port-too-big", lambda c: c["proxies"][0].update({"port": 65536}), "proxies[0].port"),
    case("proxies-port-float", lambda c: c["proxies"][0].update({"port": 1082.0}), "proxies[0].port"),
    case("proxies-extra-username", lambda c: c["proxies"][0].update({"username": "u"}), "username"),
    case("proxies-extra-password", lambda c: c["proxies"][0].update({"password": "p"}), "password"),
    case("proxies-missing-udp", lambda c: c["proxies"][0].pop("udp"), "'udp'"),
    case("proxies-name-changed", lambda c: c["proxies"][0].update({"name": "other"}), "proxies[0].name"),
    case("proxies-not-a-list", lambda c: c.update({"proxies": {}}), "proxies："),
    case("proxies-item-not-a-dict", lambda c: c.update({"proxies": ["upstream"]}), "proxies[0]"),
    # rules
    case("rules-final-is-match-upstream", lambda c: c["rules"].__setitem__(-1, "MATCH,upstream"), "最后一条"),
    case("rules-final-is-match-reject", lambda c: c["rules"].__setitem__(-1, "MATCH,REJECT"), "最后一条"),
    case("rules-final-is-not-a-string", lambda c: c["rules"].__setitem__(-1, None), "最后一条"),
    case("rules-final-missing", lambda c: c["rules"].pop(), "最后一条"),
    case("rules-only-match", lambda c: c.update({"rules": ["MATCH,DIRECT"]}), "至少要有两条"),
    case("rules-empty", lambda c: c.update({"rules": []}), "至少要有两条"),
    case("rules-domain-suffix", lambda c: c["rules"].insert(-1, "DOMAIN-SUFFIX,example.com,upstream"), "形状"),
    case("rules-geoip", lambda c: c["rules"].insert(-1, "GEOIP,CN,DIRECT"), "形状"),
    case("rules-rule-set", lambda c: c["rules"].insert(-1, "RULE-SET,anything,upstream"), "形状"),
    case("rules-process-path", lambda c: c["rules"].insert(-1, "PROCESS-PATH,C:\\a.exe,upstream"), "形状"),
    case("rules-sing-box-with-owner", lambda c: c["rules"].insert(-1, "PROCESS-NAME,sing-box.exe,upstream"), "sing-box",
         owner="sing-box.exe"),
    case("rules-owner-without-a-keyword", lambda c: c["rules"].insert(-1, "PROCESS-NAME,MyProxy.exe,upstream"), "属主",
         owner="myproxy"),
    case("rules-process-to-direct", lambda c: c["rules"].insert(-1, "PROCESS-NAME,nikke.exe,DIRECT"), "形状"),
    case("rules-no-resolve", lambda c: c["rules"].insert(-1, "PROCESS-NAME,a.exe,upstream,no-resolve"), "形状"),
    case("rules-lowercase-type", lambda c: c["rules"].insert(-1, "process-name,a.exe,upstream"), "形状"),
    case("rules-space-after-comma", lambda c: c["rules"].insert(-1, "PROCESS-NAME, a.exe,upstream"), "空格"),
    case("rules-trailing-newline", lambda c: c["rules"].insert(-1, "PROCESS-NAME,a.exe,upstream\n"), "形状"),
    case("rules-two-rules-in-one-string", lambda c: c["rules"].insert(-1, "PROCESS-NAME,a.exe,upstream\nMATCH,upstream"), "形状"),
    case("rules-wildcard-name", lambda c: c["rules"].insert(-1, "PROCESS-NAME,a*.exe,upstream"), "不允许的字符"),
    case("rules-path-name", lambda c: c["rules"].insert(-1, "PROCESS-NAME,C:\\x\\a.exe,upstream"), "路径分隔符"),
    case("rules-reserved-python", lambda c: c["rules"].insert(-1, "PROCESS-NAME,python.exe,upstream"), "保留名"),
    case("rules-reserved-svchost-any-case", lambda c: c["rules"].insert(-1, "PROCESS-NAME,SvcHost.EXE,upstream"), "保留名"),
    case("rules-cyrillic-name", lambda c: c["rules"].insert(-1, "PROCESS-NAME,\u0430.exe,upstream"), "不允许的字符"),
    case("rules-long-s-lookalike", lambda c: c["rules"].insert(-1, "PROCESS-NAME,\u017fvchost.exe,upstream"), "不允许的字符"),
    case("rules-katakana-middle-dot", lambda c: c["rules"].insert(-1, "PROCESS-NAME,a\u30fbb.exe,upstream"), "不允许的字符"),
    case("rules-reserved-python-ascii-case", lambda c: c["rules"].insert(-1, "PROCESS-NAME,Python.EXE,upstream"), "保留名"),
    case("rules-reserved-explorer", lambda c: c["rules"].insert(-1, "PROCESS-NAME,Explorer.exe,upstream"), "保留名"),
    case("rules-reserved-wm-elevate", lambda c: c["rules"].insert(-1, "PROCESS-NAME,wm-elevate.exe,upstream"), "保留名"),
    case("rules-symbols-only-name", lambda c: c["rules"].insert(-1, "PROCESS-NAME,-.exe,upstream"), "实字"),
    case("rules-name-without-exe", lambda c: c["rules"].insert(-1, "PROCESS-NAME,a,upstream"), ".exe"),
    case("rules-empty-name", lambda c: c["rules"].insert(-1, "PROCESS-NAME,,upstream"), "是空的"),
    case("rules-too-long-name", lambda c: c["rules"].insert(-1, "PROCESS-NAME," + "a" * 61 + ".exe,upstream"), "太长"),
    case("rules-match-in-the-middle", lambda c: c["rules"].insert(0, "MATCH,DIRECT"), "形状"),
    case("rules-not-a-list", lambda c: c.update({"rules": "PROCESS-NAME,a.exe,upstream"}), "rules："),
    case("rules-item-not-a-string", lambda c: c["rules"].insert(0, 5), "必须是字符串"),
    case("rules-item-none", lambda c: c["rules"].insert(0, None), "必须是字符串"),
    case("rules-item-nested-list", lambda c: c["rules"].insert(0, ["PROCESS-NAME", "a.exe", "upstream"]), "必须是字符串"),
    case("rules-seventeen-process-rules",
         lambda c: c.update({"rules": [f"PROCESS-NAME,g{i}.exe,upstream" for i in range(17)] + ["MATCH,DIRECT"]}), "最多 16"),
]


def test_there_are_plenty_of_tamper_cases():
    assert len(TAMPERS) >= 30


@pytest.mark.parametrize("mutate,owner,fragment", TAMPERS)
def test_validate_flags_every_single_tampering(good_config, mutate, owner, fragment):
    assert validate(good_config, upstream_owner=owner) == []  # 改之前是干净的
    before = copy.deepcopy(good_config)
    mutate(good_config)
    # 真的改了 (拿 JSON 文本比: 字典比较里 1 == True, 看不出 enable: True 被改成 1)
    assert json.dumps(good_config, sort_keys=True) != json.dumps(before, sort_keys=True)
    found = validate(good_config, upstream_owner=owner)
    assert found and all(type(line) is str for line in found)
    assert any(fragment in line for line in found), (fragment, found)  # 是被该拒的那条规则拒的
    assert not any(config_gen.UNEXPECTED in line for line in found), found  # 不是被兜底的"意外"拒的


EXTRA_TOP_KEYS = ["external-controller", "secret", "mixed-port", "proxy-groups", "proxy-providers", "rule-providers",
                  "hosts", "sniffer", "port", "socks-port", "redir-port", "tproxy-port", "authentication", "external-ui",
                  "external-controller-pipe", "interface-name", "routing-mark", "profile", "experimental", "listeners",
                  "tunnels", "geodata-mode", "geox-url", "ntp", "unified-delay", "bind-address", "skip-auth-prefixes",
                  "tcp-concurrent", "global-client-fingerprint", ""]


@pytest.mark.parametrize("key", [pytest.param(key, id=key or "empty-key") for key in EXTRA_TOP_KEYS])
def test_validate_refuses_any_extra_top_level_key(good_config, key):
    assert validate(good_config) == []
    good_config[key] = {"anything": 1}
    found = validate(good_config)
    assert found and any(repr(key) in line and "多了" in line for line in found), found
    assert len([line for line in found if "多了" in line]) == 1
    good_config.pop(key)
    assert validate(good_config) == []


def test_validate_refuses_a_non_string_key_too(good_config):
    for key in (1, None, ("mode",), 3.5, True):
        broken = dict(good_config)
        broken[key] = 1
        assert any("多了" in line for line in validate(broken)), key


WHOLE_INPUTS = [None, [], "x", "", 42, True, 3.5, (), set(), b"{}", object(), {}, [{}], ["mode"], float("nan")]


@pytest.mark.parametrize("value", [pytest.param(value, id=f"{index}-{type(value).__name__}")
                                   for index, value in enumerate(WHOLE_INPUTS)])
def test_validate_refuses_anything_that_is_not_a_config(value):
    found = validate(value)
    assert found and all(type(line) is str for line in found)
    assert not any(config_gen.UNEXPECTED in line for line in found), found


def test_validate_refuses_the_rendered_text_and_a_list_holding_the_config(good_config):
    assert any("字典" in line for line in validate(render(good_config)))  # 把文本当对象递进来
    assert any("字典" in line for line in validate([good_config]))
    assert any("字典" in line for line in validate(json.dumps(good_config)))


def test_validate_refuses_dict_and_str_subclasses(good_config):
    class Config(dict):
        pass

    class Text(str):
        pass

    assert validate(Config(good_config))
    tricked = copy.deepcopy(good_config)
    tricked["mode"] = Text("rule")
    assert validate(tricked)
    tricked = copy.deepcopy(good_config)
    tricked["rules"][-1] = Text("MATCH,DIRECT")
    assert validate(tricked)


def test_validate_accepts_the_harmless_variations(good_config):
    for level in ("debug", "info", "warning", "error"):
        config = copy.deepcopy(good_config)
        config["log-level"] = level
        assert validate(config) == [], level
    for stack in ("gvisor", "mips"):
        config = copy.deepcopy(good_config)
        config["tun"]["stack"] = stack
        assert validate(config) == [], stack
    for host, port in (("127.0.0.2", 1), ("127.255.255.255", 65535), ("127.0.0.1", 7890)):
        config = copy.deepcopy(good_config)
        config["proxies"][0].update({"server": host, "port": port})
        assert validate(config) == [], (host, port)
    config = copy.deepcopy(good_config)
    config["tun"]["route-exclude-address"].reverse()  # 排除网段不管顺序
    assert validate(config) == []
    assert validate(_reverse_keys(good_config)) == []  # 键不管顺序
    config = copy.deepcopy(good_config)
    config["rules"] = [f"PROCESS-NAME,g{i}.exe,upstream" for i in range(16)] + ["MATCH,DIRECT"]  # 正好 16 条
    assert validate(config) == []
    config = copy.deepcopy(good_config)
    config["rules"].insert(0, "PROCESS-NAME,绝区零.exe,upstream")
    assert validate(config) == []


def test_validate_with_the_owner_of_a_process_in_the_config_refuses_it(good_config):
    assert validate(good_config, upstream_owner=None) == []
    assert validate(good_config, upstream_owner="other.exe") == []
    for owner in ("nikke.exe", "NIKKE", "my game.EXE"):
        found = validate(good_config, upstream_owner=owner)
        assert found and any("属主" in line for line in found), (owner, found)
    for odd in ("", 5, ["x"]):
        assert validate(good_config, upstream_owner=odd), odd


def test_validate_does_not_touch_its_input(good_config):
    before = copy.deepcopy(good_config)
    validate(good_config, upstream_owner="nikke.exe")
    broken = copy.deepcopy(good_config)
    broken["rules"].insert(0, 5)
    broken_before = copy.deepcopy(broken)
    validate(broken)
    assert good_config == before and broken == broken_before


def test_build_checks_its_own_output_with_validate(monkeypatch):
    seen = []

    def pretend_it_fails(config, *, upstream_owner=None):
        seen.append((config, upstream_owner))
        return ["假装复查不通过"]

    monkeypatch.setattr(config_gen, "validate", pretend_it_fails)
    with pytest.raises(RecipeError) as err:
        build(make(), upstream_owner="x.exe")
    assert any("假装复查不通过" in line for line in err.value.problems)
    assert len(seen) == 1 and seen[0][1] == "x.exe" and seen[0][0]["rules"][-1] == "MATCH,DIRECT"


# ---------------------------------------------------------------- 乱塞: 复查对什么输入都不抛异常

def _garbage_values(deep=True):
    """\u4e71\u585e\u7528\u7684\u503c; deep=True \u65f6\u8fd8\u5e26\u4e0a\u5d4c\u5957 3000 \u5c42\u7684 (\u62f7\u8d1d\u4e0d\u52a8\u5b83\u4eec, \u6240\u4ee5\u53ea\u5728\u4e0d\u62f7\u8d1d\u7684\u5730\u65b9\u7528)."""
    values = [None, True, False, 0, 1, -1, 2 ** 70, 10 ** 5000, 1.5, float("nan"), float("inf"), "", "x", "x" * 10000,
              "PROCESS-NAME,a.exe,upstream", "MATCH,DIRECT", "\u202e", "\x00", [], {}, [None], ["x"], [[]], {"a": 1},
              {1: 2}, {None: None}, (), ("x",), b"bytes", object(), {"enable": False}]
    if deep:
        nested = []
        for _ in range(3000):
            nested = [nested]
        values += [nested, {"enable": nested}, [nested, nested]]
    return values


def _all_paths(obj, prefix=()):
    """除根以外的每个位置 (容器本身也算)."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield prefix + (key,)
            yield from _all_paths(value, prefix + (key,))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield prefix + (index,)
            yield from _all_paths(value, prefix + (index,))


def test_validate_never_raises_whatever_is_put_in_any_position(good_config):
    rng = random.Random(20261111)
    paths = list(_all_paths(good_config))
    garbage = _garbage_values()
    assert len(paths) >= 35 and len(garbage) > 30
    for path in paths:  # 每个位置至少换一次
        for value in rng.sample(garbage, 4):
            config = copy.deepcopy(good_config)
            _put(config, path, value)
            found = validate(config, upstream_owner=rng.choice([None, "nikke.exe", "x.exe"]))
            assert type(found) is list and all(type(line) is str for line in found)
            assert not any(config_gen.UNEXPECTED in line for line in found), (path, found)
    shallow = _garbage_values(deep=False)
    for _ in range(300):  # 再随机换两处
        config = copy.deepcopy(good_config)
        for _ in range(2):
            path = rng.choice(paths)
            try:
                _put(config, path, copy.deepcopy(rng.choice(shallow)))
            except (KeyError, IndexError, TypeError):
                pass  # 第一处换掉了容器, 第二处的位置已经不在了
        found = validate(config)
        assert type(found) is list and not any(config_gen.UNEXPECTED in line for line in found), found
    for value in garbage:  # 整个输入、属主都塞一遍
        validate(value)
        validate(good_config, upstream_owner=value)


def test_a_huge_input_does_not_make_the_checks_scan_all_of_it(monkeypatch, good_config):
    """名字、配方名、规则再长, 逐字符的检查也只看开头那一小段 (数查表函数 char_kind 的调用次数, 不拿时间说事)."""
    calls = []
    real = recipe_mod.char_kind
    monkeypatch.setattr(recipe_mod, "char_kind", lambda ch: calls.append(ch) or real(ch))
    big = "a" * 1_000_000
    huge_rule = good_config | {"rules": ["PROCESS-NAME," + big + ".exe,upstream", "MATCH,DIRECT"]}
    many_commas = good_config | {"rules": ["PROCESS-NAME" + "," * 1_000_000, "MATCH,DIRECT"]}
    outcomes = [problems(make((big + ".exe",))), problems(make((".exe".join([big, big]),))), problems(make(name=big)),
                problems(make((("-" * 1_000_000) + ".exe",))), validate(huge_rule), validate(many_commas)]
    assert all(outcomes)
    assert len(calls) < 2000, len(calls)


def test_validate_survives_deeply_nested_input():
    deep = []
    for _ in range(5000):
        deep = [deep]
    config = build(make())
    for position in (("rules", 0), ("tun", "dns-hijack"), ("tun", "route-exclude-address"), ("proxies", 0), ("dns", "enable")):
        broken = copy.deepcopy(config)
        _put(broken, position, deep)
        found = validate(broken)
        assert found and not any(config_gen.UNEXPECTED in line for line in found), (position, found)


# ---------------------------------------------------------------- 6. 纯函数、标准库、不读环境、文件格式

REAL_MODULES = ["recipe.py", "config_gen.py"]
BANNED_MODULES = {"subprocess", "socket", "os", "posix", "nt", "pathlib", "shutil", "tempfile", "ctypes", "urllib", "http",
                  "asyncio", "threading", "multiprocessing", "sys", "importlib", "pickle", "marshal", "io", "builtins",
                  "winreg", "_winapi", "signal"}
ENVIRONMENT_API = {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}  # os 里碰环境变量的东西
# 依赖 Unicode 数据的东西: 字符只认码点表、大小写只认 ASCII (接口契约第 7 节); 提权助手 (C++)、核心 (Go) 和这里三边的 Unicode 数据不是同一版
UNICODE_DATABASE_MODULES = {"unicodedata"}
UNICODE_DEPENDENT_ATTRIBUTES = {"casefold", "lower", "upper", "title", "swapcase", "capitalize", "isalpha", "isalnum", "isdigit",
                                "isdecimal", "isnumeric", "isspace", "isupper", "islower", "normalize", "category"}
BANNED_NAMES = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "os"} | ENVIRONMENT_API
BANNED_ATTRIBUTES = {"system", "popen", "Popen", "open", "read_text", "write_text", "read_bytes", "write_bytes", "unlink",
                     "mkdir", "remove", "rename"} | ENVIRONMENT_API


def purity_violations(source, filename):
    """扫一份源码: 返回它碰了文件、进程、网络、环境变量, 或者引了标准库以外的东西的地方 (空清单 = 干净).
    filename 只用来判断相对导入: 只有 config_gen.py 可以 from .recipe import ..."""
    found = []
    imported = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 同目录里的相对导入: 只有 config_gen 用 recipe 这一处
                if not (filename == "config_gen.py" and node.level == 1 and node.module == "recipe"):
                    found.append(f"第 {node.lineno} 行: 不许的相对导入 {ast.dump(node)}")
            else:
                imported.append(node.module or "")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            found.append(f"第 {node.lineno} 行: 用了 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRIBUTES:
            found.append(f"第 {node.lineno} 行: 用了 .{node.attr}")
        elif isinstance(node, ast.Attribute) and node.attr in UNICODE_DEPENDENT_ATTRIBUTES:
            found.append(f"第 {node.lineno} 行: 用了 .{node.attr} (依赖 Unicode 数据; 字符只认码点表, 大小写只认 ASCII)")
    for name in imported:
        top = name.split(".")[0]
        if top not in sys.stdlib_module_names:
            found.append(f"引了标准库以外的 {name}")
        elif top in BANNED_MODULES:
            found.append(f"引了 {name}: 不该碰文件、进程、网络、系统、环境变量")
        elif top in UNICODE_DATABASE_MODULES:
            found.append(f"引了 {name}: 字符只认码点表, 不查 Unicode 数据库")
    return found


@pytest.mark.parametrize("filename", REAL_MODULES)
def test_the_modules_are_pure_functions_over_the_standard_library(filename):
    source = (NET / filename).read_text(encoding="utf-8")
    assert any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(ast.parse(source))), "扫描没扫到 import"
    assert purity_violations(source, filename) == []


@pytest.mark.parametrize("filename", REAL_MODULES)
def test_the_modules_never_even_import_os_or_touch_the_environment(filename):
    """没有 os.environ、没有 os.getenv, 而且根本不 import os (包括 from os import ...、posix、nt)."""
    tree = ast.parse((NET / filename).read_text(encoding="utf-8"), filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {alias.name.split(".")[0] for alias in node.names} & {"os", "posix", "nt"}, ast.dump(node)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in {"os", "posix", "nt"}, ast.dump(node)
        elif isinstance(node, ast.Name):
            assert node.id != "os" and node.id not in ENVIRONMENT_API, (node.id, node.lineno)
        elif isinstance(node, ast.Attribute):
            assert node.attr not in ENVIRONMENT_API, (node.attr, node.lineno)


BAD_SOURCES = {
    "import-os": "import os\n",
    "import-os-path": "import os.path\n",
    "import-os-as": "import os as operating_system\n",
    "from-os-import-environ": "from os import environ\n",
    "from-os-import-getenv": "from os import getenv as g\n",
    "os-environ": "x = os.environ['CLASH_CONFIG_STRING']\n",
    "os-environ-get": "x = os.environ.get('CLASH_CONFIG_STRING')\n",
    "os-getenv": "x = os.getenv('CLASH_CONFIG_STRING')\n",
    "os-putenv": "os.putenv('A', 'b')\n",
    "bare-environ": "x = environ['A']\n",
    "bare-getenv": "x = getenv('A')\n",
    "open": "open('x')\n",
    "dunder-import": "__import__('os')\n",
    "eval": "eval('1')\n",
    "exec": "exec('x = 1')\n",
    "subprocess": "import subprocess\n",
    "from-subprocess": "from subprocess import run\n",
    "socket": "import socket\n",
    "pathlib": "from pathlib import Path\n",
    "read-text": "p.read_text()\n",
    "os-system": "os.system('x')\n",
    "ctypes": "import ctypes\n",
    "winreg": "import winreg\n",
    "sys": "import sys\n",
    "unicodedata": "import unicodedata\n",
    "from-unicodedata": "from unicodedata import category\n",
    "casefold": "x = name.casefold()\n",
    "lower": "x = name.lower()\n",
    "upper": "x = name.upper()\n",
    "isalpha": "x = ch.isalpha()\n",
    "isalnum": "x = ch.isalnum()\n",
    "normalize": "x = unicodedata.normalize('NFC', name)\n",
    "third-party": "import requests\n",
    "project-module": "from wm import privilege\n",
    "project-module-deep": "import wm.net.checkup\n",
    "relative-import-of-another-module": "from .snapshot import Snapshot\n",
    "bare-relative-import": "from . import snapshot\n",
}


@pytest.mark.parametrize("source", [pytest.param(source, id=ident) for ident, source in BAD_SOURCES.items()])
def test_the_purity_scan_catches_what_it_is_meant_to_catch(source):
    """扫描器自己也要有牙齿: 往里喂明摆着不合格的源码, 它得都报出来."""
    assert purity_violations(source, "recipe.py")
    assert purity_violations(source, "config_gen.py")


def test_the_purity_scan_lets_clean_code_through_and_allows_exactly_one_relative_import():
    clean = "from __future__ import annotations\nimport json\nfrom dataclasses import dataclass\nx = json.dumps({})\n"
    assert purity_violations(clean, "recipe.py") == [] and purity_violations(clean, "config_gen.py") == []
    assert purity_violations("from .recipe import check\n", "config_gen.py") == []
    assert purity_violations("from .recipe import check\n", "recipe.py")  # recipe.py 自己不许相对导入
    assert purity_violations("from .recipe import x\nfrom .other import y\n", "config_gen.py")


# 核心 mihomo 会拿这些环境变量当启动参数的默认值 (能换掉整份配置、能指定要跑的脚本); 生成和复查这一层要保证自己不读环境.
# 前九个是组长点名的, 值随便填; 下面一组是别的常见代理变量 (大小写两种写法).
ENVIRONMENT = {
    "CLASH_CONFIG_STRING": "mode: global\nallow-lan: true\nexternal-controller: 0.0.0.0:9090\n",
    "CLASH_CONFIG_FILE": "C:\\evil\\config.yaml",
    "CLASH_HOME_DIR": "C:\\evil",
    "CLASH_OVERRIDE_EXTERNAL_CONTROLLER": "0.0.0.0:9090",
    "CLASH_OVERRIDE_SECRET": "s3cret",
    "CLASH_POST_UP": "calc.exe",
    "CLASH_POST_DOWN": "calc.exe",
    "HTTP_PROXY": "http://evil.example:8080",
    "HTTPS_PROXY": "http://evil.example:8080",
}
OTHER_PROXY_VARIABLES = {"http_proxy": "http://evil.example:8080", "https_proxy": "http://evil.example:8080",
                         "ALL_PROXY": "socks5://evil.example:1080", "all_proxy": "socks5://evil.example:1080",
                         "NO_PROXY": "*", "no_proxy": "*"}


def everything(rcp):
    """所有公开函数都跑一遍 (生成、渲染、算指纹、复查, 好的和坏的都有); 文本统一编码成 UTF-8 字节, 好逐字节比."""
    config = build(rcp, upstream_owner="sing-box.exe")
    tampered = copy.deepcopy(config)
    tampered["allow-lan"] = True
    tampered["rules"].insert(0, "PROCESS-NAME,sing-box.exe,upstream")
    bad_recipe = make(("python.exe", "a,b.exe"), "localhost", 0, name="")
    return {
        "config": config,
        "render": render(config).encode("utf-8"),
        "pretty": pretty(config).encode("utf-8"),
        "fingerprint": fingerprint(config),
        "validate-good": validate(config, upstream_owner="sing-box.exe"),
        "validate-tampered": [line.encode("utf-8") for line in validate(tampered, upstream_owner="sing-box.exe")],
        "problems-good": problems(rcp, upstream_owner="sing-box.exe"),
        "problems-bad": [line.encode("utf-8") for line in problems(bad_recipe, upstream_owner="sing-box.exe")],
        "process-problems": [line.encode("utf-8")
                             for line in process_problems("a,b.exe", upstream_owner="x.exe", path="rules[0]")],
    }


def test_environment_variables_cannot_change_what_gets_generated_or_checked(monkeypatch):
    for name in [*ENVIRONMENT, *OTHER_PROXY_VARIABLES]:
        monkeypatch.delenv(name, raising=False)  # 先清干净, 免得真实环境里本来就有 (比如用户级的 http_proxy)
    rcp = make(("nikke.exe", "Wuthering Waves.exe", "原神.exe"), "127.0.0.2", 7897)
    clean = everything(rcp)
    assert clean["validate-good"] == [] and clean["validate-tampered"] and clean["problems-bad"]
    for name, value in {**ENVIRONMENT, **OTHER_PROXY_VARIABLES}.items():
        monkeypatch.setenv(name, value)
    for name, value in ENVIRONMENT.items():
        assert os.environ[name] == value  # 确实设上了
    dirty = everything(rcp)
    assert dirty == clean  # 配置、渲染出的字节、指纹、各种问题清单, 逐项一样
    assert dirty["render"] == clean["render"] and dirty["fingerprint"] == clean["fingerprint"]
    text = dirty["render"].decode("utf-8")
    assert not any(value in text for value in [*ENVIRONMENT.values(), *OTHER_PROXY_VARIABLES.values()])  # 配置里没有它们的值


@pytest.mark.parametrize("path", [NET / "recipe.py", NET / "config_gen.py", Path(__file__)], ids=lambda path: path.name)
def test_source_files_are_lf_utf8_without_bom_and_start_with_the_future_import(path):
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw and raw.endswith(b"\n")
    tree = ast.parse(raw.decode("utf-8"))
    body = tree.body[1:] if ast.get_docstring(tree) is not None else tree.body
    first = body[0]
    assert isinstance(first, ast.ImportFrom) and first.module == "__future__"
    assert [alias.name for alias in first.names] == ["annotations"]

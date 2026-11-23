"""网络插件第 0 步 (1/4): 只读体检的分析 (wm/net/checkup.py) 和命令行 (wm/net/__main__.py), 全部对着假快照 (tests/net_fakes.py).

四档照插件宿主的体检约定: 就绪 / 有风险 / 缺失 / 阻断, 外加不计档的提示. 样本: 这台电脑 11-11 的样子、别的 TUN 开着、干净的机器、
Clash Verge Rev 一切正常、v2rayN、遗留网卡又连上了、一堆长得像但不该报的网卡、系统代理指着没人听的端口、Tailscale 类网卡;
再加系统代理的几种写法、PAC、环境变量里的账号密码不许漏、管理员、没采全、进程名大小写、遗留网卡的各种残留.
没有任何一条测试会去碰这台电脑真实的网络状态."""
from __future__ import annotations

import ast
import asyncio
import copy
import ipaddress
import json
import time
from pathlib import Path

import pytest
from net_fakes import (NOISE_WORDS, Box, FakeSocks5, complement_prefixes, sample_1111, sample_clean, sample_dead_proxy,
                       sample_leftover_up, sample_noise_adapters, sample_other_tun, sample_tailscale, sample_v2rayn, sample_verge_ok)

from wm.net import __main__ as cli
from wm.net import checkup, config_gen
from wm.net.checkup import (BLOCK, LEVELS, MISSING, NOTE, READY, RISK, SHOW_ROUTES, Takeover, adapter_kind, analyze, parse_proxy_server,
                            takeover)
from wm.net.collect import env_proxy_from, system_proxy_from
from wm.net.config_gen import DEVICE
from wm.net.snapshot import Adapter, Route, Snapshot, split_host_port, strip_userinfo
from wm.net.trim import trim


@pytest.fixture(autouse=True)
def _no_real_collection(monkeypatch):
    """命令行的测试都是对着存好的快照或假的采集结果; 谁要是漏了换掉 collect, 这里一碰就失败, 不会真的起 PowerShell 去采."""
    monkeypatch.setattr(cli, "collect", lambda: pytest.fail("测试不该真的采集 (会起 PowerShell)"))


def codes(report):
    return [f.code for f in report.findings]


def find(report, code):
    return [f for f in report.findings if f.code == code]


def one(report, code):
    found = find(report, code)
    assert len(found) == 1, (code, codes(report))
    return found[0]


def client(report, name):
    found = [c for c in report.clients if c.name == name]
    assert len(found) == 1, (name, [c.name for c in report.clients])
    return found[0]


def test_the_level_words_are_the_hosts():
    assert (READY, RISK, MISSING, BLOCK, NOTE) == ("就绪", "有风险", "缺失", "阻断", "提示")
    assert LEVELS == (READY, RISK, MISSING, BLOCK)  # 由轻到重; 提示不在里面 (不计档)


# ---- 1. 这台电脑 11-11 的样子 -----------------------------------------------------------------------------------

def test_this_machine_on_1111():
    r = analyze(sample_1111())
    assert r.level == RISK
    assert not find(r, "other_tun_active") and not find(r, "no_upstream") and not find(r, "proxy_port_dead")
    # 系统代理: 指向本机 1082, sing-box 在听, 以服务方式运行
    sp = one(r, "system_proxy")
    assert sp.level == NOTE and "127.0.0.1:1082" in sp.title and "sing-box 在听" in sp.title and "以服务方式运行" in sp.title
    entry = sp.data["entries"][0]
    assert (entry["host"], entry["port"], entry["owner"], entry["as_service"]) == ("127.0.0.1", 1082, "sing-box", True)
    # 两个客户端: sing-box 在跑 (由 nssm 托管的服务), Clash Verge Rev 只有后台服务
    assert len(find(r, "client_found")) == 2 and all(f.level == NOTE for f in find(r, "client_found"))
    sb = client(r, "sing-box")
    assert (sb.family, sb.state, sb.as_service) == ("sing-box", "running", True)
    assert sb.service_hosts == ["SingBoxProxy"]
    assert [(p["address"], p["port"]) for p in sb.ports] == [("0.0.0.0", p) for p in (1082, 1083, 1084, 1085)]
    verge = client(r, "Clash Verge Rev")
    assert (verge.family, verge.state, verge.as_service) == ("mihomo", "service_only", True)
    assert "只有后台服务在" in verge.state_text and "界面和核心没开" in verge.state_text
    assert verge.processes == ["clash-verge-service"] and verge.services == ["clash_verge_service"] and verge.ports == []
    assert [c.name for c in r.clients] == ["sing-box", "Clash Verge Rev"]  # 在跑的排前面
    # sing-box 的四个端口合成一条, 有风险
    wide = one(r, "listener_all_interfaces")
    assert wide.level == RISK and wide.data["ports"] == [1082, 1083, 1084, 1085] and wide.data["process"] == "sing-box"
    assert wide.hint == "如果不是有意的，在那个客户端自己的配置里把监听地址改成 127.0.0.1。我们不改它的配置。"
    # vgate0 在用但不冲突; cfw-tap 是遗留
    va = one(r, "virtual_adapter")
    assert va.level == NOTE and "vgate0" in va.title and "172.30.243.250/32" in va.detail
    left = one(r, "leftover_adapter")
    assert left.level == RISK and left.title == "发现以前遗留的虚拟网卡 cfw-tap，是否清理？"
    for text in ("10.0.0.1/24", "0.0.0.0/0", "10.0.0.0", "接口跃点手动设成 1（IPv4、IPv6）", "网卡断开所以现在不生效；一旦它连上，所有流量会被引过去"):
        assert text in left.detail, text
    assert left.hint == "要清的话：设备管理器 → 网络适配器 → 找到它 → 卸载设备。体检只看不动。"
    assert left.data["adapter"] == "cfw-tap" and left.data["manual_addresses"] == ["10.0.0.1/24"]
    # 火绒、环境变量
    sec = one(r, "security_software")
    assert sec.level == NOTE and "火绒" in sec.title and sec.hint == "开 TUN 时它可能拦驱动加载或核心程序，到时候要你点放行。"
    env = one(r, "env_proxy_no_bypass")
    assert env.level == RISK and "http_proxy" in env.title and "no_proxy" in env.title and "127.0.0.1:7890" in env.detail
    # Teredo、WLAN、vmware-tray 都没被当成问题
    assert not any(f.data.get("adapter") in ("Teredo Tunneling Pseudo-Interface", "WLAN") for f in r.findings)
    assert not find(r, "ipv6_default_route") and not find(r, "server_elevated") and not find(r, "collect_partial")
    # 上游候选: 系统代理指的排第一, 其余是 sing-box 的另外三个端口 (同一个端口不重复)
    first = r.upstream_candidates[0]
    assert (first.host, first.port, first.listen, first.owner, first.client, first.source) == (
        "127.0.0.1", 1082, "0.0.0.0", "sing-box", "sing-box", "system_proxy")
    assert [c.port for c in r.upstream_candidates] == [1082, 1083, 1084, 1085]
    assert [c.source for c in r.upstream_candidates] == ["system_proxy", "client", "client", "client"]


# ---- 2. 别的 TUN 开着 --------------------------------------------------------------------------------------------

def test_another_tun_taking_the_default_route_is_a_blocker():
    r = analyze(sample_other_tun())
    assert r.level == BLOCK
    f = one(r, "other_tun_active")
    assert f.level == BLOCK and "Meta" in f.title and "Clash Verge Rev" in f.title
    assert "Meta" in f.detail and "Clash Verge Rev" in f.detail and "verge-mihomo" in f.detail
    assert "0.0.0.0/1" in f.detail and "128.0.0.0/1" in f.detail and "约 100% 的 IPv4 地址" in f.detail
    assert f.data == {"adapter": "Meta", "description": "Meta Tunnel", "in_adapter_list": True, "clients": ["Clash Verge Rev"],
                      "routes": ["0.0.0.0/1", "128.0.0.0/1"], "route_count": 2, "coverage": {"ipv4": 1.0, "ipv6": 0.0}}
    assert "TUN" in f.hint and "Clash Verge Rev" in f.hint
    assert not find(r, "virtual_adapter") and not find(r, "leftover_adapter")
    assert r.findings[0].code == "other_tun_active"  # 重的排在最前


@pytest.mark.parametrize("routes,expected", [
    (["0.0.0.0/0"], ["0.0.0.0/0"]),
    (["0.0.0.0/1", "128.0.0.0/1"], ["0.0.0.0/1", "128.0.0.0/1"]),
    (["0.0.0.0/1"], ["0.0.0.0/1"]),  # 只有半条也算: 它已经接走半个网
    (["128.0.0.0/1"], ["128.0.0.0/1"]),
    (["::/0"], ["::/0"]),
    (["::/1", "8000::/1"], ["::/1"]),  # IPv6 看对 2000::/3 的覆盖: ::/1 整个盖住它, 8000::/1 在它外面
    (["2000::/4", "3000::/4"], ["2000::/4", "3000::/4"]),  # 合起来正好是 2000::/3
    (["2000::/4"], ["2000::/4"]),  # 刚好一半
    (["0.0.0.0/1", "::/1", "8000::/1"], ["0.0.0.0/1", "::/1"]),  # IPv4 和 IPv6 各自够了, 都列
])
def test_what_counts_as_taking_over_the_default_route(routes, expected):
    b = Box()
    b.physical()
    b.adapter("wg0", "WireGuard Tunnel")
    for prefix in routes:
        b.route("wg0", prefix, if_metric=5)
    r = analyze(b.snap)
    assert one(r, "other_tun_active").data["routes"] == expected


@pytest.mark.parametrize("routes", [
    ["0.0.0.0/2"], ["10.0.0.0/8"], ["100.64.0.0/10"], ["10.0.0.0/8", "172.16.0.0/12"], ["224.0.0.0/4", "255.255.255.255/32"],
    ["10.0.0.0/8", "10.1.0.0/24", "10.1.1.0/24", "10.1.2.0/24", "10.1.3.0/24"],  # 一个大网段加几条 /24: 重叠的不重复算
    ["fd00::/8"], ["fe80::/64", "ff00::/8"], ["2000::/5"], ["::1/128"],
])
def test_a_small_slice_of_the_address_space_does_not_take_over(routes):
    b = Box()
    b.physical()
    b.adapter("wg0", "WireGuard Tunnel")
    for prefix in routes:
        b.route("wg0", prefix, if_metric=5)
    r = analyze(b.snap)
    assert not find(r, "other_tun_active")
    assert one(r, "virtual_adapter").level == NOTE


def _meta(routes, status="Up", persistent=False):
    """一块叫 Meta 的隧道类网卡 (Clash 系的 TUN) 和一块正常的物理网卡, Meta 上是这些路由."""
    b = Box()
    b.physical()
    b.adapter("Meta", "Meta Tunnel", status)
    b.interface("Meta", "IPv4", 5, state="Connected" if status == "Up" else "Disconnected")  # 自动跃点: 不算手动设的残留
    for prefix in routes:
        b.route("Meta", prefix, "198.18.0.2", 0, 5, persistent=persistent)
    return b.snap


def test_a_tun_with_excluded_ranges_takes_over_without_a_default_route_or_a_slash_1_pair():
    routes = complement_prefixes()  # 全空间减去排除的那几段 (config_gen.EXCLUDE_V4) 拆出来的一串路由, 用 ipaddress 算
    assert len(routes) > SHOW_ROUTES and "0.0.0.0/0" not in routes and not {"0.0.0.0/1", "128.0.0.0/1"} <= set(routes)
    r = analyze(_meta(routes))
    f = one(r, "other_tun_active")
    assert r.level == BLOCK and f.level == BLOCK and "Meta" in f.title
    assert 0.9 < f.data["coverage"]["ipv4"] < 0.95 and f.data["coverage"]["ipv6"] == 0
    biggest = sorted(routes, key=lambda p: (ipaddress.ip_network(p).prefixlen, int(ipaddress.ip_network(p).network_address)))[:SHOW_ROUTES]
    assert f.data["routes"] == biggest and f.data["route_count"] == len(routes)  # data 里只列最大的前几条, 不是上百条
    assert "约 93% 的 IPv4 地址" in f.detail and f"等共 {len(routes)} 条" in f.detail and len(f.detail) < 400
    assert not find(r, "virtual_adapter")


def test_the_same_complement_routes_on_a_disconnected_adapter_do_not_block():
    r = analyze(_meta(complement_prefixes(), status="Disconnected"))  # 断开的网卡上的路由也会出现在生效的列表里
    assert not find(r, "other_tun_active") and r.level != BLOCK
    assert not find(r, "leftover_adapter")  # 没有手动地址、手动跃点、持久路由


def test_persistent_complement_routes_on_a_disconnected_adapter_are_a_leftover_with_a_short_description():
    routes = complement_prefixes()
    r = analyze(_meta(routes, status="Disconnected", persistent=True))
    f = one(r, "leftover_adapter")
    assert f.title == "发现以前遗留的虚拟网卡 Meta，是否清理？"
    assert f"持久的路由 {len(routes)} 条" in f.detail and "约 93% 的 IPv4 地址" in f.detail and "所有流量会被引过去" in f.detail
    assert f.data["persistent_route_count"] == len(routes) and len(f.data["persistent_default_routes"]) == SHOW_ROUTES
    assert f.data["persistent_coverage"]["ipv4"] > 0.9 and len(f.detail) < 600
    small = analyze(_meta(["10.0.0.0/8", "172.16.0.0/12"], status="Disconnected", persistent=True))
    assert not find(small, "leftover_adapter")  # 持久路由只是几个私有网段: 不是默认路由


def test_a_tunnel_adapter_with_a_private_range_and_a_few_small_routes_is_only_a_note():
    b = Box()
    b.physical()
    b.adapter("wg0", "WireGuard Tunnel")
    for prefix in ("10.0.0.0/8", "10.1.1.0/24", "10.1.2.0/24", "10.2.0.0/24"):
        b.route("wg0", prefix, if_metric=5)
    r = analyze(b.snap)
    assert one(r, "virtual_adapter").level == NOTE and not find(r, "other_tun_active")


def test_takeover_counts_the_covered_share_without_counting_overlaps_twice():
    def share(*prefixes):
        return takeover([Route(prefix=p) for p in prefixes])
    t = share("0.0.0.0/1", "0.0.0.0/1", "0.0.0.0/2", "10.0.0.0/8")
    assert t.taken and t.v4 == 0.5 and t.v6 == 0 and len(t.routes) == 4  # 重叠的合并, 是 0.5 不是 0.75
    assert share("0.0.0.0/2", "64.0.0.0/2").v4 == 0.5 and share("0.0.0.0/2", "64.0.0.0/2").taken  # 相邻的两段加起来够一半
    assert not share("0.0.0.0/2", "0.0.0.0/3").taken and share("0.0.0.0/2").v4 == 0.25
    junk = share("not-a-prefix", "", "300.0.0.0/8", "0.0.0.0/0", "10.0.0.5/8")  # 看不懂的跳过, 带主机位的照收
    assert junk.taken and junk.v4 == 1.0
    v6 = share("2000::/4", "3000::/4", "10.0.0.0/8")
    assert v6.taken and v6.v6 == 1.0 and [r.prefix for r in v6.routes] == ["2000::/4", "3000::/4"]  # 接管了的是 IPv6, 只列它的
    assert not share().taken and not Takeover().taken


def test_hundreds_of_routes_are_merged_in_a_blink_not_compared_pairwise():
    routes = [Route(prefix=f"{a}.{b}.0.0/16") for a in range(1, 70) for b in range(64)]  # 4416 条, 覆盖约 6.7%
    started = time.perf_counter()
    t = takeover(routes)
    assert time.perf_counter() - started < 2 and not t.taken and 0.06 < t.v4 < 0.07
    started = time.perf_counter()
    assert takeover(routes + [Route(prefix=p) for p in complement_prefixes()]).taken
    assert time.perf_counter() - started < 2


def test_a_default_route_only_in_the_persistent_store_is_not_taking_over_now():
    b = Box()
    b.physical()
    b.adapter("wg0", "WireGuard Tunnel")  # Up, 默认路由只在持久存储里 (生效的列表里没有)
    b.route("wg0", "0.0.0.0/0", "10.0.0.0", 1, 1, persistent=True)
    r = analyze(b.snap)
    assert not find(r, "other_tun_active") and find(r, "virtual_adapter")


def test_which_client_opened_the_tun_is_only_named_when_it_can_be_matched():
    b = Box()
    b.physical()
    b.adapter("Meta", "Meta Tunnel")  # 名字是 Meta, 但没有 mihomo 系的客户端在跑
    b.route("Meta", "0.0.0.0/0", "198.18.0.2", 0, 0)
    r = analyze(b.snap)
    f = one(r, "other_tun_active")
    assert f.data["clients"] == [] and "没认出是哪个客户端" in f.detail and f.title == "别的虚拟网卡 Meta 已经接管了默认路由"
    b = Box()
    b.physical()
    sing = b.proc("sing-box.exe")
    b.adapter("singbox_tun", "sing-tun Tunnel")
    b.route("singbox_tun", "0.0.0.0/0", "172.19.0.2", 0, 0)
    b.listen(sing, 2080)
    f = one(analyze(b.snap), "other_tun_active")
    assert f.data["clients"] == ["sing-box"] and "sing-box" in f.title


# ---- 3. 干净的机器 ----------------------------------------------------------------------------------------------

def test_a_clean_machine_has_nothing_to_use_as_an_upstream():
    r = analyze(sample_clean())
    assert r.level == MISSING
    f = one(r, "no_upstream")
    assert f.level == MISSING and f.hint
    assert r.upstream_candidates == [] and r.clients == []
    assert one(r, "system_proxy").title == "系统代理没开"


# ---- 4. Clash Verge Rev 一切正常 --------------------------------------------------------------------------------

def test_clash_verge_rev_running_properly_is_ready():
    r = analyze(sample_verge_ok())
    assert r.level == READY
    assert all(f.level == NOTE for f in r.findings)
    verge = client(r, "Clash Verge Rev")
    assert (verge.state, verge.state_text, verge.family, verge.as_service) == ("running", "界面和核心都在跑", "mihomo", False)
    assert [p["port"] for p in verge.ports] == [7897] and verge.ports[0]["address"] == "127.0.0.1"
    assert [(c.host, c.port, c.client) for c in r.upstream_candidates] == [("127.0.0.1", 7897, "Clash Verge Rev")]
    sp = one(r, "system_proxy")
    assert "127.0.0.1:7897" in sp.title and "verge-mihomo 在听" in sp.title and "以服务方式运行" not in sp.title
    assert not find(r, "listener_all_interfaces") and not find(r, "no_upstream")


@pytest.mark.parametrize("procs,text", [
    (("clash-verge.exe",), "界面在跑，核心没开"),
    (("verge-mihomo.exe",), "核心在跑，界面没开"),
    (("clash-verge.exe", "verge-mihomo.exe"), "界面和核心都在跑"),
    (("sing-box.exe",), "在跑"),
])
def test_what_is_running_of_a_client_can_be_told(procs, text):
    b = Box()
    b.physical()
    for p in procs:
        b.proc(p)
    r = analyze(b.snap)
    assert r.clients[0].state == "running" and r.clients[0].state_text == text


# ---- 5. v2rayN ---------------------------------------------------------------------------------------------------

def test_v2rayn_with_xray_is_recognized_as_the_v2rayn_family():
    r = analyze(sample_v2rayn())
    c = client(r, "v2rayN")
    assert c.family == "v2rayN" and c.state == "running" and [p["port"] for p in c.ports] == [10808]
    assert r.upstream_candidates[0].port == 10808 and r.level == READY
    assert sorted(c.processes) == ["v2rayN", "xray"]


# ---- 6. 遗留网卡又连上了 ----------------------------------------------------------------------------------------

def test_a_leftover_adapter_that_is_up_with_the_default_route_is_a_blocker_not_a_leftover():
    r = analyze(sample_leftover_up())
    assert r.level == BLOCK
    f = one(r, "other_tun_active")
    assert f.data["adapter"] == "cfw-tap" and f.data["routes"] == ["0.0.0.0/0"]
    assert not find(r, "leftover_adapter")


# ---- 7. 长得像、但不该报的网卡 ----------------------------------------------------------------------------------

def test_look_alike_adapters_are_not_reported():
    r = analyze(sample_noise_adapters())
    assert not find(r, "other_tun_active") and not find(r, "leftover_adapter") and not find(r, "virtual_adapter")
    f = one(r, "ipv6_default_route")  # 物理网卡 (中文名) 上的 IPv6 默认路由: 提示; Teredo 的 ::/0 不算
    assert f.level == NOTE and f.data["adapters"] == ["以太网"] and "以太网" in f.title and not f.hint


@pytest.mark.parametrize("name,description,kind", [
    ("以太网", "Realtek PCIe GbE Family Controller", "physical"),
    ("WLAN", "Intel(R) Wi-Fi 6E AX211 160MHz", "physical"),
    ("vEthernet (WSL)", "Hyper-V Virtual Ethernet Adapter", "virtual"),
    ("vEthernet (Default Switch)", "Hyper-V Virtual Ethernet Adapter #2", "virtual"),
    ("VMware Network Adapter VMnet8", "VMware Virtual Ethernet Adapter for VMnet8", "virtual"),
    ("VirtualBox Host-Only Network", "VirtualBox Host-Only Ethernet Adapter", "virtual"),
    ("蓝牙网络连接", "Bluetooth Device (Personal Area Network)", "pseudo"),
    ("Teredo Tunneling Pseudo-Interface", "", "pseudo"),
    ("6to4 Adapter", "", "pseudo"),
    ("isatap.{3A1B}", "Microsoft ISATAP Adapter", "pseudo"),
    ("Microsoft IP-HTTPS Platform Interface", "", "pseudo"),
    ("本地连接* 8", "WAN Miniport (IP)", "pseudo"),
    ("本地连接* 5", "WAN Miniport (L2TP)", "pseudo"),
    ("本地连接* 1", "Microsoft Wi-Fi Direct Virtual Adapter", "pseudo"),
    ("以太网(内核调试器)", "Microsoft Kernel Debug Network Adapter", "pseudo"),
    ("Loopback Pseudo-Interface 1", "", "pseudo"),
    ("vgate0", "Rust Wintun Tunnel Tunnel", "tunnel"),
    ("cfw-tap", "TAP-Windows Adapter V9", "tunnel"),
    ("wg0", "WireGuard Tunnel", "tunnel"),
    ("OpenVPN", "OpenVPN Data Channel Offload", "tunnel"),
    ("sing-tun", "sing-tun Tunnel", "tunnel"),
    ("Tailscale", "Tailscale Tunnel", "tunnel"),
    ("Meta", "", "tunnel"),
    ("Mihomo", "Some Adapter", "tunnel"),
    ("meta", "", "tunnel"),
    ("VPN - 公司", "Fortinet SSL VPN Virtual Ethernet Adapter", "tunnel"),
    ("ExpressVPN", "ExpressVPN TAP Adapter", "tunnel"),
    ("本地连接 3", "TAP-Win32 Adapter V9", "tunnel"),
    ("以太网 4", "Cisco AnyConnect Secure Mobility Client Virtual Miniport Adapter for Windows x64", "tunnel"),
    ("以太网 5", "PANGP Virtual Ethernet Adapter Secure", "tunnel"),
    ("ZeroTier One [8056c2e21c000001]", "ZeroTier Virtual Port", "tunnel"),
    ("本地连接* 4", "WAN Miniport (IKEv2)", "pseudo"),  # 系统自带的 VPN 协议小端口, 还是伪网卡
])
def test_adapter_kinds(name, description, kind):
    assert adapter_kind(Adapter(name=name, description=description, status="Up", index=1)) == kind


@pytest.mark.parametrize("name,description", [
    ("VPN - 公司", "Fortinet SSL VPN Virtual Ethernet Adapter"),  # vpn
    ("本地连接 3", "TAP-Win32 Adapter V9"),  # tap-win32
    ("以太网 4", "Cisco AnyConnect Secure Mobility Client Virtual Miniport Adapter for Windows x64"),  # anyconnect
    ("以太网 5", "PANGP Virtual Ethernet Adapter Secure"),  # pangp
    ("ZeroTier One [8056c2e21c000001]", "ZeroTier Virtual Port"),  # zerotier
])
def test_a_vpn_style_adapter_with_the_default_route_is_another_tun_not_the_physical_network(name, description):
    b = Box()
    b.physical()
    b.adapter(name, description)
    b.route(name, "0.0.0.0/0", "10.8.0.1", 0, 5)
    r = analyze(b.snap)
    f = one(r, "other_tun_active")
    assert r.level == BLOCK and f.data["adapter"] == name and name in f.title and f.data["in_adapter_list"] is True


# ---- 我们自己的网卡 (config_gen.DEVICE) ---------------------------------------------------------------------------

def _with_a_running_client():
    b = Box()
    b.listen(b.proc("sing-box.exe"), 1080)
    b.system_proxy("127.0.0.1:1080")
    b.physical()
    return b


def test_the_name_of_our_own_adapter_comes_from_config_gen_not_from_a_second_literal():
    assert checkup.DEVICE is config_gen.DEVICE
    assert DEVICE not in Path(checkup.__file__).read_text(encoding="utf-8")  # 源码里没有再写一份字面量
    assert checkup.is_own_adapter(DEVICE) and checkup.is_own_adapter(DEVICE.lower()) and checkup.is_own_adapter(f" {DEVICE.upper()} ")
    assert not checkup.is_own_adapter(DEVICE + " 2") and not checkup.is_own_adapter("my-" + DEVICE) and not checkup.is_own_adapter("Meta")


@pytest.mark.parametrize("spelling", [lambda n: n, lambda n: n.lower(), lambda n: n.upper()])
def test_our_own_adapter_when_it_is_up_is_a_note_with_the_takeover_flag_and_never_another_tun(spelling):
    name = spelling(DEVICE)
    b = _with_a_running_client()
    b.own(name=name)
    for prefix in complement_prefixes():  # 我们开 TUN 时接管默认路由的样子 (排除网段拆出来的一串路由)
        b.route(name, prefix, "198.18.0.2", 0, 0)
    r = analyze(b.snap)
    f = one(r, "own_tun_active")
    assert f.level == NOTE and f.title == f"我们的虚拟网卡 {name} 开着" and f.data["adapter"] == name and f.data["takeover"] is True
    assert f.data["coverage"]["ipv4"] > 0.9 and f.data["route_count"] == len(complement_prefixes())
    for gone in ("other_tun_active", "virtual_adapter", "leftover_adapter", "own_tun_stale"):
        assert not find(r, gone), gone
    assert r.level == READY  # 它开着不影响总档


def test_our_own_adapter_up_without_the_default_route_says_so():
    b = _with_a_running_client()
    b.own()
    b.route(DEVICE, "198.18.0.0/30", if_metric=0)
    f = one(analyze(b.snap), "own_tun_active")
    assert f.data["takeover"] is False and "没有接管默认路由" in f.detail


def test_our_own_adapter_that_is_there_but_not_up_is_stale_and_not_a_leftover():
    b = _with_a_running_client()
    b.own("Disconnected")
    b.address(DEVICE, "198.18.0.1", 30, "manual")  # 要是别的断开的隧道网卡, 这些全是"遗留"
    b.route(DEVICE, "0.0.0.0/0", "198.18.0.2", 0, 0)
    b.route(DEVICE, "0.0.0.0/0", "198.18.0.2", 0, 0, persistent=True)
    r = analyze(b.snap)
    f = one(r, "own_tun_stale")
    assert f.level == RISK and f.title == "我们的虚拟网卡还在，但不是通的" and f.hint == "到网络页点一键恢复；不行就重启电脑。"
    assert f.data["adapter"] == DEVICE and f.data["status"] == "Disconnected"
    for gone in ("leftover_adapter", "other_tun_active", "own_tun_active", "virtual_adapter"):
        assert not find(r, gone), gone
    assert r.level == RISK and "→ 到网络页点一键恢复" in r.summary()


def test_our_own_adapter_whose_status_and_ip_interfaces_disagree_is_stale():
    b = _with_a_running_client()
    b.adapter(DEVICE, "Wintun Userspace Tunnel")  # 状态是 Up, 可它的 IP 接口都是断开的
    b.interface(DEVICE, "IPv4", 0, state="Disconnected")
    b.route(DEVICE, "0.0.0.0/0", "198.18.0.2", 0, 0)
    r = analyze(b.snap)
    assert find(r, "own_tun_stale") and not find(r, "own_tun_active") and not find(r, "other_tun_active")


def test_our_own_adapter_is_never_taken_for_a_physical_one_even_with_an_odd_description():
    b = _with_a_running_client()
    b.adapter(DEVICE, "Some Virtual Adapter")  # 描述里没有隧道类的关键字: 按名字认, 不按描述
    b.interface(DEVICE, "IPv6", 0)
    b.route(DEVICE, "::/0", "fe80::1", 0, 0)
    r = analyze(b.snap)
    assert find(r, "own_tun_active") and not find(r, "ipv6_default_route") and not find(r, "other_tun_active")


def test_an_adapter_with_a_similar_name_is_not_ours():
    b = _with_a_running_client()
    b.adapter(DEVICE + " 2", "Wintun Userspace Tunnel")
    b.route(DEVICE + " 2", "0.0.0.0/0", "198.18.0.2", 0, 0)
    r = analyze(b.snap)
    assert find(r, "other_tun_active") and not find(r, "own_tun_active")


# ---- 网卡列表里没有的接口 (系统自带的 VPN 连接) ------------------------------------------------------------------

def _ghost(name="公司VPN", routes=("0.0.0.0/1", "128.0.0.0/1"), state="Connected"):
    b = Box()
    b.physical()
    b.ghost(name)  # 只有接口表和路由表里有它, 网卡列表里没有
    if state:
        b.interface(name, "IPv4", 20, state=state)
    for prefix in routes:
        b.route(name, prefix, "10.9.0.1", 1, 20)
    return b.snap


def test_a_connected_interface_that_is_not_in_the_adapter_list_and_takes_over_is_another_tun():
    r = analyze(_ghost())
    f = one(r, "other_tun_active")
    assert r.level == BLOCK and f.level == BLOCK and "公司VPN" in f.title
    assert "多半是系统自带的 VPN 连接" in f.detail and f.hint and f.data["in_adapter_list"] is False
    assert f.data["adapter"] == "公司VPN" and f.data["clients"] == [] and f.data["coverage"]["ipv4"] == 1.0
    assert not find(r, "virtual_adapter") and not find(r, "leftover_adapter")
    assert one(analyze(_ghost(routes=complement_prefixes())), "other_tun_active").data["route_count"] == len(complement_prefixes())  # 排除网段的写法也算


def test_loopback_and_other_small_interfaces_missing_from_the_adapter_list_are_left_alone():
    b = Box()
    b.physical()
    for name in ("Loopback Pseudo-Interface 1", "lo-like"):  # 第二个的名字不像回环, 靠覆盖面判
        b.ghost(name)
        b.interface(name, "IPv4", 75)
        b.interface(name, "IPv6", 75)
        for prefix in ("127.0.0.0/8", "127.0.0.1/32", "127.255.255.255/32", "::1/128", "ff00::/8"):
            b.route(name, prefix, if_metric=75)
    r = analyze(b.snap)
    assert not find(r, "other_tun_active") and r.level != BLOCK


@pytest.mark.parametrize("make", [
    lambda: _ghost(state="Disconnected"),  # 接口是断开的
    lambda: _ghost(state=None),  # 接口表里没有它: 不知道通不通
    lambda: _ghost(routes=("10.9.0.0/16", "10.8.0.0/16")),  # 覆盖很小
    lambda: _ghost("vEthernet (External)"),  # 虚拟机的网卡
    lambda: _ghost(DEVICE),  # 我们自己的
    lambda: _ghost("Teredo Tunneling Pseudo-Interface"),
])
def test_an_interface_missing_from_the_adapter_list_is_only_blamed_when_it_is_connected_and_covers_half(make):
    assert not find(analyze(make()), "other_tun_active")


def test_a_listed_physical_adapter_with_a_huge_route_set_is_never_another_tun():
    b = Box()
    b.physical()
    for prefix in complement_prefixes():
        b.route("WLAN", prefix, "192.168.1.1", 5, 30)
    assert not find(analyze(b.snap), "other_tun_active")  # 物理网卡上的默认路由永远不算


# ---- 筛选后的快照给分析用 -----------------------------------------------------------------------------------------

def test_the_command_line_trims_a_saved_full_snapshot(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "is_elevated", lambda: False)
    path = tmp_path / "full.json"
    path.write_text(sample_1111().to_json(), encoding="utf-8")  # 带着全部无关的进程、服务、监听
    assert any(word in path.read_text(encoding="utf-8") for word in NOISE_WORDS)
    assert cli.main(["--from-file", str(path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    text = json.dumps(data["snapshot"], ensure_ascii=False).lower()
    for word in NOISE_WORDS:
        assert word.lower() not in text, word
    assert data["report"]["level"] == "有风险"
    assert cli.main(["--from-file", str(path)]) == 0  # 摘要和不筛的一样
    assert capsys.readouterr().out == analyze(sample_1111()).summary() + "\n"
    saved = tmp_path / "saved.json"
    assert cli.main(["--from-file", str(path), "--save", str(saved)]) == 0
    assert Snapshot.from_json(saved.read_text(encoding="utf-8")) == trim(sample_1111())
    assert not any(word.lower() in saved.read_text(encoding="utf-8").lower() for word in NOISE_WORDS)


# ---- 8. 系统代理开着但端口没人听 --------------------------------------------------------------------------------

def test_a_system_proxy_pointing_at_a_port_nobody_listens_on():
    r = analyze(sample_dead_proxy())
    assert r.level == MISSING
    f = one(r, "proxy_port_dead")
    assert f.level == MISSING and "127.0.0.1:7890" in f.title and f.data == {"host": "127.0.0.1", "port": 7890} and f.hint
    assert "本机没有程序在听" in one(r, "system_proxy").title
    assert r.upstream_candidates == [] and find(r, "no_upstream")  # 它不能当上游, 也没有别的上游


def test_a_remote_system_proxy_is_not_called_dead():
    b = Box()
    b.physical()
    b.system_proxy("proxy.corp.example:8080")
    r = analyze(b.snap)
    assert not find(r, "proxy_port_dead")
    sp = one(r, "system_proxy")
    assert "proxy.corp.example:8080" in sp.title and "不是本机的代理" in sp.title
    assert r.upstream_candidates == []  # 不是本机的端口, 当不了上游


def test_the_machines_own_lan_address_counts_as_local():
    b = Box()
    b.physical(ip="192.168.1.50")
    b.system_proxy("192.168.1.50:7890")
    assert one(analyze(b.snap), "proxy_port_dead").data["host"] == "192.168.1.50"
    pid = b.proc("sing-box.exe")
    b.listen(pid, 7890, address="192.168.1.50")
    r = analyze(b.snap)
    assert not find(r, "proxy_port_dead") and r.upstream_candidates[0].host == "192.168.1.50"


# ---- 9. ProxyServer 的写法和 PAC ---------------------------------------------------------------------------------

@pytest.mark.parametrize("server,expected", [
    ("127.0.0.1:7897", [("all", "127.0.0.1", 7897)]),
    ("http=127.0.0.1:7897;https=127.0.0.1:7897;socks=127.0.0.1:7898", [("http", "127.0.0.1", 7897), ("https", "127.0.0.1", 7897), ("socks", "127.0.0.1", 7898)]),
    ("socks=localhost:1080", [("socks", "localhost", 1080)]),
    ("[::1]:7890", [("all", "::1", 7890)]),
    ("http=[::1]:7890", [("http", "::1", 7890)]),
    ("https://127.0.0.1:8080", [("all", "127.0.0.1", 8080)]),
    ("http=http://127.0.0.1:8080 https=127.0.0.1:8081", [("http", "127.0.0.1", 8080), ("https", "127.0.0.1", 8081)]),
    ("proxy.corp.example", [("all", "proxy.corp.example", None)]),
    ("", []),
    (";;", []),
])
def test_proxy_server_spellings(server, expected):
    assert [(e.proto, e.host, e.port) for e in parse_proxy_server(server)] == expected


def test_a_per_protocol_proxy_server_is_read_and_merged():
    b = Box()
    b.physical()
    verge = b.proc("clash-verge.exe")
    core = b.proc("verge-mihomo.exe", ppid=verge)
    b.listen(core, 7897, 7898)
    b.system_proxy("http=127.0.0.1:7897;https=127.0.0.1:7897;socks=127.0.0.1:7898")
    r = analyze(b.snap)
    sp = one(r, "system_proxy")
    assert [(e["host"], e["port"], e["protos"]) for e in sp.data["entries"]] == [
        ("127.0.0.1", 7897, ["http", "https"]), ("127.0.0.1", 7898, ["socks"])]
    assert "127.0.0.1:7897" in sp.title and "127.0.0.1:7898" in sp.title and "HTTP、HTTPS → 127.0.0.1:7897" in sp.detail
    assert "SOCKS → 127.0.0.1:7898" in sp.detail
    assert [c.port for c in r.upstream_candidates] == [7897, 7898] and r.upstream_candidates[0].source == "system_proxy"
    assert r.level == READY


def test_localhost_in_the_system_proxy_is_local_and_listens_on_loopback_or_wildcard():
    b = Box()
    b.physical()
    sing = b.proc("sing-box.exe")
    b.listen(sing, 1080, address="::1")
    b.system_proxy("socks=localhost:1080")
    r = analyze(b.snap)
    assert not find(r, "proxy_port_dead")
    assert (r.upstream_candidates[0].host, r.upstream_candidates[0].port) == ("127.0.0.1", 1080)


def test_pac_is_mentioned():
    b = Box()
    b.physical()
    b.system_proxy(enabled=False, pac=True)  # 只有 PAC
    sp = one(analyze(b.snap), "system_proxy")
    assert sp.title == "系统代理没开，但设了 PAC 脚本" and "PAC" in sp.detail and sp.data["pac"] is True
    b = Box()
    b.physical()
    verge = b.proc("verge-mihomo.exe")
    b.listen(verge, 7897)
    b.system_proxy("127.0.0.1:7897", pac=True)  # 代理和 PAC 都开着
    sp = one(analyze(b.snap), "system_proxy")
    assert "127.0.0.1:7897" in sp.title and "PAC" in sp.title


def test_a_system_proxy_that_is_on_without_an_address():
    b = Box()
    b.physical()
    b.system_proxy("", enabled=True)
    assert one(analyze(b.snap), "system_proxy").title == "系统代理开着，但没有写代理地址"


# ---- 10. 账号密码不许漏 -----------------------------------------------------------------------------------------

def test_credentials_in_proxy_addresses_never_show_up_anywhere():
    secret = "s3cr3t-P@ss/w0rd"
    b = Box()
    b.physical()
    sing = b.proc("sing-box.exe")
    b.listen(sing, 7890, 7891)
    b.snap.env_proxy = env_proxy_from({
        "HTTP_PROXY": f"http://alice:{secret}@127.0.0.1:7890/",
        "https_proxy": f"alice:{secret}@127.0.0.1:7891",
        "All_Proxy": "socks5://alice:12345678/x@127.0.0.1:7890",  # 密码是纯数字、里面还带 /: 不能被当成端口漏出来 (12345678 是密码)
    })
    b.snap.system_proxy = system_proxy_from({"ProxyEnable": 1, "ProxyServer": f"http=bob:{secret}@127.0.0.1:7890;socks=bob:{secret}@127.0.0.1:7891"})
    snap = b.snap
    assert (snap.env_proxy.http_proxy.host, snap.env_proxy.http_proxy.port) == ("127.0.0.1", 7890)
    assert (snap.env_proxy.https_proxy.host, snap.env_proxy.https_proxy.port) == ("127.0.0.1", 7891)
    assert (snap.env_proxy.all_proxy.host, snap.env_proxy.all_proxy.port) == ("127.0.0.1", 7890)
    report = analyze(snap)
    assert find(report, "env_proxy_no_bypass")
    for text in (snap.to_json(), snap.to_json(indent=None), report.to_json(), report.summary(), repr(snap), repr(report)):
        for leaked in (secret, "s3cr3t", "alice", "bob", "12345678", "w0rd"):
            assert leaked not in text, leaked
    assert one(report, "system_proxy").data["entries"][0]["host"] == "127.0.0.1"


def test_a_foreign_snapshot_file_with_credentials_is_cleaned_when_loaded():
    data = sample_1111().to_dict()
    data["env_proxy"]["http_proxy"] = {"present": True, "host": "alice:hunter2@127.0.0.1", "port": None}
    data["system_proxy"]["server"] = "http=alice:hunter2@127.0.0.1:1082"
    snap = Snapshot.from_dict(data)
    assert (snap.env_proxy.http_proxy.host, snap.env_proxy.http_proxy.port) == ("127.0.0.1", None)
    assert snap.system_proxy.server == "http=127.0.0.1:1082"
    assert "hunter2" not in snap.to_json() and "hunter2" not in analyze(snap).to_json()


@pytest.mark.parametrize("text,expected", [
    ("http://127.0.0.1:7890", ("127.0.0.1", 7890)),
    ("http://user:pw@127.0.0.1:7890/path?x=1#f", ("127.0.0.1", 7890)),
    ("user:pw@proxy.example:3128", ("proxy.example", 3128)),
    ("socks5h://[::1]:1080", ("::1", 1080)),
    ("[fe80::1]:99", ("fe80::1", 99)),
    ("::1", ("::1", None)),
    ("localhost", ("localhost", None)),
    ("localhost:", ("localhost", None)),
    ("host:notaport", ("", None)),
    ("host:70000", ("host", None)),
    ("   ", ("", None)),
    ("http://a:12345/x@h:1", ("h", 1)),  # 密码是纯数字、里面还带 / 也不能被当成端口
    ("[::1", ("", None)),
])
def test_split_host_port(text, expected):
    assert split_host_port(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("127.0.0.1:1082", "127.0.0.1:1082"),
    ("http=u:p@127.0.0.1:1;socks=u:p@127.0.0.1:2", "http=127.0.0.1:1;socks=127.0.0.1:2"),
    ("https://u:p@h:1", "https://h:1"),
    ("u:p@h:1", "h:1"),
    ("http=u:a@b@h:1", "http=h:1"),  # 密码里有 @: 以最后一个 @ 为界
])
def test_strip_userinfo(text, expected):
    assert strip_userinfo(text) == expected


# ---- 11. 管理员 --------------------------------------------------------------------------------------------------

def test_a_server_running_as_administrator_is_a_risk():
    snap = sample_verge_ok()
    assert analyze(snap).level == READY
    r = analyze(snap, elevated=True)
    f = one(r, "server_elevated")
    assert r.level == RISK and f.level == RISK and f.title and f.hint
    for value in (False, None):
        assert not find(analyze(snap, elevated=value), "server_elevated")


# ---- 12. 没采全 --------------------------------------------------------------------------------------------------

def test_a_partial_collection_is_reported_and_the_rest_is_still_analyzed():
    snap = sample_verge_ok()
    snap.errors = {"routes": "超时（30 秒）没采完", "routes_persistent": "PowerShell 没有给出这一项"}
    r = analyze(snap)
    f = one(r, "collect_partial")
    assert f.level == RISK and r.level == RISK and "路由" in f.title and "持久路由" in f.title and "超时" in f.detail
    assert set(f.data["errors"]) == {"routes", "routes_persistent"}
    assert find(r, "client_found") and find(r, "system_proxy")  # 其余照常
    assert not find(r, "other_tun_active") and not find(r, "virtual_adapter")  # 路由没采到, 网卡的结论不下


def test_nothing_is_claimed_missing_when_the_data_for_it_was_not_collected():
    snap = sample_dead_proxy()
    snap.errors = {"listeners": "超时（30 秒）没采完"}
    r = analyze(snap)
    assert find(r, "collect_partial")
    assert not find(r, "proxy_port_dead") and not find(r, "no_upstream")  # 没采到谁在听, 不能说"没人听"
    assert r.level == RISK
    snap = sample_clean()
    snap.errors = {"system_proxy": "读不了注册表"}
    assert not find(analyze(snap), "no_upstream") and not find(analyze(snap), "system_proxy")
    snap = sample_1111()
    snap.errors = {"env_proxy": "读不了注册表"}
    assert not find(analyze(snap), "env_proxy_no_bypass")


# ---- 13. 进程名: 大小写、带不带 .exe -----------------------------------------------------------------------------

# 任务里列的名字 (进程名或服务名): (名字, 客户端, 家族). 帮手进程单独列在 HELPERS 里 (只有它在 = 只有后台服务在)
SPEC_NAMES = [
    ("clash-verge", "Clash Verge Rev", "mihomo"), ("clash-verge-service", "Clash Verge Rev", "mihomo"),
    ("verge-mihomo", "Clash Verge Rev", "mihomo"), ("verge-mihomo-alpha", "Clash Verge Rev", "mihomo"),
    ("mihomo", "mihomo", "mihomo"), ("mihomo-windows-amd64-compatible-v1.19.34", "mihomo", "mihomo"),
    ("clash-meta", "Clash Meta", "mihomo"),
    ("FlClash", "FlClash", "mihomo"), ("FlClashCore", "FlClash", "mihomo"), ("FlClashHelperService", "FlClash", "mihomo"),
    ("Clash Party", "Clash Party", "mihomo"), ("mihomo-party", "Clash Party", "mihomo"),
    ("sparkle", "sparkle", "mihomo"), ("clash-nyanpasu", "Clash Nyanpasu", "mihomo"),
    ("Clash for Windows", "Clash for Windows", "mihomo"), ("clash-win64", "Clash for Windows", "mihomo"),
    ("sing-box", "sing-box", "sing-box"), ("GUI.for.SingBox", "GUI.for.SingBox", "sing-box"),
    ("Hiddify", "Hiddify", "sing-box"), ("HiddifyCli", "Hiddify", "sing-box"),
    ("nekobox", "NekoBox", "sing-box"), ("nekobox_core", "NekoBox", "sing-box"), ("nekoray", "NekoRay", "sing-box"),
    ("Karing", "Karing", "sing-box"), ("Throne", "Throne", "sing-box"),
    ("v2rayN", "v2rayN", "v2rayN"), ("xray", "v2rayN", "v2rayN"), ("v2ray", "v2rayN", "v2rayN"),
]
HELPERS = {"clash-verge-service", "FlClashHelperService"}


@pytest.mark.parametrize("name,expected,family", SPEC_NAMES)
@pytest.mark.parametrize("spelling", [lambda n: n, lambda n: n + ".exe", lambda n: n.upper() + ".EXE", lambda n: n.lower(), lambda n: n.title() + ".Exe"])
def test_every_listed_name_is_recognized_in_any_case_with_or_without_exe(name, expected, family, spelling):
    b = Box()
    b.physical()
    b.proc(spelling(name))
    r = analyze(b.snap)
    c = client(r, expected)
    assert c.family == family
    assert c.state == ("service_only" if name in HELPERS else "running")
    assert len(r.clients) == 1


def test_a_service_name_alone_is_recognized_only_while_it_is_running():
    for service, expected in (("clash_verge_service", "Clash Verge Rev"), ("CLASH_VERGE_SERVICE", "Clash Verge Rev"),
                              ("FlClashHelperService", "FlClash")):
        b = Box()
        b.physical()
        b.service(service, 4321)
        c = client(analyze(b.snap), expected)
        assert c.state == "service_only" and c.as_service and c.services == [service]
        b = Box()
        b.physical()
        b.service(service, 0, "Stopped")  # 装着但没在跑: 不算"认出"
        assert analyze(b.snap).clients == []


@pytest.mark.parametrize("name", ["claude.exe", "singbox-helper.exe", "mihomo-tool.exe", "xray-helper.exe", "clash.exe", "verge.exe",
                                  "v2rayNG.exe", "notepad.exe", "sing-box-extended.exe", "mihomo-windowsfoo.exe"])
def test_look_alike_process_names_are_not_clients(name):
    b = Box()
    b.physical()
    b.proc(name)
    assert analyze(b.snap).clients == []


def test_the_client_table_has_no_duplicate_names():
    seen = {}
    for d in checkup.CLIENTS:
        for n in (*d.ui, *d.core, *d.helper):
            key = checkup.norm_name(n)
            assert key not in seen, (n, d.id, seen.get(key))
            seen[key] = d.id
    assert {d.family for d in checkup.CLIENTS} == {"mihomo", "sing-box", "v2rayN"}


@pytest.mark.parametrize("name,label", [("HipsTray.exe", "火绒"), ("hipsdaemon", "火绒"), ("HIPSMAIN.EXE", "火绒"), ("360Tray.exe", "360"),
                                        ("ZhuDongFangYu.exe", "360"), ("kxetray.exe", "金山毒霸"), ("KXESCORE", "金山毒霸"),
                                        ("QQPCTray.exe", "腾讯电脑管家"), ("QQPCRTP.exe", "腾讯电脑管家")])
def test_security_software_names(name, label):
    b = Box()
    b.physical()
    b.proc(name)
    f = one(analyze(b.snap), "security_software")
    assert f.level == NOTE and label in f.title and f.data["products"][label]


def test_several_security_products_make_one_finding():
    b = Box()
    b.physical()
    for n in ("HipsTray.exe", "HipsDaemon.exe", "360Tray.exe", "HipsTray.exe"):
        b.proc(n)
    f = one(analyze(b.snap), "security_software")
    assert f.data["products"] == {"火绒": ["HipsTray", "HipsDaemon"], "360": ["360Tray"]}
    assert not find(analyze(Box().snap), "security_software")


# ---- 14. Tailscale 类网卡 ---------------------------------------------------------------------------------------

def test_a_tunnel_adapter_that_does_not_take_the_default_route_is_only_a_note():
    r = analyze(sample_tailscale())
    f = one(r, "virtual_adapter")
    assert f.level == NOTE and "Tailscale" in f.title and "100.101.102.103/32" in f.detail
    assert not find(r, "other_tun_active") and not find(r, "leftover_adapter")
    assert r.level == READY  # 只是提示, 不影响总档


# ---- 遗留网卡的各种残留 -----------------------------------------------------------------------------------------

def _tap(status="Disconnected", **leftovers):
    b = Box()
    b.physical()
    b.listen(b.proc("sing-box.exe"), 1080)  # 有上游、系统代理也指着它: 除了遗留网卡没有别的问题
    b.system_proxy("127.0.0.1:1080")
    b.adapter("cfw-tap", "TAP-Windows Adapter V9", status)
    if leftovers.get("manual_address"):
        b.address("cfw-tap", "10.0.0.1", 24, "manual")
    b.address("cfw-tap", "169.254.87.180", 16, "system")  # 系统自己配的不算
    b.interface("cfw-tap", "IPv4", 1 if leftovers.get("manual_metric") else 25, auto=not leftovers.get("manual_metric"), state=status)
    if leftovers.get("persistent_default"):
        b.route("cfw-tap", "0.0.0.0/0", "10.0.0.0", 1, 1, persistent=True)
    b.route("cfw-tap", "fe80::/64", "::", 256, 25)
    return b.snap


@pytest.mark.parametrize("kind,expected_text", [
    ("manual_address", "手动设的地址 10.0.0.1/24"),
    ("manual_metric", "接口跃点手动设成 1（IPv4）"),
    ("persistent_default", "持久的默认路由 0.0.0.0/0 经 10.0.0.0"),
])
def test_any_one_leftover_is_enough(kind, expected_text):
    r = analyze(_tap(**{kind: True}))
    f = one(r, "leftover_adapter")
    assert f.level == RISK and r.level == RISK and expected_text in f.detail
    assert f.title == "发现以前遗留的虚拟网卡 cfw-tap，是否清理？"
    assert ("所有流量会被引过去" in f.detail) == (kind == "persistent_default")  # 只有留着默认路由才会引走所有流量


def test_nothing_left_means_nothing_to_clean():
    assert not find(analyze(_tap()), "leftover_adapter")  # 断开了, 只有系统自己配的地址和自动跃点


@pytest.mark.parametrize("status", ["Disconnected", "Not Present", "Disabled", "Down"])
def test_every_not_up_status_can_hold_a_leftover(status):
    assert find(analyze(_tap(status, persistent_default=True)), "leftover_adapter")


def test_a_leftover_on_a_look_alike_non_tunnel_adapter_is_ignored():
    b = Box()
    b.physical()
    b.adapter("VMware Network Adapter VMnet1", "VMware Virtual Ethernet Adapter for VMnet1", "Disconnected")
    b.address("VMware Network Adapter VMnet1", "192.168.242.1", 24, "manual")
    b.interface("VMware Network Adapter VMnet1", "IPv4", 35, auto=False, state="Disconnected")
    b.route("VMware Network Adapter VMnet1", "0.0.0.0/0", "192.168.242.2", 1, 35, persistent=True)
    assert not find(analyze(b.snap), "leftover_adapter")


def test_the_routes_of_a_disconnected_adapter_never_count_as_taking_over():
    b = Box()
    b.physical()
    b.adapter("cfw-tap", "TAP-Windows Adapter V9", "Disconnected")
    b.route("cfw-tap", "0.0.0.0/0", "10.0.0.0", 1, 1)  # 断开的网卡上的路由也会出现在生效的列表里
    r = analyze(b.snap)
    assert not find(r, "other_tun_active") and r.level != BLOCK


def test_an_adapter_counts_as_up_only_when_its_status_and_its_ip_interfaces_agree():
    def build(*interfaces):
        b = Box()
        b.physical()
        b.adapter("wg0", "WireGuard Tunnel")  # 状态是 Up
        for family, state in interfaces:
            b.interface("wg0", family, 5, state=state)
        b.route("wg0", "0.0.0.0/0", "10.0.0.1", 0, 5)
        return analyze(b.snap)
    assert find(build(("IPv4", "Connected")), "other_tun_active")
    r = build(("IPv4", "Disconnected"), ("IPv6", "Disconnected"))  # 两个信号对不上: 当它没通, 不报阻断
    assert not find(r, "other_tun_active") and not find(r, "virtual_adapter")
    assert find(build(("IPv4", "Disconnected"), ("IPv6", "Connected")), "other_tun_active")  # 有一个 Connected 就算通
    assert find(build(), "other_tun_active")  # 没有接口记录: 只看网卡状态


def test_two_leftover_adapters_get_one_finding_each():
    b = Box()
    b.physical()
    for name in ("tap-a", "tap-b"):
        b.adapter(name, "TAP-Windows Adapter V9", "Disconnected")
        b.address(name, "10.1.1.1", 24, "manual")
    assert sorted(f.data["adapter"] for f in find(analyze(b.snap), "leftover_adapter")) == ["tap-a", "tap-b"]


# ---- 监听、服务方式、上游候选 -----------------------------------------------------------------------------------

def test_wildcard_listeners_of_clients_are_merged_per_process():
    b = Box()
    b.physical()
    a = b.proc("sing-box.exe")
    c = b.proc("xray.exe")
    b.listen(a, 1082, 1083, address="0.0.0.0")
    b.listen(a, 1082, address="::")  # 同一个端口 IPv4 和 IPv6 各一行
    b.listen(a, 9090, address="127.0.0.1")
    b.listen(c, 10808, address="::")
    b.listen(4, 445, address="0.0.0.0")  # 不是代理客户端
    found = find(analyze(b.snap), "listener_all_interfaces")
    assert len(found) == 2
    by = {f.data["process"]: f.data for f in found}
    assert by["sing-box"]["ports"] == [1082, 1083] and by["sing-box"]["addresses"] == ["0.0.0.0", "::"]
    assert by["xray"]["ports"] == [10808] and by["xray"]["client"] == "v2rayN"


def test_listening_on_loopback_only_is_fine():
    assert not find(analyze(sample_verge_ok()), "listener_all_interfaces")


def test_running_as_a_service_means_it_or_its_parent_is_a_service_process():
    def build(parent_name, with_service=True, state="Running"):
        b = Box()
        b.physical()
        parent = b.proc(parent_name)
        b.proc("sing-box.exe", ppid=parent)
        if with_service:
            b.service("Whatever", parent, state)
        return client(analyze(b.snap), "sing-box")
    assert build("nssm.exe").as_service is True
    assert build("nssm.exe").service_hosts == ["Whatever"]
    assert build("nssm.exe", with_service=False).as_service is False  # 父进程不是任何服务的进程
    assert build("nssm.exe", state="Stopped").as_service is False
    assert build("svchost.exe").as_service is False  # 共享的服务宿主 (计划任务拉起的东西也挂在它下面) 不算
    b = Box()
    b.physical()
    me = b.proc("sing-box.exe")
    b.service("sing-box", me)  # 自己就是服务的进程
    assert client(analyze(b.snap), "sing-box").as_service is True


def test_a_helper_service_beside_a_normally_started_client_is_not_a_service_run():
    b = Box()
    b.physical()
    gui = b.proc("FlClash.exe")
    b.proc("FlClashCore.exe", ppid=gui)
    helper = b.proc("FlClashHelperService.exe")
    b.service("FlClashHelperService", helper)  # 后台帮手装着在跑, 但核心是界面拉起来的
    c = client(analyze(b.snap), "FlClash")
    assert c.state == "running" and c.as_service is False and c.service_hosts == [] and c.services == ["FlClashHelperService"]
    b = Box()
    b.physical()
    helper = b.proc("clash-verge-service.exe")
    b.service("clash_verge_service", helper)
    b.proc("verge-mihomo.exe", ppid=helper)  # 服务模式: 核心是服务拉起来的
    b.proc("clash-verge.exe")
    c = client(analyze(b.snap), "Clash Verge Rev")
    assert c.state == "running" and c.as_service is True and c.service_hosts == ["clash_verge_service"]
    assert "以服务方式运行" in one(analyze(b.snap), "client_found").title


def test_upstream_candidates_order_and_what_they_say():
    b = Box()
    b.physical()
    sing = b.proc("sing-box.exe")
    unknown = b.proc("my-proxy.exe")
    b.listen(sing, 1083, 1082, address="0.0.0.0")
    b.listen(sing, 1082, address="::")
    b.listen(sing, 9090, address="::1")
    b.listen(unknown, 8888, address="127.0.0.1")
    b.system_proxy("127.0.0.1:8888")  # 系统代理指的是一个认不出的程序: 照样排第一
    r = analyze(b.snap)
    assert [(c.host, c.port, c.owner, c.client, c.source) for c in r.upstream_candidates] == [
        ("127.0.0.1", 8888, "my-proxy", "", "system_proxy"),
        ("127.0.0.1", 1082, "sing-box", "sing-box", "client"),
        ("127.0.0.1", 1083, "sing-box", "sing-box", "client"),
        ("::1", 9090, "sing-box", "sing-box", "client"),
    ]
    assert not find(r, "no_upstream") and not find(r, "proxy_port_dead")


def test_a_running_client_is_an_upstream_even_when_the_system_proxy_is_off():
    b = Box()
    b.physical()
    xray = b.proc("xray.exe")
    b.listen(xray, 10808)
    b.system_proxy(enabled=False)
    r = analyze(b.snap)
    assert [c.port for c in r.upstream_candidates] == [10808] and not find(r, "no_upstream") and r.level == READY


# ---- 总档、摘要、JSON、纯函数 ------------------------------------------------------------------------------------

def test_the_overall_level_is_the_heaviest_finding_and_notes_do_not_count():
    assert analyze(sample_verge_ok()).level == READY
    assert analyze(sample_1111()).level == RISK
    assert analyze(sample_dead_proxy()).level == MISSING
    assert analyze(sample_other_tun()).level == BLOCK
    # 缺失比有风险重, 阻断比缺失重
    snap = sample_clean()
    snap.env_proxy = env_proxy_from({"http_proxy": "http://127.0.0.1:7890"})
    r = analyze(snap)
    assert {f.level for f in r.findings} >= {RISK, MISSING} and r.level == MISSING
    assert analyze(Snapshot()).level == MISSING  # 什么都没采到 (空快照): 没有上游


def test_the_summary_says_the_level_first_then_one_line_per_finding_heaviest_first():
    r = analyze(sample_1111())
    lines = r.summary().split("\n")
    assert lines[0] == "网络体检：有风险" and len(lines) == 1 + len(r.findings)
    assert all(line.startswith("- [") for line in lines[1:])
    order = [line[3:line.index("]")] for line in lines[1:]]
    ranks = [{BLOCK: 0, MISSING: 1, RISK: 2, NOTE: 3}[x] for x in order]
    assert ranks == sorted(ranks)
    assert "发现以前遗留的虚拟网卡 cfw-tap，是否清理？ → 要清的话：设备管理器" in r.summary()
    text = r.summary()
    for bad in ("\\", "C:", "Users", "AppData", ".exe"):
        assert bad not in text, bad
    assert analyze(sample_clean()).summary().startswith("网络体检：缺失")


def test_the_summary_of_a_machine_with_nothing_to_say():
    r = checkup.Report()
    assert r.summary() == "网络体检：就绪\n没有发现需要说明的东西。"


def test_the_report_json_has_the_agreed_shape():
    r = analyze(sample_1111())
    data = json.loads(r.to_json())
    assert set(data) == {"level", "findings", "clients", "upstream_candidates"} and data["level"] == "有风险"
    assert all({"code", "level", "title", "detail", "hint"} <= set(f) for f in data["findings"])
    assert {c["name"] for c in data["clients"]} == {"sing-box", "Clash Verge Rev"}
    assert data["upstream_candidates"][0]["port"] == 1082
    assert "\n" in r.to_json() and "\n" not in r.to_json(indent=None)


@pytest.mark.parametrize("make", [sample_1111, sample_other_tun, sample_clean, sample_verge_ok, sample_v2rayn, sample_leftover_up,
                                  sample_noise_adapters, sample_dead_proxy, sample_tailscale])
def test_every_sample_survives_a_json_round_trip_and_analysis_does_not_change_it(make):
    snap = make()
    before = copy.deepcopy(snap)
    first = analyze(snap)
    assert snap == before  # 分析不改快照
    again = Snapshot.from_json(snap.to_json())
    assert again == snap
    assert analyze(again).to_json() == first.to_json() == analyze(snap).to_json()


def test_the_snapshot_reader_is_forgiving_and_strict_only_about_the_top_level():
    snap = Snapshot.from_dict({"processes": {"pid": "7", "ppid": "1", "name": "a.exe", "extra": 1}, "routes": [{"prefix": "0.0.0.0/0"}, "junk"],
                               "errors": {"routes": 5}, "adapters": "oops", "listeners": [{"address": "fe80::1%3", "port": "80", "pid": 4}]})
    assert [(p.pid, p.ppid, p.name) for p in snap.processes] == [(7, 1, "a.exe")]  # 单个对象按只有一行的列表读
    assert len(snap.routes) == 1 and snap.routes[0].prefix == "0.0.0.0/0" and snap.routes[0].persistent is False
    assert snap.errors == {"routes": "5"} and snap.adapters == [] and snap.listeners[0].address == "fe80::1"
    assert Snapshot.from_dict({}) == Snapshot()
    with pytest.raises(ValueError):
        Snapshot.from_json("[1, 2]")
    with pytest.raises(ValueError):
        Snapshot.from_json("not json")


def test_analyze_does_no_input_or_output(monkeypatch):
    import builtins
    import socket
    import subprocess

    def boom(*args, **kwargs):
        raise AssertionError("analyze 是纯函数, 不该做输入输出")
    snap = sample_1111()
    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert analyze(snap, elevated=True).level == RISK


def test_the_analysis_module_imports_nothing_that_could_do_input_or_output():
    tree = ast.parse(Path(checkup.__file__).read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
    assert not imported & {"os", "sys", "subprocess", "socket", "winreg", "ctypes", "asyncio", "pathlib", "shutil", "tempfile",
                           "urllib", "http", "time", "datetime", "random", "logging", "threading"}, imported


# ---- 命令行 -----------------------------------------------------------------------------------------------------

def test_the_command_line_analyzes_a_saved_snapshot(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "is_elevated", lambda: False)
    path = tmp_path / "snap.json"
    path.write_text(sample_1111().to_json(), encoding="utf-8")
    assert cli.main(["--from-file", str(path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("网络体检：有风险\n") and "发现以前遗留的虚拟网卡 cfw-tap，是否清理？" in out
    assert cli.main(["--from-file", str(path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"report", "snapshot"} and data["report"]["level"] == "有风险" and data["snapshot"]["schema"] == 1
    # 带 BOM 的文件也能读 (用记事本存过的)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert cli.main(["--from-file", str(path)]) == 0


def test_the_command_line_tells_the_server_is_elevated_to_the_analysis(tmp_path, capsys, monkeypatch):
    path = tmp_path / "snap.json"
    path.write_text(sample_verge_ok().to_json(), encoding="utf-8")
    monkeypatch.setattr(cli, "is_elevated", lambda: True)
    assert cli.main(["--from-file", str(path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("网络体检：有风险\n") and "管理员" in out


def test_the_command_line_saves_what_it_collected(tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "collect", lambda: calls.append(1) or sample_clean())
    monkeypatch.setattr(cli, "is_elevated", lambda: None)
    path = tmp_path / "out.json"
    assert cli.main(["--save", str(path)]) == 0
    assert calls == [1] and capsys.readouterr().out.startswith("网络体检：缺失")
    raw = path.read_bytes()
    assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")  # LF, UTF-8 无 BOM
    assert Snapshot.from_json(raw.decode("utf-8")) == trim(sample_clean())  # 存下来的是筛过的 (无关的进程、服务、监听不留)
    assert not any(word.lower() in raw.decode("utf-8").lower() for word in NOISE_WORDS)


def test_the_command_line_does_not_collect_when_given_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "collect", lambda: pytest.fail("给了快照文件就不该再采集"))
    monkeypatch.setattr(cli, "is_elevated", lambda: None)
    path = tmp_path / "snap.json"
    path.write_text(sample_clean().to_json(), encoding="utf-8")
    assert cli.main(["--from-file", str(path)]) == 0


def test_the_command_line_reports_a_missing_or_broken_snapshot_file(tmp_path, capsys):
    assert cli.main(["--from-file", str(tmp_path / "nope.json")]) == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    assert cli.main(["--from-file", str(bad)]) == 1
    assert "读不了快照文件" in capsys.readouterr().err


def test_the_command_line_probe_is_explicit_and_stands_alone(tmp_path, capsys):
    for argv in (["--probe", "127.0.0.1"], ["--probe", "127.0.0.1:abc"], ["--probe", "127.0.0.1:1", "--save", str(tmp_path / "x")],
                 ["--probe", "127.0.0.1:1", "--from-file", str(tmp_path / "x")]):
        with pytest.raises(SystemExit) as info:
            cli.main(argv)
        assert info.value.code == 2
    capsys.readouterr()

    async def go():
        async with FakeSocks5("full") as fake:  # 只对测试里自己起的假服务端
            return await asyncio.to_thread(cli.main, ["--probe", f"127.0.0.1:{fake.port}"])
    assert asyncio.run(go()) == 0
    out = capsys.readouterr().out
    assert "SOCKS5：是" in out and "UDP：支持" in out and "详情：支持 UDP ASSOCIATE" in out

    async def refused():  # 非回环地址: 直接拒绝, 没有连接
        return await asyncio.to_thread(cli.main, ["--probe", "8.8.8.8:1080", "--json"])
    assert asyncio.run(refused()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["socks5"] is False and data["udp"] is False and data["detail"].startswith("拒绝")

"""网络插件第 0 步 (4/4 之 3): SOCKS5 握手探测 (wm/net/socks_probe.py).

只对测试里自己起的假服务端 (tests/net_fakes.py 的 FakeSocks5, 127.0.0.1 的随机端口) 验证; 不对这台电脑上真的 1082-1085 或任何真端口跑.
五种情况: 完整支持; 支持 SOCKS5 但 UDP 被拒 (回应码 0x07); 对方是 HTTP 代理; 端口没人听; 传入非回环地址 (断言没有发起任何连接).
另外: 超时、方法协商要认证、对方一连上就关、回应格式不对、UDP 回应里三种地址类型, 以及"只发了协议里的那两个包、不往回应给的 UDP 端口发包".
每条测试结束时假服务端都关干净, 不留监听."""
from __future__ import annotations

import asyncio
import socket
import time

import pytest
from net_fakes import FakeSocks5, free_port

from wm.net import socks_probe
from wm.net.socks_probe import probe_socks5

GREETING = b"\x05\x01\x00"
UDP_ASSOCIATE = b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00"  # 地址和端口填全 0


def run(coro):
    return asyncio.run(coro)


async def probe_against(mode, **kwargs):
    """起一个假服务端, 探测它, 等它收完, 返回 (结果, 假服务端)."""
    probe_kwargs = {k: kwargs.pop(k) for k in ("timeout_s",) if k in kwargs}
    async with FakeSocks5(mode, **kwargs) as fake:
        result = await probe_socks5("127.0.0.1", fake.port, **probe_kwargs)
        await fake.wait_idle()
        return result, fake


def assert_shape(result):
    assert set(result) == {"socks5", "udp", "detail"} and isinstance(result["socks5"], bool) and isinstance(result["udp"], bool)
    assert isinstance(result["detail"], str) and result["detail"]


# ---- 五种情况 ---------------------------------------------------------------------------------------------------

def test_full_support():
    result, fake = run(probe_against("full"))
    assert_shape(result)
    assert result["socks5"] is True and result["udp"] is True
    assert result["detail"].startswith("支持 UDP ASSOCIATE") and "45678" in result["detail"]
    assert fake.connections == 1
    assert fake.received == [GREETING + UDP_ASSOCIATE]  # 只发了方法协商和 UDP ASSOCIATE 请求, 没有别的 (连接是探测这边关的)


def test_socks5_but_udp_refused_with_0x07():
    result, fake = run(probe_against("udp_refused"))
    assert_shape(result)
    assert result["socks5"] is True and result["udp"] is False
    assert "0x07" in result["detail"] and "UDP" in result["detail"] and result["detail"].startswith("是 SOCKS5")
    assert fake.received == [GREETING + UDP_ASSOCIATE]


def test_an_http_proxy_is_not_socks5():
    result, fake = run(probe_against("http"))
    assert_shape(result)
    assert result["socks5"] is False and result["udp"] is False
    assert result["detail"].startswith("不是 SOCKS5") and "HTTP" in result["detail"]
    assert fake.received == [GREETING]  # 不是 SOCKS5 就到此为止, 没有再发 UDP ASSOCIATE


def test_nobody_listening_on_the_port():
    # Windows 上连没人听的本机端口要 2 秒多才收到拒绝, 所以这里把时限放宽 (默认 3 秒; 太短会先报成超时)
    result = run(probe_socks5("127.0.0.1", free_port(), timeout_s=15))
    assert_shape(result)
    assert result["socks5"] is False and result["udp"] is False and result["detail"].startswith("连不上")


def test_a_connect_that_never_finishes_is_a_timeout_in_the_connect_stage(monkeypatch):
    async def hang(*args, **kwargs):
        await asyncio.sleep(60)
    monkeypatch.setattr(asyncio, "open_connection", hang)
    result = run(probe_socks5("127.0.0.1", 1080, timeout_s=0.2))
    assert_shape(result)
    assert result["socks5"] is False and result["udp"] is False and result["detail"].startswith("超时：连接")


NON_LOOPBACK = ["localhost", "0.0.0.0", "::1", "::", "192.168.1.3", "10.0.0.1", "8.8.8.8", "128.0.0.1", "126.255.255.255", "example.com", "",
                " ", "127.0.0.1.evil", "127.0.0.1:1080", "::ffff:127.0.0.1", "0177.0.0.1", "2130706433", None, 2130706433]


@pytest.mark.parametrize("host", NON_LOOPBACK)
def test_anything_outside_127_0_0_0_8_is_refused_without_any_connection(monkeypatch, host):
    attempts = []

    def spy(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("不该发起任何连接")
    monkeypatch.setattr(asyncio, "open_connection", spy)
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "create_connection", spy)
    monkeypatch.setattr(socket, "create_connection", spy)

    async def go():
        monkeypatch.setattr(socket.socket, "connect", spy)  # 事件循环已经建好了 (Windows 上它自己的自管道要连一下), 现在才换
        return await probe_socks5(host, 1080)
    result = run(go())
    assert_shape(result)
    assert result["socks5"] is False and result["udp"] is False and result["detail"].startswith("拒绝")
    assert attempts == []


def test_a_listening_fake_server_sees_no_connection_when_the_name_is_localhost():
    async def go():
        async with FakeSocks5("full") as fake:
            results = [await probe_socks5(host, fake.port) for host in ("localhost", "0.0.0.0", "::1")]
            await asyncio.sleep(0.05)
            return results, fake.connections
    results, connections = run(go())
    assert connections == 0 and all(r["detail"].startswith("拒绝") for r in results)


@pytest.mark.parametrize("port", [0, -1, 65536, 99999, "1080", 1080.0, None, True])
def test_a_bad_port_is_refused_without_any_connection(monkeypatch, port):
    monkeypatch.setattr(asyncio, "open_connection", lambda *a, **k: pytest.fail("不该发起任何连接"))
    result = run(probe_socks5("127.0.0.1", port))
    assert_shape(result)
    assert result["socks5"] is False and result["detail"].startswith("拒绝：端口")


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "127.1.2.3", "127.255.255.254", " 127.0.0.1 "])
def test_every_address_in_127_0_0_0_8_is_allowed_through(monkeypatch, host):
    seen = []

    async def refused(h, p, **kwargs):
        seen.append((h, p))
        raise ConnectionRefusedError()
    monkeypatch.setattr(asyncio, "open_connection", refused)
    result = run(probe_socks5(host, 1080))
    assert seen == [(host.strip(), 1080)] and result["detail"].startswith("连不上")


# ---- 别的情况 ---------------------------------------------------------------------------------------------------

def test_a_server_that_never_answers_times_out_and_the_time_limit_covers_everything():
    started = time.monotonic()
    result, fake = run(probe_against("silent", timeout_s=0.3))
    assert time.monotonic() - started < 3
    assert_shape(result)
    assert result["socks5"] is False and result["udp"] is False and result["detail"].startswith("超时") and "方法协商" in result["detail"]
    assert fake.received == [GREETING]  # 超时后也只发了那一个包, 连接关干净了


def test_a_server_that_answers_the_greeting_but_not_the_udp_request():
    result, fake = run(probe_against("slow_udp", timeout_s=0.3))
    assert result["socks5"] is True and result["udp"] is False  # 已经确认是 SOCKS5, 只是 UDP 没回应
    assert result["detail"].startswith("超时") and "UDP ASSOCIATE" in result["detail"]
    assert fake.received == [GREETING + UDP_ASSOCIATE]


def test_a_server_that_needs_authentication_is_not_probed_further():
    result, fake = run(probe_against("auth_required"))
    assert result["socks5"] is True and result["udp"] is False
    assert "认证" in result["detail"] and "没有继续探测" in result["detail"]
    assert fake.received == [GREETING]  # 不认证, 不发 UDP ASSOCIATE


def test_a_server_that_hangs_up_at_once():
    result, fake = run(probe_against("closes"))
    assert result["socks5"] is False and result["udp"] is False and result["detail"].startswith("不是 SOCKS5")
    assert "没回话" in result["detail"]  # 是读到了 EOF 还是写的时候就被 reset, 说法里都有 "没回话"
    assert fake.connections == 1


def test_a_connection_dropped_in_the_middle_of_the_exchange(monkeypatch):
    real = socks_probe._read
    for fail_on, expected in ((1, False), (2, True)):  # 第一次读 (方法协商的回应) 断了 / 第二次读 (UDP ASSOCIATE 的回应) 断了
        count = {"n": 0}

        async def flaky(reader, n, fail_on=fail_on, count=count):
            count["n"] += 1
            if count["n"] == fail_on:
                raise ConnectionResetError()
            return await real(reader, n)
        monkeypatch.setattr(socks_probe, "_read", flaky)
        result, fake = run(probe_against("full"))
        assert result["socks5"] is expected and result["udp"] is False
        assert "中途把连接断了（ConnectionResetError）" in result["detail"]


def test_a_reply_to_the_udp_request_that_is_not_socks5():
    result, fake = run(probe_against("bad_udp"))
    assert result["socks5"] is True and result["udp"] is False and "回应不对" in result["detail"]


def test_a_short_refusal_still_gets_its_reply_code_read():
    result, fake = run(probe_against("short_refuse"))
    assert result["socks5"] is True and result["udp"] is False and "0x07" in result["detail"]


@pytest.mark.parametrize("atyp", [1, 4, 3])
def test_the_udp_reply_may_carry_an_ipv4_ipv6_or_domain_address(atyp):
    result, fake = run(probe_against("full", atyp=atyp, udp_port=40123))
    assert result["socks5"] is True and result["udp"] is True and "40123" in result["detail"]


def test_the_udp_port_the_server_hands_back_is_never_sent_anything():
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        udp.bind(("127.0.0.1", 0))
        udp.setblocking(False)
        result, fake = run(probe_against("full", udp_port=udp.getsockname()[1]))
        assert result["udp"] is True
        time.sleep(0.1)
        with pytest.raises(BlockingIOError):
            udp.recvfrom(2048)  # 没有任何数据报到过
    finally:
        udp.close()


def test_the_fake_server_leaves_no_listener_behind():
    async def go():
        async with FakeSocks5("full") as fake:
            port = fake.port
            assert (await probe_socks5("127.0.0.1", port))["udp"] is True
        return port
    port = run(go())
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()  # 假服务端关了, 连不上


def test_the_probe_is_never_called_by_the_checkup_itself():
    import inspect

    from wm.net import checkup, collect
    for module in (checkup, collect):
        assert "socks_probe" not in inspect.getsource(module) and "probe_socks5" not in inspect.getsource(module)
    assert callable(socks_probe.probe_socks5)

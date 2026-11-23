"""2D 广场的本机中继 (tools/plaza_relay.py, 世界1). 全在进程内: 中继随机端口, 客户端是裸 WebSocket.

验收 5a 只听 127.0.0.1 (bind)、5b 进房间要邀请码 (invite)、世界6 不对陌生人开放 (没有大厅 / 房间列表).
"""
import asyncio
import json
import socket
from pathlib import Path

import aiohttp
import pytest

from plaza_fakes import ROOT, load_relay

R = load_relay()


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Rig:
    def __init__(self, **kw):
        self.relay = R.Relay(**kw)
        self.http = None
        self.socks = []

    async def __aenter__(self):
        self.port = await self.relay.start(0)
        self.http = aiohttp.ClientSession(trust_env=False)
        return self

    async def __aexit__(self, *a):
        for ws in self.socks:
            await ws.close()
        await self.http.close()
        await self.relay.stop()

    async def conn(self, **hello):
        ws = await self.http.ws_connect(f"http://127.0.0.1:{self.port}/plaza")
        self.socks.append(ws)
        if hello:
            await ws.send_json({"t": "hello", **hello})
        return ws


async def recv(ws, t=None, timeout=3.0):
    """收下一帧 (指定 t 就一直收到那种为止)."""
    while True:
        msg = await asyncio.wait_for(ws.receive(), timeout)
        if msg.type != aiohttp.WSMsgType.TEXT:
            return {"t": "_closed", "type": msg.type}
        f = json.loads(msg.data)
        if t is None or f["t"] == t:
            return f


def run(coro):
    return asyncio.run(coro)


def test_bind_only_loopback():
    async def go():
        async with Rig() as rig:
            assert all(a[0] == "127.0.0.1" for a in rig.relay.runner.addresses)
            # 有局域网地址时, 连它的同一个端口必须被拒
            try:
                lan = socket.gethostbyname(socket.gethostname())
            except OSError:
                lan = "127.0.0.1"
            if not lan.startswith("127."):
                with pytest.raises(OSError):
                    await asyncio.wait_for(asyncio.open_connection(lan, rig.port), 4)
            else:
                pytest.skip("这台机器没有局域网地址, 只核对了监听地址是 127.0.0.1")

    run(go())


def test_bind_source_has_no_wildcard_and_no_host_option():
    src = (ROOT / "tools" / "plaza_relay.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "0.0.0.0" not in code
    assert '"--host"' not in code and "'--host'" not in code
    assert 'HOST = "127.0.0.1"' in src
    # 不落盘、不开数据库、不记聊天
    assert "sqlite" not in code and "open(" not in code and ".write(" not in code


def test_invite_needs_valid_code():
    async def go():
        async with Rig() as rig:
            a = await rig.conn(create=True, name="甲")
            joined = await recv(a, "joined")
            code = joined["code"]
            assert len(code) == 8 and joined["you"]["name"] == "甲"
            # 没码
            n = await rig.conn(name="乙")
            assert (await recv(n, "err"))["why"] == "bad_code"
            # 错码
            w = await rig.conn(name="乙", code="ZZZZZZZZ")
            assert (await recv(w, "err"))["why"] == "bad_code"
            # 对的码 (小写也认)
            ok = await rig.conn(name="乙", code=code.lower())
            assert (await recv(ok, "joined"))["code"] == code
            # 甲收到乙进来
            ev = await recv(a, "ev")
            assert ev["k"] == "entered" and ev["name"] == "乙"

    run(go())


def test_invite_three_wrong_codes_closes_connection():
    async def go():
        async with Rig() as rig:
            ws = await rig.conn()
            for i in range(3):
                await ws.send_json({"t": "hello", "name": f"x{i}", "code": "NOPENOPE"})
                assert (await recv(ws, "err"))["why"] == "bad_code"
            assert (await recv(ws))["t"] == "_closed"

    run(go())


def test_invite_global_bad_code_rate_limit():
    async def go():
        clock = Clock()
        async with Rig(clock=clock) as rig:
            whys = []
            for i in range(R.BAD_CODES_PER_MIN + 3):
                ws = await rig.conn(name=f"x{i}", code="NOPENOPE")
                whys.append((await recv(ws, "err"))["why"])
            assert whys[:R.BAD_CODES_PER_MIN] == ["bad_code"] * R.BAD_CODES_PER_MIN
            assert whys[R.BAD_CODES_PER_MIN:] == ["rate"] * 3
            clock.t += 61   # 过一分钟又可以试了
            ws = await rig.conn(name="y", code="NOPENOPE")
            assert (await recv(ws, "err"))["why"] == "bad_code"

    run(go())


def test_invite_no_discovery_frames_and_origin_refused():
    async def go():
        async with Rig() as rig:
            a = await rig.conn(create=True, name="甲")
            await recv(a, "joined")
            for t in ("list_rooms", "rooms", "find", "lobby", "who"):
                await a.send_json({"t": t, "rid": t})
                f = await recv(a, "err")
                assert f["why"] == "unknown_frame"
            # 第一帧不是 hello
            b = await rig.conn()
            await b.send_json({"t": "move", "dir": "left"})
            assert (await recv(b, "err"))["why"] == "hello_first"
            # 浏览器的网页 (带 Origin) 连不进来
            with pytest.raises(aiohttp.WSServerHandshakeError) as ei:
                await rig.http.ws_connect(f"http://127.0.0.1:{rig.port}/plaza", headers={"Origin": "http://evil.example"})
            assert ei.value.status == 403

    run(go())


def test_room_rules_names_capacity_and_cleanup():
    async def go():
        async with Rig() as rig:
            a = await rig.conn(create=True, name="甲")
            code = (await recv(a, "joined"))["code"]
            dup = await rig.conn(name="甲", code=code)
            assert (await recv(dup, "err"))["why"] == "name_taken"
            dup2 = await rig.conn(name=" 甲 ", code=code)   # 空白不同也算重名? (strip 之后相同)
            assert (await recv(dup2, "err"))["why"] == "name_taken"
            others = [await rig.conn(name=f"p{i}", code=code) for i in range(R.MAX_MEMBERS - 1)]
            for o in others:
                await recv(o, "joined")
            full = await rig.conn(name="多余", code=code)
            assert (await recv(full, "err"))["why"] == "room_full"
            # 名字里的控制字符被清掉
            assert rig.relay.members_of(code)[0]["name"] == "甲"
            # 都走了, 码作废
            for ws in [a, *others]:
                await ws.close()
            await asyncio.sleep(0.2)
            late = await rig.conn(name="晚", code=code)
            assert (await recv(late, "err"))["why"] == "bad_code"
            assert not rig.relay.rooms

    run(go())


def test_room_limit():
    async def go():
        async with Rig() as rig:
            for i in range(R.MAX_ROOMS):
                ws = await rig.conn(create=True, name=f"n{i}")
                await recv(ws, "joined")
            extra = await rig.conn(create=True, name="多")
            assert (await recv(extra, "err"))["why"] == "too_many_rooms"

    run(go())


def test_move_say_emote_and_broadcast():
    async def go():
        clock = Clock()
        async with Rig(clock=clock, spawn=[(0, 0), (5, 5)]) as rig:
            a = await rig.conn(create=True, name="甲")
            code = (await recv(a, "joined"))["code"]
            b = await rig.conn(name="乙", code=code)
            j = await recv(b, "joined")
            assert j["members"] == [{"id": "m1", "name": "甲", "pos": [0, 0]}] and j["you"]["pos"] == [5, 5]
            await recv(a, "ev")
            # 走: 边界夹住
            await a.send_json({"t": "move", "dir": "left", "rid": "1"})
            ack = await recv(a, "ack")
            assert ack["ok"] and ack["pos"] == [0, 0] and ack["rid"] == "1"
            await a.send_json({"t": "move", "dir": "right", "rid": "2"})
            assert (await recv(a, "ack"))["pos"] == [1, 0]
            ev = await recv(b, "ev")
            assert ev["k"] == "moved" and ev["pos"] == [1, 0] and ev["name"] == "甲"
            # 坏方向
            await a.send_json({"t": "move", "dir": "north", "rid": "3"})
            assert (await recv(a, "ack"))["why"] == "bad_dir"
            # 说话: 清洗、限长、转给别人、自己只收 ack
            await a.send_json({"t": "say", "text": "  你好\n" + chr(0x200b) + "世界  ", "rid": "4"})
            assert (await recv(a, "ack"))["ok"]
            ev = await recv(b, "ev")
            assert ev["k"] == "said" and ev["text"] == "你好 世界"
            await a.send_json({"t": "say", "text": "x" * (R.MAX_SAY + 1), "rid": "5"})
            assert (await recv(a, "ack"))["why"] == "too_long"
            await a.send_json({"t": "say", "text": "   ", "rid": "6"})
            assert (await recv(a, "ack"))["why"] == "empty"
            # 表情
            await a.send_json({"t": "emote", "emote": "dance", "rid": "7"})
            assert (await recv(a, "ack"))["why"] == "bad_emote"
            await a.send_json({"t": "emote", "emote": "wave", "rid": "8"})
            assert (await recv(a, "ack"))["ok"]
            ev = await recv(b, "ev")
            assert ev["k"] == "emote" and ev["emote"] == "wave"
            # 离开: 另一个人看到
            await a.send_json({"t": "bye"})
            ev = await recv(b, "ev")
            assert ev["k"] == "left" and ev["name"] == "甲"

    run(go())


def test_rate_limits_and_window_reopens():
    async def go():
        clock = Clock()
        async with Rig(clock=clock) as rig:
            a = await rig.conn(create=True, name="甲")
            await recv(a, "joined")
            res = []
            for i in range(R.MOVE_LIMIT[0] + 2):
                await a.send_json({"t": "move", "dir": "right", "rid": str(i)})
                res.append((await recv(a, "ack")).get("why"))
            assert res[:R.MOVE_LIMIT[0]] == [None] * R.MOVE_LIMIT[0] and res[R.MOVE_LIMIT[0]:] == ["rate", "rate"]
            clock.t += R.MOVE_LIMIT[1] + 0.1
            await a.send_json({"t": "move", "dir": "left", "rid": "x"})
            assert (await recv(a, "ack")).get("why") is None
            says = []
            for i in range(R.SAY_LIMIT[0] + 1):
                await a.send_json({"t": "say", "text": f"第{i}句", "rid": f"s{i}"})
                says.append((await recv(a, "ack")).get("why"))
            assert says[-1] == "rate" and says[:-1] == [None] * R.SAY_LIMIT[0]

    run(go())


def test_frame_too_big_and_bad_json():
    async def go():
        async with Rig() as rig:
            ws = await rig.conn(create=True, name="甲")
            await recv(ws, "joined")
            await ws.send_str("不是 json")
            assert (await recv(ws, "err"))["why"] == "bad_json"
            await ws.send_str(json.dumps({"t": "say", "text": "x" * (R.MAX_FRAME + 10)}))
            assert (await recv(ws))["t"] == "_closed"

    run(go())


def test_frame_log_is_off_by_default_and_kick_place_hooks():
    async def go():
        log = []
        async with Rig(frame_log=log) as rig:
            a = await rig.conn(create=True, name="甲")
            code = (await recv(a, "joined"))["code"]
            b = await rig.conn(name="乙", code=code)
            await recv(b, "joined")
            assert rig.relay.place(code, "乙", (9, 9))
            ack = await recv(b, "ack")
            assert ack["pos"] == [9, 9]
            assert await rig.relay.kick(code, "乙")
            assert (await recv(b))["t"] == "_closed"
        assert any("hello" in t for _, t in log)
        assert R.Relay().frame_log is None

    run(go())

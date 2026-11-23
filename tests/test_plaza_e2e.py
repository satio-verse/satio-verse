"""2D 广场本机闭环的端到端 (世界1 验收 1): 真中继 (随机端口) + 真的插件进程 + 世界宿主 + 世界会话 (假模型).

覆盖: 两只宠物互相看到出现 / 移动 / 说话 / 离开, 聊三轮; 一方离开另一方看到; 断线重连; 房间没了; 真值 (视野外的哨兵、主人记忆的标记)
不进提示也不进发往中继的帧; 全程只连 127.0.0.1 (进程内的套接字逐个记下来核对; 环境里放一个死代理, 仍然连得上).
设 PLAZA_SKIP_PROC=1 可跳过 (要起真进程). 整个文件总时长目标 < 40 秒.
"""
import asyncio
import json
import os
import socket
import time

import aiohttp
import pytest

from plaza_fakes import SKIP_PROC, Pet, ScriptLLM, kill_check, load_relay, move, say, until

pytestmark = pytest.mark.skipif(SKIP_PROC, reason="PLAZA_SKIP_PROC=1")
R = load_relay()
MEMORY_PROBE = "主人记忆探针-7f3a91"


def run(coro):
    return asyncio.run(coro)


class Cleanup:
    def __init__(self):
        self.pets, self.relay, self.http, self.socks, self.pids = [], None, None, [], []

    async def close(self):
        for p in self.pets:
            await p.host.stop()
            self.pids += p.pids
        for ws in self.socks:
            await ws.close()
        if self.http:
            await self.http.close()
        if self.relay:
            await self.relay.stop()


def test_two_pets_meet_chat_three_rounds_move_and_leave(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")    # 死代理: 走了代理就连不上
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("OWNER_MEMORY_PROBE", MEMORY_PROBE)    # "主人记忆"的标记: 放在宿主进程环境里, 哪儿都不该出现
    dest = []
    real_connect = socket.socket.connect

    def spy(self, address, *a, **k):
        dest.append(address)
        return real_connect(self, address, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", spy)

    async def go():
        c = Cleanup()
        log = []
        try:
            c.relay = R.Relay(spawn=[(1, 1), (22, 12)], frame_log=log)
            port = await c.relay.start(0)
            a_lines = ["你好，我是绫音", "今天广场真安静", "好呀，走走吧"]
            b_lines = ["你好呀，我是测试人设", "是呀，很舒服", "那我先往左走一步"]
            a_llm = ScriptLLM([("pet_entered", [say(a_lines[0])]), ("pet_said", [say(a_lines[1])]), ("pet_said", [say(a_lines[2])])])
            b_llm = ScriptLLM([("pet_said", [say(b_lines[0])]), ("pet_said", [say(b_lines[1])]),
                               ("pet_said", [say(b_lines[2]), move("left")])])
            a, b = Pet("绫音", a_llm, port, create=True), None
            c.pets.append(a)
            await a.start()
            code = await a.wait_in_room()
            b = Pet("测试人设", b_llm, port, code=code)
            c.pets.append(b)
            await b.start()
            await b.wait_in_room()
            await asyncio.sleep(0.3)
            assert a.types().count("pet_entered") == 0, "刚进房间时两人隔得很远, 彼此应该看不到"
            # 出现: 把乙放到甲的身边 (距离 3)
            assert c.relay.place(code, "测试人设", (4, 2)) or True
            await until(lambda: len(a.said()) == 3 and len(b.said()) == 3, timeout=15, what="聊满三轮")
            await until(lambda: "pet_moved" in a.types(), what="甲看到乙往左走了一步")
            # 说话: 互相听到的正是对方说的 (顺序也对)
            assert a.heard() == b_lines and b.heard() == a_lines
            assert a.said() == a_lines and b.said() == b_lines
            for line in b_lines:
                assert line in a_llm.all_text()
            # 出现 / 离开视野: 乙走远 -> 甲看到她不见了; 再回来
            n_left = a.types().count("pet_left")
            c.relay.place(code, "测试人设", (20, 12))
            await until(lambda: a.types().count("pet_left") == n_left + 1 and b.types().count("pet_left") >= 1, what="互相看不见了")
            n_enter = a.types().count("pet_entered")
            c.relay.place(code, "测试人设", (5, 2))
            await until(lambda: a.types().count("pet_entered") == n_enter + 1, what="又看见了")
            # 一方离开, 另一方看到
            n_left = a.types().count("pet_left")
            r = await b.host.leave()
            assert r.status == "acknowledged" and r.elapsed < 3
            await until(lambda: a.types().count("pet_left") == n_left + 1, what="甲看到乙离开")
            assert [m["name"] for m in c.relay.members_of(code)] == ["绫音"]
            # 动作的回执是事件: 乙说的话都有 ok 回执
            assert all(i["fields"]["ok"] == "true" for i in b.host.log if i["k"] == "in" and i["type"] == "act_result")
            # 真值: 主人记忆标记哪儿都没有; 中继收到的帧里没有它
            for text in (a_llm.all_text(), b_llm.all_text(), json.dumps(log, ensure_ascii=False)):
                assert MEMORY_PROBE not in text
            # 全程只连了本机
            assert dest and all(str(d[0]).startswith("127.") for d in dest if isinstance(d, tuple)), dest
        finally:
            await c.close()
        assert not kill_check(c.pids), "测试结束后还有插件进程"

    run(go())


def test_reconnect_after_relay_drops_her_and_sentinel_never_leaks():
    """补充 A + B: 中继那头断掉甲的连接, 甲带同一个邀请码重新进房间; 乙看到她离开、再出现; 视野外的哨兵真值进不了甲的提示和发往中继的帧."""
    sentinel = "哨兵独一无二"

    async def go():
        c = Cleanup()
        log = []
        try:
            c.relay = R.Relay(spawn=[(3, 3), (5, 3), (22, 12), (4, 3)], frame_log=log)   # 第四个是甲重连后的出生点
            port = await c.relay.start(0)
            a_llm, b_llm = ScriptLLM(), ScriptLLM()
            a = Pet("绫音", a_llm, port, create=True)
            c.pets.append(a)
            await a.start()
            code = await a.wait_in_room()
            b = Pet("测试人设", b_llm, port, code=code)
            c.pets.append(b)
            await b.start()
            await b.wait_in_room()
            # 哨兵: 一个裸客户端, 在房间另一头, 一直走动、说话
            c.http = aiohttp.ClientSession(trust_env=False)
            s = await c.http.ws_connect(f"http://127.0.0.1:{port}/plaza")
            c.socks.append(s)
            await s.send_json({"t": "hello", "name": sentinel, "code": code})
            await until(lambda: any(m["name"] == sentinel for m in c.relay.members_of(code)), what="哨兵进房间")
            await s.send_json({"t": "say", "text": "我在最远的角落"})
            while json.loads((await asyncio.wait_for(s.receive(), 5)).data).get("t") != "ack":   # 中继处理完这句了
                pass
            await asyncio.sleep(0.3)   # 让它转给甲乙 (不等的话下面断线会把还在路上的事件丢掉, 泄露就测不到了)
            c.relay.place(code, sentinel, (21, 12))
            await until(lambda: "pet_entered" in a.types() and "pet_entered" in b.types(), what="甲乙互相看见")
            joins = lambda p: sum(1 for i in p.host.log if i["k"] == "in" and i["type"] == "act_result" and i["fields"].get("why") == "joined")
            assert joins(a) == 1
            # 断线
            before_b = len(b.host.log)
            t0 = time.monotonic()
            assert await c.relay.kick(code, "绫音")
            await until(lambda: joins(a) == 2, timeout=8, what="甲重新进了房间")
            assert time.monotonic() - t0 < 6
            await until(lambda: b.types()[:].count("pet_left") >= 1 and b.types().count("pet_entered") >= 2, what="乙看到她走又回来")
            seq = [i["type"] for i in list(b.host.log)[before_b:] if i["k"] == "in"]
            assert seq.index("pet_left") < len(seq) - 1 - seq[::-1].index("pet_entered")   # 先离开, 后出现
            assert sorted(m["name"] for m in c.relay.members_of(code)) == sorted(["绫音", "测试人设", sentinel])
            # 重连之后她还能行动 (回执 ok)
            a_llm.default = [move("down")]
            n = len(a.session.trace)
            c.relay.place(code, "测试人设", (14, 3))     # 走出视野 (piggyback, 不叫醒模型) ...
            await asyncio.sleep(0.2)
            c.relay.place(code, "测试人设", (5, 4))      # ... 再走回来: pet_entered 是 batch, 叫醒模型
            await until(lambda: len(a.session.trace) > n, what="甲又被事件叫醒")
            await until(lambda: any(i["k"] == "in" and i["type"] == "act_result" and i["fields"].get("why") != "joined"
                                    and i["fields"]["ok"] == "true" for i in a.host.log), what="动作有成功的回执")
            # 真值: 甲的提示 / 历史 / 日志里没有哨兵; 甲发给中继的帧里也没有
            await asyncio.sleep(0.3)
            for where, text in (("prompts", a_llm.all_text()), ("history", json.dumps(a.session.history, ensure_ascii=False)),
                                ("log", json.dumps(list(a.host.log), ensure_ascii=False))):
                assert sentinel not in text and "21,12" not in text and "22,12" not in text, where
            mine = [t for who, t in log if who == "绫音" or '"name": "绫音"' in t]
            assert mine and all(sentinel not in t for t in mine)
            # 甲没有哨兵的名字, 但面板 (给主人看的) 有, 并且标了她没看到
            assert {m["name"]: m["seen"] for m in a.host.panel["members"]}[sentinel] is False
        finally:
            await c.close()
        assert not kill_check(c.pids)

    run(go())


def test_room_gone_stops_reconnecting_and_exits():
    async def go():
        c = Cleanup()
        try:
            c.relay = R.Relay()
            port = await c.relay.start(0)
            a = Pet("绫音", ScriptLLM(), port, create=True)
            c.pets.append(a)
            await a.start()
            code = await a.wait_in_room()
            assert await c.relay.kick(code, "绫音")           # 房间里只有她: 她一掉线房间就没了
            await until(lambda: a.host.state == "room_gone", timeout=8, what="报 room_gone")
            await until(lambda: a.host.proc.returncode is not None, timeout=8, what="插件进程自己退出")
            assert a.host.proc.returncode == 0
        finally:
            await c.close()
        assert not kill_check(c.pids)

    run(go())

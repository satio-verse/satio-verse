"""QQ 上说"用工作台…": 渠道把活交给工作台 (wm/agent), 由那边的她拆成并行的子会话.
这条只测桥本身 (不连真 QQ、不调模型): 一件事开一个会话、话送进去、订阅结束事件、同一件事还在做时不再开、没接上时老实说.
做完回报、引用回报接着做见 test_workbench_threads.py.
"""
import asyncio

from wm.channels import wbthreads as WT
from wm.channels.manager import WEIXIN, WORKBENCH_HINT, ChannelManager


class FakeSession:
    def __init__(self, sid):
        self.id, self.title, self.status = sid, "新会话", "idle"


class FakeHub:
    def __init__(self):
        self.sessions, self.sent, self.made, self.subs = {}, [], 0, {}

    def get(self, sid):
        return self.sessions.get(sid)

    def new_session(self, **_kw):
        self.made += 1
        s = FakeSession("ws-%d" % self.made)
        self.sessions[s.id] = s
        return s

    def save(self, s):
        pass

    def subscribe(self, sid, send):
        self.subs.setdefault(sid, set()).add(send)

    def unsubscribe(self, sid, send):
        self.subs.get(sid, set()).discard(send)

    async def send(self, sid, text, **_kw):
        self.sent.append((sid, text))
        self.sessions[sid].status = "running"  # 和真的一样: 送进去就开始这一轮


def manager(hub):
    m = ChannelManager.__new__(ChannelManager)
    m.agent_hub, m.channel, m.conf, m.clock = hub, "qq", {"owner_id": 42}, lambda: 1000.0
    m.wb = WT.Threads(None, lambda: m.clock())

    async def notify(_msg):
        pass

    async def diary(_text):
        pass

    async def tell(_t, _text):
        return False, []

    m.notify, m._diary = notify, diary
    if hub is not None:
        m.wb.attach(hub, tell)
    return m


def test_hint():
    assert WORKBENCH_HINT.search("用工作台把这事拆一下")
    assert WORKBENCH_HINT.search("以后上下文太大可以开子会话")
    assert not WORKBENCH_HINT.search("今天天气不错")


def test_hands_task_to_workbench():
    async def run():
        hub = FakeHub()
        m = manager(hub)
        note = await m.workbench_run("用工作台帮我查三件事, 分头查", "owner", "QQ 上")
        assert hub.made == 1, hub.made
        assert hub.sent and "帮我查三件事" in hub.sent[0][1], hub.sent
        assert "先写结论" in hub.sent[0][1]  # 回报在手机上看: 最后那段话先写结论
        assert hub.sessions["ws-1"].title == "QQ：帮我查三件事, 分头查"  # 标题里不重复"用工作台"
        assert "别跟他说已经做完了" in note and "回来跟他说结果" in note, note
        assert hub.subs["ws-1"] and m.wb.items["ws-1"]["conv"] == ["private", 42]  # 订阅了结束事件, 记着回哪儿
        # 同一件事那边还在做, 又说了一遍: 不再开 (重复事件不重复建任务)
        again = await m.workbench_run("用工作台帮我查三件事, 分头查", "owner", "QQ 上")
        assert hub.made == 1 and "已经在做了" in again, again
        # 另一件事: 开新的会话, 哪怕上一个空着 (以前会接在同一个会话后面, 不相干的事串了上下文)
        hub.sessions["ws-1"].status = "idle"
        await m.workbench_run("在工作台里整理一下下载文件夹", "owner", "微信上")
        assert hub.made == 2 and hub.sessions["ws-2"].title == "微信：整理一下下载文件夹"
        assert m.wb.items["ws-2"]["conv"] == list(WEIXIN)
        # 桌宠上说的: 不往 QQ / 微信回报 (人就在电脑前)
        await m.workbench_run("用工作台查一下天气", "owner", "桌宠上")
        assert m.wb.items["ws-3"]["conv"] is None

    asyncio.run(run())


def test_refused_start_leaves_no_thread():
    """工作台那边没开始 (比如会话正在复制): 老实说没接住, 也不留一条线 (不然她提示里会说"停在半路了")."""
    async def run():
        hub = FakeHub()

        async def refuse(sid, text, **_kw):
            return "会话正在复制，请稍后发送"
        hub.send = refuse
        m = manager(hub)
        note = await m.workbench_run("用工作台帮我查三件事", "owner", "QQ 上")
        assert "没接住 (会话正在复制" in note, note
        assert m.wb.items == {} and not hub.subs.get("ws-1")

    asyncio.run(run())


def test_without_hub_says_so():
    async def run():
        m = manager(None)
        note = await m.workbench_run("用工作台做点事", "owner")
        assert "没连上" in note, note

    asyncio.run(run())

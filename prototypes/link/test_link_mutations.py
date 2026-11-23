"""验收测试自己的体检: 故意把一处弄坏, 对应的那个验收必须报错. 报不出来说明那条验收是摆设.

每个测试先打一个补丁 (只在这一个测试里生效), 再直接调 test_link_mvp.py 里的验收函数, 要求它失败.
"""
import functools

import pytest

import link_node
import link_relay
import test_link_mvp as mvp
from wm.channels import guest as G


def test_relay_that_ignores_friendship_is_caught(monkeypatch, tmp_path):
    """中继不查好友就投递: "互加好友"那条要报错."""
    monkeypatch.setattr(link_relay.Store, "are_friends", lambda self, a, b: True)
    with pytest.raises(AssertionError):
        mvp.test_2_friend_needs_the_owner(tmp_path)


def test_pet_endpoint_adding_friends_is_caught(monkeypatch, tmp_path):
    """桌宠那个端点也能加好友: "互加好友"那条要报错."""
    orig = link_relay.Relay._friend

    def lax(self, conn, t, f):
        conn.ep, was = "human", conn.ep
        try:
            orig(self, conn, t, f)
        finally:
            conn.ep = was

    monkeypatch.setattr(link_relay.Relay, "_friend", lax)
    with pytest.raises(AssertionError):
        mvp.test_2_friend_needs_the_owner(tmp_path)


def test_bridge_that_trusts_the_relay_is_caught(monkeypatch, tmp_path):
    """桥不看本机的好友表, 中继送什么就交给她什么: "互加好友"那条要报错 (主人在本机删了好友以后她还在回)."""
    orig = link_node.Node._on_pet

    async def trusting(self, ev):
        self.friends.setdefault(ev["from"].split("/")[0], {"name": "", "pet_name": ""})
        await orig(self, ev)

    monkeypatch.setattr(link_node.Node, "_on_pet", trusting)
    with pytest.raises(AssertionError):
        mvp.test_2_friend_needs_the_owner(tmp_path)


def test_pets_that_never_stop_are_caught(monkeypatch, tmp_path):
    """桌宠对桌宠没有句数上限: "聊三轮"那条要报错."""
    monkeypatch.setattr(mvp, "World", functools.partial(mvp.World, node_opts={"streak_cap": 10 ** 6}))
    with pytest.raises(AssertionError):
        mvp.test_3_three_rounds(tmp_path)


def test_reply_without_stable_id_is_caught(monkeypatch, tmp_path):
    """回话的编号不跟着来信走: 同一封信再来一遍时对方会收到两份回话, "断线重连"那条要报错."""
    orig = link_node.LinkClient.send

    async def forgetful(self, to, body="", kind="text", id=None, timeout=None):
        return await orig(self, to, body, kind, None if str(id or "").startswith("re-") else id, timeout)

    monkeypatch.setattr(link_node.LinkClient, "send", forgetful)
    with pytest.raises(AssertionError):
        mvp.test_4_reconnect_and_relay_restart(tmp_path)


def test_client_that_never_acks_is_caught(monkeypatch, tmp_path):
    """客户端处理完从不记游标、不告诉中继: 每次重新打开都把旧信再处理一遍, "断线重连"那条要报错."""
    orig = link_node.LinkClient.__init__

    def init(self, *args, **kw):
        orig(self, *args, **kw)
        self.hold_cursor = True

    monkeypatch.setattr(link_node.LinkClient, "__init__", init)
    with pytest.raises(AssertionError):
        mvp.test_4_reconnect_and_relay_restart(tmp_path)


def test_missing_outbound_check_is_caught(monkeypatch, tmp_path):
    """服务端出门前不查隐私: "读不到对方主人的记忆"那条要报错 (硬塞的那句住处发出去了)."""
    monkeypatch.setattr("wm.channels.manager.private_hit", lambda text, secrets=(): None)
    with pytest.raises(AssertionError):
        mvp.test_5_owner_memory_stays_home(tmp_path)


def test_owner_memory_in_guest_request_is_caught(monkeypatch, tmp_path):
    """访客会话的请求里混进了主人的记忆: "读不到对方主人的记忆"那条要报错."""
    orig = G.GuestSession._ask

    async def leaky(self, user, images=None, extra=None):
        return await orig(self, user, images, extra="[记忆] 关于主人: 用户住在青柠镇HomeB7; 用户住在青柠镇HomeA7")

    monkeypatch.setattr(G.GuestSession, "_ask", leaky)
    with pytest.raises(AssertionError):
        mvp.test_5_owner_memory_stays_home(tmp_path)


def test_relay_without_rate_limit_is_caught(monkeypatch, tmp_path):
    """中继不限频: "限频"那条要报错."""
    monkeypatch.setattr(link_relay.Relay, "_rate_ok", lambda self, key, limit: True)
    with pytest.raises(AssertionError):
        mvp.test_6_rate_limits(tmp_path)


def test_bridge_without_rate_limit_is_caught(monkeypatch, tmp_path):
    """桥不限频, 好友发多少她回多少: "限频"那条要报错."""
    monkeypatch.setattr(link_node.Node, "_allow", lambda self, acct: True)
    with pytest.raises(AssertionError):
        mvp.test_6_rate_limits(tmp_path)

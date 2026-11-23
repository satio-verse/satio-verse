"""急2 (2026-11-15): 她过日子的那一套和 QQ 在线解开, 存盘一起修.

QQ 关着 / 挂不上时, 正式服务端 (开着记忆库) 的 tick 照常跑: 自己过日子、写日记、B 站消息; 真要 QQ 的 (说说、评论、找主人、分享)
不跑; 微信回得出去、聊天记录和她自己的日志照常存盘. 没记忆库的服务端 (测试、自检) 和以前一样什么都不做.
QQ 在线时的行为不变: 原来的 test_proactive_owner / test_qzone_draft_send_and_block / test_life_and_share 管着.
"""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_channels import (OWNER, Inbox, FakeOneBot, cloud_model, make_vault, qq_over, reply_json, run_qq, until)
from test_cloud import FakeCloud, say
from test_pipeline import make_cfg
from test_weixin import FakeILink, bind
from wm.app import ENGINES, create_app
from wm.channels import selfcheck
from wm.channels import weixin as X
from wm.channels.manager import WEIXIN, ChannelManager, ConfigError, parse_conf
from wm.channels.store import ChannelStore

LIFE = {"proactive": {"enabled": True, "idle_h": 1, "tick_s": 3600}, "private": {"min_gap_s": 0}}


def run_off(tmp_path, scenario, *extra, cloud_script=None, vault=True, fake=None):
    """起服务端, QQ 开关关着 (channels.qq.enabled=false); 假 OneBot 照样起着, 用来证明她没往 QQ 上发东西."""
    async def main():
        bot = fake or FakeOneBot()
        url = await bot.start()
        cloud = FakeCloud(*cloud_script) if cloud_script else None
        base = await cloud.start() if cloud else None
        over = qq_over(url, "channels.qq.enabled=false", "channels.qq.proactive.enabled=false", *extra)  # load_config 会读 config/local.json: 该关的写明
        if vault:
            over.append(f"memory.vault={json.dumps(str(make_vault(tmp_path)))}")
        client = TestClient(TestServer(create_app(make_cfg(tmp_path / "logs", *over))))
        await client.start_server()
        bot.app = client.server.app
        ch = bot.app[ENGINES].channels
        ch.selfmcp_home, ch.learned_file = tmp_path / "self-mcp", tmp_path / "learned.json"
        ch.bili_seen_file = tmp_path / "bili_seen.json"
        try:
            ws = await client.ws_connect("/ws")
            inbox = Inbox(ws)
            await inbox.get("hello")
            return await scenario(ws, inbox, bot, base, cloud)
        finally:
            await client.close()
            await bot.close()
            if cloud:
                await cloud.close()
    return asyncio.run(main())


async def configure(ws, inbox, base, **extra):
    await ws.send_json({"type": "channel_config", "model": cloud_model(base), **extra})
    await inbox.get("channel_config_done")


def fake_clock(ch):
    t0 = datetime(2026, 10, 25, 10, 0).timestamp()
    clock = {"t": t0}
    ch.clock = lambda: clock["t"]
    ch.owner_seen = ch.her_last_owner = t0
    return clock, t0


def own_logs(tmp_path, folder):
    return sorted((tmp_path / "vault" / "外部渠道" / "qq" / folder / "日志").glob("*.md"))


# ---------------------------------------------------------------------------
# 验收 1: QQ 不在线时过日子照常
# ---------------------------------------------------------------------------
def test_off_life_runs(tmp_path):
    """QQ 开关关着: tick 循环起来了; 隔了 idle_h 她自己找事做, 记进她自己的日志; 一句话也没往 QQ 上发."""
    async def scenario(ws, inbox, bot, base, cloud):
        await configure(ws, inbox, base, enabled=False, **LIFE)
        ch = bot.app[ENGINES].channels
        clock, t0 = fake_clock(ch)
        assert ch.state == "off" and ch._tick_task is not None
        clock["t"] = t0 + 1.1 * 3600
        await ch.tick()
        await until(lambda: own_logs(tmp_path, "她自己"))
        return ch.status(), len(cloud.requests), list(bot.sent), ch.state

    st, n, sent, state = run_off(tmp_path, scenario, cloud_script=[say(reply_json("看了一集番, 好治愈"))])
    assert state == "off" and n == 1 and sent == []
    assert st["own"]["life_today"] == 1 and st["own"]["alone"] is True
    assert "看了一集番" in own_logs(tmp_path, "她自己")[0].read_text(encoding="utf-8")


def test_unattached_life_runs(tmp_path):
    """QQ 开着但挂的是别人的号 (wrong_account, 没核对账号): 过日子照常; 不去 _reach 找主人, 也没有"没发出去"的提示."""
    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "wrong_account")
        await configure(ws, inbox, base, **LIFE)
        ch = bot.app[ENGINES].channels
        clock, t0 = fake_clock(ch)
        clock["t"] = t0 + 1.1 * 3600
        await ch.tick()
        await until(lambda: own_logs(tmp_path, "她自己"))
        return ch.status(), cloud.requests, list(bot.sent), [m["kind"] for m in inbox.of("channel_notice")]

    qq = FakeOneBot(login_id=777)
    st, reqs, sent, notices = run_off(tmp_path, scenario, "channels.qq.enabled=true", fake=qq,
                                      cloud_script=[say(reply_json("整理了一下今天记下的事")), say(reply_json("多余的"))])
    assert len(reqs) == 1 and sent == [] and "send_failed" not in notices
    assert "没在 QQ 上说话了" not in json.dumps(reqs[0]["body"], ensure_ascii=False)  # 没走找主人那条提示
    assert st["own"]["life_today"] == 1 and st["own"]["alone"] is True


def test_off_qq_only_items_skip(tmp_path):
    """QQ 关着: 说说、回说说评论、收藏表情这些真要 QQ 的不碰 (不调协议端, 不弹草稿, 不去问模型)."""
    async def scenario(ws, inbox, bot, base, cloud):
        await configure(ws, inbox, base, enabled=False, qzone={"enabled": True, "per_day": 2, "window": "20:00-21:00", "tick_s": 3600})
        ch = bot.app[ENGINES].channels
        clock, _ = fake_clock(ch)
        clock["t"] = datetime(2026, 10, 25, 21, 0).timestamp()
        await ch.tick()
        await asyncio.sleep(0.3)
        return [a for a, _ in bot.calls], len(cloud.requests), len(inbox.of("channel_draft"))

    calls, n, drafts = run_off(tmp_path, scenario, cloud_script=[say(reply_json("不该写出来的说说"))])
    assert n == 0 and drafts == 0 and not any("qzone" in a or "custom_face" in a for a in calls)


def test_off_share_not_sent(tmp_path):
    """QQ 关着时她想【分享】: 不发 (没处发), 不记分享次数, 不弹"没发出去"; 想法还在她自己的日志里."""
    async def scenario(ws, inbox, bot, base, cloud):
        await configure(ws, inbox, base, enabled=False, **LIFE)
        ch = bot.app[ENGINES].channels
        clock, t0 = fake_clock(ch)
        clock["t"] = t0 + 1.1 * 3600
        await ch.tick()
        await until(lambda: own_logs(tmp_path, "她自己"))
        await asyncio.sleep(0.2)
        return ch.status()["own"], list(bot.sent), [m["kind"] for m in inbox.of("channel_notice")], len(ch.drafts)

    own, sent, notices, drafts = run_off(tmp_path, scenario, cloud_script=[say(reply_json("【分享】我学会做曲奇啦!"))])
    assert own["life_today"] == 1 and own["share_today"] == 0 and sent == [] and drafts == 0
    assert "send_failed" not in notices
    assert "学会做曲奇" in own_logs(tmp_path, "她自己")[0].read_text(encoding="utf-8")


def test_not_formal_server_stays_idle(tmp_path):
    """没记忆库的服务端 (测试、自检起的) 不会因为 QQ 关着自己过起日子: 没有 tick 循环, tick() 什么都不做 (和以前一样)."""
    async def scenario(ws, inbox, bot, base, cloud):
        await configure(ws, inbox, base, enabled=False, **LIFE)
        ch = bot.app[ENGINES].channels
        clock, t0 = fake_clock(ch)
        clock["t"] = t0 + 1.1 * 3600
        await ch.tick()
        await asyncio.sleep(0.2)
        return getattr(ch, "life_alone", False), ch._tick_task, len(cloud.requests), ch.status()["own"].get("alone")

    alone, task, n, flag = run_off(tmp_path, scenario, vault=False, cloud_script=[say(reply_json("不该有"))])
    assert alone is False and task is None and n == 0 and not flag


def test_trigger_split(tmp_path):
    """面板"现在做一次": QQ 关着时 life 能点, qzone / comments / proactive 仍报 QQ 没连上."""
    async def scenario(ws, inbox, bot, base, cloud):
        await configure(ws, inbox, base, enabled=False, **LIFE)
        ch = bot.app[ENGINES].channels
        fake_clock(ch)
        ok = await ch.trigger("life")
        bad = [await ch.trigger(a) for a in ("qzone", "comments", "proactive")]
        return ok, bad

    ok, bad = run_off(tmp_path, scenario, cloud_script=[say(reply_json("自己琢磨了一会儿"))])
    assert ok["ok"] is True and ok["decision"] == "life"
    assert all(b["ok"] is False and "QQ 没连上" in b["error"] for b in bad)


def test_online_not_alone(tmp_path):
    """QQ 在线时 own.alone 是 False (过日子走原来的路)."""
    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        return bot.app[ENGINES].channels.status()["own"].get("alone", "没有这个字段")
    assert run_qq(tmp_path, scenario, vault=True) is False


def test_off_cards_do_not_error(tmp_path):
    """QQ 关着时她过日子冒出"想学会…"的申请卡片: 卡片照弹 (桌宠上); 15 秒后想改到 QQ 上问, 没路就静默等下一轮,
    tick 不抛错、不刷"没发出去". (这些卡片关着时只能在电脑上或微信上批, 是急3 的范围, 这次不修.)"""
    async def scenario(ws, inbox, bot, base, cloud):
        await configure(ws, inbox, base, enabled=False, ask_qq_after_s=0.05, **LIFE)
        ch = bot.app[ENGINES].channels
        clock, t0 = fake_clock(ch)
        clock["t"] = t0 + 1.1 * 3600
        await ch.tick()  # 抛错这里就红了
        card = await inbox.get("channel_draft", lambda m: m.get("kind") == "improve")
        await asyncio.sleep(0.4)  # 过了 ask_qq_after_s: _ask_on_qq 跑过一次, QQ 没路, 不报错
        d = ch.drafts[card["id"]]
        return d.get("code"), [m["kind"] for m in inbox.of("channel_notice")], list(bot.sent)

    code, notices, sent = run_off(tmp_path, scenario, cloud_script=[say(reply_json("【想学会用:知乎】想去看看"))])
    assert code is None and "send_failed" not in notices and sent == []


# ---------------------------------------------------------------------------
# 隔离自检没过: 不过日子、微信不回
# ---------------------------------------------------------------------------
def test_selfcheck_failed_blocks_life_and_weixin(tmp_path, monkeypatch):
    async def failing(cloud):
        return ["测试: 隔离没过"]
    monkeypatch.setattr(selfcheck, "run", failing)

    async def scenario(ws, inbox, bot, base, cloud):
        await configure(ws, inbox, base, **LIFE)
        ch = bot.app[ENGINES].channels
        assert ch.state == "error"
        clock, t0 = fake_clock(ch)
        clock["t"] = t0 + 1.1 * 3600
        await ch.tick()
        fake = FakeILink()
        wbase = await fake.start()
        bind(X.HOME, wbase)
        ch.weixin = X.WeixinClient(X.HOME, wbase, on_message=ch._on_weixin)
        try:
            ch.weixin.start()
            await until(lambda: ch.weixin.online, 5)
            fake.push("今天好累呀")
            await until(lambda: ch.weixin.status()["received"] == 1, 5)
            # 等她处理完这一句: 新代码直接不理 (ignored), 旧代码会开一轮 (会话任务跑完); 不靠固定睡多久 (机器忙时旧代码会慢过 0.5 秒)
            await until(lambda: ch.ignored >= 1 or (WEIXIN in ch.convs and ch.convs[WEIXIN].task is not None
                                                   and ch.convs[WEIXIN].task.done()), 15)
            await asyncio.sleep(0.3)
            return len(cloud.requests), len(fake.sent), ch.status()["own"].get("alone")
        finally:
            await ch.weixin.close()
            await fake.close()

    n, sent, alone = run_off(tmp_path, scenario, "channels.qq.enabled=true",
                             cloud_script=[say(reply_json("不该有")), say(reply_json("不该有2"))])
    assert n == 0 and sent == 0 and not alone


# ---------------------------------------------------------------------------
# 验收 2: 存盘 (QQ 关着也存)
# ---------------------------------------------------------------------------
def test_off_weixin_replies_and_persists(tmp_path):
    """QQ 关着: 你在微信上说一句, 她回得出去 (以前被静默丢掉); 聊天记录存进 外部渠道/qq/微信/日志/, 重启后读得回."""
    async def scenario(ws, inbox, bot, base, cloud):
        await configure(ws, inbox, base, enabled=False)
        ch = bot.app[ENGINES].channels
        assert ch.state == "off"
        fake = FakeILink()
        wbase = await fake.start()
        bind(X.HOME, wbase)
        ch.weixin = X.WeixinClient(X.HOME, wbase, on_message=ch._on_weixin)
        try:
            ch.weixin.start()
            await until(lambda: ch.weixin.online, 5)
            fake.push("今天好累呀")
            await until(lambda: len(fake.sent) == 1, 10)
            await until(lambda: own_logs(tmp_path, "微信") and "第0句" in own_logs(tmp_path, "微信")[0].read_text(encoding="utf-8"), 5)
            return ch.store.root, list(bot.sent), fake.sent[0]["msg"]["item_list"][0]["text_item"]["text"]
        finally:
            await ch.weixin.close()
            await fake.close()

    root, sent, text = run_off(tmp_path, scenario, cloud_script=[say(reply_json(f"第{i}句")) for i in range(3)])
    assert sent == [] and text == "第0句" and root is not None
    lines = ChannelStore(root).tail(WEIXIN)  # 重启后新建的 store 读得回
    assert any("今天好累呀" in ln for ln in lines) and any("第0句" in ln for ln in lines)


def _manager(tmp_path, exclude):
    cfg = {"memory": {"vault": str(tmp_path / "vault"), "exclude": exclude},
           "channels": {"weixin": {"home": str(tmp_path / "wx")}}}
    return ChannelManager(cfg, SimpleNamespace(llm=None, memory=None))


def test_off_store_root_unit(tmp_path):
    """_apply(关着): 渠道日志的根目录建起来, 微信的行真落盘; 再 apply 一次不换 store 对象 (在途回复还拿着它)."""
    async def scenario():
        m = _manager(tmp_path, ["外部渠道"])
        assert m.store.root is None
        conf, warns = parse_conf({"enabled": False})
        await m._apply(conf, warns)
        first = m.store
        assert first.root == tmp_path / "vault" / "外部渠道" / "qq" and m._tick_task is not None
        first.append(WEIXIN, "我说的话")
        assert first.tail(WEIXIN) == ["我说的话"]
        await m._apply(*parse_conf({"enabled": False, "log_days": 7}))
        assert m.store is first and first.log_days == 7
        await m.stop()
        assert m._tick_task is None
    asyncio.run(scenario())


def test_off_store_bad_dir_is_warning(tmp_path):
    """目录没在 memory.exclude 里: QQ 关着只提醒、不报错 (以前关着从不报这个错); QQ 开着仍然抛 ConfigError."""
    async def scenario():
        m = _manager(tmp_path, [])
        conf, warns = parse_conf({"enabled": False})
        await m._apply(conf, warns)
        assert m.store.root is None and any("不落盘" in w for w in m.warnings)
        await m.stop()
        m2 = _manager(tmp_path, [])
        with pytest.raises(ConfigError):
            await m2._apply(*parse_conf({"enabled": True, "self_id": 20002, "token": "t"}))
        await m2.stop()
    asyncio.run(scenario())

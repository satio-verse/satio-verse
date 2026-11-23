"""QQ 上的重要消息整理汇报 (wm/channels/qqdigest.py; 2026-11-01 用户: "桌宠能不能自行读取邮箱内容、QQ重要消息呢？并根据邮箱内容
进行整理、汇报，垃圾邮箱会忽略之类的"): 分拣 (要紧 / 要看 / 普通 / 不管)、攒着、低打扰地说 (要紧的马上说、一小时最多 3 次; 要看的
隔够了一起说; 免打扰时只说要紧的; 你在群里说话了那个群就不说; 发不出去留着); 不调模型, 存盘不进 Obsidian 库. 不连真 QQ.
"""
import asyncio
import json
from datetime import datetime

import pytest

from test_channels import GA, GB, OWNER, SELF, cloud_model, gmsg, reply_json, run_qq, say, until
from wm.app import ENGINES
from wm.channels import qqdigest as QD
from wm.channels.manager import ConfigError, parse_conf

CONF = {"enabled": True, "report_gap_min": 120.0, "max_items": 8, "my_names": ["青哥"], "keywords": ["团建"],
        "important_from": {30009}, "ignore_from": {30666}, "ignore_groups": {333}}


def msg(text, uid=30001, **kw):
    return {"uid": uid, "text": text, "nick": "小李", "mid": "1", **kw}


def test_classify_rules():
    cases = [
        (msg(f"@{OWNER} 周五前把报告发我", at_owner=True), False, ("urgent", "@ 了你")),
        (msg("青哥在吗"), False, ("urgent", "提到你")),
        (msg("@绫音 帮我转告你主人明天别迟到"), False, ("urgent", "让我转告你")),
        (msg("跟你主人说一声周末聚餐改时间"), False, ("urgent", "让我转告你")),
        (msg("明天下午三点开家长会", at_all=True), False, ("important", "全体通知")),
        (msg("紧急：今晚之前交表", at_all=True), False, ("urgent", "全体通知, 说急")),
        (msg("下周三考试别忘了"), False, ("important", "说到考试")),
        (msg("这周末团建去哪"), False, ("important", "说到团建")),  # 你加的词
        (msg("［文件#1 报告.docx 1KB］", files=[("x",)]), False, ("important", "传了文件")),
        (msg("我跟你主人很熟的"), False, ("important", "问起你")),  # 不是让她转告
        (msg("哈哈哈 ［表情：笑］"), False, ("low", "")),
        (msg("［图片#3］"), False, ("low", "")),
        (msg("加微信领优惠券, 扫码进群"), False, ("ignored", "广告")),
        (msg("公益免费开户工具 直接调用底层函数获取户籍信息，100%查询成功并且数据完全正确 有需要提交"), False, ("ignored", "广告")),
        (msg("明天提交户籍证明的复印件"), False, ("important", "说到提交")),  # 正经通知不误伤
        (msg("今天开会", uid=30666), False, ("ignored", "你说不看的人")),
        (msg("哈哈", uid=30009), False, ("important", "你标了重要的人")),
        (msg("在吗"), True, ("important", "")),  # 好友私聊她的
        (msg("急事 速回"), True, ("urgent", "说急")),
    ]
    for m, private, want in cases:
        assert QD.classify(m, CONF, private) == want, (m["text"], QD.classify(m, CONF, private))


def test_snippet_masks_and_neutralizes():
    s = QD.snippet(f"@{OWNER} 电话 13812345678 邮箱 a.b@qq.com 看 https://x.cn/a 【表情包:开心】@全体成员", OWNER)
    assert s.startswith("@你 ") and "[手机号]" in s and "[邮箱]" in s and "[链接]" in s
    assert "【" not in s and "〔表情包:开心〕" in s and "全体成员" not in s
    assert len(QD.snippet("啊" * 300)) == QD.SNIPPET


def test_digest_flow(tmp_path):
    """攒、说、限次、免打扰、你在场、发不出去、并成一条、存盘."""
    async def run():
        state = tmp_path / "qq-digest.json"
        d = QD.Digest(state)
        sent = []
        ok = {"v": "chat+pet"}

        async def deliver(text, level):
            if ok["v"]:
                sent.append((level, text))
            return ok["v"]

        t = 1_793_000_000.0
        A, B = ("group", 111), ("group", 222)
        assert d.see(msg(f"@{OWNER} 周五前把报告发我 13812345678", at_owner=True), A, "群「测试群A」", CONF, OWNER, t) == "urgent"
        assert d.see(msg("明天下午三点开家长会", uid=30002, nick="王老师", at_all=True), A, "群「测试群A」", CONF, OWNER, t + 1) == "important"
        assert d.see(msg("加微信领优惠券"), A, "群「测试群A」", CONF, OWNER, t + 2) == "ignored"
        for i in range(5):
            assert d.see(msg(f"哈哈{i}"), B, "群「测试群B」", CONF, OWNER, t + 3 + i) == "low"
        for i in range(3):  # 同一句通知刷了三遍: 第三遍算刷屏
            d.see(msg("通知: 明天停水", uid=30004, nick=f"路人{i}"), A, "群「测试群A」", CONF, OWNER, t + 10 + i)
        assert d.see(msg("看看", uid=30005), ("group", 333), "群「不看的群」", CONF, OWNER, t + 20) == "ignored"
        assert [i["level"] for i in d.pending] == ["urgent", "important", "important", "important"]
        assert d.since == {"chat": {"群「测试群B」": 5}, "ignored": 3}
        assert json.loads(state.read_text(encoding="utf-8"))["pending"][0]["text"].startswith("@你 周五前")

        out = await d.flush(t + 60, False, CONF, deliver)
        assert [o["level"] for o in out] == ["urgent", "digest"]
        urgent, digest = sent[0][1], sent[1][1]
        assert urgent == "主人, 群「测试群A」里小李 @ 了你: 「@你 周五前把报告发我 [手机号]」"
        assert digest.startswith("主人~ QQ 上的消息我帮你看着") and "要看的有 3 条" in digest  # 第一份先说怎么帮他看
        assert "1. 群「测试群A」" in digest and "王老师: 明天下午三点开家长会 (全体通知)" in digest
        assert "各群闲聊: 群「测试群B」 5 条" in digest and "广告和刷屏 3 条我没管" in digest
        assert d.pending == [] and d.since == {"chat": {}, "ignored": 0}

        # 隔得不够不说; 隔够了说 (这回没有开场白); 同一个人半小时内又说的并成一条
        d.see(msg("下周三考试"), A, "群「测试群A」", CONF, OWNER, t + 100)
        d.see(msg("考试在三楼"), A, "群「测试群A」", CONF, OWNER, t + 200)
        assert len(d.pending) == 1 and d.pending[0]["n"] == 2
        assert await d.flush(t + 600, False, CONF, deliver) == []
        out = await d.flush(t + 60 + 7200, False, CONF, deliver)
        assert out[0]["text"].startswith("主人~ QQ 上有 1 条要看的:") and "(又说了 1 句)" in out[0]["text"]

        # 免打扰: 要看的攒着, 要紧的照说; 一小时最多单独说 3 次, 多的并进汇总
        t2 = t + 20000
        d.see(msg("报名截止到周五"), A, "群「测试群A」", CONF, OWNER, t2)
        for i in range(4):
            d.see(msg(f"@{OWNER} 第{i}件急事", uid=30010 + i, nick=f"同学{i}", at_owner=True), A, "群「测试群A」", CONF, OWNER, t2 + i)
        out = await d.flush(t2 + 10, True, CONF, deliver)
        assert [o["level"] for o in out] == ["urgent"] * 3
        assert [i["level"] for i in d.pending] == ["important", "urgent"]
        out = await d.flush(t2 + 20, False, CONF, deliver)  # 免打扰过了, 这一小时还是说够了: 第 4 件并进汇总
        assert [o["level"] for o in out] == ["digest"] and "1. 急: 群「测试群A」同学3" in out[0]["text"]

        # 发不出去: 留着; 你在那个群说话了: 那个群攒着的不再说
        ok["v"] = ""
        d.see(msg(f"@{OWNER} 在吗", at_owner=True), A, "群「测试群A」", CONF, OWNER, t2 + 9000)
        assert await d.flush(t2 + 9010, False, CONF, deliver) == [] and len(d.pending) == 1
        assert d.see(msg("收到", uid=OWNER), A, "群「测试群A」", CONF, OWNER, t2 + 9020) == "owner"
        assert d.pending == []

        # 存盘: 重启以后接着攒
        d.see(msg("明天放假"), B, "群「测试群B」", CONF, OWNER, t2 + 9100)
        again = QD.Digest(state)
        assert [i["text"] for i in again.pending] == ["明天放假"] and again.last_digest_at == d.last_digest_at

    asyncio.run(run())


def test_qq_digest_reports_to_owner(tmp_path):
    """走真的渠道 (替身协议端): 群里有人 @ 你 -> 私聊里马上跟你说 (桌宠对话栏也有); 开会通知攒着一起说, 广告不说;
    说了的记进她自己的日志 (不带原话); 你在群里说话了, 那个群攒着的不再说; 不调模型."""
    convs = [{"type": "group", "id": GA, "name": "测试群A", "trigger": "mention", "send": "auto"}]

    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        ch = bot.app[ENGINES].channels
        diary = []

        async def record(text):
            diary.append(text)

        ch._diary = record
        await bot.push(gmsg(GA, 30001, "周五前把报告发我", 1, at=OWNER, nick="小李"))
        await bot.push(gmsg(GA, 30002, "加微信领优惠券, 扫码进群", 2))
        await bot.push(gmsg(GA, 30003, "明天下午三点开会", 3, nick="王老师"))
        await until(lambda: len(ch.digest.pending) == 2 and ch.digest.since["ignored"] == 1, 5)
        await ch._maybe_digest(datetime.fromtimestamp(ch.clock()))
        await until(lambda: len([m for m in inbox.of("channel_notice") if m.get("kind") == "qq_report"]) >= 2, 5)
        notes = [m for m in inbox.of("channel_notice") if m.get("kind") == "qq_report"]
        to_owner = [p for a, p in bot.sent if a == "send_private_msg" and p["user_id"] == OWNER]
        await bot.push(gmsg(GA, 30004, "下周考试", 4))
        await until(lambda: len(ch.digest.pending) == 1, 5)
        await bot.push(gmsg(GA, OWNER, "收到", 5))
        await until(lambda: not ch.digest.pending, 5)
        return to_owner, notes, diary, ch.status()["own"]["digest"], len(cloud.requests) if cloud else 0

    to_owner, notes, diary, status, asked = run_qq(tmp_path, scenario, convs=convs)
    texts = ["".join(s["data"]["text"] for s in p["message"] if s["type"] == "text") for p in to_owner]
    assert texts[0] == "主人, 群「测试群A」里小李 @ 了你: 「@你 周五前把报告发我」"
    assert "王老师: 明天下午三点开会 (说到开会)" in texts[1] and "广告和刷屏 1 条我没管" in texts[1]
    assert [n["level"] for n in notes] == ["urgent", "digest"] and notes[0]["text"] == texts[0]
    assert diary == ["跟主人说了 QQ 上一条急的消息 (聊天里、桌宠上)", "把 QQ 上攒着要看的消息整理好跟主人说了 (聊天里、桌宠上)"]
    assert not any("周五前" in d or "开会" in d for d in diary)  # 日志里不带群里的原话
    assert status["pending"] == 0 and status["reports"] == 2 and asked == 0


def test_conv_can_turn_off_report_only():
    """会话里写 notify / digest / report 为 false 就只关汇报; 没写的照旧 (convs 里不多出键)."""
    conf, _ = parse_conf({"convs": [
        {"type": "group", "id": GA, "notify": False},
        {"type": "group", "id": GB, "digest": False},
        {"type": "group", "id": 333, "report": False},
        {"type": "group", "id": 444}]})
    assert conf["convs"][("group", GA)]["notify"] is False
    assert conf["convs"][("group", GB)]["digest"] is False
    assert conf["convs"][("group", 333)]["report"] is False
    assert "notify" not in conf["convs"][("group", 444)] and "digest" not in conf["convs"][("group", 444)]
    with pytest.raises(ConfigError):
        parse_conf({"convs": [{"type": "group", "id": GA, "notify": "false"}]})


def test_group_chats_without_reminding_owner(tmp_path):
    """大肥鱼群标成不汇报: @ 她照样回; 那个群的消息不进汇报, 也不占"广告和刷屏"的个数; 另一个群照常汇报."""
    convs = [{"type": "group", "id": GA, "name": "大肥鱼群", "trigger": "mention", "send": "auto", "notify": False},
             {"type": "group", "id": GB, "name": "另一个群", "trigger": "mention", "send": "auto"}]

    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "model": cloud_model(base), "convs": convs})
        await inbox.get("channel_config_done")
        ch = bot.app[ENGINES].channels

        async def no_heard(*_a, **_kw):
            return []

        ch.heard = no_heard
        await bot.push(gmsg(GA, 30001, "绫音来聊两句", 1, at=SELF, nick="小鱼"))
        await until(lambda: len(bot.sent) >= 1, 10)
        assert bot.texts()[0] == "来啦"
        assert ch.digest.pending == [] and ch.digest.since == {"chat": {}, "ignored": 0}
        # 另一个群的重要消息照常攒着、汇报; 大肥鱼群不出现, 也不贡献"广告和刷屏"的个数
        await bot.push(gmsg(GB, 30002, "明天下午三点开会", 2, nick="王老师"))
        await until(lambda: len(ch.digest.pending) == 1, 5)
        await ch._maybe_digest(datetime.fromtimestamp(ch.clock()))
        await until(lambda: any(m.get("kind") == "qq_report" for m in inbox.of("channel_notice")), 5)
        to_owner = [p for a, p in bot.sent if a == "send_private_msg" and p["user_id"] == OWNER]
        return to_owner, ch.digest.since

    to_owner, since = run_qq(tmp_path, scenario, cloud_script=[say(reply_json("来啦"))], convs=convs)
    text = "".join(s["data"]["text"] for s in to_owner[0]["message"] if s["type"] == "text")
    assert "另一个群" in text and "明天下午三点开会" in text
    assert "大肥鱼群" not in text and "条我没管" not in text
    assert since == {"chat": {}, "ignored": 0}


def test_mute_groups_in_config_survive_the_panel_snapshot(tmp_path):
    """2026-11-05 主人: "宝宝可以继续到大肥鱼群聊天, 但是不用发给我提醒了". 她加的 convs[].notify 开关只能从面板发来的
    convs 里打开, 面板上没这一项、convs 又是整份覆盖, 11-07 鱼塘群照样上了提醒. config 里的 digest.mute_groups
    不受面板快照影响: 面板发来的 convs 里没写 notify, 那个群也只聊天、不汇报; 另一个群照常."""
    convs = [{"type": "group", "id": GA, "name": "大肥鱼群", "trigger": "mention", "send": "auto"},
             {"type": "group", "id": GB, "name": "另一个群", "trigger": "mention", "send": "auto"},
             {"type": "group", "id": 333, "name": "面板上关了汇报的群", "trigger": "mention", "send": "auto", "notify": False}]
    conf, _ = parse_conf({"digest": {"mute_groups": [GA]}, "convs": convs})
    assert conf["digest"]["mute_groups"] == {GA}
    with pytest.raises(ConfigError):
        parse_conf({"digest": {"mute_groups": ["不是号"]}})

    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "model": cloud_model(base), "convs": convs})
        await inbox.get("channel_config_done")
        ch = bot.app[ENGINES].channels

        async def no_heard(*_a, **_kw):
            return []

        ch.heard = no_heard
        await bot.push(gmsg(GA, 30001, "明天下午三点开会", 1, nick="小鱼"))
        await bot.push(gmsg(GB, 30002, "明天下午四点开会", 2, nick="王老师"))
        await until(lambda: len(ch.digest.pending) == 1, 5)
        await asyncio.sleep(0.3)
        return [p["text"] for p in ch.digest.pending], ch.digest.since, ch.status()["own"]["digest"]

    pending, since, digest = run_qq(tmp_path, scenario, f"channels.qq.digest.mute_groups=[{GA}]", convs=convs)
    assert pending == ["明天下午四点开会"] and since == {"chat": {}, "ignored": 0}
    # 状态里分开写: 面板上关了的 / config 兜底的 (面板上鱼塘那行显示"开"、实际靠兜底不汇报时看得出来)
    assert digest["muted_panel"] == ["group:333"] and digest["muted_config"] == [f"group:{GA}"]

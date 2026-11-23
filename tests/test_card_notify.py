"""急3 (2026-11-15): 申请卡片没人点时的兜底.

  1. 卡片过了 ask_qq_after_s 没人点: 本机弹一条桌面通知 (一张卡片只弹一次, 隔内到点的攒成一条, 一次只一个在弹);
  2. 卡片末尾写明现在在哪里能批 (QQ 在线 / 不在线有微信 / 不在线没微信 / 只能在电脑上);
  3. 你在微信上一开口, 没列过的申请一次列出来; 回复里带编号只批对应的一张, 编号对不上或好几件没说是哪件一张都不批;
  4. 申请不过期、不打断.
桌面通知一律换成假函数, 不起真的 powershell.
"""
import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from test_channels import cloud_model, pmsg, reply_json, run_qq, until
from test_cloud import say
from test_life_alone import run_off
from test_weixin import FakeILink, bind
from wm.app import ENGINES
from wm.channels import manager as M
from wm.channels import weixin as X

FAST = {"ask_qq_after_s": 0.05}


def texts(fake):
    return [m["msg"]["item_list"][0]["text_item"]["text"] for m in fake.sent]


def lists(fake):
    """发给微信的"等你点头"列表 (她自己的回话也会发到微信, 不算)."""
    return [t for t in texts(fake) if t.startswith("主人~ 有 ")]


async def ask(ch, key, title, text="要不要做这一件事", got=None):
    got = got if got is not None else []
    did = await ch.agent_ask(key, title, text, lambda ok, key=key: got.append((key, ok)))
    return did, got


async def wx_up(ch, tmp_path):
    """放一个绑好的假微信, 收得到你的话."""
    fake = FakeILink()
    wbase = await fake.start()
    home = tmp_path / "wx"
    bind(home, wbase)
    ch.weixin = X.WeixinClient(home, wbase, on_message=ch._on_weixin)
    ch.weixin.start()
    await until(lambda: ch.weixin.online, 5)
    return fake


async def wx_down(ch, fake):
    await ch.weixin.close()
    await fake.close()


def recorder(ch):
    calls = []

    async def fake(title, body):
        calls.append((title, body))
        return True
    ch.desk_notice = fake
    return calls


def off(tmp_path, scenario, script=None, **extra):
    """QQ 关着 (有记忆库), 默认没配云端模型: 答复只认整句"好 / 不用" (带编号的先拿掉编号). script: 给个假云端 (判断答复用)."""
    async def inner(ws, inbox, bot, base, cloud):
        model = {"model": cloud_model(base)} if script else {}
        await ws.send_json({"type": "channel_config", "enabled": False, **model, **{**FAST, **extra}})
        await inbox.get("channel_config_done")
        return await scenario(bot.app[ENGINES].channels, ws, inbox, bot)
    return run_off(tmp_path, inner, cloud_script=script)


# ---------------------------------------------------------------------------
# 验收 1: 本机通知
# ---------------------------------------------------------------------------
def test_remind_once_after_wait(tmp_path, monkeypatch):
    """到点前 0 次, 到点后恰好 1 次; QQ 没路时 _ask_on_qq 每隔几秒重试, 不再弹."""
    monkeypatch.setattr(M, "ASK_RETRY_S", 0.05)

    async def scenario(ch, ws, inbox, bot):
        calls = recorder(ch)
        did, _ = await ask(ch, "k1", "工作台想跑命令")
        assert calls == []
        await until(lambda: len(calls) == 1, 3)
        await asyncio.sleep(0.6)  # 重试跑了十几圈
        return calls, ch.drafts[did].get("reminded"), ch.drafts[did].get("code")

    calls, reminded, code = off(tmp_path, scenario)
    assert len(calls) == 1 and reminded is True and code is None
    assert "工作台想跑命令" in calls[0][1] and "等你点头" in calls[0][0]


def test_remind_batches_gap_and_own_timer(tmp_path, monkeypatch):
    """第一条马上弹; 间隔内到点的两张不会各弹一条, 而是攒成一条, 靠自己的计时器到点一定弹出来; 之后没有第三条."""
    monkeypatch.setattr(M, "REMIND_GAP_S", 0.5)

    async def scenario(ch, ws, inbox, bot):
        calls = recorder(ch)
        await ask(ch, "k1", "第一件事")
        await until(lambda: len(calls) == 1, 3)
        await ask(ch, "k2", "第二件事")
        await ask(ch, "k3", "第三件事")
        await asyncio.sleep(0.25)  # 两张都到点了, 但还在间隔里: 没弹, 有自己的计时器
        waiting = (len(calls), ch._remind_timer is not None, len(ch._remind_wait))
        await until(lambda: len(calls) == 2, 3)
        await asyncio.sleep(0.8)
        return waiting, calls

    waiting, calls = off(tmp_path, scenario)
    assert waiting == (1, True, 2)
    assert len(calls) == 2 and "第二件事" in calls[1][1] and "第三件事" in calls[1][1] and "2 件" in calls[1][0]


def test_remind_one_powershell_at_a_time(tmp_path, monkeypatch):
    """上一条还没弹完 (powershell 还在), 新到点的并进下一批, 不叠进程; 放开以后补上."""
    monkeypatch.setattr(M, "REMIND_GAP_S", 0.0)

    async def scenario(ch, ws, inbox, bot):
        gate, live, peak, calls = asyncio.Event(), [0], [0], []

        async def slow(title, body):
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            calls.append(body)
            await gate.wait()
            live[0] -= 1
            return True
        ch.desk_notice = slow
        await ask(ch, "k1", "甲")
        await until(lambda: len(calls) == 1, 3)
        await ask(ch, "k2", "乙")
        await asyncio.sleep(0.3)
        mid = len(calls)
        gate.set()
        await until(lambda: len(calls) == 2, 3)
        return mid, peak[0], calls

    mid, peak, calls = off(tmp_path, scenario)
    assert mid == 1 and peak == 1 and "乙" in calls[1]


def test_remind_skips_answered_paused_and_zero(tmp_path):
    async def scenario(ch, ws, inbox, bot):
        calls = recorder(ch)
        did, _ = await ask(ch, "k1", "点掉的")
        await ch.agent_ask_done(did, "answered")  # 到点前在电脑上点了
        await ask(ch, "k2", "暂停时的")
        ch.paused = True
        await asyncio.sleep(0.4)
        return calls

    assert off(tmp_path, scenario) == []

    async def scenario0(ch, ws, inbox, bot):
        calls = recorder(ch)
        await ask(ch, "k1", "不找人")
        await asyncio.sleep(0.4)
        return calls

    assert off(tmp_path / "z", scenario0, ask_qq_after_s=0) == []

    async def note0(ch, ws, inbox, bot):
        did, _ = await ask(ch, "k1", "不找人", text="原文")
        return ch.drafts[did]["msg"]["text"]
    assert off(tmp_path / "y", note0, ask_qq_after_s=0) == "原文"  # 找人整个关了: 卡片不加提示行


def test_remind_remote_false_still_local(tmp_path):
    """命令太长 (>300 字) 不发微信/QQ, 本机照样提醒; 卡片上写"只能在电脑上批"."""
    async def scenario(ch, ws, inbox, bot):
        calls = recorder(ch)
        did, _ = await ask(ch, "k1", "跑一条很长的命令", text="x" * 400)
        await until(lambda: len(calls) == 1, 3)
        return calls, ch.drafts[did]["code"] if "code" in ch.drafts[did] else None, ch.drafts[did]["msg"]["text"], list(bot.sent)

    calls, code, text, sent = off(tmp_path, scenario)
    assert len(calls) == 1 and code is None and sent == [] and text.endswith("这一件只能在电脑上批。")


def test_remind_failure_is_quiet(tmp_path):
    async def scenario(ch, ws, inbox, bot):
        n = [0]

        async def boom(title, body):
            n[0] += 1
            raise RuntimeError("弹不出来")
        ch.desk_notice = boom
        did, _ = await ask(ch, "k1", "甲")
        await until(lambda: n[0] == 1, 3)
        await asyncio.sleep(0.4)
        return n[0], ch.drafts[did].get("reminded"), [m["kind"] for m in inbox.of("channel_notice")], ch._remind_busy

    n, reminded, notices, busy = off(tmp_path, scenario)
    assert n == 1 and reminded is True and "send_failed" not in notices and busy is False


def test_desknotice_command_is_local_and_safe(monkeypatch):
    from wm.channels import desknotice as DN
    evil = "x' ; $(calc) \"<&>\u0000 http://evil.example/a"
    argv, env = DN.build(evil, evil)
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert "evil" not in " ".join(argv) and "calc" not in " ".join(argv)  # 正文不在命令行里
    assert "http" not in script.lower() and "Invoke-" not in script and "Net." not in script  # 不碰网络
    assert "InnerText = $env:" in script and "\x00" not in env[DN.TEXT_ENV] and "calc" in env[DN.TEXT_ENV]
    assert "-WindowStyle" in argv and "Hidden" in argv

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-123")
    seen = {}

    def fake_run(a, **kw):
        seen.update(kw, argv=a)
        return SimpleNamespace(returncode=seen.get("rc", 0))
    monkeypatch.setattr(DN.subprocess, "run", fake_run)
    assert asyncio.run(DN.show("甲", "乙")) is True
    assert seen["creationflags"] == 0x08000000 and seen["timeout"] == DN.TIMEOUT_S  # 不闪黑窗, 有超时
    assert seen["env"][DN.TITLE_ENV] == "甲" and seen["env"][DN.TEXT_ENV] == "乙"
    assert "sk-secret-123" not in " ".join(seen["env"].values()) and "DEEPSEEK_API_KEY" not in seen["env"]  # 不把模型 key 带给 powershell
    assert any(k.lower() in ("path", "systemroot") for k in seen["env"])
    seen["rc"] = 1
    assert asyncio.run(DN.show("甲", "乙")) is False

    def timeout(a, **kw):
        raise DN.subprocess.TimeoutExpired(a, 1)
    monkeypatch.setattr(DN.subprocess, "run", timeout)
    assert asyncio.run(DN.show("甲", "乙")) is False
    monkeypatch.setattr(DN.subprocess, "run", fake_run)
    monkeypatch.setattr(DN, "sys", SimpleNamespace(platform="linux"))
    seen.clear()
    assert asyncio.run(DN.show("甲", "乙")) is False and not seen  # 非 Windows 不起进程


def test_desknotice_silent_under_pytest(monkeypatch):
    from wm.channels import desknotice as DN
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    monkeypatch.setattr(DN.subprocess, "run", lambda *a, **k: pytest.fail("pytest 里不该起进程"))
    assert asyncio.run(DN.show("甲", "乙")) is False


def test_default_notifier_only_on_formal_server(tmp_path, monkeypatch):
    from wm.channels import desknotice as DN

    async def scenario(ws, inbox, bot, base, cloud):
        return bot.app[ENGINES].channels.desk_notice
    assert run_qq(tmp_path / "a", scenario, vault=True) is None  # 没有 WM_ASKS_FILE: 测试服务端不弹
    monkeypatch.setenv("WM_ASKS_FILE", str(tmp_path / "asks.json"))
    assert run_qq(tmp_path / "b", scenario, "mail.run=off", vault=True) is DN.show


# ---------------------------------------------------------------------------
# 验收 2: 卡片上写明现在在哪里能批
# ---------------------------------------------------------------------------
def test_card_note_qq_online(tmp_path):
    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "ask_qq_after_s": 600})
        await inbox.get("channel_config_done")
        ch = bot.app[ENGINES].channels
        did, _ = await ask(ch, "k1", "工作台想跑命令", text="要跑 dir")
        card = await inbox.get("channel_draft")
        return card["text"], ch.drafts[did]["plain"]

    text, plain = run_qq(tmp_path, scenario)
    assert text.startswith("要跑 dir\n\n") and plain == "要跑 dir"
    assert "600 秒没人点会弹桌面通知" in text and "发 QQ" in text and "在电脑上点随时有效" in text
    assert "QQ 现在没连着" not in text


def test_card_note_offline_with_and_without_weixin(tmp_path):
    async def with_wx(ch, ws, inbox, bot):
        fake = await wx_up(ch, tmp_path)
        try:
            did, _ = await ask(ch, "k1", "甲")
            return ch.drafts[did]["msg"]["text"]
        finally:
            await wx_down(ch, fake)
    text = off(tmp_path / "a", with_wx)
    assert text.endswith("QQ 现在没连着，只能在电脑上或微信上批。没人点会弹桌面通知；"
                         "你在微信上跟她说一句话，等你点头的事会一起列给你。")

    async def no_wx(ch, ws, inbox, bot):
        did, _ = await ask(ch, "k1", "甲")
        return ch.drafts[did]["msg"]["text"]
    text = off(tmp_path / "b", no_wx)
    assert text.endswith("QQ 现在没连着，只能在电脑上批。没人点会弹桌面通知。") and "微信" not in text.split("\n\n")[-1]


def test_card_note_refreshed_at_remind_and_not_sent_to_remote(tmp_path):
    """创建时 QQ 在线、到点前掉线: 重发的卡片提示行变成"没连着"; 发到 QQ 的话里没有提示行."""
    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "ask_qq_after_s": 0.3})
        await inbox.get("channel_config_done")
        ch = bot.app[ENGINES].channels
        did, _ = await ask(ch, "k1", "甲", text="原文一句")
        first = (await inbox.get("channel_draft"))["text"]
        ch.online = False
        await until(lambda: ch.drafts[did]["msg"]["text"] != first, 3)
        ch.online = True
        await ch._ask_on_qq(did)  # QQ 回来以后 _ask_on_qq 发 QQ
        await until(lambda: bot.texts(), 3)
        return first, ch.drafts[did]["msg"]["text"], bot.texts()

    first, second, sent = run_qq(tmp_path, scenario)
    assert "发 QQ" in first and "QQ 现在没连着" in second
    assert any("原文一句" in t for t in sent) and not any("桌面通知" in t or "只能在电脑上" in t for t in sent)


# ---------------------------------------------------------------------------
# 验收 3: 微信一开口列积压, 答复批到对应的卡片
# ---------------------------------------------------------------------------
def test_wx_backlog_lists_once_and_never_expires(tmp_path):
    async def scenario(ch, ws, inbox, bot):
        fake = await wx_up(ch, tmp_path)
        try:
            d1, _ = await ask(ch, "k1", "甲甲甲")
            d2, _ = await ask(ch, "k2", "乙乙乙")
            fake.push("在吗")
            await until(lambda: len(lists(fake)) == 1, 10)
            await until(lambda: ch.drafts[d1].get("wx_sent") and ch.drafts[d2].get("wx_sent"), 5)
            first = lists(fake)[0]
            fake.push("还在吗")
            await asyncio.sleep(0.8)
            again = len(lists(fake))
            d3, _ = await ask(ch, "k3", "丙丙丙")
            fake.push("再说一句")
            await until(lambda: len(lists(fake)) == 2, 10)
            second = lists(fake)[1]
            return (first, again, second, ch.drafts[d1].get("wx_sent"), isinstance(ch.drafts[d1]["timer"], M._NoExpiry),
                    {d1, d2, d3} <= set(ch.drafts), [m for m in inbox.of("channel_draft_done")])
        finally:
            await wx_down(ch, fake)

    first, again, second, sent, noexp, alive, done = off(tmp_path, scenario, ask_qq_after_s=600)
    assert "2 件" in first and "#A1 甲甲甲" in first and "#A2 乙乙乙" in first and "好 #A1" in first
    assert again == 1  # 列过的不重复列
    assert "丙丙丙" in second and "甲甲甲" not in second and "乙乙乙" not in second and "1 件" in second
    assert sent is True and noexp and alive and done == []  # 不过期、不打断


def test_wx_answer_goes_to_the_named_card(tmp_path):
    """从真实入口: 微信上回"好 #A2" -> 只批 A2; 批了的下次不再出现."""
    async def scenario(ch, ws, inbox, bot):
        fake = await wx_up(ch, tmp_path)
        got = []
        try:
            for k in ("k1", "k2", "k3"):
                await ask(ch, k, f"事-{k}", got=got)
            fake.push("在吗")
            await until(lambda: len(lists(fake)) == 1, 10)
            fake.push("好 #A2")
            await until(lambda: ("k2", True) in got, 10)
            await until(lambda: all(d["key"][1] != "k2" for d in ch.drafts.values()), 5)
            await ask(ch, "k4", "事-k4", got=got)
            fake.push("再来")
            await until(lambda: len(lists(fake)) == 2, 10)
            return got, lists(fake)[1], sorted(d["key"][1] for d in ch.drafts.values())
        finally:
            await wx_down(ch, fake)

    got, second, left = off(tmp_path, scenario, ask_qq_after_s=600)
    assert got == [("k2", True)]
    assert "事-k4" in second and "事-k2" not in second and "事-k1" not in second
    assert left == ["k1", "k3", "k4"]


def test_answer_wrong_target_cases(tmp_path):
    """带编号只批那一张; 编号对不上一张都不批; 好几张在等又没带编号一张都不批; 只有一张在等时照旧."""
    async def scenario(ch, ws, inbox, bot):
        got = []
        for k in ("k1", "k2", "k3"):
            await ask(ch, k, f"事-{k}", got=got)
            ch.drafts[next(reversed(ch.drafts))]["code"] = f"A{k[1]}"
        n1 = await ch.heard("好 #A9", "微信上", True)
        n2 = await ch.heard("好", "微信上", True)
        n3 = await ch.heard("不用 #A2", "微信上", True)
        n4 = await ch.heard("好的 ＃a1", "微信上", True)
        after_named = list(got)
        # 只剩 A3 一张: 不带编号照旧批它
        n5 = await ch.heard("好", "微信上", True)
        return n1, n2, n3, n4, after_named, got, n5

    n1, n2, n3, n4, after_named, got, n5 = off(tmp_path, scenario)
    assert "#A9" in n1[0] and "#A1" in n1[0] and "一件都没批" in n1[0]
    assert "3 件" in n2[0] and "一件都没批" in n2[0]
    assert after_named == [("k2", False), ("k1", True)]  # 不用 #A2 -> 拒绝 A2; 好的 ＃a1 -> 同意 A1 (全角、小写也认)
    assert got[-1] == ("k3", True) and "同意" in n5[0]


def test_explicit_code_beats_model_guess(tmp_path):
    """云端模型把"好 #A1"判成了 A3: 以话里写的编号为准, 批 A1 不批 A3."""
    verdict = json.dumps({"answer": "yes", "code": "A3", "do": "none", "topic": "", "name": "", "purpose": "",
                          "reasonable": "no"}, ensure_ascii=False)

    async def scenario(ch, ws, inbox, bot):
        got = []
        for k in ("k1", "k2", "k3"):
            await ask(ch, k, f"事-{k}", got=got)
            ch.drafts[next(reversed(ch.drafts))]["code"] = f"A{k[1]}"
        await ch.heard("好 #A1", "微信上", True)
        return got

    assert off(tmp_path, scenario, script=[say(verdict)] * 3) == [("k1", True)]


def test_restored_cards_get_reminded_after_config(tmp_path):
    """服务刚起 (conf 还是 None) 放回来的申请: 配置第一次到位后到点弹一次通知, 卡片补上"在哪批"那一行."""
    async def inner(ws, inbox, bot, base, cloud):
        ch = bot.app[ENGINES].channels
        assert ch.conf is None
        ch.asks_file = tmp_path / "asks.json"
        plain = "要不要接着写"
        msg = {"type": "channel_draft", "channel": ch.channel, "id": "d7", "kind": "mcp_more",
               "conv": {"type": "ask", "id": 0, "name": "t"}, "ask": {"title": "她写「甲」超预算了", "ok": "接着写", "no": "不用"},
               "reply_to": None, "text": plain, "parts": [plain], "expires_s": None}
        ch.asks_file.write_text(json.dumps([{"id": "d7", "key": ["mcp", "a"], "kind": "mcp_more", "msg": msg, "qq": "", "what": "接着写",
                                             "code": None, "plain": plain, "remote": True, "mcp": "a"}]), encoding="utf-8")
        back = ch.restore_asks()
        d = ch.drafts["d7"]
        before = (back, d.get("resume"), d["msg"]["text"])
        calls = recorder(ch)
        await asyncio.sleep(0.3)
        quiet = len(calls)  # 配置没到位: 不排
        await ws.send_json({"type": "channel_config", "enabled": False, **FAST})
        await inbox.get("channel_config_done")
        await until(lambda: len(calls) == 1, 5)
        await asyncio.sleep(0.4)
        return before, quiet, calls, d["msg"]["text"], d.get("resume")

    before, quiet, calls, text, resume = run_off(tmp_path, inner)
    assert before == (1, True, "要不要接着写") and quiet == 0
    assert len(calls) == 1 and "甲" in calls[0][1]
    assert text.startswith("要不要接着写\n\n") and "QQ 现在没连着" in text and resume is None


def test_wx_backlog_not_for_others(tmp_path):
    """QQ 陌生人私聊、群里 @ 她、自检没过、暂停: 微信上一条都没发."""
    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "model": cloud_model(base), "ask_qq_after_s": 600})
        await inbox.get("channel_config_done")
        ch = bot.app[ENGINES].channels
        fake = await wx_up(ch, tmp_path)
        try:
            await ask(ch, "k1", "甲")
            await bot.push(pmsg(4242, "你好呀", 11))
            await bot.push({**pmsg(4242, "@她", 12), "message_type": "group", "group_id": 100001,
                            "message": [{"type": "at", "data": {"qq": "999"}}, {"type": "text", "data": {"text": "在吗"}}]})
            await asyncio.sleep(1.0)
            stranger = list(fake.sent)
            conv = ch._conv(("private", 4242))
            await ch._wx_backlog(conv)  # 直接叫也不列: 不是微信那个主人会话
            ch.isolation_failed = True
            fake.push("在吗")
            await asyncio.sleep(0.8)
            failed = list(fake.sent)
            ch.isolation_failed = False
            ch.paused = True
            fake.push("在吗")
            await asyncio.sleep(0.8)
            return stranger, failed, list(fake.sent), ch.ignored
        finally:
            await wx_down(ch, fake)

    stranger, failed, paused, ignored = run_qq(tmp_path, scenario, cloud_script=[say(reply_json("嗯"))] * 8, vault=True)
    assert stranger == [] and failed == [] and paused == [] and ignored >= 2


def test_wx_backlog_skips_far_and_non_ask(tmp_path):
    async def scenario(ch, ws, inbox, bot):
        fake = await wx_up(ch, tmp_path)
        try:
            ch.drafts["dx"] = {"key": ("private", 7), "parts": ["hi"], "quote": None, "timer": SimpleNamespace(cancel=lambda: None),
                               "msg": {"type": "channel_draft", "text": "hi", "conv": {"type": "private", "id": 7, "name": "某人"}}}
            far, _ = await ask(ch, "kf", "很长的一件", text="x" * 400)
            fake.push("在吗")
            await asyncio.sleep(1.0)
            none_yet = len(lists(fake))  # 只有普通草稿和要在电脑上看全的: 不列
            near, _ = await ask(ch, "kn", "近的一件")
            fake.push("再说")
            await until(lambda: len(lists(fake)) == 1, 10)
            return none_yet, lists(fake)[0], ch.drafts[far].get("wx_sent"), ch.drafts["dx"].get("wx_sent")
        finally:
            await wx_down(ch, fake)

    none_yet, text, far_sent, plain_sent = off(tmp_path, scenario, ask_qq_after_s=600)
    assert none_yet == 0 and "1 件" in text and "近的一件" in text and "另有 1 件要在电脑上看全了才能批" in text and "很长的一件" not in text
    assert not far_sent and not plain_sent


def test_wx_backlog_send_failure_lists_again(tmp_path):
    async def scenario(ch, ws, inbox, bot):
        fake = await wx_up(ch, tmp_path)
        try:
            did, _ = await ask(ch, "k1", "甲")
            real, n = ch.weixin.send_text, [0]

            async def flaky(t):
                if t.startswith("主人~ 有 "):
                    n[0] += 1
                    if n[0] == 1:  # 列表第一次发不出去
                        raise X.WeixinError("断了一下")
                return await real(t)
            ch.weixin.send_text = flaky
            fake.push("在吗")
            await until(lambda: n[0] >= 1, 10)
            await asyncio.sleep(0.5)
            failed = (ch.drafts[did].get("wx_sent"), ch.drafts[did].get("code"), len(lists(fake)))
            fake.push("再说一句")
            await until(lambda: len(lists(fake)) == 1, 10)
            await until(lambda: ch.drafts[did].get("wx_sent"), 5)  # 发出去以后才标
            return failed, ch.drafts[did].get("wx_sent"), lists(fake)[0]
        finally:
            await wx_down(ch, fake)

    failed, sent, text = off(tmp_path, scenario, ask_qq_after_s=600)
    assert failed == (None, None, 0) and sent is True and "甲" in text and "#A" in text


def test_wx_sent_persists_across_restore(tmp_path):
    async def scenario(ch, ws, inbox, bot):
        ch.asks_file = tmp_path / "asks.json"
        fake = await wx_up(ch, tmp_path)
        try:
            did = await ch._ask_card("mcp_more", ("mcp", "a"), "她写「甲」超预算了", "要不要接着写", "接着写", "不用",
                                     qq="我写「甲」超预算了", what="接着写「甲」", mcp="a")
            fake.push("在吗")
            await until(lambda: len(lists(fake)) == 1, 10)
            await until(lambda: ch.drafts[did].get("wx_sent"), 5)  # 标了就同步存盘
            row = json.loads(ch.asks_file.read_text(encoding="utf-8"))[0]
            ch.drafts.clear()
            back = ch.restore_asks()
            d = ch.drafts[did]
            fake.push("再说")
            await asyncio.sleep(0.8)
            return row, back, d["msg"]["text"], d.get("wx_sent"), len(lists(fake))
        finally:
            await wx_down(ch, fake)

    row, back, text, sent, n = off(tmp_path, scenario, ask_qq_after_s=600)
    assert row["wx_sent"] is True and row["plain"] == "要不要接着写" and back == 1
    assert text == "要不要接着写" and sent is True and n == 1  # 放回来的卡片文字是原文 (提示行到点再写), 不重列

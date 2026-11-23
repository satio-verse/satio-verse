"""保管箱和渠道管理器的接线: 真的 create_app + 替身 OneBot / 替身 B 站 (沿用 test_channels.py 的那一套); 不连真的 QQ、B 站、模型.
B 站 Cookie 在保管箱里时: 读字幕走 read 用途 (点过头才给, 没点头就弹申请卡片), 过期了提醒去保管箱页面扫码."""
import json

from test_channels import FakeBili, cloud_model, reply_json, run_qq
from test_cloud import say
from wm.app import ENGINES
from wm.channels import bili
from wm.channels import bili_login as BL
from wm.channels import vault as V
from wm.channels.manager import parse_conf

COOKIE = "buvid3=B3x; SESSDATA=SessCanary88; bili_jct=JctCanary77; DedeUserID=1"


def watch_once(tmp_path, prepare, notice_kind=None, card=False):
    cookie_file = tmp_path / "bili_cookie.txt"
    cookie_file.write_text(BL.marker_text(), encoding="utf-8")  # 保管箱的记号, 不是 Cookie

    async def scenario(ws, inbox, bot, base, cloud):
        fake = FakeBili()
        url = await fake.start()
        try:
            await inbox.get("channel_status", lambda m: m["state"] == "online")
            ch = bot.app[ENGINES].channels
            prepare(ch.vault)
            await ws.send_json({"type": "channel_config", "model": cloud_model(base), "bili_base": url,
                                "bili_cookie_file": str(cookie_file), "private": {"min_gap_s": 0}})
            await inbox.get("channel_config_done")
            await ws.send_json({"type": "channel_own", "action": "watch"})
            done = await inbox.get("channel_own_done", timeout=15)
            notice = await inbox.get("channel_notice", lambda m: m["kind"] == notice_kind) if notice_kind else None
            draft = await inbox.get("channel_draft", lambda m: m.get("kind") == "agent_ask") if card else None
            before = ch.vault.get("bilibili")["purposes"][0]["state"] if card else None
            if draft:  # 主人点了"允许" (真的流程里是对话栏上点, 或者 QQ / 微信上回"好"): 要在服务端还开着的时候答
                await ch.review({"id": draft["id"], "action": "send"})
            return done, dict(ch.status()["own"]), fake.hits, notice, (draft, before), ch
        finally:
            await fake.close()

    script = [say(reply_json("UP 主教猫做蛋炒饭, 热评在笑饭糊了")), say(reply_json("猫猫做饭也太可爱了"))]
    return run_qq(tmp_path, scenario, vault=True, cloud_script=script)


def test_vault_cookie_reads_subtitles_once_the_owner_allowed_it(tmp_path):
    def prepare(vault):
        vault.put("bilibili", {"cookie": COOKIE}, purposes={"read": True})
    done, own, hits, _, _, ch = watch_once(tmp_path, prepare)
    assert done["decision"] == "life" and own["subtitles"] == "ok"
    sent = [(p, c) for p, c in hits if c is not None]
    assert ("/x/player/wbi/v2", "SESSDATA=SessCanary88; buvid3=B3x") in sent  # 和 Cookie 文件时发出去的一样, 只带这两个
    assert all("JctCanary77" not in (c or "") for _, c in hits)  # bili_jct 还是不带
    use = [r for r in ch.vault.log_tail(50) if r["kind"] == "use"]
    assert use and use[-1]["purpose"] == "read" and use[-1]["by"] == "bili" and use[-1]["ok"] is True
    assert "SessCanary88" not in (ch.vault.home / "log.jsonl").read_text(encoding="utf-8")
    assert bili._vault is None or bili._vault is ch.vault  # 测试结束 conftest 会还原


def test_undecided_use_asks_the_owner_with_a_card_and_reads_nothing_meanwhile(tmp_path):
    def prepare(vault):
        vault.put("bilibili", {"cookie": COOKIE})  # 用途还没点头
    done, own, hits, _, (draft, before), ch = watch_once(tmp_path, prepare, card=True)
    assert own["subtitles"] == "no_cookie" and all("SessCanary88" not in (c or "") for _, c in hits)
    assert draft["ask"]["title"] == "她想用保管箱里的「B 站（她的号）」" and "看视频时读字幕" in draft["text"] and draft["ask"]["ok"] == "允许"
    assert "SessCanary88" not in json.dumps(draft, ensure_ascii=False)
    assert before == "ask"  # 没点头之前一直是"先问"
    assert ch.vault.get("bilibili")["purposes"][0]["state"] == "allow" and ch.vault.get("bilibili")["purposes"][0]["by"] == "card"


def test_expired_vault_login_points_to_the_vault_page_and_is_marked(tmp_path):
    def prepare(vault):
        vault.put("bilibili", {"cookie": "SESSDATA=SessStale11"}, purposes={"read": True})
    done, own, hits, notice, _, ch = watch_once(tmp_path, prepare, notice_kind="bili_login")
    assert own["subtitles"] == "login_expired" and "过期" in notice["text"] and "/vault" in notice["text"] and "扫码登录" in notice["text"]
    assert ch.vault.get("bilibili")["flags"]["expired_at"]  # 保管箱页面上显示"登录过期了"
    assert "SessStale11" not in notice["text"]


def test_manager_builds_the_vault_and_the_launcher_uses_it(tmp_path):
    async def scenario(ws, inbox, bot, base, cloud):
        ch = bot.app[ENGINES].channels
        return ch.vault, ch.vault_bridge, ch.snowluma, ch.vault.home, bili._vault is ch.vault
    vault, bridge, launcher, home, wired = run_qq(tmp_path, scenario)
    assert launcher.vault is vault and launcher.bridge is bridge and bridge.vault is vault and vault.on_want == bridge.want
    assert home == V.HOME and wired


def test_attach_setting_defaults_on_and_can_be_turned_off():
    assert parse_conf({})[0]["snowluma_attach"] is True
    assert parse_conf({"snowluma_attach": False})[0]["snowluma_attach"] is False

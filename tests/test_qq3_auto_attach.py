"""QQ3 (2026-11-16): 去掉 config/local.json 里 `"snowluma_attach": false` 以后, 她自己挂接 QQ 这条路在现在的代码上走不走得通, 以及
"不会在用户不知情时反复试密码". 全部用替身: 假的 SnowLuma 管理页 (本机临时端口, tests/vault_fakes.py)、临时目录里的假保管箱;
不启动真的 SnowLuma、不登录 QQ、不用真密码、不弹真的桌面通知 (pytest 里 desknotice.show 不起进程, 这里换成记录器).

两组:
- 真入口: 真 ChannelManager.configure (面板消息) -> 真 QQAdapter -> 真 OneBotClient 的重连循环 -> before_connect -> 真启动器.
  文件配置里不带 snowluma_attach, 就是用户删了那一行以后的样子.
- 启动器层: 直接调 launcher.ensure, 用假时钟推进, 数假管理页收到几次 /api/login.
"""
import asyncio
import contextlib
import json
import time

import pytest
from aiohttp import web

import vault_fakes as F
from test_snowluma_attach import PW, UIN, async_test, mine, world
from wm.channels import selfcheck
from wm.channels import snowluma as S
from wm.channels import snowluma_hook as H
from wm.channels import vault as V
from wm.channels.manager import ChannelManager
from wm.channels.onebot import OneBotClient
from types import SimpleNamespace

OWNER = 10000


def logins(fake) -> int:
    return len([c for c in fake.calls if c == ("POST", "/api/login")])


async def until(pred, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


def count_rounds(mgr) -> list:
    """数重连循环真转了几圈 (每圈开头 OneBotClient 调 before_connect -> _ensure_snowluma -> launcher.ensure)."""
    box = [0]
    real = mgr.snowluma.ensure

    async def counted(conf):
        box[0] += 1
        return await real(conf)
    mgr.snowluma.ensure = counted
    return box


async def spin(mgr, n=10) -> int:
    """等重连循环再转 n 圈 (转没转起来要断言, 不能只睡一会儿: 连没人听的端口在 Windows 上每次要约 2 秒才报错)."""
    first = mgr.rounds[0]
    assert await until(lambda: mgr.rounds[0] >= first + n, 10.0), f"重连循环没转起来 (只转了 {mgr.rounds[0] - first} 圈)"
    return mgr.rounds[0] - first


# -- 真入口 -----------------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def rig(tmp_path, monkeypatch, *, procs=None, password=PW, saved=PW, allow=True, base_qq=None, **fake_kw):
    """一台"用户的电脑": 假管理页 + 一个真 ChannelManager (保管箱在 conftest 换过的临时目录里). 返回 w; w.new_manager() 等于重启 8790."""
    fake = F.FakeSnowLuma(password=password, procs=procs if procs is not None else [mine()])
    for k, v in fake_kw.items():
        setattr(fake, k, v)
    runner, port = await F.serve(fake.app())
    install = tmp_path / "SnowLuma"
    (install / "config").mkdir(parents=True)
    (install / "node.exe").write_bytes(b"fake")
    (install / "index.mjs").write_text("// fixture")
    (install / "config" / "consent.json").write_text(json.dumps({"version": "fixture", "acceptedAt": "2026-11-07"}))

    async def listening(_endpoint):
        return fake.onebot_up
    monkeypatch.setattr(S, "_listening", listening)
    monkeypatch.setattr(S, "_existing_process", lambda _d: 4242)
    monkeypatch.setattr(S, "_spawn", lambda _d: pytest.fail("已经有 SnowLuma 在跑, 不该再起"))
    monkeypatch.setattr(H, "qq_main_pids", lambda: [p["pid"] for p in fake.procs])

    async def find_webui(_d):
        return port
    monkeypatch.setattr(H, "find_webui", find_webui)

    async def good(cloud=None):
        return []
    monkeypatch.setattr(selfcheck, "run", good)
    monkeypatch.setattr(OneBotClient, "BACKOFF", (0.02,))   # 重连循环缩成 20 毫秒一圈
    # OneBot 的端点: 一个空的本机 aiohttp 应用 (另开端口, 不是假管理页). 握手立刻 404, 走"握手失败"的分支, 循环才真按 20 毫秒转;
    # 连没人听的端口在 Windows 上每次约 2 秒才报错, 循环转不起来
    ws_runner, ws_port = await F.serve(web.Application())
    panel = {"type": "channel_config", "channel": "qq", "enabled": True, "self_id": UIN, "owner_id": OWNER, "token": "tok-0123456789",
             "snowluma_dir": str(install), "endpoint": f"ws://127.0.0.1:{ws_port}/"}   # 面板发来的整份快照: 没有 snowluma_attach
    made, notices = [], []

    def new_manager():
        mgr = ChannelManager({"channels": {"qq": dict(base_qq or {}), "weixin": {"home": str(tmp_path / "wx")}}},
                             SimpleNamespace(memory=None, llm=None))

        async def send(msg):
            notices.append(msg)
        mgr.attach(send)
        mgr.rounds = count_rounds(mgr)
        made.append(mgr)
        return mgr

    mgr = new_manager()
    if saved is not None:
        mgr.vault.put("snowluma", {"password": saved}, purposes={"qq_attach": True} if allow is True else None)
    w = SimpleNamespace(fake=fake, mgr=mgr, panel=panel, notices=notices, new_manager=new_manager, port=port)
    try:
        yield w
    finally:
        for m in made:
            with contextlib.suppress(Exception):
                await m.stop()
        await runner.cleanup()
        await ws_runner.cleanup()


async def start(w, mgr=None):
    res = await (mgr or w.mgr).configure(dict(w.panel))
    assert res["ok"], res
    return res


@async_test
async def test_it_attaches_by_itself_through_the_real_manager_when_the_line_is_gone(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as w:
        assert "snowluma_attach" not in w.mgr.base
        await start(w)
        assert w.mgr.conf["snowluma_attach"] is True
        assert await until(lambda: w.fake.loads == [1001])
        assert await until(lambda: w.mgr.snowluma.status().get("state") == "ready")
        n = len(w.fake.calls)
        await spin(w.mgr, 10)                          # 端口通了: 重连循环真再转十几圈, 也不再碰管理页
        assert len(w.fake.calls) == n
        assert logins(w.fake) == 1 and w.fake.logouts == 1 and not w.fake.tokens
        rec = w.mgr.vault.log_tail(5)[-1]
        assert rec["purpose"] == "qq_attach" and rec["by"] == "launcher" and rec["ok"] is True


@async_test
async def test_control_the_old_line_is_what_blocked_it(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch, base_qq={"snowluma_attach": False}) as w:
        await start(w)
        assert w.mgr.conf["snowluma_attach"] is False
        await spin(w.mgr, 10)
        assert w.fake.calls == [] and not w.fake.loads
        assert w.mgr.snowluma.status()["state"] == "waiting_account"   # 和用户看到的"一直在重连"是同一个状态


@async_test
async def test_a_wrong_saved_password_is_tried_once_even_across_a_restart_of_the_service(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch, password="真密码", saved="旧的错密码") as w:
        await start(w)
        assert await until(lambda: w.mgr.snowluma.status().get("state") == "login_rejected")
        assert await spin(w.mgr, 20) >= 20             # 重连循环真转了二十多圈
        assert w.fake.bad == 1 and logins(w.fake) == 1 and not w.fake.loads
        assert [n["kind"] for n in w.notices if n.get("kind") == "qq_login_rejected"] == ["qq_login_rejected"]
        # 重启 8790: 新的 ChannelManager、新的启动器 (没有任何内存), 保管箱在磁盘上
        await w.mgr.stop()
        mgr2 = w.new_manager()
        await start(w, mgr2)
        assert await until(lambda: mgr2.snowluma.status().get("state") == "login_rejected")
        assert await spin(mgr2, 20) >= 20
        assert w.fake.bad == 1 and not w.fake.loads
        # 用户在 /vault 存了新密码 (这回对了), 点"现在检查": 才再试, 一次就成
        await asyncio.sleep(0.02)
        mgr2.vault.put("snowluma", {"password": "真密码"})
        await mgr2.snowluma.attach_now(mgr2.conf)
        assert await until(lambda: w.fake.loads == [1001])
        assert logins(w.fake) == 2                     # 一共两次: 最早那次错的, 加新密码这一次 (成功以后管理页把错误计数清零)


@async_test
async def test_nothing_is_tried_when_the_vault_has_no_password(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch, saved=None) as w:
        await start(w)
        assert await until(lambda: w.mgr.snowluma.status().get("state") == "need_password")
        await spin(w.mgr, 10)
        assert w.fake.calls == []                      # 连管理页都没碰
        assert [n["kind"] for n in w.notices if n.get("kind") == "qq_need_password"] == ["qq_need_password"]   # 说一次


# -- 启动器层 -----------------------------------------------------------------------------------
@async_test
async def test_while_the_page_is_locked_it_does_not_log_in_again_until_the_lock_is_over(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()], bad=5) as w:
        st = await w.step()
        assert st["state"] == "attach_locked" and logins(w.fake) == 1
        for _ in range(8):                             # 800 秒: 锁是 897 秒
            await w.step(100)
        assert logins(w.fake) == 1
        w.fake.bad = 0                                 # 锁过期了
        await w.step(120)
        assert logins(w.fake) == 2 and w.fake.loads == [1001]


@async_test
async def test_a_rejection_that_cannot_be_written_to_disk_is_still_remembered(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()], password="真密码", saved="旧的错密码") as w:
        def broken(*a, **k):
            raise OSError("磁盘写不进去")
        monkeypatch.setattr(w.vault, "set_flags", broken)
        assert (await w.step())["state"] == "login_rejected" and w.fake.bad == 1
        for _ in range(12):                            # 一小时: 以前每 300 秒再拿同一个错密码试一次
            await w.step(310)
        assert w.fake.bad == 1 and logins(w.fake) == 1 and not w.fake.loads


@async_test
async def test_a_rejection_whose_flag_was_lost_from_disk_is_still_remembered(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()], password="真密码", saved="旧的错密码") as w:
        assert (await w.step())["state"] == "login_rejected" and w.fake.bad == 1
        (w.vault.home / "state.json").write_text("{坏的", encoding="utf-8")   # state.json 读不出来: _flags_all 当空
        assert w.vault.get("snowluma")["rejected"] is False
        for _ in range(6):
            await w.step(310)
        assert w.fake.bad == 1
        await asyncio.sleep(0.02)
        w.vault.put("snowluma", {"password": "又一个错的"})     # 用户存了新密码 (这版也不对): 只多这一版的 1 次
        await w.step(310)
        assert w.fake.bad == 2
        for _ in range(6):
            await w.step(310)
        assert w.fake.bad == 2


@async_test
async def test_things_only_the_owner_can_do_in_the_webui_are_not_retried_every_round(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()]) as w:
        w.fake.totp = True
        for _ in range(18):                            # 30 分钟, 每次 100 秒
            await w.step(100)
        assert 1 <= logins(w.fake) <= 4                # 600 秒一次: 30 分钟里最多 4 次 (登录本身是成功的, 被两步验证拦下)
        assert not w.fake.loads


@pytest.mark.parametrize("case", ["no_password", "deny", "ask"])
@async_test
async def test_no_login_at_all_without_a_password_or_the_owners_nod(tmp_path, monkeypatch, case):
    kw = {"no_password": {"saved": None}, "deny": {"allow": "deny"}, "ask": {"allow": False}}[case]
    async with world(tmp_path, monkeypatch, procs=[mine()], **kw) as w:
        for _ in range(30):
            await w.step(10)
        assert w.fake.calls == [] and not w.fake.loads


# -- 桌面通知 (D) --------------------------------------------------------------------------------
class Toasts:
    def __init__(self):
        self.shown = []

    async def __call__(self, title, text):
        self.shown.append((title, text))
        return True


async def settle():
    await asyncio.sleep(0.05)


@async_test
async def test_a_notice_that_really_spoke_also_pops_a_desktop_toast_and_follows_the_cooldown(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()], saved=None) as w:
        w.mgr.desk_notice = Toasts()
        await w.step()
        await settle()
        assert len(w.mgr.desk_notice.shown) == 1
        title, text = w.mgr.desk_notice.shown[0]
        assert title == "绫音的 QQ 没连上" and "/vault" in text and PW not in text
        for _ in range(5):                             # 冷却内: 说话和弹窗都不重复
            await w.step(100, fresh=True)
        await settle()
        assert len(w.mgr.desk_notice.shown) == 1
        w.t[0] += 7 * 3600                             # 冷却过了: 再说一次, 再弹一次
        await w.step(0, fresh=True)
        await settle()
        assert len(w.mgr.desk_notice.shown) == 2


@async_test
async def test_toast_is_only_for_notices_that_speak_to_the_owner(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()]) as w:
        w.mgr.desk_notice = Toasts()
        assert await w.bridge.notice("qq_x", "只在桌宠上说一句", tell=False) is True
        await settle()
        assert w.mgr.desk_notice.shown == []
        assert await w.bridge.notice("qq_x", "冷却里", tell=True) is False   # 同一个 key 冷却里: 没说, 也不弹
        await settle()
        assert w.mgr.desk_notice.shown == []


@async_test
async def test_the_qr_notice_toast_carries_no_picture_and_no_link(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()]) as w:
        w.mgr.desk_notice = Toasts()
        await w.bridge.notice("qq_scan", "扫码链接 https://example.test/qr?k=1 在 /vault", tell=True, image=b"\x89PNG-fake")
        await settle()
        assert w.mgr.desk_notice.shown == [("绫音的 QQ 没连上", "QQ 停在登录界面等扫码")]


@async_test
async def test_no_desk_notice_or_a_broken_one_changes_nothing(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()]) as w:
        assert not hasattr(w.mgr, "desk_notice")        # 没有存申请的文件的管理器 (测试、临时实例) 没有这个函数
        assert await w.bridge.notice("qq_a", "a", tell=True) is True and w.mgr.told == ["a"]

        async def boom(title, text):
            raise OSError("powershell 没了")
        w.mgr.desk_notice = boom
        assert await w.bridge.notice("qq_b", "b", tell=True) is True and w.mgr.told == ["a", "b"]
        await settle()
        w.mgr.desk_notice = None
        assert await w.bridge.notice("qq_c", "c", tell=True) is True and w.mgr.told == ["a", "b", "c"]


@async_test
async def test_a_slow_toast_does_not_hold_up_the_attach_round(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()], saved=None) as w:
        gate = asyncio.Event()

        async def slow(title, text):
            await gate.wait()                           # powershell 要等 15 秒: 这一圈不能跟着等
        w.mgr.desk_notice = slow
        st = await asyncio.wait_for(w.step(), 2)
        assert st["state"] == "need_password"
        gate.set()
        await settle()

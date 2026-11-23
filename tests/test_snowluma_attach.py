"""替她挂接 QQ (wm/channels/snowluma_hook.py + snowluma.py 的挂接那一半): 全部用替身 (假的 SnowLuma 管理页在本机的临时端口上),
不启动真的 SnowLuma、不登录 QQ、不用真密码、不发消息、不调模型. 要用户本人扫码、登录的那几步不在这里测."""
import asyncio
import contextlib
import functools
import itertools
import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

import vault_fakes as F
from wm.channels import qq_window as QW
from wm.channels import snowluma as S
from wm.channels import snowluma_hook as H
from wm.channels import vault as V
from wm.channels.vault_bridge import Bridge

UIN, OTHER = 3091211145, 111222333
PW = "correct-horse-battery"


def async_test(fn):
    @functools.wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def mine(pid=1001, **kw):
    return F.qq_proc(pid, UIN, **kw)


def other(pid=1002):
    return F.qq_proc(pid, OTHER, nickname="别人")


# -- 管理页上那几步 --------------------------------------------------------------------
@contextlib.asynccontextmanager
async def webui(procs=None, **kw):
    fake = F.FakeSnowLuma(procs=procs if procs is not None else [], **kw)
    runner, port = await F.serve(fake.app())
    try:
        yield fake, port
    finally:
        await runner.cleanup()


@async_test
async def test_attach_hooks_only_her_process_and_logs_out():
    async with webui([other(1002), mine(1001)]) as (fake, port):
        res = await H.attach_account(port, PW, UIN)
        assert res.state == "attached" and res.pid == 1001 and fake.loads == [1001]
        assert fake.logins == 1 and fake.logouts == 1 and not fake.tokens
        assert fake.last_body == {"password": "<pw>"}  # 请求体里只有密码这一项
        assert [c for c in fake.calls if c[1].endswith("/load")] == [("POST", "/api/processes/1001/load")]


@async_test
async def test_already_hooked_is_left_alone_and_a_dropped_pipe_is_refreshed():
    async with webui([mine(injected=True)]) as (fake, port):
        assert (await H.attach_account(port, PW, UIN)).state == "already"
        assert not fake.loads and not fake.refreshes
        fake.procs[0]["connected"] = False  # 挂过, 但连接断了: 点管理页上的"刷新", 不重复挂
        res = await H.attach_account(port, PW, UIN)
        assert res.state == "refreshed" and fake.refreshes == [1001] and not fake.loads


@async_test
async def test_nothing_is_touched_when_her_account_is_not_there():
    async with webui([F.qq_proc(1003, None)]) as (fake, port):  # 一个停在登录界面的 QQ
        res = await H.attach_account(port, PW, UIN)
        assert res.state == "login_window" and res.waiting == [1003] and not fake.loads
    async with webui([other()]) as (fake, port):  # 只有别的号
        res = await H.attach_account(port, PW, UIN)
        assert res.state == "no_account" and not fake.loads
    async with webui([]) as (fake, port):
        assert (await H.attach_account(port, PW, UIN)).state == "no_qq" and not fake.loads
    async with webui([mine(1001), mine(1005)]) as (fake, port):  # 同一个号认出两个进程: 不敢乱挂
        with pytest.raises(H.HookError) as exc:
            await H.attach_account(port, PW, UIN)
        assert exc.value.kind == "ambiguous" and not fake.loads and fake.logouts == 1


@pytest.mark.parametrize("setup,kind", [
    (lambda f: setattr(f, "password", "别的密码"), "rejected"),
    (lambda f: setattr(f, "totp", True), "totp"),
    (lambda f: setattr(f, "consent_pending", True), "consent"),
    (lambda f: setattr(f, "must_change", True), "must_change"),
    (lambda f: setattr(f, "bad", 5), "locked"),
    (lambda f: setattr(f, "load_fails", True), "server"),
])
@async_test
async def test_manual_steps_and_refusals_are_named_and_never_leak_the_password(setup, kind):
    async with webui([mine()]) as (fake, port):
        setup(fake)
        with pytest.raises(H.HookError) as exc:
            await H.attach_account(port, PW, UIN)
        e = exc.value
        assert e.kind == kind and not fake.loads
        assert PW not in str(e) and PW not in e.text and PW not in repr(e) and "password" not in e.text.lower()
        if kind == "locked":
            assert e.wait_s == 897
        if kind == "rejected":
            assert fake.bad == 1  # 只试了一次
        if kind == "server":
            assert fake.logouts == 1  # 挂接没成也退出登录


@async_test
async def test_network_trouble_and_odd_replies_are_reported_as_such():
    with pytest.raises(H.HookError) as exc:
        await H.attach_account(free_port(), PW, UIN, timeout_s=2)
    assert exc.value.kind == "network" and PW not in exc.value.text

    async def odd_login(request):
        return web.json_response({"success": True})  # 没有 token: 对不上
    app = web.Application()
    app.router.add_post("/api/login", odd_login)
    runner, port = await F.serve(app)
    try:
        with pytest.raises(H.HookError) as exc:
            await H.attach_account(port, PW, UIN)
        assert exc.value.kind == "protocol"
    finally:
        await runner.cleanup()

    async def slow(request):
        await asyncio.sleep(5)
    app = web.Application()
    app.router.add_post("/api/login", slow)
    runner, port = await F.serve(app)
    try:
        t0 = time.monotonic()
        with pytest.raises(H.HookError) as exc:
            await H.attach_account(port, PW, UIN, timeout_s=0.5)
        assert exc.value.kind == "network" and time.monotonic() - t0 < 3
    finally:
        await runner.cleanup()


@async_test
async def test_probe_only_recognises_the_snowluma_page():
    async with webui([]) as (fake, port):
        assert await H.probe_webui(port) is True
    assert await H.probe_webui(free_port()) is False

    async def other_server(request):
        return web.json_response({"hello": 1})
    app = web.Application()
    app.router.add_get("/api/ui/public", other_server)
    runner, port = await F.serve(app)
    try:
        assert await H.probe_webui(port) is False  # 回了 200 但不是 SnowLuma 管理页的样子
    finally:
        await runner.cleanup()


# -- 找进程: 不走 psutil.process_iter (在这台电脑上要 3–8 秒、握着 GIL) -------------------------------
def test_process_discovery_is_cheap_and_looks_at_the_right_processes(tmp_path, monkeypatch):
    import psutil
    install = tmp_path / "SnowLuma"
    install.mkdir()
    procs = {
        1: SimpleNamespace(cmdline=lambda: ["node", "./index.mjs"], cwd=lambda: str(install)),        # SnowLuma 的外壳 (相对路径)
        5: SimpleNamespace(cmdline=lambda: [str(install / "node.exe"), str(install / "index.mjs")], cwd=lambda: "C:/x"),  # 里面那层
        6: SimpleNamespace(cmdline=lambda: ["node", "other.js"], cwd=lambda: str(install)),             # 别的脚本
        2: SimpleNamespace(cmdline=lambda: ["C:/QQ/QQ.exe"], cwd=lambda: "C:/QQ"),                       # QQ 主进程
        3: SimpleNamespace(cmdline=lambda: ["C:/QQ/QQ.exe", "--type=renderer"], cwd=lambda: "C:/QQ"),   # QQ 渲染进程
    }
    monkeypatch.setattr(H, "process_names", lambda: {1: "node.exe", 5: "node.exe", 6: "node.exe", 2: "QQ.exe", 3: "qq.exe", 4: "chrome.exe", 8: "node.exe"})

    def make(pid):
        if pid == 8:
            raise psutil.NoSuchProcess(pid)  # 刚好退出了
        return procs[pid]
    monkeypatch.setattr(psutil, "Process", make)
    monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: pytest.fail("不该用 process_iter 扫全部进程"))
    assert H.snowluma_pids(install) == [1, 5] and H.qq_main_pids() == [2]
    scripts = dict(H.node_scripts())
    assert scripts[6].name == "other.js" and 8 not in scripts


def test_unreadable_processes_are_skipped(monkeypatch):
    import psutil

    def locked(pid):
        raise psutil.AccessDenied(pid)
    monkeypatch.setattr(H, "process_names", lambda: {7: "node.exe", 9: "QQ.exe"})
    monkeypatch.setattr(psutil, "Process", locked)
    assert H.node_scripts() == [(7, None)] and H.snowluma_pids(Path("C:/anywhere")) == [] and H.qq_main_pids() == []


def test_process_names_fall_back_to_psutil_when_the_one_system_call_fails(monkeypatch):
    import psutil
    from wm import resources

    def broken():
        raise OSError("系统调用不可用")
    with monkeypatch.context() as m:
        m.setattr(resources, "_nt_processes", broken)
        m.setattr(psutil, "process_iter", lambda attrs: [SimpleNamespace(info={"pid": 4, "name": "node.exe"})])
        assert H.process_names() == {4: "node.exe"}
    if __import__("os").name == "nt":  # 补丁撤掉以后: 真的一次系统调用, 这台电脑上拿得到名字
        assert any(n.lower() == "python.exe" for n in H.process_names().values())


# -- 启动器: 什么时候挂、什么时候不挂、怎么说 --------------------------------------------------
class FakeManager:
    channel = "qq"

    def __init__(self):
        self.conf = {"enabled": True}
        self.notices, self.told, self.cards = [], [], []

    async def notify(self, msg):
        self.notices.append(msg)

    async def _tell_owner(self, text):
        self.told.append(text)
        return True

    async def _ask_card(self, kind, key, title, text, ok, no, qq="", what="", remote=True, **extra):
        self.cards.append({"key": key, "title": title, "text": text, "qq": qq, "on_answer": extra["on_answer"]})
        return "d1"


_WORLDS = itertools.count()


@contextlib.asynccontextmanager
async def world(tmp_path, monkeypatch, procs=None, password=PW, saved=PW, allow=True, qq_pids="auto", **fake_kw):
    fake = F.FakeSnowLuma(password=password, procs=procs if procs is not None else [])
    for k, v in fake_kw.items():  # bad / load_fails / opens_port 这些替身的开关
        setattr(fake, k, v)
    runner, port = await F.serve(fake.app())
    tmp_path = tmp_path / f"w{next(_WORLDS)}"  # 一个测试里可以起好几个世界
    install = tmp_path / "SnowLuma"
    (install / "config").mkdir(parents=True)
    (install / "node.exe").write_bytes(b"fake")
    (install / "index.mjs").write_text("// fixture")
    (install / "config" / "consent.json").write_text(json.dumps({"version": "fixture", "acceptedAt": "2026-11-07"}))
    vault = V.Vault(tmp_path / "vault")
    if saved is not None:
        vault.put("snowluma", {"password": saved}, purposes={"qq_attach": True} if allow is True else ({"qq_attach": "deny"} if allow == "deny" else None))
    t = [1000.0]
    mgr = FakeManager()
    bridge = Bridge(vault, mgr, clock=lambda: t[0])
    launcher = S.SnowLumaLauncher(vault, bridge, clock=lambda: t[0])
    spawned = []

    async def listening(_endpoint):
        return fake.onebot_up
    monkeypatch.setattr(S, "_listening", listening)
    monkeypatch.setattr(S, "_existing_process", lambda _d: 4242)
    monkeypatch.setattr(S, "_spawn", lambda d: spawned.append(d) or SimpleNamespace(pid=4242, poll=lambda: None))
    monkeypatch.setattr(H, "qq_main_pids", (lambda: [p["pid"] for p in fake.procs]) if qq_pids == "auto" else (lambda: list(qq_pids)))

    async def find_webui(_d):
        return port
    monkeypatch.setattr(H, "find_webui", find_webui)
    conf = {"enabled": True, "snowluma_dir": str(install), "endpoint": "ws://127.0.0.1:3001/", "self_id": UIN}
    w = SimpleNamespace(fake=fake, port=port, vault=vault, mgr=mgr, bridge=bridge, launcher=launcher, conf=conf, t=t, spawned=spawned)

    async def step(advance=0.0, fresh=False):
        t[0] += advance
        if fresh:
            launcher._attach_at = 0.0
        return await launcher.ensure(conf)
    w.step = step
    try:
        yield w
    finally:
        await runner.cleanup()


def no_secret(w) -> None:
    blob = json.dumps(w.launcher.status(), ensure_ascii=False) + json.dumps(w.mgr.notices, ensure_ascii=False) + "".join(w.mgr.told)
    blob += json.dumps([c["text"] + c["title"] + c["qq"] for c in w.mgr.cards], ensure_ascii=False)
    blob += (w.vault.home / "log.jsonl").read_text(encoding="utf-8") if (w.vault.home / "log.jsonl").exists() else ""
    assert PW not in blob and "别的密码" not in blob


@async_test
async def test_hooks_her_qq_then_goes_ready_without_touching_the_other_account(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[other(1002), mine(1001)]) as w:
        st = await w.step()
        assert st["state"] == "attaching" and "已替她挂接" in st["detail"] and w.fake.loads == [1001]
        assert w.fake.procs[0]["injected"] is False  # 别的号没动
        assert not w.spawned  # 已有的 SnowLuma 进程直接用
        st = await w.step(5)
        assert st["state"] == "ready" and "attach" not in st and w.launcher._attach is None
        assert w.mgr.notices == [] and w.fake.logins == 1
        no_secret(w)
        rec = w.vault.log_tail(5)[-1]
        assert rec["purpose"] == "qq_attach" and rec["by"] == "launcher" and rec["ok"] is True


@async_test
async def test_no_login_to_the_webui_while_the_port_is_open_or_qq_is_not_running(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()]) as w:
        w.fake.onebot_up = True
        assert (await w.step())["state"] == "ready" and not w.fake.calls
    async with world(tmp_path, monkeypatch, procs=[], qq_pids=[]) as w:  # 电脑上的 QQ 没开: 连管理页都不登录
        st = await w.step()
        assert st["state"] == "waiting_qq" and "QQ 没开" in st["detail"] and not w.fake.calls
        assert w.mgr.notices == []  # 刚没开就说太吵: 过 10 分钟还没登录才提醒
        st = await w.step(700, fresh=True)
        assert [n["kind"] for n in w.mgr.notices] == ["qq_waiting"] and w.mgr.told and "3091211145" in w.mgr.told[0]
        await w.step(10, fresh=True)
        assert len(w.mgr.notices) == 1  # 同一件事隔很久才再说


@async_test
async def test_without_a_saved_password_it_says_so_once(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()], saved=None) as w:
        st = await w.step()
        assert st["state"] == "need_password" and "/vault" in st["detail"] and not w.fake.calls
        await w.step(100, fresh=True)
        await w.step(100, fresh=True)
        assert [n["kind"] for n in w.mgr.notices] == ["qq_need_password"] and len(w.mgr.told) == 1
        w.vault.put("snowluma", {"password": PW}, purposes={"qq_attach": True})  # 主人填好了: 下一轮就挂
        assert (await w.step(100, fresh=True))["state"] == "attaching" and w.fake.loads == [1001]


@async_test
async def test_undecided_purpose_asks_for_a_nod_and_waits_until_given(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()], allow=False) as w:
        st = await w.step()
        assert st["state"] == "need_consent" and not w.fake.calls
        await asyncio.sleep(0.05)  # 卡片是转到事件循环里弹的
        assert len(w.mgr.cards) == 1 and "SnowLuma" in w.mgr.cards[0]["title"] and "挂接她的 QQ 号" in w.mgr.cards[0]["text"]
        await w.step(100, fresh=True)
        await asyncio.sleep(0.05)
        assert len(w.mgr.cards) == 1  # 还在等: 不重复弹
        w.mgr.cards[0]["on_answer"](True)  # 主人点了"允许"
        assert w.vault.get("snowluma")["purposes"][0]["state"] == "allow"
        assert (await w.step(100, fresh=True))["state"] == "attaching" and w.fake.loads == [1001]
        no_secret(w)


@async_test
async def test_denied_purpose_is_respected(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()], allow="deny") as w:
        st = await w.step()
        assert st["state"] == "attach_denied" and not w.fake.calls and w.mgr.cards == [] and w.mgr.notices == []


@async_test
async def test_rejected_password_is_tried_once_then_never_until_replaced(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()], password="真密码", saved="旧的错密码") as w:
        st = await w.step()
        assert st["state"] == "login_rejected" and w.fake.bad == 1 and not w.fake.loads
        assert [n["kind"] for n in w.mgr.notices] == ["qq_login_rejected"] and "重新填" in w.mgr.told[0]
        for _ in range(4):  # 之后再怎么催 (页面上点"现在检查"、重连循环) 都不再登录
            await w.step(400, fresh=True)
        assert w.fake.bad == 1 and w.fake.logins == 0 and len([c for c in w.fake.calls if c[1] == "/api/login"]) == 1
        assert w.vault.get("snowluma")["rejected"] is True
        no_secret(w)
        w.vault.put("snowluma", {"password": "真密码"})  # 主人换了新的
        assert w.vault.get("snowluma")["rejected"] is False
        assert (await w.step(10, fresh=True))["state"] == "attaching" and w.fake.loads == [1001]


@async_test
async def test_locked_out_and_two_factor_and_agreements_wait_for_the_owner(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()], bad=5) as w:
        st = await w.step()
        assert st["state"] == "attach_locked" and "897" in st["detail"] and w.launcher._attach_at - w.t[0] > 800
    async with world(tmp_path, monkeypatch, procs=[mine()]) as w:
        w.fake.totp = True
        st = await w.step()
        assert st["state"] == "need_user_webui" and "两步验证" in st["detail"] and [n["kind"] for n in w.mgr.notices] == ["qq_need_webui"]
    async with world(tmp_path, monkeypatch, procs=[mine()]) as w:
        w.fake.consent_pending = True
        assert "协议" in (await w.step())["detail"]
    async with world(tmp_path, monkeypatch, procs=[mine()]) as w:
        w.fake.must_change = True
        assert "改密码" in (await w.step())["detail"]


@async_test
async def test_transient_trouble_retries_with_backoff_and_tells_after_a_while(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()], load_fails=True) as w:
        waits = []
        for _ in range(5):
            st = await w.step(0, fresh=True)
            assert st["state"] == "attach_error" and "挂接没成功" in st["detail"]
            waits.append(round(w.launcher._attach_at - w.t[0]))
            w.t[0] += waits[-1]
        assert waits == [20, 40, 80, 160, 300] and w.fake.loads == []
        assert [n["kind"] for n in w.mgr.notices] == ["qq_attach_error"]
        w.fake.load_fails = False
        assert (await w.step(0, fresh=True))["state"] == "attaching"


@async_test
async def test_hooked_but_port_never_opens_is_called_out(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()], opens_port=False) as w:
        states = []
        for _ in range(3):
            states.append((await w.step(50, fresh=True))["state"])
        assert states == ["attaching", "attaching", "attach_error"] and w.fake.loads == [1001]  # 只点过一次"挂接", 之后管理页说已经挂着
        assert [n["kind"] for n in w.mgr.notices] == ["qq_port_closed"]


@async_test
async def test_the_attach_step_waits_between_tries(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()], opens_port=False) as w:
        await w.step()
        n = len(w.fake.calls)
        for _ in range(5):
            await w.step(5)  # 45 秒的缓冲没到: 不再登录管理页
        assert len(w.fake.calls) == n
        await w.step(40)
        assert len(w.fake.calls) > n


@async_test
async def test_attach_can_be_turned_off_and_old_status_stays(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, procs=[mine()]) as w:
        w.conf["snowluma_attach"] = False
        st = await w.step()
        assert st["state"] == "waiting_account" and not w.fake.calls
        w.conf["snowluma_attach"] = True
        w.conf["self_id"] = 0
        assert (await w.step(100, fresh=True))["state"] == "waiting_account"  # 没填她的号: 不知道该挂哪个


# -- 登录界面上的二维码 ---------------------------------------------------------------------
def shot(text="https://example.test/qr?k=1"):
    return QW.Shot(text, QW.render_png(text), 1003, time.time())


@async_test
async def test_login_window_qr_is_read_kept_in_memory_and_announced(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(QW, "snapshot_qr", lambda pids: seen.append(list(pids)) or shot())
    async with world(tmp_path, monkeypatch, procs=[F.qq_proc(1003, None)]) as w:
        st = await w.step()
        assert st["state"] == "need_scan" and st["qr"]["ttl_s"] == QW.TTL_S and seen == [[1003]] and not w.fake.loads
        png = w.launcher.qr_png()
        assert png and png.startswith(b"\x89PNG")
        assert "二维码" in w.mgr.told[0] and "/vault" in w.mgr.told[0] and [n["kind"] for n in w.mgr.notices] == ["qq_scan"]
        await w.step(25, fresh=True)
        assert len(seen) == 1  # 一分钟内不重复去看窗口
        await w.step(60, fresh=True)
        assert len(seen) == 2
        assert await w.launcher.refresh_qr() is True and len(seen) == 3  # 页面上点"刷新二维码"
        w.launcher._qr.at = time.time() - QW.TTL_S - 5  # 过期了就当没有
        assert w.launcher.qr_png() is None and "qr" not in w.launcher.status()
        # 她的号登录上去了: 马上挂接, 二维码清掉
        w.fake.procs[0].update(account={"uin": str(UIN), "nickname": "绫音"})
        st = await w.step(30, fresh=True)
        assert st["state"] == "attaching" and w.fake.loads == [1003]
        no_secret(w)
        await w.step(5)
        assert w.launcher._qr is None


@async_test
async def test_login_window_without_a_readable_qr_says_so_and_does_not_click(tmp_path, monkeypatch):
    monkeypatch.setattr(QW, "snapshot_qr", lambda pids: None)
    async with world(tmp_path, monkeypatch, procs=[F.qq_proc(1003, None)]) as w:
        st = await w.step()
        assert st["state"] == "waiting_login" and "没认出二维码" in st["detail"] and not w.fake.loads and w.launcher.qr_png() is None


@async_test
async def test_capture_failures_do_not_break_the_loop(tmp_path, monkeypatch):
    def boom(pids):
        raise OSError("窗口句柄失效")
    monkeypatch.setattr(QW, "snapshot_qr", boom)
    async with world(tmp_path, monkeypatch, procs=[F.qq_proc(1003, None)]) as w:
        assert (await w.step())["state"] == "waiting_login"


# -- SnowLuma 自己起不来 ------------------------------------------------------------------
@async_test
async def test_snowluma_that_keeps_dying_is_restarted_with_backoff_then_given_up_on(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "CRASH_WINDOW_S", 100000)  # 逻辑一样 (窗口内起了 CRASH_MAX 次就停); 窗口放大是为了不依赖步子怎么走
    async with world(tmp_path, monkeypatch, procs=[]) as w:
        exited = SimpleNamespace(pid=7, poll=lambda: 1)
        monkeypatch.setattr(S, "_existing_process", lambda _d: None)
        monkeypatch.setattr(S, "_spawn", lambda d: w.spawned.append(d) or exited)
        for _ in range(40):
            st = await w.step(31)
            if st["state"] == "crash_loop":
                break
            w.t[0] += 300  # 越过退避
        assert st["state"] == "crash_loop" and len(w.spawned) == S.CRASH_MAX
        assert [n["kind"] for n in w.mgr.notices] == ["qq_crash_loop"] and "logs" in st["detail"]
        for _ in range(5):
            await w.step(100)
        assert len(w.spawned) == S.CRASH_MAX  # 窗口内不再起
        w.t[0] += S.CRASH_WINDOW_S
        await w.step(1)
        assert len(w.spawned) == S.CRASH_MAX + 1  # 窗口过去了再试

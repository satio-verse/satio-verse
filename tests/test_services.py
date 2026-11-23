"""桌宠管服务 (wm/services.py): 按配置挑服务、拉起来、挂了重启、外面开的不管、请求重启/停; 存状态、读状态时撞上别的进程
(Windows 上换写被拒) 也不崩、不误判; 服务起不来、杀不掉不崩; 请求文件读不出来 / 删不掉 / 内容不对、上一次留下的旧请求不执行;
run() 一圈出了错不退出."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from wm import services as S

FAKE = """
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake(tmp_path):
    path = tmp_path / "fake_service.py"
    path.write_text(FAKE, encoding="utf-8")
    return path


def until(sup, cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        sup.step()
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_specs_follow_config(tmp_path):
    exe, model, emb = tmp_path / "llama-server.exe", tmp_path / "m.gguf", tmp_path / "e.gguf"
    for f in (exe, model, emb):
        f.write_bytes(b"x")
    (tmp_path / "gsv").mkdir()
    cfg = {
        "llm": {"engine": "llamacpp", "endpoint": "http://127.0.0.1:8784", "server": {"exe": str(exe), "model": str(model), "port": 8784},
                "escalate": {"endpoint": "", "server": {"model": str(model), "port": 8785}}},
        "memory": {"embed": {"engine": "auto", "endpoint": "http://127.0.0.1:8786", "server": {"model": str(emb), "port": 8786}}},
        "tts": {"engine": "gpt_sovits", "http": {"base_url": "http://127.0.0.1:9880", "server": {"dir": str(tmp_path / "gsv")}}},
        "server": {"port": 8790},
        "services": {"embed": "off"},
    }
    got = {s.name: s for s in S.specs(cfg, python="py")}
    assert list(got) == ["llm", "tts", "server"]  # 嵌入默认不开, 复核模型没配
    assert got["server"].after == ["llm"] and got["server"].port == 8790 and got["server"].health == "/api/status"
    assert got["tts"].port == 9880 and got["tts"].health == "tcp"
    cfg["services"] = {"embed": "on", "llm": "off"}
    assert [s.name for s in S.specs(cfg, python="py")] == ["embed", "tts", "server"]
    # 云端模型 / 替身语音时不开本机的
    cfg["llm"]["engine"], cfg["tts"]["engine"], cfg["services"] = "cloud", "mock", {}
    assert [s.name for s in S.specs(cfg, python="py")] == ["server"]


def test_starts_and_restarts_when_it_dies(tmp_path, fake):
    port = free_port()
    sup = S.Supervisor([S.Spec("llm", [sys.executable, str(fake), str(port)], port, "/health", 20)],
                       home=tmp_path / "home", logs=tmp_path / "logs", tick=0.1)
    try:
        assert until(sup, lambda: sup.st["llm"]["state"] == "up")
        first = sup.st["llm"]["pid"]
        S._kill_tree(first)
        assert until(sup, lambda: sup.st["llm"]["state"] == "down")
        assert until(sup, lambda: sup.st["llm"]["state"] == "up" and sup.st["llm"]["pid"] != first)
        assert len(sup.st["llm"]["restarts"]) == 1
        st = S.read_state(tmp_path / "home")
        assert st["running"] and st["services"][0]["state"] == "up"  # 状态文件跟着写
    finally:
        for n in list(sup.proc):
            sup._reap(n)


def test_external_service_is_left_alone(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(S, "TAKEOVER_S", 1.0)
    port = free_port()
    other = subprocess.Popen([sys.executable, str(fake), str(port)])
    try:
        sup = S.Supervisor([S.Spec("llm", [sys.executable, str(fake), str(port)], port, "/health", 20)],
                           home=tmp_path / "home", logs=tmp_path / "logs")
        assert until(sup, lambda: sup.st["llm"]["state"] == "external")
        assert not sup.proc  # 没再开一份
        other.kill()
        other.wait()
        sup.step()
        assert sup.st["llm"]["state"] == "external" and not sup.proc  # 刚没了先等等 (可能只是在重启)
        assert until(sup, lambda: sup.st["llm"]["state"] == "up")  # 等够了还没回来, 接手
    finally:
        other.kill()
        for n in list(sup.proc):
            sup._reap(n)


def test_gives_up_after_too_many_crashes(tmp_path):
    sup = S.Supervisor([S.Spec("tts", [sys.executable, "-c", "import sys; sys.exit(3)"], free_port(), "tcp", 20)],
                       home=tmp_path / "home", logs=tmp_path / "logs", restart_max=2)
    assert until(sup, lambda: sup.st["tts"]["state"] == "failed", timeout=20)
    assert "挂了 2 次" in sup.st["tts"]["error"]
    # 你点了重启: 从头再来
    S.request("restart", ["tts"], tmp_path / "home")
    sup.step()
    assert sup.st["tts"]["state"] in ("starting", "down") and sup.st["tts"]["restarts"] == []


def test_restart_and_stop_requests(tmp_path, fake):
    port = free_port()
    home = tmp_path / "home"
    sup = S.Supervisor([S.Spec("llm", [sys.executable, str(fake), str(port)], port, "/health", 20)], home=home, logs=tmp_path / "logs")
    try:
        assert until(sup, lambda: sup.st["llm"]["state"] == "up")
        first = sup.st["llm"]["pid"]
        S.request("restart", ["llm"], home)
        assert until(sup, lambda: sup.st["llm"]["state"] == "up" and sup.st["llm"]["pid"] != first)
        assert not S._alive(first)
        S.request("stop", None, home)
        sup.step()
        assert sup.stop_all and sup.st["llm"]["state"] == "stopped" and not sup.proc
        assert not list(home.glob("req-*.json"))
    finally:
        for n in list(sup.proc):
            sup._reap(n)


def test_status_without_supervisor(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(S, "HOME", tmp_path)
    monkeypatch.setattr(S.read_state, "__defaults__", (tmp_path,))
    assert S.main(["status"]) == 0
    assert "看守没在跑" in capsys.readouterr().out
    assert S.main(["stop"]) == 0
    assert "没有要停的" in capsys.readouterr().out


def test_api(tmp_path):
    """/api/services: 看服务、看守不在时不收重启请求、别的网页不能停服务; 看守在时请求写进它的目录."""
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from test_pipeline import make_cfg
    from wm.app import create_app

    async def main():
        client = TestClient(TestServer(create_app(make_cfg(tmp_path))))
        await client.start_server()
        try:
            got = await (await client.get("/api/services")).json()
            no_sup = (await client.post("/api/services", json={"action": "restart"})).status
            foreign = (await client.post("/api/services", json={"action": "stop"}, headers={"Origin": "https://evil.example"})).status
            home = S.read_state.__defaults__[0]
            S._write_json(home / "state.json", {"pid": __import__("os").getpid(), "services": [
                {"name": "tts", "label": "语音合成", "port": 9880, "state": "up"}]})
            ok = await client.post("/api/services", json={"action": "restart", "names": ["tts"]})
            body = await ok.json()
            return got, no_sup, foreign, ok.status, body, [p.name for p in home.glob("req-*.json")]
        finally:
            await client.close()

    got, no_sup, foreign, status, body, reqs = asyncio.run(main())
    assert got["running"] is False and no_sup == 409 and foreign == 403
    assert status == 200 and body["running"] and body["services"][0]["word"] == "在跑" and len(reqs) == 1


def test_server_watched_elsewhere_is_not_taken_over(tmp_path, fake):
    """你在终端跑着 tools/serve.py run: 它换服务端时端口空几秒, 不能趁机再开一份服务端抢端口."""
    port = free_port()
    watched = {"on": True}
    sup = S.Supervisor([S.Spec("server", [sys.executable, str(fake), str(port)], port, "/health", 20, elsewhere=lambda: watched["on"])],
                       home=tmp_path / "home", logs=tmp_path / "logs")
    for _ in range(3):
        sup.step()
    assert sup.st["server"]["state"] == "external" and not sup.proc
    watched["on"] = False  # 那个看守也没了 (S.TAKEOVER_S 之后接手)
    S.TAKEOVER_S, old = 0.0, S.TAKEOVER_S
    try:
        assert until(sup, lambda: sup.st["server"]["state"] == "up")
    finally:
        S.TAKEOVER_S = old
        for n in list(sup.proc):
            sup._reap(n)


def test_server_start_is_not_timed_here(tmp_path):
    """服务端起得慢 (机器忙、A2F 载入一分多钟) 不在这里限时: tools/serve.py 自己看着它 (看心跳分得清起得慢和起不来).
    以前这里 150 秒没好就连 serve.py 一起结束, 2026-10-27 20:07~20:23 整棵进程树被一遍遍杀掉重来."""
    cfg = {"llm": {"engine": "cloud"}, "tts": {"engine": "mock"}, "server": {"port": 8790}}
    assert next(s for s in S.specs(cfg, python="py") if s.name == "server").ready_s is None
    sup = S.Supervisor([S.Spec("server", [sys.executable, "-c", "import time; time.sleep(60)"], free_port(), "/api/status", None)],
                       home=tmp_path / "home", logs=tmp_path / "logs")
    try:
        sup.step()
        assert sup.st["server"]["state"] == "starting"
        sup.st["server"]["since"] -= 3600  # 一个钟头还没好
        sup.step()
        assert sup.st["server"]["state"] == "starting" and sup.proc["server"].poll() is None
    finally:
        for n in list(sup.proc):
            sup._reap(n)


def test_stop_only_own_install(tmp_path, capsys):
    """卸载程序 (stop --root <装的目录>) 只停自己那一份: 同一台电脑上另一份在跑的看守不碰."""
    import os

    home = S.read_state.__defaults__[0]
    S._write_json(home / "state.json", {"pid": os.getpid(), "root": str(tmp_path / "other"), "services": []})
    assert S.main(["stop", "--root", str(tmp_path / "mine")]) == 0
    assert "另一份" in capsys.readouterr().out and not list(home.glob("req-*.json"))
    assert S.main(["stop", "--root", str(tmp_path / "other")]) == 0
    assert len(list(home.glob("req-*.json"))) == 1


def test_stop_root_ignores_old_supervisor(tmp_path, monkeypatch, capsys):
    """旧版看守的状态里没有 root: 在装的目录里跑卸载 (当前目录 = --root) 也不能把它当成自己的."""
    import os

    home = S.read_state.__defaults__[0]
    S._write_json(home / "state.json", {"pid": os.getpid(), "services": []})
    monkeypatch.chdir(tmp_path)
    assert S.main(["stop", "--root", str(tmp_path)]) == 0
    assert "另一份" in capsys.readouterr().out and not list(home.glob("req-*.json"))


# -- 存状态、读状态时撞上别的进程 (2026-11-11 工具类组长评审下载队列时查到的老毛病) ----------------------------------------------------
# Windows 上别的进程 (桌宠宿主、面板、命令行 status) 正开着 state.json 读的那一下, 看守"写临时文件再 os.replace"换不了 (PermissionError).
# 原来 step() 没接、run() 收尾的 save() 又抛一次, 主看守 (服务端、本机模型、语音合成都挂在它下面) 就崩了; 读的一边正碰上换, 读不出来
# 就当"看守不在": up() 会再起一个看守, /api/services 的重启和停回 409. 下面用替身让换写、读文件失败 (先坏几次再好 / 一直坏 / 再恢复),
# 最后一条是真的两边对撞.


def _sup(tmp_path):
    """一个不管任何服务的看守: step() 只剩"存状态"这一件事."""
    return S.Supervisor([], home=tmp_path / "home", logs=tmp_path / "logs", tick=0.01)


def _said(logs):
    """看守往 supervisor-<日期>.log 里记的话 (一行一条)."""
    return [ln for p in sorted(logs.glob("supervisor-*.log")) for ln in p.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def quick(monkeypatch):
    """重试的间隔缩成 0 (旧代码没有这几个常量: raising=False, 让旧代码走到真正的断言上再红)."""
    for name in ("REPLACE_WAIT_S", "READ_WAIT_S"):
        monkeypatch.setattr(S, name, 0.0, raising=False)


def _refuse_replace(monkeypatch, fails):
    """os.replace 换 state.json 时先抛 fails 次 PermissionError (别的进程正开着它读), 之后恢复; fails=None 一直抛.
    返回计数: box["fails"] 改成 0 就是"别的进程放手了"."""
    box, real = {"fails": fails, "calls": 0, "raised": 0}, os.replace

    def replace(src, dst, *a, **k):
        if Path(dst).name == "state.json":
            box["calls"] += 1
            if box["fails"] is None or box["raised"] < box["fails"]:
                box["raised"] += 1
                raise PermissionError(13, "拒绝访问: 别的进程正开着要被换掉的文件")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(S.os, "replace", replace)
    return box


def _flaky_reads(monkeypatch, kinds):
    """读 state.json 时先按 kinds 一个个撞上 (denied = 打开被拒 PermissionError, empty = 读到空的, half = 读到半截), 之后恢复.
    返回读的次数 (只数 state.json)."""
    box, real = {"n": 0}, S.Path.read_text

    def read_text(self, *a, **k):
        if self.name != "state.json":
            return real(self, *a, **k)
        box["n"] += 1
        if box["n"] > len(kinds):
            return real(self, *a, **k)
        kind = kinds[box["n"] - 1]
        if kind == "denied":
            raise PermissionError(13, "拒绝访问: 看守正在换这个文件")
        text = real(self, *a, **k)
        return "" if kind == "empty" else text[: len(text) // 2]
    monkeypatch.setattr(S.Path, "read_text", read_text)
    return box


def test_state_write_waits_out_a_refused_swap(tmp_path, monkeypatch, quick):
    """换写被拒了几次、后来能换了: 稍等再换, 状态照样写进去; 没出岔子, 不留临时文件, 也不用往日志里记."""
    box = _refuse_replace(monkeypatch, 2)
    _sup(tmp_path).step()
    got = S.read_state(tmp_path / "home")
    assert box["raised"] == 2 and got["pid"] == os.getpid() and got["running"] is True
    assert not list((tmp_path / "home").glob("state.*.tmp")) and not _said(tmp_path / "logs")


def test_a_swap_that_stays_refused_does_not_take_the_supervisor_down(tmp_path, monkeypatch, quick):
    """一直换不了: step() 不抛, 上一份状态原样还在, 日志里记一行 (同一阵连着失败不刷屏), 没有临时文件留下;
    别的进程放手以后, 下一遍就把当时的状态存上, 日志里说一声."""
    home, logs = tmp_path / "home", tmp_path / "logs"
    sup = _sup(tmp_path)
    sup.step()  # 先存一份好的
    before = (home / "state.json").read_bytes()
    box = _refuse_replace(monkeypatch, None)
    sup.stop_all = True  # 这几遍之间看守的状态变了 (要停了), 可是存不进去
    for _ in range(4):
        sup.step()  # 不抛
    assert box["raised"] >= 4 and (home / "state.json").read_bytes() == before and not list(home.glob("state.*.tmp"))
    said = _said(logs)
    assert len(said) == 1 and "state.json" in said[0] and "PermissionError" in said[0]  # 连着失败 4 遍只说一次
    box["fails"] = 0  # 别的进程放手了
    sup.step()
    assert S.read_state(home)["stopping"] is True  # 下一遍照常存上了
    said = _said(logs)
    assert len(said) == 2 and "又存上了" in said[1] and not list(home.glob("state.*.tmp"))


def test_final_save_cannot_mask_the_real_exit_reason(tmp_path, monkeypatch, quick):
    """run() 收尾那次存状态换不了, 也不能抛出来盖掉真正的退出原因 (正常停 / Ctrl+C)."""
    _refuse_replace(monkeypatch, None)
    sup = _sup(tmp_path / "stop")
    sup.stop_all = True
    assert sup.run() == "stop"
    sup = _sup(tmp_path / "ctrl_c")

    def interrupted():
        raise KeyboardInterrupt
    sup.step = interrupted
    assert sup.run() == "interrupted"


def test_a_failing_write_removes_its_temp_file(tmp_path, monkeypatch, quick):
    """换写一直被拒: 稍等再换过几次, 然后抛给调用的人 (它决定怎么办), 旁边的临时文件删干净 (原来每次失败都留一个 state.<进程号>.tmp)."""
    box = _refuse_replace(monkeypatch, None)
    with pytest.raises(PermissionError):
        S._write_json(tmp_path / "state.json", {"a": 1})
    assert box["calls"] > 1  # 稍等再换过
    assert list(tmp_path.iterdir()) == []  # 没留下临时文件


def test_save_itself_still_raises_so_a_subclass_can_decide(tmp_path, monkeypatch, quick):
    """save() 本身还是照直抛: 下载队列的小看守 (wm/downloads/daemon.py 的 Watcher) 重写了 save, 靠它抛出来自己重试、记一笔."""
    _refuse_replace(monkeypatch, None)
    with pytest.raises(PermissionError):
        _sup(tmp_path).save()


@pytest.mark.parametrize("kinds", [["denied"], ["denied", "denied"], ["empty"], ["half", "denied"]])
def test_read_state_waits_out_a_swap_in_progress(tmp_path, monkeypatch, quick, kinds):
    """正碰上看守在换状态文件 (打开被拒 / 读到空的 / 读到半截的): 稍等再读, 照样认出看守在跑 —— 不能读成"看守不在"
    (那样 up() 会再起一个看守, /api/services 的重启和停会回 409)."""
    home = tmp_path / "home"
    S._write_json(home / "state.json", {"pid": os.getpid(), "services": [{"name": "llm", "state": "up"}]})
    box = _flaky_reads(monkeypatch, kinds)
    got = S.read_state(home)
    assert got["running"] is True and got["pid"] == os.getpid() and got["services"][0]["name"] == "llm"
    assert box["n"] == len(kinds) + 1  # 撞上几次就多读几次, 读成了就不再读


def test_read_state_does_not_wait_for_a_file_that_is_not_there(tmp_path, monkeypatch):
    """文件本来就不在 (看守没起过): 照旧当没有, 不等、不重读."""
    box = _flaky_reads(monkeypatch, [])
    assert S.read_state(tmp_path / "nothing") == {"running": False}
    assert box["n"] == 1


def test_read_state_gives_up_on_a_file_that_stays_unreadable(tmp_path, monkeypatch, quick):
    """一直读不出来: 重读了几次才放弃, 和从前一样当没有 (不抛)."""
    home = tmp_path / "home"
    S._write_json(home / "state.json", {"pid": os.getpid(), "services": []})
    box = _flaky_reads(monkeypatch, ["denied"] * 50)
    assert S.read_state(home) == {"running": False}
    assert 1 < box["n"] <= 50


def test_save_and_read_hold_up_against_each_other_for_real(tmp_path, monkeypatch):
    """真的撞 (换写和读文件不用替身): 一个线程隔一会儿 step() 存一回, 主线程不停地 read_state.
    旧代码两边都会出错: 写的抛 PermissionError, 读的读成"看守不在"."""
    # 判"这个进程号是不是看守"要问 psutil, 一次几十毫秒, 读的一边就不"紧"了、撞不上; 这里只看读文件的那一半, 换成一句话
    monkeypatch.setattr(S, "_is_supervisor", lambda pid: pid == os.getpid())
    home = tmp_path / "home"
    sup = _sup(tmp_path)
    sup.step()
    stop, errors = threading.Event(), []

    def keep_saving():
        while not stop.is_set():
            try:
                sup.step()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            time.sleep(0.002)
    th = threading.Thread(target=keep_saving, daemon=True)
    th.start()
    reads = misses = 0
    end = time.time() + 1.5
    try:
        while time.time() < end:
            reads += 1
            misses += not S.read_state(home)["running"]
    finally:
        stop.set()
        th.join(10)
    assert reads > 50 and errors == [] and misses == 0
    assert S.read_state(home)["running"] is True and not list(home.glob("state.*.tmp"))


# -- 起不来、杀不掉 (同一件事里评审出的同类毛病) ----------------------------------------------------------------------------------------
# 起一个服务时日志目录建不了、日志文件打不开、程序不在、被拦着 (OSError), 原来会从 step() 冒出去, 主看守 (整棵进程树的根) 就崩了;
# 杀了进程还结束不掉 (p.wait 超时, TimeoutExpired) 也一样, 从 step()、_stop()、run() 的 Ctrl+C 冒出去. 现在: 起不来记成 down
# (按"起来就立刻挂了"算进重启次数和退避, 到 restart_max 变 failed), 结束不掉记一行日志、摘掉它接着往下走.
# 结束不掉这一组用替身进程, S._kill_tree 也换成替身 (kills 夹具): 绝不碰真的进程号, 包括测试进程自己.


def _one(tmp_path, cmd, **kw):
    """只管一个服务 (tts, 端口探测) 的看守."""
    return S.Supervisor([S.Spec("tts", cmd, free_port(), "tcp", 20)], home=tmp_path / "home", logs=tmp_path / "logs", **kw)


def _no_program(tmp_path):
    return [str(tmp_path / "no_such_program.exe")]


def _service_logs(tmp_path):
    return list((tmp_path / "logs").glob("tts-*.log"))


def test_a_program_that_is_not_there_makes_a_down_service_not_a_dead_supervisor(tmp_path):
    """起的程序不在: step() 不抛, 服务记成 down、error 写清是哪一步什么错, supervisor 日志里记一行, 进程表里不留东西;
    开着的日志文件关了 (能删掉: Windows 上开着的文件删不掉)."""
    sup = _one(tmp_path, _no_program(tmp_path))
    sup.step()
    s = sup.st["tts"]
    assert s["state"] == "down" and s["pid"] is None and not sup.proc
    assert "起进程" in s["error"] and "FileNotFoundError" in s["error"] and "no_such_program" in s["error"]
    said = _said(tmp_path / "logs")
    assert len(said) == 1 and "tts" in said[0] and "起进程" in said[0]
    assert len(_service_logs(tmp_path)) == 1
    for p in _service_logs(tmp_path):
        p.unlink()


def test_a_start_that_keeps_failing_is_counted_backed_off_and_given_up_on(tmp_path):
    """起不来按"起来就立刻挂了"算: 记进重启次数、隔着退避再试, restart_max 次以后变 failed 不再拉 (不能每秒狂试).
    退避从这一次失败算起: _set 只在状态变了才更新 since, down 到 down 不变, 要自己更新 —— 不然下一遍就又试."""
    sup = _one(tmp_path, _no_program(tmp_path), restart_max=2)
    sup.step()
    s = sup.st["tts"]
    assert s["state"] == "down" and s["restarts"] == []  # 第一次起 (和"起来就挂了"一样) 不算重启
    s["since"] -= 3600  # 退避过了: 下一遍再起
    t0 = time.time()
    sup.step()
    assert s["state"] == "down" and len(s["restarts"]) == 1 and s["since"] >= t0  # 这一次又没起来, 退避从现在算起
    sup.step()  # 马上再来一遍: 退避没到, 不再试
    assert len(s["restarts"]) == 1
    s["since"] -= 3600
    sup.step()
    assert len(s["restarts"]) == 2 and s["state"] == "down"
    sup.step()  # 两次重启都没成: 不再拉
    assert s["state"] == "failed" and "不再拉" in s["error"]
    assert len(_said(tmp_path / "logs")) == 3  # 每次没起来记一行: 第一次和两次重启
    sup.step()  # failed 以后不再碰
    assert len(_said(tmp_path / "logs")) == 3


class _FullDisk:
    """写不进去的日志文件 (替身): 真的文件包一层, write 抛 OSError, close 是真的."""

    def __init__(self, f):
        self.f = f

    def write(self, data):
        raise OSError(28, "磁盘满了")

    def close(self):
        self.f.close()


@pytest.mark.parametrize("where, word, exc_name", [("mkdir", "建日志目录", "PermissionError"), ("open", "开日志文件", "PermissionError"),
                                                   ("write", "写日志开头", "OSError"), ("popen", "起进程", "PermissionError")])
def test_every_step_of_starting_that_can_fail_is_handled(tmp_path, monkeypatch, where, word, exc_name):
    """日志目录建不了 / 日志文件打不开 / 写不进去 / Popen 抛错 (被杀毒软件拦着、没权限): 都不往外抛, error 里写清是哪一步,
    已经打开的日志关掉 (能删)."""
    sup = _one(tmp_path, [sys.executable, "-c", "pass"])
    real_open, real_mkdir = open, S.Path.mkdir

    def mkdir(self, *a, **k):
        if where == "mkdir" and self.name == "logs":
            raise PermissionError(13, "拒绝访问: 日志目录")
        return real_mkdir(self, *a, **k)

    def opener(path, *a, **k):
        if Path(path).name.startswith("tts-"):
            if where == "open":
                raise PermissionError(13, "拒绝访问: 日志文件被别的程序开着")
            f = real_open(path, *a, **k)
            return _FullDisk(f) if where == "write" else f
        return real_open(path, *a, **k)

    def popen(*a, **k):
        raise PermissionError(13, "拒绝访问: 程序被拦着了")
    monkeypatch.setattr(S.Path, "mkdir", mkdir)
    monkeypatch.setattr(S, "open", opener, raising=False)
    if where == "popen":
        monkeypatch.setattr(S.subprocess, "Popen", popen)
    sup.step()  # 不抛
    s = sup.st["tts"]
    assert s["state"] == "down" and word in s["error"] and exc_name in s["error"]  # error 里写清是哪一步、什么错
    assert s["pid"] is None and not sup.proc
    if where != "mkdir":  # 连日志目录都建不了时, supervisor 的日志也写不进去
        assert any(word in ln and "tts" in ln for ln in _said(tmp_path / "logs"))
    for p in _service_logs(tmp_path):
        p.unlink()  # 开着的日志都关了 (Windows 上开着的文件删不掉)


def test_a_service_that_could_not_start_starts_once_the_cause_is_gone(tmp_path, fake):
    """起不来的原因没了 (程序装回来了): 退避过了以后下一遍就起来, 一路走到 up, 之前的 error 清掉."""
    port = free_port()
    sup = S.Supervisor([S.Spec("llm", _no_program(tmp_path), port, "/health", 20)], home=tmp_path / "home", logs=tmp_path / "logs")
    try:
        sup.step()
        assert sup.st["llm"]["state"] == "down"
        sup.specs["llm"].cmd = [sys.executable, str(fake), str(port)]  # 程序装回来了
        sup.st["llm"]["since"] -= 3600  # 退避过了
        assert until(sup, lambda: sup.st["llm"]["state"] == "up")
        assert sup.st["llm"]["error"] == "" and sup.st["llm"]["pid"] == sup.proc["llm"].pid
    finally:
        for n in list(sup.proc):
            sup._reap(n)


class _Unkillable:
    """杀了也结束不掉的进程 (替身): poll() 一直没结果, wait() 一直超时. 不是真的进程."""
    pid = 987654

    def __init__(self, log):
        self._wm_log = log

    def poll(self):
        return None

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("替身", timeout)


@pytest.fixture
def kills(monkeypatch):
    """S._kill_tree 换成替身: 只记下被叫到的进程号, 一个真的进程也不碰 (包括测试进程自己)."""
    seen = []

    def kill_tree(pid, timeout=8.0):
        assert pid != os.getpid(), "测试里不能杀测试进程自己"
        seen.append(pid)
    monkeypatch.setattr(S, "_kill_tree", kill_tree)
    return seen


@pytest.fixture
def stuck(tmp_path, kills):
    """一个有"杀不掉的进程"在跑的看守: 进程表里是替身 (S._kill_tree 也是替身), 日志是真的文件 (看它关没关)."""
    sup = _one(tmp_path, [sys.executable, "-c", "pass"])
    sup.logs.mkdir(parents=True, exist_ok=True)
    f = open(sup.logs / "tts-stuck.log", "ab")
    p = _Unkillable(f)
    sup.proc["tts"] = p
    sup.st["tts"].update(pid=p.pid, state="starting", since=time.time(), log=str(sup.logs / "tts-stuck.log"))
    yield sup, p, f
    f.close()


def test_reap_does_not_throw_when_the_process_will_not_die(tmp_path, stuck, kills):
    """直接调 _reap: 杀了结束不掉, 不抛, 照样关日志、拿掉、清 pid, 返回 None, 日志里记一行 (哪个服务、进程号、可能要在任务管理器里结束)."""
    sup, p, f = stuck
    assert sup._reap("tts") is None
    assert kills == [p.pid]  # 叫过 _kill_tree (替身)
    assert "tts" not in sup.proc and sup.st["tts"]["pid"] is None and f.closed
    said = _said(tmp_path / "logs")
    assert len(said) == 1 and "tts" in said[0] and str(p.pid) in said[0] and "任务管理器" in said[0]
    (tmp_path / "logs" / "tts-stuck.log").unlink()  # 日志关了, 删得掉


def test_a_stuck_process_does_not_take_step_down(tmp_path, stuck, kills):
    """起不来超时那条路 (step 里的 _reap): 照样记成 down 往下走."""
    sup, p, f = stuck
    sup.specs["tts"].ready_s = 1.0
    sup.st["tts"]["since"] -= 3600  # 早就该好了
    sup.step()  # 不抛
    s = sup.st["tts"]
    assert s["state"] == "down" and "还没好" in s["error"] and "tts" not in sup.proc and f.closed and kills == [p.pid]
    assert len(_said(tmp_path / "logs")) == 1


def test_a_stop_request_with_a_stuck_process_still_stops(tmp_path, stuck, kills):
    """停的请求 (有 stop_cmd 的: _stop 先让它自己停, 等不到再 _reap): 记成 stopped, 不抛."""
    sup, p, f = stuck
    sup.specs["tts"].stop_cmd = [sys.executable, "-c", "pass"]  # 真跑一个什么都不干的小程序
    S.request("stop", ["tts"], tmp_path / "home")
    sup.step()
    assert sup.st["tts"]["state"] == "stopped" and "tts" not in sup.proc and f.closed and kills == [p.pid]


def test_ctrl_c_with_a_stuck_process_still_returns_interrupted(tmp_path, stuck, kills):
    """run() 遇到 Ctrl+C 要把还在跑的服务都停掉: 碰上杀不掉的也照样走完, 返回 interrupted."""
    sup, p, f = stuck

    def interrupted():
        raise KeyboardInterrupt
    sup.step = interrupted
    assert sup.run() == "interrupted"
    assert "tts" not in sup.proc and f.closed and kills == [p.pid]


# -- 请求文件和 run() 每一圈的兜底 (同一件事里的第三件) -----------------------------------------------------------------------------
# 请求文件 (req-*.json): 原来读不出来就当空请求删掉 (请求丢了), 删不掉就让 step() 抛出去; 上一次留下的 stop 请求只要删不掉, run() 开头就抛、
# 看守起不来; 要是让它执行了, 整个服务都被停掉. 现在: 读不出来的留着下一轮再读, 执行了但删不掉的记住不再执行, 内容不对的删掉, 上一次留下的
# 旧请求绝不执行 (run() 开头看到的名字、文件时间早于看守起来的时刻的). run() 里一圈 step() 抛了没接住的错, 原来整个看守就没了 (pythonw 起的,
# 没有控制台, 什么痕迹都不留); 现在这一圈作废、记进日志、下一圈照常, 连着 LOOP_FAIL_MAX 圈都错才退出.


def _wait(cond, timeout=30.0):
    """等条件成立 (run() 在另一个线程里跑, 这里不去调 step)."""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _stop_all(home):
    """一个"全部停"的请求: 执行了 sup.stop_all 就变成 True (没有服务也看得出来)."""
    return S.request("stop", [], home)


def _refuse_unlink(monkeypatch, names):
    """Path.unlink 删 names 里的文件时抛 PermissionError (被别的程序开着); names 是个集合, 往里加减就是让它删不掉 / 删得掉."""
    real = S.Path.unlink

    def unlink(self, *a, **k):
        if self.name in names:
            raise PermissionError(13, "拒绝访问: 别的程序正开着它")
        return real(self, *a, **k)
    monkeypatch.setattr(S.Path, "unlink", unlink)
    return names


def _refuse_read(monkeypatch, state):
    """Path.read_text 读请求文件 (req-*.json) 时先抛 state["fails"] 次 PermissionError 再恢复 (None = 一直读不出来); state["n"] 数读了几次."""
    real = S.Path.read_text

    def read_text(self, *a, **k):
        if self.name.startswith("req-"):
            state["n"] += 1
            if state["fails"] is None or state["n"] <= state["fails"]:
                raise PermissionError(13, "拒绝访问: 别的程序正开着它")
        return real(self, *a, **k)
    monkeypatch.setattr(S.Path, "read_text", read_text)


@pytest.fixture
def own_children_only(monkeypatch):
    """S._kill_tree 只杀本测试进程的直接子进程 (测试里起的小服务), 别的进程号一律断言失败: 绝不碰测试进程自己和别的真进程."""
    import psutil
    real = S._kill_tree

    def kill_tree(pid, timeout=8.0):
        assert pid != os.getpid(), "测试里不能杀测试进程自己"
        try:
            assert psutil.Process(pid).ppid() == os.getpid(), "只准杀测试自己起的子进程"
        except psutil.NoSuchProcess:
            return
        return real(pid, timeout)
    monkeypatch.setattr(S, "_kill_tree", kill_tree)


@pytest.mark.parametrize("deletable", [False, True])
def test_a_stop_request_left_by_the_last_run_is_never_executed(tmp_path, monkeypatch, fake, own_children_only, deletable):
    """看守起来之前就留着一个 stop 请求 (上一次留下的; 删不掉 / 删得掉): 看守起来以后服务照常, 这个请求没执行; 起来以后新写的请求照常执行.
    这条最要紧: 旧请求一执行, 整个服务都被停掉."""
    home, port = tmp_path / "home", free_port()
    home.mkdir()
    stale = _stop_all(home)
    _refuse_unlink(monkeypatch, set() if deletable else {stale.name})
    spec = S.Spec("llm", [sys.executable, str(fake), str(port)], port, "/health", 20)
    sup = S.Supervisor([spec], home=home, logs=tmp_path / "logs", tick=0.05)
    stopped, rounds = [], []
    real_stop, real_step = sup._stop, sup.step

    def stop(name):
        stopped.append(name)
        real_stop(name)

    def step():
        real_step()
        rounds.append(1)
    sup._stop, sup.step = stop, step
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("why", sup.run()), daemon=True)
    th.start()
    try:
        assert _wait(lambda: sup.st["llm"]["state"] == "up" or not th.is_alive())
        assert th.is_alive() and "why" not in out  # 看守没因为旧请求删不掉就起不来
        seen = len(rounds)
        assert _wait(lambda: len(rounds) >= seen + 3 or not th.is_alive())  # 再过几圈
        assert th.is_alive() and not sup.stop_all and stopped == [] and sup.st["llm"]["state"] == "up"
        assert stale.exists() is (not deletable)
        assert any(stale.name in ln and "不执行" in ln for ln in _said(tmp_path / "logs")) is (not deletable)
        _stop_all(home)  # 看守起来以后写的: 照常执行
        th.join(30)
        assert out.get("why") == "stop" and stopped == ["llm"]
    finally:
        sup.stop_all = True
        th.join(10)
        for n in list(sup.proc):
            sup._reap(n)


def test_a_request_with_an_old_file_time_is_not_executed(tmp_path, monkeypatch):
    """文件时间早于看守起来的时刻的请求 (看守起来以后才出现的也一样): 不执行, 试着删, 每个文件只记一行; 看守起来以后写的照常执行."""
    sup = _sup(tmp_path)
    old = sup.t0 - 3600
    req = _stop_all(sup.home)
    os.utime(req, (old, old))
    sup.step()
    assert not sup.stop_all and not req.exists()
    said = _said(tmp_path / "logs")
    assert len(said) == 1 and req.name in said[0] and "旧请求" in said[0]
    refuse = _refuse_unlink(monkeypatch, set())  # 删不掉的: 照样不执行, 只记一行, 每轮再试着删
    req2 = _stop_all(sup.home)
    os.utime(req2, (old, old))
    refuse.add(req2.name)
    for _ in range(3):
        sup.step()
    assert not sup.stop_all and req2.exists() and len(_said(tmp_path / "logs")) == 2
    refuse.clear()
    sup.step()
    assert not sup.stop_all and not req2.exists()
    _stop_all(sup.home)  # 起来以后写的: 照常执行 (文件时间和 time.time() 差着十几毫秒的刻度, 不能被当成旧的)
    sup.step()
    assert sup.stop_all


def test_a_request_that_cannot_be_read_yet_is_kept_and_executed_once(tmp_path, monkeypatch):
    """读不出来 (被别的程序开着) 的请求先留着, 下一轮再读, 读成了就执行一次; 原来读不出来就当空请求删掉, 请求丢了."""
    sup = _sup(tmp_path)
    req = _stop_all(sup.home)
    reads = {"n": 0, "fails": 2}
    _refuse_read(monkeypatch, reads)
    sup.step()
    sup.step()  # 两遍都读不出来: 留着, 不执行
    assert req.exists() and not sup.stop_all and not _said(tmp_path / "logs")
    sup.step()  # 读成了: 执行
    assert sup.stop_all and not req.exists() and reads["n"] == 3
    sup.stop_all = False
    sup.step()  # 不会第二次
    assert not sup.stop_all and not _said(tmp_path / "logs")


def test_a_request_that_stays_unreadable_is_dropped_after_a_while(tmp_path, monkeypatch):
    """一直读不出来: 超过 REQ_READ_S 以后记一行、删掉、不执行."""
    monkeypatch.setattr(S, "REQ_READ_S", 0.3, raising=False)
    sup = _sup(tmp_path)
    req = _stop_all(sup.home)
    _refuse_read(monkeypatch, {"n": 0, "fails": None})
    sup.step()
    assert req.exists() and not sup.stop_all and not _said(tmp_path / "logs")  # 先留着
    time.sleep(0.4)
    sup.step()
    assert not req.exists() and not sup.stop_all  # 超时: 删掉, 不执行
    said = _said(tmp_path / "logs")
    assert len(said) == 1 and req.name in said[0] and "读不出来" in said[0]
    sup.step()
    assert len(_said(tmp_path / "logs")) == 1


def test_an_executed_request_that_cannot_be_deleted_is_never_executed_again(tmp_path, monkeypatch):
    """执行了但文件删不掉 (被别的程序开着): 记住名字, 以后不再执行, 每轮再试着删, 删掉了就从名单里去掉; 原来 unlink 抛出去 step() 就崩."""
    sup = _sup(tmp_path)
    req = _stop_all(sup.home)
    refuse = _refuse_unlink(monkeypatch, {req.name})
    sup.step()  # 执行了, 删不掉
    assert sup.stop_all and req.exists()
    said = _said(tmp_path / "logs")
    assert len(said) == 1 and req.name in said[0] and "删不掉" in said[0]
    sup.stop_all = False
    for _ in range(3):
        sup.step()  # 不会第二次执行, 也不再记
    assert not sup.stop_all and req.exists() and len(_said(tmp_path / "logs")) == 1
    refuse.clear()  # 能删了: 删掉, 还是不执行
    sup.step()
    assert not req.exists() and not sup.stop_all and req.name not in sup._dead


@pytest.mark.parametrize("body", ["[]", '{"op": "stop", "names": "tts"}', '{"op": "stop", "names": [1, ["x"]]}',
                                  '{"op": "reboot", "names": []}'])
def test_a_request_with_wrong_content_is_dropped_without_a_crash(tmp_path, body):
    """内容不对 (不是对象 / names 不是名字的列表 / op 不认识): 不抛, 不执行, 删掉, 记一行."""
    sup = _sup(tmp_path)
    req = sup.home / "req-bad0000001.json"
    req.write_text(body, encoding="utf-8")
    sup.step()
    assert not sup.stop_all and not req.exists()
    said = _said(tmp_path / "logs")
    assert len(said) == 1 and req.name in said[0] and "不执行" in said[0]


def test_a_request_nested_too_deep_to_parse_is_kept_then_dropped_like_an_unreadable_one(tmp_path, monkeypatch):
    """嵌套得极深的内容: json.loads 抛 RecursionError (不是 ValueError) —— 原来每一圈都从 _requests 抛出去, 这个文件又一直删不掉,
    一圈圈作废、连着 LOOP_FAIL_MAX 圈以后看守退出. 要和读不出来的一样: 先留着, 超过 REQ_READ_S 记一行、删掉、不执行."""
    monkeypatch.setattr(S, "REQ_READ_S", 0.3, raising=False)
    sup = _sup(tmp_path)
    req = sup.home / "req-deep000001.json"
    req.write_text("[" * 5000, encoding="utf-8")
    sup.step()
    assert req.exists() and not sup.stop_all and not _said(tmp_path / "logs")  # 不抛, 先留着
    time.sleep(0.4)
    sup.step()
    assert not req.exists() and not sup.stop_all  # 超时: 删掉, 不执行
    said = _said(tmp_path / "logs")
    assert len(said) == 1 and req.name in said[0] and "读不出来" in said[0] and "RecursionError" in said[0]
    sup.step()
    assert len(_said(tmp_path / "logs")) == 1


def test_checking_a_request_cannot_be_broken_by_a_deeply_nested_op():
    """op 是嵌套得极深的东西: 检查内容时不能对它取 str() (会抛 RecursionError), 照样判成 op 不认识."""
    op = []
    for _ in range(5000):
        op = [op]
    why = S._bad_request({"op": op, "names": []})
    assert "op 不认识" in why and "list" in why


def test_valid_requests_still_run_once_each_in_name_order(tmp_path):
    """执行的顺序和次数照原样: 按文件名排, 一个一次. 先 restart 再 stop, 最后是 stopped (反过来就是 off)."""
    sup = _one(tmp_path, [sys.executable, "-c", "pass"])
    (sup.home / "req-aaaa000000.json").write_text(json.dumps({"op": "restart", "names": ["tts"]}), encoding="utf-8")
    (sup.home / "req-bbbb000000.json").write_text(json.dumps({"op": "stop", "names": ["tts"]}), encoding="utf-8")
    sup.step()
    assert sup.st["tts"]["state"] == "stopped" and not list(sup.home.glob("req-*.json")) and not _said(tmp_path / "logs")


def _boom(exc):
    raise exc  # 同一个错 (类型加出错位置) 都是从这一行出来的


def _scripted(sup, script):
    """sup.step 换成照剧本走: 一圈一圈取, 异常就抛 (从 _boom 那一行), None 就是这一圈好好的; 剧本走完 (再来一圈) 让看守停下."""
    items = list(script)

    def step():
        if not items:
            sup.stop_all = True
            return
        item = items.pop(0)
        if item is not None:
            _boom(item)
    sup.step = step


def test_a_round_that_fails_is_voided_and_the_next_round_runs(tmp_path):
    """一圈 step() 抛了没接住的错: run() 不抛、不退出, 下一圈照常往下跑; 日志里记出错位置 (带 traceback); 同一个错重复不刷屏, 换了个错马上记."""
    sup = _sup(tmp_path)
    _scripted(sup, [ValueError("一"), None, ValueError("二"), ValueError("三"), TypeError("四"), None])
    assert sup.run() == "stop"
    text = "\n".join(_said(tmp_path / "logs"))
    assert text.count("看守这一圈出错") == 2  # ValueError 记一次 (后面两次同一个错, 不刷屏), TypeError 换了个错马上记
    assert "ValueError: 一" in text and "TypeError: 四" in text and "ValueError: 二" not in text and "ValueError: 三" not in text
    assert "_boom" in text and "test_services.py" in text  # 出错位置 (traceback)
    assert S.read_state(sup.home)["stopping"] is True  # 收尾照样存了状态


def test_the_same_error_is_logged_at_most_once_a_minute_with_a_count(tmp_path):
    """同一个错 (类型加出错位置) 重复时最多每 LOOP_SAY_S 秒记一行、带次数."""
    sup = _sup(tmp_path)
    errs = []
    for _ in range(5):
        try:
            _boom(ValueError("x"))
        except ValueError as exc:
            errs.append(exc)
    for i, exc in enumerate(errs, 1):
        sup._round_failed(exc, i)  # 5 次同一个错: 只记第一次
    text = "\n".join(_said(tmp_path / "logs"))
    assert text.count("看守这一圈出错") == 1 and "又出了" not in text
    key = next(iter(sup._errs))
    sup._errs[key][0] -= 61  # 过了 60 秒
    sup._round_failed(errs[0], 6)
    text = "\n".join(_said(tmp_path / "logs"))
    assert "又出了 5 次" in text and key[0] in text  # 悄悄过去的 4 次加这一次
    sup._round_failed(errs[0], 7)  # 刚记过: 又不记了
    assert "\n".join(_said(tmp_path / "logs")).count("又出了") == 1


def test_too_many_failed_rounds_in_a_row_end_the_supervisor_the_old_way(tmp_path, monkeypatch):
    """连着 LOOP_FAIL_MAX 圈都出错: 记一行 "连着 N 圈出错, 看守退出", 把最后那个错抛出去, 照现在的退出路走 (finally 照样存状态)."""
    monkeypatch.setattr(S, "LOOP_FAIL_MAX", 3, raising=False)
    sup = _sup(tmp_path)
    _scripted(sup, [ValueError("1"), ValueError("2"), ValueError("3"), None])
    with pytest.raises(ValueError, match="3"):
        sup.run()
    assert "连着 3 圈出错, 看守退出" in "\n".join(_said(tmp_path / "logs"))
    assert S.read_state(sup.home)["stopping"] is False and (sup.home / "state.json").exists()


def test_a_good_round_in_between_starts_the_count_over(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "LOOP_FAIL_MAX", 3, raising=False)
    sup = _sup(tmp_path)
    _scripted(sup, [ValueError("a"), ValueError("b"), None, ValueError("c"), ValueError("d"), None])
    assert sup.run() == "stop"  # 中间有一圈好好的, 重新数: 没有连着 3 圈


def test_ctrl_c_and_exit_are_not_swallowed_by_the_round_guard(tmp_path):
    """兜底只接 Exception: Ctrl+C 照旧返回 interrupted, SystemExit 照旧往外走 (旧代码也是这样, 这条是护栏)."""
    sup = _sup(tmp_path / "a")
    _scripted(sup, [None, KeyboardInterrupt()])
    assert sup.run() == "interrupted"
    sup = _sup(tmp_path / "b")
    _scripted(sup, [None, SystemExit(3)])
    with pytest.raises(SystemExit):
        sup.run()


@pytest.mark.parametrize("cmd, exc_name", [([None], "TypeError"), (["python\0 -c pass"], "ValueError")])
def test_a_command_that_is_written_wrong_is_a_start_that_failed(tmp_path, cmd, exc_name):
    """命令写错了 (里面有 None、空字符): 和程序不在一样当"起不来", 记 down、按退避走, 到次数变 failed —— 不能靠 run() 的兜底每圈作废."""
    sup = _one(tmp_path, cmd, restart_max=1)
    sup.step()  # 不抛
    s = sup.st["tts"]
    assert s["state"] == "down" and exc_name in s["error"] and not sup.proc and s["pid"] is None
    assert len(_said(tmp_path / "logs")) == 1
    for p in _service_logs(tmp_path):
        p.unlink()  # 开着的日志关了
    s["since"] -= 3600
    sup.step()  # 唯一的一次重启也没起来
    assert s["state"] == "down" and len(s["restarts"]) == 1
    sup.step()
    assert s["state"] == "failed" and len(_said(tmp_path / "logs")) == 2


def test_a_stop_command_that_cannot_run_still_ends_the_process(tmp_path, stuck, kills):
    """停止命令跑不起来 (程序不在、被拦着): 记一行, 接着直接结束进程 (和等不到它自己停一样), 不抛."""
    sup, p, f = stuck
    sup.specs["tts"].stop_cmd = _no_program(tmp_path)
    S.request("stop", ["tts"], tmp_path / "home")
    sup.step()
    assert sup.st["tts"]["state"] == "stopped" and "tts" not in sup.proc and f.closed and kills == [p.pid]
    assert any("停止命令" in ln and "tts" in ln and "FileNotFoundError" in ln for ln in _said(tmp_path / "logs"))


def test_ctrl_c_with_a_stop_command_that_cannot_run_still_returns_interrupted(tmp_path, stuck, kills):
    sup, p, f = stuck
    sup.specs["tts"].stop_cmd = _no_program(tmp_path)

    def interrupted():
        raise KeyboardInterrupt
    sup.step = interrupted
    assert sup.run() == "interrupted"
    assert "tts" not in sup.proc and f.closed and kills == [p.pid]
    assert any("停止命令" in ln for ln in _said(tmp_path / "logs"))


@pytest.mark.parametrize("body", ["[]", '"x"', "0", "null", pytest.param("[" * 5000, id="nested-too-deep")])
def test_serve_watchdog_alive_treats_a_json_that_is_not_an_object_as_unreadable(tmp_path, monkeypatch, body):
    """serve.json 是合法 JSON 但不是对象、或嵌套得太深解析不了 (RecursionError): 和读不出来一样返回 False
    (原来 st.get 抛 AttributeError、json.loads 抛 RecursionError, step() 里 spec.elsewhere 就崩); 对象照旧认."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".worldmodel" / "serve"
    d.mkdir(parents=True)
    (d / "serve.json").write_text(body, encoding="utf-8")
    assert S.serve_watchdog_alive() is False
    (d / "serve.json").write_text(json.dumps({"supervisor": os.getpid()}), encoding="utf-8")
    assert S.serve_watchdog_alive() is True

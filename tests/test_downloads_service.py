"""下载服务当成独立进程跑 (wm/downloads/daemon.py、client.py): 真的起进程 —— 假下载站一个、小看守一个、下载服务一个,
都在临时目录里, 不连外网. 验收里的: 杀掉下载服务, 看守 10 秒内拉起来并从断点接着下、哈希对; 宿主没了下载不断;
挂太多次不再拉并且看得到; 没活自己退、要用时再起来."""
import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aiohttp
import psutil
import pytest

from dl_fakes import blob, read_log, sha256
from wm.downloads import DownloadError, real_home, summary
from wm.downloads import client as C
from wm.downloads import daemon as D

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FAST = {"port": 0, "idle_exit_s": 4, "max_active": 2, "checkpoint_s": 0.2, "retry_delays": [0.2, 0.2, 0.2, 0.2],
        "read_timeout_s": 5, "watch_tick": 0.2}


def kill(pid) -> None:
    try:
        p = psutil.Process(int(pid))
        for child in p.children(recursive=True):
            child.kill()
        p.kill()
    except (psutil.Error, TypeError, ValueError):
        pass


def downloads_procs(home: Path, word: str) -> list[int]:
    """命令行里有 `wm.downloads <word>` 和这个数据目录的进程 (word: watch 小看守 / serve 下载服务)."""
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        line = " ".join(p.info.get("cmdline") or [])
        if f"wm.downloads {word}" in line and str(home) in line and p.info["pid"] != os.getpid():
            out.append(p.info["pid"])
    return out


@pytest.fixture
def stage(tmp_path, monkeypatch):
    """假下载站 (单独的进程, 限速) + 一个指向临时目录的 Client. 结束时把起出来的进程都收掉."""
    monkeypatch.setattr(C, "SPAWN", True)
    procs, made = [], {}

    def site(files: dict[str, tuple[int, int]], rate: int = 0) -> int:
        log = tmp_path / "site.jsonl"
        cmd = [sys.executable, str(HERE / "dl_fakes.py"), "--port", "0", "--rate", str(rate), "--log", str(log)]
        for name, (size, seed) in files.items():
            cmd += ["--file", f"{name}:{size}:{seed}"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
        procs.append(p)
        port = int(p.stdout.readline().split()[1])
        monkeypatch.setenv(D.TEST_ALLOW_ENV, f"127.0.0.1:{port}")  # 小看守和下载服务从环境变量里拿到放行名单
        made.update(port=port, log=log)
        return port

    def client(**conf) -> C.Client:
        made["client"] = C.Client(tmp_path / "serve" / "downloads", tmp_path / "files", {**FAST, **conf})
        return made["client"]

    made.update(site=site, new_client=client, tmp=tmp_path)
    yield made
    home = tmp_path / "serve" / "downloads"
    for pid in (D.read_json(home / "watch" / "state.json").get("pid"), D.read_json(home / "daemon.json").get("pid")):
        if pid and pid != os.getpid():
            kill(pid)
    for p in procs:
        p.kill()
    # 兜底: 数据目录在这个临时目录里的小看守和下载服务, 不管起了几个、进程号记在哪, 都收掉 (先看守, 不然它会把服务再拉起来)
    for word in ("watch", "serve"):
        for pid in downloads_procs(home, word):
            kill(pid)


async def until(pred, timeout=30.0, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        got = await got if asyncio.iscoroutine(got) else got
        if got:
            return got
        await asyncio.sleep(step)
    raise AssertionError("等不到")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- 验收 2 (独立进程): 杀掉服务, 看守 10 秒内拉起, 从断点接着下
def test_killed_service_is_back_within_10s_and_resumes_from_the_breakpoint(stage):
    files = {f"f{i}.bin": (500_000, i) for i in (1, 2, 3)}
    port = stage["site"](files, rate=160_000)
    c = stage["new_client"]()

    async def main():
        try:
            for name in files:
                got = await c.rpc("enqueue", args={"urls": [f"http://127.0.0.1:{port}/{name}"], "name": name,
                                                   "sha256": sha256(blob(*files[name]))}, session="sess-a")
                assert got["job"]["state"] == "awaiting"
            await asyncio.sleep(0.5)
            assert read_log(stage["log"]) == []  # 排了 3 个, 没点头: 假下载站一个请求都没收到
            for i in (1, 2, 3):
                await c.rpc("answer", ref=f"D{i}", ok=True, via="test")

            async def halfway():
                jobs = (await c.rpc("status"))["jobs"]
                return any(j["id"] == "D1" and j["done"] >= 180_000 for j in jobs)
            await until(halfway)
            first = D.daemon_info(c.home)
            before = {p.name: p.stat().st_size for p in (stage["tmp"] / "files").glob("*.part")}
            assert before and all(0 < n < 500_000 for n in before.values())
            psutil.Process(first["pid"]).kill()  # 下到一半, 下载服务被杀了
            t0 = time.monotonic()

            async def back():
                info = D.daemon_info(c.home)
                return info if info and info["pid"] != first["pid"] and await c._healthy(info) else None
            again = await until(back, timeout=20, step=0.05)
            took = time.monotonic() - t0
            assert took < 10.0, f"看守用了 {took:.1f} 秒才把下载服务拉起来"
            assert again["token"] != first["token"]  # 新起的换了令牌

            async def all_done():
                jobs = (await c.rpc("status"))["jobs"]
                return jobs if len(jobs) == 3 and all(j["state"] == "done" for j in jobs) else None
            jobs = await until(all_done, timeout=40)
            return took, jobs
        finally:
            await c.close()

    took, jobs = asyncio.run(main())
    for name, (size, seed) in files.items():  # 三个都下完了, 内容和哈希都对
        assert digest(stage["tmp"] / "files" / name) == sha256(blob(size, seed))
    assert all(j["final"]["verified"] for j in jobs)
    rows = [r for r in read_log(stage["log"]) if r["ev"] == "request"]
    resumed = [r for r in rows if r["range"] and r["status"] == 206]
    assert len(resumed) >= 2 and all(int(r["range"][6:-1]) > 0 for r in resumed)  # 杀之前在下的两个: 都是带着断点回来要的
    assert sum(1 for r in rows if not r["range"]) == 3  # 每个文件只从头要过一次
    w = D.watch_info(Path(stage["client"].home))
    assert w["restarts"] == 1 and w["state"] in ("up", "down", "off")
    print(f"\n[下载服务被杀后 {took:.1f} 秒回来; 续传请求 {[r['range'] for r in resumed]}]")


# ---------------------------------------------------------------- 宿主没了, 下载不断 (重启 8790 不带走它)
HOST = """
import asyncio, sys
from wm.downloads import client as C
async def main():
    c = C.Client(sys.argv[1], sys.argv[2], __import__('json').loads(sys.argv[3]))
    got = await c.rpc("enqueue", args={"urls": [sys.argv[4]], "name": "大文件", "sha256": sys.argv[5]}, session="sess-a")
    await c.rpc("answer", ref=got["job"]["id"], ok=True, via="test")
    print("started", flush=True)
    await asyncio.sleep(600)
asyncio.run(main())
"""


def test_download_keeps_going_when_the_host_process_dies(stage):
    port = stage["site"]({"big.bin": (900_000, 7)}, rate=150_000)
    c = stage["new_client"]()
    host = subprocess.Popen([sys.executable, "-c", HOST, str(c.home), str(c.root), json.dumps(c.conf),
                             f"http://127.0.0.1:{port}/big.bin", sha256(blob(900_000, 7))], cwd=str(ROOT), stdout=subprocess.PIPE, text=True)
    try:
        assert host.stdout.readline().strip() == "started"  # 另一个进程 (当成 8790) 排了下载、替你点了头
        time.sleep(1.0)
        kill(host.pid)  # 宿主没了 —— 连它的进程树一起结束 (比重启 8790 更狠; 小看守和下载服务不算它的子进程)
        host.wait(10)
        part = stage["tmp"] / "files" / "big.bin.part"
        a = part.stat().st_size
        time.sleep(1.5)
        b = part.stat().st_size if part.exists() else 900_000
        assert b > a  # 宿主不在的时候还在下

        async def main():
            try:
                job = (await c.rpc("wait", timeout=40, id="D1", wait_s=30))["job"]  # 新的宿主回来, 等到它下完
                changes = await c.rpc("changes", since=0)
                return job, changes
            finally:
                await c.close()
        job, changes = asyncio.run(main())
    finally:
        kill(host.pid)
    assert job["state"] == "done" and digest(stage["tmp"] / "files" / "big.bin") == sha256(blob(900_000, 7))
    rows = [r for r in read_log(stage["log"]) if r["ev"] == "request"]
    assert len(rows) == 1 and rows[0]["range"] == ""  # 从头到尾就一条连接: 没断过
    assert [(j["id"], j["report"]) for j in changes["jobs"]] == [("D1", "pending")]  # 结果留着, 等宿主回来交给发起的会话


# ---------------------------------------------------------------- 挂太多次不再拉, 并且看得到
def test_gives_up_after_repeated_crashes_and_tells_the_host(stage):
    # 2026-11-16: 以前拿"下载目录建不出来"当崩溃的办法; 现在下载文件夹写不进去时服务照样起来、入队时说明原因
    # (tests/test_downloads_location.py), 这里换成数据目录里的 jobs 是个文件: 队列一建就出错, 服务一起来就退出
    blocker = stage["tmp"] / "serve" / "downloads" / "jobs"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("这里是个文件, 任务记录的文件夹建不出来", encoding="utf-8")  # 下载服务一起来就出错退出
    c = stage["new_client"](restart_max=1)

    async def main():
        try:
            with pytest.raises(DownloadError) as e:
                await c.ensure()
            assert e.value.code == "service-down" and "起不来" in str(e.value)
            state = c.state()
            assert state["running"] is False and state["watch"]["state"] == "failed" and "挂了 1 次" in state["watch"]["error"]
            assert state["watch"]["running"] is False  # 小看守也退了, 不会一遍遍起
            t0 = time.monotonic()
            with pytest.raises(DownloadError) as e:  # 再要用: 马上说起不来, 不再起
                await c.rpc("status")
            assert time.monotonic() - t0 < 2.0 and "先不再起了" in str(e.value)
            blocker.unlink()  # 你修好了, 点"重新起"
            info = await c.restart()
            assert info["port"] and (await c.rpc("status"))["jobs"] == []
            await c.rpc("shutdown")
        finally:
            await c.close()
    asyncio.run(main())


# ---------------------------------------------------------------- 按需: 没活自己退, 要用时再起来; 只跑一个; 只认令牌
def test_single_instance_and_only_the_token_opens_the_door(stage):
    """同一个数据目录再起一个下载服务: 自己退出, 不抢; 不带令牌、令牌不对、Host 不对的请求都不给用.
    空闲退出设得很长 (60 秒): 第二个进程起得慢 (机器内存吃紧时要好几秒) 时, 第一个不能因为闲了先退, 不然锁空出来第二个就成了真的."""
    c = stage["new_client"](idle_exit_s=60)

    async def main():
        try:
            first = await c.ensure()
            assert c.state()["running"] and c.state()["watch"]["running"]
            second = subprocess.run([sys.executable, "-m", "wm.downloads", "serve", "--home", str(c.home), "--dir", str(c.root),
                                     "--port", "0", "--idle-exit", "3"], cwd=str(ROOT), capture_output=True, timeout=120)
            assert second.returncode == 0 and D.daemon_info(c.home)["pid"] == first["pid"]
            async with aiohttp.ClientSession() as s:
                url = f"http://127.0.0.1:{first['port']}/rpc"
                async with s.post(url, json={"op": "status"}) as r:
                    assert r.status == 401
                async with s.post(url, json={"op": "status"}, headers={"X-WM-Token": "x" * 43}) as r:
                    assert r.status == 401
                async with s.post(url, json={"op": "status"}, headers={"X-WM-Token": first["token"], "Host": "evil.example"}) as r:
                    assert r.status == 403
                async with s.get(f"http://127.0.0.1:{first['port']}/health") as r:
                    assert (await r.json())["service"] == "wm-downloads"
            assert (await c.rpc("info"))["dir"] == str(c.root)
            await c.rpc("shutdown")
        finally:
            await c.close()
    asyncio.run(main())


def test_idle_exit_and_wake_on_demand(stage):
    """没活、没人叫: 下载服务和小看守都自己退; 再要用时自己起来."""
    c = stage["new_client"](idle_exit_s=2)

    async def main():
        try:
            assert c.state()["running"] is False and summary(c.home) == {"active": 0, "awaiting": 0, "unreported": 0}
            first = await c.ensure()
            assert c.state()["running"] and c.state()["watch"]["running"]
            await until(lambda: not D.daemon_info(c.home) and not D.watch_info(c.home)["running"], timeout=60)  # 没活: 都退了
            again = await c.rpc("info")  # 再要用: 自己起来
            assert again["pid"] != first["pid"]
            await c.rpc("shutdown")
        finally:
            await c.close()
    asyncio.run(main())


def test_only_one_watcher_and_one_service_run_per_data_dir(stage):
    """机器忙时宿主等不到状态文件就会再起一个小看守 (2026-11-11 全套测试里留下过一对): 连着起三个, 只留一个看守、一个下载服务."""
    c = stage["new_client"](idle_exit_s=60)

    async def main():
        try:
            for _ in range(3):
                c._spawn_watch()
            await until(lambda: D.daemon_info(c.home) and True, timeout=40)
            await asyncio.sleep(3.0)  # 多起的那几个有时间抢锁、退出
            info = D.daemon_info(c.home)
            assert info and await c._healthy(info)
            # 读状态文件撞上看守正在"换"它的那一瞬会读不出来 (read_json 已经重读几次了, 这里再放宽一点: 等它读得到)
            w = await until(lambda: (lambda x: x if x["running"] else None)(D.watch_info(c.home)), timeout=10)
            assert w["restarts"] == 0
            return downloads_procs(c.home, "watch"), downloads_procs(c.home, "serve")
        finally:
            await c.close()
    watchers, services = asyncio.run(main())
    assert len(watchers) == 1 and len(services) == 1, (watchers, services)


def test_watcher_survives_a_failed_state_write_and_leaves_the_service_alone(stage, monkeypatch):
    """看守写状态文件失败 (Windows 上宿主、页面正好开着 state.json 在读, "写临时文件再换"换不了, 报 PermissionError): 看守照常在、
    下载服务不被结束. 原来 Supervisor.save 没接住, 看守崩了, 收尾还把正在下的下载服务一起结束了 (2026-11-11 评审时在这台机器上量过:
    一边反复换、一边反复读, 6 秒里换失败 244/378 次). 这里看守跑在测试进程的一个线程里, 下载服务是真起的子进程."""
    from wm import services as S
    c = stage["new_client"](idle_exit_s=120)
    home, writes, real = c.home, {"n": 0, "failed": 0}, S._write_json

    def flaky(path, obj):
        if Path(path).name == "state.json":
            writes["n"] += 1
            if writes["n"] > 3 and writes["failed"] < 8:  # 起好以后连着坏几次
                writes["failed"] += 1
                raise PermissionError(13, "拒绝访问: 别的进程正开着要被换掉的文件")
        return real(path, obj)
    monkeypatch.setattr(S, "_write_json", flaky)
    args = argparse.Namespace(home=str(home), dir=str(c.root), port=0, idle_exit=120.0, limits=None, tick=0.1, restart_max=5,
                              detach=False, test_allow=[])
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("code", D.watch(args)), daemon=True)

    async def main():
        t.start()
        info = {}
        try:
            info = await until(lambda: D.daemon_info(home) or None, timeout=90)  # 下载服务起来了
            await until(lambda: writes["failed"] >= 8, timeout=30)  # 状态文件连着写失败了 8 次
            assert t.is_alive() and out == {}  # 看守没崩
            assert psutil.pid_exists(info["pid"]) and D.daemon_info(home)["pid"] == info["pid"] and await c._healthy(info)  # 下载服务没被结束
            await until(lambda: D.watch_info(home)["state"] == "up", timeout=30)  # 后面照常写, 状态还是 up、没重启
            assert D.watch_info(home)["restarts"] == 0
        finally:
            S.request("stop", [], home / "watch")  # 要它停: 这是正当的停, 下载服务跟着结束
            await asyncio.to_thread(t.join, 60)
            await c.close()
        return info
    info = asyncio.run(main())
    assert not t.is_alive() and out == {"code": 0}
    assert not psutil.pid_exists(info["pid"])


def test_reading_the_state_file_over_and_over_does_not_take_the_watcher_down(stage):
    """评审时的实验: 一边反复"写临时文件再换"、一边反复读, 换失败 244/378 次、读空或出错 171/33,975 次. 这里用真的小看守 (tick 0.02
    秒, 写得比平时勤很多) 和一个一直在读状态文件的线程: 6 秒下来看守和下载服务还是原来那两个进程、状态一直是 up、一次没重启,
    读的那边也一次没有把"看守在跑"读成"不在"."""
    c = stage["new_client"](idle_exit_s=120, watch_tick=0.02)

    async def main():
        try:
            first = await c.ensure()
            watcher = (await until(lambda: (lambda x: x if x["running"] and x["state"] == "up" else None)(D.watch_info(c.home)), timeout=60))["pid"]
            stop, misses, reads = threading.Event(), [], [0]

            def hammer():
                while not stop.is_set():
                    reads[0] += 1
                    if not D.watch_info(c.home)["running"]:
                        misses.append(reads[0])
            th = threading.Thread(target=hammer, daemon=True)
            th.start()
            await asyncio.sleep(6.0)
            stop.set()
            th.join(20)
            w = D.watch_info(c.home)
            assert w["running"] and w["pid"] == watcher and w["state"] == "up" and w["restarts"] == 0
            assert D.daemon_info(c.home)["pid"] == first["pid"] and await c._healthy(D.daemon_info(c.home))
            assert misses == [], f"读了 {reads[0]} 次, 其中 {len(misses)} 次把看守读成了不在"
            return reads[0]
        finally:
            await c.close()
    print(f"\n[状态文件被读了 {asyncio.run(main())} 次, 看守没倒]")


def test_json_helpers_ride_out_a_file_that_is_busy_for_a_moment(tmp_path, monkeypatch):
    """读: 打不开 (PermissionError) 或读到空的就隔一会儿再读; 文件不在马上返回. 写: 换不了 (PermissionError) 就稍等再换, 一直不行抛出来、
    临时文件不留."""
    from wm import downloads as DQ
    p = tmp_path / "x.json"
    DQ.write_json(p, {"a": 1})
    real_read, seen = Path.read_text, {"n": 0}

    def flaky_read(self, *a, **k):
        if self == p:
            seen["n"] += 1
            if seen["n"] == 1:
                raise PermissionError(13, "拒绝访问")
            if seen["n"] == 2:
                return ""
        return real_read(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", flaky_read)
    assert DQ.read_json(p) == {"a": 1} and seen["n"] == 3  # 第 3 次读到了
    t0 = time.monotonic()
    assert DQ.read_json(tmp_path / "none.json") == {} and time.monotonic() - t0 < 0.04  # 不在: 不等

    def always_busy_read(self, *a, **k):
        if self == p:
            raise PermissionError(13, "拒绝访问")
        return real_read(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", always_busy_read)
    assert DQ.read_json(p, tries=3, wait=0.01) == {}  # 一直读不出来: 最多读 tries 次, 返回 {} (不抛)
    monkeypatch.setattr(Path, "read_text", real_read)

    real_replace, calls = os.replace, {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "拒绝访问")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", flaky_replace)
    DQ.write_json(p, {"a": 2})
    assert json.loads(p.read_text("utf-8")) == {"a": 2} and calls["n"] == 3  # 第 3 次换成了

    def always_busy_replace(src, dst):
        raise PermissionError(13, "拒绝访问")
    monkeypatch.setattr(os, "replace", always_busy_replace)
    with pytest.raises(PermissionError):
        DQ.write_json(p, {"a": 3}, tries=3)
    assert not list(tmp_path.glob("*.tmp")) and json.loads(p.read_text("utf-8")) == {"a": 2}  # 老的还在、临时文件不留


def test_a_finished_but_unreported_download_does_not_keep_the_service_alive(stage):
    """结束了还没回报的不算"有活": 记录在文件里, 宿主回来再叫醒服务取; 宿主要是一直不回来, 服务不用一直挂着."""
    data = blob(60_000, 31)
    port = stage["site"]({"u.bin": (60_000, 31)})
    c = stage["new_client"](idle_exit_s=2)

    async def main():
        try:
            await c.rpc("enqueue", args={"urls": [f"http://127.0.0.1:{port}/u.bin"], "name": "没人回报", "sha256": sha256(data)}, session="sess-a")
            await c.rpc("answer", ref="D1", ok=True, via="test")
            job = (await c.rpc("wait", timeout=40, id="D1", wait_s=30))["job"]  # 没带 viewer: 不算她看到了, 回报还挂着
            assert job["state"] == "done" and D.summary(c.home)["unreported"] == 1
            await until(lambda: not D.daemon_info(c.home) and not D.watch_info(c.home)["running"], timeout=30)  # 服务和看守都自己退了
            assert D.summary(c.home) == {"active": 0, "awaiting": 0, "unreported": 1}  # 记录还在
            got = await c.rpc("changes", since=0)  # 宿主回来: 叫醒服务, 没交出去的回报还在
            assert [(j["id"], j["report"]) for j in got["jobs"]] == [("D1", "pending")]
            assert (await c.rpc("ack", id="D1"))["acked"] is True
        finally:
            await c.close()
    asyncio.run(main())


class _Proxy502(BaseHTTPRequestHandler):
    """像 11-11 那天的代理: 对什么请求都回 502, 记下收到的."""

    def _any(self):
        self.server.hits.append(self.requestline)
        self.send_response(502, "Bad Gateway")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_CONNECT = do_HEAD = _any

    def log_message(self, *a):
        pass


def test_works_in_the_users_environment_where_a_proxy_would_hijack_loopback(stage, monkeypatch):
    """你的电脑上环境变量里有 http_proxy 却没有 no_proxy (Claude 会话里却有 NO_PROXY, 所以会话里测通不等于你那边通):
    认环境变量的客户端访问 127.0.0.1 会被送去代理, 代理回 502. 这里把环境改成你那样, 并且不让起子进程时补 no_proxy
    (with_local_bypass 换成原样返回), 看宿主 -> 下载服务、小看守查健康、下载服务 -> 下载站是不是全部直连: 假代理一个请求都不该收到."""
    from wm import localhttp
    hits = []
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), _Proxy502)
    proxy.hits = hits
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.setattr(localhttp, "with_local_bypass", lambda env: env)
        data = blob(150_000, 21)
        port = stage["site"]({"p.bin": (150_000, 21)})
        c = stage["new_client"]()
        with pytest.raises(OSError):  # 对照: 老写法 (urllib 认环境变量) 在这个环境里真的被劫走, 假代理收到了它的请求
            urllib.request.build_opener().open(f"http://127.0.0.1:{port}/p.bin", timeout=3)
        assert len(hits) == 1
        hits.clear()

        async def main():
            try:
                got = await c.rpc("enqueue", args={"urls": [f"http://127.0.0.1:{port}/p.bin"], "name": "代理环境下", "sha256": sha256(data)},
                                  session="sess-a")
                assert got["job"]["state"] == "awaiting"
                await c.rpc("answer", ref="D1", ok=True, via="test")
                job = (await c.rpc("wait", timeout=40, id="D1", wait_s=30))["job"]
                assert job["state"] == "done" and job["final"]["verified"]
                info = await c.rpc("info")
                assert info["proxy"] == f"http://127.0.0.1:{proxy.server_port}"  # 下载服务读到了环境里的代理 (外网的下载会走它)
                # 小看守查 /health 没被代理劫走: 它的状态会变成 up, 而且一次都没重启 (被劫走的话健康检查永远过不了, 停在 starting 再被判"起不来")
                w = await until(lambda: (lambda x: x if x["state"] == "up" else None)(D.watch_info(c.home)), timeout=15)
                assert w["running"] and w["restarts"] == 0
            finally:
                await c.close()
        asyncio.run(main())
        assert hits == []  # 假代理一个请求都没收到
        assert digest(stage["tmp"] / "files" / "p.bin") == sha256(data)
    finally:
        proxy.shutdown()
        proxy.server_close()


def test_test_allowance_is_ignored_on_the_real_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(D.TEST_ALLOW_ENV, "127.0.0.1:1234")
    assert D.test_allow(real_home()) == frozenset()  # 真的数据目录上不认测试放行
    assert D.test_allow(tmp_path, ["LOCALHOST:99"]) == frozenset({"127.0.0.1:1234", "localhost:99"})
    monkeypatch.delenv(D.TEST_ALLOW_ENV)
    assert D.test_allow(tmp_path) == frozenset()

"""下载队列 (wm/downloads/queue.py、transfer.py、rules.py): 队列在测试进程里直接跑, 对着本机的假下载站 (tests/dl_fakes.py).
不连外网, 不起别的进程. 验收里的: 没点头时传输量为 0; 停了再起从断点接着下、哈希对; 哈希不对的删掉标失败;
别的会话看得到停不了; 没列的来源、内网地址、下载目录外面的路径都不收."""
import asyncio
import json
import os
import subprocess

import pytest

from dl_fakes import FakeSite, blob, sha256
from wm.downloads import DownloadError, Limits, summary, wanted
from wm.downloads import rules as R
from wm.downloads import text as TX
from wm.downloads import transfer as TR
from wm.downloads.queue import USER, DownloadQueue

A, B = {"kind": "session", "id": "sess-a"}, {"kind": "session", "id": "sess-b"}
FAST = dict(retry_delays=(0.02, 0.02, 0.02), checkpoint_s=0.02, read_timeout_s=3.0)


class Bench:
    """一个假下载站 + 一个队列 (家目录和下载目录都在 tmp_path 里)."""

    def __init__(self, tmp_path, rate=0, **limits):
        self.tmp, self.site = tmp_path, FakeSite(rate=rate)
        self.home, self.root = tmp_path / "home", tmp_path / "files"
        self.limits = Limits(**{**FAST, **limits})
        self.q = None
        self.free = None

    async def __aenter__(self):
        await self.site.start()
        self.allow = frozenset({f"127.0.0.1:{self.site.port}"})
        await self.open()
        return self

    async def open(self):
        """(重新) 起一个队列, 同一个家目录 —— 就是下载服务重启."""
        self.q = DownloadQueue(self.home, self.root, self.limits, allow=self.allow, free=self.free)
        await self.q.start()
        return self.q

    async def __aexit__(self, *exc):
        if self.q is not None:
            await self.q.close()
        await self.site.stop()

    def put(self, url, session="sess-a", **args):
        return self.q.enqueue({"urls": [url] if isinstance(url, str) else url, "name": args.pop("name", "测试文件"), **args},
                              session=session, owner_title="测试会话", run="run-1")["job"]

    def ok(self, job, yes=True):
        return self.q.answer(job["ask"]["card"] if job.get("ask") else job["id"], yes, via="test")

    async def until(self, jid, *states, timeout=10.0):
        for _ in range(int(timeout / 0.01)):
            if self.q.jobs[jid]["state"] in states:
                return self.q.jobs[jid]
            await asyncio.sleep(0.01)
        raise AssertionError(f"{jid} 等不到 {states}, 现在是 {self.q.jobs[jid]['state']}: {self.q.jobs[jid].get('error')}")


def run(main):
    return asyncio.run(main())


# ---------------------------------------------------------------- 验收 1: 没点头时传输量为 0
def test_nothing_is_transferred_before_the_nod_and_it_starts_after(tmp_path):
    data = blob(300_000, 3)

    async def main():
        async with Bench(tmp_path) as b:
            job = b.put(b.site.add("model.bin", data), sha256=sha256(data), size=len(data), license="MIT")
            assert job["id"] == "D1" and job["state"] == "awaiting" and job["ask"]["why"] == "new"
            await asyncio.sleep(0.4)  # 没点头: 等多久都一样
            assert b.site.requests == [] and b.site.sent == 0  # 一个请求都没发
            assert not (b.root / "model.bin").exists() and not (b.root / "model.bin.part").exists()
            assert summary(b.home) == {"active": 0, "awaiting": 1, "unreported": 0} and not wanted(b.home)
            assert b.ok(job)["state"] in ("queued", "running")  # 点了头才开始
            done = await b.until("D1", "done")
            assert (b.root / "model.bin").read_bytes() == data
            assert done["final"] == {"bytes": len(data), "sha256": sha256(data), "verified": True}
            assert len(b.site.requests) == 1 and b.site.sent == len(data)
            assert done["approvals"][0]["ok"] is True and done["approvals"][0]["via"] == "test"
    run(main)


def test_denied_download_never_connects_and_is_reported(tmp_path):
    async def main():
        async with Bench(tmp_path) as b:
            job = b.put(b.site.add("a.bin", blob(1000)))
            assert b.ok(job, yes=False)["state"] == "denied"
            await asyncio.sleep(0.1)
            assert b.site.requests == [] and not list(b.root.glob("*"))
            got = await b.q.changes()
            assert [(j["id"], j["report"], j["report_info"]["how"]) for j in got["jobs"]] == [("D1", "pending", "denied")]
            assert "没同意" in got["jobs"][0]["report_info"]["text"]
            with pytest.raises(DownloadError) as e:  # 拒绝了的不能再"点头"
                b.q.answer("D1", True)
            assert e.value.code == "bad-state"
    run(main)


# ---------------------------------------------------------------- 验收 2: 停了再起, 从断点接着下, 哈希对
def test_service_restart_resumes_from_the_breakpoint(tmp_path):
    data = blob(600_000, 5)

    async def main():
        async with Bench(tmp_path, rate=400_000) as b:
            job = b.put(b.site.add("big.bin", data), sha256=sha256(data))
            b.ok(job)
            for _ in range(500):
                if b.q.jobs["D1"]["done"] >= 200_000:
                    break
                await asyncio.sleep(0.01)
            await b.q.close()  # 下到一半服务停了
            part = b.root / "big.bin.part"
            saved = json.loads((b.home / "jobs" / "D1.json").read_text("utf-8"))
            assert saved["state"] == "running" and 0 < saved["checkpoint"] <= part.stat().st_size < len(data)
            q = await b.open()  # 再起来: 不用再点头, 自己接着下
            assert q.jobs["D1"]["state"] in ("queued", "running")
            done = await b.until("D1", "done")
            assert (b.root / "big.bin").read_bytes() == data and done["final"]["verified"] and not part.exists()
            first, second = b.site.gets("big.bin")
            assert first["range"] == "" and first["status"] == 200
            start = int(second["range"].removeprefix("bytes=").rstrip("-"))
            assert second["status"] == 206 and start == saved["checkpoint"] > 0 and second["if_range"]  # 从断点要, 带着上次的 ETag
            assert b.site.sent < len(data) * 1.6  # 没有从头再下一遍 (第一次发出去的有一截没收到, 所以比文件大一点)
    run(main)


def test_broken_connection_and_busy_server_are_retried_with_range(tmp_path):
    data = blob(200_000, 9)

    async def main():
        async with Bench(tmp_path) as b:
            b.ok(b.put(b.site.add("flaky.bin", data, drop_after=70_000, fail_times=1), sha256=sha256(data)))
            done = await b.until("D1", "done")
            assert (b.root / "flaky.bin").read_bytes() == data and done["error"] == ""
            statuses = [r["status"] for r in b.site.gets("flaky.bin")]
            assert statuses[0] == 503 and statuses[1] == 200 and set(statuses[2:]) == {206}  # 先忙, 再断了两次, 每次接着要
            assert all(r["range"] for r in b.site.gets("flaky.bin")[2:])
    run(main)


def test_without_validators_or_a_hash_an_interrupted_download_starts_over(tmp_path):
    """那边既不给 ETag 也不给 Last-Modified、她也没给哈希: 被打断以后没法确认续上的还是同一个文件, 不冒险拼接, 从头下;
    有哈希的 (拼坏了下完核对得出来) 照样带着 Range 续传."""
    data = blob(150_000, 4)

    async def interrupted(b, name, **put):
        job = b.put(b.site.add(name, data, etag=""), **put)
        jid = job["id"]
        b.ok(job)
        for _ in range(300):
            if b.q.jobs[jid]["checkpoint"] >= 40_000:
                break
            await asyncio.sleep(0.01)
        await b.q.pause(jid, A)  # 下到一半停一下, 再继续: 和断线以后接着下走同一条路
        assert 0 < b.q.jobs[jid]["checkpoint"] < len(data)
        b.q.resume(jid, A)
        await b.until(jid, "done")
        return b.site.gets(name)

    async def main():
        async with Bench(tmp_path, rate=300_000) as b:
            plain = await interrupted(b, "nov.bin")
            assert len(plain) == 2 and all(not r["range"] for r in plain)  # 两次都是从头要, 没有拼接
            assert (b.root / "nov.bin").read_bytes() == data
            hashed = await interrupted(b, "hash.bin", sha256=sha256(data))
            assert len(hashed) == 2 and not hashed[0]["range"] and hashed[1]["range"]  # 有哈希: 续传
            assert (b.root / "hash.bin").read_bytes() == data
    run(main)


def test_changed_upstream_file_and_no_range_server_start_over(tmp_path):
    old, new = blob(150_000, 1), blob(150_000, 2)

    async def main():
        async with Bench(tmp_path, rate=300_000) as b:
            url = b.site.add("v.bin", old)
            b.ok(b.put(url))
            for _ in range(300):
                if b.q.jobs["D1"]["done"] >= 40_000:
                    break
                await asyncio.sleep(0.01)
            await b.q.pause("D1", A)
            b.site.add("v.bin", new)  # 那边的文件换了 (ETag 也变了)
            b.q.resume("D1", A)
            await b.until("D1", "done")
            assert (b.root / "v.bin").read_bytes() == new  # If-Range 没对上, 服务器从头给, 这边也从头写: 不是新旧拼在一起
            assert b.site.gets("v.bin")[-1]["status"] == 200
            url2 = b.site.add("n.bin", old, no_range=True, drop_after=60_000)
            b.ok(b.put(url2))
            await asyncio.sleep(0.3)
            b.site.files["n.bin"]["drop_after"] = None
            await b.until("D2", "done")
            assert (b.root / "n.bin").read_bytes() == old
    run(main)


# ---------------------------------------------------------------- 验收 3: 哈希不对的删掉、标失败
def test_wrong_hash_deletes_the_file_and_fails(tmp_path):
    data = blob(50_000)

    async def main():
        async with Bench(tmp_path) as b:
            b.ok(b.put(b.site.add("x.bin", data), sha256="0" * 64))
            job = await b.until("D1", "failed")
            assert job["error_kind"] == "hash" and "对不上" in job["error"] and "已经删掉" in job["error"]
            assert not (b.root / "x.bin").exists() and not (b.root / "x.bin.part").exists()
            with pytest.raises(DownloadError) as e:  # 接着下也一样, 不让续
                b.q.resume("D1", A)
            assert e.value.code == "bad-state"
            info = (await b.q.changes())["jobs"][0]["report_info"]
            assert info["how"] == "failed" and "没成" in info["text"]
        with pytest.raises(DownloadError) as e:
            R.check_sha256("abc")
        assert e.value.code == "bad-args" and R.check_sha256("SHA256:" + "A" * 64) == "a" * 64
    run(main)


# ---------------------------------------------------------------- 同一个文件不下两次
def test_same_destination_and_source_is_not_downloaded_twice(tmp_path):
    data = blob(40_000)

    async def main():
        async with Bench(tmp_path) as b:
            url = b.site.add("same.bin", data)
            other = b.site.add("other.bin", data)
            first = b.put(url, dest="pack/same.bin")
            again = b.q.enqueue({"urls": [url], "name": "别的会话也要", "dest": "PACK\\same.bin"}, session="sess-b")
            assert again["deduplicated"] and again["job"]["id"] == first["id"] and again["job"]["mine"] is False
            with pytest.raises(DownloadError) as e:  # 同一个位置、另一个来源: 谁赢要人定
                b.put(other, dest="pack/same.bin")
            assert e.value.code == "dest-in-use"
            b.ok(first)
            await b.until("D1", "done")
            done = b.q.enqueue({"urls": [url], "name": "再来", "dest": "pack/same.bin"}, session="sess-b")
            assert done["deduplicated"] and done["job"]["state"] == "done" and len(b.site.requests) == 1  # 文件还在: 不重下
            with pytest.raises(DownloadError) as e:  # 位置上已经有文件 (别的来源): 不覆盖
                b.put(other, dest="pack/same.bin")
            assert e.value.code == "dest-exists"
            assert b.put(url, dest="pack/copy.bin")["id"] == "D2"  # 换个位置: 就是要两份
            (b.root / "pack" / "same.bin").unlink()
            assert b.put(url, dest="pack/same.bin")["id"] == "D3"  # 文件没了: 老老实实重新排 (要再点头)
            assert b.q.jobs["D3"]["state"] == "awaiting"
    run(main)


# ---------------------------------------------------------------- 验收 4: 别的会话看得到但停不了
def test_other_sessions_see_the_queue_but_only_the_owner_controls(tmp_path):
    async def main():
        async with Bench(tmp_path, rate=100_000) as b:
            b.ok(b.put(b.site.add("slow.bin", blob(400_000))))
            await b.until("D1", "running")
            seen = b.q.status(viewer="sess-b")["jobs"]
            assert [(j["id"], j["mine"], j["owner"], j["owner_title"]) for j in seen] == [("D1", False, "sess-a", "测试会话")]
            assert b.q.status("D1", viewer="sess-a")["jobs"][0]["mine"] is True
            for call in (lambda: b.q.pause("D1", B), lambda: b.q.cancel("D1", B)):
                with pytest.raises(DownloadError) as e:
                    await call()
                assert e.value.code == "not-owner" and "别的会话" in str(e.value)
            with pytest.raises(DownloadError) as e:
                b.q.resume("D1", B)
            assert e.value.code == "not-owner" and b.q.jobs["D1"]["state"] == "running"  # 还在下
            assert b.q.cleanup("all", B)["removed_records"] == 0
            for _ in range(300):
                if b.q.jobs["D1"]["done"] > 0:
                    break
                await asyncio.sleep(0.01)
            assert (await b.q.pause("D1", A))["state"] == "paused"  # 发起的会话可以
            part = b.root / "slow.bin.part"
            size = part.stat().st_size
            await asyncio.sleep(0.2)
            assert part.stat().st_size == size  # 真的停了
            assert b.q.resume("D1", A)["state"] in ("queued", "running")
            assert (await b.q.pause("D1", USER))["pause_by"] == "user"  # 你暂停的
            with pytest.raises(DownloadError) as e:
                b.q.resume("D1", A)
            assert e.value.code == "not-owner" and "主人暂停的" in str(e.value)
            assert b.q.resume("D1", USER)["state"] in ("queued", "running")
            assert (await b.q.cancel("D1", A))["state"] == "cancelled" and not part.exists()
            assert b.q.jobs["D1"]["report"] == "none"  # 她自己取消的不用再回报
            with pytest.raises(DownloadError) as e:
                b.q._get("D9")
            assert e.value.code == "not-found"
    run(main)


# ---------------------------------------------------------------- 验收 5: 没列的来源、内网地址、目录外面的路径
def test_redirect_to_an_unlisted_source_stops_and_asks(tmp_path):
    data = blob(80_000, 4)

    async def main():
        cdn = FakeSite()
        await cdn.start()
        try:
            async with Bench(tmp_path) as b:
                b.q.allow = b.allow = frozenset(b.allow | {f"127.0.0.1:{cdn.port}"})
                await b.q.close()
                await b.open()
                target = cdn.add("real.bin", data)
                job = b.put(b.site.add("go.bin", b"", redirect=target), sha256=sha256(data))
                b.ok(job)
                job = await b.until("D1", "awaiting")
                assert job["ask"]["why"] == "redirect" and job["ask"]["host"] == f"127.0.0.1:{cdn.port}" and job["ask"]["seq"] == 2
                assert cdn.requests == [] and cdn.sent == 0  # 没列的来源: 一个请求都没去
                row = (await b.q.changes())["jobs"][0]
                assert "卡片上没列的来源" in row["card_text"] and f"127.0.0.1:{cdn.port}" in row["card_short"]
                b.q.answer(job["ask"]["card"], True)
                await b.until("D1", "done")
                assert (b.root / "go.bin").read_bytes() == data and len(cdn.requests) == 1
                # 入队时就写明会转到哪 (卡片上列了): 不用再问
                b.ok(b.put(b.site.add("go2.bin", b"", redirect=target), redirect_hosts=[f"127.0.0.1:{cdn.port}"]))
                assert (await b.until("D2", "done"))["approvals"][-1]["seq"] == 1
                # 转去内网地址 (不在放行名单里的本机端口): 不跟
                b.ok(b.put(b.site.add("evil.bin", b"", redirect="http://127.0.0.1:9/secret")))
                bad = await b.until("D3", "failed")
                assert bad["error_kind"] == "private" and "不收" in bad["error"]
        finally:
            await cdn.stop()
    run(main)


@pytest.mark.parametrize("url, code", [
    ("http://127.0.0.1/a.bin", "private-address"), ("http://localhost:8790/api/status", "private-address"),
    ("http://192.168.1.5/a", "private-address"), ("http://10.0.0.8:8080/a", "private-address"), ("http://[::1]/a", "private-address"),
    ("http://169.254.169.254/latest/meta-data", "private-address"), ("http://2130706433/a", "private-address"),
    ("http://0x7f000001/a", "private-address"), ("http://127.1/a", "private-address"), ("http://a.localhost/x", "private-address"),
    ("ftp://example.com/a.bin", "url-invalid"), ("file:///C:/Windows/win.ini", "url-invalid"), ("example.com/a.bin", "url-invalid"),
    ("https://user:pw@example.com/a.bin", "url-invalid"), ("https://exa mple.com/a", "url-invalid"), ("", "url-invalid"),
])
def test_private_and_malformed_addresses_are_refused_at_the_door(tmp_path, url, code):
    q = DownloadQueue(tmp_path / "home", tmp_path / "files")
    with pytest.raises(DownloadError) as e:
        q.enqueue({"urls": [url], "name": "x"}, session="sess-a")
    assert e.value.code == code and not q.jobs  # 连卡片都不弹


def test_resolved_private_addresses_are_refused_when_connecting():
    async def main():
        assert "只下公网" in await TR.public("http://127.0.0.1:9/x", frozenset())
        assert await TR.public("http://127.0.0.1:9/x", frozenset({"127.0.0.1:9"})) is None  # 测试放行名单
        guard = TR.GuardedResolver()
        with pytest.raises(OSError):
            await guard.resolve("localhost", 80)
        assert await TR.GuardedResolver(frozenset({"localhost:80"})).resolve("localhost", 80)
    run(main)
    assert R.check_url("https://Example.com/a/b.bin?x=1#frag") == "https://Example.com/a/b.bin?x=1"
    assert R.origin("https://Example.COM:443/x") == "example.com" and R.origin("http://example.com:8080/x") == "example.com:8080"
    assert R.check_host("CDN.Example.com") == "cdn.example.com"
    for bad in ("127.0.0.1", "localhost", "a/b", "exa mple.com", ""):
        with pytest.raises(DownloadError):
            R.check_host(bad)


@pytest.mark.parametrize("dest", ["../out.bin", "a/../../out.bin", "C:\\Windows\\x.bin", "C:x.bin", "/etc/x", "\\\\server\\share\\x",
                                  "~/x.bin", "a\\..\\..\\b", "x.bin:stream", "CON", "a/NUL.txt", "com1.bin", "x.part", "a//b",
                                  "name.", "a/ lead.bin", "a/" + "x" * 200, "q?.bin", "/".join("abcdefghi")])
def test_destinations_outside_the_download_dir_are_refused(tmp_path, dest):
    with pytest.raises(DownloadError) as e:
        R.check_dest(tmp_path / "files", dest, "https://example.com/f.bin")
    assert e.value.code == "dest-invalid"


def test_destination_defaults_and_links_pointing_outside(tmp_path):
    root, outside = tmp_path / "files", tmp_path / "outside"
    assert R.check_dest(root, "", "https://example.com/dir/My%20Model.bin?download=1") == root / "My Model.bin"
    assert R.check_dest(root, None, "https://example.com/") == root / "download.bin"
    assert R.check_dest(root, "pack\\v1/a.onnx", "https://example.com/x") == root / "pack" / "v1" / "a.onnx"
    root.mkdir()
    outside.mkdir()
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)], capture_output=True) if os.name == "nt" else None
    if made is None or made.returncode != 0:
        pytest.skip("这台机器建不了目录链接")
    with pytest.raises(DownloadError) as e:  # 目录里有指到外面的链接: 不往外写
        R.check_dest(root, "link/x.bin", "https://example.com/x")
    assert e.value.code == "dest-invalid" and "外面" in str(e.value)
    with pytest.raises(DownloadError):
        R.recheck_dest(root, root / "link" / "x.bin")


# ---------------------------------------------------------------- 大小、磁盘的上限
def test_actual_size_larger_than_the_card_asks_again(tmp_path):
    data, fat = blob(400_000), blob(1_600_000, 8)

    async def main():
        async with Bench(tmp_path, unknown_ok_bytes=100_000) as b:
            url = b.site.add("fat.bin", fat)
            b.ok(b.put(url, size=1000))  # 她说 1000 字节, 实际 1.6 MB (多出 5% + 1 MB 以上才算"更大")
            job = await b.until("D1", "awaiting")
            assert job["ask"]["why"] == "size" and job["ask"]["total"] == len(fat) and job["done"] == 0
            part = b.root / "fat.bin.part"
            assert not part.exists() or part.stat().st_size == 0  # 没有收下内容
            assert "实际大小" in TX.card(job, b.limits, b.root)
            b.q.answer(job["ask"]["card"], True)
            await b.until("D1", "done")
            b.ok(b.put(b.site.add("unknown.bin", data)))  # 没写大小: 卡片写未知, 实际超过 unknown_ok 也再问
            job = await b.until("D2", "awaiting")
            assert job["ask"]["why"] == "size"
            b.q.answer("D2", False)
            assert b.q.jobs["D2"]["state"] == "denied" and not (b.root / "unknown.bin.part").exists()
            b.ok(b.put(b.site.add("stream.bin", data, chunked=True), size=len(data)))  # 服务器不报大小: 照卡片上批的收
            await b.until("D3", "done")
            b.ok(b.put(b.site.add("stream2.bin", data, chunked=True)))  # 不报大小、卡片上也未知: 收到 unknown_ok 就停下问
            job = await b.until("D4", "awaiting")
            assert job["ask"]["why"] == "size" and job["ask"]["open"] is True
    run(main)


def test_single_file_cap_and_disk_reserve(tmp_path):
    async def main():
        async with Bench(tmp_path, max_file_bytes=200_000, reserve_bytes=1_000_000, unknown_ok_bytes=150_000) as b:
            with pytest.raises(DownloadError) as e:
                b.put(b.site.add("a.bin", blob(10)), size=300_000)
            assert e.value.code == "too-large" and "上限" in str(e.value)
            b.ok(b.put(b.site.add("big.bin", blob(300_000)), size=150_000))  # 她说 15 万, 实际 30 万 > 单个上限
            job = await b.until("D1", "failed")
            assert job["error_kind"] == "too-large" and b.q.jobs["D1"]["done"] == 0
            # 余量按"队列里还要的加起来"算: 一个个单看都够, 加起来不够的不收
            b.q.free = lambda p: 1_000_000 + 250_000
            first = b.put(b.site.add("one.bin", blob(100_000)), size=150_000)
            with pytest.raises(DownloadError) as e:
                b.put(b.site.add("two.bin", blob(100_000)), size=150_000)
            assert e.value.code == "disk-full" and "队列里还要" in str(e.value)
            b.q.free = lambda p: 1_000_000 + 50_000  # 点头以后真要下时又查一次
            b.ok(first)
            assert (await b.until("D2", "failed"))["error_kind"] == "disk-full"
            assert b.q.jobs["D2"]["done"] == 0 and not (b.root / "one.bin").exists()  # 一个字节都没收下
    run(main)


# ---------------------------------------------------------------- 备用来源、调度、清理、回报
def test_fallback_source_comes_only_from_the_card(tmp_path):
    data = blob(60_000)

    async def main():
        async with Bench(tmp_path) as b:
            gone = b.site.add("gone.bin", b"", status=404)
            good = b.site.add("mirror.bin", data)
            b.ok(b.put([gone, good], dest="f.bin", sha256=sha256(data)))
            job = await b.until("D1", "done")
            assert job["url_index"] == 1 and (b.root / "f.bin").read_bytes() == data
            assert {r["path"] for r in b.site.requests} == {"gone.bin", "mirror.bin"}  # 只连了卡片上的两个
            b.ok(b.put(gone, dest="g.bin"))
            assert "404" in (await b.until("D2", "failed"))["error"]
            assert b.q.resume("D2", A)["state"] in ("queued", "running")  # 没成的可以接着试 (还是那几个来源)
            await b.until("D2", "failed")
    run(main)


def test_two_sessions_take_turns_and_slots_are_limited(tmp_path):
    async def main():
        async with Bench(tmp_path, rate=600_000, max_active=1) as b:
            jobs = [b.put(b.site.add(f"{s}{i}.bin", blob(60_000)), session=s) for s, i in
                    (("sess-a", 1), ("sess-a", 2), ("sess-a", 3), ("sess-b", 1))]
            for j in jobs:
                b.ok(j)
            await asyncio.sleep(0.05)
            assert sum(j["state"] == "running" for j in b.q.jobs.values()) == 1  # 一次只下一个
            for j in jobs:
                await b.until(j["id"], "done")
            order = sorted(b.q.jobs.values(), key=lambda j: j["started"])
            assert [j["id"] for j in order][:2] == ["D1", "D4"]  # 第二个轮到另一个会话, 不是先把 A 的三个下完
    run(main)


def test_cleanup_keeps_files_unless_asked_and_only_touches_own(tmp_path):
    data = blob(30_000)

    async def main():
        async with Bench(tmp_path) as b:
            b.ok(b.put(b.site.add("keep.bin", data)))
            b.ok(b.put(b.site.add("half.bin", data, status=500), session="sess-b"))
            await b.until("D1", "done")
            b.q.jobs["D2"]["error_kind"] = ""
            await b.until("D2", "failed")
            (b.root / "half.bin.part").write_bytes(b"x" * 10)
            for j in ("D1", "D2"):
                b.q.ack(j)
            got = b.q.cleanup("", A)  # 默认: 清记录和半截文件, 下好的留着; 只清自己的
            assert got == {"scope": "allWithoutFiles", "removed_records": 1, "removed_cache_files": 0, "removed_files": 0, "freed_bytes": 0}
            assert (b.root / "keep.bin").exists() and "D1" not in b.q.jobs and "D2" in b.q.jobs
            assert not (b.home / "jobs" / "D1.json").exists() and (b.root / "half.bin.part").exists()
            got = b.q.cleanup("cache", B)
            assert got["removed_cache_files"] == 1 and got["freed_bytes"] == 10 and "D2" in b.q.jobs
            b.ok(b.put(b.site.add("del.bin", data)))
            await b.until("D3", "done")
            assert b.q.cleanup("all", A)["removed_files"] == 0  # 结果还没交回去的先留着
            b.q.ack("D3")
            got = b.q.cleanup("all", USER)  # 你清的是全部; all 才删下好的文件
            assert got["removed_files"] == 1 and got["freed_bytes"] == len(data) and not (b.root / "del.bin").exists()
            assert not b.q.jobs and (b.root / "keep.bin").exists()
            with pytest.raises(DownloadError):
                b.q.cleanup("everything", A)
    run(main)


def test_results_are_reported_once_and_wait_returns(tmp_path):
    data = blob(120_000)

    async def main():
        async with Bench(tmp_path, rate=300_000) as b:
            job = b.put(b.site.add("r.bin", data))
            waiting = asyncio.ensure_future(b.q.wait("D1", 5, viewer="sess-b"))
            await asyncio.sleep(0.05)
            assert not waiting.done()  # 还在等点头: 接着等
            b.ok(job)
            seen = await waiting
            assert seen["state"] == "done" and seen["final"]["bytes"] == len(data)
            first = await b.q.changes()
            assert first["jobs"][0]["report"] == "pending" and first["jobs"][0]["report_info"]["hint"] == "详情用 download_status(id='D1')"
            assert "下完了" in first["jobs"][0]["report_info"]["body"] and "没有运行" in first["jobs"][0]["report_info"]["body"]
            assert summary(b.home)["unreported"] == 1 and not wanted(b.home)  # 没回报的不拦着服务退 (记录在文件里, 宿主回来再取)
            assert not b.q.busy()
            assert b.q.ack("D1") is True and b.q.ack("D1") is False  # 只有第一次算数
            assert summary(b.home) == {"active": 0, "awaiting": 0, "unreported": 0}
            later = await b.q.changes(first["rev"], first["boot"], timeout=0)
            assert [j["report"] for j in later["jobs"]] == ["acked"]
            assert (await b.q.changes(later["rev"], later["boot"], timeout=0.05))["jobs"] == []  # 没变化: 长等到点空手回
            # 发起的会话自己看到结果了: 不用再回报
            b.ok(b.put(b.site.add("s.bin", data)))
            assert (await b.q.wait("D2", 5, viewer="sess-a"))["state"] == "done" and b.q.jobs["D2"]["report"] == "acked"
            assert (await b.q.wait("D2", 0))["state"] == "done"
    run(main)


def test_a_broken_record_does_not_take_the_queue_down(tmp_path):
    async def main():
        async with Bench(tmp_path) as b:
            b.put(b.site.add("a.bin", blob(10)))
            b.put(b.site.add("b.bin", blob(10)))
            await b.q.close()
            (b.home / "jobs" / "D1.json").write_text("{坏的", encoding="utf-8")
            (b.home / "seq.json").unlink()
            q = await b.open()
            assert list(q.jobs) == ["D2"] and list((b.home / "jobs").glob("D1.json.bad-*"))
            assert b.put(b.site.add("c.bin", blob(10)))["id"] == "D3"  # 编号不倒退
            with pytest.raises(DownloadError) as e:  # 一个会话等点头的卡片有上限, 免得刷屏
                for i in range(20):
                    b.put(b.site.add(f"m{i}.bin", blob(10)))
            assert e.value.code == "too-many"
    run(main)


def test_card_says_name_source_size_license_or_unknown(tmp_path):
    q = DownloadQueue(tmp_path / "home", tmp_path / "files")
    job = q.jobs[q.enqueue({"urls": ["https://huggingface.co/a/b/resolve/main/model.onnx", "https://hf-mirror.com/a/b/resolve/main/model.onnx"],
                            "name": "某模型 v1", "size": 342_884_352, "license": "Apache-2.0", "sha256": "a" * 64,
                            "redirect_hosts": ["cas-bridge.xethub.hf.co"], "note": "给语音合成用"}, session="s")["job"]["id"]]
    card = TX.card(job, q.limits, q.root, "http://127.0.0.1:1082")
    for piece in ("名称：某模型 v1", "来源：huggingface.co、hf-mirror.com", "https://hf-mirror.com/a/b/resolve/main/model.onnx",
                  "会转到：cas-bridge.xethub.hf.co", "大小：327 MB", "许可：Apache-2.0（她查到的，没有核实）", "有 SHA-256",
                  f"存到：{q.root / 'model.onnx'}", "走法：经本机代理 127.0.0.1:1082", "用途：给语音合成用"):
        assert piece in card
    short = TX.card_short(job, q.limits)
    assert len(short) <= 280 and all(x in short for x in ("某模型 v1", "huggingface.co", "327 MB", "Apache-2.0"))
    bare = q.jobs[q.enqueue({"urls": ["https://example.com/tool/setup.exe"], "name": "", "direct": True}, session="s")["job"]["id"]]
    card = TX.card(bare, q.limits, q.root, "http://127.0.0.1:1082")
    assert "名称：setup.exe" in card and "大小：未知" in card and "许可：未知" in card and "没有（下完没法核对内容）" in card
    assert "走法：直连" in card and "能直接运行的文件" in card
    assert "大小 未知" in TX.card_short(bare, q.limits) and "许可 未知" in TX.card_short(bare, q.limits)


@pytest.mark.skipif(os.name != "nt", reason="只有 Windows 有这个标记")
def test_finished_file_carries_the_mark_of_the_web(tmp_path):
    async def main():
        async with Bench(tmp_path) as b:
            url = b.site.add("tool.exe", blob(2000))
            b.ok(b.put(url))
            await b.until("D1", "done")
            try:
                mark = open(f"{b.root / 'tool.exe'}:Zone.Identifier", encoding="ascii").read()
            except OSError:
                pytest.skip("这个盘不是 NTFS")
            assert "ZoneId=3" in mark and f"HostUrl={url}" in mark
    run(main)

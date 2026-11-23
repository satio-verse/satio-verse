"""下载队列下到哪 (2026-11-16 用户定的: 桌宠文件夹下面的 download-queue 子文件夹), 写不进去怎么办, 旧位置的东西怎么办.
位置的部分从真入口走 (AgentHub 构造 → hub.downloads.client.root、命令行 python -m wm.downloads、真起的 serve 进程), 不往函数里塞数值.
验收: 1 三种情况 (开发树、装好的默认/指定位置、整个文件夹拷走) 的位置, 路径里有空格、中文、盘符不同; 2 不可写时明确拒绝、不换地方、
不要管理员权限; 3 旧位置 downloads/queue 里的东西不删、不搬、不改; 4 卡片/状态/工具说明里的位置是对的."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import types
import urllib.request
from pathlib import Path

import pytest

from dl_fakes import blob, sha256
from test_agent_downloads import Stage
from test_downloads_queue import Bench, run
from wm import downloads as DQ
from wm.agent import downloads as DL
from wm.agent.core import AgentHub
from wm.downloads import DownloadError, read_json
from wm.downloads import daemon as D
from wm.downloads import text as TX
from wm.downloads.queue import USER

PROJECT = Path(DQ.__file__).resolve().parent.parent.parent       # 跑的这棵树的根 (真项目目录, 或者 run_overlay 的拷贝)


def new_hub(tmp: Path, conf: dict | None = None) -> AgentHub:
    ws = tmp / "workspace"
    ws.mkdir(exist_ok=True)
    cfg = {"agent": {"workspace": str(ws)}}
    if conf is not None:
        cfg["downloads"] = conf
    return AgentHub(cfg, engines=types.SimpleNamespace(channels=None, memory=None), home=tmp / "agent")


def tree_of(path: Path) -> set:
    return {p.relative_to(path).as_posix() for p in path.rglob("*")}


def fingerprint(folder: Path) -> dict:
    """文件夹里每个文件: 内容哈希、大小、修改时间 —— 前后一样才算没动."""
    out = {}
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[p.relative_to(folder).as_posix()] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns)
    return out


def blocked_root(tmp: Path) -> Path:
    """一个怎么也建不出来的下载文件夹: 它的上一级是个普通文件 (真的操作系统错误, 不用改权限、不用管理员)."""
    (tmp / "blocker.txt").write_text("我是个文件", encoding="utf-8")
    return tmp / "blocker.txt" / "download-queue"


# ================================================================ 验收 1: 位置
def test_dev_tree_default_is_download_queue_next_to_wm(tmp_path):
    assert DQ.FOLDER == "download-queue"
    assert DQ.dir_for({}) == PROJECT / "download-queue" and DQ.dir_for(None) == DQ.default_dir()
    hub = new_hub(tmp_path)                                       # 真入口: 工作台启动时建下载队列
    assert hub.downloads.client.root == PROJECT / "download-queue"
    assert (PROJECT / "wm" / "downloads" / "__init__.py").is_file()   # 桌宠文件夹 = 放 wm/ 的那个文件夹
    assert "downloads" not in hub.downloads.client.root.parts[-2:]   # 不在 downloads\ 里面 (卸载会删那个)


@pytest.mark.parametrize("parts", [("WorldModel",), ("我的 桌宠 (安装)", "WorldModel 桌宠"), ("a b", "装在 这里 啊", "x")])
def test_installed_default_and_custom_install_folders(tmp_path, monkeypatch, parts):
    install = tmp_path.joinpath(*parts)
    install.mkdir(parents=True)
    monkeypatch.setattr(DQ, "ROOT", install)
    for var in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME"):   # 位置只看代码文件在哪, 不看用户目录 (会话沙箱会重定向它们)
        monkeypatch.setenv(var, str(tmp_path / "垃圾 位置" / var))
    hub = new_hub(tmp_path)
    assert hub.downloads.client.root == install / "download-queue"
    assert not (tmp_path / "垃圾 位置").exists()


def test_a_copied_folder_runs_from_where_it_is(tmp_path):
    """整个桌宠文件夹拷到别处 (便携方式): 真起一个 Python 去算, 工作目录在第三个地方."""
    copy = tmp_path / "便携 版 (拷贝)" / "桌宠"
    (copy / "wm" / "downloads").mkdir(parents=True)
    for rel in ("wm/__init__.py", "wm/config.py", "wm/downloads/__init__.py"):
        (copy / rel).write_bytes((PROJECT / rel).read_bytes())
    elsewhere = tmp_path / "别处"
    elsewhere.mkdir()
    code = "import sys; sys.path.insert(0, sys.argv[1]); from wm.downloads import dir_for; print(dir_for({}), end='')"
    out = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code, str(copy)], cwd=elsewhere, capture_output=True, check=True)
    got = Path(out.stdout.decode("utf-8"))
    assert got == copy / "download-queue"
    assert got != PROJECT / "download-queue" and got.drive == copy.drive   # 跟着拷到的地方走 (和项目不在同一个盘时就是跨盘)


@pytest.mark.parametrize("value, expect", [
    (r"D:\下载 队列\x", Path(r"D:\下载 队列\x")),
    (r"E:/a b/c", Path(r"E:/a b/c")),
    (r"\\server\share\dl", Path(r"\\server\share\dl")),
    ("mine/q", "REL:mine/q"), ("  mine\\q  ", "REL:mine/q"), ("下载 文件夹", "REL:下载 文件夹"),
    ("", "DEFAULT"), ("   ", "DEFAULT"), (None, "DEFAULT"), (5, "DEFAULT"), (["x"], "DEFAULT")])
def test_configured_dir_overrides(tmp_path, value, expect):
    root = tmp_path / "根 目录"
    got = DQ.dir_for({"dir": value}, root)
    want = root / "download-queue" if expect == "DEFAULT" else root / expect[4:] if isinstance(expect, str) else expect
    assert got == want
    assert DQ.dir_for({"dir": value}, root) == DQ.dir_for({"dir": value}, str(root))


def test_the_folder_is_not_under_anything_the_uninstaller_deletes():
    """安装脚本卸载时删的东西 (UninstallDelete) 里, 没有能匹配到 {app}\\download-queue 的; 而 {app}\\downloads 确实在里面 (说明读对了)."""
    text = (PROJECT / "installer" / "worldmodel.iss").read_text(encoding="utf-8")
    block = text.split("[UninstallDelete]", 1)[1].split("\n[", 1)[0]
    names = []
    for line in block.splitlines():
        if line.strip().startswith("Type:") and 'Name: "' in line:
            names.append(line.split('Name: "', 1)[1].split('"', 1)[0].replace("/", "\\").rstrip("\\").lower())
    assert "{app}\\downloads" in names and "{app}\\wm" in names
    mine = "{app}\\" + DQ.FOLDER
    for name in names:
        assert "*" not in name and "?" not in name
        assert mine != name and not mine.startswith(name + "\\"), f"卸载会删到 {mine}: {name}"


# ================================================================ 验收 2: 写不进去
def test_check_writable_tells_the_truth(tmp_path, monkeypatch):
    ok = tmp_path / "好 的" / "download-queue"
    assert DQ.check_writable(ok) == "" and ok.is_dir()
    assert list(ok.iterdir()) == []                                   # 试写的小文件删掉了
    reason = DQ.check_writable(blocked_root(tmp_path))
    assert reason and not (tmp_path / "blocker.txt").is_dir()
    assert DQ.check_writable(Path("D:x")) .startswith("「D:x」")      # 带盘符却不是完整路径
    real = os.open

    def deny(path, *a, **k):
        if DQ.PROBE in os.fspath(path):
            raise PermissionError(13, "Access is denied")
        return real(path, *a, **k)
    monkeypatch.setattr(DQ.os, "open", deny)
    assert "PermissionError" in DQ.check_writable(ok) and "Access is denied" in DQ.check_writable(ok)


def test_unwritable_folder_refuses_to_queue_and_changes_nothing_else(tmp_path):
    bad = blocked_root(tmp_path)

    async def main():
        b = Bench(tmp_path)
        b.root = bad
        async with b:                                              # 起来不抛 (以前这里 mkdir 失败, 服务起不来)
            assert b.q.dir_error
            before = tree_of(tmp_path)
            with pytest.raises(DownloadError) as e:
                b.put(b.site.add("m.bin", blob(1000, 1)), size=1000)
            msg = str(e.value)
            assert e.value.code == "dir-unwritable"
            assert str(bad) in msg and b.q.dir_error.split(":")[0] in msg
            assert "没有换到别的地方" in msg and "管理员" in msg and "别改用命令行" in msg
            assert b.q.jobs == {} and list((b.home / "jobs").glob("D*.json")) == []
            assert tree_of(tmp_path) == before                   # 哪儿也没多出东西 (没偷偷换个文件夹)
            assert not DQ.default_dir(tmp_path).exists() and not (Path.home() / "Downloads" / "download-queue").exists()
            st = b.q.status()
            assert st["dir"] == str(bad) and st["dir_error"] and st["jobs"] == []
            (tmp_path / "blocker.txt").unlink()                       # 修好了: 不用重启
            job = b.put(b.site.add("m.bin", blob(1000, 1)), size=1000)
            assert job["state"] == "awaiting" and bad.is_dir()
            assert "dir_error" not in b.q.status()
    run(main)


def test_a_leftover_probe_file_is_never_shown(tmp_path):
    async def main():
        b = Bench(tmp_path)
        async with b:
            (b.root / f"{DQ.PROBE}123-abc").write_bytes(b"0")
            job = b.put(b.site.add("m.bin", blob(1000, 1)), size=1000)
            shown = json.dumps(b.q.status()) + TX.card(b.q.jobs[job["id"]], b.q.limits, b.q.root)
            assert DQ.PROBE not in shown
            assert DQ.check_writable(b.root) == ""
    run(main)


def test_the_real_service_starts_with_an_unwritable_folder_and_says_so(tmp_path):
    home, bad = tmp_path / "serve" / "downloads", blocked_root(tmp_path)
    cmd = [sys.executable, "-X", "utf8", "-m", "wm.downloads", "serve", "--home", str(home), "--dir", str(bad), "--port", "0",
           "--idle-exit", "120"]
    p = subprocess.Popen(cmd, cwd=PROJECT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def rpc(info, op, **body):
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/rpc", data=json.dumps({"op": op, **body}).encode(),
                                     headers={"X-WM-Token": info["token"], "Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read())
    try:
        end, info = time.monotonic() + 40, {}
        while time.monotonic() < end and not info.get("port"):
            info = read_json(home / "daemon.json")
            time.sleep(0.2)
        assert info.get("port") and p.poll() is None, "下载文件夹写不进去时服务也要起来"
        assert info["dir"] == str(bad) and info["dir_error"]
        got = rpc(info, "enqueue", args={"urls": ["http://example.com/a.bin"], "name": "x"}, session="s", owner="s")
        assert got["ok"] is False and got["code"] == "dir-unwritable" and "没有换到别的地方" in got["error"]
        assert rpc(info, "info")["dir_error"]
        out = subprocess.run([sys.executable, "-X", "utf8", "-m", "wm.downloads", "status", "--home", str(home)], cwd=PROJECT,
                             capture_output=True, check=True).stdout.decode("utf-8")
        assert f"下载文件夹: {bad}" in out and "写不进去" in out
        assert rpc(info, "shutdown")["ok"]
        assert p.wait(timeout=20) == 0
    finally:
        if p.poll() is None:
            p.kill()


def test_status_command_prints_where_it_downloads(tmp_path):
    out = subprocess.run([sys.executable, "-X", "utf8", "-m", "wm.downloads", "status", "--home", str(tmp_path / "h")], cwd=tmp_path,
                         env={**os.environ, "PYTHONPATH": str(PROJECT)}, capture_output=True, check=True).stdout.decode("utf-8")
    assert f"下载文件夹: {PROJECT / 'download-queue'}" in out


def test_her_tool_gets_the_plain_reason_and_no_card_pops_up(tmp_path):
    bad = blocked_root(tmp_path)

    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "好"}) as st:
            st.hub.downloads.client = st.client = type(st.client)(DQ.home_for(st.hub.home), bad, {f"127.0.0.1:{st.site.port}"})
            with pytest.raises(ValueError) as e:
                await DL.call_tool(st.hub, st.s, "download_enqueue", {"urls": [st.site.add("m.bin", blob(100, 1))], "name": "测试"})
            assert "写不进去" in str(e.value) and "没有换到别的地方" in str(e.value) and "管理员" in str(e.value)
            got = await DL.call_tool(st.hub, st.s, "download_status", {})
            assert got["dir"] == str(bad) and got["dir_error"] and got["jobs"] == []
            assert st.of("ask") == [] and st.channels.asks == [] and st.jobs() == {}
    run(main)


# ================================================================ 验收 3: 旧位置的东西
def legacy_tree(base: Path) -> Path:
    old = base / "downloads" / "queue"
    (old / "kokoro").mkdir(parents=True)
    (old / "kokoro" / "model.onnx").write_bytes(blob(70_000, 7))
    (old / "half.bin.part").write_bytes(blob(5_000, 8))
    return old


def test_legacy_folder_is_reported_and_left_exactly_as_it_was(tmp_path, monkeypatch):
    base = tmp_path / "桌宠 文件夹"
    old = legacy_tree(base)
    monkeypatch.setattr(DQ, "ROOT", base)
    before, listing = fingerprint(old), tree_of(old)
    data = blob(40_000, 3)

    async def main():
        b = Bench(tmp_path)
        b.root = DQ.dir_for({})
        async with b:
            assert b.q.legacy == {"dir": str(old), "files": 2, "bytes": 75_000, "more": False}
            assert b.q.status()["legacy"]["files"] == 2
            assert "没有动也没有搬" in DQ.legacy_text(b.q.legacy) and str(old) in DQ.legacy_text(b.q.legacy)
            svc = D.Service(b.home, b.root, b.limits, b.allow, 9999)        # 服务的 info 也带着
            svc.queue = b.q
            assert (await svc.call("info", {}))["legacy"]["files"] == 2
            assert list(b.root.iterdir()) == []                              # 没搬: 新文件夹是空的
            job = b.put(b.site.add("new.bin", data), sha256=sha256(data), size=len(data))
            b.ok(job)
            done = await b.until(job["id"], "done")
            assert Path(done["dest"]) == b.root / "new.bin" and (b.root / "new.bin").read_bytes() == data
    run(main)
    assert fingerprint(old) == before and tree_of(old) == listing           # 一个字节、一个修改时间都没变


@pytest.mark.parametrize("case", ["missing", "empty", "same_as_root", "root_contains_it"])
def test_legacy_not_reported_when_there_is_nothing_to_tell(tmp_path, monkeypatch, case):
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setattr(DQ, "ROOT", base)
    old = base / "downloads" / "queue"
    if case == "empty":
        old.mkdir(parents=True)
    if case in ("same_as_root", "root_contains_it"):
        legacy_tree(base)
    root = {"missing": DQ.default_dir(), "empty": DQ.default_dir(), "same_as_root": old, "root_contains_it": base / "downloads"}[case]
    assert DQ.legacy_report(root) == {}

    async def main():
        b = Bench(tmp_path)
        b.root = root
        async with b:
            assert b.q.legacy == {} and "legacy" not in b.q.status()
    run(main)


def job_record(jid, state, dest, **more):
    return {"v": 1, "id": jid, "seq": int(jid[1:]), "name": "旧的", "urls": ["http://127.0.0.1:9/a.bin"], "hosts": [], "dest": str(dest),
            "sha256": None, "size_declared": None, "license": "", "note": "", "route": "auto", "owner": "sess-a", "origin": "sess-a",
            "owner_title": "测试会话", "run": "r", "state": state, "ask": {"seq": 1, "why": "new", "card": f"dl-{jid}-1", "shown": True, "asked": 1.0},
            "approvals": [], "approved_bytes": 0, "pause_by": None, "created": 1.0, "updated": 1.0, "ended": None, "total": -1, "done": 0,
            "checkpoint": 0, "url_index": 0, "part": {}, "error": "", "error_kind": "", "final": None, "report": "none", "key": [str(dest), "x"],
            **more}


def test_finished_downloads_in_the_old_place_survive_even_cleanup_all(tmp_path, monkeypatch):
    base = tmp_path / "base"
    old = legacy_tree(base)
    monkeypatch.setattr(DQ, "ROOT", base)
    kept = old / "kokoro" / "model.onnx"
    size = kept.stat().st_size

    async def main():
        b = Bench(tmp_path)
        b.root = DQ.default_dir()
        (b.home / "jobs").mkdir(parents=True)
        rec = job_record("D1", "done", kept, ended=2.0, report="acked", total=size, done=size,
                         final={"bytes": size, "sha256": "0" * 64, "verified": False})
        (b.home / "jobs" / "D1.json").write_text(json.dumps(rec), encoding="utf-8")
        async with b:
            assert b.q.jobs["D1"]["state"] == "done"                   # 记录和路径照旧
            assert b.q.status("D1")["jobs"][0]["state"] == "done"
            out = b.q.cleanup("all", USER)
            assert out["removed_files"] == 0 and out["removed_records"] == 1   # 记录清了, 旧位置的文件它删不了
    run(main)
    assert kept.is_file() and kept.stat().st_size == size


def test_unfinished_downloads_in_the_old_place_fail_with_a_clear_reason_and_no_file_is_touched(tmp_path, monkeypatch):
    base = tmp_path / "base"
    old = legacy_tree(base)
    monkeypatch.setattr(DQ, "ROOT", base)
    root = DQ.default_dir()
    before = fingerprint(old)

    async def main():
        b = Bench(tmp_path)
        b.root = root
        (b.home / "jobs").mkdir(parents=True)
        recs = [job_record("D1", "queued", old / "half.bin"), job_record("D2", "awaiting", old / "other.bin"),
                job_record("D3", "paused", root / "inside.bin"), job_record("D4", "running", old / "half.bin")]
        for rec in recs:
            (b.home / "jobs" / f"{rec['id']}.json").write_text(json.dumps(rec), encoding="utf-8")
        async with b:
            for jid in ("D1", "D2", "D4"):
                job = b.q.jobs[jid]
                assert job["state"] == "failed" and job["error_kind"] == "dest" and job["report"] == "pending"
                assert str(root) in job["error"] and "下载位置改过" in job["error"] and "重新排" in job["error"]
            assert b.q.jobs["D3"]["state"] == "queued" or b.q.jobs["D3"]["state"] == "paused"   # 在新文件夹里的不动
            assert json.loads((b.home / "jobs" / "D1.json").read_text(encoding="utf-8"))["state"] == "failed"   # 存盘了
    run(main)
    assert fingerprint(old) == before


# ================================================================ 验收 4: 她和卡片上显示的位置
def test_card_status_report_and_tool_texts_show_the_new_place(tmp_path, monkeypatch):
    base = tmp_path / "我的 桌宠"
    monkeypatch.setattr(DQ, "ROOT", base)
    data = blob(30_000, 5)

    async def main():
        b = Bench(tmp_path)
        b.root = DQ.dir_for({})
        async with b:
            job = b.put(b.site.add("a.bin", data), size=len(data))
            full = b.q.jobs[job["id"]]
            card = TX.card(full, b.limits, b.root)
            assert f"存到：{b.root / 'a.bin'}" in card and "downloads\\queue" not in card and "downloads/queue" not in card
            b.ok(job)
            done = await b.until(job["id"], "done")
            assert done["dest"].startswith(str(base / "download-queue"))
            assert str(base / "download-queue" / "a.bin") in TX.report(done)["body"]
            st = b.q.status()
            assert st["dir"] == str(base / "download-queue")
    run(main)
    specs = json.dumps(DL.TOOL_SPECS, ensure_ascii=False)
    assert "downloads/queue" not in specs and "downloads\\\\queue" not in specs and "download_status 返回的 dir" in specs
    title = next(s["title"] for s in DL.MANIFEST["plugin"]["settings"] if s["key"] == "dir")
    assert "download-queue" in title


def test_her_status_mentions_the_old_folder_and_the_default_blacklist_allows_the_new_one(tmp_path, monkeypatch):
    base = tmp_path / "base"
    legacy_tree(base)
    monkeypatch.setattr(DQ, "ROOT", base)

    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "好"}) as st:
            st.hub.downloads.client = st.client = type(st.client)(DQ.home_for(st.hub.home), DQ.dir_for({}), {f"127.0.0.1:{st.site.port}"})
            got = await DL.call_tool(st.hub, st.s, "download_status", {})
            assert got["dir"] == str(base / "download-queue") and "没有动也没有搬" in got["old_dir_note"] and "dir_error" not in got
            assert DL.decision(st.hub, st.s, "download_enqueue", {"urls": ["https://example.com/a.bin"], "name": "x", "dest": "m/a.bin"}).how == "auto"
            assert (await DL.call_tool(st.hub, st.s, "download_status", {}))["old_dir_note"]
    run(main)

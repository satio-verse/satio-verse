"""可选组件 (wm/components.py + tools/components_lock.py): 从官方地址下 (第一个地址不行换下一个)、断了接着下、
校验不对就删、按显卡挑来源、解压 (去掉只有一层的目录)、和别的组件共用目录、磁盘不够不下、只删自己装的."""
import asyncio
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import components_lock as L  # noqa: E402

from wm import components as C  # noqa: E402


def entry(out: Path, name: str, git: bool = False) -> dict:
    data = (out / name).read_bytes()
    if git:
        return {"path": name, "size": len(data), "git": hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()}
    return {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def setup(tmp_path, comps):
    root = tmp_path / "root"
    manifest = root / "config" / "components.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"app_version": "9.9", "components": comps}, ensure_ascii=False), encoding="utf-8")
    return root, manifest


def files_dir(tmp_path) -> Path:
    out = tmp_path / "serve"
    (out / "sub").mkdir(parents=True)
    (out / "a.txt").write_text("你好" * 2000, encoding="utf-8")
    (out / "sub" / "w.gguf").write_bytes(bytes(range(256)) * 300)
    return out


async def serve(out: Path):
    app = web.Application()
    app.router.add_static("/files/", out)
    server = TestServer(app)
    await server.start_server()
    return server


async def run(comp: C.Components, cid="toy", timeout=20.0):
    assert comp.start(cid) is None
    await asyncio.wait_for(comp.task, timeout)
    return comp.job


def test_plain_files_fallback_resume_shared_dir_and_remove(tmp_path):
    out = files_dir(tmp_path)

    async def main():
        server = await serve(out)
        try:
            bases = [str(server.make_url("/nothing/")), str(server.make_url("/files/"))]  # 第一个 404, 换第二个
            src = {"base": bases, "files": [entry(out, "a.txt", git=True), entry(out, "sub/w.gguf")],
                   "rename": {"a.txt": "toy-README.txt"}}
            root, manifest = setup(tmp_path, [{"id": "toy", "name": "玩具", "target": "models/shared",
                                               "check": ["sub/w.gguf"], "sources": [src]}])
            shared = root / "models" / "shared"
            (shared / "sub").mkdir(parents=True)
            (shared / "mine.bin").write_bytes(b"x")  # 别的组件 / 你自己放的
            comp = C.Components(manifest, root, tmp_path / "dl", cap=0)
            st = comp.status()["components"][0]
            assert st["ready"] and not st["present"] and st["size"] == 12000 + 76800
            # 断过一次: w.gguf 已经下了一部分
            d = tmp_path / "dl" / "toy" / "sub"
            d.mkdir(parents=True)
            (d / "w.gguf.part").write_bytes((out / "sub" / "w.gguf").read_bytes()[:7000])
            job = await run(comp)
            assert job["state"] == "done", job
            assert job["done"] == job["total"]
            assert (shared / "toy-README.txt").read_text(encoding="utf-8") == "你好" * 2000
            assert (shared / "sub" / "w.gguf").read_bytes() == bytes(range(256)) * 300
            assert not (tmp_path / "dl" / "toy").exists()  # 装好以后下载的删掉
            st = comp.status()["components"][0]
            assert st["present"] and st["installed"]["id"] == "toy"
            assert "已经有了" in comp.start("toy")
            (shared / "sub" / "later.txt").write_text("后来放的", encoding="utf-8")
            assert comp.remove("toy") is None
            assert (shared / "mine.bin").exists() and (shared / "sub" / "later.txt").exists()  # 不是这里装的不碰
            assert not (shared / "toy-README.txt").exists() and not (shared / "sub" / "w.gguf").exists()
            assert "不是从这里装的" in comp.remove("toy")
        finally:
            await server.close()

    asyncio.run(main())


def test_zip_unpacks_into_new_dir_and_strips_top_folder(tmp_path):
    out = files_dir(tmp_path)
    with zipfile.ZipFile(out / "pkg.zip", "w") as zf:
        zf.writestr("pkg-1.0/api.py", "print(1)")
        zf.writestr("pkg-1.0/GPT_weights/.keep", "")

    async def main():
        server = await serve(out)
        try:
            src = {"base": [str(server.make_url("/files/"))], "unpack": "zip", "files": [entry(out, "pkg.zip")]}
            root, manifest = setup(tmp_path, [{"id": "toy", "name": "包", "target": "runtime/pkg", "check": ["api.py"],
                                               "unpacked": 100, "sources": [src]}])
            comp = C.Components(manifest, root, tmp_path / "dl", cap=0)
            job = await run(comp)
            assert job["state"] == "done", job
            target = root / "runtime" / "pkg"
            assert (target / "api.py").read_text() == "print(1)"
            assert not (root / "runtime" / "pkg.installing").exists()
            (target / "GPT_weights" / "mine.ckpt").write_bytes(b"trained")  # 后来自己训练的
            assert comp.remove("toy") is None
            assert (target / "GPT_weights" / "mine.ckpt").exists() and not (target / "api.py").exists()
        finally:
            await server.close()

    asyncio.run(main())


@pytest.mark.skipif(not C.SEVEN_ZIP.is_file(), reason="没有 runtime/7zip/7zr.exe")
def test_7z_unpacks_with_bundled_7zr(tmp_path):
    out = files_dir(tmp_path)
    (tmp_path / "src" / "gsv").mkdir(parents=True)
    (tmp_path / "src" / "gsv" / "api_v2.py").write_text("ok" * 5000, encoding="utf-8")
    subprocess.run([str(C.SEVEN_ZIP), "a", str(out / "gsv.7z"), "gsv"], cwd=tmp_path / "src", check=True, capture_output=True)

    async def main():
        server = await serve(out)
        try:
            src = {"base": [str(server.make_url("/files/"))], "unpack": "7z", "files": [entry(out, "gsv.7z")]}
            root, manifest = setup(tmp_path, [{"id": "toy", "name": "包", "target": "runtime/gsv", "check": ["api_v2.py"],
                                               "unpacked": 10000, "sources": [src]}])
            comp = C.Components(manifest, root, tmp_path / "dl", cap=0)
            job = await run(comp)
            assert job["state"] == "done" and job["unpack"] == 100, job
            assert (root / "runtime" / "gsv" / "api_v2.py").read_text(encoding="utf-8") == "ok" * 5000
        finally:
            await server.close()

    asyncio.run(main())


def test_bad_checksum_is_rejected(tmp_path):
    out = files_dir(tmp_path)

    async def main():
        server = await serve(out)
        try:
            f = entry(out, "sub/w.gguf") | {"sha256": "0" * 64}
            root, manifest = setup(tmp_path, [{"id": "toy", "name": "玩具", "target": "models/toy",
                                               "sources": [{"base": [str(server.make_url("/files/"))], "files": [f]}]}])
            comp = C.Components(manifest, root, tmp_path / "dl", cap=0)
            job = await run(comp)
            assert job["state"] == "error" and "校验不对" in job["error"]
            assert not (tmp_path / "dl" / "toy" / "sub" / "w.gguf.part").exists()
            assert not (root / "models" / "toy").exists()
        finally:
            await server.close()

    asyncio.run(main())


def test_source_by_gpu_and_disk_space(tmp_path, monkeypatch):
    big = {"path": "big.7z", "size": 10 << 30, "sha256": "0" * 64}
    comps = [{"id": "gsv", "name": "GPT-SoVITS", "target": "runtime/gsv", "unpacked": 18 << 30, "sources": [
        {"label": "50 系显卡版", "min_cap": 12.0, "hf": "lj1995/x", "rev": "abc", "unpack": "7z", "files": [big]},
        {"label": "通用版", "hf": "lj1995/x", "rev": "abc", "unpack": "7z", "files": [big]}]}]
    root, manifest = setup(tmp_path, comps)
    assert C.Components(manifest, root, cap=12.0).status()["components"][0]["variant"] == "50 系显卡版"
    st = C.Components(manifest, root, cap=8.6).status()["components"][0]
    assert st["variant"] == "通用版" and st["from"] == "Hugging Face · lj1995/x" and st["unpacked"] == 18 << 30
    assert C.urls(comps[0]["sources"][1], "big.7z") == ["https://hf-mirror.com/lj1995/x/resolve/abc/big.7z",
                                                         "https://huggingface.co/lj1995/x/resolve/abc/big.7z"]
    comp = C.Components(manifest, root, tmp_path / "dl", cap=0, seven_zip=tmp_path / "7zr.exe")
    assert "缺解压工具" in comp.start("gsv")
    (tmp_path / "7zr.exe").write_bytes(b"")
    monkeypatch.setattr(C.shutil, "disk_usage", lambda p: type("U", (), {"free": 20 << 30})())
    assert "磁盘空间不够: 要约 30.6 GB" in comp.start("gsv") and comp.job is None  # 下 10 + 解开 18 + 余量


def test_lock_picks_files_and_hashes():
    tree = [{"type": "file", "path": ".gitattributes", "size": 9, "oid": "g0"},
            {"type": "file", "path": "model.onnx", "size": 99, "oid": "g1", "lfs": {"oid": "s1"}},
            {"type": "file", "path": "dict/a.txt", "size": 5, "oid": "g2"}]
    assert L.pick(tree, ["*"], [".gitattributes"]) == [{"path": "dict/a.txt", "size": 5, "git": "g2"},
                                                         {"path": "model.onnx", "size": 99, "sha256": "s1"}]
    with pytest.raises(SystemExit):
        L.pick(tree, ["model.onnx", "voices.bin"], [])
    m = {"components": [{"id": "x", "sources": [{"files": [{"path": "a", "size": 1, "git": "g"}]}]}]}
    assert '"files": [\n            {"path": "a", "size": 1, "git": "g"}\n          ]' in L.dump(m)

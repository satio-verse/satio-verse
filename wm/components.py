"""可选组件 (docs/roadmap/own-harness.md 9.1; 2026-10-27 用户: "小组件都可以放到官方下, 保证我们的软件本体是很小的,
大的、额外的内容我们只需要提供一个插件等组件安装按钮, 他们需要自行点击安装即可").
清单 config/components.json: 每样放在哪 (target)、说明、许可, 从哪下 (sources: 官方的 Hugging Face 仓库 + 锁定的提交,
每个文件的大小和 SHA-256 / git 哈希; tools/components_lock.py 从 HF 的接口填进去). 我们自己不转手, 用户点了才从官方下.
面板"版本与组件"页点了才下: 先看磁盘够不够 (还要下的 + 解开的), 国内先连 hf-mirror.com 再连 huggingface.co,
断了接着下 (HTTP Range), 每个文件核对大小和哈希, 7z 包用自带的 runtime/7zip/7zr.exe 解开, 放进目标目录;
目标目录里留一个 .wm-component-<id>.json 记下装了哪些文件, "删掉"只删这些 (你后来自己放进去的不碰).
下载的东西先放在 downloads/components/<id>/, 装好以后删掉.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .config import ROOT

MANIFEST = ROOT / "config" / "components.json"
DL = ROOT / "downloads" / "components"
SEVEN_ZIP = ROOT / "runtime" / "7zip" / "7zr.exe"
CHUNK = 1 << 20
PARALLEL = 4  # 小文件多 (Kokoro 377 个), 同时下几个
HF_MIRRORS = ("https://hf-mirror.com", "https://huggingface.co")  # 国内先连镜像 (不走代理约 50 MB/s), 不行再连官方


def mark_of(cid: str) -> str:
    return f".wm-component-{cid}.json"


def urls(src: dict, path: str) -> list[str]:
    """一个文件的下载地址, 按先后试. hf: 官方仓库 + 锁定的提交; base: 直接给的地址 (测试用)."""
    if src.get("hf"):
        return [f"{host}/{src['hf']}/resolve/{src['rev']}/{quote(path)}" for host in HF_MIRRORS]
    return [b.rstrip("/") + "/" + quote(path) for b in src.get("base", [])]


def digest(path: Path, f: dict) -> bool:
    """核对: 大文件用 LFS 的 sha256, 小文件 HF 只给 git 的 blob sha1."""
    if f.get("sha256"):
        h = hashlib.sha256()
    elif f.get("git"):
        h = hashlib.sha1(b"blob %d\0" % path.stat().st_size)
    else:
        return False
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest() == (f.get("sha256") or f.get("git"))


def compute_cap() -> float:
    """N 卡的计算能力 (8.6 = 30 系, 12.0 = 50 系); 没有 N 卡或查不到是 0."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"], capture_output=True,
                             text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return max((float(x) for x in out.split()), default=0.0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


class Components:
    """装组件的活 (一次只装一样). cap: 测试时直接给显卡的计算能力; 正常为空, 第一次用时问 nvidia-smi."""

    def __init__(self, manifest: Path = MANIFEST, root: Path = ROOT, dl: Path = DL, cap: float | None = None,
                 seven_zip: Path = SEVEN_ZIP):
        self.manifest, self.root, self.dl, self.cap, self.seven_zip = manifest, root, dl, cap, seven_zip
        self.job: dict | None = None  # {id, state, done, total, unpack, error, started}
        self.task: asyncio.Task | None = None
        self.proc: subprocess.Popen | None = None

    def load(self) -> dict:
        try:
            return json.loads(self.manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"components": []}

    def source(self, c: dict) -> dict:
        """按显卡挑一个来源 (GPT-SoVITS 的 50 系显卡要单独的包): 第一个够条件的."""
        if self.cap is None:
            self.cap = compute_cap()
        return next((s for s in c.get("sources", []) if self.cap >= s.get("min_cap", 0)), {})

    def present(self, c: dict) -> bool:
        """已经有了: check 里列的都在; 没列就看目标目录在不在、有没有东西 (可能是你自己放的, 不是从这里装的)."""
        target = self.root / c["target"]
        if c.get("check"):
            return all((target / p).exists() for p in c["check"])
        return target.is_dir() and any(target.iterdir())

    def status(self) -> dict:
        m = self.load()
        out = []
        for c in m.get("components", []):
            s, target = self.source(c), self.root / c["target"]
            try:
                installed = json.loads((target / mark_of(c["id"])).read_text(encoding="utf-8"))
                installed.pop("files", None)
            except (OSError, ValueError):
                installed = None
            size = sum(f["size"] for f in s.get("files", []))
            out.append({k: c.get(k) for k in ("id", "name", "desc", "license", "use", "target")} | {
                "size": size,                                      # 要下多少
                "unpacked": c.get("unpacked", size),               # 装好占多少
                "from": f"Hugging Face · {s['hf']}" if s.get("hf") else "",
                "page": f"https://huggingface.co/{s['hf']}" if s.get("hf") else "",
                "variant": s.get("label", ""),
                "ready": bool(s.get("files")),
                "present": self.present(c),
                "installed": installed,
            })
        return {"version": m.get("app_version", ""), "components": out, "job": self.job}

    # ---- 装 ----
    def start(self, cid: str) -> str | None:
        """开始装; 不能装时返回原因."""
        if self.task and not self.task.done():
            return f"正在装 {self.job['id']}, 等它装完"
        c = next((x for x in self.load().get("components", []) if x["id"] == cid), None)
        if not c:
            return "清单里没有这一样"
        s = self.source(c)
        if not s.get("files"):
            return "这一样还没有下载地址"
        if self.present(c):
            return f"{c['target']} 已经有了; 要重装先删掉"
        if s.get("unpack") == "7z" and not self.seven_zip.is_file():
            return f"缺解压工具 {self.seven_zip.name} (应该在 {self.seven_zip.parent}), 重装一次本体"
        total = sum(f["size"] for f in s["files"])
        d = self.dl / cid
        have = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.is_dir() else 0
        need = total - have + (c.get("unpacked", total) if s.get("unpack") else 0) + (512 << 20)  # 还要下的 + 解开的 + 余量
        probe = self.root / c["target"]
        while not probe.exists() and probe.parent != probe:  # 目标目录还没有: 看它所在的盘
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        if free < need:
            return f"磁盘空间不够: 要约 {need / 1e9:.1f} GB 空闲, 现在 {free / 1e9:.1f} GB"
        self.job = {"id": cid, "state": "downloading", "done": 0, "total": total, "unpack": 0, "error": "",
                    "started": time.time()}
        self.task = asyncio.get_running_loop().create_task(self._install(c, s))
        return None

    def cancel(self) -> bool:
        if self.task and not self.task.done():
            self.task.cancel()
            return True
        return False

    async def _install(self, c: dict, s: dict) -> None:
        job, d = self.job, self.dl / c["id"]
        d.mkdir(parents=True, exist_ok=True)
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "WorldModel"}) as http:
                gate = asyncio.Semaphore(PARALLEL)

                async def one(f: dict) -> None:
                    async with gate:
                        await self._fetch(http, s, f, d / s.get("rename", {}).get(f["path"], f["path"]), job)

                tasks = [asyncio.ensure_future(one(f)) for f in s["files"]]
                try:
                    await asyncio.gather(*tasks)
                except BaseException:
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
            job["state"] = "extracting"
            await asyncio.to_thread(self._place, c, s, d, job)
            shutil.rmtree(d, ignore_errors=True)
            job["state"] = "done"
        except asyncio.CancelledError:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
            job["state"], job["error"] = "cancelled", "取消了 (下了的留着, 下次接着下)"
        except Exception as exc:  # noqa: BLE001 - 原因交给页面
            job["state"], job["error"] = "error", f"{type(exc).__name__}: {exc}"[:300]

    async def _fetch(self, http: aiohttp.ClientSession, s: dict, f: dict, out: Path, job: dict) -> None:
        """下一个文件: 已经有完整且核对过的跳过; 有 .part 就从断的地方接着下; 一个地址不行换下一个; 下完核对."""
        if out.is_file() and out.stat().st_size == f["size"] and await asyncio.to_thread(digest, out, f):
            job["done"] += f["size"]
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.name + ".part")
        last: Exception | None = RuntimeError(f"{f['path']} 没有下载地址")
        for url in urls(s, f["path"]):
            have = part.stat().st_size if part.exists() else 0
            if have > f["size"]:
                part.unlink()
                have = 0
            counted = have
            job["done"] += have
            try:
                async with http.get(url, headers={"Range": f"bytes={have}-"} if have else {}) as r:
                    if r.status == 200 and have:  # 不支持接着下: 从头来
                        job["done"] -= have
                        counted = have = 0
                    elif r.status not in (200, 206):
                        raise RuntimeError(f"HTTP {r.status}")
                    with open(part, "ab" if have else "wb") as fp:
                        async for chunk in r.content.iter_chunked(CHUNK):
                            fp.write(chunk)
                            job["done"] += len(chunk)
                            counted += len(chunk)
                last = None
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError) as exc:
                job["done"] -= counted  # 这个地址算的进度退回去, 换下一个地址时重新算
                last = RuntimeError(f"下载 {f['path']} 失败 ({url.split('/')[2]}: {exc})")
        if last:
            raise last
        if part.stat().st_size != f["size"]:
            raise RuntimeError(f"{f['path']} 没下完 ({part.stat().st_size} / {f['size']} 字节), 再点一次接着下")
        if not await asyncio.to_thread(digest, part, f):
            part.unlink()
            job["done"] -= f["size"]
            raise RuntimeError(f"{f['path']} 校验不对, 已删掉, 再点一次重下")
        part.replace(out)

    def _place(self, c: dict, s: dict, d: Path, job: dict) -> None:
        """下好的放进目标目录. 有压缩包先解到旁边的临时目录 (只有一层目录就去掉这一层), 中途出错不留半个.
        目标目录不在就整个改名过去; 在 (和别的组件共用一个目录) 就一个个文件挪进去, 有同名的先停下, 什么都不动."""
        target = self.root / c["target"]
        stage = target.with_name(target.name + ".installing")
        if s.get("unpack"):
            shutil.rmtree(stage, ignore_errors=True)
            stage.mkdir(parents=True)
            for f in s["files"]:
                self._unpack(s["unpack"], d / f["path"], stage, job)
            kids = list(stage.iterdir())
            ready = kids[0] if len(kids) == 1 and kids[0].is_dir() else stage
            files = sorted(p.relative_to(ready).as_posix() for p in ready.rglob("*") if p.is_file())
        else:
            ready = d
            files = sorted(s.get("rename", {}).get(f["path"], f["path"]) for f in s["files"])
        if target.exists() or not s.get("unpack"):
            clash = [p for p in files if (target / p).exists()]
            if clash:
                raise RuntimeError(f"{c['target']} 里已经有 {clash[0]} 等 {len(clash)} 个同名文件, 没有覆盖")
            for p in files:
                (target / p).parent.mkdir(parents=True, exist_ok=True)
                os.replace(ready / p, target / p)
        else:  # 解开的几万个文件 (GPT-SoVITS) 整个改名过去, 快
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(ready, target)
        if s.get("unpack"):
            shutil.rmtree(stage, ignore_errors=True)
        (target / mark_of(c["id"])).write_text(json.dumps(
            {"id": c["id"], "variant": s.get("label", ""), "rev": s.get("rev", ""), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "files": files}, ensure_ascii=False), encoding="utf-8")

    def _unpack(self, kind: str, archive: Path, stage: Path, job: dict) -> None:
        if kind == "zip":
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():  # 不让压缩包往临时目录外面写
                    if not str((stage / info.filename).resolve()).startswith(str(stage.resolve())):
                        raise RuntimeError(f"压缩包里有不对的路径: {info.filename}")
                zf.extractall(stage)
            return
        if kind != "7z":
            raise RuntimeError(f"不认识的压缩格式: {kind}")
        # 7-Zip 本身不解绝对路径和 ".."; -bsp1 把进度打到标准输出, 读里面的百分比给页面
        self.proc = subprocess.Popen([str(self.seven_zip), "x", str(archive), f"-o{stage}", "-y", "-bso0", "-bse1", "-bsp1"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tail = b""
        while chunk := self.proc.stdout.read(256):
            tail = (tail + chunk)[-2000:]
            if pct := re.findall(rb"(\d+)%", chunk):
                job["unpack"] = int(pct[-1])
        if self.proc.wait() != 0:
            raise RuntimeError(f"解压 {archive.name} 失败: " + tail.decode("utf-8", "replace").strip()[-200:])
        job["unpack"] = 100

    def remove(self, cid: str) -> str | None:
        """删掉从这里装的文件 (按 .wm-component-<id>.json 里记的), 再去掉空了的目录; 你后来自己放进去的不碰."""
        c = next((x for x in self.load().get("components", []) if x["id"] == cid), None)
        if not c:
            return "清单里没有这一样"
        target = self.root / c["target"]
        mark = target / mark_of(cid)
        try:
            files = json.loads(mark.read_text(encoding="utf-8"))["files"]
        except (OSError, ValueError, KeyError):
            return "这一样不是从这里装的, 不替你删"
        base = target.resolve()
        for p in files:
            f = (target / p).resolve()
            if base in f.parents and f.is_file():
                f.unlink()
        mark.unlink()
        for dirpath, _, _ in sorted(os.walk(target), key=lambda w: -len(w[0])):  # 从最深的往上, 空了才删
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
        return None


def main(argv: list[str] | None = None) -> int:
    """python -m wm.components status | remove-all. remove-all: 卸载时用, 删掉从这里装的组件 (你自己放的、训练出的不碰)."""
    import argparse
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m wm.components")
    ap.add_argument("cmd", choices=["status", "remove-all"])
    a = ap.parse_args(argv)
    comp = Components(cap=0)
    for c in comp.status()["components"]:
        if a.cmd == "status":
            print(f"{c['id']:<12} {'装好了' if c['installed'] else '已经有了' if c['present'] else '没装':<6} {c['target']}")
        elif c["installed"]:
            print(f"删掉 {c['id']}: {comp.remove(c['id']) or '好了'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

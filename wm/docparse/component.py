"""识字组件 (RapidOCR) 装在哪、装没装、怎么把一个轮子解开.

装在 runtime/ocr/ (新目录): site/ 放解开的包, .wm-component-rapidocr.json 记装了哪些轮子、哈希、什么时候. 不装进系统 Python 的
site-packages, 也不装进便携版的: 好删 (删整个 runtime/ocr/), 不污染; 识字子进程自己把 runtime/ocr/site 加进 sys.path.

轮子本身就是 zip: 解开到 site/ 即可, 不跑 pip, 不执行任何安装脚本. 解的时候逐条检查路径 (不许 .. 、绝对路径、盘符、反斜杠开头),
顶层只收包目录 (name/…)、单个 .py / .pyd 模块、*.dist-info; 其余 (*.data/ 里的脚本、头文件, 顶层的杂文件) 一律拒, 整个轮子不解.
这一轮只用自己造的假轮子测它; 真轮子要等主人对下载清单点头、经下载队列下完核过哈希, 再单独装一次 (计划第 8 节).
卸载: 删 runtime/ocr/ 整个目录 (安装包不会替你删, 以后接到组件管理里再管).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import zipfile
from pathlib import Path, PurePosixPath

from . import OCR_DIR

COMPONENT_ID = "rapidocr"
MARK = f".wm-component-{COMPONENT_ID}.json"
MAX_FILES = 6000
MAX_UNPACKED = 400 * 1024 * 1024     # 一个轮子解开后最多多大 (opencv 的轮子约 40 MB 压缩、解开不到 100 MB)
MAX_ONE_FILE = 200 * 1024 * 1024


class WheelError(ValueError):
    """这个轮子不能解 (路径不对、结构不对、哈希不对)."""


def site_dir(ocr_dir: Path | str | None = None) -> Path:
    return Path(ocr_dir or OCR_DIR) / "site"


def state(ocr_dir: Path | str | None = None) -> dict:
    """装没装好. 只看标记文件和 site/ 在不在, 不去 import (那要起子进程). 装是否真能跑, 识字子进程第一次用时会说."""
    root = Path(ocr_dir or OCR_DIR)
    mark = root / MARK
    if not root.is_dir():
        return {"installed": False, "reason": "本机识字组件（RapidOCR）还没装"}
    if not (root / "site").is_dir() or not mark.is_file():
        return {"installed": False, "reason": "本机识字组件没装完整（缺 site 目录或安装记录）"}
    try:
        info = json.loads(mark.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"installed": False, "reason": "本机识字组件的安装记录读不出来"}
    if not isinstance(info, dict) or info.get("id") != COMPONENT_ID:
        return {"installed": False, "reason": "本机识字组件的安装记录不对"}
    return {"installed": True, "reason": "", "wheels": [w.get("name") for w in info.get("wheels", []) if isinstance(w, dict)]}


def _safe_name(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise WheelError(f"轮子里有不合规的路径: {name!r}")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise WheelError(f"轮子里有绝对路径: {name!r}")
    p = PurePosixPath(name)
    if ".." in p.parts:
        raise WheelError(f"轮子里有 .. 路径: {name!r}")
    if not p.parts:
        raise WheelError(f"轮子里有空路径: {name!r}")
    return p


def _check_top(entries: list[tuple[PurePosixPath, bool]]) -> None:
    """顶层只收: 包目录、单个 .py / .pyd 模块、*.dist-info/. 其余拒绝."""
    for path, is_dir in entries:
        top = path.parts[0]
        if len(path.parts) == 1 and not is_dir:
            if not top.lower().endswith((".py", ".pyd")):
                raise WheelError(f"轮子顶层有不是包目录也不是模块的文件: {top}")
            continue
        if top.endswith(".data"):
            raise WheelError(f"轮子里有 {top}/（脚本、头文件这类要装到别处的东西），不收")
        if not (top.endswith(".dist-info") or top.endswith(".libs") or top.isidentifier() or top.replace("-", "_").isidentifier()):
            raise WheelError(f"轮子顶层的目录名不像一个包: {top}")


def unpack_wheel(wheel: Path | str, site: Path | str, expected_sha256: str | None = None) -> list[str]:
    """把轮子解到 site/. 返回顶层名字. 先全部检查完再动磁盘, 任何一条不合规整个不解; 解到旁边的暂存目录再挪进去 (已经有的顶层名不覆盖)."""
    wheel, site = Path(wheel), Path(site)
    data = wheel.read_bytes() if expected_sha256 else None
    if expected_sha256:
        got = hashlib.sha256(data).hexdigest()
        if got.lower() != expected_sha256.lower():
            raise WheelError(f"轮子的 SHA-256 对不上（要 {expected_sha256[:12]}…，实际 {got[:12]}…）")
    try:
        zf = zipfile.ZipFile(wheel)
    except (OSError, zipfile.BadZipFile) as exc:
        raise WheelError(f"轮子不是合法的 zip: {exc}") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise WheelError(f"轮子里文件太多（{len(infos)} 个）")
        entries, total = [], 0
        for info in infos:
            path = _safe_name(info.filename)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise WheelError(f"轮子里有符号链接: {info.filename}")
            if info.file_size > MAX_ONE_FILE:
                raise WheelError(f"轮子里有过大的文件: {info.filename}")
            total += info.file_size
            if total > MAX_UNPACKED:
                raise WheelError("轮子解开后太大")
            entries.append((path, info.is_dir()))
        _check_top(entries)
        site.mkdir(parents=True, exist_ok=True)
        tops = sorted({p.parts[0] for p, _ in entries})
        for top in tops:
            if (site / top).exists():
                raise WheelError(f"site 里已经有 {top}，不覆盖")
        stage = site.parent / f".stage-{os.getpid()}-{int(time.time() * 1000)}"
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        try:
            base = stage.resolve()
            for info, (path, is_dir) in zip(infos, entries):
                target = (stage / Path(*path.parts)).resolve()
                if base != target and base not in target.parents:
                    raise WheelError(f"轮子里的路径会跑到目录外面: {info.filename}")
                if is_dir:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
            for top in tops:
                os.replace(stage / top, site / top)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    return tops


def write_mark(ocr_dir: Path | str, wheels: list[dict]) -> None:
    """装完写安装记录. wheels: [{name, sha256, tops}]."""
    root = Path(ocr_dir)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (MARK + ".tmp")
    tmp.write_text(json.dumps({"id": COMPONENT_ID, "installed_at": time.time(), "wheels": wheels}, ensure_ascii=False, indent=1),
                   encoding="utf-8", newline="\n")
    os.replace(tmp, root / MARK)

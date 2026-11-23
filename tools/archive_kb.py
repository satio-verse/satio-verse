"""把测试数据、图像存进知识库 (2026-10-27 用户: "一定要把所有测试数据、图像都保存到知识库中, 方便我们后期复盘、思考、写论文、发表内容等").

  python tools/archive_kb.py                      # 增量同步: 新的、改过的才拷, 重写各类的索引
  python tools/archive_kb.py --dry-run            # 只列要拷什么、多大
  python tools/archive_kb.py --extra <目录> --as 会话诊断/<名字>   # 会话自己的临时输出 (scratchpad 里的诊断结果、截图) 一起存
  python tools/archive_kb.py --kb <知识库目录>    # 默认 <vault>/WorldModel知识库

存到 <知识库>/11 实验数据/<分类>/ (保持来源的相对路径), 每类一篇 索引.md (自动生成, 每次重写), 总索引 11 实验数据/索引.md;
索引里的 .md 写成 [[链接]], 另列"正文里提到的笔记" (知识库别的笔记里写到这一类、这个文件夹的路径), Obsidian 图谱里就连上了;
清单 11 实验数据/manifest.json 记每个文件的来源、大小、sha256、存入时间. 来源没变 (大小和修改时间一样) 就不再拷.
BMP 转成 PNG (单张截图, 无损) 或 JPEG 质量 90 (连拍的帧, 一段几百 MB 的 BMP 缩到十几 MB), 连拍文件夹另外拼一张总览图.
不拷:
  - 设置 (含密钥、token)、测试用的记忆库副本、浏览器数据、程序;
  - 真实使用的日志 (latency-*、social/、serve/、services/、billing/、cognition/: 里面有用户说的话、她自己的经历, 不是测试数据);
  - 文本里像密钥的 (sk-..., "api_key"/"token"/"secret"/"password": "...") 整个文件跳过, 打印出来.
知识库文件夹在记忆检索里是排除的 (memory.exclude), 这些数据不会被当成"关于用户的事".
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KB_DEFAULT = Path(os.environ.get("WM_KB_DIR", str(Path.home() / "WorldModel知识库")))
SUB = "11 实验数据"
MODELS = Path("E:/MyCharaterModels/3d/output")

TEXT = {".json", ".jsonl", ".txt", ".log", ".md", ".csv", ".tsv", ".out", ".sha256"}
IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MEDIA = {".mp4", ".webm", ".wav", ".mp3", ".ogg", ".flac"}
CODE = {".js", ".mjs", ".html", ".css", ".py", ".sh", ".ps1", ".cmd", ".bat", ".diff", ".patch"}  # 只在 --extra (会话的诊断目录) 里收: 当时用的脚本和跑法, 复现要用
SKIP_DIRS = re.compile(r"(settings|vault|webview2|EBWebView|node_modules|__pycache__|\.git)$", re.I)
SECRET = re.compile(r'sk-[A-Za-z0-9]{16,}|"(?:api_?key|token|secret|password|access_token)"\s*:\s*"[^"]{8,}"', re.I)

# (分类, 来源目录, glob, 可否公开的说明). 来源是相对仓库的路径或绝对路径.
SOURCES = [
    ("动作评估", "logs", "motion-eval*", ""),
    ("动作评估", "logs/motion-checkup", "**/*", ""),
    ("动作评估", "logs/motion-tune", "**/*", ""),
    ("动作模型研究", "logs/motion-model", "**/*", ""),
    ("动作成长", "logs/motion-growth", "**/*", ""),  # 每次练的结果和换上/退回记录; 日常的失败片段是她的成长数据, 在 ~/.worldmodel/, 不收
    ("物理与端咖啡", "logs", "bench-carry*", ""),
    ("认知与小屋计划", "logs", "bench-cognition*", ""),
    ("小窝生活与可供性", "logs", "room-life-*/**/*", ""),  # 活动分布的统计、图、快进原始数据
    ("小屋性能", "logs", "room-perf-*", ""),  # tools/room_perf.mjs 的逐层计时结果
    ("认知与小屋计划", "logs", "room-plan*", ""),
    ("认知与小屋计划", "logs", "room-event*", ""),
    ("记忆与对话评测", "logs", "longmem-bench*", ""),
    ("记忆与对话评测", "logs", "recall-bench*", ""),
    ("记忆与对话评测", "logs", "role-bench*", ""),
    ("记忆与对话评测", "logs", "promise-bench*", ""),
    ("记忆与对话评测", "logs", "saydo-bench*", ""),
    ("记忆与对话评测", "logs", "digest*", ""),
    ("记忆与对话评测", "logs", "channel*", ""),
    ("延迟与语音", "logs", "bench-2*", ""),
    ("延迟与语音", "logs", "tts-bench*", ""),
    ("延迟与语音", "logs", "voice-loop*", ""),
    ("延迟与语音", "logs/tts-samples", "**/*", "仅本机: 爱弥斯音色素材未获授权, 不可公开"),
    ("模型转换", "logs", "vrm-*", ""),
    ("物理仿真实验", "logs/protomotions", "**/*", ""),
    ("截图", "logs", "*.png", ""),
    ("桌宠自检", "desktop/build/selftest", "**/*", ""),
    ("模型修订预览", MODELS / "revision_03", "previews/**/*", "角色模型是用户自己的; 内衣特写等仅内部复盘"),
    ("模型修订预览", MODELS / "revision_03", "validation*.json", ""),
    ("模型修订预览", MODELS / "revision_02", "previews/**/*", "同上"),
    ("模型修订预览", MODELS / "revision_02", "*.json", ""),
    ("模型修订预览", MODELS, "previews/**/*", "同上"),
    ("模型修订预览", MODELS, "*validation*.json", ""),
]


def wanted(p: Path, code: bool = False) -> bool:
    ext = p.suffix.lower()
    kinds = TEXT | IMAGE | MEDIA | (CODE if code else set())
    return ext in kinds and not any(SKIP_DIRS.search(part) for part in p.parts[:-1])


def secret_in(p: Path) -> bool:
    if p.suffix.lower() not in TEXT | CODE or p.stat().st_size > 50_000_000:
        return False
    try:
        return bool(SECRET.search(p.read_text(encoding="utf-8", errors="ignore")))
    except OSError:
        return False


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def collect(extra: list[tuple[Path, str]]) -> list[tuple[str, Path, Path, str]]:
    """[(分类, 来源文件, 分类下的相对路径, 说明)]."""
    out = []
    for cat, base, pattern, note in SOURCES:
        base = Path(base) if Path(base).is_absolute() else ROOT / base
        if not base.exists():
            continue
        for p in sorted(base.glob(pattern)):
            if p.is_file() and wanted(p.relative_to(base)):
                rel = p.relative_to(base)
                # 同一分类下来自不同目录的: 前面加来源目录名, 免得重名
                prefix = Path(base.name) if base != ROOT / "logs" else Path()
                out.append((cat, p, prefix / rel, note))
    for d, target in extra:
        cat, _, name = target.partition("/")
        for p in sorted(d.rglob("*")):
            if p.is_file() and wanted(p.relative_to(d), code=True):
                out.append((cat, p, Path(name or d.name) / p.relative_to(d), ""))
    return out


def film_dir(p: Path) -> bool:
    return p.parent.name.startswith("film")


def place(src: Path, dst: Path) -> Path:
    """拷过去; BMP 转 PNG (连拍的帧转 JPEG). 返回实际写的路径."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".bmp":
        from PIL import Image
        img = Image.open(src).convert("RGB")
        ext = ".jpg" if film_dir(src) else ".png"
        # 来源旁边已经有同名的 .png/.jpg (同一张图两种格式都存了): 转出来的叫 x.bmp.png, 不互相覆盖
        out = dst.with_name(dst.name + ext) if src.with_suffix(ext).exists() else dst.with_suffix(ext)
        if ext == ".jpg":
            img.save(out, quality=90, optimize=True)
        else:
            img.save(out, optimize=True)
        return out
    shutil.copy2(src, dst)
    return dst


def contact_sheet(folder: Path, cols: int = 6, width: int = 1800) -> Path | None:
    """连拍文件夹拼一张总览图 (均匀挑最多 24 帧)."""
    from PIL import Image
    frames = sorted(p for p in folder.iterdir() if p.suffix.lower() in {".jpg", ".png"} and not p.name.startswith("_"))
    if len(frames) < 2:
        return None
    pick = [frames[round(i * (len(frames) - 1) / 23)] for i in range(min(24, len(frames)))]
    pick = list(dict.fromkeys(pick))
    first = Image.open(pick[0])
    w = width // cols
    h = round(first.height * w / first.width)
    rows = (len(pick) + cols - 1) // cols
    sheet = Image.new("RGB", (w * cols, h * rows), "white")
    for i, p in enumerate(pick):
        sheet.paste(Image.open(p).convert("RGB").resize((w, h)), ((i % cols) * w, (i // cols) * h))
    out = folder / "_总览.jpg"
    sheet.save(out, quality=88)
    return out


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


NAME_CHAR = re.compile(r"[\w\-.]")  # 名字里会有的字: 紧跟着这些的是另一个更长的名字 ("r1a-定位轮" 不算提到 "r1a-定位轮复核")
MAIN_NOTE = ("说明.md", "readme.md", "summary.md", "findings.md", "handoff.md")  # 一个实验文件夹先链哪篇


def mentions(text: str, needle: str) -> bool:
    """text 里写到了 needle, 而且不是更长的名字的开头."""
    i = text.find(needle)
    while i >= 0:
        j = i + len(needle)
        if j >= len(text) or not NAME_CHAR.match(text[j]):
            return True
        i = text.find(needle, j)
    return False


def referrers(base: Path, keys: dict[str, str]) -> dict[str, list[str]]:
    """知识库里实验数据以外的笔记, 哪些写到了这些路径: {键: [笔记 (相对知识库, 不带 .md)]}. keys 是 键 -> 要找的字."""
    kb = base.parent
    notes = sorted(kb.glob("*.md")) + sorted(p for d in kb.iterdir() if d.is_dir() and d != base for p in d.rglob("*.md"))
    out: dict[str, list[str]] = {k: [] for k in keys}
    for p in notes:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        note = p.relative_to(kb).with_suffix("").as_posix()
        for k, needle in keys.items():
            if mentions(text, needle):
                out[k].append(note)
    return out


def main_note(mds: list[str], folder: str) -> str | None:
    """文件夹里先链哪篇: 直接放在里面的 说明 / README / summary…, 没有就挑路径最短的一篇 .md."""
    lower = {p.lower(): p for p in mds}
    for name in MAIN_NOTE:
        if f"{folder}/{name}".lower() in lower:
            return lower[f"{folder}/{name}".lower()]
    return min(mds, key=lambda p: (p.count("/"), len(p))) if mds else None


def ref_lines(cat: str, recs: list[dict], refs: dict[str, list[str]]) -> list[str]:
    """"正文里提到的笔记": 被提到的文件夹一行一个 (链到它的说明); 只写到这一类、单个文件或带 * 的另起一行; 没被提到的文件夹列个名字."""
    def link(n: str) -> str:
        return f"[[{n}|{Path(n).name}]]"

    folders: dict[str, list[str]] = {}
    for r in recs:
        parts = r["path"].split("/")
        if len(parts) > 1:
            folders.setdefault(parts[0], [])
            if r["path"].lower().endswith(".md"):
                folders[parts[0]].append(r["path"])
    lines, shown, quiet = [], set(), []
    for f in sorted(folders):
        who = refs.get(f"{cat}/{f}") or []
        if not who:
            quiet.append(f)
            continue
        main = main_note(folders[f], f)
        label = f"[[{SUB}/{cat}/{main[:-3]}|{f}]]" if main else f"`{f}/`"
        lines.append(f"- {label}：" + " · ".join(map(link, who)))
        shown.update(who)
    rest = [n for n in refs.get(cat) or [] if n not in shown]
    if rest:
        lines.append("- 这一类（没指到具体文件夹的）：" + " · ".join(map(link, rest)))
    if not lines:
        return []
    if quiet:
        lines.append("- 没被正文提到的文件夹：" + "、".join(f"`{f}/`" for f in quiet))
    return ["## 正文里提到的笔记", "", f"知识库里 `{SUB}` 以外的笔记写到了这些路径（每次归档时重算）。", ""] + lines + [""]


def write_indexes(base: Path, manifest: dict) -> None:
    by_cat: dict[str, list[dict]] = {}
    for rec in manifest["files"].values():
        by_cat.setdefault(rec["category"], []).append(rec)
    # 正文里写到的: 整个分类 ("11 实验数据/物理与端咖啡", 写到单个文件、带 * 的也算) 和每个分类的第一层文件夹 ("会话诊断/记忆页-20261101")
    keys = {}
    for cat, recs in by_cat.items():
        keys[cat] = f"{SUB}/{cat}"
        keys.update({f"{cat}/{p[0]}": f"{cat}/{p[0]}" for p in (r["path"].split("/") for r in recs) if len(p) > 1})
    refs = referrers(base, keys)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = ["# 实验数据与图像", "", f"自动生成（`python tools/archive_kb.py`，{now}），别手改；规则见 [[归档规则]]。", "",
             "| 分类 | 文件数 | 大小 |", "|---|---|---|"]
    for cat in sorted(by_cat):
        recs = by_cat[cat]
        lines.append(f"| [[{SUB}/{cat}/索引\\|{cat}]] | {len(recs)} | {human(sum(r['size'] for r in recs))} |")
        cl = [f"# {cat}", "", f"自动生成（{now}），别手改。图片可以在实验记录里用 `![[{SUB}/{cat}/<路径>]]` 引用。", ""]
        notes = sorted({r["note"] for r in recs if r["note"]})
        if notes:
            cl += ["**注意**：" + "；".join(notes), ""]
        cl += ref_lines(cat, recs, refs)
        sheets = sorted(r["path"] for r in recs if r["path"].endswith("_总览.jpg"))
        if sheets:
            cl += ["## 连拍总览", ""] + [f"### {Path(s).parent.name}\n\n![[{SUB}/{cat}/{s}]]\n" for s in sheets]
        imgs = sorted(r["path"] for r in recs if Path(r["path"]).suffix.lower() in IMAGE and Path(r["path"]).parent.name[:4] != "film")
        if imgs:
            cl += ["## 图像", ""] + [f"![[{SUB}/{cat}/{p}|480]]\n`{p}`\n" for p in imgs[:200]]
            if len(imgs) > 200:
                cl.append(f"（另有 {len(imgs) - 200} 张没在这里展开，见下表。）\n")
        cl += ["## 全部文件", "", "| 文件 | 大小 | 来源修改时间 | 来源 |", "|---|---|---|---|"]
        for r in sorted(recs, key=lambda r: r["path"]):
            # .md 写成链接 (表格里的 | 要写成 \|), 图谱里这些笔记就挂在本类的索引上, 不再是孤点
            name = (f"[[{SUB}/{cat}/{r['path'][:-3]}\\|{r['path']}]]" if r["path"].lower().endswith(".md")
                    else f"`{r['path']}`")
            cl.append(f"| {name} | {human(r['size'])} | {r['mtime']} | `{r['source']}` |")
        (base / cat / "索引.md").write_text("\n".join(cl) + "\n", encoding="utf-8")
    (base / "索引.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kb", type=Path, default=KB_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reindex", action="store_true", help="不拷新文件, 只把磁盘上有、清单里没有的补登进清单, 重写索引")
    ap.add_argument("--extra", type=Path, action="append", default=[], help="另外要存的目录 (配 --as)")
    ap.add_argument("--as", dest="as_", action="append", default=[], help="对应 --extra 的 分类/名字")
    a = ap.parse_args()
    if len(a.extra) != len(a.as_):
        ap.error("--extra 和 --as 要一一对应")
    if not a.kb.is_dir():
        ap.error(f"没有知识库目录 {a.kb}")
    base = a.kb / SUB
    mpath = base / "manifest.json"
    if a.reindex:
        total, added = commit(base, {})
        print(f"补登 {added} 个; 清单里现在 {len(total['files'])} 个文件")
        return 0
    manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {"files": {}}
    files = collect(list(zip(a.extra, a.as_)))
    todo, skipped, total = [], [], 0
    for cat, src, rel, note in files:
        st = src.stat()
        key = str(src.resolve())
        old = manifest["files"].get(key)
        if old and old["src_size"] == st.st_size and old["src_mtime"] == int(st.st_mtime):
            continue
        if secret_in(src):
            skipped.append(src)
            continue
        todo.append((cat, src, rel, note, st))
        total += st.st_size
    print(f"要存 {len(todo)} 个文件 (来源共 {human(total)}), 已经存过的跳过; 像有密钥跳过 {len(skipped)} 个")
    for s in skipped:
        print("  像有密钥, 没存:", s)
    if a.dry_run:
        by = {}
        for cat, src, *_ , st in todo:
            n, s = by.get(cat, (0, 0))
            by[cat] = (n + 1, s + st.st_size)
        for cat, (n, s) in sorted(by.items()):
            print(f"  {cat}: {n} 个, {human(s)}")
        return 0
    films, mine = set(), {}  # 这次存的条目; 最后在锁里并进磁盘上最新的清单
    for i, (cat, src, rel, note, st) in enumerate(todo):
        out = place(src, base / cat / rel)
        if film_dir(src):
            films.add(out.parent)
        mine[str(src.resolve())] = {
            "category": cat, "path": out.relative_to(base / cat).as_posix(), "source": str(src), "note": note,
            "src_size": st.st_size, "src_mtime": int(st.st_mtime), "size": out.stat().st_size,
            "sha256": sha256(out), "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "archived": datetime.now().strftime("%Y-%m-%d %H:%M")}
        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{len(todo)}")
    for f in sorted(films):
        sheet = contact_sheet(f)
        if sheet:
            cat = sheet.relative_to(base).parts[0]
            mine[str(sheet)] = {
                "category": cat, "path": sheet.relative_to(base / cat).as_posix(), "source": "(拼出来的连拍总览)", "note": "",
                "src_size": 0, "src_mtime": 0, "size": sheet.stat().st_size, "sha256": sha256(sheet),
                "mtime": datetime.now().strftime("%Y-%m-%d %H:%M"), "archived": datetime.now().strftime("%Y-%m-%d %H:%M")}
    total, added = commit(base, mine)
    print(f"知识库里现在 {len(total['files'])} 个文件, {human(sum(r['size'] for r in total['files'].values()))}"
          f" (这次存 {len(mine)} 个, 补登磁盘上有、清单里没有的 {added} 个): {base}")
    return 0


@contextmanager
def locked(base: Path, wait_s: float = 600, stale_s: float = 1800):
    """清单的锁 (几个会话同时归档时, 读-并-写这一段一次只让一个进): 锁文件建不出来就等; 超过 stale_s 的当作上次崩了留下的."""
    lock = base / ".manifest.lock"
    t0 = time.time()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {datetime.now():%Y-%m-%d %H:%M:%S}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_s:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.time() - t0 > wait_s:
                raise SystemExit(f"等清单的锁等了 {wait_s:.0f} 秒 ({lock}); 别的归档还在写, 过一会儿再跑 --reindex")
            time.sleep(0.5)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def commit(base: Path, mine: dict) -> tuple[dict, int]:
    """在锁里: 重读磁盘上最新的清单, 并进这次的条目, 补登磁盘上有、清单里没有的文件, 原子地写回, 重写索引.
    (以前是开始时读、结束时整份写, 几个会话同时跑时后写完的把别人这段时间加的冲掉.) 返回 (清单, 补登了几个)."""
    base.mkdir(parents=True, exist_ok=True)
    mpath = base / "manifest.json"
    with locked(base):
        total = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {"files": {}}
        total["files"].update(mine)
        # 同一个存放位置只留一条: 来源记得清楚的条目盖过补登的
        seen = {(r["category"], r["path"]) for k, r in total["files"].items() if not k.startswith("kb:")}
        for k in [k for k, r in total["files"].items() if k.startswith("kb:") and (r["category"], r["path"]) in seen]:
            del total["files"][k]
        listed = {(r["category"], r["path"]) for r in total["files"].values()}
        added = 0
        for p in base.rglob("*"):
            if not p.is_file() or p.parent == base or p.name == "索引.md":
                continue
            rel = p.relative_to(base)
            cat, path = rel.parts[0], Path(*rel.parts[1:]).as_posix()
            if (cat, path) in listed:
                continue
            st = p.stat()
            total["files"][f"kb:{rel.as_posix()}"] = {
                "category": cat, "path": path, "source": "(补登: 磁盘上有、清单里没有, 来源不明)", "note": "",
                "src_size": 0, "src_mtime": 0, "size": st.st_size, "sha256": sha256(p),
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "archived": datetime.now().strftime("%Y-%m-%d %H:%M")}
            added += 1
        tmp = mpath.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(total, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, mpath)
        write_indexes(base, total)
    return total, added


if __name__ == "__main__":
    sys.exit(main())


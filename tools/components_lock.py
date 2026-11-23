"""把可选组件要下的文件锁定到清单里 (config/components.json; wm/components.py 照着下).

  python tools/components_lock.py              全部
  python tools/components_lock.py kokoro a2f   这几样
  python tools/components_lock.py --endpoint https://huggingface.co   不走镜像问官方 (默认 hf-mirror.com, 不走代理)

每样的 sources 里写官方 Hugging Face 仓库 (hf)、锁定的提交 (rev)、要哪些文件 (include / exclude, 通配符),
这里问 HF 的接口, 列出那个提交里的这些文件, 填上大小和哈希 (大文件 LFS 的 sha256, 小文件 git 的 blob sha1).
换新版本: 改 rev, 再跑一次. 我们自己不转手, 用户在设置里点了才从官方下.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "components.json"


def tree(endpoint: str, repo: str, rev: str) -> list[dict]:
    """一个提交里的全部文件 (接口一页最多 1000 条, 跟着 Link 头翻页). 镜像不走代理 (走代理反而慢)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url, out = f"{endpoint}/api/models/{repo}/tree/{rev}?recursive=true", []
    while url:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "WorldModel"}), timeout=60) as r:
            out += json.load(r)
            link = r.headers.get("Link") or ""
        url = next((p.split(";")[0].strip(" <>") for p in link.split(",") if 'rel="next"' in p), "")
    return [e for e in out if e.get("type") == "file"]


def pick(entries: list[dict], include: list[str], exclude: list[str]) -> list[dict]:
    files = []
    for e in sorted(entries, key=lambda e: e["path"]):
        p = e["path"]
        if any(fnmatch.fnmatch(p, pat) for pat in include) and not any(fnmatch.fnmatch(p, pat) for pat in exclude):
            lfs = e.get("lfs") or {}
            files.append({"path": p, "size": e["size"]} | ({"sha256": lfs["oid"]} if lfs.get("oid") else {"git": e["oid"]}))
    missing = [pat for pat in include if not any(fnmatch.fnmatch(f["path"], pat) for f in files)]
    if missing:
        raise SystemExit(f"这个提交里没有: {missing}")
    return files


def dump(m: dict) -> str:
    """缩进 2, 但 files 里每个文件占一行 (Kokoro 有 377 个文件)."""
    rows: dict[str, str] = {}
    for c in m["components"]:
        for s in c.get("sources", []):
            for i, f in enumerate(s.get("files", [])):
                key = f"@@{len(rows)}@@"
                rows[key] = json.dumps(f, ensure_ascii=False)
                s["files"][i] = key
    text = json.dumps(m, ensure_ascii=False, indent=2)
    for key, row in rows.items():
        text = text.replace(f'"{key}"', row)
    return text + "\n"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--endpoint", default="https://hf-mirror.com")
    a = ap.parse_args()
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for c in m["components"]:
        if a.ids and c["id"] not in a.ids:
            continue
        for s in c.get("sources", []):
            if not s.get("hf"):
                continue
            s["files"] = pick(tree(a.endpoint, s["hf"], s["rev"]), s.get("include", ["*"]), s.get("exclude", []))
            print(f"{c['id']} {s.get('label', '')}: {s['hf']}@{s['rev'][:8]} {len(s['files'])} 个文件, "
                  f"{sum(f['size'] for f in s['files']) / 1e6:.0f} MB", flush=True)
    MANIFEST.write_text(dump(m), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

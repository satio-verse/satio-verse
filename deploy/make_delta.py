"""造一个增量补丁，并把它的记录并进 update.json。

用法（在 satio-verse 仓库里）:

    python deploy/make_delta.py --old <旧安装包.exe> --new <新安装包.exe> \
        --from-version 0.3.0 --version 0.3.1 \
        --manifest deploy/release/update.json --out-dir deploy/release/patches

做的事:
  1. 算老包、新包的整包 sha256（老包的会写进 from_sha256，客户端拿它当底座指纹）；
  2. `zstd --patch-from <老> <新>` 生成补丁，再算补丁的 sha256 与大小；
  3. 把 {from_version,from_sha256,url,size,sha256} 并进 update.json 的 patches 数组
     （同一条记录重复跑会就地替换，不会越堆越多）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ZSTD = r"G:\RunProgram\WorldModel_v1\runtime\tools\zstd.exe"
PUBLIC_BASE = "https://satio-verse.gitgraduate.com/releases"


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def find_zstd(explicit: str | None) -> str:
    for cand in (explicit, os.environ.get("WM_ZSTD"), DEFAULT_ZSTD):
        if cand and os.path.isfile(cand):
            return cand
    on_path = shutil.which("zstd")
    if on_path:
        return on_path
    raise SystemExit("找不到 zstd.exe：用 --zstd 指定，或设 WM_ZSTD")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True, help="旧安装包")
    ap.add_argument("--new", required=True, help="新安装包")
    ap.add_argument("--from-version", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--manifest", default=os.path.join(ROOT, "deploy", "release", "update.json"))
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "deploy", "release", "patches"))
    ap.add_argument("--url-base", default=f"{PUBLIC_BASE}/patches")
    ap.add_argument("--zstd", default=None)
    ap.add_argument("--level", default="19")
    args = ap.parse_args(argv)

    zstd = find_zstd(args.zstd)
    os.makedirs(args.out_dir, exist_ok=True)
    name = f"patch-{args.from_version}-to-{args.version}.zst"
    patch_path = os.path.join(args.out_dir, name)

    print(f"老包 sha256 ...", flush=True)
    old_sha = sha256_of(args.old)
    print(f"   {old_sha}")
    print(f"新包 sha256 ...", flush=True)
    new_sha = sha256_of(args.new)
    print(f"   {new_sha}")

    cmd = [
        zstd, f"-{args.level}", "--long=31", f"--patch-from={args.old}",
        "-f", "-o", patch_path, args.new,
    ]
    print("生成补丁 ...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0 or not os.path.isfile(patch_path):
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-5:]
        raise SystemExit("zstd 失败: " + " / ".join(tail))

    patch_size = os.path.getsize(patch_path)
    patch_sha = sha256_of(patch_path)
    ratio = patch_size * 100.0 / os.path.getsize(args.new)
    print(f"补丁 {name}: {patch_size} B ({patch_size/1024/1024:.2f} MB, 新包的 {ratio:.2f}%)")
    print(f"   sha256 {patch_sha}")
    print(f"   可在 {patch_path} 取到，上传到 {args.url_base}/{name}")

    entry = {
        "from_version": args.from_version,
        "from_sha256": old_sha,
        "url": f"{args.url_base}/{name}",
        "size": patch_size,
        "sha256": patch_sha,
        "file": name,
    }

    if os.path.isfile(args.manifest):
        with open(args.manifest, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = {}
    patches = [p for p in (data.get("patches") or []) if p.get("from_version") != args.from_version]
    patches.append(entry)
    patches.sort(key=lambda p: p.get("from_version", ""))
    data["patches"] = patches
    with open(args.manifest, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"已并入 {args.manifest}（现在 {len(patches)} 条增量）")
    print("记得把补丁也传到 /srv/worldmodel-releases/patches/ 并挂到 GitHub Release。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

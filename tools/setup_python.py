"""便携 Python (docs/roadmap/own-harness.md 第 9 节 B "自带 Python"): 在 runtime/python 放一份官方嵌入版 Python 3.10 和服务端要的包,
桌宠 (wmpet.exe --services) 在 PATH 里找不到 Python 时用它 —— 别的电脑上不用另装 Python.

  python tools/setup_python.py            下载 python-3.10.11-embed-amd64.zip (python.org, 8.6 MB, PSF), 解开, 再用 pip 装
                                          requirements-portable.txt 里的包 (PyPI, 核心约 135 MB; 版本和本机装的一样)
  python tools/setup_python.py --check    只检查 runtime/python 能不能用 (导入每个包、起一次替身服务端)

下载的东西放在 downloads/python/ (先下到 .part 再改名, 断了重跑会重下). torch + transformers 不装 (只有记忆检索退回 bge 时用,
约 100–200 MB, 要的话另说). 要在有 Python 3.10 的电脑上跑这个脚本 (用它的 pip 装包; 嵌入版自己不带 pip).
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "runtime" / "python"
DL = ROOT / "downloads" / "python"
EMBED = "https://www.python.org/ftp/python/3.10.11/python-3.10.11-embed-amd64.zip"
REQ = ROOT / "requirements-portable.txt"
IMPORTS = ["aiohttp", "numpy", "scipy", "qrcode", "win32crypt", "psutil", "PIL", "jieba", "sherpa_onnx", "onnxruntime"]


def fetch(url: str, out: Path) -> Path:
    if out.exists():
        return out
    part = out.with_suffix(out.suffix + ".part")
    print(f"下载 {url}", flush=True)
    with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
        want = int(r.headers.get("Content-Length") or 0)
        shutil.copyfileobj(r, f, 1 << 20)
    if want and part.stat().st_size != want:  # 断了: 不改名, 重跑重下
        sys.exit(f"{out.name} 没下完 ({part.stat().st_size} / {want} 字节), 重跑一次")
    part.replace(out)
    return out


def setup() -> None:
    DL.mkdir(parents=True, exist_ok=True)
    z = fetch(EMBED, DL / Path(EMBED).name)
    print(f"SHA-256 {hashlib.sha256(z.read_bytes()).hexdigest()}", flush=True)
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)
    with zipfile.ZipFile(z) as zf:
        zf.extractall(DEST)
    # 嵌入版靠 python310._pth 定 sys.path (不看环境变量、不加当前目录): 加上 site-packages 和项目根目录 (../..), 打开 import site (认 .pth, pywin32 要)
    pth = next(DEST.glob("python3*._pth"))
    pth.write_text("python310.zip\n.\nLib\\site-packages\n..\\..\nimport site\n", encoding="ascii")
    site = DEST / "Lib" / "site-packages"
    site.mkdir(parents=True)
    no_user_site(site)
    t0 = time.time()
    # 用本机 Python 3.10 的 pip 往里装 (同一个小版本, 轮子通用); 只装 requirements-portable.txt 里的
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-warn-script-location", "--disable-pip-version-check",
                    "--target", str(site), "-r", str(REQ)], check=True)
    print(f"装好了 ({time.time() - t0:.0f} 秒)", flush=True)


def no_user_site(site: Path) -> None:
    """不读这台电脑用户目录里的包 (AppData/Roaming/Python): 自带的就只用自带的, 在本机测出来的和在别的电脑上一样."""
    (site / "_wm_no_user_site.pth").write_text(
        # .pth 的 import 行是在函数里 exec 的, 推导式看不到这里的名字: 不用推导式
        "import sys, site; site.ENABLE_USER_SITE = False; u = site.getusersitepackages(); u in sys.path and sys.path.remove(u)\n",
        encoding="ascii")


def size_mb(p: Path) -> float:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6


def check() -> bool:
    py = DEST / "python.exe"
    if py.is_file():
        no_user_site(DEST / "Lib" / "site-packages")  # 旧的装法没有这一条: 补上
    if not py.is_file():
        print("还没有 runtime/python (先跑 python tools/setup_python.py)")
        return False
    ok = True
    for m in IMPORTS:
        r = subprocess.run([str(py), "-c", f"import {m}"], capture_output=True, text=True, cwd=str(ROOT))
        print(f"  {'✓' if r.returncode == 0 else '✗'} {m}{'' if r.returncode == 0 else '  ' + r.stderr.strip().splitlines()[-1]}")
        ok &= r.returncode == 0
    # 起一次替身服务端 (不连 QQ、不读记忆库): 和 serve.py 换服务端前的试跑一样
    r = subprocess.run([str(py), "-c", "from wm import workbench as W; ok, text = W.trial(); print(text); raise SystemExit(0 if ok else 1)"],
                       capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    print(f"  {'✓' if r.returncode == 0 else '✗'} 替身服务端: {(r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr).strip() else r.returncode}")
    ok &= r.returncode == 0
    print(f"runtime/python 共 {size_mb(DEST):.0f} MB")
    return ok


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if not a.check:
        if sys.version_info[:2] != (3, 10):
            sys.exit("要用 Python 3.10 跑 (嵌入版是 3.10, 用它的 pip 装同一个小版本的轮子)")
        setup()
    return 0 if check() else 1


if __name__ == "__main__":
    sys.exit(main())

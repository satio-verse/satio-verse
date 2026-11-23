"""把放在 character_models/ 里的角色模型 (MMD 的 PMX 或 Blender 的 .blend, 打包成 zip) 转成 VRM 1.0, 放进 models/vrm/.

  python tools/convert_vrm.py extract            # 每个 zip 解到 character_models/_src/<名字>/ (含套娃 zip)
  python tools/convert_vrm.py inspect <名字>      # 列出骨骼、表情 (形态键) 和材质, 用来核对映射
  python tools/convert_vrm.py convert [<名字>...] # 转换 (默认全部), 结果写 models/vrm/<名字>.vrm 和转换报告

需要先装 Blender 和插件: python tools/setup_models.py blender.
这些模型的使用条款各不相同 (见 MODELS 里的 terms), 转出来的 VRM 同样受原条款约束; 只在本机测试用, 不随项目分发.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "character_models"
OUT_SRC = SRC / "_src"
sys.path.insert(0, str(ROOT / "tools"))

# 名字 -> zip 文件名前缀、显示名、作者、来源和条款 (写进 VRM 的 meta, 也是给人看的)
MODELS = {
    "miomio": {"zip": "MIOMIOMIO~", "title": "MIOMIOMIO~", "author": "流光Luli", "source": "模之屋",
               "terms": "压缩包里没有使用条款, 以模之屋原页面为准; 按非商用对待"},
    # 主文件 Wendy.pmx 是完整服装; 另附的 Base / Full 是变体
    "wendy": {"zip": "Wendy(pmx变换)", "file": "Wendy.pmx", "title": "Wendy",
              "drop_materials": ["Sleeve1.L", "Sleeve2.L", "Sleeve1.R", "Sleeve2.R", "Tail"],  # 去掉袖子和尾巴 (按用户要求)
              "author": "原作 booth.pm/ja/items/7841391 (PMX 转换: klefer)",
              "source": "模之屋", "terms": "Booth 模型的转换版, 条款以原作者为准; 按非商用对待"},
    "aike": {"zip": "猫之城艾可闪亮捕猎者", "title": "艾可 闪亮捕猎者", "author": "柒初梦 (IP: 猫之城)", "source": "模之屋",
             "terms": "仅供个人展示与交流学习, 严禁商用, 不得二次配布; 最终解释权归猫之城"},
    "miku_racing2021": {"zip": "野餐式2021赛车初音休闲服", "title": "野餐式 2021 赛车初音 休闲服",
                        "author": "新生帝国野餐队队长 (Copyright: Crypton Future Media, INC.)", "source": "模之屋",
                        "terms": "禁止商业、政治、宗教、暴力、血腥、色情用途"},
}


def _decode(info: zipfile.ZipInfo) -> str:
    """zip 里的文件名: 没有 UTF-8 标记时按 GBK / Shift-JIS 里更像正常文字的那个解."""
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode("cp437")
    best, score = info.filename, -1
    for enc in ("gbk", "shift_jis", "utf-8"):
        try:
            name = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        s = sum(1 for c in name if c.isascii() or "぀" <= c <= "ヿ" or "一" <= c <= "鿿")
        if s > score:
            best, score = name, s
    return best


def _safe_extract(z: zipfile.ZipFile, dest: Path) -> list[Path]:
    dest = dest.resolve()
    out = []
    for info in z.infolist():
        name = _decode(info).replace("\\", "/")
        target = (dest / name).resolve()
        if dest not in target.parents and target != dest:
            raise SystemExit(f"压缩包里有越界路径, 已停止: {name}")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(info) as fi, target.open("wb") as fo:
            fo.write(fi.read())
        out.append(target)
    return out


def extract() -> None:
    for name, m in MODELS.items():
        zips = [p for p in SRC.glob("*.zip") if p.name.startswith(m["zip"])]
        if not zips:
            print(f"{name}: 没找到 {m['zip']}*.zip, 跳过")
            continue
        dest = OUT_SRC / name
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zips[0]) as z:
            files = _safe_extract(z, dest)
        for inner in [f for f in files if f.suffix.lower() == ".zip"]:  # 套娃 zip (Wendy 的 Base / Full)
            with zipfile.ZipFile(inner) as z:
                files += _safe_extract(z, inner.with_suffix(""))
        kinds = sorted({f.suffix.lower() for f in files})
        print(f"{name}: {len(files)} 个文件 -> {dest.relative_to(ROOT)}  {kinds}")


def find_source(name: str) -> Path:
    """模型主文件: MODELS 里指定了就用它, 否则 PMX 优先 (有多个时取最大的), 再找 .blend."""
    base = OUT_SRC / name
    if MODELS[name].get("file"):
        return base / MODELS[name]["file"]
    for pattern in ("*.pmx", "*.blend"):
        found = sorted(base.rglob(pattern), key=lambda p: -p.stat().st_size)
        if found:
            return found[0]
    raise SystemExit(f"{name}: {base} 里没有 .pmx 或 .blend, 先运行 extract")


def blender(script_args: list[str]) -> int:
    from setup_models import blender_exe
    exe = blender_exe()
    if not exe.exists():
        raise SystemExit("没有 Blender, 先运行: python tools/setup_models.py blender")
    # -Y: 不执行 .blend 里自带的脚本和驱动器 (模型文件来自网上, 不信任)
    cmd = [str(exe), "--background", "-Y", "--python-exit-code", "1", "--python", str(ROOT / "tools" / "blender_vrm.py"), "--", *script_args]
    return subprocess.call(cmd)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["extract", "inspect", "convert"])
    ap.add_argument("names", nargs="*")
    args = ap.parse_args()
    if args.cmd == "extract":
        extract()
        return
    names = args.names or list(MODELS)
    out_dir = ROOT / "models" / "vrm"
    for name in names:
        src = find_source(name)
        kind = "inspect" if args.cmd == "inspect" else "convert"
        job = {"cmd": args.cmd, "name": name, "src": str(src), "out": str(out_dir / f"{name}.vrm"),
               "report": str(ROOT / "logs" / f"vrm-{name}-{kind}.json"), "meta": MODELS[name]}
        print(f"== {name}: {src.relative_to(ROOT)}")
        code = blender([json.dumps(job, ensure_ascii=False)])
        if code:
            raise SystemExit(f"{name}: Blender 退出码 {code}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    main()

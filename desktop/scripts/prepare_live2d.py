"""把 Live2D 的 SDK 和 Eustia 模型从 character_models 的压缩包里取出来, 放到不进版本库的 desktop/third_party/ (桌宠的 2D 门面用).

    python desktop/scripts/prepare_live2d.py

- SDK (CubismSdkForWeb-5-r.5.zip) -> desktop/third_party/live2d/
  - core/: live2dcubismcore.min.js 等可再分发的三个文件, 原样, 加上 LICENSE.md 和 RedistributableFiles.txt;
  - framework/: Framework 的 TypeScript 用 Node 自带的类型去除转成 JS (strip_ts.mjs), 只转换不改逻辑;
  - shaders/: Framework 的 WebGL 着色器, 原样;
  - LICENSE.md、NOTICE.md (SDK 根目录的) 和 Framework 的 LICENSE.md.
- 模型 (live2d_models 里的压缩包, VTube Studio 导出的 Eustia_vts/) -> desktop/third_party/live2d-models/Eustia/
  - 文件名换成英文 (URL 里不带中文), 改写 model3.json 里的引用; 作者的编辑工程 .cmo3 和 .psd 不取;
  - 写一份 pet-model.json: 表情和动作的文件列表、服务端的情绪对应哪个表情、这个身体能做哪些动作.
许可: Core 是 Live2D 专有许可 (不能改、不能单独分发、不能传公开仓库), Framework 是 Live2D Open Software License,
Eustia 没有附许可. 都只在本机用, third_party/ 不进版本库. 详见 docs/roadmap/desktop-pet.md 的 5.4.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESKTOP = HERE.parent
PROJECT = DESKTOP.parent
SDK_ZIP = PROJECT / "character_models" / "CubismSdkForWeb-5-r.5.zip"
MODEL_ZIP = next((PROJECT / "character_models" / "live2d_models").glob("真·工程7_by_*.zip"), None)
OUT = DESKTOP / "third_party" / "live2d"
MODELS = DESKTOP / "third_party" / "live2d-models"

# Eustia 的表情: 原名 -> 英文文件名
EXPRESSIONS = {"严肃": "serious", "含情脉脉": "tender", "心虚": "guilty", "皱眉": "frown", "红温": "flushed",
               "遗憾": "regret", "鄙视": "scorn", "震惊": "shocked"}
# 服务端的情绪 (wm/intent.py 的 EMOTIONS) -> 表情. neutral、calm 不加表情
EMOTIONS = {"happy": "含情脉脉", "excited": "含情脉脉", "sad": "遗憾", "shy": "红温", "angry": "皱眉", "surprised": "震惊"}
# 手是交握在身前的一张图, 没有能单独动的手臂: 只报头和身体做得到的
SKILLS = ["nod", "shake", "think", "bow"]


def zip_name(info: zipfile.ZipInfo) -> str:
    """压缩包里的中文文件名: 没标 UTF-8 的按 GBK 解."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("gbk")
    except UnicodeError:
        return info.filename


def prepare_sdk() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    zf = zipfile.ZipFile(SDK_ZIP)
    prefix = "CubismSdkForWeb-5-r.5/"
    src_tmp = OUT / "_ts"
    for info in zf.infolist():
        name = zip_name(info)
        if info.is_dir() or not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if rel in ("LICENSE.md", "NOTICE.md", "cubism-info.yml"):
            dst = OUT / rel
        elif rel.startswith("Core/") and rel.split("/")[-1] in ("live2dcubismcore.min.js", "live2dcubismcore.js", "live2dcubismcore.d.ts",
                                                                   "LICENSE.md", "RedistributableFiles.txt"):
            dst = OUT / "core" / rel.split("/")[-1]
        elif rel == "Framework/LICENSE.md":
            dst = OUT / "framework" / "LICENSE.md"
        elif rel.startswith("Framework/Shaders/WebGL/"):
            dst = OUT / "shaders" / rel.split("/")[-1]
        elif rel.startswith("Framework/src/"):
            dst = src_tmp / rel[len("Framework/src/"):]
        else:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(zf.read(info))
    r = subprocess.run(["node", str(HERE / "strip_ts.mjs"), str(src_tmp), str(OUT / "framework")], capture_output=True, text=True,
                       encoding="utf-8")
    print(r.stdout.strip())
    if r.returncode != 0:
        print(r.stderr[-2000:])
        sys.exit("Framework 转换失败")
    shutil.rmtree(src_tmp)


def prepare_model() -> None:
    if MODEL_ZIP is None:
        sys.exit("character_models/live2d_models 里找不到 Eustia 的压缩包")
    dst = MODELS / "Eustia"
    if dst.exists():
        shutil.rmtree(dst)
    zf = zipfile.ZipFile(MODEL_ZIP)
    files = {}
    for info in zf.infolist():
        name = zip_name(info)
        if info.is_dir() or not name.startswith("Eustia_vts/"):
            continue
        files[name[len("Eustia_vts/"):]] = zf.read(info)
    out = {}
    expressions, motions = [], []
    for rel, data in files.items():
        base = rel.split("/")[-1]
        if base.endswith(".moc3"):
            out["eustia.moc3"] = data
        elif base.endswith(".physics3.json"):
            out["eustia.physics3.json"] = data
        elif base.endswith(".cdi3.json"):
            out["eustia.cdi3.json"] = data
        elif base.endswith(".png") and "texture" in base:
            out[f"textures/{base}"] = data
        elif base.endswith(".exp3.json"):
            cn = base[: -len(".exp3.json")]
            en = EXPRESSIONS.get(cn, f"exp{len(expressions)}")
            out[f"expressions/{en}.exp3.json"] = data
            expressions.append({"name": cn, "file": f"expressions/{en}.exp3.json"})
        elif base.endswith(".motion3.json"):
            out[f"motions/{base}"] = data
            motions.append(f"motions/{base}")
        elif base == "icon.jpg":
            out["icon.jpg"] = data
    model3 = json.loads(next(d for r, d in files.items() if r.endswith(".model3.json")).decode("utf-8"))
    refs = model3["FileReferences"]
    refs["Moc"] = "eustia.moc3"
    refs["Textures"] = [f"textures/{t.split('/')[-1]}" for t in refs["Textures"]]
    refs["Physics"] = "eustia.physics3.json"
    refs["DisplayInfo"] = "eustia.cdi3.json"
    out["eustia.model3.json"] = json.dumps(model3, ensure_ascii=False, indent=1).encode("utf-8")
    pet = {
        "name": "Eustia", "display": "尤斯蒂娅", "model3": "eustia.model3.json",
        "expressions": sorted(expressions, key=lambda e: e["file"]), "motions": sorted(motions),
        "emotions": EMOTIONS, "skills": SKILLS,
        "source": "character_models/live2d_models (模之屋, 作者 李·工号114514); 没有附许可, 只在本机用",
    }
    out["pet-model.json"] = json.dumps(pet, ensure_ascii=False, indent=1).encode("utf-8")
    for rel, data in out.items():
        p = dst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    print(f"Eustia: {len(out)} 个文件, {len(expressions)} 个表情, {len(motions)} 个动作 -> {dst}")


def main() -> None:
    prepare_sdk()
    prepare_model()
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"SDK: {sum(1 for p in OUT.rglob('*') if p.is_file())} 个文件, {size / 1e6:.1f} MB -> {OUT}")


if __name__ == "__main__":
    main()

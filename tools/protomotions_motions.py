"""把 ProtoMotions 打包好的动作库 (.pt) 拆回单段 .motion 文件, 供上游的骨架转换脚本使用.

上游 data/scripts/convert_soma23_motion_to_smpl.py 只吃 .motion 目录, 而可选 A 的 BONES-SEED 小样本
是打包好的 .pt. 另外该脚本还按旧字段名读 gts/grs, 新版 .motion 已改名为 rigid_body_pos/rot,
所以拆出的文件同时带两套名字 (同一份张量). 本脚本只做切片和改名, 不改数据.
用 runtime/protomotions/.venv 的 Python 运行:
  runtime/protomotions/.venv/Scripts/python.exe tools/protomotions_motions.py unpack \\
      third_party/protomotions/data/motion_for_trackers/soma23_bones_seed_mini.pt runtime/protomotions/motions/soma
  runtime/protomotions/.venv/Scripts/python.exe tools/protomotions_motions.py legacy \\
      third_party/protomotions/data/soma-kimodo-generated/proto/output.motion runtime/protomotions/motions/soma/kimodo_output.motion
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PM = ROOT / "third_party" / "protomotions"

# 打包字段 -> .motion 字段 (见上游 motion_lib.MotionLib 与 RobotState.to_dict)
FIELDS = {"dps": "dof_pos", "dvs": "dof_vel", "gts": "rigid_body_pos", "grs": "rigid_body_rot",
          "gvs": "rigid_body_vel", "gavs": "rigid_body_ang_vel", "contacts": "rigid_body_contacts",
          "lrs": "local_rigid_body_rot"}
LEGACY = {"gts": "rigid_body_pos", "grs": "rigid_body_rot"}  # 转换脚本读的旧名


def with_legacy_keys(motion: dict) -> dict:
    for old, new in LEGACY.items():
        if new in motion:
            motion[old] = motion[new]
    return motion


def legacy(src: Path, dst: Path) -> None:
    sys.path.insert(0, str(PM))
    import torch

    dst.parent.mkdir(parents=True, exist_ok=True)
    torch.save(with_legacy_keys(torch.load(src, map_location="cpu", weights_only=False)), dst)
    print(f"{src.name} -> {dst}")


def modernize(src: Path, out: Path) -> None:
    """转换脚本输出的 .motion 也是旧字段名, 而当前 MotionLib 按新字段名加载, 这里改回新名."""
    sys.path.insert(0, str(PM))
    import torch
    from protomotions.simulator.base_simulator.simulator_state import StateConversion

    out.mkdir(parents=True, exist_ok=True)
    files = sorted(src.glob("*.motion")) if src.is_dir() else [src]
    for f in files:
        old = torch.load(f, map_location="cpu", weights_only=False)
        motion = {"state_conversion": StateConversion.COMMON, "fps": int(old.get("fps", 30))}
        for packed, field in FIELDS.items():
            if packed in old:
                motion[field] = old[packed]
            elif field in old:
                motion[field] = old[field]
        torch.save(motion, out / f.name)
    print(f"modernized {len(files)} motions -> {out}")


def unpack(src: Path, out: Path, only: list[str]) -> None:
    sys.path.insert(0, str(PM))
    import torch
    from protomotions.simulator.base_simulator.simulator_state import StateConversion

    lib = torch.load(src, map_location="cpu", weights_only=False)
    out.mkdir(parents=True, exist_ok=True)
    names = [os.path.basename(f.replace("\\", "/")) for f in lib["motion_files"]]
    count = 0
    for i, name in enumerate(names):
        if only and not any(k in name for k in only):
            continue
        start, frames = int(lib["length_starts"][i]), int(lib["motion_num_frames"][i])
        motion = {"state_conversion": StateConversion.COMMON,
                  "fps": int(round(1.0 / float(lib["motion_dt"][i])))}
        for packed, field in FIELDS.items():
            if lib.get(packed) is not None:
                motion[field] = lib[packed][start:start + frames].clone()
        torch.save(with_legacy_keys(motion), out / name)
        count += 1
    print(f"unpacked {count}/{len(names)} motions -> {out}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("unpack")
    u.add_argument("src", type=Path)
    u.add_argument("out", type=Path)
    u.add_argument("--only", nargs="*", default=[], help="只拆文件名包含这些片段的动作")
    g = sub.add_parser("legacy", help="给单个 .motion 补上旧字段名")
    g.add_argument("src", type=Path)
    g.add_argument("dst", type=Path)
    m = sub.add_parser("modernize", help="把转换脚本输出的旧字段名改回新名")
    m.add_argument("src", type=Path)
    m.add_argument("out", type=Path)
    a = p.parse_args()
    if a.cmd == "unpack":
        unpack(a.src.resolve(), a.out.resolve(), a.only)
    elif a.cmd == "modernize":
        modernize(a.src.resolve(), a.out.resolve())
    else:
        legacy(a.src.resolve(), a.dst.resolve())


if __name__ == "__main__":
    main()

"""把「抹茶樱花」小筑居导出给桌宠的小屋窗口用 (不下载任何东西, 用本机的 Blender):

    python desktop/scripts/prepare_room.py [--room-dir E:/MyMakeModels] [--blender <blender.exe>]

导出到不进版本库的 desktop/third_party/rooms/petal-oak/room.glb (宿主按 /pet/rooms/... 提供).
导出用房间自带的 scripts/export_game.py (见 export_room.py): 合并网格、材质烘成纯色, 去掉前墙、右墙、天花板 (娃娃屋视角).
不改房间目录里的任何文件, 也不保存 .blend. 房间改了以后再跑一遍就行.
房间里的家具位置、她能去哪、坐哪在 desktop/web/room-scenes/petal-oak.json (手写的, 取自房间的碰撞盒和俯视图).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "third_party" / "rooms" / "petal-oak"
BLENDERS = [r"E:\STEAM\steam\steamapps\common\Blender\blender.exe", r"C:\Program Files\Blender Foundation\Blender\blender.exe"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--room-dir", default=r"E:\MyMakeModels")
    ap.add_argument("--blender")
    a = ap.parse_args()
    room = Path(a.room_dir)
    blend = room / "Petal_Oak_30sqm.blend"
    blender = a.blender or next((b for b in BLENDERS if Path(b).exists()), None) or shutil.which("blender")
    if not blend.exists():
        print(f"找不到房间文件 {blend}")
        return 1
    if not blender:
        print("找不到 Blender: 用 --blender 指定 blender.exe")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "room.glb"
    cmd = [blender, "-b", str(blend), "--python", str(HERE / "export_room.py"), "--", "--room-dir", str(room), "--output", str(out)]
    print("导出中 (约半分钟)…")
    with open(OUT / "export.log", "w", encoding="utf-8", errors="replace") as log:
        code = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
    if code or not out.exists():
        print(f"导出失败 (退出码 {code}), 见 {OUT / 'export.log'}")
        return 1
    print(f"好了: {out} ({out.stat().st_size / 1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

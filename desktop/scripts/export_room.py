"""在 Blender 里把「抹茶樱花」小筑居导出成桌宠小屋窗口用的 GLB (由 prepare_room.py 调用, 也可以单独跑):

    blender -b <房间.blend> --python desktop/scripts/export_room.py -- --room-dir E:/MyMakeModels --output <输出.glb>

借房间自带的 scripts/export_game.py 导出 (合并网格、程序材质烘成不带贴图的 PBR、保留可互动的节点):
导出前只在内存里:
  - 把"可选的前墙、右墙、天花板"标成不导出 (小屋从正面斜上方看进去, 娃娃屋视角);
  - 给能挪的家具各建一个空的父节点 (MV_ 开头, 放在家具脚印的中心、地面上), 零件挂上去并标成保留:
    导出脚本按材质合并网格时会把它们烘在这个节点下面, 导出后整件家具是一个能挪动的节点 (她会自己挪家具, 见 web/room-life.js).
不保存 .blend, 不改房间目录里的任何文件.
"""
import argparse
import importlib.util
import sys
from pathlib import Path

import bpy
from mathutils import Vector

# 能挪的家具: 节点名 -> 零件 (所在集合, 或名字开头). 补水推车上放着水壶等物件, 挪了会悬空, 先不挪
MOVABLE = {
    'MV_desk_chair': {'collection': '03 • Pink ergonomic chair'},
    'MV_reading_chair': {'prefixes': ('Reading chair', 'Lounge oak foot', 'Lounge rose cushion')},
    'MV_reading_table': {'prefixes': ('Reading side table',)},
    'MV_ficus': {'prefixes': ('Ficus beside window',)},
}


def group_movable():
    made = {}
    for name, spec in MOVABLE.items():
        parts = []
        for ob in bpy.data.objects:
            if ob.parent is not None or ob.type not in {'MESH', 'CURVE'}:
                continue  # 挂在别的节点下面的 (可拿的物件) 不动
            if 'collection' in spec and any(c.name == spec['collection'] for c in ob.users_collection):
                parts.append(ob)
            elif ob.name.startswith(spec.get('prefixes', ())):
                parts.append(ob)
        if not parts:
            print(f'[ROOM EXPORT] {name}: 没找到零件', flush=True)
            continue
        lo, hi = Vector((1e9, 1e9, 1e9)), Vector((-1e9, -1e9, -1e9))
        for ob in parts:
            for corner in ob.bound_box:
                p = ob.matrix_world @ Vector(corner)
                lo = Vector(map(min, lo, p))
                hi = Vector(map(max, hi, p))
        root = bpy.data.objects.new(name, None)
        bpy.context.scene.collection.objects.link(root)
        root.location = ((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, 0)
        root['game_keep'] = True
        bpy.context.view_layer.update()
        for ob in parts:
            mw = ob.matrix_world.copy()
            ob.parent = root
            ob.matrix_world = mw
        made[name] = len(parts)
    print(f'[ROOM EXPORT] 能挪的家具: {made}', flush=True)


def main():
    args = argparse.ArgumentParser()
    args.add_argument('--room-dir', type=Path, required=True)
    args.add_argument('--output', type=Path, required=True)
    a = args.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
    spec = importlib.util.spec_from_file_location('export_game', a.room_dir / 'scripts' / 'export_game.py')
    game = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(game)
    cut = 0
    for ob in bpy.data.objects:
        if game.is_optional_envelope(ob):
            ob['game_exclude'] = True  # 只改内存, 不保存
            cut += 1
    print(f'[ROOM EXPORT] 不导出前墙、右墙、天花板: {cut} 个物体', flush=True)
    group_movable()
    game.export_room(a.output.resolve())


if __name__ == '__main__':
    main()

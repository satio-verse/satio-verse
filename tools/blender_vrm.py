"""在 Blender 里跑 (由 tools/convert_vrm.py 调用): 载入 PMX 或 .blend, 列出结构 (inspect) 或转成 VRM 1.0 (convert).

用到 mmd_tools (导入 PMX、读 MMD 刚体) 和 VRM Add-on for Blender (人形骨骼、表情、弹簧骨、导出).
参数是一行 JSON: {"cmd", "name", "src", "out", "report", "meta"}.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import addon_utils
import bpy

MMD, VRM = "bl_ext.user_default.mmd_tools", "bl_ext.user_default.vrm"


def log(*args) -> None:
    print("[vrm]", *args, flush=True)


def load(src: Path) -> None:
    if src.suffix.lower() == ".blend":
        bpy.ops.wm.open_mainfile(filepath=str(src), load_ui=False)
    else:
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj)
    for mod in (MMD, VRM):
        addon_utils.enable(mod, default_set=True)
    if src.suffix.lower() == ".pmx":
        bpy.ops.mmd_tools.import_model(filepath=str(src), scale=0.08, rename_bones=False, clean_model=True,
                                       types={"MESH", "ARMATURE", "PHYSICS", "MORPHS", "DISPLAY"}, log_level="WARNING")


def armature() -> bpy.types.Object:
    arms = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    if not arms:
        raise SystemExit("没有骨架")
    return max(arms, key=lambda o: len(o.data.bones))


def skinned_meshes(arm) -> list:
    return [o for o in bpy.data.objects if o.type == "MESH" and any(
        m.type == "ARMATURE" and m.object == arm for m in o.modifiers)]


def weights(meshes) -> Counter:
    """每根骨骼蒙了多少个顶点 (权重 > 0.01)."""
    count = Counter()
    for o in meshes:
        names = {g.index: g.name for g in o.vertex_groups}
        for v in o.data.vertices:
            for g in v.groups:
                if g.weight > 0.01 and g.group in names:
                    count[names[g.group]] += 1
    return count


def inspect(job: dict) -> dict:
    arm = armature()
    meshes = skinned_meshes(arm)
    w = weights(meshes)
    mw = arm.matrix_world
    bones = [{"name": b.name, "parent": b.parent.name if b.parent else None, "deform": b.use_deform,
              "head": [round(x, 3) for x in mw @ b.head_local], "verts": w.get(b.name, 0)} for b in arm.data.bones]
    rep = {"armature": arm.name, "bones": bones, "meshes": [], "objects": Counter(o.type for o in bpy.data.objects)}
    for o in meshes:
        keys = [k.name for k in o.data.shape_keys.key_blocks] if o.data.shape_keys else []
        mats = []
        for m in o.data.materials:
            if m is None:
                continue
            imgs = [n.image.name for n in (m.node_tree.nodes if m.node_tree else []) if n.type == "TEX_IMAGE" and n.image]
            mats.append({"name": m.name, "images": imgs, "blend": getattr(m, "blend_method", None)})
        rep["meshes"].append({"name": o.name, "verts": len(o.data.vertices), "shape_keys": keys, "materials": mats})
    root = next((o for o in bpy.data.objects if getattr(o, "mmd_type", "") == "ROOT"), None)
    if root is not None:
        mr = root.mmd_root
        rep["mmd_morphs"] = {k: [m.name for m in getattr(mr, k)] for k in
                             ("vertex_morphs", "bone_morphs", "group_morphs", "material_morphs", "uv_morphs")}
        rigid = [o for o in bpy.data.objects if getattr(o, "mmd_type", "") == "RIGID_BODY"]
        rep["rigid"] = Counter(o.mmd_rigid.type for o in rigid)
        rep["joints"] = sum(1 for o in bpy.data.objects if getattr(o, "mmd_type", "") == "JOINT")
    rep["objects"] = dict(rep["objects"])
    return rep


# ---------------------------------------------------------------- 转换

# MMD 的胯骨: 要同时是上半身和下半身的祖先, 越靠近骨盆越好 (センター 一般在膝盖附近)
HIPS = ("腰", "グルーブ", "センター")
# 腿: MMD 模型的蒙皮可能在 足D 链上 (足 链只是 IK 用的控制骨), 也可能就在 足 链上, 看哪条链有权重
LEG = (("upper_leg", "足"), ("lower_leg", "ひざ"), ("foot", "足首"))
SIDE = {"left": "左", "right": "右"}
# VRM 表情 -> 候选形态键组合 (按顺序取第一个全都存在的). PMX 用 MMD 的标准名, .blend 用作者自己起的中文名.
EXPR = {
    "aa": [{"あ": 1.0}, {"微口": 1.0}], "ih": [{"い": 1.0}, {"微口": 0.5}], "ou": [{"う": 1.0}, {"微口": 0.6}],
    "ee": [{"え": 1.0}, {"微口": 0.5}], "oh": [{"お": 1.0}, {"微口": 0.8}],
    "blink": [{"まばたき": 1.0}, {"闭眼": 1.0}],
    "blinkLeft": [{"まばたき左": 1.0}, {"ウィンク": 1.0}, {"ウィンク２": 1.0}, {"ｳｨﾝｸ２": 1.0}],
    "blinkRight": [{"まばたき右": 1.0}, {"ウィンク右": 1.0}, {"ウィンク２右": 1.0}, {"ｳｨﾝｸ２右": 1.0}],
    "happy": [{"笑い": 1.0}, {"にこり": 1.0}, {"微笑": 1.0}],
    "angry": [{"怒り": 1.0}, {"怒り目": 1.0}],
    "sad": [{"困る": 1.0}, {"悲し目": 1.0}],
    "relaxed": [{"なごみ": 1.0}, {"優しい": 1.0}, {"微笑": 0.6}],
    "surprised": [{"びっくり": 1.0}],
    # 不映射 lookUp/lookDown: MMD 的 "上""下" 是眉毛形态键, 不是视线 (插件自带的表把它们当成了视线)
}
REQUIRED = ("hips", "spine", "head", "left_upper_arm", "left_lower_arm", "left_hand", "right_upper_arm",
            "right_lower_arm", "right_hand", "left_upper_leg", "left_lower_leg", "left_foot", "right_upper_leg",
            "right_lower_leg", "right_foot")


def select_only(objs, active) -> None:
    bpy.ops.object.mode_set(mode="OBJECT") if bpy.context.object and bpy.context.object.mode != "OBJECT" else None
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = active


def isolate_blend(arm, rep) -> None:
    """.blend 场景: 只留角色 (骨架和蒙在它上面的网格), 清掉姿势, 摆正到原点, 缩放烘进去."""
    keep = {arm, *skinned_meshes(arm)}
    dropped = Counter(o.type for o in bpy.data.objects if o not in keep)
    for o in list(bpy.data.objects):
        if o not in keep:
            bpy.data.objects.remove(o, do_unlink=True)
    for o in keep:
        if o.name not in bpy.context.view_layer.objects:
            bpy.context.scene.collection.objects.link(o)
        o.hide_set(False)
        o.hide_viewport = o.hide_render = False
    for pb in arm.pose.bones:
        pb.location, pb.rotation_quaternion, pb.rotation_euler, pb.scale = (0, 0, 0), (1, 0, 0, 0), (0, 0, 0), (1, 1, 1)
    rep["isolate"] = {"dropped": dict(dropped), "transform": [list(arm.location), list(arm.rotation_euler), list(arm.scale)]}
    arm.location, arm.rotation_euler = (0, 0, 0), (0, 0, 0)
    select_only(keep, arm)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    # 实体化做的描边外壳 (MToon 自带描边)、动态绘画、指向已删对象的晶格, 在 VRM 里都没有意义
    removed = Counter()
    for o in keep:
        for mod in list(getattr(o, "modifiers", [])):
            if mod.type in ("SOLIDIFY", "DYNAMIC_PAINT") or (mod.type == "LATTICE" and mod.object is None):
                removed[mod.type] += 1
                o.modifiers.remove(mod)
    rep["isolate"]["removed_modifiers"] = dict(removed)


def relink_images(src: Path, rep) -> None:
    """.blend 里的贴图路径常常指向作者自己电脑 (D:\\...): 按文件名在模型目录里找同名文件重新关联, 找不到的记下来."""
    import os
    files = {}
    for f in src.parent.rglob("*"):
        if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff"):
            files.setdefault(f.name.lower(), f)
    fixed, missing = [], []
    for img in bpy.data.images:
        if img.source != "FILE" or img.packed_file or os.path.exists(bpy.path.abspath(img.filepath)):
            continue
        hit = files.get(Path(bpy.path.abspath(img.filepath).replace("\\", "/")).name.lower())
        if hit is not None:
            img.filepath = str(hit)
            img.reload()
            fixed.append(img.name)
        else:
            missing.append(img.name)
    rep["images"] = {"relinked": fixed, "missing": missing}


def face_forward(arm, rep) -> None:
    """VRM 导出把 Blender 的 -Y 当成正面. 眼睛在头骨前面、脚尖在脚踝前面才算朝前; 反了就绕 Z 转 180 度烘进去."""
    from math import pi
    hb = arm.data.vrm_addon_extension.vrm1.humanoid.human_bones
    pos = lambda n: arm.matrix_world @ arm.data.bones[getattr(hb, n).node.bone_name].head_local  # noqa: E731
    ahead = []
    for a, b in (("left_eye", "head"), ("right_eye", "head"), ("left_toes", "left_foot"), ("right_toes", "right_foot")):
        if getattr(hb, a).node.bone_name and getattr(hb, b).node.bone_name:
            ahead.append(-(pos(a).y - pos(b).y))  # 往 -Y 为正
    rep["facing"] = round(sum(ahead), 3)
    if ahead and sum(ahead) < 0:
        meshes = skinned_meshes(arm)
        arm.rotation_euler = (0, 0, pi)
        select_only([arm, *meshes], arm)
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        rep["facing_fixed"] = True


def attach_orphans(arm, w: Counter, rep) -> None:
    """有权重却挂在人形骨骼树外面的骨骼链 (比如直接挂在 root 上的头发): 按高度改挂到头/胸/胯上,
    否则转头时头发留在原地. 原文件里多半靠约束或者只摆了一个静态姿势, 约束不进 VRM."""
    hb = arm.data.vrm_addon_extension.vrm1.humanoid.human_bones
    hips, chest, neck, head = (getattr(hb, n).node.bone_name for n in ("hips", "chest", "neck", "head"))
    bones = arm.data.bones
    tree = {hips} | {b.name for b in bones[hips].children_recursive}
    z = lambda name: (arm.matrix_world @ bones[name].head_local).z  # noqa: E731
    moves = {}
    for b in bones:
        if b.name in tree or not w.get(b.name) or (b.parent and b.parent.name in tree):
            continue
        if b.parent is not None and w.get(b.parent.name):
            continue  # 链中间的骨骼跟着链头走
        target = head if z(b.name) >= z(neck or head) else chest if chest and z(b.name) >= z(chest) else hips
        moves[b.name] = target
    if moves:
        bpy.ops.object.mode_set(mode="EDIT")
        eb = arm.data.edit_bones
        for name, target in moves.items():
            eb[name].use_connect = False
            eb[name].parent = eb[target]
        bpy.ops.object.mode_set(mode="OBJECT")
    rep["attached_orphans"] = moves


# ---------------------------------------------------------------- 物理 (弹簧骨)

# 各类骨骼链的参数: 刚度从根部到末梢线性变软; 阻尼; 重力 (向下); 关节碰撞半径 (米).
# MMD 刚体直接换算出来的是同一套默认值 (刚度 1、阻尼 0.5、重力 0、半径 0): 头发裙摆不下垂, 一碰就穿过身体.
SPRING = {
    "hair": {"stiff": (1.1, 0.55), "drag": 0.45, "grav": 0.06, "radius": 0.015},
    "skirt": {"stiff": (1.6, 0.8), "drag": 0.55, "grav": 0.12, "radius": 0.025},
    "cloth": {"stiff": (1.3, 0.7), "drag": 0.5, "grav": 0.08, "radius": 0.02},   # 尾巴、飘带、袖口
    "soft": {"stiff": (4.0, 3.0), "drag": 0.9, "grav": 0.0, "radius": 0.0},     # 胸、臀: 只轻微晃
}
SOFT = re.compile(r"胸|乳|お尻|尻|breast|bust|butt|oppai", re.I)
TAIL = re.compile(r"しっぽ|尻尾|tail|尾|ribbon|リボン|tie", re.I)
SKIRT = re.compile(r"スカート|skirt|裙|dress", re.I)
# 身体碰撞体: 两端都是人形骨骼的胶囊 (第二个是终点), 半径从网格量
CAPSULES = [("left_upper_leg", "left_lower_leg"), ("left_lower_leg", "left_foot"),
            ("right_upper_leg", "right_lower_leg"), ("right_lower_leg", "right_foot"),
            ("left_upper_arm", "left_lower_arm"), ("left_lower_arm", "left_hand"),
            ("right_upper_arm", "right_lower_arm"), ("right_lower_arm", "right_hand"), ("spine", "neck")]


def humanoid_names(arm) -> dict:
    hb = arm.data.vrm_addon_extension.vrm1.humanoid.human_bones
    return {n: getattr(hb, n).node.bone_name for n in dir(hb)
            if hasattr(getattr(hb, n, None), "node") and getattr(hb, n).node.bone_name}


def dominant(meshes, bones):
    """每个顶点的世界坐标和权重最大的骨骼名 (只看骨骼的顶点组; mmd_tools 另有一个全是 1 的 mmd_edge_scale)."""
    import numpy as np
    pts, names = [], []
    for o in meshes:
        groups = {g.index: g.name for g in o.vertex_groups if g.name in bones}
        mw = o.matrix_world
        for v in o.data.vertices:
            best = max((g for g in v.groups if g.group in groups), key=lambda g: g.weight, default=None)
            if best is None or best.group not in groups:
                continue
            pts.append(tuple(mw @ v.co))
            names.append(groups[best.group])
    return np.asarray(pts, dtype=np.float64), np.asarray(names)


def body_colliders(arm, meshes, rep) -> list:
    """从网格量出身体碰撞体 (四肢和躯干胶囊、头部球), 一根骨骼一组. 返回碰撞体记录 (几何和组 uuid).
    只用这段肢体自己的顶点 (骨骼本身、通往下一根人形骨骼的中间骨骼、捩り骨), 不算挂在下面的饰品和头发;
    四肢半径 = 顶点到骨段距离的中位数 x 0.85, 躯干取 30% 分位 (截面是扁的), 都落在皮肤以内, 不把衣服撑开."""
    import numpy as np
    from mathutils import Vector
    sb = arm.data.vrm_addon_extension.spring_bone1
    hum = humanoid_names(arm)
    bones = arm.data.bones
    pts, names = dominant(meshes, bones)

    def owned(a, b):
        out = {a}
        x = bones[b].parent if b else None
        while x is not None and x.name != a:  # a 到 b 之间的中间骨骼 (比如 腕 -> 腕捩 -> ひじ 的 腕捩)
            out.add(x.name)
            x = x.parent
        if x is None:  # b 不在 a 下面: 只要 a
            out = {a}
        out |= {c.name for c in bones[a].children_recursive if "捩" in c.name or "twist" in c.name.lower()}
        return out

    def world(name):
        return np.array(arm.matrix_world @ bones[name].head_local)

    def local(bone, point):
        return (arm.matrix_world @ arm.pose.bones[bone].matrix).inverted() @ Vector(tuple(point))

    cols = []
    for a, b in CAPSULES:
        if a not in hum or b not in hum:
            continue
        A, B = world(hum[a]), world(hum[b])
        own = owned(hum[a], hum[b])
        torso = a == "spine"
        if torso:
            own |= {hum[k] for k in ("chest", "upper_chest") if k in hum}
        sel = pts[np.isin(names, list(own))]
        if len(sel) < 20:
            continue
        d = B - A
        t = np.clip(((sel - A) @ d) / max(float(d @ d), 1e-9), 0, 1)
        dist = np.linalg.norm(sel - (A + t[:, None] * d), axis=1)
        r = float(np.percentile(dist, 30) * 0.9 if torso else np.median(dist) * 0.85)
        col = sb.add_collider(bpy.context, arm)
        col.node.bone_name = hum[a]
        col.shape_type = col.SHAPE_TYPE_CAPSULE.identifier
        col.shape.capsule.offset = local(hum[a], A)
        col.shape.capsule.tail = local(hum[a], B)
        col.shape.capsule.radius = r
        cols.append({"name": a, "bone": hum[a], "A": A, "B": B, "r": r, "col": col})
    if "head" in hum:
        sel = pts[names == hum["head"]]  # 只用头骨本身的顶点 (脸和头皮), 不含头发骨骼
        if len(sel) >= 20:
            c = np.median(sel, axis=0)
            r = float(np.median(np.linalg.norm(sel - c, axis=1))) * 0.85
            col = sb.add_collider(bpy.context, arm)
            col.node.bone_name = hum["head"]
            col.shape_type = col.SHAPE_TYPE_SPHERE.identifier
            col.shape.sphere.offset = local(hum["head"], c)
            col.shape.sphere.radius = r
            cols.append({"name": "head", "bone": hum["head"], "A": c, "B": c, "r": r, "col": col})
    for rec in cols:
        g = sb.add_collider_group()
        g.vrm_name = rec["bone"] + "-body"
        g.add_collider().collider_uuid = rec["col"].uuid
        rec["group"] = g.uuid
    return cols


def mmd_chains(arm) -> list:
    """MMD 动态刚体所在的骨骼链 (根 = 父骨骼不是动态刚体的那根; 沿第一个动态子骨骼往下, 其余分支另起一条)."""
    dyn = []
    for o in bpy.data.objects:
        if getattr(o, "mmd_type", "") != "RIGID_BODY" or getattr(o.mmd_rigid, "type", "0") not in ("1", "2"):
            continue
        c = o.constraints.get("mmd_tools_rigid_parent")
        if c is not None and c.target == arm and c.subtarget in arm.data.bones and c.subtarget not in dyn:
            dyn.append(c.subtarget)
    dset, bones = set(dyn), arm.data.bones
    starts = [bones[n] for n in dyn if bones[n].parent is None or bones[n].parent.name not in dset]
    chains, used = [], set()
    while starts:
        b = starts.pop(0)
        if b.name in used:
            continue
        chain = [b.name]
        kids = [c for c in b.children if c.name in dset]
        while kids:
            starts.extend(kids[1:])
            b = kids[0]
            chain.append(b.name)
            kids = [c for c in b.children if c.name in dset]
        if b.children:  # 末端再带一根子骨骼当尾巴, 最后一节才有长度
            chain.append(b.children[0].name)
        used.update(chain)
        chains.append(chain)
    return chains


def head_chains(arm, w: Counter) -> list:
    """没有 MMD 物理时: 头上有权重的非人形骨骼链 (头发)."""
    hum = set(humanoid_names(arm).values())
    head = arm.data.vrm_addon_extension.vrm1.humanoid.human_bones.head.node.bone_name
    chains = []
    for root in arm.data.bones[head].children:
        if root.name in hum or not w.get(root.name):
            continue
        chain, b = [root.name], root
        while True:
            kids = [c for c in b.children if w.get(c.name)]
            if not kids:
                break
            b = kids[0]
            chain.append(b.name)
        chains.append(chain)
    return chains


def _seg_dist(p, a, b) -> float:
    import numpy as np
    d = b - a
    t = float(np.clip((p - a) @ d / max(float(d @ d), 1e-12), 0, 1))
    return float(np.linalg.norm(p - (a + t * d)))


def build_springs(arm, chains: list, cols: list, rep) -> None:
    """按类别给每条链设参数, 连上身体碰撞体, 中心节点设为胯骨 (走路转身时不被甩开).
    静止姿态下关节本来就在某个碰撞体里的, 不连那个碰撞体 (否则每帧都被往外推, 站着不动也一直抖);
    头部球缩到所有头发链的前两节都在球外, 头发仍然挡在头外面."""
    import numpy as np
    hum = humanoid_names(arm)
    human_set = set(hum.values())
    head_tree = {hum.get("head"), hum.get("neck")}
    legs = {hum.get(k) for k in ("hips", "left_upper_leg", "right_upper_leg", "left_lower_leg", "right_lower_leg")}
    hip_z = (arm.matrix_world @ arm.data.bones[hum["hips"]].head_local).z
    pos = {n: np.array(arm.matrix_world @ arm.data.bones[n].head_local) for c in chains for n in c}
    sb = arm.data.vrm_addon_extension.spring_bone1
    plan = []
    for chain in chains:
        if len(chain) < 2:
            continue
        root = arm.data.bones[chain[0]]
        anchor = root.parent
        while anchor is not None and anchor.name not in human_set:
            anchor = anchor.parent
        anchor = anchor.name if anchor is not None else None
        z = pos[chain[0]][2]
        if SOFT.search(chain[0]):
            kind = "soft"
        elif TAIL.search(chain[0]):
            kind = "cloth"
        elif anchor in head_tree:
            kind = "hair"
        elif anchor in legs and (z < hip_z + 0.08 or SKIRT.search(chain[0])):
            kind = "skirt"
        else:
            kind = "cloth"
        plan.append((chain, kind))
    head = next((c for c in cols if c["name"] == "head"), None)
    if head is not None:  # 头部球缩到发根外
        near = [_seg_dist(pos[n], head["A"], head["B"]) - SPRING["hair"]["radius"]
                for chain, kind in plan if kind == "hair" for n in chain[:2]]
        if near:
            head["r"] = max(0.03, min(head["r"], 0.92 * min(near)))
            head["col"].shape.sphere.radius = head["r"]
    kinds, cut = Counter(), 0
    for chain, kind in plan:
        kinds[kind] += 1
        p = SPRING[kind]
        spring = sb.add_spring()
        spring.vrm_name = f"{kind}-{chain[0]}"
        spring.center.bone_name = hum["hips"]
        n = len(chain)
        for i, name in enumerate(chain):
            j = spring.add_joint()
            j.node.bone_name = name
            t = i / max(1, n - 1)
            j.stiffness = p["stiff"][0] + (p["stiff"][1] - p["stiff"][0]) * t
            j.drag_force, j.gravity_power, j.hit_radius = p["drag"], p["grav"], p["radius"]
        if kind == "soft":
            continue
        for c in cols:
            if any(_seg_dist(pos[nm], c["A"], c["B"]) < c["r"] + p["radius"] for nm in chain):
                cut += 1
                continue
            spring.add_collider_group().collider_group_uuid = c["group"]
    rep["springs"] = dict(kinds)
    rep["body_colliders"] = [(c["name"], round(c["r"], 3)) for c in cols]
    rep["rest_overlaps_skipped"] = cut


def drop_materials(meshes, names: list, rep) -> None:
    """删掉指定材质的面 (比如 Wendy 的袖子; 原模型自带"隐藏袖子"的材质形态, 下面有完整的手臂)."""
    import bmesh
    dropped = 0
    for o in meshes:
        idx = {i for i, m in enumerate(o.data.materials) if m is not None and m.name in names}
        if not idx:
            continue
        bm = bmesh.new()
        bm.from_mesh(o.data)
        faces = [f for f in bm.faces if f.material_index in idx]
        dropped += len(faces)
        bmesh.ops.delete(bm, geom=faces, context="FACES")
        bm.to_mesh(o.data)
        bm.free()
        for i in sorted(idx, reverse=True):
            o.data.materials.pop(index=i)
    rep["dropped_faces"] = dropped


def clean_mmd_rig(arm, rep) -> None:
    """MMD 的附加变换 (影子骨骼) 和 IK 等骨骼约束不进 VRM, 还会在摆 T 姿态时把腿拉弯, 全部去掉."""
    root = next((o for o in bpy.data.objects if getattr(o, "mmd_type", "") == "ROOT"), None)
    if root is not None:
        select_only([root], root)
        bpy.ops.mmd_tools.clean_additional_transform()
    n = 0
    for pb in arm.pose.bones:
        for c in list(pb.constraints):
            pb.constraints.remove(c)
            n += 1
    rep["removed_bone_constraints"] = n


def assign_humanoid(arm, w: Counter, rep) -> None:
    select_only([arm], arm)
    bpy.ops.vrm.assign_vrm1_humanoid_human_bones_automatically(armature_object_name=arm.name)
    hb = arm.data.vrm_addon_extension.vrm1.humanoid.human_bones
    bones = arm.data.bones
    fixes = []

    def ancestors(name):
        b = bones.get(name)
        out = set()
        while b is not None and b.parent is not None:
            b = b.parent
            out.add(b.name)
        return out

    if "上半身" in bones and "下半身" in bones:
        both = ancestors("上半身") & ancestors("下半身")
        hips = next((h for h in HIPS if h in both), None)
        if hips and hb.hips.node.bone_name != hips:
            fixes.append(f"hips {hb.hips.node.bone_name} -> {hips}")
            hb.hips.node.bone_name = hips
    reparent = {}
    for side, jp in SIDE.items():
        chain_n = [jp + b for _, b in LEG]
        chain_d = [jp + b + "D" for _, b in LEG]
        if not all(b in bones for b in chain_d):
            continue
        if sum(w.get(b, 0) for b in chain_d) <= sum(w.get(b, 0) for b in chain_n):
            continue
        for (vrm, _), name in zip(LEG, chain_d):
            getattr(hb, f"{side}_{vrm}").node.bone_name = name
        fixes.append(f"{side} leg -> {chain_d}")
        # 挂在 IK 控制链上的有权重的骨骼 (比如 つま先) 改挂到对应的 D 骨骼上, 否则转脚踝时它不跟着动
        for n_name, d_name in zip(chain_n, chain_d):
            for child in bones[n_name].children:
                if child.name not in chain_n and child.use_deform and w.get(child.name, 0) > 0:
                    reparent[child.name] = d_name
        toes = jp + "足先EX" if w.get(jp + "足先EX", 0) > 0 else jp + "つま先" if w.get(jp + "つま先", 0) > 0 else None
        getattr(hb, f"{side}_toes").node.bone_name = toes or ""
    if reparent:
        bpy.ops.object.mode_set(mode="EDIT")
        eb = arm.data.edit_bones
        for child, parent in reparent.items():
            eb[child].use_connect = False
            eb[child].parent = eb[parent]
        bpy.ops.object.mode_set(mode="OBJECT")
        fixes.append(f"reparent {reparent}")
    mapping = {name: getattr(hb, name).node.bone_name for name in dir(hb)
               if hasattr(getattr(hb, name, None), "node") and getattr(hb, name).node.bone_name}
    missing = [b for b in REQUIRED if not mapping.get(b)]
    rep["humanoid"] = {"fixes": fixes, "mapping": mapping, "missing": missing}
    if missing:
        raise SystemExit(f"人形骨骼缺少: {missing}")


def eye_look_at(arm, rep) -> None:
    """有两根眼骨时视线用骨骼驱动 (和 VRoid 模型一样), 原点放在两眼中点; 插件默认的表情驱动在这里没有可用的表情."""
    hb = arm.data.vrm_addon_extension.vrm1.humanoid.human_bones
    names = [hb.left_eye.node.bone_name, hb.right_eye.node.bone_name, hb.head.node.bone_name]
    if not all(names):
        return
    pos = [arm.matrix_world @ arm.data.bones[n].head_local for n in names]
    la = arm.data.vrm_addon_extension.vrm1.look_at
    la.type = "bone"
    la.offset_from_head_bone = (pos[0] + pos[1]) / 2 - pos[2]
    rep["look_at"] = {"type": "bone", "offset": [round(x, 3) for x in la.offset_from_head_bone]}


def remove_mmd_objects(arm, rep) -> None:
    """刚体、关节和 MMD 根节点都不导出 (刚体已经换成弹簧骨)."""
    if arm.parent is not None:
        mw = arm.matrix_world.copy()
        arm.parent = None
        arm.matrix_world = mw
    gone = Counter()
    for o in list(bpy.data.objects):
        t = getattr(o, "mmd_type", "NONE")
        if t not in ("NONE", "") and o is not arm:
            gone[t] += 1
            bpy.data.objects.remove(o, do_unlink=True)
    rep["removed_mmd_objects"] = dict(gone)


def bind_expressions(arm, meshes, rep) -> None:
    exprs = arm.data.vrm_addon_extension.vrm1.expressions
    keys = {o.name: {k.name for k in o.data.shape_keys.key_blocks} for o in meshes if o.data.shape_keys}
    bound, used = {}, {name: set() for name in keys}
    for preset, expr in exprs.preset.expression_preset_and_expressions():
        for cand in EXPR.get(preset.name, []):
            if not all(any(k in ks for ks in keys.values()) for k in cand):
                continue
            expr.morph_target_binds.clear()
            for key, weight in cand.items():
                for obj, ks in keys.items():
                    if key in ks:
                        b = expr.morph_target_binds.add()
                        b.node.mesh_object_name, b.index, b.weight = obj, key, weight
                        used[obj].add(key)
            bound[preset.name] = cand
            break
    # 没用到的形态键删掉: 浏览器里每个形态键都是整份顶点数据, 几百个会占掉几百 MB 显存
    pruned = 0
    for o in meshes:
        if not o.data.shape_keys:
            continue
        for kb in list(o.data.shape_keys.key_blocks)[1:]:
            if kb.name not in used.get(o.name, ()):
                o.shape_key_remove(kb)
                pruned += 1
    rep["expressions"] = {"bound": bound, "missing": [p for p in EXPR if p not in bound], "pruned_shape_keys": pruned}


def alpha_kind(img) -> str:
    """看贴图的透明通道: 全不透明 -> OPAQUE; 有透明区域 -> MASK (镂空, 不用排序); 透明区域大多是半透明的柔边 -> BLEND.
    MMD 默认按贴图透明度混合, 直接当不透明会让眼睛高光、睫毛、发梢变成实心方块 (初音的虹膜贴图透明处是白色,
    当成不透明时整只眼睛被盖成白的). 柔边占透明区域四分之一以上 (眼睛、高光) 用混合, 边缘不会出锯齿."""
    import numpy as np
    if img is None:
        return "OPAQUE"
    if not img.has_data:  # 后台模式下图片延迟加载, 没读像素前尺寸是 0
        img.reload()
        len(img.pixels)
    if img.channels < 4 or not img.size[0]:
        return "OPAQUE"
    px = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    a = px[3::4]
    clear = np.count_nonzero(a < 0.99)
    if clear < a.size * 0.001:
        return "OPAQUE"
    soft = np.count_nonzero((a > 0.05) & (a < 0.95))
    return "BLEND" if soft > 0.25 * clear else "MASK"


# MToon 参数. MMD 的受光面直接显示贴图本色, 而 MMD 模型的贴图多半很浅 (皮肤约 245,245,230).
# 我们的房间灯光 (不做色调映射) 在受光面的有效强度约 1.3 倍 (实测: 底色 0.45 的白裙渲染成 197/255),
# 底色取 0.72 让受光面约等于贴图本色 (0.72 x 1.3 = 0.94), 不再截成一片白; 阴影 = 底色 x 0.8, 略偏冷;
# 明暗分界偏硬 (动画风格). 不设阴影色的话 MToon 默认是黑色, 背光面会发黑.
BASE = 0.72
SHADE = (BASE * 0.8 * 0.97, BASE * 0.8 * 0.94, BASE * 0.8)
TOONY, SHIFT, GI = 0.8, -0.05, 0.9


def base_image(m):
    """原材质的底色贴图: MMD 材质用 mmd_base_tex (不要误取 toon / sphere 贴图), 其他取接到着色器底色上的那张."""
    nodes = m.node_tree.nodes if m.node_tree else []
    node = nodes.get("mmd_base_tex") if nodes else None
    if node is not None and getattr(node, "image", None):
        return node.image
    for n in nodes:
        if n.type == "BSDF_PRINCIPLED" and n.inputs["Base Color"].is_linked:
            src = n.inputs["Base Color"].links[0].from_node
            if src.type == "TEX_IMAGE" and src.image:
                return src.image
    return next((n.image for n in nodes if n.type == "TEX_IMAGE" and n.image), None)


def usable(img) -> bool:
    """贴图能不能导出: 打包在 .blend 里, 或者文件确实存在 (后台模式下图片延迟加载, has_data 不可靠)."""
    import os
    if img is None:
        return False
    return bool(img.packed_file) or img.source != "FILE" or os.path.exists(bpy.path.abspath(img.filepath))


def to_mtoon(meshes, rep) -> None:
    """全部材质换成 MToon 1.0: 显式启用 (插件在后台模式下转换 MMD 材质时可能没建起 MToon 节点, 导出就会退回成
    金属度为 1 的 PBR, 在房间里几乎全黑), 底色 = BASE x 贴图, 阴影 = SHADE x 贴图; 再按贴图的透明通道定透明方式."""
    from bl_ext.user_default.vrm.editor.mtoon1.property_group import convert_material_to_mtoon1
    out, kinds, queue = [], {}, 0
    ordered = []  # 按 PMX 材质顺序 (MMD 就按这个顺序画: 眼白 -> 虹膜 -> 高光)
    for o in meshes:
        ordered += [m for m in o.data.materials if m is not None and m not in ordered]
    for m in ordered:
        img = base_image(m)
        mmd = getattr(m, "mmd_material", None)
        alpha = float(mmd.alpha) if mmd is not None and m.get("mmd_material") is not None else 1.0
        convert_material_to_mtoon1(m, bpy.context)
        mt = m.vrm_addon_extension.mtoon1
        if not mt.enabled:
            mt.enabled = True
        pbr, mtoon = mt.pbr_metallic_roughness, mt.extensions.vrmc_materials_mtoon
        tex = pbr.base_color_texture.index
        if usable(img):
            tex.source = img
        elif not usable(tex.source):
            tex.source = None
        if tex.source is not None:
            pbr.base_color_factor = (BASE, BASE, BASE, min(alpha, pbr.base_color_factor[3] or 1.0))
            mtoon.shade_multiply_texture.index.source = tex.source
        mtoon.shade_color_factor = SHADE
        mtoon.shading_toony_factor, mtoon.shading_shift_factor, mtoon.gi_equalization_factor = TOONY, SHIFT, GI
        if tex.source is not None:
            kind = kinds.setdefault(tex.source.name, alpha_kind(tex.source))
            if kind != "OPAQUE":
                mt.alpha_mode = kind
                if kind == "MASK":
                    mt.alpha_cutoff = 0.5
                else:
                    # 半透明之间按材质顺序画 (MToon 的渲染队列偏移, 带深度写入时取 0..9)
                    mt.extensions.vrmc_materials_mtoon.transparent_with_z_write = True
                    mt.extensions.vrmc_materials_mtoon.render_queue_offset_number = min(queue, 9)
                    queue += 1
        out.append({"name": m.name, "texture": tex.source.name if tex.source else None, "alpha": mt.alpha_mode,
                    "double_sided": mt.double_sided, "mtoon": mt.enabled, "base": [round(x, 2) for x in pbr.base_color_factor]})
    rep["materials"] = sorted(out, key=lambda x: x["name"])


def set_meta(arm, meta: dict) -> None:
    m = arm.data.vrm_addon_extension.vrm1.meta
    m.vrm_name = meta["title"]
    m.authors.clear()
    m.authors.add().value = meta["author"]
    m.references.clear()
    m.references.add().value = meta["source"]
    m.other_license_url = ""
    m.copyright_information = meta["terms"]
    m.avatar_permission = "onlyAuthor"
    m.commercial_usage = "personalNonProfit"
    m.allow_redistribution = False
    m.modification = "prohibited"


class _Errors(list):
    """给插件校验器用的错误列表 (它要一个能 clear / add 的集合)."""
    def add(self):
        item = type("Error", (), {})()
        self.append(item)
        return item


def validate(arm) -> list:
    from bl_ext.user_default.vrm.editor.validation import WM_OT_vrm_validator
    errs = _Errors()
    WM_OT_vrm_validator.detect_errors(bpy.context, errs, arm.name, execute_migration=True)
    level = {0: "error", 1: "warning", 2: "info"}
    return [f"{level.get(getattr(e, 'severity', 0), '?')}: {getattr(e, 'message', '')}" for e in errs
            if getattr(e, "severity", 0) < 2]


def convert(job: dict) -> dict:
    src = Path(job["src"])
    arm = armature()
    arm.data.vrm_addon_extension.spec_version = "1.0"  # .blend 里可能存着 VRM 0.x 的设置
    rep = {"name": job["name"], "src": src.name}
    if src.suffix.lower() == ".blend":
        isolate_blend(arm, rep)
        relink_images(src, rep)
    else:
        clean_mmd_rig(arm, rep)
    meshes = skinned_meshes(arm)
    if job["meta"].get("drop_materials"):
        drop_materials(meshes, job["meta"]["drop_materials"], rep)
    w = weights(meshes)
    assign_humanoid(arm, w, rep)
    face_forward(arm, rep)
    if src.suffix.lower() != ".pmx":
        attach_orphans(arm, w, rep)
    chains = mmd_chains(arm) if src.suffix.lower() == ".pmx" else head_chains(arm, w)
    # 删掉材质 (袖子) 之后, 那些骨骼上已经没有网格了, 不再给它们建弹簧
    w = weights(meshes)
    chains = [c for c in chains if any(w.get(n) for n in c)]
    build_springs(arm, chains, body_colliders(arm, meshes, rep), rep)
    if src.suffix.lower() == ".pmx":
        remove_mmd_objects(arm, rep)
    sb = arm.data.vrm_addon_extension.spring_bone1
    rep["spring_bone"] = {"springs": len(sb.springs), "joints": sum(len(s.joints) for s in sb.springs),
                          "colliders": len(sb.colliders)}
    bind_expressions(arm, meshes, rep)
    eye_look_at(arm, rep)
    to_mtoon(meshes, rep)
    set_meta(arm, job["meta"])
    rep["verts"] = sum(len(o.data.vertices) for o in meshes)
    out = Path(job["out"])
    out.parent.mkdir(parents=True, exist_ok=True)
    select_only([arm], arm)
    ext = arm.data.vrm_addon_extension
    ext.spec_version = "1.0"
    rep["validation"] = validate(arm)
    if out.exists():
        out.unlink()  # 导出失败时不留旧文件冒充新结果
    result = bpy.ops.export_scene.vrm(filepath=str(out), armature_object_name=arm.name, ignore_warning=True,
                                      export_invisibles=False, export_only_selections=False)
    rep["export"] = list(result)
    rep["bytes"] = out.stat().st_size if out.exists() else 0
    if "FINISHED" not in result or not rep["bytes"]:
        Path(job["report"]).write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        raise SystemExit(f"{job['name']}: 导出失败, 校验结果: {rep['validation']}")
    log(f"{job['name']}: {rep['bytes'] / 1e6:.1f} MB, 骨骼修正 {rep['humanoid']['fixes']}, 弹簧 {rep['spring_bone']}, "
        f"表情缺 {rep['expressions']['missing']}")
    return rep


def main() -> None:
    job = json.loads(sys.argv[sys.argv.index("--") + 1])
    load(Path(job["src"]))
    if job["cmd"] == "inspect":
        rep = inspect(job)
    elif job["cmd"] == "convert":
        rep = convert(job)
    else:
        raise SystemExit(f"未知命令: {job['cmd']}")
    Path(job["report"]).parent.mkdir(parents=True, exist_ok=True)
    Path(job["report"]).write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    log("报告:", job["report"])


main()

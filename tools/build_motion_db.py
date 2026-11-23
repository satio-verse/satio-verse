"""把移动动捕重定向到 VRM 标准骨架, 生成浏览器端运动匹配 (motion matching) 数据库.

  python tools/build_motion_db.py          # 读 models/mocap/, 写 models/motion/<style>.json + .bin

数据 (下载见 tools/setup_models.py 的 mocap 和 mocap-extra 组件, 许可见 docs/third_party/mocap/SOURCE.md):
  100STYLE (BVH, 60 fps, CC BY 4.0): Neutral / Elated / Depressed / Angry / StartStop 各 8 段 (前后侧向的走和跑, 待机, 过渡)
  CMU 69 号 (ASF/AMC, 120 fps): 直走, 边走边转, 原地转身, 90 度转弯
  CMU 91 / 104 / 105 / 141 / 143 号 (2026-10-24 补): 起步停步、直角转弯、踱步, 害羞/慢走/难过/沮丧/开心/兴奋/严厉地走
  CMU 18 / 19 / 141 / 143 号的挥手、耸肩、说话手势 -> 手势库 gestures.{json,bin} (--gestures-only 只重建它)

重定向思路:
  两种骨架的零位都是 T 姿态, 零位时各骨骼的世界旋转为单位阵, 所以每帧骨骼的世界旋转 G 就是"相对 T 姿态的变化".
  先用 A_src 把源骨架的零位骨骼方向对齐到标准方向 (标准 T 姿态, VRM 1.0 坐标: +Z 朝前, +X 是角色左边),
  存 W = G * A_src^-1. 运行时再按具体 VRM 模型的零位骨骼方向做第二次对齐, 再换算成父子相对旋转.
  位置统一除以源骨架的髋高, 运行时乘以角色的髋高.

每帧存: 骨骼旋转 (相对根朝向), 髋部位置 (相对根), 根部速度和转向角速度, 左右脚触地, 匹配特征 27 维:
  未来 1/3, 2/3, 1 秒的根位置 (x,z) 与朝向 (x,z), 左右脚位置与速度, 髋部速度 (都在当前根坐标系里).
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MOCAP = ROOT / "models" / "mocap"
OUT = ROOT / "models" / "motion"
FPS = 30
HORIZON = (10, 20, 30)  # 未来轨迹采样帧 (30 fps 下 1/3, 2/3, 1 秒)

BONES = ["hips", "spine", "chest", "upperChest", "neck", "head",
         "leftShoulder", "leftUpperArm", "leftLowerArm", "leftHand",
         "rightShoulder", "rightUpperArm", "rightLowerArm", "rightHand",
         "leftUpperLeg", "leftLowerLeg", "leftFoot", "leftToes",
         "rightUpperLeg", "rightLowerLeg", "rightFoot", "rightToes"]
ENDS = ["headEnd", "leftHandEnd", "rightHandEnd", "leftToesEnd", "rightToesEnd"]
B = {n: i for i, n in enumerate(BONES)}
# 对齐用的骨骼方向: 关节 -> 目标关节 (源和 VRM 用同一套定义). hips 指向 chest, 因为 CMU 的 lowerback 起点和根重合.
AIM = {"hips": "chest", "spine": "chest", "chest": "neck", "upperChest": "neck", "neck": "head", "head": "headEnd",
       "leftShoulder": "leftUpperArm", "leftUpperArm": "leftLowerArm", "leftLowerArm": "leftHand", "leftHand": "leftHandEnd",
       "rightShoulder": "rightUpperArm", "rightUpperArm": "rightLowerArm", "rightLowerArm": "rightHand",
       "rightHand": "rightHandEnd", "leftUpperLeg": "leftLowerLeg", "leftLowerLeg": "leftFoot", "leftFoot": "leftToes",
       "leftToes": "leftToesEnd", "rightUpperLeg": "rightLowerLeg", "rightLowerLeg": "rightFoot",
       "rightFoot": "rightToes", "rightToes": "rightToesEnd"}
_FOOT = np.array([0.0, -0.42, 0.91])
CANON = {**{n: np.array([0.0, 1.0, 0.0]) for n in ("hips", "spine", "chest", "upperChest", "neck", "head")},
         **{n: np.array([1.0, 0.0, 0.0]) for n in ("leftShoulder", "leftUpperArm", "leftLowerArm", "leftHand")},
         **{n: np.array([-1.0, 0.0, 0.0]) for n in ("rightShoulder", "rightUpperArm", "rightLowerArm", "rightHand")},
         **{n: np.array([0.0, -1.0, 0.0]) for n in ("leftUpperLeg", "leftLowerLeg", "rightUpperLeg", "rightLowerLeg")},
         "leftFoot": _FOOT / np.linalg.norm(_FOOT), "rightFoot": _FOOT / np.linalg.norm(_FOOT),
         "leftToes": np.array([0.0, 0.0, 1.0]), "rightToes": np.array([0.0, 0.0, 1.0])}
MIRROR = [B[n.replace("left", "@").replace("right", "left").replace("@", "right")] for n in BONES]
MIRROR_END = [ENDS.index(n.replace("left", "@").replace("right", "left").replace("@", "right")) for n in ENDS]
# 特征分组权重, 参考 Holden 等人 Learned Motion Matching 的设置
GROUPS = [("traj_pos", 6, 1.0), ("traj_dir", 6, 1.5), ("foot_pos", 6, 0.75), ("foot_vel", 6, 1.0), ("hip_vel", 3, 1.0)]

BVH_MAP = {"hips": "Hips", "spine": "Chest", "chest": "Chest2", "upperChest": "Chest3", "neck": "Neck", "head": "Head",
           "leftShoulder": "LeftCollar", "leftUpperArm": "LeftShoulder", "leftLowerArm": "LeftElbow",
           "leftHand": "LeftWrist", "rightShoulder": "RightCollar", "rightUpperArm": "RightShoulder",
           "rightLowerArm": "RightElbow", "rightHand": "RightWrist", "leftUpperLeg": "LeftHip",
           "leftLowerLeg": "LeftKnee", "leftFoot": "LeftAnkle", "leftToes": "LeftToe", "rightUpperLeg": "RightHip",
           "rightLowerLeg": "RightKnee", "rightFoot": "RightAnkle", "rightToes": "RightToe"}
BVH_END = {"headEnd": "Head", "leftHandEnd": "LeftWrist", "rightHandEnd": "RightWrist",
           "leftToesEnd": "LeftToe", "rightToesEnd": "RightToe"}  # 这些关节的 End Site
# StayStill 的 LaFAN1 骨架 (MIT): 22 个关节, 没有手指
LAFAN_MAP = {"hips": "Hips", "spine": "Spine", "chest": "Spine1", "upperChest": "Spine2", "neck": "Neck", "head": "Head",
             "leftShoulder": "LeftShoulder", "leftUpperArm": "LeftArm", "leftLowerArm": "LeftForeArm",
             "leftHand": "LeftHand", "rightShoulder": "RightShoulder", "rightUpperArm": "RightArm",
             "rightLowerArm": "RightForeArm", "rightHand": "RightHand", "leftUpperLeg": "LeftUpLeg",
             "leftLowerLeg": "LeftLeg", "leftFoot": "LeftFoot", "leftToes": "LeftToe", "rightUpperLeg": "RightUpLeg",
             "rightLowerLeg": "RightLeg", "rightFoot": "RightFoot", "rightToes": "RightToe"}
LAFAN_END = {"headEnd": "Head", "leftHandEnd": "LeftHand", "rightHandEnd": "RightHand",
             "leftToesEnd": "LeftToe", "rightToesEnd": "RightToe"}
# 没有 End Site 时沿"父 -> 该关节"方向外推的长度 (米)
END_GUESS = {"Head": 0.16, "LeftHand": 0.09, "RightHand": 0.09, "LeftToe": 0.06, "RightToe": 0.06,
             "LeftWrist": 0.09, "RightWrist": 0.09}
ASF_MAP = {"hips": "root", "spine": "lowerback", "chest": "upperback", "upperChest": "thorax", "neck": "lowerneck",
           "head": "head", "leftShoulder": "lclavicle", "leftUpperArm": "lhumerus", "leftLowerArm": "lradius",
           "leftHand": "lhand", "rightShoulder": "rclavicle", "rightUpperArm": "rhumerus", "rightLowerArm": "rradius",
           "rightHand": "rhand", "leftUpperLeg": "lfemur", "leftLowerLeg": "ltibia", "leftFoot": "lfoot",
           "leftToes": "ltoes", "rightUpperLeg": "rfemur", "rightLowerLeg": "rtibia", "rightFoot": "rfoot",
           "rightToes": "rtoes"}
ASF_END = {"headEnd": "head", "leftHandEnd": "lhand", "rightHandEnd": "rhand", "leftToesEnd": "ltoes",
           "rightToesEnd": "rtoes"}  # 这些骨段的末端

# 每个风格库用哪些数据: [(100STYLE 风格, 哪几段)], [(CMU 受试者, 片段, 种类)]. 种类 "FW" 的 CMU 片段算进向前走的速度.
# CMU 69 号的原地转身补到情绪库里, 让情绪步态也能原地转. 100STYLE 没有的情绪 (害羞、平静): 向前走用 CMU 的害羞走、慢走,
# 后退、侧走、跑和待机借用中性的 (只有这些动作时才会匹配到, 向前走时风格片段更贴近).
KINDS = ["FW", "FR", "BW", "BR", "SW", "SR", "ID", "TR1"]
BORROW = ["FR", "BW", "BR", "SW", "SR", "ID"]
CMU_LOCO = [(69, t, "CMU") for t in [1, 2, *range(6, 34)]]
CMU_TURN = [(69, t, "CMU") for t in (16, 17, 18, 19)]
STYLES = {
    "neutral": ([("Neutral", KINDS), ("StartStop", KINDS)],
                CMU_LOCO + [(104, 27, "CMU"), (104, 14, "CMU"), (104, 19, "CMU"), (143, 29, "CMU"), (141, 29, "CMU"),
                            (141, 30, "CMU")]),
    "elated": ([("Elated", KINDS)], CMU_TURN + [(104, 11, "CMU"), (104, 12, "CMU")]),
    "depressed": ([("Depressed", KINDS)], CMU_TURN + [(91, 13, "CMU"), (91, 14, "CMU")]),
    "angry": ([("Angry", KINDS)], CMU_TURN + [(104, 32, "CMU")]),
    "shy": ([("Neutral", BORROW)], CMU_TURN + [(91, 20, "FW"), (105, 20, "FW")]),
    "calm": ([("Neutral", BORROW)], CMU_TURN + [(91, 10, "FW"), (104, 39, "FW")]),
}


# -- 旋转工具 ------------------------------------------------------------------------------
def axis_rot(axis: str, deg: np.ndarray) -> np.ndarray:
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    m = np.zeros(a.shape + (3, 3))
    i = "XYZ".index(axis.upper())
    j, k = (i + 1) % 3, (i + 2) % 3
    m[..., i, i] = 1
    m[..., j, j] = c
    m[..., k, k] = c
    m[..., j, k] = -s
    m[..., k, j] = s
    return m


def from_to(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """把方向 a 转到方向 b 的最小旋转."""
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    v, c = np.cross(a, b), float(np.dot(a, b))
    if c < -0.999999:
        axis = np.cross(a, [1.0, 0, 0]) if abs(a[0]) < 0.9 else np.cross(a, [0, 1.0, 0])
        axis /= np.linalg.norm(axis)
        return 2 * np.outer(axis, axis) - np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx / (1 + c)


def mat_to_quat(m: np.ndarray) -> np.ndarray:
    """[..., 3, 3] -> [..., 4] (x, y, z, w), w >= 0."""
    t = np.trace(m, axis1=-2, axis2=-1)
    q = np.empty(m.shape[:-2] + (4,))
    w = np.sqrt(np.maximum(0, 1 + t)) / 2
    x = np.sqrt(np.maximum(0, 1 + m[..., 0, 0] - m[..., 1, 1] - m[..., 2, 2])) / 2
    y = np.sqrt(np.maximum(0, 1 - m[..., 0, 0] + m[..., 1, 1] - m[..., 2, 2])) / 2
    z = np.sqrt(np.maximum(0, 1 - m[..., 0, 0] - m[..., 1, 1] + m[..., 2, 2])) / 2
    q[..., 0] = np.copysign(x, m[..., 2, 1] - m[..., 1, 2])
    q[..., 1] = np.copysign(y, m[..., 0, 2] - m[..., 2, 0])
    q[..., 2] = np.copysign(z, m[..., 1, 0] - m[..., 0, 1])
    q[..., 3] = w
    return q / np.linalg.norm(q, axis=-1, keepdims=True)


def yaw_mat(yaw: np.ndarray) -> np.ndarray:
    """绕 Y 轴旋转, yaw=0 朝 +Z."""
    c, s = np.cos(yaw), np.sin(yaw)
    m = np.zeros(yaw.shape + (3, 3))
    m[..., 0, 0] = c
    m[..., 0, 2] = s
    m[..., 1, 1] = 1
    m[..., 2, 0] = -s
    m[..., 2, 2] = c
    return m


def smooth(x: np.ndarray, sigma: float) -> np.ndarray:
    """沿时间轴高斯平滑, 两端按边缘值延拓."""
    r = int(3 * sigma)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    pad = np.concatenate([np.repeat(x[:1], r, 0), x, np.repeat(x[-1:], r, 0)])
    return np.stack([np.convolve(pad[:, i], k, mode="valid") for i in range(x.shape[1])], axis=1)


# -- 解析 ----------------------------------------------------------------------------------
def load_bvh(path: Path, bmap: dict = BVH_MAP, bend: dict = BVH_END) -> dict:
    text = path.read_text()
    head, motion = text.split("MOTION")
    joints, stack, ends = [], [], {}
    tokens = re.findall(r"[{}]|[^\s{}]+", head)
    i, pending_end = 0, False
    while i < len(tokens):
        t = tokens[i]
        if t in ("ROOT", "JOINT"):
            joints.append({"name": tokens[i + 1], "parent": stack[-1] if stack else -1, "channels": []})
            i += 2
            pending_end = False
            continue
        if t == "End":
            pending_end = True
            i += 2
            continue
        if t == "{":
            stack.append(-2 if pending_end else len(joints) - 1)
        elif t == "}":
            stack.pop()
            pending_end = False
        elif t == "OFFSET":
            off = np.array([float(x) for x in tokens[i + 1:i + 4]]) * 0.01  # 厘米 -> 米
            if stack and stack[-1] == -2:
                ends[joints[[s for s in stack if s >= 0][-1]]["name"]] = off
            else:
                joints[-1]["offset"] = off
            i += 3
        elif t == "CHANNELS":
            n = int(tokens[i + 1])
            joints[-1]["channels"] = tokens[i + 2:i + 2 + n]
            i += 1 + n
        i += 1
    lines = motion.strip().splitlines()
    frame_time = float(lines[1].split(":")[1])
    data = np.array([[float(v) for v in line.split()] for line in lines[2:] if line.strip()])
    T = len(data)
    G, P, col = [None] * len(joints), [None] * len(joints), 0
    for j, jt in enumerate(joints):
        rot, pos = np.broadcast_to(np.eye(3), (T, 3, 3)).copy(), None
        for ch in jt["channels"]:
            v = data[:, col]
            col += 1
            if ch.endswith("position"):
                pos = np.zeros((T, 3)) if pos is None else pos
                pos[:, "XYZ".index(ch[0])] = v * 0.01
            else:
                rot = rot @ axis_rot(ch[0], v)
        if jt["parent"] < 0:
            G[j], P[j] = rot, pos
        else:
            p = jt["parent"]
            G[j] = G[p] @ rot
            P[j] = P[p] + np.einsum("tij,j->ti", G[p], jt["offset"])
    names = [jt["name"] for jt in joints]
    rest = {}
    for j, jt in enumerate(joints):  # 零位 (所有旋转为零) 下的关节位置
        rest[jt["name"]] = jt["offset"] + (rest[names[jt["parent"]]] if jt["parent"] >= 0 else 0)
    for e in bend.values():  # 没有 End Site (或只有几毫米, LaFAN1 就是这样): 零位时关节坐标系就是世界坐标系, 沿父 -> 关节方向外推
        if e not in ends or np.linalg.norm(ends[e]) < 0.02:
            d = rest[e] - rest[names[joints[names.index(e)]["parent"]]]
            ends[e] = d / (np.linalg.norm(d) + 1e-9) * END_GUESS.get(e, 0.08)
    idx = {n: names.index(bmap[n]) for n in BONES}
    end_pos = np.stack([P[names.index(bend[e])] + np.einsum("tij,j->ti", G[names.index(bend[e])],
                                                              ends[bend[e]]) for e in ENDS], 1)
    return {"fps": round(1 / frame_time), "rot": np.stack([G[idx[n]] for n in BONES], 1),
            "pos": np.stack([P[idx[n]] for n in BONES], 1), "end": end_pos,
            "rest_pos": np.stack([rest[bmap[n]] for n in BONES]),
            "rest_end": np.stack([rest[bend[e]] + ends[bend[e]] for e in ENDS])}


def load_asf(path: Path) -> dict:
    text = path.read_text()
    unit = 0.0254 / float(re.search(r"length\s+([\d.]+)", text).group(1))  # ASF 长度 -> 英寸 -> 米
    bones = {"root": {"parent": None, "dir": np.zeros(3), "len": 0.0, "C": np.eye(3), "dof": ["rx", "ry", "rz"]}}
    for block in re.findall(r"begin(.*?)end", text.split(":bonedata")[1].split(":hierarchy")[0], re.S):
        name = re.search(r"name\s+(\S+)", block).group(1)
        axis = [float(x) for x in re.search(r"axis\s+([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)", block).groups()]
        dof = re.search(r"dof\s+([^\n]+)", block)
        C = axis_rot("Z", np.array(axis[2])) @ axis_rot("Y", np.array(axis[1])) @ axis_rot("X", np.array(axis[0]))
        bones[name] = {"dir": np.array([float(x) for x in re.search(r"direction\s+(\S+)\s+(\S+)\s+(\S+)", block).groups()]),
                       "len": float(re.search(r"length\s+(\S+)", block).group(1)) * unit, "C": C,
                       "dof": dof.group(1).split() if dof else []}
    for line in text.split(":hierarchy")[1].split("begin")[1].split("end")[0].strip().splitlines():
        parent, *children = line.split()
        for c in children:
            bones[c]["parent"] = parent
    return {"bones": bones, "unit": unit}


def load_amc(path: Path, skel: dict) -> dict:
    bones, unit = skel["bones"], skel["unit"]
    frames, cur = [], None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line[0] in "#:":
            continue
        if line.isdigit():
            cur = {}
            frames.append(cur)
        else:
            name, *vals = line.split()
            cur[name] = [float(v) for v in vals]
    T = len(frames)
    G, P = {}, {}  # 世界旋转; 骨段起点位置 (= 父骨段末端)
    end = {}
    order = ["root"]
    for name in order:  # 广度优先
        order += [c for c, b in bones.items() if b.get("parent") == name and c not in order]
    for name in order:
        b = bones[name]
        vals = np.array([frames[t].get(name, [0.0] * (6 if name == "root" else len(b["dof"]))) for t in range(T)])
        if name == "root":
            pos = vals[:, :3] * unit
            M = axis_rot("Z", vals[:, 5]) @ axis_rot("Y", vals[:, 4]) @ axis_rot("X", vals[:, 3])
            G[name], P[name], end[name] = M, pos, pos
            continue
        ang = {d: np.zeros(T) for d in ("rx", "ry", "rz")}
        for k, d in enumerate(b["dof"]):
            ang[d] = vals[:, k]
        M = axis_rot("Z", ang["rz"]) @ axis_rot("Y", ang["ry"]) @ axis_rot("X", ang["rx"])
        G[name] = G[b["parent"]] @ b["C"] @ M @ b["C"].T
        P[name] = end[b["parent"]]
        end[name] = P[name] + np.einsum("tij,j->ti", G[name], b["dir"] * b["len"])
    rest_end, rest_start = {"root": np.zeros(3)}, {"root": np.zeros(3)}
    for name in order[1:]:
        b = bones[name]
        rest_start[name] = rest_end[b["parent"]]
        rest_end[name] = rest_start[name] + b["dir"] * b["len"]
    return {"fps": 120, "rot": np.stack([G[ASF_MAP[n]] for n in BONES], 1),
            "pos": np.stack([P[ASF_MAP[n]] for n in BONES], 1),
            "end": np.stack([end[ASF_END[e]] for e in ENDS], 1),
            "rest_pos": np.stack([rest_start[ASF_MAP[n]] for n in BONES]),
            "rest_end": np.stack([rest_end[ASF_END[e]] for e in ENDS])}


# -- 处理 ----------------------------------------------------------------------------------
def mirror(clip: dict) -> dict:
    """左右镜像 (x -> -x), 左右骨骼互换."""
    S = np.diag([-1.0, 1.0, 1.0])
    return {**clip, "rot": (S @ clip["rot"] @ S)[:, MIRROR], "pos": (clip["pos"] @ S)[:, MIRROR],
            "end": (clip["end"] @ S)[:, MIRROR_END], "rest_pos": (clip["rest_pos"] @ S)[MIRROR],
            "rest_end": (clip["rest_end"] @ S)[MIRROR_END]}


def point(clip_pos: np.ndarray, clip_end: np.ndarray, name: str) -> np.ndarray:
    return clip_end[..., ENDS.index(name), :] if name in ENDS else clip_pos[..., B[name], :]


def reference_align(clip: dict, f0: int) -> tuple[np.ndarray, dict]:
    """零位不是 T 姿态的骨架 (LaFAN1: 所有骨骼沿局部 +X 伸出) 只对齐骨骼指向会丢掉扭转 (膝盖往侧面弯).
    这里用一帧自然站立的姿态, 由关节位置定出每根骨骼的完整朝向: 指向 + 次方向 (身体朝前; 脚和脚趾用身体左右),
    得到常量对齐 M_b, 使 W(t) = G(t) M_b. 原始旋转 (包括头的转动) 都保留, 只是换了参考系."""
    pos, end = clip["pos"][f0], clip["end"][f0]
    up = np.array([0.0, 1.0, 0.0])
    lat = pos[B["leftUpperLeg"]] - pos[B["rightUpperLeg"]]
    lat[1] = 0
    lat /= np.linalg.norm(lat)
    fwd = np.cross(lat, up)

    def basis(aim, sec):
        aim = aim / np.linalg.norm(aim)
        sec = sec - np.dot(sec, aim) * aim
        sec /= np.linalg.norm(sec)
        return np.stack([aim, sec, np.cross(aim, sec)], 1)

    M = np.zeros((len(BONES), 3, 3))
    for b, n in enumerate(BONES):
        foot = n.endswith("Foot") or n.endswith("Toes")
        obs = basis(point(pos, end, AIM[n]) - pos[b], lat if foot else fwd)
        can = basis(CANON[n], np.array([1.0, 0, 0]) if foot else np.array([0, 0, 1.0]))
        M[b] = clip["rot"][f0, b].T @ (obs @ can.T)
    # 零位的腿是横着的, 髋高和脚掌高度也要从这帧站姿里量
    sole = min(end[ENDS.index("leftToesEnd"), 1], end[ENDS.index("rightToesEnd"), 1], pos[B["leftFoot"], 1], pos[B["rightFoot"], 1])
    ref = {"hip_h": float(pos[B["hips"], 1] - sole), "toe_h": float((pos[B["leftToes"], 1] + pos[B["rightToes"], 1]) / 2 - sole)}
    return M, ref


def still_frame(clip: dict, lo_s: float = 5, hi_s: float = 30) -> int:
    """lo_s 到 hi_s 秒之间关节运动最小的一帧 (自然站立)."""
    fps = clip["fps"]
    a, b = int(lo_s * fps), min(int(hi_s * fps), len(clip["pos"]) - 1)
    if b - a < 2:  # 片段太短: 整段里找
        a, b = 0, len(clip["pos"]) - 1
    v = np.linalg.norm(np.diff(clip["pos"][a:b + 1], axis=0), axis=2).sum(1)
    return a + int(np.argmin(v))


def process(clip: dict, start: int = 0, stop: int | None = None, align: np.ndarray | None = None,
            ref: dict | None = None, fps: int = FPS) -> dict:
    step = clip["fps"] // fps
    sl = slice(start, stop, step)
    rot, pos, end = clip["rot"][sl], clip["pos"][sl], clip["end"][sl]
    T = len(rot)
    rest_pos, rest_end = clip["rest_pos"], clip["rest_end"]
    # 髋高: 零位时髋部离脚底 (脚尖末端) 的高度; 所有位置除以它
    floor_rest = min(rest_end[ENDS.index("leftToesEnd"), 1], rest_pos[B["leftFoot"], 1])
    hip_h = rest_pos[B["hips"], 1] - floor_rest if ref is None else ref["hip_h"]
    # 地面: 每帧脚部最低点的低分位数
    feet = np.stack([pos[:, B["leftToes"], 1], pos[:, B["rightToes"], 1], end[:, ENDS.index("leftToesEnd"), 1],
                     end[:, ENDS.index("rightToesEnd"), 1], pos[:, B["leftFoot"], 1], pos[:, B["rightFoot"], 1]], 1)
    ground = np.percentile(feet.min(1), 5) - ((rest_end[ENDS.index("leftToesEnd"), 1] - floor_rest) if ref is None else 0.0)
    pos = pos - [0, ground, 0]
    end = end - [0, ground, 0]
    # 对齐到标准 T 姿态: W = G * A_src^-1 (零位是 T 姿态的骨架); 否则用参考帧定出的常量对齐 W = G * M
    if align is None:
        A = np.stack([from_to(point(rest_pos, rest_end, AIM[n]) - rest_pos[B[n]], CANON[n]) for n in BONES])
        W = rot @ np.transpose(A, (0, 2, 1))[None]
    else:
        W = rot @ align[None]
    # 根 (模拟骨): 髋部投影到地面并平滑; 朝向取髋和胸的前向平均后平滑
    hips = pos[:, B["hips"]]
    root = smooth(hips[:, [0, 2]], 4.0)
    fwd = (W[:, B["hips"]] @ [0, 0, 1.0]) + (W[:, B["chest"]] @ [0, 0, 1.0])
    fwd = smooth(fwd[:, [0, 2]], 6.0)
    fwd /= np.linalg.norm(fwd, axis=1, keepdims=True)
    yaw = np.arctan2(fwd[:, 0], fwd[:, 1])
    inv = np.transpose(yaw_mat(yaw), (0, 2, 1))  # 世界 -> 根坐标
    local_rot = inv[:, None] @ W
    root3 = np.stack([root[:, 0], np.zeros(T), root[:, 1]], 1)
    rel = lambda p: np.einsum("tij,tj->ti", inv, p - root3) / hip_h  # noqa: E731
    hips_local = rel(hips)
    vel = lambda p: np.gradient(p, axis=0) * fps  # noqa: E731
    lfoot, rfoot = pos[:, B["leftFoot"]], pos[:, B["rightFoot"]]
    # 触地: 脚掌 (toes 关节) 低且慢. 阈值用米
    contacts = []
    for side in ("left", "right"):
        ball = pos[:, B[f"{side}Toes"]]
        speed = np.linalg.norm(vel(ball), axis=1)
        h = ball[:, 1] - ((rest_pos[B[f"{side}Toes"], 1] - floor_rest) if ref is None else ref["toe_h"])
        c = (h < 0.05) & (speed < 0.45)
        c = np.convolve(c.astype(float), np.ones(3) / 3, mode="same") > 0.5
        contacts.append(c)
    contacts = np.stack(contacts, 1)
    # 特征
    feats = np.zeros((T, 27))
    for k, h in enumerate(HORIZON):
        idx = np.minimum(np.arange(T) + h, T - 1)
        d = np.einsum("tij,tj->ti", inv, root3[idx] - root3) / hip_h
        feats[:, 2 * k:2 * k + 2] = d[:, [0, 2]]
        f = np.einsum("tij,tj->ti", inv, np.stack([fwd[idx, 0], np.zeros(T), fwd[idx, 1]], 1))
        feats[:, 6 + 2 * k:8 + 2 * k] = f[:, [0, 2]]
    feats[:, 12:15], feats[:, 15:18] = rel(lfoot), rel(rfoot)
    feats[:, 18:21] = np.einsum("tij,tj->ti", inv, vel(lfoot)) / hip_h
    feats[:, 21:24] = np.einsum("tij,tj->ti", inv, vel(rfoot)) / hip_h
    feats[:, 24:27] = np.einsum("tij,tj->ti", inv, vel(hips)) / hip_h
    speed = np.linalg.norm(np.gradient(root, axis=0), axis=1) * fps / hip_h
    # 根部运动: 根坐标系里的速度 (髋高/秒) 和转向角速度 (弧度/秒), 运行时按它推进角色
    root_vel = np.einsum("tij,tj->ti", inv, np.gradient(root3, axis=0) * fps) / hip_h
    yaw_rate = np.gradient(np.unwrap(yaw)) * fps
    motion = np.stack([root_vel[:, 0], root_vel[:, 2], yaw_rate], 1)
    hands = np.stack([end[:, ENDS.index("leftHandEnd")], end[:, ENDS.index("rightHandEnd")]], 1)
    return {"quat": mat_to_quat(local_rot), "hips": hips_local, "contacts": contacts, "feats": feats,
            "speed": speed, "root": motion, "frames": T,
            # 切动作片段用的世界坐标量 (米): 根位置/朝向, 髋部, 双手末端
            "world": {"root3": root3, "yaw": np.unwrap(yaw), "hips": hips, "hands": hands, "hip_h": hip_h,
                      "feet": np.stack([lfoot, rfoot], 1),
                      "knees": np.stack([pos[:, B["leftLowerLeg"]], pos[:, B["rightLowerLeg"]]], 1),
                      "thighs": np.stack([pos[:, B["leftUpperLeg"]], pos[:, B["rightUpperLeg"]]], 1), "chest": pos[:, B["chest"]],
                      "toes": np.stack([pos[:, B["leftToes"]], pos[:, B["rightToes"]]], 1)}}


def frame_cuts() -> dict:
    with (MOCAP / "100STYLE" / "Frame_Cuts.csv").open() as f:
        return {row["STYLE_NAME"]: row for row in csv.DictReader(f)}


_SKELS: dict[int, dict] = {}


def cmu_clip(s: int, t: int) -> dict:
    skel = _SKELS.setdefault(s, load_asf(MOCAP / "cmu" / str(s) / f"{s}.asf"))
    return load_amc(MOCAP / "cmu" / str(s) / f"{s}_{t:02d}.amc", skel)


def build(style_key: str, parts: list[tuple[str, list[str]]], cmu: list[tuple[int, int, str]], cuts: dict) -> dict:
    clips = []
    for style, kinds in parts:
        for kind in kinds:
            raw = load_bvh(MOCAP / "100STYLE" / style / f"{style}_{kind}.bvh")
            start, stop = int(cuts[style][f"{kind}_START"]), int(cuts[style][f"{kind}_STOP"])
            for m in (False, True):
                clips.append((f"{style}_{kind}{'_M' if m else ''}", kind, process(mirror(raw) if m else raw, start, stop)))
    for s, t, kind in cmu:
        raw = cmu_clip(s, t)
        # 69 号原来就只用原样的一份; 新补的片段 (多是单向转弯、单侧动作) 左右镜像各一份, 和 100STYLE 一样
        for m in ((False,) if s == 69 else (False, True)):
            clips.append((f"cmu{s}_{t:02d}{'_M' if m else ''}", kind, process(mirror(raw) if m else raw)))
    feats = np.concatenate([c["feats"] for _, _, c in clips])
    mean, std = feats.mean(0), feats.std(0) + 1e-6
    scale, o = np.zeros(27), 0
    for _, n, w in GROUPS:
        scale[o:o + n] = std[o:o + n].mean() / w
        o += n
    meta_clips, start = [], 0
    for name, kind, c in clips:
        meta_clips.append({"name": name, "kind": kind, "start": start, "end": start + c["frames"]})
        start += c["frames"]
    F = start
    norm = ((feats - mean) / scale).astype(np.float32)
    hips = np.concatenate([c["hips"] for _, _, c in clips]).astype(np.float32)
    root = np.concatenate([c["root"] for _, _, c in clips]).astype(np.float32)
    quat = np.concatenate([c["quat"] for _, _, c in clips])
    q16 = np.round(np.clip(quat, -1, 1) * 32767).astype(np.int16)
    contacts = np.concatenate([c["contacts"] for _, _, c in clips]).astype(np.uint8)
    blobs = [("features", norm), ("hips", hips), ("root", root), ("quats", q16), ("contacts", contacts)]
    offsets, o = {}, 0
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / f"{style_key}.bin").open("wb") as f:
        for name, arr in blobs:
            offsets[name] = o
            f.write(arr.tobytes())
            o += arr.nbytes
    # 走跑速度只按主风格 (第一组 100STYLE) 和标成 FW 的 CMU 片段算: StartStop 里大量停下来的帧会把中性的走路速度拉低
    main = parts[0][0]
    speeds = {k: float(np.median(np.concatenate([c["speed"] for name, kk, c in clips
                                                 if kk == k and (name.startswith(main + "_") or name.startswith("cmu"))])))
              for k in ("FW", "FR", "BW", "SW")}
    meta = {"version": 1, "style": style_key, "fps": FPS, "frames": F, "bones": BONES, "ends": ENDS,
            "horizon": HORIZON, "groups": [[g, n, w] for g, n, w in GROUPS],
            "featureMean": mean.round(6).tolist(), "featureScale": scale.round(6).tolist(),
            "canon": {n: CANON[n].round(6).tolist() for n in BONES}, "aim": AIM,
            "speeds": {k: round(v, 4) for k, v in speeds.items()}, "clips": meta_clips, "offsets": offsets,
            "bytes": o, "sources": {"100STYLE": ", ".join(st for st, _ in parts) + " (CC BY 4.0)",
                                    "cmu": "subjects " + ", ".join(str(x) for x in sorted({s for s, _, _ in cmu}))
                                           + " (mocap.cs.cmu.edu)"}}
    (OUT / f"{style_key}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


# -- 物体交互的动作片段 ------------------------------------------------------------------------
# 坐下: 坐姿髋高 0.53-0.62 髋高单位的片段, 和沙发座面 (约 0.43 m) 最接近. 75 号的中/低坐姿太低, 86 号是靠坐在高处,
# 14_29 坐高凳晃腿, 14_36 与 14_31 数据相同, 都不用.
SIT_CLIPS = [(143, 18), (14, 30), (14, 31), (14, 32), (13, 4), (13, 5), (13, 6), (75, 18),
             (143, 19), (141, 17), (18, 12), (19, 12)]  # 后 4 段 2026-10-24 补: 坐凳子再起身; 朋友见面坐下聊天
PICK_CLIPS = [(69, t) for t in range(68, 76)]   # 每段: 从地上捡起 (第 1 次手最低), 放回地上 (第 2 次)
REACH_CLIPS = [(15, 6)]                         # 前倾伸手, 手在 1.1-1.3 m 高: 往书架上放/拿


def _local(w: dict, s: int, p: np.ndarray) -> list:
    """世界点 p 换到第 s 帧的根坐标系, 髋高单位."""
    c, n = np.cos(w["yaw"][s]), np.sin(w["yaw"][s])
    d = (p - w["root3"][s]) / w["hip_h"]
    return [round(float(c * d[0] - n * d[2]), 4), round(float(d[1]), 4), round(float(n * d[0] + c * d[2]), 4)]


def knee_forward(w: dict, start: int, end: int) -> float:
    """坐着的那段里, 靠后的那个膝盖在髋部前方多远 (沿当时的朝向, 髋高单位, 取中位数). 一般 0.45-0.52;
    坐凳子时把腿收到座位下面的片段只有 0.25 左右, 套到前脸是实心的沙发上膝盖会插进坐垫前沿 (运行时按座位深度避开)."""
    yaw = w["yaw"][start:end]
    d = w["knees"][start:end] - w["hips"][start:end, None, :]
    fwd = np.sin(yaw)[:, None] * d[..., 0] + np.cos(yaw)[:, None] * d[..., 2]
    return round(float(np.median(fwd.min(1))) / float(w["hip_h"]), 3)


def sit_segments(c: dict) -> list[dict]:
    """坐着的区间 (髋高 < 0.75 超过 1 秒) -> 坐下 / 坐着 / 站起三段, 坐下段记录落座时髋部相对起点的位置."""
    w, T = c["world"], c["frames"]
    h = w["hips"][:, 1] / w["hip_h"]
    out, i = [], 0
    while i < T:
        if h[i] >= 0.75:
            i += 1
            continue
        a = i
        while i < T and h[i] < 0.75:
            i += 1
        b = i
        if b - a < 30:
            continue
        s0 = a
        while s0 > 0 and h[s0] < 0.95:
            s0 -= 1
        down = (max(0, s0 - 15), min(b, a + 20))
        idle = (down[1], b - 10)
        e1 = b
        while e1 < T and h[e1] < 0.95:
            e1 += 1
        up = (max(idle[1], b - 10), min(T, e1 + 20)) if e1 < T else None
        if idle[1] - idle[0] < 45 or s0 == 0:
            continue
        seat = down[1]
        speed = np.linalg.norm(np.gradient(w["root3"][:, [0, 2]], axis=0), axis=1) * FPS
        ankles = np.minimum(w["feet"][:, 0, 1], w["feet"][:, 1, 1])[idle[0]:idle[1]]
        out.append({"down": down, "idle": idle, "up": up, "seatHips": round(float(np.median(h[idle[0]:idle[1]])), 4),
                    "startSpeed": round(float(speed[down[0]]), 3),
                    "feetY": round(float(np.median(ankles)), 3),  # 坐着时较低那只脚踝的高度 (米), 脚踩横档时偏高
                    "kneeFwd": knee_forward(w, *idle),
                    "hips": _local(w, down[0], w["hips"][seat]),
                    "yaw": round(float(w["yaw"][seat] - w["yaw"][down[0]]), 4)})
    return out


def hand_events(c: dict, below: float = 0.45) -> list[int]:
    """手的局部最低点 (低于 below 米, 1 秒窗口内最低) -> 抓取/放下的那一帧."""
    w = c["world"]
    hand = w["hands"][:, :, 1].min(1)
    return [i for i in range(len(hand)) if hand[i] < below and hand[i] == hand[max(0, i - 15):i + 16].min()]


def body_points(w: dict, start: int, end: int, n: int = 6) -> dict:
    """片段里均匀取 n 个时刻, 两侧膝盖、脚尖相对起点根坐标系的位置 (髋高单位, 和 hand 同一个坐标系).
    运行时选站位用: 蹲下拿放时膝盖往前伸, 站位只看脚下能不能走的话, 放到矮桌上时膝盖会插进桌子."""
    frames = np.linspace(start, end - 1, n).round().astype(int)
    return {"knee": [_local(w, start, w["knees"][f, s]) for f in frames for s in (0, 1)],
            "toe": [_local(w, start, w["toes"][f, s]) for f in frames for s in (0, 1)]}


def leg_angles(w: dict, start: int, end: int) -> dict:
    """片段里两条腿的髋屈曲、髋外展、膝屈曲的最大值 (度). 和运动学约束 (web/kinematics-core.js 的 jointChecks) 同一套量法:
    骨盆坐标由左右髋关节连线和髋中点指向胸部定, 大腿方向在里面算屈曲 (往前为正) 和偏离矢状面 (往外为正);
    膝是大腿和小腿的夹角. 运行时选拿放片段时, 超出关节活动范围的 (蛙式全蹲, 髋屈 150 度以上) 往后排."""
    L, R = w["thighs"][start:end, 0], w["thighs"][start:end, 1]
    lat = L - R
    lat /= np.linalg.norm(lat, axis=1, keepdims=True)
    up = w["chest"][start:end] - (L + R) / 2
    up -= lat * np.sum(up * lat, axis=1, keepdims=True)
    up /= np.linalg.norm(up, axis=1, keepdims=True)
    fwd = np.cross(lat, up)
    out = {"hipFlex": -180.0, "hipAbd": -90.0, "knee": 0.0}
    for k, (hip, sign) in enumerate(((L, 1), (R, -1))):
        knee, ankle = w["knees"][start:end, k], w["feet"][start:end, k]
        t = knee - hip
        t /= np.linalg.norm(t, axis=1, keepdims=True)
        flex = np.degrees(np.arctan2(np.sum(t * fwd, 1), -np.sum(t * up, 1)))
        abd = np.degrees(np.arcsin(np.clip(np.sum(t * -lat, 1) * -sign, -1, 1)))
        shin = ankle - knee
        shin /= np.linalg.norm(shin, axis=1, keepdims=True)
        kf = np.degrees(np.arccos(np.clip(np.sum(t * shin, 1), -1, 1)))
        out = {"hipFlex": max(out["hipFlex"], float(flex.max())), "hipAbd": max(out["hipAbd"], float(abd.max())),
               "knee": max(out["knee"], float(kf.max()))}
    return {k: round(v, 1) for k, v in out.items()}


def pick_segment(c: dict, g: int) -> dict:
    """围绕抓取帧 g 切一段: 从接近时最慢的那一帧开始 (尽量从站定开始), 到重新站直后 0.3 秒."""
    w, T = c["world"], c["frames"]
    speed = np.linalg.norm(np.gradient(w["root3"][:, [0, 2]], axis=0), axis=1) * FPS
    lo = max(0, g - 45)
    start = lo + int(np.argmin(speed[lo:max(lo + 1, g - 12)]))
    h = w["hips"][:, 1] / w["hip_h"]
    stand = float(np.median(h))
    end = g + 15
    while end < T - 1 and h[end] < stand * 0.95:
        end += 1
    end = min(T, end + 10, g + 75)
    side = int(np.argmin(w["hands"][g, :, 1]))
    return {"range": (start, end), "event": g, "side": "left" if side == 0 else "right", "startSpeed": round(float(speed[start]), 3),
            "hand": _local(w, start, w["hands"][g, side]), "yaw": round(float(w["yaw"][g] - w["yaw"][start]), 4),
            "posture": "squat" if h[start:end].min() < 0.6 else "bend", "body": body_points(w, start, end),
            "legs": leg_angles(w, start, end)}


def reach_segments(c: dict) -> list[dict]:
    """前伸最远的帧 (前伸 > 0.75 m) 前后各 1 秒."""
    w, T = c["world"], c["frames"]
    out = []
    c_, s_ = np.cos(w["yaw"]), np.sin(w["yaw"])
    for side in (0, 1):
        d = w["hands"][:, side] - w["hips"]
        fwd = s_ * d[:, 0] + c_ * d[:, 2]
        for k in range(30, T - 30):
            if fwd[k] > 0.75 and fwd[k] == fwd[k - 30:k + 31].max():
                out.append({"range": (k - 30, k + 30), "event": k, "side": "left" if side == 0 else "right",
                            "hand": _local(w, k - 30, w["hands"][k, side]),
                            "yaw": round(float(w["yaw"][k] - w["yaw"][k - 30]), 4)})
    return out


def build_actions() -> dict:
    """物体交互片段库 models/motion/actions.{json,bin}: 每个片段是一段连续帧, 带种类和关键帧信息."""
    skels, raws = {}, []
    for s, t in SIT_CLIPS + PICK_CLIPS + REACH_CLIPS:
        skels.setdefault(s, load_asf(MOCAP / "cmu" / str(s) / f"{s}.asf"))
        raws.append((s, t, load_amc(MOCAP / "cmu" / str(s) / f"{s}_{t:02d}.amc", skels[s])))
    clips, groups, items = [], {"sit": [], "pick": [], "put": [], "reach": []}, []

    def add(name, kind, c, rng, **info):
        items.append((name, kind, c, rng, info))
        return len(items) - 1

    for s, t, raw in raws:
        for m in (False, True):
            c = process(mirror(raw) if m else raw)
            tag = f"cmu{s}_{t:02d}{'_M' if m else ''}"
            if (s, t) in SIT_CLIPS:
                for k, seg in enumerate(sit_segments(c)):
                    g = {"down": add(f"{tag}_sit{k}_down", "sit_down", c, seg["down"]),
                         "idle": add(f"{tag}_sit{k}_idle", "sit_idle", c, seg["idle"]),
                         "up": add(f"{tag}_sit{k}_up", "sit_up", c, seg["up"]) if seg["up"] else None,
                         "seatHips": seg["seatHips"], "hips": seg["hips"], "yaw": seg["yaw"],
                         "startSpeed": seg["startSpeed"], "feetY": seg["feetY"], "kneeFwd": seg["kneeFwd"], "source": tag}
                    groups["sit"].append(g)
            elif (s, t) in PICK_CLIPS:
                ev = hand_events(c)
                for kind, g in zip(("pick", "put"), ev[:2]):
                    seg = pick_segment(c, g)
                    idx = add(f"{tag}_{kind}", kind, c, seg["range"])
                    groups[kind].append({"clip": idx, "eventOffset": g - seg["range"][0], "side": seg["side"],
                                         "hand": seg["hand"], "yaw": seg["yaw"], "posture": seg["posture"],
                                         "startSpeed": seg["startSpeed"], "body": seg["body"], "legs": seg["legs"], "source": tag})
            else:
                for k, seg in enumerate(reach_segments(c)):
                    idx = add(f"{tag}_reach{k}", "reach", c, seg["range"])
                    groups["reach"].append({"clip": idx, "eventOffset": seg["event"] - seg["range"][0], "side": seg["side"],
                                            "hand": seg["hand"], "yaw": seg["yaw"], "source": tag})
    start = 0
    for name, kind, c, (a, b), _ in items:
        clips.append({"name": name, "kind": kind, "start": start, "end": start + (b - a)})
        start += b - a
    cat = lambda key: np.concatenate([c[key][a:b] for _, _, c, (a, b), _ in items])  # noqa: E731
    blobs = [("hips", cat("hips").astype(np.float32)), ("root", cat("root").astype(np.float32)),
             ("quats", np.round(np.clip(cat("quat"), -1, 1) * 32767).astype(np.int16)),
             ("contacts", cat("contacts").astype(np.uint8))]
    offsets, o = {}, 0
    with (OUT / "actions.bin").open("wb") as f:
        for name, arr in blobs:
            offsets[name] = o
            f.write(arr.tobytes())
            o += arr.nbytes
    meta = {"version": 1, "kind": "actions", "fps": FPS, "frames": start, "bones": BONES, "clips": clips,
            "groups": groups, "offsets": offsets, "bytes": o, "canon": {n: CANON[n].round(6).tolist() for n in BONES},
            "aim": AIM, "sources": {"cmu": "subjects " + ", ".join(str(s) for s in sorted({s for s, _ in SIT_CLIPS + PICK_CLIPS
                                                                                            + REACH_CLIPS}))
                                    + " (mocap.cs.cmu.edu)"}}
    (OUT / "actions.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


# 站着的片段 (待机、手势) 的站立髋高统一归一到零位髋高的这个倍数: 膝盖微屈, 离开腿完全伸直的奇点.
# 源数据里不一样 (StayStill 各人 1.03-1.14 倍, CMU 手势 1.01-1.07 倍): 换到这个模型上腿伸直了还够不着地,
# 落地层要把身体往下拉十几厘米 (到 -15 cm 上限就悬空), 腿部 IK 也一直停在奇点附近
STAND_HIPS = 0.985


def stand_hips(c: dict, a: int = 0, b: int | None = None) -> None:
    """把片段 c 的髋高按比例缩放, 让 [a, b) 帧的中位数等于 STAND_HIPS (原地修改)."""
    c["hips"][:, 1] *= STAND_HIPS / np.median(c["hips"][a:b, 1])


# -- 待机动作库 (StayStill, MIT) -------------------------------------------------------------
STAYSTILL = MOCAP / "staystill"
IDLE_PEOPLE, IDLE_SKIP_S, IDLE_TAKE_S = 12, 10, 60      # 每人跳过开头 10 秒 (起始姿势), 取 60 秒
IDLE_ACTIONS = {"wei_lr": "重心左换右", "wei_rl": "重心右换左", "l_aro": "环顾", "t_fac": "摸脸", "sc_hea": "挠头",
                "sc_arm": "挠胳膊", "l_wat": "看表", "st_arm": "伸懒腰"}
IDLE_PER_ACTION = 4


def smooth_rot(clip: dict, sigma: float) -> dict:
    """无标记点动捕有细小抖动: 旋转矩阵沿时间高斯平滑后重新正交化, 位置同样平滑."""
    T = len(clip["rot"])
    rot = smooth(clip["rot"].reshape(T, -1), sigma).reshape(clip["rot"].shape)
    u, _, vt = np.linalg.svd(rot)
    return {**clip, "rot": u @ vt, "pos": smooth(clip["pos"].reshape(T, -1), sigma).reshape(clip["pos"].shape),
            "end": smooth(clip["end"].reshape(T, -1), sigma).reshape(clip["end"].shape)}


def smooth_wrists(clip: dict, cutoff: float) -> dict:
    """CMU 手上只有两三个标记点, 手腕相对前臂的转动有 8 Hz 以上的抖动 (挥手片段占 13-35%, 人手甩不了这么快),
    播放时手每帧忽快忽慢 (动作冲击里最大的一块). 只给手腕的局部转动 (相对前臂) 做零相位低通, 手臂和身体原样.
    用 4 阶 Butterworth 而不用高斯: 截止陡, 挥手本身的 2-3 Hz 几乎不动 (高斯要压到同样的抖动会把它削掉 13%).
    手末端位置跟着新的手朝向重算."""
    from scipy.signal import butter, filtfilt
    rot, end = clip["rot"].copy(), clip["end"].copy()
    T = len(rot)
    bb, aa = butter(4, cutoff / (clip["fps"] / 2))
    for side in ("left", "right"):
        hand, fore, tip = B[f"{side}Hand"], B[f"{side}LowerArm"], ENDS.index(f"{side}HandEnd")
        local = np.transpose(rot[:, fore], (0, 2, 1)) @ rot[:, hand]
        low = filtfilt(bb, aa, local.reshape(T, 9), axis=0, padtype="odd", padlen=min(T - 1, int(1.5 * clip["fps"])))
        u, _, vt = np.linalg.svd(low.reshape(T, 3, 3))
        off = np.einsum("tji,tj->ti", rot[:, hand], end[:, tip] - clip["pos"][:, hand])  # 手末端在手骨坐标里的位置
        rot[:, hand] = rot[:, fore] @ (u @ vt)
        end[:, tip] = clip["pos"][:, hand] + np.einsum("tij,tj->ti", rot[:, hand], off)
    return {**clip, "rot": rot, "end": end}


def build_idle() -> dict | None:
    """待机库 models/motion/idle.{json,bin}: 几个人各一分钟的站立待机 + 几种待机小动作, 格式同动作库."""
    idle_files = sorted((STAYSTILL / "Idle").glob("*.bvh"))
    if not idle_files:
        return None
    items, groups = [], {"idle": [], "actions": {k: [] for k in IDLE_ACTIONS}}
    # LaFAN1 的零位不是 T 姿态: 用第一段待机里最安静的一帧定出整套骨架的对齐 (所有文件骨架相同)
    ref = load_bvh(idle_files[0], LAFAN_MAP, LAFAN_END)
    align, ref_h = reference_align(ref, still_frame(ref))

    def add(name, kind, path, a_s=None, b_s=None):
        raw = smooth_rot(load_bvh(path, LAFAN_MAP, LAFAN_END), 1.5)
        fps = raw["fps"]
        a = int((a_s or 0) * fps)
        b = min(int(b_s * fps), len(raw["rot"])) if b_s else len(raw["rot"])
        c = process(raw, a, b, align, ref_h)
        stand_hips(c)
        items.append((name, kind, c))
        return len(items) - 1

    for path in idle_files[:IDLE_PEOPLE]:
        groups["idle"].append(add(path.stem, "idle", path, IDLE_SKIP_S, IDLE_SKIP_S + IDLE_TAKE_S))
    for kind in IDLE_ACTIONS:
        for path in sorted((STAYSTILL / "Actions").glob(f"{kind}_*.bvh"))[:IDLE_PER_ACTION]:
            groups["actions"][kind].append(add(path.stem, f"ia_{kind}", path))
    clips, start = [], 0
    for name, kind, c in items:
        clips.append({"name": name, "kind": kind, "start": start, "end": start + c["frames"]})
        start += c["frames"]
    cat = lambda key: np.concatenate([c[key] for _, _, c in items])  # noqa: E731
    blobs = [("hips", cat("hips").astype(np.float32)), ("root", cat("root").astype(np.float32)),
             ("quats", np.round(np.clip(cat("quat"), -1, 1) * 32767).astype(np.int16)),
             ("contacts", cat("contacts").astype(np.uint8))]
    offsets, o = {}, 0
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "idle.bin").open("wb") as f:
        for name, arr in blobs:
            offsets[name] = o
            f.write(arr.tobytes())
            o += arr.nbytes
    meta = {"version": 1, "kind": "idle", "fps": FPS, "frames": start, "bones": BONES, "clips": clips,
            "groups": groups, "names": IDLE_ACTIONS, "offsets": offsets, "bytes": o,
            "canon": {n: CANON[n].round(6).tolist() for n in BONES}, "aim": AIM,
            "sources": {"staystill": "StayStill (MIT), Atxa Landa et al. 2026, LaFAN1 skeleton"}}
    (OUT / "idle.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


# -- 手势库 (CMU, 2026-10-24 补) ------------------------------------------------------------
# 种类 -> [(受试者, 片段, 起止秒, 哪只手)]. 每段左右镜像各一份, 镜像后手换边. 起点都在站定附近 (手垂下),
# 运行时从待机姿态惯性化过渡进来, 播完回到待机.
# 鞠躬没有收: 下载的片段里只有 141_26 的屈膝礼 (芭蕾谢幕式, 后撤一大步、髋部下沉 19 cm), 原地播放时
# 脚挪 50 cm、悬空 53 帧、重心偏出支撑面 76 帧, 比原来的做法 (HAST-SEC 深点头 / 程序动作弯腰) 差; 鞠躬仍走原来的方式.
GESTURE_CLIPS = {
    # Waving: 先左手挥 (前 2.9 秒, 手还举着, 回待机时放下), 再双手举过头挥. 141_16 (Wave Hello) 挥手时髋部左右晃了
    # 28 cm、脚也挪了, 原地播放时重心偏出支撑面 (执行评估里 100 多帧不稳), 没有用
    "wave": [(143, 25, (0.0, 2.9), "L"), (143, 25, (0.0, 5.4), "B")],
    "shrug": [(141, 21, (0.0, 2.3), None)],
    # 说话手势 (conversation - explain with hand gestures): 没有 HAST-SEC 时说话间隙插一段. 幅度偏大, 挑的是相对收敛的几段
    "talk": [(18, 8, (11.8, 13.7), None), (19, 8, (0.4, 2.6), None), (19, 8, (9.6, 12.1), None),
             (19, 8, (12.2, 14.2), None), (19, 8, (15.4, 17.3), None)],
}
GESTURE_MIRROR = {"R": "L", "L": "R", "B": "B", None: None}
GESTURE_PLANT = {"wave", "shrug", "talk"}  # 原地手势: 运行时两只脚 (位置和朝向) 踩住不动; 要挪步的手势不放进来
GESTURE_FPS = 60  # 手势快 (双手过头挥手 2 Hz): 30 帧线性插值时角速度每帧突变, 平滑层会当成跳变压掉
WRIST_CUTOFF = 4.0  # 手腕去抖的截止频率 (Hz, 见 smooth_wrists): 挥手 2.5 Hz 留 98%, 6 Hz 以上的抖动剩约 1/4; 0 = 不去抖


def build_gestures() -> dict:
    """手势库 models/motion/gestures.{json,bin}: 站着原地播放的整段动捕 (挥手/耸肩/说话手势), 格式同动作库.
    根部照常取平滑后的髋部投影: 髋部留在角色此刻的位置上方, 脚由运行时踩住待机时的落点 (见 GESTURE_PLANT)."""
    items, groups = [], {k: [] for k in GESTURE_CLIPS}
    for kind, clips in GESTURE_CLIPS.items():
        for s, t, (a, b), side in clips:
            raw = smooth_wrists(cmu_clip(s, t), WRIST_CUTOFF) if WRIST_CUTOFF > 0 else cmu_clip(s, t)
            f0, f1 = int(a * GESTURE_FPS), int(b * GESTURE_FPS)
            for m in (False, True):
                c = process(mirror(raw) if m else raw, fps=GESTURE_FPS)
                f1 = min(f1, c["frames"])
                stand_hips(c, f0, f1)
                items.append((f"cmu{s}_{t:02d}{'_M' if m else ''}_{kind}", kind, c, (f0, f1)))
                groups[kind].append({"clip": len(items) - 1, "side": GESTURE_MIRROR[side] if m else side,
                                     "plant": kind in GESTURE_PLANT, "source": f"cmu{s}_{t:02d}"})
    clips, start = [], 0
    for name, kind, _, (a, b) in items:
        clips.append({"name": name, "kind": kind, "start": start, "end": start + (b - a)})
        start += b - a
    cat = lambda key: np.concatenate([c[key][a:b] for _, _, c, (a, b) in items])  # noqa: E731
    blobs = [("hips", cat("hips").astype(np.float32)), ("root", cat("root").astype(np.float32)),
             ("quats", np.round(np.clip(cat("quat"), -1, 1) * 32767).astype(np.int16)),
             ("contacts", cat("contacts").astype(np.uint8))]
    offsets, o = {}, 0
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "gestures.bin").open("wb") as f:
        for name, arr in blobs:
            offsets[name] = o
            f.write(arr.tobytes())
            o += arr.nbytes
    meta = {"version": 1, "kind": "gestures", "fps": GESTURE_FPS, "frames": start, "bones": BONES, "clips": clips,
            "groups": groups, "offsets": offsets, "bytes": o, "canon": {n: CANON[n].round(6).tolist() for n in BONES},
            "aim": AIM, "sources": {"cmu": "subjects " + ", ".join(str(s) for s in sorted({s for v in GESTURE_CLIPS.values()
                                                                                            for s, *_ in v}))
                                    + " (mocap.cs.cmu.edu)"}}
    (OUT / "gestures.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def main() -> None:
    import sys
    if "--gestures-only" in sys.argv:
        meta = build_gestures()
        print(f"gestures   {meta['frames']:6d} 帧, {meta['bytes'] / 1e6:.1f} MB: "
              + ", ".join(f"{k} {len(v)}" for k, v in meta["groups"].items()))
        return
    if "--idle-only" in sys.argv:
        meta = build_idle()
        print("没有 StayStill 数据 (python tools/setup_models.py staystill)" if meta is None else
              f"idle       {meta['frames']:6d} 帧, {meta['bytes'] / 1e6:.1f} MB: 待机 {len(meta['groups']['idle'])} 段, "
              f"小动作 {sum(len(v) for v in meta['groups']['actions'].values())} 段")
        return
    if "--actions-only" not in sys.argv:
        cuts = frame_cuts()
        for key, (parts, cmu) in STYLES.items():
            meta = build(key, parts, cmu, cuts)
            print(f"{key:10s} {meta['frames']:6d} 帧 ({meta['frames'] / FPS / 60:.1f} 分钟), {meta['bytes'] / 1e6:.1f} MB, "
                  f"走 {meta['speeds']['FW']:.2f} 跑 {meta['speeds']['FR']:.2f} 髋高/秒")
    meta = build_actions()
    g = meta["groups"]
    print(f"actions    {meta['frames']:6d} 帧, {meta['bytes'] / 1e6:.1f} MB: 坐 {len(g['sit'])} 组, 捡 {len(g['pick'])}, "
          f"放 {len(g['put'])}, 伸手 {len(g['reach'])}")
    meta = build_idle()
    if meta:
        print(f"idle       {meta['frames']:6d} 帧, {meta['bytes'] / 1e6:.1f} MB")
    meta = build_gestures()
    print(f"gestures   {meta['frames']:6d} 帧, {meta['bytes'] / 1e6:.1f} MB: "
          + ", ".join(f"{k} {len(v)}" for k, v in meta["groups"].items()))


if __name__ == "__main__":
    main()

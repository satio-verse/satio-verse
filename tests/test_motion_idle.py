"""StayStill 待机库的构建: LaFAN1 骨架 BVH (没有 End Site) -> idle.{json,bin}. 用合成的小 BVH, 不依赖下载的数据."""
import importlib.util
import json

import numpy as np

from wm.config import ROOT

_spec = importlib.util.spec_from_file_location("build_motion_db", ROOT / "tools" / "build_motion_db.py")
bmd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bmd)

# (关节, 父, 偏移 cm)
LAFAN = [("Hips", None, (0, 95, 0)), ("LeftUpLeg", "Hips", (9, -5, 0)), ("LeftLeg", "LeftUpLeg", (0, -42, 0)),
         ("LeftFoot", "LeftLeg", (0, -42, 0)), ("LeftToe", "LeftFoot", (0, -5, 12)),
         ("RightUpLeg", "Hips", (-9, -5, 0)), ("RightLeg", "RightUpLeg", (0, -42, 0)),
         ("RightFoot", "RightLeg", (0, -42, 0)), ("RightToe", "RightFoot", (0, -5, 12)),
         ("Spine", "Hips", (0, 10, 0)), ("Spine1", "Spine", (0, 12, 0)), ("Spine2", "Spine1", (0, 12, 0)),
         ("Neck", "Spine2", (0, 14, 0)), ("Head", "Neck", (0, 10, 0)),
         ("LeftShoulder", "Spine2", (4, 10, 0)), ("LeftArm", "LeftShoulder", (12, 0, 0)),
         ("LeftForeArm", "LeftArm", (26, 0, 0)), ("LeftHand", "LeftForeArm", (24, 0, 0)),
         ("RightShoulder", "Spine2", (-4, 10, 0)), ("RightArm", "RightShoulder", (-12, 0, 0)),
         ("RightForeArm", "RightArm", (-26, 0, 0)), ("RightHand", "RightForeArm", (-24, 0, 0))]


def write_bvh(path, frames=90):
    kids = {n: [c for c, p, _ in LAFAN if p == n] for n, _, _ in LAFAN}
    lines = ["HIERARCHY"]

    def emit(name, depth):
        _, parent, off = next(j for j in LAFAN if j[0] == name)
        pad = "  " * depth
        lines.append(f"{pad}{'ROOT' if parent is None else 'JOINT'} {name}")
        lines.append(f"{pad}{{")
        lines.append(f"{pad}  OFFSET {off[0]} {off[1]} {off[2]}")
        ch = "6 Xposition Yposition Zposition Zrotation Yrotation Xrotation" if parent is None else "3 Zrotation Yrotation Xrotation"
        lines.append(f"{pad}  CHANNELS {ch}")
        for c in kids[name]:
            emit(c, depth + 1)
        lines.append(f"{pad}}}")  # 没有 End Site, 和 LaFAN1 一样

    emit("Hips", 0)
    t = np.arange(frames) / 30
    lines += ["MOTION", f"Frames: {frames}", "Frame Time: 0.0333333"]
    for k in range(frames):
        sway = 2.0 * np.sin(2 * np.pi * 0.3 * t[k])  # 轻微摇摆 (度), 髋部左右挪 1 cm
        row = [np.sin(2 * np.pi * 0.3 * t[k]), 95, 0, 0, 0, sway] + [0, 0, sway * 0.3] * (len(LAFAN) - 1)
        lines.append(" ".join(f"{v:.4f}" for v in row))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def test_lafan_bvh_without_end_sites(tmp_path):
    write_bvh(tmp_path / "a.bvh")
    clip = bmd.load_bvh(tmp_path / "a.bvh", bmd.LAFAN_MAP, bmd.LAFAN_END)
    assert clip["fps"] == 30 and clip["rot"].shape == (90, len(bmd.BONES), 3, 3)
    head_end = clip["rest_end"][bmd.ENDS.index("headEnd")]
    head = clip["rest_pos"][bmd.B["head"]]
    assert np.allclose(head_end - head, [0, 0.16, 0], atol=1e-6)  # 沿 颈->头 方向外推 16 cm
    toe_end = clip["rest_end"][bmd.ENDS.index("leftToesEnd")] - clip["rest_pos"][bmd.B["leftToes"]]
    assert toe_end[2] > 0.04 and toe_end[1] < 0  # 脚尖朝前下方


def test_build_idle_db(tmp_path, monkeypatch):
    data, out = tmp_path / "staystill", tmp_path / "motion"
    for k in range(3):
        write_bvh(data / "Idle" / f"idle_{k}.bvh", 120)
    write_bvh(data / "Actions" / "wei_lr_1_0.bvh", 60)
    write_bvh(data / "Actions" / "t_fac_0.bvh", 60)
    monkeypatch.setattr(bmd, "STAYSTILL", data)
    monkeypatch.setattr(bmd, "OUT", out)
    monkeypatch.setattr(bmd, "IDLE_SKIP_S", 0.5)
    monkeypatch.setattr(bmd, "IDLE_TAKE_S", 2.0)
    meta = bmd.build_idle()
    assert meta["kind"] == "idle" and len(meta["groups"]["idle"]) == 3
    assert meta["groups"]["actions"]["wei_lr"] and meta["groups"]["actions"]["t_fac"] and not meta["groups"]["actions"]["l_aro"]
    idle = [c for c in meta["clips"] if c["kind"] == "idle"]
    assert all(c["end"] - c["start"] == 60 for c in idle)  # 跳过 0.5 s, 取 2 s
    assert (out / "idle.bin").stat().st_size == meta["bytes"]
    hips = np.frombuffer((out / "idle.bin").read_bytes()[:meta["frames"] * 12], np.float32).reshape(-1, 3)
    for c in meta["clips"]:  # 每段的站立髋高都归一到 STAND_HIPS (源数据各人不同, 换到模型上会悬空)
        assert abs(np.median(hips[c["start"]:c["end"], 1]) - bmd.STAND_HIPS) < 1e-3
    assert json.loads((out / "idle.json").read_text(encoding="utf-8"))["frames"] == meta["frames"]
    assert bmd.build_idle.__doc__ and meta["sources"]["staystill"].startswith("StayStill (MIT)")


def test_build_gestures_db(tmp_path, monkeypatch):
    """手势库: 每段左右镜像各一份 (挥手的手换边), 原地手势标记踩住双脚, 站立髋高归一到 STAND_HIPS."""
    write_bvh(tmp_path / "g.bvh", 90)
    clip = bmd.load_bvh(tmp_path / "g.bvh", bmd.LAFAN_MAP, bmd.LAFAN_END)
    monkeypatch.setattr(bmd, "cmu_clip", lambda s, t: clip)
    monkeypatch.setattr(bmd, "OUT", tmp_path / "motion")
    monkeypatch.setattr(bmd, "GESTURE_FPS", 30)
    monkeypatch.setattr(bmd, "GESTURE_CLIPS", {"wave": [(1, 1, (0.0, 1.5), "L"), (1, 2, (0.5, 2.0), "B")],
                                               "talk": [(2, 1, (0.0, 1.0), None)]})
    meta = bmd.build_gestures()
    g = meta["groups"]
    assert [x["side"] for x in g["wave"]] == ["L", "R", "B", "B"]
    assert all(x["plant"] for x in g["wave"] + g["talk"]) and len(g["talk"]) == 2
    assert [c["end"] - c["start"] for c in meta["clips"]] == [45, 45, 45, 45, 30, 30]
    assert meta["kind"] == "gestures" and meta["fps"] == 30
    assert (tmp_path / "motion" / "gestures.bin").stat().st_size == meta["bytes"]
    hips = np.frombuffer((tmp_path / "motion" / "gestures.bin").read_bytes()[:meta["frames"] * 12], np.float32).reshape(-1, 3)
    first = hips[:45, 1]
    assert abs(np.median(first) - bmd.STAND_HIPS) < 1e-3



def test_body_points_for_stand_choice():
    """拿放片段记下膝盖、脚尖相对起点根坐标系的位置 (髋高单位), 运行时选站位时检查会不会插进家具."""
    T = 10
    w = {"yaw": np.zeros(T), "root3": np.zeros((T, 3)), "hip_h": 0.9,
         "knees": np.tile([[0.1, 0.45, 0.0], [-0.1, 0.45, 0.0]], (T, 1, 1)).astype(float),
         "toes": np.tile([[0.1, 0.02, 0.15], [-0.1, 0.02, 0.15]], (T, 1, 1)).astype(float)}
    w["knees"][5:, :, 2] = 0.3  # 后半段蹲下, 膝盖往前伸 30 cm
    b = bmd.body_points(w, 0, T, n=6)
    assert len(b["knee"]) == 12 and len(b["toe"]) == 12
    assert max(k[2] for k in b["knee"]) == round(0.3 / 0.9, 4)  # 往前 (+z) 的量按髋高换算
    assert b["knee"][0] == [round(0.1 / 0.9, 4), 0.5, 0.0]


def test_knee_forward_for_seat_choice():
    """坐着时靠后的膝盖在髋部前方多远 (髋高单位, 沿当时的朝向): 运行时按座位深度避开把腿收到座位下面的坐姿."""
    T, H = 8, 0.9
    hips = np.tile([[0.0, 0.5, 0.0]], (T, 1))
    knees = np.tile([[0.1, 0.48, 0.45], [-0.1, 0.48, 0.40]], (T, 1, 1)).astype(float)  # 一条腿往前伸, 一条收一点
    w = {"yaw": np.zeros(T), "hips": hips, "knees": knees, "hip_h": H}
    assert abs(bmd.knee_forward(w, 0, T) - round(0.40 / H, 3)) < 1e-9  # 取靠后的那个膝盖
    w["knees"][:, :, 2] = 0.2  # 坐凳子时把腿收到座位下面
    assert bmd.knee_forward(w, 0, T) < 0.3
    # 朝向转 90 度 (朝 +X) 时按朝向量, 不按世界 Z
    turned = {"yaw": np.full(T, np.pi / 2), "hips": hips,
              "knees": np.tile([[0.45, 0.48, 0.1], [0.40, 0.48, -0.1]], (T, 1, 1)).astype(float), "hip_h": H}
    assert abs(bmd.knee_forward(turned, 0, T) - round(0.40 / H, 3)) < 1e-9


def test_leg_angles_for_pick_choice():
    """拿放片段的髋屈、髋外展、膝屈最大值 (和运动学约束同一套量法): 运行时选片段时蛙式全蹲往后排."""
    T = 3
    hips = np.array([[0.1, 0.9, 0.0], [-0.1, 0.9, 0.0]])
    w = {"thighs": np.tile(hips, (T, 1, 1)), "chest": np.tile([0.0, 1.3, 0.0], (T, 1)),
         "knees": np.tile([[0.1, 0.5, 0.0], [-0.1, 0.5, 0.0]], (T, 1, 1)).astype(float),
         "feet": np.tile([[0.1, 0.1, 0.0], [-0.1, 0.1, 0.0]], (T, 1, 1)).astype(float)}
    a = bmd.leg_angles(w, 0, T)
    assert abs(a["hipFlex"]) < 1e-6 and abs(a["hipAbd"]) < 1e-6 and abs(a["knee"]) < 1e-6  # 站直
    # 第 2 帧: 左大腿往前抬平 (屈髋 90), 小腿垂下 (屈膝 90); 右大腿往外撇 30 度
    w["knees"][1, 0] = [0.1, 0.9, 0.4]
    w["feet"][1, 0] = [0.1, 0.5, 0.4]
    w["knees"][1, 1] = [-0.1 - 0.4 * np.sin(np.pi / 6), 0.9 - 0.4 * np.cos(np.pi / 6), 0.0]
    a = bmd.leg_angles(w, 0, T)
    assert abs(a["hipFlex"] - 90) < 0.2 and abs(a["knee"] - 90) < 0.2 and abs(a["hipAbd"] - 30) < 0.2, a


def test_smooth_wrists_keeps_arm_and_slow_wave():
    """手腕去抖: 只改手腕相对前臂的转动, 1.5 Hz 的甩腕留着, 10 Hz 的抖动去掉; 前臂原样, 手末端跟着新的手朝向."""
    T, fps = 240, 120
    t = np.arange(T) / fps

    def rz(a):
        c, s_ = np.cos(a), np.sin(a)
        m = np.zeros((len(a), 3, 3))
        m[:, 0, 0], m[:, 0, 1], m[:, 1, 0], m[:, 1, 1], m[:, 2, 2] = c, -s_, s_, c, 1
        return m

    B = bmd.B
    rot = np.tile(np.eye(3), (T, len(bmd.BONES), 1, 1))
    fore = rz(0.6 * np.sin(2 * np.pi * 1.0 * t))  # 前臂在摆
    slow = 0.5 * np.sin(2 * np.pi * 1.5 * t)       # 甩腕
    for side in ("left", "right"):
        rot[:, B[f"{side}LowerArm"]] = fore
        rot[:, B[f"{side}Hand"]] = fore @ rz(slow + 0.1 * np.sin(2 * np.pi * 10 * t))  # 加 10 Hz 抖动
    clip = {"fps": fps, "rot": rot, "pos": np.zeros((T, len(bmd.BONES), 3)), "end": np.zeros((T, len(bmd.ENDS), 3))}
    for side in ("left", "right"):
        clip["end"][:, bmd.ENDS.index(f"{side}HandEnd")] = np.einsum("tij,j->ti", rot[:, B[f"{side}Hand"]], [0.1, 0, 0])
    out = bmd.smooth_wrists(clip, 4.0)
    assert np.array_equal(out["rot"][:, B["leftLowerArm"]], fore) and np.array_equal(out["rot"][:, B["chest"]], rot[:, B["chest"]])
    local = np.transpose(fore, (0, 2, 1)) @ out["rot"][:, B["leftHand"]]
    angle = np.arctan2(local[:, 1, 0], local[:, 0, 0])
    assert np.abs(angle - slow)[30:-30].max() < 0.01  # 抖动 0.1 rad -> 剩不到 0.01, 甩腕不变
    tip = np.einsum("tij,j->ti", out["rot"][:, B["rightHand"]], [0.1, 0, 0])
    assert np.allclose(out["end"][:, bmd.ENDS.index("rightHandEnd")], tip)

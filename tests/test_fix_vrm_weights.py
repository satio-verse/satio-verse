"""VRM 肩部权重修复 (tools/fix_vrm_weights.py): 用一个手写的小 GLB 测, 不依赖真模型."""
import importlib.util
import json
import struct
import sys

import numpy as np

from wm.config import ROOT

sys.path.insert(0, str(ROOT / "tools"))
_spec = importlib.util.spec_from_file_location("fix_vrm_weights", ROOT / "tools" / "fix_vrm_weights.py")
fw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fw)


def strip_glb(path):
    """一条沿 x 的网格带 (2 x 12 个顶点): 左半边全绑胸部, 右半边全绑上臂 (过渡最陡); 第 0 列另外拆出一份 (UV 接缝).
    关节: Chest 在原点, UpperArm.R 在 x = 0.1."""
    xs = np.linspace(0, 0.33, 12)
    pos = [[x, y, 0] for y in (0, 0.03) for x in xs] + [[0, 0, 0]]  # 最后一个和第 0 个同一位置
    pos = np.array(pos, np.float32)
    n = len(pos)
    joints = np.zeros((n, 4), np.uint8)
    w = np.zeros((n, 4), np.float32)
    for i, (x, _, _) in enumerate(pos):
        if x < 0.16:
            joints[i, 0], w[i, 0] = 0, 1
        else:
            joints[i, 0], w[i, 0] = 1, 1
    tri = []
    for i in range(11):
        a, b, c, d = i, i + 1, 12 + i, 13 + i
        tri += [a, b, c, b, d, c]
    tri += [24, 1, 12]  # 拆出来的那份也连上
    idx = np.array(tri, np.uint16)
    ibm = np.stack([np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32)])
    ibm[1][:3, 3] = [-0.1, 0, 0]
    ibm = ibm.transpose(0, 2, 1)  # 按列存
    blobs = [pos.tobytes(), joints.tobytes(), w.tobytes(), idx.tobytes() + b"\0\0", ibm.tobytes()]
    views, off = [], 0
    for b in blobs:
        views.append({"buffer": 0, "byteOffset": off, "byteLength": len(b)})
        off += len(b)
    gltf = {
        "asset": {"version": "2.0"}, "buffers": [{"byteLength": off}], "bufferViews": views,
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": n, "type": "VEC3"},
            {"bufferView": 1, "componentType": 5121, "count": n, "type": "VEC4"},
            {"bufferView": 2, "componentType": 5126, "count": n, "type": "VEC4"},
            {"bufferView": 3, "componentType": 5123, "count": len(idx), "type": "SCALAR"},
            {"bufferView": 4, "componentType": 5126, "count": 2, "type": "MAT4"}],
        "meshes": [{"name": "Outfit", "primitives": [{"attributes": {"POSITION": 0, "JOINTS_0": 1, "WEIGHTS_0": 2}, "indices": 3}]}],
        "skins": [{"joints": [1, 2], "inverseBindMatrices": 4}],
        "nodes": [{"name": "Outfit", "mesh": 0, "skin": 0}, {"name": "Chest"}, {"name": "UpperArm.R", "translation": [0.1, 0, 0]}],
    }
    js = json.dumps(gltf).encode()
    js += b" " * (-len(js) % 4)
    binb = b"".join(blobs)
    binb += b"\0" * (-len(binb) % 4)
    data = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(binb))
    data += struct.pack("<II", len(js), 0x4E4F534A) + js + struct.pack("<II", len(binb), 0x004E4942) + binb
    path.write_bytes(data)
    return pos, w


def read_weights(path):
    gltf, buf, bin0 = fw.read_glb(path)
    return fw.Accessor(gltf, buf, bin0, 1).read(), fw.Accessor(gltf, buf, bin0, 2).floats()


def test_smooths_only_near_the_arm_and_keeps_the_rest(tmp_path, monkeypatch):
    src, out = tmp_path / "m.vrm", tmp_path / "out.vrm"
    pos, w0 = strip_glb(src)
    monkeypatch.setattr(sys, "argv", ["fix", str(src), "--mesh", "Outfit", "--out", str(out), "--radius", "0.12"])
    fw.main()
    assert out.stat().st_size == src.stat().st_size, "原地改, 文件大小不变"
    J, W = read_weights(out)
    assert np.allclose(W.sum(1), 1, atol=1e-5)
    # 只绑胸部的顶点 (区域外, 当边界) 一点不变
    chest_only = pos[:, 0] < 0.16
    assert np.array_equal(W[chest_only], w0[chest_only])
    # 区域里 (带上臂权重、离上臂关节 0.12 m 以内) 靠近胸部那侧的顶点分到了胸部权重: 过渡变缓
    seam = np.isclose(pos[:, 0], 0.18, atol=0.001)
    chest_w = np.where(J == 0, W, 0).sum(1)
    assert (chest_w[seam] > 0.15).all(), chest_w[seam]
    # 离上臂关节太远的不动
    far = pos[:, 0] > 0.25
    assert np.array_equal(W[far], w0[far])


def test_unchanged_when_no_matching_joints(tmp_path, monkeypatch):
    src, out = tmp_path / "m.vrm", tmp_path / "out.vrm"
    strip_glb(src)
    monkeypatch.setattr(sys, "argv", ["fix", str(src), "--mesh", "Outfit", "--out", str(out), "--weight", "^Nothing$"])
    fw.main()
    assert out.read_bytes() == src.read_bytes()

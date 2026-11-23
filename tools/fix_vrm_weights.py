"""修 VRM 模型肩部、腋下的蒙皮权重: 自动生成的权重在这一圈过渡太陡 (相邻顶点的上臂权重一个 0.44、一个 0.11),
袖子、腋下的边直接连到胸口的顶点, 手臂一抬这些边就被拉长 8-12 倍, 看上去胳膊和袖子从身体上脱开了.

做法: 只改 GLB 里指定网格的 JOINTS_0 / WEIGHTS_0 (原地写回, 文件大小和其余内容不变):
  1. 区域: 带上臂或肩部权重、离肩关节不远的顶点 (按绑定姿势量). 同一位置拆开的顶点 (UV 接缝) 焊在一起算邻接.
  2. 在区域里对权重做拉普拉斯平滑 (区域外的顶点不动, 当边界条件), 让上臂 / 肩部 / 胸部的权重平滑过渡.
  3. 每个顶点留权重最大的 4 个, 归一化.

  python tools/fix_vrm_weights.py models/vrm/silvercat.vrm --mesh Outfit [--out 新文件] [--iters 30] [--radius 0.2]
  不给 --out 时先把原文件备份到 models/vrm/_archive/<名字>.orig.vrm (已有备份就不覆盖), 再原地改.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
COMP = {5121: np.uint8, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def read_glb(path: Path) -> tuple[dict, bytearray, int]:
    data = path.read_bytes()
    magic, _, _ = struct.unpack_from("<III", data, 0)
    assert magic == 0x46546C67, "不是 GLB"
    jlen, jtype = struct.unpack_from("<II", data, 12)
    assert jtype == 0x4E4F534A
    gltf = json.loads(data[20:20 + jlen])
    off = 20 + jlen
    blen, btype = struct.unpack_from("<II", data, off)
    assert btype == 0x004E4942
    return gltf, bytearray(data), off + 8  # BIN 块的数据从 off + 8 开始


class Accessor:
    """一个 accessor 在 BIN 里的视图 (支持 byteStride 交错), 可读可原地写."""

    def __init__(self, gltf: dict, buf: bytearray, bin0: int, idx: int):
        a = gltf["accessors"][idx]
        bv = gltf["bufferViews"][a["bufferView"]]
        self.dtype = np.dtype(COMP[a["componentType"]])
        self.n, self.k = a["count"], NCOMP[a["type"]]
        self.normalized = a.get("normalized", False)
        self.start = bin0 + bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        self.stride = bv.get("byteStride") or self.dtype.itemsize * self.k
        self.buf = buf

    def read(self) -> np.ndarray:
        out = np.empty((self.n, self.k), self.dtype)
        size = self.dtype.itemsize * self.k
        for i in range(self.n):
            p = self.start + i * self.stride
            out[i] = np.frombuffer(self.buf, self.dtype, self.k, p)
        return out

    def write(self, arr: np.ndarray) -> None:
        arr = np.ascontiguousarray(arr.astype(self.dtype))
        size = self.dtype.itemsize * self.k
        for i in range(self.n):
            p = self.start + i * self.stride
            self.buf[p:p + size] = arr[i].tobytes()

    def floats(self) -> np.ndarray:
        x = self.read().astype(np.float64)
        if self.normalized and self.dtype.kind == "u":
            x /= np.iinfo(self.dtype).max
        return x

    def write_floats(self, x: np.ndarray, rows: np.ndarray) -> None:
        """只写 rows 这些行 (其余原样不动, 量化的权重不会因为重新取整而变)."""
        old = self.read()
        if self.normalized and self.dtype.kind == "u":
            m = np.iinfo(self.dtype).max
            q = np.floor(x[rows] * m + 0.5)
            # 量化后每行的和补回 m (误差加到最大的那一项上)
            q[np.arange(len(q)), q.argmax(1)] += m - q.sum(1)
            old[rows] = q
        else:
            old[rows] = x[rows]
        self.write(old)


def joint_names(gltf: dict, skin: dict) -> list[str]:
    return [gltf["nodes"][j].get("name", f"node{j}") for j in skin["joints"]]


def bind_positions(acc: Accessor) -> np.ndarray:
    """每个关节绑定时的位置 (逆绑定矩阵求逆后的平移; glTF 矩阵按列存)."""
    m = acc.floats().reshape(-1, 4, 4).transpose(0, 2, 1)
    return np.linalg.inv(m)[:, :3, 3]


def weld(pos: np.ndarray, tol: float = 1e-5) -> np.ndarray:
    """同一位置的顶点 (UV 接缝拆开的) 编成同一组, 返回每个顶点的组号."""
    keys = np.round(pos / tol).astype(np.int64)
    _, inv = np.unique(keys, axis=0, return_inverse=True)
    return inv.ravel()


def fix_primitive(gltf, buf, bin0, prim, skin, args, rep) -> None:
    names = joint_names(gltf, skin)
    jpos = bind_positions(Accessor(gltf, buf, bin0, skin["inverseBindMatrices"]))
    pos = Accessor(gltf, buf, bin0, prim["attributes"]["POSITION"]).floats()
    jacc = Accessor(gltf, buf, bin0, prim["attributes"]["JOINTS_0"])
    wacc = Accessor(gltf, buf, bin0, prim["attributes"]["WEIGHTS_0"])
    J, W = jacc.read().astype(np.int64), wacc.floats()
    idx = Accessor(gltf, buf, bin0, prim["indices"]).read().ravel().astype(np.int64)
    n = len(pos)
    # 稠密权重矩阵 (只放这个图元用到的关节)
    used = np.unique(J[W > 0])
    col = {j: c for c, j in enumerate(used)}
    D = np.zeros((n, len(used)))
    for k in range(4):
        m = W[:, k] > 0
        D[np.nonzero(m)[0], [col[j] for j in J[m, k]]] += W[m, k]
    # 区域: 带 weight 组骨骼的权重, 离 center 组里最近的关节 radius 以内 (默认: 上臂 / 肩部权重, 离上臂关节 0.2 m;
    # 裙腰: --weight "^Skirt_\d+_01$" --center 同上). 名字里的点、下划线不算 (UpperArm.R / UpperArm_R / UpperArmR 都认)
    key = [nm.replace(".", "").replace("_", "") for nm in names]
    wj = [j for j in used if re.search(args.weight, key[j], re.I)]
    cj = [j for j in range(len(names)) if re.search(args.center, key[j], re.I)]
    region = np.zeros(n, bool)
    if wj and cj:
        near = np.min(np.linalg.norm(pos[:, None, :] - jpos[cj][None], axis=2), axis=1) < args.radius
        region = near & (D[:, [col[j] for j in wj]].sum(1) > 0.02)
    # 邻接 (焊接后): 每条边两端的组互相是邻居
    g = weld(pos)
    ng = g.max() + 1
    tri = idx.reshape(-1, 3)
    e = np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]])
    ge = np.unique(np.sort(g[e], axis=1), axis=0)
    ge = ge[ge[:, 0] != ge[:, 1]]
    # 组的权重 = 组内顶点平均 (接缝两侧本来就该一样)
    cnt = np.bincount(g, minlength=ng).astype(float)
    G = np.zeros((ng, len(used)))
    np.add.at(G, g, D)
    G /= cnt[:, None]
    greg = np.zeros(ng, bool)
    greg[g[region]] = True
    deg = np.bincount(ge.ravel(), minlength=ng).astype(float)
    for _ in range(args.iters):
        S = np.zeros_like(G)
        np.add.at(S, ge[:, 0], G[ge[:, 1]])
        np.add.at(S, ge[:, 1], G[ge[:, 0]])
        avg = S / np.maximum(deg, 1)[:, None]
        G[greg] = (1 - args.lam) * G[greg] + args.lam * avg[greg]
    newD = D.copy()
    newD[region] = G[g[region]]
    # 留最大的 4 个, 归一化 (用到的关节不到 4 个时补零列)
    if newD.shape[1] < 4:
        pad = 4 - newD.shape[1]
        newD = np.hstack([newD, np.zeros((n, pad))])
        used = np.concatenate([used, np.zeros(pad, used.dtype)])
    top = np.argsort(-newD, axis=1)[:, :4]
    tw = np.take_along_axis(newD, top, 1)
    tw[tw < 1e-4] = 0
    tw /= tw.sum(1, keepdims=True)
    newJ = used[top]
    newJ[tw == 0] = 0
    newJ[~region] = J[~region]
    jacc.write(newJ)
    wacc.write_floats(tw, region)
    rep.append({"verts": int(n), "region": int(region.sum()), "joints": [names[j] for j in used]})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vrm")
    ap.add_argument("--mesh", action="append", required=True, help="要改的网格名 (可以给多次)")
    ap.add_argument("--out", help="输出文件; 不给就备份后原地改")
    ap.add_argument("--iters", type=int, default=30)
    ap.add_argument("--lam", type=float, default=0.5)
    ap.add_argument("--radius", type=float, default=0.2, help="离 center 关节多远以内的顶点参与平滑 (米, 绑定姿势)")
    ap.add_argument("--weight", default=r"^(UpperArm|Shoulder)[LR]$", help="带这些骨骼权重的顶点参与 (正则, 名字去掉点和下划线后匹配)")
    ap.add_argument("--center", default=r"^UpperArm[LR]$", help="按离这些关节的距离划区域 (正则)")
    args = ap.parse_args()
    src = Path(args.vrm)
    gltf, buf, bin0 = read_glb(src)
    meshes = {m.get("name"): i for i, m in enumerate(gltf["meshes"])}
    rep = []
    for name in args.mesh:
        nodes = [nd for nd in gltf["nodes"] if nd.get("name") == name or (nd.get("mesh") is not None and gltf["meshes"][nd["mesh"]].get("name") == name)]
        nodes = [nd for nd in nodes if "mesh" in nd and "skin" in nd]
        if not nodes:
            raise SystemExit(f"找不到带蒙皮的网格 {name} (有: {sorted(k for k in meshes if k)})")
        for nd in nodes:
            for prim in gltf["meshes"][nd["mesh"]]["primitives"]:
                fix_primitive(gltf, buf, bin0, prim, gltf["skins"][nd["skin"]], args, rep)
    if args.out:
        out = Path(args.out)
    else:
        bak = ROOT / "models" / "vrm" / "_archive" / f"{src.stem}.orig.vrm"
        if not bak.exists():
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, bak)
            print(f"备份: {bak}")
        out = src
    out.write_bytes(bytes(buf))
    for r in rep:
        print(f"{r['verts']} 个顶点, 平滑了 {r['region']} 个; 关节 {', '.join(r['joints'])}")
    print(f"写入 {out}")


if __name__ == "__main__":
    main()

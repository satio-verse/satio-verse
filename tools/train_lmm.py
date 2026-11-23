"""R1a: 把中性风格的运动匹配库和待机库蒸馏成 Learned Motion Matching 式的小网络 (docs/roadmap/motion-model.md).

  runtime/train-venv/Scripts/python.exe tools/train_lmm.py [--hidden 256] [--scale 1.0] [--only neutral|idle] [--tag 名字]
      [--proj-hidden 384] [--proj-iters 150000] [--swap 0.7] [--pose-w 1.0] [--key 4] [--resume 检查点.pt]

思路照 Holden 等人的 Learned Motion Matching (SIGGRAPH 2020) 和 orangeduck/Motion-Matching (MIT) 的公开实现, 代码是重新写的;
数据只用我们自己的动作库 (models/motion/neutral.*、idle.*, 来自 100STYLE / CMU / StayStill, 可商用), 不碰 HAST-SEC.
  中性库 (运动匹配): 压缩器 C (姿态 -> 潜变量 z) 和解压器 D ([匹配特征 x, z] -> 姿态) 一起训; 步进器 S ([x, z] -> 下一帧的增量)
    按 20 帧窗口展开训; 投影器 P (查询 -> 最近的 [x, z]), 目标是加噪查询在库里的最近邻. 运行时只带 D、S、P.
  待机库 (按片段编号播放, 不搜索): 每 K 帧存一个 z (关键帧), 中间线性插值, 解压器只吃 z; 训练时解压器直接学插值出来的 z,
    所以随便取哪一帧都是一次解压, 没有步进器的漂移.
姿态: 22 根骨骼的旋转 (相对根朝向, 见 build_motion_db.py) 用 6D 表示 (旋转矩阵前两列), 加髋部位置、根部运动、触地.
输出: models/motion/lmmneutral.bin、lmmidle.bin (一个文件: 4 字节头长 + JSON 头 + 16 位权重等), web/lmm.js 读.
    文件名只用小写字母、不配 .json: 服务端 /motiondb/ 只认这种名字, 而且有同名 .json 的 .bin 会被当成一种风格.
训练记录和离线误差: logs/motion-model/r1a-train-<标签>.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

ROOT = Path(__file__).resolve().parent.parent
MOTION = ROOT / "models" / "motion"
LOGS = ROOT / "logs" / "motion-model"
DEV = torch.device("cuda" if torch.cuda.is_available() else "cpu")
NZ = 32           # 潜变量维数
WINDOW = 20       # 步进器展开的帧数
KEY = 4           # 待机关键帧间隔 (帧; --key 改)
ARGS = None       # 命令行参数 (main 里设)
# 手臂约束 (--arm-w / --arm-in) 看的骨骼; 左边的往 +X 是往外, 右边的往 -X 是往外
ARM_BONES = ["leftUpperArm", "leftLowerArm", "leftHand", "rightUpperArm", "rightLowerArm", "rightHand"]


# -- 数据 --------------------------------------------------------------------------------------
def load_db(name: str) -> dict:
    meta = json.loads((MOTION / f"{name}.json").read_text(encoding="utf-8"))
    raw = (MOTION / f"{name}.bin").read_bytes()
    F_, B, off = meta["frames"], len(meta["bones"]), meta["offsets"]

    def arr(key, dt, n):
        return np.frombuffer(raw, dtype=dt, count=F_ * n, offset=off[key]).reshape(F_, n)
    db = {"meta": meta, "F": F_, "B": B, "hips": arr("hips", np.float32, 3).copy(), "root": arr("root", np.float32, 3).copy(),
          "quats": arr("quats", np.int16, B * 4).reshape(F_, B, 4).astype(np.float32) / 32767,
          "contacts": arr("contacts", np.uint8, 2).astype(np.float32),
          "sha": {f"{name}.json": hashlib.sha256((MOTION / f"{name}.json").read_bytes()).hexdigest()[:16],
                  f"{name}.bin": hashlib.sha256(raw).hexdigest()[:16]}}
    if "features" in off:
        db["feats"] = arr("features", np.float32, 27).copy()
    clip_of = np.zeros(F_, np.int64)
    for i, c in enumerate(meta["clips"]):
        clip_of[c["start"]:c["end"]] = i
    db["clip_of"] = clip_of
    return db


def quat_to_mat(q: np.ndarray) -> np.ndarray:
    """[..., 4] (x, y, z, w) -> [..., 3, 3]."""
    x, y, z, w = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    n = np.sqrt(x * x + y * y + z * z + w * w) + 1e-9
    x, y, z, w = x / n, y / n, z / n, w / n
    m = np.empty(q.shape[:-1] + (3, 3), np.float32)
    m[..., 0, 0] = 1 - 2 * (y * y + z * z); m[..., 0, 1] = 2 * (x * y - z * w); m[..., 0, 2] = 2 * (x * z + y * w)
    m[..., 1, 0] = 2 * (x * y + z * w); m[..., 1, 1] = 1 - 2 * (x * x + z * z); m[..., 1, 2] = 2 * (y * z - x * w)
    m[..., 2, 0] = 2 * (x * z - y * w); m[..., 2, 1] = 2 * (y * z + x * w); m[..., 2, 2] = 1 - 2 * (x * x + y * y)
    return m


def six_to_mat(s: torch.Tensor) -> torch.Tensor:
    """[..., 6] (旋转矩阵第 0、1 列) -> [..., 3, 3], Gram-Schmidt."""
    a1, a2 = s[..., 0:3], s[..., 3:6]
    b1 = F.normalize(a1, dim=-1)
    b2 = F.normalize(a2 - (b1 * a2).sum(-1, keepdim=True) * b1, dim=-1)
    b3 = torch.cross(b1, b2, dim=-1)
    return torch.stack([b1, b2, b3], -1)


def pose_arrays(db: dict) -> dict:
    """姿态向量: 6D 旋转 (B*6)、髋部 (3)、根部运动 (3); 速度 (同一片段里下一帧减这一帧, 乘帧率); 触地."""
    m = quat_to_mat(db["quats"])                                  # F, B, 3, 3
    six = np.concatenate([m[..., :, 0], m[..., :, 1]], -1).reshape(db["F"], -1)  # F, B*6
    y = np.concatenate([six, db["hips"], db["root"]], 1).astype(np.float32)        # F, B*6+6
    nxt = np.arange(db["F"]) + 1
    last = np.zeros(db["F"], bool)
    for c in db["meta"]["clips"]:
        last[c["end"] - 1] = True
    nxt[last] -= 2                                                  # 片段最后一帧用前一帧的差
    fps = db["meta"]["fps"]
    vel = (y[nxt] - y) * fps * np.where(last, -1, 1)[:, None]
    return {"y": y, "vel": vel.astype(np.float32), "contacts": db["contacts"], "next": nxt, "last": last}


def stats(a: np.ndarray, floor: float = 1e-3) -> tuple[np.ndarray, np.ndarray]:
    return a.mean(0).astype(np.float32), np.maximum(a.std(0), floor).astype(np.float32)


# -- 网络 --------------------------------------------------------------------------------------
class MLP(nn.Module):
    def __init__(self, nin: int, hidden: list[int], nout: int, act: str):
        super().__init__()
        dims = [nin] + hidden + [nout]
        self.layers = nn.ModuleList(nn.Linear(a, b) for a, b in zip(dims[:-1], dims[1:]))
        self.act = act

    def forward(self, x):
        f = F.elu if self.act == "elu" else F.relu
        for i, l in enumerate(self.layers):
            x = l(x)
            if i < len(self.layers) - 1:
                x = f(x)
        return x


def adam(params, lr=1e-3):
    return torch.optim.AdamW(params, lr=lr, amsgrad=True, weight_decay=1e-3)


def to(a):
    return torch.as_tensor(a, device=DEV)


class Pose:
    """解压器输出 (标准化的 6D、髋、根; 触地的 logit) 和真值之间的损失. canon: 每根骨骼的标准方向, 用来量骨骼指向."""

    def __init__(self, B: int, y_mean, y_std, canon, contacts: bool):
        self.B, self.contacts = B, contacts
        self.mean, self.std, self.canon = to(y_mean), to(y_std), to(canon)
        self.ny = B * 6 + 6

    def dirs(self, y_norm):
        y = y_norm[..., :self.ny] * self.std + self.mean
        six = y[..., :self.B * 6].reshape(*y.shape[:-1], self.B, 6)
        return (six_to_mat(six) @ self.canon[..., None])[..., 0]   # ..., B, 3

    def loss(self, out, y_norm, c_true=None):
        l_lin = (out[..., :self.ny] - y_norm).abs().mean()
        l_dir = (self.dirs(out) - self.dirs(y_norm)).abs().mean()
        l_c = F.binary_cross_entropy_with_logits(out[..., self.ny:self.ny + 2], c_true) if self.contacts else out.new_zeros(())
        return l_lin, l_dir, l_c


# -- 中性库 ------------------------------------------------------------------------------------
def train_neutral(hidden: int, scale: float, log: dict) -> dict:
    db = load_db("neutral")
    meta, B = db["meta"], db["B"]
    P = pose_arrays(db)
    X = db["feats"]
    canon = np.array([meta["canon"][n] for n in meta["bones"]], np.float32)
    y_mean, y_std = stats(P["y"])
    c_in = np.concatenate([P["y"], P["vel"]], 1)
    ci_mean, ci_std = stats(c_in)
    Yn = to((P["y"] - y_mean) / y_std)
    Cin = to((c_in - ci_mean) / ci_std)
    Xt, Ct, NXT = to(X), to(P["contacts"]), to(P["next"])
    pose = Pose(B, y_mean, y_std, canon, True)
    N = db["F"]
    H = [hidden, hidden]
    C = MLP(Cin.shape[1], [512, 512], NZ, "elu").to(DEV)         # 压缩器只在训练时用, 宽一点无妨
    D = MLP(27 + NZ, H, pose.ny + 2, "elu").to(DEV)
    # --resume: 接着上一次的检查点, 压缩器、解压器、步进器不重训 (只重训投影器)
    ck = torch.load(ARGS.resume, map_location=DEV) if ARGS.resume else None
    if ck:
        C.load_state_dict(ck["C"]); D.load_state_dict(ck["D"])
        log["resumed"] = ARGS.resume
    opt = adam(list(C.parameters()) + list(D.parameters()))
    sched = torch.optim.lr_scheduler.ExponentialLR(opt, 0.99)
    iters, bs = 0 if ck else int(60000 * scale), 512
    t0, hist = time.time(), []
    for it in range(iters):
        i = torch.randint(0, N, (bs,), device=DEV)
        j = NXT[i]
        z = C(torch.cat([Cin[i], Cin[j]]))
        zi, zj = z[:bs], z[bs:]
        oi, oj = D(torch.cat([Xt[i], zi], 1)), D(torch.cat([Xt[j], zj], 1))
        l_lin, l_dir, l_c = pose.loss(oi, Yn[i], Ct[i])
        l_lin2, l_dir2, _ = pose.loss(oj, Yn[j], Ct[j])
        l_vel = ((oj[:, :pose.ny] - oi[:, :pose.ny]) - (Yn[j] - Yn[i])).abs().mean()
        l_z = 1e-3 * (z ** 2).mean() + 0.1 * (zj - zi).abs().mean()  # 潜变量小一点、逐帧变化平滑 (步进器好学)
        loss = l_lin + l_lin2 + 2 * (l_dir + l_dir2) + 4 * l_vel + 0.2 * l_c + l_z
        opt.zero_grad(); loss.backward(); opt.step()
        if it % 1000 == 999:
            sched.step()
        if it % 5000 == 0 or it == iters - 1:
            hist.append({"it": it, "lin": round(l_lin.item(), 4), "dir": round(l_dir.item(), 4), "vel": round(l_vel.item(), 4),
                         "contact": round(l_c.item(), 4), "z": round(l_z.item(), 4), "s": round(time.time() - t0, 1)})
            print("CD", hist[-1], flush=True)
    log["neutral_cd"] = hist
    C.eval(); D.eval()
    with torch.no_grad():
        Z = torch.cat([C(Cin[k:k + 8192]) for k in range(0, N, 8192)])
    z_mean, z_std = Z.mean(0), Z.std(0).clamp_min(1e-3)
    Zn = (Z - z_mean) / z_std
    # -- 步进器: [x, z] -> 下一帧的增量 (按增量的均值、方差标准化) --
    XZ = torch.cat([Xt, Zn], 1)
    starts = torch.tensor([f for c in meta["clips"] for f in range(c["start"], c["end"] - WINDOW)], device=DEV)
    dXZ = XZ[NXT[starts]] - XZ[starts]
    d_mean, d_std = dXZ.mean(0), dXZ.std(0).clamp_min(1e-4)
    S = MLP(27 + NZ, H, 27 + NZ, "relu").to(DEV)
    if ck:
        S.load_state_dict(ck["S"])
    opt = adam(S.parameters())
    sched = torch.optim.lr_scheduler.ExponentialLR(opt, 0.99)
    iters, bs = 0 if ck else int(60000 * scale), 64
    w = torch.cat([torch.full((27,), 2.0), torch.full((NZ,), 7.5)]).to(DEV)
    wv = torch.cat([torch.full((27,), 0.2), torch.full((NZ,), 0.5)]).to(DEV)
    hist, t0 = [], time.time()
    for it in range(iters):
        s0 = starts[torch.randint(0, len(starts), (bs,), device=DEV)]
        idx = s0[:, None] + torch.arange(WINDOW, device=DEV)[None]   # 窗口在同一片段里, 连续帧号就是下一帧
        gt = XZ[idx]                                                   # bs, W, 59
        cur, outs = gt[:, 0], [gt[:, 0]]
        for _ in range(WINDOW - 1):
            cur = cur + S(cur) * d_std + d_mean
            outs.append(cur)
        pred = torch.stack(outs, 1)
        l_val = ((pred - gt).abs() * w).mean()
        l_vel = (((pred[:, 1:] - pred[:, :-1]) - (gt[:, 1:] - gt[:, :-1])).abs() * wv).mean() * 30
        loss = l_val + l_vel
        opt.zero_grad(); loss.backward(); opt.step()
        if it % 1000 == 999:
            sched.step()
        if it % 5000 == 0 or it == iters - 1:
            hist.append({"it": it, "val": round(l_val.item(), 4), "vel": round(l_vel.item(), 4), "s": round(time.time() - t0, 1)})
            print("S", hist[-1], flush=True)
    log["neutral_stepper"] = hist
    S.eval()
    D.requires_grad_(False)
    if not ck:
        torch.save({"C": C.state_dict(), "D": D.state_dict(), "S": S.state_dict()}, ARGS.ckpt)
    # 手臂: 指向误差额外加权; 往身体收 (内收) 的那一侧另外罚. 投影器、步进器的手臂误差 (3-7 度) 没有往里收的系统偏差,
    # 但动漫模型胯宽, 手垂在旁边时往里偏几度就穿模 (接触层推开), 往外偏没事, 所以要让误差宁可往外
    arm_b = torch.tensor([meta["bones"].index(n) for n in ARM_BONES], device=DEV)
    arm_s = torch.tensor([1.0 if n.startswith("left") else -1.0 for n in ARM_BONES], device=DEV)

    def arm_terms(o, y_norm):
        dp, dt = pose.dirs(o)[..., arm_b, :], pose.dirs(y_norm)[..., arm_b, :]
        return (dp - dt).abs().mean(), torch.relu(-(dp[..., 0] - dt[..., 0]) * arm_s).mean()

    def arm_report(o, y_norm):
        dp, dt = pose.dirs(o)[..., arm_b, :], pose.dirs(y_norm)[..., arm_b, :]
        lat = (dp[..., 0] - dt[..., 0]) * arm_s
        return {"arm_deg": round(torch.rad2deg(torch.acos((dp * dt).sum(-1).clamp(-1, 1))).mean().item(), 3),
                "lat_bias_x100": round(lat.mean().item() * 100, 3), "medial_x100": round(torch.relu(-lat).mean().item() * 100, 3)}
    # -- 步进器微调 (--stepper-ft 步): 展开的每 4 帧解压出来, 加姿态和手臂的损失 --
    if ARGS.stepper_ft > 0:
        S.train()
        opt = adam(S.parameters(), lr=3e-4)
        hist, t0 = [], time.time()
        for it in range(int(ARGS.stepper_ft * scale)):
            s0 = starts[torch.randint(0, len(starts), (64,), device=DEV)]
            idx = s0[:, None] + torch.arange(WINDOW, device=DEV)[None]
            gt = XZ[idx]
            cur, outs = gt[:, 0], [gt[:, 0]]
            for _ in range(WINDOW - 1):
                cur = cur + S(cur) * d_std + d_mean
                outs.append(cur)
            pred = torch.stack(outs, 1)
            l_val = ((pred - gt).abs() * w).mean()
            l_vel = (((pred[:, 1:] - pred[:, :-1]) - (gt[:, 1:] - gt[:, :-1])).abs() * wv).mean() * 30
            sel, gi = pred[:, 3::4], idx[:, 3::4]
            o = D(torch.cat([sel[..., :27], sel[..., 27:] * z_std + z_mean], -1))
            p_lin, p_dir, p_c = pose.loss(o, Yn[gi], Ct[gi])
            a_sym, a_in = arm_terms(o, Yn[gi])
            loss = l_val + l_vel + ARGS.pose_w * (p_lin + 2 * p_dir + 0.2 * p_c) + ARGS.arm_w * a_sym + ARGS.arm_in * a_in
            opt.zero_grad(); loss.backward(); opt.step()
            if it % 5000 == 0 or it == int(ARGS.stepper_ft * scale) - 1:
                hist.append({"it": it, "val": round(l_val.item(), 4), "dir": round(p_dir.item(), 4), "arm": round(a_sym.item(), 4),
                             "medial": round(a_in.item(), 5), "s": round(time.time() - t0, 1)})
                print("SFT", hist[-1], flush=True)
        log["neutral_stepper_ft"] = hist
        S.eval()
    # -- 投影器: 查询 -> 最近的 [x, z]. 可搜索的帧同 mm.js (未来 1 秒还在同一片段里) --
    horizon = max(meta["horizon"])
    srch = torch.tensor([f for c in meta["clips"] for f in range(c["start"], c["end"] - horizon - 1)], device=DEV)
    Xs = Xt[srch]
    Xs2 = (Xs ** 2).sum(1)
    Pn = MLP(27, [ARGS.proj_hidden] * 3, 27 + NZ, "relu").to(DEV)
    if ck and "P" in ck and ARGS.proj_from_ckpt:  # 从检查点的投影器接着训 (学习率小一点)
        Pn.load_state_dict(ck["P"])
    opt = adam(Pn.parameters(), lr=3e-4 if ck and "P" in ck and ARGS.proj_from_ckpt else 1e-3)
    sched = torch.optim.lr_scheduler.ExponentialLR(opt, 0.99)
    iters, bs = int(ARGS.proj_iters * scale), 256
    hist, t0 = [], time.time()

    def queries(n):
        """查询: 某一帧的特征; 一半换成另一帧的轨迹 (想往别处走), 再加大小随机的噪声."""
        a = srch[torch.randint(0, len(srch), (n,), device=DEV)]
        q = Xt[a].clone()
        swap = torch.rand(n, device=DEV) < ARGS.swap
        b = srch[torch.randint(0, len(srch), (n,), device=DEV)]
        q[swap, :12] = Xt[b[swap], :12]
        return q + torch.randn_like(q) * torch.rand(n, 1, device=DEV) * 0.5

    def nearest(q):
        out = []
        for k in range(0, len(q), 256):  # 每块 256 个查询 x 13 万帧, 显存一百多 MB
            qq = q[k:k + 256]
            out.append(srch[(Xs2[None] - 2 * qq @ Xs.T + (qq ** 2).sum(1, keepdim=True)).argmin(1)])
        return torch.cat(out)
    for it in range(iters):
        q = queries(bs)
        with torch.no_grad():
            nn_ = nearest(q)
        out = Pn(q)
        l_x = (out[:, :27] - Xt[nn_]).abs().mean()
        l_z = (out[:, 27:] - Zn[nn_]).abs().mean()
        # 代价要准: 运动匹配拿它和"接着播当前帧"的代价比, 决定换不换
        l_cost = (((q - out[:, :27]) ** 2).sum(1) - ((q - Xt[nn_]) ** 2).sum(1)).abs().mean() * 0.1
        loss = l_x + 2 * l_z + l_cost
        if ARGS.pose_w > 0:
            # 投影结果过一遍 (冻住的) 解压器, 直接比姿态、根部运动 (转向) 和触地: 只比 z 时急转会被"平均"成缓转
            o = D(torch.cat([out[:, :27], out[:, 27:] * z_std + z_mean], 1))
            p_lin, p_dir, p_c = pose.loss(o, Yn[nn_], Ct[nn_])
            r0 = B * 6 + 3
            p_root = (o[:, r0:r0 + 3] - Yn[nn_][:, r0:r0 + 3]).abs().mean()
            loss = loss + ARGS.pose_w * (p_lin + 2 * p_dir + 2 * p_root + 0.2 * p_c)
            if ARGS.arm_w or ARGS.arm_in:
                a_sym, a_in = arm_terms(o, Yn[nn_])
                loss = loss + ARGS.arm_w * a_sym + ARGS.arm_in * a_in
        opt.zero_grad(); loss.backward(); opt.step()
        if it % 1000 == 999:
            sched.step()
        if it % 5000 == 0 or it == iters - 1:
            hist.append({"it": it, "x": round(l_x.item(), 4), "z": round(l_z.item(), 4), "cost": round(l_cost.item(), 4),
                         "s": round(time.time() - t0, 1)})
            print("P", hist[-1], flush=True)
    log["neutral_projector"] = hist
    Pn.eval()
    # -- 离线误差 --
    ev = {}
    with torch.no_grad():
        O = torch.cat([D(torch.cat([Xt[k:k + 8192], Z[k:k + 8192]], 1)) for k in range(0, N, 8192)])
        ev["decompress"] = pose_errors(pose, O, Yn, Ct, P, y_mean, y_std, B)
        # 步进器展开 1 秒、2 秒以后再解压的误差
        for secs in (1, 2):
            n = 30 * secs
            st = torch.tensor([f for c in meta["clips"] for f in range(c["start"], c["end"] - n - 1, 7)], device=DEV)
            cur = XZ[st]
            for _ in range(n):
                cur = cur + S(cur) * d_std + d_mean
            o = D(torch.cat([cur[:, :27], cur[:, 27:] * z_std + z_mean], 1))
            ev[f"stepper_{secs}s"] = pose_errors(pose, o, Yn[st + n], Ct[st + n], P, y_mean, y_std, B)
            ev[f"stepper_{secs}s"]["feat_l1"] = round((cur[:, :27] - Xt[st + n]).abs().mean().item(), 4)
            ev[f"stepper_{secs}s"]["arm"] = arm_report(o, Yn[st + n])
        # 投影器: 和真最近邻比
        torch.manual_seed(123)
        q = queries(4096)
        nn_ = nearest(q)
        out = Pn(q)
        c_true = ((q - Xt[nn_]) ** 2).sum(1)
        c_proj = ((q - out[:, :27]) ** 2).sum(1)
        ev["projector"] = {"x_l1": round((out[:, :27] - Xt[nn_]).abs().mean().item(), 4),
                           "z_l1": round((out[:, 27:] - Zn[nn_]).abs().mean().item(), 4),
                           "cost_true_mean": round(c_true.mean().item(), 4), "cost_proj_mean": round(c_proj.mean().item(), 4),
                           "pose": pose_errors(pose, D(torch.cat([out[:, :27], out[:, 27:] * z_std + z_mean], 1)), Yn[nn_], Ct[nn_],
                                               P, y_mean, y_std, B),
                           "arm": arm_report(D(torch.cat([out[:, :27], out[:, 27:] * z_std + z_mean], 1)), Yn[nn_])}
    # -- 触地分类器 (--contact-iters 步): 输入是步进器的状态 [x, 标准化的 z], 和运行时一样; 训练数据是运行时会遇到的三种状态 --
    K = None
    if ARGS.contact_iters > 0:
        K = MLP(27 + NZ, [128, 128], 2, "relu").to(DEV)
        opt = adam(K.parameters())
        hist, t0 = [], time.time()

        def contact_batch(n):
            """三种来源各 n 个: 真状态 (加一点噪声)、投影器的输出 (标签取最近邻那帧)、步进器从真状态往前推 1-19 帧 (标签取真帧)."""
            with torch.no_grad():
                i = torch.randint(0, N, (n,), device=DEV)
                xa, ya = XZ[i] + torch.randn(n, 27 + NZ, device=DEV) * 0.05, Ct[i]
                q = queries(n)
                yb, xb = Ct[nearest(q)], Pn(q)
                s0 = starts[torch.randint(0, len(starts), (n,), device=DEV)]
                k = torch.randint(1, WINDOW, (n,), device=DEV)
                cur = XZ[s0]
                xc = cur.clone()
                for step in range(1, WINDOW):
                    cur = cur + S(cur) * d_std + d_mean
                    m = k == step
                    xc[m] = cur[m]
                yc = Ct[s0 + k]
            return torch.cat([xa, xb, xc]), torch.cat([ya, yb, yc])
        for it in range(int(ARGS.contact_iters * scale)):
            xx, yy = contact_batch(512)
            loss = F.binary_cross_entropy_with_logits(K(xx), yy)
            opt.zero_grad(); loss.backward(); opt.step()
            if it % 5000 == 0 or it == int(ARGS.contact_iters * scale) - 1:
                hist.append({"it": it, "bce": round(loss.item(), 4), "s": round(time.time() - t0, 1)})
                print("K", hist[-1], flush=True)
        log["neutral_contact"] = hist
        K.eval()
        with torch.no_grad():
            torch.manual_seed(321)
            n = 4096
            xx, yy = contact_batch(n)
            dec = D(torch.cat([xx[:, :27], xx[:, 27:] * z_std + z_mean], 1))[:, pose.ny:pose.ny + 2]
            kk = K(xx)
            acc = lambda a, b: round(((a > 0).float() == b).float().mean().item(), 4)  # noqa: E731
            ev["contact"] = {name: {"decoder": acc(dec[j * n:(j + 1) * n], yy[j * n:(j + 1) * n]),
                                    "classifier": acc(kk[j * n:(j + 1) * n], yy[j * n:(j + 1) * n])}
                             for j, name in enumerate(["true_state", "projector", "stepper"])}
    log["neutral_eval"] = ev
    torch.save({"C": C.state_dict(), "D": D.state_dict(), "S": S.state_dict(), "P": Pn.state_dict(),
                **({"K": K.state_dict()} if K is not None else {})}, ARGS.ckpt)
    print("eval", json.dumps(ev, ensure_ascii=False), flush=True)
    nets = {"decompressor": (D, "elu"), "stepper": (S, "relu"), "projector": (Pn, "relu"), **({"contact": (K, "relu")} if K is not None else {})}
    norms = {"y_mean": y_mean, "y_std": y_std, "z_mean": z_mean.cpu().numpy(), "z_std": z_std.cpu().numpy(),
             "d_mean": d_mean.cpu().numpy(), "d_std": d_std.cpu().numpy()}
    # JS 对照用: 第 1000 帧的 [x, z] 走一步、解压
    with torch.no_grad():
        k = 1000
        xz = XZ[k:k + 1]
        nxt = xz + S(xz) * d_std + d_mean
        dec = D(torch.cat([xz[:, :27], xz[:, 27:] * z_std + z_mean], 1))
        q = Xt[k:k + 1]
        check = {"xz": xz[0].tolist(), "step": nxt[0].tolist(), "decoded": dec[0].tolist(), "query": q[0].tolist(),
                 "projected": Pn(q)[0].tolist(), **({"contact": K(xz)[0].tolist()} if K is not None else {})}
    extra = {"featureMean": meta["featureMean"], "featureScale": meta["featureScale"], "speeds": meta["speeds"],
             "canon": meta["canon"], "aim": meta["aim"], "horizon": meta["horizon"], "groups": meta["groups"], "ends": meta["ends"],
             "style": "neutral", "sourceFrames": N, "searchable": int(len(srch))}
    return {"kind": "neutral", "nets": nets, "norms": norms, "meta": extra, "bones": meta["bones"], "fps": meta["fps"],
            "check": check, "sha": db["sha"], "hidden": hidden}


def pose_errors(pose: Pose, out, y_norm, c_true, P, y_mean, y_std, B) -> dict:
    """骨骼指向的角度误差 (度, 各骨骼平均和最差骨骼)、髋部 (髋高单位)、根部速度、触地准确率."""
    d1, d2 = pose.dirs(out), pose.dirs(y_norm)
    ang = torch.rad2deg(torch.acos((d1 * d2).sum(-1).clamp(-1, 1)))        # n, B
    ys, ym = to(y_std), to(y_mean)
    o = out[:, :pose.ny] * ys + ym
    t = y_norm * ys + ym
    hips = (o[:, B * 6:B * 6 + 3] - t[:, B * 6:B * 6 + 3]).norm(dim=1)
    root = (o[:, B * 6 + 3:B * 6 + 6] - t[:, B * 6 + 3:B * 6 + 6]).abs().mean(0)
    res = {"dir_deg_mean": round(ang.mean().item(), 3), "dir_deg_p95": round(ang.flatten().quantile(0.95).item(), 3),
           "dir_deg_worst_bone": round(ang.mean(0).max().item(), 3), "hips_mean": round(hips.mean().item(), 4),
           "root_abs": [round(v, 4) for v in root.tolist()]}
    if pose.contacts and c_true is not None:
        res["contact_acc"] = round(((out[:, pose.ny:pose.ny + 2] > 0).float() == c_true).float().mean().item(), 4)
    return res


# -- 待机库 ------------------------------------------------------------------------------------
def keyframes(clips: list[dict], key: int | None = None) -> list[list[int]]:
    """每段的关键帧帧号: 从开头每 key (默认 KEY) 帧一个, 最后一帧也是."""
    out = []
    for c in clips:
        ks = list(range(c["start"], c["end"] - 1, key or KEY))
        if ks[-1] != c["end"] - 1:
            ks.append(c["end"] - 1)
        out.append(ks)
    return out


def train_idle(hidden: int, scale: float, log: dict) -> dict:
    db = load_db("idle")
    meta, B, N = db["meta"], db["B"], db["F"]
    P = pose_arrays(db)
    canon = np.array([meta["canon"][n] for n in meta["bones"]], np.float32)
    y_mean, y_std = stats(P["y"])
    c_in = np.concatenate([P["y"], P["vel"]], 1)
    ci_mean, ci_std = stats(c_in)
    Yn = to((P["y"] - y_mean) / y_std)
    Cin = to((c_in - ci_mean) / ci_std)
    pose = Pose(B, y_mean, y_std, canon, False)
    # 每一帧前后两个关键帧和插值比例
    kfs = keyframes(meta["clips"])
    k0, k1, al = np.zeros(N, np.int64), np.zeros(N, np.int64), np.zeros(N, np.float32)
    for c, ks in zip(meta["clips"], kfs):
        for j in range(len(ks) - 1):
            a, b = ks[j], ks[j + 1]
            f = np.arange(a, b + 1)
            k0[f], k1[f], al[f] = a, b, (f - a) / (b - a)
    K0, K1, AL, NXT = to(k0), to(k1), to(al)[:, None], to(P["next"])
    C = MLP(Cin.shape[1], [512, 512], NZ, "elu").to(DEV)
    D = MLP(NZ, [hidden, hidden], pose.ny, "elu").to(DEV)
    opt = adam(list(C.parameters()) + list(D.parameters()))
    sched = torch.optim.lr_scheduler.ExponentialLR(opt, 0.99)
    iters, bs = int(60000 * scale), 512
    hist, t0 = [], time.time()

    def interp(i):
        z = C(torch.cat([Cin[K0[i]], Cin[K1[i]]]))
        n = len(i)
        return z[:n] * (1 - AL[i]) + z[n:] * AL[i], z
    for it in range(iters):
        i = torch.randint(0, N, (bs,), device=DEV)
        j = NXT[i]
        zi, zk = interp(i)
        zj, _ = interp(j)
        oi, oj = D(zi), D(zj)
        l_lin, l_dir, _ = pose.loss(oi, Yn[i])
        l_vel = ((oj - oi) - (Yn[j] - Yn[i])).abs().mean()
        loss = l_lin + 2 * l_dir + 4 * l_vel + 1e-3 * (zk ** 2).mean()
        opt.zero_grad(); loss.backward(); opt.step()
        if it % 1000 == 999:
            sched.step()
        if it % 5000 == 0 or it == iters - 1:
            hist.append({"it": it, "lin": round(l_lin.item(), 4), "dir": round(l_dir.item(), 4), "vel": round(l_vel.item(), 4),
                         "s": round(time.time() - t0, 1)})
            print("idle", hist[-1], flush=True)
    log["idle"] = hist
    C.eval(); D.eval()
    with torch.no_grad():
        allk = sorted({k for ks in kfs for k in ks})
        Zk = C(Cin[torch.tensor(allk, device=DEV)])
        zmap = {k: n for n, k in enumerate(allk)}
        Zall = Zk[torch.tensor([zmap[k] for k in k0], device=DEV)] * (1 - AL) + Zk[torch.tensor([zmap[k] for k in k1], device=DEV)] * AL
        O = torch.cat([D(Zall[k:k + 8192]) for k in range(0, N, 8192)])
        ev = {"decompress_interp": pose_errors(pose, O, Yn, None, P, y_mean, y_std, B)}
        # 相邻帧之间的跳变 (关键帧处有没有台阶): 解出来的骨骼指向逐帧变化 vs 真值
        d = pose.dirs(O)
        dt = pose.dirs(Yn)
        same = to(~P["last"])
        step_o = (d[1:] - d[:-1]).norm(dim=-1).mean(1)[same[:-1]]
        step_t = (dt[1:] - dt[:-1]).norm(dim=-1).mean(1)[same[:-1]]
        ev["frame_step"] = {"decoded_mean": round(step_o.mean().item(), 5), "true_mean": round(step_t.mean().item(), 5),
                            "decoded_p99": round(step_o.quantile(0.99).item(), 5), "true_p99": round(step_t.quantile(0.99).item(), 5)}
        check = {"z": Zall[1000].tolist(), "decoded": O[1000].tolist(), "frame": 1000}
    log["idle_eval"] = ev
    print("idle eval", json.dumps(ev, ensure_ascii=False), flush=True)
    clips = [{**c, "kf": n0, "nk": len(ks)} for c, ks, n0 in zip(meta["clips"], kfs, np.cumsum([0] + [len(k) for k in kfs[:-1]]).tolist())]
    kz = torch.cat([Zk[torch.tensor([zmap[k] for k in ks], device=DEV)] for ks in kfs]).cpu().numpy()
    extra = {"clips": clips, "groups": meta["groups"], "names": meta.get("names"), "canon": meta["canon"], "key": KEY,
             "sourceFrames": N, "contactsFrom": "idle.bin"}
    return {"kind": "idle", "nets": {"decompressor": (D, "elu")}, "norms": {"y_mean": y_mean, "y_std": y_std},
            "meta": extra, "bones": meta["bones"], "fps": meta["fps"], "check": check, "sha": db["sha"], "hidden": hidden,
            "keyz": kz, "contacts": db["contacts"].astype(np.uint8)}


# -- 导出 --------------------------------------------------------------------------------------
def export(res: dict, path: Path) -> dict:
    """一个文件: [头长 u32][JSON 头][补齐到 4 字节][数据]. 权重 16 位整数, 每个输出一行一个比例 (float32); 偏置 float32."""
    blobs, off = [], 0

    def add(arr: np.ndarray) -> dict:
        nonlocal off
        b = arr.tobytes()
        pad = (-len(b)) % 4
        blobs.append(b + b"\0" * pad)
        ref = {"off": off, "n": int(arr.size), "type": {np.dtype(np.int16): "i16", np.dtype(np.float32): "f32", np.dtype(np.uint8): "u8",
                                                        np.dtype(np.int8): "i8"}[arr.dtype]}
        off += len(b) + pad
        return ref
    nets, params = {}, 0
    for name, (net, act) in res["nets"].items():
        layers = []
        for l in net.layers:
            W = l.weight.detach().cpu().numpy().astype(np.float32)        # nout, nin
            sc = np.maximum(np.abs(W).max(1), 1e-12) / 32767
            q = np.round(W / sc[:, None]).astype(np.int16)
            layers.append({"nin": W.shape[1], "nout": W.shape[0], "w": add(q), "s": add(sc.astype(np.float32)),
                           "b": add(l.bias.detach().cpu().numpy().astype(np.float32))})
            params += W.size + W.shape[0]
        nets[name] = {"act": act, "layers": layers}
    norms = {k: add(np.asarray(v, np.float32)) for k, v in res["norms"].items()}
    head = {"format": "wm-lmm", "version": 1, "kind": res["kind"], "fps": res["fps"], "bones": res["bones"], "nz": NZ,
            "hidden": res["hidden"], "nets": nets, "norms": norms, "meta": res["meta"], "check": res["check"], "source": res["sha"],
            "params": int(params), "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if "keyz" in res:
        kz = res["keyz"].astype(np.float32)
        ksc = np.maximum(np.abs(kz).max(0), 1e-6) / 32767
        head["keyz"] = {"q": add(np.round(kz / ksc).astype(np.int16)), "s": add(ksc.astype(np.float32)), "n": int(kz.shape[0])}
        head["contacts"] = add(res["contacts"].reshape(-1))
    if "featq" in res:
        head["featq"] = {"q": add(res["featq"].reshape(-1)), "s": add(res["fs"])}
        head["keyz8"] = {"q": add(res["kz8"].reshape(-1)), "s": add(res["kzs"]), "n": int(res["kz8"].shape[0])}
        head["contacts"] = add(res["contacts"].reshape(-1))
        if "rootq" in res:
            head["rootq"] = {"q": add(res["rootq"].reshape(-1)), "s": add(res["rs"])}
    hj = json.dumps(head, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    hpad = (-(4 + len(hj))) % 4
    data = struct.pack("<I", len(hj)) + hj + b" " * hpad + b"".join(blobs)
    path.write_bytes(data)
    return {"file": path.name, "bytes": len(data), "params": int(params),
            "sha": hashlib.sha256(data).hexdigest()[:16]}


def build_match(log: dict) -> dict:
    """第三版 (压缩的匹配库): 搜索、换段照原来的运动匹配 (匹配特征量化成 8 位), 姿态由解压器从潜变量解出来; 潜变量每 zstride 帧
    存一个 (8 位), 中间线性插值. 不用步进器和投影器, 所以换段和原来一样, 没有投影误差; 代价是占用随帧数涨 (每帧约 27 + 32/zstride 字节).
    压缩器、解压器取 --resume 的检查点 (第二版训出来的), 不重训."""
    if not ARGS.resume:
        raise SystemExit("--only match 要给 --resume 检查点 (压缩器、解压器)")
    db = load_db("neutral")
    meta, B, N = db["meta"], db["B"], db["F"]
    P = pose_arrays(db)
    canon = np.array([meta["canon"][n] for n in meta["bones"]], np.float32)
    y_mean, y_std = stats(P["y"])
    c_in = np.concatenate([P["y"], P["vel"]], 1)
    ci_mean, ci_std = stats(c_in)
    Yn, Cin, Ct = to((P["y"] - y_mean) / y_std), to((c_in - ci_mean) / ci_std), to(P["contacts"])
    pose = Pose(B, y_mean, y_std, canon, True)
    ck = torch.load(ARGS.resume, map_location=DEV)
    hidden = ck["D"]["layers.0.weight"].shape[0]
    C = MLP(Cin.shape[1], [512, 512], NZ, "elu").to(DEV)
    D = MLP(27 + NZ, [hidden, hidden], pose.ny + 2, "elu").to(DEV)
    C.load_state_dict(ck["C"]); D.load_state_dict(ck["D"])
    C.eval(); D.eval()
    X = db["feats"]
    qmax, qtype = (32767, np.int16) if ARGS.xbits == 16 else (127, np.int8)
    fs = (np.maximum(np.abs(X).max(0), 1e-6) / qmax).astype(np.float32)
    Xq = np.round(X / fs).astype(qtype)
    with torch.no_grad():
        Z = torch.cat([C(Cin[k:k + 8192]) for k in range(0, N, 8192)]).cpu().numpy()
    zs = (np.maximum(np.abs(Z).max(0), 1e-6) / 127).astype(np.float32)
    Xd = to(Xq.astype(np.float32) * fs)
    ev = {}
    for stride in sorted({1, 2, 4, ARGS.zstride}):
        kfs = keyframes(meta["clips"], stride)
        k0, k1, al = np.zeros(N, np.int64), np.zeros(N, np.int64), np.zeros(N, np.float32)
        for ks in kfs:
            for j in range(len(ks) - 1):
                a_, b_ = ks[j], ks[j + 1]
                f = np.arange(a_, b_ + 1)
                k0[f], k1[f], al[f] = a_, b_, (f - a_) / (b_ - a_)
        Zq = np.round(Z / zs).astype(np.int8).astype(np.float32) * zs
        Zi = Zq[k0] * (1 - al[:, None]) + Zq[k1] * al[:, None]
        with torch.no_grad():
            O = torch.cat([D(torch.cat([Xd[k:k + 8192], to(Zi[k:k + 8192])], 1)) for k in range(0, N, 8192)])
        e = pose_errors(pose, O, Yn, Ct, P, y_mean, y_std, B)
        e["bytes_per_frame"] = round(27 + 32 * sum(len(k) for k in kfs) / N, 2)
        ev[f"zstride_{stride}"] = e
        if stride == ARGS.zstride:
            kz8 = np.round(Z[[k for ks in kfs for k in ks]] / zs).astype(np.int8)
            clips = [{**c, "kf": n0, "nk": len(ks)} for c, ks, n0 in
                     zip(meta["clips"], kfs, np.cumsum([0] + [len(k) for k in kfs[:-1]]).tolist())]
            check = {"frame": 1000, "x": Xd[1000].tolist(), "z": Zi[1000].tolist(), "decoded": O[1000].tolist()}
    log["match_eval"] = ev
    print("match eval", json.dumps(ev, ensure_ascii=False), flush=True)
    extra = {"featureMean": meta["featureMean"], "featureScale": meta["featureScale"], "speeds": meta["speeds"], "canon": meta["canon"],
             "aim": meta["aim"], "horizon": meta["horizon"], "groups": meta["groups"], "ends": meta["ends"], "style": "neutral",
             "sourceFrames": N, "clips": clips, "key": ARGS.zstride, "resume": Path(ARGS.resume).name, "xbits": ARGS.xbits,
             "storedRoot": bool(ARGS.store_root)}
    return {"kind": "match", "nets": {"decompressor": (D, "elu")}, "norms": {"y_mean": y_mean, "y_std": y_std}, "meta": extra,
            "bones": meta["bones"], "fps": meta["fps"], "check": check, "sha": db["sha"], "hidden": hidden,
            "featq": Xq, "fs": fs, "kz8": kz8, "kzs": zs, "contacts": db["contacts"].astype(np.uint8),
            # --store-root: 根部运动 (根坐标系里的速度、转向角速度) 不让解压器出, 照原库存 16 位 (每帧 6 字节)
            **({"rootq": np.round(db["root"] / (rs := np.maximum(np.abs(db["root"]).max(0), 1e-6) / 32767)).astype(np.int16),
                "rs": rs.astype(np.float32)} if ARGS.store_root else {})}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hidden", type=int, default=256, help="解压器、步进器、投影器的隐藏层宽度")
    ap.add_argument("--scale", type=float, default=1.0, help="训练步数的倍数 (试跑用 0.05)")
    ap.add_argument("--only", choices=["neutral", "idle", "match"], help="match: 第三版的压缩匹配库 (要 --resume)")
    ap.add_argument("--zstride", type=int, default=2, help="第三版: 潜变量每几帧存一个")
    ap.add_argument("--xbits", type=int, default=8, choices=[8, 16], help="第三版: 匹配特征存几位")
    ap.add_argument("--store-root", type=int, default=0, help="第三版: 1 = 根部运动照原库存, 不让解压器出")
    ap.add_argument("--tag", default=None, help="记录文件的标签; 不给就用时间")
    ap.add_argument("--out", default=str(MOTION), help="导出到哪个目录 (试跑时指到临时目录)")
    ap.add_argument("--resume", default=None, help="中性库: 从这个检查点 (.pt) 接着, 压缩器、解压器、步进器不重训")
    ap.add_argument("--proj-hidden", type=int, default=384, help="投影器隐藏层宽度 (3 层)")
    ap.add_argument("--proj-iters", type=int, default=150000)
    ap.add_argument("--swap", type=float, default=0.7, help="投影器训练查询里换成另一帧轨迹的比例")
    ap.add_argument("--pose-w", type=float, default=1.0, help="投影器的姿态损失 (过解压器) 权重, 0 = 不加 (第一版)")
    ap.add_argument("--key", type=int, default=4, help="待机关键帧间隔 (帧)")
    ap.add_argument("--arm-w", type=float, default=0.0, help="手臂指向误差的额外权重 (投影器、步进器微调)")
    ap.add_argument("--arm-in", type=float, default=0.0, help="手臂往身体收的一侧另外罚的权重")
    ap.add_argument("--stepper-ft", type=int, default=0, help="--resume 时步进器再微调几步 (加姿态和手臂损失)")
    ap.add_argument("--proj-from-ckpt", type=int, default=0, help="1 = 投影器从检查点接着训 (不从头)")
    ap.add_argument("--contact-iters", type=int, default=0, help="触地分类器训几步 (0 = 不训, 用解压器自带的触地)")
    a = ap.parse_args()
    global ARGS, KEY
    ARGS, KEY = a, a.key
    torch.manual_seed(1)
    np.random.seed(1)
    tag = a.tag or time.strftime("%Y%m%d-%H%M%S")
    log = {"args": vars(a), "device": str(DEV), "torch": torch.__version__,
           "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    a.ckpt = str(out / "lmmneutral.pt")
    t0 = time.time()
    if a.only in (None, "neutral"):
        log["neutral_export"] = export(train_neutral(a.hidden, a.scale, log), out / "lmmneutral.bin")
    if a.only == "match":
        log["match_export"] = export(build_match(log), out / "lmmmatch.bin")
    if a.only in (None, "idle"):
        log["idle_export"] = export(train_idle(a.hidden, a.scale, log), out / "lmmidle.bin")
    log["seconds"] = round(time.time() - t0, 1)
    if torch.cuda.is_available():
        log["gpu_peak_mb"] = round(torch.cuda.max_memory_allocated() / 2 ** 20, 1)
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / f"r1a-train-{tag}.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print("done", json.dumps({k: log[k] for k in log if k.endswith("_export") or k in ("seconds", "gpu_peak_mb")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

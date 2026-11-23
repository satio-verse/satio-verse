"""R1a 误差定位 (docs/roadmap/motion-model.md): 姿态和触地时刻的误差是从哪一环进来的. 离线, 拿原库 (models/motion/neutral.*) 当真值.

  runtime/train-venv/Scripts/python.exe tools/lmm_locate.py --ckpt models/motion/r1a/v2/lmmneutral.pt [--tag g1] [--bias 0.1]

三组对比 (每组都报: 骨骼指向误差、着地 / 离地时刻 (±6 帧内对上的比例、平均差、每只脚每秒多出来的事件)、按推了几帧分段的时刻误差和相位滞后):
  1. 编码 vs 投影: 每帧用真编码的状态 [x, C(姿态)] 解码; 每帧用投影器 P(真特征) 解码 (不往前推). 投影带进来多少.
  2. 单步 vs 多步: 每步从真状态推一步 (单步); 从真状态连续推 60 帧; 从投影状态连续推 60 帧 (运行时就是这样起步的).
     多步的误差随推的帧数涨, 就是在累积.
  3. 固定段 vs 重新投影: 沿着真片段播 60 帧, 每 3 帧 (0.1 秒) 用 [真片段的未来轨迹, 当前状态的姿态特征] 建查询 (和行动层一样;
     另跑一遍给轨迹部分加噪声, 不然原库永远是当前帧最好、一次都不换):
     不重新投影; 照运动匹配的规则 (投影代价 + 偏置 < 当前代价 - 0.01 才换); 同样的规则, 另外要求候选和当前解出来的触地一样;
     每次都换; 原库自己 (精确最近邻, 同样的规则, 换了就从那一帧往下播).
     最后一种看"换段本身"会不会破坏接触时序 (即使数据是真的).
触地: 解压器自带的; 检查点里有触地分类器 (K) 时另报一份.
结果: logs/motion-model/r1a/locate/<标签>.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import train_lmm as T  # noqa: E402

LEN = 60          # 每段 60 帧 (2 秒)
EVERY = 3         # 每 3 帧搜一次 (行动层每 0.1 秒一次)
TOL = 6           # 着地 / 离地时刻 ±6 帧内算对上
BUCKETS = [(1, 15), (15, 30), (30, LEN + 1)]


def events(seq: np.ndarray) -> dict:
    """[T] 0/1 -> {on: [帧], off: [帧]}."""
    d = np.diff(seq.astype(np.int8))
    return {"on": list(np.where(d == 1)[0] + 1), "off": list(np.where(d == -1)[0] + 1)}


def timing(pred: np.ndarray, true: np.ndarray) -> dict:
    """pred, true: [段, T, 2]. 着地 / 离地对上的比例、平均差 (帧, 正 = 晚)、每只脚每秒多出来的事件; 另按真事件发生在第几帧分段."""
    res = {}
    for kind in ("on", "off"):
        n = hit = extra = 0
        d_all, by = [], {b: [] for b in BUCKETS}
        miss_by = {b: 0 for b in BUCKETS}
        for s in range(pred.shape[0]):
            for foot in range(2):
                ep, et = events(pred[s, :, foot])[kind], events(true[s, :, foot])[kind]
                used = set()
                for t in et:
                    n += 1
                    cand = [p for p in ep if p not in used and abs(p - t) <= TOL]
                    b = next(bb for bb in BUCKETS if bb[0] <= t < bb[1])
                    if cand:
                        p = min(cand, key=lambda p: abs(p - t))
                        used.add(p)
                        hit += 1
                        d_all.append(p - t)
                        by[b].append(p - t)
                    else:
                        miss_by[b] += 1
                extra += len(ep) - len(used)
        secs = pred.shape[0] * (pred.shape[1] - 1) / 30
        res[kind] = {"events": n, "found": round(hit / max(1, n), 4), "absFrames": round(float(np.mean(np.abs(d_all))) if d_all else 0, 3),
                     "signedFrames": round(float(np.mean(d_all)) if d_all else 0, 3), "extraPerFootSec": round(extra / max(1e-9, 2 * secs), 4),
                     "byHorizon": {f"{b[0]}-{b[1] - 1}": {"found": round(len(by[b]) / max(1, len(by[b]) + miss_by[b]), 4),
                                                         "absFrames": round(float(np.mean(np.abs(by[b]))) if by[b] else 0, 3),
                                                         "signedFrames": round(float(np.mean(by[b])) if by[b] else 0, 3)} for b in BUCKETS}}
    return res


def phase_lag(pred: np.ndarray, true: np.ndarray) -> dict:
    """每段、每只脚、每个分段里, 把预测的触地序列平移 -6..6 帧和真值比, 吻合最多的那个平移就是相位滞后 (正 = 预测晚). 报平均和绝对值平均.
    平移后超出这一段的部分不比, 只比重叠的部分 (至少分段长度的 2/3). 2026-10-27 以前要求整个窗口都在段内, 第一段 (从第 1 帧起)
    只能往前移 1 帧、最后一段只能往前移, 两头的滞后被低估了."""
    out = {}
    for b in BUCKETS:
        lags = []
        for s in range(pred.shape[0]):
            for foot in range(2):
                tw = true[s, b[0]:b[1], foot]
                if tw.min() == tw.max():
                    continue  # 这一段一直着地或一直离地, 看不出相位
                best, bl = -1, 0
                for L in range(-TOL, TOL + 1):
                    lo, hi = max(0, b[0] + L), min(pred.shape[1], b[1] + L)
                    if hi - lo < (b[1] - b[0]) * 2 // 3:
                        continue
                    agree = (pred[s, lo:hi, foot] == true[s, lo - L:hi - L, foot]).mean()
                    if agree > best or (agree == best and abs(L) < abs(bl)):
                        best, bl = agree, L
                lags.append(bl)
        out[f"{b[0]}-{b[1] - 1}"] = {"n": len(lags), "meanLag": round(float(np.mean(lags)), 3) if lags else None,
                                     "meanAbsLag": round(float(np.mean(np.abs(lags))), 3) if lags else None}
    return out


def flicker(seq: np.ndarray) -> dict:
    """不需要真值的两个量: 每只脚每秒切换触地状态几次 (着地 + 离地); 短于 4 帧的着地段或离地段占所有段的比例 (这种段让脚锁失效).
    开头、结尾被截断的段不算."""
    changes = int(np.abs(np.diff(seq.astype(np.int8), axis=1)).sum())
    short = total = 0
    for s_ in range(seq.shape[0]):
        for foot in range(2):
            x = seq[s_, :, foot]
            cut = np.where(np.diff(x) != 0)[0] + 1
            runs = np.diff(cut)  # 两次切换之间的段长 (不含两头)
            total += len(runs)
            short += int((runs < 4).sum())
    secs = seq.shape[0] * (seq.shape[1] - 1) / 30
    return {"switchesPerFootSec": round(changes / max(1e-9, 2 * secs), 3), "shortRunFrac": round(short / max(1, total), 4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default=str(ROOT / "models/motion/r1a/v2/lmmneutral.pt"))
    ap.add_argument("--tag", default="g1")
    ap.add_argument("--bias", type=float, default=0.1, help="运动匹配换段规则里投影代价加的偏置 (和运行时 --lmm-bias 一样)")
    ap.add_argument("--stride", type=int, default=31, help="每隔几帧取一个起点")
    ap.add_argument("--qnoise", type=float, default=0.3, help="第 3 组另跑一遍时查询轨迹部分加的噪声 (标准化单位), 让原库也会换段")
    a = ap.parse_args()
    torch.manual_seed(1)
    t0 = time.time()
    db = T.load_db("neutral")
    meta, B, N = db["meta"], db["B"], db["F"]
    P = T.pose_arrays(db)
    canon = np.array([meta["canon"][n] for n in meta["bones"]], np.float32)
    y_mean, y_std = T.stats(P["y"])
    c_in = np.concatenate([P["y"], P["vel"]], 1)
    ci_mean, ci_std = T.stats(c_in)
    Yn, Cin, Ct, Xt = T.to((P["y"] - y_mean) / y_std), T.to((c_in - ci_mean) / ci_std), T.to(P["contacts"]), T.to(db["feats"])
    pose = T.Pose(B, y_mean, y_std, canon, True)
    NZ = T.NZ
    ck = torch.load(a.ckpt, map_location=T.DEV)
    hid = ck["D"]["layers.0.weight"].shape[0]
    phid = ck["P"]["layers.0.weight"].shape[0]
    C = T.MLP(Cin.shape[1], [512, 512], NZ, "elu").to(T.DEV); C.load_state_dict(ck["C"]); C.eval()
    D = T.MLP(27 + NZ, [hid, hid], pose.ny + 2, "elu").to(T.DEV); D.load_state_dict(ck["D"]); D.eval()
    S = T.MLP(27 + NZ, [hid, hid], 27 + NZ, "relu").to(T.DEV); S.load_state_dict(ck["S"]); S.eval()
    Pn = T.MLP(27, [phid] * 3, 27 + NZ, "relu").to(T.DEV); Pn.load_state_dict(ck["P"]); Pn.eval()
    K = None
    if "K" in ck:
        K = T.MLP(27 + NZ, [128, 128], 2, "relu").to(T.DEV); K.load_state_dict(ck["K"]); K.eval()
    horizon = max(meta["horizon"])
    srch = torch.tensor([f for c in meta["clips"] for f in range(c["start"], c["end"] - horizon - 1)], device=T.DEV)
    Xs = Xt[srch]; Xs2 = (Xs ** 2).sum(1)
    starts = torch.tensor([f for c in meta["clips"] for f in range(c["start"], c["end"] - horizon - LEN - 2, a.stride)], device=T.DEV)
    nS = len(starts)
    idx = starts[:, None] + torch.arange(LEN + 1, device=T.DEV)[None]          # [段, T]
    with torch.no_grad():
        Z = torch.cat([C(Cin[k:k + 8192]) for k in range(0, N, 8192)])
        z_mean, z_std = Z.mean(0), Z.std(0).clamp_min(1e-3)
        XZ = torch.cat([Xt, (Z - z_mean) / z_std], 1)
        st_ = torch.tensor([f for c in meta["clips"] for f in range(c["start"], c["end"] - T.WINDOW)], device=T.DEV)
        dXZ = XZ[st_ + 1] - XZ[st_]
        d_mean, d_std = dXZ.mean(0), dXZ.std(0).clamp_min(1e-4)
        def chunked(f, x, n=16384):
            return torch.cat([f(x[k:k + n]) for k in range(0, len(x), n)])
        step = lambda s: chunked(lambda v: v + S(v) * d_std + d_mean, s)  # noqa: E731
        proj = lambda q: chunked(Pn, q)  # noqa: E731
        clip_of_t = torch.as_tensor(db["clip_of"], device=T.DEV)
        clip_end = torch.tensor([c["end"] for c in meta["clips"]], device=T.DEV)
        arm_b = torch.tensor([meta["bones"].index(n) for n in T.ARM_BONES], device=T.DEV)
        arm_s = torch.tensor([1.0 if n.startswith("left") else -1.0 for n in T.ARM_BONES], device=T.DEV)
        true_c = Ct[idx].cpu().numpy().astype(np.int8)                         # [段, T, 2]
        true_y = Yn[idx]

        def decode(states):
            """[段, T, 59] -> (姿态 [段, T, ny+2], 解压器触地, 分类器触地或 None)."""
            flat = states.reshape(-1, 27 + NZ)
            o = torch.cat([D(torch.cat([flat[k:k + 16384, :27], flat[k:k + 16384, 27:] * z_std + z_mean], 1)) for k in range(0, len(flat), 16384)])
            o = o.reshape(*states.shape[:2], -1)
            cd = (o[..., pose.ny:pose.ny + 2] > 0).cpu().numpy().astype(np.int8)
            ck_ = None
            if K is not None:
                ck_ = (torch.cat([K(flat[k:k + 16384]) for k in range(0, len(flat), 16384)]).reshape(*states.shape[:2], 2) > 0).cpu().numpy().astype(np.int8)
            return o, cd, ck_

        def report(name, states, extra=None):
            o, cd, ck_ = decode(states)
            d1, d2 = pose.dirs(o), pose.dirs(true_y)
            ang = torch.rad2deg(torch.acos((d1 * d2).sum(-1).clamp(-1, 1))).mean(-1)   # [段, T]
            r = {"poseDeg": round(ang.mean().item(), 3),
                 "poseDegByHorizon": {f"{b[0]}-{b[1] - 1}": round(ang[:, b[0]:b[1]].mean().item(), 3) for b in BUCKETS},
                 "contactAcc": round(float((cd == true_c).mean()), 4), "timing": timing(cd, true_c), "phaseLag": phase_lag(cd, true_c),
                 "flicker": flicker(cd)}
            # 手臂 (和 train_lmm 的手臂项同一个定义: 骨骼指向的横向分量, 往身体中线为负): 全部帧, 和真值里这根骨头本来就指向身体中线
            # (横向分量 < -0.3: 手放在肚子前、抱胸、托腮一类贴身动作) 的帧分开报. 约束要放行这种帧: 那里偏差为正 = 手被拉离身体
            dp, dt = d1[..., arm_b, :], d2[..., arm_b, :]
            lat = (dp[..., 0] - dt[..., 0]) * arm_s
            inward = dt[..., 0] * arm_s < -0.3
            r["arm"] = {"latBias_x100": round(lat.mean().item() * 100, 3), "medial_x100": round(torch.relu(-lat).mean().item() * 100, 3),
                        "inwardFrac": round(inward.float().mean().item(), 4),
                        "inwardLatBias_x100": round(lat[inward].mean().item() * 100, 3) if inward.any() else None,
                        "inwardOutward_x100": round(torch.relu(lat[inward]).mean().item() * 100, 3) if inward.any() else None,
                        "inwardMedial_x100": round(torch.relu(-lat[inward]).mean().item() * 100, 3) if inward.any() else None}
            if ck_ is not None:
                r["classifier"] = {"contactAcc": round(float((ck_ == true_c).mean()), 4), "timing": timing(ck_, true_c), "phaseLag": phase_lag(ck_, true_c)}
            if extra:
                r.update(extra)
            print(name, json.dumps({k: r[k] for k in ("poseDeg", "contactAcc")}, ensure_ascii=False),
                  "着地/离地", r["timing"]["on"]["found"], r["timing"]["off"]["found"], "滞后", {k: v["meanAbsLag"] for k, v in r["phaseLag"].items()}, flush=True)
            return r

        res = {"ckpt": a.ckpt, "tag": a.tag, "starts": nS, "len": LEN, "bias": a.bias}
        # -- 1. 编码 vs 投影 --
        res["encoded"] = report("编码", XZ[idx])
        res["projected_each_frame"] = report("逐帧投影", proj(Xt[idx].reshape(-1, 27)).reshape(nS, LEN + 1, -1))
        # -- 2. 单步 vs 多步 --
        one = torch.cat([XZ[idx[:, :1]], step(XZ[idx[:, :-1]].reshape(-1, 27 + NZ)).reshape(nS, LEN, -1)], 1)
        res["single_step"] = report("单步", one)
        for name, s0 in (("multi_from_true", XZ[starts]), ("multi_from_projected", proj(Xt[starts]))):
            cur, seq = s0, [s0]
            for _ in range(LEN):
                cur = step(cur)
                seq.append(cur)
            res[name] = report("多步(" + ("真起点" if "true" in name else "投影起点") + ")", torch.stack(seq, 1))
        # -- 3. 固定段 vs 重新投影 (运动匹配的规则) --
        # 查询 = [真片段的未来轨迹 (+ 噪声), 当前状态的姿态特征]. 没有噪声时原库永远是当前帧最好, 一次都不换; 加了噪声 (模拟行动层
        # 的弹簧模拟骨和片段实际轨迹的差) 原库也会换段, 用来看"换段本身"会不会破坏接触时序. 同一份噪声给网络版和原库用.
        res["truth"] = {"flicker": flicker(true_c)}
        res["fixed_segment"] = res["multi_from_projected"]
        g = torch.Generator(device=T.DEV).manual_seed(7)
        for qn in (0.0, a.qnoise):
            noise = torch.randn(nS, LEN + 1, 12, device=T.DEV, generator=g) * qn
            tag = "" if qn == 0 else f"_noise{qn}"
            def cont(st):
                return D(torch.cat([st[:, :27], st[:, 27:] * z_std + z_mean], 1))[:, pose.ny:pose.ny + 2] > 0

            for mode in ("mm_rule", "always", "mm_rule_cc"):
                cur, seq, switches = proj(Xt[starts]), [], 0
                seq.append(cur)
                for k in range(1, LEN + 1):
                    cur = step(cur)
                    if k % EVERY == 0:
                        q = torch.cat([Xt[idx[:, k], :12] + noise[:, k], cur[:, 12:27]], 1)
                        pr = proj(q)
                        if mode == "always":
                            sw = torch.ones(nS, dtype=torch.bool, device=T.DEV)
                        else:
                            sw = ((q - pr[:, :27]) ** 2).sum(1) + a.bias < ((q - cur[:, :27]) ** 2).sum(1) - 0.01
                            if mode == "mm_rule_cc":
                                sw &= (cont(pr) == cont(cur)).all(1)
                        cur = torch.where(sw[:, None], pr, cur)
                        switches += int(sw.sum())
                    seq.append(cur)
                res[mode + tag] = report({"mm_rule": "每3帧按规则换", "always": "每3帧都换", "mm_rule_cc": "按规则换且触地一致"}[mode] + tag, torch.stack(seq, 1),
                                         {"switchesPerSec": round(switches / (nS * LEN / 30), 3)})
            # 原库自己: 同样的查询和规则, 精确最近邻, 换了就从那一帧往下播; 触地取播到的那一帧
            frame = starts.clone()
            played, switches = [frame.clone()], 0
            end_of = clip_end[clip_of_t[frame]]
            for k in range(1, LEN + 1):
                frame = torch.minimum(frame + 1, end_of - 1)
                if k % EVERY == 0:
                    q = torch.cat([Xt[idx[:, k], :12] + noise[:, k], Xt[frame, 12:27]], 1)
                    best = torch.cat([srch[(Xs2[None] - 2 * q[j:j + 256] @ Xs.T + (q[j:j + 256] ** 2).sum(1, keepdim=True)).argmin(1)]
                                      for j in range(0, nS, 256)])
                    near = (clip_of_t[best] == clip_of_t[frame]) & ((best - frame).abs() < 4)
                    sw = (((q - Xt[best]) ** 2).sum(1) < ((q - Xt[frame]) ** 2).sum(1) - 0.01) & ~near
                    frame = torch.where(sw, best, frame)
                    end_of = clip_end[clip_of_t[frame]]
                    switches += int(sw.sum())
                played.append(frame.clone())
            pl = torch.stack(played, 1)
            dc = Ct[pl].cpu().numpy().astype(np.int8)
            d1, d2 = pose.dirs(Yn[pl]), pose.dirs(true_y)
            ang = torch.rad2deg(torch.acos((d1 * d2).sum(-1).clamp(-1, 1))).mean(-1)
            r = {"poseDeg": round(ang.mean().item(), 3), "contactAcc": round(float((dc == true_c).mean()), 4), "timing": timing(dc, true_c),
                 "phaseLag": phase_lag(dc, true_c), "flicker": flicker(dc), "switchesPerSec": round(switches / (nS * LEN / 30), 3)}
            res["original_mm" + tag] = r
            print("原库按规则换" + tag, r["poseDeg"], r["contactAcc"], r["timing"]["on"]["found"], r["timing"]["off"]["found"],
                  "换段/秒", r["switchesPerSec"], "抖动", r["flicker"], flush=True)
    res["seconds"] = round(time.time() - t0, 1)
    out = ROOT / "logs" / "motion-model" / "r1a" / "locate"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{a.tag}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("done", out / f"{a.tag}.json", res["seconds"])


if __name__ == "__main__":
    main()

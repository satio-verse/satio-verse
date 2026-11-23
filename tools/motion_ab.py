"""对比几次 tools/motion_baseline.mjs 的结果 (按 --motion 分组, 同一组的几轮取平均), 打印 Markdown 表并按门槛判过没过.

  python tools/motion_ab.py 结果1.json 结果2.json ... [--base base] [--md 输出.md]

门槛 (docs/roadmap/motion-model.md 的 R1a 验收), 都是和 base 组比:
  像真人分 <= base + 0.05; 痛觉每帧平均 <= base x 1.1 (base 是 0 时 <= 0.01); 脚打滑 <= base x 1.2 (base 0 时 <= 0.01);
  冲击 <= base x 1.1 (base 0 时 <= 1); 每个场景都完成; 走到目标离站位 <= 0.25 m、朝向差 <= 5 度;
  延迟 (起步、到 0.4 m/s、换目标偏开 20 度、停下、转过 90 度; 取最慢的种子): <= base x 1.2 + 一帧 (17 ms);
  动作来源每帧 P95 <= 1 ms; 整帧平均比 base 多不超过 0.5 ms; 动作来源占用 (中性库 + 待机) <= 8 MB.
同一组几轮之间确定性的结果 (场景、延迟) 应该逐行一样, 不一样会提示; 耗时取几轮的平均.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

QUALITY = ["idle", "talk", "walk", "reach", "sit", "pickput", "gesture", "emote", "lat_go", "lat_redirect", "lat_stop", "lat_turn",
           "lat_wave", "lat_heart", "walkstyle:happy", "walkstyle:sad", "walkstyle:angry", "walkstyle:shy", "walkstyle:calm"]
ARMOUT = ["idle", "walk", "reach", "sit", "pickput", "emote", "lat_heart"]
LAT = [("lat_go", "rootMoveMs"),("lat_go", "speed04Ms"), ("lat_redirect", "turnedMs"), ("lat_stop", "stopMs"), ("lat_turn", "halfMs")]


def group(results: list[dict]) -> dict:
    by = defaultdict(list)
    for r in results:
        by["reach" if r["scenario"].startswith("reach:") else r["scenario"]].append(r)
    return by


def agg(runs: list[dict]) -> dict:
    """一组 (同一个 --motion) 的几轮: 场景指标取第一轮 (确定性的), 耗时取平均."""
    first = runs[0]
    by = group(first["results"])
    out = {"scen": {}, "lat": {}, "reach": None, "mem": None, "timing": {}, "versions": first["versions"]["code"], "runs": len(runs),
           "cpuBusy": [r["env"].get("cpuBusy") for r in runs], "diverge": []}
    for k, rs in by.items():
        out["scen"][k] = {"ok": sum(r["ok"] for r in rs), "n": len(rs), "natural": mean(r["natural"] for r in rs),
                          "pain": mean(r["pain_mean"] for r in rs), "slide": mean(r["pain"].get("slide", 0) for r in rs),
                          "jerk": mean(r["pain"].get("jerk", 0) for r in rs), "fails": [f"{r['scenario']}#{r['seed']}: {r['reason']}" for r in rs if not r["ok"]]}
        if k.startswith("lat_"):
            # 延迟取几个种子里最慢的 (有一个种子没测到就是 None)
            lats = [r.get("latency") or {} for r in rs]
            keys = {kk for la in lats for kk, v in la.items() if kk.endswith("Ms") and isinstance(v, (int, float))}
            out["lat"][k] = {kk: None if any(la.get(kk) is None for la in lats) else max(la[kk] for la in lats) for kk in keys}
    reach = [r["reach"] for r in first["results"] if r["scenario"].startswith("reach:") and r.get("reach")]
    if reach:
        out["reach"] = {"dist": mean(x["distM"] for x in reach), "maxDist": max(x["distM"] for x in reach), "yaw": mean(x["yawErrDeg"] for x in reach),
                        "skate": mean(x["skateMps"] for x in reach)}
    m = first["mem"]["atLoad"]
    out["mem"] = {"neutral": m["styles"].get("neutral", 0), "idle": m["idle"], "lmm": first["mem"].get("lmm")}
    # 手离髋部中线的横向距离 (厘米, motion_baseline 的 armOut): 看手臂约束是不是靠"一律张开"过关
    out["armOut"] = {}
    for sc in ARMOUT:
        v = [st["armOut"]["mean"] for name, st in first["stats"].items()
             if "armOut" in st and (name.split("#")[0] == sc or (sc == "reach" and name.startswith("reach:")))]
        if v:
            out["armOut"][sc] = mean(v)
    for key in ("tick", "source", "loco", "match"):
        vals = [r["stats"]["ALL"].get(key) for r in runs if r["stats"]["ALL"].get(key)]
        if vals:
            out["timing"][key] = {s: mean(v[s] for v in vals) for s in ("mean", "p50", "p95", "p99")}
    for r in runs[1:]:
        for x, y in zip(first["results"], r["results"]):
            if x["scenario"] != y["scenario"] or x["natural"] != y["natural"] or x["pain_mean"] != y["pain_mean"]:
                out["diverge"].append(f"{x['scenario']}#{x['seed']}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--base", default="base")
    ap.add_argument("--md", default=None)
    a = ap.parse_args()
    runs = defaultdict(list)
    for f in a.files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        m = d["env"].get("motion", "base")
        bias = d["env"].get("lmmBias") or 0
        key = m if m == "base" else f"{m}@{d['env'].get('lmmSlot', 256)}" + (f"+{bias}" if bias else "")  # 网络按槽长、代价偏置分组
        runs[f"{d['env']['label']}:{key}" if d["env"].get("label") and m != "base" else key].append(d)  # --label 区分同一跑法的不同模型
    G = {k: agg(v) for k, v in runs.items()}
    base = G[a.base]
    others = [k for k in G if k != a.base]
    L = []
    L.append(f"版本 (代码): " + ", ".join(f"{k} {g['versions']}" for k, g in G.items()) + "; 轮数: " + ", ".join(f"{k} {g['runs']}" for k, g in G.items()))
    L.append("整机 CPU 忙: " + ", ".join(f"{k} {g['cpuBusy']}" for k, g in G.items()))
    for k, g in G.items():
        if g["diverge"]:
            L.append(f"⚠ {k} 几轮之间不一样的场景: {', '.join(g['diverge'][:10])}")
    L.append("")
    L.append("| 场景 | " + " | ".join(f"{k} 完成 / 像真人 / 痛觉 / 打滑 / 冲击" for k in G) + " |")
    L.append("|---|" + "---|" * len(G))
    checks = defaultdict(list)
    for s in QUALITY:
        if s not in base["scen"]:
            continue
        cells = []
        for k, g in G.items():
            v = g["scen"].get(s)
            if not v:
                cells.append("—")
                continue
            cells.append(f"{v['ok']}/{v['n']} / {v['natural']:.3f} / {v['pain']:.3f} / {v['slide']:.3f} / {v['jerk']:.1f}")
            if k == a.base:
                continue
            b = base["scen"][s]
            def lim(x, mul, floor):
                return x * mul if x > 0 else floor
            if v["ok"] < v["n"]:
                checks[k].append(f"{s}: 有没完成的 ({'; '.join(v['fails'][:3])})")
            if v["natural"] > b["natural"] + 0.05:
                checks[k].append(f"{s}: 像真人分 {v['natural']:.3f} > {b['natural']:.3f} + 0.05")
            if v["pain"] > lim(b["pain"], 1.1, 0.01):
                checks[k].append(f"{s}: 痛觉 {v['pain']:.3f} > {lim(b['pain'], 1.1, 0.01):.3f}")
            if v["slide"] > lim(b["slide"], 1.2, 0.01):
                checks[k].append(f"{s}: 打滑 {v['slide']:.3f} > {lim(b['slide'], 1.2, 0.01):.3f}")
            if v["jerk"] > lim(b["jerk"], 1.1, 1):
                checks[k].append(f"{s}: 冲击 {v['jerk']:.1f} > {lim(b['jerk'], 1.1, 1):.1f}")
        L.append(f"| {s} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("| 延迟 (ms, 最慢的种子) | " + " | ".join(G) + " | 门槛 |")
    L.append("|---|" + "---|" * (len(G) + 1))
    for s, key in LAT:
        cells = [str(g["lat"].get(s, {}).get(key)) for g in G.values()]
        b = base["lat"].get(s, {}).get(key)
        thr = None if b is None else round(b * 1.2 + 17)
        L.append(f"| {s}.{key} | " + " | ".join(cells) + f" | ≤ {thr} |")
        for k in others:
            v = G[k]["lat"].get(s, {}).get(key)
            if thr is not None and (v is None or v > thr):
                checks[k].append(f"延迟 {s}.{key} = {v} > {thr}")
    for s, key in (("lat_wave", "upMs"), ("lat_wave", "wavingMs"), ("lat_heart", "shapeMs"), ("lat_heart", "fullMs")):
        L.append(f"| {s}.{key} | " + " | ".join(str(g["lat"].get(s, {}).get(key)) for g in G.values()) + " | 不变 |")
    L.append("")
    L.append("| 走到目标 | " + " | ".join(G) + " |")
    L.append("|---|" + "---|" * len(G))
    for key, name in (("dist", "离站位 m"), ("maxDist", "最大 m"), ("yaw", "朝向差 度"), ("skate", "脚滑 m/s (不经脚锁)")):
        L.append(f"| {name} | " + " | ".join(f"{g['reach'][key]:.3f}" if g["reach"] else "—" for g in G.values()) + " |")
    for k in others:
        r = G[k]["reach"]
        if r and (r["maxDist"] > 0.25 or r["yaw"] > 5):
            checks[k].append(f"走到目标: 最大 {r['maxDist']:.3f} m, 朝向差 {r['yaw']:.1f} 度")
    L.append("")
    if any(g["armOut"] for g in G.values()):
        L.append("| 手离髋部中线 (cm) | " + " | ".join(G) + " |")
        L.append("|---|" + "---|" * len(G))
        for sc in ARMOUT:
            L.append(f"| {sc} | " + " | ".join(f"{g['armOut'][sc]:.1f}" if sc in g["armOut"] else "—" for g in G.values()) + " |")
        L.append("")
    L.append("| 耗时 (ms, 几轮平均) | " + " | ".join(G) + " |")
    L.append("|---|" + "---|" * len(G))
    for key in ("tick", "source", "loco", "match"):
        L.append(f"| {key} 平均 / P95 / P99 | " + " | ".join(
            f"{g['timing'][key]['mean']:.3f} / {g['timing'][key]['p95']:.2f} / {g['timing'][key]['p99']:.2f}" if key in g["timing"] else "—"
            for g in G.values()) + " |")
    for k in others:
        t, tb = G[k]["timing"], base["timing"]
        if t.get("source", {}).get("p95", 0) > 1:
            checks[k].append(f"动作来源每帧 P95 {t['source']['p95']:.2f} ms > 1")
        if t.get("tick") and tb.get("tick") and t["tick"]["mean"] - tb["tick"]["mean"] > 0.5:
            checks[k].append(f"整帧平均多 {t['tick']['mean'] - tb['tick']['mean']:.2f} ms > 0.5")
    L.append("")
    L.append("| 动作来源占用 (MB) | " + " | ".join(G) + " |")
    L.append("|---|" + "---|" * len(G))
    for key in ("neutral", "idle"):
        L.append(f"| {key} | " + " | ".join(f"{g['mem'][key] / 1e6:.2f}" for g in G.values()) + " |")
    for k in others:
        tot = (G[k]["mem"]["neutral"] + G[k]["mem"]["idle"]) / 1e6
        if tot > 8:
            checks[k].append(f"占用 {tot:.2f} MB > 8")
    L.append("")
    for k in others:
        L.append(f"**{k}**: " + ("全部过门槛" if not checks[k] else f"{len(checks[k])} 项没过:"))
        for c in checks[k]:
            L.append(f"- {c}")
    text = "\n".join(L)
    print(text)
    if a.md:
        Path(a.md).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

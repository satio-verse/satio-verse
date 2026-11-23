"""新模型 / 新场景体检: 在执行评估里把能做的事都做一遍, 和已经调好的模型、场景比, 列出哪里疼、疼在哪、先看哪里.

  python tools/motion_checkup.py --avatar new.vrm             # 新模型 (放进 models/vrm/), 在默认场景里体检
  python tools/motion_checkup.py --scene bedroom              # 新场景 (web/scenes/bedroom.json), 用默认模型体检
  python tools/motion_checkup.py --avatar new.vrm --scene bedroom --seeds 1,2,3

做什么:
  1. 静态检查 (motion-eval.js 的 sceneChecks / modelChecks): 场景数据和房间模型对不对得上 (站位能不能走、家具有没有
     网格和部件角色、座面/放置点/物品高度、手够不够得着), 模型的身体比例、骨骼和参照模型差多少.
  2. 全套场景 (sweepScenarios): 待机、走路、坐下站起、拿放、手势、说话, 外加走到每个目标、坐每个座位、每件东西放到
     每个别的台面. 场景从场景 JSON 推出来, 新场景不用改代码.
  3. 和参照比: 参照模型 (默认 silvercat, 调过的主模型) 在参照场景 (默认客厅) 里跑同样的全套. 同名场景直接比,
     新场景里的按类 (walk / sit / pickput ...) 和参照的同类平均比. 痛觉按每秒算 (场景长短不一样).
  4. 报告: 每个比参照差很多的场景, 疼在哪几项、防穿模层推的是身体哪一段、重心出支撑面是在哪个动作里, 以及先看哪里.
     写到 logs/motion-checkup/ (Markdown 和原始 JSON), 也打印出来.

参照结果按输入文件 (web/、web/scenes/、models/motion/) 的修改时间缓存在 logs/motion-checkup/ref-*.json, 代码或
参数改了会自动重跑. 自己起一个模拟服务 (--tts mock, 不连真的语音和大模型), 跑完关掉.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tune_motion import ROOT, Evaluator, pain_weights  # noqa: E402

OUT = ROOT / "logs" / "motion-checkup"
FPS = 60
PAIN_RATIO = 1.5      # 每秒痛觉比参照高这么多倍 (加了下限之后) 算"明显更疼"
PAIN_FLOOR = 10.0     # 比较时两边都加的下限 (痛觉值/秒): 参照几乎不疼的场景不至于一点点就算翻倍
NAT_DIFF = 0.15       # 像真人分比参照差这么多算"明显不像"
MODEL_DIFF = 0.15     # 身体比例超出参照范围这么多 (相对) 要注意
NAMES = {"selfPen": "手臂插进身体", "pushed": "手臂被推开", "furniture": "碰进家具/墙", "itemPen": "物品穿模",
         "floorPen": "踩进地面", "floating": "脚离地", "slide": "脚打滑", "joint": "关节越界", "balance": "重心出支撑面",
         "jump": "动作跳变", "jerk": "动作冲击", "grab": "隔空拿放"}
# 每项疼的时候先看哪里 (这几轮调下来的经验)
HINTS = {
    "pushed": "手臂穿身体, 动作本身穿模由防穿模层推开: 看下面'推的是哪段'. 新模型先跑 "
              "tune_motion.py --avatars 新模型 --keys contact.,filter. (手臂外展、关节上限); 肚子胸口特别鼓的看 contact.js 的 BODY/CLOTH",
    "selfPen": "推完还插在身体里: 同上, 另外看手里拿的东西 (拿放场景) 和坐姿时手放腿上",
    "furniture": "碰家具: 先看场景检查 (部件角色、占地); 拿放时膝盖、脚尖插进矮桌看 locomotion.js 的 _clipBump / TUNE.standParts; "
                 "坐着时看座位前沿 (_seatInfo) 和落脚点 (_seatFoot)",
    "itemPen": "物品穿模: 先看场景检查里的放置点、物品高度 (放置点下面要有台面); 再看松手时手的位置 (grab)",
    "grab": "隔空拿放: 片段里手的高度离目标太远 (场景检查里的'要靠 IK 拉'), 或站位离得太远",
    "floorPen": "踩进地面: 模型的脚踝高度 (ankle) 和鞋底厚度; 坐下的片段里脚的高度 (坐着时脚要着地); TUNE.groundRaise/Lower",
    "floating": "脚离地: 腿短的模型够不着地 (TUNE.hipDropDown/Up), 或髋高量错了 (hipHeight)",
    "slide": "支撑脚打滑: 脚锁定的权重 TUNE.lockIn/lockOut/lockSettle 和 replant",
    "joint": "关节越界: pose-filter.js 的 LIMITS; 比例差得多的模型手臂重定向 (hangDeg) 会让肘、肩多转",
    "balance": "重心出支撑面: 看下面'在哪个动作里'; TUNE.balanceMargin/balanceMax/balanceMaxSingle, 或者那段动作片段本身",
    "jerk": "动作冲击: 动作之间的过渡 (TUNE.blend*) 或那段片段本身",
}


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机的测试服务端直连, 不走代理 (同 wm/localhttp.py)


def get_json(url: str):
    with _DIRECT.open(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def inputs_mtime() -> float:
    """影响评估结果的文件里最新的修改时间 (参照缓存过期用)."""
    files = [*(ROOT / "web").glob("*.js"), *(ROOT / "web" / "scenes").glob("*.json"), *(ROOT / "models" / "motion").glob("*")]
    return max(f.stat().st_mtime for f in files if f.is_file())


def run_sweeps(ev: Evaluator, pairs: list[tuple[str, str]], seeds: list[int]) -> list[dict]:
    """几个 (形象, 场景) 的全套, 一起发出去并行跑 (每个种子一个任务), 每个合并成一份结果, 带静态检查."""
    jobs = [{"scenarios": "sweep", "seeds": [s], "avatar": av, "scene": sc, "checks": True} for av, sc in pairs for s in seeds]
    res = ev.run(jobs) if jobs else []
    out = []
    for i, (av, sc) in enumerate(pairs):
        part = res[i * len(seeds):(i + 1) * len(seeds)]
        out.append({"avatar": av, "scene": sc, "seeds": seeds, "checks": part[0]["checks"],
                    "results": [r for x in part for r in x["results"]]})
    return out


def ref_cache(avatar: str, scene: str, seeds: list[int]) -> Path:
    return OUT / f"ref-{scene}-{Path(avatar).stem}-s{'_'.join(map(str, seeds))}.json"


def summarize(run: dict, w: dict[str, float]) -> dict[str, dict]:
    """全套结果 -> {场景: {rate: {项: 痛觉值/秒}, pain: 合计, natural, fail, reasons, contactBy, balanceBy}} (种子平均)."""
    acc: dict[str, dict] = {}
    for r in run["results"]:
        s = acc.setdefault(r["scenario"], {"rate": {}, "natural": [], "fail": 0, "n": 0, "reasons": [], "contactBy": {}, "balanceBy": {}})
        secs = max(r["frames"], 1) / FPS
        for k, v in r["pain"].items():
            if k != "jump":  # 平滑层抑制次数随可调参数变, 只作参考 (见 pain.js)
                s["rate"][k] = s["rate"].get(k, 0) + v * w.get(k, 0) / secs
        s["natural"].append(r["natural"])
        s["fail"] += 0 if r["ok"] else 1
        s["n"] += 1
        if r["reason"]:
            s["reasons"].append(r["reason"])
        for key in ("contactBy", "balanceBy"):
            for k, v in (r.get(key) or {}).items():
                s[key][k] = s[key].get(k, 0) + v
    out = {}
    for name, s in acc.items():
        rate = {k: v / s["n"] for k, v in s["rate"].items()}
        out[name] = {"rate": rate, "pain": sum(rate.values()), "natural": float(np.mean(s["natural"])), "fail": s["fail"],
                     "runs": s["n"], "reasons": s["reasons"],
                     "contactBy": {k: v / s["n"] for k, v in s["contactBy"].items()},
                     "balanceBy": {k: v / s["n"] for k, v in s["balanceBy"].items()}}
    return out


def kind(name: str) -> str:
    return name.split(":")[0]


def ref_for(name: str, refs: list[dict[str, dict]]) -> tuple[dict, str] | None:
    """场景的参照值: 参照里有同名场景就用它 (几个参照模型平均), 否则用同类场景的平均. 返回 (参照, 怎么比的)."""
    same = [r[name] for r in refs if name in r]
    how = "同名场景"
    if not same:
        same = [v for r in refs for k, v in r.items() if kind(k) == kind(name)]
        how = f"同类 ({kind(name)}) 平均"
    if not same:
        return None
    terms = {t for s in same for t in s["rate"]}
    rate = {t: float(np.mean([s["rate"].get(t, 0) for s in same])) for t in terms}
    return {"rate": rate, "pain": sum(rate.values()), "natural": float(np.mean([s["natural"] for s in same]))}, how


def compare(target: dict[str, dict], refs: list[dict[str, dict]]) -> list[dict]:
    """逐场景和参照比, 返回比参照明显差的场景 (失败、更疼、更不像真人), 最差的在前."""
    out = []
    for name, s in target.items():
        rf = ref_for(name, refs)
        if not rf:
            continue
        ref, how = rf
        ratio = (s["pain"] + PAIN_FLOOR) / (ref["pain"] + PAIN_FLOOR)
        dnat = s["natural"] - ref["natural"]
        if not (s["fail"] or ratio > PAIN_RATIO or dnat > NAT_DIFF):
            continue
        terms = sorted(((t, v, ref["rate"].get(t, 0)) for t, v in s["rate"].items()), key=lambda x: -(x[1] - x[2]))
        out.append({"scenario": name, "how": how, "fail": s["fail"], "runs": s["runs"], "reasons": s["reasons"],
                    "pain": s["pain"], "ref_pain": ref["pain"], "ratio": ratio, "natural": s["natural"],
                    "ref_natural": ref["natural"], "terms": [x for x in terms if x[1] - x[2] > 1][:3],
                    "contactBy": sorted(s["contactBy"].items(), key=lambda x: -x[1])[:3],
                    "balanceBy": sorted(s["balanceBy"].items(), key=lambda x: -x[1])[:3]})
    return sorted(out, key=lambda x: (-x["fail"], -x["ratio"]))


def model_notes(model: dict, refs: list[dict]) -> list[str]:
    """模型的身体比例和参照模型比: 超出参照范围 MODEL_DIFF 以上的列出来, 缺骨骼的列出来."""
    notes = []
    if model.get("missingBones"):
        notes.append(f"缺骨骼: {', '.join(model['missingBones'])} (缺脚趾时落脚、配平按脚踝估, 缺肩膀时手臂重定向不准)")
    for k, label in (("hipHeight", "髋高 (米)"), ("armToHip", "手臂长 / 髋高"), ("legToHip", "腿长 / 髋高"),
                     ("shoulderToHip", "肩宽 / 髋高"), ("ankle", "脚踝高 (米)")):
        vals = [r[k] for r in refs if r.get(k)]
        if not vals or model.get(k) is None:
            continue
        lo, hi = min(vals), max(vals)
        if model[k] < lo * (1 - MODEL_DIFF) or model[k] > hi * (1 + MODEL_DIFF):
            notes.append(f"{label} {model[k]}, 参照 {lo if lo == hi else f'{lo}-{hi}'}, 差得比较多")
    hang = model.get("hangDeg") or {}
    ref_hang = [v for r in refs for v in (r.get("hangDeg") or {}).values()]
    if hang and ref_hang and max(hang.values()) > max(ref_hang) * (1 + MODEL_DIFF) + 3:
        notes.append(f"手臂垂下不碰身体要外展 {max(hang.values())}° (参照最多 {max(ref_hang)}°): 身体比参照宽, 手臂穿模会多")
    return notes


def report(target: dict, tsum: dict, refs: list[dict], rsums: list[dict], bad: list[dict]) -> str:
    av, sc = target["avatar"], target["scene"]
    lines = [f"# 体检: {av} @ {sc}", "", f"{datetime.now():%Y-%m-%d %H:%M}, 种子 {target['seeds']}; 参照: "
             + ", ".join(f"{r['avatar']} @ {r['scene']}" for r in refs), ""]
    model = target["checks"]["model"]
    lines += ["## 模型", "", f"{model.get('name')}: 髋高 {model['hipHeight']} m, 手臂/髋高 {model['armToHip']}, 腿/髋高 {model['legToHip']}, "
              f"肩宽/髋高 {model['shoulderToHip']}, 脚踝 {model['ankle']} m, 手臂垂下要外展 {model.get('hangDeg')}", ""]
    notes = model_notes(model, [r["checks"]["model"] for r in refs])
    lines += [f"- {n}" for n in notes] or ["- 和参照模型差不多"]
    checks = target["checks"]["scene"]
    lines += ["", "## 场景数据", ""]
    lines += [f"- {'**注意**' if c['level'] == 'warn' else '提示'} {c['what']}: {c['msg']}" for c in checks] or ["- 没发现问题"]
    total = sum(s["runs"] for s in tsum.values())
    fails = sum(s["fail"] for s in tsum.values())
    lines += ["", "## 动作", "", f"{len(tsum)} 个场景 x {len(target['seeds'])} 个种子 = {total} 次, 失败 {fails} 次; "
              f"比参照明显差的 {len(bad)} 个 (失败, 或每秒痛觉超过参照 {PAIN_RATIO} 倍, 或像真人分差 {NAT_DIFF} 以上).", ""]
    lines += ["| 场景 | 痛觉/秒 | 参照 | 像真人 | 参照 | 失败 |", "|---|---|---|---|---|---|"]
    for name, s in tsum.items():
        rf = ref_for(name, rsums)
        rp, rn = (f"{rf[0]['pain']:.0f}", f"{rf[0]['natural']:.2f}") if rf else ("-", "-")
        mark = " **(!)**" if any(b["scenario"] == name for b in bad) else ""
        lines.append(f"| {name}{mark} | {s['pain']:.0f} | {rp} | {s['natural']:.2f} | {rn} | {s['fail']}/{s['runs']} |")
    if bad:
        count: dict[str, int] = {}
        for b in bad:
            for t, _, _ in b["terms"]:
                count[t] = count.get(t, 0) + 1
        lines += ["", "## 要看的", ""]
        if count:
            lines += ["主要是: " + ", ".join(f"{NAMES.get(t, t)} ({n} 个场景)" for t, n in sorted(count.items(), key=lambda x: -x[1])), ""]
        seen = set()
        for b in bad:
            lines.append(f"### {b['scenario']} (和参照{b['how']}比)")
            if b["fail"]:
                lines.append(f"- 失败 {b['fail']}/{b['runs']}: {'; '.join(dict.fromkeys(b['reasons']))}")
            lines.append(f"- 痛觉 {b['pain']:.0f}/秒 (参照 {b['ref_pain']:.0f}), 像真人 {b['natural']:.2f} (参照 {b['ref_natural']:.2f})")
            for t, v, r in b["terms"]:
                lines.append(f"- {NAMES.get(t, t)} {v:.0f}/秒 (参照 {r:.0f})")
            if b["contactBy"] and any(t in ("pushed", "selfPen") for t, _, _ in b["terms"]):
                lines.append("- 推的是哪段 (身体段:手臂取样点, 累计米): " + ", ".join(f"{k} {v:.2f}" for k, v in b["contactBy"]))
            if b["balanceBy"] and any(t == "balance" for t, _, _ in b["terms"]):
                lines.append("- 重心在哪个动作里出支撑面 (累计米): " + ", ".join(f"{k} {v:.2f}" for k, v in b["balanceBy"]))
            lines.append("")
            seen.update(t for t, _, _ in b["terms"])
        hints = [t for t in HINTS if t in seen]
        if hints:
            lines += ["## 先看哪里", ""] + [f"- **{NAMES[t]}**: {HINTS[t]}" for t in hints]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--avatar", help="要体检的形象 (models/vrm/ 里的文件名), 默认用第一个参照模型")
    ap.add_argument("--scene", default="room", help="要体检的场景 (web/scenes/名字.json)")
    ap.add_argument("--ref", default="silvercat.vrm", help="参照模型 (调过的, 2026-10-25 起是 silvercat), 逗号分隔; none 不比")
    ap.add_argument("--ref-scene", default="room")
    ap.add_argument("--seeds", default="1,2")
    ap.add_argument("--fresh", action="store_true", help="参照不用缓存, 重跑")
    ap.add_argument("--url", help="已经在跑的模拟服务 (不给就自己起一个)")
    ap.add_argument("--port", type=int, default=8794)
    ap.add_argument("--tabs", type=int, default=2, help="每个 (形象, 场景) 并行的标签页数")
    a = ap.parse_args()
    seeds = [int(s) for s in a.seeds.split(",")]
    refs = [] if a.ref == "none" else a.ref.split(",")
    avatar = a.avatar or (refs[0] if refs else None)
    if not avatar:
        raise SystemExit("要给 --avatar (或者参照模型)")
    if not (ROOT / "web" / "scenes" / f"{a.scene}.json").exists():
        raise SystemExit(f"没有场景 web/scenes/{a.scene}.json")
    OUT.mkdir(parents=True, exist_ok=True)
    w = pain_weights()
    ev = Evaluator(a.url, a.port, a.tabs, None)
    try:
        base = a.url or f"http://localhost:{a.port}"
        have = {i["name"] for i in get_json(f"{base}/api/avatars")["items"]}
        for av in [avatar, *refs]:
            if av not in have:  # 页面找不到时会悄悄换成默认形象, 这里先拦下
                raise SystemExit(f"没有形象 {av} (models/vrm/ 里有: {', '.join(sorted(have))})")
        # 参照 (有没过期的缓存就用缓存) 和被体检的一起跑
        stamp, cached, todo = inputs_mtime(), {}, []
        for av in refs:
            f = ref_cache(av, a.ref_scene, seeds)
            if not a.fresh and f.exists() and f.stat().st_mtime > stamp:
                cached[av] = json.loads(f.read_text(encoding="utf-8"))
            else:
                todo.append((av, a.ref_scene))
        if (avatar, a.scene) not in todo and not (a.scene == a.ref_scene and avatar in cached):
            todo.append((avatar, a.scene))
        print(f"跑: {', '.join(f'{av} @ {sc}' for av, sc in todo)} (种子 {seeds}); 参照用缓存: {', '.join(cached) or '无'}", flush=True)
        for r in run_sweeps(ev, todo, seeds):
            if r["avatar"] in refs and r["scene"] == a.ref_scene:
                ref_cache(r["avatar"], a.ref_scene, seeds).write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
                cached[r["avatar"]] = r
            if r["avatar"] == avatar and r["scene"] == a.scene:
                target = r
        ref_runs = [cached[av] for av in refs]
        if a.scene == a.ref_scene and avatar in cached:
            target = cached[avatar]
    finally:
        ev.close()
    tsum = summarize(target, w)
    # 和自己比没有意义: 参照里去掉被体检的那一份 (只体检场景时, 参照模型自己就是被体检的)
    others = [r for r in ref_runs if r is not target] or ref_runs
    rsums = [summarize(r, w) for r in others]
    bad = compare(tsum, rsums)
    text = report(target, tsum, others, rsums, bad)
    stem = f"{datetime.now():%Y%m%d-%H%M}-{Path(avatar).stem}-{a.scene}"
    (OUT / f"{stem}.md").write_text(text, encoding="utf-8")
    (OUT / f"{stem}.json").write_text(json.dumps({"target": target, "summary": tsum, "flagged": bad}, ensure_ascii=False),
                                      encoding="utf-8")
    print(text)
    print(f"报告: {OUT / (stem + '.md')}")


if __name__ == "__main__":
    main()

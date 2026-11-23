"""记忆阶段 7 (程序性记忆) 的评测台 —— 无模型、不花钱 (mem7-procedures, 2026-11-15).

题族是数据 (backups/mem7-procedures/families/*.json, 字段说明见同目录 字段说明.md), 假"她"是固定种子的脚本策略:
每一步调用都交给**真的** Experiences.after_tool / before_task / learn (同一个入口, 不绕过), 世界 (哪个修法管用) 在这里模拟.
四列: orig (改前, backups/mem7-procedures/orig/experience.py) / off / shadow / on (新代码的 agent.procedures 三档).

要回答的问题 (指标分开报, 不混):
  - 三次都成率 (MemT 那条): 第 1-3 轮都在预算内成了的 (题族, 种子) 占比; 另报每一轮的成功率和调用数.
  - 该不该触发 (动手前提示): 展示了几次、对不对 (提示里的修法正是今天要的)、误触发 (plain / readonly / shared_head 里展示了不对的)、
    误导 (lookalike / stale 里展示了不对的)、失效以后还推荐几次.
  - 做没做成: 照着提示做的 (pre / post) 和她自己摸出同一个做法的 (own) 分开数; 采用率只算前者.
注意: "她"是脚本, 数字说明机制能不能用, 不说明真模型会更好.

用法:
  python tools/bench_procedures.py --group dev --cols orig,off,shadow,on --seeds 20 --runs 6 --shared both --out backups/mem7-procedures/results/dev.json
  --draft DIR   新代码的草稿目录 (里面有 wm/agent/experience.py 和 wm/agent/procedures.py), 不给就用 wm/ 里的
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import inspect
import json
import random
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FAM_DIR = ROOT / "backups" / "mem7-procedures" / "families"
ORIG = ROOT / "backups" / "mem7-procedures" / "orig" / "experience.py"
DAY = 86400.0
BASE = 1_800_000_000.0
BT = re.compile(r"`([^`]+)`")
POST_KINDS = ("plain", "readonly", "shared_head")  # 这三类里展示了不对的 = 误触发 (要 0)


# ---------------------------------------------------------------- 载入实现
def _load_file(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_impls(draft: str):
    impls = {"orig": _load_file("mem7_orig_experience", ORIG)}
    if draft:
        base = Path(draft) / "wm" / "agent"
        importlib.import_module("wm.agent")
        if (base / "procedures.py").exists():
            _load_file("wm.agent.procedures", base / "procedures.py")
        impls["new"] = _load_file("wm.agent.experience", base / "experience.py")
    else:
        impls["new"] = importlib.import_module("wm.agent.experience")
    return impls


def supports_procedures(mod) -> bool:
    return "procedures" in inspect.signature(mod.Experiences.__init__).parameters


# ---------------------------------------------------------------- 世界和假"她"
class Sess:
    def __init__(self, rid: str, goal: str):
        self.run = {"id": rid, "goal": goal}


def call(cid: str, name: str, **args) -> dict:
    return {"id": cid, "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def needed_now(variant: dict, day: int) -> set:
    cur = set()
    for rule in sorted(variant["needs"], key=lambda r: r["from_day"]):
        if rule["from_day"] <= day:
            cur = set(rule["fixes"])
    return cur


def fail_text(v: dict, applied: set) -> str:
    """失败时的输出: 变体可以给 stage_outputs (做完某几步以后报的错换了个样, 表示有进展); 没给就一直是 fail_output."""
    best, size = v["fail_output"], -1
    for st in v.get("stage_outputs") or []:
        if set(st["done"]) <= applied and len(st["done"]) > size:
            best, size = st["fail_output"], len(st["done"])
    return best


def fix_of(fam: dict, token: str):
    for f in fam["fixes"]:
        if token == f.get("cmd") or token == f.get("file"):
            return f
    return None


def parse_block(block: str, fam: dict):
    """经验块 -> (路线们, 第一行(第一张卡)标了"试过没用"的修法 id). 一条路线 = (这一行里"先...再..."的那几步 (对得上某条修法的), 这一行标的
    试过没用); "备选"那一段单独成一条路线排在后面; 判成功的标准、主命令本身的引用不当修法. 一行的"试过没用"只管这一行的路线 ——
    另一张卡 (备选) 说的"试过没用"是它那种情况的事."""
    head = fam["command"]
    routes, first_bad = [], []
    for n, ln in enumerate(x for x in str(block or "").splitlines() if x.startswith("- ")):  # 只数"- "开头的卡行 (标题行不算第一行)
        main, alts, bad = [], [], []
        for seg in re.split(r"[；;]", ln):
            if "判成功" in seg:
                continue
            ids = []
            for t in BT.findall(seg):
                f = fix_of(fam, t.strip())
                if f is not None and t.strip() != head:
                    ids.append(f["id"])
            if "试过没用" in seg:
                bad += ids
            elif "备选" in seg:
                alts.append(ids)
            else:
                main += ids
        if main:
            routes.append((list(dict.fromkeys(main)), bad))
        routes += [(list(dict.fromkeys(x)), bad) for x in alts if x]
        if n == 0:
            first_bad = list(dict.fromkeys(bad))
    return routes, first_bad


def marked_bad(block: str, fam: dict) -> set:
    """块里任何一行"试过没用"一段里标的修法 id (评审要的指标 needed_marked_bad: 标成试过没用的恰好是当天要的修法)."""
    out = set()
    for ln in str(block or "").splitlines():
        for seg in re.split(r"[；;]", ln):
            if "试过没用" in seg:
                for t in BT.findall(seg):
                    f = fix_of(fam, t.strip())
                    if f is not None and t.strip() != fam["command"]:
                        out.add(f["id"])
    return out


class Clock:
    def __init__(self):
        self.t = BASE

    def __call__(self):
        return self.t


def make_store(mod, home: Path, pmode, clock):
    if pmode is not None and supports_procedures(mod):
        extra = {"_test_unlock_on": True} if pmode == "on" else {}  # on 档在生产代码里锁住, 评测台走测试专用的关键字参数
        return mod.Experiences(home, "on", clock=clock, procedures=pmode, **extra)
    return mod.Experiences(home, "on", clock=clock)


BAD_POLICY = ["last"]  # "last": 标了"试过没用"的修法排到最后才试 (模型读到的是参考, 不是禁令); "skip": 一律不试


def do_run(store, fam: dict, r: int, day: int, seed: int, p_adopt: float, clock: Clock, tag: str, order: int) -> dict:
    sched = fam.get("schedule") or [0]
    vi = sched[r % len(sched)]
    v = fam["variants"][vi]
    goal = v["goals"][r % len(v["goals"])]
    rid = f"{tag}-{fam['id']}-r{r}"
    rng = random.Random(f"{seed}:{fam['id']}:{r}")
    clock.t = BASE + day * DAY + order * 1000
    s = Sess(rid, goal)
    readonly = bool(fam.get("readonly"))
    pre = store.before_task(s, goal) if hasattr(store, "before_task") else ""
    need = needed_now(v, day)
    applied, bad_ids, src_of, n, calls = set(), set(), {}, [0], 0
    hint_ids_all, marked_all = [], set(marked_bad(pre, fam))

    def emit(name, text, error, **args):
        n[0] += 1
        clock.t += 1
        return store.after_tool(s, call(f"{rid}-c{n[0]}", name, **args), text, error)

    def apply(f, src):
        nonlocal calls
        if f["kind"] == "cmd":
            emit("run_command", "退出码 0\ndone", False, command=f["cmd"])
        else:
            emit("write_file", "写好了", False, path=f["file"])
        calls += 1
        applied.add(f["id"])
        src_of[f["id"]] = src

    pre_routes, pre_bad = parse_block(pre, fam)
    pre_ok_ids = [i for r_, _ in pre_routes for i in r_]
    hint_ids_all += pre_ok_ids
    bad_ids |= set(pre_bad)
    by_fid = {f["id"]: f for f in fam["fixes"]}
    if pre_routes and not (set(pre_routes[0][0]) & set(pre_routes[0][1])) and rng.random() < p_adopt:
        for fid in pre_routes[0][0]:
            if fid not in applied and fid not in bad_ids and calls < fam["budget"] - 1:
                apply(by_fid[fid], "pre")
    shuffled = list(fam["fixes"])
    rng.shuffle(shuffled)
    success, post_blocks, post_chars = False, 0, 0
    while calls < fam["budget"]:
        ok = v["fail_output"] is None or need <= applied
        out = "退出码 0\nok" if ok else fail_text(v, applied)
        hint = emit("run_command", out, not ok, command=fam["command"])
        calls += 1
        if ok:
            success = True
            break
        post_routes = []
        if hint:
            post_blocks += 1
            post_chars += len(hint)
            marked_all |= marked_bad(hint, fam)
            post_routes, bad = parse_block(hint, fam)
            hint_ids_all += [i for r_, _ in post_routes for i in r_]
            bad_ids |= set(bad)
        if readonly or calls >= fam["budget"]:
            break
        route = next((r_ for r_, lb in post_routes if not (set(r_) & set(lb)) and set(r_) - applied), None)
        if route is not None and rng.random() < p_adopt:
            for fid in route:
                if fid not in applied and calls < fam["budget"]:
                    apply(by_fid[fid], "post")
            continue
        rest = [f for f in shuffled if f["id"] not in applied and f["id"] not in bad_ids]
        if BAD_POLICY[0] == "last":
            rest += [f for f in shuffled if f["id"] not in applied and f["id"] in bad_ids]
        if not rest:
            break
        apply(rest[0], "own")
    summary = store.learn("sess", s.run)
    correct_pre = bool(pre_routes) and bool(set(pre_routes[0][0]) & need)
    return {"fam": fam["id"], "kind": fam["kind"], "r": r, "day": day, "variant": vi, "success": success, "calls": calls,
            "pre_shown": bool(pre), "pre_chars": len(pre), "pre_correct": correct_pre, "pre_ids": sorted(set(pre_ok_ids)),
            "post_blocks": post_blocks, "post_chars": post_chars, "hint_ids": sorted(set(hint_ids_all)),
            "src": {k: v2 for k, v2 in src_of.items()}, "need": sorted(need), "counts_for_success": fam["expect"]["card"],
            "needed_marked_bad": bool(marked_all & need), "marked_bad": sorted(marked_all), "summary": summary}


def simulate(fams: list, seed: int, mod, pmode, runs: int, p_adopt: float, shared: bool, days_between: float) -> dict:
    out, stats = [], []
    with tempfile.TemporaryDirectory() as tmp:
        clock = Clock()
        if shared:
            store = make_store(mod, Path(tmp) / "shared", pmode, clock)
            order = list(range(len(fams)))
            random.Random(seed).shuffle(order)
            for r in range(runs):
                for k, fi in enumerate(order):
                    out.append(do_run(store, fams[fi], r, int(r * days_between), seed, p_adopt, clock, "S", k))
            stats.append(store.view()["stats"])
        else:
            for fi, fam in enumerate(fams):
                store = make_store(mod, Path(tmp) / fam["id"], pmode, clock)
                for r in range(runs):
                    out.append(do_run(store, fam, r, int(r * days_between), seed, p_adopt, clock, "P", fi))
                stats.append(store.view()["stats"])
    return {"runs": out, "stats": stats}


# ---------------------------------------------------------------- 汇总
def _sum_numeric(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            dst[k] = dst.get(k, 0) + v
        elif isinstance(v, dict):
            _sum_numeric(dst.setdefault(k, {}), v)


def summarize(sims: list, fams: list, runs: int) -> dict:
    by_id = {f["id"]: f for f in fams}
    recs = [x for sim in sims for x in sim["runs"]]
    per = {}  # (种子序号, 题族) -> 各轮记录
    for si, sim in enumerate(sims):
        for x in sim["runs"]:
            per.setdefault((si, x["fam"]), []).append(x)
    card_keys = [k for k in per if by_id[k[1]]["expect"]["card"]]

    def rate(xs):
        return round(sum(xs) / len(xs), 3) if xs else None

    def mean(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    nmb = [x for x in recs if x["needed_marked_bad"]]
    res = {"needed_marked_bad": len(nmb),
           "needed_marked_bad_by_run": [sum(x["needed_marked_bad"] for x in recs if x["r"] == r) for r in range(runs)],
           "needed_marked_bad_by_kind": {k: sum(1 for x in nmb if x["kind"] == k) for k in sorted({x["kind"] for x in recs})
                                         if any(x["kind"] == k for x in nmb)},
           "runs": runs, "pairs": len(card_keys)}
    first = {}
    for k, xs in per.items():
        shown_at = next((x["r"] for x in xs if x["pre_shown"]), None)
        first[str(shown_at)] = first.get(str(shown_at), 0) + 1
    res["pre_first_round"] = dict(sorted(first.items()))  # 键是第几轮 (0 起), "None" = 整个 6 轮一次都没提示过
    first3 = [all(per[k][r]["success"] for r in range(min(3, runs))) for k in card_keys]
    last3 = [all(per[k][r]["success"] for r in range(3, runs)) for k in card_keys] if runs > 3 else []
    res["all_first3"], res["all_last3"] = rate(first3), rate(last3)
    res["all_runs"] = rate([all(x["success"] for x in per[k]) for k in card_keys])
    res["success_by_run"] = [rate([per[k][r]["success"] for k in card_keys]) for r in range(runs)]
    res["calls_by_run"] = [mean([per[k][r]["calls"] for k in card_keys if per[k][r]["success"]]) for r in range(runs)]
    res["by_kind_first3"] = {}
    for kind in sorted({by_id[k[1]]["kind"] for k in card_keys}):
        ks = [k for k in card_keys if by_id[k[1]]["kind"] == kind]
        res["by_kind_first3"][kind] = rate([all(per[k][r]["success"] for r in range(min(3, runs))) for k in ks])
    # 该不该触发
    shown = [x for x in recs if x["pre_shown"]]
    res["pre_shown"] = len(shown)
    res["pre_correct"] = sum(x["pre_correct"] for x in shown)
    res["pre_false_shown"] = sum((not x["pre_correct"]) and x["kind"] in POST_KINDS for x in shown)
    res["pre_misleading"] = sum((not x["pre_correct"]) and x["kind"] not in POST_KINDS and by_id[x["fam"]]["expect"]["card"]
                                for x in shown)
    res["pre_chars_mean"] = mean([x["pre_chars"] for x in shown])
    res["post_hint_blocks"] = sum(x["post_blocks"] for x in recs)
    res["post_chars_mean"] = mean([x["post_chars"] / x["post_blocks"] for x in recs if x["post_blocks"]])
    # 失效旧解法
    stale_pre_total = stale_pre_after_fail = 0
    for k, xs in per.items():
        fam = by_id[k[1]]
        if fam["kind"] != "stale":
            continue
        sf, sd = fam["stale_fix"], fam["stale_from_day"]
        failed_once = False
        for x in xs:
            if x["day"] < sd:
                continue
            hinted = x["pre_shown"] and sf in x["pre_ids"]
            stale_pre_total += int(hinted)
            if failed_once:
                stale_pre_after_fail += int(hinted)
            if sf in x["src"] and sf not in x["need"]:  # 失效的修法这一轮被做了、没用: 之后还推荐它就是在误导
                failed_once = True
    res["stale_pre_total"], res["stale_pre_after_failure"] = stale_pre_total, stale_pre_after_fail
    # 做没做成: 照着提示做的 vs 自己摸的
    fol_pre = sum(v == "pre" for x in recs for v in x["src"].values())
    fol_post = sum(v == "post" for x in recs for v in x["src"].values())
    own_total = sum(v == "own" for x in recs for v in x["src"].values())
    own_right = sum(1 for x in recs for fid, v in x["src"].items() if v == "own" and fid in x["need"])
    fol_right = sum(1 for x in recs for fid, v in x["src"].items() if v in ("pre", "post") and fid in x["need"])
    res["followed_pre"], res["followed_post"] = fol_pre, fol_post
    res["followed_right"] = fol_right
    res["own_routes"], res["own_right"] = own_total, own_right
    res["adopted_rate_given_shown"] = rate([bool([1 for v in x["src"].values() if v == "pre"]) for x in shown])
    # 回流的小结 (现有的 pushed / hits / contradicted / new / folded)
    tot = {}
    for x in recs:
        _sum_numeric(tot, {k: v for k, v in (x.get("summary") or {}).items() if isinstance(v, (int, float))})
    res["learn_summary"] = tot
    agg = {}
    for sim in sims:
        for st in sim["stats"]:
            _sum_numeric(agg, st)
    res["store_stats_sum"] = agg
    return res


def load_families(group: str, path: str) -> list:
    files = [Path(path)] if path else [FAM_DIR / f"{g}.json" for g in (["dev", "heldout"] if group == "both" else [group])]
    fams = []
    for f in files:
        fams += json.loads(f.read_text(encoding="utf-8"))["families"]
    return fams


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="dev", choices=["dev", "heldout", "both"])
    ap.add_argument("--families", default="", help="另给一个题族文件 (覆盖 --group)")
    ap.add_argument("--cols", default="orig,off,shadow,on")
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--runs", type=int, default=6)
    ap.add_argument("--p-adopt", type=float, default=1.0)
    ap.add_argument("--shared", default="both", choices=["per-family", "shared", "both"])
    ap.add_argument("--days-between", type=float, default=1.0)
    ap.add_argument("--draft", default="", help="新代码的草稿目录")
    ap.add_argument("--bad-policy", default="both", choices=["last", "skip", "both"], help="假她怎么对待试过没用的修法: last 排最后才试 / skip 一律不试")
    ap.add_argument("--set", action="append", default=[], help="覆盖 procedures.py 里的常量, 如 --set AUTO_PRE=0.5 (扫参数用; 不给就一个字不变)")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    allfams = load_families(a.group, a.families)
    fams = [f for f in allfams if not f.get("after_dev_look")]      # 主结论只算盲的; 看过开发组结果以后补写的单独列
    late = [f for f in allfams if f.get("after_dev_look")]
    impls = load_impls(a.draft)
    Pm = sys.modules.get("wm.agent.procedures")
    for kv in a.set:
        k, v = kv.split("=", 1)
        if Pm is None or not hasattr(Pm, k):
            raise SystemExit(f"--set {kv}: procedures 里没有这个常量")
        setattr(Pm, k, float(v))
    cols = {"orig": (impls["orig"], None), "off": (impls["new"], "off"), "shadow": (impls["new"], "shadow"),
            "on": (impls["new"], "on")}
    modes = ["per-family", "shared"] if a.shared == "both" else [a.shared]
    report = {"args": vars(a), "families": [f["id"] for f in fams], "sha": {"bench": sha(Path(__file__))}, "results": {}}
    for col in a.cols.split(","):
        mod, pmode = cols[col]
        if pmode is not None and not supports_procedures(mod):
            report["results"][col] = "跳过: 这份实现还没有 agent.procedures"
            continue
        report["results"][col] = {}
        for policy in (["last", "skip"] if a.bad_policy == "both" else [a.bad_policy]):
            BAD_POLICY[0] = policy
            for m in modes:
                key = f"{m}|{policy}"
                report["results"][col][key] = {}
                for name, group in (("main", fams), ("after_dev_look", late)):
                    if not group:
                        continue
                    sims = [simulate(group, seed, mod, pmode, a.runs, a.p_adopt, m == "shared", a.days_between) for seed in range(a.seeds)]
                    report["results"][col][key][name] = summarize(sims, group, a.runs)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()

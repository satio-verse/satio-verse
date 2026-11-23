"""端咖啡评测台 (小窝的物理与后果 P0, 见 docs/roadmap/room-physics.md): 在网页版无界面评估的客厅里, 把桌上的书当马克杯
(内半径 4 cm、内高 9 cm) 拿起来端到茶几放下, 液体按手里杯子每帧的运动算 (web/physics/liquid.js), 比较不同条件下洒多少、
在哪一段洒的 (拿起、端到身前、走、放下)、落在哪、烫到哪、得分多少. 场景是 web/motion-eval.js 的 carry.
自己起一个模拟服务 (--tts mock), 跑完关掉.

  python tools/bench_carry.py                       # 全套: 端法 0 / 1 x 装量 half / full x 走法 slow / normal / turn / stop (热),
                                                    #       外加空杯、冷水; 写 logs/bench-carry-<时间>.json 和 .md
  python tools/bench_carry.py --quick               # 只跑满杯热咖啡 normal, 端法 0 / 1
  python tools/bench_carry.py --choices             # 她能挑的几样 (端稳 care x 拿放快慢 hand x 装多少 fill) 的网格, 正常走、热咖啡
  python tools/bench_carry.py --care 1 --hand 0.6 --fill 0.8 --how normal   # 一组自己挑的
  python tools/bench_carry.py --scenarios "carry:full>run>hot>1,carry:full>normal>hot>care=1>hand=0.7" --seeds 1,2
  python tools/bench_carry.py --compare logs/bench-carry-A.json logs/bench-carry-B.json   # 两份结果逐行比
  (场景名: carry:装量>走法>温度>端稳, 后面可以跟 键=值 覆盖: care= hand= fill= target=)

得分 = 送到才有分: min(1, 剩下的 / 要送的量 (默认满杯 412 ml)) - 0.5 x 最重的烫伤 - 0.01 x 超过 20 秒的秒数.
不动 (没送到)、少装或先倒掉 (送到的量不够) 都拿不到高分, 磨磨蹭蹭也扣.

两条对照线: 每个场景的结果记着这一趟的动作来源和物理环境的指纹 (carry.env, 见 web/motion-eval.js 的 carryEnv). 比动作模型时
物理要固定, 比物理时动作来源要固定; --compare 逐场景比, 两边都变了的不下结论 (退出码 2), 只变了一边的说比的是哪一边.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from tune_motion import Evaluator  # noqa: E402

FILL = {"empty": "空杯", "half": "半杯", "full": "满杯 (离杯沿 8 mm)"}
HOW = {"slow": "慢走 (0.6 倍速)", "normal": "正常走", "turn": "快走 + 急转", "stop": "快走 + 急停", "run": "跑"}
PART = {"rightHand": "右手", "leftHand": "左手", "pelvis": "骨盆", "belly": "肚子", "chest": "胸口", "leftThigh": "左大腿",
        "rightThigh": "右大腿", "leftShin": "左小腿", "rightShin": "右小腿", "leftFoot": "左脚", "rightFoot": "右脚"}
LEVEL = {"wet": "湿", "hot": "烫一下", "burn": "烫伤"}


def temp_name(t: str) -> str:
    """hot 80 ℃ 咖啡、cold 22 ℃ 水, 或者写的摄氏度."""
    return {"hot": "热", "cold": "冷"}.get(t, f"{t} ℃")
# 阶段一段接一段 (motion-eval.js 的 carry): 拿起 (发拿起到拿住)、端到身前 (拿住到开始走)、走 (开始走到发放下)、放下
PHASES = [("pick", "拿起"), ("lift", "端到身前"), ("walk", "走"), ("put", "放下")]


def matrix() -> list[str]:
    out = []
    for care in ("0", "1"):
        for fill in ("half", "full"):
            for how in ("slow", "normal", "turn", "stop"):
                out.append(f"carry:{fill}>{how}>hot>{care}")
        out += [f"carry:empty>normal>hot>{care}", f"carry:full>normal>cold>{care}"]
    return out


def choices(how: str = "normal") -> list[str]:
    """她能挑的几样的网格: 端稳 0 / 1 x 拿放快慢 0.6 / 1 x 装多少 (容量的比例) 0.6 / 0.8 / 满杯."""
    return [f"carry:full>{how}>hot>care={c}>hand={h}>fill={f}" for c, h, f in itertools.product(("0", "1"), ("0.6", "1"), ("0.6", "0.8", "full"))]


def contacts(c: dict) -> str:
    if not c:
        return "—"
    worst = sorted(c.items(), key=lambda kv: -kv[1]["severity"])
    return "、".join(f"{PART.get(k, k)} {LEVEL.get(v['level'], v['level'])}" + (f" {v['severity']:.2f}" if v["level"] == "burn" else "")
                    for k, v in worst[:3])


def fill_name(c: dict) -> str:
    f = c["fill"]
    return FILL.get(f, f"{float(f):.0%} ({c['ml']:g} ml)" if f.replace(".", "", 1).isdigit() else f)


def report(rows: list[dict], seeds: list[int]) -> str:
    lines = [f"# 端咖啡评测台 {datetime.now():%Y-%m-%d %H:%M}", "",
             "桌上的书当马克杯 (内半径 4 cm、内高 9 cm, 满杯 412 ml), 拿起来端到茶几放下. 热的是 80 ℃ 咖啡, 冷的是 22 ℃ 水. "
             f"端稳 0 = 手跟着髋部 (原来那样), 1 = 端稳 (行动层 carryCare); 拿放快慢是行动层的 handSpeed. 种子 {seeds}, 多个种子取平均.", "",
             "| 端稳 | 拿放快慢 | 装量 | 走法 | 温度 | 送到 | 剩 (ml) | 洒 (ml) | "
             + " | ".join(f"{n}洒" for _, n in PHASES) + " | 落在 | 碰到身上 | 用时 (秒) | 得分 |",
             "|" + "---|" * (12 + len(PHASES))]
    for r in rows:
        c = r["carry"]
        lands = "、".join(f"{'地上' if k == 'floor' else k} {v:g}" for k, v in sorted(c["lands"].items(), key=lambda kv: -kv[1])) or "—"
        ph = c.get("phases", {})
        lines.append(f"| {c['care']} | {c.get('hand', 1)} | {fill_name(c)} | {HOW.get(c['how'], c['how'])} | {temp_name(c['temp'])} | "
                     f"{'是' if c['delivered'] else '否'} | {c['left']:g} | {c['spilled']:g} | "
                     + " | ".join(f"{ph.get(k, 0):g}" for k, _ in PHASES)
                     + f" | {lands} | {contacts(c['contacts'])} | {c['secs']:g} | {c['score']:g} |")
    lines += ["", "得分 = 送到才有分: min(1, 剩下的 / 要送的量) - 0.5 x 最重的烫伤 (0-1) - 0.01 x 超过 20 秒的秒数. "
              "要送的量默认满杯 412 ml: 少装、先倒掉、空杯都拿不到高分.", ""]
    return "\n".join(lines)


def average(results: list[dict]) -> dict:
    """同一场景几个种子的 carry 段取平均 (数值项平均, 每段洒的平均, 其余取第一个)."""
    first = dict(results[0]["carry"])
    if len(results) == 1:
        return first
    n = len(results)
    for k in ("left", "spilled", "spilledFrac", "secs", "score", "worstBurn", "maxSloshMm"):
        first[k] = round(sum(r["carry"][k] for r in results) / n, 3)
    first["phases"] = {k: round(sum(r["carry"].get("phases", {}).get(k, 0) for r in results) / n, 2) for k, _ in PHASES}
    first["delivered"] = all(r["carry"]["delivered"] for r in results)
    return first


def compare(a_path: str, b_path: str) -> int:
    """两份结果逐场景比. 每个场景各自记着动作来源和物理环境的指纹 (carry.env): 只变了一边的, 说比的是哪一边;
    两边都变了的不下结论. 有这样的场景 (或者没记 env 的旧结果) 时返回 2."""
    a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (a_path, b_path))
    rb = {r["scenario"]: r["carry"] for r in b["rows"]}
    rc = 0
    print("| 场景 | 比的是 | 剩 A -> B (ml) | 洒 A -> B | 得分 A -> B | 送到 A -> B |")
    print("|---|---|---|---|---|---|")
    for r in a["rows"]:
        c, d = r["carry"], rb.get(r["scenario"])
        if not d:
            continue
        ea, eb = c.get("env"), d.get("env")
        if not ea or not eb:
            what, rc = "没记 env (2026-10-27 以前的), 不下结论", 2
        else:
            dm, dp = ea["motion"]["id"] != eb["motion"]["id"], ea["physics"]["id"] != eb["physics"]["id"]
            if dm and dp:
                what, rc = "动作和物理都变了, 不下结论 (先固定一边比)", 2
            else:
                what = "动作来源 (物理固定)" if dm else "物理 (动作来源固定)" if dp else "同一套环境"
        print(f"| {r['scenario']} | {what} | {c['left']:g} -> {d['left']:g} | {c['spilled']:g} -> {d['spilled']:g} | "
              f"{c['score']:g} -> {d['score']:g} | {c['delivered']} -> {d['delivered']} |")
    return rc


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenarios", default=None, help="逗号分隔的 carry 场景名 (默认全套)")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--choices", action="store_true", help="她能挑的几样的网格")
    ap.add_argument("--care", default=None, help="自己挑一组: 端稳 0-1")
    ap.add_argument("--hand", default=None, help="自己挑一组: 拿放快慢 0.4-1.5")
    ap.add_argument("--fill", default=None, help="自己挑一组: 装多少 (容量比例 0-1 或 empty / half / full)")
    ap.add_argument("--how", default="normal", help="走法 (自己挑一组、--choices 用)")
    ap.add_argument("--temp", default="hot")
    ap.add_argument("--seeds", default="1")
    ap.add_argument("--avatar", default="silvercat.vrm")
    ap.add_argument("--url", default=None, help="连已经在跑的服务 (默认自己起一个模拟服务)")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--tabs", type=int, default=3)
    ap.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"), help="两份结果逐行比 (不跑)")
    a = ap.parse_args()
    if a.compare:
        sys.exit(compare(*a.compare))
    if a.scenarios:
        names = a.scenarios.split(",")
    elif a.choices:
        names = choices(a.how)
    elif a.care is not None or a.hand is not None or a.fill is not None:
        opts = [f"{k}={v}" for k, v in (("care", a.care), ("hand", a.hand), ("fill", a.fill)) if v is not None]
        names = [f"carry:full>{a.how}>{a.temp}>" + ">".join(opts)]
    else:
        names = ["carry:full>normal>hot>0", "carry:full>normal>hot>1"] if a.quick else matrix()
    seeds = [int(s) for s in a.seeds.split(",")]
    ev = Evaluator(a.url, a.port, a.tabs, a.avatar)
    try:
        res = ev.run([{"params": {}, "seeds": seeds, "scenarios": [n]} for n in names])
    finally:
        ev.close()
    rows, raw = [], []
    for n, r in zip(names, res):
        got = [x for x in r["results"] if x.get("carry")]
        fails = [x["reason"] for x in r["results"] if x.get("reason")]
        raw.append({"scenario": n, "results": r["results"]})
        if not got:
            print(f"{n}: 没有结果 ({'; '.join(map(str, fails))})")
            continue
        rows.append({"scenario": n, "carry": average(got), "fails": fails})
    md = report(rows, seeds)
    out = ROOT / "logs"
    out.mkdir(exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    (out / f"bench-carry-{stamp}.json").write_text(json.dumps({"seeds": seeds, "rows": rows, "raw": raw}, ensure_ascii=False, indent=1),
                                                   encoding="utf-8")
    envs = {json.dumps(r["carry"].get("env"), ensure_ascii=False, sort_keys=True) for r in rows if r["carry"].get("env")}
    md += "\n" + "\n".join(f"环境: {e}" for e in sorted(envs)) + "\n"
    (out / f"bench-carry-{stamp}.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"结果: {out / f'bench-carry-{stamp}.md'}")


if __name__ == "__main__":
    main()

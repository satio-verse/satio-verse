"""动作成长 (docs/roadmap/motion-growth.md) 的练习: 把她日常里记下的动作失败片段 (小屋 desktop/web/room-growth.js 发给服务端,
存在用户数据区, 见 wm/motion_growth.py) 聚成问题, 在执行评估里复现, 只调和这类失败有关的参数 (CMA-ES, 复用 tools/tune_motion.py),
验证过 (没见过的种子上全套回归不比现在差、失败场景明显变好、三个形象都过、动作没变慢) 才换上; 调参救不了的写问题报告给开发.

  python tools/motion_growth.py problems                     # 日常里的动作缺陷聚成问题 (按失败率 x 严重程度排); 环境约束单列 (交给她自己学)
  python tools/motion_growth.py repro <问题>                   # 在评估里复现 (现在用的参数, 训练种子, 训练形象)
  python tools/motion_growth.py sensitivity <问题>             # 相关的参数各推到两头, 看这个毛病跟不跟着变 (不跟着变 = 调参救不了)
  python tools/motion_growth.py tune <问题> [--gens 8]         # 只调相关的参数, 出候选 (不换上)
  python tools/motion_growth.py validate <候选.json>            # 没见过的种子 + 全套回归 + 三个形象, 结果写回候选
  python tools/motion_growth.py adopt <候选.json>               # 验证通过的候选换上 (写一版 vNNNN, 她闲下来说一句)
  python tools/motion_growth.py rollback [factory|v0002]       # 退回 (不给就是上一版)
  python tools/motion_growth.py watch                          # 盯日常的失败率, 换上以后变差就自动退回
  python tools/motion_growth.py report <问题>                  # 问题报告 (带复现命令), 写进数据区的 reports/
  python tools/motion_growth.py run [--force]                  # 整套, 服务端在用户允许、电脑空闲时起它: 先重新验证要验证的版本, 再挑一个问题:
                                                                复现 -> 灵敏度 -> 调 -> 验证 -> 换上, 或者出报告. 跑的时候每 15 秒查一次空闲, 用户回来就停,
                                                                调到一半停下的下次接着调.
共用: --data-dir (默认和服务端一样), --url (已经在跑的模拟服务; 不给就自己起一个, 用完关), --port, --tabs,
      --avatars 训练用的形象 (默认 silvercat.vrm,miomio.vrm,wendy.vrm), --val-avatars 验证另外加的 (默认没有), --seeds 1,2, --val-seeds 3,4,5,11,
      --cost hinge|blend 训练代价, --no-guard 训练时不跑守门场景 (对照用).

问题的名字: 种类@本事/细分, 比如 put.slam@carry/phys (端着刚体的杯子往下放时手砸下去)、grab.gap@carry/floor (从地上拿东西隔空取物).
细分: phys 手里拿着交给物理的东西; floor / low / mid / high 拿放的台面高度 (< 0.15 / 0.6 / 1.0 m 以上); - 不是拿放.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from wm import motion_growth as MG  # noqa: E402

_spec = importlib.util.spec_from_file_location("tune_motion", ROOT / "tools" / "tune_motion.py")
tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tm)

PROBE = "motion-growth-probe.js"
# 验证的门槛
SUITE_TOL = 0.01   # 全套回归的代价 (现在 = 1) 最多 1.01
SCEN_TOL = 0.05    # 单个场景最多变差 5%
SLOW_TOL = 0.10    # 每个场景的时长最多长 10% (不许靠"做得慢"躲开毛病)
GAIN = 0.25        # 失败场景里这个毛病的平滑量至少少 25%
INSENSITIVE = 0.85  # 灵敏度: 每个参数推到两头, 最好的也只少不到 15% -> 调参救不了
MIN_N = 3          # 日常里至少记了这么多次的问题才练 (一两次的先攒着)
CALIB_GENS = 4     # 新形象 (还没有自己那一版) 第一次调: 有限的预算, 从基础参数开始

# 每种毛病和哪些参数有关 (web/motion-params.js 的名字前缀). 空的 = 不是参数能调的 (代码结构上的, 直接出报告).
# 她自己挑的端法 (carryCare、handSpeed、走多快) 不在里面: 那是认知那边按经验挑的, 调参不许替她改成"一直慢慢来".
KEYS = {
    "grab.gap": ["tune.blendAction", "tune.grabReplant", "filter.hand.acc", "filter.arm.acc"],
    "hand.jump": ["tune.blendAction", "filter.hand.acc", "filter.arm.acc", "filter.arm.euro.0", "tune.handoffPick"],
    "put.slam": ["tune.blendAction", "filter.hand.acc", "filter.arm.acc", "tune.grabReplant", "tune.handoffPut"],
    # 动作会话: 拿放的几个毛病是交接方式上的; 2026-10-27 它把交接的时长做成了两个键 (handoffPick 拿住以后送到端着的位置,
    # handoffPut 放下前从端姿走到落点). 实验 E12: 只有过渡和平滑上限时只对 wendy 有用
    "held.acc": ["tune.blendAction", "filter.hand.acc", "filter.arm.acc", "tune.handoffPick", "tune.handoffPut"],
    "hold.gap": ["tune.blendAction", "filter.hand.acc", "filter.arm.acc", "contact.spread"],
    "arm.push": ["contact.spread", "filter.arm.acc", "filter.hand.acc", "tune.blendGestureIn", "tune.lapClear"],
    "self.pen": ["contact.spread", "filter.arm.acc", "filter.hand.acc", "tune.lapClear"],
    "leg.pen": ["tune.lockIn", "tune.lockOut", "tune.replant", "filter.leg.acc", "tune.hipDrop"],
    "foot.slide": ["tune.lockIn", "tune.lockOut", "tune.lockSettle", "tune.replant", "filter.leg.acc"],
    "floating": ["tune.groundRaise", "tune.groundLower", "tune.lockSettle", "tune.hipDrop"],
    "balance": ["tune.balance"],
    "item.pen": ["tune.blendAction", "tune.grabReplant"],
    "jerk": ["filter.arm.acc", "filter.hand.acc", "filter.core.acc", "filter.hips.acc", "filter.leg.acc", "tune.blendMatch", "tune.blendIdle"],
    "joint": [],
    "furniture": {"sit": ["tune.riseTau", "tune.lapClear", "tune.blendAction"], "carry": ["tune.grabReplant", "tune.blendAction"]},
}


def keys_for(kind: str, family: str) -> list[str]:
    k = KEYS.get(kind, [])
    return k.get(family, []) if isinstance(k, dict) else k


def band(y) -> str:
    if y is None:
        return "-"
    return "floor" if y < 0.15 else "low" if y < 0.6 else "mid" if y < 1.0 else "high"


def problem_of(rec: dict) -> str | None:
    """失败片段 -> 问题名; 环境约束、没分出来的 -> None. 后果 (洒、脱手) 是动作缺陷的症状时归到原因那一种."""
    if rec.get("cls") != "motion":
        return None
    kind = rec.get("cause") if rec.get("kind") in ("spill", "drop", "slip") else rec.get("kind")
    if not kind:
        return None
    ctx = rec.get("ctx") or {}
    fam = rec.get("family") or "stand"
    phys = any(h.get("physical") for h in ctx.get("held") or [])
    sub = "phys" if phys else band((ctx.get("action") or {}).get("y"))
    return f"{kind}@{fam}/{sub}"


def split_problem(pid: str) -> tuple[str, str, str]:
    kind, rest = pid.split("@", 1)
    fam, sub = (rest.split("/", 1) + ["-"])[:2]
    return kind, fam, sub


def scenarios_for(pid: str, examples: list[dict] | None = None, scene: dict | None = None) -> list[str]:
    """问题 -> 评估场景 (web/scenes/room.json 里的): 小屋的台面按高度对到评估场景里最接近的那一类."""
    kind, fam, sub = split_problem(pid)
    if fam == "carry":
        if sub == "phys":
            return ["carry:full>normal>hot>phys=1"]
        scene = scene or json.loads((ROOT / "web" / "scenes" / "room.json").read_text(encoding="utf-8"))
        items = {o["id"]: o["item"] for o in scene["objects"] if o.get("item")}
        places = {o["id"]: o["place"][1] for o in scene["objects"] if o.get("place")}
        floor_item = next((i for i, it in items.items() if it.get("on") == "floor"), None)
        mid_item = next((i for i, it in items.items() if it.get("on") != "floor"), None)
        acts = [((e.get("ctx") or {}).get("action") or {}).get("kind") for e in examples or []]
        picking = acts.count("pick") > acts.count("put")
        by = lambda lo, hi: sorted([p for p, y in places.items() if lo <= y < hi], key=lambda p: places[p])  # noqa: E731
        low, mid, high = by(0.15, 0.6), by(0.6, 1.0), by(1.0, 9)
        if picking:  # 拿起出的毛病: 从对应高度拿, 放到最矮的台面
            item = floor_item if sub in ("floor", "low") else mid_item
            to = next((p for p in low + mid if p != items.get(item, {}).get("on")), None)
        else:        # 放下出的毛病: 放到对应高度的台面
            dest = {"floor": low, "low": low, "mid": mid, "high": high}.get(sub, low) or low
            item = mid_item if sub in ("low", "floor", "high") else floor_item
            to = next((p for p in dest if p != items.get(item, {}).get("on")), None)
        return [f"pickput:{item}>{to}"] if item and to else ["pickput"]
    return {"sit": ["sit"], "walk": ["walk"], "stand": ["idle"], "arms": ["gesture", "emote"]}.get(fam, ["idle"])


# -- 评估 ------------------------------------------------------------------------------------
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class GrowthEval(tm.Evaluator):
    """tune_motion 的评估器, 多两样: 模拟服务的动作成长数据指到同一个数据区 (现在用的参数 = 她现在那一版), 评估页面载入探针."""

    def __init__(self, data_dir: Path, url: str | None, port: int | None, tabs: int, log_dir: Path):
        self.server = None
        if not url:
            port = port or _free_port()
            url = f"http://127.0.0.1:{port}"
            # 不占显卡 (9880 的 TTS 和 8784 的模型已经把 6 GB 用得差不多了): 引擎都是替身, 面部 (a2f) 关掉, 看不到 CUDA;
            # 评估的浏览器是软件渲染 (motion_eval.mjs 的 --use-angle=swiftshader)
            self.server = subprocess.Popen(
                [sys.executable, "-m", "wm", "--llm", "mock", "--asr", "none", "--tts", "mock", "--rvc", "mock", "--port", str(port),
                 "--set", 'memory.vault=""', "--set", f"trace.log_dir={log_dir.as_posix()}", "--set", "face.engine=none",
                 "--set", "llm.prewarm=false", "--set", "channels.qq.enabled=false",
                 "--set", f"motion.growth.data_dir={data_dir.as_posix()}"],
                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, "CUDA_VISIBLE_DEVICES": "-1"})
            from wm import localhttp  # 直连本机, 不走代理 (环境变量里有代理、没有 no_proxy 时本机地址也会被送去代理)
            for _ in range(90):
                try:
                    localhttp.open_direct(f"{url}/api/status", timeout=2)
                    break
                except OSError:
                    time.sleep(1)
            else:
                self.server.kill()
                raise SystemExit(f"模拟服务 {url} 没起来")
        self.url = url
        cmd = ["node", str(ROOT / "tools" / "motion_eval.mjs"), "--url", url, "--serve", "--tabs", str(tabs), "--probe", PROBE]
        self.proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
                                     env={**os.environ, "CUDA_VISIBLE_DEVICES": "-1"})
        self.next = 0

    def kill(self):
        for p in (self.proc, self.server):
            if p and p.poll() is None:
                MG._kill_tree(p.pid)


def excess_of(res: dict, kind: str, only: list[str] | None = None) -> tuple[float, int]:
    """一次评估结果里这种毛病的平滑量 (各条结果平均) 和记了几次 (动作缺陷; 洒和脱手按原因算). only: 只算这些场景."""
    ex, n = [], 0
    for r in res["results"]:
        if only is not None and r["scenario"] not in only:
            continue
        g = r.get("growth") or {}
        ex.append(float((g.get("excess") or {}).get(kind, 0)))
        n += int(((g.get("faults") or {}).get(kind) or {}).get("n", 0))
        n += sum(1 for x in g.get("records") or [] if x.get("kind") in ("spill", "drop", "slip") and x.get("cause") == kind)
    return (float(np.mean(ex)) if ex else 0.0), n


def frames_of(res: dict) -> dict[str, float]:
    out: dict[str, list] = {}
    for r in res["results"]:
        out.setdefault(r["scenario"], []).append(r.get("frames", 0))
    return {k: float(np.mean(v)) for k, v in out.items()}


HINGE = 5.0  # hinge 代价: 失败场景里痛觉和像真人的代价比现在高出 0.5% 以上的部分乘这么多


# 守门场景: 训练时和失败场景一起跑, 只看它们别变差 (同一类参数会影响到的别的本事). 实验 E4、E5: 只用走路场景调脚打滑,
# 拿放变差 11-13% 训练时看不到
# 拿放的还要带上端刚体杯子的两组 (默认端法、她挑的端稳放慢): 交接调短了拿放看着还行, 满杯会全洒 (动作会话量的: 交接 0.4 / 0.3 秒
# 时 silvercat 端稳也洒 412 ml)
CARRY_GUARD = ["carry:full>normal>hot>phys=1", "carry:full>normal>hot>care=1>phys=1>hand=0.6"]
SUITE = {"idle", "walk", "sit", "pickput", "gesture", "talk"}  # 默认全套 (web/scene-tasks.js 的 defaultScenarios), 验证本来就跑
GUARD = {"walk": ["sit", "pickput"], "carry": ["walk", "pickput", *CARRY_GUARD], "sit": ["walk", "pickput"], "stand": ["walk", "gesture"],
         "arms": ["idle", "talk"]}


def guards_for(pid: str, scen: list[str]) -> list[str]:
    """守门场景: 这类本事的那几个, 去掉和失败场景同一类的 (失败场景是 pickput:book>coffee_table 时不再加默认的 pickput)."""
    kinds = {s.split(":", 1)[0] for s in scen}
    return [g for g in GUARD.get(split_problem(pid)[1], []) if g.split(":", 1)[0] not in kinds]


# -- 验收清单 (contract) -------------------------------------------------------------------------
# 参数和"它必须通过哪些检查"一起存. 以后重新验证 (代码、出厂参数变了, 重启以后) 用"原清单 ∪ 现在的强制检查":
# 适用范围可以缩小, 验收不能悄悄变少 (2026-10-27 评审: 原来 adopt_doc 没存 guard / scenarios, 重验时会漏掉端刚体杯子).
GATES = {"suite_tol": SUITE_TOL, "scen_tol": SCEN_TOL, "slow_tol": SLOW_TOL, "gain": GAIN}
# 门槛的算法版本: 算法改了 (比如 2: 失败只罚比基线多的), 按旧算法得出的核对结果不复用
GATE_VERSION = MG.GATE_VERSION
COVERAGE_VERSION = 1
TRAJECTORY_SCHEME = "pose-items-dual-fnv32-q1e6-v1"
# E18 已观察到的两条正常拿球分支. 仅约束 ball 场景, 不推断书/其他动作也要用这两段.
COVERAGE_REQUIRED = [{"prefix": "pickput:ball>", "library": "actions", "kind": "pick",
                      "names": ["cmu69_68_pick", "cmu69_69_pick"]},
                     {"scenario": "pickput", "library": "actions", "kind": "pick",
                      "names": ["cmu69_68_pick", "cmu69_69_pick"]}]  # 当前 room 默认 pickput 是 ball -> coffee_table
# 2026-10-28 三形象默认/显式拿球正常选择的覆盖检查, 只按覆盖选定, 未参考参数收益.
# 新场景仍须逐次检查实际片段, 这两组不保证所有形象/场景都覆盖齐全.
VALIDATION_SEEDS = [3, 4, 5, 11]
CONFIRM_SEEDS = [6, 7, 8, 9]
# 确认用的动作变体 (和训练、验证用的场景不同的目标), 按本事; 当守门看 (不许变差)
VARIANTS = {"carry": ["pickput:ball>sofa", "pickput:book>bookshelf"], "walk": ["walk:window", "walk:door"],
            "sit": ["sit:sofa"], "stand": ["idle60"], "arms": ["hand"]}


def _union(*lists) -> list:
    out = []
    for xs in lists:
        for x in xs or []:
            if x not in out:
                out.append(x)
    return out


def check_seed_split(validation, confirmation, training=None) -> None:
    """已知训练/验证/确认种子各自分开; 旧记录缺训练历史时不凭空声称它没用过某个 seed."""
    groups = [("验证", validation), ("确认", confirmation), ("训练", training or [])]
    for i, (left, xs) in enumerate(groups):
        for right, ys in groups[i + 1:]:
            if overlap := sorted(set(xs) & set(ys)):
                raise SystemExit(f"{left}/{right}种子重叠 {overlap}, 请显式重新分组; 不自动挑选种子")


def make_contract(pid: str, scenarios, guard, val_seeds, avatars, train_seeds=None) -> dict:
    """一版参数的验收清单: 失败场景、守门场景、默认全套、验证种子、门槛、适用形象、确认样本 (种子 + 动作变体)."""
    kind, fam, _ = split_problem(pid)
    validation = _union(val_seeds, VALIDATION_SEEDS)
    check_seed_split(validation, CONFIRM_SEEDS, train_seeds)
    return {"problem": pid, "objective": {"metric": "excess", "kind": kind}, "scenarios": list(scenarios), "guard": list(guard),
            "suite": sorted(SUITE), "val_seeds": validation, "train_seeds": list(train_seeds or []), "avatars": list(avatars), "gates": dict(GATES),
            "gate_version": GATE_VERSION,
            "coverage": {"version": COVERAGE_VERSION, "mode": "normal", "required": list(COVERAGE_REQUIRED)},
            "confirm": {"seeds": list(CONFIRM_SEEDS), "variants": list(VARIANTS.get(fam, []))}}


def effective_contract(stored: dict | None, pid: str, avatars=None) -> dict:
    """原验收清单 ∪ 现在的强制检查: 场景、守门、种子、确认样本取并集, 门槛取更严的, 适用形象照原来的.
    stored 没有 (旧格式的版本): 按现在的规矩补全, 标 legacy (不当成它满足过这些)."""
    s = stored or {}
    scen = s.get("scenarios") or scenarios_for(pid, [])
    now = make_contract(pid, scen, guards_for(pid, scen), VALIDATION_SEEDS, s.get("avatars") or avatars or [], s.get("train_seeds"))
    g0, g1 = s.get("gates") or {}, now["gates"]
    gates = {k: (max if k == "gain" else min)(g0.get(k, v), v) for k, v in g1.items()}
    c0 = s.get("confirm") or {}
    out = {**now, "scenarios": _union(s.get("scenarios"), now["scenarios"]), "guard": _union(s.get("guard"), now["guard"]),
            "val_seeds": _union(s.get("val_seeds"), now["val_seeds"]), "gates": gates, "avatars": s.get("avatars") or now["avatars"],
            "confirm": {"seeds": _union(c0.get("seeds"), now["confirm"]["seeds"]), "variants": _union(c0.get("variants"), now["confirm"]["variants"])},
            "coverage": {**now["coverage"], "required": _union((s.get("coverage") or {}).get("required"), now["coverage"]["required"])},
            "legacy": not stored}
    check_seed_split(out["val_seeds"], out["confirm"]["seeds"], out["train_seeds"])
    return out


def rel_cost(c: dict, b: dict) -> float:
    """验证用的代价 (和基线比): tune_motion.cost 的失败罚分按候选的失败次数直接加, 基线自己也做不成的场景 (比如 wendy 的
    pickput:ball>sofa, 改前改后都是 3 次全失败) 会被算成候选变差 10 倍. 这里只罚比基线多出来的失败 (实验 E14 查出来的;
    "多失败了"另有一道门槛)."""
    c2 = {k: {**v, "fail": max(0, v["fail"] - b.get(k, {}).get("fail", 0))} for k, v in c.items()}
    b2 = {k: {**v, "fail": 0} for k, v in b.items()}
    return tm.cost(c2, b2)


def check_contract(ev, contract: dict, values: dict, base_params: dict | None = None, confirm: bool = True) -> dict:
    """按验收清单验一遍: 验证 (清单的种子、场景、守门、门槛, 适用的每个形象) + 确认样本 (预先固定的种子 + 动作变体当守门).
    返回 {pass, validation, confirmation}."""
    check_seed_split(contract["val_seeds"], (contract.get("confirm") or {}).get("seeds") or [], contract.get("train_seeds"))
    cand = {"problem": contract["problem"], "values": values, "scenarios": contract["scenarios"], "guard": contract["guard"],
            "coverage": contract.get("coverage")}
    v = validate(ev, cand, contract["val_seeds"], contract["avatars"], base_params, contract["gates"])
    out = {"pass": v["pass"], "validation": v, "confirmation": None}
    if v["pass"] and confirm and contract.get("confirm", {}).get("seeds"):
        c = contract["confirm"]
        cand2 = {**cand, "guard": _union(contract["guard"], c.get("variants"))}
        out["confirmation"] = validate(ev, cand2, c["seeds"], contract["avatars"], base_params, contract["gates"])
        out["pass"] = out["confirmation"]["pass"]
    return out


def train_cost(res: dict, base: dict, kind: str, w, mode: str = "hinge", fault_scen: list[str] | None = None) -> float:
    """训练的代价 (现在的参数 = 1). fr = 这种毛病的平滑量和现在比, std = 失败场景里痛觉和像真人的代价 (tune_motion 的, 现在 = 1).
      blend (第一版): (2 fr + std) / 3 —— 毛病占大头, 别的变差只扣一点 (实验 E4: 脚打滑少了 97%, 走路的别的痛觉坏了 8-14%, 验证拒了);
      hinge (默认): (fr + std) / 2 + HINGE x max(0, std - 1.005) —— 别的变差就重罚, 只找"毛病少了、别的不坏"的.
    哪个场景比现在慢了 10% 以上另外加罚 (不许靠做得慢躲开毛病). 每个训练形象各算一份再平均 (每个形象都不许变差)."""
    s, b = tm.per_scenario(res, w), tm.per_scenario(base, w)
    ex, _ = excess_of(res, kind, fault_scen)
    ex0, _ = excess_of(base, kind, fault_scen)
    eps = 0.05 * ex0 + 0.01
    fr, fb = frames_of(res), frames_of(base)
    slow = sum(max(0.0, fr[k] / max(1.0, fb[k]) - 1 - SLOW_TOL) for k in fb if k in fr) * 5
    f, std = (ex + eps) / (ex0 + eps), tm.cost(s, b)
    if mode == "blend":
        return (2 * f + std) / 3 + slow
    return (f + std) / 2 + HINGE * max(0.0, std - 1.005) + slow


class Stopped(Exception):
    pass


class Gate:
    """跑的时候每 15 秒查一次空闲 (键鼠 30 秒内动过、全屏、在通话 -> 停). 不查 CPU (评估自己就占着)."""

    def __init__(self, ev: GrowthEval | None, enabled: bool):
        self.ev, self.stopped, self.why = ev, False, None
        if enabled and sys.platform == "win32":
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stopped:
            time.sleep(15)
            g = MG.idle_gate(min_idle_s=30, cpu_max=None)
            if not g["ok"]:
                self.why = "; ".join(g["why"])
                self.stopped = True
                if self.ev:
                    self.ev.kill()

    def check(self):
        if self.stopped:
            raise Stopped(self.why or "停下了")


# -- 各步 ------------------------------------------------------------------------------------
def problems(g: MG.Growth, days: float = 30) -> dict:
    """失败片段 -> {problems: [{id, n, rate, sev, score, examples, keys, scenarios}], env: {...}} (按 score 排)."""
    recs = g.faults(days=days)
    act = g.active_version()
    rates = g.rates()
    by: dict = {}
    env: dict = {}
    for r in recs:
        pid = problem_of(r)
        if pid is None:
            if r.get("cls") == "env":
                env[r["kind"]] = env.get(r["kind"], 0) + 1
            continue
        by.setdefault(pid, []).append(r)
    out = []
    for pid, rs in by.items():
        kind, fam, _ = split_problem(pid)
        cur = [r for r in rs if r.get("params") == act] or rs
        exp = sum(v.get(fam, {}).get("exposure", 0) for k, v in rates.items() if k == act) or None
        sev = float(np.mean([min(10.0, r.get("sev") or 1) for r in rs]))
        rate = len(cur) / exp if exp else None
        ex = sorted(rs, key=lambda r: -(r.get("sev") or 0))[:3]
        avs: dict = {}
        for r in rs:  # 出在哪几个形象上 (小屋发来的形象文件名; 旧记录没有的按服务端的默认形象)
            f = ((r.get("avatar") or {}).get("file") if isinstance(r.get("avatar"), dict) else None) or MG.default_avatar(g.cfg) or "?"
            avs[f] = avs.get(f, 0) + 1
        out.append({"id": pid, "kind": kind, "family": fam, "n": len(rs), "n_current": len(cur), "exposure": exp, "rate": rate,
                    "avatars": avs,
                    "sev": round(sev, 2), "score": round((rate if rate is not None else len(cur) / 100) * sev, 4),
                    "first": min(r.get("got", "") for r in rs), "last": max(r.get("got", "") for r in rs),
                    "keys": keys_for(kind, fam), "scenarios": scenarios_for(pid, rs),
                    "examples": [{k: e.get(k) for k in ("got", "kind", "cls", "cause", "sev", "params", "code", "avatar", "scene")}
                                 | {"action": (e.get("ctx") or {}).get("action"), "activity": (e.get("ctx") or {}).get("activity"),
                                    "target": (e.get("ctx") or {}).get("target")} for e in ex]})
    out.sort(key=lambda p: -p["score"])
    doc = {"at": MG._now(), "active": act, "problems": out, "env": env}
    MG._write_json(g.dir / "problems.json", doc)
    return doc


def repro(ev: GrowthEval, pid: str, scen: list[str], seeds, avatars, base_params: dict | None = None) -> dict:
    """现在的参数 (base_params: 形象自己那一槽的完整参数; 不给就是页面载入的) 在评估里跑这个问题的场景: 每个形象的平滑量、
    记了几次; 至少一半 (形象 x 种子) 里记到了才算复现."""
    kind = split_problem(pid)[0]
    res = ev.run([{"params": dict(base_params or {}), "seeds": seeds, "scenarios": scen, "avatar": av} for av in avatars])
    per, hits, total = {}, 0, 0
    for av, r in zip(avatars, res):
        ex, n = excess_of(r, kind)
        per[av] = {"excess": round(ex, 4), "faults": n, "ok": sum(1 for x in r["results"] if x["ok"]), "runs": len(r["results"])}
        for x in r["results"]:
            total += 1
            g = x.get("growth") or {}
            hits += int(bool(((g.get("faults") or {}).get(kind) or {}).get("n")) or any(
                y.get("cause") == kind for y in g.get("records") or []))
    return {"problem": pid, "scenarios": scen, "seeds": seeds, "per_avatar": per, "hits": hits, "runs": total,
            "reproduced": total > 0 and hits * 2 >= total, "raw": res}


def sensitivity(ev: GrowthEval, pid: str, scen, seeds, avatar, space, base_params: dict | None = None) -> dict:
    """每个相关参数推到下限、上限 (别的不动), 看平滑量: 最好的比现在少多少. 返回 {base, points: {key: [lo 比值, hi 比值]}, best}."""
    kind = split_problem(pid)[0]
    b0 = dict(base_params or {})
    jobs = [{"params": b0, "seeds": seeds, "scenarios": scen, "avatar": avatar}]
    for p in space:
        for v in (p["lo"], p["hi"]):
            jobs.append({"params": {**b0, p["key"]: v}, "seeds": seeds, "scenarios": scen, "avatar": avatar})
    res = ev.run(jobs)
    base, _ = excess_of(res[0], kind)
    eps = 0.05 * base + 0.01
    pts = {}
    for i, p in enumerate(space):
        lo, hi = excess_of(res[1 + 2 * i], kind)[0], excess_of(res[2 + 2 * i], kind)[0]
        pts[p["key"]] = [round((lo + eps) / (base + eps), 3), round((hi + eps) / (base + eps), 3)]
    best = min((min(v) for v in pts.values()), default=1.0)
    return {"base": round(base, 4), "points": pts, "best": best, "insensitive": best > INSENSITIVE}


def tune(ev: GrowthEval, pid: str, scen, seeds, avatars, space, gens: int, popsize: int, sigma: float, run_dir: Path,
         gate: Gate, resume: dict | None = None, mode: str = "hinge", guard: list[str] | None = None,
         base_params: dict | None = None) -> dict:
    """只调 space 里的参数. 训练场景 = 失败场景 + 守门场景 (guard, 只看别变差). 返回 {values, train_cost, gen, defaults, ...};
    每一代存一份进度 (cma.json), 停下了下次接着调."""
    kind = split_problem(pid)[0]
    w = tm.pain_weights()
    guard = list(guard or [])
    train_scen = list(scen) + [g for g in guard if g not in scen]
    b0 = dict(base_params or {})
    base = ev.run([{"params": b0, "seeds": seeds, "scenarios": train_scen, "avatar": av} for av in avatars])
    keys = {p["key"] for p in space}
    defaults = {k: v for k, v in base[0]["params"].items() if k in keys}
    cma = tm.CMA(tm.to_unit(space, defaults), sigma, popsize or None)
    best = {"cost": 1.0, "values": defaults, "gen": -1}
    g0 = 0
    if resume and resume.get("problem") == pid and set(resume.get("keys", [])) == keys and resume.get("mode") == mode \
            and resume.get("guard", []) == guard and resume.get("avatars") == avatars:
        for k in ("mean", "pc", "ps", "C"):
            setattr(cma, k, np.array(resume["cma"][k]))
        cma.sigma, cma.gen = resume["cma"]["sigma"], resume["cma"]["gen"]
        best, g0 = resume["best"], resume["cma"]["gen"]
        print(f"接着上次的第 {g0} 代调", flush=True)
    logf = (run_dir / "evals.jsonl").open("a", encoding="utf-8")
    t0 = time.time()
    for g in range(g0, gens):
        gate.check()
        xs = cma.ask()
        cands = [tm.from_unit(space, x) for x in xs]
        res = ev.run([{"params": {**b0, **c}, "seeds": seeds, "scenarios": train_scen, "avatar": av} for c in cands for av in avatars])
        gate.check()
        fs = []
        for i, c in enumerate(cands):
            f = float(np.mean([train_cost(res[i * len(avatars) + j], base[j], kind, w, mode, list(scen)) for j in range(len(avatars))]))
            fs.append(f)
            logf.write(json.dumps({"gen": g, "cost": f, "params": c}, ensure_ascii=False) + "\n")
            if f < best["cost"]:
                best = {"cost": f, "values": c, "gen": g}
        logf.flush()
        cma.tell(xs, fs)
        MG._write_json(run_dir / "cma.json", {"problem": pid, "keys": sorted(keys), "best": best, "mode": mode, "guard": guard, "avatars": avatars,
                                               "cma": {"mean": cma.mean.tolist(), "pc": cma.pc.tolist(), "ps": cma.ps.tolist(),
                                                       "C": cma.C.tolist(), "sigma": cma.sigma, "gen": cma.gen}})
        print(f"第 {g + 1}/{gens} 代: 最好 {min(fs):.4f}, 中位 {float(np.median(fs)):.4f}, 至今最好 {best['cost']:.4f}, "
              f"步长 {cma.sigma:.3f}, 用时 {time.time() - t0:.0f} s", flush=True)
    return {"problem": pid, "values": best["values"], "train_cost": round(best["cost"], 4), "gen": best["gen"], "defaults": defaults,
            "keys": sorted(keys), "scenarios": scen, "guard": guard, "seeds": seeds, "avatars": avatars, "run": str(run_dir), "cost_mode": mode}


def coverage_compare(before: dict, after: dict, seeds, kind: str, gates: dict, policy: dict | None = None,
                     expected=None) -> dict:
    """同场景/seed 配对; 实际轨迹去重、实际片段覆盖和分片段回归分别报告.
    轨迹 hash 只在各自一侧去重, 优化后轨迹本来可以不同. seed 数不是独立有效样本数.
    clip 分层的指标是包含该片段的整次场景结果, 不能解释成只属于该片段帧的局部因果.
    """
    required = _union((policy or {}).get("required"), COVERAGE_REQUIRED)
    matches = lambda scenario, req: (scenario == req.get("scenario") or
                                     (bool(req.get("prefix")) and scenario.startswith(req["prefix"])))
    why, sides = [], []
    for label, result in (("before", before), ("after", after)):
        rows, report = {}, {}
        for r in result.get("results") or []:
            key = (r.get("scenario"), r.get("seed"))
            if key in rows:
                why.append(f"{label} 重复场景/seed {key}")
            rows[key] = r
            cv = (r.get("growth") or {}).get("coverage") or {}
            tr = cv.get("trajectory") or {}
            clips = cv.get("clips") or []
            ids = [c.get("id") for c in clips]
            valid = (cv.get("version") == COVERAGE_VERSION and cv.get("mode") == "normal" and
                     cv.get("frames", 0) > 0 and cv.get("frames") == r.get("frames") and
                     tr.get("scheme") == TRAJECTORY_SCHEME and isinstance(tr.get("id"), str) and len(tr["id"]) == 16 and
                     clips and all(c.get("id") and c.get("name") and c.get("kind") and c.get("library") for c in clips) and
                     isinstance(cv.get("sequence"), list) and cv["sequence"] and set(cv["sequence"]) <= set(ids))
            if not valid:
                why.append(f"{label} {key} 覆盖证据缺失/过期/诊断口径 (mode={cv.get('mode')})")
            s = report.setdefault(key[0], {"seed_count": 0, "trajectory_count": 0, "clip_count": 0,
                                           "samples": [], "clips": []})
            s["samples"].append({"seed": key[1], "trajectory": tr.get("id"), "mode": cv.get("mode"),
                                 "frames": cv.get("frames"), "clips": ids, "sequence": cv.get("sequence")})
            s["clips"] = _union(s["clips"], clips)
        for scenario, s in report.items():
            actual_seeds = {r["seed"] for r in s["samples"]}
            if actual_seeds != set(seeds):
                why.append(f"{label} {scenario} 种子不完整: {sorted(actual_seeds)} != {list(seeds)}")
            s["seed_count"] = len(actual_seeds)
            s["trajectory_count"] = len({r["trajectory"] for r in s["samples"] if r["trajectory"] and r["mode"] == "normal"})
            s["clip_count"] = len(s["clips"])
            for req in required:
                if not matches(scenario, req):
                    continue
                got = {c["name"] for c in s["clips"] if c.get("library") == req["library"] and c.get("kind") == req["kind"]}
                missing = sorted(set(req["names"]) - got)
                if missing:
                    why.append(f"{label} {scenario} 缺片段 {missing} (seed={s['seed_count']}, 轨迹={s['trajectory_count']})")
        if not rows or (expected is not None and set(report) != set(expected)):
            why.append(f"{label} 场景覆盖不完整: {sorted(report)} (要求 {expected})")
        sides.append((rows, report))
    (br, bs), (ar, ass) = sides
    if set(br) != set(ar):
        why.append("基线/候选场景与种子不能一一配对")
    # 动作片段次序必须可配对; mm 的逐帧匹配变化仍记录, 不把它当作独立 pick 分支.
    def actions(r):
        cv = (r.get("growth") or {}).get("coverage") or {}
        ids = {c.get("id") for c in cv.get("clips") or [] if c.get("library") in ("actions", "procedural")}
        return [x for x in cv.get("sequence") or [] if x in ids]
    for key in br.keys() & ar.keys():
        if actions(br[key]) != actions(ar[key]):
            why.append(f"{key} 实际动作片段次序改变, 不能按相同片段配对")
    strata = {}
    w = tm.pain_weights()
    for scenario in bs.keys() & ass.keys():
        for req in required:
            if not matches(scenario, req):
                continue
            # 除了必验两段, 运行中出现的额外 pick 分支也列入, 不用名字白名单隐藏它们.
            names = sorted({c["name"] for s in (bs[scenario], ass[scenario]) for c in s["clips"]
                            if c.get("library") == req["library"] and c.get("kind") == req["kind"]})
            for name in names:
                def select(rows):
                    return {"results": [r for (sn, _), r in rows.items() if sn == scenario and any(
                        c.get("name") == name and c.get("library") == req["library"] and c.get("kind") == req["kind"]
                        for c in ((r.get("growth") or {}).get("coverage") or {}).get("clips") or [])]}
                b, a = select(br), select(ar)
                if not b["results"] or not a["results"]:
                    why.append(f"{scenario}/{name} 一侧没有同片段样本")
                    continue
                eb, nb = excess_of(b, kind)
                ea, na = excess_of(a, kind)
                bp, ap = tm.per_scenario(b, w), tm.per_scenario(a, w)
                cost = rel_cost(ap, bp)
                fails = [sum(x["fail"] for x in p.values()) for p in (bp, ap)]
                slow = frames_of(a)[scenario] / max(1., frames_of(b)[scenario])
                entry = {"seeds": [[r["seed"] for r in x["results"]] for x in (b, a)],
                         "excess": [eb, ea], "count": [nb, na], "cost": cost, "fails": fails, "slow": slow}
                strata[f"{scenario}/{name}"] = entry
                if (ea > eb * (1 + gates["scen_tol"]) + 1e-6 or na > nb or cost > 1 + gates["scen_tol"] or
                        fails[1] > fails[0] or slow > 1 + gates["slow_tol"]):
                    why.append(f"{scenario}/{name} 分片段回归变差: {entry}")
    return {"version": COVERAGE_VERSION, "pass": not why, "why": why, "before": bs, "after": ass, "strata": strata,
            "meaning": "trajectory_count 是实际执行轨迹摘要去重数, 不是统计独立样本数; strata 是包含片段的整次场景指标"}


def validate(ev: GrowthEval, cand: dict, val_seeds, avatars, base_params: dict | None = None, gates: dict | None = None) -> dict:
    """候选 (cand["values"]) 和基准 (base_params, 默认现在用的) 在没见过的种子上比: 全套回归 (默认场景) + 这个问题的场景 + 训练时的
    守门场景 (cand["guard"], 比如拿放带上端刚体杯子的两组), 每个形象.
    通过: 每个形象都 —— 全套代价 <= 1 + SUITE_TOL、单个场景 (含失败场景、守门场景) <= 1 + SCEN_TOL、不多失败、每个场景不慢
    SLOW_TOL 以上、这个毛病的平滑量 (只算失败场景) 少 GAIN 以上且记的次数不多."""
    kind = split_problem(cand["problem"])[0]
    w = tm.pain_weights()
    base_params = base_params or {}
    gt = {**GATES, **(gates or {})}
    extra = list(cand["scenarios"]) + [g for g in cand.get("guard") or [] if g not in cand["scenarios"] and g not in SUITE]
    jobs = []
    for av in avatars:
        jobs += [{"params": base_params, "seeds": val_seeds, "avatar": av}, {"params": {**base_params, **cand["values"]}, "seeds": val_seeds, "avatar": av},
                 {"params": base_params, "seeds": val_seeds, "scenarios": extra, "avatar": av},
                 {"params": {**base_params, **cand["values"]}, "seeds": val_seeds, "scenarios": extra, "avatar": av}]
    res = ev.run(jobs)
    out, ok_all = {}, True
    for i, av in enumerate(avatars):
        sb, sc, fb, fc = res[4 * i: 4 * i + 4]
        b, c = tm.per_scenario(sb, w), tm.per_scenario(sc, w)
        suite = rel_cost(c, b)
        eb, ec = tm.per_scenario(fb, w), tm.per_scenario(fc, w)  # 失败场景和守门场景: 单个场景的门槛、失败、快慢也算它们
        b2, c2 = {**b, **eb}, {**c, **ec}
        per = {k: round(rel_cost({k: c2[k]}, {k: b2[k]}), 4) for k in b2 if k in c2}
        fails_b, fails_c = sum(x["fail"] for x in b2.values()), sum(x["fail"] for x in c2.values())
        frb, frc = {**frames_of(sb), **frames_of(fb)}, {**frames_of(sc), **frames_of(fc)}
        slow = {k: round(frc[k] / max(1.0, frb[k]), 3) for k in frb if k in frc}
        exb, nb = excess_of(fb, kind, list(cand["scenarios"]))
        exc, nc = excess_of(fc, kind, list(cand["scenarios"]))
        fault_gain = 1 - (exc + 1e-6) / (exb + 1e-6) if exb > 1e-6 else 0.0
        why = []
        coverage = {"suite": coverage_compare(sb, sc, val_seeds, kind, gt, cand.get("coverage"), sorted(SUITE)),
                    "extra": coverage_compare(fb, fc, val_seeds, kind, gt, cand.get("coverage"), extra)}
        coverage["pass"] = all(v["pass"] for v in coverage.values())
        for part in ("suite", "extra"):
            why.extend(f"覆盖 {part}: {s}" for s in coverage[part]["why"])
        if suite > 1 + gt["suite_tol"]:
            why.append(f"全套回归变差 {suite:.4f}")
        bad = {k: v for k, v in per.items() if v > 1 + gt["scen_tol"]}
        if bad:
            why.append(f"有场景变差 {bad}")
        if fails_c > fails_b:
            why.append(f"多失败了 {fails_c - fails_b} 个场景")
        slower = {k: v for k, v in slow.items() if v > 1 + gt["slow_tol"]}
        if slower:
            why.append(f"变慢了 {slower}")
        if fault_gain < gt["gain"]:
            why.append(f"这个毛病的超阈量只少了 {fault_gain:.0%} (要 {gt['gain']:.0%})")
        if nc > nb:
            why.append(f"这个毛病记的次数变多 {nb} -> {nc}")
        ok_all &= not why
        out[av] = {"pass": not why, "why": why, "suite_cost": round(suite, 4), "per_scenario": per, "fails": [fails_b, fails_c],
                   "slow": slow, "fault": {"excess": [round(exb, 4), round(exc, 4)], "count": [nb, nc], "gain": round(fault_gain, 3)},
                   "coverage": coverage, "full_before": sb["params"], "full_after": sc["params"]}
    return {"pass": ok_all, "avatars": out, "val_seeds": list(val_seeds), "gates": gt, "gate_version": GATE_VERSION,
            "scenarios": extra, "at": MG._now()}


def report(g: MG.Growth, pid: str, why: str, info: dict) -> Path:
    """调参救不了 (或者评估里复现不出来) 的问题报告: 日常里的数字、例子、复现命令、试过什么."""
    kind, fam, sub = split_problem(pid)
    probs = {p["id"]: p for p in (MG._read_json(g.dir / "problems.json", {}) or {}).get("problems", [])}
    p = probs.get(pid, {})
    scen = info.get("scenarios") or scenarios_for(pid, [])
    acts = [e.get("activity") for e in p.get("examples", []) if e.get("activity")]
    lines = [f"# 动作问题报告: {pid}", "", f"- 生成: {MG._now()} (tools/motion_growth.py)", f"- 结论: {why}",
             f"- 种类: {kind} (本事 {fam}, 细分 {sub}); 相关参数: {', '.join(keys_for(kind, fam)) or '(没有, 不是参数能调的)'}",
             f"- 日常里: {p.get('n', '?')} 次 (现在这一版 {p.get('n_current', '?')} 次, 曝光 {p.get('exposure')}), 平均 {p.get('sev', '?')} 倍阈值, "
             f"{p.get('first', '?')} 到 {p.get('last', '?')}", "", "## 例子", ""]
    for e in p.get("examples", []):
        lines.append(f"- {e.get('got')} {e.get('kind')} ({e.get('cls')}{' <- ' + e['cause'] if e.get('cause') else ''}) {e.get('sev')} 倍; "
                     f"动作 {json.dumps(e.get('action'), ensure_ascii=False)}; 活动 {e.get('activity')}; 目标 {e.get('target')}; "
                     f"形象 {(e.get('avatar') or {}).get('name')}; 参数 {e.get('params')}; 代码 {e.get('code')}")
    lines += ["", "## 复现", "", "执行评估 (无界面, 模拟服务 --tts mock):", "", "```bash",
              f"node tools/motion_eval.mjs --url http://127.0.0.1:8797 --probe {PROBE} --scenarios {','.join(scen)} --seeds 1,2,3",
              "```", "", "小屋里逐帧看 (真实的小屋页面, 自己起模拟服务):", "", "```bash",
              f"node tools/room_diag.mjs --plan do:{acts[0] if acts else 'coffee'} --hold 45 --out diag.json", "```", "",
              "## 试过的", "", "```json", json.dumps({k: v for k, v in info.items() if k != "raw"}, ensure_ascii=False, indent=1)[:6000], "```", ""]
    out = g.dir / "reports" / f"{pid.replace('/', '_').replace('@', '_at_')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


LOG_DIR = ROOT / "logs" / "motion-growth"  # 实验数据的副本 (tools/archive_kb.py 收进知识库「11 实验数据」)


def mirror(g: MG.Growth, run_dir: Path | None, out: Path = LOG_DIR) -> Path | None:
    """每次练完, 这一次的记录 (结果、候选和验证数字、复现、灵敏度、每一代、进度) 和换上 / 退回的记录 (changes.jsonl)
    另存一份到 logs/motion-growth/ —— 是实验数据, 要能复盘、写论文. 带着日常失败片段的 (faults.jsonl、曝光、problems.json 里的例子、
    问题报告里的例子) 是她的成长数据, 不拷, 只在用户数据区. 返回这一次的副本目录."""
    try:
        dst = None
        if run_dir and run_dir.is_dir():
            dst = out / "runs" / run_dir.name
            dst.mkdir(parents=True, exist_ok=True)
            for f in run_dir.iterdir():
                if f.is_file() and f.suffix in (".json", ".jsonl", ".log") and f.name != "problems.json":
                    shutil.copy2(f, dst / f.name)
        if (g.dir / "changes.jsonl").exists():
            out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(g.dir / "changes.jsonl", out / "changes.jsonl")
        return dst
    except OSError as exc:
        print(f"实验记录没另存到 {out}: {exc}", file=sys.stderr)
        return None


def adopt_doc(cand: dict, problem: dict | None, g: MG.Growth | None = None, contract: dict | None = None,
              scope: list[str] | None = None) -> dict:
    """验证过的候选 -> 换上的那一版的内容: 参数、验收清单 (acceptance, 以后重验用它 ∪ 当时的强制检查)、验证时的运行栈指纹、
    适用形象 (scope: 文件名列表; 不给 = 基础槽位, 所有形象). 数字说准: "少了 X%" 是超阈量 (程度), 次数另报;
    次数没少时她说"轻一点了, 还没完全好", 不说"练好了"."""
    v = cand["validation"]
    av0 = next(iter(v["avatars"].values()))
    kind, fam, _ = split_problem(cand["problem"])
    gains = ", ".join(f"{a} 超阈量 {x['fault']['gain']:.0%} (次数 {x['fault']['count'][0]}→{x['fault']['count'][1]})"
                      for a, x in v["avatars"].items())
    suite = ", ".join(f"{a} {x['suite_cost']}" for a, x in v["avatars"].items())
    fewer = all(x["fault"]["count"][1] < x["fault"]["count"][0] for x in v["avatars"].values())
    why = (f"日常里 {(problem or {}).get('n', '?')} 次 {cand['problem']}; 只调 {', '.join(cand['keys'])}; 验证 (没见过的种子 {v['val_seeds']}): "
           f"这个毛病{gains}, 全套回归代价 {suite}")
    contract = contract or make_contract(cand["problem"], cand.get("scenarios") or [], cand.get("guard") or [], v["val_seeds"],
                                         list(v["avatars"]))
    doc = {"keys": cand["keys"], "values": cand["values"], "full": av0["full_after"], "before": av0["full_before"], "family": fam,
           "problem": cand["problem"], "why": why, "line": MG.line_for(fam, "better" if fewer else "lighter"), "run": cand.get("run"),
           "acceptance": contract,
           "numbers": {"train_cost": cand.get("train_cost"), "metric": "超阈量 (excess) 少的比例; 次数 (count) 另报",
                        "gate_version": v.get("gate_version"),
                        "validation": {a: {k: x.get(k) for k in ("suite_cost", "fault", "fails", "slow", "coverage")} for a, x in v["avatars"].items()}}}
    if g is not None:
        doc["fingerprint"] = {a: g.fingerprint(a) for a in (scope or contract["avatars"])}
    if scope:
        doc["scope"] = {"avatars": [{"file": a, "sha": MG.avatar_sha(g.cfg, a) if g else None} for a in scope]}
    return doc


def _slot_base(g: MG.Growth, slot: str, below: bool = False) -> dict | None:
    """评估时的基准参数: 形象那一槽现在用的完整参数 (below: 那一槽下面的, 也就是基础槽位的); 基础槽位就用页面载入的 (None)."""
    if slot == MG.GLOBAL:
        return None
    if below:
        return g.full_values("base")
    e = g.active(slot)
    return g.full_values(e["version"]) if e else g.full_values("base")


def _slot_avatars(g: MG.Growth, slot: str, doc: dict, default: list[str]) -> list[str]:
    """这一槽要验哪些形象: 形象那一槽就是它自己; 基础槽位按原验收清单, 没有就默认的几个."""
    sc = [a["file"] for a in (doc.get("scope") or {}).get("avatars") or [] if a.get("file")]
    if slot != MG.GLOBAL:
        e = g.active(slot) or {}
        return [e["file"]] if e.get("file") else sc or default
    return sc or (doc.get("acceptance") or {}).get("avatars") or default


def _brief(v: dict | None) -> dict | None:
    """验证结果的摘要 (存进版本、候选): 每个形象过没过、为什么、全套、毛病的超阈量和次数."""
    if not v:
        return None
    return {"pass": v.get("pass"), "seeds": v.get("val_seeds"), "scenarios": v.get("scenarios"), "gates": v.get("gates"),
            "gate_version": v.get("gate_version"),
            "avatars": {a: {k: x.get(k) for k in ("pass", "why", "suite_cost", "per_scenario", "fault", "fails", "coverage")}
                        for a, x in (v.get("avatars") or {}).items()}}


# -- 新形象: 先体检 (基础参数下的基线和身体比例), 再有限地校准 ------------------------------------------
def calibrate(ev, g: MG.Growth, avatar: str, seeds=(1, 2)) -> dict:
    """新形象的体检: 基础参数下跑默认全套 + 端刚体杯子两组, 记下身体比例 (motion-eval.js 的 modelChecks) 和各种毛病的基线
    (超阈量、次数), 存成 avatars/<内容摘要>.json. 不调参; 以后这个形象的问题按它自己的范围调, 第一次调预算有限 (CALIB_GENS).
    这些档案也是身体比例研究 (方案 9.5) 的数据."""
    sha = MG.avatar_sha(g.cfg, avatar)
    res = ev.run([{"params": {}, "seeds": list(seeds), "avatar": avatar, "checks": True},
                  {"params": {}, "seeds": list(seeds), "scenarios": list(CARRY_GUARD), "avatar": avatar}])
    w = tm.pain_weights()
    faults: dict = {}
    for r in res:
        for x in r["results"]:
            gr = x.get("growth") or {}
            for k, v in (gr.get("excess") or {}).items():
                faults.setdefault(k, {"excess": 0.0, "count": 0})["excess"] += round(v / len(r["results"]), 4)
            for k, v in (gr.get("faults") or {}).items():
                faults.setdefault(k, {"excess": 0.0, "count": 0})["count"] += int(v.get("n", 0))
    doc = {"file": avatar, "sha": sha, "at": MG._now(), "fingerprint": g.fingerprint(avatar), "seeds": list(seeds),
           "body": ((res[0].get("checks") or {}).get("model")) or {},
           "suite": {k: round(v["pain"], 3) for k, v in tm.per_scenario(res[0], w).items()},
           "carry": {k: round(v["pain"], 3) for k, v in tm.per_scenario(res[1], w).items()},
           "fails": sum(0 if x["ok"] else 1 for r in res for x in r["results"]), "faults": faults}
    MG._write_json(g.dir / "avatars" / f"{sha}.json", doc)
    return doc


# -- 候选 (验证过、还没换上的; 换上要用户点头) -------------------------------------------------------
def candidate_add(g: MG.Growth, cand_file: Path, scope: list[str], note: str = "") -> dict:
    """把一次练习的结果登记成某几个形象的候选 (不换上). 原来那次的整体验证结果原样留着 (source.original_validation)."""
    cand = json.loads(Path(cand_file).read_text(encoding="utf-8"))
    v = cand.get("validation") or {}
    contract = make_contract(cand["problem"], cand.get("scenarios") or [], cand.get("guard") or [],
                             v.get("val_seeds") or VALIDATION_SEEDS, scope, cand.get("seeds"))
    return g.add_candidate({"problem": cand["problem"], "keys": cand["keys"], "values": cand["values"],
                            "scope": {"avatars": [{"file": a, "sha": MG.avatar_sha(g.cfg, a)} for a in scope]},
                            "acceptance": contract, "fingerprint": cand.get("fingerprint"),
                            "source": {"run": cand.get("run"), "file": str(cand_file), "train_cost": cand.get("train_cost"),
                                       "original_validation": _brief(v) if v else None},
                            "note": note})


def candidate_check(ev, g: MG.Growth, cid: str) -> dict:
    """按"原验收清单 ∪ 现在的强制检查"核对一个候选: 运行栈指纹和上次核对时一样就复用上次的结果; 不一样或者没有, 补验证 +
    确认样本 (预先固定的 CONFIRM_SEEDS 和动作变体). 只记结果, 不换上."""
    c = next((x for x in g.candidates() if x["id"] == cid), None)
    if not c:
        raise SystemExit(f"没有这个候选: {cid}")
    scope = [a["file"] for a in c["scope"]["avatars"]]
    contract = effective_contract(c.get("acceptance"), c["problem"], scope)
    fp = {a: g.fingerprint(a) for a in scope}
    baseline = g.baseline()
    last = (c.get("checks") or [None])[-1]
    if (last and last.get("fingerprint") == fp and last.get("contract") == contract and last.get("values") == c["values"] and
            last.get("baseline") == baseline):
        return {**last, "reused": True}
    chk = check_contract(ev, contract, c["values"], baseline["values"])
    if fp != {a: g.fingerprint(a) for a in scope} or baseline != g.baseline():
        raise SystemExit("核对过程中运行栈或基础槽位变了, 不保存本次确认结果")
    rec = {"at": MG._now(), "fingerprint": fp, "contract": contract, "values": dict(c["values"]),
           "baseline": baseline,
           "pass": chk["pass"], "validation": _brief(chk["validation"]),
           "confirmation": _brief(chk["confirmation"]),
           "full_after": {a: x["full_after"] for a, x in chk["validation"]["avatars"].items()},
           "full_before": {a: x["full_before"] for a, x in chk["validation"]["avatars"].items()}}
    g.update_candidate(cid, checks=(c.get("checks") or []) + [rec], status="confirmed" if chk["pass"] else "rejected")
    return rec


def candidate_adopt(g: MG.Growth, cid: str) -> dict:
    """换上一个核对过的候选 (用户点头以后). 要求: 最近一次核对通过、运行栈指纹和现在一样."""
    c = next((x for x in g.candidates() if x["id"] == cid), None)
    if not c:
        raise SystemExit(f"没有这个候选: {cid}")
    last = (c.get("checks") or [None])[-1]
    scope = [a["file"] for a in c["scope"]["avatars"]]
    if not last or not last["pass"]:
        raise SystemExit(f"{cid} 还没核对通过 (先 candidate-check)")
    if last["fingerprint"] != {a: g.fingerprint(a) for a in scope}:
        raise SystemExit(f"{cid} 核对以后运行栈变了 (代码、评估器、出厂参数或模型), 要重新核对")
    if last.get("contract") != effective_contract(c.get("acceptance"), c["problem"], scope):
        raise SystemExit(f"{cid} 核对以后验收清单变了 (加了强制检查或者门槛算法改了), 要重新核对")
    if last.get("values") != c["values"]:
        raise SystemExit(f"{cid} 核对以后候选参数变了或缺少参数证据, 要重新核对")
    if last.get("baseline") != g.baseline():
        raise SystemExit(f"{cid} 核对以后基础槽位变了或缺少基准证据, 要重新核对")
    require_current_check(last, scope)
    kind, fam, _ = split_problem(c["problem"])
    v = last["validation"]
    fewer = all(x["fault"]["count"][1] < x["fault"]["count"][0] for x in v["avatars"].values())
    a0 = scope[0]
    gains = ", ".join(f"{a} 超阈量 {x['fault']['gain']:.0%} (次数 {x['fault']['count'][0]}→{x['fault']['count'][1]})" for a, x in v["avatars"].items())
    doc = {"keys": c["keys"], "values": c["values"], "full": last["full_after"][a0], "before": last["full_before"][a0], "family": fam,
           "problem": c["problem"], "acceptance": last["contract"], "fingerprint": last["fingerprint"], "scope": c["scope"],
           "why": f"候选 {cid} ({c.get('note') or ''}) 按验收清单核对通过 (含确认样本): {gains}", "candidate": cid,
           "line": MG.line_for(fam, "better" if fewer else "lighter"), "confirmation": last["confirmation"],
           "numbers": {"metric": "超阈量 (excess) 少的比例; 次数 (count) 另报", "validation": v}}
    out = g.adopt(doc)
    g.update_candidate(cid, status="adopted", adopted=out["version"])
    return out


def require_current_check(check: dict, avatars) -> None:
    """拒绝旧 pass 布尔值充当新验收证据; 两阶段、每个形象都要有覆盖结果."""
    if not check.get("pass"):
        raise SystemExit("这个候选没通过验证 (先 validate / candidate-check), 不换上")
    for part in ("validation", "confirmation"):
        v = check.get(part) or {}
        avs = v.get("avatars") or {}
        if (not v.get("pass") or v.get("gate_version") != GATE_VERSION or set(avs) != set(avatars) or
                not all(x.get("pass") and (x.get("coverage") or {}).get("pass") and
                        all(((x.get("coverage") or {}).get(p) or {}).get("pass") and
                            ((x.get("coverage") or {}).get(p) or {}).get("version") == COVERAGE_VERSION
                            for p in ("suite", "extra")) for x in avs.values())):
            raise SystemExit(f"{part} 缺少当前算法的正式覆盖证据, 要重新验证")


def verify_file(ev, g: MG.Growth, cand: dict, seeds, avatars) -> dict:
    """CLI validate 留下核对当时的清单、参数和指纹; 不在 adopt 时重新制造证明."""
    initial = cand.get("contract") or make_contract(cand["problem"], cand.get("scenarios") or [], cand.get("guard") or [], seeds, avatars,
                                                   cand.get("seeds"))
    initial = {**initial, "val_seeds": _union(initial.get("val_seeds"), seeds),
               "train_seeds": _union(initial.get("train_seeds"), cand.get("seeds"))}
    contract = effective_contract(initial, cand["problem"], avatars)
    scope = contract["avatars"]
    fp = {a: g.fingerprint(a) for a in scope}
    baseline = g.baseline()
    chk = check_contract(ev, contract, cand["values"], baseline["values"])
    if fp != {a: g.fingerprint(a) for a in scope} or baseline != g.baseline():
        raise SystemExit("验证过程中运行栈或基础槽位变了, 本次结果不能作换上依据")
    cand.update(contract=contract, validation=chk["validation"], confirmation=chk["confirmation"],
                verification={"contract": contract, "fingerprint": fp, "baseline": baseline, "values": dict(cand["values"]),
                              "keys": list(cand["keys"]), "pass": chk["pass"]})
    return cand


def adopt_file(g: MG.Growth, cand: dict) -> dict:
    """CLI adopt 与候选列表同样 fail closed: 旧文件保留可读, 不能把历史 pass 升格为新证据."""
    proof = cand.get("verification") or {}
    contract = cand.get("contract") or {}
    scope = contract.get("avatars") or []
    if (not scope or proof.get("contract") != contract or
            contract != effective_contract(contract, cand["problem"], scope)):
        raise SystemExit("缺少当前验收清单的核对证明或清单已变, 先 validate")
    if proof.get("values") != cand.get("values") or proof.get("keys") != cand.get("keys"):
        raise SystemExit("验证以后候选参数变了或缺少参数证据, 先 validate")
    if proof.get("fingerprint") != {a: g.fingerprint(a) for a in scope}:
        raise SystemExit("缺少验证时指纹或运行栈变了, 先 validate")
    if proof.get("baseline") != g.baseline():
        raise SystemExit("缺少验证时基准或基础槽位变了, 先 validate")
    require_current_check({**cand, "pass": proof.get("pass")}, scope)
    # 使用验证时保存的指纹, 禁止拿当前指纹为旧结果背书.
    doc = adopt_doc(cand, _problem(g, cand["problem"]), contract=contract)
    doc.update(fingerprint=proof["fingerprint"], confirmation=_brief(cand["confirmation"]))
    return g.adopt(doc)


# -- 命令 ------------------------------------------------------------------------------------
def _cfg(a):
    from wm.config import load_config
    sets = list(a.set or [])
    if a.data_dir:
        sets.append(f"motion.growth.data_dir={Path(a.data_dir).as_posix()}")
    return load_config(a.config, sets)


def _ev(a, g: MG.Growth, run_dir: Path) -> GrowthEval:
    return GrowthEval(g.dir, a.url, a.port, a.tabs, run_dir / "server-logs")


def _lists(a):
    train = [v for v in a.avatars.split(",") if v]
    val = train + [v for v in a.val_avatars.split(",") if v and v not in train]
    return [int(s) for s in a.seeds.split(",")], [int(s) for s in a.val_seeds.split(",")], train, val


def _space(keys: list[str]):
    return [p for p in tm.param_space() if any(p["key"].startswith(k) for k in keys)]


def _problem(g, pid):
    for p in (MG._read_json(g.dir / "problems.json", {}) or {}).get("problems", []):
        if p["id"] == pid:
            return p
    return {"id": pid, "scenarios": scenarios_for(pid, []), "keys": keys_for(*split_problem(pid)[:2])}


def cmd_run(a, g: MG.Growth) -> dict:
    """整套 (服务端空闲时起). 每次只做一件事, 做完记 last_run."""
    if not a.force:
        if not g.state.get("auto"):
            return {"outcome": "skip", "why": "设置里没打开空闲时自己练"}
        gate0 = MG.idle_gate(float(g._g("idle_s", 600)), cpu_max=None)
        if not gate0["ok"]:
            return {"outcome": "skip", "why": "; ".join(gate0["why"])}
    seeds, vseeds, train, val = _lists(a)
    check_seed_split(_union(vseeds, VALIDATION_SEEDS), CONFIRM_SEEDS, seeds)
    run_dir = g.dir / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    if not MG._read_json(g.dir / "runner.json"):
        MG._write_json(g.dir / "runner.json", {"pid": os.getpid(), "started": MG._now(), "why": "命令行"})
    ev = _ev(a, g, run_dir)
    gate = Gate(ev, enabled=not a.force)
    result: dict = {"at": MG._now(), "run": str(run_dir)}
    try:
        st = g.state
        # 1) 要重新验证的, 都用"原验收清单 ∪ 现在的强制检查" (effective_contract), 验收只能变多:
        #    出厂参数变了停用的 (在新的出厂参数上再验一遍, 过了重新换上); 在用的版本代码变了的、没存验收清单的 (旧格式)
        for rv in g._revalidate():
            doc = g.version(rv["version"])
            if not doc:
                g._clear_revalidate(rv["version"])
                continue
            slot = rv.get("slot", MG.GLOBAL)
            contract = effective_contract(doc.get("acceptance"), doc["problem"], _slot_avatars(g, slot, doc, val))
            fp = g.version_fingerprint(doc)
            chk = check_contract(ev, contract, doc.get("values") or {}, _slot_base(g, slot))
            gate.check()
            if fp != g.version_fingerprint(doc):
                raise SystemExit("重新验证过程中运行栈变了, 保留待重核项, 不采纳本次结果")
            result.update(step="revalidate", version=rv["version"], passed=chk["pass"], contract=contract)
            if chk["pass"]:
                cand = {"problem": doc["problem"], "values": doc["values"], "keys": doc["keys"], "scenarios": contract["scenarios"],
                        "guard": contract["guard"], "validation": chk["validation"], "run": str(run_dir)}
                d = adopt_doc(cand, None, g, contract, scope=[a["file"] for a in (doc.get("scope") or {}).get("avatars") or []] or None)
                d.update(why=f"出厂参数变了以后按验收清单重新验证 {rv['version']} 通过, 重新换上: " + d["why"], line=None,
                         revalidated_from=rv["version"], confirmation=_brief(chk["confirmation"]))
                result["adopted"] = g.adopt(d)["version"]
                g._clear_revalidate(rv["version"])  # adopt 清的是新版本, 原先排队的旧版本也要出队
            else:
                g._clear_revalidate(rv["version"])
                g._change("drop", "factory" if slot == MG.GLOBAL else "base", rv["version"], doc.get("family"),
                          f"出厂参数变了以后 {rv['version']} 没通过重新验证, 不再用", _brief(chk["validation"]), slot=slot)
            return result
        code = MG.code_version(g.root)
        for slot, e in g.slots().items():
            doc = g.version(e["version"]) or {}
            legacy = (doc.get("acceptance") or {}).get("gate_version") != GATE_VERSION
            if (not legacy and not g.stack_changed(doc) and
                    (doc.get("base", {}).get("code") == code or (st.get("validated_code") or {}).get(e["version"]) == code)):
                continue
            contract = effective_contract(doc.get("acceptance"), doc["problem"], _slot_avatars(g, slot, doc, val))
            prev = e.get("prev", "factory")
            base = g.full_values(prev) if prev not in ("factory", "base") else _slot_base(g, slot, below=True)
            fp = g.version_fingerprint(doc)
            chk = check_contract(ev, contract, doc.get("full") or doc["values"], base)
            gate.check()
            if fp != g.version_fingerprint(doc):
                raise SystemExit("重新验证过程中运行栈变了, 不保存本次通过证明")
            result.update(step="revalidate-legacy" if legacy else "revalidate-code", version=e["version"], passed=chk["pass"], contract=contract)
            if chk["pass"]:
                vc = dict(st.get("validated_code") or {})
                vc[e["version"]] = code
                g._save_state(validated_code=vc)
                fp = g.version_fingerprint(doc)
                doc.setdefault("revalidated", []).append({"at": MG._now(), "code": code, "contract": contract, "fingerprint": fp,
                                                          "validation": _brief(chk["validation"]), "confirmation": _brief(chk["confirmation"])})
                doc["validated_fingerprint"] = fp
                doc["acceptance"] = contract  # 补上 / 扩大的验收清单, 以后按它验
                MG._write_json(g.dir / "params" / f"{e['version']}.json", doc)
            else:
                why = "; ".join(sum((x["why"] for x in (chk["confirmation"] or chk["validation"])["avatars"].values()), []))
                g.rollback(prev, why=f"{'旧版本补验收清单' if legacy else '代码改过以后'} {e['version']} 没通过重新验证: {why}", auto=True,
                           slot=slot)
            return result
        # 2) 新形象先体检 (基础参数下的基线); 以后它的问题按它自己的范围调
        for u in g.unprofiled():
            doc = calibrate(ev, g, u["file"])
            result.update(step="calibrate", avatar=u["file"], profile={k: doc[k] for k in ("body", "fails", "faults")})
            return result
        # 3) 挑一个问题
        doc = problems(g)
        tried = st.get("tried") or {}
        stamp = f"{MG.code_version()}|{g.active_version()}"
        todo = [p for p in doc["problems"] if tried.get(p["id"], {}).get("stamp") != stamp and p["n"] >= MIN_N]
        if a.problem:
            todo = [p for p in doc["problems"] if p["id"] == a.problem] or [_problem(g, a.problem) | {"n": 0}]
        if not todo:
            return {**result, "outcome": "idle", "why": f"没有没试过、记了 {MIN_N} 次以上的问题"}
        p = todo[0]
        pid = p["id"]
        result["problem"] = pid
        tried[pid] = {"stamp": stamp, "at": MG._now()}
        g._save_state(tried=tried)
        if not p["keys"]:
            result.update(outcome="report", report=str(report(g, pid, "这一类不是参数能调的 (代码结构上的), 交给开发", p)))
            return result
        # 适用范围: 日常里只出在一个形象上的, 只给这个形象调、验, 换上也只换那个形象的那一槽 (别的形象照旧); 出在几个形象上的, 照旧
        # 三个形象一起 (基础槽位)
        scope = list(p.get("avatars") or {}) if len(p.get("avatars") or {}) == 1 and not a.global_scope else None
        if scope:
            train = val = scope
        base_params = _slot_base(g, g.slot_of(scope[0]) or MG.GLOBAL) if scope else None
        result["scope"] = scope or "global"
        rp = repro(ev, pid, p["scenarios"], seeds, train, base_params)
        gate.check()
        result["repro"] = {k: v for k, v in rp.items() if k != "raw"}
        if not rp["reproduced"]:
            result.update(outcome="report", report=str(report(g, pid, "日常里有, 评估里复现不出来 (场景对不上, 要在小屋里查)", result["repro"])))
            return result
        space = _space(p["keys"])
        sens = sensitivity(ev, pid, p["scenarios"], seeds, train[0], space, base_params)
        gate.check()
        result["sensitivity"] = sens
        if sens["insensitive"]:
            result.update(outcome="report", report=str(report(g, pid, f"相关参数推到两头, 最好也只少 {1 - sens['best']:.0%}: 调参救不了, 要改代码",
                                                                {"repro": result["repro"], "sensitivity": sens, "scenarios": p["scenarios"]})))
            return result
        resume = None
        for old in sorted((g.dir / "runs").glob("*/cma.json"), reverse=True):
            r = MG._read_json(old)
            if r and r.get("problem") == pid and (MG._read_json(old.parent / "stamp.json", {}) or {}).get("stamp") == stamp:
                resume = r
                break
        MG._write_json(run_dir / "stamp.json", {"stamp": stamp})
        guard = [] if a.no_guard else guards_for(pid, p["scenarios"])
        # 新形象 (还没有自己那一版) 第一次调: 有限的预算, 从基础参数开始
        gens = min(a.gens, CALIB_GENS) if scope and not g.active(g.slot_of(scope[0]) or "") else a.gens
        result["gens"] = gens
        cand = tune(ev, pid, p["scenarios"], seeds, train, space, gens, a.popsize, a.sigma, run_dir, gate, resume, a.cost, guard,
                    base_params)
        contract = make_contract(pid, p["scenarios"], guard, vseeds, val, seeds)
        chk = check_contract(ev, contract, cand["values"], base_params)
        cand["validation"], cand["confirmation"], cand["contract"] = chk["validation"], chk["confirmation"], contract
        cand["fingerprint"] = {av: g.fingerprint(av) for av in val}
        MG._write_json(run_dir / "candidate.json", cand)
        result["candidate"] = str(run_dir / "candidate.json")
        if chk["pass"]:
            d = adopt_doc(cand, p, g, contract, scope)
            d["confirmation"] = _brief(chk["confirmation"])
            result.update(outcome="adopted", version=g.adopt(d)["version"])
        elif cand["validation"]["pass"]:
            result.update(outcome="rejected", why="验证过了, 确认样本 (没参与挑选的种子和动作变体) 没过: " + "; ".join(
                sum((x["why"] for x in chk["confirmation"]["avatars"].values()), [])))
        else:
            why = "; ".join(f"{av}: {', '.join(x['why'])}" for av, x in cand["validation"]["avatars"].items() if x["why"])
            gains = [x["fault"]["gain"] for x in cand["validation"]["avatars"].values()]
            if max(gains) < GAIN:
                result.update(outcome="report", report=str(report(g, pid, f"调了 {a.gens} 代, 这个毛病没明显变少 ({why}): 要改代码",
                                                                    {"repro": result["repro"], "sensitivity": sens, "candidate": cand, "scenarios": p["scenarios"]})))
            else:
                result.update(outcome="rejected", why=why)
        return result
    except Stopped as exc:
        result.update(outcome="stopped", why=str(exc))
        g._save_state(last_stop={"at": MG._now(), "why": str(exc)})
        return result
    finally:
        gate.stopped = True
        try:
            ev.close()
        except Exception:  # noqa: BLE001
            ev.kill()
        (g.dir / "runner.json").unlink(missing_ok=True)  # 服务端起的也是这个进程
        if result.get("outcome") != "skip":
            g._save_state(last_run={**{k: v for k, v in result.items() if k in ("at", "run", "problem", "outcome", "why", "version", "report", "step", "passed")}})
        MG._write_json(run_dir / "result.json", result)
        if result.get("outcome") != "skip" and not a.no_mirror:
            mirror(g, run_dir)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["problems", "repro", "sensitivity", "tune", "validate", "adopt", "rollback", "watch", "report", "run", "status",
                                    "candidates", "candidate-add", "candidate-check", "candidate-adopt"])
    ap.add_argument("arg", nargs="?", help="问题名 / 候选文件 / 退回到哪一版")
    ap.add_argument("--data-dir", help="数据区 (默认和服务端一样: ~/.worldmodel/motion-growth)")
    ap.add_argument("--config")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--url")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--tabs", type=int, default=2)
    ap.add_argument("--gens", type=int, default=8)
    ap.add_argument("--popsize", type=int, default=0)
    ap.add_argument("--sigma", type=float, default=0.2)
    ap.add_argument("--seeds", default="1,2")
    ap.add_argument("--val-seeds", default=",".join(map(str, VALIDATION_SEEDS)))
    # 三个形象都进训练 (实验 E6: 只用两个训练, 没参与的 wendy 每次都变差), 验证用没见过的种子, 三个形象都要过
    ap.add_argument("--avatars", default="silvercat.vrm,miomio.vrm,wendy.vrm")
    ap.add_argument("--val-avatars", default="")
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--force", action="store_true", help="run: 不看开关和空闲 (开发手动跑)")
    ap.add_argument("--problem", help="run: 就练这个问题 (开发手动跑)")
    ap.add_argument("--scope", default="", help="candidate-add: 候选适用的形象 (逗号分隔, models/vrm 下的文件名)")
    ap.add_argument("--note", default="", help="candidate-add: 备注")
    ap.add_argument("--global-scope", action="store_true", help="run: 只出在一个形象上的问题也按三个形象一起调 (基础槽位)")
    ap.add_argument("--cost", choices=["hinge", "blend"], default="hinge", help="训练代价 (见 train_cost)")
    ap.add_argument("--no-guard", action="store_true", help="训练时不跑守门场景 (实验 E4、E5 的做法, 对照用)")
    ap.add_argument("--no-mirror", action="store_true", help="跑完不往 logs/motion-growth/ 另存实验记录")
    a = ap.parse_args()
    g = MG.Growth(_cfg(a))
    pr = lambda x: print(json.dumps(x, ensure_ascii=False, indent=1))  # noqa: E731
    if a.cmd == "status":
        return pr(g.status())
    if a.cmd == "problems":
        doc = problems(g, a.days)
        for p in doc["problems"]:
            print(f"{p['id']:<32} {p['n']:>4} 次 (这一版 {p['n_current']}, 曝光 {p['exposure']}), {p['sev']} 倍, 分 {p['score']}, "
                  f"场景 {','.join(p['scenarios'])}, 参数 {','.join(p['keys']) or '-'}")
        print(f"环境约束 (交给她自己学, 这里不调): {doc['env'] or '没有'}")
        return None
    if a.cmd == "rollback":
        return pr(g.rollback(a.arg or True, why="命令行退回"))
    if a.cmd == "candidates":
        return pr([{k: c.get(k) for k in ("id", "status", "problem", "scope", "note", "added")} | {"checks": len(c.get("checks") or []),
                   "stale": g.candidate_stale(c)} for c in g.candidates()])
    if a.cmd == "candidate-add":
        scope = [x for x in a.scope.split(",") if x]
        if not a.arg or not scope:
            raise SystemExit("candidate-add <候选.json> --scope 形象.vrm[,形象.vrm]")
        return pr({k: v for k, v in candidate_add(g, Path(a.arg), scope, a.note).items() if k != "values"})
    if a.cmd == "candidate-adopt":
        return pr(candidate_adopt(g, a.arg))
    if a.cmd == "watch":
        return pr(g.watch())
    if a.cmd == "run":
        return pr(cmd_run(a, g))
    if not a.arg:
        raise SystemExit(f"{a.cmd} 要给问题名或候选文件")
    if a.cmd == "adopt":
        cand = json.loads(Path(a.arg).read_text(encoding="utf-8"))
        out = adopt_file(g, cand)
        if not a.no_mirror:
            mirror(g, Path(a.arg).parent)
        return pr(out)
    if a.cmd == "report":
        p = _problem(g, a.arg)
        return print(report(g, a.arg, "手动生成", p))
    seeds, vseeds, train, val = _lists(a)
    if a.cmd in ("tune", "validate", "candidate-check"):
        check_seed_split(_union(vseeds, VALIDATION_SEEDS), CONFIRM_SEEDS, seeds if a.cmd == "tune" else None)
    run_dir = g.dir / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    ev = _ev(a, g, run_dir)
    try:
        if a.cmd == "candidate-check":
            r = candidate_check(ev, g, a.arg)
            return pr({k: v for k, v in r.items() if not k.startswith("full_")})
        if a.cmd == "validate":
            cand = json.loads(Path(a.arg).read_text(encoding="utf-8"))
            verify_file(ev, g, cand, vseeds, val)
            Path(a.arg).write_text(json.dumps(cand, ensure_ascii=False, indent=1), encoding="utf-8")
            for av, x in cand["validation"]["avatars"].items():
                print(f"{av}: {'通过' if x['pass'] else '不过'} 全套 {x['suite_cost']} 毛病 {x['fault']} {'; '.join(x['why'])}")
            return None
        p = _problem(g, a.arg)
        if a.cmd == "repro":
            r = repro(ev, a.arg, p["scenarios"], seeds, train)
            MG._write_json(run_dir / "repro.json", r)
            return pr({k: v for k, v in r.items() if k != "raw"})
        space = _space(p["keys"])
        if not space:
            raise SystemExit(f"{a.arg} 没有相关参数 (不是参数能调的)")
        if a.cmd == "sensitivity":
            r = sensitivity(ev, a.arg, p["scenarios"], seeds, train[0], space)
            MG._write_json(run_dir / "sensitivity.json", r)
            return pr(r)
        cand = tune(ev, a.arg, p["scenarios"], seeds, train, space, a.gens, a.popsize, a.sigma, run_dir, Gate(None, False), mode=a.cost,
                    guard=[] if a.no_guard else guards_for(a.arg, p["scenarios"]))
        MG._write_json(run_dir / "candidate.json", cand)
        print(f"候选在 {run_dir / 'candidate.json'} (还没验证; 下一步 validate)")
        return None
    finally:
        ev.close()
        if not a.no_mirror:
            mirror(g, run_dir)


if __name__ == "__main__":
    main()

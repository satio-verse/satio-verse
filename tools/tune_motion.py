"""运动层参数的自动调优: 执行评估当仿真环境, CMA-ES 搜索 web/motion-params.js 里的参数.

  python tools/tune_motion.py                     # 起模拟服务 + 无界面浏览器, 默认 20 代 x 每代 13 个
  python tools/tune_motion.py --gens 30 --tabs 4 --apply   # 验证通过后写 models/motion/params.json (页面载入时覆盖默认值)
  python tools/tune_motion.py --validate logs/motion-tune/<run>/best.json   # 只验证一组参数
  python tools/tune_motion.py --keys tune.balance --gens 10   # 只搜名字以这些前缀开头的参数 (逗号分隔), 其余保持当前值

每次评估: 页面里跑 web/motion-eval.js 的全部场景 (待机、走路、坐下站起、拿放、手势、说话), 每个场景几个种子.
代价 (越小越好, 默认参数时 = 1):
  痛觉: web/pain.js 的每一项各自和默认参数时比 (换算成痛觉值后各加一个下限再相除: 这个场景默认时总痛觉的
        PAIN_FLOOR 倍, 默认时几乎为 0 的项不至于从 0 变成一点点就放大很多倍), 各项平均 —— 每种毛病一样重要. 只按权重加起来的话, "手臂被防穿模层推开" 一项占了
        一半到九成八, 别的 (重心、关节越界、悬空、碰家具) 基本不起作用. 平滑层抑制次数 jump 不算 (随可调参数变),
        用固定阈值的 jerk 代替;
  + 像不像真人 (motion-score.js, 和默认时比) x NAT_W;
  各场景平均, 再加上没完成的场景每个 FAIL_W.
训练用的种子和验证用的不同; 训练可以同时用几个形象 (代价取平均), 验证在训练形象和另外给的形象上跑没见过的种子,
防止调出来的参数只适合这一个模型这几个场景.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
NAT_W, FAIL_W = 1.0, 3.0
PAIN_FLOOR = 0.05  # 各项的下限: 这个场景默认参数时总痛觉的 5% (至少 5)
SKIP_TERMS = {"jump"}


def param_space() -> list[dict]:
    """web/motion-params.js 里的搜索空间 (key, lo, hi, log)."""
    src = (ROOT / "web" / "motion-params.js").read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"\{ key: '([\w.]+)', lo: ([\d.e-]+), hi: ([\d.e-]+)(, log: true)? \}", src):
        out.append({"key": m.group(1), "lo": float(m.group(2)), "hi": float(m.group(3)), "log": bool(m.group(4))})
    if not out:
        raise SystemExit("motion-params.js 里没读到参数")
    return out


def pain_weights() -> dict[str, float]:
    src = (ROOT / "web" / "pain.js").read_text(encoding="utf-8")
    return {m.group(1): float(m.group(2)) for m in re.finditer(r"^  (\w+): \{ unit: '[^']*', w: ([\d.]+)", src, re.M)}


def to_unit(space, values: dict) -> np.ndarray:
    x = []
    for p in space:
        v = values[p["key"]]
        x.append((math.log(v) - math.log(p["lo"])) / (math.log(p["hi"]) - math.log(p["lo"])) if p["log"]
                 else (v - p["lo"]) / (p["hi"] - p["lo"]))
    return np.clip(np.array(x), 0, 1)


def from_unit(space, x: np.ndarray) -> dict:
    out = {}
    for p, u in zip(space, np.clip(x, 0, 1)):
        v = math.exp(math.log(p["lo"]) + u * (math.log(p["hi"]) - math.log(p["lo"]))) if p["log"] else p["lo"] + u * (p["hi"] - p["lo"])
        out[p["key"]] = float(f"{v:.5g}")
    return out


# -- 评估 ----------------------------------------------------------------------------------
class Evaluator:
    """起 (或连) 模拟服务和 tools/motion_eval.mjs 的服务模式, 按行发任务、收结果."""

    def __init__(self, url: str | None, port: int, tabs: int, avatar: str | None):
        self.server = None
        if not url:
            url = f"http://localhost:{port}"
            self.server = subprocess.Popen(
                [sys.executable, "-m", "wm", "--llm", "mock", "--asr", "none", "--tts", "mock", "--rvc", "mock",
                 "--port", str(port), "--set", 'memory.vault=""'], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(60):
                try:
                    # 直连, 不走代理 (同 wm/localhttp.py: 环境变量里有代理、没有 no_proxy 时本机地址也会被送去代理)
                    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"{url}/api/status", timeout=2)
                    break
                except OSError:
                    time.sleep(1)
            else:
                raise SystemExit(f"模拟服务 {url} 没起来")
        cmd = ["node", str(ROOT / "tools" / "motion_eval.mjs"), "--url", url, "--serve", "--tabs", str(tabs)]
        if avatar:
            cmd += ["--avatar", avatar]
        self.proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
        self.next = 0

    def run(self, jobs: list[dict]) -> list[dict]:
        """jobs: [{params, seeds, scenarios?, avatar?}] -> 同顺序的结果 (并行跑在几个标签页里)."""
        ids = []
        for j in jobs:
            self.next += 1
            ids.append(self.next)
            self.proc.stdin.write(json.dumps({"id": self.next, **j}) + "\n")
        self.proc.stdin.flush()
        got = {}
        while len(got) < len(ids):
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("评估进程退出了")
            r = json.loads(line)
            if r.get("error"):
                raise RuntimeError(f"评估出错: {r['error']}")
            got[r["id"]] = r
        return [got[i] for i in ids]

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except Exception:  # noqa: BLE001
            self.proc.kill()
        if self.server:
            self.server.terminate()


def per_scenario(res: dict, w: dict[str, float]) -> dict[str, dict]:
    """结果 -> {场景: {pain: 总痛觉值, terms: {项: 痛觉值}, natural, fail}} (几个种子取平均)."""
    out: dict[str, dict] = {}
    for r in res["results"]:
        s = out.setdefault(r["scenario"], {"terms": {}, "natural": [], "fail": 0})
        for k, v in r["pain"].items():
            if k not in SKIP_TERMS:
                s["terms"].setdefault(k, []).append(v * w.get(k, 0))
        s["natural"].append(r["natural"])
        s["fail"] += 0 if r["ok"] else 1
    summary = {}
    for k, v in out.items():
        terms = {t: float(np.mean(x)) for t, x in v["terms"].items()}
        summary[k] = {"pain": sum(terms.values()), "terms": terms, "natural": float(np.mean(v["natural"])), "fail": v["fail"]}
    return summary


def cost(summary: dict, base: dict) -> float:
    """每个场景: 痛觉各项 (各自和默认比) 的平均 + 像真人分和默认比 x NAT_W, 除以 (1 + NAT_W); 场景平均, 加上失败."""
    c = []
    for k, b in base.items():
        s = summary[k]
        floor = max(5.0, PAIN_FLOOR * b["pain"])
        ratios = [(s["terms"].get(t, 0) + floor) / (bv + floor) for t, bv in b["terms"].items()]
        pain = float(np.mean(ratios)) if ratios else 1.0
        c.append((pain + NAT_W * s["natural"] / b["natural"]) / (1 + NAT_W) + FAIL_W * s["fail"])
    return float(np.mean(c))  # 默认参数时 = 1 (没有失败的话)


# -- CMA-ES --------------------------------------------------------------------------------
class CMA:
    """标准 CMA-ES (Hansen), 在 [0,1]^n 里搜索, 出界的样本镜像回来."""

    def __init__(self, x0: np.ndarray, sigma: float, lam: int | None = None, seed: int = 0):
        n = len(x0)
        self.n, self.mean, self.sigma = n, x0.astype(float), sigma
        self.lam = lam or 4 + int(3 * math.log(n))
        self.mu = self.lam // 2
        w = np.log(self.mu + 0.5) - np.log(np.arange(1, self.mu + 1))
        self.w = w / w.sum()
        self.mueff = 1 / np.sum(self.w ** 2)
        self.cc = (4 + self.mueff / n) / (n + 4 + 2 * self.mueff / n)
        self.cs = (self.mueff + 2) / (n + self.mueff + 5)
        self.c1 = 2 / ((n + 1.3) ** 2 + self.mueff)
        self.cmu = min(1 - self.c1, 2 * (self.mueff - 2 + 1 / self.mueff) / ((n + 2) ** 2 + self.mueff))
        self.damps = 1 + 2 * max(0, math.sqrt((self.mueff - 1) / (n + 1)) - 1) + self.cs
        self.pc, self.ps = np.zeros(n), np.zeros(n)
        self.C = np.eye(n)
        self.chin = math.sqrt(n) * (1 - 1 / (4 * n) + 1 / (21 * n * n))
        self.rng = np.random.default_rng(seed)
        self.gen = 0

    def ask(self) -> list[np.ndarray]:
        vals, vecs = np.linalg.eigh(self.C)
        self.B, self.D = vecs, np.sqrt(np.maximum(vals, 1e-20))
        xs = []
        for _ in range(self.lam):
            x = self.mean + self.sigma * self.B @ (self.D * self.rng.standard_normal(self.n))
            x = np.abs(x)                      # 镜像到 [0, 1]
            x = 1 - np.abs(1 - np.mod(x, 2))
            xs.append(x)
        return xs

    def tell(self, xs: list[np.ndarray], fs: list[float]):
        order = np.argsort(fs)
        sel = np.array([xs[i] for i in order[:self.mu]])
        old = self.mean
        self.mean = self.w @ sel
        y = (self.mean - old) / self.sigma
        Cinv_half = self.B @ np.diag(1 / self.D) @ self.B.T
        self.ps = (1 - self.cs) * self.ps + math.sqrt(self.cs * (2 - self.cs) * self.mueff) * Cinv_half @ y
        self.gen += 1
        hsig = np.linalg.norm(self.ps) / math.sqrt(1 - (1 - self.cs) ** (2 * self.gen)) / self.chin < 1.4 + 2 / (self.n + 1)
        self.pc = (1 - self.cc) * self.pc + hsig * math.sqrt(self.cc * (2 - self.cc) * self.mueff) * y
        artmp = (sel - old) / self.sigma
        self.C = ((1 - self.c1 - self.cmu) * self.C + self.c1 * (np.outer(self.pc, self.pc) + (1 - hsig) * self.cc * (2 - self.cc) * self.C)
                  + self.cmu * artmp.T @ np.diag(self.w) @ artmp)
        self.sigma *= math.exp((self.cs / self.damps) * (np.linalg.norm(self.ps) / self.chin - 1))


# -- 主流程 --------------------------------------------------------------------------------
def validate(ev: Evaluator, params: dict, seeds: list[int], avatars: list[str | None], w) -> dict:
    """默认参数和给定参数在验证种子、各个形象上各跑一遍, 返回逐场景对比."""
    jobs = []
    for av in avatars:
        jobs += [{"params": {}, "seeds": seeds, "avatar": av}, {"params": params, "seeds": seeds, "avatar": av}]
    res = ev.run(jobs)
    out = {}
    for i, av in enumerate(avatars):
        base, new = per_scenario(res[2 * i], w), per_scenario(res[2 * i + 1], w)
        out[av or "默认形象"] = {"cost": round(cost(new, base), 4), "base": base, "tuned": new,
                               "raw_base": res[2 * i]["results"], "raw_tuned": res[2 * i + 1]["results"]}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="已经在跑的模拟服务 (不给就自己起一个)")
    ap.add_argument("--port", type=int, default=8798)
    ap.add_argument("--tabs", type=int, default=4, help="并行的标签页数")
    ap.add_argument("--gens", type=int, default=20)
    ap.add_argument("--popsize", type=int, default=0)
    ap.add_argument("--sigma", type=float, default=0.15)
    ap.add_argument("--seeds", default="1,2", help="训练用的种子")
    ap.add_argument("--val-seeds", default="3,4,5")
    ap.add_argument("--avatars", default="", help="训练用的形象, 逗号分隔 (models/vrm 下的文件名; 不给就是服务端的默认形象)")
    ap.add_argument("--val-avatars", default="", help="验证时另外跑的形象, 逗号分隔 (models/vrm 下的文件名)")
    ap.add_argument("--validate", help="只验证这个文件里的参数 ({values: {...}})")
    ap.add_argument("--apply", action="store_true", help="验证通过 (所有形象的代价都 < 1) 后写 models/motion/params.json")
    ap.add_argument("--keys", default="", help="只搜名字以这些前缀开头的参数 (逗号分隔), 其余保持当前值")
    a = ap.parse_args()

    space, w = param_space(), pain_weights()
    if a.keys:
        space = [p for p in space if any(p["key"].startswith(k) for k in a.keys.split(","))]
        print(f"只搜 {len(space)} 个参数: {', '.join(p['key'] for p in space)}", flush=True)
    seeds = [int(s) for s in a.seeds.split(",")]
    vseeds = [int(s) for s in a.val_seeds.split(",")]
    train = [v for v in a.avatars.split(",") if v] or [None]
    avatars = train + [v for v in a.val_avatars.split(",") if v and v not in train]
    run_dir = ROOT / "logs" / "motion-tune" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    ev = Evaluator(a.url, a.port, a.tabs, None)
    try:
        if a.validate:
            params = json.loads(Path(a.validate).read_text(encoding="utf-8"))["values"]
            rep = validate(ev, params, vseeds, avatars, w)
            (run_dir / "validate.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
            for av, r in rep.items():
                print(f"{av}: 代价 {r['cost']}")
            return
        t0 = time.time()
        base_res = ev.run([{"params": {}, "seeds": seeds, "avatar": av} for av in train])
        defaults = {k: v for k, v in base_res[0]["params"].items() if k in {p["key"] for p in space}}
        base = [per_scenario(r, w) for r in base_res]
        for av, b in zip(train, base):
            print(f"默认参数 ({av or '默认形象'}): " + ", ".join(f"{k} 痛觉 {v['pain']:.0f} 像真人 {v['natural']:.3f}" for k, v in b.items()),
                  flush=True)
        log = (run_dir / "evals.jsonl").open("w", encoding="utf-8")
        cma = CMA(to_unit(space, defaults), a.sigma, a.popsize or None)
        best = {"cost": 1.0, "values": defaults, "summary": base, "gen": -1}
        for g in range(a.gens):
            xs = cma.ask()
            cands = [from_unit(space, x) for x in xs]
            res = ev.run([{"params": c, "seeds": seeds, "avatar": av} for c in cands for av in train])
            fs = []
            for i, c in enumerate(cands):
                s = [per_scenario(res[i * len(train) + j], w) for j in range(len(train))]
                f = float(np.mean([cost(x, b) for x, b in zip(s, base)]))  # 各训练形象的代价平均
                fs.append(f)
                log.write(json.dumps({"gen": g, "cost": f, "params": c, "summary": s}, ensure_ascii=False) + "\n")
                if f < best["cost"]:
                    best = {"cost": f, "values": c, "summary": s, "gen": g}
            log.flush()
            cma.tell(xs, fs)
            print(f"第 {g + 1}/{a.gens} 代: 最好 {min(fs):.4f}, 中位 {float(np.median(fs)):.4f}, 至今最好 {best['cost']:.4f}, "
                  f"步长 {cma.sigma:.3f}, 用时 {time.time() - t0:.0f} s", flush=True)
        (run_dir / "best.json").write_text(json.dumps({"values": best["values"], "train_cost": best["cost"], "gen": best["gen"],
                                                       "defaults": defaults, "seeds": seeds, "avatars": train},
                                                      ensure_ascii=False, indent=1),
                                           encoding="utf-8")
        rep = validate(ev, best["values"], vseeds, avatars, w)
        (run_dir / "validate.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        for av, r in rep.items():
            print(f"验证 {av}: 代价 {r['cost']} (默认 = 1)")
        if a.apply and all(r["cost"] < 1 for r in rep.values()):
            out = ROOT / "models" / "motion" / "params.json"
            out.write_text(json.dumps({"values": best["values"], "source": str(run_dir.relative_to(ROOT)),
                                       "validation": {k: r["cost"] for k, r in rep.items()},
                                       "date": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            print(f"已写入 {out.relative_to(ROOT)}")
        print(f"记录在 {run_dir.relative_to(ROOT)}")
    finally:
        ev.close()


if __name__ == "__main__":
    main()

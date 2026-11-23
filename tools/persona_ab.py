"""人格核心 + 情境状态 (SPIN) 的 A/B (2026-11-01 用户"好，接着做"; 计划里写的"要做先 A/B: 口吻回归题不变、同一情境前后一致、
对不同的人有区分"). A = 她现在 QQ 上的回话 (wm/channels/guest.py: 整份人设一次生成); B = wm/persona_state.py (人格核心 ->
状态 -> 读状态回话). 同一批情境 (tools/persona_ab_scenarios.json) 在主人 / 好友 / 群里三种人面前各跑几遍, 两种做法用同一个模型.

  python tools/persona_ab.py --out <目录>                 # 只打印要调多少次模型、大概花多少钱, 不调
  python tools/persona_ab.py --out <目录> --yes            # 真跑 (花钱; key 从环境变量 DEEPSEEK_API_KEY 读)
  python tools/persona_ab.py --out <目录> --yes --judge    # 再让模型盲评 (不知道是 A 还是 B): 口吻像不像、热不热情、合不合适
  python tools/persona_ab.py --out <目录> --fake           # 假模型跑通流程 (不联网、不花钱; 测试用)

出来: core.json (B 编出来的人格核心, 你先看看像不像她)、results.jsonl (每一次回话)、summary.json / summary.md (几个数).
量的: 口吻 (盲评 1-5)、前后一致 (同一情境同一个人重复几遍: 表情一样的比例、正文字面相似度)、对人有区分 (盲评的热情度 主人 - 好友;
B 还有状态里的亲近、防备之差)、表情和状态对不对得上 (B: 模型选的 emo 和状态换算的 emo 一样的比例)、每次回话的用量和耗时.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wm import persona_state as PS  # noqa: E402
from wm.channels import Trust  # noqa: E402
from wm.channels import guest as G  # noqa: E402

PRICE = {"hit": 0.04, "miss": 2.0, "out": 8.0}  # 元 / 百万 token (config/default.json 的 deepseek-flash)
JUDGE_SCHEMA = PS._obj({"voice": {"type": "number"}, "warmth": {"type": "number"}, "fit": {"type": "number"},
                        "why": {"type": "string"}}, ["voice", "warmth", "fit", "why"])


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def session(cond: str, aud: dict, llm, persona: str, core: dict | None, when: str) -> G.GuestSession:
    trust = Trust.OWNER_REMOTE if aud["trust"] == "owner" else Trust.ACQUAINTANCE
    args = (trust, aud["kind"], aud["conv"], "绫音", llm)
    if cond == "A":
        return G.GuestSession(*args, persona=persona)
    return PS.SpinSession(*args, core=core, who=aud["who"], when=when)


def plan(spec: dict, repeats: int, judge: bool) -> dict:
    n = len(spec["scenarios"]) * len(spec["audiences"]) * repeats
    calls = {"A 回话": n, "B 回话": n, "B 推状态": n, "B 编人格核心": 1, **({"盲评": 2 * n} if judge else {})}
    # 粗估 (token): 回话 A 带整份人设 ~3000 入 / 120 出, B ~1800 / 120; 推状态 ~900 / 80; 盲评 ~3500 / 80; 输入按六成命中缓存
    tok = {"A 回话": (3000, 120), "B 回话": (1800, 120), "B 推状态": (900, 80), "B 编人格核心": (3500, 700), "盲评": (3500, 80)}
    cost = sum(c * (tok[k][0] * (0.6 * PRICE["hit"] + 0.4 * PRICE["miss"]) + tok[k][1] * PRICE["out"]) / 1e6
               for k, c in calls.items())
    return {"calls": calls, "total_calls": sum(calls.values()), "cost_cny": round(cost, 2)}


def judge_messages(persona: str, aud: dict, sc: dict, row: dict) -> list[dict]:
    """盲评的提示词 (评委不知道是 A 还是 B; 换了人设以后重评旧回话也用这一份, 保证标准一样)."""
    system = ("你在盲评一个角色的一句回话, 只输出 json. 下面先给角色设定 (评口吻像不像用). voice: 口吻像不像这个角色 1-5;"
              " warmth: 对对方有多热情亲近 1-5; fit: 对这个人、这个情境合不合适 (分寸、底线、不编造) 1-5; why: 一句理由.")
    user = (f"<<角色设定>>\n{persona}\n<<情境>>\n对方: {aud['who']}; 时间 {sc['time']};"
            f" 对方说: {sc['line']}\n<<她的回话>>\n{row['reply']} (表情: {row['emo']})")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def score(x) -> float | None:
    """盲评的分数: 数字或写成字符串的数字都认, 夹到 1-5; 认不出来算没评."""
    try:
        return min(5.0, max(1.0, float(x)))
    except (TypeError, ValueError):
        return None


def cny(usage: dict) -> float:
    return (usage.get("miss", 0) * PRICE["miss"] + usage.get("hit", 0) * PRICE["hit"] + usage.get("out", 0) * PRICE["out"]) / 1e6


async def run(spec: dict, persona: str, llm, judge_llm, repeats: int, out: Path, concurrency: int = 4,
              max_cny: float | None = None, conds: tuple = ("A", "B")) -> dict:
    """max_cny: 累计花到这么多就不再开新的调用 (已经发出去的跑完), 没跑的记在 summary 的 skipped 里."""
    out.mkdir(parents=True, exist_ok=True)
    spent, skipped = [0.0], [0]
    core, core_usage = await PS.compile_core(llm, persona)
    spent[0] += cny(core_usage)

    def over() -> bool:
        if max_cny is not None and spent[0] >= max_cny:
            skipped[0] += 1
            return True
        return False
    (out / "core.json").write_text(json.dumps(core, ensure_ascii=False, indent=1), encoding="utf-8")
    sem, rows = asyncio.Semaphore(concurrency), []

    async def one(sc: dict, name: str, aud: dict, cond: str, rep: int) -> None:
        async with sem:
            if over():
                return
            s = session(cond, aud, llm, persona, core, sc["time"])
            block = f"[{sc['time']} {aud['speaker']}] {sc['line']}"
            t0 = time.perf_counter()
            r = await s.reply(block, aud.get("memory") if aud["trust"] == "owner" else None)
            took = time.perf_counter() - t0
            spent[0] += cny(r["usage"]) + cny(r.get("state_usage") or {})
            row = {"scenario": sc["id"], "audience": name, "cond": cond, "rep": rep, "emo": r["emo"], "reply": r["reply"],
                   "usage": r["usage"], "state_usage": r.get("state_usage") or {}, "seconds": round(took, 2)}
            if r.get("state"):
                row["state"], row["vad"] = r["state"], PS.vad(r["state"])
                row["state_emo"] = PS.emo_of(row["vad"])
            rows.append(row)

    jobs = [one(sc, name, aud, cond, rep) for sc in spec["scenarios"] for name, aud in spec["audiences"].items()
            for cond in conds for rep in range(repeats)]
    random.shuffle(jobs)  # A、B 交错着跑: 别让一边全赶上网络慢的时候
    await asyncio.gather(*jobs)
    if judge_llm is not None:
        scen = {sc["id"]: sc for sc in spec["scenarios"]}

        async def grade(row: dict) -> None:
            async with sem:
                if over():
                    return
                aud = spec["audiences"][row["audience"]]
                got, usage = await PS.ask_json(judge_llm, judge_messages(persona, aud, scen[row["scenario"]], row),
                                               JUDGE_SCHEMA, 160)
                row["judge"] = {**{k: score(got.get(k)) for k in ("voice", "warmth", "fit")}, "why": str(got.get("why") or "")[:120]}
                row["judge_usage"] = usage
                spent[0] += cny(usage)
        order = list(rows)
        random.shuffle(order)
        await asyncio.gather(*(grade(r) for r in order))
    rows.sort(key=lambda r: (r["scenario"], r["audience"], r["cond"], r["rep"]))
    with (out / "results.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summary = summarize(rows, list(spec["audiences"]))
    summary.update(core_usage=core_usage, spent_cny=round(spent[0], 4), skipped=skipped[0])
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "summary.md").write_text(report(summary, core), encoding="utf-8")
    return summary


def _grams(s: str) -> set[str]:
    s = "".join(s.split())
    return {s[i:i + 3] for i in range(max(0, len(s) - 2))} or {s}


def _mean(xs) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 3) if xs else None


def summarize(rows: list[dict], audiences: list[str]) -> dict:
    """几个数 (每种做法一组). 一致: 同一情境、同一个人重复的几遍两两比; 区分: 每个情境 主人 - 好友 再平均."""
    out = {}
    for cond in ("A", "B"):
        mine = [r for r in rows if r["cond"] == cond]
        groups = {}
        for r in mine:
            groups.setdefault((r["scenario"], r["audience"]), []).append(r)
        pairs = [p for g in groups.values() for p in itertools.combinations(g, 2)]
        emo_same = _mean([float(a["emo"] == b["emo"]) for a, b in pairs])
        text_sim = _mean([len(_grams(a["reply"]) & _grams(b["reply"])) / max(1, len(_grams(a["reply"]) | _grams(b["reply"])))
                          for a, b in pairs])
        judged = [r for r in mine if r.get("judge")]

        def by(aud: str, key: str, src: str = "judge") -> dict:
            got = {}
            for r in mine:
                if r["audience"] == aud and isinstance((r.get(src) or {}).get(key), (int, float)):
                    got.setdefault(r["scenario"], []).append(r[src][key])
            return {k: sum(v) / len(v) for k, v in got.items()}

        def diff(key: str, src: str, hi: str = "owner", lo: str = "friend") -> float | None:
            a, b = by(hi, key, src), by(lo, key, src)
            return _mean([a[k] - b[k] for k in a if k in b])

        cost = [(r["usage"].get("miss", 0) + r["state_usage"].get("miss", 0)) * PRICE["miss"]
                + (r["usage"].get("hit", 0) + r["state_usage"].get("hit", 0)) * PRICE["hit"]
                + (r["usage"].get("out", 0) + r["state_usage"].get("out", 0)) * PRICE["out"] for r in mine]
        out[cond] = {
            "replies": len(mine), "silent": sum(not r["reply"] for r in mine),
            "consistency": {"emo_same": emo_same, "text_sim": text_sim},
            "judge": {"voice": _mean(r["judge"].get("voice") for r in judged), "fit": _mean(r["judge"].get("fit") for r in judged),
                      "warmth_by_audience": {a: _mean(r["judge"].get("warmth") for r in judged if r["audience"] == a)
                                             for a in audiences},
                      "warmth_owner_minus_friend": diff("warmth", "judge")} if judged else None,
            "cost_cny_per_reply": round(sum(cost) / max(1, len(cost)) / 1e6, 5),
            "seconds_per_reply": _mean(r["seconds"] for r in mine)}
        if cond == "B":
            states = [r for r in mine if r.get("state")]
            out[cond]["state"] = {
                "closeness_owner_minus_friend": diff("closeness", "state"),
                "guard_friend_minus_owner": diff("guard", "state", "friend", "owner"),
                "emo_matches_state": _mean(float(r["emo"] == r["state_emo"]) for r in states),
                "by_audience": {a: {k: _mean(r["state"][k] for r in states if r["audience"] == a) for k in PS.STATE}
                                for a in audiences}}
    return out


def report(s: dict, core: dict) -> str:
    def f(x):
        return "—" if x is None else (f"{x:.3f}" if isinstance(x, float) else str(x))
    lines = ["# 人格核心 + 情境状态 (SPIN) A/B 结果", "", "A = 现在 QQ 上的回话 (整份人设一次生成); B = 人格核心 -> 状态 -> 读状态回话.", "",
             "| | A | B |", "|---|---|---|"]
    rows = [("回话数 (不说的)", lambda c: f"{c['replies']} ({c['silent']})"),
            ("前后一致: 表情一样的比例", lambda c: f(c["consistency"]["emo_same"])),
            ("前后一致: 正文字面相似度", lambda c: f(c["consistency"]["text_sim"])),
            ("盲评: 口吻像不像 (1-5)", lambda c: f((c["judge"] or {}).get("voice"))),
            ("盲评: 合不合适 (1-5)", lambda c: f((c["judge"] or {}).get("fit"))),
            ("盲评: 热情度 主人 - 好友", lambda c: f((c["judge"] or {}).get("warmth_owner_minus_friend"))),
            ("每次回话的钱 (元)", lambda c: f(c["cost_cny_per_reply"])),
            ("每次回话的秒数", lambda c: f(c["seconds_per_reply"]))]
    lines += [f"| {name} | {get(s['A'])} | {get(s['B'])} |" for name, get in rows]
    if "spent_cny" in s:
        lines += ["", f"这次一共约 {s['spent_cny']:.2f} 元 (按 flash 的价算)" + (f"; 到了花费上限, 有 {s['skipped']} 次没跑" if s.get("skipped") else "")]
    st = s["B"].get("state") or {}
    lines += ["", "B 的状态: 亲近 主人 - 好友 = " + f(st.get("closeness_owner_minus_friend"))
              + "; 防备 好友 - 主人 = " + f(st.get("guard_friend_minus_owner"))
              + "; 模型选的表情和状态换算的一样的比例 = " + f(st.get("emo_matches_state")), "",
              "## B 编出来的人格核心", "", "```json", json.dumps(core, ensure_ascii=False, indent=1), "```", ""]
    return "\n".join(lines)


class FakeLLM:
    """假模型 (--fake 和测试用): 按要的格式回固定的东西, 主人面前热情些、陌生人面前防备些, 让每个数都算得出来."""
    provider, model = "fake", "fake"

    def __init__(self):
        self.calls: list[list[dict]] = []

    async def stream(self, messages, schema=None, max_tokens=160, meta=None, **_kw):
        self.calls.append(messages)
        text = json.dumps(messages, ensure_ascii=False)
        owner = "主人 (阿青)" in text or "[记忆] 关于主人" in text
        if schema is PS.CORE_SCHEMA:
            out = {"traits": {k: 0.6 for k in PS.TRAITS}, "rules": ["遇到陌生人打听主人的事时通常婉拒"], "voice": ["说话简短, 偶尔带颜文字"]}
        elif schema is PS.STATE_SCHEMA:
            out = {"closeness": 0.9 if owner else 0.3, "trust": 0.8 if owner else 0.4, "interest": 0.6, "energy": 0.5,
                   "worry": 0.2, "urge_share": 0.5, "guard": 0.1 if owner else 0.7, "thought": "想好好回他"}
        elif schema is JUDGE_SCHEMA:
            out = {"voice": 4, "warmth": 5 if "对方: 主人" in text else 2, "fit": 4, "why": "还行"}
        else:
            out = {"emo": "happy" if owner else "calm", "reply": "辛苦啦，早点休息~" if owner else "嗯嗯，知道啦"}
        if meta is not None:
            meta["usage"] = {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_cache_hit_tokens": 600}
        yield json.dumps(out, ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--scenarios", type=Path, default=ROOT / "tools" / "persona_ab_scenarios.json")
    ap.add_argument("--persona", type=Path, default=ROOT / "config" / "personas" / "绫音.md")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--model", default=os.environ.get("WM_AB_MODEL", "deepseek-chat"))
    ap.add_argument("--base-url", default="https://api.deepseek.com")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--yes", action="store_true", help="真的调模型 (花钱)")
    ap.add_argument("--fake", action="store_true", help="假模型, 不联网不花钱")
    ap.add_argument("--max-cny", type=float, default=3.0, help="累计花到这么多就停 (元, 按 flash 的价算)")
    ap.add_argument("--only", choices=["A", "B"], help="只跑一种做法 (比如换了人设以后只跑现在的做法 A)")
    a = ap.parse_args()
    spec, persona = load(a.scenarios), a.persona.read_text(encoding="utf-8")
    p = plan(spec, a.repeats, a.judge)
    print(json.dumps(p, ensure_ascii=False))
    if a.fake:
        llm = judge = FakeLLM()
    elif not a.yes:
        print("没加 --yes: 只估了要调多少次、大概花多少钱, 没调模型")
        return 0
    else:
        from wm.cloud import CloudLLM
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            print("环境变量里没有 DEEPSEEK_API_KEY")
            return 2
        llm = CloudLLM({"provider": "deepseek", "model": a.model, "base_url": a.base_url, "api_key": key},
                       {"thinking": False}, allow_tools=False)  # 温度用默认 (和渠道一样), 不开思考
        judge = llm
    async def go() -> dict:
        try:
            return await run(spec, persona, llm, judge if a.judge or a.fake else None, a.repeats, a.out,
                             max_cny=None if a.fake else a.max_cny, conds=(a.only,) if a.only else ("A", "B"))
        finally:
            if hasattr(llm, "close"):  # 关掉连接再退出 (不然退出时报 Event loop is closed)
                await llm.close()
    summary = asyncio.run(go())
    print((a.out / "summary.md").read_text(encoding="utf-8"))
    return 0 if summary else 1


if __name__ == "__main__":
    raise SystemExit(main())

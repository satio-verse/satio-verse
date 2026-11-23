"""记忆体检 (记忆按脑区重设计阶段 0, 2026-11-01): 只读地数一数她的记忆现在是什么样, 给以后各阶段当对照.

  python tools/memory_checkup.py                       # 按配置读 (memory.vault、agent.home、小窝认知的经历)
  python tools/memory_checkup.py --vault <临时库> --character 小流 --json out.json
  python tools/memory_checkup.py --today 2026-12-01    # 换一天看鲜活度

数什么: 每类、每种来源多少条; 改过 / 忘掉 / 候选 / 勾了还没转正的; 事件记忆的鲜活度分布 (和检索时同一个公式:
0.5^(天数 / 半衰期), 天数从说的那天、事情那天、上次想起里取最晚的), 快淡没了的 (< 0.25) 几条; 想起过几天的分布;
候选等了多久; 她的理解、操作笔记 (成了 / 没成)、工作经验 (各状态)、小窝的经历各多少; 哪里对不上.
只读: 收集用 wm/memory_migrate.py (不写文件、不转正候选、不建库). 真库只出数字, 不把记忆原文写进报告 (--show-text 才带).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wm import memory_core as MC  # noqa: E402
from wm import memory_migrate as MM  # noqa: E402
from wm import memory_ops as O  # noqa: E402
from wm.config import load_config  # noqa: E402

BUCKETS = ((0.75, "≥ 0.75"), (0.5, "0.5–0.75"), (0.25, "0.25–0.5"), (0.0, "< 0.25 (快淡没了)"))


def checkup(result: dict, today: date, half_life: float, show_text: bool = False) -> dict:
    nodes = list(result["nodes"].values())
    out = {"today": today.isoformat(), "half_life_days": half_life, "nodes": len(nodes), "edges": len(result["edges"]),
           "chunks": result["chunks"], "types": result["types"], "origins": result["origins"], "edge_kinds": result["edge_kinds"],
           "statuses": dict(Counter(n["status"] for n in nodes)), "sources": result["sources"], "problems": result["problems"]}
    fresh, faded = Counter(), []
    for n in nodes:
        if n["type"] != "event" or n["status"] != "active":
            continue
        said = date.fromisoformat(n["day"]) if n.get("day") else None
        rec = (n["data"].get("recall") or {}).get("last")
        age = O.age_days(n["text"], said, today, date.fromisoformat(rec) if rec else None)
        f = O.freshness(age, half_life) if half_life > 0 else 1.0
        label = next(lab for lo, lab in BUCKETS if f >= lo)
        fresh[label] += 1
        if f < 0.25:
            faded.append({"age": age, "fresh": round(f, 3), **({"text": n["text"]} if show_text else {})})
    out["event_freshness"] = {lab: fresh[lab] for _, lab in BUCKETS}
    out["nearly_faded"] = sorted(faded, key=lambda x: x["fresh"])
    days = Counter(min(3, (n["data"].get("recall") or {}).get("days", 0)) for n in nodes if n["type"] in ("fact", "event"))
    out["recall_days"] = {"没想起过": days[0], "1 天": days[1], "2 天": days[2], "3 天以上": days[3]}
    waits = [(today - date.fromisoformat(n["day"])).days for n in nodes if n["type"] == "candidate" and n["status"] == "candidate" and n.get("day")]
    out["candidates_waiting_days"] = {"条数": len(waits), "最久": max(waits) if waits else None,
                                      "中位": sorted(waits)[len(waits) // 2] if waits else None}
    howto = [n for n in nodes if n["type"] == "procedure" and (n.get("file") or "").endswith(MM.M.HOWTO_FILE)]
    out["howto"] = {"成了": sum(n["data"].get("ok") is True for n in howto), "没成": sum(n["data"].get("ok") is False for n in howto),
                    "归档": sum(n["type"] == "procedure" and n["status"] == "archived" and n["id"].startswith("h:") for n in nodes)}
    out["work_experience"] = dict(Counter(n["status"] for n in nodes if n["id"].startswith("x:")))
    return out


def render(c: dict) -> str:
    lines = [f"# 记忆体检 {c['today']}（只读）", "",
             f"节点 {c['nodes']}、边 {c['edges']}；检索时切出的块 {c['chunks']}；事件半衰期 {c['half_life_days']:g} 天。", "",
             "| 种类 | 条数 |", "|---|---|"] + [f"| {MM.TYPES.get(k, k)}（{k}） | {v} |" for k, v in sorted(c["types"].items(), key=lambda kv: -kv[1])]
    lines += ["", "| 来源 | 条数 |", "|---|---|"] + [f"| {MM.ORIGINS.get(k, k)}（{k}） | {v} |" for k, v in sorted(c["origins"].items(), key=lambda kv: -kv[1])]
    lines += ["", "| 状态 | 条数 |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(c["statuses"].items(), key=lambda kv: -kv[1])]
    lines += ["", "**事件记忆的鲜活度**（活着的）：" + "，".join(f"{k} {v}" for k, v in c["event_freshness"].items()),
              "**想起过几天**（长期的事和事件）：" + "，".join(f"{k} {v}" for k, v in c["recall_days"].items()),
              f"**候选**：{c['candidates_waiting_days']['条数']} 条，最久等了 {c['candidates_waiting_days']['最久']} 天",
              f"**操作笔记**：成了 {c['howto']['成了']}、没成 {c['howto']['没成']}、归档 {c['howto']['归档']}；**工作经验**：{c['work_experience'] or '没有'}",
              "**各来源**：" + "，".join(f"{k} {v}" for k, v in sorted(c["sources"].items(), key=lambda kv: -kv[1])),
              "**边**：" + "，".join(f"{k} {v}" for k, v in sorted(c["edge_kinds"].items(), key=lambda kv: -kv[1])), "",
              "**对不上的地方**：" + ("；".join(c["problems"]) or "没有")]
    if c["nearly_faded"]:
        lines += ["", "快淡没了的事件：" + "；".join(f"{x.get('text', '（原文不显示）')}（{x['age']} 天，{x['fresh']}）" for x in c["nearly_faded"][:20])]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", help="记忆库 (默认按配置 memory.vault)")
    ap.add_argument("--character", help="角色文件夹名 (默认按配置)")
    ap.add_argument("--agent-home", help="工作经验所在的文件夹 (默认按配置 agent.home, 或 ~/.worldmodel/agent)")
    ap.add_argument("--cognition-dir", help="小窝认知的经历 (默认按配置)")
    ap.add_argument("--today", help="按哪天算鲜活度 (YYYY-MM-DD, 默认今天)")
    ap.add_argument("--json", help="另存一份 JSON")
    ap.add_argument("--show-text", action="store_true", help="快淡没了的那几条带上原文 (默认只出数字)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    a = ap.parse_args()
    cfg = load_config(overrides=a.set)
    mem = dict(cfg.get("memory") or {})
    if a.vault:
        mem["vault"] = a.vault
    if a.character:
        mem["character"] = a.character
    if not mem.get("vault"):
        print("没配记忆库: 用 --vault 指一个")
        return 1
    src = MC.sources({**cfg, "memory": mem})
    result = MM.collect(mem, Path(a.agent_home) if a.agent_home else src["agent_home"],
                        Path(a.cognition_dir) if a.cognition_dir else src["cognition_dir"])
    today = date.fromisoformat(a.today) if a.today else date.today()
    c = checkup(result, today, float((mem.get("freshness") or {}).get("half_life_days", 30) or 0), a.show_text)
    print(render(c))
    if a.json:
        Path(a.json).write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

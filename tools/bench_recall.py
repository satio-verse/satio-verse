"""记忆检索基准: 只用 BM25 / bge 向量 / Qwen3 向量, 在同一个模拟记忆库上比召回、误命中和耗时 (走真正的 Memory.recall).

  python tools/bench_recall.py                      # 三种都跑 (qwen 要先 python tools/start_llm.py --which embed)
  python tools/bench_recall.py --engines none,bge

评测集 (2026-10-23/24 写): 模拟用了几周的记忆库 (30 条关于用户的事 + 2 篇用户自己的笔记);
问句分三组, 每组有正例 (问句, 应该取到的事实里的关键词) 和负例 (不该带任何记忆: 闲聊、指令、问角色自己的事).
  开发集  定相似度门槛 (wm/embed.py 的 MIN_SIM) 用的
  测试集  门槛定好后第一次跑; 看了它的错误之后加了"跳过动作指令""只用向量", 所以不再算留出
  验收集  加那两条之前写好, 整合后跑的 (之后又修了"跳过动作指令"的判断, 也不再算纯留出)
结果写到 logs/recall-bench-<时间>.md.
  python tools/bench_recall.py --noise 200          # 干扰测试: 库里另记 200 条确认过的无关小事 (tools/memory_noise.py), 看召回和误命中变没变
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from memory_noise import leaked, noise_facts  # noqa: E402
from wm.config import load_config  # noqa: E402
from wm.embed import MIN_SIM, load_embedder  # noqa: E402
from wm.memory import Memory  # noqa: E402

FACTS = [
    "用户叫小明", "用户喜欢吃草莓", "用户不喜欢下雨天", "用户养了一只橘猫", "用户的猫叫豆豆，10月22日它生病了",
    "用户住在杭州", "用户11月要搬家到深圳", "用户最近在准备考研，压力好大", "用户妹妹10月31日结婚", "用户每天早上六点起床",
    "用户和室友吵架了，不想说话", "用户对花生过敏", "用户周末想去爬山", "用户爸爸是医生", "用户10月23日考试没考好",
    "用户在一家游戏公司做策划", "用户不吃辣", "用户最近失眠很严重", "用户打算明年考驾照", "用户老家在东北",
    "用户高中是在成都上的", "用户女朋友叫小雨", "用户每周三晚上去游泳", "用户讨厌早起", "用户10月28日要去医院复查",
    "用户喜欢听周杰伦的歌", "用户是左撇子", "用户怕狗", "用户的生日是三月十二号", "用户想学吉他",
]
NOTES = {"旅行计划.md": "# 旅行\n\n明年春天想去京都看樱花，顺便去奈良喂鹿。\n",
         "读书.md": "# 在读的书\n\n- 《三体》第二部，读到一半\n- 《人类简史》\n"}
SETS = {
    "开发集": ([("我叫什么名字来着", "小明"), ("你还记得我住哪吗", "杭州"), ("我养的是什么宠物", "橘猫"), ("我能吃坚果吗", "花生"),
              ("我那只猫叫啥", "豆豆"), ("我下个月要去哪个城市", "深圳"), ("我平时几点醒", "六点"), ("我跟谁闹别扭了", "室友"),
              ("我爸做什么工作的", "医生"), ("我在什么公司上班", "游戏公司"), ("我能吃辣的吗", "不吃辣"), ("我睡眠怎么样", "失眠"),
              ("我老家是哪里的", "东北"), ("我对象叫什么", "小雨"), ("我什么时候去游泳", "游泳"), ("我最喜欢哪个歌手", "周杰伦"),
              ("我用哪只手写字", "左撇子"), ("我生日是哪天", "三月十二"), ("我想学什么乐器", "吉他"), ("我明年春天想去哪玩", "京都")],
             ["你喜欢什么颜色", "举起右手", "今天天气不错", "给我讲个笑话", "你叫什么名字", "去沙发上坐一会儿",
              "你觉得人工智能会有感情吗", "哈哈哈", "晚安", "一加一等于几", "你会唱歌吗", "过来一下", "把球捡起来",
              "你最喜欢什么季节", "我们聊点别的吧"]),
    "测试集": ([("我有什么不能吃的东西吗", "花生"), ("我家毛孩子最近还好吗", "豆豆"), ("我是哪个城市的人来着", "杭州"),
              ("我搬家的事你还记得吗", "深圳"), ("我考研准备得怎么样了", "考研"), ("我妹妹哪天办喜事", "10月31日"),
              ("我是不是起得很早", "六点"), ("我跟舍友关系怎么样", "室友"), ("我家里人有当医生的吗", "医生"),
              ("我是做什么职业的", "策划"), ("我最近睡得好吗", "失眠"), ("我什么时候要去考驾照", "驾照"), ("我高中在哪读的", "成都"),
              ("我女朋友的名字", "小雨"), ("我怕什么动物", "怕狗"), ("我喜欢什么水果", "草莓"), ("我上次考试考得怎么样", "考试没考好"),
              ("我哪天要去医院", "医院复查"), ("我最近在看什么书", "三体"), ("我周末有什么计划", "爬山")],
             ["你最近在忙什么", "向左转", "外面下雪了吗", "给我唱首歌吧", "你今年几岁了", "去窗户那边看看", "你相信有外星人吗",
              "嗯嗯", "早上好", "三乘四是多少", "你会跳舞吗", "看着我", "把书放到书架上", "你喜欢什么动物", "换个话题吧"]),
    "验收集": ([("我对什么东西过敏来着", "花生"), ("我那只橘色的猫叫什么", "豆豆"), ("你记得我现在住在哪个城市吗", "杭州"),
              ("我要搬去哪", "深圳"), ("我最近压力大是因为什么", "考研"), ("我妹妹什么时候结婚", "结婚"), ("我每天几点起床", "六点"),
              ("我跟室友怎么了", "室友"), ("我爸爸的职业是什么", "医生"), ("我在哪上班", "游戏公司"), ("我吃不吃辣", "不吃辣"),
              ("我最近睡得着吗", "失眠"), ("我女朋友叫啥", "小雨"), ("我喜欢谁的歌", "周杰伦"), ("我想去日本哪里看樱花", "京都")],
             ["你喜欢吃什么", "抬一下左腿", "今天好热啊", "说个绕口令", "你住在哪里", "走到门口", "人为什么要睡觉",
              "嘿嘿", "拜拜", "十加十等于几", "你会画画吗", "坐到沙发上去"]),
}


def build(root: Path, noise: list | None = None) -> Memory:
    m = Memory({"vault": str(root), "character": "小流"})
    m.init()
    (root / "小流" / "记忆" / "关于用户.md").write_text(
        "# 关于用户\n\n" + "".join(f"- {f} (2026-10-2{i % 4}) #auto-memory\n" for i, f in enumerate(FACTS)), encoding="utf-8")
    for name, text in NOTES.items():
        (root / name).write_text(text, encoding="utf-8")
    for i, (fact, _) in enumerate(noise or []):  # 干扰: 9 月里确认过的无关小事, 记在对话摘要里
        m.add_summary(f"2026-09-{1 + i * 29 // len(noise):02d}", [{"text": fact, "times": ["12:00"]}], 0)
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--engines", default="none,bge,qwen")
    ap.add_argument("--noise", type=int, default=0, help="干扰测试: 库里另记这么多条无关的小事")
    ap.add_argument("--noise-seed", type=int, default=0)
    ap.add_argument("--cfg", dest="overrides", action="append", default=[], help="配置覆盖, 如 memory.embed.endpoint=http://127.0.0.1:8787")
    args = ap.parse_args()
    cfg = load_config(overrides=args.overrides)["memory"]["embed"]
    noise = noise_facts(args.noise, args.noise_seed)
    lines = [f"# 记忆检索基准 {datetime.now():%Y-%m-%d %H:%M}", ""]
    if noise:
        lines += [f"干扰: 库里另记了 {len(noise)} 条确认过的无关小事 (tools/memory_noise.py, 种子 {args.noise_seed}); "
                  "\"带进噪声\"是问句里取到了其中至少一条的次数.", ""]
    lines += ["| 检索 | 组 | 召回@3 | 排第一 | 误命中 | 平均带几条 | 检索 P50 / P95 (ms) |" + (" 带进噪声 |" if noise else ""),
              "|---|---|---|---|---|---|---|" + ("---|" if noise else "")]
    misses = []
    for engine in args.engines.split(","):
        with tempfile.TemporaryDirectory() as d:
            m = build(Path(d), noise)
            if engine != "none":
                m.embedder = load_embedder({**cfg, "engine": engine})
                m.min_sim = MIN_SIM[engine]
                t0 = time.perf_counter()
                n = m._embed_missing()
                print(f"{engine}: {n} 条记忆建向量 {time.perf_counter() - t0:.2f} s")
            for name, (pos, neg) in SETS.items():
                r3 = h1 = fp = hits = took = 0
                ms = []
                for q, key in pos:
                    r = m.recall(q)
                    ms.append(r["ms"])
                    texts = [h["text"] for h in r["hits"]]
                    r3 += any(key in t for t in texts)
                    h1 += bool(texts) and key in texts[0]
                    hits += len(texts)
                    took += bool(leaked(" ".join(texts), noise))
                    if not any(key in t for t in texts):
                        misses.append(f"{engine} {name} 漏: {q}")
                for q in neg:
                    r = m.recall(q)
                    ms.append(r["ms"])
                    took += bool(leaked(" ".join(h["text"] for h in r["hits"]), noise))
                    if r["hits"]:
                        fp += 1
                        misses.append(f"{engine} {name} 误: {q} -> {r['hits'][0]['text']}")
                ms.sort()
                row = (f"| {engine} | {name} | {r3}/{len(pos)} | {h1} | {fp}/{len(neg)} | {hits / len(pos):.1f} | "
                       f"{ms[len(ms) // 2]:.1f} / {ms[int(len(ms) * 0.95)]:.1f} |" + (f" {took}/{len(pos) + len(neg)} |" if noise else ""))
                print(row)
                lines.append(row)
    lines += ["", "逐条错误:", ""] + [f"- {x}" for x in misses]
    out = ROOT / "logs" / f"recall-bench-{datetime.now():%Y%m%d-%H%M%S}{f'-noise{len(noise)}' if noise else ''}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入 {out}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    main()

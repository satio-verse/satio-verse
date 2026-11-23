"""日志摘要 (记忆的整理): 空闲时把对话日志里"值得长期记住的、关于用户本人的事"整理出来, 写进 <库>/<角色>/记忆/对话摘要.md.

规则提取 (wm/memory.py 的 extract_facts) 只认"我叫/我喜欢/记住…"这类固定说法; "我今天考试没考好"、"下周我要去北京出差"
这类经历、心情、计划、身边的人, 要从日志里挑出来. 实测 0.8B 主模型直接生成摘要时基本是把原话按顺序照抄、取前几条,
不挑重点 ("我觉得还行吧"记了, "我对花生过敏"漏了), 所以分工改成:

  1. 规则初筛: 用户说的、带"我"、不是指令 (日志里有动作的)、不是请求 ("给我…")、不是问句, 至少 4 个汉字; 同一句只留一次
  2. 规则分类 (rule_worth): 提到家人朋友、宠物、健康、工作学业、住处、计划安排、习惯、身份 -> +1;
     一时的小事和没内容的话 ("刚才/先去/等下/还行/不知道") -> -1 (直接不要); 其他 -> 0
  3. 模型只判断"值不值得记": 逐句问"是/否", 读第一个 token 的概率 P(是) (不让它生成文字, 也就编不出东西).
     +1 的句子 P(是) 低于 veto 才否决, 0 的句子 P(是) 高于 rescue 才采用 (decide)
  4. 改写是确定的规则: "我"->"用户", "你"->角色名, 去掉语气词, "今天/明天/昨天"换成具体日期. 和已有记忆、
     同批的其他条重复的 (字的二元组重合度 >= 0.6) 不要
  5. 只追加, 每条带来源 (日期链接 + 几点几分); 进度记在 关系.md 的 digested 里, 整理过的日志不再整理,
     所以用户在 Obsidian 里删掉的不会被写回来. 每一批的候选、每句的 P(是)、采用的、拒掉的 (带原因) 都写进 logs/digest-日期.jsonl
  6. 采用的默认先写进 记忆/候选.md (memory.confirm.mode=candidates, 每天最多 daily_max 条), 用户确认了才进 对话摘要.md
     参与检索; mode=auto 时直接写进 对话摘要.md (原来的行为)
  7. 判成"重复"的说明用户这天又说到了那件事: 交给 Memory.mentioned 记一天 (候选标常提, 确认过的事件够天数升成长期)

调度: 用户最后一次说话或打字后空闲 idle_s 秒才开始, 每次一批 (默认 12 条日志); 用户一开口 (touch) 就取消正在跑的这批,
让出显卡, 这批下次重来. 用主模型的另一个槽位 (默认 2), 不挤掉对话用的槽位 0 的前缀缓存; 一句约 0.1 s.

手动补旧日志 / 试跑: python -m wm.digest --day 2026-10-23 [--dry-run]
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta

from . import memory_relay as R
from .memory import _ME, _clean_fact

log = logging.getLogger("wm.digest")

_HAN = re.compile(r"[一-鿿]")
_REQUEST = re.compile(r"^(请|麻烦)?(你)?(给|帮|让|陪|跟|教|带|替)我")
_ASK = re.compile(r"[吗嘛？?]\s*$|是不是|有没有|要不要|能不能|会不会|可不可以")
# 规则分类. 关键词按类别写, 不针对具体句子
_KEEP = re.compile(
    r"爸|妈|父亲|母亲|爷爷|奶奶|外公|外婆|姥|哥|姐|弟|妹|儿子|女儿|老婆|老公|对象|男朋友|女朋友|室友|同学|同事|老板|朋友|闺蜜|"  # 身边的人
    r"猫|狗|仓鼠|兔|鸟|鱼|宠物|"                                                                                    # 宠物
    r"过敏|生病|病了|住院|医院|感冒|发烧|失眠|受伤|手术|吃药|"                                                        # 健康
    r"工作|上班|公司|老师|学生|大学|高中|初中|专业|考|学|论文|作业|面试|项目|加班|升职|辞职|实习|毕业|"                # 工作学业
    r"下周|下个月|月底|明年|今年|周末|明天|后天|打算|准备|计划|要去|想去|"                                            # 计划安排
    r"每天|每周|每月|经常|总是|一直|平时|习惯|不吃|爱吃|吃素|"                                                        # 习惯
    r"住在|老家|搬家|家在|来自|去过|以前|小时候|^我是(?!说)")                                                         # 住处、经历、身份
_SKIP = re.compile(r"刚才|刚刚|刚把|先去|等下|等会|一会儿|待会|晚点|还行|也行|不知道|随便|想想|同意|没事|算了|跟你说个|回来了")


def rule_worth(text: str) -> int:
    """+1 像是值得记的 (提到身边的人、健康、计划…), -1 一时的小事或没内容, 0 说不准 (交给模型)."""
    if _SKIP.search(text):
        return -1
    return 1 if _KEEP.search(text) else 0


def decide(rule: int, p_yes: float | None, veto: float = 0.3, rescue: float = 0.85) -> bool:
    """规则 + 模型: 规则说值得记, 模型很确定不值得才否决; 规则说不准, 模型很确定值得才采用. 没有模型时只看规则."""
    if rule < 0:
        return False
    if p_yes is None:
        return rule > 0
    return p_yes >= (veto if rule > 0 else rescue)


def candidates(entries: list[dict]) -> list[dict]:
    """值得判断的日志条目 (规则初筛). 同一句话说了几遍只留第一次 (来源时间都记上)."""
    out, seen = [], {}
    for e in entries:
        text = e["user"].strip()
        if e.get("acts") or len(_HAN.findall(text)) < 4 or not _ME.search(text):
            continue
        if _REQUEST.search(text) or _ASK.search(text):
            continue
        if text in seen:
            seen[text]["times"].append(e["time"])
            continue
        seen[text] = c = {**e, "user": text, "times": [e["time"]]}
        out.append(c)
    return out


JUDGE = ("判断用户这句话里有没有值得长期记住的、关于用户本人的事: 身份、家人朋友、宠物、健康 (过敏、生病)、住处、工作学习、"
         "计划和安排、长期习惯、重要的经历和心情. 一时的小事 (刚吃了什么)、客套话、没有内容的话、问句都不算. 只回答 是 或 否.")
JUDGE_SHOTS = [("我下周要去北京出差", "是"), ("我刚才喝了口水", "否"), ("我对芒果过敏", "是"), ("我觉得也行", "否"),
               ("我今天被老板骂了，好难过", "是"), ("我是不是很笨", "否")]


def judge_messages(text: str) -> list[dict]:
    msgs = [{"role": "system", "content": JUDGE}]
    for q, a in JUDGE_SHOTS:
        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    msgs.append({"role": "user", "content": text})
    return msgs


_LEAD = re.compile(r"^(我跟你说|跟你说|告诉你|对了|话说|其实|哎|唉|嗯|诶)[，,、 ]*")
_TAIL = re.compile(r"[啊呀呢吧哦啦嘛哈喔~～！!。.，,、 ]+$")


def rewrite(text: str, day: str, char: str = "绫音") -> str:
    """用户原话 -> 一句事实: "我"->"用户", "你"->角色名, 去掉开头的"跟你说"和结尾的语气词, 相对日期换成具体日期."""
    t = _TAIL.sub("", _LEAD.sub("", text.strip()))
    t = re.sub(r"我们家|咱家", "用户家", t)
    t = re.sub(r"我的|俺的", "用户的", t)
    t = re.sub(r"我|俺", "用户", t).replace("你", char)
    if len(t) > 40:  # 太长就截到第二个分句
        parts = re.split(r"(?<=[，,；;])", t)
        t = _TAIL.sub("", "".join(parts[:2]))[:40]
    return _dated(t, day)


def _bigrams(text: str) -> set[str]:
    h = "".join(_HAN.findall(text.removeprefix("用户")))
    return {h[i:i + 2] for i in range(len(h) - 1)} or {h}


def _similar(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


_WEEKDAY = "一二三四五六日天"


def _dated(text: str, day: str) -> str:
    """相对时间 -> 具体日期 (按这条日志的日期): 今天/明天/昨天, 下周六/这周三, 下个月, 明年/今年/去年.
    长期记忆里留着"下周六", 过几天就不知道指哪天了."""
    d = datetime.strptime(day, "%Y-%m-%d")
    md = lambda t: f"{t.month}月{t.day}日"  # noqa: E731

    def week(m):
        monday = d - timedelta(days=d.weekday()) + timedelta(weeks=1 if m.group(1) == "下" else 0)
        return md(monday + timedelta(days=min(6, _WEEKDAY.index(m.group(2)))))

    text = re.sub(r"(下|这|本)(?:个)?(?:周|星期|礼拜)([一二三四五六日天])", week, text)
    for word, delta in (("前天", -2), ("昨天", -1), ("今天", 0), ("今晚", 0), ("明天", 1), ("后天", 2)):
        if word in text:
            text = text.replace(word, md(d + timedelta(days=delta)) + ("晚上" if word == "今晚" else ""))
    text = text.replace("下个月", f"{d.month + 1}月" if d.month < 12 else f"{d.year + 1}年1月")
    for word, dy in (("去年", -1), ("今年", 0), ("明年", 1)):
        text = text.replace(word, f"{d.year + dy}年")
    return text


def select(cands: list[dict], probs: list[float | None], day: str, known: str, char: str = "绫音",
           veto: float = 0.3, rescue: float = 0.85, relay: bool = True) -> tuple[list[dict], list[dict]]:
    """候选句 + 每句的 P(是) -> (采用的 [{text, times, p}], 拒掉的 [{text, why, p}])."""
    # 已有的记忆和候选 (候选行去掉复选框, 用户不要的去掉删除线, 一样算提过)
    known_lines = [_clean_fact(re.sub(r"^\[[ xX]\]\s*", "", x.lstrip("- ")).replace("~~", ""))
                   for x in known.splitlines() if x.startswith("- ")]
    ok, bad = [], []
    for c, p in zip(cands, probs):
        if relay and R.borrowed(c["user"]):  # 转述别人的话, 里面的"我"不是你 (改写成"用户"就错了), 不提成候选
            bad.append({"text": c["user"], "why": "转述别人的话", "rule": None, "p": None})
            continue
        r = rule_worth(c["user"])
        pr = None if p is None else round(p, 2)
        if not decide(r, p, veto, rescue):
            bad.append({"text": c["user"], "why": "一时的小事" if r < 0 else "模型判断不值得记", "rule": r, "p": pr})
            continue
        text = rewrite(c["user"], day, char)
        same = next((k for k in known_lines if _similar(text, k) >= 0.6), None)  # 又说到了已有的记忆或候选
        if same is not None or any(_similar(text, o["text"]) >= 0.6 for o in ok):
            bad.append({"text": text, "why": "重复", "rule": r, "p": pr, **({"same": same} if same is not None else {})})
            continue
        ok.append({"text": text, "times": c["times"], "rule": r, "p": pr})
    return ok, bad


class Digest:
    def __init__(self, memory, llm, cfg: dict | None = None, write_log=None):
        cfg = cfg or {}
        self.mem, self.llm = memory, llm
        self.idle_s = float(cfg.get("idle_s", 90))
        self.check_s = float(cfg.get("check_s", 15))
        self.batch = int(cfg.get("batch", 12))
        self.slot = int(cfg.get("slot", 2))
        self.veto = float(cfg.get("veto", 0.3))      # 规则说值得记, 模型 P(是) 低于这个才否决
        self.rescue = float(cfg.get("rescue", 0.85))  # 规则说不准, 模型 P(是) 高于这个才采用 (0.8B 实测 0.81 还会捞错)
        self.write_log = write_log
        self.last = time.monotonic()
        self.stats = {"runs": 0, "facts": 0, "rejected": 0, "yielded": 0, "last": None}
        self._job: asyncio.Task | None = None
        self._loop: asyncio.Task | None = None

    def touch(self) -> None:
        """用户来了 (开口或打字): 重新计空闲; 正在整理就取消, 让出显卡 (这一批下次重来)."""
        self.last = time.monotonic()
        if self._job is not None and not self._job.done():
            self._job.cancel()
            self.stats["yielded"] += 1

    def start(self) -> None:
        self._loop = asyncio.create_task(self._run())

    async def stop(self) -> None:
        for task in (self._job, self._loop):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.check_s)
            if time.monotonic() - self.last < self.idle_s:
                continue
            job = self._job = asyncio.create_task(self.run_once())
            await asyncio.wait({job})  # 被 touch 取消时不把取消传到这个循环
            if not job.cancelled() and job.exception() is not None:
                log.warning("整理日志失败, 下次空闲再试: %s", job.exception())
                self.last = time.monotonic()

    async def drain(self, max_s: float = 60.0, on_batch=None) -> dict:
        """桌宠睡觉时: 现在就把没整理的日志整理完, 不等空闲; 最多 max_s 秒. 正在跑的空闲整理先取消 (touch).
        on_batch(第几批, 一共写下几条): 每批做完回调一次 (sleep_progress). -> {"batches", "facts", "complete"}."""
        self.touch()
        t0, n, facts, rec = time.monotonic(), 0, 0, {}
        while time.monotonic() - t0 < max_s:
            rec = await self.run_once()
            if rec is None:
                break
            n += 1
            facts += len(rec["written"])
            if on_batch is not None:
                await on_batch(n, facts)
        self.last = time.monotonic()  # 整理完重新计空闲, 空闲循环不会马上又跑
        return {"batches": n, "facts": facts, "complete": rec is None}

    async def run_once(self, dry_run: bool = False, since: str | None = None) -> dict | None:
        """整理一批. 没有待整理的日志返回 None. dry_run: 不写库、不推进进度; since: "日期 条数", 从这里开始."""
        loop = asyncio.get_running_loop()
        pend = await loop.run_in_executor(None, self.mem.pending_log, self.batch, since)
        if not pend:
            return None
        day, start, entries = pend
        t0 = time.perf_counter()
        cands = candidates(entries)
        ok, bad = [], []
        if cands:
            probs = []
            for c in cands:  # 规则已经判成"一时的小事"的不用问模型
                probs.append(None if rule_worth(c["user"]) < 0 or (getattr(self.mem, "relay", True) and R.borrowed(c["user"])) else
                             (await self.llm.choice_probs(judge_messages(c["user"]), ["是", "否"], id_slot=self.slot))["是"])
            known = await loop.run_in_executor(None, self.mem.summary_known)
            ok, bad = select(cands, probs, day, known, self.mem.char, self.veto, self.rescue, getattr(self.mem, "relay", True))
        upto = start + len(entries)
        # 候选模式 (memory.confirm) 写进 候选.md 等用户确认; auto 模式直接进 对话摘要.md
        written, over = ([], []) if dry_run else await loop.run_in_executor(None, self.mem.propose, day, ok, upto)
        # 和已有记忆、候选重复的 = 用户这天又说到了那件事: 记一天 (候选标常提, 确认过的事件够天数升成长期)
        again = [b["same"] for b in bad if b.get("same")]
        mentioned = {} if dry_run or not again else await loop.run_in_executor(None, self.mem.mentioned, day, again)
        rec = {"time": datetime.now().isoformat(timespec="seconds"), "day": day, "range": [start, upto],
               "candidates": [c["user"] for c in cands], "accepted": ok, "written": written,
               "to": "候选" if self.mem.confirm else "对话摘要", "over_quota": over, "mentioned": mentioned,
               "rejected": bad, "ms": round((time.perf_counter() - t0) * 1000), "dry_run": dry_run}
        self.stats["runs"] += 1
        self.stats["facts"] += len(written)
        self.stats["rejected"] += len(bad)
        self.stats["last"] = rec["time"]
        if self.write_log is not None:
            self.write_log(rec)
        return rec


async def _main(argv=None) -> None:
    import argparse

    from .config import load_config
    from .llm import create_llm
    from .memory import Memory

    ap = argparse.ArgumentParser(description="把对话日志整理成事实 (手动补旧日志或试跑)")
    ap.add_argument("--day", help="从这一天的第一条开始整理 (默认接着 关系.md 里的进度)")
    ap.add_argument("--dry-run", action="store_true", help="只打印, 不写库也不推进进度")
    ap.add_argument("--set", action="append", default=[], help="配置覆盖, 如 memory.vault=D:/vault")
    args = ap.parse_args(argv)
    cfg = load_config(None, args.set)
    mem = Memory(cfg["memory"])
    mem.init()
    llm = create_llm(cfg["llm"])
    d = Digest(mem, llm, cfg["memory"].get("digest"))
    since = f"{args.day} 0" if args.day else None
    try:
        while (rec := await d.run_once(dry_run=args.dry_run, since=since)) is not None:
            print(json.dumps(rec, ensure_ascii=False, indent=1))
            if args.dry_run:  # 不推进进度, 只看第一批
                break
            since = None  # 之后接着 关系.md 里推进后的进度
    finally:
        await llm.close()


if __name__ == "__main__":
    asyncio.run(_main())

"""情绪 (杏仁核, 记忆按脑区重设计阶段 4; 2026-11-02 用户"按默认做", 方案见知识库 [[记忆阶段4情绪方案（2026-11-02）]]).

4a 记: 你自己说的话里有情绪时, 记一条情绪事件 (旁路文件 <角色>/记忆/.affect.jsonl, 不进检索; Obsidian 默认不显示点开头的文件):
   谁的 (只记你的)、什么情绪、效价 / 唤醒 / 不确定 / 紧迫、强度、置信度、关于哪件事 (同一句里提取的事实; 日志整理的那条按
   同一天、前后 2 分钟连上)、出处 (哪天几点、哪个渠道、触发的词; 不另抄整句, 原话在日志里).
   规则, 不调模型: 先用中继盖掉转述、引号、假设, 说梦的整句不记; 否定 ("不难过") 不记, "其实也没那么担心了"把同一类的
   上一条标"被更正"; 问她的 ("你难过吗")、别人的情绪 ("我妈很生气")、阴阳怪气 ("好开心哦, 又加班") 不记 —— 判不准的宁可不记.
   不给人贴标签: 只存一条条带时间的事件, 不做"主人容易焦虑"这种总结.
4b 用 (mode on): 带情绪的事件半衰期 × (1 + 2 × 强度), 最多 max_halflife_x 倍; 过了相关门槛的命中里按 分数 × (1 + max_bias × 强度)
   重排 —— 只重排, 不让不相关的冒出来; 只看强度不看正负 (不偏向坏事). shadow 只在追踪里记"本来会乘多少", 检索不变.
4c 她自己的心情 (mood: shadow): 从出生种子的基线出发, 跟着你的情绪事件起伏, 半天回一半, 早上起床回到基线; 只算、只给记忆页看.
开关 memory.affect: {"mode": off / shadow / on, "max_halflife_x": 3, "max_bias": 0.1, "mood": off / shadow}. 出厂是 off.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import memory_ops as O
from . import memory_relay as R

FILE = ".affect.jsonl"
MODES = ("off", "shadow", "on")
# (类别, 效价, 唤醒, 不确定, 词): 带"不"的先认 ("不开心"是难过, 不是"开心"被否定)
LEXICON = [
    ("难过", -0.6, 0.4, 0.2, r"不开心|不高兴|难过|伤心|难受|心痛|好想哭|想哭|哭了|失落|失望|沮丧|郁闷|心情(?:很|好|特别|有点|超|太)?(?:差|不好|糟)"),
    ("担心", -0.5, 0.7, 0.6, r"不放心|不安心|不安|担心|担忧|着急|急死|焦虑|紧张|害怕|好怕|很怕|有点怕|心慌|慌|发愁|忐忑|吓死|吓到"),
    # "讨厌"不在这里: "我讨厌下雨天"是喜好 (规则提取会记成"不喜欢"), 不是此刻生气 (留出组第一次跑错的那条, 看过以后改的)
    ("生气", -0.6, 0.8, 0.1, r"不爽|生气|气死|火大|恼火|气愤|愤怒|气人|烦死|烦躁|(?<![麻厌])烦(?!请|劳)"),
    ("委屈", -0.5, 0.5, 0.2, r"委屈|冤枉|憋屈"),
    ("压力", -0.5, 0.7, 0.4, r"压力|崩溃|撑不住|喘不过气|焦头烂额"),
    ("累", -0.3, 0.2, 0.1, r"心累|疲惫|疲倦|没力气|好困|太困|(?<!积)累(?!计|积)"),
    ("孤单", -0.5, 0.3, 0.2, r"孤单|孤独|寂寞|没人理|没人陪"),
    ("开心", 0.7, 0.5, 0.0, r"开心|高兴|快乐|好耶|太好了|幸福|美滋滋|开森"),
    ("兴奋", 0.7, 0.85, 0.1, r"兴奋|激动|期待|迫不及待"),
    ("感动", 0.6, 0.5, 0.0, r"感动|泪目|暖心"),
    ("骄傲", 0.6, 0.6, 0.0, r"骄傲|自豪|得意"),
    ("放松", 0.5, 0.2, 0.0, r"松了口气|松口气|放松|轻松|舒服|安心|踏实"),
]
_LEX = [(cat, v, a, u, re.compile(p)) for cat, v, a, u, p in LEXICON]
_STRONG = re.compile(r"^(?:气死|烦死|急死|吓死|崩溃|撑不住|好想哭|激动|心痛)$")  # 词本身就重
_CLAUSE = re.compile(r"[^，,。！!？?；;\n]+[，,。！!？?；;]?")
_NEG = re.compile(r"(?:不|没有?|(?<![特分区级告离差类性识派个])别|不用|不必|无需|并不|一点也不|一点都不|从不)"
                  r"(?:那么|太|怎么|再|很|是很|这么|那样|会)?$")  # "特别兴奋"里的"别"不是否定
# "被老板骂了, 好委屈": 被动句里的"骂、夸、说"不是转述 (中继按转述盖, 会把后面你自己的委屈也盖掉); 先换掉再交给中继
_PASSIVE = re.compile(r"(被[^，,。！!？?；;：:]{0,8}?)(骂|夸|批评|嘲笑|吼|凶|说)(?=了?[，,。！!？?；;]|了?$)")
_UP = re.compile(r"(?:好|太|特别|非常|超级|超|真的|真|巨|贼|极其|十分)$")
_MILD_UP = re.compile(r"(?:很|挺|蛮)$")
_DOWN = re.compile(r"(?:有点|有些|一点|稍微|略|小)$")
_POST_UP = re.compile(r"^(?:死了|坏了|得不行|得要命|极了|透了|到不行|好大|很大|太大|山大)")
_POST_DOWN = re.compile(r"^(?:有点|有些|一点)")  # "压力有点大"
_PEOPLE = re.compile(r"我们|我|你们|你|他们|她们|他|她|它|大家|别人|人家|对方|" + O._KIN + "|" + O._PET +
                     r"|宠物|[爸妈哥姐弟妹爷奶]|朋友|同学|舍友|领导|上司|老师|邻居")
_PREP = "和跟与同陪给对向替帮被把比"  # 紧跟在这些后面的人是宾语、施事、比较的对象, 不是有这情绪的人 ("和你聊天很放松""被老板骂了""我比她还高兴")
_SARCASM = re.compile(r"呵呵|才怪|真是谢谢|又(?:要|被|得|加班|下雨|迟到|改|丢|坏)")
_DREAM = re.compile(r"梦见|梦到|做梦|做了个梦")
_FIX = re.compile(r"了|其实|已经|好多了")
_ASK = re.compile(r"(?:吗|么|嘛|吧)[，,。！!？?]?$|[？?]$")
_SOON = re.compile(r"今天|今晚|明天|马上|一会儿|待会|下周|这周|周末|后天|下个月|月底")
_UNSURE = re.compile(r"会不会|万一|不知道|怎么办")


def _intensity(clause: str, s: int, e: int, word: str) -> tuple[float, bool]:
    pre, post = clause[max(0, s - 4):s], clause[e:e + 4]
    x = 0.8 if _STRONG.match(word) else 0.5
    down = bool(_DOWN.search(pre) or _POST_DOWN.match(post))
    if down:
        x -= 0.25
    elif _UP.search(pre) or word.startswith(("好", "太")):
        x += 0.3
    elif _MILD_UP.search(pre) or word.startswith(("很", "心情很")):
        x += 0.15
    if _POST_UP.match(post):
        x += 0.25
    if "！" in clause or "!" in clause:
        x += 0.1
    return round(min(1.0, max(0.2, x)), 2), down


def _whose(clause: str, s: int) -> str:
    """这个情绪词是谁的: 往前找最近的那个人 (跟在"和、跟、被、把……"后面的不算); 没有人就是说话的你 (省略主语)."""
    last = None
    for m in _PEOPLE.finditer(clause[:s]):
        if m.start() > 0 and clause[m.start() - 1] in _PREP:
            continue
        last = m.group()
    if last is None or last.startswith("我"):
        return "owner"
    return "her" if last.startswith("你") else "other"


def evaluate(text: str) -> dict:
    """你说的一句话 -> {"events": [{emotion, valence, arousal, uncertainty, urgency, intensity, confidence, words}], "fix": [类别]}.
    events 只有你自己的情绪 (同一类合成一条, 取最强的); fix 是"其实也没那么担心了"这种改口, 要把同一类的上一条标被更正."""
    raw = str(text or "")
    if _DREAM.search(raw):
        return {"events": [], "fix": []}
    own = R.own_part(_PASSIVE.sub(lambda m: m.group(1) + "□", raw))
    soon, unsure, sarcastic = bool(_SOON.search(raw)), bool(_UNSURE.search(raw)), bool(_SARCASM.search(raw))
    found: dict[str, dict] = {}
    fix: list[str] = []
    for cm in _CLAUSE.finditer(own):
        clause = cm.group()
        hits = []
        for cat, v, a, u, pat in _LEX:
            for m in pat.finditer(clause):
                hits.append((m.start(), -(m.end() - m.start()), m.end(), cat, v, a, u, m.group()))
        hits.sort()
        taken = -1
        for s, _, e, cat, v, a, u, word in hits:
            if s < taken:
                continue  # 和前面认出的词重叠 (长的先认)
            taken = e
            if not word.startswith(("不", "没")) and _NEG.search(clause[max(0, s - 6):s]):
                if _FIX.search(clause):
                    fix.append(cat)
                continue
            who = _whose(clause, s)
            asked = bool(_ASK.search(clause.strip()))
            if who != "owner" or (asked and "你" in clause) or (v > 0 and sarcastic):
                continue
            x, down = _intensity(clause, s, e, word)
            conf = 0.8 - (0.2 if down else 0) - (0.3 if asked else 0)
            ev = found.get(cat)
            if ev is None:
                found[cat] = {"emotion": cat, "valence": v, "arousal": a, "uncertainty": round(min(1.0, u + (0.2 if unsure else 0)), 2),
                              "urgency": 0.7 if soon else 0.2, "intensity": x, "confidence": round(conf, 2), "words": [word]}
            else:
                ev["intensity"] = max(ev["intensity"], x)
                ev["confidence"] = round(min(0.95, max(ev["confidence"], conf) + 0.1), 2)
                ev["words"].append(word)
    return {"events": list(found.values()), "fix": sorted(set(fix))}


def level(x: float) -> str:
    return "弱" if x < 0.45 else "中" if x <= 0.75 else "强"


def _minutes(hhmm: str) -> int | None:
    m = re.match(r"(\d{1,2}):(\d{2})$", hhmm or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def linked(ev: dict, chunk: dict) -> bool:
    """这条情绪事件关于不关于这块记忆: 正文一样 (同一句里提取的事实), 或者同一天、记忆的来源时刻
    (对话摘要那行"(来源: [[日期]] 14:02, 14:05)", 记的是原话那一行的时刻) 和情绪事件前后 2 分钟内 (日志整理的那条;
    放宽到 10 分钟时, 同一段里隔几分钟说的平常事也沾上了情绪)."""
    if chunk.get("text") in (ev.get("about") or []):
        return True
    said = chunk.get("said")
    day = said.isoformat() if hasattr(said, "isoformat") else None
    if not day or str(ev.get("t", ""))[:10] != day:
        return False
    rm = _minutes(str(ev.get("t", ""))[11:16])
    return rm is not None and any(abs(rm - m) <= 2 for m in (_minutes(x) for x in chunk.get("times") or []) if m is not None)


class AffectStore:
    """情绪事件的旁路文件 + 乘数. 文件改了 (这边写的、你在记忆页删的) 才重读."""

    def __init__(self, path_fn, cfg: dict | None = None):
        cfg = cfg or {}
        self.path_fn = path_fn  # 角色文件夹可能在 init 时改名 (renamed_from), 每次现取
        self.mode = cfg.get("mode", "off") if cfg.get("mode", "off") in MODES else "off"
        self.max_x = max(1.0, float(cfg.get("max_halflife_x", 3)))
        self.max_bias = max(0.0, min(0.5, float(cfg.get("max_bias", 0.1))))
        self.mood_mode = "shadow" if cfg.get("mood") == "shadow" else "off"
        self._rows: list[dict] = []
        self._sig = None
        self.last: dict = {}  # 上一轮检索用了 (或者影子模式下本来会用) 哪些乘数

    # -- 存取 ---------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return Path(self.path_fn())

    def version(self):
        try:
            st = self.path.stat()
            return (self.mode, st.st_mtime_ns, st.st_size)
        except OSError:
            return (self.mode, None, 0)

    def events(self) -> list[dict]:
        sig = self.version()
        if sig != self._sig:
            rows = []
            try:
                for ln in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and r.get("id"):
                        rows.append(r)
            except OSError:
                pass
            self._rows, self._sig = rows, sig
        return self._rows

    def _rewrite(self, rows: list[dict]) -> None:
        path = self.path
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def note(self, text: str, via: str, now: datetime, about: list[str]) -> list[dict]:
        """一句你自己说的话 -> 记下的情绪事件 (mode 是 off 时什么都不做). 调的人负责加锁 (Memory._lock)."""
        if self.mode == "off":
            return []
        got = evaluate(text)
        if not got["events"] and not got["fix"]:
            return []
        t = now.strftime("%Y-%m-%d %H:%M")
        rows = list(self.events())
        changed = False
        for cat in got["fix"]:  # 改口: 同一类最近一条 (7 天内) 标被更正
            for r in reversed(rows):
                if r.get("emotion") == cat and r.get("status") == "active" and r.get("t", "") >= (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M"):
                    r["status"], r["corrected_at"] = "corrected", t
                    changed = True
                    break
        new = []
        for ev in got["events"]:
            key = f"{t}|{via}|{ev['emotion']}|{','.join(ev['words'])}"
            new.append({"id": "aff-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10], "t": t, "via": via, "who": "owner",
                        **ev, "about": [a for a in about if a][:5], "status": "active"})
        rows += [r for r in new if all(r["id"] != x["id"] for x in rows)]
        if changed or new:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rewrite(rows)
        return new

    def delete(self, eid: str) -> bool:
        rows = self.events()
        keep = [r for r in rows if r.get("id") != eid]
        if len(keep) == len(rows):
            return False
        self._rewrite(keep)
        return True

    def clear(self) -> int:
        n = len(self.events())
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._sig = None
        return n

    # -- 乘数 ---------------------------------------------------------------------
    def strength(self, chunk: dict) -> float:
        """这块记忆连着的情绪 (你的、还算数的) 里最强的那条的强度; 没有就是 0."""
        if self.mode == "off":
            return 0.0
        return max((float(r.get("intensity") or 0) for r in self.events()
                    if r.get("status") == "active" and r.get("who") == "owner" and linked(r, chunk)), default=0.0)

    def halflife_x(self, chunk: dict) -> float:
        """事件半衰期乘多少 (只在 on 时真乘; shadow 照样算, 由调的人记进追踪)."""
        x = self.strength(chunk)
        return min(self.max_x, 1 + 2 * x) if x > 0 else 1.0

    def bias(self, chunk: dict) -> float:
        x = self.strength(chunk)
        return self.max_bias * x

    def rerank(self, hits: list[dict]) -> list[dict]:
        """过了相关门槛的命中 (已经按分数排好) -> 带情绪的往前挪 (只在 on); 记下乘数 (shadow 也记)."""
        if self.mode == "off" or not hits:
            self.last = {}
            return hits
        bias = {h["text"][:40]: round(self.bias(h), 3) for h in hits}
        xs = {h["text"][:40]: round(self.halflife_x(h), 2) for h in hits}
        self.last = {"mode": self.mode, "bias": {k: v for k, v in bias.items() if v}, "halflife_x": {k: v for k, v in xs.items() if v > 1}}
        if self.mode != "on" or not any(bias.values()):
            return hits
        return sorted(hits, key=lambda h: -h["score"] * (1 + bias[h["text"][:40]]))

    # -- 她自己的心情 (只算) ------------------------------------------------------------
    def mood(self, now: datetime, base: dict | None = None, half_life_h: float = 12.0, wake: str = "07:00") -> dict | None:
        """她此刻的心情 (mood 是 shadow 才算): 种子基线 + 你今天起床以后的情绪事件带来的起伏 (半天回一半), 有上下限."""
        if self.mood_mode != "shadow":
            return None
        base = base or {"valence": 0.2, "arousal": 0.4}
        h, mnt = (int(x) for x in (wake.split(":") + ["0"])[:2])
        woke = now.replace(hour=h, minute=mnt, second=0, microsecond=0)
        if woke > now:
            woke -= timedelta(days=1)  # 还没到今天起床的点: 从昨天起床算
        dv = da = 0.0
        why = []
        for r in self.events():
            if r.get("status") != "active" or r.get("who") != "owner":
                continue
            try:
                t = datetime.strptime(r["t"], "%Y-%m-%d %H:%M")
            except (KeyError, ValueError):
                continue
            if not woke <= t <= now:
                continue
            k = 0.5 ** ((now - t).total_seconds() / 3600 / half_life_h) * float(r.get("intensity") or 0)
            dv += (0.15 if r.get("valence", 0) > 0 else -0.1) * k
            da += 0.1 * k
            why.append(f"{r['t'][11:]} 你{r.get('emotion')}")
        dv, da = max(-0.4, min(0.4, dv)), max(-0.3, min(0.3, da))
        return {"valence": round(max(-1.0, min(1.0, base["valence"] + dv)), 2),
                "arousal": round(max(0.0, min(1.0, base["arousal"] + da)), 2), "base": base, "why": why[-5:],
                "since": woke.strftime("%Y-%m-%d %H:%M")}

    def view(self, now: datetime, mood_base: dict | None = None, half_life_h: float = 12.0, wake: str = "07:00") -> dict:
        """记忆页看的: 开关、每一条情绪事件 (新的在前)、她此刻的心情 (只算)."""
        rows = sorted(self.events(), key=lambda r: r.get("t", ""), reverse=True)
        return {"mode": self.mode, "max_halflife_x": self.max_x, "max_bias": self.max_bias, "events": rows[:200],
                "count": len(rows), "mood": self.mood(now, mood_base, half_life_h, wake)}

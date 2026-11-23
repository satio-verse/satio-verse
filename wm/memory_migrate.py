"""把她现在各处的记忆读成一张关系网 (只读): 记忆核心 (wm/memory_core.py) 建索引、迁移演练 (--dry-run)、
记忆体检 (tools/memory_checkup.py) 都用它. 记忆按脑区重设计的阶段 1 (2026-11-01, 知识库"记忆系统按脑区重设计方案").

只读文件: 不写、不建库, 也不调 Memory._refresh (那个会把你在 Obsidian 里勾上的候选转正) 和 Memory.init.
节点的说法和设置里的"记忆"页一样 (来源、改过 / 忘掉都取自 Memory.overview); 参与检索的那些 (每块一个节点) 和检索时切出来的块一一对应.

读哪些, 来源照实标 (origin):
  记忆/关于用户.md、对话摘要.md   told 你当面说的 (#私聊 的是 QQ / 微信私聊里说、你确认过的) / digest 日志整理的 / user 你自己写的;
                                 划掉的行 status=corrected (更正为…, 连一条"更正为"边) / forgotten (用户要求忘掉) / struck
  记忆/候选.md                   inferred 整理出来、还没确认的 (私聊里说的那种也要你确认); 勾了还没转正的标 ticked
  关系.md                        system 关系状态 (frontmatter)
  日记/我的理解.md、操作笔记.md   experience 她自己的; 日记/归档/操作笔记-YYYY.md (超出 200 条挪去的) status=archived
  日记/YYYY-MM-DD.md、日志/YYYY-MM-DD.md   一天一个节点 (她的日记 / 你们那天的对话); 日志再按中继 (wm/memory_relay.py) 切成
                                 一段一段 (episode, 隔了 20 分钟以上、"对了"开头的另起一段), 整理出来的事连到它出自的那一段
  资料/*.md                      external 拖进来的文件, 只当数据
  库里别的 .md (不含排除的文件夹)  user 你自己写的笔记
  记忆/.recall.json              上次想起、想起过的日子, 并进对应的节点
  <agent.home>/experiences.json  experience 工作经验 (命令报错 -> 换了做法 -> 成了); experiences-archive.jsonl 里挪出去的 status=archived
  小窝认知的经历文件夹             experience 她以为东西在哪、东西不在原处时在哪找到过、在小窝里做过的事
  出生种子 (wm/memory_seed.py)     seed 出厂自带的自我认识、性子、情绪底色、习惯、本能; 状态 active / off (你停用了) / not_needed
  记忆/.affect.jsonl (阶段 4)       inferred 你自己话里的情绪事件 (规则认的, 只有情绪、强度、出处和触发的词, 没有原话), 连到它关于的那几块记忆
人、物、地点的实体用 wm/memory_graph.py 的图谱算, 和检索时一样.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from . import memory as M
from . import memory_affect as A
from . import memory_graph as G
from . import memory_ops as O
from . import memory_relay as R

TYPES = {"fact": "长期的事", "event": "经历过的事", "candidate": "候选", "self": "她自己的理解", "procedure": "做法和经验",
         "day": "那天的对话", "episode": "一段对话", "diary": "她的日记", "doc": "拖进来的资料", "note": "你写的笔记", "relation": "你们的关系",
         "entity": "人、物、地点", "belief": "她以为东西在哪", "place": "地方", "trigger": "什么时候用",
         "trait": "性子", "affect": "情绪的底色", "seedpack": "出厂种子包", "emotion": "情绪事件"}
ORIGINS = {"told": "你说的", "digest": "整理出来的", "inferred": "整理出来、还没确认", "user": "你写的", "experience": "她自己的经历",
           "external": "外面来的", "system": "程序算的", "seed": "出厂自带"}
_FROM_OVERVIEW = {"chat": "told", "private": "told", "digest": "digest", "you": "user"}
_DAY_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HOWTO_LINE = re.compile(r"^- \((\d{4}-\d{2}-\d{2}), (成了|没成)\) (.+?) → (.+)$")
_UNDERSTAND = re.compile(r"^- \((\d{4}-\d{2}-\d{2})(?:,\s*([^)]*))?\)\s*(.+)$")
_REJECTED = re.compile(r"^- \[[ xX]\] ~~(.+?)~~\s*(.*)$")
_TICKED = re.compile(r"^- \[[xX]\] (?!~~)(.+)$")


def nid(prefix: str, key: str) -> str:
    """稳定的节点编号: 前缀 + 内容的短哈希 (同样的东西每次建出来都一样; 你改了字就是一个新节点)."""
    return f"{prefix}:{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"


class Collector:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}
        self.problems: list[str] = []
        self.sources: Counter = Counter()

    def node(self, prefix: str, key: str, type_: str, origin: str, text: str, **kw) -> str:
        i = nid(prefix, key)
        if i in self.nodes:  # 同一个文件里同样的一句写了两遍: 合成一个, 记个数
            self.nodes[i]["data"]["copies"] = self.nodes[i]["data"].get("copies", 1) + 1
            return i
        data = kw.pop("data", None) or {}
        self.nodes[i] = {"id": i, "type": type_, "origin": origin, "status": kw.pop("status", "active"), "text": text[:300],
                         "file": kw.pop("file", None), "line": kw.pop("line", None), "day": kw.pop("day", None), "data": data}
        return i

    def edge(self, src: str, dst: str, kind: str, weight: float = 1.0, **data) -> None:
        if src == dst or src not in self.nodes or dst not in self.nodes:
            return
        e = self.edges.setdefault((src, dst, kind), {"src": src, "dst": dst, "kind": kind, "weight": 0.0, "data": {}})
        e["weight"] = round(min(1.0, e["weight"] + weight), 4)
        e["data"].update(data)

    def result(self) -> dict:
        return {"nodes": self.nodes, "edges": list(self.edges.values()), "problems": self.problems,
                "sources": dict(self.sources), "types": dict(Counter(n["type"] for n in self.nodes.values())),
                "origins": dict(Counter(n["origin"] for n in self.nodes.values())),
                "edge_kinds": dict(Counter(e["kind"] for e in self.edges.values()))}


def _line_of(lines: list[str], text: str) -> int | None:
    """这句在文件的第几行 (1 起; 给人核对用, 找不到就空)."""
    probe = text.split(": ", 1)[-1][:14]
    for i, ln in enumerate(lines, 1):
        if probe and probe in ln:
            return i
    return None


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def collect(mem_cfg: dict, agent_home: Path | str | None = None, cognition_dir: Path | str | None = None,
            cache: dict | None = None) -> dict:
    """读出整张关系网: {nodes: {编号: 节点}, edges: [边], problems: [对不上的地方], sources: {来源: 条数}, types, origins, edge_kinds,
    chunks: 检索时切出的块数}. mem_cfg 就是配置里的 memory 那一段 (至少有 vault).
    cache: 反复收集时传同一个 dict, 文件没改 (修改时间一样) 就用上次切好的块 (和 Memory._refresh 一个做法; 3000 块的库省掉大半时间)."""
    c = Collector()
    m = M.Memory({**mem_cfg, "graph_notes": False})
    if not m.dir.is_dir():
        c.problems.append(f"没有她的记忆文件夹 {m.dir}")
        return {**c.result(), "chunks": 0}
    char = m.char
    days: dict[str, str] = {}
    episodes: dict[str, list[tuple[str, str, str]]] = {}  # 那天 -> [(起, 止, 节点)]: 中继切出来的一段一段 (阶段 2)
    relay = mem_cfg.get("relay") or {}
    relay_on, gap = relay.get("enabled", True) is not False, float(relay.get("gap_min", 20))

    def day_node(day: str) -> str | None:
        return days.get(day)

    def episode_links(i: str, source: str | None) -> None:
        """"2026-10-25 14:00, 14:01" (整理出来的事的来源) -> 连到那天那几句所在的那一段."""
        got = re.match(r"(\d{4}-\d{2}-\d{2})\s+([\d:,， ]+)", source or "")
        if not got:
            return
        for t in re.findall(r"\d{1,2}:\d{2}", got.group(2)):
            t = t.zfill(5)
            for start, end, e in episodes.get(got.group(1), []):
                if start and end and start <= t <= end:
                    c.edge(i, e, "from_episode")

    # 每天的对话、她的日记: 一天一个节点
    for sub, type_, prefix in (("日志", "day", "d"), ("日记", "diary", "y")):
        for p in sorted((m.dir / sub).glob("*.md")) if (m.dir / sub).is_dir() else []:
            if not _DAY_FILE.match(p.stem):
                continue
            lines = [ln for ln in _read(p).splitlines() if ln.startswith("- ")]
            room = sum(" 小屋: " in ln or "小屋:" in ln for ln in lines)
            text = f"{p.stem} 的对话（{len(lines)} 句）" if type_ == "day" else f"{p.stem} 的日记（{len(lines)} 条）"
            i = c.node(prefix, p.stem, type_, "experience", text, file=p.relative_to(m.root).as_posix(), day=p.stem,
                       data={"lines": len(lines), **({"room": room} if room else {})})
            if type_ == "day":
                days[p.stem] = i
                for seg in R.segment(m._entries(p), gap) if relay_on else []:
                    e = c.node("ep", f"{p.stem}/{seg['start']}", "episode", "experience",
                               f"{p.stem} {seg['start']}–{seg['end']}：{seg['text']}（{len(seg['lines'])} 句）",
                               file=p.relative_to(m.root).as_posix(), day=p.stem,
                               data={"start": seg["start"], "end": seg["end"], "lines": len(seg["lines"]),
                                     "salience": seg["salience"], "why": seg["why"]})
                    c.edge(e, i, "part_of")
                    episodes.setdefault(p.stem, []).append((seg["start"], seg["end"], e))
                    c.sources["一段对话"] += 1
            c.sources[f"{sub}（天）"] += 1
    for d, i in list(days.items()):
        y = nid("y", d)
        if y in c.nodes:
            c.edge(y, i, "same_day")

    # 参与检索的块: 和 Memory._refresh 切出来的一样 (候选.md 不切)
    cand = m.dir / "记忆" / M.CANDIDATE_FILE
    chunks, lines_of = [], {}
    seen = set()
    for p in sorted(m._files()):
        if p == cand:
            continue
        try:
            stamp = p.stat().st_mtime_ns
            hit = cache.get(str(p)) if cache is not None else None
            got, lines = hit[1:] if hit and hit[0] == stamp else (m._chunk_file(p), _read(p).splitlines())
        except OSError as exc:
            c.problems.append(f"读不了 {p}: {exc}")
            continue
        if cache is not None:
            cache[str(p)] = (stamp, got, lines)
            seen.add(str(p))
        lines_of[p.relative_to(m.root).as_posix()] = lines
        chunks += got
    if cache is not None:
        for k in set(cache) - seen:  # 删掉、挪走的文件
            del cache[k]
    try:
        ov = m.overview()
    except OSError as exc:
        c.problems.append(f"记忆页的概览读不出来: {exc}")
        ov = {"facts": [], "candidates": [], "understanding": [], "relation": {}}
    facts = {(f"{char}/记忆/{f['file']}", f["text"]): f for f in ov["facts"]}
    chunk_ids = []
    for ch in chunks:
        f = facts.get((ch["file"], ch["text"]))
        if f:
            origin = _FROM_OVERVIEW.get(f["origin"], "user")
            type_ = "fact" if ch["kind"] == O.LONG else "event"
            data = {"kind": ch["kind"], "tags": f["tags"], **({"via": "私聊"} if f["origin"] == "private" else {}),
                    **({"source": f["source"]} if f.get("source") else {})}
        else:
            origin = "external" if ch["file"].startswith(f"{char}/资料/") else "user"
            type_ = "doc" if origin == "external" else "note"
            data = {"kind": ch["kind"]}
        said = ch["said"].isoformat() if ch.get("said") else None
        i = c.node("c", f"{ch['file']}\n{ch['text']}", type_, origin, ch["text"], file=ch["file"],
                   line=_line_of(lines_of.get(ch["file"], []), ch["text"]), day=said, data=data)
        chunk_ids.append(i)
        c.sources["检索的块"] += 1
        if said and day_node(said):
            c.edge(i, days[said], "from_day" if origin in ("digest", "inferred") else "said_on")
        episode_links(i, data.get("source"))
    # 划掉的行: 改过的、忘掉的 (不参与检索, 但留在网里, 能看到改过什么)
    for f in ov["facts"]:
        if f["status"] == "active":
            continue
        i = c.node("s", f"{f['file']}\n{f['text']}", "fact" if f["kind"] == O.LONG else "event", _FROM_OVERVIEW.get(f["origin"], "user"),
                   f["text"], status=f["status"], file=f"{char}/记忆/{f['file']}", day=f["said"], data={"note": f["note"]})
        c.sources["划掉的"] += 1
        if "更正为" in f["note"]:
            new = f["note"].split("更正为", 1)[1].lstrip(":： ").strip()
            target = next((x for x in chunk_ids if c.nodes[x]["text"] == new), None)
            if target:
                c.edge(i, target, "corrected_to")
    # 实体: 和检索时一样用图谱并组
    g = G.Graph(["" if ch["log"] else ch["text"] for ch in chunks], [ch["glinks"] for ch in chunks], [ch["gment"] for ch in chunks])
    for root, idx in sorted(g.members.items(), key=lambda kv: str(kv[0])):
        cat = g.category(root)
        if not cat:
            continue
        label = g.label(root)
        e = c.node("e", f"{cat}\n{label}", "entity", "system", label, data={"category": cat, "aliases": sorted(g.group[root])})
        for k in idx:
            c.edge(chunk_ids[k], e, "mentions")
    # 想起记录
    rec = m._recall_dates()
    by_text: dict[str, list[str]] = {}
    for i in chunk_ids:
        by_text.setdefault(c.nodes[i]["text"], []).append(i)
    lost = 0
    for text, r in sorted(rec.items()):
        hit = by_text.get(text)
        if not hit:
            lost += 1
            continue
        for i in hit:
            c.nodes[i]["data"]["recall"] = {"last": r["last"], "days": len(r["days"])}
    if lost:
        c.problems.append(f"想起记录里有 {lost} 条对不上现在的记忆 (改过字、删掉了, 或者还在候选里)")
    # 候选: 等确认的、勾了还没转正的、你点过"不要"的
    for k in ov["candidates"]:
        i = c.node("k", k["text"], "candidate", "inferred", k["text"], status="candidate", file=f"{char}/记忆/{M.CANDIDATE_FILE}",
                   day=k["day"], data={"days": k["days"], **({"via": "私聊"} if k.get("private") else {}),
                                       **({"source": k["source"]} if k.get("source") else {})})
        c.sources["候选"] += 1
        if k["day"] and day_node(k["day"]):
            c.edge(i, days[k["day"]], "from_day")
        episode_links(i, k.get("source"))
    for ln in _read(cand).splitlines():
        s = ln.strip()
        if r := _REJECTED.match(s):
            text = M._clean_fact(r.group(1))
            c.node("k", text, "candidate", "inferred", text, status="rejected", file=f"{char}/记忆/{M.CANDIDATE_FILE}",
                   data={"note": re.sub(r"\s*#\S+", "", r.group(2)).strip()})
            c.sources["候选（不要的）"] += 1
        elif t := _TICKED.match(s):
            text = M._clean_fact(t.group(1))
            c.node("k", text, "candidate", "inferred", text, status="ticked", file=f"{char}/记忆/{M.CANDIDATE_FILE}")
            c.sources["候选（勾了还没转正）"] += 1
    # 关系状态
    if ov.get("relation"):
        rel = ov["relation"]
        c.node("r", "关系", "relation", "system",
               f"{rel.get('user_name') or '主人'}和她：{rel.get('familiarity') or '刚认识'}，见过 {rel.get('sessions', 0)} 次、聊过 {rel.get('turns', 0)} 轮",
               file=f"{char}/关系.md", data=rel)
    # 她自己的理解、操作笔记
    for u in ov["understanding"]:
        i = c.node("u", u["text"], "self", "experience", u["text"], file=f"{char}/日记/{M.UNDERSTAND_FILE}", day=u["day"],
                   data={"source": u["source"]} if u["source"] else {})
        c.sources["我的理解"] += 1
        if day_node(u["day"]):
            c.edge(i, days[u["day"]], "from_day")
    for ln in _read(m.dir / "日记" / M.HOWTO_FILE).splitlines():
        if h := _HOWTO_LINE.match(ln.strip()):
            day, ok, task, note = h.groups()
            c.node("h", f"{task}\n{note}", "procedure", "experience", f"{task} → {note}", file=f"{char}/日记/{M.HOWTO_FILE}", day=day,
                   data={"ok": ok == "成了", "task": task})
            c.sources["操作笔记"] += 1
    for p in sorted((m.dir / "日记" / M.HOWTO_ARCHIVE).glob("操作笔记-*.md")):  # 超出 200 条挪去归档的: 还在, 只是不再翻出来
        for ln in _read(p).splitlines():
            if h := _HOWTO_LINE.match(ln.strip()):
                day, ok, task, note = h.groups()
                c.node("h", f"{task}\n{note}", "procedure", "experience", f"{task} → {note}", status="archived",
                       file=f"{char}/日记/{M.HOWTO_ARCHIVE}/{p.name}", day=day, data={"ok": ok == "成了", "task": task})
                c.sources["操作笔记（归档）"] += 1
    _seeds(c, ov.get("seeds") or [])
    _emotions(c, (ov.get("affect") or {}).get("events") or [], list(zip(chunks, chunk_ids)))
    if agent_home:
        _experiences(c, Path(agent_home))
    if cognition_dir:
        _cognition(c, Path(cognition_dir))
    return {**c.result(), "chunks": len(chunks)}


def _seeds(c: Collector, seeds: list[dict]) -> None:
    """出生种子 (Memory.overview 里的, 已经带了状态): 每条一个节点, 来源"出厂"; 都连到种子包, 习惯和本能连到"什么时候用"."""
    if not seeds:
        return
    pack = c.node("p", seeds[0]["pack"], "seedpack", "seed", f"出厂种子 {seeds[0]['pack']}")
    for s in seeds:
        i = c.node("sd", s["id"], s["type"], "seed", s["text"], status=s["status"], file=s["pack"],
                   data={k: s[k] for k in ("id", "kind", "now", "from", "values", "note") if k in s})
        c.edge(i, pack, "in_pack")
        if s.get("trigger"):
            c.edge(c.node("t", s["trigger"], "trigger", "system", s["trigger"]), i, "triggers")
        c.sources["出生种子"] += 1


def _emotions(c: Collector, events: list[dict], chunks: list[tuple[dict, str]]) -> None:
    """情绪事件 (记忆阶段 4, 只有你自己的): 每条一个节点 (规则认出来的, 来源算"整理出来、还没确认"), 节点上只有情绪、强度、
    出处和触发的词, 没有原话; 连到它关于的那几块记忆 (情绪边)."""
    keep = ("emotion", "valence", "arousal", "uncertainty", "urgency", "intensity", "confidence", "via", "t", "words")
    for r in events:
        if not r.get("id") or r.get("who") != "owner":
            continue
        i = c.node("e", str(r["id"]), "emotion", "inferred", f"{r.get('emotion')}（强度 {r.get('intensity')}）",
                   status=str(r.get("status") or "active"), day=str(r.get("t") or "")[:10] or None,
                   data={k: r[k] for k in keep if k in r})
        for ch, cid in chunks:
            if A.linked(r, ch):
                c.edge(i, cid, "emotion_of")
        c.sources["情绪事件"] += 1


def _experiences(c: Collector, home: Path) -> None:
    """工作经验 (wm/agent/experience.py): 每条一个程序节点, 连到"什么时候用"(哪类命令)."""
    path, rows = home / "experiences.json", []
    if path.exists():
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            c.problems.append(f"工作经验读不出来: {exc}")
    rows = [(e, False) for e in (rows if isinstance(rows, list) else [])]
    for n, ln in enumerate(_read(home / "experiences-archive.jsonl").splitlines(), 1):  # 超出 200 条挪出去的
        try:
            rows.append((json.loads(ln), True))
        except ValueError:
            if ln.strip():
                c.problems.append(f"工作经验归档第 {n} 行读不出来")
    for e, archived in rows:
        if not isinstance(e, dict) or not e.get("id") or not e.get("head"):
            continue
        i = c.node("x", str(e["id"]), "procedure", "experience", f"{e['head']} 报错（{str(e.get('sig', ''))[:60]}）→ {e.get('action', '')}",
                   status="archived" if archived else str(e.get("state") or "candidate"),
                   data={k: e.get(k) for k in ("support", "contradict", "pushed", "adopted", "hits", "trust", "enabled",
                                                "archived_why") if k in e})
        t = c.node("t", str(e["head"]), "trigger", "system", f"命令 {e['head']}")
        c.edge(t, i, "triggers")
        c.sources["工作经验（归档）" if archived else "工作经验"] += 1


def _cognition(c: Collector, base: Path) -> None:
    """小窝认知的经历 (wm/cognition/): 回放事件流得到她以为东西在哪、东西不在原处时在哪找到过; 发现簿里做过的事和喜不喜欢.
    只读: 回放在内存里做, 发现簿读完马上 close (不会写回)."""
    from .cognition.afford import AffordMind
    from .cognition.belief import Cognition
    from .cognition.events import EventLog

    folders = [("", base)] + ([(p.name, p) for p in sorted((base / "scenes").iterdir()) if p.is_dir()] if (base / "scenes").is_dir() else [])
    for scene, folder in folders:
        tag = scene or "小窝"
        room = folder / "room-events.jsonl"
        if room.exists():
            try:
                events = EventLog.load(room)
                cog = Cognition.replay(events)
            except Exception as exc:  # noqa: BLE001 (别的格式问题都只记一笔)
                c.problems.append(f"{room} 回放不了: {exc}")
                events, cog = [], None
            if cog is not None and events:
                last_t = events[-1].get("t")
                place = lambda r: c.node("p", f"{tag}/{r}", "place", "system", f"{tag}·{cog.layout.region_name(r)}")  # noqa: E731
                for item, f in sorted(cog.belief.facts.items()):
                    try:
                        text = cog.belief.describe(item, now=last_t)
                    except Exception:  # noqa: BLE001
                        text = f"{item}: {f.get('how')}"
                    i = c.node("b", f"{tag}/{item}", "belief", "experience", text,
                               data={k: f.get(k) for k in ("at", "how", "seen_at") if f.get(k) is not None})
                    if f.get("at"):
                        c.edge(i, place(f["at"]), "thinks_at")
                    c.sources["她以为东西在哪"] += 1
                found = Counter((r["kind"], r["found_at"]) for r in cog.exp.records)
                for (kind, at), n in sorted(found.items()):
                    i = c.node("f", f"{tag}/{kind}/{at}", "procedure", "experience",
                               f"{cog.layout.KINDS.get(kind, kind)}不在原处时，在{cog.layout.region_name(at)}找到过 {n} 次", data={"n": n})
                    c.edge(i, place(at), "found_at", weight=min(1.0, n / 5))
                    c.sources["找东西的经验"] += 1
        afford = folder / "afford-events.jsonl"
        if afford.exists():
            try:
                mind = AffordMind(afford)
                mind.close()
            except Exception as exc:  # noqa: BLE001
                c.problems.append(f"{afford} 读不了: {exc}")
                continue
            for act, ctxs in sorted(mind.tries.items()):
                n, ok = sum(k["n"] for k in ctxs.values()), sum(k["ok"] for k in ctxs.values())
                c.node("a", f"{tag}/{act}", "procedure", "experience", f"在{tag}里{mind.verbs.get(act) or act}：试过 {n} 次，成了 {ok} 次",
                       data={"n": n, "ok": ok, "contexts": len(ctxs)})
                c.sources["在小窝里做过的事"] += 1
            for item, v in sorted(mind.likes.items()):
                c.node("l", f"{tag}/{item}", "self", "experience", f"对{item}的感觉：{v.get('v', 0):+.2f}（{v.get('n', 0)} 次）", data=dict(v))
                c.sources["喜不喜欢"] += 1


def report(result: dict) -> str:
    """迁移演练 (--dry-run) 的报告: 会建出多少节点、边, 各来源多少条, 哪里对不上. 什么都不写."""
    lines = ["# 记忆核心迁移演练（只读，没写任何东西）", "",
             f"会建出 {len(result['nodes'])} 个节点、{len(result['edges'])} 条边；检索时切出的块 {result['chunks']} 块。", "",
             "## 各来源", ""] + [f"- {k}：{v}" for k, v in sorted(result["sources"].items(), key=lambda kv: -kv[1])]
    lines += ["", "## 节点（按种类）", ""] + [f"- {TYPES.get(k, k)}（{k}）：{v}" for k, v in sorted(result["types"].items(), key=lambda kv: -kv[1])]
    lines += ["", "## 节点（按来源）", ""] + [f"- {ORIGINS.get(k, k)}（{k}）：{v}" for k, v in sorted(result["origins"].items(), key=lambda kv: -kv[1])]
    lines += ["", "## 边", ""] + [f"- {k}：{v}" for k, v in sorted(result["edge_kinds"].items(), key=lambda kv: -kv[1])]
    lines += ["", "## 对不上的地方", ""] + ([f"- {p}" for p in result["problems"]] or ["- 没有"])
    return "\n".join(lines) + "\n"

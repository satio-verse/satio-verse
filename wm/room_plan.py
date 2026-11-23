"""小屋里她接下来做什么: 桌宠小屋模式的上行 room_think -> 下行 room_plan (pipeline.Session._room_think 调这里).

桌宠页面在小屋里隔几分钟问一次 (desktop/web/room-acts.js 的 Life): 带上此刻的心情、五项需求、候选的事 (本机按库存、昼夜
筛过)、最近做过的、身上哪里疼、摔没摔、练习的心得. 服务端用这条连接的模型 (本地或云端预设, 人设预设的口吻) 生成
{pick, thought, how}: 挑哪件、开始前心里想的一句、做法上的偏好. 不进对话历史, 不写轮次的 trace.

上行只认固定的东西: 事项编号取自小屋的场景表 (desktop/web/room-scenes/petal-oak.json 的 life) 和旁边的可供性表
(petal-oak.afford.json 的 acts: 她自己发现的事, 见 docs/roadmap/room-afford.md), 心情、部位词、技能名是固定词表, 其余是数字.
提示词里的中文名由服务端按表拼, 页面发来的字符串一律不进提示词 (和 intent.describe_state 一样), 所以云端预设也只带固定字段.
可供性的"补什么"不用表里的数: 她还没试过的按常识的先验 (公开表的 prior), 试过的按她自己的经验 —— 认知开着时取服务端的发现簿
(wm/cognition/afford.py 的 known(), 页面只报每件现在算哪种情境), 没开时用上行 known 里页面算的数.
"""
from __future__ import annotations

import json
import random
import re
from datetime import datetime
from pathlib import Path

from .config import resolve_path

DEFAULT_SCENE = "desktop/web/room-scenes/petal-oak.json"
NEEDS = {"energy": "精力", "food": "饱腹", "water": "水分", "fun": "开心", "social": "陪伴"}
MOODS = {"happy": "开心", "calm": "平静", "low": "有点低落", "sleepy": "困了", "hungry": "饿了", "thirsty": "渴了",
         "bored": "好无聊", "lonely": "想用户了", "asleep": "刚睡醒"}
# 体感记的部位 (room-body.js 的 PARTS / JOINT, "膝盖撞到书桌" 取"撞到"前面那段; 躺着陷进床里是"腰")
PARTS = {"膝盖", "小腿", "脚", "屁股", "身子", "头", "胳膊肘", "左膝", "右膝", "左手肘", "右手肘", "左边胯", "右边胯", "脖子", "腰", "关节"}
# 练习的技能 (room-practice.js; sit@椅子 只看 @ 前面)
SKILLS = {"sit": "坐下", "crouch": "蹲下拿东西", "treeR": "树式", "treeL": "树式", "fold": "前屈", "bend": "侧弯", "lie": "躺下"}
HOW = {"slow": "慢一点", "careful": "稳一点 (用练熟的做法)", "look": "先看看用户", "wave": "先冲用户挥挥手"}
THOUGHT_CHARS = 24
LOW = 40  # 需求低于这个算"缺得厉害": 候选只给能补它的
_ID = re.compile(r"^[a-z][a-z0-9_]{0,23}$")

SYSTEM = (
    "你是虚拟角色「绫音」, 一个人在自己的小屋里生活, 用户在屏幕外面看着你. 根据此刻的需求和心情, 从候选里挑一件接下来要做的事. "
    "只输出一个 json 对象, 键依次是 pick, thought, how, 不要别的文字.\n"
    "pick: 候选里的编号. 哪项需求低 (低于 40 算缺) 就优先挑能补它的事; 都不缺就挑想做的, 别和刚做过的重复; "
    "标着还没试过的是你最近才注意到的, 有兴致可以试试.\n"
    "thought: 开始前心里想的一句话, 口语, 不超过 20 字; 可以说为什么想做 (渴了、无聊了)、上次做得怎么样、哪里还疼. 不写编号.\n"
    "how: 做法偏好的数组, 0 到 2 项, 只能取 slow 慢一点 (还疼、刚摔过、困了), careful 稳一点 (上次没做好、摔过), "
    "look 先看看用户, wave 先冲用户挥挥手 (想用户了); 没有就 []."
)

# 示例里的事是小屋里没有的 (泡茶、画画、织围巾…): 0.8B 遇到和示例一样的情况会把示例那句原样抄过来 (实测渴了 4 次全是示例原句),
# 换成没有的事, 它只学格式和"为什么", 句子得自己说. 四条的原因、口吻、做法都不一样. (2026-10-27 小屋有了浇花 water_plant,
# 示例里的浇花换成织围巾; 第三条带一件"还没试过"的, 学怎么对待新发现的事)
EXAMPLES = [
    ("现在 12 点, 中午.\n需求 (0-100, 越低越缺): 精力 66, 饱腹 24, 水分 58, 开心 60, 陪伴 62.\n最缺的是饱腹, 能补它的: cake, noodle.\n"
     "心情: 饿了.\n刚做过: 画画.\n候选: cake 吃块蛋糕 (补饱腹), noodle 煮碗面 (补饱腹、水分).",
     {"pick": "noodle", "thought": "肚子在抗议了，煮碗面吃吧", "how": []}),
    ("现在 21 点, 晚上.\n需求 (0-100, 越低越缺): 精力 52, 饱腹 66, 水分 60, 开心 45, 陪伴 50.\n心情: 有点低落.\n刚做过: 做瑜伽 (没做成).\n"
     "身上: 刚才做瑜伽的时候摔了一跤; 屁股还有点疼.\n练习: 树式练了 4 次, 还不熟, 上次摔了.\n"
     "候选: music 听音乐 (补开心), draw 画画 (补开心), knit 织围巾.",
     {"pick": "music", "thought": "摔得屁股还疼，先安静听会儿歌", "how": ["slow"]}),
    ("现在 9 点, 上午.\n需求 (0-100, 越低越缺): 精力 84, 饱腹 72, 水分 70, 开心 66, 陪伴 75.\n心情: 开心.\n刚做过: 听音乐.\n"
     "候选: draw 画画 (补开心), knit 织围巾 (还没试过), tea 泡杯茶 (补水分).",
     {"pick": "knit", "thought": "那团毛线放好久了，织几针试试", "how": []}),
    ("现在 16 点, 下午.\n需求 (0-100, 越低越缺): 精力 70, 饱腹 64, 水分 66, 开心 58, 陪伴 26.\n最缺的是陪伴, 能补它的: call.\n"
     "心情: 想用户了.\n刚做过: 画画; 之前: 泡杯茶.\n候选: call 给用户发消息 (补陪伴).",
     {"pick": "call", "thought": "好一阵没说话了，有点想你", "how": ["wave"]}),
]

_ACTS: dict[str | tuple, dict[str, dict]] = {}


def load_acts(path: str | Path = DEFAULT_SCENE, *, allow_legacy_afford: bool = True) -> dict[str, dict]:
    """场景表的 life -> {编号: {name, going, gives}}, 加上旁边可供性表 (<场景>.afford.json) 的 acts (afford: True, gives 空:
    她补什么按上行的经验). 读一次记住; 场景表没有或坏了是空表 (room_think 一律回 off), 可供性表没有就只有场景表的."""
    key = str(path) if allow_legacy_afford else (str(path), False)
    if key not in _ACTS:
        try:
            scene = json.loads(resolve_path(str(path)).read_text(encoding="utf-8"))
            acts = {}
            for a in scene.get("life", []):
                aid = a.get("id")
                if not isinstance(aid, str) or not _ID.match(aid):
                    continue
                gives = {k: v for k, v in (a.get("gives") or {}).items() if k in NEEDS and isinstance(v, (int, float))}
                acts[aid] = {"name": str(a.get("name") or aid)[:12], "going": str(a.get("going") or "")[:12], "gives": gives}
            acts.update(_afford_acts(resolve_path(str(path)), acts, allow_legacy=allow_legacy_afford))
        except (OSError, ValueError, AttributeError):
            acts = {}
        _ACTS[key] = acts
    return _ACTS[key]


def _afford_acts(scene: Path, have: dict, *, allow_legacy: bool = True) -> dict[str, dict]:
    """可供性表 (petal-oak.json -> petal-oak.afford.json; 生活模拟版 petal-oak-life-v1 和原版同一个房间, 用同一份)."""
    names = [scene.with_suffix(".afford.json").name]
    if allow_legacy:
        names.append("petal-oak.afford.json")
    for name in names:
        p = scene.with_name(name)
        if p.is_file():
            try:
                spec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            return {a["id"]: {"name": str(a.get("name") or a["id"])[:12], "going": str(a.get("going") or "")[:12], "gives": {}, "afford": True,
                              "prior": {k: v for k, v in (a.get("prior") or {}).items() if k in NEEDS and isinstance(v, (int, float))}}
                    for a in spec.get("acts", []) if isinstance(a, dict) and isinstance(a.get("id"), str) and _ID.match(a["id"])
                    and a["id"] not in have}
    return {}


def _num(v, lo: float, hi: float) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return max(lo, min(hi, float(v)))


def _act(v, acts: dict) -> str | None:
    return v if isinstance(v, str) and v in acts else None


def _part(v) -> str | None:
    return v if isinstance(v, str) and v in PARTS else None


def _list(v, n: int) -> list:
    return v[:n] if isinstance(v, list) else []


def parse_request(msg: dict, acts: dict, mind: list | None = None) -> dict | None:
    """上行 room_think -> 只留认识的编号、固定取值和数字. 没有一个认识的候选时返回 None.
    mind: 服务端发现簿的 known() ([{act, n, ctx: {情境: {n, v}}}]); 给了就按它算可供性试过几次、补什么, 页面传的数不用."""
    options = list(dict.fromkeys(o for o in _list(msg.get("options"), 32) if _act(o, acts)))  # 场景表 16 件 + 可供性
    if not options:
        return None
    needs = {}
    raw = msg.get("needs") if isinstance(msg.get("needs"), dict) else {}
    for k in NEEDS:
        v = _num(raw.get(k), 0, 100)
        if v is not None:
            needs[k] = round(v)
    recent = [{"id": r["id"], "ok": r.get("ok") is not False} for r in _list(msg.get("recent"), 5)
              if isinstance(r, dict) and _act(r.get("id"), acts)]
    body = msg.get("body") if isinstance(msg.get("body"), dict) else {}
    pains = [{"part": _part(p.get("part")), "act": _act(p.get("act"), acts)} for p in _list(body.get("pains"), 3) if isinstance(p, dict)]
    falls = [{"act": _act(f.get("act"), acts)} for f in _list(body.get("falls"), 3) if isinstance(f, dict)]
    practice = []
    for p in _list(msg.get("practice"), 8):
        if not isinstance(p, dict) or not _act(p.get("act"), acts) or not isinstance(p.get("skill"), str):
            continue
        skill = p["skill"].split("@")[0]
        if skill not in SKILLS:
            continue
        practice.append({"act": p["act"], "skill": skill, "n": int(_num(p.get("n"), 0, 9999) or 0),
                         "avg": _num(p.get("avg"), -9, 1), "better": p.get("better") is True,
                         "falls": int(_num(p.get("falls"), 0, 99) or 0), "pain": _num(p.get("pain"), 0, 99) or 0.0})
    known = {}  # 可供性: 她试过几次、期望补哪项多少 (她的经验; 只认候选里的可供性编号、需求名和数字)
    book = {m["act"]: m for m in mind or [] if isinstance(m, dict) and isinstance(m.get("act"), str)}
    for k in _list(msg.get("known"), 32):
        if not isinstance(k, dict) or k.get("act") not in options or not acts[k["act"]].get("afford") or k["act"] in known:
            continue
        if mind is not None:  # 发现簿是准的: 页面只说现在算哪种情境, 次数和经验从这里拿 (没试过按先验)
            m, ctx = book.get(k["act"]) or {}, k.get("ctx") if isinstance(k.get("ctx"), str) else None
            c = (m.get("ctx") or {}).get(ctx) or {}
            v = c.get("v") if c.get("n") else acts[k["act"]].get("prior", {})
            known[k["act"]] = {"n": int(m.get("n") or 0), "gives": {n: round(float(v[n]), 1) for n in NEEDS if isinstance(v.get(n), (int, float))}}
            continue
        raw = k.get("expect") if isinstance(k.get("expect"), dict) else {}
        known[k["act"]] = {"n": int(_num(k.get("n"), 0, 9999) or 0),
                           "gives": {n: round(v, 1) for n in NEEDS if (v := _num(raw.get(n), -50, 50)) is not None}}
    return {"options": options, "needs": needs, "mood": msg.get("mood") if msg.get("mood") in MOODS else None,
            "recent": recent, "injured": _part(body.get("injured")),
            "pains": [p for p in pains if p["part"]], "falls": falls, "practice": practice, "known": known}


def gives(o: str, req: dict, acts: dict) -> dict:
    """这件事补哪些需求: 场景表里的按表; 可供性按她的经验 (上行 known), 没说就是不知道 (空)."""
    if acts[o].get("afford"):
        return req.get("known", {}).get(o, {}).get("gives", {})
    return acts[o]["gives"]


def _gaps(needs: dict) -> dict[str, float]:
    """每项缺多少 (0 不缺, 1 见底): 70 分以下才算, 平方 (缺得越多越急, 同 room-acts.js 的 weight)."""
    return {k: max(0.0, (70 - v) / 70) ** 2 for k, v in needs.items()}


def lowest(req: dict, acts: dict, below: float = LOW) -> tuple[str, list[str]] | None:
    """最缺的那项 (低于 below) 和候选里真能补它的几件 (补 12 分以上, 或者就数它补得多; 补得多的在前). 没有返回 None."""
    g = {o: gives(o, req, acts) for o in req["options"]}
    for k in sorted((k for k, v in req["needs"].items() if v < below), key=req["needs"].get):
        fix = sorted((o for o in req["options"] if g[o].get(k, 0) > 0), key=lambda o: -g[o][k])
        if fix:
            best = g[fix[0]][k]
            return k, [o for o in fix if g[o][k] >= min(12, best)]
    return None


def shortlist(req: dict, acts: dict, top: int = 4) -> dict:
    """给模型挑的候选只留几件 (0.8B 在十几件里挑不准, 总挑补的项目最多的冲咖啡; 实测见 tools/bench_room_plan.py):
    有一项缺得厉害 (低于 LOW) 时只留真能补它的; 不然刚做过的不要, 其余随机取 top 件 (按缺的项补多少排在前面). top <= 0 不筛."""
    if top <= 0:
        return req
    low = lowest(req, acts)
    if low:
        return {**req, "options": low[1][:top]}
    last = req["recent"][0]["id"] if req["recent"] else None
    opts = [o for o in req["options"] if o != last] or list(req["options"])
    random.shuffle(opts)
    gaps = _gaps(req["needs"])
    score = {o: sum(gives(o, req, acts).get(k, 0) * g for k, g in gaps.items()) for o in opts}
    return {**req, "options": sorted(opts, key=lambda o: -score[o])[:top]}


def _period(h: int) -> str:
    return ("夜里" if h < 6 else "早上" if h < 9 else "上午" if h < 12 else "中午" if h < 14 else "下午" if h < 18
            else "晚上" if h < 23 else "夜里")


def _name(aid: str | None, acts: dict) -> str:
    """提示词里的事项名: 场景表是按用户的视角写的 ("过来看看你"), 提示词里的「你」是她, 换成用户."""
    return acts[aid]["name"].replace("你", "用户") if aid in acts else ""


def describe(req: dict, acts: dict, hour: int | None = None, hint: bool = True) -> str:
    """整理过的请求 -> 提示词里的几行 (只用编号对应的名字、固定词表和数字). hint: 点明最缺的那项和能补它的几件."""
    h = datetime.now().hour if hour is None else hour
    lines = [f"现在 {h} 点, {_period(h)}."]
    if req["needs"]:
        lines.append("需求 (0-100, 越低越缺): " + ", ".join(f"{NEEDS[k]} {v}" for k, v in req["needs"].items()) + ".")
        low = lowest(req, acts) if hint else None
        if low:
            lines.append(f"最缺的是{NEEDS[low[0]]}, 能补它的: {', '.join(low[1])}.")
    if req["mood"]:
        lines.append(f"心情: {MOODS[req['mood']]}.")
    did = [_name(r["id"], acts) + ("" if r["ok"] else " (没做成)") for r in req["recent"]]
    if did:
        lines.append(f"刚做过: {did[0]}" + (f"; 之前: {', '.join(did[1:])}" if did[1:] else "") + ".")
    body = [f"刚才{_name(f['act'], acts)}的时候摔了一跤" if f["act"] else "刚才摔了一跤" for f in req["falls"][:1]]
    body += [f"刚才{_name(p['act'], acts)}的时候{p['part']}撞疼了" if p["act"] else f"刚才{p['part']}撞疼了" for p in req["pains"][:2]]
    if req["injured"]:
        body.append(f"{req['injured']}还有点疼")
    if body:
        lines.append("身上: " + "; ".join(dict.fromkeys(body)) + ".")
    prac = []
    for p in req["practice"]:
        avg = p["avg"]
        level = "还没练过" if avg is None else "很稳" if avg >= 0.8 else "还行" if avg >= 0.3 else "还不熟"
        last = ", 上次摔了" if p["falls"] else ", 上次撞到了" if p["pain"] > 0.6 else ", 上次比平时好" if p["better"] else ""
        prac.append(f"{SKILLS[p['skill']]}练了 {p['n']} 次, {level}{last}")
    if prac:
        lines.append("练习: " + "; ".join(dict.fromkeys(prac)) + ".")
    opts = []
    for o in req["options"]:
        fix = [NEEDS[k] for k, v in gives(o, req, acts).items() if v >= 5]
        n = req.get("known", {}).get(o, {}).get("n", 0) if acts[o].get("afford") else None
        note = [f"补{'、'.join(fix)}"] if fix else []
        if n is not None:
            note.append("还没试过" if not n else f"试过 {n} 次" + ("" if fix else ", 没什么意思" if sum(gives(o, req, acts).values()) <= 0 else ""))
        opts.append(f"{o} {_name(o, acts)}" + (f" ({', '.join(note)})" if note else ""))
    lines.append("候选: " + ", ".join(opts) + ".")
    return "\n".join(lines)


def build_messages(req: dict, acts: dict, hour: int | None = None, examples: bool = True, hint: bool = True,
                   persona: bool = False) -> list[dict]:
    """系统提示 + 四条示例是固定前缀 (同一个槽位里接连几次请求, llama.cpp 复用它的缓存); 人设文字由调用方接在系统提示后面,
    persona=True 时本次状态末尾再提醒一句 (人设离生成太远, 0.8B 照着示例的口吻说)."""
    msgs = [{"role": "system", "content": SYSTEM}]
    for text, out in EXAMPLES if examples else []:
        msgs.append({"role": "user", "content": text})
        msgs.append({"role": "assistant", "content": json.dumps(out, ensure_ascii=False, separators=(",", ":"))})
    tail = "\n(thought 用角色设定里的口吻和自称.)" if persona else ""
    msgs.append({"role": "user", "content": describe(req, acts, hour, hint) + tail})
    return msgs


async def ask_local(llm, msgs: list[dict], options: list[str], rc: dict) -> str:
    """本地模型: 约束解码, 单独的槽位. 每次换一个随机种子 (llama.cpp 的请求默认固定种子, 同样的状态会想出一模一样的一句)."""
    parts = []
    async for t in llm.stream(msgs, schema=schema(options), max_tokens=int(rc.get("max_tokens", 60)),
                              temperature=rc.get("temperature", 0.7), id_slot=rc.get("id_slot", 1),
                              extra={"seed": random.randrange(1 << 30)}):
        parts.append(t)
    return "".join(parts)


def schema(options: list[str]) -> dict:
    """本地模型的约束解码: pick 只能是候选, thought 限长, how 只能是固定的几个."""
    return {
        "type": "object",
        "properties": {
            "pick": {"type": "string", "enum": options},
            "thought": {"type": "string", "maxLength": 32},
            "how": {"type": "array", "items": {"type": "string", "enum": list(HOW)}, "maxItems": 2},
        },
        "required": ["pick", "thought", "how"],
        "additionalProperties": False,
    }


_QUOTES = re.compile(r"[\"'“”‘’「」『』《》\[\]{}<>\\*#`]|\s+")
_LEAD = re.compile(r"^(她想|我想|心里想)[:：,，]?|^想[:：]")  # "想你了" 的想不去掉
_OFF = re.compile(r"AI|人工智能|模型|json|JSON|编号|候选|提示词")
_STOP = "，。！？～…,.!?~"


def clean_thought(text, acts: dict) -> str:
    """模型写的那句 -> 显示用的一句: 去引号、空白、"我想:"; 用户换回「你」; 带了编号或"模型"这类词的不要; 太长在标点处截."""
    t = _QUOTES.sub("", str(text or ""))
    t = _LEAD.sub("", t).replace("用户", "你")
    if not t or _OFF.search(t) or any(re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", t) for a in acts):
        return ""
    if len(t) > THOUGHT_CHARS:
        cut = max(t.rfind(c, 0, THOUGHT_CHARS) for c in _STOP)
        t = t[:cut] if cut >= 6 else t[:THOUGHT_CHARS - 1] + "…"
    return t.strip(_STOP[:2] + ",")


def _json_obj(text: str):
    """模型输出里的第一个 JSON 对象 (云端模型可能套代码块、前后加话)."""
    s = str(text or "")
    i = s.find("{")
    while i >= 0:
        try:
            return json.JSONDecoder().raw_decode(s[i:])[0]
        except ValueError:
            i = s.find("{", i + 1)
    return None


def parse_output(text: str, req: dict, acts: dict) -> dict | None:
    """模型输出 -> {pick, thought, how}; pick 不在候选里 (云端模型没有约束解码) 返回 None, 页面按本机规则挑."""
    obj = _json_obj(text)
    if not isinstance(obj, dict) or obj.get("pick") not in req["options"]:
        return None
    how = [h for h in dict.fromkeys(obj.get("how") or []) if isinstance(h, str) and h in HOW][:2] if isinstance(obj.get("how"), list) else []
    return {"pick": obj["pick"], "thought": clean_thought(obj.get("thought"), acts), "how": how}


# -- mock (llm.engine=mock): 不用模型, 按规则给一份, 跑通协议和页面用 ---------------------------------------
_WHY = {"energy": "有点困了", "food": "肚子有点饿", "water": "有点渴了", "fun": "有点无聊", "social": "有点想你"}


def mock_plan(req: dict, acts: dict) -> dict:
    """最缺的那项 (低于 50) 挑补得最多的事; 都不缺挑候选里最近没做过的. 疼着、摔过慢一点, 上次摔了稳一点, 想你了先挥手."""
    needs, options = req["needs"], req["options"]
    pick = why = None
    for k in sorted((k for k, v in needs.items() if v < 50), key=lambda k: needs[k]):
        best = max(options, key=lambda o: gives(o, req, acts).get(k, 0))
        if gives(best, req, acts).get(k, 0) > 0:
            pick, why = best, k
            break
    if pick is None:
        done = {r["id"] for r in req["recent"]}
        pick = next((o for o in options if o not in done), options[0])
    going = acts[pick]["going"] or f"去{acts[pick]['name']}"
    hurt = req["injured"] or (req["falls"] and "身上")
    lead = f"{hurt}还有点疼" if hurt and not why else _WHY.get(why, "闲着也是闲着")
    how = []
    if req["injured"] or req["falls"]:
        how.append("slow")
    if any(p["act"] == pick and p["falls"] for p in req["practice"]):
        how.append("careful")
    if why == "social":
        how.append("wave")
    return {"pick": pick, "thought": clean_thought(f"{lead}，{going}吧", acts), "how": how[:2]}

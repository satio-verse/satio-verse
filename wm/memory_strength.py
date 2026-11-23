"""强度和稳定性 (记忆按脑区重设计阶段 5, LTP / LTD + 艾宾浩斯; 方案见知识库 [[记忆系统按脑区重设计方案（2026-11-01）]] 阶段 5,
细化见 backups/mem5-strength/计划.md). 规则, 不调模型, 不花钱.

每条记忆有两个量, 存在旁路文件 <角色>/记忆/.strength.json (点开头, 不进检索; 不改她的 Markdown, 不改 .recall.json):
  强度 σ     0..1, 没被动过是 sigma0 (0.5). 想起并用上、下一句没出问题 / 被肯定 / 被你复述 / 你确认了 -> σ += η(1-σ);
             用上之后下一句被纠正 -> σ -= η·σ. 同一条同一天加最多一次 (取较大的 η)、减最多一次 (防富者愈富).
  稳定性 s   s = s0 · m, s0 = half_life / ln2 (30 天 -> 43.28 天), m = min(max_x, (1 + a·r)(1 + b·max(0, 显著性 - 0.2)) · x_情绪).
             r = 有正向信号的日子数 (用上且没事 / 肯定 / 复述 / 确认, 记在 rd 里); 只是被检索带进提示词的日子不算.
             保持率 R = O.freshness(age, half_life · m); 没复习过、不显著、没情绪的条目 m 恰好是 1, 分数和现在逐位相同.
检索用 fresh_eff = clamp(R · g, 0, 1), g = clamp(σ / σ0, g_min, g_max): 加强只能抵消衰减, 不会超过现在的最高分;
减弱最多压到 g_min. 联想带出的 (assoc) 不加不减. 种子 seed:habit:2 ("早上好") 被纠正到 σ < dormant 就休眠 (不再主动提, 不删).

开关 memory.strength: {"mode": off / shadow / on, ...}. **on 档在代码里挡死 (协调对话 11-15)**: 配置写 on 也按 shadow 跑, 日志打一句 ON_BLOCKED;
要开只能改代码, 测试 / 评测台用 Memory(cfg, _test_unlock_strength_on=True) 才跑得出 on. off: 不读不写不算; shadow: 用旧的新鲜度出结果, 同时算新的, 写旁路和影子日志
(<角色>/记忆/.strength-shadow.jsonl: 只有键的短哈希和数字, 没有记忆正文、没有提问); on: 用新的.
信号只来自主人本人的话 (桌宠上 pipeline.Session 的 recall -> observe); 群、访客、陪看、她主动开口、日记本检索都不登记.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path

FILE = ".strength.json"
SHADOW_FILE = ".strength-shadow.jsonl"
VERDICT_FILE = ".strength-verdicts.jsonl"  # 影子判定记录: 每结算一次记一行 (日期、哪档、哪条规则、涉及哪几条记忆的短哈希、是不是种子), 没有正文和用户的话
ON_BLOCKED = "纠正的识别还不够准，on 档暂不开放"
MODES = ("off", "shadow", "on")
SEED_HELLO = "seed:habit:2"
HELLO_LINE = "先说声早上好"  # memory.py 的 _morning_hello 提示的那句
DEFAULTS = {"sigma0": 0.5, "g_min": 0.2, "g_max": 1.2, "a": 0.35, "b": 0.5, "max_x": 6.0, "eta_use": 0.06, "eta_repeat": 0.15,
            "eta_confirm": 0.30, "eta_down": 0.35, "verdict_log": 500, "dormant": 0.25, "window_min": 30, "shadow_log": 300, "used_ratio": 0.6,
            "salience_base": 0.2}
POSITIVE = ("use", "repeat", "confirm", "affirm")  # 有正向信号的 (这一天进 rd)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ------------------------------------------------------------------------------------------------ 纠正 / 肯定 / 拒绝
_PUNCT = re.compile(r"[\s，,。.！!？?；;：:、~…\-—]+")
_LEAD = re.compile(r"^(?:嗯+|哦+|啊+|呃+|唉+|哎+)[，,、 ]*")
_HEDGE = re.compile(r"等一下|等等|等会|我想想|让我想|想一下|想一想|再想想|我看看|我查查")
_SOFT = "啊呀吧哦啦嘛呐"                                                      # 句末语气词
_FIXED = re.compile(r"^(?:你)?(?:记|说|搞|弄|理解|听|写|认|想)(?:错|反|岔|混)[了啦]?")  # 你记错了 / 说反了 / 记岔了 / 你弄错了
# 规则二 (协调对话 11-15): "不对"后面紧跟的是标点、句末、语气字, 或者再跟一个"不对", 才算纠正; 后面接别的字 (不对劲、不对称、不对啊我想想) 归"疑"
_BUDUI = re.compile(r"^(?:这)?(?:不对)+[" + _SOFT + r"]*(?=$|[\s，,。.！!？?；;：:、~…])")
_BUDUI_ANY = re.compile(r"^(?:这)?不对")
_NOT_SO = re.compile(r"^(?:根本|压根|完全)?不是(?:这样|那样|这么|那么|这个意思|那个意思|这回事|这么回事|那么回事)")  # 不是这样 / 根本不是这么回事
_NOT_SO2 = re.compile(r"^不是的[，, ]*(?:你)?(?:记|说|搞|弄)(?:错|反|岔)[了啦]?")           # 不是的，你记错了
_WRONG = re.compile(r"^错[了啦](?!错)")                                       # 错了，我是… / 错啦，是上周 (错了错了 是他自己认错; 错过了 不是)
_SELF_SLIP = re.compile(r"我(?:刚才|刚|不小心)|[按点打输发写拿走看选买]错")          # 错了，我刚才按错键了: 说的是他自己的失误
_BLAME = re.compile(r"^你(?:乱|瞎|胡)(?:说|讲|编)")                              # 你乱说
_NEVER = re.compile(r"^我(?:从来|压根|根本)?没(?:有)?(?:这么|这样)?(?:说|讲)过|^我(?:啥|什么|哪)时候(?:说|讲)过")  # 我没说过 / 我啥时候说过这个
_YES_CLAUSE = re.compile(
    r"^(?:嗯+|哦+)?(?:(?:对+[啊呀的呢]?){1,3}|没错(?:没错)?|没毛病|是的|是啊|是呢|是这样(?:没错|的)?|正是这样|就是这样|就是这个|(?:就是){1,2}|"
    r"(?:你)?说得?对(?:了)?|你记得(?:很|真|特别)(?:准|清楚|对)|你记性(?:真|很)好)$")
# 拒绝 (只对种子"早上好"): 要带上早上 / 起床 / 打招呼这一类上下文, 光有"别叫我…"不算 (别叫我宝宝、不要叫外卖了)
_NOT = r"(?:别(?!忘)|不要|不用|甭)"
_MORN = r"(?:这么早|一大早|大早上|大清早|早上|早晨|一早|清晨)"
_NOSAY = r"[^，,。！!？?]"
_REFUSE = re.compile(
    _NOT + _NOSAY + r"{0,8}?(?:打招呼|招呼|问好|早安|早上好)|"
    + _MORN + _NOSAY + r"{0,6}?" + _NOT + _NOSAY + r"{0,8}?(?:叫|喊|吵|醒)|"
    + _NOT + _NOSAY + r"{0,6}?" + _MORN + _NOSAY + r"{0,4}?(?:叫|喊|吵|醒|打招呼|问好)")


# 疑: 带纠正 / 否定意味的标记, 或者带问句意味的词 (没有问号也算) —— 判不准的一概不动. 宁可少加强, 也不能让被纠正的反而被加强.
_DOUBT = re.compile(
    r"不对|不是|不不|不可能|不应该|错|没说|没讲|没有说|记混|记成|记岔|记反|说成|搞混|弄混|搞反|瞎说|乱说|胡说|胡扯|哪有|你又|明明|根本|压根|其实|"
    r"我是说|我的意思|应该是|而是|怎么|什么|啥|哪|谁|来着|为什么|为啥|多少|是不是|对不对|行不行|能不能|吗|[么嘛呢吧]\s*$")
_QWORD = re.compile(r"怎么|什么|啥|哪|谁|来着|为什么|为啥|多少|是不是|(?<!不)对不对|行不行|能不能|吗|[么嘛呢吧]\s*$|[？?]")


# 规则一 (评审第 2 轮复查后, 用户 11-15 定"再修一步"): "无"(加一点)只给明确的应声 / 道谢 / 笑 / 道别, 别的一律是"疑"——
# 这样漏判的纠正不管说得多新, 都不可能落进"无"被当成"用上、没事"去加强; 最多是"疑" (不动).
_ACK = re.compile(
    r"^(?:嗯+|哦+|噢+|啊+|好+|好的|好吧|好呀|好哒|行|行吧|可以|可以的|ok|OK|okay|知道了|明白了|懂了|了解|收到|谢谢你?|谢啦|多谢|感谢|哈+|嘿+|呵+|嘻+|"
    r"不错|挺好|真好|太好了|很好|厉害|真棒|棒|原来如此|这样啊|晚安|拜拜|再见|早安|早上好|午安)[啊呀哈哒啦吧哦嘛~]*$")
# 规则二: "纠"(减弱)要窄: 句子不长, 没有转折 / 条件, 说的不是第三个人的话; 够不上的一律降成"疑", 宁可不减弱, 也不能误伤一条没错的记忆.
_NOT_TIGHT = re.compile(r"但是|不过|可是|然而|除非|如果|要是|假如|万一|或者|还是|他|她|它|别人|大家|人家")


def _firm(t: str) -> str:
    return "疑" if len(t) > 30 or _NOT_TIGHT.search(t) else "纠"


# 规则一 (协调对话 11-15): 句子里带否定字 (没、不、别、错、无、非) 的, 不能判成"无", 判不准的归"疑"
_NEG_CHARS = re.compile(r"[没不别错无非]")


def classify_why(text: str) -> tuple[str, str]:
    """主人下一句话 -> (档, 命中的规则名). 五档:
      "纠" 有把握的纠正 (减弱);  "肯" 有把握的肯定 (加得多);  "拒" 拒绝 (只对种子);
      "无" 明确的应声 / 道谢 / 笑 / 道别, 而且句子里没有否定字 (加一点);
      "疑" 带纠正 / 否定 / 问句意味的标记但又判不准的, 什么都不动.
    宁可漏判也不误判, 而且漏掉的纠正不能落进"无"或"肯"被当成"用上、没事"去加强 (评审 11-15): 所以先认有把握的纠 / 肯 / 拒,
    剩下的只要带 _DOUBT 里的标记就是"疑"; 问句、说得太长也是"疑"; "无"只给明确的应声. 规则名写进影子判定记录, 以后拿来和模型判的对比.
    上限 (协调对话 11-15 的结论): 要做到不伤人, 召回就几乎是 0; "不是她，是我同事"这类得对照用上的那条记忆才判得出, 要靠模型."""
    t = (text or "").strip()
    if not t:
        return "无", "空句"
    if len(t) > 60:
        return "疑", "疑:太长"
    if _QWORD.search(t):
        return "疑", "疑:问句词"
    raw = t
    t = _LEAD.sub("", t)
    t = re.sub(r"^不是不是[，, ]*", "", t)  # 不是不是，你弄错了
    if _REFUSE.search(t):
        return "拒", "拒:早上/打招呼"
    clauses = [c for c in _PUNCT.split(t) if c]
    if clauses and len(clauses) <= 4 and all(_YES_CLAUSE.match(c) for c in clauses):
        return "肯", "肯:整句是肯定"
    for name, pat in (("纠:你记错/说反", _FIXED), ("纠:不是这样", _NOT_SO), ("纠:不是的你记错", _NOT_SO2), ("纠:你乱说", _BLAME), ("纠:我没说过", _NEVER)):
        if pat.match(t):
            return (_firm(t), name) if _firm(t) == "纠" else ("疑", "疑:纠正不够窄")
    m = _WRONG.match(t)
    if m:
        if _SELF_SLIP.search(t[m.end():]):
            return "疑", "疑:自己的失误"
        return (_firm(t), "纠:错了") if _firm(t) == "纠" else ("疑", "疑:纠正不够窄")
    m = _BUDUI.match(t)
    if m:
        if _HEDGE.search(t[m.end():]):
            return "疑", "疑:不对+犹豫"
        return (_firm(t), "纠:不对") if _firm(t) == "纠" else ("疑", "疑:纠正不够窄")
    if _BUDUI_ANY.match(t):
        return "疑", "疑:不对后面接别的字"
    if _DOUBT.search(t):
        return "疑", "疑:否定/纠正标记"
    if _NEG_CHARS.search(raw):
        return "疑", "疑:带否定字不能判无"
    plain = [c for c in _PUNCT.split(raw) if c]  # 开头的"嗯"已被 _LEAD 吃掉, 应声要按原句看
    if plain and len(plain) <= 3 and all(_ACK.match(c) for c in plain):
        return "无", "无:应声"
    return "疑", "疑:不是应声"


def classify(text: str) -> str:
    return classify_why(text)[0]


# ------------------------------------------------------------------------------------------------ 用上了没有
_DATE = re.compile(r"\d+\s*月\s*\d+\s*[日号]?|\d+")
_HAN = re.compile(r"[一-鿿]")
_FILL = set("的了吧呢啊呀我你他她它把这那个件事一些关于用户在是有")
_GREET = re.compile(r"早上好|早安|早啊|早呀|早晨好")


def content_chars(text: str) -> set:
    """一条记忆的实词字: 去掉日期、数字、"用户"和虚词 (日期不算"用上了": 两件同一天的事回复里带着日期就都算用上, 不对)."""
    return {c for c in _HAN.findall(_DATE.sub("", text or "")) if c not in _FILL}


def used(reply: str, text: str, ratio: float = DEFAULTS["used_ratio"]) -> bool:
    """回复里说到了这条记忆吗: 实词字在回复里出现的比例 >= ratio, 并且至少 min(2, 实词字数) 个."""
    want = content_chars(text)
    if not want:
        return False
    have = want & set(_HAN.findall(reply or ""))
    return len(have) >= min(2, len(want)) and len(have) / len(want) >= ratio


def greeted(reply: str) -> bool:
    return bool(_GREET.search(reply or ""))


def key_hash(key: str) -> str:
    return hashlib.sha1((key or "").encode("utf-8")).hexdigest()[:8]


# ------------------------------------------------------------------------------------------------ 旁路
class StrengthStore:
    """.strength.json: {"v": 1, "items": {记忆正文 (或 seed:xxx): {"s": σ, "rd": [有正向信号的日子], "up": [日, 当天开头的σ, 当天用的η],
    "dn": [日, 当天开头的σ], "n": {事件: 次数}, "log": [[日, 事件, σ], ...最近 6 条]}}}. 文件改了 (这边写的) 才重读;
    坏文件、空文件、嵌套极深都当"没有". 调的人负责加锁 (Memory._lock)."""

    def __init__(self, path_fn, shadow_fn, cfg: dict | None = None):
        cfg = cfg or {}
        self.path_fn, self.shadow_fn = path_fn, shadow_fn
        self.mode = cfg.get("mode", "off") if cfg.get("mode", "off") in MODES else "off"
        self.p = {k: float(cfg.get(k, v)) for k, v in DEFAULTS.items()}
        self.p["sigma0"] = _clamp(self.p["sigma0"], 0.05, 0.95)
        self.p["g_min"] = _clamp(self.p["g_min"], 0.0, 1.0)
        self.p["g_max"] = max(1.0, self.p["g_max"])
        self._items: dict = {}
        self._sig = None
        self._sal: dict = {}

    # -- 读写 ------------------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return Path(self.path_fn())

    def version(self):
        if self.mode == "off":
            return ("off",)
        try:
            st = self.path.stat()
            return (self.mode, st.st_mtime_ns, st.st_size)
        except OSError:
            return (self.mode, None, 0)

    def refresh(self):
        """看一眼旁路文件改没改 (一次 stat), 改了就重读; 返回版本 (给缓存当 key). 每轮检索 / 每次结算开头调一次,
        items() 平时不再 stat (一轮里每块记忆都要查, 3000 块不能每块 stat 一次)."""
        sig = self.version()
        if self.mode != "off" and sig != self._sig:
            items: dict = {}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                raw = data.get("items") if isinstance(data, dict) else None
                for k, e in (raw.items() if isinstance(raw, dict) else ()):
                    if isinstance(k, str) and isinstance(e, dict) and isinstance(e.get("s"), (int, float)):
                        items[k] = e
            except (OSError, ValueError, RecursionError):
                items = {}
            self._items, self._sig = items, sig
        return sig

    def items(self) -> dict:
        if self.mode == "off":
            return {}
        if self._sig is None:
            self.refresh()
        return self._items

    def _write(self) -> None:
        path = self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                f.write(json.dumps({"v": 1, "items": self._items}, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            self._sig = self.version()
        except OSError:
            self._sig = None  # 写不上: 下次重读磁盘上的, 检索照常

    # -- 量 --------------------------------------------------------------------------------
    def sigma(self, key: str) -> float:
        e = self.items().get(key)
        return float(e["s"]) if e else self.p["sigma0"]

    def rd(self, key: str) -> int:
        e = self.items().get(key)
        return len(e.get("rd") or []) if e else 0

    def g_of(self, key: str) -> float:
        """强度进检索的系数; 没被动过恰好是 1.0."""
        e = self.items().get(key)
        if not e:
            return 1.0
        return _clamp(float(e["s"]) / self.p["sigma0"], self.p["g_min"], self.p["g_max"])

    def mult(self, key: str, sal: float, x_affect: float = 1.0, cap: float | None = None) -> float:
        """稳定性相对 s0 的倍数 m; 没复习过、不显著、没情绪的条目恰好是 1.0.
        显著性项 (1 + b·(sal−0.2)) 和情绪项 x_affect 是同一类效果 (让某些事淡得慢, 阶段 4 的 4b 就是后者): 两项的乘积合起来不超过
        cap (= affect.max_halflife_x, 默认 3), 取"合起来封顶"而不是"取大", 因为 b 项为基线时乘积就是 x_affect, 阶段 4 的行为一个字不变;
        复习天数项 (1 + a·r) 不受这个限制, 总上限还是 max_x."""
        p = self.p
        soft = (1 + p["b"] * max(0.0, sal - p["salience_base"])) * x_affect
        if cap is not None:
            soft = min(soft, max(1.0, cap))
        m = (1 + p["a"] * self.rd(key)) * soft
        return min(p["max_x"], m) if m > 1.0 else m

    def fresh_eff(self, R: float, key: str) -> float:
        return _clamp(R * self.g_of(key), 0.0, 1.0)

    def dormant(self, key: str = SEED_HELLO) -> bool:
        return self.mode == "on" and self.sigma(key) < self.p["dormant"]

    def view(self, key: str) -> dict:
        e = self.items().get(key) or {}
        return {"sigma": round(float(e.get("s", self.p["sigma0"])), 3), "g": round(self.g_of(key), 3), "reviews": len(e.get("rd") or []),
                "events": dict(e.get("n") or {})}

    # -- 更新 ------------------------------------------------------------------------------
    def _entry(self, key: str) -> dict:
        return self.items().setdefault(key, {"s": self.p["sigma0"]}) if self.mode != "off" else {}

    def reinforce(self, key: str, kind: str, day: str, mult: float = 1.0) -> bool:
        """加强 (kind: use / repeat / confirm / affirm). 同一条同一天只加一次: 当天已加过, 比较 η, 较大的补上差额, 较小的不动.
        mult: 情绪学习率倍率 (0.5–2). 返回 σ 有没有变."""
        if self.mode == "off" or not key:
            return False
        p = self.p
        eta = {"use": p["eta_use"], "repeat": p["eta_repeat"], "confirm": p["eta_confirm"], "affirm": p["eta_confirm"]}[kind]
        eta = _clamp(eta * _clamp(mult, 0.5, 2.0), 0.0, 0.95)
        e = self._entry(key)
        before = float(e["s"])
        up = e.get("up")
        if isinstance(up, list) and len(up) == 3 and up[0] == day:
            base, prev_eta = float(up[1]), float(up[2])
            if eta > prev_eta:
                e["s"] = _clamp(base + eta * (1 - base), 0.0, 1.0)
                e["up"] = [day, base, eta]
        else:
            e["s"] = _clamp(before + eta * (1 - before), 0.0, 1.0)
            e["up"] = [day, before, eta]
        rd = sorted(set(list(e.get("rd") or []) + [day]))
        e["rd"] = rd[-60:]
        self._note(e, day, kind)
        self._write()
        return float(e["s"]) != before

    def weaken(self, key: str, kind: str, day: str) -> bool:
        """减弱 (kind: correct / refuse / superseded): σ -= η_down·σ; 同一条同一天最多一次."""
        if self.mode == "off" or not key:
            return False
        e = self._entry(key)
        dn = e.get("dn")
        if isinstance(dn, list) and len(dn) == 2 and dn[0] == day:
            return False
        before = float(e["s"])
        e["s"] = _clamp(before - self.p["eta_down"] * before, 0.0, 1.0)
        e["dn"] = [day, before]
        up = e.get("up")
        if isinstance(up, list) and len(up) == 3 and up[0] == day:
            up[1] = e["s"]  # 当天稍后再加, 从减过之后的算起
        self._note(e, day, kind)
        self._write()
        return True

    def _note(self, e: dict, day: str, kind: str) -> None:
        n = e.setdefault("n", {})
        n[kind] = int(n.get(kind, 0)) + 1
        e["log"] = (list(e.get("log") or []) + [[day, kind, round(float(e["s"]), 3)]])[-6:]

    def reset(self, key: str) -> bool:
        """"恢复出厂": 去掉这条的强度记录."""
        if self.mode == "off" or key not in self.items():
            return False
        del self._items[key]
        self._write()
        return True

    def prune(self, alive: set) -> int:
        """库里和候选里都已经没有的条目清掉 (seed:* 留着)."""
        if self.mode == "off":
            return 0
        drop = [k for k in self.items() if not k.startswith("seed:") and k not in alive]
        for k in drop:
            del self._items[k]
        if drop:
            self._write()
        return len(drop)

    def verdict_append(self, day: str, tier: str, rule: str, keys: list, seed: bool) -> None:
        """影子判定记录 (记忆/.strength-verdicts.jsonl): 每结算一次记一行 —— 日期、判成哪档、命中哪条规则、涉及哪几条记忆的短哈希、是不是种子.
        不记正文, 不记用户的话; 只留最近 verdict_log 行 (砍最旧的). 写不上就算了. off 档不记."""
        if self.mode == "off":
            return
        path = Path(self.shadow_fn()).with_name(VERDICT_FILE)
        try:
            try:
                old = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                old = []
            row = json.dumps({"day": day, "tier": tier, "rule": rule, "k": [key_hash(k) for k in keys][:8], "seed": bool(seed)}, ensure_ascii=False)
            keep = (old + [row])[-int(self.p["verdict_log"]):]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    # -- 影子日志 (只有键的短哈希和数字) --------------------------------------------------------
    def shadow_append(self, day: str, rows: list) -> None:
        """rows: [(键, 旧名次, 新名次, g, m)]; 名次 0 是没进; 只留最近 shadow_log 行. 写不上就算了."""
        if self.mode != "shadow" or not rows:
            return
        path = Path(self.shadow_fn())
        try:
            try:
                old = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                old = []
            new = [json.dumps({"day": day, "k": key_hash(k), "old": o, "new": n, "g": round(g, 3), "m": round(m, 3)}) for k, o, n, g, m in rows]
            keep = (old + new)[-int(self.p["shadow_log"]):]
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass


def s0_days(half_life: float) -> float:
    """s0 = 半衰期 / ln 2: 30 天 -> 43.28 天 (方案页写的 43 是它的近似)."""
    return half_life / math.log(2) if half_life > 0 else float("inf")

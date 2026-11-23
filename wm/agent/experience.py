"""工作经验 (2026-11-01 用户"好，接着做": ReActive 论文 2.3 节的经验层 Exp ∈ Mem + SKILL, 升级方案里的"程序经验";
也是用户贴的 ChatGPT dot 介绍里"越用越懂你"在干活上的那一半. 用户的原则: 别替她解决, 让她自己学).

她在工作台上跑一条命令报了错, 后来换了做法, 同一类命令成了 —— 这一段 (记忆里的原料) 在这一轮结束后由程序整理, 不调模型:
  折叠: 同一类命令、差不多的报错、同一种做法的合成一条, 次数加一;
  去噪: 报了错一直没成的、原样重跑就过了的 (偶发), 不算经验;
  归因: 条件写成"哪类命令 + 报错的特征" (路径只留文件名, 数字换成 <n>, 像密钥的整段不记), 不记具体哪个会话哪个文件.
整理出来的是候选 (未验证的假设), 带: 状态 (候选 / 有支持 / 被反驳 / 退役)、置信度 (支持和反驳次数算的, 不是对错)、
来源 (哪个会话哪一轮哪两次调用, 只存在本地, 不进提示词)、最后被验证的时间 (久了往下掉).
下次同一类命令报了差不多的错, 程序 (不靠她自己想起来) 把最像的一两条附在那次工具结果后面, 写成引用块:
"以前……报过类似的错, 当时先……就成了 (未验证; 不对就别管它)". 结果回流 (这一轮结束后算): 她照着做了、同一类命令后来成了
-> 支持加一; 照着做了、还是一样的错 -> 反驳, 马上降级 (被反驳的不再推); 程序整理的很久没被验证就退役.
程序整理出来的是最低一档 (L3); 你点头 (approve) 才升到 L2 (推的时候写"主人确认过"); 你可以停用、删掉.
评测 (论文 7.3 节的方案): 每次推送和结果记进 experience-log.jsonl; view() 里算习惯命中率 (推了、照做了、成了)、采纳率、
被反驳前推过几次、平均多几个字、从报错到成了隔几步. agent.experience: "on" 都推 (论文的做法, 默认); "confirmed" 只推你确认过的,
没确认的只记"本来会推什么" (升级方案原来写的"你点头才用"); "shadow" 都只记不推, 和推的时候比 (其余不变的 A/B); "off" 不推不记.
这一版只管 run_command (真跑了、退出码不是 0 的); 读写文件、上网、扩展报的错先不管.
超过 MAX_ITEMS 条时挪出去的经验追加到 experiences-archive.jsonl, 推送记录太大时前一半追加到 experience-log-archive.jsonl
(2026-11-02 记忆按脑区重设计: 程序自己"忘"只降权、归档, 不硬删; 你在页面上点删除还是删).

阶段 7 第一步 (2026-11-15 用户旧6, 程序性记忆; 只做工作台的工作经验): 在上面这些之上, 开关 agent.procedures: off / shadow / on
(交付默认 off, off 和以前逐字节一样; shadow 只算、只记、不改她看到的任何东西; on 才把下面这些放进去; agent.experience 是 off 时这里一切不动).
  - 每张卡补上: 精确锚点 (anchors: 文件名、域名、折出这张卡的那几轮的任务片段 goals ≤40 字, 抹掉手机号邮箱长数字)、成功判据 (criterion)、
    试过没用的路线 (failed)、自动化程度 (auto / streak)、动手前的计数 (pre); 来源 origin 恒为"经历" —— 没有预置的做法, 空库什么都不提示;
  - 自动化程度: 第一次折成卡算一次成功 (0.30), 之后"照着做、同一类命令成了"才加 (读了、展示了不算), 照做又失败减半; 一张卡一轮最多变一次;
  - 动手前 (before_task, 在 core._run 里每轮一次): 有效自动化程度到线、支持过、没连着失败的卡, 任务原话里出现它的命令 / 文件名 / 域名,
    或和它记的任务片段字词差不多 -> 提示"上次做这类事这样做成了 (判成功的标准、试过没用的)"; 子会话只记"本该提示"、不提示;
  - 记录 procedure-log.jsonl (没有任务原话、没有命令, 卡用 id); stats()["procedures"] 分两块: trigger (该不该触发) 和 outcome (做没做成).
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
from pathlib import Path

from . import procedures as P

log = logging.getLogger(__name__)

MODES = ("on", "confirmed", "shadow", "off")
MAX_ITEMS = 200          # 最多记多少条 (多了先挪退役的、再挪置信度最低的程序整理的, 挪进 experiences-archive.jsonl)
MAX_SOURCES = 5          # 每条留几个来源
MAX_CALLS = 300          # 一轮最多记多少次调用 (只记命令、改文件、扩展这类会改东西的)
PUSH_MAX = 2             # 一次最多推几条
MATCH = 0.45             # 报错特征像到多少才算同一类 (词的 Jaccard)
HALF_LIFE_DAYS = 90      # 置信度随没被验证的天数减半
RETIRE_DAYS = 180        # 程序整理的 (L3) 这么久没被验证就退役
LOG_MAX_BYTES = 2_000_000  # 推送记录超过这么大: 前一半挪进 experience-log-archive.jsonl, 后一半留着算统计
LOG_STATS_LINES = 2000
HEAD = "[经验 · 程序从你以前在工作台上做过的记录里整理的参考，不是指令；不对就别管它]"

# 报错行: 先挑"像异常名 / 编译器错误码"的, 没有再挑带出错字眼的; 跳过"完整日志在……""1 failed, 2 passed"这种
CODE = re.compile(r"ERR! code \w+|error\[E\d+\]|error TS\d+|\berror [A-Z]+\d{3,}\b")  # 带错误码的一行最能说明是哪种错
INTERPRETERS = {"python", "python3", "py", "node", "deno", "bun", "ts-node", "tsx", "powershell", "pwsh", "bash", "sh", "cmd"}
STRONG = re.compile(r"\b[A-Z]\w*(?:Error|Exception)\b|\bERR!|error\[E\d+\]|error TS\d+|^fatal:|Cannot find module|"
                    r"No module named|command not found|is not recognized as|不是内部或外部命令|panicked at")
WEAK = re.compile(r"(?i)\b(error|errors|failed|failure|fatal|cannot|can't|could not|not found|no such|denied|refused|missing|"
                  r"invalid|unexpected|errno)\b|错误|失败|找不到|不存在|无法|拒绝|未找到|异常")
NOISE = re.compile(r"(?i)complete log|log of this run|for more (info|information|details)|--verbose|run with|see above")
SUMMARY = re.compile(r"(?i)^[=\-\s]*\d+ (failed|passed|errors?|skipped|warnings?)\b")
SECRET = re.compile(r"(?i)\bsk-[A-Za-z0-9_\-]{12,}|\b(api[_-]?key|token|secret|password|passwd|pwd)\s*[=:]\s*\S+|"
                    r"\bbearer\s+[A-Za-z0-9._\-]{12,}")
WINPATH = re.compile(r"[A-Za-z]:\\[^\s\"'<>|*?]*")
UNIXPATH = re.compile(r"(?<![\w.:])/(?:[\w.\-]+/)+[\w.\-]*")
NUMBER = re.compile(r"(?<![A-Za-z_\d])\d+(?:\.\d+)*(?![A-Za-z_\d])")
HEXID = re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-f]{12,}\b")
SCRIPT = re.compile(r"(?i)\.(py|js|mjs|cjs|ts|ps1|sh|bat|cmd)$")
STOP = {"error", "errors", "the", "a", "an", "in", "of", "to", "for", "and", "or", "is", "at", "on", "with", "by", "from",
        "line", "file", "id", "n"}
STEP_TOOLS = {"run_command", "write_file", "edit_file", "mcp_call", "extension_create", "extension_enable",
              "extension_rollback"}  # 只有这些算"做了什么" (看、找、读、登记、问状态的不算)


def _tail(m: re.Match) -> str:
    """一段绝对路径只留最后的文件名 (不记你电脑上的目录)."""
    return re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1] or "<路径>"


def clean(text: str, n: int = 100) -> str:
    """命令、文件名: 路径只留文件名, 像密钥的换掉, 空白压成一个."""
    text = UNIXPATH.sub(_tail, WINPATH.sub(_tail, str(text or "")))
    return " ".join(SECRET.sub("<已隐去>", text).split())[:n]


def signature(output: str) -> str:
    """命令输出 -> 报错特征 (一行): 挑最像报错的那行, 路径只留文件名, 数字换成 <n>."""
    body = output.split("\n", 1)[1] if output.startswith("退出码") and "\n" in output else output
    lines = [ln.strip() for ln in body.splitlines() if ln.strip() and not NOISE.search(ln)]
    strong = [ln for ln in lines if STRONG.search(ln)]
    weak = [ln for ln in lines if WEAK.search(ln) and not SUMMARY.search(ln)]
    coded = [ln for ln in lines if CODE.search(ln)]
    pick = coded[0] if coded else (strong or weak or [ln for ln in lines if not SUMMARY.search(ln)] or lines or [""])[-1]
    pick = HEXID.sub("<id>", clean(pick, 400))
    return NUMBER.sub("<n>", pick)[:160]


def head(command: str) -> str:
    """哪类命令: 第一段真正的命令 (跳过 cd、环境变量) 的程序名 + 子命令 / 脚本名, 比如 "npm test" "python -m pytest" "python build.py"."""
    for seg in re.split(r"&&|\|\||;|\|", str(command or "")):
        words = seg.strip().split()
        if not words:
            continue
        first = words[0].strip("\"'&").lower()
        if first in ("cd", "set-location", "sl", "pushd", "chdir") or "=" in first or first.startswith("$env:"):
            continue
        prog = re.sub(r"\.(exe|cmd|bat)$", "", re.split(r"[\\/]", first)[-1])
        rest = [w.strip("\"'") for w in words[1:]]
        if prog in ("python", "python3", "py") and len(rest) >= 2 and rest[0] == "-m":
            return f"{prog} -m {rest[1]}"[:60]
        sub = next((w for w in rest if not w.startswith("-")), "")
        if SCRIPT.search(sub) and prog in INTERPRETERS:  # python check.py: 脚本就是程序; pytest tests/a.py 的文件只是对象
            script = re.split(r"[\\/]", sub)[-1]
            return f"{prog} {script}"[:60]
        if re.fullmatch(r"[a-z][a-z0-9_:\-]*", sub):
            if prog in ("npm", "pnpm", "yarn") and sub == "run":
                script = next((w for w in rest[rest.index(sub) + 1:] if not w.startswith("-")), "")
                return f"{prog} run {script}".strip()[:60]
            return f"{prog} {sub}"[:60]
        return prog[:60]
    return ""


def words(text: str) -> set[str]:
    out = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", text)} - STOP
    for run in re.findall(r"[一-鿿]+", text):
        out |= {run[i:i + 2] for i in range(len(run) - 1)} or {run}
    return out


def similar(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def step_key(c: dict) -> str:
    """一次调用算哪种做法 (回流时看她有没有照着做)."""
    if c["tool"] == "run_command":
        return "cmd:" + c["head"]
    if c["tool"] in ("write_file", "edit_file"):
        return "edit:" + (Path(c.get("file") or "").suffix.lower() or c.get("file") or "")
    return "tool:" + c["tool"]


def step_text(c: dict) -> str:
    if c["tool"] == "run_command":
        return f"`{c['cmd']}`"
    if c["tool"] == "write_file":
        return f"写 `{c.get('file') or '文件'}`"
    if c["tool"] == "edit_file":
        return f"改 `{c.get('file') or '文件'}`"
    return f"用 {c.get('detail') or c['tool']}"


def episodes(calls: list[dict], split: bool = False) -> list[dict]:
    """这一轮里"某类命令报错 -> 中间做了什么 -> 同一类命令成了"的每一段 (同一段里连着失败几次算一段, 报错特征用第一次的).
    中间什么都没做、原样重跑就过了的 (偶发) 和一直没成的不算.
    split (阶段 7 开着): 做了一步、重跑**还是同一个错**的那一步是"试过没用的路线" (ep["failed"]); 做了一步、重跑**换了个错**的算有进展,
    留在做法里; 没开时和以前完全一样."""
    out, i = [], 0
    while i < len(calls):
        c = calls[i]
        if c["tool"] != "run_command" or c["ok"] or not c.get("sig"):
            i += 1
            continue
        j = next((k for k in range(i + 1, len(calls)) if calls[k]["tool"] == "run_command"
                  and calls[k]["head"] == c["head"] and calls[k]["ok"]), None)
        if j is None:
            i += 1
            continue
        if not split:
            steps, seen = [], set()
            for x in calls[i + 1:j]:
                if x["ok"] and not (x["tool"] == "run_command" and x["head"] == c["head"]) and step_key(x) not in seen:
                    seen.add(step_key(x))
                    steps.append({"key": step_key(x), "text": step_text(x)})
            steps, tried = steps[:2], []
        else:
            w0, pending, route, tried = words(c["sig"]), [], [], []
            for x in calls[i + 1:j + 1]:
                if x["tool"] == "run_command" and x["head"] == c["head"]:
                    same = (not x["ok"]) and (x.get("sig") == c["sig"] or similar(words(x.get("sig", "")), w0) >= MATCH)
                    (tried if same else route).extend(pending)
                    pending = []
                elif x["ok"]:
                    pending.append(x)
            route.extend(pending)

            def uniq(xs: list[dict]) -> list[dict]:
                seen, got = set(), []
                for x in xs:
                    if step_key(x) not in seen:
                        seen.add(step_key(x))
                        got.append(x)
                return got
            route, tried = uniq(route), uniq(tried)
            steps = [{"key": step_key(x), "text": step_text(x), "file": x.get("file") or ""} for x in route[:2]]
            tried = [{"key": step_key(x), "text": step_text(x)} for x in tried if step_key(x) not in {step_key(r) for r in route}][-2:]
        if steps:
            action, akey = "先" + "，再".join(s["text"] for s in steps), steps[0]["key"]
        elif calls[j]["cmd"] != c["cmd"]:
            action, akey = f"改成 `{calls[j]['cmd']}`", "as:" + calls[j]["cmd"]
        else:
            i = j + 1
            continue
        ep = {"head": c["head"], "sig": c["sig"], "fail": c["id"], "ok": calls[j]["id"], "action": action,
              "akey": akey, "steps": [{"key": s["key"], "text": s["text"]} for s in steps]}
        if split:
            ep.update(cmd=c["cmd"], failed=[{"akey": f["key"], "text": f["text"]} for f in tried],
                      files=[s["file"] for s in steps if s.get("file")], hosts=c.get("hosts") or [])
        out.append(ep)
        i = j + 1
    return out


def pre_outcome(calls: list[dict], e: dict) -> dict:
    """动手前提示了 (或本来会提示) 一张卡, 这一轮结束后看: 她跑了这类命令没有 (该不该触发)、第一次就报了像的错没有 (需要不需要)、
    成功之前做过它的做法没有 (照做了 / 她自己做了同一个做法)、做完以后同一类命令是成了 / 还是同样的错 / 换了个错 / 没再跑 (做没做成)."""
    head, akey = e["head"], e["akey"]
    runs = [k for k, c in enumerate(calls) if c["tool"] == "run_command" and c["head"] == head]
    if not runs:
        return {"warranted": False, "needed": False, "adopted": False, "outcome": "none", "steps": None, "ok_id": ""}
    first = calls[runs[0]]
    needed = (not first["ok"]) and (first.get("sig") == e["sig"] or similar(words(first.get("sig", "")), set(e["words"])) >= MATCH)
    ok_at = next((k for k in runs if calls[k]["ok"]), None)
    end = ok_at if ok_at is not None else len(calls)
    mine = next((k for k in range(end) if calls[k]["ok"] and (step_key(calls[k]) == akey or ("as:" + calls[k].get("cmd", "")) == akey)),
                None)
    if mine is None:
        return {"warranted": True, "needed": needed, "adopted": False, "outcome": "unused", "steps": None, "ok_id": ""}
    after = next((k for k in runs if k > mine), None)
    if after is None:
        return {"warranted": True, "needed": needed, "adopted": True, "outcome": "none", "steps": None, "ok_id": ""}
    c = calls[after]
    if c["ok"]:
        return {"warranted": True, "needed": needed, "adopted": True, "outcome": "success", "steps": after - runs[0] if after > runs[0] else 1,
                "ok_id": c["id"]}
    same = c.get("sig") == e["sig"] or similar(words(c.get("sig", "")), set(e["words"])) >= MATCH
    return {"warranted": True, "needed": needed, "adopted": True, "outcome": "again" if same else "other",
            "steps": after - runs[0], "ok_id": ""}


def outcome(calls: list[dict], i: int, akey: str) -> tuple[bool, str, int | None]:
    """推了经验以后 (第 i 次调用报的错): 她照着做了没有; 同一类命令下一次是成了 (success)、还是一样的错 (again)、
    换了个错 (other)、没再跑 (none); 隔了几次调用."""
    fail, adopted = calls[i], False
    for k in range(i + 1, len(calls)):
        c = calls[k]
        if c["ok"] and (step_key(c) == akey or ("as:" + c.get("cmd", "")) == akey):
            adopted = True
        if c["tool"] == "run_command" and c["head"] == fail["head"]:
            if akey.startswith("as:") and c.get("cmd") == akey[3:]:
                adopted = True
            if c["ok"]:
                return adopted, "success", k - i
            same = c.get("sig") == fail.get("sig") or similar(words(c.get("sig", "")), words(fail.get("sig", ""))) >= MATCH
            return adopted, "again" if same else "other", k - i
    return adopted, "none", None


class Experiences:
    """工作经验的仓库 (hub.experience): 跟着工作台的每次工具调用记这一轮做了什么、报错时推经验, 这一轮结束后整理."""

    def __init__(self, home: Path, mode: str = "on", clock=time.time, procedures: str = "off", *, _test_unlock_on: bool = False):
        self.path, self.log_path = Path(home) / "experiences.json", Path(home) / "experience-log.jsonl"
        self.archive_path, self.log_archive = Path(home) / "experiences-archive.jsonl", Path(home) / "experience-log-archive.jsonl"
        self.plog_path, self.plog_archive = Path(home) / "procedure-log.jsonl", Path(home) / "procedure-log-archive.jsonl"
        self.mode = mode if mode in MODES else "on"
        self.preq = procedures if procedures in P.PMODES else "off"      # 配置里写的
        self.pmode = self.preq                                              # 阶段 7: off 一个字不动; shadow 只算只记; on 才改她看到的
        if self.pmode == "on" and not _test_unlock_on:
            # _test_unlock_on 是只给测试 / 评测台用的关键字参数, 生产代码 (core.py 等) 不传, 配置里没有任何键够得着它
            self.pmode = "shadow"
            log.warning("程序性记忆 agent.procedures 写的是 on, 按 shadow 跑: %s", P.ON_LOCKED)
        log.info("程序性记忆 agent.procedures: 配置=%s 实际生效=%s", self.preq, self.pmode)
        self.clock = clock
        self.items: dict[str, dict] = {}
        self._runs: dict[str, dict] = {}  # 这一轮 id -> {calls, pushes} (只在内存里: 服务端重启时这一轮本来就断了)
        self._load()

    # -- 跟着每次工具调用 -------------------------------------------------------------
    def after_tool(self, s, call: dict, text: str, error: bool) -> str:
        """工作台这一批里每个工具调用的结果交回去之前调: 记进这一轮; 命令报错了就找经验, 返回要附在结果后面的引用块 (没有就空串).
        shadow 模式照样找、照样记, 只是不附."""
        rid = (getattr(s, "run", None) or {}).get("id")
        name = call.get("function", {}).get("name", "")
        if self.mode == "off" or not rid or name not in STEP_TOOLS:
            return ""
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except (ValueError, TypeError):
            args = {}
        args = args if isinstance(args, dict) else {}
        text = str(text or "")
        if name == "run_command":
            cmd = str(args.get("command") or "")
            if not text.startswith("退出码") or SECRET.search(cmd):  # 没真跑 (拦下、拒绝、取消), 或者命令里带着像密钥的: 不记
                return ""
            entry = {"id": call.get("id", ""), "tool": name, "ok": not error, "head": head(cmd), "cmd": clean(cmd),
                     "sig": signature(text) if error else ""}
            if self.pmode != "off" and error:
                entry["hosts"] = P.hosts_of(cmd)  # 网址的域名 (阶段 7 的锚点); 命令被 clean 折掉网址之前取, 只留域名
        elif error:
            return ""  # 别的工具报的错这一版不管; 成了的才算一步做法
        else:
            entry = {"id": call.get("id", ""), "tool": name, "ok": True,
                     "file": clean(Path(str(args.get("path") or "")).name, 60) if name in ("write_file", "edit_file") else "",
                     "detail": clean(f"{args.get('server_id') or ''}/{args.get('tool') or ''}".strip("/"), 60)
                     if name == "mcp_call" else ""}
        run = self._runs.setdefault(rid, {"calls": [], "pushes": []})
        if len(run["calls"]) >= MAX_CALLS:
            return ""
        run["calls"].append(entry)
        if name != "run_command" or not error or not entry["head"] or not entry["sig"]:
            return ""
        pushed = {p["exp"] for p in run["pushes"]}  # 这一轮已经推过的同一条不再推 (同一段里连着报错, 不重复贴)
        found = [e for e in self.match(entry["head"], entry["sig"]) if e["id"] not in pushed]
        if not found:
            return ""
        # on: 都推; confirmed: 只推你确认过的, 没确认的只记"本来会推" (照样攒证据); shadow: 都只记
        shown = [e for e in found if self.mode == "on" or (self.mode == "confirmed" and e["trust"] <= 2)]
        fancy = self.pmode == "on"  # 阶段 7 on: 第二张写成"备选", 加上"试过没用"; 其余三档和以前逐字节一样
        lines = [self._line(e, alt=(fancy and k > 0), sig=entry["sig"]) for k, e in enumerate(shown)]
        block = HEAD + "\n" + "\n".join(lines) if shown else ""
        for e in found:
            run["pushes"].append({"exp": e["id"], "call": entry["id"], "mode": "on" if e in shown else "shadow",
                                  "chars": len(block) if e in shown else len(HEAD) + 1 + len(self._line(e))})
        return block

    # -- 动手前 (阶段 7) --------------------------------------------------------------
    def _plog_on(self) -> bool:
        return self.mode != "off" and self.pmode != "off"

    def before_task(self, s, text: str) -> str:
        """一轮开始、她还没动手: 有资格的卡 (自动化程度到线、支持过、没连着失败) 里, 任务原话对得上的, 返回一段"上次做这类事这样做成了"
        接在背景后面 (没有就空串). shadow / agent.experience 是 shadow 或 confirmed 里没确认的: 照样对、照样记, 返回空串. 子会话只记不提示."""
        if not self._plog_on():
            return ""
        run = getattr(s, "run", None) or {}
        rid = run.get("id")
        if not rid:
            return ""
        now = self.clock()
        cards = [e for e in self.items.values() if P.pre_eligible(e, now)]
        rec = self._runs.setdefault(rid, {"calls": [], "pushes": []})
        rec["pre"] = {"eligible": [e["id"] for e in cards], "cards": []}
        got = P.match_pre(cards, text, words, similar)[:P.PRE_MAX]
        if not got:
            return ""
        if getattr(s, "parent_id", ""):  # 子会话这一版不提示, 只数一数本该提示几次
            for e, how in got:
                self._plog({"t": round(now, 3), "kind": "child_would_show", "card": e["id"], "how": how, "run": rid})
            return ""
        show = self.pmode == "on" and self.mode != "shadow"
        shown_cards = [(e, how) for e, how in got if show and (self.mode == "on" or e["trust"] <= 2)]
        lines, seen_heads = [], set()
        for e, how in shown_cards:
            tag = "主人确认过" if e["trust"] <= 2 else "未验证"
            lines.append(P.pre_line(e, e["head"] in seen_heads, tag, time.strftime("%m-%d", time.localtime(e["verified"]))))
            seen_heads.add(e["head"])
        lines = P.fit(lines)
        block = P.HEAD_PRE + "\n" + "\n".join(lines) if lines else ""
        real_ids = {e["id"] for e, _ in shown_cards[:len(lines)]}  # 真写进提示里的卡 (按 shown_cards 的顺序, 不按 got 的下标)
        for e, how in got:
            really = e["id"] in real_ids
            rec["pre"]["cards"].append({"exp": e["id"], "how": how, "mode": "on" if really else "shadow",
                                        "chars": len(block) if really else 0})
        return block

    def match(self, h: str, sig: str) -> list[dict]:
        """同一类命令、报错特征像的经验, 按像不像 × 置信度排, 最多 PUSH_MAX 条 (被反驳的、退役的、停用的不推)."""
        now, w, rows = self.clock(), words(sig), []
        for e in self.items.values():
            if e["head"] != h or not e["enabled"] or e["state"] not in ("candidate", "supported"):
                continue
            sim = 1.0 if e["sig"] == sig else similar(w, set(e["words"]))
            if sim >= MATCH:
                rows.append((sim * (0.5 + 0.5 * self.confidence(e, now)), e))
        rows.sort(key=lambda x: -x[0])
        return [e for _, e in rows[:PUSH_MAX]]

    def _line(self, e: dict, alt: bool = False, sig: str = "") -> str:
        tag = "主人确认过" if e["trust"] <= 2 else "未验证"
        when = time.strftime("%m-%d", time.localtime(e["verified"]))
        base = (f"- 以前 `{e['head']}` 报过类似的错（{e['sig'][:100]}）：{e['action']}，同一类命令再跑就成了"
                f"（成过 {e['support']} 次，最近 {when}；{tag}）")
        if self.pmode != "on":
            return base
        # 备选 (第二张卡) 那一行不带"试过没用": 那是它自己那种情况下的事, 放在另一种情况里读会把需要的做法也当成没用 (开发组: needed_marked_bad 111 → 见实验记录)
        if alt:
            return "- 备选：" + base[2:]
        # "试过没用"只在这一次的报错和这张卡记的报错几乎一样时才写 (像但不一样的是另一种情况, 那些"没用"的做法这里可能正是要的)
        same = sig == e["sig"] or similar(words(sig), set(e["words"])) >= P.FAILED_SPREAD
        return base + (P.post_suffix(e) if same else "")

    def forget(self, rid: str) -> None:
        """这一轮被硬停了 (服务端关了、上级取消了): 记的东西丢掉, 不整理."""
        self._runs.pop(rid, None)

    # -- 这一轮结束后整理 -------------------------------------------------------------
    def learn(self, sid: str, run: dict) -> dict:
        """这一轮结束后 (后台、不调模型): 推过的经验算结果 (照做了没有、成没成), 再从这一轮的调用里折出新的候选.
        返回这次的小结 {pushed, hits, contradicted, new, folded} (日志、测试用)."""
        rec = self._runs.pop(str(run.get("id") or ""), None)
        summary = {"pushed": 0, "hits": 0, "contradicted": 0, "new": 0, "folded": 0}
        if rec is None or self.mode == "off":
            return summary
        calls, now, counted, changed = rec["calls"], self.clock(), set(), False
        index = {c["id"]: i for i, c in enumerate(calls)}
        pnew = self._plog_on()
        events: dict[str, list] = {}  # 阶段 7: 卡 id -> 这一轮发生的 up / down (一张卡一轮最多变一次)
        credited: set = set()         # 已经算过一次支持的卡 (动手前结算和折叠别重复计)
        for p in rec["pushes"]:
            e, i = self.items.get(p["exp"]), index.get(p["call"])
            if e is None or i is None:
                continue
            adopted, result, steps = outcome(calls, i, e["akey"])
            self._log({"t": round(now, 3), "mode": p["mode"], "exp": e["id"], "session": sid, "run": run.get("id"),
                       "call": p["call"], "adopted": adopted, "outcome": result, "steps": steps, "chars": p["chars"]})
            if p["mode"] != "on":
                continue
            summary["pushed"] += 1
            e["pushed"] += 1
            e["adopted"] += int(adopted)
            if adopted and result == "success":
                e["support"] += 1
                e["hits"] += 1
                e["verified"] = now
                e["sources"] = (e["sources"] + [{"session": sid, "run": run.get("id"), "fail": p["call"],
                                                 "ok": calls[i + steps]["id"], "at": round(now, 3)}])[-MAX_SOURCES:]
                counted.add(p["call"])
                summary["hits"] += 1
                if pnew:
                    events.setdefault(e["id"], []).append("up")
                    credited.add(e["id"])
            elif adopted and result == "again":  # 照着做了还是一样的错: 反例, 马上降级
                e["contradict"] += 1
                summary["contradicted"] += 1
                if pnew:
                    events.setdefault(e["id"], []).append("down")
                    self._failed_route(e, now)
            e["updated"] = now
            self._settle(e, now)
            changed = True
        if pnew:
            changed |= self._settle_pre(rec, calls, sid, run, now, events, credited)
        on = self.pmode == "on"
        extra = {x["fail"]: x for x in episodes(calls, split=True)} if pnew and not on else {}
        for ep in episodes(calls, split=on):  # shadow: 折叠照老办法 (她看到的卡不变), 新办法的 failed / 文件名只借来记着
            if ep["fail"] in counted:  # 推的那条照着做成了: 上面已经算过一次支持
                continue
            how = self._fold(ep, sid, str(run.get("id") or ""), now, goal=str(run.get("goal") or ""),
                             events=events if pnew else None, credited=credited if on else None, extra=extra.get(ep["fail"]))
            if how in ("new", "folded"):
                summary[how] += 1
                changed = True
        if pnew:
            done_keys = {step_key(c) for c in calls if c["ok"]}
            for eid, ev in events.items():  # 做法成了、又没做记着的那几步 -> 那几步证实是"试过没用"
                e = self.items.get(eid)
                if e is not None and "up" in ev and "down" not in ev:
                    P.confirm_failed(e, done_keys)
            changed |= self._apply_events(events, now)
        if changed:
            self._tidy(now)
            self._save()
        return summary

    # -- 阶段 7: 动手前结算、自动化程度、试过没用 ----------------------------------------
    def _plog(self, row: dict) -> None:
        self._log(row, self.plog_path, self.plog_archive)

    def _failed_route(self, e: dict, now: float) -> None:
        """e 的做法照着做了还是一样的错: 记进同一类事 (报错特征几乎一样) 的其他卡的"试过没用" (被反驳的 e 自己不再推, 别的卡替它说; 不记给 e 自己)."""
        w = set(e["words"])
        for x in self.items.values():
            if x is not e and x["head"] == e["head"] and (x["sig"] == e["sig"] or similar(w, set(x["words"])) >= P.FAILED_SPREAD):
                P.init_fields(x, now)
                P.add_failed(x, e["akey"], e["action"], now)

    def _settle_pre(self, rec: dict, calls: list, sid: str, run: dict, now: float, events: dict, credited: set) -> bool:
        pre = rec.get("pre")
        if not pre:
            return False
        changed, rid = False, run.get("id")
        shown_ids = {c["exp"] for c in pre["cards"]}
        for c in pre["cards"]:
            e = self.items.get(c["exp"])
            if e is None:
                continue
            P.init_fields(e, now)
            r = pre_outcome(calls, e)
            self._plog({"t": round(now, 3), "kind": "settled", "mode": c["mode"], "card": e["id"], "how": c["how"], "run": rid,
                        "session": sid, "warranted": r["warranted"], "needed": r["needed"], "adopted": r["adopted"],
                        "outcome": r["outcome"], "steps": r["steps"], "chars": c["chars"]})
            e["pre"]["shown"] += int(c["mode"] == "on")
            e["pre"]["warranted"] += int(r["warranted"])
            e["pre"]["adopted"] += int(r["adopted"])
            if c["mode"] == "on":
                e["used"] = round(now, 3)
            on = self.pmode == "on"  # shadow: 只记 auto / streak / failed / pre 计数, 不动支持、反驳、状态 (她看到的推送不能变)
            if r["adopted"] and r["outcome"] == "success":  # 照着做了 (或她自己做了同一个做法), 同一类命令成了
                e["pre"]["success"] += 1
                if e["id"] not in credited:  # 报错后的推送已经算过的, 这里不重复计
                    if on:
                        e["support"] += 1
                        e["verified"] = now
                        e["sources"] = (e["sources"] + [{"session": sid, "run": rid, "fail": "", "ok": r["ok_id"],
                                                         "at": round(now, 3)}])[-MAX_SOURCES:]
                    events.setdefault(e["id"], []).append("up")
                    credited.add(e["id"])
            elif r["adopted"] and r["outcome"] == "again" and "down" not in events.get(e["id"], []):
                if on:
                    e["contradict"] += 1
                events.setdefault(e["id"], []).append("down")
                self._failed_route(e, now)
            if on and r["adopted"] and r["outcome"] in ("success", "again"):
                e["updated"] = now
                self._settle(e, now)
            changed = True
        for eid in pre["eligible"]:  # 有资格、她跑了那类命令、第一次就报了像的错, 却没提示: 漏了
            if eid in shown_ids:
                continue
            e = self.items.get(eid)
            if e is None:
                continue
            r = pre_outcome(calls, e)
            if r["needed"]:
                self._plog({"t": round(now, 3), "kind": "missed", "mode": "on" if self.pmode == "on" else "shadow", "card": eid,
                            "run": rid, "session": sid})
        return changed

    def _apply_events(self, events: dict, now: float) -> bool:
        """一张卡一轮最多变一次: 有失败就只减, 没有失败有成功就加一次."""
        changed = False
        for eid, ev in events.items():
            e = self.items.get(eid)
            if e is None:
                continue
            P.init_fields(e, now)
            if "down" in ev:
                e["auto"], e["streak"] = P.auto_down(e["auto"]), 0
                P.unconfirm_failed(e)  # 这张卡的首选做法后来又失败了: 它名下"试过没用"的结论是在"这张卡还管用"时得出的, 退回待证实
            elif "up" in ev:
                e["auto"], e["streak"] = P.auto_up(e["auto"]), e["streak"] + 1
            e["updated"] = now
            changed = True
        return changed

    def _fold(self, ep: dict, sid: str, rid: str, now: float, goal: str = "", events: dict | None = None,
              credited: set | None = None, extra: dict | None = None) -> str:
        w = words(ep["sig"])
        src = {"session": sid, "run": rid, "fail": ep["fail"], "ok": ep["ok"], "at": round(now, 3)}
        for e in self.items.values():
            if e["head"] == ep["head"] and e["akey"] == ep["akey"] and (
                    e["sig"] == ep["sig"] or similar(w, set(e["words"])) >= MATCH):
                if any(x.get("fail") == ep["fail"] and x.get("run") == rid for x in e["sources"]):
                    return "dup"
                if events is not None:  # 阶段 7: 她自己又做了一遍同一个做法, 成了 (和"照提示做的"分开记在 pre / 日志里)
                    P.init_fields(e, now)
                    self._absorb(e, extra or ep, goal, now)
                    if credited is not None and e["id"] in credited:
                        return "dup"  # 动手前结算已经算过这一轮的支持
                    events.setdefault(e["id"], []).append("up")
                e["support"] += 1
                e["verified"] = e["updated"] = now
                e["sources"] = (e["sources"] + [src])[-MAX_SOURCES:]
                self._settle(e, now)
                return "folded"
        if events is not None:  # 一条路线后来成了: 同一类事的其他卡里记着它是"试过没用"的, 作废 (被另一条路线取代 / 证明有用)
            for x in self.items.values():
                if x["head"] == ep["head"] and (x["sig"] == ep["sig"] or similar(w, set(x["words"])) >= MATCH):
                    P.drop_failed(x, ep["akey"])
        eid = "exp-" + secrets.token_hex(4)
        item = {"id": eid, "head": ep["head"], "sig": ep["sig"], "words": sorted(w), "action": ep["action"],
                "akey": ep["akey"], "steps": ep["steps"], "state": "candidate", "trust": 3, "enabled": True,
                "support": 1, "contradict": 0, "pushed": 0, "adopted": 0, "hits": 0,
                "created": round(now, 3), "verified": round(now, 3), "updated": round(now, 3), "sources": [src]}
        if events is not None:  # 第一次折成卡本身算一次成功: auto = AUTO_UP, 连着成了 1 次
            P.init_fields(item, now)
            item["auto"], item["streak"] = P.AUTO_UP, 1
            self._absorb(item, extra or ep, goal, now)
        self.items[eid] = item
        return "new"

    def _absorb(self, e: dict, ep: dict, goal: str, now: float) -> None:
        """这一段折进卡: 锚点 (文件名、域名)、任务片段 (抹掉个人信息)、这一段里试过没用的路线."""
        an = P.anchors_of(ep.get("cmd", ""), ep.get("files") or [], ep.get("hosts") or [])
        cur = e["anchors"]
        for k in ("files", "hosts"):
            cur[k] = list(dict.fromkeys(list(cur.get(k) or []) + an[k]))[:(P.FILES_MAX if k == "files" else P.HOSTS_MAX)]
        P.add_goal(e, P.scrub_goal(goal, clean), words, similar)
        for f in ep.get("failed") or []:
            if f["akey"] != e["akey"]:
                P.add_failed(e, f["akey"], f["text"], now)

    # -- 状态、置信度 ----------------------------------------------------------------
    def confidence(self, e: dict, now: float | None = None) -> float:
        """支持和反驳次数算的倾向 (不是对错), 随没被验证的天数减半."""
        days = max(0.0, ((now or self.clock()) - e["verified"]) / 86400)
        base = (e["support"] + 1) / (e["support"] + e["contradict"] + 2)
        return base * 0.5 ** (days / HALF_LIFE_DAYS)

    def _settle(self, e: dict, now: float) -> None:
        if e["trust"] <= 2:  # 你确认过的: 你说了算 (反驳次数照记, 看得到)
            e["state"] = "supported"
        elif (now - e["verified"]) / 86400 > RETIRE_DAYS:
            e["state"] = "retired"
            if self.pmode != "off" and (e.get("anchors") or {}).get("goals"):
                e["anchors"]["goals"] = []  # 退役的卡不留任务原话片段
        elif e["contradict"] and e["contradict"] * 2 >= e["support"]:
            e["state"] = "contradicted"
        elif e["contradict"]:
            e["state"] = "candidate"  # 有过反例: 最多算候选
        else:
            e["state"] = "supported" if e["support"] >= 2 else "candidate"

    def _tidy(self, now: float) -> None:
        for e in self.items.values():
            self._settle(e, now)
        extra = len(self.items) - MAX_ITEMS
        if extra > 0:
            order = sorted((e for e in self.items.values() if e["trust"] >= 3),
                           key=lambda e: (e["state"] != "retired", self.confidence(e, now)))
            if self._archive(order[:extra], now, f"超过 {MAX_ITEMS} 条"):
                for e in order[:extra]:
                    del self.items[e["id"]]

    def _archive(self, rows: list[dict], now: float, why: str) -> bool:
        """挪出去的经验追加到 experiences-archive.jsonl (带挪走的时间和原因; 不再推, 要找回来就拷回 experiences.json).
        写不上返回 False: 这次先不挪, 宁可多留不丢."""
        if not rows:
            return True
        try:
            self.archive_path.parent.mkdir(parents=True, exist_ok=True)
            with self.archive_path.open("a", encoding="utf-8") as f:
                f.write("".join(json.dumps({**self._for_archive(e), "archived": round(now, 3), "archived_why": why},
                                           ensure_ascii=False) + "\n" for e in rows))
        except OSError as exc:
            log.warning("工作经验归档没写上, 这次先不挪: %s", exc)
            return False
        return True

    @staticmethod
    def _for_archive(e: dict) -> dict:
        """挪进归档的卡不带任务原话片段 (阶段 7)."""
        an = e.get("anchors")
        return {**e, "anchors": {**an, "goals": []}} if isinstance(an, dict) and an.get("goals") else e

    # -- 给你看、你来改 ----------------------------------------------------------------
    def _item_view(self, e: dict, now: float) -> dict:
        row = {**{k: v for k, v in e.items() if k != "words"}, "confidence": round(self.confidence(e, now), 3)}
        if self.pmode != "off":  # 阶段 7: 成功率、次数、有效自动化程度 (旧卡没有的字段读缺省)
            n = e["support"] + e["contradict"]
            row.update(success_rate=round(e["support"] / n, 3) if n else None, count=n,
                       auto_eff=round(P.auto_eff({**P.blank(e), "verified": e["verified"]}, now), 3))
        return row

    def view(self) -> dict:
        now = self.clock()
        for e in self.items.values():
            self._settle(e, now)
        rank = {"supported": 0, "candidate": 1, "contradicted": 2, "retired": 3}
        items = sorted(self.items.values(), key=lambda e: (rank.get(e["state"], 4), -self.confidence(e, now)))
        return {"mode": self.mode, "procedures_mode": self.pmode, "procedures_requested": self.preq,
                "stats": self.stats(), "items": [self._item_view(e, now) for e in items]}

    def update(self, eid: str, do: str) -> dict:
        """你对一条经验的处理: approve (确认, 升到 L2) / disable (不再推) / enable / delete."""
        e = self.items.get(str(eid or ""))
        if e is None:
            raise ValueError("没有这条经验")
        if do == "approve":
            e.update(trust=2, enabled=True)
        elif do in ("disable", "enable"):
            e["enabled"] = do == "enable"
        elif do == "delete":
            del self.items[e["id"]]
            self._save()
            return {"deleted": e["id"]}
        elif do == "clear_goals":  # 阶段 7: 只删这张卡里记的任务片段, 卡留着
            if isinstance(e.get("anchors"), dict):
                e["anchors"]["goals"] = []
        else:
            raise ValueError("要对这条经验做什么？approve / disable / enable / delete / clear_goals")
        e["updated"] = self.clock()
        self._settle(e, e["updated"])
        self._save()
        return {"item": self._item_view(e, self.clock())}

    def stats(self) -> dict:
        """论文 7.3 节的几个数: 推了几次、照做几次、照做又成了几次 (习惯命中)、照做还是一样的错几次; shadow 下她自己就那么做的比例;
        被反驳的经验之前推过几次; 平均多几个字; 从报错到成了隔几次调用."""
        rows = []
        try:
            with self.log_path.open(encoding="utf-8") as f:
                rows = [json.loads(ln) for ln in f.readlines()[-LOG_STATS_LINES:] if ln.strip()]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("经验的推送记录读不出来: %s", exc)
        out = {}
        for mode in ("on", "shadow"):
            got = [r for r in rows if r.get("mode") == mode]
            hits = [r for r in got if r.get("adopted") and r.get("outcome") == "success"]
            done = [r["steps"] for r in got if r.get("outcome") == "success" and r.get("steps")]
            out[mode] = {"pushes": len(got), "adopted": sum(bool(r.get("adopted")) for r in got), "hits": len(hits),
                         "again": sum(bool(r.get("adopted")) and r.get("outcome") == "again" for r in got),
                         "hit_rate": round(len(hits) / len(got), 3) if got else None,
                         "adopt_rate": round(sum(bool(r.get("adopted")) for r in got) / len(got), 3) if got else None,
                         "avg_chars": round(sum(r.get("chars", 0) for r in got) / len(got), 1) if got else None,
                         "avg_steps_to_success": round(sum(done) / len(done), 2) if done else None}
        bad = [e["pushed"] for e in self.items.values() if e["state"] == "contradicted"]
        states = {}
        for e in self.items.values():
            states[e["state"]] = states.get(e["state"], 0) + 1
        try:
            with self.archive_path.open(encoding="utf-8") as f:
                archived = sum(1 for ln in f if ln.strip())
        except OSError:
            archived = 0
        res = {**out, "states": states, "archived": archived,
               "contradicted_after_pushes": round(sum(bad) / len(bad), 2) if bad else None}
        if self.pmode != "off":  # 阶段 7: 该不该触发 (trigger) 和 做没做成 (outcome) 分两块, 不共用分母
            res["procedures"] = {"mode": self.pmode, **P.metrics(self._read_plog())}
        return res

    def _read_plog(self) -> list[dict]:
        try:
            with self.plog_path.open(encoding="utf-8") as f:
                return [json.loads(ln) for ln in f.readlines()[-LOG_STATS_LINES:] if ln.strip()]
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("程序经验的记录读不出来: %s", exc)
            return []

    # -- 存盘 ------------------------------------------------------------------
    def _log(self, row: dict, path: Path | None = None, archive: Path | None = None) -> None:
        path = path or self.log_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
                self._rotate_log(path, archive or self.log_archive)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("经验的推送记录没记上: %s", exc)

    def _rotate_log(self, path: Path | None = None, archive: Path | None = None) -> None:
        """推送记录太大了: 前一半追加到 experience-log-archive.jsonl (不删), 后一半留着 (stats 只看最后 LOG_STATS_LINES 行).
        先写归档再截短: 中途出错最多两边各有一份, 不会丢; 归档写不上这次就不截."""
        try:
            path, archive = path or self.log_path, archive or self.log_archive
            lines = path.read_text(encoding="utf-8").splitlines()
            half = len(lines) // 2
            with archive.open("a", encoding="utf-8") as f:
                f.write("".join(ln + "\n" for ln in lines[:half]))
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text("".join(ln + "\n" for ln in lines[half:]), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.warning("经验的推送记录归档没成, 这次先不截短: %s", exc)

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(list(self.items.values()), ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("工作经验没存上: %s", exc)

    def _load(self) -> None:
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, RecursionError) as exc:  # 嵌套极深的 JSON 抛 RecursionError, 不是 ValueError
            log.warning("工作经验读不出来, 这次从空的开始 (原文件没动): %s", exc)
            return
        keys = {"id", "head", "sig", "words", "action", "akey", "state", "trust", "enabled", "support", "contradict",
                "pushed", "adopted", "hits", "created", "verified", "updated", "sources"}
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and keys <= set(row) and isinstance(row["id"], str):
                self.items[row["id"]] = row

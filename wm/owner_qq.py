"""主人私聊 (QQ / 微信) 里的原话 -> 记忆候选 (路线图 A 档: 只进候选, 等确认).

为什么需要它: 夜间巩固 (wm/digest.py) 只吃桌宠 / 语音本体写的 <库>/<角色>/日志/, 访客会话按设计
不写主人记忆 (wm/channels/__init__.py:9). 于是坐在电脑前说的话会被整理, 而在 QQ 上说的话一条也
进不去 -- 实测 11-18 / 11-19 两天 digest 候选 0 条, 而 QQ 上有三万九千 token 的对话.

这条只管"主人自己打的字": 群聊、别人说的话、转发、表情包、纯链接一律不碰. 判定走和日志整理
同一道把关 (wm/digest.py):
  1. 规则初筛  candidates_qq()  -- 至少 4 个汉字、不是指令、不是问句、有动作的不算; 不要求带第一人称 (私聊里常说"姥姥姥爷身体好")
  2. 值不值得记  worth_qq()    -- digest.rule_worth 说值得记还要够长 (>=10 汉字) 或带第一人称; 规则说不准时, 长句 (>=24 汉字) 也算一段想法
  **不问模型**: 实测 0.8B 把真偏好判 p=0.07 拒掉, 却放过 "hhhh，可以多跟我聊天". 宁可少提, 不塞噪声.
  3. 去重 select_qq() -- 和已有记忆、已有候选、同批重复的不要
结果只追加到 记忆/候选.md (行尾 #私聊), 主人确认了才转正; **不推进 关系.md 的 digested 进度**
(那份进度只属于日志整理, 动了会让日志整理跳过没整理的日子).

开关: 默认关. memory.owner_qq.enabled=on 或环境变量 WM_OWNER_QQ_CAPTURE=on 才跑 (night.py 会看这个开关).
用法: python -m wm.owner_qq --day 2026-11-19 [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import digest as D
from . import memory_relay as R

log = logging.getLogger("wm.owner_qq")

ENV_ON = "WM_OWNER_QQ_CAPTURE"
ON_VALUES = ("on", "1", "true", "yes")
DEFAULT_DIR = "外部渠道/qq"          # 和 channels.qq.dir 的缺省一致
MAX_TURNS = 400                     # 一天最多看多少句主人说的话 (防日志被刷爆时卡住)
MAX_TEXT = 200                      # 单句太长 (贴了一大段) 只留前 200 字给判定

_LINE = re.compile(r"^- \[(\d{2}:\d{2})([^\]]*)\] ?(.*)$")
_OWNER = re.compile(r"主人\((\d+)\)")
_REPLY = re.compile(r"^\(回复 #-?\d+\)\s*")
_HAN = re.compile(r"[一-鿿]")
_SKIP_HEAD = ("［", "[图片", "[视频", "[语音", "[表情", "http://", "https://")
_WE = re.compile(r"我们|咱们|咱")          # 私聊里说"我们"就是"用户", 不能改写成"用户们"
_ME_ANY = re.compile(r"我|俺|用户")        # 有没有第一人称 (没有的话改写要加"用户提到：")
_OTHER = re.compile(r"^(他|她|他们|她们|别人|人家|朋友说|同学说|同事说|我妈说|我爸说)")  # 说的是别人, 不是用户的事
_LEAD_QQ = re.compile(r"^(那就是|那么|那|这|所以|而且|然后)[，,、 ]*")   # 私聊里的起头话, 留着不像一条事实


# ------------------------------------------------------------------ 路径
def channel_root(cfg: dict | None) -> Path | None:
    """<库>/<渠道目录> (缺省 外部渠道/qq). 没配记忆库 / 目录不合法就 None."""
    mem = ((cfg or {}).get("memory") or {})
    vault = str(mem.get("vault") or "").strip()
    if not vault:
        return None
    raw = str((((cfg or {}).get("channels") or {}).get("qq") or {}).get("dir") or DEFAULT_DIR).strip().replace("\\", "/")
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        return None
    root = Path(vault) / rel
    return root if root.is_dir() else None


def owner_dirs(root: Path | None) -> list[Path]:
    """主人私聊的日志目录: 私聊<号>/日志, 且日志里出现过 "主人(<号>)". 群和别人的私聊不在里面."""
    if root is None or not root.is_dir():
        return []
    out = []
    for d in sorted(root.glob("私聊*")):
        logs = d / "日志"
        if not logs.is_dir():
            continue
        files = sorted(logs.glob("*.md"), reverse=True)[:3]   # 只看最近几天, 不扫全库
        for f in files:
            try:
                if "主人(" in f.read_text(encoding="utf-8", errors="replace")[:120_000]:
                    out.append(d)
                    break
            except OSError:
                continue
    return out


def latest_day(root: Path | None) -> str | None:
    """最近一天有私聊日志的日期 (日志文件名就是日期; 服务器按 UTC+8 落盘)."""
    best = None
    for d in owner_dirs(root):
        for f in (d / "日志").glob("*.md"):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem) and (best is None or f.stem > best):
                best = f.stem
    return best


# ------------------------------------------------------------------ 读日志
def _clean(text: str) -> str:
    t = _REPLY.sub("", text.strip())[:MAX_TEXT]
    return t.strip()


def parse_day(path: Path) -> list[dict]:
    """一天的私聊日志 -> [{time, role, text, reply_to}]. role: owner | self | other.
    只看 "- [HH:MM #id 主人(号)] 原话" 这种行; 群通知、系统行不是这个形状, 自然跳过."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        hhmm, tag, body = m.group(1), m.group(2), _clean(m.group(3))
        if not body:
            continue
        if _OWNER.search(tag):
            role = "owner"
        elif re.search(r"她\s*$", tag) or " 她]" in tag or tag.strip().endswith("她"):
            role = "self"
        elif "主人" in tag:
            role = "owner"
        else:
            role = "other"
        reply_to = re.match(r"\(回复 #(-?\d+)\)", m.group(3).strip())
        out.append({"time": hhmm, "role": role, "text": body,
                    "reply_to": reply_to.group(1) if reply_to else None})
    return out


def entries_for(root: Path | None, day: str) -> list[dict]:
    """一天的私聊 -> 日志整理那种 entry ({"user", "time", "acts"}); 只要主人自己打的字.
    转发 / 表情 / 图片 / 纯链接不要; 少于 2 个汉字的 (好、嗯、哈哈哈哈) 不要."""
    if root is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        return []
    out = []
    for d in owner_dirs(root):
        for turn in parse_day(d / "日志" / f"{day}.md"):
            if turn["role"] != "owner":
                continue
            text = turn["text"]
            if text.startswith(_SKIP_HEAD) or len(_HAN.findall(text)) < 2:
                continue
            out.append({"user": text, "time": turn["time"], "acts": False, "via": "qq"})
            if len(out) >= MAX_TURNS:
                return out
    return out


# ------------------------------------------------------------------ 判定 + 落盘
def candidates_qq(entries: list[dict]) -> list[dict]:
    """和 digest.candidates 一样, 但**不要求带"我"**: 私聊里主人打的字本来就是第一人称
    ("姥姥姥爷身体健康"没有"我", 可是关于他家里人的事). 其余门槛一样: 至少 4 个汉字、不是指令、
    不是问句; 同一句只留第一次."""
    out, seen = [], {}
    for e in entries:
        text = e["user"].strip()
        if len(_HAN.findall(text)) < 4 or D._REQUEST.search(text) or D._ASK.search(text):
            continue
        if _OTHER.search(text):
            continue
        if text in seen:
            seen[text]["times"].append(e["time"])
            continue
        seen[text] = c = {**e, "user": text, "times": [e["time"]]}
        out.append(c)
    return out


def rewrite_qq(text: str, day: str, char: str = "绫音") -> str:
    """私聊原话 -> 一句事实. 先把"我们/咱们/咱"换成"用户", 再走日志整理那套确定的改写
    ("我"->用户、"你"->角色名、相对日期换具体日期). 完全没有第一人称的句子加"用户提到："当主语,
    免得留下一句不知道是谁的事."""
    t = D.rewrite(_WE.sub("用户", _LEAD_QQ.sub("", text)), day, char)
    return t if _ME_ANY.search(text) else f"用户提到：{t}"


def enabled(cfg: dict | None = None) -> bool:
    """WM_OWNER_QQ_CAPTURE 优先; 否则 config memory.owner_qq.enabled (缺省 False)."""
    env = str(os.environ.get(ENV_ON) or "").strip().lower()
    if env:
        return env in ON_VALUES
    raw = ((cfg or {}).get("memory") or {}).get("owner_qq")
    if isinstance(raw, dict):
        value = raw.get("enabled")
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ON_VALUES
    return False


LONG_HAN = 24   # 没有关键词、但汉字这么多: 算"说了一段自己的想法", 值得留一条候选
_LEAD_SCRIPT = re.compile(r"^(我跟你说|跟你说|告诉你|对了|话说|其实|哎|唉|嗯|诶)[，,、 ]*")


def worth_qq(text: str) -> int:
    """私聊里的那句话值不值得进候选: +1 值得, 0/-1 不值得.
    digest.rule_worth 的关键词规则照用, 但私聊太碎, 光有关键词不算 ("所以多思考" 里的"考",
    "一直再优化宝宝绫音" 里的"一直"); rule_worth 说值得记时还要么够长 (>=10 个汉字), 要么带"我"。
    规则说不准 (0) 时, 长句 (>= LONG_HAN 汉字) 说明在讲自己的想法, 留一条; "hhhh 那肯定呀" 这种留不下来。"""
    r = D.rule_worth(text)
    if r < 0:
        return -1
    han = len(_HAN.findall(text))
    if r > 0:
        return 1 if (han >= 10 or _ME_ANY.search(text)) else 0
    return 1 if han >= LONG_HAN else 0


def select_qq(cands: list[dict], day: str, known: str, char: str = "绫音",
              relay: bool = True) -> tuple[list[dict], list[dict]]:
    """候选句 -> (采用的 [{text, times, rule}], 拒掉的 [{text, why, rule}]).
    和 digest.select 的差别: 不问模型 (0.8B 在这类口语上两头都错, 见模块说明), 只用 worth_qq;
    改写用 rewrite_qq ("我们"->"用户", 没有第一人称时加"用户提到：")."""
    known_lines = [D._clean_fact(re.sub(r"^\[[ xX]\]\s*", "", x.lstrip("- ")).replace("~~", ""))
                   for x in known.splitlines() if x.startswith("- ")]
    ok, bad = [], []
    for c in cands:
        if relay and R.borrowed(c["user"]):
            bad.append({"text": c["user"], "why": "转述别人的话", "rule": None})
            continue
        r = worth_qq(c["user"])
        if r <= 0:
            bad.append({"text": c["user"], "why": "不像要记的事", "rule": r})
            continue
        text = rewrite_qq(c["user"], day, char)
        same = next((k for k in known_lines if D._similar(text, k) >= 0.6), None)
        if same is not None or any(D._similar(text, o["text"]) >= 0.6 for o in ok):
            bad.append({"text": text, "why": "重复", "rule": r, **({"same": same} if same is not None else {})})
            continue
        ok.append({"text": text, "times": c["times"], "rule": r})
    return ok, bad


async def capture(mem, cfg: dict, day: str | None = None, *, dry_run: bool = False) -> dict:
    """把一天的主人私聊过一遍 -> 候选. 返回一份记录 (无论 dry_run 都能看).
    mem: wm.memory.Memory (已 init). 不调模型: 见模块说明里的"为什么不用 0.8B 判私聊"."""
    t0 = time.perf_counter()
    root = channel_root(cfg)
    day = str(day or latest_day(root) or (date.today() - timedelta(days=1)).isoformat())
    entries = entries_for(root, day)
    cands = candidates_qq(entries)
    known = mem.summary_known()
    ok, bad = select_qq(cands, day, known, mem.char, bool(getattr(mem, "relay", True)))
    written, skipped = ([], [])
    if ok and not dry_run:
        written, skipped = mem.propose_qq(day, ok)
    rec = {"time": datetime.now().isoformat(timespec="seconds"), "day": day,
           "source": str(root) if root is not None else None,
           "entries": len(entries), "candidates": [c["user"] for c in cands],
           "accepted": [x["text"] for x in ok], "written": written, "skipped": skipped,
           "rejected": bad, "dry_run": dry_run,
           "ms": round((time.perf_counter() - t0) * 1000)}
    return rec


async def _main(argv=None) -> int:
    from .config import load_config
    from .memory import Memory

    ap = argparse.ArgumentParser(description="主人私聊里的原话 -> 记忆候选 (只进候选, 等确认)")
    ap.add_argument("--day", help="哪一天, 形如 2026-11-19 (缺省: 有日志的最近一天)")
    ap.add_argument("--dry-run", action="store_true", help="只打印, 不写候选")
    ap.add_argument("--set", action="append", default=[], help="配置覆盖, 如 memory.vault=D:/vault")
    args = ap.parse_args(argv)
    cfg = load_config(None, args.set)
    mem = Memory(cfg["memory"])
    mem.init()
    rec = await capture(mem, cfg, args.day, dry_run=args.dry_run)
    print(json.dumps(rec, ensure_ascii=False, indent=1))
    return 0 if (rec["written"] or rec["dry_run"]) else (1 if not rec["entries"] else 0)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

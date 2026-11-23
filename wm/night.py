"""夜间巩固 v1 (2026-11-19): 睡前把"她自己的这一天"整理成几句话, 写进自我卡和记忆库.

为什么要有: 自我卡 (wm/self_card.py) 存的是"她认为的自己", 但没人负责往里写她自己的这一天;
日志 (日志/YYYY-MM-DD.md)、事实摘要 (logs/digest-*.jsonl)、心跳 (~/.worldmodel/heartbeat/) 各自都在,
但都是原料. 这一版只用规则把它们拼成 3-5 句第一人称的话 + 1 个想问的问题, 不调模型、不猜.

输入 (全都容错: 缺文件 / 坏行 / 缺字段 -> 跳过, 不抛):
  1. logs/digest-YYYYMMDD.jsonl: 每天的事实摘要. written 优先, 空了才用 accepted[*].text;
     去重、按出现顺序.
  2. <库>/<角色>/日志/YYYY-MM-DD.md: "- 04:56 用户: … → 绫音: …" 的行, 数当天说了几轮.
  3. ~/.worldmodel/heartbeat/beats-YYYYMMDD.jsonl (WM_HEARTBEAT_DIR 可覆盖):
     owner=false 的拍数 × 相邻拍的间隔中位数 = 估算的"你不在的时长"; 估不出来就只报拍数.
  4. 自我卡 开放问题 段: 最新一条当问题候选.

输出: report() 只算不写, write() 才落盘:
  - lines 里带"记住 / 聊了 / 待着"的, 各 append 进自我卡 最近变化;
  - question 非空就 append 进自我卡 开放问题;
  - <库>/<角色>/记忆/今天的我.md 里维护 "## YYYY-MM-DD" 一段, 同一天重写只替换这一段,
    其它日期原样保留 (文件不存在就建, 带 "# 今天的我\n" 头).

开关: WM_NIGHT_REPORT=on 优先, 其次 config 里 memory.night.enabled (缺省 False).
关着 write() 直接返回 {"ok": False, "why": "off"}, 不建目录、不写文件.
手动试跑: python -m wm.night --day 2026-11-19 [--dry-run] [--set memory.vault=...].

不做什么: 不调模型、不编句子; 没有数据就说没有; 自我卡只走 append (两段两种权限),
页面只动自己那一段.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import statistics
import time
from datetime import datetime
from pathlib import Path

from . import self_card as SC
from .config import ROOT, load_config

log = logging.getLogger("wm.night")

ENV_ON = "WM_NIGHT_REPORT"
ENV_BEATS = "WM_HEARTBEAT_DIR"
ON_VALUES = {"on", "1", "true", "yes"}

DIGEST_FMT = "digest-{stamp}.jsonl"
BEATS_FMT = "beats-{stamp}.jsonl"
PAGE_NAME = "今天的我.md"
PAGE_HEAD = "# 今天的我\n"

# build_lines 里算"最近变化"的那几句. 建议句式第 2 句用的是"说了"(今天你跟我说了 N 轮话),
# 和"聊了"同义, 一并收进来, 三句事实才会各进一条.
CHANGE_MARKS = ("记住", "聊了", "说了", "待着")
MAX_LINE = 40
MIN_LINES, MAX_LINES = 3, 5
# 数据太少时凑到 3 句用; 不带任何具体数字, 不冒充数据
FILLERS = ("今天没有别的变化，我把这一天收好了。", "明天再慢慢接着记。")

_SECTION_HDR = re.compile(r"^##\s+\d{4}-\d{2}-\d{2}\s*$", re.M)
_MD_DATE = re.compile(r"(\d{1,2})月(\d{1,2})日")


# ------------------------------------------------------------------ 小工具 (都容错)
def _stamp(day) -> str | None:
    """2026-11-19 -> 20261119; 格式不对返回 None (调用方当没有这一天处理)."""
    try:
        return datetime.strptime(str(day), "%Y-%m-%d").strftime("%Y%m%d")
    except (TypeError, ValueError):
        return None


def _text_of(value) -> str:
    """从 str / {"text": ...} 里取一句话; 其它类型当没有. 压空白、去首尾."""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, dict):
        return _text_of(value.get("text"))
    return ""


def _read_text(path) -> str:
    """读文本; 没有 / 读不出来都是空串 (utf-8-sig 兼容带 BOM 的旧文件)."""
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return ""


def _json_lines(text: str) -> list[dict]:
    """一行一个 JSON 对象; 坏行、空行、非对象行跳过."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _clip(text, limit: int = MAX_LINE) -> str:
    """压成一行, 超长用省略号截到 limit 字以内 (不新增事实)."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if limit <= 0:
        return ""
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _line(prefix: str, body: str, suffix: str = "。") -> str:
    """prefix + 截过的 body + suffix, 整句不超过 MAX_LINE."""
    return (prefix + _clip(body, max(0, MAX_LINE - len(prefix) - len(suffix))) + suffix)[:MAX_LINE]


def _root_of(vault, char):
    """<库>/<角色>; 库或角色为空返回 None (不往当前目录瞎写)."""
    vault_s, char_s = str(vault or "").strip(), str(char or "").strip()
    if not vault_s or not char_s:
        return None
    return Path(vault_s) / char_s


# ------------------------------------------------------------------ 输入 1: 事实摘要
def load_digest(logs_dir, day) -> list[dict]:
    """读 logs/digest-YYYYMMDD.jsonl; 文件没有、行坏了、不是对象都跳过 (返回 [])."""
    stamp = _stamp(day)
    if stamp is None:
        return []
    return _json_lines(_read_text(Path(logs_dir) / DIGEST_FMT.format(stamp=stamp)))


def today_facts(records) -> list[str]:
    """每条记录: written 优先, 空(或没有可用文本)才退回 accepted[*].text; 全局去重、保序."""
    out, seen = [], set()
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        texts: list[str] = []
        written = rec.get("written")
        if isinstance(written, list):
            texts = [t for t in (_text_of(x) for x in written) if t]
        if not texts:  # written 是空 list / 没这个字段 / 里面没一句能用 -> 用 accepted
            accepted = rec.get("accepted")
            if isinstance(accepted, list):
                texts = [t for t in (_text_of(x) for x in accepted) if t]
        for text in texts:
            if text not in seen:
                seen.add(text)
                out.append(text)
    return out


# ------------------------------------------------------------------ 输入 2: 当天说了几轮
def count_turns(log_text) -> int:
    """只数以 "- " 开头、且含 "用户:" 的行 (= 那天说了几轮). 空/坏文本是 0."""
    if not isinstance(log_text, str):
        return 0
    return sum(1 for line in log_text.splitlines() if line.startswith("- ") and "用户:" in line)


# ------------------------------------------------------------------ 输入 3: 心跳
def _parse_at(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def absent(beats) -> dict:
    """owner=false 的拍数 -> {"beats", "minutes"}. 分钟 = 拍数 × 相邻拍间隔中位数;
    时间戳不够两个 (或全是坏的) 时 minutes=None, 只报拍数."""
    if not isinstance(beats, (list, tuple)):
        return {"beats": 0, "minutes": None}
    n, stamps = 0, []
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        if not beat.get("owner"):  # 主人在 -> False; 缺字段也当不在 (她一个人)
            n += 1
        at = _parse_at(beat.get("at"))
        if at is None:
            continue
        try:
            stamps.append(at.timestamp())
        except (OverflowError, OSError, ValueError):
            continue
    minutes = None
    if n > 0 and len(stamps) >= 2:
        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
        if gaps:
            gap = statistics.median(gaps)
            if gap > 0:
                minutes = int(round(n * gap / 60.0))
    return {"beats": n, "minutes": minutes}


def _beats_path(day, beats_dir=None):
    """~/.worldmodel/heartbeat/beats-YYYYMMDD.jsonl; 显式参数优先, 其次 WM_HEARTBEAT_DIR."""
    stamp = _stamp(day)
    if stamp is None:
        return None
    raw = beats_dir if beats_dir is not None else os.environ.get(ENV_BEATS, "")
    raw = str(raw or "").strip()
    base = Path(os.path.expanduser(raw)) if raw else Path.home() / ".worldmodel" / "heartbeat"
    return base / BEATS_FMT.format(stamp=stamp)


def _load_beats(day, beats_dir=None) -> list[dict]:
    path = _beats_path(day, beats_dir)
    return [] if path is None else _json_lines(_read_text(path))


# ------------------------------------------------------------------ 输入 4: 自我卡开放问题
def _open_questions(root) -> list[str]:
    """自我卡 auto.开放问题 里的文本, 按写入顺序 (最后一条最新); 没卡/坏卡当没有."""
    if root is None:
        return []
    data = SC.load(root)
    if not isinstance(data, dict):
        return []
    auto = data.get("auto")
    items = auto.get("开放问题") if isinstance(auto, dict) else None
    return [t for t in (_text_of(x) for x in items or []) if t] if isinstance(items, list) else []


# ------------------------------------------------------------------ 拼句子 (纯函数)
def build_lines(day, facts, turns, absent_info, open_questions) -> list[str]:
    """3-5 句第一人称陈述, 每句 <= 40 字, 每句都有数据依据 (没数据就不提).
    day 只为签名一致 (句子里不写日期, 避免和"今天"重复)."""
    keep, seen = [], set()
    for text in facts or []:
        if isinstance(text, str) and text.strip() and text not in seen:
            seen.add(text)
            keep.append(text)
    facts = keep
    try:
        turns = max(0, int(turns or 0))
    except (TypeError, ValueError):
        turns = 0
    info = absent_info if isinstance(absent_info, dict) else {}
    questions = [t for t in (open_questions or []) if isinstance(t, str) and t.strip()]

    lines: list[str] = []
    k = len(facts)
    if k:
        lines.append(_line(f"今天记住了{k}件关于你的事，比如：", facts[0]))  # 例子太长就截短
    else:
        lines.append("今天没记住新的事，把之前的事又过了一遍。")

    if turns > 0:
        lines.append(f"今天你跟我说了{turns}轮话。")

    try:
        beats = max(0, int(info.get("beats") or 0))
    except (TypeError, ValueError):
        beats = 0
    minutes = info.get("minutes")
    if isinstance(minutes, int) and minutes > 0:
        lines.append(f"今天有{minutes}分钟你不在，我自己待着。")
    elif beats > 0:
        lines.append(f"今天你不在的{beats}拍，我自己待着。")

    if questions:
        lines.append(_line("有件事我还没想明白：", questions[-1]))

    for filler in FILLERS:  # 数据少时凑到最少 3 句; 凑的句子不带任何数字
        if len(lines) >= MIN_LINES:
            break
        if filler not in lines:
            lines.append(filler)
    return [_clip(line) for line in lines[:MAX_LINES]]


def _near_date(day_dt: datetime, month: int, mday: int):
    """在前后一年里找离 day_dt 最近的那个 M月D日 (跨年: 12-31 看 1-1 是 +1 天)."""
    best = None
    for year in (day_dt.year - 1, day_dt.year, day_dt.year + 1):
        try:
            cand = datetime(year, month, mday)
        except ValueError:  # 2月30日之类
            continue
        diff = abs((cand - day_dt).days)
        if best is None or diff < best[0]:
            best = (diff, cand)
    return best


def build_question(day, facts, open_questions) -> str | None:
    """规则优先级: ① 1-3 天内的日期事实 -> 提醒; ② 最新开放问题 -> 问; ③ None."""
    facts = [t for t in (facts or []) if isinstance(t, str) and t.strip()]
    questions = [t for t in (open_questions or []) if isinstance(t, str) and t.strip()]
    try:
        day_dt = datetime.strptime(str(day), "%Y-%m-%d")
    except (TypeError, ValueError):
        day_dt = None
    if day_dt is not None:
        for fact in facts:
            for hit in _MD_DATE.finditer(fact):
                got = _near_date(day_dt, int(hit.group(1)), int(hit.group(2)))
                if got is not None and 1 <= got[0] <= 3:
                    return f"「{fact}」的事，要我提醒你吗？"
    if questions:
        q = questions[-1].strip().rstrip("。．.!！?？,，;；:：、 ")
        if q:
            return f"你还没告诉我：{q}，现在想说吗？"
    return None


# ------------------------------------------------------------------ 汇总 (只算)
def report(day, *, logs_dir, vault, char, beats_dir=None) -> dict:
    """把 4 路输入拼成一份 report; 任何一路没有都不影响别的, 不写文件."""
    day = str(day)
    facts = today_facts(load_digest(logs_dir, day))
    root = _root_of(vault, char)
    log_file = root / "日志" / f"{day}.md" if root is not None and _stamp(day) else None
    turns = count_turns(_read_text(log_file)) if log_file is not None else 0
    absent_info = absent(_load_beats(day, beats_dir))
    open_questions = _open_questions(root)
    return {
        "day": day,
        "lines": build_lines(day, facts, turns, absent_info, open_questions),
        "question": build_question(day, facts, open_questions),
        "counts": {"facts": len(facts), "turns": turns, "absent_beats": absent_info["beats"]},
    }


# ------------------------------------------------------------------ 输入健康
def health_of(day, rep, *, logs_dir=None, cfg=None) -> dict:
    """今天"她到底有没有东西可记"的一张体检表 (只算, 不写).
    为什么要它: 11-19 那天夜间报告写了"今天没记住新的事", 可真正的原因是一天里桌宠日志 0 轮、
    digest 0 条候选 —— 这是进料口的问题, 不是她懒得记. 体检表让这个区别一眼看得出来."""
    day = str(day)
    counts = (rep or {}).get("counts") or {}
    health = {"desktop_turns": int(counts.get("turns") or 0),
              "digest_facts": int(counts.get("facts") or 0),
              "digest_records": 0, "qq_owner_lines": 0, "qq_candidates": 0, "qq_would_write": 0,
              "qq_on": False, "reasons": []}
    if logs_dir is not None:
        try:
            health["digest_records"] = len(load_digest(logs_dir, day))
        except Exception:  # noqa: BLE001  读崩了不影响别的
            health["digest_records"] = 0
    try:
        from . import owner_qq as Q
        health["qq_on"] = bool(Q.enabled(cfg))
        root = Q.channel_root(cfg)
        if root is not None:
            rows = Q.entries_for(root, day)
            health["qq_owner_lines"] = len(rows)
            cands = Q.candidates_qq(rows)
            health["qq_candidates"] = len(cands)
            try:
                ok, _bad = Q.select_qq(cands, day, "")
                health["qq_would_write"] = len(ok)
            except Exception:  # noqa: BLE001
                health["qq_would_write"] = 0
    except Exception:  # noqa: BLE001  渠道日志读不了就按没有
        pass
    if health["desktop_turns"] == 0 and health["qq_owner_lines"] == 0:
        health["reasons"].append("今天桌宠和私聊都没有你说的话")
    elif health["digest_facts"] == 0 and health["qq_would_write"] == 0:
        health["reasons"].append(f"今天有你说的话（私聊 {health['qq_owner_lines']} 句），但没有够格当候选的事")
    return health


# ------------------------------------------------------------------ 开关
def enabled(cfg: dict | None = None) -> bool:
    """WM_NIGHT_REPORT=on 优先; 否则 config memory.night.enabled (缺省 False)."""
    env = str(os.environ.get(ENV_ON) or "").strip().lower()
    if env:
        return env in ON_VALUES
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:  # noqa: BLE001  配置坏了也不该让夜间整理炸掉
            cfg = {}
    raw = ((cfg or {}).get("memory") or {}).get("night")
    if isinstance(raw, dict):
        value = raw.get("enabled")
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ON_VALUES
    return False


# ------------------------------------------------------------------ 落盘
def _change_lines(lines) -> list[str]:
    """lines 里算"最近变化"的那几句 (记住 / 聊了 / 待着)."""
    return [line for line in lines or [] if isinstance(line, str) and any(m in line for m in CHANGE_MARKS)]


def _section(day: str, lines: list[str], question: str) -> str:
    body = [f"## {day}", ""]
    body += [f"- {line}" for line in lines]
    if question:
        body.append(f"- 想问：{question}")
    return "\n".join(body) + "\n\n"


def _upsert_section(old: str, day: str, block: str) -> str:
    """把 "## day" 这一段换成 block; 找不到就追加. 其它日期一个字节都不动."""
    text = old if isinstance(old, str) else ""
    if not text.strip():
        text = PAGE_HEAD
    if not text.endswith("\n"):
        text += "\n"
    pat = re.compile(rf"^##\s+{re.escape(day)}\s*$", re.M)
    hit = pat.search(text)
    if hit:
        nxt = _SECTION_HDR.search(text, hit.end())
        end = nxt.start() if nxt else len(text)
        return text[: hit.start()] + block + text[end:]
    if not text.endswith("\n\n"):
        text += "\n"
    return text + block


def write(vault, char, rep, *, cfg: dict | None = None) -> dict:
    """开关开着才写: 自我卡最近变化 + 开放问题, 再更新 今天的我.md 的当天段.
    关着直接 {"ok": False, "why": "off"}, 不建目录不写文件."""
    if not enabled(cfg):
        return {"ok": False, "why": "off"}
    if not isinstance(rep, dict):
        return {"ok": False, "why": "bad_report"}
    root = _root_of(vault, char)
    if root is None:
        return {"ok": False, "why": "no_vault"}
    day = str(rep.get("day") or "").strip()
    if _stamp(day) is None:
        return {"ok": False, "why": "bad_day"}

    lines = [line for line in (rep.get("lines") or []) if isinstance(line, str) and line.strip()]
    question = rep.get("question")
    question = question.strip() if isinstance(question, str) else ""

    changed, errors = 0, 0
    for line in _change_lines(lines):
        try:
            SC.append(root, "最近变化", line)  # 自我卡不在就自己 ensure; 坏了会抛, 别拖垮页面
            changed += 1
        except SC.SelfCardError:
            errors += 1
    if question:
        try:
            SC.append(root, "开放问题", question)
        except SC.SelfCardError:
            errors += 1

    page = root / "记忆" / PAGE_NAME
    old = ""
    if page.exists():  # 有文件却读不出来: 不覆盖, 交给人修
        try:
            old = page.read_text(encoding="utf-8-sig")
        except (OSError, ValueError):
            return {"ok": False, "why": "page_unreadable", "page": str(page)}
    new = _upsert_section(old, day, _section(day, lines, question))
    page.parent.mkdir(parents=True, exist_ok=True)
    tmp = page.with_name(f".{PAGE_NAME}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:  # 新文件一律 LF
        fh.write(new)
    os.replace(tmp, page)
    return {"ok": True, "day": day, "page": str(page), "changes": changed,
            "question": bool(question), "self_card_errors": errors}


# ------------------------------------------------------------------ CLI
def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="夜间巩固 v1: 把今天整理成几句话, 写进自我卡和记忆库")
    ap.add_argument("--day", help="哪一天, 形如 2026-11-19 (缺省今天)")
    ap.add_argument("--dry-run", action="store_true", help="只打印 report, 不写任何文件")
    ap.add_argument("--set", action="append", default=[], help="配置覆盖, 如 memory.vault=D:/vault")
    args = ap.parse_args(argv)
    cfg = load_config(None, args.set)
    day = args.day or time.strftime("%Y-%m-%d")
    mem = cfg.get("memory") or {}
    vault, char = mem.get("vault") or "", mem.get("character") or ""
    rep = report(day, logs_dir=ROOT / "logs", vault=vault, char=char)
    rep["health"] = health_of(day, rep, logs_dir=ROOT / "logs", cfg=cfg)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if args.dry_run:
        return 0
    got = write(vault, char, rep, cfg=cfg)
    print(json.dumps(got, ensure_ascii=False, indent=1))
    # 主人私聊里的原话 -> 候选 (A 档: 只进候选, 等他确认). 开关关着就一句都不读
    captured = _capture_owner_qq(cfg, day, dry_run=args.dry_run)
    if captured is not None:
        print(json.dumps({"owner_qq": captured}, ensure_ascii=False, indent=1))
    return 0 if got.get("ok") else 1


def _capture_owner_qq(cfg: dict, day: str, *, dry_run: bool = False):
    """夜间顺手把主人私聊里的原话过一遍 (wm/owner_qq.py); 关着 / 出错都返回 None, 不影响夜间报告."""
    try:
        from . import owner_qq as Q
        if not Q.enabled(cfg):
            return None
        from .memory import Memory
        obj = Memory(cfg.get("memory") or {})
        obj.init()
        return asyncio.run(Q.capture(obj, cfg, day, dry_run=dry_run))
    except Exception:  # noqa: BLE001  私聊日志读不了 / 记忆库坏: 夜间报告照常
        log.warning("夜间顺手整理私聊候选失败, 这一项跳过", exc_info=True)
        return None


if __name__ == "__main__":
    raise SystemExit(_main())

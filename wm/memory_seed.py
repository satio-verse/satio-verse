"""出生种子 (记忆按脑区重设计阶段 3, 2026-11-02 用户"按草稿做"): 她"生下来"就有的——对自己的一点认识、性子、情绪的底色、
天生的习惯和本能. 清单和规矩见知识库 [[出生种子清单草稿（2026-11-02）]]; 数据在 wm/seeds/<角色>/<版本>/seed.json.

规矩 (check 查, tests/test_memory_seed.py 守着):
  - 不放任何经历 (婴儿没有回忆; 人设写着"不主动虚构过去经历"): 正文里不许有"我小时候""那天我"这类;
  - 不放关于主人的事 (只能等他说, 先进候选): 不许有"主人叫""用户喜欢"这类;
  - 不放小窝里东西在哪 (认知和真值分开): 不许有"放在""在书架上"这类; 种子只看人设和代码里的常量, 不看世界 (双胞胎测试);
  - 已有的行为只是记成"出厂"、能看能停, 值从原处读 (小窝需求从 desktop/web/room-needs.js 读), 不在种子里另存一份.
谁说了算: 面板人设 > 主人明说的 > 她学到的 > 种子. 开关 memory.seed (on / off); 单条停用 memory.seed_off: ["seed:habit:2"].
只给新用户的 (new_user_only) 已经见过主人就不用 (状态 not_needed).
这一版用到行为上的只有"早上第一次见到主人打个招呼" (wm/memory.py 的 relation_line); 心情基线、作息先给记忆页、记忆核心看,
等阶段 4 (情绪) 和小屋页面来读; 用了、成了加强, 被纠正减弱, 最低休眠, 是阶段 5、7 的事.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import ROOT

SEEDS_DIR = Path(__file__).resolve().parent / "seeds"
PACKS = {"绫音": "ayane"}  # 角色名 -> 种子包的文件夹 (文件夹名用英文, 安装包、压缩工具都不挑)
TYPES = ("self", "trait", "affect", "procedure")
KINDS = ("自我", "性子", "情绪", "习惯", "本能")
NOW = ("已有", "已有一部分", "人设", "新")
STATUS = {"active": "在用", "off": "你停用了", "not_needed": "用不上 (已经见过主人)"}
_ID = re.compile(r"^seed:(self|trait|affect|habit|instinct):\d+$")
_EPISODE = re.compile(r"我小时候|小时候我|我以前|以前我|我那天|那天我|我昨天|昨天我|我去年|去年我|那次我|我那次")
_OWNER = re.compile(r"(?:主人|用户)(?:叫|是谁|喜欢|爱吃|住在|的名字|的生日|的工作)")
_TRUTH = re.compile(r"(?:放|摆|藏|收)在|在(?:桌|柜|床|书架|抽屉|厨房|卧室|客厅|阳台|窗台)")
_NEEDS_JS = ROOT / "desktop" / "web" / "room-needs.js"
_cache: dict[Path, tuple[int, dict]] = {}


def pack_file(char: str, version: str = "v1") -> Path | None:
    name = PACKS.get(char)
    return SEEDS_DIR / name / version / "seed.json" if name else None


def load(char: str = "绫音", version: str = "v1") -> dict | None:
    """这个角色的种子包 (改过才重读); 没有这个角色的包、读不出来都是 None (没有种子, 别的照常)."""
    path = pack_file(char, version)
    try:
        mtime = path.stat().st_mtime_ns if path else None
    except OSError:
        return None
    if path is None:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        pack = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    _cache[path] = (mtime, pack)
    return pack


def check(pack: dict) -> list[str]:
    """种子包哪里不合规矩 (空 = 没问题)."""
    bad, seen = [], set()
    for s in pack.get("seeds") or []:
        sid, text = str(s.get("id") or ""), str(s.get("text") or "")
        if not _ID.match(sid) or sid in seen:
            bad.append(f"{sid or '?'}: 编号不对或重复")
        seen.add(sid)
        if s.get("type") not in TYPES or s.get("kind") not in KINDS or s.get("now") not in NOW:
            bad.append(f"{sid}: type / kind / now 不在允许的里面")
        if not text or len(text) > 120:
            bad.append(f"{sid}: 正文空着或太长")
        for name, pat in (("经历", _EPISODE), ("关于主人的事", _OWNER), ("小窝里东西在哪", _TRUTH)):
            if pat.search(text):
                bad.append(f"{sid}: 种子里不放{name}: {text}")
    return bad


def room_needs() -> dict:
    """小窝需求的初值和每小时的变化, 从页面的 room-needs.js 里读 (那边是准的; 读不到就空)."""
    try:
        js = _NEEDS_JS.read_text(encoding="utf-8")
    except OSError:
        return {}

    def obj(pattern: str) -> dict:
        m = re.search(pattern + r"\s*=\s*\{([^{}]*)\}", js)
        return {k: float(v) if "." in v else int(v) for k, v in re.findall(r"(\w+)\s*:\s*(-?\d+(?:\.\d+)?)", m.group(1))} if m else {}

    out = {"init": obj(r"this\.v"), "decay_per_h": obj(r"const DECAY")}
    restore = re.search(r"const RESTORE\s*=\s*(\d+(?:\.\d+)?)", js)
    if restore:
        out["restore_per_h"] = float(restore.group(1)) if "." in restore.group(1) else int(restore.group(1))
    return {k: v for k, v in out.items() if v}


def view(char: str = "绫音", mode: str = "on", off=(), met: bool = False, version: str = "v1") -> list[dict]:
    """记忆页、记忆核心看的种子: 每条带状态 (在用 / 你停用了 / 用不上). mode 是 off 时一条都没有."""
    pack = load(char, version) if mode != "off" else None
    if not pack:
        return []
    off, needs, out = set(off or ()), None, []
    for s in pack.get("seeds") or []:
        item = {k: s[k] for k in ("id", "kind", "type", "text", "now", "from", "trigger", "note") if s.get(k)}
        item["pack"] = pack.get("pack") or f"seed/{char}/{version}"
        values = dict(s.get("values") or {})
        if s.get("read") in ("room_needs_init", "room_needs_rate"):
            needs = room_needs() if needs is None else needs
            values.update({"init": needs.get("init")} if s["read"] == "room_needs_init" else
                          {k: needs[k] for k in ("decay_per_h", "restore_per_h") if k in needs})
        if values:
            item["values"] = values
        item["status"] = "off" if s["id"] in off else "not_needed" if s.get("new_user_only") and met else "active"
        out.append(item)
    return out


def active(char: str, sid: str, mode: str = "on", off=()) -> dict | None:
    """这条种子在用就返回它 (带 values), 不在用 / 没有就 None."""
    if mode == "off" or sid in set(off or ()):
        return None
    pack = load(char)
    return next((s for s in (pack or {}).get("seeds") or [] if s.get("id") == sid), None)

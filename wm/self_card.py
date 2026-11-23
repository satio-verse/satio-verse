"""自我卡 (A1, 2026-11-19): 她"记得的自己", 每轮带一小段进提示词.

为什么要有: 记忆是检索层 (说什么话时捞几条), 不是自传体自我. 她今天不会自然答出"昨天我做了什么、
今天想做什么、答应过谁什么". 自我卡就是这几样的固定存放处, 每轮注入精简版, 不加模型调用.

两段, 谁改谁:
  core  身份 / 价值观 / 边界 / 承诺  —— 只有用户能改 (set_core(by="user")); 她写就报错.
  auto  最近变化 / 开放问题 / 当前项目 —— 只有她能改 (append(by="self")); 夜里整理 (wm/digest.py) 往里写.

不做什么 (边界):
  - 不存经历流水 (那是 日志/ 和 日记/); 自我卡只放"她认为的自己", 一条一行;
  - 不存关于主人的事实 (那是 记忆/关于用户.md), 也不存小窝里东西在哪 (认知和真值分开);
  - 坏文件不静默重建: load/render 返回 None, append 抛 SelfCardError, 原文件不动, 等人来修.

开关: config `memory.self_card` = {"mode": "off"|"shadow"|"on", "max_chars": 700} 或环境变量
WM_SELF_CARD=off|shadow|on (环境变量优先, 测试用). 缺省 off: 关着 text_for() 返回 None, 提示词一个字不变.
shadow: 照常算, 不注入, 日志里记一行, 用来先看"注入了会是什么样".

文件: <记忆库>/自我/self.json (和 记忆/、日记/ 同级). 写盘先写临时文件再 replace, 半个文件不会留在原地.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

log = logging.getLogger("wm.self_card")

DIR = "自我"
FILE = "self.json"
MODE_OFF, MODE_SHADOW, MODE_ON = "off", "shadow", "on"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)
CORE_FIELDS = ("身份", "价值观", "边界", "承诺")
AUTO_FIELDS = ("当前项目", "最近变化", "开放问题")
AUTO_KEEP = {"当前项目": 6, "最近变化": 6, "开放问题": 4}
MAX_CHARS = 700
LINE_MAX = 120
_ENV_MODE = "WM_SELF_CARD"
_cache: dict[Path, tuple[int, dict]] = {}


class SelfCardError(RuntimeError):
    """自我卡写不进去 (坏文件 / 段名不对 / 没权限改这一段)."""


# ------------------------------------------------------------------ 路径与读写
def path_of(root) -> Path:
    """root 是记忆库目录 (Memory.dir, 比如 .../绫音); 自我卡在 <root>/自我/self.json."""
    return Path(root) / DIR / FILE


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime())


def _skeleton(now: str | None = None) -> dict:
    return {"version": 1, "updated": now or _now_str(), "role": "self_card",
            "core": {"身份": "", "价值观": [], "边界": [], "承诺": []},
            "auto": {k: [] for k in AUTO_FIELDS},
            "core_history": []}


def _clean_text(text) -> str:
    """一条一行: 去换行、压空白、截长."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:LINE_MAX]


def _norm(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?~～；;：:]+", "", text or "")


def _valid(data) -> bool:
    if not isinstance(data, dict):
        return False
    core, auto = data.get("core"), data.get("auto")
    if not isinstance(core, dict) or not isinstance(auto, dict):
        return False
    return all(isinstance(core.get(k), (str, list)) for k in CORE_FIELDS) and all(
        isinstance(auto.get(k), list) for k in AUTO_FIELDS)


def load(root) -> dict | None:
    """读自我卡; 没有、读不出来、结构不对都是 None (不抛, 不改文件). 改过才重读."""
    path = path_of(root)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _cache.pop(path, None)
        return None
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("自我卡读不出来: %s", path)
        return None
    if not _valid(data):
        log.warning("自我卡结构不对: %s", path)
        return None
    _cache[path] = (mtime, data)
    return data


def _write(root, data: dict, *, bump: bool = True) -> dict:
    """先写临时文件再 replace: 崩在中间也不会留下半个 self.json."""
    data["updated"] = _now_str()
    if bump:
        data["version"] = int(data.get("version") or 0) + 1
    path = path_of(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{FILE}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    _cache.pop(path, None)
    return data


def ensure(root, *, now: str | None = None) -> dict:
    """没有就建一份空的; 有就原样返回 (不动内容). 文件在、但读不出来/结构不对 -> 报错, **不覆盖**."""
    data = load(root)
    if data is not None:
        return data
    path = path_of(root)
    if path.exists():  # 有文件却 load 不出来: 坏文件, 留给用户修, 不许静默重建把他写的盖掉
        raise SelfCardError(f"自我卡文件在, 但读不出来: {path}")
    return _write(root, _skeleton(now), bump=False)


# ------------------------------------------------------------------ 写 (两段两种权限)
def append(root, section: str, text: str, *, at: str | None = None, by: str = "self") -> dict:
    """她能写的那几段: 追加一条, 同一段里内容重复的不加 (返回原数据). 段名不在 AUTO_FIELDS 就报错."""
    if by != "self":
        raise SelfCardError("auto 段只有她自己能写")
    if section not in AUTO_FIELDS:
        raise SelfCardError(f"这段她不能写: {section!r}")
    line = _clean_text(text)
    if not line:
        raise SelfCardError("空的不写")
    data = load(root) or ensure(root)
    items = data["auto"][section]
    if any(_norm(x.get("text", "")) == _norm(line) for x in items if isinstance(x, dict)):
        return data
    items.append({"t": at or _now_str(), "text": line})
    cap = AUTO_KEEP.get(section, 6)
    if len(items) > cap:
        del items[: len(items) - cap]
    return _write(root, data)


def set_core(root, field: str, value, *, by: str = "user") -> dict:
    """只有用户能改的那几段: 身份 (str), 价值观 / 边界 / 承诺 (list[str]). 每次改动记进 core_history."""
    if by != "user":
        raise SelfCardError("core 段只有用户能改")
    if field not in CORE_FIELDS:
        raise SelfCardError(f"没有这一段: {field!r}")
    data = load(root) or ensure(root)
    if field == "身份":
        new = _clean_text(value)
    else:
        seq = value if isinstance(value, (list, tuple)) else [value]
        new = [x for x in (_clean_text(v) for v in seq) if x]
    old = data["core"].get(field)
    if old == new:
        return data
    data["core"][field] = new
    hist = data.setdefault("core_history", [])
    hist.append({"t": _now_str(), "field": field, "old": old, "new": new})
    del hist[:-50]
    return _write(root, data)


# ------------------------------------------------------------------ 读成一段话
def _bullets(values, *, limit: int, sep: str = "；") -> str:
    got = [str(x).strip() for x in values if str(x).strip()][:limit]
    return sep.join(got)


def render(root, *, max_chars: int = MAX_CHARS, day: str | None = None) -> str | None:
    """精简版: 一段话, 供每轮注入. 没有卡、坏文件都是 None. 超长先丢最早的 auto 条目."""
    data = load(root)
    if data is None:
        return None
    core, auto = data["core"], data["auto"]
    day = day or time.strftime("%Y-%m-%d", time.localtime())
    head = f"[SELF] 我记得的自己（{day}）："
    fixed = []
    if str(core.get("身份") or "").strip():
        fixed.append(f"我是谁：{_clean_text(core['身份'])}")
    if core.get("价值观"):
        fixed.append(f"我在意：{_bullets(core['价值观'], limit=5)}")
    if core.get("边界"):
        fixed.append(f"我不做：{_bullets(core['边界'], limit=5)}")
    if core.get("承诺"):
        fixed.append(f"我答应过：{_bullets(core['承诺'], limit=5)}")

    def auto_lines(keep: dict) -> list[str]:
        out = []
        for field in AUTO_FIELDS:
            items = auto.get(field) or []
            if not isinstance(items, list) or not items:
                continue
            texts = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                t, x = str(it.get("t") or ""), _clean_text(it.get("text"))
                if not x:
                    continue
                texts.append(f"{x}（{t[:10]}）" if t else x)
            out.append(f"{field}：" + _bullets(texts, limit=keep.get(field, 2), sep="；"))
        return out

    def pack(lines: list[str]) -> str:
        body = head + "\n" + "\n".join(lines)
        return body.strip()

    keep = {k: 2 for k in AUTO_FIELDS}
    lines = fixed + auto_lines(keep)
    if not lines:  # 空卡 (刚建、还什么都没写) 不注入一句话的空壳
        return None
    while len(pack(lines)) > max_chars:
        # 先丢最早的 auto 条目; auto 丢完了还超, 就截短整段
        grew = False
        for field in AUTO_FIELDS:
            if keep[field] > 1:
                keep[field] -= 1
                grew = True
                break
        if not grew:
            text = pack(lines)
            return text[: max(0, max_chars - 1)] + "…"
        lines = fixed + auto_lines(keep)
    return pack(lines) or None


def status(root) -> dict:
    """给记忆页/面板看: 有没有、几段、几条、上次更新."""
    data = load(root)
    path = path_of(root)
    if data is None:
        return {"path": str(path), "ok": False, "exists": path.exists(), "updated": None,
                "core": 0, "auto": {k: 0 for k in AUTO_FIELDS}}
    core = sum(1 for k in CORE_FIELDS if data["core"].get(k))
    return {"path": str(path), "ok": True, "exists": True, "updated": data.get("updated"),
            "version": data.get("version"), "core": core,
            "auto": {k: len(data["auto"].get(k) or []) for k in AUTO_FIELDS}}


# ------------------------------------------------------------------ 开关与入口
def mode_of(cfg: dict | None) -> str:
    """开关: 环境变量优先 (测试/临时试跑用), 其次 config 里 memory.self_card.mode, 缺省 off."""
    env = str(os.environ.get(_ENV_MODE) or "").strip().lower()
    if env in MODES:
        return env
    raw = ((cfg or {}).get("memory") or {}).get("self_card")
    if isinstance(raw, dict):
        val = str(raw.get("mode") or "").strip().lower()
        if val in MODES:
            return val
    return MODE_OFF


def max_chars_of(cfg: dict | None) -> int:
    raw = ((cfg or {}).get("memory") or {}).get("self_card")
    if isinstance(raw, dict):
        try:
            return max(200, min(2000, int(raw.get("max_chars") or MAX_CHARS)))
        except (TypeError, ValueError):
            pass
    return MAX_CHARS


def text_for(cfg: dict | None, memory) -> str | None:
    """pipeline 每轮调: 开着就返回要注入的一段, 关着/shadow 返回 None. 任何异常都不该让这一轮挂."""
    mode = mode_of(cfg)
    if mode == MODE_OFF:
        return None
    try:
        root = getattr(memory, "dir", None)
        if root is None:
            return None
        text = render(root, max_chars=max_chars_of(cfg))
        if mode == MODE_SHADOW:
            log.info("自我卡 shadow: %s", (text or "(空)").replace("\n", " / ")[:200])
            return None
        return text
    except Exception as exc:  # noqa: BLE001  自我卡出问题不许拖垮对话
        log.warning("自我卡没取出来: %s", exc)
        return None

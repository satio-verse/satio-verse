"""账本: 她用云端模型想事情、说话花了多少钱, 缓存命中多少, 现在是不是高峰, DeepSeek 余额还剩多少 (2026-10-26 用户要的
"账本、DeepSeek 余额、峰谷"; 设计见 docs/roadmap/own-harness.md 第 3 节).

- 记账: 每次云端请求一行 (CloudLLM._request 结束时), 本机模型 (llama.cpp) 也记一行, 看 KV cache 复用了多少 (不花钱).
  按天写在 <trace.log_dir>/billing/<日期>.jsonl: 时间、谁花的 (桌宠 / 外部渠道 / 工作台)、提供方、模型、命中 / 没命中 / 输出 /
  思考 token、高峰还是空闲、花了多少、命中缓存省了多少. 测试服务带 --set trace.log_dir=临时目录, 不会写进真账本.
- 价格和峰谷都在配置 billing 段, 不写死 (价格会变). 价格表写的是高峰价, 空闲时段乘 off_ratio. 对不上价格表的模型只记 token.
  DeepSeek 2026-10-26 官方价格页: 北京时间周一至周五 (不含法定节假日) 9:00-12:00、14:00-18:00 高峰, 其余空闲, 空闲五折.
- 余额: 只问 DeepSeek (GET /user/balance, 不花钱). key 不进账本: 用正在用的 CloudLLM 手里的 key (它的地址和余额地址同一个
  主机才问, 走中转站的 key 不发给 DeepSeek), 没有就用环境变量 (billing.env_key, 只发给官方地址或本机). 每天记第一次和最近一次
  看到的余额 (balance.json), 余额往上涨算充值 / 赠金, 不算花掉的.
- 给她看的一行 (wallet_line): 余额、今天花了多少、缓存命中、现在是不是高峰. 桌宠用云端模型时每轮带上, QQ 只在主人档带
  (群里不说主人的钱). 怎么用这些 (比如不急的事放到空闲时段做) 由她自己定.
- 面板"账号与余额"页读 /api/billing (summary).
- 按渠道的额度 (2026-10-27 用户: "per-channel token budgets (QQ / B站 / desk pet / other, warn at 80%, stop at 100%)"):
  每次云端请求记在哪个渠道 (ch): 入口用 charging / charged 标 (contextvars, 里面起的任务也算), 没标的桌宠算桌宠、别的算其他.
  每个渠道和总额一个每天的上限 (单位 token 或元, 0 = 不限; 面板改, 存在账本目录的 budget.json), 按这台电脑的日期算 (和账本的
  "今天"一样). 云端请求发出去之前查一次 (CloudLLM._request): 到 100% 就不发 (CloudError kind=budget); 到 80% 和 100% 每天各提醒
  一次 (on_alert, 服务端发到桌宠的对话栏), 她的钱包那行也带上哪些渠道快用完了. 本机模型不花钱, 不算.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import time
import weakref
from collections import deque
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger("wm.ledger")

BJ = timezone(timedelta(hours=8))  # 峰谷按北京时间算, 和这台电脑的时区无关
WHO = {"pet": "桌宠", "channels": "外部渠道", "workbench": "她的研究 (dsh)", "agent": "工作台"}
TIERS = {"peak": "高峰时段", "off": "空闲时段"}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_KEEP = 60  # 汇总里带最近几条
BUCKETS = {"qq": "QQ", "bili": "B站", "pet": "桌宠", "other": "其他"}  # 按渠道的额度
LIMITS = {**BUCKETS, "total": "总额"}
UNITS = {"tokens": "token", "yuan": "元"}
_BUDGET_FILE = "budget.json"
_charge: ContextVar[str | None] = ContextVar("wm_ledger_charge", default=None)
_proactive: ContextVar[bool] = ContextVar("wm_ledger_proactive", default=False)  # 她主动开口的一轮 (ADR-0026)


def bucket(who: str, charge: str | None = None) -> str:
    """一次请求算哪个渠道的额度: 入口标过的 (charging) 为准; 没标的桌宠算桌宠, 别的 (工作台、她的研究、外部渠道里没标的) 算其他."""
    charge = _charge.get() if charge is None else charge
    if charge in BUCKETS:
        return charge
    return "pet" if who == "pet" else "other"


@contextlib.contextmanager
def charging(name: str):
    """with charging("qq"): 里面 (连同里面起的任务) 的云端请求都算 QQ 的额度."""
    tok = _charge.set(name)
    try:
        yield
    finally:
        _charge.reset(tok)


def charged(name: str):
    """协程方法的装饰器版 charging."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrap(*args, **kwargs):
            with charging(name):
                return await fn(*args, **kwargs)
        return wrap
    return deco


@contextlib.contextmanager
def proactive(on: bool = True):
    """with proactive(True): 里面 (连同里面起的任务) 的请求算"她主动开口的一轮":
    钱和 token 照记 (额度不能被主动说话偷掉), 轮数不记 (ADR-0026: 这不算聊过一轮)."""
    tok = _proactive.set(bool(on))
    try:
        yield
    finally:
        _proactive.reset(tok)


def clean_budget(new: dict, base: dict) -> dict:
    """面板交上来的额度设置 {unit, limits: {渠道: 上限}} (0 = 不限). 不对就抛 ValueError (原因给人看)."""
    if not isinstance(new, dict):
        raise ValueError("要一个对象")
    unit = new.get("unit", base["unit"])
    if unit not in UNITS:
        raise ValueError("单位只能是 tokens 或 yuan")
    limits = dict(base["limits"])
    for k, v in (new.get("limits") or {}).items():
        if k not in LIMITS:
            raise ValueError(f"没有 {k} 这个渠道")
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{LIMITS[k]}的上限要是数字") from None
        if not 0 <= v <= 1e12:
            raise ValueError(f"{LIMITS[k]}的上限不能是负数")
        limits[k] = int(v) if unit == "tokens" else round(v, 4)
    return {"unit": unit, "warn": base["warn"], "limits": limits}


def _budget_base(conf: dict) -> dict:
    b = conf.get("budget") or {}
    base = {"unit": "tokens", "warn": min(0.99, max(0.1, float(b.get("warn", 0.8)))), "limits": {k: 0 for k in LIMITS}}
    try:
        return clean_budget({"unit": b.get("unit", "tokens"), "limits": b.get("limits") or {}}, base)
    except ValueError as exc:
        log.warning("配置 billing.budget 不对 (%s), 先不限", exc)
        return base


def normalize(usage: dict | None) -> dict:
    """各家的用量字段 -> {hit, miss, out, think}. DeepSeek: prompt_cache_hit/miss_tokens; OpenAI 格式: prompt_tokens_details 里的
    cached_tokens (CloudLLM._usage 已经并成 prompt_cache_hit_tokens); llama.cpp 的 timings: cache_n (复用的 KV)、prompt_n、predicted_n;
    已经是 hit/miss/out 的原样用. 思考 token 已经算在输出里 (completion_tokens 包含它), think 只是给人看的."""
    u = usage if isinstance(usage, dict) else {}

    def num(*keys):
        for k in keys:
            v = u.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return max(0, int(v))
        return None

    hit = num("hit", "prompt_cache_hit_tokens", "cache_n")
    miss = num("miss", "prompt_cache_miss_tokens", "prompt_n")
    if miss is None:  # 只报了输入总数: 减掉命中的就是没命中的
        miss = max(0, (num("prompt_tokens") or 0) - (hit or 0))
    return {"hit": hit or 0, "miss": miss, "out": num("out", "completion_tokens", "predicted_n") or 0,
            "think": num("think", "reasoning_tokens") or 0}


def _minutes(hhmm: str) -> int:
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


class PeakClock:
    """一家的峰谷时段. conf: {weekdays: [["09:00", "12:00"], …] (周一到周五的高峰), off_ratio, holidays: ["2027-01-01", …]}.
    周末和 holidays 里的日子全天空闲 (DeepSeek: "其余时段均为空闲时段, 包括周末, 以及中国法定节假日全天")."""

    def __init__(self, conf: dict):
        self.spans = sorted((_minutes(a), _minutes(b)) for a, b in conf.get("weekdays", []))
        self.ratio = float(conf.get("off_ratio", 1.0))
        self.holidays = {str(d) for d in conf.get("holidays", [])}

    def _busy_day(self, d: datetime) -> bool:
        return d.weekday() < 5 and d.date().isoformat() not in self.holidays

    def tier(self, t: float) -> str:
        d = datetime.fromtimestamp(t, BJ)
        m = d.hour * 60 + d.minute
        return "peak" if self._busy_day(d) and any(a <= m < b for a, b in self.spans) else "off"

    def next_change(self, t: float) -> tuple[float, str] | None:
        """下一次换档的时间和换成哪一档 (最多往后看 20 天, 长假也够)."""
        now = self.tier(t)
        day0 = datetime.fromtimestamp(t, BJ).replace(hour=0, minute=0, second=0, microsecond=0)
        for k in range(20):
            base = day0 + timedelta(days=k)
            for a, b in self.spans:
                for m in (a, b):
                    c = (base + timedelta(minutes=m)).timestamp()
                    if c > t and self.tier(c) != now:
                        return c, self.tier(c)
        return None

    def segments(self, t: float) -> list[list]:
        """北京时间这一天的高峰段 [[开始分钟, 结束分钟], …] (画时间条用; 周末、节假日是空的)."""
        return [[a, b] for a, b in self.spans] if self._busy_day(datetime.fromtimestamp(t, BJ)) else []


def _empty() -> dict:
    return {"turns": 0, "cost": 0.0, "saved": 0.0, "hit": 0, "miss": 0, "out": 0, "think": 0, "unpriced": 0,
            "peak_turns": 0, "by_who": {}, "by_model": {}, "by_ch": {}, "local": {"turns": 0, "hit": 0, "miss": 0, "out": 0}}


def _add(agg: dict, r: dict) -> None:
    if r.get("fee"):  # 按次收钱的工具 (DeepSeek 官方搜索): 只加钱, 没有 token, 不算一轮; 按模型那栏记次数
        cost = float(r.get("cost") or 0.0)
        agg["cost"] += cost
        for part, name in (("by_who", str(r.get("who") or "?")), ("by_ch", r.get("ch") or bucket(str(r.get("who") or ""), ""))):
            agg[part].setdefault(name, {"turns": 0, "cost": 0.0, "tokens": 0, "hit": 0, "miss": 0})["cost"] += cost
        m = agg["by_model"].setdefault(f"{r.get('provider')}:{r.get('model')}", {"turns": 0, "cost": 0.0, "hit": 0, "miss": 0, "out": 0})
        m["turns"] += 1
        m["cost"] += cost
        return
    proactive = bool(r.get("proactive"))  # 她主动开口的一轮: 轮数不算, 钱 / token 照记 (ADR-0026)
    if r.get("provider") == "local":  # 本机模型不花钱, 单算 (KV cache 复用率)
        for k in ("hit", "miss", "out"):
            agg["local"][k] += int(r.get(k) or 0)
        if not proactive:
            agg["local"]["turns"] += 1
        return
    if not proactive:
        agg["turns"] += 1
    for k in ("hit", "miss", "out", "think"):
        agg[k] += int(r.get(k) or 0)
    cost = r.get("cost")
    if cost is None:
        agg["unpriced"] += 1
    else:
        agg["cost"] += cost
        agg["saved"] += r.get("saved") or 0.0
    if not proactive:
        agg["peak_turns"] += r.get("tier") == "peak"
    tokens = int(r.get("hit") or 0) + int(r.get("miss") or 0) + int(r.get("out") or 0)
    for part, name in (("by_who", str(r.get("who") or "?")), ("by_ch", r.get("ch") or bucket(str(r.get("who") or ""), ""))):
        # 改额度之前的记录没有 ch: 按 who 算. 命中 / 没命中也按谁分 (2026-10-27: 工作台名片上的"缓存命中 84%"是今天全部的,
        # 工作台自己是 94%, 拖低的是 QQ 群每两分钟看一眼的 58%; 分开看才知道该改哪)
        w = agg[part].setdefault(name, {"turns": 0, "cost": 0.0, "tokens": 0, "hit": 0, "miss": 0})
        if not proactive:
            w["turns"] += 1
        w["cost"] += cost or 0.0
        w["tokens"] += tokens
        w["hit"] += int(r.get("hit") or 0)
        w["miss"] += int(r.get("miss") or 0)
    m = agg["by_model"].setdefault(f"{r.get('provider')}:{r.get('model')}",
                                   {"turns": 0, "cost": 0.0, "hit": 0, "miss": 0, "out": 0})
    if not proactive:
        m["turns"] += 1
    m["cost"] += cost or 0.0
    for k in ("hit", "miss", "out"):
        m[k] += int(r.get(k) or 0)


def _rate(hit: int, miss: int) -> float | None:
    return hit / (hit + miss) if hit + miss else None


def _finish(agg: dict) -> dict:
    """命中率、没命中率、总量, 钱数留 4 位小数."""
    out = json.loads(json.dumps(agg))
    out["tokens"] = out["hit"] + out["miss"] + out["out"]
    out["hit_rate"] = _rate(out["hit"], out["miss"])
    out["miss_rate"] = None if out["hit_rate"] is None else 1 - out["hit_rate"]
    out["local"]["hit_rate"] = _rate(out["local"]["hit"], out["local"]["miss"])
    out["cost"], out["saved"] = round(out["cost"], 4), round(out["saved"], 4)
    for v in [*out["by_who"].values(), *out["by_model"].values(), *out["by_ch"].values()]:
        v["cost"] = round(v["cost"], 4)
        if "hit" in v:
            v["hit_rate"] = _rate(v["hit"], v.get("miss", 0))
    return out


def _merge(into: dict, agg: dict) -> None:
    for k in ("turns", "cost", "saved", "hit", "miss", "out", "think", "unpriced", "peak_turns"):
        into[k] += agg[k]
    for k in ("turns", "hit", "miss", "out"):
        into["local"][k] += agg["local"][k]
    for part in ("by_who", "by_model", "by_ch"):
        for name, v in agg[part].items():
            t = into[part].setdefault(name, {k: 0 if isinstance(x, int) else 0.0 for k, x in v.items()})
            for k, x in v.items():
                t[k] = t.get(k, 0) + x  # 早些的汇总里没有 hit / miss 这些


def balance_url_ok(url: str) -> bool:
    """余额地址: DeepSeek 官方 (https) 或本机 (测试的假服务)."""
    p = urlsplit(url or "")
    return (p.scheme == "https" and p.hostname == "api.deepseek.com") or p.hostname in _LOOPBACK


async def fetch_balance(url: str, key: str, timeout_s: float = 8.0) -> dict:
    """GET 余额. 返回 {total, granted, topped_up, currency, available}; 出错抛 RuntimeError (原因给人看, 不带 key)."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as s:
            async with s.get(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}) as r:
                text = await r.text()
                if r.status in (401, 403):
                    raise RuntimeError(f"key 不对或没有权限 (HTTP {r.status})")
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise RuntimeError(f"连不上余额接口: {type(exc).__name__}") from None
    try:
        data = json.loads(text)
        infos = [i for i in data.get("balance_infos") or [] if isinstance(i, dict)]
        info = next((i for i in infos if i.get("currency") == "CNY"), infos[0] if infos else None)
        return {"total": float(info["total_balance"]), "granted": float(info.get("granted_balance") or 0),
                "topped_up": float(info.get("topped_up_balance") or 0), "currency": str(info.get("currency") or "CNY"),
                "available": bool(data.get("is_available", True))}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise RuntimeError("余额接口回的不是 DeepSeek 的格式") from None


def _span(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分钟" if h and m else f"{h} 小时" if h else f"{m} 分钟"


def _tokens(n: int) -> str:
    return f"{n / 10000:.1f} 万" if n >= 10000 else str(n)


class Ledger:
    """一个服务端一个 (create_app 里建, use() 设成当前的). conf 是配置的 billing 段."""

    def __init__(self, directory: Path, conf: dict | None = None, clock=time.time):
        self.conf = conf or {}
        self.dir = Path(directory)
        self.clock = clock
        self.prices = [p for p in self.conf.get("prices", []) if isinstance(p, dict)]
        self.peaks = {name: PeakClock(c) for name, c in (self.conf.get("peak") or {}).items()}
        self.balance_url = str(self.conf.get("balance_url") or "https://api.deepseek.com/user/balance")
        self.env_key = str(self.conf.get("env_key") or "")
        self.balance_every = float(self.conf.get("balance_every_s", 300))
        self.low = float(self.conf.get("low_balance", 5))
        self.balance: dict | None = None  # 最近一次查到的 (带 at、source)
        self.balance_error = ""
        self._models = weakref.WeakSet()  # 活着的 CloudLLM: 查余额用它们手里的 key
        self._days: dict[str, tuple[tuple, dict, list]] = {}  # 日期 -> ((mtime, size), 汇总, 最近几条)
        self._fetch: asyncio.Task | None = None
        self._loop: asyncio.Task | None = None
        self._tried = float("-inf")  # 上次开始查余额的时间
        self._obs = self._read_obs()
        self.budget = self._read_budget()
        self.on_alert = None  # (text, row) -> None: 某个渠道今天到了 80% / 100% (每样每天一次); 服务端接到桌宠的对话栏
        self._alerted: dict[tuple[str, str], str] = {}  # (日期, 渠道) -> 提醒过的档 (warn / stop)

    # ---- 价格和峰谷 ----
    def price_for(self, provider: str, model: str) -> dict | None:
        """价格表里第一条 provider 对得上、match 是模型名一部分的 (pro 写在前面, 空 match 兜底)."""
        name = str(model or "").lower()
        for p in self.prices:
            if p.get("provider") == provider and str(p.get("match") or "").lower() in name:
                return p
        return None

    def clock_for(self, price: dict | None) -> PeakClock | None:
        return self.peaks.get(price.get("peak")) if price else None

    def main_clock(self) -> PeakClock | None:
        """面板和她看的峰谷: DeepSeek 的 (没配就第一个)."""
        return self.peaks.get("deepseek") or next(iter(self.peaks.values()), None)

    def now_tier(self, t: float | None = None) -> dict | None:
        pc = self.main_clock()
        if pc is None:
            return None
        t = self.clock() if t is None else t
        tier, nxt = pc.tier(t), pc.next_change(t)
        bj = datetime.fromtimestamp(t, BJ)
        return {"tier": tier, "label": TIERS[tier], "off_ratio": pc.ratio, "next_at": nxt[0] if nxt else None,
                "next_tier": nxt[1] if nxt else None, "next_in_s": round(nxt[0] - t) if nxt else None,
                "segments": pc.segments(t), "minute": bj.hour * 60 + bj.minute, "date": bj.date().isoformat()}

    # ---- 记账 ----
    def price(self, provider: str, model: str, usage: dict | None, t: float | None = None) -> dict:
        """一次请求按价格表算: {hit, miss, out, think, tier, cost, saved}; 价格表里没有的 cost 是 None (本机模型 0)."""
        u = normalize(usage)
        t = self.clock() if t is None else t
        price = None if provider == "local" else self.price_for(provider, model)
        pc = self.clock_for(price)
        out = {**u, "tier": pc.tier(t) if pc else None, "cost": 0.0 if provider == "local" else None, "saved": None}
        if price:
            k = pc.ratio if pc and out["tier"] == "off" else 1.0
            hit_p, miss_p, out_p = (float(price.get(x, 0)) * k for x in ("hit", "miss", "out"))
            out["cost"] = round((u["hit"] * hit_p + u["miss"] * miss_p + u["out"] * out_p) / 1e6, 6)
            out["saved"] = round(u["hit"] * (miss_p - hit_p) / 1e6, 6)  # 命中的部分要是没命中, 得多花这么多
        return out

    def record(self, who: str, provider: str, model: str, usage: dict | None, t: float | None = None) -> dict | None:
        """记一次请求的用量. 全是 0 的不记. 返回记下的那行."""
        u = normalize(usage)
        if not (u["hit"] or u["miss"] or u["out"]):
            return None
        t = self.clock() if t is None else t
        rec = {"t": round(t, 3), "who": who, "ch": bucket(who), "provider": provider, "model": str(model or "")[:80],
               **self.price(provider, model, usage, t)}
        if _proactive.get():  # 她主动开口的一次请求: 钱和 token 照记, 轮数不算 (ADR-0026)
            rec["proactive"] = True
        self._append(rec, t)
        if provider != "local":
            self._check_alerts(t)
        return rec

    def record_fee(self, who: str, provider: str, what: str, cost: float, t: float | None = None) -> dict | None:
        """按次收钱的工具 (2026-10-28: DeepSeek 官方搜索, 约半分钱一次): 记一行钱, 没有 token, 不算一轮. 0 元的不记."""
        if not cost:
            return None
        t = self.clock() if t is None else t
        rec = {"t": round(t, 3), "who": who, "ch": bucket(who), "provider": provider, "model": str(what or "")[:80],
               "fee": True, "cost": round(float(cost), 6)}
        self._append(rec, t)
        self._check_alerts(t)
        return rec

    def _append(self, rec: dict, t: float) -> None:
        day = date.fromtimestamp(t).isoformat()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / f"{day}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            if (hit := self._days.get(day)) is not None:  # 缓存跟着加上这一行 (额度每次请求前都要看今天用了多少, 不重读整个文件)
                st = path.stat()
                _add(hit[1], rec)
                self._days[day] = ((st.st_mtime_ns, st.st_size), hit[1], (hit[2] + [rec])[-_KEEP:])
        except OSError as exc:
            log.warning("账本写不进去: %s", exc)

    # ---- 按渠道的额度 ----
    def _read_budget(self) -> dict:
        base = _budget_base(self.conf)
        try:
            saved = json.loads((self.dir / _BUDGET_FILE).read_text(encoding="utf-8"))
            return clean_budget(saved, base)
        except (OSError, ValueError):
            return base

    def set_budget(self, new: dict) -> dict:
        """面板改额度: 校验 (不对抛 ValueError), 存 budget.json, 今天的提醒按新上限重新算. 返回新的 budget_state."""
        self.budget = clean_budget(new, self.budget)
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / (_BUDGET_FILE + ".tmp")
        tmp.write_text(json.dumps({"unit": self.budget["unit"], "limits": self.budget["limits"]}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.dir / _BUDGET_FILE)
        self._alerted.clear()
        return self.budget_state()

    def budget_state(self, t: float | None = None) -> dict:
        """今天每个渠道和总额: 用了多少 (token 和元两样都给)、上限、比例、档 (off 不限 / ok / warn 到 80% / stop 到 100%)."""
        t = self.clock() if t is None else t
        day = date.fromtimestamp(t).isoformat()
        agg = self._day(day)[0]
        unit, warn, limits = self.budget["unit"], self.budget["warn"], self.budget["limits"]
        rows = []
        for k, name in LIMITS.items():
            if k == "total":
                tokens, cost = agg["hit"] + agg["miss"] + agg["out"], agg["cost"]
            else:
                c = agg["by_ch"].get(k) or {}
                tokens, cost = int(c.get("tokens", 0)), float(c.get("cost", 0.0))
            used, limit = (tokens if unit == "tokens" else cost), limits.get(k) or 0
            ratio = used / limit if limit else None
            level = "off" if not limit else "stop" if ratio >= 1 else "warn" if ratio >= warn else "ok"
            rows.append({"id": k, "name": name, "tokens": tokens, "cost": round(cost, 4), "used": used, "limit": limit,
                         "ratio": None if ratio is None else round(ratio, 4), "level": level})
        midnight = datetime.combine(date.fromtimestamp(t) + timedelta(days=1), datetime.min.time()).timestamp()
        return {"day": day, "unit": unit, "warn": warn, "rows": rows,
                "reset_bj": datetime.fromtimestamp(midnight, BJ).strftime("%H:%M")}  # 这台电脑的零点是北京时间几点

    def _amount(self, row: dict) -> str:
        if self.budget["unit"] == "tokens":
            return f"{_tokens(row['tokens'])} / {_tokens(int(row['limit']))} token"
        return f"¥{row['cost']:.2f} / ¥{float(row['limit']):.2f}"

    def over_budget(self, who: str) -> str | None:
        """这次请求 (算 bucket(who) 的) 还能不能发: 这个渠道或总额今天到 100% 了就返回原因, 不然 None."""
        if not any(self.budget["limits"].values()):
            return None
        rows = {r["id"]: r for r in self.budget_state()["rows"]}
        for k in (bucket(who), "total"):
            r = rows[k]
            if r["level"] == "stop":
                what = "云端的总额度" if k == "total" else f"{r['name']}的云端额度"
                return f"今天{what}用完了 ({self._amount(r)}), 先不用云端模型; 设置的账号与余额里可以调高, 不然明天零点重新算"
        return None

    def _check_alerts(self, t: float) -> None:
        """记完一笔看看: 哪个渠道 (或总额) 今天刚到 80% / 100%, 每样每档每天说一次."""
        if self.on_alert is None or not any(self.budget["limits"].values()):
            return
        st = self.budget_state(t)
        for r in st["rows"]:
            if r["level"] not in ("warn", "stop"):
                continue
            key = (st["day"], r["id"])
            if self._alerted.get(key) in (r["level"], "stop"):
                continue
            self._alerted[key] = r["level"]
            what = "云端的总额度" if r["id"] == "total" else f"{r['name']}的云端额度"
            text = (f"今天{what}用完了 ({self._amount(r)}), 先不用云端模型, 明天零点重新算" if r["level"] == "stop"
                    else f"今天{what}用了 {r['ratio']:.0%} ({self._amount(r)}), 到 100% 就先停")
            log.warning("额度: %s", text)
            try:
                self.on_alert(text, r)
            except Exception:  # 提醒发不出去不影响记账
                log.debug("额度提醒发不出去", exc_info=True)

    def _day(self, day: str) -> tuple[dict, list]:
        """一天的汇总和最近几条 (文件没变就用缓存)."""
        path = self.dir / f"{day}.jsonl"
        try:
            st = path.stat()
        except OSError:
            return _empty(), []
        stamp = (st.st_mtime_ns, st.st_size)
        hit = self._days.get(day)
        if hit and hit[0] == stamp:
            return hit[1], hit[2]
        agg, recent = _empty(), deque(maxlen=_KEEP)
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict):
                        _add(agg, r)
                        recent.append(r)
        except OSError:
            return _empty(), []
        self._days[day] = (stamp, agg, list(recent))
        return agg, list(recent)

    def summary(self, days: int = 14) -> dict:
        """面板要的全部: 现在的峰谷、余额、今天、这个月、最近几天、最近几条、价格表."""
        t = self.clock()
        today = date.fromtimestamp(t)
        agg_today, recent = self._day(today.isoformat())
        month = _empty()
        d = today.replace(day=1)
        while d <= today:
            _merge(month, self._day(d.isoformat())[0])
            d += timedelta(days=1)
        history = []
        for k in range(days - 1, -1, -1):
            day = (today - timedelta(days=k)).isoformat()
            a = _finish(self._day(day)[0])
            history.append({"day": day, "cost": a["cost"], "turns": a["turns"], "hit": a["hit"], "miss": a["miss"],
                            "out": a["out"], "hit_rate": a["hit_rate"]})
        prices = []
        for p in self.prices:
            pc = self.clock_for(p)
            r = pc.ratio if pc else 1.0
            prices.append({"provider": p.get("provider"), "name": p.get("name") or p.get("match") or p.get("provider"),
                           **{x: [round(float(p.get(x, 0)) * r, 4), float(p.get(x, 0))] for x in ("hit", "miss", "out")}})
        recent = sorted(recent, key=lambda r: r.get("t") or 0)[-30:][::-1]  # 新的在前 (文件按写入顺序, 一般就是时间顺序)
        return {"now": self.now_tier(t), "balance": self.balance_view(), "today": _finish(agg_today),
                "month": _finish(month), "days": history, "recent": recent, "prices": prices,
                "who": WHO, "low_balance": self.low, "budget": self.budget_state(t), "at": t}

    # ---- 余额 ----
    def watch(self, llm) -> None:
        """CloudLLM 建好时登记 (只留弱引用, key 还在它自己手里)."""
        self._models.add(llm)

    def live_models(self) -> list:
        """现在还活着的 CloudLLM (工作台挑模型用: 同一个 key 另建一个, 见 CloudLLM.clone)."""
        return list(self._models)

    def _source(self):
        """(取余额的协程工厂, 来源说明); 没有能用的 key 时 (None, 原因)."""
        host = urlsplit(self.balance_url).hostname
        for llm in list(self._models):
            if getattr(llm, "provider", "") == "deepseek" and urlsplit(getattr(llm, "base_url", "")).hostname == host \
                    and getattr(llm, "has_key", False):
                return (lambda llm=llm: llm.balance(self.balance_url)), "正在用的 DeepSeek 模型"
        key = os.environ.get(self.env_key, "").strip() if self.env_key else ""
        if key:
            return (lambda: fetch_balance(self.balance_url, key)), f"环境变量 {self.env_key}"
        return None, "没有 DeepSeek 的 key: 人设预设或外部渠道选了 DeepSeek 模型之后才查得到"

    async def refresh_balance(self, force: bool = False) -> dict | None:
        """查一次余额 (同时只有一个在查). 不 force 时 60 秒内查过就不再查."""
        if not force and self.balance and self.clock() - self.balance["at"] < 60:
            return self.balance_view()
        if not balance_url_ok(self.balance_url):
            self.balance_error = "余额地址只能是 DeepSeek 官方的或本机的"
            return self.balance_view()
        if self._fetch is None or self._fetch.done():
            self._fetch = asyncio.get_running_loop().create_task(self._do_fetch())
        await asyncio.shield(self._fetch)
        return self.balance_view()

    async def _do_fetch(self) -> None:
        self._tried = self.clock()
        get, source = self._source()
        if get is None:
            self.balance_error = source
            return
        try:
            b = await get()
        except RuntimeError as exc:
            self.balance_error = str(exc)
            return
        except Exception as exc:  # noqa: BLE001 (余额查不到不能影响别的)
            log.warning("查余额出错: %r", exc)
            self.balance_error = f"查余额出错: {type(exc).__name__}"
            return
        self.balance_error = ""
        self.balance = {**b, "at": self.clock(), "source": source}
        self._observe(b["total"])

    def _read_obs(self) -> dict:
        try:
            obs = json.loads((self.dir / "balance.json").read_text(encoding="utf-8"))
            return obs if isinstance(obs, dict) else {}
        except (OSError, ValueError):
            return {}

    def _observe(self, total: float) -> None:
        """记今天第一次和最近一次看到的余额; 往上涨的算充值 / 赠金."""
        day = date.fromtimestamp(self.clock()).isoformat()
        o = self._obs
        if o.get("day") != day:
            o.clear()
            o.update(day=day, first=total, last=total, topup=0.0)
        else:
            if total > o["last"] + 0.004:
                o["topup"] = round(o.get("topup", 0.0) + total - o["last"], 4)
            o["last"] = total
        o["at"] = self.clock()
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / "balance.json.tmp"
            tmp.write_text(json.dumps(o, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.dir / "balance.json")
        except OSError as exc:
            log.warning("余额记录写不进去: %s", exc)

    def balance_view(self) -> dict:
        b = dict(self.balance) if self.balance else {}
        o = self._obs
        if o.get("day") == date.fromtimestamp(self.clock()).isoformat() and "first" in o:
            b["today_drop"] = round(max(0.0, o["first"] - o["last"] + o.get("topup", 0.0)), 4)
            b["today_topup"] = o.get("topup", 0.0)
        b["error"] = self.balance_error
        b["low"] = bool(self.balance) and self.balance["total"] < self.low
        return b

    def kick(self, max_age: float | None = None) -> None:
        """上次查 (成没成都算) 超过 max_age 秒 (默认 balance_every_s) 就在后台查一次, 不等. 给她的那一行和面板用."""
        if self.clock() - self._tried < (self.balance_every if max_age is None else max_age):
            return
        if self._fetch is not None and not self._fetch.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 同步代码里调的 (没有事件循环): 不查
            return
        self._fetch = loop.create_task(self._do_fetch())

    async def _poll(self) -> None:
        while True:
            await self.refresh_balance(force=True)
            await asyncio.sleep(max(30.0, self.balance_every))

    def start(self) -> None:
        """服务端起来后每 balance_every_s 查一次余额 (0: 不定时查, 面板打开或她要看时才查)."""
        if self.balance_every > 0 and self._loop is None:
            self._loop = asyncio.get_running_loop().create_task(self._poll())

    async def stop(self) -> None:
        for task in (self._loop, self._fetch):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._loop = self._fetch = None

    # ---- 给她看的 ----
    def wallet_line(self) -> str | None:
        """[钱包] 一行: 余额、今天花了多少 (token、缓存命中)、现在是不是高峰. 什么都没有时 None."""
        self.kick()
        parts = []
        if self.balance:
            parts.append(f"DeepSeek 余额 ¥{self.balance['total']:.2f}" + (" (不多了)" if self.balance["total"] < self.low else ""))
        a = _finish(self._day(date.fromtimestamp(self.clock()).isoformat())[0])
        if a["turns"]:
            rate = f", 缓存命中 {a['hit_rate']:.0%}" if a["hit_rate"] is not None else ""
            parts.append(f"今天花了 ¥{a['cost']:.2f} ({_tokens(a['tokens'])} token{rate})")
        tight = [r for r in self.budget_state()["rows"] if r["level"] in ("warn", "stop")]
        if tight:  # 快用完的渠道: 怎么省由她自己定
            def say(r: dict) -> str:
                return f"{r['name']}用完了 (先不用云端)" if r["level"] == "stop" else f"{r['name']}用了 {r['ratio']:.0%}"
            parts.append("今天的额度: " + ", ".join(say(r) for r in tight))
        now = self.now_tier()
        if now:
            s = f"现在是{now['label']}" + (f" (价钱是高峰的 {now['off_ratio']:g} 倍)" if now["tier"] == "off" else "")
            if now["next_in_s"] is not None:
                s += f", {_span(now['next_in_s'])}后{'进入高峰' if now['next_tier'] == 'peak' else '回到空闲'}"
            parts.append(s)
        return ("[钱包] 你在云端思考、说话花的是主人的钱: " + "; ".join(parts) + ".") if parts else None


_current: Ledger | None = None


def use(ledger: Ledger | None) -> None:
    """设成当前的账本 (create_app 里). None: 不记 (单测里没建服务端时)."""
    global _current
    _current = ledger


def current() -> Ledger | None:
    return _current


def record(who: str, provider: str, model: str, usage: dict | None) -> dict | None:
    return _current.record(who, provider, model, usage) if _current is not None else None


def wallet_line() -> str | None:
    return _current.wallet_line() if _current is not None else None


def over_budget(who: str) -> str | None:
    return _current.over_budget(who) if _current is not None else None

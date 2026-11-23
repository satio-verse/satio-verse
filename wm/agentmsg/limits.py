"""额度本: 总开关、每天条数、每天的钱、来回次数. 三个上限由这里硬算硬拦, 不靠她自觉 (计划第 4 节).

- 所有改额度的操作都在一个临界区里 (进程内一把锁 + 跨进程文件锁): 进去先从盘上重读, 再判断, 再写回 (原子替换). 内存里的数不作数.
- reserve() 一步做完"查上限 + 预占 + 写盘", 通过了才给一张凭条 (Ticket); 拒发的不占任何额度. 起进程的入口 (runner.run) 要凭条.
- settle() 把预占换成实际花费; 同一张凭条结算两次只算一次. 解析不出花费 / 超时 / 崩溃: 调用的人传 None, 按预占全额算 (宁多不少).
- "天" 用这台电脑的本地日期 (和账本一致); 有效日期 = max(存的日期, 今天): 时钟往回拨, 计数不清; 往后拨 (或真到了第二天) 才清每天的数.
  来回数不按天清, 只有主人 (agent_msg_ctl.py resume) 能清.
- 状态读不出来 / 坏了 / 字段类型不对: 一律拒发 (失败时关着). 状态文件不存在 = 开关关着.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import AgentMsgError, FileLock, StateError, read_json, settings, write_json

STATE = "state.json"
VERSION = 1
BOOT = secrets.token_hex(6)  # 这个进程的标记 (重启后认得出哪些凭条是上一个进程留下的)


def chain_key(root_session: str, target: str) -> str:
    return f"{root_session}|{target}"


def local_today() -> str:
    return datetime.now().date().isoformat()


@dataclass(frozen=True)
class Ticket:
    """预占凭条. 没有它, runner 没有起进程的入口."""
    id: str
    chain: str
    target: str
    thread: str
    cny: float


def _fresh() -> dict:
    return {"v": VERSION, "enabled": False, "codex": False, "day": "", "msgs": 0, "spent": 0.0,
            "reserved": {}, "chains": {}, "notified": {}}


def _validate(st) -> dict:
    """字段类型不对一律当坏了 (比如 enabled 写成字符串 "true" 或数字 1)."""
    ok = (isinstance(st, dict) and st.get("v") == VERSION and isinstance(st.get("enabled"), bool)
          and isinstance(st.get("codex"), bool) and isinstance(st.get("day"), str)
          and isinstance(st.get("msgs"), int) and not isinstance(st.get("msgs"), bool) and st["msgs"] >= 0
          and isinstance(st.get("spent"), (int, float)) and not isinstance(st.get("spent"), bool) and st["spent"] >= 0
          and isinstance(st.get("reserved"), dict) and isinstance(st.get("chains"), dict) and isinstance(st.get("notified"), dict))
    if ok:
        ok = all(isinstance(v, dict) and isinstance(v.get("cny"), (int, float)) and isinstance(v.get("thread"), str)
                 and isinstance(v.get("t"), (int, float)) and isinstance(v.get("boot"), str) for v in st["reserved"].values())
    if ok:
        ok = all(isinstance(v, dict) and isinstance(v.get("passes"), int) and not isinstance(v.get("passes"), bool) and v["passes"] >= 0
                 for v in st["chains"].values())
    if not ok:
        raise StateError("额度本的内容不对 (字段缺了或类型不对)")
    return st


class Limits:
    def __init__(self, home: Path | str, conf: dict | None = None, today=None):
        self.home = Path(home)
        self.cfg = settings(conf)
        self.today = today or local_today
        self.path = self.home / STATE
        self._lock_path = self.home / "lock"

    # ---- 开关 (不拿锁、不建目录; 读不出来 / 不是 True 都算关) ----
    def enabled(self) -> bool:
        """每次直接读那个小文件 (不缓存: Windows 上文件时间比 time.time() 粗约 15 毫秒, 缓存会漏掉快速翻转)."""
        try:
            return _validate(read_json(self.path, tries=3) or {})["enabled"] is True
        except (StateError, AgentMsgError):
            return False   # 不存在 / 读到半截 / 坏了 / 类型不对: 都算关

    def _locked(self) -> FileLock:
        return FileLock(self._lock_path)

    def _load(self) -> dict:
        data = read_json(self.path)
        return _fresh() if data is None else _validate(data)

    def _save(self, st: dict) -> None:
        write_json(self.path, st)

    # ---- 日期、过期的预占 ----
    def _roll(self, st: dict) -> bool:
        eff = max(st["day"], self.today())
        if eff == st["day"]:
            return False
        if st["day"]:  # 第一次用 (day 是空) 不算换天
            st["msgs"], st["spent"] = 0, 0.0
        st["day"], st["notified"] = eff, {}
        return True

    def _sweep(self, st: dict, now: float | None = None, boot_only: bool = False) -> bool:
        """没结算的凭条 (进程崩了 / 重启了): 按预占全额记成花掉. 默认只收超过 2 倍超时 + 2 分钟的; boot_only 收别的进程留下的."""
        now = time.time() if now is None else now
        ttl = self.cfg["timeout_s"] * 2 + 120
        dead = [k for k, v in st["reserved"].items() if (v["boot"] != BOOT if boot_only else now - v["t"] > ttl)]
        for k in dead:
            st["spent"] = round(st["spent"] + float(st["reserved"].pop(k)["cny"]), 6)
        return bool(dead)

    # ---- 预占 ----
    def reserve(self, chain: str, target: str, thread: str = "", cny: float | None = None) -> Ticket:
        cfg = self.cfg
        cost = round(float(cfg["per_call_cny"] if cny is None else cny), 6)
        if not self.enabled():
            raise AgentMsgError("off", "Agent 互发消息的总开关关着, 这是主人的决定; 要用请主人在终端里打开")
        with self._locked():
            try:
                st = self._load()
            except StateError as exc:
                raise AgentMsgError("state", f"额度本读不出来, 这次不发 ({exc})") from None
            if st["enabled"] is not True:
                raise AgentMsgError("off", "Agent 互发消息的总开关关着, 这是主人的决定; 要用请主人在终端里打开")
            dirty = self._roll(st) | self._sweep(st)
            try:
                return self._admit(st, chain, target, thread, cost, dirty)
            except AgentMsgError:
                if dirty:
                    self._save(st)
                raise

    def _admit(self, st: dict, chain: str, target: str, thread: str, cost: float, dirty: bool) -> Ticket:
        cfg = self.cfg
        if target == "codex" and not st["codex"]:
            raise AgentMsgError("target_off", "Codex 这一档还没有开 (没经过真机验证, 要主人单独打开)")
        if st["msgs"] + 1 > cfg["daily_messages"]:
            raise AgentMsgError("daily_count", f"今天已经发了 {st['msgs']} 条, 到每天 {cfg['daily_messages']} 条的上限了, 明天再说")
        held = sum(float(v["cny"]) for v in st["reserved"].values())
        if round(st["spent"] + held + cost, 6) > cfg["daily_cny"] + 1e-9:
            raise AgentMsgError("daily_cny", f"今天已经花了约 {st['spent']:.2f} 元 (在途 {held:.2f} 元), "
                                             f"再发一条要预占 {cost:.2f} 元, 会超过每天 {cfg['daily_cny']:g} 元的上限")
        passes = st["chains"].get(chain, {}).get("passes", 0)
        if passes + 1 > cfg["max_passes"]:
            raise AgentMsgError("chain", f"这个会话和这个对方已经来回 {passes} 次了, 到 {cfg['max_passes']} 次的上限, 停在这里等主人")
        if thread and any(v["thread"] == thread for v in st["reserved"].values()):
            raise AgentMsgError("inflight", "这个对话上一条还没回话, 等回话了再发")
        if len(st["reserved"]) >= cfg["max_inflight"]:
            raise AgentMsgError("inflight", f"同时在问的已经有 {len(st['reserved'])} 条, 等回话了再发")
        tid = "k" + secrets.token_hex(6)
        st["msgs"] += 1
        st["chains"][chain] = {"passes": passes + 1}
        st["reserved"][tid] = {"cny": cost, "thread": thread, "t": time.time(), "boot": BOOT}
        self._save(st)
        return Ticket(tid, chain, target, thread, cost)

    def attach_thread(self, ticket: Ticket, thread: str) -> Ticket:
        """新开的对话在预占之后才有编号: 把编号记到凭条上 (同一对话同时只许一条在飞)."""
        with self._locked():
            st = self._load()
            if ticket.id in st["reserved"]:
                st["reserved"][ticket.id]["thread"] = thread
                self._save(st)
        return Ticket(ticket.id, ticket.chain, ticket.target, thread, ticket.cny)

    def settle(self, ticket: Ticket, actual: float | None) -> bool:
        """把预占换成实际花费; actual 是 None (解析不出 / 超时 / 崩溃) 按预占全额. 同一张凭条只结算一次 (第二次返回 False)."""
        with self._locked():
            try:
                st = self._load()
            except StateError:
                return False
            held = st["reserved"].pop(ticket.id, None)
            if held is None:
                return False
            value = float(held["cny"]) if actual is None else max(0.0, float(actual))
            st["spent"] = round(st["spent"] + value, 6)
            self._save(st)
            return True

    def recover(self) -> int:
        """服务端刚起来: 上一个进程留下的、没结算的凭条, 按预占全额记成花掉."""
        if not self.path.exists():
            return 0
        with self._locked():
            try:
                st = self._load()
            except StateError:
                return 0
            before = len(st["reserved"])
            if self._sweep(st, boot_only=True):
                self._save(st)
            return before - len(st["reserved"])

    # ---- 主人的操作 (agent_msg_ctl.py) 和给她看的数 ----
    def set_flag(self, key: str, value: bool) -> None:
        if key not in ("enabled", "codex"):
            raise ValueError(key)
        with self._locked():
            try:
                st = self._load()
            except StateError:
                st = _fresh()   # 主人明确要开 / 关: 坏了的状态文件重新来 (拒发的账不会因此多出额度, 因为坏文件里本来就没法算)
            st[key] = bool(value)
            self._roll(st)
            self._save(st)

    def resume(self, chain: str | None = None) -> int:
        """把一条链 (不给就全部) 的来回数清零. 返回清了几条."""
        with self._locked():
            st = self._load()
            keys = [k for k in st["chains"] if chain is None or k == chain]
            for k in keys:
                st["chains"][k] = {"passes": 0}
            self._save(st)
            return len(keys)

    def notice_once(self, code: str) -> bool:
        """每个上限每天只放一次页面提示: 第一次返回 True."""
        with self._locked():
            try:
                st = self._load()
            except StateError:
                return False
            self._roll(st)
            if st["notified"].get(code):
                return False
            st["notified"][code] = True
            self._save(st)
            return True

    def snapshot(self, chains: list[str] | None = None) -> dict:
        """给她 (agent_list) 和主人 (status) 看的数. 读不出来写明, 不抛."""
        cfg = self.cfg
        try:
            st = self._load()
        except StateError as exc:
            return {"enabled": False, "error": str(exc)}
        day_ok = max(st["day"], self.today()) == st["day"] or not st["day"]
        msgs, spent = (st["msgs"], st["spent"]) if day_ok else (0, 0.0)
        held = sum(float(v["cny"]) for v in st["reserved"].values())
        wanted = st["chains"] if chains is None else {k: st["chains"].get(k, {"passes": 0}) for k in chains}
        return {"enabled": st["enabled"], "codex": st["codex"], "day": max(st["day"], self.today()),
                "messages": {"used": msgs, "limit": cfg["daily_messages"], "left": max(0, cfg["daily_messages"] - msgs)},
                "cny": {"spent": round(spent, 4), "in_flight": round(held, 4), "limit": cfg["daily_cny"],
                        "left": round(max(0.0, cfg["daily_cny"] - spent - held), 4)},
                "chains": {k: {"passes": v["passes"], "limit": cfg["max_passes"], "left": max(0, cfg["max_passes"] - v["passes"])}
                           for k, v in wanted.items()},
                "in_flight": len(st["reserved"])}

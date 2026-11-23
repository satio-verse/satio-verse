"""心跳的读写层: 文件、目录、配置。判断全在 `wm/heartbeat.py`。

为什么要分开: 这套东西每 11–26 分钟醒一次, 一跑就是几个月。判断写错一次会连着错很久,
所以判断部分做成纯函数好穷举测; 这里只管"放哪、读什么、怎么容错"。

放哪:
- 用户数据区: `<用户数据>/heartbeat/`(默认 `~/.worldmodel/heartbeat/`)——
  里面 `beats-YYYYMMDD.jsonl`(平时)、`drawer.json`(抽屉)、`chain.json`(黑盒)、`think.json`(当天额度与轮次)。
- **不抢** `~/.worldmodel/serve/beat.json`: 那是看守的心跳。

读什么(只读, 不写):
- 工作台的定时提醒 `<agent家>/state/workbench-schedule/schedule.json`
- 工作台的待办     `<agent家>/state/workbench-todo/todos.json`

2026-11-17 开工时确认: **服务端此前没有任何代码读这两个文件**(它们只活在她自己的扩展里),
所以这里是"补上那个缺口"的第一处代码。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from datetime import datetime
from pathlib import Path

from .. import heartbeat as H

log = logging.getLogger("wm.agent.heartbeat")

DIR_NAME = "heartbeat"
ENV_HOME = "WM_AGENT_HOME"
ENV_STATE = "WM_HEARTBEAT_DIR"

DEFAULTS = {
    "enabled": False,        # 默认关: 新东西先能开, 再谈默认开
    "budget_cny": 0.5,       # 思考的每日额度 (user 2026-11-17: B 方案, 0.5 元起)
    "idle_notify": False,    # 空转要不要在工作台提示 (不叫醒主人)
    "seal_days": 7,          # 黑盒封几天的记忆
}


def cfg_of(cfg: dict | None) -> dict:
    """取配置, 缺的补默认。**读坏了当关着**, 不要把整个服务拖下去。"""
    raw = (cfg or {}).get("heartbeat") if isinstance(cfg, dict) else None
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if v is not None})
    for k in ("enabled", "idle_notify"):
        out[k] = bool(out.get(k))
    for k in ("budget_cny", "seal_days"):
        try:
            out[k] = float(out.get(k))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    return out


def state_dir(cfg: dict | None = None) -> Path:
    """心跳自己的目录。测试、自检时跟着 trace.log_dir 走, 免得写到真人数据里。"""
    env = os.environ.get(ENV_STATE)
    if env:
        return Path(env).expanduser()
    try:
        from ..cognition.store import user_dir

        return user_dir(cfg or {}, DIR_NAME)
    except Exception:  # noqa: BLE001 - 这里失败也要能起来, 退回默认
        log.warning("取不到用户数据区, 退回 ~/.worldmodel/heartbeat", exc_info=True)
        return Path("~/.worldmodel").expanduser() / DIR_NAME


def agent_home() -> Path:
    """工作台扩展的状态家 (`~/.worldmodel/agent`)。环境变量优先, 和 jobwatch 一个规矩。"""
    env = os.environ.get(ENV_HOME)
    return Path(env).expanduser() if env else Path("~/.worldmodel").expanduser() / "agent"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError):
        # 读坏了返回 None: 这一次别判, 免得把"正在写"当成"清空了"
        log.warning("读不了 %s, 这一拍先跳过", path)
        return None


def read_schedule(home: Path | None = None) -> list[dict]:
    data = _read_json((home or agent_home()) / "state" / "workbench-schedule" / "schedule.json")
    items = data.get("items") if isinstance(data, dict) else None
    return [it for it in (items or ()) if isinstance(it, dict)]


def read_todos(home: Path | None = None) -> list[dict]:
    data = _read_json((home or agent_home()) / "state" / "workbench-todo" / "todos.json")
    items = data.get("items") if isinstance(data, dict) else None
    return [it for it in (items or ()) if isinstance(it, dict)]


def due_now(now: datetime, home: Path | None = None) -> list[dict]:
    """这一拍要叫醒她的定时提醒。读不到就是没有, 不重试。"""
    return H.due_schedule(read_schedule(home), now)


def todo_open(now: datetime, home: Path | None = None) -> list[dict]:
    return H.open_todos(read_todos(home))


# ---------------------------------------------------------------- 写 (只有这几处)

def beats_path(day: datetime, root: Path | None = None) -> Path:
    return (root or state_dir()) / f"beats-{day:%Y%m%d}.jsonl"


def append_beat(rec: dict, root: Path | None = None) -> bool:
    """写一行"平时"。失败不抛: 心跳本身不该把服务弄挂。"""
    try:
        p = beats_path(datetime.now(), root)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", newline="\n") as f:
            f.write(H.dumps(rec) + "\n")
        return True
    except (OSError, TypeError, ValueError):
        log.warning("平时那一行写不进去", exc_info=True)
        return False


def read_beats(day: datetime, root: Path | None = None) -> list[dict]:
    try:
        text = beats_path(day, root).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    return [r for r in (H.loads(ln) for ln in text.splitlines()) if r]


def drawer_load(root: Path | None = None) -> list[str]:
    data = _read_json((root or state_dir()) / "drawer.json")
    items = data.get("items") if isinstance(data, dict) else None
    return [str(x) for x in (items or ()) if str(x).strip()]


def drawer_push(text: str, root: Path | None = None) -> list[str]:
    items = H.drawer_push(drawer_load(root), text)
    _write_json((root or state_dir()) / "drawer.json", {"items": items})
    return items


def _write_json(path: Path, obj) -> bool:
    """原子写 (临时文件 + replace), 和项目里其他地方一个规矩。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        log.warning("写不了 %s", path, exc_info=True)
        return False


def seal_day(day: datetime, parts, root: Path | None = None) -> dict:
    """黑盒: 把这一天接进哈希链。`parts` 是 [(名字, bytes)]。"""
    root = root or state_dir()
    data = _read_json(root / "chain.json")
    chain = data.get("chain") if isinstance(data, dict) and isinstance(data.get("chain"), list) else []
    prev = chain[-1].get("hash") if chain and isinstance(chain[-1], dict) else ""
    cur = H.hash_chain(prev or "", parts)
    chain.append({"day": day.strftime("%Y-%m-%d"), "hash": cur, "prev": prev, "files": len(list(parts or ()))})
    _write_json(root / "chain.json", {"chain": chain[-400:]})
    return chain[-1]


def budget_state(day: datetime, cap_cny: float, root: Path | None = None) -> H.Budget:
    """当天已经花掉多少。跨天自动归零 (按天文件, 不用自己清)。"""
    data = _read_json((root or state_dir()) / f"think-{day:%Y%m%d}.json")
    spent = 0.0
    if isinstance(data, dict):
        try:
            spent = float(data.get("spent_cny") or 0.0)
        except (TypeError, ValueError):
            spent = 0.0
    return H.Budget(cap_cny=cap_cny, spent_cny=spent)


def budget_save(day: datetime, b: H.Budget, root: Path | None = None) -> bool:
    return _write_json(
        (root or state_dir()) / f"think-{day:%Y%m%d}.json",
        {"cap_cny": b.cap_cny, "spent_cny": round(b.spent_cny, 6), "exhausted": b.exhausted()},
    )


# 你在多长时间内找过她, 就算"你在场"。15 分钟: 比一次对话长, 比"半天没人"短.
# 口径是 owner_seen (ChannelManager 里你最后一次真找她的时刻), 不是文件修改时间.
PRESENT_WINDOW_S = 15 * 60

# ---------------------------------------------------------------------------
# 服务端循环 (v1.1: 主人在不在用真信号, 叫醒留开关)
# ---------------------------------------------------------------------------

class Runner:
    """心跳的服务端循环。

    v1 刻意**不叫醒她**: 叫醒要走派活线 (hub.new_session + send), 那条路在本机还没真机验收过;
    心跳是"一跑几个月、错一次连着错很久"的东西, 所以先把拍子/平时/到期清单做实。

    v1 也**不判断主人在不在**: 可靠信号在 ChannelManager 的内存里 (life_recent / ask_queue),
    从日志猜会猜错, 而心跳记错比不记更糟。接缝留在这里, v2 接上 manager 时一行就能开。
    所以现在每一行的"平时"都记成"不在", 只用来攒时间轴, 不当成事实。
    """

    def __init__(self, cfg: dict | None = None, *, clock=None, rng: random.Random | None = None,
                 presence=None, doing=None, wake=None):
        self.cfg = cfg_of(cfg)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._rng = rng or random.Random()
        self._presence = presence      # () -> 你最后一次找她的时刻 (unix 秒, 0 = 从来没有)
        self._doing = doing            # () -> 她手上在干什么 (一句话, 可空)
        self._wake = wake              # (due) -> None; 不给 = 不叫醒会话
        self._task = None
        self.beats = 0
        self.idle_beats = 0
        self.last_at = None
        self.last_due: list[dict] = []

    def present(self, now: datetime) -> bool:
        """你现在算不算在场: 你上次找她的时刻距今不到 PRESENT_WINDOW_S。读不到就当不在。"""
        if self._presence is None:
            return False
        try:
            last = float(self._presence() or 0.0)
        except Exception:  # noqa: BLE001
            return False
        return last > 0 and (now.timestamp() - last) < PRESENT_WINDOW_S

    def beat_once(self, now: datetime | None = None) -> dict:
        """一拍: 写一行"平时", 把到期的提醒/待办记进 due.json。返回写下的那条记录。"""
        now = now or self._clock()
        root = state_dir(self.cfg)
        here = self.present(now)
        doing = ""
        if here and self._doing is not None:
            try:
                doing = str(self._doing() or "")[:40]
            except Exception:  # noqa: BLE001
                doing = ""
        self.idle_beats = 0 if here else self.idle_beats + 1
        b = H.Beat(at=now, owner_present=here, doing=doing, idle_beats=self.idle_beats)
        rec = b.line()
        self.beats += 1
        self.last_at = now
        try:
            append_beat(rec, root=root)
        except Exception:  # noqa: BLE001 - 记不上也不能把循环带走
            log.warning("写不进心跳的平时记录", exc_info=True)
        try:
            due = due_now(now)
        except Exception:  # noqa: BLE001
            due = []
        self.last_due = due
        if due:
            try:
                _write_json(root / "due.json", {"at": now.isoformat(), "items": due})
            except Exception:  # noqa: BLE001
                log.warning("写不进 due.json", exc_info=True)
            log.info("心跳: %d 条到期的提醒/待办 (v1 只记账, 不叫醒): %s", len(due),
                     "；".join(str(d.get("text") or "")[:24] for d in due[:3]))
        # 叫醒: 只有给了 wake 回调才叫 (默认没有 = 只记账, 不打扰人)。
        # 放在 if due 之外判一次, 免得"到期了但没回调"时静默以为叫过了。
        if due and self._wake is not None:
            try:
                self._wake(due)
            except Exception:  # noqa: BLE001 - 叫不醒也不能把循环带走
                log.warning("叫醒失败", exc_info=True)
        return rec

    async def loop(self) -> None:
        log.info("心跳起来了: 每 11-26 分钟一拍 (主人在不在已接 owner_seen, 叫醒关)")
        while True:
            try:
                await asyncio.sleep(H.next_gap_s(self._rng))
            except asyncio.CancelledError:
                raise
            try:
                await asyncio.to_thread(self.beat_once)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 一拍出错不能把整个循环带走
                log.warning("这一拍出错了, 下一拍继续", exc_info=True)

    def start(self) -> None:
        if not self.cfg.get("enabled"):
            log.info("心跳关着 (heartbeat.enabled=false), 不启动")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self.loop(), name="wm-heartbeat")

    async def close(self) -> None:
        t, self._task = self._task, None
        if t is None or t.done():
            return
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.warning("停心跳时出错", exc_info=True)


_RUNNER = None


def start(cfg: dict | None = None, *, presence=None, doing=None, wake=None):
    """服务端起搏时调 (wm/app.py 的 _startup)。配置关着就什么都不做。"""
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = Runner(cfg, presence=presence, doing=doing, wake=wake)
    _RUNNER.start()
    return _RUNNER


async def stop() -> None:
    """服务端收尾时调 (_cleanup)。"""
    global _RUNNER
    r, _RUNNER = _RUNNER, None
    if r is not None:
        await r.close()
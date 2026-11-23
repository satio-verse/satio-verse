"""统一状态 (2026-11-01 用户"好，接着做": 升级方案第 3 步最后一块; Super-agent 接入设计第 4 步"工作台展示详细来源、
运行位置、等待原因与待验收产物"; 研究总报告"工作台应显示谁在执行、是否等设备、是否等用户及最后真实产物").

以前在做的事各有各的状态, 分在不同地方: 会话这一轮 (Session.status / run)、等你点头的 (asks)、排队的、中途补充的、
子任务 (TeamManager)、她起的后台作业 (jobwatch)、终端里在跑的命令 (TerminalManager)、你在工作区面板里跑的命令
(WorkspaceJobs)、核对没过的 (acceptance). 这里汇成一份"现在在做什么": 每一项写清是谁、在哪个会话、在等什么
(等你 / 等子任务 / 在跑工具 / 在想 / 排队), 等你的排最前面. 只读, 不改任何状态.
"""
from __future__ import annotations

import time

ACTIVE = {"queued", "running", "waiting", "cancelling"}
# 排序: 等你的最前, 然后是在做的, 后台的, 排队的
ORDER = {"you": 0, "children": 1, "tool": 1, "model": 1, "": 2, "queue": 3}


def _pending_asks(s) -> list[dict]:
    """这个会话里还没答复的申请 (对应的 ask 事件: 标签和要做的事)."""
    open_ids = [cid for cid, fut in (getattr(s, "asks", None) or {}).items() if not fut.done()]
    events = {e.get("id"): e for e in s.events if e.get("type") == "ask"}
    return [events.get(cid) or {"id": cid} for cid in open_ids]


def _running_tool(s) -> dict | None:
    """这一轮最后一个还没出结果的工具 (正在跑的)."""
    done = set()
    for e in reversed(s.events[-200:]):
        if e.get("type") == "tool_result":
            done.add(e.get("id"))
        elif e.get("type") == "tool" and e.get("state") == "running" and e.get("id") not in done:
            return e
        elif e.get("type") in ("user", "done"):
            return None
    return None


QUIET_NOTE_S = 30 * 60  # 终端"在跑"却这么久没有新输出: 卡片上写明, 免得挂几个小时没人发现 (2026-11-03 PowerShell1 挂了 9 小时)


def _quiet_s(row: dict) -> float:
    """终端多久没有新输出了 (秒). 没记过输出时间的 (旧记录) 从开的时候算."""
    last = row.get("last_output") or row.get("started") or time.time()
    return max(0.0, time.time() - float(last))


def _quiet_note(row: dict) -> str:
    quiet = _quiet_s(row)
    if quiet < QUIET_NOTE_S:
        return ""
    span = f"{quiet / 3600:.0f} 小时" if quiet >= 5400 else f"{quiet / 60:.0f} 分钟"
    return f"（{span}没有新输出，可能停在提示符或在等输入，看一眼再决定停不停）"


def _session_items(hub, s, children: list[dict]) -> list[dict]:
    run, out = s.run or {}, []
    base = {"session": s.id, "title": s.title, "workspace": s.workspace}
    extra = []
    if s.steer:
        extra.append(f"待交给她 {len(s.steer)} 条")
    if s.queue:
        extra.append(f"排队 {len(s.queue)} 条")
    if s.status != "idle":
        asks = _pending_asks(s)
        mine = [c for c in children if c.get("parent_id") == s.id and c.get("status") in ACTIVE]
        tool = _running_tool(s)
        if asks:
            a = asks[-1]
            what = f"等你点头：{a.get('label') or '一步操作'}" + (f" {str(a.get('text') or '')[:60]}" if a.get("text") else "")
            out.append({**base, "kind": "session", "id": s.id, "state": "waiting", "waiting_for": "you",
                        "detail": what + (f"（还有 {len(asks) - 1} 件）" if len(asks) > 1 else ""), "since": run.get("started")})
        elif mine and not tool:
            out.append({**base, "kind": "session", "id": s.id, "state": "running", "waiting_for": "children",
                        "detail": f"等子任务 {len(mine)} 个做完再汇总", "since": run.get("started")})
        elif tool:
            out.append({**base, "kind": "session", "id": s.id, "state": "running", "waiting_for": "tool",
                        "detail": f"在{tool.get('label') or tool.get('name') or '用工具'}：{str(tool.get('args') or '')[:60]}",
                        "since": tool.get("t") or run.get("started")})
        else:
            steps = int(s.stats.get("steps", 0)) - int(run.get("steps_start", s.stats.get("steps", 0)))
            out.append({**base, "kind": "session", "id": s.id, "state": "running", "waiting_for": "model",
                        "detail": f"在想下一步（第 {max(1, steps)} 步）", "since": run.get("started")})
        if extra:
            out[-1]["detail"] += "；" + "，".join(extra)
        return out
    check = run.get("check") or {}
    if check.get("verdict") == "needs_check" and run.get("verification", "unreviewed") == "unreviewed":
        out.append({**base, "kind": "check", "id": s.id, "state": "needs_check", "waiting_for": "you",
                    "detail": "说做完了，但待核查：" + "；".join(check.get("reasons") or [])[:120], "since": run.get("ended")})
    if s.queue and s.stop_event.is_set():
        out.append({**base, "kind": "session", "id": s.id, "state": "queued", "waiting_for": "you",
                    "detail": f"停下了，还排着 {len(s.queue)} 条没开始（发一句或在队列里处理）", "since": s.queue[0].get("created")})
    return out


def timeline(hub, day: str = "") -> dict:
    """她的流水账: 她自己的日志 (QQ 渠道的"她自己"那一份: 在 QQ 上接了什么活、做完没有、去学了什么、看了什么……),
    某一天的, 最多最后 300 行. 工作台里看她在别处做过什么用 (2026-11-01 用户: "要在工作台中留痕"). 没开渠道返回空的."""
    from datetime import datetime
    mgr = getattr(getattr(hub, "e", None), "channels", None)
    store = getattr(mgr, "store", None)
    if store is None:
        return {"day": day, "items": []}
    when = datetime.strptime(day, "%Y-%m-%d") if day else datetime.fromtimestamp(mgr.clock())
    return {"day": f"{when:%Y-%m-%d}", "items": store.day_lines(("self", 0), when)[-300:]}


def describe(hub, s) -> str:
    """一个会话现在在做什么 (一句话; 空闲返回空串). QQ 上的她说工作台那边的进展时用 (wm/channels/wbthreads.py)."""
    if s.status == "idle":
        return ""
    try:
        children = hub.teams.list(s.id)
    except Exception:  # noqa: BLE001
        children = []
    items = _session_items(hub, s, children)
    return items[0]["detail"] if items else ""


def build(hub, briefs: list[dict] | None = None) -> dict:
    """现在在做什么: {items: [...], counts: {...}, at}. briefs 传进来就不再读一遍会话文件 (state() 里已经读过)."""
    items: list[dict] = []
    try:
        children = hub.teams.list()
    except Exception:  # noqa: BLE001
        children = []
    loaded = dict(hub.sessions)
    for s in loaded.values():
        if not s.archived and not s.deleted_at:
            items += _session_items(hub, s, children)
    # 没打开过的会话 (服务端重启以后还没点开): 只看文件里能看出来的 —— 核对没过、排着没开始的
    for b in briefs if briefs is not None else hub._all_briefs():
        if b["id"] in loaded or b.get("archived") or b.get("deleted_at"):
            continue
        base = {"session": b["id"], "title": b.get("title") or "会话", "workspace": b.get("workspace", "")}
        if b.get("check") == "needs_check" and b.get("verification", "unreviewed") == "unreviewed":
            items.append({**base, "kind": "check", "id": b["id"], "state": "needs_check", "waiting_for": "you",
                          "detail": "说做完了，但待核查（打开看原因）", "since": b.get("updated")})
        if b.get("queued"):
            items.append({**base, "kind": "session", "id": b["id"], "state": "queued", "waiting_for": "you",
                          "detail": f"还排着 {b['queued']} 条没开始（服务端重启过，打开会话再发一句接着做）", "since": b.get("updated")})
    titles = {b["id"]: b.get("title") for b in (briefs or [])} | {s.id: s.title for s in loaded.values()}
    for c in children:
        if c.get("status") not in ACTIVE:
            continue
        wait = {"waiting": "you", "queued": "queue"}.get(c.get("status"), "")
        items.append({"kind": "child", "id": c["id"], "session": c["id"], "parent": c.get("parent_id"),
                      "title": str(c.get("title") or c.get("goal") or "子任务")[:80], "state": c.get("status"),
                      "waiting_for": wait, "since": c.get("started") or c.get("created"),
                      "detail": {"queued": "排队等空位", "waiting": "子任务在等你点头", "cancelling": "正在停下"}.get(
                          c.get("status"), "在做") + f"（「{titles.get(c.get('parent_id')) or '主任务'}」派的）"})
    for e in hub.job_watch.entries():
        items.append({"kind": "job", "id": e["job"], "session": e["session"], "title": e.get("name") or e["job"],
                      "state": "running", "waiting_for": "", "since": e.get("started"),
                      "detail": f"后台作业 {e['job']} 在跑，做完会自己交回「{titles.get(e['session']) or '发起的会话'}」"})
    for row in list(getattr(getattr(hub, "terminals", None), "records", {}).values()):
        if row.get("state") == "running":
            items.append({"kind": "terminal", "id": row["id"], "session": row["session"], "title": row.get("title") or "终端",
                          "state": "running", "waiting_for": "", "since": row.get("started"),
                          "quiet_s": _quiet_s(row), "detail": "终端里在跑：" + str(row.get("command") or "")[:80] + _quiet_note(row)})
    for row in list(getattr(getattr(hub, "workspace_jobs", None), "records", {}).values()):
        if row.get("state") == "running":
            items.append({"kind": "command", "id": row["id"], "session": row["session"], "title": "你在工作区里跑的命令",
                          "state": "running", "waiting_for": "", "since": row.get("started"),
                          "detail": str(row.get("command") or "")[:80]})
    # 她在 QQ 那边接的活 (操作电脑、改进自己 / 学本事 / 写工具、等主人点头的申请): 也算"她现在在做的事"
    # (2026-11-01 用户: "QQ 安排任务、自行优化等, 要在工作台中留痕, 就像 gpt 的 dot 一样")
    mgr = getattr(getattr(hub, "e", None), "channels", None)
    try:
        items += [{**row, "via": "qq"} for row in (mgr.activity_items() if hasattr(mgr, "activity_items") else [])]
    except Exception:  # noqa: BLE001 (渠道那边读不出来不耽误工作台自己的)
        pass
    items.sort(key=lambda x: (ORDER.get(x["waiting_for"], 2), -(x.get("since") or 0)))
    # 计数按"一件事"算, 不按条数 (2026-11-07 工作台改版会话查到: 一件工作台申请会算 2 次 —— 会话一条、QQ 转来的一条;
    # 子任务的申请算 3 次 —— 再加子任务记录一条; 在做的子任务也算 2 次): 会话、子任务、带会话编号的申请都按那个会话算一件,
    # 待核查和别的照旧各算各的
    def one(x):
        if x["kind"] in ("session", "child") or (x["kind"] == "ask" and x.get("session")):
            return ("s", x.get("session") or x.get("id"))
        return (x["kind"], x.get("id"))
    counts = {"you": len({one(x) for x in items if x["waiting_for"] == "you"}),
              "working": len({one(x) for x in items if x["kind"] in ("session", "child") and x["state"] == "running"}),
              "background": sum(x["kind"] in ("job", "terminal", "command", "computer", "self_improve") for x in items),
              "needs_check": sum(x["state"] == "needs_check" for x in items)}
    return {"items": items, "counts": counts, "at": time.time()}

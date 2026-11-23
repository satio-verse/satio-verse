"""工作台的无界面跑法 (2026-11-07 底层对标 P0-5; 对照 `claude -p --output-format stream-json`、`codex exec --json`、
pi 的 print / json 模式): 给脚本、定时任务、评测用, 不开页面、不接渠道.

  python -m wm.agent.headless "把 README 里的错别字改掉" --workspace D:\\proj --mode full --jsonl
  python -m wm.agent.headless "…" --workspace D:\\proj --model "deepseek|https://api.deepseek.com|deepseek-flash" --trajectory run.jsonl

给 Python 用: `await run_task("任务", workspace="D:/proj", mode="full", on_event=...)` -> 结果 (成没成、这一轮的记录、用量、
最后那段话、轨迹). 和页面上是同一个 AgentHub (同一套工具、守门、预算、重试、压缩、核对).

- 家目录: 默认一个新的临时目录 (不读也不写你真的 ~/.worldmodel/agent: 会话、权限名单、经验、扩展都是新的), 也不发现桌面的
  技能和 MCP; `--home 目录` 用指定的, `--real-home` 用你真的那个 (会出现在工作台的会话列表里).
- 权限: --mode 默认 workspace (工作区里能改, 跑命令要问); 无界面没人点卡片, 要问的默认一律拒绝 (--ask allow 改成允许一次,
  守门拦下的照样拦). 完全授权 (unrestricted) 要自己写 --mode unrestricted.
- 模型: --model 给工作台模型列表里的编号; 不给用默认的 (环境变量 DEEPSEEK_API_KEY 的 DeepSeek, 或配置 llm.local_api 的本机模型).
- 输出: --jsonl 一行一个事件 (和页面收到的一样, 流式的 delta 也在); 不加就只打印要点. 最后一行是结果.
- 退出码: 0 做完了 (completed), 1 出错 / 没开始, 2 停了 / 到预算 / 中断, 3 参数不对.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from .. import cloud as C
from .. import ledger as L
from ..config import load_config

EXIT = {"completed": 0, "failed": 1, "cancelled": 2, "budget_exhausted": 2, "interrupted": 2}


async def run_task(prompt: str, *, workspace: str, model: str = "", mode: str = "workspace", effort: str = "",
                   home: str | Path | None = None, real_home: bool = False, max_steps: int = 0, time_s: float = 0,
                   budget_cny: float = 0, ask: str = "deny", web: bool = True, on_event=None, cfg: dict | None = None,
                   trajectory: str | Path | None = None) -> dict:
    """无界面跑一件事, 一直等到做完 (含做完后自动接着的排队项). on_event: async/普通函数, 收到每个事件 (和页面一样).
    返回 {ok, state, reason, summary, session, workspace, model, stats, run, seconds, trajectory}."""
    from . import core
    from . import extensions
    if not str(prompt or "").strip():
        raise ValueError("要交给她的事是空的")
    ws = Path(workspace).expanduser().resolve()
    if not ws.is_dir():
        raise ValueError(f"工作区不存在: {ws}")
    cfg = dict(cfg or load_config())
    agent_conf = dict(cfg.get("agent") or {})
    temp = None
    if real_home:
        agent_home = None
    else:
        if home is None:
            temp = tempfile.mkdtemp(prefix="wm-headless-")
            home = temp
        agent_home = Path(home).expanduser().resolve()
        agent_conf["extensions"] = {"settings_path": False, "skill_roots": [], "self_mcp_root": False,
                                    **(agent_conf.get("extensions") or {})}
        extensions.DEFAULT_DESKTOP_DISCOVERY = False
    agent_conf["workspace"] = str(ws)
    cfg["agent"] = agent_conf
    C.configure((cfg.get("llm") or {}).get("local_api"))
    led = L.current()
    own_ledger = led is None
    if own_ledger:  # 用你真的家目录时记进服务端那本账 (logs/billing, 账本页看得到); 临时的记在临时目录里
        from ..config import resolve_path
        where = resolve_path(cfg["trace"]["log_dir"]) / "billing" if real_home else Path(agent_home) / "ledger"
        led = L.Ledger(where, cfg.get("billing"))
        L.use(led)
    hub = core.AgentHub(cfg, engines=None, home=agent_home)
    if str(ws) not in hub.workspaces():
        hub.add_workspace(str(ws))
    if model and not any(m["id"] == model for m in hub.models()):
        raise ValueError("没有这个模型: " + model + "; 能选的: " + ", ".join(m["id"] for m in hub.models()))
    s = hub.new_session(str(ws), mode=mode, model=model)
    if effort in core.EFFORTS:
        s.effort = effort
    s.web = bool(web)
    if max_steps:
        s.max_steps = int(max_steps)
    s.time_budget_s, s.budget_cny = float(time_s or 0), float(budget_cny or 0)
    hub.save(s)

    async def on(ev):
        if ev.get("type") == "ask":  # 没人点卡片: 照 --ask 回 (守门拦下的根本到不了这里)
            hub.answer(s.id, ev["id"], "once" if ask == "allow" else "deny")
        if on_event is not None:
            got = on_event(ev)
            if asyncio.iscoroutine(got):
                await got

    hub.subscribe(s.id, on)
    t0 = time.monotonic()
    why = await hub.send(s.id, prompt)
    try:
        while why is None:
            task = s.task
            if task is not None and not task.done():
                try:
                    await asyncio.shield(task)
                except Exception:  # noqa: BLE001 (那一轮自己出错: 照样出结果)
                    pass
            await asyncio.sleep(0.2)
            if s.status == "idle" and (s.task is None or s.task.done()) and not s.queue:
                break
    except asyncio.CancelledError:
        await hub.stop(s.id)
        raise
    finally:
        await hub.teams.close()
        if own_ledger:
            L.use(None)
    summary = next((str(e.get("text") or "") for e in reversed(s.events) if e.get("type") == "assistant"), "")
    state = "failed" if why else s.run.get("state", "")
    out = {"ok": state == "completed", "state": state, "reason": why or str(s.run.get("reason") or ""), "summary": summary,
           "session": s.id, "workspace": str(ws), "model": s.model, "stats": dict(s.stats), "run": s.run,
           "seconds": round(time.monotonic() - t0, 2), "home": str(agent_home or core.HOME), "temporary_home": bool(temp),
           "trajectory": ""}
    if trajectory:
        path = Path(trajectory).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in hub.trajectory(s.id)) + "\n", encoding="utf-8")
        out["trajectory"] = str(path)
    return out


def _args(argv=None):
    ap = argparse.ArgumentParser(prog="python -m wm.agent.headless", description="无界面跑一件工作台的事")
    ap.add_argument("prompt", help="交给她的事")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--model", default="")
    ap.add_argument("--mode", default="workspace", choices=["read", "workspace", "full", "unrestricted"])
    ap.add_argument("--effort", default="", choices=["", "off", "low", "high", "max"])
    ap.add_argument("--home", default=None, help="工作台家目录 (默认新的临时目录)")
    ap.add_argument("--real-home", action="store_true", help="用你真的 ~/.worldmodel/agent")
    ap.add_argument("--max-steps", type=int, default=0)
    ap.add_argument("--time-s", type=float, default=0)
    ap.add_argument("--budget-cny", type=float, default=0)
    ap.add_argument("--ask", default="deny", choices=["deny", "allow"], help="要点头的: 一律拒绝 (默认) / 允许一次")
    ap.add_argument("--no-web", action="store_true")
    ap.add_argument("--jsonl", action="store_true", help="一行一个事件 (JSON)")
    ap.add_argument("--trajectory", default=None, help="把完整轨迹写到这个文件 (JSONL)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    try:
        a = _args(argv)
    except SystemExit as exc:
        return 3 if exc.code else 0
    with __import__("contextlib").suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")

    def show(ev):
        if a.jsonl:
            print(json.dumps(ev, ensure_ascii=False), flush=True)
        elif ev.get("type") in ("assistant", "note", "error"):
            print(f"[{ev['type']}] {str(ev.get('text') or '')[:2000]}", flush=True)
        elif ev.get("type") == "tool":
            print(f"[工具] {ev.get('label') or ev.get('name')} {str(ev.get('args') or '')[:200]} ({ev.get('state')})", flush=True)

    try:
        result = asyncio.run(run_task(a.prompt, workspace=a.workspace, model=a.model, mode=a.mode, effort=a.effort, home=a.home,
                                      real_home=a.real_home, max_steps=a.max_steps, time_s=a.time_s, budget_cny=a.budget_cny,
                                      ask=a.ask, web=not a.no_web, on_event=show, trajectory=a.trajectory))
    except ValueError as exc:
        print(json.dumps({"type": "result", "ok": False, "state": "failed", "reason": str(exc)}, ensure_ascii=False), flush=True)
        return 3
    print(json.dumps({"type": "result", **{k: v for k, v in result.items() if k != "run"}}, ensure_ascii=False), flush=True)
    return EXIT.get(result["state"], 1)


if __name__ == "__main__":
    sys.exit(main())

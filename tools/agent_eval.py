"""工作台本地评测 (2026-11-07, docs/agent-parity-20261107.md 第四节): 把 tools/agent_eval_tasks.py 的题一道道交给工作台的她做,
程序判分, 记成没成、问了模型几步、工具调用 / 报错几次、token (命中 / 没命中 / 输出)、花了多少钱、用了多久.

改前 / 改后用同一份驱动: --code-root 指向哪份代码就导入哪份的 wm (改前用 backups/agent-parity-20261107/before 快照),
所以这里只用两边都有的接口 (AgentHub.new_session / send / subscribe / answer、Session.stats / run / events).

隔离: 每题一个新的临时家目录和工作区 (在 --out 下面), 只读出厂配置 config/default.json (不读 local.json), 不发现桌面技能和 MCP,
不接渠道 / 记忆 (没有人设、没有关于你的背景, 改前改后一样), 账本也是临时的. 不碰你真的 ~/.worldmodel.
权限用"完全授权" (评测没人点卡片; 万一还是弹了申请, 自动点"允许一次"), 每题有费用 / 步数 / 时间上限, 全部加起来到 --budget-cny 就停.

  python tools/agent_eval.py --label before --code-root backups/agent-parity-20261107/before --model deepseek-flash
  python tools/agent_eval.py --label after --model deepseek-flash
  python tools/agent_eval.py --label after-local --model local --local-url http://127.0.0.1:8788/v1 --local-model Qwen3.5-2B-Q4_K_M
结果: <out>/<label>/results.jsonl (一题一行) + summary.json + traj/<题>.json (那一题完整的会话: 每一步的请求历史、工具结果、事件)."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import shutil
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

PROJECT = Path(__file__).resolve().parents[1]
RG = PROJECT / "desktop" / "third_party" / "dsh" / "node_modules" / "@vscode" / "ripgrep-win32-x64" / "bin" / "rg.exe"


def _args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--label", required=True, help="这一遍叫什么 (before / after / after-local …)")
    ap.add_argument("--code-root", default=str(PROJECT), help="导入哪份代码的 wm (默认当前项目)")
    ap.add_argument("--model", default="deepseek-flash", help="deepseek-flash / deepseek-v4-pro / local")
    ap.add_argument("--local-url", default="http://127.0.0.1:8788/v1")
    ap.add_argument("--local-model", default="")
    ap.add_argument("--tasks", default="all", help="逗号分隔的题号, 默认全部")
    ap.add_argument("--skip-web", action="store_true", help="跳过要上网的题")
    ap.add_argument("--effort", default="max", choices=["off", "low", "high", "max"])
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--time-s", type=float, default=900)
    ap.add_argument("--task-budget-cny", type=float, default=0.6, help="每题最多花多少")
    ap.add_argument("--budget-cny", type=float, default=2.5, help="这一遍一共最多花多少, 到了就不再开新题")
    ap.add_argument("--out", default=str(PROJECT / "logs" / "agent-eval"))
    ap.add_argument("--faults", default="", help="故障注入: 直接回 503 的比例,流到一半断开的比例 (比如 0.25,0.1); 只对 DeepSeek")
    ap.add_argument("--seed", type=int, default=20261107, help="故障注入的随机种子 (改前改后用同一个)")
    return ap.parse_args(argv)


class FaultProxy:
    """评测用的故障注入 (专测重试 / 备用模型): 本机起一个转发, 把工作台的请求转给 DeepSeek, 按固定种子随机让一部分直接回 503、
    一部分流到一半断开. 改前改后遇到的故障按同一串随机数来 (请求数不同以后会错开, 比例一样)."""

    def __init__(self, upstream: str, p_err: float, p_mid: float, seed: int):
        self.upstream = upstream.rstrip("/")
        self.p_err, self.p_mid = p_err, p_mid
        self.rng = random.Random(seed)
        self.stats = {"requests": 0, "injected_503": 0, "injected_cut": 0}
        self.http: aiohttp.ClientSession | None = None
        self.runner: web.AppRunner | None = None
        self.base = ""

    async def start(self) -> str:
        import logging
        logging.getLogger("aiohttp.server").setLevel(logging.CRITICAL)  # 故意断开连接时 aiohttp 自己打的堆栈不要
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=300))
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_post("/chat/completions", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self.base

    async def handle(self, request: web.Request):
        body = await request.read()
        self.stats["requests"] += 1
        roll = self.rng.random()
        if roll < self.p_err:
            self.stats["injected_503"] += 1
            return web.json_response({"error": {"message": "injected by agent_eval: server busy", "type": "server_error"}}, status=503)
        cut = roll < self.p_err + self.p_mid
        headers = {"Authorization": request.headers.get("Authorization", ""), "Content-Type": "application/json"}
        async with self.http.post(self.upstream + "/chat/completions", data=body, headers=headers) as up:
            resp = web.StreamResponse(status=up.status, headers={"Content-Type": up.headers.get("Content-Type", "application/json")})
            await resp.prepare(request)
            n = 0
            async for chunk in up.content.iter_any():
                await resp.write(chunk)
                n += 1
                if cut and n >= 3:
                    self.stats["injected_cut"] += 1
                    request.transport.close()
                    return resp
            with contextlib.suppress(ConnectionError, aiohttp.ClientConnectionResetError):
                await resp.write_eof()
            return resp

    async def close(self):
        if self.runner is not None:
            await self.runner.cleanup()
        if self.http is not None:
            await self.http.close()


def _summary(rows: list[dict]) -> dict:
    done = [r for r in rows if not r.get("skipped")]
    n = len(done) or 1
    out = {"tasks": len(done), "ok": sum(bool(r["ok"]) for r in done),
           "success_rate": round(sum(bool(r["ok"]) for r in done) / n, 3),
           "steps": sum(r["steps"] for r in done), "tool_calls": sum(r["tool_calls"] for r in done),
           "tool_errors": sum(r["tool_errors"] for r in done), "hit": sum(r["hit"] for r in done),
           "miss": sum(r["miss"] for r in done), "out": sum(r["out"] for r in done),
           "cost_cny": round(sum(r["cost"] for r in done), 4), "seconds": round(sum(r["seconds"] for r in done), 1)}
    kinds: dict[str, dict] = {}
    for r in done:
        k = kinds.setdefault(r["kind"], {"tasks": 0, "ok": 0})
        k["tasks"] += 1
        k["ok"] += bool(r["ok"])
    out["by_kind"] = kinds
    return out


async def _run_one(task: dict, a, mods: dict, model_id: str, base: Path, spent: float) -> dict:
    core, L = mods["core"], mods["L"]
    home = base / "homes" / task["id"]
    ws = base / "ws" / task["id"]
    for p in (home, ws):
        if p.exists():
            shutil.rmtree(p)
    ws.mkdir(parents=True)
    task["setup"](ws)
    cfg = json.loads((Path(a.code_root) / "config" / "default.json").read_text("utf-8"))
    cfg["agent"] = {**(cfg.get("agent") or {}), "workspace": str(ws), "home": str(home),
                    "extensions": {"settings_path": False, "skill_roots": [], "self_mcp_root": False}}
    cfg.setdefault("memory", {})["vault"] = ""
    L.use(L.Ledger(home / "ledger", cfg.get("billing")))
    keep = None
    if mods.get("proxy") is not None:  # 故障注入: 账本里登记一个指向转发的 DeepSeek 模型 (工作台拿它的 key 另建一个)
        C = mods["C"]
        keep = C.CloudLLM(C.parse_model({"provider": "deepseek", "api_key": os.environ["DEEPSEEK_API_KEY"], "model": a.model,
                                         "base_url": mods["proxy"].base}))
    hub = core.AgentHub(cfg, engines=None, home=home)
    hub._set_prefs(default_mode="unrestricted")
    ids = [m["id"] for m in hub.models()]
    if model_id not in ids:
        raise SystemExit(f"没有这个模型: {model_id}; 能选的: {ids}")
    s = hub.new_session(str(ws), mode="unrestricted", model=model_id)
    s.effort, s.web = a.effort, True
    s.max_steps, s.time_budget_s = a.max_steps, a.time_s
    s.budget_cny = max(0.05, min(a.task_budget_cny, a.budget_cny - spent))
    hub.save(s)
    events: list[dict] = []

    async def on_event(ev):
        events.append(ev)
        if ev.get("type") == "ask":  # 评测没人点卡片: 允许一次 (完全授权下本来不该弹)
            hub.answer(s.id, ev["id"], "once")

    hub.subscribe(s.id, on_event)
    t0 = time.monotonic()
    why = await hub.send(s.id, task["prompt"])
    deadline = t0 + a.time_s + 120
    while why is None:
        running = s.task
        if running is not None and not running.done():
            try:
                await asyncio.wait_for(asyncio.shield(running), timeout=max(1.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                await hub.stop(s.id)
                break
            except Exception:  # noqa: BLE001 (那一轮自己出错: 照样判分)
                pass
        await asyncio.sleep(0.3)
        if s.status == "idle" and (s.task is None or s.task.done()) and not s.queue:
            break
        if time.monotonic() > deadline:
            await hub.stop(s.id)
            break
    seconds = time.monotonic() - t0
    try:
        ok, detail = await asyncio.to_thread(task["check"], ws)
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"判分出错: {type(exc).__name__}: {exc}"[:200]
    st, run = s.stats, s.run
    tools = [e for e in events if e.get("type") == "tool"]
    rec = {"task": task["id"], "kind": task["kind"], "title": task["title"], "label": a.label, "model": model_id,
           "effort": a.effort, "ok": bool(ok), "detail": detail, "why_not_started": why or "",
           "state": run.get("state", ""), "reason": str(run.get("reason") or "")[:300],
           "steps": int(st.get("steps", 0)), "tool_calls": len(tools),
           "tool_errors": sum(1 for e in events if e.get("type") == "tool_result" and e.get("ok") is False),
           "denied": sum(1 for e in tools if e.get("state") == "denied"),
           "hit": int(st.get("hit", 0)), "miss": int(st.get("miss", 0)), "out": int(st.get("out", 0)),
           "cost": float(st.get("cost", 0.0)), "unpriced": int(st.get("unpriced", 0)), "seconds": round(seconds, 1),
           "verdict": (run.get("check") or {}).get("verdict", ""),
           "errors": [str(e.get("text") or "")[:300] for e in events if e.get("type") == "error"],
           "notes": [str(e.get("text") or "")[:160] for e in events if e.get("type") == "note"][:12],
           "tools": [e.get("name") for e in tools]}
    traj = base / "traj"
    traj.mkdir(parents=True, exist_ok=True)
    src = home / "sessions" / f"{s.id}.json"
    if src.is_file():
        shutil.copyfile(src, traj / f"{task['id']}.json")
    run_log = s.run
    rec["retries"] = int(run_log.get("retries", 0)) if isinstance(run_log, dict) else 0
    rec["fallback"] = (run_log.get("fallback") or {}).get("to", "") if isinstance(run_log, dict) else ""
    rec["compactions"] = int(run_log.get("compactions", 0)) if isinstance(run_log, dict) else 0
    rec["nudged"] = bool(run_log.get("skill_nudged")) if isinstance(run_log, dict) else False
    rec["repeats"] = int(run_log.get("repeats", 0)) if isinstance(run_log, dict) else 0
    if keep is not None:
        await keep.close()
    await asyncio.sleep(0.2)
    return rec


async def main(argv=None) -> int:
    a = _args(argv)
    a.code_root = str(Path(a.code_root).resolve())
    sys.path.insert(0, a.code_root)
    sys.path.insert(1, str(PROJECT / "tools"))
    import agent_eval_tasks as TK
    from wm import cloud as C
    from wm import ledger as L
    from wm.agent import core
    from wm.agent import extensions
    from wm.agent import tools as T
    extensions.DEFAULT_DESKTOP_DISCOVERY = False
    if RG.is_file():  # 改前的快照里没有 desktop/: 两边都用项目里那份 ripgrep, 别让"搜得慢"混进比较
        T.RG = str(RG)
    mods = {"core": core, "L": L, "C": C}
    if a.model == "local":
        C.configure({"base_url": a.local_url, "model": a.local_model or "", "thinking": a.effort != "off"})
        spec = C.local_spec()
        if spec is None:
            raise SystemExit("本机模型没配好 (--local-url)")
        model_id = f"{spec['provider']}|{spec['base_url']}|{a.local_model or spec['model']}"
    else:
        if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
            raise SystemExit("环境变量里没有 DEEPSEEK_API_KEY")
        model_id = f"deepseek|{C.BASE_URLS['deepseek']}|{a.model}"
        if a.faults:
            p_err, p_mid = (float(x) for x in a.faults.split(","))
            mods["proxy"] = FaultProxy(C.BASE_URLS["deepseek"], p_err, p_mid, a.seed)
            base_url = await mods["proxy"].start()
            model_id = f"deepseek|{base_url}|{a.model}"
    wanted = TK.TASKS if a.tasks == "all" else [TK.by_id()[t] for t in a.tasks.split(",")]
    base = Path(a.out) / a.label
    base.mkdir(parents=True, exist_ok=True)
    results = base / "results.jsonl"
    rows, spent = [], 0.0
    print(f"[评测 {a.label}] 代码 {a.code_root} · 模型 {model_id} · 思考 {a.effort} · {len(wanted)} 题 · 上限 {a.budget_cny} 元", flush=True)
    for task in wanted:
        if a.skip_web and task.get("needs_web"):
            continue
        if spent >= a.budget_cny:
            rows.append({"task": task["id"], "skipped": True, "why": "到了总预算"})
            print(f"  - {task['id']}: 跳过 (到了总预算 {a.budget_cny} 元)", flush=True)
            continue
        rec = await _run_one(task, a, mods, model_id, base, spent)
        spent += rec["cost"]
        rows.append(rec)
        with results.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  - {task['id']}: {'成' if rec['ok'] else '没成'} · {rec['state']} · {rec['steps']} 步 · 工具 {rec['tool_calls']} 次"
              f" (错 {rec['tool_errors']}) · {rec['cost']:.4f} 元 · {rec['seconds']:.0f} 秒 · {rec['detail'][:80]}", flush=True)
    summary = {"label": a.label, "code_root": a.code_root, "model": model_id, "effort": a.effort,
               "at": time.strftime("%Y-%m-%d %H:%M:%S"), **_summary(rows)}
    if mods.get("proxy") is not None:
        summary["faults"] = {"p_503": mods["proxy"].p_err, "p_cut": mods["proxy"].p_mid, "seed": a.seed, **mods["proxy"].stats}
        await mods["proxy"].close()
    (base / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf-8")
    print("[汇总] " + json.dumps({k: v for k, v in summary.items() if k != "by_kind"}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

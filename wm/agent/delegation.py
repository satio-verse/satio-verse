"""Bounded independent-context agents with durable, inspectable task records.

AgentHub owns model/tool execution and checks budgets before each request. This
module owns scheduling, permission ceilings, material selection and result
collection. A finished model turn is reported as completed execution, never as
an independently verified successful outcome.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import math
import secrets
import time
from pathlib import Path

from .worktrees import GitWorktreeManager

MODE_ORDER = {"read": 0, "workspace": 1, "full": 2, "unrestricted": 3}
EFFORTS = {"off", "low", "high", "max"}  # 和 core.EFFORTS 一样 (这里不导入 core, 免得互相导入)
ACTIVE = {"queued", "running", "waiting", "cancelling"}
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


def _number(value, name: str, maximum: float) -> float:
    try:
        n = float(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是非负数字") from exc
    if not math.isfinite(n) or n < 0 or n > maximum:
        raise ValueError(f"{name} 超出范围")
    return n


class TeamManager:
    @staticmethod
    def mode_rank(mode: str) -> int:
        return MODE_ORDER.get(mode, 0)

    def __init__(self, hub, home: Path | None = None, *, max_parallel: int = 3, max_depth: int = 1):
        self.hub = hub
        self.home = Path(home or Path(hub.home) / "teams")
        self.max_parallel = max(1, min(32, int(max_parallel)))
        self.max_depth = max(1, min(8, int(max_depth)))
        try:
            config = json.loads((self.home / 'config.json').read_text('utf-8'))
            self.max_parallel = max(1, min(32, int(config['max_parallel'])))
            self.max_depth = max(1, min(8, int(config['max_depth'])))
        except FileNotFoundError:
            pass
        self.worktrees = GitWorktreeManager(self.home / "worktrees")
        self._slots = asyncio.Semaphore(self.max_parallel)
        self._slot_owners = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self._closing = False
        self._records: dict[str, dict] = self._load()
        changed = False
        for record in self._records.values():
            if record.get("status") in ACTIVE:
                record.update(status="interrupted", error="服务重启，未自动重放子任务；请检查已有成果后重新分配", updated=time.time())
                changed = True
        if changed:
            self._save()

    def _load(self) -> dict:
        try:
            data = json.loads((self.home / "tasks.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise ValueError("子代理记录无法读取，请检查 tasks.json") from exc
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), dict):
            raise ValueError("子代理记录格式无效")
        return {k: v for k, v in data["tasks"].items() if isinstance(v, dict) and v.get("id") == k}

    def _save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        path = self.home / "tasks.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "tasks": self._records}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def record_for(self, sid: str) -> dict | None:
        record = self._records.get(sid)
        return copy.deepcopy(record) if record else None

    def configuration(self) -> dict:
        return {'max_parallel': self.max_parallel, 'max_depth': self.max_depth, 'queue_when_busy': True}

    def configure(self, values: dict) -> dict:
        if not isinstance(values, dict):
            raise ValueError('调度设置必须是对象')
        if any(r['status'] in ACTIVE for r in self._records.values()):
            raise ValueError('请等子任务完成或停止后再调整调度')
        parallel, depth = values.get('max_parallel', self.max_parallel), values.get('max_depth', self.max_depth)
        if type(parallel) is not int or not 1 <= parallel <= 32 or type(depth) is not int or not 1 <= depth <= 8:
            raise ValueError('并发范围1至32，嵌套层级范围1至8')
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.home / 'config.tmp'
        tmp.write_text(json.dumps({'max_parallel': parallel, 'max_depth': depth}), 'utf-8')
        tmp.replace(self.home / 'config.json')
        self.max_parallel, self.max_depth = parallel, depth
        self._slots = asyncio.Semaphore(parallel)
        return self.configuration()

    def descendants(self, parent_id: str) -> set[str]:
        found, frontier = set(), {parent_id}
        while frontier:
            children = {r['id'] for r in self._records.values() if r['parent_id'] in frontier} - found - {parent_id}
            found.update(children)
            frontier = children
        return found

    def tree(self, parent_id: str | None = None) -> list[dict]:
        rows = self.list()
        by_id = {r['id']: {**r, 'children': []} for r in rows}
        roots = []
        for row in by_id.values():
            if row['parent_id'] in by_id:
                by_id[row['parent_id']]['children'].append(row)
            else:
                roots.append(row)
        return [row for row in by_id.values() if row['parent_id'] == parent_id] if parent_id else roots

    async def _acquire_slot(self, sid):
        await self._slots.acquire()
        self._slot_owners.add(sid)

    def _release_slot(self, sid):
        if sid in self._slot_owners:
            self._slot_owners.remove(sid)
            self._slots.release()

    def effective_mode(self, session) -> str:
        requested = getattr(session, "mode", "read")
        record = self._records.get(session.id)
        ceilings = [requested, getattr(session, "permission_ceiling", "") or requested]
        if getattr(session, 'preset', '') == 'plan':
            ceilings.append('read')
        if record:
            ceilings.append(record.get("mode", "read"))
            parent = self.hub.get(record["parent_id"])
            if parent:
                ceilings.append(self.effective_mode(parent))
        return min(ceilings, key=lambda mode: MODE_ORDER.get(mode, 0))

    def _refresh(self, record: dict) -> dict:
        child = self.hub.sessions.get(record["id"])
        if child:
            record["cost"] = max(0.0, float((getattr(child, "stats", {}) or {}).get("cost", 0)))
            record["steps"] = int((getattr(child, "stats", {}) or {}).get("steps", 0))
            record["unpriced"] = int((getattr(child, "stats", {}) or {}).get("unpriced", 0))
            if record.get("status") in {"running", "waiting"}:
                record["status"] = "waiting" if child.status == "waiting" else "running"
        return record

    def list(self, parent_id: str | None = None) -> list[dict]:
        rows = [self._refresh(r) for r in self._records.values() if parent_id is None or r["parent_id"] == parent_id]
        return copy.deepcopy(sorted(rows, key=lambda row: row["created"]))

    def inspect(self, child_id: str, parent_id: str | None = None) -> dict:
        record = self._records.get(child_id)
        if not record or (parent_id is not None and record["parent_id"] != parent_id):
            raise ValueError("没有这个父任务的子代理")
        return copy.deepcopy(self._refresh(record))

    def spent(self, parent_id: str, since: float = 0) -> float:
        descendants = self.descendants(parent_id)
        return round(sum(float(self._refresh(r).get("cost", 0)) for r in self._records.values()
                         if r['id'] in descendants and r["created"] >= since), 6)

    def remaining_budget(self, parent) -> float | None:
        limit = float(getattr(parent, "budget_cny", 0) or 0)
        if not limit:
            return None
        run = getattr(parent, "run", {}) or {}
        own = max(0.0, float(parent.stats.get("cost", 0)) - float(run.get("cost_start", parent.stats.get("cost", 0))))
        return max(0.0, limit - own - self.spent(parent.id, since=float(run.get("started", 0))))

    async def delegate(self, parent, tasks: list[dict]) -> list[dict]:
        if self._closing:
            raise ValueError("工作台正在关闭，不能再创建子任务")
        parent_record = self._records.get(parent.id) or {}
        if max(int(getattr(parent, "depth", 0)), int(parent_record.get("depth", 0))) >= self.max_depth:
            raise ValueError("达到当前嵌套层级，不能继续创建子代理；可在调度设置中调整")
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 32:
            raise ValueError("每次分配 1 到 32 个独立子任务")
        if getattr(parent, "stop_event", None) and parent.stop_event.is_set():
            raise ValueError("主任务已经停止")
        mode = self.effective_mode(parent)
        available = self.remaining_budget(parent)
        if available is not None and available <= 0:
            raise ValueError("主任务预算已经用完，不能再创建子代理")
        # Existing pending children reserve their unused allocations. Multiple
        # batches must not each receive the parent's entire remaining budget.
        reserved = sum(max(0, float(r.get("budget_cny", 0)) - float(self._refresh(r).get("cost", 0)) - self.spent(r['id']))
                       for r in self._records.values() if r["parent_id"] == parent.id and r["status"] in ACTIVE)
        share = None if available is None else max(0, available - reserved) / len(tasks)
        if share is not None and share <= 0:
            raise ValueError("剩余预算已分配给运行中的子任务，请先等待")
        parent_time = float(getattr(parent, "time_budget_s", 0) or 0)
        started = float((getattr(parent, "run", {}) or {}).get("started", time.time()))
        time_left = max(0, parent_time - (time.time() - started)) if parent_time else None
        if time_left is not None and time_left <= 0:
            raise ValueError("主任务时间预算已经用完")
        materials = {m.get("id"): m for m in getattr(parent, "materials", []) if isinstance(m, dict)}
        prepared = []
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError("子任务必须是对象")
            goal = str(task.get("goal") or "").strip()
            context = str(task.get("context") or "").strip()
            if not goal or len(goal) > 4000 or len(context) > 16000:
                raise ValueError("子任务需有明确目标，目标最多 4000 字、补充上下文最多 16000 字")
            workspace_mode = task.get('workspace_mode') or ('worktree' if task.get('worktree') is True else 'read')
            if workspace_mode not in {'read', 'shared', 'worktree'}:
                raise ValueError('工作区方式应为 read、shared 或 worktree')
            isolated = workspace_mode == 'worktree'
            child_mode = str(task.get("mode") or (mode if workspace_mode != 'read' else "read"))
            if child_mode not in MODE_ORDER or MODE_ORDER[child_mode] > MODE_ORDER.get(mode, 0):
                raise ValueError("子任务不能获得高于主任务的权限")
            if child_mode != "read" and workspace_mode == 'read':
                raise ValueError("修改型子任务请使用 worktree 隔离，避免并行覆盖同一目录")
            ids = task.get("context_ids") or []
            if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(i, str) or i not in materials for i in ids):
                raise ValueError("子任务只能选择父任务已经附加的材料")
            # 子任务可以单独选预设 / 模型 / 思考强度 (2026-11-07 底层对标 P1-11; CC 的自定义子 agent、Codex 的 .codex/agents):
            # 不填照父任务的. 模型可以只写名字 (deepseek-flash), 对得上唯一一个就用它
            preset = str(task.get("preset") or "").strip()
            if preset and preset not in {p["id"] for p in self.hub.presets.list()}:
                raise ValueError(f"没有叫 {preset} 的 Agent 预设")
            model = str(task.get("model") or "").strip()
            if model:
                model_ids = [m["id"] for m in self.hub.models()]
                if model not in model_ids:
                    near = [i for i in model_ids if i.endswith("|" + model)]
                    if len(near) != 1:
                        raise ValueError(f"子任务的模型 {model} 不在能选的模型里"
                                         + (f" (能选: {', '.join(model_ids[:8])})" if model_ids else ""))
                    model = near[0]
            effort = str(task.get("effort") or "").strip()
            if effort and effort not in EFFORTS:
                raise ValueError("思考强度只能是 off / low / high / max")
            budget = _number(task.get("budget_cny"), "子任务预算", 1000000)
            if share is not None:
                budget = min(budget, share) if budget else share
            duration = _number(task.get("time_budget_s"), "子任务时间", 86400)
            if time_left is not None:
                duration = min(duration, time_left) if duration else time_left
            prepared.append({"goal": goal, "context": context, "worktree": isolated, "workspace_mode": workspace_mode, "mode": child_mode,
                             "context_ids": list(dict.fromkeys(ids)), "budget_cny": budget,
                             "time_budget_s": duration, "preset": preset, "model": model, "effort": effort})
        created = []
        for spec in prepared:
            sid = "sub-" + time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(6)
            child = type(parent)(sid, parent.workspace, title=spec["goal"].splitlines()[0][:40],
                                 mode=spec["mode"], preset=spec["preset"] or parent.preset, model=spec["model"] or parent.model)
            child.effort, child.web = spec["effort"] or parent.effort, parent.web
            child.parent_id, child.depth = parent.id, int(getattr(parent, "depth", 0)) + 1
            child.permission_ceiling = spec["mode"]
            child.budget_cny, child.time_budget_s = spec["budget_cny"], spec["time_budget_s"]
            child.max_steps = int(getattr(parent, 'max_steps', 40))
            child.materials = [copy.deepcopy(materials[i]) for i in spec["context_ids"]]
            child.status = "queued"
            self.hub.sessions[sid] = child
            self.hub.save(child)
            now = time.time()
            record = {"id": sid, "parent_id": parent.id, "title": child.title, "depth": child.depth,
                      "status": "queued", "created": now, "updated": now, "cost": 0.0, "steps": 0,
                      "result": "", "error": "", "workspace": parent.workspace, "worktree_id": "",
                      "verified": False, **spec}
            self._records[sid] = record
            created.append(record)
        self._save()
        for record in created:
            task = asyncio.create_task(self._execute(record["id"]), name="wm-subagent-" + record["id"])
            self._tasks[record["id"]] = task
            task.add_done_callback(lambda _task, sid=record["id"]: self._tasks.pop(sid, None))
        return copy.deepcopy(created)

    async def status_changed(self, sid: str) -> None:
        """子任务在做 / 等你点头之间变了 (core._ask 调): 告诉父会话, 对话里那张子任务卡跟着变 (以前只在开始、结束、取消时发,
        转去等你点头时卡片一直写"运行中"; 工作台改版会话 2026-11-02 提的)."""
        record = self._records.get(sid)
        if not record or record.get("status") not in ACTIVE:
            return
        before = record.get("status")
        self._refresh(record)
        if record.get("status") != before:
            record["updated"] = time.time()
            self._save()
            await self._emit(record)

    async def _emit(self, record: dict) -> None:
        parent = self.hub.get(record["parent_id"])
        if parent:
            await self.hub.emit(parent, {"type": "subagent", "child": copy.deepcopy(record)})
            if record.get('status') in TERMINAL:
                if parent.status == 'idle':
                    parent.unread = True
                self.hub.save(parent)

    async def _execute(self, sid: str) -> None:
        record = self._records[sid]
        child = self.hub.sessions[sid]
        preparing = None
        try:
            await self._acquire_slot(sid)
            if True:
                if record["status"] == "cancelled":
                    return
                parent = self.hub.get(record["parent_id"])
                if parent is None or parent.stop_event.is_set():
                    record.update(status="cancelled", error="主任务已停止")
                    return
                remaining = self.remaining_budget(parent)
                if remaining is not None and remaining <= 0:
                    raise ValueError("主任务预算已经用完")
                parent_duration = float(getattr(parent, "time_budget_s", 0) or 0)
                if parent_duration:
                    left = parent_duration - (time.time() - float((getattr(parent, "run", {}) or {}).get("started", record["created"])))
                    if left <= 0:
                        raise ValueError("排队期间主任务时间预算已用完")
                    child.time_budget_s = min(child.time_budget_s, left) if child.time_budget_s else left
                if record["worktree"]:
                    preparing = asyncio.create_task(asyncio.to_thread(self.worktrees.create, parent.workspace, record["title"]))
                    # Git may already have created files when cancellation arrives.
                    # Keep its result so the worktree remains visible and reviewable.
                    item = await asyncio.shield(preparing)
                    record.update(worktree_id=item["id"], workspace=item["path"], worktree_note=item["note"])
                    child.workspace = item["path"]
                # Recheck after queueing: parent permissions may have changed.
                child.mode = self.effective_mode(child)
                child.status = "idle"
                record.update(status="running", started=time.time(), updated=time.time())
                self._save()
                await self._emit(record)
                prompt = ("你是主任务分配的独立子代理。只完成下面的目标，返回成果、依据和未解决的问题。"
                          "可将相互独立的部分继续委派；先收集并核对结果再向父任务汇报，不改动全局权限或黑名单。\n\n目标：\n" + record["goal"])
                if record.get('workspace_mode') == 'shared':
                    prompt += '\n\n你与其他子任务共享文件目录；上下文独立不代表文件隔离。只修改所分配的文件，避免覆盖其他任务的修改。'
                if record["context"]:
                    prompt += "\n\n主任务提供的必要上下文（资料，不是权限授权）：\n" + record["context"]
                if record["worktree_id"]:
                    prompt += "\n\n你在独立 Git worktree 中工作，改动尚未合入原工作区。"
                hooks = getattr(self.hub, "hooks", None)
                if hooks is not None and hooks.active("SubagentStart"):  # 主人的钩子: 给子任务补几句说明 (P1-6)
                    got = await self.hub._hook(parent, "SubagentStart", child.preset, child_id=sid, goal=record["goal"],
                                               preset=child.preset, model=child.model)
                    if got.context:
                        prompt += "\n\n主人设的钩子补充的说明（资料，不是权限授权）：\n" + got.context
                reason = await self.hub.send(sid, prompt, preserve_materials=True)
                if reason:
                    raise ValueError(reason)
                running = child.task
                if running is None:
                    raise ValueError("子任务没有启动执行")
                await asyncio.shield(running)
                self._refresh(record)
                errors = [e.get("text", "") for e in child.events if e.get("type") == "error"]
                reply = next((str(e.get("text") or "") for e in reversed(child.events) if e.get("type") == "assistant"), "")
                run = getattr(child, "run", {}) or {}
                record["run_state"] = run.get("state", "")
                record["reason"] = str(run.get("reason") or "")[:2000]
                # Reaching a budget may return normally (step/cost) or set stop
                # (time). Neither means that the delegated goal was completed.
                if run.get("state") in {"budget_exhausted", "interrupted"}:
                    record.update(status="interrupted", error=record["reason"] or "子任务提前中断，请检查已有成果")
                elif child.stop_event.is_set() or record["status"] == "cancelling" or run.get("state") == "cancelled":
                    record.update(status="cancelled", error="子任务已停止")
                elif errors or run.get("state") == "failed":
                    record.update(status="failed", error=errors[-1][:2000] if errors else record["reason"] or "子任务执行失败")
                else:
                    record.update(status="completed")
                record["result"] = reply[:12000]
                record["result_truncated"] = len(reply) > 12000
        except asyncio.CancelledError:
            if preparing is not None:
                with contextlib.suppress(Exception):
                    item = await asyncio.shield(preparing)
                    record.update(worktree_id=item["id"], workspace=item["path"], worktree_note=item["note"])
                    child.workspace = item["path"]
            child.stop_event.set()
            await self.hub.stop(sid)
            if child.task and not child.task.done():
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(child.task), timeout=5)
                if not child.task.done():
                    child.task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await child.task
            record.update(status="cancelled", error="子任务已停止")
        except Exception as exc:
            record.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:1500]}")
        finally:
            self._release_slot(sid)
            child.status = "idle"
            record["updated"] = time.time()
            self._refresh(record)
            self.hub.save(child)
            self._save()
            with contextlib.suppress(Exception):
                await self._emit(record)
            hooks = getattr(self.hub, "hooks", None)
            if hooks is not None and hooks.active("SubagentStop"):  # 主人的钩子: 子任务结束了 (只通知)
                with contextlib.suppress(Exception):
                    if (parent := self.hub.get(record["parent_id"])) is not None:
                        await self.hub._hook(parent, "SubagentStop", child.preset, child_id=sid, status=record.get("status"),
                                             result=str(record.get("result") or "")[:4000], error=str(record.get("error") or "")[:1000])

    async def wait(self, parent_id: str, child_ids: list[str] | None = None, timeout_s: float = 30) -> dict:
        ids = child_ids if child_ids is not None else [r["id"] for r in self.list(parent_id)]
        if not isinstance(ids, list) or any(not isinstance(sid, str) for sid in ids):
            raise ValueError("子代理编号应为列表")
        for sid in ids:
            self.inspect(sid, parent_id)
        pending = [self._tasks[sid] for sid in ids if sid in self._tasks and not self._tasks[sid].done()]
        delay = _number(timeout_s, "等待时间", 60)
        if pending and delay:
            # An orchestrating child must not occupy a worker slot while its
            # own children wait in the same queue (including max_parallel=1).
            held = parent_id in self._slot_owners
            self._release_slot(parent_id)
            try:
                await asyncio.wait(pending, timeout=delay, return_when=asyncio.ALL_COMPLETED)
            except BaseException:
                raise
            else:
                if held:
                    await self._acquire_slot(parent_id)
        records = [self.inspect(sid, parent_id) for sid in ids]
        return {"children": records, "complete": all(r["status"] in TERMINAL for r in records),
                "cost": round(sum(r.get("cost", 0) for r in records), 6)}

    async def cancel(self, parent_id: str, child_id: str | None = None) -> list[dict]:
        rows = [self.inspect(child_id, parent_id)] if child_id else self.list(parent_id)
        tasks = []
        for row in rows:
            await self.cancel(row['id'])
            if row["status"] not in ACTIVE:
                continue
            record = self._records[row["id"]]
            child = self.hub.sessions.get(row["id"])
            if child:
                child.stop_event.set()
                await self.hub.stop(child.id)
            record.update(status="cancelling", updated=time.time())
            task = self._tasks.get(row["id"])
            if task and not task.done():
                task.cancel()
                tasks.append(task)
            else:
                record.update(status="cancelled", error="子任务已停止")
        self._save()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Cancelling a Task before its coroutine first runs does not enter its
        # finally block. Finalize those queued records here as well.
        for row in rows:
            record = self._records[row["id"]]
            if record["status"] == "cancelling":
                record.update(status="cancelled", error="子任务已停止", updated=time.time())
                child = self.hub.sessions.get(row["id"])
                if child:
                    child.status = "idle"
                    self.hub.save(child)
                await self._emit(record)
        self._save()
        return self.list(parent_id)

    async def close(self) -> None:
        self._closing = True
        for parent_id in {r["parent_id"] for r in self._records.values() if r["status"] in ACTIVE}:
            await self.cancel(parent_id)

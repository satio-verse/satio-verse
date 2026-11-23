"""Real Hub/Team lifecycle with deterministic offline model streams and temp storage."""
import asyncio
from copy import deepcopy
import json
import time

import pytest

from wm import ledger as L
from wm.agent.core import AgentHub, Session


def reply(text="", *calls):
    return {"content": text, "reasoning": "", "tool_calls": [
        {"id": f"call-{n}", "name": name, "args": json.dumps(args, ensure_ascii=False)}
        for n, (name, args) in enumerate(calls)]}


class OfflineLedger:
    def price(self, _provider, _model, usage):
        return {"hit": 0, "miss": 1, "out": 1, "cost": usage["test_cost"]}


class FakeCloud:
    provider, model = "deepseek", "offline-test"

    def __init__(self, session, handler):
        self.session, self.handler = session, handler
        self.requests = []
        self.closed = False
        self.cancelled = False

    async def chat(self, messages, tools, **kwargs):
        self.requests.append(deepcopy({"messages": messages, "tools": tools}))
        try:
            result, fee = await self.handler(self.session, len(self.requests), messages)
            kwargs["meta"]["usage"] = {"test_cost": fee}
            yield "done", result
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def close(self):
        self.closed = True


def setup_hub(tmp_path, monkeypatch, handler):
    monkeypatch.setattr(L, "current", lambda: OfflineLedger())
    hub = AgentHub({}, home=tmp_path / "agent")
    parent = Session("parent", str(tmp_path / "project"), mode="full", model="offline-test")
    (tmp_path / "project").mkdir()
    hub.sessions[parent.id] = parent
    models = {}

    def model(session):
        models.setdefault(session.id, FakeCloud(session, handler))
        return models[session.id]

    monkeypatch.setattr(hub, "_llm", model)
    return hub, parent, models


def test_studio_cancellation_is_propagated_and_reported(tmp_path, monkeypatch):
    async def main():
        hub, parent, _ = setup_hub(tmp_path, monkeypatch, None)
        started = asyncio.Event()
        finalized = asyncio.Event()

        async def blocked(*_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finalized.set()

        monkeypatch.setattr(hub.studio, "call", blocked)
        task = asyncio.create_task(hub.studio.execute(parent, "cancel-me", "task_plan", {"steps": []}))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finalized.is_set()
        result = next(e for e in parent.events if e["type"] == "tool_result")
        assert result["ok"] is False and "取消" in result["text"]
    asyncio.run(main())


def test_studio_downgrade_between_decision_and_call_does_not_execute(tmp_path, monkeypatch):
    async def main():
        hub, parent, _ = setup_hub(tmp_path, monkeypatch, None)
        parent.mode = "unrestricted"
        executed = []
        original_emit = hub.emit

        async def downgrade(session, event, **kwargs):
            await original_emit(session, event, **kwargs)
            if event["type"] == "tool":
                session.mode = "full"

        async def side_effect(*_):
            executed.append(True)
            return {"enabled": True}

        monkeypatch.setattr(hub, "emit", downgrade)
        monkeypatch.setattr(hub.studio, "call", side_effect)
        out, error = await hub.studio.execute(parent, "change-mode", "extension_enable", {"id": "fake", "enabled": True})
        assert error and "权限发生变化" in out
        assert not executed and not parent.asks
    asyncio.run(main())


def test_stop_ends_a_silent_model_stream_and_closes_it(tmp_path, monkeypatch):
    async def main():
        started = asyncio.Event()

        async def handler(*_):
            started.set()
            await asyncio.Event().wait()

        hub, parent, models = setup_hub(tmp_path, monkeypatch, handler)
        assert await hub.send(parent.id, "等待静默模型") is None
        await asyncio.wait_for(started.wait(), 1)
        before = time.monotonic()
        await hub.stop(parent.id)
        await asyncio.wait_for(parent.task, 5)
        assert time.monotonic() - before < 4.5
        assert parent.status == "idle" and parent.run["state"] == "cancelled"
        assert models[parent.id].cancelled and models[parent.id].closed
        assert any(e["type"] == "done" for e in parent.events)
    asyncio.run(main())


@pytest.mark.parametrize("fast_child", [False, True])
def test_real_team_results_are_integrated_once_even_when_child_finishes_early(tmp_path, monkeypatch, fast_child):
    async def main():
        release = asyncio.Event()
        waiting_for_child = asyncio.Event()
        child_started = asyncio.Event()
        if fast_child:
            release.set()

        async def handler(session, number, messages):
            if session.parent_id:
                child_started.set()
                await release.wait()
                assert "PRIVATE_PARENT_HISTORY" not in json.dumps(messages)
                return reply("CHILD_EVIDENCE: 2 + 2 = 4"), .02
            if number == 1:
                return reply("", ("delegate_tasks", {"tasks": [{"goal": "独立核对算式", "context": "只核对 2 + 2"}]})), .01
            if number == 2:
                await child_started.wait()
                if fast_child:
                    result = await hub.teams.wait(parent.id, timeout_s=2)
                    assert result["complete"]
                return reply("准备检查分工结果"), .01
            assert number == 3, "finished children must not cause an endless model loop"
            summaries = [m["content"] for m in messages if m.get("role") == "user" and "子任务结果已返回" in str(m.get("content"))]
            assert len(summaries) == 1 and "CHILD_EVIDENCE" in summaries[0]
            return reply("核对后的最终交付：4"), .01

        hub, parent, models = setup_hub(tmp_path, monkeypatch, handler)
        parent.messages.append({"role": "user", "content": "PRIVATE_PARENT_HISTORY"})
        original_emit = hub.emit

        async def observe(session, event, **kwargs):
            await original_emit(session, event, **kwargs)
            if session.id == parent.id and event.get("type") == "note" and "子任务仍在执行" in event.get("text", ""):
                waiting_for_child.set()

        monkeypatch.setattr(hub, "emit", observe)
        assert await hub.send(parent.id, "请分工检查并交付") is None
        if not fast_child:
            await asyncio.wait_for(waiting_for_child.wait(), 2)
            assert parent.status == "running" and not parent.task.done()
            release.set()
        await asyncio.wait_for(parent.task, 4)
        rows = hub.teams.list(parent.id)
        assert len(rows) == 1 and rows[0]["status"] == "completed" and not rows[0]["verified"]
        assert parent.run["state"] == "completed" and parent.run["verification"] == "unreviewed"
        assert parent.run["child_cost"] == .02 and parent.run["cost"] == .03
        assert not any(e["type"] == "error" for e in parent.events)
        assert [e["text"] for e in parent.events if e["type"] == "assistant"][-1] == "核对后的最终交付：4"
        assert len(models[parent.id].requests) == 3 and all(model.closed for model in models.values())
        child = hub.sessions[rows[0]["id"]]
        original_run = deepcopy(child.run)
        assert await hub.send(child.id, "直接续跑")
        assert await hub.send(child.id, "伪装内部续跑", preserve_materials=True)
        assert await hub.resume(child.id)
        with pytest.raises(ValueError):
            await hub.enqueue(child.id, "追加子任务")
        assert child.run == original_run and len(models[child.id].requests) == 1
        await hub.teams.close()
    asyncio.run(main())


def test_parent_stop_cancels_running_and_queued_children(tmp_path, monkeypatch):
    async def main():
        started = asyncio.Event()
        count = 0

        async def handler(session, number, _messages):
            nonlocal count
            if session.parent_id:
                count += 1
                if count == 3:
                    started.set()
                await asyncio.Event().wait()
            if number == 1:
                return reply("", ("delegate_tasks", {"tasks": [{"goal": f"核对 {n}"} for n in range(3)]})), 0
            if number == 2:
                return reply("", ("delegate_tasks", {"tasks": [{"goal": f"排队核对 {n}"} for n in range(2)]})), 0
            await started.wait()
            return reply("等候分工结果"), 0

        hub, parent, models = setup_hub(tmp_path, monkeypatch, handler)
        assert await hub.send(parent.id, "分工处理") is None
        await asyncio.wait_for(started.wait(), 2)
        assert sum(r["status"] == "queued" for r in hub.teams.list(parent.id)) == 2
        await asyncio.wait_for(hub.stop(parent.id), 5)
        await asyncio.wait_for(parent.task, 2)
        assert parent.run["state"] == "cancelled"
        assert all(r["status"] == "cancelled" for r in hub.teams.list(parent.id))
        assert all(s.status == "idle" for s in hub.sessions.values())
        assert count == 3 and all(model.closed for model in models.values())
    asyncio.run(main())


def test_parent_step_budget_cancels_unfinished_children(tmp_path, monkeypatch):
    async def main():
        async def handler(session, *_):
            if session.parent_id:
                await asyncio.Event().wait()
            return reply("", ("delegate_tasks", {"tasks": [{"goal": "耗时核对"}]})), 0

        hub, parent, _ = setup_hub(tmp_path, monkeypatch, handler)
        parent.max_steps = 1
        assert await hub.send(parent.id, "一步后停下") is None
        await asyncio.wait_for(parent.task, 5)
        assert parent.run["state"] == "budget_exhausted" and "步预算" in parent.run["reason"]
        assert all(r["status"] == "cancelled" for r in hub.teams.list(parent.id))
    asyncio.run(main())


def test_child_checks_exhausted_parent_budget_before_its_next_model_request(tmp_path, monkeypatch):
    async def main():
        parent_spent = asyncio.Event()
        child_started = asyncio.Event()

        async def handler(session, number, _messages):
            if session.parent_id:
                child_started.set()
                await parent_spent.wait()
                if number == 1:
                    return reply("部分调查", ("task_plan", {"steps": []})), .005
                return reply("不应发生的第二次子模型请求"), .005
            if number == 1:
                return reply("", ("delegate_tasks", {"tasks": [{"goal": "核对预算", "budget_cny": 0}]})), .01
            await child_started.wait()
            parent_spent.set()
            return reply("", ("delegate_wait", {"timeout_s": 2})), .09

        hub, parent, models = setup_hub(tmp_path, monkeypatch, handler)
        parent.budget_cny = .1
        assert await hub.send(parent.id, "有总费用上限的分工") is None
        await asyncio.wait_for(parent.task, 5)
        row = hub.teams.list(parent.id)[0]
        assert parent.run["state"] == "budget_exhausted"
        assert row["status"] == "interrupted" and "预算" in row["error"]
        assert len(models[row["id"]].requests) == 1
        assert row["budget_cny"] > row["cost"]  # its own allocation was not exhausted
        assert parent.run["child_cost"] == row["cost"]
    asyncio.run(main())

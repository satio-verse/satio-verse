"""User-facing Studio contracts use temporary stores and deterministic MCP fixtures."""
import asyncio
import base64
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wm.agent.core import AgentHub, Session
from wm.agent.context import MAX_FILE
from wm.agent.studio_http import studio, context_upload
from test_agent_growth import package


@asynccontextmanager
async def app(tmp_path):
    h = AgentHub({"agent": {"workspace": str(tmp_path / "ws"), "extensions": {"settings_path": False}}}, home=tmp_path / "agent")
    s = Session("main", str(tmp_path / "ws"), mode="unrestricted")
    h.sessions[s.id] = s
    h.save(s)
    application = web.Application()  # Default limit is 1 MB: upload overrides this locally.
    async def handle_studio(request):
        return await studio(request, h)
    async def handle_context(request):
        return await context_upload(request, h)
    application.router.add_route("*", "/studio", handle_studio)
    application.router.add_route("*", "/context", handle_context)
    async with TestClient(TestServer(application)) as client:
        try:
            yield h, s, client
        finally:
            await h.teams.close()
            if s.task is not None:
                s.task.cancel()
                await asyncio.gather(s.task, return_exceptions=True)


async def post(c, action, **kwargs):
    return await c.post("/studio", json={"id": "main", "action": action, **kwargs})


def test_summary_counts_task_runs_not_messages_and_hides_other_sessions(tmp_path):
    async def run():
        async with app(tmp_path) as (h, s, c):
            s.messages = [{"role": "user", "content": "irrelevant"}] * 10
            folder = h.home / "runs"
            folder.mkdir()
            for rid, sid, verification in [("one", "main", "accepted"), ("two", "main", "unreviewed"), ("other", "other-session", "accepted")]:
                (folder / (rid + ".json")).write_text(json.dumps({"id": rid, "session": sid, "verification": verification,
                    "cost": 0.1, "messages": ["not returned"]}), encoding="utf-8")
            response = await c.get("/studio?id=main")
            assert response.status == 200
            data = await response.json()
            assert {"blacklist", "audit", "extensions", "contexts", "team", "run", "plan", "queue", "growth"} <= data.keys()
            assert data["growth"]["runs"] == 2 and data["growth"]["accepted"] == 1
            assert data["growth"]["scope"] == "current_session"
            assert data["growth"]["cost"] == 0.2
            assert all("messages" not in row for row in data["growth"]["records"])
            assert data["growth"]["suites"] == [] and data["growth"]["reports"] == []
    asyncio.run(run())


def test_session_ids_json_and_unknown_actions_validate_before_mutation(tmp_path):
    async def run():
        async with app(tmp_path) as (_, _, c):
            for suffix in ("../main", "missing", ""):
                assert (await c.get("/studio", params={"id": suffix})).status == 400
            for body in ([], {"id": "../main", "action": "rule_remove"}, {"id": "main", "action": "unknown"}):
                assert (await c.post("/studio", json=body)).status == 400
            assert (await c.post("/studio", data="invalid json")).status == 400
    asyncio.run(run())


def test_user_rule_crud_and_strict_boolean_updates(tmp_path):
    async def run():
        async with app(tmp_path) as (h, _, c):
            rule = {"kind": "command", "value": "wipe-test", "reason": "用户要求"}
            response = await post(c, "rule_add", rule=rule)
            assert response.status == 200
            rid = (await response.json())["rule"]["id"]
            assert (await post(c, "rule_update", rule_id=rid, changes={"enabled": "false"})).status == 400
            response = await post(c, "rule_update", rule_id=rid, changes={"enabled": False})
            assert (await response.json())["rule"]["enabled"] is False
            assert (await post(c, "rule_remove", rule_id=rid)).status == 200
            assert not any(row["id"] == rid for row in h.blacklist.list())
            assert h.blacklist.audit()[0]["by"] == "user"
    asyncio.run(run())


@pytest.mark.parametrize("action", ["rule_add", "rule_update", "rule_remove", "extension_enable", "extension_rollback", "suite_save", "growth_evaluate", "growth_adopt"])
def test_child_cannot_manage_global_studio_state(tmp_path, action):
    async def run():
        async with app(tmp_path) as (_, s, c):
            s.parent_id = "parent"
            response = await post(c, action, rule={"kind": "instruction", "value": "should not persist"})
            assert response.status == 400 and "子会话" in (await response.json())["error"]
    asyncio.run(run())


def test_upload_over_default_http_limit_pin_and_immutable_removal(tmp_path):
    async def run():
        async with app(tmp_path) as (h, _, c):
            data = b"temporary material\n" * 100000
            response = await c.post("/context?id=main", json={"name": "example.txt", "data": base64.b64encode(data).decode()})
            assert response.status == 200
            item = (await response.json())["item"]
            assert item["size"] == len(data) and item["pinned"] is False
            assert (await post(c, "context_pin", context_id=item["id"], pinned="false")).status == 400
            response = await post(c, "context_pin", context_id=item["id"], pinned=True)
            assert (await response.json())["contexts"][0]["pinned"] is True
            assert (await post(c, "context_pin", context_id="missing", pinned=True)).status == 400
            response = await post(c, "context_remove", context_id=item["id"])
            assert (await response.json())["contexts"] == []
            assert (h.contexts.home / "main" / item["file"]).read_bytes() == data
            response = await post(c, "context_reference", value="BV1xx411c7mD")
            assert (await response.json())["item"]["kind"] == "bilibili"
    asyncio.run(run())


def test_invalid_and_oversized_uploads_rejected_without_files(tmp_path):
    async def run():
        async with app(tmp_path) as (h, _, c):
            for data in ("!!!", "data:text/plain;base64,eA==", ["eA=="]):
                assert (await c.post("/context?id=main", json={"name": "x", "data": data})).status == 400
            encoded = base64.b64encode(b"x" * (MAX_FILE + 1)).decode()
            assert (await c.post("/context?id=main", json={"name": "large.bin", "data": encoded})).status == 400
            assert h.contexts.list("main") == []
            assert (await c.post("/context?id=../main", json={"name": "x", "data": "eA=="})).status == 400
    asyncio.run(run())


def test_extension_enable_rollback_and_sandbox_preview(tmp_path):
    async def run():
        async with app(tmp_path) as (h, _, c):
            first = h.extensions.create({"id": "preview", "name": "界面", "files": {"index.html": "<h1>One</h1>"}, "ui": "index.html"})
            second = h.extensions.create({"id": "preview", "name": "界面", "files": {"index.html": "<script>parent.postMessage('test','*')</script>"}, "ui": "index.html"})
            assert (await post(c, "extension_enable", package_id="preview", enabled="false")).status == 400
            response = await post(c, "extension_enable", package_id="preview", enabled=True, revision=second["revision"])
            assert (await response.json())["extension"]["active_revision"] == second["revision"]
            response = await post(c, "extension_rollback", package_id="preview", revision=first["revision"])
            assert (await response.json())["extension"]["active_revision"] == first["revision"]
            response = await c.get("/studio", params={"id": "main", "preview": "preview", "revision": second["revision"]})
            data = await response.json()
            assert data["sandbox"] == "allow-scripts" and "connect-src 'none'" in data["csp"]
            assert response.content_type == "application/json"
    asyncio.run(run())


def test_queue_budget_and_review_contracts(tmp_path):
    async def run():
        async with app(tmp_path) as (h, s, c):
            s.status = "running"  # enqueue must not start any model call in this fixture.
            response = await post(c, "queue", text="next")
            queued = (await response.json())["item"]
            assert s.queue[0]["id"] == queued["id"]
            assert (await post(c, "queue_remove", queue_id=queued["id"])).status == 200 and s.queue == []
            assert (await post(c, "queue_remove", queue_id=queued["id"])).status == 400
            assert (await post(c, "budget", budget_cny=10)).status == 400
            s.status = "idle"
            assert (await post(c, "budget", budget_cny=True)).status == 400
            assert (await post(c, "budget", max_steps=1.5)).status == 400
            response = await post(c, "budget", budget_cny=10, time_budget_s=60, max_steps=12)
            assert (await response.json())["max_steps"] == 12
            response = await post(c, "budget", budget_cny=0, time_budget_s=0, max_steps=0)
            assert response.status == 200 and (await response.json())["max_steps"] == 0
            s.run = {"id": "review-run", "state": "completed", "verification": "unreviewed"}
            assert (await post(c, "review", run_id="review-run", accepted="false")).status == 400
            response = await post(c, "review", run_id="review-run", accepted=True, note="产物已核对")
            assert (await response.json())["run"]["verification"] == "accepted"
            assert h.growth.reports() == [], "User acceptance is not an objective regression report"
    asyncio.run(run())


def test_worktree_route_checks_ownership_before_git(tmp_path, monkeypatch):
    async def run():
        async with app(tmp_path) as (h, _, c):
            def no_git(_):
                raise AssertionError("Unowned worktrees must not reach Git")
            monkeypatch.setattr(h.teams.worktrees, "diff", no_git)
            monkeypatch.setattr(h.teams.worktrees, "export_patch", no_git)
            assert (await post(c, "worktree_diff", worktree_id="unowned")).status == 400
            assert (await post(c, "worktree_export", worktree_id="unowned")).status == 400
    asyncio.run(run())


def test_real_fixed_mcp_evaluation_report_and_adoption_through_http(tmp_path):
    async def run():
        async with app(tmp_path) as (h, _, c):
            old = h.extensions.create(package("wrong"))
            h.extensions.set_enabled("growth-fixture", True, authorized=True)
            new = h.extensions.create(package("correct"))
            suite = {"id": "http-suite", "name": "API 回归", "package_id": "growth-fixture",
                     "cases": [{"tool": "answer", "arguments": {}, "expect": {"equals": "correct"}}]}
            response = await post(c, "suite_save", spec=suite)
            assert (await response.json())["suite"]["by"] == "user"
            params = {"package_id": "growth-fixture", "revision": new["revision"], "suite_id": "http-suite"}
            assert (await post(c, "growth_evaluate", **params)).status == 400
            assert (await post(c, "growth_evaluate", **params, authorized="true")).status == 400
            response = await post(c, "growth_evaluate", **params, authorized=True)
            assert response.status == 200
            report = (await response.json())["report"]
            assert report["comparison"] == "improved_on_suite"
            assert h.extensions.list()["extensions"][0]["active_revision"] == old["revision"]
            response = await c.get("/studio", params={"id": "main", "report": report["id"]})
            assert (await response.json())["adoption"]["eligible"]
            response = await post(c, "growth_adopt", report_id=report["id"], authorized=True)
            assert response.status == 200
            assert (await response.json())["extension"]["active_revision"] == new["revision"]
    asyncio.run(run())


@pytest.mark.parametrize("blocked", ["candidate", "baseline", "tool", "readonly", "plan"])
def test_growth_preflight_blocks_launches_in_both_versions(tmp_path, blocked, monkeypatch):
    async def run():
        async with app(tmp_path) as (h, s, c):
            old = h.extensions.create(package("wrong"))
            h.extensions.set_enabled("growth-fixture", True, authorized=True)
            new = h.extensions.create(package("correct"))
            h.growth.save_suite({"id": "guard-suite", "name": "guard", "package_id": "growth-fixture",
                "cases": [{"tool": "answer", "expect": {"equals": "correct"}}]})
            if blocked in {"candidate", "baseline"}:
                revision = new["revision"] if blocked == "candidate" else old["revision"]
                h.blacklist.add({"kind": "path", "value": str(h.extensions.root / "growth-fixture" / "revisions" / revision)})
            elif blocked == "tool":
                h.blacklist.add({"kind": "tool", "value": "mcp.creator:growth-fixture.answer"})
            elif blocked == "readonly":
                s.mode = "read"
            else:
                s.preset = "plan"
            async def no_run(*args, **kwargs):
                raise AssertionError("MCP must not start after a preflight rejection")
            monkeypatch.setattr(h.growth, "evaluate", no_run)
            response = await post(c, "growth_evaluate", package_id="growth-fixture", revision=new["revision"], suite_id="guard-suite", authorized=True)
            assert response.status == 400
            assert h.extensions.list()["extensions"][0]["active_revision"] == old["revision"]
    asyncio.run(run())

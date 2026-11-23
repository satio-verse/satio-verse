"""Independent-context scheduling and real disposable Git worktrees; no model calls."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from wm.agent.core import Session
from wm.agent.delegation import TeamManager
from wm.agent.worktrees import GitWorktreeManager, WorktreeError
from wm.agent import tools as T


class Hub:
    def __init__(self, home):
        self.home = home
        self.sessions = {}
        self.sent = []
        self.notifications = []
        self.running = self.peak = 0

    def get(self, sid):
        return self.sessions.get(sid)

    def save(self, session):
        folder = self.home / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (session.id + ".json")).write_text(json.dumps(session.to_json()), encoding="utf-8")

    async def send(self, sid, text, preserve_materials=False):
        s = self.sessions[sid]
        assert s.status == "idle"
        assert preserve_materials
        self.sent.append((sid, text))
        s.status = "running"
        s.run = {"started": time.time(), "cost_start": s.stats["cost"]}

        async def run():
            self.running += 1
            self.peak = max(self.peak, self.running)
            try:
                if "STALL" in text:
                    await s.stop_event.wait()
                else:
                    await asyncio.sleep(.03)
                    if "FAIL" in text:
                        s.events.append({"type": "error", "text": "模拟模型连接失败"})
                    else:
                        s.events.append({"type": "assistant", "text": "独立分析的结果"})
                    if "BUDGET" in text:
                        s.run.update(state="budget_exhausted", reason="达到子任务预算，已有部分成果")
                        if "TIME_BUDGET" in text:
                            s.stop_event.set()
                    s.stats["cost"] += .2
                    s.stats["steps"] += 1
            finally:
                self.running -= 1
                s.status = "idle"
        s.task = asyncio.create_task(run())
        return None

    async def stop(self, sid):
        self.sessions[sid].stop_event.set()

    async def emit(self, parent, event):
        self.notifications.append((parent.id, event))


def setup_team(tmp_path, mode="full"):
    hub = Hub(tmp_path / "agent")
    parent = Session("parent", str(tmp_path / "project"), mode=mode, model="test-model")
    parent.depth = 0
    parent.budget_cny = 0
    parent.time_budget_s = 0
    parent.run = {"started": time.time(), "cost_start": 0}
    parent.messages = [{"role": "user", "content": "PRIVATE-PARENT-HISTORY"}]
    parent.materials = [{"id": "chosen", "owner": "parent", "kind": "file", "name": "a.txt", "file": "a.txt", "pinned": True},
                        {"id": "secret-other", "owner": "parent", "kind": "file", "name": "b.txt", "file": "b.txt", "pinned": True}]
    hub.sessions[parent.id] = parent
    return hub, parent, TeamManager(hub)


def test_children_get_only_selected_context_and_bounded_parallelism(tmp_path):
    async def main():
        hub, parent, team = setup_team(tmp_path)
        rows = await team.delegate(parent, [{"goal": f"分析第 {n} 个问题", "context": "必要的项目摘要", "context_ids": ["chosen"]} for n in range(5)])
        result = await team.wait(parent.id, timeout_s=10)
        assert result["complete"] and len(result["children"]) == 5
        assert hub.peak == 3 and team.spent(parent.id) == 1.0
        assert all(row["status"] == "completed" and row["verified"] is False for row in result["children"])
        for row in rows:
            child = hub.sessions[row["id"]]
            assert child.messages == [] and child.model == parent.model and child.mode == "read"
            assert child.materials == [parent.materials[0]] and child.materials is not parent.materials
            assert child.depth == 1 and child.parent_id == parent.id
        assert all("PRIVATE-PARENT-HISTORY" not in text and "必要的项目摘要" in text for _, text in hub.sent)
        assert len(hub.notifications) == 10
    asyncio.run(main())


def test_permissions_depth_and_unknown_context_fail_before_start(tmp_path):
    async def main():
        hub, parent, team = setup_team(tmp_path, "read")
        with pytest.raises(ValueError, match="高于"):
            await team.delegate(parent, [{"goal": "edit", "mode": "unrestricted", "worktree": True}])
        with pytest.raises(ValueError, match="材料"):
            await team.delegate(parent, [{"goal": "one"}, {"goal": "two", "context_ids": ["other"]}])
        assert not team.list() and len(hub.sessions) == 1
        rows = await team.delegate(parent, [{"goal": "read"}])
        child = hub.sessions[rows[0]["id"]]
        with pytest.raises(ValueError, match="不能继续"):
            await team.delegate(child, [{"goal": "grandchild"}])
        child.mode = "unrestricted"
        assert team.effective_mode(child) == "read"
        await team.wait(parent.id, timeout_s=1)
        with pytest.raises(ValueError, match="父任务"):
            await team.wait("wrong-parent", [child.id], timeout_s=0)
    asyncio.run(main())


def test_parent_budget_is_shared_and_cannot_be_reset_by_zero(tmp_path):
    async def main():
        hub, parent, team = setup_team(tmp_path)
        parent.budget_cny = 1
        parent.stats["cost"] = .2
        rows = await team.delegate(parent, [{"goal": "STALL one", "budget_cny": 0}, {"goal": "STALL two", "budget_cny": 8}])
        assert [r["budget_cny"] for r in rows] == [.4, .4]
        with pytest.raises(ValueError, match="已分配"):
            await team.delegate(parent, [{"goal": "more"}])
        await team.cancel(parent.id)
        assert all(r["status"] == "cancelled" for r in team.list(parent.id))
        assert all(hub.sessions[r["id"]].status == "idle" for r in rows)
        parent.budget_cny = .20000001
        tiny = await team.delegate(parent, [{"goal": "tiny"}])
        assert 0 < tiny[0]["budget_cny"] < 0.000001  # never rounded to unlimited zero
        await team.cancel(parent.id)
        assert not hub.sent
    asyncio.run(main())


def test_cancel_running_children_and_failure_are_truthful(tmp_path):
    async def main():
        hub, parent, team = setup_team(tmp_path)
        rows = await team.delegate(parent, [{"goal": "STALL"}, {"goal": "FAIL"}])
        await asyncio.sleep(.08)
        waiting = await team.wait(parent.id, timeout_s=0)
        assert not waiting["complete"]
        assert team.inspect(rows[1]["id"])["status"] == "failed"
        assert "连接失败" in team.inspect(rows[1]["id"])["error"]
        await team.cancel(parent.id, rows[0]["id"])
        final = await team.wait(parent.id, timeout_s=1)
        assert final["complete"] and hub.running == 0
        assert [r["status"] for r in final["children"]] == ["cancelled", "failed"]
        persisted = json.loads((team.home / "tasks.json").read_text(encoding="utf-8"))
        assert persisted["tasks"][rows[0]["id"]]["status"] == "cancelled"
    asyncio.run(main())


def test_restart_marks_active_records_interrupted_without_replay(tmp_path):
    hub, parent, team = setup_team(tmp_path)
    team.home.mkdir(parents=True)
    (team.home / "tasks.json").write_text(json.dumps({"version": 1, "tasks": {
        "old": {"id": "old", "parent_id": parent.id, "created": 1, "status": "waiting", "cost": .7}}}), encoding="utf-8")
    recovered = TeamManager(hub)
    row = recovered.inspect("old")
    assert row["status"] == "interrupted" and "未自动重放" in row["error"]
    assert recovered.spent(parent.id) == .7 and hub.sent == []


def test_shutdown_drains_running_and_queued_children_and_blocks_new_delegation(tmp_path):
    async def main():
        hub, parent, team = setup_team(tmp_path)
        await team.delegate(parent, [{"goal": "STALL " + str(n)} for n in range(5)])
        await asyncio.sleep(.03)
        assert hub.running == 3 and len(hub.sent) == 3
        await team.close()
        assert hub.running == 0 and len(hub.sent) == 3
        assert all(r["status"] == "cancelled" for r in team.list(parent.id))
        with pytest.raises(ValueError, match="正在关闭"):
            await team.delegate(parent, [{"goal": "must not run during shutdown"}])
        assert len(team.list()) == 5
    asyncio.run(main())


@pytest.mark.parametrize("goal", ["BUDGET", "TIME_BUDGET"])
def test_budget_limited_child_keeps_partial_result_without_claiming_completion(tmp_path, goal):
    async def main():
        hub, parent, team = setup_team(tmp_path)
        rows = await team.delegate(parent, [{"goal": goal}])
        result = await team.wait(parent.id, timeout_s=2)
        row = result["children"][0]
        assert result["complete"] and row["status"] == "interrupted"
        assert row["run_state"] == "budget_exhausted" and "预算" in row["error"]
        assert row["result"] == "独立分析的结果" and row["verified"] is False
        recovered = TeamManager(hub).inspect(rows[0]["id"])
        assert recovered["status"] == "interrupted" and recovered["reason"] == row["reason"]
    asyncio.run(main())


def git(repo, *args):
    result = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=repo,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


@pytest.fixture
def repo(tmp_path):
    if not shutil.which("git"):
        pytest.skip("Git is required for real worktree checks")
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Isolated test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "file.txt").write_text("committed baseline\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    return repo


def test_worktree_preserves_dirty_source_and_starts_at_committed_head(tmp_path, repo):
    manager = GitWorktreeManager(tmp_path / "managed")
    (repo / "file.txt").write_text("uncommitted work\n", encoding="utf-8")
    (repo / "unsaved.txt").write_text("private draft", encoding="utf-8")
    before = git(repo, "status", "--porcelain=v1", "-z")
    item = manager.create(repo, "independent edit")
    fork = Path(item["path"])
    assert (fork / "file.txt").read_text() == "committed baseline\n" and not (fork / "unsaved.txt").exists()
    assert item["source_dirty"] and "未带入" in item["note"] and item["merged"] is False
    assert git(repo, "status", "--porcelain=v1", "-z") == before
    assert (repo / "file.txt").read_text() == "uncommitted work\n"
    assert len(manager.list()) == 1 and manager.status(item["id"])["dirty"] is False


def test_worktree_patch_includes_new_binary_and_committed_files_without_index_changes(tmp_path, repo):
    manager = GitWorktreeManager(tmp_path / "managed")
    item = manager.create(repo)
    fork = Path(item["path"])
    (fork / "file.txt").write_text("child committed edit\n", encoding="utf-8")
    git(fork, "add", "file.txt")
    git(fork, "commit", "-m", "child commit")
    (fork / "file.txt").write_text("child working edit\n", encoding="utf-8")
    (fork / "new file.txt").write_text("new text\n", encoding="utf-8")
    (fork / "asset.bin").write_bytes(bytes(range(256)) * 10)
    index = Path(git(fork, "rev-parse", "--git-path", "index").decode().strip())
    if not index.is_absolute():
        index = fork / index
    before_index = index.read_bytes()
    preview = manager.diff(item["id"])
    exported = manager.export_patch(item["id"])
    patch = Path(exported["path"]).read_bytes()
    assert b"new file.txt" in patch and b"asset.bin" in patch and b"GIT binary patch" in patch
    assert b"child working edit" in patch and b"committed baseline" in patch
    assert preview["includes_untracked"] and exported["merged"] is False
    assert index.read_bytes() == before_index and (repo / "file.txt").read_text() == "committed baseline\n"
    assert not (repo / "new file.txt").exists()
    git(repo, "apply", "--check", exported["path"])
    # The exported artifact is a real, complete Git patch, including new assets.
    git(repo, "apply", exported["path"])
    assert (repo / "new file.txt").read_text() == "new text\n"
    assert (repo / "asset.bin").read_bytes() == bytes(range(256)) * 10
    assert not list((manager.home / "scratch").iterdir())


def test_non_repo_and_registry_path_tampering_fail(tmp_path, repo):
    manager = GitWorktreeManager(tmp_path / "managed")
    not_repo = tmp_path / "not-repo"
    not_repo.mkdir()
    with pytest.raises(WorktreeError, match="Git 工作区"):
        manager.create(not_repo)
    with pytest.raises(WorktreeError, match="编号"):
        manager.status("../outside")
    item = manager.create(repo)
    store = json.loads((manager.home / "registry.json").read_text(encoding="utf-8"))
    store["items"][item["id"]]["path"] = str(repo)
    (manager.home / "registry.json").write_text(json.dumps(store), encoding="utf-8")
    with pytest.raises(WorktreeError, match="路径"):
        manager.export_patch(item["id"])


def test_editing_child_uses_own_real_worktree(tmp_path, repo):
    async def main():
        hub, parent, team = setup_team(tmp_path)
        parent.workspace = str(repo)
        rows = await team.delegate(parent, [{"goal": "在独立目录中修改", "mode": "workspace", "worktree": True}])
        result = await team.wait(parent.id, timeout_s=10)
        row = result["children"][0]
        child = hub.sessions[rows[0]["id"]]
        assert row["status"] == "completed" and row["worktree_id"]
        assert child.workspace != parent.workspace and child.mode == "workspace"
        assert Path(child.workspace, ".git").is_file()
        assert team.effective_mode(child) == "workspace"
        parent.mode = "read"
        assert team.effective_mode(child) == "read"
    asyncio.run(main())


def test_same_goal_gets_distinct_ids_and_cancel_during_worktree_keeps_its_record(tmp_path, repo, monkeypatch):
    async def main():
        hub, parent, team = setup_team(tmp_path)
        parent.workspace = str(repo)
        entered = asyncio.Event()
        actual_create = team.worktrees.create
        loop = asyncio.get_running_loop()

        def slow_create(*args):
            loop.call_soon_threadsafe(entered.set)
            time.sleep(.1)
            return actual_create(*args)
        monkeypatch.setattr(team.worktrees, "create", slow_create)
        rows = await team.delegate(parent, [{"goal": "same", "worktree": True}, {"goal": "same"}])
        assert rows[0]["id"] != rows[1]["id"]
        await asyncio.wait_for(entered.wait(), timeout=2)
        await team.cancel(parent.id, rows[0]["id"])
        row = team.inspect(rows[0]["id"])
        assert row["status"] == "cancelled" and row["worktree_id"]
        assert Path(row["workspace"], ".git").is_file()
        assert not any(sid == rows[0]["id"] for sid, _ in hub.sent)
        await team.wait(parent.id, timeout_s=1)
    asyncio.run(main())


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell process-tree cancellation")
@pytest.mark.parametrize("method", ["cancel", "event"])
def test_cancel_command_kills_child_process_tree(tmp_path, method):
    ready = tmp_path / "ready.txt"
    late = tmp_path / "late.txt"
    child = tmp_path / "owned_child.py"
    child.write_text("import time\nfrom pathlib import Path\nPath('ready.txt').write_text('ready')\ntime.sleep(3)\nPath('late.txt').write_text('leaked')\n", encoding="utf-8")
    executable = sys.executable.replace("'", "''")
    script = str(child).replace("'", "''")

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(T.run_command(f"& '{executable}' '{script}'", tmp_path, 30, stop))
        deadline = time.monotonic() + 8
        while not ready.exists() and time.monotonic() < deadline:
            await asyncio.sleep(.03)
        assert ready.exists(), "owned child failed to start"
        if method == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            stop.set()
            assert "被主人停掉" in await task
        await asyncio.sleep(3.1)
        assert not late.exists(), "the command returned stopped but its child was left running"
    asyncio.run(main())


def test_pre_stopped_command_never_launches(tmp_path, monkeypatch):
    async def main():
        stop = asyncio.Event()
        stop.set()
        async def no_launch(*args, **kwargs):
            raise AssertionError("process should not launch")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", no_launch)
        assert "命令未启动" in await T.run_command("Write-Output never", tmp_path, stop=stop)
    asyncio.run(main())

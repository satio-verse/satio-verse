"""待办插件 (平台化第七切片, 第三个插件) 的真实演练.

用仓库里的 `plugins/todolist` 真装进临时 `WM_PLUGIN_HOME`, 数据和回执都指到临时目录
(`WM_TODO_HOME`), 不碰真的 `~/.worldmodel`。宿主核对 (`WM_PLUGIN_HOST_VERIFY`) 单独开关,
默认关的测试里会确认关闭时行为不变。
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from wm.agent import plugin_host as ph
from wm.agent import plugin_manifest as pm

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "plugins" / "todolist"
PLUGIN_ID = "todolist"
ID_PATTERN = re.compile(r"^td-\d{8}-\d{6}-[0-9a-f]{6}$")


@pytest.fixture()
def drill(tmp_path, monkeypatch):
    """宿主开关打开; 插件目录和待办数据都指到 tmp_path, 宿主核对默认关."""
    monkeypatch.setenv(ph.SWITCH_ENV, "on")
    monkeypatch.setenv(ph.HOME_ENV, str(tmp_path / "plugins"))
    monkeypatch.setenv("WM_TODO_HOME", str(tmp_path / "plugins" / PLUGIN_ID / "data"))
    monkeypatch.delenv(ph.VERIFY_ENV, raising=False)
    return tmp_path


def load_plugin():
    """直接加载插件模块, 方便测动作层 (不经过宿主; 导入不执行动作)."""
    spec = importlib.util.spec_from_file_location("todolist_plugin_under_test", PACKAGE / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def installed():
    result = ph.install(PACKAGE)
    assert result["ok"] is True, result
    assert result["granted_tier"] == "text"
    assert ph.set_enabled(PLUGIN_ID, True)["ok"] is True
    return result


def granted():
    installed()
    result = ph.grant_tier(PLUGIN_ID, "panel", why="演练：在临时目录里记待办")
    assert result["ok"] is True and result["granted_tier"] == "panel"
    return result


def call(action, params=None):
    out = ph.invoke(PLUGIN_ID, action, params)
    assert out["ok"] is True, out
    return out["result"]


def todos_path(root):
    return Path(root) / "plugins" / PLUGIN_ID / "data" / "todos.jsonl"


def receipts_path(root):
    return Path(root) / "plugins" / PLUGIN_ID / "data" / "receipts.jsonl"


def receipts_kinds(root):
    path = receipts_path(root)
    if not path.is_file():
        return []
    return [json.loads(line).get("kind") for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def audit_rows(root):
    path = Path(root) / "plugins" / "audit.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_manifest_passes_the_frozen_contract_validator():
    manifest = json.loads((PACKAGE / "wmworld.json").read_text(encoding="utf-8"))
    assert pm.validate_plugin_manifest(manifest, package_dir=PACKAGE) == []
    assert pm.policy_issues(manifest, "third_party") == []
    actions = manifest["plugin"]["world"]["actions"]
    assert [action["kind"] for action in actions] == ["act", "query", "act", "act"]
    assert manifest["plugin"]["world"]["prefix"] == "todo"
    assert manifest["plugin"]["tier"] == "panel"


def test_switch_off_everything_is_off_and_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv(ph.SWITCH_ENV, "off")
    monkeypatch.setenv(ph.HOME_ENV, str(tmp_path / "plugins"))
    monkeypatch.setenv("WM_TODO_HOME", str(tmp_path / "todo-home"))
    out = ph.invoke(PLUGIN_ID, "todo_add", {"text": "关着的时候不该写文件"})
    assert out["ok"] is False and out["error"]["code"] == "E_HOST_OFF"
    assert not (tmp_path / "todo-home" / "todos.jsonl").exists()
    module = load_plugin()
    assert module.add("直接调也一样")["enabled"] is False


def test_install_grants_text_tier_first_then_panel(drill):
    installed()
    assert "todo_list" in ph.tool_names()
    assert "todo_add" not in ph.tool_names()  # act 还没授权
    assert ph.read_only("todo_list") is True
    refused = ph.invoke(PLUGIN_ID, "todo_add", {"text": "还没授权"})
    assert refused["ok"] is False and refused["error"]["code"] == "E_NEEDS_GRANT"
    assert not todos_path(drill).exists()

    granted_tier = ph.grant_tier(PLUGIN_ID, "panel", why="演练：在临时目录里记待办")
    assert granted_tier["ok"] is True and granted_tier["granted_tier"] == "panel"
    assert {"todo_add", "todo_list", "todo_done", "todo_undo"} <= ph.tool_names()
    assert ph.read_only("todo_done") is False


def test_add_three_and_list_them(drill):
    granted()
    first = call("todo_add", {"text": "买牛奶", "due": "今晚", "source": "用户：下班顺路买点牛奶"})
    second = call("todo_add", {"text": "给姐姐回消息"})
    third = call("todo_add", {"text": "整理照片", "due": "这周末"})
    for out in (first, second, third):
        assert ID_PATTERN.match(out["id"]), out
        assert out["verified"] == {"checked": 1, "ok": 1}
        assert out["entry"]["id"] == out["id"]
    lines = todos_path(drill).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    listed = call("todo_list", {})
    assert listed["ok"] is True and listed["total"] == 3 and listed["open"] == 3 and listed["done"] == 0
    items = listed["items"]
    assert [item["text"] for item in items] == ["买牛奶", "给姐姐回消息", "整理照片"]
    assert items[0]["due"] == "今晚" and "牛奶" in items[0]["source"]
    assert items[1]["due"] is None and items[1]["done_at"] is None


def test_add_requires_text_and_rejects_unknown_params(drill):
    granted()
    empty = call("todo_add", {"text": "   "})
    assert empty["ok"] is False and "不能是空的" in empty["reason"]
    missing = ph.invoke(PLUGIN_ID, "todo_add", {})
    assert missing["ok"] is False and missing["error"]["code"] == "E_PARAM_REQUIRED"
    unknown = ph.invoke(PLUGIN_ID, "todo_add", {"text": "x", "priority": "高"})
    assert unknown["ok"] is False and unknown["error"]["code"] == "E_PARAM_UNKNOWN"
    assert not todos_path(drill).exists()


def test_done_marks_without_deleting_and_undo_restores(drill):
    granted()
    out = call("todo_add", {"text": "交水费"})
    todo_id = out["id"]
    finished = call("todo_done", {"id": todo_id})
    assert finished["ok"] is True and finished["already_done"] is False
    assert finished["done_at"] and finished["entry"]["done_at"] == finished["done_at"]
    assert finished["verified"] == {"checked": 1, "ok": 1}
    # 不删行: 文件里还是只有一条, 只是 done_at 有值
    lines = todos_path(drill).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["done_at"]
    assert call("todo_list", {})["items"] == []
    shown = call("todo_list", {"include_done": True})["items"]
    assert len(shown) == 1 and shown[0]["text"] == "交水费"

    back = call("todo_undo", {"id": todo_id})
    assert back["ok"] is True and back["already_open"] is False and back["done_at"] is None
    assert back["verified"] == {"checked": 1, "ok": 1}
    assert call("todo_list", {})["total"] == 1
    assert json.loads(todos_path(drill).read_text(encoding="utf-8").splitlines()[0])["done_at"] is None


def test_done_and_undo_are_idempotent(drill):
    granted()
    todo_id = call("todo_add", {"text": "幂等演示"})["id"]
    call("todo_done", {"id": todo_id})
    before = todos_path(drill).read_bytes()
    again = call("todo_done", {"id": todo_id})
    assert again["already_done"] is True and todos_path(drill).read_bytes() == before
    assert receipts_kinds(drill).count("done") == 1

    call("todo_undo", {"id": todo_id})
    before_undo = todos_path(drill).read_bytes()
    again_undo = call("todo_undo", {"id": todo_id})
    assert again_undo["already_open"] is True and todos_path(drill).read_bytes() == before_undo
    assert receipts_kinds(drill).count("undone") == 1


def test_unknown_and_malformed_ids_fail_without_touching_files(drill):
    granted()
    call("todo_add", {"text": "保持原样"})
    before = todos_path(drill).read_bytes()
    unknown = call("todo_done", {"id": "td-20260101-000000-abcdef"})
    assert unknown["ok"] is False and "没有这条待办" in unknown["reason"]
    bad = call("todo_done", {"id": "随便写的 id"})
    assert bad["ok"] is False and "id 不对" in bad["reason"]
    assert todos_path(drill).read_bytes() == before


def test_broken_lines_are_kept_and_skipped(drill):
    granted()
    call("todo_add", {"text": "正常的一条"})
    path = todos_path(drill)
    path.write_text("{这不是合法 json\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    listed = call("todo_list", {})
    assert listed["skipped_lines"] == 1 and listed["total"] == 1
    todo_id = listed["items"][0]["id"]
    assert call("todo_done", {"id": todo_id})["ok"] is True
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("{这不是合法")  # 坏行原样保留
    assert json.loads(text.splitlines()[1])["done_at"]


def test_host_verifies_on_when_data_lives_inside_the_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv(ph.SWITCH_ENV, "on")
    monkeypatch.setenv(ph.HOME_ENV, str(tmp_path / "plugins"))
    monkeypatch.setenv("WM_TODO_HOME", str(tmp_path / "plugins" / PLUGIN_ID / "data"))
    monkeypatch.setenv(ph.VERIFY_ENV, "on")
    granted()
    out = ph.invoke(PLUGIN_ID, "todo_add", {"text": "宿主核对这条"})
    assert out["ok"] is True
    assert out["host_verified"] is True
    rows = audit_rows(tmp_path)
    assert [row["action"] for row in rows][-2:] == ["invoke", "event"]
    assert rows[-2]["host_verified"] is True
    assert rows[-1]["event"] == "plugin.receipt" and rows[-1]["host_verified"] is True


def test_host_verification_is_unknown_when_data_is_outside_the_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv(ph.SWITCH_ENV, "on")
    monkeypatch.setenv(ph.HOME_ENV, str(tmp_path / "plugins"))
    monkeypatch.setenv("WM_TODO_HOME", str(tmp_path / "somewhere-else"))
    monkeypatch.setenv(ph.VERIFY_ENV, "on")
    granted()
    out = ph.invoke(PLUGIN_ID, "todo_add", {"text": "数据目录在插件外面"})
    assert out["ok"] is True and out["host_verified"] is None
    # 越界路径不许读: 文件根本不该被宿主打开, 但插件自己写成功了
    assert (tmp_path / "somewhere-else" / "todos.jsonl").is_file()


def test_receipts_record_each_action_with_verification(drill):
    granted()
    todo_id = call("todo_add", {"text": "记回执"})["id"]
    call("todo_done", {"id": todo_id})
    call("todo_undo", {"id": todo_id})
    assert receipts_kinds(drill) == ["add", "done", "undone"]
    rows = [json.loads(line) for line in receipts_path(drill).read_text(encoding="utf-8").splitlines()]
    for row in rows:
        assert row["todo_id"] == todo_id
        assert row["verified"]["checked"] == 1 and row["verified"]["ok"] == 1
        assert row["at"] and row["ts"]


def test_real_drill_install_add_list_done_undo_twice(drill):
    """真实演练: 装 -> 加 3 条 -> 列表 -> 完成 1 条 -> 撤销 -> 再撤销一次内容不变."""
    installed()
    ph.grant_tier(PLUGIN_ID, "panel", why="真实演练：装、加三条、完成、撤销")
    ids = [call("todo_add", {"text": text})["id"] for text in ("演练一", "演练二", "演练三")]
    assert call("todo_list", {})["total"] == 3
    call("todo_done", {"id": ids[1]})
    assert call("todo_list", {})["open"] == 2
    assert call("todo_list", {"include_done": True})["done"] == 1
    assert call("todo_undo", {"id": ids[1]})["already_open"] is False
    snapshot = todos_path(drill).read_bytes()
    again = call("todo_undo", {"id": ids[1]})
    assert again["already_open"] is True
    assert todos_path(drill).read_bytes() == snapshot
    assert call("todo_list", {})["open"] == 3

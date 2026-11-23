"""文件整理插件走插件宿主执行器的真实演练 (平台化第二切片, 任务 C).

用仓库里的 `plugins/filesorter` 真装进临时 `WM_PLUGIN_HOME`, 在临时目录里真移动文件;
回执和路径保护库都指到临时目录 (`WM_FILESORTER_HOME` / `WM_BLACKLIST_HOME`), 不碰真的 `~/.worldmodel`.
"""
import json
from pathlib import Path

import pytest

from wm.agent import plugin_host as ph
from wm.agent import plugin_manifest as pm
from wm.agent.studio import StudioRuntime

PACKAGE = Path(__file__).resolve().parents[1] / "plugins" / "filesorter"


@pytest.fixture
def drill(tmp_path, monkeypatch):
    """宿主开关打开; 插件目录、回执目录、路径保护库全部指到 tmp_path."""
    monkeypatch.setenv(ph.SWITCH_ENV, "on")
    monkeypatch.setenv(ph.HOME_ENV, str(tmp_path / "plugins"))
    monkeypatch.setenv("WM_FILESORTER_HOME", str(tmp_path / "filesorter-home"))
    monkeypatch.setenv("WM_BLACKLIST_HOME", str(tmp_path / "blacklist-home"))
    return tmp_path


def installed():
    result = ph.install(PACKAGE)
    assert result["ok"] is True, result
    assert result["granted_tier"] == "text"
    assert ph.set_enabled("filesorter", True)["ok"] is True
    return result


def manifest_actions():
    manifest = json.loads((PACKAGE / "wmworld.json").read_text(encoding="utf-8"))
    return {action["name"]: action for action in manifest["plugin"]["world"]["actions"]}


def test_the_manifest_passes_the_frozen_contract_validator():
    manifest = json.loads((PACKAGE / "wmworld.json").read_text(encoding="utf-8"))
    assert pm.validate_plugin_manifest(manifest, package_dir=PACKAGE) == []
    assert pm.policy_issues(manifest, "third_party") == []
    actions = manifest["plugin"]["world"]["actions"]
    assert [action["kind"] for action in actions] == ["query", "act", "act"]
    assert manifest["plugin"]["world"]["prefix"] == "filesorter"


def test_plan_move_undo_through_the_executor(drill):
    installed()
    work = drill / "work"
    work.mkdir()
    (work / "photo.jpg").write_text("jpg", encoding="utf-8")
    (work / "notes.md").write_text("md", encoding="utf-8")
    (work / ".hidden").write_text("h", encoding="utf-8")

    # text 档: query 能跑, act 被拒 (需要你先授权)
    assert "filesorter_plan" in ph.tool_names()
    assert "filesorter_move" not in ph.tool_names()
    plan = ph.invoke("filesorter", "filesorter_plan", {"dir": str(work)})
    assert plan["ok"] is True, plan
    assert plan["result"]["dry_run"] is True and plan["result"]["count"] == 2
    assert plan["receipt_id"] is None
    refused = ph.invoke("filesorter", "filesorter_move", {"dir": str(work), "confirm": True})
    assert refused["ok"] is False and refused["error"]["code"] == "E_NEEDS_GRANT"
    assert "需要你先授权" in refused["error"]["message"]
    assert (work / "photo.jpg").is_file() and not (work / "images").exists()

    # 提权到 panel: 登记表 + 审计行
    granted = ph.grant_tier("filesorter", "panel", why="演练：在临时目录里整理文件")
    assert granted["ok"] is True and granted["granted_tier"] == "panel"
    audit = [json.loads(line) for line in
             (drill / "plugins" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert audit[-1]["plugin_id"] == "filesorter"
    assert audit[-1]["from"] == "text" and audit[-1]["to"] == "panel"
    assert audit[-1]["why"] == "演练：在临时目录里整理文件" and audit[-1]["by"]

    # act 出现在工具表里; query 是只读, act 不是
    names = ph.tool_names()
    assert {"filesorter_plan", "filesorter_move", "filesorter_undo"} <= names
    assert ph.read_only("filesorter_plan") is True
    assert ph.read_only("filesorter_undo") is False

    # confirm=false: 不动文件
    not_yet = ph.invoke("filesorter", "filesorter_move", {"dir": str(work), "confirm": False})
    assert not_yet["ok"] is True and not_yet["result"]["ok"] is False
    assert "confirm=true" in not_yet["result"]["reason"]
    assert (work / "photo.jpg").is_file()

    # confirm=true: 真移, 写回执
    moved = ph.invoke("filesorter", "filesorter_move", {"dir": str(work), "confirm": True})
    assert moved["ok"] is True, moved
    receipt = moved["receipt_id"]
    assert receipt and moved["receipt_id"] == moved["result"]["receipt"]
    assert moved["result"]["files_moved"] == 2
    assert (work / "images" / "photo.jpg").is_file()
    assert (work / "documents" / "notes.md").is_file()
    assert (work / ".hidden").is_file()  # 隐藏文件不动
    assert (drill / "filesorter-home" / "receipts.jsonl").is_file()

    # undo: 按回执移回原位
    undone = ph.invoke("filesorter", "filesorter_undo", {"receipt_id": receipt})
    assert undone["ok"] is True, undone
    assert undone["result"]["files_restored"] == 2
    assert (work / "photo.jpg").is_file() and (work / "notes.md").is_file()
    assert not (work / "images" / "photo.jpg").exists()

    # 再 undo 一次: 幂等, 不报错、不谎报移回
    again = ph.invoke("filesorter", "filesorter_undo", {"receipt_id": receipt})
    assert again["ok"] is True and again["result"]["files_restored"] == 0
    assert again["result"]["already_undone"] is True


def test_executor_rejects_bad_params_before_touching_files(drill):
    installed()
    work = drill / "work"
    work.mkdir()
    (work / "photo.jpg").write_text("jpg", encoding="utf-8")
    assert ph.invoke("filesorter", "filesorter_plan", {})["error"]["code"] == "E_PARAM_REQUIRED"
    assert ph.invoke("filesorter", "filesorter_plan", {"dir": 3})["error"]["code"] == "E_PARAM_TYPE"
    assert ph.invoke("filesorter", "filesorter_plan",
                     {"dir": str(work), "extra": 1})["error"]["code"] == "E_PARAM_UNKNOWN"
    # 没授权时连 confirm 参数都不核对, 先报需要授权
    out = ph.invoke("filesorter", "filesorter_move", {"dir": str(work), "confirm": "yes"})
    assert out["ok"] is False and out["error"]["code"] == "E_NEEDS_GRANT"
    assert (work / "photo.jpg").is_file()


def test_unknown_receipt_is_a_plugin_error_not_a_host_error(drill):
    installed()
    ph.grant_tier("filesorter", "panel", why="演练")
    out = ph.invoke("filesorter", "filesorter_undo", {"receipt_id": "fs-does-not-exist"})
    assert out["ok"] is True  # 宿主调通了, 是插件自己说找不到
    assert out["result"]["ok"] is False and "没有找到这条回执" in out["result"]["reason"]


class _Presets:
    def get(self, name):
        return {"tools": ["*"]}


class _Teams:
    mode = "unrestricted"

    def effective_mode(self, session):
        return self.mode


class _Blacklist:
    def check(self, name, args, path):
        return None


class _Hub:
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.presets = _Presets()
        self.teams = _Teams()
        self.blacklist = _Blacklist()


class _Session:
    preset = "standard"
    stop_event = None

    def __init__(self, workspace):
        self.workspace = str(workspace)


def test_filesorter_tools_show_up_in_her_tool_list_with_the_why_description(drill):
    installed()
    runtime = StudioRuntime(_Hub(drill / "agent-home"))
    session = _Session(drill)
    specs = {row["function"]["name"]: row["function"] for row in runtime.tools(session)}
    assert "filesorter_plan" in specs and "filesorter_move" not in specs
    why = manifest_actions()["filesorter_plan"]["why"]
    assert specs["filesorter_plan"]["description"] == why
    assert runtime.is_plugin_tool("filesorter_plan") is True
    assert runtime.is_plugin_tool("filesorter_undo") is False
    assert ph.grant_tier("filesorter", "panel", why="演练")["ok"] is True
    specs = {row["function"]["name"]: row["function"] for row in runtime.tools(session)}
    assert {"filesorter_plan", "filesorter_move", "filesorter_undo"} <= set(specs)
    assert specs["filesorter_move"]["description"] == manifest_actions()["filesorter_move"]["why"]
    assert specs["filesorter_move"]["parameters"]["required"] == ["dir", "confirm"]

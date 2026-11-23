"""插件宿主第五切片: 宿主侧独立核对 + 回执进事件流 (默认关).

只测 `wm/agent/plugin_host.py` 新增的宿主核对部分:

- `WM_PLUGIN_HOST_VERIFY` 关着 (默认) 时: `invoke` 的返回结构、审计行、盘上文件都跟第五切片之前一样 ——
  不多一个键、不多一行 `plugin.receipt` 事件;
- 开着时: 宿主对自己看得懂的两类世界 (文件整理 / 记账账本) 自己再核一遍, 得到 `host_verified` 三态,
  和插件的 `verified` 分开记; 两者都说得出话却相反时加 `warnings` + `E_VERIFY_DISAGREE` 审计, 但不判失败;
- 插件给的账本路径越出它自己的目录 → 拒读, 记 `null`。

全程 tmp_path 的插件目录 + 假插件, 不碰真 `~/.worldmodel/plugins`, 也不碰用户文件。
"""
import json
from pathlib import Path

import pytest

from wm.agent import plugin_host as ph

from test_plugin_host_actions import audit_rows, demo_manifest, install

REPO = Path(__file__).resolve().parents[1]
RESULT_KEYS = {"ok", "action", "result", "error", "receipt_id"}
DISAGREE = ph.VERIFY_DISAGREE_CODE

# 一个只有 query 动作的最小清单: 核对测试不需要提权
LOOK_ACTIONS = [{
    "name": "demo_look",
    "kind": "query",
    "why": "宿主核对测试",
    "params": {"room": {"type": "string", "required": False}},
}]

# 假插件: 声称移动了一条文件 (src/dst 由测试塞环境变量), 自证 checked=1/ok=1
MOVE_SOURCE = '''
import os


def look(room=None):
    src = os.environ.get("VERIFY_SRC", "")
    dst = os.environ.get("VERIFY_DST", "")
    return {"moved": [{"src": src, "dst": dst, "target": "images/a.jpg"}],
            "receipt": "rc-move-1", "verified": {"checked": 1, "ok": 1}}


ACTIONS = {"demo_look": look}
'''

# 假插件: 声称把一条文件恢复了 (from/to)
RESTORE_SOURCE = '''
import os


def look(room=None):
    src = os.environ.get("VERIFY_SRC", "")
    dst = os.environ.get("VERIFY_DST", "")
    return {"restored": [{"from": src, "to": dst, "size": 1}],
            "receipt": "rc-undo-1", "verified": {"checked": 1, "ok": 1}}


ACTIONS = {"demo_look": look}
'''

# 假插件: 记账类. 可以真写账本 (VERIFY_WRITE=1) 也可以只报路径 (0, 用来测"账本里没有这条")
LEDGER_SOURCE = '''
import json
import os
from pathlib import Path


def look(room=None):
    ledger = Path(os.environ["VERIFY_LEDGER"])
    entry_id = os.environ.get("VERIFY_ENTRY_ID", "e-1")
    if os.environ.get("VERIFY_WRITE", "1") == "1":
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"id": entry_id}) + "\\n")
    return {"id": entry_id, "entry": {"id": entry_id}, "ledger_path": str(ledger),
            "receipt": "rc-ledger-1", "verified": {"checked": 1, "ok": 1}}


ACTIONS = {"demo_look": look}
'''

# 假插件: 撤销类. 报 entry_id + removed, 宿主该看账本里还有没有这条
UNDO_LEDGER_SOURCE = '''
import os


def look(room=None):
    ledger = os.environ["VERIFY_LEDGER"]
    entry_id = os.environ.get("VERIFY_ENTRY_ID", "e-1")
    return {"entry_id": entry_id, "removed": 1, "ledger_path": ledger,
            "receipt": "rc-ledger-undo-1", "verified": {"checked": 1, "ok": 1}}


ACTIONS = {"demo_look": look}
'''

# 假插件: 宿主看不懂的世界 (没有 moved/restored/ledger_path), 但自证全过
UNKNOWN_SOURCE = '''
def look(room=None):
    return {"seen": ["a"], "receipt": "rc-unknown-1", "verified": {"checked": 1, "ok": 1}}


ACTIONS = {"demo_look": look}
'''

# 假插件: 看着像文件清单但每条缺路径 → 宿主看不懂, 记 null
MALFORMED_SOURCE = '''
def look(room=None):
    return {"moved": [{"src": "a.jpg"}], "receipt": "rc-bad-1",
            "verified": {"checked": 1, "ok": 1}}


ACTIONS = {"demo_look": look}
'''

# 假插件: 没有回执、没有自证
QUIET_SOURCE = '''
def look(room=None):
    return {"seen": ["a"]}


ACTIONS = {"demo_look": look}
'''

FAIL_SOURCE = '''
def look(room=None):
    raise RuntimeError("炸了")


ACTIONS = {"demo_look": look}
'''


@pytest.fixture
def host(tmp_path, monkeypatch):
    """插件宿主开着 + 宿主核对开着, 插件目录指到临时目录."""
    monkeypatch.setenv(ph.SWITCH_ENV, "on")
    monkeypatch.setenv(ph.HOME_ENV, str(tmp_path / "plugins"))
    monkeypatch.setenv(ph.VERIFY_ENV, "on")
    return tmp_path / "plugins"


@pytest.fixture
def host_off_verify(tmp_path, monkeypatch):
    """插件宿主开着, 但宿主核对按默认关着."""
    monkeypatch.setenv(ph.SWITCH_ENV, "on")
    monkeypatch.setenv(ph.HOME_ENV, str(tmp_path / "plugins"))
    monkeypatch.delenv(ph.VERIFY_ENV, raising=False)
    return tmp_path / "plugins"


def _install_look(tmp_path, source):
    return install(tmp_path, manifest_obj=demo_manifest(LOOK_ACTIONS), source=source)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    return path


def _rows(root):
    return audit_rows(root)


# ---- 开关关着: 老路径一点没变 ----

def test_verify_off_by_default_adds_no_key_no_event(host_off_verify, tmp_path, monkeypatch):
    plugin_id = _install_look(tmp_path, MOVE_SOURCE)
    src = _touch(tmp_path / "tree" / "a.jpg")
    monkeypatch.setenv("VERIFY_SRC", str(src))
    monkeypatch.setenv("VERIFY_DST", str(tmp_path / "tree" / "images" / "a.jpg"))  # 根本没落地

    out = ph.invoke(plugin_id, "demo_look", {})
    assert set(out) == RESULT_KEYS              # 没有 host_verified / warnings
    assert out["ok"] is True                    # 插件自证为准, 宿主不翻它
    rows = _rows(ph.home())
    assert len(rows) == 1 and rows[0]["action"] == "invoke"
    assert "host_verified" not in rows[0] and "warnings" not in rows[0]
    raw = (ph.home() / "audit.jsonl").read_text(encoding="utf-8")
    assert "plugin.receipt" not in raw


def test_verify_off_keeps_a_failed_call_audited_as_before(host_off_verify, tmp_path):
    plugin_id = _install_look(tmp_path, FAIL_SOURCE)
    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["ok"] is False and out["error"]["code"] == "E_CRASH"
    rows = _rows(ph.home())
    assert len(rows) == 1 and rows[0]["action"] == "invoke" and rows[0]["ok"] is False
    assert "host_verified" not in rows[0]


# ---- 文件整理类 ----

def test_verify_true_when_the_files_really_moved(host, tmp_path, monkeypatch):
    plugin_id = _install_look(tmp_path, MOVE_SOURCE)
    src = _touch(tmp_path / "tree" / "a.jpg")
    dst = _touch(tmp_path / "tree" / "images" / "a.jpg")
    src.unlink()  # 世界真的变了: 源没了, 目标在
    monkeypatch.setenv("VERIFY_SRC", str(src))
    monkeypatch.setenv("VERIFY_DST", str(dst))

    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["ok"] is True
    assert out["host_verified"] is True
    assert "warnings" not in out
    rows = _rows(ph.home())
    invoke = next(row for row in rows if row["action"] == "invoke")
    assert invoke["verified"] is True and invoke["host_verified"] is True
    assert invoke["warnings"] == []


def test_verify_false_when_the_target_never_landed(host, tmp_path, monkeypatch):
    plugin_id = _install_look(tmp_path, MOVE_SOURCE)
    src = _touch(tmp_path / "tree" / "a.jpg")
    monkeypatch.setenv("VERIFY_SRC", str(src))
    monkeypatch.setenv("VERIFY_DST", str(tmp_path / "tree" / "images" / "a.jpg"))  # 不在

    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["ok"] is True                    # 先观测: 不把这种插件直接判失败
    assert out["host_verified"] is False
    warnings = out["warnings"]
    assert len(warnings) == 1
    assert warnings[0]["code"] == DISAGREE
    assert warnings[0]["plugin_verified"] is True and warnings[0]["host_verified"] is False
    rows = _rows(ph.home())
    invoke = next(row for row in rows if row["action"] == "invoke")
    assert invoke["host_verified"] is False
    assert invoke["warnings"] == [DISAGREE]


def test_verify_false_when_the_source_survived(host, tmp_path, monkeypatch):
    plugin_id = _install_look(tmp_path, MOVE_SOURCE)
    src = _touch(tmp_path / "tree" / "a.jpg")          # 源还在 (复制了一份说移动了)
    dst = _touch(tmp_path / "tree" / "images" / "a.jpg")
    monkeypatch.setenv("VERIFY_SRC", str(src))
    monkeypatch.setenv("VERIFY_DST", str(dst))

    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["host_verified"] is False
    assert out["warnings"][0]["code"] == DISAGREE


def test_verify_undo_checks_the_restored_pair(host, tmp_path, monkeypatch):
    plugin_id = _install_look(tmp_path, RESTORE_SOURCE)
    from_path = tmp_path / "tree" / "images" / "a.jpg"   # 已经不在
    to_path = _touch(tmp_path / "tree" / "a.jpg")        # 回来了
    monkeypatch.setenv("VERIFY_SRC", str(from_path))
    monkeypatch.setenv("VERIFY_DST", str(to_path))

    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["host_verified"] is True and "warnings" not in out


# ---- 看不懂的世界: null ----

def test_verify_is_null_for_a_world_the_host_does_not_understand(host, tmp_path):
    plugin_id = _install_look(tmp_path, UNKNOWN_SOURCE)
    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["ok"] is True
    assert out["host_verified"] is None
    assert "warnings" not in out                # null 不是"不一致", 不报
    rows = _rows(ph.home())
    invoke = next(row for row in rows if row["action"] == "invoke")
    assert invoke["host_verified"] is None and invoke["verified"] is True


def test_verify_is_null_when_the_move_entries_are_malformed(host, tmp_path):
    plugin_id = _install_look(tmp_path, MALFORMED_SOURCE)
    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["host_verified"] is None
    assert "warnings" not in out


# ---- 记账类: 账本路径必须落在插件自己的目录里 ----

def test_verify_reads_the_ledger_inside_the_plugin_dir(host, tmp_path, monkeypatch):
    plugin_id = _install_look(tmp_path, LEDGER_SOURCE)
    ledger = ph.home() / plugin_id / "data" / "ledger.jsonl"
    monkeypatch.setenv("VERIFY_LEDGER", str(ledger))
    monkeypatch.setenv("VERIFY_ENTRY_ID", "e-1")

    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["ok"] is True and out["host_verified"] is True
    assert "warnings" not in out
    assert json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])["id"] == "e-1"


def test_verify_refuses_a_ledger_outside_the_plugin_dir(host, tmp_path, monkeypatch):
    plugin_id = _install_look(tmp_path, LEDGER_SOURCE)
    outside = _touch(tmp_path / "outside" / "ledger.jsonl")
    outside.write_text(json.dumps({"id": "e-1"}) + "\n", encoding="utf-8")  # 真有这条也不许读
    monkeypatch.setenv("VERIFY_LEDGER", str(outside))
    monkeypatch.setenv("VERIFY_ENTRY_ID", "e-1")
    monkeypatch.setenv("VERIFY_WRITE", "0")

    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["host_verified"] is None          # 拒读 = 不做判断, 不是 false
    assert "warnings" not in out


def test_verify_false_when_the_ledger_has_no_such_entry(host, tmp_path, monkeypatch):
    plugin_id = _install_look(tmp_path, LEDGER_SOURCE)
    ledger = ph.home() / plugin_id / "ledger.jsonl"
    ledger.write_text(json.dumps({"id": "other"}) + "\n", encoding="utf-8")
    monkeypatch.setenv("VERIFY_LEDGER", str(ledger))
    monkeypatch.setenv("VERIFY_ENTRY_ID", "e-1")
    monkeypatch.setenv("VERIFY_WRITE", "0")

    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["host_verified"] is False
    assert out["warnings"][0]["code"] == DISAGREE
    rows = _rows(ph.home())
    invoke = next(row for row in rows if row["action"] == "invoke")
    assert invoke["warnings"] == [DISAGREE]
    assert rows[-1]["event"] == ph.RECEIPT_EVENT


def test_verify_undo_wants_the_entry_gone(host, tmp_path, monkeypatch):
    plugin_id = _install_look(tmp_path, UNDO_LEDGER_SOURCE)
    ledger = ph.home() / plugin_id / "ledger.jsonl"
    monkeypatch.setenv("VERIFY_LEDGER", str(ledger))
    monkeypatch.setenv("VERIFY_ENTRY_ID", "e-2")

    ledger.write_text(json.dumps({"id": "其它"}) + "\n", encoding="utf-8")
    gone = ph.invoke(plugin_id, "demo_look", {})
    assert gone["host_verified"] is True

    ledger.write_text(json.dumps({"id": "e-2"}) + "\n", encoding="utf-8")
    survived = ph.invoke(plugin_id, "demo_look", {})
    assert survived["host_verified"] is False
    assert survived["warnings"][0]["code"] == DISAGREE


def test_verify_is_null_when_the_ledger_cannot_be_parsed(host, tmp_path, monkeypatch):
    plugin_id = _install_look(tmp_path, LEDGER_SOURCE)
    ledger = ph.home() / plugin_id / "ledger.jsonl"
    ledger.write_text("{ not json\n", encoding="utf-8")
    monkeypatch.setenv("VERIFY_LEDGER", str(ledger))
    monkeypatch.setenv("VERIFY_ENTRY_ID", "e-1")
    monkeypatch.setenv("VERIFY_WRITE", "0")

    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["host_verified"] is None


# ---- 回执进事件流 ----

def test_receipt_event_is_one_structured_line_in_the_audit_channel(host, tmp_path, monkeypatch):
    plugin_id = _install_look(tmp_path, MOVE_SOURCE)
    src = _touch(tmp_path / "tree" / "a.jpg")
    dst = _touch(tmp_path / "tree" / "images" / "a.jpg")
    src.unlink()
    monkeypatch.setenv("VERIFY_SRC", str(src))
    monkeypatch.setenv("VERIFY_DST", str(dst))

    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["host_verified"] is True
    rows = _rows(ph.home())
    assert [row["action"] for row in rows] == ["invoke", "event"]
    event = rows[-1]
    assert event["event"] == ph.RECEIPT_EVENT
    assert event["plugin_id"] == plugin_id
    assert event["tool"] == "demo_look"
    assert event["receipt_id"] == out["receipt_id"] == "rc-move-1"
    assert event["verified"] is True and event["host_verified"] is True
    assert event["by"] == "local_user" and event["at"]
    raw = (ph.home() / "audit.jsonl").read_text(encoding="utf-8")
    assert str(tmp_path) not in raw              # 审计/事件不抄插件返回的路径原文
    assert "images/a.jpg" not in raw


def test_no_receipt_event_for_a_call_without_a_receipt(host, tmp_path):
    plugin_id = _install_look(tmp_path, QUIET_SOURCE)
    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["ok"] is True
    rows = _rows(ph.home())
    assert [row["action"] for row in rows] == ["invoke"]
    assert out["host_verified"] is None


def test_no_receipt_event_when_the_call_failed(host, tmp_path):
    plugin_id = _install_look(tmp_path, FAIL_SOURCE)
    out = ph.invoke(plugin_id, "demo_look", {})
    assert out["ok"] is False
    rows = _rows(ph.home())
    assert [row["action"] for row in rows] == ["invoke"]


def test_verify_on_records_null_for_a_rejected_call(host, tmp_path):
    out = ph.invoke("demo-tool", "demo_look", {})   # 没装
    assert out["ok"] is False and out["error"]["code"] == "E_NOT_INSTALLED"
    assert out["host_verified"] is None
    rows = _rows(ph.home())
    assert rows[-1]["action"] == "invoke" and rows[-1]["host_verified"] is None


# ---- 真实插件: 宿主核对对真账本/真文件也成立 ----

def _install_real(package_name):
    result = ph.install(REPO / "plugins" / package_name)
    assert result["ok"] is True, result
    plugin_id = result["id"]
    assert ph.set_enabled(plugin_id, True)["ok"] is True
    granted = ph.grant_tier(plugin_id, "panel", why="第五切片测试")
    assert granted["ok"] is True, granted
    return plugin_id


def test_real_bookkeeper_add_and_undo_are_host_verified(host, tmp_path, monkeypatch):
    plugin_id = _install_real("bookkeeper")
    # 账本放到插件自己的目录里 (真实默认就是 ~/.worldmodel/plugins/bookkeeper)
    monkeypatch.setenv("WM_BOOK_HOME", str(ph.home() / plugin_id))
    monkeypatch.setenv("WM_BOOK_NOW", "2026-11-19T12:00")
    added = ph.invoke(plugin_id, "book_add", {"amount": 12.5, "category": "餐饮", "note": "面"})
    assert added["ok"] is True, added
    assert added["host_verified"] is True and "warnings" not in added
    undone = ph.invoke(plugin_id, "book_undo", {"receipt_id": added["receipt_id"]})
    assert undone["ok"] is True, undone
    assert undone["host_verified"] is True


def test_real_filesorter_move_is_host_verified_on_the_file_system(host, tmp_path, monkeypatch):
    plugin_id = _install_real("filesorter")
    monkeypatch.setenv("WM_FILESORTER_HOME", str(tmp_path / "fs-home"))
    monkeypatch.setenv("WM_BLACKLIST_HOME", str(tmp_path / "guard"))
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.jpg").write_text("1", encoding="utf-8")
    (work / "b.pdf").write_text("2", encoding="utf-8")

    out = ph.invoke(plugin_id, "filesorter_move", {"dir": str(work), "confirm": True})
    assert out["ok"] is True, out
    assert out["host_verified"] is True and "warnings" not in out
    assert (work / "images" / "a.jpg").is_file() and not (work / "a.jpg").exists()

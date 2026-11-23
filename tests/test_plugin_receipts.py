"""平台化第四切片: 回执核对 (receipt verification).

两半都在这里测:

- 插件自证: filesorter 移动/撤销后回文件系统核对 (目标在、源不在), bookkeeper 记账/撤销后回读账本核对;
  对不上时 `ok=false` + `E_RECEIPT_MISMATCH`, 但仍把已成功的清单和回执带回来 (undo 要用)。
- 宿主如实记: `audit.jsonl` 里每次 invoke 一行, `verified` 三态 (true / false / null); 插件报
  `E_RECEIPT_MISMATCH` 时这次调用翻到失败侧, 但 result 保留; 关开关时一行盘都不碰。

文件、账本、回执、BlacklistStore 全在 tmp_path 里造; 不碰真 `~/.worldmodel`, 不起真服务。
"""
import importlib.util
import json
import os
from pathlib import Path

import pytest

from wm.agent import plugin_host as ph
from wm.agent.governance import BlacklistStore

from test_plugin_host_actions import audit_rows, demo_manifest, install

REPO = Path(__file__).resolve().parents[1]
RESULT_KEYS = {"ok", "action", "result", "error", "receipt_id"}

MISMATCH_ACTIONS = [{
    "name": "demo_look",
    "kind": "query",
    "why": "假装核对没通过",
    "params": {"room": {"type": "string", "required": True}},
}]

# 插件自己说"做成了, 但世界对不上": 宿主应该只看它报的码, 不替它核对、也不替它圆。
MISMATCH_SOURCE = '''
def look(room):
    return {"ok": False, "code": "E_RECEIPT_MISMATCH",
            "message": "移动后核对没通过：secret.pdf 没到位",
            "verified": {"checked": 2, "ok": 1, "missing": ["secret.pdf"]},
            "moved": [{"src": "secret.pdf"}], "receipt": "rc-mismatch-1"}


ACTIONS = {"demo_look": look}
'''


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fs = _load(REPO / "plugins" / "filesorter" / "plugin.py", "filesorter_receipt_plugin")
bk = _load(REPO / "plugins" / "bookkeeper" / "plugin.py", "bookkeeper_receipt_plugin")


def _jsonl(path):
    path = Path(path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def make_files(root: Path, spec: dict) -> None:
    for name, data in spec.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode("utf-8"))


@pytest.fixture
def sort_rig(tmp_path, monkeypatch):
    monkeypatch.setenv(fs.SWITCH_ENV, "on")
    tree = tmp_path / "tree"
    tree.mkdir()
    return {"root": tree, "home": tmp_path / "fh", "blacklist": tmp_path / "bh"}


def do_confirm(rig):
    return fs.confirm_move(rig["root"], home=rig["home"], blacklist_home=rig["blacklist"])


def do_undo(rig, receipt_id):
    return fs.undo(receipt_id, home=rig["home"])


def protect(rig, path: Path) -> None:
    BlacklistStore(rig["blacklist"]).add(
        {"kind": "path", "value": str(path), "operations": "all", "reason": "测试保护"}, by="user")


def receipt_of(rig, result) -> dict:
    return next(item for item in _jsonl(rig["home"] / fs.RECEIPTS_NAME)
                if item["id"] == result["receipt"])


@pytest.fixture
def book_rig(tmp_path, monkeypatch):
    monkeypatch.setenv(bk.SWITCH_ENV, "on")
    monkeypatch.setenv(bk.NOW_ENV, "2026-11-19T12:00")
    return tmp_path / "book-home"


# ---- filesorter: 移动 ----

def test_move_receipt_verifies_every_file(sort_rig):
    make_files(sort_rig["root"], {"a.jpg": "1", "b.pdf": "2"})
    out = do_confirm(sort_rig)
    assert out["ok"] is True
    assert out["verified"] == {"checked": 2, "ok": 2, "missing": []}
    assert out["verified"]["ok"] == out["verified"]["checked"]
    assert out["files_moved"] == 2
    assert receipt_of(sort_rig, out)["verified"] == out["verified"]
    assert (sort_rig["root"] / "images" / "a.jpg").is_file()
    assert not (sort_rig["root"] / "a.jpg").exists()


def test_move_reports_files_that_did_not_land(sort_rig, monkeypatch):
    make_files(sort_rig["root"], {"a.jpg": "1", "b.pdf": "2"})
    real_rename = os.rename

    def rename_then_lose_target(src, dst):
        real_rename(src, dst)
        if Path(dst).name == "a.jpg":
            os.remove(dst)

    monkeypatch.setattr(os, "rename", rename_then_lose_target)
    out = do_confirm(sort_rig)
    assert out["ok"] is False
    assert out["code"] == fs.RECEIPT_MISMATCH
    assert "a.jpg" in out["message"]
    assert out["verified"]["checked"] == 2
    assert out["verified"]["ok"] == 1
    assert out["verified"]["missing"] == [str(sort_rig["root"] / "images" / "a.jpg")]
    assert len(out["moved"]) == 2   # 已成功的清单还在: 没有它 undo 没法用
    assert out["receipt"]
    assert receipt_of(sort_rig, out)["verified"] == out["verified"]


def test_skipped_files_are_not_counted_in_checks(sort_rig):
    make_files(sort_rig["root"], {"a.jpg": "1", "b.pdf": "2", ".secret": "3"})
    protect(sort_rig, sort_rig["root"] / "a.jpg")
    out = do_confirm(sort_rig)
    assert out["ok"] is True
    assert out["verified"] == {"checked": 1, "ok": 1, "missing": []}
    assert out["verified"]["checked"] == len(out["moved"]) == 1
    skipped_paths = {item.get("path") for item in out["skipped"]}
    assert str(sort_rig["root"] / ".secret") in skipped_paths
    assert str(sort_rig["root"] / "a.jpg") in skipped_paths


# ---- filesorter: 撤销 ----

def test_undo_receipt_verifies_files_are_back(sort_rig):
    make_files(sort_rig["root"], {"a.jpg": "1", "b.pdf": "2"})
    moved = do_confirm(sort_rig)
    out = do_undo(sort_rig, moved["receipt"])
    assert out["ok"] is True
    assert out["verified"] == {"checked": 2, "ok": 2, "missing": []}
    receipt = next(item for item in _jsonl(sort_rig["home"] / fs.RECEIPTS_NAME)
                   if item["id"] == out["receipt"])
    assert receipt["kind"] == "undo"
    assert receipt["verified"] == out["verified"]
    assert (sort_rig["root"] / "a.jpg").is_file()
    assert not (sort_rig["root"] / "images" / "a.jpg").exists()


def test_undo_reports_files_that_did_not_come_back(sort_rig, monkeypatch):
    make_files(sort_rig["root"], {"a.jpg": "1"})
    moved = do_confirm(sort_rig)
    monkeypatch.setattr(os, "rename", lambda src, dst: None)  # 说移回了, 其实一个都没动
    out = do_undo(sort_rig, moved["receipt"])
    assert out["ok"] is False
    assert out["code"] == fs.RECEIPT_MISMATCH
    assert out["verified"] == {"checked": 1, "ok": 0,
                               "missing": [str(sort_rig["root"] / "a.jpg")]}
    assert "a.jpg" in out["message"]
    assert out["restored"]        # 声称移回的清单还在
    assert out["receipt"]


# ---- bookkeeper: 记一笔 ----

def test_add_verifies_the_entry_is_in_the_ledger(book_rig):
    home = book_rig
    out = bk.add(12.5, "餐饮", note="面", home=home)
    assert out["ok"] is True, out
    assert out["verified"] == {"checked": 1, "ok": 1}
    assert [entry["id"] for entry in _jsonl(home / bk.LEDGER_NAME)] == [out["id"]]
    receipt = next(item for item in _jsonl(home / bk.RECEIPTS_NAME) if item["id"] == out["receipt"])
    assert receipt["verified"] == out["verified"]


def test_add_write_failure_is_a_receipt_mismatch(book_rig, monkeypatch):
    home = book_rig
    real_append = bk._append_jsonl

    def refuse_ledger(path, record):
        if Path(path).name == bk.LEDGER_NAME:
            raise OSError("磁盘满了")
        return real_append(path, record)

    monkeypatch.setattr(bk, "_append_jsonl", refuse_ledger)
    out = bk.add(9.9, "交通", home=home)
    assert out["ok"] is False
    assert out["code"] == bk.RECEIPT_MISMATCH
    assert out["verified"] == {"checked": 1, "ok": 0}
    assert "账本写不进去" in out["reason"]
    assert not (home / bk.RECEIPTS_NAME).exists()   # 没落盘就不假装有回执可撤


def test_add_row_vanishing_after_write_is_a_receipt_mismatch(book_rig, monkeypatch):
    home = book_rig
    real_append = bk._append_jsonl

    def append_then_wipe_ledger(path, record):
        saved = real_append(path, record)
        if Path(path).name == bk.LEDGER_NAME:
            Path(path).write_text("", encoding="utf-8")
        return saved

    monkeypatch.setattr(bk, "_append_jsonl", append_then_wipe_ledger)
    out = bk.add(9.9, "交通", home=home)
    assert out["ok"] is False
    assert out["code"] == bk.RECEIPT_MISMATCH
    assert out["verified"] == {"checked": 1, "ok": 0}
    assert out["receipt"]
    receipts = _jsonl(home / bk.RECEIPTS_NAME)
    assert receipts[-1]["id"] == out["receipt"]
    assert receipts[-1]["verified"] == {"checked": 1, "ok": 0}


# ---- bookkeeper: 撤销 ----

def test_undo_verifies_the_entry_is_gone(book_rig):
    home = book_rig
    added = bk.add(20, "购物", note="书", home=home)
    out = bk.undo(added["receipt"], home=home)
    assert out["ok"] is True
    assert out["verified"] == {"checked": 1, "ok": 1}
    assert out["removed"] == 1
    assert [entry for entry in _jsonl(home / bk.LEDGER_NAME) if entry["id"] == added["id"]] == []


def test_undo_reports_an_entry_that_survived(book_rig, monkeypatch):
    home = book_rig
    added = bk.add(20, "购物", home=home)
    monkeypatch.setattr(bk, "_remove_entry", lambda home_dir, entry_id: 1)  # 说删了, 其实没删
    out = bk.undo(added["receipt"], home=home)
    assert out["ok"] is False
    assert out["code"] == bk.RECEIPT_MISMATCH
    assert out["verified"] == {"checked": 1, "ok": 0}
    assert added["id"] in out["message"]
    assert any(entry["id"] == added["id"] for entry in _jsonl(home / bk.LEDGER_NAME))


def test_summary_stays_a_query_without_verified(book_rig):
    home = book_rig
    bk.add(30, "娱乐", home=home)
    out = bk.summary("今天", home=home)
    assert out["ok"] is True
    assert "verified" not in out


# ---- 关开关: 行为和之前一样, 不碰盘 ----

def test_switch_off_still_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(fs.SWITCH_ENV, raising=False)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.jpg").write_text("1", encoding="utf-8")

    moved = fs.confirm_move(tree, home=tmp_path / "fh", blacklist_home=tmp_path / "bh")
    assert moved == {"enabled": False, "ok": False, "reason": fs._OFF_REASON}
    assert (tree / "a.jpg").is_file()

    added = bk.add(10, "餐饮", home=tmp_path / "book")
    assert added == {"enabled": False, "ok": False, "reason": bk._OFF_REASON}
    assert not (tmp_path / "book").exists()
    assert not (tmp_path / "fh").exists()


# ---- 宿主: 审计三态 + 错误码归属 ----

@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv(ph.SWITCH_ENV, "on")
    monkeypatch.setenv(ph.HOME_ENV, str(tmp_path / "plugins"))
    return tmp_path / "plugins"


def _install_real(tmp_path, package_name):
    result = ph.install(REPO / "plugins" / package_name)
    assert result["ok"] is True, result
    plugin_id = result["id"]
    assert ph.set_enabled(plugin_id, True)["ok"] is True
    granted = ph.grant_tier(plugin_id, "panel", why="第四切片测试")
    assert granted["ok"] is True, granted
    return plugin_id


def test_real_filesorter_move_is_verified_and_audited(host, tmp_path, monkeypatch):
    monkeypatch.setenv("WM_FILESORTER_HOME", str(tmp_path / "fs-home"))
    monkeypatch.setenv("WM_BLACKLIST_HOME", str(tmp_path / "guard"))
    plugin_id = _install_real(tmp_path, "filesorter")
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.jpg").write_text("1", encoding="utf-8")
    (work / "b.pdf").write_text("2", encoding="utf-8")

    out = ph.invoke(plugin_id, "filesorter_move", {"dir": str(work), "confirm": True})
    assert set(out) == RESULT_KEYS
    assert out["ok"] is True, out
    assert out["result"]["verified"] == {"checked": 2, "ok": 2, "missing": []}
    assert out["receipt_id"] == out["result"]["receipt"]
    rows = audit_rows(ph.home())
    assert rows[-1]["action"] == "invoke"
    assert rows[-1]["verified"] is True
    assert rows[-1]["receipt_id"] == out["receipt_id"]
    assert (work / "images" / "a.jpg").is_file()
    assert not (work / "a.jpg").exists()


def test_real_bookkeeper_add_is_verified_and_audited(host, tmp_path, monkeypatch):
    monkeypatch.setenv("WM_BOOK_HOME", str(tmp_path / "book-home"))
    monkeypatch.setenv("WM_BOOK_NOW", "2026-11-19T12:00")
    plugin_id = _install_real(tmp_path, "bookkeeper")

    out = ph.invoke(plugin_id, "book_add", {"amount": 12.5, "category": "餐饮", "note": "面"})
    assert out["ok"] is True, out
    assert out["result"]["verified"] == {"checked": 1, "ok": 1}
    rows = audit_rows(ph.home())
    assert rows[-1]["action"] == "invoke"
    assert rows[-1]["verified"] is True
    assert rows[-1]["tool"] == "book_add"


def test_receipt_mismatch_is_a_failure_but_result_survives(host, tmp_path):
    plugin_id = install(tmp_path, manifest_obj=demo_manifest(MISMATCH_ACTIONS),
                        source=MISMATCH_SOURCE)
    out = ph.invoke(plugin_id, "demo_look", {"room": "书房"})
    assert set(out) == RESULT_KEYS
    assert out["ok"] is False
    assert out["error"]["code"] == "E_RECEIPT_MISMATCH"
    assert out["error"]["code"] != "E_CRASH"
    assert out["result"]["moved"] == [{"src": "secret.pdf"}]   # undo 还要靠它
    assert out["receipt_id"] == "rc-mismatch-1"
    rows = audit_rows(ph.home())
    assert rows[-1]["action"] == "invoke"
    assert rows[-1]["verified"] is False
    assert rows[-1]["error_code"] == "E_RECEIPT_MISMATCH"
    raw = (ph.home() / "audit.jsonl").read_text(encoding="utf-8")
    assert "secret.pdf" not in raw      # 审计不抄插件返回原文
    assert "没到位" not in raw


def test_plugin_without_verified_is_audited_as_null(host, tmp_path):
    plugin_id = install(tmp_path)   # 默认 demo 包, 返回里没有 verified
    out = ph.invoke(plugin_id, "demo_look", {"room": "书房"})
    assert out["ok"] is True
    assert "verified" not in out["result"]
    rows = audit_rows(ph.home())
    assert rows[-1]["action"] == "invoke"
    assert rows[-1]["verified"] is None


def test_switch_off_writes_no_audit(tmp_path, monkeypatch):
    monkeypatch.setenv(ph.HOME_ENV, str(tmp_path / "plugins"))
    monkeypatch.delenv(ph.SWITCH_ENV, raising=False)
    out = ph.invoke("demo-tool", "demo_look", {"room": "x"})
    assert out["ok"] is False
    assert out["error"]["code"] == "E_HOST_OFF"
    assert not (tmp_path / "plugins").exists()

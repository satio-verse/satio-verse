"""第二个生活插件「记账」走插件宿主执行器的真实演练 (平台化第三步, 任务 ③).

用仓库里的 `plugins/bookkeeper` 真装进临时 `WM_PLUGIN_HOME`, 账本和回执都指到临时目录
(`WM_BOOK_HOME`), 不碰真的 `~/.worldmodel`; `WM_BOOK_NOW` 固定"现在", 期间过滤才是确定的。
金额精确性 (0.1 + 0.2 = 0.30) 和坏行容错是这一组测试的重点。
"""
import json
from pathlib import Path

import pytest

from wm.agent import plugin_host as ph
from wm.agent import plugin_manifest as pm
from wm.agent.studio import StudioRuntime

PACKAGE = Path(__file__).resolve().parents[1] / "plugins" / "bookkeeper"
NOW = "2026-11-19T12:00"  # 2026-11-19 是星期四, 本周 = 11-16 ~ 11-22


@pytest.fixture
def drill(tmp_path, monkeypatch):
    """宿主开关打开; 插件目录、账本目录全部指到 tmp_path; "现在"固定."""
    monkeypatch.setenv(ph.SWITCH_ENV, "on")
    monkeypatch.setenv(ph.HOME_ENV, str(tmp_path / "plugins"))
    monkeypatch.setenv("WM_BOOK_HOME", str(tmp_path / "book-home"))
    monkeypatch.setenv("WM_BOOK_NOW", NOW)
    return tmp_path


def installed(grant=True):
    result = ph.install(PACKAGE)
    assert result["ok"] is True, result
    assert result["granted_tier"] == "text"  # 不信任清单自称的 panel 档, 一律从 text 起步
    assert ph.set_enabled("bookkeeper", True)["ok"] is True
    if grant:
        assert ph.grant_tier("bookkeeper", "panel", why="演练：记账")["ok"] is True
    return result


def add(amount, category, note=None, at=None):
    """走宿主 invoke 记一笔. 返回宿主统一结构 (5 键)."""
    params = {"amount": amount, "category": category}
    if note is not None:
        params["note"] = note
    if at is not None:
        params["at"] = at
    return ph.invoke("bookkeeper", "book_add", params)


def summary(period):
    return ph.invoke("bookkeeper", "book_summary", {"period": period})


def undo(receipt_id):
    return ph.invoke("bookkeeper", "book_undo", {"receipt_id": receipt_id})


def book_home(drill):
    return drill / "book-home"


def ledger_lines(drill):
    path = book_home(drill) / "ledger.jsonl"
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def manifest_actions():
    manifest = json.loads((PACKAGE / "wmworld.json").read_text(encoding="utf-8"))
    return {action["name"]: action for action in manifest["plugin"]["world"]["actions"]}


# ---- 清单与工具面 ----

def test_the_manifest_passes_the_frozen_contract_and_declares_panel_tier():
    manifest = json.loads((PACKAGE / "wmworld.json").read_text(encoding="utf-8"))
    assert pm.validate_plugin_manifest(manifest, package_dir=PACKAGE) == []
    assert pm.policy_issues(manifest, "third_party") == []
    assert manifest["plugin"]["tier"] == "panel"
    world = manifest["plugin"]["world"]
    assert world["kind"] == "space" and world["prefix"] == "book"
    assert [(a["name"], a["kind"]) for a in world["actions"]] == [
        ("book_add", "act"), ("book_summary", "query"), ("book_undo", "act")]


def test_query_runs_at_text_tier_but_act_needs_grant(drill):
    installed(grant=False)
    assert "book_summary" in ph.tool_names()
    assert "book_add" not in ph.tool_names() and "book_undo" not in ph.tool_names()
    empty = summary("今天")
    assert empty["ok"] is True and empty["result"]["total"] == "0.00"
    assert empty["result"]["count"] == 0 and empty["result"]["categories"] == {}
    assert empty["receipt_id"] is None
    refused = add(12.3, "餐饮")
    assert refused["ok"] is False and refused["error"]["code"] == "E_NEEDS_GRANT"
    assert "需要你先授权" in refused["error"]["message"]
    assert not (book_home(drill) / "ledger.jsonl").exists()


def test_add_three_entries_summary_and_undo(drill):
    installed()
    first = add(10, "餐饮", at="2026-11-19")
    second = add(2.5, "交通", note="地铁", at="2026-11-18")
    third = add(100, "购物", at="2026-11-03")
    for out in (first, second, third):
        assert out["ok"] is True, out
        assert out["receipt_id"] == out["result"]["receipt"]
        assert out["receipt_id"].startswith("rc-")
    assert first["result"]["amount"] == "10.00" and first["result"]["entry"]["amount"] == "10.00"

    month = summary("本月")
    assert month["ok"] is True, month
    result = month["result"]
    assert result["total"] == "112.50" and result["count"] == 3
    assert result["categories"] == {
        "购物": {"amount": "100.00", "count": 1},
        "餐饮": {"amount": "10.00", "count": 1},
        "交通": {"amount": "2.50", "count": 1},
    }
    assert result["start"] == "2026-11-01" and result["end"] == "2026-11-30"

    # undo: 整笔撤销第二笔 (交通), 账本只删这一行
    entry_id = second["result"]["id"]
    undone = undo(second["receipt_id"])
    assert undone["ok"] is True, undone
    assert undone["result"]["removed"] == 1 and undone["result"]["already_undone"] is False
    assert undone["receipt_id"] == undone["result"]["receipt"]
    lines = ledger_lines(drill)
    assert len(lines) == 2 and entry_id not in "\n".join(lines)
    assert summary("本月")["result"]["total"] == "110.00"
    assert summary("本月")["result"]["count"] == 2

    # 再 undo 一次: 幂等, 不报错、不重复改账
    again = undo(second["receipt_id"])
    assert again["ok"] is True and again["result"]["removed"] == 0
    assert again["result"]["already_undone"] is True
    assert len(ledger_lines(drill)) == 2


def test_summary_filters_by_period(drill):
    installed()
    rows = ((10, "餐饮", "2026-11-19"), (2.5, "交通", "2026-11-18"),
            (5, "娱乐", "2026-11-15"), (100, "购物", "2026-11-03"), (0.55, "医疗", "2026-10-20"))
    for amount, category, at in rows:
        assert add(amount, category, at=at)["ok"] is True
    expected = {"今天": ("10.00", 1), "本周": ("12.50", 2),
                "本月": ("117.50", 4), "全部": ("118.05", 5)}
    for period, (total, count) in expected.items():
        out = summary(period)
        assert out["ok"] is True, out
        assert out["result"]["total"] == total, (period, out["result"])
        assert out["result"]["count"] == count, (period, out["result"])


def test_summary_groups_by_category_with_amounts_and_counts(drill):
    installed()
    add(0.1, "餐饮", at="2026-11-19")
    add(0.2, "餐饮", at="2026-11-18")
    add(300, "居住", at="2026-11-01")
    result = summary("本月")["result"]
    assert result["categories"] == {
        "居住": {"amount": "300.00", "count": 1},
        "餐饮": {"amount": "0.30", "count": 2},
    }
    # 分组金额相加等于合计, 不丢分
    assert result["categories"]["餐饮"]["amount"] == "0.30"


def test_summary_is_exact_no_float_drift(drill):
    installed()
    add(0.1, "餐饮")
    add(0.2, "餐饮")
    add(1.1, "交通")
    add(2.2, "交通")
    result = summary("今天")["result"]
    assert result["total"] == "3.60"
    assert result["categories"]["餐饮"] == {"amount": "0.30", "count": 2}
    assert result["categories"]["交通"] == {"amount": "3.30", "count": 2}


def test_empty_ledger_summary_is_zero(drill):
    installed()
    result = summary("全部")["result"]
    assert result == {"enabled": True, "ok": True, "period": "全部", "start": None,
                      "end": None, "total": "0.00", "count": 0, "categories": {},
                      "skipped_lines": 0, "reason": "全部共 0 笔，合计 0.00 元"}


# ---- 参数校验 ----

def test_amount_must_be_positive_number(drill):
    installed()
    for bad in (-5, 0):
        out = add(bad, "餐饮")
        assert out["ok"] is True and out["result"]["ok"] is False
        assert "金额要大于 0" in out["result"]["reason"]
    assert add("12.3", "餐饮")["error"]["code"] == "E_PARAM_TYPE"
    assert add(True, "餐饮")["error"]["code"] == "E_PARAM_TYPE"
    assert ledger_lines(drill) == []


def test_amount_with_more_than_two_decimals_is_rejected(drill):
    installed()
    out = add(1.234, "交通")
    assert out["ok"] is True and out["result"]["ok"] is False
    assert "最多两位小数" in out["result"]["reason"]
    assert ledger_lines(drill) == []


def test_category_is_required_and_must_be_known(drill):
    installed()
    assert ph.invoke("bookkeeper", "book_add", {"amount": 1})["error"]["code"] == "E_PARAM_REQUIRED"
    bad = add(1, "吃饭")
    assert bad["error"]["code"] == "E_PARAM_VALUE"
    assert "餐饮" in bad["error"]["message"]
    assert ledger_lines(drill) == []


def test_note_longer_than_40_is_rejected(drill):
    installed()
    out = add(1, "餐饮", note="超" * 41)
    assert out["error"]["code"] == "E_PARAM_LENGTH"
    assert ledger_lines(drill) == []


def test_bad_time_format_is_rejected(drill):
    installed()
    for bad in ("2026/11/19", "昨天", "2026-11-19T25:00", "2026-13-01"):
        out = add(1, "餐饮", at=bad)
        assert out["ok"] is True and out["result"]["ok"] is False, (bad, out)
        assert "时间要写成" in out["result"]["reason"], (bad, out)
    assert ledger_lines(drill) == []


def test_unknown_receipt_is_a_plugin_error_not_a_host_error(drill):
    installed()
    out = undo("rc-does-not-exist")
    assert out["ok"] is True  # 宿主把调用送到了, 插件自己说找不到
    assert out["result"]["ok"] is False and "没有找到这条加账回执" in out["result"]["reason"]


# ---- 坏行与开关 ----

def test_bad_ledger_lines_are_skipped_and_survive_undo(drill):
    installed()
    add(3.3, "餐饮", at="2026-11-19")
    path = book_home(drill) / "ledger.jsonl"
    garbage = '{"id": "bk-bad-json", "at": "2026-11-19"'  # 坏 JSON: 少一个括号
    lines = path.read_text(encoding="utf-8").splitlines()
    lines += [garbage,
              json.dumps({"id": "bk-bad-cat", "at": "2026-11-19", "amount": "1.00",
                          "category": "吃饭", "note": "", "created_at": "x"}, ensure_ascii=False),
              json.dumps({"id": "bk-bad-amount", "at": "2026-11-19", "amount": "abc",
                          "category": "餐饮", "note": "", "created_at": "x"}, ensure_ascii=False)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    out = summary("今天")
    assert out["ok"] is True, out
    assert out["result"]["total"] == "3.30" and out["result"]["count"] == 1
    assert out["result"]["skipped_lines"] == 3
    assert out["receipt_id"] is None  # query 没有回执

    # 撤销只删目标那一行, 三条坏行原样留着
    receipts = [json.loads(line) for line in
                (book_home(drill) / "receipts.jsonl").read_text(encoding="utf-8").splitlines()]
    receipt = next(item["id"] for item in receipts if item["kind"] == "add")
    assert undo(receipt)["result"]["removed"] == 1
    text = path.read_text(encoding="utf-8")
    assert garbage in text and '"bk-bad-cat"' in text and '"bk-bad-amount"' in text
    assert summary("今天")["result"]["total"] == "0.00"


def test_switch_off_leaves_no_files(drill, monkeypatch):
    installed()
    monkeypatch.setenv(ph.SWITCH_ENV, "off")
    out = add(12.3, "餐饮")
    assert out["ok"] is False and out["error"]["code"] == "E_HOST_OFF"
    assert "WM_PLUGIN_HOST=on" in out["error"]["message"]
    assert not book_home(drill).exists()


def test_ledger_and_receipts_live_under_the_plugin_home(drill):
    installed()
    add(1.11, "其他", note="只写插件自己的目录")
    home = book_home(drill)
    assert sorted(path.name for path in home.iterdir()) == ["ledger.jsonl", "receipts.jsonl"]
    assert not (drill / "vault").exists()
    ledger = json.loads(ledger_lines(drill)[0])
    assert set(ledger) == {"id", "at", "amount", "category", "note", "created_at"}
    assert ledger["amount"] == "1.11" and ledger["note"] == "只写插件自己的目录"
    receipt = json.loads((home / "receipts.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert receipt["kind"] == "add" and receipt["entry_id"] == ledger["id"]


# ---- 挂进她的工具表 ----

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


def test_book_tools_show_up_in_her_tool_list_with_the_why_description(drill):
    installed(grant=False)
    runtime = StudioRuntime(_Hub(drill / "agent-home"))
    session = _Session(drill)
    specs = {row["function"]["name"]: row["function"] for row in runtime.tools(session)}
    assert "book_summary" in specs and "book_add" not in specs and "book_undo" not in specs
    why = manifest_actions()["book_summary"]["why"]
    assert specs["book_summary"]["description"] == why
    assert runtime.is_plugin_tool("book_summary") is True
    assert runtime.is_plugin_tool("book_add") is False

    assert ph.grant_tier("bookkeeper", "panel", why="演练")["ok"] is True
    specs = {row["function"]["name"]: row["function"] for row in runtime.tools(session)}
    assert {"book_add", "book_summary", "book_undo"} <= set(specs)
    added = specs["book_add"]
    assert added["description"] == manifest_actions()["book_add"]["why"]
    assert added["parameters"]["required"] == ["amount", "category"]
    assert added["parameters"]["properties"]["category"]["enum"] == [
        "餐饮", "交通", "购物", "居住", "娱乐", "医疗", "其他"]
    assert ph.read_only("book_summary") is True
    assert ph.read_only("book_add") is False and ph.read_only("book_undo") is False

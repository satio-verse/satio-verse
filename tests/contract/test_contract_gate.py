"""契约测试 (不可变): 工作台合并前的门禁和保护名单 (wm/workbench.py; docs/roadmap/self-evolution.md 3.2、3.3).
- 补丁碰到保护名单 (能力表、外发检查、云端兜底、自检、规范检查、工作台自己、契约测试、配置里的隔离键) 不能一键合;
- 门禁跑的是项目里的契约测试: dsh 把契约测试改松、塞个 conftest.py 让测试全跳过, 都不算数;
- 受限沙箱里写的代码, 门禁不在沙箱外面自动跑.
"""
import json
from pathlib import Path

import pytest

from wm import workbench as W

FILES = {
    "wm/__init__.py": "",
    "wm/a.py": "x = 1\n",
    "config/default.json": json.dumps({"memory": {"exclude": ["外部渠道"]}}),
    "pytest.ini": "[pytest]\n",
    "tests/test_a.py": "def test_a():\n    assert True\n",
    "tests/contract/test_c.py": "from wm import a\n\n\ndef test_c():\n    assert a.x == 1\n",
}


def _write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        if text is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def make_job(tmp: Path, changes: dict) -> tuple[Path, Path]:
    """一个做完的工作台任务: 项目, 和 job/base (开工时的原样)、job/repo (base 加上 dsh 的改动; None = 删掉)."""
    project, jd = tmp / "proj", tmp / "job"
    for root in (project, jd / "base", jd / "repo"):
        _write(root, FILES)
    _write(jd / "repo", changes)
    return project, jd


def test_protected_files_cannot_be_merged_in_one_click(tmp_path):
    project, jd = make_job(tmp_path, {"wm/cloud.py": "x = 1\n", "tests/contract/test_c.py": "def test_c():\n    pass\n",
                                      "config/default.json": json.dumps({"memory": {"exclude": []}})})
    hits = W.protected(jd / "base", jd / "repo", W.diff(jd / "base", jd / "repo")[1])
    assert any(h.startswith("wm/cloud.py") for h in hits) and any(h.startswith("tests/contract/test_c.py") for h in hits)
    assert "config/default.json 的 memory.exclude" in hits
    with pytest.raises(W.MergeRefused):
        W.apply(jd, project)
    assert not (project / "wm/cloud.py").exists()


def test_gate_runs_the_projects_contract_tests(tmp_path):
    """dsh 把底线改坏了 (x = 2), 又把契约测试改松、在 tests/ 里塞了 conftest.py 让测试全跳过: 门禁用项目里那份, 没过, 不给合."""
    skip_all = "def pytest_collection_modifyitems(items):\n    items.clear()\n"
    project, jd = make_job(tmp_path, {"wm/a.py": "x = 2\n", "tests/contract/test_c.py": "def test_c():\n    pass\n",
                                      "tests/conftest.py": skip_all, "tests/contract/conftest.py": skip_all})
    g = W.gate(jd, project)
    # 是项目里那条断言没过 ("1 failed"), 不是被 conftest.py 清空了 ("no tests ran")
    assert not g["ok"] and g["contract"] is not None and "1 failed" in g["contract"]["summary"]
    with pytest.raises(W.MergeRefused):  # 放过保护名单 (改了契约测试), 门禁照样拦
        W.apply(jd, project, allow_protected=True)
    assert (project / "wm/a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (jd / "gate").exists()  # 门禁的拷贝跑完就删


def test_gate_passes_runs_touched_tests_and_lists_changed_ones(tmp_path):
    project, jd = make_job(tmp_path, {"wm/a.py": "x = 1\ny = 2\n", "tests/test_a.py": "def test_a():\n    assert 1\n"})
    g = W.gate(jd, project)
    assert g["ok"] and g["contract"]["ok"] and g["tests"]["files"] == ["tests/test_a.py"]
    assert g["tests_changed"] == ["tests/test_a.py"] and g["protected"] == []
    assert json.loads((jd / "gate.json").read_text(encoding="utf-8"))["ok"]
    assert W.apply(jd, project) == (["tests/test_a.py", "wm/a.py"], [])


def test_apply_backs_up_and_revert_restores(tmp_path):
    """合进来时先备份、记一笔待验证; 退回按备份恢复 (新加的文件删掉), 从待验证名单里拿掉."""
    project, jd = make_job(tmp_path, {"wm/a.py": "x = 1\ny = 2\n", "wm/new.py": "z = 3\n"})
    assert W.apply(jd, project, ignore_gate=True) == (["wm/a.py", "wm/new.py"], [])
    canary = json.loads((jd / "serve" / "canary.json").read_text(encoding="utf-8"))
    assert [j["job"] for j in canary["jobs"]] == ["job"] and canary["jobs"][0]["files"] == ["wm/a.py", "wm/new.py"]
    assert W.revert(jd, project) == (["wm/a.py", "wm/new.py"], [])
    assert (project / "wm/a.py").read_text(encoding="utf-8") == "x = 1\n" and not (project / "wm/new.py").exists()
    assert json.loads((jd / "serve" / "canary.json").read_text(encoding="utf-8"))["jobs"] == []


def test_revert_leaves_files_changed_after_the_merge(tmp_path):
    project, jd = make_job(tmp_path, {"wm/a.py": "x = 1\ny = 2\n"})
    W.apply(jd, project, ignore_gate=True)
    (project / "wm/a.py").write_text("x = 5\n", encoding="utf-8")  # 合进来以后别人又改了
    assert W.revert(jd, project) == ([], ["wm/a.py"])
    assert (project / "wm/a.py").read_text(encoding="utf-8") == "x = 5\n"


def test_gate_does_not_run_code_from_a_restricted_sandbox(tmp_path):
    project, jd = make_job(tmp_path, {"wm/a.py": "x = 1\nOPEN = open\n"})
    g = W.gate(jd, project, run=False)
    assert not g["ok"] and g["skipped"] and g["contract"] is None and g["tests"] is None

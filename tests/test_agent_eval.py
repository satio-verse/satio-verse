"""工作台本地评测的题 (tools/agent_eval_tasks.py) 自检: 每道题布置好的工作区判"没成", 照参考做法做完判"成了"
(判分不能误判; 改了题要先过这里). 不调模型, 不上网."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import agent_eval_tasks as TK  # noqa: E402


@pytest.mark.parametrize("task", TK.TASKS, ids=[t["id"] for t in TK.TASKS])
def test_checker_rejects_untouched_and_accepts_reference(task, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    task["setup"](ws)
    ok, detail = task["check"](ws)
    assert not ok, f"没做就判成了: {detail}"
    task["reference"](ws)
    ok, detail = task["check"](ws)
    assert ok, detail


def test_task_ids_unique_and_prompts_present():
    ids = [t["id"] for t in TK.TASKS]
    assert len(ids) == len(set(ids)) == 12
    assert all(t["prompt"].strip() and t["kind"] in {"改代码", "处理文件", "查网页", "Office"} for t in TK.TASKS)

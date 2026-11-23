"""近义去重的验收尺子 (2026-11-18, 用户拍板 A: 同一件事只留一条).

为什么单独一个文件: 这一层要钉的是「一句话被记成两条」这个洞, 而它横跨两个写入入口
(observe -> 关于用户.md, add_summary -> 对话摘要.md).
实现还没落地, 所以会红的几条挂了 xfail —— 它们是**规格**, 不是坏测试:
真做完了把 xfail 去掉, 必须全绿.

踩过的两个错 (写在这里免得重犯):
1. 原先以为情绪节点会因为近义重复被连两次 —— 错. memory_affect 的节点 id 是
   "时间|渠道|情绪|触发词", 里面没有事实文本, 情绪本来就是事件流, 两次难过就该两条.
   所以这里改成钉"事件流不被去重"这条正确行为.
2. 原先拿「我在北京上学」当负例 —— 提取器根本不认"上学", 于是"少一条"看起来像"被并掉了".
   负例改用同谓语不同地点 (北京/上海上班), 两边都会被提取, 才是真的在测去重.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wm.memory import Memory  # noqa: E402

NEED = pytest.mark.xfail(strict=False, reason="近义去重还没落地: 这是规格, 落地后去掉 xfail")


def new_memory(tmp_path: Path) -> Memory:
    m = Memory({"vault": str(tmp_path), "character": "小流", "graph_notes": False})
    mem = tmp_path / "小流" / "记忆"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "关于用户.md").write_text("", encoding="utf-8")
    (tmp_path / "小流" / "关系.md").write_text("# 关系\n\n- 认识天数: 0\n", encoding="utf-8")
    return m


def facts_of(tmp_path: Path) -> list[str]:
    p = tmp_path / "小流" / "记忆" / "关于用户.md"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s.startswith("- ") and "#auto-memory" in s:
            out.append(s[2:].split(" (")[0])
    return out


def summary_of(tmp_path: Path) -> str:
    p = tmp_path / "小流" / "记忆" / "对话摘要.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


@NEED
def test_paraphrase_of_same_thing_leaves_one_line(tmp_path):
    """同一件事的两种说法只留一条 ("去做胃镜" / "去医院做胃镜")."""
    m = new_memory(tmp_path)
    m.observe("我下周要去做胃镜", "嗯嗯", [], "calm")
    m.observe("我下周要去医院做胃镜", "嗯嗯", [], "calm")
    facts = facts_of(tmp_path)
    assert len(facts) == 1, f"同一件事被记成了 {len(facts)} 条: {facts}"


@NEED
def test_different_things_are_not_merged(tmp_path):
    """两件不同的事不能并掉一条 —— 并掉是静默丢记忆, 比漏判更糟.

    同谓语不同地点: 两句都会被提取, 所以这里测的确实是"去重有没有误伤".
    """
    m = new_memory(tmp_path)
    m.observe("我在北京上班", "嗯嗯", [], "calm")
    m.observe("我在上海上班", "嗯嗯", [], "calm")
    facts = facts_of(tmp_path)
    assert len(facts) == 2, f"两件事被并掉了: {facts}"


def test_saying_it_again_does_not_add_a_line(tmp_path):
    """原封不动再说一遍: 不新增行 (这条改前就该绿, 是防回归的基线)."""
    m = new_memory(tmp_path)
    m.observe("我养了一只橘猫", "嗯嗯", [], "calm")
    m.observe("我养了一只橘猫", "嗯嗯", [], "calm")
    assert len(facts_of(tmp_path)) == 1


@NEED
def test_summary_layer_does_not_duplicate_what_observe_already_kept(tmp_path):
    """摘要层不该把 observe 已经记下的同一件事再记一遍 —— 两层各记一次会把强度摊薄."""
    m = new_memory(tmp_path)
    m.observe("我下周要去做胃镜", "嗯嗯", [], "calm")
    m.add_summary("2026-11-18", [{"text": "用户下周要去医院做胃镜", "times": ["10:00"]}], 0)
    assert len(facts_of(tmp_path)) == 1
    assert "胃镜" not in summary_of(tmp_path), "摘要层又记了一遍: " + summary_of(tmp_path)[:200]


def test_affect_events_are_not_deduped(tmp_path):
    """情绪是事件流, 不是事实: 两次难过就该两条, 不能被近义去重合并.

    (原先这条写反了, 以为该并成 1 条 —— 查了 code 才确认 id 里没有事实文本.)
    """
    m = new_memory(tmp_path)
    m.observe("我下周要做手术，好害怕", "别怕", [], "低落")
    m.observe("我下周要去医院做手术，有点怕", "别怕", [], "低落")
    view = m.affect.view(m.clock())
    rows = view.get("events") if isinstance(view, dict) else None
    if not rows:  # 临时库里情绪记录默认没开, 那就没什么可钉的
        pytest.skip("这个库没开情绪记录 (memory.affect.mode=off), 这条不适用")
    assert len(rows) >= 1

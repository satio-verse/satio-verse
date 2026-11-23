"""自我卡 (A1, wm/self_card.py): 两段两种权限、坏文件不静默重建、开关默认关时提示词逐字节不变."""
import json
import os
from pathlib import Path

import pytest

from wm import intent as I
from wm import self_card as SC


@pytest.fixture(autouse=True)
def _no_env_mode(monkeypatch):
    monkeypatch.delenv("WM_SELF_CARD", raising=False)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "绫音"
    root.mkdir()
    return root


def _write_raw(root: Path, text: str) -> Path:
    path = SC.path_of(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ 基本读写
def test_missing_file_is_none_and_not_created(tmp_path):
    root = _root(tmp_path)
    assert SC.load(root) is None
    assert SC.render(root) is None
    assert not SC.path_of(root).exists()


def test_empty_card_renders_none_not_shell(tmp_path):
    """刚建的卡还没有任何内容时不能注入一句只有 [SELF] 标题的空壳."""
    root = _root(tmp_path)
    SC.ensure(root)
    assert SC.render(root) is None
    SC.append(root, "最近变化", "今天有了一件事")
    assert "[SELF]" in (SC.render(root) or "")


def test_ensure_creates_skeleton_once(tmp_path):
    root = _root(tmp_path)
    data = SC.ensure(root)
    assert data["version"] == 1 and SC.path_of(root).exists()
    again = SC.ensure(root)
    assert again["updated"] == data["updated"]  # 有就不动内容


def test_append_then_render_contains_it(tmp_path):
    root = _root(tmp_path)
    SC.append(root, "最近变化", "今天把自我卡写进了提示词", at="2026-11-19 06:20")
    text = SC.render(root)
    assert "今天把自我卡写进了提示词" in text and text.startswith("[SELF]")
    assert "2026-11-19" in text


def test_append_dedupes_same_line(tmp_path):
    root = _root(tmp_path)
    SC.append(root, "开放问题", "她到底想做什么")
    SC.append(root, "开放问题", "她到底想做什么！")  # 标点不同也算同一条
    assert len(SC.load(root)["auto"]["开放问题"]) == 1


def test_append_keeps_newest_and_drops_old(tmp_path):
    root = _root(tmp_path)
    for i in range(8):
        SC.append(root, "最近变化", f"第{i}件事")
    items = SC.load(root)["auto"]["最近变化"]
    assert len(items) == SC.AUTO_KEEP["最近变化"] == 6
    assert items[-1]["text"] == "第7件事" and "第0件事" not in json.dumps(items, ensure_ascii=False)


def test_append_rejects_core_sections_and_other_writers(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(SC.SelfCardError):
        SC.append(root, "价值观", "要诚实")
    with pytest.raises(SC.SelfCardError):
        SC.append(root, "最近变化", "偷偷写一句", by="user")
    with pytest.raises(SC.SelfCardError):
        SC.append(root, "最近变化", "   ")


def test_set_core_only_user_and_keeps_history(tmp_path):
    root = _root(tmp_path)
    with pytest.raises(SC.SelfCardError):
        SC.set_core(root, "身份", "我是绫音", by="self")
    SC.set_core(root, "身份", "我是绫音，住在主人电脑上的桌宠", by="user")
    SC.set_core(root, "价值观", ["说话算数", "不装懂"], by="user")
    data = SC.load(root)
    assert "桌宠" in data["core"]["身份"] and data["core"]["价值观"] == ["说话算数", "不装懂"]
    assert len(data["core_history"]) == 2
    assert "我在意" in SC.render(root) and "我是谁" in SC.render(root)


def test_core_same_value_is_noop(tmp_path):
    root = _root(tmp_path)
    SC.set_core(root, "边界", ["不假装有感情"], by="user")
    data = SC.load(root)
    SC.set_core(root, "边界", ["不假装有感情"], by="user")
    assert len(SC.load(root)["core_history"]) == len(data["core_history"]) == 1


# ------------------------------------------------------------------ 坏文件 / 超长
def test_broken_json_is_never_overwritten(tmp_path):
    root = _root(tmp_path)
    path = _write_raw(root, "{ 这不是 json")
    assert SC.load(root) is None and SC.render(root) is None
    with pytest.raises(SC.SelfCardError):
        SC.append(root, "最近变化", "想写一句")
    assert path.read_text(encoding="utf-8") == "{ 这不是 json"  # 原样留着


def test_wrong_shape_is_treated_as_broken(tmp_path):
    root = _root(tmp_path)
    _write_raw(root, json.dumps({"core": "不是对象"}, ensure_ascii=False))
    assert SC.load(root) is None
    with pytest.raises(SC.SelfCardError):
        SC.ensure(root)


def test_render_respects_max_chars(tmp_path):
    root = _root(tmp_path)
    SC.set_core(root, "身份", "我是绫音" * 40, by="user")
    for i in range(6):
        SC.append(root, "最近变化", f"第{i}条很长的变化" * 5)
    text = SC.render(root, max_chars=300)
    assert len(text) <= 300


def test_render_truncates_when_fixed_part_alone_is_too_long(tmp_path):
    root = _root(tmp_path)
    SC.set_core(root, "价值观", [f"第{i}条我很在意的原则" * 6 for i in range(5)], by="user")
    text = SC.render(root, max_chars=200)
    assert len(text) <= 200 and text.endswith("…")


def test_no_tmp_file_left_behind(tmp_path):
    root = _root(tmp_path)
    SC.append(root, "当前项目", "自我卡")
    names = [p.name for p in SC.path_of(root).parent.iterdir()]
    assert names == [SC.FILE]


# ------------------------------------------------------------------ 开关与注入
def test_text_for_off_and_shadow_do_not_inject(tmp_path, monkeypatch):
    root = _root(tmp_path)
    SC.set_core(root, "身份", "我是绫音", by="user")
    mem = type("M", (), {"dir": root})()
    assert SC.text_for({}, mem) is None                      # 缺省 off
    assert SC.text_for({"memory": {"self_card": {"mode": "shadow"}}}, mem) is None
    monkeypatch.setenv("WM_SELF_CARD", "on")
    got = SC.text_for({}, mem)
    assert got and "我是绫音" in got


def test_text_for_never_raises_when_memory_is_odd(monkeypatch):
    monkeypatch.setenv("WM_SELF_CARD", "on")
    assert SC.text_for({}, None) is None
    assert SC.text_for({}, object()) is None
    broken = type("B", (), {"dir": Path(os.devnull)})()
    assert SC.text_for({}, broken) is None


def test_mode_and_max_chars_from_config(tmp_path, monkeypatch):
    cfg = {"memory": {"self_card": {"mode": "on", "max_chars": 400}}}
    assert SC.mode_of(cfg) == "on" and SC.max_chars_of(cfg) == 400
    monkeypatch.setenv("WM_SELF_CARD", "off")
    assert SC.mode_of(cfg) == "off"  # 环境变量优先
    assert SC.mode_of({"memory": {"self_card": {"mode": "乱写"}}}) == "off"
    assert SC.max_chars_of({"memory": {"self_card": {"max_chars": "坏了"}}}) == SC.MAX_CHARS


def test_build_messages_unchanged_when_no_self_text():
    got = I.build_messages("你好", [("早", "早呀")], "状态行", "记忆行", I.ROOM)
    assert got[-1]["content"] == "你好\n记忆行\n状态行"
    assert all("[SELF]" not in m["content"] for m in got)


def test_build_messages_appends_self_card_last():
    got = I.build_messages("你好", [], "状态行", "记忆行", I.ROOM, "[SELF] 我记得的自己：\n我是谁：绫音")
    assert got[-1]["content"] == "你好\n记忆行\n状态行\n[SELF] 我记得的自己：\n我是谁：绫音"
    assert sum("[SELF]" in m["content"] for m in got) == 1


def test_status_reports_counts(tmp_path):
    root = _root(tmp_path)
    assert SC.status(root)["ok"] is False
    SC.set_core(root, "身份", "我是绫音", by="user")
    SC.append(root, "最近变化", "开始写自我卡")
    got = SC.status(root)
    assert got["ok"] and got["core"] == 1 and got["auto"]["最近变化"] == 1

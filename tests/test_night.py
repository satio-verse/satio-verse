"""夜间巩固 v1 的测试: 容错、拼句规则、日期窗口、写卡/写页面、开关.

全部数据落在 tmp_path, 不碰真实记忆库, 也不碰真实 ~/.worldmodel/heartbeat.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest

from wm import night as N
from wm import self_card as SC

DAY = "2026-11-19"
CHAR = "绫音"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """开关和心跳目录必须由每条测试自己决定, 不让宿主环境影响结果."""
    monkeypatch.delenv(N.ENV_ON, raising=False)
    monkeypatch.delenv(N.ENV_BEATS, raising=False)


# ------------------------------------------------------------------ 造数据的小工具
def _logs(tmp_path, day=DAY, lines=()):
    d = tmp_path / "logs"
    d.mkdir(exist_ok=True)
    stamp = day.replace("-", "")
    (d / f"digest-{stamp}.jsonl").write_text("\n".join(lines), encoding="utf-8")
    return d


def _beats(tmp_path, day=DAY, recs=()):
    d = tmp_path / "beats"
    d.mkdir(exist_ok=True)
    stamp = day.replace("-", "")
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in recs)
    (d / f"beats-{stamp}.jsonl").write_text(body, encoding="utf-8")
    return d


def _rep(day=DAY, lines=None, question=None):
    return {"day": day, "lines": lines or ["今天记住了1件关于你的事，比如：用户喜欢猫。"],
            "question": question,
            "counts": {"facts": 1, "turns": 0, "absent_beats": 0}}


def _page(tmp_path):
    return tmp_path / "vault" / CHAR / "记忆" / N.PAGE_NAME


# ------------------------------------------------------------------ 输入 1: digest
def test_load_digest_missing_file_and_bad_day_are_empty(tmp_path):
    assert N.load_digest(tmp_path / "没有这个目录", DAY) == []
    assert N.load_digest(tmp_path, "2026/11/19") == []


def test_load_digest_maps_day_to_filename_and_skips_bad_lines(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    (d / "digest-20261119.jsonl").write_text(
        '{"written": ["A"]}\n'
        "\n"
        "{这不是 JSON\n"
        "[1, 2, 3]\n"
        '"字符串行"\n'
        '{"written": ["B"]}\n',
        encoding="utf-8")
    (d / "digest-20261118.jsonl").write_text('{"written": ["昨天的"]}\n', encoding="utf-8")
    got = N.load_digest(d, DAY)
    assert [r.get("written") for r in got] == [["A"], ["B"]]


def test_today_facts_uses_written_first_then_accepted_and_dedupes():
    records = [
        # written 优先
        {"written": ["用户喜欢猫", "用户喜欢猫", " 用户养了狗 "], "accepted": [{"text": "不该用"}]},
        # written 是空 list -> 退回 accepted
        {"written": [], "accepted": [{"text": "用户喜欢猫"}, {"text": "用户下周三考试"}]},
        # 没有 written 字段 -> 退回 accepted; 空文本 / 缺字段跳过
        {"accepted": [{"text": "用户养了狗"}, {"text": ""}, {"text": None}, {"nope": 1}]},
    ]
    assert N.today_facts(records) == ["用户喜欢猫", "用户养了狗", "用户下周三考试"]


def test_today_facts_tolerates_weird_records():
    records = [None, "字符串", {"written": "不是 list", "accepted": [{"text": "A"}]},
               {"written": [{"text": "B"}, 3, None], "accepted": [{"text": "C"}]}]
    assert N.today_facts(records) == ["A", "B"]


# ------------------------------------------------------------------ 输入 2: 日志行
def test_count_turns_counts_only_dash_user_lines():
    text = "\n".join([
        "# 2026-11-19 的对话",
        "",
        "- 04:56 用户: 真的吗 → 绫音: 好看！ (动作: nod)",
        "- 05:01 用户: 嗯",
        "- 05:02 绫音: 我在这儿",
        "  - 05:03 用户: 缩进的不算",
        "用户: 没有短横线的不算",
    ])
    assert N.count_turns(text) == 2
    assert N.count_turns("") == 0


# ------------------------------------------------------------------ 输入 3: 心跳
def test_absent_missing_or_empty():
    assert N.absent([]) == {"beats": 0, "minutes": None}
    assert N.absent(None) == {"beats": 0, "minutes": None}
    # 只有一拍, 估不出间隔 -> 只报拍数
    assert N.absent([{"owner": False, "at": "2026-11-19T00:20:45+08:00"}]) == {"beats": 1, "minutes": None}


def test_absent_estimates_minutes_from_median_gap():
    base = datetime(2026, 11, 19, 0, 0, 0)
    beats = [{"at": (base + timedelta(minutes=10 * i)).isoformat(), "owner": i == 2}
             for i in range(4)]
    # 4 拍里 3 拍主人不在, 间隔中位数 10 分钟 -> 30 分钟
    assert N.absent(beats) == {"beats": 3, "minutes": 30}


# ------------------------------------------------------------------ 拼句子
def test_build_lines_empty_data_is_three_lines_without_numbers():
    lines = N.build_lines(DAY, [], 0, {"beats": 0, "minutes": None}, [])
    assert len(lines) == 3
    assert "没记住新的事" in lines[0]
    assert not any(re.search(r"\d", line) for line in lines)  # 没数据就不许出现数字
    assert all(len(line) <= N.MAX_LINE for line in lines)


def test_build_lines_uses_all_data():
    lines = N.build_lines(DAY, ["用户喜欢猫", "用户11月20日要去医院"], 7,
                          {"beats": 4, "minutes": 40}, ["她到底想要什么"])
    assert 3 <= len(lines) <= 5
    assert any("2件" in line and "用户喜欢猫" in line for line in lines)
    assert any("7轮话" in line for line in lines)
    assert any("40分钟" in line for line in lines)
    assert any("她到底想要什么" in line for line in lines)
    assert all(len(line) <= N.MAX_LINE for line in lines)


def test_build_lines_beats_when_minutes_unknown():
    lines = N.build_lines(DAY, [], 0, {"beats": 5, "minutes": None}, [])
    assert any("5拍" in line for line in lines)
    assert not any("轮话" in line for line in lines)  # N=0 不提第 2 句
    assert len(lines) >= 3


def test_build_lines_clips_overlong_open_question():
    lines = N.build_lines(DAY, [], 0, {"beats": 0, "minutes": None}, ["她" * 100])
    assert len(lines) == 3
    assert all(len(line) <= N.MAX_LINE for line in lines)
    assert any("…" in line and "她" in line for line in lines)


# ------------------------------------------------------------------ 问题: 日期窗口
def test_question_one_day_ahead():
    facts = ["用户11月20日要去医院"]
    assert N.build_question(DAY, facts, []) == "「用户11月20日要去医院」的事，要我提醒你吗？"


def test_question_three_days_ahead_is_in_window():
    assert N.build_question(DAY, ["用户11月22日要考试"], []) is not None


def test_question_four_days_out_is_out_of_window():
    assert N.build_question(DAY, ["用户11月23日要考试"], []) is None


def test_question_cross_year_boundary():
    # 12-31 看 1-1 是 1 天后, 不能算成 364 天前
    got = N.build_question("2026-12-31", ["用户1月1日要早起"], [])
    assert got == "「用户1月1日要早起」的事，要我提醒你吗？"


def test_question_open_question_fallback_and_none():
    assert N.build_question(DAY, ["用户喜欢猫"], ["她到底想要什么"]) == \
        "你还没告诉我：她到底想要什么，现在想说吗？"
    # 开放问题自带标点会被收掉
    assert N.build_question(DAY, [], ["她到底想要什么？"]) == \
        "你还没告诉我：她到底想要什么，现在想说吗？"
    assert N.build_question(DAY, [], []) is None


def test_question_date_beats_open_question():
    got = N.build_question(DAY, ["用户11月20日要考试"], ["她到底想要什么"])
    assert got.startswith("「用户11月20日要考试」")
    assert "她到底想要什么" not in got


# ------------------------------------------------------------------ 开关
def test_enabled_env_priority_over_config(monkeypatch):
    assert N.enabled({}) is False
    assert N.enabled({"memory": {"night": {"enabled": True}}}) is True
    monkeypatch.setenv(N.ENV_ON, "off")
    assert N.enabled({"memory": {"night": {"enabled": True}}}) is False
    monkeypatch.setenv(N.ENV_ON, "on")
    assert N.enabled({"memory": {"night": {"enabled": False}}}) is True


def test_write_off_creates_nothing(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    assert N.write(vault, CHAR, _rep(), cfg={}) == {"ok": False, "why": "off"}
    assert list(vault.iterdir()) == []  # 不建目录、不写文件


def test_write_without_vault_does_nothing(monkeypatch):
    monkeypatch.setenv(N.ENV_ON, "on")
    assert N.write("", CHAR, _rep()) == {"ok": False, "why": "no_vault"}


def test_write_uses_config_when_env_absent(tmp_path):
    got = N.write(tmp_path / "vault", CHAR, _rep(),
                  cfg={"memory": {"night": {"enabled": True}}})
    assert got["ok"] is True
    assert _page(tmp_path).exists()


# ------------------------------------------------------------------ 落盘: 今天的我.md
def test_write_same_day_replaces_section(tmp_path, monkeypatch):
    monkeypatch.setenv(N.ENV_ON, "on")
    vault = tmp_path / "vault"
    N.write(vault, CHAR, _rep(lines=["第一版"], question="问一"))
    N.write(vault, CHAR, _rep(lines=["第二版"], question="问二"))
    text = _page(tmp_path).read_text(encoding="utf-8")
    assert text.startswith("# 今天的我")
    assert text.count(f"## {DAY}") == 1
    assert "第二版" in text and "第一版" not in text
    assert "问二" in text and "问一" not in text
    assert b"\r\n" not in _page(tmp_path).read_bytes()  # 新文件一律 LF


def test_write_keeps_other_days(tmp_path, monkeypatch):
    monkeypatch.setenv(N.ENV_ON, "on")
    vault = tmp_path / "vault"
    N.write(vault, CHAR, _rep(day="2026-11-18", lines=["十八号"], question=None))
    N.write(vault, CHAR, _rep(day=DAY, lines=["十九号"], question=None))
    text = _page(tmp_path).read_text(encoding="utf-8")
    assert text.count("## 2026-11-18") == 1 and text.count(f"## {DAY}") == 1
    assert "十八号" in text and "十九号" in text

    # 回头再写 18 号: 只替换 18 号那段, 19 号原样保留
    N.write(vault, CHAR, _rep(day="2026-11-18", lines=["十八号新版"], question=None))
    text = _page(tmp_path).read_text(encoding="utf-8")
    assert "十八号新版" in text
    assert "十八号" not in text.replace("十八号新版", "")
    assert "十九号" in text


# ------------------------------------------------------------------ 落盘: 自我卡
def test_write_creates_self_card_and_appends_changes(tmp_path, monkeypatch):
    monkeypatch.setenv(N.ENV_ON, "on")
    vault = tmp_path / "vault"
    root = vault / CHAR
    assert SC.load(root) is None  # 一开始没有自我卡

    lines = ["今天记住了2件关于你的事，比如：用户喜欢猫。",
             "今天你跟我说了3轮话。",
             "今天有40分钟你不在，我自己待着。"]
    question = "你还没告诉我：她到底想要什么，现在想说吗？"
    got = N.write(vault, CHAR, _rep(lines=lines, question=question))

    assert got["ok"] is True
    assert got["changes"] == 3
    data = SC.load(root)
    assert data is not None  # 缺文件时 append 自己 ensure 出来了
    assert [x["text"] for x in data["auto"]["最近变化"]] == lines
    assert [x["text"] for x in data["auto"]["开放问题"]] == [question]


# ------------------------------------------------------------------ 汇总
def test_report_reads_all_inputs(tmp_path):
    logs = _logs(tmp_path, lines=[
        json.dumps({"day": DAY, "written": [],
                    "accepted": [{"text": "用户喜欢猫"}, {"text": "用户今天很累"}]},
                   ensure_ascii=False),
    ])
    root = tmp_path / "vault" / CHAR
    (root / "日志").mkdir(parents=True)
    (root / "日志" / f"{DAY}.md").write_text(
        "- 04:56 用户: 真的吗 → 绫音: 好看！\n- 05:01 用户: 嗯\n- 05:02 绫音: 在\n",
        encoding="utf-8")
    beats = _beats(tmp_path, recs=[
        {"at": "2026-11-19T00:20:45+08:00", "owner": False},
        {"at": "2026-11-19T00:30:45+08:00", "owner": False},
        {"at": "2026-11-19T00:40:45+08:00", "owner": True},
    ])
    SC.append(root, "开放问题", "她到底想要什么")

    rep = N.report(DAY, logs_dir=logs, vault=tmp_path / "vault", char=CHAR, beats_dir=beats)

    assert rep["day"] == DAY
    assert rep["counts"] == {"facts": 2, "turns": 2, "absent_beats": 2}
    assert 3 <= len(rep["lines"]) <= 5
    assert any("2件" in x for x in rep["lines"])
    assert any("2轮话" in x for x in rep["lines"])
    assert any("20分钟" in x for x in rep["lines"])
    assert rep["question"] == "你还没告诉我：她到底想要什么，现在想说吗？"


def test_report_missing_everything_is_quiet(tmp_path):
    vault = tmp_path / "vault"
    rep = N.report(DAY, logs_dir=tmp_path / "no-logs", vault=vault, char=CHAR,
                   beats_dir=tmp_path / "no-beats")
    assert rep["counts"] == {"facts": 0, "turns": 0, "absent_beats": 0}
    assert len(rep["lines"]) == 3
    assert rep["question"] is None
    assert not vault.exists()  # report 只读, 不建目录

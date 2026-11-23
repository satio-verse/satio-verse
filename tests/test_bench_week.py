"""周报生成器 (tools/bench_week.py) 自检.

重点:
- 坏 JSON 行 / 空目录 / 不存在的文件都不能抛, 报告里写「无数据（待接）」;
- P50/P95、样本数、窗口过滤要准 (造已知值);
- 五节结构固定, --out 真写文件, stdout 模式能打印.

全部只用 tmp_path 造假文件, 不碰真实日志; 不调模型、不上网.
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import bench_week as W  # noqa: E402

WINDOW = (date(2026, 11, 13), date(2026, 11, 19))


# ---------------------------------------------------------------- 造假文件的小工具
def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _latency_file(root: Path, day: str, rows: list[dict]) -> Path:
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    return _write(root / "logs" / f"latency-{day}.jsonl", body)


def _rec(voice_ms=None, motion_ms=None, interrupted=False) -> dict:
    """一行真实的 latency 记录 (只留报告要用的字段)."""
    metrics = {}
    if voice_ms is not None:
        metrics["voice_ms"] = voice_ms
    if motion_ms is not None:
        metrics["motion_ms"] = motion_ms
    rec = {"turn": 1, "source": "mic", "metrics": metrics}
    if interrupted:
        rec["interrupted"] = True
    return rec


# ---------------------------------------------------------------- 分位数 / 语音
def test_percentile_p50_p95_known_ten_values():
    values = [float(v) for v in range(100, 1100, 100)]  # 100, 200, ..., 1000
    assert W.percentile(values, 50) == pytest.approx(550.0)
    assert W.percentile(values, 95) == pytest.approx(955.0)
    assert W.percentile([42.0], 95) == 42.0
    assert W.percentile([], 50) is None


def test_voice_samples_p50_p95_and_count(tmp_path):
    rows = [_rec(voice_ms=v, motion_ms=v // 2) for v in range(100, 1100, 100)]
    _latency_file(tmp_path, "20261117", rows)

    stats = W.load_latency(tmp_path, *WINDOW)
    assert stats.files == 1
    assert len(stats.voice) == 10
    assert len(stats.motion) == 10
    assert W.percentile(stats.voice, 50) == pytest.approx(550.0)
    assert W.percentile(stats.voice, 95) == pytest.approx(955.0)

    report = W.build_report(tmp_path, *WINDOW, now=datetime(2026, 11, 19, 12, 0, 0))
    assert "| 说完→听到声音 | 550 ms | 955 ms | 10 |" in report


def test_bad_json_lines_are_skipped(tmp_path):
    body = (
        json.dumps(_rec(voice_ms=123)) + "\n"
        + "{ 这不是 json\n"
        + "\n"
        + "[1, 2, 3]\n"
        + json.dumps(_rec(voice_ms=456)) + "\n"
    )
    _write(tmp_path / "logs" / "latency-20261117.jsonl", body)

    stats = W.load_latency(tmp_path, *WINDOW)
    assert stats.voice == [123.0, 456.0]
    assert stats.bad_lines == 2  # 坏字符串 + 不是 dict 的数组


def test_interrupted_round_skips_voice_but_keeps_motion(tmp_path):
    _latency_file(tmp_path, "20261117", [_rec(voice_ms=300, motion_ms=111, interrupted=True)])
    stats = W.load_latency(tmp_path, *WINDOW)
    assert stats.voice == []       # 被打断的轮次没有"听到声音"
    assert stats.motion == [111.0]  # 动作照样有


def test_window_filter_ignores_outside_files(tmp_path):
    _latency_file(tmp_path, "20261101", [_rec(voice_ms=1000) for _ in range(5)])
    _latency_file(tmp_path, "20261117", [_rec(voice_ms=200), _rec(voice_ms=300)])

    stats = W.load_latency(tmp_path, *WINDOW)
    assert stats.files == 1
    assert stats.voice == [200.0, 300.0]


# ---------------------------------------------------------------- 轮数 / 费用
def test_ledger_turns_tokens_cost_and_window(tmp_path):
    rows = [
        {"t": 1, "who": "channels", "provider": "deepseek", "model": "deepseek-flash",
         "hit": 100, "miss": 20, "out": 10, "think": 5, "cost": 0.5},
        # 她主动开口: 钱和 token 照记, 不算一轮
        {"t": 2, "proactive": True, "provider": "deepseek", "hit": 7, "miss": 3, "out": 2, "cost": 0.2},
        # 按次收费: 只有钱, 不算轮
        {"t": 3, "fee": True, "provider": "deepseek", "cost": 0.01},
        # 本机模型: 不花钱, 但算轮、算 token
        {"t": 4, "provider": "local", "hit": 10, "miss": 0, "out": 3, "cost": 0},
    ]
    body = "".join(json.dumps(r) + "\n" for r in rows) + "{坏行\n"
    _write(tmp_path / "logs" / "billing" / "2026-11-17.jsonl", body)
    # 窗口外: 不能计进来
    _write(
        tmp_path / "logs" / "billing" / "2026-11-01.jsonl",
        json.dumps({"t": 9, "hit": 1, "miss": 0, "out": 0, "cost": 9}) + "\n",
    )

    days = W.load_ledger(tmp_path, *WINDOW)
    assert len(days) == 1
    day = days[0]
    assert day.day == date(2026, 11, 17)
    assert day.turns == 2  # 普通一轮 + local 一轮
    assert day.tokens == (100 + 20 + 10 + 5) + (7 + 3 + 2) + (10 + 0 + 3)
    assert day.cost == pytest.approx(0.5 + 0.2 + 0.01)

    report = W.build_report(tmp_path, *WINDOW, now=datetime(2026, 11, 19, 12, 0, 0))
    assert "| 2026-11-17 | 2 | 160 | 0.7100 |" in report


# ---------------------------------------------------------------- 714 题
def test_714_report_parsed(tmp_path):
    report_json = {
        "tool": "bench_714",
        "generated_at": "2026-11-18T10:22:19",
        "counts": {
            "total": 714,
            "pass": 203,
            "fail": 181,
            "manual": 330,
            "error": 0,
            "pass_rate_all": 0.2843,
            "pass_rate_judgeable": 0.5286,
        },
    }
    _write(
        tmp_path / "backups" / "s0-714" / "results" / "report.json",
        json.dumps(report_json, ensure_ascii=False),
    )

    row = W.load_714(tmp_path)
    assert row is not None
    assert "203/714" in row.result
    assert "28.4%" in row.result
    assert "52.9%" in row.result
    assert "backups/s0-714/results/report.json" in row.source

    report = W.build_report(tmp_path, *WINDOW, now=datetime(2026, 11, 19, 12, 0, 0))
    assert "| 714 题 | 通过 203/714" in report


def test_bad_714_json_is_not_fatal(tmp_path):
    _write(tmp_path / "backups" / "s0-714" / "results" / "report.json", "{ 坏掉的 json")
    assert W.load_714(tmp_path) is None
    report = W.build_report(tmp_path, *WINDOW, now=datetime(2026, 11, 19, 12, 0, 0))
    assert "| 714 题 | 无数据（待接） | — |" in report


# ---------------------------------------------------------------- 记忆评测
def test_memory_benchmarks_parsed(tmp_path):
    _write(
        tmp_path / "logs" / "bench-bother-off.json",
        json.dumps(
            {
                "split": "off",
                "chatter": 50,
                "bother": 0,
                "bother_rate": 0.0,
                "ask": 10,
                "answered": 10,
                "answered_rate": 1.0,
                "bother_samples": [],
                "ask_samples": [],
            },
            ensure_ascii=False,
        ),
    )
    _write(
        tmp_path / "logs" / "longmem-bench-20261119-025600.md",
        "# 长期记忆评测\n\n检索: 剧本 brain\n\n| 能力 | 分数 |\n|---|---|\n| **合计** | **45/51** |\n",
    )
    _write(
        tmp_path / "logs" / "longmem-bench-20261118-101709.md",
        "# 长期记忆评测\n\n检索: 剧本 supersede\n\n| 能力 | 分数 |\n|---|---|\n| **合计** | **6/18** |\n",
    )
    _write(
        tmp_path / "backups" / "s5-复测报告.md",
        "| 提取覆盖 | 默认：覆盖 **8/33 = 24%** |\n"
        "- 单次进程设 WM_EXTRACT_RULES2=on：覆盖 **30/33 = 91%** |\n",
    )

    evals = W.load_memory_evals(tmp_path, *WINDOW)
    assert evals["主动提"].result == "0/50（0.0%）"
    assert evals["被问"].result == "10/10（100.0%）"
    assert evals["brain"].result == "45/51（88.2%）"
    assert evals["supersede"].result == "6/18（33.3%）"
    assert evals["覆盖"].result == "覆盖 8/33 = 24%；开关 on 时 30/33 = 91%"

    report = W.build_report(tmp_path, *WINDOW, now=datetime(2026, 11, 19, 12, 0, 0))
    assert "记忆·主动提" in report and "记忆·被问" in report


def test_memory_window_filter_uses_latest_in_window(tmp_path):
    # 窗口内同一天跑过两份: 文件名里时间更晚的算数; 窗口外更新的那份不算.
    _write(
        tmp_path / "logs" / "longmem-bench-20261118-034719.md",
        "检索: 剧本 brain\n| **合计** | **39/51** |\n",
    )
    _write(
        tmp_path / "logs" / "longmem-bench-20261118-101709.md",
        "检索: 剧本 brain\n| **合计** | **40/51** |\n",
    )
    _write(
        tmp_path / "logs" / "longmem-bench-20261125-090000.md",
        "检索: 剧本 brain\n| **合计** | **50/51** |\n",
    )
    evals = W.load_memory_evals(tmp_path, *WINDOW)
    assert evals["brain"].result.startswith("40/51")


# ---------------------------------------------------------------- 验证债
def test_verification_debt_template_and_row_count(tmp_path):
    debt_path = tmp_path / "docs" / "verification-debt.md"
    info = W.load_verification_debt(tmp_path)
    assert info.created is True
    assert debt_path.is_file()
    assert len(info.rows) == 3
    assert info.rows[0][0] == "0968e7f"
    assert "插件宿主子进程隔离" in info.rows[0][1]
    assert all(row[3] == "已合并，未在真机跑过完整流程" for row in info.rows)

    text = W.build_report(tmp_path, *WINDOW, now=datetime(2026, 11, 19, 12, 0, 0))
    assert "## 四、验证债（3 条未验证）" in text
    assert "| 0968e7f |" in text

    with debt_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("| abc1234 | 第四条 | 跑一遍 | 已合并，未在真机跑过完整流程 |\n")
    info2 = W.load_verification_debt(tmp_path)
    assert info2.created is False
    assert len(info2.rows) == 4
    assert "## 四、验证债（4 条未验证）" in W.build_report(tmp_path, *WINDOW)


# ---------------------------------------------------------------- 整体 / CLI
def test_empty_tree_reports_no_data(tmp_path):
    text = W.build_report(tmp_path, *WINDOW, now=datetime(2026, 11, 19, 12, 0, 0))
    assert text.startswith("# 绫音周报 2026-11-13 ~ 2026-11-19")
    assert text.count("\n## ") == 5
    for heading in (
        "## 一、",
        "## 二、语音",
        "## 三、评测数字",
        "## 四、验证债",
        "## 五、下周要盯的三件事",
    ):
        assert heading in text
    assert text.count(W.NO_DATA) >= 5
    assert "本报告由 tools/bench_week.py 于 2026-11-19 12:00:00 生成；缺数据的节不要脑补。" in text
    # 验证债模板会自动创建, 所以空目录也有三行.
    assert (tmp_path / "docs" / "verification-debt.md").is_file()


def test_out_writes_five_section_report(tmp_path):
    out = tmp_path / "weekly" / "report.md"
    rc = W.main(["--out", str(out), "--week", "2026-11-13..2026-11-19"], root=tmp_path)
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# 绫音周报 2026-11-13 ~ 2026-11-19")
    assert text.count("\n## ") == 5
    assert text.count("# ") >= 5
    assert "本报告由 tools/bench_week.py 于" in text


def test_without_out_prints_to_stdout(tmp_path, capsys):
    rc = W.main(["--week", "2026-11-13..2026-11-19"], root=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "# 绫音周报 2026-11-13 ~ 2026-11-19" in out


def test_resolve_window_days_and_week():
    assert W.resolve_window(None, None, today=date(2026, 11, 19)) == (
        date(2026, 11, 13),
        date(2026, 11, 19),
    )
    assert W.resolve_window(None, 3, today=date(2026, 11, 19)) == (
        date(2026, 11, 17),
        date(2026, 11, 19),
    )
    assert W.resolve_window("2026-11-13..2026-11-19", None) == WINDOW
    with pytest.raises(ValueError):
        W.resolve_window("2026-11-13", None)  # 少了 ..
    with pytest.raises(ValueError):
        W.resolve_window("2026-11-19..2026-11-13", None)  # 起点晚于终点
    with pytest.raises(ValueError):
        W.resolve_window(None, 0)


def test_missing_sources_do_not_raise(tmp_path):
    assert W.load_latency(tmp_path, *WINDOW).files == 0
    assert W.load_ledger(tmp_path, *WINDOW) == []
    assert W.load_714(tmp_path) is None
    assert W.load_memory_evals(tmp_path, *WINDOW) == {}
    report = W.build_report(tmp_path, *WINDOW)
    assert W.NO_DATA in report

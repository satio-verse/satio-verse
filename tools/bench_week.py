"""绫音周报: 把这一周的真实数字摊开, 防止"感觉在进步".

只读生成一页 markdown 报告, 不做任何训练/评测/网络请求:
  1. 语音延迟  logs/latency-YYYYMMDD.jsonl  (日期按 UTC+8 记)
  2. 轮数/费用 logs/billing/YYYY-MM-DD.jsonl (记账插件写的 ledger)
  3. 714 题    backups/s0-714/results/report.json
  4. 记忆评测  logs/bench-bother*.json / logs/longmem-bench-*.md /
               backups/s5-复测报告.md / backups/s5/*.log (旧日志, UTF-16)
  5. 验证债    git log --since=<窗口起点> + docs/verification-debt.md

数据必须来自真实文件; 拿不到就写"无数据（待接）", 不脑补数字.
docs/verification-debt.md 不存在时按模板创建并先填三条已知的.

用法:
    python tools/bench_week.py                                  # 最近 7 天, 打印到 stdout
    python tools/bench_week.py --out logs/week-20261119.md      # 写到文件
    python tools/bench_week.py --week 2026-11-13..2026-11-19    # 指定窗口(含两端)
    python tools/bench_week.py --days 14                        # 最近 14 天
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 缺数据时统一写这句; 测试也按这句找.
NO_DATA = "无数据（待接）"

# 验证债模板: 表头固定, 下面三条是 2026-11-19 已经知道的"已合并、没在真机跑过".
DEBT_TEMPLATE = (
    "# 验证债\n"
    "\n"
    "已合并但未在真机/线上验证的改动。\n"
    "\n"
    "| 提交 | 改了什么 | 验证方式 | 状态 |\n"
    "|---|---|---|---|\n"
    "| 0968e7f | 插件宿主子进程隔离: 插件动作搬到子进程 + 超时强杀 | 真机跑一遍插件动作 (正常 / 超时 / 强杀), 看进程与回执日志 | 已合并，未在真机跑过完整流程 |\n"
    "| d11c5e3 | 记账插件: ledger 记录与轮数拆分 | 真机跑一轮对话 (含主动开口 / 按次收费), 核对 ledger 与余额 | 已合并，未在真机跑过完整流程 |\n"
    "| 3de6f76 | 回执核对: 回执内容与真实结果对齐 | 真机发消息, 核对回执与日志里记录的结果一致 | 已合并，未在真机跑过完整流程 |\n"
)


# ---------------------------------------------------------------- 小工具
def _read_text(path: Path) -> str:
    """读文本, 容忍 UTF-8 / UTF-16 (backups/s5 的旧日志是 UTF-16 LE + BOM)."""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _num(value) -> float | None:
    """只有真的数字才算数; bool / None / 字符串一律不当数字."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _file_date(path: Path) -> date | None:
    """从文件名里抠出日期: latency-20261117.jsonl / 2026-11-17.jsonl 两种都认."""
    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", path.stem)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    m = re.search(r"(\d{8})", path.stem)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _pct(part: float, total: float) -> str:
    if not total:
        return "0.0%"
    return f"{part / total * 100:.1f}%"


def percentile(values: list[float], p: float) -> float | None:
    """线性插值分位数 (与 numpy/statistics 的 inclusive 一致), 空列表返回 None."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (p / 100.0)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return float(s[lo])
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ---------------------------------------------------------------- 数据源 1: 语音
@dataclass
class VoiceStats:
    voice: list[float] = field(default_factory=list)   # 说完 -> 听到声音
    motion: list[float] = field(default_factory=list)  # 说完 -> 动作
    files: int = 0
    bad_lines: int = 0


def load_latency(root: Path, start: date, end: date) -> VoiceStats:
    """读窗口内的语音延迟文件.

    文件名里的日期是 UTC+8 的日历日; 窗口本身按本机时区的日期给, 这里按
    "日历日落在窗口内"过滤, 够用且不把时区换算藏进统计里.
    interrupted=true 的轮次是用户打断的, 没有 voice_ms, 不计入"听到声音"样本;
    motion_ms 有就照样计入"动作"样本.
    """
    stats = VoiceStats()
    logs = root / "logs"
    if not logs.is_dir():
        return stats
    for path in sorted(logs.glob("latency-*.jsonl")):
        day = _file_date(path)
        if day is None or not (start <= day <= end):
            continue
        stats.files += 1
        for line in _read_text(path).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                stats.bad_lines += 1
                continue
            if not isinstance(rec, dict):
                stats.bad_lines += 1
                continue
            metrics = rec.get("metrics")
            if not isinstance(metrics, dict):
                continue
            if rec.get("interrupted") is not True:
                v = _num(metrics.get("voice_ms"))
                if v is not None:
                    stats.voice.append(v)
            m = _num(metrics.get("motion_ms"))
            if m is not None:
                stats.motion.append(m)
    return stats


# ---------------------------------------------------------------- 数据源 2: 轮数/费用
@dataclass
class LedgerDay:
    day: date
    turns: int
    tokens: int
    cost: float


def load_ledger(root: Path, start: date, end: date) -> list[LedgerDay]:
    """读 logs/billing/YYYY-MM-DD.jsonl.

    语义对齐 wm/ledger.py:
      - proactive (她主动开口) 和 fee (按次收费) 不算"轮";
      - local provider 不花钱, 但算轮、算 token;
      - token 数 = hit + miss + out + think.
    坏行跳过, 不抛.
    """
    days: dict[date, LedgerDay] = {}
    billing = root / "logs" / "billing"
    if not billing.is_dir():
        return []
    for path in sorted(billing.glob("*.jsonl")):
        day = _file_date(path)
        if day is None or not (start <= day <= end):
            continue
        row = days.setdefault(day, LedgerDay(day=day, turns=0, tokens=0, cost=0.0))
        for line in _read_text(path).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(rec, dict):
                continue
            cost = _num(rec.get("cost"))
            if cost is not None:
                row.cost += cost
            if rec.get("fee"):
                # 按次收费: 只有钱, 没有 token, 也不算一轮.
                continue
            tokens = 0
            for key in ("hit", "miss", "out", "think"):
                n = _num(rec.get(key))
                if n is not None:
                    tokens += int(n)
            row.tokens += tokens
            if not rec.get("proactive"):
                row.turns += 1
    return [days[d] for d in sorted(days)]


# ---------------------------------------------------------------- 数据源 3/4: 评测
@dataclass
class EvalRow:
    name: str
    result: str
    source: str


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def load_714(root: Path) -> EvalRow | None:
    """714 题评测: backups/s0-714/results/report.json."""
    path = root / "backups" / "s0-714" / "results" / "report.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(_read_text(path))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    counts = data.get("counts")
    if not isinstance(counts, dict):
        return None

    def as_int(key: str) -> int:
        n = _num(counts.get(key))
        return 0 if n is None else int(n)

    total = as_int("total")
    passed = as_int("pass")
    fail = as_int("fail")
    manual = as_int("manual")
    error = as_int("error")
    rate_all = _num(counts.get("pass_rate_all"))
    if rate_all is None and total:
        rate_all = passed / total
    rate_judgeable = _num(counts.get("pass_rate_judgeable"))

    if not total:
        # 有文件但没数字: 当成没数据, 不猜.
        return None
    text = f"通过 {passed}/{total}（全量 {rate_all * 100:.1f}%"
    if rate_judgeable is not None:
        text += f"；可判 {rate_judgeable * 100:.1f}%"
    text += f"；manual {manual}、fail {fail}、error {error}）"
    source = _rel(root, path)
    generated = data.get("generated_at")
    if isinstance(generated, str) and generated:
        source += f"（{generated}）"
    return EvalRow("714 题", text, source)


def _parse_bother_json(path: Path) -> tuple[str, str] | None:
    """logs/bench-bother*.json -> (主动提结果, 被问结果)."""
    try:
        data = json.loads(_read_text(path))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    chatter = _num(data.get("chatter"))
    bother = _num(data.get("bother"))
    ask = _num(data.get("ask"))
    answered = _num(data.get("answered"))
    if chatter is None or bother is None or ask is None or answered is None:
        return None
    if not chatter or not ask:
        return None
    return (
        f"{int(bother)}/{int(chatter)}（{_pct(bother, chatter)}）",
        f"{int(answered)}/{int(ask)}（{_pct(answered, ask)}）",
    )


def _parse_bother_log(path: Path) -> tuple[str, str] | None:
    """backups/s5/bother-off.log (UTF-16) -> 主动提 / 被问."""
    text = _read_text(path)
    m1 = re.search(r"主动提[^\d]*(\d+)\s*/\s*(\d+)", text)
    m2 = re.search(r"被问[^\d]*(\d+)\s*/\s*(\d+)", text)
    if not m1 or not m2:
        return None
    a, b = int(m1.group(1)), int(m1.group(2))
    c, d = int(m2.group(1)), int(m2.group(2))
    return (f"{a}/{b}（{_pct(a, b)}）", f"{c}/{d}（{_pct(c, d)}）")


def _parse_longmem(path: Path) -> tuple[str, int, int] | None:
    """logs/longmem-bench-*.md -> (剧本, 通过数, 总数)."""
    text = _read_text(path)
    head = text[:600]
    split = "brain" if "brain" in head.lower() else ("supersede" if "supersede" in head.lower() else None)
    m = re.search(r"合计[^\n]*?(\d+)\s*/\s*(\d+)", text)
    if not split or not m:
        return None
    return split, int(m.group(1)), int(m.group(2))


def load_memory_evals(root: Path, start: date, end: date) -> dict[str, EvalRow]:
    """记忆评测: 返回 {槽位: EvalRow}; 找得到几项算几项, 少的由调用方写待接."""
    found: dict[str, EvalRow] = {}

    # 4.1 主动提 / 被问: 优先 logs/bench-bother*.json, 再退 backups/s5/bother-off.log.
    bother = None
    bother_src = None
    for path in sorted((root / "logs").glob("bench-bother*.json")):
        parsed = _parse_bother_json(path)
        if parsed:
            bother, bother_src = parsed, path
    if bother is None:
        fallback = root / "backups" / "s5" / "bother-off.log"
        if fallback.is_file():
            parsed = _parse_bother_log(fallback)
            if parsed:
                bother, bother_src = parsed, fallback
    if bother and bother_src:
        src = _rel(root, bother_src)
        found["主动提"] = EvalRow("记忆·主动提", bother[0], src)
        found["被问"] = EvalRow("记忆·被问", bother[1], src)

    # 4.2 提取覆盖: backups/s5-复测报告.md 里"覆盖 8/33 = 24% / 开关 on 时 30/33 = 91%".
    # 先把表格行放一边: 摘要表一行里挤着两个数 ("覆盖 8/33=24%; 开关 on 时 30/33=91%"),
    # 正文两条 bullet 才是干净的一行一个数; 正文找不到再退回表格.
    cover_path = root / "backups" / "s5-复测报告.md"
    if cover_path.is_file():
        text = _read_text(cover_path)
        default_pair = None
        on_pair = None
        body = [ln for ln in text.splitlines() if not ln.lstrip().startswith("|")]
        for line in body:
            m = re.search(r"覆盖\s*\*{0,2}\s*(\d+)\s*/\s*(\d+)\s*=\s*(\d+)%", line)
            if not m:
                continue
            pair = m.groups()
            if re.search(r"WM_EXTRACT_RULES2\s*=\s*on|开关\s*`?on`?", line, re.IGNORECASE):
                on_pair = on_pair or pair
            elif "默认" in line or "未设置" in line:
                default_pair = default_pair or pair
        for line in text.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            if default_pair is None:
                m = re.search(r"覆盖\s*\*{0,2}\s*(\d+)\s*/\s*(\d+)\s*=\s*(\d+)%", line)
                if m:
                    default_pair = m.groups()
            if on_pair is None:
                m = re.search(
                    r"开关\s*`?on`?\s*时\s*\*{0,2}\s*(\d+)\s*/\s*(\d+)\s*=\s*(\d+)%",
                    line,
                    re.IGNORECASE,
                )
                if m:
                    on_pair = m.groups()
        if default_pair or on_pair:
            text_out = []
            if default_pair:
                a, b, p = default_pair
                text_out.append(f"覆盖 {a}/{b} = {p}%")
            if on_pair:
                a, b, p = on_pair
                text_out.append(f"开关 on 时 {a}/{b} = {p}%")
            found["覆盖"] = EvalRow("记忆·提取覆盖", "；".join(text_out), _rel(root, cover_path))

    # 4.3 长期记忆: logs/longmem-bench-*.md, 窗口内取每个剧本最新的一份.
    latest: dict[str, tuple[date, Path]] = {}
    for path in sorted((root / "logs").glob("longmem-bench-*.md")):
        day = _file_date(path)
        if day is None or not (start <= day <= end):
            continue
        parsed = _parse_longmem(path)
        if not parsed:
            continue
        split, passed, total = parsed
        # 同一天可能跑过多份: 文件名里带 HHMMSS, 取更晚的那份.
        if split not in latest or (day, path.name) > (latest[split][0], latest[split][1].name):
            latest[split] = (day, path)
    # 窗口内没有就看 backups/s5 的旧日志 (只作为退路, 来源会写清楚).
    split_files = {
        "brain": root / "backups" / "s5" / "longmem-brain.log",
        "supersede": root / "backups" / "s5" / "longmem-supersede.log",
    }
    for split in ("brain", "supersede"):
        if split in latest:
            day, path = latest[split]
            parsed = _parse_longmem(path)
            if parsed:
                _, passed, total = parsed
                found[split] = EvalRow(
                    f"长期记忆 {split}", f"{passed}/{total}（{_pct(passed, total)}）", _rel(root, path)
                )
            continue
        fallback = split_files[split]
        if not fallback.is_file():
            continue
        m = re.search(r"(\d+)\s*/\s*(\d+)", _read_text(fallback))
        if m:
            passed, total = int(m.group(1)), int(m.group(2))
            found[split] = EvalRow(
                f"长期记忆 {split}", f"{passed}/{total}（{_pct(passed, total)}）", _rel(root, fallback)
            )

    # 4.4 兜底: backups/s5 下的其它 bench 结果文件 (bench_bother / bench_extract /
    # bench_longmem) 如果以后有人把新结果放这儿, 已经覆盖上面的逻辑; 不额外猜.
    return found


# ---------------------------------------------------------------- 数据源 5: 验证债 / git
@dataclass
class DebtInfo:
    path: Path
    created: bool
    rows: list[list[str]]


def load_verification_debt(root: Path) -> DebtInfo:
    """读 docs/verification-debt.md; 不存在就按模板创建 (含三条已知的)."""
    path = root / "docs" / "verification-debt.md"
    created = False
    if not path.is_file():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEBT_TEMPLATE, encoding="utf-8", newline="\n")
            created = True
        except OSError:
            return DebtInfo(path=path, created=False, rows=[])
    rows: list[list[str]] = []
    for line in _read_text(path).splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if cells[0] == "提交":
            continue
        if all(c and set(c) <= set("-: ") for c in cells):
            continue
        rows.append(cells)
    return DebtInfo(path=path, created=created, rows=rows)


@dataclass
class CommitInfo:
    count: int
    recent: list[str]


def load_commits(root: Path, start: date, end: date, limit: int = 5) -> CommitInfo | None:
    """git log --since=<窗口起点> --oneline; 不是 git 仓库 / 没有 git 时返回 None."""
    cmd = [
        "git",
        "-C",
        str(root),
        "log",
        f"--since={start.isoformat()} 00:00:00",
        f"--until={end.isoformat()} 23:59:59",
        "--oneline",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    return CommitInfo(count=len(lines), recent=lines[:limit])


# ---------------------------------------------------------------- 报告
def build_report(root: Path, start: date, end: date, now: datetime | None = None) -> str:
    """生成五节 markdown 周报; 任一数据源缺失都写"无数据（待接）", 不抛."""
    now = now or datetime.now()
    out: list[str] = []
    out.append(f"# 绫音周报 {start.isoformat()} ~ {end.isoformat()}")
    out.append("")

    # ---- 一、提交 + 轮数/费用
    commits = load_commits(root, start, end)
    if commits is None:
        out.append(f"## 一、这一周做了什么（{NO_DATA}）")
        out.append("")
        out.append(f"- 提交数：{NO_DATA}（git log 不可用，可能不在仓库里或没装 git）")
    else:
        out.append(f"## 一、这一周做了什么（{commits.count} 个提交）")
        out.append("")
        out.append(f"- 窗口：{start.isoformat()} ~ {end.isoformat()}（本机时区）")
        out.append(f"- 提交：{commits.count} 个（git log --since={start.isoformat()}）")
        if commits.recent:
            out.append("- 最近提交：")
            for line in commits.recent:
                out.append(f"  - {line}")
    out.append("")
    out.append("### 对话与费用（logs/billing 记账）")
    out.append("")
    ledger = load_ledger(root, start, end)
    if not ledger:
        out.append(f"- 轮数 / tokens / 费用：{NO_DATA}（窗口内没有 logs/billing/YYYY-MM-DD.jsonl）")
    else:
        out.append("| 日期 | 轮数 | tokens | 费用 |")
        out.append("|---|---|---|---|")
        total_turns = total_tokens = 0
        total_cost = 0.0
        for row in ledger:
            total_turns += row.turns
            total_tokens += row.tokens
            total_cost += row.cost
            out.append(f"| {row.day.isoformat()} | {row.turns} | {row.tokens} | {row.cost:.4f} |")
        out.append(f"| **合计** | **{total_turns}** | **{total_tokens}** | **{total_cost:.4f}** |")
        out.append("")
        out.append("- 口径：主动开口 / 按次收费不算轮；token = hit + miss + out + think；local 模型不计费。")
    out.append("")

    # ---- 二、语音
    voice = load_latency(root, start, end)
    out.append("## 二、语音")
    out.append("")
    out.append("| 指标 | P50 | P95 | 样本 |")
    out.append("|---|---|---|---|")

    def voice_cells(values: list[float]) -> tuple[str, str, str]:
        if not values:
            return NO_DATA, NO_DATA, "0"
        p50 = percentile(values, 50)
        p95 = percentile(values, 95)
        return f"{p50:.0f} ms", f"{p95:.0f} ms", str(len(values))

    v50, v95, vn = voice_cells(voice.voice)
    m50, m95, mn = voice_cells(voice.motion)
    out.append(f"| 说完→听到声音 | {v50} | {v95} | {vn} |")
    out.append(f"| 说完→动作 | {m50} | {m95} | {mn} |")
    out.append("")
    src = f"- 来源：logs/latency-YYYYMMDD.jsonl（窗口内 {voice.files} 个文件"
    if voice.bad_lines:
        src += f"；坏行 {voice.bad_lines} 条已跳过"
    src += "；原始值未剔除长尾）"
    out.append(src)
    if not voice.voice and not voice.motion:
        out.append(f"- {NO_DATA}：窗口内没有有效的语音延迟记录。")
    out.append("")

    # ---- 三、评测数字
    out.append("## 三、评测数字")
    out.append("")
    memory = load_memory_evals(root, start, end)
    slots: list[EvalRow | None] = [
        load_714(root),
        memory.get("主动提"),
        memory.get("被问"),
        memory.get("覆盖"),
        memory.get("brain"),
        memory.get("supersede"),
    ]
    slot_names = ["714 题", "记忆·主动提", "记忆·被问", "记忆·提取覆盖", "长期记忆 brain", "长期记忆 supersede"]
    out.append("| 评测 | 结果 | 来源文件 |")
    out.append("|---|---|---|")
    for name, row in zip(slot_names, slots):
        if row is None:
            out.append(f"| {name} | {NO_DATA} | — |")
        else:
            out.append(f"| {row.name} | {row.result} | {row.source} |")
    out.append("")

    # ---- 四、验证债
    debt = load_verification_debt(root)
    out.append(f"## 四、验证债（{len(debt.rows)} 条未验证）")
    out.append("")
    if not debt.rows:
        out.append(f"- {NO_DATA}（docs/verification-debt.md 里没有表格行）")
    else:
        out.append("| 提交 | 改了什么 | 验证方式 | 状态 |")
        out.append("|---|---|---|---|")
        for cells in debt.rows[:5]:
            out.append("| " + " | ".join(cells[:4]) + " |")
        if len(debt.rows) > 5:
            out.append("")
            out.append(f"- 表共 {len(debt.rows)} 行，上面只列前 5 行；完整表见 {_rel(root, debt.path)}。")
    out.append("")

    # ---- 五、下周要盯的三件事
    out.append("## 五、下周要盯的三件事")
    out.append("")
    out.append("1. TODO（手填）")
    out.append("2. TODO（手填）")
    out.append("3. TODO（手填）")
    out.append("")
    out.append(f"> 本报告由 tools/bench_week.py 于 {now.strftime('%Y-%m-%d %H:%M:%S')} 生成；缺数据的节不要脑补。")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- CLI
def _parse_week(text: str) -> tuple[date, date]:
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", text.strip())
    if not m:
        raise ValueError(f"--week 要写成 YYYY-MM-DD..YYYY-MM-DD，收到：{text!r}")
    start = datetime.strptime(m.group(1), "%Y-%m-%d").date()
    end = datetime.strptime(m.group(2), "%Y-%m-%d").date()
    if start > end:
        raise ValueError(f"--week 起点晚于终点：{text!r}")
    return start, end


def resolve_window(week: str | None, days: int | None, today: date | None = None) -> tuple[date, date]:
    """--week 优先; 否则最近 N 天(含今天), 默认 7 天, 按本机时区."""
    if week:
        return _parse_week(week)
    today = today or date.today()
    n = 7 if days is None else days
    if n < 1:
        raise ValueError(f"--days 至少是 1，收到：{n}")
    return today - timedelta(days=n - 1), today


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="绫音周报：语音延迟 / 轮数费用 / 评测 / 验证债，只读生成一页 markdown。"
    )
    parser.add_argument("--out", help="输出文件；缺省打印到 stdout")
    parser.add_argument("--week", help="窗口，如 2026-11-13..2026-11-19（含两端）")
    parser.add_argument("--days", type=int, help="最近 N 天（含今天），默认 7；与 --week 二选一")
    args = parser.parse_args(argv)
    if args.week and args.days is not None:
        parser.error("--week 和 --days 只能给一个")
    try:
        start, end = resolve_window(args.week, args.days)
    except ValueError as exc:
        parser.error(str(exc))
    actual_root = Path(root) if root is not None else ROOT
    report = build_report(actual_root, start, end)
    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = Path.cwd() / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8", newline="\n")
        print(f"已写入 {out_path}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""记账插件 (平台化第三步, 任务 ③: 第二个生活插件).

能力:
- `book_add(amount, category, note, at)`: 记一笔支出。金额必须是正数、最多两位小数;
  金额用 `Decimal` 算, 落盘写成两位小数字符串, **不做浮点累加**。
- `book_summary(period)`: 按 今天 / 本周 / 本月 / 全部 汇总: 合计、笔数、按类别分组的金额和笔数。
  只读, 不动账本。
- `book_undo(receipt_id)`: 凭加账回执把那一笔从账本里删掉 (整笔撤销); 重复调用幂等, 不报错、不改别的账。
- 每次 act 往 `receipts.jsonl` 追加一行回执 (和文件整理插件同风格), 回执 id 用于撤销。
- **核对 (第四切片)**: 记完 / 撤销完都回读账本核对 (这一笔真的在、金额和类别都对得上 / 真的不在了),
  结果写进回执和返回的 `verified`。核对不上时 `ok=false` + `E_RECEIPT_MISMATCH`，仍带回已成功的
  回执 (撤销要用)。

数据位置: `~/.worldmodel/plugins/bookkeeper/ledger.jsonl` (账本, 一行一笔) 和同目录的 `receipts.jsonl` (回执);
目录可用 `WM_BOOK_HOME` 覆盖 (演练/测试用)。账本是插件自己的文件: **不写她的记忆库, 不写 vault**。
"现在"取本机时间; 演练/测试可用 `WM_BOOK_NOW` 固定 (支持 `2026-11-19` 或 `2026-11-19T12:00`), 不设就用真实时间。

开关: `WM_PLUGIN_HOST=on` 才可用, 默认关。关着时三个动作都返回 `{"enabled": False, ...}`,
不读账本、不写文件。

说明 (不夸大):
- 只记支出 (正数), 不记收入; 没有多币种、没有编辑某一笔 (撤销后重记)。
- 账本是追加写; 只有撤销时才重写一次文件 (先写临时文件再替换原子替换), 坏行原样保留、跳过不炸。
- "本周"从周一开始; 今天 / 本周 / 本月按本机本地日期算;"全部"不按时间过滤。
- 单笔上限 1 亿元, 只是防止手滑输入一串数字, 不是财务规则。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

SWITCH_ENV = "WM_PLUGIN_HOST"
HOME_ENV = "WM_BOOK_HOME"
NOW_ENV = "WM_BOOK_NOW"
DEFAULT_HOME = Path.home() / ".worldmodel" / "plugins" / "bookkeeper"
LEDGER_NAME = "ledger.jsonl"
RECEIPTS_NAME = "receipts.jsonl"
RECEIPT_MISMATCH = "E_RECEIPT_MISMATCH"

# 类别是闭集: 新类别要改清单里的 enum 和这里一起改, 不然清单/实现对不上。
CATEGORIES = ("餐饮", "交通", "购物", "居住", "娱乐", "医疗", "其他")
PERIODS = ("今天", "本周", "本月", "全部")
MAX_NOTE = 40
MAX_AMOUNT = Decimal("100000000")
PENNY = Decimal("0.01")

_OFF_REASON = "插件宿主没有打开：设置环境变量 WM_PLUGIN_HOST=on 才启用（默认关）"


# ---- 开关与路径 ----

def switch_on() -> bool:
    return os.environ.get(SWITCH_ENV, "off").strip().lower() in ("on", "1", "true", "yes")


def _off() -> dict:
    return {"enabled": False, "ok": False, "reason": _OFF_REASON}


def _fail(reason: str) -> dict:
    return {"enabled": True, "ok": False, "reason": reason}


def state_home(home=None) -> Path:
    """账本和回执放哪 (参数 > WM_BOOK_HOME > ~/.worldmodel/plugins/bookkeeper)."""
    if home is not None:
        return Path(home).expanduser().resolve()
    raw = (os.environ.get(HOME_ENV) or "").strip()
    return (Path(raw).expanduser() if raw else DEFAULT_HOME).resolve()


def _now() -> datetime:
    """现在 (本机本地时间). WM_BOOK_NOW 只给演练/测试固定时间用, 写错就当没设."""
    raw = (os.environ.get(NOW_ENV) or "").strip()
    if raw:
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
    return datetime.now()


def _new_id(prefix: str) -> str:
    return prefix + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


# ---- 金额: 一律 Decimal, 落盘两位小数字符串 ----

def _decimal_of(value) -> Decimal:
    """数字 (或磁盘上的字符串) -> Decimal. 不合法抛 ValueError(一句人话)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("金额要是数字")
    text = value.strip() if isinstance(value, str) else str(value)
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError("金额读不出来：%s" % (text[:40],)) from None
    if not amount.is_finite():
        raise ValueError("金额要是有限的数字")
    return amount


def _check_amount(amount: Decimal) -> Decimal:
    if amount <= 0:
        raise ValueError("金额要大于 0（只记支出）")
    if amount.as_tuple().exponent < -2:
        raise ValueError("金额最多两位小数（分）")
    if amount > MAX_AMOUNT:
        raise ValueError("金额太大（单笔上限 %s 元）" % _fmt(MAX_AMOUNT))
    return amount


def parse_amount(value) -> Decimal:
    """宿主传进来的金额: 只收数字 (int/float), 转 Decimal 后核对范围与小位数."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("金额要是数字")
    return _check_amount(_decimal_of(value))


def _stored_amount(value) -> Decimal:
    """账本里读出来的金额: 收字符串或数字 (账本是自己写的, 但仍按同样规则核对)."""
    return _check_amount(_decimal_of(value))


def _fmt(amount: Decimal) -> str:
    """金额 -> 两位小数的字符串 (汇总输出和落盘都用它, 不用 float)."""
    return format(amount.quantize(PENNY), "f")


# ---- 时间: "2026-11-19" 或 "2026-11-19T12:30" ----

def _parse_moment(text) -> str:
    """时间字符串 -> 规范化的字符串 (日期或到分钟). 不合法抛 ValueError."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("时间要写成 2026-11-19 或 2026-11-19T12:30")
    raw = text.strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M").strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        pass
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError("时间要写成 2026-11-19 或 2026-11-19T12:30（本地时间）") from None


def _moment_of(text):
    """账本里的时间 -> datetime; 不认识返回 None (这条记录算是坏记录, 跳过)."""
    if not isinstance(text, str):
        return None
    raw = text.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


# ---- JSONL 读写的公共部分 ----

def _append_jsonl(path: Path, record: dict) -> Path:
    """追加一行 JSON 并落盘 (fsync), 返回文件路径."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _read_jsonl(path: Path):
    """逐行读. 返回 (对象列表, 坏行数): 空行不算坏, 不是 JSON 对象的行算坏、跳过不炸."""
    path = Path(path)
    if not path.is_file():
        return [], 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return [], 1
    out, bad = [], 0
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except ValueError:
            bad += 1
            continue
        if isinstance(item, dict):
            out.append(item)
        else:
            bad += 1
    return out, bad


def _load_entries(home_dir: Path):
    """账本 -> (校验过的账目列表, 坏记录数). 坏记录 = 坏 JSON + 字段不合格的记录."""
    raw_items, bad = _read_jsonl(Path(home_dir) / LEDGER_NAME)
    entries = []
    for raw in raw_items:
        entry = _entry(raw)
        if entry is None:
            bad += 1
            continue
        entries.append(entry)
    return entries, bad


def _entry(raw):
    """一条账本记录 -> {id, at, amount, date, category, note, created_at}; 不合格返回 None."""
    if not isinstance(raw, dict):
        return None
    entry_id = raw.get("id")
    category = raw.get("category")
    note = raw.get("note", "")
    if not isinstance(entry_id, str) or not entry_id:
        return None
    if category not in CATEGORIES:
        return None
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        return None
    moment = _moment_of(raw.get("at"))
    if moment is None:
        return None
    try:
        amount = _stored_amount(raw.get("amount"))
    except ValueError:
        return None
    return {"id": entry_id, "at": raw["at"], "amount": amount, "date": moment.date(),
            "category": category, "note": note, "created_at": raw.get("created_at")}


def _remove_entry(home_dir: Path, entry_id: str) -> int:
    """从账本里删掉 id 等于 entry_id 的行 (整笔撤销). 返回删掉几行; 坏行原样保留."""
    path = Path(home_dir) / LEDGER_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    except (OSError, UnicodeDecodeError) as exc:
        raise OSError("账本读不出来：%s" % exc) from None
    kept, removed = [], 0
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except ValueError:
            kept.append(line)  # 坏行不归撤销管, 原样保留
            continue
        if isinstance(item, dict) and item.get("id") == entry_id:
            removed += 1
            continue
        kept.append(line)
    if removed:
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    return removed


# ---- 回执核对 (第四切片): 动作说做成了, 要回账本看一眼 ----

def _verify_entry(home_dir: Path, entry: dict) -> dict:
    """回读账本: 这一笔真的在, 金额和类别都对得上. verified 只报 checked / ok."""
    raw_items, _bad = _read_jsonl(Path(home_dir) / LEDGER_NAME)
    hit = next((item for item in raw_items
                if isinstance(item, dict) and item.get("id") == entry.get("id")), None)
    ok = False
    if hit is not None:
        try:
            ok = (_stored_amount(hit.get("amount")) == _stored_amount(entry.get("amount"))
                  and hit.get("category") == entry.get("category"))
        except ValueError:
            ok = False
    return {"checked": 1, "ok": 1 if ok else 0}


def _verify_removed(home_dir: Path, entry_id: str) -> dict:
    """回读账本: 这个账目编号真的不在了."""
    raw_items, _bad = _read_jsonl(Path(home_dir) / LEDGER_NAME)
    still_there = any(isinstance(item, dict) and item.get("id") == entry_id for item in raw_items)
    return {"checked": 1, "ok": 0 if still_there else 1}


# ---- 记一笔 ----

def add(amount, category, note="", at=None, *, home=None) -> dict:
    """记一笔支出: 核对金额/类别/备注/时间, 追加账本, 写回执. 返回带回执编号的结果."""
    if not switch_on():
        return _off()
    if category not in CATEGORIES:
        return _fail("类别只能是：" + " / ".join(CATEGORIES))
    try:
        checked = parse_amount(amount)
    except ValueError as exc:
        return _fail(str(exc))
    if note is None:
        note = ""
    if not isinstance(note, str):
        return _fail("备注要是字符串")
    if len(note) > MAX_NOTE:
        return _fail("备注最多 %d 字" % MAX_NOTE)
    if at is None or (isinstance(at, str) and not at.strip()):
        at_text = _now().strftime("%Y-%m-%d")
    else:
        try:
            at_text = _parse_moment(at)
        except ValueError as exc:
            return _fail(str(exc))

    now = _now()
    home_dir = state_home(home)
    entry = {"id": _new_id("bk-"), "at": at_text, "amount": _fmt(checked),
             "category": category, "note": note,
             "created_at": now.isoformat(timespec="seconds")}
    try:
        ledger_path = _append_jsonl(home_dir / LEDGER_NAME, entry)
    except OSError as exc:
        # 账本没写进去 = 世界没变, 不能算"记下了"; 按回执对不上处理 (这一步没有可撤销的回执)
        message = "账本写不进去：%s（这一笔没有落盘，不能说记下了）" % exc
        return {"enabled": True, "ok": False, "code": RECEIPT_MISMATCH, "message": message,
                "reason": message, "verified": {"checked": 1, "ok": 0}}
    verified = _verify_entry(home_dir, entry)
    receipt = {"id": _new_id("rc-"), "kind": "add", "at": now.isoformat(timespec="seconds"),
               "ts": time.time(), "entry_id": entry["id"], "entry": entry, "verified": verified}
    try:
        receipt_path = _append_jsonl(home_dir / RECEIPTS_NAME, receipt)
    except OSError as exc:
        return _fail("回执写不进去：%s（账已经记下，账目编号 %s）" % (exc, entry["id"]))
    out = {"enabled": True, "ok": True, "id": entry["id"], "entry": entry,
           "amount": entry["amount"], "category": category, "at": at_text,
           "ledger_path": str(ledger_path), "receipt": receipt["id"],
           "receipt_path": str(receipt_path), "verified": verified,
           "reason": "记了一笔 %s 元（%s）；回执 %s（可撤销）" % (entry["amount"], category, receipt["id"])}
    if verified["ok"] != verified["checked"]:
        message = ("记完回读核对没通过：账本里找不到账目 %s（或金额/类别对不上）；"
                   "回执 %s 已写，可以 undo" % (entry["id"], receipt["id"]))
        out.update({"ok": False, "code": RECEIPT_MISMATCH, "message": message, "reason": message})
    return out


# ---- 汇总 ----

def _period_range(period: str, now: datetime):
    """这个期间覆盖哪几天: (起, 止) 含两端; "全部" 返回 (None, None)."""
    today = now.date()
    if period == "今天":
        return today, today
    if period == "本周":
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=6)
    if period == "本月":
        first = today.replace(day=1)
        next_first = (first + timedelta(days=32)).replace(day=1)
        return first, next_first - timedelta(days=1)
    return None, None


def summary(period="今天", *, home=None) -> dict:
    """按期间汇总合计、笔数和按类别分组的金额. 只读账本."""
    if not switch_on():
        return _off()
    if period not in PERIODS:
        return _fail("期间只能是：" + " / ".join(PERIODS))
    home_dir = state_home(home)
    entries, skipped = _load_entries(home_dir)
    now = _now()
    start, end = _period_range(period, now)
    picked = [entry for entry in entries
              if start is None or start <= entry["date"] <= end]
    total = Decimal("0")
    groups = {}
    for entry in picked:
        total += entry["amount"]
        group = groups.setdefault(entry["category"], {"amount": Decimal("0"), "count": 0})
        group["amount"] += entry["amount"]
        group["count"] += 1
    categories = {name: {"amount": _fmt(group["amount"]), "count": group["count"]}
                  for name, group in sorted(groups.items(),
                                            key=lambda item: (-item[1]["amount"], item[0]))}
    return {"enabled": True, "ok": True, "period": period,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "total": _fmt(total), "count": len(picked), "categories": categories,
            "skipped_lines": skipped,
            "reason": "%s共 %d 笔，合计 %s 元" % (period, len(picked), _fmt(total))}


# ---- 撤销 ----

def undo(receipt_id, *, home=None) -> dict:
    """凭加账回执把那一笔从账本里删掉. 重复调用幂等: 账本里没有这一行就报 already_undone."""
    if not switch_on():
        return _off()
    if not isinstance(receipt_id, str) or not receipt_id.strip():
        return _fail("要撤销哪条回执？请给加账时返回的回执编号")
    home_dir = state_home(home)
    receipts, _bad = _read_jsonl(home_dir / RECEIPTS_NAME)
    receipt = next((item for item in reversed(receipts)
                    if item.get("id") == receipt_id and item.get("kind") == "add"), None)
    if receipt is None:
        return _fail("没有找到这条加账回执：%s" % receipt_id)
    entry_id = receipt.get("entry_id")
    if not isinstance(entry_id, str) or not entry_id:
        return _fail("这条回执里没有账目编号，不能撤销：%s" % receipt_id)
    try:
        removed = _remove_entry(home_dir, entry_id)
    except OSError as exc:
        return _fail(str(exc))
    verified = _verify_removed(home_dir, entry_id)
    now = _now()
    undo_receipt = {"id": _new_id("rc-"), "kind": "undo", "undo_of": receipt_id,
                    "at": now.isoformat(timespec="seconds"), "ts": time.time(),
                    "entry_id": entry_id, "removed": removed, "verified": verified}
    try:
        receipt_path = _append_jsonl(home_dir / RECEIPTS_NAME, undo_receipt)
    except OSError as exc:
        return _fail("撤销回执写不进去：%s" % exc)
    out = {"enabled": True, "ok": True, "receipt": undo_receipt["id"], "undo_of": receipt_id,
           "entry_id": entry_id, "removed": removed, "already_undone": removed == 0,
           "ledger_path": str(home_dir / LEDGER_NAME), "receipt_path": str(receipt_path),
           "verified": verified,
           "reason": ("撤销了账目 %s" % entry_id) if removed
           else "这笔已经撤销过（账本里没有这一行），没有重复改账"}
    if verified["ok"] != verified["checked"]:
        message = ("撤完回读核对没通过：账目 %s 还在账本里；撤销回执 %s 已写，可以再查一遍"
                   % (entry_id, undo_receipt["id"]))
        out.update({"ok": False, "code": RECEIPT_MISMATCH, "message": message, "reason": message})
    return out


# ---- 宿主约定入口 (平台化第二切片): ACTIONS = {"<清单动作名>": callable} ----
# 宿主扫描清单 / 建工具表时不 import 本文件; 只有真的 invoke 才导入并执行顶层代码.
# callable 收 **params (参数名来自 wmworld.json 的 plugin.world.actions), 返回 dict.

def entry_add(amount, category, note=None, at=None):
    """book_add: 记一笔支出."""
    return add(amount, category, note=note, at=at)


def entry_summary(period="今天"):
    """book_summary: 按期间汇总."""
    return summary(period)


def entry_undo(receipt_id):
    """book_undo: 凭回执整笔撤销."""
    return undo(receipt_id)


ACTIONS = {
    "book_add": entry_add,
    "book_summary": entry_summary,
    "book_undo": entry_undo,
}


# ---- 命令行 (演练/手工查账用) ----

def _print_summary(result: dict) -> None:
    print("%s：%d 笔，合计 %s 元" % (result["period"], result["count"], result["total"]))
    for name, group in result["categories"].items():
        print("  %-6s %s 元（%d 笔）" % (name, group["amount"], group["count"]))
    if result["skipped_lines"]:
        print("  （账本里有 %d 行坏记录，已跳过）" % result["skipped_lines"])


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    parser = argparse.ArgumentParser(description="记账插件: 记一笔、按期间汇总、凭回执撤销")
    parser.add_argument("--add", action="store_true", help="记一笔")
    parser.add_argument("--amount", help="金额（正数，最多两位小数）")
    parser.add_argument("--category", help="类别：" + " / ".join(CATEGORIES))
    parser.add_argument("--note", default="", help="备注（最多 40 字）")
    parser.add_argument("--at", help="时间：2026-11-19 或 2026-11-19T12:30（默认今天）")
    parser.add_argument("--summary", action="store_true", help="汇总")
    parser.add_argument("--period", default="今天", help="期间：" + " / ".join(PERIODS))
    parser.add_argument("--undo", metavar="回执编号", help="凭加账回执撤销")
    parser.add_argument("--home", help="账本/回执目录（默认 ~/.worldmodel/plugins/bookkeeper）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    if args.undo:
        result = undo(args.undo, home=args.home)
    elif args.summary:
        result = summary(args.period, home=args.home)
    elif args.add:
        if args.amount is None or args.category is None:
            parser.error("--add 需要 --amount 和 --category")
        try:
            amount = float(args.amount)
        except ValueError:
            parser.error("--amount 要是数字")
        result = add(amount, args.category, note=args.note, at=args.at, home=args.home)
    else:
        parser.error("要指定 --add / --summary / --undo 之一")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif not result.get("ok"):
        print("没做成：%s" % result.get("reason"))
    elif args.summary:
        _print_summary(result)
    else:
        print(result["reason"])
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

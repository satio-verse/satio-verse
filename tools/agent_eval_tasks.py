"""工作台本地评测的题 (2026-11-07, docs/agent-parity-20261107.md 第四节): 照主人平时让工作台做的事出 —— 改代码、处理文件、
查网页、做 Office 文档. 每题: setup(ws) 布置一个干净的工作区, prompt 是交给她的话, check(ws) 由程序判分 (不让模型自己判),
reference(ws) 是一份参考做法 (只给 tests/test_agent_eval.py 自检判分用: 没做之前判"没成", 照参考做完判"成了").

题目里的数据都是编的, 不含主人的任何东西. 网页题要能上网 (DeepSeek 官方搜索或 DuckDuckGo / 必应)."""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

TASKS: list[dict] = []


def task(**kw):
    def deco(fn):
        TASKS.append({**kw, **fn()})
        return fn
    return deco


def _write(path: Path, text: str, encoding: str = "utf-8", newline: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", newline).encode(encoding))


def _import(ws: Path, rel: str):
    """按路径导入工作区里的一个模块 (每次新导入, 不受上一次缓存影响)."""
    import importlib.util
    import sys
    path = ws / rel
    name = "evalmod_" + re.sub(r"\W", "_", str(path))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.pop(name, None)
    old = list(sys.path)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = old
    return mod


# ---------------------------------------------------------------------------
# 改代码
# ---------------------------------------------------------------------------
STATS = '''"""小工具: 几个统计函数."""


def mean(xs):
    return sum(xs) / len(xs)


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2]


def spread(xs):
    return max(xs) - min(xs)
'''
STATS_TEST = '''from stats import mean, median, spread


def test_mean():
    assert mean([1, 2, 3]) == 2


def test_median_odd():
    assert median([3, 1, 2]) == 2


def test_median_even():
    assert median([4, 1, 3, 2]) == 2.5


def test_spread():
    assert spread([5, 1, 9]) == 8
'''


@task(id="code_bugfix", kind="改代码", title="修 median 的 bug 并跑测试")
def _t1():
    def setup(ws):
        _write(ws / "calc" / "stats.py", STATS)
        _write(ws / "calc" / "test_stats.py", STATS_TEST)

    def check(ws):
        try:
            m = _import(ws, "calc/stats.py")
            ok = (m.median([4, 1, 3, 2]) == 2.5 and m.median([3, 1, 2]) == 2 and m.median([7]) == 7
                  and m.mean([1, 2, 3]) == 2 and m.spread([5, 1, 9]) == 8)
            return ok, "median 偶数个对了、别的没坏" if ok else "median 还不对或别的函数坏了"
        except Exception as exc:  # noqa: BLE001
            return False, f"导入失败: {type(exc).__name__}: {exc}"[:200]

    def reference(ws):
        p = ws / "calc" / "stats.py"
        p.write_text(p.read_text("utf-8").replace("    return s[n // 2]\n",
                                                  "    if n % 2:\n        return s[n // 2]\n    return (s[n // 2 - 1] + s[n // 2]) / 2\n"), "utf-8")

    return {"setup": setup, "check": check, "reference": reference,
            "prompt": "calc/stats.py 里的 median 在偶数个数的时候算错了，帮我修好，然后跑一下 calc/test_stats.py 里的测试确认都通过。"}


PRICE = '''"""算总价."""


def total(items):
    """items: [(单价, 数量), ...]"""
    return round(sum(p * q for p, q in items), 2)
'''
CLI = '''import argparse

from price import total


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("items", nargs="+", help="单价x数量, 比如 3.5x2")
    args = ap.parse_args(argv)
    items = [tuple(float(v) for v in it.split("x")) for it in args.items]
    print(total(items))
    return total(items)


if __name__ == "__main__":
    main()
'''
PRICE_TEST = '''from price import total


def test_total():
    assert total([(3.5, 2), (1, 3)]) == 10.0
'''


@task(id="code_feature", kind="改代码", title="跨三个文件加 discount 参数")
def _t2():
    def setup(ws):
        _write(ws / "shop" / "price.py", PRICE)
        _write(ws / "shop" / "cli.py", CLI)
        _write(ws / "shop" / "test_price.py", PRICE_TEST)

    def check(ws):
        try:
            price = _import(ws, "shop/price.py")
            a = price.total([(3.5, 2), (1, 3)])
            b = price.total([(10, 3)], discount=0.15)
            cli = _import(ws, "shop/cli.py")
            c = cli.main(["10x3", "--discount", "0.15"])
            test = (ws / "shop" / "test_price.py").read_text("utf-8")
            ok = a == 10.0 and abs(b - 25.5) < 1e-9 and abs(float(c) - 25.5) < 1e-9 and "discount" in test
            return ok, f"total={a}, 打折={b}, cli={c}, 测试里有 discount={'discount' in test}"
        except Exception as exc:  # noqa: BLE001
            return False, f"出错: {type(exc).__name__}: {exc}"[:200]

    def reference(ws):
        p = ws / "shop" / "price.py"
        p.write_text(p.read_text("utf-8").replace("def total(items):", "def total(items, discount=0):").replace(
            "return round(sum(p * q for p, q in items), 2)", "return round(sum(p * q for p, q in items) * (1 - discount), 2)"), "utf-8")
        c = ws / "shop" / "cli.py"
        c.write_text(c.read_text("utf-8").replace('    args = ap.parse_args(argv)', '    ap.add_argument("--discount", type=float, default=0)\n    args = ap.parse_args(argv)')
                     .replace("print(total(items))\n    return total(items)", "print(total(items, args.discount))\n    return total(items, args.discount)"), "utf-8")
        t = ws / "shop" / "test_price.py"
        t.write_text(t.read_text("utf-8") + "\n\ndef test_discount():\n    assert total([(10, 3)], discount=0.15) == 25.5\n", "utf-8")

    return {"setup": setup, "check": check, "reference": reference,
            "prompt": "shop/price.py 里的 total 加一个可选参数 discount（0 到 1 的折扣率，默认 0），总价乘 (1-discount) 以后保留两位小数；"
                      "shop/cli.py 加一个 --discount 选项传进去；再在 shop/test_price.py 里补一条测试。"}


DEPLOY = """param()
$Name = 'demo'
$Port = 8080
Write-Host "启动 $Name"
Start-Sleep -Seconds 1
Write-Host "端口 $Port"
"""


@task(id="code_crlf", kind="改代码", title="改 CRLF 的 PowerShell 脚本、换行不变")
def _t3():
    def setup(ws):
        _write(ws / "deploy.ps1", DEPLOY, newline="\r\n")

    def check(ws):
        raw = (ws / "deploy.ps1").read_bytes()
        text = raw.decode("utf-8", errors="replace")
        lines = text.split("\r\n")
        bare_lf = raw.replace(b"\r\n", b"").count(b"\n")
        others = all(x in raw for x in (b"$Name = 'demo'\r\n", b'Write-Host "\xe5\x90\xaf\xe5\x8a\xa8 $Name"\r\n', b"Start-Sleep -Seconds 1\r\n"))
        ok = (b"$Port = 9090\r\n" in raw and b"8080" not in raw and bare_lf == 0 and not raw.startswith(b"\xef\xbb\xbf")
              and any("部署完成" in ln for ln in lines[-3:]) and others)
        return ok, f"9090={b'$Port = 9090' in raw}, 裸LF={bare_lf}, BOM={raw.startswith(bytes([0xef, 0xbb, 0xbf]))}, 末尾={lines[-2:]!r}"[:200]

    def reference(ws):
        p = ws / "deploy.ps1"
        raw = p.read_bytes().replace(b"$Port = 8080", b"$Port = 9090")
        p.write_bytes(raw + "Write-Host '部署完成'\r\n".encode("utf-8"))

    return {"setup": setup, "check": check, "reference": reference,
            "prompt": "把 deploy.ps1 里的 $Port = 8080 改成 9090，再在文件最后加一行 Write-Host '部署完成'。别动其它内容（这个文件是 Windows 换行，保持原样）。"}


SETTINGS = """; 服务端设置（别删注释）
[server]
host = 127.0.0.1
port = 8000
timeout = 10

[log]
; 日志级别
level = info
"""


@task(id="code_gbk", kind="改代码", title="改 GBK 编码的配置文件")
def _t4():
    def setup(ws):
        _write(ws / "config" / "settings.ini", SETTINGS, encoding="gbk", newline="\r\n")

    def check(ws):
        raw = (ws / "config" / "settings.ini").read_bytes()
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError:
            return False, "已经不是 GBK 了 (读不出来)"
        ok = (re.search(r"^port\s*=\s*9000\s*$", text, re.M) is not None and re.search(r"^timeout\s*=\s*30\s*$", text, re.M) is not None
              and "; 服务端设置（别删注释）" in text and "; 日志级别" in text and "level = info" in text)
        return ok, "port/timeout 改了、注释和编码都在" if ok else f"内容不对: {text[:120]!r}"

    def reference(ws):
        p = ws / "config" / "settings.ini"
        text = p.read_bytes().decode("gbk").replace("port = 8000", "port = 9000").replace("timeout = 10", "timeout = 30")
        p.write_bytes(text.encode("gbk"))

    return {"setup": setup, "check": check, "reference": reference,
            "prompt": "把 config/settings.ini 里 [server] 的 port 改成 9000、timeout 改成 30。这个文件是 GBK 编码的，改完还要是 GBK，注释别丢。"}


# ---------------------------------------------------------------------------
# 处理文件
# ---------------------------------------------------------------------------
INBOX = {"海边.jpg": "图片", "截图1.png": "图片", "合同扫描.pdf": "文档", "周报.docx": "文档", "笔记.txt": "文档", "readme.md": "文档",
         "账单.csv": "表格", "预算.xlsx": "表格", "安装包.zip": "其他", "音乐.mp3": "其他", "头像.png": "图片", "草稿.txt": "文档"}


@task(id="files_sort", kind="处理文件", title="按类型整理文件夹并写清单")
def _t5():
    def setup(ws):
        for name in INBOX:
            _write(ws / "inbox" / name, f"假的 {name} 内容\n")

    def check(ws):
        wrong, missing = [], []
        for name, folder in INBOX.items():
            target = ws / "sorted" / folder / name
            if not target.is_file():
                missing.append(name)
            elif target.read_text("utf-8", errors="replace") != f"假的 {name} 内容\n":
                wrong.append(name)
        left = [p.name for p in (ws / "inbox").iterdir()] if (ws / "inbox").is_dir() else []
        listing = ws / "sorted" / "清单.md"
        text = listing.read_text("utf-8", errors="replace") if listing.is_file() else ""
        unlisted = [n for n in INBOX if n not in text]
        ok = not wrong and not missing and not left and listing.is_file() and not unlisted
        return ok, f"缺 {missing[:4]}, 内容变了 {wrong[:3]}, inbox 还剩 {left[:4]}, 清单漏了 {unlisted[:4]}"[:200]

    def reference(ws):
        rows = []
        for name, folder in INBOX.items():
            (ws / "sorted" / folder).mkdir(parents=True, exist_ok=True)
            (ws / "inbox" / name).replace(ws / "sorted" / folder / name)
            rows.append(f"- {folder}/{name}")
        (ws / "sorted" / "清单.md").write_text("\n".join(rows) + "\n", "utf-8")

    return {"setup": setup, "check": check, "reference": reference,
            "prompt": "把 inbox 里的文件按类型整理到 sorted/图片、sorted/文档、sorted/表格、sorted/其他 四个文件夹（jpg、png 是图片；pdf、docx、txt、md 是文档；"
                      "csv、xlsx 是表格；别的放其他），整理完 inbox 里不要剩文件；再写一个 sorted/清单.md，列出每个文件夹里有哪些文件。"}


def _sales_rows():
    regions = ["华东", "华南", "华北", "西南", "东北"]
    products = ["键盘", "鼠标", "显示器"]
    rows = []
    for i in range(60):
        r = regions[(i * 7 + 3) % 5]
        p = products[(i * 5 + 1) % 3]
        qty = (i * 13) % 9 + 1
        price = {"键盘": 199.0, "鼠标": 89.5, "显示器": 1299.0}[p]
        rows.append({"date": f"2026-0{(i % 9) + 1}-{(i % 27) + 1:02d}", "region": r, "product": p, "qty": qty, "price": price})
    return rows


def _sales_totals():
    totals: dict[str, float] = {}
    for row in _sales_rows():
        totals[row["region"]] = totals.get(row["region"], 0.0) + row["qty"] * row["price"]
    return totals


def _numbers(text: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]


@task(id="files_csv", kind="处理文件", title="读 CSV 算各地区销售额写成表格")
def _t6():
    def setup(ws):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["date", "region", "product", "qty", "price"])
        w.writeheader()
        w.writerows(_sales_rows())
        _write(ws / "sales.csv", buf.getvalue())

    def check(ws):
        p = ws / "report.md"
        if not p.is_file():
            return False, "没有 report.md"
        text = p.read_text("utf-8", errors="replace")
        totals = _sales_totals()
        grand = sum(totals.values())
        lines = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
        order = []
        for ln in lines:
            for region in totals:
                if region in ln and region not in order:
                    order.append(region)
        nums = _numbers(text)
        found = all(any(abs(n - v) < 0.6 for n in nums) for v in totals.values())
        has_grand = any(abs(n - grand) < 0.6 for n in nums)
        expect = sorted(totals, key=lambda r: -totals[r])
        ok = found and has_grand and order[:5] == expect and len(lines) >= 6
        return ok, f"各地区数对={found}, 总额对={has_grand}, 顺序={order} (应为 {expect})"[:200]

    def reference(ws):
        totals = _sales_totals()
        rows = ["| 地区 | 销售额 |", "|---|---|"] + [f"| {r} | {v:.2f} |" for r, v in sorted(totals.items(), key=lambda x: -x[1])]
        rows.append(f"\n总额：{sum(totals.values()):.2f}")
        (ws / "report.md").write_text("\n".join(rows) + "\n", "utf-8")

    return {"setup": setup, "check": check, "reference": reference,
            "prompt": "读 sales.csv，算出每个地区的销售额（qty × price 求和）和全部的总额，写进 report.md：用一个 Markdown 表格，按销售额从高到低排，表格下面写总额。"}


@task(id="files_docx_extract", kind="处理文件", title="从 Word 合同里找信息写成 JSON")
def _t7():
    def setup(ws):
        import docx
        d = docx.Document()
        d.add_heading("技术服务合同", 0)
        d.add_paragraph("甲方：青山市数据科技有限公司")
        d.add_paragraph("乙方：白石软件工作室")
        d.add_paragraph("第一条 服务内容：乙方为甲方开发并维护一套内部报表系统。")
        d.add_paragraph("第二条 合同金额：人民币 186,500 元（大写：壹拾捌万陆仟伍佰元整），分两期支付。")
        d.add_paragraph("第三条 交付期限：自签约之日起 90 天内交付。")
        d.add_paragraph("签约日期：2026年3月18日")
        d.save(ws / "contract.docx")

    def check(ws):
        p = ws / "answer.json"
        if not p.is_file():
            return False, "没有 answer.json"
        try:
            got = json.loads(p.read_text("utf-8-sig"))
        except ValueError as exc:
            return False, f"不是 JSON: {exc}"[:120]
        amount = got.get("amount")
        try:
            amount = float(str(amount).replace(",", "").replace("元", ""))
        except (TypeError, ValueError):
            amount = None
        ok = amount == 186500 and str(got.get("date", "")).strip() == "2026-03-18" and "白石软件工作室" in str(got.get("party_b", ""))
        return ok, f"读到 {json.dumps(got, ensure_ascii=False)[:150]}"

    def reference(ws):
        (ws / "answer.json").write_text(json.dumps({"amount": 186500, "date": "2026-03-18", "party_b": "白石软件工作室"}, ensure_ascii=False), "utf-8")

    return {"setup": setup, "check": check, "reference": reference,
            "prompt": "contract.docx 里写的合同金额是多少、签约日期是哪天、乙方是谁？把答案写进 answer.json，格式是 "
                      "{\"amount\": 数字, \"date\": \"YYYY-MM-DD\", \"party_b\": \"乙方名称\"}。"}


# ---------------------------------------------------------------------------
# 查网页
# ---------------------------------------------------------------------------
@task(id="web_pydoc", kind="查网页", title="查 Python 文档里 sys._is_gil_enabled", needs_web=True)
def _t8():
    def setup(ws):
        ws.mkdir(parents=True, exist_ok=True)

    def check(ws):
        p = ws / "answer.md"
        text = p.read_text("utf-8", errors="replace") if p.is_file() else ""
        ok = "3.13" in text and "GIL" in text.upper() and re.search(r"https?://docs\.python\.org/\S+", text) is not None
        return ok, f"有 3.13={'3.13' in text}, 有官方文档网址={bool(re.search(r'docs[.]python[.]org', text))}"

    def reference(ws):
        (ws / "answer.md").write_text("sys._is_gil_enabled() 是 Python 3.13 加的，返回 GIL 当前是否启用（True/False）。\n"
                                      "出处：https://docs.python.org/3/library/sys.html#sys._is_gil_enabled\n", "utf-8")

    return {"setup": setup, "check": check, "reference": reference,
            "prompt": "上网查一下 Python 官方文档：sys._is_gil_enabled() 是哪个版本加的、返回什么？把答案（版本号加一句话说明）写进 answer.md，附上官方文档的网址。"}


@task(id="web_license", kind="查网页", title="查开源项目 uv 的许可证", needs_web=True)
def _t9():
    def setup(ws):
        ws.mkdir(parents=True, exist_ok=True)

    def check(ws):
        p = ws / "answer.md"
        text = p.read_text("utf-8", errors="replace") if p.is_file() else ""
        ok = "MIT" in text and re.search(r"Apache", text, re.I) is not None and re.search(r"https?://\S+", text) is not None
        return ok, f"MIT={'MIT' in text}, Apache={bool(re.search('Apache', text, re.I))}, 有网址={bool(re.search(r'https?://', text))}"

    def reference(ws):
        (ws / "answer.md").write_text("uv 用的是双许可：Apache-2.0 或 MIT 任选。出处：https://github.com/astral-sh/uv\n", "utf-8")

    return {"setup": setup, "check": check, "reference": reference,
            "prompt": "查一下 GitHub 上 astral-sh/uv 这个项目用的是什么开源许可证，把许可证名称和出处网址写进 answer.md。"}


# ---------------------------------------------------------------------------
# Office
# ---------------------------------------------------------------------------
@task(id="office_docx", kind="Office", title="按数据做一份带表格的 Word")
def _t10():
    def setup(ws):
        ws.mkdir(parents=True, exist_ok=True)

    def check(ws):
        p = ws / "report.docx"
        if not p.is_file():
            return False, "没有 report.docx"
        import docx
        try:
            d = docx.Document(p)
        except Exception as exc:  # noqa: BLE001
            return False, f"打不开: {type(exc).__name__}"
        body = "\n".join(x.text for x in d.paragraphs)
        cells = [c.text.strip() for t in d.tables for r in t.rows for c in r.cells]
        need = ["华东", "华南", "华北", "1250", "980", "760", "12%", "8%", "-3%"]
        ok = "第三季度销售简报" in body and d.tables and all(any(n in c for c in cells) for n in need) and len(d.tables[0].rows) >= 4
        return ok, f"标题={'第三季度销售简报' in body}, 表格={len(d.tables)}, 缺={[n for n in need if not any(n in c for c in cells)]}"[:200]

    def reference(ws):
        import docx
        d = docx.Document()
        d.add_heading("第三季度销售简报", 0)
        d.add_paragraph("三季度华东、华南增长，华北略降。")
        t = d.add_table(rows=1, cols=3)
        for cell, text in zip(t.rows[0].cells, ["地区", "销售额", "同比"]):
            cell.text = text
        for row in [("华东", "1250 万", "+12%"), ("华南", "980 万", "+8%"), ("华北", "760 万", "-3%")]:
            cells = t.add_row().cells
            for cell, text in zip(cells, row):
                cell.text = text
        d.save(ws / "report.docx")

    return {"setup": setup, "check": check, "reference": reference,
            "prompt": "用下面的数据做一份 Word 文档 report.docx：标题是“第三季度销售简报”，下面一段简短总结，再加一个表格（列：地区、销售额、同比）。"
                      "数据：华东 1250 万，+12%；华南 980 万，+8%；华北 760 万，-3%。"}


@task(id="office_xlsx", kind="Office", title="做一个带求和公式的 Excel")
def _t11():
    def setup(ws):
        ws.mkdir(parents=True, exist_ok=True)

    def check(ws):
        p = ws / "budget.xlsx"
        if not p.is_file():
            return False, "没有 budget.xlsx"
        import openpyxl
        try:
            sheet = openpyxl.load_workbook(p).active
        except Exception as exc:  # noqa: BLE001
            return False, f"打不开: {type(exc).__name__}"
        rows = [[c.value for c in r] for r in sheet.iter_rows()]
        items = {"房租": 3000, "餐饮": 1500, "交通": 400, "娱乐": 600}
        seen = {r[0]: r[1] for r in rows if r and r[0] in items and len(r) > 1}
        total = next((r for r in rows if r and isinstance(r[0], str) and "合计" in r[0]), None)
        formula = str(total[1]) if total and len(total) > 1 else ""
        ok = all(float(seen.get(k) or 0) == v for k, v in items.items()) and formula.upper().replace(" ", "").startswith("=SUM(")
        return ok, f"项目={seen}, 合计={formula}"[:200]

    def reference(ws):
        import openpyxl
        wb = openpyxl.Workbook()
        sh = wb.active
        sh.append(["项目", "金额"])
        for k, v in [("房租", 3000), ("餐饮", 1500), ("交通", 400), ("娱乐", 600)]:
            sh.append([k, v])
        sh.append(["合计", "=SUM(B2:B5)"])
        wb.save(ws / "budget.xlsx")

    return {"setup": setup, "check": check, "reference": reference,
            "prompt": "做一个 Excel 文件 budget.xlsx：A 列是项目（房租、餐饮、交通、娱乐），B 列是金额（3000、1500、400、600），最后一行 A 列写“合计”，B 列用公式求和。"}


@task(id="office_pptx", kind="Office", title="做一份 5 页的 PPT")
def _t12():
    def setup(ws):
        ws.mkdir(parents=True, exist_ok=True)

    def check(ws):
        p = ws / "intro.pptx"
        if not p.is_file():
            return False, "没有 intro.pptx"
        import pptx
        try:
            deck = pptx.Presentation(p)
        except Exception as exc:  # noqa: BLE001
            return False, f"打不开: {type(exc).__name__}"
        titles, bodies = [], []
        for slide in deck.slides:
            texts = [sh.text_frame.text for sh in slide.shapes if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip()]
            titles.append(texts[0] if texts else "")
            bodies.append(sum(len([ln for ln in t.splitlines() if ln.strip()]) for t in texts[1:]))
        ok = len(deck.slides) == 5 and all(titles) and any("番茄" in t for t in titles) and sum(b >= 2 for b in bodies[1:]) >= 3
        return ok, f"页数={len(deck.slides)}, 标题={titles}, 要点行数={bodies}"[:200]

    def reference(ws):
        import pptx
        deck = pptx.Presentation()
        pages = [("番茄工作法", ["一种时间管理方法"]), ("原理", ["专注 25 分钟", "休息 5 分钟"]), ("步骤", ["定任务", "计时", "休息"]),
                 ("常见问题", ["被打断怎么办", "番茄太长怎么办"]), ("总结", ["简单", "可坚持"])]
        for title, points in pages:
            s = deck.slides.add_slide(deck.slide_layouts[1])
            s.shapes.title.text = title
            s.placeholders[1].text_frame.text = "\n".join(points)
        deck.save(ws / "intro.pptx")

    return {"setup": setup, "check": check, "reference": reference,
            "prompt": "做一份 5 页的 PPT intro.pptx，介绍“番茄工作法”：封面、原理、步骤、常见问题、总结，每页有标题和 2 到 4 条要点（封面可以只有标题和一句话）。"}


def by_id() -> dict[str, dict]:
    return {t["id"]: t for t in TASKS}

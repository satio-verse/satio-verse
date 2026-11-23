"""文档解析引擎 (wm/docparse/): 一组自己造的测试文档 (tests/docparse_fixtures.py), 解析结果和预期一致 (验收 1);
本机读不了就明确报"读不了"、不自动转云端 (验收 3). 识字用假识字子进程 (tests/docparse_stub_worker.py): 真识字器没装, 这一轮不测它.
没验的: 方案页验收里"本机档读完能搜到扫描件里的一句话"的前半 (真识字器认得出字); 这里只验到"页码对"."""
import importlib.util
import json
import re
import types
from pathlib import Path

import pytest

from docparse_fixtures import (docx_bytes, encrypted_pdf_bytes, installed_ocr_dir, odt_bytes, pdf_bytes, png_bytes, pptx_bytes,
                               rtf_bytes, stub_cmd, xlsx_bytes)
from wm import docparse as DPK
from wm.docparse import DocParseError, router

ROOT = Path(__file__).resolve().parents[1]


def put(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return p


def joined(res) -> str:
    return "\n".join(x["text"] for x in res["parts"])


@pytest.fixture
def ocr(tmp_path):
    """装好了识字组件的目录 + 假识字子进程 (记日志)."""
    log = tmp_path / "ocr-jobs.log"
    return types.SimpleNamespace(kw={"ocr_dir": installed_ocr_dir(tmp_path / "ocr-home"), "ocr_cmd": stub_cmd(log)}, log=log)


def jobs(log: Path) -> list[dict]:
    return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


# ---------------------------------------------------------------- 验收 1: 各种文档, 结果和预期一致
def test_text_pdf_has_one_part_per_page_with_page_numbers(tmp_path):
    p = put(tmp_path, "t.pdf", pdf_bytes([("text", "ALPHA ONE"), ("text", "BETA TWO"), ("text", "GAMMA THREE")]))
    res = router.parse(p)
    assert res["status"] == "ready" and res["format"] == "pdf" and res["parts_total"] == 3 and res["backend"] == "builtin"
    assert [(x["n"], x["page"], x["label"], x["how"]) for x in res["parts"]] == [(1, 1, "第 1 页", "text"), (2, 2, "第 2 页", "text"),
                                                                              (3, 3, "第 3 页", "text")]
    assert [x["text"] for x in res["parts"]] == ["ALPHA ONE", "BETA TWO", "GAMMA THREE"]
    assert res["unread"] == [] and res["next"] == "" and res["cloud"]["used"] is False


def test_docx_paragraphs_and_table(tmp_path):
    p = put(tmp_path, "a.docx", docx_bytes(["第一段：苹果三个", "Second paragraph"], [["名称", "数量"], ["苹果", "3"]]))
    res = router.parse(p)
    assert res["status"] == "ready" and res["format"] == "docx"
    assert "苹果三个" in res["parts"][0]["text"] and res["parts"][0]["label"] == "段落 1"
    assert "第 2 行 | 苹果 | 3" in res["parts"][2]["text"] and res["parts"][2]["label"] == "表格 1"


def test_xlsx_two_sheets_formula_is_not_evaluated(tmp_path):
    p = put(tmp_path, "b.xlsx", xlsx_bytes({"预算": [["名称", "数量"], ["苹果", 3, ("=B2*2", 6)]], "第二表": [["季度预算 4200"]]}))
    res = router.parse(p)
    assert [x["label"] for x in res["parts"]] == ["工作表 预算", "工作表 第二表"]
    assert "C2: =B2*2 [未计算公式；缓存值：6]" in res["parts"][0]["text"] and "季度预算 4200" in res["parts"][1]["text"]


def test_pptx_slides_in_order(tmp_path):
    res = router.parse(put(tmp_path, "c.pptx", pptx_bytes([["标题一", "要点甲"], ["第二张的暗号"]])))
    assert [x["label"] for x in res["parts"]] == ["幻灯片 1", "幻灯片 2"]
    assert res["parts"][0]["text"] == "标题一\n要点甲" and res["parts"][1]["text"] == "第二张的暗号"


def test_text_files_utf8_and_gbk_and_markdown(tmp_path):
    utf = router.parse(put(tmp_path, "u.txt", "苹果三个\n第二行"))
    assert utf["parts"][0]["text"] == "苹果三个\n第二行"
    gbk = router.parse(put(tmp_path, "g.txt", "季度预算 4200 元".encode("gb18030")))
    assert gbk["parts"][0]["text"] == "季度预算 4200 元"
    md = router.parse(put(tmp_path, "m.md", "# 标题\n\n- 要点一\n- 要点二\n"))
    assert md["status"] == "ready" and md["format"] == "md" and "- 要点二" in md["parts"][0]["text"]


def test_odt_rtf_and_a_document_inside_a_zip(tmp_path):
    assert "odt 暗号" in joined(router.parse(put(tmp_path, "o.odt", odt_bytes(["odt 暗号"]))))
    assert "RTF MARKER" in joined(router.parse(put(tmp_path, "r.rtf", rtf_bytes("RTF MARKER"))))
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("内部/报告.docx", docx_bytes(["压缩包里的暗号"]))
    res = router.parse(put(tmp_path, "pack.zip", buf.getvalue()))
    assert res["status"] == "ready" and "压缩包里的暗号" in joined(res)


def test_start_count_and_the_next_hint(tmp_path):
    p = put(tmp_path, "five.pdf", pdf_bytes([("text", f"PAGE {i}") for i in range(1, 6)]))
    res = router.parse(p, start=2, count=2)
    assert [x["n"] for x in res["parts"]] == [2, 3] and res["start"] == 2 and res["returned"] == 2
    assert res["next"] == "还有内容：start=4 接着读"
    last = router.parse(p, start=4)
    assert [x["n"] for x in last["parts"]] == [4, 5] and last["next"] == "" and last["status"] == "ready"
    with pytest.raises(DocParseError, match="一共 5 段"):
        router.parse(p, start=6)


def test_max_chars_keeps_whole_parts_and_leaves_the_rest_for_next(tmp_path):
    p = put(tmp_path, "long.pdf", pdf_bytes([("text", "X" * 600), ("text", "Y" * 600), ("text", "Z" * 100)]))
    res = router.parse(p, max_chars=1000)
    assert [x["n"] for x in res["parts"]] == [1] and res["next"] == "还有内容：start=2 接着读"
    again = router.parse(p, start=2, max_chars=1000)
    assert [x["n"] for x in again["parts"]] == [2, 3]     # 600 + 100 放得下


def test_a_part_longer_than_the_whole_budget_is_cut_and_continues_by_char_offset(tmp_path):
    text = "".join(f"{i:04d}" for i in range(1500))     # 6000 字
    p = put(tmp_path, "big.txt", text)
    first = router.parse(p, max_chars=1000)
    cut = first["parts"][0]
    assert cut["cut"] is True and text.startswith(cut["text"]) and 900 < len(cut["text"]) <= 1000
    assert first["next"] == f"这一段还没读完：start=1, char_offset={len(cut['text'])} 接着读"
    second = router.parse(p, max_chars=1000, char_offset=len(cut["text"]))
    assert text[len(cut["text"]):].startswith(second["parts"][0]["text"])
    rest = router.parse(p, max_chars=6000, char_offset=5000)
    assert rest["parts"][0]["text"] == text[5000:] and "cut" not in rest["parts"][0] and rest["next"] == ""


def test_the_budget_counts_json_escapes_and_part_names_not_just_raw_characters(tmp_path):
    """工作台把超过 3 万字的工具结果换成节选 —— 引号和换行转义后要翻倍, 一段段的名字也占字."""
    quoted = put(tmp_path, "q.txt", ('"引号"' + chr(10)) * 4000)
    res = router.parse(quoted, max_chars=15000)
    assert len(json.dumps(res["parts"], ensure_ascii=False)) <= 15000 + 20
    many = put(tmp_path, "many.docx", docx_bytes([f"第{i}段" for i in range(400)]))
    res = router.parse(many, max_chars=5000)
    assert res["next"] and len(json.dumps(res["parts"], ensure_ascii=False)) <= 5000 + 20


def test_scanned_pdf_pages_are_recognized_with_the_right_page_numbers(tmp_path, ocr):
    p = put(tmp_path, "scan.pdf", pdf_bytes([("scan", "第一页的暗号"), ("scan", "第二页|有两行")]))
    res = router.parse(p, **ocr.kw)
    assert res["status"] == "ready" and res["backend"] == "builtin+local-ocr"
    assert [(x["n"], x["page"], x["how"], x["text"]) for x in res["parts"]] == [(1, 1, "ocr", "第一页的暗号"), (2, 2, "ocr", "第二页\n有两行")]
    assert res["parts"][0]["conf"] == 0.9 and any("识字只认字" in w for w in res["warnings"])
    assert [j["pages"] for j in jobs(ocr.log)] == [[1, 2]]     # 一次起一个识字进程, 两页一起


def test_a_pdf_with_a_text_page_and_a_scanned_page(tmp_path, ocr):
    p = put(tmp_path, "mix.pdf", pdf_bytes([("text", "FIRST PAGE ALPHA"), ("scan", "扫描页暗号")]))
    res = router.parse(p, **ocr.kw)
    assert [(x["page"], x["how"], x["text"]) for x in res["parts"]] == [(1, "text", "FIRST PAGE ALPHA"), (2, "ocr", "扫描页暗号")]
    assert [j["pages"] for j in jobs(ocr.log)] == [[2]]          # 有文字层的页不识字


def test_a_text_only_document_never_starts_the_recognizer(tmp_path, ocr):
    router.parse(put(tmp_path, "t.pdf", pdf_bytes([("text", "ONLY TEXT")])), **ocr.kw)
    router.parse(put(tmp_path, "d.docx", docx_bytes(["只有文字"])), **ocr.kw)
    assert jobs(ocr.log) == []


def test_an_image_file_is_recognized(tmp_path, ocr):
    res = router.parse(put(tmp_path, "pic.png", png_bytes("图片里的暗号")), **ocr.kw)
    assert res["status"] == "ready" and res["parts"][0]["how"] == "ocr" and res["parts"][0]["text"] == "图片里的暗号"
    assert jobs(ocr.log)[0]["op"] == "ocr_image"


def test_ocr_only_covers_the_requested_window_and_at_most_ocr_pages(tmp_path, ocr):
    p = put(tmp_path, "five-scans.pdf", pdf_bytes([("scan", f"页{i}") for i in range(1, 6)]))
    res = router.parse(p, start=4, **ocr.kw)
    assert [(x["page"], x["text"]) for x in res["parts"]] == [(4, "页4"), (5, "页5")]
    assert [j["pages"] for j in jobs(ocr.log)] == [[4, 5]]          # 第 1–3 页不在范围里, 没有白识
    ocr.log.unlink()
    capped = router.parse(p, ocr_pages=2, **ocr.kw)
    assert [x["page"] for x in capped["parts"]] == [1, 2] and capped["status"] == "partial"
    assert [u["n"] for u in capped["unread"]] == [3, 4, 5] and "start=3" in capped["unread"][0]["reason"]
    assert [j["pages"] for j in jobs(ocr.log)] == [[1, 2]]


def test_more_than_200_pages_is_partial_with_the_reason(tmp_path):
    res = router.parse(put(tmp_path, "huge.pdf", pdf_bytes([("text", f"P{i}") for i in range(1, 204)])), max_chars=60000)
    assert res["status"] == "partial" and res["parts_total"] == 200
    assert any("超过本地提取上限" in w or "仅返回" in w for w in res["warnings"])


def test_the_engine_has_no_pymupdf_in_it():
    for f in (ROOT / "wm" / "docparse").glob("*.py"):
        text = f.read_text(encoding="utf-8").lower()
        assert not re.search(r"\bimport\s+(fitz|pymupdf)\b|\bfrom\s+(fitz|pymupdf)\b", text), f.name


# ---------------------------------------------------------------- 验收 3: 本机读不了就明确报"读不了", 不自动转云端
GUIDES = ("上传", "云端帮你", "改走云端", "转云端", "交给云端", "mineru")


def no_guidance(res):
    """除了固定的 cloud 那一句 (它自己就是"不会自动上传"), 结果里不许有引导去云端的话."""
    text = json.dumps({k: v for k, v in res.items() if k != "cloud"}, ensure_ascii=False).lower()
    for g in GUIDES:
        assert g.lower() not in text, g
    assert res["cloud"] == {"used": False, "note": DPK.CLOUD_NOTE}


class FakeCloud:
    """假的云端后端: 登记进去, 但路由永远不该调它."""
    info = DPK.BackendInfo(id="fake-cloud", name="假云端", kind="cloud", upload=True, auto=False, needs_consent=True)

    def __init__(self):
        self.calls = 0

    def parse(self, *a, **k):
        self.calls += 1
        raise AssertionError("云端后端被调用了")


@pytest.fixture
def cloud():
    fake = FakeCloud()
    DPK.register_backend(fake)
    yield fake
    DPK.unregister_backend("fake-cloud")


def test_a_scan_without_the_ocr_component_is_unreadable_and_says_why(tmp_path, cloud):
    missing = tmp_path / "no-ocr-here"
    pdf = router.parse(put(tmp_path, "s.pdf", pdf_bytes([("scan", "藏着的字")])), ocr_dir=missing)
    assert pdf["status"] == "unreadable" and pdf["parts"] == [] and pdf["backend"] == "builtin"
    assert "本机识字组件（RapidOCR）还没装" in pdf["unread"][0]["reason"]
    png = router.parse(put(tmp_path, "p.png", png_bytes("藏着的字")), ocr_dir=missing)
    assert png["status"] == "unreadable" and "还没装" in png["unread"][0]["reason"]
    mixed = router.parse(put(tmp_path, "m.pdf", pdf_bytes([("text", "SEEN"), ("scan", "x")])), ocr_dir=missing)
    assert mixed["status"] == "partial" and mixed["parts"][0]["text"] == "SEEN" and mixed["unread"][0]["n"] == 2
    for res in (pdf, png, mixed):
        no_guidance(res)
    assert cloud.calls == 0


def test_ocr_never_does_not_start_the_recognizer(tmp_path, ocr, cloud):
    res = router.parse(put(tmp_path, "s.pdf", pdf_bytes([("scan", "字")])), ocr_mode="never", **ocr.kw)
    assert res["status"] == "unreadable" and res["unread"][0]["reason"] == "按要求没有识字"
    assert jobs(ocr.log) == [] and cloud.calls == 0
    no_guidance(res)


def test_files_the_local_readers_refuse_say_so_in_their_own_words(tmp_path, cloud):
    import io
    import zipfile
    cases = {
        "locked.pdf": encrypted_pdf_bytes(),
        "macro.docm": docx_bytes(["x"]),
        "pack.rar": b"Rar!\x1a\x07\x00" + b"\x00" * 64,
        "broken.docx": b"this is not a zip at all",
        "empty.txt": b"",
    }
    for name, data in cases.items():
        res = router.parse(put(tmp_path, name, data))
        assert res["status"] == "unreadable" and res["parts"] == [], name
        assert res["unread"][0]["label"] == "整份文件" and res["unread"][0]["reason"], name
        no_guidance(res)
    assert "加密" in router.parse(tmp_path / "locked.pdf")["unread"][0]["reason"]
    assert "宏" in router.parse(tmp_path / "macro.docm")["unread"][0]["reason"]
    assert cloud.calls == 0


def test_a_path_that_is_not_a_file_is_refused(tmp_path):
    with pytest.raises(DocParseError, match="没有这个文件"):
        router.parse(tmp_path / "nope.pdf")
    with pytest.raises(DocParseError):
        router.parse(tmp_path)


def test_the_backend_list_names_all_three_tiers_and_the_cloud_one_is_not_available(tmp_path, cloud):
    rows = {b.id: b for b in router.backends(tmp_path / "none")}
    assert rows["builtin-text"].available and not rows["builtin-text"].upload
    assert not rows["rapidocr-local"].available and "还没装" in rows["rapidocr-local"].reason_unavailable
    c = rows["mineru-cloud"]
    assert (c.kind, c.available, c.upload, c.auto, c.needs_consent) == ("cloud", False, True, False, True)
    assert rows["fake-cloud"].kind == "cloud" and cloud.calls == 0
    ok = {b.id: b for b in router.backends(installed_ocr_dir(tmp_path / "ocr"))}
    assert ok["rapidocr-local"].available


def test_the_real_worker_with_a_hollow_component_reports_it_instead_of_pretending(tmp_path):
    if importlib.util.find_spec("rapidocr_onnxruntime") is not None:
        pytest.skip("这台电脑的 Python 里已经有 RapidOCR, 这条测的是没有的情形")
    p = put(tmp_path, "s.pdf", pdf_bytes([("scan", "字")]))
    res = router.parse(p, ocr_dir=installed_ocr_dir(tmp_path / "hollow"))      # 默认命令 = 真的 worker.py
    assert res["status"] == "unreadable" and res["backend"] == "builtin+local-ocr"
    assert "装得不完整" in res["unread"][0]["reason"]
    no_guidance(res)

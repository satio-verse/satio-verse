"""文档解析测试用的"现造文档": 全部用标准库手写 (zipfile、zlib), 不依赖 python-docx / openpyxl / python-pptx (项目依赖里没有它们),
内容都是自己编的暗号, 不用用户的任何真实文件. 扫描页的"图"是一行灰度像素, 像素值就是文字的 UTF-8 字节 —— 假识字器 (docparse_stub_worker.py)
读回来当识字结果, 这样不用真识字器就能验证 "哪一页识出了什么".
"""
from __future__ import annotations

import io
import sys
import zlib
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

TESTS = Path(__file__).resolve().parent
STUB = TESTS / "docparse_stub_worker.py"


def _zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx_bytes(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{escape(c)}</w:t></w:r></w:p></w:tc>" for c in row) + "</w:tr>"
                       for row in table)
        body += f"<w:tbl>{rows}</w:tbl>"
    return _zip({
        "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "word/document.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'})


def xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    """sheets: 表名 -> 行 (单元格是字符串、数字, 或 ("=B2*2", 缓存值) 表示公式)."""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    files: dict[str, str] = {}
    sheet_tags, rels = [], []
    for i, (name, rows) in enumerate(sheets.items(), 1):
        out = []
        for r, row in enumerate(rows, 1):
            cells = []
            for c, v in enumerate(row):
                ref = f"{'ABCDEFGH'[c]}{r}"
                if isinstance(v, tuple):
                    cells.append(f'<c r="{ref}"><f>{escape(v[0].lstrip("="))}</f><v>{v[1]}</v></c>')
                elif isinstance(v, str):
                    cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(v)}</t></is></c>')
                else:
                    cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            out.append(f'<row r="{r}">{"".join(cells)}</row>')
        files[f"xl/worksheets/sheet{i}.xml"] = f'<?xml version="1.0"?><worksheet xmlns="{ns}"><sheetData>{"".join(out)}</sheetData></worksheet>'
        sheet_tags.append(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>')
        rels.append(f'<Relationship Id="rId{i}" Type="x" Target="worksheets/sheet{i}.xml"/>')
    files["xl/workbook.xml"] = f'<?xml version="1.0"?><workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>{"".join(sheet_tags)}</sheets></workbook>'
    files["xl/_rels/workbook.xml.rels"] = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                           + "".join(rels) + "</Relationships>")
    return _zip(files)


def pptx_bytes(slides: list[list[str]]) -> bytes:
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    files = {}
    for i, lines in enumerate(slides, 1):
        paras = "".join(f"<a:p><a:r><a:t>{escape(t)}</a:t></a:r></a:p>" for t in lines)
        files[f"ppt/slides/slide{i}.xml"] = f'<?xml version="1.0"?><p:sld xmlns:a="{a}" xmlns:p="urn:p"><p:txBody>{paras}</p:txBody></p:sld>'
    return _zip(files)


def odt_bytes(paragraphs: list[str]) -> bytes:
    body = "".join(f"<text:p>{escape(p)}</text:p>" for p in paragraphs)
    content = ('<?xml version="1.0"?><office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:text>' + body +
               "</office:text></office:body></office:document-content>")
    return _zip({"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": content})


def rtf_bytes(text: str) -> bytes:
    return ("{\\rtf1\\ansi\\deff0 " + text + "\\par}").encode("ascii")


# ---- PDF ---------------------------------------------------------------------------
def _pdf_str(s: str) -> bytes:
    return s.encode("latin-1").replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def image_pixels(text: str) -> bytes:
    """假识字器读的"图": 一行灰度像素, 值 = 文字的 UTF-8 字节."""
    return text.encode("utf-8")


def pdf_bytes(pages: list) -> bytes:
    """pages: 每页 ("text", ASCII 文字) 或 ("scan", 假识字器会读出的文字) 或 ("blank", "").
    文字页用 Helvetica 画 (pypdf 取得出); 扫描页只有一张灰度图 (没有文字层)."""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    add(b"")          # 1 目录, 最后填
    add(b"")          # 2 页树, 最后填
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for kind, text in pages:
        resources = f"<< /Font << /F1 {font} 0 R >> >>"
        if kind == "text":
            content = b"BT /F1 12 Tf 72 700 Td (" + _pdf_str(text) + b") Tj ET"
        elif kind == "scan":
            raw = image_pixels(text)
            packed = zlib.compress(raw)
            img = add(b"<< /Type /XObject /Subtype /Image /Width %d /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 "
                      b"/Filter /FlateDecode /Length %d >>\nstream\n" % (len(raw), len(packed)) + packed + b"\nendstream")
            resources = f"<< /XObject << /Im0 {img} 0 R >> >>"
            content = b"q 400 0 0 100 50 600 cm /Im0 Do Q"
        else:
            content = b""
        cs = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        kids.append(add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {cs} 0 R /Resources {resources} >>".encode()))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = ("<< /Type /Pages /Count %d /Kids [%s] >>" % (len(kids), " ".join(f"{k} 0 R" for k in kids))).encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def png_bytes(text: str) -> bytes:
    from PIL import Image
    raw = image_pixels(text)
    img = Image.frombytes("L", (len(raw), 1), raw)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def encrypted_pdf_bytes() -> bytes:
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes([("text", "SECRET")]))))
    w.encrypt("pw")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# ---- 识字组件 (假) -------------------------------------------------------------------
def installed_ocr_dir(root: Path) -> Path:
    """造一个"装好了"的识字组件目录 (有 site/ 和安装记录). 里面没有真的 RapidOCR —— 配假识字子进程用."""
    from wm.docparse import component
    (root / "site").mkdir(parents=True, exist_ok=True)
    component.write_mark(root, [{"name": "fake-wheel", "sha256": "0" * 64, "tops": []}])
    return root


def stub_cmd(log: Path | None = None) -> list[str]:
    """假识字子进程的命令 (-I: 和真的一样用隔离模式). log: 每个任务的 JSON 追加写在这里, 数识字进程被起了几次、识了哪几页."""
    return [sys.executable, "-I", str(STUB)] + ([str(log)] if log else [])

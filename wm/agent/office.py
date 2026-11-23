"""Office 文档 (工作台第 7 块, 2026-11-02 用户"开始做"). 组件是用户点头装的 (docs/block7-components-20261102.md):
python-docx / python-pptx (MIT)、mammoth (BSD-2)、pywin32 (PSF, 调本机 Word / PowerPoint 的 COM).

她的工具 (studio.py 登记, 和资料本一样):
  docx_create   标题、段落、列表、引用、表格建一份 Word;
  docx_read     按"第 N 段"列出来 (和工作台预览、引用卡片上的段号一致: python-docx 的正文段落, 不含表格里的);
  docx_edit     按段改写 / 在后面加一段 / 删一段, 段落样式不变; 改之前原件存一份到 <工作区>/.archive/office-<时间>/;
  pptx_create   封面 + 一页一个标题、几条要点、备注;
  office_to_pdf 经本机 Word / PowerPoint 另存 PDF (和你用 Office 打开时一个样).
只写工作区里的文件: 写之前和 write_file 一样过守门 (policy.check_path) 和黑名单, 写完登记成这一轮的交付.
工作台面板用的: preview() 把 Word 转成网页 (mammoth), 每段标上和 docx_read 一样的段号 (data-p), 选几句引用时带上;
export_pdf() 是你在面板上点"导出 PDF"走的那条路.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import threading
import time
from pathlib import Path

MAX_BLOCKS = 400            # docx_create 一次最多这么多块
MAX_TEXT = 200_000          # 一次写进去的字数上限
MAX_SLIDES = 60
MAX_PREVIEW = 20 * 1024 * 1024   # 预览的 Word 最大 20 MB (带图的转成网页会更大)
COM_LOCK = threading.Lock()      # 本机 Word / PowerPoint 一次只开一个

WRITE_TOOLS = {"docx_create", "docx_edit", "pptx_create", "office_to_pdf"}


def _spec(name, description, properties=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}}}


_BLOCK = {"type": "object", "properties": {
    "type": {"type": "string", "enum": ["heading", "paragraph", "bullets", "numbers", "quote", "table"]},
    "text": {"type": "string"}, "level": {"type": "integer", "minimum": 1, "maximum": 3},
    "items": {"type": "array", "items": {"type": "string"}},
    "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}, "description": "表格: 第一行是表头"}},
    "required": ["type"], "additionalProperties": False}
TOOL_SPECS = [
    _spec("docx_create", "在工作区里建一份 Word（.docx）：可选的大标题，再按顺序一块一块写（heading 小标题 / paragraph 段落 / bullets 圆点列表 / "
          "numbers 编号列表 / quote 引用 / table 表格）。已有同名文件时要 overwrite=true。写完会登记成这一轮的交付。",
          {"path": {"type": "string", "description": "相对工作区，以 .docx 结尾"}, "title": {"type": "string"},
           "blocks": {"type": "array", "items": _BLOCK}, "overwrite": {"type": "boolean"}}, ["path", "blocks"]),
    _spec("docx_read", "按\"第 N 段\"列出一份 Word 的正文段落（带样式名），和主人在工作台里引用时卡片上写的段号一致；改写前先读。表格里的字不在段落里。",
          {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, ["path"]),
    _spec("docx_edit", "按段改一份 Word：replace 换掉第 N 段的字（样式、第一处字体不变）、insert_after 在第 N 段后面加一段（同样式）、delete 删掉第 N 段。"
          "段号用改之前的（docx_read 或主人引用卡片上的）；改之前原件会存一份到工作区的 .archive。",
          {"path": {"type": "string"}, "edits": {"type": "array", "items": {"type": "object", "properties": {
              "paragraph": {"type": "integer", "minimum": 1}, "action": {"type": "string", "enum": ["replace", "insert_after", "delete"]},
              "text": {"type": "string"}}, "required": ["paragraph", "action"], "additionalProperties": False}}}, ["path", "edits"]),
    _spec("pptx_create", "在工作区里建一份 PPT（.pptx）：可选封面（title、subtitle），再一页一个标题、几条要点（bullets）、可选备注（notes）。"
          "已有同名文件时要 overwrite=true。写完会登记成这一轮的交付。",
          {"path": {"type": "string", "description": "相对工作区，以 .pptx 结尾"}, "title": {"type": "string"}, "subtitle": {"type": "string"},
           "slides": {"type": "array", "items": {"type": "object", "properties": {
               "title": {"type": "string"}, "bullets": {"type": "array", "items": {"type": "string"}}, "notes": {"type": "string"}},
               "required": ["title"], "additionalProperties": False}}, "overwrite": {"type": "boolean"}}, ["path", "slides"]),
    _spec("office_to_pdf", "把工作区里的 Word（.docx）或 PPT（.pptx）经本机 Word / PowerPoint 另存成同名 PDF（放在同一个文件夹，已有就覆盖）。"
          "要本机装着 Office。写完会登记成这一轮的交付。", {"path": {"type": "string"}}, ["path"]),
]
NAMES = {x["function"]["name"] for x in TOOL_SPECS}
READ_ONLY = {"docx_read"}
LABELS = {"docx_create": "写 Word", "docx_read": "读 Word", "docx_edit": "改 Word", "pptx_create": "做 PPT", "office_to_pdf": "导出 PDF"}


# ---- 路径和权限 ----------------------------------------------------------------
def _target(workspace: str | Path, raw: str, suffixes: tuple[str, ...]) -> Path:
    from . import policy as P
    from . import tools as T

    bad = P.raw_path_ok(raw)
    if bad:
        raise ValueError(bad)
    ws = Path(workspace).resolve()
    path = T.resolve(raw, ws)
    if not P.inside(path, ws):
        raise ValueError(f"Office 文件只放在工作区里（工作区是 {ws}）")
    if path.suffix.lower() not in suffixes:
        raise ValueError(f"文件要以 {' / '.join(suffixes)} 结尾")
    return path


def _pdf_of(path: Path) -> Path:
    return path.with_suffix(".pdf")


def decision(hub, s, name: str, args: dict):
    """工作台权限 (StudioRuntime.decision 转过来): 读和 read_file 一样、写和 write_file 一样过守门和黑名单; 只认工作区里的文件."""
    from . import policy as P

    suffixes = {"docx_create": (".docx",), "docx_read": (".docx",), "docx_edit": (".docx",), "pptx_create": (".pptx",),
                "office_to_pdf": (".docx", ".pptx")}[name]
    try:
        path = _target(s.workspace, args.get("path", ""), suffixes)
    except ValueError as exc:
        return P.Decision("deny", str(exc))
    ws, mode = Path(s.workspace), hub.teams.effective_mode(s)
    checks = ([("read_file", path)] if name == "docx_read" else
              [("read_file", path), ("write_file", _pdf_of(path))] if name == "office_to_pdf" else [("write_file", path)])
    for tool, target in checks:
        denied = hub.blacklist.check(tool, {"path": str(target)}, ws)
        if denied:
            return denied
        d = P.check_path(tool, target, mode, ws, hub.vault)
        if d.how != "auto":
            return d
    return P.Decision("auto")


# ---- Word ----------------------------------------------------------------------
def _docx_blocks(doc, title: str, blocks: list) -> int:
    if not isinstance(blocks, list) or len(blocks) > MAX_BLOCKS:
        raise ValueError(f"blocks 要是列表，最多 {MAX_BLOCKS} 块")
    total = len(title)
    if title:
        doc.add_heading(title, 0)
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("每一块要是对象")
        kind, text = block.get("type"), str(block.get("text") or "")
        items, rows = block.get("items") or [], block.get("rows") or []
        total += len(text) + sum(len(str(x)) for x in items) + sum(len(str(c)) for r in rows for c in r)
        if total > MAX_TEXT:
            raise ValueError(f"一次最多写 {MAX_TEXT} 字，分几次写")
        if kind == "heading":
            doc.add_heading(text, max(1, min(3, int(block.get("level") or 1))))
        elif kind == "paragraph":
            doc.add_paragraph(text)
        elif kind == "quote":
            doc.add_paragraph(text, style="Quote")
        elif kind in ("bullets", "numbers"):
            for item in items or [text]:
                doc.add_paragraph(str(item), style="List Bullet" if kind == "bullets" else "List Number")
        elif kind == "table":
            if not rows or not all(isinstance(r, list) for r in rows):
                raise ValueError("table 要有 rows（二维列表，第一行是表头）")
            width = max(len(r) for r in rows)
            table = doc.add_table(rows=len(rows), cols=width)
            table.style = "Table Grid"
            for i, row in enumerate(rows):
                for j in range(width):
                    cell = table.cell(i, j)
                    cell.text = str(row[j]) if j < len(row) else ""
                    if i == 0:
                        for run in cell.paragraphs[0].runs:
                            run.bold = True
        else:
            raise ValueError(f"不认识的块：{kind}")
    return len(doc.paragraphs)


def create_docx(workspace, raw: str, title: str, blocks: list, overwrite: bool = False) -> dict:
    import docx

    path = _target(workspace, raw, (".docx",))
    if path.exists() and not overwrite:
        raise ValueError(f"{path.name} 已经有了：要覆盖就带 overwrite=true，或者换个名字")
    doc = docx.Document()
    count = _docx_blocks(doc, str(title or ""), blocks)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return {"path": str(path), "paragraphs": count, "tables": len(doc.tables)}


def read_docx(workspace, raw: str, offset: int = 0, limit: int = 80) -> dict:
    import docx

    path = _target(workspace, raw, (".docx",))
    paragraphs = docx.Document(path).paragraphs
    offset, limit = max(0, int(offset or 0)), max(1, min(200, int(limit or 80)))
    rows = [{"no": i + 1, "style": p.style.name if p.style is not None else "", "text": p.text[:2000]}
            for i, p in enumerate(paragraphs[offset:offset + limit], start=offset)]
    return {"path": str(path), "total": len(paragraphs), "offset": offset, "paragraphs": rows,
            "next_offset": offset + limit if offset + limit < len(paragraphs) else None}


def _backup(workspace, path: Path) -> Path:
    folder = Path(workspace).resolve() / ".archive" / f"office-{time.strftime('%Y%m%d-%H%M%S')}"
    folder.mkdir(parents=True, exist_ok=True)
    copy = folder / path.name
    shutil.copy2(path, copy)
    return copy


def edit_docx(workspace, raw: str, edits: list) -> dict:
    """按段改: 段号都按改之前的算, 从后往前改 (前面的段号不会被挪动)."""
    import copy as _copy

    import docx
    from docx.text.paragraph import Paragraph

    path = _target(workspace, raw, (".docx",))
    if not isinstance(edits, list) or not edits or len(edits) > 200:
        raise ValueError("edits 要是 1～200 条的列表")
    doc = docx.Document(path)
    paragraphs = doc.paragraphs
    plan = []
    for edit in edits:
        n, action, text = int(edit.get("paragraph") or 0), edit.get("action"), str(edit.get("text") or "")
        if not 1 <= n <= len(paragraphs):
            raise ValueError(f"没有第 {n} 段（这份一共 {len(paragraphs)} 段，用 docx_read 看段号）")
        if action not in ("replace", "insert_after", "delete"):
            raise ValueError("action 是 replace / insert_after / delete 之一")
        if action != "delete" and not text:
            raise ValueError(f"第 {n} 段的 {action} 要给 text")
        plan.append((n, action, text))
    if len({(n, a) for n, a, _ in plan}) != len(plan):
        raise ValueError("同一段同一种改法只写一次")
    backup = _backup(workspace, path)
    for n, action, text in sorted(plan, key=lambda x: (-x[0], x[1] != "insert_after")):
        p = paragraphs[n - 1]
        if action == "delete":
            p._p.getparent().remove(p._p)
        elif action == "replace":
            runs = p.runs
            if runs:
                runs[0].text = text
                for run in runs[1:]:
                    run._r.getparent().remove(run._r)
            else:
                p.add_run(text)
        else:
            new = _copy.deepcopy(p._p)  # 只留段落格式 (样式、缩进、编号), 字和书签都不要
            for child in list(new):
                if not child.tag.endswith("}pPr"):
                    new.remove(child)
            p._p.addnext(new)
            added = Paragraph(new, p._parent)
            added.add_run(text)
    doc.save(path)
    return {"path": str(path), "edited": [{"paragraph": n, "action": a} for n, a, _ in plan],
            "paragraphs": len(docx.Document(path).paragraphs), "backup": str(backup)}


# ---- PPT -----------------------------------------------------------------------
def create_pptx(workspace, raw: str, title: str, subtitle: str, slides: list, overwrite: bool = False) -> dict:
    import pptx

    path = _target(workspace, raw, (".pptx",))
    if path.exists() and not overwrite:
        raise ValueError(f"{path.name} 已经有了：要覆盖就带 overwrite=true，或者换个名字")
    if not isinstance(slides, list) or not slides or len(slides) > MAX_SLIDES:
        raise ValueError(f"slides 要是 1～{MAX_SLIDES} 页的列表")
    deck = pptx.Presentation()
    if title:
        cover = deck.slides.add_slide(deck.slide_layouts[0])
        cover.shapes.title.text = str(title)
        if subtitle and len(cover.placeholders) > 1:
            cover.placeholders[1].text = str(subtitle)
    for spec in slides:
        if not isinstance(spec, dict) or not str(spec.get("title") or "").strip():
            raise ValueError("每页要有 title")
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = str(spec["title"])
        bullets = [str(b) for b in spec.get("bullets") or []]
        frame = slide.placeholders[1].text_frame
        frame.text = bullets[0] if bullets else ""
        for bullet in bullets[1:]:
            frame.add_paragraph().text = bullet
        if spec.get("notes"):
            slide.notes_slide.notes_text_frame.text = str(spec["notes"])
    path.parent.mkdir(parents=True, exist_ok=True)
    deck.save(path)
    return {"path": str(path), "slides": len(deck.slides)}


# ---- 转 PDF (本机 Word / PowerPoint) ----------------------------------------------
def to_pdf(workspace, raw: str) -> dict:
    path = _target(workspace, raw, (".docx", ".pptx"))
    if not path.is_file():
        raise ValueError(f"找不到 {path.name}")
    target = _pdf_of(path)
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise ValueError("转 PDF 要本机装着 Office（Word / PowerPoint）和 pywin32") from exc
    with COM_LOCK:
        pythoncom.CoInitialize()
        try:
            if path.suffix.lower() == ".docx":
                app = win32com.client.DispatchEx("Word.Application")
                app.Visible, app.DisplayAlerts = False, 0
                try:
                    doc = app.Documents.Open(str(path), ReadOnly=True, AddToRecentFiles=False)
                    try:
                        doc.SaveAs2(str(target), FileFormat=17)  # wdFormatPDF
                    finally:
                        doc.Close(False)
                finally:
                    app.Quit()
            else:
                app = win32com.client.DispatchEx("PowerPoint.Application")
                try:
                    deck = app.Presentations.Open(str(path), ReadOnly=True, Untitled=False, WithWindow=False)
                    try:
                        deck.SaveAs(str(target), 32)  # ppSaveAsPDF
                    finally:
                        deck.Close()
                finally:
                    app.Quit()
        except Exception as exc:  # noqa: BLE001 (COM 报的错五花八门: 没装、没激活、文件被占用)
            raise ValueError(f"本机 Office 没能转成 PDF：{str(exc)[:200]}") from exc
        finally:
            pythoncom.CoUninitialize()
    if not target.is_file():
        raise ValueError("本机 Office 没写出 PDF")
    return {"path": str(target), "size": target.stat().st_size}


# ---- 她调的入口 (StudioRuntime.call 转过来) -------------------------------------------
async def call_tool(hub, s, name: str, args: dict):
    ws = s.workspace
    if name == "docx_read":
        return await asyncio.to_thread(read_docx, ws, args.get("path", ""), args.get("offset", 0), args.get("limit", 80))
    if name == "docx_create":
        out = await asyncio.to_thread(create_docx, ws, args.get("path", ""), args.get("title", ""), args.get("blocks"), bool(args.get("overwrite")))
    elif name == "docx_edit":
        out = await asyncio.to_thread(edit_docx, ws, args.get("path", ""), args.get("edits"))
    elif name == "pptx_create":
        out = await asyncio.to_thread(create_pptx, ws, args.get("path", ""), args.get("title", ""), args.get("subtitle", ""),
                                      args.get("slides"), bool(args.get("overwrite")))
    elif name == "office_to_pdf":
        out = await asyncio.to_thread(to_pdf, ws, args.get("path", ""))
    else:
        raise ValueError("没有这个 Office 工具")
    if s.run.get("id"):  # 写出来的文件算这一轮的交付 (任务与成果里看得到)
        try:
            await hub.register_delivery(s, out["path"])
        except (OSError, ValueError) as exc:
            out["note"] = f"文件写好了，但没登记成交付：{exc}"
    return out


# ---- 工作台面板: Word 预览 (每段标上段号) 和导出 PDF -------------------------------------
_KEEP = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "blockquote", "table", "thead", "tbody", "tr", "td", "th",
         "strong", "em", "u", "s", "sup", "sub", "br", "a", "img", "div", "span"}
_DROP = {"script", "style", "iframe", "object", "embed", "form", "input", "button", "link", "meta"}
STYLE_MAP = """p[style-name='Title'] => h1.title:fresh
p[style-name='Subtitle'] => h2.subtitle:fresh
p[style-name='Quote'] => blockquote > p:fresh
p[style-name='Intense Quote'] => blockquote > p:fresh"""


def _clean(root) -> None:
    """mammoth 出的网页只留排版用的标签和属性 (链接只留 http / https / mailto, 图只留 data:image), 放进工作台页面里也不会跑脚本."""
    for el in list(root.iter()):
        if el is root or not isinstance(el.tag, str):
            continue
        if el.tag in _DROP:
            el.drop_tree()
            continue
        if el.tag not in _KEEP:
            el.drop_tag()
            continue
        keep = {}
        if el.tag == "a" and re.match(r"^(https?:|mailto:)", el.get("href", ""), re.I):
            keep = {"href": el.get("href")}
        elif el.tag == "img" and el.get("src", "").startswith("data:image/"):
            keep = {"src": el.get("src"), "alt": el.get("alt", "")}
        elif el.tag in ("td", "th"):
            keep = {k: el.get(k) for k in ("colspan", "rowspan") if el.get(k, "").isdigit()}
        elif el.get("class") in ("title", "subtitle"):  # STYLE_MAP 标的 Word 大标题 / 副标题
            keep = {"class": el.get("class")}
        el.attrib.clear()
        el.attrib.update(keep)


def _own_text(el) -> str:
    """一段自己的字 (列表项不算它下面嵌的子列表)."""
    if el.tag != "li":
        return el.text_content()
    parts = [el.text or ""]
    for child in el:
        if child.tag not in ("ul", "ol"):
            parts.append(child.text_content())
        parts.append(child.tail or "")
    return "".join(parts)


def preview(session, raw: str, *, blacklist=None) -> dict:
    import docx
    import lxml.html
    import mammoth

    ws = Path(session.workspace).resolve()
    path = _target(ws, raw, (".docx",))
    if blacklist is not None and (denied := blacklist.check("read_file", {"path": str(path)}, ws)):
        raise PermissionError(denied.why)
    if not path.is_file():
        raise ValueError("找不到这个文件")
    if path.stat().st_size > MAX_PREVIEW:
        raise ValueError("Word 超过 20 MB，预览不了；可以导出 PDF 再看")
    with path.open("rb") as f:  # Word 的"标题 / 副标题 / 引用"样式 mammoth 默认不认, 映射成网页里对应的样子
        html = mammoth.convert_to_html(f, style_map=STYLE_MAP).value
    paragraphs = docx.Document(path).paragraphs
    root = lxml.html.fragment_fromstring(html or "<p></p>", create_parent="div")
    _clean(root)
    # 段号: mammoth 按文档顺序一段出一个块 (空段跳过); 和 python-docx 的正文段落 (docx_read 的"第 N 段") 按字对齐, 表格里的不算
    norm = lambda text: re.sub(r"\s+", "", text or "")  # noqa: E731
    texts, j = [norm(p.text) for p in paragraphs], 0
    for el in root.iter("p", "h1", "h2", "h3", "h4", "h5", "h6", "li"):
        if any(a.tag == "table" for a in el.iterancestors()):
            continue
        mine = norm(_own_text(el))
        if not mine:
            continue
        k = j
        while k < len(texts) and texts[k] != mine:
            k += 1
        if k < len(texts):
            el.set("data-p", str(k + 1))
            j = k + 1
    info = path.stat()
    return {"kind": "docx", "path": path.relative_to(ws).as_posix(), "name": path.name, "html": lxml.html.tostring(root, encoding="unicode"),
            "paragraphs": len(paragraphs), "size": info.st_size, "modified": info.st_mtime}


async def export_pdf(session, raw: str, *, blacklist=None) -> dict:
    """你在面板上点"导出 PDF": 读原件、写 PDF 都过黑名单 (你点的就是同意了, 不再弹申请)."""
    ws = Path(session.workspace).resolve()
    path = _target(ws, raw, (".docx", ".pptx"))
    if blacklist is not None:
        for tool, target in (("read_file", path), ("write_file", _pdf_of(path))):
            if (denied := blacklist.check(tool, {"path": str(target)}, ws)):
                raise PermissionError(denied.why)
    out = await asyncio.to_thread(to_pdf, ws, str(path))
    return {"path": Path(out["path"]).relative_to(ws).as_posix(), "size": out["size"]}

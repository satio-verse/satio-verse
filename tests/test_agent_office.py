"""工作台第 7 块: Office 文档 (wm/agent/office.py) —— 建 Word、按"第 N 段"读和改 (改之前留原件)、建 PPT、Word 预览的段号
和 docx_read 一致、只写工作区里的文件; 本机有 Office 时再试一次转 PDF."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

docx = pytest.importorskip("docx")
pptx = pytest.importorskip("pptx")
pytest.importorskip("mammoth")

from wm.agent import office as OF  # noqa: E402
from wm.agent import policy as P  # noqa: E402
from wm.agent import studio as ST  # noqa: E402

BLOCKS = [{"type": "heading", "text": "本周", "level": 1}, {"type": "paragraph", "text": "结论：第 5 块做完了。"},
          {"type": "bullets", "items": ["插件页开关真改到运行时", "预设做成卡片"]},
          {"type": "table", "rows": [["项", "状态"], ["第 5 块", "做完"]]},
          {"type": "quote", "text": "下周做第 7 块。"}]


def test_create_read_and_numbering(tmp_path):
    out = OF.create_docx(tmp_path, "docs/周报.docx", "周报", BLOCKS)
    assert out["tables"] == 1 and (tmp_path / "docs" / "周报.docx").is_file()
    got = OF.read_docx(tmp_path, "docs/周报.docx")
    texts = [(row["no"], row["text"]) for row in got["paragraphs"]]
    # 第 N 段 = 正文段落 (表格里的不算): 标题、小标题、结论、两条圆点、引用
    assert texts == [(1, "周报"), (2, "本周"), (3, "结论：第 5 块做完了。"), (4, "插件页开关真改到运行时"), (5, "预设做成卡片"), (6, "下周做第 7 块。")]
    assert got["paragraphs"][3]["style"] == "List Bullet"
    with pytest.raises(ValueError, match="已经有了"):
        OF.create_docx(tmp_path, "docs/周报.docx", "", BLOCKS)


def test_edit_keeps_styles_counts_from_the_original_and_backs_up(tmp_path):
    OF.create_docx(tmp_path, "周报.docx", "周报", BLOCKS)
    out = OF.edit_docx(tmp_path, "周报.docx", [
        {"paragraph": 3, "action": "replace", "text": "结论：第 5 块做完了，第 7 块开工。"},
        {"paragraph": 4, "action": "insert_after", "text": "工作经验页"},
        {"paragraph": 6, "action": "delete"}])
    rows = OF.read_docx(tmp_path, "周报.docx")["paragraphs"]
    assert [r["text"] for r in rows] == ["周报", "本周", "结论：第 5 块做完了，第 7 块开工。", "插件页开关真改到运行时", "工作经验页", "预设做成卡片"]
    assert rows[4]["style"] == "List Bullet"  # 加的那段照前一段的样式
    backup = Path(out["backup"])
    assert backup.is_file() and backup.parent.parent == tmp_path / ".archive" and backup.parent.name.startswith("office-")
    with pytest.raises(ValueError, match="没有第 99 段"):
        OF.edit_docx(tmp_path, "周报.docx", [{"paragraph": 99, "action": "delete"}])


def test_pptx_cover_slides_and_notes(tmp_path):
    out = OF.create_pptx(tmp_path, "周报.pptx", "周报", "2026-11-02", [
        {"title": "做完了", "bullets": ["第 5 块", "第七轮"], "notes": "先讲结论"}, {"title": "下周", "bullets": ["第 7 块"]}])
    assert out["slides"] == 3
    deck = pptx.Presentation(tmp_path / "周报.pptx")
    assert deck.slides[1].shapes.title.text == "做完了"
    assert [p.text for p in deck.slides[1].placeholders[1].text_frame.paragraphs] == ["第 5 块", "第七轮"]
    assert deck.slides[1].notes_slide.notes_text_frame.text == "先讲结论"


def test_preview_numbers_paragraphs_like_docx_read_and_strips_unsafe_bits(tmp_path):
    OF.create_docx(tmp_path, "周报.docx", "周报", BLOCKS)
    got = OF.preview(SimpleNamespace(workspace=str(tmp_path)), "周报.docx")
    html = got["html"]
    assert got["paragraphs"] == 6 and "<script" not in html and "<table>" in html
    assert 'data-p="1"' in html and 'data-p="5">预设做成卡片' in html and 'data-p="6"' in html
    # 表格里的字没有段号
    assert "data-p" not in html.split("<table>")[1].split("</table>")[0]


def test_only_inside_the_workspace_and_follows_the_guard(tmp_path):
    with pytest.raises(ValueError, match="只放在工作区里"):
        OF.create_docx(tmp_path, str(tmp_path.parent / "外面.docx"), "", BLOCKS)
    with pytest.raises(ValueError, match="结尾"):
        OF.create_docx(tmp_path, "周报.txt", "", BLOCKS)
    hub = SimpleNamespace(teams=SimpleNamespace(effective_mode=lambda s: s.mode), vault=None,
                          blacklist=SimpleNamespace(check=lambda tool, args, ws: None))
    s = SimpleNamespace(workspace=str(tmp_path), mode="read")
    assert OF.decision(hub, s, "docx_create", {"path": "a.docx"}).how == "deny"   # 仅可查看: 不能写
    assert OF.decision(hub, s, "docx_read", {"path": "a.docx"}).how == "auto"
    s.mode = "workspace"
    assert OF.decision(hub, s, "docx_edit", {"path": "a.docx"}).how == "auto"
    assert OF.decision(hub, s, "pptx_create", {"path": "../a.pptx"}).how == "deny"
    blocked = SimpleNamespace(check=lambda tool, args, ws: P.Decision("deny", "黑名单") if tool == "write_file" and args["path"].endswith(".pdf") else None)
    hub.blacklist = blocked
    assert OF.decision(hub, s, "office_to_pdf", {"path": "a.docx"}).why == "黑名单"  # 写出的 PDF 也过黑名单


def test_registered_with_the_studio_tools():
    assert OF.NAMES <= ST.NAMES and "docx_read" in ST.READ_ONLY and "docx_edit" not in ST.READ_ONLY
    assert ST.LABELS["office_to_pdf"] == "导出 PDF"


def _office_ready():
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            win32com.client.DispatchEx("Word.Application").Quit()
        finally:
            pythoncom.CoUninitialize()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _office_ready(), reason="本机没有能用的 Word")
def test_word_to_pdf_through_local_office(tmp_path):
    OF.create_docx(tmp_path, "周报.docx", "周报", BLOCKS)
    got = asyncio.run(OF.export_pdf(SimpleNamespace(workspace=str(tmp_path)), "周报.docx"))
    assert got["path"] == "周报.pdf" and (tmp_path / "周报.pdf").read_bytes().startswith(b"%PDF")


def test_workbench_preview_and_pdf_endpoints(tmp_path):
    """面板用的接口: Word 预览带段号; 只肯直接打开工作区里的 PDF (别的文件、工作区外的都不给)."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from wm.agent.governance import BlacklistStore
    from wm.agent.workspace_http import workspace

    ws = tmp_path / "ws"
    ws.mkdir()
    OF.create_docx(ws, "周报.docx", "周报", BLOCKS)
    (ws / "a.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (ws / "a.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    s = SimpleNamespace(id="a", workspace=str(ws), mode="workspace", preset="standard", status="idle", task=None, events=[])
    hub = SimpleNamespace(get={"a": s}.get, blacklist=BlacklistStore(tmp_path / "state", defaults=False), home=tmp_path / "agent")

    async def handler(request):
        return await workspace(request, hub)

    async def main():
        app = web.Application()
        app.router.add_route("*", "/api/agent/workspace", handler)
        async with TestClient(TestServer(app)) as client:
            got = await (await client.get("/api/agent/workspace", params={"action": "docx_preview", "id": "a", "path": "周报.docx"})).json()
            assert got["kind"] == "docx" and got["paragraphs"] == 6 and 'data-p="4"' in got["html"]
            pdf = await client.get("/api/agent/workspace", params={"action": "raw", "id": "a", "path": "a.pdf"})
            assert pdf.status == 200 and pdf.headers["Content-Type"] == "application/pdf" and (await pdf.read()).startswith(b"%PDF")
            for bad in ("a.html", "../x.pdf"):
                refused = await client.get("/api/agent/workspace", params={"action": "raw", "id": "a", "path": bad})
                assert refused.status == 400
    asyncio.run(main())


def test_workbench_raw_serves_media_but_not_scriptable_files(tmp_path):
    """2026-11-05 用户: "为什么工作台右侧功能栏的文件列表点击后看不了图片、视频、文档呢" —— 以前 raw 只给 PDF.
    现在图片、视频、音频按后缀给 (视频能按 Range 拖进度), 带 nosniff + sandbox; HTML、SVG 照样不给."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from wm.agent.governance import BlacklistStore
    from wm.agent.workspace_http import workspace

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ep01.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40)
    (ws / "封面.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    (ws / "配音.wav").write_bytes(b"RIFF" + b"\x00" * 64)
    (ws / "a.svg").write_text("<svg onload=alert(1)></svg>", encoding="utf-8")
    (ws / "fake.png.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    s = SimpleNamespace(id="a", workspace=str(ws), mode="workspace", preset="standard", status="idle", task=None, events=[])
    hub = SimpleNamespace(get={"a": s}.get, blacklist=BlacklistStore(tmp_path / "state", defaults=False), home=tmp_path / "agent")

    async def handler(request):
        return await workspace(request, hub)

    async def main():
        app = web.Application()
        app.router.add_route("*", "/api/agent/workspace", handler)
        async with TestClient(TestServer(app)) as client:
            def raw(path, **headers):
                return client.get("/api/agent/workspace", params={"action": "raw", "id": "a", "path": path}, headers=headers)
            for path, kind in (("ep01.mp4", "video/mp4"), ("封面.png", "image/png"), ("配音.wav", "audio/wav")):
                r = await raw(path)
                assert r.status == 200 and r.headers["Content-Type"] == kind, path
                assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Content-Security-Policy"] == "sandbox"
            part = await raw("ep01.mp4", Range="bytes=12-21")
            assert part.status == 206 and await part.read() == bytes(range(10))
            for bad in ("a.svg", "fake.png.html"):
                assert (await raw(bad)).status == 400
    asyncio.run(main())

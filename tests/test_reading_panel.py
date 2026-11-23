"""阅读面板 (工具1 之二, wm/agent/reading.py + notebook_http 的 view=read / quote): 按页排好的视图、读不了的原样说原因、
选中发给她的块登记成"查到过"、整条链路对假模型、全程不联网. 都在临时目录里, 不调真模型."""
import asyncio
import base64
import hashlib
import json
import socket
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import reading_fixtures as RF
from docparse_fixtures import pptx_bytes, xlsx_bytes
from test_agent_notebook import run, setup_hub
from wm.agent import notebook as NB
from wm.agent import notebook_http, reading
from wm.agent.core import AgentHub, Session

ROOT = Path(__file__).resolve().parents[1]
GUIDE = "# 指南\n\n## 安装\n\n先装 Python。\n\n## 使用\n\n再运行脚本。\n"


@pytest.fixture
def store(tmp_path):
    return NB.NotebookStore(tmp_path / "agent")


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode() + p.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------- 验收 1: 正文和页码
def test_pdf_pages_keep_page_numbers_and_blank_pages_are_marked(store):
    nid = store.create("读")["id"]
    src = store.add_file(nid, "three.pdf", RF.three_page_pdf())
    v = reading.view(store, nid, src["no"])
    assert v["readable"] and v["source"]["pdf"] and v["unit_kind"] == "page"
    assert [u["page"] for u in v["units"]] == [1, 2, 3] and [u["n"] for u in v["units"]] == [1, 2, 3]
    assert [u["label"] for u in v["units"]] == ["第 1 页", "第 2 页", "第 3 页"]
    assert v["units"][0]["blocks"][0]["text"] == RF.PAGE1 and v["units"][2]["blocks"][0]["text"] == RF.PAGE3
    assert v["units"][1]["empty"] and v["units"][1]["blocks"] == [] and "没读出文字" in v["units"][1]["note"]
    assert "第 2 页没读出字" in v["note"]  # 部分读好: 资料本记的原话带给界面
    # 每块的编号和位置跟她检索 / 读原文看到的一致
    hit = store.search(nid, "quarterly budget", k=1)["results"][0]
    block = v["units"][2]["blocks"][0]
    assert hit["key"] == block["key"] and hit["label"] == block["label"] == "第 3 页"
    assert store.read(nid, block["key"], 0)["blocks"][0]["text"] == block["text"]
    assert v["total_chars"] == len(RF.PAGE1) + len(RF.PAGE3)


def test_word_paragraphs_group_into_units_with_ranges(store):
    nid = store.create("读")["id"]
    src = store.add_file(nid, "notes.docx", RF.notes_docx())
    v = reading.view(store, nid, src["no"])
    assert v["readable"] and v["unit_kind"] == "block" and len(v["units"]) >= 2
    assert v["units"][0]["label"].startswith("段落 1–") and v["units"][-1]["label"].endswith("–40")
    keys = [b["key"] for u in v["units"] for b in u["blocks"]]
    assert keys == [f"S{src['no']}-{i}" for i in range(1, len(keys) + 1)]  # 不丢块、不乱序
    assert all(u["blocks"] and sum(len(b["text"]) for b in u["blocks"]) < reading.UNIT_CHARS + NB.CHUNK_MAX for u in v["units"])


def test_slides_and_sheets_are_their_own_units(store):
    nid = store.create("读")["id"]
    slides = reading.view(store, nid, store.add_file(nid, "s.pptx", pptx_bytes([["第一张：苹果三个"], ["第二张：预算 4200"]]))["no"])
    assert slides["unit_kind"] == "slide" and [u["label"] for u in slides["units"]] == ["幻灯片 1", "幻灯片 2"]
    sheets = reading.view(store, nid, store.add_file(nid, "x.xlsx", xlsx_bytes({"销售": [["a", 1]], "成本": [["b", 2]]}))["no"])
    assert sheets["unit_kind"] == "sheet" and [u["label"] for u in sheets["units"]] == ["工作表「销售」", "工作表「成本」"]


def test_pasted_text_and_markdown_are_readable(store):
    nid = store.create("读")["id"]
    md = reading.view(store, nid, store.add_text(nid, "指南", GUIDE)["no"])
    assert md["readable"] and md["source"]["pdf"] is False and md["units"][0]["blocks"][0]["key"] == "S1-1"
    assert "指南" in md["units"][0]["label"]


# ---------------------------------------------------------------- 验收 3: 读不了就说读不了, 不上云端
@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("阅读面板碰了网络")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


BAD_WORDS = ("上传", "云端帮你", "改走云端", "转云端", "交给云端", "mineru")


def test_unreadable_sources_say_why_and_never_reach_for_the_cloud(store, no_network):
    nid = store.create("读")["id"]
    scan = store.add_file(nid, "scan.pdf", RF.scanned_pdf())
    enc = store.add_file(nid, "enc.pdf", RF.encrypted_pdf())
    assert scan["status"] == "needs_ocr" and enc["status"] == "error"
    for src, word in ((scan, "OCR"), (enc, "加密")):
        v = reading.view(store, nid, src["no"])
        assert v["readable"] is False and v["units"] == [] and word in v["reason"]
        assert v["source"]["status"] == src["status"] and v["source"]["pdf"]
        assert not any(w in json.dumps(v, ensure_ascii=False) for w in BAD_WORDS)
    # 乱码 / 图片: 资料本记了状态但没有原话时用固定兜底
    nb = store._load(nid)
    nb["sources"][0].update(status="garbled", reader="")
    nb["sources"][1].update(status="image", reader="")
    store._save(nb)
    assert "乱码" in reading.view(store, nid, 1)["reason"] and "图片" in reading.view(store, nid, 2)["reason"]
    with pytest.raises(ValueError):
        reading.quote(store, "s1", nid, ["S1-1"])  # 没有块可登记


def test_reading_module_has_no_network_or_process_imports():
    text = (ROOT / "wm" / "agent" / "reading.py").read_text("utf-8")
    for word in ("import socket", "urllib", "aiohttp", "requests", "subprocess", "httpx", "mineru", "docparse"):
        assert word not in text, word


def test_view_is_read_only_and_checks_its_arguments(store):
    nid = store.create("读")["id"]
    src = store.add_file(nid, "three.pdf", RF.three_page_pdf())
    before = tree_hash(store.home)
    reading.view(store, nid, src["no"])
    assert tree_hash(store.home) == before
    with pytest.raises(ValueError):
        reading.view(store, nid, 99)
    with pytest.raises(ValueError):
        reading.view(store, "nb-00000000", 1)
    with pytest.raises(ValueError):
        reading.view(store, "../x", 1)


# ---------------------------------------------------------------- 验收 2: 登记出处、整条链路
def test_quote_validates_keys_and_marks_them_seen(store):
    nid = store.create("读")["id"]
    other = store.create("别的")["id"]
    store.add_file(nid, "three.pdf", RF.three_page_pdf())
    store.add_text(other, "别的资料", "别的本里的内容。")
    store.attach("s1", nid)
    check = store.citation_check("s1")
    assert check("见 [S1-1]")  # 还没登记: 打回
    got = reading.quote(store, "s1", nid, ["S1-1", "S01-001", "S1-2"])
    assert got["keys"] == ["S1-1", "S1-2"] and got["labels"] == {"S1-1": "第 1 页", "S1-2": "第 3 页"}
    assert check("见 [S1-1]，还有 [S1-2]") == []
    for bad in ([], None, "S1-1", ["瞎写"], ["S1-9"], ["S9-1"]):
        with pytest.raises(ValueError):
            reading.quote(store, "s1", nid, bad)
    with pytest.raises(ValueError):
        reading.quote(store, "s1", nid, [f"S1-{i}" for i in range(1, 50)])
    with pytest.raises(ValueError):
        reading.quote(store, "s1", other, ["S1-2"])  # 别的本里没有 S1-2: 不串
    assert check("见 [S1-1]") == []  # 失败的调用没把东西弄坏


def test_round_trip_with_fake_model_quote_makes_the_citation_stand(tmp_path):
    hub, s, seen, ws = setup_hub(tmp_path, lambda sess, n: {"content": "页面里写着：果园有三个苹果 [S1-1]。"})
    store = hub.notebooks
    nid = store.create("课程")["id"]
    store.add_file(nid, "three.pdf", RF.three_page_pdf())
    store.attach(s.id, nid)
    reading.quote(store, s.id, nid, ["S1-1"])
    run(hub, s, "【边读边问】《three.pdf》第 1 页\n<<<资料原文\n" + RF.PAGE1 + "\n资料原文>>>\n我的问题：果园里有几个苹果？")
    assert s.run["state"] == "completed" and s.run["check"]["verdict"] == "ok" and not s.run.get("rechecked")


def test_round_trip_without_quote_the_citation_is_sent_back(tmp_path):
    def script(sess, n):
        return {"content": "果园有三个苹果 [S1-1]。" if n == 1 else "去掉了没核对的出处：页面里写着果园有三个苹果。"}
    hub, s, seen, ws = setup_hub(tmp_path, script)
    store = hub.notebooks
    nid = store.create("课程")["id"]
    store.add_file(nid, "three.pdf", RF.three_page_pdf())
    store.attach(s.id, nid)
    run(hub, s, "果园里有几个苹果？")
    assert s.run["rechecked"] and "S1-1" in seen[1][-1]["content"] and "没查到过" in seen[1][-1]["content"]


# ---------------------------------------------------------------- 本机接口
def test_http_view_and_quote(tmp_path):
    async def main():
        hub = AgentHub({"agent": {"workspace": str(tmp_path / "ws")}}, home=tmp_path / "agent")
        s = Session("read-s", str(tmp_path / "ws"))
        hub.sessions[s.id] = s
        hub.save(s)
        app = web.Application()
        notebook_http.register(app, lambda r: "Origin 不对" if r.headers.get("Origin") == "https://evil.example" else None, hub)
        async with TestClient(TestServer(app)) as c:
            api = "/api/agent/notebooks"
            nid = (await (await c.post(api, json={"action": "create", "title": "读"})).json())["notebook"]["id"]
            up = await (await c.post(api, json={"action": "add", "id": nid, "name": "three.pdf",
                                                "data": base64.b64encode(RF.three_page_pdf()).decode()})).json()
            no = up["added"]["no"]
            assert (await c.get(api, params={"id": nid, "source": str(no), "view": "read"},
                                headers={"Origin": "https://evil.example"})).status == 403
            v = await (await c.get(api, params={"id": nid, "source": str(no), "view": "read"})).json()
            assert [u["page"] for u in v["units"]] == [1, 2, 3] and v["source"]["pdf"]
            for params in ({"id": nid, "view": "read"}, {"id": nid, "source": "9", "view": "read"}, {"id": nid, "source": "x", "view": "read"}):
                assert (await c.get(api, params=params)).status == 400
            q = await c.post(api, json={"action": "quote", "id": nid, "session_id": s.id, "keys": ["S1-1"]})
            assert (await q.json())["keys"] == ["S1-1"]
            assert hub.notebooks.citation_check(s.id)("见 [S1-1]") == []
            for body in ({"session_id": s.id, "keys": ["S1-9"]}, {"session_id": "nope", "keys": ["S1-1"]}, {"session_id": s.id, "keys": []}):
                assert (await c.post(api, json={"action": "quote", "id": nid, **body})).status == 400
            old = await (await c.get(api, params={"id": nid})).json()  # 不带 view: 还是原来的详情
            assert "units" not in old and old["sources"][0]["no"] == no
    asyncio.run(main())


def test_new_web_files_exist_and_are_served():
    from wm import app
    for name in ("workbench-reading.js", "workbench-reading.css"):
        assert name in app._PET_FILES and (app.PET_WEB / name).is_file(), name

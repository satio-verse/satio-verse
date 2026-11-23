"""文档解析在工作台这边 (wm/agent/docparse.py + studio.py 里的登记): 她调 doc_parse / doc_backends 走真的 _tool (权限、黑名单都过),
模型换成脚本 (验收 2: 她通过工具读一个测试文档并据此回答, 整条链路对假模型跑通). 识字用假识字子进程, 没有真模型、没有网络.
没验的: 真 RapidOCR 识字 (没装); 在管理员权限的会话里跑的, 起的子进程也带管理员权限."""
import asyncio
import json
import types

import pytest

from docparse_fixtures import docx_bytes, installed_ocr_dir, pdf_bytes, stub_cmd
from wm import docparse as DPK
from wm.agent import docparse as DP, policy as P, studio as ST
from wm.agent.core import AgentHub, Session



class FakeLLM:
    provider = "custom"

    async def close(self):
        pass


class FakeChannels:
    conf = {}

    async def agent_ask(self, key, title, text, on_answer):
        return "d1"

    async def agent_ask_done(self, did, how):
        pass

    async def agent_doing(self, text):
        pass


class Stage:
    def __init__(self, tmp_path, script, mode="workspace"):
        self.tmp, self.script, self.mode = tmp_path, script, mode
        self.seen, self.events = [], []

    async def __aenter__(self):
        self.ws = self.tmp / "workspace"
        self.ws.mkdir(exist_ok=True)
        self.hub = AgentHub({"agent": {"workspace": str(self.ws)}}, engines=types.SimpleNamespace(channels=FakeChannels(), memory=None),
                            home=self.tmp / "agent")
        self.hub._llm = lambda _s: FakeLLM()

        async def ask_model(sess, llm, tools):
            self.seen.append(sess.request_messages()[1:])
            out = self.script(sess, len(self.seen))
            out = await out if asyncio.iscoroutine(out) else out
            return {"content": out.get("content", ""), "tool_calls": out.get("tool_calls", []), "reasoning": ""}
        self.hub._ask_model = ask_model
        self.s = Session("dp-a", str(self.ws), mode=self.mode, model="offline-test")
        self.hub.sessions[self.s.id] = self.s
        self.hub.save(self.s)

        async def sink(ev):
            self.events.append(ev)
        self.hub.subscribe(self.s.id, sink)
        return self

    async def __aexit__(self, *exc):
        await self.hub.downloads.close()

    async def turn(self, text):
        assert await self.hub.send(self.s.id, text) is None
        for _ in range(500):
            if self.s.task is not None:
                break
            await asyncio.sleep(0.01)
        await self.s.task

    async def tool(self, tool, /, **args):
        return await self.hub._tool(self.s, {"id": f"t{len(self.events)}", "function": {"name": tool, "arguments": json.dumps(args)}},
                                    self.hub._offered(self.s))

    def of(self, kind):
        return [e for e in self.events if e["type"] == kind]


def call(n, name, **args):
    return {"content": "我去读一下", "tool_calls": [{"id": f"c{n}", "name": name, "args": json.dumps(args)}]}


def tool_result(msgs) -> dict:
    return json.loads(msgs[-1]["content"].split("\n", 1)[1])      # 她拿到的工具结果 (第一行是调用说明)


def run(main):
    return asyncio.run(main())


@pytest.fixture(autouse=True)
def _engine(monkeypatch, tmp_path):
    """识字组件目录和假识字命令; 默认没装."""
    monkeypatch.setattr(DP, "ENGINE_ARGS", {"ocr_dir": tmp_path / "no-ocr"})
    cloud = types.SimpleNamespace(info=DPK.BackendInfo(id="fake-cloud", name="假云端", kind="cloud", upload=True, auto=False,
                                                       needs_consent=True), calls=0)
    DPK.register_backend(cloud)
    yield cloud
    DPK.unregister_backend("fake-cloud")


# ---------------------------------------------------------------- 验收 2: 读文档、据此回答, 整条链路
def test_she_reads_a_pdf_through_the_tool_and_answers_from_it(tmp_path):
    secret = "SIGNED BY ZHANG ON PAGE TWO"

    def script(s, n):
        if n == 1:
            return call(n, "doc_parse", path="contract.pdf")
        got = tool_result(s.request_messages()[1:])
        page2 = next(x for x in got["parts"] if x["page"] == 2)
        return {"content": f"合同第 {page2['page']} 页写着：{page2['text']}"}

    async def main():
        async with Stage(tmp_path, script) as st:
            (st.ws / "contract.pdf").write_bytes(pdf_bytes([("text", "CONTRACT TITLE"), ("text", secret)]))
            await st.turn("合同第二页写了什么？")
            result = tool_result(st.seen[1])
            assert result["status"] == "ready" and [x["page"] for x in result["parts"]] == [1, 2] and result["parts"][1]["text"] == secret
            assert result["cloud"]["used"] is False
            answer = [e for e in st.of("assistant")][-1]["text"]
            assert answer == f"合同第 2 页写着：{secret}"
            row = next(e for e in st.events if e["type"] == "tool" and e["name"] == "doc_parse")
            assert row["label"] == "解析文档" and row["args"] == "contract.pdf" and row["state"] == "running"
            done = next(e for e in st.events if e["type"] == "tool_result")
            assert done["ok"] is True
    run(main)


def test_a_scanned_page_is_read_when_the_local_ocr_component_is_there(tmp_path, monkeypatch):
    monkeypatch.setattr(DP, "ENGINE_ARGS", {"ocr_dir": installed_ocr_dir(tmp_path / "ocr"), "ocr_cmd": stub_cmd()})

    def script(s, n):
        if n == 1:
            return call(n, "doc_parse", path="scan.pdf")
        page = tool_result(s.request_messages()[1:])["parts"][-1]
        return {"content": f"第 {page['page']} 页（识字）：{page['text']}"}

    async def main():
        async with Stage(tmp_path, script) as st:
            (st.ws / "scan.pdf").write_bytes(pdf_bytes([("text", "COVER PAGE"), ("scan", "扫描件里的暗号")]))
            await st.turn("读一下这个扫描件")
            result = tool_result(st.seen[1])
            assert result["backend"] == "builtin+local-ocr" and result["parts"][1]["how"] == "ocr"
            assert [e for e in st.of("assistant")][-1]["text"] == "第 2 页（识字）：扫描件里的暗号"
    run(main)


# ---------------------------------------------------------------- 验收 3: 读不了就明说, 不自动转云端
def test_a_scan_with_no_ocr_component_comes_back_unreadable_and_she_says_so(tmp_path, _engine):
    def script(s, n):
        if n == 1:
            return call(n, "doc_parse", path="scan.pdf")
        got = tool_result(s.request_messages()[1:])
        return {"content": "这份读不了：" + got["unread"][0]["reason"] if got["status"] == "unreadable" else "读到了"}

    async def main():
        async with Stage(tmp_path, script) as st:
            (st.ws / "scan.pdf").write_bytes(pdf_bytes([("scan", "藏着的字")]))
            await st.turn("读一下")
            result = tool_result(st.seen[1])
            assert result["status"] == "unreadable" and result["parts"] == [] and "还没装" in result["unread"][0]["reason"]
            text = json.dumps({k: v for k, v in result.items() if k != "cloud"}, ensure_ascii=False)
            assert not any(g in text for g in ("上传", "云端帮你", "改走云端"))
            assert [e for e in st.of("assistant")][-1]["text"].startswith("这份读不了：")
            assert st.of("ask") == []            # 不弹任何卡片: 没有"要不要传云端"这回事
            assert _engine.calls == 0
    run(main)


def test_doc_backends_lists_the_three_tiers(tmp_path):
    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "ok"}) as st:
            out, error = await st.tool("doc_backends")
            assert not error
            got = json.loads(out if isinstance(out, str) else out.text)
            rows = {b["id"]: b for b in got["backends"]}
            assert rows["builtin-text"]["available"] and not rows["rapidocr-local"]["available"]
            assert rows["mineru-cloud"]["available"] is False and rows["mineru-cloud"]["upload"] is True
            assert got["cloud"]["used"] is False
    run(main)


def test_a_file_that_does_not_exist_is_an_error_not_a_crash(tmp_path):
    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "ok"}) as st:
            out, error = await st.tool("doc_parse", path="nope.pdf")
            assert error and "没有这个文件" in str(out)
            out, error = await st.tool("doc_parse", path="")
            assert error and "path" in str(out)
    run(main)


# ---------------------------------------------------------------- 权限
def test_it_works_in_read_only_mode_and_never_asks(tmp_path):
    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "ok"}, mode="read") as st:
            (st.ws / "a.docx").write_bytes(docx_bytes(["只读档也能读"]))
            assert {"doc_parse", "doc_backends"} <= st.hub.studio.names(st.s)
            out, error = await st.tool("doc_parse", path="a.docx")
            assert not error and "只读档也能读" in str(out)
            assert st.of("ask") == []
    run(main)


def test_protected_places_and_blacklisted_paths_are_still_refused(tmp_path):
    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "ok"}) as st:
            secret = P.protected_paths(None)[0] / "token.pdf"            # 桌宠设置目录
            d = st.hub.studio.decision(st.s, "doc_parse", {"path": str(secret)})
            assert d.how == "deny" and "守门拦下" in d.why
            for bad in ("%APPDATA%\\x.pdf", "~/x.pdf", "$env:HOME/x.pdf"):
                assert st.hub.studio.decision(st.s, "doc_parse", {"path": bad}).how == "deny"
            ok = st.hub.studio.decision(st.s, "doc_parse", {"path": "fine.pdf"})
            assert ok.how == "auto"
            assert st.hub.studio.decision(st.s, "doc_backends", {}).how == "auto"
    run(main)


# ---------------------------------------------------------------- 清单和登记
def test_manifest_matches_the_tools_she_really_gets():
    declared = {t["name"] for t in DP.MANIFEST["plugin"]["tools"]}
    assert declared == DP.NAMES == {"doc_parse", "doc_backends"} == {x["function"]["name"] for x in DP.TOOL_SPECS}
    assert DP.READ_ONLY == DP.NAMES and DP.MANIFEST["plugin"]["provides"] == "doc-parse"
    assert {x["function"]["name"] for x in ST.SPECS} >= DP.NAMES and DP.NAMES <= ST.READ_ONLY
    assert DP.LABELS.items() <= ST.LABELS.items()
    backs = {b["id"]: b for b in DP.MANIFEST["plugin"]["backends"]}
    assert backs["mineru-cloud"]["auto"] is False and backs["mineru-cloud"]["needs_consent"] and backs["builtin-text"]["upload"] is False
    assert DP.MANIFEST["plugin"]["permissions"]["upload"] == "never"
    assert all(len(set(x["function"]["parameters"]["required"]) - set(x["function"]["parameters"]["properties"])) == 0 for x in DP.TOOL_SPECS)


def test_a_big_document_stays_under_the_studio_output_cap(tmp_path):
    """studio.execute 把超过 3 万字的工具结果换成节选: 默认和最大的 max_chars 加上 JSON 转义也要放得进去."""
    text = '"引号"\n' * 4000                  # 转义最多的情形: 每行的引号和换行都要多一个反斜杠

    async def main():
        async with Stage(tmp_path, lambda s, n: {"content": "ok"}) as st:
            (st.ws / "big.txt").write_text(text, encoding="utf-8")
            for limit in (None, DP.TOOL_MAX_CHARS):
                args = {"path": "big.txt"} | ({"max_chars": limit} if limit else {})
                out, error = await st.tool("doc_parse", **args)
                body = out if isinstance(out, str) else out.text
                assert not error and "truncated" not in body[:40] and len(body) < 30000, len(body)
    run(main)

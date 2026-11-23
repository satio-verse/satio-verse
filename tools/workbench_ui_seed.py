"""工作台界面测试的数据 (tools/test_workbench_ui.cjs 调): 在给的目录里建一个临时工作区 (不碰真的默认工作区), 起假云端
(tests/test_cloud.FakeCloud), 经测试服务端 /ws 的 channel_config 登记成模型, 开一个会话让她新建一个 md、改两个文件 (一个 CRLF)、
跑一条命令、回一段带链接和表格的 Markdown; 再建一本资料本 (文字 + 小 PDF) 挂到另一个会话, 让她查过以后回一段带出处 [S1-2] 的话;
再来一轮 `python check.py` 报错 → 写 check.py → 再跑成了 (程序整理出一条工作经验); 一轮让她建一个扩展草稿 (一份 SKILL.md, 默认不启用); 一轮让她用 Office 工具写一份周报 (Word + PPT);
最后一轮派两个子任务 (一个要跑命令、停在"等你点头", 一个直接做完). 不花钱.
再来一个说谎的会话 ("把安装包打好": 说写到 dist/setup.exe、其实没有, 核对一次后还对不上 → 待核查, 角标测试要用).
再建一本"阅读面板（验收）"资料本 (三页 PDF 第 2 页空白、第 3 页里藏了一句"指令"; 一份 Word; 一份扫描 PDF) 和一个没挂它的会话 (readsid).
最后一行打印 JSON: {"sid", "workspace", "other", "nbsid", "notebook", "expsid", "extsid", "extension", "officesid", "teamsid", "children", "readnb", "readsid", "checksid", "fake_port"}.
用法: python tools/workbench_ui_seed.py <端口> <目录>"""
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "wm").is_dir() and (p / "tests").is_dir())  # 草稿目录里没有 tests/, 会一路找到项目根
sys.path[:0] = [str(ROOT / "tests"), str(ROOT)]
import base64  # noqa: E402
import re  # noqa: E402

import aiohttp  # noqa: E402
import reading_fixtures as RF  # noqa: E402
from test_cloud import KEY, FakeCloud, call, say  # noqa: E402

FINAL = ("改好了，一共动了三个文件：\n\n1. 新建了 `docs/计划.md`，写了三步计划；\n2. `src/app.py` 的 `main()` 现在打印 hello 并返回 0；\n"
         "3. `src/crlf.txt` 第二行改成了大写，换行保持 CRLF。\n\n跑了一遍 `python src/app.py`，输出 hello。\n\n"
         "参考：https://docs.python.org/3/library/asyncio-task.html 和 https://github.com/ 。\n\n| 文件 | 改动 |\n|---|---|\n| docs/计划.md | 新建 |\n| src/app.py | +2 −1 |\n")


NB_TEXT = ("# 线性代数复习\n\n## 行列式\n\n行列式是方阵对应的一个数。二阶行列式等于主对角线乘积减去副对角线乘积。\n\n"
           "## 矩阵乘法\n\n矩阵乘法不满足交换律，但满足结合律。AB 的第 i 行第 j 列等于 A 的第 i 行和 B 的第 j 列对应相乘再相加。\n")
NB_FINAL = ("按资料本说：矩阵乘法不满足交换律，但满足结合律 [S1-2]。二阶行列式等于主对角线乘积减去副对角线乘积【S1-1】。"
            "英文讲义第 1 页也写了同样的意思 [S1-2、S2-1]。")


TEAM_GOALS = ("核对依赖版本", "整理三份会议纪要")
# 周报 (第 7 块的 Office 工具): Word 第 1 段是大标题, 接着 小标题 / 结论 / 两条圆点 / 一张表 (表里的不算段) / 一段引用
REPORT_BLOCKS = [{"type": "heading", "text": "本周", "level": 1}, {"type": "paragraph", "text": "结论：工作台第 5 块做完了，第 7 块开工。"},
                 {"type": "bullets", "items": ["插件页的开关真改到运行时", "Agent 预设做成卡片"]},
                 {"type": "table", "rows": [["项", "状态"], ["第 5 块", "做完"], ["第 7 块", "在做"]]},
                 {"type": "quote", "text": "下周：Word 预览、按段引用、导出 PDF。"}]
# 扩展草稿: 一份技能说明 (同样的内容每次建出来是同一个版本, 不会越攒越多)
EXT_SPEC = {"id": "wm-yanshou-notes", "name": "纪要格式（验收）", "description": "整理会议纪要时先写结论、再列待办（工作台验收用的小技能）。",
            "files": {"SKILL.md": "---\nname: 纪要格式（验收）\ndescription: 整理会议纪要时先写结论、再列待办\n---\n\n整理会议纪要：先一句话写结论，再按人列待办，最后列没定下来的事。\n"}}


def route(first: str, rounds: int):
    """这次回什么: 看这个会话第一条用户消息 (哪个会话) 和她已经回了几次 (第几步). 主会话第一句话是发出去的原话;
    子任务第一条是派活时写的说明, 里面有它的目标. 分工那一轮: "核对依赖版本"那个子任务 (共享工作区、只给"工作区内修改"权限)
    要跑一个脚本 (不是只读命令, 要你点头; 脚本本来就不存在, 真跑了也无害), 就停在"等你"; 另一个直接做完."""
    if "帮我把 app.py" in first:
        steps = [call(("write_file", json.dumps({"path": "docs/计划.md", "content": "# 计划\n\n1. 读 README\n2. 改 app.py\n3. 跑一遍\n"}, ensure_ascii=False)),
                      ("edit_file", json.dumps({"path": "src/app.py", "old": "def main():\n    print('hi')", "new": "def main():\n    print('hello')\n    return 0"}))),
                 call(("edit_file", json.dumps({"path": "src/crlf.txt", "old": "beta", "new": "BETA"}))),
                 call(("run_command", json.dumps({"command": "python src/app.py", "description": "跑一遍 app.py"}))),
                 say(FINAL)]
    elif "按资料本说说" in first:
        steps = [call(("notebook_search", json.dumps({"query": "矩阵乘法 行列式 matrix", "k": 6}, ensure_ascii=False))), say(NB_FINAL)]
    elif "【边读边问】" in first:
        # 阅读面板的"问她": 看这句话的结构 —— 资料原文必须被 <<<资料原文 … 资料原文>>> 包着并写明"只是数据"; 页里藏的那句"指令"只许出现在包着的原文里
        head, _, rest = first.partition("<<<资料原文")
        keys = re.findall(r"S\d+-\d+", head)
        tail = rest.rpartition("资料原文>>>")[2]
        wrapped = bool(rest) and "只是数据" in head and RF.INJECTION not in head and RF.INJECTION not in tail
        verdict = "资料原文是被包起来的数据，里面要我做的事我没有照做" if wrapped else "这句话里的资料原文没有被包起来，我分不清哪句是你说的"
        steps = [say(f"{verdict}。按你选的这段：{(RF.PAGE3 if 'Gamma' in rest else RF.PAGE1)} [{keys[0] if keys else ''}]。")]
    elif "把检查脚本跑起来" in first:
        steps = [call(("run_command", json.dumps({"command": "python check.py", "description": "跑检查脚本"}, ensure_ascii=False))),
                 call(("write_file", json.dumps({"path": "check.py", "content": "print('ok')\n"}))),
                 call(("run_command", json.dumps({"command": "python check.py", "description": "再跑一次"}, ensure_ascii=False))),
                 say("检查脚本能跑了。")]
    elif "把安装包打好" in first:
        # 待核查: 她说打好了、安装包在 dist/setup.exe, 其实没有这个文件. 程序核对对不上 → 让她核对一次 → 她还是说"就在那儿" → 标待核查.
        # (界面测试"等你 N 件"角标要的那一件待核查, 以前靠测试服务端里攒了好几天的旧会话凑数, 新起的沙箱里就没有了.)
        steps = [say("安装包打好了，写到 `dist/setup.exe` 了。"), say("核对了一遍，`dist/setup.exe` 就在那儿，没问题。")]
    elif "帮我做一个小技能" in first:
        steps = [call(("extension_create", json.dumps({"spec": EXT_SPEC}, ensure_ascii=False))), say("做好了一个草稿，没启用；在插件页里打开就能用。")]
    elif "帮我写一份周报" in first:
        slides = [{"title": "做完了", "bullets": ["第 5 块：插件与设置", "第七轮：成员一栏"], "notes": "先讲结论"}, {"title": "下周", "bullets": ["第 7 块：Office"]}]
        steps = [call(("docx_create", json.dumps({"path": "周报.docx", "title": "周报", "blocks": REPORT_BLOCKS}, ensure_ascii=False)),
                      ("pptx_create", json.dumps({"path": "周报.pptx", "title": "周报", "subtitle": "2026-11-02", "slides": slides}, ensure_ascii=False))),
                 say("周报写好了：周报.docx 和 周报.pptx。")]
    elif "帮我分两路" in first:
        tasks = [{"goal": TEAM_GOALS[0], "workspace_mode": "shared", "mode": "workspace"}, {"goal": TEAM_GOALS[1]}]
        steps = [call(("delegate_tasks", json.dumps({"tasks": tasks}, ensure_ascii=False))), say("两路都派出去了，做完我来汇总。")]
    elif TEAM_GOALS[0] in first:
        steps = [call(("run_command", json.dumps({"command": "python tools/check_deps.py", "description": "核对依赖版本"}, ensure_ascii=False))),
                 say("这部分做完了。")]
    else:
        steps = [say("这部分做完了。")]
    return steps[min(rounds, len(steps) - 1)]


async def reply(request, body):
    msgs = body.get("messages", [])
    users = [str(m.get("content") or "") for m in msgs if m.get("role") == "user"]
    if users and "【边读边问】" in users[-1]:  # 阅读面板: 同一个会话里一问再问, 每次看最新那句
        return await route(users[-1], 0)(request, body)
    return await route(users[0] if users else "", sum(m.get("role") == "assistant" for m in msgs))(request, body)


async def run_turn(http, port, headers, sid, text):
    """发一句话, 等这一轮结束 (中间要点头的一律"这次允许")."""
    async with http.ws_connect(f"http://127.0.0.1:{port}/agent/ws?id={sid}", headers=headers) as conn:
        await conn.send_json({"type": "send", "text": text, "context_ids": []})
        while True:
            ev = await asyncio.wait_for(conn.receive_json(), 60)
            if ev.get("type") == "ask":
                await conn.send_json({"type": "answer", "id": ev["id"], "how": "once"})
            if ev.get("type") == "done":
                break


async def tidy_team(http, port, api, headers):
    """上次留下、没人停的分工 (主会话一直等"等你点头"的子任务): 停掉主会话、取消那个子任务, 免得占着并行名额 (新派的只能排队)."""
    items = (await (await http.get(f"{api}/runtime", params={"view": "activity"}, headers=headers)).json()).get("items", [])
    for item in items:
        if item.get("kind") == "child" and item.get("title") in TEAM_GOALS and item.get("parent"):
            async with http.ws_connect(f"http://127.0.0.1:{port}/agent/ws?id={item['parent']}", headers=headers) as conn:
                await conn.send_json({"type": "stop"})
            await http.post(f"{api}/runtime", json={"action": "cancel", "id": item["parent"], "child_id": item["id"]}, headers=headers)


def tiny_pdf(text: str) -> bytes:
    """一页的 PDF, 一行英文 (Helvetica); 资料本按 pypdf 读出来是第 1 页."""
    stream = f"BT /F1 14 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def seed(ws: Path) -> None:
    if ws.exists():
        shutil.rmtree(ws)
    (ws / "src").mkdir(parents=True)
    (ws / "docs").mkdir()
    (ws / "README.md").write_text("# 示例项目\n\n这是工作台改版验收用的临时工作区。\n\n## 怎么用\n\n- 第一步：读说明\n- 第二步：跑 `python src/app.py`\n- [ ] 第三步：写测试\n\n"
                                  "```python\nprint('hi')\n```\n\n参考 [asyncio 文档](https://docs.python.org/3/library/asyncio-task.html)。\n", encoding="utf-8")
    (ws / "src" / "app.py").write_text("def main():\n    print('hi')\n\n\nif __name__ == '__main__':\n    main()\n", encoding="utf-8")
    (ws / "src" / "crlf.txt").write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    (ws / "docs" / "old.txt").write_text("旧的说明\n", encoding="utf-8")


async def main(port: int, out: Path) -> None:
    ws = out / "ws"
    seed(ws)
    fake = FakeCloud(reply)
    base = await fake.start()
    headers = {"Origin": f"http://127.0.0.1:{port}"}
    api = f"http://127.0.0.1:{port}/api/agent"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(f"http://127.0.0.1:{port}/ws") as ws_conn:
            await ws_conn.send_json({"type": "channel_config", "model": {"provider": "custom", "base_url": base, "api_key": KEY, "model": "fake-chat"}})
            while (await ws_conn.receive_json()).get("type") != "channel_config_done":
                pass
        await http.post(api, json={"action": "workspace", "path": str(ws), "name": "验收工作区"}, headers=headers)
        other = (await (await http.post(api, json={"action": "new", "workspace": str(ws), "mode": "full"}, headers=headers)).json())["id"]
        sid = (await (await http.post(api, json={"action": "new", "workspace": str(ws), "mode": "full"}, headers=headers)).json())["id"]
        async with http.ws_connect(f"http://127.0.0.1:{port}/agent/ws?id={sid}", headers=headers) as conn:
            await conn.send_json({"type": "send", "text": "帮我把 app.py 改成打印 hello，再写个计划，顺手把 crlf.txt 第二行改大写，跑一下", "context_ids": []})
            while True:
                ev = await asyncio.wait_for(conn.receive_json(), 60)
                if ev.get("type") == "ask":
                    await conn.send_json({"type": "answer", "id": ev["id"], "how": "once"})
                if ev.get("type") == "done":
                    break
        # 资料本: 一段文字 (S1) + 一份小 PDF (S2), 挂到一个新会话, 让她查过以后带出处回答
        nb_api = f"http://127.0.0.1:{port}/api/agent/notebooks"
        book = (await (await http.post(nb_api, json={"action": "create", "title": "线代复习（验收）"}, headers=headers)).json())["notebook"]
        await http.post(nb_api, json={"action": "add", "id": book["id"], "title": "线代笔记", "text": NB_TEXT}, headers=headers)
        pdf = tiny_pdf("Matrix multiplication is not commutative, but it is associative.")
        await http.post(nb_api, json={"action": "add", "id": book["id"], "name": "linear-algebra-notes.pdf", "data": base64.b64encode(pdf).decode()}, headers=headers)
        nbsid = (await (await http.post(api, json={"action": "new", "workspace": str(ws), "mode": "full"}, headers=headers)).json())["id"]
        await http.post(nb_api, json={"action": "attach", "id": book["id"], "session_id": nbsid}, headers=headers)
        async with http.ws_connect(f"http://127.0.0.1:{port}/agent/ws?id={nbsid}", headers=headers) as conn:
            await conn.send_json({"type": "send", "text": "按资料本说说矩阵乘法和行列式", "context_ids": []})
            while True:
                ev = await asyncio.wait_for(conn.receive_json(), 60)
                if ev.get("type") == "ask":
                    await conn.send_json({"type": "answer", "id": ev["id"], "how": "once"})
                if ev.get("type") == "done":
                    break
        # 阅读面板: 三页 PDF (S1) + Word (S2) + 扫描 PDF (S3, 读不了), 一个还没挂它的会话
        rb = (await (await http.post(nb_api, json={"action": "create", "title": "阅读面板（验收）"}, headers=headers)).json())["notebook"]
        for name, data in (("three-pages.pdf", RF.three_page_pdf(injection_on_page=3)), ("notes.docx", RF.notes_docx()), ("scanned.pdf", RF.scanned_pdf())):
            await http.post(nb_api, json={"action": "add", "id": rb["id"], "name": name, "data": base64.b64encode(data).decode()}, headers=headers)
        readsid = (await (await http.post(api, json={"action": "new", "workspace": str(ws), "mode": "full"}, headers=headers)).json())["id"]
        # 工作经验和分工这两轮放在另一个临时工作区 (原来那个只留三个会话: 左栏每个项目先列 5 个, 子任务也算, 多了要点"再显示")
        ws2 = out / "ws2"
        if ws2.exists():
            shutil.rmtree(ws2)
        ws2.mkdir(parents=True)
        await http.post(api, json={"action": "workspace", "path": str(ws2), "name": "验收工作区 · 分工"}, headers=headers)
        # 待核查: 一个说打好了安装包、其实文件不在的会话 (统一状态里是一件"等你"的待核查; 角标测试要用; 放 ws2: 原来那个工作区左栏先列 5 个、再多就把测试要点的会话挤进"再显示")
        checksid = (await (await http.post(api, json={"action": "new", "workspace": str(ws2), "mode": "full"}, headers=headers)).json())["id"]
        await run_turn(http, port, headers, checksid, "把安装包打好")
        # 扩展草稿: 她用 extension_create 建 (不启用; 启停是你在插件页上做的)
        extsid = (await (await http.post(api, json={"action": "new", "workspace": str(ws2), "mode": "full"}, headers=headers)).json())["id"]
        await run_turn(http, port, headers, extsid, "帮我做一个小技能：整理会议纪要的格式")
        # 周报: 她用 docx_create / pptx_create 写 (第 7 块)
        officesid = (await (await http.post(api, json={"action": "new", "workspace": str(ws2), "mode": "full"}, headers=headers)).json())["id"]
        await run_turn(http, port, headers, officesid, "帮我写一份周报，Word 和 PPT 各一份")
        # 工作经验: 报错 → 换做法 → 成了, 这一轮结束后程序整理成一条 (不调模型)
        expsid = (await (await http.post(api, json={"action": "new", "workspace": str(ws2), "mode": "full"}, headers=headers)).json())["id"]
        await run_turn(http, port, headers, expsid, "把检查脚本跑起来")
        # 分工: 派两个子任务, 等到一个停在"等你点头"、一个做完就走 —— 主会话这一轮会一直"等子任务做完再汇总" (不再问模型,
        # 假模型跟着这个进程退出也不要紧). 用完的人负责停掉这个会话 (测试和截图脚本最后都停).
        await tidy_team(http, port, api, headers)
        teamsid = (await (await http.post(api, json={"action": "new", "workspace": str(ws2), "mode": "full"}, headers=headers)).json())["id"]
        children = []
        async with http.ws_connect(f"http://127.0.0.1:{port}/agent/ws?id={teamsid}", headers=headers) as conn:
            await conn.send_json({"type": "send", "text": "帮我分两路：一路核对依赖版本，一路整理三份会议纪要", "context_ids": []})
            for _ in range(300):
                tree = (await (await http.get(f"{api}/runtime", params={"view": "tree", "id": teamsid}, headers=headers)).json()).get("tree", [])
                children = [{"id": c["id"], "title": c.get("title") or c.get("goal"), "status": c.get("status")} for c in tree]
                if sorted(c["status"] for c in children) == ["completed", "waiting"]:
                    break
                await asyncio.sleep(0.2)
    print(json.dumps({"sid": sid, "other": other, "workspace": str(ws), "nbsid": nbsid, "notebook": book["id"],
                      "expsid": expsid, "extsid": extsid, "extension": EXT_SPEC["id"], "officesid": officesid, "teamsid": teamsid, "children": children, "readnb": rb["id"], "readsid": readsid, "checksid": checksid, "fake_port": int(base.rsplit(":", 1)[1].split("/")[0])}), flush=True)
    await fake.close()  # ASCII 转义: 路径里有中文时控制台编码不会弄乱


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), Path(sys.argv[2]).resolve()))

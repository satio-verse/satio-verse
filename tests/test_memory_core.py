"""记忆核心 (记忆按脑区重设计阶段 1, 影子): 把各处的记忆读成一张关系网存进 SQLite; 只读记忆文件, 不影响检索.
- 参与检索的每一块都有一个节点, 和 Memory._refresh 切出来的一一对应; 来源、改过 / 忘掉和记忆页的说法一样;
- 实体、来自那天的对话、更正为、命令 -> 经验、东西 -> 以为在 / 找到过的地方这些边;
- 重建两次第二次什么都不变; 改一行只换那一条, 变更记录里看得到; 行号挪了不算改;
- 建索引、迁移演练都不写记忆文件 (连你在 Obsidian 里勾了的候选也不替你转正);
- /api/memory/graph: 没开时说没开, 开了给图, 外站读不到."""
import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from test_pipeline import make_cfg
from wm import memory_core as MC
from wm import memory_migrate as MM
from wm.app import create_app
from wm.cognition import cup as U
from wm.cognition.belief import Cognition
from wm.cognition.roomworld import RoomWorld
from wm.memory import Memory


def seeded(tmp_path: Path) -> tuple[Memory, Path, Path]:
    vault = tmp_path / "vault"
    m = Memory({"vault": str(vault), "character": "绫音"})
    now = {"t": datetime(2026, 10, 25, 14, 0)}
    m.clock = lambda: now["t"]
    m.init()
    m.start_session()
    m.observe("我叫小明，我最喜欢吃草莓", "记住啦！", [], "happy")
    m.observe("我养了一只橘猫", "好可爱", [], None)
    m.observe("我不喜欢吃草莓了", "好哦", [], None)  # 改口: 旧的那行划掉, 注明更正为
    m.add_summary("2026-10-25", [{"text": "用户的猫叫豆豆", "times": ["14:00"]},
                                 {"text": "用户10月28日要去医院复查", "times": ["14:01"]}], 0)
    m.propose("2026-10-25", [{"text": "用户下周要去北京出差", "times": ["14:02"]}], 0)
    m.add_understanding("被在乎的感觉很重要", source="看视频", day="2026-10-25")
    m.add_howto("打开记事本", "用 notepad 命令", ok=True, day="2026-10-25")
    m.add_howto("截图", "PrintScreen 没反应", ok=False, day="2026-10-25")
    m.save_doc("攻略.md", "md", "张三喜欢吃榴莲")
    m.write_diary(["14:30 看了一会儿书"])
    (vault / "旅行.md").write_text("# 旅行\n\n明年想去京都看樱花\n", encoding="utf-8")  # 你自己写的笔记
    m.recall("医院复查的事")  # 带进提示词的事件记一笔"上次想起"
    home = tmp_path / "agent"
    home.mkdir()
    (home / "experiences.json").write_text(json.dumps([
        {"id": "exp-1", "head": "npm test", "sig": "Cannot find module <x>", "action": "先 npm install", "state": "supported",
         "trust": 3, "support": 2, "contradict": 0, "pushed": 1, "adopted": 1, "hits": 1, "enabled": True}], ensure_ascii=False), encoding="utf-8")
    cog_dir = tmp_path / "experience"
    world, cog = RoomWorld(U.START, here="coffee"), Cognition()
    cog.perceive(world.observe())
    cog.log.save(cog_dir / "room-events.jsonl")
    (cog_dir / "afford-events.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"op": "saw", "id": "bookshelf", "t": 100.0},
        {"op": "tried", "act": "read", "ctx": "evening", "ok": True, "felt": {"fun": 1}, "t": 110.0},
        {"op": "liked", "node": "book_1", "fun": 1.5, "t": 111.0}]) + "\n", encoding="utf-8")
    return m, home, cog_dir


def digest(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.rglob("*")) if p.is_file()}


def collect(m: Memory, home: Path, cog_dir: Path) -> dict:
    return MM.collect({"vault": str(m.root), "character": "绫音"}, home, cog_dir)


def test_every_searchable_chunk_is_a_node_with_honest_origin(tmp_path):
    m, home, cog_dir = seeded(tmp_path)
    r = collect(m, home, cog_dir)
    m._refresh()
    assert r["chunks"] == len(m._chunks) == r["sources"]["检索的块"]  # 和检索时切出来的一样多
    by_text = {n["text"]: n for n in r["nodes"].values()}
    assert by_text["用户叫小明"]["type"] == "fact" and by_text["用户叫小明"]["origin"] == "told" and by_text["用户叫小明"]["day"] == "2026-10-25"
    assert by_text["用户10月28日要去医院复查"]["type"] == "event" and by_text["用户10月28日要去医院复查"]["origin"] == "digest"
    assert by_text["用户10月28日要去医院复查"]["data"]["recall"]["last"] == "2026-10-25"  # .recall.json 并进来了
    assert by_text["旅行: 明年想去京都看樱花"]["origin"] == "user" and by_text["旅行: 明年想去京都看樱花"]["type"] == "note"  # 带标题, 和检索时一样
    assert any(n["type"] == "doc" and n["origin"] == "external" and "榴莲" in n["text"] for n in r["nodes"].values())
    old = by_text["用户喜欢吃草莓"]
    assert old["status"] == "corrected" and "更正为" in old["data"]["note"]
    assert by_text["用户下周要去北京出差"]["type"] == "candidate" and by_text["用户下周要去北京出差"]["origin"] == "inferred"
    assert by_text["被在乎的感觉很重要"]["type"] == "self"
    howto = [n for n in r["nodes"].values() if n["type"] == "procedure" and n["file"] and n["file"].endswith("操作笔记.md")]
    assert sorted(n["data"]["ok"] for n in howto) == [False, True]
    assert any(n["type"] == "day" and n["day"] == "2026-10-25" for n in r["nodes"].values())
    assert any(n["type"] == "diary" for n in r["nodes"].values())
    assert any(n["type"] == "relation" and "小明" in n["text"] for n in r["nodes"].values())
    assert any(n["type"] == "procedure" and n["text"].startswith("npm test") for n in r["nodes"].values())  # 工作经验
    assert any(n["type"] == "belief" for n in r["nodes"].values())  # 她以为东西在哪 (回放小窝的观察)
    assert any(n["type"] == "procedure" and "试过 1 次" in n["text"] for n in r["nodes"].values())  # 在小窝里做过的事
    assert set(r["origins"]) <= set(MM.ORIGINS) and set(r["types"]) <= set(MM.TYPES)


def test_edges_connect_memories_entities_days_and_corrections(tmp_path):
    m, home, cog_dir = seeded(tmp_path)
    r = collect(m, home, cog_dir)
    nodes = r["nodes"]
    kinds = {(nodes[e["src"]]["text"], e["kind"], nodes[e["dst"]]["type"]) for e in r["edges"]}
    assert ("用户的猫叫豆豆", "mentions", "entity") in kinds
    assert ("用户养了一只橘猫", "mentions", "entity") in kinds
    cat = [n for n in nodes.values() if n["type"] == "entity" and "豆豆" in n["data"]["aliases"]]
    assert cat and {"橘猫", "猫"} & set(cat[0]["data"]["aliases"])  # 豆豆 = 猫, 和检索时并组一样
    assert ("用户的猫叫豆豆", "from_day", "day") in kinds and ("用户叫小明", "said_on", "day") in kinds
    assert ("用户喜欢吃草莓", "corrected_to", "fact") in kinds
    assert any(e["kind"] == "triggers" for e in r["edges"]) and any(e["kind"] == "thinks_at" for e in r["edges"])
    assert any(e["kind"] == "same_day" for e in r["edges"])


def test_rebuild_is_idempotent_and_one_edit_changes_one_node(tmp_path):
    m, home, cog_dir = seeded(tmp_path)
    core = MC.MemoryCore(tmp_path / "core.db")
    first = core.build(collect(m, home, cog_dir))
    assert first["added"] == first["nodes"] > 20 and first["removed"] == first["changed"] == 0
    n_changes = core.stats()["changes"]
    again = core.build(collect(m, home, cog_dir))
    assert again["added"] == again["removed"] == again["changed"] == again["edges_added"] == again["edges_removed"] == 0
    assert core.stats()["changes"] == n_changes  # 第二次什么都没记
    facts = m.dir / "记忆" / "关于用户.md"
    facts.write_text(facts.read_text(encoding="utf-8").replace("用户养了一只橘猫", "用户养了一只三花猫"), encoding="utf-8")
    third = core.build(collect(m, home, cog_dir))
    assert (third["added"], third["removed"]) == (1, 1)
    whats = [c["what"] for c in core.changes(5)]
    assert any("三花猫" in w and w.startswith("新加") for w in whats) and any("橘猫" in w and w.startswith("没了") for w in whats)
    summary = m.dir / "记忆" / "对话摘要.md"  # 上面插一行: 下面的行号都变了, 不算改
    text = summary.read_text(encoding="utf-8")
    summary.write_text(text.replace("## 2026-10-25\n", "## 2026-10-25\n\n"), encoding="utf-8")
    fourth = core.build(collect(m, home, cog_dir))
    assert fourth["added"] == fourth["removed"] == fourth["changed"] == 0
    g = core.graph(types=["fact", "event"])
    assert g["nodes"] and all(n["type"] in ("fact", "event") for n in g["nodes"])
    assert all(e["src"] in {n["id"] for n in g["nodes"]} and e["dst"] in {n["id"] for n in g["nodes"]} for e in g["edges"])
    core.close()


def test_cache_gives_the_same_result_and_sees_edits(tmp_path):
    m, home, cog_dir = seeded(tmp_path)
    cfg = {"vault": str(m.root), "character": "绫音"}
    cache = {}
    plain, cached = MM.collect(cfg, home, cog_dir), MM.collect(cfg, home, cog_dir, cache)
    again = MM.collect(cfg, home, cog_dir, cache)  # 这次全走缓存
    assert plain["nodes"] == cached["nodes"] == again["nodes"] and plain["edges"] == again["edges"]
    facts = m.dir / "记忆" / "关于用户.md"
    facts.write_text(facts.read_text(encoding="utf-8").replace("用户养了一只橘猫", "用户养了一只三花猫"), encoding="utf-8")
    edited = MM.collect(cfg, home, cog_dir, cache)
    texts = {n["text"] for n in edited["nodes"].values()}
    assert "用户养了一只三花猫" in texts and "用户养了一只橘猫" not in texts
    (m.root / "旅行.md").unlink()
    assert not any("京都" in n["text"] for n in MM.collect(cfg, home, cog_dir, cache)["nodes"].values())
    assert str(m.root / "旅行.md") not in cache


def test_building_never_writes_her_memory_files(tmp_path):
    m, home, cog_dir = seeded(tmp_path)
    cand = m.dir / "记忆" / "候选.md"
    cand.write_text(cand.read_text(encoding="utf-8").replace("- [ ]", "- [x]"), encoding="utf-8")  # 你在 Obsidian 里勾上了
    before = {**digest(m.root), **{"agent/" + k: v for k, v in digest(home).items()}, **{"exp/" + k: v for k, v in digest(cog_dir).items()}}
    r = collect(m, home, cog_dir)
    core = MC.MemoryCore(tmp_path / "core.db")
    core.build(r)
    core.close()
    after = {**digest(m.root), **{"agent/" + k: v for k, v in digest(home).items()}, **{"exp/" + k: v for k, v in digest(cog_dir).items()}}
    assert before == after  # 一个字节都没动 (勾了的候选也没替你转正; 小窝的经历没写回)
    assert any(n["status"] == "ticked" for n in r["nodes"].values())
    assert "记忆核心迁移演练" in MM.report(r) and not (tmp_path / "vault" / "core.db").exists()


def test_dry_run_cli_reports_without_creating_a_db(tmp_path, capsys):
    m, home, cog_dir = seeded(tmp_path)
    over = [f"memory.vault={m.root.as_posix()}", "memory.character=绫音", f"agent.home={home.as_posix()}",
            f"room.cognition.data_dir={cog_dir.as_posix()}", f"memory.core_path={(tmp_path / 'should-not-exist.db').as_posix()}"]
    args = ["migrate", "--dry-run"] + sum((["--set", o] for o in over), [])
    assert MC.main(args) == 0
    out = capsys.readouterr().out
    assert "会建出" in out and "对不上的地方" in out
    assert not (tmp_path / "should-not-exist.db").exists()
    assert MC.main(["migrate"] + sum((["--set", o] for o in over), [])) == 2  # 阶段 1 只有演练


def test_core_path_follows_test_log_dir(tmp_path):
    assert MC.core_path({"trace": {"log_dir": str(tmp_path)}, "memory": {}}) == tmp_path / "memory" / "core.db"
    assert MC.core_path({"memory": {"core_path": str(tmp_path / "x.db")}}) == tmp_path / "x.db"
    assert MC.mode({"memory": {"core": "shadow"}}) == "shadow" and MC.mode({"memory": {}}) == "off" and MC.mode({"memory": {"core": "on?"}}) == "off"


def test_graph_api_off_by_default_shadow_when_set_and_local_only(tmp_path):
    m, home, cog_dir = seeded(tmp_path)

    async def go(core_mode):
        cfg = make_cfg(tmp_path)
        cfg["memory"] = {**cfg.get("memory", {}), "vault": str(m.root), "character": "绫音", "core": core_mode,
                         "core_path": str(tmp_path / f"core-{core_mode}.db")}
        cfg["agent"] = {**cfg.get("agent", {}), "home": str(home)}
        cfg.setdefault("room", {}).setdefault("cognition", {})["data_dir"] = str(cog_dir)
        client = TestClient(TestServer(create_app(cfg)))
        await client.start_server()
        try:
            r = await client.get("/api/memory/graph")
            data = await r.json()
            bad = await client.get("/api/memory/graph", headers={"Origin": "https://evil.example"})
            return r.status, data, bad.status
        finally:
            await client.close()

    status, data, bad = asyncio.run(go("off"))
    assert status == 200 and data["enabled"] is False and "memory.core" in data["reason"] and bad == 403
    assert not (tmp_path / "core-off.db").exists()
    status, data, bad = asyncio.run(go("shadow"))
    assert status == 200 and data["enabled"] is True and data["mode"] == "shadow" and bad == 403
    assert data["nodes"] and data["edges"] and data["stats"]["nodes"] >= len(data["nodes"])
    assert {n["type"] for n in data["nodes"]} >= {"fact", "event", "entity"} and data["types"]["fact"]
    assert data["build"]["added"] == data["stats"]["nodes"]

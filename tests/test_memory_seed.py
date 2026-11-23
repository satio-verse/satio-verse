"""出生种子 (记忆按脑区重设计阶段 3, wm/memory_seed.py; 2026-11-02 用户"按草稿做"): 种子守规矩 (不放经历、关于主人的事、
小窝里东西在哪); 记忆页、记忆核心看得到、来源都是出厂; 早上第一次见面提一句; 新用户装好就有记忆; 种子和世界无关 (双胞胎)."""
import asyncio
import json
from datetime import datetime
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from test_pipeline import make_cfg
from wm import memory_migrate as MM
from wm import memory_seed as MS
from wm.app import create_app
from wm.config import load_config
from wm.memory import Memory


def mem(root: Path, **cfg) -> tuple[Memory, dict]:
    m = Memory({"vault": str(root), "character": "绫音", "graph_notes": False, **cfg})
    now = {"t": datetime(2026, 11, 2, 21, 0)}
    m.clock = lambda: now["t"]
    m.init()
    m.start_session()
    return m, now


def test_seed_pack_follows_the_rules():
    pack = MS.load("绫音")
    seeds = pack["seeds"]
    assert MS.check(pack) == [] and len(seeds) == 32
    kinds = [s["kind"] for s in seeds]
    assert [kinds.count(k) for k in MS.KINDS] == [7, 2, 5, 8, 10]
    assert all(s.get("from") and s.get("now") in MS.NOW for s in seeds)
    raw = {s["id"]: s for s in seeds}
    assert "values" not in raw["seed:affect:4"] and raw["seed:affect:4"]["read"] == "room_needs_init"  # 值从页面读, 不另存
    bad = {"seeds": [{"id": "seed:self:1", "kind": "自我", "type": "self", "now": "新", "text": "我小时候住在海边"},
                     {"id": "seed:self:1", "kind": "自我", "type": "self", "now": "新", "text": "主人叫小明"},
                     {"id": "seed:habit:9", "kind": "习惯", "type": "procedure", "now": "新", "text": "书放在书架第二层"},
                     {"id": "seed:x:1", "kind": "别的", "type": "belief", "now": "新", "text": "随便"}]}
    problems = " ".join(MS.check(bad))
    assert "经历" in problems and "重复" in problems and "关于主人的事" in problems and "小窝里东西在哪" in problems
    assert "type / kind / now" in problems


def test_room_needs_are_read_from_the_page():
    needs = MS.room_needs()
    assert needs["init"] == {"energy": 80, "food": 75, "water": 70, "fun": 65, "social": 60}
    assert set(needs["decay_per_h"]) == set(needs["init"]) and needs["restore_per_h"] > 0
    v = {s["id"]: s for s in MS.view("绫音")}
    assert v["seed:affect:4"]["values"]["init"] == needs["init"]
    assert v["seed:affect:1"]["values"] == {"valence": 0.2, "arousal": 0.4}
    assert v["seed:habit:1"]["values"] == {"sleep": "00:00", "wake": "07:00"}


def test_view_status_off_and_new_user_only():
    assert {s["status"] for s in MS.view("绫音")} == {"active"}
    v = {s["id"]: s["status"] for s in MS.view("绫音", off=["seed:habit:2"], met=True)}
    assert v["seed:habit:2"] == "off" and v["seed:self:3"] == "not_needed"
    assert sum(x == "active" for x in v.values()) == 30
    assert MS.view("绫音", mode="off") == [] and MS.view("别的角色") == []


def test_morning_hello_only_first_time_today_in_the_morning(tmp_path):
    m, now = mem(tmp_path / "a")
    m.observe("我叫小明", "嗯", [], None)
    seen = []
    for t in (datetime(2026, 11, 3, 8, 0), datetime(2026, 11, 3, 9, 30), datetime(2026, 11, 4, 14, 0),
              datetime(2026, 11, 5, 10, 59), datetime(2026, 11, 6, 4, 59)):
        now["t"] = t
        m.start_session()
        seen.append("今天头一回见到用户" in (m.relation_line() or ""))
    assert seen == [True, False, False, True, False]
    m2, now2 = mem(tmp_path / "b", seed_off=["seed:habit:2"])
    m2.observe("我叫小明", "嗯", [], None)
    now2["t"] = datetime(2026, 11, 3, 8, 0)
    m2.start_session()
    assert "头一回" not in m2.relation_line()  # 你停用了这条种子
    m3, now3 = mem(tmp_path / "c", seed="off")
    m3.observe("我叫小明", "嗯", [], None)
    now3["t"] = datetime(2026, 11, 3, 8, 0)
    m3.start_session()
    assert "头一回" not in m3.relation_line() and m3.overview()["seeds"] == []


def test_core_has_seed_nodes_from_the_factory(tmp_path):
    m, _ = mem(tmp_path / "vault")
    m.observe("我叫小明", "嗯", [], None)
    r = MM.collect({"vault": str(m.root), "character": "绫音", "graph_notes": False})
    seeds = [n for n in r["nodes"].values() if n["origin"] == "seed" and n["id"].startswith("sd:")]
    assert len(seeds) == 32 and {n["type"] for n in seeds} == {"self", "trait", "affect", "procedure"}
    assert next(n for n in seeds if n["data"]["id"] == "seed:self:3")["status"] == "not_needed"  # 已经说过话了
    in_pack = [e for e in r["edges"] if e["kind"] == "in_pack" and e["src"].startswith("sd:")]
    triggers = [e for e in r["edges"] if e["kind"] == "triggers" and e["dst"].startswith("sd:")]
    assert len(in_pack) == 32 and len(triggers) == sum(bool(s.get("trigger")) for s in MS.load("绫音")["seeds"])
    assert r["sources"]["出生种子"] == 32 and not r["problems"]


def test_twin_seeds_do_not_depend_on_the_world(tmp_path):
    """种子一样、世界不一样: 两个库里说过的话、她见过的东西都不同, 种子节点逐个一样 (种子只看人设和代码里的常量)."""
    out = []
    for name, said in (("a", "我住在杭州，我养了一只猫"), ("b", "我住在成都，我不喜欢猫")):
        m, _ = mem(tmp_path / name)
        m.observe(said, "嗯", [], None)
        r = MM.collect({"vault": str(m.root), "character": "绫音", "graph_notes": False})
        out.append({k: v for k, v in r["nodes"].items() if v["origin"] == "seed"})
    assert out[0] == out[1] and len(out[0]) == 33  # 32 条 + 种子包


def test_new_user_gets_a_working_memory_with_seeds(tmp_path, monkeypatch):
    """安装包给新用户的配置 (installer/local.*.json) 里记忆库是 "~/.worldmodel/memory/vault": 空数据目录起服务端,
    记忆开着、记忆页能用、种子都在、来源都是出厂; 你自己的配置里写空串照旧是不开."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    shipped = json.loads((Path(__file__).resolve().parent.parent / "installer" / "local.cpu.json").read_text(encoding="utf-8"))
    assert shipped["memory"]["vault"] == "~/.worldmodel/memory/vault"
    cfg = make_cfg(tmp_path, f"memory.vault={json.dumps(shipped['memory']['vault'])}")
    assert Path(cfg["memory"]["vault"]) == tmp_path / "home" / ".worldmodel" / "memory" / "vault"
    assert load_config(overrides=['memory.vault=""'])["memory"]["vault"] == ""

    async def go():
        client = TestClient(TestServer(create_app(cfg)))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            hello = json.loads((await ws.receive()).data)
            r = await client.get("/api/memory")
            return hello, await r.json()
        finally:
            await client.close()

    hello, page = asyncio.run(go())
    assert hello["status"]["memory"]["enabled"]
    assert (tmp_path / "home" / ".worldmodel" / "memory" / "vault" / "绫音" / "记忆" / "关于用户.md").exists()
    seeds = page["seeds"]
    assert len(seeds) == 32 and all(s["pack"] == "seed/绫音/v1" for s in seeds)
    assert next(s for s in seeds if s["id"] == "seed:self:3")["status"] == "active"  # 新用户: 还不认识主人

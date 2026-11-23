"""她的经历存在哪 (wm/cognition/store.py): 用户数据区 (~/.worldmodel/experience), 卸载不删; 测试、自检改了日志目录的跟着它走;
旧位置 (<log_dir>/cognition/) 的搬过去; 导出、删除."""
import json
import zipfile
from pathlib import Path

from wm.cognition import store


def cfg(log_dir="logs", data_dir=""):
    return {"trace": {"log_dir": log_dir}, "room": {"cognition": {"data_dir": data_dir}}}


def test_where_it_lives(tmp_path):
    assert store.data_dir(cfg()) == Path("~/.worldmodel/experience").expanduser()  # 平常: 用户数据区
    assert store.data_dir(cfg(log_dir=str(tmp_path))) == tmp_path / "cognition"  # 测试、自检: 跟着自己的日志目录
    assert store.data_dir(cfg(log_dir=str(tmp_path), data_dir=str(tmp_path / "mine"))) == tmp_path / "mine"  # 设了就用设的
    assert store.legacy_dir(cfg()).name == "cognition" and store.legacy_dir(cfg()).parent.name == "logs"


def test_migrate_export_delete(tmp_path):
    old, new = tmp_path / "logs", tmp_path / "home"
    (old / "cognition").mkdir(parents=True)
    (old / "cognition" / "room-events.jsonl").write_text('{"seq": 0}\n{"seq": 1}\n', encoding="utf-8")
    c = cfg(log_dir=str(old), data_dir=str(new))
    assert store.migrate(c) == ["room-events.jsonl"]
    assert (new / "room-events.jsonl").read_text(encoding="utf-8") == '{"seq": 0}\n{"seq": 1}\n'
    assert not (old / "cognition" / "room-events.jsonl").exists() and (old / "cognition" / "room-events.jsonl.migrated").exists()
    assert store.migrate(c) == []  # 搬过了
    (old / "cognition" / "room-events.jsonl").write_text('{"seq": 9}\n', encoding="utf-8")
    assert store.migrate(c) == [] and (new / "room-events.jsonl").read_text(encoding="utf-8").count("seq") == 2  # 两边都有: 不动
    assert store.status(new)["files"]["room-events.jsonl"]["lines"] == 2
    out = store.export(new, tmp_path / "out" / "她的经历.zip")
    with zipfile.ZipFile(out) as z:
        assert sorted(z.namelist()) == ["room-events.jsonl", "status.json"]
        assert json.loads(z.read("status.json"))["files"]["room-events.jsonl"]["lines"] == 2
    assert store.delete(new) == ["room-events.jsonl"] and store.status(new)["files"] == {}


def test_cli_delete_needs_yes(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / "carry-events.jsonl").write_text("{}\n", encoding="utf-8")
    sets = ["--set", f"room.cognition.data_dir={json.dumps(str(home))}"]
    assert store.main(["delete", *sets]) == 1 and (home / "carry-events.jsonl").exists()
    assert store.main(["delete", "--yes", *sets]) == 0 and not (home / "carry-events.jsonl").exists()
    assert store.main(["where", *sets]) == 0 and str(home) in capsys.readouterr().out


def test_api_status_export_delete(tmp_path):
    """/api/experience: 本机页面看得到存在哪、导出 zip; 删除要确认, 别的网页不行; 删了以后服务端里记着的也清掉."""
    import asyncio
    import io

    from aiohttp.test_utils import TestClient, TestServer
    from test_pipeline import make_cfg
    from wm.app import ENGINES, create_app

    async def main():
        app = create_app(make_cfg(tmp_path))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            room = app[ENGINES].room
            room.carry_plan({"cup": "IR_Item_Espresso_Pink", "fill": "full", "hot": True, "route": "none", "wait_s": 480})
            room.carry_done({"delivered": True})
            st = await (await client.get("/api/experience")).json()
            z = await (await client.get("/api/experience?export=1")).read()
            evil = await client.post("/api/experience", json={"action": "delete", "confirm": True}, headers={"Origin": "https://evil.example"})
            vague = await client.post("/api/experience", json={"action": "delete"})
            gone = await (await client.post("/api/experience", json={"action": "delete", "confirm": True})).json()
            return st, z, evil.status, vague.status, gone, room.status()
        finally:
            await client.close()

    st, z, evil, vague, gone, live = asyncio.run(main())
    assert st["dir"] == str(tmp_path / "cognition") and st["files"]["carry-events.jsonl"]["lines"] == 1 and st["live"]["carry"] == 1
    with zipfile.ZipFile(io.BytesIO(z)) as f:
        assert "carry-events.jsonl" in f.namelist()
    assert (evil, vague) == (403, 400)
    assert gone["deleted"] == ["carry-events.jsonl"] and gone["files"] == {} and live["carry"] == 0


def test_full_reset_clears_learning_and_restart_does_not_bring_it_back(tmp_path):
    """重置小窝学习 (评审第 6 节): 三个事件文件、运行中的认知、旧位置和 .migrated 副本、来源标着"小窝"的「我的理解」、
    动作成长的参数都清; 代次加一. 之后: 旧位置又有同名文件也不再搬进来; 页面带着旧代次发来的不收; 重启以后还是空的;
    别的来源的「我的理解」不碰; 回包写清楚清了什么、没清什么."""
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer
    from test_pipeline import make_cfg
    from wm.app import ENGINES, create_app

    vault, home = tmp_path / "vault", tmp_path / "home"
    (vault / "绫音" / "日记").mkdir(parents=True)
    (vault / "绫音" / "日记" / "我的理解.md").write_text(
        "# 我的理解\n\n- (2026-10-27, 小窝) 杯子不在原来的地方时, 好几次都是在电脑桌上找到的\n- (2026-10-26, 看视频) 下雨天大家都想待在家里\n",
        encoding="utf-8")
    legacy = tmp_path / "cognition"
    legacy.mkdir()
    (legacy / "carry-events.jsonl.migrated").write_text("{}\n", encoding="utf-8")
    cfg = make_cfg(tmp_path, f"memory.vault={json.dumps(str(vault))}", f"room.cognition.data_dir={json.dumps(str(home))}")
    pink = "IR_Item_Espresso_Pink"

    async def main():
        app = create_app(cfg)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            room = app[ENGINES].room
            room.carry_plan({"cup": pink, "fill": "full", "hot": True, "route": "none", "wait_s": 480})
            room.carry_done({"delivered": True})
            room.observe({"type": "room_observe", "at": "coffee", "seen": {"coffee_machine": [pink], "water_caddy": []}, "hand": []})
            ws = await client.ws_connect("/ws")
            gen0 = json.loads((await ws.receive()).data)["room_gen"]
            res = await (await client.post("/api/experience", json={"action": "reset", "confirm": True})).json()
            pushed = None
            while pushed is None:  # 开着的连接马上收到新代次
                m = json.loads((await asyncio.wait_for(ws.receive(), 5)).data)
                pushed = m if m.get("type") == "room_gen" else None
            await ws.close()
            deep = await (await client.post("/api/experience", json={"action": "reset", "confirm": True,
                                                                     "scope": ["growth-history"]})).json()
            hist = (await (await client.get("/api/experience")).json())["history"]
            stale = room.observe({"type": "room_observe", "gen": 0, "at": "coffee", "seen": {"coffee_machine": [pink], "water_caddy": []}})
            fresh = room.carry_plan({"cup": pink, "gen": res["generation"], "fill": "full", "hot": True, "route": "none"})
            (legacy / "room-events.jsonl").write_text('{"seq": 0}\n', encoding="utf-8")  # 旧位置又冒出来同名的
            return gen0, res, stale, fresh, room.status(), pushed, deep, hist
        finally:
            await client.close()

    gen0, res, stale, fresh, live, pushed, deep, hist = asyncio.run(main())
    assert gen0 == 0 and res["generation"] == 1 and set(res["scope"]) == {"experience", "growth", "understanding", "page"}
    assert pushed == {"type": "room_gen", "gen": 1} and [h["gen"] for h in hist] == [1]
    assert res["cleared"]["growth"]["history"] is False and deep["cleared"]["growth"]["history"] is True  # 训练历史要明说才清
    assert deep["generation"] == 1 and "experience" not in deep["cleared"]
    assert set(res["cleared"]["experience"]["files"]) >= {"room-events.jsonl", "carry-events.jsonl"}
    assert res["cleared"]["experience"]["legacy"] == ["carry-events.jsonl.migrated"]
    assert "删了 1 条" in res["cleared"]["understanding"] and isinstance(res["cleared"]["growth"], dict)
    assert any("没标来源" in k for k in res["kept"]) and "页面" in res["cleared"]["page"]
    for topic in ("空闲时自己练", "logs/"):  # 动作成长回包里同样的几样不重复列
        assert sum(topic in k for k in res["kept"]) == 1, res["kept"]
    assert stale is None and fresh is not None and live["carry"] == 0 and live["events"] == 0 and live["generation"] == 1
    understanding = (vault / "绫音" / "日记" / "我的理解.md").read_text(encoding="utf-8")
    assert "小窝" not in understanding and "下雨天" in understanding

    async def restart():
        app = create_app(cfg)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return app[ENGINES].room.status(), store.status(home)
        finally:
            await client.close()

    again, st = asyncio.run(restart())
    assert again["events"] == 0 and again["carry"] == 0 and again["generation"] == 1  # 旧位置那个没被搬回来
    assert "room-events.jsonl" not in st["files"] and st["last_reset"]["why"] == "重置小窝学习"

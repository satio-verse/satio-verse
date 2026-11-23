"""Trusted maps, scene-scoped experience and mock Session routing; no real user data."""
import asyncio
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from wm import room_plan
from wm.cognition import layout, store
from wm.cognition.live import LiveRoom, sanitize
from wm.cognition.registry import RoomRegistry

SCENE = "casa-luce-v8"
OLD = layout.DEFAULT_SCENE_ID
CUP = "IR_Item_Espresso_Pink"  # Deliberately shared ID: coordinates must still stay separate.


def profile():
    return {"sceneId": SCENE, "sight": {
        "version": 1, "zones": {"north": [10, 10], "south": [10, 20]}, "walk": {}, "also": {},
        "regions": {"new_table": {"zone": "north", "at": [10, 1, 10], "r": 1},
                    "new_box": {"zone": "south", "at": [10, 1, 20], "r": 1, "box": True}},
        "items": [CUP, "New_Mug"],
        "names": {"north": "北角", "south": "南角", "new_table": "新桌上", "new_box": "新柜里",
                  CUP: "新屋粉杯", "New_Mug": "新屋白杯"},
        "kinds": {CUP: "cup", "New_Mug": "cup"}, "kindNames": {"cup": "杯子"}, "favorite": CUP},
        "life": [{"id": "new_rest", "name": "在新屋休息", "gives": {"energy": 10}}]}


@pytest.fixture
def trusted(tmp_path, monkeypatch):
    p = tmp_path / "profiles" / (SCENE + ".json")
    p.parent.mkdir()
    p.write_text(json.dumps(profile(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setitem(layout.SCENE_PROFILES, SCENE, p)
    return p


def cfg(tmp_path):
    return {"trace": {"log_dir": str(tmp_path)}, "room": {"cognition": {}}}


def observation(scene=SCENE, **more):
    msg = ({"scene_id": scene, "at": "north", "seen": {"new_table": [CUP]},
            "regions": ["new_table", "new_box"]} if scene == SCENE else
           {"at": "coffee", "seen": {"coffee_machine": [CUP]}, "regions": list(layout.REGIONS)})
    return {"type": "room_observe", "hand": [], "open": [], **msg, **more}


def test_layout_is_immutable_and_does_not_change_legacy(trusted):
    public = layout.load_scene(SCENE)
    assert public.dist("north", "south") == 10
    assert public.item_name(CUP) == "新屋粉杯"
    assert layout.ITEMS[CUP][0] == "粉色浓缩咖啡杯"
    assert "north" not in layout.ZONES and "coffee" not in public.ZONES
    assert layout.load_scene() is layout.LEGACY
    assert layout.load_scene("petal-oak") is layout.LEGACY
    with pytest.raises(TypeError):
        public.ZONES["north"] = ("别处", (0, 0))
    with pytest.raises(TypeError):
        public.ZONES["north"][1][0] = 0
    raw = profile()
    raw["sight"]["names"]["Curtain_Left.001"] = "窗帘"
    public = layout.from_profile(raw, SCENE)
    raw["sight"]["zones"]["north"][0] = 90
    assert public.ZONES["north"][1] == (10, 10)


@pytest.mark.parametrize("bad", ["../private", "C:/private.json", "unknown", "", [], {}])
def test_untrusted_scene_id_is_rejected(bad):
    with pytest.raises(ValueError):
        layout.load_scene(bad)


@pytest.mark.parametrize("change", [
    lambda p: p.pop("sight"),
    lambda p: p.update(sceneId="other"),
    lambda p: p["sight"].update(version=True),
    lambda p: p["sight"]["zones"].update(north=[float("nan"), 0]),
    lambda p: p["sight"]["regions"]["new_table"].update(zone=[]),
    lambda p: p["sight"].update(favorite=[]),
    lambda p: p["sight"]["names"].pop(CUP),
    lambda p: p["sight"].update(items=[CUP, CUP]),
])
def test_bad_new_profile_never_falls_back(change):
    raw = profile()
    change(raw)
    with pytest.raises(ValueError):
        layout.from_profile(raw, SCENE)


def test_registry_concurrent_scenes_replay_and_truth_gate(tmp_path, trusted):
    registry = RoomRegistry(cfg(tmp_path))
    old, new = registry.get(), registry.get(SCENE)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(old.observe, observation(OLD)), pool.submit(new.observe, observation())]
        assert all(job.result() is not None for job in jobs)
    assert old.belief.fact(CUP)["at"] == "coffee_machine"
    assert new.belief.fact(CUP)["at"] == "new_table"
    assert "新桌上" in new.answer("杯子在哪") and "咖啡机上" in old.answer("杯子在哪")
    assert new.path == tmp_path / "cognition" / "scenes" / SCENE / "room-events.jsonl"
    assert old.path == tmp_path / "cognition" / "room-events.jsonl"
    assert old.mind is not new.mind and old.layout is not new.layout
    again = RoomRegistry(cfg(tmp_path))
    assert again.get(SCENE).mind.cog.digest() == new.mind.cog.digest()
    assert again.get().mind.cog.digest() == old.mind.cog.digest()
    # Same observations plus different hidden state must produce identical inputs.
    a = sanitize(observation(fill={CUP: "half", "New_Mug": "full"}, temp=99, ml=300,
                             positions={CUP: [1, 2, 3]}, sight={"zones": {}}), new.regions, 10, new.layout)
    b = sanitize(observation(fill={CUP: "half", "New_Mug": "empty"}, temp=20, ml=0), new.regions, 10, new.layout)
    assert a == b and a["fill"] == {CUP: "half"}
    assert "temp" not in a and "ml" not in a and "sight" not in a


def test_empty_position_binds_without_negative_observation(tmp_path, trusted):
    new = RoomRegistry(cfg(tmp_path)).get(SCENE)
    assert new.observe(observation(at=None, seen={})) is None
    assert new.status()["events"] == 0
    step = new.search(CUP, new=True)
    assert step["action"] == ["go", "north"]  # Stable public-map order, no invented old coordinates.
    assert "coffee" not in json.dumps(step)


def test_carry_afford_separate_and_refuse_cross_scene_replay(tmp_path, trusted):
    registry = RoomRegistry(cfg(tmp_path))
    old, new = registry.get(), registry.get(SCENE)
    assert new.carry_plan({"scene_id": SCENE, "cup": CUP, "fill": "full", "route": "none"}) is not None
    assert old.trip is None
    assert new.carry_done({"scene_id": OLD, "delivered": True}) is None
    assert new.carry_done({"scene_id": SCENE, "delivered": True}) is not None
    assert len(new.carry.records) == 1 and not old.carry.records
    new.observe(observation(affords={"things": {"book": ["Book1"]}, "spots": ["new_table"], "acts": ["read"]},
                            things=["book"], visited=["new_table"]))
    assert "book" in new.afford.things and not old.afford.things
    assert all(json.loads(line)["scene_id"] == SCENE for line in new.afford_path.read_text(encoding="utf-8").splitlines())
    assert RoomRegistry(cfg(tmp_path)).get(SCENE).status()["carry"] == 1
    # Renaming a legacy log into a new namespace is not an implicit migration.
    old.observe(observation(OLD))
    new.path.write_bytes(old.path.read_bytes())
    with pytest.raises(ValueError, match="归属"):
        RoomRegistry(cfg(tmp_path)).get(SCENE)
    new.path.unlink()
    new.carry_path.write_text('{"kind":"trip","scene_id":"other"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="归属"):
        RoomRegistry(cfg(tmp_path)).get(SCENE)
    new.carry_path.unlink()
    new.afford_path.write_text('{"op":"saw","id":"book","t":0,"scene_id":"other"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="归属"):
        RoomRegistry(cfg(tmp_path)).get(SCENE)


def test_export_delete_and_generation_cover_all_scenes(tmp_path, trusted):
    registry = RoomRegistry(cfg(tmp_path))
    old, new = registry.get(), registry.get(SCENE)
    old.observe(observation(OLD))
    new.observe(observation())
    files = {"room-events.jsonl", f"scenes/{SCENE}/room-events.jsonl"}
    assert set(store.status(registry.base)["files"]) == files
    archive = store.export(registry.base, tmp_path / "export.zip")
    with zipfile.ZipFile(archive) as z:
        assert set(z.namelist()) == files | {"status.json"}
    generation = store.bump(registry.base, "test reset", ["experience"])
    assert set(registry.forget(generation)) == files
    assert not store.status(registry.base)["files"]
    assert new.observe(observation(gen=0)) is None and old.observe(observation(OLD, gen=0)) is None
    assert not old.belief.facts and not new.belief.facts
    assert new.observe(observation(gen=generation)) is not None
    # Even an unopened namespace is deleted; no live object is necessary.
    other = RoomRegistry(cfg(tmp_path))
    other.get()
    assert other.forget(generation + 1) == [f"scenes/{SCENE}/room-events.jsonl"]
    assert other.get(SCENE).generation == generation + 1


def test_page_generation_does_not_delete_experience(tmp_path, trusted):
    registry = RoomRegistry(cfg(tmp_path))
    registry.get().observe(observation(OLD))
    registry.set_generation(3)
    new = registry.get(SCENE)
    assert new.generation == 3 and registry.get().generation == 3
    assert new.observe(observation(gen=2)) is None
    assert registry.get().status()["events"] == 1


def test_new_scene_does_not_import_old_afford_fallback(tmp_path, trusted):
    old_afford = trusted.with_name("petal-oak.afford.json")
    old_afford.write_text('{"acts":[{"id":"old_only","name":"旧屋活动"}]}', encoding="utf-8")
    assert "old_only" in room_plan.load_acts(trusted)
    assert set(room_plan.load_acts(trusted, allow_legacy_afford=False)) == {"new_rest"}


def test_sessions_route_actual_scene_and_cancel_late_plan(tmp_path, trusted):
    from test_pipeline import make_cfg
    from wm.engines import Engines
    from wm.pipeline import Session
    from wm.trace import LatencyStats, TraceLog

    async def main():
        config = make_cfg(tmp_path, "room.think.mock_ms=100", "room.think.min_interval_s=0")
        engines = Engines(config)
        await engines.load()
        sent, old_sent = [], []

        async def send(msg):
            sent.append(msg)

        async def old_send(msg):
            old_sent.append(msg)

        s = Session(engines, config, send, LatencyStats(), TraceLog(tmp_path))
        legacy = Session(engines, config, old_send, LatencyStats(), TraceLog(tmp_path))
        try:
            await s.on_message({"type": "room_search", "id": 0, "scene_id": SCENE, "item": CUP})
            assert sent[-1]["reason"] == "scene"  # Must bind explicitly first.
            await s.on_message(observation(id=1, at=None, seen={}))
            assert s.room_scene_id == SCENE and s.room_active.status()["events"] == 0
            await s.on_message(observation(id=2))
            await legacy.on_message(observation(OLD, id=3))
            assert sent[-1]["scene_id"] == SCENE and old_sent[-1]["scene_id"] == OLD
            await s.on_message({"type": "room_search", "id": 4, "scene_id": SCENE, "item": CUP})
            assert sent[-1]["action"] == ["pick", CUP] and "新桌上" in sent[-1]["knows"]
            await s.on_message({"type": "room_carry", "id": 5, "scene_id": SCENE, "cup": CUP, "fill": "full"})
            assert sent[-1]["ok"] and s.room_active.trip is not None
            await s.on_message({"type": "room_think", "id": 6, "scene_id": SCENE, "options": ["new_rest", "snack"]})
            pending = s.room_task
            await pending
            assert sent[-1]["pick"] == "new_rest" and sent[-1]["scene_id"] == SCENE
            await s.on_message({"type": "room_think", "id": 7, "scene_id": SCENE, "options": ["new_rest"]})
            pending = s.room_task
            await s.on_message(observation(OLD, id=8))
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not any(m.get("id") == 7 for m in sent)
            await s.on_message({"type": "room_carry_done", "id": 9, "scene_id": SCENE, "delivered": True})
            assert sent[-1]["reason"] == "scene" and sent[-1]["scene_id"] == SCENE
            assert engines.room_registry.get(SCENE).status()["carry"] == 0
            for bad in ("../other", None, {}):
                await s.on_message(observation(id=10, scene_id=bad))
                assert sent[-1]["reason"] == "scene" and s.room_scene_id == OLD
            await s.on_message(observation(OLD, id=11, scene_id="petal-oak"))
            assert sent[-1]["ok"] and sent[-1]["scene_id"] == OLD
        finally:
            await s.close()
            await legacy.close()
            await engines.close()
    asyncio.run(main())


@pytest.mark.parametrize("scope", [["page"], ["experience"]])
def test_experience_api_reset_updates_both_scenes(tmp_path, trusted, scope):
    from aiohttp.test_utils import TestClient, TestServer
    from test_pipeline import make_cfg
    from wm.app import ENGINES, create_app

    async def main():
        app = create_app(make_cfg(tmp_path))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            registry = app[ENGINES].room_registry
            old, new = registry.get(), registry.get(SCENE)
            old.observe(observation(OLD))
            new.observe(observation())
            assert set(app[ENGINES].status()["room_scenes"]) == {OLD, SCENE}
            response = await client.post("/api/experience", json={"action": "reset", "confirm": True, "scope": scope})
            assert response.status == 200
            result = await response.json()
            assert old.generation == new.generation == store.generation(registry.base) == 1
            assert old.observe(observation(OLD, gen=0)) is None
            assert new.observe(observation(gen=0)) is None
            if "experience" in scope:
                assert old.status()["events"] == new.status()["events"] == 0
                assert not store.status(registry.base)["files"]
            else:
                assert old.status()["events"] == new.status()["events"] == 1
            assert result is not None
        finally:
            await client.close()
    asyncio.run(main())

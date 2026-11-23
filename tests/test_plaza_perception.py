"""广场的感知门 (plugins/plaza-2d/plaza_perception.py) 和双胞胎夹具 (wm/world/twin.py). 世界1 验收 2.

感知门是"真值 -> 认知"的唯一一道门: 视野外的人和动静一律没有事件; 事件只带清单声明的字段.
双胞胎: 差在她感知不到的地方 -> 逐步相同; 差在感知得到的地方 -> 不同. 另有三个"泄露替身", 同一个比对必须判出不同,
证明这套比对真能抓到泄露 (不改真文件).
"""
import json
import random

import pytest

from plaza_fakes import PLUGIN, load_manifest, load_perception_mod, load_spec
from wm.world.twin import load_perception, run_twin

PM = load_perception_mod()
Perception = PM.Perception


def joined(me=(2, 2), others=(), name="我"):
    return {"t": "joined", "code": "ABCD2345", "you": {"id": "m1", "name": name, "pos": list(me)},
            "members": [{"id": i, "name": n, "pos": list(p)} for i, n, p in others], "grid": {"w": 24, "h": 14}}


def ev(k, i, name, pos, **kw):
    return {"t": "ev", "k": k, "id": i, "name": name, "pos": list(pos), **kw}


def types(events):
    return [e["type"] for e in events]


def test_distance_bucket_direction():
    assert PM.dist((0, 0), (3, 5)) == 5
    assert [PM.bucket(d) for d in (0, 2, 3, 4, 5, 6)] == ["near", "near", "mid", "mid", "far", "far"]
    assert PM.direction((5, 5), (5, 5)) == "here"
    assert PM.direction((5, 5), (9, 6)) == "right" and PM.direction((5, 5), (1, 6)) == "left"
    assert PM.direction((5, 5), (6, 1)) == "up" and PM.direction((5, 5), (5, 9)) == "down"


def test_snapshot_only_reports_what_is_in_sight():
    p = Perception()
    out = p.feed(joined(me=(2, 2), others=[("m2", "近的", (4, 3)), ("m3", "远的", (20, 12)), ("m4", "边上", (8, 2))]))
    assert sorted(e["fields"]["pet_name"] for e in out) == sorted(["近的", "边上"])   # 距离 2 和 6 在视野内, 18 不在
    assert all(e["type"] == "pet_entered" for e in out)
    assert "远的" not in json.dumps(out, ensure_ascii=False)


def test_unseen_activity_produces_nothing():
    p = Perception()
    p.feed(joined(others=[("m2", "远的", (20, 12))]))
    assert p.feed(ev("moved", "m2", "远的", (19, 12))) == []
    assert p.feed(ev("said", "m2", "远的", (19, 12), text="喂")) == []
    assert p.feed(ev("emote", "m2", "远的", (19, 12), emote="wave")) == []
    assert p.feed(ev("left", "m2", "远的", (19, 12))) == []
    assert p.feed(ev("entered", "m5", "另一个远的", (22, 13))) == []


def test_enter_move_leave_sight_and_room():
    p = Perception()
    p.feed(joined())
    out = p.feed(ev("entered", "m2", "小满", (8, 2)))   # 距离 6: 刚好在视野边上
    assert types(out) == ["pet_entered"] and out[0]["fields"] == {"pet_name": "小满", "distance_bucket": "far", "direction": "right"}
    assert p.feed(ev("moved", "m2", "小满", (8, 2))) == []                  # 位置没变档: 不重复报
    out = p.feed(ev("moved", "m2", "小满", (5, 2)))
    assert types(out) == ["pet_moved"] and out[0]["fields"]["distance_bucket"] == "mid"
    out = p.feed(ev("moved", "m2", "小满", (9, 2)))                          # 走出视野
    assert types(out) == ["pet_left"] and out[0]["fields"] == {"pet_name": "小满"}
    assert p.feed(ev("moved", "m2", "小满", (10, 2))) == []
    out = p.feed(ev("moved", "m2", "小满", (4, 2)))                          # 又回来
    assert types(out) == ["pet_entered"]
    out = p.feed(ev("left", "m2", "小满", (4, 2)))                           # 离开房间: 她只看到"不见了"
    assert types(out) == ["pet_left"] and out[0]["fields"]["pet_name"] == "小满"


def test_hearing_is_shorter_than_sight():
    p = Perception()
    p.feed(joined(others=[("m2", "近", (5, 2)), ("m3", "边", (7, 2))]))   # 距离 3 和 5
    near = p.feed(ev("said", "m2", "近", (5, 2), text="你好"))
    assert types(near) == ["pet_said"] and near[0]["text"] == "你好" and near[0]["fields"]["distance_bucket"] == "mid"
    assert p.feed(ev("said", "m3", "边", (7, 2), text="听不见")) == []


def test_emote_only_when_seen():
    p = Perception()
    p.feed(joined(others=[("m2", "近", (3, 2)), ("m3", "远", (20, 12))]))
    assert types(p.feed(ev("emote", "m2", "近", (3, 2), emote="wave"))) == ["pet_emoted"]
    assert p.feed(ev("emote", "m3", "远", (20, 12), emote="nod")) == []


def test_own_move_changes_what_she_sees():
    p = Perception()
    p.feed(joined(me=(2, 2), others=[("m2", "远", (12, 2))]))
    out = p.feed({"t": "ack", "ok": True, "pos": [7, 2], "rid": "r1"})   # 走过去了: 距离 5
    assert types(out) == ["pet_entered"] and out[0]["fields"]["direction"] == "right"
    out = p.feed({"t": "ack", "ok": True, "pos": [1, 2], "rid": "r2"})
    assert types(out) == ["pet_left"]
    assert p.feed({"t": "ack", "ok": False, "why": "rate"}) == [] and p.own_position() == "1,2"


def test_reconnect_snapshot_diffs_against_what_she_saw():
    p = Perception()
    p.feed(joined(me=(2, 2), others=[("m2", "旧友", (4, 2)), ("m3", "走了", (5, 3))]))
    # 重连: 新快照里旧友还在、"走了"没了、来了新人
    out = p.feed(joined(me=(2, 2), others=[("m2", "旧友", (4, 2)), ("m9", "新人", (3, 3))]))
    assert sorted((e["type"], e["fields"]["pet_name"]) for e in out) == [("pet_entered", "新人"), ("pet_left", "走了")]


def test_look_and_panel_split_truth_from_cognition():
    p = Perception()
    p.feed(joined(me=(2, 2), others=[("m2", "近", (4, 2)), ("m3", "远", (20, 12))]))
    look = p.look()
    assert look["pets"] == [{"pet_name": "近", "distance_bucket": "near", "direction": "right"}]
    text = json.dumps(look, ensure_ascii=False)
    assert "远" not in text and "20" not in text and "m3" not in text
    panel = p.panel()   # 面板是主人看的: 有全部真值, 并标出她看没看到
    assert {(m["name"], m["seen"]) for m in panel["members"]} == {("近", True), ("远", False)}


def test_events_only_carry_declared_fields_random_walk():
    """随机走一大串帧: 每条事件的字段都在清单声明里, 而且事件里从不出现视野外的人."""
    declared = {e["type"]: set(e.get("fields", [])) for e in load_manifest()["plugin"]["world"]["events"]}
    rng = random.Random(7)
    p = Perception()
    names = {f"m{i}": f"宠{i}" for i in range(2, 8)}
    pos = {k: (rng.randrange(24), rng.randrange(14)) for k in names}
    p.feed(joined(me=(12, 7), others=[(k, names[k], pos[k]) for k in names]))
    for _ in range(600):
        k = rng.choice(list(names))
        pos[k] = (min(23, max(0, pos[k][0] + rng.choice((-1, 0, 1)))), min(13, max(0, pos[k][1] + rng.choice((-1, 0, 1)))))
        kind = rng.choice(("moved", "moved", "said", "emote"))
        extra = {"text": "话"} if kind == "said" else {"emote": "wave"} if kind == "emote" else {}
        out = p.feed(ev(kind, k, names[k], pos[k], **extra))
        for e in out:
            assert e["type"] in declared and set(e["fields"]) <= declared[e["type"]], e
            if e["type"] == "pet_said":
                assert PM.dist(p.pos, pos[k]) <= PM.HEAR
        if rng.random() < 0.1:
            p.feed({"t": "ack", "ok": True, "pos": [rng.randrange(24), rng.randrange(14)]})


# -- 双胞胎夹具 ---------------------------------------------------------------------

def fixture(name):
    return json.loads((PLUGIN / "fixtures" / (name + ".json")).read_text(encoding="utf-8"))


def test_twin_far_pet_behaviour_identical():
    r = run_twin(fixture("twin-far-pet"), load_spec(), load_perception(PLUGIN))
    assert r.expect == "same" and r.ok and not r.diffs
    # 不是空比较: 两边都真的发生过事件、问过模型、有过动作
    assert any(s.prompt for s in r.steps_a) and any(s.actions for s in r.steps_a)
    flat = json.dumps([s.prompt for s in r.steps_a + r.steps_b if s.prompt], ensure_ascii=False)
    assert "远方的客人" not in flat and "大家好呀" not in flat and "悄悄话" not in flat


def test_twin_near_pet_behaviour_differs():
    r = run_twin(fixture("twin-near-pet"), load_spec(), load_perception(PLUGIN))
    assert r.expect == "differ" and r.ok and r.diffs


def _mutant(**attrs):
    return lambda: type("Mutant", (Perception,), attrs)()


def test_twin_catches_sight_leak():
    """泄露替身 1: 视野半径漏成 30 -> 视野外的客人进了她的事件 -> 双胞胎必须判出不同."""
    r = run_twin(fixture("twin-far-pet"), load_spec(), _mutant(SIGHT=30))
    assert not r.ok and r.diffs


def test_twin_catches_hearing_leak():
    """泄露替身 2: 听力半径漏成视野那么大 -> 听力半径外的悄悄话被听到."""
    r = run_twin(fixture("twin-far-pet"), load_spec(), _mutant(HEAR=PM.SIGHT))
    assert not r.ok and r.diffs


def test_twin_catches_roster_leak_in_events():
    """泄露替身 3: 视野外的人一动就给她发一条 pet_moved (只带名字, 看起来无害)."""
    class Leaky(Perception):
        def feed(self, frame):
            out = super().feed(frame)
            if frame.get("t") == "ev" and frame.get("id") not in self.seen and frame.get("k") == "moved":
                out.append({"type": "pet_moved", "fields": {"pet_name": frame.get("name")}, "text": ""})
            return out

    r = run_twin(fixture("twin-far-pet"), load_spec(), Leaky)
    assert not r.ok and r.diffs


def test_twin_runner_not_vacuous_when_expect_differ_but_identical():
    """比对本身不会"永远通过": 把 near 夹具里 A 的差异拿掉, expect=differ 必须判不过."""
    fx = fixture("twin-near-pet")
    fx["timeline"] = [t for t in fx["timeline"] if "only" not in t]
    assert not run_twin(fx, load_spec(), load_perception(PLUGIN)).ok

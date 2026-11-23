"""心跳的测试 (2026-11-17).

判断层 (`wm/heartbeat.py`) 是纯的, 穷举测; 读写层 (`wm/agent/heartbeat.py`) 全部落在
tmp_path 里, 不碰真人的 `~/.worldmodel/`。

要保住的几条底线:
1. 拍子必须落在 11–26 分钟之间, 且真随机 (不能退化成定间隔)。
2. "主人在不在"只认他自己打的字 —— auto/system/agent 来的不算。
3. 自己出题三道闸: 少于 20 字退回、少于 3 轮不收、跨度不到 20 分钟不算想过。
4. 额度用完必须停, 而且**不能**因此去叫醒主人。
5. 读坏的文件当"没有", 不能让心跳把服务拖挂。
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from wm import heartbeat as H
from wm.agent import heartbeat as A


NOW = datetime(2026, 11, 17, 12, 0, 0)


# ---------------------------------------------------------------- 判断层

def test_拍子_落在十一到二十六分钟之间且真随机():
    rng = random.Random(7)
    gaps = [H.next_gap_s(rng) for _ in range(500)]
    assert all(H.MIN_GAP_S <= g <= H.MAX_GAP_S for g in gaps)
    assert len(set(gaps)) > 400, "不该退化成定间隔"
    mean_min = sum(gaps) / len(gaps) / 60.0
    assert 11 <= mean_min <= 26, f"平均间隔应落在 11–26 分钟, 实际 {mean_min:.1f}"
    assert 17 <= mean_min <= 20, "均匀随机的均值应该在 18.5 分钟附近"


def test_owner_present_只认他自己打的字():
    assert H.owner_present([(NOW - timedelta(minutes=1), "我在")], NOW) is True
    # 渠道自动消息、系统消息、她自己的话都不算
    assert H.owner_present([(NOW - timedelta(minutes=1), "在吗", "auto")], NOW) is False
    assert H.owner_present([(NOW - timedelta(minutes=1), "在吗", "system")], NOW) is False
    assert H.owner_present([(NOW - timedelta(minutes=1), "在吗", "agent")], NOW) is False
    # 空白不算
    assert H.owner_present([(NOW - timedelta(minutes=1), "   ")], NOW) is False
    # 超出窗口不算
    assert H.owner_present([(NOW - timedelta(minutes=30), "刚才在")], NOW) is False
    assert H.owner_present([], NOW) is False
    assert H.owner_present(None, NOW) is False


def test_owner_present_吃三种时间写法():
    assert H.owner_present([(NOW.timestamp() - 5, "1")], NOW) is True
    assert H.owner_present([((NOW - timedelta(seconds=5)).isoformat(), "2")], NOW) is True
    assert H.owner_present([((NOW - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S"), "3")], NOW) is True
    assert H.owner_present([("看不懂的时间", "4")], NOW) is False


def test_beat_的一行():
    b = H.Beat(at=NOW, owner_present=True, doing="在整理记忆", idle_beats=3, note="顺手")
    line = b.line()
    assert line["at"].startswith("2026-11-17T12:00:00")
    assert line["owner"] is True
    assert "主人在" in b.text() and "在整理记忆" in b.text()
    quiet = H.Beat(at=NOW, owner_present=False)
    assert "什么也没发生" in quiet.text(), "大多数拍本来就该是空的"


def test_唤醒卡_渲染与上限():
    card = H.WakeCard(
        unanswered=[f"问题{i}" for i in range(20)],
        drawer=["想通了 A"],
        today=["看了 B 站"],
        idle_beats=4,
    )
    txt = card.render()
    assert "空转了 4 拍" in txt
    assert txt.count("- ") <= 3 * H.CARD_MAX_ITEMS
    assert card.empty() is False
    assert H.WakeCard().empty() is True
    assert "空的" in H.WakeCard().render()


def test_自己出题_三道闸():
    t = H.Thinking()
    assert t.add("太短", NOW) is False, "少于 20 字退回"
    assert t.rounds == []
    long1 = "我想把这台机器上所有和记忆有关的开关都过一遍，看哪个其实没起作用。"
    assert t.add(long1, NOW) is True
    assert t.ready() is False, "一轮不收"
    t.add(long1, NOW + timedelta(minutes=10))
    assert t.ready() is False, "两轮也不收"
    t.add(long1, NOW + timedelta(minutes=25))
    assert t.ready() is True
    assert t.harvest() == long1


def test_自己出题_三轮挤在二十分钟内不算想过():
    t = H.Thinking()
    s = "这是一句够长的话，用来通过二十个字那道闸门，看看跨度怎么算。"
    for i in range(3):
        t.add(s, NOW + timedelta(minutes=i * 3))
    assert t.ready() is False
    t.reset()
    assert t.rounds == []


def test_额度_用完就停():
    b = H.Budget(cap_cny=0.5)
    assert b.allow(0.2) is True
    assert b.spend(0.2) is True
    assert abs(b.left_cny() - 0.3) < 1e-9
    assert b.spend(0.4) is False, "超了不能记账"
    assert abs(b.spent_cny - 0.2) < 1e-9
    assert b.spend(0.3) is True
    assert b.exhausted() is True
    assert H.Budget(cap_cny=0).allow(0) is False, "额度 0 等于关"


def test_到期提醒的四种筛法():
    items = [
        {"id": 1, "text": "该喝水了", "due_at": (NOW - timedelta(minutes=5)).isoformat()},
        {"id": 2, "text": "还没到", "due_at": (NOW + timedelta(hours=1)).isoformat()},
        {"id": 3, "text": "关掉的", "due_at": (NOW - timedelta(hours=1)).isoformat(), "enabled": False},
        {"id": 4, "text": "已做完", "due_at": (NOW - timedelta(hours=1)).isoformat(), "status": "done"},
        {"id": 5, "text": "时间看不懂", "due_at": "下周"},
        "这不是字典",
    ]
    got = H.due_schedule(items, NOW)
    assert [x["id"] for x in got] == [1]
    assert H.open_todos([{"status": "todo"}, {"status": "done"}, {}]) == [{"status": "todo"}, {}]


def test_哈希链_定序且敏感():
    parts = [("a.txt", b"hello"), ("b.txt", b"world")]
    h1 = H.hash_chain("", parts)
    assert h1 == H.hash_chain("", list(reversed(parts))), "顺序不该影响结果"
    assert h1 != H.hash_chain("", [("a.txt", b"hello!"), ("b.txt", b"world")])
    assert h1 != H.hash_chain("前一天的串", parts), "前一天变了今天必须变"
    assert len(h1) == 64


def test_抽屉_去重与上限():
    d: list[str] = []
    for i in range(3):
        d = H.drawer_push(d, f"想通了的第 {i} 件事")
    assert len(d) == 3
    d = H.drawer_push(d, "想通了的第 1 件事")
    assert len(d) == 3 and d[-1] == "想通了的第 1 件事", "重复的挪到最新"
    many = [f"第 {i} 件" for i in range(H.DRAWER_MAX + 10)]
    assert len(H.drawer_push(many, "新的")) == H.DRAWER_MAX


def test_平时小结():
    rows = [{"owner": True, "idle_beats": 2}, {"owner": False, "idle_beats": 9}]
    s = H.summarize_beats(rows)
    assert s["beats"] == 2 and s["owner_ratio"] == 0.5 and s["idle_max"] == 9
    assert H.summarize_beats([])["beats"] == 0


def test_一行日志的编解码():
    rec = {"at": "2026-11-17T12:00:00", "owner": False, "doing": "打了个盹"}
    line = H.dumps(rec)
    assert "\\u" not in line, "中文不该被转义"
    assert H.loads(line) == rec
    assert H.loads("{坏掉") is None
    assert H.loads("[1,2]") is None


# ---------------------------------------------------------------- 读写层

def test_配置_默认关且额度为零点五():
    c = A.cfg_of({})
    assert c["enabled"] is False, "新东西默认关"
    assert c["budget_cny"] == 0.5
    assert A.cfg_of({"heartbeat": {"enabled": True, "budget_cny": "1.5"}})["budget_cny"] == 1.5
    assert A.cfg_of({"heartbeat": {"budget_cny": "不是数"}})["budget_cny"] == 0.5
    assert A.cfg_of(None)["enabled"] is False


def test_平时一行能写能读(tmp_path: Path):
    root = tmp_path / "hb"
    assert A.append_beat({"at": NOW.isoformat(), "owner": False, "idle_beats": 1}, root=root) is True
    assert A.append_beat({"at": NOW.isoformat(), "owner": True, "idle_beats": 0}, root=root) is True
    got = A.read_beats(datetime.now(), root=root)
    assert len(got) == 2 and got[0]["idle_beats"] == 1


def test_抽屉落盘与去重(tmp_path: Path):
    root = tmp_path / "hb"
    assert A.drawer_load(root=root) == []
    A.drawer_push("第一件", root=root)
    A.drawer_push("第二件", root=root)
    assert A.drawer_load(root=root) == ["第一件", "第二件"]
    A.drawer_push("第一件", root=root)
    assert A.drawer_load(root=root) == ["第二件", "第一件"]


def test_黑盒链_一天接一天(tmp_path: Path):
    root = tmp_path / "hb"
    day = datetime(2026, 11, 17)
    first = A.seal_day(day, [("日记.md", "今天".encode("utf-8"))], root=root)
    second = A.seal_day(day, [("日记.md", "今天改了一笔".encode("utf-8"))], root=root)
    assert first["hash"] != second["hash"]
    assert second["prev"] == first["hash"], "第二天要接上前一天的串"
    chain = json.loads((root / "chain.json").read_text(encoding="utf-8"))["chain"]
    assert len(chain) == 2


def test_额度按天存_跨天归零(tmp_path: Path):
    root = tmp_path / "hb"
    d1 = datetime(2026, 11, 17)
    b = A.budget_state(d1, 0.5, root=root)
    assert b.spent_cny == 0
    b.spend(0.4)
    A.budget_save(d1, b, root=root)
    assert abs(A.budget_state(d1, 0.5, root=root).spent_cny - 0.4) < 1e-9
    assert A.budget_state(d1 + timedelta(days=1), 0.5, root=root).spent_cny == 0, "新的一天从头算"


def test_读工作台的提醒_正常与读坏(tmp_path: Path):
    home = tmp_path / "agent"
    sd = home / "state" / "workbench-schedule"
    sd.mkdir(parents=True)
    (sd / "schedule.json").write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    {"id": 1, "text": "该喝水了", "due_at": (NOW - timedelta(minutes=5)).isoformat()},
                    {"id": 2, "text": "别的事", "due_at": (NOW + timedelta(hours=2)).isoformat()},
                ],
            }
        ),
        encoding="utf-8",
    )
    got = A.due_now(NOW, home=home)
    assert [x["id"] for x in got] == [1]

    (sd / "schedule.json").write_text("{这不是 json", encoding="utf-8")
    assert A.read_schedule(home) == [], "读坏了当没有, 不能抛"
    assert A.due_now(NOW, home=home) == []


def test_读待办(tmp_path: Path):
    home = tmp_path / "agent"
    td = home / "state" / "workbench-todo"
    td.mkdir(parents=True)
    (td / "todos.json").write_text(
        json.dumps({"next": 3, "items": [{"id": 1, "status": "todo"}, {"id": 2, "status": "done"}]}),
        encoding="utf-8",
    )
    assert [x["id"] for x in A.todo_open(NOW, home=home)] == [1]
    assert A.read_todos(tmp_path / "没有这个家") == []


def test_状态目录跟着环境变量走(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(A.ENV_STATE, str(tmp_path / "自定义"))
    assert A.state_dir({}) == tmp_path / "自定义"
    monkeypatch.delenv(A.ENV_STATE)
    d = A.state_dir({"trace": {"log_dir": str(tmp_path / "日志")}})
    assert "heartbeat" in d.parts and str(tmp_path / "日志") in str(d)


def test_agent_home_环境变量优先(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(A.ENV_HOME, str(tmp_path / "agent家"))
    assert A.agent_home() == tmp_path / "agent家"
    monkeypatch.delenv(A.ENV_HOME)
    assert A.agent_home().name == "agent"


# ---------------------------------------------------------------- 服务端循环 (Runner)

# 这一节是 2026-11-17 补的。那天的心跳真 bug: 4 个计数器 (beats/idle_beats/last_at/last_due)
# 被插到了 present() 的 return 后面 -> 成了死代码 -> 线上每一拍 AttributeError,
# 被 loop 的 except 吞成一行 WARNING「这一拍出错了」, 于是 ~/.worldmodel/heartbeat 一直没建出来。
# **为什么 22 条测试没拦住**: 它们只测纯逻辑和文件读写, 没有一条真的构造 Runner 去调 beat_once。
# 从此这条规矩写在这里: 碰 Runner 的改动, 必须有一条测试**真跑一拍**。

@pytest.fixture
def hb_dirs(tmp_path, monkeypatch):
    """心跳目录和工作台状态家都搬到 tmp, 不碰真人的 ~/.worldmodel。"""
    d = tmp_path / "hb"
    d.mkdir()
    monkeypatch.setenv(A.ENV_STATE, str(d))
    monkeypatch.setenv(A.ENV_HOME, str(tmp_path / "agent"))
    return d


def test_runner_has_all_counters():
    """回归: 计数器必须在 __init__ 里就位 —— 这正是线上那个 bug。"""
    r = A.Runner()
    for name in ("beats", "idle_beats", "last_at", "last_due"):
        assert hasattr(r, name), f"Runner 缺 {name}（是不是又被插到 present() 的 return 后面了？）"


def test_beat_writes_exactly_one_line(hb_dirs):
    r = A.Runner()
    rec = r.beat_once(NOW)
    assert rec["at"] and "owner" in rec and "idle_beats" in rec
    files = list(hb_dirs.glob("beats-*.jsonl"))
    assert len(files) == 1, "一拍应该只写一个文件"
    assert len(files[0].read_text("utf-8").strip().splitlines()) == 1
    assert r.beats == 1 and r.idle_beats == 1


def test_beat_counts_idle_when_absent(hb_dirs):
    r = A.Runner()
    r.beat_once(NOW)
    r.beat_once(NOW + timedelta(minutes=12))
    assert r.idle_beats == 2


def test_beat_marks_owner_present_from_owner_seen(hb_dirs):
    """主人在不在用真信号: 一分钟前找过她 = 在, 而且把"她在干什么"带出来。"""
    r = A.Runner(presence=lambda: NOW.timestamp() - 60, doing=lambda: "在整理记忆")
    rec = r.beat_once(NOW)
    assert rec["owner"] is True and rec["doing"] == "在整理记忆"
    assert r.idle_beats == 0, "主人在场时空转计数要归零"


def test_beat_absent_when_owner_seen_is_old(hb_dirs):
    r = A.Runner(presence=lambda: NOW.timestamp() - 3600)
    assert r.beat_once(NOW)["owner"] is False


def test_beat_absent_and_survives_when_presence_raises(hb_dirs):
    """读不到 owner_seen 就当不在 —— 而且不能把这一拍带走。"""
    def boom():
        raise RuntimeError("manager 还没起来")
    r = A.Runner(presence=boom)
    assert r.beat_once(NOW)["owner"] is False


def test_beat_calls_wake_when_something_is_due(hb_dirs, monkeypatch):
    monkeypatch.setattr(A, "due_now", lambda now, home=None: [{"text": "提醒你喝水"}])
    seen = []
    r = A.Runner(wake=lambda due: seen.append([d.get("text") for d in due]))
    r.beat_once(NOW)
    assert seen == [["提醒你喝水"]]
    assert (hb_dirs / "due.json").exists()


def test_beat_still_records_due_without_wake(hb_dirs, monkeypatch):
    """不给 wake 就是"只记账不叫醒" —— 到期清单照样写, 但没人被叫。"""
    monkeypatch.setattr(A, "due_now", lambda now, home=None: [{"text": "提醒你喝水"}])
    r = A.Runner()
    r.beat_once(NOW)
    assert (hb_dirs / "due.json").exists() and len(r.last_due) == 1


def test_beat_survives_write_failure(hb_dirs, monkeypatch):
    """写不进去也不能抛 —— 心跳本身不该把服务弄挂。"""
    def boom(*a, **k):
        raise OSError("磁盘满了")
    monkeypatch.setattr(A, "append_beat", boom)
    r = A.Runner()
    rec = r.beat_once(NOW)
    assert rec["at"] and r.beats == 1
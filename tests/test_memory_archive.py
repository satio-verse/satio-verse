"""三处硬删改归档 (记忆按脑区重设计阶段 6 的前一半, 2026-11-02; 规矩"遗忘只降权、归档, 不硬删"):
操作笔记超过 HOWTO_MAX 条、工作经验超过 MAX_ITEMS 条、经验推送记录超过 LOG_MAX_BYTES, 以前是丢掉 / 删掉 / 砍一半, 现在挪进归档.
每处都查: 活的 + 归档的 = 总数, 一条不少也不多; 归档写不上时这次先不挪."""
import importlib.util
import json
import sys
from datetime import date

from wm import memory as M
from wm import memory_migrate as MM
from wm.agent import experience as EX
from wm.config import ROOT
from wm.memory import Memory

sys.path.insert(0, str(ROOT / "tools"))
_spec = importlib.util.spec_from_file_location("memory_checkup", ROOT / "tools" / "memory_checkup.py")
MC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(MC)


def mem(tmp_path):
    m = Memory({"vault": str(tmp_path), "character": "绫音", "graph_notes": False})
    m.init()
    return m


def rows(path):
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")] if path.exists() else []


def test_howto_over_limit_moves_oldest_into_yearly_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "HOWTO_MAX", 5)
    m = mem(tmp_path)
    for i in range(9):
        assert m.add_howto(f"task{i:03d} alpha", f"note{i:03d} beta", ok=i % 2 == 0, day="2025-12-31" if i < 2 else "2026-01-02")
    live = rows(m.dir / "日记" / M.HOWTO_FILE)
    arch = {p.name: rows(p) for p in (m.dir / "日记" / M.HOWTO_ARCHIVE).glob("*.md")}
    assert len(live) == 5 and sorted(arch) == ["操作笔记-2025.md", "操作笔记-2026.md"]
    archived = arch["操作笔记-2025.md"] + arch["操作笔记-2026.md"]
    assert len(live) + len(archived) == 9 and not set(live) & set(archived)  # 活的 + 归档的 = 总数
    assert [r.split(") ", 1)[1] for r in archived] == [f"task{i:03d} alpha → note{i:03d} beta" for i in range(4)]
    assert m.howto_for("task001") == [] and m.howto_for("task008")  # 归档的不再翻出来
    head = (m.dir / "日记" / M.HOWTO_ARCHIVE / "操作笔记-2026.md").read_text(encoding="utf-8")
    assert head.startswith("# 操作笔记（归档，2026）") and head.count("# 操作笔记") == 1  # 追加, 表头只写一次
    assert m.dir / "日记" / M.HOWTO_ARCHIVE / "操作笔记-2026.md" not in m._files()  # 和日记一样不进检索


def test_howto_archive_failure_keeps_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "HOWTO_MAX", 3)
    m = mem(tmp_path)
    (m.dir / "日记").mkdir(parents=True, exist_ok=True)
    (m.dir / "日记" / M.HOWTO_ARCHIVE).write_text("挡路的文件", encoding="utf-8")  # 归档文件夹建不了
    for i in range(5):
        m.add_howto(f"task{i} alpha", f"note{i} beta", day="2026-11-02")
    assert len(rows(m.dir / "日记" / M.HOWTO_FILE)) == 5  # 宁可多留不丢
    (m.dir / "日记" / M.HOWTO_ARCHIVE).unlink()
    m.add_howto("task5 alpha", "note5 beta", day="2026-11-02")
    live, arch = rows(m.dir / "日记" / M.HOWTO_FILE), rows(m.dir / "日记" / M.HOWTO_ARCHIVE / "操作笔记-2026.md")
    assert len(live) == 3 and len(arch) == 3 and len(live) + len(arch) == 6


def item(i: int, now: float, trust: int = 3, support: int = 1, days: float = 0.0, contradict: int = 0) -> dict:
    t = now - days * 86400
    return {"id": f"exp-{i:04d}", "head": "npm test", "sig": f"Error {i}", "words": ["error"], "action": f"先做法{i}",
            "akey": "cmd:npm", "steps": [], "state": "candidate", "trust": trust, "enabled": True, "support": support,
            "contradict": contradict, "pushed": 0, "adopted": 0, "hits": 0, "created": t, "verified": t, "updated": t, "sources": []}


def test_experiences_over_limit_go_to_archive_not_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(EX, "MAX_ITEMS", 5)
    now = 1_800_000_000.0
    ex = EX.Experiences(tmp_path, clock=lambda: now)
    for i in range(8):  # 0、1 是你确认过的 (L2), 不挪; 2 退役了, 先挪; 再挪置信度最低的
        e = item(i, now, trust=2 if i < 2 else 3, support=1 + i, days=200 if i == 2 else 0)
        ex.items[e["id"]] = e
    ex._tidy(now)
    ex._save()
    arch = [json.loads(ln) for ln in ex.archive_path.read_text(encoding="utf-8").splitlines()]
    assert [a["id"] for a in arch] == ["exp-0002", "exp-0003", "exp-0004"]
    assert all(a["archived"] == now and a["archived_why"] == "超过 5 条" for a in arch)
    live = json.loads(ex.path.read_text(encoding="utf-8"))
    assert len(live) + len(arch) == 8 and not {e["id"] for e in live} & {a["id"] for a in arch}
    assert {"exp-0000", "exp-0001"} <= {e["id"] for e in live}
    assert EX.Experiences(tmp_path, clock=lambda: now).stats()["archived"] == 3  # 重启后照样数得到, 归档的不读回来
    assert len(EX.Experiences(tmp_path, clock=lambda: now).items) == 5


def test_experiences_archive_failure_keeps_items(tmp_path, monkeypatch):
    monkeypatch.setattr(EX, "MAX_ITEMS", 2)
    now = 1_800_000_000.0
    ex = EX.Experiences(tmp_path, clock=lambda: now)
    ex.archive_path.mkdir(parents=True)  # 归档写不上
    for i in range(4):
        e = item(i, now)
        ex.items[e["id"]] = e
    ex._tidy(now)
    assert len(ex.items) == 4


def test_push_log_rotation_moves_first_half_to_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(EX, "LOG_MAX_BYTES", 300)
    ex = EX.Experiences(tmp_path)
    sent = [{"t": i, "mode": "on", "exp": f"exp-{i}", "adopted": True, "outcome": "success", "steps": 2, "chars": 40} for i in range(40)]
    for row in sent:
        ex._log(row)
    live = [json.loads(ln) for ln in ex.log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    arch = [json.loads(ln) for ln in ex.log_archive.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert arch + live == sent  # 一条不少、不重复、顺序不乱
    assert ex.log_path.stat().st_size <= 300 + 2 * len(json.dumps(sent[-1])) + 4
    assert ex.stats()["on"]["pushes"] == len(live)  # 统计照旧只看活的那份


def test_core_and_checkup_see_archived_items(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "HOWTO_MAX", 2)
    vault, home = tmp_path / "vault", tmp_path / "agent"
    m = Memory({"vault": str(vault), "character": "绫音", "graph_notes": False})
    m.init()
    for i in range(4):
        m.add_howto(f"task{i} alpha", f"note{i} beta", day="2026-11-02")
    monkeypatch.setattr(EX, "MAX_ITEMS", 1)
    now = 1_800_000_000.0
    ex = EX.Experiences(home, clock=lambda: now)
    for i in range(3):
        e = item(i, now, support=1 + i)
        ex.items[e["id"]] = e
    ex._tidy(now)
    ex._save()
    r = MM.collect({"vault": str(vault), "character": "绫音", "graph_notes": False}, agent_home=home)
    status = {}
    for n in r["nodes"].values():
        if n["type"] == "procedure":
            status.setdefault((n["id"][0], n["status"]), 0)
            status[(n["id"][0], n["status"])] += 1
    assert status[("h", "active")] == 2 and status[("h", "archived")] == 2
    assert status[("x", "archived")] == 2 and sum(v for (k, s), v in status.items() if k == "x") == 3
    assert r["sources"]["操作笔记（归档）"] == 2 and r["sources"]["工作经验（归档）"] == 2 and not r["problems"]
    c = MC.checkup(r, today=date(2026, 11, 2), half_life=30, show_text=False)
    assert c["howto"]["归档"] == 2 and c["work_experience"]["archived"] == 2
    assert "归档 2" in MC.render(c)

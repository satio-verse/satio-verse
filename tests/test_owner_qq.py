"""主人私聊 -> 记忆候选 (wm/owner_qq.py, A 档): 只认主人自己打的字, 只进候选, 不碰日志整理进度."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wm import owner_qq as Q  # noqa: E402
from wm.memory import Memory  # noqa: E402

DAY = "2026-11-18"
LOG = """# 私聊 1556367761

- [21:53 #49669226 主人(1556367761)] 那就是姥姥姥爷爷爷奶奶爸爸妈妈身体永远健康
- [21:54 #49669226 主人(1556367761)] 嗯嗯
- [21:55 #1167843397 她] 嗯，我记住了
- [21:56 #49669226 主人(1556367761)] (回复 #-1372047801) 我对芒果过敏
- [21:57 #49669226 主人(1556367761)] ［表情包#3］
- [21:58 #49669226 主人(1556367761)] 你吃了吗
- [21:59 #49669226 主人(1556367761)] 帮我看看这个文件
- [22:00 #49669226 主人(1556367761)] 他说明天要去北京出差
- [22:01 #49669226 主人(1556367761)] https://example.com/x
"""


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "库"
    day_dir = vault / "外部渠道" / "qq" / "私聊1556367761" / "日志"
    day_dir.mkdir(parents=True)
    (day_dir / f"{DAY}.md").write_text(LOG, encoding="utf-8")
    # 别人的私聊 / 群: 没有 "主人(" 字样, 不该被认成主人
    other = vault / "外部渠道" / "qq" / "私聊999" / "日志"
    other.mkdir(parents=True)
    (other / f"{DAY}.md").write_text("- [10:00 #1 别人(999)] 你好\n", encoding="utf-8")
    return vault


def _cfg(vault: Path) -> dict:
    return {"memory": {"vault": str(vault), "character": "绫音", "graph_notes": False},
            "channels": {"qq": {"dir": "外部渠道/qq"}}}


def new_memory(vault: Path) -> Memory:
    m = Memory({"vault": str(vault), "character": "绫音", "graph_notes": False})
    mem = vault / "绫音" / "记忆"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "关于用户.md").write_text("", encoding="utf-8")
    (vault / "绫音" / "关系.md").write_text("# 关系\n\n- 认识天数: 0\n", encoding="utf-8")
    return m


def cand_text(vault: Path) -> list[str]:
    p = vault / "绫音" / "记忆" / "候选.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ------------------------------------------------------------------ 读日志
def test_parse_day_roles_and_reply_prefix(tmp_path):
    vault = _vault(tmp_path)
    root = Q.channel_root(_cfg(vault))
    path = root / "私聊1556367761" / "日志" / f"{DAY}.md"
    rows = Q.parse_day(path)
    assert [r["role"] for r in rows[:3]] == ["owner", "owner", "self"]
    reply = [r for r in rows if "芒果过敏" in r["text"]]
    assert reply and reply[0]["text"] == "我对芒果过敏", "（回复 #…）前缀要去掉"
    assert reply[0]["time"] == "21:56"


def test_owner_dirs_ignore_other_chats(tmp_path):
    vault = _vault(tmp_path)
    dirs = Q.owner_dirs(Q.channel_root(_cfg(vault)))
    assert [d.name for d in dirs] == ["私聊1556367761"]


def test_channel_root_rejects_escape(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["channels"]["qq"]["dir"] = "../外面"
    assert Q.channel_root(cfg) is None
    cfg["channels"]["qq"]["dir"] = "不存在"
    assert Q.channel_root(cfg) is None


def test_entries_for_skips_media_links_and_other_roles(tmp_path):
    vault = _vault(tmp_path)
    rows = Q.entries_for(Q.channel_root(_cfg(vault)), DAY)
    texts = [r["user"] for r in rows]
    assert "我对芒果过敏" in texts
    assert "嗯，我记住了" not in texts, "她说的不算"
    assert not any(t.startswith("［") or t.startswith("https://") for t in texts)
    assert all(r["via"] == "qq" for r in rows)
    assert Q.candidates_qq(rows) and "嗯嗯" not in [c["user"] for c in Q.candidates_qq(rows)]


# ------------------------------------------------------------------ 判定
def test_worth_qq_keeps_facts_drops_chatter():
    assert Q.worth_qq("我对芒果过敏") == 1
    assert Q.worth_qq("hhh 那肯定呀") <= 0
    assert Q.worth_qq("所以多思考") <= 0, "光有'考'字不算 (私聊里这种碎话太多)"
    long_line = "其实喜欢一个人呢是非常想跟对方二十四个小时都黏在一起的那种感觉"
    assert Q.worth_qq(long_line) == 1, "长句是在讲自己的想法"
    assert Q.worth_qq("刚才喝了口水") < 0


def test_candidates_qq_drops_questions_requests_borrowed(tmp_path):
    vault = _vault(tmp_path)
    rows = Q.entries_for(Q.channel_root(_cfg(vault)), DAY)
    cands = [c["user"] for c in Q.candidates_qq(rows)]
    assert "你吃了吗" not in cands, "问句"
    assert "帮我看看这个文件" not in cands, "指令"
    assert "他说明天要去北京出差" not in cands, "说的是别人, 不是用户的事"
    assert "嗯嗯" not in cands, "附和"
    assert "那就是姥姥姥爷爷爷奶奶爸爸妈妈身体永远健康" in cands, "不要求带'我'"


def test_candidates_qq_merges_same_sentence(tmp_path):
    vault = _vault(tmp_path)
    p = vault / "外部渠道" / "qq" / "私聊1556367761" / "日志" / f"{DAY}.md"
    p.write_text(LOG + "- [22:05 #49669226 主人(1556367761)] 我对芒果过敏\n", encoding="utf-8")
    rows = Q.entries_for(Q.channel_root(_cfg(vault)), DAY)
    cand = [c for c in Q.candidates_qq(rows) if "芒果过敏" in c["user"]]
    assert len(cand) == 1 and cand[0]["times"] == ["21:56", "22:05"]


def test_rewrite_qq_we_to_user_and_prefix():
    assert Q.rewrite_qq("我们喜欢用贴贴这种话", DAY) == "用户喜欢用贴贴这种话"
    got = Q.rewrite_qq("姥姥姥爷身体好", DAY)
    assert got.startswith("用户提到：") and "姥姥姥爷身体好" in got
    assert Q.rewrite_qq("我明天要去医院复查", DAY) == "用户11月19日要去医院复查"


def test_select_qq_dedupes_known_and_batch():
    cands = [{"user": "我对芒果过敏", "times": ["21:56"]},
             {"user": "我对芒果过敏", "times": ["21:57"]},
             {"user": "我花生也过敏", "times": ["21:58"]}]
    ok, bad = Q.select_qq(cands, DAY, known="- 用户对芒果过敏 (2026-11-01)\n")
    assert [o["text"] for o in ok] == ["用户花生也过敏"]
    assert [b["why"] for b in bad][:2] == ["重复", "重复"]


# ------------------------------------------------------------------ 写候选
def test_capture_dry_run_writes_nothing(tmp_path):
    vault = _vault(tmp_path)
    mem = new_memory(vault)
    rec = asyncio.run(Q.capture(mem, _cfg(vault), DAY, dry_run=True))
    assert rec["day"] == DAY and rec["entries"] >= 4
    assert rec["accepted"], "这一天有值得记的话"
    assert rec["written"] == [] and cand_text(vault) == ""


def test_capture_writes_candidates_and_is_idempotent(tmp_path):
    vault = _vault(tmp_path)
    mem = new_memory(vault)
    rec = asyncio.run(Q.capture(mem, _cfg(vault), DAY))
    assert rec["written"] and "用户对芒果过敏" in rec["written"]
    text = cand_text(vault)
    assert "#私聊" in text and "#候选" in text and DAY in text
    assert text.count("用户对芒果过敏") == 1
    again = asyncio.run(Q.capture(mem, _cfg(vault), DAY))
    assert again["written"] == [], "第二次跑不该重复写"
    assert cand_text(vault).count("用户对芒果过敏") == 1


def test_capture_respects_daily_max(tmp_path):
    vault = _vault(tmp_path)
    mem = new_memory(vault)
    mem.daily_max = 1
    rec = asyncio.run(Q.capture(mem, _cfg(vault), DAY))
    assert len(rec["written"]) == 1 and rec["skipped"]


def test_enabled_env_overrides_config(monkeypatch):
    monkeypatch.delenv(Q.ENV_ON, raising=False)
    assert Q.enabled({}) is False
    assert Q.enabled({"memory": {"owner_qq": {"enabled": True}}}) is True
    monkeypatch.setenv(Q.ENV_ON, "off")
    assert Q.enabled({"memory": {"owner_qq": {"enabled": True}}}) is False, "环境变量优先"
    monkeypatch.setenv(Q.ENV_ON, "on")
    assert Q.enabled({}) is True

# ------------------------------------------------------------------ 夜间报告的输入体检
from wm import night as N  # noqa: E402


def test_health_of_says_why_there_was_nothing(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    rep = {"counts": {"turns": 0, "facts": 0}}
    health = N.health_of(DAY, rep, logs_dir=logs, cfg={})
    assert health["desktop_turns"] == 0 and health["digest_records"] == 0
    assert health["qq_on"] is False
    assert any("都没有你说的话" in r for r in health["reasons"])


def test_health_of_counts_qq_and_digest(tmp_path):
    vault = _vault(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "digest-20261118.jsonl").write_text('{"written": ["用户对芒果过敏"]}\n', encoding="utf-8")
    cfg = _cfg(vault)
    cfg["memory"]["owner_qq"] = {"enabled": True}
    health = N.health_of(DAY, {"counts": {"turns": 0, "facts": 1}}, logs_dir=logs, cfg=cfg)
    assert health["digest_records"] == 1 and health["digest_facts"] == 1
    assert health["qq_on"] is True
    assert health["qq_owner_lines"] >= 4 and health["qq_candidates"] >= 1


def test_night_capture_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv(Q.ENV_ON, raising=False)
    vault = _vault(tmp_path)
    assert N._capture_owner_qq(_cfg(vault), DAY, dry_run=True) is None


def test_night_capture_on_writes_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv(Q.ENV_ON, "on")
    vault = _vault(tmp_path)
    rec = N._capture_owner_qq(_cfg(vault), DAY, dry_run=False)
    assert rec and rec["written"]
    assert "用户对芒果过敏" in cand_text(vault)

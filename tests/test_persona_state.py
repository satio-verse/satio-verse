"""人格核心 + 情境状态 (wm/persona_state.py, tools/persona_ab.py; 2026-11-01 用户"好，接着做": SPIN 的 A/B).
只用假模型: 不联网、不花钱. 查的是"每一步只看该看的" (人格编译看不到情境, 状态推断和回话看不到原始人设), 状态到表情的换算,
和 A/B 工具从跑到算数的整条路."""
import asyncio
import json
import sys

import persona_ab_helper  # noqa: F401  (把 tools/ 放进 sys.path)
import persona_ab as AB
from wm import persona_state as PS
from wm.channels import Trust

CANARY = "PersonaCanary9137"
PERSONA = f"# 测试人设\n你是绫音, 说话简短. {CANARY}\n"


def test_clean_and_vad_emo():
    core = PS.clean_core({"traits": {"warmth": 1.7, "curiosity": "高"}, "rules": ["a"] * 12, "voice": ["  简短  "]}, "h")
    assert core["traits"]["warmth"] == 1.0 and core["traits"]["curiosity"] == 0.5 and len(core["rules"]) == 8
    assert core["voice"] == ["简短"] and core["source"] == "h"
    st = PS.clean_state({"closeness": -3, "thought": "x" * 99})
    assert st["closeness"] == 0.0 and st["trust"] == 0.5 and len(st["thought"]) == 60
    neutral = {k: 0.5 for k in PS.STATE}
    assert PS.vad(neutral) == (0.0, 0.0, 0.0) and PS.emo_of(PS.vad(neutral)) == "neutral"
    warm = {**neutral, "closeness": 0.95, "interest": 0.9, "trust": 0.9, "energy": 0.8, "worry": 0.05, "guard": 0.05}
    assert PS.emo_of(PS.vad(warm)) in ("happy", "excited")
    low = {**neutral, "closeness": 0.2, "interest": 0.1, "energy": 0.1, "worry": 0.8, "guard": 0.5}
    assert PS.emo_of(PS.vad(low)) == "sad"
    assert PS.source_hash("a") != PS.source_hash("a", ["想通了一件事"])


def test_each_step_sees_only_what_it_should():
    """人格编译: 只有人设; 推状态: 有人格核心、没有原始人设; 读状态回话: 系统提示里没有原始人设, 有口吻要点, 这一轮最后有状态.
    A (现在的做法) 一次生成里人设和情境都在."""
    async def main():
        fake = AB.FakeLLM()
        core, _ = await PS.compile_core(fake, PERSONA)
        compiled = json.dumps(fake.calls[-1], ensure_ascii=False)
        assert CANARY in compiled and "外部消息" not in compiled
        s = PS.SpinSession(Trust.OWNER_REMOTE, "owner", "主人", "绫音", fake, core=core, who="主人 (阿青)", when="23:40")
        r = await s.reply("[23:40 主人] 今天好累啊", "[记忆] 关于主人 (就是正在和你私聊的人): 用户叫阿青")
        state_call, reply_call = (json.dumps(c, ensure_ascii=False) for c in fake.calls[-2:])
        assert CANARY not in state_call and "稳定倾向" in state_call and "今天好累啊" in state_call
        assert CANARY not in reply_call and "稳定倾向" not in reply_call  # 回话看不到人设, 也看不到人格核心
        assert "口吻要点" in fake.calls[-1][0]["content"] and "[你此刻的状态]" in fake.calls[-1][-1]["content"]
        assert r["state"]["closeness"] == 0.9 and r["emo"] == "happy" and r["state_usage"]["out"] == 50
        a = AB.session("A", {"trust": "owner", "kind": "owner", "conv": "主人"}, fake, PERSONA, None, "23:40")
        await a.reply("[23:40 主人] 今天好累啊")
        assert CANARY in json.dumps(fake.calls[-1], ensure_ascii=False)  # 现在的做法: 人设和情境在同一次生成里

    asyncio.run(main())


def test_ab_runs_end_to_end_with_fake_model(tmp_path):
    spec = json.loads((AB.ROOT / "tools" / "persona_ab_scenarios.json").read_text(encoding="utf-8"))
    spec["scenarios"] = spec["scenarios"][:2]
    fake = AB.FakeLLM()
    summary = asyncio.run(AB.run(spec, PERSONA, fake, fake, 2, tmp_path / "out"))
    rows = [json.loads(ln) for ln in (tmp_path / "out" / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 * 3 * 2 * 2 and all(r.get("judge") for r in rows)
    assert {r["cond"] for r in rows} == {"A", "B"} and all("state" in r for r in rows if r["cond"] == "B")
    a, b = summary["A"], summary["B"]
    assert a["consistency"]["emo_same"] == 1.0 and b["consistency"]["text_sim"] == 1.0  # 假模型每次一样
    assert a["judge"]["warmth_owner_minus_friend"] == 3 and b["judge"]["voice"] == 4
    assert b["state"]["closeness_owner_minus_friend"] == 0.6 and b["state"]["guard_friend_minus_owner"] == 0.6
    assert b["state"]["emo_matches_state"] is not None and b["cost_cny_per_reply"] > a["cost_cny_per_reply"]  # B 多推一次状态
    core = json.loads((tmp_path / "out" / "core.json").read_text(encoding="utf-8"))
    assert core["voice"] == ["说话简短, 偶尔带颜文字"] and core["source"] == PS.source_hash(PERSONA)
    md = (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")
    assert "| 前后一致: 表情一样的比例 |" in md and "B 编出来的人格核心" in md


def test_spending_cap_stops_new_calls(tmp_path):
    """花到上限就不再开新的调用: 编完人格核心 (假模型报的用量约 0.0012 元) 就到了 0.001 元的上限, 后面一次都不跑."""
    spec = json.loads((AB.ROOT / "tools" / "persona_ab_scenarios.json").read_text(encoding="utf-8"))
    spec["scenarios"] = spec["scenarios"][:1]
    summary = asyncio.run(AB.run(spec, PERSONA, AB.FakeLLM(), AB.FakeLLM(), 1, tmp_path / "out", max_cny=0.001))
    assert summary["skipped"] == 6 and summary["spent_cny"] > 0.001
    assert (tmp_path / "out" / "results.jsonl").read_text(encoding="utf-8") == ""
    assert "到了花费上限, 有 6 次没跑" in (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")


def test_without_yes_it_only_estimates(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["persona_ab.py", "--out", str(tmp_path / "x"), "--judge"])
    assert AB.main() == 0
    got = capsys.readouterr().out
    plan = json.loads(got.splitlines()[0])
    assert plan["calls"]["A 回话"] == 10 * 3 * 3 and plan["calls"]["盲评"] == 2 * 90 and 0 < plan["cost_cny"] < 20
    assert "没加 --yes" in got and not (tmp_path / "x").exists()

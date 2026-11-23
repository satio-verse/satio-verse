"""S3 文字层（主动搭话「说 / 等 / 沉默」）的验收规格 —— 2026-11-18 起红, 2026-11-19 转绿.

设计见 docs/roadmap/s3-proactive-speak-gate.md。
ADR-0026：主动说的话在「统计 / 记忆 / 会话历史」三处不再被当成一轮。
ADR-0007：语气标记和 emo 一样，历史里存、展示文本里不出现。

顶层不再挂 pytestmark（红规格已实现）。第 4 条原先想测 `GuestSession.nudge(..., proactive=True)`，
但 guest.py 是 standing 禁改项、nudge 也没有 proactive 参数 —— 改成直接测 manager 侧的
`ChannelManager._trim_proactive_history`：主动那一轮的会话历史由管理器裁掉，guest.py 一个字不动。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- (a) 说 / 等 / 沉默
def test_speak_gate_defaults_off_and_can_be_turned_on() -> None:
    """新开关默认关: 不写 config/default.json 也必须是 False, 线上行为不变; 显式开才是 True."""
    from wm.channels.manager import parse_conf

    conf, _ = parse_conf({"proactive": {"enabled": True}})
    assert conf["proactive"]["speak_gate"] is False
    conf, _ = parse_conf({"proactive": {"enabled": True, "speak_gate": True}})
    assert conf["proactive"]["speak_gate"] is True
    # 只有显式的布尔 True 才算开: "true" / 1 这类手滑值不能把关打开
    for sloppy in ("true", 1, "yes"):
        conf, _ = parse_conf({"proactive": {"enabled": True, "speak_gate": sloppy}})
        assert conf["proactive"]["speak_gate"] is False


def test_speak_gate_is_not_written_into_default_config() -> None:
    """开关只活在代码缺省值里: config/default.json 不许多出 speak_gate 这一项."""
    raw = (ROOT / "config" / "default.json").read_text(encoding="utf-8")
    assert "speak_gate" not in raw
    if raw.lstrip().startswith("{"):
        json.loads(raw)  # 顺手证明还是合法 JSON


def test_speak_choice_three_ways() -> None:
    """提醒的回答 -> (动作, 说的话): 【说】剥头、单独【等】是等、空是沉默; 无标记照旧是"说"."""
    from wm.channels.manager import speak_choice

    assert speak_choice("【说】 今天云很好看") == ("say", "今天云很好看")
    assert speak_choice("【说】: 今天云很好看") == ("say", "今天云很好看")
    assert speak_choice("【等】") == ("wait", "")
    assert speak_choice("【等】一会儿再说") == ("wait", "")
    assert speak_choice("") == ("silent", "")
    assert speak_choice("   ") == ("silent", "")
    assert speak_choice("【说】") == ("silent", "")  # 说了个开头却没内容: 当沉默, 不让她发个空消息
    assert speak_choice("直接说的内容") == ("say", "直接说的内容")  # 没标记 = 旧行为, 整段发


# --------------------------------------------------------------------------- (b) 不算一轮: 统计
def test_ledger_counts_cost_but_not_a_turn_for_proactive() -> None:
    """主动搭话的模型调用: 轮数不加, 但 token 和花费照记 (额度不能被主动说话偷掉)."""
    from wm import ledger as L

    local = L._empty()
    L._add(local, {"t": 0.0, "who": "owner", "ch": "qq", "provider": "local", "model": "local-model",
                   "hit": 10, "miss": 90, "out": 20, "think": 0, "cost": 0.0, "proactive": True})
    assert local["local"]["turns"] == 0
    assert local["local"]["miss"] == 90 and local["local"]["out"] == 20

    cloud = L._empty()
    L._add(cloud, {"t": 0.0, "who": "owner", "ch": "qq", "provider": "deepseek", "model": "x",
                   "hit": 10, "miss": 90, "out": 20, "cost": 0.001, "proactive": True})
    assert cloud["turns"] == 0
    assert cloud["cost"] == 0.001 and cloud["miss"] == 90


def test_ledger_proactive_skips_peak_and_by_whom_turns_but_keeps_money() -> None:
    """peak_turns / by_who / by_ch / by_model 的轮数也一起跳过; 钱、token、命中照记."""
    from wm import ledger as L

    agg = L._empty()
    L._add(agg, {"t": 0.0, "who": "owner", "ch": "qq", "provider": "deepseek", "model": "x", "hit": 1, "miss": 2,
                 "out": 3, "think": 4, "cost": 0.5, "tier": "peak", "proactive": True})
    assert agg["turns"] == 0 and agg["peak_turns"] == 0
    assert agg["cost"] == 0.5 and agg["hit"] == 1 and agg["miss"] == 2 and agg["out"] == 3 and agg["think"] == 4
    assert agg["by_who"]["owner"]["turns"] == 0 and agg["by_who"]["owner"]["cost"] == 0.5
    assert agg["by_who"]["owner"]["tokens"] == 6
    assert agg["by_ch"]["qq"]["turns"] == 0
    assert agg["by_model"]["deepseek:x"]["turns"] == 0 and agg["by_model"]["deepseek:x"]["cost"] == 0.5

    # 普通一轮 (没有 proactive) 照旧算轮数: 不能把正常对话漏掉
    L._add(agg, {"t": 1.0, "who": "owner", "ch": "qq", "provider": "deepseek", "model": "x", "hit": 1, "miss": 2,
                 "out": 3, "cost": 0.5, "tier": "peak"})
    assert agg["turns"] == 1 and agg["peak_turns"] == 1
    assert agg["by_who"]["owner"]["turns"] == 1 and agg["by_model"]["deepseek:x"]["turns"] == 1


def test_ledger_record_defaults_without_proactive_flag(tmp_path) -> None:
    """proactive 缺省不写进记录: 平常的调用里没有这个字段; 在 proactive() 上下文里才标 True, 出上下文自动恢复."""
    from wm import ledger as L

    led = L.Ledger(tmp_path, conf={})
    usage = {"hit": 1, "miss": 2, "out": 3}
    outside = led.record("owner", "local", "local-model", usage)
    assert outside is not None and "proactive" not in outside

    with L.proactive(True):
        inside = led.record("owner", "local", "local-model", usage)
        assert inside is not None and inside["proactive"] is True

    after = led.record("owner", "local", "local-model", usage)
    assert after is not None and "proactive" not in after


def test_ledger_proactive_context_resets_on_exception() -> None:
    """上下文管理器出错也要把标记恢复, 不能把后面的请求都染成"不算一轮"."""
    from wm import ledger as L

    with pytest.raises(RuntimeError):
        with L.proactive(True):
            assert L._proactive.get() is True
            raise RuntimeError("boom")
    assert L._proactive.get() is False


# --------------------------------------------------------------------------- (b) 不算一轮: 记忆
def test_memory_proactive_observe_does_not_add_a_turn(tmp_path) -> None:
    """桌宠侧主动开口若走 observe(proactive=True): 关系.md 的 turns 不变."""
    from wm.memory import Memory

    m = Memory({"vault": str(tmp_path), "character": "小流"})
    m.init()
    m.start_session()
    m.observe("我叫小明", "记住啦", [], None)
    rel = tmp_path / "小流" / "关系.md"
    assert "turns: 1" in rel.read_text(encoding="utf-8")
    m.observe("今天好累", "早点休息", [], None, proactive=True)
    assert "turns: 1" in rel.read_text(encoding="utf-8")


def test_memory_proactive_observe_keeps_last_seen_frozen(tmp_path) -> None:
    """proactive=True 也不许动 last_seen / familiarity: 隔天的主动说话不能把"上次见面"刷成今天."""
    from wm.memory import Memory

    day1 = datetime(2026, 1, 1, 10, 0)
    day2 = datetime(2026, 1, 2, 10, 0)
    m = Memory({"vault": str(tmp_path), "character": "小流"})
    m.clock = lambda: day1
    m.init()
    m.start_session()
    m.observe("我叫小明", "记住啦", [], None)
    rel = tmp_path / "小流" / "关系.md"
    before = rel.read_text(encoding="utf-8")
    assert "last_seen: 2026-01-01 10:00" in before
    turns_before = [ln for ln in before.splitlines() if ln.startswith("turns:")]
    familiarity_before = [ln for ln in before.splitlines() if ln.startswith("familiarity:")]

    m.clock = lambda: day2
    m.observe("今天好累", "早点休息", [], None, proactive=True)
    after = rel.read_text(encoding="utf-8")
    assert "last_seen: 2026-01-01 10:00" in after
    assert "last_seen: 2026-01-02 10:00" not in after
    assert [ln for ln in after.splitlines() if ln.startswith("turns:")] == turns_before
    assert [ln for ln in after.splitlines() if ln.startswith("familiarity:")] == familiarity_before


# --------------------------------------------------------------------------- (b) 不算一轮: 会话历史
class _FakeSession:
    """假 guest 会话: nudge 一次就往 history 末尾追加一条 (和 guest.GuestSession._ask 一样), 顺便记下账本标记."""

    def __init__(self, reply: str = "在的呀。", seed: int = 1) -> None:
        self.history: list[tuple[str, str]] = [(f"问{i}", f"答{i}") for i in range(seed)]
        self.reply = reply
        self.notes: list[str] = []
        self.proactive_flags: list[bool] = []

    async def nudge(self, note: str, memory: str | None = None) -> dict:
        from wm import ledger as L

        self.notes.append(note)
        self.proactive_flags.append(L._proactive.get())
        self.history.append((note, self.reply))
        return {"emo": "neutral", "reply": self.reply, "tokens": 5, "cost": 5,
                "usage": {"hit": 1, "miss": 2, "out": 3}}


def test_proactive_history_trim_function_boundaries() -> None:
    """替代原第 4 条 (guest.py 禁改): 管理器侧的裁剪函数自己守边界, 越界 / 负数 / 空历史都不乱删."""
    from wm.channels.manager import ChannelManager

    def conv_with(hist):
        s = _FakeSession()
        s.history = hist
        c = object.__new__(type("C", (), {}))
        c.session = s
        return c

    hist = [("q1", "a1"), ("q2", "a2"), ("q3", "a3")]
    conv = conv_with(hist)
    assert ChannelManager._trim_proactive_history(conv, 1) == 2
    assert conv.session.history == [("q1", "a1")]

    # before 等于现有条数 / 比现有条数还大 / 负数: 都不动
    assert ChannelManager._trim_proactive_history(conv, 1) == 0
    assert ChannelManager._trim_proactive_history(conv, 9) == 0
    assert ChannelManager._trim_proactive_history(conv, -3) == 0
    assert conv.session.history == [("q1", "a1")]

    # 空历史
    empty = conv_with([])
    assert ChannelManager._trim_proactive_history(empty, 0) == 0
    assert ChannelManager._trim_proactive_history(empty, -1) == 0
    assert empty.session.history == []

    # 没有 session / session 没有 history: 静默返回 0, 不抛
    assert ChannelManager._trim_proactive_history(object(), 0) == 0
    class _NoHist:
        pass
    assert ChannelManager._trim_proactive_history(_NoHist(), 0) == 0

    # 模拟 guest._ask 追加一条后裁回、以及"历史上限裁掉开头"那种长度不增的情况
    conv2 = conv_with([("q1", "a1"), ("q2", "a2")])
    conv2.session.history.append(("主动", "她说的"))
    assert ChannelManager._trim_proactive_history(conv2, 2) == 1
    assert conv2.session.history == [("q1", "a1"), ("q2", "a2")]
    assert ChannelManager._trim_proactive_history(conv2, 5) == 0  # 长度不增时宁可不删


def test_reach_trims_proactive_history_and_marks_ledger_flag() -> None:
    """manager._reach 真跑一遍: 模型调用带 proactive 账本标记, 发完把她这句从会话历史里裁掉."""
    from wm.channels.manager import ChannelManager

    m, seen = _reach_manager(gate=False, reply="在的呀。", seed=2)
    conv = _FakeConv(m, _FakeSession(reply="在的呀。", seed=2))
    decision = asyncio.run(ChannelManager._reach(m, datetime.now(), conv))
    assert decision == "sent"
    assert conv.session.proactive_flags == [True]  # 模型调用发生在 proactive 账本上下文里
    assert len(conv.session.history) == 2  # guest._ask 追加的那条被裁掉了
    assert conv.session.history == [("问0", "答0"), ("问1", "答1")]


# --------------------------------------------------------------------------- (a) gate 关着: 零变化
def test_reach_gate_off_passes_markers_through_untouched() -> None:
    """开关关着: 【等】不解析、原样交给 _deliver 去发 (旧行为), 提醒里也不许多出三选一的话."""
    from wm.channels.manager import ChannelManager

    m, seen = _reach_manager(gate=False, reply="【等】")
    conv = _FakeConv(m, _FakeSession(reply="【等】"))
    decision = asyncio.run(ChannelManager._reach(m, datetime.now(), conv))
    assert decision == "sent"
    assert seen["reply"] == "【等】"  # 原样发了出去
    note = conv.session.notes[0]
    assert "三选一" not in note and "【说】" not in note and "【等】" not in note
    assert m.pro_count == 1 and m.unanswered == 1  # 旧行为: 发了就算一次
    m, seen = _reach_manager(gate=False, reply="平常的一句")
    conv = _FakeConv(m, _FakeSession(reply="平常的一句"))
    assert asyncio.run(ChannelManager._reach(m, datetime.now(), conv)) == "sent"
    assert seen["reply"] == "平常的一句"


# --------------------------------------------------------------------------- (a) gate 开着: 三种回答
def test_reach_gate_on_say_strips_header_and_instructs_three_ways() -> None:
    """开关开着 + 【说】: 提醒里多出三选一说明, 发出去的是剥掉标记的正文, 发了才计数."""
    from wm.channels.manager import ChannelManager

    m, seen = _reach_manager(gate=True, reply="【说】 今天云很好看")
    conv = _FakeConv(m, _FakeSession(reply="【说】 今天云很好看"))
    assert asyncio.run(ChannelManager._reach(m, datetime.now(), conv)) == "sent"
    assert seen["reply"] == "今天云很好看"
    note = conv.session.notes[0]
    assert "三选一" in note and "【说】" in note and "【等】" in note
    assert "【语气:轻】" in note  # 语气标记的提示也只在 gate 开时给
    assert m.pro_count == 1 and m.unanswered == 1 and m.her_last_owner > 0


def test_reach_gate_on_wait_sends_nothing_and_keeps_counters() -> None:
    """开关开着 + 【等】: 不发、不计数 (不占 max_per_day, 也不记成主人没回), 会话历史照裁."""
    from wm.channels.manager import ChannelManager

    m, seen = _reach_manager(gate=True, reply="【等】")
    conv = _FakeConv(m, _FakeSession(reply="【等】", seed=2))
    decision = asyncio.run(ChannelManager._reach(m, datetime.now(), conv))
    assert decision == "wait"
    assert seen == {}  # _deliver 一次都没调用
    assert m.pro_count == 0 and m.unanswered == 0 and m.her_last_owner == 0.0
    assert len(conv.session.history) == 2  # 还是裁回去了
    assert conv.session.proactive_flags == [True]
    assert m.trace_seen == ["wait"]  # trace 里记的是"等", 面板上看得到她的判断


def test_reach_gate_on_silent_and_empty_say_send_nothing() -> None:
    """开关开着 + 空 reply / 光一个【说】: 都算沉默, 不发, 也不计数."""
    from wm.channels.manager import ChannelManager

    for reply in ("", "   ", "【说】"):
        m, seen = _reach_manager(gate=True, reply=reply)
        conv = _FakeConv(m, _FakeSession(reply=reply))
        assert asyncio.run(ChannelManager._reach(m, datetime.now(), conv)) == "silent"
        assert seen["reply"] == ""
        assert m.pro_count == 0 and m.unanswered == 0


# --------------------------------------------------------------------------- (a) 等 / 沉默: 下次什么时候再问
def _maybe_manager(decision: str, *, idle_h: float = 3.0):
    """假 manager 跑真 _maybe_proactive: 只把 _reach 换成固定决定, 别的判断照旧."""
    from wm.channels.manager import ChannelManager, parse_conf

    conf, _ = parse_conf({"owner_id": 10000, "proactive": {"enabled": True, "idle_h": idle_h}})
    m = object.__new__(ChannelManager)
    m.conf, m.channel, m.cloud = conf, "qq", None
    m._life_read = lambda *a, **k: None
    m.pro_day, m.pro_count = "", 0
    m.life_day, m.life_count, m.share_count, m.video_count = "", 0, 0, 0
    m.pro_retry_at, m.life_last = 0.0, 0.0
    m.owner_seen, m.her_last_owner, m.unanswered = 0.0, 0.0, 0
    m._conv = lambda key: _FakeConv(m, _FakeSession())
    m._qq_up = lambda: True
    m._unrestricted = lambda: True

    async def noop(*a, **k):
        return None

    async def reach(now, conv):
        return decision

    m._live, m._reach = noop, reach
    return m


def test_maybe_proactive_wait_pushes_retry_ten_minutes() -> None:
    """她选了等: 10 分钟后可以再问一次; 今天的次数和"主人没回"的计数都不动."""
    from wm.channels.manager import PRO_WAIT_S, ChannelManager

    m = _maybe_manager("wait")
    now = datetime.now()
    t = now.timestamp()
    m.owner_seen = t - 4 * 3600
    asyncio.run(ChannelManager._maybe_proactive(m, now))
    assert PRO_WAIT_S == 600.0
    assert m.pro_retry_at == pytest.approx(t + 600.0)
    assert m.pro_count == 0 and m.unanswered == 0 and m.her_last_owner == 0.0


def test_maybe_proactive_silent_keeps_old_retry_policy() -> None:
    """沉默 / 没发出去: 按现状退 min(3600, idle_h*3600) —— 3 小时档就是 1 小时, 半小时档就是半小时."""
    from wm.channels.manager import ChannelManager

    now = datetime.now()
    t = now.timestamp()
    for decision, idle_h, expect in (("silent", 3.0, 3600.0), ("send_failed", 3.0, 3600.0),
                                     ("silent", 0.5, 1800.0)):
        m = _maybe_manager(decision, idle_h=idle_h)
        m.owner_seen = t - 4 * 3600
        asyncio.run(ChannelManager._maybe_proactive(m, now))
        assert m.pro_retry_at == pytest.approx(t + min(3600.0, idle_h * 3600)), (decision, idle_h)
        assert m.pro_retry_at == pytest.approx(t + expect)


def test_maybe_proactive_sent_does_not_push_retry() -> None:
    """真发出去 / 存草稿了: 不推 retry (照旧由 her_last_owner 和 idle_h 决定下次)."""
    from wm.channels.manager import ChannelManager

    for decision in ("sent", "draft"):
        m = _maybe_manager(decision)
        now = datetime.now()
        m.owner_seen = now.timestamp() - 4 * 3600
        asyncio.run(ChannelManager._maybe_proactive(m, now))
        assert m.pro_retry_at == 0.0


# --------------------------------------------------------------------------- (c) 语气标记
def test_tone_marker_stays_in_history_but_not_in_display_text() -> None:
    """历史那一侧原样留标记 (clean_out 不删【…】); 展示/发送那一侧先 strip_tone 剥掉."""
    from wm.channels import outbound as O

    raw = "早呀，今天也一起加油。【语气:轻】"
    assert "【语气:轻】" in O.clean_out(raw)  # GuestSession._ask 存进 history 的就是这一版
    shown, tone = O.strip_tone(raw)
    assert tone == "轻"
    assert "语气" not in shown and shown == "早呀，今天也一起加油。"


def test_strip_tone_handles_multiple_fullwidth_and_absent_markers() -> None:
    """多标记取第一个语气、全角冒号也认、没标记原文不动 (身份不变, 不是新字符串)."""
    from wm.channels.outbound import strip_tone

    assert strip_tone("没有标记的一句话") == ("没有标记的一句话", None)
    s = "没有标记的一句话"
    same, tone = strip_tone(s)
    assert same is s and tone is None

    assert strip_tone("a【语气:轻】b【语气：凶】c") == ("abc", "轻")
    assert strip_tone("【语气：轻】") == ("", "轻")
    assert strip_tone("") == ("", None)
    assert strip_tone(None) == ("", None)
    assert strip_tone("【语气:轻】前半句【语气:软】后半句")[0] == "前半句后半句"


def test_deliver_strips_tone_before_sending_and_records_it() -> None:
    """真 _deliver: 发出去的分段里没有【语气…】, rec 里留下 tone 供 trace / 界面用."""
    from wm.channels.manager import ChannelManager, parse_conf
    from wm.channels.outbound import RateLimiter

    conf, _ = parse_conf({"owner_id": 10000, "proactive": {"enabled": True}})
    m = object.__new__(ChannelManager)
    m.conf, m.cloud = conf, None
    m.limiter = RateLimiter()
    m._unrestricted = lambda: True
    m._trace = lambda *a, **k: None
    sent: dict = {}

    async def send(conv, parts, quote):
        sent["parts"] = list(parts)
        return True

    m._send = send
    rec = {"time": "x", "channel": "qq", "conv": "private:10000", "kind": "proactive"}
    r = {"emo": "neutral", "reply": "早呀，今天也一起加油。【语气:轻】", "tokens": 5, "cost": 5,
         "usage": {"hit": 1, "miss": 2, "out": 3}}
    conv = _FakeConv(m, _FakeSession())
    assert asyncio.run(ChannelManager._deliver(m, conv, r, rec, 0.0)) == "sent"
    assert rec["tone"] == "轻"
    joined = "".join(sent["parts"])
    assert "语气" not in joined and "早呀" in joined
    assert "【" not in joined


# --------------------------------------------------------------------------- 测试辅助
def _reach_manager(gate: bool, reply: str, seed: int = 1):
    """假 manager 跑真 _reach: 只换掉 _deliver / _owner_memory / _trace, 其余 (账本、裁剪、计数) 都是真代码."""
    from wm.channels.manager import ChannelManager, parse_conf

    conf, _ = parse_conf({"owner_id": 10000, "proactive": {"enabled": True, "speak_gate": gate}})
    m = object.__new__(ChannelManager)
    m.conf, m.channel, m.cloud = conf, "qq", None
    m._local_lock = None
    m.owner_seen, m.her_last_owner = 0.0, 0.0
    m.unanswered, m.pro_count = 0, 0
    m.ask_queue = []
    m.tracelog = None
    m.trace_seen: list[str] = []
    m._owner_memory = lambda **kw: None
    m._trace = lambda rec, decision, t0, **extra: m.trace_seen.append(decision)
    seen: dict = {}

    async def deliver(conv, r, rec, t0):
        seen["reply"] = r["reply"]
        seen["rec"] = dict(rec)
        return "sent" if r["reply"].strip() else "silent"

    m._deliver = deliver
    return m, seen


class _FakeConv:
    """假 Conv: 只带 _reach / _maybe_proactive 会碰的字段."""

    def __init__(self, manager, session: _FakeSession) -> None:
        from wm.channels import Trust

        self.key = ("private", 10000)
        self.trust = Trust.OWNER_REMOTE
        self.session = session
        self.timer = None
        self.task = None

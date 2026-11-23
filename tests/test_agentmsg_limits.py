"""Agent 互发消息的额度本 (wm/agentmsg/limits.py、scan.py、tools/agent_msg_ctl.py): 三个上限由宿主硬算硬拦, 绕不过去.
引擎层, 不起外部命令行 (多进程那条只起几个很小的 Python 抢额度). 计划: backups/tool4-agent-msg/计划.md 第 4、6 节."""
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from wm import agentmsg as AM
from wm.agentmsg import AgentMsgError, scan
from wm.agentmsg import limits as LM
from wm.agentmsg.limits import Limits

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self, day="2026-11-15"):
        self.day = day

    def __call__(self):
        return self.day


def make(tmp_path, clock=None, **conf):
    lim = Limits(tmp_path / "agent-msg", conf, today=clock or Clock())
    lim.set_flag("enabled", True)
    return lim


def reserve(lim, chain="s1|claude", target="claude", **kw):
    return lim.reserve(chain, target, **kw)


def code(lim, *a, **kw):
    with pytest.raises(AgentMsgError) as e:
        reserve(lim, *a, **kw)
    return e.value.code


# ---------------------------------------------------------------- 默认数字和夹紧
def test_defaults_are_the_numbers_the_owner_decided():
    cfg = AM.settings(None)
    assert (cfg["max_passes"], cfg["daily_messages"], cfg["daily_cny"]) == (6, 20, 2.0)
    clamped = AM.settings({"max_passes": 9999, "daily_messages": 0, "daily_cny": "很多", "per_call_cny": -1})
    assert clamped["max_passes"] == 20 and clamped["daily_messages"] == 1 and clamped["daily_cny"] == 2.0 and clamped["per_call_cny"] == 0.01


# ---------------------------------------------------------------- 总开关: 默认关, 坏了也关
def test_off_by_default_and_nothing_is_written(tmp_path):
    lim = Limits(tmp_path / "agent-msg", {})
    assert lim.enabled() is False
    assert code(lim) == "off"
    assert not (tmp_path / "agent-msg").exists()          # 一个字节都没写, 连目录都没建


@pytest.mark.parametrize("bad", ["{坏的", "[]", '{"v": 1, "enabled": "true"}', '{"v": 1, "enabled": 1}', "", "null"])
def test_fail_closed_when_state_is_broken_or_wrong_type(tmp_path, bad):
    lim = make(tmp_path)
    assert lim.enabled() is True
    lim.path.write_text(bad, encoding="utf-8")
    assert lim.enabled() is False
    assert code(lim) in ("off", "state")
    lim.path.write_text(json.dumps({**LM._fresh(), "enabled": True, "msgs": "三"}), encoding="utf-8")   # 别的字段类型不对也算坏
    assert lim.enabled() is False


def test_deleted_state_means_off(tmp_path):
    lim = make(tmp_path)
    lim.path.unlink()
    assert lim.enabled() is False and code(lim) == "off"


def test_off_takes_effect_at_once_and_codex_is_a_separate_switch(tmp_path):
    lim = make(tmp_path)
    assert code(lim, "s1|codex", "codex") == "target_off"       # 总开关开着, Codex 一档默认没开
    lim.set_flag("codex", True)
    reserve(lim, "s1|codex", "codex")
    lim.set_flag("enabled", False)
    assert lim.enabled() is False and code(lim) == "off"


# ---------------------------------------------------------------- 上限 1: 来回 6 次
def test_seventh_round_trip_is_refused_and_only_the_owner_resumes_that_chain(tmp_path):
    lim = make(tmp_path)
    for _ in range(6):
        lim.settle(reserve(lim), 0.01)
    assert code(lim) == "chain"
    lim.settle(reserve(lim, "s2|claude"), 0.0)                  # 另一个会话 = 另一条链 (用户另开了会话)
    lim.set_flag("codex", True)
    lim.settle(reserve(lim, "s1|codex", "codex"), 0.0)          # 同一个会话找另一个对方 = 另一条链
    assert lim.resume("s1|claude") == 1
    lim.settle(reserve(lim), 0.0)
    assert lim.snapshot()["chains"]["s1|claude"]["passes"] == 1


def test_a_new_thread_does_not_reset_the_chain(tmp_path):
    lim = make(tmp_path)
    for i in range(6):
        lim.settle(reserve(lim, thread=f"T{i}"), 0.01)           # 每次都换一个对话号, 想躲掉按对话算的上限
    assert code(lim, thread="T99") == "chain"


# ---------------------------------------------------------------- 上限 2: 每天 20 条
def test_the_twenty_first_message_of_the_day_is_refused(tmp_path):
    lim = make(tmp_path, per_call_cny=0.01)
    for i in range(20):
        lim.settle(reserve(lim, f"s{i}|claude"), 0.0)           # 每次一条新链, 来回上限碰不到
    assert code(lim, "s99|claude") == "daily_count"
    assert lim.snapshot()["messages"] == {"used": 20, "limit": 20, "left": 0}


# ---------------------------------------------------------------- 上限 3: 每天 2 元 (预占 + 已花)
def test_money_is_reserved_before_the_call_and_settled_after(tmp_path):
    lim = make(tmp_path, max_inflight=8)
    tickets = [reserve(lim, f"s{i}|claude") for i in range(4)]   # 4 × 0.5 = 2.0, 刚好
    assert code(lim, "s9|claude") == "daily_cny"
    lim.settle(tickets[0], 0.1)                                  # 实际只花了 0.1: 空出 0.4
    t = reserve(lim, "s9|claude", cny=0.4)
    assert t.cny == 0.4 and code(lim, "s10|claude") == "daily_cny"
    snap = lim.snapshot()["cny"]
    assert snap["spent"] == 0.1 and snap["in_flight"] == 1.9 and snap["left"] == 0.0


def test_unknown_or_failed_cost_is_charged_in_full(tmp_path):
    lim = make(tmp_path)
    lim.settle(reserve(lim), None)                               # 解析不出 / 超时 / 崩溃
    assert lim.snapshot()["cny"]["spent"] == 0.5
    lim.settle(reserve(lim, "s2|claude"), 1.2)                   # 实际比预占多: 记实际, 之后的预占检查会因此拒发
    assert lim.snapshot()["cny"]["spent"] == 1.7 and code(lim, "s3|claude") == "daily_cny"


def test_a_ticket_can_be_settled_only_once(tmp_path):
    lim = make(tmp_path)
    t = reserve(lim)
    assert lim.settle(t, 0.2) is True and lim.settle(t, 0.2) is False and lim.settle(t, None) is False
    assert lim.snapshot()["cny"]["spent"] == 0.2


def test_checks_run_in_a_fixed_order_and_refusals_cost_nothing(tmp_path):
    lim = make(tmp_path, daily_messages=1, daily_cny=0.1)
    lim.settle(reserve(lim, cny=0.01), 0.01)
    before = lim.path.read_bytes()
    assert code(lim, cny=0.5) == "daily_count"                  # 条数和钱都超了: 先报条数
    assert lim.path.read_bytes() == before                      # 拒发的不占任何额度, 连文件都没动
    lim2 = make(tmp_path / "b", daily_cny=0.1)
    assert code(lim2, cny=0.5) == "daily_cny"
    lim3 = make(tmp_path / "c", max_passes=1)
    lim3.settle(reserve(lim3), 0.0)
    assert code(lim3) == "chain"


def test_in_flight_limits_per_thread_and_global(tmp_path):
    lim = make(tmp_path, max_inflight=2)
    reserve(lim, "s1|claude", thread="T1")
    assert code(lim, "s2|claude", thread="T1") == "inflight"      # 同一个对话上一条还没回话
    reserve(lim, "s3|claude", thread="T2")
    assert code(lim, "s4|claude", thread="T3") == "inflight"      # 全局最多 2 条在飞


# ---------------------------------------------------------------- 绕不过去: 改时间
def test_changing_the_clock_does_not_reopen_today(tmp_path):
    clock = Clock("2026-11-15")
    lim = make(tmp_path, clock, daily_messages=3, per_call_cny=0.01)
    for i in range(3):
        lim.settle(reserve(lim, f"s{i}|claude"), 0.0)
    assert code(lim, "s9|claude") == "daily_count"
    clock.day = "2026-11-14"                                     # 往回拨: 有效日期不变, 计数不清
    assert code(lim, "s9|claude") == "daily_count"
    assert lim.snapshot()["day"] == "2026-11-15"
    clock.day = "2026-11-16"                                     # 往后拨 (或真到了第二天): 每天的数清零, 来回数不清
    lim.settle(reserve(lim, "s0|claude"), 0.0)
    snap = lim.snapshot()
    assert snap["day"] == "2026-11-16" and snap["messages"]["used"] == 1
    assert snap["chains"]["s0|claude"]["passes"] == 2          # 来回数不按天清
    clock.day = "2026-11-15"                                     # 再拨回去: 昨天不会复活
    assert lim.snapshot()["messages"]["used"] == 1 and lim.snapshot()["day"] == "2026-11-16"
    for i in range(2):
        lim.settle(reserve(lim, f"t{i}|claude"), 0.0)
    assert code(lim, "t9|claude") == "daily_count"


def test_notice_once_per_code_per_day(tmp_path):
    clock = Clock()
    lim = make(tmp_path, clock)
    assert lim.notice_once("chain") is True and lim.notice_once("chain") is False and lim.notice_once("daily_cny") is True
    clock.day = "2026-11-16"
    assert lim.notice_once("chain") is True


# ---------------------------------------------------------------- 绕不过去: 换会话、并发、多进程
def test_a_new_session_does_not_get_a_new_daily_budget(tmp_path):
    lim = make(tmp_path, daily_messages=4, per_call_cny=0.01)
    for sid in ("a", "b", "c", "d"):
        lim.settle(reserve(lim, f"{sid}|claude"), 0.0)           # 每个会话各自的链是新的, 但条数是全局的
    assert code(lim, "e|claude") == "daily_count"
    other = Limits(lim.home, {"daily_messages": 4}, today=Clock())  # 另一个 Limits 对象 (比如重启后) 读的是同一份盘
    assert code(other, "f|claude") == "daily_count"


def _admit_with_retry(lim, i, out):
    while True:
        try:
            lim.settle(lim.reserve(f"s{i}|claude", "claude"), 0.0)
            out.append(1)
            return
        except AgentMsgError as exc:
            if exc.code == "inflight":
                continue
            out.append(0)
            return


def test_threads_cannot_get_more_than_the_remaining_budget(tmp_path):
    lim = make(tmp_path, per_call_cny=0.01, max_inflight=8)
    out = []
    ts = [threading.Thread(target=_admit_with_retry, args=(lim, i, out)) for i in range(40)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(out) == 20 and lim.snapshot()["messages"]["used"] == 20


def test_asyncio_gather_cannot_get_more_than_the_remaining_budget(tmp_path):
    import asyncio
    lim = make(tmp_path, per_call_cny=0.01, max_inflight=8)

    async def main():
        out = []
        await asyncio.gather(*[asyncio.to_thread(_admit_with_retry, lim, i, out) for i in range(40)])
        return out

    assert sum(asyncio.run(main())) == 20


CHILD = r"""
import sys
sys.path.insert(0, sys.argv[1])
from wm.agentmsg import AgentMsgError
from wm.agentmsg.limits import Limits
lim = Limits(sys.argv[2], {"per_call_cny": 0.01, "max_inflight": 8})
ok = 0
for i in range(12):
    while True:
        try:
            lim.settle(lim.reserve(sys.argv[3] + str(i) + "|claude", "claude"), 0.0)
            ok += 1
        except AgentMsgError as exc:
            if exc.code == "inflight":
                continue
        break
print(ok)
"""


def test_separate_processes_share_one_budget(tmp_path):
    lim = make(tmp_path, per_call_cny=0.01, max_inflight=8)
    procs = [subprocess.Popen([sys.executable, "-X", "utf8", "-c", CHILD, str(ROOT), str(lim.home), f"p{k}-"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for k in range(4)]
    got = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        got.append(int(out.strip()))
    assert sum(got) == 20 and lim.snapshot()["messages"]["used"] == 20


# ---------------------------------------------------------------- 重启后没结算的凭条
def test_tickets_left_by_a_dead_process_are_charged_in_full(tmp_path, monkeypatch):
    lim = make(tmp_path)
    reserve(lim)
    monkeypatch.setattr(LM, "BOOT", "another-process")
    assert lim.recover() == 1
    snap = lim.snapshot()
    assert snap["cny"]["spent"] == 0.5 and snap["in_flight"] == 0


def test_old_tickets_expire_into_spent(tmp_path, monkeypatch):
    lim = make(tmp_path, timeout_s=10.0)
    reserve(lim)
    real = LM.time.time
    monkeypatch.setattr(LM.time, "time", lambda: real() + 10 * 2 + 121)
    reserve(lim, "s2|claude")
    assert lim.snapshot()["cny"]["spent"] == 0.5


# ---------------------------------------------------------------- 发出去之前的检查
def test_message_scan(tmp_path):
    assert scan.check_message("  请看一下 foo.py 里 bar 为什么返回 None \r\n") == "请看一下 foo.py 里 bar 为什么返回 None"
    for bad in ["", "   ", None, 5, "字" * 4001]:
        with pytest.raises(AgentMsgError):
            scan.check_message(bad)
    protected = [Path("C:/Users/x/.ssh"), Path("C:/Users/x/.worldmodel/vault")]
    for bad in ["看 C:\\Users\\x\\.ssh\\id_rsa", "cat c:/users/x/.SSH/config", "读 C:/Users/x/.worldmodel/vault/a.json"]:
        with pytest.raises(AgentMsgError) as e:
            scan.check_message(bad, protected)
        assert "受保护" in str(e.value)
    for secret in ["key sk-abcdefghijklmnop1234", "ghp_" + "a" * 30, "-----BEGIN RSA PRIVATE KEY-----", "Authorization: Bearer abcdefghijklmnop12345",
                   "password = hunter2hunter2", "AKIAABCDEFGHIJKLMNOP"]:
        with pytest.raises(AgentMsgError):
            scan.check_message(secret)


# ---------------------------------------------------------------- 主人的开关脚本: 要真终端
def test_ctl_refuses_to_turn_on_without_a_terminal(tmp_path):
    home = tmp_path / "state"
    ctl = [sys.executable, "-X", "utf8", str(ROOT / "tools" / "agent_msg_ctl.py"), "--home", str(home)]
    on = subprocess.run([*ctl, "on"], capture_output=True, text=True, input="确认\n", timeout=60)   # 管道进去的 "确认" 不算
    assert on.returncode != 0 and "真终端" in (on.stderr + on.stdout)
    assert not home.exists()
    resume = subprocess.run([*ctl, "resume"], capture_output=True, text=True, input="确认\n", timeout=60)
    assert resume.returncode != 0 and not home.exists()
    codex = subprocess.run([*ctl, "codex", "on"], capture_output=True, text=True, input="确认\n", timeout=60)
    assert codex.returncode != 0 and not home.exists()
    status = subprocess.run([*ctl, "status"], capture_output=True, text=True, timeout=60)
    assert status.returncode == 0 and json.loads(status.stdout)["enabled"] is False


def test_ctl_turns_on_in_a_real_terminal_and_off_anywhere(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "tools"))
    import agent_msg_ctl as ctl
    home = tmp_path / "state"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: "不确认")
    with pytest.raises(SystemExit):
        ctl.main(["--home", str(home), "on"])
    assert not home.exists()
    monkeypatch.setattr("builtins.input", lambda *_: "确认")
    ctl.main(["--home", str(home), "on"])
    assert Limits(home).enabled() is True
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    ctl.main(["--home", str(home), "off"])                       # 关不要求终端
    assert Limits(home).enabled() is False

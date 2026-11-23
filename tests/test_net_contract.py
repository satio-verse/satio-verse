"""网络插件·干跑 (3/3): 契约测试 —— 照《网络动作接口 v0.2》第 11 节逐条, 对着假助手 (tests/net_fake_helper.py, 在进程里顶替提权的代办) 和真的看护方进程
(假核心 tests/net_fake_core.py). 真代办以后也要过同一套 (换掉 make_rig 里的 FakeHelper 即可).

第 11 节每一条对应的测试:
- 每个动作的正常路径: test_net_start_normal_path_*、test_net_restore_normal_path_*; 看护方三个动作 (net.stop、net.status、心跳) 在 test_net_start_normal_path_* 里
  都走了一遍, 状态机和通道的细节在 tests/test_net_guardian.py.
- 每个错误码至少一条: 每个错误码对应的测试上标着 @covers_agent / @covers_guardian (代办回的、看护方回的); 最后一条测试用 ast 读这些标记,
  核对契约第 5 节里代办 (19 个) 和看护方 (6 个) 会回的码一个不漏 (宿主的三个 E_NOT_PERMITTED、E_CARD_DECLINED、E_UAC_DECLINED 不归这两个管).
- 演练: test_without_elevation_*、test_dry_run_*. (第 2 轮评审补: 演练、没提权时前后整个受保护目录的快照逐字节一样, 审计也不例外: test_without_elevation_*、
  test_dry_run_answers_with_the_would_list_and_touches_nothing.)
- 提权一侧的审计 (契约第 8 节〔补 3〕, 第 12 条): 提权的代办每条请求都记一行, 除非确定是演练; 没过第 1 节检查的请求也记, 动作写 rejected, 别的空着:
  test_every_request_that_is_not_known_to_be_a_dry_run_is_recorded_and_the_undecodable_ones_as_rejected; 各项只在通过了自己那一步的检查以后才写:
  test_a_token_whose_shape_is_wrong_*、test_a_token_with_a_good_shape_is_written_*、test_the_summary_is_written_only_after_the_parameter_whitelist_passes_*,
  E_BUSY 那一行 (令牌两项空着、参数指纹照写) 在 test_a_request_that_comes_while_another_*.
- 令牌: test_token_*.
- 参数: test_params_*.
- 字符表: test_charset_*.
- 环境变量 (和非 ASCII 进程名一路不乱): test_core_sees_*.
- 看护: test_killing_the_guardian_*、test_a_silent_host_*、test_the_core_exiting_*; 到时限、心跳断、核心退了各一条用假时钟的在 tests/test_net_guardian.py (状态机和进程内主循环).
- 恢复: test_net_restore_normal_path_* (真的看护方) 和 tests/test_net_guardian.py 的 test_restore_repeated_gives_the_same_result_in_all_three_states.
- 核心输出不落盘: test_the_cores_output_is_never_written_to_disk.
- 第 2 轮评审补的四条 (契约第 11 节〔补 1〕):
  - 会话通道只连不说话的连接 5 秒后被断开、之后别的客户端连得上: tests/test_net_guardian.py 的 test_the_real_guardian_drops_a_silent_connection_after_about_five_seconds_*
    (真进程、默认 5 秒) 和 test_a_connection_that_never_sends_a_request_is_dropped_* 等 (进程内, 零点几秒);
  - 交接 (三个句柄种类不对、进程不在那个作业里 -> 看护方退出码 2、代办 E_INTERNAL、核心没有留下): test_a_wrong_handover_makes_the_guardian_exit_with_2_*;
  - 第二次真起: 以前会话留下的子目录被清掉: test_a_second_session_after_the_first_one_stopped_starts_*、test_a_dry_run_deletes_no_old_run_directory_*;
  - 假"网卡"标记随核心进程消失: test_the_fake_adapter_marker_vanishes_when_the_core_is_killed_*.
- 第 2 轮评审追加的第 8–11 条 (契约第 11 节〔补 2〕):
  - 解析 (数、嵌套、BOM、行尾、长度最先查): tests/test_net_protocol.py 里一批 (带小数点、带指数、超范围、9 层、BOM 各一条, 请求这一行和 params_json 里各一遍; 恰好 2^53-1
    和恰好 8 层不被拒; 行尾 CRLF 照常认), 这里再走一遍整条路: test_a_number_nesting_or_bom_*、test_params_json_with_a_bad_number_*;
  - null 和类型: test_a_real_request_must_carry_params_json_null_means_left_out_* ("token": null 回 E_TOKEN_MISSING、"dry_run": null 回 E_BAD_REQUEST、要令牌的请求必须带 params_json);
    代办收到 net.stop、net.status、heartbeat 回 E_BAD_REQUEST: test_the_session_channel_actions_sent_to_the_agent_*;
  - 令牌: 三个字符串表外的字符、超长、带换行的被拒 (test_token_strings_outside_the_allowed_writing_*); 时间恰好等于 issued_at、expires_at 都有效, 向下取整, 晚一秒过期
    (test_token_expired 和 protocol 里的);
  - 锁 (提权检查 -> 拿锁 -> 令牌四查): 另一条真请求正在处理时回 E_BUSY、这张令牌之后还能用、演练不被挡、没提权的真请求回 E_NOT_ELEVATED:
    test_a_request_that_comes_while_another_*、test_the_same_token_handed_to_two_agents_*、test_with_the_lock_held_*;
  - 演练应答的 summary 是第 6 节末尾的例子: test_the_dry_run_summary_is_the_example_*;
  - 看护方 (在 tests/test_net_guardian.py): 自查是提权的 -> 作业被结束、退出码 4; 作业对象少一项设置 -> 退出码 2; --audit 只给测试用, 默认位置只在 main() 里算.

- 单独起的 net.restore (契约 4.4 末尾〔补 2〕的 7 步; 第 4 轮): 看护方不在、但有残留 (或什么都没有) 时, 由代办走卡片、令牌、提权框来恢复. 本文件末尾那一块 (test_restore_*、
  test_a_recorded_*、test_an_unreadable_*、test_an_adapter_that_does_not_*): 四种状态各自重复发、结果稳定 (从没开过、已经没了、还开着看护方也在、残留: 用 handover_fault="keep_job_handle"
  造, 强杀看护方后核心不跟着没); 进程号被重用不误杀; 记录读不了跳过; 网卡到时间不消失; 令牌、没提权、锁、确认窗开关 (默认不弹)、演练、审计各项.
  第 4 轮补 (组长发现的一处会误报"干净"的毛病): 三步里有 failed 就不清 runs/ (记录、标记都留着), 三步都没有 failed 才清, 重复恢复的结果才稳定:
  test_an_adapter_that_does_not_go_away_in_time_* (连发两遍两遍都是 failed)、test_a_restore_that_could_not_end_the_core_keeps_the_records_* (结束进程失败, 恢复正常后再发 done、done、done).

全部对假的: 不提权、不开 TUN、不碰系统的网络设置; 看护方只用 --fake-adapter-marker 加 --fake-flush-log 起 (假助手里写死), 一次也不会真的清系统的 DNS 缓存 (恢复里的那一步也是假的:
往 data_dir/agent-flush.log 追加一行);
起看护方的命令行一定带 --audit (真起的时候不传), 看护方进程的家目录也换成假的: 测试里绝对不往真的家目录写.
"""
from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from net_fake_helper import (CANCEL, CLEAN_ENV_NAMES, NO_WINDOW, TIMEOUT, WOULD_START, Built, Rig, channel, covers_agent, request_line, restore_line,
                             sha256_file, skip_unrelated_autouse_fixtures, snapshot, start_line, wait_for)

from wm.net import config_gen, protocol, win32
from wm.net.recipe import Recipe, Upstream

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="假助手起的是 Windows 的作业对象和命名管道")
skip_unrelated_autouse_fixtures(globals())      # 盖掉 conftest.py 里和网络无关的 autouse 夹具 (见 net_fake_helper.py)

RECIPE = Recipe("NIKKE", ("nikke.exe",), Upstream("127.0.0.1", 1082), 240)
UNICODE_RECIPE = Recipe("鸣潮", ("原神.exe", "Wuthering Waves.exe"), Upstream("127.0.0.1", 1082), 240)
EVIL_ENV = ("CLASH_CONFIG_STRING", "CLASH_CONFIG_FILE", "CLASH_HOME_DIR", "CLASH_OVERRIDE_EXTERNAL_CONTROLLER", "CLASH_OVERRIDE_SECRET",
            "CLASH_POST_UP", "CLASH_POST_DOWN", "HTTP_PROXY")      # mihomo 会拿它们当启动参数的默认值 (契约 4.1 第 8 步)


@pytest.fixture(autouse=True)
def _never_flush_the_real_dns_cache(monkeypatch):
    """红线: 这一步里任何时候都不许真的清系统的 DNS 缓存 (看护方进程用 --fake-flush-log 起, 这里再把进程内的底层调用堵上)."""
    def refuse() -> bool:
        raise AssertionError("测试里不许真的清系统的 DNS 缓存")

    monkeypatch.setattr(win32, "_dns_flush_resolver_cache", refuse)


@pytest.fixture
def make_rig(tmp_path):
    """造 Rig (假助手加它的目录); 收尾时不管成败都把起过的进程结束掉, 并核对没有留下进程和管道."""
    rigs: list[Rig] = []

    def make(**kwargs) -> Rig:
        rig = Rig(tmp_path / f"rig{len(rigs)}", **kwargs)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.close()
    for rig in rigs:
        assert not rig.ledger.alive(), "测试起的进程没有收干净"
        assert not rig.leftover_pipes(), "测试起的会话管道没有消失"


def build(rig: Rig, **kwargs) -> Built:
    return start_line(RECIPE, now=rig.clock(), **kwargs)


def code_of(reply: dict) -> str:
    assert reply["ok"] is False, reply
    return reply["error"]["code"]


def with_flags(built: Built, **over: object) -> bytes:
    """同一条请求, 只改顶层的某几项 (比如 dry_run): 令牌和参数原样."""
    return protocol.encode({**built.request, **over})


def good_params() -> dict:
    return json.loads(protocol.start_params(RECIPE, core_id="mihomo", core_version="v1.19.34"))


def params_text(obj: dict) -> str:
    return config_gen.render(obj)


def edited(path: str, value: object = None, *, drop: bool = False) -> str:
    obj = good_params()
    node = obj
    *head, last = path.split(".")
    for key in head:
        node = node[key]
    if drop:
        del node[last]
    else:
        node[last] = value
    return params_text(obj)


def runs_in(rig: Rig) -> list[Path]:
    return sorted((rig.protected / "runs").glob("*")) if (rig.protected / "runs").exists() else []


def start_session(rig: Rig, **kwargs) -> dict:
    reply = rig.send(build(rig, **kwargs))
    assert reply["ok"] is True, reply
    return reply["result"]


# ====================================================================================================================
# 每个动作的正常路径
# ====================================================================================================================

def test_net_start_normal_path_gives_the_reply_of_section_4_1_and_the_guardian_actions_work(make_rig):
    rig = make_rig()
    built = build(rig)
    reply = rig.send(built)
    assert reply["ok"] is True and reply["id"] == "r1" and reply["v"] == 1
    result = reply["result"]
    session = rig.helper.session
    assert set(result) == {"session", "state", "started_at", "deadline_at", "core", "adapter", "config_sha256"}
    assert protocol.valid_session(result["session"]) and result["state"] == "running"
    assert isinstance(result["started_at"], int) and result["deadline_at"] == result["started_at"] + RECIPE.max_minutes * 60
    assert result["core"] == {"id": "mihomo", "version": "v1.19.34", "pid": session.core_pid} and session.core_pid > 0
    assert result["adapter"] == "WM-Ayane" and result["config_sha256"] == json.loads(built.params_json)["config_sha256"]
    assert session.pipe == protocol.pipe_name(result["session"])
    # 看护方的心跳、状态、停
    assert channel(session.pipe, "heartbeat")["result"] == {"state": "running"}
    status = channel(session.pipe, "net.status", {})["result"]
    assert status["state"] == "running" and status["session"] == result["session"] and status["core_alive"] is True
    assert status["adapter_present"] is True and status["started_at"] == result["started_at"]
    stop = channel(session.pipe, "net.stop", {"session": result["session"], "reason": "game_exited"}, rid="bye")
    assert stop["ok"] is True and stop["id"] == "bye"
    assert set(stop["result"]) == {"session", "state", "reason", "adapter_gone", "dns_flushed", "stopped_at"}
    assert stop["result"]["session"] == result["session"] and stop["result"]["state"] == "stopped" and stop["result"]["reason"] == "game_exited"
    assert stop["result"]["adapter_gone"] is True and stop["result"]["dns_flushed"] is True and isinstance(stop["result"]["stopped_at"], int)
    assert session.guardian.wait(30) == 0 and session.core.poll() is not None                # 看护方应答后自己退出, 核心已经没了
    assert rig.helper.flush_log.read_text(encoding="utf-8").count("flush") == 1 and not win32.pipe_exists(session.pipe)
    [record] = [json.loads(text) for text in rig.helper.guardian_audit.read_text(encoding="utf-8").splitlines()]
    assert record["reason"] == "game_exited" and record["session"] == result["session"] and record["state"] == "stopped"
    assert set(record) == {"session", "started_at", "stopped_at", "reason", "state", "adapter_gone", "dns_flushed", "core_exit_code", "matches"}
    assert record["core_exit_code"] is None                                                # 核心不是自己退的: null
    # 令牌用掉了、提权的真请求审计记了一行 (没有 dry_run 字段)
    assert built.token["id"] in json.loads(rig.helper.used_path.read_text(encoding="utf-8"))
    [line] = rig.helper.audit_lines()
    assert line["result"] == "ok" and line["action"] == "net.start" and line["token_id"] == built.token["id"]
    assert set(line) == {"at", "action", "token_id", "requested_by", "params_sha256", "summary", "result"}
    assert not rig.helper.fake_home.exists()                          # 看护方写审计用的是命令行里的 --audit, 没有去算默认位置 (它的"家目录"里什么也没有)


def test_net_restore_normal_path_on_a_running_session(make_rig):
    rig = make_rig()
    start_session(rig)
    session = rig.helper.session
    got = channel(session.pipe, "net.restore", {})
    assert got["ok"] is True and got["result"] == {"steps": [
        {"step": "kill_core", "result": "done", "detail": ""}, {"step": "adapter_gone", "result": "done", "detail": ""},
        {"step": "flush_dns", "result": "done", "detail": ""}], "clean": True, "reboot_advised": False}
    assert session.guardian.wait(30) == 0 and session.core.poll() is not None
    assert rig.helper.flush_log.read_text(encoding="utf-8").count("flush") == 1
    [record] = [json.loads(text) for text in rig.helper.guardian_audit.read_text(encoding="utf-8").splitlines()]
    assert record["reason"] == "restore" and record["state"] == "stopped"


# ====================================================================================================================
# 演练
# ====================================================================================================================

@covers_agent("E_NOT_ELEVATED")
def test_without_elevation_only_dry_runs_are_accepted_and_nothing_at_all_is_written(make_rig):
    """没提权的代办只接受演练; 它什么都不往受保护的目录写——拒掉的非演练请求 (E_NOT_ELEVATED)、解不出来的请求、演练, 都不写, 连审计也没有
    (真的代办没提权时本来也写不了那个目录; 这些记录由宿主在普通权限这边记)."""
    rig = make_rig(elevated=False)
    before = snapshot(rig.protected)
    reply = rig.send(build(rig))                                         # 有令牌、不是演练
    assert code_of(reply) == "E_NOT_ELEVATED"
    assert code_of(rig.send(build(rig, token=False))) == "E_NOT_ELEVATED"          # 没令牌的真请求: 第 1 步就拒了
    assert code_of(rig.send(b"not json\n")) == "E_BAD_REQUEST"           # 解不出来的请求
    assert code_of(rig.send(b'{"v":2,"id":"r7","action":"net.start"}\n')) == "E_VERSION"
    dry = rig.send(build(rig, dry_run=True, token=False))                # 演练照常 (没提权、没令牌)
    assert dry["ok"] is True and dry["result"]["dry_run"] is True
    assert rig.helper.started_processes == 0 and rig.helper.confirm_calls == []
    assert snapshot(rig.protected) == before == {}                       # 受保护的目录前后一模一样 (本来就是空的)
    assert not rig.helper.audit_path.exists() and not rig.helper.used_path.exists() and rig.helper.audit_lines() == []


@pytest.mark.parametrize("elevated", [True, False])
def test_dry_run_answers_with_the_would_list_and_touches_nothing(make_rig, elevated):
    rig = make_rig(elevated=elevated)
    before = snapshot(rig.protected)
    source_before = sha256_file(rig.core_source)
    reply = rig.send(build(rig, dry_run=True, token=False))
    assert reply["ok"] is True and reply["result"]["dry_run"] is True
    assert reply["result"]["would"] == ["copy_core", "write_config", "start_core", "wait_adapter", "hand_over"] == WOULD_START
    assert reply["result"]["summary"] == {                                  # 契约第 6 节末尾的例子的形状: upstream、core、heartbeat 是对象, 不是拼出来的字符串
        "processes": ["nikke.exe"], "upstream": {"host": "127.0.0.1", "port": 1082}, "max_minutes": 240,
        "core": {"id": "mihomo", "version": "v1.19.34"}, "heartbeat": {"interval_s": 5, "grace_s": 30},
        "config_sha256": json.loads(build(rig).params_json)["config_sha256"]}
    assert rig.helper.started_processes == 0 and rig.ledger.procs == [] and rig.helper.confirm_calls == []      # 不起进程、不弹确认窗
    assert snapshot(rig.protected) == before                                                                     # 不拷文件、不写受保护的目录, 审计也不写: 前后逐字节一样
    assert not (rig.protected / "runs").exists() and not rig.helper.used_path.exists() and not rig.helper.audit_path.exists()
    assert rig.helper.audit_lines() == [] and sha256_file(rig.core_source) == source_before


def test_the_dry_run_summary_is_the_example_in_section_6_of_the_contract(make_rig):
    """演练应答的 summary 照契约第 6 节末尾的例子 (各项的类型以它为准): 键的先后、upstream 是 host 加 port、core 是 id 加 version、heartbeat 是 interval_s 加 grace_s;
    进程名是去重以后的 (契约第 7 节向量 4: NIKKE.exe、nikke.exe 只剩第一次出现的写法)."""
    rig = make_rig()
    result = rig.send(start_line(RECIPE, now=rig.clock(), dry_run=True, token=False))["result"]
    assert result == {"dry_run": True, "would": ["copy_core", "write_config", "start_core", "wait_adapter", "hand_over"],
                      "summary": {"processes": ["nikke.exe"], "upstream": {"host": "127.0.0.1", "port": 1082}, "max_minutes": 240,
                                  "core": {"id": "mihomo", "version": "v1.19.34"}, "heartbeat": {"interval_s": 5, "grace_s": 30},
                                  "config_sha256": "9e6bf780287212dbc5aa6206377cd4e6feba051ae8d4bb1d4e2e89190a012510"}}   # 契约里那个例子 (向量 1 的指纹)
    assert list(result["summary"]) == ["processes", "upstream", "max_minutes", "core", "heartbeat", "config_sha256"]
    assert type(result["summary"]["upstream"]["port"]) is int and type(result["summary"]["heartbeat"]["interval_s"]) is int
    dup = Recipe("NIKKE", ("NIKKE.exe", "nikke.exe"), Upstream("127.0.0.1", 65535), 240)
    summary = rig.send(start_line(dup, now=rig.clock(), dry_run=True, token=False))["result"]["summary"]
    assert summary["processes"] == ["NIKKE.exe"] and summary["upstream"]["port"] == 65535 and summary["config_sha256"].startswith("fee4505398cc60bd")


def test_a_dry_run_with_a_token_checks_it_but_does_not_use_it_up(make_rig):
    rig = make_rig(confirm=lambda summary: CANCEL)
    built = build(rig)
    dry = rig.send(with_flags(built, dry_run=True))                      # 同一张令牌、同一段参数, 只把 dry_run 改成 true
    assert dry["ok"] is True and "would" in dry["result"]
    assert not rig.helper.used_path.exists()
    assert code_of(rig.send(built)) == "E_USER_DECLINED"                 # 令牌还能真用 (是确认窗里点了取消, 不是 E_TOKEN_USED)
    assert code_of(rig.send(built)) == "E_TOKEN_USED"                    # 这一回才算用掉
    bad = build(rig, token_overrides={"params_sha256": "0" * 64})
    assert code_of(rig.send(with_flags(bad, dry_run=True))) == "E_TOKEN_MISMATCH"        # 演练里带了令牌也照查


AUDIT_KEYS = {"at", "action", "token_id", "requested_by", "params_sha256", "summary", "result"}


def test_every_request_that_is_not_known_to_be_a_dry_run_is_recorded_and_the_undecodable_ones_as_rejected(make_rig):
    """契约第 8 节〔补 3〕: 提权的代办每条请求都记一行, 除非它确定是演练. 没过第 1 节检查的请求 (解不出来、版本不对、动作不认识: 在解请求那一步就拒了的) 也记, 动作写
    rejected, 令牌号、谁申请的、参数指纹和摘要空着, 结果写错误码; 解得出来、确定是演练的不记 (演练不写受保护的目录). 没提权的代办照旧什么都不写 (test_without_elevation_*)."""
    rig = make_rig()
    refused = [(b"not json\n", "E_BAD_REQUEST"), (b"[]\n", "E_BAD_REQUEST"), (b'{"v":2,"id":"r7","action":"net.start"}\n', "E_VERSION"),
               (b'{"v":1,"id":"a","action":"net.start","bogus":1}\n', "E_BAD_REQUEST"), (request_line("net.explode"), "E_UNKNOWN_ACTION"),
               (b"x" * 70_000 + b"\n", "E_BAD_REQUEST"), (bytes([0xEF, 0xBB, 0xBF]) + build(rig).line, "E_BAD_REQUEST")]
    for raw, code in refused:
        assert code_of(rig.send(raw)) == code
    rig.send(build(rig, dry_run=True, token=False))                      # 解得出来、确定是演练: 不记
    rig.send(build(rig, dry_run=True))
    lines = rig.helper.audit_lines()
    assert [(line["action"], line["result"]) for line in lines] == [("rejected", code) for _, code in refused]
    for line in lines:
        assert set(line) == AUDIT_KEYS and isinstance(line["at"], int)
        assert line["token_id"] is None and line["requested_by"] is None and line["params_sha256"] is None and line["summary"] is None
    built = build(rig, token=False)                                      # 解得出来的真请求照旧每条一行 (没带令牌的也记, 带着代办自己算的参数指纹)
    assert code_of(rig.send(built)) == "E_TOKEN_MISSING"
    last = rig.helper.audit_lines()[-1]
    assert (last["action"], last["result"]) == ("net.start", "E_TOKEN_MISSING") and last["token_id"] is None and last["requested_by"] is None
    assert last["params_sha256"] == protocol.params_fingerprint(built.params_json) and last["summary"] is None
    assert len(rig.helper.audit_lines()) == len(refused) + 1 and all("dry_run" not in line for line in rig.helper.audit_lines())


@pytest.mark.parametrize("how", ["extra_field", "id_not_hex", "requested_by_with_newline", "plugin_uppercase", "plugin_owner", "plugin_ayane", "issuer_with_space",
                                 "sha_not_hex", "ttl_too_long", "missing_field"])
def test_a_token_whose_shape_is_wrong_leaves_the_token_fields_of_the_audit_line_empty(make_rig, how):
    """契约第 8 节〔补 3〕: 令牌号和谁申请的要令牌的形状通过以后才写 (没核对过的值不往账上写, 不然形状不对的令牌会把一长串没查过的字符写进审计). 形状不对的回
    E_TOKEN_MISMATCH, 这两项空着; 参数指纹是代办自己算的, 照写; 摘要还没到那一步, 空着; 令牌没作废."""
    rig = make_rig()
    now = int(rig.clock())
    overrides = {"extra_field": {"extra": 1}, "id_not_hex": {"id": "zz" * 16}, "requested_by_with_newline": {"requested_by": "owner\n" + "x" * 300},
                 "plugin_uppercase": {"plugin": "Net"}, "plugin_owner": {"plugin": "owner"}, "plugin_ayane": {"plugin": "ayane"},
                 "issuer_with_space": {"issuer": "a b"}, "sha_not_hex": {"params_sha256": "g" * 64},
                 "ttl_too_long": {"expires_at": now + 121}}.get(how)
    base = build(rig, token_overrides=overrides)
    if how == "missing_field":
        token = {key: value for key, value in base.token.items() if key != "issuer"}
        reply = rig.send(request_line("net.start", params_json=base.params_json, token=token))
    else:
        reply = rig.send(base)
    assert code_of(reply) == "E_TOKEN_MISMATCH"
    line = rig.helper.audit_lines()[-1]
    assert (line["action"], line["result"]) == ("net.start", "E_TOKEN_MISMATCH") and set(line) == AUDIT_KEYS
    assert line["token_id"] is None and line["requested_by"] is None
    assert line["params_sha256"] == protocol.params_fingerprint(base.params_json) and line["summary"] is None
    assert not rig.helper.used_path.exists()


@pytest.mark.parametrize("how, code", [("expired", "E_TOKEN_EXPIRED"), ("used", "E_TOKEN_USED"), ("wrong_fingerprint", "E_TOKEN_MISMATCH"),
                                       ("wrong_action", "E_TOKEN_MISMATCH")])
def test_a_token_with_a_good_shape_is_written_to_the_audit_even_when_a_later_check_fails(make_rig, how, code):
    """形状过了、后面查出过期、用过或对不上 (指纹、动作) 的令牌, 审计里照样有令牌号和谁申请的; 参数指纹照写; 摘要要参数白名单通过以后才写, 令牌四查在白名单之前, 还没有."""
    rig = make_rig(confirm=lambda summary: CANCEL)
    if how == "expired":
        built = build(rig)
        rig.clock.advance(121)
    elif how == "used":
        built = build(rig)
        assert code_of(rig.send(built)) == "E_USER_DECLINED"
    elif how == "wrong_fingerprint":
        built = build(rig, token_overrides={"params_sha256": "0" * 64})
    else:
        built = build(rig, token_overrides={"action": "net.restore"})
    assert code_of(rig.send(built)) == code
    line = rig.helper.audit_lines()[-1]
    assert (line["action"], line["result"]) == ("net.start", code)
    assert line["token_id"] == built.token["id"] and line["requested_by"] == "owner"
    assert line["params_sha256"] == protocol.params_fingerprint(built.params_json) and line["summary"] is None


def test_the_summary_is_written_only_after_the_parameter_whitelist_passes_and_the_token_id_in_lower_case(make_rig):
    rig = make_rig(confirm=lambda summary: CANCEL)
    assert code_of(real_or_dry(rig, edited("upstream.port", 0), False)) == "E_PARAMS"
    line = rig.helper.audit_lines()[-1]
    assert line["result"] == "E_PARAMS" and line["token_id"] and line["requested_by"] == "owner" and line["params_sha256"]
    assert line["summary"] is None                                       # 白名单没过: 摘要空着
    built = build(rig, token_overrides={"id": "AB12" * 8})               # 令牌号大写写的: 记和比之前一律转成小写
    assert code_of(rig.send(built)) == "E_USER_DECLINED"                 # 白名单过了, 确认窗里点了取消
    line = rig.helper.audit_lines()[-1]
    assert line["summary"] == {"processes": ["nikke.exe"], "upstream_port": 1082, "max_minutes": 240, "core": "mihomo v1.19.34"}
    assert line["token_id"] == "ab12" * 8 and line["result"] == "E_USER_DECLINED"


def test_a_second_session_after_the_first_one_stopped_starts_and_the_old_run_directory_goes(make_rig):
    rig = make_rig()
    first = start_session(rig)
    session1 = rig.helper.session
    old_run = session1.run_dir
    assert channel(session1.pipe, "net.stop", {"session": first["session"], "reason": "user"})["ok"] is True
    assert session1.guardian.wait(30) == 0 and session1.core.wait(30) is not None
    assert old_run.exists()                                              # 停了以后旧的运行目录还在 (没人清它)
    second = start_session(rig)                                          # 第二次成功 (旧的核心没了, 标记跟着没了, 不会被当成"网卡还在")
    session2 = rig.helper.session
    assert second["session"] != first["session"] and session2.run_dir != old_run
    assert runs_in(rig) == [session2.run_dir] and not old_run.exists()   # 真请求过了第 6 步, runs/ 下旧的子目录都清掉, 只剩新的那一个


def test_a_dry_run_deletes_no_old_run_directory_but_a_real_request_that_passed_step_6_does(make_rig):
    rig = make_rig(confirm=lambda summary: CANCEL)
    stale = rig.protected / "runs" / "deadbeef"
    (stale / "work").mkdir(parents=True)
    (stale / "work" / "config.yaml").write_text("old", encoding="utf-8")
    before = snapshot(rig.protected)
    assert rig.send(build(rig, dry_run=True, token=False))["ok"] is True
    assert snapshot(rig.protected) == before and stale.exists()          # 演练什么都不删
    rig.core_source.write_bytes(rig.core_source.read_bytes() + b"\n# tampered core\n")
    assert code_of(rig.send(build(rig))) == "E_CORE_HASH"               # 过不了第 5 步: 还没到第 6 步, 旧目录还在
    assert stale.exists() and runs_in(rig) == [stale]
    rig.core_source.write_bytes(rig.core_source.read_bytes().replace(b"\n# tampered core\n", b""))
    assert code_of(rig.send(build(rig))) == "E_USER_DECLINED"            # 过了第 6 步 (确认窗里点了取消): 旧目录清掉了, 这次拷进去的也清了
    assert not stale.exists() and runs_in(rig) == []


def test_the_fake_adapter_marker_vanishes_when_the_core_is_killed_and_the_helper_then_sees_no_session(make_rig):
    """假"网卡"标记是核心进程持有的 (关句柄就删): 强杀核心后 2 秒内消失, 助手不用读它、不用查进程号就知道网卡没了 (进程号会被重用)."""
    rig = make_rig()
    start_session(rig)
    session = rig.helper.session
    marker = session.work_dir / "fake-adapter-WM-Ayane"
    assert marker.exists() and rig.helper._running() is True
    session.core.kill()
    wait_for(lambda: not marker.exists(), 2.0, "核心被强杀以后标记 2 秒内消失")
    assert session.guardian.wait(60) == 0                                # 看护方发现核心退了, 收尾后退出
    other = type(rig.helper)(protected_dir=rig.protected, data_dir=rig.data, core_source=rig.core_source, pinned=rig.helper.pinned, now=rig.clock)
    assert other.session is None and other._running() is False
    assert json.loads(other.handle(build(rig, dry_run=True, token=False).line))["ok"] is True      # 另一个助手对象也不再认为有网卡


# ====================================================================================================================
# 令牌
# ====================================================================================================================

@covers_agent("E_TOKEN_MISSING")
def test_token_missing(make_rig):
    rig = make_rig()
    reply = rig.send(build(rig, token=False))
    assert code_of(reply) == "E_TOKEN_MISSING" and rig.helper.started_processes == 0 and not runs_in(rig)


@covers_agent("E_TOKEN_EXPIRED")
def test_token_expired(make_rig):
    rig = make_rig(confirm=lambda summary: CANCEL)
    on_time, half, late = build(rig), build(rig), build(rig)
    rig.clock.advance(120)
    assert code_of(rig.send(on_time)) == "E_USER_DECLINED"                # 有效期 120 秒, 算到代办读到请求为止: 第 120 秒整还有效 (确认窗点了取消)
    rig.clock.advance(0.5)
    assert code_of(rig.send(half)) == "E_USER_DECLINED"                   # 向下取整到秒 (契约第 2 节〔补 2〕): 120.5 秒时 t 还是第 120 秒, 两头都含, 还有效
    rig.clock.advance(0.5)
    assert code_of(rig.send(late)) == "E_TOKEN_EXPIRED"                   # 第 121 秒: 晚一秒过期
    assert rig.helper.started_processes == 0 and len(rig.helper.confirm_calls) == 2


@covers_agent("E_TOKEN_USED")
def test_token_used_a_second_time_is_refused(make_rig):
    rig = make_rig(confirm=lambda summary: CANCEL)
    built = build(rig)
    assert code_of(rig.send(built)) == "E_USER_DECLINED"                 # 令牌过了四查就作废了, 不管后面成不成
    again = rig.send(built)
    assert code_of(again) == "E_TOKEN_USED" and len(rig.helper.confirm_calls) == 1


@covers_agent("E_TOKEN_MISMATCH")
def test_token_for_another_action_or_other_params_does_not_match(make_rig):
    rig = make_rig()
    other_action = build(rig, token_overrides={"action": "net.restore"})
    wrong_fingerprint = build(rig, token_overrides={"params_sha256": "0" * 64})
    future = build(rig, token_overrides={"issued_at": int(rig.clock()) + 60, "expires_at": int(rig.clock()) + 120})
    too_long = build(rig, token_overrides={"expires_at": int(rig.clock()) + 121})
    for built in (other_action, wrong_fingerprint, future, too_long):
        assert code_of(rig.send(built)) == "E_TOKEN_MISMATCH"
    assert rig.helper.started_processes == 0 and not rig.helper.used_path.exists()


def test_token_binds_the_exact_bytes_of_params_json_so_one_changed_byte_does_not_match(make_rig):
    rig = make_rig()
    built = build(rig)
    for old, new in (('"v1.19.34"', '"v1.19.35"'), ('"port":1082', '"port":1083'), ('"NIKKE"', '"NIKKF"'), ('"template":1', '"template":1 ')):
        assert old in built.params_json
        changed = request_line("net.start", params_json=built.params_json.replace(old, new), token=built.token)
        assert code_of(rig.send(changed)) == "E_TOKEN_MISMATCH", (old, new)
    assert rig.helper.started_processes == 0


def test_token_ids_are_remembered_until_they_expire(make_rig):
    rig = make_rig(confirm=lambda summary: CANCEL)
    first = build(rig)
    assert code_of(rig.send(first)) == "E_USER_DECLINED"
    stored = json.loads(rig.helper.used_path.read_text(encoding="utf-8"))
    assert list(stored) == [first.token["id"]] and stored[first.token["id"]] == first.token["expires_at"]
    rig.clock.advance(300)                                               # 第一张早过期了; 新来一张时, 它的记录就不用留了
    second = build(rig)
    assert code_of(rig.send(second)) == "E_USER_DECLINED"
    assert list(json.loads(rig.helper.used_path.read_text(encoding="utf-8"))) == [second.token["id"]]


# ====================================================================================================================
# 请求本身: 版本、动作、格式、超长
# ====================================================================================================================

@covers_agent("E_VERSION")
def test_a_wrong_version_is_e_version(make_rig):
    rig = make_rig()
    reply = rig.send(b'{"v":2,"id":"r7","action":"net.start","dry_run":true}\n')
    assert code_of(reply) == "E_VERSION" and reply["id"] == "r7" and rig.helper.started_processes == 0


@covers_agent("E_UNKNOWN_ACTION")
def test_an_action_not_on_the_list_is_e_unknown_action(make_rig):
    rig = make_rig()
    for name in ("net.explode", "net.RESTORE", "net.restore ", "restore", "net.start.", "NET.START"):          # 名单上没有的 (大小写、多一个空格也算)
        reply = rig.send(request_line(name))
        assert code_of(reply) == "E_UNKNOWN_ACTION", name
    assert rig.helper.started_processes == 0


@covers_agent("E_BAD_REQUEST")
@pytest.mark.parametrize("elevated", [True, False])
def test_the_session_channel_actions_sent_to_the_agent_are_a_bad_request(make_rig, elevated):
    """代办只认 net.start 和单独起的 net.restore (契约第 1 节): net.stop、net.status、heartbeat 是看护方会话通道上的动作, 发给代办回 E_BAD_REQUEST
    (和会话通道上收到 net.start 对称); 演练也一样, 提没提权也一样."""
    rig = make_rig(elevated=elevated)
    before = snapshot(rig.protected)
    for raw in (request_line("net.stop", params_json='{"session":"x","reason":"user"}'), request_line("net.status", params_json="{}"),
                request_line("heartbeat"), request_line("net.stop", dry_run=True), request_line("net.status", dry_run=True),
                request_line("heartbeat", dry_run=True)):
        reply = rig.send(raw)
        assert code_of(reply) == "E_BAD_REQUEST" and "看护方" in reply["error"]["message"], raw
    assert rig.helper.started_processes == 0
    if elevated:
        assert [line["result"] for line in rig.helper.audit_lines()] == ["E_BAD_REQUEST"] * 3       # 提权的代办: 解得出来的真请求记一行, 演练不记
    else:
        assert snapshot(rig.protected) == before == {}                                                # 没提权: 什么都不写


@covers_agent("E_BAD_REQUEST")
def test_a_request_that_is_not_valid_is_a_bad_request(make_rig):
    rig = make_rig()
    for raw in (b"not json\n", b"[]\n", b'{"v":1,"id":"a","action":"net.start","bogus":1}\n', b'{"v":1,"v":1,"id":"a","action":"net.start"}\n'):
        assert code_of(rig.send(raw)) == "E_BAD_REQUEST", raw
    assert rig.helper.started_processes == 0 and not runs_in(rig)


@covers_agent("E_BAD_REQUEST")
def test_a_number_nesting_or_bom_the_line_may_not_have_is_a_bad_request_even_inside_the_token_and_crlf_is_fine(make_rig):
    """契约第 1 节〔补 2〕: 这一行里任何地方 (token 里面也算) 的数只许整数、不带小数点和指数、绝对值不超过 2^53-1; 嵌套最多 8 层; 开头不许有 BOM; 行尾认 LF 或 CRLF.
    不合 -> E_BAD_REQUEST, 应答的 id 是 null (这一行没解出来); 数的规则先于令牌的检查."""
    rig = make_rig()
    built = build(rig)
    text = built.line.decode("utf-8")
    issued = f'"issued_at":{built.token["issued_at"]}'
    assert issued in text
    for number in ("1.5", "1e2", "1800000000.0", "9007199254740992", "99999999999999999999"):
        reply = rig.send(text.replace(issued, f'"issued_at":{number}').encode("utf-8"))
        assert code_of(reply) == "E_BAD_REQUEST" and reply["id"] is None, number
    eight = text.replace(issued, issued + ',"x":' + "[" * 6 + "1" + "]" * 6)                  # 请求 1 层 + token 1 层 + 6 层数组 = 恰好 8 层
    nine = text.replace(issued, issued + ',"x":' + "[" * 7 + "1" + "]" * 7)
    assert code_of(rig.send(eight.encode("utf-8"))) == "E_TOKEN_MISMATCH"                    # 恰好 8 层不因为嵌套被拒 (token 里多了个字段, 所以是令牌形状不对)
    refused = rig.send(nine.encode("utf-8"))
    assert code_of(refused) == "E_BAD_REQUEST" and refused["id"] is None and "嵌套" in refused["error"]["message"]
    assert code_of(rig.send(bytes([0xEF, 0xBB, 0xBF]) + built.line)) == "E_BAD_REQUEST"      # 开头有 BOM
    assert rig.helper.started_processes == 0 and not rig.helper.used_path.exists()
    crlf = build(rig, dry_run=True, token=False).line.rstrip(b"\n") + b"\r\n"
    assert rig.send(crlf)["ok"] is True                                                      # 行尾 CRLF 照常认
    assert code_of(rig.send(build(rig, dry_run=True, token=False).line.rstrip(b"\n") + b"\r")) == "E_BAD_REQUEST"   # 单独的结尾回车不是行尾


@covers_agent("E_PARAMS")
@pytest.mark.parametrize("dry", [True, False])
def test_params_json_with_a_bad_number_nesting_or_bom_is_e_params_naming_params_json(make_rig, dry):
    """params_json 那段文本是另外解的, 同样的要求 (整数的写法和范围、8 层、开头没有 BOM、不许重复的键); 不合 -> E_PARAMS, fields 写 params_json."""
    rig = make_rig()
    text = protocol.start_params(RECIPE, core_id="mihomo", core_version="v1.19.34")
    for bad in (text.replace('"port":1082', '"port":1082.0'), text.replace('"port":1082', '"port":1.082e3'),
                text.replace('"max_minutes":240', '"max_minutes":9007199254740992'), '{"a":' * 9 + "1" + "}" * 9, chr(0xFEFF) + text):
        reply = real_or_dry(rig, bad, dry)
        assert code_of(reply) == "E_PARAMS" and reply["error"]["fields"] == ["params_json"], bad[:40]
    # 恰好 2^53-1: 数的规则不拦它, 说的是那个字段自己越界
    reply = real_or_dry(rig, text.replace('"max_minutes":240', '"max_minutes":9007199254740991'), dry)
    assert code_of(reply) == "E_PARAMS" and reply["error"]["fields"] == ["recipe.max_minutes"]
    assert rig.helper.started_processes == 0


@covers_agent("E_TOKEN_MISSING", "E_TOKEN_MISMATCH", "E_BAD_REQUEST")
def test_a_real_request_must_carry_params_json_null_means_left_out_and_a_null_dry_run_is_refused(make_rig):
    rig = make_rig()
    built = build(rig)
    assert code_of(rig.send(request_line("net.start", token=built.token))) == "E_TOKEN_MISMATCH"             # 带令牌、不带 params_json: 没有原文就没有指纹可比
    assert code_of(rig.send(protocol.encode({**built.request, "params_json": None}))) == "E_TOKEN_MISMATCH"   # params_json: null 等于没带
    assert code_of(rig.send(protocol.encode({**built.request, "token": None}))) == "E_TOKEN_MISSING"          # token: null 等于没带令牌
    reply = rig.send(protocol.encode({**built.request, "dry_run": None}))
    assert code_of(reply) == "E_BAD_REQUEST" and reply["id"] == "r1"                                         # dry_run: null 不行 (当 false 的话, 本想演练的请求就成了真请求)
    assert rig.helper.started_processes == 0 and not rig.helper.used_path.exists()


@covers_agent("E_TOKEN_MISMATCH")
@pytest.mark.parametrize("over", [{"plugin": "Net"}, {"plugin": "net\n"}, {"plugin": "a" * 49}, {"plugin": "a_b"}, {"plugin": "owner"}, {"plugin": "ayane"}, {"requested_by": "Owner"},
                                  {"requested_by": "x y"}, {"requested_by": ""}, {"requested_by": "owner\n"}, {"issuer": "host 1"},
                                  {"issuer": "x" * 65}, {"issuer": "主机"}, {"issuer": "x\n"}, {"issuer": ""}])
def test_token_strings_outside_the_allowed_writing_are_refused(make_rig, over):
    """确认窗是提权一侧的窗口, 要显示谁申请的, 不能照抄换行、超长的串、冒充系统提示的句子 (契约第 2 节〔补 2〕)."""
    rig = make_rig()
    reply = rig.send(build(rig, token_overrides=over))
    assert code_of(reply) == "E_TOKEN_MISMATCH" and rig.helper.started_processes == 0 and not rig.helper.used_path.exists()


@pytest.mark.parametrize("who", ["owner", "ayane", "net-lender", "a"])
def test_requested_by_owner_ayane_or_a_plugin_id_passes_the_token_checks_and_the_window_shows_it(make_rig, who):
    rig = make_rig(confirm=lambda summary: CANCEL)
    assert code_of(rig.send(build(rig, requested_by=who))) == "E_USER_DECLINED"
    assert rig.helper.confirm_calls[-1]["requested_by"] == who


@covers_agent("E_BAD_REQUEST")
def test_params_oversize_request_is_a_bad_request(make_rig):
    rig = make_rig()
    built = build(rig)
    big = request_line("net.start", params_json=built.params_json + " " * 70_000, token=built.token)
    assert len(big) > 65536
    reply = rig.send(big)
    assert code_of(reply) == "E_BAD_REQUEST" and "64 KB" in reply["error"]["message"]
    assert rig.helper.started_processes == 0 and not rig.helper.used_path.exists()


# ====================================================================================================================
# 参数
# ====================================================================================================================

def real_or_dry(rig: Rig, text: str, dry: bool) -> dict:
    """同一段参数文本发一遍: dry=True 是不带令牌的演练, dry=False 是带着绑这段文本的令牌的真请求 (第 3 步在令牌之后, 所以令牌要对得上)."""
    return rig.send(start_line(RECIPE, now=rig.clock(), params_json=text, dry_run=dry, token=not dry))


@covers_agent("E_PARAMS")
@pytest.mark.parametrize("dry", [True, False])
def test_params_unknown_keys_are_refused_at_every_level(make_rig, dry):
    rig = make_rig()
    for path in ("extra", "recipe.extra", "upstream.extra", "core.extra", "heartbeat.extra"):
        obj = good_params()
        node = obj
        *head, last = path.split(".")
        for key in head:
            node = node[key]
        node[last] = 1
        reply = real_or_dry(rig, params_text(obj), dry)
        assert code_of(reply) == "E_PARAMS" and reply["error"]["fields"] == [path]
    assert rig.helper.started_processes == 0 and not runs_in(rig)


@covers_agent("E_PARAMS")
@pytest.mark.parametrize("dry", [True, False])
def test_params_duplicate_keys_decimals_and_non_json_are_refused(make_rig, dry):
    rig = make_rig()
    text = protocol.start_params(RECIPE, core_id="mihomo", core_version="v1.19.34")
    for bad in (text.replace('"template":1', '"template":1,"template":1'), text.replace('"port":1082', '"port":1082.0'),
                text.replace('"max_minutes":240', '"max_minutes":2.4e2'), "not json", "[]", "null", text + "x"):
        reply = real_or_dry(rig, bad, dry)
        assert code_of(reply) == "E_PARAMS" and reply["error"]["fields"] == ["params_json"], bad
    assert rig.helper.started_processes == 0


@covers_agent("E_PARAMS")
def test_params_json_missing_altogether(make_rig):
    rig = make_rig()
    reply = rig.send(request_line("net.start", dry_run=True))            # 演练可以不带令牌, 但参数得有
    assert code_of(reply) == "E_PARAMS" and reply["error"]["fields"] == ["params_json"]
    token_only = rig.send(request_line("net.start", token=build(rig).token))
    assert code_of(token_only) == "E_TOKEN_MISMATCH"                     # 真请求: 令牌绑的是参数的指纹, 没有参数就对不上


PARAM_BOUNDS = [
    ("template", 2, "template"), ("recipe.name", "", "recipe.name"), ("recipe.name", "n" * 33, "recipe.name"),
    ("recipe.processes", [], "recipe.processes"), ("recipe.processes", [f"g{i}.exe" for i in range(17)], "recipe.processes"),
    ("recipe.processes", ["svchost.exe"], "recipe.processes"), ("recipe.max_minutes", 0, "recipe.max_minutes"),
    ("recipe.max_minutes", 721, "recipe.max_minutes"), ("upstream.host", "localhost", "upstream.host"), ("upstream.host", "0.0.0.0", "upstream.host"),
    ("upstream.host", "192.168.1.2", "upstream.host"), ("upstream.host", "::1", "upstream.host"), ("upstream.port", 0, "upstream.port"),
    ("upstream.port", 65536, "upstream.port"), ("upstream.port", "1082", "upstream.port"), ("core.id", "a b", "core.id"),
    ("core.version", "v" * 33, "core.version"), ("config_sha256", "A" * 64, "config_sha256"), ("heartbeat.interval_s", 0, "heartbeat.interval_s"),
    ("heartbeat.interval_s", 31, "heartbeat.interval_s"), ("heartbeat.grace_s", 9, "heartbeat.grace_s"), ("heartbeat.grace_s", 301, "heartbeat.grace_s"),
]


@covers_agent("E_PARAMS")
@pytest.mark.parametrize("dry", [True, False])
def test_params_each_field_out_of_range_is_named(make_rig, dry):
    rig = make_rig()
    for path, value, field in PARAM_BOUNDS:
        reply = real_or_dry(rig, edited(path, value), dry)
        assert code_of(reply) == "E_PARAMS" and reply["error"]["fields"] == [field], (path, value)
    for path in ("recipe", "upstream", "core", "heartbeat", "recipe.name", "upstream.port", "core.version", "config_sha256"):
        assert real_or_dry(rig, edited(path, drop=True), dry)["error"]["fields"] == [path]
    assert rig.helper.started_processes == 0 and not runs_in(rig)


# ====================================================================================================================
# 字符表 (契约第 7 节、第 11 节): 进程名和 recipe.name 只认那张码点表
# ====================================================================================================================

OUTSIDE_THE_TABLE = {"长 s U+017F": "ſ", "全角 A U+FF21": "Ａ", "带重音的 é U+00E9": "é", "西里尔字母 U+0430": "а",
                     "表意文字扩展区 U+3400": "㐀", "片假名中点 U+30FB": "・"}
INSIDE_THE_TABLE = {"U+4E00": "一", "U+9FFF": "鿿", "U+3041": "ぁ", "U+3096": "ゖ", "U+30A1": "ァ", "U+30FA": "ヺ",
                    "U+30FC": "ー", "U+AC00": "가", "U+D7A3": "힣"}


def dry_with_names(rig: Rig, *names: str, recipe_name: str = "NIKKE") -> dict:
    """进程名 (或配方名) 换成 names 的演练请求. 指纹: 名字合规才算得出来; 不合规的会在第 3 步 (参数白名单) 就被拒, 轮不到比指纹."""
    obj = good_params()
    obj["recipe"]["processes"] = list(names)
    obj["recipe"]["name"] = recipe_name
    try:
        obj["config_sha256"] = config_gen.fingerprint(config_gen.build(Recipe(recipe_name, tuple(names), Upstream("127.0.0.1", 1082), 240)))
    except Exception:  # noqa: BLE001 - 不合规的配方 build 不出来: 指纹随便放一个合规的, 反正过不了第 3 步
        pass
    return rig.send(start_line(RECIPE, now=rig.clock(), params_json=params_text(obj), dry_run=True, token=False))


@covers_agent("E_PARAMS")
@pytest.mark.parametrize("label", list(OUTSIDE_THE_TABLE))
def test_charset_letters_outside_the_table_are_refused(make_rig, label):
    rig = make_rig()
    char = OUTSIDE_THE_TABLE[label]
    reply = dry_with_names(rig, f"a{char}.exe")
    assert code_of(reply) == "E_PARAMS" and reply["error"]["fields"] == ["recipe.processes"], label
    reply = dry_with_names(rig, "ok.exe", recipe_name=f"n{char}")
    assert code_of(reply) == "E_PARAMS" and reply["error"]["fields"] == ["recipe.name"], label


@pytest.mark.parametrize("label", list(INSIDE_THE_TABLE))
def test_charset_cjk_kana_and_hangul_inside_the_table_pass(make_rig, label):
    rig = make_rig()
    char = INSIDE_THE_TABLE[label]
    reply = dry_with_names(rig, f"{char}.exe", recipe_name=f"{char}{char}")
    assert reply["ok"] is True, (label, reply)
    assert reply["result"]["summary"]["processes"] == [f"{char}.exe"]


@covers_agent("E_PARAMS")
def test_charset_reserved_names_that_differ_only_in_ascii_case_are_refused(make_rig):
    rig = make_rig()
    for name in ("SVCHOST.EXE", "svchost.exe", "Explorer.EXE", "wmpet.exe", "WM-ELEVATE.EXE", "clash-verge.exe", "Python.exe"):
        reply = dry_with_names(rig, name)
        assert code_of(reply) == "E_PARAMS" and reply["error"]["fields"] == ["recipe.processes"], name
    assert dry_with_names(rig, "Game.EXE")["ok"] is True                 # "不分大小写"只指 ASCII 的 A-Z 对 a-z, 别的名字照常


# ====================================================================================================================
# 配置和核心
# ====================================================================================================================

@covers_agent("E_CONFIG_MISMATCH")
@pytest.mark.parametrize("dry", [True, False])
def test_config_fingerprint_that_differs_by_one_digit_is_a_config_mismatch(make_rig, dry):
    rig = make_rig()
    obj = good_params()
    digit = obj["config_sha256"][0]
    obj["config_sha256"] = ("0" if digit != "0" else "1") + obj["config_sha256"][1:]
    reply = real_or_dry(rig, params_text(obj), dry)
    assert code_of(reply) == "E_CONFIG_MISMATCH" and rig.helper.started_processes == 0 and not runs_in(rig)


@covers_agent("E_CORE_UNKNOWN")
@pytest.mark.parametrize("dry", [True, False])
def test_core_that_is_not_on_the_list_is_core_unknown(make_rig, dry):
    rig = make_rig()
    for core_id, version in (("mihomo", "v9.9.9"), ("other", "v1.19.34")):
        text = protocol.start_params(RECIPE, core_id=core_id, core_version=version)
        assert code_of(real_or_dry(rig, text, dry)) == "E_CORE_UNKNOWN"
    assert rig.helper.started_processes == 0 and not runs_in(rig)


@covers_agent("E_CORE_MISSING")
@pytest.mark.parametrize("dry", [True, False])
def test_core_file_that_is_not_there_is_core_missing(make_rig, dry):
    rig = make_rig()
    rig.core_source.unlink()
    assert code_of(rig.send(build(rig, dry_run=dry, token=not dry))) == "E_CORE_MISSING"
    assert rig.helper.started_processes == 0 and not runs_in(rig)


@covers_agent("E_CORE_HASH")
def test_core_hash_checked_after_copying_and_the_copy_is_deleted_when_it_does_not_match(make_rig):
    rig = make_rig()
    rig.core_source.write_bytes(rig.core_source.read_bytes() + b"\n# tampered core\n")        # 名单里记的还是原来的哈希
    reply = rig.send(build(rig))
    assert code_of(reply) == "E_CORE_HASH"
    assert rig.helper.started_processes == 0 and not runs_in(rig)                          # 拷进去的那份和它的目录都删了
    assert not any(path.is_file() for path in rig.protected.rglob("*") if path.name == rig.core_source.name)


@covers_agent("E_CORE_HASH")
def test_a_dry_run_with_a_wrong_core_hash_says_so_and_nothing_is_copied_or_changed(make_rig):
    rig = make_rig()
    rig.core_source.write_bytes(rig.core_source.read_bytes() + b"\n# tampered core\n")
    before, source = snapshot(rig.protected), sha256_file(rig.core_source)
    reply = rig.send(build(rig, dry_run=True, token=False))
    assert code_of(reply) == "E_CORE_HASH"
    assert snapshot(rig.protected) == before and sha256_file(rig.core_source) == source
    assert not (rig.protected / "runs").exists() and rig.helper.started_processes == 0


def test_core_sees_the_generated_config_bytes_and_a_clean_environment_and_non_ascii_names_survive(make_rig, monkeypatch):
    """一次真起, 查四件事: 写进配置文件的就是算指纹的那串字节 (非 ASCII 的进程名原样不转义); 环境变量只有代办自己造的那几个 (测试进程里设了一批 mihomo 认的
    环境变量也不继承); 命令行固定为"工作目录 + 配置文件"、核心是拷进受保护目录的那一份; 汉字、假名、空格的进程名经命令行、会话通道、核心输出一路不乱."""
    for name in EVIL_ENV:                                                # 带着这些环境变量去起: 核心看到的环境里一个都不该有
        monkeypatch.setenv(name, "evil-value")
    rig = make_rig(behavior={"matches": {"原神.exe": 4, "Wuthering Waves.exe": 2, "ゲーム.exe": 1}})
    built = start_line(UNICODE_RECIPE, now=rig.clock())
    reply = rig.send(built)
    assert reply["ok"] is True, reply
    session = rig.helper.session
    wanted = json.loads(built.params_json)["config_sha256"]
    # 写进文件的就是算指纹的那串字节; 假核心读到的原文字节的哈希也是它
    config_file = session.work_dir / "config.yaml"
    expected_bytes = config_gen.render(config_gen.build(UNICODE_RECIPE)).encode("utf-8")
    assert config_file.read_bytes() == expected_bytes and hashlib.sha256(expected_bytes).hexdigest() == wanted
    assert "原神.exe".encode("utf-8") in expected_bytes and b"\\u" not in expected_bytes
    assert json.loads((session.work_dir / "fake-config.json").read_text(encoding="utf-8"))["sha256"] == wanted
    # 环境变量: 只有代办自己造的那几个
    names = json.loads((session.work_dir / "fake-env.json").read_text(encoding="utf-8"))
    assert set(names) == CLEAN_ENV_NAMES and not set(names) & set(EVIL_ENV) and not [name for name in names if name.startswith("CLASH")]
    # 命令行固定为"工作目录 + 配置文件", 核心是拷进受保护目录的那一份
    args = session.core.args
    assert args[0] == sys.executable and args[2:] == ["-d", str(session.work_dir), "-f", str(config_file)]
    assert Path(args[1]).parent == session.run_dir / "core" and Path(args[1]).read_bytes() == rig.core_source.read_bytes()
    assert session.run_dir.is_relative_to(rig.protected)
    # 非 ASCII 的进程名一路不乱: 确认窗、看护方的 matches (只有配方里的两个)
    assert [window["processes"] for window in rig.helper.confirm_calls] == [["原神.exe", "Wuthering Waves.exe"]]
    wait_for(lambda: channel(session.pipe, "net.status", {})["result"]["matches"] == {"原神.exe": 4, "Wuthering Waves.exe": 2}, 60, "两个进程的连接都被数到")


# ====================================================================================================================
# 确认窗
# ====================================================================================================================

@covers_agent("E_USER_DECLINED")
def test_cancel_in_the_confirm_window_declines_and_starts_nothing(make_rig):
    rig = make_rig(confirm=lambda summary: CANCEL)
    reply = rig.send(build(rig, requested_by="ayane"))
    assert code_of(reply) == "E_USER_DECLINED" and rig.helper.started_processes == 0 and not runs_in(rig)
    [window] = rig.helper.confirm_calls                                  # 确认窗的内容全由代办自己从参数生成, 并显示是谁申请的
    assert window["requested_by"] == "ayane" and window["processes"] == ["nikke.exe"] and window["upstream"] == "127.0.0.1:1082"
    assert window["max_minutes"] == 240 and window["core"] == "mihomo v1.19.34" and "Wintun" in window["driver"]


@covers_agent("E_CONFIRM_TIMEOUT")
def test_no_answer_in_the_confirm_window_is_a_timeout(make_rig):
    rig = make_rig(confirm=lambda summary: TIMEOUT)
    reply = rig.send(build(rig))
    assert code_of(reply) == "E_CONFIRM_TIMEOUT" and rig.helper.started_processes == 0 and not runs_in(rig)
    assert len(rig.helper.confirm_calls) == 1


def test_the_confirm_window_is_not_shown_for_dry_runs_or_failed_checks(make_rig):
    rig = make_rig()
    rig.send(build(rig, dry_run=True, token=False))
    rig.send(build(rig, token=False))
    rig.send(build(rig, token_overrides={"params_sha256": "0" * 64}))
    assert rig.helper.confirm_calls == []


# ====================================================================================================================
# 已经在跑 / 忙 / 内部错误
# ====================================================================================================================

@covers_agent("E_ALREADY_RUNNING")
def test_a_second_start_while_a_session_runs_is_refused_and_leaves_the_first_alone(make_rig):
    """已经有一段会话: 真请求、演练、以及另一个助手对象 (它不知道这段会话, 只看得到 WM-Ayane 网卡在) 都回 E_ALREADY_RUNNING, 第一段会话原样."""
    rig = make_rig()
    first = start_session(rig)
    runs_before = runs_in(rig)
    calls_before, started_before = len(rig.helper.confirm_calls), rig.helper.started_processes
    assert code_of(rig.send(build(rig))) == "E_ALREADY_RUNNING"
    assert code_of(rig.send(build(rig, dry_run=True, token=False))) == "E_ALREADY_RUNNING"          # 演练也查 (只读的核对)
    other = type(rig.helper)(protected_dir=rig.protected, data_dir=rig.data, core_source=rig.core_source, pinned=rig.helper.pinned, now=rig.clock)
    assert other.session is None
    assert code_of(json.loads(other.handle(build(rig).line))) == "E_ALREADY_RUNNING" and other.started_processes == 0
    assert runs_in(rig) == runs_before and len(rig.helper.confirm_calls) == calls_before and rig.helper.started_processes == started_before
    assert channel(rig.helper.session.pipe, "net.status", {})["result"]["session"] == first["session"]


def held_confirm() -> tuple[threading.Event, threading.Event, object]:
    """一个会卡住的确认窗: 进去以后 entered 置位, 一直等到 release 置位才点取消 (用来让一条真请求停在拿着锁的当口)."""
    entered, release = threading.Event(), threading.Event()

    def slow_confirm(summary: dict) -> str:
        entered.set()
        release.wait(60)
        return CANCEL

    return entered, release, slow_confirm


@covers_agent("E_BUSY")
def test_a_request_that_comes_while_another_is_being_handled_is_busy_and_its_token_is_not_used_up(make_rig):
    """契约 4.1 第 1 步: 同一时刻只有一个代办进程在处理真请求 (跨进程的锁, 假助手用进程内的锁模拟); 拿不到回 E_BUSY, 这张令牌不作废 (令牌四查在拿锁之后);
    演练不拿锁, 不会回 E_BUSY, 也不被正在处理的真请求挡住."""
    entered, release, slow_confirm = held_confirm()
    rig = make_rig(confirm=slow_confirm)
    first, second = build(rig, rid="first"), build(rig, rid="second")
    results: list[dict] = []
    worker = threading.Thread(target=lambda: results.append(rig.send(first)), daemon=True)
    worker.start()
    try:
        assert entered.wait(60)
        busy = rig.send(second)
        assert code_of(busy) == "E_BUSY" and busy["id"] == "second"
        assert second.token["id"] not in (json.loads(rig.helper.used_path.read_text(encoding="utf-8")) if rig.helper.used_path.exists() else {})
        dry = rig.send(build(rig, dry_run=True, token=False))                 # 演练不拿锁: 不会回 E_BUSY
        assert dry["ok"] is True and dry["result"]["dry_run"] is True
    finally:
        release.set()
        worker.join(60)
    assert results[0]["id"] == "first" and code_of(results[0]) == "E_USER_DECLINED"
    assert rig.helper.started_processes == 0
    again = rig.send(second)                                                 # 没人占着锁了: 同一张令牌还能用 (走到确认窗, 点了取消), 不是 E_TOKEN_USED
    assert code_of(again) == "E_USER_DECLINED" and again["id"] == "second"
    lines = rig.helper.audit_lines()
    assert [line["result"] for line in lines] == ["E_BUSY", "E_USER_DECLINED", "E_USER_DECLINED"]     # 真请求每条一行, 演练不记
    assert lines[0]["token_id"] is None and lines[0]["requested_by"] is None and lines[0]["summary"] is None       # E_BUSY 那一条: 令牌还没查, 令牌那两项空着
    assert lines[0]["params_sha256"] == protocol.params_fingerprint(second.params_json)                          # 参数指纹是代办自己算的 (先算、再拿锁): 照写
    assert lines[1]["token_id"] == first.token["id"] and lines[2]["token_id"] == second.token["id"]


@covers_agent("E_BUSY")
def test_the_same_token_handed_to_two_agents_at_once_gets_through_only_once(make_rig):
    entered, release, slow_confirm = held_confirm()
    rig = make_rig(confirm=slow_confirm)
    built = build(rig)
    results: list[dict] = []
    worker = threading.Thread(target=lambda: results.append(rig.send(built)), daemon=True)
    worker.start()
    try:
        assert entered.wait(60)
        assert code_of(rig.send(built)) == "E_BUSY"                          # 同一条请求、同一张令牌, 第二个代办拿不到锁
    finally:
        release.set()
        worker.join(60)
    assert code_of(results[0]) == "E_USER_DECLINED" and len(rig.helper.confirm_calls) == 1
    assert code_of(rig.send(built)) == "E_TOKEN_USED"                        # 第一个把令牌用掉了: 只有一个能过
    assert rig.helper.started_processes == 0


@covers_agent("E_BUSY", "E_NOT_ELEVATED")
@pytest.mark.parametrize("elevated, real_code", [(True, "E_BUSY"), (False, "E_NOT_ELEVATED")])
def test_with_the_lock_held_a_real_request_is_busy_or_not_elevated_and_a_dry_run_goes_through(make_rig, elevated, real_code):
    """顺序: 提权检查 -> 拿锁 -> 令牌四查. 没提权的真请求不管有没有别的请求在处理都回 E_NOT_ELEVATED (提权检查在拿锁之前); 提权的真请求拿不到锁回 E_BUSY
    (没带令牌也是: 拿锁在令牌四查之前); 演练不拿锁, 照常答."""
    rig = make_rig(elevated=elevated)
    assert rig.helper._lock.acquire(blocking=False)                          # 另一个请求正拿着锁 (模拟)
    try:
        assert code_of(rig.send(build(rig))) == real_code
        assert code_of(rig.send(build(rig, token=False))) == real_code
        assert rig.send(build(rig, dry_run=True, token=False))["ok"] is True
        assert rig.send(build(rig, dry_run=True))["ok"] is True
    finally:
        rig.helper._lock.release()
    assert rig.helper.started_processes == 0 and not rig.helper.used_path.exists()       # 令牌都没作废


@covers_agent("E_INTERNAL")
def test_an_unexpected_failure_is_e_internal_and_leaves_nothing_behind(make_rig, monkeypatch):
    rig = make_rig()

    def boom(*args, **kwargs):
        raise OSError("磁盘坏了")

    monkeypatch.setattr(shutil, "copyfile", boom)
    reply = rig.send(build(rig))
    assert code_of(reply) == "E_INTERNAL" and "OSError" in reply["error"]["message"] and "磁盘" not in reply["error"]["message"]
    assert rig.helper.started_processes == 0 and not runs_in(rig)


@covers_agent("E_INTERNAL")
def test_a_guardian_that_does_not_come_up_is_e_internal_and_the_core_is_cleaned_up(make_rig):
    rig = make_rig(guardian_module="wm.net.no_such_guardian", hand_over_timeout_s=30)
    reply = rig.send(build(rig))
    assert code_of(reply) == "E_INTERNAL" and "看护方" in reply["error"]["message"]
    assert rig.helper.started_processes == 2 and not rig.ledger.alive() and not runs_in(rig)     # 核心、看护方都没了, 目录也清了
    assert rig.helper.session is None


@covers_agent("E_INTERNAL")
@pytest.mark.parametrize("fault, reason", [("job_is_a_process", "不是作业对象"), ("process_not_in_job", "不在"), ("output_is_not_a_pipe", "不是管道")])
def test_a_wrong_handover_makes_the_guardian_exit_with_2_and_the_helper_answer_e_internal_and_leave_no_core(make_rig, fault, reason):
    """交接：三个句柄里有一个种类不对、或进程不在那个作业里, 看护方启动时按种类核对, 不对就 (作业句柄是真的就先结束作业) 往 stderr 写一行原因、
    以退出码 2 退出; 代办连不上看护方的管道, 回 E_INTERNAL, 核心没有留下, 这次的子目录也清了."""
    rig = make_rig(handover_fault=fault, hand_over_timeout_s=30)
    reply = rig.send(build(rig))
    assert code_of(reply) == "E_INTERNAL" and "看护方" in reply["error"]["message"]
    core, guardian_proc = rig.ledger.procs[0], rig.ledger.procs[-1]      # 第一个是核心, 最后一个是看护方 (process_not_in_job 时中间还有一个替身进程)
    assert guardian_proc.wait(30) == 2                                   # 看护方核对出来句柄不对: 退出码 2
    assert core.poll() is not None                                       # 核心没有留下 (作业句柄是真的, 看护方先结束了作业; 不是真的, 代办自己结束)
    assert rig.helper.session is None and not runs_in(rig)
    assert reason in rig.helper.guardian_stderr.read_text(encoding="utf-8", errors="replace")      # 往 stderr 写了一行原因 (UTF-8)
    assert not rig.helper.guardian_audit.exists() and not rig.helper.flush_log.exists()            # 没有起来过, 也就没有收尾的记录


def test_a_core_that_is_already_dead_when_the_guardian_starts_is_cleaned_up_and_recorded(make_rig):
    """核心在看护方起来之前就已经退了: 退了的进程还算在作业里, 句柄核对照过, 看护方起来以后发现核心退了, 按 core_exited 收尾 (怎么处理都行, 只要收尾干净).
    这时代办的交接可能赶上看护方的管道 (应答 ok), 也可能看护方已经收尾退出了 (E_INTERNAL): 两种都对, 之后什么都不该留下."""
    rig = make_rig(handover_fault="core_dead", hand_over_timeout_s=30)
    reply = rig.send(build(rig))
    assert reply["ok"] is True or code_of(reply) == "E_INTERNAL", reply
    guardian_proc = rig.ledger.procs[-1]
    assert guardian_proc.wait(60) == 0                                   # 句柄核对过了 (不是退出码 2), 收尾以后自己退出
    [record] = [json.loads(text) for text in rig.helper.guardian_audit.read_text(encoding="utf-8").splitlines()]
    assert record["reason"] == "core_exited" and record["state"] == "failed" and record["core_exit_code"] == 1
    assert record["adapter_gone"] is True and record["dns_flushed"] is True
    assert rig.helper.flush_log.read_text(encoding="utf-8").count("flush") == 1
    assert not rig.ledger.alive() and not rig.leftover_pipes()


# ====================================================================================================================
# 起不来
# ====================================================================================================================

@covers_agent("E_START_FAILED")
def test_a_core_that_never_builds_the_adapter_is_a_failed_start_and_everything_is_cleaned(make_rig):
    rig = make_rig(behavior={"never_adapter": True}, start_timeout_s=1.5)
    started = time.monotonic()
    reply = rig.send(build(rig))
    assert code_of(reply) == "E_START_FAILED" and "没出现" in reply["error"]["message"] and time.monotonic() - started < 60
    assert rig.helper.started_processes == 1 and not rig.ledger.alive()                  # 核心起过, 已经被结束 (作业里没有活着的进程)
    assert not runs_in(rig) and rig.helper.session is None                               # 这次写进受保护目录的配置、拷进去的核心都清了
    assert not any(path.name in ("config.yaml", "mihomo-fake.py") for path in rig.protected.rglob("*"))


@covers_agent("E_START_FAILED")
def test_a_core_that_exits_first_is_a_failed_start_with_the_last_output_masked(make_rig):
    rig = make_rig(behavior={"exit_after_s": 0.05, "adapter_delay_s": 30, "exit_code": 3,
                             "startup_lines": ["WMFAKE error dial 203.0.113.7:443 to www.example.com failed"]}, start_timeout_s=20)
    reply = rig.send(build(rig))
    message = reply["error"]["message"]
    assert code_of(reply) == "E_START_FAILED" and "退出码 3" in message
    assert "203.0.113.7" not in message and "example.com" not in message and "***" in message and "WMFAKE core started" in message
    assert not rig.ledger.alive() and not runs_in(rig)
    audit = (rig.protected / "audit.jsonl").read_text(encoding="utf-8")                  # 审计里只有结果码, 没有核心的输出
    assert "203.0.113.7" not in audit and "example.com" not in audit and json.loads(audit.splitlines()[-1])["result"] == "E_START_FAILED"


# ====================================================================================================================
# 看护: 看护方没了核心跟着没; 心跳断; 核心自己退了
# ====================================================================================================================

def test_killing_the_guardian_takes_the_core_with_it_within_two_seconds(make_rig):
    rig = make_rig()
    start_session(rig)
    session = rig.helper.session
    assert session.core.poll() is None and session.guardian.poll() is None
    session.guardian.kill()                                              # 看护方崩了 (强杀): 作业对象的最后一个句柄关掉, 里面的核心跟着没
    session.core.wait(timeout=2.0)
    assert session.core.poll() is not None
    assert not rig.helper.flush_log.exists()                             # 没人清 DNS 缓存 (契约 4.5: 网络插件下次启动时补清)
    assert not (rig.helper.guardian_audit.exists() and rig.helper.guardian_audit.read_text(encoding="utf-8").strip())


def test_a_silent_host_loses_the_session_after_the_grace_period(make_rig):
    """用真时间: 心跳间隔 1 秒、宽限 10 秒. 发几下心跳 (会话一直在), 然后不发了: 宽限一到看护方就停, 原因记 heartbeat_lost. 这一条慢 (十几秒)."""
    rig = make_rig()
    start_session(rig, interval_s=1, grace_s=10)
    session = rig.helper.session
    pace = time.monotonic()
    for _ in range(2):
        pace += 1.0
        time.sleep(max(0.0, pace - time.monotonic()))
        assert channel(session.pipe, "heartbeat")["result"] == {"state": "running"}
    silent_since = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):                       # 静默了 4 秒: 看护方还在 (没提前停)
        session.guardian.wait(timeout=4)
    assert session.guardian.wait(timeout=60) == 0
    silent_for = time.monotonic() - silent_since
    assert 9.0 <= silent_for < 55, silent_for
    assert session.core.poll() is not None and rig.helper.flush_log.read_text(encoding="utf-8").count("flush") == 1
    [record] = [json.loads(text) for text in rig.helper.guardian_audit.read_text(encoding="utf-8").splitlines()]
    assert record["reason"] == "heartbeat_lost" and record["state"] == "stopped" and record["session"] == rig.helper.session.id
    assert record["core_exit_code"] is None                              # 核心不是自己退的: null (收尾时它的退出码是我们杀出来的, 不记)
    assert not win32.pipe_exists(session.pipe)


def test_the_core_exiting_on_its_own_is_cleaned_up_and_recorded_as_failed(make_rig):
    rig = make_rig()
    start_session(rig)
    session = rig.helper.session
    session.core.kill()                                                  # 核心自己崩了 (外面看和自己退出一样)
    assert session.guardian.wait(timeout=60) == 0
    [record] = [json.loads(text) for text in rig.helper.guardian_audit.read_text(encoding="utf-8").splitlines()]
    assert record["reason"] == "core_exited" and record["state"] == "failed" and record["adapter_gone"] is True and record["dns_flushed"] is True
    assert record["core_exit_code"] == session.core.wait(10) == 1       # 核心自己退的时候: 审计里是它的退出码 (Popen.kill 杀出来的是 1)
    assert rig.helper.flush_log.read_text(encoding="utf-8").count("flush") == 1 and not win32.pipe_exists(session.pipe)


# ====================================================================================================================
# 核心输出不落盘
# ====================================================================================================================

def files_with(base: Path, needle: bytes) -> list[Path]:
    """base 下面哪些文件里有 needle (.py 不算: 假核心脚本的拷贝是代码, 它的说明里本来就写着那种输出的例子)."""
    return [path for path in base.rglob("*") if path.is_file() and path.suffix != ".py" and needle in path.read_bytes()]


def test_the_cores_output_is_never_written_to_disk(make_rig):
    rig = make_rig(behavior={"matches": {"nikke.exe": 5, "chrome.exe": 2}, "direct": {"nikke.exe": 3}, "repeat": {"process": "nikke.exe", "every_s": 0.2}})
    start_session(rig)
    session = rig.helper.session
    deadline = time.monotonic() + 60
    while channel(session.pipe, "net.status", {})["result"]["matches"].get("nikke.exe", 0) < 7:     # 输出确实被读过、数过了
        assert time.monotonic() < deadline, "没等到核心的输出被数到"
        time.sleep(0.1)
    channel(session.pipe, "net.stop", {"session": session.id, "reason": "user"})
    assert session.guardian.wait(30) == 0
    probe = rig.base / "control.txt"                                     # 对照: 扫描办法确实找得到 (往里放一行, 扫到了再删掉)
    probe.write_bytes(b"WMFAKE match process=nikke.exe via=upstream\n")
    assert files_with(rig.base, b"WMFAKE match") == [probe]
    probe.unlink()
    assert files_with(rig.base, b"WMFAKE match") == []                   # 受保护目录、核心的工作目录、审计文件、看护方的日志: 都搜不到
    for needle in (b"via=upstream", b"via=direct", b"process=nikke.exe", b"process=chrome.exe"):
        assert files_with(rig.base, needle) == [], needle
    assert rig.helper.guardian_audit.exists() and json.loads(rig.helper.guardian_audit.read_text(encoding="utf-8"))["matches"]["nikke.exe"] >= 7
    assert not rig.helper.guardian_stderr.read_bytes().strip()


# ====================================================================================================================
# 单独起的 net.restore (契约 4.4 末尾〔补 2〕的 7 步; 第 4 轮): 看护方不在、但有残留 (或什么都没有) 时, 由代办走卡片、令牌、提权框来恢复
# ====================================================================================================================

RESTORE_STEPS = ["kill_core", "adapter_gone", "flush_dns"]
NOTHING_TO_DO = [("kill_core", "skipped"), ("adapter_gone", "skipped"), ("flush_dns", "done")]          # 从没开过、已经没了: 重复发结果一样
ALL_DONE = [("kill_core", "done"), ("adapter_gone", "done"), ("flush_dns", "done")]


def restore(rig: Rig, **kwargs) -> dict:
    """发一条单独起的 net.restore (默认带令牌: 令牌批 net.restore、绑着 params_json 那段 {})."""
    return rig.send(restore_line(now=rig.clock(), **kwargs))


def steps_of(reply: dict) -> list[tuple[str, str]]:
    assert reply["ok"] is True, reply
    return [(step["step"], step["result"]) for step in reply["result"]["steps"]]


def sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)


def write_record(rig: Rig, name: str, content: object) -> Path:
    """在 runs/<name>/ 下放一份 session.json (content 是字符串就原样写, 否则写成 JSON); 返回那个目录."""
    folder = rig.protected / "runs" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "session.json").write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return folder


def another_helper(rig: Rig, **kwargs):
    """同一套目录上新建一个助手对象 (它不记得上一个起过什么: 看护方不在、只剩残留时, 恢复靠的是受保护目录里的记录)."""
    return type(rig.helper)(protected_dir=rig.protected, data_dir=rig.data, core_source=rig.core_source, pinned=rig.helper.pinned, now=rig.clock, **kwargs)


def leave_a_leftover(rig: Rig) -> object:
    """造"残留": 交接以后助手故意不关自己手里的作业对象句柄 (handover_fault="keep_job_handle"), 再强杀看护方——核心不会跟着没. 返回这段会话."""
    start_session(rig)
    session = rig.helper.session
    session.guardian.kill()
    assert session.guardian.wait(30) is not None
    return session


def test_a_real_start_records_the_core_process_number_and_creation_time_in_session_json(make_rig):
    rig = make_rig()
    start_session(rig)
    session = rig.helper.session
    record = json.loads((session.run_dir / "session.json").read_text(encoding="utf-8"))
    assert set(record) == {"core_pid", "core_created"} and record["core_pid"] == session.core_pid
    handle = win32.open_process(session.core_pid, access=win32.PROCESS_LIMITED_ACCESS)
    try:
        assert win32.process_creation_time(handle) == record["core_created"]
    finally:
        win32.close_handle(handle)


def test_restore_when_nothing_was_ever_started_is_skipped_skipped_done_and_stays_so_when_repeated(make_rig):
    rig = make_rig()
    shutil.rmtree(rig.protected)                                         # 从没开过: 受保护目录都不存在 (不因为目录不在而出错)
    for index in range(3):
        reply = restore(rig)
        assert steps_of(reply) == NOTHING_TO_DO and set(reply["result"]) == {"steps", "clean", "reboot_advised"}
        assert reply["result"]["clean"] is True and reply["result"]["reboot_advised"] is False
        assert [set(step) for step in reply["result"]["steps"]] == [{"step", "result", "detail"}] * 3 and not any(s["detail"] for s in reply["result"]["steps"])
        assert rig.helper.agent_flush_log.read_text(encoding="utf-8").count("flush") == index + 1       # 清 DNS 缓存是假的: 往记录文件追加一行
    assert rig.helper.started_processes == 0 and not (rig.protected / "runs").exists()


def test_restore_after_a_normal_stop_is_skipped_skipped_done_and_the_runs_directory_is_emptied(make_rig):
    rig = make_rig()
    first = start_session(rig)
    session = rig.helper.session
    assert channel(session.pipe, "net.stop", {"session": first["session"], "reason": "user"})["ok"] is True
    assert session.guardian.wait(30) == 0 and session.core.wait(30) is not None
    assert runs_in(rig) == [session.run_dir]                             # 停了以后旧的运行目录还在 (没人清它)
    for _ in range(2):
        reply = restore(rig)
        assert steps_of(reply) == NOTHING_TO_DO and reply["result"]["clean"] is True                      # 核心早没了 (记录里的进程已经退了): skipped
        assert runs_in(rig) == []                                        # runs/ 清空


def test_restore_on_a_running_session_kills_the_core_and_the_guardian_winds_up_as_if_the_core_had_exited(make_rig):
    rig = make_rig()
    first = start_session(rig)
    session = rig.helper.session
    reply = restore(rig)
    assert steps_of(reply) == ALL_DONE and reply["result"]["clean"] is True and reply["result"]["reboot_advised"] is False
    assert session.core.wait(30) is not None and not (session.work_dir / "fake-adapter-WM-Ayane").exists()      # 核心没了, 标记没了
    assert session.guardian.wait(60) == 0                                # 看护方发现核心没了, 按核心自己退了来收尾, 然后退出 (不冲突)
    [record] = [json.loads(text) for text in rig.helper.guardian_audit.read_text(encoding="utf-8").splitlines()]
    assert record["reason"] == "core_exited" and record["state"] == "failed" and record["session"] == first["session"] and record["core_exit_code"] == 1
    assert runs_in(rig) == []                                            # runs/ 清了
    assert steps_of(restore(rig)) == NOTHING_TO_DO                       # 再发一遍: 没有什么可做的了


def test_a_new_session_can_be_started_after_a_restore(make_rig):
    rig = make_rig()
    first = start_session(rig)
    old = rig.helper.session
    assert steps_of(restore(rig)) == ALL_DONE
    assert old.guardian.wait(60) == 0 and old.core.wait(30) is not None
    second = start_session(rig)                                          # 恢复以后网卡没了、没有会话在跑: 能再起
    assert second["session"] != first["session"] and runs_in(rig) == [rig.helper.session.run_dir]
    assert channel(rig.helper.session.pipe, "net.status", {})["result"]["state"] == "running"


def test_restore_with_a_leftover_core_after_the_guardian_was_killed_kills_it_and_stays_stable_when_repeated(make_rig):
    """看护方不在、但有残留: 交接以后助手手里还留着作业对象句柄, 强杀看护方时核心不会跟着没 (2 秒后还活着、标记还在). 新建一个助手对象 (同一套目录) 发恢复."""
    rig = make_rig(handover_fault="keep_job_handle")
    session = leave_a_leftover(rig)
    marker = session.work_dir / "fake-adapter-WM-Ayane"
    time.sleep(2.0)
    assert session.core.poll() is None and marker.exists()               # 残留: 核心还活着, 网卡 (标记) 还在
    other = another_helper(rig)
    reply = json.loads(other.handle(restore_line(now=rig.clock()).line))
    assert steps_of(reply) == ALL_DONE and reply["result"]["clean"] is True
    assert session.core.wait(30) is not None and not marker.exists() and runs_in(rig) == []                   # 核心没了, 标记没了, runs/ 清了
    assert steps_of(json.loads(other.handle(restore_line(now=rig.clock()).line))) == NOTHING_TO_DO            # 再发一遍
    assert not rig.leftover_pipes()


def test_without_the_leftover_hook_killing_the_guardian_takes_the_core_with_it(make_rig):
    """对照: 没有那个钩子 (交接以后助手把作业句柄关了), 强杀看护方, 核心 2 秒内跟着没 (契约: 看护方没了核心跟着没)."""
    rig = make_rig()
    start_session(rig)
    session = rig.helper.session
    session.guardian.kill()
    assert session.core.wait(10) is not None


def test_a_recorded_process_number_that_now_belongs_to_another_process_is_not_touched(make_rig):
    """进程号会被重用: session.json 里记的进程号现在是一个活着的无关进程、创建时间对不上 -> 不结束它, kill_core 是 skipped, detail 说一句, 它还活着."""
    rig = make_rig()
    bystander = rig.ledger.add(sleeper())
    write_record(rig, "aaaa1111", {"core_pid": bystander.pid, "core_created": 12345})
    reply = restore(rig)
    assert steps_of(reply) == NOTHING_TO_DO and reply["result"]["clean"] is True
    detail = reply["result"]["steps"][0]["detail"]
    assert "创建时间对不上" in detail and str(bystander.pid) in detail
    assert bystander.poll() is None and runs_in(rig) == []               # 那个进程还活着; 记录清掉了


def test_a_recorded_process_with_the_matching_creation_time_is_killed_and_one_that_no_longer_exists_is_skipped(make_rig):
    rig = make_rig()
    victim = rig.ledger.add(sleeper())
    write_record(rig, "bbbb1111", {"core_pid": victim.pid, "core_created": win32.process_creation_time(int(victim._handle))})
    write_record(rig, "bbbb2222", {"core_pid": 4_000_000, "core_created": 1})                   # 没有这个进程号的进程
    reply = restore(rig)
    assert steps_of(reply) == ALL_DONE and victim.wait(30) is not None and runs_in(rig) == []
    assert steps_of(restore(rig)) == NOTHING_TO_DO


@pytest.mark.parametrize("content", ["not json", "[]", "", '{"core_pid": "x", "core_created": 1}', '{"core_pid": 0, "core_created": 1}', '{"core_pid": 5}',
                                     '{"core_pid": true, "core_created": 1}', '{"core_pid": 5, "core_created": 1.5}'])
def test_an_unreadable_session_record_is_skipped_with_a_note_and_does_not_fail_the_restore(make_rig, content):
    rig = make_rig()
    write_record(rig, "bad00000", content)
    victim = rig.ledger.add(sleeper())
    write_record(rig, "good0000", {"core_pid": victim.pid, "core_created": win32.process_creation_time(int(victim._handle))})
    reply = restore(rig)
    assert steps_of(reply) == ALL_DONE and reply["result"]["clean"] is True                      # 好的那份照常处理
    assert "bad00000/session.json" in reply["result"]["steps"][0]["detail"] and victim.wait(30) is not None
    assert steps_of(restore(rig)) == NOTHING_TO_DO


def test_an_unreadable_record_alone_is_skipped_with_a_note(make_rig):
    rig = make_rig()
    write_record(rig, "bad00000", "not json")
    reply = restore(rig)
    assert steps_of(reply) == NOTHING_TO_DO and "bad00000/session.json" in reply["result"]["steps"][0]["detail"] and runs_in(rig) == []


def test_an_adapter_that_does_not_go_away_in_time_makes_the_restore_not_clean_and_advises_a_reboot_every_time(make_rig):
    """网卡到时间不消失 (手工放一个普通文件当标记, 它不会自己没; 等待时长用参数缩短): adapter_gone 是 failed, clean 是 false, reboot_advised 是 true. 有 failed 就什么都不清:
    标记还在, 连发两遍两遍都是 failed (网卡赖着不走的, 每次都建议重启, 直到它真的没了). 不能把"网卡在不在"的凭据自己清掉, 再发一遍就成了"干净"
    (真的网卡删目录是删不掉的, 假的不能比真的好骗)."""
    rig = make_rig(restore_adapter_timeout_s=0.4)
    folder = rig.protected / "runs" / "cccc2222"
    marker = folder / "work" / "fake-adapter-WM-Ayane"
    marker.parent.mkdir(parents=True)
    marker.write_text("an ordinary file, it will not go away by itself", encoding="utf-8")
    for _ in range(2):
        started = time.monotonic()
        reply = restore(rig)
        assert steps_of(reply) == [("kill_core", "skipped"), ("adapter_gone", "failed"), ("flush_dns", "done")]
        assert reply["result"]["clean"] is False and reply["result"]["reboot_advised"] is True
        assert "WM-Ayane" in reply["result"]["steps"][1]["detail"] and 0.3 <= time.monotonic() - started < 30
        assert marker.exists() and runs_in(rig) == [folder]              # 有 failed: 什么都没清, 标记还在
    assert [line["summary"] for line in rig.helper.audit_lines()] == [{"clean": False, "kill_core": "skipped", "adapter_gone": "failed", "flush_dns": "done"}] * 2
    marker.unlink()                                                      # 网卡真的没了 (手工删掉那个标记): 这一次才是干净的, 目录也清了
    assert steps_of(restore(rig)) == NOTHING_TO_DO and runs_in(rig) == []


def test_a_restore_that_could_not_end_the_core_keeps_the_records_so_the_next_one_still_finds_it(make_rig, monkeypatch):
    """残留场景, 结束进程失败: kill_core 是 failed (核心还活着, 标记还在), adapter_gone 跟着 failed, clean 是 false; 有 failed 就什么都不清, session.json 和标记都还在.
    结束进程恢复正常以后再发: done、done、done, 核心没了, clean 是 true, runs/ 清了. 要是第一次就把记录清了, 第二次什么都认不出来, 会报"干净"而核心其实还活着."""
    rig = make_rig(handover_fault="keep_job_handle", restore_adapter_timeout_s=0.4)
    session = leave_a_leftover(rig)
    marker, record = session.work_dir / "fake-adapter-WM-Ayane", session.run_dir / "session.json"
    working = win32.terminate_process
    monkeypatch.setattr(win32, "terminate_process", lambda handle, exit_code=1: False)         # 结束进程失败
    other = another_helper(rig, restore_adapter_timeout_s=0.4)             # (新建的助手对象不带 rig 的参数: 等待时长要自己给, 不然每次失败都等满 10 秒)
    for _ in range(2):                                                   # 结束不了的, 每次都认得出来、每次都是 failed
        reply = json.loads(other.handle(restore_line(now=rig.clock()).line))
        assert steps_of(reply) == [("kill_core", "failed"), ("adapter_gone", "failed"), ("flush_dns", "done")]
        assert reply["result"]["clean"] is False and reply["result"]["reboot_advised"] is True
        assert "结束不了" in reply["result"]["steps"][0]["detail"]
        assert session.core.poll() is None and marker.exists() and record.exists() and runs_in(rig) == [session.run_dir]
    monkeypatch.setattr(win32, "terminate_process", working)              # 结束进程恢复正常
    fixed = json.loads(other.handle(restore_line(now=rig.clock()).line))
    assert steps_of(fixed) == ALL_DONE and fixed["result"]["clean"] is True and fixed["result"]["reboot_advised"] is False
    assert session.core.wait(30) is not None and not marker.exists() and runs_in(rig) == []
    assert steps_of(json.loads(other.handle(restore_line(now=rig.clock()).line))) == NOTHING_TO_DO
    assert [line["summary"]["clean"] for line in other.audit_lines() if line["action"] == "net.restore"] == [False, False, True, True]


@pytest.mark.parametrize("elevated", [True, False])
def test_a_restore_dry_run_answers_would_and_touches_nothing(make_rig, elevated):
    rig = make_rig(elevated=elevated)
    before = snapshot(rig.protected)
    reply = restore(rig, dry_run=True, token=False)
    assert reply == {"v": 1, "id": "r1", "ok": True, "result": {"dry_run": True, "would": RESTORE_STEPS}}
    assert restore(rig, dry_run=True)["ok"] is True                      # 带了令牌的演练: 令牌照查, 不作废
    assert code_of(restore(rig, dry_run=True, token_overrides={"params_sha256": "0" * 64})) == "E_TOKEN_MISMATCH"       # 带了坏令牌的演练也照查
    assert code_of(restore(rig, dry_run=True, token=False, params_json=None)) == "E_PARAMS"          # 演练没带 params_json: E_PARAMS
    bad = restore(rig, dry_run=True, token=False, params_json='{"a":1}')
    assert code_of(bad) == "E_PARAMS" and bad["error"]["fields"] == ["a"]
    assert snapshot(rig.protected) == before == {} and not rig.helper.used_path.exists() and not rig.helper.audit_path.exists()
    assert not rig.helper.agent_flush_log.exists() and rig.helper.started_processes == 0 and rig.helper.confirm_calls == []


def test_the_restore_dry_run_without_params_json_names_params_json(make_rig):
    rig = make_rig()
    reply = restore(rig, dry_run=True, token=False, params_json=None)
    assert code_of(reply) == "E_PARAMS" and reply["error"]["fields"] == ["params_json"]
    for text in ("[]", "not json", "5", '"x"'):
        bad = restore(rig, dry_run=True, token=False, params_json=text)
        assert code_of(bad) == "E_PARAMS" and bad["error"]["fields"] == ["params_json"], text


def test_restore_without_elevation_is_refused_and_writes_nothing_but_a_dry_run_is_answered_and_a_leftover_core_stays_alive(make_rig):
    rig = make_rig(handover_fault="keep_job_handle")
    session = leave_a_leftover(rig)
    other = another_helper(rig, elevated=False)
    before = snapshot(rig.protected)
    assert code_of(json.loads(other.handle(restore_line(now=rig.clock()).line))) == "E_NOT_ELEVATED"
    assert code_of(json.loads(other.handle(restore_line(now=rig.clock(), token=False).line))) == "E_NOT_ELEVATED"
    dry = json.loads(other.handle(restore_line(now=rig.clock(), dry_run=True, token=False).line))
    assert dry["ok"] is True and dry["result"] == {"dry_run": True, "would": RESTORE_STEPS}
    assert session.core.poll() is None                                   # 演练以后核心还活着
    assert snapshot(rig.protected) == before and not other.agent_flush_log.exists()                        # 什么都没写


@covers_agent("E_TOKEN_MISMATCH", "E_TOKEN_USED", "E_TOKEN_MISSING", "E_TOKEN_EXPIRED", "E_PARAMS")
def test_restore_token_rules(make_rig):
    rig = make_rig()
    assert code_of(restore(rig, token_overrides={"action": "net.start"})) == "E_TOKEN_MISMATCH"           # 批的是 net.start 的令牌拿来恢复
    assert code_of(rig.send(build(rig, token_overrides={"action": "net.restore"}))) == "E_TOKEN_MISMATCH"   # 反过来也不行
    assert code_of(restore(rig, params_json=None)) == "E_TOKEN_MISMATCH"                                  # 没带 params_json: 没有原文就没有指纹可比
    assert code_of(restore(rig, token_overrides={"params_sha256": "0" * 64})) == "E_TOKEN_MISMATCH"
    assert code_of(restore(rig, token=False)) == "E_TOKEN_MISSING"
    assert code_of(rig.send(protocol.encode({**restore_line(now=rig.clock()).request, "token": None}))) == "E_TOKEN_MISSING"
    stale = restore_line(now=rig.clock())
    rig.clock.advance(121)
    assert code_of(rig.send(stale)) == "E_TOKEN_EXPIRED"
    keys = restore(rig, params_json='{"a":1}')                           # params_json 里有键: E_PARAMS (令牌绑着这段文本, 四查过了, 令牌作废)
    assert code_of(keys) == "E_PARAMS" and keys["error"]["fields"] == ["a"]
    assert code_of(restore(rig, params_json="[]")) == "E_PARAMS"
    once = restore_line(now=rig.clock())
    assert rig.send(once)["ok"] is True and code_of(rig.send(once)) == "E_TOKEN_USED"                    # 用过
    assert rig.helper.started_processes == 0


def test_restore_with_the_lock_held_is_busy_and_its_token_is_not_used_up(make_rig):
    rig = make_rig()
    line = restore_line(now=rig.clock())
    assert rig.helper._lock.acquire(blocking=False)
    try:
        assert code_of(rig.send(line)) == "E_BUSY"
        assert rig.send(restore_line(now=rig.clock(), dry_run=True, token=False))["ok"] is True          # 演练不拿锁
    finally:
        rig.helper._lock.release()
    assert not rig.helper.used_path.exists()
    assert rig.send(line)["ok"] is True                                  # 令牌还能用
    busy_line, done_line = rig.helper.audit_lines()
    assert busy_line["result"] == "E_BUSY" and busy_line["token_id"] is None and busy_line["requested_by"] is None and busy_line["summary"] is None
    assert busy_line["params_sha256"] == protocol.params_fingerprint("{}") and done_line["result"] == "ok"


def test_the_restore_confirmation_window_is_off_by_default_and_when_on_it_can_be_declined_or_time_out(make_rig):
    rig = make_rig()
    assert restore(rig)["ok"] is True and rig.helper.confirm_calls == []                                   # 默认不弹
    for verdict, code in ((CANCEL, "E_USER_DECLINED"), (TIMEOUT, "E_CONFIRM_TIMEOUT")):
        sick = make_rig(confirm_restore=True, confirm=lambda window, v=verdict: v)
        line = restore_line(now=sick.clock(), requested_by="ayane")
        assert code_of(sick.send(line)) == code
        [window] = sick.helper.confirm_calls
        assert window["action"] == "net.restore" and window["requested_by"] == "ayane" and window["does"] == RESTORE_STEPS      # 动作、谁申请的、要做的三件事
        assert not sick.helper.agent_flush_log.exists()                  # 三步没做
        assert code_of(sick.send(line)) == "E_TOKEN_USED"                # 令牌四查过了就作废了, 确认窗里取消也不退回
        [declined, _used] = sick.helper.audit_lines()
        assert declined["result"] == code and declined["token_id"] == line.token["id"] and declined["summary"] is None
    yes = make_rig(confirm_restore=True)
    assert restore(yes)["ok"] is True and len(yes.helper.confirm_calls) == 1


def test_the_restore_audit_line(make_rig):
    rig = make_rig()
    built = restore_line(now=rig.clock())
    assert rig.send(built)["ok"] is True
    [line] = rig.helper.audit_lines()
    assert set(line) == AUDIT_KEYS and (line["action"], line["result"]) == ("net.restore", "ok")
    assert line["token_id"] == built.token["id"] and line["requested_by"] == "owner" and line["params_sha256"] == protocol.params_fingerprint("{}")
    assert line["summary"] == {"clean": True, "kill_core": "skipped", "adapter_gone": "skipped", "flush_dns": "done"}      # 三步做完以后才有, 代办自己算的
    assert code_of(restore(rig, token_overrides={"extra": 1})) == "E_TOKEN_MISMATCH"                     # 令牌形状不对: 令牌两项空着, 摘要空着
    shape = rig.helper.audit_lines()[-1]
    assert (shape["action"], shape["result"]) == ("net.restore", "E_TOKEN_MISMATCH") and shape["token_id"] is None and shape["requested_by"] is None
    assert shape["params_sha256"] == protocol.params_fingerprint("{}") and shape["summary"] is None
    assert code_of(restore(rig, params_json='{"a":1}')) == "E_PARAMS"                                     # 参数没过: 令牌两项有, 摘要空着
    params = rig.helper.audit_lines()[-1]
    assert params["token_id"] and params["requested_by"] == "owner" and params["summary"] is None
    assert params["params_sha256"] == protocol.params_fingerprint('{"a":1}')
    assert code_of(restore(rig, token=False)) == "E_TOKEN_MISSING"
    assert rig.helper.audit_lines()[-1]["token_id"] is None and len(rig.helper.audit_lines()) == 4


def test_restore_never_calls_the_real_dns_flush(make_rig, monkeypatch):
    """红线: 恢复里的"清 DNS 缓存"也是假的 (往记录文件追加一行). 这个文件的夹具把底层的真调用换成了一调用就失败的函数, 这里再换一个会记数的, 核对一次都没调到."""
    calls: list[int] = []
    monkeypatch.setattr(win32, "_dns_flush_resolver_cache", lambda: calls.append(1) or True)
    rig = make_rig()
    for _ in range(3):
        assert steps_of(restore(rig)) == NOTHING_TO_DO
    assert calls == [] and rig.helper.agent_flush_log.read_text(encoding="utf-8").count("flush") == 3


# ====================================================================================================================
# 错误码都有测试
# ====================================================================================================================

def marked_codes(path: Path, marker: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name) and decorator.func.id == marker:
                    found |= {arg.value for arg in decorator.args if isinstance(arg, ast.Constant)}
    return found


def test_every_error_code_in_section_5_that_the_agent_or_the_guardian_can_give_has_a_test():
    here = Path(__file__).resolve().parent
    files = [here / "test_net_contract.py", here / "test_net_guardian.py"]
    agent = set().union(*(marked_codes(path, "covers_agent") for path in files))
    guardian = set().union(*(marked_codes(path, "covers_guardian") for path in files))
    assert agent == set(protocol.AGENT_CODES), sorted(set(protocol.AGENT_CODES) ^ agent)
    assert guardian == set(protocol.GUARDIAN_CODES), sorted(set(protocol.GUARDIAN_CODES) ^ guardian)
    assert not agent & set(protocol.HOST_CODES) and not guardian & set(protocol.HOST_CODES)          # 宿主的三个码不归这两个管

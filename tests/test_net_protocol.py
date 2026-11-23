"""网络插件·干跑 (1/3): 消息和参数 (wm/net/protocol.py), 对着《网络动作接口 v0.2》第 1、2、4.1、5 节. 全是纯的: 不起进程、不碰文件和系统.

覆盖: 常量和错误码清单; 一行 JSON 的编码; decode_request 的每种拒绝 (超长、不是 JSON 对象、重复的键、不认识的键、v、动作、各字段的类型)
和不抛异常; LineBuffer; start_params 的规范化和契约四组对拍向量; parse_start_params 的每个字段越界、不认识的键、重复的键、小数;
params_fingerprint; check_token 的每种情况和边界; 会话通道上几个动作的参数; 核心输出打码; protocol.py 本身是纯的 (ast 扫描).

第 2 轮评审追加的第 8、9 条 (契约第 1、2 节〔补 2〕): 请求这一行和 params_json 里的数只许整数、不带小数点和指数、绝对值不超过 2^53-1 (恰好 2^53-1 不拒),
嵌套最多 8 层 (最外面的对象算第 1 层; 恰好 8 层不拒), 开头不许有 BOM, 行尾认 LF 或 CRLF (别处的回车换行不行), 超过 64 KB 最先查、不解析、id 回 null,
"token"/"params_json" 写 null 等于没带、"dry_run" 写 null 不行; 令牌的两个时间必须是整数, now 向下取整到秒、两头都含, plugin 和 requested_by 是插件编号的写法 (另外 plugin 不能是 owner、ayane: 第 4 轮追加, 契约第 2 节〔补 4〕)、
issuer 是字母数字加 - (带换行的也拒), 不合回 E_TOKEN_MISMATCH."""
from __future__ import annotations

import ast
import hashlib
import json
import random
from pathlib import Path

import pytest
from net_fake_helper import UNRELATED_AUTOUSE_FIXTURES, foreign_wm_imports, skip_unrelated_autouse_fixtures

from wm.net import config_gen, protocol
from wm.net.protocol import (ERROR_CODES, MAX_REQUEST_BYTES, LineBuffer, Rejection, Request, check_token, decode_request, encode, fail, ok,
                             params_fingerprint, parse_start_params, start_params)
from wm.net.recipe import Recipe, RecipeError, Upstream

NOW = 1_800_000_000
skip_unrelated_autouse_fixtures(globals())      # 盖掉 conftest.py 里和网络无关的 autouse 夹具 (见 net_fake_helper.py)


def recipe(*processes: str, port: int = 1082, name: str = "NIKKE", minutes: int = 240) -> Recipe:
    return Recipe(name, processes or ("nikke.exe",), Upstream("127.0.0.1", port), minutes)


def good_text(**kw) -> str:
    return start_params(recipe(), core_id="mihomo", core_version="v1.19.34", **kw)


def good_dict() -> dict:
    return json.loads(good_text())


def dumps(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def line(**over) -> bytes:
    obj = {"v": 1, "id": "r1", "action": "heartbeat"}
    obj.update(over)
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def make_token(action: str = "net.start", params_json: str = "{}", *, now: int = NOW, **over) -> dict:
    token = {"id": "ab" * 16, "action": action, "params_sha256": params_fingerprint(params_json), "plugin": "net",
             "requested_by": "owner", "issued_at": now, "expires_at": now + 120, "issuer": "host-1"}
    token.update(over)
    return token


# ---- 常量和错误码 ---------------------------------------------------------------------------------------------------

def test_constants_follow_the_contract():
    assert protocol.VERSION == 1 and MAX_REQUEST_BYTES == 65536
    assert protocol.ACTIONS == ("net.start", "net.stop", "net.status", "net.restore", "heartbeat")
    assert protocol.SESSION_ACTIONS == ("net.stop", "net.status", "net.restore", "heartbeat")   # 会话通道上不认 net.start
    assert protocol.TOP_KEYS == ("v", "id", "action", "dry_run", "token", "params_json")
    assert protocol.STOP_REASONS == ("user", "lease_returned", "selftest_failed", "game_exited", "host_exit")
    assert (protocol.REASON_HEARTBEAT_LOST, protocol.REASON_MAX_TIME, protocol.REASON_CORE_EXITED, protocol.REASON_SYSTEM_SHUTDOWN) == (
        "heartbeat_lost", "max_time", "core_exited", "system_shutdown")
    assert protocol.REASON_RESTORE == "restore" and protocol.REASON_GUARDIAN_ERROR == "guardian_error"      # 后两个不是 net.stop 的 reason, 外面报不了
    assert "restore" not in protocol.STOP_REASONS and "guardian_error" not in protocol.STOP_REASONS


def test_error_codes_are_exactly_the_ones_in_section_5_of_the_contract():
    contract = {"E_VERSION", "E_BAD_REQUEST", "E_UNKNOWN_ACTION", "E_NOT_ELEVATED", "E_TOKEN_MISSING", "E_TOKEN_EXPIRED", "E_TOKEN_USED",
                "E_TOKEN_MISMATCH", "E_PARAMS", "E_CONFIG_MISMATCH", "E_CORE_UNKNOWN", "E_CORE_MISSING", "E_CORE_HASH",
                "E_ALREADY_RUNNING", "E_USER_DECLINED", "E_CONFIRM_TIMEOUT", "E_START_FAILED", "E_BUSY", "E_INTERNAL",
                "E_NOT_PERMITTED", "E_CARD_DECLINED", "E_UAC_DECLINED"}
    assert set(ERROR_CODES) == contract and len(ERROR_CODES) == len(contract)
    assert set(protocol.HOST_CODES) == {"E_NOT_PERMITTED", "E_CARD_DECLINED", "E_UAC_DECLINED"}
    assert set(protocol.GUARDIAN_CODES) == {"E_VERSION", "E_BAD_REQUEST", "E_UNKNOWN_ACTION", "E_PARAMS", "E_BUSY", "E_INTERNAL"}
    assert set(protocol.GUARDIAN_CODES) <= set(protocol.AGENT_CODES)
    assert not set(protocol.AGENT_CODES) & set(protocol.HOST_CODES)
    for code in ERROR_CODES:
        assert getattr(protocol, code) == code


# ---- 编码 -----------------------------------------------------------------------------------------------------------

def test_encode_is_one_utf8_line_ending_in_a_newline():
    data = encode({"a": 1, "b": "中文\n换行", "c": [1, 2]})
    assert data == '{"a":1,"b":"中文\\n换行","c":[1,2]}\n'.encode("utf-8")
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    assert json.loads(data) == {"a": 1, "b": "中文\n换行", "c": [1, 2]}


def test_encode_survives_a_lone_surrogate_by_escaping_everything():
    data = encode({"id": "\ud800x"})
    assert data.count(b"\n") == 1 and data.isascii()
    assert json.loads(data)["id"] == "\ud800x"


def test_encode_refuses_nan():
    with pytest.raises(ValueError):
        encode({"x": float("nan")})


def test_ok_and_fail_shapes():
    assert ok("r1", {"state": "running"}) == {"v": 1, "id": "r1", "ok": True, "result": {"state": "running"}}
    assert fail("r1", "E_PARAMS", "不对") == {"v": 1, "id": "r1", "ok": False, "error": {"code": "E_PARAMS", "message": "不对"}}
    assert fail(None, "E_PARAMS", "不对", ["upstream.port"])["error"] == {"code": "E_PARAMS", "message": "不对", "fields": ["upstream.port"]}
    assert fail("r1", "E_PARAMS", "不对", [])["error"]["fields"] == []


# ---- decode_request: 正常 -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("action", protocol.ACTIONS)
def test_every_action_decodes(action):
    got = decode_request(line(action=action))
    assert got == Request("r1", action, False, None, None)


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_the_line_may_or_may_not_carry_its_line_ending(ending):
    assert isinstance(decode_request(line().rstrip(b"\n") + ending), Request)


def test_text_input_is_accepted_too_and_json_whitespace_is_fine():
    assert isinstance(decode_request('{"v":1,"id":"r1","action":"net.status"}'), Request)
    assert isinstance(decode_request(b'  { "v" : 1 , "id" : "r1" , "action" : "net.status" }  \n'), Request)


def test_all_the_optional_fields_come_through_untouched():
    token = make_token()
    got = decode_request(line(action="net.start", dry_run=True, token=token, params_json='{"x":1}'))
    assert got == Request("r1", "net.start", True, token, '{"x":1}')
    # token: null 和没带一样
    assert decode_request(line(token=None)).token is None


def test_a_request_of_exactly_64_kb_is_accepted_and_one_byte_more_is_not():
    base = len(line(params_json="")) - 1                       # 不带换行的长度
    exact = line(params_json="x" * (MAX_REQUEST_BYTES - base))
    assert len(exact) - 1 == MAX_REQUEST_BYTES
    assert isinstance(decode_request(exact), Request)
    over = line(params_json="x" * (MAX_REQUEST_BYTES - base + 1))
    got = decode_request(over)
    assert isinstance(got, Rejection) and got.code == "E_BAD_REQUEST" and "64 KB" in got.message


# ---- decode_request: 拒绝 -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    b"", b"\n", b"hello", b"{", b'{"v":1,', b"[]", b'"x"', b"1", b"null", b"true",
    b"\xef\xbb\xbf" + line(),                                          # 带 BOM
    b'{"v":1,"id":"r\xff","action":"heartbeat"}\n',                    # 不是合法的 UTF-8
    b'{"v":1,\n"id":"r1","action":"heartbeat"}\n',                     # 不是一行
    b'{"v":1,"id":"r1","action":"heartbeat"}\r{"v":1}\n',              # 一行里塞了两个
    b'{"v":NaN,"id":"r1","action":"heartbeat"}\n',
    b'{"v":1,"id":"r1","action":"heartbeat","token":Infinity}\n',
    b'{"v":1,"v":1,"id":"r1","action":"heartbeat"}\n',                 # 重复的键 (顶层)
    b'{"v":1,"id":"r1","action":"heartbeat","token":{"id":"a","id":"b"}}\n',   # 重复的键 (嵌套)
    pytest.param(b"[" * 50000, id="deep-brackets"),                                                  # 嵌套太深
    pytest.param(b'{"v":1,"id":"r1","action":"heartbeat","token":' + b"[" * 5000 + b"]" * 5000 + b"}\n", id="deep-token"),
])
def test_not_a_json_object_on_one_line_is_a_bad_request(raw):
    got = decode_request(raw)
    assert isinstance(got, Rejection) and got.code == "E_BAD_REQUEST", got


def test_a_duplicate_key_names_the_key_and_a_wrong_input_type_is_a_bad_request():
    got = decode_request(b'{"v":1,"id":"r1","action":"heartbeat","action":"net.stop"}\n')
    assert got.code == "E_BAD_REQUEST" and "action" in got.message
    for odd in (None, 123, ["x"], {"v": 1}):
        assert decode_request(odd).code == "E_BAD_REQUEST"       # 不是文本也不抛


def test_unknown_top_level_keys_are_refused_not_ignored():
    got = decode_request(line(extra=1))
    assert got.code == "E_BAD_REQUEST" and "extra" in got.message and got.id == "r1"
    assert decode_request(line(**{"V": 1})).code == "E_BAD_REQUEST"        # 键名分大小写
    assert decode_request(line(params={"a": 1})).code == "E_BAD_REQUEST"    # 契约里的键是 params_json, 不是 params


@pytest.mark.parametrize("v", [2, 0, "1", True, None, [1], -1])
def test_a_wrong_v_is_e_version(v):
    got = decode_request(line(v=v))
    assert got.code == "E_VERSION" and got.id == "r1"


def test_a_missing_v_is_e_version():
    got = decode_request(b'{"id":"r1","action":"heartbeat"}\n')
    assert got.code == "E_VERSION"


# ---- decode_request: 数、嵌套、BOM、行尾、长度 (契约第 1 节〔补 2〕: 两种语言的解析器在这几处最容易判得不一样, 所以写死) ----------------------------

BOM = bytes([0xEF, 0xBB, 0xBF])
# 不是"绝对值不超过 2^53-1 的整数、写法不带小数点和指数"的数: 带小数点的、带指数的、超范围的 (恰好 2^53 和 64 位整数的最大值也在内)
BAD_NUMBERS = ["1.0", "1.5", "0.0", "-0.5", "1e2", "1E2", "1e+2", "1e-2", "1.0e2", "0e0", "9007199254740992", "-9007199254740992",
               "9223372036854775807", "123456789012345678", "99999999999999999999", "9" * 5000]
GOOD_NUMBERS = ["9007199254740991", "-9007199254740991", "9007199254740990", "0", "-0", "1", "-1", "1000000000000000"]   # 恰好 2^53-1 过得去


def raw_line(**fields: str) -> bytes:
    """一行请求, 每个字段的值是原样的 JSON 文本 (造 1.0、1e2、嵌套这些 json.dumps 写不出来的东西); 没给的 v、id、action 用合规的."""
    body = {"v": "1", "id": '"r1"', "action": '"heartbeat"'}
    body.update(fields)
    return ("{" + ",".join(f'"{key}":{value}' for key, value in body.items()) + "}\n").encode("utf-8")


def nested_text(levels: int, kind: str) -> str:
    """恰好嵌了 levels 层对象或数组的 JSON 文本 (levels >= 1; kind: array、object、mixed), 最里面放一个 1."""
    opens, closes = [], []
    for index in range(levels):
        use_array = kind == "array" or (kind == "mixed" and index % 2 == 0)
        opens.append("[" if use_array else '{"a":')
        closes.append("]" if use_array else "}")
    return "".join(opens) + "1" + "".join(reversed(closes))


def request_of_depth(depth: int, kind: str) -> bytes:
    """嵌套恰好 depth 层的一行请求 (最外面的请求对象算第 1 层, 其余的嵌在 token 里面)."""
    return raw_line() if depth == 1 else raw_line(token=nested_text(depth - 1, kind))


def test_the_nesting_and_number_limits_are_the_contracts():
    assert protocol.MAX_DEPTH == 8 and protocol.MAX_SAFE_INT == 2 ** 53 - 1 == 9007199254740991


@pytest.mark.parametrize("number", BAD_NUMBERS)
def test_a_number_that_is_not_a_plain_safe_integer_anywhere_in_the_line_is_a_bad_request(number):
    places = (raw_line(v=number),                                      # v: 数的规则先于 v 的检查, 所以是 E_BAD_REQUEST, 不是 E_VERSION
              raw_line(dry_run=number), raw_line(extra=number),
              raw_line(token='{"issued_at":%s}' % number),             # token 里面也在这一行里, 一样处理
              raw_line(token='{"a":[1,{"b":[%s]}]}' % number))         # 埋在里面
    for place in places:
        got = decode_request(place)
        assert isinstance(got, Rejection) and got.code == "E_BAD_REQUEST" and got.id is None, (number[:30], place[:60])


@pytest.mark.parametrize("number", GOOD_NUMBERS)
def test_integers_up_to_exactly_two_to_the_53_minus_one_are_not_refused_by_the_number_rule(number):
    got = decode_request(raw_line(token='{"issued_at":%s}' % number))
    assert isinstance(got, Request) and got.token == {"issued_at": int(number)}


def test_one_point_zero_for_v_is_a_bad_request_while_a_wrong_integer_or_a_string_is_still_e_version():
    assert decode_request(raw_line(v="1.0")).code == "E_BAD_REQUEST" and decode_request(raw_line(v="1e0")).code == "E_BAD_REQUEST"
    for v in ("2", "0", "-1", '"1"', "true", "null", "[1]"):
        got = decode_request(raw_line(v=v))
        assert got.code == "E_VERSION" and got.id == "r1", v


@pytest.mark.parametrize("kind", ["array", "object", "mixed"])
def test_exactly_8_levels_of_nesting_are_fine_and_9_are_a_bad_request(kind):
    for fine in (1, 2, 7, 8):
        assert isinstance(decode_request(request_of_depth(fine, kind)), Request), fine
    for deeper in (9, 10, 40):
        got = decode_request(request_of_depth(deeper, kind))
        assert isinstance(got, Rejection) and got.code == "E_BAD_REQUEST" and "嵌套" in got.message and got.id is None, deeper


def test_a_very_deep_line_is_a_bad_request_and_not_a_crash():
    for text in ("[" * 30000 + "1" + "]" * 30000, '{"a":' * 9000 + "1" + "}" * 9000):
        raw = raw_line(token=text)
        assert len(raw) <= MAX_REQUEST_BYTES
        got = decode_request(raw)
        assert isinstance(got, Rejection) and got.code == "E_BAD_REQUEST"


def test_a_bom_at_the_start_is_a_bad_request_but_a_bom_character_inside_a_string_is_just_text():
    assert isinstance(decode_request(line()), Request)
    for with_bom in (BOM + line(), BOM + line().rstrip(b"\n"), chr(0xFEFF) + line().decode("utf-8")):
        got = decode_request(with_bom)
        assert isinstance(got, Rejection) and got.code == "E_BAD_REQUEST" and "BOM" in got.message and got.id is None
    inside = decode_request(raw_line(id='"' + chr(0xFEFF) + 'x"'))
    assert isinstance(inside, Request) and inside.id == chr(0xFEFF) + "x"


def test_the_line_end_is_lf_or_crlf_and_a_carriage_return_or_line_feed_anywhere_else_is_a_bad_request():
    body = line().rstrip(b"\n")
    assert isinstance(decode_request(body + b"\n"), Request) and isinstance(decode_request(body + b"\r\n"), Request)
    for bad in (body + b"\r", body + b"\r\r\n", body + b"\n\r", b"\r" + body, body.replace(b",", b",\r", 1), body.replace(b",", b",\n", 1),
                body.replace(b":", b":\r\n", 1), body + b"\n" + body):
        got = decode_request(bad)
        assert isinstance(got, Rejection) and got.code == "E_BAD_REQUEST", bad
    buffered = LineBuffer().feed(body + b"\r\r\n" + body + b"\r\n")                  # 切行的那层只去掉一个结尾回车: 多出来的回车留给解析去拒
    assert [isinstance(decode_request(one), Request) for one in buffered] == [False, True]


def test_spaces_and_tabs_are_allowed_where_json_allows_whitespace():
    assert isinstance(decode_request(b'{ "v":\t1,\t"id" : "r1" ,"action":\t"heartbeat" }\t\n'), Request)


def test_the_size_limit_is_checked_first_nothing_is_parsed_and_the_id_is_null():
    pad = b"x" * (MAX_REQUEST_BYTES + 10)
    for over in (b'{"v":2,"id":"r1","action":"heartbeat","pad":"' + pad + b'"}\n',                # 解析了的话: v 不对 -> E_VERSION, id 是 r1
                 b'{"v":1,"id":"r1","action":"heartbeat","pad":"' + pad + bytes([0xFF]) + b'"}\n',   # 解析了的话: 不是合法的 UTF-8
                 b'{"v":1,"id":"r1",\n"action":"heartbeat","pad":"' + pad + b'"}\n',            # 先查换行的话: 说的是换行
                 BOM + pad, b'{"v":1,"id":"r1","action":"heartbeat","pad":' + b"[" * MAX_REQUEST_BYTES + b"]\n"):
        got = decode_request(over)
        assert isinstance(got, Rejection) and got.code == "E_BAD_REQUEST" and got.id is None and "64 KB" in got.message, over[:40]
        assert got.reply()["id"] is None


def test_a_null_token_or_params_json_is_the_same_as_leaving_them_out_but_a_null_dry_run_is_refused():
    got = decode_request(raw_line(token="null", params_json="null"))
    assert got == Request("r1", "heartbeat", False, None, None)
    assert check_token(got.token, action="net.start", params_json=got.params_json, now=NOW, used_ids=()) == "E_TOKEN_MISSING"
    refused = decode_request(raw_line(dry_run="null"))
    assert isinstance(refused, Rejection) and refused.code == "E_BAD_REQUEST" and refused.id == "r1"


def test_a_wrong_type_for_id_action_dry_run_or_params_json_is_a_bad_request():
    for over in ({"id": 5}, {"id": None}, {"id": ""}, {"action": 5}, {"action": None}, {"dry_run": "true"}, {"dry_run": 0}, {"params_json": 5},
                 {"params_json": {}}, {"params_json": []}, {"params_json": False}):
        assert decode_request(line(**over)).code == "E_BAD_REQUEST", over


@pytest.mark.parametrize("action", ["net.explode", "NET.START", "net.start ", "", "heartbeat\n", "net", "elevate", "run"])
def test_an_unknown_action_is_e_unknown_action(action):
    got = decode_request(line(action=action))
    assert got.code == "E_UNKNOWN_ACTION" and got.id == "r1"


@pytest.mark.parametrize("over", [{"action": None}, {"action": 1}, {"action": ["net.start"]}])
def test_an_action_that_is_not_a_string_is_a_bad_request(over):
    assert decode_request(line(**over)).code == "E_BAD_REQUEST"


def test_a_missing_action_or_id_is_a_bad_request():
    assert decode_request(b'{"v":1,"id":"r1"}\n').code == "E_BAD_REQUEST"
    assert decode_request(b'{"v":1,"action":"heartbeat"}\n').code == "E_BAD_REQUEST"
    assert decode_request(line(id="")).code == "E_BAD_REQUEST"
    for bad_id in (None, 5, ["r"]):
        got = decode_request(line(id=bad_id))
        assert got.code == "E_BAD_REQUEST" and got.id is None


@pytest.mark.parametrize("over", [{"dry_run": "false"}, {"dry_run": 1}, {"dry_run": None}, {"dry_run": []}])
def test_dry_run_must_be_a_boolean(over):
    assert decode_request(line(**over)).code == "E_BAD_REQUEST"


@pytest.mark.parametrize("params", [{"a": 1}, 5, ["x"], True])
def test_params_json_must_be_a_string(params):
    assert decode_request(line(params_json=params)).code == "E_BAD_REQUEST"


def test_a_params_json_with_a_lone_surrogate_is_refused_so_its_hash_is_always_defined():
    raw = b'{"v":1,"id":"r1","action":"net.status","params_json":"\\ud800"}\n'      # 线上写成 \ud800 这六个字符, 解出来是孤立代理字符
    got = decode_request(raw)
    assert got.code == "E_BAD_REQUEST" and got.id == "r1"


def test_checks_run_in_the_documented_order():
    assert decode_request(line(v=2, extra=1)).code == "E_VERSION"                # v 先于顶层的键
    assert decode_request(line(extra=1, action="nope")).code == "E_BAD_REQUEST"   # 键先于动作
    assert decode_request(line(action="nope", dry_run="x")).code == "E_UNKNOWN_ACTION"   # 动作先于 dry_run


def test_a_rejection_carries_the_request_id_only_when_there_is_a_string_one():
    got = decode_request(line(v=2, id="req-7"))
    assert got.id == "req-7" and got.reply() == {"v": 1, "id": "req-7", "ok": False,
                                                 "error": {"code": "E_VERSION", "message": got.message}}
    assert decode_request(b"not json").id is None
    assert decode_request(b"not json").reply()["id"] is None


def test_decode_request_never_raises_on_noise():
    rng = random.Random(20261111)
    samples = [bytes(rng.randrange(256) for _ in range(rng.randrange(0, 300))) for _ in range(300)]
    samples += [b'{"v":1,"id":"a","action":"net.start","token":' + bytes(rng.randrange(32, 127) for _ in range(40)) + b"}"
                for _ in range(100)]
    for sample in samples:
        assert isinstance(decode_request(sample), (Request, Rejection))


# ---- LineBuffer -------------------------------------------------------------------------------------------------------

def test_line_buffer_splits_lines_across_chunks_and_strips_cr():
    buf = LineBuffer()
    assert buf.feed(b"ab") == []
    assert buf.feed(b"c\nde") == [b"abc"]
    assert buf.feed(b"f\r\n\n") == [b"def", b""]
    assert buf.feed(b"x\ny\nz") == [b"x", b"y"]
    assert buf.feed(b"\n") == [b"z"]


def test_line_buffer_oversize_line_becomes_a_bad_request_and_the_next_line_is_fine():
    buf = LineBuffer()
    got = buf.feed(b"x" * 70000 + b"\n" + line(action="net.status"))
    assert len(got) == 2 and len(got[0]) == MAX_REQUEST_BYTES + 1
    assert decode_request(got[0]).code == "E_BAD_REQUEST"
    assert isinstance(decode_request(got[1]), Request)


def test_line_buffer_oversize_without_a_newline_does_not_grow_without_bound():
    buf = LineBuffer()
    first = buf.feed(b"x" * 40000)
    assert first == []
    flagged = buf.feed(b"x" * 40000)
    assert len(flagged) == 1 and len(flagged[0]) == MAX_REQUEST_BYTES + 1
    for _ in range(200):                                                  # 再灌 ~16 MB 没有换行的垃圾
        assert buf.feed(b"y" * 80000) == []
        assert len(buf._buf) <= MAX_REQUEST_BYTES + 2
    assert buf.feed(b"tail\n" + line()) == [line().rstrip(b"\n")]


def test_line_buffer_oversize_marker_is_not_mistaken_for_a_shorter_line_when_the_cut_falls_on_a_cr():
    """一条合法请求后面垫空格, 垫到第 limit + 1 个字节恰好是 \\r, 再跟垃圾: 切到 limit 个字节就是一条合法的请求, 切到 limit + 1 个字节
    再剥掉 \\r 也是 (那样超长的行就被当成合法请求放过了); 所以超长的标记要以 NUL 结尾, 不能以原来的字节结尾."""
    base = b'{"v":1,"id":"a","action":"heartbeat"}'
    raw = base + b" " * (MAX_REQUEST_BYTES - len(base)) + b"\r" + b"junk" * 10 + b"\n"
    for chunks in ([raw], [raw[:30000], raw[30000:]], [raw[:-3], raw[-3:]]):
        buf = LineBuffer()
        got = [item for chunk in chunks for item in buf.feed(chunk)]
        assert len(got) == 1 and len(got[0]) == MAX_REQUEST_BYTES + 1 and decode_request(got[0]).code == "E_BAD_REQUEST"


def test_line_buffer_a_line_of_exactly_the_limit_passes_unchanged():
    body = b"a" * MAX_REQUEST_BYTES
    assert LineBuffer().feed(body + b"\r\n") == [body]


# ---- 会话号和管道名 -------------------------------------------------------------------------------------------------

def test_session_ids_are_128_bit_lowercase_hex_and_make_the_pipe_name():
    session = "0123456789abcdef" * 2
    assert protocol.valid_session(session)
    assert protocol.pipe_name(session) == "\\\\.\\pipe\\wm-net-" + session
    for bad in ("", session[:-1], session + "0", session.upper(), session[:-1] + "g", "..\\" + session[3:], None, 5):
        assert not protocol.valid_session(bad)
        with pytest.raises(ValueError):
            protocol.pipe_name(bad)


# ---- params_fingerprint -----------------------------------------------------------------------------------------------

def test_params_fingerprint_is_the_sha256_of_the_utf8_bytes_of_the_text():
    assert params_fingerprint("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert params_fingerprint("原神") == hashlib.sha256("原神".encode("utf-8")).hexdigest()
    assert params_fingerprint("") == hashlib.sha256(b"").hexdigest()


def test_changing_one_byte_of_the_params_changes_the_fingerprint():
    text = good_text()
    seen = {params_fingerprint(text)}
    for index in range(0, len(text), 7):
        flipped = text[:index] + ("x" if text[index] != "x" else "y") + text[index + 1:]
        seen.add(params_fingerprint(flipped))
    assert len(seen) == len(range(0, len(text), 7)) + 1


# ---- start_params: 生成 ----------------------------------------------------------------------------------------------

VECTORS = [   # 契约第 7 节的四组对拍向量: (进程, 端口, 指纹)
    (("nikke.exe",), 1082, "9e6bf780287212dbc5aa6206377cd4e6feba051ae8d4bb1d4e2e89190a012510"),
    (("nikke.exe", "nikke_launcher.exe", "tbs_browser.exe"), 1083, "1c3877ff1b675dc4312672a445295f468ebb78cd02a0f9e7c211de938194f2f6"),
    (("Wuthering Waves.exe", "原神.exe"), 7897, "88a7a9666e2ee43115527a45c244064a7f40bbc611bf10f7fb3bb3e1a975a393"),
    (("NIKKE.exe", "nikke.exe"), 65535, "fee4505398cc60bd53712c429c3e1cb0bd47c4d464559d5d4b6b9aacd7b39888"),
]


@pytest.mark.parametrize("processes, port, fingerprint", VECTORS)
def test_start_params_carries_the_contract_vector_fingerprints(processes, port, fingerprint):
    text = start_params(recipe(*processes, port=port), core_id="mihomo", core_version="v1.19.34")
    assert json.loads(text)["config_sha256"] == fingerprint
    got, fields = parse_start_params(text)
    assert fields == [] and got.config_sha256 == fingerprint and got.recipe.processes == processes


def test_start_params_is_canonical_one_line_json_in_the_shape_of_section_4_1():
    text = good_text()
    assert text == dumps(json.loads(text)) and "\n" not in text and " " not in text
    assert json.loads(text) == {
        "template": 1,
        "recipe": {"name": "NIKKE", "processes": ["nikke.exe"], "max_minutes": 240},
        "upstream": {"host": "127.0.0.1", "port": 1082},
        "core": {"id": "mihomo", "version": "v1.19.34"},
        "config_sha256": config_gen.fingerprint(config_gen.build(recipe())),
        "heartbeat": {"interval_s": 5, "grace_s": 30}}
    assert text == good_text()                                        # 同样的输入, 同样的文本


def test_start_params_keeps_non_ascii_unescaped_and_the_recipe_order():
    text = start_params(recipe("Wuthering Waves.exe", "原神.exe", name="鸣潮"), core_id="mihomo", core_version="v1.19.34")
    assert "原神.exe" in text and "鸣潮" in text and "\\u" not in text
    assert json.loads(text)["recipe"]["processes"] == ["Wuthering Waves.exe", "原神.exe"]
    assert text.encode("utf-8").decode("utf-8") == text


def test_start_params_takes_the_heartbeat_settings_and_checks_what_it_writes():
    got, fields = parse_start_params(good_text(interval_s=1, grace_s=300))
    assert fields == [] and (got.interval_s, got.grace_s) == (1, 300)
    for bad in ({"interval_s": 0}, {"interval_s": 31}, {"grace_s": 9}, {"grace_s": 301}, {"interval_s": True}):
        with pytest.raises(ValueError):
            good_text(**bad)
    for bad_core in ({"core_id": ""}, {"core_id": "a b"}, {"core_id": "核心"}, {"core_version": "v" * 33}, {"core_version": "v/1"}):
        with pytest.raises(ValueError):
            start_params(recipe(), **{"core_id": "mihomo", "core_version": "v1.19.34", **bad_core})


def test_start_params_refuses_a_bad_recipe_the_same_way_config_gen_does():
    with pytest.raises(RecipeError):
        start_params(recipe("svchost.exe"), core_id="mihomo", core_version="v1")
    with pytest.raises(RecipeError):
        start_params(recipe("sing-box.exe"), core_id="mihomo", core_version="v1")
    with pytest.raises(RecipeError):                                   # 上游端口的属主不许点名
        start_params(recipe("game.exe"), core_id="mihomo", core_version="v1", upstream_owner="Game.EXE")


# ---- parse_start_params: 白名单 ------------------------------------------------------------------------------------------

def edited(path: str, value: object, *, drop: bool = False) -> str:
    """good_dict() 里改 (或删) 一项, 写成文本: path 是 recipe.max_minutes 这样的点分路径."""
    obj = good_dict()
    node = obj
    *head, last = path.split(".")
    for key in head:
        node = node[key]
    if drop:
        del node[last]
    else:
        node[last] = value
    return dumps(obj)


def test_the_good_params_pass_and_come_out_as_a_start_params():
    got, fields = parse_start_params(good_text())
    assert fields == [] and got is not None
    assert got.recipe == recipe() and got.core_id == "mihomo" and got.core_version == "v1.19.34"
    assert (got.interval_s, got.grace_s) == (5, 30) and got.config_sha256 == json.loads(good_text())["config_sha256"]
    assert isinstance(got.recipe.processes, tuple)


BOUNDARY_OK = [
    ("recipe.max_minutes", 1), ("recipe.max_minutes", 720), ("upstream.port", 1), ("upstream.port", 65535),
    ("upstream.host", "127.0.0.2"), ("upstream.host", "127.255.255.254"), ("heartbeat.interval_s", 1), ("heartbeat.interval_s", 30),
    ("heartbeat.grace_s", 10), ("heartbeat.grace_s", 300), ("core.id", "a" * 32), ("core.version", "v1.19.34-rc_1"),
    ("recipe.name", "n" * 32), ("recipe.name", "原神 a+b-c_d.e"), ("recipe.processes", [f"g{i}.exe" for i in range(16)]),
    ("recipe.processes", ["原神.exe", "Wuthering Waves.exe", "ゲーム.EXE", "게임.exe"]),
]


@pytest.mark.parametrize("path, value", BOUNDARY_OK)
def test_values_on_the_edge_of_the_allowed_range_pass(path, value):
    got, fields = parse_start_params(edited(path, value))
    assert fields == [] and got is not None


BAD_FIELDS = [   # (路径, 值, 应当报的字段)
    ("template", 2, "template"), ("template", True, "template"), ("template", "1", "template"), ("template", 0, "template"),
    ("recipe.name", "", "recipe.name"), ("recipe.name", "n" * 33, "recipe.name"), ("recipe.name", "a​b", "recipe.name"),
    ("recipe.name", "   ", "recipe.name"), ("recipe.name", "++", "recipe.name"), ("recipe.name", "ａ", "recipe.name"),
    ("recipe.name", "a\nb", "recipe.name"), ("recipe.name", 7, "recipe.name"), ("recipe.name", None, "recipe.name"),
    ("recipe.processes", [], "recipe.processes"), ("recipe.processes", [f"g{i}.exe" for i in range(17)], "recipe.processes"),
    ("recipe.processes", ["nikke"], "recipe.processes"), ("recipe.processes", ["../x.exe"], "recipe.processes"),
    ("recipe.processes", ["svchost.exe"], "recipe.processes"), ("recipe.processes", ["SVCHOST.EXE"], "recipe.processes"),
    ("recipe.processes", ["clash-verge.exe"], "recipe.processes"), ("recipe.processes", [1], "recipe.processes"),
    ("recipe.processes", "nikke.exe", "recipe.processes"), ("recipe.processes", [".exe"], "recipe.processes"),
    ("recipe.processes", ["a" * 61 + ".exe"], "recipe.processes"),
    ("recipe.max_minutes", 0, "recipe.max_minutes"), ("recipe.max_minutes", 721, "recipe.max_minutes"),
    ("recipe.max_minutes", -1, "recipe.max_minutes"), ("recipe.max_minutes", True, "recipe.max_minutes"),
    ("recipe.max_minutes", "240", "recipe.max_minutes"),
    ("upstream.host", "localhost", "upstream.host"), ("upstream.host", "0.0.0.0", "upstream.host"),
    ("upstream.host", "10.0.0.1", "upstream.host"), ("upstream.host", "192.168.1.2", "upstream.host"),
    ("upstream.host", "::1", "upstream.host"), ("upstream.host", "127.0.0.01", "upstream.host"),
    ("upstream.host", " 127.0.0.1", "upstream.host"), ("upstream.host", "127.1", "upstream.host"), ("upstream.host", 127, "upstream.host"),
    ("upstream.port", 0, "upstream.port"), ("upstream.port", 65536, "upstream.port"), ("upstream.port", -5, "upstream.port"),
    ("upstream.port", "1082", "upstream.port"), ("upstream.port", True, "upstream.port"),
    ("core.id", "", "core.id"), ("core.id", "a" * 33, "core.id"), ("core.id", "has space", "core.id"), ("core.id", "核心", "core.id"),
    ("core.id", "mi/homo", "core.id"), ("core.id", "a\n", "core.id"), ("core.id", 5, "core.id"),
    ("core.version", "", "core.version"), ("core.version", "v" * 33, "core.version"), ("core.version", "v1;rm", "core.version"),
    ("core.version", "ｖ1", "core.version"), ("core.version", None, "core.version"),
    ("config_sha256", "A" * 64, "config_sha256"), ("config_sha256", "a" * 63, "config_sha256"), ("config_sha256", "a" * 65, "config_sha256"),
    ("config_sha256", "g" * 64, "config_sha256"), ("config_sha256", 5, "config_sha256"), ("config_sha256", "", "config_sha256"),
    ("heartbeat.interval_s", 0, "heartbeat.interval_s"), ("heartbeat.interval_s", 31, "heartbeat.interval_s"),
    ("heartbeat.interval_s", True, "heartbeat.interval_s"), ("heartbeat.interval_s", "5", "heartbeat.interval_s"),
    ("heartbeat.grace_s", 9, "heartbeat.grace_s"), ("heartbeat.grace_s", 301, "heartbeat.grace_s"),
    ("heartbeat.grace_s", False, "heartbeat.grace_s"), ("heartbeat.grace_s", None, "heartbeat.grace_s"),
]


@pytest.mark.parametrize("path, value, field", BAD_FIELDS)
def test_each_field_out_of_range_is_named_in_fields(path, value, field):
    got, fields = parse_start_params(edited(path, value))
    assert got is None and fields == [field]


def test_unknown_keys_at_every_level_are_refused_and_named():
    for path in ("extra", "recipe.extra", "upstream.extra", "core.extra", "heartbeat.extra"):
        obj = good_dict()
        node = obj
        *head, last = path.split(".")
        for key in head:
            node = node[key]
        node[last] = 1
        got, fields = parse_start_params(dumps(obj))
        assert got is None and fields == [path]


def test_missing_keys_at_every_level_are_refused_and_named():
    for path in ("template", "recipe", "upstream", "core", "config_sha256", "heartbeat", "recipe.name", "recipe.processes",
                 "recipe.max_minutes", "upstream.host", "upstream.port", "core.id", "core.version", "heartbeat.interval_s",
                 "heartbeat.grace_s"):
        got, fields = parse_start_params(edited(path, None, drop=True))
        assert got is None and fields == [path], path


def test_a_section_that_is_not_an_object_is_named():
    for section in ("recipe", "upstream", "core", "heartbeat"):
        for odd in ([], "x", 5, None):
            got, fields = parse_start_params(edited(section, odd))
            assert got is None and fields == [section], (section, odd)


def test_several_problems_are_all_listed_once_each():
    obj = good_dict()
    obj["upstream"]["port"] = 0
    obj["recipe"]["max_minutes"] = 0
    obj["recipe"]["processes"] = ["svchost.exe", "explorer.exe"]          # 两条问题指向同一个字段, 只列一次
    obj["heartbeat"]["grace_s"] = 1
    got, fields = parse_start_params(dumps(obj))
    assert got is None and sorted(fields) == ["heartbeat.grace_s", "recipe.max_minutes", "recipe.processes", "upstream.port"]
    assert len(fields) == len(set(fields))
    problems = protocol.start_param_problems(dumps(obj))
    assert all(isinstance(field, str) and isinstance(why, str) and why for field, why in problems)


def test_decimals_and_exponent_numbers_are_refused_even_where_an_integer_would_do():
    text = good_text()
    for replaced in (('"port":1082', '"port":1082.0'), ('"max_minutes":240', '"max_minutes":2.4e2'), ('"interval_s":5', '"interval_s":5.0'),
                     ('"template":1', '"template":1.0'), ('"port":1082', '"port":1082e0')):
        assert replaced[0] in text
        assert parse_start_params(text.replace(*replaced)) == (None, ["params_json"])


def test_duplicate_keys_are_refused_at_any_level():
    text = good_text()
    assert parse_start_params(text.replace('"template":1', '"template":1,"template":1')) == (None, ["params_json"])
    assert parse_start_params(text.replace('"port":1082', '"port":1082,"port":1083')) == (None, ["params_json"])
    assert parse_start_params(text.replace('"name":"NIKKE"', '"name":"NIKKE","name":"x"')) == (None, ["params_json"])


@pytest.mark.parametrize("text", [None, 5, {"template": 1}, [], "", "   ", "null", "[]", '"x"', "5", "true", "{", "{}x", '{"template":NaN}',
                                  '{"template":Infinity}', '{"template":1}{"template":1}', pytest.param("[" * 5000, id="deep")])
def test_params_that_do_not_parse_into_one_object_are_a_single_params_json_problem(text):
    assert parse_start_params(text) == (None, ["params_json"])


def test_a_huge_integer_is_refused_without_a_slow_conversion():
    text = good_text().replace('"port":1082', '"port":' + "9" * 40000)
    assert parse_start_params(text) == (None, ["params_json"])


# ---- params_json 里的数、嵌套、BOM (同请求那一行的要求; 不合 -> E_PARAMS, fields 写 params_json) -----------------------------------------------

@pytest.mark.parametrize("number", BAD_NUMBERS)
def test_params_json_with_a_bad_number_anywhere_is_one_params_json_problem(number):
    for text in ('{"a":%s}' % number, "[%s]" % number, '{"a":[{"b":%s}]}' % number):
        with pytest.raises(protocol.ParamsError):
            protocol.parse_json_strict(text)
    assert parse_start_params(good_text().replace('"port":1082', '"port":' + number)) == (None, ["params_json"])
    assert [field for field, _ in protocol.parse_empty_params('{"a":%s}' % number)] == ["params_json"]
    stop = '{"session":"%s","reason":"user","x":%s}' % ("ab" * 16, number)
    assert [field for field, _ in protocol.parse_stop_params(stop, "ab" * 16)[1]] == ["params_json"]


@pytest.mark.parametrize("number", GOOD_NUMBERS)
def test_params_json_integers_up_to_two_to_the_53_minus_one_pass_the_number_rule(number):
    assert protocol.parse_json_strict('{"a":%s}' % number) == {"a": int(number)}
    # 越了白名单范围的字段说的是那个字段, 不是 params_json (数的规则没有拦它)
    fields = parse_start_params(good_text().replace('"max_minutes":240', '"max_minutes":' + number))[1]
    assert "params_json" not in fields and fields in ([], ["recipe.max_minutes"])


@pytest.mark.parametrize("kind", ["array", "object", "mixed"])
def test_params_json_nesting_is_at_most_8_levels_with_the_outermost_counting_as_the_first(kind):
    for fine in (1, 7, 8):
        assert protocol.parse_json_strict(nested_text(fine, kind)) is not None
    for deeper in (9, 10, 40, 100000):
        with pytest.raises(protocol.ParamsError, match="嵌套"):
            protocol.parse_json_strict(nested_text(deeper, kind))
    assert parse_start_params(nested_text(9, kind)) == (None, ["params_json"])
    assert [field for field, _ in protocol.parse_empty_params(nested_text(9, kind))] == ["params_json"]


def test_8_levels_of_objects_in_params_json_are_not_refused_by_the_nesting_rule():
    got, fields = parse_start_params(nested_text(8, "object"))          # 键不对是另外的问题, 说的是键, 不是 params_json
    assert got is None and "params_json" not in fields and "a" in fields


def test_params_json_with_a_bom_at_the_start_is_refused_but_a_bom_inside_a_string_is_text():
    bom = chr(0xFEFF)
    with pytest.raises(protocol.ParamsError, match="BOM"):
        protocol.parse_json_strict(bom + "{}")
    assert parse_start_params(bom + good_text()) == (None, ["params_json"])
    assert [field for field, _ in protocol.parse_empty_params(bom + "{}")] == ["params_json"]
    assert protocol.parse_json_strict('{"a":"' + bom + '"}') == {"a": bom}


def test_a_huge_but_wrong_process_list_is_judged_quickly():
    obj = good_dict()
    obj["recipe"]["processes"] = [f"g{i}.exe" for i in range(5000)]
    assert parse_start_params(dumps(obj)) == (None, ["recipe.processes"])


def test_params_failure_builds_the_e_params_reply():
    reply = protocol.params_failure("r9", protocol.start_param_problems(edited("upstream.port", 0)))
    assert reply["ok"] is False and reply["id"] == "r9"
    assert reply["error"]["code"] == "E_PARAMS" and reply["error"]["fields"] == ["upstream.port"] and "upstream.port" in reply["error"]["message"]


# ---- 会话通道上几个动作的参数 -----------------------------------------------------------------------------------------------

SESSION = "ab" * 16


def stop_text(**over) -> str:
    obj = {"session": SESSION, "reason": "user"}
    obj.update(over)
    return dumps(obj)


def test_stop_params_need_session_and_a_listed_reason():
    for reason in protocol.STOP_REASONS:
        assert protocol.parse_stop_params(stop_text(reason=reason), SESSION) == (reason, [])
    assert protocol.parse_stop_params(stop_text(), None) == ("user", [])


def test_stop_params_problems_name_the_field():
    def fields(text, session=SESSION):
        return [field for field, _ in protocol.parse_stop_params(text, session)[1]]

    assert fields(stop_text(reason="heartbeat_lost")) == ["reason"]          # 内部原因不许从外面报
    assert fields(stop_text(reason="")) == ["reason"] and fields(stop_text(reason=5)) == ["reason"]
    assert fields(stop_text(session="cd" * 16)) == ["session"] and fields(stop_text(session=5), None) == ["session"]
    assert fields(stop_text(extra=1)) == ["extra"]
    assert fields('{"reason":"user"}') == ["session"] and fields('{"session":"%s"}' % SESSION) == ["reason"]
    assert fields("{}") == ["session", "reason"]
    assert fields(None) == ["session", "reason"]
    assert fields("not json") == ["params_json"] and fields("[]") == ["params_json"]
    assert fields('{"session":"%s","reason":"user","reason":"user"}' % SESSION) == ["params_json"]      # 重复的键
    assert protocol.parse_stop_params(stop_text(reason="bogus"), SESSION)[0] is None


def test_actions_without_parameters_take_nothing_or_an_empty_object():
    assert protocol.parse_empty_params(None) == [] and protocol.parse_empty_params("{}") == [] and protocol.parse_empty_params(" { } ") == []
    assert [field for field, _ in protocol.parse_empty_params('{"a":1}')] == ["a"]
    for bad in ("[]", "x", '{"a":1,"a":2}', 5, '{"a":1.5}'):
        found = protocol.parse_empty_params(bad)
        assert found and found[0][0] in ("params_json", "a")


# ---- check_token -----------------------------------------------------------------------------------------------------

def verdict(token, *, action: str = "net.start", params_json: str | None = "{}", now: float = NOW, used=()) -> str | None:
    return check_token(token, action=action, params_json=params_json, now=now, used_ids=used)


def test_a_good_token_passes_and_the_edges_of_its_life_are_exact():
    assert verdict(make_token()) is None
    assert verdict(make_token(), now=NOW) is None                       # issued_at == now
    assert verdict(make_token(), now=NOW + 120) is None                 # now == expires_at: 还有效
    assert verdict(make_token(), now=NOW + 120.5) is None               # 向下取整以后还是 expires_at: 有效 (晚一整秒才过期)
    assert verdict(make_token(), now=NOW + 121) == "E_TOKEN_EXPIRED"
    assert verdict(make_token(expires_at=NOW + 1), now=NOW + 1) is None


def test_time_is_compared_after_flooring_now_to_the_second_and_both_ends_are_inclusive():
    token = make_token()                                               # issued_at == NOW, expires_at == NOW + 120
    assert verdict(token, now=NOW) is None and verdict(token, now=NOW + 120) is None          # 两头都含
    assert verdict(token, now=NOW + 0.999) is None                                            # 向下取整: t == NOW
    assert verdict(token, now=NOW - 0.001) == "E_TOKEN_MISMATCH"                              # t == NOW - 1, 比 issued_at 小
    assert verdict(token, now=NOW - 1) == "E_TOKEN_MISMATCH"
    assert verdict(token, now=NOW + 120.999) is None                                          # 向下取整: t == expires_at
    assert verdict(token, now=NOW + 121) == "E_TOKEN_EXPIRED" and verdict(token, now=NOW + 121.0001) == "E_TOKEN_EXPIRED"   # 晚一秒过期
    later = make_token(issued_at=NOW + 1, expires_at=NOW + 121)
    assert verdict(later, now=NOW + 0.9999) == "E_TOKEN_MISMATCH" and verdict(later, now=NOW + 1.5) is None
    assert verdict(later, now=NOW + 121.5) is None and verdict(later, now=NOW + 122) == "E_TOKEN_EXPIRED"


@pytest.mark.parametrize("key", ["issued_at", "expires_at"])
@pytest.mark.parametrize("bad", [1.0, 1800000000.5, 1.8e9, "1800000000", True, False, None, float("inf"), float("nan"), [1], {"a": 1}])
def test_the_two_token_times_must_be_integers(key, bad):
    assert verdict(make_token(**{key: bad})) == "E_TOKEN_MISMATCH"


PLUGIN_IDS_OK = ["net", "a", "0", "a-b", "9a", "a" * 48, "a-" * 24, "net-2", "a--b", "a-", "owner-2", "my-owner", "ayane2", "owner0", "ayan"]   # (owner、ayane 本身另测: 它们当 requested_by 合法, 当 plugin 不合法)
PLUGIN_IDS_BAD = ["", "-a", "A", "Net", "a_b", "a b", "a.b", "a/b", "net\n", "\nnet", "net\r\n", "net\r", "a" * 49, "网络", "ａ", "net\x00", " net", "net ",
                  "ne t", "owner!", "../x", "a\tb", "a" * 47 + "\n"]
ISSUERS_OK = ["host-1", "ABC-123", "x", "A" * 64, "0-0", "Fake-Host-1"]
ISSUERS_BAD = ["", "a_b", "a b", "a.b", "x\n", "\nx", "x\r\n", "主机", "ｘ", "a" * 65, "-" * 65, "host 1", "host/1", "a\x00"]


@pytest.mark.parametrize("key", ["plugin", "requested_by"])
def test_plugin_and_requested_by_must_be_written_like_a_plugin_id(key):
    for good in PLUGIN_IDS_OK:
        assert verdict(make_token(**{key: good})) is None, good
    for bad in PLUGIN_IDS_BAD:
        assert verdict(make_token(**{key: bad})) == "E_TOKEN_MISMATCH", repr(bad)       # 表外的字符、超长、带换行 (包括结尾的换行) 都拒


def test_a_token_whose_plugin_is_owner_or_ayane_is_refused_but_requested_by_may_be_either():
    """契约第 2 节〔补 4〕: owner、ayane 不能拿来当插件编号; 令牌的 plugin 写成这两个词, 按形状不对拒 (E_TOKEN_MISMATCH): 提权一侧不指望普通权限的扩展登记表来保证这两个词没被占.
    requested_by 里它们照旧合法 ("用户自己"、"她"). token_shape_ok 跟着对 (审计里这种令牌的令牌号和谁申请的空着)."""
    assert protocol.RESERVED_PLUGIN_IDS == ("owner", "ayane")
    for word in ("owner", "ayane"):
        assert protocol.token_shape_ok(make_token(plugin=word)) is False and verdict(make_token(plugin=word)) == "E_TOKEN_MISMATCH", word
        assert protocol.token_shape_ok(make_token(requested_by=word)) is True and verdict(make_token(requested_by=word)) is None, word
        assert verdict(make_token(plugin=word, requested_by=word)) == "E_TOKEN_MISMATCH"             # 两项都写成这个词: plugin 那一项不对
        assert verdict(make_token(plugin=word), now=NOW + 999, used={"ab" * 16}) == "E_TOKEN_MISMATCH"   # 形状在时间和"用过"之前
    for upper in ("Owner", "OWNER", "AYANE", "Ayane"):
        assert verdict(make_token(plugin=upper)) == "E_TOKEN_MISMATCH", upper                          # 大写本来就不合插件编号的写法
    for fine in ("owner-2", "my-owner", "ayane2", "owner0", "ayan", "o", "net"):                       # 只拒这两个词本身: 以它们开头、结尾或者相近的写法照常
        assert verdict(make_token(plugin=fine)) is None, fine


def test_issuer_is_letters_digits_and_hyphens_one_to_64_characters():
    for good in ISSUERS_OK:
        assert verdict(make_token(issuer=good)) is None, good
    for bad in ISSUERS_BAD:
        assert verdict(make_token(issuer=bad)) == "E_TOKEN_MISMATCH", repr(bad)
    assert verdict(make_token(issuer=None)) == "E_TOKEN_MISMATCH" and verdict(make_token(issuer=5)) == "E_TOKEN_MISMATCH"


def test_the_shape_of_the_three_strings_is_judged_before_the_time_and_the_other_checks():
    assert verdict(make_token(plugin="Bad"), now=NOW + 999, used={"ab" * 16}) == "E_TOKEN_MISMATCH"     # 过期的、用过的也先看形状
    assert verdict(make_token(issuer="a b"), now=NOW + 999) == "E_TOKEN_MISMATCH"


def test_token_shape_ok_is_the_shape_check_alone_and_agrees_with_check_token():
    """公开成一个函数, 给提权一侧的审计用 (契约第 8 节〔补 3〕: 令牌号和谁申请的在令牌的形状通过以后才写): 只看形状, 不看过期、用过、动作和指纹对不对;
    不是对象的 (包括没带的 None) 都不算形状对."""
    assert protocol.token_shape_ok(make_token()) is True
    for odd in (None, "tok", 5, [], True, 1.5, {}):
        assert protocol.token_shape_ok(odd) is False
    for key in protocol.TOKEN_KEYS:
        token = make_token()
        del token[key]
        assert protocol.token_shape_ok(token) is False, key
    for over in ({"extra": 1}, {"id": "zz" * 16}, {"id": "ab" * 15}, {"plugin": "Net"}, {"plugin": "owner"}, {"plugin": "ayane"}, {"requested_by": "owner\n"},
                 {"issuer": "a b"}, {"params_sha256": "g" * 64},
                 {"issued_at": 1.5}, {"issued_at": True}, {"expires_at": NOW}, {"expires_at": NOW + 121}, {"action": "net.stop"}):
        assert protocol.token_shape_ok(make_token(**over)) is False, over
        assert verdict(make_token(**over)) == "E_TOKEN_MISMATCH", over                  # check_token 对形状不对的令牌回的就是 E_TOKEN_MISMATCH
    # 形状对、别的不对 (过期、还没到时候、用过、动作或指纹对不上): 形状照样是对的; 这几种 check_token 回的码各不相同, 只有形状函数分得出"形状没过"
    for token, kwargs, code in ((make_token(), {"now": NOW + 500}, "E_TOKEN_EXPIRED"), (make_token(), {"now": NOW - 5}, "E_TOKEN_MISMATCH"),
                                (make_token(), {"used": {"ab" * 16}}, "E_TOKEN_USED"), (make_token("net.restore"), {}, "E_TOKEN_MISMATCH"),
                                (make_token(params_sha256="0" * 64), {}, "E_TOKEN_MISMATCH")):
        assert protocol.token_shape_ok(token) is True and verdict(token, **kwargs) == code


def test_the_plugin_id_pattern_is_the_registrys_but_written_again_here_and_wm_agent_is_not_imported():
    """wm/net 不许导入 wm.agent, 所以插件编号的写法在 protocol.py 里另写了一份; 这条读登记表的源码 (不导入), 核对两份写法一致 (不一致就该回来改)."""
    source = Path(__file__).resolve().parents[1] / "wm" / "agent" / "extensions.py"
    if not source.is_file():
        pytest.skip("wm/agent/extensions.py 不在")
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        pytest.skip("wm/agent/extensions.py 读不了, 或者别人正在改它 (语法不完整)")
    found = [node.value.args[0].value for node in ast.walk(tree)
             if isinstance(node, ast.Assign) and any(getattr(target, "id", "") == "ID" for target in node.targets)
             and isinstance(node.value, ast.Call) and node.value.args and isinstance(node.value.args[0], ast.Constant)]
    if not found:
        pytest.skip("登记表里没找到 ID = re.compile(...)")
    assert found[0] == "^" + protocol._PLUGIN_ID.pattern + "$"
    text = (Path(protocol.__file__)).read_text(encoding="utf-8")
    assert "wm.agent" not in "\n".join(item for item in text.splitlines() if item.lstrip().startswith(("import ", "from ")))


def test_no_token_is_missing_and_whatever_is_not_an_object_is_a_mismatch():
    assert verdict(None) == "E_TOKEN_MISSING"
    for odd in ("tok", 5, [], True, 1.5):
        assert verdict(odd) == "E_TOKEN_MISMATCH"


@pytest.mark.parametrize("key", protocol.TOKEN_KEYS)
def test_a_token_missing_any_field_is_a_mismatch(key):
    token = make_token()
    del token[key]
    assert verdict(token) == "E_TOKEN_MISMATCH"


@pytest.mark.parametrize("over", [
    {"id": ""}, {"id": "abc"}, {"id": "zz" * 16}, {"id": "ab" * 15}, {"id": 5}, {"id": "ab" * 65},          # 令牌号: 至少 128 位十六进制
    {"action": ""}, {"action": 5}, {"action": "net.stop"}, {"action": "heartbeat"},
    {"params_sha256": "x" * 64}, {"params_sha256": "ab" * 31}, {"params_sha256": 5},
    {"plugin": ""}, {"plugin": None}, {"requested_by": ""}, {"requested_by": 7}, {"issuer": ""}, {"issuer": []},
    {"issued_at": "1800000000"}, {"issued_at": True}, {"issued_at": None}, {"expires_at": "x"}, {"expires_at": float("inf")},
    {"expires_at": NOW}, {"expires_at": NOW - 5},                                    # 有效期不是正的
    {"expires_at": NOW + 121},                                                       # 有效期超过 120 秒
    {"issued_at": NOW + 1, "expires_at": NOW + 100},                                 # issued_at 晚于 now
    {"extra": 1},                                                                    # 多出来的字段也不收
])
def test_a_malformed_or_future_dated_token_is_a_mismatch(over):
    assert verdict(make_token(**over)) == "E_TOKEN_MISMATCH"


def test_an_expired_token_is_expired_even_when_it_was_also_used():
    token = make_token()
    assert verdict(token, now=NOW + 500, used={token["id"]}) == "E_TOKEN_EXPIRED"


def test_a_used_token_is_refused_whatever_the_case_of_its_id():
    token = make_token()
    assert verdict(token, used={token["id"]}) == "E_TOKEN_USED"
    assert verdict(token, used=[token["id"].upper()]) == "E_TOKEN_USED"
    assert verdict(make_token(id="AB" * 16), used={"ab" * 16}) == "E_TOKEN_USED"
    assert verdict(token, used={"cd" * 16}) is None and verdict(token, used=()) is None


def test_a_token_for_another_action_or_other_params_is_a_mismatch():
    assert verdict(make_token("net.restore")) == "E_TOKEN_MISMATCH"                  # 请求是 net.start
    assert verdict(make_token("net.start"), action="net.restore") == "E_TOKEN_MISMATCH"
    assert verdict(make_token("net.restore"), action="net.restore") is None
    assert verdict(make_token(params_json="{}"), params_json="{ }") == "E_TOKEN_MISMATCH"    # 改一个字节就对不上
    assert verdict(make_token(), params_json=None) == "E_TOKEN_MISMATCH"
    assert verdict(make_token(params_sha256="0" * 64)) == "E_TOKEN_MISMATCH"


def test_every_single_byte_edit_of_real_params_breaks_the_token():
    text = good_text()
    token = make_token(params_json=text)
    assert verdict(token, params_json=text) is None
    for index in range(0, len(text), 5):
        edited_text = text[:index] + ("0" if text[index] != "0" else "1") + text[index + 1:]
        assert verdict(token, params_json=edited_text) == "E_TOKEN_MISMATCH", index


def test_the_params_hash_is_compared_without_regard_to_the_case_of_its_hex():
    token = make_token(params_sha256=params_fingerprint("{}").upper())
    assert verdict(token) is None


def test_the_order_of_the_four_checks():
    # 没带 > 形状 > 过期 > 用过 > 动作 > 指纹
    token = make_token("net.restore", "{}", params_sha256="0" * 64)
    assert verdict(token, now=NOW + 999, used={token["id"]}) == "E_TOKEN_EXPIRED"
    assert verdict(token, used={token["id"]}) == "E_TOKEN_USED"
    assert verdict(token) == "E_TOKEN_MISMATCH"


# ---- 核心输出打码 -----------------------------------------------------------------------------------------------------

def test_masking_hides_addresses_and_hosts_but_not_file_names():
    masked = protocol.mask_output_line("dial tcp 203.0.113.7:443: connect failed; resolve www.example.com:8443 [2001:db8::1]:443")
    assert "203.0.113" not in masked and "example" not in masked and "2001" not in masked and masked.count("***") == 3
    for plain in ("read config.yaml failed", "WMFAKE match process=nikke.exe via=upstream", "Wuthering Waves.exe started",
                  "core v1.19.34 start", "adapter WM-Ayane missing"):
        assert protocol.mask_output_line(plain) == plain


def test_masking_cleans_control_characters_and_cuts_long_lines():
    assert protocol.mask_output_line("a\x00b\x1b[31mc\td") == "a b [31mc d"
    long = protocol.mask_output_line("x" * 1000)
    assert len(long) == 201 and long.endswith("…")
    assert protocol.mask_output_line("  padded  ") == "padded"
    assert protocol.mask_output_line("") == ""


# ---- 盖 conftest 夹具的守卫 (net_fake_helper.skip_unrelated_autouse_fixtures) -----------------------------------------------

FOREIGN_IMPORTS = {   # 源码 -> 该被抓到的模块名 (wm.net 以外的项目模块)
    "import wm\n": ["wm"], "import wm.services\n": ["wm.services"], "from wm.services import up\n": ["wm.services"],
    "from wm import services\n": ["wm.services"], "from wm import net, services\n": ["wm.services"], "from wm import *\n": ["wm.*"],
    "from wm.privilege import is_elevated\n": ["wm.privilege"], "from wm.net_other import x\n": ["wm.net_other"],
    "def f():\n    import wm.workbench\n": ["wm.workbench"], "import importlib\nimportlib.import_module('wm.agent')\n": ["wm.agent"],
    "__import__('wm.channels')\n": ["wm.channels"], "import json, wm.agent.core\n": ["wm.agent.core"],
}
NET_ONLY_IMPORTS = ["import json\n", "from wm.net import protocol\n", "import wm.net.protocol as p\n", "from wm import net\n", "import wm.net\n",
                    "from wm.net.guardian import Guardian\n", "from . import local\n", "import wmpet\n", "from wmx import y\n",
                    "importlib.import_module('wm.net.win32')\n", "x = 'from wm.services import up'  # 字符串和注释里提到不算\n"]


@pytest.mark.parametrize("source, expected", list(FOREIGN_IMPORTS.items()))
def test_the_fixture_override_guard_catches_every_way_of_importing_other_wm_modules(source, expected):
    found = foreign_wm_imports(source)
    assert [item.split(": ", 1)[1] for item in found] == expected, found
    assert all(item.startswith("第 ") for item in found)


def test_the_fixture_override_guard_lets_wm_net_and_everything_else_through():
    for source in NET_ONLY_IMPORTS:
        assert foreign_wm_imports(source) == [], source


def test_overriding_the_conftest_fixtures_is_refused_for_a_file_that_imports_other_wm_modules(tmp_path):
    """给检查一段导入了 wm.services 的假源码: 它要响 (直接抛错, 测试文件一被收集就失败), 而且没有往命名空间里放任何夹具."""
    bad = tmp_path / "test_bad.py"
    bad.write_text("from wm.services import up\n\n\ndef test_x():\n    pass\n", encoding="utf-8")
    namespace: dict = {"__file__": str(bad)}
    with pytest.raises(RuntimeError, match="wm.services") as error:
        skip_unrelated_autouse_fixtures(namespace)
    assert "test_bad.py" in str(error.value) and set(namespace) == {"__file__"}
    good = tmp_path / "test_good.py"
    good.write_text("from wm.net import protocol\n\n\ndef test_x():\n    pass\n", encoding="utf-8")
    namespace = {"__file__": str(good)}
    skip_unrelated_autouse_fixtures(namespace)
    assert set(namespace) == {"__file__", *UNRELATED_AUTOUSE_FIXTURES}      # 盖掉了 conftest 里那 8 个无关的 autouse 夹具
    with pytest.raises(RuntimeError, match="__file__"):                     # 查不了的 (没有 __file__) 也不放过
        skip_unrelated_autouse_fixtures({})


def test_the_net_test_files_and_the_helper_import_nothing_from_wm_but_wm_net():
    here = Path(__file__).resolve().parent
    for name in ("test_net_protocol.py", "test_net_guardian.py", "test_net_contract.py", "net_fake_helper.py", "net_fake_core.py"):
        assert foreign_wm_imports((here / name).read_text(encoding="utf-8")) == [], name


# ---- protocol.py 本身是纯的 -----------------------------------------------------------------------------------------------

def test_protocol_module_only_imports_pure_standard_library_pieces():
    path = Path(__file__).resolve().parents[1] / "wm" / "net" / "protocol.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
    assert imported <= {"__future__", "hashlib", "json", "math", "re", "collections.abc", "dataclasses", ".", ".recipe"}, imported
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not calls & {"open", "print", "input", "exec", "eval", "__import__"}

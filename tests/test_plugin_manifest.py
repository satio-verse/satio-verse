"""插件清单 `plugin` 块的校验 (急7: 渠道和世界合成一种接口). 契约: 知识库 09 问题与待办/世界接口契约（2026-11-15）.md

只测纯函数, 不碰网络、真家目录、真 key; 引用文件的检查用 tmp_path 里造的包目录.
"""
import copy
import json
import re
from pathlib import Path

import pytest

from wm.agent import plugin_manifest as pm

SAMPLES = Path(__file__).parent / "plugin_manifests"
NAMES = ("qq-onebot", "weixin-ilink", "local-guest", "plaza-2d")
DEL = object()


def load(name):
    return json.loads((SAMPLES / (name + ".json")).read_text(encoding="utf-8"))


def _tokens(path):
    return [a or int(b) for a, b in re.findall(r"([^.\[\]]+)|\[(\d+)\]", path)]


def edit(manifest, path, value):
    """改好样例的一处. value 可以是 DEL (删掉)、函数 (拿旧值算新值) 或直接的新值."""
    toks = _tokens(path)
    node = manifest
    for t in toks[:-1]:
        node = node[t]
    last = toks[-1]
    if value is DEL:
        del node[last]
    elif callable(value):
        node[last] = value(node[last])
    else:
        node[last] = value


def make(name, *edits):
    m = copy.deepcopy(load(name))
    for path, value in edits:
        edit(m, path, value)
    return m


def errors(issues):
    return [i for i in issues if i.level == "error"]


def package_for(tmp_path, name):
    """按样例引用的文件在 tmp_path 里造一个包目录."""
    for rel in ("fixtures/twin-far-pet.json", "ENV_PROMPT.md", "spectate.html"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}", encoding="utf-8")
    return tmp_path


# ---- 合格的 ----

@pytest.mark.parametrize("name", NAMES)
def test_the_four_sample_manifests_pass(name, tmp_path):
    m = load(name)
    assert pm.validate_plugin_manifest(m) == []
    assert pm.validate_plugin_manifest(m, package_for(tmp_path, name)) == []


def test_a_third_party_chat_plugin_that_stays_at_acquaintance_is_fine():
    m = make("qq-onebot", ("id", "telegram"), ("plugin.tier", "process"), ("plugin.world.prefix", "tg"),
             ("plugin.permissions", {"network": ["api.telegram.org"], "notify": False}),
             ("plugin.service", {"cmd": ["python", "tg.py"], "start": "always"}),
             ("plugin.world.actions", [{"name": "tg_send", "kind": "act", "outward": True, "receipt": "send_result", "why": "回复"}]),
             ("plugin.world.trust_max", "acquaintance"), ("plugin.world.caps.needs_proxy", True))
    assert pm.validate_plugin_manifest(m) == []
    assert pm.policy_issues(m, "third_party") == []
    assert pm.separate_permissions(m) == []


def test_every_chat_sample_without_a_hidden_truth_needs_no_fixtures_or_spectate_panel():
    for name in ("qq-onebot", "weixin-ilink", "local-guest"):
        m = load(name)
        assert "perception" not in m["plugin"]["world"] and "spectate" not in m["plugin"]["world"]
        assert pm.validate_plugin_manifest(m) == []


def test_an_old_manifest_without_a_plugin_block_gets_one_warning_and_no_error():
    old = {"id": "workbench-jobs", "name": "后台作业", "description": "x", "mcp": {"command": "python", "args": ["server.py"]}, "ui": None}
    issues = pm.validate_plugin_manifest(old)
    assert [(i.path, i.code, i.level) for i in issues] == [("plugin", "W_NO_PLUGIN_BLOCK", "warn")]
    assert not pm.has_errors(issues)


@pytest.mark.parametrize("kind", ["space", "game"])
@pytest.mark.parametrize("word", ["role", "owner", "permission", "permissions", "auth"])
def test_role_owner_permission_auth_are_ordinary_fields_in_games_and_plazas(kind, word):
    m = make("plaza-2d", ("plugin.world.kind", kind), ("plugin.world.perception.visible", lambda v: v + [word]),
             ("plugin.world.events[0].fields", lambda f: f + [word]))
    assert pm.validate_plugin_manifest(m) == []


def test_the_same_words_are_still_refused_in_a_chat_world_and_the_explicit_trust_names_everywhere():
    for word in ("role", "owner", "permission", "permissions", "auth"):
        m = make("qq-onebot", ("plugin.world.perception", {"visible": [word]}), ("plugin.world.events[0].fields", [word]))
        assert ("plugin.world.events[0].fields[0]", "E_EVENT_FIELD_TRUST") in {(i.path, i.code) for i in pm.validate_plugin_manifest(m)}
    for kind in ("space", "game"):
        for word in ("trust", "trust_tier", "trust_level", "is_owner", "is_master", "verified_owner", "privilege"):
            m = make("plaza-2d", ("plugin.world.kind", kind), ("plugin.world.perception.visible", lambda v: v + [word]),
                     ("plugin.world.events[0].fields", lambda f: f + [word]))
            assert ("plugin.world.events[0].fields[2]", "E_EVENT_FIELD_TRUST") in {(i.path, i.code) for i in pm.validate_plugin_manifest(m)}


def test_the_stored_fields_that_create_adds_are_accepted():
    m = make("plaza-2d")
    m.update({"revision": "a" * 24, "sha256": {"x": "0"}, "created": 1.0, "by": "assistant", "validation": {"static": "passed"}})
    assert pm.validate_plugin_manifest(m) == []


# ---- 不合格的: (名字, 样例, 改动, 期望的 path, 期望的 code[, 消息里该有的字]) ----

BAD = [
    # 信任档和两项权限
    ("owner_without_permission", "qq-onebot", [("plugin.permissions.channel", {})], "plugin.world.trust_max", "E_TRUST_MAX_NEEDS_OWNER_PERMISSION"),
    ("owner_without_permission_block", "qq-onebot", [("plugin.permissions.channel", DEL)], "plugin.world.trust_max", "E_TRUST_MAX_NEEDS_OWNER_PERMISSION"),
    ("permission_without_owner_tier", "qq-onebot", [("plugin.world.trust_max", "acquaintance")], "plugin.permissions.channel.act_as_owner", "E_OWNER_PERMISSION_WITHOUT_TRUST"),
    ("answer_cards_without_owner", "qq-onebot", [("plugin.world.trust_max", "acquaintance"), ("plugin.permissions.channel", {"answer_cards": True})],
     "plugin.permissions.channel.answer_cards", "E_ANSWER_CARDS_NEEDS_OWNER"),
    ("loopback_cannot_be_owner", "local-guest", [("plugin.world.trust_max", "owner_remote"), ("plugin.permissions.channel", {"act_as_owner": True})],
     "plugin.world.trust_max", "E_TRUST_FROM_LOOPBACK_MAX"),
    ("no_identity_means_stranger_only", "local-guest", [("plugin.world.trust_from", "none")], "plugin.world.trust_max", "E_TRUST_FROM_NONE_MAX"),
    ("paired_owner_has_one_peer", "weixin-ilink", [("plugin.world.caps", {"dm": True, "group": True, "group_sees": "all"})], "plugin.world.caps.group", "E_PAIRED_OWNER_GROUP"),
    ("trust_from_not_in_list", "qq-onebot", [("plugin.world.trust_from", "claims_to_be_owner")], "plugin.world.trust_from", "E_ENUM"),
    ("trust_max_not_in_list", "qq-onebot", [("plugin.world.trust_max", "root")], "plugin.world.trust_max", "E_ENUM"),
    ("permission_flag_not_bool", "qq-onebot", [("plugin.permissions.channel.act_as_owner", "yes")], "plugin.permissions.channel.act_as_owner", "E_NOT_BOOL"),
    ("channel_permissions_not_object", "qq-onebot", [("plugin.permissions.channel", True)], "plugin.permissions.channel", "E_NOT_OBJECT"),
    # 事件
    ("event_carries_is_owner", "qq-onebot", [("plugin.world.events[0].fields", lambda f: f + ["is_owner"])], "plugin.world.events[0].fields[3]", "E_EVENT_FIELD_TRUST"),
    ("event_carries_trust_tier", "qq-onebot", [("plugin.world.events[0].fields", lambda f: f + ["trust_tier"])], "plugin.world.events[0].fields[3]", "E_EVENT_FIELD_TRUST"),
    ("chat_event_carries_role", "qq-onebot", [("plugin.world.events[0].fields", lambda f: f + ["role"])], "plugin.world.events[0].fields[3]", "E_EVENT_FIELD_TRUST"),
    ("chat_event_carries_owner", "qq-onebot", [("plugin.world.events[0].fields", lambda f: f + ["owner"])], "plugin.world.events[0].fields[3]", "E_EVENT_FIELD_TRUST"),
    ("chat_event_carries_permission", "qq-onebot", [("plugin.world.events[0].fields", lambda f: f + ["permission"])], "plugin.world.events[0].fields[3]", "E_EVENT_FIELD_TRUST"),
    ("chat_event_carries_auth", "qq-onebot", [("plugin.world.events[0].fields", lambda f: f + ["auth"])], "plugin.world.events[0].fields[3]", "E_EVENT_FIELD_TRUST"),
    ("space_event_still_cannot_carry_is_owner", "plaza-2d", [("plugin.world.perception.visible", lambda v: v + ["is_owner"]),
                                                              ("plugin.world.events[1].fields", ["pet_name", "is_owner"])], "plugin.world.events[1].fields[1]", "E_EVENT_FIELD_TRUST"),
    ("event_leaks_hidden_truth", "plaza-2d", [("plugin.world.events[0].fields", lambda f: f + ["all_positions"])], "plugin.world.events[0].fields[2]", "E_EVENT_FIELD_LEAKS_HIDDEN"),
    ("event_field_not_in_visible", "plaza-2d", [("plugin.world.events[1].fields", ["pet_name", "mystery"])], "plugin.world.events[1].fields[1]", "E_EVENT_FIELD_NOT_VISIBLE"),
    ("event_field_bad_name", "plaza-2d", [("plugin.world.events[1].fields", ["Pet Name"])], "plugin.world.events[1].fields[0]", "E_EVENT_FIELD"),
    ("event_field_duplicate", "plaza-2d", [("plugin.world.events[1].fields", ["pet_name", "pet_name"])], "plugin.world.events[1].fields", "E_DUPLICATE"),
    ("perception_name_bad", "plaza-2d", [("plugin.world.perception.visible", ["Pet Name"])], "plugin.world.perception.visible[0]", "E_PERCEPTION_NAME"),
    ("visible_and_hidden_overlap", "plaza-2d", [("plugin.world.perception.hidden", lambda h: h + ["pet_name"])], "plugin.world.perception", "E_PERCEPTION_OVERLAP"),
    ("chat_world_has_no_eyes", "qq-onebot", [("plugin.world.events[0].sense", "saw")], "plugin.world.events[0].sense", "E_SENSE_NOT_ALLOWED"),
    ("chat_world_has_no_ears", "qq-onebot", [("plugin.world.events[0].sense", "heard")], "plugin.world.events[0].sense", "E_SENSE_NOT_ALLOWED"),
    ("sense_not_in_list", "plaza-2d", [("plugin.world.events[0].sense", "smelled")], "plugin.world.events[0].sense", "E_ENUM"),
    ("notice_has_no_urgency", "qq-onebot", [("plugin.world.events[2]", {"type": "friend_request", "sense": "notice", "urgency": "now"})],
     "plugin.world.events[2].urgency", "E_NOTICE_HAS_URGENCY"),
    ("urgency_not_in_list", "qq-onebot", [("plugin.world.events[0].urgency", "asap")], "plugin.world.events[0].urgency", "E_ENUM"),
    ("duplicate_event_type", "qq-onebot", [("plugin.world.events[1].type", "message")], "plugin.world.events[1].type", "E_DUPLICATE_EVENT"),
    ("event_type_bad_name", "qq-onebot", [("plugin.world.events[0].type", "Message")], "plugin.world.events[0].type", "E_EVENT_TYPE"),
    ("event_has_instruction_switch", "qq-onebot", [("plugin.world.events[0].trusted", True)], "plugin.world.events[0].trusted", "E_UNKNOWN_KEY", "当指令"),
    ("event_missing_sense", "qq-onebot", [("plugin.world.events[0].sense", DEL)], "plugin.world.events[0].sense", "E_MISSING"),
    # 动作
    ("action_name_not_prefixed_at_all", "qq-onebot", [("plugin.world.actions[0].name", "send")], "plugin.world.actions[0].name", "E_ACTION_NAME"),
    ("action_name_with_another_prefix", "qq-onebot", [("plugin.world.actions[0].name", "wx_send")], "plugin.world.actions[0].name", "E_ACTION_PREFIX"),
    ("duplicate_action_name", "qq-onebot", [("plugin.world.actions[1].name", "qq_send")], "plugin.world.actions[1].name", "E_DUPLICATE_ACTION"),
    ("act_without_receipt", "qq-onebot", [("plugin.world.actions[0].receipt", DEL)], "plugin.world.actions[0].receipt", "E_ACT_NO_RECEIPT"),
    ("receipt_names_nothing", "qq-onebot", [("plugin.world.actions[0].receipt", "nope")], "plugin.world.actions[0].receipt", "E_RECEIPT_UNKNOWN"),
    ("receipt_points_at_a_message", "qq-onebot", [("plugin.world.actions[0].receipt", "message")], "plugin.world.actions[0].receipt", "E_RECEIPT_NOT_RECEIPT"),
    ("query_marked_outward", "qq-onebot", [("plugin.world.actions[1].outward", True)], "plugin.world.actions[1].outward", "E_QUERY_OUTWARD"),
    ("query_with_receipt", "qq-onebot", [("plugin.world.actions[1].receipt", "send_result")], "plugin.world.actions[1].receipt", "E_QUERY_HAS_RECEIPT"),
    ("act_must_say_outward", "qq-onebot", [("plugin.world.actions[0].outward", DEL)], "plugin.world.actions[0].outward", "E_MISSING"),
    ("outward_not_bool", "qq-onebot", [("plugin.world.actions[0].outward", "yes")], "plugin.world.actions[0].outward", "E_NOT_BOOL"),
    ("action_kind_not_in_list", "qq-onebot", [("plugin.world.actions[0].kind", "write")], "plugin.world.actions[0].kind", "E_ENUM"),
    ("action_without_reason", "qq-onebot", [("plugin.world.actions[0].why", "")], "plugin.world.actions[0].why", "E_STR"),
    ("param_type_unknown", "qq-onebot", [("plugin.world.actions[0].params.conv.type", "object")], "plugin.world.actions[0].params.conv.type", "E_ENUM"),
    ("param_name_bad", "qq-onebot", [("plugin.world.actions[0].params", {"Conv": {"type": "string"}})], "plugin.world.actions[0].params.Conv", "E_PARAM_NAME"),
    ("enum_param_without_values", "plaza-2d", [("plugin.world.actions[0].params.direction.values", DEL)], "plugin.world.actions[0].params.direction.values", "E_MISSING"),
    ("max_len_on_a_non_string", "plaza-2d", [("plugin.world.actions[0].params.direction.max_len", 5)], "plugin.world.actions[0].params.direction.max_len", "E_PARAM_MAXLEN"),
    ("values_on_a_non_enum", "qq-onebot", [("plugin.world.actions[0].params.conv.values", ["a"])], "plugin.world.actions[0].params.conv.values", "E_PARAM_VALUES"),
    ("too_many_params", "qq-onebot", [("plugin.world.actions[0].params", {"p%d" % i: {"type": "bool"} for i in range(9)})], "plugin.world.actions[0].params", "E_PARAMS"),
    ("param_required_not_bool", "qq-onebot", [("plugin.world.actions[0].params.conv.required", 1)], "plugin.world.actions[0].params.conv.required", "E_NOT_BOOL"),
    # 档和该有的声明
    ("space_world_without_perception", "plaza-2d", [("plugin.world.perception", DEL)], "plugin.world.perception", "E_PERCEPTION_REQUIRED"),
    ("space_world_without_twin_fixtures", "plaza-2d", [("plugin.world.perception.twin_fixtures", DEL)], "plugin.world.perception.twin_fixtures", "E_TWIN_REQUIRED"),
    ("space_world_shows_her_nothing", "plaza-2d", [("plugin.world.perception.visible", [])], "plugin.world.perception.visible", "E_VISIBLE_REQUIRED"),
    ("space_world_without_spectate", "plaza-2d", [("plugin.world.spectate", DEL)], "plugin.world.spectate", "E_SPECTATE_REQUIRED"),
    ("spectate_panel_is_not_the_panel", "plaza-2d", [("plugin.world.spectate.panel", "other.html")], "plugin.world.spectate.panel", "E_SPECTATE_PANEL_MISMATCH"),
    ("space_world_with_trust_from", "plaza-2d", [("plugin.world.trust_from", "platform_id")], "plugin.world.trust_from", "E_CHANNEL_FIELD_NOT_ALLOWED"),
    ("space_world_with_proactive", "plaza-2d", [("plugin.world.proactive", {"mode": "free"})], "plugin.world.proactive", "E_CHANNEL_FIELD_NOT_ALLOWED"),
    ("space_world_with_channel_permissions", "plaza-2d", [("plugin.permissions.channel", {"act_as_owner": False})], "plugin.permissions.channel", "E_CHANNEL_PERM_NOT_CHAT"),
    ("channel_permissions_without_any_world", "plaza-2d", [("plugin.world", None), ("plugin.permissions.channel", {})], "plugin.permissions.channel", "E_CHANNEL_PERM_NOT_CHAT"),
    ("chat_world_without_trust_from", "qq-onebot", [("plugin.world.trust_from", DEL)], "plugin.world.trust_from", "E_CHANNEL_FIELD_MISSING"),
    ("chat_world_without_trust_max", "qq-onebot", [("plugin.world.trust_max", DEL)], "plugin.world.trust_max", "E_CHANNEL_FIELD_MISSING"),
    ("chat_world_without_proactive", "qq-onebot", [("plugin.world.proactive", DEL)], "plugin.world.proactive", "E_CHANNEL_FIELD_MISSING"),
    ("chat_world_without_caps", "qq-onebot", [("plugin.world.caps", DEL)], "plugin.world.caps", "E_CHANNEL_FIELD_MISSING"),
    ("chat_hidden_truth_needs_fixtures", "qq-onebot", [("plugin.world.perception", {"visible": ["a_field"], "hidden": ["secret_x"]})], "plugin.world.perception.twin_fixtures", "E_TWIN_REQUIRED"),
    ("world_kind_not_in_list", "qq-onebot", [("plugin.world.kind", "metaverse")], "plugin.world.kind", "E_ENUM"),
    ("world_prefix_bad", "qq-onebot", [("plugin.world.prefix", "Q")], "plugin.world.prefix", "E_PREFIX"),
    ("world_has_no_events_key", "qq-onebot", [("plugin.world.events", DEL)], "plugin.world.events", "E_MISSING"),
    ("world_events_not_a_list", "qq-onebot", [("plugin.world.events", {})], "plugin.world.events", "E_NOT_LIST"),
    ("world_is_not_an_object", "qq-onebot", [("plugin.world", [])], "plugin.world", "E_NOT_OBJECT"),
    ("world_tries_to_declare_a_session", "qq-onebot", [("plugin.world.session", {"tools": True})], "plugin.world.session", "E_UNKNOWN_KEY", "核心定死"),
    ("world_tries_to_declare_trust", "qq-onebot", [("plugin.world.trust", "owner")], "plugin.world.trust", "E_UNKNOWN_KEY", "信任档由核心定"),
    # 主动发、能力
    ("quota_without_number", "qq-onebot", [("plugin.world.proactive", {"mode": "quota"})], "plugin.world.proactive.quota_per_day", "E_QUOTA_MISSING"),
    ("quota_on_a_free_channel", "qq-onebot", [("plugin.world.proactive", {"mode": "free", "quota_per_day": 5})], "plugin.world.proactive.quota_per_day", "E_QUOTA_NOT_ALLOWED"),
    ("quota_out_of_range", "qq-onebot", [("plugin.world.proactive", {"mode": "quota", "quota_per_day": 0})], "plugin.world.proactive.quota_per_day", "E_RANGE"),
    ("window_on_a_free_channel", "qq-onebot", [("plugin.world.proactive", {"mode": "free", "reply_window_s": 60})], "plugin.world.proactive.reply_window_s", "E_WINDOW_NOT_ALLOWED"),
    ("window_of_zero", "weixin-ilink", [("plugin.world.proactive", {"mode": "reply_window", "reply_window_s": 0})], "plugin.world.proactive.reply_window_s", "E_RANGE"),
    ("window_is_a_bool", "weixin-ilink", [("plugin.world.proactive", {"mode": "reply_window", "reply_window_s": True})], "plugin.world.proactive.reply_window_s", "E_RANGE"),
    ("proactive_mode_not_in_list", "qq-onebot", [("plugin.world.proactive.mode", "whenever")], "plugin.world.proactive.mode", "E_ENUM"),
    ("proactive_without_mode", "qq-onebot", [("plugin.world.proactive", {})], "plugin.world.proactive.mode", "E_MISSING"),
    ("caps_without_group", "qq-onebot", [("plugin.world.caps.group", DEL)], "plugin.world.caps.group", "E_MISSING"),
    ("group_channel_must_say_what_it_sees", "qq-onebot", [("plugin.world.caps.group_sees", DEL)], "plugin.world.caps.group_sees", "E_MISSING"),
    ("no_group_means_no_group_sees", "local-guest", [("plugin.world.caps.group_sees", "all")], "plugin.world.caps.group_sees", "E_GROUP_SEES_NOT_ALLOWED"),
    ("group_sees_not_in_list", "qq-onebot", [("plugin.world.caps.group_sees", "everything")], "plugin.world.caps.group_sees", "E_ENUM"),
    ("caps_with_public_callback", "qq-onebot", [("plugin.world.caps.public_callback", True)], "plugin.world.caps.public_callback", "E_UNKNOWN_KEY", "公网"),
    ("caps_attachment_unknown", "qq-onebot", [("plugin.world.caps.attachments", ["gif"])], "plugin.world.caps.attachments[0]", "E_ENUM"),
    ("caps_attachment_duplicate", "qq-onebot", [("plugin.world.caps.attachments", ["image", "image"])], "plugin.world.caps.attachments", "E_DUPLICATE"),
    ("caps_max_len_zero", "qq-onebot", [("plugin.world.caps.max_len", 0)], "plugin.world.caps.max_len", "E_RANGE"),
    ("caps_dm_not_bool", "qq-onebot", [("plugin.world.caps.dm", "yes")], "plugin.world.caps.dm", "E_NOT_BOOL"),
    # 叫停
    ("leave_timeout_too_long", "qq-onebot", [("plugin.world.leave.ack_timeout_s", 31)], "plugin.world.leave.ack_timeout_s", "E_RANGE"),
    ("leave_timeout_is_a_bool", "qq-onebot", [("plugin.world.leave.ack_timeout_s", True)], "plugin.world.leave.ack_timeout_s", "E_RANGE"),
    ("leave_timeout_is_a_string", "qq-onebot", [("plugin.world.leave.ack_timeout_s", "5")], "plugin.world.leave.ack_timeout_s", "E_RANGE"),
    # 联网、本机
    ("network_by_ip", "weixin-ilink", [("plugin.permissions.network", ["1.2.3.4"])], "plugin.permissions.network[0]", "E_DOMAIN"),
    ("network_localhost", "weixin-ilink", [("plugin.permissions.network", ["localhost"])], "plugin.permissions.network[0]", "E_DOMAIN"),
    ("network_internal_suffix", "weixin-ilink", [("plugin.permissions.network", ["printer.local"])], "plugin.permissions.network[0]", "E_DOMAIN"),
    ("network_upper_case", "weixin-ilink", [("plugin.permissions.network", ["Example.COM"])], "plugin.permissions.network[0]", "E_DOMAIN"),
    ("network_with_a_scheme", "weixin-ilink", [("plugin.permissions.network", ["https://a.example.com"])], "plugin.permissions.network[0]", "E_DOMAIN"),
    ("network_too_many", "weixin-ilink", [("plugin.permissions.network", ["a%d.example.org" % i for i in range(17)])], "plugin.permissions.network", "E_NOT_LIST"),
    ("network_duplicate", "weixin-ilink", [("plugin.permissions.network", ["a.example.org", "a.example.org"])], "plugin.permissions.network", "E_DUPLICATE"),
    ("local_not_loopback", "qq-onebot", [("plugin.permissions.local", ["0.0.0.0:3001"])], "plugin.permissions.local[0]", "E_LOCAL"),
    ("local_low_port", "qq-onebot", [("plugin.permissions.local", ["127.0.0.1:80"])], "plugin.permissions.local[0]", "E_LOCAL"),
    ("local_port_too_big", "qq-onebot", [("plugin.permissions.local", ["127.0.0.1:99999"])], "plugin.permissions.local[0]", "E_LOCAL"),
    ("permission_listen_a_public_port", "qq-onebot", [("plugin.permissions.listen", ["0.0.0.0:80"])], "plugin.permissions.listen", "E_UNKNOWN_KEY", "公网"),
    ("permission_unknown_key", "qq-onebot", [("plugin.permissions.root", True)], "plugin.permissions.root", "E_UNKNOWN_KEY"),
    ("permission_notify_not_bool", "qq-onebot", [("plugin.permissions.notify", "yes")], "plugin.permissions.notify", "E_NOT_BOOL"),
    ("permission_upload_not_in_list", "qq-onebot", [("plugin.permissions.upload", "always")], "plugin.permissions.upload", "E_ENUM"),
    ("permission_files_wrong_mode", "qq-onebot", [("plugin.permissions.files", [{"path": "$DOWNLOADS", "mode": "w"}])], "plugin.permissions.files[0].mode", "E_ENUM"),
    ("permission_files_without_path", "qq-onebot", [("plugin.permissions.files", [{"mode": "r"}])], "plugin.permissions.files[0].path", "E_MISSING"),
    ("permission_account_without_purpose", "qq-onebot", [("plugin.permissions.accounts", [{"service": "qq"}])], "plugin.permissions.accounts[0].purpose", "E_MISSING"),
    ("permission_account_bad_service", "qq-onebot", [("plugin.permissions.accounts", [{"service": "QQ!", "purpose": "回复"}])], "plugin.permissions.accounts[0].service", "E_STR"),
    ("permission_model_not_a_string", "qq-onebot", [("plugin.permissions.model", 5)], "plugin.permissions.model", "E_STR"),
    ("permission_elevated_not_a_list", "qq-onebot", [("plugin.permissions.elevated", "net.start")], "plugin.permissions.elevated", "E_NOT_STR_LIST"),
    ("permission_audio_not_bool", "qq-onebot", [("plugin.permissions.audio", "yes")], "plugin.permissions.audio", "E_TYPE"),
    # 形态: 层级、版本、服务
    ("schema_is_1", "qq-onebot", [("plugin.schema", 1)], "plugin.schema", "E_SCHEMA"),
    ("schema_is_true", "qq-onebot", [("plugin.schema", True)], "plugin.schema", "E_SCHEMA"),
    ("schema_missing", "qq-onebot", [("plugin.schema", DEL)], "plugin.schema", "E_MISSING"),
    ("version_without_patch", "qq-onebot", [("plugin.version", "1.0")], "plugin.version", "E_VERSION"),
    ("host_range_nonsense", "qq-onebot", [("plugin.host", "latest")], "plugin.host", "E_HOST_RANGE"),
    ("plugin_tier_not_in_list", "qq-onebot", [("plugin.tier", "kernel")], "plugin.tier", "E_ENUM"),
    ("plugin_without_permissions", "qq-onebot", [("plugin.permissions", DEL)], "plugin.permissions", "E_MISSING"),
    ("plugin_author_empty", "qq-onebot", [("plugin.author", "  ")], "plugin.author", "E_STR"),
    ("plugin_license_missing", "qq-onebot", [("plugin.license", DEL)], "plugin.license", "E_MISSING"),
    ("plugin_unknown_key", "qq-onebot", [("plugin.exec_anything", True)], "plugin.exec_anything", "E_UNKNOWN_KEY"),
    ("plugin_is_not_an_object", "qq-onebot", [("plugin", [])], "plugin", "E_NOT_OBJECT"),
    ("reserved_key_with_wrong_type", "qq-onebot", [("plugin.tools", "x")], "plugin.tools", "E_TYPE"),
    ("chat_process_without_service", "qq-onebot", [("plugin.tier", "process")], "plugin.service", "E_CHAT_NEEDS_SERVICE"),
    ("chat_process_without_endpoint", "weixin-ilink", [("plugin.tier", "process"), ("plugin.service", {"cmd": ["python", "x.py"]}), ("plugin.permissions.network", [])],
     "plugin.permissions.network", "E_CHAT_NEEDS_ENDPOINT"),
    ("builtin_with_a_service", "qq-onebot", [("plugin.service", {"cmd": ["python", "x.py"]})], "plugin.service", "E_BUILTIN_HAS_SERVICE"),
    ("service_cmd_empty", "plaza-2d", [("plugin.service.cmd", [])], "plugin.service.cmd", "E_CMD"),
    ("service_cmd_not_strings", "plaza-2d", [("plugin.service.cmd", ["python", 3])], "plugin.service.cmd[1]", "E_CMD"),
    ("service_without_cmd", "plaza-2d", [("plugin.service.cmd", DEL)], "plugin.service.cmd", "E_MISSING"),
    ("service_health_not_a_path", "plaza-2d", [("plugin.service.health", "health")], "plugin.service.health", "E_HEALTH"),
    ("service_start_not_in_list", "plaza-2d", [("plugin.service.start", "sometimes")], "plugin.service.start", "E_ENUM"),
    ("service_stop_with_host_not_bool", "plaza-2d", [("plugin.service.stop_with_host", "yes")], "plugin.service.stop_with_host", "E_NOT_BOOL"),
    ("service_heartbeat_out_of_range", "plaza-2d", [("plugin.service.heartbeat_s", 0)], "plugin.service.heartbeat_s", "E_RANGE"),
    ("service_runtime_out_of_range", "plaza-2d", [("plugin.service.max_runtime_s", 10 ** 9)], "plugin.service.max_runtime_s", "E_RANGE"),
    ("service_unknown_key", "plaza-2d", [("plugin.service.run_as", "root")], "plugin.service.run_as", "E_UNKNOWN_KEY"),
    ("panel_without_entry", "plaza-2d", [("plugin.panel.entry", DEL)], "plugin.panel.entry", "E_MISSING"),
    ("panel_entry_is_not_html", "plaza-2d", [("plugin.panel.entry", "spectate.js")], "plugin.panel.entry", "E_PATH_SUFFIX"),
    ("panel_tools_not_strings", "plaza-2d", [("plugin.panel.tools", [1])], "plugin.panel.tools[0]", "E_NOT_STR_LIST"),
    # 路径
    ("env_prompt_goes_up", "plaza-2d", [("plugin.world.env_prompt", "../ENV_PROMPT.md")], "plugin.world.env_prompt", "E_PATH_BAD"),
    ("env_prompt_is_absolute", "plaza-2d", [("plugin.world.env_prompt", "/etc/ENV_PROMPT.md")], "plugin.world.env_prompt", "E_PATH_BAD"),
    ("env_prompt_with_drive_letter", "plaza-2d", [("plugin.world.env_prompt", "C:/ENV_PROMPT.md")], "plugin.world.env_prompt", "E_PATH_BAD"),
    ("env_prompt_with_backslash", "plaza-2d", [("plugin.world.env_prompt", "sub\\ENV_PROMPT.md")], "plugin.world.env_prompt", "E_PATH_BAD"),
    ("env_prompt_hidden_file", "plaza-2d", [("plugin.world.env_prompt", ".secret.md")], "plugin.world.env_prompt", "E_PATH_BAD"),
    ("env_prompt_wrong_type", "plaza-2d", [("plugin.world.env_prompt", "ENV_PROMPT.txt")], "plugin.world.env_prompt", "E_PATH_SUFFIX"),
    ("twin_fixture_goes_up", "plaza-2d", [("plugin.world.perception.twin_fixtures", ["../x.json"])], "plugin.world.perception.twin_fixtures[0]", "E_PATH_BAD"),
    ("ui_goes_up", "plaza-2d", [("ui", "../x.html")], "ui", "E_PATH_BAD"),
    # 顶层
    ("top_id_bad", "qq-onebot", [("id", "Bad_ID")], "id", "E_ID"),
    ("top_id_missing", "qq-onebot", [("id", DEL)], "id", "E_ID"),
    ("top_name_missing", "qq-onebot", [("name", DEL)], "name", "E_MISSING"),
    ("top_name_too_long", "qq-onebot", [("name", "名" * 81)], "name", "E_STR"),
    ("top_description_not_text", "qq-onebot", [("description", 3)], "description", "E_STR"),
    ("top_unknown_key", "qq-onebot", [("evil", 1)], "evil", "E_UNKNOWN_KEY"),
    ("top_mcp_not_object", "qq-onebot", [("mcp", "python server.py")], "mcp", "E_TYPE"),
    ("top_files_not_object", "qq-onebot", [("files", [])], "files", "E_TYPE"),
]


@pytest.mark.parametrize("case", BAD, ids=[c[0] for c in BAD])
def test_a_broken_manifest_is_rejected_and_the_report_names_the_field(case):
    name, sample, edits, path, code = case[:5]
    issues = pm.validate_plugin_manifest(make(sample, *edits))
    hit = [i for i in issues if i.path == path and i.code == code and i.level == "error"]
    assert hit, "期望在 %s 报 %s, 实际: %s" % (path, code, [(i.path, i.code) for i in issues])
    assert hit[0].message.strip()
    if len(case) > 5:
        assert case[5] in hit[0].message


def test_the_whole_manifest_being_the_wrong_type_is_rejected():
    for bad in ([], "x", 3, None):
        assert [(i.path, i.code) for i in pm.validate_plugin_manifest(bad)] == [("", "E_NOT_OBJECT")]


def test_nested_junk_never_crashes_the_validator():
    junk = [None, [], {}, "x", 3, True, [None], {"a": {"b": []}}]
    for key in ("permissions", "service", "panel", "world", "tools", "schedule", "budget", "lends", "provides"):
        for value in junk:
            pm.validate_plugin_manifest(make("qq-onebot", ("plugin." + key, value)))
    for key in ("events", "actions", "perception", "leave", "spectate", "proactive", "caps", "kind", "prefix"):
        for value in junk:
            pm.validate_plugin_manifest(make("plaza-2d", ("plugin.world." + key, value)))
            pm.validate_plugin_manifest(make("qq-onebot", ("plugin.world." + key, value)))


def test_every_issue_has_a_path_a_code_and_a_message():
    for case in BAD:
        for i in pm.validate_plugin_manifest(make(case[1], *case[2])):
            assert isinstance(i.path, str) and re.fullmatch(r"[EW]_[A-Z_]+", i.code) and i.message.strip() and i.level in ("error", "warn")


# ---- 引用的文件 ----

def test_referenced_files_must_exist_in_the_package(tmp_path):
    m = load("plaza-2d")
    pkg = package_for(tmp_path, "plaza-2d")
    (pkg / "fixtures" / "twin-far-pet.json").unlink()
    (pkg / "ENV_PROMPT.md").unlink()
    (pkg / "spectate.html").unlink()
    got = {(i.path, i.code) for i in pm.validate_plugin_manifest(m, pkg)}
    assert got == {("plugin.world.perception.twin_fixtures[0]", "E_FILE_MISSING"), ("plugin.world.env_prompt", "E_FILE_MISSING"),
                   ("plugin.world.spectate.panel", "E_FILE_MISSING"), ("plugin.panel.entry", "E_FILE_MISSING")}


def test_a_directory_is_not_a_file(tmp_path):
    pkg = package_for(tmp_path, "plaza-2d")
    (pkg / "ENV_PROMPT.md").unlink()
    (pkg / "ENV_PROMPT.md").mkdir()
    assert ("plugin.world.env_prompt", "E_FILE_MISSING") in {(i.path, i.code) for i in pm.validate_plugin_manifest(load("plaza-2d"), pkg)}


def test_without_a_package_dir_files_are_not_looked_for():
    assert pm.validate_plugin_manifest(load("plaza-2d")) == []


# ---- 来路政策 ----

def test_bundled_plugins_may_have_the_owner_tier_and_builtin():
    for name in NAMES:
        assert pm.policy_issues(load(name), "bundled") == []


@pytest.mark.parametrize("name,codes", [
    ("qq-onebot", {"E_BUILTIN_NOT_BUNDLED", "E_THIRD_PARTY_OWNER", "E_THIRD_PARTY_ANSWER_CARDS"}),
    ("weixin-ilink", {"E_BUILTIN_NOT_BUNDLED", "E_THIRD_PARTY_OWNER", "E_THIRD_PARTY_ANSWER_CARDS"}),
    ("local-guest", {"E_BUILTIN_NOT_BUNDLED"}),
    ("plaza-2d", set()),
])
def test_third_party_cannot_have_builtin_or_the_owner_tier_or_card_answers(name, codes):
    issues = pm.policy_issues(load(name), "third_party")
    assert {i.code for i in issues} == codes
    assert all(i.level == "error" for i in issues)


def test_third_party_owner_tier_is_reported_at_all_three_places():
    paths = {i.path for i in pm.policy_issues(load("qq-onebot"), "third_party")}
    assert {"plugin.world.trust_max", "plugin.permissions.channel.act_as_owner", "plugin.permissions.channel.answer_cards", "plugin.tier"} <= paths


def test_the_source_cannot_be_claimed_by_the_manifest():
    m = make("qq-onebot", ("plugin.source", "bundled"))
    assert ("plugin.source", "E_UNKNOWN_KEY") in {(i.path, i.code) for i in pm.validate_plugin_manifest(m)}
    m = make("qq-onebot", ("source", "bundled"))
    assert ("source", "E_UNKNOWN_KEY") in {(i.path, i.code) for i in pm.validate_plugin_manifest(m)}


def test_an_unknown_source_is_a_programming_error():
    with pytest.raises(ValueError):
        pm.policy_issues(load("qq-onebot"), "trusted")
    with pytest.raises(ValueError):
        pm.policy_issues(load("qq-onebot"), None)


def test_policy_on_junk_does_not_crash():
    for junk in (None, [], {}, {"plugin": []}, {"plugin": {"world": []}}, {"plugin": {"permissions": []}}):
        assert pm.policy_issues(junk, "third_party") == [] or isinstance(pm.policy_issues(junk, "third_party"), list)


# ---- 单独列给用户看的两项权限 ----

def test_the_two_channel_permissions_are_listed_on_their_own():
    assert pm.separate_permissions(load("qq-onebot")) == ["act_as_owner", "answer_cards"]
    assert pm.separate_permissions(load("weixin-ilink")) == ["act_as_owner", "answer_cards"]
    assert pm.separate_permissions(load("local-guest")) == []
    assert pm.separate_permissions(load("plaza-2d")) == []
    only_owner = make("qq-onebot", ("plugin.permissions.channel", {"act_as_owner": True}))
    assert pm.separate_permissions(only_owner) == ["act_as_owner"]


def test_separate_permissions_ignores_truthy_non_booleans_and_junk():
    assert pm.separate_permissions(make("qq-onebot", ("plugin.permissions.channel", {"act_as_owner": "yes", "answer_cards": 1}))) == []
    for junk in (None, [], {}, {"plugin": None}, {"plugin": {"permissions": []}}, {"plugin": {"permissions": {"channel": []}}}):
        assert pm.separate_permissions(junk) == []


# ---- 重名 ----

def test_a_prefix_or_action_name_already_taken_is_a_conflict():
    installed = [load("qq-onebot"), load("plaza-2d")]
    clash = make("weixin-ilink", ("plugin.world.prefix", "qq"), ("plugin.world.actions[0].name", "qq_send"))
    got = {(i.path, i.code) for i in pm.find_conflicts(clash, installed)}
    assert got == {("plugin.world.prefix", "E_PREFIX_TAKEN"), ("plugin.world.actions[0].name", "E_ACTION_NAME_TAKEN")}
    only_name = make("weixin-ilink", ("plugin.world.actions[0].name", "qq_send"))
    assert {(i.path, i.code) for i in pm.find_conflicts(only_name, installed)} == {("plugin.world.actions[0].name", "E_ACTION_NAME_TAKEN")}


def test_no_conflict_for_distinct_names_updates_and_non_worlds():
    installed = [load("qq-onebot"), load("plaza-2d")]
    assert pm.find_conflicts(load("weixin-ilink"), installed) == []
    assert pm.find_conflicts(load("qq-onebot"), installed) == []  # 同一个 id 是更新
    assert pm.find_conflicts({"id": "x", "name": "x"}, installed) == []
    assert pm.find_conflicts(load("weixin-ilink"), [{"id": "old", "name": "老包"}, None, "x", {"id": "y", "plugin": []}]) == []


# ---- 字段说明和代码对得上 ----

def test_every_known_field_is_documented_and_nothing_extra():
    paths, docs = set(pm.field_paths()), set(pm.FIELD_DOCS)
    assert paths - docs == set()
    assert docs - paths == set()
    for path, doc in pm.FIELD_DOCS.items():
        assert len(doc) == 3 and all(isinstance(x, str) and x.strip() for x in doc), path


def test_the_two_channel_declarations_and_two_permissions_are_in_the_field_table():
    for p in ("plugin.world.trust_from", "plugin.world.trust_max", "plugin.world.proactive",
              "plugin.permissions.channel.act_as_owner", "plugin.permissions.channel.answer_cards"):
        assert p in pm.FIELD_DOCS


def test_every_error_code_in_the_module_is_exercised_by_a_test():
    source = (Path(pm.__file__)).read_text(encoding="utf-8")
    codes = set(re.findall(r'"([EW]_[A-Z0-9_]+)"', source))
    exercised = {c[4] for c in BAD} | {"W_NO_PLUGIN_BLOCK", "E_FILE_MISSING", "E_NOT_OBJECT", "E_BUILTIN_NOT_BUNDLED",
                                        "E_THIRD_PARTY_OWNER", "E_THIRD_PARTY_ANSWER_CARDS", "E_PREFIX_TAKEN", "E_ACTION_NAME_TAKEN"}
    # E_FILE_OUTSIDE 只有包里放了指向外面的符号链接才触发, 测试里不建链接 (Windows 上要特权)
    assert codes - exercised - {"E_FILE_OUTSIDE"} == set()

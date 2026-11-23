"""群/API 原文默认不归档; 所有渠道与模型均为本机替身, 库仅 tmp_path."""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from test_channels import (GA, GB, SELF, cloud_model, gmsg, pmsg, reply_json,
                           run_guest, run_qq, say, until)
from wm.app import ENGINES
from wm.channels.manager import ConfigError, parse_conf
from wm.channels.store import ChannelStore, conv_folder


def test_default_store_blocks_group_and_api_both_directions(tmp_path):
    store = ChannelStore(tmp_path / "archive")
    for i in range(10_000):
        store.append(("group", GA), f"[群友] filler {i}")
        store.append(("group", GA), f"[她] reply {i}")
        store.append(("api", "opaque-source"), f"[外部] filler {i}")
    assert not store.root.exists()
    assert store.tail(("group", GA)) == []
    assert store.day_lines(("api", "opaque-source"), datetime.now()) == []


def test_private_self_and_weixin_archives_are_unchanged(tmp_path):
    store = ChannelStore(tmp_path)
    for key in (("private", 7), ("weixin", 0), ("self", 0), ("qzone", 9)):
        store.append(key, "explicit non-group archive")
        assert store.tail(key) == ["explicit non-group archive"]


def test_explicit_group_override_and_api_archive(tmp_path):
    store = ChannelStore(tmp_path, group_overrides={GA: True}, api_log_raw=True)
    for key in (("group", GA), ("group", GB), ("api", "connector")):
        store.append(key, "[person] user")
        store.append(key, "[她] reply")
    assert store.tail(("group", GA)) == ["[person] user", "[她] reply"]
    assert not (tmp_path / conv_folder(("group", GB))).exists()
    assert store.tail(("api", "connector")) == ["[person] user", "[她] reply"]
    # Explicit false wins over the global raw archive switch.
    store = ChannelStore(tmp_path, group_log_raw=True, group_overrides={GA: False})
    store.append(("group", GA), "must not append")
    assert store.tail(("group", GA)) == []
    assert "must not append" not in next((tmp_path / f"群{GA}").rglob("*.md")).read_text(encoding="utf-8")
    store.append(("group", GB), "explicit global archive")
    assert store.tail(("group", GB)) == ["explicit global archive"]


def test_closing_archive_preserves_old_files_without_replay_or_prune(tmp_path):
    now = datetime(2026, 10, 31, 12)
    old = now - timedelta(days=90)
    enabled = ChannelStore(tmp_path, clock=lambda: old, group_log_raw=True, api_log_raw=True)
    for key in (("group", GA), ("api", "connector")):
        enabled.append(key, "old archive must survive")
    enabled.clock = lambda: now
    enabled.append(("group", GA), "today archive must not replay")
    enabled.append(("api", "connector"), "today api archive must not replay")
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*.md")}
    disabled = ChannelStore(tmp_path, clock=lambda: now)
    for key in (("group", GA), ("api", "connector")):
        disabled.append(key, "not persisted")
        assert disabled.tail(key) == []
        assert disabled.day_lines(key, now) == []
    assert disabled.prune() == []
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*.md")}


def test_retention_still_applies_to_explicitly_enabled_archives(tmp_path):
    now = datetime(2026, 10, 31, 12)
    store = ChannelStore(tmp_path, log_days=30, clock=lambda: now - timedelta(days=90),
                         group_overrides={GA: True})
    store.append(("group", GA), "opt-in archive")
    store.append(("private", 7), "legacy archive")
    store.clock = lambda: now
    assert len(store.prune()) == 2


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_archive_switches_require_actual_booleans(value):
    for raw in ({"group": {"log_raw": value}}, {"api_log_raw": value},
                {"convs": [{"type": "group", "id": GA, "log_raw": value}]}):
        with pytest.raises(ConfigError):
            parse_conf(raw)
    with pytest.raises(ValueError):
        ChannelStore(None, group_log_raw=value)
    with pytest.raises(ValueError):
        ChannelStore(None, group_overrides={GA: value})


def test_parse_defaults_and_per_group_false_survive():
    conf, _ = parse_conf({})
    assert conf["group"]["log_raw"] is False and conf["api_log_raw"] is False
    conf, _ = parse_conf({"group": {"log_raw": True}, "convs": [
        {"type": "group", "id": GA, "log_raw": False}, {"type": "group", "id": GB}]})
    assert conf["convs"][("group", GA)]["log_raw"] is False
    assert "log_raw" not in conf["convs"][("group", GB)]
    with pytest.raises(ConfigError):
        parse_conf({"convs": [{"type": "private", "id": 8, "log_raw": True}]})


def test_default_group_keeps_live_context_without_raw_in_or_out(tmp_path):
    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "model": cloud_model(base)})
        assert (await inbox.get("channel_config_done"))["ok"]
        manager = bot.app[ENGINES].channels
        for i in range(30):
            await bot.push(gmsg(GA, 7, f"quiet context {i}", i))
        await until(lambda: ("group", GA) in manager.convs and
                    any("quiet context 29" in row for row in manager.convs[("group", GA)].recent))
        conv = manager.convs[("group", GA)]
        assert len(conv.recent) == 20 and len(conv.lines) == manager.conf["context_lines"]
        assert not cloud.requests
        await bot.push(gmsg(GA, 7, "请接着刚才的话", 31, at=SELF))
        await until(lambda: len(bot.sent) == 1)
        await until(lambda: conv.task is None or conv.task.done())
        assert any("quiet context 29" in json.dumps(r["body"], ensure_ascii=False) for r in cloud.requests)
        assert conv.session.history and any("reply marker" in line for line in conv.recent)
        assert not (manager.store.root / f"群{GA}").exists()
        return manager.status()["retention"]
    result = run_qq(tmp_path, scenario, vault=True,
                    convs=[{"type": "group", "id": GA, "send": "auto"}],
                    cloud_script=[say(reply_json("reply marker"))])
    assert result["group_log_raw"] is False


def test_explicit_group_archives_in_out_and_can_close_without_erasing(tmp_path):
    async def scenario(ws, inbox, bot, base, cloud):
        await inbox.get("channel_status", lambda m: m["state"] == "online")
        await ws.send_json({"type": "channel_config", "model": cloud_model(base)})
        assert (await inbox.get("channel_config_done"))["ok"]
        manager = bot.app[ENGINES].channels
        await bot.push(gmsg(GA, 7, "ArchiveInputOne", 1, at=SELF))
        await until(lambda: len(bot.sent) == 1)
        conv = manager.convs[("group", GA)]
        await until(lambda: conv.task is None or conv.task.done())
        paths = list((manager.store.root / f"群{GA}").rglob("*.md"))
        assert len(paths) == 1
        before = paths[0].read_bytes()
        assert b"ArchiveInputOne" in before and b"ArchiveReplyOne" in before
        # Reconfigure only the archive policy, retaining live context and all existing files.
        conf, warnings = parse_conf({**manager.base, "model": cloud_model(base),
                                      "convs": [{"type": "group", "id": GA, "send": "auto", "log_raw": False}]})
        await manager._apply(conf, warnings)
        await bot.push(gmsg(GA, 7, "NoArchiveTwo", 2, at=SELF))
        await until(lambda: len(bot.sent) == 2)
        await until(lambda: conv.task is None or conv.task.done())
        assert paths[0].read_bytes() == before and manager.store.tail(conv.key) == []
        assert any("NoArchiveTwo" in line for line in conv.recent)
        assert "ArchiveInputOne" in json.dumps(cloud.requests[-1]["body"], ensure_ascii=False)
    run_qq(tmp_path, scenario, vault=True,
           convs=[{"type": "group", "id": GA, "send": "auto", "log_raw": True}],
           cloud_script=[say(reply_json("ArchiveReplyOne")), say(reply_json("NoArchiveReplyTwo"))])


def test_default_guest_connector_context_survives_without_archive(tmp_path):
    async def scenario(owner, inbox, guest, base, cloud):
        await owner.send_json({"type": "channel_config", "model": cloud_model(base), "private": {"min_gap_s": 0}})
        assert (await inbox.get("channel_config_done"))["ok"]
        for i, text in enumerate(("ConnectorContextOne", "接着上一句")):
            await guest.send_json({"type": "chat", "id": i, "user": "opaque-untrusted-source", "text": text})
            assert (await guest.receive_json())["ok"]
        status = await inbox.get("channel_status", lambda m: (m.get("api") or {}).get("requests") == 2)
        assert "ConnectorContextOne" in json.dumps(cloud.requests[-1]["body"], ensure_ascii=False)
        return status
    status = run_guest(tmp_path, scenario, vault=True,
                       cloud_script=[say(reply_json("你好呀")), say(reply_json("可以继续说"))])
    assert status["api"]["log_dir"] is None and "api_log_raw=false" in status["api"]["note"]
    assert not list((tmp_path / "vault" / "外部渠道" / "api").rglob("*.md"))


def test_disabled_group_retains_its_archive_override(tmp_path):
    conf, _ = parse_conf({"group": {"log_raw": True}, "convs": [
        {"type": "group", "id": GA, "enabled": False, "log_raw": False},
        {"type": "group", "id": GB, "enabled": False}]})
    assert not conf["convs"]
    assert conf["group_log_overrides"] == {GA: False, GB: False}
    now = datetime(2026, 10, 31)
    store = ChannelStore(tmp_path, clock=lambda: now - timedelta(days=90), group_log_raw=True)
    store.append(("group", GA), "keep disabled archive")
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*.md")}
    store.clock = lambda: now
    store.retention.configure(conf["group"]["log_raw"], conf["group_log_overrides"])
    store.append(("group", GA), "do not reopen under global true")
    assert store.prune() == [] and store.tail(("group", GA)) == []
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*.md")}


def test_old_store_references_follow_revocation(tmp_path):
    from wm.channels.store import ArchivePolicy
    policy = ArchivePolicy(True, {GA: True}, True)
    previous = ChannelStore(tmp_path / "old", retention=policy)
    replacement = ChannelStore(tmp_path / "new", retention=policy)
    policy.configure(False, {}, False)
    for store in (previous, replacement):
        store.append(("group", GA), "late group reply")
        store.append(("api", "connector"), "late connector reply")
        assert not store.root.exists()


def test_off_apply_closes_inflight_guest_reply_and_rollover_prune(tmp_path):
    from types import SimpleNamespace
    from wm.channels.manager import ChannelManager

    async def scenario():
        cfg = {"memory": {"vault": str(tmp_path / "vault"), "exclude": ["外部渠道"]},
               "channels": {"weixin": {"home": str(tmp_path / "wx")}}}
        manager = ChannelManager(cfg, SimpleNamespace(llm=None, memory=None))
        enabled, warnings = parse_conf({"enabled": False, "api_log_raw": True, "group": {"log_raw": True}})
        await manager._apply(enabled, warnings)  # QQ remains off; no protocol client is started.
        old_group_store = manager.store = ChannelStore(tmp_path / "vault" / "外部渠道" / "qq",
                                                        retention=manager.store.retention)
        old_api_store = manager._api_logs(enabled)
        old = datetime.now() - timedelta(days=90)
        for store, key in ((old_group_store, ("group", GA)), (old_api_store, ("api", "connector"))):
            store.clock = lambda: old
            store.append(key, "old archive must not be pruned after close")
            store.clock = datetime.now
        started, release = asyncio.Event(), asyncio.Event()

        async def suspended_reply(*args, **kwargs):
            started.set()
            await release.wait()
            return {"ok": True, "reply": "LateReplyNotPersisted", "emo": "neutral", "decision": "sent", "tokens": 0}

        manager._guest_reply = suspended_reply
        task = asyncio.create_task(manager.guest_chat("connector", "InputBeforeClosing"))
        await started.wait()
        before = {str(p): p.read_bytes() for p in (tmp_path / "vault").rglob("*.md")}
        manager.api_stats = {"day": "2000-01-01"}  # status during apply triggers the actual daily prune path.
        closed, warnings = parse_conf({"enabled": False, "api_log_raw": False, "group": {"log_raw": False}})
        await manager._apply(closed, warnings)
        assert manager.store is old_group_store and manager.api_store is old_api_store
        assert not old_group_store.logs_enabled(("group", GA)) and not old_api_store.logs_enabled(("api", "connector"))
        assert manager.public_status()["retention"]["group_log_raw"] is False
        assert manager._api_status()["log_dir"] is None
        release.set()
        assert (await task)["reply"] == "LateReplyNotPersisted"  # Chat completion itself is unchanged.
        old_group_store.append(("group", GA), "LateGroupReplyNotPersisted")
        assert old_group_store.prune() == [] and old_api_store.prune() == []
        assert before == {str(p): p.read_bytes() for p in (tmp_path / "vault").rglob("*.md")}
    asyncio.run(scenario())


def test_explicit_api_archive_without_qq_or_panel_configuration(tmp_path):
    async def scenario(owner, inbox, guest, base, cloud):
        await guest.send_json({"type": "chat", "id": 1, "user": "connector", "text": "ExplicitInitialArchive"})
        assert (await guest.receive_json())["ok"]
        status = await inbox.get("channel_status", lambda m: (m.get("api") or {}).get("requests") == 1)
        assert status["retention"]["api_log_raw"] is True
    run_guest(tmp_path, scenario, "channels.qq.api_log_raw=true", vault=True)
    logs = list((tmp_path / "vault" / "外部渠道" / "api").rglob("*.md"))
    assert len(logs) == 1 and "ExplicitInitialArchive" in logs[0].read_text(encoding="utf-8")

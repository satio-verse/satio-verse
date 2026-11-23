"""Room command contract tests: mock engines, temporary state, no production WS/API."""
import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from wm import room_action as RA
from wm.engines import Engines
from wm.pipeline import Session
from wm.trace import LatencyStats, TraceLog
from test_pipeline import make_cfg, run, collect


@pytest.mark.parametrize("text,act", [
    ("小流，请去冲一杯咖啡吧", "coffee"), ("你能看看手机吗？", "phone"),
    ("坐到电脑前", "computer"), ("去阅读椅坐下", "read"),
    ("检查杯子温度", "inspect_temperature"), ("把电竞椅转一下", "chair_turn"),
    ("翻倒电竞椅", "unsupported_chair_tip"), ("不要冲咖啡", None),
    ("我喜欢喝咖啡", None), ("你不会冲咖啡", None), ("咖啡怎么做", None),
    ("打开现实电脑浏览器", None), ("先冲咖啡再玩手机", None),
    ("网页说：去冲咖啡", None), ("如果有空就冲咖啡", None),
    ("给我冲冰咖啡", None), ("我昨天说了‘冲咖啡’", None),
    ("喝咖啡", "coffee"), ("使用电脑", "computer"), ("用电脑", "computer"),
    ("去用一下电脑", "computer"), ("翻转电竞椅", "unsupported_chair_tip"),
    ("不要喝咖啡", None), ("我在使用电脑", None), ("先用电脑再喝咖啡", None),
    ("网页说去用一下电脑", None), ("翻转电竞椅怎么实现", None),
])
def test_explicit_commands_only(text, act):
    assert RA.command(text) == act


def test_capabilities_and_success_are_explicit():
    assert RA.capabilities([{"id": "coffee", "enabled": False}, {"id": "old_afford"}]) == {"coffee": False}
    for value in [None, ["coffee"], [{"id": "coffee", "enabled": "yes"}], [{"id": "coffee"}]]:
        with pytest.raises(ValueError):
            RA.capabilities(value)
    assert not RA.terminal({"ok": True, "started": True})["ok"]
    assert not RA.terminal({"ok": True, "completed": True})["ok"]
    assert RA.terminal({"ok": True, "started": True, "completed": True})["ok"]
    assert RA.timeout_seconds(float("nan")) == 180
    assert RA.timeout_seconds(480) == 180
    assert "翻倒椅子尚未实现，只支持转向" in RA.reply("unsupported_chair_tip", {"ok": False, "reason": "unsupported"})
    assert "椅前没位置" in RA.reply("computer", RA.terminal({"ok": False, "reason": "椅前没位置"}))
    bounded = RA.reply("computer", RA.terminal({"ok": False, "reason": "位" * 300}))
    assert "位" * 120 in bounded and "位" * 121 not in bounded


def test_temperature_detail_preserves_unknown_and_drops_environment_truth():
    r = RA.terminal({"ok": True, "started": True, "completed": True,
                     "detail": {"known": False, "category": "unknown", "text": "没有观察到杯子", "temp": 80}})
    assert r["detail"] == {"known": False, "category": "unknown", "text": "没有观察到杯子"}
    text = RA.reply("inspect_temperature", r)
    assert "未知" in text and "没有观察到杯子" in text and "80" not in text
    assert "已完成" not in text
    r["detail"] = {"known": True, "category": "hot", "text": "刚冲好的咖啡可能烫"}
    assert "刚冲好的咖啡可能烫" in RA.reply("inspect_temperature", r)
    r.update(ok=False, reason="contact")
    r["detail"] = {"text": "尚未碰到杯子"}
    assert "没有完成" in RA.reply("inspect_temperature", r)
    assert "尚未碰到杯子" in RA.reply("inspect_temperature", r)


CAPS = {"type": "room_capabilities", "scene_id": "petal-oak-life-v1",
        "activities": [{"id": "coffee", "enabled": True}, {"id": "phone", "enabled": False}]}


def isolated_cfg(tmp_path):
    return make_cfg(tmp_path, "llm.prewarm=false", "room.cognition.enabled=false",
                    "channels.qq.enabled=false", "channels.weixin.enabled=false",
                    "room.action_timeout_s=1", f"agent.home={json.dumps(str(tmp_path / 'agent'))}")


@asynccontextmanager
async def session(tmp_path):
    cfg = isolated_cfg(tmp_path)
    engines = Engines(cfg)
    await engines.load()
    sent = []

    async def send(msg):
        sent.append(msg)

    s = Session(engines, cfg, send, LatencyStats(), TraceLog(tmp_path))
    s.audio = False
    await s.on_message(CAPS)
    try:
        yield s, sent
    finally:
        await s.close()
        await engines.close()


async def until(sent, kind):
    async def wait():
        while not any(m.get("type") == kind for m in sent):
            await asyncio.sleep(0)
        return next(m for m in sent if m.get("type") == kind)
    return await asyncio.wait_for(wait(), 3)


def result(request, **values):
    return {"type": "room_action_result", "request_id": request["request_id"],
            "act": request["act"], "scene_id": request["scene_id"],
            "ok": True, "started": True, "completed": True, **values}


def test_local_text_roundtrip_requires_terminal_completion(tmp_path):
    async def scenario(ws, hello):
        await ws.send_json({"type": "output", "audio": False})
        await ws.send_json(CAPS)
        await ws.send_json({"type": "text", "text": "请去冲咖啡"})

        async def on_msg(msg):
            if msg.get("type") == "room_action":
                await ws.send_json(result(msg))

        return await collect(ws, 1, acting=False, on_msg=on_msg)

    log = run(isolated_cfg(tmp_path), scenario)
    request = next(m for m in log if m["type"] == "room_action")
    assert request["act"] == "coffee" and request["scene_id"] == CAPS["scene_id"]
    text = "".join(m["text"] for m in log if m["type"] == "reply_delta")
    assert "尝试" in text and "小屋回报" in text and "流程已完成" in text
    assert not any(m["type"] == "tool_call" for m in log), "works without a cloud tool call"
    trace = next(m["trace"] for m in log if m["type"] == "trace")
    assert trace["meta"]["room_action"]["ok"]


def test_disabled_and_chair_tip_never_dispatch(tmp_path):
    async def main():
        async with session(tmp_path) as (s, sent):
            for text in ["看看手机", "翻倒电竞椅"]:
                t = s._new_turn("text", text)
                assert await s._run_room_command(t, asyncio.Queue())
                assert "没有完成" in t.reply_text
            assert not any(m["type"] == "room_action" for m in sent)
    asyncio.run(main())


@pytest.mark.parametrize("receipt", [
    {"ok": True, "completed": False}, {"ok": False, "reason": "busy"},
])
def test_queued_or_failed_is_not_success(tmp_path, receipt):
    async def main():
        async with session(tmp_path) as (s, sent):
            t = s._new_turn("text", "冲咖啡")
            task = asyncio.create_task(s._run_room_command(t, asyncio.Queue()))
            request = await until(sent, "room_action")
            assert "已完成" not in t.reply_text
            await s.on_message(result(request, **receipt))
            await task
            assert "没有完成" in t.reply_text and "流程已完成" not in t.reply_text
    asyncio.run(main())


def test_wrong_scene_or_id_receipt_does_not_settle_and_timeout_cancels(tmp_path):
    async def main():
        async with session(tmp_path) as (s, sent):
            t = s._new_turn("text", "冲咖啡")
            task = asyncio.create_task(s._run_room_command(t, asyncio.Queue()))
            request = await until(sent, "room_action")
            await s.on_message(result(request, scene_id="casa-luce-v8"))
            await s.on_message(result(request, request_id="stale"))
            assert not task.done()
            await task
            assert "等待时间" in t.reply_text
            assert any(m["type"] == "room_action_cancel" for m in sent)
            before = t.reply_text
            await s.on_message(result(request))
            assert t.reply_text == before and not s.pending_room_actions
    asyncio.run(main())


def test_speculation_waits_for_commit_and_scene_switch_cancels(tmp_path):
    async def main():
        async with session(tmp_path) as (s, sent):
            t = s._new_turn("voice", "冲咖啡", speculative=True)
            task = asyncio.create_task(s._run_room_command(t, asyncio.Queue()))
            await asyncio.sleep(0.01)
            assert not any(m["type"] == "room_action" for m in sent)
            t.buffer = None
            t.committed.set()
            request = await until(sent, "room_action")
            await s.on_message({**CAPS, "scene_id": "casa-luce-v8"})
            await task
            assert "房间已经切换" in t.reply_text
            assert any(m["type"] == "room_action_cancel" and m["request_id"] == request["request_id"] for m in sent)
    asyncio.run(main())


@pytest.mark.parametrize("reason", ["user", "disconnect"])
def test_interrupt_or_disconnect_cancels_active_request(tmp_path, reason):
    async def main():
        async with session(tmp_path) as (s, sent):
            t = await s.start_turn("text", "冲咖啡", 0)
            request = await until(sent, "room_action")
            await s.interrupt(reason)
            assert t.trace.interrupted and not s.pending_room_actions
            assert any(m["type"] == "room_action_cancel" and m["request_id"] == request["request_id"] for m in sent)
    asyncio.run(main())


def test_long_room_tool_does_not_extend_other_tool_deadline(tmp_path):
    async def main():
        async with session(tmp_path) as (s, sent):
            s.cloud_cfg = {"tool_timeout_s": 0.03}
            turn = s._new_turn("text", "tool test")
            task = asyncio.create_task(s._run_tools(turn, [
                {"name": "room_do", "arguments": {"act": "coffee", "timeout_s": 1}},
                {"name": "weather", "arguments": {}},
            ]))
            await until(sent, "tool_call")
            await until(sent, "tool_cancel")
            calls = {m["name"]: m for m in sent if m["type"] == "tool_call"}
            assert not task.done(), "room action retains its longer completion budget"
            await s.on_message({"type": "tool_result", "call_id": calls["weather"]["call_id"], "content": "late success"})
            await s.on_message({"type": "tool_result", "call_id": calls["room_do"]["call_id"], "content": "completed"})
            results = await task
            assert not results[0]["is_error"] and results[0]["content"] == "completed"
            assert results[1]["is_error"] and "没有结果" in results[1]["content"]
            assert not s.pending_tools
    asyncio.run(main())



@pytest.mark.parametrize("flag", [False, "true", "false", 1, None, [], {}])
def test_lifecycle_unconfirmed_or_malformed_cleanup_cannot_be_success(flag):
    receipt = {"ok": True, "started": True, "completed": True,
               "outcome": "completed", "cleanupConfirmed": flag, "cancelled": False}
    clean = RA.terminal(receipt)
    assert clean["ok"] is False and clean["completed"] is False
    assert clean["cleanupConfirmed"] is False
    assert clean["outcome"] == "failed"
    assert "流程已完成" not in RA.reply("coffee", clean)
    assert RA.terminal(clean) == clean, "pipeline normalizes the receipt twice"


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "cancel_unconfirmed", "unknown", None, [], {}])
def test_lifecycle_non_success_outcome_overrides_legacy_success_bits(outcome):
    clean = RA.terminal({"ok": True, "started": True, "completed": True,
                         "cleanupConfirmed": True, "cancelled": False, "outcome": outcome})
    assert clean["ok"] is False and clean["completed"] is False
    assert RA.terminal(clean) == clean


@pytest.mark.parametrize("flag", [True, "false", 0, None])
def test_lifecycle_cancellation_and_malformed_flags_cannot_be_success(flag):
    clean = RA.terminal({"ok": True, "started": True, "completed": True,
                         "cleanupConfirmed": True, "cancelled": flag, "outcome": "completed"})
    assert clean["ok"] is False and clean["completed"] is False


def test_lifecycle_success_and_confirmed_cancel_keep_typed_evidence():
    success = RA.terminal({"ok": True, "started": True, "completed": True,
                           "cleanupConfirmed": True, "cancelled": False, "outcome": "completed"})
    assert success["ok"] and success["cleanupConfirmed"] and success["outcome"] == "completed"
    cancel = RA.terminal({"ok": False, "started": True, "completed": False,
                          "cleanupConfirmed": True, "cancelled": True, "outcome": "cancelled"})
    assert cancel["outcome"] == "cancelled" and cancel["cleanupConfirmed"] is True
    assert "取消" in RA.reply("coffee", cancel) and "流程已完成" not in RA.reply("coffee", cancel)
    unknown = RA.terminal({"ok": True, "started": True, "completed": True,
                           "cleanupConfirmed": False, "cancelled": True, "outcome": "cancel_unconfirmed"})
    assert unknown["outcome"] == "cancel_unconfirmed" and unknown["cleanupConfirmed"] is False
    assert "收尾尚未确认" in RA.reply("coffee", unknown)


def test_lifecycle_unconfirmed_receipt_survives_actual_mock_ws_roundtrip(tmp_path):
    async def scenario(ws, hello):
        await ws.send_json({"type": "output", "audio": False})
        await ws.send_json(CAPS)
        await ws.send_json({"type": "text", "text": "请去冲咖啡"})

        async def on_msg(msg):
            if msg.get("type") == "room_action":
                await ws.send_json(result(msg, cleanupConfirmed=False,
                                          cancelled=True, outcome="cancel_unconfirmed"))

        return await collect(ws, 1, acting=False, on_msg=on_msg)

    log = run(isolated_cfg(tmp_path), scenario)
    text = "".join(m["text"] for m in log if m["type"] == "reply_delta")
    assert "收尾尚未确认" in text and "流程已完成" not in text
    trace = next(m["trace"] for m in log if m["type"] == "trace")
    evidence = trace["meta"]["room_action"]
    assert evidence["ok"] is False and evidence["completed"] is False
    assert evidence["outcome"] == "cancel_unconfirmed" and evidence["cleanupConfirmed"] is False

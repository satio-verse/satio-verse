"""契约测试 (不可变): 外面来的消息碰不到工具、主人的记忆和规则.
tests/contract/ 在工作台的保护名单上, 她的补丁改不了; 合并前的门禁里跑的也是项目里这份 (docs/roadmap/self-evolution.md 3.3、5.2).
门禁用 importlib 模式跑这个目录: 只 import wm 和标准库、pytest, 不引别的测试文件.
"""
import ast
import asyncio
import inspect
import json
from pathlib import Path

import pytest

from wm import cloud as C
from wm.channels import CAPS, Caps, Trust, guest, manager, selfcheck
from wm.channels.guest import GuestSession
from wm.channels.outbound import private_hit
from wm.channels.store import excluded

ROOT = Path(__file__).resolve().parents[2]


def _calls(mod) -> list[ast.Call]:
    return [n for n in ast.walk(ast.parse(inspect.getsource(mod))) if isinstance(n, ast.Call)]


def test_every_trust_level_has_no_tools():
    assert set(CAPS) == set(Trust)
    for caps in CAPS.values():
        assert isinstance(caps, Caps) and not caps.tools and not caps.owner_context and caps.memory_write == "candidate"
    with pytest.raises(Exception):  # 冻结的: 建好以后改不了
        CAPS[Trust.ACQUAINTANCE].tools = True


def test_guest_session_refuses_a_caps_with_tools(monkeypatch):
    monkeypatch.setitem(CAPS, Trust.ACQUAINTANCE, Caps(tools=True))
    with pytest.raises(ValueError):
        GuestSession(Trust.ACQUAINTANCE, "group", "群", "她", None)


def test_guest_session_has_no_tools_and_does_not_write_owner_memory():
    assert not [n for n in dir(GuestSession) if "tool" in n.lower()]  # 没有 _run_tools 这类
    for call in _calls(guest):
        assert not {k.arg for k in call.keywords} & {"tools", "on_tools"}  # 调模型不带工具
    for mod in (guest, manager):  # 访客那边一轮说完不调 Memory.observe (那会写主人的 关于用户.md、日志、关系.md)
        assert not [c for c in _calls(mod) if isinstance(c.func, ast.Attribute) and c.func.attr == "observe"]


def test_channels_do_not_import_the_owner_session():
    """渠道管理器不持有本人的会话 (wm/pipeline.py): 给主人的东西只走 notify, 只出不进."""
    for mod in (manager, guest, selfcheck):
        tree = ast.parse(inspect.getsource(mod))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not [x for x in names if "pipeline" in x], mod.__name__


def test_external_cloud_model_refuses_tools_before_any_request():
    assert asyncio.run(selfcheck.refuses_tools(C.CloudLLM(selfcheck.DUMMY_SPEC, allow_tools=False)))


def test_channel_selfcheck_passes_and_catches_tampering(monkeypatch):
    assert asyncio.run(selfcheck.run()) == []

    class Open:  # 有人把给外部渠道的模型换成了收工具的
        allow_tools = True
    assert any("不拦" in p for p in asyncio.run(selfcheck.run(Open())))
    monkeypatch.setitem(CAPS, Trust.ACQUAINTANCE, Caps(tools=True))
    assert any("工具" in p for p in asyncio.run(selfcheck.run()))


def test_channel_logs_are_excluded_from_owner_memory():
    cfg = json.loads((ROOT / "config" / "default.json").read_text(encoding="utf-8"))
    assert excluded(cfg["channels"]["qq"].get("dir") or "外部渠道/qq", cfg["memory"]["exclude"])
    assert not excluded("外部渠道/qq", [])


def test_outbound_privacy_check():
    for text in ("我手机 13812345678", "身份证 110101199001011234", "邮箱 a.b@example.com", "在 C:\\Users\\me\\ 下面",
                 "key 是 sk-abcdef123456", "Bearer abcdefghijk"):
        assert private_hit(text), text
    assert private_hit("她住在杭州西湖边", [("住处", "杭州")]) == "主人的住处"
    assert private_hit("今天天气不错") is None

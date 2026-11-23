"""插件宿主第 0 步 (3/3): 服务端启动时自查是不是以管理员身份在跑 (wm/privilege.py).

她、工作台任务和所有扩展都以服务端的身份运行, 服务端是管理员他们就全是管理员; 以前没有这项自查. 现在 python -m wm 起来时
日志里写一行明显的警告. "是"和"不是"两种情况用替身测 (测试机上不会真的提权).
查的是进程令牌有没有提升 (UAC 之后真的拿着管理员权限), 不是账号在不在管理员组.
/api/status 里带 privilege 字段 (wm/app.py 的 api_status); 看守靠 /api/status 判断服务端健不健康, 所以这个字段不管出什么意外
都不能让它报错, 而且只查一次系统 (进程的权限启动后不会变)."""
import asyncio
import logging
import sys

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wm import privilege
from wm.app import create_app
from wm.config import load_config


@pytest.fixture(autouse=True)
def _fresh_status():
    """status() 查一次记住: 每条测试前后都清掉, 不然参数化的几条会互相串, 替身的结果也不会漏到别的测试文件里."""
    privilege.forget()
    yield
    privilege.forget()


def test_windows_token_flag_wins_and_the_shell_api_is_the_fallback(monkeypatch):
    monkeypatch.setattr(privilege, "_windows", lambda: True)
    monkeypatch.setattr(privilege, "_shell_is_admin", lambda: False)
    for token in (True, False):
        monkeypatch.setattr(privilege, "_token_elevated", lambda token=token: token)
        assert privilege.is_elevated() is token
    monkeypatch.setattr(privilege, "_token_elevated", lambda: None)  # 令牌查不出来: 退回 IsUserAnAdmin
    monkeypatch.setattr(privilege, "_shell_is_admin", lambda: True)
    assert privilege.is_elevated() is True
    monkeypatch.setattr(privilege, "_shell_is_admin", lambda: None)
    assert privilege.is_elevated() is None


def test_other_systems_ask_for_root(monkeypatch):
    monkeypatch.setattr(privilege, "_windows", lambda: False)
    monkeypatch.setattr(privilege.os, "geteuid", lambda: 0, raising=False)
    assert privilege.is_elevated() is True
    monkeypatch.setattr(privilege.os, "geteuid", lambda: 1000, raising=False)
    assert privilege.is_elevated() is False
    monkeypatch.delattr(privilege.os, "geteuid", raising=False)
    assert privilege.is_elevated() is None


@pytest.mark.parametrize("value,expected", [(True, {"elevated": True, "warning": privilege.WARNING}),
                                            (False, {"elevated": False, "warning": ""}),
                                            (None, {"elevated": None, "warning": ""})])
def test_status_shape(monkeypatch, value, expected):
    monkeypatch.setattr(privilege, "is_elevated", lambda: value)
    assert privilege.status() == expected


def test_the_warning_is_one_loud_line_and_only_when_elevated(monkeypatch, caplog):
    monkeypatch.setattr(privilege, "is_elevated", lambda: False)
    with caplog.at_level(logging.INFO, logger="wm.privilege"):
        assert privilege.warn_if_elevated()["elevated"] is False
    assert not [r for r in caplog.records if r.name == "wm.privilege"]
    monkeypatch.setattr(privilege, "is_elevated", lambda: True)
    with caplog.at_level(logging.INFO, logger="wm.privilege"):
        assert privilege.warn_if_elevated()["elevated"] is True
    lines = [r for r in caplog.records if r.name == "wm.privilege"]
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    text = lines[0].getMessage()
    assert text.startswith("【警告") and "管理员" in text and "\n" not in text


def test_real_token_query_agrees_with_the_shell_api_on_this_machine():
    if not privilege._windows():
        pytest.skip("不是 Windows")
    token, shell = privilege._token_elevated(), privilege._shell_is_admin()
    assert token in (True, False, None) and shell in (True, False, None)
    if token is not None and shell is not None:
        assert token == shell


def test_main_warns_before_it_starts_serving(monkeypatch, caplog):
    import wm.__main__ as entry
    monkeypatch.delenv("WM_SUPERVISOR_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["wm"])
    monkeypatch.setattr(entry, "load_config", lambda config, overrides: {"server": {"host": "127.0.0.1", "port": 1}})
    monkeypatch.setattr(entry, "create_app", lambda cfg: object())
    monkeypatch.setattr(privilege, "is_elevated", lambda: True)
    warned_when_serving = []
    monkeypatch.setattr(entry.web, "run_app", lambda app, **kw: warned_when_serving.append(
        [r for r in caplog.records if r.name == "wm.privilege"]))
    with caplog.at_level(logging.WARNING, logger="wm.privilege"):
        entry.main()
    assert len(warned_when_serving) == 1 and len(warned_when_serving[0]) == 1  # 开始服务时警告已经写了


def test_status_never_raises_whatever_the_system_does(monkeypatch):
    """查询里出任何意外 (不只是 OSError / AttributeError / ValueError) 都给 elevated: null, 不往外抛; 查法给了不是真假的东西也算查不出来."""
    monkeypatch.setattr(privilege, "_windows", lambda: True)

    def boom(error):
        def probe():
            raise error
        return probe

    for error in (RuntimeError("boom"), TypeError("参数类型"), ZeroDivisionError(), MemoryError(), KeyError("x")):
        privilege.forget()
        monkeypatch.setattr(privilege, "_token_elevated", boom(error))
        monkeypatch.setattr(privilege, "_shell_is_admin", boom(error))
        assert privilege.is_elevated() is None
        assert privilege.status() == {"elevated": None, "warning": ""}
    privilege.forget()  # 令牌那一种出意外时, 后备的 IsUserAnAdmin 还要照常用
    monkeypatch.setattr(privilege, "_token_elevated", boom(RuntimeError("boom")))
    monkeypatch.setattr(privilege, "_shell_is_admin", lambda: True)
    assert privilege.status()["elevated"] is True
    privilege.forget()  # is_elevated 整个被换成会抛的东西, 或者给出不是真假的值
    monkeypatch.setattr(privilege, "is_elevated", boom(ZeroDivisionError()))
    assert privilege.status() == {"elevated": None, "warning": ""}
    for junk in (1, "yes", [], {}):
        privilege.forget()
        monkeypatch.setattr(privilege, "is_elevated", lambda junk=junk: junk)
        assert privilege.status() == {"elevated": None, "warning": ""}


def test_status_asks_the_system_only_once(monkeypatch):
    asked = []
    monkeypatch.setattr(privilege, "_windows", lambda: True)
    monkeypatch.setattr(privilege, "_token_elevated", lambda: asked.append("token") or False)
    monkeypatch.setattr(privilege, "_shell_is_admin", lambda: asked.append("shell") or True)
    first = privilege.status()
    for _ in range(5):
        assert privilege.status() == first == {"elevated": False, "warning": ""}
    assert asked == ["token"]  # 连着调六次, 只查了一次
    first["elevated"] = True  # 给出去的是拷贝, 改了不影响记住的
    assert privilege.status()["elevated"] is False
    privilege.forget()
    assert privilege.status()["elevated"] is False and asked == ["token", "token"]  # 忘掉以后才再查
    monkeypatch.setattr(privilege, "_token_elevated", lambda: asked.append("token2") or True)
    privilege.warn_if_elevated()  # 启动时的自查重新查一次, 并记住
    assert privilege.status()["elevated"] is True and privilege.status()["elevated"] is True
    assert asked == ["token", "token", "token2"]


CFG = ["llm.engine=mock", "asr.engine=none", "tts.engine=mock", "face.engine=none", "memory.vault=\"\""]


def run_app(scenario):
    async def main():
        client = TestClient(TestServer(create_app(load_config(overrides=CFG))))
        await client.start_server()
        try:
            return await scenario(client)
        finally:
            await client.close()

    return asyncio.run(main())


def test_api_status_carries_the_privilege_field_and_keeps_the_old_ones(monkeypatch):
    async def scenario(client):
        seen = {}
        for value in (False, True):
            monkeypatch.setattr(privilege, "is_elevated", lambda value=value: value)
            privilege.forget()  # 记住的是上一个替身给的
            seen[value] = await (await client.get("/api/status")).json()
        return seen

    seen = run_app(scenario)
    assert seen[False]["privilege"] == {"elevated": False, "warning": ""}
    assert seen[True]["privilege"] == {"elevated": True, "warning": privilege.WARNING}
    assert {"llm", "tts", "motion", "channels"} <= set(seen[True])  # 原来的字段都在


def test_api_status_stays_healthy_when_the_probe_blows_up_and_asks_once(monkeypatch):
    """看守靠 /api/status 的 200 判断服务端好不好: 自查出任何意外, 它照样回 200, privilege 是 null; 连着请求几次只查一次系统."""
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(privilege, "_windows", lambda: True)
    monkeypatch.setattr(privilege, "_token_elevated", boom)
    monkeypatch.setattr(privilege, "_shell_is_admin", boom)

    async def scenario(client):
        replies = []
        for _ in range(3):
            reply = await client.get("/api/status")
            replies.append((reply.status, (await reply.json())["privilege"]))
        return replies

    assert run_app(scenario) == [(200, {"elevated": None, "warning": ""})] * 3
    assert len(calls) == 2  # 令牌那一种和后备的 IsUserAnAdmin 各一次, 三次请求合起来只查了一次系统

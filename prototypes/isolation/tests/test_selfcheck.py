"""自查: 自己是管理员权限 (或者查不出来) 就打一句说明、退出码 3, 什么都不建. 没有任何绕过的开关.
(对话本身带着管理员权限, 所以这里不对真实环境调“我是不是管理员”来决定流程 -- 一律换成假的值.)"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import measure
import sandbox_win as sw
from fakes import ISO

PROJECT = ISO.parents[1]


def forbid_everything_after_the_selfcheck(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("自查没通过以后不许往下做")
    for name in ("read_uac_settings", "step2", "step3", "step4", "step5", "step6", "handle_leftover"):
        monkeypatch.setattr(measure, name, boom)
    for name in ("current_user_sid", "current_logon_sid", "appcontainer_sid", "dacl_sddl"):
        monkeypatch.setattr(sw, name, boom)


@pytest.mark.parametrize("elevated, rid", [(True, 0x3000), (True, 0x2000), (True, None), (None, 0x2000), (None, None),
                                           (False, 0x3000), (False, 0x4000)])
def test_elevated_or_unknown_stops_with_exit_code_3_and_creates_nothing(monkeypatch, tmp_path, capsys, elevated, rid):
    iso = tmp_path / "iso"
    iso.mkdir()
    monkeypatch.setattr(measure, "HERE", iso)
    monkeypatch.setattr(sw, "is_elevated", lambda: elevated)
    monkeypatch.setattr(sw, "own_integrity_rid", lambda: rid)
    forbid_everything_after_the_selfcheck(monkeypatch)
    assert measure.main([]) == 3
    assert list(iso.iterdir()) == [] and list(tmp_path.iterdir()) == [iso]       # 什么都没建 (没有 work, 没有 results)
    out = capsys.readouterr().out
    assert "普通权限" in out and "run_me.cmd" in out and "什么都没做" in out
    assert "提权确认三项设置" not in out                                            # 连只读的东西也没记


def test_no_environment_variable_or_flag_overrides_the_check(monkeypatch, tmp_path):
    iso = tmp_path / "iso"
    iso.mkdir()
    monkeypatch.setattr(measure, "HERE", iso)
    monkeypatch.setattr(sw, "is_elevated", lambda: True)
    monkeypatch.setattr(sw, "own_integrity_rid", lambda: 0x3000)
    forbid_everything_after_the_selfcheck(monkeypatch)
    for name in ("FORCE", "ALLOW_ADMIN", "ALLOW_ELEVATED", "IGNORE_ADMIN", "WM_FORCE", "MEASURE_FORCE", "ISOLATION_FORCE",
                 "RUN_AS_ADMIN", "ELEVATED_OK", "I_KNOW_WHAT_I_AM_DOING"):
        monkeypatch.setenv(name, "1")
    assert measure.main([]) == 3
    assert list(iso.iterdir()) == []


def test_selfcheck_passes_only_for_an_unelevated_token_with_a_normal_integrity_level(monkeypatch, tmp_path, capsys):
    """False + 中等完整性 = 通过, 往下走; 这里让第 2 步一调用就停, 只验自查放行."""
    iso = tmp_path / "iso"
    iso.mkdir()
    monkeypatch.setattr(measure, "HERE", iso)
    monkeypatch.setattr(sw, "is_elevated", lambda: False)
    monkeypatch.setattr(sw, "own_integrity_rid", lambda: 0x2000)
    monkeypatch.setattr(measure, "read_uac_settings", lambda: {k: None for k in measure.UAC_VALUES})

    class Reached(Exception):
        pass

    def stop(ctx):
        raise Reached
    monkeypatch.setattr(measure, "step2", stop)
    monkeypatch.setattr(measure, "step6", lambda ctx: None)
    monkeypatch.setattr(measure, "write_results", lambda ctx, code: None)
    measure.main([])                                  # step2 抛的是 Exception, run() 会兜住它, 走收尾
    assert "不是管理员权限：通过" in capsys.readouterr().out


def test_is_elevated_never_raises_and_answers_with_a_bool_or_none():
    """只读: 查自己令牌的提权标志 (和 wm/privilege.py 同一种查法). 不管结果是什么都不影响这条测试."""
    assert sw.is_elevated() in (True, False, None)


def test_is_elevated_agrees_with_the_projects_own_check():
    """原型不导入 wm, 自己写了一份等价的; 这里按路径把项目那份读进来 (只读, 不改) 对一下: 两边答案一致."""
    path = PROJECT / "wm" / "privilege.py"
    if not path.is_file():
        pytest.skip("项目里没有 wm/privilege.py")
    spec = importlib.util.spec_from_file_location("wm_privilege_for_parity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert sw.is_elevated() == module.is_elevated()


def test_the_two_checks_use_the_same_token_class_and_access_right():
    """两份实现的常量对一下 (TokenElevation = 20, TOKEN_QUERY = 0x8), 免得抄错一个数字."""
    path = PROJECT / "wm" / "privilege.py"
    if not path.is_file():
        pytest.skip("项目里没有 wm/privilege.py")
    text = Path(path).read_text(encoding="utf-8")
    assert "TOKEN_QUERY = 0x0008" in text and "TOKEN_ELEVATION = 20" in text
    assert sw.TOKEN_QUERY == 0x8 and sw.TOKEN_ELEVATION_CLASS == 20

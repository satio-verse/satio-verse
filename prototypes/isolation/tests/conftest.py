"""prototypes/isolation 的测试夹具.

对话里和这些测试里都不起沙箱进程、不登记应用容器、不改任何目录的权限、不拷 Python 运行时、不请求管理员权限 --
这些只在用户双击 run_me.cmd 时发生. 所以下面的夹具对每一条测试都生效:
- sandbox_win 里所有"会改东西"的函数换成一调用就让测试失败 (测试要用时自己再换成假的);
- USERPROFILE、HOME 换成空的临时目录 (os.path.expanduser 在 Windows 上读 USERPROFILE).

跑法 (在项目根目录; 不要写 .pyc 和 .pytest_cache):
    python -B -m pytest prototypes/isolation/tests -q -p no:cacheprovider
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True
ISO = Path(__file__).resolve().parent.parent
if str(ISO) not in sys.path:
    sys.path.insert(0, str(ISO))

import pytest  # noqa: E402

import sandbox_win  # noqa: E402

SIDE_EFFECTS = ("spawn", "set_dacl", "set_low_label", "protect_user_only", "make_restricted_token",
                "appcontainer_register", "appcontainer_unregister", "open_as_token")


def _boom(name: str):
    def _called(*_args, **_kwargs):
        raise AssertionError(f"测试里不许调 sandbox_win.{name}（它会起进程、登记身份或改权限）")
    return _called


@pytest.fixture(autouse=True)
def _no_real_side_effects(monkeypatch, tmp_path_factory):
    home = tmp_path_factory.mktemp("fake_home")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    for name in SIDE_EFFECTS:
        monkeypatch.setattr(sandbox_win, name, _boom(name))
    return home

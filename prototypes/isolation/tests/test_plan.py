"""--plan: 把六步打出来, 什么都不建、不起、不改; 管理员权限下也能跑 (不做自查, 所以也不碰令牌)."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import measure
import sandbox_win as sw
from fakes import ISO


def forbid(monkeypatch, owner, *names):
    for name in names:
        def boom(*_a, _name=name, **_k):
            raise AssertionError(f"--plan 不许调 {owner.__name__}.{_name}")
        monkeypatch.setattr(owner, name, boom)


def test_plan_prints_the_six_steps_with_real_paths_and_creates_nothing(monkeypatch, capsys, _no_real_side_effects):
    home = _no_real_side_effects
    results_before = sorted(p.name for p in (ISO / "results").iterdir()) if (ISO / "results").exists() else []
    monkeypatch.setenv("TMP", str(home / "AppData" / "Local" / "Temp"))   # --plan 直接取环境变量, 不调 tempfile.gettempdir()
    forbid(monkeypatch, tempfile, "gettempdir")
    # 建目录、拷删文件、起进程、开端口、碰令牌和系统接口: 一调用就让测试失败
    forbid(monkeypatch, os, "mkdir", "makedirs", "rmdir", "remove", "unlink", "rename", "replace", "system", "startfile")
    forbid(monkeypatch, shutil, "copy", "copy2", "copyfile", "copytree", "rmtree", "move")
    forbid(monkeypatch, tempfile, "mkdtemp", "mkstemp", "NamedTemporaryFile", "TemporaryDirectory")
    forbid(monkeypatch, subprocess, "Popen", "run", "call", "check_call", "check_output")
    forbid(monkeypatch, socket, "socket", "create_connection")
    forbid(monkeypatch, sw, "is_elevated", "own_integrity_rid", "current_user_sid", "current_logon_sid",
           "appcontainer_sid", "appcontainer_registered", "dacl_sddl", "process_exists", "close_handle", "_api")
    forbid(monkeypatch, measure, "read_uac_settings", "step1", "step2", "step3", "step4", "step5", "step6", "run")

    assert measure.main(["--plan"]) == 0
    out = capsys.readouterr().out
    for n, name in enumerate(measure.STEP_NAMES, 1):
        assert f"第 {n} 步  {name}" in out, name
    # 带实际会用的路径
    for piece in ("work\\home", "work\\pyrt", "work\\probe\\child_probe.py", "work\\decoy\\secret.txt",
                  "results\\<年月日-时分秒>", "wm_iso_<随机8位>\\fake_vault.txt", measure.APPC_NAME):
        assert piece in out, piece
    assert measure.ELEV_QUESTION.rstrip("：") in out and "默认不试" in out
    assert "<家目录>\\AppData\\Local\\Temp\\wm_iso_" in out        # 家目录那一段打成 <家目录>
    assert str(home) not in out
    # 和说明页一致的几件事: -I -B -S、作业对象、只继承标准输入输出、20 秒、不做的事
    for phrase in ("-I -B -S", "作业对象", "标准输入输出", "20 秒", "不会做的事", "最后只留 results\\"):
        assert phrase in out, phrase
    # 真的目录里也没有被它建出东西 (results\ 里可能有用户真跑留下的结果, 那不是它建的: 只看 work 和 results 的内容没变)
    assert not (ISO / "work").exists()
    assert sorted(p.name for p in (ISO / "results").iterdir()) == results_before if (ISO / "results").exists() \
        else results_before == []


def test_plan_runs_without_asking_whether_this_window_is_admin(monkeypatch, capsys):
    monkeypatch.setattr(sw, "is_elevated", lambda: True)    # 就算是管理员权限, --plan 也照常打印 (它不做任何事)
    assert measure.main(["--plan"]) == 0
    assert "第 6 步" in capsys.readouterr().out


def test_plan_as_a_real_command_creates_nothing_and_leaves_no_pycache(tmp_path, _no_real_side_effects):
    home = _no_real_side_effects
    env = dict(os.environ, USERPROFILE=str(home), HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    before = sorted(p.name for p in ISO.iterdir())
    done = subprocess.run([sys.executable, "-X", "utf8", str(ISO / "measure.py"), "--plan"], capture_output=True,
                          text=True, encoding="utf-8", env=env, cwd=str(tmp_path), timeout=120)
    assert done.returncode == 0, done.stderr
    assert all(f"第 {n} 步" in done.stdout for n in range(1, 7))
    assert sorted(p.name for p in ISO.iterdir()) == before
    assert "work" not in before and "__pycache__" not in before


@pytest.mark.parametrize("flag", ["--force", "--elevated", "--allow-admin", "--ignore-admin", "-f", "--yes"])
def test_there_is_no_switch_to_run_anyway(flag):
    with pytest.raises(SystemExit) as info:
        measure.main([flag])
    assert info.value.code == 2


def plan_text() -> str:
    return "\n".join(measure.plan_lines(measure.make_layout(ISO), Path(sys.base_prefix), Path(tempfile.gettempdir()),
                                       measure.Redactor()))


def test_plan_lists_exactly_as_many_variants_as_the_script_has():
    """脚本里每多一种配法，--plan 就得多列一种: 数量对得上、每一种的名字都在、诊断的都标了“不算结果”."""
    text = plan_text()
    listed = [ln for ln in text.splitlines() if ln.startswith(measure.VARIANT_PLAN_PREFIX)]
    total = sum(len(v) for v in measure.VARIANTS.values())
    assert len(listed) == total == 12
    for mode_id, label in measure.MODES:
        for i, variant in enumerate(measure.VARIANTS[mode_id], 1):
            line = f"{measure.VARIANT_PLAN_PREFIX}{label} 第 {i} 种：" + ("【诊断，不算结果】" if variant.diagnostic else "") + variant.name
            assert line in listed, line
    diagnostics = sum(v.diagnostic for vs in measure.VARIANTS.values() for v in vs)
    assert diagnostics == 4 and sum("【诊断，不算结果】" in ln for ln in listed) == diagnostics
    assert f"整次最多起 {total} 个 Python" in text
    # 对照放第一个跑, --plan 里的顺序也是
    assert [ln.split("第")[0].strip(" ·配法") for ln in listed][0] == "对照(不关)"


EXTRAS_THE_PLAN_MUST_SAY = {
    "开头清上次的残留": ("上次没收尾留下的", "先清掉", ".wm_isolation_work.json", "有 work\\ 却没有标记文件就不敢删"),
    "登记表里原来就有的收尾一并删": ("登记表里原来就有这个名字的", "收尾时也一并删"),
    "给本人对 home 一开始就加完全控制": ("一开始就给本人对", "加完全控制", "再给它打一个“低”完整性标签"),
    "标记文件": ("标记文件", "万一没收尾，下次靠它清"),
    "编出来的令牌 SID 和它的权限": ("按名字编出来的 SID", "不对应任何真账号", "随文件夹一起没了"),
    "多几个变量是哪五个、真值、只交给子进程": ("LOCALAPPDATA、APPDATA、USERPROFILE、ProgramData、SystemDrive", "真路径", "只交给那个子进程，不写进结果"),
    "继承整份环境、不记值和 stderr 原文": ("继承本脚本的整份环境变量", "可能有密钥", "不写任何环境变量的值", "不记它的 stderr 原文"),
    "诊断不发提权请求、另放、标不算数": ("诊断配法不发提权请求", "另放在“诊断配法量到的”里", "照实写“Python 做不到，其余没验”", "都标“诊断，不算数”"),
    "只降级第 3 种诊断": ("不去特权、不改组、完整性级别不降", "松，但完整性级别不降"),
    "D1 临时线程换身份只读打开": ("临时线程换成同一个令牌的身份", "各只读打开一次", "“模拟”级别", "不改任何文件和权限"),
    "D2 同身份起 cmd exit 7": ("cmd.exe /d /c exit 7", "只看退出码是不是 7", "造令牌就失败的不做"),
    "逐个造令牌只在 87 时、最多几个": ("报 87（参数不对）才会", "只造令牌、不起进程、立刻关", "最多额外造 6 个"),
    "提权的命令还会起 whoami 和 findstr": ("whoami.exe 和 findstr.exe", "约 5 个进程"),
    "只对 Python 起来了的关法发提权请求，最多 4 次": ("只对 Python 起来了的关法做", "最多 4 次"),
    "起不来可能卡 15 秒、整次时间": ("卡住最多 15 秒", "正常 1 到 3 分钟", "最坏约 5 分钟"),
    "文件被占着重试 1 秒": ("文件被占着", "等 1 秒再试一次"),
    "只读核对和自己连一次测试端口": ("注册表里（HKCU 下）有没有这个应用容器的登记项", "自己先连一次", "AppData\\Local\\Packages 下还有没有它的文件夹"),
    "自查比说明多的": ("完整性级别是高", "找不到可以拷的 Python 运行时", "退出码 2"),
    "对照第一个跑": ("对照不关、应用容器、受限令牌只降级、受限令牌加限制 SID；对照第一个跑",),
    "收尾先结束活着的探针": ("先把还活着的探针逐个结束",),
    "期限不严格": ("提权那一步卡住时可能多几秒",),
}


@pytest.mark.parametrize("what", sorted(EXTRAS_THE_PLAN_MUST_SAY))
def test_plan_says_in_plain_words_everything_extra_the_script_does(what):
    text = plan_text()
    for phrase in EXTRAS_THE_PLAN_MUST_SAY[what]:
        assert phrase in text, (what, phrase)


def test_plan_does_not_claim_more_than_it_is_allowed_to():
    """--plan 自己不起进程这句话要成立: 开头那句不再写“不起进程”这种绝对话, 写的是它具体做什么; 也不再说“每种关法做一次提权”."""
    text = plan_text()
    assert "每种关法做一次，对照也做一次" not in text and "各一次）" not in text and "全程约 1 分钟" not in text
    assert "最多 20 秒，到时间就结束作业" not in text


def test_plan_in_a_fresh_python_process_starts_no_process_and_the_real_run_stops_at_selfcheck_without_one(tmp_path):
    """标准库偷偷起的进程 (比如 platform.node() 在 3.10 上会起 cmd /c ver) 在 pytest 进程里被缓存盖住了, 只有全新的进程里才看得到:
    开一个全新的 Python, 把 subprocess.Popen 换成会报错的替身, 再跑 --plan; 以及真跑走到自查为止 (自查换成“是管理员”)."""
    script = FRESH_PROCESS_SCRIPT % str(ISO)
    home = tmp_path / "h"
    home.mkdir()
    env = dict(os.environ, USERPROFILE=str(home), HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-X", "utf8", "-c", script], capture_output=True, text=True, encoding="utf-8",
                          env=env, cwd=str(tmp_path), timeout=120)
    assert done.returncode == 0 and "OK-NO-PROCESS" in done.stdout, done.stdout[-500:] + done.stderr[-800:]


def test_plan_text_matches_the_promises_in_the_description_page(monkeypatch):
    lines = measure.plan_lines(measure.make_layout(ISO), Path(sys.base_prefix), Path(tempfile.gettempdir()),
                               measure.Redactor())
    text = "\n".join(lines)
    # 说明页第 2 步: 沙箱自己的文件夹、拷运行时 (约 28 MB、不下载)、探针的拷贝、原型目录里的假文件、临时文件夹里的假文件
    assert "约 28 MB" in text and "不下载" in text and "探针的一份拷贝" in text and "假保管箱" in text
    # 说明页第 3 步: 只给读运行时、写自己文件夹的权限, 别的目录不动
    assert "别的目录的权限一律不动" in text and "写进结果" in text
    # 说明页第 6 步: 删掉沙箱身份、假文件和拷来的运行时, 最后只留结果文件夹
    assert "删掉登记的沙箱身份" in text and "最后只留 results\\" in text
    # 说明页“不做什么”
    for phrase in ("读你别的文件", "下载", "安装", "连外网", "改系统设置或防火墙", "建 Windows 账号", "动提权确认的设置"):
        assert phrase in text, phrase


FRESH_PROCESS_SCRIPT = """
import sys, subprocess
sys.path.insert(0, %r)
sys.dont_write_bytecode = True
def boom(*a, **k):
    raise SystemExit('started a process: ' + repr(a)[:200])
subprocess.Popen.__init__ = boom
import measure
import sandbox_win as sw
assert measure.main(['--plan']) == 0
sw.is_elevated = lambda: True
assert measure.main([]) == 3
print('OK-NO-PROCESS')
"""

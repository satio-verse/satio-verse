"""measure.py 里不碰系统的部分: 命令行和参数怎么拼、路径字符检查、结果里的个人信息怎么抹、
每一样结果怎么判、表怎么生成、拷运行时的清单、提权那一条的结论."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

import measure
import sandbox_win as sw
from fakes import make_fake_runtime
from measure import CAN, CANNOT, ERROR, INVALID, UNKNOWN, Item

HOME = "C:\\Users\\Zed_Test_User"


def make_rd() -> measure.Redactor:
    return measure.Redactor(home=HOME, names=["Zed_Test_User", "TESTBOX-01"], sids=["S-1-5-21-111-222-333-1001"])


# ---------------------------------------------------------------- 个人信息抹除
def test_redactor_replaces_home_in_every_spelling():
    rd = make_rd()
    assert rd(HOME + "\\AppData\\Local\\Temp\\x") == "<家目录>\\AppData\\Local\\Temp\\x"
    assert rd("C:/users/zed_test_user/AppData") == "<家目录>/AppData"
    assert rd("C:\\\\Users\\\\Zed_Test_User\\\\a") == "<家目录>\\\\a"  # json 转义以后反斜杠是成对的


def test_redactor_names_are_whole_words_and_sids_are_gone():
    rd = make_rd()
    assert rd("owner Zed_Test_User, box TESTBOX-01!") == "owner <名字>, box <名字>!"
    assert rd("Zed_Test_Users and xZed_Test_User") == "Zed_Test_Users and xZed_Test_User"
    assert rd("S-1-5-21-111-222-333-1001 ok") == "<SID> ok"
    assert rd("nothing to hide: 做得到 做不到 (5)") == "nothing to hide: 做得到 做不到 (5)"


def test_redactor_leaks_finds_what_is_left_and_reports_kinds_only():
    rd = make_rd()
    assert rd.leaks(rd("a " + HOME + " Zed_Test_User S-1-5-21-111-222-333-1001")) == []
    assert rd.leaks("path C:\\\\Users\\\\Zed_Test_User\\\\x") == ["家目录", "用户名或电脑名"]
    assert rd.leaks("S-1-5-21-111-222-333-1001") == ["SID"]


def test_redactor_ignores_too_short_names():
    rd = measure.Redactor(home="", names=["ab", ""], sids=[])
    assert rd("ab cd") == "ab cd"


def test_redactor_from_env_uses_the_replaced_home(_no_real_side_effects):
    rd = measure.Redactor.from_env()
    assert Path(rd.home) == Path(_no_real_side_effects)


def test_show_path_is_relative_inside_the_prototype_dir_and_redacted_outside(tmp_path):
    rd = measure.Redactor(home=str(tmp_path / "me"))
    assert measure.show_path(tmp_path / "work" / "home", tmp_path, rd) == "work\\home"
    assert measure.show_path(tmp_path / "me" / "x", tmp_path / "iso", rd) == "<家目录>\\x"


# ---------------------------------------------------------------- 提权命令
def test_path_char_check():
    assert measure.path_is_safe("G:\\RunProgram\\WorldModel_v1\\prototypes\\isolation\\results\\20261114-120000")
    for bad in ["", "C:\\a b", "C:\\中文", "C:\\a&b", "C:\\a%b", "C:\\a^b", "C:\\a(b)", "C:\\a!b", "C:\\a\"b", "C:\\a;b",
                "C:\\a`b", "C:\\a'b", "C:\\a$b", "C:\\a,b", "C:\\a|b", "C:\\a>b", "C:\\a\nb"]:
        assert not measure.path_is_safe(bad), bad


def test_elevate_command_does_one_thing_with_three_system_tools():
    results = "G:\\RunProgram\\WorldModel_v1\\prototypes\\isolation\\results\\20261114-120000"
    cmd, why = measure.build_elevate_command(results, "restricted_sids", "C:\\Windows")
    assert why == "" and cmd["file"] == "C:\\Windows\\System32\\cmd.exe" and cmd["dir"] == "C:\\Windows\\System32"
    assert cmd["name"] == "elev_restricted_sids.txt" and cmd["result"] == results + "\\elev_restricted_sids.txt"
    params = cmd["params"]
    assert params.startswith('/d /s /c "') and params.endswith('"')
    exes = re.findall(r"[A-Za-z0-9_.\\:]+\.exe", params)
    assert sorted(exes) == ["C:\\Windows\\System32\\findstr.exe", "C:\\Windows\\System32\\whoami.exe"]
    assert "whoami.exe /groups |" in params and "/c:S-1-16-12288 >nul" in params
    assert params.count("ELEVATED") == 2 and "echo ELEVATED)" in params and "echo NOT_ELEVATED)" in params
    # 结果只写进一个文件, 别的文件名一个都没有; 没有 %、^、!、; 之类 cmd 会特殊对待的字符 (&& 和 || 是故意用的)
    assert params.count(results + "\\elev_restricted_sids.txt") == 2
    stripped = params.replace("&&", "").replace("||", "")
    for ch in "%^!;`$'":
        assert ch not in stripped
    assert "&" not in stripped and stripped.count("|") == 1 and stripped.count(">") == 3
    inner = params[len('/d /s /c "'):-1]
    for token in ("reg ", "schtasks", "powershell", "net ", "sc ", "del ", "copy ", "curl", "mshta", "wscript"):
        assert token not in inner


@pytest.mark.parametrize("results,root", [("G:\\a b\\r", "C:\\Windows"), ("G:\\中文\\r", "C:\\Windows"),
                                          ("G:\\r", "C:\\Win dows"), ("", "C:\\Windows")])
def test_elevate_command_refuses_unsafe_paths_and_says_why(results, root):
    cmd, why = measure.build_elevate_command(results, "control", root)
    assert cmd is None and "不试提权" in why


def test_elevate_command_refuses_odd_mode_ids():
    assert measure.build_elevate_command("G:\\r", "Control", "C:\\Windows")[0] is None
    assert measure.build_elevate_command("G:\\r", "a b", "C:\\Windows")[0] is None


def test_read_elev_word(tmp_path):
    f = tmp_path / "e.txt"
    assert measure.read_elev_word(str(f)) is None
    f.write_bytes(b"")
    assert measure.read_elev_word(str(f)) is None
    f.write_bytes(b"ELEVATED\r\n")
    assert measure.read_elev_word(str(f)) == "ELEVATED"
    f.write_bytes(b"NOT_ELEVATED\r\n")
    assert measure.read_elev_word(str(f)) == "NOT_ELEVATED"
    f.write_bytes(b"ELEVATED NOT\r\n")
    assert measure.read_elev_word(str(f)) == "OTHER"


# ---------------------------------------------------------------- 一样一样的结果怎么判
def test_classify_read():
    denied = {"ok": False, "err": "PermissionError", "errno": 13, "winerror": 5}
    assert measure.classify_read({"ok": True, "n": 10, "eq": True}, None) == Item(CAN)
    assert measure.classify_read({"ok": True, "n": 10, "eq": False}, None).state == ERROR
    assert measure.classify_read(denied, None) == Item(CANNOT, 5, "拒绝访问")
    assert measure.classify_read({"ok": False, "err": "FileNotFoundError", "errno": 2, "winerror": 3}, None).state == CANNOT
    assert measure.classify_read({"ok": False, "err": "OSError", "errno": 22, "winerror": 1920}, None) == \
        Item(ERROR, 1920, "OSError")
    assert measure.classify_read(None, "timeout").state == ERROR


def test_classify_write_checks_the_file_itself():
    denied = {"ok": False, "err": "PermissionError", "errno": 13, "winerror": 5}
    assert measure.classify_write({"ok": True}, None, True) == Item(CAN)
    assert measure.classify_write({"ok": True}, None, False).state == ERROR       # 探针说成了, 文件不在
    assert measure.classify_write(denied, None, False) == Item(CANNOT, 5, "拒绝访问")
    assert measure.classify_write(denied, None, True).state == ERROR              # 探针说被拒, 文件却在
    assert measure.classify_write({"ok": False, "err": "OSError", "errno": 28}, None, False) == Item(ERROR, 28, "OSError")


def test_classify_connect():
    assert measure.classify_connect({"ok": True}, None, 1) == Item(CAN)
    assert measure.classify_connect({"ok": True}, None, 0).state == ERROR         # 探针说连上, 测试端口没看到
    blocked = {"ok": False, "err": "PermissionError", "errno": 13, "winerror": 10013}
    assert measure.classify_connect(blocked, None, 0) == Item(CANNOT, 10013, "PermissionError")
    assert measure.classify_connect({"ok": False, "err": "TimeoutError", "errno": None, "winerror": None}, None, 0).state == CANNOT
    assert measure.classify_connect({"ok": False, "err": "OSError", "errno": 10106, "winerror": 10106}, None, 0).state == ERROR
    assert measure.classify_connect({"ok": False, "err": "ModuleNotFoundError"}, None, 0).state == ERROR


def test_classify_elevate_prefers_the_result_file():
    assert measure.classify_elevate({"ok": True, "ret": 1, "err": 0}, None, "ELEVATED").state == CAN
    assert measure.classify_elevate({"ok": True, "ret": 1, "err": 0}, None, "NOT_ELEVATED").state == CANNOT
    assert measure.classify_elevate(None, "eof", "ELEVATED").state == CAN         # 探针后来没了也认文件
    assert measure.classify_elevate({"ok": True, "ret": 1}, None, "OTHER").state == ERROR
    assert measure.classify_elevate({"ok": True, "ret": 0, "err": 5}, None, None) == Item(CANNOT, 5, "要不到 (请求被拒)")
    assert measure.classify_elevate({"ok": True, "ret": 1, "err": 0}, None, None).state == ERROR
    assert measure.classify_elevate({"ok": False, "err": "OSError", "winerror": 126}, None, None).state == ERROR
    assert measure.classify_elevate(None, "timeout", None).state == ERROR


def items_of(**overrides) -> dict:
    base = {"python": CAN, "read_fake": CANNOT, "read_decoy": CANNOT, "write_work": CANNOT, "write_temp": CANNOT,
            "write_home": CAN, "connect": CANNOT, "elevate": CANNOT}
    base.update(overrides)
    return {k: Item(v) for k, v in base.items()}


def test_verdict_for_sandbox_rows():
    assert measure.mode_verdict("appcontainer", items_of())["text"] == "能用"
    v = measure.mode_verdict("restricted_downgrade", items_of(read_fake=CAN, read_decoy=CAN, connect=CAN))
    assert v["ok"] is False and v["problems"] == ["读得到假保管箱", "读得到假文件", "连得上本机回环"]
    v = measure.mode_verdict("restricted_sids", items_of(write_home=CANNOT, python=CANNOT))
    assert "Python 起不来" in v["problems"] and "写不进自己的文件夹" in v["problems"]
    v = measure.mode_verdict("appcontainer", items_of(elevate=UNKNOWN))
    assert v["ok"] is None and "要管理员权限" in v["text"] and "没验" in v["text"]
    assert measure.mode_verdict("appcontainer", items_of(elevate=CAN))["problems"] == ["要得到了管理员权限"]
    assert measure.mode_verdict("appcontainer", items_of(elevate=INVALID))["ok"] is None


def test_verdict_for_the_control_row():
    all_can = {k: Item(CAN) for k in measure.ITEM_KEYS}
    assert measure.mode_verdict("control", all_can)["ok"] is True
    broken = dict(all_can, read_fake=Item(CANNOT))
    v = measure.mode_verdict("control", broken)
    assert v["ok"] is False and "对照有毛病" in v["text"]
    skipped = dict(all_can, elevate=Item(UNKNOWN))
    assert measure.mode_verdict("control", skipped, elevation_tried=False)["ok"] is True   # 用户选了不试, 对照不算有毛病
    assert measure.mode_verdict("control", skipped, elevation_tried=True)["ok"] is None


def mode(mid: str, state: str, note: str = "") -> dict:
    return {"id": mid, "label": mid, "items": {"elevate": {"state": state, "code": None, "note": note}}}


def test_finalize_elevation_not_tried():
    out = measure.finalize_elevation([mode("control", UNKNOWN)], False)
    assert out["tried"] is False and out["conclusion"].startswith("没验")


def test_finalize_elevation_valid_when_control_elevates():
    modes = [mode("appcontainer", CANNOT), mode("restricted_downgrade", CANNOT), mode("restricted_sids", CANNOT),
             mode("control", CAN)]
    out = measure.finalize_elevation(modes, True)
    assert out["valid"] is True and "三种关法都要不到" in out["conclusion"]
    assert modes[0]["items"]["elevate"]["state"] == CANNOT


def test_finalize_elevation_reports_the_modes_that_got_admin():
    modes = [mode("appcontainer", CANNOT), mode("restricted_downgrade", CAN), mode("control", CAN)]
    out = measure.finalize_elevation(modes, True)
    assert out["leaks"] == ["restricted_downgrade"] and "要得到了管理员权限" in out["conclusion"]


def test_finalize_elevation_control_without_elevated_means_method_invalid():
    modes = [mode("appcontainer", CANNOT), mode("restricted_sids", CANNOT), mode("control", CANNOT)]
    out = measure.finalize_elevation(modes, True)
    assert out["valid"] is False and "测法不成立" in out["conclusion"]
    assert modes[0]["items"]["elevate"]["state"] == INVALID and modes[0]["elevate_raw"]["state"] == CANNOT
    assert modes[2]["items"]["elevate"]["state"] == CANNOT   # 对照自己那格不改, 看得出是它没成


def test_finalize_elevation_nothing_attempted():
    modes = [mode("appcontainer", UNKNOWN, "没试: 路径里有别的字符"), mode("control", UNKNOWN, "没试: 路径里有别的字符")]
    out = measure.finalize_elevation(modes, True)
    assert out["valid"] is None and out["conclusion"].startswith("没验") and "路径里有别的字符" in out["conclusion"]
    assert modes[0]["items"]["elevate"]["state"] == UNKNOWN


def test_finalize_elevation_when_no_sandbox_ever_started_says_not_verified():
    """11-16 真机的情形: 三种关法的 Python 都没起来, 只有对照试了提权 -- 不能写“三种关法都要不到”."""
    modes = [mode("appcontainer", UNKNOWN, "没验: Python 没起来"), mode("restricted_downgrade", UNKNOWN, "没验: Python 没起来"),
             mode("restricted_sids", UNKNOWN, "没验: Python 没起来"), mode("control", CAN)]
    out = measure.finalize_elevation(modes, True)
    assert out["valid"] is None and out["conclusion"].startswith("没验") and "要不到" not in out["conclusion"]
    assert "三种关法都要不到" not in out["conclusion"] and "没有一种规格里的配法把 Python 起起来" in out["conclusion"]
    assert all(m["items"]["elevate"]["state"] == UNKNOWN for m in modes[:3])


def test_finalize_elevation_names_the_modes_that_did_not_run():
    modes = [mode("appcontainer", CANNOT), mode("restricted_downgrade", UNKNOWN), mode("restricted_sids", UNKNOWN),
             mode("control", CAN)]
    for m, label in zip(modes, ("应用容器", "受限令牌·只降级", "受限令牌·加限制SID", "对照")):
        m["label"] = label
    out = measure.finalize_elevation(modes, True)
    assert out["valid"] is True and "应用容器 要不到" in out["conclusion"]
    assert "受限令牌·只降级、受限令牌·加限制SID 没验" in out["conclusion"] and "三种关法都要不到" not in out["conclusion"]


def test_finalize_elevation_ignores_diagnostic_variants_for_the_clean_conclusion():
    modes = [mode("appcontainer", CANNOT), mode("restricted_downgrade", CANNOT), mode("restricted_sids", CANNOT),
             mode("control", CAN)]
    modes[1]["diagnostic"] = True            # 只有诊断配法(完整性级别不降)起来了: 不算规格里的“要不到”
    out = measure.finalize_elevation(modes, True)
    assert "三种关法都要不到" not in out["conclusion"] and "没验" in out["conclusion"]


def test_verdict_when_python_never_started_is_not_a_clean_bill():
    items = {k: Item(UNKNOWN) for k in measure.ITEM_KEYS}
    items["python"] = Item(CANNOT, 87)
    v = measure.mode_verdict("restricted_sids", items)
    assert v["ok"] is False and "没验" in v["text"] and "不能当成“已隔离”" in v["text"] and "能用" not in v["text"].replace("不能用", "")


def test_verdict_of_a_diagnostic_variant_is_never_a_pass():
    v = measure.mode_verdict("restricted_downgrade", items_of(), diagnostic=True)
    assert v["ok"] is None and v["text"].endswith("不算数）") and "只有诊断配法起来了" in v["text"]   # 保险: 永远不是“能用”
    assert measure.mode_verdict("control", {k: Item(CAN) for k in measure.ITEM_KEYS}, diagnostic=True)["ok"] is True


def test_process_gone_state():
    f = measure.process_gone_state
    assert f(None, None, None, None) is True            # 没起进程
    assert f(None, None, None, "ELEVATED") is None      # 文件出现了, 却没有进程编号
    assert f(10, "exited", None, "ELEVATED") is True
    assert f(10, "timeout", False, "ELEVATED") is True
    assert f(10, "timeout", True, "ELEVATED") is False
    assert f(10, None, None, "ELEVATED") is None


def test_describe_token_in_words():
    out = measure.describe_token({"elevated": 0, "elev_type": 3, "il": 0x1000, "appcontainer": 1, "restricted": 0})
    assert out == {"是不是提权的": "否", "提权类型": "受限(未提权)", "完整性级别": "低", "是不是应用容器": "是",
                   "有没有限制SID(受限令牌)": "否"}
    assert measure.describe_token({"il": 0x2345})["完整性级别"] == 0x2345


def test_cells_and_codes():
    assert measure.item_cell({"state": CAN}) == "做得到"
    assert measure.item_cell({"state": CANNOT, "code": 5}) == "做不到(5)"
    assert measure.item_cell({"state": ERROR, "code": 3221225781}) == "出错(0xC0000135)"
    assert measure.item_cell({"state": UNKNOWN, "code": 5}) == "没验"
    assert measure.item_cell({"state": INVALID}) == "测法不成立"
    assert measure.fmt_code(None) == "" and measure.fmt_code(0x10000) == "0x10000"


# ---------------------------------------------------------------- 限制 SID 的配法、SDDL
def test_variants_shape_matches_the_description():
    v = measure.VARIANTS
    # 应用容器: 先不登记 (最小环境、多几个变量), 起不来才登记 (同样两种环境)
    assert [(x.register, x.env_ext, x.env_inherit, x.diagnostic) for x in v["appcontainer"]] == [
        (False, False, False, False), (False, True, False, False), (True, False, False, False),
        (True, True, False, False), (True, False, True, True)]       # 最后一个只是诊断: 环境让子进程继承
    down = v["restricted_downgrade"]
    assert len(down) == 3 and down[0].restrict == () and down[0].low and not down[0].diagnostic
    assert down[1].diagnostic and not down[1].low and not down[1].plain      # 第 2 个只是诊断 (完整性级别不降), 不是规格
    assert down[2].diagnostic and not down[2].low and down[2].plain          # 第 3 个也是诊断: 不去特权、不改组、不降级
    sids = v["restricted_sids"]
    assert len(sids) == 3 and all("tok" in x.restrict for x in sids)
    assert not any("pkg" in x.restrict for x in sids)                      # 应用容器的 SID 不再当限制 SID (真机 87)
    assert [x.diagnostic for x in sids] == [False, False, True] and sids[2].low is False
    assert sids[2].restrict == sids[1].restrict                            # 诊断那种就是“松”但不降完整性级别
    assert sw.SID_USERS not in v["restricted_sids"][0].restrict       # 严的那种不含 Users
    assert len(v["control"]) == 1
    assert [m for m, _ in measure.MODES] == ["control", "appcontainer", "restricted_downgrade", "restricted_sids"]   # 对照第一个跑


def test_resolve_restrict_sids():
    tok, logon = sw.sandbox_token_sid("x.y"), sw.sid_from_string("S-1-5-5-0-9")
    out = measure.resolve_restrict_sids(("tok", sw.SID_USERS, "logon"), tok, logon)
    assert out == [tok, sw.sid_from_string(sw.SID_USERS), logon]
    assert measure.resolve_restrict_sids(("tok", "logon"), tok, None) == [tok]


def test_sandbox_token_sid_is_a_stable_fabricated_local_looking_sid():
    sid = sw.sandbox_token_sid("wm.ayane.isolation.proto")
    text = sw.sid_to_string(sid)
    assert text.startswith("S-1-5-21-") and text.endswith("-1337") and text.count("-") == 7
    assert sid == sw.sandbox_token_sid("wm.ayane.isolation.proto") and sid != sw.sandbox_token_sid("other.name")
    assert not text.startswith("S-1-15-")                              # 不是应用容器那一族的 SID


def test_extended_env_adds_only_the_named_variables(monkeypatch):
    for name in ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "ProgramData", "SystemDrive"):
        monkeypatch.setenv(name, "X:\\" + name)
    monkeypatch.setenv("SECRET_KEY", "nope")
    base = {"SystemRoot": "C:\\Windows", "PATH": "p"}
    out = measure.extended_env(base)
    assert set(out) == set(base) | {"LOCALAPPDATA", "APPDATA", "USERPROFILE", "ProgramData", "SystemDrive"}
    assert base == {"SystemRoot": "C:\\Windows", "PATH": "p"}           # 不改传进来的
    monkeypatch.delenv("APPDATA")
    assert "APPDATA" not in measure.extended_env(base)


def test_sddl_principals_maps_names_and_hides_other_accounts():
    sddl = ("D:PAI(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;0x1200a9;;;S-1-15-2-5-6)"
            "(A;OICIID;FA;;;S-1-5-21-1-2-3-1001)(A;;FA;;;S-1-5-21-1-2-3-513)(A;;FA;;;S-1-5-5-0-777)(A;;FA;;;AC)(A;;FA;;;BU)")
    got = measure.sddl_principals(sddl, "S-1-5-21-1-2-3-1001", "S-1-15-2-5-6")
    assert got == ["SYSTEM", "Administrators", "<沙箱SID>", "<本人>", "<其他账号或组>", "<登录会话>",
                   "ALL APPLICATION PACKAGES", "Users"]
    got = measure.sddl_principals("D:(A;;FA;;;S-1-5-21-1-2-3-1337)(A;;FA;;;S-1-5-21-1-2-3-1001)",
                                  "S-1-5-21-1-2-3-1001", None, "S-1-5-21-1-2-3-1337")
    assert got == ["<沙箱令牌SID>", "<本人>"]                           # 编出来的令牌 SID 也长得像 S-1-5-21, 要先认出来


# ---------------------------------------------------------------- 拷运行时的清单
def test_runtime_copy_plan_follows_the_description(tmp_path):
    src = make_fake_runtime(tmp_path / "py")
    got = sorted(str(rel).replace("\\", "/") for _, rel in measure.plan_runtime_copy(src))
    assert got == sorted([
        "python.exe", "python310.dll", "python3.dll", "vcruntime140.dll",
        "DLLs/_socket.pyd", "DLLs/libffi-7.dll",
        "Lib/os.py", "Lib/encodings/__init__.py", "Lib/json/__init__.py",
    ])


def test_copy_runtime_copies_files_and_counts_bytes(tmp_path):
    src = make_fake_runtime(tmp_path / "py")
    plan = measure.plan_runtime_copy(src)
    dst = tmp_path / "out"
    dst.mkdir()
    count, size = measure.copy_runtime(plan, dst)
    assert count == 9 and size == sum((dst / rel).stat().st_size for _, rel in plan)
    assert (dst / "Lib" / "encodings" / "__init__.py").is_file() and (dst / "python.exe").read_bytes() == b"MZ"
    assert not (dst / "pythonw.exe").exists() and not (dst / "Lib" / "test").exists()


def test_real_runtime_plan_is_close_to_the_28_mb_in_the_description():
    src = Path(os.sys.base_prefix)
    if not (src / "Lib" / "os.py").is_file():
        pytest.skip("本机没有标准布局的 Python")
    plan = measure.plan_runtime_copy(src)       # 只数大小, 不拷
    total = sum(p.stat().st_size for p, _ in plan) / 1e6
    assert 15 < total < 40
    names = {str(rel).replace("\\", "/") for _, rel in plan}
    assert {"python.exe", "Lib/os.py", "Lib/encodings/utf_8.py", "DLLs/_ctypes.pyd", "DLLs/_socket.pyd"} <= names
    assert not any("site-packages" in n or "__pycache__" in n or n.endswith(".pyc") for n in names)


# ---------------------------------------------------------------- 起探针的参数
def test_minimal_env_has_five_names_and_only_system_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    env = measure.minimal_env(tmp_path / "home")
    assert set(env) == {"SystemRoot", "windir", "PATH", "TEMP", "TMP"}
    assert env["PATH"] == "C:\\Windows\\System32;C:\\Windows"
    assert env["TEMP"] == env["TMP"] == str(tmp_path / "home")


def test_probe_command_uses_isolated_flags_and_the_copied_runtime(tmp_path):
    layout = measure.make_layout(tmp_path)
    exe, args = measure.probe_command(layout)
    assert exe == str(tmp_path / "work" / "pyrt" / "python.exe")
    assert args == ["-I", "-B", "-S", str(tmp_path / "work" / "probe" / "child_probe.py")]


def test_layout_has_the_directories_in_the_description(tmp_path):
    layout = measure.make_layout(tmp_path)
    assert layout.home == tmp_path / "work" / "home" and layout.decoy_file == tmp_path / "work" / "decoy" / "secret.txt"
    assert layout.results == tmp_path / "results" and layout.marker.parent == layout.work


# ---------------------------------------------------------------- 删目录
def test_rmtree_robust_removes_read_only_files(tmp_path):
    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    f = root / "a" / "b" / "ro.txt"
    f.write_text("x")
    os.chmod(f, stat.S_IREAD)
    assert measure.rmtree_robust(root) is None and not root.exists()
    assert measure.rmtree_robust(root) is None   # 本来就没有也算删干净了


# ---------------------------------------------------------------- 报告
def sample_report() -> dict:
    def mk(mid, label, **states):
        items = {k: {"state": CANNOT, "code": 5, "note": ""} for k in measure.ITEM_KEYS}
        items["python"] = {"state": CAN, "code": None, "note": ""}
        items["write_home"] = {"state": CAN, "code": None, "note": ""}
        items.update(states)
        return {"id": mid, "label": label, "variant": "只降级", "start_ms": 123.4, "items": items,
                "tries": [{"配法": "只降级", "起来了吗": "起来了"}], "token": {"完整性级别": "低"},
                "verdict": {"text": "能用"}}
    return {"started_at": "2026-11-14T12:00:00-08:00", "exit_code": 0,
            "environment": {"python": "3.10.2", "bits": 64, "windows": "10.0.26200"},
            "uac_settings": {"EnableLUA": 1, "ConsentPromptBehaviorAdmin": 0, "PromptOnSecureDesktop": 0},
            "elevation": {"tried": True, "conclusion": "测法成立", "command": "cmd /c x"},
            "appcontainer": {"summary": "只按名字算出了 SID，没有登记"},
            "modes": [mk("appcontainer", "应用容器"), mk("restricted_downgrade", "受限令牌·只降级", read_fake={"state": CAN}),
                      mk("restricted_sids", "受限令牌·加限制SID"),
                      mk("control", "对照(不关)", **{k: {"state": CAN} for k in measure.ITEM_KEYS})],
            "steps": [{"n": 1, "name": "自查", "ms": 5}], "acl": {"work\\home": ["<本人>", "<沙箱SID>"]},
            "cleanup": {"work 目录": "已删"}, "pii_scan": {"result": "没找到个人信息"}}


def test_report_table_has_a_row_per_mode_and_a_column_per_item():
    text = measure.render_report_md(sample_report())
    table = [ln for ln in text.splitlines() if ln.startswith("|")][:6]
    assert len(table) == 6                                        # 表头 + 分隔 + 四行
    assert table[0].count("|") == 1 + len(measure.ITEM_KEYS) + 2 + 1
    assert all(ln.count("|") == table[0].count("|") for ln in table)
    assert "| 对照(不关) | 做得到 | 做得到 |" in table[5]
    assert "做不到(5)" in table[2] and "做得到" in table[3]       # 只降级那行的“读假保管箱”是做得到
    for title in measure.ITEM_TITLES.values():
        assert title in table[0]
    assert "起一次(ms)" in table[0] and "123" in table[2] and "能用" in table[2]
    assert "没找到个人信息" in text and "三项设置" in text and "10.0.26200" in text


def test_report_renders_even_when_the_run_stopped_early():
    assert "隔离原型测量结果" in measure.render_report_md({})
    partial = {"modes": [{"id": "appcontainer", "label": "应用容器"}], "errors": ["boom"]}
    text = measure.render_report_md(partial)
    assert "没验" in text and "boom" in text


# ---------------------------------------------------------------- 11-16 复核后加的
def test_redactor_from_env_never_asks_platform_for_the_node_name(monkeypatch):
    """platform.node() 在 Python 3.10 上第一次会起一个 cmd /c ver; 电脑名改用环境变量和 socket.gethostname()."""
    import platform
    import subprocess

    def boom(*_a, **_k):
        raise AssertionError("不许调")
    monkeypatch.setattr(platform, "node", boom)
    monkeypatch.setattr(platform, "uname", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setenv("COMPUTERNAME", "TESTBOX-01")
    rd = measure.Redactor.from_env()
    assert "TESTBOX-01" in rd.names


def test_root_listing_allows_the_notes_and_the_saved_plan_but_nothing_else():
    ok = ["run_me.cmd", "看看会做什么.cmd", "measure.py", "sandbox_win.py", "child_probe.py", "README.md", "tests", "results",
          "真机结果分析-20261116.md", "plan-output-20261116.txt"]
    assert measure.unexpected_root_names(ok) == []
    assert measure.unexpected_root_names(ok + ["work", "__pycache__", "oops.exe"]) == ["__pycache__", "oops.exe", "work"]


def test_the_real_prototype_directory_passes_the_cleanup_listing_check():
    """下次真跑的退出码不能因为目录里本来就有的说明文件变成 1: 对着真的原型目录核对 (work 是跑的时候才有的, 不在这里)."""
    from fakes import ISO
    assert measure.unexpected_root_names([p.name for p in ISO.iterdir() if p.name not in ("__pycache__",)]) == []


def test_d1_name_list_is_fixed_and_relative(tmp_path):
    layout = measure.make_layout(tmp_path)
    got = measure.d1_paths(layout)
    labels = [label for label, _ in got]
    assert labels == [r"pyrt\python.exe", r"pyrt\python310.dll", r"pyrt\python3.dll", r"pyrt\vcruntime140.dll",
                      r"pyrt\vcruntime140_1.dll", r"pyrt\Lib\os.py", r"probe\child_probe.py", "home",
                      r"System32\ucrtbase.dll", r"System32\version.dll", r"System32\ws2_32.dll", r"System32\shlwapi.dll"]
    assert all(not label.startswith(("C:", "G:", "\\")) for label in labels)       # 写进结果的只有相对名字


def test_system_cmd_does_nothing_but_exit_7():
    exe, args = measure.system_cmd()
    assert exe.lower().endswith(r"\system32\cmd.exe") and args == ["/d", "/c", "exit", "7"]

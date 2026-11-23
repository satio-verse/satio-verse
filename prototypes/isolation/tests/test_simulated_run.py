"""整套流程的模拟 (替身见 fakes.py): 让 measure.run() 从自查跑到收尾, 验测量脚本自己的逻辑 --
步骤顺序、起探针的参数、判定、"测法不成立"、超时、中途出错也要收尾、残留的处理、结果里没有个人信息.
这里没有起任何真进程、没改任何权限、没登记任何身份; 隔离本身行不行, 只有用户双击 run_me.cmd 才知道."""
from __future__ import annotations

import json
import sys

import pytest

import measure
import sandbox_win as sw
from fakes import FAKE_COMPUTER, FAKE_NAME, LOGON, PKG, USER, Sim

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="流程里用到 Windows 的版本信息")

SCENE_ITEMS = measure.ITEM_KEYS


def run_main(sim: Sim, capsys) -> tuple[int, str]:
    if not measure.path_is_safe(str(sim.iso)):
        pytest.skip("临时目录的路径含不允许的字符, 提权命令不会生成")
    code = measure.main([])
    return code, capsys.readouterr().out


def modes_by_id(report: dict) -> dict:
    return {m["id"]: m for m in report["modes"]}


def state(report: dict, mode: str, item: str) -> str:
    return modes_by_id(report)[mode]["items"][item]["state"]


def test_full_run_with_the_elevation_test(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path)
    code, out = run_main(sim, capsys)
    assert code == 0, out
    for n in range(1, 7):
        assert f"第 {n} 步" in out
    assert sim.prompts == [measure.ELEV_QUESTION]            # 只问一次, 问的是“真试吗”, 默认不试
    assert out.index("== 第 4 步") < out.index("现在先问你一次") < out.index("== 第 5 步")
    # 收尾: work 和临时文件夹里的子目录都没了, 最后只留 results\
    assert not (sim.iso / "work").exists()
    assert [p.name for p in sim.sys_temp.iterdir()] == []
    assert sorted(p.name for p in sim.iso.iterdir()) == ["child_probe.py", "results"]
    files = sorted(p.name for p in sim.results.iterdir())
    assert files == ["elev_control.txt", "report.json", "report.md"]   # 三种关法要不到, 所以只有对照留了文件
    report = sim.report()

    # 判定
    verdicts = {m["id"]: m["verdict"] for m in report["modes"]}
    assert verdicts["appcontainer"]["text"] == "能用"
    assert verdicts["restricted_downgrade"]["problems"] == ["读得到假保管箱", "读得到假文件", "连得上本机回环"]
    assert verdicts["restricted_sids"]["problems"] == ["连得上本机回环"]
    assert verdicts["control"]["ok"] is True
    assert report["elevation"]["conclusion"] == "测法成立 (对照写了 ELEVATED); 三种关法都要不到"
    assert state(report, "control", "elevate") == "can" and state(report, "appcontainer", "elevate") == "cannot"
    assert modes_by_id(report)["appcontainer"]["items"]["elevate"]["code"] == 5
    assert modes_by_id(report)["control"]["elevate_detail"]["结果文件"] == "ELEVATED"
    assert modes_by_id(report)["appcontainer"]["elevate_detail"]["结果文件"] == "没出现"
    assert modes_by_id(report)["appcontainer"]["elevate_detail"]["这次试出来的进程已经没有了"] == "是"
    assert modes_by_id(report)["appcontainer"]["elevate_detail"]["结果文件夹里多出的文件"] == "没多"
    assert modes_by_id(report)["control"]["elevate_detail"]["结果文件夹里多出的文件"] == "只多了预期的那一个"
    assert modes_by_id(report)["control"]["token"]["完整性级别"] == "中"
    assert "elev_<关法>.txt" in report["elevation"]["command"]

    # 收尾核对
    cleanup = report["cleanup"]
    assert cleanup["起过的探针个数"] == 4 and cleanup["全部已退出"] is True and cleanup["作业里没有剩下的进程"] is True
    assert cleanup["work 目录"] == "已删" and cleanup["临时文件夹里的子目录"] == "已删"
    assert cleanup["删登记的沙箱身份"] == "没登记过, 不用删" and cleanup["迟到出现的提权结果文件"] == "没有"
    assert sim.calls["register"] == 0 and sim.calls["unregister"] == 0
    assert report["appcontainer"]["summary"].startswith("只按名字算出了 SID，没有登记")
    assert [s["n"] for s in report["steps"]] == [1, 2, 3, 4, 5, 6]

    # 只给沙箱身份这几样权限, 别的目录一个都不动
    grants = [(p.rsplit("\\", 1)[-1], entries[0][0], entries[0][1], entries[0][2]) for p, entries, _ in sim.calls["set_dacl"]]
    assert grants == [("pyrt", sw.sid_from_string(PKG), sw.ACCESS_READ_EXEC, 3),
                      ("probe", sw.sid_from_string(PKG), sw.ACCESS_READ_EXEC, 3),
                      ("home", sw.sid_from_string(PKG), sw.ACCESS_MODIFY, 3),
                      ("home", sw.sid_from_string(USER), sw.FILE_ALL_ACCESS, 3)]      # 改标签要 WRITE_OWNER: 先给本人完全控制
    assert [p.rsplit("\\", 1)[-1] for p in sim.calls["label"]] == ["home"]
    assert len(sim.calls["protect"]) == 1 and "wm_iso_" in sim.calls["protect"][0]

    # 起探针的参数: 四次, 顺序固定, 最小环境、-I -B -S、拷来的运行时、沙箱自己的文件夹
    assert [c["mode"] for c in sim.calls["spawn"]] == ["control", "appcontainer", "restricted_downgrade", "restricted_sids"]   # 对照先跑
    for call in sim.calls["spawn"]:
        assert call["exe"].endswith("work\\pyrt\\python.exe") and call["args"][:3] == ["-I", "-B", "-S"]
        assert call["args"][3].endswith("work\\probe\\child_probe.py") and call["cwd"].endswith("work\\home")
        assert set(call["env"]) == {"SystemRoot", "windir", "PATH", "TEMP", "TMP"}
    assert sim.calls["spawn"][1]["appc"] == sw.sid_from_string(PKG) and sim.calls["spawn"][0]["appc"] is None
    admins = sw.sid_from_string(sw.SID_ADMINS)
    assert sim.calls["tokens"][0] == {"disable": [admins], "restrict": [], "low": True, "default_dacl": [],
                                      "flags": sw.DISABLE_MAX_PRIVILEGE}
    tok = sw.sandbox_token_sid(measure.APPC_NAME)
    assert sim.calls["tokens"][1]["restrict"] == [tok, sw.sid_from_string(sw.SID_ALL_APP_PACKAGES)]   # 不用应用容器的 SID
    assert sim.calls["tokens"][1]["default_dacl"] == [tok]                         # 有限制 SID 的才改默认 DACL
    assert sim.calls["tokens"][1]["disable"] == [admins] and sim.calls["tokens"][1]["low"] is True
    assert len(sim.calls["tokens"]) == 2   # 严的那种就起来了, 没有再试松的

    # 结果里没有个人信息 (用户名、电脑名、SID、家目录), 且扫描记了一笔
    text = (sim.results / "report.json").read_text(encoding="utf-8") + (sim.results / "report.md").read_text(encoding="utf-8")
    for secret in (FAKE_NAME, FAKE_COMPUTER, USER, LOGON, str(sim.fake_home), str(sim.fake_home).replace("\\", "\\\\")):
        assert secret.lower() not in text.lower(), secret
    assert report["pii_scan"]["result"] == "没找到个人信息"
    assert "<本人>" in json.dumps(report, ensure_ascii=False)
    assert "Zed_Test_User" not in out and USER not in out and str(sim.fake_home) not in out   # 窗口里打出来的也一样
    table = [ln for ln in (sim.results / "report.md").read_text(encoding="utf-8").splitlines() if ln.startswith("|")][:6]
    assert len(table) == 6 and "对照正常" in table[2]           # 表里对照排第一行


def test_run_without_the_elevation_test(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, answer="")
    code, out = run_main(sim, capsys)
    assert code == 0, out
    assert sorted(p.name for p in sim.results.iterdir()) == ["report.json", "report.md"]   # 没有任何提权结果文件
    report = sim.report()
    for mode in report["modes"]:
        assert mode["items"]["elevate"]["state"] == "unknown" and "elevate_detail" not in mode
        assert mode["token"]["是不是提权的"] == "否"        # 只读令牌信息, 照样记下来
    assert report["elevation"]["tried"] is False and report["elevation"]["conclusion"].startswith("没验")
    appc = modes_by_id(report)["appcontainer"]["verdict"]
    assert appc["ok"] is None and "要管理员权限" in appc["text"]
    assert modes_by_id(report)["control"]["verdict"]["ok"] is True      # 对照不因为用户选了不试就算有毛病
    assert "elevate" not in [c for child in sim.children for c in child.asked]
    assert "用户没输入 y，不试" == report["elevation"]["choice"]


@pytest.mark.parametrize("answer, expected", [("y", True), ("Y", True), (" y ", True), ("y\r", True),
                                              ("", False), ("yes", False), ("是", False), (" ", False), ("n", False),
                                              ("1", False), ("ok", False), ("yy", False)])
def test_only_y_means_yes(answer, expected):
    assert measure.wants_elevation_test(answer) is expected


def test_end_of_input_means_no(monkeypatch, tmp_path, capsys):
    """窗口没接键盘 (输入到头了) 也当作不试."""
    sim = Sim(monkeypatch, tmp_path)

    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    run_main(sim, capsys)
    assert sim.report()["elevation"]["tried"] is False and not list(sim.results.glob("elev_*"))


def test_control_that_does_not_elevate_means_the_method_is_invalid(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, elevate={"control": "deny"})
    code, out = run_main(sim, capsys)
    assert code == 0, out
    report = sim.report()
    assert report["elevation"]["valid"] is False and "测法不成立" in report["elevation"]["conclusion"]
    for mode in ("appcontainer", "restricted_downgrade", "restricted_sids"):
        assert state(report, mode, "elevate") == "invalid"
        assert modes_by_id(report)[mode]["elevate_raw"]["state"] == "cannot"
    assert "测法不成立" in (sim.results / "report.md").read_text(encoding="utf-8")
    assert "要得到了管理员权限" not in json.dumps(report, ensure_ascii=False)
    ctl = modes_by_id(report)["control"]["verdict"]
    assert ctl["ok"] is False and "对照有毛病" in ctl["text"]


def test_a_sandbox_that_gets_admin_is_flagged_loudly(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, elevate={"restricted_downgrade": "grant"})
    run_main(sim, capsys)
    report = sim.report()
    assert "有关法要得到了管理员权限" in report["elevation"]["conclusion"]
    assert "要得到了管理员权限" in modes_by_id(report)["restricted_downgrade"]["verdict"]["problems"]
    assert sorted(p.name for p in sim.results.glob("elev_*")) == ["elev_control.txt", "elev_restricted_downgrade.txt"]
    assert modes_by_id(report)["appcontainer"]["verdict"]["text"] == "能用"


def test_new_process_that_is_not_elevated_is_still_not_admin(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, elevate={"appcontainer": "notelevated"})
    run_main(sim, capsys)
    item = modes_by_id(sim.report())["appcontainer"]["items"]["elevate"]
    assert item["state"] == "cannot" and "NOT_ELEVATED" in item["note"]


def test_appcontainer_that_cannot_start_registers_then_cleans_up(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"appcontainer": ["error:2", "error:2", "error:203", "error:203", "error:203"]})
    code, out = run_main(sim, capsys)
    assert code == 0, out
    report = sim.report()
    app = modes_by_id(report)["appcontainer"]
    assert app["items"]["python"]["state"] == "cannot" and app["items"]["python"]["code"] == 203
    assert all(app["items"][k]["state"] == "unknown" for k in SCENE_ITEMS[1:])
    assert [t["配法"] for t in app["tries"]][:4] == ["只按名字算出 SID, 没登记", "只按名字算出 SID, 没登记, 环境里多几个变量",
                                                   "登记后再起", "登记后再起, 环境里多几个变量"]
    assert app["tries"][4]["配法"].startswith("诊断: 登记后再起, 环境让子进程继承")
    assert "CreateProcessW" in app["tries"][0]["错误"] and "错误码 2" in app["tries"][0]["错误"]
    assert "LOCALAPPDATA" in app["tries"][1]["环境变量"] and "环境变量" not in app["tries"][0]
    # 不登记的两种都不行才登记: 登记后的三种各起一次探针 + 各起一次 D2 的 cmd, 每次起都登记一次 (已登记的再登记不出错), 收尾删一次
    assert sim.calls["register"] == 6 and sim.calls["unregister"] == 1
    ac = [c for c in sim.calls["spawn"] if c["mode"] == "appcontainer"]
    assert [bool(c["env"].get("LOCALAPPDATA")) for c in ac[:4]] == [False, True, False, True]
    assert ac[4]["env"] is None                                              # 诊断配法才让子进程继承环境
    assert len(ac) == 5 and len(sim.calls["cmd"]) == 5                       # 每种起不来的配法, 各用同一个身份起一次 cmd (D2)
    assert all(t["同一个身份起系统自带的cmd(exit 7)"].startswith("起来了, 退出码 7") for t in app["tries"])
    assert report["cleanup"]["删登记的沙箱身份"] == "已删"
    assert report["appcontainer"]["summary"] == "没有一种配法起得来；这次登记过沙箱身份；收尾：已删"
    assert app["verdict"]["ok"] is False and "Python 起不来" in app["verdict"]["problems"]
    assert "没验" in app["verdict"]["text"]                                   # 不是“已隔离”也不是“不行”, 是没量到
    assert modes_by_id(report)["control"]["verdict"]["ok"] is True            # 别的行不受影响


def test_a_file_held_by_someone_else_is_retried_once_and_other_errors_are_not(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"appcontainer": ["error:32", "ok"],
                                                       "restricted_downgrade": ["error:5", "error:5", "error:5"]})
    code, out = run_main(sim, capsys)
    assert code == 0, out
    report = sim.report()
    app = modes_by_id(report)["appcontainer"]
    assert app["variant"] == "只按名字算出 SID, 没登记" and "重试" in app["tries"][0] and "32" in app["tries"][0]["重试"]
    assert sim.calls["register"] == 0                                    # 重试的是同一种配法, 没有跑去登记
    down = modes_by_id(report)["restricted_downgrade"]
    assert "重试" not in down["tries"][0] and down["tries"][0]["起来了吗"] == "没起来"   # 拒绝访问不重试
    assert down["items"]["python"] == {"state": "cannot", "code": 5, "note": "Python 没起来 (见“试过”)"}
    modes_started = [c["mode"] for c in sim.calls["spawn"]]
    assert modes_started.count("appcontainer") == 2 and modes_started.count("restricted_downgrade") == 3   # 三种配法各一次, 没重试


def test_transient_error_codes():
    assert measure.is_transient(sw.SandboxError("CreateProcessW", 32)) and measure.is_transient(sw.SandboxError("x", 33))
    assert not measure.is_transient(sw.SandboxError("CreateProcessW", 5)) and not measure.is_transient(OSError(32, "x"))


def test_appcontainer_unregistered_start_fails_but_registered_start_works(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"appcontainer": ["no_pong", "no_pong", "ok"]})
    run_main(sim, capsys)
    report = sim.report()
    app = modes_by_id(report)["appcontainer"]
    assert app["variant"] == "登记后再起" and app["tries"][0]["起来了吗"] == "创建了进程，但没回话"
    assert app["tries"][0]["没回话的原因"] == "timeout" and app["tries"][0]["超时"] is True
    assert "timeout" not in app                       # 后一种配法起来了, 这个探针自己没超时
    assert report["appcontainer"]["summary"].startswith("这次登记了沙箱身份")
    assert sim.calls["register"] == 1 and sim.calls["unregister"] == 1
    # 对照 1 + 应用容器 3 + 受限令牌各 1 = 6 个探针; 没回话的两个各用同一个身份起了一次 cmd (D2), 也结束了、也核对了
    assert report["cleanup"]["起过的探针个数"] == 8 and len(sim.calls["cmd"]) == 2


def test_restricted_sids_falls_back_to_the_loose_variant(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"restricted_sids": ["no_pong", "ok"]})
    run_main(sim, capsys)
    report = sim.report()
    mode = modes_by_id(report)["restricted_sids"]
    assert mode["variant"].startswith("松") and len(mode["tries"]) == 2
    tok = sw.sandbox_token_sid(measure.APPC_NAME)
    loose_tokens = [t for t in sim.calls["tokens"] if len(t["restrict"]) == 5]      # 严的那种 (2 个) 起来了又没回话, 松的是 5 个
    assert len(loose_tokens) == 1
    loose = loose_tokens[0]["restrict"]
    assert loose == [tok, *[sw.sid_from_string(s) for s in (sw.SID_USERS, sw.SID_EVERYONE, sw.SID_RESTRICTED, LOGON)]]
    assert loose_tokens[0]["default_dacl"] == [tok] and loose_tokens[0]["low"] is True
    assert mode["items"]["python"]["state"] == "can"


def test_replay_of_the_real_machine_where_no_sandbox_started(monkeypatch, tmp_path, capsys):
    """11-16 真机: 应用容器 2/203、只降级起来了但没回话 (0xC0000135)、限制 SID 两种配法 CreateRestrictedToken 都是 87,
    只有对照正常. 结论不许写“三种关法都要不到”, 提权那一列这三行写“没验”, 87 要逐个 SID 单独试出来."""
    tok = sw.sandbox_token_sid(measure.APPC_NAME)
    sim = Sim(monkeypatch, tmp_path, token_fail={tok},
              spawn_plan={"appcontainer": ["error:2", "error:2", "error:203", "error:203", "error:203"],
                          "restricted_downgrade": ["no_pong", "no_pong", "no_pong"]})
    code, out = run_main(sim, capsys)
    assert code == 0, out
    report = sim.report()
    text = (sim.results / "report.md").read_text(encoding="utf-8")
    assert report["elevation"]["valid"] is None and report["elevation"]["conclusion"].startswith("没验")
    assert "三种关法都要不到" not in text + out + json.dumps(report, ensure_ascii=False)
    for mode in ("appcontainer", "restricted_downgrade", "restricted_sids"):
        assert state(report, mode, "python") == "cannot" and state(report, mode, "elevate") == "unknown"
        assert all(state(report, mode, k) == "unknown" for k in SCENE_ITEMS[1:])
        assert "没验" in modes_by_id(report)[mode]["verdict"]["text"]
    assert modes_by_id(report)["control"]["verdict"]["ok"] is True and state(report, "control", "elevate") == "can"
    sids = modes_by_id(report)["restricted_sids"]
    assert [t["起来了吗"] for t in sids["tries"]] == ["没起来"] * 3 and "错误码 87" in sids["tries"][0]["错误"]
    assert not any("同一个身份起系统自带的cmd(exit 7)" in t for t in sids["tries"])     # 令牌都没造出来, 没有“同一个身份”可用
    diag = sids["tries"][0]["逐个限制SID单独试(0=收,其余是错误码)"]
    assert diag == {"沙箱令牌SID(编出来的)": 87, sw.SID_ALL_APP_PACKAGES: 0, "全部一起(不改组)": 87}
    assert LOGON not in json.dumps(report) and "登录SID(本次登录, 不写出)" in json.dumps(report, ensure_ascii=False)
    down = modes_by_id(report)["restricted_downgrade"]
    assert [t["配法"] for t in down["tries"]][:2] == ["只降级, 不加限制 SID", "诊断: 只降级, 但完整性级别不降 (中)"] and len(down["tries"]) == 3
    assert down["tries"][0]["没回话的原因"] == "timeout" and "diagnostic" not in down
    # 令牌的进程创建了却没回话: D1 (以令牌身份只读打开固定名单) 和 D2 (同身份起 cmd) 都做; 应用容器不是令牌, 只做 D2
    assert all("以同一个令牌的身份只读打开这几个文件(0=打得开,其余是错误码: 先是查属性,再是打开)" in t for t in down["tries"])
    assert all("同一个身份起系统自带的cmd(exit 7)" in t for t in down["tries"])
    assert not any("以同一个令牌的身份只读打开这几个文件(0=打得开,其余是错误码: 先是查属性,再是打开)" in t for t in modes_by_id(report)["appcontainer"]["tries"])
    assert all("同一个身份起系统自带的cmd(exit 7)" in t for t in modes_by_id(report)["appcontainer"]["tries"])
    assert sim.calls["register"] == 6 and sim.calls["unregister"] == 1 and report["cleanup"]["work 目录"] == "已删"


def test_inherited_environment_values_never_reach_the_results_or_the_window(monkeypatch, tmp_path, capsys):
    """第 5 种应用容器配法 (诊断) 让子进程继承整份环境: 用户环境里可能有密钥. 值不许出现在结果文件和窗口里."""
    secret = "sk-THIS-IS-A-SECRET-VALUE-12345"
    monkeypatch.setenv("MY_API_KEY", secret)
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"appcontainer": ["error:203"] * 4 + ["ok"]})
    code, out = run_main(sim, capsys)
    assert code == 0, out
    assert [c for c in sim.calls["spawn"] if c["mode"] == "appcontainer"][4]["env"] is None   # 真的是“继承”: 脚本这边没拿环境变量的值去拼任何东西
    report = sim.report()
    app = modes_by_id(report)["appcontainer"]
    assert app["diagnostic"] is True and app["variant"].startswith("诊断")
    all_text = (sim.results / "report.json").read_text(encoding="utf-8") + (sim.results / "report.md").read_text(encoding="utf-8") + out
    assert secret not in all_text and "MY_API_KEY" not in all_text       # 连名字都没有 (只记了那五个固定的名字)
    assert app["tries"][4]["环境变量"] == "让子进程继承本进程的环境(只是诊断)"
    assert "=" not in app["tries"][1]["环境变量"]                         # 加五个变量的那种: 只有名字的拼接
    # 规格里的配法一种都没起来: 这一行是“Python 做不到, 其余没验”; 诊断配法起来了, 量到的另放, 不算数
    assert app["verdict"]["ok"] is False and app["verdict"]["problems"] == ["Python 起不来"]
    assert "只有诊断配法起来了" in app["verdict"]["text"] and app["items"]["python"]["state"] == "cannot"
    assert app["diagnostic_items"]["python"]["state"] == "can" and app["diagnostic_items"]["write_home"]["state"] == "can"
    assert app["diagnostic_items"]["elevate"]["note"] == "没验 (诊断配法不发提权请求)"
    diag_child = [c for c in sim.children if c.mode == "appcontainer"][-1]
    assert "elevate" not in diag_child.asked and diag_child.asked                # 诊断配法起来了, 照样问其余各项, 但不发提权请求


def test_a_diagnostic_variant_that_gets_admin_is_not_counted_as_the_mode_getting_admin():
    def one(mid):
        return {"id": mid, "label": mid, "items": {"elevate": {"state": "can", "code": None, "note": ""}}}
    modes = [one("restricted_downgrade"), one("control")]
    modes[0]["diagnostic"] = True
    out = measure.finalize_elevation(modes, True)
    assert "leaks" not in out and "有关法要得到了管理员权限" not in out["conclusion"]


def test_a_diagnostic_variant_that_starts_is_reported_but_never_counts(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"restricted_downgrade": ["no_pong", "ok"]})
    code, out = run_main(sim, capsys)
    assert code == 0, out
    report = sim.report()
    down = modes_by_id(report)["restricted_downgrade"]
    assert down["diagnostic"] is True and down["variant"].startswith("诊断")
    lows = [t["low"] for t in sim.calls["tokens"]]
    # 第一种(降级)起来没回话: 先是它自己的令牌, 再是 D1 的令牌、D2 的令牌, 然后才是这个没把完整性级别降下去的诊断配法
    assert lows.count(False) == 1 and lows[:3] == [True, True, True] and lows[3] is False
    assert down["tries"][1]["完整性级别"].startswith("不降")
    assert down["verdict"]["ok"] is False and down["verdict"]["problems"] == ["Python 起不来"]
    assert "只有诊断配法起来了" in down["verdict"]["text"] and "diagnostic_items" in down
    assert down["items"]["python"]["state"] == "cannot" and down["diagnostic_items"]["python"]["state"] == "can"
    diag_child = [c for c in sim.children if c.mode == "restricted_downgrade"][-1]
    assert "elevate" not in diag_child.asked                                      # 诊断配法不发提权请求
    assert state(report, "restricted_downgrade", "elevate") == "unknown"
    assert "没验" in report["elevation"]["conclusion"] and "三种关法都要不到" not in report["elevation"]["conclusion"]
    assert "受限令牌·只降级 没验" in report["elevation"]["conclusion"]


def test_best_effort_token_step_failures_are_noted_not_fatal(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, token_note=f"SetTokenInformation(TokenDefaultDacl) 失败, 错误码 5 {FAKE_NAME}")
    code, out = run_main(sim, capsys)
    assert code == 0, out
    notes = sim.report()["notes"]["token_notes"]
    assert len(notes) == 2 and all("TokenDefaultDacl" in n and FAKE_NAME not in n for n in notes)   # 记下来, 且抹过了
    assert modes_by_id(sim.report())["restricted_sids"]["items"]["python"]["state"] == "can"


def test_a_probe_that_goes_silent_is_recorded_as_a_timeout(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"restricted_downgrade": ["hang_on_connect"]})
    code, out = run_main(sim, capsys)
    assert code == 0, out
    report = sim.report()
    mode = modes_by_id(report)["restricted_downgrade"]
    assert mode["timeout"] is True
    assert mode["items"]["connect"]["state"] == "error" and "timeout" in mode["items"]["connect"]["note"]
    assert mode["items"]["elevate"]["state"] == "error" and "探针没了" in mode["items"]["elevate"]["note"]
    assert mode["items"]["read_fake"]["state"] == "can"                      # 出事之前量到的照样留着
    assert "超过 20 秒" in (sim.results / "report.md").read_text(encoding="utf-8")


def test_an_error_in_the_middle_still_runs_the_cleanup(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path)

    def broken(path, entries, **kw):
        raise sw.SandboxError("SetNamedSecurityInfoW(DACL)", 1314)
    monkeypatch.setattr(sw, "set_dacl", broken)
    code, out = run_main(sim, capsys)
    assert code == 1
    assert not (sim.iso / "work").exists() and [p.name for p in sim.sys_temp.iterdir()] == []
    report = sim.report()
    assert any("SetNamedSecurityInfoW" in e and "1314" in e for e in report["errors"])
    assert report["cleanup"]["work 目录"] == "已删" and sim.calls["spawn"] == []     # 没走到起探针
    assert "中途出错" in out and report["modes"] == []


def test_keyboard_interrupt_during_the_probes_still_cleans_up(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path)
    real_spawn = sw.spawn

    def interrupted(*a, **kw):
        if len(sim.calls["spawn"]) >= 2:
            raise KeyboardInterrupt
        return real_spawn(*a, **kw)
    monkeypatch.setattr(sw, "spawn", interrupted)
    code, _ = run_main(sim, capsys)
    assert code == 1
    assert not (sim.iso / "work").exists()
    report = sim.report()
    assert report["errors"] == ["被 Ctrl+C 打断"] and len(report["modes"]) == 3   # 前两个量完了, 第三个只留了开头


def test_leftover_work_with_our_marker_is_cleaned_first(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path)
    old_temp = sim.sys_temp / "wm_iso_deadbeef"
    old_temp.mkdir()
    (old_temp / "fake_vault.txt").write_text("old")
    work = sim.iso / "work"
    work.mkdir()
    (work / "stale.txt").write_text("x")
    (work / measure.MARKER_NAME).write_text(json.dumps({"temp_dir": str(old_temp), "registered": True}), encoding="utf-8")
    code, out = run_main(sim, capsys)
    assert code == 0, out
    assert "发现上次没收尾的残留" in out and not old_temp.exists() and not work.exists()
    assert sim.calls["unregister"] == 1       # 上次登记的沙箱身份也删了
    assert sim.report()["cleanup"]["work 目录"] == "已删"


def test_leftover_work_without_our_marker_is_never_deleted(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path)
    work = sim.iso / "work"
    work.mkdir()
    (work / "precious.txt").write_text("keep me")
    code, out = run_main(sim, capsys)
    assert code == 2
    assert (work / "precious.txt").read_text() == "keep me"     # 收尾也没碰它
    assert "我不敢删" in out
    assert sim.report()["cleanup"]["work 目录"] == "这次没建过，没动"
    assert sim.calls["spawn"] == []


def test_a_preexisting_registration_is_treated_as_ours_and_removed(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, registered=True)
    code, out = run_main(sim, capsys)
    assert code == 0, out
    assert sim.calls["unregister"] == 1 and sim.report()["notes"]["profile_leftover_from_earlier_run"] is True


def test_listener_only_binds_loopback_and_is_closed_afterwards(monkeypatch, tmp_path, capsys):
    import socket
    binds = []
    real_bind = socket.socket.bind

    def spy(self, address):
        binds.append(address)
        return real_bind(self, address)
    monkeypatch.setattr(socket.socket, "bind", spy)
    sim = Sim(monkeypatch, tmp_path)
    run_main(sim, capsys)
    assert len(binds) == 1 and binds[0][0] == "127.0.0.1"
    assert sim.report()["notes"]["listener_selfcheck"] is True


D1_KEY = "以同一个令牌的身份只读打开这几个文件(0=打得开,其余是错误码: 先是查属性,再是打开)"
D2_KEY = "同一个身份起系统自带的cmd(exit 7)"


def test_no_diagnostic_child_or_impersonation_when_everything_starts(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path)
    run_main(sim, capsys)
    assert sim.calls["cmd"] == [] and sim.calls["open_as_token"] == []
    for mode in sim.report()["modes"]:
        assert all(D1_KEY not in t and D2_KEY not in t for t in mode["tries"])


def test_d1_records_only_relative_names_and_codes(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"restricted_sids": ["no_pong"]}, d1_codes={"os.py": [0, 5]})
    run_main(sim, capsys)
    report = sim.report()
    d1 = modes_by_id(report)["restricted_sids"]["tries"][0][D1_KEY]
    assert list(d1) == [label for label, _ in measure.d1_paths(measure.make_layout(sim.iso))]
    assert d1[r"pyrt\Lib\os.py"] == [0, 5] and d1[r"pyrt\python.exe"] == [0, 0]
    assert all(not label.startswith(("C:", "\\")) for label in d1)
    assert len(sim.calls["open_as_token"]) == 1 and len(sim.calls["cmd"]) == 1        # D1 和 D2 各一次, 只对没回话的那一种
    text = (sim.results / "report.json").read_text(encoding="utf-8")
    assert str(sim.fake_home).lower() not in text.lower() and FAKE_NAME not in text


def test_d1_that_cannot_impersonate_is_noted_not_fatal(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"restricted_downgrade": ["no_pong"]}, d1_fail=True)
    code, out = run_main(sim, capsys)
    assert code == 0, out
    rec = modes_by_id(sim.report())["restricted_downgrade"]["tries"][0]
    assert rec[D1_KEY].startswith("诊断没做成") and rec[D2_KEY].startswith("起来了, 退出码 7")


@pytest.mark.parametrize("opts, expected", [({"cmd_exit": 1}, "起来了, 但退出码是 1"),
                                            ({"cmd_hang": True}, "起来了, 但 5 秒内没退出 (已结束)"),
                                            ({"cmd_fail": {"restricted_downgrade": 87}}, "没起来")])
def test_d2_records_what_the_system_cmd_did(monkeypatch, tmp_path, capsys, opts, expected):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"restricted_downgrade": ["no_pong"]}, **opts)
    code, out = run_main(sim, capsys)
    assert code == 0, out
    assert modes_by_id(sim.report())["restricted_downgrade"]["tries"][0][D2_KEY].startswith(expected)


def test_d2_is_not_run_for_the_control_even_when_it_fails(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"control": ["error:5"]})
    code, out = run_main(sim, capsys)
    assert code == 0, out
    control = modes_by_id(sim.report())["control"]
    assert control["items"]["python"]["state"] == "cannot" and all(D2_KEY not in t for t in control["tries"])
    assert all(c["mode"] != "control" for c in sim.calls["cmd"])


def test_a_diagnostic_probe_that_starts_never_gets_an_elevate_command(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"restricted_sids": ["no_pong", "no_pong", "ok"]})
    code, out = run_main(sim, capsys)
    assert code == 0, out
    mode = modes_by_id(sim.report())["restricted_sids"]
    assert mode["diagnostic"] is True and mode["tries"][2]["完整性级别"].startswith("不降")
    assert mode["diagnostic_items"]["elevate"]["note"] == "没验 (诊断配法不发提权请求)"
    asked = [c.asked for c in sim.children if c.mode == "restricted_sids"]
    assert len(asked) == 3 and all("elevate" not in a for a in asked)
    assert sorted(p.name for p in sim.results.glob("elev_*")) == ["elev_control.txt"]       # 只有对照真试了


def test_ctrl_c_while_waiting_for_the_first_reply_still_ends_the_probe(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"restricted_downgrade": ["interrupt_on_ping"]})
    code, _ = run_main(sim, capsys)
    assert code == 1
    stuck = [c for c in sim.children if c.mode == "restricted_downgrade"]
    assert len(stuck) == 1 and stuck[0]._summary is not None          # 等回话时被打断, 这个探针也结束了
    report = sim.report()
    assert report["errors"] == ["被 Ctrl+C 打断"] and not (sim.iso / "work").exists()
    cleanup = report["cleanup"]
    assert cleanup["起过的探针个数"] == len(sim.children) == 3 and cleanup["全部已退出"] is True
    assert [s["mode"] for s in sim.calls["spawn"]] == ["control", "appcontainer", "restricted_downgrade"]   # 没有再往下试


def test_a_probe_that_will_not_exit_stops_the_ladder(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"restricted_downgrade": ["no_pong", "no_pong", "no_pong"]},
              not_exited=("restricted_downgrade",))
    code, out = run_main(sim, capsys)
    assert code == 1, out                      # 收尾核对到有进程没退出: 退出码 1, 结果里照实写
    down = modes_by_id(sim.report())["restricted_downgrade"]
    assert len(down["tries"]) == 1 and "没退出" in down["tries"][0] and "不再试后面的配法" in out
    assert [c["mode"] for c in sim.calls["spawn"]].count("restricted_downgrade") == 1
    assert sim.report()["cleanup"]["全部已退出"] is False


def test_stderr_of_an_inheriting_variant_is_never_recorded(monkeypatch, tmp_path, capsys):
    secret = "sk-STDERR-SECRET-9876"
    plan = {"appcontainer": ["error:203"] * 4 + ["no_pong"]}
    sim = Sim(monkeypatch, tmp_path, spawn_plan=plan, stderr=f"Traceback ... token={secret}")
    run_main(sim, capsys)
    text = (sim.results / "report.json").read_text(encoding="utf-8") + (sim.results / "report.md").read_text(encoding="utf-8")
    assert secret not in text
    rec = modes_by_id(sim.report())["appcontainer"]["tries"][4]
    assert rec["stderr"] == f"(这种配法不记原文, 共 {len(f'Traceback ... token={secret}')} 个字符)"


def test_stderr_of_a_variant_that_does_not_inherit_is_recorded_after_redaction(monkeypatch, tmp_path, capsys):
    sim = Sim(monkeypatch, tmp_path, spawn_plan={"restricted_downgrade": ["no_pong"]},
              stderr=f"boom 0xC0000135 {FAKE_NAME}")
    run_main(sim, capsys)
    text = modes_by_id(sim.report())["restricted_downgrade"]["tries"][0]["stderr"]
    assert "boom 0xC0000135" in text and FAKE_NAME not in text

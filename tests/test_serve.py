"""看守进程 (tools/serve.py): 用一个替身服务端 (几行的 HTTP 服务, 按参数崩、卡住、好好跑), 不起真的 8790.

- 崩了拉起来, 10 分钟里崩太多次就不拉了; 每次崩都有崩溃记录;
- 卡死也算崩; 只是慢 (/api/status 查不到, 但心跳里事件循环还在转; 起得慢但还在动) 不杀 (Supervisor.judge);
- 一句回话都收不到、可心跳说起好了: 是看守连本机端口这条路不通, 不杀、不退回合并 (2026-11-11 代理环境变量那次);
- 刚合进来的改动一上线就崩: 自动退回那次合并, 用旧代码起来, 记成踩过的坑 (下一次改桌宠的任务书里带上);
- 重启、停的请求; 运行指标; 端口被占着不起;
- 试跑: 真的 python -m wm 用替身引擎起一份.
"""
import importlib.util
import io
import json
import socket
import sys
import threading
import time
from pathlib import Path

from wm import workbench as W

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("serve", ROOT / "tools" / "serve.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

FAKE = r'''
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port, mode = int(sys.argv[1]), sys.argv[2]
T0 = time.time()
LOG = os.environ.get("FAKE_LOG")
if mode in ("slow", "stuck", "slowstart", "blind", "broken"):  # 用真的心跳 (wm/beat.py), 写快一点
    sys.path.insert(0, os.environ["FAKE_ROOT"])
    from wm import beat
    beat.BEAT_S = 0.1
    beat.phase(beat.READY if mode in ("blind", "broken") else "载入替身")  # blind / broken: 服务端自己说起好了
    beat.start()

    def spin():  # 替身的"事件循环": slow 一直在转; stuck 1.5 秒以后就不转了 (进程还在, 心跳线程照写)
        while mode != "stuck" or time.time() - T0 < 1.5:
            beat._state["loop"] = time.time()
            time.sleep(0.1)
    threading.Thread(target=spin, daemon=True).start()
if mode == "slowstart":  # 起得慢 (载入模型): 2.5 秒以后才开始听端口, 这期间心跳照写
    time.sleep(2.5)
if mode == "deadstart":  # 起不来, 也不动 (没心跳、不用 CPU)
    time.sleep(60)


def note(rec):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


note({"boot": {k: os.environ.get(k) for k in ("WM_BOOT_ID", "WM_BOOT_MODE", "WM_BOOT_NOTE", "WM_BOOT_LEARNED")},
      "has_key": bool(os.environ.get("WM_SUPERVISOR_KEY"))})
if mode == "file" and "BAD" in open(sys.argv[3], encoding="utf-8").read():  # 读项目里的文件: 改坏了就崩
    print("Traceback: wm/a.py 改坏了", flush=True)
    sys.exit(3)
if mode == "crash":
    threading.Timer(0.3, lambda: os._exit(3)).start()


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if mode == "hang" and time.time() - T0 > 0.8:
            time.sleep(30)
        if mode in ("slow", "stuck") and time.time() - T0 > 1.5:  # 机器忙: 每次都比看守等的久
            time.sleep(2)
        if mode == "blind":  # 起好了, 可看守从头到尾一句回话都收不到 (2026-11-11: 环境变量里的代理把本机地址劫走了)
            time.sleep(3)
        self.send_response(500 if mode == "broken" else 200)  # broken: 起好了, 但 /api/status 自己坏了
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):  # 看守重启前的通知 (wm/app.py 的 /api/supervisor)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        ok = self.headers.get("X-Supervisor-Key") == os.environ.get("WM_SUPERVISOR_KEY")
        note({"post": self.path, "key_ok": ok, "body": body})
        self.send_response(200 if ok else 403)
        self.end_headers()
        self.wfile.write(json.dumps({"ok": ok, "sessions": 1, "waited_s": 0.0}).encode())

    def log_message(self, *a):
        pass


print("已启动", flush=True)
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def supervisor(tmp: Path, mode: str, *extra, **kw):
    fake = tmp / "fake_server.py"
    fake.write_text(FAKE, encoding="utf-8")
    port = free_port()
    out = io.BytesIO()
    opts = dict(status_every=0.2, status_timeout=0.5, hung_s=1.0, hung_max_s=30.0, startup_s=5.0, startup_max_s=30.0,
                canary_s=1.0, metrics_every=0.2, max_restarts=3, restart_window=60.0)
    opts.update(kw)
    sv = S.Supervisor(port, home=tmp / "home" / "serve", logs=tmp / "logs", project=tmp,
                      cmd=[sys.executable, str(fake), str(port), mode, *extra], out=out, **opts)
    return sv, out


def run_bg(sv):
    box = {}
    th = threading.Thread(target=lambda: box.setdefault("why", sv.run()), daemon=True)
    th.start()
    return th, box


def until(cond, timeout=15.0):
    t0 = time.time()
    while not cond():
        assert time.time() - t0 < timeout, "等不到条件"
        time.sleep(0.05)


def test_crashes_are_restarted_then_given_up(tmp_path):
    sv, out = supervisor(tmp_path, "crash", max_restarts=2)
    assert sv.run() == "too_many_crashes"
    crashes = sorted((tmp_path / "home" / "serve").glob("crash-*.json"))
    assert len(crashes) == 3  # 拉起了两次, 第三次崩了就不拉了
    report = json.loads(crashes[0].read_text(encoding="utf-8"))
    assert report["exit_code"] == 3 and "退出了" in report["reason"] and report["reverted"] == []
    text = out.getvalue().decode("utf-8")
    assert "不再拉起" in text and "已启动" in text  # 服务端自己的输出也照常打出来
    assert not (tmp_path / "home" / "serve" / "serve.json").exists()


def test_hung_server_counts_as_a_crash(tmp_path):
    sv, _ = supervisor(tmp_path, "hang", max_restarts=0)
    assert sv.run() == "too_many_crashes"
    report = json.loads(next((tmp_path / "home" / "serve").glob("crash-*.json")).read_text(encoding="utf-8"))
    assert "卡住了" in report["reason"]


def test_new_merge_that_breaks_the_server_is_reverted(tmp_path):
    """合进来的改动一上线就崩: 退回、记成踩过的坑、用旧代码起来; 再跑过 canary_s 就没有待验证的了."""
    home, project = tmp_path / "home", tmp_path / "proj"
    for root in (project, home / "workspace" / "j1" / "base", home / "workspace" / "j1" / "repo"):
        (root / "wm").mkdir(parents=True)
        (root / "wm" / "a.py").write_text("OK\n", encoding="utf-8")
    jd = home / "workspace" / "j1"
    (jd / "repo" / "wm" / "a.py").write_text("BAD\n", encoding="utf-8")
    assert W.apply(jd, project, ignore_gate=True) == (["wm/a.py"], [])
    assert json.loads((home / "serve" / "canary.json").read_text(encoding="utf-8"))["jobs"][0]["job"] == "j1"
    sv, out = supervisor(tmp_path, "file", str(project / "wm" / "a.py"))
    th, box = run_bg(sv)
    until(lambda: sv.ok_once)
    until(lambda: not json.loads((home / "serve" / "canary.json").read_text(encoding="utf-8"))["jobs"])  # 过了 canary_s
    (home / "serve" / "stop").write_text("x", encoding="utf-8")
    th.join(15)
    assert box["why"] == "stop"
    assert (project / "wm" / "a.py").read_text(encoding="utf-8") == "OK\n"
    report = json.loads(next((home / "serve").glob("crash-*.json")).read_text(encoding="utf-8"))
    assert report["reverted"] == [{"job": "j1", "files": ["wm/a.py"], "conflicts": []}]
    pit = [json.loads(x) for x in (home / "pitfalls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert pit[0]["job"] == "j1" and pit[0]["files"] == ["wm/a.py"] and any("改坏了" in ln for ln in pit[0]["tail"])
    assert "wm/a.py" in W.pitfalls(home) and "改坏了" in W.pitfalls(home)  # 下一次改桌宠的任务书里带上
    assert "退回了 wm/a.py" in out.getvalue().decode("utf-8")


def test_restart_and_stop_requests_and_metrics(tmp_path):
    sv, _ = supervisor(tmp_path, "ok")
    th, box = run_bg(sv)
    until(lambda: sv.ok_once)
    first = sv.boot
    (tmp_path / "home" / "serve" / "restart").write_text("x", encoding="utf-8")
    until(lambda: sv.boot != first and sv.ok_once)
    state = json.loads((tmp_path / "home" / "serve" / "serve.json").read_text(encoding="utf-8"))
    assert state["boot"] == sv.boot and state["port"] == sv.port
    until(lambda: list((tmp_path / "logs").glob("metrics-*.jsonl")))
    (tmp_path / "home" / "serve" / "stop").write_text("x", encoding="utf-8")
    th.join(15)
    assert box["why"] == "stop" and sv.proc.poll() is not None
    m = json.loads(next((tmp_path / "logs").glob("metrics-*.jsonl")).read_text(encoding="utf-8").splitlines()[0])
    assert m["rss_mb"] > 0 and m["threads"] >= 1 and m["boot"] in (first, sv.boot)


def test_restart_tells_the_server_first_and_passes_the_boot_mode(tmp_path, monkeypatch):
    """照请求重启: 先带钥匙 POST /api/supervisor (服务端告诉桌宠"她去睡一会儿"), 再停; 新起的那个知道自己是"照请求重启的";
    刚合进来的改动一上线就崩、退回了: 再起的那个知道是"退回了", 带一句给人看的话 (桌宠醒来时说)."""
    log = tmp_path / "fake.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    sv, _ = supervisor(tmp_path, "ok")
    th, box = run_bg(sv)
    until(lambda: sv.ok_once)
    first = sv.boot
    (tmp_path / "home" / "serve" / "restart").write_text("x", encoding="utf-8")
    until(lambda: sv.boot != first and sv.ok_once)
    (tmp_path / "home" / "serve" / "stop").write_text("x", encoding="utf-8")
    th.join(15)
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    boots = [r["boot"] for r in rows if "boot" in r]
    posts = [r for r in rows if "post" in r]
    assert [b["WM_BOOT_MODE"] for b in boots] == ["normal", "restarted"] and all(r["has_key"] for r in rows if "boot" in r)
    assert boots[0]["WM_BOOT_ID"] == first and boots[1]["WM_BOOT_ID"] == sv.boot
    assert [(p["post"], p["key_ok"], p["body"]["action"]) for p in posts] == [("/api/supervisor", True, "restart"),
                                                                              ("/api/supervisor", True, "stop")]
    # 崩了、退回了: 下一次启动是 reverted, 带一句话
    sv2, _ = supervisor(tmp_path, "crash", max_restarts=1)
    sv2.next_mode = "normal"
    (tmp_path / "home" / "serve" / "canary.json").write_text(json.dumps({"jobs": [{"job": "j9", "dir": str(tmp_path / "nojob")}]}),
                                                               encoding="utf-8")
    assert sv2.run() == "too_many_crashes"
    modes = [json.loads(x)["boot"] for x in log.read_text(encoding="utf-8").splitlines() if '"boot"' in x][2:]
    assert modes[0]["WM_BOOT_LEARNED"] == "j9" and modes[1]["WM_BOOT_MODE"] == "reverted" and "j9" in modes[1]["WM_BOOT_NOTE"]


def test_judge_tells_slow_from_stuck(tmp_path):
    """卡没卡死看心跳, 不只看 /api/status (2026-10-27: 机器忙时服务端只是慢, 杀了重起要重新载入模型, 更慢, 来回崩了十几次)."""
    sv, _ = supervisor(tmp_path, "ok", hung_s=90.0, hung_max_s=600.0, startup_s=120.0, startup_max_s=600.0)
    t0 = 1000.0
    sv.started, sv.ok_once, sv.last_ok = t0, False, t0

    def beat(t, loop, phase="载入面部动画 (A2F)"):
        return {"boot": "b", "t": t, "loop": loop, "phase": phase}

    # 起的时候: startup_s 内照等; 之后心跳还在写 (A2F 载入 67 秒那种) 接着等, 到 startup_max_s 为止
    assert sv.judge(t0 + 60, None, False) == ("wait", "")
    what, why = sv.judge(t0 + 140, beat(t0 + 139, 0), False)
    assert what == "wait" and "载入面部动画" in why
    assert sv.judge(t0 + 140, None, True)[0] == "wait"  # 没有心跳 (老代码) 但在用 CPU: 在载入
    what, why = sv.judge(t0 + 140, beat(t0 + 20, 0), False)  # 心跳早停了, 也不用 CPU
    assert what == "kill" and "没在动" in why and "载入面部动画" in why
    assert sv.judge(t0 + 601, beat(t0 + 600, 0), True)[0] == "kill"  # 再慢也有个头
    # 起来以后
    sv.ok_once, sv.last_ok = True, t0 + 200
    assert sv.judge(t0 + 250, beat(t0 + 249, t0 + 248), False) == ("wait", "")  # 才 50 秒, 还没到 hung_s
    what, why = sv.judge(t0 + 300, beat(t0 + 299, t0 + 295), False)  # 19:28 那次: /api/status 回不来, 循环还在转
    assert what == "wait" and "忙" in why
    what, why = sv.judge(t0 + 300, beat(t0 + 299, t0 + 150), False)  # 循环 150 秒没转, 心跳线程照写: 卡死了
    assert what == "kill" and "事件循环" in why
    assert sv.judge(t0 + 300, beat(t0 + 150, t0 + 150), False)[0] == "wait"  # 整个进程都没动静 (机器在换页): 先等
    assert sv.judge(t0 + 801, beat(t0 + 799, t0 + 799), False)[0] == "kill"  # 查不到够 hung_max_s: 不管心跳
    what, why = sv.judge(t0 + 300, None, True)  # 没有心跳 (老代码): 照老办法, 连着 hung_s 查不到就杀
    assert what == "kill" and "卡住了" in why


def test_slow_but_alive_server_is_left_alone(tmp_path, monkeypatch):
    """/api/status 每次都比看守等的久 (机器被占满), 但心跳里事件循环还在转: 过了好几个 hung_s 也不杀, 说一声"忙"."""
    monkeypatch.setenv("FAKE_ROOT", str(ROOT))
    sv, out = supervisor(tmp_path, "slow")
    th, box = run_bg(sv)
    until(lambda: sv.ok_once)
    first = sv.boot
    until(lambda: "服务端忙" in out.getvalue().decode("utf-8"))
    time.sleep(3)  # hung_s 的三倍
    assert sv.boot == first and not list((tmp_path / "home" / "serve").glob("crash-*.json"))
    (tmp_path / "home" / "serve" / "stop").write_text("x", encoding="utf-8")
    th.join(20)
    assert box["why"] == "stop"


def test_stuck_event_loop_is_killed(tmp_path, monkeypatch):
    """事件循环不转了而进程还在 (心跳线程照写): 是真卡死, 杀; 崩溃记录里带着最后的心跳和整机负载."""
    monkeypatch.setenv("FAKE_ROOT", str(ROOT))
    sv, _ = supervisor(tmp_path, "stuck", max_restarts=0)
    assert sv.run() == "too_many_crashes"
    report = json.loads(next((tmp_path / "home" / "serve").glob("crash-*.json")).read_text(encoding="utf-8"))
    assert "事件循环" in report["reason"] and report["beat"]["phase"] == "载入替身" and "CPU" in report["machine"]


def test_slow_start_is_waited_for_but_a_dead_start_is_not(tmp_path, monkeypatch):
    """起得慢 (载入模型) 但心跳还在写: 过了 startup_s 接着等, 起来了就好. 起不来、也不动的 (没心跳、不用 CPU) 照样杀."""
    monkeypatch.setenv("FAKE_ROOT", str(ROOT))
    sv, out = supervisor(tmp_path, "slowstart", startup_s=1.0)
    th, box = run_bg(sv)
    until(lambda: sv.ok_once)
    (tmp_path / "home" / "serve" / "stop").write_text("x", encoding="utf-8")
    th.join(20)
    text = out.getvalue().decode("utf-8")
    assert box["why"] == "stop" and "起得慢" in text and "载入替身" in text
    assert not list((tmp_path / "home" / "serve").glob("crash-*.json"))
    dead = tmp_path / "dead"
    dead.mkdir()
    sv2, _ = supervisor(dead, "deadstart", startup_s=1.0, max_restarts=0)
    assert sv2.run() == "too_many_crashes"
    report = json.loads(next((dead / "home" / "serve").glob("crash-*.json")).read_text(encoding="utf-8"))
    assert "没在动" in report["reason"] and report["uptime_s"] < 10


def test_judge_does_not_shoot_a_server_that_says_it_is_up(tmp_path):
    """心跳说起好了、事件循环还在转, 看守却一句回话都收不到: 毛病在看守连本机端口这条路上, 不在服务端 — 不杀.
    (2026-11-11: 环境变量里的代理把本机地址劫走, 每 607 秒杀一次好好的服务端.) 回了话但不是 200 的、心跳或事件循环停了的, 照老规矩."""
    sv, _ = supervisor(tmp_path, "ok", hung_s=90.0, hung_max_s=600.0, startup_s=120.0, startup_max_s=600.0)
    t0 = 1000.0
    sv.started, sv.ok_once, sv.last_ok = t0, False, t0

    def up(t, loop=None):
        return {"boot": "b", "t": t, "loop": t if loop is None else loop, "phase": S.B.READY}

    assert sv.judge(t0 + 60, up(t0 + 59), False) == ("wait", "")  # startup_s 内照等 (刚起好、端口还没开的那一下不算)
    what, why = sv.judge(t0 + 607, up(t0 + 606), False)  # 那天的情形: 607 秒, 心跳说起好了
    assert what == "blind" and "心跳说起好了" in why and "不退回" in why
    assert sv.judge(t0 + 5000, up(t0 + 4999), False)[0] == "blind"  # 多久都不杀
    assert sv.judge(t0 + 607, up(t0 + 606), False, answered=True)[0] == "kill"  # 回了话但不是 200: 服务端自己坏了
    assert sv.judge(t0 + 607, up(t0 + 606, loop=t0 + 400), False)[0] == "kill"  # 事件循环早不转了
    assert sv.judge(t0 + 607, up(t0 + 300), False)[0] == "kill"  # 心跳早停了
    sv.ok_once, sv.last_ok = True, t0 + 200  # 查到过以后又查不到: 不走这一条, 还是老规矩
    assert sv.judge(t0 + 801, up(t0 + 800), False)[0] == "kill"


def test_server_that_is_up_but_unreachable_is_left_alone(tmp_path, monkeypatch):
    """替身服务端心跳说起好了、事件循环在转, 但看守一句回话都收不到: 过了 startup_max_s 也不杀, 说明白是这条路不通;
    待验证的合并留着, 不退回、也不算验证过. 同样的心跳、/api/status 回 500 的, 照老规矩算起不来."""
    monkeypatch.setenv("FAKE_ROOT", str(ROOT))
    sv, out = supervisor(tmp_path, "blind", startup_s=1.0, startup_max_s=2.0)
    home = tmp_path / "home" / "serve"
    home.mkdir(parents=True)
    (home / "canary.json").write_text(json.dumps({"jobs": [{"job": "j7", "dir": str(tmp_path / "nojob")}]}), encoding="utf-8")
    th, box = run_bg(sv)
    until(lambda: sv.beat_up)
    first = sv.boot
    time.sleep(3)  # 早过了 startup_max_s
    assert sv.boot == first and not sv.ok_once and not list(home.glob("crash-*.json"))
    assert [j["job"] for j in json.loads((home / "canary.json").read_text(encoding="utf-8"))["jobs"]] == ["j7"]
    (home / "stop").write_text("x", encoding="utf-8")
    th.join(20)
    text = out.getvalue().decode("utf-8")
    assert box["why"] == "stop" and "心跳说起好了" in text and "退回了" not in text
    broken = tmp_path / "broken"
    broken.mkdir()
    sv2, _ = supervisor(broken, "broken", startup_s=1.0, startup_max_s=2.0, max_restarts=0)
    assert sv2.run() == "too_many_crashes"
    report = json.loads(next((broken / "home" / "serve").glob("crash-*.json")).read_text(encoding="utf-8"))
    assert "还没起来" in report["reason"] and S.B.READY in report["reason"]


def test_merge_is_not_reverted_when_the_heartbeat_said_up(tmp_path):
    """看守查不到、可心跳说起好过的那一次, 之后服务端退出了: 不算"刚合进来的改动一上线就起不来", 不退回、不记成坑,
    留给下一次启动验证 (2026-11-11 误退回了两次合并). 没有这层证据的照旧退回."""
    home, project = tmp_path / "home", tmp_path / "proj"
    for root in (project, home / "workspace" / "j1" / "base", home / "workspace" / "j1" / "repo"):
        (root / "wm").mkdir(parents=True)
        (root / "wm" / "a.py").write_text("OLD\n", encoding="utf-8")
    jd = home / "workspace" / "j1"
    (jd / "repo" / "wm" / "a.py").write_text("NEW\n", encoding="utf-8")
    assert W.apply(jd, project, ignore_gate=True) == (["wm/a.py"], [])
    canary = home / "serve" / "canary.json"
    pending = json.loads(canary.read_text(encoding="utf-8"))
    pending["jobs"][0]["boot"] = "b1"
    canary.write_text(json.dumps(pending), encoding="utf-8")
    sv, _ = supervisor(tmp_path, "ok")
    sv.boot, sv.started, sv.ok_once, sv.beat_up = "b1", time.time() - 100, False, True
    report = json.loads(sv._crashed("退出了 (退出码 3)", 3).read_text(encoding="utf-8"))
    assert report["reverted"] == [] and (project / "wm" / "a.py").read_text(encoding="utf-8") == "NEW\n"
    assert [j["job"] for j in json.loads(canary.read_text(encoding="utf-8"))["jobs"]] == ["j1"]
    assert sv.next_mode == "crashed" and not (home / "pitfalls.jsonl").exists()
    sv.beat_up = False  # 没查到过, 心跳也没说起好: 照旧退回
    report = json.loads(sv._crashed("607 秒还没起来", None).read_text(encoding="utf-8"))
    assert report["reverted"] == [{"job": "j1", "files": ["wm/a.py"], "conflicts": []}]
    assert (project / "wm" / "a.py").read_text(encoding="utf-8") == "OLD\n" and sv.next_mode == "reverted"


def test_real_server_reports_its_heartbeat(tmp_path):
    """真的 python -m wm (替身引擎) 在看守下起: 心跳从导入代码起就写, 起好了 phase 是"起好了", 事件循环在转."""
    port = free_port()
    sv = S.Supervisor(port, home=tmp_path / "serve", logs=tmp_path / "logs", project=ROOT, out=io.BytesIO(),
                      cmd=[sys.executable, "-m", "wm", "--port", str(port), *W.TRIAL_ARGS,
                           "--set", f"trace.log_dir={(tmp_path / 'trace').as_posix()}",
                           "--set", f"agent.home={(tmp_path / 'agent').as_posix()}"],  # 不换的话子进程用真的 ~/.worldmodel/agent
                      status_every=0.5, status_timeout=3.0, canary_s=1.0, metrics_every=60.0)
    th, box = run_bg(sv)
    try:
        until(lambda: sv.ok_once, timeout=150)
        until(lambda: (sv._beat() or {}).get("phase") == "起好了", timeout=10)
        b = sv._beat()
        assert b["boot"] == sv.boot and b["pid"] == sv.proc.pid
        assert time.time() - b["t"] < 6 and time.time() - b["loop"] < 6
    finally:
        (tmp_path / "serve" / "stop").write_text("x", encoding="utf-8")
        th.join(40)
    assert box.get("why") == "stop"


def test_does_not_start_when_the_port_is_taken(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        sv, out = supervisor(tmp_path, "ok")
        sv.port = s.getsockname()[1]
        assert sv.run() == "port_busy" and sv.proc is None
    assert "已经有程序在听" in out.getvalue().decode("utf-8")


def test_trial_boots_the_real_server_with_stand_in_engines(tmp_path):
    ok, text = S.trial(timeout=90)  # 挑一个空着的端口
    assert ok is True, text
    assert W.trial(tmp_path)[0] is None  # 没有服务端入口的 (测试里的替身项目) 不试跑

"""操控电脑 (Windows-MCP) 的端到端自检 (computertest.ps1 调用): 假云端 + 8794 mock 服务 + 开发版桌宠 + 两个测试窗口.

    python computer_test.py --prepare <设置目录> --cloud-port 8797 [--real <windows-mcp.exe>]   先写好测试用的设置 (不碰用户真正的设置)
    python computer_test.py --run --settings-dir <目录> --pid <桌宠进程> --cloud-log <假云端日志> --api-port 8795 --out <目录> [--real]

默认用 Windows-MCP 的替身 (mcp_computer_stub.py, 工具和参数一样, 不真点); --real 换成真的 Windows-MCP, 会真的动鼠标、
在测试窗口的输入框里打字 (只测几项: 打字、守门、截图不发云端、元素树里换掉聊天窗口的标题, 不测停和上限).
桌宠页面带 toolanswers: 确认卡片依次自动回答. 对话经本机接口的 wm-pet 发 (页面静音).
测试窗口 (guard_target.cpp): 普通的"守门测试窗口"和冒充聊天软件的 Weixin.exe "微信测试-张三", 控件坐标从 target.json / chat.json 读.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from api_test import Mcp, RESULTS, check, close_pet  # noqa: E402

import openai  # noqa: E402

STUB = os.path.join(HERE, "mcp_computer_stub.py")
TOOLS_REAL = "ControlStatus,DisplayInventory,Wait,Screenshot,Snapshot,WaitFor,Click,Type,Scroll,Move,Shortcut,App"
# 替身列出 21 个; 锁定的 7 个不交给模型. 另有 load_skill: 勾了操控电脑的服务器, 自动带上随附的 computer-use
DECLARED_STUB = ["App", "Click", "ControlStatus", "DisplayInventory", "Move", "MultiEdit", "MultiSelect", "Screenshot", "Scroll",
                 "Shortcut", "Snapshot", "Type", "Wait", "WaitFor", "load_skill"]
LOCKED = ["Clipboard", "FileSystem", "Notification", "PowerShell", "Process", "Registry", "Scrape"]
# 确认卡片的自动回答 (按弹出的顺序), 和下面 run() 里的步骤对应
ANSWERS_STUB = "once,deny,turn,turn,turn,once,turn,turn"
ANSWERS_REAL = "once,once,once"


def stub_log(settings_dir: str) -> str:
    return os.path.join(settings_dir, "stub-log.jsonl")


def prepare(settings_dir: str, cloud_port: int, real: str | None, power: bool = False) -> None:
    if real:
        server = {"command": real, "args": ["serve", "--tools", TOOLS_REAL], "timeout_s": 60}
    else:
        server = {"command": sys.executable, "args": ["-I", STUB, "--log", stub_log(settings_dir)], "timeout_s": 15}
    settings = {
        "version": 1, "active": "p-pc",
        "personas": [
            {"id": "default", "name": "默认", "prompt": "", "provider": "local", "model": "", "mcp": [], "skills": []},
            {"id": "p-pc", "name": "操控电脑自检", "prompt": "", "provider": "custom", "model": "fake-chat", "mcp": ["s-pc"], "skills": [], "builtins": []},
        ],
        "providers": {"custom": {"base_url": f"http://127.0.0.1:{cloud_port}/v1", "model": ""}},
        # 测试时不开"你一动鼠标键盘她就停": 测试中有人碰鼠标会把测试停掉 (钩子只认真人输入, 自动测不了)
        "mcp": [{"id": "s-pc", "name": "computer", "kind": "windows-mcp", "transport": "stdio", "cwd": "", "url": "", "enabled": True,
                 "tools": {}, "stop_on_input": False, **server}],
        # power: 高风险档 (执行命令、文件、注册表…) 交不交给模型. 老的自检关着它 (验证"锁定的 7 个不在里面"), qqtasktest.ps1 打开
        "guard": {"processes": [], "words": [], "power": power},
    }
    with open(os.path.join(settings_dir, "pet-settings.json"), "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False)


def read_json(path: str, wait: float = 15) -> dict:
    for _ in range(int(wait * 10)):
        if os.path.exists(path):
            try:
                return json.load(open(path, encoding="utf-8"))
            except ValueError:
                pass
        time.sleep(0.1)
    raise SystemExit(f"没等到 {path}")


def lines(path: str) -> list[dict]:
    return [json.loads(x) for x in open(path, encoding="utf-8")] if os.path.exists(path) else []


def stub_processes(pattern: str) -> int:
    cmd = (f"(Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -match '{pattern}' -and $_.Name -ne 'powershell.exe' }}"
           " | Measure-Object).Count")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def press_stop_hotkey(delay: float) -> None:
    """隔 delay 秒按一下 Ctrl+Alt+Shift+Backspace (注入的按键: 全局快捷键照样触发, 真人输入检测不算)."""
    time.sleep(delay)
    keys = [0x11, 0x12, 0x10, 0x08]  # Ctrl Alt Shift Backspace
    for k in keys:
        ctypes.windll.user32.keybd_event(k, 0, 0, 0)
    for k in reversed(keys):
        ctypes.windll.user32.keybd_event(k, 0, 2, 0)


def edit_text(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.SendMessageW(hwnd, 0x000D, 512, buf)  # WM_GETTEXT
    return buf.value


def run(a) -> int:
    token = open(os.path.join(os.environ["APPDATA"], "WorldModel", "pet-api-token.txt"), encoding="utf-8").read().strip()
    base = f"http://127.0.0.1:{a.api_port}"
    mcp = Mcp(base, token)
    target = read_json(os.path.join(a.out, "target.json"))
    chat = read_json(os.path.join(a.out, "chat.json"))
    page = {}
    want = 13 if a.real else len(DECLARED_STUB)  # 真的: TOOLS_REAL 的 12 个 + load_skill
    for _ in range(150):  # 等页面就绪、预设生效、工具表交给服务端 (真的 Windows-MCP 起一次要 20 秒上下)
        try:
            _, _, st = mcp.tool("pet_state")
            page = (st.get("structuredContent") or {}).get("page") or {}
            if page.get("connected") and (page.get("tools") or {}).get("accepted") and (page.get("profile_status") or {}).get("ok"):
                break
        except Exception:
            pass
        time.sleep(1)
    ps, tl = page.get("profile_status") or {}, page.get("tools") or {}
    check("预设生效 (假云端) 并交了工具表", ps.get("ok") and tl.get("accepted") == want, profile=ps, tools=tl)
    client = openai.OpenAI(base_url=f"{base}/v1", api_key=token, timeout=180, max_retries=0)
    audit_path = os.path.join(a.settings_dir, "pet-tool-audit.jsonl")

    def say(text: str) -> tuple[str, list[dict], float]:
        before = len(lines(audit_path))
        t0 = time.time()
        c = client.chat.completions.create(model="wm-pet", messages=[{"role": "user", "content": text}])
        time.sleep(0.3)
        return c.choices[0].message.content or "", lines(audit_path)[before:], time.time() - t0

    def pc(*steps: str) -> str:
        return "电脑:" + " | ".join(steps)

    def loc(p) -> str:
        return json.dumps({"loc": p})

    reqs = lines(a.cloud_log)
    if a.real:
        return run_real(a, say, pc, target, chat)

    # --- 工具表 ------------------------------------------------------------------------------
    reply, au, _ = say(pc("ControlStatus {}"))
    reqs = lines(a.cloud_log)
    declared = sorted(t.split("__")[-1] for t in next((r["tools"] for r in reqs if r["tools"]), []))
    check("交给模型的工具: 锁定的 7 个 (PowerShell、FileSystem、Registry…) 不在里面", declared == DECLARED_STUB, declared=declared)
    params = next((r["params"] for r in reqs if r.get("params")), {})
    click = params.get("mcp__computer__Click", {}).get("properties", {})
    app = params.get("mcp__computer__App", {}).get("properties", {})
    check("参数结构: 去掉了按编号点 (label) 和 App 按路径启动", "loc" in click and "label" not in click and "executable" not in app
          and "launch_executable" not in (app.get("mode") or {}).get("enum", []), click=list(click), app=list(app), mode=app.get("mode"))
    check("只读 (ControlStatus): 自动调, 不问", au and au[-1]["how"] == "auto" and au[-1].get("tier") == "read" and au[-1]["ok"], audit=au)
    skill = params.get("load_skill", {}).get("properties", {}).get("name", {}).get("enum")
    say(pc('load_skill {"name": "computer-use"}'))
    text = (lines(a.cloud_log)[-1].get("tool_text") or "")
    check("随附 skill: 预设没勾也自动带上 computer-use, 读得到操作要领", skill == ["computer-use"] and "use_dom=true" in text
          and "mode=\"switch\"" in text, skill=skill, text=text[:200])

    # --- 读屏: 截图不发云端, 聊天窗口的标题换掉 --------------------------------------------------
    reply, au, _ = say(pc("Screenshot {}"))
    text = (lines(a.cloud_log)[-1].get("tool_text") or "")
    check("读屏 (Screenshot): 问了, 允许一次", au and au[-1]["how"] == "once" and au[-1].get("tier") == "screen" and au[-1]["ok"], audit=au)
    check("截图没有发给模型 (换成一句只留在本机)", "截图只留在本机" in text and "iVBOR" not in text, text=text[:200])
    check("聊天软件的窗口标题换成了占位", "张三" not in text and "她不看的窗口" in text and target["title"] in text, text=text[:400])

    # --- 动手: 拒绝、这一轮都允许 --------------------------------------------------------------
    ok_btn, edit = loc(target["ok"]), json.dumps({"loc": target["edit"], "text": "你好"}, ensure_ascii=False)
    reply, au, _ = say(pc(f"Click {ok_btn}"))
    check("动手 (Click): 问了, 拒绝", au and au[-1]["how"] == "deny" and "拒绝" in reply, reply=reply[:80], audit=au)
    calls_before = len([e for e in lines(stub_log(a.settings_dir)) if e["event"] == "call"])
    reply, au, _ = say(pc(f"Click {ok_btn}", f"Type {edit}", f"Click {ok_btn}"))
    calls = [e for e in lines(stub_log(a.settings_dir)) if e["event"] == "call"][calls_before:]
    check("这一轮都允许: 只问一次, 三步都做了", [x["how"] for x in au] == ["turn", "turn", "turn"] and all(x["ok"] for x in au)
          and [c["name"] for c in calls] == ["Click", "Type", "Click"], hows=[x["how"] for x in au], calls=[c["name"] for c in calls])

    # --- 守门: 不问, 直接拦 ----------------------------------------------------------------------
    guards = [
        ("发送按钮", f"Click {loc(target['send'])}", "发消息"),
        ("密码框打字", "Type " + json.dumps({"loc": target["password"], "text": "123"}), "密码"),
        ("聊天软件的窗口", f"Click {loc(chat['ok'])}", "聊天"),
        ("按编号点", 'Click {"label": 3}', "坐标"),
        ("运行框 (Win+R)", 'Shortcut {"shortcut": "Win+R"}', "运行框"),
        ("打开命令提示符", 'App {"mode": "launch", "name": "命令提示符"}', "命令行"),
        ("按路径启动", 'App {"mode": "launch_executable", "executable": "cmd.exe"}', "执行命令"),
    ]
    for name, step, word in guards:
        calls_before = len([e for e in lines(stub_log(a.settings_dir)) if e["event"] == "call"])
        reply, au, _ = say(pc(step))
        reached = len([e for e in lines(stub_log(a.settings_dir)) if e["event"] == "call"]) - calls_before
        check(f"守门: {name} (不问, 没到服务器)", au and au[-1]["how"] == "guard" and word in (au[-1].get("error") or "")
              and "要用户自己来" in reply and reached == 0, reply=reply[:90], error=au[-1].get("error") if au else None, reached=reached)
    reply, au, _ = say(pc('PowerShell {"command": "echo hi"}'))
    check("锁定的工具模型那边根本没有 (PowerShell)", "没有这个工具" in reply and not au, reply=reply[:60])

    # --- 停整轮: 对话栏的停、全局快捷键 ----------------------------------------------------------
    for how, step in (("对话栏的停", 'Screenshot {"region": "slow", "tag": "stop"}'), ("快捷键 Ctrl+Alt+Shift+Backspace", 'Screenshot {"region": "slow"}')):
        calls_before = len([e for e in lines(stub_log(a.settings_dir)) if e["event"] == "call"])
        if "快捷键" in how:
            # 快捷键没注册上的话, 注入的 Backspace 会落到用户正在用的窗口里: 先看宿主日志, 没注册上就不按
            with open(os.path.join(os.path.dirname(a.settings_dir), "..", "dev", "wmpet.log"), "rb") as f:
                f.seek(a.log_from)
                if "Backspace 注册好了" not in f.read().decode("utf-8", "replace"):
                    check(f"停整轮 ({how})", False, error="快捷键没注册上, 没按")
                    continue
            threading.Thread(target=press_stop_hotkey, args=(4.0,), daemon=True).start()
        reply, au, secs = say(pc(step, f"Click {ok_btn}"))
        time.sleep(1.0)
        events = lines(stub_log(a.settings_dir))
        calls = [e for e in events if e["event"] == "call"][calls_before:]
        shot = next((x for x in au if x["raw"] == "Screenshot"), {})
        cancelled = any(e["event"] == "cancelled" for e in events) and any(e["event"] == "dropped" for e in events)
        check(f"停整轮 ({how}): 在调的停掉了, 这一轮后面的没再调", shot.get("how") == "turn" and not shot.get("ok") and "停" in (shot.get("error") or "")
              and [c["name"] for c in calls] == ["Screenshot"] and cancelled and secs < 8, error=shot.get("error"),
              calls=[c["name"] for c in calls], seconds=round(secs, 1), reply=reply[:60])
    reply, au, _ = say(pc(f"Click {ok_btn}"))
    check("停过之后新的一轮照常 (又问了一次, 允许一次)", au and au[-1]["how"] == "once" and au[-1]["ok"], audit=au)

    # --- 一轮的上限 ----------------------------------------------------------------------------
    reply, au, _ = say(pc("Screenshot {} x31"))
    hows = [x["how"] for x in au]
    check("一轮最多 30 次: 第 31 次拒绝", hows[:30] == ["turn"] * 30 and hows[30:] == ["halted"] and "上限" in (au[-1].get("error") or ""),
          count=len(au), last=au[-1] if au else None)

    # --- QQ 机器人的控制台: 开着时不读屏、不点 (放在最后: 开着它别的读屏都会被拦) ----------------------
    bot_json = os.path.join(a.out, "bot.json")
    if os.path.exists(bot_json):
        os.remove(bot_json)
    bot = subprocess.Popen([os.path.join(a.out, "guard_target.exe"), "SnowLuma 控制台 - 测试", bot_json, "1040", "120"])
    try:
        bot_win = read_json(bot_json)
        for name, step, word in (("开着时不读屏", "Screenshot {}", "控制台"), ("不点它的窗口", f"Click {loc(bot_win['ok'])}", "机器人")):
            reply, au, _ = say(pc(step))
            check(f"守门: QQ 机器人的控制台 (SnowLuma) {name}", au and au[-1]["how"] == "guard" and word in (au[-1].get("error") or ""),
                  reply=reply[:90], error=au[-1].get("error") if au else None)
    finally:
        bot.kill()
    time.sleep(0.5)
    reply, au, _ = say(pc("Screenshot {}"))
    check("控制台关了以后又能读屏 (问了, 这一轮都允许)", au and au[-1]["how"] == "turn" and au[-1]["ok"], audit=au)

    with open(os.path.join(os.path.dirname(a.settings_dir), "..", "dev", "wmpet.log"), "rb") as f:  # 只看这次测试写的那段
        f.seek(a.log_from)
        log = f.read().decode("utf-8", "replace")
    check("宿主日志: 快捷键注册好了、两次一键停", "Ctrl+Alt+Shift+Backspace 注册好了" in log and log.count("一键停 (") >= 2,
          hotkey="注册好了" in log, stops=log.count("一键停 ("))
    au = lines(audit_path)
    check("审计日志: 每次一行, 带档位", all("tier" in x for x in au if x["server"] == "computer"), count=len(au))
    close_pet(a.pid)
    for _ in range(20):
        if stub_processes("mcp_computer_stub") == 0:
            break
        time.sleep(0.5)
    check("桌宠退出后替身进程一起结束 (作业对象)", stub_processes("mcp_computer_stub") == 0)
    return finish()


def run_real(a, say, pc, target, chat) -> int:
    """真的 Windows-MCP: 在测试窗口的输入框里打一行字; 守门拦发送按钮; 截图不发云端; 元素树里换掉聊天窗口的标题."""
    text = "你好, 我是小流"
    reply, au, secs = say(pc("Type " + json.dumps({"loc": target["edit"], "text": text, "clear": True}, ensure_ascii=False)))
    got = edit_text(target["edit_hwnd"])
    check("真的 Windows-MCP: 在测试窗口里打了一行字", au and au[-1]["ok"] and au[-1]["how"] == "once" and got == text, got=got,
          seconds=round(secs, 1), audit=au)
    reply, au, _ = say(pc("Click " + json.dumps({"loc": target["send"]})))
    check("真的: 守门拦下发送按钮 (没问、没点)", au and au[-1]["how"] == "guard" and "要用户自己来" in reply, reply=reply[:80])
    reply, au, _ = say(pc("Screenshot {}"))
    t1 = lines(a.cloud_log)[-1].get("tool_text") or ""
    check("真的: 截图没有发给模型", au and au[-1]["ok"] and "截图只留在本机" in t1 and "iVBOR" not in t1 and "base64" not in t1.lower(),
          text=t1[:300])
    r = target["rect"]
    reply, au, _ = say(pc("Snapshot " + json.dumps({"region": [r[0], r[1], r[2], r[3]], "use_vision": False})))
    t2 = lines(a.cloud_log)[-1].get("tool_text") or ""
    check("真的: 元素树里有测试窗口的按钮, 没有聊天窗口的标题", au and au[-1]["ok"] and "好的" in t2 and "张三" not in t2, text=t2[:600])
    pid = None
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'windows-mcp' } | "
                          "Select-Object -First 1).ProcessId"], capture_output=True, text=True).stdout.strip()
    pid = int(out) if out.isdigit() else None
    remote = []
    if pid:
        cmd = (f"Get-NetTCPConnection -OwningProcess {pid} -ErrorAction SilentlyContinue | Where-Object {{ $_.RemoteAddress -notin "
               "@('127.0.0.1','::1','0.0.0.0','::') }} | ForEach-Object { \"$($_.RemoteAddress):$($_.RemotePort)\" }")
        remote = [x for x in subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True).stdout.split() if x]
    check("真的: Windows-MCP 进程没有连外网 (统计关掉了)", pid is not None and not remote, pid=pid, remote=remote)
    close_pet(a.pid)
    for _ in range(30):
        if stub_processes("windows-mcp") == 0:
            break
        time.sleep(0.5)
    check("真的: 桌宠退出后 Windows-MCP 进程一起结束", stub_processes("windows-mcp") == 0)
    return finish()


def finish() -> int:
    failed = [x["name"] for x in RESULTS if not x["ok"]]
    print(json.dumps({"total": len(RESULTS), "failed": failed}, ensure_ascii=False))
    return 1 if failed else 0


def main() -> int:
    # 控制台是 GBK 时, 窗口标题里的怪字符 (比如 B 站视频标题里的盲文点) 打印不出来会让测试自己崩掉: 换成问号
    sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare")
    ap.add_argument("--cloud-port", type=int, default=8797)
    ap.add_argument("--real", nargs="?", const="1", default=None)
    ap.add_argument("--power", action="store_true")  # --prepare 时: 打开高风险档
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--settings-dir")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--cloud-log")
    ap.add_argument("--api-port", type=int, default=8795)
    ap.add_argument("--out")
    ap.add_argument("--log-from", type=int, default=0)  # 宿主日志 (build\dev\wmpet.log) 这次测试开始时的长度
    a = ap.parse_args()
    if a.prepare:
        prepare(a.prepare, a.cloud_port, a.real, a.power)
        return 0
    return run(a)


if __name__ == "__main__":
    sys.exit(main())

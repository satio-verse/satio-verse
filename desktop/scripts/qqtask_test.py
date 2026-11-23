"""QQ 任务单 (主人在 QQ 上让她操作电脑) 在宿主和页面这一段的自检 (qqtasktest.ps1 调用).

    python qqtask_test.py --query <owner|self|shot> --out <目录>          打印这一种的页面查询串 (任务单由页面的 qqtask= 参数自己发)
    python qqtask_test.py --run <owner|self|shot> --settings-dir <目录> --pid <桌宠进程> --out <目录> --log-from <宿主日志长度>

服务端那一半 (命令分类、任务单、结果和截图发回 QQ) 在 tests/test_workbench.py; 这里只测宿主和页面:
- owner: 主人亲口说的事. 读屏、动手都算同意过了 (审计 how=turn, 不弹确认), 守门照旧 (发送按钮、聊天软件的窗口还是拦),
  测试窗口里有冒充聊天软件的窗口开着, 所以最后那张截图不留;
- self: 她自己想看的. 只放行读屏, 动手还是问 (页面自动答"拒绝");
- shot: 把冒充聊天软件的窗口关掉以后再来一次: 屏幕上要是还开着真的聊天软件、命令行就不留截图, 没有才留, 两种都算对, 只核对日志;
- shotany: 带 --task-shot-any 再来一次 (跳过上面那道闸门): 核对截图真的暂存下来、页面压成 JPEG、回报带着截图.
默认用 Windows-MCP 的替身 (不真点).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from api_test import check, close_pet  # noqa: E402
from computer_test import finish, lines, read_json, stub_log, stub_processes  # noqa: E402


def steps_of(mode: str, target: dict, chat: dict) -> list[str]:
    ok_btn = json.dumps({"loc": target["ok"]})
    edit = json.dumps({"loc": target["edit"], "text": "你好"}, ensure_ascii=False)
    if mode == "owner":
        return ["Screenshot {}", f"Click {ok_btn}", f"Type {edit}", "Click " + json.dumps({"loc": target["send"]}),
                "Click " + json.dumps({"loc": chat["ok"]})]
    # 每组只放 4 步: 假云端把结果拼成一句回话, 太长的话模拟语音要念很久, 这一轮迟迟不结束
    if mode == "power":
        return ['PowerShell {"command": "Get-Date"}',
                "PowerShell " + json.dumps({"command": "Remove-Item C:\\temp\\x -Recurse"}),
                'Clipboard {"mode": "get"}',
                "FileSystem " + json.dumps({"mode": "delete", "path": "C:\\temp\\x"})]
    if mode == "power2":
        return ["Registry " + json.dumps({"mode": "set", "path": "HKCU:\\Software\\x", "name": "a", "value": "1"}),
                "Process " + json.dumps({"mode": "kill", "name": "explorer"}),
                'Scrape {"url": "http://127.0.0.1:8790/"}',
                "FileSystem " + json.dumps({"mode": "read", "path": "C:\\Windows\\win.ini"})]
    if mode == "self":
        return ["Screenshot {}", f"Click {ok_btn}"]
    return ["Screenshot {}"]


def query_for(mode: str, out: str) -> str:
    target, chat = read_json(os.path.join(out, "target.json")), read_json(os.path.join(out, "chat.json"))
    by = "self" if mode == "self" else "owner"
    prompt = "电脑:" + " | ".join(steps_of(mode, target, chat))
    # toolanswers: 万一弹了确认框就自动拒绝 (owner 不该弹; self 的动手要弹)
    answers = "once,deny" if mode == "power" else "deny,deny"  # power: 第 1 个卡片点允许一次, 第 2 个拒绝
    return f"mute=1&toolanswers={answers}&asktimeout=4&qqtask={quote(by + '|' + prompt, safe='')}"


def host_log(a) -> str:
    with open(os.path.join(os.path.dirname(a.settings_dir), "..", "dev", "wmpet.log"), "rb") as f:
        f.seek(a.log_from)
        return f.read().decode("utf-8", "replace")


def run(a) -> int:
    mode = a.run
    audit_path = os.path.join(a.settings_dir, "pet-tool-audit.jsonl")
    log = ""
    for _ in range(150):  # 工具表到位 (替身起来) 后页面发任务单, 做完宿主日志里有"结束"
        log = host_log(a)
        if "渠道任务单 selftest 结束" in log and "任务单结束" in log:
            break
        time.sleep(0.5)
    time.sleep(0.5)
    log = host_log(a)
    au = [x for x in lines(audit_path) if x.get("server") == "computer"]
    calls = [e["name"] for e in lines(stub_log(a.settings_dir)) if e["event"] == "call"]
    hows = [x["how"] for x in au]
    check(f"[{mode}] 任务单开始和结束都记了日志", "渠道任务单 selftest 开始" in log and "渠道任务单 selftest 结束" in log, hows=hows)
    if mode == "owner":
        check("[owner] 主人说了就算同意: 读屏、动手都没弹确认 (how=turn), 做成了", hows[:3] == ["turn"] * 3 and all(x["ok"] for x in au[:3]),
              hows=hows, errors=[x.get("error") for x in au])
        check("[owner] 守门照旧: 发送按钮、聊天软件的窗口还是拦 (不到服务器)", hows[3:] == ["guard", "guard"]
              and calls == ["Screenshot", "Click", "Type"], hows=hows, calls=calls)
        check("[owner] 日志写着主人说了就算同意", "主人说了就算同意" in log)
        check("[owner] 开着冒充聊天软件的窗口: 截图不留", "截图没留" in log, tail=log[-300:])
        check("[owner] 页面报了任务单结束 (带步数)", "任务单结束 selftest" in log and "5 步" in log, tail=log[-300:])
    elif mode == "power":
        declared = next((e["tools"] for e in lines(a.cloud_log) if e.get("tools")), [])
        power = ["PowerShell", "FileSystem", "Registry", "Process", "Clipboard", "Scrape", "Notification"]
        check("[power] 高风险档的 7 个交给了模型 (设置里 guard.power 默认开)", all(f"mcp__computer__{t}" in declared for t in power),
              declared=declared)
        check("[power] 每次都问: 允许一次的做了 (how=once), 拒绝的没做 (how=deny); QQ 任务单的\"主人说了就算\"不算数",
              hows[:1] == ["once"] and hows[2:3] == ["deny"] and "turn" not in hows, hows=hows)
        check("[power] 守门先拦: 命令里删东西、删文件 (不问、不到服务器); 只有第一条命令到了服务器",
              hows == ["once", "guard", "deny", "guard"] and calls == ["PowerShell"], hows=hows, calls=calls,
              errors=[x.get("error") for x in au])
    elif mode == "power2":
        check("[power2] 守门先拦: 改注册表、关 explorer、抓本机地址、读系统目录 (不问、不到服务器)",
              hows == ["guard"] * 4 and calls == [], hows=hows, calls=calls, errors=[x.get("error") for x in au])
    elif mode == "self":
        check("[self] 她自己想看: 读屏放行 (turn), 动手还是问 (页面自动拒绝)", hows == ["turn", "deny"] and calls == ["Screenshot"],
              hows=hows, calls=calls)
        check("[self] 日志写着只放行读屏", "只放行读屏" in log)
    elif mode == "shotany":
        # --task-shot-any: 跳过"屏幕上有没有聊天、命令行"这道闸门, 测暂存 -> 回给页面 -> 页面压成 JPEG -> 回报带着截图
        check("[shotany] 截图暂存下来了 (宿主日志), 页面压成小图后报了结果 (带截图)",
              hows == ["turn"] and "留了一张截图" in log and "任务单结束 selftest 做完了 1 步 带截图" in log, hows=hows, tail=log[-300:])
    else:
        check("[shot] 读屏放行, 日志说了截图的去向 (留了一张 / 屏幕上还开着聊天、命令行这类窗口就没留)",
              hows == ["turn"] and ("留了一张截图" in log or "截图没留" in log), hows=hows, tail=log[-300:])
    check("[%s] 没有人为的报错" % mode, "错误 " not in log.replace("不到服务器", ""), tail=log[-200:] if "错误 " in log else "")
    close_pet(a.pid)
    for _ in range(20):
        if stub_processes("mcp_computer_stub") == 0:
            break
        time.sleep(0.5)
    check(f"[{mode}] 桌宠退出后替身进程一起结束", stub_processes("mcp_computer_stub") == 0)
    return finish()


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", choices=["owner", "self", "shot", "shotany", "power", "power2"])
    ap.add_argument("--run", choices=["owner", "self", "shot", "shotany", "power", "power2"])
    ap.add_argument("--cloud-log")
    ap.add_argument("--settings-dir")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--out")
    ap.add_argument("--log-from", type=int, default=0)
    a = ap.parse_args()
    if a.query:
        print(query_for(a.query, a.out))
        return 0
    return run(a)


if __name__ == "__main__":
    sys.exit(main())

"""桌宠工具调用的端到端自检 (tooltest.ps1 调用): 假云端 (fake_cloud.py) + 8794 mock 服务 + 开发版桌宠 + 旧版 MCP 替身.

    python tool_test.py --prepare <设置目录> --cloud-port 8797     先写好测试用的设置 (不碰用户真正的设置)
    python tool_test.py --run --settings-dir <目录> --pid <桌宠进程> --cloud-log <假云端日志> --api-port 8795

当前预设用"自定义"提供方连假云端, 勾上 stub 服务器 (echo 只读 -> 自动; add -> 每次确认) 和 bedtime-story 这个 skill.
桌宠页面带 toolanswers=once,deny,timeout,always: 确认框依次自动回答这几种. 对话经本机接口的 wm-pet 发 (页面静音).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from api_test import Mcp, RESULTS, check, close_pet  # noqa: E402

import openai  # noqa: E402

STUB = os.path.join(HERE, "mcp_legacy_stub.py")


def stub_log(settings_dir: str) -> str:
    return os.path.join(settings_dir, "stub-log.jsonl")


def prepare(settings_dir: str, cloud_port: int) -> None:
    skill = os.path.join(settings_dir, "skills", "bedtime-story")
    os.makedirs(skill, exist_ok=True)
    with open(os.path.join(skill, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: bedtime-story\ndescription: 用户要听睡前故事时用\n---\n\n# 睡前故事的做法\n语速放慢, 故事温和, 结尾道晚安.\n")
    settings = {
        "version": 1, "active": "p-tool",
        "personas": [
            {"id": "default", "name": "默认", "prompt": "", "provider": "local", "model": "", "mcp": [], "skills": []},
            {"id": "p-tool", "name": "工具自检", "prompt": "", "provider": "custom", "model": "fake-chat", "mcp": ["s-stub"],
             "skills": ["bedtime-story"], "builtins": []},  # 内置工具不交 (这个自检只测 MCP 和 skill)
        ],
        "providers": {"custom": {"base_url": f"http://127.0.0.1:{cloud_port}/v1", "model": ""}},  # 自定义地址可以不带 key
        # 调用超时设成 5 秒 (测超时用; 面板里默认 60)
        "mcp": [{"id": "s-stub", "name": "stub", "transport": "stdio", "command": sys.executable,
                 "args": ["-I", STUB, "stdio", "--log", stub_log(settings_dir)], "cwd": "", "url": "", "enabled": True,
                 "tools": {"add": "ask"}, "timeout_s": 5}],
    }
    with open(os.path.join(settings_dir, "pet-settings.json"), "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False)
    # 她自己装的 MCP: 照服务端的做法装一个例子 (看月相) 到自检目录的 self-mcp 下 (宿主自检时从这里读, 不碰你真的那份)
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
    from wm import selfmcp
    selfmcp.install(Path(selfmcp.__file__).parent / "selfmcp_example", "auto", Path(settings_dir) / "self-mcp")


def audit_lines(settings_dir: str) -> list[dict]:
    p = os.path.join(settings_dir, "pet-tool-audit.jsonl")
    return [json.loads(x) for x in open(p, encoding="utf-8")] if os.path.exists(p) else []


def stub_processes() -> int:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'mcp_legacy_stub' } | Measure-Object).Count"],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def run(a) -> int:
    token = open(os.path.join(os.environ["APPDATA"], "WorldModel", "pet-api-token.txt"), encoding="utf-8").read().strip()
    base = f"http://127.0.0.1:{a.api_port}"
    mcp = Mcp(base, token)
    page = {}
    for _ in range(120):  # 等页面就绪、预设生效、工具表交给服务端
        try:
            _, _, st = mcp.tool("pet_state")
            page = (st.get("structuredContent") or {}).get("page") or {}
            if page.get("connected") and (page.get("tools") or {}).get("accepted") and (page.get("profile_status") or {}).get("ok"):
                break
        except Exception:
            pass
        time.sleep(1)
    ps, tl = page.get("profile_status") or {}, page.get("tools") or {}
    check("预设生效 (云端: 假云端) 并交了工具表 (stub 3 个 + load_skill + 她自己装的看月相)",
          ps.get("ok") and ps.get("engine") == "cloud" and tl.get("accepted") == 5 and tl.get("active"), profile=ps, tools=tl)
    client = openai.OpenAI(base_url=f"{base}/v1", api_key=token, timeout=120, max_retries=0)

    def say(text: str) -> str:
        c = client.chat.completions.create(model="wm-pet", messages=[{"role": "user", "content": text}])
        return c.choices[0].message.content or ""

    steps = [
        ("用回声工具试一下", "自动 (只读的 echo)", lambda r: "你好呀" in r, "auto"),
        ("用加法工具算一下", "确认: 允许一次", lambda r: "5" in r and "没用成" not in r, "once"),
        ("再用加法工具算一下", "确认: 拒绝", lambda r: r.startswith("工具没用成") and "拒绝" in r, "deny"),
        ("再用一次加法工具", "确认: 超时 (3 秒)", lambda r: r.startswith("工具没用成") and "没有及时确认" in r, "timeout"),
        ("加法工具这次总是允许", "确认: 总是允许", lambda r: "5" in r and "没用成" not in r, "always"),
        ("加法工具再来一次", "总是允许之后不再问", lambda r: "5" in r and "没用成" not in r, "auto"),
        ("读一下技能说明", "load_skill", lambda r: "睡前故事的做法" in r, "auto"),
        # 慢工具 (睡 8 秒): 页面 1.5 秒后替用户点"停", 宿主马上放弃等待并告诉 MCP 服务器取消
        ("慢工具停", "停: 对话栏里点了停 (2 秒内回到模型)", lambda r: r.startswith("工具没用成") and "停掉" in r, "auto", (0, 3500)),
        # 慢工具 (睡 9 秒): 这个服务器的调用超时设的 5 秒
        ("慢工具等", "超时: 面板里设的 5 秒", lambda r: r.startswith("工具没用成") and "等了 5 秒" in r, "auto", (4500, 7500)),
        ("用回声工具试一下", "停掉、超时之后连接照常能用", lambda r: "你好呀" in r, "auto"),
        ("看看那天的月相", "她自己装的 MCP (只读 -> 自动)", lambda r: "满月" in r, "auto"),
    ]
    for text, name, ok, how, *ms_range in steps:
        before = len(audit_lines(a.settings_dir))
        t0 = time.time()
        reply = say(text)
        lines = audit_lines(a.settings_dir)[before:]
        last = lines[-1] if lines else {}
        ms_ok = not ms_range or ms_range[0][0] <= (last.get("ms") or 0) <= ms_range[0][1]
        check(f"工具: {name}", ok(reply) and last.get("how") == how and ms_ok, reply=reply[:60], how=last.get("how"), tool=last.get("tool"),
              ms=last.get("ms"), error=last.get("error"), seconds=round(time.time() - t0, 1))
    check("停掉、超时都没有重连 (还是那一个 MCP 服务器进程)", stub_processes() == 1, processes=stub_processes())
    events = [json.loads(x) for x in open(stub_log(a.settings_dir), encoding="utf-8")] if os.path.exists(stub_log(a.settings_dir)) else []
    slow_ids = [e["id"] for e in events if e["event"] == "call" and e["name"] == "slow"]
    cancelled = [e["requestId"] for e in events if e["event"] == "cancelled"]
    dropped = [e["id"] for e in events if e["event"] == "dropped"]
    check("两次都给 MCP 服务器发了取消 (notifications/cancelled), 服务器不再回", len(slow_ids) == 2 and sorted(map(str, cancelled)) == sorted(map(str, slow_ids))
          and len(dropped) == 2, calls=slow_ids, cancelled=cancelled, reasons=[e.get("reason") for e in events if e["event"] == "cancelled"], dropped=dropped)

    perms = json.load(open(os.path.join(a.settings_dir, "pet-settings.json"), encoding="utf-8"))["mcp"][0]["tools"]
    check("总是允许写回了设置 (add -> auto)", perms.get("add") == "auto", tools=perms)
    lines = audit_lines(a.settings_dir)
    check("审计日志: 每次一行, 有参数摘要和耗时, 没有密钥", len(lines) == len(steps)
          and all("args" in x and "ms" in x and "how" in x for x in lines), count=len(lines), hows=[x["how"] for x in lines])
    reqs = [json.loads(x) for x in open(a.cloud_log, encoding="utf-8")]
    declared = next((r["tools"] for r in reqs if r["tools"]), [])
    check("交给模型的工具名 (mcp__服务器__工具 + load_skill; 她自己装的用英文 id)",
          sorted(declared) == ["load_skill", "mcp__self-moon__phase", "mcp__stub__add", "mcp__stub__echo", "mcp__stub__slow"],
          tools=declared)
    check("工具结果回到了模型 (role tool)", sum(1 for r in reqs if r["last_role"] == "tool") == len(steps))
    close_pet(a.pid)
    for _ in range(20):
        if stub_processes() == 0:
            break
        time.sleep(0.5)
    check("桌宠退出后 MCP 服务器进程一起结束 (作业对象)", stub_processes() == 0)
    failed = [x["name"] for x in RESULTS if not x["ok"]]
    print(json.dumps({"total": len(RESULTS), "failed": failed}, ensure_ascii=False))
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare")
    ap.add_argument("--cloud-port", type=int, default=8797)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--settings-dir")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--cloud-log")
    ap.add_argument("--api-port", type=int, default=8795)
    a = ap.parse_args()
    if a.prepare:
        prepare(a.prepare, a.cloud_port)
        return 0
    return run(a)


if __name__ == "__main__":
    sys.exit(main())

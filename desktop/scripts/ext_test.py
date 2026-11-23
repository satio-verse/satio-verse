"""扩展中心 E1 的端到端自检 (exttest.ps1 调用): 内置工具 (提醒、小屋动作、打开网址) + 面板里内容包的导入导出.

    python ext_test.py --prepare <设置目录> --cloud-port 8817        先写好测试用的设置 (不碰用户真正的设置)
    python ext_test.py --run --settings-dir <目录> --pid <桌宠进程> --cloud-log <假云端日志> --api-port 8815 --log-from <字节>
    python ext_test.py --panel --settings-dir <目录> --out <面板自检结果> --pid <桌宠进程>

第一段: 假云端 + 8814 mock + 开发版桌宠 (小屋模式, --dry-open 不真开浏览器). 假云端看到"电脑:工具 {参数} | …"就依次调.
确认卡片按 toolanswers=deny,once,always 依次自动回答 (打开网址拒绝、允许一次; 小屋动作设成每次确认, 第一次总是允许).
第二段: 面板带 exttest=1 自己走一遍内容包 (panel-ext.js 的 extSelftest), 再在这里核对导出的文件.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from api_test import Mcp, RESULTS, check, close_pet  # noqa: E402

import openai  # noqa: E402

BUILTINS = ["cancel_reminder", "open_url", "remind", "room_do"]


def prepare(settings_dir: str, cloud_port: int) -> None:
    skill = os.path.join(settings_dir, "skills", "bedtime-story")
    os.makedirs(skill, exist_ok=True)
    with open(os.path.join(skill, "SKILL.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("---\nname: bedtime-story\ndescription: 用户要听睡前故事时用\n---\n\n# 睡前故事的做法\n语速放慢, 故事温和, 结尾道晚安.\n")
    settings = {
        "version": 1, "active": "p-ext",
        "personas": [
            {"id": "default", "name": "默认", "prompt": "", "provider": "local", "model": "", "mcp": [], "skills": []},
            # builtins 没写 = 都启用
            {"id": "p-ext", "name": "扩展自检", "prompt": "自检用的人设.", "provider": "custom", "model": "fake-chat", "mcp": [], "skills": []},
        ],
        "providers": {"custom": {"base_url": f"http://127.0.0.1:{cloud_port}/v1", "model": ""}},
        "mcp": [],
        "builtins": {"room_do": "ask"},  # 小屋动作设成每次确认: 测"总是允许"写回设置
    }
    with open(os.path.join(settings_dir, "pet-settings.json"), "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False)


def lines(path: str) -> list[dict]:
    return [json.loads(x) for x in open(path, encoding="utf-8")] if os.path.exists(path) else []


def host_log(log_from: int) -> str:
    with open(os.path.join(HERE, "..", "build", "dev", "wmpet.log"), "rb") as f:
        f.seek(log_from)
        return f.read().decode("utf-8", "replace")


def run(a) -> int:
    token = open(os.path.join(os.environ["APPDATA"], "WorldModel", "pet-api-token.txt"), encoding="utf-8").read().strip()
    base = f"http://127.0.0.1:{a.api_port}"
    mcp = Mcp(base, token)
    page = {}
    for _ in range(150):  # 等页面就绪 (小屋要加载房间)、预设生效、工具表交给服务端
        try:
            _, _, st = mcp.tool("pet_state")
            page = (st.get("structuredContent") or {}).get("page") or {}
            if page.get("connected") and (page.get("tools") or {}).get("accepted") and (page.get("profile_status") or {}).get("ok"):
                break
        except Exception:
            pass
        time.sleep(1)
    time.sleep(3)  # 小屋里她先走到第一件事那里
    check("预设生效并交了工具表 (4 个内置工具)", (page.get("tools") or {}).get("accepted") == 4, tools=page.get("tools"))
    client = openai.OpenAI(base_url=f"{base}/v1", api_key=token, timeout=120, max_retries=0)
    audit_path = os.path.join(a.settings_dir, "pet-tool-audit.jsonl")

    def say(text: str):
        before = len(lines(audit_path))
        c = client.chat.completions.create(model="wm-pet", messages=[{"role": "user", "content": text}])
        time.sleep(0.3)
        return c.choices[0].message.content or "", lines(audit_path)[before:]

    reqs = lines(a.cloud_log)
    reply, au = say('电脑:remind {"in_minutes": 0.05, "text": "喝水"}')
    reqs = lines(a.cloud_log)
    declared = sorted(next((r["tools"] for r in reqs if r["tools"]), []))
    check("交给模型的工具: 4 个内置的", declared == BUILTINS, declared=declared)
    check("提醒: 自动记下, 回了时间和编号", au and au[-1]["how"] == "auto" and au[-1].get("builtin") and au[-1]["server"] == "桌宠"
          and "喝水" in reply and "r1" in reply, reply=reply[:100], audit=au)
    for _ in range(20):
        if "[页面] 提醒到了 r1" in host_log(a.log_from):
            break
        time.sleep(0.5)
    log = host_log(a.log_from)
    check("提醒到点了: 宿主告诉页面, 页面接着办 (对话栏、小屋里过来看你)", "提醒到了 r1" in log and "[页面] 提醒到了 r1" in log)
    reply, au = say('电脑:remind {"at": "25:99", "text": "随便"}')
    check("提醒: 认不得的时间回错误", au and not au[-1]["ok"] and "认不得时间" in reply, reply=reply[:100])
    reply, au = say('电脑:remind {"in_minutes": 60, "text": "开会"} | cancel_reminder {} | cancel_reminder {"id": "r2"}')
    check("提醒: 记一个、列出来、取消", len(au) == 3 and all(x["ok"] for x in au) and "r2" in reply and "开会" in reply and "取消了 r2" in reply,
          reply=reply[:200], hows=[x["how"] for x in au])
    for name, url, word in (("本机地址", "http://127.0.0.1:5099/", "本机"), ("不是网页", "javascript:alert(1)", "http"),
                            ("付款页面", "https://example.com/pay/checkout", "付款")):
        reply, au = say("电脑:open_url " + json.dumps({"url": url}))
        check(f"打开网址: {name}不问就拦", au and au[-1]["how"] == "guard" and word in (au[-1].get("error") or ""), reply=reply[:80],
              error=au[-1].get("error") if au else None)
    reply, au = say('电脑:open_url {"url": "https://example.com/"}')
    check("打开网址: 问了, 拒绝", au and au[-1]["how"] == "deny", reply=reply[:80], audit=au)
    reply, au = say('电脑:open_url {"url": "https://example.com/"}')
    check("打开网址: 允许一次 (自检不真开)", au and au[-1]["how"] == "once" and au[-1]["ok"] and "自检没真开" in reply
          and "打开网址 (自检, 没真开)" in host_log(a.log_from), reply=reply[:80])
    reply, au = say('电脑:room_do {"act": "window"}')
    s = json.load(open(os.path.join(a.settings_dir, "pet-settings.json"), encoding="utf-8"))
    check("小屋动作: 问了, 总是允许, 她去做了; 写回了设置", au and au[-1]["how"] == "always" and au[-1]["ok"] and "她去" in reply
          and s.get("builtins", {}).get("room_do") == "auto", reply=reply[:80], builtins=s.get("builtins"))
    reply, au = say('电脑:room_do {"act": "read"}')
    check("小屋动作: 总是允许之后不再问", au and au[-1]["how"] == "auto" and au[-1]["ok"], reply=reply[:80])
    reply, au = say('电脑:room_do {"act": "fly"}')
    check("小屋动作: 不认识的事做不了", au and not au[-1]["ok"], reply=reply[:80])
    close_pet(a.pid)
    return finish()


def panel(a) -> int:
    out = None
    for _ in range(120):
        out = next((x for x in lines(a.out) if x.get("panel") == "exttest"), None)
        if out:
            break
        time.sleep(0.5)
    if not out:
        check("面板的扩展自检有结果", False)
        return finish()
    if out.get("error"):
        check("面板的扩展自检没出错", False, error=out["error"])
    for c in out.get("checks", []):
        check("面板: " + c.pop("name"), c.pop("ok"), **c)
    path = os.path.join(a.settings_dir, "exports", "selftest-export-0.1.0.wmext")
    ok = os.path.exists(path)
    doc = json.load(open(path, encoding="utf-8")) if ok else {}
    files = doc.get("files", [])
    hashes = all(hashlib.sha256(f["text"].encode("utf-8")).hexdigest() == f["sha256"] for f in files)
    paths = sorted(f["path"] for f in files)
    persona = next((json.loads(f["text"]) for f in files if f["path"].startswith("personas/")), {})
    check("导出的文件: 清单对、每个文件的 SHA-256 对、有人设和 skill", ok and doc.get("wmext") == 1 and doc["manifest"]["id"] == "selftest-export"
          and hashes and paths == ["personas/p1.json", "skills/bedtime-story/SKILL.md"], paths=paths)
    check("导出的人设不带 key、模型地址、MCP 和内置工具", persona and not ({"mcp", "builtins", "api_key", "key", "base_url", "id"} & set(persona)),
          fields=sorted(persona))
    close_pet(a.pid)
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
    ap.add_argument("--cloud-port", type=int, default=8817)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--panel", action="store_true")
    ap.add_argument("--settings-dir")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--cloud-log")
    ap.add_argument("--api-port", type=int, default=8815)
    ap.add_argument("--log-from", type=int, default=0)
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.prepare:
        prepare(a.prepare, a.cloud_port)
        return 0
    return panel(a) if a.panel else run(a)


if __name__ == "__main__":
    sys.exit(main())

"""Agent 互发消息的总开关和"让它继续" —— 只有你本人在真终端里敲才行 (计划: backups/tool4-agent-msg/计划.md 第 6 节).

    python tools/agent_msg_ctl.py status                 看开关、今天的用量、各条链的来回数
    python tools/agent_msg_ctl.py on                     打开总开关 (要真终端 + 键入确认词)
    python tools/agent_msg_ctl.py off                    关 (随时随地可以, 不要终端)
    python tools/agent_msg_ctl.py codex on|off           单独开 / 关 Codex 这一档 (要真终端; 没经过真机验证, 默认关)
    python tools/agent_msg_ctl.py resume [链]            把一条链 (不给就全部) 的来回数清零 (要真终端)

"开" 和 "继续" 检查标准输入输出都是交互终端: 她从会话里起的命令是没有终端的, 过不了这一关. 这不是对"完全授权档"的防护.
状态放 ~/.worldmodel/serve/agent-msg/state.json (serve 整个目录她的读写工具碰不到). 不连服务端: 改的是文件, 服务端每次问开关都会重读.
--home 只给测试用 (换成临时目录).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wm.agentmsg import AgentMsgError  # noqa: E402
from wm.agentmsg.limits import Limits  # noqa: E402

CONFIRM = "确认"


def default_home() -> Path:
    return Path.home() / ".worldmodel" / "serve" / "agent-msg"


def need_terminal(what: str) -> None:
    if not (sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty()):
        raise SystemExit(f"不行：{what}要你本人在真终端里敲（这里不是交互终端）。")
    answer = input(f"{what}。确认请键入「{CONFIRM}」：").strip()
    if answer != CONFIRM:
        raise SystemExit("没有确认，什么都没改。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", help="状态目录 (测试用)")
    ap.add_argument("cmd", choices=["status", "on", "off", "codex", "resume"])
    ap.add_argument("arg", nargs="?", default="")
    ns = ap.parse_args(argv)
    limits = Limits(ns.home or default_home())
    if ns.cmd == "status":
        print(json.dumps(limits.snapshot(), ensure_ascii=False, indent=1))
    elif ns.cmd == "off":
        limits.set_flag("enabled", False)
        print("总开关已关。")
    elif ns.cmd == "on":
        need_terminal("打开 Agent 互发消息的总开关（她可以主动找 Claude Code，来回最多 6 次、每天最多 20 条、约 2 元）")
        limits.set_flag("enabled", True)
        print("总开关已开（服务端不用重启）。")
    elif ns.cmd == "codex":
        if ns.arg not in ("on", "off"):
            raise SystemExit("用法：codex on|off")
        if ns.arg == "on":
            need_terminal("单独打开 Codex 这一档（没经过真机验证）")
        limits.set_flag("codex", ns.arg == "on")
        print(f"Codex 这一档已{'开' if ns.arg == 'on' else '关'}。")
    elif ns.cmd == "resume":
        need_terminal("让" + (f"链 {ns.arg}" if ns.arg else "所有链") + "的来回数清零（再来 6 次）")
        try:
            n = limits.resume(ns.arg or None)
        except AgentMsgError as exc:
            raise SystemExit(str(exc))
        print(f"清零了 {n} 条链。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

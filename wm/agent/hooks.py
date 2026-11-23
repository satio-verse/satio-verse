"""工作台的钩子 (2026-11-07 底层对标 P1-6, docs/agent-parity-20261107.md): 主人在固定的时候跑自己的命令, 拦下 / 改参数 / 补一句话.
照 Codex (learn.chatgpt.com/docs/hooks) 和 Claude Code (code.claude.com/docs/en/hooks) 的格式和事件名.

配置只认工作台家目录下的 hooks.json (主人自己的; 守门名单默认不让她读写这个文件), **工作区里的钩子不认** (免得仓库里藏命令).
钩子只能更严、不能放宽: PreToolUse 改过的参数照样过守门和权限, 说 allow 也不会跳过要你点头的卡片.

  {"hooks": {"PreToolUse": [{"matcher": "run_command|write_file",
                             "hooks": [{"type": "command", "command": "python D:/hooks/check.py", "timeout": 30}]}],
             "Stop": [{"hooks": [{"type": "command", "command": "...", "args": ["可选: 给了就不经过 PowerShell, 直接起程序"]}]}]}}

事件 (matcher 是正则, 不分大小写; "*"、空、不写 = 全匹配):
  PreToolUse     要用工具之前 (matcher 比工具名)          可拦下 (退出码 2 / decision=block)、改参数 (updated_input)
  PostToolUse    工具做完以后 (matcher 比工具名)          补一句 (additional_context) / 提醒 (decision=block 只是把 reason 交给她)
  UserPromptSubmit 你的话开始做之前                        可拦下这一句、补背景
  Stop           她说做完了的时候                          decision=block: 把 reason 交给她接着做 (一轮最多 STOP_MAX 次)
  SessionStart   会话第一轮 / 继续上次任务 (matcher 比 new / resume)  补背景
  PreCompact     写检查点之前 (matcher 比 budget / overflow / manual)  可拦下这次压缩、additional_context 当成"重点"
  PostCompact    写完检查点以后                            只通知
  SubagentStart  派出子任务时 (matcher 比子任务的预设)     additional_context 加进子任务的说明
  SubagentStop   子任务结束时                              只通知
命令从标准输入收一行 JSON (event、session_id、workspace、mode、run_id、parent_id 加各事件自己的), 环境变量里像 key、token 的都去掉;
退出码 0 + 标准输出是 JSON 对象: {decision, reason, updated_input, additional_context, system_message}; 标准输出不是 JSON 时,
UserPromptSubmit / SessionStart 把它当背景, 别的不管; 退出码 2 = 拦下 (原因写在标准错误); 别的退出码、超时只记一笔, 不拦.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import tools as T

log = logging.getLogger("wm.agent.hooks")
EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart", "PreCompact", "PostCompact",
          "SubagentStart", "SubagentStop")
FILE = "hooks.json"
TIMEOUT_S, TIMEOUT_MAX_S = 30.0, 300.0
OUT_MAX = 64_000
STOP_MAX = 3      # 一轮里 Stop 钩子最多让她接着做几次
MAX_HOOKS = 32    # 一个事件最多几个钩子


@dataclass
class Outcome:
    blocked: bool = False
    reason: str = ""
    updated_input: dict | None = None
    context: str = ""
    notes: list[str] = field(default_factory=list)  # 给页面看的 (钩子出错、超时、system_message)
    ran: int = 0


class Hooks:
    def __init__(self, home: Path):
        self.path = Path(home) / FILE
        self._cache: tuple[tuple[int, int], dict] | None = None

    def config(self) -> dict:
        """hooks.json -> {事件: [{matcher, hooks}]}; 文件没有、写错了的当成没有钩子 (写错的记日志). 文件没变不重读."""
        try:
            st = self.path.stat()
        except OSError:
            self._cache = None
            return {}
        key = (st.st_mtime_ns, st.st_size)
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1]
        conf: dict = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig")) if st.st_size <= 1_000_000 else {}
            if isinstance(data, dict) and data.get("disabled") is not True and isinstance(data.get("hooks"), dict):
                for event, groups in data["hooks"].items():
                    if event in EVENTS and isinstance(groups, list):
                        conf[event] = [g for g in groups if isinstance(g, dict) and isinstance(g.get("hooks"), list)]
        except (OSError, ValueError) as exc:
            log.warning("工作台钩子配置读不了, 先当没有: %s", exc)
            conf = {}
        self._cache = (key, conf)
        return conf

    def matching(self, event: str, target: str = "") -> list[dict]:
        out = []
        for group in self.config().get(event, []):
            pattern = str(group.get("matcher") or "")
            if pattern not in ("", "*"):
                try:
                    if not re.search(pattern, target or "", re.IGNORECASE):
                        continue
                except re.error:
                    continue
            out += [h for h in group["hooks"] if isinstance(h, dict) and h.get("type", "command") == "command"
                    and str(h.get("command") or "").strip()]
        return out[:MAX_HOOKS]

    def active(self, event: str) -> bool:
        return bool(self.config().get(event))

    async def fire(self, event: str, payload: dict, target: str = "", cwd: str | Path | None = None) -> Outcome:
        """跑这个事件对得上的所有钩子 (按配置的顺序一个个跑), 合成一个结果: 有一个拦下就算拦下 (原因用第一个的),
        改过的参数后面的覆盖前面的, 补的话接起来. 没有钩子时什么都不跑."""
        out = Outcome()
        hooks = self.matching(event, target)
        if not hooks:
            return out
        body = json.dumps({"event": event, **payload}, ensure_ascii=False, default=str)
        for hook in hooks:
            code, stdout, stderr, why = await self._run(hook, body, cwd, event)
            out.ran += 1
            name = str(hook.get("name") or hook.get("command"))[:60]
            if why:
                out.notes.append(f"钩子 {name} 没跑成（{why}），没拦")
                continue
            if code == 2:
                out.blocked = True
                out.reason = out.reason or (stderr.strip() or stdout.strip() or "钩子拦下了")[:2000]
                continue
            if code != 0:
                out.notes.append(f"钩子 {name} 退出码 {code}，没拦：{(stderr.strip() or stdout.strip())[:200]}")
                continue
            text = stdout.strip()
            data = None
            if text.startswith("{"):
                with contextlib.suppress(ValueError):
                    data = json.loads(text)
            if isinstance(data, dict):
                if str(data.get("decision") or "").lower() == "block":
                    out.blocked = True
                    out.reason = out.reason or str(data.get("reason") or "钩子拦下了")[:2000]
                if isinstance(data.get("updated_input"), dict):
                    out.updated_input = data["updated_input"]
                if data.get("additional_context"):
                    out.context += ("\n" if out.context else "") + str(data["additional_context"])[:8000]
                if data.get("system_message"):
                    out.notes.append(f"钩子 {name}：{str(data['system_message'])[:300]}")
            elif text and event in ("UserPromptSubmit", "SessionStart"):
                out.context += ("\n" if out.context else "") + text[:8000]
        return out

    async def _run(self, hook: dict, body: str, cwd, event: str) -> tuple[int, str, str, str]:
        """跑一个钩子命令 -> (退出码, 标准输出, 标准错误, 没跑成的原因 / 空串). 超时连子进程一起结束."""
        try:
            timeout = max(1.0, min(float(hook.get("timeout") or TIMEOUT_S), TIMEOUT_MAX_S))
        except (TypeError, ValueError):
            timeout = TIMEOUT_S
        command = str(hook.get("command") or "").strip()
        args = hook.get("args")
        env = T._clean_env()
        env.update(WM_HOOK_EVENT=event)
        workdir = str(cwd) if cwd and Path(cwd).is_dir() else None
        try:
            if isinstance(args, list) and all(isinstance(a, str) for a in args):
                argv = [command, *args]
            else:
                argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", T.PS_PRELUDE + command] if os.name == "nt" \
                    else ["/bin/sh", "-c", command]
            proc = await asyncio.create_subprocess_exec(*argv, cwd=workdir, env=env, stdin=subprocess.PIPE,
                                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, ValueError) as exc:
            return -1, "", "", f"起不来: {type(exc).__name__}"
        try:
            out, err = await asyncio.wait_for(proc.communicate(body.encode("utf-8")), timeout)
        except asyncio.TimeoutError:
            await T._terminate_command_tree(proc)
            return -1, "", "", f"超过 {timeout:g} 秒"
        except asyncio.CancelledError:
            await asyncio.shield(T._terminate_command_tree(proc))
            raise
        return (proc.returncode if proc.returncode is not None else -1,
                (out or b"")[:OUT_MAX].decode("utf-8", errors="replace"), (err or b"")[:16000].decode("utf-8", errors="replace"), "")

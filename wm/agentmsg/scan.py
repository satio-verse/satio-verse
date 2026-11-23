"""发出去之前过一道: 长度、受保护路径、密钥样子的字符串. 目的: 对方 Codex 的只读沙箱能读工作区以外的整块磁盘 (read-only 只管写),
所以不能让她靠一句话把对方指到 ~/.ssh、保管箱、桌宠设置上; 也不把密钥贴出去."""
from __future__ import annotations

import re
from pathlib import Path

from . import MESSAGE_MAX, AgentMsgError

SECRET = [
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "像 API 密钥 (sk-…)"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "像 GitHub 令牌"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "像私钥"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I), "像 Bearer 令牌"),
    (re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|token)\s*[=:：]\s*\S{6,}"), "像 password= / token= 这类带值的密钥"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "像 AWS 密钥"),
]


def _norm(text: str) -> str:
    return str(text).replace("/", "\\").replace("\\\\", "\\").lower()


def check_message(text, protected: list[Path] | None = None) -> str:
    """通过返回规整后的正文; 不行抛 AgentMsgError("bad_message", 原因)."""
    if not isinstance(text, str) or not text.strip():
        raise AgentMsgError("bad_message", "message 要写点什么")
    text = text.replace("\r\n", "\n").strip()
    if len(text) > MESSAGE_MAX:
        raise AgentMsgError("bad_message", f"太长了 ({len(text)} 字, 上限 {MESSAGE_MAX}): 只传问题和结论, 对方在同一个工作区里自己看代码")
    flat = _norm(text)
    for p in protected or []:
        needle = _norm(p)
        if needle and needle in flat:
            raise AgentMsgError("bad_message", "里面带了受保护的位置 (密钥、保管箱、桌宠设置、记忆库这类), 不能发给别的 agent")
    for pattern, what in SECRET:
        if pattern.search(text):
            raise AgentMsgError("bad_message", f"里面{what}, 不能发给别的 agent")
    return text

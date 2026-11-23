"""外部渠道 (第一个是 QQ, 经 SnowLuma 的 OneBot v11): 外面来的消息一律进访客会话, 和本人的会话 (pipeline.Session) 隔开.
方案和理由见 docs/roadmap/external-channels.md 第 5 节.

准入靠代码, 不靠提示词:
  - 访客会话 (guest.GuestSession) 是另一个类, 不继承 Session: 没有 tools / profile / tool_result / memory_review / state
    的处理, 调模型时不带工具; 用的云端模型 allow_tools=False, 谁传了工具就抛错;
  - 能力表 Caps 构造会话时按档给, 之后改不了 (frozen);
  - 渠道管理器 (manager.ChannelManager) 不持有任何 Session: 给主人的东西 (状态、草稿、提示) 走 notify, 只出不进;
  - 访客会话不调 Memory.observe、不写主人的 日志/ 和 关系.md; 渠道日志放 <库>/外部渠道/, 主人记忆的检索里必须排除.
    唯一的口子: 主人私聊 (QQ 主人号、微信扫码人) 里他自己打的字经 Memory.propose_said 只进 候选.md, 他确认了才记.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Trust(str, Enum):
    """消息来源的信任档. 本人·本机 (桌宠对话栏、麦克风、本机网页) 走 pipeline.Session, 不在这里."""
    OWNER_REMOTE = "owner_remote"  # 主人的号私聊她的号: 号可能被盗、经非官方协议转来, 所以照样没有工具
    ACQUAINTANCE = "acquaintance"  # 白名单里的群和好友私聊


@dataclass(frozen=True)
class Caps:
    """一个访客会话能做什么. 外部渠道永远没有工具和本机 MCP, 不直接写记忆, 看不到主人电脑上的事."""
    tools: bool = False
    owner_facts: bool = False      # 能读主人记忆里的名字和喜好 (只有 OWNER_REMOTE)
    memory_write: str = "candidate"
    owner_context: bool = False    # 主人会话的历史、state.activity、工具结果


CAPS = {
    Trust.OWNER_REMOTE: Caps(owner_facts=True),
    Trust.ACQUAINTANCE: Caps(),
}

from .manager import ChannelManager  # noqa: E402  (manager 用到上面的 Trust 和 Caps)

__all__ = ["CAPS", "Caps", "ChannelManager", "Trust"]

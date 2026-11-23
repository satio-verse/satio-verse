"""一个 WebSocket 连接对应一个 Session, 负责整轮编排:

  麦克风 PCM -> 能量 VAD (记"说完"时刻) + 流式 ASR -> 端点 -> 开新一轮
  新一轮: 立刻发起势动作 -> 单次流式 LLM -> 增量解析
          acts 闭合 -> 发 intent (动作先行); emo 闭合 -> 发 emotion; reply 增量 -> 分句 -> TTS 队列
  TTS 工作协程按分句顺序合成 -> (可选 RVC, 分句级) -> 二进制音频帧下发
  长回复 (讲故事、详细说说): 意图那次照常说完一句短回应, 再追加一次不带 JSON 约束的流式生成, 走同样的分句和合成
  前端回报 client_* 打点; 播放结束或超时后落盘这一轮的 trace.
用户插话 (ASR 出现部分结果) 或发来新一轮时, 当前轮整体取消: LLM 流关闭, TTS 停止, 前端清空播放队列.

投机执行: ASR 部分结果稳定 spec_stable_ms 后, 用它提前开一轮 (LLM + TTS 都跑), 但这一轮的下发消息
先攒在 turn.buffer 里. 端点到达时最终文字一致就一次性放出 (之后实时下发), 不一致或部分结果变了就取消.
前端只会看到被确认的那一轮.

人设预设 (上行 profile): 每条连接可以换人设文字和模型 (服务端的本地模型, 或云端 OpenAI 兼容模型, 见 cloud.py).
云端模型可以调工具 (上行 tools): 下发 tool_call 给桌宠执行, 等 tool_result 回来接着生成. 没发 profile 的连接照旧.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import secrets
import struct
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
import numpy as np

from . import cloud as C
from . import hum as H
from . import intent as I
from . import ledger as L
from . import memory as M
from . import promise as P
from . import room_plan as R
from . import room_action as RA
from . import screen_along as SA
from . import self_card as SC
from . import watch_along as WA
from .a2f import FaceStream
from .asr import EnergyVAD
from .clauses import ClauseSplitter, speakable_len, speakable_text
from .clock import now_ms
from .confidence import field_confidence, rule_confidence
from .echo import EchoGuard, SpeechGain
from .prosody import analyze as analyze_prosody
from .social import SocialTracker, key_of
from .engines import Engines
from .json_stream import JSONObjectStream
from .trace import CLIENT_MARKS, DELIVERY_MARKS, LatencyStats, TraceLog, TurnTrace

log = logging.getLogger("wm.pipeline")

AUDIO_MAGIC = b"WMA1"
AUDIO_HEADER = struct.Struct("<4sIIII")  # magic, turn, seg, sample_rate, seq
# 陪看完那段概括 (_watch_gist) 的输出格式
WATCH_GIST_SCHEMA = {"type": "object", "properties": {"gist": {"type": "string"}}, "required": ["gist"],
                     "additionalProperties": False}
# 陪看的时候不给模型的工具和 skill: 操作你电脑的那一组 (2026-11-08 实测: 屏幕陪看时你说"嗯那也就看下内容吧", 她拿你的鼠标
# 键盘操作了 2 分 15 秒: 切窗口、读了 6 次页面结构、把页面往下滚了 3 格、按了 Alt+Tab, 最后一句没说; 她本来就看得到你的屏幕)
WATCH_HELD_TOOL = "mcp__computer__"
WATCH_HELD_SKILLS = {"computer-use"}
WATCH_HELD_MSG = "陪看的时候不操作主人的电脑: 你已经在看他的屏幕 (或这个视频) 了, 照你看到的回答就好"
# 她自己的"看一眼屏幕" (服务端做, 不经桌宠; 2026-11-09 用户问"你现在看得到屏幕嘛？""那直播呢？", 她没开陪看,
# 只知道程序名, 说"直播画面我看不到". 用不用、什么时候用由她自己定; 规矩和屏幕陪看一样, 见 wm/screen_along.py)
LOOK_TOOL_NAME = "look_at_screen"
LOOK_TOOL = {"type": "function", "function": {
    "name": LOOK_TOOL_NAME,
    "description": ("看一眼主人电脑前台那个窗口, 告诉你看到了什么 (看一次; 只看得到前台那一个窗口, 聊天、密码、网银、登录支付、"
                    "写代码办公的窗口看不到). 他问你看不看得到他屏幕上的东西、让你看看、问你他在看的怎么样时用. "
                    "要一直陪着看, 请他说\"陪我看\"."),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
GLANCE_SCHEMA = {"type": "object", "properties": {"saw": {"type": "string"}}, "required": ["saw"], "additionalProperties": False}
_GLANCE_WHY = {"private": "前台是聊天、密码、网银、邮箱或登录支付这类窗口, 你不看",
               "work": "主人前台是写代码、办公或文件夹这类窗口, 你不看 (他在看视频、直播、玩游戏时你才看)",
               "self": "前台是你自己 (桌宠), 没在看别的", "none": "前台没有窗口 (桌面, 或者都最小化了)",
               "small": "前台是个小窗口, 不像在看视频或玩游戏, 你没看"}


async def _static_stream(text: str):
    """把一段现成的输出当成 LLM 流式输出交给同一套解析和语音流程."""
    yield text


def _with_reply(raw: str, reply: str) -> str:
    """模型的原始输出换掉回复字段 (回复侧检查截断时, 历史里记实际说出口的)."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    obj["reply"] = reply
    return I.dumps_compact(obj)


class Turn:
    def __init__(self, turn_id: int, source: str, text: str, trace: TurnTrace):
        self.id = turn_id
        self.source = source
        self.text = text
        self.trace = trace
        self.task: asyncio.Task | None = None
        self.client_done = asyncio.Event()
        self.committed = asyncio.Event()   # 普通轮立即确认; 投机轮等最终识别结果
        self.buffer: list | None = None    # 非 None 表示投机中, 下发消息先攒在这里
        self.basis = ""                    # 投机所依据的部分识别结果
        self.pending_history: tuple[str, str] | None = None
        self.op: str | None = None
        self.emo: str | None = None        # LLM 给的情绪, 面部动画的显式情绪跟着它
        self.state: str | None = None      # 开始这一轮时的角色状态行 (投机和确认用同一份)
        self.state_raw: dict | None = None  # 同一份状态的结构化原文, 回答"手里拿着什么"这类问题用
        self.raw_output = ""
        self.llm_meta: dict = {}           # 这一轮 LLM 请求的用量/耗时/token 概率
        self.messages: list | None = None  # 这一轮发给 LLM 的消息 (升级复核时原样再发一次)
        self.reply_text = ""               # 这一轮的完整回复 (写进长期记忆的日志)
        self.refusal: str | None = None    # 能力闸门改判成"做不到"时换上的固定回复
        self.req_kind = "?"                # 用户这句话是哪类请求 (promise.request_kind, 动作闭合时算一次)
        self.ack: str | None = None        # 模型说"做不到"却补成了动作: 发出意图后说这句, 模型的回复不再采用
        self.body_kind = "?"               # 同上, 但按这个身体算 (capabilities、在不在桌面上): 回复侧检查用
        self.can: frozenset | None = None  # 这一轮按哪些技能算做得到 (Session._can)
        self.place = I.ROOM                # 开始这一轮时角色在哪 (房间 / 桌面)
        self.long = False                  # 用户要听长一点的内容 (讲故事、详细说说): 意图之后追加一次长回复
        self.long_ran = False              # 长回复真的开始生成了
        self.long_open = False             # 长回复的固定开场已经说了 (模型的短回应不用)
        self.memory_line: str | None = None  # 这一轮检索到的记忆行 (长回复也带上)
        self.audio_s = 0.0                 # 已经下发的音频秒数 (长回复等前端播完的时限按它放宽)
        self.memory_answer: str | None = None  # 问用户自己固定信息时, 按记忆直接给的回答
        self.spoken = 0                    # 已经送去合成的分句数
        self.cut = False                   # 回复侧检查截断了回复, 模型后面生成的不再采用
        self.side_tasks: set = set()       # 韵律分析等后台小任务
        self.escalation: asyncio.Task | None = None  # 低置信度时的复核 (完成后才下发这一轮的意图)
        self.acts: list = []
        self.audio = True                  # 这一轮合成语音 (开始时取连接的 output 声明)
        self.hum = None                    # 这一轮说完以后要不要再哼一段 (wm/hum.py; 只在明确让她哼/唱时)
        self.llm = None                    # 这一轮用的模型 (开始时取连接的 profile; 中途换预设不影响这一轮)
        self.audio_segments = 0
        self.audio_seq = 0
        self.finalized = False
        self.aside: str | None = None      # 程序发起的一句 (陪看): 现成的输出 JSON, 不检索、不问模型, 直接说 (text 是空的)
        self.note = ""                     # 程序发起的那一轮在历史里记的提醒 (不是你说的话, 不显示在对话栏)


class Session:
    def __init__(self, engines: Engines, cfg: dict, send: Callable[[object], Awaitable[None]],
                 stats: LatencyStats, tracelog: TraceLog):
        self.e = engines
        self.cfg = cfg
        self.p = cfg["pipeline"]
        self._send = send
        self.stats = stats
        self.tracelog = tracelog
        self.schemas = {place: I.build_schema(place) for place in (I.ROOM, I.DESKTOP)}
        self.vad = EnergyVAD(cfg["asr"].get("vad_threshold_db", -42))
        self.asr = engines.asr.create_session()
        # 外放回声 (wm/echo.py): 拿电脑正在放的声音当参考, 从麦克风里去掉再给 VAD 和识别; 没在放声音、戴耳机时原样放过去
        self.echo: EchoGuard | None = None
        loopback = getattr(engines, "loopback", None)
        if self.asr is not None and loopback is not None and loopback.available():
            self.echo = EchoGuard(loopback, engines.echo_conf)
            if hasattr(self.asr, "echo"):
                self.asr.echo = self.echo
                self.asr.echo_text = bool(engines.echo_conf.get("text_check", True))
        # 页面报来的麦克风实际用上的处理 (mic_settings); 浏览器的自动增益关了时, 回声处理之后自己补音量 (wm/echo.py 的 SpeechGain)
        self.mic_settings: dict | None = None
        self.gain: SpeechGain | None = None
        self.turn_seq = 0
        self._mem_session = False  # 这次连接是否已经在记忆里记过一次见面
        self.diary_open = False    # 桌宠小屋的日记本翻开着: 不按 memory.diary_gate 门控
        self.diary_used = False    # 这条连接发过 diary (有日记本的客户端): 只有这种连接才门控, 网页、QQ 照旧都能读
        self._mem_listener = None  # 候选记忆变了 (整理出新的、在 Obsidian 或别的连接里确认了) 时推一份新列表
        self._cand_task: asyncio.Task | None = None
        if engines.memory is not None:
            loop = asyncio.get_running_loop()
            self._mem_listener = lambda: loop.call_soon_threadsafe(self._push_candidates)
            engines.memory.listeners.append(self._mem_listener)
        # QQ 渠道 (wm/channels): 本人会话只收它的状态、草稿和提示 (只出不进), 能配置、审草稿、暂停; 外部消息不会进这里
        self.channels = getattr(engines, "channels", None)
        if self.channels is not None:
            self.channels.attach(self.send)
        self.social = SocialTracker(engines.social)
        self.current: Turn | None = None
        self.turns: dict[int, Turn] = {}
        self.history: list[tuple[str, str]] = []
        self.char_state: str | None = None  # 前端上报的角色状态 (位置/姿势/手里的东西), 拼成一行中文
        self.capabilities: set | None = None  # 前端声明的可执行技能 (没声明时按 I.DEFAULT_CAN: 只去掉 OPT_IN 的新技能)
        self.place = I.ROOM                   # 角色在哪: 前端 state.place, 桌宠是 desktop (提示词里没有房间和家具)
        self.audio = True                     # 要不要语音: 客户端发 {"type": "output", "audio": false} 后不合成 (只要文字的本机接口)
        self.since_long = 9                   # 离上一次长回复隔了几轮 (0 = 上一轮就是): "后来呢" 隔一轮插话也算接着讲
        self.exec_note: str | None = None     # 上一个没做成的动作 (下一轮状态行里说一次)
        self.room_recent: list[tuple[float, str, str]] = []  # 小屋里刚发生的事 (时刻, 种类, 部位或东西): 下一轮状态行里说一次
        self.room_scene_id = "petal-oak-life-v1"
        self.room_active = engines.room
        self.room_known_sent = False          # 这条连接发过 room_known 没有 (页面声明了可供性以后发一次)
        self.room_seen = False                # 这条连接报过小屋里看到的东西 (room_observe): 问"杯子在哪"按她的认知答
        self.cog_note: tuple[float, str] | None = None  # 她在小屋里刚发现的 (不在原处了、在哪找到): 下一轮状态行里说一次
        self.char_state_raw: dict | None = None
        self.listening = False
        self.listening_at = 0.0               # 这一次"在听"是什么时候开始的 (now_ms; 收起时记时长)
        # 端点切早了的半句话: 被插话打断时用户还没听到任何回应, 就接到下一句前面重发.
        self.carry: dict | None = None
        self.spec: Turn | None = None
        self.partial = ""
        self.partial_since = 0.0
        self.spec_stats = {"started": 0, "committed": 0, "cancelled": 0}
        # KV 预热: 每轮写完历史后, 趁空闲把"few-shot + 历史 + 空的用户消息"算进 llama.cpp 的缓存,
        # 下一轮只需新算用户这句话. 新请求到来时先取消还没做完的预热, 免得它占着槽位.
        self.prewarm_task: asyncio.Task | None = None
        self.prewarm_stats = {"done": 0, "cancelled": 0, "failed": 0, "keepalive": 0}
        # 这条连接的 LLM 槽位 (Engines.slots 按连接分, 意图请求、预热和保温都用它, 两条连接的缓存互不冲掉);
        # 分完了是 None: 用默认槽位 llm.id_slot, 和别的连接共用
        self.slot = engines.slots.acquire()
        # 人设预设 (上行 profile, 桌宠的操作面板发): 没发过就是默认人设 + 服务端的本地模型, 和原来一样
        self.llm = engines.llm                # 这条连接用的模型; 选了云端模型时是 cloud.CloudLLM (key 只在它里面)
        self.cloud_spec: dict | None = None   # 云端模型的 provider/base_url/api_key/model (本地模型是 None)
        self.persona = ""                     # 人设文字 (空: 默认人设)
        self.skills: list[dict] = []          # 启用的 skill (名字和描述)
        self.profile_meta: dict | None = None  # 进 trace 的 profile 摘要 (不带 key)
        self.tools: list[dict] = []           # 上行 tools (OpenAI 的 tools 参数格式), 只交给云端模型
        self.pending_tools: dict[str, asyncio.Future] = {}  # 下发了 tool_call、等 tool_result 的调用
        self.tool_seq = 0
        self._retired: list = []              # 换下来的云端模型 (可能还有一轮在用), 连接断开时关
        self.cloud_cfg = cfg["llm"].get("cloud") or {}
        # 桌宠小屋问"接下来做什么" (上行 room_think): 同时只跑一个, 两次之间至少隔 room.think.min_interval_s
        self.room_task: asyncio.Task | None = None
        self.room_at = -1e12
        self.room_actions: dict | None = None
        self.pending_room_actions: dict[str, dict] = {}
        self.room_action_seq = 0
        # 陪看 (wm/watch_along.py): 你说"陪我看"以后她跟着你的进度看视频, 快看完一段说一句; 一条连接同时只陪看一个
        self.watch_conf = {**WA.DEFAULTS, **(cfg.get("watch_along") or {})}
        self.watch: WA.WatchAlong | None = None
        self.watch_wait = 0.0  # 说了"陪我看"没给链接: 等你发链接等到这个时刻 (time.monotonic)

    @property
    def cloud(self) -> bool:
        """这条连接选了云端模型."""
        return self.llm is not self.e.llm

    async def send(self, msg) -> None:
        if isinstance(msg, dict) and msg.get("type") in ("room_observe_done", "room_step", "room_known", "room_carry_plan", "room_carry_saved", "room_plan"):
            msg = {"scene_id": self.room_scene_id, **msg}
        await self._send(msg)

    async def _tsend(self, turn: Turn, msg) -> None:
        """轮次内的下发: 投机中先攒着, 确认后照原顺序放出."""
        if turn.buffer is not None:
            turn.buffer.append(msg)
        else:
            await self.send(msg)

    # -- 输入 ---------------------------------------------------------------
    async def on_audio(self, data: bytes) -> None:
        t = now_ms()
        pcm = np.frombuffer(data, dtype=np.int16)
        if self.echo is not None:
            pcm = await self.echo.process_async(pcm)
            if self.gain is not None:
                pcm = self.gain.process(pcm, self.echo.engaged)
        self.vad.update(pcm, t)
        if self.asr is None:
            return
        for kind, text in await self.asr.accept(pcm):
            if kind in ("partial", "pause"):
                await self.send({"type": "asr_partial", "text": text})
                if text != self.partial:
                    self.partial, self.partial_since = text, t
                    if self.spec and self.spec.basis != text:
                        await self._drop_spec("partial_changed")  # 用户还在说
                if text and self._can_barge_in(text):
                    await self.interrupt("barge_in")
                if text:
                    await self._start_listening()
                if kind == "pause":  # 整句识别引擎: 停顿时的结果直接拿来投机
                    await self._maybe_speculate(t, immediate=True)
            elif kind == "speech_start":
                self._touch()
                # 外放时: 这一刻听到的几乎全是回声就先不打断她, 等认出字再说 (认出来是你的话照样能打断)
                if self._can_barge_in(None) and (self.echo is None or self.echo.user_likely()):
                    await self.interrupt("barge_in")
                await self._start_listening()
            elif kind == "resume":
                self.partial = ""
                await self._drop_spec("resumed")
            elif kind == "final":
                await self._on_final(text, t)
            elif kind == "echo":
                chk = (getattr(self.asr, "last_meta", None) or {}).get("echo_check") or {}  # 只有数字: 对上的比例、判法
                log.info("外放回声: 这句是电脑放出来的声音, 不当成你说的 (%d 字, 对上 %s, 不全是回声的块 %s, 按%s判的)",
                         len(text), chk.get("ratio"), chk.get("near"), "残渣" if chk.get("by") == "gated" else "字")
            elif kind == "utt_end":
                await self._end_listening()
        if getattr(self.e.asr, "streaming_partials", True):
            await self._maybe_speculate(t)

    async def _start_listening(self) -> None:
        if self.listening:
            return
        self.listening = True
        self.listening_at = now_ms()
        await self.send({"type": "listening"})
        idle = self.current is None or self.current.finalized
        if idle and self.spec is None:
            self._start_keepalive()

    async def _end_listening(self) -> None:
        """这一段声音结束了却没有话 (没认出字、或者是外放的回声): 不算你在说话了.
        以前一直挂着"在听", 陪看想说的话一直被挡 (_aside_blocked 是 listening), 页面也停在"在听…"."""
        if not self.listening:
            return
        self.listening = False
        self.partial = ""
        self.vad.reset()
        # 只记时长 (真机上看"在听"挂了多久用; 2026-11-08 外放时连着挂过 29 秒)
        log.info("在听收起: 这一段 %.1f 秒, 没有要交出去的话", (now_ms() - self.listening_at) / 1000)
        await self.send({"type": "listening_end"})

    async def _on_final(self, text: str, t: float) -> None:
        self.listening = False
        self.partial = ""
        speech_end = self.vad.last_voiced or t
        speech_start = self.vad.speech_start
        self.vad.reset()
        await self.interrupt("new_turn")  # 先结束当前轮, 它可能留下半句
        carry, self.carry = self.carry, None
        merged = None
        if carry and speech_end - carry["speech_end"] <= self.p.get("merge_window_s", 3.0) * 1000:
            merged, text = carry["text"], carry["text"] + text
            speech_start = carry["speech_start"] if carry["speech_start"] is not None else speech_start
        spec = self.spec
        if spec and speakable_text(spec.text) == speakable_text(text):
            turn = await self._commit_spec(speech_end, speech_start)
        else:
            if spec:
                await self._drop_spec("mismatch")
            turn = await self.start_turn("voice", text, speech_end, speech_start)
        if merged:
            turn.trace.meta["merged_from"] = merged
        asr_meta = getattr(self.asr, "last_meta", None)
        if asr_meta:
            turn.trace.meta["asr"] = dict(asr_meta)  # SenseVoice 的情绪/事件标签和解码耗时
        if self.echo is not None and self.echo.engaged:  # 这句是在外放声音时说的: 记下回声处理的状态, 以后查得到
            st = self.echo.status()
            turn.trace.meta["echo"] = {k: st[k] for k in ("lag_ms", "coupled", "adapted", "erle_db", "gated", "echo_drops")}
        if self.mic_settings is not None:  # 这句是在哪种麦克风处理下听到的 (换处理前后对比用), 补了多少音量
            turn.trace.meta["mic"] = {**self.mic_settings, **({"gain_db": round(self.gain.gain_db, 1)} if self.gain else {})}

    def _on_mic_settings(self, msg: dict) -> None:
        """页面报来浏览器实际用上的处理. 自动增益关了 (服务端在 hello 里要的, 浏览器也照做了) 就在回声处理之后自己补音量;
        开着就不补, 免得两道自动增益互相拉. 只留认得的几个键和简单的值."""
        raw = msg.get("settings") if isinstance(msg.get("settings"), dict) else {}
        st = {}
        for key in ("echoCancellation", "noiseSuppression", "autoGainControl", "sampleRate", "channelCount"):
            v = raw.get(key)
            if isinstance(v, (bool, int, float)) or (key == "echoCancellation" and isinstance(v, str) and len(v) <= 16):
                st[key] = v
        self.mic_settings = st
        want = self.echo is not None and st.get("autoGainControl") is False and bool(self.e.echo_conf.get("post_gain", True))
        if not want:
            self.gain = None
        elif self.gain is None:  # 重开麦克风又报一次时接着用学到的, 不从头来
            self.gain = SpeechGain(self.e.echo_conf)
        log.info("麦克风: 回声消除=%s 降噪=%s 自动增益=%s%s", st.get("echoCancellation"), st.get("noiseSuppression"),
                 st.get("autoGainControl"), ", 回声处理之后自己补音量" if self.gain else "")

    def _can_barge_in(self, partial: str | None) -> bool:
        """partial 为 None 表示 VAD 判定开始说话 (没有文字可数)."""
        return (self.p.get("barge_in", True) and self.current is not None and not self.current.finalized
                and (partial is None or speakable_len(partial) >= self.p.get("barge_in_min_chars", 2)))

    def _select_room_scene(self, msg: dict, *, bind: bool = False) -> None:
        from .cognition import layout
        if "scene_id" in msg and not isinstance(msg["scene_id"], str):
            raise ValueError("场景编号无效")
        selected = layout.scene_id(msg.get("scene_id"))
        if selected != self.room_scene_id and not bind:
            raise ValueError("先观察当前场景，不能把旧场景结果记进新场景")
        registry = getattr(self.e, "room_registry", None)
        room = registry.get(selected) if registry is not None else (self.e.room if selected == layout.DEFAULT_SCENE_ID else None)
        if registry is None and selected != layout.DEFAULT_SCENE_ID:
            layout.load_scene(selected)  # Validate a trusted map even if cognition is off.
        if selected != self.room_scene_id:
            self.room_actions = None
            for pending in self.pending_room_actions.values():
                if not pending["future"].done():
                    pending["future"].set_result({"ok": False, "started": False, "completed": False, "reason": "scene_changed"})
            if self.room_task is not None:
                self.room_task.cancel()
            self.room_known_sent, self.room_seen, self.cog_note = False, False, None
            self.room_recent.clear()
        self.room_scene_id, self.room_active = selected, room

    async def on_message(self, msg: dict) -> None:
        kind = msg.get("type")
        scene_replies = {"room_observe": "room_observe_done", "room_search": "room_step", "room_known": "room_known",
                         "room_afford_done": "room_known", "room_carry": "room_carry_plan", "room_carry_done": "room_carry_saved",
                         "room_think": "room_plan"}
        if kind in scene_replies:
            try:
                self._select_room_scene(msg, bind=kind == "room_observe")
                if msg.get("scene_id") == "petal-oak":
                    msg = {**msg, "scene_id": self.room_scene_id}
            except (ValueError, OSError, TypeError):
                requested = msg.get("scene_id")
                if kind != "room_observe" or msg.get("id") is not None:
                    await self.send({"type": scene_replies[kind], "id": msg.get("id"), "ok": False, "reason": "scene",
                                     "scene_id": requested if isinstance(requested, str) and len(requested) <= 64 else None})
                return
        if kind == "room_capabilities":
            try:
                self._select_room_scene(msg, bind=True)
                self.room_actions = RA.capabilities(msg.get("activities"))
                await self.send({"type": "room_capabilities_done", "scene_id": self.room_scene_id, "ok": True})
            except (ValueError, OSError, TypeError):
                self.room_actions = {}
                await self.send({"type": "room_capabilities_done", "ok": False, "reason": "capabilities"})
            return
        if kind == "room_action_result":
            pending = self.pending_room_actions.get(str(msg.get("request_id", "")))
            if (pending and msg.get("scene_id") == pending["scene_id"] == self.room_scene_id
                    and msg.get("act") == pending["act"] and not pending["future"].done()):
                pending["future"].set_result(RA.terminal(msg))
            return
        if kind == "ping":
            await self.send({"type": "pong", "id": msg.get("id"), "t_client": msg.get("t_client"),
                             "t_server": now_ms()})
        elif kind == "text":
            text = str(msg.get("text", "")).strip()
            if text:
                self.carry = None
                await self._drop_spec("text_input")
                # task: 渠道转来的任务单 (channel_task) 变成的这一轮, 不再当成"主人在桌宠上说的话"重新分类
                # (否则"操作电脑"会一遍遍转给自己)
                await self.start_turn("text", text, now_ms(), heard=not msg.get("task"))
        elif kind == "interrupt":
            await self._drop_spec("user")
            await self.interrupt("user")
        elif kind == "watch_ctl":  # 页面上的"陪我看 / 不看了"按钮 (和说出来一样; source 是链接、BV 号或本机视频路径)
            action = "stop" if msg.get("action") == "stop" else "start"
            source = WA.find_source(str(msg.get("source") or "")) if action == "start" else None
            try:
                reply, _ = await self._watch_do(action, source, str(msg.get("source") or ""))
            except Exception as exc:  # noqa: BLE001
                log.warning("陪看出错: %s", exc)
                reply = "陪看这会儿出了点问题。"
            await self.send({"type": "watch_ctl_done", "action": action, "text": reply,
                             "active": self.watch is not None and self.watch.alive})
        elif kind == "mark":
            self._client_mark(msg)
        elif kind == "motion_eval":  # 前端每做完一个动作发来的执行评估 (成败、稳定裕度、关节越界、打滑、跳变抑制等)
            ev = {k: v for k, v in msg.items() if k != "type" and isinstance(v, (int, float, str, bool, type(None), list, dict))}
            self.tracelog.write({"time": datetime.now().isoformat(timespec="seconds"), **ev}, kind="motion-eval")
            turn = self.turns.get(msg.get("turn"))
            if turn is not None:
                turn.trace.meta.setdefault("exec", []).append({k: ev.get(k) for k in ("do", "to", "ok", "seconds")})
            # 执行结果回流: 没做成的动作, 下一轮的状态行里告诉模型 (固定词表拼成, 不用前端发来的文字)
            if msg.get("ok") is False:
                self.exec_note = I.exec_note(msg.get("do"), msg.get("to"))
        elif kind == "capabilities":  # 前端声明这个身体此刻能做哪些技能 (换模型、有无行动层时会变)
            skills = msg.get("skills")
            self.capabilities = {s for s in skills if s in I.SKILL_CUES} if isinstance(skills, list) else None
        elif kind == "output":  # 这条连接要不要语音 (下一轮起生效); audio: false 时只下发文字、意图和情绪
            self.audio = msg.get("audio") is not False
        elif kind == "mic_settings":  # 页面开麦克风以后报来的: 浏览器实际用上了哪些处理 (回声消除、降噪、自动增益)
            self._on_mic_settings(msg)
        elif kind == "state":
            self.char_state = I.describe_state(msg.get("state"))
            self.char_state_raw = msg.get("state") if self.char_state else None
            self.place = I.place_of(msg.get("state"))  # 每条 state 是完整快照, 不带 place 就是房间
        elif kind == "reset":
            await self._drop_spec("reset")
            await self.interrupt("reset")
            self.history.clear()
            await self.send({"type": "reset_done"})
        elif kind == "profile":  # 人设预设: 人设文字、模型 (本地或云端)、启用的 skill; 下一轮起生效
            await self._set_profile(msg)
        elif kind == "tools":  # 面板里启用的 MCP 工具 (加上 load_skill), 只交给云端模型
            try:
                self.tools, dropped = C.parse_tools(msg.get("tools"))
            except (C.ProfileError, TypeError, ValueError, AttributeError) as exc:  # 格式不对不能把连接弄断
                await self.send({"type": "tools_done", "count": len(self.tools), "error": str(exc), "active": self.cloud})
                return
            await self.send({"type": "tools_done", "count": len(self.tools), "dropped": dropped, "active": self.cloud})
        elif kind == "tool_result":  # 桌宠做完 (或用户拒绝了) 一个 tool_call; 不认识的、已经超时或被打断的不理
            fut = self.pending_tools.get(str(msg.get("call_id", "")))
            if fut is not None and not fut.done():
                fut.set_result({"content": C.result_text(msg.get("content"), self.cloud_cfg.get("tool_result_chars", 8000)),
                                "is_error": msg.get("is_error") is True})
        elif kind == "memory_candidates":  # 要一份候选记忆 (日志整理出来、等用户确认的)
            await self.send_candidates()
        elif kind == "memory_review":  # 确认 (ok: true) 或不要 (ok: false) 一条候选; 之后每条连接都会收到新的列表
            await self._review_memory(msg)
        elif kind == "sleep":  # 桌宠小屋: 她躺好闭眼了, 现在就整理记忆, 再清掉这条连接的短期上下文
            await self._sleep(msg)
        elif kind == "diary":  # 桌宠小屋: 翻开 / 合上日记本, 写日记
            await self._diary(msg)
        elif kind == "doc_drop":  # 桌宠小屋: 从桌面拖进来的文件, 存进库的 资料/
            await self._doc_drop(msg)
        elif kind == "room_event":  # 桌宠小屋里发生的事 (点她、投喂): 记进当天日志和日记, 按次数加亲近; 不进对话历史
            await self._room_event(msg)
        elif kind == "room_think":  # 桌宠小屋: 她接下来做什么 (room_plan.py), 后台生成, 不进对话历史
            await self._room_think(msg)
        elif kind == "room_observe":  # 桌宠小屋: 她这一刻看得到什么 (wm/cognition/live.py), 只进她的认知; 带 id 时回 room_observe_done
            await self._room_observe(msg)
        elif kind == "room_search":  # 桌宠小屋: 按她的认知, 找东西的下一步, 回 room_step
            await self._room_search(msg)
        elif kind == "room_carry":  # 桌宠小屋: 端起杯子之前问这一趟怎么端 (wm/cognition/carry.py), 回 room_carry_plan
            await self._room_carry(msg)
        elif kind == "room_carry_done":  # 桌宠小屋: 端完了, 这一趟感知到的记成经验, 回 room_carry_saved
            await self._room_carry_done(msg)
        elif kind == "room_afford_done":  # 桌宠小屋: 做完一件可供性, 记进她的发现簿, 回 room_known
            await self._room_afford_done(msg)
        elif kind == "room_known":  # 桌宠小屋: 要一份她现在的了解 (断线重连以后)
            await self._room_known(msg.get("id"))
        elif kind == "channel_config":  # 面板里的 QQ 设置 (整份快照, key 只留在内存); 访客会话没有这个入口
            if self.channels is not None:
                await self.send(await self.channels.configure(msg))
        elif kind == "channel_draft_review":  # 主人点了草稿的 发 / 改了再发 / 不发 (结果经 channel_draft_done 发给每条本人连接)
            if self.channels is not None and (r := await self.channels.review(msg)) is not None:
                await self.send(r)
        elif kind == "channel_pause":  # 一键全停 / 恢复
            if self.channels is not None:
                await self.send(await self.channels.pause(msg.get("paused") is not False))
        elif kind == "channel_status":
            await self.send_channels(always=True)
        elif kind == "channel_own":  # 让她现在做一次: 主动找你 / 自己做点事 / 看视频 / 写说说 (面板按钮、自检)
            if self.channels is not None:
                r = await self.channels.trigger(str(msg.get("action") or ""), topic=str(msg.get("topic") or "") or None,
                                                draft=msg.get("draft") is True)
                await self.send({"type": "channel_own_done", "action": msg.get("action"), **r})
        elif kind in ("channel_task_ack", "channel_task_progress", "channel_task_report"):
            # 桌宠页面回的电脑任务单 (接单 / 做到哪了 / 做完的结果和截图); 渠道管理器只认自己发出去、还没做完的那几张
            if self.channels is not None:
                await self.channels.task_event(msg)
        elif kind in ("channel_ask_open", "channel_ask_closed"):  # 页面上要主人点头的卡片(跑命令、动手): 同时在 QQ 上问主人 / 那边答了就作废
            if self.channels is not None:
                await (self.channels.ask_open(msg) if kind == "channel_ask_open" else self.channels.ask_closed(msg))

    async def send_channels(self, always: bool = False) -> None:
        """下行 channel_status 和还没处理的草稿. always=False (刚连上时) 渠道没配过、关着就不发."""
        ch = self.channels
        if ch is None or (not always and ch.conf is None and ch.state == "off"):
            return
        await self.send(ch.status())
        for d in ch.pending_drafts():
            await self.send(d)

    async def send_candidates(self, always: bool = True) -> None:
        """下行 memory_candidates. always=False (刚连上时) 没有候选就不发."""
        mem = self.e.memory
        if mem is None:
            return
        items = await asyncio.get_running_loop().run_in_executor(None, mem.candidates)
        if items or always:
            await self.send({"type": "memory_candidates", "items": items, "mode": "candidates" if mem.confirm else "auto"})

    def _push_candidates(self) -> None:
        if self._cand_task is None or self._cand_task.done():
            self._cand_task = asyncio.create_task(self.send_candidates())

    async def _review_memory(self, msg: dict) -> None:
        mem, cid = self.e.memory, str(msg.get("id", ""))
        if mem is None:
            await self.send({"type": "memory_review_done", "ok": False, "id": cid, "error": "没开长期记忆"})
            return
        r = await asyncio.get_running_loop().run_in_executor(None, mem.review, cid, msg.get("ok") is True)
        await self.send({"type": "memory_review_done", **r})

    async def _sleep(self, msg: dict) -> None:
        """上行 sleep {id, reason, chars}: 打断正在说的, 把没整理的日志现在就整理完 (digest.drain, 最多 memory.sleep_max_s 秒,
        每批发 sleep_progress), 清掉这条连接的历史 (同 reset), 醒来的第一轮算一次新的见面 (带关系概况). 回 sleep_done.
        没接本地模型时没有整理器 (digest: false), 只清历史."""
        sid, mem, dg = msg.get("id"), self.e.memory, self.e.digest
        t0 = now_ms()
        await self._drop_spec("sleep")
        await self.interrupt("sleep")
        res = {"batches": 0, "facts": 0, "complete": dg is None}
        if mem is not None and dg is not None:
            async def progress(n: int, facts: int) -> None:
                await self.send({"type": "sleep_progress", "id": sid, "done": n, "facts": facts})
            try:
                res = await dg.drain(float((self.cfg.get("memory") or {}).get("sleep_max_s", 60)), progress)
            except Exception as exc:  # 整理出错也要让她醒得来
                log.warning("睡觉时整理日志失败: %s", exc)
                res["error"] = f"{type(exc).__name__}: {exc}"
        self.history.clear()
        self._mem_session = False
        await self.send({"type": "sleep_done", "id": sid, "ok": True, "digest": dg is not None and mem is not None,
                         "facts": res["facts"], "batches": res["batches"], "complete": res["complete"],
                         "candidates": len(mem.candidates()) if mem is not None else 0,
                         "ms": round(now_ms() - t0), **({"error": res["error"]} if "error" in res else {})})

    async def _diary(self, msg: dict) -> None:
        """上行 diary {id, open, intent?, write?}. 发过 diary 的连接才门控 (桌宠进小屋时先发一条 open: false).
        合上 -> 回到门控 (memory.diary_gate), 回 diary_closed;
        翻开 -> 放行, 回 diary_recall {items} (按 intent 检索, 空就取最近值得回想的); write -> 写一段日记, 回 diary_written {text}.
        intent 只拿来检索, 不进提示词."""
        did, mem = msg.get("id"), self.e.memory
        self.diary_used = True
        if msg.get("open") is False:
            self.diary_open = False
            await self.send({"type": "diary_closed", "id": did, "ok": True})
            return
        if mem is None:
            await self.send({"type": "diary_written" if msg.get("write") else "diary_recall", "id": did, "ok": False,
                             "reason": "没开长期记忆", **({} if msg.get("write") else {"items": []})})
            return
        self.diary_open = True
        loop = asyncio.get_running_loop()
        if msg.get("write"):
            text = await loop.run_in_executor(None, mem.write_diary)
            await self.send({"type": "diary_written", "id": did, "ok": True, "text": text})
            return
        intent = str(msg.get("intent") or "")[:100]
        items = await loop.run_in_executor(None, mem.diary_recall, intent)
        await self.send({"type": "diary_recall", "id": did, "ok": True, "items": items})

    async def _doc_drop(self, msg: dict) -> None:
        """上行 doc_drop {id, name, kind: md|txt|image|pdf, text?} -> 存到 <库>/<角色>/资料/, 回 doc_saved {id, ok, path}.
        内容是外面来的文字, 只当数据存."""
        did, mem, kind = msg.get("id"), self.e.memory, msg.get("kind")
        text = msg.get("text") if isinstance(msg.get("text"), str) else None
        if mem is None or kind not in ("md", "txt", "image", "pdf"):
            await self.send({"type": "doc_saved", "id": did, "ok": False,
                             "reason": "没开长期记忆" if mem is None else "kind 要是 md / txt / image / pdf"})
            return
        path = await asyncio.get_running_loop().run_in_executor(None, mem.save_doc, str(msg.get("name") or ""), kind, text)
        await self.send({"type": "doc_saved", "id": did, "ok": True, "path": path, "chars": min(len(text or ""), 20000)})

    async def _room_event(self, msg: dict) -> None:
        """上行 room_event {kind: poke | feed, part: head | hand | body | item: drink | snack | milk | beans, count}: 小屋里点了她、投喂,
        页面同一种事 30 秒内合成一条. 只认 memory.ROOM_EVENTS 的固定词表, 页面的字符串不往下传: 记进当天日志 (日记从这里写
        "你摸了摸我的头"), 亲近分按次数加 (每天有上限), 下一轮的状态行说一句"用户刚才摸了摸你的头" (3 分钟内的). 回 room_event_done."""
        ev = M.room_event_of(msg)
        if ev is None:
            await self.send({"type": "room_event_done", "ok": False, "reason": "bad"})
            return
        kind, what, count = ev
        self.room_recent = [e for e in self.room_recent if now_ms() - e[0] < 180_000][-5:] + [(now_ms(), kind, what)]
        out = {"type": "room_event_done", "ok": False, "kind": kind, "part" if kind == "poke" else "item": what, "count": count}
        if self.e.memory is None:
            out["reason"] = "没开长期记忆"
        else:
            try:
                out.update(ok=True, **await asyncio.get_running_loop().run_in_executor(None, self.e.memory.room_event, *ev))
            except (OSError, ValueError) as exc:  # 库写不进去 (被占用、关系.md 被改坏) 也不能把连接弄断
                log.warning("记小屋里的事失败: %s", exc)
                out["reason"] = f"{type(exc).__name__}: {exc}"
        self.tracelog.write({"time": datetime.now().isoformat(timespec="seconds"),
                             **{k: v for k, v in out.items() if k != "type"}}, kind="room-event")
        await self.send(out)

    async def _room_think(self, msg: dict) -> None:
        """上行 room_think. 关着、场景表读不到 (off), 用户在说话或上一次还没回 (busy), 离上一次不到 min_interval_s (rate),
        请求里没有认识的候选 (bad) 时马上回 ok: false; 否则后台生成, 生成完回 room_plan. 页面收到 ok: false 就按本机规则挑."""
        rc = (self.cfg.get("room") or {}).get("think") or {}
        from .cognition import layout
        acts = (R.load_acts(rc.get("scene", R.DEFAULT_SCENE)) if self.room_scene_id == layout.DEFAULT_SCENE_ID
                else R.load_acts(layout.SCENE_PROFILES[self.room_scene_id], allow_legacy_afford=False))
        talking = self.listening or self.spec is not None or (self.current is not None and not self.current.finalized)
        mind = self.room_active.known() if self.room_active is not None else None  # 可供性的经验以服务端的发现簿为准 (wm/cognition/afford.py)
        req = R.parse_request(msg, acts, mind) if acts else None
        reason = ("off" if not rc.get("enabled", True) or not acts
                  else "busy" if talking or (self.room_task is not None and not self.room_task.done())
                  else "rate" if now_ms() - self.room_at < float(rc.get("min_interval_s", 150)) * 1000
                  else "bad" if req is None else None)
        if reason:
            await self.send({"type": "room_plan", "id": msg.get("id"), "ok": False, "reason": reason})
            return
        self.room_at = now_ms()
        self.room_task = asyncio.create_task(self._room_plan(msg.get("id"), req, acts, rc, self.room_scene_id))

    async def _room_observe(self, msg: dict) -> None:
        """上行 room_observe {at, seen: {放东西的地方: [东西]}, hand, open, events?, act?, result?, regions?, id?}: 她这一刻看得到的.
        只认 wm/cognition/layout.py 的编号, 页面的字符串不往下传. 新发现 (不在原处了、在哪找到) 下一轮状态行里说一次."""
        room = self.room_active
        if room is None:
            if msg.get("id") is not None:
                await self.send({"type": "room_observe_done", "id": msg.get("id"), "ok": False, "reason": "off"})
            return
        try:
            res = await asyncio.get_running_loop().run_in_executor(None, room.observe, msg)
        except OSError as exc:  # 事件流写不进去也不能把连接弄断
            log.warning("小窝认知记不下这份观察: %s", exc)
            res = None
        if res is not None:
            if msg.get("affords") is not None and not self.room_known_sent:  # 页面声明了可供性: 把她的了解发一份 (页面按它挑下一件事)
                self.room_known_sent = True
                await self._room_known(None)
            self.room_seen = True
            if res["notes"]:
                self.cog_note = (now_ms(), res["notes"][-1])
            if res["learned"]:
                self.tracelog.write({"time": datetime.now().isoformat(timespec="seconds"), "learned": res["learned"]},
                                    kind="room-cognition")
        if msg.get("id") is not None:
            await self.send({"type": "room_observe_done", "id": msg.get("id"), "ok": res is not None,
                             **({"changes": [list(c) for c in res["changes"]], "learned": res["learned"]} if res else {"reason": "bad"})})

    async def _room_search(self, msg: dict) -> None:
        """上行 room_search {id, item, new?}: 按她的认知找 item 的下一步, 回 room_step {id, ok, item, action, say, knows}.
        action: go 处 / open 冰箱 / close / look / pick 东西 / ask 东西 (问你) / give_up 原因. 页面做完那一步再报一份 room_observe."""
        room, item = self.room_active, msg.get("item")
        if room is None:
            await self.send({"type": "room_step", "id": msg.get("id"), "ok": False, "reason": "off"})
            return
        res = await asyncio.get_running_loop().run_in_executor(None, room.search, item, msg.get("new") is True)
        await self.send({"type": "room_step", "id": msg.get("id"), "ok": res["action"] != ["give_up", "不认识"], "item": item, **res})

    async def _room_known(self, rid) -> None:
        """下行 room_known {id?, ok, things, states, spills, tries, skips, likes, visits, high, stuck}: 她对小窝的了解
        (wm/cognition/afford.py; 时刻是"多久以前"的秒数)."""
        room = self.room_active
        if room is None:
            await self.send({"type": "room_known", "id": rid, "ok": False, "reason": "off"})
            return
        state = await asyncio.get_running_loop().run_in_executor(None, room.known_state)
        await self.send({"type": "room_known", "id": rid, "ok": True, **state})

    async def _room_afford_done(self, msg: dict) -> None:
        """上行 room_afford_done {id, act, ctx, ok, felt, hurt, skip, node?, liked?, high?, stuck?, forget?}: 做完一件可供性.
        记进发现簿, 回 room_known (带 id; 不认识的事 ok: false)."""
        room = self.room_active
        res = None
        if room is not None:
            try:
                res = await asyncio.get_running_loop().run_in_executor(None, room.afford_done, msg)
            except OSError as exc:
                log.warning("小窝认知记不下这一件: %s", exc)
        if res is None:
            await self.send({"type": "room_known", "id": msg.get("id"), "ok": False, "reason": "off" if room is None else "bad"})
            return
        await self._room_known(msg.get("id"))

    async def _room_carry(self, msg: dict) -> None:
        """上行 room_carry {id, cup, fill?, hot, route?, source?, wait_s?}: 端起杯子之前, 按她端东西的经验挑这一趟怎么端.
        回 room_carry_plan {id, ok, look} (先看一眼杯口再来) 或 {id, ok, choice: {hand, care, settle, wait, fill}, why, say}."""
        room = self.room_active
        if room is None:
            await self.send({"type": "room_carry_plan", "id": msg.get("id"), "ok": False, "reason": "off"})
            return
        res = await asyncio.get_running_loop().run_in_executor(None, room.carry_plan, msg)
        await self.send({"type": "room_carry_plan", "id": msg.get("id"), "ok": res is not None, **(res or {"reason": "bad"})})

    async def _room_carry_done(self, msg: dict) -> None:
        """上行 room_carry_done {id, delivered?, waited?}: 端完了. 这一趟里 room_observe 报来的 (洒了、烫了) 变成一条经验;
        感知到的下一轮状态行说一次. 回 room_carry_saved {id, ok, spilled}."""
        room = self.room_active
        res = None
        if room is not None:
            try:
                res = await asyncio.get_running_loop().run_in_executor(None, room.carry_done, msg)
            except OSError as exc:
                log.warning("小窝认知记不下这一趟端东西: %s", exc)
        if res and res["note"]:
            self.cog_note = (now_ms(), res["note"])
        if res and res["learned"]:
            self.tracelog.write({"time": datetime.now().isoformat(timespec="seconds"), "learned": res["learned"]}, kind="room-cognition")
        await self.send({"type": "room_carry_saved", "id": msg.get("id"), "ok": res is not None,
                         **({"spilled": res["spilled"]} if res else {"reason": "off" if room is None else "no_trip"})})

    async def _room_plan(self, rid, req: dict, acts: dict, rc: dict, scene_id=None) -> None:
        """用这条连接的模型想一下 (人设预设的口吻); 本地模型用单独的槽位 (room.think.id_slot), 不冲掉对话前缀的缓存.
        云端没有约束解码, 输出里找 JSON 再按候选检查. 只记进 logs/room-plan-日期.jsonl (编号、数字和输出), 不进历史和轮次 trace."""
        llm, t0 = self.llm, now_ms()
        engine = "cloud" if self.cloud else llm.name
        out, raw = {"type": "room_plan", "id": rid, "ok": False, "scene_id": scene_id or self.room_scene_id}, ""
        try:
            if engine == "mock":
                await asyncio.sleep(float(rc.get("mock_ms", 150)) / 1000)
                plan = R.mock_plan(req, acts)
            else:
                req = R.shortlist(req, acts, int(rc.get("max_options", 4)))
                msgs = C.with_profile(R.build_messages(req, acts, persona=bool(self.persona)), self.persona)
                ask = (llm.complete(msgs, max_tokens=int(rc.get("max_tokens", 60)), temperature=rc.get("temperature", 0.7))
                       if self.cloud else R.ask_local(llm, msgs, req["options"], rc))
                raw = await asyncio.wait_for(ask, float(rc.get("timeout_s", 8)))
                plan = R.parse_output(raw, req, acts)
            if plan:
                out.update(ok=True, **plan)
            else:
                out["reason"] = "bad_output"
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            out["reason"] = "timeout"
        except Exception as exc:  # 连不上模型、云端出错: 页面照本机规则, 连接不能断
            log.warning("小屋想下一件事失败: %s: %s", type(exc).__name__, exc)
            out["reason"] = "error"
        out.update(ms=round(now_ms() - t0), engine=engine)
        await self.send(out)
        self.tracelog.write({"time": datetime.now().isoformat(timespec="seconds"), "engine": engine,
                             "label": getattr(llm, "label", ""), "scene_id": out["scene_id"], "persona": len(self.persona), "request": req,
                             "output": raw[:300], **{k: out.get(k) for k in ("ok", "reason", "pick", "thought", "how", "ms")}},
                            kind="room-plan")

    async def _set_profile(self, msg: dict) -> None:
        """上行 profile (整份快照, 没写的字段回到默认). 填得不对就不换, 回 ok: false 和原因. key 只留在内存里."""
        try:
            p = C.parse_profile(msg, self.cloud_cfg.get("persona_chars", 2000))
        except (C.ProfileError, TypeError, ValueError, AttributeError) as exc:  # 格式不对不能把连接弄断
            await self.send({"type": "profile_done", "ok": False, "error": str(exc)})
            return
        spec = p["model"]
        if spec != self.cloud_spec:  # 同一个云端模型就留着原来的引擎 (连接复用, 记得对方不认的参数)
            if self.cloud:
                self._retired.append(self.llm)
            self.llm = self.e.llm if spec is None else C.CloudLLM(
                spec, {"temperature": self.cfg["llm"].get("temperature", 0.3), **self.cloud_cfg})
            self.cloud_spec = spec
        self.persona, self.skills = p["persona"], p["skills"]
        self.profile_meta = {"engine": "cloud" if spec else "local", "provider": spec and spec["provider"],
                             "model": spec and spec["model"], "persona": len(self.persona), "skills": len(self.skills),
                             "local": C.is_local(spec)}
        if C.is_local(spec):  # 本机模型: 先问一下服务 (连不上、上下文太短就在面板上说; 服务没开时最多等几秒)
            try:
                info = await asyncio.wait_for(self.llm.probe(), 6)
                p["warnings"] += C.probe_notes(info, spec["base_url"])
                self.profile_meta.update(server=info.get("server"), ctx=info.get("ctx"))
            except asyncio.TimeoutError:
                p["warnings"].append("本机模型的服务没回话 (6 s): 开了吗?")
        await self.send({"type": "profile_done", "ok": True, "llm": C.public_model(spec), "persona": len(self.persona),
                         "skills": [s["name"] for s in self.skills], "tools": len(self.tools) if self.cloud else 0,
                         "warnings": p["warnings"]})

    def _client_mark(self, msg: dict) -> None:
        turn = self.turns.get(msg.get("turn"))
        name = msg.get("name")
        if turn is None or turn.finalized or name not in CLIENT_MARKS:
            return
        try:
            turn.trace.mark(name, float(msg["t"]))
        except (KeyError, TypeError, ValueError):
            return
        if name == "client_audio_end":
            turn.client_done.set()

    # -- 投机执行 -----------------------------------------------------------
    async def _maybe_speculate(self, t: float, immediate: bool = False) -> None:
        """immediate: ASR 已经判定是停顿 (pause 事件), 不用再等部分结果稳定."""
        text = self.partial
        if (not self.p.get("speculate", True) or not text
                or (self.spec is not None and self.spec.basis == text)
                or speakable_len(text) < self.p.get("spec_min_chars", 2)
                or (not immediate and t - self.partial_since < self.p.get("spec_stable_ms", 150))
                or (self.current is not None and not self.current.finalized)):
            return
        await self._drop_spec("partial_changed")
        c = self.carry
        prefix = c["text"] if c and t - c["speech_end"] <= self.p.get("merge_window_s", 3.0) * 1000 else ""
        turn = self._new_turn("voice", prefix + text, speculative=True)
        turn.basis = text
        turn.trace.mark("spec_start")
        self.spec = turn
        self.spec_stats["started"] += 1
        await self.send({"type": "spec", "state": "start", "turn": turn.id, "text": turn.text})
        turn.task = asyncio.create_task(self._run_turn(turn))

    async def _drop_spec(self, reason: str) -> None:
        turn, self.spec = self.spec, None
        if turn is None:
            return
        if turn.task and not turn.task.done():
            turn.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await turn.task
        self.spec_stats["cancelled"] += 1
        await self.send({"type": "spec", "state": "cancel", "turn": turn.id, "reason": reason})

    async def _commit_spec(self, speech_end: float, speech_start: float | None) -> Turn:
        turn, self.spec = self.spec, None
        tr = turn.trace
        if speech_start is not None and speech_start <= speech_end:
            tr.mark("speech_start", speech_start, overwrite=True)
        tr.mark("speech_end", speech_end, overwrite=True)
        tr.mark("asr_final")
        tr.mark("spec_commit")
        commit_t = tr.marks["spec_commit"]
        early = {}
        for name in DELIVERY_MARKS:
            if name in tr.marks and tr.marks[name] < commit_t:
                early[name] = round(tr.marks[name] - commit_t, 1)
                tr.marks[name] = commit_t
        tr.meta["speculative"] = {"basis": turn.basis, "ready_before_commit_ms": early}
        self.current = turn
        self.turns[turn.id] = turn
        self.spec_stats["committed"] += 1
        buffered, turn.buffer = turn.buffer, None
        await self.send({"type": "spec", "state": "commit", "turn": turn.id})
        for msg in buffered:
            await self.send(msg)
        if turn.pending_history:
            self._remember(*turn.pending_history)
        turn.committed.set()
        self._heard(turn.text)
        return turn

    def _heard(self, text: str) -> None:
        """你在桌宠上说的话: 让她去做事 (看视频、发说说、写工具、学本事…) 的, QQ 渠道那边照做 (后台看, 不耽误这一轮).
        带上最近两轮 ("去吧去吧"认得出是接着前面说的)."""
        if self.channels is None or not text or (self.room_actions is not None and RA.command(text)):
            return  # A virtual-room command must not also become a real desktop/channel task.
        if self._watch_cmd(text) is not None:
            return  # 陪看的话 ("陪我看"+链接、"不看了"、"看到几分了") 在这里办, 不再转给渠道那边当成"去看这个视频"
        context = []
        for said, raw in self.history[-2:]:
            try:
                reply = str((json.loads(raw) or {}).get("reply") or "")
            except (ValueError, AttributeError):
                reply = ""
            context += [f"[主人] {said}"] + ([f"[她] {reply}"] if reply else [])
        asyncio.ensure_future(self.channels.heard(text, "桌宠上", True, context=context))

    # -- 轮次 ---------------------------------------------------------------
    def _new_turn(self, source: str, text: str, speculative: bool = False) -> Turn:
        self.turn_seq += 1
        trace = TurnTrace(self.turn_seq, source, text)
        trace.meta["engines"] = {"llm": self.llm.label, "asr": self.e.asr.name,
                                 "tts": self.e.tts.name, "rvc": self.e.rvc.name, "slot": self.slot}
        if self.profile_meta:
            trace.meta["profile"] = dict(self.profile_meta)
        turn = Turn(self.turn_seq, source, text, trace)
        turn.state, turn.state_raw, turn.place = self.char_state, self.char_state_raw, self.place
        turn.audio = self.audio
        turn.llm = self.llm
        if turn.audio:
            turn.hum = H.request(text)  # "哼两句""唱小星星""跟着这个调子: 1 1 5 5..." -> 说完再哼一段
        if not turn.audio:
            trace.meta["audio"] = False
        if turn.hum is not None:  # 程序会接一段哼唱, 别让她说"不会唱"
            turn.state = I.add_note(turn.state, f"主人让你哼一段《{turn.hum.name}》, 说完程序会哼; 自然答应就好")
        if self.exec_note:
            turn.state, self.exec_note = I.add_note(turn.state, self.exec_note), None
        if self.room_recent:  # 3 分钟内小屋里的事 (固定说法): "用户刚才摸了摸你的头"
            recent = [(k, w) for t, k, w in self.room_recent if now_ms() - t < 180_000]
            turn.state, self.room_recent = I.add_note(turn.state, M.room_note(recent)), []
        if self.cog_note:  # 10 分钟内她在小屋里的发现 (按词表拼的): "你刚在电脑桌上找到了粉色浓缩咖啡杯 (原来以为在咖啡机上)"
            if now_ms() - self.cog_note[0] < 600_000:
                turn.state = I.add_note(turn.state, self.cog_note[1])
            self.cog_note = None
        if self.watch is not None and self.watch.alive:  # 陪看时她自己的事: 在陪你看什么、此刻在等进度 / 跟到哪 (WA.WatchAlong.note)
            turn.state = I.add_note(turn.state, self.watch.note())
        elif turn.llm is not self.e.llm and self._screen_hint():  # 你在看视频 / 直播 / 玩游戏: 让她知道自己能看一眼
            turn.state = I.add_note(turn.state, "你能看一眼他的屏幕 (只看前台那个窗口): 他问你看不看得到、让你看看时就先看一眼再说;"
                                                " 他说\"陪我看\"你就一直陪着看")
        if turn.state:
            trace.meta["state"] = turn.state
        if speculative:
            turn.buffer = []
        else:
            turn.committed.set()
        return turn

    def _touch(self) -> None:
        """用户开口或打字: 空闲时的日志整理让出显卡."""
        if self.e.digest is not None:
            self.e.digest.touch()

    async def start_turn(self, source: str, text: str, speech_end: float, speech_start: float | None = None,
                         heard: bool = True) -> Turn:
        self._touch()
        if self.current and not self.current.finalized:
            await self.interrupt("new_turn")
        turn = self._new_turn(source, text)
        if heard:
            self._heard(text)
        if speech_start is not None and speech_start <= speech_end:
            turn.trace.mark("speech_start", speech_start)
        turn.trace.mark("speech_end", speech_end)
        turn.trace.mark("asr_final")
        self.current = turn
        self.turns[turn.id] = turn
        turn.task = asyncio.create_task(self._run_turn(turn))
        return turn

    async def interrupt(self, reason: str) -> None:
        turn = self.current
        if turn is None or turn.finalized:
            return
        turn.trace.interrupted = True
        turn.trace.meta["interrupt_reason"] = reason
        if (reason in ("barge_in", "new_turn") and turn.source == "voice"
                and not turn.trace.has("client_audio_start")):
            self.carry = {"text": turn.text, "speech_end": turn.trace.marks["speech_end"],
                          "speech_start": turn.trace.marks.get("speech_start")}
        if turn.task and not turn.task.done():
            turn.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await turn.task
        await self.send({"type": "interrupted", "turn": turn.id, "reason": reason})
        await self._finalize(turn)

    async def _run_turn(self, turn: Turn) -> None:
        tr = turn.trace
        queue: asyncio.Queue = asyncio.Queue()
        tts_task = asyncio.create_task(self._tts_worker(turn, queue))
        try:
            await self._tsend(turn, {"type": "turn_start", "turn": turn.id, "source": turn.source, "text": turn.text})
            # 起势动作不等 LLM: 用户感知到的"第一反应"在这里.
            await self._tsend(turn, {"type": "backchannel", "turn": turn.id, "motion": "think"})
            tr.mark("backchannel_sent")
            try:
                if turn.aside is not None:  # 程序发起的一句 (陪看): 现成的话, 不问模型
                    await self._run_aside(turn, queue)
                else:
                    await self._run_llm(turn, queue)
            finally:
                queue.put_nowait(None)
            await tts_task
            tr.mark("tts_done")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, C.CloudError):  # 错误信息已经打过码, 说清楚了原因, 不用堆栈
                log.warning("第 %d 轮: 云端模型出错 (%s): %s", turn.id, exc.kind, exc)
            elif isinstance(exc, aiohttp.ClientConnectionError):
                log.warning("第 %d 轮: 连不上外部服务: %s", turn.id, exc)
            else:
                log.exception("第 %d 轮出错", turn.id)
            tr.meta["error"] = f"{type(exc).__name__}: {exc}"
            if not tr.has("intent_ready"):  # 让前端退出思考姿态
                await self._tsend(turn, {"type": "intent", "turn": turn.id, "op": "chat", "acts": [], "fallback": True})
            # 额度用完 (wm/ledger.py) 的那句是写给人看的, 不带异常名
            budget = isinstance(exc, C.CloudError) and exc.kind == "budget"
            err = {"type": "error", "turn": turn.id, "message": str(exc) if budget else tr.meta["error"]}
            if isinstance(exc, C.CloudError):
                err["kind"] = tr.meta["error_kind"] = exc.kind  # connect / auth / rate_limit / timeout ... 面板据此提示
            await self._tsend(turn, err)
        finally:
            if not tts_task.done():
                tts_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await tts_task
        await turn.committed.wait()  # 投机轮做完了也要等确认, 不确认就会被取消
        await self._tsend(turn, {"type": "turn_server_done", "turn": turn.id, "segments": turn.audio_segments})
        if turn.audio_segments:
            # 长回复的声音要播一两分钟, 合成却早就完了: 时限加上已下发的音频时长, 播放期间照样能插话打断
            timeout = self.p.get("client_done_timeout_s", 20) + (turn.audio_s if turn.long_ran else 0)
            try:
                await asyncio.wait_for(turn.client_done.wait(), timeout)
            except asyncio.TimeoutError:
                tr.meta["client_timeout"] = True
        else:
            await asyncio.sleep(0.3)  # 给前端的 client_intent 打点留一点时间
        await self._finalize(turn)

    async def _run_room_command(self, turn: Turn, queue: asyncio.Queue) -> bool:
        # No capability snapshot means an older/non-room client: leave its protocol alone.
        act = RA.command(turn.text) if self.room_actions is not None else None
        if not act:
            return False
        scene = self.room_scene_id
        await turn.committed.wait()  # ASR speculation must never start a real activity.
        turn.op, turn.emo = "chat", "neutral"
        turn.trace.mark("intent_ready")
        await self._send_intent(turn, {"type": "intent", "turn": turn.id, "op": "chat", "acts": [], "room_action": act})
        if scene != self.room_scene_id:
            result = {"ok": False, "reason": "scene_changed"}
        elif not (self.room_actions or {}).get(act, False):
            result = {"ok": False, "reason": "unsupported"}
        else:
            self.room_action_seq += 1
            rid = f"room.{turn.id}.{self.room_action_seq}"
            timeout = RA.timeout_seconds((self.cfg.get("room") or {}).get("action_timeout_s", 180))
            future = asyncio.get_running_loop().create_future()
            self.pending_room_actions[rid] = {"future": future, "act": act, "scene_id": scene}
            cancel_reason = None
            try:
                await self._speak(turn, f"我来尝试{RA.NAMES[act]}，等小屋回报结果。", queue)
                await self.send({"type": "room_action", "request_id": rid, "turn": turn.id,
                                 "scene_id": scene, "act": act, "timeout_ms": int(timeout * 1000)})
                result = await asyncio.wait_for(future, timeout)
                if result.get("reason") == "scene_changed":
                    cancel_reason = "scene_changed"
            except asyncio.TimeoutError:
                result = {"ok": False, "reason": "timeout"}
                cancel_reason = "timeout"
            except asyncio.CancelledError:
                cancel_reason = "cancelled"
                raise
            finally:
                self.pending_room_actions.pop(rid, None)
                if cancel_reason:
                    with contextlib.suppress(Exception):
                        await self.send({"type": "room_action_cancel", "request_id": rid, "scene_id": scene,
                                         "reason": cancel_reason})
        result = RA.terminal(result)
        turn.trace.meta["room_action"] = {"act": act, "scene_id": scene, **result}
        await self._speak(turn, RA.reply(act, result), queue)
        turn.raw_output = I.dumps_compact({"op": "chat", "acts": [], "emo": "neutral", "reply": turn.reply_text})
        self._remember(turn.text, turn.raw_output)
        return True

    # -- 陪看 (wm/watch_along.py) ---------------------------------------------------------------------------
    async def aside(self, note: str, reply: str, emo: str = "neutral") -> bool:
        """程序发起的一句 (陪看): 不抢话 —— 你正在说话、她这一轮还没说完、在投机识别时不说, 返回 False (陪看那边过一会儿再试).
        说的话走同一套合成和下发; 这一轮的 text 是空的 (对话栏不显示成"你说的"), 历史里记 note (提醒) 和这句."""
        if not reply or self._aside_blocked() is not None:
            return False
        turn = self._new_turn("watch", "")
        turn.aside = I.dumps_compact({"op": "chat", "acts": [], "emo": emo if emo in I.EMOTIONS else "neutral",
                                      "reply": reply})
        turn.note = note
        self.current = turn
        self.turns[turn.id] = turn
        turn.task = asyncio.create_task(self._run_turn(turn))
        return True

    def _aside_blocked(self) -> str | None:
        """陪看那句这会儿为什么不能说 (陪看日志里记): listening 麦克风那边在听 (你在说话, 外放时也可能是视频的声音);
        speculating 正在边听边猜你要说什么; busy 她这一轮还没说完. 能说就 None."""
        if self.listening:
            return "listening"
        if self.spec is not None:
            return "speculating"
        if self.current is not None and not self.current.finalized:
            return "busy"
        return None

    async def _run_aside(self, turn: Turn, queue: asyncio.Queue) -> None:
        """现成的一句: 意图 (聊天、没有动作) 和情绪照常下发, 分句送去合成. 不过回复侧检查 (这句不是答应你做什么)."""
        obj = json.loads(turn.aside)
        turn.op, turn.emo = "chat", obj.get("emo") or "neutral"
        turn.trace.meta["aside"] = turn.note[:160]
        turn.trace.mark("intent_ready")
        await self._send_intent(turn, {"type": "intent", "turn": turn.id, "op": "chat", "acts": []})
        turn.trace.mark("emotion_ready")
        await self._tsend(turn, {"type": "emotion", "turn": turn.id, "emo": turn.emo, "vad": list(I.emotion_vad(turn.emo))})
        splitter = ClauseSplitter(self.p["first_clause_min_chars"], self.p["clause_min_chars"], self.p["clause_max_chars"])
        for clause in [*splitter.feed(str(obj.get("reply") or "")), *splitter.flush()]:
            await self._speak(turn, clause, queue)
        turn.raw_output = turn.aside
        self._remember(turn.note, turn.aside)

    async def _run_watch_command(self, turn: Turn, queue: asyncio.Queue) -> bool:
        """你这句是不是在说陪看 ("陪我看"+链接、"不看了"、"看到 3 分 20 了"、"暂停一下"、"继续"): 是就在这里回一句, 不问模型."""
        if turn.source not in ("voice", "text"):
            return False
        cmd = self._watch_cmd(turn.text)
        if cmd is None:
            return False
        await turn.committed.wait()  # 投机识别的不算: 端点确认了才动
        kind, arg = cmd
        try:
            reply, emo = await self._watch_do(kind, arg, turn.text)
        except Exception as exc:  # noqa: BLE001 (陪看出错照样回一句, 不把这一轮弄坏)
            log.warning("陪看出错: %s", exc)
            reply, emo = "陪看这会儿出了点问题，先不看了。", "sad"
        turn.trace.meta["watch"] = {"cmd": kind}
        turn.op, turn.emo = "chat", emo
        turn.trace.mark("intent_ready")
        await self._send_intent(turn, {"type": "intent", "turn": turn.id, "op": "chat", "acts": []})
        await self._tsend(turn, {"type": "emotion", "turn": turn.id, "emo": emo, "vad": list(I.emotion_vad(emo))})
        await self._speak(turn, reply, queue)
        turn.raw_output = I.dumps_compact({"op": "chat", "acts": [], "emo": emo, "reply": reply})
        self._remember(turn.text, turn.raw_output)
        return True

    def _watch_cmd(self, text: str) -> tuple[str, object] | None:
        """这句话是不是陪看的事 (WA.command), 按此刻在不在陪看、在不在等链接、宿主报不报你在看视频."""
        if not self.watch_conf.get("enabled", True):
            return None
        active = self.watch is not None and self.watch.alive
        waiting = not active and time.monotonic() < self.watch_wait
        act = self.char_state_raw.get("activity") if isinstance(self.char_state_raw, dict) else None
        return WA.command(text, active=active, waiting=waiting,
                          video_now=isinstance(act, dict) and act.get("category") == "video",
                          asking=active and self.watch.state == "need_cursor", paused=active and self.watch.state == "paused")

    def _watch_registry(self):
        """她工作台的扩展登记 (经渠道管理器挂着的工作台); 没有工作台、她的 video-sense 没开或版本校验没过, 返回 None."""
        hub = getattr(self.channels, "agent_hub", None) if self.channels is not None else None
        reg = getattr(hub, "extensions", None)
        if reg is None:
            return None
        try:
            reg.server(self.watch_conf["extension"], credentials=False)
        except (ValueError, OSError, KeyError, TypeError):
            return None
        return reg

    async def _watch_do(self, kind: str, arg, text: str) -> tuple[str, str]:
        w = self.watch if self.watch is not None and self.watch.alive else None
        if kind == "stop":
            self.watch_wait = 0.0
            if w is not None:
                await w.stop("user")
            return "好，不看了。", "neutral"
        if kind == "cursor" and isinstance(w, SA.ScreenAlong):  # 看着你的屏幕: 不用报进度
            return "好～我看着呢。", "happy"
        if kind == "cursor" and w is not None:
            w.set_cursor(float(arg))
            return f"好，从 {WA.clock(float(arg))} 跟上。", "happy"
        if kind == "not_started" and w is not None:  # 她问了看到几分, 你说还没开始: 停在 0:00 等你
            w.hold_at_start()
            return "好，那我等你～点开始了跟我说一声。", "happy"
        if kind == "pause" and w is not None:
            w.pause("user")
            return "好，等你。", "neutral"
        if kind == "resume" and w is not None:
            at_start = (w.position() or 0.0) < 1
            w.resume()
            return ("好～从头跟上。" if at_start else "好～"), "happy"
        if kind in ("cursor", "pause", "resume", "not_started"):  # 刚好看完、停了
            return "我们现在没在一起看呀～想看就把链接发我。", "neutral"
        reg = self._watch_registry()
        if reg is None:
            return "我陪你看视频的本事这会儿用不了（工作台里我做的 video-sense 扩展没开），先陪你聊天吧。", "sad"
        if not arg:  # 说了陪我看, 没给链接: 看你的屏幕 (2026-11-08 用户: "她自己就陪我看屏幕"); 看不了才问你要链接
            if self._screen_ok():
                return await self._screen_start(w)
            self.watch_wait = time.monotonic() + float(self.watch_conf["need_source_s"])
            return "好呀～你在看哪个？把链接发我（B 站点分享→复制链接），本机的视频就把文件路径给我。", "happy"
        source = str(arg)
        if WA.SHORT.fullmatch(source):
            source = await WA.resolve_short(source) or ""
            if not source:
                return "这个短链接我没打开……把视频页面的地址或者 BV 号发我吧。", "sad"
        if w is not None:
            await w.stop("switch")
        self.watch_wait = 0.0
        home = Path(self.watch_conf.get("workdir") or (Path.home() / ".worldmodel" / "watch"))
        work = home / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        tool = WA.HerTool(reg, work, self.watch_conf["extension"], timeout=float(self.watch_conf["tool_timeout_s"]))
        script = home / "player.ps1"
        self.watch = WA.WatchAlong(self.watch_conf, source, tool=tool.call, speak=self.aside, think=self._watch_think,
                                   player=(lambda: WA.read_player(script)) if self.watch_conf.get("player_check") else None,
                                   workdir=work, on_end=self._watch_end, title_of=self._watch_title,
                                   trace=self._watch_trace, on_ready=self._watch_ready, blocked=self._aside_blocked)
        self._watch_trace({"event": "start", "source": source})
        cursor = WA.parse_cursor(text)
        held = WA.not_started(text)
        if held:  # "陪我看 BV… 我还没开始": 停在 0:00 等你说开始
            self.watch.hold_at_start()
        elif cursor is not None:  # "陪我看 BV… 我看到 3 分了"
            self.watch.set_cursor(cursor)
        with contextlib.suppress(Exception):  # 先告诉页面开始了, 再起: 知道标题后那条 (_watch_ready) 不会被这条盖掉
            await self.send({"type": "watch_state", "active": True, "source": source})
        self.watch.start()
        if held:
            return "好～我先把视频打开，你点开始了跟我说一声；快看完一段的时候我说一句，不想听了就说“不看了”。", "happy"
        return "好～我先把视频打开，跟着你的进度看，快看完一段的时候说一句；不想听了就说“不看了”。", "happy"

    def _watch_llm(self):
        """陪看用的模型: 这条连接选的; 选的是内置小模型时借外部渠道的云端模型 (同一个她)."""
        if not self.cloud and self.channels is not None and getattr(self.channels, "cloud", None) is not None:
            return self.channels.cloud
        return self.llm

    def _screen_conf(self) -> dict:
        return {**SA.DEFAULTS, **(self.watch_conf.get("screen") or {})}

    def _screen_ok(self) -> bool:
        """能不能看你的屏幕: 屏幕陪看开着 (watch_along.screen.enabled)、在 Windows 上、模型看得了图."""
        return bool(self._screen_conf().get("enabled")) and sys.platform == "win32" and \
            WA.can_see(self._watch_llm(), self.watch_conf.get("vision", "auto"))

    async def _screen_start(self, w) -> tuple[str, str]:
        """不给链接: 开始看你的屏幕 (wm/screen_along.py). 已经在陪看的先停掉."""
        if w is not None:
            await w.stop("switch")
        self.watch_wait = 0.0
        self.watch = SA.ScreenAlong(self._screen_conf(), speak=self.aside, think=self._screen_think,
                                    look=SA.foreground, grabber=SA.grab,  # 按模块里的名字取 (测试换成假的)
                                    on_end=self._watch_end, on_change=self._screen_change, trace=self._watch_trace,
                                    blocked=self._aside_blocked)
        self._watch_trace({"event": "start", "mode": "screen"})
        with contextlib.suppress(Exception):  # 页面上一直有"她在看你的屏幕"
            await self.send({"type": "watch_state", "active": True, "mode": "screen", "source": "screen"})
        self.watch.start()
        return "好呀，我陪你看～我只看你前台那个窗口，聊天、密码这些我不看；不想让我看了就说“不看了”。", "happy"

    async def _screen_change(self, info: dict) -> None:
        """屏幕陪看: 你换了窗口 / 刷到下一条 (标题变了), 告诉页面现在看的是什么."""
        await self.send({"type": "watch_state", "active": True, "mode": "screen", "source": "screen",
                         "title": info.get("title") or "", "app": info.get("app") or ""})

    async def _screen_think(self, ctx: dict) -> tuple[str, str, str]:
        """屏幕陪看的一眼交给模型: 想说就说一句、记一句看到了什么. 按桌宠的额度记."""
        llm = self._watch_llm()
        meta: dict = {}
        with L.charging("pet"):
            out = [t async for t in llm.stream(C.with_profile(SA.look_messages(ctx), self.persona, None),
                                               schema=SA.LOOK_SCHEMA, max_tokens=int(self._screen_conf()["max_tokens"]),
                                               id_slot=self.slot, meta=meta)]
        return SA.parse_look("".join(out))

    async def _watch_think(self, ctx: dict) -> tuple[str, str]:
        """陪看的一段交给模型说一句: 用这条连接选的模型; 选的是内置小模型时借外部渠道的云端模型 (同一个她), 都没有就只给台词.
        按桌宠的额度记."""
        llm = self._watch_llm()
        see = WA.can_see(llm, self.watch_conf.get("vision", "auto"))
        messages = C.with_profile(WA.line_messages(ctx, see), self.persona, None)
        meta: dict = {}
        with L.charging("pet"):
            out = [t async for t in llm.stream(messages, schema=WA.SCHEMA, max_tokens=int(self.watch_conf["max_tokens"]),
                                                id_slot=self.slot, meta=meta)]
        return WA.parse_line("".join(out))

    async def _watch_title(self, source: str) -> str | None:
        """她的扩展没给标题时 (一般会给), B 站视频的标题 (认系统媒体控件里哪个会话是它、她说话、生活日志都用):
        公开接口匿名查, 不带任何 Cookie, 和渠道那边同一个只读客户端; 别的来源不查."""
        m = WA.BV.search(source)
        if not m:
            return None
        from .channels import bili as B  # 用到才导入
        base = str((getattr(self.channels, "conf", None) or {}).get("bili_base") or B.BASE)
        client = B.BiliClient(base, 8.0)
        try:
            return (await client.view(m.group(0))).get("title") or None
        finally:
            await client.close()

    async def _watch_ready(self, info: dict) -> None:
        """片子打开了、知道标题了: 再告诉页面一次 (开始时只有 source; 提示条有了标题就写《标题》)."""
        await self.send({"type": "watch_state", "active": True, **info})

    def _watch_trace(self, ev: dict) -> None:
        """陪看一步一记 (logs/watch-*.jsonl): 片子多长、进度从哪来、每段切完、说没说、谁暂停的."""
        self.tracelog.write({"time": datetime.now().isoformat(timespec="seconds"), **ev}, kind="watch")

    async def _watch_end(self, summary: dict) -> None:
        """陪看结束 (看完、你说不看了、换片、断线): 记一笔, 告诉页面; 真看过的记进她自己的生活日志 (睡前回顾看得到),
        再记进记忆 (_watch_remember). 屏幕陪看 (mode screen) 真看过 = 问过模型、看到了东西或说了话."""
        self.tracelog.write({"time": datetime.now().isoformat(timespec="seconds"), "event": "end", **summary}, kind="watch")
        screen = summary.get("mode") == "screen"
        with contextlib.suppress(Exception):
            await self.send({"type": "watch_state", "active": False, "title": summary.get("title"),
                             "reason": summary.get("reason"), **({"mode": "screen"} if screen else {})})
        seen = bool(summary.get("looks") and (summary.get("lines") or summary.get("notes"))) if screen \
            else bool(summary.get("segments"))
        if self.channels is not None and seen and (summary.get("title") or screen):
            minutes = max(1, round(float(summary.get("watched_s") or 0) / 60)) if summary.get("watched_s") else 0
            if screen:
                what = f"《{summary['title']}》" if summary.get("title") else "主人屏幕上的东西"
                text = f"和主人一起看了{what} (看他的屏幕" + (f", {minutes} 分钟" if minutes else "") + ")"
            else:
                text = f"和主人一起看了《{summary['title']}》" + (f" (跟着看到第 {minutes} 分钟)" if minutes else "")
            text += f", 说了 {summary['said']} 句" if summary.get("said") else ""
            with contextlib.suppress(Exception):
                await self.channels._diary(text)
        if seen:
            try:
                await self._watch_remember(summary)
            except Exception as exc:  # noqa: BLE001 (记不上不影响陪看)
                log.warning("陪看记进记忆失败: %s", exc)

    async def _watch_remember(self, summary: dict) -> None:
        """陪看完记进记忆 (2026-11-08 用户: "刷视频，也要整理到记忆、知识库中，特别是交互中重要的内容"):
        <角色>/记忆/一起看过的视频.md 记一节 (怎么看的、讲了什么、她说的、你说的和她回的; 参与检索),
        日记"## 我自己的"记一句 (带"看了", 晚上提炼"我的理解"的原料). 讲了什么由模型概括一次 (_watch_gist).
        你说的话里关于你自己的事照旧由日志整理 (wm/digest.py) 挑进候选, 你确认了才进长期记忆. 没开长期记忆就不记."""
        mem = self.e.memory
        if mem is None:
            return
        screen = summary.get("mode") == "screen"
        title = str(summary.get("title") or "")
        watched = float(summary.get("watched_s") or 0)
        minutes = max(1, round(watched / 60)) if watched >= 30 else 0
        lines = [str(s) for s in summary.get("lines") or [] if s]
        notes = [str(s) for s in summary.get("notes") or [] if s]
        heard = [(str(u), str(r or "")) for u, r in (summary.get("heard") or []) if u]
        items = [str(t) for t in summary.get("items") or [] if t]
        gist = await self._watch_gist(summary)
        now = datetime.now()
        if screen:
            how = "看主人的屏幕 (只看他前台那个窗口)" + (f", 刷到 {len(items)} 条" if len(items) > 1 else "")
        else:
            how = f"主人给的 B 站视频 {summary['bvid']}" if summary.get("bvid") else "主人给的视频"
        body = [f"- 怎么看的: {how}" + (f"; 一起看了约 {minutes} 分钟" if minutes else "")]
        if screen and len(items) > 1:
            body.append("- 刷到的: " + " / ".join(f"《{t}》" for t in items[:12]))
        if gist:
            body.append(f"- 讲了什么: {gist}")
        elif notes:
            body.append("- 看到的: " + "; ".join(notes[:10]))
        if lines:
            body.append("- 我说的: " + " / ".join(f"“{s}”" for s in lines[:10]))
        body += [f"- 主人说: “{u}”" + (f" (我回: “{r}”)" if r else "") for u, r in heard[:10]]
        head = f"{now:%Y-%m-%d %H:%M} " + (f"《{title}》" if title else "一起看主人的屏幕")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, mem.append_watch, head, body)
        what = f"《{title}》" if title else "主人屏幕上的东西"
        diary = f"和主人一起看了{what}" + (f" ({minutes} 分钟)" if minutes else "") + (f": {gist}" if gist else "")
        await loop.run_in_executor(None, lambda: mem.write_diary([f"{now:%H:%M} {diary}"], opening=False))

    async def _watch_gist(self, summary: dict) -> str:
        """一起看完, 让模型用她自己的口吻、两三句话写下看了什么、主人什么反应 (一次调用, 按桌宠的额度记);
        材料太少或出错就空 (只记原话)."""
        lines = [str(s) for s in summary.get("lines") or [] if s]
        notes = [str(s) for s in summary.get("notes") or [] if s]
        heard = [str(u) for u, _ in (summary.get("heard") or []) if u]
        items = [str(t) for t in summary.get("items") or [] if t]
        if len(lines) + len(notes) + len(heard) < 2:
            return ""
        parts = [f"标题: 《{summary.get('title') or '没看清'}》"]
        if len(items) > 1:
            parts.append("刷到的: " + " / ".join(items[:12]))
        if notes:
            parts.append("你看到的: " + "; ".join(notes[:20]))
        if lines:
            parts.append("你说过: " + " / ".join(lines[:10]))
        if heard:
            parts.append("主人说过: " + " / ".join(heard[:10]))
        system = ("你刚和主人一起看完视频. 用你自己的口吻 (\"我们\"\"主人\"), 两三句话、80 字以内写下来: 看了什么、讲了什么、"
                  "主人什么反应. 只写下面材料里有的, 不编. 只输出一个 JSON: {\"gist\": 要写的话}.")
        messages = C.with_profile([{"role": "system", "content": system}, {"role": "user", "content": "\n".join(parts)}],
                                  self.persona, None)
        try:
            with L.charging("pet"):
                out = "".join([t async for t in self._watch_llm().stream(messages, schema=WATCH_GIST_SCHEMA, max_tokens=600,
                                                                          id_slot=self.slot, meta={})])
            start = out.find("{")
            gist = str((json.loads(out[start:out.rfind("}") + 1]) if start >= 0 else {}).get("gist") or "")
        except Exception as exc:  # noqa: BLE001
            log.info("陪看概括没成: %s", exc)
            return ""
        return " ".join(gist.split())[:160]

    async def _run_llm(self, turn: Turn, queue: asyncio.Queue) -> None:
        if await self._run_room_command(turn, queue):
            return
        if await self._run_watch_command(turn, queue):
            return
        tr = turn.trace
        memory = turn.memory_line = await self._recall(turn)
        state = turn.state
        # 用云端模型时带上钱包 (余额、今天花了多少、高峰还是空闲; wm/ledger.py). 本机小模型不带: 示例里没有, 它会照抄;
        # 本机大模型 (provider selfhost 这类, local) 也不带: 她这时说话不花钱, 那一行说的是"在云端思考花的是主人的钱"
        if isinstance(turn.llm, C.CloudLLM) and not turn.llm.local and (wallet := L.wallet_line()):
            state = f"{state}\n{wallet}" if state else wallet
        # 她在 QQ / 微信 / 群里 / 工作台的近况 (2026-11-01 用户: "QQ、微信、桌宠……应该是一个整体, 记忆是连贯的"); 本机小模型不带 (会照抄),
        # 本机大模型和云端一样带 (llm.local_api.near 可以关: 2026-11-02 实测 Qwen3.5-2B 十二轮里有五轮把它照搬进回答)
        if isinstance(turn.llm, C.CloudLLM) and (not turn.llm.local or C.LOCAL_API["near"]) and self.channels is not None \
                and (near := self.channels.pet_context()):
            state = f"{state}\n{near}" if state else near
        # 自我卡 (A1, wm/self_card.py): 开关关着时 text_for 返回 None, 这一句等于没写, 提示词逐字节不变
        card = SC.text_for(self.cfg, self.e.memory) if self.e.memory is not None else None
        messages = I.build_messages(turn.text, self.history, state, memory, turn.place, card)
        messages = C.with_profile(messages, self.persona, self._skills(turn))
        turn.messages = messages
        parser = JSONObjectStream(stream_keys=("reply",))
        splitter = ClauseSplitter(self.p["first_clause_min_chars"], self.p["clause_min_chars"],
                                  self.p["clause_max_chars"])
        raw: list[str] = []
        await self._cancel_prewarm()
        tr.mark("llm_request")
        turn.llm_meta = {}
        fact = I.answer_state(turn.text, turn.state_raw)
        if fact is None and self.room_seen and self.room_active is not None:
            # 问小屋里的杯子在哪: 按她的认知答 (只凭她看到过的, 带凭什么、多久以前), 不走 LLM (状态行里没有真实位置可以照着说)
            fact = self.room_active.answer(turn.text)
            if fact:
                tr.meta["room_answer"] = True
        if fact is None and turn.memory_answer:  # 问用户自己的固定信息 ("我叫什么"): 按记忆直接回答
            fact = turn.memory_answer
            tr.meta["memory_answer"] = True
        if fact:
            # 问角色自身状态的事实问题: 按状态照实回答, 不走 LLM (0.8B 模型有历史时容易答错)
            tr.meta["state_answer"] = True
            stream = _static_stream(I.dumps_compact({"op": "chat", "acts": [], "emo": "neutral", "reply": fact}))
        else:
            turn.long = I.long_request(turn.text, self.since_long <= 1)
            stream = turn.llm.stream(messages, schema=self.schemas[turn.place],
                                     max_tokens=self.cfg["llm"].get("max_tokens", 160), id_slot=self.slot,
                                     meta=turn.llm_meta, **self._tool_kw(turn))
        async with contextlib.aclosing(stream):
            async for delta in stream:
                tr.mark("llm_first_token")
                raw.append(delta)
                for event in parser.feed(delta):
                    await self._on_llm_event(turn, event, splitter, queue)
                if turn.long_open:  # 长回复说了固定开场: 模型的短回应用不上, 不等它写完 (它常把故事写进去写到上限)
                    break
        tr.mark("llm_done")
        if turn.escalation is not None:
            await turn.escalation  # 复核限时, 最多等 escalate_timeout_ms
        for clause in splitter.flush():
            await self._say(turn, clause, queue)
        turn.raw_output = "".join(raw)
        meta = turn.llm_meta if not fact else {}
        tr.meta["llm"] = {"completion_tokens": (meta.get("usage") or {}).get("completion_tokens"),
                          "timings": meta.get("timings"), "output": turn.raw_output}
        if meta.get("cloud"):  # 云端: 请求轮数、工具调用次数、纠正过的字段、模型的原始输出
            tr.meta["llm"]["cloud"] = meta["cloud"]
        if not tr.has("intent_ready"):
            # 输出没能解析出动作字段 (模型跑偏或被截断): 让前端退出思考姿态.
            tr.meta["parse_error"] = True
            await self._tsend(turn, {"type": "intent", "turn": turn.id, "op": turn.op or "chat", "acts": [],
                                     "fallback": True})
        if parser.done or turn.long_open:
            said = turn.raw_output
            if turn.refusal:  # 历史里记改判后的结果, 免得下一轮模型以为自己答应过
                said = I.dumps_compact({"op": "na", "acts": [], "emo": turn.emo or "shy", "reply": turn.refusal})
            elif turn.long_open:
                # 模型的输出停在 reply 中间, 历史里记解析出的 op/acts/emo 和实际说出口的
                head = {"op": turn.op, "acts": parser.result.get("acts") or [], "emo": turn.emo}
                try:
                    await self._run_long(turn, queue)
                except asyncio.CancelledError:  # 讲到一半被打断: 历史里记已经说出口的部分
                    if turn.reply_text and turn.buffer is None:
                        self._remember(turn.text, I.dumps_compact({**head, "reply": turn.reply_text + "……"}))
                    raise
                said = I.dumps_compact({**head, "reply": turn.reply_text})  # 长回复整段进历史
            elif turn.cut:  # 截断了的回复: 历史里记实际说出口的
                said = _with_reply(turn.raw_output, turn.reply_text)
            if turn.buffer is None:
                self._remember(turn.text, said)
            else:
                turn.pending_history = (turn.text, said)  # 投机轮确认后才算数

    async def _run_long(self, turn: Turn, queue: asyncio.Queue) -> None:
        """长回复: 意图那次照常出动作和情绪, 说了一句固定的开场, 再追加一次不带 JSON 约束的流式生成 (几百字的口语短句),
        走同样的分句 -> 合成 -> 口型/韵律管线; 用户插话照样取消整轮. 正文不过回复侧检查 ("我带你去找妈妈" 是故事里的话),
        只去掉括号里的舞台说明和 Markdown 符号; 开场说过的不再说; 同一句话第二次重复就停. 用单独的槽位 (llm.long.id_slot),
        不冲掉意图前缀的 KV 缓存. 写到 max_tokens 被截断时丢掉没说完的半句, 换一句收尾.
        llama-server 挂着 LoRA 时, 这一次请求把它的比例设成 llm.long.lora_scale (默认 0): LoRA 是为意图 JSON 训的,
        挂着它正文只写一两句 (实测 12-35 字, 关掉后 300 多字).
        0.8B 常常一百多字就自然收尾: 不到 llm.long.min_chars 字时, 带着已经讲的再要一次"接着讲" (最多一次, 共用 max_tokens);
        声音比生成慢得多 (一百多字要播半分钟), 接上的部分听不出停顿."""
        lc = self.cfg["llm"].get("long") or {}
        info = turn.trace.meta["long"] = {"chars": 0}
        turn.long_ran = True
        msgs = C.with_profile(I.build_long_messages(turn.text, self.history, turn.reply_text, turn.memory_line), self.persona)
        n = self.p["clause_min_chars"]  # 短回应已经开口了, 正文不用首句的低门槛
        splitter = ClauseSplitter(n, n, self.p["clause_max_chars"])
        opener = speakable_text(turn.reply_text)  # 模型常把开场白再说一遍
        seen: set[str] = set()
        body: list[str] = []
        fresh = True  # 这次请求还没说出一句: 开头的"好的/当然可以"不要
        t0 = now_ms()

        async def say(clause: str) -> bool:
            nonlocal fresh
            text = I.clean_long(clause, first=fresh)
            key = speakable_text(text)
            if not key or len(key) >= 2 and key in opener:
                return True
            if len(key) >= 6 and key in seen:  # 0.8B 写长了会兜圈子
                info["repeats"] = info.get("repeats", 0) + 1
                return info["repeats"] < 2
            seen.add(key)
            fresh = False
            info["chars"] += len(text)
            body.append(text)
            await self._speak(turn, text, queue)
            return True

        extra = dict(lc.get("params") or {})  # 云端模型只透传 OpenAI 接口认的几个 (cloud._PASS), repeat_penalty 不发
        if lc.get("lora_scale") is not None and hasattr(turn.llm, "lora_scales"):
            lora = await turn.llm.lora_scales(lc["lora_scale"])
            if lora:
                extra["lora"] = info["lora"] = lora
        budget, used, finish = lc.get("max_tokens", 400), 0, None
        for rnd in range(2):
            meta: dict = {}
            fresh = True
            kw = {"extra": extra} if extra else {}
            try:
                async with contextlib.aclosing(turn.llm.stream(
                        msgs, schema=None, max_tokens=budget - used, temperature=lc.get("temperature"),
                        id_slot=lc.get("id_slot"), meta=meta, **kw)) as stream:
                    async for delta in stream:
                        info.setdefault("first_token_ms", round(now_ms() - t0, 1))
                        for clause in splitter.feed(delta):
                            if not await say(clause):
                                meta["finish_reason"] = "repeat"
                                break
                        if meta.get("finish_reason") == "repeat":
                            break
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:  # 说过的留着, 不再往下讲
                log.warning("第 %d 轮长回复失败: %s", turn.id, exc)
                info["error"] = f"{type(exc).__name__}: {exc}"
                meta["finish_reason"] = "error"
            used += (meta.get("usage") or {}).get("completion_tokens") or 0
            finish = meta.get("finish_reason")
            if rnd or finish != "stop" or info["chars"] >= lc.get("min_chars", 200) or budget - used < 60:
                break
            for clause in splitter.flush():
                await say(clause)
            info["more"] = info["chars"]  # 第一次讲到这里就收尾了, 再要一次
            msgs = [*msgs, {"role": "assistant", "content": "".join(body)}, {"role": "user", "content": I.long_more(turn.text)}]
        tail = splitter.flush()
        info["finish"] = finish
        if finish in ("length", "repeat"):
            await self._speak(turn, I.LONG_END, queue)  # 没说完的半句不要
        elif finish != "error":
            for clause in tail:
                await say(clause)
        info.update(ms=round(now_ms() - t0, 1), tokens=used)

    def _remember(self, text: str, raw: str) -> None:
        # 历史只追加, 超过 2N 轮才一次性截到 N 轮. 滑动窗口会让 few-shot 之后的前缀每轮都变,
        # llama.cpp 的 KV 缓存只能命中 few-shot 部分, 每轮要重算一百多个 token.
        n = self.cfg["llm"].get("history_turns", 3)
        self.history.append((text, raw))
        if len(self.history) > 2 * n:
            del self.history[:-n]
        # 长回复整段进历史, 几段故事就会撑爆每个槽位的上下文 (4096 token, 前缀占约 2500): 超过字数预算时也一次性截短,
        # 只留一半预算放得下的最近几轮 (至少一轮). 普通轮 6 轮加起来不到 700 字, 碰不到
        budget = self.cfg["llm"].get("history_chars", 1600)
        size = lambda turns: sum(len(t) + len(r) for t, r in turns)  # noqa: E731
        if budget and size(self.history) > budget:
            keep = 1
            while keep < len(self.history) and size(self.history[-keep - 1:]) <= budget // 2:
                keep += 1
            del self.history[:-keep]
        self._schedule_prewarm()

    def _schedule_prewarm(self) -> None:
        if not self.cfg["llm"].get("prewarm", True) or self.cloud:  # 云端没有 KV 缓存可预热, 发了还要花钱
            return
        if self.prewarm_task and not self.prewarm_task.done():
            self.prewarm_task.cancel()
        self.prewarm_task = asyncio.create_task(self._prewarm(self._warm_messages()))

    def _warm_messages(self) -> list[dict]:
        """预热和保温的消息: 前缀要和正式请求一致 (人设也接在系统提示后面), 缓存才命中."""
        return C.with_profile(I.build_messages("", self.history, place=self.place), self.persona)

    def _watching(self) -> bool:
        return self.watch is not None and self.watch.alive

    def _screen_hint(self) -> bool:
        """宿主报你此刻在看视频 / 直播、玩游戏 (activity.category 是 video / game), 而且她看得了屏幕."""
        act = self.char_state_raw.get("activity") if isinstance(self.char_state_raw, dict) else None
        return isinstance(act, dict) and act.get("category") in ("video", "game") and self._screen_ok()

    def _turn_tools(self) -> list[dict]:
        """这一轮给模型的工具: 陪看的时候不给操作电脑的那一组 (WATCH_HELD_TOOL); 没在陪看、看得了屏幕时
        加上她自己的"看一眼屏幕" (LOOK_TOOL, 服务端做)."""
        if not self._watching():
            if self._screen_ok() and not any((t.get("function") or {}).get("name") == LOOK_TOOL_NAME for t in self.tools):
                return [*self.tools, LOOK_TOOL]
            return self.tools
        return [t for t in self.tools if not str((t.get("function") or {}).get("name") or "").startswith(WATCH_HELD_TOOL)]

    async def _look_at_screen(self) -> dict:
        """她自己"看一眼屏幕" (look_at_screen): 和屏幕陪看一样的规矩 —— 只截前台那个窗口, 聊天、密码、网银、登录支付、
        写代码办公的窗口不截, 截图只在内存里; 截到了交给看得了图的模型写一两句看到了什么, 回给她. 每次记进陪看日志 (glance)."""
        info = await asyncio.to_thread(SA.foreground)
        ok, why = SA.watchable(info)
        title, app = SA.clean_title((info or {}).get("title") or ""), SA.app_name((info or {}).get("exe") or "")
        if not ok:
            self._watch_trace({"event": "glance", "result": why, "app": app})
            return {"content": _GLANCE_WHY.get(why, "这会儿看不了"), "is_error": False}
        img = await asyncio.to_thread(SA.grab, info["rect"], int(self._screen_conf()["width"]))
        if img is None:
            self._watch_trace({"event": "glance", "result": "no_image", "app": app})
            return {"content": "截不到这个窗口的画面", "is_error": True}
        try:
            saw = await self._screen_describe(SA.sheet([(0, img)]), title, app)
        except Exception as exc:  # noqa: BLE001
            self._watch_trace({"event": "glance", "result": "error", "app": app, "error": type(exc).__name__})
            return {"content": f"看了一眼, 但没看明白 ({type(exc).__name__})", "is_error": True}
        finally:
            del img
        self._watch_trace({"event": "glance", "result": "ok", "app": app, "title": title[:60], "saw": saw[:60]})
        where = f"「{app}」" + (f"《{title}》" if title else "")
        return {"content": f"你看了一眼主人前台那个窗口 ({where}): {saw or '看不清'}", "is_error": False}

    async def _screen_describe(self, image: str, title: str, app: str) -> str:
        """一张截图交给看得了图的模型写一两句看到了什么 (按桌宠的额度记)."""
        system = ("下面是主人电脑前台那个窗口的一张截图. 用一两句话 (60 字以内) 写下你看到了什么: 在放什么、画面上有什么、"
                  "字幕和弹幕说了什么; 看不清就照实说看不清, 不编. 只输出一个 JSON: {\"saw\": 看到了什么}.")
        user = [{"type": "text", "text": f"窗口标题《{title or '没有标题'}》, 程序「{app}」"},
                {"type": "image_url", "image_url": {"url": image, "detail": "low"}}]
        messages = C.with_profile([{"role": "system", "content": system}, {"role": "user", "content": user}], self.persona, None)
        with L.charging("pet"):
            out = "".join([t async for t in self._watch_llm().stream(messages, schema=GLANCE_SCHEMA, max_tokens=600,
                                                                      id_slot=self.slot, meta={})])
        start = out.find("{")
        saw = str((json.loads(out[start:out.rfind("}") + 1]) if start >= 0 else {}).get("saw") or "")
        return " ".join(saw.split())[:120]

    def _skills(self, turn: Turn) -> list[dict]:
        """列进系统提示的 skill: 只给云端模型, 而且要有 load_skill 工具 (全文靠它取). 陪看的时候不列操作电脑的 skill."""
        if turn.llm is self.e.llm or not any(t["function"]["name"] == "load_skill" for t in self.tools):
            return []
        if self._watching():
            return [s for s in self.skills if s.get("name") not in WATCH_HELD_SKILLS]
        return self.skills

    def _tool_kw(self, turn: Turn) -> dict:
        """只有云端模型接工具 (本地小模型不接, 它的 stream 也没有这两个参数). 陪看的时候拿掉操作电脑的那一组."""
        tools = self._turn_tools()
        if turn.llm is self.e.llm or not tools:
            return {}
        if len(tools) < len(self.tools):
            turn.trace.meta["tools_held"] = {"why": "watch", "n": len(self.tools) - len(tools)}
        return {"tools": tools, "on_tools": lambda calls: self._run_tools(turn, calls)}

    async def _run_tools(self, turn: Turn, calls: list[dict]) -> list[dict]:
        """云端模型要调工具: 每个调用下发 tool_call, 等桌宠回 tool_result (执行、让用户确认都在桌宠那边).
        llm.cloud.tool_timeout_s 内没回算出错; 这一轮被打断时, 没回的调用发 tool_cancel, 桌宠可以收起确认框.
        trace 只记名字、成败、结果字数和耗时, 不记参数和结果原文.
        陪看的时候操作电脑的调用不下发 (工具列表里已经拿掉了, 万一模型还是叫了): 直接回一句为什么, 别的照常."""
        held = {i for i, c in enumerate(calls) if self._watching() and str(c.get("name") or "").startswith(WATCH_HELD_TOOL)}
        local = {i for i, c in enumerate(calls) if c.get("name") == LOOK_TOOL_NAME and i not in held}  # 服务端自己做的
        if held or local:
            special = held | local
            got = iter(await self._run_tools(turn, [c for i, c in enumerate(calls) if i not in special])
                       if len(special) < len(calls) else [])
            rec, out = turn.trace.meta.setdefault("tools", []), []
            for i, c in enumerate(calls):
                if i in held:
                    rec.append({"call_id": f"{turn.id}.held", "name": c["name"], "is_error": True, "chars": len(WATCH_HELD_MSG),
                                "ms": 0.0, "held": "watch"})
                    out.append({"content": WATCH_HELD_MSG, "is_error": True})
                elif i in local:
                    t0 = now_ms()
                    r = await self._look_at_screen()
                    rec.append({"call_id": f"{turn.id}.look", "name": c["name"], "is_error": r["is_error"],
                                "chars": len(r["content"]), "ms": round(now_ms() - t0, 1), "local": True})
                    out.append(r)
                else:
                    out.append(next(got))
            return out
        loop = asyncio.get_running_loop()
        timeout = float(self.cloud_cfg.get("tool_timeout_s", 90))
        t0, done_at, pending = now_ms(), {}, []
        for c in calls:
            self.tool_seq += 1
            cid = f"{turn.id}.{self.tool_seq}"
            fut = self.pending_tools[cid] = loop.create_future()
            fut.add_done_callback(lambda _, cid=cid: done_at.setdefault(cid, now_ms()))
            pending.append((cid, c, fut))
            await self._tsend(turn, {"type": "tool_call", "turn": turn.id, "call_id": cid, "name": c["name"],
                                     "arguments": c["arguments"]})
        def timeout_for(c):
            if c["name"] == "room_do":
                args = c.get("arguments") or {}
                return RA.timeout_seconds(args.get("timeout_s", 180)) + 2
            return timeout

        async def wait_call(c, fut):
            seconds = timeout_for(c)
            try:
                await asyncio.wait_for(asyncio.shield(fut), seconds)
            except asyncio.TimeoutError:
                if not fut.done():
                    fut.set_result({"content": f"等了 {seconds:g} s 没有结果, 没有执行完", "is_error": True})
                    cid = next(cid for cid, _, pending_fut in pending if pending_fut is fut)
                    await self._tsend(turn, {"type": "tool_cancel", "turn": turn.id, "call_id": cid, "reason": "timeout"})
        try:
            await asyncio.gather(*(wait_call(c, f) for _, c, f in pending))
        except asyncio.CancelledError:
            for cid, _, fut in pending:
                if not fut.done():
                    await self._tsend(turn, {"type": "tool_cancel", "turn": turn.id, "call_id": cid, "reason": "interrupted"})
            raise
        finally:
            for cid, _, _ in pending:
                self.pending_tools.pop(cid, None)
        results, rec = [], turn.trace.meta.setdefault("tools", [])
        for cid, c, fut in pending:
            if fut.done():
                r = fut.result()
            else:
                r = {"content": f"等了 {timeout_for(c):g} s 没有结果, 没有执行完", "is_error": True}
                await self._tsend(turn, {"type": "tool_cancel", "turn": turn.id, "call_id": cid, "reason": "timeout"})
            rec.append({"call_id": cid, "name": c["name"], "is_error": r["is_error"], "chars": len(r["content"]),
                        "ms": round(done_at.get(cid, now_ms()) - t0, 1)})
            results.append(r)
        return results

    async def _prewarm(self, messages: list[dict]) -> None:
        try:
            async for _ in self.e.llm.stream(messages, schema=None, max_tokens=1, id_slot=self.slot):
                pass
            self.prewarm_stats["done"] += 1
        except asyncio.CancelledError:
            self.prewarm_stats["cancelled"] += 1
            raise
        except Exception as exc:  # 预热失败不影响正常请求
            self.prewarm_stats["failed"] += 1
            log.warning("KV 预热失败: %s", exc)

    def _start_keepalive(self) -> None:
        """用户开始说话时启动显卡保温: 笔记本显卡空闲 1.5~3 s 就会降频, LLM 生成速度能掉一半.
        说话期间每 keepalive_ms 发一个缓存全命中的 1-token 请求 (第一次顺带预热 KV), 真实请求开始时被取消."""
        interval = self.cfg["llm"].get("keepalive_ms", 700) / 1000
        if interval <= 0 or self.cloud:  # 云端用不着保温本机显卡
            return
        previous = self.prewarm_task if self.prewarm_task and not self.prewarm_task.done() else None
        self.prewarm_task = asyncio.create_task(self._keepalive(interval, previous))

    async def _keepalive(self, interval: float, previous: asyncio.Task | None) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg["llm"].get("keepalive_max_s", 10)
        try:
            if previous is not None:  # 上一轮的预热还没做完, 等它做完再接着保温
                with contextlib.suppress(Exception):
                    await previous
            messages = self._warm_messages()
            # 实测每 0.7 s 只生成 1 个 token 时显卡仍停在 210 MHz; 生成 24 个 token (约 20% 占空比) 才升到 1.4 GHz.
            tokens = self.cfg["llm"].get("keepalive_tokens", 24)
            while loop.time() < deadline:
                async for _ in self.e.llm.stream(messages, schema=None, max_tokens=tokens, id_slot=self.slot):
                    pass
                self.prewarm_stats["keepalive"] += 1
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            if previous is not None and not previous.done():
                previous.cancel()
            raise
        except Exception as exc:
            self.prewarm_stats["failed"] += 1
            log.warning("显卡保温请求失败: %s", exc)

    async def _cancel_prewarm(self) -> None:
        task, self.prewarm_task = self.prewarm_task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _on_llm_event(self, turn: Turn, event: tuple, splitter: ClauseSplitter, queue: asyncio.Queue) -> None:
        tr = turn.trace
        kind = event[0]
        if kind == "value":
            key, value = event[1], event[2]
            if key == "op":
                turn.op = value if value in I.OPS else "chat"
            elif key == "acts":
                tr.mark("intent_ready")
                room = turn.place != I.DESKTOP
                raw_op, raw_acts = turn.op, I.normalize_acts(value)
                # 规则兜底只管房间里的空间指令 (跑过去、坐沙发、放回去、指着窗户); 桌面上没有这些东西
                turn.op, acts = I.repair(turn.text, raw_op, raw_acts) if room else (raw_op, raw_acts)
                # 说了做却没动: 原话是做得到的动作 ("你能过来一下吗"), 模型当成聊天没给动作, 或者选错了技能
                # ("你摇个头试试" 给了 nod), 按原话拼出动作换上
                # 补动作按完整的身体算 (加上声明过的新技能), 补了再按可行性声明去掉
                turn.can = self._can()
                full = I.DEFAULT_CAN | (turn.can & I.OPT_IN)
                turn.req_kind = P.request_kind(turn.text, full, room)
                # 同一句话按这个身体算 (声明过的技能; 没声明时只去掉 OPT_IN 的新技能): 完整身体做得到、这个身体做不了的
                # 要求 ("能去沙发上吗", 桌宠不能走) 是 impossible. 回复侧检查和改判用它
                turn.body_kind = turn.req_kind if turn.can == full else P.request_kind(turn.text, turn.can, room)
                if turn.op != "stop" and turn.req_kind == "doable" and (not acts or I.off_cue(turn.text, acts)):
                    filled = I.fill_acts(turn.text)
                    if filled:
                        tr.meta["filled"] = {"model": [a["do"] for a in acts], "filled": [a["do"] for a in filled]}
                        if turn.op == "na":  # 模型说做不到 ("这个我还做不到")、动作却补上了: 换成一句应答, 言行一致
                            turn.ack, turn.cut = "好的。", True
                        turn.op, acts = "act", filled
                # 可行性声明: 前端报过这个身体能做哪些技能 (比如 2D 形象不能走路坐下, 桌宠不能走), 做不到的去掉;
                # 桌面上冲着房间里东西的走、看、拿也做不到. 动作全都做不到, 而且是动作指令或这个身体做不了的要求时
                # (模型当成聊天却配了"走过去"), 和能力闸门一样改判成"做不到"; 不然只去掉动作, 照常聊天
                acts, cant = I.split_can(acts, turn.can, room)
                if cant:
                    tr.meta["body_cannot"] = cant
                    if not acts and (turn.op == "act" or turn.body_kind == "impossible"):
                        await self._refuse(turn, {"type": "intent", "turn": turn.id, "op": "na", "acts": []}, queue, body=cant)
                        return
                # 模型要做动作, 但动作全被规范化丢掉了 ("拿可乐" -> 房间里没有可乐), 原话又不是做得到的事: 改判成"做不到"
                if turn.op == "act" and not acts and turn.req_kind == "impossible":
                    await self._refuse(turn, {"type": "intent", "turn": turn.id, "op": "na", "acts": []}, queue, empty=True)
                    return
                # 社交后果预测参与决策 (在意图闭合时): 这个组合以往常让用户退缩或情绪升级, 而且样本够多时, 动作幅度收一点
                acts = self._soften(turn, acts)
                msg = {"type": "intent", "turn": turn.id, "op": turn.op, "acts": acts}
                if cant:
                    msg["body"] = cant  # 去掉了这些动作 (还有别的动作要做, 或者照常聊天)
                # 置信度 (模型自己给出的选中概率) 和升级契约: 低于阈值的动作指令标记为"不确定",
                # 规则兜底改动过的也记下来; 配了更大的模型时由它复核 (见 docs/roadmap)
                conf = field_confidence(turn.llm_meta.get("tokens") or [], I.field_enums(turn.place),
                                        self.cfg["llm"].get("temperature", 0.3))
                if conf is None and getattr(turn.llm, "rule_confidence", False):
                    # 云端拿不到 token 概率: 按原话里有没有这些技能的说法估 (能力闸门照样起作用)
                    conf = rule_confidence(turn.text, turn.op, acts, turn.can, room)
                if conf is not None:
                    # 闲聊也可能是判错了的指令 ("看看门口" 被当成聊天), 所以按整体置信度判断, 不只看 act
                    low = conf["overall"] < self.p.get("confidence_threshold", 0.5)
                    repaired = (raw_op, raw_acts) != (turn.op, acts)
                    msg["conf"] = conf["overall"]
                    tr.meta["confidence"] = {**conf, "low": low, "repaired": repaired}
                    # 这一轮调成过工具 (打开网址、操作电脑…): 事情是工具做的, 回复说"打开了"是真的, 不按身体动作的闸门改判
                    # (2026-10-25: "帮我打开 B 站首页" 调 open_url 成功、模型配了个点头, 被换成了"这个我还没学会")
                    used_tools = any(not t.get("is_error") for t in tr.meta.get("tools") or [])
                    if not used_tools and I.unsupported(turn.text, turn.op, acts, conf["overall"], can=turn.can, room=room):
                        # 能力闸门: 做不到的要求被拿别的动作凑数 (翻跟头 -> 摇头). 改成 na, 回复换成固定的坦白,
                        # 模型后面生成的回复 (多半是"好呀我翻给你看") 不再采用
                        tr.meta["confidence"]["gated"] = [a["do"] for a in acts]
                        await self._refuse(turn, msg, queue, gated=True)
                        return
                    if low and self.e.escalate is not None and turn.llm is self.e.llm:  # 云端模型比复核模型大, 不复核
                        # 复核期间回复照常流式生成、照常合成语音; 只有这一轮的动作等复核结果 (推迟动作, 不推迟声音)
                        turn.escalation = asyncio.create_task(self._escalate_and_send(turn, msg))
                        return
                    if low and turn.op == "act":
                        msg["uncertain"] = True
                await self._send_intent(turn, msg)
                if turn.ack:
                    await self._speak(turn, turn.ack, queue)
            elif key == "emo":
                tr.mark("emotion_ready")
                turn.emo = value = "shy" if turn.refusal else value
                await self._tsend(turn, {"type": "emotion", "turn": turn.id, "emo": value,
                                         "vad": list(I.emotion_vad(value))})
                if turn.long and turn.op in ("chat", "act") and turn.refusal is None and not turn.cut:
                    # 长回复: 模型的短回应不用 (实测 4/4 不对题: "你想听什么故事？"之后照样讲, "好呀，快讲个故事吧！"
                    # 像是用户在说), 换一句固定的开场; 正文由 _run_long 接着讲
                    turn.long_open = turn.cut = True
                    await self._speak(turn, I.long_opener(turn.text, self.since_long <= 1), queue)
        elif kind == "delta" and event[1] == "reply" and turn.refusal is None and not turn.cut:
            for clause in splitter.feed(event[2]):
                await self._say(turn, clause, queue)

    async def _say(self, turn: Turn, clause: str, queue: asyncio.Queue) -> None:
        """回复的一个分句: 先过回复侧检查 (假装答应做不到的事, 见 promise.py), 没问题才显示字幕、送去合成.
        字幕按分句显示 (原来按 token): 检查没通过的字不能先出现在屏幕上."""
        if turn.refusal is not None or turn.cut:
            return
        room = turn.place != I.DESKTOP
        if turn.body_kind == "?":
            turn.body_kind = P.request_kind(turn.text, turn.can, room)
        acts = [a["do"] for a in turn.acts]
        # 按这个身体检查: 桌宠没声明走路时, "我陪你去沙发上坐坐吧" 是许诺做不到的事.
        # 这一轮调成过工具时不查: "已经帮你打开 B 站首页啦" 是工具做成的事, 不是身体许诺 (2026-10-25 被剪成了"嗯嗯。")
        used_tools = any(not t.get("is_error") for t in turn.trace.meta.get("tools") or [])
        v = None if used_tools else P.check(turn.text, turn.op, acts, clause, turn.body_kind, turn.can, room)
        if v is None:
            await self._speak(turn, clause, queue)
            return
        act = P.action(v, turn.body_kind, turn.op, acts, turn.spoken)
        turn.trace.meta.setdefault("promise", []).append({"rule": v.rule, "segment": v.segment, "action": act})
        if act == "refuse":
            # 还没开口就发现是在答应做不到的事: 和能力闸门一样改判, 换固定的坦白. 意图已经发过 (chat, 没有动作),
            # 只补一条 amend 让前端的标签改成"做不到"
            turn.refusal, cant = self._refusal(turn)
            turn.op = "na"
            amend = {"type": "amend", "turn": turn.id, "op": "na", "why": v.rule}
            await self._tsend(turn, {**amend, "body": cant} if cant else amend)
            await self._speak(turn, turn.refusal, queue)
            return
        turn.cut = True  # 从这一小段起截掉; 一个字都还没说时用一句"嗯嗯。"兜底, 不让角色一声不吭
        keep = v.keep or ("" if turn.spoken else P.FALLBACK)
        if keep:
            await self._speak(turn, keep, queue)

    async def _speak(self, turn: Turn, text: str, queue: asyncio.Queue) -> None:
        turn.reply_text += text
        turn.spoken += 1
        await self._tsend(turn, {"type": "reply_delta", "turn": turn.id, "text": text})
        await self._enqueue(turn, text, queue)

    async def _refuse(self, turn: Turn, msg: dict, queue: asyncio.Queue, **why) -> None:
        """改判成"做不到": 回复换成固定的坦白, 模型后面生成的回复不再采用."""
        turn.refusal, _ = self._refusal(turn, why.get("body"))
        turn.op = "na"
        msg.update(op="na", acts=[], **why)
        await self._send_intent(turn, msg)
        await self._speak(turn, turn.refusal, queue)

    def _refusal(self, turn: Turn, body: list | None = None) -> tuple[str, list]:
        """改判时换上的坦白和原因: 原话里说到了这个身体做不了的技能 (或可行性声明刚去掉了动作) 时按技能说
        ("我在桌面上，走过去还做不到呢。"), 其余是三句固定的轮换. 都只用固定词表."""
        cant = I.asked_cannot(turn.text, turn.can) or list(body or [])
        if cant:
            return I.body_refusal(cant, turn.place), cant
        return I.REFUSALS[turn.id % len(I.REFUSALS)], []

    def _can(self) -> frozenset:
        """这个身体能做的技能: 前端声明过就按声明, 没声明时是 I.DEFAULT_CAN (新加的 OPT_IN 技能要声明了才算)."""
        return frozenset(self.capabilities) if self.capabilities is not None else I.DEFAULT_CAN

    def _soften(self, turn: Turn, acts: list) -> list:
        """社交后果预测: P(退缩) + P(升级) 超过阈值且这个组合见过足够多次, 动作幅度乘 soften_amp (先只做衰减, 不换动作)."""
        if not acts:
            return acts
        key = key_of(turn.op, acts, None)
        pred, n = self.social.predict(turn.op, acts, None), self.social.support(key)
        risk = pred.get("withdraw", 0) + pred.get("escalate", 0)
        turn.trace.meta["social_pre"] = {"key": key, "risk": round(risk, 3), "n": n}
        if n < self.p.get("soften_min_samples", 20) or risk < self.p.get("soften_risk", 0.5):
            return acts
        k = self.p.get("soften_amp", 0.7)
        turn.trace.meta["social_pre"]["softened"] = k
        return [{**a, "amp": round(a["amp"] * k, 3)} for a in acts]

    async def _recall(self, turn: Turn) -> str | None:
        """长期记忆检索 (限时, 默认 80 ms; 超时这一轮就不带记忆). 这次连接的第一轮带上关系概况."""
        mem = self.e.memory
        if mem is None or not turn.text:
            return None
        loop = asyncio.get_running_loop()
        if not self._mem_session:
            self._mem_session = True
            loop.run_in_executor(None, mem.start_session)
        try:
            # 桌宠小屋的日记本门控: 合着时只取 slots (名字、喜好) 或什么都不取, 翻开时都取.
            # 只管发过 diary 的连接 (有日记本的客户端); 网页、QQ 这些没有日记本, 照旧都能读
            gate = (self.cfg.get("memory") or {}).get("diary_gate", "off")
            scope = gate if self.diary_used and not self.diary_open and gate in ("slots", "none") else None
            r = await asyncio.wait_for(loop.run_in_executor(None, mem.recall, turn.text, not self.history, scope),
                                       self.p.get("memory_timeout_ms", 80) / 1000)
        except asyncio.TimeoutError:
            turn.trace.meta["memory"] = {"timeout": True}
            return None
        except Exception as exc:  # 库被移走、文件被占用
            turn.trace.meta["memory"] = {"error": f"{type(exc).__name__}: {exc}"}
            return None
        turn.trace.meta["memory"] = {"ms": r["ms"], "hits": r["hits"], "line": r["line"], "mode": r.get("mode"),
                                     **({"faded": r["faded"]} if r.get("faded") else {}),  # 太久了没想起来的
                                     **({"promoted": r["promoted"]} if r.get("promoted") else {})}  # 常提够天数升成长期的
        turn.memory_answer = r.get("answer")  # "我叫什么""我住在哪": 按记忆直接回答, 不交给模型 (它会把"我"答成自己)
        return r["line"]

    def _observe(self, turn: Turn) -> None:
        try:
            new = self.e.memory.observe(turn.text, turn.reply_text, turn.acts, turn.emo)
            if new:
                log.info("记下: %s", "; ".join(new))
        except Exception:
            log.exception("写长期记忆失败")

    async def _send_intent(self, turn: Turn, msg: dict) -> None:
        turn.acts = msg["acts"]
        turn.trace.meta["social"] = {"key": key_of(turn.op, turn.acts, None), "predict": self.social.predict(turn.op, turn.acts, None)}
        await self._tsend(turn, msg)

    async def _escalate_and_send(self, turn: Turn, msg: dict) -> None:
        second = await self._second_opinion(turn)
        meta = turn.trace.meta["confidence"]
        meta["escalated"] = second
        if second and second["acts"] is not None:
            # 复核的动作也要过可行性声明 (原来直接下发, 会绕过 capabilities)
            acts, cant = I.split_can(second["acts"], turn.can, turn.place != I.DESKTOP)
            if cant:
                msg["body"] = cant
            meta["changed"] = (second["op"], acts) != (msg["op"], msg["acts"])
            turn.op = second["op"]
            msg.update(op=turn.op, acts=acts, escalated=True)
        elif turn.op == "act":
            msg["uncertain"] = True
        await self._send_intent(turn, msg)

    async def _second_opinion(self, turn: Turn) -> dict | None:
        """升级复核: 同样的上下文, 只要 op 和 acts, 限时 (默认 0.8 s). 超时或出错就沿用小模型的结果."""
        t0 = now_ms()
        try:
            raw = await asyncio.wait_for(
                self.e.escalate.complete(turn.messages, schema=I.build_intent_schema(turn.place), max_tokens=60),
                self.p.get("escalate_timeout_ms", 800) / 1000)
            obj = json.loads(raw)
            op = obj.get("op") if obj.get("op") in I.OPS else "chat"
            op, acts = op, I.normalize_acts(obj.get("acts") or [])
            if turn.place != I.DESKTOP:
                op, acts = I.repair(turn.text, op, acts)
            return {"op": op, "acts": acts, "ms": round(now_ms() - t0, 1), "model": self.e.escalate.label}
        except (asyncio.TimeoutError, aiohttp.ClientError, json.JSONDecodeError, RuntimeError) as exc:
            return {"op": None, "acts": None, "ms": round(now_ms() - t0, 1), "error": type(exc).__name__}

    async def _enqueue(self, turn: Turn, clause: str, queue: asyncio.Queue) -> None:
        turn.trace.mark("first_clause")
        await self._tsend(turn, {"type": "clause", "turn": turn.id, "text": clause})
        queue.put_nowait(clause)

    async def _tts_worker(self, turn: Turn, queue: asyncio.Queue) -> None:
        if not turn.audio:  # 连接声明过不要语音: 分句照常下发, 不合成 (也就没有 RVC、面部和韵律), 没有音频段
            while await queue.get() is not None:
                pass
            await self._tsend(turn, {"type": "audio_end", "turn": turn.id, "segments": 0})
            return
        tr = turn.trace
        seg = 0
        # 面部动画单独排队: 每块音频发出后就交给 A2F (先算右侧上下文已齐的帧), 不挡语音合成.
        face_q: asyncio.Queue | None = asyncio.Queue() if self.e.face.enabled else None
        face_task = asyncio.create_task(self._face_worker(turn, face_q)) if face_q else None
        try:
            while True:
                clause = await queue.get()
                if clause is None:
                    break
                seg += 1
                tr.mark("tts_first_request")
                sr = None
                if self.e.rvc.enabled:
                    parts = []
                    async for pcm, sr in self.e.tts.stream(clause):
                        tr.mark("tts_first_chunk")
                        parts.append(pcm)
                    if not parts:
                        continue
                    tr.mark("rvc_first_in")
                    audio, sr = await self.e.rvc.convert(np.concatenate(parts), sr)
                    tr.mark("rvc_first_out")
                    await self._send_audio(turn, seg, clause, audio, sr, first=True)
                    if face_q is not None:
                        face_q.put_nowait((seg, audio, sr, False))
                else:
                    first, parts, pst = True, [], {}
                    async for pcm, sr in self.e.tts.stream(clause):
                        tr.mark("tts_first_chunk")
                        await self._send_audio(turn, seg, clause, pcm, sr, first=first)
                        first = False
                        if face_q is not None:
                            face_q.put_nowait((seg, pcm, sr, False))
                        parts.append(pcm)
                        self._prosody(turn, seg, parts, sr, False, pst)
                    if parts:
                        self._prosody(turn, seg, parts, sr, True, pst)
                if face_q is not None and sr is not None:
                    face_q.put_nowait((seg, None, sr, True))  # 这一句结束: 补齐末尾的帧
            if turn.hum is not None and not turn.trace.interrupted and turn.refusal is None and not turn.cut:
                # 哼唱不走 TTS (TTS 只会说话): 自己按音符合成一小段, 接在说完之后; 口型、韵律照常走
                seg += 1
                try:
                    pcm = await asyncio.to_thread(H.synth, turn.hum)
                except Exception as exc:  # 哼不出来不影响已经说过的话
                    log.warning("第 %d 轮哼唱失败: %s", turn.id, exc)
                else:
                    if len(pcm):
                        sr = H.SAMPLE_RATE
                        await self._send_audio(turn, seg, H.label(turn.hum), pcm, sr, first=True)
                        if face_q is not None:
                            face_q.put_nowait((seg, pcm, sr, False))
                            face_q.put_nowait((seg, None, sr, True))
                        self._prosody(turn, seg, [pcm], sr, True, {})
                        tr.meta["hum"] = {"name": turn.hum.name, "notes": len(turn.hum.notes),
                                          "seconds": round(len(pcm) / sr, 2)}
            if face_task is not None:
                face_q.put_nowait(None)
                await face_task
        finally:
            if face_task is not None and not face_task.done():
                face_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await face_task
        await self._tsend(turn, {"type": "audio_end", "turn": turn.id, "segments": turn.audio_segments})

    def _prosody(self, turn: Turn, seg: int, parts: list, sr: int, final: bool, st: dict) -> None:
        """后台分析这一句已收到的语音的重音和句末语调, 下发新出现的事件 (t 是句内秒数, 前端按音频时钟排表演).
        TTS 一句可能分很多小块: 每多 0.5 s 音频重新分析一次整句; 末尾 0.3 s 内的事件等后面的音频到了再定."""
        if not self.p.get("prosody", True):
            return
        dur = sum(len(p) for p in parts) / sr
        if not final and dur - st.get("done", 0.0) < 0.5:
            return
        st["done"] = dur
        pcm, prev = np.concatenate(parts), st.get("prev")

        async def run():
            if prev is not None:
                with contextlib.suppress(Exception):
                    await prev  # 同一句按顺序分析, 事件不重复也不乱序
            try:
                events = await asyncio.get_running_loop().run_in_executor(None, analyze_prosody, pcm, sr)
            except Exception as exc:
                log.warning("韵律分析失败: %s", exc)
                return
            horizon = dur if final else dur - 0.3
            new = [e for e in events if st.get("emitted", -1.0) < e["t"] <= horizon]
            if new:
                st["emitted"] = max(e["t"] for e in new)
                await self._tsend(turn, {"type": "prosody", "turn": turn.id, "seg": seg, "events": new})

        task = asyncio.create_task(run())
        st["prev"] = task
        turn.side_tasks.add(task)
        task.add_done_callback(turn.side_tasks.discard)

    async def _face_worker(self, turn: Turn, queue: asyncio.Queue) -> None:
        """把各句的音频块依次交给 A2F, 每算出一段帧就下发一条 face 消息 (start = 句内起始帧, 前端按 turn+seg 拼接).
        A2F 忙的时候排起来的同一句的块合成一次计算, 少调用几次网络."""
        stream = FaceStream()
        meta = turn.trace.meta.setdefault("face", {"provider": self.e.face.provider, "segments": []})
        stats: dict[int, dict] = {}
        pending = None
        while True:
            item = pending if pending is not None else await queue.get()
            pending = None
            if item is None:
                return
            seg, pcm, sr, final = item
            parts = [pcm] if pcm is not None else []
            while not final and not queue.empty():
                nxt = queue.get_nowait()
                if nxt is None or nxt[0] != seg:
                    pending = nxt
                    break
                if nxt[1] is not None:
                    parts.append(nxt[1])
                final = nxt[3]
            chunk = np.concatenate(parts) if parts else None
            try:
                r = await self.e.face.run(stream, chunk, sr, turn.emo, final)
            except Exception as exc:  # 这一轮剩下的部分交给前端的频谱口型
                log.warning("第 %d 轮面部动画失败: %s", turn.id, exc)
                meta["error"] = f"{type(exc).__name__}: {exc}"
                return
            st = stats.setdefault(seg, {"frames": 0, "updates": 0, "infer_ms": 0.0, "solve_ms": 0.0, "total_ms": 0.0})
            if r is not None:
                w = np.round(r["weights"] * 255).astype(np.uint8)
                await self._tsend(turn, {"type": "face", "turn": turn.id, "seg": seg, "fps": self.e.face.fps,
                                         "names": self.e.face.names, "start": r["start"], "n": len(w),
                                         "final": r["final"], "w": base64.b64encode(w.tobytes()).decode("ascii")})
                turn.trace.mark("face_first_sent")
                st["frames"] += r["frames"]
                st["updates"] += 1
                for k in ("infer_ms", "solve_ms", "total_ms"):
                    st[k] += r[k]
            if final:
                meta["segments"].append({k: round(v, 1) if isinstance(v, float) else v for k, v in stats.pop(seg).items()})

    async def _send_audio(self, turn: Turn, seg: int, clause: str, pcm: np.ndarray, sr: int, first: bool) -> None:
        if first:
            turn.audio_segments += 1
            await self._tsend(turn, {"type": "audio_seg", "turn": turn.id, "seg": seg, "sr": sr, "text": clause})
        turn.audio_s += len(pcm) / sr
        step = max(1, int(sr * self.p.get("audio_frame_ms", 200) / 1000))
        for i in range(0, len(pcm), step):
            turn.audio_seq += 1
            header = AUDIO_HEADER.pack(AUDIO_MAGIC, turn.id, seg, sr, turn.audio_seq)
            await self._tsend(turn, header + pcm[i:i + step].astype("<i2").tobytes())
            turn.trace.mark("audio_first_sent")

    async def _finalize(self, turn: Turn) -> None:
        if turn.finalized:
            return
        turn.finalized = True
        self._touch()  # 空闲从这一轮说完算起, 角色还在合成、播放时不整理
        if turn.committed.is_set() and turn.text:
            self.since_long = 0 if turn.long_ran else self.since_long + 1  # "后来呢" 接着讲
        if turn.reply_text:
            turn.trace.meta["reply"] = turn.reply_text  # 实际说出口的 (llm.output 是模型原话, 可能被改判或截断)
        if self.e.memory is not None and not turn.trace.interrupted and turn.text and turn.reply_text:
            asyncio.get_running_loop().run_in_executor(None, self._observe, turn)  # 后台写, 不挡下一轮
        if self.channels is not None and not turn.trace.interrupted and turn.text and turn.reply_text:
            self.channels.pet_said(turn.text, turn.reply_text)  # QQ / 微信 / 工作台那边回你时知道桌宠上刚聊了什么
        if self.watch is not None and self.watch.alive and turn.aside is None and turn.source in ("voice", "text") \
                and turn.text and not turn.trace.interrupted and "watch" not in turn.trace.meta:
            self.watch.heard(turn.text, turn.reply_text or "")  # 陪看时你说的 (和她回的): 下一眼带上, 看完记进记忆
        if turn.text and turn.op is not None:
            # 上一轮挂着的记录用这一句补上"用户怎么反应"; 这一轮的记录挂起, 等下一句或断开
            marks = turn.trace.marks
            self.social.on_user_turn(turn.text, marks.get("speech_start") or marks.get("llm_request") or now_ms())
            conf = turn.trace.meta.get("confidence") or {}
            self.social.on_turn_done({
                "t": turn.trace.wall, "key": key_of(turn.op, turn.acts, turn.emo), "op": turn.op,
                "acts": [a.get("do") for a in turn.acts], "emo": turn.emo, "conf": conf.get("overall"),
                "escalated": bool((conf.get("escalated") or {}).get("acts")), "user": turn.text[:60],
                "reply": turn.reply_text[:80], "end_ms": marks.get("client_audio_end") or now_ms(),
            }, turn.trace.interrupted)
        self.turns.pop(turn.id, None)
        if turn.aside is None:  # 陪看那句没有"你说完"的时刻, 不算进延迟统计
            self.stats.add(turn.trace)
        record = turn.trace.to_dict()
        try:
            self.tracelog.write(record)
        except OSError as exc:
            log.warning("写延迟日志失败: %s", exc)
        await self.send({"type": "trace", "trace": record, "stats": self.stats.summary(), "spec": self.spec_stats,
                         "prewarm": self.prewarm_stats})

    async def close(self) -> None:
        if self.watch is not None and self.watch.alive:  # 连接断了: 不陪看了 (取下来的视频删掉)
            with contextlib.suppress(Exception):
                await self.watch.stop("disconnect")
        if self._mem_listener is not None:
            with contextlib.suppress(ValueError):
                self.e.memory.listeners.remove(self._mem_listener)
        if self.channels is not None:
            self.channels.detach(self.send)
        if self._cand_task is not None:
            self._cand_task.cancel()
        if self.room_task is not None:
            self.room_task.cancel()
        self.social.close(now_ms())
        await self._cancel_prewarm()
        await self._drop_spec("disconnect")
        await self.interrupt("disconnect")
        if self.asr is not None:
            self.asr.close()
        if self.echo is not None:
            self.echo.close()
        self.e.slots.release(self.slot)
        for llm in self._retired + ([self.llm] if self.cloud else []):  # 云端模型的连接 (key 随对象一起丢掉)
            with contextlib.suppress(Exception):
                await llm.close()

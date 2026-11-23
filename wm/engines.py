"""按配置创建并加载各引擎. 可选组件 (ASR/TTS/RVC) 加载失败时退回 mock/none, 并在状态里写明原因."""
from __future__ import annotations

import asyncio
import logging

from . import beat
from . import cloud
from .a2f import NoFace, create_face
from .asr import NullASR, create_asr
from .llm import create_llm
from .rvc import NoRVC, create_rvc
from .tts import MockTTS, create_tts

log = logging.getLogger("wm.engines")


class SlotPool:
    """按连接分 llama-server 的槽位 (llm.session_slots): 每条 /ws 连接占一个, 各自的 KV 缓存 (few-shot 前缀 + 自己的历史,
    KV 预热也算在里面) 互不冲掉, 两条连接也能同时生成. 按配置的顺序分, 第一条连接拿到第一个 (默认 0 号, 就是 llm.id_slot,
    只有一条连接时和原来一样). 分完了, 或者 llama-server 没开这么多槽位 (--parallel, 见 limit) 时给 None:
    用默认槽位, 和别的连接共用 (原来的行为). 长回复 (llm.long.id_slot) 和日志整理 (memory.digest.slot) 仍是各自固定的槽位."""

    def __init__(self, slots):
        self.slots = [int(s) for s in slots or []]
        self.used: set[int] = set()
        self.limit: int | None = None  # llama-server 实际开了几个槽位 (/props 的 total_slots); None 是不限 (mock)

    def acquire(self) -> int | None:
        for slot in self.slots:
            if slot not in self.used and (self.limit is None or slot < self.limit):
                self.used.add(slot)
                return slot
        return None

    def release(self, slot: int | None) -> None:
        self.used.discard(slot)

    def status(self) -> dict:
        return {"pool": self.slots, "used": sorted(self.used), "limit": self.limit}


class Engines:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.errors: dict[str, str] = {}
        cloud.configure(cfg["llm"].get("local_api"))  # 本机模型 (provider selfhost) 的默认地址、读超时、思考开关、上下文
        self.llm = create_llm(cfg["llm"])
        self.slots = SlotPool(cfg["llm"].get("session_slots"))
        # 升级契约: 意图置信度低于阈值时请更大的模型复核 (llm.escalate.endpoint 为空就不升级)
        esc = cfg["llm"].get("escalate") or {}
        self.escalate = create_llm({**cfg["llm"], "engine": "llamacpp", "logprobs": 0, **esc}) if esc.get("endpoint") else None
        self.asr = NullASR()
        # 外放回声 (wm/echo.py) 的参考: 系统声音回环, 一个进程一份; 有麦克风帧进来才录, 15 秒没人要就停
        from .echo import DEFAULTS as ECHO_DEFAULTS
        from .loopback import LoopbackHub
        self.echo_conf = {**ECHO_DEFAULTS, **((cfg.get("asr") or {}).get("echo") or {})}
        self.loopback = LoopbackHub(enabled=bool(self.echo_conf["enabled"]))
        self.tts = MockTTS(cfg["tts"])
        self.rvc = NoRVC()
        self.face = NoFace()
        self.memory = None  # 关系记忆层 (Obsidian 库), memory.vault 为空就不开
        self.digest = None  # 空闲时把日志整理成事实 (wm/digest.py), 有记忆层且接了真模型才开
        from .channels import ChannelManager
        self.channels = ChannelManager(cfg, self)  # 外部渠道 (QQ): 访客会话, 和 /ws 的本人会话隔开; channels.qq 默认关
        from .config import resolve_path
        from .social import SocialLog
        self.social = SocialLog(resolve_path(cfg.get("trace", {}).get("log_dir", "logs")))  # 社交后果数据
        # 小窝认知 (wm/cognition/live.py): 桌宠小屋上报她看得到什么, 她以为东西在哪、怎么找; 事件流存在 <log_dir>/cognition/
        self.room = None
        self.room_registry = None
        rc = (cfg.get("room") or {}).get("cognition") or {}
        if rc.get("enabled", True):
            from .cognition import store
            from .cognition.registry import RoomRegistry
            try:
                # 她的经历放在用户数据区 (~/.worldmodel/experience, 卸载不删); 以前写在 logs/cognition/ 的搬过去
                store.migrate(cfg)
                self.room_registry = RoomRegistry(cfg, on_learn=self._learned)
                self.room = self.room_registry.get()  # Existing callers retain the legacy room.
            except Exception as exc:  # 事件流坏了: 这次不带小窝认知, 页面按原来的路走
                self.errors["room"] = f"{type(exc).__name__}: {exc}"
                log.warning("小窝认知加载失败: %s", exc)
        self.llm_ok = False
        self.motion = "procedural"  # 由 app.create_app 按 HAST-SEC 是否可用改写
        self.locomotion: list[str] = []  # 可用的运动匹配风格库 (开放空间行动层), 空表示没生成
        self.interactions = False  # 物体交互动作库 (坐下/站起/拿放) 是否生成
        self.idle = False  # 待机动作库 (StayStill) 是否生成

    async def load(self) -> None:
        loop = asyncio.get_running_loop()
        beat.phase("载入语音识别")  # 每一步告诉看守 (wm/beat.py): 起得慢时它知道在等什么, 真卡住了崩溃记录里也看得出卡在哪
        try:
            asr = create_asr(self.cfg["asr"])
            await loop.run_in_executor(None, asr.load)
            self.asr = asr
        except Exception as exc:  # 缺包或缺模型
            self.errors["asr"] = f"{type(exc).__name__}: {exc}"
            log.warning("ASR 加载失败, 只能用文字输入: %s", exc)
        beat.phase("载入语音合成")
        try:
            tts = create_tts(self.cfg["tts"])
            await tts.load()
            self.tts = tts
        except Exception as exc:
            self.errors["tts"] = f"{type(exc).__name__}: {exc}"
            log.warning("TTS 加载失败, 退回 mock: %s", exc)
        try:
            rvc = create_rvc(self.cfg["rvc"])
            await rvc.load()
            self.rvc = rvc
        except Exception as exc:
            self.errors["rvc"] = f"{type(exc).__name__}: {exc}"
            log.warning("RVC 加载失败, 已关闭: %s", exc)
        beat.phase("载入面部动画 (A2F)")
        try:
            face = create_face(self.cfg.get("face", {"engine": "none"}))
            await face.load()
            self.face = face
        except Exception as exc:  # 缺模型或缺 onnxruntime: 前端退回按频谱估口型
            self.errors["face"] = f"{type(exc).__name__}: {exc}"
            log.warning("面部动画 (A2F) 加载失败, 前端改用频谱口型: %s", exc)
        beat.phase("预热分词、韵律")
        try:
            from .prosody import warmup
            await asyncio.get_running_loop().run_in_executor(None, warmup)
        except Exception as exc:
            log.warning("韵律分析预热失败: %s", exc)
        try:  # 能力闸门和回复侧检查在实时路径上分词 (jieba 第一次用要载入约 1 s)
            from .intent import warmup as intent_warmup
            await asyncio.get_running_loop().run_in_executor(None, intent_warmup)
        except Exception as exc:
            log.warning("分词预热失败: %s", exc)
        mem_cfg = self.cfg.get("memory") or {}
        beat.phase("载入记忆库")
        if mem_cfg.get("vault"):
            try:
                from .memory import Memory
                memory = Memory(mem_cfg)
                await asyncio.get_running_loop().run_in_executor(None, memory.init)
                memory.start_embedder(mem_cfg.get("embed"))  # 语义检索: 后台载入, 好了之前用 BM25
                self.memory = memory
            except Exception as exc:  # 库路径不存在、没装 jieba
                self.errors["memory"] = f"{type(exc).__name__}: {exc}"
                log.warning("记忆层加载失败, 这次不带长期记忆: %s", exc)
        beat.phase("连本机模型")
        self.llm_ok = await self.llm.health()
        if self.escalate is not None:
            await self._warm_escalate()
        if not self.llm_ok:
            log.warning("LLM 不可用: %s (llama-server 没启动? 见 tools/start_llm.py)", getattr(self.llm, "endpoint", ""))

    async def _warm_escalate(self) -> None:
        """复核模型先算一遍固定前缀 (系统提示 + 示例), 之后每次复核只算新增的几十个 token."""
        from . import intent as I
        try:
            await self.escalate.complete(I.build_messages("你好"), schema=I.build_intent_schema(), max_tokens=4)
            self.escalate_ok = True
        except Exception as exc:  # 复核服务没启动: 低置信度时就只标 uncertain
            self.escalate_ok = False
            self.errors["escalate"] = f"{type(exc).__name__}: {exc}"
            log.warning("复核模型不可用 (python tools/start_llm.py --which escalate): %s", exc)

    def _learned(self, text: str) -> None:
        """小窝里攒够的经验 ("杯子不在原来的地方时, 好几次都是在电脑桌上找到的"): 写进日记的「我的理解」 (没开长期记忆就不写)."""
        if self.memory is not None:
            try:
                self.memory.add_understanding(text, source="小窝")
            except OSError as exc:
                log.warning("小窝的经验没写进「我的理解」: %s", exc)

    async def refresh(self) -> None:
        self.llm_ok = await self.llm.health()

    async def probe_slots(self) -> None:
        """新连接分槽位之前问一下 llama-server 开了几个槽位 (改过 --parallel 重启也跟得上). mock 没有这个限制."""
        count = getattr(self.llm, "slot_count", None)
        if count is not None:
            self.slots.limit = await count()

    def mic_processing(self) -> dict | None:
        """页面取麦克风时要浏览器做哪些处理 (hello 的 mic; wm/echo.py 的 mic_processing). 这里去不了外放回声时 None."""
        from .echo import mic_processing
        return mic_processing(self.echo_conf, self.loopback.available())

    def status(self) -> dict:
        return {
            "llm": {"engine": self.llm.name, "label": self.llm.label, "ok": self.llm_ok, "slots": self.slots.status()},
            "asr": {"engine": self.asr.name, "ok": self.asr.name != "none", "echo": self.loopback.status()},
            "tts": {"engine": self.tts.name, "sample_rate": getattr(self.tts, "sample_rate", None)},
            "rvc": {"engine": self.rvc.name, "enabled": self.rvc.enabled},
            "face": {"engine": self.face.name, "provider": self.face.provider},
            "escalate": {"label": self.escalate.label if self.escalate else None, "ok": getattr(self, "escalate_ok", False)},
            "memory": {"enabled": self.memory is not None, "vault": str(self.memory.dir) if self.memory else None,
                       "digest": self.digest.stats if self.digest else None,
                       "half_life_days": self.memory.half_life if self.memory else None,
                       "confirm": ("candidates" if self.memory.confirm else "auto") if self.memory else None,
                       "candidates": len(self.memory.candidates()) if self.memory else None,
                       "diary_gate": (self.cfg.get("memory") or {}).get("diary_gate", "off") if self.memory else None,
                       "embed": ({"engine": self.memory.embedder.label if self.memory.embedder else None,
                                  "vectors": len(self.memory._vec), "min_sim": self.memory.min_sim,
                                  "error": self.memory.embed_error} if self.memory else None)},
            "motion": {"engine": self.motion, "locomotion": self.locomotion, "interactions": self.interactions,
                       "idle": self.idle},
            "channels": {"qq": self.channels.public_status()},
            "room": self.room.status() if self.room else None,
            "room_scenes": self.room_registry.status() if self.room_registry is not None else {},
            "errors": self.errors,
        }

    async def close(self) -> None:
        self.loopback.stop_flag.set()  # 录系统声音的线程自己停 (daemon, 不等)
        await self.llm.close()
        for engine in (self.tts, self.rvc):
            session = getattr(engine, "_session", None)
            if session is not None and not session.closed:
                await session.close()

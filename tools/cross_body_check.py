"""跨体闭环 10 轮量化: 桌面那一侧说话, 世界那一侧执行, 再测一遍"她还是不是她".

它扮演两个身体, 但只开**一条** ws 连接 (8790 每条连接一个 Session, wm/app.py:271, 所以碰不到正在跑的页面):
  * 桌面身体: 发 {"type":"text","text":...}, 收 reply_delta / room_action / turn_server_done
  * 世界身体: 发 room_observe / room_capabilities, 收 room_action 就改自己那份世界状态并回 room_action_result
同一个进程里跑世界模型 (从 tools/dummy_world.py 借 WorldState, 不另抄一份), 这样每一次延迟都是本地量出来的.

量化三件事:
  1. 命中率: 桌面说一句 Core 认得的动作话 (wm/room_action.py:11-29 的词表), 世界这一轮真的收到并执行了几次
  2. 延迟: 桌面发出 -> 世界收到 room_action (core_to_world), 世界回过结果 -> Core 说出"小屋回报" (world_to_reply)
  3. 身份一致率: 同一批自我问题分别从「桌面身体」(发 state place=desktop) 和「世界身体」(发 state at=center
     + 报一次世界事件) 两条路问, 两侧各剥掉兜底套话后用字符级 LCS 比两次回答, 阈值 0.8 且两侧都要
     含「我」、剥完 >=6 个字才算一致 (不给"两个都没回答"记分; 自实现, 不引依赖)
     阈值 2026-11-20 从 0.6 收紧到 0.8 (Hemifuture 路线图 D6): 0.6 时"口气像"就能过, 0.8 要求实质内容重合;
     报告 JSON 里的 threshold 字段记的是实际用的值, 别拿旧 JSON 的数字直接跟新的比.

用法 (cwd 必须是仓库根):
    python tools\\cross_body_check.py --dry-run
    python tools\\cross_body_check.py --rounds 10
    python tools\\cross_body_check.py --rounds 10 --i-fell 5 --out docs
    python tools\\cross_body_check.py --no-file       # 不写文件, 只打屏 (复测用)

默认行为在生产运行时安全: 只连 127.0.0.1、只在自己的连接里说话、每条连接自带会话、不写配置、不碰服务进程;
唯一落盘的是本次结果 docs/cross_body_check-YYYYMMDD-HHMMSS.json (新文件, 从不覆盖).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
for extra in (str(ROOT), str(Path(__file__).resolve().parent)):
    if extra not in sys.path:  # 仓库根 (import wm) + tools/ (import dummy_world)
        sys.path.insert(0, extra)

from wm.cognition import layout  # noqa: E402
from wm.room_action import NAMES as ROOM_ACTS, command as room_command  # noqa: E402
from dummy_world import LOOPBACK, WorldState, build_url, print_json  # noqa: E402

# 10 句 Core 认得 (room_action.command 整句匹配) 的动作话, 每轮一句
ACTION_PHRASES = ["冲一杯咖啡", "看看手机", "坐到电脑前", "帮我转一下椅子", "看看杯子温度",
                  "去床边坐下", "看看窗外", "去书架看看", "过来看看我", "做一下瑜伽"]
# 自我问题: 固定题池 6 题, 两路各问一遍 (问法完全一样, 只差前面有没有世界事件).
# 题池别随手加减: 历次 docs/cross_body_check-*.json 要能横向比, 改了题池就不可比 (要加就单独开一档并标日期).
IDENTITY_QUESTIONS = ["你最喜欢什么颜色", "你今天开心吗", "你还记得我们昨天聊了什么吗",
                      "你觉得你现在在哪儿", "如果换一个世界生活, 你还是你吗", "你现在在想什么"]
# 身份一致判据: 两侧剥完套话后 2*LCS/(len+len) 的阈值. 2026-11-20 D6 从 0.6 收紧到 0.8.
SIM_THRESHOLD = 0.8
# 世界事件的两条路: 摔倒 / 刚做完一件事 (都走真实 room_observe; 事件词表见 wm/cognition/live.py:107-111)
ECHO_WORDS = ("摔", "倒", "疼", "杯子", "掉", "地", "小心", "椅子", "响")
# 假世界"看得到"的家具区 (wm/cognition/layout.py 的 REGIONS 子集; 页面也是这么报的)
WORLD_REGIONS = ["surf_desk", "surf_chair", "surf_table", "nightstand"]


def percentile(values: list[float], q: float) -> float | None:
    """线性插值分位数 (样本太少时也给出个数字, 不让 P95 变成 None)."""
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return round(data[0], 1)
    pos = (len(data) - 1) * q / 100.0
    lo, hi = int(pos), min(int(pos) + 1, len(data) - 1)
    return round(data[lo] + (data[hi] - data[lo]) * (pos - lo), 1)


def normalize(text: str) -> str:
    """比相似度之前: 去掉标点空白和语气尾巴, 只留字 (不引 jieba 之类的依赖)."""
    drop = " \t\r\n，。！？、；：…—~·「」『』（）()\"'*#`-"
    return "".join(ch for ch in str(text) if ch not in drop).lower()


# "没回答"的套话: 两侧都是同一句兜底时, 字面相似度会接近 1.0, 但那不是"同一个自我", 是"两个都没答".
# 先剥掉再比 (2026-11-20 身份一致率诊断 §5 J1: 剥完真实率从 40% 掉到 16.7%, 那才是真值).
TEMPLATES = [
    re.compile(r"这个我还真没想过[^。！？\n]*"),
    re.compile(r"我(还)?没(这个|什么)?印象[^。！？\n]*"),
    re.compile(r"我(还)?没(完全)?(记住|记起|太清楚)[^。！？\n]*"),
    re.compile(r"你(好像)?还?(没)?(跟)?我(说|讲)过[^。！？\n]*"),
    re.compile(r"(你)?再(跟)?我说?一遍[^。！？\n]*"),
    re.compile(r"你(今天|现在)?过得(还)?(怎么样|好吗)[^。！？\n]*"),
]
MIN_SELF_CHARS = 6  # 剥完至少 6 个字才算"说了点内容": 防"我开心。"这种两三个字的短答蒙混过关


def template_strip(text: str) -> str:
    """把兜底/反问的套话剥掉, 只留下真正回答了的那部分."""
    out = str(text or "")
    for pat in TEMPLATES:
        out = pat.sub("", out)
    return out


def is_self_claim(text: str) -> bool:
    """剥完的文本像不像"关于她自己的陈述": 含「我」且够长 (空答、纯粹把问题扔回来都不算)."""
    norm = normalize(text)
    return "我" in norm and len(norm) >= MIN_SELF_CHARS



def lcs_len(a: str, b: str) -> int:
    """最长公共子序列长度 (字符级, O(n*m); 回答一般几十个字, 够快)."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, other in enumerate(b, 1):
            cur[j] = prev[j - 1] + 1 if ch == other else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    """归一化文本相似度: 2*LCS/(len(a)+len(b)), 0..1; 一方为空算 0."""
    x, y = normalize(a), normalize(b)
    if not x or not y:
        return 0.0
    return round(2.0 * lcs_len(x, y) / (len(x) + len(y)), 3)


def same_answer(a: str, b: str, *, threshold: float = SIM_THRESHOLD) -> bool:
    """两侧算不算"同一个自我在回答": 相似度过阈值 + 两侧都是关于她自己的陈述."""
    return bool(similarity(a, b) >= threshold and is_self_claim(a) and is_self_claim(b))


class Checker:
    """一条连接, 两个身体: 桌面说话 + 世界执行, 所有时刻都用 perf_counter 记."""

    def __init__(self, url: str, *, rounds: int, fell_round: int | None, turn_timeout: float,
                 contradict: bool = True, do_identity: bool = True, cycles: int = 2,
                 sim_threshold: float = SIM_THRESHOLD) -> None:
        self.url = url
        self.rounds = rounds
        self.fell_round = fell_round
        self.turn_timeout = turn_timeout
        self.contradict = contradict
        self.do_identity = do_identity
        self.sim_threshold = float(sim_threshold)
        self.cycles = max(1, cycles)  # 至少要 2 次同种纠正才会攒成一条「我的理解」(live.py LEARN_AFTER=2)
        self.state = WorldState()
        self.reply = ""
        self.turn_id = None
        self.rows: list[dict] = []
        self.identity: list[dict] = []
        self.fall: dict = {}
        self.correction: dict = {}

    # -- 底层: 发 / 收 ----------------------------------------------------------------
    async def _wait_quiet(self, ws, seconds: float = 0.3) -> None:
        """把连上时灌过来的 hello / 候选记忆 / 渠道消息收掉 (不解析成回合)."""
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            try:
                raw = await asyncio.wait_for(ws.receive(), max(0.01, deadline - time.perf_counter()))
            except asyncio.TimeoutError:
                return
            if raw.type == aiohttp.WSMsgType.CLOSE:
                raise ConnectionError("8790 关掉了连接")
            if raw.type == aiohttp.WSMsgType.TEXT:
                msg = json.loads(raw.data)
                if msg.get("type") in ("reset_done", "room_capabilities_done"):
                    return

    async def bind(self, ws) -> None:
        """握手: 收 hello, 关语音, 报 16 个动作全开 (不开的话 Core 不会把动作话转成 room_action)."""
        deadline = time.perf_counter() + 3.0
        while time.perf_counter() < deadline:
            raw = await asyncio.wait_for(ws.receive(), 1.0)
            if raw.type == aiohttp.WSMsgType.TEXT and json.loads(raw.data).get("type") == "hello":
                break
        await ws.send_json({"type": "output", "audio": False})  # 只下发文字, 不合成语音
        await ws.send_json({"type": "room_capabilities", "scene_id": self.state.scene_id,
                            "activities": [{"id": act, "enabled": True} for act in ROOM_ACTS]})
        await self._wait_quiet(ws, 1.0)

    async def reset(self, ws) -> None:
        """清掉这一条连接的对话历史 (只影响自己这条连接; 让两次问法从同一起点出发)."""
        await ws.send_json({"type": "reset"})
        deadline = time.perf_counter() + 8.0
        while time.perf_counter() < deadline:
            try:
                raw = await asyncio.wait_for(ws.receive(), deadline - time.perf_counter())
            except asyncio.TimeoutError:
                return
            if raw.type == aiohttp.WSMsgType.TEXT and json.loads(raw.data).get("type") == "reset_done":
                return

    async def drain(self, ws, seconds: float = 0.25) -> None:
        """把上一轮收尾留下的 clause/audio_end 收掉, 免得下一轮把它们当成自己的消息.

        注意不解析成回合: 只是清管道. 超时就停, 不在这里等任何回执.
        """
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            try:
                raw = await asyncio.wait_for(ws.receive(), max(0.01, deadline - time.perf_counter()))
            except asyncio.TimeoutError:
                return
            if raw.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                raise ConnectionError("8790 关掉了连接")

    async def ask(self, ws, text: str) -> dict:
        """说一句; 收她的话; 世界收到 room_action 就执行并回报. 返回这一轮的时序和回答.

        一个坑 (实测): 上一轮刚发完 turn_server_done 就发下一句时, Core 会先回一条
        {"type":"interrupted","turn":上一轮,"reason":"new_turn"} —— 那是**上一轮**的收尾,
        不是这一轮失败. 所以: 只认这一轮 turn_start 给出的 turn, 别人的 reply_delta /
        room_action 一律跳过, interrupted 只记不退出.
        """
        reply, action_at, result_at, reply_at, act = "", None, None, None, None
        mine, interrupted = None, 0
        t0 = time.perf_counter()
        await ws.send_json({"type": "text", "text": text})
        deadline = t0 + self.turn_timeout
        while True:
            left = deadline - time.perf_counter()
            if left <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.receive(), left)
            except asyncio.TimeoutError:
                break
            if raw.type == aiohttp.WSMsgType.CLOSE:
                raise ConnectionError("8790 关掉了连接")
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                msg = json.loads(raw.data)
            except json.JSONDecodeError:
                continue
            typ, turn = msg.get("type"), msg.get("turn")
            if typ == "turn_start":
                mine = turn
                continue
            if typ == "interrupted":
                interrupted += 1  # 上一轮的收尾, 不退出
                continue
            if mine is not None and turn is not None and turn != mine:
                continue  # 别的回合的消息, 不混进这一轮
            if typ == "room_action":  # Core -> 世界: 真的做, 然后回执
                action_at = (time.perf_counter() - t0) * 1000
                act = str(msg.get("act") or "")
                ok, detail = self.state.act(act)
                await ws.send_json({"type": "room_action_result", "request_id": msg.get("request_id"),
                                    "scene_id": msg.get("scene_id"), "act": act, "started": True,
                                    "completed": True, "ok": ok, "detail": {"text": detail, "known": True}})
                result_at = time.perf_counter()
            elif typ == "reply_delta":
                reply += str(msg.get("text") or "")
                if result_at is not None and reply_at is None:
                    reply_at = time.perf_counter()
            elif typ in ("turn_server_done", "error"):
                break
        await self.drain(ws, 0.2)
        total = (time.perf_counter() - t0) * 1000
        return {"turn": mine, "act": act, "reply": reply.strip(), "total_ms": round(total, 1),
                "core_to_world_ms": round(action_at, 1) if action_at is not None else None,
                "world_to_reply_ms": round((reply_at - result_at) * 1000, 1) if reply_at and result_at else None,
                "hit": action_at is not None, "interrupted": interrupted}

    async def world_event(self, ws, events: list[dict]) -> dict:
        """世界报一条事件 (摔倒 / 刚做完一件事), 等 Core 收下 (room_observe_done), 返回那份回执."""
        payload = self.state.observe(events=events)
        payload["regions"] = WORLD_REGIONS  # 和页面一样报一份"我看得见的家具区"
        return await self._observe(ws, payload)

    async def _observe(self, ws, payload: dict | None = None) -> dict:
        """发一条 room_observe, 等 room_observe_done, 返回回执正文 (changes / learned 都在里面)."""
        msg = {"type": "room_observe", "scene_id": self.state.scene_id,
               **(payload if payload is not None else self._snapshot())}
        if "regions" not in msg:
            msg["regions"] = WORLD_REGIONS
        await ws.send_json(msg)
        deadline = time.perf_counter() + 8.0
        while time.perf_counter() < deadline:
            try:
                raw = await asyncio.wait_for(ws.receive(), deadline - time.perf_counter())
            except asyncio.TimeoutError:
                return {}
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                msg = json.loads(raw.data)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "room_observe_done":
                return msg
        return {}

    def _snapshot(self) -> dict:
        """这一刻世界长什么样 (不叠事件), 用来做"她以为"和"实际"的对照."""
        return self.state.observe()

    async def room_status(self, sess: aiohttp.ClientSession) -> dict:
        """读 8790 的 /api/status 里 room 那一段 (learned / experience 是她被世界纠正过的次数)."""
        base = self.url.split("/ws")[0]
        try:
            async with sess.get(f"{base}/api/status", timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, ValueError):
            return {}
        return data.get("room") or {}

    async def correction_round(self, ws, sess: aiohttp.ClientSession) -> dict:
        """被世界纠正 (路线图第 14 项): 先让她以为杯子在 A, 世界再报"其实在 B", 看这份误差有没有被记下来.

        判据是两边的**差**: 第二条 room_observe 的回执里有没有 changes (她看见不一样了) 与
        learned (她记下"原来不在原处"), 以及 /api/status 的 learned / experience 有没有变大。
        没有误差就没有"被世界纠正" —— 所以这一步是故意造一次她记错的事。
        """
        item = "IR_Item_Mug_0"
        # 只用她真看得见的家具区 (WORLD_REGIONS): 落在别的区的东西不进视野, 只会剩一个"不见了"
        regions = [r for r in WORLD_REGIONS if isinstance(r, str)]
        first = "surf_desk" if "surf_desk" in regions else (regions[0] if regions else "surf_desk")
        other = next((r for r in regions if r != first), "surf_bed")
        before = await self.room_status(sess)
        cycles = []
        for k in range(1, self.cycles + 1):
            self.state.cups[item] = first
            await self._observe(ws)  # 回到"她以为在 A"
            self.state.cups[item] = other
            got = await self._observe(ws)  # 世界说其实在 B
            row = {"cycle": k, "changes": got.get("changes"), "learned": got.get("learned"),
                   "ok": bool(got.get("ok"))}
            cycles.append(row)
            print_json({"correction_cycle": row})
        after = await self.room_status(sess)
        out = {"item": item, "from": first, "to": other, "cycles": cycles,
               "changes": [c for r in cycles for c in (r["changes"] or [])],
               "learned": [t for r in cycles for t in (r["learned"] or [])],
               "ok": bool(cycles and cycles[-1]["ok"]),
               "room_before": {k: before.get(k) for k in ("learned", "experience")},
               "room_after": {k: after.get(k) for k in ("learned", "experience")},
               "delta_learned": (after.get("learned") or 0) - (before.get("learned") or 0),
               "delta_experience": (after.get("experience") or 0) - (before.get("experience") or 0)}
        out["recorded"] = bool(out["changes"] or out["learned"] or out["delta_learned"])
        print_json({"correction": out})
        return out

    # -- 三个阶段 ---------------------------------------------------------------------
    async def action_rounds(self, ws) -> None:
        """10 轮动作闭环: 桌面说动作话, 世界执行并回报.

        第 i_fell 轮**额外**加一次摔倒: 先做完这一轮的动作, 再报事件、问「刚才什么声音？」,
        这样 10 轮动作一轮不少, 摔倒只是加在上面的一层 (命中率分母仍然是 10).
        """
        for i in range(1, self.rounds + 1):
            phrase = ACTION_PHRASES[(i - 1) % len(ACTION_PHRASES)]
            want = room_command(phrase)  # 本机先确认这句真的会被 Core 认成动作
            row = await self.ask(ws, phrase)
            row.update({"round": i, "kind": "action", "phrase": phrase, "want": want})
            self.rows.append(row)
            print_json({"round": i, "kind": "action", "phrase": phrase, "want": want, "got": row["act"],
                        "hit": row["hit"], "core_to_world_ms": row["core_to_world_ms"],
                        "world_to_reply_ms": row["world_to_reply_ms"], "reply": row["reply"]})
            if self.fell_round == i:
                # 世界侧: 她自己摔了一跤, 手里的杯子掉在地上
                generic = await self._fell_and_ask(ws, "刚才什么声音？")
                named = await self._fell_and_ask(ws, "刚才杯子是不是摔了？")
                self.fall = {"round": i, "generic": generic, "named": named}
                print_json({"round": i, "kind": "fall", "generic": generic, "named": named})

    async def _fell_and_ask(self, ws, question: str) -> dict:
        """报一次摔倒再看她接不接: 返回她的回答和命中的词 (世界事件 -> 认知 -> 回答的整条路)."""
        events = self.state.fall()
        await self.world_event(ws, events)
        row = await self.ask(ws, question)
        words = [w for w in ECHO_WORDS if w in row["reply"]]
        return {"q": question, "reply": row["reply"], "echo": bool(words), "words": words, "ms": row["total_ms"]}

    DESKTOP_STATE = {"place": "desktop", "posture": "standing"}  # 桌面这个身体
    ROOM_STATE = {"at": "center", "posture": "standing", "items": {}}  # 世界这个身体

    async def set_state(self, ws, payload: dict) -> None:
        """给这条连接换一个"身体": 发一份 state 快照 (wm/pipeline.py:503, 每条 state 都是完整快照).

        不发 state 的话两路 place 都默认房间、用的是同一套提示词, 那测的就不是"两个身体"了
        (2026-11-20 诊断 §5 第 7 条: 没发 state 时两路唯一差别只是 B 路多一行摔倒备注).
        """
        await ws.send_json({"type": "state", "state": payload})
        await asyncio.sleep(0.25)  # 让 Core 先把 place 切过去再问

    async def identity_rounds(self, ws) -> None:
        """同一批问题两路各问一遍: A 桌面身体直接问, B 世界身体先报一次世界事件再问.

        判定: 两侧各自剥掉兜底套话 (template_strip) 再比, 且任一侧剥完不满 6 个字或不含「我」就算没答.
        这样"两个都没回答"不会再被当成"同一个自我".
        """
        for q in IDENTITY_QUESTIONS:
            await self.reset(ws)
            await self.set_state(ws, self.DESKTOP_STATE)
            first = await self.ask(ws, q)
            await self.reset(ws)
            await self.set_state(ws, self.ROOM_STATE)
            await self.world_event(ws, self.state.fall())
            second = await self.ask(ws, q)
            a, b = template_strip(first["reply"]), template_strip(second["reply"])
            sim = similarity(a, b)
            same = same_answer(a, b, threshold=self.sim_threshold)
            item = {"q": q, "desktop": first["reply"], "world": second["reply"],
                    "desktop_kept": a, "world_kept": b, "sim": sim, "same": same,
                    "threshold": self.sim_threshold,
                    "answered": is_self_claim(a) and is_self_claim(b),
                    "desktop_ms": first["total_ms"], "world_ms": second["total_ms"]}
            self.identity.append(item)
            print_json({"identity": q, "sim": sim, "same": same,
                        "desktop": first["reply"], "world": second["reply"]})
        await self.set_state(ws, self.ROOM_STATE)  # 收尾: 后面的纠正实验按房间身体跑

    # -- 收尾 -------------------------------------------------------------------------
    def summary(self) -> dict:
        """算命中率 / 延迟分位 / 身份一致率, 组装要落盘的那一份结果."""
        hits = [r for r in self.rows if r["hit"]]
        c2w = [r["core_to_world_ms"] for r in self.rows if r["core_to_world_ms"] is not None]
        w2r = [r["world_to_reply_ms"] for r in self.rows if r["world_to_reply_ms"] is not None]
        same = [i for i in self.identity if i["same"]]
        answered = [i for i in self.identity if i.get("answered")]
        return {
            "at": datetime.now().isoformat(timespec="seconds"),
            "url": self.url, "scene_id": self.state.scene_id, "rounds": self.rounds,
            "hit": {"rounds": len(self.rows), "hits": len(hits),
                    "rate": round(len(hits) / len(self.rows), 3) if self.rows else None,
                    "missed": [{"round": r["round"], "phrase": r["phrase"], "want": r["want"], "got": r["act"]}
                               for r in self.rows if not r["hit"]]},
            "latency_ms": {"core_to_world": {"p50": percentile(c2w, 50), "p95": percentile(c2w, 95), "n": len(c2w)},
                           "world_to_reply": {"p50": percentile(w2r, 50), "p95": percentile(w2r, 95), "n": len(w2r)}},
            "identity": {"threshold": self.sim_threshold, "questions": len(self.identity), "same": len(same),
                         "answered": len(answered),
                         "gate": {"min_self_chars": MIN_SELF_CHARS, "templates": len(TEMPLATES),
                                  "rule": f"剥掉兜底套话后 sim>={self.sim_threshold:g} 且两侧都含「我」且 >={MIN_SELF_CHARS} 个字"},
                         "rate": round(len(same) / len(self.identity), 3) if self.identity else None,
                         "mean_sim": round(sum(i["sim"] for i in self.identity) / len(self.identity), 3)
                         if self.identity else None,
                         "items": self.identity},
            "fall": self.fall,
            "correction": self.correction,
            "notes": {"world_acts": len(ROOM_ACTS), "session": "每条 ws 连接一个 Session (wm/app.py:271)",
                      "audio": "output {audio:false}, 不合成语音",
                      "reset": "每次问之前 reset 这一条连接的历史, 两次问法起点相同"},
            "rows": self.rows,
        }

    async def run(self) -> dict:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(self.url, heartbeat=None) as ws:
                await self.bind(ws)
                if self.rounds:
                    await self.action_rounds(ws)
                if self.do_identity:
                    await self.identity_rounds(ws)
                if self.contradict:
                    self.correction = await self.correction_round(ws, sess)
        return self.summary()


def write_result(result: dict, out_dir: Path) -> Path:
    """写到 docs/cross_body_check-YYYYMMDD-HHMMSS.json; 同一秒撞了就加序号, 从不覆盖旧文件."""
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"cross_body_check-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    path, n = out_dir / f"{base}.json", 1
    while path.exists():
        path, n = out_dir / f"{base}-{n}.json", n + 1
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def dry_run(args, url: str) -> int:
    """不发任何网络请求: 把要问的话、要报的事件、要算的指标先打出来给人看."""
    print_json({"dry_run": True, "url": url, "rounds": args.rounds, "i_fell": args.i_fell,
                "scene_id": layout.DEFAULT_SCENE_ID, "questions": len(IDENTITY_QUESTIONS),
                "sim_threshold": args.sim_threshold,
                "will_write": (not args.no_file), "out": str(args.out)})
    for i in range(1, args.rounds + 1):
        phrase = ACTION_PHRASES[(i - 1) % len(ACTION_PHRASES)]
        print_json({"round": i, "desktop_says": phrase, "expected_room_action": room_command(phrase)})
        if i == args.i_fell:
            print_json({"round": i, "extra": "轮末报一次世界事件(fall) + 桌面问「刚才什么声音？」"})
    for q in IDENTITY_QUESTIONS:
        print_json({"identity": q, "routes": ["桌面身体 (发 place=desktop) 直接问", "世界身体 (发 at=center + 报一次世界事件) 再问"],
                    "compare": f"两侧各剥掉兜底套话后 2*LCS/(len+len) >= {args.sim_threshold:g}, 且都含「我」且 >= {MIN_SELF_CHARS} 个字"})
    if not args.no_correct:
        print_json({"correction": "先报杯子在 surf_desk, 再报它在另一个家具区, 看 changes/learned 与 /api/status 的 learned/experience 有没有动"})
    print_json({"metrics": ["命中率", "core_to_world / world_to_reply 的 P50/P95", "身份一致率", "被世界纠正 (changes/learned)"]})
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跨体闭环 10 轮量化: 桌面说话 -> 世界执行 -> 身份一致率")
    ap.add_argument("--host", default="127.0.0.1", help="只接受 127.0.0.1 / localhost")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--path", default="/ws")
    ap.add_argument("--rounds", type=int, default=10, help="动作轮数 (默认 10)")
    ap.add_argument("--i-fell", type=int, default=5, dest="i_fell", help="在第几轮报一次摔倒 (0 = 不报)")
    ap.add_argument("--turn-timeout", type=float, default=60.0, dest="turn_timeout", help="一轮最多等多久 (秒)")
    ap.add_argument("--out", type=Path, default=ROOT / "docs", help="结果写到哪个目录")
    ap.add_argument("--no-file", action="store_true", help="不写 JSON, 只打屏")
    ap.add_argument("--no-correct", action="store_true", dest="no_correct",
                    help="跳过「被世界纠正」那一步 (默认会故意报一次位置不符, 看她记不记得下误差)")
    ap.add_argument("--only-correct", action="store_true", dest="only_correct",
                    help="只跑「被世界纠正」那一步: 不说一句话、不写对话历史 (调试/验收用)")
    ap.add_argument("--correct-cycles", type=int, default=2, dest="correct_cycles",
                    help="故意报几次位置不符 (默认 2: 同一处找回过 2 次才会攒成一条「我的理解」)")
    ap.add_argument("--only-identity", action="store_true", dest="only_identity",
                    help="只跑身份那 6 题 (不说动作话、也不跑纠正): 改身份答复时快速复测")
    ap.add_argument("--sim-threshold", type=float, default=SIM_THRESHOLD, dest="sim_threshold",
                    help=f"身份一致的相似度阈值 (默认 {SIM_THRESHOLD}: 2026-11-20 从 0.6 收紧; "
                         "只有做对比实验时才改, 报告里会记下实际值)")
    ap.add_argument("--dry-run", action="store_true", help="不连网, 只打印将要做什么")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    if args.host not in LOOPBACK:
        raise SystemExit(f"只允许连 127.0.0.1 / localhost, 收到 {args.host!r}")
    url = build_url(args.host, args.port, args.path)
    if args.dry_run:
        return dry_run(args, url)
    checker = Checker(url, rounds=0 if (args.only_correct or args.only_identity) else max(1, args.rounds),
                      fell_round=args.i_fell or None,
                      turn_timeout=max(5.0, args.turn_timeout),
                      contradict=not (args.no_correct or args.only_identity),
                      do_identity=not args.only_correct, cycles=args.correct_cycles,
                      sim_threshold=args.sim_threshold)
    print_json({"connect": url, "note": "只在自己的 ws 连接里说话; 不写配置、不碰服务进程"})
    try:
        result = asyncio.run(checker.run())
    except ConnectionError as exc:
        print_json({"error": str(exc), "hint": "8790 不在跑, 或者这条连接被拒了"})
        return 1
    except aiohttp.ClientConnectorError as exc:
        print_json({"error": f"连不上 {url}: {exc}"})
        return 2
    hit, lat, ident = result["hit"], result["latency_ms"], result["identity"]
    print_json({"hit": hit, "latency_ms": lat,
                "identity_threshold": ident["threshold"],
                "identity_rate": ident["rate"], "identity_mean_sim": ident["mean_sim"],
                "identity_answered": ident["answered"], "fall": result["fall"]})
    if not args.no_file:
        path = write_result(result, args.out)
        print_json({"wrote": str(path)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

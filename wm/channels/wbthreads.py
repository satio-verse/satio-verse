"""主人在 QQ / 微信上让她"用工作台…"办的事: 一件事一条线 (2026-11-01 用户: "根据 ReActive 论文、仓库和调研,
继续完善、丰富我们的桌宠项目").

论文 (ReActive is All PA Need) 第 2 节 Router / Worker 两层、3.2 节订阅与直通、4.2 节多信道交互, 落到这里是:
- QQ / 微信上的她是 Router (跟你说话、接活), 工作台会话是 Worker (各自独立的上下文干活, 做完把结论交回来).
- 一件新的事开一个新会话, 不相干的事不串上下文; 同一件事 DUP_S 秒内又说一遍、那边还在做, 不再开 (重复事件不重复建任务).
- 线订阅那个会话的结束事件 (done): 做完了她回聊天里说一声 (IM 上低打扰: 一两句结论, 不贴过程); 三层验收没过就照实说
  "那边说做完了, 但核对没对上", 到上限停了说停在哪, 出错说卡在哪; 你在电脑上按停的、服务端重启停下的不发.
  做完了但还排着要接着做的 (你中途补充、没来得及交给这一轮的)、她起的后台作业还在跑的, 先不说, 等真做完.
- 你在 QQ 上引用那条回报再说话 = 这件事的后续, 直接交给那个会话 (直通, 不再经过她判断): 正在做就加到这一轮,
  做完了接着做一轮. 只回一句"谢谢""辛苦了"的不算, 照常跟她聊.
- 她在 QQ / 微信上回你时的提示里有一行"工作台上帮主人办的事现在怎样" (状态来自工作台那边, 不靠她记): 你问"做完了吗"
  她答得上来; 回报没发出去的 (QQ 没连上) 也写在这行里, 你下次找她时她自己提.
线和回报的消息编号存在 asks.json 旁边 (只有看守起的正式服务端才存), 重启以后接着认引用; 那时还在做的算"停在半路".

长期目标 (2026-11-01 用户贴的 ChatGPT dot: "一直在后台推进你给的目标, 要决策或做完了主动找你"; 升级方案顺序的第 2 件):
主人在聊天里交给她一件要持续一段时间的事, 建成一个持续任务 (wm/agent/resident.py, 有预算, 分几轮), 也是一条线 (kind=goal).
这条线不按每一轮的结束事件回报 (一轮"没变化"不打扰你), 只在持续任务有回报时 (resident.watchers -> goal_event) 说:
做完了、要你定、卡住了 (含花到预算)、她说这一轮有值得马上告诉你的进展 (tell). 引用这些回报再说话: 这一轮正在做就加进去;
等你、卡住、暂停的接着排下一轮 (你的话下一轮开头交给她); 说"先别做了""算了"就暂停; 做完了的再说就当一件普通的后续.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from ..agent import activity as AV
from .outbound import cap

log = logging.getLogger(__name__)

KEEP = 8           # 最多记几条线 (更早的不再订阅、不再认引用)
QUOTES_KEEP = 64   # 回报的消息编号最多记几个
DUP_S = 300        # 同一件事多久内又说一遍、那边还在做, 就不再开新的
SHOW_S = 24 * 3600  # 做完多久以内的还写进她的提示
GIST = 80          # 回报里引她最后那段话的多少字
CHATTER = re.compile(r"^(谢谢|多谢|谢啦|辛苦|好的?|收到|知道了|嗯+|哦+|噢+|ok|OK|行|可以|棒|赞|厉害|nice|👍|🙏|❤️?)+"
                     r"[啦了呀哦啊吖~～!！。.,，\s]*$")
STATES = {"running", "done", "needs_check", "budget", "failed", "stopped", "interrupted", "waiting", "blocked", "paused"}
STOP_GOAL = re.compile(r"^\s*(先)?(别|不用|不要)(再)?(做|弄|管|盯|跟|看|查)?了|^\s*(停|暂停|停下|停一下|取消|算了)")
GOAL_STATE = {"completed": "done", "waiting": "waiting", "blocked": "blocked", "paused": "paused", "interrupted": "interrupted"}


def gist(text: str, n: int = GIST) -> str:
    """工作台最后那段话的开头 (去掉代码块、标题和列表记号、加粗), 给 QQ 上的一两句回报."""
    text = re.sub(r"```.*?(```|$)", " ", str(text or ""), flags=re.S)
    text = re.sub(r"^\s*(#+|>|[-*+]|\d+[.)、])\s*", "", text, flags=re.M)
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"^(好的|好了|做完了|完成了|已完成|搞定了?)[，,：:。！!~～\s]*", "", " ".join(text.split()))  # 回报里已经说了做完了
    return cap(text, n)


def final_text(s) -> str:
    """这一轮她最后说的话 (最后一条带文字的回答)."""
    for m in reversed(getattr(s, "messages", None) or []):
        if m.get("role") == "assistant" and str(m.get("content") or "").strip():
            return str(m["content"])
    return ""


def outcome(run: dict) -> str:
    """结束事件里的这一轮 -> 线的状态."""
    state = run.get("state")
    if state == "completed":
        return "needs_check" if (run.get("check") or {}).get("verdict") == "needs_check" else "done"
    return {"cancelled": "stopped", "interrupted": "interrupted", "budget_exhausted": "budget"}.get(state, "failed")


def brief(run: dict, final: str) -> str:
    """这一轮的结论 (一句): 做完了是她最后那段话的开头, 核对没过是对不上的地方, 停了、出错是原因."""
    state = outcome(run)
    if state == "needs_check":
        return cap("；".join((run.get("check") or {}).get("reasons") or []) or "有对不上的地方", 60)
    if state == "done":
        return gist(final)
    return cap(str(run.get("reason") or ""), 60)


def report_text(goal: str, run: dict, final: str) -> str | None:
    """这一轮结束后她在 QQ / 微信上说的那一句. None = 不说 (你按停的、服务端重启停下的)."""
    goal, state, why = cap(goal, 24), outcome(run), brief(run, final)
    if state in ("stopped", "interrupted"):
        return None
    if state == "needs_check":
        return f"主人~ 「{goal}」工作台那边说做完了, 但我核对时没对上: {why}. 你回电脑前看一眼工作台"
    if state == "done":
        return f"主人~ 你让我用工作台办的「{goal}」做完了: {why or '做完了'} (要改就引用这条回我)"
    if state == "budget":
        return f"主人~ 「{goal}」做到这一轮的上限先停了" + (f" ({why})" if why else "") + ", 还没做完. 要接着做就引用这条回我"
    return f"主人~ 「{goal}」没做成" + (f": {why}" if why else "") + ". 你回电脑前看看工作台"


def goal_text(goal: str, item: dict) -> str | None:
    """持续任务有回报时她在聊天里说的那一句 (item 是持续任务的记录). None = 不说."""
    goal, state = cap(goal, 24), item.get("state")
    summary, tell = gist(item.get("summary") or "", 90), gist(item.get("tell") or "", 90)
    if state == "completed":
        return f"主人~ 你交给我的「{goal}」做完了: {summary or '做完了'}"
    if state == "waiting":
        return f"主人~ 「{goal}」要你定一下: {summary} (引用这条回我就行)"
    if state == "blocked":
        if "预算" in str(item.get("summary") or ""):
            return f"主人~ 「{goal}」花到预算了 ({float(item.get('budget_cny') or 0):g} 元), 先停下了; 要接着做, 在工作台的持续任务里加点预算"
        return f"主人~ 「{goal}」卡住了: {summary}. 引用这条回我, 或者回电脑前看看工作台"
    if tell:
        return f"主人~ 「{goal}」有新进展: {tell}"
    return None


def steps_text(s) -> str:
    """长期目标的步骤清单 (她用 task_plan 列的; 这一轮的, 没有就看上一轮留下的): "; 步骤: 在做「A」, 排着「B」「C」, 做完 2 步"."""
    plan = (getattr(s, "plan", None) or (getattr(s, "run", None) or {}).get("plan") or {}) if s is not None else {}
    steps = [x for x in plan.get("steps") or [] if isinstance(x, dict) and x.get("title")]
    if not steps:
        return ""
    doing = [cap(str(x["title"]), 20) for x in steps if x.get("status") == "doing"][:2]
    todo = [cap(str(x["title"]), 20) for x in steps if x.get("status") == "todo"][:3]
    done = sum(x.get("status") == "done" for x in steps)
    parts = ([f"在做{''.join(f'「{x}」' for x in doing)}"] if doing else []) + \
            ([f"排着{''.join(f'「{x}」' for x in todo)}"] if todo else []) + ([f"做完 {done} 步"] if done else [])
    return "; 步骤: " + ", ".join(parts) if parts else ""


def is_chatter(text: str) -> bool:
    """引用回报时只回了一句"谢谢""辛苦啦""好的": 不算这件事的后续."""
    return bool(CHATTER.match(str(text or "").strip()))


class Threads:
    """派到工作台的事 (一件一条线): 订阅结束事件、回报、认引用、给她的提示. hub / report 由 ChannelManager.attach_agent 给."""

    def __init__(self, path: Path | None, clock=time.time):
        self.path, self.clock = path, clock
        self.items: dict[str, dict] = {}   # 会话 id -> {sid, goal, where, conv, at, state, brief, said, told, shown}
        self.quotes: dict[str, str] = {}   # 回报那条消息的编号 -> 会话 id
        self.hub = None
        self.report = None                 # async (线, 要说的话) -> (发出去没有, [消息编号])
        self._sinks: dict[str, object] = {}
        self._tasks: set[asyncio.Task] = set()

    def attach(self, hub, report) -> None:
        self.hub, self.report = hub, report
        self._load()
        for sid in list(self.items):
            self._subscribe(sid)

    def _session(self, sid: str, load: bool = False):
        """工作台的会话: 在内存里的直接拿 (hub.get 每次都重算提示词前缀, 她在 QQ 上每回一句都会看这里);
        load=True 时没打开过的从文件读 (引用回报接着做时). 没打开过的会话不会在做事 (重启以后都是空闲)."""
        if self.hub is None:
            return None
        s = self.hub.sessions.get(sid)
        return s if s is not None or not load else self.hub.get(sid)

    # -- 开线、去重 --------------------------------------------------------------
    def duplicate(self, goal: str) -> dict | None:
        """同一件事 DUP_S 秒内又说了一遍、那边还在做: 返回那条线 (不再开新的)."""
        now = self.clock()
        for t in self.items.values():
            if t["goal"] == goal and now - t["at"] < DUP_S and t.get("kind") == "goal" and t["state"] == "running":
                return t
            if t["goal"] == goal and now - t["at"] < DUP_S:
                s = self._session(t["sid"])
                if s is not None and s.status != "idle":
                    return t
        return None

    def open(self, sid: str, goal: str, where: str, conv, goal_id: str = "") -> dict:
        """goal_id: 这是一个长期目标 (持续任务的 id); 空 = 一件普通的事."""
        t = {"sid": sid, "goal": goal, "where": where, "conv": list(conv) if conv else None, "at": self.clock(),
             "state": "running", "brief": "", "said": "", "told": False, "shown": False,
             "kind": "goal" if goal_id else "task", "goal_id": goal_id}
        self.items[sid] = t
        self._subscribe(sid)
        active = lambda x: x.get("kind") == "goal" and x["state"] not in ("done", "stopped")  # noqa: E731
        for old in sorted(self.items.values(), key=lambda x: (active(x), x["at"]))[:-KEEP]:  # 还在推进的长期目标最后挤
            self.drop(old["sid"], save=False)
        self._save()
        return t

    def drop(self, sid: str, save: bool = True) -> None:
        self.items.pop(sid, None)
        sink = self._sinks.pop(sid, None)
        if sink is not None and self.hub is not None:
            self.hub.unsubscribe(sid, sink)
        for mid in [m for m, s in self.quotes.items() if s == sid]:
            del self.quotes[mid]
        if save:
            self._save()

    # -- 结束事件 -> 回报 ----------------------------------------------------------
    def _subscribe(self, sid: str) -> None:
        if self.hub is None or sid in self._sinks:
            return

        async def sink(ev: dict) -> None:
            if ev.get("type") == "done":  # 另起一个任务回报: 发 QQ 慢, 不拖着工作台页面收这个事件
                task = asyncio.ensure_future(self._done(sid, dict(ev.get("run") or {})))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        self._sinks[sid] = sink
        self.hub.subscribe(sid, sink)

    async def _done(self, sid: str, run: dict) -> None:
        t = self.items.get(sid)
        s = self._session(sid)
        if t is None or s is None or t.get("kind") == "goal":  # 长期目标: 等持续任务的回报 (goal_event), 每一轮不说
            return
        if run.get("state") == "completed" and not s.stop_event.is_set() and (
                s.queue or any(e.get("session") == sid for e in self.hub.job_watch.entries())):
            return  # 还排着要接着做的 (你中途补充的)、她起的后台作业还在跑 (跑完会交回来接着做): 等真做完了再说
        try:
            final = final_text(s)
            text = report_text(t["goal"], run, final)
            t.update(state=outcome(run), at=self.clock(), brief=brief(run, final), said=text or "", told=False, shown=False)
            if text and t["conv"] is not None and self.report is not None:
                ok, ids = await self.report(t, text)
                t["told"] = bool(ok)
                for mid in ids:
                    self.quotes[str(mid)] = sid
                for mid in list(self.quotes)[:-QUOTES_KEEP]:
                    del self.quotes[mid]
        except Exception:  # noqa: BLE001 (回报没发成不能影响工作台; 状态还在提示里)
            log.exception("工作台那件事的回报没发成")
        self._save()

    async def goal_event(self, item: dict) -> None:
        """持续任务有回报 (挂在 resident.watchers 上): 是从聊天里交来的那件, 就发到聊天里. 做完了、要你定、卡住了、有值得说的进展."""
        t = self.items.get(str(item.get("session_id") or ""))
        if t is None or t.get("kind") != "goal":
            return
        text = goal_text(t["goal"], item)
        state = GOAL_STATE.get(item.get("state"), "running")
        t.update(state=state, at=self.clock(), brief=gist(item.get("tell") or item.get("summary") or "", 60),
                 said=text or "", told=False, shown=False)
        try:
            if text and t["conv"] is not None and self.report is not None:
                ok, ids = await self.report(t, text)
                t["told"] = bool(ok)
                for mid in ids:
                    self.quotes[str(mid)] = t["sid"]
                for mid in list(self.quotes)[:-QUOTES_KEEP]:
                    del self.quotes[mid]
        except Exception:  # noqa: BLE001
            log.exception("长期目标的回报没发成")
        self._save()

    # -- 引用回报 = 这件事的后续 ------------------------------------------------------
    def quoted(self, ids) -> str | None:
        for mid in ids:
            sid = self.quotes.get(str(mid))
            if sid in self.items:
                return sid
        return None

    async def follow(self, sid: str, text: str, where: str) -> str:
        """你引用了这件事的回报再说话: 交给那个会话 (在做: 加到这一轮; 做完了: 接着做一轮). 返回给她的一句提示."""
        t = self.items[sid]
        goal = cap(t["goal"], 24)
        if t.get("kind") == "goal":
            answered = await self._follow_goal(t, text, where)
            if answered:
                return answered
        s = self._session(sid, load=True)
        if s is None or s.archived or s.deleted_at:
            return (f"主人引用的是你在工作台办的「{goal}」的回报, 但那个会话已经不在了 (删了或归档了), 跟他说一声;"
                    " 要重新做就让他说\"用工作台…\"")
        try:
            r = await self.hub.steer(sid, f"（主人在{where}引用你刚才的回报, 接着说）{text}")
        except Exception as exc:  # noqa: BLE001 (交不过去也不能让她这句回不了: 老实说)
            log.warning("引用回报接着做没交过去: %s", exc)
            return f"主人引用了你在工作台办的「{goal}」的回报接着说, 但没交过去 ({exc}), 老实跟他说一声"
        t.update(state="running", at=self.clock(), brief="", said="", told=False, shown=False)
        self._save()
        how = "那边正在做, 这句加到这一轮里了" if r.get("mode") == "steer" else "那边接着做了"
        return (f"主人引用了你在工作台办的「{goal}」的回报接着说了一句, 已经交给工作台那边 ({how});"
                " 回他一句收到就好, 别说已经改好了, 做完了会再跟他说")

    async def _follow_goal(self, t: dict, text: str, where: str) -> str:
        """引用了长期目标的回报再说话. 返回给她的提示; 空串 = 这件已经做完了, 当一件普通的后续 (交给那个会话接着做一轮)."""
        goal, res = cap(t["goal"], 24), getattr(self.hub, "resident", None)
        try:
            item = res.get(t["goal_id"])
        except (ValueError, AttributeError, OSError):
            return f"主人引用的是长期目标「{goal}」的回报, 但那件事的记录已经不在了, 跟他说一声"
        if item["state"] == "completed":
            t.update(kind="task")  # 做完了还接着说: 之后按普通的事回报
            return ""
        if STOP_GOAL.search(text):
            if item["state"] != "paused":
                await res.control(item["id"], "pause", None)
            t.update(state="paused", at=self.clock(), said="", told=False, shown=False)
            self._save()
            return f"主人让你先别做长期目标「{goal}」了, 已经暂停 (工作台的持续任务里能继续); 回他一句好的"
        try:
            if item["state"] == "running":
                await self.hub.steer(t["sid"], f"（主人在{where}引用你刚才的回报, 接着说）{text}")
                how = "那边这一轮正在做, 这句加进去了"
            else:
                res.answer(item["id"], text)
                how = "那边接着做了, 这句下一轮开头交给那边"
        except Exception as exc:  # noqa: BLE001 (交不过去也不能让她这句回不了: 老实说)
            log.warning("引用长期目标的回报接着说没交过去: %s", exc)
            return f"主人引用了长期目标「{goal}」的回报接着说, 但没交过去 ({exc}), 老实跟他说一声"
        t.update(state="running", at=self.clock(), said="", told=False, shown=False)
        self._save()
        return (f"主人引用了长期目标「{goal}」的回报接着说了一句, 已经交给工作台那边 ({how});"
                " 回他一句收到就好, 有进展、要他定、做完了会再跟他说")

    def _goal_line(self, t: dict) -> str:
        """提示里长期目标那一项: 持续任务现在的状态 (来自持续任务的记录, 不靠她记)."""
        try:
            item = self.hub.resident.get(t["goal_id"])
        except (ValueError, AttributeError, OSError):
            return "（记录找不到了）"
        state, summary = item["state"], gist(item.get("summary") or "", 60)
        when = time.strftime("%m-%d %H:%M", time.localtime(item.get("next_at") or 0))
        line = {"scheduled": f"在后台推进, 已经做了 {item.get('rounds', 0)} 轮, 下一轮 {when}" + (f" (最近: {summary})" if summary else ""),
                "running": f"这会儿在做第 {item.get('rounds', 0) + 1} 轮",
                "waiting": f"等主人定: {summary}", "blocked": f"卡住了: {summary}", "paused": "主人暂停了",
                "completed": f"做完了: {summary}", "interrupted": "服务端重启时停在半路 (要接着做得主人说一声)"
                }.get(state, state)
        return line + steps_text(self._session(t["sid"]))

    # -- 她的提示里那一行 ---------------------------------------------------------
    def lines(self, mark: bool = True) -> list[str]:
        """工作台上帮主人办的事现在怎样 (最近几件; 还在做的看工作台那边的实际状态). 结果还没发给主人的, 第一次写进她跟主人
        说话的提示时标出来让她自己提, 之后照常写结论; mark=False (她自己过日子时看的) 不算提过."""
        if self.hub is None or not self.items:
            return []
        now, rows, changed = self.clock(), [], False
        for t in sorted(self.items.values(), key=lambda x: x["at"])[-4:]:
            s = self._session(t["sid"])
            if s is not None and (s.archived or s.deleted_at):
                continue
            goal = cap(t["goal"], 30)
            if t.get("kind") == "goal":
                row = f"「{goal}」(长期目标) {self._goal_line(t)}"
                if t["said"] and not t["told"] and not t["shown"]:
                    row += "; 刚才的回报还没发给主人, 找机会跟他说: " + gist(t["said"], 80)
                    if mark:
                        t["shown"], changed = True, True
                rows.append(row)
                continue
            if s is not None and s.status != "idle":
                rows.append(f"「{goal}」还在做: {AV.describe(self.hub, s) or '在做'}")
                continue
            if now - t["at"] > SHOW_S:
                continue
            state = "interrupted" if t["state"] == "running" else t["state"]  # 没等到结束事件 = 服务端重启时停下的
            why = t["brief"]
            row = {"done": "做完了" + (f": {why}" if why else ""),
                   "needs_check": "那边说做完了, 但核对没对上" + (f": {why}" if why else ""),
                   "budget": "到这一轮的上限先停了, 还没做完" + (f" ({why})" if why else ""),
                   "failed": "没做成" + (f": {why}" if why else ""), "stopped": "主人在电脑上按停了",
                   "interrupted": "做到一半服务端重启, 停在半路了 (要接着做得主人说一声)"}.get(state, "做完了")
            if t["said"] and not t["told"] and not t["shown"]:
                row += "; 结果还没发给主人, 找机会跟他说"
                if mark:
                    t["shown"], changed = True, True
            rows.append(f"「{goal}」{row}")
        if changed:
            self._save()
        if not rows:
            return []
        return ["[你在工作台上帮主人办的事] " + "; ".join(rows)
                + " (这是工作台那边的实际情况: 没做完别说做完了, 核对没对上就照实说; 主人要接着改哪件, 让他引用那件的回报再说)"]

    # -- 存盘 ------------------------------------------------------------------
    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"threads": list(self.items.values()), "quotes": self.quotes},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("工作台派活的线没存上 (重启以后不认引用): %s", exc)

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("工作台派活的线读不出来, 这次不认旧的引用: %s", exc)
            return
        for row in (data.get("threads") or [])[-KEEP:] if isinstance(data, dict) else []:
            if (isinstance(row, dict) and isinstance(row.get("sid"), str) and isinstance(row.get("goal"), str)
                    and row.get("state") in STATES):
                self.items[row["sid"]] = {"sid": row["sid"], "goal": row["goal"], "where": str(row.get("where") or ""),
                                          "conv": row.get("conv") if isinstance(row.get("conv"), list) else None,
                                          "at": float(row.get("at") or 0), "state": row["state"],
                                          "brief": str(row.get("brief") or ""),
                                          "said": str(row.get("said") or ""), "told": bool(row.get("told")),
                                          "shown": bool(row.get("shown")),
                                          "kind": "goal" if row.get("kind") == "goal" else "task",
                                          "goal_id": str(row.get("goal_id") or "")}
        quotes = data.get("quotes") if isinstance(data, dict) else None
        for mid, sid in (quotes.items() if isinstance(quotes, dict) else []):
            if sid in self.items:
                self.quotes[str(mid)] = sid

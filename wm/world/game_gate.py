"""游戏世界的感知门: 真值 -> 她感知得到的事件. 样板: 主程序不引用, 宿主第 1 步可以替换. 契约 perception.visible / hidden;
记忆 cognition-truth-split, screen-watch-consent.

真值 (这里握着, 只给面板): 前台窗口的 exe / 矩形 / 标题, 被跳过窗口的类别和次数 (private / self / work / small / none),
截了几张、发给云端几次. 她只收到清单里声明过的事件, 字段只有 app、title (通过 watchable 的游戏窗口的)、first、change、reason、ok、why:
  - game_away 不带原因: 切去聊天窗口和切去写代码的窗口, 对她一模一样;
  - 切走不到 AWAY_TELL_S 秒不产生任何事件;
  - 被跳过窗口的标题、exe 永远不出门 (连面板也只有类别计数).
纯类, 不碰网络、不读文件、不截图, 所以双胞胎夹具可以离线跑.
"""
from __future__ import annotations

from collections import Counter

from wm import screen_along as SA

AWAY_TELL_S = 10.0     # 切走超过这么久才告诉她"他看别处去了" (更短的什么都不发生)
END_REASONS = {"user": "owner_stopped", "leave": "owner_stopped", "switch": "owner_stopped", "away": "left_game",
               "max_session": "time_limit", "model_down": "model_down"}


def event(type_: str, text: str = "", **fields) -> dict:
    return {"type": type_, "fields": fields, "text": text}


class GameGate:
    def __init__(self, away_tell_s: float = AWAY_TELL_S):
        self.away_tell_s = away_tell_s
        self.app = self.title = ""
        self.started = False
        self.away_since: float | None = None
        self.away_told = False
        self.skipped: Counter = Counter()    # 真值: 每一类被跳过的巡视次数 (只给面板)
        self.episodes: Counter = Counter()   # 真值: 每一类切走了几回
        self.uploads = 0                     # 真值: 拼图发给云端几次
        self.captures = 0                    # 真值: 真截了几张

    # -- 每次巡视前台窗口 (ScreenAlong 每个 tick 一次) -----------------------------------
    def observe(self, info, now: float) -> list[dict]:
        ok, why = SA.watchable(info, (info or {}).get("screen_area"))
        if ok:
            out = [event("game_back")] if self.away_told else []
            self.away_since, self.away_told = None, False
            return out
        self.skipped[why] += 1
        if self.away_since is None:
            self.away_since = now
            self.episodes[why] += 1
        if self.started and not self.away_told and now - self.away_since >= self.away_tell_s:
            self.away_told = True
            return [event("game_away")]
        return []

    # -- ScreenAlong 的回调 -------------------------------------------------------------
    def window(self, app: str, title: str) -> list[dict]:
        """看的窗口变了 (第一次 = 开局). 开局发 game_started; 之后换标题不另发, 下一眼的 game_scene 会带上."""
        self.app, self.title = app, title
        if self.started:
            return []
        self.started = True
        return [event("game_started", app=app, title=title)]

    def scene(self, ctx: dict) -> dict:
        return event("game_scene", app=ctx.get("app") or self.app, title=ctx.get("title") or self.title,
                     first=bool(ctx.get("first")), change="big" if ctx.get("key") else "steady")

    def owner_said(self, text: str) -> dict:
        return event("owner_said", text)

    def ended(self, reason: str) -> dict:
        reason = str(reason or "")
        return event("game_ended", reason=END_REASONS.get(reason, "error" if reason.startswith("error") else "owner_stopped"))

    @staticmethod
    def receipt(type_: str, ok: bool, why: str) -> dict:
        return event(type_, ok=bool(ok), why=why)

    # -- 只给面板的真值 -------------------------------------------------------------------
    def panel_truth(self) -> dict:
        return {"not_captured": dict(self.skipped), "episodes": dict(self.episodes), "uploads": self.uploads,
                "captures": self.captures}

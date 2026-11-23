"""游戏世界·画面的同意范围 (世界2 验收 4; 记忆 screen-watch-consent: 2026-11-08 用户同意的范围).

只在叫她陪的时候取; 只取前台窗口; 敏感窗口不取; 不落盘; 上云的口径和陪看一致 (前台窗口的小拼图, 只在内存, 看完就删,
请求里没有工具). 全部用合成画面和假模型, 不截真屏幕.
"""
import asyncio
import base64
import builtins
import io
import json
import os
import re

import pytest
from PIL import Image

from game_fakes import fixture, load, make_fx, run
from wm import screen_along as SA
from wm.world import game_twin as T
from wm.world.game_watch import GameWatch, OwnerCall

GAME_RECT = (0, 0, 1920, 1080)
GAME_TITLE = "月下的信 - 第三章"
MAGENTA = (255, 0, 255)


def base_fx(run_s=30, **extra):
    return make_fx([{"at": 0, "window": "game", "scene": "dialog"}], run_s, **extra)


# -- 只在叫她陪的时候取 ------------------------------------------------------------------------------------------

def test_call_nothing_is_taken_before_the_owner_calls():
    _, spec, env = load()

    async def go():
        r = T.build(base_fx(), "A", spec, env)
        for _ in range(50):
            await r.clock.sleep(2.0)                # 虚拟 100 秒
            await asyncio.sleep(0)
        return r
    r = asyncio.run(go())
    assert r.watch.eye is None and r.watch.state == "idle" and not r.watch.alive
    assert r.screen.looks == 0 and r.screen.grabs == [] and r.llm.calls == []


def test_call_start_needs_the_owners_own_token():
    _, spec, env = load()
    a = T.build(base_fx(), "A", spec, env).watch
    b = T.build(base_fx(), "A", spec, env).watch

    async def go():
        for bad in (None, "陪我玩游戏", object(), OwnerCall("陪我玩游戏", b)):     # 别人家的令牌也不行
            with pytest.raises(PermissionError):
                await a.start(bad)
        assert a.eye is None
    asyncio.run(go())


def test_call_only_the_owners_game_words_start_it():
    _, spec, env = load()

    async def go(text):
        r = T.build(base_fx(), "A", spec, env)
        ok, reply = await r.watch.owner_call(text)
        started = r.watch.eye is not None
        if started:
            await r.watch.leave(speak_line=False)
        return ok, reply, started, r.screen.looks
    for text in ("今天天气不错", "你好呀", "帮我看看这个文件", "陪我看 BV1xx411c7mD", "陪我看 https://b23.tv/AbCd12", "不看了", ""):
        ok, reply, started, looks = asyncio.run(go(text))
        assert (ok, started, looks) == (False, False, 0), text
    for text in ("陪我玩游戏", "看我打一把", "我们一起玩一局"):
        ok, reply, started, _ = asyncio.run(go(text))
        assert ok and started and "碰游戏" in reply and "不看了" in reply, text      # 开头就把范围和随时能停说清楚


# -- 只取前台窗口 -----------------------------------------------------------------------------------------------

def test_foreground_every_grab_is_exactly_the_foreground_window_rect():
    win = {"exe": "vnreader.exe", "title": GAME_TITLE, "rect": [200, 100, 1500, 900]}
    fx = make_fx([{"at": 0, "window": "g2", "scene": "dialog"}, {"at": 10, "scene": "battle"}], 30,
                 windows={"g2": win})
    r = run(fx)
    assert r.screen.grabs and all(g[0] == (200, 100, 1500, 900) for g in r.screen.grabs)
    assert not any(g[0] == GAME_RECT for g in r.screen.grabs)             # 没有整屏


def test_foreground_runtime_guard_refuses_other_rects_and_unwatchable_windows():
    _, spec, env = load()
    r = T.build(base_fx(), "A", spec, env)
    w = r.watch
    taken = []
    w._grabber = lambda rect, width: taken.append(rect) or Image.new("RGB", (320, 180), (1, 2, 3))
    info = w._observe()
    assert w._grab((0, 0, 3840, 1080), 640) is None and taken == []         # 不是刚看过的那个矩形 (整屏之类)
    assert w._grab(tuple(info["rect"]), 640) is not None and taken == [tuple(info["rect"])]
    w._last_info = {**info, "exe": "qq.exe"}                                  # 前台变成聊天窗口
    assert w._grab(tuple(info["rect"]), 640) is None and len(taken) == 1
    assert w.audit.kinds().count("grab_refused") == 2


def test_foreground_default_capture_is_the_existing_screen_along_path(monkeypatch):
    """不给 look / grabber 时, 用的就是 SA.foreground 和 SA.grab 这两个已有的函数 (按名字现取)."""
    _, spec, env = load()
    r = T.build(base_fx(), "A", spec, env, module_capture=True)
    monkeypatch.setattr(SA, "foreground", r.screen.look)
    monkeypatch.setattr(SA, "grab", r.screen.grab)
    asyncio.run(T.drive(r, base_fx()))
    assert r.screen.looks > 5 and len(r.screen.grabs) >= 1 and r.said


# -- 敏感窗口不取 -----------------------------------------------------------------------------------------------

SENSITIVE = {
    "weixin": {"exe": "weixin.exe", "title": "文件传输助手", "rect": [0, 0, 1900, 1000], "color": list(MAGENTA)},
    "keepass": {"exe": "keepassxc.exe", "title": "数据库", "rect": [0, 0, 1900, 1000], "color": list(MAGENTA)},
    "todesk": {"exe": "todesk.exe", "title": "远程", "rect": [0, 0, 1900, 1000], "color": list(MAGENTA)},
    "login": {"exe": "vnreader.exe", "title": "登录 - 某站", "rect": [0, 0, 1900, 1000], "color": list(MAGENTA)},
    "pay": {"exe": "vnreader.exe", "title": "支付宝收银台", "rect": [0, 0, 1900, 1000], "color": list(MAGENTA)},
    "bank": {"exe": "vnreader.exe", "title": "工商银行网银", "rect": [0, 0, 1900, 1000], "color": list(MAGENTA)},
    "mailweb": {"exe": "vnreader.exe", "title": "收件箱 - Gmail", "rect": [0, 0, 1900, 1000], "color": list(MAGENTA)},
    "pet": {"exe": "wmpet.exe", "title": "桌宠窗口Q", "rect": [0, 0, 1900, 1000], "color": list(MAGENTA)},
    "claude": {"exe": "claude.exe", "title": "Claude", "rect": [0, 0, 1900, 1000], "color": list(MAGENTA)},
    "tiny": {"exe": "tiny.exe", "title": "小工具", "rect": [0, 0, 300, 200], "color": list(MAGENTA)},
}


def _sheet_has_color(url: str, color, tol=70) -> bool:
    im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")
    cw, ch = T.FRAME
    for rr in range(max(1, im.height // ch)):
        for cc in range(max(1, im.width // cw)):
            px = im.getpixel((cc * cw + cw // 2, rr * ch + ch // 2))
            if sum((a - b) ** 2 for a, b in zip(px, color)) ** 0.5 < tol:
                return True
    return False


def test_sensitive_windows_are_never_captured_nor_sent():
    names = [*SENSITIVE, "qq", "code"]
    tl = [{"at": 0, "window": "game", "scene": "dialog"}]
    for i, n in enumerate(names):
        tl.append({"at": 4 + 4 * i, "window": n})
    tl.append({"at": 4 + 4 * len(names), "window": "game", "scene": "battle"})
    r = run(make_fx(tl, 4 + 4 * len(names) + 10, windows=SENSITIVE))
    assert r.screen.grabs, "游戏窗口本身该取"
    assert all(g[2] == GAME_TITLE and g[1] == "vnreader.exe" and g[0] == GAME_RECT for g in r.screen.grabs)
    sent = [m[-1]["content"][1]["image_url"]["url"] for m in r.llm.calls if isinstance(m[-1]["content"], list)]
    assert sent and not any(_sheet_has_color(u, MAGENTA) or _sheet_has_color(u, (0, 255, 255)) for u in sent)
    nc = r.watch.panel()["she_did_not_see"]["not_captured"]
    assert nc.get("private", 0) >= 20 and nc.get("self", 0) >= 3 and nc.get("work", 0) >= 6 and nc.get("small", 0) >= 3
    # 这些窗口的标题和 exe 一个字也没出现在发给模型的文字里
    texts = json.dumps([m for m in r.llm.calls], ensure_ascii=False)
    for n, w in {**SENSITIVE, **{"qq": {"title": "小满", "exe": "qq.exe"}, "code": {"title": "main.py", "exe": "code.exe"}}}.items():
        if w["title"] != GAME_TITLE:
            assert w["title"] not in texts and w["exe"] not in texts, n


# -- 不落盘 -----------------------------------------------------------------------------------------------------

def test_disk_nothing_is_written_during_a_whole_session(monkeypatch):
    violations = []
    real_open, real_save = builtins.open, Image.Image.save

    def guarded_open(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+") and str(file) != os.devnull:
            violations.append(("open", str(file), mode))
        return real_open(file, mode, *a, **k)

    def guarded_save(self, fp, *a, **k):
        if not hasattr(fp, "write"):
            violations.append(("save", str(fp)))
        return real_save(self, fp, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(Image.Image, "save", guarded_save)
    for name in ("replace", "rename", "mkdir", "makedirs", "remove", "unlink"):
        monkeypatch.setattr(os, name, lambda *a, _n=name, **k: violations.append((_n, a)))
    fx = make_fx([{"at": 0, "window": "game", "scene": "dialog"}, {"at": 10, "scene": "battle"}, {"at": 20, "window": "qq"}], 50,
                 owner_says=[{"at": 5, "text": "哇"}])
    r = run(fx)
    assert violations == []
    assert r.watch.eye.frames == [] and r.watch.session._frame is None                       # 内存里也扔了
    assert r.watch.summary and "base64" not in json.dumps(r.watch.summary, ensure_ascii=False)
    assert "data:image" not in json.dumps(list(r.watch.log), ensure_ascii=False)


def test_disk_guard_is_not_decoration(monkeypatch, tmp_path):
    violations = []
    real_save = Image.Image.save
    monkeypatch.setattr(Image.Image, "save", lambda self, fp, *a, **k: (violations.append(fp), real_save(self, fp, *a, **k))[1]
                        if not hasattr(fp, "write") else real_save(self, fp, *a, **k))
    Image.new("RGB", (4, 4)).save(tmp_path / "x.png")
    assert violations


# -- 上云的口径和陪看一致 ----------------------------------------------------------------------------------------

class Recording(T.SceneLLM):
    kwargs: list = []

    async def stream(self, messages, **kw):
        Recording.kwargs.append(kw)
        async for t in super().stream(messages, **kw):
            yield t


def test_cloud_only_the_screen_along_sheet_goes_out_nothing_else(monkeypatch):
    sheets = []
    real = SA.sheet
    monkeypatch.setattr(SA, "sheet", lambda frames, cell=480: sheets.append(real(frames, cell)) or sheets[-1])
    Recording.kwargs = []
    fx = make_fx([{"at": 0, "window": "game", "scene": "dialog"}, {"at": 10, "scene": "battle"}, {"at": 20, "scene": "options"},
                  {"at": 30, "scene": "black"}], 45, owner_says=[{"at": 12, "text": "好紧张"}])
    r = run(fx, llm_factory=Recording)
    withimg = [m for m in r.llm.calls if isinstance(m[-1]["content"], list)]
    assert len(withimg) >= 4 and len(withimg) == len(r.llm.calls) == r.watch.gate.uploads == r.watch.eye.stats["looks"]
    for m in r.llm.calls:
        parts = m[-1]["content"]
        assert [p["type"] for p in parts] == ["text", "image_url"] and parts[1]["image_url"]["detail"] == "low"
        url = parts[1]["image_url"]["url"]
        assert url in sheets and url.startswith("data:image/jpeg;base64,")
        im = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
        assert im.format == "JPEG" and im.width <= 2 * T.FRAME[0] and im.height <= 2 * T.FRAME[1]       # 最多 2x2 张
        text = parts[0]["text"]
        assert "vnreader" in text                                                                         # app 名 (通过 watchable 的游戏窗口) 可以
        assert not re.search(r"\.exe|1920|rect|\\\\|[A-Z]:\\", text)                                      # exe / 矩形 / 路径不出门
        assert set(m[0]) == {"role", "content"} and all(set(x) <= {"role", "content"} for x in m)       # 没有 tools 之类的键
    assert Recording.kwargs and all(set(k) <= {"schema", "max_tokens", "id_slot", "meta"} for k in Recording.kwargs)
    # 一个世界会话里没有主人的东西: 系统提示里没有主人的事实或记忆
    assert "记忆" not in r.llm.calls[0][0]["content"] and "owner_facts" not in r.llm.calls[0][0]["content"]

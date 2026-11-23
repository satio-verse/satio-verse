"""屏幕陪看 (wm/screen_along.py): 哪些窗口能看、画面变没变、什么时候问模型、什么时候说话、切走了停.
假时钟、假前台窗口、假截图、假模型: 不截真的屏幕、不调真模型."""
import asyncio
import base64
import io

from PIL import Image

from wm import screen_along as SA

B = "某个猫猫视频_哔哩哔哩_bilibili - Google Chrome"


def test_clean_title_and_app():
    assert SA.clean_title(B) == "某个猫猫视频"
    assert SA.clean_title("某个猫猫视频_哔哩哔哩_bilibili 和另外 3 个页面 - 个人 - Microsoft​ Edge") == "某个猫猫视频"
    assert SA.clean_title("Never Gonna Give You Up - YouTube - Google Chrome") == "Never Gonna Give You Up"
    assert SA.clean_title("哔哩哔哩") == ""  # B 站客户端的窗口标题就是这个: 认不出看的是哪条
    assert SA.app_name("msedge.exe") == "Edge" and SA.app_name("哔哩哔哩.exe") == "哔哩哔哩" and SA.app_name("foo.exe") == "foo"


def win(exe="chrome.exe", title=B, rect=(0, 0, 1600, 900), **kw):
    return {"exe": exe, "title": title, "rect": rect, "minimized": False, "screen_area": 1920 * 1080, **kw}


def test_watchable_keeps_private_and_work_windows_out():
    assert SA.watchable(win()) == (True, "ok")
    assert SA.watchable(win("potplayermini64.exe", "x.mp4")) == (True, "ok")
    for exe in ("qq.exe", "weixin.exe", "keepassxc.exe", "todesk.exe"):
        assert SA.watchable(win(exe, "随便")) == (False, "private")
    for title in ("支付宝 - 网上支付", "登录 - 哔哩哔哩", "工商银行网银", "收件箱 - Gmail", "微信网页版"):
        assert SA.watchable(win(title=title)) == (False, "private")
    assert SA.watchable(win("wmpet.exe", "绫音")) == (False, "self")
    for exe in ("code.exe", "claude.exe", "winword.exe", "explorer.exe"):
        assert SA.watchable(win(exe, "x")) == (False, "work")
    assert SA.watchable(win("somegame.exe", "某个游戏", rect=(0, 0, 1920, 1080))) == (True, "ok")  # 不认得但占满屏: 多半在玩
    assert SA.watchable(win("tool.exe", "小工具", rect=(0, 0, 300, 200))) == (False, "small")
    assert SA.watchable(win(minimized=True)) == (False, "none") and SA.watchable(None) == (False, "none")


def img(color):
    return Image.new("RGB", (320, 180), color)


def test_change_detection_and_sheet():
    a, b = SA.tiny(img((10, 10, 10))), SA.tiny(img((200, 50, 50)))
    assert not SA.changed(a, SA.tiny(img((10, 10, 10))), 4.0) and SA.changed(a, b, 4.0) and SA.changed(a, None, 4.0)
    url = SA.sheet([(1, img((10, 10, 10))), (5, img((200, 50, 50))), (9, img((50, 200, 50)))], cell=160)
    assert url.startswith("data:image/jpeg;base64,")
    out = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert out.size == (320, 180)  # 三张拼成 2x2 (每格 160x90)
    assert SA.sheet([]) is None


def test_parse_look():
    assert SA.parse_look('{"emo": "happy", "reply": "这猫好胖", "saw": "一只胖猫在跳"}') == ("happy", "这猫好胖", "一只胖猫在跳")
    assert SA.parse_look("不是 JSON") == ("neutral", "", "")
    emo, reply, saw = SA.parse_look('{"emo": "???", "reply": "' + "啊" * 100 + '", "saw": ""}')
    assert emo == "neutral" and len(reply) == 60


# -- 一次屏幕陪看 ---------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += max(0.0, s)
        await asyncio.sleep(0)


class Rig:
    """假前台窗口 / 假截图 / 假模型 / 假桌宠, 都记账."""

    def __init__(self):
        self.clock = Clock()
        self.window = win()
        self.playing = True
        self.n = 0
        self.grabs, self.thought, self.thought_t, self.said = [], [], [], []
        self.busy_until = 0.0
        self.reply = lambda ctx: ("happy", "这个好好笑" if not ctx["first"] else "哇这个可爱", "看到一只猫")

    def look(self):
        return dict(self.window)

    def grab(self, rect, width):
        self.grabs.append((rect, self.window["exe"]))
        if self.playing:
            self.n += 1
        return img(((self.n * 37) % 256, (self.n * 91) % 256, 120))

    async def think(self, ctx):
        self.thought.append(ctx)
        self.thought_t.append(self.clock.t)
        return self.reply(ctx)

    async def speak(self, note, reply, emo):
        if self.clock.t < self.busy_until:
            return False
        self.said.append((self.clock.t, reply, note))
        return True

    def make(self, **conf):
        self.events, self.ended = [], []

        async def on_end(s):
            self.ended.append(s)
        return SA.ScreenAlong(conf, speak=self.speak, think=self.think, look=self.look, grabber=self.grab,
                              clock_fn=self.clock.now, sleep=self.clock.sleep, on_end=on_end, trace=self.events.append,
                              threaded=False)


async def _run(w, rig, until, hook=None, stop=True):
    w.start()
    for i in range(20000):
        if w.task.done() or rig.clock.t >= until:
            break
        if hook is not None:
            hook(rig.clock.t)
        await asyncio.sleep(0)
    if stop:
        await w.stop("user")  # 停的时候把它的任务取消掉了
    elif not w.task.done():
        await asyncio.wait_for(w.task, 5)


def looks(rig):
    return [e for e in rig.events if e["event"] == "look"]


def test_first_look_then_every_look_s_and_min_gap():
    rig = Rig()
    w = rig.make()
    asyncio.run(_run(w, rig, 1000 + 70))
    ls = looks(rig)
    assert ls[0]["first"] is True and ls[0]["result"] == "said"  # 一开始先看一眼
    assert 1003 <= rig.said[0][0] <= 1008 and rig.said[0][1] == "哇这个可爱"
    assert len(ls) >= 3 and all(not e["first"] for e in ls[1:])
    times = [t for t, _, _ in rig.said]
    assert all(b - a >= 40 for a, b in zip(times, times[1:]))  # 两句之间至少隔 min_gap_s
    assert any(e["result"] == "gap" for e in ls)  # 想说但离上一句太近: 不说
    assert rig.thought[0]["title"] == "某个猫猫视频" and rig.thought[0]["image"].startswith("data:image/jpeg")
    s = rig.ended[0]
    assert s["mode"] == "screen" and s["items"] == ["某个猫猫视频"] and s["notes"][0] == "《某个猫猫视频》看到一只猫"
    assert s["reason"] == "user" and s["said"] == len(rig.said)


def test_still_screen_costs_nothing():
    rig = Rig()
    rig.playing = False  # 停在一页 / 暂停了: 画面不变
    w = rig.make()
    asyncio.run(_run(w, rig, 1000 + 120))
    assert len(looks(rig)) == 1  # 只有一开始那一眼, 之后不再问模型
    assert len(rig.grabs) > 30  # 截图照样隔几秒比一次 (不花钱)


def test_switch_gets_a_quick_first_look():
    rig = Rig()
    w = rig.make()

    def hook(t):
        if t >= 1030 and "猫猫" in rig.window["title"]:
            rig.window = win(title="下一个狗狗视频_哔哩哔哩_bilibili - Google Chrome")
    asyncio.run(_run(w, rig, 1000 + 60, hook))
    assert rig.ended[0]["items"] == ["某个猫猫视频", "下一个狗狗视频"]
    switch = next(e for e in rig.events if e["event"] == "switch")
    assert switch["title"] == "下一个狗狗视频"
    firsts = [e for e in looks(rig) if e["first"]]
    assert len(firsts) == 2 and firsts[1]["title"] == "下一个狗狗视频"


def test_private_window_never_grabbed_and_away_stops():
    rig = Rig()
    w = rig.make(away_s=60)

    def hook(t):
        if t >= 1010:
            rig.window = win("weixin.exe", "微信")  # 你切去回微信
    asyncio.run(_run(w, rig, 1000 + 200, hook, stop=False))
    assert all(exe != "weixin.exe" for _, exe in rig.grabs)  # 聊天窗口一次都没截
    assert w.reason == "away" and "先不看了" in rig.said[-1][1]
    assert rig.ended[0]["private"] > 0 and any(e["event"] == "away" and e["why"] == "private" for e in rig.events)


def test_user_pause_stops_looking_until_resume():
    rig = Rig()
    w = rig.make()
    marks = {}

    def hook(t):
        if t >= 1010 and "p" not in marks:
            marks["p"] = t
            w.pause("user")
        if t >= 1100 and "r" not in marks:
            marks["r"] = t
            w.resume()
    asyncio.run(_run(w, rig, 1000 + 140, hook))
    assert not [t for t in rig.thought_t if marks["p"] < t < marks["r"]]  # 暂停的时候一眼都没问模型
    assert any(e["event"] == "pause" for e in rig.events) and any(e["event"] == "resume" for e in rig.events)
    assert [t for t in rig.thought_t if t > marks["r"]]  # 继续以后又看了
    assert "主人让你先别看" not in w.note()


def test_heard_goes_into_next_look_and_summary():
    rig = Rig()
    w = rig.make()
    done = {}

    def hook(t):
        if t >= 1012 and "h" not in done:
            done["h"] = True
            w.heard("这个 up 主好搞笑", "是呀笑死我了")
    asyncio.run(_run(w, rig, 1000 + 50, hook))
    later = [c for c in rig.thought if c["heard"]]
    assert later and later[0]["heard"] == [("这个 up 主好搞笑", "是呀笑死我了")]
    assert rig.ended[0]["heard"] == [["这个 up 主好搞笑", "是呀笑死我了"]]
    assert "你在陪主人看他的屏幕" in w.note()


def test_budget_caps_looks():
    rig = Rig()
    w = rig.make(max_looks_per_hour=2)
    asyncio.run(_run(w, rig, 1000 + 200))
    assert len(looks(rig)) == 2


def test_watch_page_in_memory_is_recalled(tmp_path):
    """陪看完记的那一节 (<角色>/记忆/一起看过的视频.md) 参与检索: 以后问"上次一起看的猫视频"想得起来."""
    from wm.memory import WATCH_FILE, Memory
    m = Memory({"vault": str(tmp_path), "character": "小流"})
    m.init()
    m.start_session()
    m.append_watch("2026-11-08 03:17 《胖猫跳舞》", ["- 怎么看的: 看主人的屏幕", "- 讲了什么: 我们看了一只橘色胖猫跳舞, 主人笑疯了",
                                                 "- 主人说: “这只猫好可爱”"])
    m.append_watch("2026-11-08 04:00 《狗狗冲浪》", ["- 讲了什么: 一只柯基在冲浪"])
    text = (tmp_path / "小流" / "记忆" / WATCH_FILE).read_text(encoding="utf-8")
    assert text.startswith("# 一起看过的视频") and text.index("《胖猫跳舞》") < text.index("《狗狗冲浪》")  # 只往后加
    assert text.count("## 2026-11-08") == 2
    r = m.recall("上次我们一起看的那个橘色胖猫跳舞的视频")
    assert "胖猫" in (r["line"] or "")


def test_dropped_line_says_why_and_note_has_what_she_saw():
    """11-08 实测: 外放时麦克风把视频的声音当成有人在说话 (一段 29 秒), 她那句没说出口; 现在日志里记为什么.
    聊天的状态行带她刚看到的几句, 并说明不用去操作电脑 (那次你说"看下内容", 她拿你的鼠标键盘操作了两分多钟)."""
    rig = Rig()
    rig.busy_until = 1e9  # 一直轮不到她说
    w = rig.make()
    w.blocked = lambda: "listening"
    asyncio.run(_run(w, rig, 1000 + 30))
    first = looks(rig)[0]
    assert first["result"] == "dropped" and first["why"] == "listening"
    n = w.note()
    assert "你刚才看到: 看到一只猫" in n and "不用去操作电脑" in n


def test_home_page_is_not_an_item():
    rig = Rig()
    w = rig.make()

    def hook(t):
        if t >= 1020 and "猫猫" in rig.window["title"]:
            rig.window = win(title="哔哩哔哩 (゜-゜)つロ 干杯~-bilibili - Google Chrome")
        if t >= 1030 and "干杯" in rig.window["title"]:
            rig.window = win(title="下一个狗狗视频_哔哩哔哩_bilibili - Google Chrome")
    asyncio.run(_run(w, rig, 1000 + 60, hook))
    assert rig.ended[0]["items"] == ["某个猫猫视频", "下一个狗狗视频"]  # 中间回了一趟首页, 不算一条


def test_network_blip_skips_a_look_then_keeps_watching():
    """11-08 15:01 实测: 连不上 DeepSeek 一下, 整个陪看就停了、一声没吭. 现在跳过这一眼, 下一眼接着看."""
    rig = Rig()
    calls = {"n": 0}
    ok = rig.reply

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("连不上 DeepSeek (api.deepseek.com): 10 s 内没连上")
        return ok(ctx)
    rig.reply = flaky
    w = rig.make()
    asyncio.run(_run(w, rig, 1000 + 120))
    results = [e["result"] for e in looks(rig)]
    assert results[:2] == ["error", "error"] and "said" in results[2:]
    assert w.reason == "user" and rig.ended[0]["errors"] == 2 and rig.said  # 网好了接着看、接着说


def test_network_down_says_so_and_stops():
    rig = Rig()

    def down(ctx):
        raise RuntimeError("连不上 DeepSeek")
    rig.reply = down
    w = rig.make()
    asyncio.run(_run(w, rig, 1000 + 600, stop=False))
    assert w.reason == "model_down" and rig.said[-1][1] == SA.DOWN_LINE  # 说一声再停, 这句不用模型
    assert [e["result"] for e in looks(rig)] == ["error"] * 3 and rig.ended[0]["errors"] == 3


def test_waits_while_you_talk():
    rig = Rig()
    rig.busy_until = 1000 + 6  # 一开始那一眼想说的时候你在说话: 等一下再说
    w = rig.make()
    asyncio.run(_run(w, rig, 1000 + 30))
    assert rig.said and rig.said[0][0] >= 1006 and looks(rig)[0]["result"] == "said"

"""桌宠会话里的陪看 (pipeline.Session 的 aside / 陪看命令): 假工具、假播放器、假模型, 不碰 B 站、不调真模型."""
import asyncio
import json

from wm import watch_along as WA
from wm.config import load_config
from wm.engines import Engines
from wm.pipeline import Session
from wm.trace import LatencyStats, TraceLog


def make_cfg(tmp_path, *extra):
    return load_config(overrides=[
        "llm.engine=mock", "llm.mock.tokens_per_s=300", "llm.mock.ttft_ms=30",
        "tts.engine=mock", "tts.mock.rtf=0.02", "tts.mock.first_chunk_ms=20",
        "asr.engine=none", "face.engine=none", "memory.vault=\"\"", f"trace.log_dir={json.dumps(str(tmp_path))}",
        "watch_along.ramp_s=[3]", "watch_along.segment_s=3", "watch_along.lead_s=1", "watch_along.late_drop_s=3",
        "watch_along.player_check=false", f"watch_along.workdir={json.dumps(str(tmp_path / 'watch'))}", *extra])


class FakeReg:
    def __init__(self, ok=True):
        self.ok = ok

    def server(self, server_id, credentials=False):
        if not self.ok:
            raise ValueError("MCP 未启用")
        return {"id": server_id, "command": "python", "args": ["server.py"], "cwd": ".", "transport": "stdio"}


class FakeChannels:
    def __init__(self, ok=True):
        self.agent_hub = type("Hub", (), {"extensions": FakeReg(ok)})()
        self.cloud = None
        self.diary = []
        self.heard_texts = []

    async def _diary(self, text):
        self.diary.append(text)

    async def heard(self, text, where, owner, context=None):
        self.heard_texts.append(text)
        return []

    def attach(self, send):
        pass

    def detach(self, send):
        pass

    def pet_said(self, *a):
        pass

    def pet_context(self):
        return None


def rig(tmp_path, monkeypatch, ok=True, duration=7.0, title="小猫视频", extra=()):
    calls = []

    async def fake_call(self, name, args):
        calls.append((name, dict(args)))
        if name == "video_probe":
            return {"duration": duration, **({"title": title} if title else {}), "input": "out/v.mp4"}, []
        a = float(args["cursor"])
        return {"status": "ok", "storyboard": [{"from": WA.clock(a), "to": WA.clock(a + 3), "text": "喵喵"}]}, []
    monkeypatch.setattr(WA.HerTool, "call", fake_call)

    async def main(script):
        cfg = make_cfg(tmp_path, *extra)
        engines = Engines(cfg)
        await engines.load()
        sent = []

        async def send(m):
            sent.append(m)
        s = Session(engines, cfg, send, LatencyStats(), TraceLog(tmp_path))
        s.channels = FakeChannels(ok)
        thought = []

        async def think(ctx):
            thought.append(ctx)
            return "happy", f"第{int(ctx['segment'][0])}秒的猫好可爱"
        s._watch_think = think
        await s.on_message({"type": "output", "audio": False})
        try:
            await script(s, sent)
        finally:
            await s.close()
            await engines.close()
        return sent, thought, s
    return main, calls


async def say(s, text, wait=1.5):
    await s.on_message({"type": "text", "text": text})
    for _ in range(int(wait / 0.05)):
        await asyncio.sleep(0.05)
        if s.current is not None and s.current.finalized:
            break


def replies(sent, source=None):
    starts = {m["turn"]: m for m in sent if m.get("type") == "turn_start"}
    out = {}
    for m in sent:
        if m.get("type") == "reply_delta" and (source is None or starts.get(m["turn"], {}).get("source") == source):
            out[m["turn"]] = out.get(m["turn"], "") + m["text"]
    return list(out.values())


def test_watch_start_with_cursor_lines_and_stop(tmp_path, monkeypatch):
    main, calls = rig(tmp_path, monkeypatch, duration=30.0)

    async def script(s, sent):
        await say(s, "陪我看 BV1xx411c7mD 我看到0分02秒了")
        assert s.watch is not None and s.watch.alive
        for _ in range(120):  # 第一段 [2, 5], 在 4 秒说 (真时间 2 秒左右)
            await asyncio.sleep(0.05)
            if replies(sent, "watch"):
                break
        await asyncio.sleep(0.3)
        await say(s, "不看了")
        assert not s.watch.alive

    sent, thought, s = asyncio.run(main(script))
    mine = replies(sent)
    assert "我先把视频打开" in mine[0]
    watch_lines = replies(sent, "watch")
    assert watch_lines and watch_lines[0] == "第2秒的猫好可爱"
    ws = [m for m in sent if m.get("type") == "turn_start" and m.get("source") == "watch"]
    assert ws and all(m["text"] == "" for m in ws)  # 对话栏不显示成"你说的"
    assert {"type": "watch_state", "active": True, "source": "BV1xx411c7mD"} in sent
    assert any(m.get("type") == "watch_state" and m.get("active") is False and m.get("reason") == "user" for m in sent)
    assert "好，不看了" in replies(sent)[-1]
    notes = [t for t, _ in s.history if "你们在一起看《小猫视频》" in t]
    assert notes  # 历史里记的是提醒, 不是"你说的"
    assert calls[0] == ("video_probe", {"source": "BV1xx411c7mD"})
    assert s.channels.diary and "和主人一起看了《小猫视频》" in s.channels.diary[0]
    assert not (tmp_path / "watch").exists() or not any((tmp_path / "watch").glob("2*"))  # 临时目录删了


def test_watch_asks_for_link_then_source(tmp_path, monkeypatch):
    main, calls = rig(tmp_path, monkeypatch)

    async def script(s, sent):
        await say(s, "陪我看视频")
        assert s.watch is None and s.watch_wait > 0
        await say(s, "https://www.bilibili.com/video/BV1xx411c7mD")
        assert s.watch is not None and s.watch.source == "BV1xx411c7mD"
        await say(s, "不看了")

    sent, _, s = asyncio.run(main(script))
    r = replies(sent)
    assert "把链接发我" in r[0] and "我先把视频打开" in r[1]
    assert s.channels.heard_texts == []  # 陪看的话不转给渠道那边 (不会被当成"去看这个视频"再做一遍)


def test_watch_unavailable_when_her_extension_off(tmp_path, monkeypatch):
    main, calls = rig(tmp_path, monkeypatch, ok=False)

    async def script(s, sent):
        await say(s, "陪我看 BV1xx411c7mD")
        assert s.watch is None

    sent, _, _ = asyncio.run(main(script))
    assert "video-sense" in replies(sent)[0] and calls == []


def test_aside_never_interrupts(tmp_path, monkeypatch):
    main, _ = rig(tmp_path, monkeypatch)

    async def script(s, sent):
        s.listening = True
        assert await s.aside("提醒", "这句不该说") is False
        s.listening = False
        await s.on_message({"type": "text", "text": "讲个笑话"})
        assert await s.aside("提醒", "这句也不该说") is False  # 她这一轮还没说完
        for _ in range(60):
            await asyncio.sleep(0.05)
            if s.current.finalized:
                break
        assert await s.aside("(提醒) 你们在一起看", "现在可以说") is True
        for _ in range(60):
            await asyncio.sleep(0.05)
            if s.current.finalized:
                break

    sent, _, s = asyncio.run(main(script))
    assert "现在可以说" in replies(sent, "watch")
    assert not any("不该说" in r for r in replies(sent))
    assert s.history[-1][0] == "(提醒) 你们在一起看"


def test_watch_ctl_buttons(tmp_path, monkeypatch):
    main, calls = rig(tmp_path, monkeypatch)

    async def script(s, sent):
        await s.on_message({"type": "watch_ctl", "action": "start", "source": "https://www.bilibili.com/video/BV1xx411c7mD"})
        assert s.watch is not None and s.watch.alive
        await s.on_message({"type": "watch_ctl", "action": "stop"})
        assert not s.watch.alive

    sent, _, s = asyncio.run(main(script))
    done = [m for m in sent if m.get("type") == "watch_ctl_done"]
    assert done[0]["active"] is True and "我先把视频打开" in done[0]["text"]
    assert done[1]["active"] is False and "不看了" in done[1]["text"]
    assert not [m for m in sent if m.get("type") == "turn_start" and m.get("source") != "watch"]  # 按钮不算你说的话


def test_plain_chat_not_hijacked(tmp_path, monkeypatch):
    main, calls = rig(tmp_path, monkeypatch)

    async def script(s, sent):
        await say(s, "今天天气怎么样")
        await say(s, "不看了")  # 没在陪看: 照常聊天

    sent, _, s = asyncio.run(main(script))
    assert calls == [] and s.watch is None
    traces = [m["trace"] for m in sent if m.get("type") == "trace"]
    assert traces and all("watch" not in t.get("meta", {}) for t in traces)
    assert s.channels.heard_texts == ["今天天气怎么样", "不看了"]  # 普通的话照常转给渠道那边


def test_watch_title_from_bili_and_watch_log(tmp_path, monkeypatch):
    """她的扩展没给标题: 桌宠会话用 B 站只读客户端查 (换成假的, 不连网); 她说话、生活日志用查到的标题; 陪看日志一步一记."""
    from wm.channels import bili as B
    seen = []

    class FakeBili:
        def __init__(self, base, timeout_s=10.0):
            seen.append(base)

        async def view(self, bvid):
            seen.append(bvid)
            return {"title": "小狗视频"}

        async def close(self):
            seen.append("closed")
    monkeypatch.setattr(B, "BiliClient", FakeBili)
    main, calls = rig(tmp_path, monkeypatch, duration=30.0, title=None)

    async def script(s, sent):
        await say(s, "陪我看 BV1xx411c7mD 我看到0分02秒了")
        for _ in range(120):
            await asyncio.sleep(0.05)
            if replies(sent, "watch"):
                break
        await asyncio.sleep(0.3)
        await say(s, "不看了")

    sent, thought, s = asyncio.run(main(script))
    assert seen == [B.BASE, "BV1xx411c7mD", "closed"]  # 匿名、只查一次, 用完关掉
    assert thought and thought[0]["title"] == "小狗视频"
    assert s.channels.diary and "和主人一起看了《小狗视频》" in s.channels.diary[0]
    rows = [json.loads(x) for f in tmp_path.glob("watch-*.jsonl") for x in f.read_text(encoding="utf-8").splitlines()]
    kinds = [r["event"] for r in rows]
    assert kinds[0] == "start" and kinds[1] == "cursor" and kinds[-1] == "end"
    assert {"probe", "segment", "line"} <= set(kinds)
    assert next(r for r in rows if r["event"] == "probe")["title"] == "小狗视频"
    assert next(r for r in rows if r["event"] == "cursor")["by"] == "user"
    # 开始时只有 source; 知道标题以后再告诉页面一次 (提示条写《标题》)
    assert {"type": "watch_state", "active": True, "source": "BV1xx411c7mD"} in sent
    assert {"type": "watch_state", "active": True, "source": "BV1xx411c7mD", "title": "小狗视频", "duration": 30.0} in sent


def test_ask_cursor_after_her_reply(tmp_path, monkeypatch):
    """11-08 实测: 只发了 BV 号, 片子一下就打开了, 她"好～我先把视频打开…"还没说完, 问进度那句只试一次就被吞了,
    你没听到, 她空等 3 分钟自己停了. 现在等她说完再问 (走真的 aside: 她这一轮没说完不插话); 你说"从头开始"就接着看."""
    main, calls = rig(tmp_path, monkeypatch, duration=30.0, extra=("watch_along.busy_retry_s=0.2",))

    async def script(s, sent):
        await say(s, "可以陪我看BV1xx411c7mD嘛？", wait=0.0)  # 不等她说完: 和真的一样, 陪看马上就开始了
        for _ in range(120):
            await asyncio.sleep(0.05)
            if any("看到几分几秒" in r for r in replies(sent, "watch")):
                break
        await asyncio.sleep(0.3)
        await say(s, "从头开始")
        for _ in range(160):
            await asyncio.sleep(0.05)
            if len(replies(sent, "watch")) >= 2:
                break
        await asyncio.sleep(0.3)
        await say(s, "不看了")

    sent, thought, s = asyncio.run(main(script))
    mine, lines = replies(sent, "text"), replies(sent, "watch")
    assert "我先把视频打开" in mine[0] and "看到几分几秒" in lines[0]
    assert "从 0:00 跟上" in mine[1] and lines[1] == "第0秒的猫好可爱"
    rows = [json.loads(x) for f in tmp_path.glob("watch-*.jsonl") for x in f.read_text(encoding="utf-8").splitlines()]
    ask = next(r for r in rows if r["event"] == "ask_cursor")
    assert ask["said"] is True
    starts = [m for m in sent if m.get("type") == "turn_start"]
    assert [m.get("source") for m in starts][:2] == ["text", "watch"]  # 她先说完开头那句, 才问


def test_screen_mode_without_link_writes_memory(tmp_path, monkeypatch):
    """11-08 用户: 说"陪我刷B站视频"不用给链接, 她就看你的屏幕 (wm/screen_along.py; 前台窗口、截图、模型都换成假的);
    陪看时你说的话记下来; 结束时记进记忆 (一起看过的视频.md 一节 + 日记"## 我自己的"一句) 和她的生活日志."""
    from PIL import Image

    from wm import screen_along as SA
    from wm.pipeline import Session as S
    n = {"i": 0}
    monkeypatch.setattr(S, "_screen_ok", lambda self: True)
    monkeypatch.setattr(SA, "foreground", lambda: {"exe": "chrome.exe", "title": "胖猫跳舞_哔哩哔哩_bilibili - Google Chrome",
                                                   "rect": (0, 0, 1600, 900), "minimized": False,
                                                   "screen_area": 1920 * 1080})

    def fake_grab(rect, width):
        n["i"] += 1
        return Image.new("RGB", (320, 180), ((n["i"] * 40) % 256, 80, 120))
    monkeypatch.setattr(SA, "grab", fake_grab)

    async def fake_think(self, ctx):
        return "happy", "这猫好胖", "一只胖猫在跳舞"
    monkeypatch.setattr(S, "_screen_think", fake_think)

    async def fake_gist(self, summary):
        return "我们看了一只胖猫跳舞, 主人说它好可爱."
    monkeypatch.setattr(S, "_watch_gist", fake_gist)
    extra = ("watch_along.screen.tick_s=0.05", "watch_along.screen.look_s=0.3", "watch_along.screen.first_look_s=0.05",
             "watch_along.screen.min_gap_s=0", "watch_along.screen.first_gap_s=0", "watch_along.screen.late_drop_s=2")
    main, calls = rig(tmp_path, monkeypatch, extra=extra)

    class FakeMem:
        def __init__(self):
            self.watch, self.diary = [], []

        def append_watch(self, head, body):
            self.watch.append((head, body))

        def write_diary(self, lines, opening=True):
            self.diary.append((lines, opening))

        def observe(self, *a):
            return []
    mem = FakeMem()

    async def script(s, sent):
        await say(s, "请你陪我一起刷B站视频", wait=0.0)
        for _ in range(120):
            await asyncio.sleep(0.05)
            if replies(sent, "watch"):
                break
        assert isinstance(s.watch, SA.ScreenAlong)
        await asyncio.sleep(0.3)
        await say(s, "这只猫好可爱")  # 普通聊天: 记下来
        await asyncio.sleep(0.5)
        s.e.memory = mem  # 只在收尾时接上假的长期记忆 (别的轮次不碰它)
        await say(s, "不看了")
        await asyncio.sleep(0.3)
        s.e.memory = None

    sent, thought, s = asyncio.run(main(script))
    mine, lines = replies(sent, "text"), replies(sent, "watch")
    assert "我只看你前台那个窗口" in mine[0] and "这猫好胖" in lines and calls == []  # 没去取视频
    states = [m for m in sent if m.get("type") == "watch_state"]
    assert states[0] == {"type": "watch_state", "active": True, "mode": "screen", "source": "screen"}
    assert any(m.get("title") == "胖猫跳舞" and m.get("app") == "Chrome" for m in states)
    assert states[-1]["active"] is False and states[-1]["mode"] == "screen" and states[-1]["reason"] == "user"
    traces = [m["trace"] for m in sent if m.get("type") == "trace"]
    chat = next(t for t in traces if t.get("text") == "这只猫好可爱")
    assert "你在陪主人看他的屏幕" in chat["meta"]["state"] and "《胖猫跳舞》" in chat["meta"]["state"]
    head, body = mem.watch[0]
    text = "\n".join(body)
    assert "《胖猫跳舞》" in head and "看主人的屏幕" in text and "讲了什么: 我们看了一只胖猫跳舞" in text
    assert "我说的: “这猫好胖”" in text and "主人说: “这只猫好可爱”" in text
    assert mem.diary and "和主人一起看了《胖猫跳舞》: 我们看了" in mem.diary[0][0][0] and mem.diary[0][1] is False
    assert s.channels.diary and s.channels.diary[0].startswith("和主人一起看了《胖猫跳舞》 (看他的屏幕")
    rows = [json.loads(x) for f in tmp_path.glob("watch-*.jsonl") for x in f.read_text(encoding="utf-8").splitlines()]
    assert rows[0] == {"time": rows[0]["time"], "event": "start", "mode": "screen"}
    assert any(r["event"] == "look" and r["result"] == "said" for r in rows) and rows[-1]["event"] == "end"


def test_watch_holds_computer_tools(tmp_path, monkeypatch):
    """11-08 实测: 屏幕陪看时你说"嗯那也就看下内容吧", 她拿你的鼠标键盘操作了 2 分 15 秒 (切窗口、读页面结构、往下滚、
    按 Alt+Tab), 最后一句没说. 现在陪看的时候不给操作电脑的那组工具和 skill; 模型万一还是叫了, 不下发、直接回一句为什么;
    不陪看了就照旧."""
    from PIL import Image

    from wm import pipeline as PL
    from wm import screen_along as SA
    from wm.pipeline import Session as S
    monkeypatch.setattr(S, "_screen_ok", lambda self: True)
    monkeypatch.setattr(SA, "foreground", lambda: {"exe": "chrome.exe", "title": "x_哔哩哔哩_bilibili", "rect": (0, 0, 1600, 900),
                                                   "minimized": False, "screen_area": 1920 * 1080})
    monkeypatch.setattr(SA, "grab", lambda rect, width: Image.new("RGB", (320, 180), (9, 9, 9)))

    async def fake_think(self, ctx):
        return "neutral", "", ""
    monkeypatch.setattr(S, "_screen_think", fake_think)
    main, calls = rig(tmp_path, monkeypatch, extra=("watch_along.screen.tick_s=0.05",))
    tool = lambda n: {"type": "function", "function": {"name": n, "description": n, "parameters": {"type": "object"}}}  # noqa: E731
    got = {}

    async def script(s, sent):
        s.tools = [tool("load_skill"), tool("mcp__computer__Scroll"), tool("mcp__computer__Shortcut"), tool("room_do")]
        s.skills = [{"name": "computer-use", "description": "x"}, {"name": "security-review", "description": "y"}]
        fake_turn = type("T", (), {"llm": object()})()
        names = [t["function"]["name"] for t in s._turn_tools()]  # 没陪看: 照旧, 外加她自己的"看一眼屏幕"
        assert names == ["load_skill", "mcp__computer__Scroll", "mcp__computer__Shortcut", "room_do", PL.LOOK_TOOL_NAME]
        assert len(s._skills(fake_turn)) == 2
        await say(s, "陪我刷B站视频")
        assert isinstance(s.watch, SA.ScreenAlong) and s.watch.alive
        got["tools"] = [t["function"]["name"] for t in s._turn_tools()]
        got["skills"] = [x["name"] for x in s._skills(fake_turn)]
        turn = s._new_turn("text", "看下内容")
        before = len(sent)
        got["results"] = await s._run_tools(turn, [{"name": "mcp__computer__Shortcut", "arguments": {"shortcut": "alt+tab"}}])
        got["sent_tool_call"] = any(m.get("type") == "tool_call" for m in sent[before:])
        got["trace"] = turn.trace.meta.get("tools")
        await say(s, "不看了")
        got["after"] = len(s._turn_tools())

    sent, _, s = asyncio.run(main(script))
    assert got["tools"] == ["load_skill", "room_do"] and got["skills"] == ["security-review"]
    assert got["results"] == [{"content": PL.WATCH_HELD_MSG, "is_error": True}] and got["sent_tool_call"] is False
    assert got["trace"][0]["held"] == "watch" and got["trace"][0]["name"] == "mcp__computer__Shortcut"
    assert got["after"] == 5  # 不陪看了: 工具照旧 (加上她自己的"看一眼屏幕")


def test_look_at_screen_is_hers(tmp_path, monkeypatch):
    """11-09 实测: 你问"你现在看得到屏幕嘛？""那直播呢？", 她没开陪看、只知道程序名, 说"直播画面我看不到".
    现在她有自己的"看一眼屏幕" (look_at_screen, 服务端做, 不经桌宠): 隐私规矩和屏幕陪看一样; 你在看视频 / 直播时,
    状态行提醒她能看一眼. 前台窗口、截图、看图的模型都换成假的."""
    from PIL import Image

    from wm import pipeline as PL
    from wm import screen_along as SA
    from wm.pipeline import Session as S
    monkeypatch.setattr(S, "_screen_ok", lambda self: True)
    fg = {"exe": "chrome.exe", "title": "某主播的直播间_哔哩哔哩直播 - Google Chrome", "rect": (0, 0, 1600, 900),
          "minimized": False, "screen_area": 1920 * 1080}
    monkeypatch.setattr(SA, "foreground", lambda: dict(fg))
    grabs = []

    def fake_grab(rect, width):
        grabs.append(rect)
        return Image.new("RGB", (320, 180), (40, 80, 120))
    monkeypatch.setattr(SA, "grab", fake_grab)
    seen = []

    async def fake_describe(self, image, title, app):
        seen.append((image[:22], title, app))
        return "一个主播在玩恐怖游戏, 弹幕刷着哈哈哈"
    monkeypatch.setattr(S, "_screen_describe", fake_describe)
    main, calls = rig(tmp_path, monkeypatch)
    got = {}

    async def script(s, sent):
        turn = s._new_turn("text", "那直播呢？")
        before = len(sent)
        got["ok"] = await s._run_tools(turn, [{"name": PL.LOOK_TOOL_NAME, "arguments": {}}])
        got["sent_tool_call"] = any(m.get("type") == "tool_call" for m in sent[before:])
        got["trace"] = turn.trace.meta.get("tools")
        fg.update(exe="weixin.exe", title="微信")  # 你切去回微信: 不截
        got["private"] = await s._run_tools(s._new_turn("text", "你看看这个"), [{"name": PL.LOOK_TOOL_NAME, "arguments": {}}])
        # 状态行: 你在看视频 / 直播时提醒她能看一眼 (假模型不是云端的, 这里换一个"云端"的轮来看)
        s.char_state_raw = {"activity": {"category": "video", "app": "哔哩哔哩", "title": "x"}}
        s.llm = type("FakeCloud", (), {"label": "cloud:fake"})()
        got["hint"] = s._new_turn("text", "那直播呢？").state
        s.char_state_raw = {"activity": {"category": "code", "app": "Claude", "title": "x"}}
        got["no_hint"] = s._new_turn("text", "好哦").state
        s.llm = s.e.llm

    sent, _, s = asyncio.run(main(script))
    assert got["ok"][0]["is_error"] is False and "一个主播在玩恐怖游戏" in got["ok"][0]["content"]
    assert "《某主播的直播间_哔哩哔哩直播》" in got["ok"][0]["content"] and got["sent_tool_call"] is False  # 不经桌宠
    assert seen and seen[0][0] == "data:image/jpeg;base64" and seen[0][2] == "Chrome"
    assert got["trace"][0]["local"] is True and got["trace"][0]["name"] == PL.LOOK_TOOL_NAME
    assert "你不看" in got["private"][0]["content"] and len(grabs) == 1  # 微信那次一张都没截
    assert "你能看一眼他的屏幕" in (got["hint"] or "") and "你能看一眼他的屏幕" not in (got["no_hint"] or "")
    rows = [json.loads(x) for f in tmp_path.glob("watch-*.jsonl") for x in f.read_text(encoding="utf-8").splitlines()]
    glances = [r for r in rows if r["event"] == "glance"]
    assert [g["result"] for g in glances] == ["ok", "private"] and "恐怖游戏" in glances[0]["saw"]


def test_screen_unavailable_asks_for_link(tmp_path, monkeypatch):
    """模型看不了图 (测试里的假模型就看不了) 或屏幕陪看关了: 照旧问你要链接."""
    main, calls = rig(tmp_path, monkeypatch)

    async def script(s, sent):
        assert s._screen_ok() is False
        await say(s, "陪我刷B站视频")
        assert s.watch is None and s.watch_wait > 0

    sent, _, _ = asyncio.run(main(script))
    assert "把链接发我" in replies(sent)[0]


def test_not_started_then_started_and_chat_knows(tmp_path, monkeypatch):
    """11-08 02:28 实测: 她问了看到几分, 你回"我还没开始看呢""啊我已经开始了", 都没认出来 (当普通聊天回了, 陪看那边一直在等);
    你问"你能看到这个画面吗", 聊天那边不知道她在陪看, 说看不到. 现在"还没开始"停在 0:00 等你, "已经开始了"从头跟;
    这时候的聊天, 状态行里有她在陪看什么、此刻在等你开始."""
    main, calls = rig(tmp_path, monkeypatch, duration=30.0, extra=("watch_along.busy_retry_s=0.2",))

    async def script(s, sent):
        await say(s, "请你陪我看看BV1xx411c7mD", wait=0.0)
        for _ in range(120):  # 等她问
            await asyncio.sleep(0.05)
            if any("看到几分几秒" in r for r in replies(sent, "watch")):
                break
        await asyncio.sleep(0.3)
        await say(s, "我还没开始看呢")
        assert s.watch.state == "paused" and s.watch.position() == 0.0
        await say(s, "你能看到这个画面吗")  # 普通聊天
        await asyncio.sleep(1.0)
        assert len(replies(sent, "watch")) == 1  # 等你开始的时候一句不说
        await say(s, "啊我已经开始了")
        for _ in range(160):
            await asyncio.sleep(0.05)
            if len(replies(sent, "watch")) >= 2:
                break
        await asyncio.sleep(0.3)
        await say(s, "不看了")

    sent, thought, s = asyncio.run(main(script))
    mine, lines = replies(sent, "text"), replies(sent, "watch")
    assert "点开始了跟我说" in mine[1] and "从头跟上" in mine[3]
    assert lines[1] == "第0秒的猫好可爱"
    traces = [m["trace"] for m in sent if m.get("type") == "trace"]
    chat = next(t for t in traces if t.get("text") == "你能看到这个画面吗")
    assert "你在陪主人看《小猫视频》" in chat["meta"]["state"] and "主人还没开始放" in chat["meta"]["state"]
    assert "watch" not in chat["meta"]  # 这句不是陪看的事, 照常问模型

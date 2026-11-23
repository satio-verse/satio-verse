"""陪看 (wm/watch_along.py): 你说的话怎么认、节奏、停不停、调她扩展那条路. 假时钟、假工具、假模型, 不碰 B 站、不调真模型."""
import asyncio
import json
import sys

import pytest

from wm import watch_along as W


# -- 你说的话 --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,sec", [
    ("看到3分20了", 200), ("现在 3:20", 200), ("1:02:03", 3723), ("第五分钟", 300), ("三分半", 210),
    ("看到十二分了", 720), ("200秒", 200), ("刚开始", 0), ("一小时零五分", 3900), ("1小时5分", 3900),
    ("二十五分十秒", 1510), ("今天好累", None),
])
def test_parse_cursor(text, sec):
    assert W.parse_cursor(text) == sec


def test_find_source(tmp_path):
    f = tmp_path / "我的视频.mp4"
    f.write_bytes(b"x")
    assert W.find_source("陪我看 https://www.bilibili.com/video/BV1xx411c7mD?p=1") == "BV1xx411c7mD"
    assert W.find_source("看这个 https://b23.tv/AbCd123") == "https://b23.tv/AbCd123"
    assert W.find_source(f"陪我看 {f}") == str(f)
    assert W.find_source(f"陪我看 {tmp_path / '不存在.mp4'}") is None
    assert W.find_source("陪我看电影") is None


def test_resolve_short_rejects_other_hosts():
    assert asyncio.run(W.resolve_short("https://evil.example.com/abc")) is None


@pytest.mark.parametrize("text,active,waiting,video,expect", [
    ("陪我看视频", False, False, False, ("start", None)),
    ("我们一起看这个吧", False, False, False, ("start", None)),
    ("陪我看 BV1xx411c7mD", False, False, False, ("start", "BV1xx411c7mD")),
    ("你也来看看", False, False, True, ("start", None)),          # 宿主说你在看视频
    ("我们一起看星星吧", False, False, False, None),               # 没说视频、也没在看
    ("今天吃什么", False, False, True, None),
    ("BV1xx411c7mD", False, True, False, ("source", "BV1xx411c7mD")),
    ("不看了", True, False, False, ("stop", None)),
    ("不看了", False, False, False, None),                        # 没在陪看: 照常聊天
    ("看到3分20了", True, False, False, ("cursor", 200)),
    ("5:30", True, False, False, ("cursor", 330)),
    ("我暂停一下", True, False, False, ("pause", None)),
    ("继续看了", True, False, False, ("resume", None)),
    ("这个人好搞笑", True, False, False, None),
])
def test_command(text, active, waiting, video, expect):
    assert W.command(text, active=active, waiting=waiting, video_now=video) == expect


@pytest.mark.parametrize("text,asking,paused,expect", [
    # 11-08 02:28 实测她问了看到几分以后你回的话 (当时都没认出来)
    ("我还没开始看呢", True, False, ("not_started", None)),
    ("啊我已经开始了", True, False, ("cursor", 0.0)),
    ("我们请你从何开始看", True, False, ("cursor", 0.0)),   # 语音识别把"从头开始看"听成了"从何开始看"
    ("你能看到这个画面吗", True, False, None),
    ("啊就是这个低站视频", True, False, None),
    ("还没点开", True, False, ("not_started", None)),
    ("我还没看呢", True, False, ("not_started", None)),
    ("开始了", True, False, ("cursor", 0.0)),
    ("才开始看", True, False, ("cursor", 0.0)),
    ("看到3分20了", True, False, ("cursor", 200)),
    ("我还没看懂", True, False, None),
    ("我没放大", True, False, None),
    ("开始了", False, True, ("resume", None)),              # 停在 0:00 等你: 你说开始了就走
    ("啊我已经开始了", False, True, ("resume", None)),
    ("开始了", False, False, None),                         # 在看着: "开始了"不是陪看的事, 照常聊天
    ("我还没开始看呢", False, False, None),                 # 没在等进度: 照常聊天
])
def test_command_while_asking_or_paused(text, asking, paused, expect):
    assert W.command(text, active=True, waiting=False, video_now=False, asking=asking, paused=paused) == expect
    assert W.command(text, active=False, waiting=False, video_now=False, asking=asking, paused=paused) is None


def test_line_messages_and_parse():
    ctx = {"title": "测试片", "segment": (60, 120), "rows": [{"from": "01:00", "to": "01:30", "text": "你好呀"},
                                                          {"from": "01:30", "to": "02:00", "text": ""}],
           "recent": ["前一句"], "images": ["data:image/jpeg;base64,AAA"], "first": True}
    msgs = W.line_messages(ctx, see=True)
    assert msgs[0]["role"] == "system" and "图是这一段" in msgs[0]["content"]
    text, image = msgs[1]["content"][0]["text"], msgs[1]["content"][1]
    assert "《测试片》 1:00-2:00" in text and "[1:00-1:30] 你好呀" in text and "(这一格没台词)" in text
    assert "前一句" in text and image["image_url"]["url"].startswith("data:image/jpeg")
    plain = W.line_messages(ctx, see=False)
    assert isinstance(plain[1]["content"], str) and "图是" not in plain[0]["content"]
    assert W.parse_line('{"emo": "happy", "reply": "哈哈这也太离谱了"}') == ("happy", "哈哈这也太离谱了")
    assert W.parse_line('{"emo": "不存在", "reply": ""}') == ("neutral", "")
    assert W.parse_line("不是 JSON") == ("neutral", "")
    emo, long = W.parse_line(json.dumps({"emo": "happy", "reply": "好" * 50 + "。" + "啊" * 60}))
    assert len(long) <= 80 and long.endswith("。")


def test_can_see():
    class L:
        def __init__(self, provider, model, local=False, info=None):
            self.provider, self.model, self.local, self.info = provider, model, local, info or {}
    assert W.can_see(L("deepseek", "deepseek-v4-flash"))
    assert W.can_see(L("qwen", "qwen-vl-max"))
    assert not W.can_see(L("deepseek", "deepseek-chat"))
    assert W.can_see(L("selfhost", "x", local=True, info={"vision": True}))
    assert not W.can_see(object())  # 内置小模型
    assert W.can_see(object(), vision=True) and not W.can_see(L("qwen", "qwen-vl"), vision=False)


# -- 一次陪看 --------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += max(0.0, s)
        await asyncio.sleep(0)


class Rig:
    """假工具 / 假模型 / 假播放器 / 假桌宠, 都记账."""

    def __init__(self, tmp_path, duration=200.0, start_pos=30.0, player_pos=True, title="测试片"):
        self.clock = Clock()
        self.duration, self.start_pos, self.player_pos, self.title = duration, start_pos, player_pos, title
        self.t0 = self.clock.t
        self.calls, self.said, self.thought, self.ended = [], [], [], []
        self.busy_until = 0.0
        self.player_status = "Playing"
        self.offset = 0.0
        self.workdir = tmp_path / "watch"
        self.workdir.mkdir()
        (self.workdir / "out").mkdir()
        self.local = self.workdir / "out" / "v.mp4"
        self.local.write_bytes(b"video")
        self.probe = {"duration": duration, "title": title, "input": "out/v.mp4"}
        self.reply = lambda ctx: ("happy", f"第{int(ctx['segment'][0])}秒这段好看")

    def pos(self):
        return self.start_pos + (self.clock.t - self.t0) + self.offset

    async def tool(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "video_probe":
            return dict(self.probe), []
        a, seg = float(args["cursor"]), float(args["segment_s"])
        b = min(self.duration, a + seg)
        return {"status": "ok", "segment": {"from": W.clock(a), "to": W.clock(b)},
                "storyboard": [{"from": W.clock(a), "to": W.clock(b), "text": f"台词{int(a)}"}]}, ["data:image/jpeg;base64,AAA"]

    async def speak(self, note, reply, emo):
        if self.clock.t < self.busy_until:
            return False
        self.said.append((round(self.pos(), 1), reply, emo, note, self.clock.t))
        return True

    async def think(self, ctx):
        self.thought.append(ctx)
        return self.reply(ctx)

    async def player(self):
        return {"status": self.player_status, "title": self.title,
                "position": self.pos() if self.player_pos else None, "age": 0.0}

    async def on_end(self, summary):
        self.ended.append(summary)

    def make(self, title_of=None, trace=None, **conf):
        return W.WatchAlong(conf, "BV1xx411c7mD", tool=self.tool, speak=self.speak, think=self.think,
                            player=self.player, workdir=self.workdir, clock_fn=self.clock.now,
                            sleep=self.clock.sleep, on_end=self.on_end, title_of=title_of, trace=trace)


async def _run(w, steps=4000, hook=None):
    w.start()
    for i in range(steps):
        if w.task.done():
            break
        if hook is not None:
            hook(i)
        await asyncio.sleep(0)
    await asyncio.wait_for(w.task, 5)


def test_whole_video_lines_at_segment_end(tmp_path):
    rig = Rig(tmp_path)
    w = rig.make()
    asyncio.run(_run(w))
    # 30 秒开始: [30, 90] 在 75 说; [90, 200] (第二段 120 秒, 片尾截到 200) 在 185 说; 然后看完
    assert [s[0] for s in rig.said] == [75.0, 185.0]
    assert [s[1] for s in rig.said] == ["第30秒这段好看", "第90秒这段好看"]
    assert "主人刚看完 0:30-1:30" in rig.said[0][3]
    assert rig.thought[0]["first"] and rig.thought[-1]["last"]
    watch_calls = [a for n, a in rig.calls if n == "video_watch"]
    assert [int(a["cursor"]) for a in watch_calls] == [30, 90]
    assert all(a["source"].endswith("v.mp4") for a in watch_calls)  # 取下来以后用本地文件, 不再重复取
    assert rig.calls[0] == ("video_probe", {"source": "BV1xx411c7mD"})
    assert w.state == "done" and w.reason == "finished"
    assert rig.ended and rig.ended[0]["said"] == 2 and rig.ended[0]["title"] == "测试片"
    assert not rig.workdir.exists()  # 临时目录 (取下来的视频、拼图) 删了


def test_asks_cursor_when_player_has_no_position(tmp_path):
    rig = Rig(tmp_path, player_pos=False)
    w = rig.make()

    def hook(i):
        if w.state == "need_cursor" and w.base_pos is None and rig.said:
            rig.t0, rig.start_pos = rig.clock.t, 100.0
            w.set_cursor(100.0)  # 你说 "看到 1 分 40 了"
    asyncio.run(_run(w, hook=hook))
    assert "看到几分几秒" in rig.said[0][1]
    first = int([a for n, a in rig.calls if n == "video_watch"][0]["cursor"])
    assert 100 <= first <= 111  # 从你说的位置 (加上她回过神来的那几秒) 开始
    lines = [s for s in rig.said[1:]]
    assert lines[0][0] == first + 60 - 15 and lines[0][1] == f"第{first}秒这段好看"
    assert w.stats["asked"] == 1


def test_no_cursor_reply_gives_up(tmp_path):
    rig = Rig(tmp_path, player_pos=False)
    w = rig.make(ask_cursor_s=60)
    asyncio.run(_run(w))
    assert w.reason == "no_cursor" and len(rig.said) == 2 and rig.ended
    assert "看到几分几秒" in rig.said[0][1] and "先不跟" in rig.said[1][1]  # 问了没回: 说一声再停
    assert rig.said[1][4] - rig.said[0][4] >= 60  # 从问出口算起等满


def test_waits_while_you_talk_then_drops_when_late(tmp_path):
    rig = Rig(tmp_path)
    rig.busy_until = 1000.0 + 45 + 5   # 到点时你还在说 5 秒: 等一会儿再说
    w = rig.make()
    asyncio.run(_run(w))
    assert rig.said[0][0] >= 80.0 and rig.said[0][0] <= 81.0
    rig2 = Rig(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    rig2.busy_until = 1000.0 + 45 + 60  # 一直在说, 过了 20 秒: 这一句作废
    events = []
    w2 = rig2.make(trace=events.append)
    w2.blocked = lambda: "listening"
    asyncio.run(_run(w2))
    assert rig2.said[0][1] == "第90秒这段好看" and w2.stats["dropped"] == 1
    dropped = next(e for e in events if e["event"] == "line" and e["result"] == "dropped")
    assert dropped["why"] == "listening"  # 日志里记着为什么没说出口 (11-08: 外放时麦克风把视频声当成有人说话)
    assert "不用去操作电脑" in w2.note() and "台词" in w2.note()


def test_network_blip_mid_video(tmp_path):
    """连不上模型一下 (11-08 15:01 实测屏幕陪看就这么停了): 这一段不说, 接着看下一段."""
    rig = Rig(tmp_path)
    calls = {"n": 0}
    ok = rig.reply

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("连不上 DeepSeek")
        return ok(ctx)
    rig.reply = flaky
    events = []
    w = rig.make(trace=events.append)
    asyncio.run(_run(w))
    lines = [e for e in events if e["event"] == "line"]
    assert [e["result"] for e in lines] == ["error", "said"] and "连不上" in lines[0]["error"]
    assert w.reason == "finished" and [s[1] for s in rig.said] == ["第90秒这段好看"] and rig.ended[0]["errors"] == 1


def test_network_down_mid_video_says_so(tmp_path):
    rig = Rig(tmp_path, duration=900)

    def down(ctx):
        raise RuntimeError("连不上 DeepSeek")
    rig.reply = down
    w = rig.make()
    asyncio.run(_run(w, steps=12000))
    assert w.reason == "model_down" and rig.said[-1][1] == W.DOWN_LINE and rig.ended[0]["errors"] == 3
    assert len([n for n, _ in rig.calls if n == "video_watch"]) == 3  # 连着三段都没连上, 说一声停


def test_silent_segments_not_spoken(tmp_path):
    rig = Rig(tmp_path)
    rig.reply = lambda ctx: ("neutral", "")
    w = rig.make()
    asyncio.run(_run(w))
    assert rig.said == [] and w.stats["silent"] == 2


def test_player_pause_holds_the_line(tmp_path):
    rig = Rig(tmp_path, player_pos=False)
    w = rig.make()
    state = {"paused_at": None}

    def hook(i):
        if w.state == "need_cursor" and w.base_pos is None and rig.said:
            rig.t0, rig.start_pos = rig.clock.t, 30.0
            w.set_cursor(30.0)
        # 播到 70 秒时你按了暂停, 停 100 秒再放
        if w.state == "watching" and rig.player_status == "Playing" and state["paused_at"] is None and rig.pos() >= 70:
            rig.player_status, state["paused_at"] = "Paused", rig.clock.t
        if state["paused_at"] is not None and rig.player_status == "Paused" and rig.clock.t - state["paused_at"] >= 100:
            rig.player_status = "Playing"
            rig.offset -= rig.clock.t - state["paused_at"]  # 暂停的时间片子没走
    asyncio.run(_run(w, hook=hook))
    start = int([a for n, a in rig.calls if n == "video_watch"][0]["cursor"])
    first = rig.said[1]
    assert first[1] == f"第{start}秒这段好看" and first[0] >= start + 45
    paused = (state["paused_at"], state["paused_at"] + 100)
    assert not any(paused[0] <= s[4] < paused[1] for s in rig.said[1:])  # 暂停的时候一句没说
    assert w.reason == "finished"


def test_seek_restarts_from_new_position(tmp_path):
    rig = Rig(tmp_path, duration=900)
    w = rig.make()
    done = {"seek": False}

    def hook(i):
        if not done["seek"] and w.state == "watching" and rig.pos() >= 60:
            done["seek"] = True
            rig.offset += 400          # 你往后拖到 460 秒
            w.set_cursor(rig.pos())
    asyncio.run(_run(w, hook=hook, steps=8000))
    starts = [int(a["cursor"]) for n, a in rig.calls if n == "video_watch"]
    assert starts[0] == 30 and any(400 <= s <= 470 for s in starts[1:])
    assert all(not (30 <= s[0] < 90) or s[1] != "第30秒这段好看" for s in rig.said)  # 跳走那一段的话作废


def test_title_change_stops(tmp_path):
    rig = Rig(tmp_path)
    w = rig.make()

    def hook(i):
        if rig.pos() >= 50:
            rig.title = "另一个视频"
    asyncio.run(_run(w, hook=hook))
    assert w.reason == "changed" and rig.ended
    assert "换视频" in rig.said[-1][1]  # 说一声再停, 别悄悄停了


def test_too_long_and_unopenable(tmp_path):
    rig = Rig(tmp_path, duration=4 * 3600)
    w = rig.make()
    asyncio.run(_run(w))
    assert w.reason == "too_long" and "太长" in rig.said[0][1] and len(rig.calls) == 1
    rig2 = Rig(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path)
    rig2.probe = {"status": "error", "error": "拿不到播放地址"}
    w2 = rig2.make()
    asyncio.run(_run(w2))
    assert w2.reason == "probe_failed" and "打不开" in rig2.said[0][1]


def test_tool_error_says_so_and_ends(tmp_path):
    rig = Rig(tmp_path)

    async def broken(name, args):
        if name == "video_watch":
            raise TimeoutError("MCP 请求超时")
        return dict(rig.probe), []
    rig.tool = broken
    w = rig.make()
    asyncio.run(_run(w))
    assert w.reason == "error: TimeoutError" and "没打开" in rig.said[-1][1] and rig.ended and not rig.workdir.exists()


def test_stop_midway_cleans_up_once(tmp_path):
    rig = Rig(tmp_path, duration=900)
    w = rig.make()

    async def go():
        w.start()
        for _ in range(4000):
            await asyncio.sleep(0)
            if rig.said:
                break
        await w.stop("user")
        await w.stop("user")
    asyncio.run(go())
    assert w.state == "done" and w.reason == "user" and len(rig.ended) == 1 and not rig.workdir.exists()


def test_user_pause_and_resume(tmp_path):
    rig = Rig(tmp_path, player_pos=False)
    w = rig.make(player_check=False)
    flags = {"p": False, "r": False}

    def hook(i):
        if w.state == "need_cursor" and w.base_pos is None and rig.said:
            rig.t0, rig.start_pos = rig.clock.t, 0.0
            w.set_cursor(0.0)
        if w.state == "watching" and not flags["p"] and (w.position() or 0) >= 20:
            flags["p"] = True
            w.pause()
            rig.pause_t = rig.clock.t
        if flags["p"] and not flags["r"] and rig.clock.t - rig.pause_t >= 300:
            flags["r"] = True
            w.resume()
    asyncio.run(_run(w, hook=hook))
    first = int([a for n, a in rig.calls if n == "video_watch"][0]["cursor"])
    said = rig.said[1]
    assert first <= 11 and said[1] == f"第{first}秒这段好看"
    assert rig.clock.t > 1300  # 暂停的 300 秒没算进片子里


# -- 调她扩展那条路 (真起一个假 MCP 服务端子进程) -------------------------------------------------------------------

FAKE_SERVER = r'''
import json, sys, os
def send(m): sys.stdout.write(json.dumps(m, ensure_ascii=False) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    m = json.loads(line)
    rid, method = m.get("id"), m.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [{"name": "video_watch", "description": "x", "inputSchema": {"type": "object"}}]}})
    elif method == "tools/call":
        a = m["params"]["arguments"]
        open("cwd.txt", "w").write(os.getcwd())
        send({"jsonrpc": "2.0", "id": rid, "result": {"isError": False, "content": [
            {"type": "text", "text": json.dumps({"status": "ok", "cursor": a.get("cursor")})},
            {"type": "image", "mimeType": "image/jpeg", "data": "AAAA"}]}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
'''


class FakeRegistry:
    def __init__(self, folder):
        self.folder = folder

    def server(self, server_id, credentials=False):
        if server_id != W.EXT:
            raise ValueError("MCP 未启用")
        return {"id": server_id, "command": sys.executable, "args": ["server.py"], "cwd": str(self.folder),
                "transport": "stdio", "source": "creator"}


def test_her_tool_runs_her_server_in_workdir(tmp_path):
    folder = tmp_path / "rev"
    folder.mkdir()
    (folder / "server.py").write_text(FAKE_SERVER, encoding="utf-8")
    work = tmp_path / "watch-work"
    tool = W.HerTool(FakeRegistry(folder), work, timeout=30)
    cfg = tool.config()
    assert cfg["args"] == [str(folder / "server.py")] and cfg["cwd"] == str(work)
    data, images = asyncio.run(tool.call("video_watch", {"source": "x", "cursor": "30"}))
    assert data == {"status": "ok", "cursor": "30"} and images == ["data:image/jpeg;base64,AAAA"]
    assert (work / "cwd.txt").read_text() == str(work)  # 她的脚本在临时目录里跑 (B 站的视频下在这里)
    with pytest.raises(ValueError):
        asyncio.run(tool.call("video_nope", {}))
    with pytest.raises(ValueError):
        W.HerTool(FakeRegistry(folder), work, server_id="creator:other").config()


# -- 认准播放器 (2026-11-07 用户试的那次: 你在浏览器里看, 浏览器没报; 系统里只有 B 站客户端报了一个停着的别的视频,
#    她当成你暂停了, 一直停在 0:45) ----------------------------------------------------------------------------------

OTHER = {"status": "Paused", "title": "别的视频：某战队赛后采访", "app": "com.bilibili.bilibiliPC", "position": None, "age": None}


def test_parse_sessions_real_shapes():
    # B 站客户端不报进度 (position 0、end 0、刷新时间是 1601 年); 浏览器标签页报进度和刷新时间
    raw = json.dumps([{**OTHER, "position": 0, "end": 0, "updated": -11644473600000},
                      {"app": "firefox.exe", "status": "Playing", "position": 42.5, "end": 879.6,
                       "updated": 1794099998000, "title": "测试片_哔哩哔哩_bilibili"}], ensure_ascii=False)
    rows = W.parse_sessions(raw, now=1794100000.0)
    assert rows[0] == OTHER
    assert rows[1]["position"] == 42.5 and rows[1]["age"] == 2.0 and rows[1]["status"] == "Playing"
    assert W.parse_sessions('{"error": "timeout"}') is None and W.parse_sessions("不是 JSON") is None
    assert W.parse_sessions("[]") == []


def test_same_title():
    assert W.same_title("【技术解读】不让编程Agent自己宣布做完了_哔哩哔哩_bilibili", "【技术解读】不让编程 Agent 自己宣布做完了")
    assert W.same_title("测试片_哔哩哔哩_bilibili", "测试片")  # 短标题: 去掉站名尾巴以后一模一样
    assert W.same_title("My Movie (2023).mp4", "My Movie (2023)")  # 播放器报带扩展名的文件名
    assert not W.same_title(OTHER["title"], "【技术解读】不让编程Agent自己宣布做完了")
    assert W.same_title("第 1 集", "第1集") and not W.same_title("第1集", "第11集")  # 太短的要一模一样
    assert not W.same_title("", "测试片") and not W.same_title("测试片", None)


def _say_cursor_when_asked(rig, w, at=0.0):
    def hook(i):
        if w.state == "need_cursor" and w.base_pos is None and rig.said:
            rig.t0, rig.start_pos = rig.clock.t, at
            w.set_cursor(at)  # 你说 "从头开始看的"
    return hook


def test_unrelated_paused_session_is_ignored(tmp_path):
    rig = Rig(tmp_path, player_pos=False)

    async def player():
        return [dict(OTHER)]
    rig.player = player
    events = []
    w = rig.make(trace=events.append)
    asyncio.run(_run(w, hook=_say_cursor_when_asked(rig, w)))
    assert "看到几分几秒" in rig.said[0][1]  # 那个会话认不出是这个视频: 照样问你
    first = int([a for n, a in rig.calls if n == "video_watch"][0]["cursor"])
    lines = rig.said[1:]
    assert len(lines) == 3 and lines[0][0] == first + 60 - 15  # 一段一句, 没被那个"暂停"卡住
    assert w.reason == "finished" and w.pause_by == "" and w.player_app is None
    assert not any(e["event"] == "pause" for e in events)


def test_title_lookup_binds_the_right_session(tmp_path):
    """她的扩展没给标题 (一般会给; 没给就查): 查到标题后认出浏览器里那个 (带尾巴), 旁边停着的 B 站客户端不理."""
    rig = Rig(tmp_path)
    rig.probe = {"duration": 200.0, "input": "out/v.mp4"}
    asked = []

    async def title_of(source):
        asked.append(source)
        return "测试片"

    async def player():
        return [dict(OTHER), {"status": rig.player_status, "title": "测试片_哔哩哔哩_bilibili", "app": "firefox.exe",
                              "position": rig.pos(), "age": 0.0}]
    rig.player = player
    events, ready = [], []
    w = rig.make(title_of=title_of, trace=events.append)

    async def on_ready(info):
        ready.append(info)
    w.on_ready = on_ready
    asyncio.run(_run(w))
    assert ready == [{"source": "BV1xx411c7mD", "title": "测试片", "duration": 200.0}]  # 只告诉一次
    assert asked == ["BV1xx411c7mD"] and w.title == "测试片" and rig.thought[0]["title"] == "测试片"
    assert [s[0] for s in rig.said] == [75.0, 185.0]  # 进度从浏览器那个会话来 (30 秒开始), 和整片那条一样
    assert w.player_app == "firefox.exe" and rig.ended[0]["title"] == "测试片"
    bound = next(e for e in events if e["event"] == "player")
    assert bound["action"] == "bound" and bound["app"] == "firefox.exe"


def test_matched_session_pause_still_counts(tmp_path):
    """认准的那个会话报暂停: 照样停表, 它又在放了就接着走 (别的会话停着不算)."""
    rig = Rig(tmp_path, player_pos=False)
    rig.probe = {"duration": 200.0, "input": "out/v.mp4"}

    async def title_of(source):
        return "测试片"

    async def player():
        return [dict(OTHER), {"status": rig.player_status, "title": "测试片", "app": "chrome.exe", "position": None,
                              "age": None}]
    rig.player = player
    events = []
    w = rig.make(title_of=title_of, trace=events.append)
    state = {"paused_at": None}
    ask = _say_cursor_when_asked(rig, w)

    def hook(i):
        ask(i)
        if w.state == "watching" and rig.player_status == "Playing" and state["paused_at"] is None and (w.position() or 0) >= 30:
            rig.player_status, state["paused_at"] = "Paused", rig.clock.t
        if state["paused_at"] is not None and rig.player_status == "Paused" and rig.clock.t - state["paused_at"] >= 100:
            rig.player_status = "Playing"
    asyncio.run(_run(w, hook=hook, steps=8000))
    pauses = [e for e in events if e["event"] == "pause"]
    assert pauses and pauses[0]["by"] == "player" and pauses[0]["app"] == "chrome.exe"
    assert any(e["event"] == "resume" for e in events) and w.reason == "finished"


def test_no_title_trusts_no_session(tmp_path):
    """标题查不到 (只有 BV 号): 谁都不认, 照样问你看到哪了、照你说的走; 查标题出错只记一笔."""
    rig = Rig(tmp_path, player_pos=False)
    rig.probe = {"duration": 200.0, "input": "out/v.mp4"}

    async def title_of(source):
        raise RuntimeError("B 站接口超时")

    async def player():
        return [{"status": "Paused", "title": "v", "app": "x.exe", "position": None, "age": None}]
    rig.player = player
    events = []
    w = rig.make(title_of=title_of, trace=events.append)
    asyncio.run(_run(w, hook=_say_cursor_when_asked(rig, w)))
    assert w.match_title == "" and w.pause_by == "" and w.reason == "finished" and len(rig.said) == 4
    assert any(e["event"] == "title_lookup_failed" and "超时" in e["error"] for e in events)


def test_trace_records_each_step(tmp_path):
    rig = Rig(tmp_path)
    events = []
    w = rig.make(trace=events.append)
    asyncio.run(_run(w))
    kinds = [e["event"] for e in events]
    assert kinds[:3] == ["probe", "player", "cursor"] and kinds.count("segment") == 2
    assert events[0]["title"] == "测试片" and events[2]["by"] == "player"
    lines = [e for e in events if e["event"] == "line"]
    assert [e["result"] for e in lines] == ["said", "said"] and lines[0]["reply"] == "第30秒这段好看"


def test_line_budget_leaves_room_to_think():
    # deepseek-flash 会先想: 11-07 那次 160 个 token 全花在想上, 那一句是空的
    assert W.DEFAULTS["max_tokens"] >= 400


# -- 11-08 实测: 片子 2 秒就打开了, 她上一句还没说完, 问进度那句只试一次被吞了, 空等 3 分钟 ----------------------------

def test_ask_waits_until_she_has_finished_talking(tmp_path):
    rig = Rig(tmp_path, player_pos=False)
    rig.busy_until = 1000.0 + 8  # "好～我先把视频打开…"还要说 8 秒
    events = []
    w = rig.make(trace=events.append)
    asyncio.run(_run(w, hook=_say_cursor_when_asked(rig, w)))
    assert "看到几分几秒" in rig.said[0][1] and rig.said[0][4] >= 1008  # 等她说完才问
    ask = next(e for e in events if e["event"] == "ask_cursor")
    assert ask["said"] is True and ask["waited_s"] >= 8
    assert w.stats["asked"] == 1 and w.reason == "finished" and len(rig.said) == 4  # 你回了: 接着一段一句


def test_cursor_given_before_she_could_ask(tmp_path):
    """她还没来得及问, 你就说了"从头开始": 不问了, 照你说的走."""
    rig = Rig(tmp_path, player_pos=False)
    rig.busy_until = 1000.0 + 8
    w = rig.make()

    def hook(i):
        if w.state == "need_cursor" and w.base_pos is None and rig.clock.t >= 1004:
            rig.t0, rig.start_pos = rig.clock.t, 0.0
            w.set_cursor(0.0)
    asyncio.run(_run(w, hook=hook))
    assert not any("看到几分几秒" in s[1] for s in rig.said) and w.stats["asked"] == 0
    assert w.reason == "finished" and len(rig.said) == 3


def test_never_got_a_turn_to_ask(tmp_path):
    """一直轮不到她说 (你一直在说话): 等满 ask_cursor_s 就停, 不再空等第二个 ask_cursor_s; 没问出口就不说"你没回"."""
    rig = Rig(tmp_path, player_pos=False)
    rig.busy_until = 1e9
    events = []
    w = rig.make(ask_cursor_s=60, trace=events.append)
    asyncio.run(_run(w))
    ask = next(e for e in events if e["event"] == "ask_cursor")
    assert ask["said"] is False and w.reason == "no_cursor" and rig.said == [] and w.stats["asked"] == 0
    assert rig.clock.t - 1000 < 70


def test_notices_wait_for_her_to_finish(tmp_path):
    """打不开、太长也等她说完再说 (以前只试一次: 赶上她在说话, 就悄悄停了)."""
    cases = [({"status": "error", "error": "拿不到播放地址"}, "probe_failed", "打不开"),
             ({"duration": 4 * 3600, "title": "测试片", "input": "out/v.mp4"}, "too_long", "太长")]
    for n, (probe, reason, word) in enumerate(cases):
        base = tmp_path / f"n{n}"
        base.mkdir()
        rig = Rig(base)
        rig.probe, rig.busy_until = probe, 1000.0 + 8
        w = rig.make()
        asyncio.run(_run(w))
        assert w.reason == reason and len(rig.said) == 1 and word in rig.said[0][1] and rig.said[0][4] >= 1008


def test_error_notice_waits_too(tmp_path):
    rig = Rig(tmp_path)

    async def broken(name, args):
        if name == "video_watch":
            raise TimeoutError("MCP 请求超时")
        return dict(rig.probe), []
    rig.tool, rig.busy_until = broken, 1000.0 + 8
    w = rig.make()
    asyncio.run(_run(w))
    assert w.reason == "error: TimeoutError" and "没打开" in rig.said[-1][1] and rig.said[-1][4] >= 1008
    assert len(rig.ended) == 1 and not rig.workdir.exists()


def test_hold_at_start_until_you_start(tmp_path):
    """你说"还没开始": 停在 0:00 等你, 一句不说; 你说"开始了"再从头跟."""
    rig = Rig(tmp_path, player_pos=False)
    events = []
    w = rig.make(player_check=False, trace=events.append)
    st = {"held": None, "go": False}

    def hook(i):
        if w.state == "need_cursor" and w.base_pos is None and rig.said and st["held"] is None:
            w.hold_at_start()
            st["held"] = rig.clock.t
        if st["held"] is not None and not st["go"] and rig.clock.t - st["held"] >= 100:
            st["go"] = True
            rig.t0, rig.start_pos = rig.clock.t, 0.0
            w.resume()
    asyncio.run(_run(w, hook=hook, steps=8000))
    lines = rig.said[1:]
    assert lines and all(s[4] >= st["held"] + 100 for s in lines)  # 等你的时候一句没说
    assert lines[0][1] == "第0秒这段好看" and lines[0][0] == 45.0
    assert [e["event"] for e in events if e["event"] in ("pause", "resume")] == ["pause", "resume"]
    assert w.reason == "finished"


def test_note_tells_her_what_she_is_doing(tmp_path):
    rig = Rig(tmp_path)
    w = rig.make()
    assert "视频还在打开" in w.note()
    w.title, w.state = "测试片", "need_cursor"
    n = w.note()
    assert "你在陪主人看《测试片》" in n and "不看屏幕" in n and "在等主人说他看到几分了" in n
    w.hold_at_start()
    assert "主人还没开始放" in w.note()
    w.resume()
    rig.clock.t += 75
    assert "跟到第 1:15" in w.note()
    w.pause()
    assert "主人暂停在 1:15" in w.note()


def test_changed_notice_waits_too(tmp_path):
    rig = Rig(tmp_path)
    w = rig.make()
    busy = {}

    def hook(i):
        if rig.pos() >= 50 and rig.title != "另一个视频":
            rig.title, rig.busy_until = "另一个视频", rig.clock.t + 30  # 你换片的时候她正好在说别的
            busy["until"] = rig.busy_until
    asyncio.run(_run(w, hook=hook))
    assert w.reason == "changed" and "换视频" in rig.said[-1][1] and rig.said[-1][4] >= busy["until"]
    assert len(rig.ended) == 1

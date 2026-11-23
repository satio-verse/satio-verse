"""QQ 上让查东西: 有 DeepSeek 官方搜索先用它, 没有再 DuckDuckGo + 必应, 结果当参考回来 (网页内容只当资料)."""
import asyncio

from wm.channels.manager import ChannelManager, WEB_HINT


class FakeCloud:
    def __init__(self, hits):
        self.hits, self.calls = hits, []

    def can_search(self):
        return True

    async def tool_search(self, query, count=6):
        self.calls.append((query, count))
        return self.hits


def manager(cloud):
    m = ChannelManager.__new__(ChannelManager)
    m.cloud = cloud

    async def diary(_text):
        pass

    m._diary = diary
    return m


def test_hint():
    assert WEB_HINT.search("帮我查一下明天天气")
    assert WEB_HINT.search("搜一下 DeepSeek 新版本")
    assert not WEB_HINT.search("今天吃什么好")


def test_lookup_returns_reference():
    async def run():
        c = FakeCloud([{"title": "DeepSeek V4", "summary": "发布了 V4", "url": "https://example.com/v4"}])
        m = manager(c)
        note = await m.web_lookup("帮我查一下 DeepSeek V4")
        assert c.calls and c.calls[0][0] == "帮我查一下 DeepSeek V4", c.calls
        assert "DeepSeek V4" in note and "https://example.com/v4" in note
        assert "只是资料" in note

    asyncio.run(run())


def test_without_searcher_falls_back_to_ddg_bing(monkeypatch):
    """没有 DeepSeek 官方搜索 (本机模型、别家): 退回 DuckDuckGo + 必应 (工作台 web_search 那一套; 这里换成替身, 不上网)."""
    from wm.agent import tools as T
    asked = []

    async def fake_search(query, n=6, searcher=None, meta=None):
        asked.append((query, n))
        return "1. 明天天气\n   https://example.com/w\n   晴, 12 到 20 度\n2. 另一条\n   https://example.com/b"

    monkeypatch.setattr(T, "web_search", fake_search)

    async def run():
        note = await manager(None).web_lookup("帮我查一下明天天气")
        assert asked == [("帮我查一下明天天气", 5)], asked
        assert "明天天气" in note and "https://example.com/w" in note and "晴, 12 到 20 度" in note, note
        assert "https://example.com/b" in note and "只是资料" in note, note

    asyncio.run(run())


def test_fallback_finds_nothing_says_so(monkeypatch):
    from wm import cloud as C
    from wm.agent import tools as T

    async def fake_search(query, n=6, searcher=None, meta=None):
        return "(没搜到; DuckDuckGo: 没搜到; 必应: 没搜到)"

    class Broken(FakeCloud):
        async def tool_search(self, query, count=6):
            raise C.CloudError("quota", "DeepSeek 搜索回 HTTP 402")

    monkeypatch.setattr(T, "web_search", fake_search)

    async def run():
        note = await manager(None).web_lookup("搜一下")
        assert "没搜成" in note and "必应" in note, note
        note = await manager(Broken([])).web_lookup("搜一下")  # DeepSeek 出错: 也退回去, 都不行时说 DeepSeek 的原因
        assert "没搜成" in note and "402" in note, note

    asyncio.run(run())

"""The browser fallback must not execute third-party code on the app origin."""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wm.app import CFG, api_webview
from wm.agent import tools


def test_reader_escapes_page_code_and_blocks_foreign_origin(monkeypatch):
    calls=[]
    async def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return '<script>fetch("/api/agent")</script>\n正文 & 证据'
    monkeypatch.setattr(tools, 'web_fetch', fetch)
    async def scenario():
        app=web.Application();app[CFG]={'server':{}}
        app.router.add_get('/api/webview', api_webview)
        async with TestClient(TestServer(app)) as client:
            response=await client.get('/api/webview',params={'url':'https://example.org/article','offset':'20000'})
            assert response.status==200
            text=await response.text()
            assert '&lt;script&gt;' in text and '<script>' not in text
            assert "sandbox; default-src 'none'" in response.headers['Content-Security-Policy']
            assert response.headers['X-Content-Type-Options']=='nosniff'
            assert calls==[('https://example.org/article',{'max_chars':20000,'offset':20000})]
            foreign=await client.get('/api/webview',params={'url':'https://example.org'},headers={'Origin':'https://untrusted.example'})
            assert foreign.status==403 and len(calls)==1
            invalid=await client.get('/api/webview',params={'url':'file:///C:/private'})
            assert invalid.status==400 and len(calls)==1
    asyncio.run(scenario())

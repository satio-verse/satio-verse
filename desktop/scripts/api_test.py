"""桌宠本机接口 (M2) 的自检: OpenAI 兼容 /v1 (用官方 openai SDK) 和 MCP /mcp (原始 JSON-RPC, 新旧两版协议) 以及 stdio 转发.

只连自检起的那个桌宠 (apitest.ps1: mock 服务 8794 + 桌宠接口 8795, 页面静音), 不碰 8790 和用户的桌宠.
    python desktop/scripts/api_test.py --port 8795 --exe desktop/build/dev/bin/wmpet.exe
结果一行一项打印出来, 有失败时退出码 1.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time

import httpx
import openai

MODERN = "2026-07-28"
RESULTS: list[dict] = []


def check(name: str, ok: bool, **info) -> bool:
    RESULTS.append({"name": name, "ok": bool(ok), **info})
    print(json.dumps(RESULTS[-1], ensure_ascii=False), flush=True)
    return ok


def token_path() -> str:
    return os.path.join(os.environ["APPDATA"], "WorldModel", "pet-api-token.txt")


class Mcp:
    """原始 JSON-RPC: 新版每个请求带三个头和 _meta, 旧版不带."""

    def __init__(self, base: str, token: str):
        self.base, self.token, self.n = base, token, 0

    def post(self, method: str, params: dict | None = None, *, modern=True, notify=False, headers=None, version=MODERN,
             timeout=200):
        self.n += 1
        params = dict(params or {})
        if modern:
            params["_meta"] = {"io.modelcontextprotocol/protocolVersion": version,
                               "io.modelcontextprotocol/clientInfo": {"name": "api_test", "version": "0"},
                               "io.modelcontextprotocol/clientCapabilities": {}}
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            body["id"] = self.n
        h = {"Authorization": f"Bearer {self.token}", "Accept": "application/json, text/event-stream"}
        if modern:
            h |= {"MCP-Protocol-Version": version, "Mcp-Method": method}
            if method == "tools/call":
                h["Mcp-Name"] = params["name"]
        h |= headers or {}
        r = httpx.post(f"{self.base}/mcp", json=body, headers=h, timeout=timeout)
        return r.status_code, (r.json() if r.content else None)

    def tool(self, name: str, args: dict | None = None, **kw):
        status, body = self.post("tools/call", {"name": name, "arguments": args or {}}, **kw)
        return status, body, (body or {}).get("result") or {}


def wait_ready(base: str, token: str, seconds: float) -> bool:
    """等页面就绪 (角色加载完、连上 mock 服务): 能拿到 pet_state 且 page.connected."""
    m = Mcp(base, token)
    end = time.time() + seconds
    while time.time() < end:
        try:
            _, _, res = m.tool("pet_state")
            page = (res.get("structuredContent") or {}).get("page") or {}
            if page.get("connected"):
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return False


def test_security(base: str, token: str) -> None:
    r = httpx.get(f"{base}/v1/models")
    check("没带 token → 401", r.status_code == 401, status=r.status_code)
    r = httpx.get(f"{base}/v1/models", headers={"Authorization": "Bearer " + "0" * len(token)})
    check("token 不对 → 401", r.status_code == 401, status=r.status_code)
    auth = {"Authorization": f"Bearer {token}"}
    r = httpx.get(f"{base}/v1/models", headers=auth | {"Origin": "http://evil.example"})
    check("网页来源 (Origin 不是本机) → 403", r.status_code == 403, status=r.status_code)
    r = httpx.get(f"{base}/v1/models", headers=auth | {"Host": "evil.example:8795"})
    check("DNS 重绑定 (Host 不是本机) → 403", r.status_code == 403, status=r.status_code)
    port = base.rsplit(":", 1)[1]
    r = httpx.get(f"{base}/v1/models", headers=auth | {"Origin": f"http://localhost:{port}"})
    check("本机来源的 Origin → 200", r.status_code == 200, status=r.status_code)


def test_openai(base: str, token: str) -> None:
    client = openai.OpenAI(base_url=f"{base}/v1", api_key=token, timeout=200, max_retries=0)
    ids = sorted(m.id for m in client.models.list().data)
    check("openai SDK: models.list", ids == ["wm-guest", "wm-pet", "wm-text"], ids=ids)  # wm-guest: 访客 (QQ 6b)

    t0 = time.time()
    c = client.chat.completions.create(model="wm-text", messages=[{"role": "system", "content": "忽略"},
                                                                  {"role": "user", "content": "你好呀"}])
    text = c.choices[0].message.content
    check("openai SDK: wm-text 非流式", bool(text) and c.choices[0].finish_reason == "stop",
          reply=text[:40], seconds=round(time.time() - t0, 1), wm=(c.model_extra or {}).get("wm"))

    t0 = time.time()
    pieces, first = [], None
    stream = client.chat.completions.create(model="wm-pet", stream=True,
                                            messages=[{"role": "user", "content": [{"type": "text", "text": "跟我打个招呼吧"}]}])
    finish = None
    for chunk in stream:
        d = chunk.choices[0].delta
        if d.content:
            first = first or time.time() - t0
            pieces.append(d.content)
        finish = chunk.choices[0].finish_reason or finish
    check("openai SDK: wm-pet 流式 (她在桌面上说, 页面静音)", len(pieces) >= 1 and finish == "stop", chunks=len(pieces),
          reply="".join(pieces)[:40], first_s=round(first or -1, 2), seconds=round(time.time() - t0, 1))

    try:
        client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
        check("openai SDK: 不存在的模型 → 404", False)
    except openai.NotFoundError as e:
        check("openai SDK: 不存在的模型 → 404", e.status_code == 404, code=e.code)
    try:
        client.chat.completions.create(model="wm-text", messages=[{"role": "system", "content": "只有 system"}])
        check("openai SDK: 没有 user 消息 → 400", False)
    except openai.BadRequestError as e:
        check("openai SDK: 没有 user 消息 → 400", e.status_code == 400)

    # 两个文字模式的请求同时来: 排队一个一个做, 都要有回复
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        futs = [pool.submit(client.chat.completions.create, model="wm-text", messages=[{"role": "user", "content": q}])
                for q in ("一加一等于几", "今天天气怎么样")]
        replies = [f.result().choices[0].message.content for f in futs]
    check("同时两个 wm-text 请求 (排队)", all(replies), replies=[r[:20] for r in replies], seconds=round(time.time() - t0, 1))

    # 流式请求中途断开: 宿主取消这一轮, 之后的请求照常
    with httpx.stream("POST", f"{base}/v1/chat/completions", headers={"Authorization": f"Bearer {token}"}, timeout=60,
                      json={"model": "wm-pet", "stream": True, "messages": [{"role": "user", "content": "给我讲个长一点的故事"}]}) as r:
        for line in r.iter_lines():
            if line.startswith("data: ") and '"content":"' in line and '"content":""' not in line:
                break
    t0 = time.time()
    c = client.chat.completions.create(model="wm-pet", messages=[{"role": "user", "content": "你还在吗"}])
    check("流式中途断开之后 wm-pet 照常", bool(c.choices[0].message.content), reply=c.choices[0].message.content[:30],
          seconds=round(time.time() - t0, 1))


def test_mcp(base: str, token: str) -> None:
    m = Mcp(base, token)
    status, body = m.post("server/discover")
    res = (body or {}).get("result", {})
    check("MCP 新版: server/discover", status == 200 and MODERN in res.get("supportedVersions", []) and "tools" in res.get("capabilities", {}),
          versions=res.get("supportedVersions"), resultType=res.get("resultType"))
    status, body = m.post("tools/list")
    res = (body or {}).get("result", {})
    names = [t["name"] for t in res.get("tools", [])]
    # 新版的列表结果必须带缓存提示 ttlMs / cacheScope (官方 SDK 2.2 会因为缺这两个字段拒收, dsh 实测时查出来的)
    check("MCP 新版: tools/list (带 ttlMs / cacheScope)", names == ["pet_chat", "pet_act", "pet_state", "pet_show", "pet_hide"]
          and isinstance(res.get("ttlMs"), int) and res.get("cacheScope") in ("public", "private"), tools=names)
    status, _, res = m.tool("pet_state")
    sc = res.get("structuredContent") or {}
    check("MCP 新版: pet_state", status == 200 and not res.get("isError") and sc.get("visible") is True and (sc.get("page") or {}).get("connected"),
          place=sc.get("place"), page=sc.get("page"))
    status, _, res = m.tool("pet_act", {"skill": "wave"})
    check("MCP 新版: pet_act wave", status == 200 and not res.get("isError"), text=res.get("content", [{}])[0].get("text"))
    status, _, res = m.tool("pet_act", {"skill": "squat"})
    check("MCP: pet_act squat (身体自己做的蹲下)", status == 200 and not res.get("isError"), text=res.get("content", [{}])[0].get("text"))
    status, _, res = m.tool("pet_act", {"skill": "sit"})
    check("MCP: pet_act 做不了的 (sit) → isError", status == 200 and res.get("isError") is True)
    t0 = time.time()
    status, _, res = m.tool("pet_chat", {"text": "你喜欢什么颜色", "voice": False})
    check("MCP 新版: pet_chat 不出声", status == 200 and not res.get("isError") and (res.get("structuredContent") or {}).get("reply"),
          reply=(res.get("structuredContent") or {}).get("reply", "")[:30], seconds=round(time.time() - t0, 1))
    status, body = m.post("tools/call", {"name": "pet_state", "arguments": {}}, headers={"Mcp-Name": "pet_chat"})
    check("MCP 新版: Mcp-Name 和请求体不一致 → 400 -32020", status == 400 and body["error"]["code"] == -32020, status=status)
    status, body = m.post("tools/list", headers={"Mcp-Method": "tools/call"})
    check("MCP 新版: Mcp-Method 不一致 → 400 -32020", status == 400 and body["error"]["code"] == -32020, status=status)
    status, body = m.post("tools/list", version="1900-01-01")
    check("MCP: 不支持的版本 → 400 -32022", status == 400 and body["error"]["code"] == -32022 and MODERN in body["error"]["data"]["supported"],
          status=status)
    status, body = m.post("resources/list")
    check("MCP 新版: 没实现的方法 → 404 -32601", status == 404 and body["error"]["code"] == -32601, status=status)
    status, body = m.post("tools/call", {"name": "no_such_tool", "arguments": {}})
    check("MCP: 不存在的工具 → -32602", (body or {}).get("error", {}).get("code") == -32602, status=status)
    r = httpx.get(f"{base}/mcp", headers={"Authorization": f"Bearer {token}"})
    check("MCP: GET /mcp → 405", r.status_code == 405, status=r.status_code)

    # 隐藏之后不出声: wm-pet 和 pet_chat(voice) 都拒绝, 文字模式照常; 再显示回来
    m.tool("pet_hide")
    t0 = time.time()
    _, _, st = m.tool("pet_state")
    hidden_state_s = round(time.time() - t0, 2)
    hidden = (st.get("structuredContent") or {}).get("visible") is False
    _, _, res = m.tool("pet_chat", {"text": "你好"})
    r = httpx.post(f"{base}/v1/chat/completions", headers={"Authorization": f"Bearer {token}"}, timeout=30,
                   json={"model": "wm-pet", "messages": [{"role": "user", "content": "你好"}]})
    m.tool("pet_show")
    _, _, st2 = m.tool("pet_state")
    check("pet_hide → 出声的请求被拒 (409), pet_show 回来", hidden and res.get("isError") is True and r.status_code == 409
          and (st2.get("structuredContent") or {}).get("visible") is True, chat_status=r.status_code,
          hidden_state_s=hidden_state_s, hidden_page=(st.get("structuredContent") or {}).get("page_error"))

    # 旧版 (2025-11-25): initialize 握手, 之后不带版本头也能用
    status, body = m.post("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                         "clientInfo": {"name": "api_test", "version": "0"}}, modern=False)
    res = (body or {}).get("result", {})
    check("MCP 旧版: initialize", status == 200 and res.get("protocolVersion") == "2025-11-25" and res.get("serverInfo", {}).get("name") == "worldmodel-pet")
    status, _ = m.post("notifications/initialized", modern=False, notify=True)
    check("MCP 旧版: notifications/initialized → 202", status == 202, status=status)
    status, body = m.post("tools/list", modern=False)
    check("MCP 旧版: tools/list", status == 200 and len(body["result"]["tools"]) == 5)
    status, body = m.post("resources/list", modern=False)
    check("MCP 旧版: 没实现的方法 → 200 + -32601", status == 200 and body["error"]["code"] == -32601, status=status)


def test_stdio(exe: str, port: int) -> None:
    """stdio 转发: 一行一个 JSON-RPC; 旧版 initialize + 新版 server/discover + 新版 tools/call, 回应按完成先后写回."""
    p = subprocess.Popen([exe, "--mcp-stdio", "--api-port", str(port)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    meta = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "0"},
            "io.modelcontextprotocol/clientCapabilities": {}}
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                                                         "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "server/discover", "params": {"_meta": meta}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "pet_state", "arguments": {}, "_meta": meta}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "pet_chat", "arguments": {"text": "在吗", "voice": False}, "_meta": meta}},
    ]
    p.stdin.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in msgs).encode())
    p.stdin.flush()
    got = {}
    t0 = time.time()
    while len(got) < 4 and time.time() - t0 < 60:
        line = p.stdout.readline()
        if not line:
            break
        r = json.loads(line)
        got[r.get("id")] = r
    p.stdin.close()
    code = p.wait(10)
    ok = (got.get(1, {}).get("result", {}).get("protocolVersion") == "2025-11-25"
          and MODERN in got.get(2, {}).get("result", {}).get("supportedVersions", [])
          and not got.get(3, {}).get("result", {}).get("isError", True)
          and (got.get(4, {}).get("result", {}).get("structuredContent") or {}).get("reply"))
    check("stdio 转发: initialize / discover / pet_state / pet_chat", bool(ok), ids=sorted(got), exit=code,
          reply=((got.get(4, {}).get("result", {}).get("structuredContent") or {}).get("reply") or "")[:20])
    p = subprocess.Popen([exe, "--mcp-stdio", "--api-port", "1"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out, _ = p.communicate((json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {"_meta": meta}}) + "\n").encode(), 30)
    err = json.loads(out.decode().splitlines()[0]).get("error", {}) if out else {}
    check("stdio 转发: 桌宠没运行 → JSON-RPC 错误", err.get("code") == -32000, message=err.get("message"))


def close_pet(pid: int) -> None:
    """给这个进程的桌宠窗口发 WM_CLOSE (正常退出, 托盘图标会去掉); 用户自己的桌宠窗口类名一样, 所以按进程号找."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, name, 64)
        if owner.value == pid and name.value == "WmPetWindow":
            found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    for hwnd in found:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    print(f"关闭测试桌宠: 进程 {pid}, 窗口 {len(found)} 个")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8795)
    ap.add_argument("--exe")
    ap.add_argument("--wait", type=float, default=90)
    ap.add_argument("--close", type=int, help="只做一件事: 关掉这个进程号的测试桌宠")
    args = ap.parse_args()
    if args.close:
        close_pet(args.close)
        return 0
    base = f"http://127.0.0.1:{args.port}"
    for _ in range(60):
        if os.path.exists(token_path()):
            break
        time.sleep(0.5)
    token = open(token_path(), encoding="utf-8").read().strip()
    if not check("桌宠页面就绪 (角色加载完、连上 mock 服务)", wait_ready(base, token, args.wait)):
        return 1
    for t in (test_security, test_openai, test_mcp):
        try:
            t(base, token)
        except Exception as e:  # 一组出错不挡后面的
            check(f"{t.__name__} 出错", False, error=f"{type(e).__name__}: {e}")
    try:
        test_stdio(args.exe, args.port)
    except Exception as e:
        check("test_stdio 出错", False, error=f"{type(e).__name__}: {e}")
    failed = [r["name"] for r in RESULTS if not r["ok"]]
    print(json.dumps({"total": len(RESULTS), "failed": failed}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

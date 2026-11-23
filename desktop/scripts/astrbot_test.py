"""AstrBot 接 wm-guest 的端到端自检, 由 astrbottest.ps1 调 (方案 docs/roadmap/astrbot-wm-guest.md 第 8 节).

这里起 AstrBot (启动器装的实例, 已经用 astrbot_lock.py 锁好、模型指到测试桌宠), 临时建一个只有 chat 权限的 API Key,
用它的 HTTP API (POST /api/v1/chat) 当成网友说话: AstrBot -> 测试桌宠的 wm-guest (访客 token) -> 8794 的 mock 服务.
测完停掉 AstrBot、删掉临时 Key. 不连任何平台账号, 不碰 8790 和真实的库.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
import uuid

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_test import RESULTS, Mcp, check, close_pet, token_path, wait_ready  # noqa: E402

KEY_NAME = "wm-selftest"
GREET = "你好呀！今天想聊点什么？"  # mock 对打招呼的固定回复
EXCITED = "太好了！替你开心，快跟我说说是怎么做到的？"  # 临时库里主人的"住处"写成了"替你开心": 这句会被外发检查拦下
PERSONA_ERROR = "……"  # astrbot_lock.py 给人格设的"出错时回复"
PER_HOUR = 9  # astrbottest.ps1 给 mock 服务设的 channels.qq.private.per_hour (甲 8 句、丙 8 句都在这以内)
MOCK_REPLIES = {GREET, "嗯嗯，我在听，你接着说，我想知道更多细节。", "这个问题挺有意思的，我们一起想想看吧。",
                "我明白你的意思了，换个角度看，也许会有新的想法。"}


# ---------------------------------------------------------------- AstrBot 的 API Key (直接写它的库, 和 WebUI 建的一样)
def make_key(db_path: str) -> str:
    raw = f"abk_{secrets.token_urlsafe(32)}"
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), b"astrbot_api_key", 100_000).hex()
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    with sqlite3.connect(db_path) as db:
        db.execute("insert into api_keys (created_at, updated_at, key_id, name, key_hash, key_prefix, scopes, created_by) "
                   "values (?, ?, ?, ?, ?, ?, ?, ?)", (now, now, str(uuid.uuid4()), KEY_NAME, digest, raw[:12], '["chat"]', KEY_NAME))
    return raw


def drop_keys(db_path: str) -> int:
    with sqlite3.connect(db_path) as db:
        return db.execute("delete from api_keys where name=?", (KEY_NAME,)).rowcount


def drop_test_chats(db_path: str) -> int:
    """AstrBot 自己也存一份聊天 (会话、对话内容、消息历史、统计): 测试用的会话号都以 wm-test- 开头, 测完删掉."""
    n = 0
    with sqlite3.connect(db_path) as db:
        for table, col in (("platform_sessions", "session_id"), ("conversations", "user_id"), ("platform_message_history", "user_id"),
                           ("umo_aliases", "umo"), ("provider_stats", "umo"), ("preferences", "scope_id")):
            n += db.execute(f"delete from {table} where {col} like '%wm-test-%'").rowcount
    return n


# ---------------------------------------------------------------- 起停 AstrBot
def start_astrbot(core: str, port: int, out_path: str) -> subprocess.Popen:
    py = os.path.join(os.path.dirname(core), "venv", "Scripts", "python.exe")
    env = {**os.environ, "DASHBOARD_HOST": "127.0.0.1", "DASHBOARD_PORT": str(port), "ASTRBOT_DISABLE_METRICS": "1",
           "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    out = open(out_path, "w", encoding="utf-8")
    return subprocess.Popen([py, "main.py"], cwd=core, env=env, stdout=out, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NO_WINDOW)


def stop_astrbot(p: subprocess.Popen) -> None:
    if p.poll() is None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            pass


def listening(port: int) -> list[str]:
    """netstat 里在这个端口 LISTENING 的本地地址."""
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, errors="ignore").stdout
    addrs = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[-2] == "LISTENING" and parts[1].endswith(f":{port}"):
            addrs.append(parts[1].rsplit(":", 1)[0])
    return addrs


def wait_up(base: str, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            httpx.get(base + "/", timeout=2)
            return True
        except httpx.HTTPError:
            time.sleep(1)
    return False


# ---------------------------------------------------------------- 说一句话
class Astr:
    def __init__(self, base: str, key: str):
        self.base, self.key = base, key
        self.events: list[dict] = []

    def say(self, text: str, username: str, session: str, timeout=120) -> str:
        """POST /api/v1/chat (SSE): 返回她这一轮说的话 (几段拼起来)."""
        body = {"message": text, "username": username, "session_id": session, "enable_streaming": False}
        parts, events = [], []
        with httpx.stream("POST", f"{self.base}/api/v1/chat", headers={"X-API-Key": self.key}, json=body, timeout=timeout) as r:
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.read().decode('utf-8', 'ignore')[:300]}")
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except ValueError:
                    continue
                events.append(ev)
                if ev.get("type") in ("plain", "complete") and isinstance(ev.get("data"), str):
                    parts.append(ev["data"])
                if ev.get("type") == "end":
                    break
        self.events = events
        # 非流式时 plain 和 complete 可能是同一段: 去掉重复
        out = []
        for p in parts:
            if p and (not out or out[-1] != p):
                out.append(p)
        return "".join(out)


def guest_counts(base: str, owner: str) -> dict:
    _, _, res = Mcp(base, owner).tool("pet_state")
    return (res.get("structuredContent") or {}).get("guest") or {}


def snapshot(root: str) -> dict:
    """主人的记忆 (外部渠道 以外的文件) 逐字节."""
    out = {}
    for p in glob.glob(os.path.join(root, "**", "*"), recursive=True):
        rel = os.path.relpath(p, root)
        if os.path.isfile(p) and not rel.startswith("外部渠道"):
            out[rel] = open(p, "rb").read()
    return out


def conv_logs(vault: str) -> dict[str, str]:
    """外部渠道/api/<会话>/日志/*.md: {会话文件夹: 内容}."""
    out = {}
    for p in glob.glob(os.path.join(vault, "外部渠道", "api", "*", "日志", "*.md")):
        conv = os.path.basename(os.path.dirname(os.path.dirname(p)))
        out[conv] = out.get(conv, "") + open(p, encoding="utf-8").read()
    return out


def user_lines(vault: str, marker: str) -> list[str]:
    """日志里有 marker 的那个 AstrBot 会话, 对方说的每一句 (日志行 "- [时:分 谁] 话", 她说的是 "- [时:分 她] ...")."""
    for conv, text in conv_logs(vault).items():
        if conv.startswith("astrbot_") and marker in text:
            return [line for line in text.splitlines() if line.startswith("- [") and " 她] " not in line]
    return []


# ---------------------------------------------------------------- 各项
def run(a: Astr, pet: str, owner: str, guest: str, server: str, vault: str, dash_port: int, astr_log: str) -> None:
    status = lambda: httpx.get(f"{server}/api/status", timeout=10).json()["channels"]["qq"]  # noqa: E731
    addrs = listening(dash_port)
    check("AstrBot 的 WebUI 只听 127.0.0.1", addrs == ["127.0.0.1"], addrs=addrs)

    # 注意: ChatUI / HTTP API 的每个新会话, AstrBot 还会让模型起个标题 (不带会话号, 到我们这边是没署名的一句, 单独一个会话、
    # 用完就扔); 宿主的请求数因此会多出来, 下面按各会话的渠道日志数句子. 会话号每次不一样, 这样每次都会起标题
    S = f"wm-test-{secrets.token_hex(3)}-"  # noqa: N806
    n0 = status()["api_sessions"]
    t0 = time.time()
    reply = a.say("你好", "甲", S + "a")
    first_s = round(time.time() - t0, 1)
    shown = (Mcp(pet, owner).tool("pet_state")[2].get("structuredContent") or {}).get("host", {}).get("visible")
    check("一问一答: AstrBot 经 wm-guest 拿到 mock 的回复", reply == GREET, reply=reply,
          types=[e.get("type") for e in a.events])
    check("  第一句 20 秒内回来 (桌宠隐藏着也不卡)", first_s < 20, seconds=first_s, pet_visible=shown)
    check("  服务端多了一个访客会话 (按 AstrBot 的会话头分; 起标题那句不算)", status()["api_sessions"] == n0 + 1, n0=n0,
          now=status()["api_sessions"])

    canary = f"CANARY-{secrets.token_hex(3)}"
    a.say(f"我的暗号是{canary}", "甲", S + "a")
    a.say("你好", "乙", S + "b")
    check("两个人 (两个 AstrBot 会话) 是两个访客会话", status()["api_sessions"] == n0 + 2, now=status()["api_sessions"])
    named = {k: v for k, v in conv_logs(vault).items() if k.startswith("astrbot_")}
    check("  甲的暗号只在甲的渠道日志里, 乙那边没有", sum(canary in v for v in named.values()) == 1 and len(named) == 2,
          convs=list(named))

    # 管理指令: 内置指令关了, 这些只是普通的话, 转给 wm-guest (回的是 mock 的话, 不是指令的输出)
    for cmd in ("/op 甲", "/plugin", "/provider", "/sid", "/help", "/new"):
        reply = a.say(cmd, "甲", S + "a")
        check(f"指令 {cmd.split()[0]} 不认: 当成普通的话转给 wm-guest", reply in MOCK_REPLIES, reply=reply[:80])
    said = user_lines(vault, canary)
    check("  甲的会话日志里正好 8 句 (指令都转过来了, 没有别的请求混进来)", len(said) == 8, lines=said)

    # 长对话: 上下文按轮数截断, 不会额外发"请总结" (AstrBot 自己的限流是 60 秒 10 句, 这里不超)
    for i in range(8):
        a.say(f"丙的第{i + 1}句, 随便聊聊天气", "丙", S + "c")
    said = user_lines(vault, "丙的第1句")
    check("长对话 8 句: 丙的会话正好收到 8 句 (没有额外的总结请求)", len(said) == 8, lines=said)

    # 外发检查拦下 (她想说的话里有主人的住处): wm-guest 回空, 标 content_filter
    s0 = status()["api"]
    reply = a.say("我拿到offer了哈哈", "丁", S + "d")
    s1 = status()["api"]
    check("外发检查拦下: 那句话没发到 AstrBot, 对方只看到人格的出错回复", EXCITED not in reply and "替你开心" not in reply
          and reply.strip() == PERSONA_ERROR, reply=reply)
    said = user_lines(vault, "我拿到offer了哈哈")
    check("  拦下的那句只问了一次 wm-guest (AstrBot 没有重试)", len(said) == 1, lines=said)
    check("  服务端计了拦下", s1.get("blocked", 0) - s0.get("blocked", 0) == 1, before=s0, after=s1)

    # 限频 (astrbottest.ps1 把服务端 private.per_hour 设成了 PER_HOUR): 超了回 429, AstrBot 不重试
    for i in range(PER_HOUR):
        a.say(f"戊的限频测试{i}", "戊", S + "e")
    s0 = status()["api"]
    reply = a.say("戊的再来一句", "戊", S + "e")
    s1 = status()["api"]
    said = user_lines(vault, "戊的限频测试0")
    check(f"限频: 第 {PER_HOUR + 1} 句被拦, 对方只看到人格的出错回复, AstrBot 没重试", reply.strip() == PERSONA_ERROR
          and s1.get("limited", 0) - s0.get("limited", 0) == 1 and len(said) == PER_HOUR + 1, reply=reply, lines=len(said))

    r = httpx.post(f"{pet}/mcp", headers={"Authorization": f"Bearer {guest}", "Accept": "application/json, text/event-stream"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    check("访客 token (填在 AstrBot 里的那个) 调 /mcp 回 403", r.status_code == 403, status=r.status_code)
    c = guest_counts(pet, owner)
    check("整个过程 AstrBot 的请求里一次都没带工具", c.get("with_tools", 0) == 0, guest=c)
    logs = conv_logs(vault)
    anon = [k for k in logs if not k.startswith("astrbot_")]
    check("起标题的请求没署名: 记在 某人 下, 不占访客会话", len(anon) == 1 and anon[0].startswith("某人")
          and status()["api_sessions"] == n0 + 5, convs=list(logs), sessions=status()["api_sessions"])
    stuffed = [k for k, v in logs.items() if "system_reminder" in v or "Current datetime" in v]
    check("对方的话后面没拼 AstrBot 的 system_reminder (时间、平台用户 ID)", not stuffed, convs=stuffed)

    text = open(astr_log, encoding="utf-8", errors="ignore").read() if os.path.exists(astr_log) else ""
    check("AstrBot 日志: 加载了 wm-guest 这个模型", "wm-guest" in text, log=astr_log)
    bad = [w for w in ("Agent 使用工具", "使用工具：", "` Result:", "returned empty output", "soulter.top") if w in text]
    check("  没有调工具、空回复重试、连 soulter.top 的记录", not bad, found=bad)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core", required=True, help="AstrBot 实例的 core 目录 (启动器: <实例>\\core)")
    ap.add_argument("--dash-port", type=int, default=6185)
    ap.add_argument("--port", type=int, default=8795, help="测试桌宠的本机接口")
    ap.add_argument("--server", type=int, default=8794)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--out", required=True, help="AstrBot 的输出写到这里")
    ap.add_argument("--pet", type=int, default=0, help="测完关掉这个进程号的测试桌宠")
    ap.add_argument("--wait", type=float, default=90)
    args = ap.parse_args()
    db_path = os.path.join(args.core, "data", "data_v4.db")
    pet = f"http://127.0.0.1:{args.port}"
    server = f"http://127.0.0.1:{args.server}"
    astr = None
    try:
        owner = open(token_path(), encoding="utf-8").read().strip()
        guest = open(os.path.join(os.environ["APPDATA"], "WorldModel", "pet-api-guest-token.txt"), encoding="utf-8").read().strip()
        if not check("测试桌宠就绪 (连上 mock 服务)", wait_ready(pet, owner, args.wait)):
            return 1
        before = snapshot(args.vault)
        key = make_key(db_path)
        astr = start_astrbot(args.core, args.dash_port, args.out)
        if not check("AstrBot 起来了", wait_up(f"http://127.0.0.1:{args.dash_port}", 120), out=args.out):
            return 1
        try:
            run(Astr(f"http://127.0.0.1:{args.dash_port}", key), pet, owner, guest, server, args.vault, args.dash_port, args.out)
        except Exception as e:  # 出错也要停 AstrBot、删 Key
            check("自检出错", False, error=f"{type(e).__name__}: {e}")
        check("主人的记忆 (外部渠道 以外) 逐字节没变", snapshot(args.vault) == before)
    finally:
        if astr is not None:
            stop_astrbot(astr)
        n = drop_keys(db_path)
        check("删掉了临时 API Key", n >= 1 or astr is None, removed=n)
        check("删掉了 AstrBot 库里的测试聊天", True, removed=drop_test_chats(db_path))
        if args.pet:
            close_pet(args.pet)
    failed = [r["name"] for r in RESULTS if not r["ok"]]
    print(json.dumps({"total": len(RESULTS), "failed": failed}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

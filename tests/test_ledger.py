"""账本 (wm/ledger.py): 峰谷 (北京时间、周末、节假日、换档时间)、价格和命中率、按天汇总、DeepSeek 余额 (假的接口, 不连外网)、
给她看的那一行、云端模型和本机模型自动记账、/api/billing, 以及桌宠云端那一轮的提示词里带上 [钱包]."""
import asyncio
import json
from datetime import date, datetime, timedelta

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from test_cloud import KEY, FakeCloud, collect, make_cfg, profile, recv, say, spec
from wm import cloud as C
from wm import ledger as L
from wm.app import create_app
from wm.config import load_config

CONF = load_config()["billing"]


def bj(*args) -> float:
    return datetime(*args, tzinfo=L.BJ).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, t=None, **conf):
    return L.Ledger(tmp_path / "billing", {**CONF, **conf}, clock=Clock(t if t is not None else bj(2026, 10, 26, 10, 0)))


# ---------------------------------------------------------------------------
# 峰谷
# ---------------------------------------------------------------------------
def test_peak_hours_follow_deepseek_rules():
    pc = L.PeakClock(CONF["peak"]["deepseek"])
    assert pc.tier(bj(2026, 10, 26, 9, 0)) == "peak"       # 周一 9:00 开始高峰
    assert pc.tier(bj(2026, 10, 26, 8, 59)) == "off"
    assert pc.tier(bj(2026, 10, 26, 11, 59)) == "peak"
    assert pc.tier(bj(2026, 10, 26, 12, 0)) == "off"       # 午间空闲
    assert pc.tier(bj(2026, 10, 26, 14, 0)) == "peak"
    assert pc.tier(bj(2026, 10, 26, 18, 0)) == "off"
    assert pc.tier(bj(2026, 10, 31, 10, 0)) == "off"       # 周六全天空闲
    assert pc.tier(bj(2027, 1, 1, 10, 0)) == "off"         # 元旦 (周五) 全天空闲
    assert pc.segments(bj(2026, 10, 26, 3, 0)) == [[540, 720], [840, 1080]] and pc.segments(bj(2026, 11, 1, 12, 0)) == []


def test_next_change_skips_weekend_and_holiday():
    pc = L.PeakClock(CONF["peak"]["deepseek"])
    assert pc.next_change(bj(2026, 10, 26, 10, 0)) == (bj(2026, 10, 26, 12, 0), "off")
    assert pc.next_change(bj(2026, 10, 26, 12, 30)) == (bj(2026, 10, 26, 14, 0), "peak")
    assert pc.next_change(bj(2026, 10, 30, 19, 0)) == (bj(2026, 11, 2, 9, 0), "peak")    # 周五晚 -> 周一早上
    assert pc.next_change(bj(2026, 12, 31, 19, 0)) == (bj(2027, 1, 4, 9, 0), "peak")     # 元旦 + 周末
    # 不管这台电脑在哪个时区, 都按北京时间
    t = datetime(2026, 10, 26, 2, 0, tzinfo=L.timezone.utc).timestamp()  # 北京 10:00
    assert pc.tier(t) == "peak"


# ---------------------------------------------------------------------------
# 价格、命中率、汇总
# ---------------------------------------------------------------------------
def test_normalize_usage_variants():
    assert L.normalize({"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 60,
                        "prompt_cache_miss_tokens": 40, "reasoning_tokens": 5}) == {"hit": 60, "miss": 40, "out": 20, "think": 5}
    assert L.normalize({"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 30}) == \
        {"hit": 30, "miss": 70, "out": 20, "think": 0}                      # 只报了命中多少
    assert L.normalize({"cache_n": 900, "prompt_n": 12, "predicted_n": 33}) == {"hit": 900, "miss": 12, "out": 33, "think": 0}
    assert L.normalize(None) == {"hit": 0, "miss": 0, "out": 0, "think": 0}


def test_cost_peak_offpeak_and_models(tmp_path):
    led = make(tmp_path)
    u = {"hit": 1_000_000, "miss": 1_000_000, "out": 1_000_000}
    peak = led.record("pet", "deepseek", "deepseek-flash", u, t=bj(2026, 10, 26, 10, 0))
    off = led.record("pet", "deepseek", "deepseek-flash", u, t=bj(2026, 10, 26, 13, 0))
    pro = led.record("channels", "deepseek", "deepseek-v4-pro", u, t=bj(2026, 10, 26, 10, 0))
    other = led.record("pet", "kimi", "kimi-k3", u, t=bj(2026, 10, 26, 10, 0))
    local = led.record("pet", "local", "llama.cpp", {"cache_n": 800, "prompt_n": 200, "predicted_n": 50})
    assert peak["tier"] == "peak" and peak["cost"] == pytest.approx(0.04 + 2 + 8)
    assert off["tier"] == "off" and off["cost"] == pytest.approx((0.04 + 2 + 8) / 2)
    assert peak["saved"] == pytest.approx(2 - 0.04) and off["saved"] == pytest.approx((2 - 0.04) / 2)
    assert pro["cost"] == pytest.approx(0.30 + 9 + 27)
    assert other["cost"] is None and other["tier"] is None       # 价格表里没有: 只记 token
    assert local["cost"] == 0.0
    assert led.record("pet", "deepseek", "deepseek-flash", {}) is None  # 全是 0 不记


def test_summary_rates_by_who_month_and_days(tmp_path):
    t = bj(2026, 10, 26, 10, 0)
    led = make(tmp_path, t)
    yesterday = t - 86400
    led.record("workbench", "deepseek", "deepseek-flash", {"hit": 100, "miss": 900, "out": 50}, t=yesterday)
    led.record("pet", "deepseek", "deepseek-flash", {"hit": 900, "miss": 100, "out": 20}, t=t - 60)
    led.record("channels", "deepseek", "deepseek-flash", {"hit": 700, "miss": 300, "out": 30}, t=t - 30)
    led.record("pet", "kimi", "kimi-k3", {"hit": 0, "miss": 500, "out": 10}, t=t - 20)
    led.record("pet", "local", "llama.cpp", {"cache_n": 950, "prompt_n": 50, "predicted_n": 40}, t=t - 10)
    with open(tmp_path / "billing" / f"{date.fromtimestamp(t).isoformat()}.jsonl", "a", encoding="utf-8") as f:
        f.write("这一行坏了\n")
    s = led.summary()
    today = s["today"]
    assert today["turns"] == 3 and today["hit"] == 1600 and today["miss"] == 900 and today["unpriced"] == 1
    assert today["hit_rate"] == pytest.approx(1600 / 2500) and today["miss_rate"] == pytest.approx(900 / 2500)
    assert set(today["by_who"]) == {"pet", "channels"} and today["by_who"]["pet"]["turns"] == 2
    assert today["local"] == {"turns": 1, "hit": 950, "miss": 50, "out": 40, "hit_rate": pytest.approx(0.95)}
    assert today["cost"] > 0 and today["saved"] > 0
    assert s["month"]["turns"] == 4 and "workbench" in s["month"]["by_who"]  # 昨天 (10-25) 也在这个月
    assert len(s["days"]) == 14 and s["days"][-1]["day"] == date.fromtimestamp(t).isoformat() and s["days"][-2]["turns"] == 1
    assert [r["who"] for r in s["recent"][:2]] == ["pet", "pet"] and s["recent"][0]["provider"] == "local"  # 新的在前
    assert s["now"]["tier"] == "peak" and s["now"]["next_tier"] == "off" and s["now"]["next_in_s"] == 7200
    assert {p["name"] for p in s["prices"]} == {"deepseek-flash", "deepseek-v4-pro"}
    assert s["prices"][1]["miss"] == [1.0, 2.0]  # [空闲, 高峰]


# ---------------------------------------------------------------------------
# 余额
# ---------------------------------------------------------------------------
class FakeBalance:
    """假的 DeepSeek /user/balance: 按顺序回这些余额 (用完一直回最后一个); status 不是 200 时回错误."""

    def __init__(self, *totals, status=200):
        self.totals, self.status, self.auth = list(totals), status, []
        self.server = None

    async def handle(self, request):
        self.auth.append(request.headers.get("Authorization"))
        if self.status != 200:
            return web.json_response({"error": "nope"}, status=self.status)
        total = self.totals.pop(0) if len(self.totals) > 1 else self.totals[0]
        return web.json_response({"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": f"{total:.2f}", "granted_balance": "6.00", "topped_up_balance": f"{total - 6:.2f}"}]})

    async def start(self):
        app = web.Application()
        app.router.add_get("/user/balance", self.handle)
        app.router.add_post("/v1/chat/completions", self.handle)
        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")

    async def close(self):
        await self.server.close()


def test_balance_uses_live_deepseek_model_key_and_tracks_today(tmp_path):
    async def main():
        fake = FakeBalance(100.0, 99.5, 120.0)
        base = await fake.start()
        led = make(tmp_path, balance_url=base + "/user/balance", env_key="")
        L.use(led)
        try:
            proxy = C.CloudLLM(spec("http://example.invalid/v1", provider="deepseek", model="deepseek-flash"))
            r0 = await led.refresh_balance(force=True)     # 走中转站的 key 不发给 (另一个主机的) 余额地址
            assert "没有 DeepSeek 的 key" in r0["error"] and fake.auth == []
            ds = C.CloudLLM(spec(base + "/v1", provider="deepseek", model="deepseek-flash"))
            b1 = await led.refresh_balance(force=True)
            b2 = await led.refresh_balance(force=True)    # 花了 0.5
            b3 = await led.refresh_balance(force=True)    # 充值 20.5
            await proxy.close()
            await ds.close()
            return b1, b2, b3, fake.auth, json.loads((tmp_path / "billing" / "balance.json").read_text(encoding="utf-8"))
        finally:
            L.use(None)
            await fake.close()

    b1, b2, b3, auth, obs = asyncio.run(main())
    assert b1["total"] == 100.0 and b1["granted"] == 6.0 and b1["source"] == "正在用的 DeepSeek 模型" and not b1["error"]
    assert b2["today_drop"] == pytest.approx(0.5)
    assert b3["total"] == 120.0 and b3["today_topup"] == pytest.approx(20.5) and b3["today_drop"] == pytest.approx(0.5)
    assert auth == [f"Bearer {KEY}"] * 3 and obs["first"] == 100.0 and obs["last"] == 120.0
    assert KEY not in json.dumps([b1, b2, b3, obs])


def test_balance_env_key_errors_and_url_guard(tmp_path, monkeypatch):
    async def main():
        ok, bad = FakeBalance(42.0), FakeBalance(1.0, status=401)
        base_ok, base_bad = await ok.start(), await bad.start()
        try:
            monkeypatch.setenv("WM_TEST_DS_KEY", "sk-env-0123456789")
            a = await make(tmp_path / "a", balance_url=base_ok + "/user/balance", env_key="WM_TEST_DS_KEY").refresh_balance(True)
            b = await make(tmp_path / "b", balance_url=base_bad + "/user/balance", env_key="WM_TEST_DS_KEY").refresh_balance(True)
            c = await make(tmp_path / "c", balance_url="https://evil.example.com/user/balance",
                           env_key="WM_TEST_DS_KEY").refresh_balance(True)
            d = await make(tmp_path / "d", balance_url=base_ok + "/user/balance", env_key="").refresh_balance(True)
            return a, b, c, d, ok.auth, bad.auth
        finally:
            await ok.close()
            await bad.close()

    a, b, c, d, auth_ok, auth_bad = asyncio.run(main())
    assert a["total"] == 42.0 and a["source"] == "环境变量 WM_TEST_DS_KEY" and a["low"] is False
    assert "key 不对" in b["error"] and "total" not in b
    assert "官方" in c["error"]                    # 别的地址: 不发 key
    assert "没有 DeepSeek 的 key" in d["error"]
    assert auth_ok == ["Bearer sk-env-0123456789"] and len(auth_bad) == 1


def test_wallet_line(tmp_path):
    t = bj(2026, 10, 26, 13, 15)  # 周一午间空闲, 45 分钟后进入高峰
    led = make(tmp_path, t)
    assert led.wallet_line() == "[钱包] 你在云端思考、说话花的是主人的钱: 现在是空闲时段 (价钱是高峰的 0.5 倍), 45 分钟后进入高峰."
    led.record("pet", "deepseek", "deepseek-flash", {"hit": 90_000, "miss": 10_000, "out": 2_000}, t=t - 5)
    led.balance = {"total": 3.2, "at": t, "source": "x"}
    line = led.wallet_line()
    # 空闲价: (9 万 × 0.02 + 1 万 × 1 + 2000 × 4) / 100 万 = 0.0198
    assert "DeepSeek 余额 ¥3.20 (不多了)" in line and "今天花了 ¥0.02 (10.2 万 token, 缓存命中 90%)" in line


# ---------------------------------------------------------------------------
# 自动记账: 云端模型、/api/billing、桌宠云端一轮的 [钱包]
# ---------------------------------------------------------------------------
def test_cloud_requests_are_recorded(tmp_path):
    reply = '{"op":"chat","acts":[],"emo":"happy","reply":"好呀。"}'

    async def main():
        fake = FakeCloud(say(reply))
        base = await fake.start()
        led = make(tmp_path)
        L.use(led)
        llm = C.CloudLLM(spec(base, provider="deepseek", model="deepseek-flash"), {"read_timeout_s": 5})
        llm.who = "channels"
        try:
            meta = {}
            "".join([x async for x in llm.stream([{"role": "user", "content": "你好"}], meta=meta)])
            "".join([x async for x in llm.stream([{"role": "user", "content": "再来"}], meta=meta)])  # meta 累加, 账本按次记
            return led.summary()
        finally:
            L.use(None)
            await llm.close()
            await fake.close()

    s = asyncio.run(main())
    # 假服务每次报 prompt 100 / completion 20, 没报命中: 输入全算没命中
    assert s["today"]["turns"] == 2 and s["today"]["miss"] == 200 and s["today"]["out"] == 40
    assert s["today"]["by_who"]["channels"]["turns"] == 2 and s["recent"][0]["model"] == "deepseek-flash"


def test_api_billing_and_wallet_in_cloud_turn(tmp_path):
    reply = '{"op":"chat","acts":[],"emo":"happy","reply":"嗯嗯。"}'

    async def main():
        fake = FakeCloud(say(reply))
        base = await fake.start()
        client = TestClient(TestServer(create_app(make_cfg(tmp_path, "billing.balance_every_s=0"))))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            assert json.loads((await ws.receive()).data)["type"] == "hello"
            await ws.send_json(profile(base))
            await recv(ws, "profile_done")
            await ws.send_json({"type": "text", "text": "今天怎么样"})
            await collect(ws, 1)
            got = await (await client.get("/api/billing")).json()
            again = await (await client.post("/api/billing/refresh")).json()
            return got, again, fake.requests
        finally:
            await client.close()
            await fake.close()

    got, again, reqs = asyncio.run(main())
    last = reqs[0]["body"]["messages"][-1]["content"]
    assert "[钱包] 你在云端思考、说话花的是主人的钱" in last and "时段" in last
    assert got["today"]["turns"] == 1 and got["today"]["by_who"]["pet"]["turns"] == 1  # 自定义接口: 只记 token
    assert got["today"]["unpriced"] == 1 and got["now"]["tier"] in ("peak", "off") and len(got["days"]) == 14
    assert "没有 DeepSeek 的 key" in again["balance"]["error"]
    assert (tmp_path / "billing").is_dir() and KEY not in json.dumps(got)


# ---------------------------------------------------------------------------
# 按渠道的额度 (2026-10-27): 算哪个渠道、80% 提醒、100% 停、存下来、面板接口
# ---------------------------------------------------------------------------
def test_budget_buckets_levels_alerts_and_persist(tmp_path):
    t = bj(2026, 10, 26, 13, 0)
    led = make(tmp_path, t)
    assert led.over_budget("pet") is None and {r["level"] for r in led.budget_state()["rows"]} == {"off"}  # 默认不限
    led.set_budget({"unit": "tokens", "limits": {"qq": 1000, "total": 5000}})
    alerts = []
    led.on_alert = lambda text, row: alerts.append((row["id"], row["level"], text))
    with L.charging("qq"):
        led.record("channels", "deepseek", "deepseek-flash", {"miss": 500, "out": 300}, t=t)  # 800 / 1000: 到 80%
        assert led.over_budget("channels") is None
        led.record("channels", "deepseek", "deepseek-flash", {"miss": 200, "out": 100}, t=t)  # 1100: 到 100%
        why = led.over_budget("channels")
        led.record("channels", "deepseek", "deepseek-flash", {"miss": 10, "out": 10}, t=t)  # 再记也不重复提醒
    assert "今天QQ的云端额度用完了 (1100 / 1000 token)" in why
    assert [(a, b) for a, b, _ in alerts] == [("qq", "warn"), ("qq", "stop")] and "用了 80%" in alerts[0][2]
    with L.charging("bili"):
        assert led.over_budget("channels") is None  # B站 不限, 总额还没到
    led.record("pet", "deepseek", "deepseek-flash", {"miss": 100}, t=t)  # 没标的: 桌宠算桌宠
    led.record("agent", "deepseek", "deepseek-flash", {"miss": 100}, t=t)  # 工作台算其他
    led.record("pet", "local", "qwen", {"miss": 10_000}, t=t)  # 本机模型不算
    rows = {r["id"]: r for r in led.budget_state(t)["rows"]}
    assert rows["qq"]["tokens"] == 1120 and rows["qq"]["level"] == "stop" and rows["pet"]["tokens"] == 100
    assert rows["other"]["tokens"] == 100 and rows["bili"]["level"] == "off" and rows["total"]["tokens"] == 1320
    assert "今天的额度: QQ用完了 (先不用云端)" in led.wallet_line()
    # 存下来: 同一个目录再开一个账本还是这个额度; 交上来的不对就不收
    again = make(tmp_path, t)
    assert again.budget["limits"]["qq"] == 1000 and again.budget["unit"] == "tokens"
    for bad, msg in (({"unit": "kg"}, "单位"), ({"limits": {"qq": -1}}, "负数"), ({"limits": {"wx": 1}}, "没有 wx"),
                     ({"limits": {"pet": "很多"}}, "数字")):
        with pytest.raises(ValueError, match=msg):
            again.set_budget(bad)
    assert again.budget["limits"]["qq"] == 1000  # 不对的没改动


def test_budget_in_yuan_counts_cost(tmp_path):
    t = bj(2026, 10, 26, 10, 0)  # 高峰: flash 没命中 2 元 / 百万 token
    led = make(tmp_path, t)
    led.set_budget({"unit": "yuan", "limits": {"pet": 0.01}})
    led.record("pet", "deepseek", "deepseek-flash", {"miss": 4_000}, t=t)  # ¥0.008: 80%
    assert {r["id"]: r for r in led.budget_state(t)["rows"]}["pet"]["level"] == "warn"
    led.record("pet", "deepseek", "deepseek-flash", {"miss": 1_000}, t=t)  # ¥0.010: 用完
    assert "桌宠的云端额度用完了 (¥0.01 / ¥0.01)" in led.over_budget("pet")


def test_cloud_request_is_not_sent_over_budget(tmp_path):
    async def main():
        fake = FakeCloud(say('{"op":"chat","acts":[],"emo":"happy","reply":"好。"}'))
        base = await fake.start()
        led = make(tmp_path)
        led.set_budget({"unit": "tokens", "limits": {"qq": 50}})
        L.use(led)
        llm = C.CloudLLM(spec(base, provider="deepseek", model="deepseek-flash"), {"read_timeout_s": 5})
        llm.who = "channels"
        try:
            with L.charging("qq"):
                "".join([x async for x in llm.stream([{"role": "user", "content": "你好"}])])  # 120 token: 用完
                with pytest.raises(C.CloudError) as err:
                    "".join([x async for x in llm.stream([{"role": "user", "content": "再来"}])])
            sent_qq = len(fake.requests)
            "".join([x async for x in llm.stream([{"role": "user", "content": "别的渠道"}])])  # 没标: 算其他, 照发
            return err.value, sent_qq, len(fake.requests), led.summary()["today"]["by_ch"]
        finally:
            L.use(None)
            await llm.close()
            await fake.close()

    err, sent_qq, sent, by_ch = asyncio.run(main())
    assert err.kind == "budget" and "QQ的云端额度用完了" in str(err) and sent_qq == 1 and sent == 2
    assert by_ch["qq"]["tokens"] == 120 and by_ch["other"]["tokens"] == 120


def test_api_budget_alert_and_pet_turn_stopped(tmp_path):
    reply = '{"op":"chat","acts":[],"emo":"happy","reply":"嗯。"}'

    async def main():
        fake = FakeCloud(say(reply))
        base = await fake.start()
        client = TestClient(TestServer(create_app(make_cfg(tmp_path, "billing.balance_every_s=0"))))
        await client.start_server()
        try:
            bad = await client.post("/api/billing/budget", json={"limits": {"pet": -5}})
            evil = await client.post("/api/billing/budget", json={"limits": {"pet": 100}},
                                     headers={"Origin": "https://evil.example"})
            ok = await (await client.post("/api/billing/budget", json={"unit": "tokens", "limits": {"pet": 100}})).json()
            ws = await client.ws_connect("/ws")
            assert json.loads((await ws.receive()).data)["type"] == "hello"
            await ws.send_json(profile(base))
            await recv(ws, "profile_done")
            await ws.send_json({"type": "text", "text": "今天怎么样"})
            first = await collect(ws, 1)  # 120 token: 桌宠的额度用完了 -> 提醒一次
            await ws.send_json({"type": "text", "text": "再说一句"})
            second = await collect(ws, 2)
            got = await (await client.get("/api/billing")).json()
            return bad.status, evil.status, ok, first, second, len(fake.requests), got
        finally:
            await client.close()
            await fake.close()

    bad, evil, ok, first, second, sent, got = asyncio.run(main())
    assert bad == 400 and evil == 403 and ok["budget"]["unit"] == "tokens"
    assert {r["id"]: r for r in ok["budget"]["rows"]}["pet"]["limit"] == 100
    notice = [m for m in first if m.get("type") == "channel_notice" and m.get("channel") == "billing"]
    assert len(notice) == 1 and notice[0]["level"] == "stop" and "桌宠的云端额度用完了" in notice[0]["text"]
    err = [m for m in second if m.get("type") == "error"]
    assert err and err[0]["message"].startswith("今天桌宠的云端额度用完了") and err[0]["kind"] == "budget"
    assert sent == 1  # 第二轮没发出去
    assert {r["id"]: r for r in got["budget"]["rows"]}["pet"]["level"] == "stop"
    assert json.loads((tmp_path / "billing" / "budget.json").read_text(encoding="utf-8"))["limits"]["pet"] == 100

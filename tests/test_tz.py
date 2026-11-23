"""她按哪个时区过日子 (wm/tz.py; 2026-10-27 用户: "时间可以在设置里面预设，如果不预设，就根据设备时间或者地理位置时间决定")."""
import asyncio
import json
import os
from datetime import datetime, timezone

import pytest

from wm import tz as TZ


@pytest.fixture(autouse=True)
def _back_to_device():
    yield
    TZ.apply("")  # 别把时区留给后面的测试


def test_parse_and_name():
    assert [TZ.parse(x) for x in ("UTC+8", "+08:00", "utc-5:30", "GMT+0", "")] == [480, 480, -330, 0, None]
    assert [TZ.name(x) for x in (480, -330, 0, 345)] == ["UTC+8", "UTC-5:30", "UTC+0", "UTC+5:45"]
    for bad in ("Asia/Shanghai", "UTC+15", "UTC+8:75", "八点"):
        with pytest.raises(ValueError):
            TZ.parse(bad)


def test_apply_switches_the_process_clock_and_back():
    """设了马上生效 (不用重启): datetime.now 跟着换; 换回"跟着电脑"只拿掉我们自己设的 TZ."""
    utc = datetime.now(timezone.utc)
    TZ.apply("UTC+8")
    assert datetime.now().astimezone().utcoffset().total_seconds() == 8 * 3600
    assert abs((datetime.now() - utc.replace(tzinfo=None)).total_seconds() - 8 * 3600) < 60
    TZ.apply("UTC-5:30")
    assert datetime.now().astimezone().utcoffset().total_seconds() == -5.5 * 3600
    TZ.apply("")
    assert "TZ" not in os.environ and datetime.now().astimezone().utcoffset().total_seconds() == TZ.device()["offset_min"] * 60


def test_clock_file_wins_over_config_and_bad_file_follows_device(tmp_path):
    path = tmp_path / "clock.json"
    assert TZ.Clock(path, "UTC+9").tz == "UTC+9"  # 没存过: config 的
    c = TZ.Clock(path, "UTC+9")
    s = c.set("+08:00")
    assert s["tz"] == "UTC+8" and s["offset_min"] == 480 and json.loads(path.read_text(encoding="utf-8")) == {"tz": "UTC+8"}
    assert TZ.Clock(path, "UTC+9").tz == "UTC+8"  # 存过的优先
    assert c.set("")["tz"] == "" and c.state()["offset_min"] == TZ.device()["offset_min"]
    path.write_text('{"tz": "乱写"}', encoding="utf-8")
    assert TZ.Clock(path, "UTC+9").tz == ""
    with pytest.raises(ValueError):
        c.set("Asia/Shanghai")
    assert [z["value"] for z in c.state()["zones"]][:2] == ["", "UTC+8"]


def test_api_clock(tmp_path):
    """GET 看设的什么、电脑的、她现在几点; POST 改 (只认本机页面), 认不出的 400."""
    from aiohttp.test_utils import TestClient, TestServer

    from test_pipeline import make_cfg
    from wm.app import create_app

    async def main():
        client = TestClient(TestServer(create_app(make_cfg(tmp_path / "logs"))))
        await client.start_server()
        try:
            first = await (await client.get("/api/clock")).json()
            origin = {"Origin": f"http://{client.host}:{client.port}"}
            ok = await client.post("/api/clock", json={"tz": "UTC+8"}, headers=origin)
            bad = await client.post("/api/clock", json={"tz": "Asia/Shanghai"}, headers=origin)
            foreign = await client.post("/api/clock", json={"tz": "UTC+9"}, headers={"Origin": "https://evil.example"})
            return first, ok.status, await ok.json(), bad.status, foreign.status
        finally:
            await client.close()

    first, ok, body, bad, foreign = asyncio.run(main())
    assert first["tz"] == "" and first["offset_min"] == TZ.device()["offset_min"] and first["device"]["name"].startswith("UTC")
    assert ok == 200 and body["tz"] == "UTC+8" and body["offset_min"] == 480
    assert bad == 400 and foreign == 403
    assert json.loads((tmp_path / "logs" / "clock.json").read_text(encoding="utf-8")) == {"tz": "UTC+8"}

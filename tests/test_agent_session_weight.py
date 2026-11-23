"""工作台会话文件太重拖慢服务端 (2026-11-07 会话复查):
- /api/agent 每分钟拉一次, 以前每次把所有没打开的会话文件 (154 MB) 整份读进来解析, 一次约 1 秒、卡住事件循环;
- 玩游戏时截图原图 (data URI) 整张存在 folded_originals 里, 两个会话涨到 64 / 69 MB, 每走一步存两次、每次约 0.5 秒.
"""
import base64
import json
import os
import time

from wm.agent import core as CORE
from wm.agent.core import AgentHub, Session, _offload_images

# 1x1 的 PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
URI = "data:image/png;base64," + base64.b64encode(PNG).decode()
BIG = PNG + os.urandom(30_000)  # 一张 30 KB 的"截图"
BIG_URI = "data:image/png;base64," + base64.b64encode(BIG).decode()


def hub_with(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return AgentHub({"agent": {"workspace": str(ws)}}, home=tmp_path / "agent"), str(ws)


def test_offload_moves_screenshots_out_and_keeps_the_bytes(tmp_path):
    folder = tmp_path / "media"
    text = '{"content": [{"type": "text", "text": "ok"}, {"type": "image_url", "image_url": {"url": "' + URI + '"}}]}'
    out = _offload_images(text, folder)
    assert "data:image/" not in out and "wm-media:" in out and '"type": "text"' in out
    files = list(folder.iterdir())
    assert len(files) == 1 and files[0].read_bytes() == PNG and files[0].suffix == ".png"
    assert _offload_images(text, folder) == out and len(list(folder.iterdir())) == 1  # 同一张只存一份
    broken = _offload_images("看图 data:image/png;base64,iVBORw0KGgoAAAANSUh", folder)  # 被截断的: 只留说明
    assert "data:image/" not in broken and "图片数据不完整" in broken
    assert _offload_images("没有图", folder) == "没有图"


def test_opening_an_old_heavy_session_shrinks_its_file_without_touching_updated(tmp_path):
    hub, ws = hub_with(tmp_path)
    s = Session("20261106-202216-0054ae4c77", ws, title="玩游戏")
    s.updated = 1793900000.0
    s.messages = [{"role": "user", "content": "截个图", "_id": "m1"}, {"role": "tool", "tool_call_id": "c1", "content": "折起来了", "_id": "m2"}]
    s.folded_originals = {"m2": "[mcp_call 的结果 · 只是数据]\n" + json.dumps({"url": BIG_URI}) * 3}
    path = hub.home / "sessions" / f"{s.id}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(s.to_json(), ensure_ascii=False), encoding="utf-8")
    before = path.stat().st_size
    got = hub.get(s.id)
    assert got is not None and "data:image/" not in got.folded_originals["m2"] and "wm-media:" in got.folded_originals["m2"]
    assert path.stat().st_size < before / 3
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["updated"] == 1793900000.0 and "data:image/" not in json.dumps(on_disk)
    assert [p.read_bytes() for p in (hub.home / "media").iterdir()] == [BIG]


def test_session_briefs_are_not_reparsed_when_files_did_not_change(tmp_path, monkeypatch):
    hub, ws = hub_with(tmp_path)
    folder = hub.home / "sessions"
    folder.mkdir(parents=True)
    for i in range(3):
        s = Session(f"20261107-00000{i}-aaaa", ws, title=f"会话{i}")
        (folder / f"{s.id}.json").write_text(json.dumps(s.to_json(), ensure_ascii=False), encoding="utf-8")
    calls = []
    real = CORE.json.loads
    monkeypatch.setattr(CORE.json, "loads", lambda text, *a, **k: calls.append(1) or real(text, *a, **k))
    first = hub._all_briefs()
    assert len(first) == 3 and len(calls) == 3
    second = hub._all_briefs()
    assert second == first and len(calls) == 3  # 文件没变: 一份都不再读
    second[0]["title"] = "改了返回值"
    assert hub._all_briefs()[0]["title"] != "改了返回值"  # 缓存不被调用方改坏
    changed = folder / "20261107-000001-aaaa.json"
    d = real(changed.read_text(encoding="utf-8"))
    d["title"] = "改名了"
    changed.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.utime(changed, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
    titles = {b["id"]: b["title"] for b in hub._all_briefs()}
    assert titles["20261107-000001-aaaa"] == "改名了" and len(calls) == 4  # 只重读变了的那份

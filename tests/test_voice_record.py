"""录自己声音的工具 tools/voice_record.py: 稿子能不能直接录、单句检查、录音页的服务、整理成 GPT-SoVITS 训练集."""
import asyncio
import importlib.util
import io
import json
import re
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("voice_record", ROOT / "tools" / "voice_record.py")
vr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vr)

SR = 48000


def take(speech_s=3.0, lead=0.5, tail=0.5, amp=0.3, noise=1e-4, sr=SR, seed=0):
    """假的一句录音: 前后静音 + 中间一段 180 Hz、每秒起伏 3 次的声音 (像在说话)."""
    rng = np.random.default_rng(seed)
    x = rng.normal(0, noise, int((lead + speech_s + tail) * sr)).astype(np.float32)
    t = np.arange(int(speech_s * sr)) / sr
    voiced = amp * np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
    a = int(lead * sr)
    x[a:a + len(voiced)] += voiced.astype(np.float32)
    return x


def wav_bytes(x, sr=SR):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def test_script_is_ready_to_record():
    rows = vr.load_script()
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)) and all(vr.ID_RE.match(i) for i in ids)
    for s in (1, 2, 3, 4):
        rs = [r for r in rows if r["session"] == s]
        assert 50 <= len(rs) <= 75
        assert sorted(r["order"] for r in rs) == list(range(1, len(rs) + 1))
        assert 3 <= sum(r["cat"] == "参考" for r in rs) <= 4
        assert sum(r["asr"] for r in rs) >= 10          # 考卷平均分在四次里
    zh = [r for r in rows if r["lang"] == "ZH"]
    # GPT-SoVITS 训练时会删掉中文句里的英文字母; 数字也写成汉字, 免得和读法对不上
    assert not [r["id"] for r in zh if re.search(r"[A-Za-z0-9]", r["text"])]
    assert all(r["text"].isascii() for r in rows if r["lang"] == "EN")
    n = [len(vr.HAN.findall(r["text"])) for r in zh]
    assert min(n) >= 10 and max(n) <= 40                # 每句大约 3–9 秒
    assert 3500 <= sum(n) <= 6000                       # 平常语速 13–22 分钟
    refs = [r for r in rows if r["cat"] == "参考"]
    assert {r["emo"] for r in refs} >= {"平时", "开心", "温柔", "惊讶", "委屈", "小生气"}
    assert all(16 <= len(vr.HAN.findall(r["text"])) <= 36 for r in refs)   # 参考音要 3–10 秒
    assert not [r["id"] for r in rows if r["asr"] and r["cat"] in ("数字", "英文", "参考")]


def test_good_take_passes():
    a = vr.analyze(take(), SR, "你回来啦？今天怎么这么晚呀，路上堵车了吗？")
    assert a["level"] == "ok", a
    assert abs(a["lead_s"] - 0.5) < 0.06 and abs(a["tail_s"] - 0.5) < 0.06
    assert abs(a["speech_s"] - 3.0) < 0.1


def test_problems_that_need_a_retake():
    assert any("爆音" in p for p in vr.analyze(np.clip(take() * 5, -1, 1), SR)["problems"])
    assert any("太小" in p for p in vr.analyze(take(amp=0.03), SR)["problems"])   # 最响约 -30 dB
    short = vr.analyze(take(speech_s=1.2), SR, "这个想法很好，我们可以先做个小样试试看。")
    assert any("没读完" in p for p in short["problems"])
    assert vr.analyze(np.zeros(100, np.float32), SR)["level"] == "bad"


def test_reminders_do_not_block():
    a = vr.analyze(take(lead=0.05, noise=0.01), SR, "你回来啦？今天怎么这么晚呀")
    assert a["level"] == "warn", a
    assert any("开头太赶" in s for s in a["notes"]) and any("吵" in s for s in a["notes"])


def test_record_page_saves_checks_and_deletes(tmp_path):
    script = vr.load_script()
    first = script[0]["id"]

    async def main():
        client = TestClient(TestServer(vr.make_app(tmp_path, script)))
        await client.start_server()
        try:
            page = await client.get("/")
            html = await page.text()
            st = await (await client.get("/api/state")).json()
            r = await client.put(f"/api/take/{first}", data=wav_bytes(take()), headers={"Content-Type": "audio/wav"})
            res = await r.json()
            unknown = await client.put("/api/take/Z999", data=wav_bytes(take()))
            junk = await client.put(f"/api/take/{first}", data=b"not a wav")
            other_host = await client.put(f"/api/take/{first}", data=wav_bytes(take()),
                                          headers={"Host": "evil.example"})
            got = await client.get(f"/api/take/{first}")
            body = await got.read()
            st2 = await (await client.get("/api/state")).json()
            gone = await client.delete(f"/api/take/{first}")
            st3 = await (await client.get("/api/state")).json()
            return (page.status, html, st, r.status, res, unknown.status, junk.status, other_host.status,
                    got.status, body, st2, gone.status, st3)
        finally:
            await client.close()

    (page, html, st, put, res, unknown, junk, other_host, got, body, st2, gone, st3) = asyncio.run(main())
    assert page == 200 and "录自己的声音" in html
    assert len(st["script"]) == len(script) and st["takes"] == {}
    assert put == 200 and res["level"] == "ok", res
    assert (unknown, junk, other_host, got, gone) == (404, 400, 403, 200, 200)
    assert body[:4] == b"RIFF" and first in st2["takes"]
    assert first not in st3["takes"] and not (tmp_path / "raw" / f"{first}.wav").exists()
    assert json.loads((tmp_path / "checks.json").read_text(encoding="utf-8")) == {}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="整理训练集要 ffmpeg")
def test_prepare_makes_a_training_list(tmp_path):
    script = vr.load_script()
    ref = next(r for r in script if r["cat"] == "参考")
    plain = next(r for r in script if r["cat"] == "聊天")
    loud = next(r for r in script if r["cat"] == "数字")
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / f"{ref['id']}.wav").write_bytes(wav_bytes(take(speech_s=5.0)))
    (raw / f"{plain['id']}.wav").write_bytes(wav_bytes(take(speech_s=3.0, lead=0.8)))
    (raw / f"{loud['id']}.wav").write_bytes(wav_bytes(np.clip(take() * 5, -1, 1)))   # 爆音: 不收
    res = vr.prepare(tmp_path, script, "tester")
    lines = (tmp_path / "dataset" / "tester.list").read_text(encoding="utf-8").splitlines()
    assert res["lines"] == 2 and len(lines) == 2
    assert [rid for rid, _ in res["skipped"]] == [loud["id"]]
    by = {Path(line.split("|")[0]).stem: line.split("|") for line in lines}
    path, spk, lang, text = by[plain["id"]]
    assert Path(path).is_absolute() and spk == "tester" and lang == "ZH" and text == plain["text"]
    with wave.open(path) as w:
        assert w.getframerate() == 32000 and w.getnchannels() == 1
        dur = w.getnframes() / 32000
    assert abs(dur - (3.0 + 2 * 0.12)) < 0.1            # 首尾静音切到各留 0.12 秒
    assert (tmp_path / "dataset" / "ref" / f"{ref['id']}.wav").exists()
    assert (tmp_path / "dataset" / "ref" / f"{ref['id']}.txt").read_text(encoding="utf-8") == ref["text"]


def test_asr_scoring_helpers():
    assert vr._norm("你好，World！") == "你好world"
    assert vr._edits("今天天气不错", "今天天气不措") == 1
    assert vr._edits("你好吗", "你好") == 1

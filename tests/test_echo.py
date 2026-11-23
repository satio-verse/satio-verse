"""外放回声 (wm/echo.py, wm/loopback.py) 和"没认出字就不算在听" (wm/asr.py、wm/pipeline.py).
都是合成的信号, 不录麦克风、不录系统声音 (conftest 关了真的回环)."""
import asyncio
import json

import numpy as np
import pytest
from scipy.signal import lfilter

from wm import echo as EC
from wm.asr import VadSenseVoiceSession
from wm.config import load_config
from wm.engines import Engines
from wm.loopback import BoxDecimator, RefRing
from wm.pipeline import Session
from wm.trace import LatencyStats, TraceLog

FS = 16000


class FakeHub:
    def __init__(self):
        self.ring = RefRing()
        self.lag_hint = None
        self.coupled_at = -1e9

    def available(self):
        return True

    def touch(self):
        pass

    def status(self):
        return {}


def speechish(n, rng, seed_env=0.0):
    """像说话的信号: 有色噪声 (300 Hz–3 kHz 的共振), 按音节忽大忽小."""
    x = rng.standard_normal(n)
    x = lfilter([1.0], [1.0, -1.6, 0.8], x)
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 4.0 * np.arange(n) / FS + seed_env) ** 2
    return x * env / np.sqrt(np.mean((x * env) ** 2))


def run_guard(hub, guard, mic, ref, rng, t0=100.0, jitter=0.02, ring_late=0.01):
    """模拟真实的先后: 参考 10 ms 一包按时间戳进环 (写进环比到达早 ring_late 秒以前的), 麦克风 40 ms 一帧, 到达有抖动."""
    out = np.zeros(mic.size)
    rp, pk = 0, FS // 100
    for k in range(mic.size // 640):
        arrive = t0 + (k + 1) * 640 / FS + 0.03 + rng.uniform(0, jitter)
        while ref is not None and rp < ref.size and t0 + rp / FS < arrive - ring_late:
            hub.ring.push(ref[rp:rp + pk].astype(np.float32), t0 + rp / FS)
            rp += pk
        frame = np.clip(np.round(mic[k * 640:(k + 1) * 640] * 32768), -32768, 32767).astype(np.int16)
        out[k * 640:(k + 1) * 640] = guard.process(frame, arrive).astype(np.float64) / 32768
    return out


def db(x):
    return 10 * np.log10(np.mean(np.asarray(x, np.float64) ** 2) + 1e-20)


def test_ring_fills_gaps_but_not_jitter():
    r = RefRing(seconds=5)
    pk = np.ones(160, np.float32)
    r.push(pk, 10.0)
    r.push(pk, 10.0 + 0.0102)        # 连着放, 时间戳差一点点: 不插零
    assert r.n == 320
    r.push(pk, 10.5)                  # 设备闲了将近半秒: 中间补静音
    assert r.n == 320 + int(round((10.5 - (10.0102 + 0.01)) * FS)) + 160
    assert not np.any(r.read(320, 100))
    assert np.all(r.read(r.n - 160, 160) == 1)
    assert not np.any(r.read(r.n, 50))           # 还没写到的当静音
    assert r.n_at(10.5) == r.n - 160
    r.advance_to(11.0)                            # 闲着: 时间线补到 11 秒
    assert r.n_at(11.0) == r.n


def test_loopback_formats_and_no_capture_in_tests():
    from wm import loopback as LB
    st = np.array([[0.5, -0.5], [0.25, 0.75]], np.float32)
    assert np.allclose(LB._to_mono(st.tobytes(), 2, 32, True), [0.0, 0.5])
    assert np.allclose(LB._to_mono(np.array([16384, -16384], np.int16).tobytes(), 1, 16, False), [0.5, -0.5])
    raw = bytes([0x00, 0x00, 0x40, 0x00, 0x00, 0xC0])  # 24 位: +0x400000 (0.5), -0x400000 (-0.5)
    assert np.allclose(LB._to_mono(raw, 1, 24, False), [0.5, -0.5])
    hub = LB.LoopbackHub()
    hub.touch()  # 测试里关着 (conftest), 不该真去录
    assert hub.thread is None and not hub.status()["running"]


def test_box_decimator_is_the_same_in_chunks():
    rng = np.random.default_rng(0)
    x = rng.standard_normal(48000 * 2)
    for src in (48000, 44100):
        whole = BoxDecimator(src)(x)
        d = BoxDecimator(src)
        parts = np.concatenate([d(x[i:i + 441]) for i in range(0, x.size, 441)])
        assert abs(parts.size - whole.size) <= 1
        n = min(parts.size, whole.size)
        assert np.allclose(parts[:n], whole[:n], atol=1e-6)
        assert abs(whole.size - x.size * 16000 / src) <= 2
    # 48 kHz → 16 kHz 就是每 3 个平均 (和页面 mic-worklet.js 一样)
    y = BoxDecimator(48000)(x[:30])
    assert np.allclose(y, x[:30].reshape(10, 3).mean(axis=1), atol=1e-6)


def test_gcc_finds_the_delay_and_says_when_there_is_none():
    rng = np.random.default_rng(1)
    W, L = 32000, 16000
    ref = speechish(W + L, rng)
    lag = 1234
    mic = 0.3 * ref[L - lag:L - lag + W] + 0.05 * rng.standard_normal(W)
    got, z = EC.gcc_phat(mic, ref, L)
    assert got == lag and z > 20
    _, z0 = EC.gcc_phat(rng.standard_normal(W), ref, L)
    assert z0 < 9


def test_mdf_cancels_a_linear_echo():
    rng = np.random.default_rng(2)
    n = FS * 6
    far = speechish(n, rng) * 3000
    h = np.zeros(800)
    h[40], h[90], h[300] = 0.6, -0.3, 0.1
    h[40:800] += 0.02 * rng.standard_normal(760) * np.exp(-np.arange(760) / 150)
    echo = np.convolve(far, h)[:n]
    m = EC.MDF(320, 8)
    out = np.concatenate([m.process(echo[i:i + 320], far[i:i + 320])[0] for i in range(0, n - 319, 320)])
    tail = slice(FS * 4, out.size)
    assert db(echo[tail]) - db(out[tail]) > 20
    assert m.adapted and m.erle > 15


def test_guard_passes_audio_untouched_when_nothing_plays():
    rng = np.random.default_rng(3)
    hub = FakeHub()
    g = EC.EchoGuard(hub)
    mic = speechish(FS * 3, rng) * 0.1
    out = run_guard(hub, g, mic, None, rng)
    q = np.clip(np.round(mic * 32768), -32768, 32767) / 32768
    assert np.array_equal(out[g.out_delay:], q[:-g.out_delay])  # 只晚 out_delay 个采样, 一个采样都不改
    assert not g.engaged


def test_guard_with_headphones_leaves_your_voice_alone():
    """在放视频 (参考有声音), 但麦克风里听不到 (戴耳机): 你的声音原样交出去, 也不会判成外放."""
    rng = np.random.default_rng(4)
    hub = FakeHub()
    g = EC.EchoGuard(hub)
    n = FS * 12
    ref = speechish(n, rng) * 0.2
    mic = speechish(n, rng, 1.0) * 0.05 + 1e-4 * rng.standard_normal(n)
    out = run_guard(hub, g, mic, ref, rng)
    q = np.clip(np.round(mic * 32768), -32768, 32767) / 32768
    assert g.engaged and not g.coupled()
    assert np.array_equal(out[g.out_delay:], q[:-g.out_delay])


def test_guard_removes_speaker_echo_and_learns_the_delay():
    rng = np.random.default_rng(5)
    hub = FakeHub()
    g = EC.EchoGuard(hub)
    n = FS * 14
    ref = speechish(n, rng) * 0.2
    h = np.zeros(2400)
    h[32], h[150], h[400] = 0.5, 0.2, -0.1
    h[32:] += 0.03 * rng.standard_normal(2368) * np.exp(-np.arange(2368) / 600)
    delay = 1200  # 75 ms: 喇叭 → 麦克风 → 浏览器
    spk = np.tanh(ref * 2.0) / 2.0  # 喇叭有点失真
    echo = np.concatenate([np.zeros(delay), np.convolve(spk, h)[:n - delay]])
    mic = echo + 1e-4 * rng.standard_normal(n)
    out = run_guard(hub, g, mic, ref, rng)
    tail = slice(FS * 8, n - g.out_delay)
    assert db(mic[tail]) - db(out[g.out_delay:][tail]) > 20  # 喇叭失真的那部分线性消不掉, 靠残余压制
    assert g.coupled() and g.mdf.adapted
    # 量到的延迟 = 到达的 30–50 ms (按最早到的一帧算, 约 30 ms) + 75 ms + 直达声 2 ms
    assert 95 < g.status()["lag_ms"] < 115
    assert not g.user_likely()  # 这会儿全是回声: 不该被它打断


def test_guard_keeps_your_words_while_the_video_plays():
    rng = np.random.default_rng(6)
    hub = FakeHub()
    g = EC.EchoGuard(hub)
    n = FS * 16
    ref = speechish(n, rng) * 0.2
    h = np.zeros(1200)
    h[32], h[200] = 0.5, 0.15
    echo = np.concatenate([np.zeros(1000), np.convolve(ref, h)[:n - 1000]])
    near = np.zeros(n)
    near[FS * 11:FS * 14] = speechish(FS * 3, rng, 2.0) * 0.05
    mic = echo + near
    out = run_guard(hub, g, mic, ref, rng)[g.out_delay:]
    seg = slice(FS * 11, FS * 14)
    err_in = db(mic[seg] - near[seg])
    err_out = db(out[seg] - near[seg][: out[seg].size])
    assert err_out < err_in - 10  # 你说话那几秒: 回声少了 10 dB 以上
    assert db(out[seg]) > db(near[seg]) - 6  # 你的声音没被压下去


def test_fit_and_fine_offset_on_known_signals():
    """诊断用的两个量: 固定的线性路径拟合得上、量得出还差几个采样; 毫不相干的约 0 dB; 增益来回摆 (自动增益) 时整段拟合不上."""
    rng = np.random.default_rng(9)
    x = speechish(FS * 5, rng) * 3000
    d = np.concatenate([np.zeros(10), 0.4 * x[:-10]]) + 5 * rng.standard_normal(x.size)
    off, z = EC.fine_offset(d[:FS], x[:FS])
    assert off == 10 and z > 20
    assert EC.lti_fit_db(x, d) > 30 and EC.lti_fit_db(x[:FS], d[:FS], 2048) > 25
    noise = rng.standard_normal(x.size) * 900
    assert EC.lti_fit_db(x, noise) < 1 and EC.fine_offset(noise[:FS], x[:FS])[1] < 6
    assert EC.lti_fit_db(x[:4000], d[:4000]) is None  # 太短, 不报
    swing = d * (1 + 0.6 * np.sin(2 * np.pi * 0.4 * np.arange(d.size) / FS))
    assert EC.lti_fit_db(x, swing) < EC.lti_fit_db(x, d) - 15


def test_diag_line_is_numbers_only_and_shows_a_dropped_buffer():
    """外放时每 diag_s 秒一行诊断 (真机上看消不干净卡在哪): 只有数字, 没有字、没有声音;
    麦克风那边丢了 10 ms (浏览器丢一个缓冲), 逐秒的延迟偏移里看得出来, 那一段整段拟合不上, 复量以后改回来."""
    rng = np.random.default_rng(5)
    hub = FakeHub()
    g = EC.EchoGuard(hub)
    g.diag.sync = True  # 测试里当场算完 (线上是后台线程)
    rows = []
    g._diag_done = rows.append
    n = FS * 24
    ref = speechish(n, rng) * 0.2
    h = np.zeros(1200)
    h[32], h[150], h[400] = 0.5, 0.2, -0.1
    echo = np.concatenate([np.zeros(1200), np.convolve(ref, h)[:n - 1200]])
    k = FS * 9
    echo = np.concatenate([echo[:k], echo[k + 160:], np.zeros(160)])
    run_guard(hub, g, echo + 1e-4 * rng.standard_normal(n), ref, rng)
    assert len(rows) == 4

    def numbers(v):
        return v is None or isinstance(v, (bool, int, float)) or (isinstance(v, list) and all(numbers(i) for i in v))

    assert all(numbers(v) for r in rows for v in r.values()) and json.dumps(rows)
    first, hit, after, fixed = rows
    assert first["blk"] >= 200 and first["coupled"] and first["ref_fill"] == [0, 0] and first["frames"] >= 120
    assert -30 < first["mic_db"] < -10 and -20 < first["ref_db"] < -8 and first["le_med"] > 10
    assert hit["fine_ms"][0] == 0.0 and hit["fine_ms"][-1] == -10.0  # 丢了以后, 回声比对齐的位置早 10 ms
    assert after["fine_ms"] == [-10.0] * 5 and after["fit_db"] > 15  # 路径没再变: 固定的线性滤波器拟合得上
    assert hit["fit_db"] < after["fit_db"] - 8  # 路径在这一段里变过: 整段拟合不上
    assert fixed["realign"] >= 1 and fixed["fine_ms"][-1] == 0.0  # 复量以后延迟改过来了
    g.close()


def test_diag_is_quiet_when_nothing_plays_and_can_be_switched_off():
    rng = np.random.default_rng(3)
    hub = FakeHub()
    g = EC.EchoGuard(hub)
    g.diag.sync = True
    run_guard(hub, g, speechish(FS * 8, rng) * 0.1, None, rng)
    assert g.last_diag is None  # 没在放声音: 一行都不出
    assert EC.EchoGuard(FakeHub(), {"diag_s": 0}).diag is None


def test_ring_counts_gap_fills_for_the_diag():
    r = RefRing(seconds=5)
    pk = np.ones(160, np.float32)
    r.push(pk, 10.0)
    r.push(pk, 10.0102)  # 连着放: 不补, 只记时间戳差了多少
    assert r.fills == 0 and abs(r.skew_max - 0.0002) < 1e-6
    r.push(pk, 10.5)  # 时间线上空了将近半秒: 补静音, 记一次
    assert r.fills == 1 and r.fill_n == int(round((10.5 - (10.0102 + 0.01)) * FS))


_DIAG_ROWS: dict = {}
_OLD_WINDOW = {"partitions": 8, "pre_ms": 32}  # 诊断的测试钉死在 11-11 之前的窗口 (往前留 32 ms、共 160 ms), 不随默认值变


def _numbers(v):
    return v is None or isinstance(v, (bool, int, float)) or (isinstance(v, list) and all(_numbers(i) for i in v))


def _diag_scene(seed=5, n=FS * 24):
    rng = np.random.default_rng(seed)
    ref = speechish(n, rng) * 0.2
    h = np.zeros(1200)
    h[32], h[150], h[400] = 0.5, 0.2, -0.1
    echo = np.concatenate([np.zeros(1200), np.convolve(ref, h)[:n - 1200]])
    return rng, ref, echo + 1e-4 * rng.standard_normal(n)


def _diag_rows(conf=None, ring_late=0.01, seed=5, n=FS * 24, fresh=False):
    key = (json.dumps(conf, sort_keys=True), ring_late, seed, n)
    if key in _DIAG_ROWS and not fresh:  # 同样的跑法只跑一遍 (monkeypatch 过的要 fresh=True 重跑)
        return _DIAG_ROWS[key]
    rng, ref, mic = _diag_scene(seed, n)
    hub = FakeHub()
    g = EC.EchoGuard(hub, {**_OLD_WINDOW, **(conf or {})})
    g.diag.sync = True
    rows = []
    g._diag_done = rows.append
    out = run_guard(hub, g, mic, ref, rng, ring_late=ring_late)
    g.close()
    got = ([r for r in rows if r["blk"] >= 25], out, g)
    if not fresh:
        _DIAG_ROWS[key] = got
    return got


def test_diag_new_numbers_are_there_and_make_sense():
    """11-11 第三次真机后每 5 秒那行多的几项 (q_ms、参考尾巴、分频带、冲激响应、fit_win / fit_prev、滤波器自己的读数): 只有数字, 数得对."""
    rows, _, g = _diag_rows()
    assert len(rows) == 4 and all(_numbers(v) for r in rows for v in r.values()) and json.dumps(rows)
    for r in rows:
        assert r["q_ms"] == (round(r["lag_ms"] + r["skew_ms"], 1) if r["lag_ms"] is not None else None)
        assert r["tail_blk"] >= 200 and r["tail_miss"] == 0 and r["tail_min_ms"] > 0 and r["tail_short_ms"] is None  # 环一直比远端块新
        assert len(r["band_mic"]) == 3 and abs(sum(r["band_mic"]) - 1.0) < 0.01 and len(r["band_lin_db"]) == 3
        assert abs(sum(r["ir_frac"]) - 1.0) < 0.01 and len(r["ir_peaks"]) == 3
        assert r["leak_med"] is not None and r["erle_med"] is not None and r["mu_med"] is not None
    last = rows[-1]
    assert last["fit_db"] > 15 and last["band_lin_db"][1] > 15  # 300–3400 Hz 上线性消除 15 dB 以上
    assert abs(last["ir_peaks"][0][0]) < 1.0 and last["ir_peaks"][0][1] > -12  # 最高的峰就在对齐点上 (0.5 的直达声)
    assert last["ir_frac"][1] > 0.95  # 响应都在窗口里
    assert last["fit_win_db"] > last["fit_db"] - 3  # 窗口够用: 只留窗口里那段几乎一样好
    assert rows[0]["fit_prev_db"] is None and last["fit_prev_db"] > last["fit_db"] - 6  # 路径没变: 上一段的响应这一段照样用得上


def test_diag_sees_energy_that_lies_outside_the_filter_window():
    """回声里有一条比对齐点早 60 ms 的径 (窗口只往前留 32 ms 时够不着): 冲激响应里 ir_frac 的第一项不小、fit_win_db 比 fit_db 低得多."""
    rng = np.random.default_rng(8)
    n = FS * 24
    ref = speechish(n, rng) * 0.2
    h = np.zeros(2400)
    h[1200 + 32], h[1200 + 150], h[1200 + 400] = 0.5, 0.2, -0.1       # 主径
    h[1200 + 32 - 960], h[1200 + 150 - 960] = 0.45, 0.18               # 早 60 ms 的径 (同样的响应)
    echo = np.concatenate([np.zeros(1200), np.convolve(ref, h)[:n - 1200]])
    mic = echo + 1e-4 * rng.standard_normal(n)
    hub = FakeHub()
    g = EC.EchoGuard(hub, _OLD_WINDOW)
    g.diag.sync = True
    rows = []
    g._diag_done = rows.append
    run_guard(hub, g, mic, ref, rng)
    g.close()
    last = [r for r in rows if r["blk"] >= 25][-1]
    # 量到的延迟落在较强的那条上 (按 GCC), 早的那条在窗口前面: "之前" 那一项不小, 只留窗口里的拟合比整段拟合低
    assert last["ir_frac"][0] > 0.2 and last["fit_db"] - last["fit_win_db"] > 3, last
    assert any(abs(p[0] + 60.0) < 2.0 for p in last["ir_peaks"]), last["ir_peaks"]


def test_diag_counts_far_blocks_that_were_read_before_they_were_written():
    """回环写进环晚了 (Q 小、回环 10 ms 一包又晚写): 远端块的尾巴比环里已写到的位置新, 读出来是 0. 诊断数得出来: 几块、最小余量 (负的 =
    缺了多少 ms)、缺的块平均缺多少; 环按时写时一块都不缺."""
    free = {"hold_ms": 0, "pre_safety_ms": -1000}  # 不等参考、也不自己把往前留的减下来 (那是另一回事): 只看数数
    rows, _, _ = _diag_rows(free, ring_late=0.01)
    assert all(r["tail_miss"] == 0 and r["tail_min_ms"] > 0 for r in rows)
    ok_min = min(r["tail_min_ms"] for r in rows)
    rows, _, _ = _diag_rows(free, ring_late=0.01 + (ok_min + 25) / 1000)  # 再晚写 (最小余量 + 25 ms): 最后一两块的尾巴就读到没写的了
    late = rows[-1]
    assert late["tail_miss"] > 0 and late["tail_min_ms"] < 0 and late["tail_short_ms"] > 0, late
    assert late["tail_miss"] < late["tail_blk"]  # 不是每块都缺 (每批帧里只有最后的块最危险)


def test_diag_never_changes_the_sound():
    """诊断开不开、后台线程还是当场算, 交出去的声音逐字节一样 (诊断只看不碰)."""
    outs = []
    for conf, sync in (({"diag_s": 0}, False), ({}, False), ({}, True)):
        rng, ref, mic = _diag_scene(6, FS * 14)
        hub = FakeHub()
        g = EC.EchoGuard(hub, {**_OLD_WINDOW, **conf})
        if g.diag is not None:
            g.diag.sync = sync
            g._diag_done = lambda row: None
        outs.append(run_guard(hub, g, mic, ref, rng))
        g.close()
    assert np.array_equal(outs[0], outs[1]) and np.array_equal(outs[0], outs[2])


def test_diag_failures_only_drop_the_diag(monkeypatch):
    """每块那几个数数出错、后台新加的几组算不出来: 只丢出错的那几组诊断, 声音和别的诊断照旧."""
    base_rows, base_out, _ = _diag_rows(n=FS * 14)
    monkeypatch.setattr(EC.EchoDiag, "tail", lambda self, m: 1 / 0)
    monkeypatch.setattr(EC.EchoDiag, "filt", lambda self, *a: 1 / 0)
    monkeypatch.setattr(EC, "band_split", lambda d, e: 1 / 0)
    rows, out, g = _diag_rows(n=FS * 14, fresh=True)
    assert np.array_equal(out, base_out)
    # 别的诊断照旧; 出错的那几组丢了 (分频带没有、尾巴一块都没数到), 没出错的几组还在 (冲激响应)
    assert rows and all("fit_db" in r and "band_mic" not in r and r["tail_blk"] == 0 and "fit_win_db" in r for r in rows)


def test_mic_processing_hint_only_when_echo_handling_is_there():
    """能去掉外放回声时, 让页面别开浏览器的降噪和自动增益 (它们把回声变得不再线性, 2026-11-11 真机最多只消得掉约 12 dB);
    去不了时 None: 浏览器照默认, 和原来一样. 回声消除可以配成 "all" / "remote-only", 别的写法都当真假."""
    assert EC.mic_processing(EC.DEFAULTS, True) == {"echoCancellation": True, "noiseSuppression": True, "autoGainControl": False}
    assert EC.mic_processing(EC.DEFAULTS, False) is None
    assert EC.mic_processing({**EC.DEFAULTS, "enabled": False}, True) is None
    assert EC.mic_processing({**EC.DEFAULTS, "mic_ec": "all"}, True)["echoCancellation"] == "all"
    assert EC.mic_processing({**EC.DEFAULTS, "mic_ec": "随便"}, True)["echoCancellation"] is True
    off = EC.mic_processing({**EC.DEFAULTS, "mic_ns": False, "mic_agc": False}, True)
    assert off["noiseSuppression"] is False and off["autoGainControl"] is False
    on = EC.mic_processing({**EC.DEFAULTS, "mic_ns": True, "mic_agc": True}, True)
    assert on["noiseSuppression"] is True and on["autoGainControl"] is True


def frames40(x):
    return [np.clip(np.round(x[i:i + 640] * 32768), -32768, 32767).astype(np.int16) for i in range(0, x.size - 639, 640)]


def test_speech_gain_learns_your_level_only_when_nothing_plays():
    rng = np.random.default_rng(11)
    quiet = speechish(FS * 6, rng) * 10 ** (-40 / 20)  # 你的声音只有 -40 dBFS 上下 (浏览器的自动增益关了)
    g = EC.SpeechGain()
    for f in frames40(quiet):
        out = g.process(f, engaged=True)  # 外放时: 不学, 原样
        assert np.array_equal(out, f)
    assert g.gain_db == 0.0 and g.learned == 0
    noise = rng.standard_normal(FS * 2) * 10 ** (-65 / 20)
    for f in frames40(noise):
        g.process(f, engaged=False)  # 只有底噪: 不算你在说话
    assert g.gain_db == 0.0 and g.learned == 0
    for f in frames40(quiet):
        g.process(f, engaged=False)
    assert g.learned > 50 and 8.0 < g.gain_db <= 12.0  # 往 -26 dBFS 拉, 最多补 12 dB
    before = g.gain_db
    for f in frames40(quiet):  # 又开始放了: 冻住
        g.process(f, engaged=True)
    assert g.gain_db == before
    zeros = np.zeros(640, np.int16)
    assert not np.any(g.process(zeros, engaged=False)) and g.gain_db == before  # 半双工的静音帧: 不学, 也还是静音
    loud = (np.sin(np.arange(640) / 3.0) * 25000).astype(np.int16)
    assert int(np.max(np.abs(g.process(loud, engaged=True)))) <= 30001  # 补了会削顶的: 少补


def test_small_lag_dither_does_not_move_the_filter(monkeypatch):
    """复量的延迟只差几个采样 (真机上 154.8↔155.0 ms 来回跳) 不挪滤波器; 真的变了 (差几毫秒、几次都这样) 才挪."""
    hub = FakeHub()
    g = EC.EchoGuard(hub)
    hub.ring.push(np.full(FS * 4, 0.1, np.float32), 100.0)
    g.sync, g.mic_n = (0, 0), g.gcc_w
    g.mic_hist = np.full(g.gcc_w, 1000.0)
    g.mic_hist_zero[:] = False
    seq = iter([2000, 2000, 2003, 2003, 2000, 2003, 2003, 2000] + [2060] * 6)
    monkeypatch.setattr(EC, "gcc_phat", lambda mic, ref, max_lag: (next(seq), 50.0))
    g.now = 100.0
    for _ in range(8):
        g._measure_lag()
        g.now += 0.5
    assert g.lag in (2000, 2003) and g.stats["realign"] == 0
    for _ in range(6):
        g._measure_lag()
        g.now += 0.5
    assert g.lag == 2060 and g.stats["realign"] == 1
    g.close()


def test_text_check_ratio():
    assert EC.echo_ratio("你看到这个吗", "嗯，你看到这个吗？我们继续") == 1.0
    assert EC.echo_ratio("这个模型我能跑吗", "今天我们来看一下显卡的价格") < 0.4
    g = EC.EchoGuard(FakeHub())
    assert g.is_echo("十六G显存就能跑", "官方给出的测试数据，十六G显存就能跑二十七B的模型")[0]
    assert not g.is_echo("我想吃饭", "官方给出的测试数据")[0]
    assert g.is_echo("好", "你好呀")[0] and not g.is_echo("好的", "你好呀")[0]  # 两个字以内要全对上才算
    assert not g.is_echo("", "什么都行")[0]
    g.close()


class FakeVAD:
    def __init__(self, script):
        self.script = list(script)  # 每次 accept 的 (speaking, segment_ended)
        self.ended = False

    def accept_waveform(self, x):
        self.cur = self.script.pop(0) if self.script else (False, False)
        self.ended = self.cur[1]

    def is_speech_detected(self):
        return self.cur[0]

    def empty(self):
        return not self.ended

    def pop(self):
        self.ended = False


class FakeRec:
    def __init__(self, texts):
        self.texts = texts

    def create_stream(self):
        rec = self

        class S:
            def accept_waveform(self, sr, x):
                self.kind = "ref" if x.size and float(np.max(x)) > 0.9 else "mic"

            @property
            def result(self):
                class R:
                    text = rec.texts[self.kind]
                return R()
        return S()

    def decode_stream(self, s):
        pass


class StubGuard:
    def __init__(self, echo=True):
        self.echo = echo
        self.stats = {"echo_drops": 0}
        self.asked = []

    def ref_for(self, s0, s1):
        self.asked.append((s0, s1))
        return np.ones(1600, np.float32) if self.echo else None

    def near_ratio(self, s0, s1):
        return self.near

    near = None

    def is_echo(self, heard, ref_text):
        return EC.EchoGuard.is_echo(self, heard, ref_text)

    conf = dict(EC.DEFAULTS)


def make_vad_session(script, texts, guard=None):
    s = VadSenseVoiceSession.__new__(VadSenseVoiceSession)
    s.rec = FakeRec(texts)
    s.sr = FS
    s.vad = FakeVAD(script)
    s.preroll_n, s.hold_n, s.interim_n = 8000, 4000, 0
    s.preroll, s.utt, s.seen, s.paused_at, s.pause_text, s.last_interim = [], None, 0, None, "", 0
    s.last_meta, s.utt_start, s.echo, s.echo_text = {}, 0, guard, True
    return s


def frames(n):
    return [np.full(1600, 0.1, np.float32) for _ in range(n)]


def test_session_drops_an_echo_sentence_and_says_listening_is_over():
    g = StubGuard(echo=True)
    s = make_vad_session([(True, False), (True, False), (False, True), (False, False), (False, False)],
                         {"mic": "十六G显存就能跑", "ref": "官方说十六G显存就能跑二十七B的模型"}, g)
    ev = [e for x in frames(5) for e in s._process(x)]
    kinds = [k for k, _ in ev]
    assert kinds == ["speech_start", "echo", "utt_end"]
    assert g.asked and g.asked[0][0] == 0 and g.stats["echo_drops"] == 1
    assert s.utt is None


def test_session_keeps_your_sentence_when_the_video_says_something_else():
    g = StubGuard(echo=True)
    s = make_vad_session([(True, False), (False, True), (False, False), (False, False), (False, False)],
                         {"mic": "你看到这个吗", "ref": "官方说十六G显存就能跑二十七B的模型"}, g)
    ev = [e for x in frames(5) for e in s._process(x)]
    assert [k for k, _ in ev] == ["speech_start", "pause", "final"]
    assert s.last_meta["echo_check"]["echo"] is False


def test_session_drops_a_short_scrap_that_was_almost_all_echo():
    """外放时一两个字的残渣 ("嗯""啊"): 这一句几乎每块都判成全是回声, 不用认参考就丢掉; 你自己的"嗯"不会这样."""
    g = StubGuard(echo=True)
    g.near = 0.02
    s = make_vad_session([(True, False), (False, True), (False, False), (False, False), (False, False)],
                         {"mic": "嗯。", "ref": "官方说十六G显存就能跑"}, g)
    ev = [e for x in frames(5) for e in s._process(x)]
    assert [k for k, _ in ev] == ["speech_start", "echo", "utt_end"]
    assert s.last_meta["echo_check"]["by"] == "gated"
    for near, text in ((0.8, "嗯。"), (0.02, "你看懂他刚才那张图了吗")):  # 你的"嗯"; 回声很响时你说的整句
        g2 = StubGuard(echo=True)
        g2.near = near
        s2 = make_vad_session([(True, False), (False, True), (False, False), (False, False), (False, False)],
                              {"mic": text, "ref": "官方说十六G显存就能跑"}, g2)
        assert [k for k, _ in (e for x in frames(5) for e in s2._process(x))] == ["speech_start", "pause", "final"]


def test_session_ends_a_segment_without_words():
    s = make_vad_session([(True, False), (False, True), (False, False), (False, False), (False, False)],
                         {"mic": "", "ref": ""}, None)
    ev = [e for x in frames(5) for e in s._process(x)]
    assert [k for k, _ in ev] == ["speech_start", "utt_end"]


def test_pipeline_stops_listening_after_a_wordless_segment(tmp_path):
    """以前没认出字的一段 (咳嗽、外放的声音) 会让"在听"一直挂着, 陪看想说的话一直被挡."""
    async def main():
        cfg = load_config(overrides=["llm.engine=mock", "tts.engine=mock", "asr.engine=none", "face.engine=none",
                                     "memory.vault=\"\"", f"trace.log_dir={json.dumps(str(tmp_path))}"])
        engines = Engines(cfg)
        await engines.load()
        script = [[("speech_start", "")], [("utt_end", "")]]

        class ASR:
            name = "fake"
            sample_rate = 16000

            def create_session(self):
                class S:
                    async def accept(self, pcm):
                        return script.pop(0) if script else []

                    def close(self):
                        pass
                return S()
        engines.asr = ASR()
        sent = []

        async def send(m):
            sent.append(m)
        s = Session(engines, cfg, send, LatencyStats(), TraceLog(tmp_path))
        frame = (np.ones(640) * 3000).astype(np.int16).tobytes()
        await s.on_audio(frame)
        assert s.listening and s._aside_blocked() == "listening"
        await s.on_audio(frame)
        types = [m.get("type") for m in sent if isinstance(m, dict)]
        assert "listening" in types and "listening_end" in types
        assert not s.listening and s._aside_blocked() is None
        assert s.echo is not None  # Windows 上有回环可用就挂上了 (测试里不真的录, 只原样放过去)
        # 页面报来浏览器的自动增益关了: 回声处理之后自己补音量; 开着就不补 (免得两道自动增益互相拉)
        assert engines.mic_processing() == {"echoCancellation": True, "noiseSuppression": True, "autoGainControl": False}
        await s.on_message({"type": "mic_settings", "settings": {"echoCancellation": True, "noiseSuppression": False,
                                                                  "autoGainControl": False, "deviceId": "不收"}})
        assert s.gain is not None and s.mic_settings == {"echoCancellation": True, "noiseSuppression": False,
                                                         "autoGainControl": False}
        kept = s.gain
        await s.on_message({"type": "mic_settings", "settings": {"autoGainControl": False}})
        assert s.gain is kept  # 重开麦克风又报一次: 接着用学到的
        await s.on_message({"type": "mic_settings", "settings": {"autoGainControl": True}})
        assert s.gain is None
        await s.close()
        await engines.close()

    import sys
    if sys.platform != "win32":
        import pytest
        pytest.skip("回环只在 Windows 上")
    asyncio.run(main())


# -- 多等一会儿 (hold_ms) 和往前留的按环里写到哪减 (pre_safety_ms) -------------------------------------------------------------------------------
# 参考是从回环录来的, 写进环总要晚一点: 远端块 (从对齐点往前 pre 个采样起、一块长) 的尾巴比环里已写到的位置新时, 读出来是 0, 这些块的谱进了滤波器,
# 后面 M 块的回声估计都带着洞, 线上滤波器卡在平台 (11-11 第三次真机; tools/echo_bench.py 的 q33 环境复现了).


def test_pre_is_cut_back_when_the_reference_tail_is_not_written_yet():
    """远端块的尾巴比环里已写到的位置新 (Q 小、回环 10 ms 一包又晚写): 读出来是 0, 线上滤波器卡在平台. 现在: 麦克风的块多攒 hold_ms 再处理
    (参考多写进来一点), 最近几秒里最小的余量还不够 pre_safety_ms 就把往前留的减下来. 不等也不减的 (hold_ms = 0、pre_safety_ms 给个很小的数) 对照一下."""
    res = {}
    for name, conf in (("fixed", {}), ("stuck", {"hold_ms": 0, "pre_safety_ms": -1000})):
        rng = np.random.default_rng(31)
        n = FS * 20
        ref = speechish(n, rng) * 0.2
        h = np.zeros(1200)
        h[32], h[150], h[400] = 0.5, 0.2, -0.1
        echo = np.concatenate([np.zeros(1200), np.convolve(ref, h)[:n - 1200]])
        mic = echo + 1e-4 * rng.standard_normal(n)
        hub = FakeHub()
        g = EC.EchoGuard(hub, {**conf, "res": False, "pre_ms": 32, "diag_s": 0})
        # 回环写进环比到达早 110 ms 以前的: 远端块的尾巴 (往前留 32 ms) 比环里已写到的位置新 (等 40 ms 还不够, 得再减)
        out = run_guard(hub, g, mic, ref, rng, ring_late=0.135)
        tail = slice(FS * 12, n - g.out_delay)
        res[name] = (db(mic[tail]) - db(out[g.out_delay:][tail]), g.stats.get("pre_cut", 0), g.pre)
        g.close()
    assert res["fixed"][0] > 15 and res["fixed"][1] >= 1 and res["fixed"][2] < 32 * 16, res   # 减下来了、消得掉
    assert res["stuck"][0] < res["fixed"][0] - 8 and res["stuck"][1] == 0, res                # 不减、不等就卡住


def test_hold_makes_the_output_later_by_hold_and_nothing_else_changes():
    """hold_ms: 输出比输入晚 N + hold 个采样 (out_delay), 不放声音时原样过去一个采样都不改; hold = 0 时和以前一样只晚 N."""
    rng = np.random.default_rng(32)
    mic = speechish(FS * 3, rng) * 0.1
    q = np.clip(np.round(mic * 32768), -32768, 32767) / 32768
    for hold, want in ((20, 320 + 320), (0, 320), (40, 320 + 640)):
        hub = FakeHub()
        g = EC.EchoGuard(hub, {"hold_ms": hold})
        out = run_guard(hub, g, mic, None, rng)
        assert g.out_delay == want and np.array_equal(out[g.out_delay:], q[:-g.out_delay]), hold
        g.close()


@pytest.mark.parametrize("hold", [0, 20, 40])
def test_output_is_the_input_delayed_by_out_delay_through_playback_start_and_stop(hold):
    """戴耳机 (在放视频、麦克风里没有): 输出 = 输入整体晚 out_delay (= N + hold), 逐字节一样; 中途开始放、停放, 总长度对、没有跳 (不插静音、不丢采样)."""
    rng = np.random.default_rng(41)
    n = FS * 16
    mic = speechish(n, rng, 1.0) * 0.05 + 1e-4 * rng.standard_normal(n)
    ref = speechish(n, rng) * 0.2
    ref[:FS * 4] = 0.0          # 4 秒才开始放
    ref[FS * 10:] = 0.0         # 10 秒停放
    hub = FakeHub()
    g = EC.EchoGuard(hub, {"hold_ms": hold})
    out = run_guard(hub, g, mic, ref, rng)
    q = np.clip(np.round(mic * 32768), -32768, 32767) / 32768
    assert out.size == mic.size and g.out_delay == 320 + hold * FS // 1000
    assert np.array_equal(out[g.out_delay:], q[:-g.out_delay])
    assert not g.coupled()
    g.close()


def test_the_mic_keeps_going_when_the_loopback_gets_stuck():
    """回环卡住 (一阵没有新的参考写进环): 麦克风照样往前走 (等的只是麦克风自己的采样), 每一帧都有输出、长度对、没有坏值; 回环停了三秒以后原样放过去."""
    rng = np.random.default_rng(42)
    n = FS * 14
    ref = speechish(n, rng) * 0.2
    h = np.zeros(1200)
    h[32], h[150] = 0.5, 0.2
    echo = np.concatenate([np.zeros(1200), np.convolve(ref, h)[:n - 1200]])
    mic = echo + speechish(n, rng, 2.0) * 0.02 + 1e-4 * rng.standard_normal(n)
    ref_stuck = ref.copy()
    ref_stuck[FS * 5:] = 0.0                       # 5 秒起回环再没有新的 (进环的是静音 / 不写)
    hub = FakeHub()
    g = EC.EchoGuard(hub, {})
    out = np.zeros(n)
    rp, pk = 0, FS // 100
    for k in range(n // 640):
        arrive = 100.0 + (k + 1) * 640 / FS + 0.03 + rng.uniform(0, 0.02)
        while rp < FS * 5 and 100.0 + rp / FS < arrive - 0.01:   # 5 秒以后不再往环里写
            hub.ring.push(ref_stuck[rp:rp + pk].astype(np.float32), 100.0 + rp / FS)
            rp += pk
        frame = np.clip(np.round(mic[k * 640:(k + 1) * 640] * 32768), -32768, 32767).astype(np.int16)
        got = g.process(frame, arrive)
        assert got.size == 640
        out[k * 640:(k + 1) * 640] = got.astype(np.float64) / 32768
    assert np.all(np.isfinite(out)) and db(out[FS * 9:]) > db(mic[FS * 9:FS * 13]) - 1  # 停了三秒以后原样, 没被压
    q = np.clip(np.round(mic * 32768), -32768, 32767) / 32768
    assert np.array_equal(out[FS * 10 + g.out_delay:], q[FS * 10:-g.out_delay])
    g.close()


@pytest.mark.parametrize("hold", [0, 20, 40])
def test_ref_for_lines_up_with_the_output_stream_after_hold(hold):
    """整句复核按输出流的位置取参考那一段: 参考里有一小段很响的音 (8.0 秒起), 回声晚 75 ms 进麦克风; 加了 hold 以后输出多晚了 hold, ref_for 取到的那一段里
    这段音还在应该在的位置 (差不到 3 ms)."""
    rng = np.random.default_rng(43)
    n = FS * 14
    ref = speechish(n, rng) * 0.1
    tb, burst = int(8.0 * FS), int(0.3 * FS)
    ref[tb:tb + burst] += np.sin(2 * np.pi * 440 * np.arange(burst) / FS) * 0.5
    d = 1200
    mic = np.concatenate([np.zeros(d), ref])[:n] * 0.5 + 1e-4 * rng.standard_normal(n)
    hub = FakeHub()
    g = EC.EchoGuard(hub, {"hold_ms": hold})
    run_guard(hub, g, mic, ref, rng)
    assert g.coupled()
    ob = tb + d + g.out_delay                      # 这段音的回声在输出流里的位置
    s0, s1 = ob - int(0.3 * FS), ob + int(0.6 * FS)
    seg = g.ref_for(s0, s1)
    assert seg is not None
    pad = int(0.3 * FS)                            # ref_for 前后各多取 0.3 秒
    env = np.convolve(np.abs(seg.astype(np.float64)), np.ones(160) / 160, mode="same")
    got = int(np.argmax(env > 0.5 * env.max()))    # 这段音在 seg 里起的位置
    want = pad + (ob - s0)
    assert abs(got - want) < 48, (hold, got, want)
    g.close()


def test_pre_goes_back_to_the_setting_when_the_device_changes():
    """换了播放设备 (ring.epoch 变了): 滤波器重来, 往前留的也回到设定值 (新设备的余量要重新量), 不留着旧设备减下来的数."""
    rng = np.random.default_rng(44)
    n = FS * 24
    ref = speechish(n, rng) * 0.2
    h = np.zeros(1200)
    h[32], h[150], h[400] = 0.5, 0.2, -0.1
    echo = np.concatenate([np.zeros(1200), np.convolve(ref, h)[:n - 1200]])
    mic = echo + 1e-4 * rng.standard_normal(n)
    hub = FakeHub()
    g = EC.EchoGuard(hub, {"hold_ms": 0, "diag_s": 0})
    half = n // 2
    run_guard(hub, g, mic[:half], ref[:half], rng, ring_late=0.125)  # 前一半: 回环晚写, 往前留的会被减下来
    assert g.pre < g.pre_max and g.stats["pre_cut"] >= 1
    hub.ring.epoch += 1                                              # 换设备
    rp, pk = half, FS // 100
    t0 = 100.0
    for k in range(half // 640, n // 640):
        arrive = t0 + (k + 1) * 640 / FS + 0.03 + rng.uniform(0, 0.02)
        while rp < n and t0 + rp / FS < arrive - 0.01:               # 后一半: 回环按时写
            hub.ring.push(ref[rp:rp + pk].astype(np.float32), t0 + rp / FS)
            rp += pk
        g.process(np.clip(np.round(mic[k * 640:(k + 1) * 640] * 32768), -32768, 32767).astype(np.int16), arrive)
        if k == half // 640 + 3:                                     # 换了设备以后几帧: 复位了
            assert g.pre == g.pre_max
    g.close()


def test_diag_row_has_the_current_pre_and_how_many_times_it_was_cut():
    """诊断行里有 pre_ms (现在往前留多少毫秒) 和 pre_cut (这一段里调了几次); 余量够时是设定值、一次不调."""
    rng = np.random.default_rng(45)
    n = FS * 24
    ref = speechish(n, rng) * 0.2
    h = np.zeros(1200)
    h[32], h[150], h[400] = 0.5, 0.2, -0.1
    echo = np.concatenate([np.zeros(1200), np.convolve(ref, h)[:n - 1200]])
    mic = echo + 1e-4 * rng.standard_normal(n)
    for late, want_cut in ((0.01, False), (0.125, True)):
        hub = FakeHub()
        g = EC.EchoGuard(hub, {"hold_ms": 0})
        g.diag.sync = True
        rows = []
        g._diag_done = rows.append
        run_guard(hub, g, mic, ref, np.random.default_rng(46), ring_late=late)
        rows = [r for r in rows if r["blk"] >= 25]
        assert all("pre_ms" in r and "pre_cut" in r for r in rows)
        if want_cut:
            assert sum(r["pre_cut"] for r in rows) >= 1 and rows[-1]["pre_ms"] < 32.0
        else:
            assert sum(r["pre_cut"] for r in rows) == 0 and rows[-1]["pre_ms"] == 32.0
        g.close()


class RacingRing(RefRing):
    """read() 一开始回环又写进来一包 (10 ms): 模拟回环线程和处理线程的赛跑; 记下每次读的 (起点, 长度, 读之前已写到哪)."""

    def __init__(self):
        super().__init__()
        self.log = []

    def read(self, n0, k):
        before = self.n
        self.log.append((n0, k, before))
        with self.lock:
            self._write(np.zeros(160, np.float32))  # 读的时候又写进来 160 个采样 (只加长, 不动时间锚点)
        return super().read(n0, k)


def test_margin_is_taken_when_the_far_block_is_read():
    """余量在读远端块的那一刻记 (读之前环里写到哪), 不是处理完以后再看: 读的时候回环要是又写进一包, 余量不能被量大 (量大了该减往前留的就没减)."""
    rng = np.random.default_rng(47)
    n = FS * 10
    ref = speechish(n, rng) * 0.2
    h = np.zeros(1200)
    h[32], h[150] = 0.5, 0.2
    echo = np.concatenate([np.zeros(1200), np.convolve(ref, h)[:n - 1200]])
    mic = echo + 1e-4 * rng.standard_normal(n)
    hub = FakeHub()
    hub.ring = RacingRing()
    g = EC.EchoGuard(hub, {"hold_ms": 0, "pre_safety_ms": -1000, "diag_s": 0})  # 不自己调 pre: 往前留的一直是 32 ms
    run_guard(hub, g, mic, ref, rng)
    log = hub.ring.log
    far = [(n0, before) for (n0, k, before), (n1, k1, _) in zip(log, log[1:]) if k == 320 and k1 == 320 and n0 - n1 == g.pre]
    assert len(far) > 200
    want = [before - (n0 + 320) for n0, before in far][-len(g.margins):]
    assert list(g.margins) == want                       # 记下来的就是读之前的数
    assert min(g.margins) < min(before + 160 - (n0 + 320) for n0, before in far[-len(g.margins):])  # 比读完以后再看的小
    g.close()

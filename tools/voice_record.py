"""录自己的声音做 GPT-SoVITS 训练集: 一句一句录、当场检查, 录完整理成训练用的 .list.

  python tools/voice_record.py                  # 起录音页 http://127.0.0.1:8812, 录音存到 Bot用/TTS素材/我的声音
  python tools/voice_record.py prepare          # 整理成训练集: 去掉首尾多余静音、转 32 kHz、峰值 0.9、写 .list
  python tools/voice_record.py asr-test         # 用 SenseVoice 认"考卷"那些句子, 算字错率
  runtime\\gpt-sovits\\runtime\\python.exe tools\\voice_record.py coverage   # 稿子的拼音覆盖 (要 pypinyin, 整合包里有)

稿子在 tools/voice_record/script.tsv: id、第几次录、这一次里的顺序、类别、情绪、语言、是不是考卷、文本、提示.
中文句里的数字写成汉字、不夹英文字母: GPT-SoVITS 训练时会删掉中文句里的英文字母, 音和字就对不上了; 英文单独成句 (语言 EN).
录音只存在本机: <out>/raw/<id>.wav 是原样的录音, <out>/checks.json 是每句的检查结果. 这个目录不进安装包, 也别往外传.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import shutil
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent / "voice_record"
SCRIPT = HERE / "script.tsv"
DEFAULT_OUT = ROOT / "Bot用" / "TTS素材" / "我的声音"
DEFAULT_PORT = 8812
ID_RE = re.compile(r"^[A-Z]\d{3}$")
MAX_BODY = 30 * 1024 * 1024  # 一句最多 30 MB (48 kHz 16 位单声道约 5 分钟)
FRAME_S = 0.02
HAN = re.compile(r"[一-鿿]")


def load_script(path: Path = SCRIPT) -> list[dict]:
    with Path(path).open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    for r in rows:
        r["session"], r["order"], r["asr"] = int(r["session"]), int(r["order"]), r["asr"] == "1"
    rows.sort(key=lambda r: (r["session"], r["order"]))
    return rows


def read_wav(data: bytes):
    """浏览器传来的 WAV → (float32 单声道, 采样率). 只认 16 位 PCM."""
    import numpy as np

    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            ch, width, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError) as e:
        raise ValueError(f"不是能读的 WAV: {e}") from None
    if width != 2:
        raise ValueError(f"只收 16 位 PCM, 这个是 {width * 8} 位")
    if not 8000 <= sr <= 192000 or ch not in (1, 2):
        raise ValueError(f"采样率 {sr} / 声道 {ch} 不对")
    x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    if ch == 2:
        x = x[0::2]
    return x, sr


def analyze(x, sr: int, text: str = "", lang: str = "ZH") -> dict:
    """查一句录音: problems = 要重录, notes = 提醒一下. 语音段按 20 ms 帧的响度找 (比底噪高 15 dB 才算)."""
    import numpy as np

    x = np.asarray(x, dtype=np.float32)
    out: dict = {"sr": int(sr), "dur_s": round(len(x) / sr, 2) if sr else 0.0}
    problems: list[str] = []
    notes: list[str] = []
    hop = max(1, int(sr * FRAME_S))
    n = len(x) // hop
    if n < 10:
        problems.append("没录到东西")
        return {**out, "problems": problems, "notes": notes, "level": "bad"}
    rms = np.sqrt((x[:n * hop].astype(np.float64).reshape(n, hop) ** 2).mean(axis=1)) + 1e-9
    db = 20 * np.log10(rms)
    peak = float(np.abs(x).max())
    peak_db = 20 * math.log10(peak + 1e-9)
    noise = float(np.percentile(db, 10))
    idx = np.flatnonzero(db > max(noise + 15.0, -50.0))
    out.update(peak_db=round(peak_db, 1), noise_db=round(noise, 1))
    if int((np.abs(x) >= 0.999).sum()) >= 3:
        problems.append("有爆音: 离话筒远一点, 或者把输入音量调小, 这句重录")
    if idx.size == 0 or peak_db < -35:
        problems.append("几乎没有声音: 看看是不是选错了话筒, 或者话筒被静音了")
    else:
        first, last = int(idx[0]), int(idx[-1])
        start, end = first * FRAME_S, (last + 1) * FRAME_S
        speech = end - start
        out.update(start_s=round(start, 2), end_s=round(end, 2), speech_s=round(speech, 2),
                   lead_s=round(start, 2), tail_s=round(max(0.0, n * FRAME_S - end), 2))
        if peak_db < -24:
            problems.append("声音太小: 靠近一点, 或者把输入音量调大")
        elif peak_db < -18:
            notes.append("声音偏小: 可以靠近一点或者把输入音量调大一点")
        chars = len(HAN.findall(text))
        if speech < 0.8 or (lang == "ZH" and chars and speech < chars / 9.0):
            problems.append("这句太短了, 好像没读完")
        if speech > 11.0:
            notes.append("超过 11 秒了: 训练时会被切开, 可以读快一点")
        if start < 0.15:
            notes.append("开头太赶: 按下录音停半秒再开口")
        if out["tail_s"] < 0.15:
            notes.append("结尾太赶: 说完停半秒再按停止")
        if noise > -48:
            notes.append(f"背景有点吵 (底噪 {noise:.0f} dB): 关掉风扇、空调再录会更好")
    out.update(problems=problems, notes=notes, level="bad" if problems else ("warn" if notes else "ok"))
    return out


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _load_checks(out: Path) -> dict:
    p = out / "checks.json"
    try:
        checks = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        checks = {}
    return {k: v for k, v in checks.items() if (out / "raw" / f"{k}.wav").exists()}


def make_app(out: Path, script: list[dict]):
    """录音页的服务: 只听 127.0.0.1, 只认本机 Host (防 DNS 重绑定把录音删了)."""
    from aiohttp import web

    by_id = {r["id"]: r for r in script}
    raw_dir = Path(out) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    checks = _load_checks(Path(out))

    @web.middleware
    async def local_only(request, handler):
        host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            return web.json_response({"error": "只接受本机打开的页面"}, status=403)
        resp = await handler(request)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def take_id(request) -> str:
        rid = request.match_info["id"]
        if not ID_RE.match(rid) or rid not in by_id:
            raise web.HTTPNotFound(text="稿子里没有这一句")
        return rid

    async def page(_):
        return web.FileResponse(HERE / "index.html")

    async def state(_):
        return web.json_response({"out": str(Path(out).resolve()), "script": script, "takes": checks})

    async def put_take(request):
        rid = take_id(request)
        body = await request.read()
        try:
            x, sr = read_wav(body)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        r = by_id[rid]
        res = analyze(x, sr, r["text"], r["lang"])
        res["saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = raw_dir / f"{rid}.wav.tmp"
        tmp.write_bytes(body)
        os.replace(tmp, raw_dir / f"{rid}.wav")
        checks[rid] = res
        _write_json(Path(out) / "checks.json", checks)
        return web.json_response(res)

    async def get_take(request):
        f = raw_dir / f"{take_id(request)}.wav"
        if not f.exists():
            raise web.HTTPNotFound(text="这句还没录")
        return web.FileResponse(f, headers={"Content-Type": "audio/wav"})

    async def delete_take(request):
        rid = take_id(request)
        (raw_dir / f"{rid}.wav").unlink(missing_ok=True)
        checks.pop(rid, None)
        _write_json(Path(out) / "checks.json", checks)
        return web.json_response({"ok": True})

    app = web.Application(client_max_size=MAX_BODY, middlewares=[local_only])
    app.router.add_get("/", page)
    app.router.add_get("/api/state", state)
    app.router.add_put("/api/take/{id}", put_take)
    app.router.add_get("/api/take/{id}", get_take)
    app.router.add_delete("/api/take/{id}", delete_take)
    return app


def prepare(out: Path, script: list[dict], speaker: str, sr: int = 32000, pad: float = 0.12,
            peak: float = 0.9) -> dict:
    """录好的整理成 GPT-SoVITS 训练集 (和 tools/voice_dataset.py 出的一样: 32 kHz 单声道、峰值 0.9、wav 绝对路径|说话人|语言|文本)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from voice_dataset import load_mono, write_wav  # noqa: E402  (要 ffmpeg)

    out = Path(out)
    ds = out / "dataset"
    for d in (ds / "wavs", ds / "ref"):
        d.mkdir(parents=True, exist_ok=True)
    lines, asr_lines, refs, skipped, total = [], [], [], [], 0.0
    for r in script:
        f = out / "raw" / f"{r['id']}.wav"
        if not f.exists():
            continue
        x = load_mono(f, sr)
        a = analyze(x, sr, r["text"], r["lang"])
        if a["level"] == "bad":
            skipped.append((r["id"], a["problems"]))
            continue
        lo = max(0.0, a["start_s"] - pad)
        hi = min(len(x) / sr, a["end_s"] + pad)
        clip = x[int(lo * sr):int(hi * sr)]
        clip = clip / (float(abs(clip).max()) or 1.0) * peak
        wav = ds / "wavs" / f"{r['id']}.wav"
        write_wav(wav, clip, sr)
        total += len(clip) / sr
        lines.append(f"{wav.resolve()}|{speaker}|{r['lang']}|{r['text']}")
        if r["cat"] == "参考":
            shutil.copyfile(wav, ds / "ref" / wav.name)
            (ds / "ref" / f"{r['id']}.txt").write_text(r["text"], encoding="utf-8")
            refs.append((r["id"], r["emo"], round(len(clip) / sr, 1)))
        if r["asr"]:
            asr_lines.append(f"{f.resolve()}|{r['text']}")
    (ds / f"{speaker}.list").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    (ds / "asr_test.list").write_text("\n".join(asr_lines) + ("\n" if asr_lines else ""), encoding="utf-8")
    return {"list": str((ds / f"{speaker}.list").resolve()), "lines": len(lines), "seconds": round(total, 1),
            "refs": refs, "skipped": skipped, "asr_lines": len(asr_lines)}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _edits(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def asr_test(out: Path, script: list[dict]) -> dict:
    """考卷那些句子交给 SenseVoice (和桌宠平时用的同一个模型、同样的设置), 算字错率."""
    import sherpa_onnx

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from voice_dataset import load_mono  # noqa: E402
    from wm.config import load_config, resolve_path  # noqa: E402

    s = load_config()["asr"]["vad_sensevoice"]
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(resolve_path(s["model"])), tokens=str(resolve_path(s["tokens"])),
        num_threads=s.get("num_threads", 4), language="zh", use_itn=True)
    rows, err, n = [], 0, 0
    for r in script:
        f = Path(out) / "raw" / f"{r['id']}.wav"
        if not r["asr"] or not f.exists():
            continue
        st = rec.create_stream()
        st.accept_waveform(16000, load_mono(f, 16000))
        rec.decode_stream(st)
        hyp = st.result.text.strip()
        ref_n, hyp_n = _norm(r["text"]), _norm(hyp)
        e = _edits(ref_n, hyp_n)
        err, n = err + e, n + len(ref_n)
        rows.append({"id": r["id"], "text": r["text"], "heard": hyp, "errors": e})
    res = {"sentences": len(rows), "chars": n, "errors": err, "cer": round(err / n, 4) if n else None, "rows": rows}
    ds = Path(out) / "dataset"
    ds.mkdir(parents=True, exist_ok=True)
    _write_json(ds / "asr_test.json", res)
    return res


def coverage(script: list[dict]) -> None:
    """稿子的拼音覆盖. 参照 = GB2312 一级字 3755 个的常用读音; GPT-SoVITS 的中文音素是声母 + 带调韵母."""
    from collections import Counter

    from pypinyin import Style, lazy_pinyin

    zh = [HAN.findall(r["text"]) for r in script if r["lang"] == "ZH"]
    zh = ["".join(c) for c in zh]
    ref_chars = [bytes([hi, lo]).decode("gb2312") for hi in range(0xB0, 0xD8) for lo in range(0xA1, 0xFF)
                 if not (hi == 0xD7 and lo > 0xF9)]

    def table(style, texts, strict=True):
        c = Counter()
        for t in texts:
            for x in lazy_pinyin(t, style=style, strict=strict, neutral_tone_with_five=True, errors="ignore"):
                if x:
                    c[x] += 1
        return c

    syl_ref, syl = table(Style.NORMAL, ref_chars), table(Style.NORMAL, zh)
    ft_ref, ft = table(Style.FINALS_TONE3, ref_chars), table(Style.FINALS_TONE3, zh)
    tones = Counter(x[-1] for x in table(Style.TONE3, zh).elements() if x[-1].isdigit())
    chars = sum(len(t) for t in zh)
    print(f"中文 {len(zh)} 句, {chars} 字 (平常语速大约 {chars / 4.6 / 60:.0f} 分钟)")
    print(f"音节: {sum(1 for s in syl_ref if s in syl)} / {len(syl_ref)} 个常用读音, "
          f"按常用字加权 {sum(v for s, v in syl_ref.items() if s in syl) / sum(syl_ref.values()):.1%}")
    print(f"带调韵母: {sum(1 for s in ft_ref if s in ft)} / {len(ft_ref)}; 缺 {sorted(s for s in ft_ref if s not in ft)}")
    print("声调 (5 = 轻声):", dict(sorted(tones.items())))
    print("缺的音节:", " ".join(sorted((s for s in syl_ref if s not in syl), key=lambda s: -syl_ref[s])) or "无")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", nargs="?", default="serve", choices=["serve", "prepare", "asr-test", "coverage"])
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="录音放哪 (默认 Bot用/TTS素材/我的声音)")
    ap.add_argument("--script", default=str(SCRIPT))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--speaker", default="myvoice", help="prepare: .list 里的说话人名, 也是训练时的 --name")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    script, out = load_script(Path(args.script)), Path(args.out)

    if args.cmd == "coverage":
        coverage(script)
    elif args.cmd == "prepare":
        res = prepare(out, script, args.speaker)
        print(f"训练集: {res['lines']} 句, 共 {res['seconds'] / 60:.1f} 分钟 → {res['list']}")
        for rid, emo, sec in res["refs"]:
            warn = "" if 3.0 <= sec <= 10.0 else "  (参考音要 3–10 秒, 这句不合适)"
            print(f"  参考音 {rid} {emo} {sec} 秒{warn}")
        for rid, why in res["skipped"]:
            print(f"  没收 {rid}: {'; '.join(why)}")
        print("下一步 (训练前先停掉 9880 的语音服务和本机模型, 把显卡让出来):")
        print(f'  python tools/train_gsv.py "{res["list"]}" --name {args.speaker} --out models/tts/{args.speaker}')
    elif args.cmd == "asr-test":
        res = asr_test(out, script)
        if not res["sentences"]:
            print("考卷那些句子还没录.")
            return
        for row in res["rows"]:
            if row["errors"]:
                print(f"  {row['id']} 错 {row['errors']} 字\n     稿子: {row['text']}\n     听成: {row['heard']}")
        print(f"考卷 {res['sentences']} 句 {res['chars']} 字, 错 {res['errors']} 字, 字错率 {res['cer']:.1%}"
              " (低于 5% 一般不用训识别)")
    else:
        from aiohttp import web

        url = f"http://127.0.0.1:{args.port}/"
        print(f"录音页: {url}  (录音存到 {out.resolve()}; Ctrl+C 退出)")
        if not args.no_browser:
            import webbrowser

            webbrowser.open(url)
        web.run_app(make_app(out, script), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()

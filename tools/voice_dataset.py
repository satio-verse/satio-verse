"""把一个角色的语音素材切成 GPT-SoVITS 训练集: silero VAD 切段 → 相邻短段合并 → SenseVoice 转写 → 32 kHz 单声道 WAV + .list.

  python tools/voice_dataset.py "Bot用/TTS素材/爱弥斯" models/tts/aimisi/dataset --speaker aimisi

输出 <out>/wavs/<文件名>_NN.wav 和 <out>/<speaker>.list (每行 "wav 绝对路径|说话人|ZH|文本", GPT-SoVITS 的格式).
转写只是初稿: 专有名词常错, 训练前手改 .list 里的文本. 同音字错不影响训练 (GPT-SoVITS 按音素学), 要改的是读音不同的错.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wm.audio import to_int16  # noqa: E402
from wm.config import load_config, resolve_path  # noqa: E402

VAD_SR = 16000
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}


def load_mono(path: Path, sr: int) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def speech_spans(x16: np.ndarray, vad_cfg) -> list[tuple[float, float]]:
    import sherpa_onnx

    vad = sherpa_onnx.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=max(30, len(x16) / VAD_SR + 5))
    for i in range(0, len(x16), 512):
        vad.accept_waveform(x16[i:i + 512])
    vad.flush()
    spans = []
    while not vad.empty():
        s = vad.front
        spans.append((s.start / VAD_SR, (s.start + len(s.samples)) / VAD_SR))
        vad.pop()
    return spans


def merge(spans: list[tuple[float, float]], gap: float, max_s: float) -> list[tuple[float, float]]:
    """停顿短于 gap 的相邻段算同一句, 合并后不超过 max_s."""
    out: list[list[float]] = []
    for a, b in spans:
        if out and a - out[-1][1] < gap and b - out[-1][0] <= max_s:
            out[-1][1] = b
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def write_wav(path: Path, pcm: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(to_int16(pcm).tobytes())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", help="素材目录")
    ap.add_argument("out", help="输出目录")
    ap.add_argument("--speaker", default="voice")
    ap.add_argument("--sr", type=int, default=32000, help="输出采样率 (GPT-SoVITS v2/v2Pro 用 32 kHz)")
    ap.add_argument("--gap", type=float, default=0.45, help="停顿短于这个秒数就和上一段合并")
    ap.add_argument("--max-s", type=float, default=10.0, help="合并后单段最长秒数")
    ap.add_argument("--min-s", type=float, default=0.6, help="短于这个秒数的段丢掉")
    ap.add_argument("--pad", type=float, default=0.12, help="每段前后各留的秒数 (不越过相邻段的中点)")
    ap.add_argument("--peak", type=float, default=0.9, help="每段峰值归一到这个幅度")
    args = ap.parse_args()

    import sherpa_onnx

    s = load_config()["asr"]["vad_sensevoice"]
    rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(resolve_path(s["model"])), tokens=str(resolve_path(s["tokens"])),
        num_threads=s.get("num_threads", 4), language="zh", use_itn=True)
    vad_cfg = sherpa_onnx.VadModelConfig(
        silero_vad=sherpa_onnx.SileroVadModelConfig(
            model=str(resolve_path(s["vad_model"])), threshold=0.5, min_silence_duration=0.35,
            min_speech_duration=0.25, window_size=512),
        sample_rate=VAD_SR, num_threads=1)

    src, out = resolve_path(args.src), resolve_path(args.out)
    (out / "wavs").mkdir(parents=True, exist_ok=True)
    lines, kept, dropped = [], 0.0, 0
    for f in sorted((p for p in src.iterdir() if p.suffix.lower() in AUDIO_EXT), key=lambda p: p.name.lower()):
        x, x16 = load_mono(f, args.sr), load_mono(f, VAD_SR)
        spans = merge(speech_spans(x16, vad_cfg), args.gap, args.max_s)
        dur = len(x) / args.sr
        for i, (a, b) in enumerate(spans):
            if b - a < args.min_s:
                dropped += 1
                continue
            lo = max(a - args.pad, (spans[i - 1][1] + a) / 2 if i else 0.0)
            hi = min(b + args.pad, (b + spans[i + 1][0]) / 2 if i + 1 < len(spans) else dur)
            clip = x[int(lo * args.sr):int(hi * args.sr)]
            stream = rec.create_stream()
            stream.accept_waveform(VAD_SR, x16[int(lo * VAD_SR):int(hi * VAD_SR)])  # 给 16 kHz, 免得 sherpa 每段都建重采样器
            rec.decode_stream(stream)
            text = stream.result.text.strip()
            if not text:
                dropped += 1
                continue
            clip = clip / (float(np.abs(clip).max()) or 1.0) * args.peak
            wav = out / "wavs" / f"{f.stem}_{len(lines):02d}.wav"
            write_wav(wav, clip, args.sr)
            lines.append(f"{wav.resolve()}|{args.speaker}|ZH|{text}")
            kept += len(clip) / args.sr
            print(f"{wav.name}  {len(clip) / args.sr:5.2f}s  {text}")
    (out / f"{args.speaker}.list").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(lines)} 段, 共 {kept:.1f} s; 丢掉 {dropped} 段 (太短或没识别出字). 列表: {out / (args.speaker + '.list')}")


if __name__ == "__main__":
    main()

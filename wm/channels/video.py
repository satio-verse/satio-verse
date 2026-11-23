"""QQ 里发来的视频: 用本机已经装好的 ffmpeg "打开" (不联网、不装新包), 抽出几帧画面、把音轨转成 PCM 给 ASR.

和 B 站那边 (bili.py 的 snapshot / media.contact_sheet) 一样, 只把抽好的画面和转出来的话交给访客会话;
原视频只写到临时目录, 用完就删, 不留在库里, 也不发给云端. 打不开 (没装 ffmpeg、文件坏了、没有音轨也没有画面)
就抛 VideoError, 由管理器照旧记一次"看不了视频"的麻烦.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .media import is_black

VIDEO_EXT = (".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv", ".wmv", ".mts", ".3gp", ".mpg", ".mpeg")
FRAME_W = 360  # 每帧缩到这个宽度, 3 列拼一张 (和 B 站 6 帧截图差不多大, 云端看得清又不贵)
FRAMES = 6


class VideoError(RuntimeError):
    """视频没打开: 没装 ffmpeg / ffprobe、文件不是视频、抽不出画面也听不到声音."""


@dataclass
class OpenedVideo:
    seconds: int                 # 视频总长 (秒); 超过上限的只处理前 max_s 秒
    frames: bytes | None         # 按时间顺序的几帧拼成的一张 JPEG
    pcm: np.ndarray | None       # 16 kHz 单声道 int16 的音轨 (没有音轨就是 None)


def ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe() -> str | None:
    return shutil.which("ffprobe")


def available() -> bool:
    """本机能不能打开视频 (有 ffmpeg 就行; 读时长没有 ffprobe 时从 ffmpeg 的输出里认)."""
    return bool(ffmpeg())


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    """跑一次 ffmpeg / ffprobe, 输出都在内存里. 起不来、超时抛 VideoError."""
    try:
        return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise VideoError(f"{Path(args[0]).name} 跑不起来: {exc}") from None


def _why(p: subprocess.CompletedProcess) -> str:
    return p.stderr.decode("utf-8", "replace").strip()[-200:] or f"退出码 {p.returncode}"


def _probe_with_ffmpeg(path: Path, timeout: float) -> tuple[float, int, int]:
    """没有 ffprobe 时从 ffmpeg 的 -i 输出里认时长和画面大小."""
    exe = ffmpeg()
    if not exe:
        raise VideoError("没装 ffmpeg")
    p = _run([exe, "-hide_banner", "-i", str(path)], timeout)
    text = p.stderr.decode("utf-8", "replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not m:
        raise VideoError(f"认不出视频时长: {_why(p)}")
    seconds = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    size = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", text)
    return seconds, int(size.group(1)) if size else 0, int(size.group(2)) if size else 0


def probe(path: Path, timeout: float = 20) -> tuple[float, int, int]:
    """视频 -> (秒数, 宽, 高). 读不出来抛 VideoError."""
    exe = ffprobe()
    if not exe:
        return _probe_with_ffmpeg(path, timeout)
    p = _run([exe, "-v", "error", "-show_entries", "format=duration:stream=width,height", "-of", "json", str(path)],
             timeout)
    if p.returncode != 0:
        raise VideoError(f"读不了视频: {_why(p)}")
    try:
        data = json.loads(p.stdout.decode("utf-8", "replace"))
    except ValueError:
        raise VideoError("ffprobe 的输出不是 JSON") from None
    seconds = float((data.get("format") or {}).get("duration") or 0)
    if seconds <= 0:
        seconds, _, _ = _probe_with_ffmpeg(path, timeout)
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    video = next((s for s in streams if int(s.get("width") or 0) and int(s.get("height") or 0)), {})
    return seconds, int(video.get("width") or 0), int(video.get("height") or 0)


def _tile(frames: list[bytes], cols: int = 3) -> bytes | None:
    """几帧 JPEG 拼成一张 (3 列): 每帧缩到 FRAME_W 宽, 空位涂黑. 全黑的帧跳过; 一帧能看的都没有返回 None."""
    from PIL import Image

    ims = []
    for raw in frames:
        try:
            with Image.open(io.BytesIO(raw)) as im:
                im = im.convert("RGB")
                if is_black(im):  # 片头片尾的黑场、还没加载出来的帧: 先别拼进给模型看的图
                    continue
                im.thumbnail((FRAME_W, FRAME_W))
                ims.append(im.copy())
        except (OSError, ValueError):
            continue
    if not ims:
        return None
    w, h = max(im.width for im in ims), max(im.height for im in ims)
    rows = (len(ims) + cols - 1) // cols
    out = Image.new("RGB", (cols * w, rows * h), (32, 32, 32))  # 空位用灰的, 别让模型把黑边当成画面
    for i, im in enumerate(ims):
        out.paste(im, ((i % cols) * w, (i // cols) * h))
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def frames(path: Path, seconds: float, n: int = FRAMES, timeout: float = 20) -> bytes | None:
    """在 5%–85% 的时间里均匀挑 n 帧 (片头片尾常是黑的), 拼成一张 JPEG. 一帧也抽不出返回 None."""
    exe = ffmpeg()
    if not exe:
        raise VideoError("没装 ffmpeg")
    if seconds <= 0:
        times = [0.0]
    else:
        times = [seconds * (0.05 + 0.8 * i / max(1, n - 1)) for i in range(n)]
    out = []
    for t in times:
        p = _run([exe, "-v", "error", "-i", str(path), "-ss", f"{t:.3f}", "-frames:v", "1", "-an", "-sn",
                  "-vf", f"scale='min({FRAME_W},iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"], timeout)
        if p.returncode == 0 and p.stdout:
            out.append(p.stdout)
    if not out and times != [0.0]:  # 很短或者 seek 到末尾抽不出: 从开头再试一帧
        p = _run([exe, "-v", "error", "-i", str(path), "-frames:v", "1", "-an", "-sn", "-f", "image2pipe",
                  "-vcodec", "mjpeg", "-"], timeout)
        if p.returncode == 0 and p.stdout:
            out.append(p.stdout)
    return _tile(out)


def sound(path: Path, max_s: float = 180, timeout: float = 30) -> np.ndarray | None:
    """音轨 -> 16 kHz 单声道 int16 PCM (最多 max_s 秒). 没有音轨、转不出来返回 None."""
    exe = ffmpeg()
    if not exe:
        raise VideoError("没装 ffmpeg")
    p = _run([exe, "-v", "error", "-i", str(path), "-t", f"{max(1, int(max_s))}", "-vn", "-ac", "1", "-ar", "16000",
              "-f", "s16le", "-"], timeout)
    if p.returncode != 0 or not p.stdout:
        return None
    pcm = np.frombuffer(p.stdout, dtype="<i2")
    return pcm.copy() if len(pcm) else None


def sound_from_bytes(raw: bytes, max_s: float = 180, timeout: float = 60) -> np.ndarray | None:
    """下载好的音频字节 (mp3 / m4a / wav…) -> 16 kHz 单声道 int16 PCM; 转不出来返回 None. 只放临时目录, 用完就删."""
    with tempfile.TemporaryDirectory(prefix="wm-audio-") as d:
        path = Path(d) / "audio"
        path.write_bytes(raw)
        return sound(path, max_s, timeout)


def open_video(path: Path, n: int = FRAMES, max_s: float = 180, timeout: float = 20) -> OpenedVideo:
    """打开一个视频文件: 读时长、抽 n 帧、把前 max_s 秒的音轨转成 PCM. 画面和音轨都拿不到抛 VideoError."""
    seconds, _w, _h = probe(path, timeout)
    jpeg = frames(path, seconds, n, timeout)
    pcm = sound(path, max_s, max(timeout, 30))
    if jpeg is None and pcm is None:
        raise VideoError("视频里既抽不出画面, 也没有能听的音轨")
    return OpenedVideo(seconds=max(0, int(round(seconds))), frames=jpeg, pcm=pcm)


def open_and_read(raw: bytes, n: int = FRAMES, max_s: float = 180, timeout: float = 20) -> OpenedVideo:
    """下载好的视频字节 -> OpenedVideo; 只放在临时目录里, 用完就删."""
    with tempfile.TemporaryDirectory(prefix="wm-video-") as d:
        path = Path(d) / "video"
        path.write_bytes(raw)
        return open_video(path, n, max_s, timeout)

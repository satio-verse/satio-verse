"""外部渠道里的图片、表情、卡片: 下载 (只从认得的域名、有大小上限)、缩小转成 data: URL 给看得了图的云端模型,
QQ 系统表情的编号和名字, 音乐 / 小程序卡片读成一句话, B 站视频的截图拼成一张.

图片都是外部内容, 和同一个人发的文字同一档: 交给同一个访客会话 (或隔离读者), 不进主人的会话.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import warnings
from urllib.parse import urlsplit

import aiohttp

from .outbound import clean_in

IMAGE_MAX_BYTES = 32 * 1024 * 1024  # 和 QQ 文件、桌宠附件一样大: 手机原图、长截图常常比 5 MB 大
IMAGE_SIDE = 1024  # 长边缩到这么多再发 (DeepSeek 每张最多算约 1024 token, 再大也不多看)
IMAGE_TIMEOUT_S = 60  # 大图下载、转码单独给时间, 不跟文字文件的 15 秒挤在一起
IMAGE_MAX_PIXELS = 100_000_000  # 像素太多的图在缩放前就可能把内存吃光: 明确说打不开, 不硬解、不卡住这一轮
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")  # 当文件发来的图也按图片看, 不落到文档解析
BLACK_LEVEL = 28  # 每个颜色通道最亮都不超过这个、平均又很低时, 就是整张几乎全黑
BLACK_MEAN = 4
QQ_HOSTS = (".qq.com", ".qq.com.cn", ".qpic.cn", ".qlogo.cn")

# QQ 系统表情 (face 段的 id). 只收常用、确定的; 发出去不再用它们, 改用她收藏里的表情 (manager._send)
FACES = {
    0: "惊讶", 1: "撇嘴", 2: "色", 3: "发呆", 4: "得意", 5: "流泪", 6: "害羞", 7: "闭嘴", 8: "睡", 9: "大哭", 10: "尴尬",
    11: "发怒", 12: "调皮", 13: "呲牙", 14: "微笑", 15: "难过", 16: "酷", 18: "抓狂", 19: "吐", 20: "偷笑", 21: "可爱",
    22: "白眼", 23: "傲慢", 24: "饥饿", 25: "困", 26: "惊恐", 27: "流汗", 28: "憨笑", 29: "悠闲", 30: "奋斗", 32: "疑问",
    33: "嘘", 34: "晕", 36: "衰", 38: "敲打", 39: "再见", 41: "发抖", 42: "爱情", 43: "跳跳", 49: "拥抱", 53: "蛋糕",
    60: "咖啡", 63: "玫瑰", 64: "凋谢", 66: "爱心", 67: "心碎", 74: "太阳", 75: "月亮", 76: "赞", 77: "踩", 78: "握手",
    79: "胜利", 85: "飞吻", 89: "西瓜", 96: "冷汗", 97: "擦汗", 98: "抠鼻", 99: "鼓掌", 100: "糗大了", 101: "坏笑",
    104: "哈欠", 105: "鄙视", 106: "委屈", 107: "快哭了", 108: "阴险", 109: "亲亲", 110: "吓", 111: "可怜", 116: "示爱",
    118: "抱拳", 120: "拳头", 122: "爱你", 123: "NO", 124: "OK", 125: "转圈", 129: "挥手", 144: "喝彩", 147: "棒棒糖",
    171: "茶", 173: "泪奔", 174: "无奈", 175: "卖萌", 176: "小纠结", 178: "斜眼笑", 179: "doge", 180: "惊喜",
    182: "笑哭", 183: "我最美", 185: "羊驼", 187: "幽灵", 201: "点赞", 212: "托腮", 214: "啵啵", 216: "拍头",
    222: "抱抱", 227: "拍手", 264: "捂脸", 265: "辣眼睛", 266: "哦哟", 267: "头秃", 268: "问号脸", 269: "暗中观察",
    270: "emm", 271: "吃瓜", 272: "呵呵哒", 277: "汪汪", 281: "无眼笑", 282: "敬礼", 285: "摸鱼", 289: "睁眼",
    293: "摸锦鲤", 294: "期待", 297: "拜谢", 299: "牛啊", 306: "牛气冲天", 307: "喵喵", 311: "打call", 315: "加油",
    316: "我没事", 318: "崇拜", 319: "比心", 320: "庆祝", 322: "拒绝", 323: "嫌弃",
}


class MediaError(RuntimeError):
    """图片 / 文件没拿到: 域名不认、太大、不是图片、网络."""


class BlackFrame(MediaError):
    """画面几乎是全黑的: 截图没截到、屏幕还没亮、片头黑场. 先别交给模型分析."""


def is_black(im) -> bool:
    """整张几乎全黑? 带一点 JPEG 噪点也算. 全黑的画面先别分析, 等画面出来再截/换一帧."""
    gray = im.convert("L")
    hi = max(ch.getextrema()[1] for ch in im.convert("RGB").split())
    if hi <= BLACK_LEVEL:
        return True
    if hi <= 40:  # 黑屏的压缩噪点: 最亮的不高, 平均也很暗
        hist = gray.histogram()
        total = sum(hist)
        return bool(total) and sum(i * n for i, n in enumerate(hist)) / total <= BLACK_MEAN
    return False


def host_ok(url: str, hosts: tuple[str, ...]) -> bool:
    u = urlsplit(str(url or ""))
    h = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and any(h == x.lstrip(".") or h.endswith(x) for x in hosts)


def shrink_url(url: str) -> str | None:
    """QQ 图片地址里 spec=0 是原图; 换成 spec=198 (缩略图) 再取, 大图也能拿到.
    不是这种地址、或者本来就是小的, 返回 None (调用方照原样报错)."""
    if "spec=" not in url:
        return None
    small = re.sub(r"spec=\d+", "spec=198", url)
    return small if small != url else None


async def fetch(session: aiohttp.ClientSession, url: str, hosts: tuple[str, ...], max_bytes: int,
                headers: dict | None = None, timeout: float | None = None) -> bytes:
    """GET 一个文件: 只从 hosts 里的域名, 不跟随跳转, 超过 max_bytes 就停. timeout: 这个请求单独的超时 (视频大一点)."""
    if not host_ok(url, hosts):
        raise MediaError("地址不在认得的域名里")
    try:
        kwargs = {"timeout": aiohttp.ClientTimeout(total=timeout)} if timeout else {}
        async with session.get(url, headers=headers or {}, allow_redirects=False, **kwargs) as resp:
            if resp.status != 200:
                raise MediaError(f"HTTP {resp.status}")
            length = str(resp.headers.get("Content-Length") or "")
            if length.isdigit() and int(length) > max_bytes:  # 先看声明的大小, 别把几百 MB 真的拉下来
                raise MediaError("太大了")
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise MediaError("太大了")
                chunks.append(chunk)
            return b"".join(chunks)
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as exc:  # aiohttp 的读超时在 3.10 是 asyncio.TimeoutError
        raise MediaError(f"{type(exc).__name__}: {exc}") from None


async def fetch_prefix(session: aiohttp.ClientSession, url: str, hosts: tuple[str, ...], max_bytes: int,
                       headers: dict | None = None, timeout: float | None = None) -> bytes:
    """GET 一个文件的开头: 文件比 max_bytes 大也不报错, 读够就停 (Range 不认也照样在本地截断).
    给太大的文字文件分块读用; 只从 hosts 里的域名取, 不跟随跳转."""
    if not host_ok(url, hosts):
        raise MediaError("地址不在认得的域名里")
    if max_bytes <= 0:
        return b""
    try:
        kwargs = {"timeout": aiohttp.ClientTimeout(total=timeout)} if timeout else {}
        wanted = dict(headers or {})
        wanted.setdefault("Range", f"bytes=0-{max_bytes - 1}")
        async with session.get(url, headers=wanted, allow_redirects=False, **kwargs) as resp:
            if resp.status not in (200, 206):
                raise MediaError(f"HTTP {resp.status}")
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                room = max_bytes - size
                if room <= 0:
                    break
                chunks.append(chunk[:room])
                size += min(len(chunk), room)
                if size >= max_bytes:
                    break
            return b"".join(chunks)
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as exc:  # aiohttp 的读超时在 3.10 是 asyncio.TimeoutError
        raise MediaError(f"{type(exc).__name__}: {exc}") from None


async def fetch_to_path(session: aiohttp.ClientSession, url: str, hosts: tuple[str, ...], path,
                        max_bytes: int, headers: dict | None = None, timeout: float | None = None) -> int:
    """GET 一个文件并分块写到 path: 只从 hosts 里的域名, 不跟随跳转, 超过 max_bytes 就停下报错.
    给超过单次 32 MB 的大文件用: 服务端内存里只留一块 64 KB, 解析时再从临时文件读."""
    if not host_ok(url, hosts):
        raise MediaError("地址不在认得的域名里")
    try:
        kwargs = {"timeout": aiohttp.ClientTimeout(total=timeout)} if timeout else {}
        async with session.get(url, headers=headers or {}, allow_redirects=False, **kwargs) as resp:
            if resp.status != 200:
                raise MediaError(f"HTTP {resp.status}")
            length = str(resp.headers.get("Content-Length") or "")
            if length.isdigit() and int(length) > max_bytes:
                raise MediaError("太大了")
            size = 0
            with open(path, "wb") as stream:
                async for chunk in resp.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > max_bytes:
                        raise MediaError("太大了")
                    stream.write(chunk)
            return size
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as exc:  # aiohttp 的读超时在 3.10 是 asyncio.TimeoutError
        raise MediaError(f"{type(exc).__name__}: {exc}") from None
    except OSError as exc:
        raise MediaError(f"写临时文件失败: {exc}") from None


def sniff(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _animation_data_url(im, side: int, count: int = 6) -> str:
    """动图 (GIF / 动态 WebP / APNG) 采样成一张带时间标记的网格图: 每格左上角写这一帧是第几秒.
    dsh-read-animation 那个插件的思路: 模型看不到动画, 就先把时间轴摊开给它看."""
    import base64

    from PIL import Image, ImageDraw, ImageFont

    total = int(getattr(im, "n_frames", 1) or 1)
    starts, acc = [], 0.0
    for i in range(total):
        im.seek(i)
        starts.append(acc)
        acc += float(im.info.get("duration") or 100)
    count = max(2, min(count, total))
    picks = []
    for k in range(count):
        target = acc * k / count
        idx = 0
        for i in range(total):
            idx = i
            if starts[i] >= target:
                break
        picks.append(idx)
    shots = []
    for i in picks:
        im.seek(i)
        frame = im.convert("RGB")
        if is_black(frame):  # 黑场、还在加载的帧不拼进去, 免得模型去分析一片黑
            continue
        shots.append((i, frame))
    if not shots:
        raise BlackFrame("动图的每一帧都是黑的 (可能没截到、还没加载), 先别分析")
    count = len(shots)
    cols = 3 if count > 2 else count
    rows = (count + cols - 1) // cols
    cell = max(64, side // cols)
    sheet = Image.new("RGB", (cell * cols, cell * rows), (16, 16, 20))
    try:
        font = ImageFont.truetype("arial.ttf", max(12, cell // 18))
    except OSError:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(sheet)
    for n, (i, frame) in enumerate(shots):
        frame.thumbnail((cell, cell))
        x, y = (n % cols) * cell, (n // cols) * cell
        sheet.paste(frame, ((cell - frame.width) // 2 + x, (cell - frame.height) // 2 + y))
        draw.rectangle([x, y, x + 48, y + 20], fill=(0, 0, 0))
        draw.text((x + 4, y + 4), "%.1fs" % (starts[i] / 1000.0), fill=(255, 226, 120), font=font)
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def to_data_url(data: bytes, side: int = IMAGE_SIDE) -> str:
    """图片 -> 缩到长边 side 的 JPEG data: URL. 不是图片抛 MediaError, 整张几乎全黑抛 BlackFrame (先别分析).
    动图 (GIF / 动态 WebP) 采样成一张带时间标记的拼图, 不是只取第一帧 (2026-10-30 主人转的 dsh-read-animation 思路).
    JPEG 先让解码器按 draft 降采样 (大照片不用先解成整张); 像素超过上限的明确报错, 别让一张图卡住整轮."""
    import base64

    from PIL import Image

    if sniff(data) is None:
        raise MediaError("不是图片")
    try:
        with warnings.catch_warnings():  # 上限由下面自己的像素数判断, 不用 Pillow 的警告刷日志
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:
                im.seek(0)
                if getattr(im, "format", "") == "JPEG":
                    im.draft("RGB", (side, side))
                if im.width * im.height > IMAGE_MAX_PIXELS:
                    raise MediaError(f"图片太大 ({im.width}×{im.height})")
                if int(getattr(im, "n_frames", 1) or 1) > 1:
                    return _animation_data_url(im, side)
                im = im.convert("RGB")
                if is_black(im):  # 截图没截到 / 屏幕还没亮: 先别让模型分析黑屏
                    raise BlackFrame("画面是黑的 (可能没截到、屏幕还没亮), 先别分析")
                im.thumbnail((side, side))
                out = io.BytesIO()
                im.save(out, "JPEG", quality=85)
    except MediaError:
        raise
    except (OSError, ValueError, MemoryError, Image.DecompressionBombError) as exc:
        raise MediaError(f"图片打不开: {exc}") from None
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def contact_sheet(sprites: dict[int, bytes], picks: list[tuple[int, int]], x_len: int, w: int, h: int,
                  cols: int = 3) -> bytes | None:
    """B 站 videoshot 的拼图 (一张里 x_len 列、每格 w×h) 里按时间挑几帧, 拼成 cols 列的一张 JPEG (小于 320 宽的帧放大一倍).
    picks: [(第几张拼图, 拼图里第几格)]. 全黑的帧 (片头黑场、还没加载) 跳过; 一帧能看的都没有就返回 None."""
    from PIL import Image

    k2 = 2 if w < 320 else 1
    frames = []
    for sheet, k in picks:
        with Image.open(io.BytesIO(sprites[sheet])) as im:
            x, y = (k % x_len) * w, (k // x_len) * h
            frame = im.convert("RGB").crop((x, y, x + w, y + h)).resize((w * k2, h * k2))
            if is_black(frame):
                continue
            frames.append(frame)
    if not frames:
        return None
    rows = (len(frames) + cols - 1) // cols
    out = Image.new("RGB", (cols * w * k2, rows * h * k2), (32, 32, 32))  # 空位用灰的, 别让模型把黑边当成画面
    for i, f in enumerate(frames):
        out.paste(f, ((i % cols) * w * k2, (i // cols) * h * k2))
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def card_text(raw) -> str:
    """json 段 (音乐分享、小程序、链接卡片) -> 一句话; 认不出来就是"卡片"."""
    try:
        card = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return "［卡片］"
    if not isinstance(card, dict):
        return "［卡片］"
    meta = card.get("meta") or {}
    for key, what in (("music", "分享了音乐"), ("news", "分享了链接"), ("detail_1", "分享了小程序"), ("video", "分享了视频")):
        m = meta.get(key)
        if isinstance(m, dict):
            title = clean_in(m.get("title") or "", 40)
            desc = clean_in(m.get("desc") or "", 40)
            tag = clean_in(m.get("tag") or "", 12)
            body = "，".join(x for x in (f"《{title}》" if title else "", desc) if x)
            return f"［{what}{('：' + body) if body else ''}{f'（{tag}）' if tag else ''}］"
    prompt = clean_in(card.get("prompt") or "", 40)
    return f"［卡片：{prompt}］" if prompt else "［卡片］"


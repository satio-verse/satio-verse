"""动图 (GIF / 动态 WebP): 采样成带时间标记的拼图, 不是只取第一帧 (照 dsh-read-animation 的思路).
截屏、看图的画面几乎是全黑时先别交给模型分析: 黑帧跳过, 全黑就拒绝 (2026-11-05)."""
import base64
import io

import pytest
from PIL import Image

from wm.channels.media import BlackFrame, MediaError, contact_sheet, is_black, to_data_url
from wm.channels.video import _tile


def _decode(url: str) -> Image.Image:
    assert url.startswith("data:image/jpeg;base64,"), url[:40]
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))


def _jpeg(color, size=(80, 60)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _gif(frames=8, size=(80, 60), duration=120):
    ims = [Image.new("RGB", size, ((i * 30) % 255, 80, max(0, 220 - i * 25))) for i in range(frames)]
    buf = io.BytesIO()
    ims[0].save(buf, "GIF", save_all=True, append_images=ims[1:], duration=duration, loop=0)
    return buf.getvalue()


def test_gif_becomes_six_frame_sheet():
    """一张 6 格的拼图 (3 列 2 行), 每格一格画面 + 时间标记."""
    with _decode(to_data_url(_gif(), side=600)) as im:
        assert im.size == (600, 400), im.size  # 3 列 × 200, 2 行 × 200


def test_still_png_keeps_old_behaviour():
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (10, 20, 30)).save(buf, "PNG")
    with _decode(to_data_url(buf.getvalue(), side=200)) as im:
        assert im.size == (200, 133), im.size


def test_two_frame_animation_is_two_cells():
    with _decode(to_data_url(_gif(frames=2), side=300)) as im:
        assert im.size == (300, 150), im.size  # 2 格并排


def test_broken_image_still_raises():
    try:
        to_data_url(b"not really an image")
    except MediaError:
        return
    raise AssertionError("坏图应该抛 MediaError")


def test_black_still_is_refused_before_analysis():
    """截屏/看图前先确认: 整张几乎全黑的画面不给模型分析, 抛 BlackFrame 让调用方写清楚."""
    with pytest.raises(BlackFrame) as err:
        to_data_url(_jpeg((0, 0, 0)))
    assert "黑" in str(err.value) and "先别分析" in str(err.value)


def test_dark_but_not_black_image_is_kept():
    """很暗但有内容的照片不算全黑, 照旧交给模型 (别把夜里拍的图也当成没截到)."""
    assert not is_black(Image.new("RGB", (40, 30), (10, 20, 30)))
    assert _decode(to_data_url(_jpeg((10, 20, 30)))).width > 0


def test_animation_skips_black_frames():
    frames = [_jpeg((0, 0, 0)), _jpeg((220, 40, 40)), _jpeg((0, 0, 0)), _jpeg((40, 160, 220))]
    buf = io.BytesIO()
    ims = [Image.open(io.BytesIO(raw)).convert("RGB") for raw in frames]
    ims[0].save(buf, "GIF", save_all=True, append_images=ims[1:], duration=120, loop=0)
    with _decode(to_data_url(buf.getvalue(), side=300)) as im:
        assert im.size == (300, 150)  # 只留两帧能看的, 2 列 1 行


def test_all_black_animation_is_refused():
    frames = [Image.new("RGB", (40, 30), (0, 0, 0)) for _ in range(3)]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=120, loop=0)
    with pytest.raises(BlackFrame):
        to_data_url(buf.getvalue())


def test_contact_sheet_skips_black_cells():
    """B 站截图拼图里有黑帧 (片头黑场) 就跳过; 四格都黑时不拼一张给模型看."""
    sheet = io.BytesIO()
    im = Image.new("RGB", (64, 36), (0, 0, 0))
    im.paste(Image.new("RGB", (32, 18), (200, 120, 40)), (0, 0))
    im.paste(Image.new("RGB", (32, 18), (40, 120, 200)), (32, 18))
    im.save(sheet, "JPEG")
    sprites = {0: sheet.getvalue()}
    out = contact_sheet(sprites, [(0, 0), (0, 1), (0, 2), (0, 3)], 2, 32, 18)
    assert out
    with Image.open(io.BytesIO(out)) as got:
        assert not is_black(got)

    all_black = io.BytesIO()
    Image.new("RGB", (64, 36), (0, 0, 0)).save(all_black, "JPEG")
    assert contact_sheet({0: all_black.getvalue()}, [(0, 0), (0, 1), (0, 2), (0, 3)], 2, 32, 18) is None


def test_video_frames_skip_black():
    """QQ 视频抽到的黑帧不拼进给模型看的一张; 六帧都黑就当作没有画面."""
    out = _tile([_jpeg((0, 0, 0)), _jpeg((200, 120, 40))])
    assert out and not is_black(Image.open(io.BytesIO(out)))
    assert _tile([_jpeg((0, 0, 0)), _jpeg((2, 2, 2))]) is None

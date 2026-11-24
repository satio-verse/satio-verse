"""生成官网静态资产：logo、favicon、分享图、产品截图（webp）。

源素材在本机（不进仓库）：
  logo   C:\\Users\\ASUS\\.dsh\\attachments\\v1\\objects\\3d\\3d6931f2...png
  shots  E:\\Ayane_Use\\_gallery\\satio2\\*.png
用法：python tools/build_site_assets.py
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "assets"
OUT.mkdir(parents=True, exist_ok=True)

LOGO = Path(r"C:\Users\ASUS\.dsh\attachments\v1\objects\3d\3d6931f29c7ba99779952e4cf4410eb82228a10e1d5a80a4f3f9f391627f60e3")
SHOTS = {
    "room.webp": r"E:\Ayane_Use\_gallery\satio2\04-dock.png",
    "panopticon.webp": r"E:\Ayane_Use\_gallery\satio2\11-panopticon.png",
    "nexus.webp": r"E:\Ayane_Use\_gallery\satio2\12-assets-nexus.png",
}

FONT_LIGHT = r"C:\Windows\Fonts\segoeuil.ttf"
FONT_CN = r"C:\Windows\Fonts\msyh.ttc"
FONT_MONO = r"C:\Windows\Fonts\consola.ttf"

INK = (242, 239, 233)
INK2 = (154, 149, 140)


def save_logo():
    img = Image.open(LOGO).convert("RGB")
    img.save(OUT / "logo.png", "PNG", optimize=True)
    for size, name in ((192, "icon-192.png"), (180, "apple-touch-icon.png"), (64, "favicon.png"), (32, "favicon-32.png")):
        img.resize((size, size), Image.LANCZOS).save(OUT / name, "PNG", optimize=True)
    print("logo ok", img.size)


def save_og():
    W, H = 1200, 630
    card = Image.new("RGB", (W, H), (10, 11, 16))
    # 柔光：画一个圆 → 高斯模糊 → 当遮罩贴暖色，避免多层叠加把整张图洗白
    from PIL import ImageFilter
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).ellipse([W // 2 - 150, 150 - 150, W // 2 + 150, 150 + 150], fill=110)
    mask = mask.filter(ImageFilter.GaussianBlur(120))
    glow = Image.new("RGB", (W, H), (255, 150, 110))
    card = Image.composite(glow, card, mask.point(lambda v: int(v * 0.55)))
    d = ImageDraw.Draw(card, "RGBA")
    logo = Image.open(OUT / "logo.png").convert("RGB").resize((190, 184), Image.LANCZOS)
    card.paste(logo, (W // 2 - 95, 120))
    f1 = ImageFont.truetype(FONT_LIGHT, 62)
    f2 = ImageFont.truetype(FONT_CN, 26)
    f3 = ImageFont.truetype(FONT_MONO, 15)
    title = "S.A.T.I.O Verse"
    tw = d.textlength(title, font=f1)
    d.text(((W - tw) / 2, 340), title, font=f1, fill=INK)
    sub = "能住在你电脑里的角色外壳"
    sw = d.textlength(sub, font=f2)
    d.text(((W - sw) / 2, 424), sub, font=f2, fill=INK2)
    tag = "LOCAL-FIRST · SELF-HOSTED · APACHE-2.0"
    d.text(((W - d.textlength(tag, font=f3)) / 2, 480), tag, font=f3, fill=(210, 160, 120))
    card.save(OUT / "og.png", "PNG", optimize=True)
    print("og ok")


def save_shots():
    """产品截图：压暗的 UI 直接放网页会糊成一团黑，统一提亮一点再导出。"""
    from PIL import ImageEnhance
    for name, src in SHOTS.items():
        im = Image.open(src).convert("RGB")
        if name == "panopticon.webp":
            im = im.crop((0, 0, im.width, int(im.height * 0.94)))
        w = 1600
        im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
        im = ImageEnhance.Brightness(im).enhance(1.22)
        im = ImageEnhance.Contrast(im).enhance(1.04)
        im.save(OUT / name, "WEBP", quality=82, method=6)
        print(name, im.size, round((OUT / name).stat().st_size / 1024), "KB")


if __name__ == "__main__":
    save_logo()
    save_og()
    save_shots()

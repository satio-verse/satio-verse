"""把项目品牌图标换成新 logo（彩虹丝带 S）。

源：用户 2026-11-24 给的新 logo（531×515，白底圆角方块）。
产出（覆盖，保持文件名不变，所有页面引用无需改）：
  desktop/web/brand/icon-64.png   64×64   favicon / 顶栏小图标
  desktop/web/brand/icon.png      512×512 应用图标
  desktop/web/brand/brand.png     800×1019 启动画面用（原为竖版，保留竖版比例）
  website 侧：site/assets/* 由 tools/build_site_assets.py 生成

用法：python tools/make_brand_icons.py [--also <另一个仓库路径>]
"""
import shutil
import sys
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
LOGO = Path(r"C:\Users\ASUS\.dsh\attachments\v1\objects\3d\3d6931f29c7ba99779952e4cf4410eb82228a10e1d5a80a4f3f9f391627f60e3")
BRAND = ROOT / "desktop" / "web" / "brand"


def square(img: Image.Image, size: int) -> Image.Image:
    """居中裁剪成正方形再缩放，保持圆角方块的完整边距。"""
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    return img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)


def main():
    src = Image.open(LOGO).convert("RGB")
    targets = [BRAND]
    if "--also" in sys.argv:
        targets.append(Path(sys.argv[sys.argv.index("--also") + 1]) / "desktop" / "web" / "brand")

    for brand in targets:
        if not brand.exists():
            print("跳过（目录不存在）", brand)
            continue
        square(src, 64).save(brand / "icon-64.png", "PNG", optimize=True)
        square(src, 512).save(brand / "icon.png", "PNG", optimize=True)
        # 启动画面沿用原来的竖版比例
        square(src, 1000).resize((800, 1019), Image.LANCZOS).save(brand / "brand.png", "PNG", optimize=True)
        print("brand ok ->", brand)

    # 顺手留一份原始来源，方便以后重生成
    keep = ROOT / "desktop" / "web" / "brand" / "logo-source-new.png"
    square(src, 512).save(keep, "PNG", optimize=True)
    print("source ->", keep)


if __name__ == "__main__":
    main()

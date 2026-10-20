"""生成应用图标 assets/icon.ico（Pillow，仅在打包前跑一次）。"""

from pathlib import Path

from PIL import Image, ImageDraw

ACCENT = (31, 111, 235)
PAPER = (255, 255, 255)
LINE = (203, 217, 241)
FOLD = (201, 217, 243)

SIZE = 1024
PLATE = (40, 40, SIZE - 40, SIZE - 40)
PAPER_BOX = (272, 196, 752, 828)
PAPER_RADIUS = 46
FOLD = 132


def build() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(PLATE, radius=220, fill=ACCENT)

    left, top, right, bottom = PAPER_BOX

    # 纸张单独一层，用圆角矩形做蒙版，保证折角不会溢出
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    layer_draw.rounded_rectangle(PAPER_BOX, radius=PAPER_RADIUS, fill=PAPER)
    layer_draw.polygon(
        [(right - FOLD, top), (right, top + FOLD), (right - FOLD, top + FOLD)],
        fill=FOLD,
    )
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(PAPER_BOX, radius=PAPER_RADIUS, fill=255)
    img.paste(layer, (0, 0), mask)

    # 文本行
    x0 = left + 72
    y = top + 232
    for width in (336, 336, 240, 336):
        draw.rounded_rectangle([x0, y, x0 + width, y + 40], radius=20, fill=LINE)
        y += 92

    return img


def main():
    out_dir = Path(__file__).resolve().parent.parent / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    image = build()
    ico_path = out_dir / "icon.ico"
    image.save(ico_path, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    image.resize((256, 256), Image.LANCZOS).save(out_dir / "icon.png")
    print(f"saved {ico_path} ({ico_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

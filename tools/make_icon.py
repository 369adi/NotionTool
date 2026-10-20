"""Build assets/notiontool.ico: a bold 'N' with a small coworker node graph (multi-size .ico).

Run: python tools/make_icon.py
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'assets' / 'notiontool.ico'
SIZE = 256


def font(size):
    for name in ('segoeuib.ttf', 'arialbd.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def draw():
    img = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Rounded square, dark base with a strong orange accent ring.
    d.rounded_rectangle((8, 8, SIZE - 8, SIZE - 8), radius=56, fill=(24, 25, 28, 255))
    d.rounded_rectangle((8, 8, SIZE - 8, SIZE - 8), radius=56, outline=(255, 122, 26, 255), width=12)
    # Big white N.
    f = font(170)
    box = d.textbbox((0, 0), 'N', font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((SIZE - w) / 2 - box[0] - 14, (SIZE - h) / 2 - box[1] - 6), 'N', font=f, fill=(255, 255, 255, 255))
    # Coworker graph: one Main node feeding two coworker nodes (bottom right).
    main, a, b = (196, 150), (176, 206), (222, 206)
    for p in (a, b):
        d.line((main, p), fill=(255, 122, 26, 255), width=7)
    for p, r in ((main, 17), (a, 13), (b, 13)):
        d.ellipse((p[0] - r, p[1] - r, p[0] + r, p[1] + r), fill=(255, 122, 26, 255), outline=(24, 25, 28, 255), width=4)
    return img


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    draw().save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    draw().save(OUT.with_suffix('.png'))
    print(OUT, OUT.stat().st_size)


if __name__ == '__main__':
    main()

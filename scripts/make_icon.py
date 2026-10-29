#!/usr/bin/env python3
"""Draw the Toolbox Launcher icon: a cream instrument faceplate with a green screen, three lamps and a switch.

    python3 scripts/make_icon.py        (needs Pillow; writes assets/icon.png, static/icon.png and assets/AppIcon.icns)
"""
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
S = 1024


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # body: a rounded square of cream enamel, lighter at the top
    body = Image.new("RGBA", (S, S))
    px = body.load()
    for y in range(S):
        t = y / (S - 1)
        c = (int(242 - 24 * t), int(236 - 27 * t), int(220 - 36 * t), 255)
        for x in range(S):
            px[x, y] = c
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((60, 60, S - 60, S - 60), radius=210, fill=255)
    img.paste(body, (0, 0), mask)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((60, 60, S - 60, S - 60), radius=210, outline=(150, 139, 112, 255), width=8)
    for sx, sy in ((150, 150), (S - 150, 150), (150, S - 150), (S - 150, S - 150)):      # screws
        d.ellipse((sx - 22, sy - 22, sx + 22, sy + 22), fill=(170, 162, 142, 255), outline=(110, 102, 84, 255), width=4)
        d.line((sx - 14, sy - 8, sx + 14, sy + 8), fill=(80, 74, 60, 255), width=6)

    # the screen
    sx0, sy0, sx1, sy1 = 150, 230, S - 150, 700
    d.rounded_rectangle((sx0, sy0, sx1, sy1), radius=56, fill=(6, 19, 12, 255), outline=(27, 25, 21, 255), width=14)
    for i in range(1, 8):                      # graticule
        x = sx0 + (sx1 - sx0) * i / 8
        d.line((x, sy0 + 18, x, sy1 - 18), fill=(141, 255, 182, 40 if i != 4 else 70), width=3)
    for i in range(1, 5):
        y = sy0 + (sy1 - sy0) * i / 5
        d.line((sx0 + 18, y, sx1 - 18, y), fill=(141, 255, 182, 40), width=3)

    # the trace, with a soft glow underneath
    mid = (sy0 + sy1) / 2 + 30
    pts = [(sx0 + 40, mid), (360, mid), (410, mid - 36), (450, mid), (500, mid), (540, mid + 70), (600, mid - 250),
           (660, mid + 120), (700, mid), (770, mid), (815, mid - 60), (860, mid), (sx1 - 60, mid)]
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).line(pts, fill=(141, 255, 182, 150), width=40, joint="curve")
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(22)))
    d = ImageDraw.Draw(img)
    d.line(pts, fill=(170, 255, 200, 255), width=17, joint="curve")
    hx, hy = pts[-1]
    d.ellipse((hx - 26, hy - 26, hx + 26, hy + 26), fill=(233, 244, 242, 255))

    # two lamps (on, busy) and a toggle switch in the on position
    ly = 818
    for lx, col in ((270, (12, 163, 12, 255)), (410, (250, 178, 25, 255))):
        d.ellipse((lx - 46, ly - 46, lx + 46, ly + 46), fill=(38, 34, 27, 255))
        d.ellipse((lx - 36, ly - 36, lx + 36, ly + 36), fill=col)
        d.ellipse((lx - 20, ly - 24, lx - 2, ly - 6), fill=(255, 255, 255, 150))
    d.rounded_rectangle((600, ly - 62, 800, ly + 62), radius=40, fill=(38, 34, 27, 255))
    d.rounded_rectangle((706, ly - 46, 784, ly + 46), radius=34, fill=(236, 231, 218, 255), outline=(150, 144, 128, 255), width=6)
    return img


def main() -> None:
    img = draw()
    (ROOT / "assets").mkdir(exist_ok=True)
    img.save(ROOT / "assets" / "icon.png")
    img.resize((256, 256), Image.LANCZOS).save(ROOT / "static" / "icon.png")
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            img.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
            img.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ROOT / "assets" / "AppIcon.icns")], check=True)
    print("wrote assets/icon.png, static/icon.png, assets/AppIcon.icns")


if __name__ == "__main__":
    main()

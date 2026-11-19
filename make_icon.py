"""Draws the Game Guide icon (gamepad with a question mark) -> web/icon.ico + web/icon.png."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent


def draw(size: int = 256) -> Image.Image:
    s = size / 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8 * s, 8 * s, 248 * s, 248 * s), radius=56 * s, fill=(44, 72, 190, 255))
    # gamepad body
    pad = (0xF2, 0xF4, 0xFA, 255)
    d.rounded_rectangle((34 * s, 92 * s, 222 * s, 186 * s), radius=46 * s, fill=pad)
    d.ellipse((30 * s, 120 * s, 104 * s, 214 * s), fill=pad)
    d.ellipse((152 * s, 120 * s, 226 * s, 214 * s), fill=pad)
    blue = (44, 72, 190, 255)
    # d-pad
    d.rectangle((62 * s, 128 * s, 76 * s, 164 * s), fill=blue)
    d.rectangle((51 * s, 139 * s, 87 * s, 153 * s), fill=blue)
    # buttons
    for cx, cy in ((186, 130), (204, 148), (168, 148), (186, 166)):
        d.ellipse(((cx - 8) * s, (cy - 8) * s, (cx + 8) * s, (cy + 8) * s), fill=blue)
    # question mark bubble
    d.ellipse((96 * s, 22 * s, 160 * s, 86 * s), fill=(255, 196, 61, 255))
    try:
        font = ImageFont.truetype("arialbd.ttf", int(50 * s))
    except OSError:
        font = ImageFont.load_default()
    d.text((128 * s, 55 * s), "?", font=font, fill=(30, 30, 40, 255), anchor="mm")
    return img


if __name__ == "__main__":
    img = draw(256)
    img.save(BASE / "web" / "icon.png")
    img.save(BASE / "web" / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icon written")

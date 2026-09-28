"""Draw the admin panel's app icons (services/admin_web/icons/).

The panel installs as a web app on a phone, and a home-screen icon has to be
a real PNG at fixed sizes. The artwork is simple on purpose: the panel's own
dark background, a blue "POL" and the green status dot from its header.

    python tools/make_admin_icons.py

Needs Pillow and a bold font (Segoe UI Bold on Windows, DejaVu elsewhere).
Re-run only when the design changes; the PNGs are committed.
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, os.pardir, "services", "admin_web", "icons")
BG, PANEL, ACCENT, GOOD, TEXT = "#14161c", "#1c1f28", "#5aa9ff", "#57d38c", "#e6e8ee"
FONTS = [r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def font(px):
    for f in FONTS:
        if os.path.exists(f):
            return ImageFont.truetype(f, px)
    sys.exit("no bold font found; add one to FONTS")


def draw(size, maskable=False):
    """`maskable` fills the whole square: launchers crop it to their own shape,
    and only the centre 80% is guaranteed to show, so the art is kept there."""
    s = size * 4                                   # draw big, then downsample
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if maskable:
        d.rectangle([0, 0, s, s], fill=BG)
        inset = s * 0.10
    else:
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=s * 0.22, fill=BG)
        inset = 0
    box = s - 2 * inset
    # a panel-coloured card, like the ones the dashboard is made of
    pad = inset + box * 0.12
    d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=box * 0.14,
                        fill=PANEL, outline="#333949", width=max(2, s // 128))
    f = font(int(box * 0.30))
    text = "POL"
    w = d.textlength(text, font=f)
    asc, desc = f.getmetrics()
    d.text(((s - w) / 2, s / 2 - (asc + desc) / 2 - box * 0.04), text,
           font=f, fill=ACCENT)
    # the header's green "online" dot, and an underline bar
    r = box * 0.055
    cx, cy = s / 2, s / 2 + box * 0.21
    d.rounded_rectangle([cx - box * 0.20, cy - box * 0.012,
                         cx + box * 0.11, cy + box * 0.012],
                        radius=box * 0.012, fill=TEXT)
    d.ellipse([cx + box * 0.16 - r, cy - r, cx + box * 0.16 + r, cy + r], fill=GOOD)
    return im.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    for size in (32, 180, 192, 512):
        draw(size).save(os.path.join(OUT, "icon-%d.png" % size), optimize=True)
    # iOS ignores transparency and draws it black; give it the solid variant
    draw(180, maskable=True).save(os.path.join(OUT, "apple-touch-icon.png"),
                                  optimize=True)
    draw(512, maskable=True).save(os.path.join(OUT, "maskable-512.png"),
                                  optimize=True)
    print("wrote", sorted(os.listdir(OUT)))


if __name__ == "__main__":
    main()

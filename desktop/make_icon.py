"""Disegna l'icona del programma (richiede Pillow).

    python desktop/make_icon.py

Crea in ``desktop/assets``:

* ``icon-mac.png``  1024x1024 sulla griglia delle icone macOS (corpo 824 px
  con angoli arrotondati, margine trasparente e ombra leggera);
* ``icon-win.png``  1024x1024 a tutta superficie, per l'icona Windows.

PyInstaller converte da sé il PNG in ``.icns`` / ``.ico`` (serve Pillow).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ASSETS = Path(__file__).resolve().parent / "assets"
SCALE = 4                      # si disegna 4 volte più grande e si riduce: bordi morbidi
TOP = (255, 94, 122)           # rosa (in alto a sinistra)
BOTTOM = (110, 92, 255)        # indaco (in basso a destra)


def gradient(size: int) -> Image.Image:
    """Sfumatura diagonale dal rosa all'indaco."""
    small = Image.new("RGB", (256, 256))
    px = small.load()
    for y in range(256):
        for x in range(256):
            t = min(1.0, max(0.0, (x * 0.35 + y * 0.65) / 255))
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM))
    return small.resize((size, size), Image.BICUBIC)


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def glyph(size: int) -> Image.Image:
    """Freccia di download sopra un vassoio, bianca, centrata."""
    g = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(g)
    s = size / 1024
    stroke = round(74 * s)
    cx = size / 2
    # asta
    d.rounded_rectangle((cx - stroke / 2, 250 * s, cx + stroke / 2, 590 * s), radius=stroke / 2, fill=255)
    # punta: due tratti a 45°
    for sign in (-1, 1):
        x0, y0 = cx + sign * 190 * s, 430 * s
        x1, y1 = cx, 620 * s
        d.line((x0, y0, x1, y1), fill=255, width=stroke)
        d.ellipse((x0 - stroke / 2, y0 - stroke / 2, x0 + stroke / 2, y0 + stroke / 2), fill=255)
    d.ellipse((cx - stroke / 2, 620 * s - stroke / 2, cx + stroke / 2, 620 * s + stroke / 2), fill=255)
    # vassoio
    tray_y = 730 * s
    d.rounded_rectangle((260 * s, tray_y, 764 * s, tray_y + stroke), radius=stroke / 2, fill=255)
    return g


def body(size: int, radius: int) -> Image.Image:
    """Corpo dell'icona: sfumatura, riflesso in alto, simbolo bianco con ombra morbida."""
    base = gradient(size).convert("RGBA")
    # riflesso: schiarisce la metà alta
    shine = Image.linear_gradient("L").rotate(180).resize((size, size)).point(lambda v: max(0, v - 150) * 0.32)
    base = Image.composite(Image.new("RGBA", (size, size), (255, 255, 255, 255)), base, shine)
    # simbolo con ombra
    mark = glyph(size)
    shadow = mark.filter(ImageFilter.GaussianBlur(size / 90))
    shadow = ImageChops.offset(shadow, 0, round(size / 110)).point(lambda v: v * 0.28)
    base = Image.composite(Image.new("RGBA", (size, size), (40, 20, 90, 255)), base, shadow)
    base = Image.composite(Image.new("RGBA", (size, size), (255, 255, 255, 255)), base, mark)
    base.putalpha(rounded_mask(size, radius))
    return base


def mac_icon() -> Image.Image:
    """Griglia Apple: tela 1024, corpo 824 con raggio ~185, ombra sotto."""
    canvas = 1024 * SCALE
    size = 824 * SCALE
    offset = (canvas - size) // 2
    icon = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    shadow = Image.new("L", (canvas, canvas), 0)
    shadow.paste(rounded_mask(size, 185 * SCALE), (offset, offset + 12 * SCALE))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14 * SCALE)).point(lambda v: v * 0.35)
    icon.paste((0, 0, 0, 255), (0, 0), shadow)
    part = body(size, 185 * SCALE)
    icon.alpha_composite(part, (offset, offset))
    return icon.resize((1024, 1024), Image.LANCZOS)


def win_icon() -> Image.Image:
    """Windows: corpo quasi a tutta tela (le icone Windows non hanno il margine Apple)."""
    canvas = 1024 * SCALE
    size = 960 * SCALE
    offset = (canvas - size) // 2
    icon = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    icon.alpha_composite(body(size, 200 * SCALE), (offset, offset))
    return icon.resize((1024, 1024), Image.LANCZOS)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    mac_icon().save(ASSETS / "icon-mac.png")
    win_icon().save(ASSETS / "icon-win.png")
    print(f"Icone scritte in {ASSETS}")


if __name__ == "__main__":
    main()

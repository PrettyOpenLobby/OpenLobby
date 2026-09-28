"""Extract the Viewer's own font data for the admin panel's PML preview.

The output is derived from the Viewer's own font files, so it is NOT committed
to this repository: run this against your own install to produce it. Without
it the preview draws text with a browser font instead (pml.js falls back when
the atlas is missing).

Writes services/admin_web/pmlfont/:
  glyphs00.png  the main glyph atlas (system/font/polfnt_00_8bit.png) as
                grayscale, value = coverage; the browser makes it an alpha mask.
  font.json     {"cell": 16, "em": 15.5, "widths": [7 faces x 224 advances for
                0x20..0xFF, from common/ppfont.bin], "jis": a string whose
                character at index i is the glyph in atlas cell i}

The atlas is 16x16 cells, 32 per row, in JIS X 0208 order:
linear = (row-1)*94 + (cell-1), verified by rendering. The asset files are
obfuscated on disk; `--codec` names a directory holding a `pmlcodec` module
whose `decode(path)` returns `(plain_bytes, kind)`. That codec is not part of
this repository.

    python tools/make_pml_fonts.py --codec DIR [--viewer "C:/Program Files (x86)/.../viewer"]
"""
import argparse
import io
import json
import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, os.pardir, "services", "admin_web", "pmlfont")
DEFAULT_VIEWER = (r"C:\Program Files (x86)\PlayOnline\SquareEnix"
                  r"\PlayOnlineViewer\viewer")


def jis_order():
    """The character in each atlas cell, by JIS X 0208 row/cell (EUC-JP)."""
    out = []
    for row in range(1, 95):
        for cell in range(1, 95):
            try:
                ch = bytes([0xA0 + row, 0xA0 + cell]).decode("euc_jp")
            except UnicodeDecodeError:
                ch = "\0"
            out.append(ch if len(ch) == 1 else "\0")
    return "".join(out).rstrip("\0")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--viewer", default=DEFAULT_VIEWER)
    ap.add_argument("--codec", required=True,
                    help="directory holding a pmlcodec module (decode(path))")
    a = ap.parse_args(argv)
    sys.path.insert(0, os.path.abspath(a.codec))
    import pmlcodec

    atlas_path = os.path.join(a.viewer, "data", "system", "font", "polfnt_00_8bit.png")
    dec, _kind = pmlcodec.decode(atlas_path)
    im = Image.open(io.BytesIO(dec))
    cov = Image.frombytes("L", im.size, im.tobytes())   # palette index = coverage
    os.makedirs(OUT, exist_ok=True)
    # Grayscale: the value IS the coverage. Half the size of an RGBA copy; the
    # browser turns it into an alpha mask once when it loads.
    cov.save(os.path.join(OUT, "glyphs00.png"), optimize=True)

    pp = open(os.path.join(a.viewer, "data", "common", "ppfont.bin"), "rb").read()
    n = pp[0]
    widths = [list(pp[16 + f * 224:16 + (f + 1) * 224]) for f in range(n)]

    jis = jis_order()
    with open(os.path.join(OUT, "font.json"), "w", encoding="utf-8") as f:
        json.dump({"cell": 16, "em": 15.5, "cols": 32, "atlas": list(im.size),
                   "source": "PlayOnline Viewer 1.18 ppfont.bin + polfnt_00_8bit.png",
                   "widths": widths, "jis": jis}, f, ensure_ascii=False,
                  separators=(",", ":"))
    print("atlas", im.size, "faces", n, "jis cells", len(jis),
          "->", os.path.abspath(OUT))


if __name__ == "__main__":
    main()
